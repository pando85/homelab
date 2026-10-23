"""Cost model — tariff-verified energy cost with effective-dated taxes.

This module answers a narrow question: **what did the imported kWh cost?**  It
prices grid energy against the pre-tax ``€/kWh`` PVPC price the sensor reports,
optionally grossed up by the effective-dated tax multiplier.

What the model deliberately does *not* do
-----------------------------------------
* **It is not an invoice.**  Only energy is priced.  Fixed charges, the power
  terms and the fiscal minimum are not modelled, so a cost figure must never be
  asserted to equal a real bill: :attr:`CostSummary.is_full_invoice` is always
  ``False`` and :func:`account_intervals` reports ``is_full_invoice: False``.
* **It adds no tolls.**  The PVPC price already includes access tolls, charges
  and production cost (ESIOS indicator 1001), so the tax multiplier is the only
  gross-up applied here, and it is applied exactly once per interval.
* **It never invents a rate.**  A tax band whose percentages are ``null``, or
  whose ``source`` is a placeholder, is *unconfirmed*: taxed ("billed") mode
  refuses with :class:`TaxesUnconfirmedError` rather than summing the confirmed
  days together with the unconfirmed ones.  No partial sums.
* **It does not round money.**  Rounding is a display concern; a month of ~2880
  intervals must not accumulate 2880 rounding steps.
* **It does not estimate missing data.**  Intervals with no energy, no price, or
  a quality flag that is not exact/measured are *excluded and counted*, never
  interpolated.  A measured ``0.0 kWh`` is real data and is kept: zero is never
  missing.
* **It only imports.**  Energy must be ``>= 0``.  Negative *prices* are real
  (PVPC reports them) and are preserved, so a negative price gives a negative
  cost; it is never clamped to zero.

Local days, not UTC days
------------------------
Spanish tax bands become effective on a **local** calendar date, so the band for
an interval is chosen from that interval's date in the configured timezone
(``cfg.tariff_periods.timezone``, ``Europe/Madrid`` in the shipped config) —
``2026-01-31T23:30:00Z`` is already February 1st in Madrid and must use the
February band, while the same instant's UTC date would silently pick January.
:func:`effective_tax_multiplier` therefore requires tz-aware input and converts
before the lookup.  Aggregates never assume one multiplier per period either:
each interval is priced with its own band, which is the split-at-rate-change
behaviour a period spanning a rate change needs.  Callers that want explicit
segments get :func:`split_span_at_tax_changes`, and callers that want a single
scalar multiplier are refused by :func:`require_constant_tax_multiplier`.

The config dataclasses are used by duck typing — only ``cfg.taxes`` and
``cfg.tariff_periods.timezone`` are read — so this module stays importable and
testable while ``config.py`` is being reworked elsewhere.
"""

from __future__ import annotations

import math
from collections.abc import Mapping, Sequence
from dataclasses import dataclass, field
from datetime import UTC, date, datetime
from typing import TYPE_CHECKING, Any
from zoneinfo import ZoneInfo

import numpy as np

if TYPE_CHECKING:  # pragma: no cover - typing only, no runtime dependency
    from energy_baseline.config import TariffConfig

__all__ = [
    "ALLOCATION_EXACT",
    "ALLOCATION_UNKNOWN_GAP",
    "COST_BASIS_TAXED",
    "COST_BASIS_UNTAXED",
    "CURRENCY",
    "ENERGY_COUNTER_RESET_BRIDGED",
    "ENERGY_GAP_PARTIAL",
    "ENERGY_GAP_TOTAL_UNKNOWN",
    "ENERGY_MEASURED",
    "EXCLUSION_REASONS",
    "INTERVAL_SECONDS",
    "PRICE_FORWARD_FILLED",
    "PRICE_MEASURED",
    "PRICE_MISSING",
    "CostError",
    "CostSegment",
    "CostSummary",
    "TaxBoundaryError",
    "TaxesUnconfirmedError",
    "UNTRUSTED_TAX_SOURCES",
    "USABLE_ALLOCATION_QUALITY",
    "USABLE_ENERGY_QUALITY",
    "USABLE_PRICE_QUALITY",
    "account_intervals",
    "applicable_tax_entry",
    "classify_interval",
    "compute_interval_cost",
    "compute_period_cost",
    "compute_period_summary",
    "config_timezone",
    "effective_prices",
    "effective_tax_multiplier",
    "effective_tax_multipliers",
    "local_day",
    "require_constant_tax_multiplier",
    "require_tax_multiplier",
    "split_span_at_tax_changes",
    "tax_confidence",
    "tax_rate_changes",
    "usable_intervals",
]

# ---------------------------------------------------------------------------
# Constants
# ---------------------------------------------------------------------------

#: The only currency this model prices in; it is never inferred or converted.
CURRENCY = "EUR"

#: Nominal length of the analysis grid (the 15-minute grid).
INTERVAL_SECONDS = 900

#: Cost-basis labels, matching ``BENCHMARK_RESULT.cost_basis`` in ``schemas.py``.
COST_BASIS_UNTAXED = "untaxed"
COST_BASIS_TAXED = "taxed"
VALID_COST_BASES = frozenset({COST_BASIS_UNTAXED, COST_BASIS_TAXED})

#: Quality vocabulary.  ``schemas.py`` is the normative source for the values;
#: the ``USABLE_*`` sets below are the defaults that count as usable here.
ENERGY_MEASURED = "measured"
ENERGY_GAP_PARTIAL = "gap_partial"
ENERGY_GAP_TOTAL_UNKNOWN = "gap_total_unknown"
ENERGY_COUNTER_RESET_BRIDGED = "counter_reset_bridged"
ALLOCATION_EXACT = "exact"
ALLOCATION_UNKNOWN_GAP = "unknown_gap"
PRICE_MEASURED = "measured"
PRICE_FORWARD_FILLED = "forward_filled"
PRICE_MISSING = "missing"

#: Only an actual counter delta may enter a cost total.
USABLE_ENERGY_QUALITY: frozenset[str] = frozenset({ENERGY_MEASURED})
#: Unknown placement of a known delta is not a billable euro.
USABLE_ALLOCATION_QUALITY: frozenset[str] = frozenset({ALLOCATION_EXACT})
#: A carried-forward price is not the price that was paid.
USABLE_PRICE_QUALITY: frozenset[str] = frozenset({PRICE_MEASURED})

#: ``source`` values that mean "nobody verified this rate".  ``"unknown"`` is
#: what ``config.load_config`` fills in when the key is absent;
#: ``"unconfirmed"`` is the sentinel the shipped ``tariff.yaml`` uses.
UNTRUSTED_TAX_SOURCES: frozenset[str] = frozenset(
    {
        "assumed",
        "default",
        "invented",
        "none",
        "null",
        "placeholder",
        "tbd",
        "todo",
        "unconfirmed",
        "unknown",
    }
)

# Exclusion reason codes (keys of ``excluded_reasons``).
REASON_ENERGY_MISSING = "energy_missing"
REASON_ENERGY_INVALID = "energy_invalid"
REASON_ENERGY_QUALITY = "energy_quality"
REASON_ALLOCATION_QUALITY = "allocation_quality"
REASON_PRICE_MISSING = "price_missing"
REASON_PRICE_INVALID = "price_invalid"
REASON_PRICE_QUALITY = "price_quality"
REASON_NOT_SELECTED = "not_selected"
EXCLUSION_REASONS: tuple[str, ...] = (
    REASON_ENERGY_MISSING,
    REASON_ENERGY_INVALID,
    REASON_ENERGY_QUALITY,
    REASON_ALLOCATION_QUALITY,
    REASON_PRICE_MISSING,
    REASON_PRICE_INVALID,
    REASON_PRICE_QUALITY,
    REASON_NOT_SELECTED,
)


# ---------------------------------------------------------------------------
# Errors
# ---------------------------------------------------------------------------


class CostError(ValueError):
    """Invalid cost input or an unusable tariff configuration."""


class TaxesUnconfirmedError(CostError):
    """Taxed ("billed") cost was requested but taxes are not confirmed.

    Raised instead of returning a figure that mixes confirmed with unconfirmed
    days: a partial sum would silently understate the bill.
    """


class TaxBoundaryError(CostError):
    """A span crosses a tax rate change but a single multiplier was requested.

    Split it with :func:`split_span_at_tax_changes`, or price each interval with
    :func:`effective_tax_multipliers`.
    """


# ---------------------------------------------------------------------------
# Scalar validation
# ---------------------------------------------------------------------------


def _finite_float(value: Any, name: str) -> float:
    """Return *value* as a finite ``float`` or raise :class:`CostError`."""
    if value is None or isinstance(value, (bool, np.bool_)):
        raise CostError(f"{name} must be a real number, got {value!r}")
    try:
        number = float(value)
    except (TypeError, ValueError) as exc:
        raise CostError(f"{name} must be a real number, got {value!r}") from exc
    if not math.isfinite(number):
        raise CostError(f"{name} must be finite, got {number!r}")
    return number


def _validated_multiplier(value: Any, name: str) -> float:
    """Coerce and range-check one multiplier: finite and ``>= 1``."""
    multiplier = _finite_float(value, name)
    if multiplier < 1.0:
        raise CostError(
            f"{name} must be >= 1, since fiscal rates are non-negative; got {multiplier}. "
            "To strip taxes from a price, divide by the multiplier explicitly instead of "
            "passing a value below 1."
        )
    return multiplier


def _resolve_basis(basis: str | None, taxed: bool, where: str) -> str:
    """Validate an explicit *basis* against how taxes were actually supplied."""
    if basis is None:
        return COST_BASIS_TAXED if taxed else COST_BASIS_UNTAXED
    if basis not in VALID_COST_BASES:
        raise CostError(
            f"{where}: basis must be {COST_BASIS_UNTAXED!r} or {COST_BASIS_TAXED!r}, got {basis!r}"
        )
    if basis == COST_BASIS_TAXED and not taxed:
        raise TaxesUnconfirmedError(
            f"{where}: basis={COST_BASIS_TAXED!r} requires a confirmed tax multiplier; "
            "refusing to call a pre-tax figure billed"
        )
    if basis == COST_BASIS_UNTAXED and taxed:
        raise CostError(
            f"{where}: basis={COST_BASIS_UNTAXED!r} but a tax multiplier was supplied — "
            "state which basis the number is in, and never tax an already-taxed price"
        )
    return basis


# ---------------------------------------------------------------------------
# Timezone + local-day helpers
# ---------------------------------------------------------------------------


def config_timezone(cfg: TariffConfig) -> ZoneInfo:
    """Return the configured local timezone (``cfg.tariff_periods.timezone``)."""
    try:
        name = cfg.tariff_periods.timezone
    except AttributeError as exc:  # pragma: no cover - defensive shape check
        raise CostError("cfg.tariff_periods.timezone is required to resolve local days") from exc
    try:
        return ZoneInfo(name)
    except Exception as exc:
        raise CostError(
            f"cfg.tariff_periods.timezone {name!r} is not a usable IANA zone: {exc}"
        ) from exc


def local_day(at: datetime | date, tz: ZoneInfo | str = UTC) -> date:
    """Return the **local** calendar date of *at* in *tz*.

    ``datetime`` input must be tz-aware: a naive timestamp cannot be placed on a
    local calendar without guessing, and guessing is exactly how a UTC day ends
    up selecting a Madrid-date tax band.  A bare ``date`` is taken as an already
    local calendar day.

    Raises:
        CostError: *at* is naive or neither date nor datetime, or *tz* is bad.
    """
    if isinstance(tz, str):
        tz = ZoneInfo(tz)
    if isinstance(at, datetime):
        if at.tzinfo is None or at.utcoffset() is None:
            raise CostError(
                f"naive timestamp {at.isoformat()} cannot be mapped to a local day; "
                "pass tz-aware ISO 8601 timestamps (UTC from the analysis grid is fine)"
            )
        return at.astimezone(tz).date()
    if isinstance(at, date):
        return at
    raise CostError(f"expected a date or tz-aware datetime, got {type(at).__name__}")


def _aware_datetime(value: Any, name: str) -> datetime:
    """Coerce *value* (tz-aware datetime or ISO-8601 string) to tz-aware datetime."""
    if isinstance(value, str):
        try:
            value = datetime.fromisoformat(value)
        except ValueError as exc:
            raise CostError(f"{name} {value!r} is not ISO 8601") from exc
    if not isinstance(value, datetime):
        raise CostError(f"{name} must be a tz-aware datetime or ISO 8601 string, got {value!r}")
    if value.tzinfo is None or value.utcoffset() is None:
        raise CostError(
            f"{name} must be tz-aware (ISO 8601 with an offset), got naive {value.isoformat()}"
        )
    return value


def _aware_datetimes(starts: Any, name: str = "starts") -> list[datetime]:
    """Coerce a column of interval-start timestamps to tz-aware datetimes.

    Accepts anything with ``to_pylist()`` (pyarrow arrays and tables), a sequence
    of tz-aware datetimes or ISO-8601 strings, or an ``object`` ndarray holding
    those.  A NumPy ``datetime64`` array is refused: it carries no timezone, and
    assuming UTC is the bug this module exists to prevent.
    """
    to_pylist = getattr(starts, "to_pylist", None)
    if callable(to_pylist):
        items: Any = to_pylist()
    elif isinstance(starts, np.ndarray) and starts.dtype.kind == "M":
        raise CostError(
            f"{name} is a tz-less numpy datetime64 array; pass a pyarrow timestamp column "
            f"(tz 'UTC'), ISO strings with an offset, or tz-aware datetimes"
        )
    else:
        items = starts
    if isinstance(items, (str, bytes)) or not hasattr(items, "__iter__"):
        raise CostError(f"{name} must be a sequence of timestamps, got {type(items).__name__}")
    return [_aware_datetime(value, f"{name}[{index}]") for index, value in enumerate(items)]


# ---------------------------------------------------------------------------
# Effective-dated taxes
# ---------------------------------------------------------------------------


def _tax_entries(cfg: TariffConfig) -> list[Any]:
    """Return ``cfg.taxes`` sorted ascending by ``valid_from``."""
    try:
        entries = list(cfg.taxes)
    except (TypeError, AttributeError) as exc:
        raise CostError("cfg.taxes must be a sequence of tax entries") from exc
    for entry in entries:
        if not hasattr(entry, "valid_from"):
            raise CostError(f"tax entry {entry!r} has no valid_from")
    return sorted(entries, key=lambda entry: entry.valid_from)


def _source_is_confirmed(source: Any) -> bool:
    """Return ``True`` when *source* names a real, verified origin."""
    if source is None:
        return False
    if not isinstance(source, str):
        raise CostError(f"tax source must be a string, got {source!r}")
    stripped = source.strip()
    if not stripped:
        return False
    return stripped.lower() not in UNTRUSTED_TAX_SOURCES


def _rate_pct(value: Any, valid_from: Any, field_name: str) -> float | None:
    """Validate one tax percentage: ``None`` is unconfirmed, else finite and >= 0."""
    if value is None:
        return None
    number = _finite_float(value, f"taxes.{field_name} (band {valid_from})")
    if number < 0 or number > 100:
        raise CostError(
            f"taxes.{field_name} for band {valid_from} is {number}: a fiscal rate is a "
            "percentage in 0..100 and is never negative; a rebate or credit is a different "
            "mechanism and must not be smuggled in as a negative rate"
        )
    return number


def _multiplier_from_entry(entry: Any) -> float | None:
    """Return ``(1 + iee/100) * (1 + iva/100)``, or ``None`` when unconfirmed.

    A band is unconfirmed when either percentage is ``null`` **or** its
    ``source`` does not name a verified origin: rates that merely look plausible
    are not evidence, and neither is the shipped placeholder band.
    """
    iee = _rate_pct(entry.iee_pct, entry.valid_from, "iee_pct")
    iva = _rate_pct(entry.iva_pct, entry.valid_from, "iva_pct")
    if iee is None or iva is None:
        return None
    if not _source_is_confirmed(getattr(entry, "source", None)):
        return None
    return (1.0 + iee / 100.0) * (1.0 + iva / 100.0)


def applicable_tax_entry(
    cfg: TariffConfig,
    at: datetime | date,
    tz: ZoneInfo | None = None,
) -> Any:
    """Return the tax band in force on the **local** day of *at*, or ``None``.

    The band is the entry with the latest ``valid_from`` on or before that local
    day.  ``None`` means nothing covers the day (a date before the earliest
    configured band) — never a guess.
    """
    day = local_day(at, tz if tz is not None else config_timezone(cfg))
    applicable: Any = None
    for entry in _tax_entries(cfg):
        if entry.valid_from > day:
            break
        applicable = entry
    return applicable


def effective_tax_multiplier(cfg: TariffConfig, at_datetime: datetime | date) -> float | None:
    """Return the confirmed tax multiplier in force on *at_datetime*'s local day.

    The lookup uses the date in ``cfg.tariff_periods.timezone`` (``Europe/Madrid``
    in the shipped config), so an instant just after local midnight uses the new
    day's band even while it is still the previous day in UTC.

    Returns:
        ``(1 + iee/100) * (1 + iva/100)``, or ``None`` when no band covers the
        local day, the band's rates are ``null``, or its ``source`` is a
        placeholder.

    Raises:
        CostError: *at_datetime* is naive, or the band's rates are invalid
            (non-finite, negative, or above 100 percent).
    """
    entry = applicable_tax_entry(cfg, at_datetime)
    if entry is None:
        return None
    return _multiplier_from_entry(entry)


def require_tax_multiplier(cfg: TariffConfig, at_datetime: datetime | date) -> float:
    """Like :func:`effective_tax_multiplier`, but refuse an unconfirmed band.

    Raises:
        TaxesUnconfirmedError: no confirmed multiplier covers that local day.
    """
    multiplier = effective_tax_multiplier(cfg, at_datetime)
    if multiplier is not None:
        return multiplier
    tz = config_timezone(cfg)
    entry = applicable_tax_entry(cfg, at_datetime, tz)
    day = local_day(at_datetime, tz)
    if entry is None:
        why = f"no taxes band starts on or before {day.isoformat()}"
    elif entry.iee_pct is None or entry.iva_pct is None:
        why = f"band {entry.valid_from} has null rates (unconfirmed)"
    else:
        why = f"band {entry.valid_from} has an unverified source {entry.source!r}"
    raise TaxesUnconfirmedError(
        f"taxed cost for local day {day.isoformat()} in {tz.key} is unavailable: {why}; "
        "confirm the rates and source in tariff.yaml, or report basis='untaxed'"
    )


def tax_confidence(cfg: TariffConfig) -> dict[str, Any]:
    """Describe how much of the tax history can actually be billed.

    For the report layer: when ``confirmed_bands`` is empty the honest label is
    "pre-tax, not billed", and there is no date range in which a taxed figure
    exists at all.
    """
    confirmed: list[date] = []
    unconfirmed: list[date] = []
    for entry in _tax_entries(cfg):
        (confirmed if _multiplier_from_entry(entry) is not None else unconfirmed).append(
            entry.valid_from
        )
    return {
        "bands": len(confirmed) + len(unconfirmed),
        "confirmed_from": sorted(confirmed)[0].isoformat() if confirmed else None,
        "confirmed_bands": [d.isoformat() for d in sorted(confirmed)],
        "unconfirmed_bands": [d.isoformat() for d in sorted(unconfirmed)],
        "taxed_mode_possible": bool(confirmed),
        "currency": CURRENCY,
    }


def tax_rate_changes(cfg: TariffConfig) -> tuple[date, ...]:
    """Local dates on which the *effective* multiplier changes (or becomes known).

    Bands that leave the multiplier identical are not reported; a change
    between a number and "unconfirmed" counts, because taxed mode starts or
    stops being legitimate there.
    """
    changes: list[date] = []
    previous: float | None = None
    seen = False
    for entry in _tax_entries(cfg):
        multiplier = _multiplier_from_entry(entry)
        if not seen or multiplier != previous:
            changes.append(entry.valid_from)
            seen = True
        previous = multiplier
    return tuple(changes)


@dataclass(frozen=True)
class CostSegment:
    """A sub-span of a period that shares one tax band, with its multiplier."""

    start: datetime
    end: datetime
    local_day: date
    tax_multiplier: float | None

    @property
    def confirmed(self) -> bool:
        """Whether this segment may be priced in taxed mode."""
        return self.tax_multiplier is not None

    def as_dict(self) -> dict[str, Any]:
        """Plain-dict form for report rendering."""
        return {
            "start": self.start,
            "end": self.end,
            "local_day": self.local_day.isoformat(),
            "tax_multiplier": self.tax_multiplier,
            "confirmed": self.confirmed,
        }


def _next_local_midnight(day: date, tz: ZoneInfo) -> datetime:
    """First instant of the local day after *day* (DST-correct wall clock)."""
    following = date.fromordinal(day.toordinal() + 1)
    return datetime(following.year, following.month, following.day, tzinfo=tz)


def split_span_at_tax_changes(
    cfg: TariffConfig,
    start: datetime,
    end: datetime,
    *,
    require_confirmed: bool = False,
) -> list[CostSegment]:
    """Split ``[start, end)`` wherever the local tax day changes.

    A rate becomes effective at local midnight, so a span that crosses one must
    be priced as separate segments rather than with one blended multiplier.

    Args:
        cfg: tariff configuration.
        start: tz-aware inclusive span start.
        end: tz-aware exclusive span end, strictly after *start*.
        require_confirmed: raise when any segment's taxes are unconfirmed.

    Returns:
        One :class:`CostSegment` per local day covered, in time order.

    Raises:
        CostError: naive timestamps or ``end <= start``.
        TaxesUnconfirmedError: *require_confirmed* and a segment is unconfirmed.
    """
    tz = config_timezone(cfg)
    cursor = _aware_datetime(start, "start")
    limit = _aware_datetime(end, "end")
    if limit <= cursor:
        raise CostError(
            f"span must go forward in time, got [{cursor.isoformat()}, {limit.isoformat()}]"
        )

    segments: list[CostSegment] = []
    while cursor < limit:
        day = cursor.astimezone(tz).date()
        boundary = min(_next_local_midnight(day, tz), limit)
        if boundary <= cursor:  # a local midnight that does not exist in this zone
            boundary = limit
        multiplier = effective_tax_multiplier(cfg, day)
        segments.append(
            CostSegment(start=cursor, end=boundary, local_day=day, tax_multiplier=multiplier)
        )
        cursor = boundary
    if require_confirmed:
        for segment in segments:
            if not segment.confirmed:
                raise TaxesUnconfirmedError(
                    f"segment {segment.local_day.isoformat()} has no confirmed tax multiplier; "
                    "refusing a partial taxed sum"
                )
    return segments


def require_constant_tax_multiplier(cfg: TariffConfig, start: datetime, end: datetime) -> float:
    """Return the single multiplier valid across all of ``[start, end)``.

    Raises:
        TaxBoundaryError: the span crosses a tax rate change (it must be split).
        TaxesUnconfirmedError: the band is unconfirmed.
    """
    segments = split_span_at_tax_changes(cfg, start, end)
    distinct = sorted({segment.tax_multiplier for segment in segments})
    if len(distinct) > 1:
        days = ", ".join(segment.local_day.isoformat() for segment in segments)
        raise TaxBoundaryError(
            f"span [{start.isoformat()}, {end.isoformat()}) covers local days {days} with "
            f"different multipliers {[str(m) for m in distinct]}; price each interval with "
            "effective_tax_multipliers() or split with split_span_at_tax_changes()"
        )
    multiplier = distinct[0]
    if multiplier is None:
        raise TaxesUnconfirmedError(
            f"span [{start.isoformat()}, {end.isoformat()}) has no confirmed tax multiplier"
        )
    return multiplier


# ---------------------------------------------------------------------------
# Core cost arithmetic
# ---------------------------------------------------------------------------


def compute_interval_cost(
    *,
    energy_kwh: float,
    price_eur_per_kwh: float,
    tax_multiplier: float | None = None,
    basis: str | None = None,
) -> float:
    """Cost of one interval in ``EUR``.

    ``energy_kwh * price_eur_per_kwh`` is the pre-tax incremental energy cost;
    with a *tax_multiplier* the result is tax-inclusive.  A ``None`` multiplier
    therefore means **pre-tax only** — never a billed figure — and callers
    labelling the result must say so (see *basis*).

    Args:
        energy_kwh: imported energy, finite and ``>= 0`` (grid import only;
            export is not billed here).  ``0.0`` is valid measured data.
        price_eur_per_kwh: pre-tax PVPC price, finite, **may be negative** — a
            negative price yields a negative cost, it is not clamped.
        tax_multiplier: confirmed multiplier (``>= 1``, since fiscal rates are
            non-negative).
        basis: ``"untaxed"``/``"taxed"``, or ``None`` to infer from
            *tax_multiplier*.  Stating it is validated, so a pre-tax number can
            never be labelled billed and an already-taxed price cannot be taxed
            again by accident.

    Raises:
        CostError: non-finite input, negative energy, or a basis/multiplier
            contradiction.
        TaxesUnconfirmedError: ``basis="taxed"`` without a confirmed multiplier.
    """
    energy = _finite_float(energy_kwh, "energy_kwh")
    if energy < 0:
        raise CostError(
            f"energy_kwh must be >= 0 (grid import only), got {energy}; export credits are "
            "not modelled by this cost function"
        )
    price = _finite_float(price_eur_per_kwh, "price_eur_per_kwh")
    pretax = energy * price
    if tax_multiplier is None:
        _resolve_basis(basis, taxed=False, where="compute_interval_cost")
        return pretax
    multiplier = _validated_multiplier(tax_multiplier, "tax_multiplier")
    _resolve_basis(basis, taxed=True, where="compute_interval_cost")
    return pretax * multiplier


def _multipliers_for_rows(
    n: int,
    tax_multiplier: float | None,
    tax_multipliers: Sequence[float] | np.ndarray | None,
) -> np.ndarray | None:
    """Resolve the scalar/array multiplier forms into one array, or ``None``.

    ``NaN``/``inf`` entries in *tax_multipliers* are allowed as the
    "unconfirmed" marker produced by :func:`effective_tax_multipliers`; they are
    only fatal for intervals that actually enter the sum.
    """
    if tax_multiplier is not None and tax_multipliers is not None:
        raise CostError("pass either tax_multiplier or tax_multipliers, not both")
    if tax_multiplier is not None:
        return np.full(n, _validated_multiplier(tax_multiplier, "tax_multiplier"))
    if tax_multipliers is None:
        return None
    array = np.asarray(tax_multipliers, dtype=np.float64)
    if array.ndim != 1 or array.shape[0] != n:
        raise CostError(f"tax_multipliers must have {n} entries, got {array.size}")
    confirmed = array[np.isfinite(array)]
    if confirmed.size and float(confirmed.min()) < 1.0:
        raise CostError("tax_multipliers must all be >= 1 for non-negative fiscal rates")
    return array


@dataclass(frozen=True)
class CostSummary:
    """A period cost with its denominator and every exclusion disclosed.

    ``n_intervals`` is what was offered, ``n_counted`` what entered the sum, and
    ``n_excluded`` what did not — so a figure can never be read as complete when
    most of the period was dropped.
    """

    basis: str = COST_BASIS_UNTAXED
    tax_applied: bool = False
    cost_eur: float = 0.0
    pretax_cost_eur: float = 0.0
    tax_uplift_eur: float = 0.0
    energy_kwh: float = 0.0
    n_intervals: int = 0
    n_counted: int = 0
    n_excluded: int = 0
    excluded_reasons: dict[str, int] = field(default_factory=dict)
    currency: str = CURRENCY

    @property
    def is_full_invoice(self) -> bool:
        """Always ``False``: energy only, no fixed/power terms, no fiscal minimum."""
        return False

    @property
    def billed(self) -> bool:
        """Whether the figure may honestly be described as billed."""
        return self.basis == COST_BASIS_TAXED and self.tax_applied

    @property
    def coverage(self) -> float:
        """Fraction of offered intervals that entered the sum (0.0 when empty)."""
        return self.n_counted / self.n_intervals if self.n_intervals else 0.0

    def as_dict(self) -> dict[str, Any]:
        """Plain-dict form for report rendering."""
        return {
            "basis": self.basis,
            "tax_applied": self.tax_applied,
            "billed": self.billed,
            "is_full_invoice": self.is_full_invoice,
            "cost_eur": self.cost_eur,
            "pretax_cost_eur": self.pretax_cost_eur,
            "tax_uplift_eur": self.tax_uplift_eur,
            "energy_kwh": self.energy_kwh,
            "n_intervals": self.n_intervals,
            "n_counted": self.n_counted,
            "n_excluded": self.n_excluded,
            "coverage": self.coverage,
            "excluded_reasons": dict(self.excluded_reasons),
            "currency": self.currency,
        }


def compute_period_summary(
    *,
    energies_kwh: Sequence[float] | np.ndarray,
    prices_eur_per_kwh: Sequence[float] | np.ndarray,
    tax_multiplier: float | None = None,
    tax_multipliers: Sequence[float] | np.ndarray | None = None,
    usable: Sequence[bool] | np.ndarray | None = None,
    basis: str | None = None,
) -> CostSummary:
    """Sum interval costs over a period, with mask, denominator and exclusions.

    Same arithmetic as :func:`compute_period_cost`, but it also reports how many
    intervals were counted and why the rest were dropped.  Money is never
    rounded here, and ``pretax_cost_eur`` / ``tax_uplift_eur`` keep the energy
    cost separate from the tax gross-up so a report can show both without ever
    taxing twice.

    Args:
        energies_kwh: per-interval import, each finite and ``>= 0``.  ``None``
            means missing energy: excluded, never read as zero.
        prices_eur_per_kwh: pre-tax prices, may be negative.  ``None`` excluded.
        tax_multiplier: one confirmed multiplier for the whole period — valid
            only if no rate change falls inside it (see
            :func:`require_constant_tax_multiplier`).
        tax_multipliers: per-interval multipliers from
            :func:`effective_tax_multipliers`; the rate-change-safe form.
        usable: selection mask, normally from :func:`usable_intervals`.
        basis: ``"untaxed"``/``"taxed"``, or ``None`` to infer.

    Raises:
        CostError: length mismatch, negative or non-finite counted values.
        TaxesUnconfirmedError: taxed basis while some *counted* interval has no
            confirmed multiplier — no partial sums.
    """
    energies = np.asarray(energies_kwh, dtype=object)
    prices = np.asarray(prices_eur_per_kwh, dtype=object)
    if energies.ndim != 1 or prices.ndim != 1:
        raise CostError("energies_kwh and prices_eur_per_kwh must be 1-dimensional")
    if energies.shape != prices.shape:
        raise CostError(
            f"energies_kwh has {energies.size} entries, prices_eur_per_kwh has {prices.size}"
        )
    n = int(energies.size)

    if usable is None:
        selected = np.ones(n, dtype=bool)
    else:
        selected = np.asarray(usable, dtype=bool)
        if selected.ndim != 1 or selected.shape[0] != n:
            raise CostError(f"usable mask has {selected.size} entries, expected {n}")

    multipliers = _multipliers_for_rows(n, tax_multiplier, tax_multipliers)
    label = _resolve_basis(basis, taxed=multipliers is not None, where="compute_period_summary")
    taxed = label == COST_BASIS_TAXED

    # Pre-validate every non-null numeric value regardless of the mask: a negative
    # import energy is a data error anywhere in the input, and the mask means
    # "don't count it", not "the data is fine".  None / NaN are allowed on
    # excluded rows (that is exactly how missing data is represented).
    for index in range(n):
        energy = energies[index]
        price = prices[index]
        if energy is None or (isinstance(energy, float) and math.isnan(energy)):
            continue
        if price is None or (isinstance(price, float) and math.isnan(price)):
            continue
        energy_value = _finite_float(energy, f"energies_kwh[{index}]")
        if energy_value < 0:
            raise CostError(
                f"energies_kwh[{index}] is {energy_value}: imported energy must be >= 0"
            )
        _finite_float(price, f"prices_eur_per_kwh[{index}]")

    reasons: dict[str, int] = {}
    counted = 0
    cost = 0.0
    pretax_cost = 0.0
    energy_total = 0.0
    for index in range(n):
        if not selected[index]:
            reasons[REASON_NOT_SELECTED] = reasons.get(REASON_NOT_SELECTED, 0) + 1
            continue
        energy = energies[index]
        price = prices[index]
        if energy is None or (isinstance(energy, float) and math.isnan(energy)):
            reasons[REASON_ENERGY_MISSING] = reasons.get(REASON_ENERGY_MISSING, 0) + 1
            continue
        if price is None or (isinstance(price, float) and math.isnan(price)):
            reasons[REASON_PRICE_MISSING] = reasons.get(REASON_PRICE_MISSING, 0) + 1
            continue
        energy_value = float(energy)
        price_value = float(price)
        base = energy_value * price_value
        if taxed:
            assert multipliers is not None
            multiplier = float(multipliers[index])
            if not math.isfinite(multiplier):
                raise TaxesUnconfirmedError(
                    f"interval {index} has no confirmed tax multiplier; taxed mode refuses to "
                    f"sum only the {counted} intervals that happen to have one"
                )
            cost += base * multiplier
            pretax_cost += base
            energy_total += energy_value
            counted += 1
            continue
        cost += base
        pretax_cost += base
        energy_total += energy_value
        counted += 1

    return CostSummary(
        basis=label,
        tax_applied=taxed,
        cost_eur=cost,
        pretax_cost_eur=pretax_cost,
        tax_uplift_eur=cost - pretax_cost,
        energy_kwh=energy_total,
        n_intervals=n,
        n_counted=counted,
        n_excluded=n - counted,
        excluded_reasons=reasons,
    )


def compute_period_cost(
    *,
    energies_kwh: list[float],
    prices_eur_per_kwh: list[float],
    tax_multiplier: float | None = None,
    tax_multipliers: Sequence[float] | np.ndarray | None = None,
    usable: Sequence[bool] | np.ndarray | None = None,
    basis: str | None = None,
) -> float:
    """Total ``EUR`` cost over a list of 15-minute intervals.

    Returns just the figure; :func:`compute_period_summary` gives the same sum
    with its denominator and exclusion breakdown.  Unusable rows are left out by
    the caller's *usable* mask — nothing here estimates them, and ``None`` energy
    or price entries are excluded rather than read as zero.

    Raises:
        CostError: length mismatch, or negative/non-finite counted values.
        TaxesUnconfirmedError: taxed basis without confirmed rates for every
            counted interval.
    """
    return compute_period_summary(
        energies_kwh=energies_kwh,
        prices_eur_per_kwh=prices_eur_per_kwh,
        tax_multiplier=tax_multiplier,
        tax_multipliers=tax_multipliers,
        usable=usable,
        basis=basis,
    ).cost_eur


# ---------------------------------------------------------------------------
# Array helpers
# ---------------------------------------------------------------------------


def effective_tax_multipliers(cfg: TariffConfig, starts: Any) -> np.ndarray:
    """Per-interval tax multipliers for a column of tz-aware start timestamps.

    This is the rate-change-safe form: every interval is priced with the band of
    its own **local** day, so an array spanning local midnight at a rate change
    gets two different multipliers instead of one blended guess.

    Returns:
        ``float64`` array of multipliers, ``NaN`` where taxes are unconfirmed.
        Taxed consumers must refuse on ``NaN`` — :func:`effective_prices` and
        :func:`account_intervals` do exactly that.
    """
    stamps = _aware_datetimes(starts)
    tz = config_timezone(cfg)
    cache: dict[date, float | None] = {}
    out = np.empty(len(stamps), dtype=np.float64)
    for index, stamp in enumerate(stamps):
        day = stamp.astimezone(tz).date()
        if day not in cache:
            cache[day] = effective_tax_multiplier(cfg, day)
        out[index] = np.nan if cache[day] is None else cache[day]
    return out


def effective_prices(
    cfg: TariffConfig,
    starts: Any,
    prices: Any,
    basis: str = COST_BASIS_UNTAXED,
) -> np.ndarray:
    """Prices in the requested *basis*, as a ``float64`` array.

    ``"untaxed"`` returns the pre-tax prices unchanged — no tolls are added,
    because the PVPC price already includes access tolls, charges and production
    cost.  ``"taxed"`` multiplies each price by the multiplier of **its own local
    day**, which is what keeps a period spanning a rate change honest.

    Args:
        starts: tz-aware interval-start timestamps (pyarrow column, or a
            sequence of datetimes / ISO-8601 strings with offsets).
        prices: pre-tax ``€/kWh``, all finite.  Missing prices must be filtered
            out first (see :func:`usable_intervals`); they are never estimated.
        basis: ``"untaxed"`` or ``"taxed"``.

    Returns:
        ``float64`` array of prices in the requested basis, same length as input.

    Raises:
        CostError: non-finite or missing price, length mismatch, naive stamps.
        TaxesUnconfirmedError: ``basis="taxed"`` with unconfirmed taxes on any
            interval — there is no partially-taxed price array.
    """
    if basis not in VALID_COST_BASES:
        raise CostError(f"basis must be one of {sorted(VALID_COST_BASES)}, got {basis!r}")
    stamps = _aware_datetimes(starts)
    values = [
        _finite_float(price, f"prices[{index}]") for index, price in enumerate(_iter_prices(prices))
    ]
    if len(values) != len(stamps):
        raise CostError(f"starts has {len(stamps)} entries, prices has {len(values)}")
    array = np.asarray(values, dtype=np.float64)
    if basis == COST_BASIS_UNTAXED:
        return array

    multipliers = effective_tax_multipliers(cfg, stamps)
    unconfirmed = np.flatnonzero(~np.isfinite(multipliers))
    if unconfirmed.size:
        tz = config_timezone(cfg)
        days = sorted({stamps[int(i)].astimezone(tz).date().isoformat() for i in unconfirmed})
        raise TaxesUnconfirmedError(
            f"basis='taxed' but {int(unconfirmed.size)} of {multipliers.size} intervals fall on "
            f"local days with unconfirmed taxes ({', '.join(days[:4])}); confirm tariff.yaml "
            "or report the pre-tax basis"
        )
    return array * multipliers


def _iter_prices(prices: Any) -> list[Any]:
    """Normalise a price column to a plain list (pyarrow, numpy or sequence)."""
    to_pylist = getattr(prices, "to_pylist", None)
    if callable(to_pylist):
        return to_pylist()
    if isinstance(prices, np.ndarray):
        return prices.tolist()
    if isinstance(prices, (str, bytes)) or not hasattr(prices, "__iter__"):
        raise CostError(f"prices must be a sequence of numbers, got {prices!r}")
    return list(prices)


# ---------------------------------------------------------------------------
# Interval selection (usable mask)
# ---------------------------------------------------------------------------


def _is_column(value: Any) -> bool:
    return hasattr(value, "__len__") and not isinstance(value, (str, bytes))


def _rows_from(intervals: Any) -> list[Mapping[str, Any]]:
    """Normalise interval input to a list of row mappings.

    Accepts a pyarrow Table/RecordBatch, a ``to_pydict()``-style column mapping,
    or any sequence of row mappings.
    """
    if intervals is None:
        raise CostError("intervals must not be None")
    to_pylist = getattr(intervals, "to_pylist", None)
    if callable(to_pylist):
        return list(to_pylist())
    if isinstance(intervals, Mapping):
        columns = dict(intervals)
        if not columns:
            return []
        if not all(_is_column(values) for values in columns.values()):
            raise CostError(
                "intervals as a mapping must be columnar (every value a sequence of equal "
                "length); pass a list of row mappings for a single row"
            )
        lengths = {name: len(values) for name, values in columns.items()}
        unique = set(lengths.values())
        if len(unique) != 1:
            raise CostError(f"interval columns have different lengths: {lengths}")
        size = unique.pop()
        keys = list(columns)
        return [{key: columns[key][index] for key in keys} for index in range(size)]
    if isinstance(intervals, (str, bytes)) or not hasattr(intervals, "__iter__"):
        raise CostError(
            f"intervals must be a table or a sequence of rows, got {type(intervals).__name__}"
        )
    rows = list(intervals)
    for index, row in enumerate(rows):
        if not isinstance(row, Mapping):
            raise CostError(f"intervals[{index}] is {type(row).__name__}, expected row mappings")
    return rows


def _row_value(row: Mapping[str, Any], *names: str) -> Any:
    """First present column among *names* (the analysis grid uses ``gross_kwh``)."""
    for name in names:
        if name in row:
            return row[name]
    return None


def classify_interval(
    row: Mapping[str, Any],
    *,
    energy_quality: frozenset[str] = USABLE_ENERGY_QUALITY,
    allocation_quality: frozenset[str] = USABLE_ALLOCATION_QUALITY,
    price_quality: frozenset[str] = USABLE_PRICE_QUALITY,
) -> str | None:
    """Return the exclusion reason for one interval row, or ``None`` if usable.

    A row is usable when it carries a real measured energy number, a price that
    was actually published for that interval, and a known placement of that
    energy in time.  ``0.0 kWh`` is usable data (a genuinely idle interval);
    ``None`` is missing and is excluded.  A negative price is usable — it is the
    real (rare, negative) PVPC price, and it makes the cost negative.
    """
    for name, allowed, reason in (
        ("energy_quality", energy_quality, REASON_ENERGY_QUALITY),
        ("allocation_quality", allocation_quality, REASON_ALLOCATION_QUALITY),
        ("price_quality", price_quality, REASON_PRICE_QUALITY),
    ):
        value = _row_value(row, name)
        if not isinstance(value, str) or value not in allowed:
            return reason

    energy = _row_value(row, "gross_kwh", "load_kwh", "energy_kwh")
    if energy is None:
        return REASON_ENERGY_MISSING
    try:
        energy_value = _finite_float(energy, "gross_kwh")
    except CostError:
        return REASON_ENERGY_INVALID
    if energy_value < 0:
        return REASON_ENERGY_INVALID

    price = _row_value(row, "price_eur_per_kwh")
    if price is None:
        return REASON_PRICE_MISSING
    try:
        _finite_float(price, "price_eur_per_kwh")
    except CostError:
        return REASON_PRICE_INVALID
    return None


def usable_intervals(
    intervals: Any,
    *,
    energy_quality: frozenset[str] = USABLE_ENERGY_QUALITY,
    allocation_quality: frozenset[str] = USABLE_ALLOCATION_QUALITY,
    price_quality: frozenset[str] = USABLE_PRICE_QUALITY,
) -> np.ndarray:
    """Boolean mask of the intervals that may enter a cost total.

    The defaults are deliberately strict — ``energy_quality="measured"``,
    ``allocation_quality="exact"``, ``price_quality="measured"`` — so a
    forward-filled price or a right-endpoint-attributed gap never quietly becomes
    a billed euro.  Widen any of them by passing a larger set, e.g.
    ``price_quality=USABLE_PRICE_QUALITY | {PRICE_FORWARD_FILLED}``: that is an
    opt-in, and the resulting exclusions stay visible in
    :func:`compute_period_summary` and :func:`account_intervals`.

    Returns:
        ``bool`` ndarray, one entry per row, in input order.
    """
    rows = _rows_from(intervals)
    mask = np.empty(len(rows), dtype=bool)
    for index, row in enumerate(rows):
        mask[index] = (
            classify_interval(
                row,
                energy_quality=energy_quality,
                allocation_quality=allocation_quality,
                price_quality=price_quality,
            )
            is None
        )
    return mask


# ---------------------------------------------------------------------------
# Monthly accounting (the report integration surface)
# ---------------------------------------------------------------------------

_SUBSECOND_TOLERANCE = 1e-6


def _month_bounds(tz: ZoneInfo, year: int, month: int) -> tuple[datetime, datetime]:
    """``[local month start, local next month start)`` as instants in UTC."""
    start = datetime(year, month, 1, tzinfo=tz)
    if month == 12:
        end = datetime(year + 1, 1, 1, tzinfo=tz)
    else:
        end = datetime(year, month + 1, 1, tzinfo=tz)
    # Convert before subtracting: two wall-clock midnights in the *same* zone
    # compare as 24*k hours (Python ignores a shared tzinfo in subtraction),
    # which is exactly the DST-blind arithmetic that would hide a 23/25-hour day.
    return start.astimezone(UTC), end.astimezone(UTC)


def _expected_intervals(tz: ZoneInfo, year: int, month: int, interval_seconds: int) -> int:
    """How many intervals the local month really holds, DST included.

    Madrid's spring-forward day has 23 hours and the fall-back day 25, so the
    count comes from the UTC distance between the two local midnights — never
    from ``days * 96``.
    """
    start, end = _month_bounds(tz, year, month)
    span = (end - start).total_seconds()
    expected, remainder = divmod(span, float(interval_seconds))
    if remainder > _SUBSECOND_TOLERANCE:
        raise CostError(
            f"local month {year:04d}-{month:02d} spans {span}s, not a whole number of "
            f"{interval_seconds}s intervals; the grid does not tile {tz.key}"
        )
    return int(expected)


def _new_account(local: datetime) -> dict[str, Any]:
    return {
        "observed_intervals": 0,
        "usable_intervals": 0,
        "excluded_intervals": 0,
        "excluded_reasons": {},
        "energy_kwh": 0.0,
        "pretax_cost_eur": 0.0,
        "tax_uplift_eur": 0.0,
        "cost_eur": 0.0,
        "_multipliers": set(),
        "_first_local_day": local.date(),
        "_last_local_day": local.date(),
    }


def account_intervals(
    intervals: Any,
    cfg: TariffConfig,
    basis: str = COST_BASIS_UNTAXED,
    *,
    interval_seconds: int = INTERVAL_SECONDS,
    energy_quality: frozenset[str] = USABLE_ENERGY_QUALITY,
    allocation_quality: frozenset[str] = USABLE_ALLOCATION_QUALITY,
    price_quality: frozenset[str] = USABLE_PRICE_QUALITY,
) -> dict[str, Any]:
    """Group per-interval rows into **local** monthly cost accounts.

    The input is the analysis grid — an ``ANALYSIS_INTERVAL`` pyarrow Table, a
    column mapping, or a list of row mappings carrying ``interval_start``,
    ``gross_kwh``, ``price_eur_per_kwh`` and the three quality columns.  The
    output is a plain dict, ready for the report layer with no further cost
    arithmetic.  Each month (and ``totals``) carries:

    ``observed_intervals``
        rows offered for that local month.
    ``usable_intervals`` / ``excluded_intervals`` / ``excluded_reasons``
        what entered the sum and, by reason code, everything that did not.
    ``expected_intervals`` / ``missing_intervals`` / ``missing_hours``
        how many intervals the local month really has and how much of it has no
        row at all — declared, never silently zero.
    ``energy_kwh`` ``cost_eur`` ``pretax_cost_eur`` ``tax_uplift_eur``
        counted energy and the cost in the requested basis, with the tax gross-up
        broken out so it can never be applied twice.
    ``mean_price_eur_per_kwh``
        energy-weighted pre-tax price (``None`` when no energy was counted).
    ``tax_multipliers``
        the distinct confirmed multipliers used (empty for ``basis="untaxed"``;
        more than one means the rate changed mid-month and was split).
    ``first_local_day`` / ``last_local_day``
        local dates actually covered.

    Properties worth stating explicitly:

    * Months are **local** calendar months in ``cfg.tariff_periods.timezone``:
      ``2026-01-31T23:30:00Z`` is accounted in February, and a fall-back day
      contributes 97 intervals, so ``missing_intervals`` is not a lie.
    * Nothing is estimated.  Unusable rows are excluded and counted; time with no
      row at all is declared missing.
    * ``basis="taxed"`` prices every interval with its own local day's band and
      refuses outright if a counted interval's taxes are unconfirmed.  With the
      shipped ``tariff.yaml`` (null rates) only ``"untaxed"`` works, and
      ``billed`` stays ``False`` — the report must say "pre-tax, not billed".
    * ``is_full_invoice`` is ``False``: fixed charges, power terms and the fiscal
      minimum are out of scope, so these figures cannot be reconciled against an
      invoice total.
    * No per-interval rounding, and no tolls added (PVPC includes them).

    Args:
        intervals: analysis-grid rows (table, column mapping, or row mappings).
        cfg: tariff configuration (timezone + tax bands).
        basis: ``"untaxed"`` (default) or ``"taxed"``.
        interval_seconds: grid length; rows carrying ``interval_end`` are
            checked against it.
        energy_quality: / allocation_quality: / price_quality: the quality values
            accepted as usable (defaults are the strict ones).

    Returns:
        ``dict`` with ``schema``, ``currency``, ``basis``, ``taxed``, ``billed``,
        ``is_full_invoice``, ``timezone``, ``interval_seconds``, ``months``
        (``"YYYY-MM"`` keys, sorted), ``totals``, ``warnings`` and ``caveats``.

    Raises:
        CostError: bad basis/interval_seconds, naive timestamps, a row whose
            ``interval_end`` disagrees with the grid, or duplicate
            ``interval_start`` rows (double counting).
        TaxesUnconfirmedError: ``basis="taxed"`` with unconfirmed taxes.
    """
    if basis not in VALID_COST_BASES:
        raise CostError(f"basis must be one of {sorted(VALID_COST_BASES)}, got {basis!r}")
    if interval_seconds <= 0:
        raise CostError(f"interval_seconds must be positive, got {interval_seconds}")
    taxed = basis == COST_BASIS_TAXED
    tz = config_timezone(cfg)
    rows = _rows_from(intervals)

    months: dict[str, dict[str, Any]] = {}
    seen_starts: dict[datetime, int] = {}
    warnings: list[str] = []

    for index, row in enumerate(rows):
        start = _aware_datetime(
            _row_value(row, "interval_start", "start"), f"intervals[{index}].interval_start"
        )
        end = _row_value(row, "interval_end", "end")
        if end is not None:
            duration = (
                _aware_datetime(end, f"intervals[{index}].interval_end") - start
            ).total_seconds()
            if duration != float(interval_seconds):
                raise CostError(
                    f"intervals[{index}] lasts {duration}s but "
                    f"interval_seconds={interval_seconds}; "
                    "a mixed-length grid would double-count or drop time"
                )
        duplicate = seen_starts.get(start)
        if duplicate is not None:
            raise CostError(
                f"interval_start {start.isoformat()} appears twice (rows {duplicate} and {index}); "
                "refusing to sum a double-counted grid"
            )
        seen_starts[start] = index

        local = start.astimezone(tz)
        key = f"{local.year:04d}-{local.month:02d}"
        account = months.setdefault(key, _new_account(local))
        account["observed_intervals"] += 1
        account["_first_local_day"] = min(account["_first_local_day"], local.date())
        account["_last_local_day"] = max(account["_last_local_day"], local.date())

        reason = classify_interval(
            row,
            energy_quality=energy_quality,
            allocation_quality=allocation_quality,
            price_quality=price_quality,
        )
        if reason is not None:
            account["excluded_intervals"] += 1
            counts = account["excluded_reasons"]
            counts[reason] = counts.get(reason, 0) + 1
            continue

        energy = _finite_float(_row_value(row, "gross_kwh", "load_kwh", "energy_kwh"), "gross_kwh")
        price = _finite_float(_row_value(row, "price_eur_per_kwh"), "price_eur_per_kwh")
        base = energy * price
        account["pretax_cost_eur"] += base
        account["energy_kwh"] += energy
        account["usable_intervals"] += 1
        if taxed:
            multiplier = require_tax_multiplier(cfg, start)
            account["_multipliers"].add(multiplier)
            account["cost_eur"] += base * multiplier
            account["tax_uplift_eur"] += base * (multiplier - 1.0)
        else:
            account["cost_eur"] += base

    for key, account in sorted(months.items()):
        year, month = (int(part) for part in key.split("-"))
        expected = _expected_intervals(tz, year, month, interval_seconds)
        missing = expected - account["observed_intervals"]
        if missing < 0:
            raise CostError(
                f"{key} has {account['observed_intervals']} rows but {tz.key} has only "
                f"{expected} intervals that month; the grid overlaps a month boundary"
            )
        multipliers = sorted(account.pop("_multipliers"))
        energy = account["energy_kwh"]
        account["expected_intervals"] = expected
        account["missing_intervals"] = missing
        account["missing_hours"] = missing * interval_seconds / 3600.0
        account["mean_price_eur_per_kwh"] = (
            account["pretax_cost_eur"] / energy if energy > 0 else None
        )
        account["tax_multipliers"] = multipliers
        account["first_local_day"] = account.pop("_first_local_day").isoformat()
        account["last_local_day"] = account.pop("_last_local_day").isoformat()
        if missing:
            warnings.append(
                f"{key}: {missing} interval(s) of {tz.key} time have no row at all "
                f"({missing * interval_seconds / 3600.0:g} h) — declared, not estimated"
            )
        if len(multipliers) > 1:
            warnings.append(
                f"{key}: tax multiplier changed mid-month ({multipliers}); costs are split "
                "per local day, never blended"
            )
        if account["excluded_intervals"]:
            warnings.append(
                f"{key}: {account['excluded_intervals']} of {account['observed_intervals']} "
                f"intervals excluded, reason counts {account['excluded_reasons']}"
            )

    excluded_total = sum(account["excluded_intervals"] for account in months.values())
    reasons_total: dict[str, int] = {}
    for account in months.values():
        for reason, count in account["excluded_reasons"].items():
            reasons_total[reason] = reasons_total.get(reason, 0) + count
    usable_total = sum(account["usable_intervals"] for account in months.values())
    energy_total = sum(account["energy_kwh"] for account in months.values())
    pretax_total = sum(account["pretax_cost_eur"] for account in months.values())
    missing_total = sum(account["missing_intervals"] for account in months.values())

    totals = {
        "observed_intervals": len(rows),
        "usable_intervals": usable_total,
        "excluded_intervals": excluded_total,
        "excluded_reasons": reasons_total,
        "expected_intervals": sum(a["expected_intervals"] for a in months.values()),
        "missing_intervals": missing_total,
        "missing_hours": missing_total * interval_seconds / 3600.0,
        "energy_kwh": energy_total,
        "cost_eur": sum(a["cost_eur"] for a in months.values()),
        "pretax_cost_eur": pretax_total,
        "tax_uplift_eur": sum(a["tax_uplift_eur"] for a in months.values()),
        "mean_price_eur_per_kwh": pretax_total / energy_total if energy_total > 0 else None,
        "tax_multipliers": sorted({m for a in months.values() for m in a["tax_multipliers"]}),
    }
    if not rows:
        warnings.append(
            "no intervals offered: every monthly account is empty by absence, not by zero"
        )

    return {
        "schema": "cost.account.v1",
        "currency": CURRENCY,
        "basis": basis,
        "taxed": taxed,
        # "billed" is only ever true when the taxes behind the figure are confirmed.
        "billed": taxed,
        "is_full_invoice": False,
        "timezone": tz.key,
        "interval_seconds": interval_seconds,
        "quality_accepts": {
            "energy_quality": sorted(energy_quality),
            "allocation_quality": sorted(allocation_quality),
            "price_quality": sorted(price_quality),
        },
        "months": {key: months[key] for key in sorted(months)},
        "totals": totals,
        "warnings": warnings,
        "caveats": [
            "energy-only cost: no fixed charges, power terms or fiscal minimum",
            "not reconcilable against an invoice total (is_full_invoice=false)",
            "PVPC prices already include tolls and charges; only taxes are grossed up",
            "excluded and missing intervals are counted, never estimated",
            "money is unrounded: rounding belongs to display, not per-interval maths",
        ],
    }
