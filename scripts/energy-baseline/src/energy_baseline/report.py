"""Monthly reconciliation report and DuckDB SQL inspection.

This module reads the artifacts produced by :mod:`energy_baseline.benchmark` and
turns them into a human-readable monthly report, plus a small, safe SQL surface
for ad-hoc inspection of the dataset.

The report is a pure function of the run: given the same ``benchmark.json``,
``results.parquet`` and ``schedules.parquet``, it writes the same bytes.  No
timestamps, no wall-clock, no guess at an annual figure.  The report is placed
inside the run directory so the run is a single self-contained artifact.

The SQL surface is read-only and sandboxed:

* the connection is in-memory; the analysis Parquet is loaded into Arrow and
  registered as a view — the database never touches the filesystem on its own;
* ``enable_external_access=false`` and ``disabled_filesystems='LocalFileSystem'``
  block ``read_parquet``, ``read_csv``, ``ATTACH``, ``COPY``, ``glob``, and any
  other file-system operation;
* ``autoinstall_known_extensions`` / ``autoload_known_extensions`` /
  ``allow_persistent_secrets`` are off, so no extension or secret can be pulled
  in by the query;
* the statement is parsed by DuckDB's own parser (``duckdb.extract_statements``)
  and rejected unless it is a single ``SELECT`` or ``WITH ... SELECT`` (an
  ``EXPLAIN SELECT`` is also allowed, for plan inspection).  ``COPY``,
  ``ATTACH``, ``INSTALL``, ``LOAD``, ``SET``, ``CREATE``, ``INSERT``, ``UPDATE``,
  ``DELETE``, ``DROP``, ``PRAGMA``, ``CALL`` and any multi-statement input are
  refused at the grammar level, before the engine ever sees them.

The curated queries are the safe default for users who do not want to write SQL.
``inspect_sql`` is the escape hatch for users who do, with the guard above.
"""

from __future__ import annotations

import json
import re
from collections.abc import Sequence
from dataclasses import dataclass
from datetime import date, datetime
from decimal import Decimal
from pathlib import Path
from typing import Any
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

import duckdb
import numpy as np
import pyarrow as pa
import pyarrow.parquet as pq

from energy_baseline.artifact_integrity import (
    BENCHMARK_MANIFEST_NAME,
    BENCHMARK_REQUIRED_FILES,
    REPORT_MANIFEST_NAME,
    REPORT_REQUIRED_FILES,
    ArtifactIntegrityError,
    finalize_run_outputs,
    verify_run_outputs,
)
from energy_baseline.benchmark import (
    BENCHMARK_VERSION,
    BENCHMARKS_DIRNAME,
    RESULTS_JSON_NAME,
    RESULTS_PARQUET_NAME,
    SCHEDULES_PARQUET_NAME,
    list_datasets,
    load_dataset,
)
from energy_baseline.manifest import config_hash
from energy_baseline.paths import resolve_data_dir, write_part_atomic
from energy_baseline.schemas import SCHEMA_VERSION

REPORT_JSON_NAME = "report.json"
REPORT_MD_NAME = "report.md"

#: Curated read-only queries over the registered ``intervals`` view.  Each is a
#: single ``SELECT`` or ``WITH ... SELECT``; they are the safe default for users
#: who do not want to write SQL.
CURATED_QUERIES: dict[str, str] = {
    "monthly_cost": """
        WITH base AS (
            SELECT
                strftime(local_day, '%Y-%m') AS month,
                SUM(CASE WHEN usable THEN 1 ELSE 0 END) AS usable_count,
                SUM(CASE WHEN usable THEN price_eur_per_kwh * grid_kwh ELSE 0 END)
                    AS meter_cost_eur,
                SUM(CASE WHEN usable THEN charge_kwh ELSE 0 END) AS ac_charge_kwh,
                SUM(CASE WHEN usable THEN discharge_kwh ELSE 0 END) AS ac_discharge_kwh
            FROM benchmark_schedules
            WHERE policy = 'oracle'
            GROUP BY month
        )
        SELECT * FROM base ORDER BY month
    """,
    "price_distribution": """
        SELECT
            COUNT(*) AS interval_count,
            COUNT(price_eur_per_kwh) AS priced_count,
            MIN(price_eur_per_kwh) AS price_min,
            APPROX_QUANTILE(price_eur_per_kwh, 0.5) AS price_p50,
            APPROX_QUANTILE(price_eur_per_kwh, 0.9) AS price_p90,
            APPROX_QUANTILE(price_eur_per_kwh, 0.99) AS price_p99,
            MAX(price_eur_per_kwh) AS price_max,
            AVG(price_eur_per_kwh) AS price_mean
        FROM intervals
        WHERE price_eur_per_kwh IS NOT NULL
    """,
    "quality_summary": """
        SELECT
            energy_quality,
            allocation_quality,
            price_quality,
            COUNT(*) AS interval_count
        FROM intervals
        GROUP BY energy_quality, allocation_quality, price_quality
        ORDER BY interval_count DESC
    """,
    "excluded_intervals": """
        SELECT
            exclusion_reason,
            COUNT(*) AS count
        FROM benchmark_schedules
        WHERE NOT usable
        GROUP BY exclusion_reason
        ORDER BY count DESC
    """,
    "benchmark_summary": """
        SELECT
            policy,
            cost_eur,
            cost_basis,
            efc,
            throughput_kwh,
            solver_status,
            bound_kind,
            is_certified
        FROM benchmark_results
    """,
    "dataset_meta": """
        SELECT * FROM dataset_meta
    """,
}


class ReportError(RuntimeError):
    """The report could not be generated (missing run, config mismatch, ...)."""


class ReportInputError(ValueError):
    """The caller's inputs are unusable (ambiguous dataset, bad benchmark path)."""


class SQLInputError(ValueError):
    """The SQL is not a single read-only statement."""


# ---------------------------------------------------------------------------
# Run resolution
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class _Run:
    directory: Path
    payload: dict[str, Any]
    results: pa.Table
    schedules: pa.Table
    benchmark_verified: bool = False


def _read_run(run_dir: Path, *, verify_integrity: bool = True) -> _Run:
    """Read a benchmark run from disk.  Every file is required.

    When *verify_integrity* is True (the default) and a benchmark manifest
    sidecar exists, the run's artifacts are verified against it before
    reading.  A tampered manifest raises :class:`ReportError`.  When no
    manifest is present (legacy run), the run is read without verification
    — this is reported as unverified provenance, not silently treated as
    verified.
    """
    if not run_dir.is_dir():
        raise ReportInputError(f"benchmark run directory does not exist: {run_dir}")
    for name in (RESULTS_JSON_NAME, RESULTS_PARQUET_NAME, SCHEDULES_PARQUET_NAME):
        if not (run_dir / name).is_file():
            raise ReportInputError(f"run {run_dir.name} is missing {name}")

    # Verify the benchmark manifest if it exists.
    manifest_path = run_dir / BENCHMARK_MANIFEST_NAME
    benchmark_verified = False
    if verify_integrity and manifest_path.is_file():
        try:
            result = verify_run_outputs(
                run_dir,
                required_files=BENCHMARK_REQUIRED_FILES,
                manifest_name=BENCHMARK_MANIFEST_NAME,
            )
        except ArtifactIntegrityError as exc:
            raise ReportError(
                f"benchmark manifest verification failed for run {run_dir.name}: {exc}"
            ) from exc
        if not result.ok:
            details: list[str] = []
            if result.hash_mismatches:
                details.append(f"hash mismatches: {result.hash_mismatches}")
            if result.size_mismatches:
                details.append(f"size mismatches: {result.size_mismatches}")
            if result.missing_required:
                details.append(f"missing required: {result.missing_required}")
            raise ReportError(
                f"benchmark manifest verification failed for run {run_dir.name}: "
                + "; ".join(details)
            )
        benchmark_verified = True

    payload = json.loads((run_dir / RESULTS_JSON_NAME).read_bytes().decode("utf-8"))
    results = pq.read_table(run_dir / RESULTS_PARQUET_NAME)
    schedules = pq.read_table(run_dir / SCHEDULES_PARQUET_NAME)

    return _Run(
        directory=run_dir,
        payload=payload,
        results=results,
        schedules=schedules,
        benchmark_verified=benchmark_verified,
    )


def _resolve_run(
    *,
    data_dir: Path | None,
    dataset: Path | str | None,
    benchmark_path: Path | str | None,
) -> _Run:
    """Find the run to report on, with no implicit "latest" globbing.

    * ``benchmark_path`` wins if given — explicit, unambiguous.
    * Otherwise ``dataset`` must resolve to exactly one run under its
      ``benchmarks/`` directory; zero or more-than-one is an error listing the
      available run ids so the caller can pick one.
    * Otherwise ``data_dir`` must resolve to exactly one dataset directory, which
      must then resolve to exactly one run.  Zero or more-than-one is again an
      error listing the options.
    """
    if benchmark_path is not None:
        run_dir = Path(benchmark_path).expanduser()
        if run_dir.is_file() and run_dir.name == RESULTS_JSON_NAME:
            run_dir = run_dir.parent
        return _read_run(run_dir)

    if dataset is not None:
        dataset_dir = Path(dataset).expanduser()
        runs_dir = dataset_dir / BENCHMARKS_DIRNAME
        if not runs_dir.is_dir():
            raise ReportInputError(
                f"dataset {dataset_dir} has no {BENCHMARKS_DIRNAME!r} directory — run "
                "the benchmark first"
            )
        candidates = sorted(child for child in runs_dir.iterdir() if child.is_dir())
        if not candidates:
            raise ReportInputError(
                f"dataset {dataset_dir} has no benchmark runs — run the benchmark first"
            )
        if len(candidates) > 1:
            raise ReportInputError(
                f"dataset {dataset_dir} has {len(candidates)} benchmark runs; pass one of "
                "them as benchmark_path to pick which one to report on: "
                + ", ".join(child.name for child in candidates)
            )
        return _read_run(candidates[0])

    if data_dir is None:  # pragma: no cover - generate_report resolves the default
        raise ReportInputError(
            "either benchmark_path, dataset or data_dir must be given — the report refuses "
            "to guess which run to report on"
        )
    datasets = list_datasets(data_dir)
    if not datasets:
        raise ReportInputError(
            f"data_dir {data_dir} has no built datasets — run the build pipeline first"
        )
    if len(datasets) > 1:
        raise ReportInputError(
            f"data_dir {data_dir} has {len(datasets)} datasets; pass one as dataset or a "
            "run as benchmark_path: " + ", ".join(d.name for d in datasets)
        )
    runs_dir = datasets[0] / BENCHMARKS_DIRNAME
    if not runs_dir.is_dir():
        raise ReportInputError(
            f"dataset {datasets[0]} has no {BENCHMARKS_DIRNAME!r} directory — run the "
            "benchmark first"
        )
    candidates = sorted(child for child in runs_dir.iterdir() if child.is_dir())
    if not candidates:
        raise ReportInputError(
            f"dataset {datasets[0]} has no benchmark runs — run the benchmark first"
        )
    if len(candidates) > 1:
        raise ReportInputError(
            f"dataset {datasets[0]} has {len(candidates)} benchmark runs; pass one of them "
            "as benchmark_path to pick which one to report on: "
            + ", ".join(child.name for child in candidates)
        )
    return _read_run(candidates[0])


# ---------------------------------------------------------------------------
# Monthly aggregation and distributions
# ---------------------------------------------------------------------------


def _terminal_inventory_adjustment(run: _Run, policy: str) -> float:
    """The whole-horizon terminal inventory adjustment attributed to one policy.

    The core reports ``inventory_adjustment_eur`` as a *horizon total* — with the
    free terminal mode at 0 EUR/kWh it is exactly 0.0.  It is not spread over
    months: the adjustment belongs to the instant the horizon ends, so it lands on
    the last month and nowhere else.
    """
    record = run.payload.get("policies", {}).get(policy, {})
    value = record.get("inventory_adjustment_eur")
    return float(value) if value is not None else 0.0


def _monthly_rows(run: _Run) -> list[dict[str, Any]]:
    """Per-month, per-policy totals from the schedules table.

    The month is taken from ``local_day`` (the local calendar day the interval
    belongs to), so a DST month keeps its 23- or 25-hour day whole.  Excluded
    intervals are counted, never costed.  ``meter_cost_eur`` is what would appear on
    the bill for that month; ``inventory_adjustment_eur`` is zero except in the last
    month of the horizon, where the terminal SOC accounting lands.
    """
    rows = run.schedules.to_pylist()
    by_month: dict[str, dict[str, dict[str, Any]]] = {}
    months_of: dict[str, list[str]] = {}
    for row in rows:
        month = f"{row['local_day'].year:04d}-{row['local_day'].month:02d}"
        policy = row["policy"]
        bucket = by_month.setdefault(month, {}).setdefault(
            policy,
            {
                "month": month,
                "policy": policy,
                "intervals_total": 0,
                "intervals_usable": 0,
                "intervals_excluded": 0,
                "meter_cost_eur": 0.0,
                "meter_cost_taxed_eur": 0.0,
                "inventory_adjustment_eur": 0.0,
                "ac_charge_kwh": 0.0,
                "ac_discharge_kwh": 0.0,
            },
        )
        months_of.setdefault(policy, [])
        if month not in months_of[policy]:
            months_of[policy].append(month)
        bucket["intervals_total"] += 1
        if not row["usable"]:
            bucket["intervals_excluded"] += 1
            continue
        bucket["intervals_usable"] += 1
        price = row["price_eur_per_kwh"]
        if price is not None:
            charge = row["charge_kwh"]
            discharge = row["discharge_kwh"]
            grid = row["grid_kwh"]
            if grid is not None:
                bucket["meter_cost_eur"] += float(grid) * float(price)
            taxed = row["interval_cost_taxed_eur"]
            if taxed is not None:
                bucket["meter_cost_taxed_eur"] += float(taxed)
            if charge is not None:
                bucket["ac_charge_kwh"] += float(charge)
            if discharge is not None:
                bucket["ac_discharge_kwh"] += float(discharge)

    out: list[dict[str, Any]] = []
    for month in sorted(by_month):
        for policy in sorted(by_month[month]):
            bucket = by_month[month][policy]
            # The terminal adjustment belongs to the last month of the horizon only.
            if month == max(months_of.get(policy, [month])):
                bucket["inventory_adjustment_eur"] = _terminal_inventory_adjustment(run, policy)
                bucket["carries_terminal_inventory_adjustment"] = True
            else:
                bucket["carries_terminal_inventory_adjustment"] = False
            out.append(bucket)
    return out


def _throughput(run: _Run) -> dict[str, Any]:
    """AC and DC throughput plus EFC per policy.

    The core's ``throughput_kwh`` is the **AC/metered** energy the battery delivered
    (the sum of ``discharge_kwh``) and its ``efc`` is **DC-referenced**.  Both sides
    are shown here, derived from the same per-interval plan and the one-way
    efficiencies this run resolved, so a reader cannot mistake an AC number for a DC
    one — they differ by exactly the loss on that leg.
    """
    battery = run.payload.get("assumptions", {}).get("battery", {})
    eta_c = battery.get("charge_efficiency") or 1.0
    eta_d = battery.get("discharge_efficiency") or 1.0
    capacity = battery.get("capacity_kwh") or 0.0

    totals: dict[str, dict[str, float]] = {}
    for row in run.schedules.to_pylist():
        if not row["usable"]:
            continue
        acc = totals.setdefault(
            row["policy"], {"ac_charge": 0.0, "ac_discharge": 0.0, "cycles": 0.0}
        )
        if row["charge_kwh"] is not None:
            acc["ac_charge"] += float(row["charge_kwh"])
        if row["discharge_kwh"] is not None:
            acc["ac_discharge"] += float(row["discharge_kwh"])
        acc["cycles"] += 1.0

    out: dict[str, Any] = {}
    for policy, record in run.payload.get("policies", {}).items():
        acc = totals.get(policy, {"ac_charge": 0.0, "ac_discharge": 0.0})
        out[policy] = {
            "ac_charge_kwh": acc["ac_charge"],
            "ac_discharge_kwh": acc["ac_discharge"],
            "dc_charge_kwh": acc["ac_charge"] * eta_c,
            "dc_discharge_kwh": acc["ac_discharge"] / eta_d,
            "core_throughput_kwh": record.get("throughput_kwh"),
            "core_throughput_basis": "AC/metered delivered energy (sum of discharge_kwh)",
            "efc": record.get("efc"),
            "efc_basis": "DC-referenced equivalent full cycles of the usable SOC window",
            "usable_intervals": int(acc.get("cycles", 0)),
            "soc_min_frac": record.get("soc_min_frac"),
            "soc_max_frac": record.get("soc_max_frac"),
            "initial_soc_kwh": record.get("initial_soc_kwh"),
            "terminal_soc_kwh": record.get("terminal_soc_kwh"),
            "one_way_efficiencies": {"charge": eta_c, "discharge": eta_d},
            "usable_window_kwh": (
                capacity
                * ((battery.get("soc_max_frac") or 0.0) - (battery.get("soc_min_frac") or 0.0))
            ),
            "note": (
                "ac_* are metered at the AC terminals (what the meter sees); dc_* are the same "
                "energy crossed the battery's own boundaries; efc is the core's DC-referenced "
                "cycle count. These are three different quantities and are not interchangeable"
            ),
        }
    return out


def _solver_bounds(run: _Run) -> dict[str, Any]:
    """Per-policy bound/tolerance/time-limit detail, kept out of results.parquet."""
    out: dict[str, Any] = {}
    for policy, record in run.payload.get("policies", {}).items():
        out[policy] = {
            "cost_eur": record.get("cost_eur"),
            "cost_upper_bound_eur": record.get("cost_upper_bound_eur"),
            "cost_lower_bound_eur": record.get("cost_lower_bound_eur"),
            "solver_status": record.get("solver_status"),
            "bound_kind": record.get("bound_kind"),
            "is_certified": record.get("is_certified"),
            "solver_gap": record.get("solver_gap"),
            "configured_mip_rel_gap": record.get("configured_mip_rel_gap"),
            "configured_mip_abs_gap": record.get("configured_mip_abs_gap"),
            "solver_time_limit_s": record.get("solver_time_limit_s"),
            "solver_message": record.get("solver_message"),
            "terminal_mode_used": record.get("terminal_mode_used"),
            "terminal_value_eur_per_kwh": record.get("terminal_value_eur_per_kwh"),
            "meter_cost_eur": record.get("meter_cost_eur"),
            "inventory_adjustment_eur": record.get("inventory_adjustment_eur"),
            "bound_notes": record.get("bound_notes", []),
        }
    errors = run.payload.get("solver", {}).get("errors", {})
    for policy, err in errors.items():
        out[policy] = {
            "error": err["error"],
            "failure_kind": err["failure_kind"],
            "note": err["note"],
        }
    return out


def _distributions(run: _Run) -> dict[str, Any]:
    """Per-policy, per-interval quantiles for cost, AC charge and AC discharge.

    Quantiles are taken over the usable intervals of that policy's own plan, so a
    policy that never discharges does not dilute another policy's distribution.
    """
    rows = run.schedules.to_pylist()
    series: dict[str, dict[str, list[float]]] = {}
    for row in rows:
        if not row["usable"]:
            continue
        buckets = series.setdefault(
            row["policy"],
            {"meter_cost_eur": [], "ac_charge_kwh": [], "ac_discharge_kwh": []},
        )
        price = row["price_eur_per_kwh"]
        grid = row["grid_kwh"]
        if price is not None and grid is not None:
            buckets["meter_cost_eur"].append(float(price) * float(grid))
        if row["charge_kwh"] is not None:
            buckets["ac_charge_kwh"].append(float(row["charge_kwh"]))
        if row["discharge_kwh"] is not None:
            buckets["ac_discharge_kwh"].append(float(row["discharge_kwh"]))

    def quantiles(values: list[float]) -> dict[str, float]:
        if not values:
            return {"count": 0}
        arr = np.asarray(values, dtype=float)
        return {
            "count": int(arr.size),
            "min": float(arr.min()),
            "p50": float(np.quantile(arr, 0.50)),
            "p90": float(np.quantile(arr, 0.90)),
            "p99": float(np.quantile(arr, 0.99)),
            "max": float(arr.max()),
            "mean": float(arr.mean()),
        }

    return {
        policy: {key: quantiles(values) for key, values in sorted(buckets.items())}
        for policy, buckets in sorted(series.items())
    }


def _terminal_note(run: _Run) -> str:
    """A one-line note about what the terminal SOC accounting did."""
    term = run.payload.get("assumptions", {}).get("terminal_accounting", {})
    used = term.get("used")
    if used == "free":
        return (
            "The oracle runs with terminal SOC free and valued at 0 EUR/kWh, so the horizon "
            "is NOT a closed cycle: energy still in the battery at the end is not counted. "
            "That is an explicit assumption of this run, not a property of the battery."
        )
    if used == "fixed_default":
        return (
            "The core did not expose a free terminal mode, so the fixed-cycle default was used: "
            "terminal SOC equals initial SOC, and the horizon IS a closed cycle."
        )
    return f"Terminal accounting: {used!r}."


def _horizon_scope_note(run: _Run) -> str:
    """Say which calendar the evaluation horizon is measured on.

    The package README records the rule: a report must state plainly that the horizon
    is UTC-scoped instead of letting ``Z``-suffixed labels imply it — a UTC year is not
    a local calendar year, because the local year turns before UTC midnight east of
    Greenwich.  Both bounds and the dataset's tariff timezone already live in the run
    payload, so the note is derived from them and names no year of its own.  The local
    rendering is the pinned zone name resolved through the system tzdata — the same
    dependency the build already has for ``local_day``, and the zone offset at these two
    instants is not one of the ambiguous ones.
    """
    evaluation = run.payload["evaluation"]
    start = str(evaluation["horizon_start"])
    end = str(evaluation["horizon_end"])
    timezone = (
        run.payload.get("lineage", {})
        .get("build_manifest_pinned_fields", {})
        .get("params", {})
        .get("timezone")
    )
    bounds: tuple[datetime, datetime] | None = None
    if timezone:
        try:
            zone = ZoneInfo(str(timezone))
            bounds = (
                datetime.fromisoformat(start).astimezone(zone),
                datetime.fromisoformat(end).astimezone(zone),
            )
        except (ValueError, TypeError, ZoneInfoNotFoundError):  # pragma: no cover
            bounds = None
    if bounds is None:  # pragma: no cover - payload without a pinned timezone
        return (
            f"Horizon scope: {start} → {end} are UTC instants, so the horizon above is "
            "UTC-scoped and is not claimed to be a calendar year in any local timezone; "
            "monthly buckets are taken on ``local_day``, a different partition of the "
            "same intervals"
        )
    local_start, local_end = bounds
    if all(
        (moment.month, moment.day, moment.hour, moment.minute) == (1, 1, 0, 0)
        for moment in (local_start, local_end)
    ):
        return (
            f"Horizon scope: the horizon above is UTC-scoped ({start} → {end}) and both "
            f"bounds fall on {timezone} midnight, so it is a {timezone} calendar year too"
        )
    return (
        f"Horizon scope: the horizon above is a UTC-scoped window, not a {timezone} calendar "
        f"year — {start} is {local_start.strftime('%Y-%m-%d %H:%M')} and {end} is "
        f"{local_end.strftime('%Y-%m-%d %H:%M')} {timezone} local, so the local day on each "
        "edge is partial, while the monthly buckets are taken on ``local_day``"
    )


# ---------------------------------------------------------------------------
# Markdown rendering
# ---------------------------------------------------------------------------


def _fmt_eur(value: float | None) -> str:
    return "—" if value is None else f"{value:,.4f} EUR"


def _num(value: float | None, digits: int = 6) -> str:
    """Format an optional number for a Markdown table cell; None stays an em-dash."""
    return "—" if value is None else f"{value:.{digits}f}"


def _table(headers: Sequence[str], rows: Sequence[Sequence[Any]]) -> str:
    """A GitHub-flavoured Markdown table, or a one-line note when there are no rows."""
    if not rows:
        return "*(no rows)*"
    cells = [[str(c) for c in row] for row in rows]
    widths = [max(len(headers[i]), *(len(row[i]) for row in cells)) for i in range(len(headers))]
    head = "| " + " | ".join(headers[i].ljust(widths[i]) for i in range(len(headers))) + " |"
    rule = "|" + "|".join("-" * (w + 2) for w in widths) + "|"
    body = [
        "| " + " | ".join(row[i].ljust(widths[i]) for i in range(len(headers))) + " |"
        for row in cells
    ]
    return "\n".join([head, rule, *body])


def _render_markdown(
    run: _Run,
    monthly: list[dict[str, Any]],
    dist: dict[str, Any],
    throughput: dict[str, Any],
    solver_bounds: dict[str, Any],
) -> str:
    """Render the run as Markdown: numbers, then what may be claimed about them."""
    payload = run.payload
    evaluation = payload["evaluation"]
    training = payload["training"]
    assumptions = payload["assumptions"]
    battery = assumptions["battery"]
    comparison = payload["comparison"]
    basis = assumptions["cost_objective"]
    out: list[str] = ["# Energy baseline — monthly reconciliation report", ""]
    out += [
        f"**Run id:** `{payload['run_id']}`",
        f"**Dataset:** `{payload['lineage']['dataset_id']}`",
        f"**Benchmark contract:** v{payload['benchmark_version']}",
        f"**Store schema:** v{SCHEMA_VERSION}",
        f"**Code:** `{payload['lineage']['code_version']}`",
        f"**Config hash:** `{payload['lineage']['config_hash']}`",
        "",
    ]

    def section(title: str, bullets: Sequence[str] = ()) -> None:
        out.append(f"## {title}")
        out.append("")
        for bullet in bullets:
            # A table is one multi-line "bullet": it needs a blank line after it or
            # the following paragraph gets absorbed into the table body.
            out.append(bullet)
            if bullet and "\n" in bullet:
                out.append("")
        out.append("")

    section(
        "Evaluation window",
        [
            f"* **Horizon:** {evaluation['horizon_start']} → {evaluation['horizon_end']}",
            f"* {_horizon_scope_note(run)}",
            f"* **Intervals:** {evaluation['interval_count']} total, "
            f"{evaluation['usable_interval_count']} usable, "
            f"{evaluation['excluded_interval_count']} excluded",
            f"* **Months:** {', '.join(evaluation['months_included'])}",
            f"* **Partial year:** {'yes' if evaluation['window_is_partial_year'] else 'no'}",
            f"* {evaluation['scaling_note']}",
        ],
    )
    section(
        "Training window (frozen before evaluation)",
        [
            f"* **Train end:** {training['train_end']} ({training['train_days']} days back)",
            f"* **Usable training intervals:** {training['usable_interval_count']}",
            f"* **Charge threshold:** {training['charge_threshold_eur_per_kwh']!r} EUR/kWh "
            f"at percentile {training['charge_pct']} of training prices",
            f"* **Discharge threshold:** {training['discharge_threshold_eur_per_kwh']!r} EUR/kWh "
            f"at percentile {training['discharge_pct']} of training prices",
            f"* {training['leakage_note']}",
        ],
    )
    section(
        "Battery assumptions",
        [
            f"* **Capacity:** {battery['capacity_kwh']} kWh, **power:** {battery['power_kw']} kW",
            f"* **One-way efficiencies:** charge {battery['charge_efficiency']:.6f} / "
            f"discharge {battery['discharge_efficiency']:.6f}",
            f"* **SOC window:** {battery['soc_min_frac']} – {battery['soc_max_frac']}, "
            f"**initial SOC:** {battery['initial_soc_frac']}",
            f"* **Import ceiling:** {battery['import_limit_kw']!r} kW",
            f"* **Idle losses:** {battery['idle_loss_kwh_per_hour']} kWh/h "
            f"({'configured' if battery['idle_losses_measured'] else 'assumed zero'})",
            f"* {battery['efficiency_mapping']}",
            f"* {battery['initial_soc_source']}",
            f"* {battery['import_limit_source']}",
        ],
    )
    section(
        "Cost basis",
        [
            f"* **Basis:** {basis['cost_basis']}, "
            f"**uniform multiplier:** {basis['uniform_multiplier']!r}",
            f"* {basis['note']}",
        ],
    )
    section(
        "Policy results",
        [
            _table(
                [
                    "policy",
                    "cost (EUR)",
                    "EFC (DC)",
                    "AC delivered (kWh)",
                    "solver status",
                    "bound",
                    "certified",
                ],
                [
                    [
                        name,
                        _fmt_eur(r["cost_eur"]),
                        _num(r["efc"], 4),
                        _num(r["throughput_kwh"], 4),
                        r["solver_status"],
                        r["bound_kind"],
                        str(r["is_certified"]),
                    ]
                    for name, r in payload["policies"].items()
                ],
            )
        ],
    )
    comparison_bullets = [
        f"* **Baseline (no battery):** {_fmt_eur(comparison['baseline_cost_eur'])} "
        f"on the {comparison['basis']} basis",
        f"* **Oracle ≤ baseline:** {comparison['oracle_dominates_baseline']}; "
        f"**oracle ≤ causal:** {comparison['oracle_dominates_causal']}",
        f"* {comparison['dominance_note']}",
    ]
    for name, entry in comparison["per_policy"].items():
        comparison_bullets.append(
            f"* **{name}:** saving {_fmt_eur(entry['savings_eur'])} "
            f"(lower bound {_fmt_eur(entry['savings_lower_bound_eur'])}, upper bound "
            f"{_fmt_eur(entry['savings_upper_bound_eur'])}); worse than baseline: "
            f"{entry['worse_than_baseline']}"
        )
    section("Comparison", comparison_bullets)
    section("Terminal accounting", [_terminal_note(run)])

    section(
        "Monthly breakdown",
        [
            _table(
                [
                    "month",
                    "policy",
                    "intervals",
                    "usable",
                    "excluded",
                    "meter cost (EUR)",
                    "inventory adj. (EUR)",
                    "AC charge (kWh)",
                    "AC discharge (kWh)",
                ],
                [
                    [
                        r["month"],
                        r["policy"],
                        r["intervals_total"],
                        r["intervals_usable"],
                        r["intervals_excluded"],
                        f"{r['meter_cost_eur']:.4f}",
                        f"{r['inventory_adjustment_eur']:.4f}",
                        f"{r['ac_charge_kwh']:.4f}",
                        f"{r['ac_discharge_kwh']:.4f}",
                    ]
                    for r in monthly
                ],
            ),
            "Costs are per-interval sums over usable intervals only, bucketed on the *local* "
            "calendar (``local_day``), so a DST month keeps its 23- or 25-hour day whole. "
            "Excluded intervals are counted, never costed. The inventory adjustment is zero "
            "except in the last month of the horizon: it describes where the SOC ended up, not "
            "what was metered.",
        ],
    )
    section(
        "Throughput, cycles and SOC range",
        [
            _table(
                [
                    "policy",
                    "AC charge (kWh)",
                    "AC deliver (kWh)",
                    "DC charge (kWh)",
                    "DC deliver (kWh)",
                    "EFC (DC)",
                    "SOC min",
                    "SOC max",
                    "SOC start (kWh)",
                    "SOC end (kWh)",
                ],
                [
                    [
                        name,
                        f"{r['ac_charge_kwh']:.4f}",
                        f"{r['ac_discharge_kwh']:.4f}",
                        f"{r['dc_charge_kwh']:.4f}",
                        f"{r['dc_discharge_kwh']:.4f}",
                        _num(r["efc"]),
                        _num(r["soc_min_frac"]),
                        _num(r["soc_max_frac"]),
                        _num(r["initial_soc_kwh"]),
                        _num(r["terminal_soc_kwh"]),
                    ]
                    for name, r in throughput.items()
                ],
            ),
            (
                f"* {next(iter(throughput.values()))['note']}"
                if throughput
                else "*(no policies ran)*"
            ),
        ],
    )
    bound_rows = [
        [
            name,
            _num(r.get("cost_eur")),
            _num(r.get("cost_upper_bound_eur")),
            _num(r.get("cost_lower_bound_eur")),
            _num(r.get("solver_gap")),
            _num(r.get("configured_mip_rel_gap")),
            _num(r.get("configured_mip_abs_gap")),
            _num(r.get("solver_time_limit_s")),
            r.get("solver_status", "error"),
            r.get("bound_kind", "none"),
            str(bool(r.get("is_certified"))),
        ]
        for name, r in solver_bounds.items()
    ]
    bound_bullets = [
        _table(
            [
                "policy",
                "cost (EUR)",
                "cost upper bound",
                "cost lower bound",
                "gap",
                "rel tol",
                "abs tol",
                "time limit (s)",
                "status",
                "bound kind",
                "certified",
            ],
            bound_rows,
        ),
        "An em-dash bound means the solver produced none, so nothing is claimed from it. "
        '"Certified" means the gap was inside the configured tolerance, not that it is zero.',
    ]
    for name, row in solver_bounds.items():
        for note in row.get("bound_notes", []):
            bound_bullets.append(f"* **{name}:** {note}")
    section("Solver bounds, tolerances and time limits", bound_bullets)

    dist_rows = []
    for policy, metrics in dist.items():
        for metric, values in metrics.items():
            if values.get("count", 0) == 0:
                dist_rows.append([policy, metric, 0, "—", "—", "—", "—", "—", "—"])
            else:
                dist_rows.append(
                    [
                        policy,
                        metric,
                        values["count"],
                        f"{values['min']:.6f}",
                        f"{values['p50']:.6f}",
                        f"{values['p90']:.6f}",
                        f"{values['p99']:.6f}",
                        f"{values['max']:.6f}",
                        f"{values['mean']:.6f}",
                    ]
                )
    section(
        "Distributions (per policy, usable intervals only)",
        [
            _table(["policy", "metric", "n", "min", "p50", "p90", "p99", "max", "mean"], dist_rows),
            "Per-interval quantities, not annual or whole-horizon aggregates: the cost row is the "
            "pretax EUR of a single 15-minute interval of that policy's plan.",
        ],
    )
    section(
        "Quality and excluded intervals",
        [
            _table(
                ["month", "intervals", "usable", "excluded", "usable fraction", "in evaluation"],
                [
                    [
                        r["month"],
                        r["intervals_total"],
                        r["intervals_usable"],
                        r["intervals_excluded"],
                        f"{r['usable_fraction']:.4f}",
                        str(r["in_evaluation"]),
                    ]
                    for r in payload["quality"]["per_month"]
                ],
            ),
            f"* Declared monthly threshold: "
            f"{payload['quality']['declared_thresholds']['min_usable_fraction']} — "
            f"{payload['quality']['declared_thresholds']['note']}",
            "* Evaluation exclusions: "
            + json.dumps(payload["quality"]["eval_exclusion_reasons"], sort_keys=True),
        ],
    )
    section(
        "EUR is for this window only",
        [
            "Every figure is the total for exactly the horizon above. Nothing is scaled to a "
            "year, and no annual cost, saving or payback period is stated here — those need a "
            "forward-looking assumption this phase does not make."
        ],
    )
    section(
        "Assumptions",
        [
            f"* {assumptions['gross_load_basis']}",
            f"* {assumptions['idle_losses_note']}",
            f"* {assumptions['exclusions_are_neutral_carry']}",
            f"* {assumptions['no_intentional_export']}",
            f"* {assumptions['soc_window_and_efficiency_are_distinct']}",
            f"* **Site import limit:** {assumptions['site_import_limit']}",
        ],
    )
    errors = payload.get("solver", {}).get("errors", {})
    if errors:
        section(
            "Policies with no result row",
            [
                f"* **{name}:** {err['error']} ({err['failure_kind']}) — {err['note']}"
                for name, err in errors.items()
            ],
        )
    return "\n".join(out).rstrip() + "\n"


# ---------------------------------------------------------------------------
# Public entry point
# ---------------------------------------------------------------------------


def generate_report(
    *,
    data_dir: str | None = None,
    config_path: str | None = None,
    dataset: str | Path | None = None,
    benchmark_path: str | Path | None = None,
) -> Path:
    """Generate a monthly reconciliation report for a benchmark run.

    Writes ``report.json`` and ``report.md`` into the run directory, and returns
    that directory.  The run is chosen explicitly: ``benchmark_path`` wins, then
    ``dataset`` (which must resolve to exactly one run), then ``data_dir`` (which
    must resolve to exactly one dataset with exactly one run).  Ambiguity is an
    error, never a silent "latest" pick.

    Args:
        data_dir: The data root, used only to enumerate datasets when neither
            ``dataset`` nor ``benchmark_path`` is given.
        config_path: The tariff config used for the run.  Its canonical hash must
            match the one pinned in the run; a mismatch raises rather than
            silently reporting against a different config.
        dataset: A dataset directory (``analysis/v<schema>/<dataset_id>``).  Must
            contain exactly one benchmark run.
        benchmark_path: A benchmark run directory (or its ``benchmark.json``).
            Overrides ``dataset``.

    Returns:
        The run directory, now containing ``report.json`` and ``report.md``.

    Raises:
        ReportInputError: no run found, ambiguous run, or a run that is missing
            one of its artifacts.
        ReportError: the supplied config does not match the one pinned in the run.
    """
    data_dir_path = Path(data_dir).expanduser() if data_dir is not None else resolve_data_dir(None)
    run = _resolve_run(
        data_dir=data_dir_path,
        dataset=dataset,
        benchmark_path=benchmark_path,
    )

    if config_path is not None:
        cfg_path = Path(config_path).expanduser()
        if not cfg_path.is_file():
            raise ReportInputError(f"tariff config not found: {cfg_path}")
        cfg_hash = config_hash(cfg_path)
        pinned = run.payload.get("lineage", {}).get("config_hash")
        if pinned and pinned != cfg_hash:
            raise ReportError(
                f"the supplied config hashes differently ({cfg_hash[:12]}) than the one pinned "
                f"in the run ({str(pinned)[:12]}): the report would be against a different "
                "tariff than the benchmark was run against. Pass the same config, or omit "
                "--config to skip the check"
            )

    monthly = _monthly_rows(run)
    distributions = _distributions(run)
    throughput = _throughput(run)
    solver_bounds = _solver_bounds(run)
    markdown = _render_markdown(run, monthly, distributions, throughput, solver_bounds)

    report_payload: dict[str, Any] = {
        "benchmark_version": BENCHMARK_VERSION,
        "store_schema_version": SCHEMA_VERSION,
        "run_id": run.payload["run_id"],
        "dataset_id": run.payload["lineage"]["dataset_id"],
        "evaluation": run.payload["evaluation"],
        "training": run.payload["training"],
        "assumptions": run.payload["assumptions"],
        "cost_basis": run.payload["assumptions"]["cost_objective"],
        "policies": run.payload["policies"],
        "solver_bounds": solver_bounds,
        "comparison": run.payload["comparison"],
        "monthly": monthly,
        "throughput": throughput,
        "distributions": distributions,
        "solver_errors": run.payload.get("solver", {}).get("errors", {}),
        "lineage": run.payload["lineage"],
        "horizon_scope_note": _horizon_scope_note(run),
        "artifact_integrity": {
            "benchmark_manifest_verified": run.benchmark_verified,
            "benchmark_manifest_present": (run.directory / BENCHMARK_MANIFEST_NAME).is_file(),
            "note": (
                "benchmark_manifest_verified=true means the benchmark artifacts were verified "
                "against the benchmark sidecar manifest before the report was generated. "
                "benchmark_manifest_verified=false with benchmark_manifest_present=true means "
                "verification failed (report was not generated). "
                "benchmark_manifest_verified=false with benchmark_manifest_present=false means "
                "this is a legacy run with no manifest — provenance is unverified, not verified"
            ),
        },
        "report_note": (
            "This report is a pure function of the run's own artifacts and the pinned config: "
            "no wall-clock timestamp and no code version of its own, so re-generating it from "
            "the same run writes the same bytes into the same immutable location. The code "
            "version that produced the numbers is lineage.code_version, from the run"
        ),
    }

    write_part_atomic(
        run.directory / REPORT_JSON_NAME,
        json.dumps(report_payload, indent=2, sort_keys=True, default=str).encode("utf-8"),
    )
    write_part_atomic(run.directory / REPORT_MD_NAME, markdown.encode("utf-8"))

    # Finalize the report stage: write the report outputs manifest sidecar.
    finalize_run_outputs(
        run.directory,
        required_files=REPORT_REQUIRED_FILES,
        manifest_name=REPORT_MANIFEST_NAME,
    )

    return run.directory


# ---------------------------------------------------------------------------
# DuckDB SQL inspection
# ---------------------------------------------------------------------------


def _connect(dataset: Path, *, benchmark: Path | None = None) -> duckdb.DuckDBPyConnection:
    """Open an in-memory DuckDB with the dataset (and optional benchmark) registered.

    The connection is hardened against filesystem access and extension loading so
    a user-supplied SELECT cannot reach outside the registered views.
    """
    loaded = load_dataset(dataset)
    conn = duckdb.connect()

    # Register the analysis grid as the primary view.
    conn.register("intervals", loaded.table)

    # A single-row meta view for lineage-aware queries.
    meta = pa.table(
        {
            "dataset_id": pa.array([loaded.dataset_id], pa.string()),
            "interval_count": pa.array([loaded.table.num_rows], pa.int64()),
            "interval_seconds": pa.array([loaded.interval_seconds], pa.float64()),
            "window_start": pa.array([loaded.start], pa.timestamp("us", tz="UTC")),
            "window_end": pa.array([loaded.end], pa.timestamp("us", tz="UTC")),
            "config_hash": pa.array([loaded.manifest.get("config_hash") or ""], pa.string()),
            "code_version": pa.array([loaded.manifest.get("code_version") or ""], pa.string()),
            "schema_version": pa.array(
                [int(loaded.manifest.get("schema_version", SCHEMA_VERSION))], pa.int64()
            ),
        }
    )
    conn.register("dataset_meta", meta)

    if benchmark is not None:
        run = _read_run(Path(benchmark))
        conn.register("benchmark_results", run.results)
        conn.register("benchmark_schedules", run.schedules)

    # Harden the connection: no filesystem access, no extensions, no secrets,
    # and UTC for any timestamp a query renders (DuckDB would otherwise follow
    # the machine's local zone).
    for stmt in (
        "SET TimeZone = 'UTC'",
        "SET enable_external_access = false",
        "SET disabled_filesystems = 'LocalFileSystem'",
        "SET autoinstall_known_extensions = false",
        "SET autoload_known_extensions = false",
        "SET allow_persistent_secrets = false",
    ):
        conn.execute(stmt)
    return conn


def _guard_sql(sql: str) -> str:
    """Validate *sql* as a single read-only statement.  Returns the stripped SQL.

    Uses DuckDB's own parser to classify the statement type.  Multi-statement
    input, anything other than ``SELECT`` / ``WITH ... SELECT`` / ``EXPLAIN
    SELECT`` is refused before the engine ever sees it.
    """
    text = sql.strip()
    if not text:
        raise SQLInputError("SQL is empty")
    try:
        statements = duckdb.extract_statements(text)
    except duckdb.ParserException as exc:
        raise SQLInputError(f"SQL could not be parsed: {exc}") from exc
    if len(statements) != 1:
        raise SQLInputError(
            f"SQL must be a single statement, got {len(statements)} — multi-statement input "
            "is not permitted"
        )
    (statement,) = statements
    if statement.type == duckdb.StatementType.SELECT:
        return text
    if statement.type == duckdb.StatementType.EXPLAIN:
        # EXPLAIN ANALYZE actually runs the query; EXPLAIN SELECT is still read-only.
        # We accept both, but only when the body is itself a SELECT/WITH.
        body = _strip_explain_prefix(text)
        try:
            inner = duckdb.extract_statements(body)
        except duckdb.ParserException as exc:
            raise SQLInputError(f"EXPLAIN body could not be parsed: {exc}") from exc
        if len(inner) != 1 or inner[0].type != duckdb.StatementType.SELECT:
            raise SQLInputError(
                "EXPLAIN is only permitted over a single SELECT / WITH ... SELECT statement"
            )
        return text
    raise SQLInputError(
        f"SQL must be SELECT, WITH ... SELECT or EXPLAIN SELECT; got statement type "
        f"{statement.type.name!r}"
    )


_EXPLAIN_RE = re.compile(
    r"^\s*EXPLAIN\s+(?:ANALYZE\s+)?(?:\(\s*\w+\s*\)\s+)?",
    re.IGNORECASE,
)


def _strip_explain_prefix(sql: str) -> str:
    """Remove the ``EXPLAIN [ANALYZE] [(options)]`` prefix from *sql*."""
    return _EXPLAIN_RE.sub("", sql, count=1).strip()


def _jsonify_row(row: dict[str, Any]) -> dict[str, Any]:
    """Make one Arrow-converted row JSON-serialisable.

    Arrow hands back ``datetime`` / ``date`` / ``Decimal`` / nested lists; the CLI
    prints these as JSON, so temporal values become ISO strings.
    """
    out: dict[str, Any] = {}
    for key, value in row.items():
        if isinstance(value, (datetime, date)):
            out[key] = value.isoformat()
        elif isinstance(value, Decimal):
            out[key] = float(value)
        else:
            out[key] = value
    return out


def inspect_sql(
    *,
    dataset: Path | str,
    sql: str,
    benchmark: Path | str | None = None,
) -> list[dict[str, Any]]:
    """Run a single read-only SQL statement against a dataset.

    Args:
        dataset: The dataset directory.  Loaded through its manifest, registered
            as the ``intervals`` view.
        sql: A single ``SELECT`` / ``WITH ... SELECT`` / ``EXPLAIN SELECT``.
            Anything else is refused at the grammar level.
        benchmark: Optional benchmark run directory.  When given, the run's
            ``results.parquet`` and ``schedules.parquet`` are registered as
            ``benchmark_results`` and ``benchmark_schedules``.

    Returns:
        A list of row dicts — the result of the query, or a single row with the
        plan for ``EXPLAIN``.

    Raises:
        SQLInputError: the SQL is not a single read-only statement.
        ReportInputError: the dataset or benchmark cannot be loaded.
    """
    _guard_sql(sql)
    conn = _connect(
        Path(dataset).expanduser(),
        benchmark=Path(benchmark).expanduser() if benchmark is not None else None,
    )
    try:
        # Arrow, not fetchall(): the DB-API path needs pytz to materialise
        # TIMESTAMP WITH TIME ZONE, and this project does not depend on it.
        table = conn.sql(sql).to_arrow_table()
        return [_jsonify_row(row) for row in table.to_pylist()]
    finally:
        conn.close()


def list_curated_queries() -> list[str]:
    """Names of the curated read-only queries, for CLI completion / help."""
    return sorted(CURATED_QUERIES)


def run_curated_query(
    name: str,
    *,
    dataset: Path | str,
    benchmark: Path | str | None = None,
) -> list[dict[str, Any]]:
    """Run a curated query by name."""
    if name not in CURATED_QUERIES:
        raise SQLInputError(f"unknown curated query {name!r}; available: {list_curated_queries()}")
    return inspect_sql(
        dataset=dataset,
        sql=CURATED_QUERIES[name],
        benchmark=benchmark,
    )
