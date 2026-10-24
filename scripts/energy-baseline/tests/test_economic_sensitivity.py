"""Tests for economic sensitivity — throughput cost and terminal value sweeps.

Pins the analytic optima of small hand-computable horizons under throughput
cost, the zero-penalty legacy identity, high-penalty throughput suppression,
parameter validation, the meter-bill vs degradation-adjusted identity, and
consistent terminal credit across compared schedules.

All monetary constants in these tests are synthetic fixture values, not
household figures.  No real degradation rates are invented or assumed; every
range is labelled user-supplied hypothetical."""

from __future__ import annotations

import numpy as np
import pytest

from energy_baseline.battery import BatterySpec, Horizon
from energy_baseline.economic_sensitivity import (
    break_even_throughput_cost,
    compare_schedules,
    optimize_with_throughput,
    score_fixed_schedule,
    sweep_sensitivity,
)
from energy_baseline.oracle import solve_oracle
from energy_baseline.policies import policy_no_battery

HOUR = 3600.0
QUARTER = 900.0


def _spec(**overrides) -> BatterySpec:
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


def _horizon(prices, load, usable=None, interval_seconds=HOUR, import_limit_kw=None) -> Horizon:
    n = len(prices)
    return Horizon(
        interval_seconds,
        prices,
        load,
        [True] * n if usable is None else usable,
        import_limit_kw,
    )


# ---------------------------------------------------------------------------
# Zero penalty == legacy
# ---------------------------------------------------------------------------


class TestZeroPenaltyIsLegacy:
    """With tp=0 the MILP is byte-identical to the historical model."""

    def test_solve_oracle_zero_throughput_cost_matches_legacy(self) -> None:
        s = _spec()
        h = _horizon([1.0, 3.0], [1.0, 1.0])
        legacy = solve_oracle(s, h)
        with_tp = solve_oracle(s, h, throughput_cost_eur_per_kwh=0.0)
        assert with_tp.cost_eur == pytest.approx(legacy.cost_eur)
        assert np.array_equal(with_tp.charge_kwh, legacy.charge_kwh)
        assert np.array_equal(with_tp.discharge_kwh, legacy.discharge_kwh)
        assert np.array_equal(with_tp.soc_kwh, legacy.soc_kwh)

    def test_score_fixed_schedule_zero_penalty_identity(self) -> None:
        """With tp=0 and V=0 the adjusted objective equals the meter bill."""
        s = _spec()
        h = _horizon([0.1, 0.4], [1.0, 1.0])
        sched = solve_oracle(s, h)
        scored = score_fixed_schedule(sched, 0.0, 0.0)
        assert scored["meter_cost_eur"] == pytest.approx(sched.meter_cost_eur)
        assert scored["throughput_penalty_eur"] == pytest.approx(0.0)
        assert scored["inventory_adjustment_eur"] == pytest.approx(0.0)
        assert scored["degradation_adjusted_objective_eur"] == pytest.approx(sched.meter_cost_eur)

    def test_build_model_zero_throughput_cost_is_unchanged(self) -> None:
        """build_model with tp=0 produces the same objective as without."""
        from energy_baseline.oracle import build_model

        s = _spec()
        h = _horizon([0.1, 0.3, 0.2], [1.0, 1.0, 1.0])
        obj_plain, _, _, _ = build_model(s, h)
        obj_tp0, _, _, _ = build_model(s, h, throughput_cost_eur_per_kwh=0.0)
        assert np.array_equal(obj_plain, obj_tp0)


# ---------------------------------------------------------------------------
# Analytic optima under throughput cost
# ---------------------------------------------------------------------------


class TestAnalyticOptimaWithThroughputCost:
    """Hand-computable optima for small horizons under throughput cost."""

    def test_one_slot_no_arbitrage_possible(self) -> None:
        """One interval: no charge/discharge pair exists, so tp changes nothing."""
        s = _spec()
        h = _horizon([0.1], [1.0])
        sched_no_tp = solve_oracle(s, h, throughput_cost_eur_per_kwh=0.0)
        sched_tp = solve_oracle(s, h, throughput_cost_eur_per_kwh=0.5)
        # one interval, no arbitrage: both idle
        assert sched_tp.charge_kwh[0] == pytest.approx(0.0)
        assert sched_tp.discharge_kwh[0] == pytest.approx(0.0)
        assert sched_tp.cost_eur == pytest.approx(sched_no_tp.cost_eur)

    def test_two_slot_lossless_break_even_throughput_cost(self) -> None:
        """Prices [0.10, 0.40], lossless: break-even tp = (0.40 - 0.10) / 2 = 0.15.

        With tp < 0.15 the oracle arbitrages (charge at t0, discharge at t1).
        With tp > 0.15 the round-trip cost exceeds the price spread and the
        oracle idles.  At tp = 0.15 exactly the oracle is indifferent.
        """
        s = _spec()
        h = _horizon([0.10, 0.40], [1.0, 1.0])

        # Below break-even: arbitrage
        sched_low = solve_oracle(s, h, throughput_cost_eur_per_kwh=0.10)
        assert sched_low.charge_kwh[0] > 0.1  # charges at the cheap slot
        assert sched_low.discharge_kwh[1] > 0.1  # discharges at the expensive

        # Above break-even: no arbitrage
        sched_high = solve_oracle(s, h, throughput_cost_eur_per_kwh=0.20)
        assert sched_high.charge_kwh[0] == pytest.approx(0.0, abs=1e-9)
        assert sched_high.discharge_kwh[1] == pytest.approx(0.0, abs=1e-9)

    def test_two_slot_lossy_break_even_throughput_cost(self) -> None:
        """Prices [0.10, 0.40], eta=0.9 each way.

        Effective charge cost = 0.10 + tp, effective discharge benefit =
        (0.40 - tp) * 0.9 * 0.9 = (0.40 - tp) * 0.81.
        Break-even: 0.10 + tp = (0.40 - tp) * 0.81
        => tp * (1 + 0.81) = 0.40 * 0.81 - 0.10 = 0.224
        => tp = 0.224 / 1.81 ≈ 0.1238.
        """
        s = _spec(charge_efficiency=0.9, discharge_efficiency=0.9)
        h = _horizon([0.10, 0.40], [1.0, 1.0])

        tp_break_even = (0.40 * 0.81 - 0.10) / (1.0 + 0.81)

        # Just below: still arbitrages
        sched_below = solve_oracle(s, h, throughput_cost_eur_per_kwh=tp_break_even - 0.01)
        assert sched_below.charge_kwh[0] > 0.1

        # Just above: no arbitrage
        sched_above = solve_oracle(s, h, throughput_cost_eur_per_kwh=tp_break_even + 0.01)
        assert sched_above.charge_kwh[0] == pytest.approx(0.0, abs=1e-9)
        assert sched_above.discharge_kwh[1] == pytest.approx(0.0, abs=1e-9)

    def test_throughput_cost_penalises_both_charge_and_discharge(self) -> None:
        """The penalty is tp * (charge + discharge), applied to both legs.

        With prices [0.05, 0.50] and tp = 0.10:
        effective charge price at t0 = 0.05 + 0.10 = 0.15
        effective discharge price at t1 = 0.50 - 0.10 = 0.40
        spread = 0.25 > 0, so arbitrage still happens but is less profitable.
        """
        s = _spec()
        h = _horizon([0.05, 0.50], [1.0, 1.0])
        sched_no_tp = solve_oracle(s, h, throughput_cost_eur_per_kwh=0.0)
        sched_tp = solve_oracle(s, h, throughput_cost_eur_per_kwh=0.10)
        # Both arbitrage, but the tp case moves less energy
        assert sched_tp.charge_kwh[0] <= sched_no_tp.charge_kwh[0] + 1e-9
        # The meter cost is higher with tp because the solver charges less
        # (less arbitrage = less savings)
        assert sched_tp.meter_cost_eur >= sched_no_tp.meter_cost_eur - 1e-9


# ---------------------------------------------------------------------------
# High penalty discourages throughput
# ---------------------------------------------------------------------------


class TestHighPenaltyDiscouragesThroughput:
    """A very high throughput cost should drive throughput to zero."""

    def test_very_high_throughput_cost_idles_the_battery(self) -> None:
        rng = np.random.default_rng(42)
        n = 24
        prices = rng.uniform(0.01, 0.50, n)
        load = rng.uniform(0.2, 1.5, n)
        s = _spec(
            capacity_kwh=5.12,
            power_kw=2.5,
            charge_efficiency=0.95,
            discharge_efficiency=0.95,
            initial_soc_frac=0.5,
        )
        h = _horizon(prices, load, interval_seconds=QUARTER)

        sched = solve_oracle(s, h, throughput_cost_eur_per_kwh=100.0)
        total_ac_throughput = float(sched.charge_kwh.sum() + sched.discharge_kwh.sum())
        assert total_ac_throughput == pytest.approx(0.0, abs=1e-6)
        assert sched.charge_kwh.sum() == pytest.approx(0.0, abs=1e-6)
        # cost equals the no-battery baseline
        nb = policy_no_battery(s, h)
        assert sched.cost_eur == pytest.approx(nb.cost_eur, abs=1e-6)

    def test_monotonic_throughput_decrease(self) -> None:
        """Throughput is non-increasing as tp rises."""
        rng = np.random.default_rng(43)
        n = 48
        prices = rng.uniform(0.01, 0.50, n)
        load = rng.uniform(0.2, 1.5, n)
        s = _spec(
            capacity_kwh=5.12,
            power_kw=2.5,
            charge_efficiency=0.95,
            discharge_efficiency=0.95,
            initial_soc_frac=0.2,
        )
        h = _horizon(prices, load, interval_seconds=QUARTER)

        throughputs = []
        for tp in [0.0, 0.01, 0.02, 0.05, 0.10, 0.20, 1.0]:
            sched = solve_oracle(s, h, throughput_cost_eur_per_kwh=tp)
            total_ac_throughput = float(sched.charge_kwh.sum() + sched.discharge_kwh.sum())
            throughputs.append(total_ac_throughput)

        for i in range(len(throughputs) - 1):
            assert throughputs[i] >= throughputs[i + 1] - 1e-9


# ---------------------------------------------------------------------------
# Invalid parameters
# ---------------------------------------------------------------------------


class TestInvalidParameters:
    def test_negative_throughput_cost_rejected_by_solve_oracle(self) -> None:
        s = _spec()
        h = _horizon([0.1, 0.2], [1.0, 1.0])
        with pytest.raises(ValueError, match=">= 0"):
            solve_oracle(s, h, throughput_cost_eur_per_kwh=-0.01)

    def test_nan_throughput_cost_rejected_by_solve_oracle(self) -> None:
        s = _spec()
        h = _horizon([0.1, 0.2], [1.0, 1.0])
        with pytest.raises(ValueError, match="finite"):
            solve_oracle(s, h, throughput_cost_eur_per_kwh=float("nan"))

    def test_inf_throughput_cost_rejected_by_solve_oracle(self) -> None:
        s = _spec()
        h = _horizon([0.1, 0.2], [1.0, 1.0])
        with pytest.raises(ValueError, match="finite"):
            solve_oracle(s, h, throughput_cost_eur_per_kwh=float("inf"))

    def test_negative_throughput_cost_rejected_by_score(self) -> None:
        s = _spec()
        h = _horizon([0.1, 0.2], [1.0, 1.0])
        sched = solve_oracle(s, h)
        with pytest.raises(ValueError, match=">= 0"):
            score_fixed_schedule(sched, -0.01)

    def test_nan_terminal_value_rejected_by_score(self) -> None:
        s = _spec()
        h = _horizon([0.1, 0.2], [1.0, 1.0])
        sched = solve_oracle(s, h)
        with pytest.raises(ValueError, match="finite"):
            score_fixed_schedule(sched, 0.0, float("nan"))

    def test_build_model_rejects_negative_throughput_cost(self) -> None:
        from energy_baseline.oracle import build_model

        s = _spec()
        h = _horizon([0.1, 0.2], [1.0, 1.0])
        with pytest.raises(ValueError, match=">= 0"):
            build_model(s, h, throughput_cost_eur_per_kwh=-1.0)

    def test_sweep_rejects_non_finite_planning_terminal_values(self) -> None:
        s = _spec()
        h = _horizon([0.1, 0.2], [1.0, 1.0])
        with pytest.raises(ValueError, match="non-finite"):
            sweep_sensitivity(
                s, h, planning_terminal_values=[0.0, float("inf")], throughput_costs=[0.0]
            )

    def test_sweep_rejects_negative_throughput_costs(self) -> None:
        s = _spec()
        h = _horizon([0.1, 0.2], [1.0, 1.0])
        with pytest.raises(ValueError, match=">= 0"):
            sweep_sensitivity(s, h, planning_terminal_values=[0.0], throughput_costs=[-0.01])

    def test_solve_oracle_rejects_bool_throughput_cost(self) -> None:
        """A bool throughput cost is a programming error, not a value.

        Python's ``bool`` is an ``int`` subclass, so ``True`` would otherwise
        pass ``float(True) == 1.0`` validation.
        """
        s = _spec()
        h = _horizon([0.1, 0.2], [1.0, 1.0])
        with pytest.raises(ValueError, match="bool"):
            solve_oracle(s, h, throughput_cost_eur_per_kwh=True)  # type: ignore[arg-type]
        with pytest.raises(ValueError, match="bool"):
            solve_oracle(s, h, throughput_cost_eur_per_kwh=False)  # type: ignore[arg-type]

    def test_score_fixed_schedule_rejects_bool_throughput_cost(self) -> None:
        s = _spec()
        h = _horizon([0.1, 0.2], [1.0, 1.0])
        sched = solve_oracle(s, h)
        with pytest.raises(ValueError, match="bool"):
            score_fixed_schedule(sched, True)  # type: ignore[arg-type]

    def test_score_fixed_schedule_rejects_bool_terminal_value(self) -> None:
        s = _spec()
        h = _horizon([0.1, 0.2], [1.0, 1.0])
        sched = solve_oracle(s, h)
        with pytest.raises(ValueError, match="bool"):
            score_fixed_schedule(sched, 0.0, True)  # type: ignore[arg-type]

    def test_break_even_rejects_bool_terminal_value(self) -> None:
        s = _spec()
        h = _horizon([0.1, 0.2], [1.0, 1.0])
        sched = solve_oracle(s, h)
        with pytest.raises(ValueError, match="bool"):
            break_even_throughput_cost(
                sched,
                sched,
                terminal_value_eur_per_kwh=True,  # type: ignore[arg-type]
            )


# ---------------------------------------------------------------------------
# Meter bill vs degradation-adjusted identity
# ---------------------------------------------------------------------------


class TestMeterBillIdentity:
    """The degradation-adjusted objective = meter + inventory + throughput."""

    def test_explicit_identity(self) -> None:
        s = _spec(initial_soc_frac=0.5)
        h = _horizon([0.1, 0.4], [1.0, 1.0])
        sched = solve_oracle(
            s,
            h,
            throughput_cost_eur_per_kwh=0.05,
            terminal_mode="free",
            terminal_value_eur_per_kwh=0.15,
        )
        scored = score_fixed_schedule(sched, 0.05, 0.15)
        expected = (
            scored["meter_cost_eur"]
            + scored["inventory_adjustment_eur"]
            + scored["throughput_penalty_eur"]
        )
        assert scored["degradation_adjusted_objective_eur"] == pytest.approx(expected)

    def test_meter_cost_unchanged_by_throughput_cost_rescoring(self) -> None:
        """Rescoring the same schedule at different tp does not change the meter bill."""
        s = _spec()
        h = _horizon([0.1, 0.4], [1.0, 1.0])
        sched = solve_oracle(s, h)
        scored_0 = score_fixed_schedule(sched, 0.0)
        scored_1 = score_fixed_schedule(sched, 0.05)
        scored_2 = score_fixed_schedule(sched, 0.10)
        assert scored_0["meter_cost_eur"] == pytest.approx(scored_1["meter_cost_eur"])
        assert scored_1["meter_cost_eur"] == pytest.approx(scored_2["meter_cost_eur"])

    def test_throughput_penalty_scales_linearly(self) -> None:
        s = _spec()
        h = _horizon([0.1, 0.4], [1.0, 1.0])
        sched = solve_oracle(s, h)
        total_ac_throughput = float(sched.charge_kwh.sum() + sched.discharge_kwh.sum())
        for tp in [0.01, 0.05, 0.10, 0.50]:
            scored = score_fixed_schedule(sched, tp)
            assert scored["throughput_penalty_eur"] == pytest.approx(tp * total_ac_throughput)


# ---------------------------------------------------------------------------
# Consistent terminal credit across compared schedules
# ---------------------------------------------------------------------------


class TestConsistentTerminalCredit:
    """When comparing schedules, the terminal credit uses the same V for all."""

    def test_compare_schedules_uses_same_terminal_value(self) -> None:
        s = _spec(initial_soc_frac=0.5)
        h = _horizon([0.1, 0.4, 0.2, 0.3], [1.0, 1.0, 1.0, 1.0])
        sched_oracle = solve_oracle(s, h, terminal_mode="free", terminal_value_eur_per_kwh=0.0)
        sched_nb = policy_no_battery(s, h)

        compared = compare_schedules(
            {"oracle": sched_oracle, "no_battery": sched_nb},
            throughput_cost_eur_per_kwh=0.02,
            terminal_value_eur_per_kwh=0.10,
        )
        # Both use the same terminal value
        assert compared["oracle"]["inventory_adjustment_eur"] == pytest.approx(
            -0.10 * (compared["oracle"]["end_soc_kwh"] - compared["oracle"]["initial_soc_kwh"])
        )
        assert compared["no_battery"]["inventory_adjustment_eur"] == pytest.approx(
            -0.10
            * (compared["no_battery"]["end_soc_kwh"] - compared["no_battery"]["initial_soc_kwh"])
        )

    def test_compare_schedules_same_throughput_cost(self) -> None:
        s = _spec()
        h = _horizon([0.1, 0.4], [1.0, 1.0])
        sched_a = solve_oracle(s, h)
        sched_b = policy_no_battery(s, h)

        compared = compare_schedules(
            {"a": sched_a, "b": sched_b},
            throughput_cost_eur_per_kwh=0.05,
        )
        tp = 0.05
        for name, sched in [("a", sched_a), ("b", sched_b)]:
            total_ac_throughput = float(sched.charge_kwh.sum() + sched.discharge_kwh.sum())
            assert compared[name]["throughput_penalty_eur"] == pytest.approx(
                tp * total_ac_throughput
            )


# ---------------------------------------------------------------------------
# No eval-derived default
# ---------------------------------------------------------------------------


class TestNoEvalDerivedDefault:
    """The module never invents a default terminal value or throughput cost
    from the data.  Both must be supplied explicitly."""

    def test_score_requires_explicit_throughput_cost(self) -> None:
        """score_fixed_schedule has no default for throughput_cost — it must be passed."""
        s = _spec()
        h = _horizon([0.1, 0.2], [1.0, 1.0])
        sched = solve_oracle(s, h)
        # The function signature requires throughput_cost_eur_per_kwh as a
        # positional argument (no default), so calling without it is a TypeError.
        with pytest.raises(TypeError):
            score_fixed_schedule(sched)  # type: ignore[call-arg]

    def test_sweep_requires_explicit_arrays(self) -> None:
        """sweep_sensitivity requires both planning_terminal_values and throughput_costs."""
        s = _spec()
        h = _horizon([0.1, 0.2], [1.0, 1.0])
        with pytest.raises(TypeError):
            sweep_sensitivity(s, h)  # type: ignore[call-arg]

    def test_optimize_requires_explicit_throughput_cost(self) -> None:
        """optimize_with_throughput requires throughput_cost as a positional arg."""
        s = _spec()
        h = _horizon([0.1, 0.2], [1.0, 1.0])
        with pytest.raises(TypeError):
            optimize_with_throughput(s, h)  # type: ignore[call-arg]


# ---------------------------------------------------------------------------
# Sweep returns correct structure
# ---------------------------------------------------------------------------


class TestSweepStructure:
    def test_sweep_returns_one_row_per_combination(self) -> None:
        s = _spec()
        h = _horizon([0.1, 0.4], [1.0, 1.0])
        results = sweep_sensitivity(
            s,
            h,
            planning_terminal_values=[0.0, 0.1],
            throughput_costs=[0.0, 0.05],
        )
        assert len(results) == 4  # 2 x 2

    def test_sweep_records_all_fields(self) -> None:
        s = _spec()
        h = _horizon([0.1, 0.4], [1.0, 1.0])
        results = sweep_sensitivity(
            s,
            h,
            planning_terminal_values=[0.0],
            throughput_costs=[0.0],
        )
        row = results[0]
        expected_keys = {
            "planning_terminal_value_eur_per_kwh",
            "throughput_cost_eur_per_kwh",
            "scoring_terminal_value_eur_per_kwh",
            "meter_cost_eur",
            "total_ac_throughput_kwh",
            "efc",
            "end_soc_kwh",
            "initial_soc_kwh",
            "planning_inventory_adjustment_eur",
            "scoring_inventory_adjustment_eur",
            "throughput_penalty_eur",
            "degradation_adjusted_objective_eur",
            "solver_status",
            "is_certified",
        }
        assert expected_keys.issubset(set(row.keys()))

    def test_sweep_reoptimizes_per_pair(self) -> None:
        """Different (planning_terminal, throughput_cost) pairs produce different schedules."""
        s = _spec()
        h = _horizon([0.10, 0.40], [1.0, 1.0])
        results = sweep_sensitivity(
            s,
            h,
            planning_terminal_values=[0.0, 0.50],
            throughput_costs=[0.0],
            terminal_mode="free",
        )
        # With planning_terminal=0.0 and tp=0.0: arbitrages 1 kWh, ends at initial SOC (0)
        # With planning_terminal=0.50 and tp=0.0: hoards (charge cost 0.10, terminal
        # value 0.50 makes storing profitable), ends at SOC 1.0
        ptv0 = [r for r in results if r["planning_terminal_value_eur_per_kwh"] == 0.0][0]
        ptv50 = [r for r in results if r["planning_terminal_value_eur_per_kwh"] == 0.50][0]
        # Different planning terminal values produce different end SOCs
        assert ptv0["end_soc_kwh"] != pytest.approx(ptv50["end_soc_kwh"], abs=1e-6)
        assert ptv50["end_soc_kwh"] > ptv0["end_soc_kwh"]

    def test_sweep_different_terminal_values_produce_distinct_end_soc(self) -> None:
        """Different planning terminal values produce different end SOCs."""
        s = _spec(initial_soc_frac=0.0)
        h = _horizon([0.10, 0.40], [1.0, 1.0])
        results = sweep_sensitivity(
            s,
            h,
            planning_terminal_values=[0.0, 0.50],
            throughput_costs=[0.0],
            terminal_mode="free",
        )
        ptv0 = [r for r in results if r["planning_terminal_value_eur_per_kwh"] == 0.0][0]
        ptv50 = [r for r in results if r["planning_terminal_value_eur_per_kwh"] == 0.50][0]
        # With planning_terminal=0.0: arbitrages, ends at initial SOC (0.0)
        # With planning_terminal=0.50: keeps charge, ends higher
        assert ptv0["end_soc_kwh"] < ptv50["end_soc_kwh"]


# ---------------------------------------------------------------------------
# Dual bound unavailability with throughput cost
# ---------------------------------------------------------------------------


class TestDualBoundUnavailability:
    """When throughput_cost > 0, the dual bound is for the penalized objective,
    not the reported meter-cost objective.  Bounds must be marked unavailable."""

    def test_zero_throughput_cost_preserves_certification(self) -> None:
        """With tp=0, the dual bound is valid and certification works."""
        s = _spec()
        h = _horizon([1.0, 3.0], [1.0, 1.0])
        sched = solve_oracle(s, h, throughput_cost_eur_per_kwh=0.0)
        assert sched.is_certified is True
        assert sched.solver_status == "optimal"
        assert sched.cost_lower_bound_eur is not None
        assert sched.cost_upper_bound_eur is not None

    def test_nonzero_throughput_cost_keeps_upper_drops_lower(self) -> None:
        """With tp>0, the upper bound is the incumbent's cost; lower is None.

        The solver's dual bound lives on the penalized objective, not the
        reported meter-cost objective; it cannot be shifted to the unpenalized
        basis.  The incumbent's unpenalized cost IS a valid (loose) upper bound
        because the constraint set is unchanged by tp.
        """
        s = _spec()
        h = _horizon([1.0, 3.0], [1.0, 1.0])
        sched = solve_oracle(s, h, throughput_cost_eur_per_kwh=0.05)
        assert sched.is_certified is False
        assert sched.solver_status == "feasible"
        assert sched.bound_kind == "upper"
        assert sched.solver_gap is None
        assert sched.cost_lower_bound_eur is None
        assert sched.cost_upper_bound_eur is not None
        assert sched.cost_upper_bound_eur == pytest.approx(sched.cost_eur)

    def test_no_inverted_bounds_with_throughput_cost(self) -> None:
        """With tp>0, lower is None and upper is the incumbent's cost."""
        s = _spec()
        h = _horizon([0.1, 0.4], [1.0, 1.0])
        sched = solve_oracle(s, h, throughput_cost_eur_per_kwh=0.10)
        assert sched.cost_lower_bound_eur is None
        assert sched.cost_upper_bound_eur is not None
        assert sched.cost_upper_bound_eur == pytest.approx(sched.cost_eur)

    def test_no_false_certificate_with_throughput_cost(self) -> None:
        """With tp>0, is_certified is always False."""
        s = _spec()
        h = _horizon([1.0, 3.0], [1.0, 1.0])
        sched = solve_oracle(s, h, throughput_cost_eur_per_kwh=0.01)
        assert sched.is_certified is False


# ---------------------------------------------------------------------------
# Break-even throughput cost
# ---------------------------------------------------------------------------


class TestBreakEvenThroughputCost:
    """Compute the throughput cost at which two schedules have equal cost."""

    def test_break_even_identical_throughput_returns_none(self) -> None:
        """If two schedules have identical throughput, break-even is undefined."""
        s = _spec()
        h = _horizon([0.1, 0.2], [1.0, 1.0])
        sched_a = solve_oracle(s, h)
        sched_b = solve_oracle(s, h)  # identical
        be = break_even_throughput_cost(sched_a, sched_b)
        assert be is None

    def test_break_even_computes_intersection(self) -> None:
        """Break-even is where meter_a + tp*T_a = meter_b + tp*T_b."""
        s = _spec()
        h = _horizon([0.10, 0.40], [1.0, 1.0])
        sched_no_tp = solve_oracle(s, h, throughput_cost_eur_per_kwh=0.0)
        sched_high_tp = solve_oracle(s, h, throughput_cost_eur_per_kwh=0.20)

        be = break_even_throughput_cost(sched_no_tp, sched_high_tp)
        assert be is not None
        # At the break-even tp, both schedules have the same degradation-adjusted cost
        scored_no_tp = score_fixed_schedule(sched_no_tp, be)
        scored_high_tp = score_fixed_schedule(sched_high_tp, be)
        assert scored_no_tp["degradation_adjusted_objective_eur"] == pytest.approx(
            scored_high_tp["degradation_adjusted_objective_eur"], abs=1e-6
        )

    def test_break_even_with_terminal_value(self) -> None:
        """Break-even accounts for terminal inventory valuation.

        Two schedules on prices [0.05, 0.05], lossless 1 kW, 1 kWh load each:

        * sched_a (ptv=0): idle.  M_a=0.10, T_a=0, S_a=0.
        * sched_b (ptv=0.50): hoard 1 kWh each interval.  M_b=0.20, T_b=2, S_b=2.

        tp = (M_b - M_a - V*(S_b - S_a)) / (T_a - T_b)
           = (0.10 - 2*V) / (-2)
           = V - 0.05

        At V=0.10, tp=0.05.  The terminal-value term matters because the
        end SOCs differ.
        """
        s = _spec(initial_soc_frac=0.0)
        h = _horizon([0.05, 0.05], [1.0, 1.0])
        sched_a = solve_oracle(s, h, terminal_mode="free", terminal_value_eur_per_kwh=0.0)
        sched_b = solve_oracle(s, h, terminal_mode="free", terminal_value_eur_per_kwh=0.50)

        # Sanity: distinct end SOCs and distinct throughputs.
        assert sched_a.terminal_soc_kwh < sched_b.terminal_soc_kwh
        ta = float(sched_a.charge_kwh.sum() + sched_a.discharge_kwh.sum())
        tb = float(sched_b.charge_kwh.sum() + sched_b.discharge_kwh.sum())
        assert ta < tb

        v = 0.10
        be = break_even_throughput_cost(sched_a, sched_b, terminal_value_eur_per_kwh=v)
        assert be is not None
        # At the break-even tp, both schedules have the same degradation-adjusted cost.
        scored_a = score_fixed_schedule(sched_a, be, v)
        scored_b = score_fixed_schedule(sched_b, be, v)
        assert scored_a["degradation_adjusted_objective_eur"] == pytest.approx(
            scored_b["degradation_adjusted_objective_eur"], abs=1e-6
        )

    def test_pairwise_break_even_classifies_signs(self) -> None:
        """Classification distinguishes real crossovers from negative-axis ones."""
        from energy_baseline.economic_sensitivity import pairwise_break_even

        s = _spec(initial_soc_frac=0.0)
        h = _horizon([0.10, 0.40], [1.0, 1.0])
        sched_arbitrage = solve_oracle(
            s,
            h,
            throughput_cost_eur_per_kwh=0.0,
            terminal_mode="free",
            terminal_value_eur_per_kwh=0.0,
        )
        sched_idle = solve_oracle(
            s,
            h,
            throughput_cost_eur_per_kwh=0.50,
            terminal_mode="free",
            terminal_value_eur_per_kwh=0.0,
        )
        result = pairwise_break_even(sched_arbitrage, sched_idle)
        assert result["classification"] == "non_negative_crossover"
        assert result["break_even_throughput_cost_eur_per_kwh"] is not None
        assert result["break_even_throughput_cost_eur_per_kwh"] >= 0.0

    def test_pairwise_break_even_identical_throughput_parallel(self) -> None:
        """Identical throughput returns parallel classification with None tp."""
        from energy_baseline.economic_sensitivity import pairwise_break_even

        s = _spec()
        h = _horizon([0.1, 0.2], [1.0, 1.0])
        sched_a = solve_oracle(s, h)
        sched_b = solve_oracle(s, h)
        result = pairwise_break_even(sched_a, sched_b)
        assert result["classification"] == "no_crossover_parallel_lines"
        assert result["break_even_throughput_cost_eur_per_kwh"] is None

    def test_pairwise_break_even_exposes_components(self) -> None:
        """The numerator and denominator are reported separately, not just the ratio."""
        from energy_baseline.economic_sensitivity import pairwise_break_even

        s = _spec()
        h = _horizon([0.10, 0.40], [1.0, 1.0])
        sched_a = solve_oracle(s, h, throughput_cost_eur_per_kwh=0.0)
        sched_b = solve_oracle(s, h, throughput_cost_eur_per_kwh=0.30)
        r = pairwise_break_even(sched_a, sched_b)
        m_a = float(sched_a.meter_cost_eur)
        m_b = float(sched_b.meter_cost_eur)
        t_a = float(sched_a.charge_kwh.sum() + sched_a.discharge_kwh.sum())
        t_b = float(sched_b.charge_kwh.sum() + sched_b.discharge_kwh.sum())
        assert r["baseline_net_advantage_eur"] == pytest.approx(m_b - m_a)
        assert r["incremental_ac_throughput_kwh"] == pytest.approx(t_a - t_b)
        # The ratio reproduces the simpler break_even_throughput_cost.
        assert r["break_even_throughput_cost_eur_per_kwh"] == pytest.approx(
            break_even_throughput_cost(sched_a, sched_b)
        )


# ---------------------------------------------------------------------------
# Field naming and conventions
# ---------------------------------------------------------------------------


class TestFieldNaming:
    """Verify field names match the specification."""

    def test_total_ac_throughput_kwh_field(self) -> None:
        """The field is total_ac_throughput_kwh, not throughput_kwh."""
        s = _spec()
        h = _horizon([0.1, 0.4], [1.0, 1.0])
        sched = solve_oracle(s, h)
        scored = score_fixed_schedule(sched, 0.05)
        assert "total_ac_throughput_kwh" in scored
        assert "throughput_kwh" not in scored

    def test_efc_uses_schedule_convention(self) -> None:
        """EFC comes from the schedule, not recomputed."""
        s = _spec()
        h = _horizon([0.1, 0.4], [1.0, 1.0])
        sched = solve_oracle(s, h)
        scored = score_fixed_schedule(sched, 0.05)
        assert scored["efc"] == pytest.approx(sched.efc)


# ---------------------------------------------------------------------------
# BLOCKER #1 — break_even with unequal initial SOC
# ---------------------------------------------------------------------------


class TestBreakEvenUnequalInitialSOC:
    """The break-even formula must account for different initial SOCs.

    Empirical proof: A(I=0,S=1,M=0.95,T=3), B(I=6,S=5,M=0.55,T=1), V=0.2.
    Correct: [(0.55-0.95) + 0.2*((1-0)-(5-6))] / (3-1) = 0.0.
    Old buggy formula: [(0.55-0.95) - 0.2*(5-1)] / (3-1) = -0.60.
    """

    def test_unequal_initial_soc_produces_correct_break_even(self) -> None:
        """Hand-computable fixture with different initial SOCs."""
        from energy_baseline.battery import Schedule

        # Construct minimal schedules with the exact parameters from the proof.
        def _make_schedule(
            *,
            meter_cost: float,
            initial_soc: float,
            terminal_soc: float,
            charge: np.ndarray,
            discharge: np.ndarray,
        ) -> Schedule:
            n = len(charge)
            return Schedule(
                charge_kwh=charge,
                discharge_kwh=discharge,
                soc_kwh=np.full(n, terminal_soc),
                grid_kwh=np.zeros(n),
                cost_eur=meter_cost,
                efc=0.0,
                throughput_kwh=float(discharge.sum()),
                soc_min_frac=0.0,
                soc_max_frac=1.0,
                initial_soc_kwh=initial_soc,
                terminal_soc_kwh=terminal_soc,
                meter_cost_eur=meter_cost,
            )

        sched_a = _make_schedule(
            meter_cost=0.95,
            initial_soc=0.0,
            terminal_soc=1.0,
            charge=np.array([1.0, 1.0, 1.0]),
            discharge=np.array([0.0, 0.0, 0.0]),
        )
        sched_b = _make_schedule(
            meter_cost=0.55,
            initial_soc=6.0,
            terminal_soc=5.0,
            charge=np.array([0.0, 0.0, 0.0]),
            discharge=np.array([0.5, 0.5, 0.0]),
        )
        # T_a = 3+0 = 3, T_b = 0+1 = 1.
        # Correct: [(0.55-0.95) + 0.2*((1-0)-(5-6))] / (3-1) = 0.0
        be = break_even_throughput_cost(sched_a, sched_b, terminal_value_eur_per_kwh=0.2)
        assert be is not None
        assert be == pytest.approx(0.0, abs=1e-9)


# ---------------------------------------------------------------------------
# BLOCKER #2 — sweep_sensitivity fixed-mode degeneracy
# ---------------------------------------------------------------------------


class TestSweepFixedModeDegeneracy:
    """In fixed mode, planning_terminal_values has no effect on the schedule."""

    def test_fixed_mode_with_multiple_planning_values_raises(self) -> None:
        """Sweeping multiple planning values in fixed mode is a no-op; refuse it."""
        s = _spec()
        h = _horizon([0.1, 0.4], [1.0, 1.0])
        with pytest.raises(ValueError, match="planning_terminal_values has no effect"):
            sweep_sensitivity(
                s,
                h,
                planning_terminal_values=[0.0, 0.1],
                throughput_costs=[0.0],
                terminal_mode="fixed",
            )

    def test_fixed_mode_with_single_planning_value_is_allowed(self) -> None:
        """A single planning value (0.0) in fixed mode is fine."""
        s = _spec()
        h = _horizon([0.1, 0.4], [1.0, 1.0])
        results = sweep_sensitivity(
            s,
            h,
            planning_terminal_values=[0.0],
            throughput_costs=[0.0],
            terminal_mode="fixed",
        )
        assert len(results) == 1


# ---------------------------------------------------------------------------
# BLOCKER #4 — tp>0 sets upper bound, drops lower bound
# ---------------------------------------------------------------------------


class TestUpperBoundWithThroughputCost:
    """With tp>0, the incumbent's unpenalized cost is a valid upper bound."""

    def test_upper_bound_set_lower_none_for_tp_positive(self) -> None:
        """The upper bound is the incumbent's cost; the lower is None."""
        s = _spec()
        h = _horizon([0.1, 0.4], [1.0, 1.0])
        sched = solve_oracle(s, h, throughput_cost_eur_per_kwh=0.05)
        assert sched.cost_upper_bound_eur is not None
        assert sched.cost_upper_bound_eur == pytest.approx(sched.cost_eur)
        assert sched.cost_lower_bound_eur is None


# ---------------------------------------------------------------------------
# BLOCKER #5 — bool rejection in array paths
# ---------------------------------------------------------------------------


class TestBoolRejectionInArrays:
    """np.asarray silently coerces bools; reject them before the conversion."""

    def test_sweep_rejects_bool_in_planning_terminal_values(self) -> None:
        s = _spec()
        h = _horizon([0.1, 0.2], [1.0, 1.0])
        with pytest.raises(ValueError, match="bool"):
            sweep_sensitivity(
                s,
                h,
                planning_terminal_values=[True, 0.1],  # type: ignore[list-item]
                throughput_costs=[0.0],
            )

    def test_sweep_rejects_bool_in_throughput_costs(self) -> None:
        s = _spec()
        h = _horizon([0.1, 0.2], [1.0, 1.0])
        with pytest.raises(ValueError, match="bool"):
            sweep_sensitivity(
                s,
                h,
                planning_terminal_values=[0.0],
                throughput_costs=[False, 0.1],  # type: ignore[list-item]
            )


# ---------------------------------------------------------------------------
# BLOCKER #6 — compare_schedules horizon-identity check
# ---------------------------------------------------------------------------


class TestCompareSchedulesHorizonCheck:
    """Schedules from different horizons must not be silently compared."""

    def test_different_horizon_lengths_raises(self) -> None:
        """Two schedules with different n are incommensurable."""
        s = _spec()
        h2 = _horizon([0.1, 0.4], [1.0, 1.0])
        h3 = _horizon([0.1, 0.4, 0.2], [1.0, 1.0, 1.0])
        sched_a = solve_oracle(s, h2)
        sched_b = solve_oracle(s, h3)
        with pytest.raises(ValueError, match="different horizon lengths"):
            compare_schedules(
                {"a": sched_a, "b": sched_b},
                throughput_cost_eur_per_kwh=0.05,
            )

    def test_same_horizon_length_is_allowed(self) -> None:
        """Same n is fine, even if the schedules differ."""
        s = _spec()
        h = _horizon([0.1, 0.4], [1.0, 1.0])
        sched_a = solve_oracle(s, h)
        sched_b = policy_no_battery(s, h)
        result = compare_schedules(
            {"a": sched_a, "b": sched_b},
            throughput_cost_eur_per_kwh=0.05,
        )
        assert "a" in result
        assert "b" in result
