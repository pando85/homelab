"""Economic sensitivity analysis — throughput cost and terminal value sweeps.

Pure numeric module: no filesystem, network, or Parquet access.  Consumes
:class:`~energy_baseline.battery.BatterySpec`, :class:`~energy_baseline.battery.Horizon`
and :class:`~energy_baseline.battery.Schedule` objects and returns plain dicts.

Purpose
-------
Before hardware is installed, the user needs to understand how sensitive the
oracle's dispatch is to two economic parameters that are not yet known:

1. **Terminal SOC value** (``terminal_value_eur_per_kwh``): what is the energy
   left in the battery at the end of the horizon worth?  This is the valuation
   that prevents a free-terminal oracle from emptying the battery at midnight.
2. **Throughput cost** (``throughput_cost_eur_per_kwh``): what is the economic
   cost of cycling energy through the battery?  This is an explicit convention
   (degradation valued as proportional to AC energy throughput), not a physical
   aging model.  No real degradation rates are invented here; all ranges are
   user-supplied hypotheticals.

The module provides:

* :func:`score_fixed_schedule` — score an existing schedule under a given
  throughput cost and terminal value.  The schedule is not re-optimized; this
  is pure rescoring.
* :func:`optimize_with_throughput` — run the oracle with a throughput cost and
  return the optimized schedule plus its degradation-adjusted objective.
* :func:`sweep_sensitivity` — sweep both parameters over user-supplied arrays
  and return a table of results.  Each row is one ``(planning_terminal_value,
  throughput_cost)`` combination.  The optimizer is re-run for each pair
  (optimized responses, not merely rescoring).  In ``terminal_mode="fixed"``
  the planning terminal value has no effect on the schedule, so only a single
  planning value is allowed; use ``terminal_mode="free"`` for terminal-value
  sensitivity.
* :func:`compare_schedules` — compare multiple named schedules under a single
  ``(terminal_value, throughput_cost)`` pair.  Useful for comparing the oracle
  against a causal policy or a no-battery baseline on the same degradation-
  adjusted basis.

Accounting
----------
For a schedule with meter cost ``M``, throughput ``T = sum(charge) + sum(discharge)``,
terminal SOC ``S_term``, initial SOC ``S_init``:

* **Meter bill**: ``M`` (the actual pretax energy cost at the meter).
* **Inventory adjustment**: ``-V * (S_term - S_init)`` where ``V`` is the
  terminal value.  Zero for a fixed-terminal plan.
* **Throughput penalty**: ``tp * T`` where ``tp`` is the throughput cost.
* **Degradation-adjusted objective**: ``M + inventory_adjustment + throughput_penalty``.

The MILP optimizer (when ``throughput_cost > 0``) minimizes the degradation-
adjusted objective.  The reported ``Schedule.cost_eur`` stays the meter bill
plus inventory adjustment (unchanged semantics); the throughput penalty is
recoverable as ``throughput_cost * total_ac_throughput_kwh``.

All ranges are user-supplied hypotheticals.  No real degradation rates are
invented or assumed.  The module labels every monetary constant as
hypothetical in its docstrings and return values.
"""

from __future__ import annotations

import math
from typing import Any

import numpy as np

from .battery import BatterySpec, Horizon, Schedule
from .oracle import solve_oracle

__all__ = [
    "break_even_throughput_cost",
    "compare_schedules",
    "optimize_with_throughput",
    "pairwise_break_even",
    "score_fixed_schedule",
    "sweep_sensitivity",
]


def _validate_throughput_cost(throughput_cost_eur_per_kwh: float) -> float:
    """Validate and return the throughput cost as a float.

    Rejects booleans: ``True``/``False`` are ``int``/``float`` subclasses in
    Python, but a bool throughput cost is a programming error, not a value.
    """
    if isinstance(throughput_cost_eur_per_kwh, bool):
        raise ValueError(
            f"throughput_cost_eur_per_kwh must be a number, got bool "
            f"{throughput_cost_eur_per_kwh!r}"
        )
    tp = float(throughput_cost_eur_per_kwh)
    if not math.isfinite(tp):
        raise ValueError(
            f"throughput_cost_eur_per_kwh must be finite, got {throughput_cost_eur_per_kwh!r}"
        )
    if tp < 0.0:
        raise ValueError(
            f"throughput_cost_eur_per_kwh must be >= 0, got {throughput_cost_eur_per_kwh}"
        )
    return tp


def _validate_terminal_value(terminal_value_eur_per_kwh: float) -> float:
    """Validate and return the terminal value as a float.

    Rejects booleans (see :func:`_validate_throughput_cost`).
    """
    if isinstance(terminal_value_eur_per_kwh, bool):
        raise ValueError(
            f"terminal_value_eur_per_kwh must be a number, got bool {terminal_value_eur_per_kwh!r}"
        )
    v = float(terminal_value_eur_per_kwh)
    if not math.isfinite(v):
        raise ValueError(
            f"terminal_value_eur_per_kwh must be finite, got {terminal_value_eur_per_kwh!r}"
        )
    return v


def _total_ac_throughput_kwh(schedule: Schedule) -> float:
    """Total AC energy throughput: sum(charge) + sum(discharge).

    Distinct from Schedule.throughput_kwh which is discharge-only.
    """
    return float(np.asarray(schedule.charge_kwh).sum()) + float(
        np.asarray(schedule.discharge_kwh).sum()
    )


def score_fixed_schedule(
    schedule: Schedule,
    throughput_cost_eur_per_kwh: float,
    terminal_value_eur_per_kwh: float = 0.0,
) -> dict[str, Any]:
    """Score a fixed schedule under a given throughput cost and terminal value.

    The schedule is **not** re-optimized; this is pure rescoring.  Useful for
    comparing a causal policy or no-battery baseline against the oracle on the
    same degradation-adjusted basis.

    Args:
        schedule: An existing schedule (from the oracle, a causal policy, etc.).
        throughput_cost_eur_per_kwh: Per-kWh penalty on AC energy throughput.
            Must be finite and non-negative.  All values are user-supplied
            hypotheticals; no real degradation rates are assumed.
        terminal_value_eur_per_kwh: EUR/DC-kWh value of the terminal inventory.
            Must be finite.  Default ``0.0`` (cash-flow objective).

    Returns:
        A dict with:

        * ``meter_cost_eur``: the actual pretax meter bill.
        * ``total_ac_throughput_kwh``: total AC energy throughput (charge + discharge).
        * ``efc``: equivalent full cycles (DC-referenced, from the schedule).
        * ``end_soc_kwh``: terminal SOC.
        * ``initial_soc_kwh``: initial SOC.
        * ``inventory_adjustment_eur``: ``-V * (terminal - initial)``.
        * ``throughput_penalty_eur``: ``tp * throughput``.
        * ``degradation_adjusted_objective_eur``: meter + inventory + throughput.
        * ``note``: labels all values as hypothetical.

    Raises:
        ValueError: If the throughput cost or terminal value is not finite, or
            the throughput cost is negative.
    """
    tp = _validate_throughput_cost(throughput_cost_eur_per_kwh)
    v = _validate_terminal_value(terminal_value_eur_per_kwh)

    meter_cost = float(schedule.meter_cost_eur)
    total_ac_throughput = _total_ac_throughput_kwh(schedule)
    efc = float(schedule.efc)
    end_soc = float(schedule.terminal_soc_kwh)
    init_soc = float(schedule.initial_soc_kwh)
    inventory_adj = -v * (end_soc - init_soc)
    throughput_penalty = tp * total_ac_throughput
    adjusted = meter_cost + inventory_adj + throughput_penalty

    return {
        "meter_cost_eur": meter_cost,
        "total_ac_throughput_kwh": total_ac_throughput,
        "efc": efc,
        "end_soc_kwh": end_soc,
        "initial_soc_kwh": init_soc,
        "inventory_adjustment_eur": inventory_adj,
        "throughput_penalty_eur": throughput_penalty,
        "degradation_adjusted_objective_eur": adjusted,
        "note": (
            "all monetary values are user-supplied hypotheticals; no real degradation "
            "rates are assumed. The throughput cost is an explicit economic convention "
            "(degradation valued as proportional to AC energy throughput), not a "
            "physical aging model"
        ),
    }


def optimize_with_throughput(
    spec: BatterySpec,
    horizon: Horizon,
    throughput_cost_eur_per_kwh: float,
    *,
    terminal_mode: str = "fixed",
    terminal_value_eur_per_kwh: float | None = None,
    time_limit_s: float = 60.0,
    mip_rel_gap: float = 1e-4,
    mip_abs_gap: float = 1e-6,
    floor_kwh: float | None = None,
) -> tuple[Schedule, dict[str, Any]]:
    """Run the oracle with a throughput cost and return the optimized schedule.

    The MILP objective includes the throughput penalty, so the solver accounts
    for degradation when choosing the dispatch.  The reported ``Schedule.cost_eur``
    stays the meter bill plus inventory adjustment (unchanged semantics); the
    throughput penalty is reported separately.

    Args:
        spec: Battery physical parameters.
        horizon: Prices, gross load, the usable mask and the optional import
            ceiling for the horizon.
        throughput_cost_eur_per_kwh: Per-kWh penalty on AC energy throughput.
            Must be finite and non-negative.  All values are user-supplied
            hypotheticals.
        terminal_mode: ``"fixed"`` (default) or ``"free"``.  See
            :func:`energy_baseline.oracle.solve_oracle`.
        terminal_value_eur_per_kwh: EUR/DC-kWh value of the terminal inventory.
            Required when ``terminal_mode="free"``.
        time_limit_s: Wall-clock limit for the solver.
        mip_rel_gap: Relative optimality tolerance.
        mip_abs_gap: Absolute optimality tolerance.
        floor_kwh: Override for the free-mode SOC floor.

    Returns:
        ``(schedule, info)`` where ``schedule`` is the optimized
        :class:`~energy_baseline.battery.Schedule` and ``info`` is a dict with:

        * ``throughput_cost_eur_per_kwh``: the value used.
        * ``total_ac_throughput_kwh``: total AC energy throughput.
        * ``throughput_penalty_eur``: ``tp * throughput``.
        * ``meter_cost_eur``: the meter bill.
        * ``degradation_adjusted_objective_eur``: meter + inventory + throughput.

    Raises:
        ValueError: If the throughput cost is not finite or negative.
        OracleInfeasibleError: If no feasible schedule exists.
        OracleNoIncumbentError: If the solver stopped without any feasible plan.
    """
    tp = _validate_throughput_cost(throughput_cost_eur_per_kwh)

    sched = solve_oracle(
        spec,
        horizon,
        time_limit_s=time_limit_s,
        mip_rel_gap=mip_rel_gap,
        mip_abs_gap=mip_abs_gap,
        terminal_mode=terminal_mode,
        terminal_value_eur_per_kwh=terminal_value_eur_per_kwh,
        floor_kwh=floor_kwh,
        throughput_cost_eur_per_kwh=tp,
    )

    total_ac_throughput = _total_ac_throughput_kwh(sched)
    meter_cost = float(sched.meter_cost_eur)
    inventory_adj = float(sched.inventory_adjustment_eur)
    throughput_penalty = tp * total_ac_throughput
    adjusted = meter_cost + inventory_adj + throughput_penalty

    info = {
        "throughput_cost_eur_per_kwh": tp,
        "total_ac_throughput_kwh": total_ac_throughput,
        "throughput_penalty_eur": throughput_penalty,
        "meter_cost_eur": meter_cost,
        "inventory_adjustment_eur": inventory_adj,
        "degradation_adjusted_objective_eur": adjusted,
        "note": (
            "the MILP objective includes the throughput penalty, so the solver "
            "accounts for degradation when choosing the dispatch. The reported "
            "Schedule.cost_eur stays the meter bill plus inventory adjustment; "
            "the throughput penalty is reported separately. All monetary values "
            "are user-supplied hypotheticals"
        ),
    }
    return sched, info


def _validate_comparable_schedules(
    schedule_a: Schedule,
    schedule_b: Schedule,
) -> None:
    """Raise ValueError if two schedules have different horizon lengths.

    Two schedules with different numbers of intervals are incommensurable:
    their meter costs, throughputs and terminal SOCs refer to different time
    bases and cannot be compared.  Grid identity (same ``interval_seconds``)
    is the caller's contract and is not enforced here — the ``Schedule``
    carries no interval field, so same-length schedules on different grids
    pass this check silently.
    """
    n_a = len(schedule_a.charge_kwh)
    n_b = len(schedule_b.charge_kwh)
    if n_a != n_b:
        raise ValueError(
            f"schedules have different horizon lengths ({n_a} vs {n_b} intervals); "
            f"break-even comparison requires the same horizon"
        )


def break_even_throughput_cost(
    schedule_a: Schedule,
    schedule_b: Schedule,
    terminal_value_eur_per_kwh: float = 0.0,
) -> float | None:
    """Throughput cost at which two schedules have equal degradation-adjusted cost.

    For schedules A and B with meter costs M_a, M_b, total AC throughputs T_a, T_b,
    terminal SOCs S_a, S_b, and initial SOCs I_a, I_b, the degradation-adjusted
    cost of X is ``M_X - V*(S_X - I_X) + tp * T_X``.  Solving for ``tp`` where
    the two adjusted costs are equal:

        M_a - V*(S_a - I_a) + tp*T_a == M_b - V*(S_b - I_b) + tp*T_b
        tp * (T_a - T_b) == (M_b - M_a) + V*((S_a - I_a) - (S_b - I_b))
        tp == [(M_b - M_a) + V*((S_a - I_a) - (S_b - I_b))] / (T_a - T_b)

    This is a FIXED-SCHEDULE pairwise intersection: it tells where the net
    advantage of one schedule over another (in meter cost and terminal-inventory
    value) is exactly cancelled by the per-kWh throughput charge applied to the
    incremental bidirectional AC throughput.  It is NOT a re-optimized solver
    threshold, and it is not a claim about what the optimizer would choose.

    Returns ``None`` if the denominator (T_a - T_b) is zero, meaning the two
    schedules have identical throughput and the lines either coincide or are
    parallel.

    Args:
        schedule_a: First schedule.
        schedule_b: Second schedule.  Must have the same horizon length as
            ``schedule_a`` (same number of intervals).
        terminal_value_eur_per_kwh: EUR/DC-kWh value of the terminal inventory.
            Must be finite.  Default ``0.0`` (cash-flow objective).

    Returns:
        The break-even throughput cost (EUR/kWh) at which the two schedules
        have equal degradation-adjusted cost.  May be negative, which is a
        valid mathematical result meaning no non-negative tp makes them equal
        (one schedule dominates for all tp >= 0).  Returns ``None`` if the
        denominator is zero.  Callers that care about economic meaning should
        inspect the sign: a negative break-even is a *classification*, not a
        usable penalty.

    Raises:
        ValueError: If the terminal value is not finite, or the schedules have
            different horizon lengths.
    """
    v = _validate_terminal_value(terminal_value_eur_per_kwh)
    _validate_comparable_schedules(schedule_a, schedule_b)

    m_a = float(schedule_a.meter_cost_eur)
    m_b = float(schedule_b.meter_cost_eur)
    t_a = _total_ac_throughput_kwh(schedule_a)
    t_b = _total_ac_throughput_kwh(schedule_b)
    s_a = float(schedule_a.terminal_soc_kwh)
    s_b = float(schedule_b.terminal_soc_kwh)
    i_a = float(schedule_a.initial_soc_kwh)
    i_b = float(schedule_b.initial_soc_kwh)

    denom = t_a - t_b
    if abs(denom) < 1e-12:
        return None

    numer = (m_b - m_a) + v * ((s_a - i_a) - (s_b - i_b))
    return numer / denom


def pairwise_break_even(
    schedule_a: Schedule,
    schedule_b: Schedule,
    terminal_value_eur_per_kwh: float = 0.0,
) -> dict[str, Any]:
    """Fixed-schedule pairwise break-even with sign classification.

    Computes where the degradation-adjusted costs of two **already-produced**
    schedules intersect as a function of the throughput cost.  This is a
    rescaling of a fixed pair of schedules; it is NOT a re-optimized solver
    threshold, and it does not claim what the MILP would produce if re-solved
    at that throughput cost.

    The breakdown is::

        tp = baseline_net_advantage / incremental_ac_throughput

    where

        baseline_net_advantage = (M_b - M_a) + V*((S_a - I_a) - (S_b - I_b))
        incremental_ac_throughput = T_a - T_b

    Returns a dict with all components and a sign classification::

        {
            "baseline_net_advantage_eur": (M_b - M_a) + V*((S_a - I_a) - (S_b - I_b)),
            "incremental_ac_throughput_kwh": T_a - T_b,
            "break_even_throughput_cost_eur_per_kwh": float | None,
            "classification": "non_negative_crossover" |
                              "negative_no_crossover_on_tp_ge_0" |
                              "no_crossover_parallel_lines",
            "terminal_value_eur_per_kwh": v,
        }

    The classification is what makes the result interpretable:

    * ``non_negative_crossover``: a real break-even exists on the economic
      axis (tp >= 0).  The schedule that is cheaper at tp = 0 loses to the
      other once tp exceeds the value.
    * ``negative_no_crossover_on_tp_ge_0``: the algebraic intersection is
      at a negative throughput cost.  One schedule dominates for every
      admissible (non-negative) tp.  Reporting the raw number would be
      economically meaningless without this label.
    * ``no_crossover_parallel_lines``: identical AC throughput; the lines
      either coincide (the break-even is every tp) or are parallel (none).
      Numerical value is None.

    Args:
        schedule_a: First schedule.
        schedule_b: Second schedule.
        terminal_value_eur_per_kwh: EUR/DC-kWh value of terminal inventory,
            finite.  Default ``0.0``.

    Returns:
        Dict with the breakdown above.
    """
    v = _validate_terminal_value(terminal_value_eur_per_kwh)
    _validate_comparable_schedules(schedule_a, schedule_b)

    m_a = float(schedule_a.meter_cost_eur)
    m_b = float(schedule_b.meter_cost_eur)
    t_a = _total_ac_throughput_kwh(schedule_a)
    t_b = _total_ac_throughput_kwh(schedule_b)
    s_a = float(schedule_a.terminal_soc_kwh)
    s_b = float(schedule_b.terminal_soc_kwh)
    i_a = float(schedule_a.initial_soc_kwh)
    i_b = float(schedule_b.initial_soc_kwh)

    baseline_net_advantage = (m_b - m_a) + v * ((s_a - i_a) - (s_b - i_b))
    incremental_ac_throughput = t_a - t_b

    if abs(incremental_ac_throughput) < 1e-12:
        return {
            "baseline_net_advantage_eur": baseline_net_advantage,
            "incremental_ac_throughput_kwh": incremental_ac_throughput,
            "break_even_throughput_cost_eur_per_kwh": None,
            "classification": "no_crossover_parallel_lines",
            "terminal_value_eur_per_kwh": v,
        }

    tp = baseline_net_advantage / incremental_ac_throughput
    classification = "non_negative_crossover" if tp >= 0.0 else "negative_no_crossover_on_tp_ge_0"
    return {
        "baseline_net_advantage_eur": baseline_net_advantage,
        "incremental_ac_throughput_kwh": incremental_ac_throughput,
        "break_even_throughput_cost_eur_per_kwh": tp,
        "classification": classification,
        "terminal_value_eur_per_kwh": v,
    }


def sweep_sensitivity(
    spec: BatterySpec,
    horizon: Horizon,
    *,
    planning_terminal_values: list[float] | np.ndarray,
    throughput_costs: list[float] | np.ndarray,
    scoring_terminal_value_eur_per_kwh: float = 0.0,
    terminal_mode: str = "free",
    time_limit_s: float = 60.0,
    mip_rel_gap: float = 1e-4,
    mip_abs_gap: float = 1e-6,
    floor_kwh: float | None = None,
) -> list[dict[str, Any]]:
    """Sweep planning terminal values and throughput costs over user-supplied arrays.

    For each (planning_terminal_value, throughput_cost) pair, the optimizer is
    re-run with that planning terminal value and throughput cost.  Each optimized
    schedule is then scored under the **scoring_terminal_value** (constant across
    all arms) to enable fair comparison.

    The planning terminal value is the coefficient inside the MILP objective that
    prevents the battery from emptying at midnight.  The scoring terminal value
    is the coefficient used to compute the reported degradation-adjusted cost,
    and is held constant across all arms so the comparison is on the same basis.

    Args:
        spec: Battery physical parameters.
        horizon: Prices, gross load, the usable mask and the optional import
            ceiling for the horizon.
        planning_terminal_values: Array of planning terminal values (EUR/DC-kWh)
            to sweep.  Each must be finite.  All values are user-supplied
            hypotheticals.  This is the coefficient inside the MILP objective.
        throughput_costs: Array of throughput costs (EUR/kWh) to sweep.  Each
            must be finite and non-negative.  All values are user-supplied
            hypotheticals.
        scoring_terminal_value_eur_per_kwh: The terminal value used to score all
            optimized schedules.  Must be finite.  Default ``0.0`` (cash-flow
            objective).  Held constant across all arms for fair comparison.
        terminal_mode: ``"fixed"`` or ``"free"`` (default).
        time_limit_s: Wall-clock limit for each solver call.
        mip_rel_gap: Relative optimality tolerance.
        mip_abs_gap: Absolute optimality tolerance.
        floor_kwh: Override for the free-mode SOC floor.

    Returns:
        A list of dicts, one per (planning_terminal_value, throughput_cost) pair.
        Each dict contains:

        * ``planning_terminal_value_eur_per_kwh``: the planning terminal value
          used inside the MILP objective.
        * ``throughput_cost_eur_per_kwh``: the throughput cost used for optimization.
        * ``scoring_terminal_value_eur_per_kwh``: the scoring terminal value
          (constant across all arms).
        * ``meter_cost_eur``: the meter bill.
        * ``total_ac_throughput_kwh``: total AC energy throughput.
        * ``efc``: equivalent full cycles.
        * ``end_soc_kwh``: terminal SOC.
        * ``initial_soc_kwh``: initial SOC.
        * ``planning_inventory_adjustment_eur``: ``-planning_V * (terminal - initial)``.
        * ``scoring_inventory_adjustment_eur``: ``-scoring_V * (terminal - initial)``.
        * ``throughput_penalty_eur``: ``tp * throughput``.
        * ``degradation_adjusted_objective_eur``: meter + scoring_inventory + throughput.
        * ``solver_status``: from the optimizer.
        * ``is_certified``: from the optimizer.
        * ``note``: labels all values as hypothetical.

    Raises:
        ValueError: If any planning terminal value or the scoring terminal value
            is not finite, or any throughput cost is not finite or negative.
    """
    # Bool rejection before np.asarray (which silently coerces True→1.0).
    if any(isinstance(v, bool) for v in planning_terminal_values):
        raise ValueError(
            "planning_terminal_values contains a bool; each value must be a number, not True/False"
        )
    if any(isinstance(v, bool) for v in throughput_costs):
        raise ValueError(
            "throughput_costs contains a bool; each value must be a number, not True/False"
        )

    ptv_arr = np.asarray(planning_terminal_values, dtype=float).reshape(-1)
    tp_arr = np.asarray(throughput_costs, dtype=float).reshape(-1)
    scoring_v = _validate_terminal_value(scoring_terminal_value_eur_per_kwh)

    for ptv in ptv_arr:
        if not math.isfinite(ptv):
            raise ValueError(f"planning_terminal_values contains a non-finite value: {ptv}")
    for tp in tp_arr:
        _validate_throughput_cost(tp)

    # In fixed mode, terminal_value_for_solve is always None, so the planning
    # terminal value has zero effect on the schedule.  Sweeping multiple values
    # would produce N×M identical plans labeled as distinct — a waste of solver
    # calls and a misleading artifact.
    if terminal_mode == "fixed" and len(ptv_arr) > 1:
        raise ValueError(
            "planning_terminal_values has no effect in fixed mode; use "
            "terminal_mode='free' for terminal-value sensitivity"
        )

    results: list[dict[str, Any]] = []

    # Reoptimize for each (planning_terminal_value, throughput_cost) pair.
    for ptv in ptv_arr:
        for tp in tp_arr:
            # Determine the terminal_value to pass to solve_oracle.
            # In "free" mode, we use the planning terminal value.
            # In "fixed" mode, terminal_value must be None.
            terminal_value_for_solve = ptv if terminal_mode == "free" else None

            sched = solve_oracle(
                spec,
                horizon,
                time_limit_s=time_limit_s,
                mip_rel_gap=mip_rel_gap,
                mip_abs_gap=mip_abs_gap,
                terminal_mode=terminal_mode,
                terminal_value_eur_per_kwh=terminal_value_for_solve,
                floor_kwh=floor_kwh,
                throughput_cost_eur_per_kwh=tp,
            )

            # Score the optimized schedule under the scoring terminal value.
            meter_cost = float(sched.meter_cost_eur)
            total_ac_throughput = _total_ac_throughput_kwh(sched)
            efc = float(sched.efc)
            end_soc = float(sched.terminal_soc_kwh)
            init_soc = float(sched.initial_soc_kwh)

            # Planning inventory adjustment (what was used inside the MILP).
            planning_inventory_adj = -ptv * (end_soc - init_soc)

            # Scoring inventory adjustment (constant across all arms).
            scoring_inventory_adj = -scoring_v * (end_soc - init_soc)

            # Throughput penalty.
            throughput_penalty = tp * total_ac_throughput

            # Degradation-adjusted objective (using scoring terminal value).
            adjusted = meter_cost + scoring_inventory_adj + throughput_penalty

            row = {
                "planning_terminal_value_eur_per_kwh": float(ptv),
                "throughput_cost_eur_per_kwh": tp,
                "scoring_terminal_value_eur_per_kwh": scoring_v,
                "meter_cost_eur": meter_cost,
                "total_ac_throughput_kwh": total_ac_throughput,
                "efc": efc,
                "end_soc_kwh": end_soc,
                "initial_soc_kwh": init_soc,
                "planning_inventory_adjustment_eur": planning_inventory_adj,
                "scoring_inventory_adjustment_eur": scoring_inventory_adj,
                "throughput_penalty_eur": throughput_penalty,
                "degradation_adjusted_objective_eur": adjusted,
                "solver_status": str(sched.solver_status),
                "is_certified": bool(sched.is_certified),
                "note": (
                    "all monetary values are user-supplied hypotheticals; no real "
                    "degradation rates are assumed. The planning_terminal_value is the "
                    "coefficient inside the MILP objective; the scoring_terminal_value "
                    "is held constant across all arms for fair comparison"
                ),
            }
            results.append(row)

    return results


def compare_schedules(
    schedules: dict[str, Schedule],
    throughput_cost_eur_per_kwh: float,
    terminal_value_eur_per_kwh: float = 0.0,
) -> dict[str, dict[str, Any]]:
    """Compare multiple named schedules under a single economic pair.

    Useful for comparing the oracle against a causal policy or a no-battery
    baseline on the same degradation-adjusted basis.

    Args:
        schedules: A dict mapping schedule names to :class:`Schedule` objects.
            All schedules must have the same horizon length (number of
            intervals); schedules from different horizons are incommensurable.
            Grid identity (same ``interval_seconds``) is the caller's contract
            and is not enforced here.
        throughput_cost_eur_per_kwh: Per-kWh penalty on AC energy throughput.
        terminal_value_eur_per_kwh: EUR/DC-kWh value of the terminal inventory.

    Returns:
        A dict mapping schedule names to their scored dicts (from
        :func:`score_fixed_schedule`).

    Raises:
        ValueError: If the throughput cost or terminal value is invalid, or if
            the schedules have different horizon lengths.
    """
    tp = _validate_throughput_cost(throughput_cost_eur_per_kwh)
    v = _validate_terminal_value(terminal_value_eur_per_kwh)

    # Validate all schedules share the same horizon length.
    lengths = {name: len(sched.charge_kwh) for name, sched in schedules.items()}
    unique_lengths = set(lengths.values())
    if len(unique_lengths) > 1:
        raise ValueError(
            f"schedules have different horizon lengths: {lengths}; "
            f"compare_schedules requires all schedules to cover the same horizon"
        )

    return {name: score_fixed_schedule(sched, tp, v) for name, sched in schedules.items()}
