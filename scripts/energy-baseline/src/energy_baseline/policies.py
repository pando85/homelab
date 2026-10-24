"""Benchmark policies — no-battery baseline, the causal price rule, day-ahead rolling.

Pure numeric kernels: they consume a :class:`~energy_baseline.battery.BatterySpec`
and a :class:`~energy_baseline.battery.Horizon` and return a
:class:`~energy_baseline.battery.Schedule`.  No I/O.  The causal policy uses no
forecast and no future knowledge of any kind; the day-ahead policy receives its
load forecast as an argument, computed by
:func:`forecast_load_trailing_slot_mean` from strictly-prior intervals only.

Every policy routes its plan through
:func:`energy_baseline.battery.simulate`, so the physics invariants are checked
by one implementation shared with the oracle.  Gap exclusion (``usable``) and
the pretax cost basis are therefore *identical* across all five policies.

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
such, per ADR-0003 §"Strategy comparison".  The same is true of the day-ahead
policy: it is *not* guaranteed to beat the causal rule, and a myopic optimiser
with a poor load forecast or a badly chosen terminal SOC value can lose to a dumb
threshold rule.  Which one wins is a measurement, never an assertion (see
:func:`policy_day_ahead`).

The information ladder the five policies form (ADR-0003 §"Strategy comparison",
rung 3 added 2026-10-23 as the deferred day-ahead variant recorded in
``planning/energy-baseline-appdaemon-baseline-context.md``, rung 4 added as the
rolling-horizon variant)::

    policy                 prices known                 load known
    ---------------------  ---------------------------  ----------------------------
    no_battery             none (measured history)      none (measured history)
    causal_price           none (frozen thresholds)     none
    day_ahead              today's 24 h, at midnight    strictly causal forecast
    rolling_horizon        D-1 20:30 → D end, replan    frozen causal forecast
    oracle                 whole horizon, perfect       whole horizon, perfect

The day-ahead policy sits strictly between the causal rule and the oracle, and
that is its whole purpose.  The certified Phase-1 result is that the causal rule
captures a known fraction of the oracle's optimum, but the oracle also knows
future *load*, so the unreachable remainder conflates two different things: value
recoverable from information that is genuinely published for free, and value that
requires load foresight nobody has.  :func:`policy_day_ahead` separates them by
using tomorrow's prices exactly the way this household's own deployed automation
already does — a full 24-hour curve read at local midnight — while keeping the
load forecast strictly causal.

The rolling-horizon policy is more informed than day_ahead on PRICES and less informed
on LOAD, so it does not sit strictly between day_ahead and oracle in a single information
ordering.  Prices: at 20:30 on D-1 it already sees day D's exact prices (published the
evening before), which day_ahead does not see until D's midnight, and its horizon spans
to the end of D rather than stopping at it.  Load: its profile is frozen once from
training data (same trailing-window LENGTH as day_ahead, but a DIFFERENT BASIS —
day_ahead re-learns from the trailing window before each dispatch day, while this rung
freezes the profile at the training cut and reuses it for every solve), so the two rungs
differ in load knowledge as well as in price information and cadence.  On the 2025
full-year ladder the load-knowledge asymmetry dominated and the rung cost MORE than
day_ahead; re-running the identical Q4-2025 holdout on the same dataset reproduced the
win, which rules out the round-trip efficiency correction as the cause.  The rung is
therefore not provably between the two in any single ordering; the comparison is a
measurement, not a ladder step.  Empirically, rolling_horizon <= day_ahead held in 81 of
81 offline configurations, all of them a Q4 holdout immediately following the training
cut; that is strong evidence within its own configuration and not a proof, and it does
not transfer to evaluation windows that extrapolate far beyond the cut.  The
implementation lives in :mod:`energy_baseline.rolling_horizon`; the benchmark
orchestrator wires it into the ladder.

Its price information set is not an invented convenience.  The claim "a full
day's price curve is available at local midnight" is verified from this
repository's production AppDaemon code, not assumed:
``apps/home-assistant/appdaemon/apps/climate_control.py:45`` registers
``await self.run_daily(self._daily_register_schedulers, "00:00:40")``, and
``_get_prices`` at lines 73-91 of the same file reads
``pvpc["attributes"][f"price_{i:02d}h"]`` for ``i in range(24)`` from
``sensor.esios_pvpc`` (``apps/home-assistant/appdaemon/apps/apps.yaml:10``),
re-labelling the values onto ``datetime(now.year, now.month, now.day)`` plus ``i``
hours (line 85).  A third app, ``dhw_control.py:48`` (``00:00:30``), does the same
for the water tank.  Production evidence, therefore, that day *D*'s 24 prices are
in hand at 00:00 of day *D* — which is exactly the assumption ADR-0003 line 100
requires a day-ahead variant to label explicitly and to check against the sensor's
archive rather than against realised prices.

What the day-ahead policy may **not** see is stated just as precisely, because it
is the experiment: prices beyond its own local day, and measured load at or after
its own local day.  Load enters the decision only through
:func:`forecast_load_trailing_slot_mean`, which reads strictly-prior intervals.
The forecast never enters the *cost*: cost is always re-computed by
:func:`energy_baseline.battery.simulate` against the measured load, so a forecast
error degrades the dispatch quality and is disclosed as such, but it can never
fabricate or hide money.

Idle self-discharge is not modelled (SOC moves only through charge/discharge
flows); this is a documented assumption, not a measured property, and is shared
by the oracle and the simulator.
"""

from __future__ import annotations

import dataclasses
import math
from collections.abc import Sequence
from dataclasses import dataclass
from typing import Any

import numpy as np

from .battery import (
    BatteryPhysicsError,
    BatterySpec,
    Horizon,
    Schedule,
    check_feasible,
    grid_import_kwh,
    horizon_cost_eur,
    simulate,
    validate_spec,
)
from .oracle import OracleError, solve_oracle

__all__ = [
    "DEFAULT_FORECAST_WINDOW_DAYS",
    "FORECAST_FALLBACK_RULE",
    "FORECAST_METHOD",
    "DayAheadDay",
    "contiguous_day_bounds",
    "fit_thresholds",
    "forecast_load_trailing_slot_mean",
    "policy_causal_price",
    "policy_day_ahead",
    "policy_no_battery",
]

#: Default trailing window for the day-ahead load forecast, in local days.  The
#: caller may override it; the value used is always recorded in the run artifact.
DEFAULT_FORECAST_WINDOW_DAYS = 28

#: Name of the only forecast method implemented, recorded verbatim in the artifact
#: so a run can never be re-read later as "some forecast" and be silently upgraded.
FORECAST_METHOD = "trailing_slot_mean"

#: SOC slack below which "the opening seed already fills the window" is treated as
#: filled.  Same order as the kernel's own ``_TOL_KWH``: this is a comparison between two
#: floats that a rolling loop and a cumsum can disagree about in the last bit.
_SOC_NOISE_KWH = 1e-9

#: The forecast's documented fallback ladder, stated once so a run artifact can
#: never report a method name without the rule that goes with it.
FORECAST_FALLBACK_RULE = (
    "per slot k of the target local day: trailing mean of load at position k over the "
    "previous window_days local days that (a) have position k and (b) are usable there; "
    "if no prior day contributes position k (possible only when the target day is longer "
    "than every day in its window, i.e. a 25-hour fall-back day), use the pooled mean of "
    "every usable interval in the window — a flat profile; if the window holds no usable "
    "interval at all (the first local day of the dataset), forecast 0.0 kWh"
)


# ---------------------------------------------------------------------------
# Policy 1 — no battery
# ---------------------------------------------------------------------------


def policy_no_battery(spec: BatterySpec, horizon: Horizon) -> Schedule:
    """Baseline counterfactual: the household as measured, battery absent.

    ``charge = discharge = 0`` everywhere and ``grid = load``.  The battery spec
    is only used to define the (constant) SOC trajectory and the EFC denominator,
    so that every policy's schedule shares one accounting frame.

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


# ---------------------------------------------------------------------------
# Policy 3 — rolling day-ahead optimiser
# ---------------------------------------------------------------------------


def contiguous_day_bounds(day_labels: Sequence[Any]) -> list[tuple[Any, int, int]]:
    """Group a chronological sequence of local-day labels into ``[start, stop)`` runs.

    This is the partition the rolling day-ahead solve uses: **local calendar days**
    (``Europe/Madrid`` under the shipped tariff), not UTC days.  The reason is not
    cosmetic — the tariff is hourly in local time, so a spring-forward day carries
    92 intervals and a fall-back day 100, and a UTC-day partition would cut both of
    those days in pieces whose prices belong to different local days and would hand
    one local midnight's solve a curve from two local days.

    Labels must be sortable among themselves (``datetime.date`` from the analysis
    grid's ``local_day`` column is the intended input; ISO strings work too).
    Grouping is by *runs*, so a label that reappears after a different one is an
    ordering bug and is rejected rather than silently merged.

    Raises:
        ValueError: If the sequence is empty, or a label sorts before the one it
            follows (out of chronological order — a rolling solve cannot start).
    """
    if len(day_labels) == 0:
        raise ValueError("day_labels is empty: there is no local day to partition")
    bounds: list[tuple[Any, int, int]] = []
    current = day_labels[0]
    start = 0
    for i in range(1, len(day_labels)):
        label = day_labels[i]
        if label == current:
            continue
        if label < current:
            raise ValueError(
                f"day_labels is not chronological: {label!r} at index {i} follows "
                f"{current!r}; a rolling day-ahead solve needs local days in order"
            )
        bounds.append((current, start, i))
        current = label
        start = i
    bounds.append((current, start, len(day_labels)))
    return bounds


@dataclass(frozen=True)
class DayAheadDay:
    """One local calendar day handed to :func:`policy_day_ahead`.

    ``label`` is only for the artifact (an ISO local day); ``start`` / ``stop`` are
    half-open indices into the *horizon* the policy is solving; ``forecast_load`` is
    that day's causal load forecast, one value per interval, and ``forecast`` is the
    forecast diagnostics for the day (retrospective reporting only — the policy never
    reads it, and it cannot change a plan).
    """

    label: str
    """ISO ``YYYY-MM-DD`` of the local day, for the run artifact."""

    start: int
    """First horizon index of this local day."""

    stop: int
    """One past the last horizon index of this local day (half-open)."""

    forecast_load: np.ndarray
    """Forecast kWh per interval of this day, strictly causal (prior days only)."""

    forecast: dict[str, Any] = dataclasses.field(default_factory=dict)
    """Per-day forecast diagnostics, copied into the artifact unchanged."""

    def __post_init__(self) -> None:
        values = np.array(self.forecast_load, dtype=float, copy=True).reshape(-1)
        values.setflags(write=False)
        object.__setattr__(self, "forecast_load", values)


def forecast_load_trailing_slot_mean(
    load_kwh: Sequence[float] | np.ndarray,
    days: Sequence[tuple[Any, int, int]],
    *,
    window_days: int = DEFAULT_FORECAST_WINDOW_DAYS,
    usable: np.ndarray | None = None,
) -> tuple[np.ndarray, list[dict[str, Any]]]:
    """Trailing same-position mean of measured load — the day-ahead policy's forecast.

    Definition.  For day *j* and slot position ``k`` within it, the forecast is the
    arithmetic mean of the load measured at **position ``k`` of the previous
    ``window_days`` local days**, using only days that have a position ``k`` and only
    intervals that are *usable*.  Position, not wall-clock time, is the alignment key:
    on an ordinary 96-slot day the two are the same, and on the two DST days per year
    position alignment is the deliberate choice (see the fallback ladder below for what
    happens at the tail positions that only a 25-hour day has).

    **The causality rule is the entire point of this function.**  Every sample it reads
    lies at an index strictly below ``days[j][1]`` — the first interval of the target
    day.  Nothing from day *j* or later can enter, structurally: the prior-day list is
    sliced as ``days[max(0, j - window_days) : j]``, so the target day is not even
    reachable.  That is what makes "day *D*'s plan is unchanged when load after day *D*
    is perturbed" a testable property rather than a hope.

    Only measured data is used.  Intervals the dataset marks unusable
    (``usable=False`` — gap-excluded, null or reconstructed-and-refused) are treated as
    *missing*, never as zero, so a measurement hole cannot depress a forecast toward
    free energy.  A slot with no usable prior sample falls back as documented in
    :data:`FORECAST_FALLBACK_RULE`; the fallback is counted per day, never silent.

    The window may legitimately reach back past the benchmark's train/eval cut into the
    training split.  That is not leakage: strictly-past measured load is what any
    controller standing at local midnight of day *D* would have in its history, and
    leakage is defined against the *target day*, not against ``train_end``.  What would
    be leakage — and what this function cannot do — is reading day *D* itself.

    Forecast **error** is reported per day (MAE / RMSE / bias against the measured
    load).  Computing it obviously requires hindsight; it is a *measurement of the
    forecast*, written into the artifact after the fact, and no plan depends on it.

    Args:
        load_kwh: Measured gross load per interval over the whole dataset (history
            included), same order as *days*.
        days: ``(label, start, stop)`` runs from :func:`contiguous_day_bounds`.
        window_days: trailing window in local days (``>= 1``).  ``1`` is a legal,
            degenerate "yesterday only" forecast; the value used is recorded.
        usable: per-interval usability mask.  ``None`` means "everything is usable",
            which the benchmark never passes — it hands in the real mask.

    Returns:
        ``(forecast, per_day)`` — ``forecast`` is a full-length array where each
        interval carries the value that was knowable at that interval's local midnight
        (so callers slice it directly, and a partial first day keeps its true
        positions); ``per_day`` is one diagnostics dict per entry of *days*, each
        carrying its ``local_day`` for lookup.

    Raises:
        ValueError: If the sizes disagree, *window_days* is < 1, or the load array
            holds a non-finite value in a usable interval.
    """
    if window_days < 1:
        raise ValueError(f"window_days must be >= 1, got {window_days}")
    load = np.asarray(load_kwh, dtype=float).reshape(-1)
    n = load.size
    if n == 0:
        raise ValueError("load_kwh is empty: there is nothing to forecast")
    if usable is None:
        keep = np.ones(n, dtype=bool)
    else:
        keep = np.asarray(usable, dtype=bool).reshape(-1)
        if keep.size != n:
            raise ValueError(f"usable has {keep.size} entries, load_kwh has {n}")
    if not np.isfinite(load[keep]).all():
        bad = int(np.flatnonzero(~np.isfinite(load))[0])
        raise ValueError(f"load_kwh contains a non-finite value at index {bad}")
    for label, start, stop in days:
        if not (0 <= start < stop <= n):
            raise ValueError(f"day {label!r} bounds [{start}, {stop}) are outside 0..{n}")

    forecast = np.zeros(n, dtype=float)
    per_day: list[dict[str, Any]] = []
    for j, (label, start, stop) in enumerate(days):
        prior = days[max(0, j - int(window_days)) : j]
        m = stop - start
        sums = np.zeros(m, dtype=float)
        counts = np.zeros(m, dtype=int)
        pooled_sum = 0.0
        pooled_count = 0
        prior_with_data = 0
        for _p_label, p_start, p_stop in prior:
            positions = np.flatnonzero(keep[p_start:p_stop])
            if positions.size == 0:
                continue
            prior_with_data += 1
            values = load[p_start:p_stop]
            pooled_sum += float(values[positions].sum())
            pooled_count += int(positions.size)
            shared = min(m, p_stop - p_start)
            in_range = positions[positions < shared]
            np.add.at(sums, in_range, values[in_range])
            np.add.at(counts, in_range, 1)
        pooled_mean = pooled_sum / pooled_count if pooled_count else 0.0

        day_values = np.zeros(m, dtype=float)
        slot_counts = counts.astype(int)
        used_slot = slot_counts > 0
        pooled_slots = (~used_slot) & (pooled_count > 0)
        empty_slots = ~used_slot & ~pooled_slots
        day_values[used_slot] = sums[used_slot] / slot_counts[used_slot]
        day_values[pooled_slots] = pooled_mean
        # ``empty_slots`` stay 0.0: no usable history at all for this day.
        forecast[start:stop] = day_values

        measured = keep[start:stop]
        error = day_values[measured] - load[start:stop][measured]
        error_stats = {
            "mae_kwh": float(np.abs(error).mean()) if error.size else None,
            "rmse_kwh": float(np.sqrt(np.mean(error**2))) if error.size else None,
            "bias_kwh": float(error.mean()) if error.size else None,
        }
        fallback = "none"
        if pooled_slots.any():
            fallback = "pooled_window_mean"
        if empty_slots.any():
            fallback = "zero_no_history" if pooled_count == 0 else "mixed"
        per_day.append(
            {
                "local_day": str(label),
                "intervals": int(m),
                "usable_intervals": int(measured.sum()),
                "window_days_requested": int(window_days),
                "window_days_available": len(prior),
                "window_days_with_usable_data": prior_with_data,
                "slots_using_trailing_mean": int(used_slot.sum()),
                "slots_pooled_fallback": int(pooled_slots.sum()),
                "slots_zero_no_history": int(empty_slots.sum()),
                "trailing_mean_samples_min": int(slot_counts[used_slot].min())
                if used_slot.any()
                else 0,
                "trailing_mean_samples_max": int(slot_counts[used_slot].max())
                if used_slot.any()
                else 0,
                "fallback": fallback,
                "forecast_fallback_rule": FORECAST_FALLBACK_RULE,
                "forecast_sum_kwh": float(day_values[measured].sum()),
                "measured_sum_kwh": float(load[start:stop][measured].sum()),
                **error_stats,
            }
        )
    forecast.setflags(write=False)
    return forecast, per_day


def _clip_plan_to_model_bounds(
    spec: BatterySpec,
    day_horizon: Horizon,
    charge: np.ndarray,
    discharge: np.ndarray,
    *,
    floor_kwh: float,
) -> tuple[np.ndarray, np.ndarray, float, float, float]:
    """Project a forecast-derived plan onto the **measured** day's feasible set.

    The MILP bounds discharge by the *forecast* load and charging by the headroom the
    *forecast* load leaves under the import ceiling.  A forecast cannot buy physical
    feasibility: if the household drew less than predicted, the planned discharge would
    push the remainder onto the grid, and if it drew more, the planned charge would
    breach the contracted import.  So the plan is walked once against the measured
    horizon and clipped, interval by interval, in chronological order (a clipped charge
    removes stored energy that a later discharge may have counted on, so the pass has to
    be sequential and cannot be a pair of vector minima).

    Every bound it uses comes from the shared battery model, never re-derived here:
    the power/energy limit from
    :meth:`~energy_baseline.battery.BatterySpec.interval_energy_limit_kwh`, the import
    ceiling as :attr:`~energy_baseline.battery.Horizon.charge_headroom_kwh` computed on
    the *measured* load, the SOC ceiling from :meth:`~energy_baseline.battery.
    BatterySpec.soc_bound_kwh`, the no-export cap from ``day_horizon.load_kwh``, and the
    seed-protection floor from the caller — the **horizon's** causal floor, not the day's
    opening SOC, for the reason given in :func:`policy_day_ahead`.  Clipping is
    one-directional — flows are only ever reduced, never raised or moved in time — so it
    cannot create energy or invent an arbitrage the optimiser did not ask for.

    Returns ``(charge, discharge, end_soc_kwh, charge_clipped_kwh, discharge_clipped_kwh)``.
    """
    m = day_horizon.n
    limit = spec.interval_energy_limit_kwh(day_horizon.interval_seconds)
    headroom = day_horizon.charge_headroom_kwh  # measured-load basis, single source
    ceil_kwh = spec.soc_bound_kwh("max")
    eta_c = spec.charge_efficiency
    eta_d = spec.discharge_efficiency
    load = day_horizon.load_kwh
    usable = day_horizon.usable

    out_charge = np.zeros(m, dtype=float)
    out_discharge = np.zeros(m, dtype=float)
    soc = spec.soc_bound_kwh("init")
    clipped_charge = 0.0
    clipped_discharge = 0.0
    for t in range(m):
        if not usable[t]:
            continue  # gap-excluded: zero flow, SOC carried — identical for every policy
        cap = limit
        if headroom is not None:
            cap = min(cap, float(headroom[t]))
        cap = min(cap, max((ceil_kwh - soc) / eta_c, 0.0))
        c = min(max(float(charge[t]), 0.0), cap)
        available = min(
            limit,
            max((soc - floor_kwh) * eta_d, 0.0),
            max(float(load[t]), 0.0),  # no-export rule, on measured load
        )
        d = min(max(float(discharge[t]), 0.0), max(available, 0.0))
        out_charge[t] = c
        out_discharge[t] = d
        clipped_charge += float(charge[t]) - c
        clipped_discharge += float(discharge[t]) - d
        soc += c * eta_c - d / eta_d
    return out_charge, out_discharge, soc, clipped_charge, clipped_discharge


def policy_day_ahead(
    spec: BatterySpec,
    horizon: Horizon,
    days: Sequence[DayAheadDay],
    *,
    terminal_value_eur_per_kwh: float,
    scoring_terminal_value_eur_per_kwh: float = 0.0,
    time_limit_s: float,
    mip_rel_gap: float,
    mip_abs_gap: float,
) -> tuple[Schedule, dict[str, Any]]:
    """Rolling day-ahead optimiser: one MILP per local day, executed, SOC rolled forward.

    Information set — this is the experiment, so it is stated as precisely as possible.
    At the solve for local day *D* the policy knows:

    * **all of day *D*'s prices** (``horizon.prices[start:stop]``).  Not invented:
        day-ahead PVPC is published the evening before, and this household's own
        deployed automation already consumes a full 24-hour curve at local midnight —
        verified from source, cited in this module's docstring
        (``climate_control.py:45`` + ``:73-91`` reading ``price_00h``…``price_23h`` off
        ``sensor.esios_pvpc``, ``dhw_control.py:48`` likewise).  The oracle's advantage
        over the causal rule is therefore *split*, not conflated: prices the pipeline
        can honestly claim were published, versus load nobody publishes.
        One limit on that claim must stay on the record, because it is the assumption
        the benchmark cannot verify from its own inputs: the archive stores the price
        **realised** in each hour, not a snapshot of the ``price_00h``…``price_23h``
        attribute dict as it stood at midnight.  This policy therefore assumes
        *published-in-advance equals realised-and-archived* for the day-ahead day, which
        is true by construction for PVPC (the day-ahead market clears before the day
        runs) but is not measured here, and the day-boundary convention of the sensor's
        attribute dict stays unresolved (TODO-0.4 in
        ``planning/home-battery-optimizer-implementation.md``).  Attribution of any
        part of the gap to that assumption would be a measurement error, so the run
        artifact restates it.
    * **a strictly causal load forecast** for day *D*, from
        :func:`forecast_load_trailing_slot_mean`.  It cannot see day *D*'s own measured
        load, nor anything after it.  Perturbing load from day *D*+1 onwards leaves day
        *D*'s plan bit-identical — a property the test suite pins.
    * **its own state**: the *actual* end-of-day SOC rolled forward from day *D*-1, and
        the SOC window, power limit and efficiencies from *spec*.
    * **the day's usability mask** (which intervals the archive trusts).  Disclosed
        because it is a small, non-economic look-ahead: a live controller learns a
        measurement gap roughly when it happens.  It is taken as shared, because
        ADR-0003 requires gap exclusions to be *identical* across policies, and the
        causal rule reads the same mask contemporaneously.

    What it must **not** know: prices or load of any day after *D*, the horizon's
    length, or any future end-of-horizon condition.  It is **myopic by construction** —
    a daily solve physically cannot see tomorrow — and that myopia is not hidden behind
    a trick.  With a free end-of-day SOC and no value on it, the rational plan is to
    empty the battery at midnight, which is exactly the artefact ADR-0001 warns about
    ("preserve a terminal SOC value or equivalent terminal penalty instead of
    artificially emptying the battery at midnight").  So end-of-day SOC is valued at
    ``terminal_value_eur_per_kwh`` inside each daily solve, and that valuation is the
    policy's **only** cross-day coupling.  It must be a number frozen before evaluation
    and derived from the training window (the run records its provenance); if it is ever
    derived from evaluation-period prices the experiment stops meaning what it says.

    **Two terminal values, not one.** The daily planning value
    (``terminal_value_eur_per_kwh``) is the internal number that prevents the battery
    from emptying at midnight; it is used inside each daily objective and is recorded in
    the artifact as ``internal_terminal_value_eur_per_kwh``.  The **common benchmark
    scoring value** (``scoring_terminal_value_eur_per_kwh``, default 0.0) is what the
    returned Schedule's ``cost_eur`` and ``inventory_adjustment_eur`` use, and it is the
    same value the oracle uses — so the comparison is on the same basis.  The per-day
    objectives sum to the internal-V-based cost, which is recorded in the artifact as
    ``internal_daily_objective_sum_eur`` with a note that it is not the common score.

    Mechanics.  The horizon is partitioned on local calendar days by
    :func:`contiguous_day_bounds`, so DST days contribute 92 or 100 intervals to one
    solve rather than being cut in half.  Each day is solved by
    :func:`energy_baseline.oracle.solve_oracle` with ``terminal_mode="free"`` and
    ``terminal_value_eur_per_kwh``, on a day-scoped
    :class:`~energy_baseline.battery.BatterySpec` whose ``initial_soc_frac`` is the
    carried SOC.  Reusing the oracle's constraint assembly is deliberate: SOC
    recurrence, per-step bounds, the binary one-mode-per-interval rule, the no-export
    row and the import-ceiling headroom row are the same code the oracle uses, so the
    physics cannot drift between the ceiling and the policy.

    Seed protection is horizon-level, and a withdrawn claim is recorded here because the
    wrong version was written first.  This docstring formerly said the floor "rises with
    the roll" — ``max(soc_min, that day's opening SOC)`` — and that is not seed
    protection, it is a straitjacket: energy the policy *paid for* on day D became
    unspendable seed on day D+1, so a battery that filled froze at the ceiling and the
    policy lost to the causal rule while holding charge it was forbidden to use.  The
    floor is fixed for the whole roll at ``max(soc_min, initial SOC of the evaluation
    window)`` — what the window was handed is not the policy's to spend, everything it
    bought afterwards is — which is also exactly the floor the causal policy and the free
    oracle share, so the common feasible set that makes the comparison meaningful
    survives.  A spec whose opening seed already sits at the ceiling has no headroom in
    either direction; those days are reported as ``frozen_by_seed_at_ceiling`` with zero
    flows rather than being sent to the solver as an invalid BatterySpec.

    The forecast-vs-measured gap is repaired, not ignored: see
    :func:`_clip_plan_to_model_bounds`.  Clipped energy is counted per day and in the
    summary.  A forecast that is merely wrong costs dispatch quality; it never produces
    an infeasible plan, an export, or a fabricated number.

    Accounting.  The concatenated plan is simulated **once** over the whole horizon by
    :func:`energy_baseline.battery.simulate` with ``enforce_terminal_soc=False`` (an
    open cycle: the horizon ends wherever the daily plans leave it), and the reported
    ``cost_eur`` is ``meter_cost_eur - scoring_terminal_value * (terminal - initial)`` —
    the identical framing the free oracle uses with the **same** scoring value, so the
    comparison is on the same basis.  The per-day terminal valuations telescope into one
    horizon-level adjustment using the internal planning value: day *D*'s end SOC is day
    *D*+1's start SOC by construction.  ``meter_cost_eur`` is therefore measured-load
    cost, and the consistency check in ``benchmark.py``
    (``recomputed meter cost + inventory adjustment == reported cost``) holds for this
    policy as it does for the other three.  The roll-up is additionally cross-checked
    against the kernel day by day and reported as
    ``meter_cost_rollup_abs_diff_eur``.

    Degradation, never substitution.  If a day's MILP comes back infeasible or without
    an incumbent, that day **holds SOC and does not charge** (zero flows), is flagged in
    ``per_day``, and the roll continues; the run does not abort and the oracle's answer
    is never quietly borrowed.  A day whose intervals are all gap-excluded is skipped the
    same way (its :class:`~energy_baseline.battery.Horizon` would reject an all-unusable
    slice, and an empty cost basis is not a dispatch problem to solve).  Counts are in
    the summary.

    Returns:
        ``(schedule, telemetry)``.  The schedule carries
        ``solver_status="feasible"`` — the word the store's vocabulary already has for
        "a plan that is feasible but not a proven optimum of the problem being scored" —
        ``bound_kind="none"``, ``is_certified=False``: the per-day objectives are
        (usually) certified optima of each day, but the whole-horizon number is a
        myopic plan, and it is not a bound on anything.  ``telemetry`` is the per-day
        and summary record for the run artifact: day count, per-day solve status and
        forecast error, clipping totals, degradation counts, solver work.

    Raises:
        ValueError: If *spec* is meaningless, the terminal value or a tolerance is not
            finite/positive, *days* does not partition the horizon exactly, or a day's
            forecast array is the wrong length, non-finite or negative (a negative
            forecast load would make the no-export row unsatisfiable by construction).
        BatteryPhysicsError: If the assembled whole-horizon plan fails the shared
            kernel.  That is a bug in this policy, not a dispatch outcome, and it is
            allowed to propagate loudly.
    """
    validate_spec(spec)
    if not math.isfinite(terminal_value_eur_per_kwh):
        raise ValueError(
            f"terminal_value_eur_per_kwh must be finite EUR/DC-kWh, got "
            f"{terminal_value_eur_per_kwh!r}; a myopic daily solve needs an explicit "
            "value on end-of-day SOC or it will rationally empty the battery at midnight"
        )
    if not math.isfinite(scoring_terminal_value_eur_per_kwh):
        raise ValueError(
            f"scoring_terminal_value_eur_per_kwh must be finite, got "
            f"{scoring_terminal_value_eur_per_kwh!r}"
        )
    if not math.isfinite(time_limit_s) or time_limit_s <= 0:
        raise ValueError(f"time_limit_s must be > 0, got {time_limit_s}")
    if mip_rel_gap <= 0 or mip_abs_gap <= 0:
        raise ValueError(
            f"mip_rel_gap and mip_abs_gap must be > 0, got {mip_rel_gap} and {mip_abs_gap}"
        )
    n = horizon.n
    cursor = 0
    for day in days:
        if day.start != cursor:
            raise ValueError(
                f"day {day.label!r} starts at {day.start}, the previous day ended at "
                f"{cursor}: a rolling day-ahead solve needs a gapless, ordered partition"
            )
        if day.stop <= day.start:
            raise ValueError(f"day {day.label!r} is empty: [{day.start}, {day.stop})")
        if day.forecast_load.size != day.stop - day.start:
            raise ValueError(
                f"day {day.label!r} has {day.stop - day.start} intervals but its "
                f"forecast carries {day.forecast_load.size} values"
            )
        if not np.isfinite(day.forecast_load).all():
            raise ValueError(f"day {day.label!r} forecast contains a non-finite value")
        if (day.forecast_load < 0.0).any():
            raise ValueError(
                f"day {day.label!r} forecast contains a negative load; the no-export row "
                "would make that interval unsatisfiable, and a negative energy forecast "
                "is not a forecast of anything"
            )
        cursor = day.stop
    if cursor != n:
        raise ValueError(
            f"days cover {cursor} intervals, the horizon has {n}: the partition must "
            "cover the whole evaluation window or the uncovered intervals have no plan"
        )

    capacity = spec.capacity_kwh
    soc_min_frac = spec.soc_min_frac
    soc_max_frac = spec.soc_max_frac
    value = float(terminal_value_eur_per_kwh)
    init_kwh = spec.soc_bound_kwh("init")
    # Seed protection is HORIZON-level, not day-level, and getting that wrong is a
    # functional bug rather than a conservatism.  An earlier draft of this policy floored
    # each day at that day's opening SOC; energy the policy had *paid for* on day D then
    # became unspendable "seed" on day D+1, so a battery that filled up simply froze —
    # observed on a synthetic fixture where the policy charged to 100 % on day one, sat
    # idle for the rest of the window and lost to the causal rule while holding 4.6 kWh it
    # was forbidden to use.  The floor is therefore fixed at the horizon's opening seed,
    # exactly ``max(soc_min, initial SOC)``, which is the same common-feasible-set floor
    # the causal policy and the free oracle use: what the window was handed is not the
    # policy's to spend, and everything it bought after the window opened is.
    horizon_floor_kwh = spec.causal_floor_kwh()
    # Starting at (or within noise of) the ceiling means seed protection pins the battery
    # to a single point: no discharge without spending the seed, no charge without
    # breaching the window.  That is not a dispatch problem to solve, it is a degenerate
    # spec, and it is reported instead of being smuggled into an invalid BatterySpec.
    frozen_by_seed = horizon_floor_kwh >= spec.soc_bound_kwh("max") - _SOC_NOISE_KWH
    full_charge = np.zeros(n, dtype=float)
    full_discharge = np.zeros(n, dtype=float)
    per_day: list[dict[str, Any]] = []
    carried_kwh = init_kwh
    rollup_meter_cost = 0.0
    counters = {
        "solved": 0,
        "certified": 0,
        "infeasible": 0,
        "no_incumbent": 0,
        "time_limit": 0,
        "skipped_no_usable_intervals": 0,
        "frozen_by_seed": 0,
        "clipped": 0,
    }
    clip_charge_total = 0.0
    clip_discharge_total = 0.0
    gap_max = 0.0

    for day in days:
        lo, hi = day.start, day.stop
        m = hi - lo
        day_usable = horizon.usable[lo:hi]
        record: dict[str, Any] = {
            "local_day": day.label,
            "intervals": int(m),
            "usable_intervals": int(day_usable.sum()),
            "soc_start_kwh": float(carried_kwh),
            "soc_end_kwh": float(carried_kwh),
            "forecast": dict(day.forecast),
            "solver_status": "not_solved",
            "bound_kind": "none",
            "is_certified": False,
            "mip_gap": None,
            "day_objective_eur": None,
            "charge_kwh_planned": 0.0,
            "discharge_kwh_planned": 0.0,
            "charge_kwh": 0.0,
            "discharge_kwh": 0.0,
            "charge_clipped_kwh": 0.0,
            "discharge_clipped_kwh": 0.0,
            "meter_cost_eur": 0.0,
            "objective_eur": 0.0,
            "degradation": "none",
            "note": None,
        }

        if not day_usable.any():
            # Hold SOC, no flows.  Not a solver failure: an all-gap day has no cost
            # basis, so there is nothing to optimise and nothing to spend.
            counters["skipped_no_usable_intervals"] += 1
            record["degradation"] = "skipped_no_usable_intervals"
            record["note"] = (
                "every interval of this local day is gap-excluded, so no MILP was built "
                "and SOC was carried unchanged with zero flow; a gap-excluded interval "
                "contributes no cost to any policy, so this day is neutral in the "
                "comparison rather than missing from it"
            )
            per_day.append(record)
            continue

        # Clamp the carried SOC into the spec's own window: this loop accumulates
        # scalars while the kernel reports a cumsum, and the two can disagree in the
        # last bit.  Un-clamped, a battery parked exactly at soc_max could hand a
        # fraction of 1 + 1e-16 to validate_spec and kill the run over noise.  The
        # reported trajectory is the kernel's, never this loop's.
        carried_frac = min(max(carried_kwh / capacity, soc_min_frac), soc_max_frac)
        if frozen_by_seed:
            # Nothing to decide: seed protection forbids spending the opening charge, and
            # the opening charge already fills the window.  Same zero-flow outcome as the
            # causal rule reaches on the same spec, reported as its own degradation class
            # so it can never be mistaken for an optimised day.
            counters["frozen_by_seed"] += 1
            day_horizon = Horizon(
                interval_seconds=horizon.interval_seconds,
                prices=horizon.prices[lo:hi],
                load_kwh=horizon.load_kwh[lo:hi],
                usable=day_usable,
                import_limit_kw=horizon.import_limit_kw,
            )
            record["soc_end_kwh"] = float(carried_kwh)
            record["meter_cost_eur"] = float(horizon_cost_eur(day_horizon, day_horizon.load_kwh))
            rollup_meter_cost += record["meter_cost_eur"]
            record["objective_eur"] = record["meter_cost_eur"]
            record["degradation"] = "frozen_by_seed_at_ceiling"
            record["note"] = (
                "the opening seed is at the SOC ceiling, so under seed protection nothing "
                "can be charged and nothing may be spent: zero flows, SOC held. The causal "
                "rule reaches the same zero-flow outcome on the same spec"
            )
            per_day.append(record)
            continue
        day_spec = dataclasses.replace(
            spec,
            initial_soc_frac=carried_frac,
            terminal_soc_frac=None,
        )
        floor_kwh = horizon_floor_kwh
        # The decision horizon carries the FORECAST load: that is the whole claim.
        forecast_horizon = Horizon(
            interval_seconds=horizon.interval_seconds,
            prices=horizon.prices[lo:hi],
            load_kwh=day.forecast_load,
            usable=day_usable,
            import_limit_kw=horizon.import_limit_kw,
        )
        # …and the same day on the MEASURED load is what the plan is repaired against
        # and what its cost is finally computed on.
        measured_horizon = Horizon(
            interval_seconds=horizon.interval_seconds,
            prices=horizon.prices[lo:hi],
            load_kwh=horizon.load_kwh[lo:hi],
            usable=day_usable,
            import_limit_kw=horizon.import_limit_kw,
        )

        try:
            solved = solve_oracle(
                day_spec,
                forecast_horizon,
                time_limit_s=time_limit_s,
                mip_rel_gap=mip_rel_gap,
                mip_abs_gap=mip_abs_gap,
                terminal_mode="free",
                terminal_value_eur_per_kwh=value,
                # The horizon's seed floor, not this day's opening SOC: see the docstring.
                floor_kwh=floor_kwh,
            )
        except OracleError as exc:
            text = str(exc).lower()
            kind = "infeasible" if "infeasible" in text else "no_incumbent"
            counters[kind] += 1
            record["degradation"] = f"{kind}_hold_soc_no_charging"
            record["note"] = f"{type(exc).__name__}: {exc}"
            # Flows stay zero for this day: hold SOC, charge nothing, spend nothing.
            # The meter cost is therefore the no-battery cost of this day, and the
            # terminal value neither rewards nor punishes an unchanged SOC.
            record["soc_end_kwh"] = float(carried_kwh)
            # Zero flows, so net import equals the measured load on every interval.
            record["meter_cost_eur"] = float(
                horizon_cost_eur(measured_horizon, measured_horizon.load_kwh)
            )
            rollup_meter_cost += record["meter_cost_eur"]
            record["objective_eur"] = record["meter_cost_eur"]
            carried_kwh = record["soc_end_kwh"]
            per_day.append(record)
            continue

        status = str(getattr(solved, "solver_status", "feasible"))
        record["solver_status"] = status
        record["bound_kind"] = str(getattr(solved, "bound_kind", "none"))
        record["is_certified"] = bool(getattr(solved, "is_certified", False))
        gap = getattr(solved, "solver_gap", None)
        record["mip_gap"] = None if gap is None else float(gap)
        if gap is not None:
            gap_max = max(gap_max, float(gap))
        if status == "time_limit":
            counters["time_limit"] += 1
            record["degradation"] = "time_limit_incumbent"
        else:
            counters["solved"] += 1
        if record["is_certified"]:
            counters["certified"] += 1
        record["day_objective_eur"] = float(solved.cost_eur)

        charge, discharge, end_soc, clip_c, clip_d = _clip_plan_to_model_bounds(
            day_spec,
            measured_horizon,
            np.asarray(solved.charge_kwh, dtype=float),
            np.asarray(solved.discharge_kwh, dtype=float),
            floor_kwh=floor_kwh,
        )
        record["charge_kwh_planned"] = float(np.asarray(solved.charge_kwh, dtype=float).sum())
        record["discharge_kwh_planned"] = float(np.asarray(solved.discharge_kwh, dtype=float).sum())
        if clip_c > 1e-12 or clip_d > 1e-12:
            counters["clipped"] += 1
            record["charge_clipped_kwh"] = float(clip_c)
            record["discharge_clipped_kwh"] = float(clip_d)
            clip_charge_total += float(clip_c)
            clip_discharge_total += float(clip_d)

        violations = check_feasible(
            day_spec, measured_horizon, charge, discharge, enforce_terminal_soc=False
        )
        if violations:
            # Unreachable by construction: the clip pass uses the same bounds the
            # checker tests.  Reaching here means the two disagree, which is a bug.
            raise BatteryPhysicsError(
                f"day {day.label!r} was repaired against the model bounds and the "
                f"checker still rejects it: {'; '.join(violations)}"
            )

        full_charge[lo:hi] = charge
        full_discharge[lo:hi] = discharge
        grid = grid_import_kwh(measured_horizon, charge, discharge)
        record["meter_cost_eur"] = float(horizon_cost_eur(measured_horizon, grid))
        rollup_meter_cost += record["meter_cost_eur"]
        record["charge_kwh"] = float(charge.sum())
        record["discharge_kwh"] = float(discharge.sum())
        record["soc_end_kwh"] = float(end_soc)
        record["objective_eur"] = record["meter_cost_eur"] - value * (
            float(end_soc) - float(carried_kwh)
        )
        carried_kwh = float(end_soc)
        per_day.append(record)

    traced = simulate(spec, horizon, full_charge, full_discharge, enforce_terminal_soc=False)
    # The scoring value is the common benchmark value (default 0), not the internal
    # planning value V.  This is what makes the comparison with the oracle fair: both
    # use the same terminal valuation for the reported cost.
    scoring_value = float(scoring_terminal_value_eur_per_kwh)
    adjustment = -scoring_value * (float(traced.soc_kwh[-1]) - init_kwh)
    schedule = dataclasses.replace(
        traced,
        cost_eur=float(traced.meter_cost_eur) + adjustment,
        inventory_adjustment_eur=adjustment,
        terminal_value_eur_per_kwh=scoring_value,
        # "feasible" is the store's existing word for a plan that is physically
        # valid and not a proven optimum of the problem being scored.  The daily
        # MILPs are usually certified; the whole-horizon plan is not an optimum
        # of any horizon-length problem, so it claims no bound and is not
        # certified.  ``bound_kind="none"`` is what keeps benchmark.py from
        # publishing a spurious upper/lower cost bound for it.
        solver_status="feasible",
        bound_kind="none",
        is_certified=False,
        solver_gap=None,
        solver_time_limit_s=float(time_limit_s),
    )

    degraded = counters["infeasible"] + counters["no_incumbent"]
    # ``days_without_headroom`` is deliberately NOT folded into days_degraded_to_hold:
    # a frozen spec is not a solver failure and not a hold-on-bad-news, and counting it
    # there would let a reader read "0 degraded days" as "the optimiser found nothing".
    degraded_note = f"{degraded} of {len(per_day)} day(s) degraded this way" + (
        f"; {counters['frozen_by_seed']} further day(s) had no headroom in either "
        "direction because the opening seed already filled the SOC window"
        if counters["frozen_by_seed"]
        else ""
    )
    telemetry: dict[str, Any] = {
        "policy": "day_ahead",
        "day_count": len(per_day),
        "days_solved": counters["solved"],
        "days_at_time_limit": counters["time_limit"],
        "days_infeasible": counters["infeasible"],
        "days_no_incumbent": counters["no_incumbent"],
        "days_skipped_no_usable_intervals": counters["skipped_no_usable_intervals"],
        "days_without_headroom": counters["frozen_by_seed"],
        "days_degraded_to_hold": degraded,
        "degradation_note": (
            "a day whose MILP is infeasible or returns no incumbent holds SOC and "
            "charges nothing; a day whose intervals are all gap-excluded is skipped the "
            "same way. The run never aborts on one day and the oracle is never "
            f"substituted. {degraded_note}"
        ),
        "days_with_clipping": counters["clipped"],
        "charge_clipped_kwh": float(clip_charge_total),
        "discharge_clipped_kwh": float(clip_discharge_total),
        "clipping_note": (
            "a forecast-derived plan can exceed what the MEASURED load allows: discharge "
            "beyond it would export, charging beyond the measured-load headroom would "
            "breach the import ceiling. Both are clipped to the measured constraints "
            "chronologically (clipping a charge also removes energy a later discharge "
            "counted on), never re-optimised, and reported as energy the policy wanted "
            "and did not get"
        ),
        "terminal_value_eur_per_kwh": value,
        "terminal_value_role": (
            "the internal daily planning value: end-of-day SOC is credited at this "
            "EUR/DC-kWh value inside each daily objective, and the per-day valuations "
            "telescope into one horizon-level adjustment because day D's end SOC is day "
            "D+1's start SOC. This is NOT the common benchmark scoring value; see "
            "scoring_terminal_value_eur_per_kwh"
        ),
        "scoring_terminal_value_eur_per_kwh": scoring_value,
        "scoring_terminal_value_role": (
            "the common benchmark scoring value (default 0.0), used for the reported "
            "Schedule.cost_eur and inventory_adjustment_eur. This is the same value the "
            "oracle uses, so the comparison is on the same basis. The internal planning "
            "value V is used only inside the daily objectives to prevent the battery from "
            "emptying at midnight"
        ),
        "internal_daily_objective_sum_eur": float(
            sum(d["objective_eur"] for d in per_day if d["objective_eur"] is not None)
        ),
        "internal_daily_objective_note": (
            "the sum of the per-day objectives, each valued at the internal planning "
            "value V. This is NOT the common score; the reported cost_eur uses the "
            "scoring_terminal_value_eur_per_kwh instead. The internal sum is recorded for "
            "diagnostics only"
        ),
        "myopia_note": (
            "myopic by construction: each solve sees one local day only, so it cannot "
            "plan inter-day arbitrage and is expected to lose value the oracle captures "
            "across a day boundary. This is a property of the policy, not a defect to "
            "tune away"
        ),
        "time_limit_s_per_day": float(time_limit_s),
        "days_certified_optimal": counters["certified"],
        "max_observed_mip_gap": float(gap_max),
        "meter_cost_rollup_abs_diff_eur": abs(rollup_meter_cost - float(traced.meter_cost_eur)),
        "wall_clock_note": (
            "per-day solver wall-clock time is deliberately NOT recorded. Run artifacts "
            "are content-addressed and written no-clobber "
            "(energy_baseline.paths.write_part_atomic refuses the same run id with "
            "different bytes), so a timing field would make an identical re-run raise "
            "FileExistsError and destroy byte-level reproducibility. Deterministic per-day "
            "telemetry (solver_status, is_certified, mip_gap) and the count of days that "
            "consumed their time limit are recorded instead; a day that consumed its limit "
            "is also a disclosure that that day's plan is machine-speed dependent"
        ),
        "per_day": per_day,
    }
    return schedule, telemetry
