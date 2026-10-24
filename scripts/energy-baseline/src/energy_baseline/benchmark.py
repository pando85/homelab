"""Benchmark orchestration: a validated dataset in, an immutable benchmark run out.

This module owns no physics and no cost arithmetic.  It selects intervals, freezes
parameters, calls the numeric core (battery / oracle / policies / cost) and serialises
exactly what came back.  See ``docs/adr/0003-phase1-measurement-and-baseline.md``
§"Policy design", §"Battery physical simulator", §"Strategy comparison".

Guarantees pinned here:

* **One interval set, five policies.** The usable mask is computed once and handed to
  every policy, so gap exclusions and the objective are identical across baseline,
  causal, day-ahead, rolling-horizon and oracle.  The causal policy may lose to the
  baseline; reported, not clipped.  So may the day-ahead and rolling-horizon policies —
  see the ordering note below.
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
from zoneinfo import ZoneInfo

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
POLICY_ROLLING_HORIZON = "rolling_horizon"
POLICY_ORACLE = "oracle"
#: The information ladder, in ascending order of what each policy is allowed to know:
#: measured history only → current price and past state → today's published prices plus
#: a causal load forecast → exact prices with replan events and partial execution → perfect
#: foresight of both.  ``all`` runs this whole set.
POLICY_NAMES: tuple[str, ...] = (
    POLICY_NO_BATTERY,
    POLICY_CAUSAL_PRICE,
    POLICY_DAY_AHEAD,
    POLICY_ROLLING_HORIZON,
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

#: What the monthly usable-fraction gate does with an evaluation month that falls below
#: the declared ``min_usable_fraction``.  ``"reject"`` (default) aborts the run before any
#: artifact is written; ``"report"`` keeps the month in the evaluation but marks it
#: not-accepted and flags the run incomplete.  There is deliberately NO ``"exclude"``: a
#: silent monthly exclusion would change the evaluation window without changing the
#: policy's measured cost basis, which is the dilution this whole gate exists to prevent.
QUALITY_POLICY_REJECT = "reject"
QUALITY_POLICY_REPORT = "report"
QUALITY_POLICIES: tuple[str, ...] = (QUALITY_POLICY_REJECT, QUALITY_POLICY_REPORT)

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

# --- rolling-horizon rung -------------------------------------------------
#
# The canonical defaults are **exact prices** and the **legacy two-event schedule**:
#
# * ``sigma=0`` / ``bias_sigma=0`` because the ladder is about *information*, not about
#   synthetic forecast error.  Every other rung sees exact prices, so a fifth rung that
#   started from a noisy estimate would not be comparable to them; the noise grid stays
#   an experiment reachable through ``--rolling-horizon-sigma`` /
#   ``--rolling-horizon-bias-sigma``.
# * ``replan_interval_slots=None`` (legacy 14:00 / 20:30), which is what the golden-value
#   tests in ``tests/test_rolling_horizon.py`` pin bit-for-bit.  ADR-0001's 15-minute
#   cadence (``--rolling-horizon-replan-interval-slots 1 --rolling-horizon-horizon-slots
#   96``) is reachable but NOT the default: it costs roughly 48x the solve count and
#   roughly 37x the wall time -- the two ratios differ because a fine-cadence solve is
#   cheaper than a legacy one -- and it has not been validated on real data.
DEFAULT_ROLLING_HORIZON_SIGMA = 0.0
DEFAULT_ROLLING_HORIZON_BIAS_SIGMA = 0.0
DEFAULT_ROLLING_HORIZON_SEED = 0

#: Per-solve limit for the rolling-horizon MILPs, in seconds **per solve** (like the
#: day-ahead's per-day limit, and unlike ``DEFAULT_TIME_LIMIT_S`` which is per whole
#: horizon).  Each rolling plan spans at most one local day plus a partial successor.
DEFAULT_ROLLING_HORIZON_TIME_LIMIT_S = DEFAULT_DAY_AHEAD_TIME_LIMIT_S

#: Horizon length in intervals when a cadence is given without an explicit horizon:
#: 24 h at 15-minute intervals, inside ADR-0001's 24-48 h band.  Same number
#: ``rolling_horizon.py`` uses; restated here because it goes into the run identity and
#: a recorded value must not depend on another module's private default.
DEFAULT_ROLLING_HORIZON_HORIZON_SLOTS = 96

#: Canonical default load forecast mode for the rolling-horizon rung.  ``causal`` uses
#: the SAME trailing-slot-mean function the day-ahead policy uses, so the two rungs
#: share a like-for-like load basis.  The frozen mode (profile fixed at the training
#: cut) is reachable through flags and is what the golden-value tests pin bit-for-bit.
DEFAULT_ROLLING_HORIZON_LOAD_FORECAST_MODE = "causal"

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
    # The rolling-horizon rung is a caller of the same core, never a reimplementation of
    # it: the benchmark only reaches the dispatcher and its frozen-profile builder, so
    # every physical constraint still comes from battery.py / oracle.py through that path.
    ("rolling_horizon", "run_rolling_horizon"),
    ("rolling_horizon", "build_frozen_load_profile"),
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
    run_rolling_horizon: Any
    build_frozen_load_profile: Any


def _modules() -> dict[str, Any]:
    from energy_baseline import battery, cost, oracle, policies, rolling_horizon

    return {
        "battery": battery,
        "cost": cost,
        "oracle": oracle,
        "policies": policies,
        "rolling_horizon": rolling_horizon,
    }


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
        run_rolling_horizon=modules["rolling_horizon"].run_rolling_horizon,
        build_frozen_load_profile=modules["rolling_horizon"].build_frozen_load_profile,
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


def _validate_rolling_horizon_knobs(
    *,
    time_limit_s: float,
    sigma: float,
    bias_sigma: float,
    replan_interval_slots: int | None,
    horizon_slots: int | None,
) -> None:
    """Refuse an impossible rolling-horizon configuration, one clean line each.

    Called from two places on purpose: by :func:`benchmark_dataset` before any dataset is
    loaded (so a typo in a flag costs nothing and cannot leave a half-written run), and by
    :func:`prepare_rolling_horizon` for direct callers.  One implementation, so the two can
    never disagree about what is legal.

    The rules mirror ``rolling_horizon.py``'s own guards rather than replacing them: a
    cadence and a horizon length must both be >= 1 when given, and neither sigma may be
    negative or non-finite (a negative noise scale is not "less noise", it is an inverted
    price curve).  ``horizon_slots`` WITHOUT a cadence is refused here too: the legacy
    two-event schedule takes its horizon from the local-day partition, so an explicit
    horizon would either be silently ignored or silently switch modes.
    """
    if time_limit_s <= 0 or not math.isfinite(float(time_limit_s)):
        raise BenchmarkInputError(
            f"rolling_horizon_time_limit_s must be > 0 and finite, got {time_limit_s}"
        )
    if not math.isfinite(sigma) or sigma < 0.0:
        raise BenchmarkInputError(f"rolling_horizon_sigma must be >= 0 and finite, got {sigma!r}")
    if not math.isfinite(bias_sigma) or bias_sigma < 0.0:
        raise BenchmarkInputError(
            f"rolling_horizon_bias_sigma must be >= 0 and finite, got {bias_sigma!r}"
        )
    if replan_interval_slots is not None and replan_interval_slots < 1:
        raise BenchmarkInputError(
            f"rolling_horizon_replan_interval_slots must be >= 1, got {replan_interval_slots}"
        )
    if horizon_slots is not None and horizon_slots < 1:
        raise BenchmarkInputError(
            f"rolling_horizon_horizon_slots must be >= 1, got {horizon_slots}"
        )
    if horizon_slots is not None and replan_interval_slots is None:
        raise BenchmarkInputError(
            "rolling_horizon_horizon_slots requires rolling_horizon_replan_interval_slots: "
            "the legacy two-event schedule derives its horizon from the local-day partition "
            "and does not accept an explicit horizon length"
        )


def prepare_rolling_horizon(
    *,
    core: _Core,
    prices: np.ndarray,
    load_kwh: np.ndarray,
    usable: np.ndarray,
    local_days: Sequence[Any],
    interval_starts_utc: Sequence[Any],
    interval_seconds: float,
    eval_start_idx: int,
    eval_end_idx: int,
    timezone: str,
    forecast_window_days: int,
    terminal_value_eur_per_kwh: float,
    terminal_value_source: str,
    time_limit_s: float,
    sigma: float,
    bias_sigma: float,
    seed: int,
    replan_interval_slots: int | None,
    horizon_slots: int | None,
    load_forecast_mode: str = DEFAULT_ROLLING_HORIZON_LOAD_FORECAST_MODE,
) -> tuple[RollingHorizonInputs, dict[str, Any]]:
    """Build the frozen load profile and resolve the planning terminal value.

    The rolling horizon operates on the full dataset arrays with absolute indices,
    because its local-day partition and the price-publication rule need whole local
    days.  The frozen load profile is built once from training data strictly before the
    cut, using the same trailing-window LENGTH as the day-ahead rung but a DIFFERENT
    BASIS: day_ahead re-learns its slot means from the trailing window before each
    dispatch day (``history_may_cross_train_end = True``), while this rung freezes the
    profile once at ``training_end_idx`` and reuses it for every solve.  The two rungs
    therefore differ in load knowledge as well as in price information and cadence; the
    frozen profile is not a like-for-like load basis for the comparison with day_ahead,
    and on the 2025 full-year ladder the asymmetry dominated.

    The planning terminal value is internal, causal, and derived from the training window
    (same rule as the day-ahead rung).  It is the only cross-event coupling: each solve
    values end-of-horizon SOC at this EUR/DC-kWh, and the per-event valuations telescope
    into one horizon-level adjustment.  The **scoring** terminal value is the shared 0.0
    (meter cost with zero inventory adjustment), so cost_eur == meter_cost_eur and the
    comparison with the oracle is on the same basis.

    Returns:
        ``(inputs, block)`` — ``inputs`` are the full-array inputs for
        :func:`energy_baseline.rolling_horizon.run_rolling_horizon`, and ``block`` is the
        artifact record: cadence, horizon, forecast method, terminal value provenance,
        and the information-set disclosure.
    """
    if forecast_window_days < 1:
        raise BenchmarkInputError(
            f"rolling_horizon_forecast_window_days must be >= 1, got {forecast_window_days}"
        )
    _validate_rolling_horizon_knobs(
        time_limit_s=time_limit_s,
        sigma=sigma,
        bias_sigma=bias_sigma,
        replan_interval_slots=replan_interval_slots,
        horizon_slots=horizon_slots,
    )
    if load_forecast_mode not in ("frozen", "causal"):
        raise BenchmarkInputError(
            f"rolling_horizon_load_forecast_mode must be 'frozen' or 'causal', "
            f"got {load_forecast_mode!r}"
        )

    # Build the load profile according to the selected mode.
    # Frozen: profile fixed at the training cut, reused by every solve.
    # Causal: a per-event provider inside run_rolling_horizon computes each event's
    # forecast from history strictly before that event's local-day start (conservative
    # day-ahead basis), so there is no single whole-array profile to precompute here.
    if load_forecast_mode == "causal":
        # frozen_load is a placeholder; the dispatcher ignores it in causal mode and
        # builds the event-specific CausalForecastProvider from the raw load arrays.
        frozen_load = np.zeros_like(load_kwh)
        forecast_meta = {
            "method": "trailing_slot_mean_causal_event_specific",
            "mode": "causal",
            "window_days": int(forecast_window_days),
            "training_end_idx": int(eval_start_idx),
            "shared_function_with_day_ahead": True,
            "data_cutoff_rule": (
                "each event's forecast uses only intervals strictly below the event's "
                "own local-day start (the conservative day-ahead basis).  No "
                "whole-array per-target-day profile is fitted, so a solve at 20:30 on "
                "day D whose horizon spans D+1 never reads day D's measured load into "
                "D+1's estimate; the next-day refresh happens only at the first event "
                "after that midnight"
            ),
            "note": (
                "Causal mode defers forecast construction to the dispatcher's "
                "CausalForecastProvider; per-event cutoff and support are recorded in "
                "the telemetry's forecast_metadata once the roll runs.  This block is "
                "the prepare-time contract, not a fitted profile."
            ),
        }
    else:
        # Frozen mode: precompute the profile at the training cut
        frozen_load, forecast_meta = core.build_frozen_load_profile(
            load_kwh,
            local_days,
            usable,
            training_end_idx=eval_start_idx,
            window_days=forecast_window_days,
            interval_starts_utc=interval_starts_utc,
            tz=ZoneInfo(timezone),
        )

    inputs = RollingHorizonInputs(
        prices=prices,
        load_kwh=load_kwh,
        usable=usable,
        local_days=local_days,
        interval_starts_utc=interval_starts_utc,
        eval_start_idx=eval_start_idx,
        eval_end_idx=eval_end_idx,
        timezone=timezone,
        interval_seconds=float(interval_seconds),
        frozen_load=frozen_load,
        forecast=forecast_meta,
        terminal_value_eur_per_kwh=terminal_value_eur_per_kwh,
        terminal_value_source=terminal_value_source,
        time_limit_s=time_limit_s,
        sigma=sigma,
        bias_sigma=bias_sigma,
        seed=seed,
        replan_interval_slots=replan_interval_slots,
        horizon_slots=horizon_slots,
        load_forecast_mode=load_forecast_mode,
        forecast_window_days=forecast_window_days,
    )

    cadence_text, horizon_text = _rh_cadence_text(
        replan_interval_slots, horizon_slots, interval_seconds
    )
    block: dict[str, Any] = {
        "cadence": {
            "description": cadence_text,
            "replan_interval_slots": replan_interval_slots,
            "horizon_slots": horizon_slots,
            "horizon_description": horizon_text,
            "note": (
                "the legacy two-event schedule is the default and what the golden-value tests "
                "pin bit-for-bit.  ADR-0001's 15-minute cadence (replan_interval_slots=1, "
                "horizon_slots=96) is reachable via flags but is NOT the default: it costs "
                "roughly 48x the solve count (~8,832 against ~185 over a 92-day holdout) and "
                "roughly 37x the wall time (~175 s against ~4.7 s per arm, synthetic fixtures).  "
                "The two ratios differ because a fine-cadence solve is cheaper: each legacy "
                "event plans the rest of its local day and the next one, while cadence 1 plans "
                "a fixed 96 slots.  It has not yet been validated on real data"
            ),
        },
        "forecast": {
            "method": forecast_meta.get("method", "trailing_slot_mean_frozen"),
            "window_days": int(forecast_window_days),
            "load_forecast_mode": load_forecast_mode,
            # In causal mode the basis IS shared with day_ahead (same function, same
            # information set).  In frozen mode it is NOT: the profile is fixed at the
            # training cut while day_ahead re-learns before each dispatch day.
            "shared_with_day_ahead": load_forecast_mode == "causal",
            "shared_note": (
                "causal mode: uses the SAME trailing-slot-mean function as day_ahead "
                "(policies.forecast_load_trailing_slot_mean), so the two rungs share a "
                "like-for-like load basis.  Each event's forecast uses only observations "
                "strictly before that event's local-day start, and that cutoff is held for "
                "the entire solve (no per-target-day refresh within the horizon)"
                if load_forecast_mode == "causal"
                else "window LENGTH is shared, forecast BASIS is not.  Both rungs use a "
                "28-day trailing slot mean, but day_ahead recomputes it from the trailing "
                "window before each dispatch day while this rung freezes the profile once "
                "at the training cut and reuses it for every solve.  A run whose "
                "evaluation window extrapolates far beyond that cut therefore gives this "
                "rung stale load knowledge, so the two rungs differ in load knowledge as "
                "well as in price information and cadence"
            ),
            "metadata": forecast_meta,
        },
        "terminal_value": {
            "eur_per_kwh": float(terminal_value_eur_per_kwh),
            "source": terminal_value_source,
            "role": "internal planning value: cross-event coupling of a myopic solver",
            "scoring_basis": (
                "the reported cost uses the shared scoring basis (0.0 EUR/DC-kWh terminal "
                "valuation), so cost_eur == meter_cost_eur with inventory adjustment 0, "
                "identical to the free oracle and the day-ahead rung"
            ),
        },
        "price_noise": {
            "sigma": float(sigma),
            "bias_sigma": float(bias_sigma),
            "seed": int(seed),
            "note": (
                "canonical defaults (sigma=0, bias_sigma=0) are exact prices: the ladder is "
                "about information, not synthetic forecast error.  Non-zero values are an "
                "experiment reachable through flags"
            ),
        },
        "solver": {
            "time_limit_s_per_solve": float(time_limit_s),
            "note": (
                "this time limit is per solve (one MILP per planning event), not per day and "
                "not per whole horizon; the whole-horizon oracle limit is a different knob and "
                "is recorded separately.  Events that consumed this limit are counted in the "
                "telemetry, because such an event makes the run depend on machine speed"
            ),
        },
        "information_set": {
            "prices": (
                "at 20:30 on D-1 the policy already sees day D's exact prices (published the "
                "evening before), which day_ahead does not see until D's midnight; the horizon "
                "spans to the end of D rather than stopping at it.  The policy is therefore "
                "more informed than day_ahead on prices"
            ),
            "load": (
                "an advancing trailing-window forecast using the SAME function as day_ahead "
                "(policies.forecast_load_trailing_slot_mean); each local day's forecast is "
                "recomputed from that day's own trailing window of strictly prior "
                "observations.  The two rungs therefore share a like-for-like load basis, "
                "and the only remaining difference is price information and cadence"
                if load_forecast_mode == "causal"
                else "a frozen trailing-window forecast built once from training data strictly "
                "before the cut; the same forecast is reused by every solve, so the policy "
                "does not see day D's own measured load.  The policy is therefore LESS "
                "informed than day_ahead on load: day_ahead re-learns from the trailing "
                "window before each dispatch day while this rung's profile is fixed at the "
                "training cut.  The rung is more informed on prices and less informed on "
                "load, so it does not sit strictly between day_ahead and oracle in a single "
                "information ordering"
            ),
            "state": "own SOC carried forward from the previous event's actual execution",
            "assumptions_disclosed": [
                "the archive stores the price REALISED in each hour, not a snapshot of the "
                "sensor's price attribute dict as it stood at 20:30, so this policy assumes "
                "published-in-advance equals realised-and-archived.  True by construction for "
                "day-ahead PVPC, but not measured here",
                "the policy's usability mask (which intervals the archive trusts) is read for "
                "the whole horizon.  It is a data-quality statement, not economic foresight, and "
                "is taken as shared because ADR-0003 requires gap exclusions to be identical "
                "across policies",
            ],
        },
    }
    return inputs, block


def _rolling_horizon_payload(
    run_block: dict[str, Any],
    telemetry: dict[str, Any] | None,
    ran: bool,
) -> dict[str, Any]:
    """The rolling-horizon artifact: cadence, forecast, terminal value, per-event solves.

    Recorded here and in the wide ``schedules.parquet`` only — ``BENCHMARK_RESULT`` and
    :meth:`energy_baseline.battery.Schedule.as_dict` are deliberately untouched.  One
    entry per planning event is the point of the block: a rolling optimiser is judged
    event by event, and the multiplicity (how many overlapping horizons covered each
    interval) is what makes clipping totals incomparable across cadences.
    """
    if not ran or not run_block:
        return {
            "ran": False,
            "note": "rolling_horizon was not one of the selected policies for this run",
        }
    merged: dict[str, Any] = {**run_block, **(telemetry or {})}
    merged["ran"] = True
    merged["artifact_note"] = (
        "per_event carries one entry per planning event in the evaluation window: solve "
        "status and certification, the SOC it started and ended at, the energy the plan "
        "wanted and the clipping refused it, and any degradation to hold-SOC.  "
        "planning_multiplicity_* says how many overlapping horizons covered each interval, "
        "which is what makes clipping totals incomparable across cadences (an interval is "
        "planned by several events but executed once).  Nothing in here is aggregated into "
        "BENCHMARK_RESULT, which is unchanged, and Schedule.as_dict was deliberately not "
        "extended for it either — the same precedent the day-ahead rung followed"
    )
    merged["wall_clock_note"] = (
        "solver wall-clock time is deliberately NOT recorded.  Run artifacts are "
        "content-addressed and written no-clobber (energy_baseline.paths.write_part_atomic "
        "refuses the same run id with different bytes), so a timing field would make an "
        "identical re-run raise FileExistsError and destroy byte-level reproducibility.  "
        "Deterministic per-event telemetry (solver_status, is_certified, mip_gap), the "
        "solve_count, and the count of events that consumed their time limit are recorded "
        "instead; an event that consumed its limit is also a disclosure that that event's "
        "plan is machine-speed dependent"
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


def _rolling_horizon_record_extras(
    record: dict[str, Any], inputs: RollingHorizonInputs
) -> dict[str, Any]:
    """Fields a rolling-horizon plan needs that the oracle's record vocabulary does not carry.

    The solver time limit is **per solve** (one MILP per planning event), not per day
    and not per whole horizon.  The terminal mode is per solve too: each event runs with
    ``terminal_mode="free"`` and an internal planning value, the same framing the
    day-ahead rung uses.  The optimisation horizon is DST-dependent at the legacy cadence
    (the 14:00 solve spans ~34 h, the 20:30 solve ~27.5 h) and fixed at a uniform cadence
    (``horizon_slots`` intervals).  The reported cost is the sum of the executed slices
    against measured load, which is not the optimum of any horizon-length problem and
    claims no bound on one.
    """
    record["solver_time_limit_scope"] = "per_solve"
    record["terminal_mode_scope"] = "per_solve"
    cadence, horizon = _rh_cadence_text(
        inputs.replan_interval_slots, inputs.horizon_slots, inputs.interval_seconds
    )
    record["optimisation_horizon"] = (
        f"{cadence}, each solve planning {horizon} and executing only until the next event: "
        "the reported cost is the sum of executed slices against measured load, which is "
        "not the optimum of any horizon-length problem and claims no bound on one"
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
    solve_oracle: Any,
    terminal_mode: str,
    time_limit_s: float,
    rel: float,
    abs_: float,
    throughput_cost_eur_per_kwh: float = 0.0,
) -> tuple[dict[str, Any], str]:
    """Build the solver kwargs, choosing free terminal mode only if the core has it.

    ``throughput_cost_eur_per_kwh`` (default ``0.0``) is propagated to ``solve_oracle``
    only when the core exposes the parameter, so a core that predates point5 stays
    callable.  ``0.0`` never changes a plan, so the ladder is bit-identical when the
    knob is unused.
    """
    if terminal_mode not in ("free", "fixed"):
        raise BenchmarkInputError(f"terminal_mode must be 'free' or 'fixed', got {terminal_mode!r}")
    kwargs: dict[str, Any] = {"time_limit_s": time_limit_s, "mip_rel_gap": rel, "mip_abs_gap": abs_}
    try:
        params = inspect.signature(solve_oracle).parameters
    except (TypeError, ValueError):  # pragma: no cover - exotic callables
        params = {}
    if "throughput_cost_eur_per_kwh" in params:
        kwargs["throughput_cost_eur_per_kwh"] = float(throughput_cost_eur_per_kwh)
    if terminal_mode != "free":
        return kwargs, "fixed_default"
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


@dataclass(frozen=True)
class RollingHorizonInputs:
    """What the rolling-horizon policy needs beyond the shared spec and horizon.

    Unlike the other policies, the rolling horizon operates on the **full** dataset
    arrays with absolute ``eval_start_idx`` / ``eval_end_idx`` indices: its local-day
    partition, the price-publication rule and the frozen load profile all need whole
    local days, not a compacted eval slice.  The returned schedule is traced over the
    eval window only, so it aligns with the plans table the other policies produce.

    Built by :func:`prepare_rolling_horizon`; ``None`` whenever the policy was not
    selected, so a run without it pays nothing for the frozen profile or the noise.
    """

    prices: np.ndarray
    load_kwh: np.ndarray
    usable: np.ndarray
    local_days: Sequence[Any]
    interval_starts_utc: Sequence[Any]
    eval_start_idx: int
    eval_end_idx: int
    timezone: str
    interval_seconds: float
    frozen_load: np.ndarray
    forecast: dict[str, Any]
    terminal_value_eur_per_kwh: float
    terminal_value_source: str
    time_limit_s: float
    sigma: float
    bias_sigma: float
    seed: int
    replan_interval_slots: int | None
    horizon_slots: int | None
    load_forecast_mode: str = "frozen"
    throughput_cost_eur_per_kwh: float = 0.0
    forecast_window_days: int = 28


def _rh_cadence_text(
    replan_interval_slots: int | None, horizon_slots: int | None, interval_seconds: float
) -> tuple[str, str]:
    """Human-readable ``(cadence, horizon)`` description of a rolling-horizon schedule.

    Both the artifact block and the policy record describe the run in the same words, so
    they cannot drift apart.  ``interval_seconds`` is the dataset's own grid length: the
    nominal horizon in hours must be derived from it rather than from a hardcoded
    quarter-hour, or a dataset on a different grid would be described wrongly.
    """
    cadence = (
        "legacy two-event schedule (14:00 and 20:30 local)"
        if replan_interval_slots is None
        else f"every {int(replan_interval_slots)} interval(s)"
    )
    hours = horizon_slots * float(interval_seconds) / 3600.0 if horizon_slots is not None else 0.0
    horizon = (
        "DST-dependent (~34 h at 14:00, ~27.5 h at 20:30), clipped by the eval window end"
        if horizon_slots is None
        else f"{int(horizon_slots)} intervals ({hours:.1f} h nominal)"
    )
    return cadence, horizon


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
    rolling_horizon: RollingHorizonInputs | None = None,
    throughput_cost_eur_per_kwh: float = 0.0,
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
        core.solve_oracle,
        terminal_mode,
        time_limit_s,
        mip_rel_gap,
        mip_abs_gap,
        throughput_cost_eur_per_kwh=throughput_cost_eur_per_kwh,
    )
    if POLICY_DAY_AHEAD in selected and day_ahead is None:
        raise BenchmarkInputError(
            "day_ahead was selected but no per-day inputs were prepared — the caller must "
            "partition the evaluation window into local days and build its causal load "
            "forecast first (see prepare_day_ahead)"
        )
    if POLICY_ROLLING_HORIZON in selected and rolling_horizon is None:
        raise BenchmarkInputError(
            "rolling_horizon was selected but no inputs were prepared — the caller must "
            "build its frozen load profile and resolve the planning terminal value first "
            "(see prepare_rolling_horizon)"
        )
    # The shared scoring basis: in free mode the oracle values terminal SOC at 0.0, so the
    # reported cost is pure metered cash.  In fixed mode the oracle pins terminal SOC to the
    # initial value, and the scoring value is also 0.0 (no valuation).  The day-ahead and
    # rolling-horizon rungs use this same 0.0 scoring basis, so cost_eur == meter_cost_eur
    # with inventory adjustment 0 for all four solver-based policies.
    scoring_value = oracle_kwargs.get("terminal_value_eur_per_kwh", 0.0)
    if POLICY_ROLLING_HORIZON in selected and scoring_value != 0.0:
        # The shared scoring basis is 0.0 in all supported terminal modes.  If a future
        # change introduces a non-zero scoring value, the rolling-horizon rung must be
        # extended in rolling_horizon.py (which owns the physics) to apply the inventory
        # adjustment, not here.  Refusing loudly keeps the invariant provable.
        raise BenchmarkInputError(
            f"the shared scoring basis is {scoring_value} EUR/DC-kWh, but the rolling-horizon "
            "rung scores at 0.0 (meter cost with zero inventory adjustment).  Extending the "
            "shared scoring basis to non-zero values belongs in rolling_horizon.py, not here"
        )

    records: dict[str, dict[str, Any]] = {}
    errors: dict[str, Any] = {}
    plans: dict[str, dict[str, np.ndarray]] = {}
    telemetry_by_policy: dict[str, Any] = {}
    caught = _core_exceptions()

    for name in selected:
        if name == POLICY_ORACLE:
            used = terminal_mode_used
        elif name in (POLICY_DAY_AHEAD, POLICY_ROLLING_HORIZON):
            # Per-solve free mode with a declared internal planning value: same accounting
            # framing as the free oracle, which is what makes the numbers comparable.
            used = "free"
        else:
            used = "not_applicable"
        if name == POLICY_DAY_AHEAD and day_ahead:
            day_limit = day_ahead.time_limit_s
        elif name == POLICY_ROLLING_HORIZON and rolling_horizon:
            day_limit = rolling_horizon.time_limit_s
        else:
            day_limit = time_limit_s
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
                sched, telemetry_by_policy[name] = core.policy_day_ahead(
                    spec,
                    horizon,
                    day_ahead.days,
                    terminal_value_eur_per_kwh=day_ahead.terminal_value_eur_per_kwh,
                    scoring_terminal_value_eur_per_kwh=scoring_value,
                    time_limit_s=day_ahead.time_limit_s,
                    mip_rel_gap=mip_rel_gap,
                    mip_abs_gap=mip_abs_gap,
                    throughput_cost_eur_per_kwh=throughput_cost_eur_per_kwh,
                )
            elif name == POLICY_ROLLING_HORIZON:
                if rolling_horizon is None:  # pragma: no cover - guarded above
                    raise BenchmarkInputError("rolling_horizon selected without inputs")
                # The rolling horizon operates on the full dataset arrays with absolute
                # indices, because its local-day partition and the price-publication rule
                # need whole local days.  The returned schedule is traced over the eval
                # window only, so it aligns with the plans table.
                result = core.run_rolling_horizon(
                    spec,
                    prices=rolling_horizon.prices,
                    load_kwh=rolling_horizon.load_kwh,
                    usable=rolling_horizon.usable,
                    interval_seconds=interval_seconds,
                    local_days=rolling_horizon.local_days,
                    interval_starts_utc=rolling_horizon.interval_starts_utc,
                    eval_start_idx=rolling_horizon.eval_start_idx,
                    eval_end_idx=rolling_horizon.eval_end_idx,
                    tz=ZoneInfo(rolling_horizon.timezone),
                    frozen_load=rolling_horizon.frozen_load,
                    terminal_value_eur_per_kwh=rolling_horizon.terminal_value_eur_per_kwh,
                    sigma=rolling_horizon.sigma,
                    bias_sigma=rolling_horizon.bias_sigma,
                    seed=rolling_horizon.seed,
                    time_limit_s=rolling_horizon.time_limit_s,
                    mip_rel_gap=mip_rel_gap,
                    mip_abs_gap=mip_abs_gap,
                    import_limit_kw=assumptions.import_limit_kw,
                    replan_interval_slots=rolling_horizon.replan_interval_slots,
                    horizon_slots=rolling_horizon.horizon_slots,
                    load_forecast_mode=rolling_horizon.load_forecast_mode,
                    throughput_cost_eur_per_kwh=throughput_cost_eur_per_kwh,
                    forecast_window_days=rolling_horizon.forecast_window_days,
                )
                # The returned schedule comes from simulate() with solver_status="not_applicable"
                # (a traced plan).  But the rolling horizon IS a solver-based policy (many MILP
                # solves), so we relabel it "feasible" to match day_ahead's vocabulary: a plan
                # that is physically valid but not a proven optimum of the problem being scored.
                # This is bookkeeping, not physics: the cost/flows/SOC are unchanged.
                sched = dataclasses.replace(
                    result.schedule,
                    solver_status="feasible",
                    bound_kind="none",
                    is_certified=False,
                    solver_gap=None,
                    solver_time_limit_s=float(rolling_horizon.time_limit_s),
                )
                # The telemetry carries solve_count, multiplicity, per-event detail, noise
                # diagnostics, and the frozen-profile forecast metadata.  The forecast_metadata
                # field is filled by the caller (us), so we merge it here.
                rh_telemetry = dict(result.telemetry)
                rh_telemetry["forecast_metadata"] = rolling_horizon.forecast
                rh_telemetry["noise_metadata"] = result.noise_metadata
                # In causal mode the dispatcher built a real per-event provider; surface its
                # runtime metadata (per-event cutoff, training content hash, usable support,
                # zero-support count, per-target-day history) so a reader can audit leakage
                # against the ACTUAL window fitted, not just the prepare-time contract.
                if result.forecast_metadata:
                    rh_telemetry["causal_runtime_metadata"] = result.forecast_metadata
                telemetry_by_policy[name] = rh_telemetry
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
            if name in (POLICY_ORACLE, POLICY_DAY_AHEAD, POLICY_ROLLING_HORIZON)
            else "not_applicable",
            terminal_mode_used=used,
        )
        if name == POLICY_DAY_AHEAD:
            _day_ahead_record_extras(record)
        elif name == POLICY_ROLLING_HORIZON:
            _rolling_horizon_record_extras(record, rolling_horizon)
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
        solver_info["day_ahead"] = telemetry_by_policy.get(POLICY_DAY_AHEAD)
    if POLICY_ROLLING_HORIZON in selected:
        solver_info["rolling_horizon"] = telemetry_by_policy.get(POLICY_ROLLING_HORIZON)
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
            "rolling_horizon": _rolling_horizon_terminal_note(solver_info),
            "policies_share_a_scoring_basis_differ_in_planning": (
                "the oracle, the day-ahead policy, and the rolling-horizon policy are SCORED "
                "on the same terminal-SOC basis (scoring_terminal_value_eur_per_kwh, default "
                "0.0), so the reported cost_eur figures are comparable. They differ only in "
                "the internal planning value V, used inside each solve's objective to stop the "
                "myopic solve from emptying the battery at its horizon end (ADR-0001). V does "
                "not touch the reported cost, the inventory adjustment, or the oracle "
                "comparison; it lives in each solve's objective and is recorded separately as "
                "day_ahead.internal_daily_objective_sum_eur or rolling_horizon's per-event "
                "telemetry. Both figures are meter cost plus their own inventory adjustment, "
                "and each adjustment is reported per policy "
                "(policies.<name>.inventory_adjustment_eur), so a reader can always see how "
                "much of a claimed saving is stored energy rather than cash"
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


def _rolling_horizon_terminal_note(solver_info: dict[str, Any]) -> dict[str, Any]:
    """Per-solve terminal SOC valuation of the rolling-horizon policy, or why there is none."""
    telemetry = solver_info.get("rolling_horizon") or {}
    if not telemetry:
        return {
            "ran": False,
            "note": (
                "rolling_horizon was not part of this run, so no per-solve terminal valuation "
                "applies"
            ),
        }
    return {
        "ran": True,
        "scope": (
            "rolling_horizon (one solve per planning event, SOC rolled forward between events)"
        ),
        "terminal_mode_used": "free",
        "internal_planning_value_eur_per_kwh": telemetry.get("terminal_value_eur_per_kwh"),
        "scoring_value_eur_per_kwh": 0.0,
        "closed_cycle": False,
        "role": (
            "the internal per-solve planning value: end-of-plan SOC is credited at this "
            "EUR/DC-kWh value inside each solve's objective, and the per-event valuations "
            "telescope into one horizon-level adjustment because each event's end SOC is the "
            "next event's start SOC. This is NOT the common benchmark scoring value; see "
            "scoring_value_eur_per_kwh"
        ),
        "scoring_role": (
            "the common benchmark scoring value (0.0 EUR/DC-kWh), used for the reported "
            "Schedule.cost_eur and inventory_adjustment_eur. This is the same value the "
            "oracle uses, so the comparison is on the same basis. The internal planning "
            "value V is used only inside each solve's objective to prevent the battery from "
            "emptying at the end of its plan"
        ),
        "note": (
            "the rolling-horizon policy has two terminal values, not one. The internal "
            "planning value (V) credits end-of-plan SOC inside each solve's objective so the "
            "myopic solve does not empty the battery at its horizon end. The scoring value is "
            "what the reported Schedule.cost_eur and inventory_adjustment_eur use, and it is "
            "the SAME value the oracle uses so the comparison is on the same basis: "
            "cost_eur == meter_cost_eur with inventory adjustment 0. V is a frozen "
            "training-window statistic (or an explicit caller override); the scoring value is "
            "the run-wide basis and is not a knob to tune"
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
    # --- rolling-horizon rung -------------------------------------------------
    rolling_cost = records.get(POLICY_ROLLING_HORIZON, {}).get("cost_eur")
    out["rolling_horizon_may_lose_day_ahead"] = True
    out["rolling_horizon_may_lose_causal"] = True
    out["rolling_horizon_may_lose_baseline"] = True
    out["oracle_dominates_rolling_horizon"] = (
        None if oracle_cost is None or rolling_cost is None else oracle_cost <= rolling_cost + _EPS
    )
    # What the fifth rung bought: positive means the rolling-horizon information set came
    # out cheaper than day_ahead.  Negative is a real, allowed, informative answer, not an
    # error to fix.
    out["rolling_horizon_minus_day_ahead_eur"] = (
        None
        if rolling_cost is None or day_ahead_cost is None
        else float(rolling_cost) - float(day_ahead_cost)
    )
    out["rolling_horizon_minus_causal_eur"] = (
        None
        if rolling_cost is None or causal_cost is None
        else float(rolling_cost) - float(causal_cost)
    )
    out["rolling_horizon_closes_gap_fraction"] = (
        None
        if rolling_cost is None
        or causal_cost is None
        or oracle_cost is None
        or (causal_cost - oracle_cost) <= 0
        else float((causal_cost - rolling_cost) / (causal_cost - oracle_cost))
    )
    oracle_dominates_rolling_guaranteed = bool(
        oracle_cost is not None
        and rolling_cost is not None
        and records.get(POLICY_ORACLE, {}).get("is_certified")
        and solver_info["oracle_terminal_mode_used"] == "free"
    )
    out["oracle_dominates_rolling_horizon_guaranteed"] = oracle_dominates_rolling_guaranteed
    out["oracle_dominates_rolling_horizon_guaranteed_note"] = (
        "True only when the oracle is certified optimal AND its terminal mode is 'free'. "
        "A free oracle minimises over exactly the feasible set that contains the rolling-horizon "
        "plan (same SOC floor max(soc_min, initial), same physics, same endpoint freedom), "
        "so oracle.cost_eur <= rolling_horizon.cost_eur by construction. When terminal_mode is "
        "'fixed' the oracle pins terminal SOC to the initial value and its feasible set may "
        "exclude the rolling-horizon plan's actual final SOC, which leaves the plan outside "
        "the oracle's optimisation problem: no dominance is provable, even if the empirical "
        "comparison happens to hold. Do not assert fixed dominance."
    )
    # The rolling-horizon rung is more informed than day_ahead on PRICES and less informed on
    # LOAD, so it does not sit "strictly between" day_ahead and oracle in a single ordering.
    # Prices: at 20:30 on D-1 it already sees day D's exact prices, which day_ahead does not see
    # until D's midnight, and its horizon spans to the end of D rather than stopping at it.
    # Load: its profile is frozen once at the training cut, while day_ahead re-learns from the
    # trailing window before every dispatch day.  Over an evaluation window that extrapolates far
    # beyond the cut that asymmetry dominates, and it is measured, not hypothetical: on the 2025
    # full-year ladder this rung cost MORE than day_ahead.  Re-running the identical Q4-2025
    # holdout on the same 0.80 dataset reproduced the win, so the efficiency correction is not
    # the cause -- the frozen profile's distance from the evaluation window is.
    # Empirically rolling_horizon <= day_ahead held in 81 of 81 offline configurations, but all
    # 81 used a holdout immediately following the training cut.  That is strong evidence within
    # its own configuration and NOT a proof, and it does not transfer to longer windows.  We
    # record the observation with its evidence and its known counterexample, and assert nothing.
    out["rolling_horizon_vs_day_ahead_observation"] = {
        "empirical_dominance": (
            "rolling_horizon was cheaper than or equal to day_ahead in 81 of 81 offline "
            "configurations tested, all of them a Q4 holdout immediately following the "
            "training cut"
        ),
        "evidence_strength": (
            "strong empirical evidence within that configuration, not a mathematical proof, "
            "and not transferable to evaluation windows that extrapolate far beyond the cut"
        ),
        "measured_counterexample": (
            "on the 2025 full-year ladder this rung cost more than day_ahead.  Re-running the "
            "identical Q4-2025 window on the same dataset reproduced the win, which rules out "
            "the round-trip efficiency correction as the cause and leaves the forecast basis: "
            "this rung's load profile is frozen at the training cut (28 days of December 2024 "
            "applied to all of 2025) while day_ahead re-learns before each dispatch day"
        ),
        "why_not_guaranteed": (
            "a longer horizon with an imperfect estimate can lose: the rolling horizon sees "
            "more price information but plans further ahead, and a forecast error over a longer "
            "horizon can outweigh the information advantage.  The guarantee would require proving "
            "that the information gain always dominates the forecast-error risk, which is not "
            "available.  We report the measured difference and the empirical observation, but "
            "make no assertion about its sign"
        ),
    }
    out["dominance_note"] = (
        "the oracle's feasible set contains every other plan only under this run's "
        f"accounting (terminal_mode_used={solver_info['oracle_terminal_mode_used']}, SOC "
        "floored at the initial value, zero terminal inventory value). The day-ahead plan "
        "and the rolling-horizon plan are additionally feasible points of the oracle's own "
        "problem — their clipped, measured-load trajectories respect the SOC window, the "
        "no-export rule, the power limit, the charging-side ceiling headroom and one binary "
        "mode per interval, their SOC floor is exactly the horizon-wide causal floor "
        "max(soc_min, initial SOC) on every day (seed protection belongs to the window, "
        "not to the day), and their terminal valuations telescope to exactly the one the "
        "oracle applies — so oracle <= day_ahead and oracle <= rolling_horizon are guaranteed "
        "WHENEVER the oracle is a certified optimum AND terminal_mode_used is 'free' (same "
        "endpoint). On a time-limited incumbent the oracle reports its own best feasible "
        "cost, which comparison is empirical, hence the separate "
        "oracle_dominates_day_ahead_guaranteed and oracle_dominates_rolling_horizon_guaranteed "
        "flags. When terminal_mode_used is 'fixed', the oracle pins terminal SOC to initial, "
        "which may exclude the day-ahead or rolling-horizon final SOC, so the guarantee does "
        "not hold. Day-ahead versus causal is guaranteed in NEITHER direction: a myopic "
        "optimiser with a poor load forecast and a badly chosen terminal value can lose to a "
        "dumb threshold rule, so the difference is reported as day_ahead_minus_causal_eur and "
        "no assertion is made about its sign. Rolling-horizon versus day-ahead is also not "
        "guaranteed: empirically rolling_horizon <= day_ahead held in 81 of 81 offline "
        "configurations, all of them a Q4 holdout immediately following the training cut, "
        "which is strong evidence within that configuration but not a proof and not "
        "transferable to evaluation windows that extrapolate far beyond the cut; on the "
        "2025 full-year ladder the same rung cost MORE than day_ahead, and the identified "
        "asymmetry sufficient to explain the reversal is the load-forecast basis: this "
        "rung's profile is frozen at the training cut while day_ahead re-learns before "
        "each dispatch day (re-running the identical Q4-2025 window on the same dataset "
        "reproduced the win, which rules out the efficiency correction as the cause). "
        "The difference is reported as "
        "rolling_horizon_minus_day_ahead_eur with an observation note and no guarantee flag. "
        "A violated dominance claim is reported, not clipped: it is an accounting signal, not "
        "a solver bug. The causal policy may lose to the baseline and that loss is reported "
        "at face value"
    )
    return out


# ---------------------------------------------------------------------------
# Monthly usable-fraction quality gate (delegated to artifact_integrity)
# ---------------------------------------------------------------------------


def _expected_intervals_for_month(month: str, interval_seconds: float) -> int:
    """Full-calendar-month interval count at this dataset's grid, for partial detection.

    A month whose evaluation coverage is below this number is partial — the window cuts
    it — and the gate declares that explicitly instead of treating a few in-window days
    as a whole-month sample.  DST stretches or shrinks a day by at most one hour (4 slots
    at a quarter-hour grid), which is far too small to flip the partial flag, so the
    calendar-day count is the honest expectation.
    """
    import calendar

    year_text, month_text = month.split("-")
    days = calendar.monthrange(int(year_text), int(month_text))[1]
    return int(round(days * 86400.0 / float(interval_seconds)))


def _run_monthly_quality_gate(
    *,
    per_month: dict[str, dict[str, int]],
    threshold: float,
    policy: str,
    eval_months: set[str],
    interval_seconds: float,
) -> dict[str, Any]:
    """Evaluate the monthly gate over **evaluation intervals only** and return a record.

    The denominators are the intervals that are actually in the evaluation window, never
    the whole-dataset month totals: a month that straddles the train/eval cut would
    otherwise be diluted by its training half, which is exactly the bias that let a
    low-coverage first month pass an absolute threshold it had not earned.  The general
    full-dataset quality table is unaffected — this helper reads only the eval-only buckets
    the caller built.

    ``threshold == 0`` disables the gate (the legacy inert default): nothing is assessed,
    ``enabled`` is reported False, and the artifact still carries the measured fractions so
    a reader can judge coverage by eye.  When the gate is active it validates the threshold
    (finite, ``0 < t <= 1``, never a bool) — the helper refuses the rest.

    Under ``reject`` a month below threshold raises
    :class:`energy_baseline.artifact_integrity.MonthlyQualityGateError` before any artifact
    is written; under ``report`` the month is retained, marked not-accepted, and the run is
    flagged incomplete.  There is no exclude path (see :data:`QUALITY_POLICIES`).
    """
    from energy_baseline.artifact_integrity import (
        monthly_usable_fraction_gate,
    )

    enabled = threshold > 0.0
    if not enabled:
        return {
            "enabled": False,
            "threshold": float(threshold),
            "policy": policy,
            "denominator": "evaluation_intervals_only",
            "accepted": None,
            "incomplete": False,
            "months": [],
            "below_threshold_months": [],
            "note": (
                "min_usable_fraction is 0.0, so the gate is DISABLED: no month is assessed "
                "or rejected. The measured eval-only usable fractions are still reported in "
                "quality.per_month for the reader to judge; disabling the gate is the legacy "
                "default, not an endorsement of the data"
            ),
        }

    expected = {
        month: _expected_intervals_for_month(month, interval_seconds) for month in eval_months
    }
    result = monthly_usable_fraction_gate(
        per_month,
        threshold,
        policy=policy,
        eval_months=eval_months,
        expected_intervals=expected,
    )

    months_out = [
        {
            "month": mf.month,
            "eval_total": mf.total,
            "eval_usable": mf.usable,
            "usable_fraction": mf.fraction,
            "expected_intervals": mf.expected_intervals,
            "coverage": mf.coverage,
            "partial_calendar": mf.coverage is not None and mf.coverage < 0.99,
            "below_threshold": mf.fraction < threshold,
            "not_accepted": mf.fraction < threshold,
        }
        for mf in result.per_month
    ]
    below = [m["month"] for m in months_out if m["below_threshold"]]
    accepted = not below
    return {
        "enabled": True,
        "threshold": float(threshold),
        "policy": policy,
        "denominator": "evaluation_intervals_only",
        "accepted": accepted,
        # incomplete is only ever True under report: reject aborts before this block is
        # ever reached, so a run that got this far and has below-threshold months kept them.
        "incomplete": bool(below) and policy == QUALITY_POLICY_REPORT,
        "months": months_out,
        "below_threshold_months": below,
        "note": (
            "the gate is computed over EVALUATION intervals only, so a month straddling the "
            "train/eval cut is judged on the part the policy actually dispatched, not on a "
            "whole-calendar-month total that training data would dilute. Under the report "
            "policy a below-threshold month is retained but marked not-accepted and the run "
            "is flagged incomplete; under reject the run aborts before any artifact is written"
            if policy == QUALITY_POLICY_REPORT
            else "the gate is computed over EVALUATION intervals only, so a month straddling "
            "the train/eval cut is judged on the part the policy actually dispatched. A "
            "below-threshold evaluation month aborts the run before any artifact is written"
        ),
    }


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
    quality_policy: str = "reject",
    allow_reconstructed_quality: bool = False,
    time_limit_s: float = DEFAULT_TIME_LIMIT_S,
    mip_rel_gap: float = DEFAULT_MIP_REL_GAP,
    mip_abs_gap: float = DEFAULT_MIP_ABS_GAP,
    terminal_mode: str = "free",
    day_ahead_forecast_window_days: int = DEFAULT_FORECAST_WINDOW_DAYS,
    day_ahead_terminal_value_eur_per_kwh: float | None = None,
    day_ahead_time_limit_s: float = DEFAULT_DAY_AHEAD_TIME_LIMIT_S,
    rolling_horizon_replan_interval_slots: int | None = None,
    rolling_horizon_horizon_slots: int | None = None,
    rolling_horizon_sigma: float = DEFAULT_ROLLING_HORIZON_SIGMA,
    rolling_horizon_bias_sigma: float = DEFAULT_ROLLING_HORIZON_BIAS_SIGMA,
    rolling_horizon_seed: int = DEFAULT_ROLLING_HORIZON_SEED,
    rolling_horizon_time_limit_s: float = DEFAULT_ROLLING_HORIZON_TIME_LIMIT_S,
    rolling_horizon_load_forecast_mode: str = DEFAULT_ROLLING_HORIZON_LOAD_FORECAST_MODE,
    throughput_cost_eur_per_kwh: float = 0.0,
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
            ``causal_price``, ``day_ahead``, ``rolling_horizon``, ``oracle``.
        train_days: how far back before *train_end* the thresholds may look.
        charge_pct / discharge_pct: percentiles of the TRAINING prices, frozen after
            fitting; no evaluation price ever influences them.
        initial_soc_frac: starting SOC as a fraction of nameplate.  Omitted means
            tariff.yaml's battery.initial_soc_frac, then soc_window_min — the run
            records which source was used.
        import_limit_kw: overrides ``battery.import_limit_kw``; ``None`` with a null
            config leaves the site import ceiling unapplied and reports that.
        min_usable_fraction: declared monthly quality threshold, evaluated over EVALUATION
            intervals only (a train/eval-straddling month is judged on the dispatched part,
            not the whole-calendar total).  ``0.0`` (default) DISABLES the gate — the legacy
            inert behaviour; nothing is assessed or rejected, the measured fractions are
            still reported.  A positive threshold activates the gate and must be finite, in
            ``(0, 1]``, and never a bool.  The gate runs before any artifact is written.
        quality_policy: what the gate does with an evaluation month below ``min_usable_fraction``
            when the gate is active.  ``"reject"`` (default) aborts the run before any
            artifact is written; ``"report"`` retains the month, marks it not-accepted, and
            flags the run ``incomplete``.  There is no silent exclude — an excluded month
            would move the evaluation window without moving the measured-cost basis, the
            exact dilution the gate prevents.  Hashed into the run id either way.
        allow_reconstructed_quality: admit reconstructed values: forward-filled
            prices and partially gapped energy totals.  Allocation boundaries
            (``estimated_boundary``) are admitted by default and are NOT gated by
            this flag.  ``unknown_gap``/``gap_total_unknown`` allocation and
            ``missing``/``ambiguous`` prices are never admitted, with or without
            the flag.
        time_limit_s / mip_rel_gap / mip_abs_gap: passed to the solver and recorded;
            certification means "within tolerance", not "zero gap".  ``time_limit_s`` is
            the whole-horizon oracle's limit; the day-ahead policy has its own per-day
            limit (``day_ahead_time_limit_s``) and the rolling-horizon policy has its own
            per-solve limit (``rolling_horizon_time_limit_s``) because the two numbers
            answer different questions and a single knob would make one of them meaningless.
        terminal_mode: ``"free"`` (default) or ``"fixed"`` (closed cycle), for the
            oracle.  The day-ahead and rolling-horizon policies are always per-solve free
            with a declared terminal value — that is what makes them myopic rather than
            smaller oracles.
        day_ahead_forecast_window_days: trailing window, in local days, of the causal
            load forecast (``>= 1``).  Recorded, along with the fallback rule it uses
            when a slot has no prior sample.  The rolling-horizon rung uses the same
            forecast window LENGTH, but its BASIS is different: this rung freezes the
            profile once at the training cut while day_ahead re-learns before each
            dispatch day, so the two rungs differ in load knowledge as well as in price
            information and cadence.
        day_ahead_terminal_value_eur_per_kwh: EUR/DC-kWh value on end-of-day SOC — the
            day-ahead policy's only cross-day coupling.  ``None`` (default) derives it
            from the frozen training window (:data:`TERMINAL_VALUE_DEFAULT_RULE`); an
            explicit number is allowed but is recorded with ``cli`` provenance so a
            hand-picked valuation is never mistaken for a measured one.  ``0.0`` is a
            legal, documented choice: a pure cash-flow objective whose plan empties the
            battery by midnight.  The rolling-horizon rung reuses the same resolved value
            (same rule, same provenance) so the two rungs differ only in information and
            cadence.
        day_ahead_time_limit_s: per-local-day solver limit for the day-ahead rolling solves.
            Days that consumed it are counted in the artifact, because such a day makes the
            run depend on machine speed.
        rolling_horizon_replan_interval_slots: how often to re-solve (in intervals).
            ``None`` (default) is the legacy two-event schedule (14:00 / 20:30 local).
            A positive integer is a uniform cadence (e.g. 1 for 15-minute replanning).
            The legacy schedule is the default and what the golden-value tests pin
            bit-for-bit; ADR-0001's 15-minute cadence is reachable but NOT the default
            because it costs roughly 48x the solve count and roughly 37x the wall time
            (the ratios differ because a fine-cadence solve is cheaper than a legacy
            one) and has not yet been validated on real data.
        rolling_horizon_horizon_slots: how far ahead each solve plans (in intervals).
            ``None`` (default) means the legacy schedule derives its horizon from the
            local-day partition (~34 h at 14:00, ~27.5 h at 20:30).  A positive integer
            is required when ``rolling_horizon_replan_interval_slots`` is set (e.g. 96
            for 24 h at 15-min intervals).  Passing ``rolling_horizon_horizon_slots``
            without ``rolling_horizon_replan_interval_slots`` is refused: the legacy
            schedule does not accept an explicit horizon length.
        rolling_horizon_sigma: per-slot price noise standard deviation (``>= 0``).
            Default 0.0 is exact prices: the ladder is about information, not synthetic
            forecast error.  Non-zero values are an experiment reachable through flags.
        rolling_horizon_bias_sigma: per-local-day multiplicative bias standard deviation
            (``>= 0``).  Default 0.0 is exact prices.  Non-zero values are an experiment.
        rolling_horizon_seed: random seed for price noise generation.  Default 0.
        rolling_horizon_time_limit_s: per-solve solver limit for the rolling-horizon
            MILPs.  Events that consumed it are counted in the artifact, because such an
            event makes the run depend on machine speed.
        rolling_horizon_load_forecast_mode: ``"causal"`` (default) builds the
            rolling-horizon load forecast with the SAME advancing trailing-slot-mean
            function the day-ahead rung uses, so the two rungs share a like-for-like
            load basis and differ only in price information and cadence.  ``"frozen"``
            builds the legacy profile once from training data at the cut and reuses it
            for every solve — the mode the golden-value tests pin bit-for-bit.
        throughput_cost_eur_per_kwh: per-kWh AC-throughput penalty added to the
            oracle, day-ahead and rolling-horizon planning objectives on a shared
            basis (default ``0.0``).  ``0.0`` leaves every plan bit-identical to a run
            without the argument; it is a planning-only term and never enters the
            reported (true-price) cost.  Recorded in the run identity.
        data_dir: data root, when the manifest lists parts relative to it.

    Returns:
        the run directory ``<dataset>/benchmarks/<run_id>`` holding
        ``benchmark.json``, ``results.parquet`` and ``schedules.parquet``.

    Raises:
        BenchmarkInputError: unusable inputs — unverified dataset, unconfirmed
            configuration, bad train split, month below threshold, empty evaluation,
            a day-ahead or rolling-horizon partition or terminal valuation that cannot
            be justified.
        BenchmarkError: the numeric core is unavailable, or no policy produced a cost.
    """
    selected = _parse_policies(policies)
    if isinstance(min_usable_fraction, bool) or not isinstance(min_usable_fraction, (int, float)):
        raise BenchmarkInputError(
            f"min_usable_fraction must be a number, got "
            f"{type(min_usable_fraction).__name__}: {min_usable_fraction!r}"
        )
    if not math.isfinite(float(min_usable_fraction)) or not 0.0 <= min_usable_fraction <= 1.0:
        raise BenchmarkInputError(
            f"min_usable_fraction must be finite and in [0, 1], got {min_usable_fraction}"
        )
    if quality_policy not in QUALITY_POLICIES:
        raise BenchmarkInputError(
            f"quality_policy must be one of {list(QUALITY_POLICIES)}, got {quality_policy!r}"
        )
    if time_limit_s <= 0:
        raise BenchmarkInputError(f"time_limit_s must be > 0, got {time_limit_s}")
    if mip_rel_gap <= 0 or mip_abs_gap < 0:
        raise BenchmarkInputError(
            f"mip_rel_gap must be > 0 and mip_abs_gap >= 0, got {mip_rel_gap}/{mip_abs_gap}"
        )
    if train_days < 1:
        raise BenchmarkInputError(f"train_days must be >= 1, got {train_days}")
    if not math.isfinite(throughput_cost_eur_per_kwh) or throughput_cost_eur_per_kwh < 0.0:
        raise BenchmarkInputError(
            "throughput_cost_eur_per_kwh must be finite and >= 0, got "
            f"{throughput_cost_eur_per_kwh!r}"
        )
    if POLICY_DAY_AHEAD in selected or POLICY_ROLLING_HORIZON in selected:
        # Only checked when at least one policy is actually run: an unrelated run
        # must not be refused over a knob it never uses.
        if day_ahead_forecast_window_days < 1:
            raise BenchmarkInputError(
                f"day_ahead_forecast_window_days must be >= 1, got {day_ahead_forecast_window_days}"
            )
    if POLICY_DAY_AHEAD in selected:
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
    if POLICY_ROLLING_HORIZON in selected:
        _validate_rolling_horizon_knobs(
            time_limit_s=rolling_horizon_time_limit_s,
            sigma=rolling_horizon_sigma,
            bias_sigma=rolling_horizon_bias_sigma,
            replan_interval_slots=rolling_horizon_replan_interval_slots,
            horizon_slots=rolling_horizon_horizon_slots,
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
    # General whole-dataset quality table (unchanged; for reporting coverage by eye).
    per_month: dict[str, dict[str, Any]] = {}
    for i, month in enumerate(months):
        bucket = per_month.setdefault(month, {"total": 0, "usable": 0, "usable_fraction": 0.0})
        bucket["total"] += 1
        bucket["usable"] += int(bool(usable[i]))
    eval_months = {months[i] for i in range(n) if eval_mask[i]}
    for bucket in per_month.values():
        bucket["usable_fraction"] = bucket["usable"] / bucket["total"] if bucket["total"] else 0.0

    # Gate denominators are EVALUATION intervals only, never whole-dataset month totals:
    # a month straddling the train/eval cut is judged on the part actually dispatched, so
    # training data cannot dilute a low-coverage first month past an absolute threshold.
    eval_per_month: dict[str, dict[str, int]] = {}
    for i in range(n):
        if not eval_mask[i]:
            continue
        month = months[i]
        bucket = eval_per_month.setdefault(month, {"total": 0, "usable": 0})
        bucket["total"] += 1
        bucket["usable"] += int(bool(usable[i]))

    # The gate runs BEFORE any artifact is written, so a reject aborts cheaply and a
    # half-written run is impossible.
    gate_block = _run_monthly_quality_gate(
        per_month=eval_per_month,
        threshold=float(min_usable_fraction),
        policy=quality_policy,
        eval_months=eval_months,
        interval_seconds=loaded.interval_seconds,
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

    # ---- rolling-horizon: frozen profile, planning terminal value, cadence ------
    rolling_horizon: RollingHorizonInputs | None = None
    rolling_horizon_block: dict[str, Any] = {}
    if POLICY_ROLLING_HORIZON in selected:
        # The rolling-horizon rung reuses the day-ahead terminal-value rule (training-window
        # mean price) so the two rungs differ only in information and cadence, not in the
        # cross-event coupling.  The resolved value is recorded with its own provenance.
        rh_terminal_value, rh_terminal_value_source = resolve_day_ahead_terminal_value(
            configured=day_ahead_terminal_value_eur_per_kwh,
            train_prices=prices[train_mask & usable],
            train_window_start=train_window_start,
            train_end=cut,
        )
        # The eval window must be contiguous for run_rolling_horizon's absolute-index contract.
        # eval_mask = s >= cut on a chronologically sorted grid → contiguous by construction.
        # Verify defensively: a non-contiguous eval window would silently mis-index.
        if index.size > 0 and (index[-1] - index[0] + 1) != index.size:
            raise BenchmarkInputError(
                "the evaluation window is not contiguous in the dataset: the rolling-horizon "
                "rung operates on the full dataset arrays with absolute indices, which requires "
                "a contiguous eval window.  This is a dataset ordering bug"
            )
        eval_start_idx = int(index[0]) if index.size > 0 else 0
        eval_end_idx = int(index[-1]) + 1 if index.size > 0 else 0
        # A cadence without an explicit horizon means "plan one nominal day ahead".  The
        # same number rolling_horizon.py would fall back to is resolved HERE so the run
        # identity and the artifact record the horizon actually used, rather than a null
        # that a later reader has to go and re-derive from another module's private default.
        rh_horizon_slots = (
            DEFAULT_ROLLING_HORIZON_HORIZON_SLOTS
            if rolling_horizon_replan_interval_slots is not None
            and rolling_horizon_horizon_slots is None
            else rolling_horizon_horizon_slots
        )
        rolling_horizon, rolling_horizon_block = prepare_rolling_horizon(
            core=core,
            prices=prices,
            load_kwh=gross,
            usable=usable,
            local_days=days,
            interval_starts_utc=starts,
            interval_seconds=loaded.interval_seconds,
            eval_start_idx=eval_start_idx,
            eval_end_idx=eval_end_idx,
            timezone=config.tariff_periods.timezone,
            forecast_window_days=day_ahead_forecast_window_days,
            terminal_value_eur_per_kwh=rh_terminal_value,
            terminal_value_source=rh_terminal_value_source,
            time_limit_s=rolling_horizon_time_limit_s,
            sigma=rolling_horizon_sigma,
            bias_sigma=rolling_horizon_bias_sigma,
            seed=rolling_horizon_seed,
            replan_interval_slots=rolling_horizon_replan_interval_slots,
            horizon_slots=rh_horizon_slots,
            load_forecast_mode=rolling_horizon_load_forecast_mode,
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
        rolling_horizon=rolling_horizon,
        throughput_cost_eur_per_kwh=throughput_cost_eur_per_kwh,
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
        # The policy only changes behaviour when the gate is enabled (threshold > 0), but
        # it is hashed unconditionally so a reject run and a report run are never the same
        # id. The gate's eval-only status (accepted/incomplete) is derived from data the id
        # already covers, so it is recorded in the payload, not folded into the id.
        "quality_policy": quality_policy,
        "time_limit_s": time_limit_s,
        "mip_rel_gap": mip_rel_gap,
        "mip_abs_gap": mip_abs_gap,
        "terminal_mode": terminal_mode,
        # The throughput penalty is a shared planning basis across the oracle, day-ahead
        # and rolling-horizon rungs, so it belongs in the base identity (like the
        # day-ahead knobs, which are always hashed even when day_ahead is not selected):
        # a run whose throughput penalty differs is a different measurement regardless of
        # which solver rungs ran.  ``0.0`` is the bit-identical default.
        "throughput_cost_eur_per_kwh": float(throughput_cost_eur_per_kwh),
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
    # Rolling-horizon knobs are added to the identity ONLY when the policy is selected,
    # so a four-policy run's identity is unchanged by this edit (the selected policy set
    # is already hashed, so `all` legitimately yields a new run id).  This is a deliberate
    # deviation from the day-ahead precedent (whose knobs are always hashed): the brief
    # explicitly requires that a four-policy run's identity be unchanged, which is only
    # possible if new knobs are conditional.  Note that `code_version` is also part of the
    # identity, so any commit changes every run id anyway; what's preserved here is that
    # no new knob enters a non-rolling run's hashed params.
    if POLICY_ROLLING_HORIZON in selected:
        identity["rolling_horizon_replan_interval_slots"] = rolling_horizon_replan_interval_slots
        identity["rolling_horizon_horizon_slots"] = rolling_horizon_horizon_slots
        identity["rolling_horizon_sigma"] = float(rolling_horizon_sigma)
        identity["rolling_horizon_bias_sigma"] = float(rolling_horizon_bias_sigma)
        identity["rolling_horizon_seed"] = int(rolling_horizon_seed)
        identity["rolling_horizon_time_limit_s"] = float(rolling_horizon_time_limit_s)
        identity["rolling_horizon_load_forecast_mode"] = rolling_horizon_load_forecast_mode
        identity["rolling_horizon_terminal_value_eur_per_kwh"] = (
            None
            if not rolling_horizon_block
            else rolling_horizon_block["terminal_value"]["eur_per_kwh"]
        )
        identity["rolling_horizon_terminal_value_rule"] = (
            "cli"
            if day_ahead_terminal_value_eur_per_kwh is not None
            else TERMINAL_VALUE_DEFAULT_RULE
        )
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
        "monthly_quality_gate": gate_block,
        "assumptions": _assumption_block(assumptions, basis, solver_info),
        "solver": solver_info,
        "day_ahead": _day_ahead_payload(
            day_ahead_block, solver_info.get("day_ahead"), POLICY_DAY_AHEAD in selected
        ),
        "rolling_horizon": _rolling_horizon_payload(
            rolling_horizon_block,
            solver_info.get("rolling_horizon"),
            POLICY_ROLLING_HORIZON in selected,
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
                "day-ahead and rolling-horizon policies' per-event telemetry join the same "
                "precedent instead of widening the canonical schema"
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

    # The outputs manifest is written LAST, after all three artifacts exist on disk, so
    # it hashes exactly what a reader will find. finalize_run_outputs is idempotent on
    # identical content, so re-running the same benchmark does not collide. The manifest
    # itself is not in its own required set (no self-reference).
    from energy_baseline.artifact_integrity import (
        BENCHMARK_REQUIRED_FILES,
        finalize_run_outputs,
    )

    finalize_run_outputs(run_dir, BENCHMARK_REQUIRED_FILES)
    return run_dir
