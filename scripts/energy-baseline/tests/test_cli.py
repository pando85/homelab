"""Offline tests for the CLI entry point.

Tests use ``httpx.MockTransport`` where HTTP is involved, never the live
server.  No secrets are logged or leaked in error output.


The benchmark CLI tests cover the day-ahead flags as well: policy names, the forecast
window, the per-day time limit, and the distinction between "no terminal value given"
(derive it from the training window) and "0.0 given" (a deliberate cash-flow objective)."""

from __future__ import annotations

import hashlib
import json
from datetime import UTC, datetime
from io import BytesIO
from pathlib import Path

import httpx
import pyarrow as pa
import pyarrow.parquet as pq
import pytest

from energy_baseline.cli import (
    _redact_error,
    build_parser,
    main,
    parse_entities,
    resolve_time_window,
)
from energy_baseline.manifest import new_manifest, write_manifest
from energy_baseline.paths import part_path, write_part_atomic
from energy_baseline.schemas import RAW_ENUM_STATE, RAW_SAMPLE

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

_SENTINEL_TOKEN = "SUPER_SECRET_TOKEN_XYZ_9876"


def _make_raw_sample_part(data_dir: Path) -> Path:
    """Write a minimal valid RAW_SAMPLE part file."""
    table = pa.table(
        {
            "entity": pa.array(["sensor.test"], pa.string()),
            "metric": pa.array(["test_metric"], pa.string()),
            "unit": pa.array(["kWh"], pa.string()),
            "series_key": pa.array(["key1"], pa.string()),
            "ts": pa.array(
                [datetime(2026, 1, 1, 0, 0, 0, tzinfo=UTC)],
                type=pa.timestamp("us", tz="UTC"),
            ),
            "value": pa.array([1.0], type=pa.float64()),
            "scrape_instance": pa.array(["inst:9090"], pa.string()),
            "extraction_id": pa.array(["test-extraction-1"], pa.string()),
            "labels_json": pa.array(['{"__name__":"test"}'], pa.string()),
        },
        schema=RAW_SAMPLE,
    )
    sink = BytesIO()
    pq.write_table(table, sink)
    payload = sink.getvalue()
    sha = hashlib.sha256(payload).hexdigest()
    path = part_path(
        data_dir,
        "prometheus",
        "prom-samples",
        datetime(2026, 1, 1, tzinfo=UTC),
        datetime(2026, 1, 2, tzinfo=UTC),
        sha[:8],
    )
    write_part_atomic(path, payload)
    return path


def _make_raw_enum_part(data_dir: Path) -> Path:
    """Write a minimal valid RAW_ENUM_STATE part file."""
    table = pa.table(
        {
            "entity": pa.array(["sensor.washer"], pa.string()),
            "state": pa.array(["idle"], pa.string()),
            "attributes_json": pa.array([None], pa.string()),
            "last_changed": pa.array(
                [datetime(2026, 1, 1, 10, 0, 0, tzinfo=UTC)],
                type=pa.timestamp("us", tz="UTC"),
            ),
            "last_updated": pa.array(
                [datetime(2026, 1, 1, 10, 5, 0, tzinfo=UTC)],
                type=pa.timestamp("us", tz="UTC"),
            ),
            "extraction_id": pa.array(["test-extraction-2"], pa.string()),
            "carry_in": pa.array([False], type=pa.bool_()),
            "source_window_start": pa.array(
                [datetime(2026, 1, 1, tzinfo=UTC)],
                type=pa.timestamp("us", tz="UTC"),
            ),
        },
        schema=RAW_ENUM_STATE,
    )
    sink = BytesIO()
    pq.write_table(table, sink)
    payload = sink.getvalue()
    sha = hashlib.sha256(payload).hexdigest()
    path = part_path(
        data_dir,
        "ha_history",
        "ha-enum",
        datetime(2026, 1, 1, tzinfo=UTC),
        datetime(2026, 1, 2, tzinfo=UTC),
        sha[:8],
    )
    write_part_atomic(path, payload)
    return path


def _write_manifest(data_dir: Path, source: str, rel: str, sha256: str) -> Path:
    """Write a minimal manifest referencing one part file."""
    from energy_baseline.config import resolve_config_path

    cfg = resolve_config_path(
        str(Path(__file__).resolve().parent.parent / "config" / "tariff.yaml")
    )
    manifest = new_manifest(
        source=source,
        config_path=cfg,
        params={"test": True},
        entities=["sensor.test"],
        source_window_start="2026-01-01T00:00:00Z",
        source_window_end="2026-01-02T00:00:00Z",
        started_at="2026-01-02T00:00:00Z",
    )
    manifest.row_count = 1
    manifest.finished_at = "2026-01-02T00:01:00Z"
    manifest.part_files = [
        {"path": rel, "name": Path(rel).name, "kind": "test", "sha256": sha256, "rows": 1}
    ]
    return write_manifest(data_dir, manifest)


# ---------------------------------------------------------------------------
# Tests: parse_entities
# ---------------------------------------------------------------------------


class TestParseEntities:
    def test_none_returns_empty(self):
        assert parse_entities(None) == []

    def test_empty_list_returns_empty(self):
        assert parse_entities([]) == []

    def test_single_entity(self):
        assert parse_entities(["sensor.foo"]) == ["sensor.foo"]

    def test_comma_separated(self):
        assert parse_entities(["sensor.foo,sensor.bar"]) == ["sensor.foo", "sensor.bar"]

    def test_repeatable(self):
        assert parse_entities(["sensor.foo", "sensor.bar"]) == ["sensor.foo", "sensor.bar"]

    def test_mixed(self):
        result = parse_entities(["sensor.a,sensor.b", "sensor.c"])
        assert result == ["sensor.a", "sensor.b", "sensor.c"]

    def test_strips_whitespace(self):
        assert parse_entities([" sensor.a , sensor.b "]) == ["sensor.a", "sensor.b"]


# ---------------------------------------------------------------------------
# Tests: resolve_time_window
# ---------------------------------------------------------------------------


class TestResolveTimeWindow:
    def test_explicit_start_end(self):
        start, end = resolve_time_window("2026-01-01T00:00:00Z", "2026-01-02T00:00:00Z", None)
        assert start == "2026-01-01T00:00:00Z"
        assert end == "2026-01-02T00:00:00Z"

    def test_start_without_end_defaults_to_now(self):
        start, end = resolve_time_window("2026-01-01T00:00:00Z", None, None)
        assert start == "2026-01-01T00:00:00Z"
        assert end is not None

    def test_lookback_days(self):
        start, end = resolve_time_window(None, None, 9)
        assert "T00:00:00" in start
        assert end is not None

    def test_no_args_raises(self):
        with pytest.raises(ValueError, match="required"):
            resolve_time_window(None, None, None)

    def test_lookback_days_zero_raises(self):
        with pytest.raises(ValueError, match=">= 1"):
            resolve_time_window(None, None, 0)


# ---------------------------------------------------------------------------
# Tests: _redact_error
# ---------------------------------------------------------------------------


class TestRedactError:
    def test_bearer_token_redacted(self):
        msg = "Authorization: Bearer my_secret_token_123 failed"
        result = _redact_error(msg)
        assert "my_secret_token_123" not in result
        assert "<REDACTED>" in result

    def test_password_param_redacted(self):
        msg = "connection failed: password=hunter2 in URL"
        result = _redact_error(msg)
        assert "hunter2" not in result
        assert "<REDACTED>" in result

    def test_token_param_redacted(self):
        msg = "error with token=abc123def in request"
        result = _redact_error(msg)
        assert "abc123def" not in result

    def test_clean_message_unchanged(self):
        msg = "connection timeout after 30s"
        assert _redact_error(msg) == msg


# ---------------------------------------------------------------------------
# Tests: parser construction
# ---------------------------------------------------------------------------


class TestParser:
    def test_no_command_returns_zero(self):
        assert main([]) == 0

    def test_extract_requires_source(self):
        with pytest.raises(SystemExit):
            main(["extract"])

    def test_extract_source_choices(self):
        parser = build_parser()
        args = parser.parse_args(
            ["extract", "--source", "prometheus", "--start", "2026-01-01T00:00:00Z"]
        )
        assert args.source == "prometheus"

    def test_validate_source_default(self):
        parser = build_parser()
        args = parser.parse_args(["validate"])
        assert args.source == "all"

    def test_lookback_days_parsed(self):
        parser = build_parser()
        args = parser.parse_args(["extract", "--source", "prometheus", "--lookback-days", "9"])
        assert args.lookback_days == 9

    def test_start_and_lookback_mutually_exclusive(self):
        with pytest.raises(SystemExit):
            main(
                [
                    "extract",
                    "--source",
                    "prometheus",
                    "--start",
                    "2026-01-01T00:00:00Z",
                    "--lookback-days",
                    "9",
                ]
            )


# ---------------------------------------------------------------------------
# Tests: cmd_validate with real data
# ---------------------------------------------------------------------------


class TestCmdValidate:
    def test_validate_prometheus_parts(self, tmp_path, monkeypatch):
        data_dir = tmp_path / "data"
        data_dir.mkdir()
        monkeypatch.setenv("ENERGY_BASELINE_DATA_DIR", str(data_dir))
        part = _make_raw_sample_part(data_dir)
        sha = hashlib.sha256(part.read_bytes()).hexdigest()
        rel = str(part.relative_to(data_dir))
        _write_manifest(data_dir, "prometheus", rel, sha)
        assert main(["validate", "--source", "prometheus"]) == 0

    def test_validate_ha_parts(self, tmp_path, monkeypatch):
        data_dir = tmp_path / "data"
        data_dir.mkdir()
        monkeypatch.setenv("ENERGY_BASELINE_DATA_DIR", str(data_dir))
        part = _make_raw_enum_part(data_dir)
        sha = hashlib.sha256(part.read_bytes()).hexdigest()
        rel = str(part.relative_to(data_dir))
        _write_manifest(data_dir, "ha_history", rel, sha)
        assert main(["validate", "--source", "ha-history"]) == 0

    def test_validate_no_parts_fails(self, tmp_path, monkeypatch):
        data_dir = tmp_path / "data"
        data_dir.mkdir()
        monkeypatch.setenv("ENERGY_BASELINE_DATA_DIR", str(data_dir))
        assert main(["validate", "--source", "prometheus"]) == 1

    def test_validate_manifest_hash_mismatch(self, tmp_path, monkeypatch):
        """Manifest hash mismatch against actual bytes yields exit 1."""
        data_dir = tmp_path / "data"
        data_dir.mkdir()
        monkeypatch.setenv("ENERGY_BASELINE_DATA_DIR", str(data_dir))
        part = _make_raw_sample_part(data_dir)
        rel = str(part.relative_to(data_dir))
        _write_manifest(data_dir, "prometheus", rel, "deadbeef" * 8)
        assert main(["validate", "--source", "prometheus"]) == 1


# ---------------------------------------------------------------------------
# Tests: cmd_extract error handling (no live HTTP)
# ---------------------------------------------------------------------------


class TestCmdExtractErrors:
    def test_extract_no_time_args_fails(self, tmp_path, monkeypatch, capsys):
        data_dir = tmp_path / "data"
        data_dir.mkdir()
        monkeypatch.setenv("ENERGY_BASELINE_DATA_DIR", str(data_dir))
        rc = main(["extract", "--source", "prometheus"])
        assert rc == 1
        captured = capsys.readouterr()
        assert "required" in captured.err.lower() or "lookback" in captured.err.lower()

    def test_extract_ha_no_token_fails(self, tmp_path, monkeypatch, capsys):
        """Extract ha-history without token fails with redacted error."""
        data_dir = tmp_path / "data"
        data_dir.mkdir()
        monkeypatch.setenv("ENERGY_BASELINE_DATA_DIR", str(data_dir))
        monkeypatch.delenv("ENERGY_BASELINE_HA_TOKEN", raising=False)
        cfg = str(Path(__file__).resolve().parent.parent / "config" / "tariff.yaml")
        rc = main(
            [
                "extract",
                "--source",
                "ha-history",
                "--start",
                "2026-01-01T00:00:00Z",
                "--end",
                "2026-01-02T00:00:00Z",
                "--config",
                cfg,
            ]
        )
        assert rc == 1
        captured = capsys.readouterr()
        assert "token" in captured.err.lower() or "bearer" in captured.err.lower()
        assert _SENTINEL_TOKEN not in captured.err

    def test_extract_error_does_not_leak_token(self):
        msg = f"HA authentication failed: Bearer {_SENTINEL_TOKEN}"
        redacted = _redact_error(msg)
        assert _SENTINEL_TOKEN not in redacted

    def test_extract_config_missing_fails(self, tmp_path, monkeypatch, capsys):
        """Extract with a nonexistent config path fails cleanly (exit 1)."""
        data_dir = tmp_path / "data"
        data_dir.mkdir()
        monkeypatch.setenv("ENERGY_BASELINE_DATA_DIR", str(data_dir))
        rc = main(
            [
                "extract",
                "--source",
                "prometheus",
                "--start",
                "2026-01-01T00:00:00Z",
                "--end",
                "2026-01-02T00:00:00Z",
                "--config",
                "/nonexistent/tariff.yaml",
            ]
        )
        assert rc == 1
        captured = capsys.readouterr()
        assert "not found" in captured.err.lower() or "tariff" in captured.err.lower()

    def test_extract_runtime_error_redacted(self, tmp_path, monkeypatch, capsys):
        """RuntimeError from extractor is caught, redacted, no traceback leaks."""
        data_dir = tmp_path / "data"
        data_dir.mkdir()
        monkeypatch.setenv("ENERGY_BASELINE_DATA_DIR", str(data_dir))
        cfg = str(Path(__file__).resolve().parent.parent / "config" / "tariff.yaml")

        import energy_baseline.extract_ha as _eh

        def boom(**_kw):
            raise RuntimeError(f"HA auth failed: Bearer {_SENTINEL_TOKEN} password=P@ss secret=xyz")

        monkeypatch.setattr(_eh, "extract_ha_history", boom)

        rc = main(
            [
                "extract",
                "--source",
                "ha-history",
                "--start",
                "2026-01-01T00:00:00Z",
                "--end",
                "2026-01-02T00:00:00Z",
                "--config",
                cfg,
            ]
        )
        assert rc == 1
        captured = capsys.readouterr()
        assert "Traceback" not in captured.err
        assert _SENTINEL_TOKEN not in captured.err
        assert "P@ss" not in captured.err
        assert "<REDACTED>" in captured.err


# ---------------------------------------------------------------------------
# Tests: main() exit codes for stubs
# ---------------------------------------------------------------------------


class TestMainExitCodes:
    def test_mirror_deferred(self, capsys):
        rc = main(["mirror"])
        assert rc == 0
        captured = capsys.readouterr()
        assert "deferred" in captured.out


# ---------------------------------------------------------------------------
# Tests: benchmark CLI wiring
# ---------------------------------------------------------------------------


class TestBenchmarkCLI:
    def test_requires_train_end(self):
        with pytest.raises(SystemExit) as exc:
            main(["benchmark", "--dataset", "/tmp/nope"])
        assert exc.value.code == 2

    def test_terminal_mode_invalid(self):
        with pytest.raises(SystemExit) as exc:
            main(
                [
                    "benchmark",
                    "--dataset",
                    "/tmp/nope",
                    "--train-end",
                    "2026-01-01T00:00:00+00:00",
                    "--terminal-mode",
                    "bogus",
                ]
            )
        assert exc.value.code == 2

    def test_core_unavailable_message(self, monkeypatch, capsys):
        from energy_baseline import benchmark

        monkeypatch.setattr(benchmark, "core_available", lambda: False)
        called = {"benchmark_dataset": False}

        def boom(**_kw):
            called["benchmark_dataset"] = True
            return Path("/tmp/nope")

        monkeypatch.setattr(benchmark, "benchmark_dataset", boom)

        rc = main(
            [
                "benchmark",
                "--dataset",
                "/tmp/nope",
                "--train-end",
                "2026-01-01T00:00:00+00:00",
            ]
        )
        assert rc == 1
        captured = capsys.readouterr()
        assert "numeric core" in captured.err
        assert called["benchmark_dataset"] is False

    @pytest.mark.parametrize(
        "flag, value, kwarg, expected",
        [
            ("--train-days", "7", "train_days", 7),
            ("--charge-pct", "20.0", "charge_pct", 20.0),
            ("--discharge-pct", "80.0", "discharge_pct", 80.0),
            ("--initial-soc-frac", "0.4", "initial_soc_frac", 0.4),
            ("--import-limit-kw", "5.5", "import_limit_kw", 5.5),
            ("--min-usable-fraction", "0.5", "min_usable_fraction", 0.5),
            ("--time-limit-s", "60.0", "time_limit_s", 60.0),
            ("--mip-rel-gap", "1e-4", "mip_rel_gap", 1e-4),
            ("--mip-abs-gap", "1e-7", "mip_abs_gap", 1e-7),
        ],
    )
    def test_flag_maps_to_keyword(self, monkeypatch, flag, value, kwarg, expected):
        from energy_baseline import benchmark

        monkeypatch.setattr(benchmark, "core_available", lambda: True)
        captured = {}

        def fake(**kw):
            captured.update(kw)
            return Path("/tmp/run")

        monkeypatch.setattr(benchmark, "benchmark_dataset", fake)
        rc = main(
            [
                "benchmark",
                "--dataset",
                "/tmp/ds",
                "--train-end",
                "2026-01-01T00:00:00+00:00",
                flag,
                value,
            ]
        )
        assert rc == 0
        assert captured[kwarg] == expected

    def test_allow_reconstructed_quality_flag(self, monkeypatch):
        from energy_baseline import benchmark

        monkeypatch.setattr(benchmark, "core_available", lambda: True)
        captured = {}

        def fake(**kw):
            captured.update(kw)
            return Path("/tmp/run")

        monkeypatch.setattr(benchmark, "benchmark_dataset", fake)
        rc = main(
            [
                "benchmark",
                "--dataset",
                "/tmp/ds",
                "--train-end",
                "2026-01-01T00:00:00+00:00",
                "--allow-reconstructed-quality",
            ]
        )
        assert rc == 0
        assert captured["allow_reconstructed_quality"] is True

    def test_terminal_mode_free_and_fixed(self, monkeypatch):
        from energy_baseline import benchmark

        monkeypatch.setattr(benchmark, "core_available", lambda: True)
        captured = {}

        def fake(**kw):
            captured.update(kw)
            return Path("/tmp/run")

        monkeypatch.setattr(benchmark, "benchmark_dataset", fake)
        for mode in ("free", "fixed"):
            captured.clear()
            rc = main(
                [
                    "benchmark",
                    "--dataset",
                    "/tmp/ds",
                    "--train-end",
                    "2026-01-01T00:00:00+00:00",
                    "--terminal-mode",
                    mode,
                ]
            )
            assert rc == 0
            assert captured["terminal_mode"] == mode

    def test_defaults_from_module(self, monkeypatch):
        from energy_baseline import benchmark

        monkeypatch.setattr(benchmark, "core_available", lambda: True)
        captured = {}

        def fake(**kw):
            captured.update(kw)
            return Path("/tmp/run")

        monkeypatch.setattr(benchmark, "benchmark_dataset", fake)
        rc = main(
            [
                "benchmark",
                "--dataset",
                "/tmp/ds",
                "--train-end",
                "2026-01-01T00:00:00+00:00",
            ]
        )
        assert rc == 0
        assert captured["train_days"] == benchmark.DEFAULT_TRAIN_DAYS
        assert captured["charge_pct"] == benchmark.DEFAULT_CHARGE_PCT
        assert captured["discharge_pct"] == benchmark.DEFAULT_DISCHARGE_PCT
        assert captured["time_limit_s"] == benchmark.DEFAULT_TIME_LIMIT_S
        assert captured["mip_rel_gap"] == benchmark.DEFAULT_MIP_REL_GAP
        assert captured["mip_abs_gap"] == benchmark.DEFAULT_MIP_ABS_GAP
        assert captured["min_usable_fraction"] == 0.0
        assert captured["initial_soc_frac"] is None
        assert captured["import_limit_kw"] is None
        assert captured["allow_reconstructed_quality"] is False
        assert captured["terminal_mode"] == "free"

    def test_summary_printed(self, monkeypatch, tmp_path, capsys):
        from energy_baseline import benchmark

        monkeypatch.setattr(benchmark, "core_available", lambda: True)
        run_dir = tmp_path / "benchmarks" / "abc123"
        run_dir.mkdir(parents=True)
        payload = {
            "run_id": "abc123",
            "assumptions": {
                "cost_objective": {"cost_basis": "untaxed"},
                "terminal_accounting": {"used": "free"},
            },
            "evaluation": {
                "months_included": ["2026-08", "2026-09"],
                "interval_count": 8700,
                "usable_interval_count": 8640,
            },
            "policies": {
                "no_battery": {
                    "cost_eur": 123.45,
                    "solver_status": "not_applicable",
                    "is_certified": False,
                    "bound_kind": "none",
                },
                "oracle": {
                    "cost_eur": 100.0,
                    "solver_status": "optimal",
                    "is_certified": True,
                    "bound_kind": "optimal",
                },
            },
            "solver": {"errors": {}},
        }
        (run_dir / benchmark.RESULTS_JSON_NAME).write_text(json.dumps(payload))

        def fake(**_kw):
            return run_dir

        monkeypatch.setattr(benchmark, "benchmark_dataset", fake)
        rc = main(
            [
                "benchmark",
                "--dataset",
                "/tmp/ds",
                "--train-end",
                "2026-01-01T00:00:00+00:00",
            ]
        )
        assert rc == 0
        captured = capsys.readouterr()
        assert "run directory:" in captured.out
        assert "summary:" in captured.out
        assert "basis=untaxed" in captured.out
        assert "no_battery=" in captured.out
        assert "oracle=" in captured.out


# ---------------------------------------------------------------------------
# Tests: inspect-sql CLI wiring
# ---------------------------------------------------------------------------


class TestInspectSQLCLI:
    def test_list_queries(self, monkeypatch, capsys):
        from energy_baseline import report

        monkeypatch.setattr(report, "list_curated_queries", lambda: ["alpha", "beta"])
        rc = main(["inspect-sql", "--list-queries"])
        assert rc == 0
        captured = capsys.readouterr()
        assert "alpha" in captured.out
        assert "beta" in captured.out

    def test_list_queries_json(self, monkeypatch, capsys):
        from energy_baseline import report

        monkeypatch.setattr(report, "list_curated_queries", lambda: ["alpha", "beta"])
        rc = main(["inspect-sql", "--list-queries", "--json"])
        assert rc == 0
        captured = capsys.readouterr()
        assert json.loads(captured.out) == ["alpha", "beta"]

    def test_sql_and_query_mutually_exclusive(self):
        with pytest.raises(SystemExit) as exc:
            main(
                [
                    "inspect-sql",
                    "--dataset",
                    "/tmp/ds",
                    "--sql",
                    "SELECT 1",
                    "--query",
                    "alpha",
                ]
            )
        assert exc.value.code == 2

    def test_sql_and_list_mutually_exclusive(self, capsys):
        rc = main(["inspect-sql", "--sql", "SELECT 1", "--list-queries"])
        assert rc == 1
        captured = capsys.readouterr()
        assert "exactly one action" in captured.err

    def test_requires_exactly_one_action(self, capsys):
        rc = main(["inspect-sql"])
        assert rc == 1
        captured = capsys.readouterr()
        assert "exactly one action" in captured.err

    def test_sql_requires_dataset(self, capsys):
        rc = main(["inspect-sql", "--sql", "SELECT 1"])
        assert rc == 1
        captured = capsys.readouterr()
        assert "--dataset" in captured.err

    def test_query_requires_dataset(self, capsys):
        rc = main(["inspect-sql", "--query", "alpha"])
        assert rc == 1
        captured = capsys.readouterr()
        assert "--dataset" in captured.err

    def test_query_forwarded(self, monkeypatch, capsys):
        from energy_baseline import report

        monkeypatch.setattr(
            report,
            "run_curated_query",
            lambda name, *, dataset, benchmark: [{"policy": "oracle", "cost_eur": 100.0}],
        )
        rc = main(["inspect-sql", "--dataset", "/tmp/ds", "--query", "alpha"])
        assert rc == 0
        captured = capsys.readouterr()
        assert "oracle" in captured.out
        assert "100" in captured.out

    def test_sql_forwarded(self, monkeypatch, capsys):
        from energy_baseline import report

        monkeypatch.setattr(
            report,
            "inspect_sql",
            lambda *, dataset, sql, benchmark: [{"n": 42}],
        )
        rc = main(["inspect-sql", "--dataset", "/tmp/ds", "--sql", "SELECT 42 AS n"])
        assert rc == 0
        captured = capsys.readouterr()
        assert "42" in captured.out


# ---------------------------------------------------------------------------
# Tests: inspect-sql benchmark-view requirement
#
# ``benchmark_results`` / ``benchmark_schedules`` are only registered when
# ``--benchmark`` is given.  Asking for one without it is an expected user
# condition, so it must be reported as one — DuckDB's CatalogException is neither
# a ValueError nor a RuntimeError and used to escape as a raw traceback.
# ---------------------------------------------------------------------------

#: Curated queries that read a benchmark-only view, and the view they read.
_BENCHMARK_VIEW_QUERIES = {
    "benchmark_summary": "benchmark_results",
    "excluded_intervals": "benchmark_schedules",
    "monthly_cost": "benchmark_schedules",
}

#: Curated queries that read only the dataset itself, so they must still work
#: without ``--benchmark``.
_DATASET_ONLY_QUERIES = ("dataset_meta", "price_distribution", "quality_summary")


class TestInspectSQLBenchmarkViews:
    @pytest.mark.parametrize("name", sorted(_BENCHMARK_VIEW_QUERIES))
    def test_benchmark_view_query_refused_without_benchmark(self, name, monkeypatch, capsys):
        from energy_baseline import report

        expected_view = _BENCHMARK_VIEW_QUERIES[name]
        called = {"ran": False}

        def fake(_name, **_kw):
            called["ran"] = True
            return []

        monkeypatch.setattr(report, "run_curated_query", fake)
        rc = main(["inspect-sql", "--dataset", "/tmp/ds", "--query", name])
        assert rc == 1
        captured = capsys.readouterr()
        assert f"'{expected_view}'" in captured.err
        assert "--benchmark" in captured.err
        assert "Traceback" not in captured.err
        # Refused up front: DuckDB is never opened, so the DB error cannot leak.
        assert called["ran"] is False

    @pytest.mark.parametrize("name", sorted(_DATASET_ONLY_QUERIES))
    def test_dataset_only_query_still_runs_without_benchmark(self, name, monkeypatch, capsys):
        from energy_baseline import report

        monkeypatch.setattr(report, "run_curated_query", lambda _name, **_kw: [{"ok": 1}])
        rc = main(["inspect-sql", "--dataset", "/tmp/ds", "--query", name])
        assert rc == 0
        assert "benchmark view" not in capsys.readouterr().err

    def test_query_with_benchmark_flag_is_not_blocked(self, monkeypatch, capsys):
        """--benchmark registers the views, so the same query must go through."""
        from energy_baseline import report

        called = {}

        def fake(name, **kw):
            called["name"] = name
            called["benchmark"] = kw["benchmark"]
            return [{"month": "2026-01"}]

        monkeypatch.setattr(report, "run_curated_query", fake)
        rc = main(
            [
                "inspect-sql",
                "--dataset",
                "/tmp/ds",
                "--query",
                "monthly_cost",
                "--benchmark",
                "/tmp/run",
            ]
        )
        assert rc == 0
        assert called == {"name": "monthly_cost", "benchmark": "/tmp/run"}
        assert "Traceback" not in capsys.readouterr().err

    def test_curated_query_roster_is_current(self):
        """Tripwire: a new or renamed curated query must be classified above.

        The split is asserted against ``report``'s real roster so the CLI's
        up-front check cannot silently stop covering a benchmark-needing query.
        """
        from energy_baseline import report

        assert set(report.list_curated_queries()) == set(_BENCHMARK_VIEW_QUERIES) | set(
            _DATASET_ONLY_QUERIES
        )

    def test_adhoc_sql_naming_benchmark_view_refused(self, monkeypatch, capsys):
        from energy_baseline import report

        called = {"ran": False}

        def fake(**_kw):
            called["ran"] = True
            return []

        monkeypatch.setattr(report, "inspect_sql", fake)
        rc = main(
            [
                "inspect-sql",
                "--dataset",
                "/tmp/ds",
                "--sql",
                "SELECT policy FROM benchmark_results WHERE cost_eur < 0",
            ]
        )
        assert rc == 1
        captured = capsys.readouterr()
        assert "'benchmark_results'" in captured.err
        assert "--benchmark" in captured.err
        assert "Traceback" not in captured.err
        assert called["ran"] is False

    def test_adhoc_sql_without_benchmark_view_not_blocked(self, monkeypatch):
        from energy_baseline import report

        monkeypatch.setattr(report, "inspect_sql", lambda **_kw: [{"n": 1}])
        rc = main(["inspect-sql", "--dataset", "/tmp/ds", "--sql", "SELECT 1 FROM intervals"])
        assert rc == 0

    def test_unknown_curated_query_keeps_report_message(self, capsys):
        """An unknown name stays ``report``'s error, not the view check's."""
        rc = main(["inspect-sql", "--dataset", "/nonexistent", "--query", "no_such_query"])
        assert rc == 1
        captured = capsys.readouterr()
        assert "unknown curated query" in captured.err
        assert "Traceback" not in captured.err

    def test_view_detection_ignores_lookalikes(self):
        """Word-boundary matching, so nothing blocks on an unrelated identifier."""
        from energy_baseline.cli import _benchmark_views_in

        assert _benchmark_views_in("SELECT * FROM benchmark_schedules") == ["benchmark_schedules"]
        assert _benchmark_views_in("SELECT * FROM intervals") == []
        assert _benchmark_views_in("SELECT benchmark_results_extra FROM t") == []
        # Both views named: reported together, in the declared order.
        assert _benchmark_views_in(
            "SELECT * FROM benchmark_schedules s JOIN benchmark_results r"
        ) == ["benchmark_results", "benchmark_schedules"]

    def test_both_missing_views_named_in_one_message(self, monkeypatch, capsys):
        from energy_baseline import report

        monkeypatch.setattr(report, "inspect_sql", lambda **_kw: [])
        rc = main(
            [
                "inspect-sql",
                "--dataset",
                "/tmp/ds",
                "--sql",
                "SELECT * FROM benchmark_results r JOIN benchmark_schedules s",
            ]
        )
        assert rc == 1
        captured = capsys.readouterr()
        err_lines = [line for line in captured.err.splitlines() if line.strip()]
        assert len(err_lines) == 1
        assert "'benchmark_results', 'benchmark_schedules'" in captured.err
        # Singular becomes plural when two views are missing, and stays one line.
        assert "benchmark views 'benchmark_results', 'benchmark_schedules', which are" in (
            captured.err
        )

    def test_blocked_message_does_not_leak_token(self, monkeypatch, capsys):
        """Redaction still applies on the new error path."""
        from energy_baseline import report

        def fake(_name, **_kw):
            raise ValueError(f"cannot read run: token={_SENTINEL_TOKEN}")

        monkeypatch.setattr(report, "run_curated_query", fake)
        rc = main(
            [
                "inspect-sql",
                "--dataset",
                "/tmp/ds",
                "--benchmark",
                "/tmp/run",
                "--query",
                "monthly_cost",
            ]
        )
        assert rc == 1
        captured = capsys.readouterr()
        assert _SENTINEL_TOKEN not in captured.out
        assert _SENTINEL_TOKEN not in captured.err
        assert "<REDACTED>" in captured.err


# ---------------------------------------------------------------------------
# Tests: build CLI wiring
# ---------------------------------------------------------------------------


class TestBuildCLI:
    def test_default_battery_absent_true(self, monkeypatch, capsys):
        from energy_baseline import build

        captured = {}

        def fake(**kw):
            captured.update(kw)
            return Path("/tmp/dataset")

        monkeypatch.setattr(build, "build_dataset", fake)
        rc = main(["build", "--start", "2026-01-01", "--end", "2026-01-02"])
        assert rc == 0
        assert captured["battery_absent"] is True
        captured_err = capsys.readouterr().err
        assert "RECORDED ASSUMPTION" in captured_err
        assert "NOT A DETECTION" in captured_err

    def test_no_assume_no_battery(self, monkeypatch, capsys):
        from energy_baseline import build

        captured = {}

        def fake(**kw):
            captured.update(kw)
            return Path("/tmp/dataset")

        monkeypatch.setattr(build, "build_dataset", fake)
        rc = main(
            [
                "build",
                "--start",
                "2026-01-01",
                "--end",
                "2026-01-02",
                "--no-assume-no-battery",
            ]
        )
        assert rc == 0
        assert captured["battery_absent"] is False
        captured_err = capsys.readouterr().err
        assert "--no-assume-no-battery" in captured_err

    def test_prints_dataset_dir(self, monkeypatch, capsys):
        from energy_baseline import build

        monkeypatch.setattr(build, "build_dataset", lambda **kw: Path("/tmp/dataset"))
        rc = main(["build", "--start", "2026-01-01", "--end", "2026-01-02"])
        assert rc == 0
        captured = capsys.readouterr()
        assert "dataset directory:" in captured.out
        assert "/tmp/dataset" in captured.out


# ---------------------------------------------------------------------------
# Tests: report CLI wiring
# ---------------------------------------------------------------------------


class TestReportCLI:
    def test_forwards_dataset_and_benchmark(self, monkeypatch):
        from energy_baseline import report

        captured = {}

        def fake(**kw):
            captured.update(kw)
            return Path("/tmp/run")

        monkeypatch.setattr(report, "generate_report", fake)
        rc = main(
            [
                "report",
                "--dataset",
                "/tmp/ds",
                "--benchmark",
                "/tmp/run",
            ]
        )
        assert rc == 0
        assert captured["dataset"] == "/tmp/ds"
        assert captured["benchmark_path"] == "/tmp/run"

    def test_ambiguity_surfaces_run_ids(self, monkeypatch, capsys):
        from energy_baseline import report

        def fake(**_kw):
            raise report.ReportInputError(
                "dataset has 3 benchmark runs; pass one of them as benchmark_path: "
                "run-aaa, run-bbb, run-ccc"
            )

        monkeypatch.setattr(report, "generate_report", fake)
        rc = main(["report", "--dataset", "/tmp/ds"])
        assert rc == 1
        captured = capsys.readouterr()
        assert "run-aaa" in captured.err
        assert "run-bbb" in captured.err
        assert "run-ccc" in captured.err


# ---------------------------------------------------------------------------
# Tests: main() error handling
# ---------------------------------------------------------------------------


class TestMainErrorHandling:
    def test_value_error_exit_1(self, monkeypatch, capsys):
        from energy_baseline import cli

        def boom(_args):
            raise ValueError("bad input: missing required field")

        monkeypatch.setitem(cli._HANDLERS, "report", boom)
        rc = main(["report"])
        assert rc == 1
        captured = capsys.readouterr()
        assert "bad input" in captured.err
        assert "Traceback" not in captured.err

    def test_runtime_error_exit_1(self, monkeypatch, capsys):
        from energy_baseline import cli

        def boom(_args):
            raise RuntimeError("something went wrong")

        monkeypatch.setitem(cli._HANDLERS, "report", boom)
        rc = main(["report"])
        assert rc == 1
        captured = capsys.readouterr()
        assert "something went wrong" in captured.err
        assert "Traceback" not in captured.err

    def test_unexpected_exception_propagates(self, monkeypatch):
        from energy_baseline import cli

        def boom(_args):
            raise KeyError("unexpected")

        monkeypatch.setitem(cli._HANDLERS, "report", boom)
        with pytest.raises(KeyError):
            main(["report"])

    def test_error_redacts_sentinel_token(self, monkeypatch, capsys):
        from energy_baseline import cli

        def boom(_args):
            raise ValueError(
                f"request failed: Authorization: Bearer {_SENTINEL_TOKEN} "
                f"and token={_SENTINEL_TOKEN} in URL"
            )

        monkeypatch.setitem(cli._HANDLERS, "report", boom)
        rc = main(["report"])
        assert rc == 1
        captured = capsys.readouterr()
        assert _SENTINEL_TOKEN not in captured.out
        assert _SENTINEL_TOKEN not in captured.err
        assert "<REDACTED>" in captured.err

    def test_error_message_one_line(self, monkeypatch, capsys):
        from energy_baseline import cli

        def boom(_args):
            raise ValueError("line one\nline two\nline three")

        monkeypatch.setitem(cli._HANDLERS, "report", boom)
        rc = main(["report"])
        assert rc == 1
        captured = capsys.readouterr()
        err_lines = [line for line in captured.err.splitlines() if line.strip()]
        assert len(err_lines) == 1
        assert "line one" in err_lines[0]
        assert "line two" in err_lines[0]
        assert "line three" in err_lines[0]


# ---------------------------------------------------------------------------
# Tests: main() OSError handling (immutable store + filesystem conditions)
#
# ``FileExistsError`` is an ``OSError``, not a ``ValueError``/``RuntimeError``, so
# the no-clobber refusal from ``build`` used to print a full traceback.  The
# refusal itself is correct and stays intact — only the presentation is checked
# here, plus the rule that a genuinely unexpected exception still propagates.
# ---------------------------------------------------------------------------


_COLLISION = (
    "refusing to overwrite /data/energy-baseline/analysis/v1/"
    "dataset-abc123/build_manifest.json with different content "
    "(existing=5b4352d3, new=27400c06)"
)


class TestOSErrorHandling:
    def test_file_exists_error_is_one_clean_line(self, monkeypatch, capsys):
        from energy_baseline import cli

        def boom(_args):
            raise FileExistsError(_COLLISION)

        monkeypatch.setitem(cli._HANDLERS, "build", boom)
        rc = main(["build", "--start", "2026-01-01", "--end", "2026-01-02"])
        assert rc == 1
        captured = capsys.readouterr()
        assert "Traceback" not in captured.err
        assert "FileExistsError" not in captured.err
        lines = [line for line in captured.err.splitlines() if line.strip()]
        assert len(lines) == 1

    def test_build_collision_explains_and_tells_what_to_do(self, monkeypatch, capsys):
        """The collision message says what happened, why, and what to do next."""
        from energy_baseline import cli

        def boom(_args):
            raise FileExistsError(_COLLISION)

        monkeypatch.setitem(cli._HANDLERS, "build", boom)
        rc = main(["build", "--start", "2026-01-01", "--end", "2026-01-02"])
        assert rc == 1
        err = capsys.readouterr().err
        # What happened: the original refusal, hashes and all.
        assert "refusing to overwrite" in err
        assert "existing=5b4352d3, new=27400c06" in err
        # Why: same id, different bytes = the store refusing to clobber itself.
        assert "immutable canonical store" in err
        assert "nothing was overwritten and nothing was deleted" in err
        assert "not hashed into the id changed" in err
        # What to do: a fresh data dir, or remove that dataset directory by hand.
        assert "--data-dir" in err
        assert "remove that one dataset directory yourself" in err

    def test_build_collision_never_deletes_or_overwrites(self, monkeypatch, tmp_path, capsys):
        """Real no-clobber collision through the real writer: bytes survive, exit 1."""
        from energy_baseline import build, paths

        existing = tmp_path / "analysis/v1/dataset-abc123/build_manifest.json"
        existing.parent.mkdir(parents=True)
        original = b'{"dataset_id": "dataset-abc123"}'
        existing.write_bytes(original)

        def collide(**_kw):
            # Same path, different content: exactly what a rebuilt window does when
            # something outside the id's inputs has changed.
            return paths.write_part_atomic(existing, b'{"dataset_id": "dataset-abc123", "x": 1}')

        monkeypatch.setattr(build, "build_dataset", collide)
        rc = main(["build", "--start", "2026-01-01", "--end", "2026-01-02"])
        assert rc == 1
        captured = capsys.readouterr()
        assert "Traceback" not in captured.err
        assert "immutable canonical store" in captured.err
        # Untouched: not overwritten, not deleted.
        assert existing.exists()
        assert existing.read_bytes() == original

    def test_generic_file_exists_error_gets_no_guidance(self, monkeypatch, capsys):
        """The guidance is for the store's own refusal, not every FileExistsError."""
        from energy_baseline import cli

        def boom(_args):
            raise FileExistsError("file exists")

        monkeypatch.setitem(cli._HANDLERS, "build", boom)
        rc = main(["build", "--start", "2026-01-01", "--end", "2026-01-02"])
        assert rc == 1
        err = capsys.readouterr().err
        assert "file exists" in err
        assert "immutable canonical store" not in err

    @pytest.mark.parametrize(
        "exc",
        [
            PermissionError(13, "Permission denied", "/mnt/readonly/analysis"),
            IsADirectoryError(21, "Is a directory", "/data/analysis"),
            FileNotFoundError(2, "No such file or directory", "/data/missing.parquet"),
            OSError("disk I/O error"),
        ],
        ids=["permission", "is-a-directory", "not-found", "plain"],
    )
    def test_unrelated_os_error_is_one_clean_line(self, monkeypatch, capsys, exc):
        from energy_baseline import cli

        def boom(_args):
            raise exc

        monkeypatch.setitem(cli._HANDLERS, "validate", boom)
        rc = main(["validate"])
        assert rc == 1
        captured = capsys.readouterr()
        assert "Traceback" not in captured.err
        assert type(exc).__name__ not in captured.err
        lines = [line for line in captured.err.splitlines() if line.strip()]
        assert len(lines) == 1
        assert lines[0].startswith("validate:")

    def test_os_error_message_redacts_credentials(self, monkeypatch, capsys):
        from energy_baseline import cli

        def boom(_args):
            raise OSError(
                f"write failed: Authorization: Bearer {_SENTINEL_TOKEN} password=P@ss token=x"
            )

        monkeypatch.setitem(cli._HANDLERS, "build", boom)
        rc = main(["build", "--start", "2026-01-01", "--end", "2026-01-02"])
        assert rc == 1
        captured = capsys.readouterr()
        assert _SENTINEL_TOKEN not in captured.out
        assert _SENTINEL_TOKEN not in captured.err
        assert "P@ss" not in captured.err
        assert "<REDACTED>" in captured.err

    def test_unexpected_exception_types_still_propagate(self, monkeypatch):
        """Only expected shapes are handled — no blanket ``except Exception``."""
        from energy_baseline import cli

        class UnexpectedBugError(Exception):
            """Not ValueError, RuntimeError or OSError: a programming error."""

        for exc in (UnexpectedBugError("bug"), KeyError("missing"), TypeError("bad type")):

            def boom(_args, _exc=exc):
                raise _exc

            monkeypatch.setitem(cli._HANDLERS, "report", boom)
            with pytest.raises(type(exc)):
                main(["report"])


# ---------------------------------------------------------------------------
# Tests: --help for every subcommand
# ---------------------------------------------------------------------------


class TestHelp:
    @pytest.mark.parametrize(
        "subcommand",
        ["extract", "validate", "build", "benchmark", "report", "inspect-sql", "mirror"],
    )
    def test_subcommand_help(self, subcommand):
        with pytest.raises(SystemExit) as exc:
            main([subcommand, "--help"])
        assert exc.value.code == 0

    def test_top_level_help(self):
        with pytest.raises(SystemExit) as exc:
            main(["--help"])
        assert exc.value.code == 0

    def test_no_args_returns_zero(self, capsys):
        rc = main([])
        assert rc == 0
        captured = capsys.readouterr()
        assert "usage:" in captured.out.lower()


# ---------------------------------------------------------------------------
# Tests: cmd_extract HA via httpx.MockTransport
# ---------------------------------------------------------------------------


class TestCmdExtractMock:
    """Exercise the HA extract path via httpx.MockTransport to confirm
    end-to-end wiring without hitting a live server."""

    def test_extract_ha_mock(self, tmp_path, monkeypatch):
        data_dir = tmp_path / "data"
        data_dir.mkdir()
        monkeypatch.setenv("ENERGY_BASELINE_DATA_DIR", str(data_dir))
        monkeypatch.setenv("ENERGY_BASELINE_HA_TOKEN", "mock-token-xyz")
        cfg = str(Path(__file__).resolve().parent.parent / "config" / "tariff.yaml")

        import energy_baseline.extract_ha as _eh
        from energy_baseline.extract_ha import extract_ha_history

        ts_str = "2026-01-01T12:00:00Z"
        fake_history = [
            [
                {
                    "entity_id": "sensor.lavadora_current_status",
                    "state": "idle",
                    "attributes": {"friendly_name": "Washer"},
                    "last_changed": ts_str,
                    "last_updated": ts_str,
                }
            ]
        ]

        def handler(request: httpx.Request) -> httpx.Response:
            return httpx.Response(200, json=fake_history)

        # Patch httpx.Client in the module to use MockTransport
        real_client_cls = httpx.Client

        def patched_client(**kw):
            return real_client_cls(
                transport=httpx.MockTransport(handler),
                timeout=kw.get("timeout"),
            )

        monkeypatch.setattr(_eh.httpx, "Client", patched_client)

        path = extract_ha_history(
            data_dir=str(data_dir),
            config_path=cfg,
            start="2026-01-01T00:00:00+00:00",
            end="2026-01-02T00:00:00+00:00",
            entities=["sensor.lavadora_current_status"],
        )
        assert path.exists()
        assert path.name.endswith(".json")
        # Verify manifest content
        data = path.read_text()
        parsed = __import__("json").loads(data)
        assert parsed["source"] == "ha_history"
        assert parsed["row_count"] == 1


# ---------------------------------------------------------------------------
# Tests: benchmark CLI wiring for the day-ahead policy
# ---------------------------------------------------------------------------


class TestDayAheadCLI:
    @staticmethod
    def _capture(monkeypatch, argv):
        from energy_baseline import benchmark

        monkeypatch.setattr(benchmark, "core_available", lambda: True)
        captured = {}

        def fake(**kw):
            captured.update(kw)
            return Path("/tmp/run")

        monkeypatch.setattr(benchmark, "benchmark_dataset", fake)
        rc = main(argv)
        assert rc == 0
        return captured

    def test_day_ahead_is_an_acceptable_policy_name(self, monkeypatch):
        captured = self._capture(
            monkeypatch,
            [
                "benchmark",
                "--dataset",
                "/tmp/ds",
                "--train-end",
                "2026-01-01T00:00:00+00:00",
                "--policies",
                "no_battery,day_ahead",
            ],
        )
        assert captured["policies"] == "no_battery,day_ahead"

    def test_help_lists_the_five_rungs(self, capsys):
        with pytest.raises(SystemExit) as exc:
            main(["benchmark", "--help"])
        assert exc.value.code == 0
        # argparse wraps help at the terminal width, so match on the flattened text:
        # the fifth rung made the four-rung phrase long enough to wrap mid-list.
        text = " ".join(capsys.readouterr().out.split())
        assert "no_battery, causal_price, day_ahead, rolling_horizon, oracle" in text

    def test_defaults_come_from_the_benchmark_module(self, monkeypatch):
        from energy_baseline import benchmark

        captured = self._capture(
            monkeypatch,
            [
                "benchmark",
                "--dataset",
                "/tmp/ds",
                "--train-end",
                "2026-01-01T00:00:00+00:00",
            ],
        )
        assert captured["day_ahead_forecast_window_days"] == benchmark.DEFAULT_FORECAST_WINDOW_DAYS
        assert captured["day_ahead_time_limit_s"] == benchmark.DEFAULT_DAY_AHEAD_TIME_LIMIT_S
        # Unset is NOT 0.0: unset means "derive it from the frozen training window".
        assert captured["day_ahead_terminal_value_eur_per_kwh"] is None

    @pytest.mark.parametrize(
        "flag, value, kwarg, expected",
        [
            ("--day-ahead-forecast-window-days", "14", "day_ahead_forecast_window_days", 14),
            ("--day-ahead-time-limit-s", "2.5", "day_ahead_time_limit_s", 2.5),
            ("--day-ahead-terminal-value", "0.213", "day_ahead_terminal_value_eur_per_kwh", 0.213),
            # 0.0 is a real, documented choice (pure cash flow, battery emptied by
            # midnight), so it must survive as 0.0 and never look like an omission.
            ("--day-ahead-terminal-value", "0", "day_ahead_terminal_value_eur_per_kwh", 0.0),
        ],
    )
    def test_day_ahead_flags_reach_the_contract(self, monkeypatch, flag, value, kwarg, expected):
        captured = self._capture(
            monkeypatch,
            [
                "benchmark",
                "--dataset",
                "/tmp/ds",
                "--train-end",
                "2026-01-01T00:00:00+00:00",
                flag,
                value,
            ],
        )
        assert captured[kwarg] == expected

    def test_summary_reports_day_ahead_telemetry(self, monkeypatch, tmp_path, capsys):
        from energy_baseline import benchmark

        monkeypatch.setattr(benchmark, "core_available", lambda: True)
        run_dir = tmp_path / "benchmarks" / "def456"
        run_dir.mkdir(parents=True)
        payload = {
            "run_id": "def456",
            "assumptions": {
                "cost_objective": {"cost_basis": "untaxed"},
                "terminal_accounting": {"used": "free"},
            },
            "evaluation": {
                "months_included": ["2026-01"],
                "interval_count": 384,
                "usable_interval_count": 384,
            },
            "policies": {
                "no_battery": {
                    "cost_eur": 100.0,
                    "solver_status": "not_applicable",
                    "is_certified": False,
                    "bound_kind": "none",
                },
                "causal_price": {
                    "cost_eur": 90.0,
                    "solver_status": "not_applicable",
                    "is_certified": False,
                    "bound_kind": "none",
                },
                "day_ahead": {
                    "cost_eur": 80.0,
                    "solver_status": "feasible",
                    "is_certified": False,
                    "bound_kind": "none",
                },
                "oracle": {
                    "cost_eur": 70.0,
                    "solver_status": "optimal",
                    "is_certified": True,
                    "bound_kind": "certified",
                },
            },
            "solver": {"errors": {}},
            "day_ahead": {
                "ran": True,
                "day_count": 4,
                "days_solved": 4,
                "days_degraded_to_hold": 0,
                "days_at_time_limit": 0,
                "forecast": {"summary": {"mae_kwh": 0.0312}},
                "terminal_value": {"eur_per_kwh": 0.1705},
            },
            "comparison": {"day_ahead_closes_gap_fraction": 0.5},
        }
        (run_dir / benchmark.RESULTS_JSON_NAME).write_text(json.dumps(payload))

        def fake(**_kw):
            return run_dir

        monkeypatch.setattr(benchmark, "benchmark_dataset", fake)
        rc = main(
            [
                "benchmark",
                "--dataset",
                "/tmp/ds",
                "--train-end",
                "2026-01-01T00:00:00+00:00",
            ]
        )
        assert rc == 0
        summary = capsys.readouterr().out
        assert "day_ahead=80.0000 EUR (feasible, uncertified, bound=none)" in summary
        assert "4/4 local days solved" in summary
        assert "0 held SOC" in summary
        assert "forecast MAE=0.0312 kWh/interval" in summary
        assert "end-of-day SOC=0.1705 EUR/kWh" in summary
        assert "causal->oracle gap closed=50.0%" in summary

    def test_summary_keeps_the_two_terminal_assumptions_apart(self, monkeypatch, tmp_path, capsys):
        """`oracle_terminal=` is the oracle's; the day-ahead value is named separately.

        Both policies end their horizon under a terminal-SOC assumption and they are
        deliberately *different* numbers (the ceiling values leftover inventory at zero,
        the myopic policy must value it).  Collapsing them into one word, or printing the
        day-ahead dict where the oracle's mode used to be, makes the line unreadable and
        the two claims indistinguishable — this pin keeps them named.
        """
        from energy_baseline import benchmark

        monkeypatch.setattr(benchmark, "core_available", lambda: True)
        run_dir = tmp_path / "benchmarks" / "bbb222"
        run_dir.mkdir(parents=True)
        (run_dir / benchmark.RESULTS_JSON_NAME).write_text(
            json.dumps(
                {
                    "run_id": "bbb222",
                    "assumptions": {
                        "cost_objective": {"cost_basis": "untaxed"},
                        "terminal_accounting": {"used": "fixed"},
                    },
                    "evaluation": {
                        "months_included": ["2026-01"],
                        "interval_count": 96,
                        "usable_interval_count": 96,
                    },
                    "policies": {
                        "day_ahead": {
                            "cost_eur": 5.0,
                            "solver_status": "feasible",
                            "is_certified": False,
                            "bound_kind": "none",
                        }
                    },
                    "solver": {"errors": {}},
                    "comparison": {"day_ahead_closes_gap_fraction": None},
                    "day_ahead": {
                        "ran": True,
                        "day_count": 1,
                        "days_solved": 1,
                        "days_degraded_to_hold": 0,
                        "days_at_time_limit": 0,
                        "forecast": {"summary": {"mae_kwh": None}},
                        "terminal_value": {
                            "eur_per_kwh": 0.2,
                            "source": "cli: 0.2 EUR/DC-kWh supplied explicitly by the caller",
                        },
                    },
                }
            )
        )
        monkeypatch.setattr(benchmark, "benchmark_dataset", lambda **_kw: run_dir)
        rc = main(
            [
                "benchmark",
                "--dataset",
                "/tmp/ds",
                "--train-end",
                "2026-01-01T00:00:00+00:00",
            ]
        )
        assert rc == 0
        summary = capsys.readouterr().out
        assert "oracle_terminal=fixed" in summary
        assert "end-of-day SOC=0.2 EUR/kWh" in summary
        assert "forecast MAE=n/a" in summary
        assert "gap closed=n/a" in summary
        # The day-ahead block is summarised, never dumped: no dict repr in the line.
        assert "{'eur_per_kwh'" not in summary
        assert "source" not in summary.split("day_ahead:")[1]

    def test_summary_omits_day_ahead_when_it_did_not_run(self, monkeypatch, tmp_path, capsys):
        from energy_baseline import benchmark

        monkeypatch.setattr(benchmark, "core_available", lambda: True)
        run_dir = tmp_path / "benchmarks" / "aaa111"
        run_dir.mkdir(parents=True)
        (run_dir / benchmark.RESULTS_JSON_NAME).write_text(
            json.dumps(
                {
                    "run_id": "aaa111",
                    "assumptions": {
                        "cost_objective": {"cost_basis": "untaxed"},
                        "terminal_accounting": {"used": "free"},
                    },
                    "evaluation": {
                        "months_included": ["2026-01"],
                        "interval_count": 96,
                        "usable_interval_count": 96,
                    },
                    "policies": {
                        "no_battery": {
                            "cost_eur": 1.0,
                            "solver_status": "not_applicable",
                            "is_certified": False,
                            "bound_kind": "none",
                        }
                    },
                    "solver": {"errors": {}},
                    "day_ahead": {"ran": False},
                }
            )
        )
        monkeypatch.setattr(benchmark, "benchmark_dataset", lambda **_kw: run_dir)
        rc = main(
            [
                "benchmark",
                "--dataset",
                "/tmp/ds",
                "--train-end",
                "2026-01-01T00:00:00+00:00",
            ]
        )
        assert rc == 0
        assert "local days solved" not in capsys.readouterr().out
