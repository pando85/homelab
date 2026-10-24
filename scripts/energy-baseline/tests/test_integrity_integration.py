"""Integration tests for benchmark → report artifact integrity chain.

Tests the full pipeline: benchmark finalizes its sidecar, report reads and
verifies it, then report finalizes its own sidecar.  The two sidecars are
independent — each covers only its own stage's files.
"""

from __future__ import annotations

import hashlib
import json
from datetime import UTC, datetime, timedelta
from pathlib import Path

import pyarrow as pa
import pyarrow.parquet as pq
import pytest

from energy_baseline import benchmark as bm
from energy_baseline import report as rp
from energy_baseline.artifact_integrity import (
    BENCHMARK_MANIFEST_NAME,
    BENCHMARK_REQUIRED_FILES,
    REPORT_MANIFEST_NAME,
    REPORT_REQUIRED_FILES,
    finalize_run_outputs,
    verify_run_outputs,
)
from energy_baseline.config import BatteryConfig, TariffConfig, TariffPeriods, TaxEntry

# ---------------------------------------------------------------------------
# Helpers (synthetic benchmark run)
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

    madrid = ZoneInfo("Europe/Madrid")
    if gross_kwh is None:
        gross_kwh = [1.0] * intervals
    if prices is None:
        prices = [0.10 + 0.05 * (i % 4) for i in range(intervals)]

    starts = [start + timedelta(seconds=i * interval_seconds) for i in range(intervals)]
    ends = [s + timedelta(seconds=interval_seconds) for s in starts]
    local_days = [s.astimezone(madrid).date() for s in starts]

    table = pa.table(
        {
            "interval_start": pa.array(starts, pa.timestamp("us", tz="UTC")),
            "interval_end": pa.array(ends, pa.timestamp("us", tz="UTC")),
            "local_day": pa.array(local_days, pa.date32()),
            "dataset_id": pa.array([dataset_id] * intervals, pa.string()),
            "gross_kwh": pa.array(gross_kwh, pa.float64()),
            "price_eur_per_kwh": pa.array(prices, pa.float64()),
            "energy_quality": pa.array(["measured"] * intervals, pa.string()),
            "allocation_quality": pa.array(["exact"] * intervals, pa.string()),
            "price_quality": pa.array(["measured"] * intervals, pa.string()),
            "exclusion_reason": pa.array(["usable"] * intervals, pa.string()),
        }
    )
    return table


def write_build_manifest(dataset_dir: Path, table: pa.Table, part_name: str) -> None:
    part_path = dataset_dir / "parts" / part_name
    sha = hashlib.sha256(part_path.read_bytes()).hexdigest()
    manifest = {
        "schema_version": 1,
        "dataset_id": table.column("dataset_id")[0].as_py(),
        "config_hash": "test-config-hash",
        "code_version": "test-code-version",
        "part_files": [
            {
                "path": f"parts/{part_name}",
                "sha256": sha,
                "rows": table.num_rows,
            }
        ],
    }
    (dataset_dir / "build_manifest.json").write_text(json.dumps(manifest, indent=2))


def make_benchmark_run(
    dataset_dir: Path,
    *,
    config: TariffConfig | None = None,
    train_days: int = 30,
    eval_days: int = 30,
    finalize: bool = True,
) -> Path:
    """Create a synthetic benchmark run directory.

    When *finalize* is True (default), the benchmark manifest sidecar is
    written.  When False, the run is left without a manifest (legacy mode).
    """

    if config is None:
        config = make_tariff_config()

    interval_seconds = 900
    total_intervals = (train_days + eval_days) * 96  # 96 intervals per day
    start = datetime(2024, 1, 1, tzinfo=UTC)

    table = make_analysis_table(
        start=start,
        intervals=total_intervals,
        interval_seconds=interval_seconds,
    )

    dataset_id = "test-dataset-001"
    dataset_dir.mkdir(parents=True, exist_ok=True)
    (dataset_dir / "parts").mkdir(exist_ok=True)

    part_name = "part-20240101-20240301-abcd1234.parquet"
    pq.write_table(table, dataset_dir / "parts" / part_name)
    write_build_manifest(dataset_dir, table, part_name)

    config_path = dataset_dir / "tariff.yaml"
    write_tariff_config(config_path, config)

    train_end = start + timedelta(days=train_days)
    run_dir = dataset_dir / "benchmarks" / "test-run-001"
    run_dir.mkdir(parents=True, exist_ok=True)

    # Write minimal benchmark artifacts (synthetic, not from actual benchmark_dataset)
    payload = {
        "run_id": "test-run-001",
        "benchmark_version": bm.BENCHMARK_VERSION,
        "lineage": {
            "dataset_id": dataset_id,
            "config_hash": "test-config-hash",
            "code_version": "test-code-version",
            "build_manifest_pinned_fields": {"params": {"timezone": "Europe/Madrid"}},
        },
        "evaluation": {
            "horizon_start": (start + timedelta(days=train_days)).isoformat(),
            "horizon_end": start.isoformat(),
            "interval_count": eval_days * 96,
            "usable_interval_count": eval_days * 96,
            "excluded_interval_count": 0,
            "months_included": ["2024-02"],
            "window_is_partial_year": False,
            "scaling_note": "test",
        },
        "training": {
            "train_end": train_end.isoformat(),
            "train_days": train_days,
            "usable_interval_count": train_days * 96,
            "charge_threshold_eur_per_kwh": 0.12,
            "charge_pct": 50.0,
            "discharge_threshold_eur_per_kwh": 0.08,
            "discharge_pct": 50.0,
            "leakage_note": "test",
        },
        "assumptions": {
            "battery": {
                "capacity_kwh": config.battery.capacity_kwh,
                "power_kw": config.battery.power_kw,
                "charge_efficiency": 0.95,
                "discharge_efficiency": 0.95,
                "soc_min_frac": config.battery.soc_window_min,
                "soc_max_frac": config.battery.soc_window_max,
                "initial_soc_frac": config.battery.soc_window_min,
                "import_limit_kw": config.battery.import_limit_kw,
                "idle_loss_kwh_per_hour": 0.0,
                "idle_losses_measured": False,
                "efficiency_mapping": "test",
                "initial_soc_source": "test",
                "import_limit_source": "test",
            },
            "cost_objective": {
                "cost_basis": "untaxed",
                "uniform_multiplier": None,
                "note": "test",
            },
            "gross_load_basis": "test",
            "idle_losses_note": "test",
            "exclusions_are_neutral_carry": "test",
            "no_intentional_export": "test",
            "soc_window_and_efficiency_are_distinct": "test",
            "site_import_limit": "test",
            "terminal_accounting": {"used": "free"},
        },
        "policies": {
            "no_battery": {
                "cost_eur": 100.0,
                "efc": 0.0,
                "throughput_kwh": 0.0,
                "solver_status": "optimal",
                "bound_kind": "none",
                "is_certified": False,
                "inventory_adjustment_eur": 0.0,
            },
            "oracle": {
                "cost_eur": 80.0,
                "efc": 10.0,
                "throughput_kwh": 50.0,
                "solver_status": "optimal",
                "bound_kind": "certified",
                "is_certified": True,
                "inventory_adjustment_eur": 0.0,
                "soc_min_frac": 0.05,
                "soc_max_frac": 0.95,
                "initial_soc_kwh": 0.256,
                "terminal_soc_kwh": 0.256,
            },
        },
        "comparison": {
            "baseline_cost_eur": 100.0,
            "basis": "untaxed",
            "oracle_dominates_baseline": True,
            "oracle_dominates_causal": True,
            "dominance_note": "test",
            "per_policy": {
                "oracle": {
                    "savings_eur": 20.0,
                    "savings_lower_bound_eur": 20.0,
                    "savings_upper_bound_eur": 20.0,
                    "worse_than_baseline": False,
                },
            },
        },
        "quality": {
            "per_month": [
                {
                    "month": "2024-02",
                    "intervals_total": eval_days * 96,
                    "intervals_usable": eval_days * 96,
                    "intervals_excluded": 0,
                    "usable_fraction": 1.0,
                    "in_evaluation": True,
                }
            ],
            "declared_thresholds": {"min_usable_fraction": 0.0, "note": "test"},
            "eval_exclusion_reasons": {},
        },
        "solver": {"errors": {}},
    }

    (run_dir / bm.RESULTS_JSON_NAME).write_text(json.dumps(payload, indent=2))

    # Write minimal parquet files
    results_table = pa.table(
        {
            "policy": ["no_battery", "oracle"],
            "cost_eur": [100.0, 80.0],
            "cost_basis": ["untaxed", "untaxed"],
            "efc": [0.0, 10.0],
            "throughput_kwh": [0.0, 50.0],
            "solver_status": ["optimal", "optimal"],
            "bound_kind": ["none", "certified"],
            "is_certified": [False, True],
        }
    )
    pq.write_table(results_table, run_dir / bm.RESULTS_PARQUET_NAME)

    # Schedules table with per-interval data
    schedules_rows = []
    for i in range(eval_days * 96):
        for policy in ["no_battery", "oracle"]:
            schedules_rows.append(
                {
                    "interval_start": start + timedelta(days=train_days, seconds=i * 900),
                    "interval_end": start + timedelta(days=train_days, seconds=(i + 1) * 900),
                    "local_day": (start + timedelta(days=train_days + i // 96)).date(),
                    "policy": policy,
                    "usable": True,
                    "exclusion_reason": "usable",
                    "price_eur_per_kwh": 0.10 + 0.05 * (i % 4),
                    "gross_kwh": 1.0,
                    "grid_kwh": 0.5 if policy == "no_battery" else 0.3,
                    "charge_kwh": 0.0 if policy == "no_battery" else 0.2,
                    "discharge_kwh": 0.0 if policy == "no_battery" else 0.2,
                    "interval_cost_taxed_eur": None,
                }
            )

    schedules_table = pa.table(
        {
            "interval_start": pa.array(
                [r["interval_start"] for r in schedules_rows], pa.timestamp("us", tz="UTC")
            ),
            "interval_end": pa.array(
                [r["interval_end"] for r in schedules_rows], pa.timestamp("us", tz="UTC")
            ),
            "local_day": pa.array([r["local_day"] for r in schedules_rows], pa.date32()),
            "policy": pa.array([r["policy"] for r in schedules_rows], pa.string()),
            "usable": pa.array([r["usable"] for r in schedules_rows], pa.bool_()),
            "exclusion_reason": pa.array(
                [r["exclusion_reason"] for r in schedules_rows], pa.string()
            ),
            "price_eur_per_kwh": pa.array(
                [r["price_eur_per_kwh"] for r in schedules_rows], pa.float64()
            ),
            "gross_kwh": pa.array([r["gross_kwh"] for r in schedules_rows], pa.float64()),
            "grid_kwh": pa.array([r["grid_kwh"] for r in schedules_rows], pa.float64()),
            "charge_kwh": pa.array([r["charge_kwh"] for r in schedules_rows], pa.float64()),
            "discharge_kwh": pa.array([r["discharge_kwh"] for r in schedules_rows], pa.float64()),
            "interval_cost_taxed_eur": pa.array(
                [r["interval_cost_taxed_eur"] for r in schedules_rows], pa.float64()
            ),
        }
    )
    pq.write_table(schedules_table, run_dir / bm.SCHEDULES_PARQUET_NAME)

    if finalize:
        finalize_run_outputs(run_dir, BENCHMARK_REQUIRED_FILES)

    return run_dir


# ---------------------------------------------------------------------------
# Integration tests
# ---------------------------------------------------------------------------


class TestBenchmarkReportIntegration:
    """Full pipeline: benchmark → report, both manifests valid."""

    def test_both_manifests_valid(self, tmp_path: Path) -> None:
        """Benchmark finalizes, report reads and verifies, then report finalizes."""
        dataset_dir = tmp_path / "dataset"
        run_dir = make_benchmark_run(dataset_dir, finalize=True)

        # Benchmark manifest exists and is valid
        benchmark_manifest = run_dir / BENCHMARK_MANIFEST_NAME
        assert benchmark_manifest.is_file()
        benchmark_result = verify_run_outputs(run_dir, BENCHMARK_REQUIRED_FILES)
        assert benchmark_result.ok

        # Generate report — should verify benchmark manifest and create report manifest
        report_dir = rp.generate_report(benchmark_path=run_dir)
        assert report_dir == run_dir

        # Report manifest exists and is valid
        report_manifest = run_dir / REPORT_MANIFEST_NAME
        assert report_manifest.is_file()
        report_result = verify_run_outputs(
            run_dir, REPORT_REQUIRED_FILES, manifest_name=REPORT_MANIFEST_NAME
        )
        assert report_result.ok

        # Benchmark manifest still valid (report didn't clobber it)
        benchmark_result_after = verify_run_outputs(run_dir, BENCHMARK_REQUIRED_FILES)
        assert benchmark_result_after.ok

        # Report payload includes integrity metadata
        report_payload = json.loads((run_dir / rp.REPORT_JSON_NAME).read_text())
        assert report_payload["artifact_integrity"]["benchmark_manifest_verified"] is True
        assert report_payload["artifact_integrity"]["benchmark_manifest_present"] is True

    def test_regenerate_report_idempotent(self, tmp_path: Path) -> None:
        """Regenerating the report produces the same bytes and manifest."""
        dataset_dir = tmp_path / "dataset"
        run_dir = make_benchmark_run(dataset_dir, finalize=True)

        # First generation
        rp.generate_report(benchmark_path=run_dir)
        report_json_sha_1 = hashlib.sha256((run_dir / rp.REPORT_JSON_NAME).read_bytes()).hexdigest()
        report_md_sha_1 = hashlib.sha256((run_dir / rp.REPORT_MD_NAME).read_bytes()).hexdigest()
        report_manifest_sha_1 = hashlib.sha256(
            (run_dir / REPORT_MANIFEST_NAME).read_bytes()
        ).hexdigest()

        # Second generation — should be idempotent
        rp.generate_report(benchmark_path=run_dir)
        report_json_sha_2 = hashlib.sha256((run_dir / rp.REPORT_JSON_NAME).read_bytes()).hexdigest()
        report_md_sha_2 = hashlib.sha256((run_dir / rp.REPORT_MD_NAME).read_bytes()).hexdigest()
        report_manifest_sha_2 = hashlib.sha256(
            (run_dir / REPORT_MANIFEST_NAME).read_bytes()
        ).hexdigest()

        assert report_json_sha_1 == report_json_sha_2
        assert report_md_sha_1 == report_md_sha_2
        assert report_manifest_sha_1 == report_manifest_sha_2

    def test_tampered_benchmark_prevents_report(self, tmp_path: Path) -> None:
        """Tampering with benchmark artifacts prevents report generation."""
        dataset_dir = tmp_path / "dataset"
        run_dir = make_benchmark_run(dataset_dir, finalize=True)

        # Tamper with a benchmark file
        benchmark_json = run_dir / bm.RESULTS_JSON_NAME
        original_content = benchmark_json.read_bytes()
        benchmark_json.write_bytes(original_content + b"\n# tampered")

        # Report generation should fail with integrity error
        with pytest.raises(rp.ReportError, match="benchmark manifest verification failed"):
            rp.generate_report(benchmark_path=run_dir)

        # Report files should not exist
        assert not (run_dir / rp.REPORT_JSON_NAME).exists()
        assert not (run_dir / rp.REPORT_MD_NAME).exists()
        assert not (run_dir / REPORT_MANIFEST_NAME).exists()

    def test_legacy_run_no_manifest_unverified(self, tmp_path: Path) -> None:
        """Legacy run without benchmark manifest is readable but unverified."""
        dataset_dir = tmp_path / "dataset"
        run_dir = make_benchmark_run(dataset_dir, finalize=False)

        # No benchmark manifest
        assert not (run_dir / BENCHMARK_MANIFEST_NAME).exists()

        # Report generation should succeed
        report_dir = rp.generate_report(benchmark_path=run_dir)
        assert report_dir == run_dir

        # Report manifest exists
        assert (run_dir / REPORT_MANIFEST_NAME).is_file()

        # Report payload indicates unverified provenance
        report_payload = json.loads((run_dir / rp.REPORT_JSON_NAME).read_text())
        assert report_payload["artifact_integrity"]["benchmark_manifest_verified"] is False
        assert report_payload["artifact_integrity"]["benchmark_manifest_present"] is False

    def test_report_manifest_does_not_clobber_benchmark(self, tmp_path: Path) -> None:
        """Report manifest is separate and doesn't affect benchmark verification."""
        dataset_dir = tmp_path / "dataset"
        run_dir = make_benchmark_run(dataset_dir, finalize=True)

        # Record benchmark manifest before report
        benchmark_manifest_before = (run_dir / BENCHMARK_MANIFEST_NAME).read_bytes()

        # Generate report
        rp.generate_report(benchmark_path=run_dir)

        # Benchmark manifest unchanged
        benchmark_manifest_after = (run_dir / BENCHMARK_MANIFEST_NAME).read_bytes()
        assert benchmark_manifest_before == benchmark_manifest_after

        # Both manifests verify independently
        benchmark_result = verify_run_outputs(run_dir, BENCHMARK_REQUIRED_FILES)
        assert benchmark_result.ok
        assert benchmark_result.manifest_name == BENCHMARK_MANIFEST_NAME

        report_result = verify_run_outputs(
            run_dir, REPORT_REQUIRED_FILES, manifest_name=REPORT_MANIFEST_NAME
        )
        assert report_result.ok
        assert report_result.manifest_name == REPORT_MANIFEST_NAME

        # Report manifest covers only report files, not benchmark files
        report_manifest_payload = json.loads((run_dir / REPORT_MANIFEST_NAME).read_text())
        report_filenames = {e["filename"] for e in report_manifest_payload["files"]}
        assert report_filenames == set(REPORT_REQUIRED_FILES)
        assert bm.RESULTS_JSON_NAME not in report_filenames
        assert bm.RESULTS_PARQUET_NAME not in report_filenames
        assert bm.SCHEDULES_PARQUET_NAME not in report_filenames

    def test_no_absolute_paths_in_manifests(self, tmp_path: Path) -> None:
        """Manifests must not contain absolute paths."""
        dataset_dir = tmp_path / "dataset"
        run_dir = make_benchmark_run(dataset_dir, finalize=True)
        rp.generate_report(benchmark_path=run_dir)

        # Check benchmark manifest
        benchmark_manifest = json.loads((run_dir / BENCHMARK_MANIFEST_NAME).read_text())
        for entry in benchmark_manifest["files"]:
            filename = entry["filename"]
            assert not Path(filename).is_absolute(), (
                f"absolute path in benchmark manifest: {filename}"
            )

        # Check report manifest
        report_manifest = json.loads((run_dir / REPORT_MANIFEST_NAME).read_text())
        for entry in report_manifest["files"]:
            filename = entry["filename"]
            assert not Path(filename).is_absolute(), f"absolute path in report manifest: {filename}"

    def test_benchmark_verify_still_works_after_report(self, tmp_path: Path) -> None:
        """Benchmark verification works after report generation."""
        dataset_dir = tmp_path / "dataset"
        run_dir = make_benchmark_run(dataset_dir, finalize=True)

        # Verify benchmark before report
        result_before = verify_run_outputs(run_dir, BENCHMARK_REQUIRED_FILES)
        assert result_before.ok

        # Generate report
        rp.generate_report(benchmark_path=run_dir)

        # Verify benchmark after report — should still work
        result_after = verify_run_outputs(run_dir, BENCHMARK_REQUIRED_FILES)
        assert result_after.ok
        assert result_after.hash_mismatches == []
        assert result_after.size_mismatches == []
        assert result_after.missing_required == []


class TestManifestTampering:
    """Manifest-level tampering must prevent report generation."""

    def test_manifest_entry_tampered_prevents_report(self, tmp_path: Path) -> None:
        """Editing the manifest itself (not a hashed file) must be detected."""
        dataset_dir = tmp_path / "dataset"
        run_dir = make_benchmark_run(dataset_dir, finalize=True)

        # Tamper with the manifest — change a sha256 value
        manifest_path = run_dir / BENCHMARK_MANIFEST_NAME
        original = json.loads(manifest_path.read_text())
        # Modify first file's sha to a bogus-but-valid-format hash
        original["files"][0]["sha256"] = "0" * 64
        manifest_path.write_text(json.dumps(original, indent=2))

        # Report generation should fail because the manifest's claimed sha
        # doesn't match the actual file content
        with pytest.raises(rp.ReportError, match="benchmark manifest verification failed"):
            rp.generate_report(benchmark_path=run_dir)

    def test_manifest_schema_version_tampered(self, tmp_path: Path) -> None:
        """Unsupported manifest schema version must prevent report."""
        dataset_dir = tmp_path / "dataset"
        run_dir = make_benchmark_run(dataset_dir, finalize=True)

        manifest_path = run_dir / BENCHMARK_MANIFEST_NAME
        original = json.loads(manifest_path.read_text())
        original["schema_version"] = 9999
        manifest_path.write_text(json.dumps(original, indent=2))

        with pytest.raises(rp.ReportError, match="benchmark manifest verification failed"):
            rp.generate_report(benchmark_path=run_dir)


class TestReportStageVerification:
    """Report sidecar verification works and covers only report files."""

    def test_report_manifest_hash_matches_files(self, tmp_path: Path) -> None:
        """Report manifest hashes are correct for the written report files."""
        dataset_dir = tmp_path / "dataset"
        run_dir = make_benchmark_run(dataset_dir, finalize=True)
        rp.generate_report(benchmark_path=run_dir)

        manifest = json.loads((run_dir / REPORT_MANIFEST_NAME).read_text())
        for entry in manifest["files"]:
            file_path = run_dir / entry["filename"]
            assert file_path.is_file()
            actual_sha = hashlib.sha256(file_path.read_bytes()).hexdigest()
            actual_size = file_path.stat().st_size
            assert entry["sha256"] == actual_sha
            assert entry["bytes"] == actual_size

    def test_report_manifest_excludes_itself(self, tmp_path: Path) -> None:
        """The report manifest must not list itself as a hashed file."""
        dataset_dir = tmp_path / "dataset"
        run_dir = make_benchmark_run(dataset_dir, finalize=True)
        rp.generate_report(benchmark_path=run_dir)

        manifest = json.loads((run_dir / REPORT_MANIFEST_NAME).read_text())
        filenames = [e["filename"] for e in manifest["files"]]
        assert REPORT_MANIFEST_NAME not in filenames
        assert BENCHMARK_MANIFEST_NAME not in filenames

    def test_tampered_report_file_detected_by_own_manifest(self, tmp_path: Path) -> None:
        """After report generation, tampering report.md is caught by report manifest."""
        dataset_dir = tmp_path / "dataset"
        run_dir = make_benchmark_run(dataset_dir, finalize=True)
        rp.generate_report(benchmark_path=run_dir)

        # Tamper with report.md after generation
        report_md = run_dir / rp.REPORT_MD_NAME
        report_md.write_text("# tampered report")

        # Verify report manifest should fail
        result = verify_run_outputs(
            run_dir, REPORT_REQUIRED_FILES, manifest_name=REPORT_MANIFEST_NAME
        )
        assert not result.ok
        assert rp.REPORT_MD_NAME in result.hash_mismatches

        # But benchmark manifest should still verify cleanly
        bm_result = verify_run_outputs(run_dir, BENCHMARK_REQUIRED_FILES)
        assert bm_result.ok
