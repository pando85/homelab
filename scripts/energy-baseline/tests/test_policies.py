"""Tests for the benchmark policies — baselines, causality, and honest losses.

The causal policy must be *strictly* causal (current price and past state only),
must never break a physics invariant, must clamp its **discharge** rather than
export (charging is *not* clamped to the load — charging raises import, so the
no-export rule puts no bound on it), must limit charging to the headroom a
configured site import ceiling leaves (and charge nothing where the measured
load already used it all — without ever failing on such history), and may cost
more than the no-battery baseline.  Gap exclusion must be identical for every
policy.
"""

from __future__ import annotations

import dataclasses
import math

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
from energy_baseline.policies import (
    fit_thresholds,
    policy_causal_price,
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
