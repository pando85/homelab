"""Benchmark orchestration: a validated dataset in, an immutable benchmark run out.

This module owns no physics and no cost arithmetic.  It selects intervals, freezes
parameters, calls the numeric core (battery / oracle / policies / cost) and serialises
exactly what came back.  See ``docs/adr/0003-phase1-measurement-and-baseline.md``
§"Policy design", §"Battery physical simulator", §"Strategy comparison".

Guarantees pinned here:

* **One interval set, three policies.** The usable mask is computed once and handed to
  every policy, so gap exclusions and the objective are identical across baseline,
  causal and oracle.  The causal policy may lose to the baseline; reported, not clipped.
* **Training before evaluation.** Causal thresholds are fitted on usable intervals
  before ``train_end``, frozen, and hashed into the run id.
* **Free terminal accounting.** The oracle is asked for ``terminal_mode="free"`` with
  ``terminal_value_eur_per_kwh=0.0``: SOC stays at or above its initial value, the
  ending SOC is free, and the objective is pure metered cash.  This is *not* a closed
  cycle and the run records that; if the core has no free mode, the fixed-cycle
  default is used and ``terminal_mode_used`` says so.
* **Honest bounds.** An uncertified incumbent is an upper bound on optimal cost, never
  an "oracle optimum"; a missing dual bound stays unknown.  Status, gap, certification,
  configured tolerances and the time limit are all serialised.
* **Loud assumptions.** ``tariff.yaml`` nulls are never defaulted away;
  ``round_trip_efficiency`` maps to ``sqrt(RTE)`` on both legs and is labelled as a
  symmetric assumption; idle losses are zero unless measured; an unconfirmed import
  ceiling is passed as ``None`` (unbounded) and reported.

Outputs, via :func:`energy_baseline.paths.write_part_atomic` (no-clobber, ``0600``,
private ``0700`` tree)::

    <dataset>/benchmarks/<run_id>/benchmark.json    full detail, all extras
    <dataset>/benchmarks/<run_id>/results.parquet   BENCHMARK_RESULT, exactly
    <dataset>/benchmarks/<run_id>/schedules.parquet per-interval plans (wide)

Bounds, SOC in kWh, the metered/inventory split, tolerances and time limits live in
``benchmark.json`` and the wide schedules file: ``BENCHMARK_RESULT`` is unchanged.
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
POLICY_ORACLE = "oracle"
POLICY_NAMES: tuple[str, ...] = (POLICY_NO_BATTERY, POLICY_CAUSAL_PRICE, POLICY_ORACLE)

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
    ("policies", "fit_thresholds"),
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
    fit_thresholds: Any
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
        fit_thresholds=modules["policies"].fit_thresholds,
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

    records: dict[str, dict[str, Any]] = {}
    errors: dict[str, Any] = {}
    plans: dict[str, dict[str, np.ndarray]] = {}
    caught = _core_exceptions()

    for name in selected:
        used = "not_applicable" if name != POLICY_ORACLE else terminal_mode_used
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
            time_limit_s=time_limit_s,
            mip_rel_gap=mip_rel_gap,
            mip_abs_gap=mip_abs_gap,
            terminal_mode_requested=terminal_mode if name == POLICY_ORACLE else "not_applicable",
            terminal_mode_used=used,
        )
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
    return records, solver_info, plans


def _interval_costs(
    cost_fn: Any,
    grid: np.ndarray,
    prices: np.ndarray,
    usable: np.ndarray,
    multiplier: float | None,
    *,
    record_taxed: bool,
) -> tuple[list[float], list[float]]:
    """Per-interval cost on both bases, computed by the core cost model.

    Excluded intervals come back as NaN: their energy and price are not knowable, so
    they contribute nothing rather than something invented.  The taxed column stays
    NaN unless a taxed basis was actually established, so no reader can mistake a
    pretax figure for a bill.
    """
    pretax: list[float] = []
    taxed: list[float] = []
    for i in range(grid.size):
        if not usable[i]:
            pretax.append(math.nan)
            taxed.append(math.nan)
            continue
        energy, price = float(grid[i]), float(prices[i])
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
    return pretax, taxed


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
        },
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
    out["oracle_dominates_baseline"] = (
        None if oracle_cost is None or baseline is None else oracle_cost <= baseline + _EPS
    )
    out["oracle_dominates_causal"] = (
        None if oracle_cost is None or causal_cost is None else oracle_cost <= causal_cost + _EPS
    )
    out["dominance_note"] = (
        "the oracle's feasible set contains both other plans only under this run's accounting "
        f"(terminal_mode_used={solver_info['oracle_terminal_mode_used']}, SOC floored at the "
        "initial value, zero terminal inventory value). A violated dominance claim is reported, "
        "not clipped: it is an accounting signal, not a solver bug. The causal policy may lose to "
        "the baseline and that loss is reported at face value"
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
            ``causal_price``, ``oracle``.
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
            certification means "within tolerance", not "zero gap".
        terminal_mode: ``"free"`` (default) or ``"fixed"`` (closed cycle).
        data_dir: data root, when the manifest lists parts relative to it.

    Returns:
        the run directory ``<dataset>/benchmarks/<run_id>`` holding
        ``benchmark.json``, ``results.parquet`` and ``schedules.parquet``.

    Raises:
        BenchmarkInputError: unusable inputs — unverified dataset, unconfirmed
            configuration, bad train split, month below threshold, empty evaluation.
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
        pretax, taxed = _interval_costs(
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
                "BENCHMARK_RESULT itself is unchanged"
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
