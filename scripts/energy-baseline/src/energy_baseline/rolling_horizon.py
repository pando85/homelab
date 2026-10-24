"""Rolling horizon dispatcher — configurable replan cadence, partial execution, SOC carry.

Two modes share one code path and one publication rule:

* **Legacy two-event schedule** (``replan_interval_slots is None``, the default):
  two planning events per local day (Europe/Madrid):

  * **14:00** — plan from 14:00 to midnight ending D+1 (~34 h).  Prices for the
    rest of day D are exact; prices for D+1 are a noisy estimate (sigma=0 is the
    exact-price control).
  * **20:30** — re-plan from 20:30 to midnight ending D+1 (~27.5 h).  Prices for
    the rest of D **and** all of D+1 are now exact (conservative: first call after
    the 20:15–20:20 publication window).

* **Configurable cadence** (``replan_interval_slots is not None``): one event every
  *replan_interval_slots* intervals, each planning *horizon_slots* ahead (default
  96 = 24 h at 15-min intervals, inside ADR-0001's 24–48 h band), executing only
  the next *replan_interval_slots* intervals.  The 15-minute cadence
  (``replan_interval_slots=1``, ``horizon_slots=96``) is the mode a real
  controller would run.

In both modes the **price information set is a function of the decision instant,
not of the cadence**.  The single publication rule (see :func:`_last_published_day`)
is: prices for local day ``D+1`` become exact at 20:30 on day ``D``, the first
quarter-hour boundary after the 20:15–20:20 publication window.  A solve at 09:00 on
day D sees day D exact and day D+1 estimated; a solve at 21:00 on day D sees both
exact.  The legacy 14:00/20:30 events and the configurable cadence derive their
``prices_exact_end_idx`` from this one rule, so the rule cannot drift between modes.

Each plan is executed **only until the next event** (legacy: 14:00 → 20:30,
20:30 → next 14:00; configurable: the next *replan_interval_slots* intervals).
The actual SOC is carried forward by walking the clipped plan chronologically,
so the next event's solve starts from the real state, not the planned one.

Warmup events cover ``[eval_start, first scheduled event)`` in legacy mode.  A
warmup never plans across a local midnight — when that window spans one it is
split at every midnight into one warmup per local day, each planning only the
day it starts in, with exact prices and no tomorrow.  So the first partial day
is not silently given future information it could not have had, and no plan is
ever shorter than the slice of it that gets executed.  Configurable-cadence mode
has no warmup events: the cadence starts at *eval_start_idx*.

Load forecasts have two modes that share one code path, selected by
``load_forecast_mode``:

* **frozen** (the dispatcher's default, what the legacy experiment and golden tests
  pin): computed once from training data (slot-position means over the trailing
  ``window_days`` local days, strictly before the cut) by
  :func:`build_frozen_load_profile` and passed pre-built into every solve.  The
  profile never advances through the evaluation window.
* **causal** (the canonical benchmark default): each event's forecast is built by
  :class:`CausalForecastProvider` at the event's decision instant, from a usable mask
  truncated at the event's own local-day start, using the SAME
  :func:`energy_baseline.policies.forecast_load_trailing_slot_mean` function the
  day-ahead policy uses.  So the bases are truly comparable, and a solve whose horizon
  spans into the next local day never reads that day's — or its own day's — measured
  load into the fit.  The next-day refresh happens only at the first event after that
  midnight.

Price noise is deterministic per ``(seed, local_date)`` via SHA-256 and has two
independent channels sharing that one derivation:

* **Within-day noise** ``e[t] ~ N(0, sigma)``, drawn per 15-minute slot.  It largely
  preserves the ordering of tomorrow's hours and averages out over a day, so it
  mainly stresses *which* hours an optimiser picks.
* **Day-level bias** ``b[day] ~ N(0, bias_sigma)``, one draw per local day applied to
  every slot of that day.  It leaves the within-day ordering untouched and instead
  moves tomorrow as a whole up or down — the way a real day-ahead proxy errs when a
  weather regime or a forward-component shift is misjudged — so it stresses the
  cross-day hold-versus-spend decision, which averaging does not correct.

``p_hat[t] = p_true[t] * (1 + b[day(t)] + e[t])``.  Either channel alone is off when
its sigma is 0; both at 0 is the exact-price control.

Scoring always uses **true** prices against the **actual** (clipped) grid
import — noisy prices influence planning only, never the reported cost.

**Multiplicity accounting.** An interval is typically covered by several
overlapping plan horizons (planned multiplicity) but executed exactly once.  Measured
on synthetic windows: mean 2.52 with a maximum of 4 at the legacy cadence, and mean
94.4 with a maximum of 96 for ``replan_interval_slots=1, horizon_slots=96`` (the mean
is a little under 96 only because the first and last few horizons are clipped by the
window edges).  Clipping totals therefore are *not* comparable across cadences.
``telemetry["planning_multiplicity_mean"]`` and
``telemetry["planning_multiplicity_max"]`` make this explicit so a reader does not
mistake planned multiplicity for curtailment.
"""

from __future__ import annotations

import hashlib
import math
from dataclasses import dataclass, replace
from datetime import UTC, date, datetime, time, timedelta
from typing import Any
from zoneinfo import ZoneInfo

import numpy as np

from .battery import (
    BatterySpec,
    Horizon,
    Schedule,
    simulate,
    validate_spec,
)
from .oracle import OracleError, solve_oracle
from .policies import (
    DayAheadDay,
    _clip_plan_to_model_bounds,
    contiguous_day_bounds,
)

__all__ = [
    "LOAD_FORECAST_MODE_CAUSAL",
    "LOAD_FORECAST_MODE_FROZEN",
    "LOAD_FORECAST_MODES",
    "RollingEvent",
    "RollingResult",
    "CausalForecastProvider",
    "build_frozen_load_profile",
    "compute_rolling_events",
    "generate_price_noise",
    "run_rolling_horizon",
    "run_day_ahead_with_frozen_forecast",
]

MADRID_TZ = "Europe/Madrid"

#: Load forecast mode names.  ``frozen`` computes slot-position means once from
#: training data strictly before the cut and reuses them for every solve; ``causal``
#: recomputes the trailing-slot mean before each local dispatch day using only
#: observations strictly before that day — the same function the day-ahead policy
#: uses, so the two rungs share a like-for-like load basis.
LOAD_FORECAST_MODE_FROZEN = "frozen"
LOAD_FORECAST_MODE_CAUSAL = "causal"
LOAD_FORECAST_MODES = (LOAD_FORECAST_MODE_FROZEN, LOAD_FORECAST_MODE_CAUSAL)
AFTERNOON_EVENT = (14, 0)
EVENING_EVENT = (20, 30)

_SOC_NOISE_KWH = 1e-9


# ---------------------------------------------------------------------------
# Data carriers
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class RollingEvent:
    """One planning event in the rolling horizon."""

    index: int
    """Sequential event number (0-based)."""

    local_day: date
    """Local calendar day of the event."""

    kind: str
    """``"warmup"``, ``"afternoon"`` (14:00), ``"evening"`` (20:30), or ``"replan"``.

    The first three are the legacy two-publication schedule; ``"replan"`` marks an event
    of a uniform-cadence roll (``replan_interval_slots`` set), whose decision instant is
    whatever interval the cadence landed on rather than a fixed wall-clock time.
    """

    start_idx: int
    """First interval index of the plan (and execution) window."""

    plan_end_idx: int
    """One past the last interval of the plan horizon."""

    exec_end_idx: int
    """One past the last interval executed from this plan."""

    prices_exact_end_idx: int
    """Prices are exact (true) up to this index; estimated from it to ``plan_end_idx``.

    Derived from the decision instant alone (see :func:`_last_published_day`), never
    from the cadence: at the legacy cadence it is the end of the event's own local day
    for the 14:00 solve and ``plan_end_idx`` for the 20:30 solve, and at any finer
    cadence it is the same boundary the 20:30 rule gives, evaluated at that solve's own
    clock time.  Equal to ``plan_end_idx`` means the whole plan used exact prices.
    """


@dataclass
class RollingResult:
    """Output of :func:`run_rolling_horizon`."""

    schedule: Schedule
    """Full traced schedule over the evaluation window."""

    telemetry: dict[str, Any]
    """Summary counters and diagnostics."""

    per_event: list[dict[str, Any]]
    """One record per planning event."""

    cost_eur: float
    """Total cost computed against TRUE prices and actual grid import."""

    meter_cost_eur: float
    """Metered energy cost (no inventory adjustment)."""

    noise_metadata: dict[str, Any]
    """Price noise diagnostics (sign flips, MAPE, etc.)."""

    forecast_metadata: dict[str, Any]
    """Frozen load forecast diagnostics."""


# ---------------------------------------------------------------------------
# Time helpers
# ---------------------------------------------------------------------------


def _build_time_index(interval_starts_utc: Any) -> dict[datetime, int]:
    """Map each interval's UTC start to its index."""
    return {dt: i for i, dt in enumerate(interval_starts_utc)}


def _local_dt_to_utc(
    year: int, month: int, day: int, hour: int, minute: int, tz: ZoneInfo
) -> datetime:
    """Build a local datetime and convert to UTC."""
    local = datetime(year, month, day, hour, minute, tzinfo=tz)
    return local.astimezone(UTC)


def _find_idx_or_none(time_index: dict[datetime, int], target_utc: datetime) -> int | None:
    """Look up an interval index; None if not found."""
    return time_index.get(target_utc)


def _find_idx_or_next(time_index: dict[datetime, int], target_utc: datetime, n: int) -> int:
    """Look up an interval index; if not found, return the next later index."""
    exact = time_index.get(target_utc)
    if exact is not None:
        return exact
    # Find the first interval at or after target_utc
    for dt, idx in sorted(time_index.items()):
        if dt >= target_utc:
            return idx
    return n


def _day_bounds_to_dict(day_bounds: list[tuple[Any, int, int]]) -> dict[date, tuple[int, int]]:
    """Map local day label to (start, stop) interval indices."""
    return {label: (start, stop) for label, start, stop in day_bounds}


def _as_local_date(label: Any) -> date:
    """Coerce a local-day label (``date`` or ISO string) to ``date``."""
    return label if isinstance(label, date) else date.fromisoformat(str(label))


def _as_utc(dt: datetime) -> datetime:
    """Treat a naive interval start as UTC; pass an aware one through unchanged."""
    return dt if dt.tzinfo is not None else dt.replace(tzinfo=UTC)


# ---------------------------------------------------------------------------
# Price publication rule (the single source of the information set)
# ---------------------------------------------------------------------------


def _last_published_day(decision_utc: datetime, tz: ZoneInfo) -> date:
    """Last local day whose prices are exact at a decision instant.

    THE publication rule, stated once.  Day-ahead prices for local day ``D+1`` are
    published in the 20:15–20:20 window on day ``D``; this module conservatively
    treats them as available from :data:`EVENING_EVENT` (20:30), the first
    quarter-hour boundary after that window.  So at a decision instant on day ``D``:

    * local time ``< 20:30`` → ``D`` is exact, ``D+1`` and later are estimated;
    * local time ``>= 20:30`` → ``D`` **and** ``D+1`` are exact, ``D+2`` onward
      are estimated.

    The answer is a function of the decision instant alone — never of the replan
    cadence — which is what lets the legacy 14:00/20:30 schedule and a 15-minute
    cadence describe the same information honestly.  Both derive their
    ``prices_exact_end_idx`` from this function, so neither carries a second copy
    of the rule that could drift.

    The comparison is on the local wall clock of the instant.  On a fall-back day
    the repeated hour is never near the boundary, so ``fold`` cannot change the
    answer here; :func:`_clock_slot_position` is where the fold matters.
    """
    local = _as_utc(decision_utc).astimezone(tz)
    if (local.hour, local.minute) >= EVENING_EVENT:
        return local.date() + timedelta(days=1)
    return local.date()


def _prices_exact_end_for_decision(
    decision_utc: datetime,
    plan_end_idx: int,
    day_starts: list[tuple[date, int]],
    tz: ZoneInfo,
) -> int:
    """First index at which the plan must use estimated prices, clipped to the plan.

    Looks the decision instant up through :func:`_last_published_day` and then
    through ``day_starts`` — ``(local_day, first_index)`` pairs in ascending day
    order, i.e. the local-day partition of the dataset — to find the first
    interval of the first day that is NOT yet published.  Everything before that
    index is exact, everything from it to ``plan_end_idx`` is estimated.

    Returns ``plan_end_idx`` when no unpublished day appears inside the plan,
    which is the "all prices exact" case the legacy evening and warmup events
    already mean.
    """
    last_exact = _last_published_day(decision_utc, tz)
    # First day strictly after the last published one, if the dataset has it.
    pos = _bisect_day_start(day_starts, last_exact)
    if pos >= len(day_starts):
        return plan_end_idx
    return min(day_starts[pos][1], plan_end_idx)


def _bisect_day_start(day_starts: list[tuple[date, int]], after: date) -> int:
    """Index of the first ``(day, start)`` entry whose day is strictly after *after*."""
    lo, hi = 0, len(day_starts)
    while lo < hi:
        mid = (lo + hi) // 2
        if day_starts[mid][0] <= after:
            lo = mid + 1
        else:
            hi = mid
    return lo


def _day_starts(local_days: Any) -> list[tuple[date, int]]:
    """Ascending ``(local_day, first_interval_index)`` partition of the dataset."""
    bounds = contiguous_day_bounds(list(local_days))
    return sorted(
        ((_as_local_date(label), start) for label, start, _stop in bounds),
        key=lambda item: item[0],
    )


def _cadence_telemetry(
    replan_interval_slots: int | None,
    horizon_slots: int | None,
    interval_seconds: float,
) -> dict[str, Any]:
    """Describe the cadence a run used, for telemetry and artifacts.

    ``None`` means the legacy two-publication schedule, spelled out here rather than
    left implicit so an artifact states which controller it measured.  The legacy
    horizon is reported as ``None`` deliberately: it is DST-dependent (34 h at 14:00 on
    a normal day, 35 h before a fall-back day, ~27.5 h at 20:30) and clipped by the end
    of the evaluation window, so any single number would be a fabrication.  A uniform
    cadence's horizon is fixed in slots and converts honestly to hours.
    """
    if replan_interval_slots is None:
        return {
            "replan_interval_slots": None,
            "replan_cadence": f"{AFTERNOON_EVENT[0]:02d}:{AFTERNOON_EVENT[1]:02d} and "
            f"{EVENING_EVENT[0]:02d}:{EVENING_EVENT[1]:02d} local (two daily publications)",
            "horizon_slots": None,
            "horizon_hours_nominal": None,
        }
    slots = 96 if horizon_slots is None else horizon_slots
    return {
        "replan_interval_slots": int(replan_interval_slots),
        "replan_cadence": f"every {int(replan_interval_slots)} interval(s)",
        "horizon_slots": int(slots),
        "horizon_hours_nominal": slots * float(interval_seconds) / 3600.0,
    }


def _infer_interval_seconds(interval_starts_utc: Any) -> float:
    """Modal length of the interval grid, in seconds.

    The median of **positive** consecutive deltas rather than ``starts[1] - starts[0]``
    so a data gap (one large delta) cannot redefine the grid for every position: a gap
    moves one delta, not the distribution.  Deltas are taken on the UTC axis, where the
    grid is uniform across DST — the transitions stretch *local* days, not UTC spacing.
    """
    starts = list(interval_starts_utc)
    if len(starts) < 2:
        raise ValueError(
            "interval_starts_utc needs at least two timestamps to infer the interval length"
        )
    base = _as_utc(starts[0])
    offsets = np.array([(_as_utc(s) - base).total_seconds() for s in starts], dtype=float)
    deltas = np.diff(offsets)
    positive = deltas[deltas > 0.0]
    if positive.size == 0:
        raise ValueError("interval_starts_utc is not increasing; slot positions are undefined")
    interval = float(np.median(positive))
    if interval <= 0.0:
        raise ValueError(f"interval length must be positive, got {interval}")
    return interval


def _clock_slot_position(
    utc_dt: datetime,
    tz: ZoneInfo,
    interval_seconds: float,
) -> int:
    """Slot position: intervals elapsed since the local midnight of its own local day.

    ``k = (t_utc − utc_instant_of_local_midnight(local_day(t))) / interval_length``.

    The midnight instant is built with ``datetime.combine(local_day, 00:00, tzinfo=tz)``,
    which resolves the **fold=0** occurrence — the first instant whose local date is that
    day — and the subtraction is done between two aware instants, so it measures real
    elapsed time rather than naive wall-clock difference.

    Why not ``local_dt − local_dt.replace(hour=0)``, which is the obvious way to write
    "offset from local midnight": ``replace`` keeps the *current* utcoffset, so after a
    fall-back it re-anchors to 01:00 CET (= 00:00 UTC) and every post-transition slot is
    shifted down by the lost hour.  On 2025-10-26 in Europe/Madrid that collapses the
    25-hour day onto positions 0..95, duplicating 8..11 and silently borrowing those
    slots' learned evening means for a clock time that is actually an hour later.

    Consequences of this rule, both pinned by tests:

    * a whole normal day gives 0..95, identical to the offset-within-dayBounds it replaced;
    * a **partial** day (a slice edge that is not local midnight) keeps its true clock
      offsets — a day entered at 02:00 CEST starts at 8, not 0;
    * the 100-slot fall-back day runs 0..99 with the repeated hour unambiguous: the first
      02:00 (fold=0, CEST) is 8 and the second (fold=1, CET) is 12;
    * the 92-slot spring-forward day runs 0..91 with no gap.

    Positions 96..99 therefore exist only on a fall-back day, which is exactly the set that
    a training window of normal days never learned and which falls back to the pooled mean.
    """
    local_day = _as_utc(utc_dt).astimezone(tz).date()
    midnight_utc = datetime.combine(local_day, time.min, tzinfo=tz).astimezone(UTC)
    elapsed_seconds = (_as_utc(utc_dt) - midnight_utc).total_seconds()
    return int(round(elapsed_seconds / interval_seconds))


# ---------------------------------------------------------------------------
# Price noise
# ---------------------------------------------------------------------------


def _day_rng_seeds(seed: int, day_str: str) -> tuple[int, int]:
    """Derive the two per-local-day RNG seeds from one SHA-256 digest.

    ``sha256(f"{seed}:{local_date}")`` gives 32 bytes; the day's **per-slot noise**
    is seeded from ``digest[:8]`` and its **day-level bias** from ``digest[8:16]``.
    The slices are disjoint, and SHA-256 output carries no correlation between
    disjoint regions, so the two channels are independent streams rather than one
    stream drawn in an order-dependent way — which matters because a bias that came
    from the *same* generator as the within-day noise would be consumed in a fixed
    slot order and could correlate with the first slots of the day.

    ``digest[:8]`` is exactly what the per-slot channel used before the bias existed,
    so ``bias_sigma=0`` reproduces earlier noisy arrays bit-for-bit: results already
    measured on disk stay comparable.  Only the *number of draws* taken from a stream
    could ever change an existing sequence, and each channel now owns its own
    generator, so neither can perturb the other.
    """
    digest = hashlib.sha256(f"{seed}:{day_str}".encode()).digest()
    return int.from_bytes(digest[:8], "big"), int.from_bytes(digest[8:16], "big")


def generate_price_noise(
    true_prices: np.ndarray,
    local_days: Any,
    sigma: float,
    seed: int = 0,
    *,
    bias_sigma: float = 0.0,
    day_bounds: list[tuple[Any, int, int]] | None = None,
    estimated_slots_mask: np.ndarray | None = None,
) -> tuple[np.ndarray, dict[str, Any]]:
    """Generate noisy price forecasts ``p_hat[t] = p[t] * (1 + b[day(t)] + e[t])``.

    Two independent channels, both deterministic per ``(seed, local_date)`` via
    SHA-256 (see :func:`_day_rng_seeds`):

    * ``e[t] ~ N(0, sigma)`` — one draw **per 15-minute slot**.  A complete day's
      worth is generated for each local day (96/92/100 slots depending on DST),
      regardless of the test horizon.
    * ``b[day] ~ N(0, bias_sigma)`` — **one draw per local day**, applied to every
      slot of that day.  It scales the whole day uniformly, so it leaves the
      within-day ordering of hours untouched and instead moves tomorrow relative to
      today: the cross-day hold-versus-spend decision, which averaging over slots
      does not correct.

    ``sigma=0`` with ``bias_sigma=0`` is the exact-price control (everything zero).
    ``sigma=0`` with ``bias_sigma>0`` is meaningful on its own: the estimate is then a
    pure day-level scaling of truth, with no within-day distortion at all.

    Args:
        true_prices: True price array
        local_days: Local day labels
        sigma: Per-slot noise standard deviation (must be >= 0)
        seed: Random seed
        bias_sigma: Per-local-day multiplicative bias standard deviation
            (must be >= 0). ``0`` reproduces the i.i.d.-only model exactly.
        day_bounds: Day boundaries for noise generation
        estimated_slots_mask: Optional boolean mask indicating which slots are
            actually estimated (noisy). If provided, stats are computed only on
            these slots. If None, stats computed on all slots.

    Returns ``(noisy_prices, metadata)`` where metadata records sign flips, count of
    zero-denominator intervals, the per-day bias actually applied (with its mean and
    standard deviation), and per-day seed provenance.
    """
    if sigma < 0.0:
        raise ValueError(f"sigma must be >= 0, got {sigma}")
    if not math.isfinite(sigma):
        raise ValueError(f"sigma must be finite, got {sigma}")
    if bias_sigma < 0.0:
        raise ValueError(f"bias_sigma must be >= 0, got {bias_sigma}")
    if not math.isfinite(bias_sigma):
        raise ValueError(f"bias_sigma must be finite, got {bias_sigma}")

    prices = np.asarray(true_prices, dtype=float).reshape(-1)
    if sigma == 0.0 and bias_sigma == 0.0:
        # Same key set as the noisy branch so a reader compares runs on one schema:
        # an exact forecast is simply perfect on every metric.
        considered = (
            int(np.count_nonzero(estimated_slots_mask))
            if estimated_slots_mask is not None
            else int(prices.size)
        )
        return prices.copy(), {
            "sigma": 0.0,
            "bias_sigma": 0.0,
            "seed": int(seed),
            "sign_flips": 0,
            "zero_price_count": int((prices == 0.0).sum()),
            "mape": 0.0,
            "mae": 0.0,
            "frac_within_10pct": 1.0,
            "frac_within_20pct": 1.0,
            "noise_applied": False,
            "bias_days_applied": 0,
            "bias_mean_applied": 0.0,
            "bias_std_applied": 0.0,
            "stats_slots_considered": considered,
            "stats_on_estimated_slots_only": estimated_slots_mask is not None,
            "per_day": [],
            "note": (
                "sigma=0 and bias_sigma=0: exact prices, no noise applied. Every accuracy "
                "metric is at its ideal value by construction (the forecast IS the truth); "
                "the keys are kept so an exact row and a noisy row have one schema"
            ),
        }

    if day_bounds is None:
        raise ValueError("day_bounds is required for noise generation")

    noisy = prices.copy()
    per_day_seeds: list[dict[str, Any]] = []
    # Bias actually applied to a day that was estimated at least once.  Only those
    # feed the summary statistics, matching the "estimated slots only" convention of
    # the accuracy metrics below: a day whose prices were known exactly contributes no
    # forecast error, so its bias draw is not part of the realised accuracy either.
    applied_biases: list[float] = []

    # Noise is drawn for whole local days (96/92/100 slots) so any day a plan horizon
    # reaches has a deterministic curve; only the days actually estimated are recorded,
    # otherwise a two-year dataset would bury the days that mattered.
    mask = (
        np.asarray(estimated_slots_mask, dtype=bool) if estimated_slots_mask is not None else None
    )

    for label, start, stop in day_bounds:
        day_str = str(label)
        noise_seed, bias_seed = _day_rng_seeds(seed, day_str)
        m = stop - start

        # One scalar for the whole local day: every slot of this day is scaled by the
        # same factor, which is what makes it a regime error rather than slot noise.
        # It is drawn even when no slot of this day is estimated, because the draw must
        # depend on (seed, date) alone — conditioning it on which load-window variant
        # happens to call would make the bias differ between paired runs.
        if bias_sigma > 0.0:
            bias = float(np.random.default_rng(bias_seed).normal(0.0, bias_sigma))
        else:
            bias = 0.0

        if sigma > 0.0:
            noise = np.random.default_rng(noise_seed).normal(0.0, sigma, size=m)
        else:
            # No draw at all rather than a scale-0 one: the within-day channel is off,
            # and a pure day-level scaling must not consume any variates.
            noise = np.zeros(m, dtype=float)

        day_prices = prices[start:stop]
        noisy_day = day_prices * (1.0 + bias + noise)
        noisy[start:stop] = noisy_day
        if mask is None or mask[start:stop].any():
            # Sign flips reported on the estimated subset only; a flip is a
            # positive true price whose forecast went negative (or vice versa).
            considered = np.ones(m, dtype=bool) if mask is None else mask[start:stop]
            sign_changed = ((day_prices > 0) & (noisy_day < 0)) | (
                (day_prices < 0) & (noisy_day > 0)
            )
            applied_biases.append(bias)
            per_day_seeds.append(
                {
                    "local_day": day_str,
                    "intervals": m,
                    "estimated_intervals": int(considered.sum()),
                    "sign_flips": int(sign_changed[considered].sum()),
                    "bias_applied": bias,
                }
            )

    # Compute stats only on estimated slots if mask provided
    if mask is not None:
        stats_prices = prices[mask]
        stats_noisy = noisy[mask]
    else:
        stats_prices = prices
        stats_noisy = noisy

    # The headline figure describes the slots that were actually estimated.
    stats_sign_flips = int(
        (((stats_prices > 0) & (stats_noisy < 0)) | ((stats_prices < 0) & (stats_noisy > 0))).sum()
    )

    zero_denom = int((stats_prices == 0.0).sum())
    # MAPE and the percentage-within metrics need a denominator, so they are taken
    # over the non-zero-priced subset only — a zero true price has no relative error.
    nonzero_mask = stats_prices != 0.0
    if stats_prices.size:
        mae: float | None = float(np.abs(stats_noisy - stats_prices).mean())
    else:
        # Nothing was estimated at all (a window whose events are all warmups or
        # evenings).  An empty mean is NaN, which is not JSON-serialisable and would
        # read as a measured error of "not a number" rather than "no forecast made".
        mae = None
    if nonzero_mask.any():
        abs_err = np.abs(stats_noisy[nonzero_mask] - stats_prices[nonzero_mask])
        denom = np.abs(stats_prices[nonzero_mask])
        mape: float | None = float((abs_err / denom).mean())
        within_10: float | None = float((abs_err <= 0.1 * denom).mean())
        within_20: float | None = float((abs_err <= 0.2 * denom).mean())
    else:
        mape = None
        within_10 = None
        within_20 = None

    metadata: dict[str, Any] = {
        "sigma": float(sigma),
        "bias_sigma": float(bias_sigma),
        "seed": int(seed),
        "sign_flips": stats_sign_flips,
        "zero_price_count": zero_denom,
        "mape": mape,
        "mae": mae,
        "frac_within_10pct": within_10,
        "frac_within_20pct": within_20,
        "noise_applied": True,
        # The bias actually realised on the days that were estimated — what was drawn,
        # not what was requested.  Sample std over one day is 0.0 by definition, so this
        # never produces an empty/ddof NaN: an estimated day always carries a number even
        # when bias_sigma=0 drew nothing (then it is exactly 0.0 on every day).
        "bias_days_applied": len(applied_biases),
        "bias_mean_applied": float(np.mean(applied_biases)) if applied_biases else 0.0,
        "bias_std_applied": float(np.std(applied_biases)) if applied_biases else 0.0,
        "stats_slots_considered": int(stats_prices.size),
        "stats_on_estimated_slots_only": estimated_slots_mask is not None,
        "per_day": per_day_seeds,
        "note": (
            "p_hat = p * (1 + b[day] + e[slot]); e ~ N(0, sigma) per 15-minute slot, "
            "b ~ N(0, bias_sigma) once per local day, drawn from independent streams of "
            "SHA-256(seed:date)[:8] and [8:16]. A shared (seed, date) draw makes the same "
            "day's bias identical across paired variants. Synthetic mechanism: uses future "
            "truth solely to construct noisy forecast; not measured, no proof deployable."
        ),
    }
    return noisy, metadata


# ---------------------------------------------------------------------------
# Frozen load forecast
# ---------------------------------------------------------------------------


def build_frozen_load_profile(
    load_kwh: np.ndarray,
    local_days: Any,
    usable: np.ndarray,
    *,
    training_end_idx: int,
    window_days: int = 28,
    interval_starts_utc: Any | None = None,
    tz: ZoneInfo | None = None,
) -> tuple[np.ndarray, dict[str, Any]]:
    """Build a frozen load forecast from training data only (strictly before cut).

    Computes slot-position means from LOCAL DATES strictly before training_end_idx.
    The learned profile is then applied across ALL evaluation days (no re-estimation).

    Slot position is derived from the **local wall clock** by
    :func:`_clock_slot_position`: ``k`` is the number of intervals elapsed since the
    local midnight *instant* of the interval's own local day.  This is robust to
    truncation at either slice edge — a partial first day (where the slice does not
    start at local midnight) keeps its true clock offsets instead of being relabelled
    from 0, which would shift its whole forecast by the number of missing morning slots.

    DST convention: position counts real elapsed time from the **fold=0** local
    midnight, and each UTC instant maps to exactly one position.  On the 2025-10-26
    fall-back day the repeated hour is therefore unambiguous (first 02:00 CEST -> 8,
    second 02:00 CET -> 12) and the 100-slot day runs 0..99; the 92-slot spring-forward
    day runs 0..91 with no hole.  Positions never learned fall back to the pooled mean.

    The same clock-derived rule is applied on both the training and the evaluation
    side, so the learned means keep their existing indexing: for a local day present in
    full, these positions are exactly the offsets within day bounds they always were, and
    only truncated boundary days move.  See ``hack/ROLLING_HORIZON_STATUS.md``
    (*Slot positioning*) for the table and the spring-forward caveat.

    Args:
        load_kwh: Full load array (train + eval)
        local_days: Local day labels for each interval
        usable: Usability mask
        training_end_idx: First index of evaluation period (all prior data is training)
        window_days: Trailing window in local days for slot means
        interval_starts_utc: **Required.** UTC timestamps for each interval, used to
            derive clock-based slot positions and the training hash.
        tz: Timezone for local-wall-clock conversion.  Defaults to Europe/Madrid.

    Returns:
        ``(forecast, metadata)`` where forecast is the same length as load_kwh.  The
        evaluation region ``[training_end_idx:]`` is the actual forecast.  The training
        region below the cut is filled with the *same* learned means (never re-derived
        from anything after the cut): callers legitimately solve windows that start
        before the cut, and a zero there would be read as "no load expected" instead of
        "the profile applies here too".
    """
    if interval_starts_utc is None:
        raise ValueError(
            "interval_starts_utc is required for correct slot positioning: "
            "without UTC timestamps, slot positions would be derived from run "
            "offsets within the sliced array, which are wrong when the slice "
            "does not start at local midnight"
        )

    if tz is None:
        tz = ZoneInfo(MADRID_TZ)

    load = np.asarray(load_kwh, dtype=float).reshape(-1)
    n = load.size

    if training_end_idx <= 0:
        raise ValueError(f"training_end_idx must be > 0, got {training_end_idx}")
    if training_end_idx >= n:
        raise ValueError(f"training_end_idx={training_end_idx} >= dataset size {n}")

    # Derive the interval length from the grid itself, so positioning needs no extra
    # argument and cannot disagree with the timestamps it is reading.
    interval_seconds = _infer_interval_seconds(interval_starts_utc)

    # Get day bounds for training data only
    train_local_days = list(local_days[:training_end_idx])
    train_days = contiguous_day_bounds(train_local_days)
    train_usable = usable[:training_end_idx]
    train_load = load[:training_end_idx]

    if not train_days:
        raise ValueError(
            f"no local day before training_end_idx={training_end_idx}: a frozen "
            "profile needs training data to learn slot means from"
        )

    # Compute slot-position means from the last window_days of training data
    # Only use days within the window for computing the frozen profile
    recent_train_days = train_days[-window_days:] if len(train_days) > window_days else train_days

    slot_sums: dict[int, float] = {}
    slot_counts: dict[int, int] = {}
    for _label, start, stop in recent_train_days:
        for idx_in_slice in range(start, stop):
            if train_usable[idx_in_slice]:
                k = _clock_slot_position(interval_starts_utc[idx_in_slice], tz, interval_seconds)
                slot_sums[k] = slot_sums.get(k, 0.0) + train_load[idx_in_slice]
                slot_counts[k] = slot_counts.get(k, 0) + 1

    # Compute means with pooled fallback
    pooled_sum = sum(slot_sums.values())
    pooled_count = sum(slot_counts.values())
    pooled_mean = pooled_sum / pooled_count if pooled_count > 0 else 0.0

    slot_means: dict[int, float] = {
        k: (slot_sums[k] / slot_counts[k] if slot_counts[k] > 0 else pooled_mean) for k in slot_sums
    }
    if not slot_means:
        raise ValueError(
            f"no usable training interval in the last {len(recent_train_days)} local day(s): "
            "a frozen profile of all-zero means is not a forecast, it is a missing input"
        )

    # Slot position is the clock-derived offset inside the interval's OWN local day,
    # so DST days (92 or 100 intervals) line up on their own clock offsets; positions
    # never learned fall back to the pooled mean.
    forecast = np.zeros(n, dtype=float)

    # The training region is filled with the same in-sample values.  It is not a
    # forecast and nothing downstream solves over it: the cut at training_end_idx is what
    # keeps test data out of the learned means, and these placeholders only keep callers
    # that index below the cut from reading a zero as "no load expected".
    for _label, start, stop in train_days:
        for idx_in_slice in range(start, stop):
            k = _clock_slot_position(interval_starts_utc[idx_in_slice], tz, interval_seconds)
            forecast[idx_in_slice] = slot_means.get(k, pooled_mean)

    eval_days = contiguous_day_bounds(list(local_days[training_end_idx:]))
    eval_slots: list[int] = []
    for _label, start, stop in eval_days:
        for offset in range(stop - start):
            idx = training_end_idx + start + offset
            k = _clock_slot_position(interval_starts_utc[idx], tz, interval_seconds)
            forecast[idx] = slot_means.get(k, pooled_mean)
            eval_slots.append(k)

    # Compute training bounds and hash
    train_start_idx = recent_train_days[0][1]
    train_slice = str(interval_starts_utc[train_start_idx:training_end_idx])
    train_hash = hashlib.sha256(train_slice.encode()).hexdigest()[:16]

    metadata: dict[str, Any] = {
        "method": "trailing_slot_mean_frozen",
        "window_days": int(window_days),
        "training_end_idx": int(training_end_idx),
        "training_start_idx": int(train_start_idx),
        "training_intervals": int(training_end_idx - train_start_idx),
        "training_days": len(train_days),
        "training_days_used": len(recent_train_days),
        "training_usable_intervals": int(train_usable.sum()),
        "training_hash": train_hash,
        "slot_positions_learned": len(slot_means),
        "pooled_mean_kwh": float(pooled_mean),
        "eval_days": len(eval_days),
        "interval_seconds": float(interval_seconds),
        "tz": str(tz),
        # A fall-back day can carry slot offsets no training day ever reached (a 100-slot
        # fall-back day against a 96-slot learned window).  Those intervals get the pooled
        # mean, and the count says how many — reporting none would hide a DST-shaped hole.
        "eval_slots_pooled_fallback": sum(1 for k in eval_slots if k not in slot_means),
        "note": (
            "Frozen profile: slot-position means computed from training data ONLY "
            "(strictly before training_end_idx). Slot positions are derived from "
            "the local wall clock (intervals since local midnight), not from run "
            "offsets within the sliced array. The learned profile is applied "
            "across all evaluation days without re-estimation. No test data leaks "
            "into the forecast."
        ),
    }
    return forecast, metadata


# ---------------------------------------------------------------------------
# Causal (event-specific) load forecast
# ---------------------------------------------------------------------------


class CausalForecastProvider:
    """Per-event causal load forecast provider — no future leakage.

    For each event, computes the forecast using only data available at the event's
    decision time.  This is the correct causal semantics: an event at 20:30 on day D
    spanning to D+1 must use data available at 20:30, not data through D+1's midnight.

    The provider truncates the usable mask to exclude all data at or after the event's
    start index, then calls :func:`forecast_load_trailing_slot_mean` with this truncated
    mask.  Each day's forecast then uses only data before the event, not before that
    day's midnight.  This is conservative (doesn't use any future data) and correct.

    **Slot-position clock conventions.**  Uses the SAME function as day-ahead
    (``forecast_load_trailing_slot_mean``), so partial first days and DST transitions
    are handled identically.  The forecast for a partial day uses whatever trailing
    history exists; if no prior day is available its forecast is zero.
    """

    def __init__(
        self,
        load_kwh: np.ndarray,
        local_days: Any,
        usable: np.ndarray,
        interval_starts_utc: Any,
        *,
        window_days: int = 28,
        interval_seconds: float = 900.0,
        tz: ZoneInfo | None = None,
    ) -> None:
        """Initialize the provider with the full dataset.

        Args:
            load_kwh: Full load array (training + evaluation).
            local_days: Local day labels for each interval.
            usable: Per-interval usability mask.
            interval_starts_utc: UTC timestamps for each interval.
            window_days: Trailing window in local days for slot means.
            interval_seconds: Interval length in seconds (for slot positioning).
            tz: Timezone for local-wall-clock conversion.
        """
        from .policies import forecast_load_trailing_slot_mean

        self._load = np.asarray(load_kwh, dtype=float).reshape(-1)
        self._local_days = list(local_days)
        self._usable = np.asarray(usable, dtype=bool).reshape(-1)
        self._interval_starts_utc = list(interval_starts_utc)
        self._window_days = int(window_days)
        self._interval_seconds = float(interval_seconds)
        self._tz = tz if tz is not None else ZoneInfo("Europe/Madrid")
        self._forecast_fn = forecast_load_trailing_slot_mean
        self._day_bounds = contiguous_day_bounds(self._local_days)
        # Cache: (cutoff_idx, window_days) -> (forecast, metadata)
        # Two events on the same local day produce identical forecasts, so we cache
        # to avoid redundant full-dataset fits.  The cache key includes window_days
        # in case the caller changes it between events (though the dispatcher doesn't).
        self._cache: dict[tuple[int, int], tuple[np.ndarray, dict[str, Any]]] = {}

    def forecast_for_event(
        self,
        event: RollingEvent,
    ) -> tuple[np.ndarray, dict[str, Any]]:
        """Compute the forecast for a single event using only data before the event's local day.

        Args:
            event: The planning event.

        Returns:
            ``(forecast, metadata)`` — ``forecast`` is the same length as the full
            dataset.  The event's horizon region ``[event.start_idx:event.plan_end_idx]``
            is the actual forecast for that event.  ``metadata`` records the cutoff
            index (the local-day start), the event's local day, and per-day fallback counts.
        """
        # Find the local-day start for the event's local_day
        event_day = _as_local_date(event.local_day)
        event_local_day_start = next(
            (
                start
                for label, start, _stop in self._day_bounds
                if _as_local_date(label) == event_day
            ),
            event.start_idx,  # fallback to event start if day not found
        )

        # Cache key: (cutoff_idx, window_days).  Two events on the same local day
        # produce byte-identical forecasts, so we cache to avoid redundant full-dataset fits.
        cache_key = (event_local_day_start, self._window_days)
        if cache_key in self._cache:
            forecast, base_metadata = self._cache[cache_key]
            # Return a copy with event-specific fields updated
            metadata = dict(base_metadata)
            metadata["event_index"] = event.index
            metadata["event_local_day"] = str(event.local_day)
            metadata["event_kind"] = event.kind
            return forecast, metadata

        # Truncate the usable mask to exclude all data at or after the local-day start
        truncated_usable = self._usable.copy()
        truncated_usable[event_local_day_start:] = False

        # Compute forecasts for all days using the truncated mask
        forecast, per_day_diag = self._forecast_fn(
            self._load,
            self._day_bounds,
            window_days=self._window_days,
            usable=truncated_usable,
        )

        # Aggregate diagnostics for the event's horizon
        horizon_days = [
            (label, start, stop)
            for label, start, stop in self._day_bounds
            if start >= event.start_idx and start < event.plan_end_idx
        ]
        diag_by_label = {str(d["local_day"]): d for d in per_day_diag}
        horizon_diag = [diag_by_label.get(str(label), {}) for label, _s, _e in horizon_days]

        days_with_pooled_fallback = sum(
            1 for d in horizon_diag if int(d.get("slots_pooled_fallback") or 0) > 0
        )
        days_with_zero_history = sum(
            1 for d in horizon_diag if int(d.get("slots_zero_no_history") or 0) > 0
        )
        total_pooled_slots = sum(int(d.get("slots_pooled_fallback") or 0) for d in horizon_diag)
        total_zero_slots = sum(int(d.get("slots_zero_no_history") or 0) for d in horizon_diag)

        # B4: Window-scoped usable support, not full prefix.  The effective trailing
        # window is at most ``window_days`` local days before the cutoff; anything older
        # is not part of the fit.  We approximate by taking the last ``window_days *
        # slots_per_day`` intervals (the frozen path uses the same approximation).
        slots_per_day = int(round(86400 / self._interval_seconds))
        window_intervals = self._window_days * slots_per_day
        window_start = max(0, event_local_day_start - window_intervals)
        training_usable_intervals = int(truncated_usable[window_start:event_local_day_start].sum())

        # B3: Training-data content hash.  SHA-256 of load+usable bytes in the window,
        # so the hash IS sensitive to load values and usable-mask bytes.  This is
        # stronger than the frozen path, which hashes only timestamps.
        # We hash the window-scoped slice to match the effective support.
        window_load = self._load[window_start:event_local_day_start]
        window_usable = truncated_usable[window_start:event_local_day_start]
        training_hash = hashlib.sha256(window_load.tobytes() + window_usable.tobytes()).hexdigest()[
            :16
        ]

        # B6: Per-target-day usable history count.  For each day in the horizon, count
        # how many usable intervals from the trailing window contributed to its forecast.
        per_target_day_usable_history_days: list[dict[str, Any]] = []
        for label, start, stop in horizon_days:
            day_diag = diag_by_label.get(str(label), {})
            per_target_day_usable_history_days.append(
                {
                    "local_day": str(label),
                    "usable_history_days": int(day_diag.get("window_days_with_usable_data") or 0),
                    "slots_pooled_fallback": int(day_diag.get("slots_pooled_fallback") or 0),
                    "slots_zero_no_history": int(day_diag.get("slots_zero_no_history") or 0),
                }
            )

        metadata: dict[str, Any] = {
            "method": "trailing_slot_mean_causal_event_specific",
            "event_index": event.index,
            "event_local_day": str(event.local_day),
            "event_kind": event.kind,
            "cutoff_idx": int(event_local_day_start),
            "cutoff_time_utc": (
                self._interval_starts_utc[event_local_day_start].isoformat()
                if event_local_day_start < len(self._interval_starts_utc)
                else None
            ),
            "data_cutoff_before_local_day_start": True,
            "training_usable_intervals": training_usable_intervals,
            "training_usable_support": training_usable_intervals > 0,
            "training_hash": training_hash,
            "window_days": self._window_days,
            "horizon_days": len(horizon_days),
            "horizon_days_with_pooled_fallback": days_with_pooled_fallback,
            "horizon_days_with_zero_history": days_with_zero_history,
            "horizon_slots_pooled_fallback": total_pooled_slots,
            "horizon_slots_zero_no_history": total_zero_slots,
            "per_target_day_usable_history_days": per_target_day_usable_history_days,
            "shared_function_with_day_ahead": True,
            "data_cutoff_rule": (
                "the forecast for this event uses only intervals at indices strictly "
                "below the event's local-day start — no observation from the event's own "
                "local day or later enters.  For a multi-day horizon, all target days use "
                "the same pre-day cutoff (the event's local-day start), not their own "
                "midnight, so no next-day refresh happens before the first event after "
                "that midnight"
            ),
            "note": (
                "Event-specific causal forecast: uses the SAME trailing-slot-mean function "
                "as the day-ahead policy (policies.forecast_load_trailing_slot_mean), but "
                "computed per-event with the usable mask truncated at the event's local-day "
                "start.  A 20:30 event on day D spanning to D+1 uses data before D's start, "
                "NOT before D+1's midnight (the whole-array per-target-day vintage that "
                "leaks D's measured load into D+1's fit).  Conservative and correct."
            ),
        }

        # Cache the result (without event-specific fields, which are added on retrieval)
        base_metadata = dict(metadata)
        base_metadata.pop("event_index", None)
        base_metadata.pop("event_local_day", None)
        base_metadata.pop("event_kind", None)
        self._cache[cache_key] = (forecast, base_metadata)

        return forecast, metadata


# ---------------------------------------------------------------------------
# Event computation
# ---------------------------------------------------------------------------


def _warmup_events(
    start_idx: int,
    stop_idx: int,
    days: list[tuple[Any, int, int]],
    *,
    first_index: int,
) -> list[RollingEvent]:
    """Warmup events tiling ``[start_idx, stop_idx)``, one per local day.

    Each returned event plans exactly the part of its own local day that falls
    inside the window: prices are exact throughout (``prices_exact_end_idx ==
    plan_end_idx``) and nothing beyond the local midnight is planned, because a
    warmup happens before tomorrow's prices exist.  Splitting at midnight is what
    keeps ``exec_end_idx <= plan_end_idx`` when the window itself crosses a
    midnight — which it does whenever evaluation starts after the 20:30 event and
    the next scheduled event is the following day's 14:00.

    Being exact to its own plan end is not a special case of the warmup rule, it is a
    consequence of the publication rule: :func:`_last_published_day` at any pre-20:30
    instant gives the event's own day as the last published one, and because a warmup
    never plans past that day's midnight there is no unpublished interval inside its
    plan to mark.

    Raises:
        ValueError: If the local-day partition does not cover the whole range.  An
            uncovered interval has no plan, and quietly dropping it would score a
            window the policy never actually dispatched.
    """
    events: list[RollingEvent] = []
    covered = start_idx
    for label, day_start, day_stop in days:
        seg_lo = max(start_idx, covered)
        seg_hi = min(stop_idx, day_stop)
        if seg_lo >= seg_hi:
            continue
        if day_start > covered:
            break  # the partition skipped this stretch: nothing can plan it
        events.append(
            RollingEvent(
                index=first_index + len(events),
                local_day=_as_local_date(label),
                kind="warmup",
                start_idx=seg_lo,
                plan_end_idx=seg_hi,
                exec_end_idx=seg_hi,
                prices_exact_end_idx=seg_hi,
            )
        )
        covered = seg_hi
    if covered < stop_idx:
        raise ValueError(
            f"local-day partition does not cover [{covered}, {stop_idx}): those "
            "intervals would run with no plan"
        )
    return events


def compute_rolling_events(
    interval_starts_utc: Any,
    local_days: Any,
    *,
    eval_start_idx: int,
    eval_end_idx: int,
    tz: ZoneInfo,
    replan_interval_slots: int | None = None,
    horizon_slots: int | None = None,
) -> list[RollingEvent]:
    """Compute the chronological list of planning events.

    Two modes share one publication rule:

    * **Legacy** (``replan_interval_slots is None``): events at 14:00 and 20:30
      local time for each day that has intervals inside the evaluation window.
      Warmup events cover whatever the window opens with before the first
      scheduled event (see :func:`_warmup_events`); unlike an afternoon or
      evening solve, a warmup never plans past its own local midnight, so it is
      never credited with tomorrow's prices.
    * **Configurable cadence** (``replan_interval_slots is not None``): one
      event every *replan_interval_slots* intervals starting at
      *eval_start_idx*, each planning *horizon_slots* ahead (default 96 = 24 h
      at 15-min intervals).  No warmup events: the cadence starts at
      *eval_start_idx*.  Each event executes only the next
      *replan_interval_slots* intervals.  The price information set is derived
      from the decision instant via :func:`_last_published_day`.

    Guarantees (both modes):
    - Each interval in [eval_start_idx, eval_end_idx) is executed exactly once
    - exec_end_idx <= plan_end_idx for every event
    - No gaps or overlaps in execution coverage
    """
    if replan_interval_slots is not None:
        if replan_interval_slots < 1:
            raise ValueError(f"replan_interval_slots must be >= 1, got {replan_interval_slots}")
        if horizon_slots is None:
            horizon_slots = 96  # 24 h at 15-min intervals
        if horizon_slots < 1:
            raise ValueError(f"horizon_slots must be >= 1, got {horizon_slots}")
        return _compute_replan_events(
            interval_starts_utc,
            local_days,
            eval_start_idx=eval_start_idx,
            eval_end_idx=eval_end_idx,
            tz=tz,
            replan_interval_slots=replan_interval_slots,
            horizon_slots=horizon_slots,
        )

    n = len(interval_starts_utc)
    time_index = _build_time_index(interval_starts_utc)
    days = contiguous_day_bounds(list(local_days))
    day_map = _day_bounds_to_dict(days)
    ds = _day_starts(local_days)

    # Find all event times at or after eval_start_idx
    raw_events: list[tuple[datetime, date, str, int]] = []

    for label, day_start, day_stop in days:
        if day_stop <= eval_start_idx or day_start >= eval_end_idx:
            continue
        d = _as_local_date(label)

        for hour, minute, kind in [
            (AFTERNOON_EVENT[0], AFTERNOON_EVENT[1], "afternoon"),
            (EVENING_EVENT[0], EVENING_EVENT[1], "evening"),
        ]:
            try:
                event_utc = _local_dt_to_utc(d.year, d.month, d.day, hour, minute, tz)
            except Exception:
                continue
            idx = _find_idx_or_none(time_index, event_utc)
            if idx is None:
                continue
            # Only include events at or after eval_start
            if idx >= eval_start_idx and idx < eval_end_idx:
                raw_events.append((event_utc, d, kind, idx))

    # Sort by time
    raw_events.sort(key=lambda e: e[0])

    events: list[RollingEvent] = []

    # Warm up whatever the window opens with, before the first scheduled event.
    # With no scheduled event at all this covers the whole window, so a short window
    # (or one whose event times are missing from the interval grid) is still executed.
    first_event_idx = raw_events[0][3] if raw_events else eval_end_idx
    if eval_start_idx < first_event_idx:
        events.extend(
            _warmup_events(eval_start_idx, first_event_idx, days, first_index=len(events))
        )

    # Nothing was scheduled inside the window (a short window between two events, or a
    # window whose event times are missing from the interval grid).  The warmup above ran
    # to eval_end_idx in that case, so the window is already covered exactly once.
    if not raw_events:
        return events

    # The next boundary to execute is the next event, or the end of the window for
    # the last one.  Clamping that to this event's own plan would be a fake clamp:
    # it leaves the clipped intervals with no plan at all (a silent coverage gap), so
    # an execution window crossing the end of its own day is split instead, one
    # continuation event per remaining local day.
    boundaries = [event[3] for event in raw_events[1:]] + [eval_end_idx]

    for (evt_utc, d, kind, idx), exec_stop in zip(raw_events, boundaries, strict=True):
        # Plan end: midnight ending D+1 — the whole next local day when it exists.
        next_day = d + timedelta(days=1)
        if next_day in day_map:
            plan_end = min(day_map[next_day][1], eval_end_idx)
        else:
            # The dataset ends inside D or D+1 is missing from the partition: use the
            # real local midnight of D+1 if the grid has it, clipped to the window.
            try:
                midnight_utc = _local_dt_to_utc(
                    next_day.year, next_day.month, next_day.day, 0, 0, tz
                )
            except Exception:
                midnight_utc = None
            if midnight_utc is None:
                plan_end = eval_end_idx
            else:
                plan_end = min(_find_idx_or_next(time_index, midnight_utc, n), eval_end_idx)

        # Prices exact end: derived from the single publication rule.  For the
        # legacy afternoon (14:00 < 20:30) the last published day is D, so the
        # boundary is the first interval of D+1 — which is end-of-D-run, i.e.
        # the legacy ``min(day_e, eval_end_idx)``.  For the legacy evening
        # (20:30) the last published day is D+1, so the boundary is the first
        # interval of D+2 — which is past plan_end, so the result is plan_end,
        # i.e. the legacy ``prices_exact_end = plan_end``.  Both branches of
        # the old ``if kind == ...`` collapse to this one call, so the rule
        # cannot drift between modes.
        prices_exact_end = _prices_exact_end_for_decision(evt_utc, plan_end, ds, tz)

        events.append(
            RollingEvent(
                index=len(events),
                local_day=d,
                kind=kind,
                start_idx=idx,
                plan_end_idx=plan_end,
                exec_end_idx=min(exec_stop, plan_end),
                prices_exact_end_idx=prices_exact_end,
            )
        )

        if exec_stop > plan_end:
            # Only reachable when a local day is missing from the partition (a data
            # gap), so the next scheduled event starts beyond this plan.  Fill the
            # stretch with day-only plans instead of clamping it away.
            events.extend(_warmup_events(plan_end, exec_stop, days, first_index=len(events)))

    if not events:
        raise ValueError(
            f"no rolling events for the evaluation window "
            f"[{eval_start_idx}, {eval_end_idx}): intervals would have no plan"
        )
    return events


def _compute_replan_events(
    interval_starts_utc: Any,
    local_days: Any,
    *,
    eval_start_idx: int,
    eval_end_idx: int,
    tz: ZoneInfo,
    replan_interval_slots: int,
    horizon_slots: int,
) -> list[RollingEvent]:
    """Uniform-cadence replan events.

    One event every *replan_interval_slots* intervals starting at
    *eval_start_idx*, each planning *horizon_slots* ahead (clipped to
    *eval_end_idx*).  Each event executes only the next *replan_interval_slots*
    intervals (or fewer for the last event).  The price information set is
    derived from the decision instant via :func:`_last_published_day`, the
    same rule the legacy 14:00/20:30 schedule uses.

    Coverage tiles [eval_start_idx, eval_end_idx) exactly once by construction:
    ``exec_end`` of each event is the ``start_idx`` of the next, and the last
    event's ``exec_end`` is ``eval_end_idx``.  ``exec_end_idx <= plan_end_idx``
    holds because ``exec_end`` is clamped to ``plan_end``.
    """
    ds = _day_starts(local_days)

    events: list[RollingEvent] = []
    idx = eval_start_idx
    while idx < eval_end_idx:
        plan_end = min(idx + horizon_slots, eval_end_idx)
        exec_end = min(idx + replan_interval_slots, plan_end)

        decision_utc = _as_utc(interval_starts_utc[idx])
        prices_exact_end = _prices_exact_end_for_decision(decision_utc, plan_end, ds, tz)
        local_day = decision_utc.astimezone(tz).date()

        events.append(
            RollingEvent(
                index=len(events),
                local_day=local_day,
                kind="replan",
                start_idx=idx,
                plan_end_idx=plan_end,
                exec_end_idx=exec_end,
                prices_exact_end_idx=prices_exact_end,
            )
        )
        idx = exec_end

    if not events:
        raise ValueError(
            f"no replan events for the evaluation window "
            f"[{eval_start_idx}, {eval_end_idx}): intervals would have no plan"
        )
    return events


# ---------------------------------------------------------------------------
# Day-ahead wrapper with frozen forecast
# ---------------------------------------------------------------------------


def run_day_ahead_with_frozen_forecast(
    spec: BatterySpec,
    horizon_obj: Horizon,
    *,
    frozen_load: np.ndarray,
    eval_start_idx: int,
    eval_end_idx: int,
    local_days: Any,
    terminal_value_eur_per_kwh: float,
    scoring_terminal_value_eur_per_kwh: float = 0.0,
    time_limit_s: float = 5.0,
    mip_rel_gap: float = 1e-6,
    mip_abs_gap: float = 1e-9,
    throughput_cost_eur_per_kwh: float = 0.0,
) -> tuple[Schedule, dict[str, Any]]:
    """Run the existing day-ahead policy using a frozen load forecast.

    Builds :class:`DayAheadDay` objects from the frozen profile and delegates
    to :func:`policy_day_ahead`.  This is the comparison baseline for the
    rolling horizon.  ``throughput_cost_eur_per_kwh`` (default ``0.0``) matches the
    knob the rolling dispatcher and the oracle expose; ``0.0`` keeps the plan
    bit-identical to a run without the argument.
    """
    from .policies import policy_day_ahead

    # Build day bounds for the evaluation window
    full_days = contiguous_day_bounds(list(local_days))
    eval_days = [
        (
            label,
            max(start, eval_start_idx) - eval_start_idx,
            min(stop, eval_end_idx) - eval_start_idx,
        )
        for label, start, stop in full_days
        if stop > eval_start_idx and start < eval_end_idx
    ]

    days: list[DayAheadDay] = []
    for label, lo, hi in eval_days:
        m = hi - lo
        if m <= 0:
            continue
        forecast_slice = frozen_load[eval_start_idx + lo : eval_start_idx + hi]
        days.append(
            DayAheadDay(
                label=str(label),
                start=lo,
                stop=hi,
                forecast_load=forecast_slice,
            )
        )

    return policy_day_ahead(
        spec,
        horizon_obj,
        days,
        terminal_value_eur_per_kwh=terminal_value_eur_per_kwh,
        scoring_terminal_value_eur_per_kwh=scoring_terminal_value_eur_per_kwh,
        time_limit_s=time_limit_s,
        mip_rel_gap=mip_rel_gap,
        mip_abs_gap=mip_abs_gap,
        throughput_cost_eur_per_kwh=throughput_cost_eur_per_kwh,
    )


# ---------------------------------------------------------------------------
# Main dispatcher
# ---------------------------------------------------------------------------


def run_rolling_horizon(
    spec: BatterySpec,
    *,
    prices: np.ndarray,
    load_kwh: np.ndarray,
    usable: np.ndarray,
    interval_seconds: float,
    local_days: Any,
    interval_starts_utc: Any,
    eval_start_idx: int,
    eval_end_idx: int,
    tz: ZoneInfo,
    frozen_load: np.ndarray,
    terminal_value_eur_per_kwh: float,
    sigma: float = 0.0,
    bias_sigma: float = 0.0,
    seed: int = 0,
    time_limit_s: float = 5.0,
    mip_rel_gap: float = 1e-6,
    mip_abs_gap: float = 1e-9,
    import_limit_kw: float | None = None,
    replan_interval_slots: int | None = None,
    horizon_slots: int | None = None,
    load_forecast_mode: str = LOAD_FORECAST_MODE_FROZEN,
    throughput_cost_eur_per_kwh: float = 0.0,
    forecast_window_days: int = 28,
) -> RollingResult:
    """Run the rolling horizon dispatcher.

    Two cadences share this one entry point:

    * **Legacy** (``replan_interval_slots is None``, the default): two events per
      local day, 14:00 and 20:30.  Each solves a MILP over ``[event_time, midnight
      ending D+1]``, using estimated prices for the part of the horizon whose
      prices were not yet published, and executes only until the next event.
    * **Configurable cadence** (``replan_interval_slots`` set): one solve every
      ``replan_interval_slots`` intervals over a ``horizon_slots``-long horizon
      (default 96 = 24 h), executing only the next ``replan_interval_slots``
      intervals — the mode a real controller runs.

    In both cases the actual SOC is carried forward from the clipped plan and the
    information set is a function of the decision instant, never of the cadence
    (see :func:`_last_published_day`).

    **Load forecast mode.**  Two modes share one code path:

    * ``frozen`` (default): the load forecast is ``frozen_load``, computed once
      from training data strictly before the cut by
      :func:`build_frozen_load_profile` and passed pre-built.  The profile never
      advances through the evaluation window.
    * ``causal``: each event's forecast is built by :class:`CausalForecastProvider`,
      which truncates the usable mask at the event's own local-day start and calls the
      SAME function the day-ahead policy uses
      (:func:`energy_baseline.policies.forecast_load_trailing_slot_mean`).  A 20:30
      solve on day D whose horizon spans D+1 therefore never reads day D's measured
      load into D+1's estimate — no whole-array per-target-day vintage and no future
      leakage.  The ``frozen_load`` argument is ignored in this mode.

    **Default mode divergence.**  The dispatcher defaults to ``frozen`` for backward
    compatibility with direct API callers and the 14 legacy golden tests.  The
    benchmark defaults to ``causal`` because it is the canonical mode: it uses the
    same forecast function as day-ahead, so the two rungs share a like-for-like load
    basis and differ only in price information and cadence.  Direct callers who want
    the canonical basis must pass ``load_forecast_mode="causal"`` explicitly.

    ``sigma`` is the per-slot forecast noise and ``bias_sigma`` the per-local-day
    multiplicative bias (see :func:`generate_price_noise`).  Both live in
    :func:`generate_price_noise`'s metadata; neither enters scoring.

    ``throughput_cost_eur_per_kwh`` (default ``0.0``) is a per-kWh AC-throughput
    penalty added to each solve's planning objective, the same knob the oracle and
    the day-ahead policy expose so the three rungs stay comparable.  ``0.0`` leaves
    every plan bit-identical to a run without the argument; a non-zero value is a
    planning-only term and never enters the reported (true-price) cost.

    ``forecast_window_days`` (default ``28``) is the trailing window in local days
    for the causal mode's slot means.  Ignored in frozen mode (the frozen profile
    has its own window baked in at build time).

    Cost is always computed against TRUE prices and actual grid import.

    **Cost of a run is visible in the artifact**: ``telemetry["solve_count"]``
    counts events actually sent to the solver (a 15-minute cadence over a 92-day
    holdout is ~8,832 of them versus 185 at the legacy cadence), and
    ``planning_multiplicity_*`` says how many overlapping horizons each interval
    was planned inside.  Clipping totals are NOT comparable across cadences for
    exactly that reason — see ``clipping_totals_comparable_across_cadences`` and
    ``clipping_note`` in the telemetry.
    """
    if load_forecast_mode not in LOAD_FORECAST_MODES:
        raise ValueError(
            f"load_forecast_mode must be one of {LOAD_FORECAST_MODES}, got {load_forecast_mode!r}"
        )
    if not math.isfinite(throughput_cost_eur_per_kwh) or throughput_cost_eur_per_kwh < 0.0:
        raise ValueError(
            f"throughput_cost_eur_per_kwh must be finite and >= 0, got "
            f"{throughput_cost_eur_per_kwh!r}"
        )
    if replan_interval_slots is not None and replan_interval_slots < 1:
        raise ValueError(f"replan_interval_slots must be >= 1, got {replan_interval_slots}")
    if horizon_slots is not None and horizon_slots < 1:
        raise ValueError(f"horizon_slots must be >= 1, got {horizon_slots}")
    if forecast_window_days < 1:
        raise ValueError(f"forecast_window_days must be >= 1, got {forecast_window_days}")

    validate_spec(spec)
    n_eval = eval_end_idx - eval_start_idx

    # Resolve the load forecast provider based on mode.
    # Frozen mode: use the precomputed array directly.
    # Causal mode: create a per-event provider that computes forecasts at decision time.
    forecast_metadata: dict[str, Any] = {}
    causal_provider: CausalForecastProvider | None = None
    causal_events_served = 0
    zero_support_events = 0  # B8: count events with zero usable training support

    if load_forecast_mode == LOAD_FORECAST_MODE_CAUSAL:
        causal_provider = CausalForecastProvider(
            load_kwh,
            local_days,
            usable,
            interval_starts_utc,
            window_days=forecast_window_days,  # B1: wire through from caller
            interval_seconds=interval_seconds,
            tz=tz,
        )
        forecast_metadata = {
            "method": "trailing_slot_mean_causal_event_specific",
            "events_served": 0,
            "forecast_window_days": forecast_window_days,
            # B8: zero-support events are counted and visible, never refused.
            "zero_support_events": 0,
        }

    # Compute events first to know which slots are estimated
    events = compute_rolling_events(
        interval_starts_utc,
        local_days,
        eval_start_idx=eval_start_idx,
        eval_end_idx=eval_end_idx,
        tz=tz,
        replan_interval_slots=replan_interval_slots,
        horizon_slots=horizon_slots,
    )

    # Build estimated_slots_mask: every slot that some plan had to ESTIMATE, i.e. the
    # part of each plan past its publication boundary (prices_exact_end_idx .. plan_end).
    # Derived from the events rather than from a kind name, because which kinds carry an
    # estimated tail depends on the cadence: at the legacy cadence only the 14:00 solve
    # has one (warmup and evening are exact to their plan end, so they contribute
    # nothing here and the mask is exactly the old afternoon-only mask), while at a
    # fine cadence every pre-publication solve contributes its own tail.
    estimated_mask = np.zeros(len(prices), dtype=bool)
    for ev in events:
        if ev.prices_exact_end_idx < ev.plan_end_idx:
            estimated_mask[ev.prices_exact_end_idx : ev.plan_end_idx] = True

    # Generate noisy prices for the full dataset
    day_bounds = contiguous_day_bounds(list(local_days))
    noisy_prices, noise_meta = generate_price_noise(
        prices,
        local_days,
        sigma,
        seed,
        bias_sigma=bias_sigma,
        day_bounds=day_bounds,
        estimated_slots_mask=estimated_mask,
    )

    if not events:
        raise ValueError("no rolling events found in the evaluation window")

    # Allocate full-horizon arrays
    full_charge = np.zeros(n_eval, dtype=float)
    full_discharge = np.zeros(n_eval, dtype=float)

    capacity = spec.capacity_kwh
    soc_min_frac = spec.soc_min_frac
    soc_max_frac = spec.soc_max_frac
    init_kwh = spec.soc_bound_kwh("init")
    horizon_floor_kwh = spec.causal_floor_kwh()
    frozen_by_seed = horizon_floor_kwh >= spec.soc_bound_kwh("max") - _SOC_NOISE_KWH

    carried_kwh = init_kwh
    per_event: list[dict[str, Any]] = []
    counters = {
        "solved": 0,
        "certified": 0,
        "infeasible": 0,
        "no_incumbent": 0,
        "time_limit": 0,
        "skipped_no_usable": 0,
        "frozen_by_seed": 0,
        "clipped": 0,
        "solver_attempts": 0,
    }
    clip_charge_total = 0.0
    clip_discharge_total = 0.0
    value = float(terminal_value_eur_per_kwh)

    # How many overlapping plan horizons cover each interval of the evaluation window.
    # An interval is PLANNED by every solve whose horizon reaches it but EXECUTED exactly
    # once, so this count grows with replan frequency: ~2.5 at the legacy two-event
    # cadence, ~horizon_slots/replan_interval_slots at a uniform one.  It is what makes
    # clipping totals incomparable across cadences, so it is measured rather than left to
    # be inferred (and it is not curtailment).
    planned_multiplicity = np.zeros(n_eval, dtype=int)

    for ev in events:
        lo = ev.start_idx - eval_start_idx  # relative to eval window
        plan_end_rel = ev.plan_end_idx - eval_start_idx
        exec_end_rel = ev.exec_end_idx - eval_start_idx
        exact_end_rel = ev.prices_exact_end_idx - eval_start_idx

        m = plan_end_rel - lo
        exec_m = exec_end_rel - lo
        planned_multiplicity[lo:plan_end_rel] += 1

        record: dict[str, Any] = {
            "event_index": ev.index,
            "local_day": str(ev.local_day),
            "kind": ev.kind,
            "start_idx": ev.start_idx,
            "plan_end_idx": ev.plan_end_idx,
            "exec_end_idx": ev.exec_end_idx,
            "prices_exact_end_idx": ev.prices_exact_end_idx,
            "plan_intervals": m,
            "exec_intervals": exec_m,
            "estimated_price_intervals": max(0, plan_end_rel - exact_end_rel),
            "soc_start_kwh": float(carried_kwh),
            "soc_end_kwh": float(carried_kwh),
            "solver_status": "not_solved",
            "charge_kwh": 0.0,
            "discharge_kwh": 0.0,
            "charge_clipped_kwh": 0.0,
            "discharge_clipped_kwh": 0.0,
            "degradation": "none",
            "note": None,
        }

        # Check usability
        plan_usable = usable[ev.start_idx : ev.plan_end_idx]
        if not plan_usable.any():
            counters["skipped_no_usable"] += 1
            record["degradation"] = "skipped_no_usable"
            per_event.append(record)
            continue

        if frozen_by_seed:
            counters["frozen_by_seed"] += 1
            record["degradation"] = "frozen_by_seed_at_ceiling"
            per_event.append(record)
            continue

        # Build planning prices: exact until prices_exact_end_idx, noisy after
        plan_prices = np.empty(m, dtype=float)
        exact_m = exact_end_rel - lo
        exact_m = max(0, min(exact_m, m))
        plan_prices[:exact_m] = prices[ev.start_idx : ev.start_idx + exact_m]
        if exact_m < m:
            plan_prices[exact_m:] = noisy_prices[ev.start_idx + exact_m : ev.plan_end_idx]

        # Build load forecast: causal mode computes per-event, frozen uses precomputed array
        if causal_provider is not None:
            # Causal mode: compute forecast for this event using only data before the
            # event's local-day start (conservative day-ahead basis; no future vintage).
            event_forecast, event_forecast_meta = causal_provider.forecast_for_event(ev)
            plan_forecast = event_forecast[ev.start_idx : ev.plan_end_idx]
            causal_events_served += 1
            # B8: Count events with zero usable training support (visible, not refused).
            if not event_forecast_meta.get("training_usable_support", True):
                zero_support_events += 1
            forecast_metadata["events_served"] = causal_events_served
            forecast_metadata["zero_support_events"] = zero_support_events
            # Record the first event that produced a forecast for the run-level audit.
            if "first_event_cutoff_idx" not in forecast_metadata:
                for key in (
                    "cutoff_idx",
                    "cutoff_time_utc",
                    "training_usable_intervals",
                    "training_usable_support",
                    "training_hash",
                    "window_days",
                    "data_cutoff_before_local_day_start",
                    "data_cutoff_rule",
                    "note",
                ):
                    forecast_metadata[f"first_event_{key}"] = event_forecast_meta[key]
                forecast_metadata["first_event_local_day"] = str(ev.local_day)
                forecast_metadata["first_event_kind"] = ev.kind
        else:
            # Frozen mode: use the precomputed array
            plan_forecast = frozen_load[ev.start_idx : ev.plan_end_idx]

        # Construct spec with carried SOC
        carried_frac = min(max(carried_kwh / capacity, soc_min_frac), soc_max_frac)
        day_spec = replace(spec, initial_soc_frac=carried_frac, terminal_soc_frac=None)

        # Forecast horizon (for solving)
        forecast_horizon = Horizon(
            interval_seconds=interval_seconds,
            prices=plan_prices,
            load_kwh=plan_forecast,
            usable=plan_usable,
            import_limit_kw=import_limit_kw,
        )

        # Measured horizon (for clipping and scoring)
        measured_horizon = Horizon(
            interval_seconds=interval_seconds,
            prices=prices[ev.start_idx : ev.plan_end_idx],
            load_kwh=load_kwh[ev.start_idx : ev.plan_end_idx],
            usable=plan_usable,
            import_limit_kw=import_limit_kw,
        )

        # Solve.  Every event that reaches this point is one solver call, which is
        # the unit of cost of a run: a 15-minute cadence over a 92-day holdout is
        # ~8,832 of them against ~185 at the legacy cadence.  Counted here, before
        # the call, so a solve that comes back infeasible or without an incumbent is
        # still charged to the run's cost — it consumed the time_limit_s budget.
        counters["solver_attempts"] += 1
        try:
            solved = solve_oracle(
                day_spec,
                forecast_horizon,
                time_limit_s=time_limit_s,
                mip_rel_gap=mip_rel_gap,
                mip_abs_gap=mip_abs_gap,
                terminal_mode="free",
                terminal_value_eur_per_kwh=value,
                floor_kwh=horizon_floor_kwh,
                throughput_cost_eur_per_kwh=throughput_cost_eur_per_kwh,
            )
        except OracleError as exc:
            text = str(exc).lower()
            kind = "infeasible" if "infeasible" in text else "no_incumbent"
            counters[kind] += 1
            record["degradation"] = f"{kind}_hold_soc"
            record["note"] = f"{type(exc).__name__}: {exc}"
            record["solver_status"] = "failed"
            per_event.append(record)
            continue

        status = str(getattr(solved, "solver_status", "feasible"))
        record["solver_status"] = status
        if status == "time_limit":
            counters["time_limit"] += 1
        else:
            counters["solved"] += 1
        if getattr(solved, "is_certified", False):
            counters["certified"] += 1

        # Clip against measured load
        charge, discharge, end_soc, clip_c, clip_d = _clip_plan_to_model_bounds(
            day_spec,
            measured_horizon,
            np.asarray(solved.charge_kwh, dtype=float),
            np.asarray(solved.discharge_kwh, dtype=float),
            floor_kwh=horizon_floor_kwh,
        )

        if clip_c > 1e-12 or clip_d > 1e-12:
            counters["clipped"] += 1
            record["charge_clipped_kwh"] = float(clip_c)
            record["discharge_clipped_kwh"] = float(clip_d)
            clip_charge_total += float(clip_c)
            clip_discharge_total += float(clip_d)

        # Execute only the execution window
        exec_charge = charge[:exec_m]
        exec_discharge = discharge[:exec_m]
        full_charge[lo:exec_end_rel] = exec_charge
        full_discharge[lo:exec_end_rel] = exec_discharge

        # Compute actual SOC at execution end by walking the executed portion
        eta_c = spec.charge_efficiency
        eta_d = spec.discharge_efficiency
        soc = carried_kwh
        for t in range(exec_m):
            if plan_usable[t]:
                soc += exec_charge[t] * eta_c - exec_discharge[t] / eta_d
        carried_kwh = float(soc)

        record["soc_end_kwh"] = carried_kwh
        record["charge_kwh"] = float(exec_charge.sum())
        record["discharge_kwh"] = float(exec_discharge.sum())
        per_event.append(record)

    # Verify coverage: each interval in [eval_start_idx, eval_end_idx) executed exactly once
    coverage = np.zeros(n_eval, dtype=int)
    for ev in events:
        lo_rel = ev.start_idx - eval_start_idx
        exec_end_rel = ev.exec_end_idx - eval_start_idx
        coverage[lo_rel:exec_end_rel] += 1
    if not np.all(coverage == 1):
        gaps = np.where(coverage == 0)[0]
        overlaps = np.where(coverage > 1)[0]
        raise ValueError(
            f"coverage violation: {len(gaps)} gaps, {len(overlaps)} overlaps. "
            f"First gap at {gaps[0] if len(gaps) else 'none'}, "
            f"first overlap at {overlaps[0] if len(overlaps) else 'none'}"
        )

    # Trace full schedule against true prices and measured load
    eval_horizon = Horizon(
        interval_seconds=interval_seconds,
        prices=prices[eval_start_idx:eval_end_idx],
        load_kwh=load_kwh[eval_start_idx:eval_end_idx],
        usable=usable[eval_start_idx:eval_end_idx],
        import_limit_kw=import_limit_kw,
    )

    traced = simulate(spec, eval_horizon, full_charge, full_discharge, enforce_terminal_soc=False)

    # Cost is computed against TRUE prices (already done by simulate with eval_horizon)
    cost_eur = float(traced.meter_cost_eur)
    meter_cost = cost_eur

    cadence: dict[str, Any] = _cadence_telemetry(
        replan_interval_slots, horizon_slots, interval_seconds
    )
    # Planned-versus-executed multiplicity, stated explicitly because it is a known
    # interpretation trap: an interval is planned by several overlapping horizons but
    # executed once, so clipping kWh is counted per plan and grows with replan
    # frequency without one extra joule being curtailed.
    multiplicity_mean = float(planned_multiplicity.mean()) if n_eval else 0.0
    multiplicity_max = int(planned_multiplicity.max()) if n_eval else 0
    # Measured, not asserted: the coverage check above would already have raised on a
    # gap or an overlap, so this is the record of that fact rather than a claim of it.
    executed_mean = float(coverage.mean()) if n_eval else 0.0

    telemetry: dict[str, Any] = {
        "policy": "rolling_horizon",
        "event_count": len(events),
        # Cost of the run, in solver calls.  Reported first-class so a 48x cadence
        # cannot be mistaken for an equally cheap run as the legacy schedule.
        "solve_count": int(counters["solver_attempts"]),
        "planning_events_not_solved": int(len(events) - counters["solver_attempts"]),
        "replan_interval_slots": cadence["replan_interval_slots"],
        "replan_cadence": cadence["replan_cadence"],
        "horizon_slots": cadence["horizon_slots"],
        "horizon_hours_nominal": cadence["horizon_hours_nominal"],
        "events_solved": counters["solved"],
        "events_certified": counters["certified"],
        "events_infeasible": counters["infeasible"],
        "events_no_incumbent": counters["no_incumbent"],
        "events_time_limit": counters["time_limit"],
        "events_skipped_no_usable": counters["skipped_no_usable"],
        "events_frozen_by_seed": counters["frozen_by_seed"],
        "events_with_clipping": counters["clipped"],
        "charge_clipped_kwh": float(clip_charge_total),
        "discharge_clipped_kwh": float(clip_discharge_total),
        # --- planned-versus-executed multiplicity -------------------------------
        # Each executed interval is also counted in every other plan whose horizon
        # covered it.  Executed multiplicity is exactly 1 by the coverage check above;
        # planned multiplicity is what inflates the clipping totals.
        "executed_multiplicity_mean": executed_mean,
        "planning_multiplicity_mean": multiplicity_mean,
        "planning_multiplicity_max": multiplicity_max,
        "planned_interval_slots_total": int(planned_multiplicity.sum()),
        "executed_interval_slots_total": int(n_eval),
        "clipping_totals_comparable_across_cadences": False,
        "clipping_note": (
            f"charge/discharge_clipped_kwh sum per-PLAN clipping over "
            f"{int(counters['solver_attempts'])} solves, and each interval was planned a "
            f"mean of {multiplicity_mean:.2f} times (max {multiplicity_max}) while being "
            "executed once. These totals are therefore NOT curtailment and NOT comparable "
            "across runs with a different replan_interval_slots or horizon_slots."
        ),
        "terminal_value_eur_per_kwh": value,
        "throughput_cost_eur_per_kwh": float(throughput_cost_eur_per_kwh),
        "sigma": float(sigma),
        "bias_sigma": float(bias_sigma),
        "seed": int(seed),
        "horizon_floor_kwh": float(horizon_floor_kwh),
        "initial_soc_kwh": float(init_kwh),
        "terminal_soc_kwh": float(traced.soc_kwh[-1]),
        "load_forecast_mode": load_forecast_mode,
        "per_event": per_event,
    }

    return RollingResult(
        schedule=traced,
        telemetry=telemetry,
        per_event=per_event,
        cost_eur=cost_eur,
        meter_cost_eur=meter_cost,
        noise_metadata=noise_meta,
        forecast_metadata=forecast_metadata,
    )
