"""Battery physical simulator — SOC recurrence with signed flows.

STUB: signatures and docstrings only.  Implementation is deferred to a
later agent.
"""

from __future__ import annotations


def simulate_battery(
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
) -> dict:
    """Simulate battery dispatch over a sequence of 15-min intervals.

    Respects physical constraints: energy conservation per interval,
    SOC bounds never violated, round-trip losses on both charge and
    discharge paths, one binary mode per interval (charge OR discharge).

    Returns:
        Dictionary with grid_flows, soc_trajectory, costs, and diagnostics.

    Raises:
        NotImplementedError: Always — this is a stub.
    """
    raise NotImplementedError(
        "battery simulator is not yet implemented — see ADR-0003 §battery physical simulator"
    )


def compute_efc(charge_kwh: list[float], discharge_kwh: list[float], capacity_kwh: float) -> float:
    """Compute equivalent full cycles from per-interval charge/discharge.

    Raises:
        NotImplementedError: Always — this is a stub.
    """
    raise NotImplementedError("EFC computation is not yet implemented")
