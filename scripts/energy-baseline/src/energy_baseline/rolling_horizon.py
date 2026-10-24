"""Rolling horizon dispatcher — two events per day, partial execution, SOC carry.

Two planning events per local day (Europe/Madrid):

* **14:00** — plan from 14:00 to midnight ending D+1 (~34 h).  Prices for the
  rest of day D are exact; prices for D+1 are a noisy estimate (sigma=0 is the
  exact-price control).
* **20:30** — re-plan from 20:30 to midnight ending D+1 (~27.5 h).  Prices for
  the rest of D **and** all of D+1 are now exact (conservative: first call after
  the 20:15–20:20 publication window).

Each plan is executed **only until the next event** (14:00 → 20:30, 20:30 →
next 14:00).  The actual SOC is carried forward by walking the clipped plan
chronologically, so the next event's solve starts from the real state, not the
planned one.

Warmup events cover ``[eval_start, first scheduled event)``.  A warmup never
plans across a local midnight — when that window spans one it is split at every
midnight into one warmup per local day, each planning only the day it starts in,
with exact prices and no tomorrow.  So the first partial day is not silently
given future information it could not have had, and no plan is ever shorter than
the slice of it that gets executed.

Load forecasts are **frozen**: computed once from training data (slot-position means
over the trailing ``window_days`` local days, strictly before the cut) by
:func:`build_frozen_load_profile` and passed pre-built into every solve.
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
    "RollingEvent",
    "RollingResult",
    "build_frozen_load_profile",
    "compute_rolling_events",
    "generate_price_noise",
    "run_rolling_horizon",
    "run_day_ahead_with_frozen_forecast",
]

MADRID_TZ = "Europe/Madrid"
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
    """``"warmup"``, ``"afternoon"`` (14:00), or ``"evening"`` (20:30)."""

    start_idx: int
    """First interval index of the plan (and execution) window."""

    plan_end_idx: int
    """One past the last interval of the plan horizon."""

    exec_end_idx: int
    """One past the last interval executed from this plan."""

    prices_exact_end_idx: int
    """Prices are exact (true) up to this index; noisy after (for afternoon)."""


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
) -> list[RollingEvent]:
    """Compute the chronological list of planning events.

    Events are at 14:00 and 20:30 local time for each day that has intervals inside
    the evaluation window.  Warmup events cover whatever the window opens with before
    the first scheduled event (see :func:`_warmup_events`); unlike an afternoon or
    evening solve, a warmup never plans past its own local midnight, so it is never
    credited with tomorrow's prices.

    Guarantees:
    - Each interval in [eval_start_idx, eval_end_idx) is executed exactly once
    - exec_end_idx <= plan_end_idx for every event
    - No gaps or overlaps in execution coverage
    """
    n = len(interval_starts_utc)
    time_index = _build_time_index(interval_starts_utc)
    days = contiguous_day_bounds(list(local_days))
    day_map = _day_bounds_to_dict(days)

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

        # Prices exact end: for afternoon, midnight D; for evening, plan_end
        if kind == "afternoon":
            day_s, day_e = day_map.get(d, (idx, plan_end))
            prices_exact_end = min(day_e, eval_end_idx)
        else:
            prices_exact_end = plan_end  # all exact for evening

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
) -> tuple[Schedule, dict[str, Any]]:
    """Run the existing day-ahead policy using a frozen load forecast.

    Builds :class:`DayAheadDay` objects from the frozen profile and delegates
    to :func:`policy_day_ahead`.  This is the comparison baseline for the
    rolling horizon.
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
) -> RollingResult:
    """Run the two-event rolling horizon dispatcher.

    For each event, solves a MILP over [event_time, midnight D+1] using noisy
    prices (for the afternoon event) or exact prices (for the evening event and
    warmup).  Executes only until the next event, carries the actual SOC
    forward, and re-solves.

    ``sigma`` is the per-slot forecast noise and ``bias_sigma`` the per-local-day
    multiplicative bias (see :func:`generate_price_noise`).  Both live in
    :func:`generate_price_noise`'s metadata; neither enters scoring.

    Cost is always computed against TRUE prices and actual grid import.
    """
    validate_spec(spec)
    n_eval = eval_end_idx - eval_start_idx

    # Compute events first to know which slots are estimated
    events = compute_rolling_events(
        interval_starts_utc,
        local_days,
        eval_start_idx=eval_start_idx,
        eval_end_idx=eval_end_idx,
        tz=tz,
    )

    # Build estimated_slots_mask: slots in the "noisy portion" of afternoon events
    # (from prices_exact_end_idx to plan_end_idx)
    estimated_mask = np.zeros(len(prices), dtype=bool)
    for ev in events:
        if ev.kind == "afternoon":
            # Noisy portion is from prices_exact_end_idx to plan_end_idx
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
    }
    clip_charge_total = 0.0
    clip_discharge_total = 0.0
    value = float(terminal_value_eur_per_kwh)

    for ev in events:
        lo = ev.start_idx - eval_start_idx  # relative to eval window
        plan_end_rel = ev.plan_end_idx - eval_start_idx
        exec_end_rel = ev.exec_end_idx - eval_start_idx
        exact_end_rel = ev.prices_exact_end_idx - eval_start_idx

        m = plan_end_rel - lo
        exec_m = exec_end_rel - lo

        record: dict[str, Any] = {
            "event_index": ev.index,
            "local_day": str(ev.local_day),
            "kind": ev.kind,
            "start_idx": ev.start_idx,
            "plan_end_idx": ev.plan_end_idx,
            "exec_end_idx": ev.exec_end_idx,
            "plan_intervals": m,
            "exec_intervals": exec_m,
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

        # Build load forecast from frozen profile
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

        # Solve
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

    telemetry: dict[str, Any] = {
        "policy": "rolling_horizon",
        "event_count": len(events),
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
        "terminal_value_eur_per_kwh": value,
        "sigma": float(sigma),
        "bias_sigma": float(bias_sigma),
        "seed": int(seed),
        "horizon_floor_kwh": float(horizon_floor_kwh),
        "initial_soc_kwh": float(init_kwh),
        "terminal_soc_kwh": float(traced.soc_kwh[-1]),
        "per_event": per_event,
    }

    return RollingResult(
        schedule=traced,
        telemetry=telemetry,
        per_event=per_event,
        cost_eur=cost_eur,
        meter_cost_eur=meter_cost,
        noise_metadata=noise_meta,
        forecast_metadata={},  # filled by caller
    )
