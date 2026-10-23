"""Benchmark policies — no-battery, causal price-based, and helpers.

STUB: signatures and docstrings only.  Implementation is deferred to a
later agent.
"""

from __future__ import annotations


def policy_no_battery(
    *,
    gross_kwh: list[float],
    prices_eur_per_kwh: list[float],
    tax_multiplier: float | None = None,
) -> dict:
    """No-battery baseline: existing AppDaemon scheduling, counterfactual cost.

    Returns:
        Dictionary with cost_eur, cost_basis, and per-interval breakdown.

    Raises:
        NotImplementedError: Always — this is a stub.
    """
    raise NotImplementedError(
        "no-battery policy is not yet implemented — see ADR-0003 §strategy comparison"
    )


def policy_causal_price(
    *,
    gross_kwh: list[float],
    prices_eur_per_kwh: list[float],
    capacity_kwh: float,
    power_kw: float,
    round_trip_efficiency: float,
    soc_window_min: float,
    soc_window_max: float,
    charge_threshold: float,
    discharge_threshold: float,
    tax_multiplier: float | None = None,
) -> dict:
    """Causal simple price-based policy.

    Uses only current price and past state — fixed thresholds derived
    from a training period.  No future knowledge required.

    Returns:
        Dictionary with cost_eur, cost_basis, soc_trajectory, and diagnostics.

    Raises:
        NotImplementedError: Always — this is a stub.
    """
    raise NotImplementedError(
        "causal price-based policy is not yet implemented — see ADR-0003 §policy design"
    )
