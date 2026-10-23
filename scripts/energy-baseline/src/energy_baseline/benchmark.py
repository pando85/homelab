"""Benchmark orchestration: a validated dataset in, an immutable benchmark run out.

This module owns no physics and no cost arithmetic.  It selects intervals, freezes
parameters, calls the numeric core (battery / oracle / policies / cost) and serialises
exactly what came back.  See ``docs/adr/0003-phase1-measurement-and-baseline.md``
§"Policy design", §"Battery physical simulator", §"Strategy comparison".

Guarantees pinned here:

* **One interval set, four policies.** The usable mask is computed once and handed to
  every policy, so gap exclusions and the objective are identical across baseline,
  causal, day-ahead and oracle.  The causal policy may lose to the baseline; reported,
  not clipped.  So may the day-ahead policy — see the ordering note below.
* **Training before evaluation.** Causal thresholds are fitted on usable intervals
  before ``train_end``, frozen, and hashed into the run id.  The day-ahead policy's
  terminal SOC valuation defaults to a statistic of that same frozen training window,
  never of evaluation prices.
* **The day-ahead forecast looks backwards only.** Day *D*'s load forecast is built
  from intervals strictly before the first interval of day *D*
  (:func:`energy_baseline.policies.forecast_load_trailing_slot_mean`), and the forecast
  never enters the cost: cost is always re-computed against the measured load.  What the
  day-ahead policy may see is today's published price curve (production evidence: the
  household's own AppDaemon apps read ``price_00h``…``price_23h`` at local midnight —
  see ``energy_baseline.policies`` module docstring) plus a causal load forecast; what it
  may not see is any price or load beyond its own local day.
* **Free terminal accounting.** The oracle is asked for ``terminal_mode="free"`` with
  ``terminal_value_eur_per_kwh=0.0``: SOC stays at or above its initial value, the
  ending SOC is free, and the objective is pure metered cash.  This is *not* a closed
  cycle and the run records that; if the core has no free mode, the fixed-cycle
  default is used and ``terminal_mode_used`` says so.  The day-ahead policy is also an
  open cycle but NOT zero-valued: a daily solve cannot see tomorrow, so with a free and
  worthless end-of-day SOC it would rationally empty the battery at midnight — the
  artefact ADR-0001 tells us to avoid — and its per-day terminal value is therefore a
  declared, frozen, provenance-recorded number.
* **Honest bounds.** An uncertified incumbent is an upper bound on optimal cost, never
  an "oracle optimum"; a missing dual bound stays unknown.  Status, gap, certification,
  configured tolerances and the time limit are all serialised.
* **Ordering is measured, not assumed.** Oracle ≤ baseline and oracle ≤ causal hold by
  construction (the oracle's feasible set contains both plans).  Oracle ≤ day-ahead also
  holds, because the day-ahead plan is a feasible point of the oracle's own problem —
  *provided* the oracle's reported figure is a certified optimum; on a time-limited
  incumbent the comparison is empirical and is labelled as such.  **Day-ahead versus
  causal is not guaranteed in either direction** and is not asserted anywhere: a myopic
  optimiser with a poor load forecast and a badly chosen terminal value can lose to a
  dumb threshold rule, and if the measurement says that, the measurement is the result.
* **Loud assumptions.** ``tariff.yaml`` nulls are never defaulted away;
  ``round_trip_efficiency`` maps to ``sqrt(RTE)`` on both legs and is labelled as a
  symmetric assumption; idle losses are zero unless measured; an unconfirmed import
  ceiling is passed as ``None`` (unbounded) and reported; the day-ahead policy assumes
  the archived price for an hour equals the price published the evening before, which
  this pipeline records as an assumption it does not verify.

Outputs, via :func:`energy_baseline.paths.write_part_atomic` (no-clobber, ``0600``,
private ``0700`` tree)::

    <dataset>/benchmarks/<run_id>/benchmark.json    full detail, all extras
    <dataset>/benchmarks/<run_id>/results.parquet   BENCHMARK_RESULT, exactly
    <dataset>/benchmarks/<run_id>/schedules.parquet per-interval plans (wide)

Bounds, SOC in kWh, the metered/inventory split, tolerances and time limits live in
``benchmark.json`` and the wide schedules file: ``BENCHMARK_RESULT`` is unchanged.  The
day-ahead policy's per-day telemetry (local day, solve status, forecast error, clipping,
degradation) is recorded the same way — in ``benchmark.json``'s ``day_ahead`` block, not
as new schema columns, and :meth:`energy_baseline.battery.Schedule.as_dict` was
deliberately not extended for it either.
"""

from __future__ import annotations

import dataclasses
import hashlib
import inspect
import io
import json
import math
from collections.abc import Iterable, Sequence
from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any

import numpy as np
import pyarrow as pa
import pyarrow.parquet as pq

from energy_baseline.config import TariffConfig, load_config, tax_multiplier
from energy_baseline.manifest import config_hash
from energy_baseline.paths import write_part_atomic
from energy_baseline.schemas import BENCHMARK_RESULT, SCHEMA_VERSION, validate_table

#: Version of this benchmark contract (distinct from the store schema version).
BENCHMARK_VERSION = 1

#: Directory name under a dataset that holds its benchmark runs.
BENCHMARKS_DIRNAME = "benchmarks"

RESULTS_JSON_NAME = "benchmark.json"
RESULTS_PARQUET_NAME = "results.parquet"
SCHEDULES_PARQUET_NAME = "schedules.parquet"

POLICY_NO_BATTERY = "no_battery"
POLICY_CAUSAL_PRICE = "causal_price"
POLICY_DAY_AHEAD = "day_ahead"
POLICY_ORACLE = "oracle"
#: The information ladder, in ascending order of what each policy is allowed to know:
#: measured history only → current price and past state → today's published prices plus
#: a causal load forecast → perfect foresight of both.  ``all`` runs this whole set.
POLICY_NAMES: tuple[str, ...] = (
    POLICY_NO_BATTERY,
    POLICY_CAUSAL_PRICE,
    POLICY_DAY_AHEAD,
    POLICY_ORACLE,
)

# Interval usability vocabulary.
#
# Allocation: ``exact`` and ``estimated_boundary`` are benchmarkable by default.
# ``estimated_boundary`` is a bounded, telescoping-preserving approximation: the
# known counter delta is split proportionally at the 15-minute boundary, the sum
# of the pieces equals the original delta exactly, and the error is bounded by
# one sample interval.  ``unknown_gap`` and ``gap_total_unknown`` are NEVER
# benchmarkable, with or without any flag.
#
# Energy: ``measured`` and ``counter_reset_bridged`` are benchmarkable by default
# (the latter carries no energy value but is a known, disclosed reset);
# ``gap_partial`` is a partially gapped total and requires the flag.
#
# Price: ``measured`` is benchmarkable by default.  ``forward_filled`` is a stale
# value carried forward and requires the flag.  ``missing`` and ``ambiguous``
# are NEVER benchmarkable, with or without any flag.
MEASURED_ENERGY_QUALITY = frozenset({"measured", "counter_reset_bridged"})
USABLE_ALLOCATION_QUALITY = frozenset({"exact", "estimated_boundary"})
MEASURED_PRICE_QUALITY = frozenset({"measured"})
RECONSTRUCTED_ENERGY_QUALITY = frozenset({"gap_partial"})
RECONSTRUCTED_PRICE_QUALITY = frozenset({"forward_filled"})
NEVER_ALLOCABLE = frozenset({"unknown_gap", "gap_total_unknown"})
NEVER_PRICE = frozenset({"missing", "ambiguous"})

DEFAULT_TIME_LIMIT_S = 120.0
DEFAULT_MIP_REL_GAP = 1e-6
DEFAULT_MIP_ABS_GAP = 1e-9
DEFAULT_TRAIN_DAYS = 30
DEFAULT_CHARGE_PCT = 25.0
DEFAULT_DISCHARGE_PCT = 75.0

#: Per-day wall-clock limit for the rolling day-ahead MILP, in seconds **per local day**
#: (the oracle's ``DEFAULT_TIME_LIMIT_S`` is per whole horizon; the two are not the same
#: knob and the run records which one a number came from).  One local day is at most 100
#: intervals, so 5 s is generous by orders of magnitude; the count of days that actually
#: consumed it is reported, because a day that consumed its limit makes the run's plan
#: depend on machine speed.
DEFAULT_DAY_AHEAD_TIME_LIMIT_S = 5.0

#: Trailing window, in local days, for the day-ahead load forecast.  ``policies.py``
#: carries its own default for direct callers; a run always passes this contract value
#: explicitly and hashes it into the run id, so the two can never disagree in an
#: artifact.  Wider windows average over more days (and across more seasonality);
#: narrower ones track the household faster but see fewer samples per slot.
DEFAULT_FORECAST_WINDOW_DAYS = 28

#: The rule the day-ahead terminal SOC valuation falls back to when the caller does not
#: supply a number.  Named here so an artifact can never report a bare figure and leave
#: the reader to guess whether it was hand-picked or measured.
TERMINAL_VALUE_DEFAULT_RULE = "training_window_mean_price"

_EPS = 1e-9


class BenchmarkError(RuntimeError):
    """The benchmark could not be completed (core unavailable, no cost rows)."""


class BenchmarkInputError(ValueError):
    """The caller's inputs are unusable (bad split, unconfirmed config, thin mask)."""


# ---------------------------------------------------------------------------
# Numeric core — imported lazily so this module loads while the core is a stub
# ---------------------------------------------------------------------------

_CORE_NAMES = (
    ("battery", "BatterySpec"),
    ("battery", "Horizon"),
    ("policies", "policy_no_battery"),
    ("policies", "policy_causal_price"),
    ("policies", "policy_day_ahead"),
    ("policies", "fit_thresholds"),
    ("policies", "contiguous_day_bounds"),
    ("policies", "forecast_load_trailing_slot_mean"),
    ("policies", "DayAheadDay"),
    ("policies", "FORECAST_METHOD"),
    ("policies", "FORECAST_FALLBACK_RULE"),
    ("oracle", "solve_oracle"),
    ("cost", "compute_interval_cost"),
)
_CORE_ERRORS = (
    ("oracle", "OracleError"),
    ("oracle", "OracleInfeasibleError"),
    ("oracle", "OracleNoIncumbentError"),
    ("battery", "BatteryPhysicsError"),
)


@dataclass(frozen=True)
class _Core:
    battery_spec: Any
    horizon: Any
    policy_no_battery: Any
    policy_causal_price: Any
    policy_day_ahead: Any
    fit_thresholds: Any
    contiguous_day_bounds: Any
    forecast_load: Any
    forecast_method: Any
    forecast_fallback_rule: Any
    day_ahead_day: Any
    solve_oracle: Any
    interval_cost: Any


def _modules() -> dict[str, Any]:
    from energy_baseline import battery, cost, oracle, policies

    return {"battery": battery, "cost": cost, "oracle": oracle, "policies": policies}


def core_or_raise() -> _Core:
    """Return the core callables the benchmark needs, or raise :class:`BenchmarkError`."""
    modules = _modules()
    missing = [
        f"{module}.{attr}"
        for module, attr in _CORE_NAMES
        if not hasattr(modules.get(module) or object(), attr)
    ]
    if missing:
        raise BenchmarkError(
            "the numeric core is not implemented yet — missing: " + ", ".join(sorted(missing))
        )
    return _Core(
        battery_spec=modules["battery"].BatterySpec,
        horizon=modules["battery"].Horizon,
        policy_no_battery=modules["policies"].policy_no_battery,
        policy_causal_price=modules["policies"].policy_causal_price,
        policy_day_ahead=modules["policies"].policy_day_ahead,
        fit_thresholds=modules["policies"].fit_thresholds,
        contiguous_day_bounds=modules["policies"].contiguous_day_bounds,
        forecast_load=modules["policies"].forecast_load_trailing_slot_mean,
        forecast_method=modules["policies"].FORECAST_METHOD,
        forecast_fallback_rule=modules["policies"].FORECAST_FALLBACK_RULE,
        day_ahead_day=modules["policies"].DayAheadDay,
        solve_oracle=modules["oracle"].solve_oracle,
        interval_cost=modules["cost"].compute_interval_cost,
    )


def core_available() -> bool:
    """True when the core exposes everything the benchmark calls."""
    try:
        core_or_raise()
    except BenchmarkError:
        return False
    return True


def _core_exceptions() -> tuple[type[BaseException], ...]:
    """The core's own error classes, when it defines them."""
    try:
        modules = _modules()
    except ImportError:  # pragma: no cover
        return ()
    found: list[type[BaseException]] = []
    for module_name, attr in _CORE_ERRORS:
        obj = getattr(modules.get(module_name) or object(), attr, None)
        if isinstance(obj, type) and issubclass(obj, BaseException) and obj not in found:
            found.append(obj)
    return tuple(found)


def _failure_kind(exc: BaseException) -> str:
    text = f"{type(exc).__name__} {exc}".lower()
    if "infeasible" in text:
        return "infeasible"
    if "time limit" in text or "noincumbent" in text or "no incumbent" in text:
        return "no_incumbent_within_time_limit"
    return "solver_error"


# ---------------------------------------------------------------------------
# Dataset loading — manifest-driven, never a bare parquet glob
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class LoadedDataset:
    """A validated analysis dataset plus the manifest that pins it."""

    dataset_id: str
    directory: Path
    table: pa.Table
    manifest: dict[str, Any]
    manifest_sha256: str
    interval_seconds: float
    part_files: list[dict[str, Any]] = field(default_factory=list)
    unlisted_parts: list[str] = field(default_factory=list)

    @property
    def start(self) -> datetime:
        return self.table.column("interval_start")[0].as_py()

    @property
    def end(self) -> datetime:
        return self.table.column("interval_end")[-1].as_py()


def dataset_dir_for(
    data_dir: Path | str, dataset_id: str, *, schema_version: int = SCHEMA_VERSION
) -> Path:
    """Where ``build`` writes a dataset: ``analysis/v<schema>/<dataset_id>``."""
    return Path(data_dir) / "analysis" / f"v{schema_version}" / dataset_id


def list_datasets(data_dir: Path | str, *, schema_version: int = SCHEMA_VERSION) -> list[Path]:
    """Every manifest-bearing dataset directory under *data_dir*, sorted."""
    root = Path(data_dir) / "analysis" / f"v{schema_version}"
    if not root.is_dir():
        return []
    return sorted(c for c in root.iterdir() if (c / "build_manifest.json").is_file())


def _resolve_part(dataset_dir: Path, data_dir: Path | None, entry: dict[str, Any]) -> Path:
    """Locate a part the manifest names; the manifest decides *which* files, here."""
    raw = entry.get("path") or entry.get("name")
    if not raw:
        raise BenchmarkInputError(f"manifest part entry has no path: {entry!r}")
    candidate = Path(str(raw))
    bases = [dataset_dir] + ([data_dir] if data_dir is not None else [])
    tried = [candidate if candidate.is_absolute() else base / candidate for base in bases]
    tried.append(dataset_dir / "parts" / candidate.name)
    for path in tried:
        if path.is_file():
            return path
    raise BenchmarkInputError(
        f"dataset {dataset_dir.name}: the manifest lists part {str(raw)!r} but it was not "
        f"found (looked in: {', '.join(str(p) for p in tried)})"
    )


def load_dataset(dataset: Path | str, *, data_dir: Path | str | None = None) -> LoadedDataset:
    """Load and verify a built dataset through its manifest.

    The files, their SHA-256 digests and their row counts all come from
    ``build_manifest.json`` and are re-verified before anything is read.  A tampered
    part, a foreign ``dataset_id``, a schema violation or a non-uniform grid aborts
    the load — benchmarking an artifact that is not what was built is worse than
    refusing to run.
    """
    directory = Path(dataset).expanduser()
    if directory.is_file():
        directory = directory.parent
    if not directory.is_dir():
        raise BenchmarkInputError(f"dataset directory does not exist: {directory}")
    manifest_path = directory / "build_manifest.json"
    if not manifest_path.is_file():
        raise BenchmarkInputError(
            f"no build_manifest.json at {manifest_path} — benchmarks run on a built dataset "
            "(see the `build` command); loose parquet is not accepted, because an unverified "
            "file pins no inputs, config or code version"
        )
    payload = manifest_path.read_bytes()
    manifest = json.loads(payload.decode("utf-8"))
    entries = manifest.get("part_files") or []
    if not entries:
        raise BenchmarkInputError(f"{manifest_path}: part_files is empty — nothing to load")

    base = Path(data_dir).expanduser() if data_dir is not None else None
    tables: list[pa.Table] = []
    resolved: list[dict[str, Any]] = []
    for entry in entries:
        path = _resolve_part(directory, base, entry)
        digest = hashlib.sha256(path.read_bytes()).hexdigest()
        declared = entry.get("sha256")
        if declared and declared != digest:
            raise BenchmarkInputError(
                f"input hash failure for dataset {directory.name}: {path.name} is "
                f"sha256={digest[:12]} but the manifest pins {str(declared)[:12]} — this is not "
                "the artifact that was built, so it will not be benchmarked"
            )
        table = pq.read_table(path)
        rows = entry.get("rows")
        if rows is not None and table.num_rows != int(rows):
            raise BenchmarkInputError(
                f"input hash failure for dataset {directory.name}: {path.name} has "
                f"{table.num_rows} rows, the manifest says {rows}"
            )
        try:
            validate_table("ANALYSIS_INTERVAL", table)
        except (TypeError, ValueError) as exc:
            raise BenchmarkInputError(f"dataset {directory.name}: {path.name}: {exc}") from exc
        tables.append(table)
        resolved.append(
            {**entry, "resolved_path": str(path), "verified_sha256": digest, "rows": table.num_rows}
        )

    combined = pa.concat_tables(tables).sort_by("interval_start")
    stamped = {v for v in combined.column("dataset_id").to_pylist() if v is not None}
    if not stamped:
        raise BenchmarkInputError(f"dataset {directory.name}: dataset_id column is all null")
    if len(stamped) > 1:
        raise BenchmarkInputError(
            f"dataset {directory.name}: several dataset_id values in one dataset: {sorted(stamped)}"
        )
    dataset_id = stamped.pop()
    if manifest.get("dataset_id") and manifest["dataset_id"] != dataset_id:
        raise BenchmarkInputError(
            f"dataset {directory.name}: manifest dataset_id {manifest['dataset_id']!r} does not "
            f"match the rows' {dataset_id!r}"
        )
    if directory.name != dataset_id:
        raise BenchmarkInputError(
            f"dataset directory {directory.name!r} does not match its dataset_id {dataset_id!r}"
        )

    starts = combined.column("interval_start").to_pylist()
    ends = combined.column("interval_end").to_pylist()
    if len(set(starts)) != len(starts):
        raise BenchmarkInputError(f"dataset {dataset_id}: duplicate interval_start values")
    durations = {(e - s).total_seconds() for s, e in zip(starts, ends)}
    if len(durations) != 1:
        raise BenchmarkInputError(
            f"dataset {dataset_id}: interval duration is not uniform: {sorted(durations)}"
        )
    interval_seconds = float(durations.pop())
    if interval_seconds <= 0:
        raise BenchmarkInputError(f"dataset {dataset_id}: non-positive interval duration")

    listed = {Path(str(e.get("path") or e.get("name") or "")).name for e in entries}
    unlisted = sorted(
        p.name for p in (directory / "parts").glob("*.parquet") if p.name not in listed
    )
    return LoadedDataset(
        dataset_id=dataset_id,
        directory=directory,
        table=combined,
        manifest=manifest,
        manifest_sha256=hashlib.sha256(payload).hexdigest(),
        interval_seconds=interval_seconds,
        part_files=resolved,
        unlisted_parts=unlisted,
    )


# ---------------------------------------------------------------------------
# Interval usability
# ---------------------------------------------------------------------------


def _reject(
    energy: float | None,
    price: float | None,
    eq: str | None,
    aq: str | None,
    pq_: str | None,
    *,
    allow_reconstructed: bool,
) -> str | None:
    """Return the reason a row is unusable, or ``None`` when it is usable."""
    if aq in NEVER_ALLOCABLE:
        return f"allocation_quality={aq}"
    if energy is None:
        return "gross_kwh=null"
    if not math.isfinite(energy):
        return "gross_kwh=not_finite"
    if energy < 0.0:
        return "gross_kwh=negative"
    if aq not in USABLE_ALLOCATION_QUALITY:
        return f"allocation_quality={aq}"
    if eq not in MEASURED_ENERGY_QUALITY and not (
        allow_reconstructed and eq in RECONSTRUCTED_ENERGY_QUALITY
    ):
        return f"energy_quality={eq}"
    if price is None:
        return "price=null"
    if not math.isfinite(price):
        return "price=not_finite"
    if pq_ in NEVER_PRICE:
        return f"price_quality={pq_}"
    if pq_ not in MEASURED_PRICE_QUALITY and not (
        allow_reconstructed and pq_ in RECONSTRUCTED_PRICE_QUALITY
    ):
        return f"price_quality={pq_}"
    return None


def usable_mask(
    table: pa.Table, *, allow_reconstructed: bool = False
) -> tuple[np.ndarray, list[str]]:
    """Per-interval ``usable`` flag plus a reason string for every row.

    Usable means: gross energy present, finite and non-negative; price present
    and finite; and the qualities all in their default-usable sets.  Allocation
    ``estimated_boundary`` is admitted by default (it is a bounded,
    telescoping-preserving approximation, not a degraded fallback).  The flag
    admits *reconstructed values*: forward-filled prices and partially gapped
    energy totals.  ``unknown_gap`` and ``gap_total_unknown`` allocation, and
    ``missing`` / ``ambiguous`` prices, are never admitted, with or without the
    flag.
    """
    gross = table.column("gross_kwh").to_pylist()
    prices = table.column("price_eur_per_kwh").to_pylist()
    energy_q = table.column("energy_quality").to_pylist()
    alloc_q = table.column("allocation_quality").to_pylist()
    price_q = table.column("price_quality").to_pylist()

    reasons: list[str] = []
    for i in range(len(gross)):
        reason = _reject(
            gross[i],
            prices[i],
            energy_q[i],
            alloc_q[i],
            price_q[i],
            allow_reconstructed=allow_reconstructed,
        )
        reasons.append(reason or "usable")
    usable = np.array([r == "usable" for r in reasons], dtype=bool)
    return usable, reasons


# ---------------------------------------------------------------------------
# Battery assumptions
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class BatteryAssumptions:
    """Resolved battery parameters and where each one came from."""

    capacity_kwh: float
    power_kw: float
    charge_efficiency: float
    discharge_efficiency: float
    soc_min_frac: float
    soc_max_frac: float
    initial_soc_frac: float
    import_limit_kw: float | None
    efficiency_mapping: str
    initial_soc_source: str
    import_limit_source: str
    idle_loss_kwh_per_hour: float
    idle_losses_measured: bool

    def as_dict(self) -> dict[str, Any]:
        return dataclasses.asdict(self)

    def spec_kwargs(self) -> dict[str, Any]:
        """``battery.BatterySpec`` keywords.  ``terminal_soc_frac`` stays None:
        the terminal condition is the solver's terminal-mode question, not this."""
        kwargs: dict[str, Any] = {
            "capacity_kwh": self.capacity_kwh,
            "power_kw": self.power_kw,
            "charge_efficiency": self.charge_efficiency,
            "discharge_efficiency": self.discharge_efficiency,
            "soc_min_frac": self.soc_min_frac,
            "soc_max_frac": self.soc_max_frac,
            "initial_soc_frac": self.initial_soc_frac,
            "terminal_soc_frac": None,
        }
        if self.idle_losses_measured:
            kwargs["idle_loss_kwh_per_hour"] = self.idle_loss_kwh_per_hour
        return kwargs


def _require(value: Any, name: str) -> float:
    if value is None:
        raise BenchmarkInputError(
            f"{name} is null (unconfirmed) in tariff.yaml — set it, or pass it as an explicit "
            "argument. The benchmark does not substitute defaults for unmeasured hardware values"
        )
    number = float(value)
    if not math.isfinite(number):
        raise BenchmarkInputError(f"{name} must be finite, got {value!r}")
    return number


def resolve_battery_assumptions(
    config: TariffConfig,
    *,
    initial_soc_frac: float | None = None,
    import_limit_kw: float | None = None,
) -> BatteryAssumptions:
    """Map ``tariff.yaml`` onto the core's per-direction battery spec.

    Unconfirmed fields raise rather than being defaulted.  A configured
    ``round_trip_efficiency`` becomes ``sqrt(RTE)`` on **both** legs — a labelled
    symmetric assumption — unless the config already carries per-direction values.
    """
    battery = config.battery
    capacity = _require(getattr(battery, "capacity_kwh", None), "battery.capacity_kwh")
    power = _require(getattr(battery, "power_kw", None), "battery.power_kw")

    charge_eff = getattr(battery, "charge_efficiency", None)
    discharge_eff = getattr(battery, "discharge_efficiency", None)
    if charge_eff is not None and discharge_eff is not None:
        eta_c = _require(charge_eff, "battery.charge_efficiency")
        eta_d = _require(discharge_eff, "battery.discharge_efficiency")
        if not (0.0 < eta_c <= 1.0 and 0.0 < eta_d <= 1.0):
            raise BenchmarkInputError(
                f"per-direction efficiencies must be in (0, 1], got {eta_c}/{eta_d}"
            )
        mapping = "config: per-direction efficiencies as configured (no symmetric assumption)"
    else:
        rte = _require(
            getattr(battery, "round_trip_efficiency", None), "battery.round_trip_efficiency"
        )
        if not 0.0 < rte <= 1.0:
            raise BenchmarkInputError(f"battery.round_trip_efficiency must be in (0, 1], got {rte}")
        eta_c = eta_d = math.sqrt(rte)
        mapping = (
            f"symmetric assumption: charge = discharge efficiency = sqrt(RTE) = {eta_c:.6f} "
            f"(round trip {rte:.6f}). Real chargers are not symmetric and RTE is unmeasured: "
            "a provisional assumption, not a battery property"
        )

    soc_min = _require(getattr(battery, "soc_window_min", None), "battery.soc_window_min")
    soc_max = _require(getattr(battery, "soc_window_max", None), "battery.soc_window_max")
    if not 0.0 <= soc_min < soc_max <= 1.0:
        raise BenchmarkInputError(
            f"battery SOC window must satisfy 0 <= min < max <= 1, got {soc_min}/{soc_max}"
        )

    configured_initial = getattr(battery, "initial_soc_frac", None)
    if initial_soc_frac is not None:
        initial = float(initial_soc_frac)
        if not soc_min <= initial <= soc_max:
            raise BenchmarkInputError(
                f"initial_soc_frac={initial} is outside the configured SOC window "
                f"[{soc_min}, {soc_max}]"
            )
        initial_source = "argument: initial_soc_frac supplied by the caller"
    elif configured_initial is not None:
        initial = _require(configured_initial, "battery.initial_soc_frac")
        if not soc_min <= initial <= soc_max:
            raise BenchmarkInputError(
                f"battery.initial_soc_frac={initial} is outside the configured SOC window "
                f"[{soc_min}, {soc_max}]"
            )
        initial_source = "config: battery.initial_soc_frac"
    else:
        initial = soc_min
        initial_source = (
            "default: initial SOC = soc_window_min. Nothing was measured or configured, so the "
            "horizon starts at the bottom of the usable window — the least favourable honest "
            "start, because the battery cannot spend energy it never had"
        )

    configured_import = getattr(battery, "import_limit_kw", None)
    if import_limit_kw is not None:
        limit: float | None = float(import_limit_kw)
        limit_source = "argument: import_limit_kw supplied by the caller"
    elif configured_import is not None:
        limit = _require(configured_import, "battery.import_limit_kw")
        limit_source = "config: battery.import_limit_kw"
    else:
        limit = None
        limit_source = (
            "unconfirmed: battery.import_limit_kw is null, so NO site import ceiling applies. "
            "This horizon is unconstrained at the connection point and may overstate what the "
            "site could actually draw"
        )

    idle = getattr(battery, "idle_loss_kwh_per_hour", None)
    return BatteryAssumptions(
        capacity_kwh=capacity,
        power_kw=power,
        charge_efficiency=eta_c,
        discharge_efficiency=eta_d,
        soc_min_frac=soc_min,
        soc_max_frac=soc_max,
        initial_soc_frac=initial,
        import_limit_kw=limit,
        efficiency_mapping=mapping,
        initial_soc_source=initial_source,
        import_limit_source=limit_source,
        idle_loss_kwh_per_hour=float(idle) if idle is not None else 0.0,
        idle_losses_measured=idle is not None,
    )


# ---------------------------------------------------------------------------
# Splitting, tax basis, small helpers
# ---------------------------------------------------------------------------


def parse_train_end(value: datetime | str | None) -> datetime:
    """Normalise ``train_end`` to an aware UTC datetime.

    ``None`` is rejected: the train/eval cut must be declared by the caller, and a
    future cut is rejected because training data cannot be something that has not
    happened yet.
    """
    if value is None:
        raise BenchmarkInputError(
            "train_end is required: thresholds are fitted on an earlier split and frozen before "
            "evaluation (ADR-0003 §policy design)"
        )
    if isinstance(value, str):
        text = value.strip().replace(" ", "T")
        if text.endswith("Z"):
            text = text[:-1] + "+00:00"
        try:
            moment = datetime.fromisoformat(text)
        except ValueError as exc:
            raise BenchmarkInputError(f"train_end is not an ISO 8601 timestamp: {value!r}") from exc
    elif isinstance(value, datetime):
        moment = value
    else:
        raise BenchmarkInputError(
            f"train_end must be a datetime or an ISO 8601 string, got {type(value).__name__}"
        )
    if moment.tzinfo is None:
        raise BenchmarkInputError(
            f"train_end={value!r} is naive — the train/eval cut is an absolute instant, so an "
            "aware timestamp is required"
        )
    moment = moment.astimezone(UTC)
    if moment > datetime.now(UTC):
        raise BenchmarkInputError(
            f"train_end={_iso(moment)} is in the future — training data cannot be something "
            "that has not happened yet"
        )
    return moment


def _iso(moment: datetime) -> str:
    return moment.astimezone(UTC).isoformat().replace("+00:00", "Z")


def _canonical_sha(payload: Any) -> str:
    text = json.dumps(payload, sort_keys=True, separators=(",", ":"), default=str)
    return hashlib.sha256(text.encode("utf-8")).hexdigest()


def code_version() -> str:
    """Code identity for lineage: the shared definition from ``manifest``.

    Delegates to :func:`energy_baseline.manifest.code_version` so a dirty tree
    produces a different ``dataset_id`` from a clean one.
    """
    from energy_baseline.manifest import code_version as _manifest_code_version

    return _manifest_code_version()


def _float_of(value: Any) -> float | None:
    if value is None:
        return None
    try:
        out = float(value)
    except (TypeError, ValueError):
        return None
    return out if math.isfinite(out) else None


def _first_float(obj: Any, names: Sequence[str]) -> float | None:
    """First non-null float among *names* on *obj* — tolerant of core field naming."""
    for name in names:
        value = _float_of(getattr(obj, name, None))
        if value is not None:
            return value
    return None


def _array(sched: Any, name: str, length: int) -> np.ndarray:
    value = getattr(sched, name, None)
    if value is None:
        return np.full(length, np.nan)
    arr = np.asarray(value, dtype=float)
    if arr.size != length:  # pragma: no cover - core contract violation
        raise BenchmarkError(f"schedule field {name!r} has {arr.size} entries, expected {length}")
    return arr


def _nullable(values: Iterable[float]) -> list[float | None]:
    return [None if not math.isfinite(value) else float(value) for value in values]


def tax_basis(config: TariffConfig, stamps: Sequence[datetime]) -> dict[str, Any]:
    """Describe the tax situation across the evaluation horizon.

    The solver's objective is pretax.  One constant multiplier across the whole
    window is a uniform scaling, which preserves the argmin, so the run may also be
    reported taxed.  Several rates, or a null rate, is not a uniform scaling: then
    only the untaxed basis is claimed and taxed figures are reconciliation.
    """
    factors = [tax_multiplier(config, stamp) for stamp in stamps]
    if not factors or any(f is None for f in factors):
        return {
            "cost_basis": "untaxed",
            "uniform_multiplier": None,
            "distinct_multipliers": sorted({round(f, 12) for f in factors if f is not None}),
            "has_unconfirmed_rate": any(f is None for f in factors),
            "note": (
                "tax rates are unconfirmed (null) for at least one interval, so NO billed figure "
                "is claimed: every cost here is untaxed energy cost. Fill taxes from a verified "
                "invoice to get a taxed basis"
            ),
        }
    distinct = sorted({round(f, 12) for f in factors})
    if len(distinct) == 1:
        return {
            "cost_basis": "taxed",
            "uniform_multiplier": float(factors[0]),
            "distinct_multipliers": distinct,
            "has_unconfirmed_rate": False,
            "note": (
                f"one confirmed tax multiplier ({factors[0]:.6f}) applies across the whole "
                "horizon, so taxed cost is a uniform scaling of the pretax objective and the "
                "optimal schedule is unchanged by it"
            ),
        }
    return {
        "cost_basis": "untaxed",
        "uniform_multiplier": None,
        "distinct_multipliers": distinct,
        "has_unconfirmed_rate": False,
        "note": (
            f"several tax rates apply inside the horizon ({distinct}) — not a uniform scaling, "
            "so optimisation and all cross-policy comparison stay UNTAXED and taxed figures are "
            "month-by-month reconciliation only"
        ),
    }


def _parse_policies(policies: Any) -> list[str]:
    if policies is None:
        names = list(POLICY_NAMES)
    elif isinstance(policies, str):
        text = policies.strip()
        names = (
            list(POLICY_NAMES)
            if text in ("", "all", "*")
            else [p.strip() for p in text.split(",") if p.strip()]
        )
    else:
        names = [str(p).strip() for p in policies if str(p).strip()]
    unknown = [n for n in names if n not in POLICY_NAMES]
    if unknown:
        raise BenchmarkInputError(
            f"unknown policies {unknown} — this phase has exactly {list(POLICY_NAMES)} "
            "(ADR-0003 §strategy comparison)"
        )
    if not names:
        raise BenchmarkInputError("no policies selected")
    return list(dict.fromkeys(names))


def _local_month(stamp: datetime, day: Any) -> str:
    """``YYYY-MM`` on the local calendar grid.

    ``local_day`` from the dataset is authoritative: bucketing by local day keeps a
    23- or 25-hour DST transition day inside one month instead of leaking an hour
    across a UTC boundary.
    """
    if day is not None:
        return f"{day.year:04d}-{day.month:02d}"
    return f"{stamp.year:04d}-{stamp.month:02d}"


def _count_reasons(reasons: Iterable[str]) -> dict[str, int]:
    counts: dict[str, int] = {}
    for reason in reasons:
        counts[reason] = counts.get(reason, 0) + 1
    return dict(sorted(counts.items(), key=lambda kv: (-kv[1], kv[0])))


# ---------------------------------------------------------------------------
# Running the policies
# ---------------------------------------------------------------------------


def resolve_day_ahead_terminal_value(
    *,
    configured: float | None,
    train_prices: np.ndarray,
    train_window_start: datetime,
    train_end: datetime,
) -> tuple[float, str]:
    """Pick the day-ahead policy's EUR/DC-kWh value on end-of-day SOC, and say where it came from.

    A rolling daily solve is **myopic by construction**: it cannot see tomorrow, so a
    worthless end-of-day SOC makes "empty the battery before midnight" the rational
    answer, which is exactly the artefact ADR-0001 rules out ("preserve a terminal SOC
    value or equivalent terminal penalty instead of artificially emptying the battery at
    midnight").  That value is therefore the policy's only cross-day coupling and it has
    to be defensible rather than convenient, hence three rules enforced here:

    * the default is a **statistic of the training window** — the same strictly-earlier
      split the causal thresholds are frozen from — never a hand-picked number;
    * no evaluation-period price can enter it, because only ``train_prices`` is read;
    * an explicit override is legal but is recorded with provenance ``cli``, so a run
      whose terminal value was chosen by hand can be told apart from one where it was
      measured, and the two are never the same run id.

    Returns:
        ``(value_eur_per_kwh, provenance)`` with provenance starting ``cli:`` or
        ``default:<rule>:``.

    Raises:
        BenchmarkInputError: If the override is not finite, or the default is asked for
            from an empty or non-finite training window (a statistic of nothing is a
            hand-picked number wearing a costume).
    """
    if configured is not None:
        value = float(configured)
        if not math.isfinite(value):
            raise BenchmarkInputError(
                f"day_ahead_terminal_value_eur_per_kwh must be finite, got {configured!r}"
            )
        return value, (
            f"cli: {value} EUR/DC-kWh supplied explicitly by the caller. Overrides are "
            "allowed but never silent: this run's cross-day coupling is the operator's "
            "number, not a measured statistic, and it is hashed into the run id"
        )

    prices = np.asarray(train_prices, dtype=float).reshape(-1)
    if prices.size == 0:
        raise BenchmarkInputError(
            "the day-ahead terminal SOC value defaults to the mean price of the training "
            f"window, but {_iso(train_window_start)} .. {_iso(train_end)} holds no usable "
            "prices; pass day_ahead_terminal_value_eur_per_kwh explicitly or widen the "
            "training split"
        )
    if not np.isfinite(prices).all():
        bad = int(np.argmax(~np.isfinite(prices)))
        raise BenchmarkInputError(f"training prices contain a non-finite value at index {bad}")
    value = float(prices.mean())
    return value, (
        f"default:{TERMINAL_VALUE_DEFAULT_RULE}: mean of the {prices.size} usable training "
        f"prices over {_iso(train_window_start)} .. {_iso(train_end)} = {value:.9f} "
        "EUR/DC-kWh. Frozen before evaluation and taken only from the split the causal "
        "thresholds are frozen from, so no evaluation price enters it. A positive value is "
        "the point: it is what stops a daily solve from dumping stored energy at midnight "
        "for nothing. Pass 0.0 for the documented alternative (a pure cash-flow objective, "
        "battery emptied by design)"
    )


def _day_label(value: Any) -> str:
    """ISO text for a dataset ``local_day`` (a ``date``, a ``datetime`` or a string)."""
    if value is None:
        raise BenchmarkInputError(
            "the dataset row carries no local_day: the day-ahead partition is defined on "
            "local calendar days, so an unlabeled interval cannot be assigned to a solve"
        )
    isoformat = getattr(value, "isoformat", None)
    return str(isoformat()) if callable(isoformat) else str(value)


def _forecast_summary(per_day: Sequence[dict[str, Any]]) -> dict[str, Any]:
    """Interval-weighted forecast error over the days a run actually planned for.

    Error is computed against the archive **after** the fact.  No decision used it:
    the policy's only forward-looking input is the forecast itself, so a run with a
    terrible forecast still plans as if it did not know it was terrible, and this
    summary exists to make that visible to the reader afterwards.
    """
    scored = [d for d in per_day if d.get("usable_intervals") and d.get("mae_kwh") is not None]
    weights = np.array([float(d["usable_intervals"]) for d in scored], dtype=float)
    out: dict[str, Any] = {
        "days_scored": len(scored),
        "days_total": len(per_day),
        "days_with_pooled_fallback_slots": sum(
            1 for d in per_day if int(d.get("slots_pooled_fallback") or 0) > 0
        ),
        "days_with_zero_history_slots": sum(
            1 for d in per_day if int(d.get("slots_zero_no_history") or 0) > 0
        ),
        "slots_missing_prior_history": sum(
            int(d.get("slots_pooled_fallback") or 0) + int(d.get("slots_zero_no_history") or 0)
            for d in per_day
        ),
        "error_note": (
            "per-interval kWh error of the causal forecast against the measured load, "
            "averaged over the usable intervals of each evaluated local day and then "
            "interval-weighted across days; computed retrospectively for reporting, never "
            "read by the policy"
        ),
    }
    if not scored:
        out.update({"mae_kwh": None, "rmse_kwh": None, "bias_kwh": None, "worst_day": None})
        return out
    mae = np.array([float(d["mae_kwh"]) for d in scored])
    rmse = np.array(
        [float(d["rmse_kwh"] if d.get("rmse_kwh") is not None else 0.0) for d in scored]
    )
    bias = np.array(
        [float(d["bias_kwh"] if d.get("bias_kwh") is not None else 0.0) for d in scored]
    )
    total = float(weights.sum())
    out["mae_kwh"] = float(np.sum(mae * weights) / total)
    out["rmse_kwh"] = float(np.sum(rmse * weights) / total)
    out["bias_kwh"] = float(np.sum(bias * weights) / total)
    worst = int(np.argmax(mae))
    out["worst_day"] = {
        "local_day": scored[worst]["local_day"],
        "mae_kwh": float(mae[worst]),
        "bias_kwh": float(bias[worst]),
    }
    out["bias_sign_note"] = (
        "bias = forecast minus measured: negative means the forecast UNDER-predicted "
        "load, which is the direction that costs this policy twice — it plans discharge "
        "it cannot deliver (clipped against the measured no-export rule) and it assumes "
        "import headroom the load has already eaten"
    )
    return out


def prepare_day_ahead(
    *,
    core: _Core,
    load_kwh: np.ndarray,
    usable: np.ndarray,
    local_days: Sequence[Any],
    eval_index: np.ndarray,
    forecast_window_days: int,
    terminal_value_eur_per_kwh: float,
    terminal_value_source: str,
    time_limit_s: float,
    timezone: str,
) -> tuple[list[Any], dict[str, Any]]:
    """Partition the evaluation window into local days and build its causal forecast.

    The partition is the dataset's own ``local_day`` column (the tariff timezone, not
    UTC), so a spring-forward day is one 92-interval solve and a fall-back day one
    100-interval solve, and the price curve handed to a solve is the curve of the local
    day it dispatches.  The forecast is deliberately computed over the **whole**
    dataset, not the evaluation slice: history that exists before ``train_end`` is
    strictly-past measured load, which is exactly what a controller standing at local
    midnight of day *D* would have, and cutting it off at the train/eval line would
    make the first evaluation days forecast-free for a reason that has nothing to do
    with causality.

    Returns:
        ``(days, block)`` — ``days`` are the per-local-day inputs for
        :func:`energy_baseline.policies.policy_day_ahead` in horizon coordinates, and
        ``block`` is the artifact record: partition, forecast method plus error summary,
        terminal value plus provenance, and the information-set disclosure.
    """
    if forecast_window_days < 1:
        raise BenchmarkInputError(
            f"day_ahead_forecast_window_days must be >= 1, got {forecast_window_days}"
        )
    full_days = core.contiguous_day_bounds(list(local_days))
    forecast_values, forecast_diag = core.forecast_load(
        load_kwh, full_days, window_days=int(forecast_window_days), usable=usable
    )
    diag_by_label = {str(d["local_day"]): d for d in forecast_diag}
    forecast_eval = forecast_values[np.asarray(eval_index, dtype=int)]
    eval_runs = core.contiguous_day_bounds([local_days[i] for i in eval_index])

    days: list[Any] = []
    eval_diag: list[dict[str, Any]] = []
    for label, lo, hi in eval_runs:
        text = _day_label(label)
        record = diag_by_label.get(text, {})
        eval_diag.append(record)
        days.append(
            core.day_ahead_day(
                label=text,
                start=int(lo),
                stop=int(hi),
                forecast_load=forecast_eval[lo:hi],
                forecast=dict(record),
            )
        )

    sizes = [int(d["intervals"]) for d in eval_diag] or [hi - lo for _, lo, hi in eval_runs]
    first_intervals = eval_runs[0][2] - eval_runs[0][1] if eval_runs else 0
    last_intervals = eval_runs[-1][2] - eval_runs[-1][1] if eval_runs else 0
    block: dict[str, Any] = {
        "partition": {
            "basis": "local_calendar_day",
            "timezone": timezone,
            "source_column": "local_day",
            "day_count": len(days),
            "intervals_per_day_min": min(sizes) if sizes else 0,
            "intervals_per_day_max": max(sizes) if sizes else 0,
            "first_day_intervals": int(first_intervals),
            "last_day_intervals": int(last_intervals),
            "first_day_is_partial": bool(first_intervals < (max(sizes) if sizes else 0)),
            "last_day_is_partial": bool(last_intervals < (max(sizes) if sizes else 0)),
            "note": (
                "one MILP per Europe/Madrid calendar day, never per UTC day: the tariff is "
                "hourly in local time, so a spring-forward day is 92 intervals and a "
                "fall-back day 100 and both belong to exactly one solve. The first and last "
                "days of the evaluation window are partial whenever train_end does not fall "
                "on a local midnight; the forecast of a partial day is still built from the "
                "same slot positions of whole prior days, so partiality does not shift the "
                "profile"
            ),
        },
        "forecast": {
            "method": str(core.forecast_method),
            "window_days": int(forecast_window_days),
            "fallback_rule": str(core.forecast_fallback_rule),
            "history_may_cross_train_end": True,
            "history_note": (
                "the trailing window may reach back past train_end into the training split: "
                "strictly-past measured load is causally available at day D's midnight and "
                "excluding it would be an artificial handicap, not honesty. Leakage is "
                "defined against the target day, and no interval at or after the target "
                "day's first index is readable by the forecast"
            ),
            "enters_cost": False,
            "enters_cost_note": (
                "the forecast constrains the decisions (no-export discharge cap and import "
                "headroom) and nothing else: every euro is re-computed by the battery kernel "
                "against the measured load, so a forecast error degrades dispatch quality and "
                "is disclosed, but can never fabricate or hide cost"
            ),
            "summary": _forecast_summary(eval_diag),
        },
        "terminal_value": {
            "eur_per_kwh": float(terminal_value_eur_per_kwh),
            "source": terminal_value_source,
            "role": "sole cross-day coupling of a myopic policy",
        },
        "solver": {
            "time_limit_s_per_day": float(time_limit_s),
            "note": (
                "this time limit is per local day, not per horizon; the whole-horizon oracle "
                "limit is a different knob and is recorded separately"
            ),
        },
        "information_set": {
            "prices": (
                "the full 24 h curve of the day being planned, taken as known at local "
                "midnight of that day. Production evidence that this is not science fiction: "
                "apps/home-assistant/appdaemon/apps/climate_control.py:45 runs daily at "
                "00:00:40 and lines 73-91 of the same file read price_00h..price_23h off "
                "sensor.esios_pvpc (apps.yaml:10); dhw_control.py:48 does the same at "
                "00:00:30. ADR-0003 line 100 requires exactly this assumption to be labelled "
                "rather than assumed"
            ),
            "load": (
                "a strictly causal trailing-window forecast only; day D's measured load is "
                "unreachable from day D's solve, and so is every later day's"
            ),
            "state": "own SOC carried forward from the previous day's actual execution",
            "assumptions_disclosed": [
                "the archive stores the price REALISED in each hour, not a midnight snapshot "
                "of the sensor's price_00h..price_23h attribute dict, so this policy assumes "
                "published-in-advance equals realised-and-archived. True by construction for "
                "day-ahead PVPC, but not measured here, and the sensor's day-boundary "
                "convention stays unresolved (TODO-0.4)",
                "the day's usability mask (which intervals the archive trusts) is read for "
                "the whole day. It is a data-quality statement, not economic foresight, and "
                "is taken as shared because ADR-0003 requires gap exclusions to be identical "
                "across policies",
                "no cross-day arbitrage is available to this policy at all: it is myopic by "
                "construction, and the terminal SOC value is the only thing tying one day to "
                "the next",
            ],
        },
    }
    return days, block


def _day_ahead_payload(
    run_block: dict[str, Any],
    telemetry: dict[str, Any] | None,
    ran: bool,
) -> dict[str, Any]:
    """The day-ahead artifact: partition, forecast, terminal value, per-day solves.

    Recorded here and in the wide ``schedules.parquet`` only — ``BENCHMARK_RESULT`` and
    :meth:`energy_baseline.battery.Schedule.as_dict` are deliberately untouched, the same
    way the solver bounds and the metered/inventory split were handled when they were
    added.  One entry per local day is the point of the block: a rolling optimiser is
    judged day by day, and "the run finished" is not the same claim as "every day was
    solved".
    """
    if not ran or not run_block:
        return {
            "ran": False,
            "note": "day_ahead was not one of the selected policies for this run",
        }
    merged: dict[str, Any] = {**run_block, **(telemetry or {})}
    merged["ran"] = True
    merged["artifact_note"] = (
        "per_day carries one entry per local calendar day in the evaluation window: solve "
        "status and certification, the day's forecast error, the SOC it started and ended "
        "at, the energy the plan wanted and the clipping refused it, and any degradation "
        "to hold-SOC. Nothing in here is aggregated into BENCHMARK_RESULT, which is "
        "unchanged, and Schedule.as_dict was deliberately not extended for it either — "
        "the same precedent the solver bounds and the metered/inventory split followed"
    )
    return merged


def _day_ahead_record_extras(record: dict[str, Any]) -> dict[str, Any]:
    """Fields a day-ahead plan needs that the oracle's record vocabulary does not carry."""
    record["solver_time_limit_scope"] = "per_local_day"
    record["terminal_mode_scope"] = "per_local_day"
    record["optimisation_horizon"] = (
        "one local day per solve, rolled forward: the reported cost is the sum of daily "
        "optima executed against measured load, which is not the optimum of any "
        "horizon-length problem and claims no bound on one"
    )
    return record


def _solver_record(
    sched: Any,
    *,
    name: str,
    time_limit_s: float,
    mip_rel_gap: float,
    mip_abs_gap: float,
    terminal_mode_requested: str,
    terminal_mode_used: str,
) -> dict[str, Any]:
    """Everything the solver said, with unknowns left unknown."""
    cost = _float_of(getattr(sched, "cost_eur", None))
    upper = _first_float(sched, ("cost_upper_bound_eur", "primal_bound_eur"))
    lower = _first_float(sched, ("cost_lower_bound_eur", "dual_bound_eur"))
    bound_kind = getattr(sched, "bound_kind", None) or "none"
    is_solver_plan = terminal_mode_used != "not_applicable"
    if bound_kind != "none" and upper is None:
        # A returned incumbent IS an upper bound on optimal cost; no cost, no bound.
        upper = cost
    if bound_kind == "none":
        # Traced plans (baseline, causal) are not optima of anything.
        upper = lower = None
    return {
        "policy": name,
        "cost_eur": cost,
        "cost_upper_bound_eur": upper,
        "cost_lower_bound_eur": lower,
        "meter_cost_eur": _first_float(sched, ("meter_cost_eur",)),
        "inventory_adjustment_eur": _first_float(sched, ("inventory_adjustment_eur",)),
        "initial_soc_kwh": _first_float(sched, ("initial_soc_kwh",)),
        "terminal_soc_kwh": _first_float(sched, ("terminal_soc_kwh",)),
        # Under a fixed terminal condition there is no inventory valuation at all,
        # so the value is reported as absent rather than as a zero that could be
        # mistaken for the free-mode assumption.
        "terminal_value_eur_per_kwh": (
            _first_float(sched, ("terminal_value_eur_per_kwh",))
            if terminal_mode_used == "free"
            else None
        ),
        "soc_min_frac": _float_of(getattr(sched, "soc_min_frac", None)),
        "soc_max_frac": _float_of(getattr(sched, "soc_max_frac", None)),
        "efc": _float_of(getattr(sched, "efc", None)),
        "throughput_kwh": _float_of(getattr(sched, "throughput_kwh", None)),
        "solver_status": getattr(sched, "solver_status", None) or "not_applicable",
        "bound_kind": bound_kind,
        "is_certified": bool(getattr(sched, "is_certified", False)),
        "solver_gap": _float_of(getattr(sched, "solver_gap", None)),
        "solver_message": getattr(sched, "solver_message", None),
        # A traced plan never called the solver, so a time limit is not a fact
        # about it: null rather than the oracle's setting.
        "solver_time_limit_s": (
            _float_of(getattr(sched, "solver_time_limit_s", None)) or time_limit_s
            if is_solver_plan
            else None
        ),
        "configured_mip_rel_gap": mip_rel_gap,
        "configured_mip_abs_gap": mip_abs_gap,
        "terminal_mode_requested": terminal_mode_requested if is_solver_plan else "not_applicable",
        "terminal_mode_used": terminal_mode_used,
        "closed_cycle": None if not is_solver_plan else (terminal_mode_used != "free"),
    }


def _bounds_note(record: dict[str, Any]) -> list[str]:
    """What *record* may and may not be used to claim."""
    if record.get("bound_kind") == "none":
        return ["no solver bound applies: this is a traced plan (or the baseline), not an optimum"]
    notes: list[str] = []
    if record.get("is_certified"):
        notes.append(
            "certified within the configured tolerance, which is NOT a zero gap: "
            f"gap={record.get('solver_gap')}, "
            f"rel tolerance={record.get('configured_mip_rel_gap')}, "
            f"abs tolerance={record.get('configured_mip_abs_gap')}"
        )
    else:
        notes.append(
            f"NOT proven optimal (status={record.get('solver_status')}, "
            f"bound={record.get('bound_kind')}, gap={record.get('solver_gap')}): the cost is a "
            "feasible incumbent, an UPPER BOUND on optimal cost, so the implied saving is a "
            "LOWER bound on optimal saving"
        )
    if record.get("cost_lower_bound_eur") is None:
        notes.append(
            "no dual (lower cost) bound exists, so no upper bound on optimal saving can be claimed"
        )
    if record.get("solver_status") == "time_limit":
        notes.append(
            f"the solver stopped on its time limit ({record.get('solver_time_limit_s')} s): a "
            "longer limit may still lower this cost, so treat it as a provisional ceiling"
        )
    return notes


def _oracle_kwargs(
    solve_oracle: Any, terminal_mode: str, time_limit_s: float, rel: float, abs_: float
) -> tuple[dict[str, Any], str]:
    """Build the solver kwargs, choosing free terminal mode only if the core has it."""
    if terminal_mode not in ("free", "fixed"):
        raise BenchmarkInputError(f"terminal_mode must be 'free' or 'fixed', got {terminal_mode!r}")
    kwargs: dict[str, Any] = {"time_limit_s": time_limit_s, "mip_rel_gap": rel, "mip_abs_gap": abs_}
    if terminal_mode != "free":
        return kwargs, "fixed_default"
    try:
        params = inspect.signature(solve_oracle).parameters
    except (TypeError, ValueError):  # pragma: no cover - exotic callables
        params = {}
    if "terminal_mode" not in params:
        return kwargs, "fixed_default"
    kwargs["terminal_mode"] = "free"
    if "terminal_value_eur_per_kwh" in params:
        kwargs["terminal_value_eur_per_kwh"] = 0.0
    return kwargs, "free"


@dataclass(frozen=True)
class DayAheadInputs:
    """What the rolling day-ahead policy needs beyond the shared spec and horizon.

    Built by :func:`prepare_day_ahead` plus the resolved terminal valuation; ``None``
    whenever ``day_ahead`` was not selected, so a run that does not ask for the policy
    pays nothing for the partition and the forecast.
    """

    days: Sequence[Any]
    terminal_value_eur_per_kwh: float
    time_limit_s: float


def _run_policies(
    *,
    core: _Core,
    assumptions: BatteryAssumptions,
    prices: np.ndarray,
    load: np.ndarray,
    usable: np.ndarray,
    interval_seconds: float,
    thresholds: tuple[float, float] | None,
    selected: Sequence[str],
    time_limit_s: float,
    mip_rel_gap: float,
    mip_abs_gap: float,
    terminal_mode: str,
    day_ahead: DayAheadInputs | None = None,
) -> tuple[dict[str, dict[str, Any]], dict[str, Any], dict[str, dict[str, np.ndarray]]]:
    """Evaluate every selected policy on one horizon with one mask."""
    try:
        spec = core.battery_spec(**assumptions.spec_kwargs())
    except TypeError as exc:
        raise BenchmarkInputError(
            f"the resolved battery assumptions do not fit battery.BatterySpec: {exc}"
        ) from exc
    horizon = core.horizon(
        interval_seconds=interval_seconds,
        prices=prices,
        load_kwh=load,
        usable=usable,
        import_limit_kw=assumptions.import_limit_kw,
    )
    oracle_kwargs, terminal_mode_used = _oracle_kwargs(
        core.solve_oracle, terminal_mode, time_limit_s, mip_rel_gap, mip_abs_gap
    )
    if POLICY_DAY_AHEAD in selected and day_ahead is None:
        raise BenchmarkInputError(
            "day_ahead was selected but no per-day inputs were prepared — the caller must "
            "partition the evaluation window into local days and build its causal load "
            "forecast first (see prepare_day_ahead)"
        )

    records: dict[str, dict[str, Any]] = {}
    errors: dict[str, Any] = {}
    plans: dict[str, dict[str, np.ndarray]] = {}
    telemetry: dict[str, Any] = {}
    caught = _core_exceptions()

    for name in selected:
        if name == POLICY_ORACLE:
            used = terminal_mode_used
        elif name == POLICY_DAY_AHEAD:
            # Per-day free mode with a declared terminal value: same accounting framing as
            # the free oracle, which is what makes the two numbers comparable at all.
            used = "free"
        else:
            used = "not_applicable"
        day_limit = (
            day_ahead.time_limit_s if (name == POLICY_DAY_AHEAD and day_ahead) else time_limit_s
        )
        try:
            if name == POLICY_NO_BATTERY:
                sched = core.policy_no_battery(spec, horizon)
            elif name == POLICY_CAUSAL_PRICE:
                if thresholds is None:  # pragma: no cover - caller always fits these
                    raise BenchmarkInputError(
                        "causal_price needs thresholds fitted on a training split"
                    )
                sched = core.policy_causal_price(
                    spec, horizon, charge_threshold=thresholds[0], discharge_threshold=thresholds[1]
                )
            elif name == POLICY_DAY_AHEAD:
                if day_ahead is None:  # pragma: no cover - guarded above
                    raise BenchmarkInputError("day_ahead selected without inputs")
                # The scoring terminal value is the common benchmark value, same as the
                # oracle uses.  This is what makes the comparison fair: both use the same
                # terminal valuation for the reported cost.
                scoring_value = oracle_kwargs.get("terminal_value_eur_per_kwh", 0.0)
                sched, telemetry = core.policy_day_ahead(
                    spec,
                    horizon,
                    day_ahead.days,
                    terminal_value_eur_per_kwh=day_ahead.terminal_value_eur_per_kwh,
                    scoring_terminal_value_eur_per_kwh=scoring_value,
                    time_limit_s=day_ahead.time_limit_s,
                    mip_rel_gap=mip_rel_gap,
                    mip_abs_gap=mip_abs_gap,
                )
            else:
                sched = core.solve_oracle(spec, horizon, **oracle_kwargs)
        except NotImplementedError as exc:
            raise BenchmarkError(
                f"numeric core stub reached while running policy {name!r}: {exc}"
            ) from exc
        except Exception as exc:
            # Only the core's declared error classes become a recorded policy
            # failure.  Anything else (bad inputs, bugs) propagates.
            if not caught or not isinstance(exc, caught):
                raise
            errors[name] = {
                "error": f"{type(exc).__name__}: {exc}",
                "failure_kind": _failure_kind(exc),
                "is_certified": False,
                "note": (
                    "no BENCHMARK_RESULT row: cost_eur is non-nullable, so a policy with no "
                    "feasible cost is reported here rather than as a fabricated number"
                ),
            }
            continue

        record = _solver_record(
            sched,
            name=name,
            time_limit_s=day_limit,
            mip_rel_gap=mip_rel_gap,
            mip_abs_gap=mip_abs_gap,
            terminal_mode_requested=terminal_mode
            if name in (POLICY_ORACLE, POLICY_DAY_AHEAD)
            else "not_applicable",
            terminal_mode_used=used,
        )
        if name == POLICY_DAY_AHEAD:
            _day_ahead_record_extras(record)
        if record["cost_eur"] is None:
            # A schedule without a cost cannot become a BENCHMARK_RESULT row
            # (cost_eur is non-nullable), and must not be shown as "0.00 EUR".
            errors[name] = {
                "error": "the core returned a schedule with no cost_eur",
                "failure_kind": "no_cost",
                "is_certified": False,
                "note": "no BENCHMARK_RESULT row written for this policy",
            }
            continue
        record["bound_notes"] = _bounds_note(record)
        records[name] = record
        n = int(usable.size)
        plans[name] = {
            "charge_kwh": _array(sched, "charge_kwh", n),
            "discharge_kwh": _array(sched, "discharge_kwh", n),
            "grid_kwh": _array(sched, "grid_kwh", n),
            "soc_kwh": _array(sched, "soc_kwh", n),
            "net_flow_kwh": _array(sched, "net_flow_kwh", n),
        }

    solver_info = {
        "oracle_terminal_mode_requested": terminal_mode,
        "oracle_terminal_mode_used": terminal_mode_used if POLICY_ORACLE in selected else "not_run",
        "free_terminal_mode_supported_by_core": terminal_mode_used == "free",
        "solve_oracle_kwargs": oracle_kwargs,
        "errors": errors,
    }
    if POLICY_DAY_AHEAD in selected:
        solver_info["day_ahead"] = telemetry
    return records, solver_info, plans


#: Slack inside which a negative net grid import is float noise, not an export.
#: Deliberately the same number the kernel judges plans with (``battery._TOL_KWH``):
#: :func:`energy_baseline.battery.check_feasible` accepts ``grid >= -1e-6`` kWh, so a
#: value in that band IS zero by the project's own tolerance, while
#: :func:`energy_baseline.cost.compute_interval_cost` refuses any negative at all.  The
#: gap between those two rules is real and was observed on a four-policy run: the
#: ORACLE's own plan carried ``discharge = 0.09280000000000013`` against
#: ``load = 0.0928`` at the final interval of a 100-slot DST day — HiGHS returning a
#: no-export row from the bound side, one ulp over it.  A plan the kernel calls feasible
#: must therefore cost as feasible, and the place that reconciles the two is the
#: orchestrator, because it owns serialising what came back.  Beyond this slack a
#: negative is never snapped: the cost model's error is the correct, loud outcome.
_GRID_NOISE_KWH = 1e-6


def _interval_costs(
    cost_fn: Any,
    grid: np.ndarray,
    prices: np.ndarray,
    usable: np.ndarray,
    multiplier: float | None,
    *,
    record_taxed: bool,
) -> tuple[list[float], list[float], int]:
    """Per-interval cost on both bases, computed by the core cost model.

    Excluded intervals come back as NaN: their energy and price are not knowable, so
    they contribute nothing rather than something invented.  The taxed column stays
    NaN unless a taxed basis was actually established, so no reader can mistake a
    pretax figure for a bill.

    Returns the two columns plus the count of usable intervals whose net import was a
    negative *inside* :data:`_GRID_NOISE_KWH` and so was costed as zero rather than
    raised on.  The count is reported per policy in ``cost_consistency``: a nonzero
    number is a solver emitting a bound one ulp over its own row, which is normal at
    1e-16 kWh, and it must be visible rather than silently absorbed.
    """
    pretax: list[float] = []
    taxed: list[float] = []
    snapped = 0
    for i in range(grid.size):
        if not usable[i]:
            pretax.append(math.nan)
            taxed.append(math.nan)
            continue
        energy, price = float(grid[i]), float(prices[i])
        if -_GRID_NOISE_KWH <= energy < 0.0:
            energy = 0.0
            snapped += 1
        base = float(cost_fn(energy_kwh=energy, price_eur_per_kwh=price, tax_multiplier=None))
        pretax.append(base)
        if not record_taxed:
            taxed.append(math.nan)
        elif multiplier in (None, 1.0):
            taxed.append(base)
        else:
            taxed.append(
                float(
                    cost_fn(energy_kwh=energy, price_eur_per_kwh=price, tax_multiplier=multiplier)
                )
            )
    return pretax, taxed, snapped


# ---------------------------------------------------------------------------
# Output artifacts
# ---------------------------------------------------------------------------

#: Wide per-interval plan table.  Not part of the canonical store schemas: it is the
#: benchmark's own record of what each policy did, and only ever grows columns.
SCHEDULE_SCHEMA = pa.schema(
    [
        pa.field("run_id", pa.string(), nullable=False),
        pa.field("policy", pa.string(), nullable=False),
        pa.field("dataset_id", pa.string(), nullable=False),
        pa.field("interval_start", pa.timestamp("us", tz="UTC"), nullable=False),
        pa.field("interval_end", pa.timestamp("us", tz="UTC"), nullable=False),
        pa.field("local_day", pa.date32(), nullable=False),
        pa.field("usable", pa.bool_(), nullable=False),
        pa.field("exclusion_reason", pa.string(), nullable=False),
        pa.field("price_eur_per_kwh", pa.float64(), nullable=True),
        pa.field("load_kwh", pa.float64(), nullable=True),
        pa.field("charge_kwh", pa.float64(), nullable=True),
        pa.field("discharge_kwh", pa.float64(), nullable=True),
        pa.field("grid_kwh", pa.float64(), nullable=True),
        pa.field("soc_kwh", pa.float64(), nullable=True),
        pa.field("interval_cost_eur", pa.float64(), nullable=True),
        pa.field("interval_cost_taxed_eur", pa.float64(), nullable=True),
    ]
)


def _schedules_table(
    *,
    run_id: str,
    dataset_id: str,
    starts: Sequence[datetime],
    ends: Sequence[datetime],
    days: Sequence[Any],
    usable: np.ndarray,
    reasons: Sequence[str],
    prices: np.ndarray,
    load: np.ndarray,
    plans: dict[str, dict[str, np.ndarray]],
    costs: dict[str, tuple[list[float], list[float]]],
) -> pa.Table:
    """One block of *n* rows per policy, stacked into a tidy-long table."""
    n = len(starts)
    columns: dict[str, list[Any]] = {name: [] for name in SCHEDULE_SCHEMA.names}
    for name, plan in plans.items():
        pretax, taxed = costs[name]
        per_policy = {
            "run_id": [run_id] * n,
            "policy": [name] * n,
            "dataset_id": [dataset_id] * n,
            "interval_start": list(starts),
            "interval_end": list(ends),
            "local_day": list(days),
            "usable": [bool(v) for v in usable],
            "exclusion_reason": list(reasons),
            "price_eur_per_kwh": _nullable(prices),
            "load_kwh": _nullable(load),
            "charge_kwh": _nullable(plan["charge_kwh"]),
            "discharge_kwh": _nullable(plan["discharge_kwh"]),
            "grid_kwh": _nullable(plan["grid_kwh"]),
            "soc_kwh": _nullable(plan["soc_kwh"]),
            "interval_cost_eur": _nullable(pretax),
            "interval_cost_taxed_eur": _nullable(taxed),
        }
        for column, values in per_policy.items():
            columns[column].extend(values)

    types = {f.name: f.type for f in SCHEDULE_SCHEMA}
    table = pa.table(
        {name: pa.array(values, types[name]) for name, values in columns.items()},
        schema=SCHEDULE_SCHEMA,
    )
    if table.num_rows != n * len(plans):  # pragma: no cover - internal invariant
        raise BenchmarkError("schedule row count does not match policies x intervals")
    return table


def _results_table(
    records: dict[str, dict[str, Any]],
    *,
    dataset_id: str,
    start: datetime,
    end: datetime,
    cost_basis: str,
) -> pa.Table:
    """One row per policy, exactly ``BENCHMARK_RESULT`` — no extra columns.

    ``soc_min`` / ``soc_max`` are the achieved SOC *fractions* of nameplate, matching
    the core schedule; the kWh view lives in ``benchmark.json`` and schedules.
    """
    values = list(records.values())
    table = pa.table(
        {
            "policy": pa.array(list(records), pa.string()),
            "dataset_id": pa.array([dataset_id] * len(values), pa.string()),
            "horizon_start": pa.array([start] * len(values), pa.timestamp("us", tz="UTC")),
            "horizon_end": pa.array([end] * len(values), pa.timestamp("us", tz="UTC")),
            "cost_eur": pa.array([r["cost_eur"] for r in values], pa.float64()),
            "cost_basis": pa.array([cost_basis] * len(values), pa.string()),
            "efc": pa.array([r["efc"] or 0.0 for r in values], pa.float64()),
            "throughput_kwh": pa.array([r["throughput_kwh"] or 0.0 for r in values], pa.float64()),
            "soc_min": pa.array([r["soc_min_frac"] or 0.0 for r in values], pa.float64()),
            "soc_max": pa.array([r["soc_max_frac"] or 0.0 for r in values], pa.float64()),
            "solver_gap": pa.array([r["solver_gap"] for r in values], pa.float64()),
            "solver_status": pa.array([r["solver_status"] for r in values], pa.string()),
            "bound_kind": pa.array([r["bound_kind"] for r in values], pa.string()),
            "is_certified": pa.array([bool(r["is_certified"]) for r in values], pa.bool_()),
        },
        schema=BENCHMARK_RESULT,
    )
    validate_table("BENCHMARK_RESULT", table)
    return table


def _parquet_bytes(table: pa.Table) -> bytes:
    sink = io.BytesIO()
    pq.write_table(table, sink)
    return sink.getvalue()


#: Build-manifest keys worth pinning into run lineage (raw inputs and their hashes).
_LINEAGE_KEYS = (
    "manifest_version",
    "schema_version",
    "config_hash",
    "code_version",
    "requested_window",
    "coverage",
    "params",
    "quality_counts",
    "part_hashes",
    "raw_inputs",
    "inputs",
    "source_manifests",
    "excluded_intervals",
    "gaps",
    "counter_resets",
    "series",
    "entity_audit",
)


def _lineage(
    dataset: LoadedDataset, config_path: Path, cfg_hash: str, version: str
) -> dict[str, Any]:
    """Everything needed to re-derive this run from source, pinned by hash."""
    return {
        "dataset_id": dataset.dataset_id,
        "dataset_dir": str(dataset.directory),
        "build_manifest": str(dataset.directory / "build_manifest.json"),
        "build_manifest_sha256": dataset.manifest_sha256,
        "store_schema_version": SCHEMA_VERSION,
        "benchmark_contract_version": BENCHMARK_VERSION,
        "analysis_parts": [
            {
                "path": e["resolved_path"],
                "sha256": e.get("verified_sha256"),
                "rows": e.get("rows"),
            }
            for e in dataset.part_files
        ],
        "unlisted_parquet_in_parts_dir": dataset.unlisted_parts,
        "config_path": str(config_path),
        "config_hash": cfg_hash,
        "code_version": version,
        "build_manifest_pinned_fields": {
            k: dataset.manifest.get(k) for k in _LINEAGE_KEYS if k in dataset.manifest
        },
    }


def _quality_block(
    reasons: Sequence[str],
    eval_reasons: Sequence[str],
    per_month: dict[str, dict[str, Any]],
    eval_months: set[str],
    min_usable_fraction: float,
    allow_reconstructed: bool,
) -> dict[str, Any]:
    return {
        "rules": {
            "measured_energy_quality": sorted(MEASURED_ENERGY_QUALITY),
            "usable_allocation_quality": sorted(USABLE_ALLOCATION_QUALITY),
            "measured_price_quality": sorted(MEASURED_PRICE_QUALITY),
            "reconstructable_with_flag": {
                "energy_quality": sorted(RECONSTRUCTED_ENERGY_QUALITY),
                "price_quality": sorted(RECONSTRUCTED_PRICE_QUALITY),
            },
            "never_benchmarked": {
                "allocation_quality": sorted(NEVER_ALLOCABLE),
                "price_quality": sorted(NEVER_PRICE),
            },
            "require_non_negative_finite_gross_kwh": True,
            "require_finite_price": True,
            "allow_reconstructed_quality": allow_reconstructed,
        },
        "declared_thresholds": {
            "min_usable_fraction": min_usable_fraction,
            "note": (
                "0.0 excludes no month on mask-rate grounds; the measured per-month fractions "
                "are reported alongside so coverage can be judged by the reader"
            ),
        },
        "dataset_exclusion_reasons": _count_reasons(reasons),
        "eval_exclusion_reasons": _count_reasons(eval_reasons),
        "per_month": [
            {
                "month": month,
                "intervals_total": per_month[month]["total"],
                "intervals_usable": per_month[month]["usable"],
                "intervals_excluded": per_month[month]["total"] - per_month[month]["usable"],
                "usable_fraction": per_month[month]["usable_fraction"],
                "in_evaluation": month in eval_months,
            }
            for month in sorted(per_month)
        ],
    }


def _assumption_block(
    assumptions: BatteryAssumptions, basis: dict[str, Any], solver_info: dict[str, Any]
) -> dict[str, Any]:
    free = solver_info["oracle_terminal_mode_used"] == "free"
    return {
        "battery": assumptions.as_dict(),
        "soc_window_and_efficiency_are_distinct": (
            "the SOC window (min/max fraction of nameplate) and the efficiencies are independent "
            "assumptions: a 0.05-0.95 window is not a 90% efficiency and neither implies the other"
        ),
        "idle_losses_note": (
            "idle / standby battery losses are assumed ZERO and were not measured"
            if not assumptions.idle_losses_measured
            else f"idle losses taken from configuration: {assumptions.idle_loss_kwh_per_hour} kWh/h"
        ),
        "battery_hardware_present": False,
        "gross_load_basis": (
            "no battery is installed, so gross household load is the whole-home feed as built by "
            "the dataset. Once a battery exists, gross load must be reconstructed from the feed "
            "plus AC charge/discharge telemetry, or the interval must be excluded"
        ),
        "exclusions_are_neutral_carry": (
            "excluded intervals carry SOC unchanged with zero flow: no charging, no discharging, "
            "no self-discharge. Energy in a gap is neither invented nor attributed to an interval"
        ),
        "no_intentional_export": "grid flow is constrained to be >= 0 (no export)",
        "cost_objective": basis,
        "site_import_limit": assumptions.import_limit_source,
        "terminal_accounting": {
            "scope": "oracle (one solve over the whole horizon)",
            "requested": solver_info["oracle_terminal_mode_requested"],
            "used": solver_info["oracle_terminal_mode_used"],
            "terminal_value_eur_per_kwh": 0.0 if free else None,
            "closed_cycle": not free,
            "note": (
                "the oracle is NOT a closed cycle: the horizon ends at whatever SOC the solution "
                "lands on, that energy is valued at 0 EUR/kWh, and the objective is pure metered "
                "cash. Energy left in the battery at the end is free in this accounting — an "
                "explicit assumption of this run, not a battery property, and the reason terminal "
                "inventory adjustments are reported separately"
                if free
                else "the core has no free terminal mode, so the fixed-cycle default (terminal "
                "SOC = initial SOC) was used: this run IS a closed cycle"
            ),
            "day_ahead": _day_ahead_terminal_note(solver_info),
            "policies_share_a_scoring_basis_differ_in_planning": (
                "the oracle and the day-ahead policy are SCORED on the same terminal-SOC "
                "basis (scoring_terminal_value_eur_per_kwh, default 0.0), so the reported "
                "cost_eur figures are comparable. They differ only in the day-ahead policy's "
                "internal planning value V, used inside each daily objective to stop the "
                "myopic solve from emptying the battery at midnight (ADR-0001). V does not "
                "touch the reported cost, the inventory adjustment, or the oracle "
                "comparison; it lives in each day's objective and is recorded separately as "
                "day_ahead.internal_daily_objective_sum_eur. Both figures are meter cost "
                "plus their own inventory adjustment, and each adjustment is reported per "
                "policy (policies.<name>.inventory_adjustment_eur), so a reader can always "
                "see how much of a claimed saving is stored energy rather than cash"
            ),
        },
    }


def _day_ahead_terminal_note(solver_info: dict[str, Any]) -> dict[str, Any]:
    """Per-day terminal SOC valuation of the day-ahead policy, or why there is none."""
    telemetry = solver_info.get("day_ahead") or {}
    if not telemetry:
        return {
            "ran": False,
            "note": "day_ahead was not part of this run, so no per-day terminal valuation applies",
        }
    return {
        "ran": True,
        "scope": "day_ahead (one solve per local calendar day, SOC rolled forward)",
        "terminal_mode_used": "free",
        "internal_planning_value_eur_per_kwh": telemetry.get("terminal_value_eur_per_kwh"),
        "scoring_value_eur_per_kwh": telemetry.get("scoring_terminal_value_eur_per_kwh"),
        "closed_cycle": False,
        "role": telemetry.get("terminal_value_role"),
        "scoring_role": telemetry.get("scoring_terminal_value_role"),
        "myopia": telemetry.get("myopia_note"),
        "note": (
            "the day-ahead policy has two terminal values, not one. The internal planning "
            "value (V) credits end-of-day SOC inside each daily objective so the myopic "
            "solve does not empty the battery at midnight — ADR-0001's terminal-SOC rule. "
            "The scoring value is what the reported Schedule.cost_eur and "
            "inventory_adjustment_eur use, and it is the SAME value the oracle uses so the "
            "comparison is on the same basis. The per-day objectives sum to the "
            "internal-V-based cost, recorded separately as "
            "day_ahead.internal_daily_objective_sum_eur and explicitly NOT the common "
            "score. V is a frozen training-window statistic (or an explicit caller "
            "override); the scoring value is the run-wide basis and is not a knob to tune"
        ),
    }


def _comparison_block(
    records: dict[str, dict[str, Any]], basis: dict[str, Any], solver_info: dict[str, Any]
) -> dict[str, Any]:
    baseline = records.get(POLICY_NO_BATTERY, {}).get("cost_eur")
    out: dict[str, Any] = {
        "baseline_cost_eur": baseline,
        "basis": basis["cost_basis"],
        "per_policy": {},
        "causal_may_lose_baseline": True,
        "day_ahead_may_lose_causal": True,
        "day_ahead_may_lose_baseline": True,
        "identical_gap_exclusions": True,
    }
    for name, record in records.items():
        cost = record["cost_eur"]
        savings = None if baseline is None or cost is None else baseline - cost
        lower = upper = None
        if baseline is not None:
            if record["cost_upper_bound_eur"] is not None:
                lower = baseline - record["cost_upper_bound_eur"]
            if record["cost_lower_bound_eur"] is not None:
                upper = baseline - record["cost_lower_bound_eur"]
        out["per_policy"][name] = {
            "savings_eur": savings,
            "savings_lower_bound_eur": lower,
            "savings_upper_bound_eur": upper,
            "worse_than_baseline": None if savings is None else savings < 0.0,
        }
    oracle_cost = records.get(POLICY_ORACLE, {}).get("cost_eur")
    causal_cost = records.get(POLICY_CAUSAL_PRICE, {}).get("cost_eur")
    day_ahead_cost = records.get(POLICY_DAY_AHEAD, {}).get("cost_eur")
    out["oracle_dominates_baseline"] = (
        None if oracle_cost is None or baseline is None else oracle_cost <= baseline + _EPS
    )
    out["oracle_dominates_causal"] = (
        None if oracle_cost is None or causal_cost is None else oracle_cost <= causal_cost + _EPS
    )
    out["oracle_dominates_day_ahead"] = (
        None
        if oracle_cost is None or day_ahead_cost is None
        else oracle_cost <= day_ahead_cost + _EPS
    )
    # What the new rung bought: positive means the day-ahead information set came out
    # cheaper than the frozen-threshold rule, i.e. part of the causal→oracle gap is
    # reachable from prices that are published for free.  Negative is a real, allowed,
    # informative answer, not an error to fix.
    out["day_ahead_minus_causal_eur"] = (
        None
        if day_ahead_cost is None or causal_cost is None
        else float(day_ahead_cost) - float(causal_cost)
    )
    out["day_ahead_closes_gap_fraction"] = (
        None
        if day_ahead_cost is None
        or causal_cost is None
        or oracle_cost is None
        or (causal_cost - oracle_cost) <= 0
        else float((causal_cost - day_ahead_cost) / (causal_cost - oracle_cost))
    )
    oracle_dominates_day_ahead_guaranteed = bool(
        oracle_cost is not None
        and day_ahead_cost is not None
        and records.get(POLICY_ORACLE, {}).get("is_certified")
        and solver_info["oracle_terminal_mode_used"] == "free"
    )
    out["oracle_dominates_day_ahead_guaranteed"] = oracle_dominates_day_ahead_guaranteed
    out["oracle_dominates_day_ahead_guaranteed_note"] = (
        "True only when the oracle is certified optimal AND its terminal mode is 'free'. "
        "A free oracle minimises over exactly the feasible set that contains the day-ahead "
        "plan (same SOC floor max(soc_min, initial), same physics, same endpoint freedom), "
        "so oracle.cost_eur <= day_ahead.cost_eur by construction. When terminal_mode is "
        "'fixed' the oracle pins terminal SOC to the initial value and its feasible set may "
        "exclude the day-ahead plan's actual final SOC, which leaves the day-ahead plan "
        "outside the oracle's optimisation problem: no dominance is provable, even if the "
        "empirical comparison happens to hold. Do not assert fixed dominance."
    )
    out["dominance_note"] = (
        "the oracle's feasible set contains every other plan only under this run's "
        f"accounting (terminal_mode_used={solver_info['oracle_terminal_mode_used']}, SOC "
        "floored at the initial value, zero terminal inventory value). The day-ahead plan "
        "is additionally a feasible point of the oracle's own problem — its clipped, "
        "measured-load trajectory respects the SOC window, the no-export rule, the power "
        "limit, the charging-side ceiling headroom and one binary mode per interval, its "
        "SOC floor is exactly the horizon-wide causal floor "
        "max(soc_min, initial SOC) on every day (seed protection belongs to the window, "
        "not to the day), and its terminal valuation telescopes to exactly the one the "
        "oracle applies — so oracle <= day_ahead is guaranteed WHENEVER the oracle is a "
        "certified optimum AND terminal_mode_used is 'free' (same endpoint). On a "
        "time-limited incumbent the oracle reports its own best feasible cost, which "
        "comparison is empirical, hence the separate oracle_dominates_day_ahead_guaranteed "
        "flag. When terminal_mode_used is 'fixed', the oracle pins terminal SOC to initial, "
        "which may exclude the day-ahead final SOC, so the guarantee does not hold. "
        "Day-ahead versus causal is guaranteed in NEITHER direction: a myopic optimiser "
        "with a poor load forecast and a badly chosen terminal value can lose to a dumb "
        "threshold rule, so the difference is reported as day_ahead_minus_causal_eur and no "
        "assertion is made about its sign. A violated dominance claim is reported, not "
        "clipped: it is an accounting signal, not a solver bug. The causal policy may lose "
        "to the baseline and that loss is reported at face value"
    )
    return out


# ---------------------------------------------------------------------------
# Public entry point
# ---------------------------------------------------------------------------


def benchmark_dataset(
    *,
    dataset: Path | str,
    config_path: Path | str,
    train_end: datetime | str | None,
    policies: Sequence[str] | str | None = None,
    train_days: int = DEFAULT_TRAIN_DAYS,
    charge_pct: float = DEFAULT_CHARGE_PCT,
    discharge_pct: float = DEFAULT_DISCHARGE_PCT,
    initial_soc_frac: float | None = None,
    import_limit_kw: float | None = None,
    min_usable_fraction: float = 0.0,
    allow_reconstructed_quality: bool = False,
    time_limit_s: float = DEFAULT_TIME_LIMIT_S,
    mip_rel_gap: float = DEFAULT_MIP_REL_GAP,
    mip_abs_gap: float = DEFAULT_MIP_ABS_GAP,
    terminal_mode: str = "free",
    day_ahead_forecast_window_days: int = DEFAULT_FORECAST_WINDOW_DAYS,
    day_ahead_terminal_value_eur_per_kwh: float | None = None,
    day_ahead_time_limit_s: float = DEFAULT_DAY_AHEAD_TIME_LIMIT_S,
    data_dir: Path | str | None = None,
) -> Path:
    """Run the selected policies on a built dataset and write an immutable run.

    Args:
        dataset: the dataset directory produced by ``build``
            (``analysis/v<schema>/<dataset_id>``); read through its manifest only.
        config_path: ``tariff.yaml`` — taxes, timezone, battery parameters.
        train_end: absolute train/eval cut.  Thresholds are fitted on usable intervals
            before it and frozen; evaluation is everything from it onwards.  Must be
            timezone-aware, not in the future, and leave usable intervals on both sides.
        policies: ``"all"``, a comma-separated string, or a subset of ``no_battery``,
            ``causal_price``, ``day_ahead``, ``oracle``.
        train_days: how far back before *train_end* the thresholds may look.
        charge_pct / discharge_pct: percentiles of the TRAINING prices, frozen after
            fitting; no evaluation price ever influences them.
        initial_soc_frac: starting SOC as a fraction of nameplate.  Omitted means
            tariff.yaml's battery.initial_soc_frac, then soc_window_min — the run
            records which source was used.
        import_limit_kw: overrides ``battery.import_limit_kw``; ``None`` with a null
            config leaves the site import ceiling unapplied and reports that.
        min_usable_fraction: declared monthly quality threshold; a month in the
            evaluation window below it aborts the run.  0.0 aborts on nothing but the
            measured fractions are still reported.
        allow_reconstructed_quality: admit reconstructed values: forward-filled
            prices and partially gapped energy totals.  Allocation boundaries
            (``estimated_boundary``) are admitted by default and are NOT gated by
            this flag.  ``unknown_gap``/``gap_total_unknown`` allocation and
            ``missing``/``ambiguous`` prices are never admitted, with or without
            the flag.
        time_limit_s / mip_rel_gap / mip_abs_gap: passed to the solver and recorded;
            certification means "within tolerance", not "zero gap".  ``time_limit_s`` is
            the whole-horizon oracle's limit; the day-ahead policy has its own per-day
            limit (``day_ahead_time_limit_s``) because the two numbers answer different
            questions and a single knob would make one of them meaningless.
        terminal_mode: ``"free"`` (default) or ``"fixed"`` (closed cycle), for the
            oracle.  The day-ahead policy is always per-day free with a declared
            terminal value — that is what makes it day-ahead rather than a smaller
            oracle.
        day_ahead_forecast_window_days: trailing window, in local days, of the causal
            load forecast (``>= 1``).  Recorded, along with the fallback rule it uses
            when a slot has no prior sample.
        day_ahead_terminal_value_eur_per_kwh: EUR/DC-kWh value on end-of-day SOC — the
            day-ahead policy's only cross-day coupling.  ``None`` (default) derives it
            from the frozen training window (:data:`TERMINAL_VALUE_DEFAULT_RULE`); an
            explicit number is allowed but is recorded with ``cli`` provenance so a
            hand-picked valuation is never mistaken for a measured one.  ``0.0`` is a
            legal, documented choice: a pure cash-flow objective whose plan empties the
            battery by midnight.
        day_ahead_time_limit_s: per-local-day solver limit for the rolling solves.  Days
            that consumed it are counted in the artifact, because such a day makes the
            run depend on machine speed.
        data_dir: data root, when the manifest lists parts relative to it.

    Returns:
        the run directory ``<dataset>/benchmarks/<run_id>`` holding
        ``benchmark.json``, ``results.parquet`` and ``schedules.parquet``.

    Raises:
        BenchmarkInputError: unusable inputs — unverified dataset, unconfirmed
            configuration, bad train split, month below threshold, empty evaluation,
            a day-ahead partition or terminal valuation that cannot be justified.
        BenchmarkError: the numeric core is unavailable, or no policy produced a cost.
    """
    selected = _parse_policies(policies)
    if not 0.0 <= min_usable_fraction <= 1.0:
        raise BenchmarkInputError(
            f"min_usable_fraction must be in [0, 1], got {min_usable_fraction}"
        )
    if time_limit_s <= 0:
        raise BenchmarkInputError(f"time_limit_s must be > 0, got {time_limit_s}")
    if mip_rel_gap <= 0 or mip_abs_gap < 0:
        raise BenchmarkInputError(
            f"mip_rel_gap must be > 0 and mip_abs_gap >= 0, got {mip_rel_gap}/{mip_abs_gap}"
        )
    if train_days < 1:
        raise BenchmarkInputError(f"train_days must be >= 1, got {train_days}")
    if POLICY_DAY_AHEAD in selected:
        # Only checked when the policy is actually run: an unrelated three-policy run
        # must not be refused over a knob it never uses.
        if day_ahead_forecast_window_days < 1:
            raise BenchmarkInputError(
                f"day_ahead_forecast_window_days must be >= 1, got {day_ahead_forecast_window_days}"
            )
        if day_ahead_time_limit_s <= 0:
            raise BenchmarkInputError(
                f"day_ahead_time_limit_s must be > 0, got {day_ahead_time_limit_s}"
            )
        if day_ahead_terminal_value_eur_per_kwh is not None and not math.isfinite(
            float(day_ahead_terminal_value_eur_per_kwh)
        ):
            raise BenchmarkInputError(
                "day_ahead_terminal_value_eur_per_kwh must be finite or None, got "
                f"{day_ahead_terminal_value_eur_per_kwh!r}"
            )

    dataset_dir = Path(dataset).expanduser()
    cfg_path = Path(config_path).expanduser()
    if not cfg_path.is_file():
        raise BenchmarkInputError(f"tariff config not found: {cfg_path}")
    loaded = load_dataset(dataset_dir, data_dir=data_dir)
    config = load_config(cfg_path)
    cfg_hash = config_hash(cfg_path)
    pinned = loaded.manifest.get("config_hash")
    if not pinned:
        raise BenchmarkInputError(
            f"dataset {loaded.dataset_id}: build manifest pins no config_hash — the dataset "
            "cannot be benchmarked because the tariff it was built against is unknown. Rebuild "
            "the dataset with the current build."
        )
    if pinned != cfg_hash:
        raise BenchmarkInputError(
            f"tariff config drift for dataset {loaded.dataset_id}: the build manifest pins "
            f"config_hash={pinned[:12]} but {cfg_path} hashes to {cfg_hash[:12]} — this dataset "
            "was built against a different tariff (and different battery assumptions) than the "
            "one being benchmarked. Rebuild the dataset with this config, or pass the config "
            "the dataset was built with (--config). There is no bypass: benchmarking a battery "
            "against another build's data is exactly the error this check exists for"
        )
    cut = parse_train_end(train_end)
    assumptions = resolve_battery_assumptions(
        config, initial_soc_frac=initial_soc_frac, import_limit_kw=import_limit_kw
    )
    core = core_or_raise()

    table = loaded.table
    n = table.num_rows
    starts = table.column("interval_start").to_pylist()
    ends = table.column("interval_end").to_pylist()
    days = table.column("local_day").to_pylist()
    usable, reasons = usable_mask(table, allow_reconstructed=allow_reconstructed_quality)

    def _col(name: str) -> np.ndarray:
        return np.array([0.0 if v is None else float(v) for v in table.column(name).to_pylist()])

    gross = _col("gross_kwh")
    prices = _col("price_eur_per_kwh")

    # ---- train / eval split -------------------------------------------------
    train_window_start = cut - timedelta(days=int(train_days))
    train_mask = np.array([s < cut and s >= train_window_start for s in starts])
    eval_mask = np.array([s >= cut for s in starts])
    train_usable = int(np.count_nonzero(train_mask & usable))
    eval_usable = int(np.count_nonzero(eval_mask & usable))
    if train_usable == 0:
        raise BenchmarkInputError(
            f"the training split has 0 usable intervals in the {train_days} day(s) before "
            f"{_iso(cut)} (dataset {_iso(loaded.start)} .. {_iso(loaded.end)}, "
            f"{int(np.count_nonzero(usable))} usable in total): thresholds cannot be frozen from "
            "nothing. Pass an earlier train_end, more train_days, or a wider dataset"
        )
    if eval_usable == 0:
        raise BenchmarkInputError(
            f"the evaluation split has 0 usable intervals from {_iso(cut)} onwards — nothing to "
            "benchmark (a train_end at or past the end of the dataset is the usual cause)"
        )

    thresholds: tuple[float, float] | None = None
    if POLICY_CAUSAL_PRICE in selected:
        fitted = core.fit_thresholds(
            prices[train_mask & usable], charge_pct=charge_pct, discharge_pct=discharge_pct
        )
        thresholds = (float(fitted[0]), float(fitted[1]))

    # ---- declared monthly quality thresholds --------------------------------
    months = [_local_month(starts[i], days[i]) for i in range(n)]
    per_month: dict[str, dict[str, Any]] = {}
    for i, month in enumerate(months):
        bucket = per_month.setdefault(month, {"total": 0, "usable": 0, "usable_fraction": 0.0})
        bucket["total"] += 1
        bucket["usable"] += int(bool(usable[i]))
    eval_months = {months[i] for i in range(n) if eval_mask[i]}
    thin: list[str] = []
    for month, bucket in per_month.items():
        bucket["usable_fraction"] = bucket["usable"] / bucket["total"] if bucket["total"] else 0.0
        if month in eval_months and bucket["usable_fraction"] < min_usable_fraction:
            thin.append(f"{month}: {bucket['usable_fraction']:.3f} < {min_usable_fraction:.3f}")
    if thin:
        raise BenchmarkInputError(
            f"monthly quality threshold not met (declared min_usable_fraction="
            f"{min_usable_fraction}): " + "; ".join(sorted(thin))
        )

    # ---- evaluation window: one horizon, one mask, every policy -------------
    index = np.array([i for i in range(n) if eval_mask[i]], dtype=int)
    eval_starts = [starts[i] for i in index]
    eval_ends = [ends[i] for i in index]
    eval_usable = usable[index]
    eval_prices = prices[index]

    # ---- day-ahead: local-day partition, causal forecast, frozen terminal value --
    day_ahead: DayAheadInputs | None = None
    day_ahead_block: dict[str, Any] = {}
    if POLICY_DAY_AHEAD in selected:
        terminal_value, terminal_value_source = resolve_day_ahead_terminal_value(
            configured=day_ahead_terminal_value_eur_per_kwh,
            train_prices=prices[train_mask & usable],
            train_window_start=train_window_start,
            train_end=cut,
        )
        da_days, day_ahead_block = prepare_day_ahead(
            core=core,
            load_kwh=gross,
            usable=usable,
            local_days=days,
            eval_index=index,
            forecast_window_days=day_ahead_forecast_window_days,
            terminal_value_eur_per_kwh=terminal_value,
            terminal_value_source=terminal_value_source,
            time_limit_s=day_ahead_time_limit_s,
            timezone=config.tariff_periods.timezone,
        )
        day_ahead = DayAheadInputs(
            days=da_days,
            terminal_value_eur_per_kwh=terminal_value,
            time_limit_s=day_ahead_time_limit_s,
        )
    records, solver_info, plans = _run_policies(
        core=core,
        assumptions=assumptions,
        prices=eval_prices,
        load=gross[index],
        usable=eval_usable,
        interval_seconds=loaded.interval_seconds,
        thresholds=thresholds,
        selected=selected,
        time_limit_s=time_limit_s,
        mip_rel_gap=mip_rel_gap,
        mip_abs_gap=mip_abs_gap,
        terminal_mode=terminal_mode,
        day_ahead=day_ahead,
    )
    if not records:
        raise BenchmarkError(
            "no policy produced a cost row — "
            + json.dumps({k: v["error"] for k, v in solver_info["errors"].items()}, default=str)
        )

    basis = tax_basis(config, [s for s, ok in zip(eval_starts, eval_usable) if ok])
    multiplier = basis["uniform_multiplier"] if basis["cost_basis"] == "taxed" else None
    costs: dict[str, tuple[list[float], list[float]]] = {}
    consistency: dict[str, Any] = {}
    for name, plan in plans.items():
        pretax, taxed, snapped = _interval_costs(
            core.interval_cost,
            plan["grid_kwh"],
            eval_prices,
            eval_usable,
            multiplier,
            record_taxed=basis["cost_basis"] == "taxed",
        )
        costs[name] = (pretax, taxed)
        reported = records[name]["cost_eur"]
        # Re-cost the returned plan on the shared mask: a mismatch means the reported
        # plan is not the plan that produced the reported cost.
        recomputed = float(np.nansum(np.asarray(pretax, dtype=float)))
        adjustment = records[name]["inventory_adjustment_eur"] or 0.0
        consistency[name] = {
            "core_cost_eur": reported,
            "recomputed_meter_cost_eur": recomputed,
            "inventory_adjustment_eur": adjustment,
            "abs_diff_eur": None if reported is None else abs(recomputed + adjustment - reported),
            "ok": reported is not None and abs(recomputed + adjustment - reported) <= 1e-6,
            # Solver noise, disclosed: intervals whose net import came back negative by
            # less than the kernel's own slack and was costed as zero.  See
            # _GRID_NOISE_KWH for the observation that made this necessary.
            "grid_noise_snapped_intervals": snapped,
            "grid_noise_note": (
                "a negative net import inside battery._TOL_KWH is a bound returned one ulp "
                "over its row, not an export: the kernel calls the plan feasible and the "
                "cost model would refuse any negative, so the orchestrator costs it as zero "
                "and counts it here. Nonzero means MILP float noise, NOT energy exported"
                if snapped
                else "no interval needed snapping: every net import was non-negative"
            ),
        }

    # ---- identity: every frozen input hashed -------------------------------
    version = code_version()
    identity = {
        "benchmark_version": BENCHMARK_VERSION,
        "store_schema_version": SCHEMA_VERSION,
        "dataset_id": loaded.dataset_id,
        "build_manifest_sha256": loaded.manifest_sha256,
        "config_hash": cfg_hash,
        "code_version": version,
        "policies": selected,
        "train_end": _iso(cut),
        "train_days": int(train_days),
        "charge_pct": charge_pct,
        "discharge_pct": discharge_pct,
        "allow_reconstructed_quality": allow_reconstructed_quality,
        "min_usable_fraction": min_usable_fraction,
        "time_limit_s": time_limit_s,
        "mip_rel_gap": mip_rel_gap,
        "mip_abs_gap": mip_abs_gap,
        "terminal_mode": terminal_mode,
        # Day-ahead knobs are always part of the identity: a run whose forecast window
        # or terminal SOC valuation differs is a different measurement, and the resolved
        # terminal value (not just "was it overridden") goes in so a re-run with the
        # same inputs reproduces the same number and the same id.
        "day_ahead_forecast_window_days": int(day_ahead_forecast_window_days),
        "day_ahead_time_limit_s": float(day_ahead_time_limit_s),
        "day_ahead_terminal_value_eur_per_kwh": (
            None if not day_ahead_block else day_ahead_block["terminal_value"]["eur_per_kwh"]
        ),
        "day_ahead_terminal_value_rule": (
            "cli"
            if day_ahead_terminal_value_eur_per_kwh is not None
            else TERMINAL_VALUE_DEFAULT_RULE
        ),
        "battery": assumptions.as_dict(),
    }
    run_id = _canonical_sha(identity)[:32]

    payload: dict[str, Any] = {
        "benchmark_version": BENCHMARK_VERSION,
        "run_id": run_id,
        "frozen_inputs": identity,
        "lineage": _lineage(loaded, cfg_path, cfg_hash, version),
        "evaluation": {
            "horizon_start": _iso(eval_starts[0]),
            "horizon_end": _iso(eval_ends[-1]),
            "interval_count": int(index.size),
            "interval_seconds": loaded.interval_seconds,
            "usable_interval_count": int(np.count_nonzero(eval_usable)),
            "excluded_interval_count": int(index.size) - int(np.count_nonzero(eval_usable)),
            "dataset_interval_count": n,
            "months_included": sorted({months[i] for i in range(n) if eval_mask[i]}),
            "window_is_partial_year": (eval_ends[-1] - eval_starts[0]) < timedelta(days=365),
            "scaling_note": (
                "every EUR figure is for THIS window only. Nothing is annualised, scaled to a "
                "year, or turned into a payback estimate"
            ),
        },
        "training": {
            "train_end": _iso(cut),
            "train_days": int(train_days),
            "window_start": _iso(train_window_start),
            "usable_interval_count": train_usable,
            "charge_pct": charge_pct,
            "discharge_pct": discharge_pct,
            "charge_threshold_eur_per_kwh": None if thresholds is None else thresholds[0],
            "discharge_threshold_eur_per_kwh": None if thresholds is None else thresholds[1],
            "thresholds_were_fitted": POLICY_CAUSAL_PRICE in selected,
            "frozen_before_evaluation": True,
            "leakage_note": (
                "thresholds were fitted on usable training prices only and frozen for the whole "
                "evaluation window; no evaluation price influenced them"
            ),
        },
        "quality": _quality_block(
            reasons,
            [reasons[i] for i in index],
            per_month,
            eval_months,
            min_usable_fraction,
            allow_reconstructed_quality,
        ),
        "assumptions": _assumption_block(assumptions, basis, solver_info),
        "solver": solver_info,
        "day_ahead": _day_ahead_payload(
            day_ahead_block, solver_info.get("day_ahead"), POLICY_DAY_AHEAD in selected
        ),
        "policies": records,
        "comparison": _comparison_block(records, basis, solver_info),
        "cost_consistency": consistency,
        "artifacts": {
            "results_schema": "BENCHMARK_RESULT",
            "results_columns": list(BENCHMARK_RESULT.names),
            "schedule_columns": list(SCHEDULE_SCHEMA.names),
            "extras_note": (
                "solver bounds, metered vs inventory cost, SOC in kWh, tolerances and the time "
                "limit are recorded here and in the wide schedules.parquet only: "
                "BENCHMARK_RESULT itself is unchanged, and so is Schedule.as_dict — the "
                "day-ahead policy's per-day telemetry joins the same precedent instead of "
                "widening the canonical schema"
            ),
        },
    }

    run_dir = loaded.directory / BENCHMARKS_DIRNAME / run_id
    write_part_atomic(
        run_dir / RESULTS_JSON_NAME,
        json.dumps(payload, indent=2, sort_keys=True, default=str).encode("utf-8"),
    )
    write_part_atomic(
        run_dir / RESULTS_PARQUET_NAME,
        _parquet_bytes(
            _results_table(
                records,
                dataset_id=loaded.dataset_id,
                start=eval_starts[0],
                end=eval_ends[-1],
                cost_basis=basis["cost_basis"],
            )
        ),
    )
    write_part_atomic(
        run_dir / SCHEDULES_PARQUET_NAME,
        _parquet_bytes(
            _schedules_table(
                run_id=run_id,
                dataset_id=loaded.dataset_id,
                starts=eval_starts,
                ends=eval_ends,
                days=[days[i] for i in index],
                usable=eval_usable,
                reasons=[reasons[i] for i in index],
                prices=eval_prices,
                load=gross[index],
                plans=plans,
                costs=costs,
            )
        ),
    )
    return run_dir
