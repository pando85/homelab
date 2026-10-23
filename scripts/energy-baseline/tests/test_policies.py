"""Tests for the benchmark policies — baselines, causality, and honest losses.

The causal policy must be *strictly* causal (current price and past state only),
must never break a physics invariant, must clamp its **discharge** rather than
export (charging is *not* clamped to the load — charging raises import, so the
no-export rule puts no bound on it), must limit charging to the headroom a
configured site import ceiling leaves (and charge nothing where the measured
load already used it all — without ever failing on such history), and may cost
more than the no-battery baseline.  Gap exclusion must be identical for every
policy.


The day-ahead policy (added as ADR-0003's deferred fourth rung) is tested for the
things it is actually claimed to do: a load forecast that cannot read the day it
forecasts, a plan that costs money on the *measured* load and never on the forecast,
end-of-day SOC valued by a single cross-day number, clipping instead of export when
the forecast was optimistic, and a day whose MILP refuses to solve degrading to
hold-SOC rather than aborting the roll."""

from __future__ import annotations

import dataclasses
import math
from collections.abc import Sequence
from datetime import date
from typing import Any

import numpy as np
import pytest

from energy_baseline.battery import (
    BatteryPhysicsError,
    BatterySpec,
    Horizon,
    ceiling_exceedance_indices,
    check_feasible,
    simulate,
)
from energy_baseline.oracle import solve_oracle
from energy_baseline.policies import (
    FORECAST_FALLBACK_RULE,
    DayAheadDay,
    contiguous_day_bounds,
    fit_thresholds,
    forecast_load_trailing_slot_mean,
    policy_causal_price,
    policy_day_ahead,
    policy_no_battery,
)

HOUR = 3600.0
QUARTER = 900.0


def spec(**overrides) -> BatterySpec:
    base = dict(
        capacity_kwh=5.12,
        power_kw=2.5,
        charge_efficiency=0.95,
        discharge_efficiency=0.95,
        soc_min_frac=0.0,
        soc_max_frac=1.0,
        initial_soc_frac=0.2,
        terminal_soc_frac=None,
    )
    base.update(overrides)
    return BatterySpec(**base)


def horizon(prices, load, usable=None, interval_seconds=QUARTER, import_limit_kw=None) -> Horizon:
    n = len(prices)
    return Horizon(
        interval_seconds,
        prices,
        load,
        [True] * n if usable is None else usable,
        import_limit_kw,
    )


# ---------------------------------------------------------------------------
# Policy 1 — no battery
# ---------------------------------------------------------------------------


class TestNoBattery:
    def test_zero_flows_grid_equals_load(self) -> None:
        s = spec()
        h = horizon([0.1, 0.2, 0.3], [1.0, 0.5, 2.0])
        sched = policy_no_battery(s, h)
        assert np.all(sched.charge_kwh == 0.0)
        assert np.all(sched.discharge_kwh == 0.0)
        assert np.allclose(sched.grid_kwh, h.load_kwh)
        assert sched.throughput_kwh == 0.0
        assert sched.efc == 0.0

    def test_cost_is_the_counterfactual_baseline(self) -> None:
        s = spec()
        prices = [0.1, 0.2, 0.3]
        load = [1.0, 0.5, 2.0]
        h = horizon(prices, load)
        sched = policy_no_battery(s, h)
        assert sched.cost_eur == pytest.approx(float(np.dot(prices, load)))

    def test_unusable_intervals_excluded_from_baseline(self) -> None:
        s = spec()
        prices = [0.1, 9.9, 0.3]  # the gap interval would dominate if counted
        load = [1.0, 1.0, 1.0]
        h = horizon(prices, load, usable=[True, False, True])
        sched = policy_no_battery(s, h)
        assert sched.cost_eur == pytest.approx(0.4)
        assert np.all(sched.grid_kwh == h.load_kwh)  # flows zero, load untouched

    def test_soc_constant_at_initial_and_not_applicable_solver(self) -> None:
        s = spec(initial_soc_frac=0.35)
        h = horizon([0.1] * 4, [1.0] * 4)
        sched = policy_no_battery(s, h)
        assert np.allclose(sched.soc_kwh, 5.12 * 0.35)
        assert sched.soc_min_frac == pytest.approx(0.35)
        assert sched.soc_max_frac == pytest.approx(0.35)
        assert sched.solver_status == "not_applicable"
        assert sched.solver_gap is None
        assert sched.bound_kind == "none"
        assert sched.is_certified is False
        # the baseline is always a feasible plan for the simulator
        assert check_feasible(s, h, sched.charge_kwh, sched.discharge_kwh) == []


# ---------------------------------------------------------------------------
# fit_thresholds — training data only
# ---------------------------------------------------------------------------


class TestFitThresholds:
    def test_percentiles_are_exact_and_ordered(self) -> None:
        train = np.arange(1.0, 101.0)  # 1..100
        ct, dt = fit_thresholds(train, charge_pct=25.0, discharge_pct=75.0)
        assert ct == pytest.approx(np.percentile(train, 25.0))
        assert dt == pytest.approx(np.percentile(train, 75.0))
        assert ct == pytest.approx(25.75)
        assert dt == pytest.approx(75.25)
        assert ct < dt

    def test_negative_prices_are_valid_training_values(self) -> None:
        train = np.array([-0.2, -0.1, 0.0, 0.1, 0.2])
        ct, dt = fit_thresholds(train, charge_pct=20.0, discharge_pct=80.0)
        assert ct == pytest.approx(-0.12)
        assert dt == pytest.approx(0.12)

    def test_thresholds_do_not_see_the_evaluation_window(self) -> None:
        """Changing evaluation prices must not change the fitted thresholds."""
        train = np.array([0.05, 0.10, 0.15, 0.20, 0.25, 0.30])
        before = fit_thresholds(train, charge_pct=25.0, discharge_pct=75.0)
        eval_prices = np.array([9.0, 9.0, 9.0, -9.0, -9.0])
        after = fit_thresholds(train, charge_pct=25.0, discharge_pct=75.0)
        assert before == after
        # and the policy consumes them as frozen scalars: recomputing on the
        # *evaluation* array would give a different, leaky pair
        leaky = fit_thresholds(
            np.concatenate([train, eval_prices]), charge_pct=25.0, discharge_pct=75.0
        )
        assert leaky != before

    def test_training_slice_length_changes_the_fit(self) -> None:
        """A shorter training window gives different thresholds (train-only)."""
        full = np.linspace(0.0, 1.0, 200)
        first_half = full[: full.size // 2]
        a = fit_thresholds(full, charge_pct=25.0, discharge_pct=75.0)
        b = fit_thresholds(first_half, charge_pct=25.0, discharge_pct=75.0)
        assert a != b
        assert b[0] == pytest.approx(np.percentile(first_half, 25.0))

    def test_identical_training_prices_gives_a_degenerate_but_valid_pair(self) -> None:
        ct, dt = fit_thresholds(np.full(50, 0.2), charge_pct=25.0, discharge_pct=75.0)
        assert ct == pytest.approx(dt) == pytest.approx(0.2)
        s = spec()
        h = horizon([0.2, 0.2], [1.0, 1.0])
        sched = policy_causal_price(s, h, charge_threshold=ct, discharge_threshold=dt)
        assert np.all(sched.charge_kwh == 0.0) and np.all(sched.discharge_kwh == 0.0)

    @pytest.mark.parametrize(
        ("kwargs", "match"),
        [
            (dict(charge_pct=-1.0, discharge_pct=75.0), "percentiles"),
            (dict(charge_pct=25.0, discharge_pct=101.0), "percentiles"),
            (dict(charge_pct=80.0, discharge_pct=20.0), "must be <= discharge_pct"),
        ],
    )
    def test_bad_percentiles_raise(self, kwargs: dict, match: str) -> None:
        with pytest.raises(ValueError, match=match):
            fit_thresholds(np.arange(10.0), **kwargs)

    def test_empty_and_non_finite_training_raise(self) -> None:
        with pytest.raises(ValueError, match="empty"):
            fit_thresholds(np.array([]), charge_pct=25.0, discharge_pct=75.0)
        with pytest.raises(ValueError, match="non-finite"):
            fit_thresholds(np.array([0.1, np.nan]), charge_pct=25.0, discharge_pct=75.0)

    def test_accepts_lists(self) -> None:
        ct, dt = fit_thresholds([0.1, 0.2, 0.3, 0.4], charge_pct=0.0, discharge_pct=100.0)
        assert ct == pytest.approx(0.1)
        assert dt == pytest.approx(0.4)


# ---------------------------------------------------------------------------
# Policy 2 — causal price rule
# ---------------------------------------------------------------------------


class TestCausalPriceBehaviour:
    def test_charges_below_and_discharges_above_thresholds(self) -> None:
        s = spec(capacity_kwh=10.0, power_kw=1.0, charge_efficiency=1.0, discharge_efficiency=1.0)
        prices = [0.05, 0.05, 0.20, 0.20, 0.50, 0.50]
        h = horizon(prices, [2.0] * 6, interval_seconds=HOUR)
        sched = policy_causal_price(s, h, charge_threshold=0.10, discharge_threshold=0.40)
        assert np.all(sched.charge_kwh[:2] > 0.0)
        assert np.all(sched.charge_kwh[2:] == 0.0)
        assert np.all(sched.discharge_kwh[:4] == 0.0)
        assert np.all(sched.discharge_kwh[4:] > 0.0)
        assert np.allclose(sched.charge_kwh[:2], [1.0, 1.0])  # full power
        assert np.allclose(sched.discharge_kwh[4:], [1.0, 1.0])

    def test_idle_band_leaves_flows_at_zero(self) -> None:
        s = spec(capacity_kwh=10.0, power_kw=1.0, initial_soc_frac=0.5)
        prices = [0.25, 0.25, 0.25]
        h = horizon(prices, [2.0] * 3, interval_seconds=HOUR)
        sched = policy_causal_price(s, h, charge_threshold=0.2, discharge_threshold=0.3)
        assert np.all(sched.charge_kwh == 0.0)
        assert np.all(sched.discharge_kwh == 0.0)
        assert np.allclose(sched.grid_kwh, h.load_kwh)
        assert sched.cost_eur == policy_no_battery(s, h).cost_eur

    def test_equal_thresholds_are_allowed(self) -> None:
        s = spec(capacity_kwh=10.0, power_kw=1.0, initial_soc_frac=0.5)
        h = horizon([0.1, 0.3], [2.0, 2.0], interval_seconds=HOUR)
        sched = policy_causal_price(s, h, charge_threshold=0.2, discharge_threshold=0.2)
        assert sched.charge_kwh[0] > 0.0
        assert sched.discharge_kwh[1] > 0.0

    def test_inverted_thresholds_rejected(self) -> None:
        s = spec()
        h = horizon([0.1, 0.2], [1.0, 1.0])
        with pytest.raises(ValueError, match="must be <= discharge_threshold"):
            policy_causal_price(s, h, charge_threshold=0.5, discharge_threshold=0.2)

    def test_non_finite_thresholds_rejected(self) -> None:
        s = spec()
        h = horizon([0.1, 0.2], [1.0, 1.0])
        with pytest.raises(ValueError, match="finite"):
            policy_causal_price(s, h, charge_threshold=np.nan, discharge_threshold=0.5)
        with pytest.raises(ValueError, match="finite"):
            policy_causal_price(s, h, charge_threshold=0.1, discharge_threshold=np.inf)

    def test_headroom_limits_charge_below_full_power(self) -> None:
        s = spec(capacity_kwh=10.0, power_kw=5.0, soc_max_frac=1.0, initial_soc_frac=0.8)
        h = horizon([0.01] * 4, [9.0] * 4, interval_seconds=HOUR)
        sched = policy_causal_price(s, h, charge_threshold=0.05, discharge_threshold=0.5)
        # 2 kWh of headroom at eta_c = 0.95 -> 2.105 kWh of AC charge, first interval
        expected = (10.0 - 8.0) / s.charge_efficiency
        assert sched.charge_kwh[0] == pytest.approx(min(5.0, expected))
        assert sched.soc_kwh[0] == pytest.approx(10.0, abs=1e-9)
        assert np.all(sched.charge_kwh[1:] == 0.0)
        assert np.all(sched.soc_kwh <= 10.0 + 1e-9)

    def test_soc_floor_limits_discharge(self) -> None:
        """An explicit terminal SOC below the seed opens the floor to window_min."""
        s = spec(
            capacity_kwh=10.0,
            power_kw=5.0,
            soc_min_frac=0.2,
            initial_soc_frac=0.5,
            terminal_soc_frac=0.2,
        )
        h = horizon([0.9] * 4, [9.0] * 4, interval_seconds=HOUR)
        sched = policy_causal_price(s, h, charge_threshold=0.05, discharge_threshold=0.5)
        available = (10.0 * 0.5 - 10.0 * 0.2) * s.discharge_efficiency
        assert sched.discharge_kwh[0] == pytest.approx(available)
        assert sched.soc_kwh[0] == pytest.approx(10.0 * 0.2, abs=1e-9)
        # float residue (~4e-16 kWh) is below the simulator's tolerance
        assert np.all(sched.discharge_kwh[1:] < 1e-9)
        # discharged all the way to its own terminal target: fully feasible plan
        assert check_feasible(s, h, sched.charge_kwh, sched.discharge_kwh) == []

    def test_seed_energy_is_not_free(self) -> None:
        """With terminal SOC required back to the seed, the policy may not spend
        the energy that was already in the battery when the window opened."""
        s = spec(capacity_kwh=10.0, power_kw=1.0, initial_soc_frac=0.5)
        h = horizon([0.9, 0.9, 0.9], [5.0] * 3, interval_seconds=HOUR)
        sched = policy_causal_price(s, h, charge_threshold=0.05, discharge_threshold=0.5)
        assert np.all(sched.soc_kwh >= 10.0 * 0.5 - 1e-9)
        assert np.all(sched.discharge_kwh == 0.0)  # nothing charged yet to give back

    def test_explicit_terminal_soc_becomes_the_discharge_floor(self) -> None:
        s = spec(capacity_kwh=10.0, power_kw=1.0, initial_soc_frac=0.5, terminal_soc_frac=0.5)
        h = horizon([0.01, 0.9, 0.9], [5.0] * 3, interval_seconds=HOUR)
        sched = policy_causal_price(s, h, charge_threshold=0.05, discharge_threshold=0.5)
        # charged 1.0 kWh at t0 (SOC 5 -> 5.95 kWh), then may only give back eta_c*eta_d
        assert sched.charge_kwh[0] == pytest.approx(1.0)
        assert sched.discharge_kwh[1] == pytest.approx(1.0 * 0.95 * 0.95)
        assert np.all(sched.soc_kwh >= 10.0 * 0.5 - 1e-9)


class TestCausalIsCausal:
    def test_prefix_of_horizon_reproduces_prefix_of_plan(self) -> None:
        """Decisions for t < k cannot change when intervals t >= k are revealed."""
        rng = np.random.default_rng(5)
        n = 96
        prices = rng.uniform(0.0, 0.4, n)
        load = rng.uniform(0.3, 2.0, n)
        s = spec()
        h_full = horizon(prices, load)
        ct, dt = fit_thresholds(prices[:48], charge_pct=25.0, discharge_pct=75.0)
        full = policy_causal_price(s, h_full, charge_threshold=ct, discharge_threshold=dt)
        for k in (1, 7, 48, 95):
            head = horizon(prices[:k], load[:k])
            partial = policy_causal_price(s, head, charge_threshold=ct, discharge_threshold=dt)
            assert np.allclose(partial.charge_kwh, full.charge_kwh[:k]), k
            assert np.allclose(partial.discharge_kwh, full.discharge_kwh[:k]), k
            assert np.allclose(partial.soc_kwh, full.soc_kwh[:k]), k
            assert partial.cost_eur == pytest.approx(float(np.dot(prices[:k], full.grid_kwh[:k])))

    def test_future_price_shock_does_not_change_today(self) -> None:
        rng = np.random.default_rng(6)
        n = 24
        base_prices = rng.uniform(0.05, 0.35, n)
        load = rng.uniform(0.5, 2.0, n)
        s = spec()
        ct, dt = fit_thresholds(base_prices, charge_pct=25.0, discharge_pct=75.0)
        first = policy_causal_price(
            s, horizon(base_prices, load), charge_threshold=ct, discharge_threshold=dt
        )
        shock = base_prices.copy()
        shock[12:] = 5.0  # an unseen spike late in the horizon
        second = policy_causal_price(
            s, horizon(shock, load), charge_threshold=ct, discharge_threshold=dt
        )
        assert np.allclose(first.charge_kwh[:12], second.charge_kwh[:12])
        assert np.allclose(first.discharge_kwh[:12], second.discharge_kwh[:12])

    def test_unusable_interval_later_does_not_change_earlier_decisions(self) -> None:
        rng = np.random.default_rng(7)
        n = 30
        prices = rng.uniform(0.0, 0.4, n)
        load = rng.uniform(0.5, 2.0, n)
        s = spec()
        ct, dt = fit_thresholds(prices, charge_pct=25.0, discharge_pct=75.0)
        clean = policy_causal_price(
            s, horizon(prices, load), charge_threshold=ct, discharge_threshold=dt
        )
        masked = np.ones(n, dtype=bool)
        masked[20:] = False
        later = policy_causal_price(
            s, horizon(prices, load, usable=masked), charge_threshold=ct, discharge_threshold=dt
        )
        assert np.allclose(clean.charge_kwh[:20], later.charge_kwh[:20])
        assert np.all(later.charge_kwh[20:] == 0.0)
        assert np.all(later.discharge_kwh[20:] == 0.0)


# ---------------------------------------------------------------------------
# Clamping the discharge, not the charge
# ---------------------------------------------------------------------------


class TestClamping:
    def test_discharge_is_clamped_to_load(self) -> None:
        s = spec(
            capacity_kwh=10.0,
            power_kw=5.0,
            charge_efficiency=1.0,
            discharge_efficiency=1.0,
            initial_soc_frac=0.2,
            terminal_soc_frac=0.0,
        )
        # terminal 0.0 == soc_min, so the discharge floor is the window floor
        h = horizon([0.9, 0.9], [0.2, 0.2], interval_seconds=HOUR)
        sched = policy_causal_price(s, h, charge_threshold=0.05, discharge_threshold=0.5)
        assert sched.discharge_kwh[0] == pytest.approx(0.2)  # clamped to load, not exported
        assert np.all(sched.grid_kwh >= 0.0)
        assert np.allclose(sched.grid_kwh, [0.0, 0.0])
        assert (
            check_feasible(s, h, sched.charge_kwh, sched.discharge_kwh, enforce_terminal_soc=False)
            == []
        )
        # an unclamped discharge of the same size would have been an export
        assert any("intentional export" in v for v in check_feasible(s, h, [0.0, 0.0], [1.0, 0.0]))

    def test_charge_is_not_clamped_to_load(self) -> None:
        """Removed spec rule: ``charge[t] <= load[t]`` was physically wrong.

        Charging raises import (``grid = load + charge - discharge``), so the
        no-export rule cannot bound it.  A 5 kW charger in front of a 0.3 kWh
        interval load is now limited by the power rating alone, and import rises
        to 5.3 kWh instead of the charge being cut to 0.3.
        """
        s = spec(capacity_kwh=10.0, power_kw=5.0, initial_soc_frac=0.2)
        h = horizon([0.01, 0.01], [0.3, 0.3], interval_seconds=HOUR)
        sched = policy_causal_price(s, h, charge_threshold=0.05, discharge_threshold=0.5)
        assert sched.charge_kwh[0] == pytest.approx(5.0)  # full power, not min(5.0, 0.3)
        assert sched.grid_kwh[0] == pytest.approx(5.3)  # import went *up*
        assert np.all(sched.grid_kwh > h.load_kwh)  # charging *added* to the import
        assert np.all(sched.grid_kwh >= 0.0)
        assert (
            check_feasible(s, h, sched.charge_kwh, sched.discharge_kwh, enforce_terminal_soc=False)
            == []
        )

    def test_low_load_low_price_night_charges_at_full_power(self) -> None:
        """The regression this fix exists for: 200 W baseload, cheap night hours.

        15-minute intervals, 5.12 kWh / 2.5 kW nameplate, 0.05 kWh of load per
        interval (200 W) and prices below the charge threshold.  The policy takes
        the full ``power_kw * h`` = 0.625 kWh per interval; the old rule capped it
        at the 0.05 kWh of load, a 12.5x loss of charging power.
        """
        s = spec(
            capacity_kwh=5.12,
            power_kw=2.5,
            charge_efficiency=0.95,
            discharge_efficiency=0.95,
            soc_min_frac=0.05,
            soc_max_frac=0.95,
            initial_soc_frac=0.05,
        )
        n = 16
        load = np.full(n, 0.2 * 0.25)  # 200 W for 15 min
        prices = np.full(n, 0.02)
        h = horizon(prices, load)
        sched = policy_causal_price(s, h, charge_threshold=0.10, discharge_threshold=0.30)

        limit = s.interval_energy_limit_kwh(h.interval_seconds)
        assert limit == pytest.approx(0.625)
        assert sched.charge_kwh[0] == pytest.approx(limit)
        assert sched.charge_kwh[0] == pytest.approx(12.5 * load[0])
        assert np.all(sched.charge_kwh[:7] == pytest.approx(limit))
        # the old rule: charge <= load, i.e. 0.05 kWh per interval
        old_rule_charge = np.minimum(limit, load)
        assert np.all(sched.charge_kwh[:7] > old_rule_charge[:7])
        # import only ever went up: 0.05 + 0.625 = 0.675 kWh, never negative
        assert np.allclose(sched.grid_kwh[:7], load[:7] + limit)
        assert np.all(sched.grid_kwh >= load)
        assert np.all(sched.grid_kwh >= 0.0)
        assert np.all(sched.discharge_kwh == 0.0)
        assert (
            check_feasible(s, h, sched.charge_kwh, sched.discharge_kwh, enforce_terminal_soc=False)
            == []
        )

    def test_night_fill_time_is_set_by_the_power_rating_not_the_load(self) -> None:
        """4.608 kWh of usable window needs 4.8505 kWh of AC energy: 8 intervals
        (2.0 h) at 2.5 kW, versus 98 intervals (~24.5 h) under the removed
        ``charge <= load`` rule at a 200 W baseload."""
        s = spec(
            capacity_kwh=5.12,
            power_kw=2.5,
            charge_efficiency=0.95,
            discharge_efficiency=0.95,
            soc_min_frac=0.05,
            soc_max_frac=0.95,
            initial_soc_frac=0.05,
        )
        n = 24
        load = np.full(n, 0.05)
        h = horizon(np.full(n, 0.02), load)
        sched = policy_causal_price(s, h, charge_threshold=0.10, discharge_threshold=0.30)

        limit = s.interval_energy_limit_kwh(h.interval_seconds)
        ac_needed = s.usable_window_kwh / s.charge_efficiency
        ceil_kwh = s.soc_bound_kwh("max")
        filled = np.flatnonzero(sched.soc_kwh >= ceil_kwh - 1e-9)
        assert filled.size > 0
        assert filled[0] == math.ceil(ac_needed / limit) - 1 == 7
        assert (filled[0] + 1) * h.interval_hours == pytest.approx(2.0)
        # the last top-up interval is headroom-limited, not load-limited
        assert sched.charge_kwh[7] == pytest.approx(ac_needed - 7 * limit, abs=1e-9)
        assert sched.charge_kwh[7] > load[0]
        assert np.all(sched.charge_kwh[8:] < 1e-12)  # ceiling reached: nothing left to take
        # what the removed rule would have needed, same numbers
        assert math.ceil(ac_needed / load[0]) == 98  # 98 * 15 min = 24.5 h

    def test_full_power_charging_never_makes_import_negative(self) -> None:
        """Randomized: sub-threshold prices, baseload-only load, no export ever."""
        rng = np.random.default_rng(70)
        s = spec(
            capacity_kwh=5.12,
            power_kw=2.5,
            charge_efficiency=0.95,
            discharge_efficiency=0.95,
            soc_min_frac=0.05,
            soc_max_frac=0.95,
            initial_soc_frac=0.5,
        )
        for trial in range(40):
            n = 12 + trial
            prices = rng.uniform(-0.1, 0.09, n)  # every interval is a charge signal
            load = rng.uniform(0.0, 0.08, n)
            h = horizon(prices, load)
            sched = policy_causal_price(s, h, charge_threshold=0.10, discharge_threshold=0.30)
            assert np.array_equal(
                sched.grid_kwh, h.load_kwh + sched.charge_kwh - sched.discharge_kwh
            )
            assert np.all(sched.grid_kwh >= 0.0), trial
            assert np.all(sched.discharge_kwh == 0.0), trial
            assert np.all(sched.charge_kwh <= s.interval_energy_limit_kwh(QUARTER) + 1e-12)
            assert np.any(sched.charge_kwh > load), trial  # the cap really is gone
            assert (
                check_feasible(
                    s, h, sched.charge_kwh, sched.discharge_kwh, enforce_terminal_soc=False
                )
                == []
            ), trial

    def test_zero_load_means_no_discharge(self) -> None:
        s = spec(capacity_kwh=10.0, power_kw=5.0, initial_soc_frac=0.5)
        h = horizon([0.9, 0.9], [0.0, 0.0], interval_seconds=HOUR)
        sched = policy_causal_price(s, h, charge_threshold=0.05, discharge_threshold=0.5)
        assert np.all(sched.discharge_kwh == 0.0)
        assert np.allclose(sched.grid_kwh, 0.0)

    def test_aggressive_price_signal_never_breaks_invariants(self) -> None:
        """0 vs 10 EUR/kWh sawtooth, absurd power, tiny loads."""
        rng = np.random.default_rng(9)
        n = 500
        prices = np.where(rng.random(n) > 0.5, 10.0, 0.0)
        load = rng.uniform(0.0, 0.4, n)
        s = spec(
            capacity_kwh=5.12,
            power_kw=50.0,
            charge_efficiency=0.97,
            discharge_efficiency=0.97,
            soc_min_frac=0.05,
            soc_max_frac=0.95,
            initial_soc_frac=0.5,
        )
        h = horizon(prices, load)
        sched = policy_causal_price(s, h, charge_threshold=2.0, discharge_threshold=8.0)
        assert np.all(sched.grid_kwh >= -1e-12)
        assert np.all(
            (sched.soc_kwh >= s.soc_bound_kwh("min") - 1e-9)
            & (sched.soc_kwh <= s.soc_bound_kwh("max") + 1e-9)
        )
        limit = s.interval_energy_limit_kwh(h.interval_seconds)
        assert np.all((sched.charge_kwh <= limit + 1e-12) & (sched.discharge_kwh <= limit + 1e-12))
        assert not np.any((sched.charge_kwh > 1e-9) & (sched.discharge_kwh > 1e-9))
        assert sched.soc_kwh[-1] >= s.soc_bound_kwh("init") - 1e-9  # seed never spent
        assert (
            check_feasible(s, h, sched.charge_kwh, sched.discharge_kwh, enforce_terminal_soc=False)
            == []
        )

    def test_unusable_intervals_get_zero_flow_in_both_policies(self) -> None:
        rng = np.random.default_rng(10)
        n = 60
        prices = rng.uniform(0.0, 0.4, n)
        load = rng.uniform(0.4, 1.6, n)
        usable = rng.random(n) > 0.25
        s = spec()
        h = horizon(prices, load, usable=usable)
        ct, dt = fit_thresholds(prices, charge_pct=25.0, discharge_pct=75.0)
        nb = policy_no_battery(s, h)
        ca = policy_causal_price(s, h, charge_threshold=ct, discharge_threshold=dt)
        assert np.all(nb.charge_kwh[~usable] == 0.0)
        assert np.all(ca.charge_kwh[~usable] == 0.0)
        assert np.all(ca.discharge_kwh[~usable] == 0.0)
        # cost basis: usable intervals only, same for both
        assert nb.cost_eur == pytest.approx(float(np.dot(prices[usable], load[usable])))
        assert ca.cost_eur == pytest.approx(float(np.dot(prices[usable], ca.grid_kwh[usable])))


# ---------------------------------------------------------------------------
# Site import ceiling (configured or unknown)
# ---------------------------------------------------------------------------


def night(n: int = 24, load_kwh: float = 0.05, price: float = 0.02) -> tuple[BatterySpec, Horizon]:
    """The representative case the charge cap broke: cheap, baseload-only hours."""
    s = spec(
        capacity_kwh=5.12,
        power_kw=2.5,
        charge_efficiency=0.95,
        discharge_efficiency=0.95,
        soc_min_frac=0.05,
        soc_max_frac=0.95,
        initial_soc_frac=0.05,
    )
    return s, horizon(np.full(n, price), np.full(n, load_kwh))


class TestImportCeiling:
    def test_unknown_ceiling_charges_at_full_power(self) -> None:
        s, h = night()
        assert h.import_limit_kw is None
        sched = policy_causal_price(s, h, charge_threshold=0.10, discharge_threshold=0.30)
        assert sched.charge_kwh[0] == pytest.approx(0.625)

    def test_configured_ceiling_clamps_charge_to_the_headroom(self) -> None:
        """1.0 kW contracted over 15 min = 0.25 kWh supplied per interval."""
        s, h0 = night()
        h = dataclasses.replace(h0, import_limit_kw=1.0)
        assert h.interval_import_limit_kwh == pytest.approx(0.25)
        sched = policy_causal_price(s, h, charge_threshold=0.10, discharge_threshold=0.30)
        assert sched.charge_kwh[0] == pytest.approx(0.20)  # 0.25 - 0.05 of load
        assert np.all(h.load_kwh + sched.charge_kwh <= 0.25 + 1e-12)
        assert np.all(sched.charge_kwh < s.interval_energy_limit_kwh(QUARTER))  # ceiling binds
        assert (
            check_feasible(s, h, sched.charge_kwh, sched.discharge_kwh, enforce_terminal_soc=False)
            == []
        )
        # without the ceiling the same horizon charges at the power rating
        free = policy_causal_price(s, h0, charge_threshold=0.10, discharge_threshold=0.30)
        assert np.any(free.charge_kwh > sched.charge_kwh + 1e-9)

    def test_ceiling_spent_by_the_load_means_zero_charge(self) -> None:
        """0.2 kW over 15 min = 0.05 kWh = exactly the baseload: nothing left over."""
        s, h = night()
        tight = dataclasses.replace(h, import_limit_kw=0.2)
        sched = policy_causal_price(s, tight, charge_threshold=0.10, discharge_threshold=0.30)
        assert np.all(sched.charge_kwh == 0.0)
        assert np.allclose(sched.grid_kwh, tight.load_kwh)
        assert check_feasible(s, tight, sched.charge_kwh, sched.discharge_kwh) == []

    def test_charge_is_zero_in_a_ceiling_exceeding_interval(self) -> None:
        """Where the load tops the ceiling the policy charges nothing — and nothing else.

        Intervals 2 and 3 carry a load above the 1.0 kW ceiling (0.25 kWh per
        15-minute interval); the rest is ordinary baseload.  The policy charges
        the residual headroom on the quiet intervals and exactly zero on the
        breaching ones, and the horizon's observation counts those two.
        """
        s = spec(
            capacity_kwh=5.12,
            power_kw=2.5,
            charge_efficiency=0.95,
            discharge_efficiency=0.95,
            soc_min_frac=0.05,
            soc_max_frac=0.95,
            initial_soc_frac=0.05,
        )
        load = np.array([0.05, 0.05, 0.30, 0.50, 0.05, 0.05])
        prices = np.full(6, 0.02)  # every interval is a charge signal
        h = horizon(prices, load, import_limit_kw=1.0)
        assert h.interval_import_limit_kwh == pytest.approx(0.25)
        assert np.array_equal(ceiling_exceedance_indices(h, load), [2, 3])
        sched = policy_causal_price(s, h, charge_threshold=0.10, discharge_threshold=0.30)
        assert sched.charge_kwh[2] == 0.0
        assert sched.charge_kwh[3] == 0.0
        # the quiet intervals charge the headroom the load leaves: 0.25 - 0.05
        assert np.all(sched.charge_kwh[[0, 1, 4, 5]] == pytest.approx(0.20))
        assert sched.ceiling_exceeding_intervals == 2
        # net import above the ceiling is a violation of nothing
        assert (
            check_feasible(s, h, sched.charge_kwh, sched.discharge_kwh, enforce_terminal_soc=False)
            == []
        )
        # and the grid identity holds even where the load dominates
        assert np.allclose(sched.grid_kwh[[2, 3]], load[[2, 3]])

    def test_discharge_still_works_when_the_ceiling_is_breached(self) -> None:
        """A breaching interval is precisely where discharge is most useful.

        Seed the battery, price everything as a discharge signal, and give the
        household a load above the ceiling: the policy discharges into it (never
        more than the load, so no export) while the ceiling stays a charge-side
        rule.
        """
        s = spec(
            capacity_kwh=5.12,
            power_kw=2.5,
            charge_efficiency=0.95,
            discharge_efficiency=0.95,
            soc_min_frac=0.05,
            soc_max_frac=0.95,
            initial_soc_frac=0.95,
            terminal_soc_frac=0.05,
        )
        load = np.array([0.5, 0.5, 0.5])
        h = horizon(np.full(3, 0.9), load, import_limit_kw=1.0)  # 0.25 kWh < 0.5 kWh load
        assert np.array_equal(ceiling_exceedance_indices(h, load), [0, 1, 2])
        sched = policy_causal_price(s, h, charge_threshold=0.10, discharge_threshold=0.30)
        assert np.all(sched.charge_kwh == 0.0)
        assert np.all(sched.discharge_kwh == pytest.approx(0.5))  # capped at the load
        assert np.all(sched.grid_kwh == pytest.approx(0.0))
        # discharging under a breached ceiling is not a violation...
        assert (
            check_feasible(s, h, sched.charge_kwh, sched.discharge_kwh, enforce_terminal_soc=False)
            == []
        )
        # ...and here it even removes the exceedance: grid 0.0 <= ceiling 0.25
        assert sched.ceiling_exceeding_intervals == 0

    def test_generous_ceiling_is_identical_to_none(self) -> None:
        """50 kW never binds, so the plan must be bit-for-bit unchanged."""
        s, h0 = night(n=48)
        h = dataclasses.replace(h0, import_limit_kw=50.0)
        free = policy_causal_price(s, h0, charge_threshold=0.10, discharge_threshold=0.30)
        capped = policy_causal_price(s, h, charge_threshold=0.10, discharge_threshold=0.30)
        assert np.array_equal(free.charge_kwh, capped.charge_kwh)
        assert np.array_equal(free.discharge_kwh, capped.discharge_kwh)
        assert free.cost_eur == capped.cost_eur
        # a ceiling of 50 kW is never exceeded by this load, so the observation
        # is zero — while the unknown ceiling reports zero too, and neither raises
        assert free.ceiling_exceeding_intervals == 0
        assert capped.ceiling_exceeding_intervals == 0
        nb_free = policy_no_battery(s, h0)
        nb_capped = policy_no_battery(s, h)
        assert np.array_equal(nb_free.grid_kwh, nb_capped.grid_kwh)
        assert nb_free.cost_eur == nb_capped.cost_eur

    def test_headroom_is_exactly_the_residual_under_a_just_below_load(self) -> None:
        """Load at 0.24 kWh under a 0.25 kWh ceiling leaves 0.01 kWh — and no more.

        The policy takes the *residual*, not the power rating: a 2.5 kW battery
        over 15 minutes could take 0.625 kWh, so a plan that ignored the headroom
        would be 60x this.  One more unit of load and the headroom is zero.
        """
        s = spec(
            capacity_kwh=5.12,
            power_kw=2.5,
            charge_efficiency=0.95,
            discharge_efficiency=0.95,
            soc_min_frac=0.05,
            soc_max_frac=0.95,
            initial_soc_frac=0.05,
        )
        ceiling_kwh = 0.25  # 1.0 kW over 15 min
        load = np.array([ceiling_kwh - 0.01] * 4)  # 0.24: just under, never over
        h = horizon(np.full(4, 0.02), load, import_limit_kw=1.0)
        sched = policy_causal_price(s, h, charge_threshold=0.10, discharge_threshold=0.30)
        assert np.all(sched.charge_kwh[:1] == pytest.approx(0.01))
        assert np.all(h.load_kwh + sched.charge_kwh <= ceiling_kwh + 1e-12)
        assert sched.ceiling_exceeding_intervals == 0
        assert s.interval_energy_limit_kwh(QUARTER) == pytest.approx(0.625)  # not the binding cap
        # the exact-ceiling case leaves nothing
        at_ceiling = dataclasses.replace(h, load_kwh=np.full(4, ceiling_kwh))
        zero = policy_causal_price(s, at_ceiling, charge_threshold=0.10, discharge_threshold=0.30)
        assert np.all(zero.charge_kwh == 0.0)
        assert zero.ceiling_exceeding_intervals == 0  # equal is not above

    def test_realistic_spike_case(self) -> None:
        """Regression mirroring the archive: 5.0 kW contract, 6.9 kW spike.

        Six 15-minute intervals of ~0.5 kW baseload with one ~6.9 kW interval
        (mirroring the sustained 2026-10-18 event the supply tolerated).  The
        battery charges normally on either side of the spike, charges nothing
        during it, and the measured baseline is reported unchanged.
        """
        s = spec(
            capacity_kwh=5.12,
            power_kw=2.5,
            charge_efficiency=0.95,
            discharge_efficiency=0.95,
            soc_min_frac=0.05,
            soc_max_frac=0.95,
            initial_soc_frac=0.05,
        )
        # 0.5 kW over 15 min = 0.125 kWh; 6.9 kW over 15 min = 1.725 kWh
        load = np.array([0.125, 0.125, 1.725, 0.125, 0.125, 0.125])
        prices = np.full(6, 0.02)  # all cheap: every quiet interval is a charge signal
        h = horizon(prices, load, interval_seconds=QUARTER, import_limit_kw=5.0)
        ceiling = h.interval_import_limit_kwh
        assert ceiling == pytest.approx(1.25)  # 5.0 kW * 0.25 h
        assert np.array_equal(ceiling_exceedance_indices(h, load), [2])
        assert np.all(
            h.charge_headroom_kwh == pytest.approx([1.125, 1.125, 0.0, 1.125, 1.125, 1.125])
        )

        ca = policy_causal_price(s, h, charge_threshold=0.10, discharge_threshold=0.30)
        # charging normally either side of the spike: the 2.5 kW rating (0.625 kWh
        # per interval) binds before the 1.125 kWh of headroom does
        assert np.all(ca.charge_kwh[[0, 1, 3]] == pytest.approx(0.625))
        assert ca.charge_kwh[2] == 0.0  # nothing to take during the spike
        assert ca.grid_kwh[2] == pytest.approx(1.725)  # the measured load, unopposed
        assert ca.ceiling_exceeding_intervals == 1
        assert (
            check_feasible(s, h, ca.charge_kwh, ca.discharge_kwh, enforce_terminal_soc=False) == []
        )

        # the baseline is measured history: same load, same cost, one observation
        nb = policy_no_battery(s, h)
        assert np.allclose(nb.grid_kwh, load)
        assert nb.ceiling_exceeding_intervals == 1
        assert nb.charge_kwh.sum() == 0.0
        assert nb.cost_eur == pytest.approx(float(np.dot(prices, load)))

        # with a larger rating the same spike still blocks only its own interval
        big = dataclasses.replace(s, power_kw=5.0)
        ca_big = policy_causal_price(big, h, charge_threshold=0.10, discharge_threshold=0.30)
        assert np.all(ca_big.charge_kwh[[0, 1, 3]] == pytest.approx(1.125))  # headroom binds
        assert ca_big.charge_kwh[2] == 0.0

    def test_no_battery_never_raises_on_a_breaching_horizon(self) -> None:
        """The baseline is a report, so no ceiling value can make it fail."""
        s, h0 = night(n=12)
        for limit_kw in (0.0, 0.1, 0.5, 5.0, 50.0):  # 0.0 kW: a ceiling nothing can sit under
            h = dataclasses.replace(h0, import_limit_kw=limit_kw)
            try:
                sched = policy_no_battery(s, h)
            except BatteryPhysicsError as exc:  # pragma: no cover - the point of this test
                pytest.fail(f"import_limit_kw={limit_kw} made the baseline infeasible: {exc}")
            assert np.allclose(sched.grid_kwh, h.load_kwh)
            assert np.all(sched.charge_kwh == 0.0)
            assert check_feasible(s, h, sched.charge_kwh, sched.discharge_kwh) == []
            # every interval breaches except at the ceilings that clear the load
            assert sched.ceiling_exceeding_intervals == (0 if limit_kw * 0.25 >= 0.05 else 12)
            # the causal policy is equally unshakeable; it charges only where the
            # ceiling leaves headroom, and exactly nothing where it leaves none
            causal = policy_causal_price(s, h, charge_threshold=0.10, discharge_threshold=0.30)
            headroom = h.charge_headroom_kwh
            assert np.all(causal.charge_kwh <= headroom + 1e-12)
            if limit_kw * 0.25 < 0.05:
                assert np.all(causal.charge_kwh == 0.0)
            assert causal.ceiling_exceeding_intervals == sched.ceiling_exceeding_intervals

    def test_no_battery_plan_still_validates_under_a_real_ceiling(self) -> None:
        """charge is always 0 there, so the ceiling only has to cover the load."""
        s, h0 = night()
        h = dataclasses.replace(h0, import_limit_kw=2.0)
        sched = policy_no_battery(s, h)
        assert np.all(sched.charge_kwh == 0.0)
        assert check_feasible(s, h, sched.charge_kwh, sched.discharge_kwh) == []

    def test_ceiling_below_the_measured_load_is_an_observation(self) -> None:
        """A 0.1 kW ceiling under a 0.05 kWh/interval load is history, not an error.

        No dispatch can fix it by charging less, so neither policy raises: the
        no-battery baseline *is* that measured history, and the causal policy
        charges nothing where the headroom is zero.  The count lands on both
        schedules as an observation.
        """
        s, h = night()
        impossible = dataclasses.replace(h, import_limit_kw=0.1)  # 0.025 kWh < 0.05 kWh load
        nb = policy_no_battery(s, impossible)
        assert nb.ceiling_exceeding_intervals == h.n
        ca = policy_causal_price(s, impossible, charge_threshold=0.10, discharge_threshold=0.30)
        assert ca.ceiling_exceeding_intervals == h.n
        assert np.all(ca.charge_kwh == 0.0)
        assert check_feasible(s, impossible, nb.charge_kwh, nb.discharge_kwh) == []
        assert (
            check_feasible(
                s, impossible, ca.charge_kwh, ca.discharge_kwh, enforce_terminal_soc=False
            )
            == []
        )

    def test_ceiling_never_touches_the_discharge_side(self) -> None:
        """The no-export bound on discharge stays ``discharge <= load`` in both modes."""
        s = spec(
            capacity_kwh=5.12,
            power_kw=2.5,
            charge_efficiency=0.95,
            discharge_efficiency=0.95,
            soc_min_frac=0.05,
            soc_max_frac=0.95,
            initial_soc_frac=0.95,
            terminal_soc_frac=0.05,
        )
        load = np.full(12, 0.05)
        prices = np.full(12, 0.9)  # every interval is a discharge signal
        for limit_kw in (None, 0.2, 1.0, 50.0):  # 0.2 kW = the load exactly: zero charge headroom
            h = horizon(prices, load, import_limit_kw=limit_kw)
            sched = policy_causal_price(s, h, charge_threshold=0.10, discharge_threshold=0.30)
            assert np.all(sched.discharge_kwh <= load + 1e-12), limit_kw
            assert np.all(sched.discharge_kwh <= s.interval_energy_limit_kwh(QUARTER) + 1e-12)
            assert np.all(sched.grid_kwh >= 0.0), limit_kw
            assert np.all(sched.charge_kwh == 0.0), limit_kw

    def test_ceiling_binds_before_the_soc_window_when_it_is_tighter(self) -> None:
        """0.3 kW (0.075 kWh/interval) minus 0.05 kWh load = 0.025 kWh of charge."""
        s, h0 = night()
        h = dataclasses.replace(h0, import_limit_kw=0.3)
        sched = policy_causal_price(s, h, charge_threshold=0.10, discharge_threshold=0.30)
        assert np.all(sched.charge_kwh[:20] == pytest.approx(0.025))
        assert np.all(h.load_kwh + sched.charge_kwh <= 0.075 + 1e-12)


# ---------------------------------------------------------------------------
# Losses are reported, not prevented
# ---------------------------------------------------------------------------


class TestLossesAreHonest:
    def test_causal_policy_can_cost_more_than_no_battery(self) -> None:
        """Round-trip loss (0.5 x 0.5) is worse than the 10% price spread: the
        battery loses money, and the benchmark must show that."""
        s = spec(
            capacity_kwh=10.0,
            power_kw=1.0,
            charge_efficiency=0.5,
            discharge_efficiency=0.5,
            initial_soc_frac=0.0,
        )
        prices = [0.10, 0.10, 0.11, 0.11]
        h = horizon(prices, [2.0] * 4, interval_seconds=HOUR)
        nb = policy_no_battery(s, h)
        ca = policy_causal_price(s, h, charge_threshold=0.105, discharge_threshold=0.105)
        assert ca.cost_eur > nb.cost_eur
        assert ca.savings_vs(nb) < 0.0
        assert ca.throughput_kwh > 0.0  # it really did cycle
        assert not ca.is_certified
        assert (
            check_feasible(s, h, ca.charge_kwh, ca.discharge_kwh, enforce_terminal_soc=False) == []
        )

    def test_negative_savings_still_representable_after_clamping(self) -> None:
        """A plan that would lose money is reported as-is; nothing floors it."""
        s = spec(
            capacity_kwh=10.0,
            power_kw=1.0,
            charge_efficiency=0.4,
            discharge_efficiency=0.4,
            initial_soc_frac=0.0,
        )
        prices = [0.05, 0.06]
        h = horizon(prices, [5.0, 5.0], interval_seconds=HOUR)
        nb = policy_no_battery(s, h)
        sched = policy_causal_price(s, h, charge_threshold=0.055, discharge_threshold=0.055)
        assert sched.cost_eur == pytest.approx(
            nb.cost_eur + 0.05 * sched.charge_kwh[0] - 0.06 * sched.discharge_kwh[1]
        )
        assert sched.cost_eur > nb.cost_eur
        # the same plan, evaluated by simulate() directly, agrees
        same = simulate(s, h, sched.charge_kwh, sched.discharge_kwh, enforce_terminal_soc=False)
        assert same.cost_eur == pytest.approx(sched.cost_eur)
        assert same.efc == pytest.approx(sched.efc)

    def test_causal_beats_baseline_when_spread_beats_losses(self) -> None:
        s = spec(
            capacity_kwh=10.0,
            power_kw=1.0,
            charge_efficiency=0.95,
            discharge_efficiency=0.95,
            initial_soc_frac=0.0,
        )
        prices = [0.05, 0.05, 0.40, 0.40]
        h = horizon(prices, [2.0] * 4, interval_seconds=HOUR)
        nb = policy_no_battery(s, h)
        ca = policy_causal_price(s, h, charge_threshold=0.10, discharge_threshold=0.30)
        assert ca.cost_eur < nb.cost_eur
        assert ca.savings_vs(nb) == pytest.approx(nb.cost_eur - ca.cost_eur)


# ---------------------------------------------------------------------------
# Negative prices and the one-binary-mode rule survive the removed charge cap
# ---------------------------------------------------------------------------


class TestNegativePricesAndBinaryMode:
    def test_negative_prices_are_a_charging_opportunity_not_free_money(self) -> None:
        """A negative price now buys full-power charging; the import side of the
        meter goes up (paying the household), and nothing is discharged at the
        same time."""
        s = spec(
            capacity_kwh=5.12,
            power_kw=2.5,
            charge_efficiency=0.95,
            discharge_efficiency=0.95,
            soc_min_frac=0.05,
            soc_max_frac=0.95,
            initial_soc_frac=0.5,
        )
        n = 24
        load = np.full(n, 0.05)
        prices = np.full(n, -0.35)
        h = horizon(prices, load)
        sched = policy_causal_price(s, h, charge_threshold=0.0, discharge_threshold=0.3)
        nb = policy_no_battery(s, h)
        assert sched.charge_kwh[0] == pytest.approx(0.625)
        assert np.all(sched.discharge_kwh == 0.0)
        assert not np.any((sched.charge_kwh > 1e-9) & (sched.discharge_kwh > 1e-9))
        assert sched.cost_eur < nb.cost_eur  # paid to import, honestly
        assert np.all(sched.grid_kwh >= 0.0)
        assert (
            check_feasible(s, h, sched.charge_kwh, sched.discharge_kwh, enforce_terminal_soc=False)
            == []
        )

    def test_sawtooth_across_zero_never_charges_and_discharges_at_once(self) -> None:
        """Cheap negative hours and expensive hours interleaved at 15-min steps."""
        rng = np.random.default_rng(71)
        s = spec(
            capacity_kwh=5.12,
            power_kw=2.5,
            charge_efficiency=0.95,
            discharge_efficiency=0.95,
            soc_min_frac=0.05,
            soc_max_frac=0.95,
            initial_soc_frac=0.5,
        )
        for trial in range(25):
            n = 40 + trial
            prices = np.where(
                rng.random(n) > 0.5, rng.uniform(0.2, 0.6, n), rng.uniform(-0.5, 0.0, n)
            )
            load = rng.uniform(0.0, 0.3, n)
            h = horizon(prices, load)
            sched = policy_causal_price(s, h, charge_threshold=0.05, discharge_threshold=0.25)
            assert not np.any((sched.charge_kwh > 1e-9) & (sched.discharge_kwh > 1e-9)), trial
            assert np.all(sched.grid_kwh >= 0.0), trial
            assert np.all(sched.discharge_kwh <= load + 1e-12), trial  # export rule intact
            assert np.any(sched.charge_kwh > load), trial  # charge cap really is gone
            assert (
                check_feasible(
                    s, h, sched.charge_kwh, sched.discharge_kwh, enforce_terminal_soc=False
                )
                == []
            ), trial

    def test_full_power_charging_can_still_lose_to_the_baseline(self) -> None:
        """Bigger charge currents make a bad signal hurt *more*, and it is still
        reported as a loss — nothing here clips or floors the result."""
        s = spec(
            capacity_kwh=5.12,
            power_kw=2.5,
            charge_efficiency=0.5,
            discharge_efficiency=0.5,
            soc_min_frac=0.0,
            soc_max_frac=1.0,
            initial_soc_frac=0.0,
        )
        n = 8
        load = np.full(n, 0.05)  # baseload: the old rule would have throttled charge
        prices = np.array([0.10] * 4 + [0.11] * 4)
        h = horizon(prices, load)
        nb = policy_no_battery(s, h)
        ca = policy_causal_price(s, h, charge_threshold=0.105, discharge_threshold=0.105)
        assert np.all(ca.charge_kwh[:4] > load[:4])
        assert ca.throughput_kwh > 0.0
        assert ca.cost_eur > nb.cost_eur
        assert ca.savings_vs(nb) < 0.0


# ---------------------------------------------------------------------------
# Determinism / purity
# ---------------------------------------------------------------------------


class TestPurity:
    def test_inputs_are_not_mutated(self) -> None:
        s = spec()
        prices = np.array([0.05, 0.2, 0.4, 0.1])
        load = np.array([1.0, 1.0, 1.0, 1.0])
        usable = np.array([True, True, False, True])
        h = horizon(prices, load, usable=usable)
        p_copy, l_copy, u_copy = prices.copy(), load.copy(), usable.copy()
        policy_no_battery(s, h)
        policy_causal_price(s, h, charge_threshold=0.1, discharge_threshold=0.3)
        fit_thresholds(prices, charge_pct=25.0, discharge_pct=75.0)
        assert np.array_equal(prices, p_copy)
        assert np.array_equal(load, l_copy)
        assert np.array_equal(usable, u_copy)
        assert h.prices.shape == p_copy.shape

    def test_repeated_runs_are_identical(self) -> None:
        rng = np.random.default_rng(12)
        n = 120
        prices = rng.uniform(0.0, 0.4, n)
        load = rng.uniform(0.4, 1.8, n)
        s = spec()
        h = horizon(prices, load)
        ct, dt = fit_thresholds(prices, charge_pct=30.0, discharge_pct=70.0)
        first = policy_causal_price(s, h, charge_threshold=ct, discharge_threshold=dt)
        second = policy_causal_price(s, h, charge_threshold=ct, discharge_threshold=dt)
        assert first.cost_eur == second.cost_eur
        assert np.array_equal(first.charge_kwh, second.charge_kwh)
        assert np.array_equal(first.soc_kwh, second.soc_kwh)

    def test_23_hour_dst_day_is_just_fewer_intervals(self) -> None:
        """92 slots (spring forward) vs 96: same kernel, no special casing."""
        rng = np.random.default_rng(13)
        s = spec()
        for slots, label in ((92, "spring"), (96, "normal"), (100, "fall")):
            prices = rng.uniform(0.0, 0.4, slots)
            load = rng.uniform(0.4, 1.6, slots)
            h = horizon(prices, load)
            assert h.n == slots, label
            sched = policy_causal_price(s, h, charge_threshold=0.1, discharge_threshold=0.3)
            assert (
                check_feasible(
                    s, h, sched.charge_kwh, sched.discharge_kwh, enforce_terminal_soc=False
                )
                == []
            ), label


# ---------------------------------------------------------------------------
# Policy 3 — day-ahead load forecast
# ---------------------------------------------------------------------------

PER_DAY = 8  # short synthetic "local days": enough slots to test position alignment


def day_labels(n_days: int, per_day: int = PER_DAY) -> list[int]:
    """Integer local-day keys, ``per_day`` intervals each."""
    return [i // per_day for i in range(n_days * per_day)]


def ragged_labels(sizes: Sequence[int]) -> list[int]:
    """Integer local-day keys for days of *different* lengths (DST-shaped input)."""
    return [i for i, size in enumerate(sizes) for _ in range(size)]


def bounds(labels: Sequence[Any]) -> list[tuple[Any, int, int]]:
    return contiguous_day_bounds(labels)


class TestContiguousDayBounds:
    def test_groups_runs_in_order(self) -> None:
        labels = [date(2026, 1, 1), date(2026, 1, 1), date(2026, 1, 2), date(2026, 1, 3)]
        assert contiguous_day_bounds(labels) == [
            (date(2026, 1, 1), 0, 2),
            (date(2026, 1, 2), 2, 3),
            (date(2026, 1, 3), 3, 4),
        ]

    def test_ragged_days_are_the_norm_not_an_error(self) -> None:
        """92 / 96 / 100-slot DST days each become exactly one run."""
        labels = ragged_labels([92, 96, 100])
        runs = contiguous_day_bounds(labels)
        assert [stop - start for _label, start, stop in runs] == [92, 96, 100]

    def test_empty_input_raises(self) -> None:
        with pytest.raises(ValueError, match="empty"):
            contiguous_day_bounds([])

    def test_non_chronological_raises(self) -> None:
        """A label that reappears after a later one is a sort bug, not a new day."""
        labels = ragged_labels([4, 4, 4])
        labels[6] = 0
        with pytest.raises(ValueError, match="not chronological"):
            contiguous_day_bounds(labels)


class TestTrailingSlotForecast:
    def test_equals_manual_trailing_mean_per_position(self) -> None:
        rng = np.random.default_rng(4)
        n_days, per_day = 5, PER_DAY
        load = rng.uniform(0.2, 1.5, n_days * per_day)
        forecast, _diag = forecast_load_trailing_slot_mean(
            load, bounds(day_labels(n_days, per_day)), window_days=3
        )
        # Day 4, slot k: mean of the same slot of days 1, 2, 3 (day 0 is out of window).
        for k in range(per_day):
            expected = float(np.mean([load[d * per_day + k] for d in (1, 2, 3)]))
            assert forecast[4 * per_day + k] == pytest.approx(expected)

    def test_window_days_limits_the_lookback(self) -> None:
        n_days, per_day = 4, PER_DAY
        load = np.arange(1.0, n_days * per_day + 1.0)
        forecast, diag = forecast_load_trailing_slot_mean(
            load, bounds(day_labels(n_days, per_day)), window_days=2
        )
        target = 3 * per_day
        expected = float(np.mean([load[per_day + 2], load[2 * per_day + 2]]))
        assert forecast[target + 2] == pytest.approx(expected)
        assert diag[3]["window_days_available"] == 2
        assert diag[3]["window_days_requested"] == 2
        assert diag[3]["trailing_mean_samples_max"] == 2

    def test_leakage_forecast_of_day_d_ignores_day_d_entirely(self) -> None:
        """The crux: day D's own load is wildly different and the forecast never notices.

        If the forecast could read the day it is forecasting, the target day's huge
        values would dominate the mean and the assertion below would fail by a mile.
        """
        n_days, per_day = 5, PER_DAY
        load = np.full(n_days * per_day, 0.5)
        load[3 * per_day :] = 50.0  # days 3 and 4 are nothing like days 0..2
        forecast, diag = forecast_load_trailing_slot_mean(
            load, bounds(day_labels(n_days, per_day)), window_days=3
        )
        assert np.allclose(forecast[3 * per_day : 4 * per_day], 0.5)
        assert diag[3]["mae_kwh"] == pytest.approx(49.5)  # the error is disclosed
        # Day 4 legitimately sees days 1..3 — including day 3's shock, which by then is
        # history.  It must NOT see its own 50.0, hence 17.0 and not 50.0.
        assert np.allclose(forecast[4 * per_day :], (0.5 + 0.5 + 50.0) / 3)
        assert diag[4]["mae_kwh"] == pytest.approx(50.0 - (0.5 + 0.5 + 50.0) / 3)

    def test_perturbing_later_days_leaves_earlier_forecast_identical(self) -> None:
        n_days, per_day = 6, PER_DAY
        rng = np.random.default_rng(5)
        base = rng.uniform(0.2, 1.5, n_days * per_day)
        perturbed = base.copy()
        perturbed[4 * per_day :] = 40.0
        days = bounds(day_labels(n_days, per_day))
        f_base, _ = forecast_load_trailing_slot_mean(base, days, window_days=3)
        f_pert, _ = forecast_load_trailing_slot_mean(perturbed, days, window_days=3)
        assert np.array_equal(f_base[: 4 * per_day], f_pert[: 4 * per_day])

    def test_unusable_intervals_are_missing_not_zero(self) -> None:
        """A measurement hole must not drag the forecast toward free energy."""
        n_days, per_day = 3, PER_DAY
        load = np.full(n_days * per_day, 1.0)
        usable = np.ones(n_days * per_day, dtype=bool)
        usable[per_day : 2 * per_day] = False  # all of day 1 is a gap
        forecast, diag = forecast_load_trailing_slot_mean(
            load, bounds(day_labels(n_days, per_day)), window_days=2, usable=usable
        )
        # Day 2's window is days 0 and 1; only day 0 contributes, still 1.0 not 0.5.
        assert np.allclose(forecast[2 * per_day :], 1.0)
        assert diag[2]["window_days_available"] == 2
        assert diag[2]["window_days_with_usable_data"] == 1

    def test_first_day_of_data_has_no_history_and_says_so(self) -> None:
        n_days, per_day = 3, PER_DAY
        load = np.full(n_days * per_day, 0.7)
        forecast, diag = forecast_load_trailing_slot_mean(
            load, bounds(day_labels(n_days, per_day)), window_days=3
        )
        assert np.allclose(forecast[:per_day], 0.0)
        assert diag[0]["fallback"] == "zero_no_history"
        assert diag[0]["slots_zero_no_history"] == per_day
        assert diag[0]["window_days_available"] == 0

    def test_pooled_fallback_when_only_longer_days_have_the_slot(self) -> None:
        """A 100-slot fall-back day following 96-slot days: slots 96..99 have no twin."""
        labels = ragged_labels([4, 4, 6])
        load = np.array([1.0, 2.0, 3.0, 4.0, 10.0, 20.0, 30.0, 40.0, 0.0, 0.0, 0.0, 0.0, 0.0, 0.0])
        forecast, diag = forecast_load_trailing_slot_mean(load, bounds(labels), window_days=3)
        # Slots 0..3 have two prior samples each; slots 4..5 exist only on this long day.
        assert np.allclose(forecast[8:12], [5.5, 11.0, 16.5, 22.0])
        pooled = float(np.mean(load[:8]))
        assert np.allclose(forecast[12:14], pooled)
        assert diag[2]["slots_pooled_fallback"] == 2
        assert diag[2]["slots_using_trailing_mean"] == 4
        assert diag[2]["fallback"] == "pooled_window_mean"

    def test_diagnostics_shape_and_sums(self) -> None:
        n_days, per_day = 4, PER_DAY
        rng = np.random.default_rng(6)
        load = rng.uniform(0.1, 1.0, n_days * per_day)
        _forecast, diag = forecast_load_trailing_slot_mean(
            load, bounds(day_labels(n_days, per_day)), window_days=3
        )
        assert len(diag) == n_days
        assert diag[3]["intervals"] == per_day
        assert diag[3]["forecast_sum_kwh"] == pytest.approx(
            float(_forecast[3 * per_day : 4 * per_day].sum())
        )
        assert diag[3]["measured_sum_kwh"] == pytest.approx(float(load[3 * per_day :].sum()))
        assert diag[3]["bias_kwh"] == pytest.approx(
            float(_forecast[3 * per_day : 4 * per_day].mean() - load[3 * per_day :].mean())
        )
        assert diag[3]["forecast_fallback_rule"] == FORECAST_FALLBACK_RULE

    def test_rejects_bad_inputs(self) -> None:
        days = bounds(day_labels(2, PER_DAY))
        with pytest.raises(ValueError, match="window_days"):
            forecast_load_trailing_slot_mean([1.0] * 16, days, window_days=0)
        with pytest.raises(ValueError, match="empty"):
            forecast_load_trailing_slot_mean(np.array([]), days)
        with pytest.raises(ValueError, match="non-finite"):
            forecast_load_trailing_slot_mean(np.array([1.0, np.nan] + [1.0] * 14), days)
        with pytest.raises(ValueError, match="usable"):
            forecast_load_trailing_slot_mean(np.ones(16), days, usable=np.ones(8, dtype=bool))

    def test_never_mutates_its_inputs(self) -> None:
        n_days, per_day = 4, PER_DAY
        load = np.arange(1.0, n_days * per_day + 1)
        labels = day_labels(n_days, per_day)
        copy = load.copy()
        forecast, _diag = forecast_load_trailing_slot_mean(load, bounds(labels), window_days=2)
        assert np.array_equal(load, copy)
        assert not forecast.flags["WRITEABLE"]


# ---------------------------------------------------------------------------
# Policy 3 — the rolling day-ahead policy itself
# ---------------------------------------------------------------------------


def day_ahead_case(
    n_days: int = 5,
    per_day: int = PER_DAY,
    *,
    seed: int = 7,
    window_days: int = 3,
    import_limit_kw: float | None = None,
    eval_from: int = 3,
    usable: np.ndarray | None = None,
) -> dict[str, Any]:
    """A synthetic rolling-day-ahead fixture: history days then evaluation days."""
    rng = np.random.default_rng(seed)
    n = n_days * per_day
    hour = np.tile(np.arange(per_day), n_days)
    prices = np.where(hour < 3, 0.04, 0.24) + rng.uniform(0.0, 0.03, n)
    load = np.where(hour >= 5, 0.9, 0.25) + rng.uniform(0.0, 0.05, n)
    mask = np.ones(n, dtype=bool) if usable is None else usable
    labels = day_labels(n_days, per_day)
    runs = contiguous_day_bounds(labels)
    forecast, diag = forecast_load_trailing_slot_mean(
        load, runs, window_days=window_days, usable=mask
    )
    start = eval_from * per_day
    horizon = Horizon(QUARTER, prices[start:], load[start:], mask[start:], import_limit_kw)
    days = [
        DayAheadDay(
            label=f"D{i}",
            start=lo - start,
            stop=hi - start,
            forecast_load=forecast[lo:hi],
            forecast=diag[i],
        )
        for i, (label, lo, hi) in enumerate(runs)
        if i >= eval_from
    ]
    return {
        "prices": prices,
        "load": load,
        "usable": mask,
        "runs": runs,
        "forecast": forecast,
        "diagnostics": diag,
        "horizon": horizon,
        "days": days,
        "start": start,
        "per_day": per_day,
        "terminal_value": float(prices[: eval_from * per_day].mean()),
    }


class TestDayAheadPolicy:
    def test_plan_is_feasible_against_measured_load(self) -> None:
        s = spec()
        case = day_ahead_case()
        sched, _tele = policy_day_ahead(
            s,
            case["horizon"],
            case["days"],
            terminal_value_eur_per_kwh=case["terminal_value"],
            time_limit_s=5.0,
            mip_rel_gap=1e-6,
            mip_abs_gap=1e-9,
        )
        assert (
            check_feasible(
                s,
                case["horizon"],
                sched.charge_kwh,
                sched.discharge_kwh,
                enforce_terminal_soc=False,
            )
            == []
        )
        assert np.all(sched.grid_kwh >= -1e-9)
        assert sched.solver_status == "feasible"
        assert sched.bound_kind == "none"
        assert sched.is_certified is False
        assert sched.solver_gap is None

    def test_cost_is_meter_plus_telescoped_inventory(self) -> None:
        """Same accounting as the free oracle: cost == meter - scoring_value*(terminal - initial).

        The scoring terminal value (default 0.0) is what the Schedule uses for cost_eur
        and inventory_adjustment_eur, so the comparison with the oracle is on the same
        basis.  The internal planning value V is used inside each daily objective to
        prevent the battery from emptying at midnight, and is recorded in the telemetry
        as internal_terminal_value_eur_per_kwh.  The per-day objectives sum to the
        internal-V-based cost, which is recorded as internal_daily_objective_sum_eur and
        is NOT the common score.
        """
        s = spec()
        case = day_ahead_case()
        internal_v = 0.2
        scoring_v = 0.0  # default
        sched, tele = policy_day_ahead(
            s,
            case["horizon"],
            case["days"],
            terminal_value_eur_per_kwh=internal_v,
            scoring_terminal_value_eur_per_kwh=scoring_v,
            time_limit_s=5.0,
            mip_rel_gap=1e-6,
            mip_abs_gap=1e-9,
        )
        # The reported cost uses the scoring value, not the internal planning value.
        expected_adjustment = -scoring_v * (sched.terminal_soc_kwh - sched.initial_soc_kwh)
        assert sched.inventory_adjustment_eur == pytest.approx(expected_adjustment, abs=1e-12)
        assert sched.cost_eur == pytest.approx(
            sched.meter_cost_eur + sched.inventory_adjustment_eur, abs=1e-12
        )
        assert sched.terminal_value_eur_per_kwh == pytest.approx(scoring_v)
        # The telemetry records both values.
        assert tele["terminal_value_eur_per_kwh"] == pytest.approx(internal_v)
        assert tele["scoring_terminal_value_eur_per_kwh"] == pytest.approx(scoring_v)
        # The per-day roll-up must agree with the kernel's single whole-horizon pass.
        assert tele["meter_cost_rollup_abs_diff_eur"] < 1e-9
        # The per-day objectives sum to the internal-V-based cost, not the reported cost.
        assert tele["internal_daily_objective_sum_eur"] == pytest.approx(
            sum(d["objective_eur"] for d in tele["per_day"] if d["objective_eur"] is not None),
            abs=1e-9,
        )

    def test_day_counts_are_recorded_per_day(self) -> None:
        s = spec()
        case = day_ahead_case()
        _sched, tele = policy_day_ahead(
            s,
            case["horizon"],
            case["days"],
            terminal_value_eur_per_kwh=case["terminal_value"],
            time_limit_s=5.0,
            mip_rel_gap=1e-6,
            mip_abs_gap=1e-9,
        )
        assert tele["day_count"] == len(case["days"]) == 2
        assert tele["days_solved"] + tele["days_skipped_no_usable_intervals"] == tele["day_count"]
        assert {d["local_day"] for d in tele["per_day"]} == {"D3", "D4"}
        assert all(d["solver_status"] == "optimal" for d in tele["per_day"])
        assert all(d["forecast"]["intervals"] == case["per_day"] for d in tele["per_day"])
        assert tele["wall_clock_note"].startswith("per-day solver wall-clock time is")

    def test_soc_is_rolled_forward_as_the_actual_end_of_day(self) -> None:
        s = spec()
        case = day_ahead_case()
        sched, tele = policy_day_ahead(
            s,
            case["horizon"],
            case["days"],
            terminal_value_eur_per_kwh=case["terminal_value"],
            time_limit_s=5.0,
            mip_rel_gap=1e-6,
            mip_abs_gap=1e-9,
        )
        per_day = case["per_day"]
        for i, record in enumerate(tele["per_day"]):
            assert record["soc_start_kwh"] == pytest.approx(
                sched.soc_kwh[i * per_day - 1] if i else s.soc_bound_kwh("init"), abs=1e-6
            )
            assert record["soc_end_kwh"] == pytest.approx(
                sched.soc_kwh[(i + 1) * per_day - 1], abs=1e-6
            )

    def test_myopia_terminal_value_is_the_only_cross_day_coupling(self) -> None:
        """Zero terminal value empties the battery by midnight; a positive one does not.

        This is the ADR-0001 artefact made visible: the daily solve cannot see tomorrow,
        so what it does with end-of-day SOC is entirely a function of how we value it.
        """
        s = spec()
        case = day_ahead_case()
        kwargs = {
            "days": case["days"],
            "time_limit_s": 5.0,
            "mip_rel_gap": 1e-6,
            "mip_abs_gap": 1e-9,
        }
        empty, _ = policy_day_ahead(s, case["horizon"], terminal_value_eur_per_kwh=0.0, **kwargs)
        hoarded, _ = policy_day_ahead(s, case["horizon"], terminal_value_eur_per_kwh=0.5, **kwargs)
        assert empty.terminal_soc_kwh <= hoarded.terminal_soc_kwh
        assert hoarded.terminal_soc_kwh > s.soc_bound_kwh("min") + 1e-9

    def test_causality_escalation_later_prices_leave_earlier_days_untouched(self) -> None:
        """Day D's plan cannot depend on a price published for a day after D."""
        s = spec()
        case = day_ahead_case()
        sched, _ = policy_day_ahead(
            s,
            case["horizon"],
            case["days"],
            terminal_value_eur_per_kwh=case["terminal_value"],
            time_limit_s=5.0,
            mip_rel_gap=1e-6,
            mip_abs_gap=1e-9,
        )
        per_day = case["per_day"]
        changed_prices = case["prices"].copy()
        changed_prices[4 * per_day :] = 0.99  # tomorrow is expensive: not today's business
        horizon2 = Horizon(
            QUARTER,
            changed_prices[case["start"] :],
            case["load"][case["start"] :],
            case["usable"][case["start"] :],
        )
        sched2, _ = policy_day_ahead(
            s,
            horizon2,
            case["days"],
            terminal_value_eur_per_kwh=case["terminal_value"],
            time_limit_s=5.0,
            mip_rel_gap=1e-6,
            mip_abs_gap=1e-9,
        )
        assert np.array_equal(sched.charge_kwh[:per_day], sched2.charge_kwh[:per_day])
        assert np.array_equal(sched.soc_kwh[:per_day], sched2.soc_kwh[:per_day])
        # …and the same day's plan DOES respond to that day's own curve: the dependency
        # is real, not a no-op policy that would make the equality above vacuous.
        same_day_change = case["prices"].copy()
        same_day_change[3 * per_day : 4 * per_day] = 0.99
        horizon3 = Horizon(
            QUARTER,
            same_day_change[case["start"] :],
            case["load"][case["start"] :],
            case["usable"][case["start"] :],
        )
        sched3, _ = policy_day_ahead(
            s,
            horizon3,
            case["days"],
            terminal_value_eur_per_kwh=0.0,
            time_limit_s=5.0,
            mip_rel_gap=1e-6,
            mip_abs_gap=1e-9,
        )
        assert np.any(sched3.charge_kwh[:per_day] != sched.charge_kwh[:per_day]) or np.any(
            sched3.discharge_kwh[:per_day] != sched.discharge_kwh[:per_day]
        )

    def test_load_after_day_d_leaves_day_d_plan_bit_identical(self) -> None:
        """Perturb measured load from day D+1 onwards: day D's plan must not move a bit.

        The requested leakage test, at plan level rather than forecast level.  Day D's
        decision reads day D's prices, day D's forecast, and its own state; the measured
        load *after* it enters neither the forecast nor day D's own clip pass (which is
        bounded by day D's intervals), so the arrays for day D must compare exactly —
        not approximately, and not just in cost.
        """
        s = spec()
        case = day_ahead_case()
        per_day = case["per_day"]
        baseline, _ = policy_day_ahead(
            s,
            case["horizon"],
            case["days"],
            terminal_value_eur_per_kwh=case["terminal_value"],
            time_limit_s=5.0,
            mip_rel_gap=1e-6,
            mip_abs_gap=1e-9,
        )
        perturbed_load = case["load"].copy()
        # Later days collapse to near-zero draw.  Downward is the direction that has to
        # bite: a plan sized by the forecast gets clipped harder against a load that
        # disappeared, whereas a bigger load is simply more headroom the plan never asked
        # for and would leave both windows identical for the wrong reason.
        perturbed_load[4 * per_day :] = 0.01
        horizon_2 = Horizon(
            QUARTER,
            case["prices"][case["start"] :],
            perturbed_load[case["start"] :],
            case["usable"][case["start"] :],
        )
        # …and the forecast the policy is handed is rebuilt from the perturbed archive,
        # so even a history that now contains a huge later day must not reach back and
        # alter the earlier plan.
        labels = day_labels(5, per_day)  # same fixture shape: 5 days, eval from day 3
        runs = contiguous_day_bounds(labels)
        forecast_2, diag_2 = forecast_load_trailing_slot_mean(
            perturbed_load, runs, window_days=3, usable=case["usable"]
        )
        days_2 = [
            DayAheadDay(
                label=d.label,
                start=d.start,
                stop=d.stop,
                forecast_load=forecast_2[case["start"] + d.start : case["start"] + d.stop],
                forecast=diag_2[3 + i],
            )
            for i, d in enumerate(case["days"])
        ]
        after, _ = policy_day_ahead(
            s,
            horizon_2,
            days_2,
            terminal_value_eur_per_kwh=case["terminal_value"],
            time_limit_s=5.0,
            mip_rel_gap=1e-6,
            mip_abs_gap=1e-9,
        )
        first = slice(0, per_day)  # day D of the evaluation window
        assert np.array_equal(baseline.charge_kwh[first], after.charge_kwh[first])
        assert np.array_equal(baseline.discharge_kwh[first], after.discharge_kwh[first])
        assert np.array_equal(baseline.soc_kwh[first], after.soc_kwh[first])
        # The perturbed day is where the difference lands, and it is the *delivered*
        # energy that changes, not the requested plan: day 4's forecast is untouched (it
        # is built from days 1..3, none of them perturbed) and so is its MILP plan, but
        # the measured-load clip that converts plan into delivery now has almost nothing
        # to spend into.  So the equality above is causality, not an inert policy.
        assert not np.array_equal(baseline.discharge_kwh[per_day:], after.discharge_kwh[per_day:])
        assert after.discharge_kwh[per_day:].sum() < baseline.discharge_kwh[per_day:].sum()

    def test_forecast_input_is_not_inert_and_leaking_would_not_help(self) -> None:
        """Negative control: day D's forecast genuinely drives day D's plan.

        Without this, the leakage assertion above would be satisfied by a policy that
        ignored its forecast entirely.  It also records the less obvious finding: a
        *leaked* (too large) forecast does not buy the policy extra discharge — it plans
        energy into load that does not exist, and the measured-load clip throws the
        difference away.  Honesty costs the policy nothing here, and the artifact shows
        the clipped energy either way.
        """
        s = spec()
        case = day_ahead_case()
        honest, _tele_honest = policy_day_ahead(
            s,
            case["horizon"],
            case["days"],
            terminal_value_eur_per_kwh=case["terminal_value"],
            time_limit_s=5.0,
            mip_rel_gap=1e-6,
            mip_abs_gap=1e-9,
        )
        leaked_days = [
            dataclasses.replace(
                d,
                forecast_load=np.where(i == 0, np.full(case["per_day"], 4.0), d.forecast_load),
            )
            for i, d in enumerate(case["days"])
        ]
        leaked, _tele_leaked = policy_day_ahead(
            s,
            case["horizon"],
            leaked_days,
            terminal_value_eur_per_kwh=case["terminal_value"],
            time_limit_s=5.0,
            mip_rel_gap=1e-6,
            mip_abs_gap=1e-9,
        )
        per_day = case["per_day"]
        assert not np.array_equal(honest.discharge_kwh[:per_day], leaked.discharge_kwh[:per_day])
        # The leaked plan wants much more discharge than the measured load can absorb…
        assert (
            _tele_leaked["per_day"][0]["discharge_kwh_planned"]
            > _tele_honest["per_day"][0]["discharge_kwh_planned"]
        )
        # …and the difference is clipped and reported, not exported.
        assert (
            _tele_leaked["per_day"][0]["discharge_clipped_kwh"]
            > _tele_honest["per_day"][0]["discharge_clipped_kwh"]
        )
        assert np.all(leaked.grid_kwh >= -1e-9)

    def test_forecast_underestimate_is_clipped_not_exported(self) -> None:
        """A plan built on a too-small load forecast is repaired, never allowed to export."""
        s = spec()
        case = day_ahead_case()
        starved = [
            dataclasses.replace(d, forecast_load=np.zeros(case["per_day"])) for d in case["days"]
        ]
        sched, tele = policy_day_ahead(
            s,
            case["horizon"],
            starved,
            terminal_value_eur_per_kwh=case["terminal_value"],
            time_limit_s=5.0,
            mip_rel_gap=1e-6,
            mip_abs_gap=1e-9,
        )
        assert np.all(sched.grid_kwh >= -1e-9)
        assert (
            check_feasible(
                s,
                case["horizon"],
                sched.charge_kwh,
                sched.discharge_kwh,
                enforce_terminal_soc=False,
            )
            == []
        )
        assert sched.discharge_kwh.sum() == pytest.approx(0.0)
        assert tele["days_infeasible"] == 0  # a wrong forecast is not an infeasibility

    def test_ceiling_headroom_is_the_measured_load_not_the_forecast(self) -> None:
        """Charging is bounded by the ceiling on the MEASURED load, and breach is an observation."""
        s = spec()
        case = day_ahead_case(import_limit_kw=1.0)
        # A forecast that claims zero load would leave the whole ceiling free…
        greedy = [
            dataclasses.replace(d, forecast_load=np.zeros(case["per_day"])) for d in case["days"]
        ]
        sched, tele = policy_day_ahead(
            s,
            case["horizon"],
            greedy,
            terminal_value_eur_per_kwh=case["terminal_value"],
            time_limit_s=5.0,
            mip_rel_gap=1e-6,
            mip_abs_gap=1e-9,
        )
        headroom = case["horizon"].charge_headroom_kwh
        assert headroom is not None
        assert np.all(sched.charge_kwh <= headroom + 1e-6)
        assert tele["charge_clipped_kwh"] > 0.0
        assert tele["days_with_clipping"] > 0
        # Net import above the ceiling stays an observation the run reports, not a failure.
        assert sched.ceiling_exceeding_intervals == len(
            ceiling_exceedance_indices(case["horizon"], sched.grid_kwh)
        )

    def test_deterministic(self) -> None:
        s = spec()
        case = day_ahead_case()
        kwargs = {
            "days": case["days"],
            "terminal_value_eur_per_kwh": case["terminal_value"],
            "time_limit_s": 5.0,
            "mip_rel_gap": 1e-6,
            "mip_abs_gap": 1e-9,
        }
        a, ta = policy_day_ahead(s, case["horizon"], **kwargs)
        b, tb = policy_day_ahead(s, case["horizon"], **kwargs)
        assert a.cost_eur == b.cost_eur
        assert np.array_equal(a.charge_kwh, b.charge_kwh)
        assert np.array_equal(a.soc_kwh, b.soc_kwh)
        assert ta["per_day"] == tb["per_day"]

    def test_ragged_dst_days_are_one_solve_each(self) -> None:
        """A 92-slot day and a 100-slot day are each partitioned and solved whole."""
        s = spec()
        sizes = [8, 8, 7, 10]  # a short "spring-forward" day and a long "fall-back" day
        labels: list[int] = []
        for i, size in enumerate(sizes):
            labels += [i] * size
        n = len(labels)
        rng = np.random.default_rng(9)
        prices = rng.uniform(0.05, 0.3, n)
        load = rng.uniform(0.2, 1.2, n)
        runs = contiguous_day_bounds(labels)
        forecast, diag = forecast_load_trailing_slot_mean(load, runs, window_days=2)
        horizon = Horizon(
            QUARTER, prices[runs[2][1] :], load[runs[2][1] :], np.ones(n - runs[2][1], dtype=bool)
        )
        days = [
            DayAheadDay(
                label=f"D{i}",
                start=lo - runs[2][1],
                stop=hi - runs[2][1],
                forecast_load=forecast[lo:hi],
                forecast=diag[i],
            )
            for i, (label, lo, hi) in enumerate(runs)
            if i >= 2
        ]
        sched, tele = policy_day_ahead(
            s,
            horizon,
            days,
            terminal_value_eur_per_kwh=0.15,
            time_limit_s=5.0,
            mip_rel_gap=1e-6,
            mip_abs_gap=1e-9,
        )
        assert [d["intervals"] for d in tele["per_day"]] == [7, 10]
        assert (
            check_feasible(
                s, horizon, sched.charge_kwh, sched.discharge_kwh, enforce_terminal_soc=False
            )
            == []
        )


class TestDayAheadDegradation:
    def test_infeasible_day_holds_soc_and_the_run_continues(self, monkeypatch) -> None:
        """One bad day must not abort the horizon and must not borrow the oracle's answer."""
        import energy_baseline.policies as policies_mod
        from energy_baseline.oracle import OracleInfeasibleError

        s = spec()
        case = day_ahead_case()
        real_solve = policies_mod.solve_oracle
        calls = {"n": 0}

        def refuse(spec_arg, horizon_arg, **kwargs):
            calls["n"] += 1
            if calls["n"] == 2:
                raise OracleInfeasibleError("day is infeasible: unit test")
            return real_solve(spec_arg, horizon_arg, **kwargs)

        monkeypatch.setattr(policies_mod, "solve_oracle", refuse)
        sched, tele = policy_day_ahead(
            s,
            case["horizon"],
            case["days"],
            terminal_value_eur_per_kwh=case["terminal_value"],
            time_limit_s=5.0,
            mip_rel_gap=1e-6,
            mip_abs_gap=1e-9,
        )
        per_day = case["per_day"]
        assert tele["days_infeasible"] == 1
        assert tele["days_degraded_to_hold"] == 1
        assert tele["per_day"][1]["degradation"] == "infeasible_hold_soc_no_charging"
        assert np.allclose(sched.charge_kwh[per_day : 2 * per_day], 0.0)
        assert np.allclose(sched.discharge_kwh[per_day : 2 * per_day], 0.0)
        # SOC held: the degraded day neither gained nor lost stored energy…
        assert sched.soc_kwh[2 * per_day - 1] == pytest.approx(sched.soc_kwh[per_day - 1], abs=1e-9)
        # …and the days around it were still optimised normally.
        assert tele["days_solved"] == 1
        assert sched.charge_kwh.sum() + sched.discharge_kwh.sum() > 0.0

    def test_no_incumbent_day_is_counted_separately(self, monkeypatch) -> None:
        import energy_baseline.policies as policies_mod
        from energy_baseline.oracle import OracleNoIncumbentError

        s = spec()
        case = day_ahead_case()

        def refuse(*args, **kwargs):
            raise OracleNoIncumbentError("no feasible schedule found (time limit): unit test")

        monkeypatch.setattr(policies_mod, "solve_oracle", refuse)
        sched, tele = policy_day_ahead(
            s,
            case["horizon"],
            case["days"],
            terminal_value_eur_per_kwh=case["terminal_value"],
            time_limit_s=5.0,
            mip_rel_gap=1e-6,
            mip_abs_gap=1e-9,
        )
        assert tele["days_no_incumbent"] == tele["day_count"]
        assert tele["days_infeasible"] == 0
        assert tele["days_degraded_to_hold"] == tele["day_count"]
        assert np.all(sched.charge_kwh == 0.0)
        assert np.all(sched.discharge_kwh == 0.0)
        # Held-SOC days still cost the meter exactly what the baseline paid.
        assert sched.cost_eur == pytest.approx(policy_no_battery(s, case["horizon"]).cost_eur)

    def test_all_gap_day_is_skipped_not_solved(self) -> None:
        """A day whose intervals are all unusable has no cost basis: hold and disclose."""
        s = spec()
        case = day_ahead_case()
        usable = case["usable"].copy()
        per_day = case["per_day"]
        usable[4 * per_day : 5 * per_day] = False
        rebuilt = Horizon(
            QUARTER,
            case["prices"][case["start"] :],
            case["load"][case["start"] :],
            usable[case["start"] :],
        )
        days = [
            DayAheadDay(
                label=d.label,
                start=d.start,
                stop=d.stop,
                forecast_load=np.zeros(d.stop - d.start),
            )
            for d in case["days"]
        ]
        sched, tele = policy_day_ahead(
            s,
            rebuilt,
            days,
            terminal_value_eur_per_kwh=case["terminal_value"],
            time_limit_s=5.0,
            mip_rel_gap=1e-6,
            mip_abs_gap=1e-9,
        )
        assert tele["days_skipped_no_usable_intervals"] == 1
        assert tele["per_day"][1]["degradation"] == "skipped_no_usable_intervals"
        assert np.all(sched.charge_kwh[per_day : 2 * per_day] == 0.0)
        assert sched.soc_kwh[2 * per_day - 1] == pytest.approx(sched.soc_kwh[per_day - 1], abs=1e-9)


class TestDayAheadInputValidation:
    def _kwargs(self, case):
        return {
            "terminal_value_eur_per_kwh": 0.15,
            "time_limit_s": 5.0,
            "mip_rel_gap": 1e-6,
            "mip_abs_gap": 1e-9,
        }

    def test_partition_must_cover_the_horizon(self) -> None:
        s = spec()
        case = day_ahead_case()
        with pytest.raises(ValueError, match="partition must"):
            policy_day_ahead(s, case["horizon"], case["days"][:-1], **self._kwargs(case))

    def test_days_must_be_contiguous(self) -> None:
        s = spec()
        case = day_ahead_case()
        shifted = [case["days"][0], dataclasses.replace(case["days"][1], start=case["per_day"] + 1)]
        with pytest.raises(ValueError, match="previous day ended"):
            policy_day_ahead(s, case["horizon"], shifted, **self._kwargs(case))

    def test_forecast_length_must_match_the_day(self) -> None:
        s = spec()
        case = day_ahead_case()
        bad = [dataclasses.replace(case["days"][0], forecast_load=np.zeros(3))] + case["days"][1:]
        with pytest.raises(ValueError, match="forecast carries"):
            policy_day_ahead(s, case["horizon"], bad, **self._kwargs(case))

    def test_negative_forecast_is_refused(self) -> None:
        """A negative energy forecast would make the no-export row unsatisfiable."""
        s = spec()
        case = day_ahead_case()
        bad = [
            dataclasses.replace(case["days"][0], forecast_load=np.full(case["per_day"], -1.0))
        ] + case["days"][1:]
        with pytest.raises(ValueError, match="negative load"):
            policy_day_ahead(s, case["horizon"], bad, **self._kwargs(case))

    def test_terminal_value_must_be_finite(self) -> None:
        s = spec()
        case = day_ahead_case()
        kwargs = self._kwargs(case)
        kwargs["terminal_value_eur_per_kwh"] = math.nan
        with pytest.raises(ValueError, match="terminal_value_eur_per_kwh"):
            policy_day_ahead(s, case["horizon"], case["days"], **kwargs)

    def test_solver_knobs_must_be_positive(self) -> None:
        s = spec()
        case = day_ahead_case()
        for knob, bad in (("time_limit_s", 0.0), ("mip_rel_gap", 0.0), ("mip_abs_gap", -1.0)):
            kwargs = self._kwargs(case)
            kwargs[knob] = bad
            with pytest.raises(ValueError):
                policy_day_ahead(s, case["horizon"], case["days"], **kwargs)


class TestDayAheadPurity:
    def test_inputs_not_mutated(self) -> None:
        s = spec()
        case = day_ahead_case()
        prices = case["horizon"].prices.copy()
        load = case["horizon"].load_kwh.copy()
        forecasts = [d.forecast_load.copy() for d in case["days"]]
        policy_day_ahead(
            s,
            case["horizon"],
            case["days"],
            terminal_value_eur_per_kwh=case["terminal_value"],
            time_limit_s=5.0,
            mip_rel_gap=1e-6,
            mip_abs_gap=1e-9,
        )
        assert np.array_equal(case["horizon"].prices, prices)
        assert np.array_equal(case["horizon"].load_kwh, load)
        for day, before in zip(case["days"], forecasts, strict=True):
            assert np.array_equal(day.forecast_load, before)

    def test_day_ahead_sits_between_causal_and_oracle_on_a_clean_fixture(self) -> None:
        """Ordering sanity for the fixture, NOT a guaranteed relation between policies.

        The benchmark deliberately asserts no day-ahead-vs-causal ordering, because none
        is guaranteed.  On this fixture (good forecast, honest terminal value) the
        expected result does hold, and pinning it here keeps the ladder from silently
        collapsing into one flat line — while saying out loud that it is a fixture
        property, not a theorem.
        """
        s = spec()
        case = day_ahead_case()
        terminal = case["terminal_value"]
        sched, _tele = policy_day_ahead(
            s,
            case["horizon"],
            case["days"],
            terminal_value_eur_per_kwh=terminal,
            time_limit_s=10.0,
            mip_rel_gap=1e-6,
            mip_abs_gap=1e-9,
        )
        history = Horizon(
            QUARTER,
            case["prices"][: case["start"]],
            case["load"][: case["start"]],
            case["usable"][: case["start"]],
        )
        ct, dt = fit_thresholds(history.prices, charge_pct=25.0, discharge_pct=75.0)
        causal = policy_causal_price(
            s, case["horizon"], charge_threshold=ct, discharge_threshold=dt
        )
        oracle = solve_oracle(
            s,
            case["horizon"],
            time_limit_s=30.0,
            mip_rel_gap=1e-6,
            mip_abs_gap=1e-9,
            terminal_mode="free",
            terminal_value_eur_per_kwh=terminal,
        )
        assert oracle.cost_eur <= sched.cost_eur + 1e-6
        assert sched.cost_eur <= causal.cost_eur + 1e-6


class TestSeedProtectionIsHorizonLevel:
    """Energy bought on day D must still be spendable on day D+1.

    These pin a withdrawn claim.  The first version of this policy floored each daily
    solve at *that day's* opening SOC, calling it seed protection.  It is not: it turns
    every earlier day's purchased charge into unspendable seed, so a battery that filled
    froze at the ceiling, did nothing for the rest of the window, and lost to the causal
    rule while hoarding energy it was forbidden to use.  Seed protection means the single
    opening charge of the evaluation window is not the policy's to spend — full stop.
    """

    def test_charge_bought_on_a_cheap_day_is_spent_on_an_expensive_one(self) -> None:
        s = spec(
            capacity_kwh=5.12,
            power_kw=2.5,
            charge_efficiency=0.95,
            discharge_efficiency=0.95,
            soc_min_frac=0.0,
            soc_max_frac=1.0,
            initial_soc_frac=0.05,
        )
        per_day = 24
        prices = np.array([0.05] * per_day + [0.35] * per_day)
        load = np.full(2 * per_day, 0.3)
        h = horizon(prices, load, interval_seconds=HOUR)
        days = [
            DayAheadDay(label="cheap", start=0, stop=per_day, forecast_load=load[:per_day]),
            DayAheadDay(
                label="expensive", start=per_day, stop=2 * per_day, forecast_load=load[per_day:]
            ),
        ]
        sched, tele = policy_day_ahead(
            s,
            h,
            days,
            # Valued above the cheap price and below the expensive one: that is the whole
            # mechanism.  Below 0.05/eta_c = 0.0526 the daily solve would rationally refuse
            # to store at all (the credit would not cover the charge), and above 0.35 it
            # would refuse to spend.  A terminal value that never lets the battery fill is
            # not a conservative choice, it is a disabled battery — which is why the
            # default is the training-window mean price rather than zero.
            terminal_value_eur_per_kwh=0.20,
            time_limit_s=30.0,
            mip_rel_gap=1e-9,
            mip_abs_gap=1e-12,
        )
        # Day 1 charged, day 2 discharged what day 1 bought.
        assert sched.charge_kwh[:per_day].sum() > 1.0
        assert tele["per_day"][1]["discharge_kwh"] > 1.0
        assert sched.discharge_kwh[:per_day].sum() == pytest.approx(0.0)
        # …and the SOC really fell on day 2 instead of being pinned at its opening value.
        assert sched.soc_kwh[per_day - 1] > 4.0
        assert sched.soc_kwh[-1] < sched.soc_kwh[per_day - 1]
        # Seed protection still holds, at the horizon's floor and nowhere higher.
        floor_kwh = s.causal_floor_kwh()
        assert float(sched.soc_kwh.min()) >= floor_kwh - 1e-6
        assert sched.cost_eur < policy_no_battery(s, h).cost_eur

    def test_a_seed_that_already_fills_the_window_is_reported_not_crashed(self) -> None:
        """Opening at the ceiling leaves no headroom either way: zero flows, and said so."""
        s = spec(
            capacity_kwh=5.12,
            power_kw=2.5,
            charge_efficiency=0.95,
            discharge_efficiency=0.95,
            soc_min_frac=0.05,
            soc_max_frac=0.95,
            initial_soc_frac=0.95,
        )
        per_day = 12
        prices = np.array([0.05] * per_day + [0.4] * per_day)
        load = np.full(2 * per_day, 0.3)
        h = horizon(prices, load)
        days = [
            DayAheadDay(label="a", start=0, stop=per_day, forecast_load=load[:per_day]),
            DayAheadDay(label="b", start=per_day, stop=2 * per_day, forecast_load=load[per_day:]),
        ]
        sched, tele = policy_day_ahead(
            s,
            h,
            days,
            terminal_value_eur_per_kwh=0.1,
            time_limit_s=10.0,
            mip_rel_gap=1e-6,
            mip_abs_gap=1e-9,
        )
        assert tele["days_without_headroom"] == 2
        assert tele["days_solved"] == 0
        assert tele["days_degraded_to_hold"] == 0
        assert all(d["degradation"] == "frozen_by_seed_at_ceiling" for d in tele["per_day"])
        assert np.all(sched.charge_kwh == 0.0)
        assert np.all(sched.discharge_kwh == 0.0)
        assert sched.cost_eur == pytest.approx(policy_no_battery(s, h).cost_eur)
        assert "no headroom" in tele["degradation_note"]
