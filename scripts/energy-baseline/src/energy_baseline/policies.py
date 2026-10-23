"""Benchmark policies — no-battery baseline and the causal price rule.

Pure numeric kernels: they consume a :class:`~energy_baseline.battery.BatterySpec`
and a :class:`~energy_baseline.battery.Horizon` and return a
:class:`~energy_baseline.battery.Schedule`.  No I/O, no forecast, no future
knowledge in the causal policy.

Every policy routes its plan through
:func:`energy_baseline.battery.simulate`, so the physics invariants are checked
by one implementation shared with the oracle.  Gap exclusion (``usable``) and
the pretax cost basis are therefore *identical* across the three policies.

Charge and discharge are **not** bounded symmetrically.  The no-export rule
(``grid = load + charge - discharge >= 0``) caps discharge at the household
load; charging raises import, so it is capped only by the power rating, by SOC
headroom and — only when configured — by the site import ceiling
(:attr:`~energy_baseline.battery.Horizon.import_limit_kw`, unknown by default).
The ceiling limits the *charging decision*, not the measurement::

    charge[t] <= max(0, import_limit_kw * h - load[t])

so an interval whose load already meets or exceeds the ceiling simply has no
charging headroom (``charge[t] = 0``) rather than being illegal.  Discharge is
not part of that rule — it *lowers* the draw, so it stays available exactly when
the ceiling is tightest, bounded only by the no-export rule.  Net grid import
may therefore exceed the ceiling when the measured load alone does, and that is
an observation, never a violation; the reason is structural, not empirical — the
ceiling constrains a **decision we make** (how much to charge), never **history
we measured**.  The inequality above is also the right *basis*: ``load[t]`` is
per-interval energy, so comparing it against ``import_limit_kw * h`` compares the
average power drawn over an interval with the contracted power — which is how
maximum demand is actually enforced, as a 15-minute average rather than against
instantaneous readings.  A policy that refused to report such intervals would
refuse to report the baseline at all.  See
:func:`energy_baseline.battery.ceiling_exceedance_indices` and
:mod:`energy_baseline.battery` invariants 6 and 7 for the verified
full-population figures and for the withdrawn peak-based justification.

Reporting honesty: the causal policy is allowed to cost **more** than the
no-battery baseline (a battery that charges into an expensive afternoon and
discharges into a cheap morning loses money and round-trip efficiency).  Nothing
here clips or floors the result — savings can be negative and are reported as
such, per ADR-0003 §"Strategy comparison".

Idle self-discharge is not modelled (SOC moves only through charge/discharge
flows); this is a documented assumption, not a measured property, and is shared
by the oracle and the simulator.
"""

from __future__ import annotations

import math

import numpy as np

from .battery import (
    BatterySpec,
    Horizon,
    Schedule,
    simulate,
    validate_spec,
)

__all__ = [
    "fit_thresholds",
    "policy_causal_price",
    "policy_no_battery",
]


# ---------------------------------------------------------------------------
# Policy 1 — no battery
# ---------------------------------------------------------------------------


def policy_no_battery(spec: BatterySpec, horizon: Horizon) -> Schedule:
    """Baseline counterfactual: the household as measured, battery absent.

    ``charge = discharge = 0`` everywhere and ``grid = load``.  The battery spec
    is only used to define the (constant) SOC trajectory and the EFC denominator,
    so that the three schedules share one accounting frame.

    The existing AppDaemon scheduling is **already inside** the historical load
    series; this baseline is what the household pays today, and no attempt is
    made to model or subtract that prior optimisation.

    This is a *report of measured history*, so a configured
    ``Horizon.import_limit_kw`` can never make it fail.  With zero flows the net
    import equals the load, and intervals where the load alone exceeds the
    ceiling are recorded as an observation —
    :attr:`~energy_baseline.battery.Schedule.ceiling_exceeding_intervals` —
    because the reason is structural, not empirical: the ceiling constrains a
    *decision we make* (charging), not *history we measured*, so no dispatch
    decision caused those intervals and no dispatch can undo them.  Raising here
    would declare the household's own past infeasible and sink every benchmark on
    real data.  The basis is already the one the contract uses — ``load[t]`` is
    per-interval *energy*, so this is the 15-minute average maximum demand is
    billed on, not an instantaneous reading — and on that basis the full-population
    2025 archive tops 5.0 kW in 5 of 34 744 clean intervals (0.014 %) and on 5 of
    365 days, against an annual mean load of 0.633 kW (12.7 % of the ceiling).
    5.0 kW is user-confirmed from the contract on 2026-10-23, never inferred from
    sensors; see :mod:`energy_baseline.battery` invariants 6 and 7 for the full
    table, including why the 8 678.9 W instantaneous maximum bears only on the
    battery's power rating and wiring headroom, never on contract feasibility.

    Returns ``solver_status="not_applicable"``, ``bound_kind="none"`` and
    ``is_certified=False``.
    """
    zero = np.zeros(horizon.n)
    return simulate(spec, horizon, zero, zero)


# ---------------------------------------------------------------------------
# Policy 2 — causal price threshold rule
# ---------------------------------------------------------------------------


def policy_causal_price(
    spec: BatterySpec,
    horizon: Horizon,
    *,
    charge_threshold: float,
    discharge_threshold: float,
) -> Schedule:
    """Fixed-threshold dispatch using only the current price and past state.

    Decision at interval ``t`` (ADR-0003 §"Policy design": no day-ahead array,
    no future load, no look-ahead of any kind):

    * ``price[t] < charge_threshold`` → charge at full power, limited by the
      power rating, by SOC headroom to ``soc_max_frac``, and by the site import
      ceiling when one is configured: ``charge[t] <= max(0, import_limit_kw * h -
      load[t])`` (the headroom the load leaves, see
      :attr:`~energy_baseline.battery.Horizon.charge_headroom_kwh`).  An interval
      whose load already meets or beats the ceiling has zero headroom, so the
      causal policy charges nothing there — charging is unavailable, not illegal.
    * ``price[t] > discharge_threshold`` → discharge at full power, limited by
      the SOC floor (raised to the seed SOC by seed protection — see the note
      below), by the power rating, and by ``load[t]`` (the no-export rule: a
      battery cannot push more into the household than the household is drawing).
      The ceiling never limits this leg: discharging *reduces* import, so it is
      allowed precisely in the ceiling-exceeding intervals where it helps most.
    * otherwise → idle.

    Why charging is **not** capped at the load: grid import is
    ``grid[t] = load[t] + charge[t] - discharge[t]`` and the only grid
    constraint is ``grid[t] >= 0``.  Charging *increases* import, so it can never
    cause an export and the no-export rule puts no upper bound on it.  A
    ``charge[t] <= load[t]`` rule is physically unmotivated and destroys the
    arbitrage the strategy exists for: household load bottoms out around a 200 W
    baseload, which would cap charging at ~0.05 kWh per 15-minute interval —
    ~20 hours to fill a 5.12 kWh battery instead of ~1.75 h at the 2.5 kW rating.
    The genuine limits are the power rating, SOC headroom, and an explicitly
    configured import ceiling (``Horizon.import_limit_kw``, unknown by default
    and never asserted from a sensor reading) applied as the *residual* headroom
    above.  Unlike the old net-import reading of the ceiling, this costs the
    policy nothing it could not already do: with one binary mode per interval it
    never discharges to earn charging headroom anyway.

    Nothing here raises because the measured load tops the ceiling: that is
    recorded on the returned schedule as
    :attr:`~energy_baseline.battery.Schedule.ceiling_exceeding_intervals`.

    The discharge cap clamps the request, it never pushes energy onto the grid:
    an over-sized discharge is cut down to ``load[t]`` rather than exported.

    Terminal SOC is deliberately **not** enforced here: a causal controller
    cannot know the end of the horizon, so the schedule reports the SOC it
    actually reached.  Seed protection (the raised discharge floor) makes the
    open cycle conservative rather than free — the policy can end with energy
    still stored (which it paid for at the meter), but it can never spend energy
    it did not charge during the window.  In the neutral case
    (``terminal_soc_frac is None``) the floor is ``max(soc_min, initial_soc)``,
    which is exactly the shared common-feasible-set floor the free oracle uses.
    :func:`energy_baseline.battery.check_feasible` with the default
    ``enforce_terminal_soc=True`` still lists the residual terminal drift, which
    is the honest signal that the horizon's accounting is not closed.

    One consequence worth stating: because the leftover energy is not valued, a
    horizon whose prices are **negative** turns the leftover into a credit, and
    the open causal cycle can then come out cheaper than the terminal-neutral
    oracle.  That is the ADR's "initial and terminal SOC are equal or have an
    explicit defensible valuation" requirement, not a dispatch result — on
    non-negative prices (the ordinary case) hoarding costs money and the oracle
    dominates.  The test suite pins both sides of that.

    Raises:
        ValueError: If *spec* is meaningless, the thresholds are non-finite, or
            ``charge_threshold > discharge_threshold`` (an inverted band means no
            price can ever discharge).  Equality is allowed: the idle band is
            then empty but the rule stays well defined.
    """
    validate_spec(spec)
    if not math.isfinite(charge_threshold) or not math.isfinite(discharge_threshold):
        raise ValueError(
            f"thresholds must be finite EUR/kWh, got charge={charge_threshold!r} "
            f"and discharge={discharge_threshold!r}"
        )
    if charge_threshold > discharge_threshold:
        raise ValueError(
            f"charge_threshold={charge_threshold} must be <= discharge_threshold="
            f"{discharge_threshold}; an inverted band never discharges"
        )

    n = horizon.n
    charge = np.zeros(n)
    discharge = np.zeros(n)
    limit = spec.interval_energy_limit_kwh(horizon.interval_seconds)
    # Per-interval charging headroom: ``max(0, ceiling*h - load[t])``.  ``None``
    # means the ceiling is unknown (not zero): the import side stays
    # unconstrained instead of silently capping charging at an invented number.
    # A load that already meets or exceeds the ceiling gives headroom 0, so the
    # policy simply charges nothing there — that is the physically meaningful
    # outcome, not an error.
    charge_headroom_kwh = horizon.charge_headroom_kwh
    # Seed protection: a causal controller cannot close the horizon's energy
    # accounting, so it is also not allowed to *open* it.  When the spec requires
    # a return to the starting SOC (``terminal_soc_frac is None``), the discharge
    # floor is raised to the seed SOC: energy that was already in the battery at
    # the start of the evaluation window is not the policy's to spend.  Without
    # this, a policy that simply drains the seed looks cheaper than the oracle
    # for free, and the comparison is no longer apples-to-apples.
    floor_kwh = max(spec.soc_bound_kwh("min"), spec.soc_bound_kwh("terminal"))
    ceil_kwh = spec.soc_bound_kwh("max")

    prices = horizon.prices
    load = horizon.load_kwh
    usable = horizon.usable
    eta_c = spec.charge_efficiency
    eta_d = spec.discharge_efficiency

    # Strictly sequential: interval t may read prices[..t] and its own SOC only.
    soc = spec.soc_bound_kwh("init")
    for t in range(n):
        if not usable[t]:
            continue  # gap-excluded interval: forced zero flow, identical for all policies
        price = prices[t]
        if price < charge_threshold:
            soc_headroom = max((ceil_kwh - soc) / eta_c, 0.0)
            cap = limit  # NOT min(limit, load[t]): charging raises import, load is no bound
            if charge_headroom_kwh is not None:
                # charge[t] <= max(0, ceiling*h - load[t]): the residual headroom
                # the load leaves under the ceiling.  Zero when the load already
                # meets or exceeds it, so charging is unavailable, not illegal.
                cap = min(cap, float(charge_headroom_kwh[t]))
            charge[t] = min(cap, soc_headroom)
        elif price > discharge_threshold:
            available_load = max(load[t], 0.0)
            stored = max((soc - floor_kwh) * eta_d, 0.0)
            # The ceiling never limits discharge: discharging lowers the draw,
            # so it is the physically correct response to a tight ceiling.
            discharge[t] = max(min(limit, stored, available_load), 0.0)
        soc += charge[t] * eta_c - discharge[t] / eta_d

    # enforce_terminal_soc=False: a causal policy cannot guarantee end-of-horizon
    # energy neutrality, and it must not be allowed to fake it by look-ahead.
    return simulate(spec, horizon, charge, discharge, enforce_terminal_soc=False)


def fit_thresholds(
    prices_train: np.ndarray,
    *,
    charge_pct: float,
    discharge_pct: float,
) -> tuple[float, float]:
    """Derive ``(charge_threshold, discharge_threshold)`` from a **training** slice.

    Percentiles are taken over ``prices_train`` only — the prices of the
    evaluation horizon never enter.  ``np.percentile``'s default linear
    interpolation is used, so the result is deterministic for a given array.

    **Freeze before evaluating.**  Thresholds must be fitted on an earlier train
    split and then held fixed while the policies are scored; refitting on the
    evaluation window (or on the whole series) leaks future prices into a policy
    that is claimed to be causal, and turns the benchmark into a measurement of
    overfitting rather than of dispatch value.  Typical split: fit on the first
    months, evaluate on the remainder.

    Typical call: ``fit_thresholds(train_prices, charge_pct=25.0,
    discharge_pct=75.0)`` — charge in the cheapest quartile, discharge in the
    most expensive.

    Args:
        prices_train: Pretax EUR/kWh prices of the training period.  May be
            empty of structure but not empty of data; negative prices are valid.
        charge_pct: Percentile for the charge threshold (0-100).
        discharge_pct: Percentile for the discharge threshold (0-100), must be
            ``>= charge_pct``.

    Raises:
        ValueError: If the training array is empty/non-finite, the percentiles
            are out of range or inverted, or the resulting pair is inverted
            (cannot happen from percentiles alone, checked as a guard).
    """
    if not 0.0 <= charge_pct <= 100.0 or not 0.0 <= discharge_pct <= 100.0:
        raise ValueError(
            f"percentiles must be within [0, 100], got charge_pct={charge_pct} "
            f"and discharge_pct={discharge_pct}"
        )
    if charge_pct > discharge_pct:
        raise ValueError(f"charge_pct={charge_pct} must be <= discharge_pct={discharge_pct}")

    train = np.asarray(prices_train, dtype=float).reshape(-1)
    if train.size == 0:
        raise ValueError("prices_train is empty; thresholds cannot be fitted")
    if not np.isfinite(train).all():
        bad = int(np.argmax(~np.isfinite(train)))
        raise ValueError(f"prices_train contains a non-finite value at index {bad}")

    low, high = np.percentile(train, [charge_pct, discharge_pct])
    charge_threshold = float(low)
    discharge_threshold = float(high)
    if charge_threshold > discharge_threshold:  # pragma: no cover - defensive
        raise ValueError(
            f"fitted thresholds are inverted: charge={charge_threshold} > "
            f"discharge={discharge_threshold}"
        )
    return charge_threshold, discharge_threshold
