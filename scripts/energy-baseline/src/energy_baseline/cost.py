"""Cost model — tariff-verified cost computation.

STUB: signatures and docstrings only.  Implementation is deferred to a
later agent.
"""

from __future__ import annotations


def compute_interval_cost(
    *,
    energy_kwh: float,
    price_eur_per_kwh: float,
    tax_multiplier: float | None = None,
) -> float:
    """Compute the cost for a single 15-min interval.

    Applies the tax multiplier when confirmed.  Refuses to produce a
    billed cost when *tax_multiplier* is ``None``.

    Raises:
        NotImplementedError: Always — this is a stub.
    """
    raise NotImplementedError("cost model is not yet implemented")


def compute_period_cost(
    *,
    energies_kwh: list[float],
    prices_eur_per_kwh: list[float],
    tax_multiplier: float | None = None,
) -> float:
    """Compute total cost over a list of intervals.

    Raises:
        NotImplementedError: Always — this is a stub.
    """
    raise NotImplementedError("cost model is not yet implemented")
