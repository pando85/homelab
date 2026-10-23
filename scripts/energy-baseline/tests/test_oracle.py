"""Tests for the perfect-information oracle — MILP invariants and bound honesty.

The oracle is the benchmark ceiling: a chronologically feasible optimum with full
price knowledge.  These tests pin the analytic optima of small hand-computable
horizons, the mutual-exclusion binary (and the negative-price pathology it
prevents), the bound/certification semantics, the free-terminal-mode accounting
(shared causal floor, explicit inventory valuation, dominance vs the causal
policy), the site import ceiling as a *charging-headroom* constraint (measured
load above it is an observation, never an infeasibility), and the failure modes —
infeasible specs raise instead of returning garbage, and a tiny time limit returns
a non-certified incumbent or raises, but never hangs.
"""

from __future__ import annotations

import time

import numpy as np
import pytest
from scipy.optimize import milp

from energy_baseline.battery import (
    BatteryPhysicsError,
    BatterySpec,
    Horizon,
    ceiling_exceedance_indices,
    check_feasible,
    simulate,
)
from energy_baseline.oracle import (
    OracleError,
    OracleInfeasibleError,
    OracleNoIncumbentError,
    build_model,
    classify_result,
    solve_oracle,
)
from energy_baseline.policies import fit_thresholds, policy_causal_price, policy_no_battery

HOUR = 3600.0
QUARTER = 900.0


def spec(**overrides) -> BatterySpec:
    base = dict(
        capacity_kwh=10.0,
        power_kw=1.0,
        charge_efficiency=1.0,
        discharge_efficiency=1.0,
        soc_min_frac=0.0,
        soc_max_frac=1.0,
        initial_soc_frac=0.0,
        terminal_soc_frac=None,
    )
    base.update(overrides)
    return BatterySpec(**base)


def horizon(prices, load, usable=None, interval_seconds=HOUR, import_limit_kw=None) -> Horizon:
    n = len(prices)
    return Horizon(
        interval_seconds,
        prices,
        load,
        [True] * n if usable is None else usable,
        import_limit_kw,
    )


def load_term(h: Horizon) -> float:
    """The constant part of the cost the objective deliberately omits."""
    return float(np.dot(h.prices[h.usable], h.load_kwh[h.usable]))


# ---------------------------------------------------------------------------
# 7. Hand-computable optima
# ---------------------------------------------------------------------------


class TestAnalyticOptima:
    def test_two_interval_toy_matches_the_known_optimum(self) -> None:
        """Charge at the cheap interval, discharge at the expensive one.

        10 kWh battery, 1 kW, 1 h intervals, lossless, SOC 0 -> 1 -> 0.
        t0 grid = 1 + 1 = 2 kWh at 1.00 -> 2.00 EUR
        t1 grid = 1 - 1 = 0 kWh at 3.00 -> 0.00 EUR
        optimum = 2.00 (baseline 4.00, savings 2.00).
        """
        s = spec()
        h = horizon([1.0, 3.0], [1.0, 1.0])
        sched = solve_oracle(s, h)
        assert sched.charge_kwh == pytest.approx([1.0, 0.0])
        assert sched.discharge_kwh == pytest.approx([0.0, 1.0])
        assert sched.soc_kwh == pytest.approx([1.0, 0.0])
        assert sched.grid_kwh == pytest.approx([2.0, 0.0])
        assert sched.cost_eur == pytest.approx(2.0)
        assert sched.throughput_kwh == pytest.approx(1.0)
        assert sched.efc == pytest.approx(0.1)
        assert sched.solver_status == "optimal"
        assert sched.is_certified is True
        assert sched.bound_kind == "certified"
        assert sched.solver_gap == pytest.approx(0.0, abs=1e-12)
        # the plan the solver claims is the plan the simulator validates
        assert check_feasible(s, h, sched.charge_kwh, sched.discharge_kwh) == []

    def test_unprofitable_cycle_is_not_taken(self) -> None:
        """0.5 one-way efficiency: 1 kWh in at 1.00 EUR only returns 0.25 kWh.

        Avoided cost 0.25 x 3.00 = 0.75 < 1.00 spent, so the optimum is to idle
        and pay the baseline 4.00 EUR.
        """
        s = spec(charge_efficiency=0.5, discharge_efficiency=0.5)
        h = horizon([1.0, 3.0], [1.0, 1.0])
        sched = solve_oracle(s, h)
        assert np.all(sched.charge_kwh == 0.0)
        assert np.all(sched.discharge_kwh == 0.0)
        assert sched.cost_eur == pytest.approx(4.0)
        assert sched.cost_eur == pytest.approx(policy_no_battery(s, h).cost_eur)

    def test_lossy_cycle_matches_the_hand_solution(self) -> None:
        """eta 0.9, prices 0.10/0.40: charge 1.0 kWh, deliver 0.81 kWh back.

        t0 grid = 2.0 at 0.10 -> 0.20 ; t1 grid = 0.19 at 0.40 -> 0.076.
        """
        s = spec(charge_efficiency=0.9, discharge_efficiency=0.9)
        h = horizon([0.1, 0.4], [1.0, 1.0])
        sched = solve_oracle(s, h)
        assert sched.charge_kwh == pytest.approx([1.0, 0.0])
        assert sched.discharge_kwh == pytest.approx([0.0, 0.81])
        assert sched.soc_kwh == pytest.approx([0.9, 0.0], abs=1e-9)
        assert sched.cost_eur == pytest.approx(0.276, abs=1e-9)
        # round-trip loss on BOTH legs: delivered = stored * eta_c * eta_d
        assert sched.throughput_kwh == pytest.approx(1.0 * 0.9 * 0.9)

    def test_expensive_first_interval_means_no_dispatch_at_all(self) -> None:
        s = spec(charge_efficiency=0.9, discharge_efficiency=0.9)
        h = horizon([0.4, 0.1], [1.0, 1.0])  # cheap interval last, nothing to shift into
        sched = solve_oracle(s, h)
        assert sched.cost_eur == pytest.approx(policy_no_battery(s, h).cost_eur)
        assert sched.throughput_kwh == pytest.approx(0.0)

    def test_three_interval_arbitrage_matches_hand_solution(self) -> None:
        """0.10 / 0.50 / 0.30: buy 1 kWh cheap, sell into the peak, pay for 1 kWh."""
        s = spec(charge_efficiency=1.0, discharge_efficiency=1.0, initial_soc_frac=0.0)
        h = horizon([0.1, 0.5, 0.3], [1.0, 1.0, 1.0])
        sched = solve_oracle(s, h)
        # charge t0 (+1), discharge t1 (-1), idle t2 -> grid [2, 0, 1]
        assert sched.grid_kwh == pytest.approx([2.0, 0.0, 1.0])
        assert sched.cost_eur == pytest.approx(0.2 + 0.0 + 0.3)

    def test_power_limit_binds_over_several_intervals(self) -> None:
        """A 1 kW battery cannot move more than 1 kWh per hour-long interval."""
        s = spec(capacity_kwh=10.0, power_kw=1.0, initial_soc_frac=0.0, terminal_soc_frac=0.5)
        h = horizon([0.01] * 5, [5.0] * 5)
        sched = solve_oracle(s, h)
        limit = s.interval_energy_limit_kwh(HOUR)
        assert np.all(sched.charge_kwh <= limit + 1e-9)
        assert sched.soc_kwh[-1] == pytest.approx(5.0)  # exactly the terminal target
        assert sched.charge_kwh.sum() == pytest.approx(5.0 * limit)


# ---------------------------------------------------------------------------
# 3/10. Negative prices and the mutual-exclusion binary
# ---------------------------------------------------------------------------


class TestNegativePrices:
    def test_oracle_charges_during_negative_prices(self) -> None:
        """Importing at a negative price pays money, so the optimum charges there.

        eta 0.9, prices -1.00 / +0.50, SOC 5 kWh seed, 1 h intervals:
        charge 1 kWh at t0 (grid 2.0 -> -2.00), give back 0.81 kWh at t1
        (grid 0.19 -> +0.095) => -1.905, better than the -0.50 baseline.
        """
        s = spec(charge_efficiency=0.9, discharge_efficiency=0.9, initial_soc_frac=0.5)
        h = horizon([-1.0, 0.5], [1.0, 1.0])
        sched = solve_oracle(s, h)
        assert sched.charge_kwh[0] == pytest.approx(1.0)
        assert sched.discharge_kwh[1] == pytest.approx(0.81)
        assert sched.grid_kwh[0] == pytest.approx(2.0)  # import grows at the negative price
        assert sched.cost_eur == pytest.approx(-1.905, abs=1e-9)
        assert sched.cost_eur < policy_no_battery(s, h).cost_eur
        # physically valid, and never both flows at once
        assert check_feasible(s, h, sched.charge_kwh, sched.discharge_kwh) == []
        assert not np.any((sched.charge_kwh > 1e-9) & (sched.discharge_kwh > 1e-9))

    def test_strongly_negative_horizon_is_physically_valid(self) -> None:
        rng = np.random.default_rng(4)
        n = 96
        prices = -rng.uniform(0.05, 1.2, n)
        load = rng.uniform(0.4, 1.8, n)
        s = spec(
            capacity_kwh=5.12,
            power_kw=2.5,
            charge_efficiency=0.95,
            discharge_efficiency=0.95,
            initial_soc_frac=0.2,
        )
        h = horizon(prices, load, interval_seconds=QUARTER)
        sched = solve_oracle(s, h)
        assert check_feasible(s, h, sched.charge_kwh, sched.discharge_kwh) == []
        assert not np.any((sched.charge_kwh > 1e-9) & (sched.discharge_kwh > 1e-9))
        assert np.all(sched.grid_kwh >= -1e-9)
        assert sched.cost_eur < policy_no_battery(s, h).cost_eur
        assert np.any(sched.charge_kwh > 0.0)

    def test_binary_mode_blocks_the_harvesting_loop(self) -> None:
        """Without the binary, the LP farms a negative price at the SOC ceiling.

        Starting full with every interval at -1.00 EUR/kWh, the MILP must not
        discharge into a negative price nor charge at the ceiling: the optimum is
        the honest arbitrage below.  Dropping integrality lets the model charge
        and discharge *simultaneously*, staying SOC-neutral while inflating
        import to collect the negative price — pure modelling artifact.
        """
        s = spec(
            capacity_kwh=10.0,
            power_kw=1.0,
            charge_efficiency=0.9,
            discharge_efficiency=0.9,
            initial_soc_frac=1.0,  # full: no headroom to charge
        )
        h = horizon([-1.0] * 3, [1.0] * 3)
        sched = solve_oracle(s, h)
        assert not np.any((sched.charge_kwh > 1e-9) & (sched.discharge_kwh > 1e-9))
        assert check_feasible(s, h, sched.charge_kwh, sched.discharge_kwh) == []

        obj, integrality, bounds, constraints = build_model(s, h)
        relaxed = milp(
            c=obj,
            integrality=np.zeros_like(integrality),
            bounds=bounds,
            constraints=constraints,
            options={"disp": False},
        )
        n = h.n
        x = relaxed.x
        charge = x[: 3 * n : 3]
        discharge = x[1 : 3 * n : 3]
        relaxed_cost = load_term(h) + float(relaxed.fun)
        # the relaxation needs simultaneity to beat the honest optimum
        assert np.any((charge > 1e-6) & (discharge > 1e-6))
        assert relaxed_cost < sched.cost_eur - 1e-6
        # ... and the binary keeps the reported plan inside the physics
        with pytest.raises(BatteryPhysicsError, match="simultaneous"):
            simulate(s, h, charge, discharge, enforce_terminal_soc=False)

    def test_negative_prices_do_not_break_terminal_accounting(self) -> None:
        rng = np.random.default_rng(8)
        for trial in range(12):
            n = 4 + trial
            prices = rng.uniform(-0.6, 0.45, n)
            load = rng.uniform(0.5, 2.0, n)
            s = spec(
                capacity_kwh=5.12,
                power_kw=2.5,
                charge_efficiency=0.95,
                discharge_efficiency=0.9,
                soc_min_frac=0.05,
                soc_max_frac=0.95,
                initial_soc_frac=0.5,
            )
            h = horizon(prices, load, interval_seconds=QUARTER)
            sched = solve_oracle(s, h)
            assert sched.soc_kwh[-1] == pytest.approx(5.12 * 0.5, abs=1e-6), trial
            assert check_feasible(s, h, sched.charge_kwh, sched.discharge_kwh) == []


# ---------------------------------------------------------------------------
# 6. The oracle is a ceiling over both other policies
# ---------------------------------------------------------------------------


class TestOracleDominates:
    @pytest.mark.parametrize(
        ("prices", "load", "usable"),
        [
            ([1.0, 3.0], [1.0, 1.0], None),
            ([0.05, 0.05, 0.20, 0.20, 0.50, 0.50], [2.0] * 6, None),
            ([0.30, 0.05, 0.02, 0.28, 0.35, 0.40], [1.0] * 6, None),
            (
                [0.30, 0.05, 0.02, 0.28, 0.35, 0.40],
                [1.0] * 6,
                [True, True, False, True, True, True],
            ),
            ([0.01, 0.40, 0.02, 0.45, 0.03, 0.42], [0.3, 1.5, 0.3, 1.5, 0.3, 1.5], None),
        ],
    )
    def test_ceiling_over_causal_and_baseline(self, prices, load, usable) -> None:
        s = spec(
            capacity_kwh=5.12,
            power_kw=2.5,
            charge_efficiency=0.95,
            discharge_efficiency=0.95,
            initial_soc_frac=0.2,
        )
        h = horizon(prices, load, usable, interval_seconds=QUARTER)
        nb = policy_no_battery(s, h)
        ct, dt = fit_thresholds(np.asarray(prices), charge_pct=25.0, discharge_pct=75.0)
        ca = policy_causal_price(s, h, charge_threshold=ct, discharge_threshold=dt)
        orc = solve_oracle(s, h)
        assert orc.cost_eur <= ca.cost_eur + 1e-9
        assert orc.cost_eur <= nb.cost_eur + 1e-9
        assert orc.savings_vs(nb) >= ca.savings_vs(nb) - 1e-9

    def test_ceiling_holds_on_a_full_day(self) -> None:
        rng = np.random.default_rng(21)
        n = 96
        prices = rng.uniform(0.02, 0.42, n)
        load = rng.uniform(0.15, 1.6, n)
        usable = rng.random(n) > 0.08
        s = spec(
            capacity_kwh=5.12,
            power_kw=2.5,
            charge_efficiency=0.95,
            discharge_efficiency=0.95,
            initial_soc_frac=0.2,
        )
        h = horizon(prices, load, usable, interval_seconds=QUARTER)
        nb = policy_no_battery(s, h)
        ct, dt = fit_thresholds(prices, charge_pct=25.0, discharge_pct=75.0)
        ca = policy_causal_price(s, h, charge_threshold=ct, discharge_threshold=dt)
        orc = solve_oracle(s, h)
        assert orc.cost_eur <= ca.cost_eur + 1e-9
        assert orc.cost_eur <= nb.cost_eur + 1e-9
        assert orc.is_certified is True
        assert orc.solver_status == "optimal"

    def test_causal_losses_are_representable_while_the_ceiling_still_holds(self) -> None:
        """The causal policy may be worse than no battery; the oracle is still best.

        eta 0.5 one-way against a 10% spread: cycling destroys money, so the
        causal policy pays more than the baseline — and the oracle, which simply
        declines to cycle, pays the baseline exactly.
        """
        s = spec(
            capacity_kwh=10.0,
            power_kw=1.0,
            charge_efficiency=0.5,
            discharge_efficiency=0.5,
            initial_soc_frac=0.0,
        )
        h = horizon([0.10, 0.10, 0.11, 0.11], [2.0] * 4)
        nb = policy_no_battery(s, h)
        ca = policy_causal_price(s, h, charge_threshold=0.105, discharge_threshold=0.105)
        orc = solve_oracle(s, h)
        assert ca.cost_eur > nb.cost_eur  # a real loss, not clipped away
        assert ca.savings_vs(nb) < 0.0  # reported as negative savings
        assert orc.cost_eur <= nb.cost_eur + 1e-9
        assert orc.cost_eur <= ca.cost_eur + 1e-9
        assert orc.savings_vs(nb) >= 0.0  # the ceiling never loses money


# ---------------------------------------------------------------------------
# 7b. Site import ceiling in the oracle
# ---------------------------------------------------------------------------


def night_peak_horizon(
    n_cheap: int = 8, n_peak: int = 24, cheap_load: float = 0.05, peak_load: float = 0.4, **kwargs
) -> Horizon:
    """A few cheap, baseload-only night hours, then a long expensive evening.

    Night load is 200 W (0.05 kWh per 15 min); the evening block is where the
    stored energy can actually be used.  A ``peak_load`` above the configured
    ceiling is allowed and means something specific: the load alone breaches it,
    so charging is unavailable there, discharge is not, and the breach is an
    observation on the schedule rather than an infeasibility.
    """
    prices = np.array([0.02] * n_cheap + [0.40] * n_peak)
    load = np.array([cheap_load] * n_cheap + [peak_load] * n_peak)
    return horizon(prices, load, interval_seconds=QUARTER, **kwargs)


def small_battery() -> BatterySpec:
    return spec(
        capacity_kwh=5.12,
        power_kw=2.5,
        charge_efficiency=0.95,
        discharge_efficiency=0.95,
        soc_min_frac=0.05,
        soc_max_frac=0.95,
        initial_soc_frac=0.05,
    )


class TestImportCeiling:
    def test_charge_is_bounded_by_power_and_headroom_not_by_load(self) -> None:
        """The oracle never had a ``charge <= load`` cap, and must not gain one.

        Cheap first interval, expensive second, 5 kW through a 0.3 kWh load: the
        optimum imports 5.3 kWh to charge, which a load cap would have forbidden.
        """
        s = spec(capacity_kwh=10.0, power_kw=5.0, charge_efficiency=1.0, discharge_efficiency=1.0)
        h = horizon([0.01, 0.5], [0.3, 6.0], interval_seconds=HOUR)
        sched = solve_oracle(s, h)
        assert sched.charge_kwh[0] == pytest.approx(5.0)
        assert sched.grid_kwh[0] == pytest.approx(5.3)
        assert sched.discharge_kwh[1] == pytest.approx(5.0)
        assert check_feasible(s, h, sched.charge_kwh, sched.discharge_kwh) == []

    def test_oracle_never_exceeds_the_configured_ceiling(self) -> None:
        """0.3 kW over 15 min = 0.075 kWh supplied, so at most 0.025 kWh of charge."""
        s = small_battery()
        h = night_peak_horizon(peak_load=0.05, import_limit_kw=0.3)
        sched = solve_oracle(s, h)
        ceiling = h.interval_import_limit_kwh
        assert ceiling == pytest.approx(0.075)
        assert np.all(h.load_kwh + sched.charge_kwh <= ceiling + 1e-9)
        assert np.all(sched.charge_kwh <= 0.025 + 1e-9)
        # non-vacuous: it really does charge, and up to the ceiling every night hour
        assert np.any(sched.charge_kwh > 1e-6)
        assert sched.charge_kwh.sum() == pytest.approx(8 * 0.025, abs=1e-6)
        assert check_feasible(s, h, sched.charge_kwh, sched.discharge_kwh) == []
        assert sched.solver_status == "optimal"

    def test_binding_ceiling_raises_the_optimum(self) -> None:
        """A ceiling that throttles charging must cost more than no ceiling at all.

        8 night intervals at the 2.5 kW rating move 4.85 kWh of AC energy; the
        same horizon contracted to 2.5 kW moves 4.60 and to 2.0 kW only 3.60.
        """
        s = small_battery()
        free = solve_oracle(s, night_peak_horizon())
        mid = solve_oracle(s, night_peak_horizon(import_limit_kw=2.5))
        tight = solve_oracle(s, night_peak_horizon(import_limit_kw=2.0))
        assert free.charge_kwh.sum() == pytest.approx(
            s.usable_window_kwh / s.charge_efficiency, abs=1e-6
        )
        assert np.any(free.charge_kwh > night_peak_horizon().load_kwh)  # fills at full power
        assert mid.charge_kwh.sum() == pytest.approx(8 * (2.5 * 0.25 - 0.05), abs=1e-6)
        assert tight.charge_kwh.sum() == pytest.approx(8 * (2.0 * 0.25 - 0.05), abs=1e-6)
        assert free.cost_eur < mid.cost_eur < tight.cost_eur
        # every one of them still beats doing nothing with the battery, and each
        # round trip is SOC-neutral by construction: D = C * eta_c * eta_d
        nb = policy_no_battery(s, night_peak_horizon())
        for sched in (free, mid, tight):
            assert sched.cost_eur < nb.cost_eur
            assert sched.discharge_kwh.sum() == pytest.approx(
                sched.charge_kwh.sum() * 0.95 * 0.95, abs=1e-6
            )

    def test_unknown_ceiling_matches_a_ceiling_that_never_binds(self) -> None:
        """Requirement: ``None`` is unconstrained, and a generous ceiling is the
        same problem — bit-for-bit, on the charge side, the discharge side and the
        cost.  The extra row block must not perturb the solution in any way.
        """
        s = small_battery()
        free = solve_oracle(s, night_peak_horizon())
        generous = solve_oracle(s, night_peak_horizon(import_limit_kw=50.0))
        assert generous.cost_eur == free.cost_eur
        assert np.array_equal(generous.charge_kwh, free.charge_kwh)
        assert np.array_equal(generous.discharge_kwh, free.discharge_kwh)
        assert np.array_equal(generous.soc_kwh, free.soc_kwh)
        assert generous.ceiling_exceeding_intervals == free.ceiling_exceeding_intervals == 0

    def test_ceiling_and_causal_policy_agree(self) -> None:
        """Both dispatchers see the same ceiling, so the ceiling still dominates.

        Thresholds are explicit here (0.10 / 0.30 EUR/kWh) because a 25/75
        percentile fit on a horizon with one cheap block cannot produce a
        discharge signal at all.
        """
        s = small_battery()
        h = night_peak_horizon(import_limit_kw=2.0)
        nb = policy_no_battery(s, h)
        ca = policy_causal_price(s, h, charge_threshold=0.10, discharge_threshold=0.30)
        orc = solve_oracle(s, h)
        ceiling = h.interval_import_limit_kwh
        assert ceiling == pytest.approx(0.5)
        assert np.all(h.load_kwh + ca.charge_kwh <= ceiling + 1e-12)
        assert np.all(h.load_kwh + orc.charge_kwh <= ceiling + 1e-12)
        assert np.any(ca.charge_kwh > 0.0) and np.any(ca.discharge_kwh > 0.0)
        # 2.0 kW minus the 0.05 kWh night load: ceiling-bound, not power-bound
        assert np.all(ca.charge_kwh[ca.charge_kwh > 0.0] == pytest.approx(0.45))
        assert orc.cost_eur <= ca.cost_eur + 1e-9
        assert orc.cost_eur <= nb.cost_eur + 1e-9
        assert (
            check_feasible(s, h, ca.charge_kwh, ca.discharge_kwh, enforce_terminal_soc=False) == []
        )

    def test_load_alone_over_the_ceiling_is_feasible(self) -> None:
        """A measured load above the ceiling is history, not an infeasibility.

        The kernel records those intervals as an observation on the schedule;
        the oracle stays feasible and charges nothing in the ceiling-exceeding
        intervals (headroom zero).
        """
        s = spec()
        h = horizon([0.1, 0.2], [1.0, 1.0], import_limit_kw=0.5)  # 0.5 kWh < 1.0 kWh load
        sched = solve_oracle(s, h)
        assert sched.ceiling_exceeding_intervals == 2
        assert np.all(sched.charge_kwh == 0.0)
        assert np.allclose(sched.grid_kwh, h.load_kwh)
        assert check_feasible(s, h, sched.charge_kwh, sched.discharge_kwh) == []
        # and the kernel reports the same observation without solving anything
        assert np.array_equal(ceiling_exceedance_indices(h, sched.grid_kwh), [0, 1])

    def test_charge_is_zero_only_in_the_breaching_intervals(self) -> None:
        """Headroom is per-interval: the oracle charges where the load leaves room
        and nowhere it does not.

        t1 carries a load above the ceiling inside an otherwise cheap horizon.
        The optimum must still charge the neighbouring cheap intervals (up to
        exactly the residual) and take nothing at t1 — no forced discharge, no
        infeasibility, no invented slack.
        """
        s = spec(
            capacity_kwh=10.0,
            power_kw=5.0,
            charge_efficiency=1.0,
            discharge_efficiency=1.0,
            initial_soc_frac=0.0,
            terminal_soc_frac=0.5,
        )
        # ceiling 6 kWh/interval; load 1.0 except t1 = 7.0 (above it)
        h = horizon(
            [0.01, 0.01, 0.01, 0.6, 0.6],
            [1.0, 1.0, 7.0, 5.0, 5.0],
            interval_seconds=HOUR,
            import_limit_kw=6.0,
        )
        assert np.all(h.charge_headroom_kwh == pytest.approx([5.0, 5.0, 0.0, 1.0, 1.0]))
        sched = solve_oracle(s, h)
        assert sched.charge_kwh[2] == 0.0  # breaching interval: charging unavailable
        # the cheap neighbours take the whole residual headroom, nothing more
        assert np.all(sched.charge_kwh[[0, 1]] == pytest.approx(5.0))
        assert np.all(sched.charge_kwh <= h.charge_headroom_kwh + 1e-9)
        # it discharges on the expensive tail instead, never exporting.  Which of
        # the two equally-priced peak hours gets the energy is degenerate, so the
        # total is what is asserted.
        assert float(sched.discharge_kwh[[3, 4]].sum()) == pytest.approx(5.0, abs=1e-6)
        assert np.all(sched.discharge_kwh[[3, 4]] <= 5.0 + 1e-9)
        assert np.all(sched.discharge_kwh[[0, 1, 2]] == 0.0)
        assert np.all(sched.grid_kwh >= -1e-9)
        # net import at t1 equals the load and tops the ceiling — an observation
        assert sched.grid_kwh[2] == pytest.approx(7.0)
        assert sched.ceiling_exceeding_intervals == 1
        assert check_feasible(s, h, sched.charge_kwh, sched.discharge_kwh) == []
        assert sched.solver_status == "optimal" and sched.is_certified

    def test_headroom_below_the_power_rating_is_taken_exactly(self) -> None:
        """Residual, not rating: 0.05 kWh of load under a 0.25 kWh ceiling leaves
        0.20 kWh of charge even though the battery could take 0.625."""
        s = small_battery()
        h = night_peak_horizon(n_cheap=4, n_peak=4, import_limit_kw=1.0)  # 0.25 kWh ceiling
        sched = solve_oracle(s, h)
        assert np.all(sched.charge_kwh[:4] == pytest.approx(0.20, abs=1e-6))
        assert s.interval_energy_limit_kwh(QUARTER) == pytest.approx(0.625)
        # exactly at the ceiling there is nothing left, and that is not a breach
        at_ceiling = night_peak_horizon(
            n_cheap=4, n_peak=4, cheap_load=0.25, peak_load=0.25, import_limit_kw=1.0
        )
        zero = solve_oracle(s, at_ceiling)
        assert np.all(zero.charge_kwh == 0.0)
        assert zero.ceiling_exceeding_intervals == 0

    def test_realistic_breaching_day_solves_and_dominates(self) -> None:
        """End-to-end regression mirroring the archive: 5.0 kW contract, 6.9 kW spike.

        One-minute intervals (the resolution the real measurements are checked at)
        around a ~0.5 kW baseload with a sustained ~10-minute 6.94 kW event — the
        2026-10-18 shape that tripped nothing.  The event sits in an expensive hour,
        so the oracle discharges into it (that is what the ceiling now permits) while
        charging stops for the ten minutes it lasts.
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
        n = 60  # one hour of one-minute intervals
        load = np.full(n, 0.5 / 60.0)  # 0.5 kW baseload
        load[20:30] = 6.94 / 60.0  # the sustained ~10-minute event
        prices = np.full(n, 0.30)
        prices[:20] = 0.02  # cheap run-in: charging happens here
        prices[30:40] = 0.02  # cheap again right after the event
        prices[20:30] = 0.40  # the event is the most expensive minute block
        h = horizon(prices, load, interval_seconds=60.0, import_limit_kw=5.0)
        ceiling = h.interval_import_limit_kwh
        assert ceiling == pytest.approx(5.0 / 60.0)
        breach = np.flatnonzero(load > ceiling + 1e-12)
        assert breach.size == 10  # the event, and nothing else

        nb = policy_no_battery(s, h)
        assert nb.ceiling_exceeding_intervals == 10  # measured, not judged
        assert np.allclose(nb.grid_kwh, load)

        ca = policy_causal_price(s, h, charge_threshold=0.10, discharge_threshold=0.30)
        assert np.all(ca.charge_kwh[breach] == 0.0)  # no headroom during the event
        assert np.any(ca.charge_kwh[:20] > 0.0)  # charges normally before it
        assert np.any(ca.charge_kwh[30:40] > 0.0)  # and normally after it

        fixed = solve_oracle(s, h)
        assert np.all(fixed.charge_kwh[breach] == 0.0)
        assert np.any(fixed.charge_kwh[:20] > 0.0)
        # discharge inside the event is permitted, and the price makes the oracle
        # take it: it lowers the draw instead of being forbidden or forced
        assert np.any(fixed.discharge_kwh[breach] > 0.0)
        assert np.all(fixed.grid_kwh >= -1e-9)
        assert fixed.ceiling_exceeding_intervals < nb.ceiling_exceeding_intervals
        assert fixed.cost_eur <= nb.cost_eur + 1e-9

        free = solve_oracle(s, h, terminal_mode="free", terminal_value_eur_per_kwh=0.0)
        assert np.all(free.charge_kwh[breach] == 0.0)
        assert free.cost_eur <= ca.cost_eur + 1e-9  # dominance over the causal plan
        assert free.cost_eur <= nb.cost_eur + 1e-9  # and over measured history
        for sched in (nb, ca, fixed, free):
            assert (
                check_feasible(
                    s, h, sched.charge_kwh, sched.discharge_kwh, enforce_terminal_soc=False
                )
                == []
            )

    def test_unknown_ceiling_and_a_generous_one_agree_under_a_breaching_load(self) -> None:
        """``None`` is unconstrained; a ceiling far above the load constrains nothing.

        Same *horizon*, ceiling unknown versus 50 kW (12.5 kWh per 15-minute
        interval): the plan must come back bit-for-bit, and the exceedance
        observation must be zero in the generous case only because no load
        reaches 12.5 kWh.
        """
        s = small_battery()
        kwarg_free = dict(n_cheap=8, n_peak=8)
        plain = solve_oracle(s, night_peak_horizon(**kwarg_free))
        generous = solve_oracle(s, night_peak_horizon(import_limit_kw=50.0, **kwarg_free))
        assert generous.cost_eur == plain.cost_eur
        assert np.array_equal(generous.charge_kwh, plain.charge_kwh)
        assert np.array_equal(generous.discharge_kwh, plain.discharge_kwh)
        assert np.array_equal(generous.soc_kwh, plain.soc_kwh)
        assert generous.ceiling_exceeding_intervals == 0
        assert plain.ceiling_exceeding_intervals == 0
        # a ceiling the load does breach is still solvable, and reports the breach
        breach_h = night_peak_horizon(n_cheap=8, n_peak=8, peak_load=1.5, import_limit_kw=5.0)
        nb = policy_no_battery(s, breach_h)
        # 1.5 kWh > 5.0 kW * 0.25 h = 1.25 kWh, and a baseline has nothing to shave
        # it with: every peak interval is an observation, not an error
        assert nb.ceiling_exceeding_intervals == 8
        assert np.all(nb.grid_kwh[8:] == pytest.approx(1.5))
        breached = solve_oracle(s, breach_h)
        assert breached.ceiling_exceeding_intervals <= nb.ceiling_exceeding_intervals
        assert np.all(breached.charge_kwh[8:] == 0.0)  # no headroom in a breaching hour
        assert np.all(breached.discharge_kwh[8:] > 0.0)  # discharge is how it responds
        assert np.all(breached.grid_kwh >= -1e-9)
        assert breached.cost_eur <= nb.cost_eur + 1e-9
        assert breached.solver_status == "optimal"

    def test_terminal_soc_unreachable_under_the_ceiling_is_infeasible(self) -> None:
        """10 kWh of terminal SOC needs 10 kWh of charge; 2 intervals at
        (3 kW - 0 kWh load) can only deliver 6 kWh."""
        s = spec(capacity_kwh=10.0, power_kw=5.0, initial_soc_frac=0.0, terminal_soc_frac=1.0)
        h = horizon([0.1, 0.2], [0.0, 0.0], import_limit_kw=3.0)
        with pytest.raises(OracleInfeasibleError, match="infeasible"):
            solve_oracle(s, h)
        # the same spec is feasible the moment the ceiling is unknown
        feasible = solve_oracle(s, horizon([0.1, 0.2], [0.0, 0.0]))
        assert feasible.soc_kwh[-1] == pytest.approx(10.0, abs=1e-6)


# ---------------------------------------------------------------------------
# 6b. Dominance re-verified with the charge cap gone, and its one honest limit
# ---------------------------------------------------------------------------


def night_heavy_prices(n: int, rng: np.random.Generator) -> np.ndarray:
    """Cheap 22:00-05:00 hours, expensive morning/evening, all non-negative."""
    hours = (np.arange(n) / 4.0 + 18.0) % 24.0
    cheap = (hours >= 22) | (hours < 5)
    return np.where(cheap, rng.uniform(0.0, 0.08, n), rng.uniform(0.15, 0.5, n))


class TestDominanceWithoutChargeCap:
    """Low-load, night-heavy horizons: exactly where ``charge <= load`` bit.

    Removing the cap widens the causal policy's actions, so dominance is not a
    consequence of nested feasible sets any more — it is re-measured here on
    randomized patterns, on closed cycles, and with and without a ceiling.
    """

    @pytest.mark.parametrize("seed", [101, 202, 303, 404, 505])
    @pytest.mark.parametrize("import_limit_kw", [None, 5.0, 2.0])
    def test_ceiling_holds_on_low_load_night_heavy_patterns(
        self, seed: int, import_limit_kw: float | None
    ) -> None:
        rng = np.random.default_rng(seed)
        n = 96
        prices = night_heavy_prices(n, rng)
        load = np.full(n, 0.05) + rng.uniform(0.0, 0.15, n)  # servers plus a little else
        s = spec(
            capacity_kwh=5.12,
            power_kw=2.5,
            charge_efficiency=0.95,
            discharge_efficiency=0.95,
            soc_min_frac=0.05,
            soc_max_frac=0.95,
            initial_soc_frac=0.2,
        )
        h = horizon(prices, load, interval_seconds=QUARTER, import_limit_kw=import_limit_kw)
        nb = policy_no_battery(s, h)
        ct, dt = fit_thresholds(prices, charge_pct=25.0, discharge_pct=75.0)
        ca = policy_causal_price(s, h, charge_threshold=ct, discharge_threshold=dt)
        orc = solve_oracle(s, h)

        assert orc.cost_eur <= ca.cost_eur + 1e-9
        assert orc.cost_eur <= nb.cost_eur + 1e-9
        assert orc.savings_vs(nb) >= ca.savings_vs(nb) - 1e-9
        # the charge cap is gone: charging exceeds the load, import never goes below 0
        assert np.any(ca.charge_kwh > load)
        assert np.all(ca.grid_kwh >= -1e-12)
        assert np.all(orc.grid_kwh >= -1e-12)
        # seed protection still holds: the policy never spends the initial charge
        assert np.all(ca.soc_kwh >= s.soc_bound_kwh("init") - 1e-9)
        if import_limit_kw is not None:
            ceiling = h.interval_import_limit_kwh
            assert np.all(h.load_kwh + ca.charge_kwh <= ceiling + 1e-12)
            assert np.all(h.load_kwh + orc.charge_kwh <= ceiling + 1e-12)

    def test_full_power_night_charging_beats_the_old_capped_policy(self) -> None:
        """Same horizon: the uncapped causal plan is strictly cheaper than what a
        ``charge <= load`` rule could do, and the oracle still sits below it."""
        s = spec(
            capacity_kwh=5.12,
            power_kw=2.5,
            charge_efficiency=0.95,
            discharge_efficiency=0.95,
            soc_min_frac=0.05,
            soc_max_frac=0.95,
            initial_soc_frac=0.05,
        )
        n = 96
        prices = night_heavy_prices(n, np.random.default_rng(606))
        load = np.full(n, 0.05)
        h = horizon(prices, load, interval_seconds=QUARTER)
        ca = policy_causal_price(s, h, charge_threshold=0.10, discharge_threshold=0.30)
        # what the removed rule would have allowed, reconstructed here
        capped_charge = np.minimum(ca.charge_kwh, load)
        assert np.any(capped_charge < ca.charge_kwh - 1e-9)
        nb = policy_no_battery(s, h)
        orc = solve_oracle(s, h)
        assert orc.cost_eur <= ca.cost_eur + 1e-9
        assert orc.cost_eur <= nb.cost_eur + 1e-9
        assert (
            check_feasible(s, h, ca.charge_kwh, ca.discharge_kwh, enforce_terminal_soc=False) == []
        )

    def test_open_cycle_under_all_negative_prices_is_not_a_dominance_claim(self) -> None:
        """The one honest limit, documented rather than clipped away.

        With every price negative, charging pays the household, and a causal plan
        that never closes its cycle keeps energy the horizon accounting does not
        value — so it can come out *cheaper* than the terminal-neutral oracle.
        That is the ADR's "initial and terminal SOC are equal or have an explicit
        defensible valuation" requirement, not a solver bug: the fix is to value
        or close the cycle, never to floor the causal cost.
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
        n = 8
        h = horizon(np.full(n, -0.2), np.full(n, 0.05), interval_seconds=QUARTER)
        ca = policy_causal_price(s, h, charge_threshold=0.0, discharge_threshold=0.3)
        orc = solve_oracle(s, h)
        # the causal plan ends at the SOC ceiling holding 4.608 kWh of unvalued energy
        assert ca.soc_kwh[-1] == pytest.approx(s.soc_bound_kwh("max"), abs=1e-6)
        assert ca.soc_kwh[-1] > s.soc_bound_kwh("init")
        assert any(
            "terminal SOC" in v for v in check_feasible(s, h, ca.charge_kwh, ca.discharge_kwh)
        )
        # the oracle close the cycle, so it moves far less energy
        assert orc.soc_kwh[-1] == pytest.approx(s.soc_bound_kwh("init"), abs=1e-6)
        assert ca.charge_kwh.sum() > orc.charge_kwh.sum()
        # and on this horizon the open cycle looks cheaper — reported, not hidden
        assert ca.cost_eur < orc.cost_eur
        assert np.all(ca.grid_kwh >= 0.0)
        assert np.all(orc.grid_kwh >= 0.0)
        # close the accounting on equal terms and the ordering is back: with
        # non-negative prices, energy left in the battery is a *cost*, so the
        # open cycle loses to the closed one instead of beating it
        balanced = horizon(
            np.array([0.02] * 6 + [0.40] * 2),
            np.full(8, 0.05),
            interval_seconds=QUARTER,
        )
        ca_up = policy_causal_price(s, balanced, charge_threshold=0.10, discharge_threshold=0.30)
        orc_up = solve_oracle(s, balanced)
        nb_up = policy_no_battery(s, balanced)
        assert ca_up.soc_kwh[-1] > s.soc_bound_kwh("init")  # still hoarding
        assert ca_up.cost_eur > nb_up.cost_eur  # and paying for it
        assert orc_up.cost_eur <= ca_up.cost_eur + 1e-9

    def test_negative_prices_still_cannot_be_farmed_by_the_kernel(self) -> None:
        """Requirement 7: the binary mode is untouched by this change."""
        rng = np.random.default_rng(707)
        s = spec(
            capacity_kwh=5.12,
            power_kw=2.5,
            charge_efficiency=0.9,
            discharge_efficiency=0.9,
            soc_min_frac=0.05,
            soc_max_frac=0.95,
            initial_soc_frac=0.5,
        )
        for trial in range(12):
            n = 24 + trial
            prices = rng.uniform(-1.5, 0.8, n)
            load = rng.uniform(0.02, 0.4, n)
            h = horizon(prices, load, interval_seconds=QUARTER)
            ca = policy_causal_price(s, h, charge_threshold=-0.2, discharge_threshold=0.4)
            orc = solve_oracle(s, h)
            for sched in (ca, orc):
                assert not np.any((sched.charge_kwh > 1e-9) & (sched.discharge_kwh > 1e-9)), trial
                assert np.all(sched.grid_kwh >= -1e-12), trial
            assert check_feasible(s, h, orc.charge_kwh, orc.discharge_kwh) == []
            assert orc.cost_eur <= policy_no_battery(s, h).cost_eur + 1e-9


# ---------------------------------------------------------------------------
# 8. Infeasibility raises
# ---------------------------------------------------------------------------


class TestInfeasibleRaises:
    def test_inverted_soc_window_raises(self) -> None:
        s = spec(soc_min_frac=0.8, soc_max_frac=0.2)
        h = horizon([0.1, 0.2], [1.0, 1.0])
        with pytest.raises(ValueError, match="soc_min_frac < soc_max_frac"):
            solve_oracle(s, h)

    @pytest.mark.parametrize(
        "override",
        [
            dict(capacity_kwh=0.0),
            dict(power_kw=-1.0),
            dict(charge_efficiency=0.0),
            dict(discharge_efficiency=1.7),
            dict(soc_max_frac=1.5),
            dict(initial_soc_frac=1.5),
            dict(terminal_soc_frac=1.5),
        ],
    )
    def test_degenerate_specs_raise_value_error(self, override: dict) -> None:
        with pytest.raises(ValueError):
            solve_oracle(spec(**override), horizon([0.1, 0.2], [1.0, 1.0]))

    def test_unreachable_terminal_target_raises(self) -> None:
        """10 kWh of SOC cannot be filled through a 1 kW limit in two intervals."""
        s = spec(charge_efficiency=0.9, initial_soc_frac=0.0, terminal_soc_frac=1.0)
        h = horizon([0.1, 0.2], [5.0, 5.0])
        with pytest.raises(OracleInfeasibleError, match="infeasible"):
            solve_oracle(s, h)

    def test_seed_drain_into_zero_load_raises(self) -> None:
        """Full at the start, empty at the end, no load to receive the energy."""
        s = spec(initial_soc_frac=1.0, terminal_soc_frac=0.0)
        h = horizon([1.0, 2.0], [0.0, 0.0])
        with pytest.raises(OracleInfeasibleError):
            solve_oracle(s, h)

    def test_all_intervals_unusable_is_rejected_at_horizon(self) -> None:
        """A horizon with no usable intervals has an empty cost basis and is
        rejected before the oracle ever runs."""
        with pytest.raises(ValueError, match="usable excludes all"):
            horizon([0.2, 0.3], [1.0, 1.0], usable=[False, False])

    def test_solver_kernel_disagreement_surfaces_as_oracle_error(self, monkeypatch) -> None:
        """The oracle never leaks a physics error: it wraps the disagreement."""
        from energy_baseline import oracle as oracle_module

        def refusing(*args, **kwargs):
            raise BatteryPhysicsError("simulated kernel refusal")

        monkeypatch.setattr(oracle_module, "simulate", refusing)
        s = spec()
        h = horizon([0.1, 0.2], [1.0, 1.0])
        with pytest.raises(OracleError, match="kernel rejects") as excinfo:
            solve_oracle(s, h)
        assert isinstance(excinfo.value.__cause__, BatteryPhysicsError)
        # not mistakable for a bad-input ValueError from the caller
        assert not isinstance(excinfo.value, ValueError)

    def test_solver_errors_are_runtime_errors(self) -> None:
        s = spec(initial_soc_frac=1.0, terminal_soc_frac=0.0)
        with pytest.raises(RuntimeError):
            solve_oracle(s, horizon([1.0, 2.0], [0.0, 0.0]))
        assert issubclass(OracleInfeasibleError, OracleError)
        assert issubclass(OracleNoIncumbentError, OracleError)

    def test_bad_time_limit_and_tolerances_raise(self) -> None:
        s = spec()
        h = horizon([0.1, 0.2], [1.0, 1.0])
        with pytest.raises(ValueError, match="time_limit_s"):
            solve_oracle(s, h, time_limit_s=0.0)
        with pytest.raises(ValueError, match="time_limit_s"):
            solve_oracle(s, h, time_limit_s=-5.0)
        with pytest.raises(ValueError, match="mip_rel_gap"):
            solve_oracle(s, h, mip_rel_gap=0.0)
        with pytest.raises(ValueError, match="mip_abs_gap"):
            solve_oracle(s, h, mip_abs_gap=-1.0)


# ---------------------------------------------------------------------------
# 9. Time limits never hang and never fake optimality
# ---------------------------------------------------------------------------


class TestTimeLimit:
    def test_tiny_time_limit_returns_or_raises_cleanly(self) -> None:
        rng = np.random.default_rng(31)
        n = 96 * 4
        prices = rng.uniform(0.0, 0.4, n)
        load = rng.uniform(0.2, 1.5, n)
        s = spec(
            capacity_kwh=5.12,
            power_kw=2.5,
            charge_efficiency=0.95,
            discharge_efficiency=0.95,
            initial_soc_frac=0.2,
        )
        h = horizon(prices, load, interval_seconds=QUARTER)
        started = time.monotonic()
        try:
            sched = solve_oracle(s, h, time_limit_s=1e-9)
        except OracleNoIncumbentError:
            # acceptable: no feasible plan found in ~no time. Must not hang.
            assert time.monotonic() - started < 20.0
            return
        elapsed = time.monotonic() - started
        assert elapsed < 20.0
        # an incumbent came back: it is a bound, never a claimed optimum
        assert sched.is_certified is False
        assert sched.solver_status in ("time_limit", "feasible")
        assert sched.bound_kind == "upper"
        assert check_feasible(s, h, sched.charge_kwh, sched.discharge_kwh) == []
        assert sched.solver_gap is not None and sched.solver_gap >= 0.0

    def test_time_limited_incumbent_is_still_a_valid_ceiling_candidate(self) -> None:
        """A gapped incumbent keeps the feasible cost and the uncertified flag."""
        rng = np.random.default_rng(32)
        n = 96 * 7
        prices = rng.uniform(0.0, 0.4, n)
        load = rng.uniform(0.2, 1.5, n)
        s = spec(
            capacity_kwh=5.12,
            power_kw=2.5,
            charge_efficiency=0.95,
            discharge_efficiency=0.95,
            initial_soc_frac=0.2,
        )
        h = horizon(prices, load, interval_seconds=QUARTER)
        try:
            limited = solve_oracle(s, h, time_limit_s=0.01)
        except OracleNoIncumbentError:
            pytest.skip("solver found no incumbent inside 10 ms on this machine")
        assert limited.is_certified is False
        full = solve_oracle(s, h, time_limit_s=30.0)
        assert full.is_certified is True
        # the uncertified incumbent is an upper bound on optimal cost
        assert limited.cost_eur >= full.cost_eur - 1e-6
        assert limited.solver_gap > 0.0

    def test_certification_is_within_tolerance_not_zero_gap(self) -> None:
        """A loose tolerance certifies a genuinely gapped solution."""
        rng = np.random.default_rng(33)
        n = 96 * 7
        prices = rng.uniform(0.0, 0.4, n)
        load = rng.uniform(0.2, 1.5, n)
        s = spec(
            capacity_kwh=5.12,
            power_kw=2.5,
            charge_efficiency=0.95,
            discharge_efficiency=0.95,
            initial_soc_frac=0.2,
        )
        h = horizon(prices, load, interval_seconds=QUARTER)
        loose = solve_oracle(s, h, time_limit_s=30.0, mip_rel_gap=0.5, mip_abs_gap=0.5)
        tight = solve_oracle(s, h, time_limit_s=30.0, mip_rel_gap=1e-9, mip_abs_gap=1e-9)
        assert loose.is_certified is True  # "optimal" == within tolerance
        assert loose.solver_gap > 0.0
        assert loose.cost_eur >= tight.cost_eur - 1e-6
        assert tight.cost_eur <= loose.cost_eur + 1e-6


# ---------------------------------------------------------------------------
# Bound semantics (pure classifier: every branch, deterministically)
# ---------------------------------------------------------------------------


class TestBoundSemantics:
    def test_optimal_within_tolerance_is_certified(self) -> None:
        status, kind, certified, gap = classify_result(
            0,
            "Optimal solution found",
            primal=10.0,
            dual=9.9999999,
            mip_rel_gap=1e-4,
            mip_abs_gap=1e-6,
        )
        assert (status, kind, certified) == ("optimal", "certified", True)
        assert gap == pytest.approx(1e-8)

    def test_optimal_beyond_tolerance_is_demoted_to_a_bound(self) -> None:
        """HiGHS says optimal (its own tolerance) but ours is tighter -> not proven."""
        status, kind, certified, gap = classify_result(
            0, "Optimal solution found", primal=10.0, dual=8.0, mip_rel_gap=1e-4, mip_abs_gap=1e-6
        )
        assert status == "feasible"
        assert kind == "upper"
        assert certified is False
        assert gap == pytest.approx(0.2)

    def test_time_limit_message_is_reported_as_time_limit(self) -> None:
        status, kind, certified, gap = classify_result(
            1,
            "The time limit was reached. (HiGHS Status 7: model_status is TimeLimit)",
            primal=10.0,
            dual=4.0,
            mip_rel_gap=1e-4,
            mip_abs_gap=1e-6,
        )
        assert status == "time_limit"
        assert kind == "upper"
        assert certified is False
        assert gap == pytest.approx(0.6)

    def test_iteration_limit_is_reported_as_bare_feasible(self) -> None:
        status, kind, certified, _ = classify_result(
            1,
            "The iteration limit was reached. (HiGHS Status 12)",
            primal=10.0,
            dual=9.0,
            mip_rel_gap=1e-4,
            mip_abs_gap=1e-6,
        )
        assert (status, kind, certified) == ("feasible", "upper", False)

    def test_missing_dual_bound_is_an_infinite_gap(self) -> None:
        status, kind, certified, gap = classify_result(
            0, "Optimal solution found", primal=10.0, dual=None, mip_rel_gap=1e-4, mip_abs_gap=1e-6
        )
        assert (status, kind, certified) == ("feasible", "upper", False)
        assert gap == pytest.approx(np.inf)

    def test_negative_incumbent_cost_still_yields_a_positive_gap(self) -> None:
        """Prices can be negative, so the incumbent objective can be too."""
        status, kind, certified, gap = classify_result(
            0, "Optimal", primal=-10.0, dual=-12.0, mip_rel_gap=1e-4, mip_abs_gap=1e-6
        )
        assert (status, kind, certified) == ("feasible", "upper", False)
        assert gap == pytest.approx(2.0 / 12.0)  # denominator is the larger magnitude

    def test_zero_primal_and_dual_is_certified(self) -> None:
        status, kind, certified, gap = classify_result(
            0, "Optimal", primal=0.0, dual=0.0, mip_rel_gap=1e-4, mip_abs_gap=1e-6
        )
        assert (status, kind, certified) == ("optimal", "certified", True)
        assert gap == 0.0

    def test_absolute_tolerance_can_certify_a_loose_relative_gap(self) -> None:
        _, _, certified, _ = classify_result(
            0, "Optimal", primal=1e-4, dual=0.0, mip_rel_gap=1e-9, mip_abs_gap=1e-3
        )
        assert certified is True

    def test_bound_and_status_vocabularies_match_the_schema(self) -> None:
        """Strings must come from schemas.BENCHMARK_RESULT's documented sets."""
        bound_kind_allowed = {"none", "upper", "lower", "certified"}
        solver_status_allowed = {
            "not_applicable",
            "optimal",
            "feasible",
            "infeasible",
            "time_limit",
        }
        cases = [
            (0, "Optimal", 10.0, 10.0),
            (0, "Optimal", 10.0, 5.0),
            (0, "Optimal", 10.0, None),
            (1, "The time limit was reached", 10.0, 5.0),
            (1, "The iteration limit was reached", 10.0, 5.0),
            (4, "Solver interrupted", 10.0, 5.0),
        ]
        for status, msg, primal, dual in cases:
            solver_status, bound_kind, _, _ = classify_result(
                status, msg, primal=primal, dual=dual, mip_rel_gap=1e-4, mip_abs_gap=1e-6
            )
            assert bound_kind in bound_kind_allowed
            assert solver_status in solver_status_allowed
            # only a proven optimum may ever claim certification
            assert (solver_status == "optimal") == (bound_kind == "certified")


# ---------------------------------------------------------------------------
# Model layout
# ---------------------------------------------------------------------------


class TestModelLayout:
    def test_variable_and_constraint_counts(self) -> None:
        n = 5
        s = spec()
        h = horizon([0.1] * n, [1.0] * n)
        obj, integrality, bounds, constraints = build_model(s, h)
        assert obj.shape == (4 * n,)
        assert integrality.shape == (4 * n,)
        # exactly one binary per interval at index 3t+2; everything else continuous
        expected = np.zeros(4 * n)
        expected[np.arange(n) * 3 + 2] = 1.0
        assert np.array_equal(integrality, expected)
        # rows: 2n mutual exclusion + n SOC recurrence + n export guard.
        # No import ceiling on this horizon, so there are no ceiling rows.
        (constraint,) = constraints
        assert constraint.A.shape == (4 * n, 4 * n)
        assert bounds.lb.shape == bounds.ub.shape == (4 * n,)
        # flow variables are bounded by the per-interval energy limit
        limit = s.interval_energy_limit_kwh(HOUR)
        assert np.allclose(bounds.ub[: 3 * n : 3], limit)
        assert np.allclose(bounds.ub[1 : 3 * n : 3], limit)
        assert np.allclose(bounds.ub[2 : 3 * n : 3], 1.0)  # mode is 0/1
        assert np.all(bounds.lb[: 3 * n] == 0.0)

    def test_import_ceiling_adds_exactly_one_row_per_interval(self) -> None:
        """`charge[t] <= max(0, import_limit_kw * h - load[t])`.

        The ceiling limits the *charging decision* by the headroom the load
        leaves, so each ceiling row has a single nonzero: ``+1`` on ``charge[t]``.
        Discharge is not part of this row (it lowers the draw and stays bounded
        by the no-export rule alone), and the per-interval right-hand side is
        floored at zero so a ceiling-exceeding load gives ``charge[t] = 0``
        instead of an infeasible negative upper bound.
        """
        n = 5
        s = spec()
        h = horizon([0.1] * n, [0.4] * n, interval_seconds=HOUR, import_limit_kw=1.4)
        _, _, _, constraints = build_model(s, h)
        (constraint,) = constraints
        assert constraint.A.shape == (5 * n, 4 * n)
        ceiling = h.interval_import_limit_kwh
        assert ceiling == pytest.approx(1.4)

        # the ceiling block is the last n rows: +1 on charge only, RHS is headroom
        rows = np.asarray(constraint.A.tocsr()[4 * n :].todense())
        assert rows.shape == (n, 4 * n)
        for t in range(n):
            row = rows[t]
            assert np.count_nonzero(row) == 1
            assert row[3 * t] == pytest.approx(1.0)  # charge coefficient
        assert np.allclose(constraint.lb[4 * n :], -np.inf)
        assert np.allclose(constraint.ub[4 * n :], np.maximum(ceiling - 0.4, 0.0))

        # and the earlier blocks are untouched: same 4n rows as the free model
        free = build_model(s, horizon([0.1] * n, [0.4] * n, interval_seconds=HOUR))[3][0]
        assert np.allclose(
            np.asarray(free.A.todense()), np.asarray(constraint.A.tocsr()[: 4 * n].todense())
        )
        assert np.allclose(free.ub[: 4 * n], constraint.ub[: 4 * n])

    def test_ceiling_headroom_rhs_is_floored_at_zero(self) -> None:
        """A load above the ceiling gives a zero RHS, never a negative one.

        The old net-grid form (``charge - discharge <= ceiling - load``) with a
        ceiling-exceeding load demanded discharge: ``-discharge <= ceiling - load``
        is a *lower* bound on discharge, so an empty battery made the whole
        problem infeasible.  Flooring the headroom at zero is what keeps the
        measured past from invalidating the model.
        """
        n = 3
        s = spec()
        h = horizon([0.1] * n, [0.4, 2.0, 0.4], interval_seconds=HOUR, import_limit_kw=1.0)
        _, _, _, constraints = build_model(s, h)
        (constraint,) = constraints
        rhs = constraint.ub[4 * n :]
        assert np.all(rhs >= 0.0)
        assert np.allclose(rhs, [0.6, 0.0, 0.6])
        # solve it: feasible, and the mid interval charges nothing
        sched = solve_oracle(s, h)
        assert sched.charge_kwh[1] == pytest.approx(0.0)
        assert sched.ceiling_exceeding_intervals == 1

    def test_solver_objective_matches_reported_cost(self) -> None:
        """c @ x equals the reported cost minus the constant load term."""
        s = spec(charge_efficiency=0.9, discharge_efficiency=0.9)
        h = horizon([0.1, 0.4, -0.2], [1.0, 2.0, 3.0])
        obj, integrality, bounds, constraints = build_model(s, h)
        res = milp(
            c=obj,
            integrality=integrality,
            bounds=bounds,
            constraints=constraints,
            options={"disp": False},
        )
        n = h.n
        charge = res.x[: 3 * n : 3]
        discharge = res.x[1 : 3 * n : 3]
        grid = h.load_kwh + charge - discharge
        cost_from_plan = float(np.dot(h.prices, grid))
        assert float(obj @ res.x) == pytest.approx(cost_from_plan - load_term(h))
        sched = solve_oracle(s, h)
        assert sched.cost_eur == pytest.approx(cost_from_plan, abs=1e-6)

    def test_objective_coefficients_are_pretax_and_usable_scoped(self) -> None:
        s = spec()
        prices = [0.1, 0.2, 0.3]
        h = horizon(prices, [1.0, 1.0, 1.0], usable=[True, False, True])
        obj, _, _, _ = build_model(s, h)
        n = h.n
        assert obj[: 3 * n : 3].tolist() == [0.1, 0.0, 0.3]  # charge coefficients
        assert obj[1 : 3 * n : 3].tolist() == [-0.1, 0.0, -0.3]  # discharge coefficients
        assert obj[2 : 3 * n : 3].tolist() == [0.0, 0.0, 0.0]  # mode costs nothing

    def test_soc_block_bounds_encode_window_and_terminal(self) -> None:
        s = spec(capacity_kwh=10.0, soc_min_frac=0.2, soc_max_frac=0.9, initial_soc_frac=0.5)
        n = 4
        h = horizon([0.1] * n, [1.0] * n)
        _, _, bounds, _ = build_model(s, h)
        soc_lb = bounds.lb[3 * n :]
        soc_ub = bounds.ub[3 * n :]
        assert soc_lb[:-1] == pytest.approx([2.0] * (n - 1))
        assert soc_ub[:-1] == pytest.approx([9.0] * (n - 1))
        # terminal SOC is pinned on the last interval (== initial when None)
        assert soc_lb[-1] == pytest.approx(5.0)
        assert soc_ub[-1] == pytest.approx(5.0)

    def test_unusable_intervals_are_blocked_in_the_model(self) -> None:
        s = spec()
        h = horizon([0.1, 0.2, 0.3], [1.0, 1.0, 1.0], usable=[True, False, True])
        _, integrality, bounds, _ = build_model(s, h)
        n = h.n
        assert bounds.ub[3 * 1] == 0.0  # charge[t=1]
        assert bounds.ub[3 * 1 + 1] == 0.0  # discharge[t=1]
        assert bounds.ub[3 * 0] == pytest.approx(s.interval_energy_limit_kwh(HOUR))
        assert np.count_nonzero(integrality) == n

    def test_mode_variables_are_integral_in_the_solution(self) -> None:
        rng = np.random.default_rng(41)
        n = 24
        prices = rng.uniform(-0.2, 0.4, n)
        load = rng.uniform(0.4, 1.8, n)
        s = spec(charge_efficiency=0.9, discharge_efficiency=0.9)
        h = horizon(prices, load)
        obj, integrality, bounds, constraints = build_model(s, h)
        # a feasibility-only objective: still an integral mode vector
        res = milp(
            c=np.zeros_like(obj),
            integrality=integrality,
            bounds=bounds,
            constraints=constraints,
            options={"disp": False},
        )
        assert res.success
        modes = res.x[2 : 3 * n : 3]
        assert np.all(np.isclose(modes, np.round(modes), atol=1e-6))
        assert set(np.round(modes).tolist()) <= {0.0, 1.0}

    def test_determinism(self) -> None:
        rng = np.random.default_rng(42)
        n = 48
        prices = rng.uniform(0.0, 0.4, n)
        load = rng.uniform(0.3, 1.7, n)
        s = spec(
            capacity_kwh=5.12,
            power_kw=2.5,
            charge_efficiency=0.95,
            discharge_efficiency=0.95,
            initial_soc_frac=0.2,
        )
        h = horizon(prices, load, interval_seconds=QUARTER)
        a = solve_oracle(s, h)
        b = solve_oracle(s, h)
        assert a.cost_eur == b.cost_eur
        assert np.array_equal(a.charge_kwh, b.charge_kwh)
        assert np.array_equal(a.discharge_kwh, b.discharge_kwh)

    def test_inputs_are_not_mutated(self) -> None:
        s = spec()
        prices = np.array([0.05, 0.3, 0.4, 0.1])
        load = np.array([1.0, 1.0, 1.0, 1.0])
        usable = np.array([True, False, True, True])
        h = horizon(prices, load, usable=usable)
        p0, l0, u0 = prices.copy(), load.copy(), usable.copy()
        solve_oracle(s, h)
        assert np.array_equal(prices, p0)
        assert np.array_equal(load, l0)
        assert np.array_equal(usable, u0)


# ---------------------------------------------------------------------------
# Scheduling sanity on a realistic day
# ---------------------------------------------------------------------------


class TestRealisticDay:
    def test_day_night_pattern_shifts_energy_to_the_peak(self) -> None:
        rng = np.random.default_rng(51)
        n = 96
        hours = np.arange(n) / 4.0
        cheap = 0.08 + rng.uniform(0.0, 0.01, n)
        expensive = 0.38 + rng.uniform(0.0, 0.01, n)
        night = hours % 24 > 18
        prices = np.where(night, expensive, cheap)
        load = 0.4 + 0.6 * np.sin(np.clip(hours, 0, 24) / 24 * np.pi) ** 2
        s = spec(
            capacity_kwh=5.12,
            power_kw=2.5,
            charge_efficiency=0.95,
            discharge_efficiency=0.95,
            soc_min_frac=0.05,
            soc_max_frac=0.95,
            initial_soc_frac=0.5,
        )
        h = horizon(prices, load, interval_seconds=QUARTER)
        orc = solve_oracle(s, h)
        in_expensive = prices > 0.3
        assert orc.cost_eur < policy_no_battery(s, h).cost_eur
        assert np.any(orc.charge_kwh > 0.0)
        assert np.any(orc.discharge_kwh[in_expensive] > 0.0)  # sold into the peak
        assert np.all(orc.charge_kwh[in_expensive] == 0.0)  # never buys at the peak
        assert orc.soc_kwh[-1] == pytest.approx(5.12 * 0.5, abs=1e-6)
        assert check_feasible(s, h, orc.charge_kwh, orc.discharge_kwh) == []
        assert orc.solver_status == "optimal" and orc.is_certified

    def test_gap_exclusions_are_identical_across_the_three_policies(self) -> None:
        """Same usable mask => same cost basis for no-battery, causal and oracle."""
        rng = np.random.default_rng(52)
        n = 60
        prices = rng.uniform(0.02, 0.4, n)
        load = rng.uniform(0.3, 1.5, n)
        usable = rng.random(n) > 0.2
        s = spec(
            capacity_kwh=5.12,
            power_kw=2.5,
            charge_efficiency=0.95,
            discharge_efficiency=0.95,
            initial_soc_frac=0.2,
        )
        h = horizon(prices, load, usable, interval_seconds=QUARTER)
        nb = policy_no_battery(s, h)
        ct, dt = fit_thresholds(prices, charge_pct=25.0, discharge_pct=75.0)
        ca = policy_causal_price(s, h, charge_threshold=ct, discharge_threshold=dt)
        orc = solve_oracle(s, h)
        assert usable.sum() < n  # the mask really does exclude something
        for sched in (nb, ca, orc):
            assert np.all(sched.charge_kwh[~usable] == 0.0)
            assert np.all(sched.discharge_kwh[~usable] == 0.0)
            # cost recomputed from the schedule on the shared basis
            manual = float(np.dot(prices[usable], sched.grid_kwh[usable]))
            assert sched.cost_eur == pytest.approx(manual)
            # and the excluded intervals' own cost never leaks into the total
            assert sched.cost_eur != pytest.approx(float(np.dot(prices, sched.grid_kwh)))
        assert orc.cost_eur <= ca.cost_eur + 1e-9
        assert orc.cost_eur <= nb.cost_eur + 1e-9


# ---------------------------------------------------------------------------
# Free terminal mode — shared causal floor, explicit inventory valuation
# ---------------------------------------------------------------------------


class TestFreeTerminalMode:
    """The free oracle lets the end SOC float within [initial, max] and prices the
    leftover inventory in the objective.  It shares the causal floor
    ``max(soc_min, initial)`` with the causal policy, so both sit on one common
    feasible set and the dominance ``oracle <= causal <= baseline`` holds even
    when all prices are negative."""

    def test_free_oracle_dominates_causal_and_baseline_with_zero_value(self) -> None:
        """The benchmark framing: terminal_value=0, common causal floor."""
        rng = np.random.default_rng(801)
        n = 96
        prices = rng.uniform(0.02, 0.42, n)
        load = rng.uniform(0.15, 1.6, n)
        s = spec(
            capacity_kwh=5.12,
            power_kw=2.5,
            charge_efficiency=0.95,
            discharge_efficiency=0.95,
            soc_min_frac=0.05,
            soc_max_frac=0.95,
            initial_soc_frac=0.2,
        )
        h = horizon(prices, load, interval_seconds=QUARTER)
        nb = policy_no_battery(s, h)
        ct, dt = fit_thresholds(prices, charge_pct=25.0, discharge_pct=75.0)
        ca = policy_causal_price(s, h, charge_threshold=ct, discharge_threshold=dt)
        free = solve_oracle(s, h, terminal_mode="free", terminal_value_eur_per_kwh=0.0)
        assert free.cost_eur <= ca.cost_eur + 1e-9
        assert free.cost_eur <= nb.cost_eur + 1e-9
        # the free plan shares the causal floor: never below initial SOC
        assert np.all(free.soc_kwh >= s.soc_bound_kwh("init") - 1e-9)
        # and the terminal SOC is free in [initial, max]
        assert free.terminal_soc_kwh >= s.soc_bound_kwh("init") - 1e-9
        assert free.terminal_soc_kwh <= s.soc_bound_kwh("max") + 1e-9
        # inventory adjustment is zero when value is zero
        assert free.inventory_adjustment_eur == pytest.approx(0.0)
        assert free.terminal_value_eur_per_kwh == pytest.approx(0.0)

    def test_free_oracle_dominates_under_all_negative_prices(self) -> None:
        """The honest limit: with all-negative prices, the free oracle (value 0)
        still dominates the causal policy — both can hoard to the ceiling, but
        the oracle does it optimally."""
        s = spec(
            capacity_kwh=5.12,
            power_kw=2.5,
            charge_efficiency=0.95,
            discharge_efficiency=0.95,
            soc_min_frac=0.05,
            soc_max_frac=0.95,
            initial_soc_frac=0.05,
        )
        n = 8
        h = horizon(np.full(n, -0.2), np.full(n, 0.05), interval_seconds=QUARTER)
        ca = policy_causal_price(s, h, charge_threshold=0.0, discharge_threshold=0.3)
        free = solve_oracle(s, h, terminal_mode="free", terminal_value_eur_per_kwh=0.0)
        nb = policy_no_battery(s, h)
        # free oracle dominates both
        assert free.cost_eur <= ca.cost_eur + 1e-9
        assert free.cost_eur <= nb.cost_eur + 1e-9
        # both hoard to the ceiling (negative prices = free charging)
        assert ca.soc_kwh[-1] == pytest.approx(s.soc_bound_kwh("max"), abs=1e-6)
        assert free.terminal_soc_kwh == pytest.approx(s.soc_bound_kwh("max"), abs=1e-6)
        # and the free oracle's cost is at most the causal cost (both negative)
        assert free.cost_eur <= ca.cost_eur + 1e-9

    def test_free_oracle_values_terminal_inventory(self) -> None:
        """With a positive terminal value, the oracle keeps charge it would
        otherwise cycle back out — the leftover inventory is worth something."""
        s = spec(
            capacity_kwh=10.0,
            power_kw=1.0,
            charge_efficiency=1.0,
            discharge_efficiency=1.0,
            soc_min_frac=0.0,
            soc_max_frac=1.0,
            initial_soc_frac=0.0,
        )
        h = horizon([0.1, 0.5], [1.0, 1.0])
        free_zero = solve_oracle(s, h, terminal_mode="free", terminal_value_eur_per_kwh=0.0)
        free_high = solve_oracle(s, h, terminal_mode="free", terminal_value_eur_per_kwh=10.0)
        # power-limited: at most 1.0 kWh/interval over 2 intervals = 2.0 kWh stored
        assert free_high.terminal_soc_kwh == pytest.approx(2.0, abs=1e-6)
        # with zero value the cheap energy is cycled back out (returns to seed)
        assert free_zero.terminal_soc_kwh == pytest.approx(0.0, abs=1e-6)
        # a high terminal value keeps the charge it paid for cheap
        assert free_high.terminal_soc_kwh > free_zero.terminal_soc_kwh
        # and the inventory adjustment is a credit (negative cost) for storing
        assert free_high.inventory_adjustment_eur < 0.0
        # the total objective (meter + inventory) is lower than with zero value
        assert free_high.cost_eur < free_zero.cost_eur

    def test_free_oracle_rejects_arbitrary_terminal_target(self) -> None:
        """A free plan must not smuggle in an arbitrary terminal target."""
        s = spec(initial_soc_frac=0.5, terminal_soc_frac=0.8)
        h = horizon([0.1, 0.2], [1.0, 1.0])
        with pytest.raises(ValueError, match="arbitrary terminal target"):
            solve_oracle(s, h, terminal_mode="free", terminal_value_eur_per_kwh=0.0)

    def test_free_oracle_requires_explicit_terminal_value(self) -> None:
        """A free plan must state an explicit valuation for the leftover."""
        s = spec()
        h = horizon([0.1, 0.2], [1.0, 1.0])
        with pytest.raises(ValueError, match="explicit terminal_value"):
            solve_oracle(s, h, terminal_mode="free")

    def test_free_oracle_rejects_non_finite_terminal_value(self) -> None:
        s = spec()
        h = horizon([0.1, 0.2], [1.0, 1.0])
        with pytest.raises(ValueError, match="finite"):
            solve_oracle(s, h, terminal_mode="free", terminal_value_eur_per_kwh=np.inf)

    def test_fixed_mode_rejects_terminal_value(self) -> None:
        """A fixed cycle returns to its seed; there is nothing to value."""
        s = spec()
        h = horizon([0.1, 0.2], [1.0, 1.0])
        with pytest.raises(ValueError, match="only meaningful for terminal_mode='free'"):
            solve_oracle(s, h, terminal_value_eur_per_kwh=1.0)

    def test_free_oracle_shares_causal_floor(self) -> None:
        """The free oracle's SOC floor is max(soc_min, initial), same as causal."""
        s = spec(
            capacity_kwh=10.0,
            power_kw=1.0,
            charge_efficiency=1.0,
            discharge_efficiency=1.0,
            soc_min_frac=0.2,
            soc_max_frac=1.0,
            initial_soc_frac=0.5,
        )
        h = horizon([0.5, 0.5], [1.0, 1.0])
        free = solve_oracle(s, h, terminal_mode="free", terminal_value_eur_per_kwh=0.0)
        # the free oracle never discharges below initial SOC
        assert np.all(free.soc_kwh >= s.soc_bound_kwh("init") - 1e-9)
        # and the causal policy (neutral terminal) also respects this floor
        ca = policy_causal_price(s, h, charge_threshold=0.1, discharge_threshold=0.9)
        assert np.all(ca.soc_kwh >= s.soc_bound_kwh("init") - 1e-9)

    def test_free_oracle_dominates_on_horizons_with_ceiling_breaches(self) -> None:
        """Ceiling breaches are measured load, so the common feasible set survives.

        A high-load horizon that exceeds the ceiling in some intervals is the
        case the old net-import ceiling declared infeasible.  With the headroom
        form every policy still has a plan: the baseline is the measurement, the
        causal policy charges only where headroom exists, and the free oracle —
        whose feasible set contains both — stays at or below each of them.
        """
        rng = np.random.default_rng(1234)
        n = 72
        prices = rng.uniform(0.02, 0.42, n)
        # 200 W–450 W baseload plus a few 6.9 kW-minute-equivalent spikes
        load = rng.uniform(0.05, 0.1125, n)
        spikes = rng.choice(n, size=6, replace=False)
        load[spikes] = 1.725  # 6.9 kW over a 15-minute interval, above the ceiling
        s = spec(
            capacity_kwh=5.12,
            power_kw=2.5,
            charge_efficiency=0.95,
            discharge_efficiency=0.95,
            soc_min_frac=0.05,
            soc_max_frac=0.95,
            initial_soc_frac=0.05,
        )
        h = horizon(prices, load, interval_seconds=QUARTER, import_limit_kw=5.0)
        ceiling = h.interval_import_limit_kwh
        assert ceiling == pytest.approx(1.25)
        nb = policy_no_battery(s, h)
        ct, dt = fit_thresholds(prices, charge_pct=25.0, discharge_pct=75.0)
        ca = policy_causal_price(s, h, charge_threshold=ct, discharge_threshold=dt)
        free = solve_oracle(s, h, terminal_mode="free", terminal_value_eur_per_kwh=0.0)
        # the breaches are real and shared: the baseline is measured history
        assert nb.ceiling_exceeding_intervals == len(spikes)
        assert np.all(nb.grid_kwh[spikes] > ceiling)
        # no ceiling-exceeding interval charges
        assert np.all(ca.charge_kwh[spikes] == 0.0)
        assert np.all(free.charge_kwh[spikes] == 0.0)
        # dominance still holds on this horizon
        assert free.cost_eur <= ca.cost_eur + 1e-9
        assert free.cost_eur <= nb.cost_eur + 1e-9
        assert free.solver_status == "optimal"
        for sched in (nb, ca, free):
            assert (
                check_feasible(
                    s, h, sched.charge_kwh, sched.discharge_kwh, enforce_terminal_soc=False
                )
                == []
            )


# ---------------------------------------------------------------------------
# New Schedule fields — bounds and inventory accounting
# ---------------------------------------------------------------------------


class TestScheduleBoundFields:
    def test_fixed_oracle_populates_bounds(self) -> None:
        """A certified fixed-mode oracle has cost_lower == cost_upper == cost_eur."""
        s = spec()
        h = horizon([1.0, 3.0], [1.0, 1.0])
        sched = solve_oracle(s, h)
        assert sched.cost_lower_bound_eur is not None
        assert sched.cost_upper_bound_eur is not None
        assert sched.cost_lower_bound_eur == pytest.approx(sched.cost_eur, abs=1e-6)
        assert sched.cost_upper_bound_eur == pytest.approx(sched.cost_eur, abs=1e-6)
        assert sched.solver_time_limit_s == pytest.approx(60.0)

    def test_free_oracle_populates_inventory_fields(self) -> None:
        s = spec(initial_soc_frac=0.5)
        h = horizon([0.1, 0.5], [1.0, 1.0])
        sched = solve_oracle(s, h, terminal_mode="free", terminal_value_eur_per_kwh=1.0)
        assert sched.initial_soc_kwh == pytest.approx(5.0)
        assert sched.terminal_soc_kwh >= 5.0 - 1e-9
        assert sched.terminal_value_eur_per_kwh == pytest.approx(1.0)
        assert sched.meter_cost_eur is not None
        assert sched.inventory_adjustment_eur == pytest.approx(
            -1.0 * (sched.terminal_soc_kwh - sched.initial_soc_kwh)
        )
        assert sched.cost_eur == pytest.approx(
            sched.meter_cost_eur + sched.inventory_adjustment_eur
        )

    def test_free_certified_lower_bound_includes_terminal_offset(self) -> None:
        """A certified free run's ``cost_lower_bound_eur`` collapses onto
        ``cost_eur`` — the dual bound is shifted by the constants the objective
        dropped (the load term and the ``terminal_value * initial_SOC`` offset),
        so the reported lower bound is on the *total* objective, not the bare
        dispatch cost."""
        s = spec(
            capacity_kwh=10.0,
            power_kw=1.0,
            charge_efficiency=1.0,
            discharge_efficiency=1.0,
            initial_soc_frac=0.0,
        )
        h = horizon([0.1, 0.5], [1.0, 1.0])
        sched = solve_oracle(s, h, terminal_mode="free", terminal_value_eur_per_kwh=0.3)
        assert sched.is_certified is True
        # total objective == meter + inventory, and both bounds collapse on it
        assert sched.cost_eur == pytest.approx(
            sched.meter_cost_eur + sched.inventory_adjustment_eur, abs=1e-9
        )
        assert sched.cost_lower_bound_eur == pytest.approx(sched.cost_eur, abs=1e-6)
        assert sched.cost_upper_bound_eur == pytest.approx(sched.cost_eur, abs=1e-9)

    def test_time_limited_oracle_reports_bounds(self) -> None:
        """A time-limited incumbent has cost_lower <= cost_upper, both finite."""
        rng = np.random.default_rng(901)
        n = 96 * 4
        prices = rng.uniform(0.0, 0.4, n)
        load = rng.uniform(0.2, 1.5, n)
        s = spec(
            capacity_kwh=5.12,
            power_kw=2.5,
            charge_efficiency=0.95,
            discharge_efficiency=0.95,
            initial_soc_frac=0.2,
        )
        h = horizon(prices, load, interval_seconds=QUARTER)
        try:
            sched = solve_oracle(s, h, time_limit_s=0.01)
        except OracleNoIncumbentError:
            pytest.skip("solver found no incumbent inside 10 ms on this machine")
        assert sched.cost_lower_bound_eur is not None
        assert sched.cost_upper_bound_eur is not None
        assert sched.cost_lower_bound_eur <= sched.cost_upper_bound_eur + 1e-9
        assert sched.cost_upper_bound_eur == pytest.approx(sched.cost_eur, abs=1e-9)


# ---------------------------------------------------------------------------
# Discharge is unconstrained by the ceiling, so peak shaving stays available
# ---------------------------------------------------------------------------


class TestPeakShavingStillAvailable:
    """The ceiling limits charging, so the oracle can still *discharge* into a
    household peak above the limit — discharging lowers the draw, which is the
    physically correct response to a tight ceiling.

    The difference from the old net-grid formulation is what happens to the
    baseline: a peak the load alone exceeds is measured history, so the
    no-battery policy reports it (an observation) instead of raising, and the
    ceiling is met by the oracle only because prices make it worth meeting.
    """

    def test_oracle_discharges_into_a_ceiling_the_baseline_breaches(self) -> None:
        # 0.8 kW ceiling over 1 h = 0.8 kWh of charging headroom per interval.
        # t0 cheap / zero load (headroom 0.8), t1 a 1.5 kWh peak ABOVE ceiling.
        s = spec(
            capacity_kwh=10.0,
            power_kw=1.0,
            charge_efficiency=1.0,
            discharge_efficiency=1.0,
            soc_min_frac=0.0,
            soc_max_frac=1.0,
            initial_soc_frac=0.5,  # 5 kWh on hand; fixed cycle must return here
        )
        h = horizon([0.1, 0.5], [0.0, 1.5], interval_seconds=HOUR, import_limit_kw=0.8)
        # the no-battery baseline pays a 1.5 kWh peak > 0.8 ceiling: it does not
        # raise, it records the breach
        nb = policy_no_battery(s, h)
        assert np.allclose(nb.grid_kwh, h.load_kwh)
        assert nb.ceiling_exceeding_intervals == 1

        orc = solve_oracle(s, h)
        ceiling = h.interval_import_limit_kwh
        assert ceiling == pytest.approx(0.8)
        # charging is capped by headroom (0.8 at t0, none at t1 where load tops it)
        assert np.all(orc.charge_kwh <= np.maximum(ceiling - h.load_kwh, 0.0) + 1e-9)
        assert orc.charge_kwh[1] == pytest.approx(0.0)
        # net grid import only exceeds the ceiling because the load alone does
        assert np.all(orc.grid_kwh <= np.maximum(h.load_kwh, ceiling) + 1e-6)
        # it shaves the peak by discharging into it
        assert orc.discharge_kwh[1] > 0.0
        assert orc.discharge_kwh[1] == pytest.approx(0.8, abs=1e-6)  # shave 1.5 -> 0.7 net
        # charged the same energy back on the cheap interval, returns to the seed
        assert orc.charge_kwh[0] == pytest.approx(orc.discharge_kwh[1], abs=1e-6)
        assert orc.soc_kwh[-1] == pytest.approx(s.soc_bound_kwh("init"), abs=1e-6)
        assert check_feasible(s, h, orc.charge_kwh, orc.discharge_kwh) == []
        assert orc.solver_status == "optimal" and orc.is_certified
        # and the oracle beats a baseline that has no battery to shave with
        assert orc.cost_eur <= nb.cost_eur + 1e-9
        assert orc.ceiling_exceeding_intervals == 0
