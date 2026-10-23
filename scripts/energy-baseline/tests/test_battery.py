"""Tests for the battery physical simulator — invariants, not snapshots.

Covers ADR-0003 §"Battery physical simulator": energy conservation with
round-trip loss on both legs, SOC bounds, one binary mode per interval,
no intentional export, the configured site import ceiling (a bound on *charging*
headroom — a measured load above it is an observation, not a violation),
terminal SOC accounting, identical
gap exclusion, the independence of efficiency from the SOC window, the DC-basis
EFC, the new terminal-inventory accounting fields on :class:`Schedule`, and
the frozen-array defensive-copy guarantee.
"""

from __future__ import annotations

import dataclasses

import numpy as np
import pytest

from energy_baseline.battery import (
    BatteryPhysicsError,
    BatterySpec,
    Horizon,
    Schedule,
    ceiling_exceedance_indices,
    check_feasible,
    compute_efc,
    simulate,
    soc_trajectory_kwh,
    validate_spec,
)

HOUR = 3600.0
QUARTER = 900.0


def spec_10kwh(**overrides) -> BatterySpec:
    """10 kWh nameplate, 1 kW default power, 1-hour intervals: hand-computable."""
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


def expected_cost(h: Horizon, grid: np.ndarray) -> float:
    """Independent re-implementation of the cost basis: usable intervals only."""
    return float(np.dot(h.prices[h.usable], grid[h.usable]))


# ---------------------------------------------------------------------------
# 1. Energy conservation, round-trip loss on BOTH legs
# ---------------------------------------------------------------------------


class TestEnergyConservation:
    def test_full_cycle_returns_charge_times_both_efficiencies(self) -> None:
        """D = C * eta_c * eta_d for a cycle that starts and ends at the same SOC."""
        spec = spec_10kwh(charge_efficiency=0.9, discharge_efficiency=0.8, power_kw=5.0)
        h = horizon([0.1, 0.5], [5.0, 5.0])

        charge = np.array([2.0, 0.0])
        # SOC rises by 2.0 * 0.9 = 1.8 kWh; to get back, discharge / 0.8 = 1.8.
        discharge = np.array([0.0, 1.8 * 0.8])
        sched = simulate(spec, h, charge, discharge)

        assert discharge[1] == pytest.approx(charge[0] * 0.9 * 0.8)
        assert sched.soc_kwh[0] == pytest.approx(1.8)
        assert sched.soc_kwh[-1] == pytest.approx(0.0)  # terminal == initial
        assert sched.throughput_kwh == pytest.approx(1.44)

    def test_soc_recurrence_uses_signed_flows(self) -> None:
        """Charge is credited at eta_c, discharge debited at 1/eta_d (loss on exit)."""
        spec = spec_10kwh(charge_efficiency=0.5, discharge_efficiency=0.5, power_kw=5.0)
        charge = np.array([2.0, 0.0, 0.0])
        discharge = np.array([0.0, 1.0, 0.0])
        soc = soc_trajectory_kwh(spec, charge, discharge)
        assert soc[0] == pytest.approx(2.0 * 0.5)
        assert soc[1] == pytest.approx(1.0 - 1.0 / 0.5)  # 1.0 - 2.0
        assert soc[2] == pytest.approx(-1.0)  # below the floor: rejected below
        h = horizon([0.1, 0.1, 0.1], [5.0, 5.0, 5.0])
        with pytest.raises(BatteryPhysicsError, match="below window floor"):
            simulate(spec, h, charge, discharge, enforce_terminal_soc=False)

    def test_simulate_soc_matches_the_recurrence_helper(self) -> None:
        """simulate() reports exactly the trajectory soc_trajectory_kwh computes."""
        rng = np.random.default_rng(1)
        n = 200
        spec = spec_10kwh(
            charge_efficiency=0.93, discharge_efficiency=0.88, power_kw=1.0, initial_soc_frac=0.5
        )
        limit = spec.interval_energy_limit_kwh(HOUR)
        floor, ceil = spec.soc_bound_kwh("min"), spec.soc_bound_kwh("max")
        soc = spec.soc_bound_kwh("init")
        charge = np.zeros(n)
        discharge = np.zeros(n)
        for t in range(n):  # greedy but clamped: guaranteed per-step feasible
            if rng.random() > 0.5:
                charge[t] = min(limit, (ceil - soc) / spec.charge_efficiency)
            else:
                discharge[t] = min(limit, (soc - floor) * spec.discharge_efficiency)
            soc += charge[t] * spec.charge_efficiency - discharge[t] / spec.discharge_efficiency
        h = horizon(np.full(n, 0.2), np.full(n, 50.0))
        # every per-step invariant holds for this walk
        assert check_feasible(spec, h, charge, discharge, enforce_terminal_soc=False) == []
        sched = simulate(spec, h, charge, discharge, enforce_terminal_soc=False)
        assert np.allclose(sched.soc_kwh, soc_trajectory_kwh(spec, charge, discharge))
        assert np.allclose(sched.grid_kwh, h.load_kwh + charge - discharge)
        assert sched.throughput_kwh == pytest.approx(discharge.sum())
        # if the walk did not land back on the seed SOC, the accounting rule says so
        drift = abs(sched.soc_kwh[-1] - spec.soc_bound_kwh("init"))
        terminal = check_feasible(spec, h, charge, discharge)
        assert bool(terminal) == (drift > 1e-6)

    def test_floor_is_checked_before_the_terminal_rule_matters(self) -> None:
        spec = spec_10kwh(charge_efficiency=0.5, discharge_efficiency=0.5, power_kw=5.0)
        h = horizon([0.1, 0.1], [5.0, 5.0])
        violations = check_feasible(spec, h, [2.0, 0.0], [0.0, 1.0])
        assert any("below window floor" in v for v in violations)

    def test_terminal_drift_from_initial_is_rejected(self) -> None:
        """A cycle that does not return to the seed SOC creates/destroys energy."""
        spec = spec_10kwh(charge_efficiency=0.9, discharge_efficiency=0.9)
        h = horizon([0.1, 0.5], [5.0, 5.0])
        with pytest.raises(BatteryPhysicsError, match="terminal SOC"):
            simulate(spec, h, [1.0, 0.0], [0.0, 0.5])

    def test_terminal_drift_is_optional_for_causal_plans(self) -> None:
        spec = spec_10kwh(charge_efficiency=0.9, discharge_efficiency=0.9)
        h = horizon([0.1, 0.5], [5.0, 5.0])
        sched = simulate(spec, h, [1.0, 0.0], [0.0, 0.5], enforce_terminal_soc=False)
        assert sched.soc_kwh[-1] == pytest.approx(0.9 - 0.5 / 0.9)
        # and the same plan is still reported as terminal-infeasible when asked
        assert any("terminal SOC" in v for v in check_feasible(spec, h, [1.0, 0.0], [0.0, 0.5]))

    def test_explicit_terminal_soc_is_honoured(self) -> None:
        spec = spec_10kwh(initial_soc_frac=0.0, terminal_soc_frac=0.3, power_kw=5.0)
        h = horizon([0.1, 0.5], [5.0, 5.0])
        sched = simulate(spec, h, [3.0, 0.0], [0.0, 0.0])
        assert sched.soc_kwh[-1] == pytest.approx(3.0)
        with pytest.raises(BatteryPhysicsError, match="terminal SOC"):
            simulate(spec, h, [2.0, 0.0], [0.0, 0.0])

    def test_bad_efficiencies_and_negative_flows(self) -> None:
        with pytest.raises(ValueError, match="charge_efficiency"):
            validate_spec(spec_10kwh(charge_efficiency=0.0))
        with pytest.raises(ValueError, match="charge_efficiency"):
            validate_spec(spec_10kwh(charge_efficiency=1.5))
        with pytest.raises(ValueError, match="discharge_efficiency"):
            validate_spec(spec_10kwh(discharge_efficiency=-0.1))
        spec = spec_10kwh()
        h = horizon([0.1], [1.0])
        with pytest.raises(BatteryPhysicsError, match="negative flow"):
            simulate(spec, h, [-0.1], [0.0])


# ---------------------------------------------------------------------------
# 2. SOC bounds at every step
# ---------------------------------------------------------------------------


class TestSocBounds:
    def test_ceiling_violation_detected(self) -> None:
        spec = spec_10kwh(soc_max_frac=0.5, power_kw=20.0)  # ceiling = 5 kWh
        h = horizon([0.1, 0.1], [30.0, 30.0])
        violations = check_feasible(spec, h, [6.0, 0.0], [0.0, 0.0])
        assert any("above window ceiling" in v for v in violations)
        with pytest.raises(BatteryPhysicsError, match="above window ceiling"):
            simulate(spec, h, [6.0, 0.0], [0.0, 0.0])

    def test_floor_violation_detected(self) -> None:
        spec = spec_10kwh(soc_min_frac=0.2, initial_soc_frac=0.3, power_kw=20.0)  # floor = 2 kWh
        h = horizon([0.1, 0.1], [30.0, 30.0])
        violations = check_feasible(spec, h, [0.0, 0.0], [2.0, 0.0])
        assert any("below window floor" in v for v in violations)

    def test_bounds_held_at_every_step_not_just_the_end(self) -> None:
        """A plan that overshot mid-horizon and came back is still infeasible."""
        spec = spec_10kwh(soc_max_frac=0.4, power_kw=20.0)  # ceiling = 4 kWh
        h = horizon([0.1] * 4, [30.0] * 4)
        charge = np.array([5.0, 0.0, 0.0, 0.0])  # overshoot at t=0
        discharge = np.array([0.0, 5.0, 0.0, 0.0])  # back to 0 by t=1
        soc = 10.0 * 0.0 + np.cumsum(charge - discharge)
        assert soc.max() > 10.0 * 0.4
        assert soc[-1] == pytest.approx(0.0)
        with pytest.raises(BatteryPhysicsError, match="above window ceiling"):
            simulate(spec, h, charge, discharge)

    def test_power_limit_per_interval_scales_with_duration(self) -> None:
        """2.5 kW for 15 min is 0.625 kWh; the same power for 1 h is 2.5 kWh."""
        spec = spec_10kwh(power_kw=2.5)
        h15 = horizon([0.1], [5.0], interval_seconds=QUARTER)
        h60 = horizon([0.1], [5.0], interval_seconds=HOUR)
        assert spec.interval_energy_limit_kwh(h15.interval_seconds) == pytest.approx(0.625)
        assert spec.interval_energy_limit_kwh(h60.interval_seconds) == pytest.approx(2.5)
        with pytest.raises(BatteryPhysicsError, match="above power limit"):
            simulate(spec, h15, [1.0], [0.0], enforce_terminal_soc=False)
        simulate(spec, h60, [1.0], [0.0], enforce_terminal_soc=False)  # fine over an hour

    def test_aggressive_price_signal_never_breaks_bounds_in_a_validated_plan(self) -> None:
        """Sawtooth prices with absurd power: any plan simulate() accepts is bounded."""
        rng = np.random.default_rng(0)
        n = 400
        prices = np.where(rng.random(n) > 0.5, 9.0, -9.0)
        load = rng.uniform(0.5, 2.0, n)
        spec = spec_10kwh(
            capacity_kwh=5.12,
            power_kw=200.0,
            charge_efficiency=0.97,
            discharge_efficiency=0.97,
            soc_min_frac=0.05,
            soc_max_frac=0.95,
            initial_soc_frac=0.5,
        )
        h = horizon(prices, load, interval_seconds=QUARTER)
        limit = spec.interval_energy_limit_kwh(h.interval_seconds)
        # a deliberately greedy plan: charge on lows, discharge on highs, at full power
        charge = np.where(prices < 0, limit, 0.0)
        discharge = np.where(prices > 0, limit, 0.0)
        violations = check_feasible(spec, h, charge, discharge)
        assert violations  # greedy plans must be caught...
        assert any("window" in v for v in violations)
        # ...while the clamped variant passes every per-step invariant
        soc = spec.soc_bound_kwh("init")
        charge2 = np.zeros(n)
        discharge2 = np.zeros(n)
        for t in range(n):
            if prices[t] < 0:
                charge2[t] = min(limit, (spec.soc_bound_kwh("max") - soc) / 0.97)
            elif prices[t] > 0:
                discharge2[t] = min(limit, (soc - spec.soc_bound_kwh("min")) * 0.97, load[t])
            soc += charge2[t] * 0.97 - discharge2[t] / 0.97
        assert check_feasible(spec, h, charge2, discharge2, enforce_terminal_soc=False) == []
        sched = simulate(spec, h, charge2, discharge2, enforce_terminal_soc=False)
        floor = spec.soc_bound_kwh("min")
        ceil = spec.soc_bound_kwh("max")
        assert np.all((sched.soc_kwh >= floor - 1e-9) & (sched.soc_kwh <= ceil + 1e-9))
        assert np.all(sched.grid_kwh >= -1e-9)


# ---------------------------------------------------------------------------
# 3. One binary mode per interval
# ---------------------------------------------------------------------------


class TestMutualExclusion:
    def test_simultaneous_charge_and_discharge_is_a_violation(self) -> None:
        spec = spec_10kwh(power_kw=2.0)
        h = horizon([0.1, 0.1], [5.0, 5.0])
        violations = check_feasible(spec, h, [1.0, 0.0], [1.0, 0.0])
        assert any("simultaneous charge and discharge" in v for v in violations)
        with pytest.raises(BatteryPhysicsError, match="simultaneous"):
            simulate(spec, h, [1.0, 0.0], [1.0, 0.0])

    def test_harvesting_loop_under_negative_prices_is_rejected(self) -> None:
        """Charging and discharging at once inflates import to farm a negative price.

        With ``eta_c*eta_d < 1`` the SOC-neutral loop ``charge = M``,
        ``discharge = M*eta_c*eta_d`` leaves the SOC unchanged while raising
        ``grid`` by ``M*(1 - eta_c*eta_d)`` — free money at any negative price.
        The one-binary-mode invariant is what forbids it.
        """
        spec = spec_10kwh(charge_efficiency=0.9, discharge_efficiency=0.9, power_kw=1.0)
        m = 1.0
        loop_discharge = m * 0.9 * 0.9
        assert loop_discharge / 0.9 == pytest.approx(m * 0.9)  # SOC-neutral by construction
        h = horizon([-5.0, -5.0], [1.0, 1.0])
        with pytest.raises(BatteryPhysicsError, match="simultaneous"):
            simulate(spec, h, [m, 0.0], [loop_discharge, 0.0], enforce_terminal_soc=False)

    def test_strongly_negative_prices_still_forbid_both_flows(self) -> None:
        spec = spec_10kwh(charge_efficiency=0.9, discharge_efficiency=0.9, power_kw=2.0)
        n = 6
        h = horizon([-12.0] * n, [3.0] * n)
        charge = np.array([1.0, 1.0, 0.0, 0.0, 1.0, 0.0])
        discharge = np.array([0.0, 1.0, 0.0, 1.0, 0.0, 0.0])
        violations = check_feasible(spec, h, charge, discharge)
        assert any("simultaneous" in v for v in violations)

    def test_zero_flows_are_not_simultaneous(self) -> None:
        spec = spec_10kwh()
        h = horizon([0.1, 0.2], [1.0, 1.0])
        assert check_feasible(spec, h, [0.0, 0.0], [0.0, 0.0]) == []


# ---------------------------------------------------------------------------
# 4. Grid import never negative (no intentional export)
# ---------------------------------------------------------------------------


class TestNoExport:
    def test_discharge_above_load_is_a_violation(self) -> None:
        spec = spec_10kwh(initial_soc_frac=1.0)
        h = horizon([0.5, 0.5], [0.2, 0.2])
        violations = check_feasible(spec, h, [0.0, 0.0], [1.0, 0.0])
        assert any("intentional export" in v for v in violations)
        with pytest.raises(BatteryPhysicsError, match="intentional export"):
            simulate(spec, h, [0.0, 0.0], [1.0, 0.0])

    def test_grid_identity_holds(self) -> None:
        """grid = load + charge - discharge, exactly."""
        spec = spec_10kwh(charge_efficiency=0.9, discharge_efficiency=0.9, initial_soc_frac=0.5)
        h = horizon([0.1, 0.2, 0.3], [1.0, 1.0, 2.0])
        charge = np.array([1.0, 0.0, 0.0])
        discharge = np.array([0.0, 0.81, 0.0])
        sched = simulate(spec, h, charge, discharge)
        assert np.allclose(sched.grid_kwh, h.load_kwh + charge - discharge)
        assert np.all(sched.grid_kwh >= -1e-12)
        assert np.allclose(sched.net_flow_kwh, charge - discharge)
        # charge increases import, discharge reduces it, never below zero
        assert sched.grid_kwh[0] == pytest.approx(2.0)
        assert sched.grid_kwh[1] == pytest.approx(0.19)

    def test_charging_above_the_load_never_creates_an_export(self) -> None:
        """The physics reason a ``charge <= load`` rule is unmotivated.

        Import is ``load + charge - discharge``: with the no-export rule written
        that way, *adding* charge can only ever move ``grid`` further above zero.
        Only the discharge side is bounded by the load.  Randomized here with
        charges far above a night baseload and zero discharge.
        """
        rng = np.random.default_rng(60)
        spec = spec_10kwh(power_kw=5.0, initial_soc_frac=0.0, terminal_soc_frac=1.0)
        for trial in range(30):
            n = 8
            load = rng.uniform(0.02, 0.2, n)  # night baseload: tens of watts
            charge = rng.uniform(0.5, 1.0, n)  # deliberately way above the load
            assert np.all(charge > load)
            assert charge.sum() <= spec.soc_bound_kwh("max")
            h = horizon(np.full(n, 0.1), load)
            sched = simulate(spec, h, charge, np.zeros(n), enforce_terminal_soc=False)
            assert np.allclose(sched.grid_kwh, load + charge), trial
            assert np.all(sched.grid_kwh >= 0.0), trial
            assert check_feasible(spec, h, charge, np.zeros(n), enforce_terminal_soc=False) == []


# ---------------------------------------------------------------------------
# 5b. Site import ceiling: configured or unknown, never asserted from a sensor
# ---------------------------------------------------------------------------


class TestImportCeiling:
    def test_field_defaults_to_unknown_not_a_number(self) -> None:
        """An unconfigured ceiling must not silently become a numeric limit."""
        h = horizon([0.1, 0.2], [1.0, 1.0])
        assert h.import_limit_kw is None
        assert h.interval_import_limit_kwh is None
        names = [f.name for f in dataclasses.fields(Horizon)]
        assert names[-1] == "import_limit_kw"

    @pytest.mark.parametrize(
        ("interval_seconds", "import_limit_kw", "expected_kwh"),
        [
            (HOUR, 2.0, 2.0),
            (QUARTER, 2.0, 0.5),
            (QUARTER, 1.0, 0.25),
            (QUARTER, 0.0, 0.0),
        ],
    )
    def test_ceiling_is_expressed_per_interval(
        self, interval_seconds: float, import_limit_kw: float, expected_kwh: float
    ) -> None:
        h = horizon(
            [0.1], [0.1], interval_seconds=interval_seconds, import_limit_kw=import_limit_kw
        )
        assert h.interval_import_limit_kwh == pytest.approx(expected_kwh)

    def test_ceiling_must_be_finite_and_non_negative(self) -> None:
        with pytest.raises(ValueError, match="import_limit_kw"):
            horizon([0.1], [1.0], import_limit_kw=-0.5)
        with pytest.raises(ValueError, match="import_limit_kw"):
            horizon([0.1], [1.0], import_limit_kw=np.nan)
        with pytest.raises(ValueError, match="import_limit_kw"):
            horizon([0.1], [1.0], import_limit_kw=np.inf)
        assert horizon([0.1], [1.0], import_limit_kw=0.0).import_limit_kw == 0.0

    def test_hand_made_schedule_over_the_ceiling_is_reported(self) -> None:
        """1 kW contracted, 1 h intervals: load 0.6 leaves 0.4 kWh of headroom.

        A 0.8 kWh charge therefore tops the ceiling by 0.4 kWh and is a violation
        of the *decision*, not of the measurement — a plan could have charged less.
        """
        spec = spec_10kwh(power_kw=5.0, initial_soc_frac=0.2)
        h = horizon([0.1, 0.1], [0.6, 0.6], import_limit_kw=1.0)
        violations = check_feasible(spec, h, [0.8, 0.0], [0.0, 0.0], enforce_terminal_soc=False)
        assert len(violations) == 1
        assert "import ceiling" in violations[0]
        assert "1 kWh/interval" in violations[0]
        with pytest.raises(BatteryPhysicsError, match="import ceiling"):
            simulate(spec, h, [0.8, 0.0], [0.0, 0.0], enforce_terminal_soc=False)
        # the identical plan is fine once the ceiling is unknown
        free = horizon([0.1, 0.1], [0.6, 0.6])
        assert check_feasible(spec, free, [0.8, 0.0], [0.0, 0.0], enforce_terminal_soc=False) == []

    def test_charge_exactly_up_to_the_ceiling_is_feasible(self) -> None:
        spec = spec_10kwh(power_kw=5.0, initial_soc_frac=0.2)
        h = horizon([0.1, 0.1], [0.6, 0.6], import_limit_kw=1.0)
        assert check_feasible(spec, h, [0.4, 0.0], [0.0, 0.0], enforce_terminal_soc=False) == []

    def test_ceiling_never_bounds_the_discharge_side(self) -> None:
        """The ceiling is a charge-side rule, so discharge is unconstrained by it.

        The constraint is ``charge[t] <= max(0, ceiling - load[t])``, not a bound
        on the net import and not a bound on discharge.  Here the battery starts
        full and discharges to cover the whole load, so net import is zero —
        the physically correct response to a tight ceiling, and one the ceiling
        rule must not interfere with.
        """
        # 10 kWh seed, 0.6 + 0.6 kWh discharged -> 8.8 kWh left, hence terminal 0.88
        spec = spec_10kwh(power_kw=5.0, initial_soc_frac=1.0, terminal_soc_frac=0.88)
        h = horizon([0.5, 0.5], [0.6, 0.6], import_limit_kw=1.0)
        charge = np.zeros(2)
        discharge = np.array([0.6, 0.6])  # grid == 0 in both intervals
        assert check_feasible(spec, h, charge, discharge) == []
        sched = simulate(spec, h, charge, discharge)
        assert np.allclose(sched.grid_kwh, 0.0)
        assert sched.ceiling_exceeding_intervals == 0
        # a discharge above the ceiling is fine; the same discharge above the
        # *load* would be an export regardless
        assert any(
            "intentional export" in v
            for v in check_feasible(spec, h, charge, [1.0, 0.6], enforce_terminal_soc=False)
        )

    def test_exceedance_report_is_indices_and_usable_scoped(self) -> None:
        """The observation names intervals, only counts usable ones, and is empty
        when no ceiling is configured — and it never touches the accounting."""
        spec = spec_10kwh(power_kw=5.0, initial_soc_frac=0.5)
        load = np.array([0.5, 2.0, 0.5, 3.0, 0.5])
        usable = [True, True, True, False, True]
        h = horizon(np.full(5, 0.2), load, usable=usable, import_limit_kw=1.0)
        # t3 has the worst load but is gap-excluded; t1 is the counted breach
        assert np.array_equal(ceiling_exceedance_indices(h, load), [1])
        charge = np.array([0.4, 0.0, 0.0, 0.0, 0.0])
        sched = simulate(spec, h, charge, np.zeros(5), enforce_terminal_soc=False)
        assert sched.ceiling_exceeding_intervals == 1
        # the ceiling is not a term in the accounting: identical cost/SOC/grid with
        # and without it, as long as no charge exceeds the headroom
        uncapped = dataclasses.replace(h, import_limit_kw=None)
        same = simulate(spec, uncapped, charge, np.zeros(5), enforce_terminal_soc=False)
        assert sched.cost_eur == pytest.approx(same.cost_eur)
        assert np.allclose(sched.soc_kwh, same.soc_kwh)
        assert np.allclose(sched.grid_kwh, same.grid_kwh)
        assert same.ceiling_exceeding_intervals == 0
        with pytest.raises(ValueError, match="grid_kwh has"):
            ceiling_exceedance_indices(h, [1.0, 2.0])
        with pytest.raises(ValueError, match="non-finite"):
            ceiling_exceedance_indices(h, [np.nan, 1.0, 1.0, 1.0, 1.0])

    def test_charge_headroom_property_shape(self) -> None:
        """``None`` unknown, else ``max(0, ceiling*h - load)`` read-only per interval."""
        load = np.array([0.5, 2.0, 1.0])
        capped = horizon([0.1] * 3, load, import_limit_kw=1.0)  # 1 h intervals
        assert np.allclose(capped.charge_headroom_kwh, [0.5, 0.0, 0.0])
        assert not capped.charge_headroom_kwh.flags.writeable
        with pytest.raises((ValueError, TypeError)):
            capped.charge_headroom_kwh[0] = 9.0
        assert horizon([0.1] * 3, load).charge_headroom_kwh is None
        # resolution-independent: the same power over 15 min is a quarter the energy
        quarter = horizon([0.1] * 3, load, interval_seconds=QUARTER, import_limit_kw=4.0)
        assert quarter.interval_import_limit_kwh == pytest.approx(1.0)  # 4 kW * 0.25 h
        assert np.allclose(quarter.charge_headroom_kwh, [0.5, 0.0, 0.0])

    def test_load_alone_over_the_ceiling_is_an_observation_not_a_violation(self) -> None:
        """A measured load above the ceiling is history, not a dispatch error.

        The ceiling limits charging decisions; it cannot un-measure the past.
        With ``charge = discharge = 0`` the net import equals the load, and the
        kernel records the ceiling-exceeding intervals as an observation on the
        schedule rather than as a violation — the no-battery baseline must
        remain feasible on real data, where a 5.0 kW contract saw 55 of 13 510
        one-minute spans above 5.0 kW without tripping the supply.
        """
        spec = spec_10kwh()
        h = horizon([0.1, 0.1], [2.0, 2.0], import_limit_kw=1.0)
        assert check_feasible(spec, h, [0.0, 0.0], [0.0, 0.0]) == []
        sched = simulate(spec, h, [0.0, 0.0], [0.0, 0.0])
        assert sched.ceiling_exceeding_intervals == 2
        assert np.array_equal(ceiling_exceedance_indices(h, sched.grid_kwh), [0, 1])
        # and the identical plan on an unknown ceiling reports zero exceedances
        free = horizon([0.1, 0.1], [2.0, 2.0])
        free_sched = simulate(spec, free, [0.0, 0.0], [0.0, 0.0])
        assert free_sched.ceiling_exceeding_intervals == 0

    def test_a_feasible_plan_never_causes_a_breach(self) -> None:
        """Charging only ever tops the ceiling where the load already did.

        ``charge[t] <= max(0, ceiling - load[t])`` means
        ``grid[t] <= max(load[t], ceiling)``, so an interval above the ceiling is
        above it *because of the load*.  Randomized greedy plans, all headroom-legal.
        """
        rng = np.random.default_rng(23)
        spec = spec_10kwh(power_kw=5.0, charge_efficiency=0.9, discharge_efficiency=0.9)
        n = 60
        load = rng.uniform(0.02, 2.0, n)  # frequently above the 1.0 kWh ceiling
        h = horizon(np.full(n, 0.2), load, import_limit_kw=1.0)
        headroom = h.charge_headroom_kwh
        limit = spec.interval_energy_limit_kwh(HOUR)
        charge = np.zeros(n)
        discharge = np.zeros(n)
        soc = spec.soc_bound_kwh("init")
        for t in range(n):
            if rng.random() < 0.5:
                charge[t] = min(limit, float(headroom[t]), (spec.soc_bound_kwh("max") - soc) / 0.9)
            else:
                discharge[t] = min(
                    limit, max(soc - spec.soc_bound_kwh("min"), 0.0) * 0.9, max(load[t], 0.0)
                )
            soc += charge[t] * 0.9 - discharge[t] / 0.9
        assert check_feasible(spec, h, charge, discharge, enforce_terminal_soc=False) == []
        sched = simulate(spec, h, charge, discharge, enforce_terminal_soc=False)
        assert np.all(sched.grid_kwh <= np.maximum(load, h.interval_import_limit_kwh) + 1e-9)
        exceeded = ceiling_exceedance_indices(h, sched.grid_kwh)
        # every reported interval is one the *load* alone already topped the
        # ceiling in, and discharge can only shrink the set — never create it
        assert np.all(load[exceeded] > h.interval_import_limit_kwh)
        assert sched.ceiling_exceeding_intervals <= int((load > 1.0 + 1e-6).sum())
        # with the battery inert the observation is exactly the load's own breaches
        inert = simulate(spec, h, np.zeros(n), np.zeros(n), enforce_terminal_soc=False)
        assert inert.ceiling_exceeding_intervals == int((load > 1.0 + 1e-6).sum())

    def test_discharge_is_permitted_in_a_ceiling_exceeding_interval(self) -> None:
        """Discharge lowers the draw, so it is the correct response to a tight ceiling.

        A battery that starts full and discharges into a load that alone exceeds
        the ceiling brings the net import *below* the ceiling — the physically
        correct outcome, not a violation.  The no-export rule still holds.
        """
        spec = spec_10kwh(power_kw=5.0, initial_soc_frac=1.0)
        h = horizon([0.5, 0.5], [2.0, 2.0], import_limit_kw=1.0)
        charge = np.zeros(2)
        # discharge 0.8 each interval: grid = 2.0 - 0.8 = 1.2 kWh, still above the
        # 1.0 kWh ceiling because the load alone is, yet feasible and export-free
        partial = simulate(spec, h, charge, np.array([0.8, 0.8]), enforce_terminal_soc=False)
        assert check_feasible(spec, h, charge, [0.8, 0.8], enforce_terminal_soc=False) == []
        assert np.all(partial.grid_kwh >= 0.0)
        assert partial.ceiling_exceeding_intervals == 2
        # a stronger discharge brings the net import under the ceiling: same plan
        # class, no observation left, still no export
        deep = simulate(spec, h, charge, np.array([1.5, 1.5]), enforce_terminal_soc=False)
        assert check_feasible(spec, h, charge, [1.5, 1.5], enforce_terminal_soc=False) == []
        assert np.all(deep.grid_kwh <= h.interval_import_limit_kwh + 1e-9)
        assert deep.ceiling_exceeding_intervals == 0
        # and pushing past the load would be an export regardless of the ceiling
        assert any(
            "intentional export" in v
            for v in check_feasible(spec, h, charge, [2.5, 1.5], enforce_terminal_soc=False)
        )

    def test_ceiling_violations_are_aggregated_over_a_long_horizon(self) -> None:
        n = 96 * 14
        spec = spec_10kwh(power_kw=5.0, initial_soc_frac=0.2)
        h = horizon(np.full(n, 0.1), np.full(n, 1.0), import_limit_kw=1.0)
        violations = check_feasible(spec, h, np.full(n, 1.0), np.zeros(n))
        assert len(violations) <= 3
        assert any("more)" in v for v in violations)

    def test_ceiling_does_not_touch_cost_or_soc(self) -> None:
        """It is a constraint on the plan, not a new term in the accounting."""
        spec = spec_10kwh(power_kw=5.0, charge_efficiency=0.9, discharge_efficiency=0.9)
        prices = [0.2, 0.4]
        load = [0.6, 0.6]
        charge = [0.3, 0.0]
        discharge = [0.0, 0.3 * 0.9 * 0.9]  # SOC-neutral by construction
        plain = simulate(spec, horizon(prices, load), charge, discharge)
        capped = simulate(
            spec, horizon(prices, load, import_limit_kw=5.0), charge, discharge
        )  # 5 kW * 1 h = 5 kWh: never binding
        assert capped.cost_eur == pytest.approx(plain.cost_eur)
        assert np.allclose(capped.soc_kwh, plain.soc_kwh)
        assert np.allclose(capped.grid_kwh, plain.grid_kwh)

    def test_replace_keeps_the_horizon_other_fields(self) -> None:
        h = horizon([0.1, 0.2], [1.0, 2.0], usable=[True, False])
        capped = dataclasses.replace(h, import_limit_kw=3.0)
        assert capped.import_limit_kw == 3.0
        assert capped.interval_import_limit_kwh == pytest.approx(3.0)
        assert capped.usable.tolist() == [True, False]
        assert np.allclose(capped.prices, h.prices)
        assert np.allclose(capped.load_kwh, h.load_kwh)


# ---------------------------------------------------------------------------
# 5/11. Cost basis and identical gap exclusion
# ---------------------------------------------------------------------------


class TestCostBasis:
    def test_cost_uses_pretax_price_and_net_import(self) -> None:
        spec = spec_10kwh()
        h = horizon([0.1, 0.2, 0.3], [1.0, 1.0, 1.0])
        sched = simulate(spec, h, [1.0, 0.0, 0.0], [0.0, 1.0, 0.0])
        # t0: grid 2.0 -> 0.2 ; t1: grid 0.0 -> 0.0 ; t2: grid 1.0 -> 0.3
        assert sched.cost_eur == pytest.approx(0.5)
        assert sched.cost_eur == pytest.approx(expected_cost(h, sched.grid_kwh))

    def test_negative_prices_produce_negative_cost(self) -> None:
        spec = spec_10kwh()
        h = horizon([-0.5, -0.2], [1.0, 1.0])
        sched = simulate(spec, h, [0.0, 0.0], [0.0, 0.0])
        assert sched.cost_eur == pytest.approx(-0.7)

    def test_unusable_intervals_are_zeroed_and_excluded(self) -> None:
        spec = spec_10kwh()
        h = horizon([0.1, 0.2, 0.3], [1.0, 1.0, 1.0], usable=[True, False, True])
        sched = simulate(spec, h, [1.0, 0.0, 0.0], [0.0, 0.0, 1.0])
        assert sched.cost_eur == pytest.approx(0.1 * 2.0 + 0.3 * 0.0)
        assert sched.cost_eur == pytest.approx(expected_cost(h, sched.grid_kwh))
        with pytest.raises(BatteryPhysicsError, match="unusable interval"):
            simulate(spec, h, [1.0, 1.0, 0.0], [0.0, 0.0, 0.0])

    def test_unusable_interval_does_not_move_soc(self) -> None:
        spec = spec_10kwh()
        prices = [0.1, 0.9, 0.1]
        h_full = horizon(prices, [1.0, 1.0, 1.0])
        h_gap = horizon(prices, [1.0, 1.0, 1.0], usable=[True, False, True])
        sched = simulate(spec, h_gap, [1.0, 0.0, 0.0], [0.0, 0.0, 1.0])
        assert sched.soc_kwh[1] == pytest.approx(sched.soc_kwh[0])
        # the same plan on a fully usable horizon costs the gap interval too
        busy = simulate(spec, h_full, [1.0, 0.0, 0.0], [0.0, 0.0, 1.0])
        assert busy.cost_eur > sched.cost_eur

    def test_all_intervals_unusable_is_rejected(self) -> None:
        """A horizon that excludes every interval has an empty cost basis.

        The comparison would report zero cost for all three policies and be
        meaningless, so :class:`Horizon` rejects it at construction time.
        """
        with pytest.raises(ValueError, match="usable excludes all"):
            horizon([0.1, 0.2], [1.0, 1.0], usable=[False, False])

    def test_horizon_validation(self) -> None:
        with pytest.raises(ValueError, match="at least one interval"):
            Horizon(HOUR, [], [], [])
        with pytest.raises(ValueError, match="interval_seconds"):
            Horizon(0.0, [0.1], [1.0], [True])
        with pytest.raises(ValueError, match="load_kwh has"):
            Horizon(HOUR, [0.1, 0.2], [1.0], [True, True])
        with pytest.raises(ValueError, match="usable has"):
            Horizon(HOUR, [0.1, 0.2], [1.0, 1.0], [True])
        with pytest.raises(ValueError, match="non-finite"):
            Horizon(HOUR, [0.1, np.nan], [1.0, 1.0], [True, True])
        broad = Horizon(HOUR, [0.1, 0.2], [1.0, 1.0], np.True_)  # scalar broadcast
        assert broad.usable.tolist() == [True, True]
        assert broad.prices.dtype == np.float64
        assert broad.interval_hours == pytest.approx(1.0)
        assert broad.n == 2

    def test_plan_shape_errors(self) -> None:
        spec = spec_10kwh()
        h = horizon([0.1, 0.2], [1.0, 1.0])
        with pytest.raises(ValueError, match="entries, horizon has"):
            simulate(spec, h, [1.0], [0.0, 0.0])
        with pytest.raises(ValueError, match="non-finite"):
            simulate(spec, h, [0.0, np.inf], [0.0, 0.0])


# ---------------------------------------------------------------------------
# 6. EFC and throughput — DC basis
# ---------------------------------------------------------------------------


class TestEfc:
    def test_efc_is_dc_throughput_over_usable_window(self) -> None:
        """EFC divides the *DC* discharge throughput by the usable DC window.

        With ``eta_d == 1`` the DC throughput equals the AC throughput, so the
        formula reduces to the historical one and existing lossless tests keep
        passing unchanged.
        """
        spec = spec_10kwh(capacity_kwh=5.0, soc_min_frac=0.1, soc_max_frac=0.9)
        window = 5.0 * 0.8
        assert spec.usable_window_kwh == pytest.approx(window)
        assert compute_efc(spec, window) == pytest.approx(1.0)
        assert compute_efc(spec, window / 2.0) == pytest.approx(0.5)
        assert compute_efc(spec, 0.0) == 0.0

    def test_efc_accounts_for_discharge_efficiency(self) -> None:
        """The same AC delivered energy required more DC coulomb throughput when
        the discharge is lossy — that extra DC work is what degrades the cells.

        2 kWh AC delivered at ``eta_d = 0.9`` means the cells moved
        ``2 / 0.9 ≈ 2.22`` kWh DC, so EFC is larger than for a lossless battery
        delivering the same AC energy.
        """
        lossless = spec_10kwh(capacity_kwh=5.0, soc_min_frac=0.1, soc_max_frac=0.9)
        lossy = spec_10kwh(
            capacity_kwh=5.0,
            soc_min_frac=0.1,
            soc_max_frac=0.9,
            charge_efficiency=0.9,
            discharge_efficiency=0.9,
        )
        window = lossless.usable_window_kwh
        # lossless: DC = AC / 1.0 = 2.0
        assert compute_efc(lossless, 2.0) == pytest.approx(2.0 / window)
        # lossy: DC = 2.0 / 0.9 > 2.0
        assert compute_efc(lossy, 2.0) == pytest.approx((2.0 / 0.9) / window)
        assert compute_efc(lossy, 2.0) > compute_efc(lossless, 2.0)

    def test_full_usable_dc_drain_equals_one_efc_with_lossy_discharge(self) -> None:
        """Draining the whole usable DC window is exactly 1 EFC, even when
        ``eta_d < 1``: the cells moved the whole window's worth of energy, only
        less of it reached the meter.

        Start at the SOC ceiling, discharge down to the floor; the AC throughput
        is ``window * eta_d`` (less than the window), but the DC throughput is
        ``window * eta_d / eta_d == window`` — one full crossing.
        """
        eta = 0.85
        spec = spec_10kwh(
            capacity_kwh=10.0,
            power_kw=10.0,
            charge_efficiency=0.92,
            discharge_efficiency=eta,
            soc_min_frac=0.1,
            soc_max_frac=0.9,
            initial_soc_frac=0.9,
            terminal_soc_frac=0.1,
        )
        window = spec.usable_window_kwh  # 8.0 kWh DC
        h = horizon([0.5], [20.0])
        ac_delivered = window * eta  # 6.8 kWh AC
        sched = simulate(spec, h, [0.0], [ac_delivered])
        assert sched.throughput_kwh == pytest.approx(ac_delivered)  # AC delivered
        assert sched.efc == pytest.approx(1.0)  # DC basis: one full crossing

    def test_efc_counts_full_window_crossings(self) -> None:
        """Four 0.25 kWh discharges of a 1 kWh window == 1.0 EFC."""
        spec = spec_10kwh(capacity_kwh=1.0, power_kw=1.0)
        h = horizon([0.1] * 8, [10.0] * 8, interval_seconds=QUARTER)
        quarter = spec.interval_energy_limit_kwh(QUARTER)
        assert quarter == pytest.approx(0.25)
        charge = np.array([quarter] * 4 + [0.0] * 4)
        discharge = np.array([0.0] * 4 + [quarter] * 4)
        sched = simulate(spec, h, charge, discharge)
        assert sched.throughput_kwh == pytest.approx(1.0)
        assert sched.efc == pytest.approx(1.0)

    def test_efc_rejects_degenerate_window_and_bad_throughput(self) -> None:
        flat = spec_10kwh(soc_min_frac=0.5, soc_max_frac=0.7, initial_soc_frac=0.6)
        with pytest.raises(ValueError, match="soc_max > soc_min"):
            compute_efc(spec_10kwh(soc_min_frac=0.7, soc_max_frac=0.7, initial_soc_frac=0.7), 1.0)
        assert flat.usable_window_kwh == pytest.approx(2.0)
        spec = spec_10kwh()
        with pytest.raises(ValueError, match="throughput"):
            compute_efc(spec, -1.0)
        with pytest.raises(ValueError, match="throughput"):
            compute_efc(spec, np.inf)


# ---------------------------------------------------------------------------
# 13. Efficiency and SOC window are independent assumptions
# ---------------------------------------------------------------------------


class TestEfficiencyVersusWindow:
    def test_80_percent_window_and_90_percent_efficiency_are_not_the_same_assumption(self) -> None:
        """An 80% usable window stores 0.8 x capacity; a 90% one-way efficiency
        delivers 0.9 x the energy per leg.  Both are applied here and the two
        effects stay separately visible — neither is derived from the other."""
        capacity = 5.12
        window_frac = 0.8  # soc_min 0.1 -> soc_max 0.9
        eta = 0.9
        spec = spec_10kwh(
            capacity_kwh=capacity,
            power_kw=6.0,
            charge_efficiency=eta,
            discharge_efficiency=eta,
            soc_min_frac=0.1,
            soc_max_frac=0.9,
            initial_soc_frac=0.1,
        )
        assert spec.usable_window_kwh == pytest.approx(capacity * window_frac)
        assert spec.charge_efficiency == pytest.approx(eta)
        # a 90% efficiency is not an 80% window, and filling the window is not 90%
        assert spec.usable_window_kwh / capacity != pytest.approx(eta)

        # charge exactly the window's worth of AC energy: the SOC rises by eta x
        # that, i.e. it does NOT reach the ceiling -> window and loss are distinct
        h = horizon([0.2, 0.6], [10.0, 10.0])
        ac_in = spec.usable_window_kwh
        sched = simulate(spec, h, [ac_in, 0.0], [0.0, ac_in * eta * eta])
        seed = capacity * 0.1
        assert sched.soc_kwh[0] - seed == pytest.approx(ac_in * eta)  # efficiency leg
        assert sched.soc_kwh[0] < spec.soc_bound_kwh("max") + 1e-12
        assert sched.soc_kwh[-1] == pytest.approx(seed)  # window closed
        assert sched.throughput_kwh == pytest.approx(ac_in * eta * eta)  # round trip AC
        # EFC is DC-referenced: DC throughput = AC / eta_d = ac_in * eta, so
        # EFC = ac_in * eta / ac_in = eta, not eta*eta.
        assert sched.efc == pytest.approx(eta)

    def test_window_change_moves_only_the_denominator(self) -> None:
        wide = spec_10kwh(capacity_kwh=5.12, soc_min_frac=0.0, soc_max_frac=1.0)
        narrow = spec_10kwh(capacity_kwh=5.12, soc_min_frac=0.4, soc_max_frac=0.6)
        assert wide.capacity_kwh == narrow.capacity_kwh  # nameplate untouched
        assert wide.usable_window_kwh == pytest.approx(5.12)
        assert narrow.usable_window_kwh == pytest.approx(5.12 * 0.2)
        # same throughput, EFC differs by the window ratio only
        assert compute_efc(wide, 1.0) == pytest.approx(1.0 / 5.12)
        assert compute_efc(narrow, 1.0) == pytest.approx(1.0 / (5.12 * 0.2))
        assert compute_efc(narrow, 1.0) == pytest.approx(compute_efc(wide, 1.0) * 5.0)

    def test_efficiency_change_moves_energy_not_the_metered_flows(self) -> None:
        """Same metered AC flows, different internal energy: efficiency is inside
        the battery, the window is a storage limit — independent assumptions."""
        lossless = spec_10kwh(capacity_kwh=5.12, power_kw=6.0, initial_soc_frac=0.5)
        lossy = spec_10kwh(
            capacity_kwh=5.12,
            power_kw=6.0,
            charge_efficiency=0.9,
            discharge_efficiency=0.9,
            initial_soc_frac=0.5,
        )
        assert lossless.usable_window_kwh == pytest.approx(lossy.usable_window_kwh)
        h = horizon([0.3, 0.3], [10.0, 10.0])
        charge = np.array([1.0, 0.0])
        discharge = np.array([0.0, 1.0])
        a = simulate(lossless, h, charge, discharge)  # closes its own cycle
        b = simulate(lossy, h, charge, discharge, enforce_terminal_soc=False)
        # identical metered flows -> identical grid and identical cost
        assert np.allclose(a.grid_kwh, b.grid_kwh)
        assert a.cost_eur == pytest.approx(b.cost_eur)
        assert a.throughput_kwh == pytest.approx(b.throughput_kwh)
        # ...yet the energy actually stored differs by eta_c, so the lossy plan
        # cannot return to its seed SOC: it is caught by the terminal rule
        delta_a = a.soc_kwh[0] - lossless.soc_bound_kwh("init")
        delta_b = b.soc_kwh[0] - lossy.soc_bound_kwh("init")
        assert delta_b == pytest.approx(delta_a * 0.9)
        with pytest.raises(BatteryPhysicsError, match="terminal SOC"):
            simulate(lossy, h, charge, discharge)
        # the EFC *denominator* (usable DC window) is untouched by the window
        # assumption, but for equal AC delivery the lossy battery moved more DC
        # coulombs, so its EFC is exactly 1/eta_d higher than the lossless one.
        assert compute_efc(lossy, 1.0) == pytest.approx(compute_efc(lossless, 1.0) / 0.9)


# ---------------------------------------------------------------------------
# Spec validation
# ---------------------------------------------------------------------------


class TestSpecValidation:
    def test_valid_spec_accepts(self) -> None:
        validate_spec(spec_10kwh())

    @pytest.mark.parametrize(
        "override",
        [
            dict(soc_min_frac=0.8, soc_max_frac=0.2),  # inverted window
            dict(soc_min_frac=0.5, soc_max_frac=0.5),  # empty window
            dict(soc_max_frac=1.4),  # outside [0, 1]
            dict(soc_min_frac=-0.1),
            dict(capacity_kwh=0.0),
            dict(capacity_kwh=-5.0),
            dict(power_kw=0.0),
            dict(discharge_efficiency=0.0),
            dict(initial_soc_frac=0.95, soc_max_frac=0.9),  # start above ceiling
            dict(initial_soc_frac=0.05, soc_min_frac=0.1),  # start below floor
            dict(terminal_soc_frac=0.95, soc_max_frac=0.9),  # target unreachable
            dict(capacity_kwh=np.nan),
            dict(power_kw=np.inf),
        ],
    )
    def test_malformed_specs_raise(self, override: dict) -> None:
        with pytest.raises(ValueError):
            validate_spec(spec_10kwh(**override))

    def test_malformed_spec_reaches_simulate_and_oracle(self) -> None:
        """The spec is not validated at construction, so bad values get caught."""
        inverted = spec_10kwh(soc_min_frac=0.8, soc_max_frac=0.2)
        h = horizon([0.1], [1.0])
        with pytest.raises(ValueError, match="soc_min_frac < soc_max_frac"):
            check_feasible(inverted, h, [0.0], [0.0])
        with pytest.raises(ValueError, match="soc_min_frac < soc_max_frac"):
            simulate(inverted, h, [0.0], [0.0])

    def test_dataclass_is_frozen_and_coerces_types(self) -> None:
        spec = spec_10kwh(capacity_kwh=5)
        assert isinstance(spec.capacity_kwh, float)
        with pytest.raises(dataclasses.FrozenInstanceError):
            spec.capacity_kwh = 9.0  # type: ignore[misc]

    def test_effective_terminal_resolves_none_to_initial(self) -> None:
        assert spec_10kwh(initial_soc_frac=0.42).effective_terminal_soc_frac == pytest.approx(0.42)
        assert spec_10kwh(
            initial_soc_frac=0.42, terminal_soc_frac=0.7
        ).effective_terminal_soc_frac == pytest.approx(0.7)

    def test_unknown_soc_bound_name_raises(self) -> None:
        with pytest.raises(ValueError, match="unknown SOC bound"):
            spec_10kwh().soc_bound_kwh("sideways")


# ---------------------------------------------------------------------------
# Schedule record
# ---------------------------------------------------------------------------


class TestScheduleRecord:
    def test_solver_fields_default_for_traced_plans(self) -> None:
        spec = spec_10kwh(charge_efficiency=0.9, discharge_efficiency=0.9)
        h = horizon([0.1, 0.5], [5.0, 5.0])
        sched = simulate(spec, h, [1.0, 0.0], [0.0, 0.81])
        assert sched.solver_status == "not_applicable"
        assert sched.solver_gap is None
        assert sched.bound_kind == "none"
        assert sched.is_certified is False

    def test_achieved_soc_extrema_include_the_initial_state(self) -> None:
        spec = spec_10kwh(charge_efficiency=0.9, discharge_efficiency=0.9)
        h = horizon([0.1, 0.5], [5.0, 5.0])
        sched = simulate(spec, h, [1.0, 0.0], [0.0, 0.81])
        assert sched.soc_min_frac == pytest.approx(0.0)  # starts and ends at 0
        assert sched.soc_max_frac == pytest.approx(0.09)  # 0.9 kWh of 10 kWh
        flat = simulate(spec, h, [0.0, 0.0], [0.0, 0.0])
        assert flat.soc_min_frac == pytest.approx(flat.soc_max_frac) == pytest.approx(0.0)

    def test_schedule_validates_equal_lengths(self) -> None:
        with pytest.raises(ValueError, match="expected"):
            Schedule(
                charge_kwh=np.zeros(2),
                discharge_kwh=np.zeros(3),
                soc_kwh=np.zeros(2),
                grid_kwh=np.zeros(2),
                cost_eur=0.0,
                efc=0.0,
                throughput_kwh=0.0,
                soc_min_frac=0.0,
                soc_max_frac=0.0,
            )

    def test_savings_vs_reports_losses_as_negative(self) -> None:
        spec = spec_10kwh()
        h = horizon([1.0, 2.0], [1.0, 1.0])
        baseline = simulate(spec, h, [0.0, 0.0], [0.0, 0.0])
        assert baseline.cost_eur == pytest.approx(3.0)
        cheap = dataclasses.replace(baseline, cost_eur=1.5)
        dear = dataclasses.replace(baseline, cost_eur=4.25)
        assert cheap.savings_vs(baseline) == pytest.approx(1.5)
        assert dear.savings_vs(baseline) == pytest.approx(-1.25)
        keys = set(dear.as_dict())
        assert {"cost_eur", "efc", "throughput_kwh", "solver_status", "bound_kind"} <= keys
        assert dear.as_dict()["solver_status"] == "not_applicable"

    def test_violation_messages_are_aggregated_not_per_interval(self) -> None:
        """A multi-week horizon must not produce one line per interval."""
        n = 96 * 7
        spec = spec_10kwh(soc_max_frac=0.5)  # ceiling = 5 kWh, power 1 kW * 1 h
        h = horizon([0.1] * n, [30.0] * n)
        charge = np.ones(n)  # overshoots the ceiling and never returns
        discharge = np.zeros(n)
        violations = check_feasible(spec, h, charge, discharge)
        assert len(violations) <= 3
        assert any("more)" in v for v in violations)
        assert all(v.count("t=") <= 3 for v in violations)


# ---------------------------------------------------------------------------
# Terminal-inventory accounting fields on Schedule
# ---------------------------------------------------------------------------


class TestScheduleInventoryFields:
    def test_simulate_populates_initial_and_terminal_soc_kwh(self) -> None:
        spec = spec_10kwh(initial_soc_frac=0.3, charge_efficiency=0.9, discharge_efficiency=0.9)
        h = horizon([0.1, 0.5], [5.0, 5.0])
        sched = simulate(spec, h, [1.0, 0.0], [0.0, 0.81])
        assert sched.initial_soc_kwh == pytest.approx(10.0 * 0.3)
        assert sched.terminal_soc_kwh == pytest.approx(10.0 * 0.3)  # closed cycle

    def test_simulate_default_inventory_is_zero(self) -> None:
        spec = spec_10kwh(charge_efficiency=0.9, discharge_efficiency=0.9)
        h = horizon([0.1, 0.5], [5.0, 5.0])
        # charge 1.0 kWh (SOC +0.9), then discharge 0.81 AC (= 0.9 DC) back to seed
        sched = simulate(spec, h, [1.0, 0.0], [0.0, 0.81])
        assert sched.meter_cost_eur == pytest.approx(sched.cost_eur)
        assert sched.inventory_adjustment_eur == pytest.approx(0.0)
        assert sched.terminal_value_eur_per_kwh == pytest.approx(0.0)
        assert sched.cost_lower_bound_eur is None
        assert sched.cost_upper_bound_eur is None
        assert sched.solver_time_limit_s is None

    def test_bare_schedule_defaults_are_consistent(self) -> None:
        """A legacy ``Schedule`` built without the new fields stays consistent:
        ``meter_cost_eur`` defaults to ``cost_eur`` (no inventory adjustment)."""
        sched = Schedule(
            charge_kwh=np.zeros(2),
            discharge_kwh=np.zeros(2),
            soc_kwh=np.zeros(2),
            grid_kwh=np.zeros(2),
            cost_eur=5.0,
            efc=0.0,
            throughput_kwh=0.0,
            soc_min_frac=0.0,
            soc_max_frac=0.0,
        )
        assert sched.meter_cost_eur == pytest.approx(5.0)
        assert sched.inventory_adjustment_eur == pytest.approx(0.0)
        assert sched.cost_eur == pytest.approx(5.0)


# ---------------------------------------------------------------------------
# Frozen-array defensive copies
# ---------------------------------------------------------------------------


class TestFrozenArrays:
    def test_horizon_arrays_are_read_only_copies(self) -> None:
        prices = np.array([0.1, 0.2, 0.3])
        load = np.array([1.0, 2.0, 3.0])
        usable = np.array([True, False, True])
        h = Horizon(HOUR, prices, load, usable)
        # mutating the caller's arrays must not change the Horizon
        prices[0] = 999.0
        load[0] = 999.0
        usable[0] = False
        assert h.prices[0] == pytest.approx(0.1)
        assert h.load_kwh[0] == pytest.approx(1.0)
        assert h.usable[0] is np.True_
        # and the Horizon's own arrays are read-only
        with pytest.raises(ValueError, match="read-only"):
            h.prices[0] = 5.0
        with pytest.raises(ValueError, match="read-only"):
            h.load_kwh[0] = 5.0

    def test_schedule_arrays_are_read_only_copies(self) -> None:
        charge = np.array([1.0, 0.0])
        discharge = np.array([0.0, 0.81])
        soc = np.array([0.9, 0.0])
        grid = np.array([2.0, 0.19])
        sched = Schedule(
            charge_kwh=charge,
            discharge_kwh=discharge,
            soc_kwh=soc,
            grid_kwh=grid,
            cost_eur=0.276,
            efc=0.0,
            throughput_kwh=0.81,
            soc_min_frac=0.0,
            soc_max_frac=0.09,
        )
        # mutating the caller's arrays must not change the Schedule
        charge[0] = 999.0
        assert sched.charge_kwh[0] == pytest.approx(1.0)
        # and the Schedule's own arrays are read-only
        with pytest.raises(ValueError, match="read-only"):
            sched.charge_kwh[0] = 5.0
        with pytest.raises(ValueError, match="read-only"):
            sched.discharge_kwh[0] = 5.0
