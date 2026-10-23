"""Tariff configuration loading and validation.

Loads ``tariff.yaml`` into frozen dataclasses.  Raises loudly when a
required value is ``null`` (unconfirmed) and a function needs it.

Tariff period boundaries are **configuration data, never code**: the P1/P2/P3
bands are declared per day type (``weekday`` / ``saturday`` / ``sunday``, plus a
day-type to which configured holidays are mapped) as half-open local-hour ranges
in ``tariff_periods.timezone``.  Looking up a day type whose rules were never
configured raises instead of falling back to a hardcoded table, so an
un-sourced holiday calendar can never silently become an assumption.
"""

from __future__ import annotations

import os
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from datetime import date, datetime
from pathlib import Path
from typing import Any

import yaml

# ---------------------------------------------------------------------------
# Day types
# ---------------------------------------------------------------------------

WEEKDAY: str = "weekday"
SATURDAY: str = "saturday"
SUNDAY: str = "sunday"

#: The day-type names ``tariff_periods.rules`` may configure.
DAY_TYPES: tuple[str, ...] = (WEEKDAY, SATURDAY, SUNDAY)

#: A day is tiled over ``[0, MAX_HOUR)`` local hours; ``end_hour`` may be 24.
MAX_HOUR: int = 24

#: The tariff periods a 2.0TD-style day can resolve to.
TARIFF_PERIOD_VALUES: tuple[int, ...] = (1, 2, 3)

#: The keys every entry of ``tariff_periods.rules.<day type>`` must carry.
RULE_KEYS: tuple[str, ...] = ("period", "start_hour", "end_hour")

# ---------------------------------------------------------------------------
# Dataclasses
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class TaxEntry:
    """A date-banded tax configuration row."""

    valid_from: date
    iee_pct: float | None
    iva_pct: float | None
    source: str


@dataclass(frozen=True)
class PeriodRule:
    """One local-hour band that maps to a tariff period.

    ``[start_hour, end_hour)`` is half-open and expressed in **local wall-clock
    hours**, so ``end_hour=24`` closes a day and the rule owning hour 23 is the
    one whose range contains 23.
    """

    period: int
    start_hour: int
    end_hour: int

    def covers(self, hour: int) -> bool:
        """Whether *hour* is inside this rule's half-open range."""
        return self.start_hour <= hour < self.end_hour


@dataclass(frozen=True)
class TariffPeriods:
    """Tariff period definitions.

    The timezone plus the per-day-type local-hour rules; ``holidays`` holds the
    dates that were actually sourced and ``holiday_day_type`` is the day type
    they borrow their bands from.  An empty rule tuple means *not configured*,
    which :func:`tariff_period_for` refuses to guess about.
    """

    timezone: str
    weekday: tuple[PeriodRule, ...] = ()
    saturday: tuple[PeriodRule, ...] = ()
    sunday: tuple[PeriodRule, ...] = ()
    holidays: tuple[date, ...] = ()
    holiday_day_type: str = SUNDAY

    def rules_for(self, name: str) -> tuple[PeriodRule, ...]:
        """The rules configured for the day-type *name*.

        Raises:
            ValueError: *name* is not one of :data:`DAY_TYPES`.
        """
        if name == WEEKDAY:
            return self.weekday
        if name == SATURDAY:
            return self.saturday
        if name == SUNDAY:
            return self.sunday
        raise ValueError(f"unknown day type {name!r} — expected one of {list(DAY_TYPES)}")

    def day_type(self, day: date) -> str:
        """The day-type name that applies to the local calendar *day*.

        A date listed in :attr:`holidays` reports :attr:`holiday_day_type`
        instead of its natural type; every other date is classified by the
        weekday of the calendar itself, and an unsourced holiday therefore
        keeps its ordinary (weekday) bands.
        """
        if day in self.holidays:
            return self.holiday_day_type
        ordinal = day.weekday()  # Monday == 0 … Sunday == 6
        if ordinal == 5:
            return SATURDAY
        if ordinal == 6:
            return SUNDAY
        return WEEKDAY


@dataclass(frozen=True)
class BatteryConfig:
    """Battery physical parameters.

    ``None`` fields are unconfirmed and must be set before the
    corresponding feature (simulator, cost model) can run.
    """

    capacity_kwh: float
    power_kw: float
    round_trip_efficiency: float | None
    soc_window_min: float | None
    soc_window_max: float | None
    import_limit_kw: float | None
    initial_soc_frac: float | None = None


@dataclass(frozen=True)
class TariffConfig:
    """Root tariff configuration."""

    version: int
    taxes: list[TaxEntry]
    tariff_periods: TariffPeriods
    battery: BatteryConfig


# ---------------------------------------------------------------------------
# Loading
# ---------------------------------------------------------------------------


def _parse_date(value: str | date) -> date:
    if isinstance(value, date):
        return value
    return date.fromisoformat(value)


def _rule_error(day_type: str, index: int, detail: str) -> ValueError:
    """A parse error pointing at one rule in the document."""
    return ValueError(f"tariff_periods.rules.{day_type}[{index}]: {detail}")


def _parse_period_rule(day_type: str, index: int, entry: Any) -> PeriodRule:
    """Build one :class:`PeriodRule` from a YAML mapping, validating its shape."""
    if not isinstance(entry, Mapping):
        raise _rule_error(day_type, index, f"must be a mapping, got {type(entry).__name__}")

    missing = [key for key in RULE_KEYS if key not in entry]
    if missing:
        raise _rule_error(day_type, index, f"missing required keys: {missing}")

    for key in RULE_KEYS:
        value = entry[key]
        if isinstance(value, bool) or not isinstance(value, int):
            raise _rule_error(day_type, index, f"{key} must be an integer, got {value!r}")

    rule = PeriodRule(
        period=entry["period"],
        start_hour=entry["start_hour"],
        end_hour=entry["end_hour"],
    )
    if rule.period not in TARIFF_PERIOD_VALUES:
        raise _rule_error(
            day_type, index, f"period must be one of {TARIFF_PERIOD_VALUES}, got {rule.period}"
        )
    if not 0 <= rule.start_hour < rule.end_hour <= MAX_HOUR:
        raise _rule_error(
            day_type,
            index,
            f"hours must satisfy 0 <= start_hour < end_hour <= {MAX_HOUR}, got "
            f"[{rule.start_hour}, {rule.end_hour})",
        )
    return rule


def _validate_tiles_day(day_type: str, rules: tuple[PeriodRule, ...]) -> None:
    """Require *rules* to cover each of the 24 local hours exactly once.

    An empty tuple is *not* a tiling failure here: it is the "not configured"
    case, and :func:`tariff_period_for` is the place that refuses to guess.
    """
    if not rules:
        return

    counts = [0] * MAX_HOUR
    for rule in rules:
        for hour in range(rule.start_hour, rule.end_hour):
            counts[hour] += 1

    uncovered = [hour for hour, count in enumerate(counts) if count == 0]
    doubled = [hour for hour, count in enumerate(counts) if count > 1]
    if uncovered:
        raise ValueError(
            f"tariff_periods.rules.{day_type} must tile hours 0-{MAX_HOUR} exactly: "
            f"uncovered hours={uncovered}"
        )
    if doubled:
        raise ValueError(
            f"tariff_periods.rules.{day_type} must tile hours 0-{MAX_HOUR} exactly: "
            f"doubly-covered hours={doubled}"
        )


def _parse_tariff_periods(tp_raw: Mapping[str, Any]) -> TariffPeriods:
    """Parse and validate the ``tariff_periods`` block."""
    rules_raw = tp_raw.get("rules")
    if rules_raw is None:
        rules_raw = {}
    if not isinstance(rules_raw, Mapping):
        raise ValueError(
            f"tariff_periods.rules must be a mapping of day type to rules, "
            f"got {type(rules_raw).__name__}"
        )

    unknown = sorted(str(key) for key in rules_raw if key not in DAY_TYPES)
    if unknown:
        raise ValueError(
            f"tariff_periods.rules has unknown day types: {unknown} — "
            f"allowed day types are {list(DAY_TYPES)}"
        )

    parsed: dict[str, tuple[PeriodRule, ...]] = {}
    for day_type in DAY_TYPES:
        entries = rules_raw.get(day_type)
        if entries is None:
            entries = ()
        if isinstance(entries, (str, bytes)) or not isinstance(entries, Sequence):
            raise ValueError(
                f"tariff_periods.rules.{day_type} must be a list of rules, "
                f"got {type(entries).__name__}"
            )
        rules = tuple(
            _parse_period_rule(day_type, index, entry) for index, entry in enumerate(entries)
        )
        _validate_tiles_day(day_type, rules)
        parsed[day_type] = rules

    holiday_day_type = tp_raw.get("holiday_day_type", SUNDAY)
    if holiday_day_type not in DAY_TYPES:
        raise ValueError(
            f"tariff_periods.holiday_day_type must be one of {list(DAY_TYPES)}, "
            f"got {holiday_day_type!r}"
        )

    holidays_raw = tp_raw.get("holidays") or ()
    if isinstance(holidays_raw, (str, bytes)) or not isinstance(holidays_raw, Sequence):
        raise ValueError(
            f"tariff_periods.holidays must be a list of ISO dates, "
            f"got {type(holidays_raw).__name__}"
        )
    holidays = tuple(sorted(_parse_date(value) for value in holidays_raw))

    return TariffPeriods(
        timezone=tp_raw.get("timezone", "UTC"),
        weekday=parsed[WEEKDAY],
        saturday=parsed[SATURDAY],
        sunday=parsed[SUNDAY],
        holidays=holidays,
        holiday_day_type=holiday_day_type,
    )


def load_config(path: Path) -> TariffConfig:
    """Load and validate a tariff YAML file."""
    raw = yaml.safe_load(path.read_text())
    if not isinstance(raw, dict):
        raise ValueError(f"tariff config must be a mapping, got {type(raw).__name__}")

    taxes = [
        TaxEntry(
            valid_from=_parse_date(t["valid_from"]),
            iee_pct=t.get("iee_pct"),
            iva_pct=t.get("iva_pct"),
            source=t.get("source", "unknown"),
        )
        for t in raw.get("taxes", [])
    ]

    tp_raw = raw.get("tariff_periods") or {}
    if not isinstance(tp_raw, dict):
        raise ValueError(f"tariff_periods must be a mapping, got {type(tp_raw).__name__}")
    tariff_periods = _parse_tariff_periods(tp_raw)

    bat_raw = raw.get("battery", {})
    battery = BatteryConfig(
        capacity_kwh=bat_raw["capacity_kwh"],
        power_kw=bat_raw["power_kw"],
        round_trip_efficiency=bat_raw.get("round_trip_efficiency"),
        soc_window_min=bat_raw.get("soc_window_min"),
        soc_window_max=bat_raw.get("soc_window_max"),
        import_limit_kw=bat_raw.get("import_limit_kw"),
        initial_soc_frac=bat_raw.get("initial_soc_frac"),
    )

    return TariffConfig(
        version=raw.get("version", 1),
        taxes=taxes,
        tariff_periods=tariff_periods,
        battery=battery,
    )


# ---------------------------------------------------------------------------
# Period lookup
# ---------------------------------------------------------------------------


def tariff_period_for(periods: TariffPeriods, day: date, hour: int) -> int:
    """Return the configured tariff period (1/2/3) for a local day and hour.

    *day* and *hour* must already be **local wall-clock** values in
    ``periods.timezone``: passing the UTC hour instead of the converted one
    mislabels the interval, which is why the lookup takes them separately
    rather than taking a timestamp.

    Raises:
        ValueError: *hour* is outside 0..23, the applicable day type has no
            configured rules, or no rule covers *hour*.
    """
    if hour < 0 or hour > MAX_HOUR - 1:
        raise ValueError(f"hour must be a local wall-clock hour in 0..{MAX_HOUR - 1}, got {hour!r}")

    day_type = periods.day_type(day)
    rules = periods.rules_for(day_type)
    if not rules:
        raise ValueError(
            f"tariff_periods.rules.{day_type} is empty for {day.isoformat()}: tariff "
            "periods are configuration data and are never hardcoded — configure the "
            "bands in tariff.yaml"
        )

    for rule in rules:
        if rule.covers(hour):
            return rule.period
    bands = ", ".join(f"[{r.start_hour},{r.end_hour})=P{r.period}" for r in rules)
    raise ValueError(
        f"tariff_periods.rules.{day_type} does not cover local hour {hour} "
        f"on {day.isoformat()}: bands are {bands}"
    )


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def resolve_config_path(cli_arg: str | None = None) -> Path:
    """Resolve the tariff config path.

    Priority: *cli_arg* > ``ENERGY_BASELINE_CONFIG`` env > CWD-relative default.
    """
    if cli_arg:
        return Path(cli_arg).resolve()
    env = os.environ.get("ENERGY_BASELINE_CONFIG")
    if env:
        return Path(env).resolve()
    return (Path.cwd() / "config" / "tariff.yaml").resolve()


def tax_multiplier(cfg: TariffConfig, at_datetime: datetime) -> float | None:
    """Return the tax multiplier for the given datetime.

    Returns ``None`` when the applicable tax entry has unconfirmed rates
    (any ``null`` percentage).  The caller must refuse to produce billed
    costs when this returns ``None``.

    The multiplier is ``(1 + iee/100) * (1 + iva/100)``.
    """
    if isinstance(at_datetime, datetime) and at_datetime.tzinfo is not None:
        d = at_datetime.date()
    elif isinstance(at_datetime, datetime):
        d = at_datetime.date()
    else:
        d = at_datetime

    applicable: TaxEntry | None = None
    for tax in sorted(cfg.taxes, key=lambda t: t.valid_from, reverse=True):
        if tax.valid_from <= d:
            applicable = tax
            break

    if applicable is None:
        return None
    if applicable.iee_pct is None or applicable.iva_pct is None:
        return None

    return (1 + applicable.iee_pct / 100) * (1 + applicable.iva_pct / 100)


def require_battery_field(value: float | None, name: str) -> float:
    """Raise if a battery config field is unconfirmed."""
    if value is None:
        raise ValueError(
            f"battery.{name} is null (unconfirmed) — "
            f"set it in tariff.yaml before running this function"
        )
    return value
