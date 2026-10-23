"""Tariff configuration loading and validation.

Loads ``tariff.yaml`` into frozen dataclasses.  Raises loudly when a
required value is ``null`` (unconfirmed) and a function needs it.
"""

from __future__ import annotations

import os
from dataclasses import dataclass
from datetime import date, datetime
from pathlib import Path

import yaml

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
class TariffPeriods:
    """Tariff period definitions (timezone + boundaries)."""

    timezone: str


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

    tp_raw = raw.get("tariff_periods", {})
    tariff_periods = TariffPeriods(timezone=tp_raw.get("timezone", "UTC"))

    bat_raw = raw.get("battery", {})
    battery = BatteryConfig(
        capacity_kwh=bat_raw["capacity_kwh"],
        power_kw=bat_raw["power_kw"],
        round_trip_efficiency=bat_raw.get("round_trip_efficiency"),
        soc_window_min=bat_raw.get("soc_window_min"),
        soc_window_max=bat_raw.get("soc_window_max"),
        import_limit_kw=bat_raw.get("import_limit_kw"),
    )

    return TariffConfig(
        version=raw.get("version", 1),
        taxes=taxes,
        tariff_periods=tariff_periods,
        battery=battery,
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
