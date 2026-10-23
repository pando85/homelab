"""Perfect-information oracle — offline benchmark using scipy.optimize.milp.

STUB: signatures and docstrings only.  Implementation is deferred to a
later agent.
"""

from __future__ import annotations


def solve_oracle(
    *,
    gross_kwh: list[float],
    prices_eur_per_kwh: list[float],
    capacity_kwh: float,
    power_kw: float,
    round_trip_efficiency: float,
    soc_window_min: float,
    soc_window_max: float,
    import_limit_kw: float | None = None,
    initial_soc: float | None = None,
    terminal_soc: float | None = None,
    time_limit_sec: float = 120.0,
) -> dict:
    """Compute the perfect-information cost-minimizing schedule.

    Uses ``scipy.optimize.milp`` (HiGHS) to find the cost-minimizing
    charge/discharge schedule subject to physical SOC and power constraints.
    The incumbent feasible solution provides an upper bound on optimal cost;
    the dual provides a lower bound.

    Returns:
        Dictionary with cost_eur, solver_gap, solver_status, bound_kind,
        is_certified, soc_trajectory, and grid_flows.

    Raises:
        NotImplementedError: Always — this is a stub.
    """
    raise NotImplementedError(
        "oracle solver is not yet implemented — see ADR-0003 §strategy comparison"
    )
