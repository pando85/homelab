"""Tests for the report generation and DuckDB SQL inspection layer.

These tests exercise the report generation, monthly aggregation, distributions,
and the SQL guard — all with synthetic data, no live cluster.
"""

from __future__ import annotations

import json
from datetime import UTC, datetime, timedelta
from pathlib import Path

import numpy as np
import pyarrow as pa
import pyarrow.parquet as pq
import pytest

from energy_baseline import benchmark as bm
from energy_baseline import report as rp
from energy_baseline.config import BatteryConfig, TariffConfig, TariffPeriods, TaxEntry

# ---------------------------------------------------------------------------
# Helpers (shared with test_benchmark.py)
# ---------------------------------------------------------------------------


def make_tariff_config(
    *,
    capacity_kwh: float = 5.12,
    power_kw: float = 2.5,
    round_trip_efficiency: float | None = 0.9025,
    soc_window_min: float | None = 0.05,
    soc_window_max: float | None = 0.95,
    import_limit_kw: float | None = None,
    iee_pct: float | None = None,
    iva_pct: float | None = None,
) -> TariffConfig:
    return TariffConfig(
        version=1,
        taxes=[
            TaxEntry(
                valid_from=datetime(2024, 1, 1).date(),
                iee_pct=iee_pct,
                iva_pct=iva_pct,
                source="test",
            )
        ],
        tariff_periods=TariffPeriods(timezone="Europe/Madrid"),
        battery=BatteryConfig(
            capacity_kwh=capacity_kwh,
            power_kw=power_kw,
            round_trip_efficiency=round_trip_efficiency,
            soc_window_min=soc_window_min,
            soc_window_max=soc_window_max,
            import_limit_kw=import_limit_kw,
        ),
    )


def write_tariff_config(path: Path, config: TariffConfig) -> None:
    import yaml

    data = {
        "version": config.version,
        "taxes": [
            {
                "valid_from": str(tax.valid_from),
                "iee_pct": tax.iee_pct,
                "iva_pct": tax.iva_pct,
                "source": tax.source,
            }
            for tax in config.taxes
        ],
        "tariff_periods": {"timezone": config.tariff_periods.timezone},
        "battery": {
            "capacity_kwh": config.battery.capacity_kwh,
            "power_kw": config.battery.power_kw,
            "round_trip_efficiency": config.battery.round_trip_efficiency,
            "soc_window_min": config.battery.soc_window_min,
            "soc_window_max": config.battery.soc_window_max,
            "import_limit_kw": config.battery.import_limit_kw,
        },
    }
    path.write_text(yaml.safe_dump(data, sort_keys=False))


def make_analysis_table(
    *,
    start: datetime,
    intervals: int,
    interval_seconds: int = 900,
    gross_kwh: list[float] | None = None,
    prices: list[float] | None = None,
    dataset_id: str = "test-dataset-001",
) -> pa.Table:
    from zoneinfo import ZoneInfo

    from energy_baseline.schemas import ANALYSIS_INTERVAL

    madrid = ZoneInfo("Europe/Madrid")
    starts = [start + timedelta(seconds=i * interval_seconds) for i in range(intervals)]
    ends = [s + timedelta(seconds=interval_seconds) for s in starts]
    local_days = [s.astimezone(madrid).date() for s in starts]

    if gross_kwh is None:
        gross_kwh = [0.5] * intervals
    if prices is None:
        prices = [0.15] * intervals

    return pa.table(
        {
            "interval_start": pa.array(starts, pa.timestamp("us", tz="UTC")),
            "interval_end": pa.array(ends, pa.timestamp("us", tz="UTC")),
            "tz_local": pa.array(["Europe/Madrid"] * intervals, pa.string()),
            "tariff_period": pa.array([3] * intervals, pa.int8()),
            "local_day": pa.array(local_days, pa.date32()),
            "dst_transition": pa.array([None] * intervals, pa.string()),
            "raw_grid_kwh": pa.array(gross_kwh, pa.float64()),
            "gross_kwh": pa.array(gross_kwh, pa.float64()),
            "battery_absent": pa.array([True] * intervals, pa.bool_()),
            "energy_quality": pa.array(["measured"] * intervals, pa.string()),
            "allocation_quality": pa.array(["exact"] * intervals, pa.string()),
            "sample_count": pa.array([4] * intervals, pa.int32()),
            "gap_seconds": pa.array([0.0] * intervals, pa.float64()),
            "price_eur_per_kwh": pa.array(prices, pa.float64()),
            "price_quality": pa.array(["measured"] * intervals, pa.string()),
            "dataset_id": pa.array([dataset_id] * intervals, pa.string()),
        },
        schema=ANALYSIS_INTERVAL,
    )


def write_synthetic_dataset(
    tmp_path: Path,
    table: pa.Table,
    *,
    dataset_id: str = "test-dataset-001",
    config_path: Path | None = None,
) -> Path:
    """Write a synthetic dataset directory with manifest + parquet.

    If ``config_path`` is None, writes a default tariff config to
    ``tmp_path/"tariff.yaml"`` and pins its hash.  If given, the file must exist
    and its hash is pinned.
    """
    import hashlib

    from energy_baseline.manifest import config_hash
    from energy_baseline.schemas import SCHEMA_VERSION

    if config_path is None:
        config_path = tmp_path / "tariff.yaml"
        write_tariff_config(config_path, make_tariff_config())
    cfg_hash = config_hash(config_path)

    dataset_dir = tmp_path / "analysis" / f"v{SCHEMA_VERSION}" / dataset_id
    parts_dir = dataset_dir / "parts"
    parts_dir.mkdir(parents=True, exist_ok=True)

    part_path = parts_dir / f"analysis-{dataset_id}-00000000.parquet"
    pq.write_table(table, part_path)
    part_bytes = part_path.read_bytes()
    part_sha = hashlib.sha256(part_bytes).hexdigest()

    manifest = {
        "manifest_version": 1,
        "dataset_id": dataset_id,
        "schema_version": SCHEMA_VERSION,
        "config_hash": cfg_hash,
        "code_version": "test-code-v1",
        "part_files": [
            {
                "path": str(part_path.relative_to(dataset_dir)),
                "sha256": part_sha,
                "rows": table.num_rows,
            }
        ],
        "requested_window": {
            "start": table.column("interval_start")[0].as_py().isoformat(),
            "end": table.column("interval_end")[-1].as_py().isoformat(),
        },
        "coverage": {"interval_count": table.num_rows, "grid_samples": table.num_rows * 4},
        "params": {"interval_seconds": 900, "timezone": "Europe/Madrid"},
    }
    (dataset_dir / "build_manifest.json").write_text(json.dumps(manifest, indent=2))
    return dataset_dir


def run_benchmark(tmp_path: Path) -> Path:
    """Run a minimal benchmark and return the run directory."""
    start = datetime(2026, 1, 1, tzinfo=UTC)
    intervals = 7 * 24 * 4  # 1 week
    rng = np.random.default_rng(42)
    prices = rng.uniform(0.05, 0.30, intervals).tolist()
    gross = rng.uniform(0.3, 1.5, intervals).tolist()
    table = make_analysis_table(start=start, intervals=intervals, gross_kwh=gross, prices=prices)
    dataset_dir = write_synthetic_dataset(tmp_path, table)

    config = make_tariff_config()
    config_path = tmp_path / "tariff.yaml"
    write_tariff_config(config_path, config)

    train_end = start + timedelta(days=2)
    return bm.benchmark_dataset(
        dataset=dataset_dir,
        config_path=config_path,
        train_end=train_end,
        time_limit_s=10.0,
    )


def requires_core() -> None:
    if not bm.core_available():
        pytest.skip("numeric core not implemented yet")


# ---------------------------------------------------------------------------
# Report generation
# ---------------------------------------------------------------------------


class TestGenerateReport:
    def test_end_to_end_report(self, tmp_path: Path) -> None:
        requires_core()
        run_dir = run_benchmark(tmp_path)
        config_path = tmp_path / "tariff.yaml"
        report_dir = rp.generate_report(
            config_path=str(config_path),
            benchmark_path=str(run_dir),
        )
        assert report_dir == run_dir
        assert (report_dir / rp.REPORT_JSON_NAME).is_file()
        assert (report_dir / rp.REPORT_MD_NAME).is_file()

        # Verify report.json has expected keys
        payload = json.loads((report_dir / rp.REPORT_JSON_NAME).read_text())
        assert payload["run_id"]
        assert payload["dataset_id"] == "test-dataset-001"
        # The report covers the evaluation window, not the whole dataset: the
        # first two days are training data and are never costed.
        assert payload["evaluation"]["dataset_interval_count"] == 7 * 24 * 4
        assert payload["evaluation"]["interval_count"] == 5 * 24 * 4
        assert payload["monthly"]
        assert payload["distributions"]

        # Verify report.md has expected sections
        markdown = (report_dir / rp.REPORT_MD_NAME).read_text()
        assert "# Energy baseline — monthly reconciliation report" in markdown
        assert "## Evaluation window" in markdown
        assert "## Policy results" in markdown
        assert "## Monthly breakdown" in markdown

    def test_tax_null_means_untaxed(self, tmp_path: Path) -> None:
        requires_core()
        run_dir = run_benchmark(tmp_path)
        # Config has null taxes (default)
        config_path = tmp_path / "tariff.yaml"
        report_dir = rp.generate_report(
            config_path=str(config_path),
            benchmark_path=str(run_dir),
        )
        payload = json.loads((report_dir / rp.REPORT_JSON_NAME).read_text())
        assert payload["cost_basis"]["cost_basis"] == "untaxed"
        assert payload["cost_basis"]["has_unconfirmed_rate"] is True

    def test_ambiguous_dataset_raises(self, tmp_path: Path) -> None:
        requires_core()
        # Run two benchmarks on the same dataset (different train_end → different run_id)
        start = datetime(2026, 1, 1, tzinfo=UTC)
        intervals = 7 * 24 * 4
        rng = np.random.default_rng(42)
        prices = rng.uniform(0.05, 0.30, intervals).tolist()
        gross = rng.uniform(0.3, 1.5, intervals).tolist()
        table = make_analysis_table(
            start=start, intervals=intervals, gross_kwh=gross, prices=prices
        )
        dataset_dir = write_synthetic_dataset(tmp_path, table)

        config = make_tariff_config()
        config_path = tmp_path / "tariff.yaml"
        write_tariff_config(config_path, config)

        # Two runs with different train_end
        bm.benchmark_dataset(
            dataset=dataset_dir,
            config_path=config_path,
            train_end=start + timedelta(days=2),
            time_limit_s=5.0,
        )
        bm.benchmark_dataset(
            dataset=dataset_dir,
            config_path=config_path,
            train_end=start + timedelta(days=3),
            time_limit_s=5.0,
        )

        # Now try to generate a report without specifying which run
        with pytest.raises(rp.ReportInputError, match="has 2 benchmark runs"):
            rp.generate_report(dataset=str(dataset_dir), config_path=str(config_path))

    def test_ambiguous_data_dir_raises(self, tmp_path: Path) -> None:
        """Several datasets under one data root is ambiguous, and stays an error.

        There is no "pick the newest" fallback: a report silently generated against
        the wrong dataset is worse than no report.
        """
        requires_core()
        start = datetime(2026, 1, 1, tzinfo=UTC)
        config_path = tmp_path / "tariff.yaml"
        write_tariff_config(config_path, make_tariff_config())
        for name in ("dataset-one", "dataset-two"):
            table = make_analysis_table(start=start, intervals=2 * 24 * 4, dataset_id=name)
            dataset_dir = write_synthetic_dataset(tmp_path, table, dataset_id=name)
            bm.benchmark_dataset(
                dataset=dataset_dir,
                config_path=config_path,
                train_end=start + timedelta(hours=12),
                time_limit_s=5.0,
            )
        with pytest.raises(rp.ReportInputError, match="has 2 datasets"):
            rp.generate_report(data_dir=str(tmp_path), config_path=str(config_path))

    def test_single_dataset_is_found_without_ambiguity(self, tmp_path: Path) -> None:
        requires_core()
        run_dir = run_benchmark(tmp_path)
        dataset_dir = run_dir.parent.parent
        # Passing only data_dir works when exactly one dataset and one run exist.
        report_dir = rp.generate_report(
            data_dir=str(tmp_path), config_path=str(tmp_path / "tariff.yaml")
        )
        assert report_dir == dataset_dir / bm.BENCHMARKS_DIRNAME / run_dir.name

    def test_missing_benchmark_artifacts_raise(self, tmp_path: Path) -> None:
        fake = tmp_path / "run-abc"
        fake.mkdir()
        with pytest.raises(rp.ReportInputError, match="missing"):
            rp._read_run(fake)

    def test_report_is_deterministic(self, tmp_path: Path) -> None:
        """Same run + same config ⇒ byte-identical report."""
        requires_core()
        run_dir = run_benchmark(tmp_path)
        config_path = tmp_path / "tariff.yaml"
        first = rp.generate_report(config_path=str(config_path), benchmark_path=str(run_dir))
        before = (first / rp.REPORT_MD_NAME).read_bytes()
        second = rp.generate_report(config_path=str(config_path), benchmark_path=str(run_dir))
        assert second == first
        assert (second / rp.REPORT_MD_NAME).read_bytes() == before

    def test_config_mismatch_raises(self, tmp_path: Path) -> None:
        requires_core()
        run_dir = run_benchmark(tmp_path)
        # Create a different config (different hash)
        different_config = make_tariff_config(round_trip_efficiency=0.85)
        different_config_path = tmp_path / "different-tariff.yaml"
        write_tariff_config(different_config_path, different_config)

        with pytest.raises(rp.ReportError, match="config hashes differently"):
            rp.generate_report(
                config_path=str(different_config_path),
                benchmark_path=str(run_dir),
            )


# ---------------------------------------------------------------------------
# SQL inspection
# ---------------------------------------------------------------------------


class TestInspectSQL:
    def test_select_works(self, tmp_path: Path) -> None:
        requires_core()
        run_dir = run_benchmark(tmp_path)
        dataset_dir = run_dir.parent.parent  # <dataset>/benchmarks/<run_id> → <dataset>
        results = rp.inspect_sql(
            dataset=str(dataset_dir),
            sql="SELECT COUNT(*) AS n FROM intervals",
        )
        assert len(results) == 1
        assert results[0]["n"] == 7 * 24 * 4

    def test_with_clause_works(self, tmp_path: Path) -> None:
        requires_core()
        run_dir = run_benchmark(tmp_path)
        dataset_dir = run_dir.parent.parent
        results = rp.inspect_sql(
            dataset=str(dataset_dir),
            sql="WITH base AS (SELECT 1 AS x) SELECT x FROM base",
        )
        assert len(results) == 1
        assert results[0]["x"] == 1

    def test_explain_select_works(self, tmp_path: Path) -> None:
        requires_core()
        run_dir = run_benchmark(tmp_path)
        dataset_dir = run_dir.parent.parent
        results = rp.inspect_sql(
            dataset=str(dataset_dir),
            sql="EXPLAIN SELECT COUNT(*) FROM intervals",
        )
        assert len(results) > 0  # EXPLAIN returns plan rows

    def test_copy_blocked(self, tmp_path: Path) -> None:
        requires_core()
        run_dir = run_benchmark(tmp_path)
        dataset_dir = run_dir.parent.parent
        with pytest.raises(rp.SQLInputError, match="must be SELECT"):
            rp.inspect_sql(
                dataset=str(dataset_dir),
                sql="COPY (SELECT 1) TO '/tmp/test.csv'",
            )

    def test_attach_blocked(self, tmp_path: Path) -> None:
        requires_core()
        run_dir = run_benchmark(tmp_path)
        dataset_dir = run_dir.parent.parent
        with pytest.raises(rp.SQLInputError, match="must be SELECT"):
            rp.inspect_sql(
                dataset=str(dataset_dir),
                sql="ATTACH '/tmp/test.db' AS test",
            )

    def test_multi_statement_blocked(self, tmp_path: Path) -> None:
        requires_core()
        run_dir = run_benchmark(tmp_path)
        dataset_dir = run_dir.parent.parent
        with pytest.raises(rp.SQLInputError, match="single statement"):
            rp.inspect_sql(
                dataset=str(dataset_dir),
                sql="SELECT 1; SELECT 2",
            )

    def test_read_csv_blocked_by_hardening(self, tmp_path: Path) -> None:
        requires_core()
        run_dir = run_benchmark(tmp_path)
        dataset_dir = run_dir.parent.parent
        # The SQL passes the grammar guard (it is a SELECT), so this proves the
        # second layer: the hardened connection cannot touch the filesystem at
        # all, which is what keeps /etc out of reach of a read-only query.
        with pytest.raises(Exception, match="disabled"):
            rp.inspect_sql(
                dataset=str(dataset_dir),
                sql="SELECT * FROM read_csv_auto('/etc/passwd')",
            )

    def test_parquet_scan_blocked_by_hardening(self, tmp_path: Path) -> None:
        requires_core()
        run_dir = run_benchmark(tmp_path)
        dataset_dir = run_dir.parent.parent
        part = dataset_dir / "parts" / "analysis-test-dataset-001-00000000.parquet"
        with pytest.raises(Exception, match="disabled"):
            rp.inspect_sql(dataset=str(dataset_dir), sql=f"SELECT * FROM read_parquet('{part}')")

    def test_glob_of_secret_path_blocked(self, tmp_path: Path) -> None:
        requires_core()
        run_dir = run_benchmark(tmp_path)
        dataset_dir = run_dir.parent.parent
        with pytest.raises(Exception, match="disabled"):
            rp.inspect_sql(
                dataset=str(dataset_dir),
                sql="SELECT * FROM glob('/home/**')",
            )

    def test_curated_queries(self, tmp_path: Path) -> None:
        requires_core()
        run_dir = run_benchmark(tmp_path)
        dataset_dir = run_dir.parent.parent
        for name in rp.list_curated_queries():
            results = rp.run_curated_query(
                name,
                dataset=str(dataset_dir),
                benchmark=str(run_dir),
            )
            assert isinstance(results, list)

    def test_inventory_adjustment_lands_only_on_the_last_month(self, tmp_path: Path) -> None:
        """The terminal inventory adjustment is a horizon-end event, not a monthly one.

        A fixed-terminal plan with a non-zero valuation reports a single
        ``inventory_adjustment_eur`` for the whole horizon.  The monthly grid must
        show it once, in the last month, and zero elsewhere — never smeared across
        months, because that would imply a cost that was never metered.
        """
        requires_core()
        start = datetime(2026, 1, 1, tzinfo=UTC)
        intervals = 3 * 24 * 4  # spans January and February, locally
        table = make_analysis_table(start=start, intervals=intervals)
        dataset_dir = write_synthetic_dataset(tmp_path, table)
        config_path = tmp_path / "tariff.yaml"
        write_tariff_config(config_path, make_tariff_config())
        run_dir = bm.benchmark_dataset(
            dataset=dataset_dir,
            config_path=config_path,
            train_end=start + timedelta(days=1),
            time_limit_s=5.0,
        )

        def rows_with_adjustment(adjustment: float) -> list[dict[str, object]]:
            payload = json.loads((run_dir / bm.RESULTS_JSON_NAME).read_text())
            for record in payload["policies"].values():
                record["inventory_adjustment_eur"] = adjustment
            run = rp._Run(
                directory=run_dir,
                payload=payload,
                results=pq.read_table(run_dir / bm.RESULTS_PARQUET_NAME),
                schedules=pq.read_table(run_dir / bm.SCHEDULES_PARQUET_NAME),
            )
            return rp._monthly_rows(run)

        default = rows_with_adjustment(0.0)
        assert {row["inventory_adjustment_eur"] for row in default} == {0.0}

        shifted = rows_with_adjustment(-3.5)
        by_policy: dict[str, list[dict[str, object]]] = {}
        for row in shifted:
            by_policy.setdefault(row["policy"], []).append(row)
        for policy, rows in by_policy.items():
            flagged = [row for row in rows if row["carries_terminal_inventory_adjustment"]]
            assert len(flagged) == 1, policy
            assert flagged[0]["month"] == max(row["month"] for row in rows), policy
            assert flagged[0]["inventory_adjustment_eur"] == pytest.approx(-3.5), policy
            assert all(row["inventory_adjustment_eur"] == 0.0 for row in rows if row not in flagged)

    def test_monthly_totals_reconcile_with_the_policy_cost(self, tmp_path: Path) -> None:
        """Σ monthly metered cost == the cost the solver reported for that policy."""
        requires_core()
        run_dir = run_benchmark(tmp_path)
        payload = json.loads((run_dir / bm.RESULTS_JSON_NAME).read_text())
        run = rp._Run(
            directory=run_dir,
            payload=payload,
            results=pq.read_table(run_dir / bm.RESULTS_PARQUET_NAME),
            schedules=pq.read_table(run_dir / bm.SCHEDULES_PARQUET_NAME),
        )
        rows = rp._monthly_rows(run)
        for policy, record in payload["policies"].items():
            total = sum(row["meter_cost_eur"] for row in rows if row["policy"] == policy)
            adjustment = record.get("inventory_adjustment_eur") or 0.0
            assert total + adjustment == pytest.approx(record["cost_eur"], abs=1e-6), policy

    def test_report_markdown_declares_bounds_and_window(self, tmp_path: Path) -> None:
        requires_core()
        run_dir = run_benchmark(tmp_path)
        config_path = tmp_path / "tariff.yaml"
        report_dir = rp.generate_report(config_path=str(config_path), benchmark_path=str(run_dir))
        markdown = (report_dir / rp.REPORT_MD_NAME).read_text()
        # Bound vocabulary is present, not glossed over.
        assert "## Solver bounds, tolerances and time limits" in markdown
        assert "NOT a zero gap" in markdown
        # The window is reported as EUR for the window, with no annualisation.
        assert "## EUR is for this window only" in markdown
        assert "Nothing is scaled to a year" in markdown
        assert "/year" not in markdown and "per year" not in markdown.lower()

    def test_report_states_the_utc_scoped_horizon(self, tmp_path: Path) -> None:
        """The horizon's calendar scope is stated, not implied by ``Z`` labels.

        The README rule: a report must say the scope is UTC-scoped when the bounds are
        UTC instants rather than leave the reader to infer it.  The fixture's bounds are
        2026-01-03 → 2026-01-08 UTC, which are 01:00 Europe/Madrid — a local day is cut
        on both edges, so the note must say the window is not a local calendar year.
        """
        requires_core()
        run_dir = run_benchmark(tmp_path)
        config_path = tmp_path / "tariff.yaml"
        report_dir = rp.generate_report(config_path=str(config_path), benchmark_path=str(run_dir))
        markdown = (report_dir / rp.REPORT_MD_NAME).read_text()
        payload = json.loads((report_dir / rp.REPORT_JSON_NAME).read_text())
        note = payload["horizon_scope_note"]
        # One and the same string carries the line into both artifacts.
        assert f"* {note}" in markdown
        assert note.startswith("Horizon scope:")
        assert "UTC-scoped" in note
        assert "not a Europe/Madrid calendar year" in note
        # Derived from the run: both bound labels from the payload appear verbatim.
        evaluation = payload["evaluation"]
        assert evaluation["horizon_start"] in note
        assert evaluation["horizon_end"] in note
        assert "local_day" in note

    def test_horizon_scope_note_is_derived_from_the_run(self) -> None:
        """The note follows the bounds: it does not assert non-locality as a constant."""

        def run_with(horizon_start: str, horizon_end: str, timezone: str | None) -> rp._Run:
            params: dict[str, object] = {}
            if timezone is not None:
                params["timezone"] = timezone
            payload = {
                "evaluation": {"horizon_start": horizon_start, "horizon_end": horizon_end},
                "lineage": {"build_manifest_pinned_fields": {"params": params}},
            }
            return rp._Run(
                directory=Path("."),
                payload=payload,
                results=pa.table({}),
                schedules=pa.table({}),
            )

        # Madrid local midnights: 2025-01-01 00:00 CET is 2024-12-31T23:00Z.  A horizon
        # bounded by two of them IS a local calendar year and the note must say so.
        aligned = rp._horizon_scope_note(
            run_with("2024-12-31T23:00:00Z", "2025-12-31T23:00:00Z", "Europe/Madrid")
        )
        assert "calendar year too" in aligned
        assert "2024" in aligned and "2025" in aligned

        # Bounds on UTC midnights are one hour into the local day on each edge.
        offset = rp._horizon_scope_note(
            run_with("2025-01-01T00:00:00Z", "2026-01-01T00:00:00Z", "Europe/Madrid")
        )
        assert "not a Europe/Madrid calendar year" in offset
        assert "2025-01-01 01:00" in offset
        assert "2026-01-01 01:00" in offset

        # A payload that pins no timezone still states the UTC scope, without inventing a
        # zone name or a verdict about a local year.
        unknown = rp._horizon_scope_note(
            run_with("2025-01-01T00:00:00Z", "2026-01-01T00:00:00Z", None)
        )
        assert "UTC-scoped" in unknown
        assert "Europe/Madrid" not in unknown
        assert "calendar year" in unknown

    def test_unknown_curated_query_raises(self, tmp_path: Path) -> None:
        requires_core()
        run_dir = run_benchmark(tmp_path)
        dataset_dir = run_dir.parent.parent
        with pytest.raises(rp.SQLInputError, match="unknown curated query"):
            rp.run_curated_query(
                "nonexistent_query",
                dataset=str(dataset_dir),
                benchmark=str(run_dir),
            )
