"""Tests for the benchmark orchestration layer.

These tests exercise the dataset loading, interval usability, train/eval split,
battery assumption resolution, and the end-to-end benchmark run — all with
synthetic data, no live cluster, no real battery hardware.  The numeric core
(battery, oracle, policies, cost) is exercised through its public API; when it
is not yet implemented the solver-dependent tests are skipped with a clear
message.
"""

from __future__ import annotations

import hashlib
import json
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any

import numpy as np
import pyarrow as pa
import pyarrow.compute as pc
import pyarrow.parquet as pq
import pytest

from energy_baseline import benchmark as bm
from energy_baseline.config import BatteryConfig, TariffConfig, TariffPeriods, TaxEntry
from energy_baseline.schemas import ANALYSIS_INTERVAL, SCHEMA_VERSION

# ---------------------------------------------------------------------------
# Helpers
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
    """A synthetic tariff config for tests."""
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
    """Write a tariff config to YAML."""
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
    energy_quality: list[str] | None = None,
    allocation_quality: list[str] | None = None,
    price_quality: list[str] | None = None,
    dataset_id: str = "test-dataset-001",
    tz_local: str = "Europe/Madrid",
) -> pa.Table:
    """Build a synthetic ANALYSIS_INTERVAL table."""
    from zoneinfo import ZoneInfo

    madrid = ZoneInfo(tz_local)
    starts = [start + timedelta(seconds=i * interval_seconds) for i in range(intervals)]
    ends = [s + timedelta(seconds=interval_seconds) for s in starts]
    local_days = [s.astimezone(madrid).date() for s in starts]
    tariff_periods = [3] * intervals  # P3 for simplicity
    dst_transitions = [None] * intervals

    if gross_kwh is None:
        gross_kwh = [0.5] * intervals
    if prices is None:
        prices = [0.15] * intervals
    if energy_quality is None:
        energy_quality = ["measured"] * intervals
    if allocation_quality is None:
        allocation_quality = ["exact"] * intervals
    if price_quality is None:
        price_quality = ["measured"] * intervals

    return pa.table(
        {
            "interval_start": pa.array(starts, pa.timestamp("us", tz="UTC")),
            "interval_end": pa.array(ends, pa.timestamp("us", tz="UTC")),
            "tz_local": pa.array([tz_local] * intervals, pa.string()),
            "tariff_period": pa.array(tariff_periods, pa.int8()),
            "local_day": pa.array(local_days, pa.date32()),
            "dst_transition": pa.array(dst_transitions, pa.string()),
            "raw_grid_kwh": pa.array(gross_kwh, pa.float64()),
            "gross_kwh": pa.array(gross_kwh, pa.float64()),
            "battery_absent": pa.array([True] * intervals, pa.bool_()),
            "energy_quality": pa.array(energy_quality, pa.string()),
            "allocation_quality": pa.array(allocation_quality, pa.string()),
            "sample_count": pa.array([4] * intervals, pa.int32()),
            "gap_seconds": pa.array([0.0] * intervals, pa.float64()),
            "price_eur_per_kwh": pa.array(prices, pa.float64()),
            "price_quality": pa.array(price_quality, pa.string()),
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
    code_version: str = "test-code-v1",
) -> Path:
    """Write a synthetic dataset directory with manifest + parquet.

    If ``config_path`` is None, writes a default tariff config to
    ``tmp_path/"tariff.yaml"`` and pins its hash.  If given, the file must exist
    and its hash is pinned.
    """
    if config_path is None:
        config_path = tmp_path / "tariff.yaml"
        write_tariff_config(config_path, make_tariff_config())
    from energy_baseline.manifest import config_hash

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
        "code_version": code_version,
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
        "coverage": {
            "interval_count": table.num_rows,
            "grid_samples": table.num_rows * 4,
        },
        "params": {"interval_seconds": 900, "timezone": "Europe/Madrid"},
    }
    (dataset_dir / "build_manifest.json").write_text(json.dumps(manifest, indent=2))
    return dataset_dir


def requires_core() -> None:
    """Skip the test if the numeric core is not implemented."""
    if not bm.core_available():
        pytest.skip("numeric core not implemented yet")


@dataclass(frozen=True)
class _PerDirectionBattery:
    """A battery config stand-in carrying per-direction efficiencies.

    The shipped ``BatteryConfig`` has no per-direction fields yet; the benchmark
    reads them defensively via ``getattr``, so this stand-in pins that the
    symmetric sqrt mapping is bypassed when they are present.
    """

    capacity_kwh: float
    power_kw: float
    charge_efficiency: float
    discharge_efficiency: float
    soc_window_min: float
    soc_window_max: float
    round_trip_efficiency: float | None = None
    import_limit_kw: float | None = None


# ---------------------------------------------------------------------------
# Dataset loading
# ---------------------------------------------------------------------------


class TestLoadDataset:
    def test_loads_a_valid_dataset(self, tmp_path: Path) -> None:
        start = datetime(2026, 1, 1, tzinfo=UTC)
        table = make_analysis_table(start=start, intervals=96)
        dataset_dir = write_synthetic_dataset(tmp_path, table)
        loaded = bm.load_dataset(dataset_dir)
        assert loaded.dataset_id == "test-dataset-001"
        assert loaded.table.num_rows == 96
        assert loaded.interval_seconds == 900.0

    def test_rejects_missing_manifest(self, tmp_path: Path) -> None:
        dataset_dir = tmp_path / "analysis" / "v1" / "test-dataset-001"
        dataset_dir.mkdir(parents=True)
        with pytest.raises(bm.BenchmarkInputError, match="no build_manifest.json"):
            bm.load_dataset(dataset_dir)

    def test_rejects_part_hash_mismatch(self, tmp_path: Path) -> None:
        start = datetime(2026, 1, 1, tzinfo=UTC)
        table = make_analysis_table(start=start, intervals=10)
        dataset_dir = write_synthetic_dataset(tmp_path, table)
        # Tamper with the parquet file
        part_path = dataset_dir / "parts" / "analysis-test-dataset-001-00000000.parquet"
        part_path.write_bytes(b"tampered")
        with pytest.raises(bm.BenchmarkInputError, match="input hash failure"):
            bm.load_dataset(dataset_dir)

    def test_rejects_non_uniform_intervals(self, tmp_path: Path) -> None:
        start = datetime(2026, 1, 1, tzinfo=UTC)
        # Build a table with non-uniform interval *durations* by hand
        durations = [900, 900, 1800, 900, 900]  # one 30-min interval among 15-min
        starts = []
        ends = []
        cursor = start
        for d in durations:
            starts.append(cursor)
            ends.append(cursor + timedelta(seconds=d))
            cursor = ends[-1]
        rows = {
            "interval_start": pa.array(starts, pa.timestamp("us", tz="UTC")),
            "interval_end": pa.array(ends, pa.timestamp("us", tz="UTC")),
            "tz_local": pa.array(["Europe/Madrid"] * 5, pa.string()),
            "tariff_period": pa.array([3] * 5, pa.int8()),
            "local_day": pa.array([s.date() for s in starts], pa.date32()),
            "dst_transition": pa.array([None] * 5, pa.string()),
            "raw_grid_kwh": pa.array([0.5] * 5, pa.float64()),
            "gross_kwh": pa.array([0.5] * 5, pa.float64()),
            "battery_absent": pa.array([True] * 5, pa.bool_()),
            "energy_quality": pa.array(["measured"] * 5, pa.string()),
            "allocation_quality": pa.array(["exact"] * 5, pa.string()),
            "sample_count": pa.array([4] * 5, pa.int32()),
            "gap_seconds": pa.array([0.0] * 5, pa.float64()),
            "price_eur_per_kwh": pa.array([0.15] * 5, pa.float64()),
            "price_quality": pa.array(["measured"] * 5, pa.string()),
            "dataset_id": pa.array(["test-dataset-001"] * 5, pa.string()),
        }
        table = pa.table(rows, schema=ANALYSIS_INTERVAL)
        dataset_dir = write_synthetic_dataset(tmp_path, table)
        with pytest.raises(bm.BenchmarkInputError, match="interval duration is not uniform"):
            bm.load_dataset(dataset_dir)


# ---------------------------------------------------------------------------
# Usability mask
# ---------------------------------------------------------------------------


class TestUsableMask:
    def test_measured_is_usable(self) -> None:
        start = datetime(2026, 1, 1, tzinfo=UTC)
        table = make_analysis_table(start=start, intervals=10)
        usable, reasons = bm.usable_mask(table)
        assert usable.all()
        assert all(r == "usable" for r in reasons)

    def test_unknown_gap_is_never_usable(self) -> None:
        start = datetime(2026, 1, 1, tzinfo=UTC)
        table = make_analysis_table(
            start=start,
            intervals=5,
            allocation_quality=["exact", "unknown_gap", "exact", "unknown_gap", "exact"],
        )
        usable, reasons = bm.usable_mask(table)
        assert usable.tolist() == [True, False, True, False, True]
        assert reasons[1] == "allocation_quality=unknown_gap"
        assert reasons[3] == "allocation_quality=unknown_gap"

    def test_unknown_gap_never_usable_even_with_flag(self) -> None:
        start = datetime(2026, 1, 1, tzinfo=UTC)
        table = make_analysis_table(
            start=start, intervals=3, allocation_quality=["exact", "unknown_gap", "exact"]
        )
        usable, _ = bm.usable_mask(table, allow_reconstructed=True)
        assert usable.tolist() == [True, False, True]

    def test_gap_partial_requires_flag(self) -> None:
        start = datetime(2026, 1, 1, tzinfo=UTC)
        table = make_analysis_table(
            start=start, intervals=3, energy_quality=["measured", "gap_partial", "measured"]
        )
        usable_no_flag, reasons_no_flag = bm.usable_mask(table, allow_reconstructed=False)
        assert usable_no_flag.tolist() == [True, False, True]
        assert reasons_no_flag[1] == "energy_quality=gap_partial"

        usable_with_flag, reasons_with_flag = bm.usable_mask(table, allow_reconstructed=True)
        assert usable_with_flag.tolist() == [True, True, True]
        assert reasons_with_flag[1] == "usable"

    def test_forward_filled_price_requires_flag(self) -> None:
        start = datetime(2026, 1, 1, tzinfo=UTC)
        table = make_analysis_table(
            start=start, intervals=3, price_quality=["measured", "forward_filled", "measured"]
        )
        usable_no_flag, _ = bm.usable_mask(table, allow_reconstructed=False)
        assert usable_no_flag.tolist() == [True, False, True]

        usable_with_flag, _ = bm.usable_mask(table, allow_reconstructed=True)
        assert usable_with_flag.tolist() == [True, True, True]

    def test_missing_price_is_never_usable(self) -> None:
        start = datetime(2026, 1, 1, tzinfo=UTC)
        table = make_analysis_table(
            start=start, intervals=3, price_quality=["measured", "missing", "measured"]
        )
        usable, _ = bm.usable_mask(table, allow_reconstructed=True)
        assert usable.tolist() == [True, False, True]

    def test_negative_gross_is_rejected(self) -> None:
        start = datetime(2026, 1, 1, tzinfo=UTC)
        table = make_analysis_table(start=start, intervals=3, gross_kwh=[0.5, -0.1, 0.5])
        usable, reasons = bm.usable_mask(table)
        assert usable.tolist() == [True, False, True]
        assert reasons[1] == "gross_kwh=negative"

    def test_null_gross_is_rejected(self) -> None:
        start = datetime(2026, 1, 1, tzinfo=UTC)
        table = make_analysis_table(start=start, intervals=3, gross_kwh=[0.5, None, 0.5])
        usable, reasons = bm.usable_mask(table)
        assert usable.tolist() == [True, False, True]
        assert reasons[1] == "gross_kwh=null"


# ---------------------------------------------------------------------------
# Train/eval split
# ---------------------------------------------------------------------------


class TestParseTrainEnd:
    def test_accepts_aware_datetime(self) -> None:
        dt = datetime(2026, 6, 1, tzinfo=UTC)
        result = bm.parse_train_end(dt)
        assert result == dt

    def test_accepts_iso_string(self) -> None:
        result = bm.parse_train_end("2026-06-01T00:00:00Z")
        assert result.year == 2026
        assert result.month == 6

    def test_rejects_none(self) -> None:
        with pytest.raises(bm.BenchmarkInputError, match="train_end is required"):
            bm.parse_train_end(None)

    def test_rejects_naive_datetime(self) -> None:
        with pytest.raises(bm.BenchmarkInputError, match="naive"):
            bm.parse_train_end(datetime(2026, 6, 1))

    def test_rejects_future(self) -> None:
        future = datetime.now(UTC) + timedelta(days=365)
        with pytest.raises(bm.BenchmarkInputError, match="in the future"):
            bm.parse_train_end(future)


# ---------------------------------------------------------------------------
# Battery assumptions
# ---------------------------------------------------------------------------


class TestResolveBatteryAssumptions:
    def test_sqrt_mapping_with_symmetric_assumption(self) -> None:
        config = make_tariff_config(round_trip_efficiency=0.81)
        assumptions = bm.resolve_battery_assumptions(config)
        assert assumptions.charge_efficiency == pytest.approx(0.9)
        assert assumptions.discharge_efficiency == pytest.approx(0.9)
        assert "symmetric assumption" in assumptions.efficiency_mapping

    def test_explicit_per_direction_efficiencies(self) -> None:
        config = make_tariff_config(round_trip_efficiency=None)
        # The core config has no per-direction fields yet; the benchmark reads
        # them defensively via getattr, so a stand-in object is enough to pin
        # that the symmetric sqrt mapping is bypassed when they are present.
        object.__setattr__(  # frozen dataclass: bypass the guard for the test
            config,
            "battery",
            _PerDirectionBattery(
                capacity_kwh=5.12,
                power_kw=2.5,
                charge_efficiency=0.95,
                discharge_efficiency=0.90,
                soc_window_min=0.05,
                soc_window_max=0.95,
                import_limit_kw=None,
            ),
        )
        assumptions = bm.resolve_battery_assumptions(config)
        assert assumptions.charge_efficiency == 0.95
        assert assumptions.discharge_efficiency == 0.90
        assert assumptions.efficiency_mapping.startswith("config: per-direction")

    def test_null_rte_raises(self) -> None:
        config = make_tariff_config(round_trip_efficiency=None)
        with pytest.raises(bm.BenchmarkInputError, match="round_trip_efficiency is null"):
            bm.resolve_battery_assumptions(config)

    def test_initial_soc_defaults_to_soc_min(self) -> None:
        config = make_tariff_config(soc_window_min=0.1, soc_window_max=0.9)
        assumptions = bm.resolve_battery_assumptions(config)
        assert assumptions.initial_soc_frac == 0.1
        assert "default: initial SOC = soc_window_min" in assumptions.initial_soc_source

    def test_explicit_initial_soc(self) -> None:
        config = make_tariff_config(soc_window_min=0.1, soc_window_max=0.9)
        assumptions = bm.resolve_battery_assumptions(config, initial_soc_frac=0.5)
        assert assumptions.initial_soc_frac == 0.5
        assert "argument: initial_soc_frac" in assumptions.initial_soc_source

    def test_initial_soc_outside_window_raises(self) -> None:
        config = make_tariff_config(soc_window_min=0.1, soc_window_max=0.9)
        with pytest.raises(bm.BenchmarkInputError, match="outside the configured SOC window"):
            bm.resolve_battery_assumptions(config, initial_soc_frac=0.05)

    def test_null_import_limit_is_unbounded(self) -> None:
        config = make_tariff_config(import_limit_kw=None)
        assumptions = bm.resolve_battery_assumptions(config)
        assert assumptions.import_limit_kw is None
        assert "unconfirmed" in assumptions.import_limit_source

    def test_explicit_import_limit(self) -> None:
        config = make_tariff_config(import_limit_kw=None)
        assumptions = bm.resolve_battery_assumptions(config, import_limit_kw=3.0)
        assert assumptions.import_limit_kw == 3.0
        assert "argument" in assumptions.import_limit_source

    def test_unsolvable_policy_is_recorded_not_fabricated(
        self, tmp_path: Path, monkeypatch
    ) -> None:
        """A policy that produces no cost gets a JSON error, not a zero-cost row."""
        requires_core()
        from energy_baseline import oracle as oracle_mod

        class StubbornError(RuntimeError):
            """Stands in for OracleInfeasibleError / OracleNoIncumbentError."""

        monkeypatch.setattr(oracle_mod, "OracleError", StubbornError, raising=True)

        def refusing(*args: Any, **kwargs: Any) -> None:
            raise StubbornError("no plan")

        monkeypatch.setattr(oracle_mod, "solve_oracle", refusing)
        start = datetime(2026, 1, 1, tzinfo=UTC)
        table = make_analysis_table(start=start, intervals=2 * 24 * 4)
        dataset_dir = write_synthetic_dataset(tmp_path, table)
        config_path = tmp_path / "tariff.yaml"
        write_tariff_config(config_path, make_tariff_config())

        run_dir = bm.benchmark_dataset(
            dataset=dataset_dir,
            config_path=config_path,
            train_end=start + timedelta(hours=12),
            time_limit_s=5.0,
        )
        payload = json.loads((run_dir / bm.RESULTS_JSON_NAME).read_text())
        assert "oracle" not in payload["policies"]
        assert payload["solver"]["errors"]["oracle"]["failure_kind"] == "solver_error"
        assert "no plan" in payload["solver"]["errors"]["oracle"]["error"]
        results = pq.read_table(run_dir / bm.RESULTS_PARQUET_NAME)
        assert set(results.column("policy").to_pylist()) == {"no_battery", "causal_price"}
        # Dominance against a policy that never ran is unknown, not asserted.
        assert payload["comparison"]["oracle_dominates_causal"] is None

    def test_no_cost_schedule_produces_no_row(self, tmp_path: Path, monkeypatch) -> None:
        """A schedule object without a cost is refused rather than written as null."""
        requires_core()
        from energy_baseline import policies as policies_mod

        class Silent:
            charge_kwh = np.zeros(4)
            discharge_kwh = np.zeros(4)
            grid_kwh = np.zeros(4)
            soc_kwh = np.zeros(4)
            net_flow_kwh = np.zeros(4)
            cost_eur = None
            efc = 0.0
            throughput_kwh = 0.0
            soc_min_frac = 0.0
            soc_max_frac = 0.0
            solver_status = "not_applicable"
            solver_gap = None
            bound_kind = "none"
            is_certified = False

        monkeypatch.setattr(policies_mod, "policy_no_battery", lambda *a, **k: Silent())
        start = datetime(2026, 1, 1, tzinfo=UTC)
        table = make_analysis_table(start=start, intervals=24 * 4)
        dataset_dir = write_synthetic_dataset(tmp_path, table)
        config_path = tmp_path / "tariff.yaml"
        write_tariff_config(config_path, make_tariff_config())
        run_dir = bm.benchmark_dataset(
            dataset=dataset_dir,
            config_path=config_path,
            train_end=start + timedelta(hours=6),
            policies="no_battery,causal_price",
            time_limit_s=5.0,
        )
        payload = json.loads((run_dir / bm.RESULTS_JSON_NAME).read_text())
        assert "no_battery" not in payload["policies"]
        assert payload["solver"]["errors"]["no_battery"]["failure_kind"] == "no_cost"


class TestBoundHonesty:
    """What may and may not be claimed, straight from the core's bound vocabulary."""

    def _record(self, **overrides: Any) -> dict[str, Any]:
        base = dict(
            name="oracle",
            time_limit_s=120.0,
            mip_rel_gap=1e-6,
            mip_abs_gap=1e-9,
            terminal_mode_requested="free",
            terminal_mode_used="free",
        )
        base.update(overrides)
        return base

    def test_certified_within_tolerance_is_not_reported_as_zero_gap(self) -> None:
        class S:
            cost_eur = 10.0
            cost_upper_bound_eur = 10.0
            cost_lower_bound_eur = 9.0
            solver_gap = 0.1
            solver_status = "optimal"
            bound_kind = "certified"
            is_certified = True
            solver_time_limit_s = 120.0

        record = bm._solver_record(S(), **self._record())
        notes = bm._bounds_note(record)
        assert any("NOT a zero gap" in n for n in notes)
        assert record["solver_gap"] == 0.1

    def test_uncertified_incumbent_is_an_upper_bound_not_an_optimum(self) -> None:
        class S:
            cost_eur = 10.0
            cost_upper_bound_eur = 10.0
            cost_lower_bound_eur = 4.0
            solver_gap = 0.6
            solver_status = "time_limit"
            bound_kind = "upper"
            is_certified = False
            solver_time_limit_s = 30.0

        record = bm._solver_record(S(), **self._record())
        notes = bm._bounds_note(record)
        assert any("UPPER BOUND on optimal cost" in n for n in notes)
        assert any("time limit" in n for n in notes)
        assert record["solver_time_limit_s"] == 30.0

    def test_missing_dual_bound_is_unknown_not_zero(self) -> None:
        class S:
            cost_eur = 10.0
            cost_upper_bound_eur = 10.0
            cost_lower_bound_eur = None
            solver_gap = None
            solver_status = "feasible"
            bound_kind = "upper"
            is_certified = False
            solver_time_limit_s = None

        record = bm._solver_record(S(), **self._record())
        assert record["cost_lower_bound_eur"] is None
        notes = bm._bounds_note(record)
        assert any("no dual" in n for n in notes)

    def test_traced_plan_claims_no_bound_at_all(self) -> None:
        class S:
            cost_eur = 12.0
            cost_upper_bound_eur = None
            cost_lower_bound_eur = None
            solver_gap = None
            solver_status = "not_applicable"
            bound_kind = "none"
            is_certified = False

        record = bm._solver_record(
            S(), **self._record(name="no_battery", terminal_mode_used="not_applicable")
        )
        # bound_kind=none ⇒ no bounds are invented from the cost
        assert record["cost_upper_bound_eur"] is None
        assert record["cost_lower_bound_eur"] is None
        # …and no solver settings are attributed to a policy that never called one
        assert record["solver_time_limit_s"] is None
        assert record["closed_cycle"] is None
        assert bm._bounds_note(record)[0].startswith("no solver bound applies")

    def test_fixed_terminal_reports_no_terminal_value(self) -> None:
        class S:
            cost_eur = 10.0
            cost_upper_bound_eur = 10.0
            cost_lower_bound_eur = 10.0
            solver_gap = 0.0
            solver_status = "optimal"
            bound_kind = "certified"
            is_certified = True
            terminal_value_eur_per_kwh = 0.0

        record = bm._solver_record(
            S(), **self._record(terminal_mode_requested="fixed", terminal_mode_used="fixed_default")
        )
        assert record["terminal_value_eur_per_kwh"] is None
        assert record["closed_cycle"] is True

    def test_oracle_kwargs_only_uses_free_when_the_core_has_it(self) -> None:
        def legacy(spec, horizon, *, time_limit_s, mip_rel_gap, mip_abs_gap):  # pragma: no cover
            raise NotImplementedError

        def modern(
            spec,
            horizon,
            *,
            time_limit_s,
            mip_rel_gap,
            mip_abs_gap,
            terminal_mode,
            terminal_value_eur_per_kwh,
        ):  # pragma: no cover
            raise NotImplementedError

        kwargs, used = bm._oracle_kwargs(legacy, "free", 10.0, 1e-6, 1e-9)
        assert used == "fixed_default"
        assert "terminal_mode" not in kwargs

        kwargs, used = bm._oracle_kwargs(modern, "free", 10.0, 1e-6, 1e-9)
        assert used == "free"
        assert kwargs["terminal_mode"] == "free"
        assert kwargs["terminal_value_eur_per_kwh"] == 0.0

        kwargs, used = bm._oracle_kwargs(modern, "fixed", 10.0, 1e-6, 1e-9)
        assert used == "fixed_default"
        assert "terminal_mode" not in kwargs
        with pytest.raises(bm.BenchmarkInputError, match="terminal_mode"):
            bm._oracle_kwargs(modern, "sideways", 10.0, 1e-6, 1e-9)


class TestTaxBasis:
    def test_null_rate_is_untaxed(self) -> None:
        config = make_tariff_config(iee_pct=None, iva_pct=None)
        basis = bm.tax_basis(config, [datetime(2026, 1, 1, tzinfo=UTC)])
        assert basis["cost_basis"] == "untaxed"
        assert basis["has_unconfirmed_rate"] is True

    def test_uniform_rate_is_taxed(self) -> None:
        config = make_tariff_config(iee_pct=5.1, iva_pct=21.0)
        basis = bm.tax_basis(config, [datetime(2026, 1, 1, tzinfo=UTC)])
        assert basis["cost_basis"] == "taxed"
        assert basis["uniform_multiplier"] == pytest.approx(1.051 * 1.21)

    def test_rate_change_inside_window_is_not_a_uniform_scaling(self) -> None:
        from dataclasses import replace

        from energy_baseline.config import TaxEntry

        config = replace(
            make_tariff_config(iee_pct=5.1, iva_pct=21.0),
            taxes=[
                TaxEntry(
                    valid_from=datetime(2024, 1, 1).date(),
                    iee_pct=5.1,
                    iva_pct=21.0,
                    source="test",
                ),
                TaxEntry(
                    valid_from=datetime(2026, 6, 1).date(),
                    iee_pct=5.1,
                    iva_pct=19.0,
                    source="test",
                ),
            ],
        )
        stamps = [datetime(2026, 1, 1, tzinfo=UTC), datetime(2026, 8, 1, tzinfo=UTC)]
        basis = bm.tax_basis(config, stamps)
        # Two rates ⇒ the objective cannot be scaled uniformly: stay untaxed.
        assert basis["cost_basis"] == "untaxed"
        assert basis["uniform_multiplier"] is None
        assert len(basis["distinct_multipliers"]) == 2

    def test_empty_stamps_are_treated_as_unconfirmed(self) -> None:
        basis = bm.tax_basis(make_tariff_config(iee_pct=5.1, iva_pct=21.0), [])
        assert basis["cost_basis"] == "untaxed"


# ---------------------------------------------------------------------------
# End-to-end benchmark
# ---------------------------------------------------------------------------


class TestBenchmarkDataset:
    def test_end_to_end_run(self, tmp_path: Path) -> None:
        requires_core()
        start = datetime(2026, 1, 1, tzinfo=UTC)
        # 7 days of 15-min intervals = 672 intervals
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

        train_end = start + timedelta(days=2)
        run_dir = bm.benchmark_dataset(
            dataset=dataset_dir,
            config_path=config_path,
            train_end=train_end,
            time_limit_s=10.0,
        )
        assert run_dir.is_dir()
        assert (run_dir / bm.RESULTS_JSON_NAME).is_file()
        assert (run_dir / bm.RESULTS_PARQUET_NAME).is_file()
        assert (run_dir / bm.SCHEDULES_PARQUET_NAME).is_file()

        # Verify results.parquet has exactly BENCHMARK_RESULT schema
        results = pq.read_table(run_dir / bm.RESULTS_PARQUET_NAME)
        from energy_baseline.schemas import validate_table

        validate_table("BENCHMARK_RESULT", results)

        # Verify benchmark.json has expected keys
        payload = json.loads((run_dir / bm.RESULTS_JSON_NAME).read_text())
        assert payload["run_id"]
        assert payload["lineage"]["dataset_id"] == "test-dataset-001"
        # The evaluation window is everything from train_end (2 days in) onwards.
        assert payload["evaluation"]["dataset_interval_count"] == intervals
        assert payload["evaluation"]["interval_count"] == intervals - 2 * 24 * 4
        assert payload["training"]["train_end"]
        assert "no_battery" in payload["policies"]
        assert "causal_price" in payload["policies"]
        assert "oracle" in payload["policies"]

        # Oracle should dominate baseline and causal (within tolerance)
        oracle_cost = payload["policies"]["oracle"]["cost_eur"]
        baseline_cost = payload["policies"]["no_battery"]["cost_eur"]
        causal_cost = payload["policies"]["causal_price"]["cost_eur"]
        assert oracle_cost <= baseline_cost + 1e-6
        assert oracle_cost <= causal_cost + 1e-6

    def test_month_grid_is_local_not_utc(self, tmp_path: Path) -> None:
        """Months come from the local calendar, not from a UTC truncation.

        Madrid is UTC+1 in January, so the interval starting 2026-01-31T23:00Z is
        2026-02-01T00:00 local: every interval here starts in January by UTC but
        the last two are February by ``local_day``.  Bucketing on the local day
        must put them in February.
        """
        requires_core()
        start = datetime(2026, 1, 31, 22, 30, tzinfo=UTC)
        intervals = 4  # all four start in January by UTC
        table = make_analysis_table(start=start, intervals=intervals)
        dataset_dir = write_synthetic_dataset(tmp_path, table)
        local_days = table.column("local_day").to_pylist()
        utc_months = {s.month for s in table.column("interval_start").to_pylist()}
        assert utc_months == {1}
        assert [d.month for d in local_days] == [1, 1, 2, 2]

        config_path = tmp_path / "tariff.yaml"
        write_tariff_config(config_path, make_tariff_config())
        run_dir = bm.benchmark_dataset(
            dataset=dataset_dir,
            config_path=config_path,
            train_end=datetime(2026, 1, 31, 23, 0, tzinfo=UTC),
            train_days=30,
            time_limit_s=5.0,
        )
        payload = json.loads((run_dir / bm.RESULTS_JSON_NAME).read_text())
        per_month = {row["month"]: row for row in payload["quality"]["per_month"]}
        assert per_month["2026-01"]["intervals_total"] == 2
        assert per_month["2026-02"]["intervals_total"] == 2
        # A UTC bucket would have reported all four in January and none in February.

    def test_run_id_is_content_hash_and_immutable(self, tmp_path: Path) -> None:
        requires_core()
        start = datetime(2026, 1, 1, tzinfo=UTC)
        intervals = 3 * 24 * 4
        rng = np.random.default_rng(7)
        prices = rng.uniform(0.05, 0.30, intervals).tolist()
        table = make_analysis_table(start=start, intervals=intervals, prices=prices)
        dataset_dir = write_synthetic_dataset(tmp_path, table)
        config_path = tmp_path / "tariff.yaml"
        write_tariff_config(config_path, make_tariff_config())

        cut = start + timedelta(days=1)
        first = bm.benchmark_dataset(
            dataset=dataset_dir, config_path=config_path, train_end=cut, time_limit_s=5.0
        )
        # Same inputs, same content: the same run id, and re-writing is idempotent.
        second = bm.benchmark_dataset(
            dataset=dataset_dir, config_path=config_path, train_end=cut, time_limit_s=5.0
        )
        assert first == second

        # A different train_end freezes different thresholds: a different run.
        third = bm.benchmark_dataset(
            dataset=dataset_dir,
            config_path=config_path,
            train_end=cut + timedelta(hours=12),
            time_limit_s=5.0,
        )
        assert third != first
        run_ids = sorted(p.name for p in (dataset_dir / bm.BENCHMARKS_DIRNAME).iterdir())
        assert len(run_ids) == 2
        assert all(len(r) == 32 for r in run_ids)

    def test_tampered_part_is_rejected_before_solving(self, tmp_path: Path) -> None:
        """An input hash failure aborts the run — no partial artifact is written."""
        requires_core()
        start = datetime(2026, 1, 1, tzinfo=UTC)
        table = make_analysis_table(start=start, intervals=24 * 4 * 3)
        dataset_dir = write_synthetic_dataset(tmp_path, table)
        config_path = tmp_path / "tariff.yaml"
        write_tariff_config(config_path, make_tariff_config())

        part = dataset_dir / "parts" / "analysis-test-dataset-001-00000000.parquet"
        original = part.read_bytes()
        part.write_bytes(original + b"\x00")
        with pytest.raises(bm.BenchmarkInputError, match="input hash failure"):
            bm.benchmark_dataset(
                dataset=dataset_dir,
                config_path=config_path,
                train_end=start + timedelta(days=1),
                time_limit_s=5.0,
            )
        assert not (dataset_dir / bm.BENCHMARKS_DIRNAME).exists()

    def test_free_terminal_mode_is_used_and_reported(self, tmp_path: Path) -> None:
        requires_core()
        start = datetime(2026, 1, 1, tzinfo=UTC)
        table = make_analysis_table(start=start, intervals=3 * 24 * 4)
        dataset_dir = write_synthetic_dataset(tmp_path, table)
        config_path = tmp_path / "tariff.yaml"
        write_tariff_config(config_path, make_tariff_config())

        run_dir = bm.benchmark_dataset(
            dataset=dataset_dir,
            config_path=config_path,
            train_end=start + timedelta(days=1),
            time_limit_s=5.0,
        )
        payload = json.loads((run_dir / bm.RESULTS_JSON_NAME).read_text())
        term = payload["assumptions"]["terminal_accounting"]
        # The core now exposes the free mode, so the benchmark must use it and say
        # plainly that the horizon is not a closed cycle.
        assert payload["solver"]["oracle_terminal_mode_used"] == "free"
        assert term["used"] == "free"
        assert term["terminal_value_eur_per_kwh"] == 0.0
        assert term["closed_cycle"] is False
        assert "NOT a closed cycle" in term["note"]
        oracle = payload["policies"]["oracle"]
        assert oracle["meter_cost_eur"] is not None
        # With the terminal inventory valued at zero, the adjustment is exactly zero
        # and the reported cost is the metered cash.
        assert oracle["inventory_adjustment_eur"] == pytest.approx(0.0, abs=1e-12)
        assert oracle["cost_eur"] == pytest.approx(oracle["meter_cost_eur"], abs=1e-9)

    def test_closed_terminal_mode_is_opt_in(self, tmp_path: Path) -> None:
        requires_core()
        start = datetime(2026, 1, 1, tzinfo=UTC)
        table = make_analysis_table(start=start, intervals=2 * 24 * 4)
        dataset_dir = write_synthetic_dataset(tmp_path, table)
        config_path = tmp_path / "tariff.yaml"
        write_tariff_config(config_path, make_tariff_config())

        run_dir = bm.benchmark_dataset(
            dataset=dataset_dir,
            config_path=config_path,
            train_end=start + timedelta(hours=12),
            terminal_mode="fixed",
            time_limit_s=5.0,
        )
        payload = json.loads((run_dir / bm.RESULTS_JSON_NAME).read_text())
        term = payload["assumptions"]["terminal_accounting"]
        assert term["used"] == "fixed_default"
        assert term["closed_cycle"] is True
        assert payload["policies"]["oracle"]["terminal_value_eur_per_kwh"] is None

    def test_unknown_terminal_mode_raises(self, tmp_path: Path) -> None:
        requires_core()
        start = datetime(2026, 1, 1, tzinfo=UTC)
        table = make_analysis_table(start=start, intervals=24 * 4 * 3)
        dataset_dir = write_synthetic_dataset(tmp_path, table)
        config_path = tmp_path / "tariff.yaml"
        write_tariff_config(config_path, make_tariff_config())
        with pytest.raises(bm.BenchmarkInputError, match="terminal_mode"):
            bm.benchmark_dataset(
                dataset=dataset_dir,
                config_path=config_path,
                train_end=start + timedelta(days=1),
                terminal_mode="whatever",
            )

    def test_results_parquet_matches_canonical_schema(self, tmp_path: Path) -> None:
        requires_core()
        from energy_baseline.schemas import BENCHMARK_RESULT

        start = datetime(2026, 1, 1, tzinfo=UTC)
        table = make_analysis_table(start=start, intervals=2 * 24 * 4)
        dataset_dir = write_synthetic_dataset(tmp_path, table)
        config_path = tmp_path / "tariff.yaml"
        write_tariff_config(config_path, make_tariff_config())
        run_dir = bm.benchmark_dataset(
            dataset=dataset_dir,
            config_path=config_path,
            train_end=start + timedelta(hours=12),
            time_limit_s=5.0,
        )
        results = pq.read_table(run_dir / bm.RESULTS_PARQUET_NAME)
        # Exactly the registered columns: bounds and tolerances live in JSON only.
        assert results.schema == BENCHMARK_RESULT
        assert set(results.column("policy").to_pylist()) == set(bm.POLICY_NAMES)

    def test_tax_null_produces_untaxed_basis_only(self, tmp_path: Path) -> None:
        """A null tax rate must never produce a billed figure, only untaxed cost."""
        requires_core()
        start = datetime(2026, 1, 1, tzinfo=UTC)
        table = make_analysis_table(start=start, intervals=2 * 24 * 4)
        dataset_dir = write_synthetic_dataset(tmp_path, table)
        config_path = tmp_path / "tariff.yaml"
        write_tariff_config(config_path, make_tariff_config(iee_pct=None, iva_pct=None))

        run_dir = bm.benchmark_dataset(
            dataset=dataset_dir,
            config_path=config_path,
            train_end=start + timedelta(hours=12),
            time_limit_s=5.0,
        )
        payload = json.loads((run_dir / bm.RESULTS_JSON_NAME).read_text())
        assert payload["assumptions"]["cost_objective"]["cost_basis"] == "untaxed"
        assert payload["assumptions"]["cost_objective"]["has_unconfirmed_rate"] is True
        results = pq.read_table(run_dir / bm.RESULTS_PARQUET_NAME)
        assert set(results.column("cost_basis").to_pylist()) == {"untaxed"}
        # The taxed schedule column is left empty rather than filled with the pretax
        # value, so no reader can mistake it for a billed cost.
        schedules = pq.read_table(run_dir / bm.SCHEDULES_PARQUET_NAME)
        assert schedules.column("interval_cost_taxed_eur").null_count == schedules.num_rows

    def test_taxed_basis_when_rate_is_confirmed(self, tmp_path: Path) -> None:
        requires_core()
        start = datetime(2026, 1, 1, tzinfo=UTC)
        table = make_analysis_table(start=start, intervals=2 * 24 * 4)
        config_path = tmp_path / "tariff.yaml"
        write_tariff_config(config_path, make_tariff_config(iee_pct=5.1, iva_pct=21.0))
        dataset_dir = write_synthetic_dataset(tmp_path, table, config_path=config_path)

        run_dir = bm.benchmark_dataset(
            dataset=dataset_dir,
            config_path=config_path,
            train_end=start + timedelta(hours=12),
            time_limit_s=5.0,
        )
        payload = json.loads((run_dir / bm.RESULTS_JSON_NAME).read_text())
        basis = payload["assumptions"]["cost_objective"]
        assert basis["cost_basis"] == "taxed"
        assert basis["uniform_multiplier"] == pytest.approx(1.051 * 1.21)
        results = pq.read_table(run_dir / bm.RESULTS_PARQUET_NAME)
        assert set(results.column("cost_basis").to_pylist()) == {"taxed"}
        # The objective stays pretax (a uniform scaling does not move the argmin),
        # so cost_eur is unchanged while the taxed schedule column is populated.
        schedules = pq.read_table(run_dir / bm.SCHEDULES_PARQUET_NAME)
        assert schedules.column("interval_cost_taxed_eur").null_count < schedules.num_rows

    def test_dst_transition_day_lands_in_one_month(self, tmp_path: Path) -> None:
        """A 23-hour spring-forward day stays whole inside its local month.

        Madrid springs forward on 2026-03-29, so that local day is 23 h = 92
        intervals, running 2026-03-28T23:00Z .. 2026-03-29T22:00Z.  A UTC month
        bucket would split it across March and April on the hour boundaries; the
        local grid keeps all 92 in one day and the day in March.
        """
        requires_core()
        start = datetime(2026, 3, 28, 23, 0, tzinfo=UTC)
        intervals = 92 + 96  # the 23-hour day, then a normal 24-hour day
        table = make_analysis_table(start=start, intervals=intervals)
        local_days = table.column("local_day").to_pylist()
        assert local_days.count(local_days[0]) == 92  # the 23-h DST day
        assert local_days[0].day == 29 and local_days[0].month == 3

        dataset_dir = write_synthetic_dataset(tmp_path, table)
        config_path = tmp_path / "tariff.yaml"
        write_tariff_config(config_path, make_tariff_config())
        run_dir = bm.benchmark_dataset(
            dataset=dataset_dir,
            config_path=config_path,
            train_end=start + timedelta(hours=23),  # cut at the end of the DST day
            train_days=30,
            time_limit_s=5.0,
        )
        payload = json.loads((run_dir / bm.RESULTS_JSON_NAME).read_text())
        rows = payload["quality"]["per_month"]
        assert [r["month"] for r in rows] == ["2026-03"]
        month = rows[0]
        assert month["intervals_total"] == intervals
        # Counts are conserved: every interval is either usable or excluded.
        assert month["intervals_usable"] + month["intervals_excluded"] == month["intervals_total"]
        assert payload["evaluation"]["horizon_start"] == "2026-03-29T22:00:00Z"

    def test_zero_usable_eval_is_refused(self, tmp_path: Path) -> None:
        requires_core()
        start = datetime(2026, 1, 1, tzinfo=UTC)
        table = make_analysis_table(start=start, intervals=48)
        dataset_dir = write_synthetic_dataset(tmp_path, table)
        config_path = tmp_path / "tariff.yaml"
        write_tariff_config(config_path, make_tariff_config())
        with pytest.raises(bm.BenchmarkInputError, match="0 usable intervals"):
            bm.benchmark_dataset(
                dataset=dataset_dir,
                config_path=config_path,
                train_end=start + timedelta(hours=48),
            )

    def test_policy_selection_subset(self, tmp_path: Path) -> None:
        requires_core()
        start = datetime(2026, 1, 1, tzinfo=UTC)
        table = make_analysis_table(start=start, intervals=2 * 24 * 4)
        dataset_dir = write_synthetic_dataset(tmp_path, table)
        config_path = tmp_path / "tariff.yaml"
        write_tariff_config(config_path, make_tariff_config())
        run_dir = bm.benchmark_dataset(
            dataset=dataset_dir,
            config_path=config_path,
            train_end=start + timedelta(hours=12),
            policies="no_battery,oracle",
            time_limit_s=5.0,
        )
        payload = json.loads((run_dir / bm.RESULTS_JSON_NAME).read_text())
        assert set(payload["policies"]) == {"no_battery", "oracle"}
        # No causal policy: no thresholds were fitted, and that is recorded.
        assert payload["training"]["thresholds_were_fitted"] is False
        assert payload["training"]["charge_threshold_eur_per_kwh"] is None

    def test_unknown_policy_raises(self, tmp_path: Path) -> None:
        start = datetime(2026, 1, 1, tzinfo=UTC)
        table = make_analysis_table(start=start, intervals=4)
        dataset_dir = write_synthetic_dataset(tmp_path, table)
        config_path = tmp_path / "tariff.yaml"
        write_tariff_config(config_path, make_tariff_config())
        with pytest.raises(bm.BenchmarkInputError, match="unknown policies"):
            bm.benchmark_dataset(
                dataset=dataset_dir,
                config_path=config_path,
                train_end=start + timedelta(hours=1),
                policies="reinforcement_learning",
            )

    def test_negative_prices_handled(self, tmp_path: Path) -> None:
        requires_core()
        start = datetime(2026, 1, 1, tzinfo=UTC)
        intervals = 48
        # Mix of negative and positive prices
        prices = [-0.05, 0.10, -0.02, 0.15] * (intervals // 4)
        gross = [0.5] * intervals
        table = make_analysis_table(
            start=start, intervals=intervals, gross_kwh=gross, prices=prices
        )
        dataset_dir = write_synthetic_dataset(tmp_path, table)

        config = make_tariff_config()
        config_path = tmp_path / "tariff.yaml"
        write_tariff_config(config_path, config)

        train_end = start + timedelta(hours=6)
        run_dir = bm.benchmark_dataset(
            dataset=dataset_dir,
            config_path=config_path,
            train_end=train_end,
            time_limit_s=5.0,
        )
        payload = json.loads((run_dir / bm.RESULTS_JSON_NAME).read_text())
        # Oracle should handle negative prices and still dominate both.
        oracle_cost = payload["policies"]["oracle"]["cost_eur"]
        baseline_cost = payload["policies"]["no_battery"]["cost_eur"]
        causal_cost = payload["policies"]["causal_price"]["cost_eur"]
        assert oracle_cost <= baseline_cost + 1e-6
        assert oracle_cost <= causal_cost + 1e-6
        assert payload["comparison"]["oracle_dominates_baseline"] is True
        assert payload["comparison"]["oracle_dominates_causal"] is True

        # Negative prices survive into the plan: charging into them is the point.
        schedules = pq.read_table(run_dir / bm.SCHEDULES_PARQUET_NAME)
        oracle_plan = schedules.filter(pc.field("policy") == bm.POLICY_ORACLE)
        assert float(np.nansum(oracle_plan.column("charge_kwh").to_pylist())) > 0.0

        # The objective is metered cash with zero terminal value, so a free-terminal
        # oracle pays nothing for leftover inventory — and is never credited either.
        assert payload["policies"]["oracle"]["inventory_adjustment_eur"] == pytest.approx(0.0)
        assert payload["policies"]["oracle"]["cost_eur"] == pytest.approx(
            payload["policies"]["oracle"]["meter_cost_eur"], abs=1e-9
        )

    def test_time_limit_recorded(self, tmp_path: Path) -> None:
        requires_core()
        start = datetime(2026, 1, 1, tzinfo=UTC)
        intervals = 96
        table = make_analysis_table(start=start, intervals=intervals)
        dataset_dir = write_synthetic_dataset(tmp_path, table)

        config = make_tariff_config()
        config_path = tmp_path / "tariff.yaml"
        write_tariff_config(config_path, config)

        train_end = start + timedelta(hours=12)
        run_dir = bm.benchmark_dataset(
            dataset=dataset_dir,
            config_path=config_path,
            train_end=train_end,
            time_limit_s=7.5,
        )
        payload = json.loads((run_dir / bm.RESULTS_JSON_NAME).read_text())
        oracle_record = payload["policies"]["oracle"]
        assert oracle_record["solver_time_limit_s"] == 7.5
        assert oracle_record["configured_mip_rel_gap"] == bm.DEFAULT_MIP_REL_GAP

    def test_min_usable_fraction_enforced(self, tmp_path: Path) -> None:
        requires_core()
        start = datetime(2026, 1, 1, tzinfo=UTC)
        intervals = 96
        # Half the intervals have missing prices
        prices = [0.15 if i % 2 == 0 else None for i in range(intervals)]
        table = make_analysis_table(start=start, intervals=intervals, prices=prices)
        dataset_dir = write_synthetic_dataset(tmp_path, table)

        config = make_tariff_config()
        config_path = tmp_path / "tariff.yaml"
        write_tariff_config(config_path, config)

        train_end = start + timedelta(hours=12)
        # With min_usable_fraction=0.6, the run should fail (only 50% usable)
        with pytest.raises(bm.BenchmarkInputError, match="monthly quality threshold not met"):
            bm.benchmark_dataset(
                dataset=dataset_dir,
                config_path=config_path,
                train_end=train_end,
                min_usable_fraction=0.6,
                time_limit_s=5.0,
            )

    def test_allow_reconstructed_quality(self, tmp_path: Path) -> None:
        requires_core()
        start = datetime(2026, 1, 1, tzinfo=UTC)
        intervals = 96
        # Some intervals have forward-filled prices
        price_quality = ["measured" if i % 3 != 0 else "forward_filled" for i in range(intervals)]
        table = make_analysis_table(start=start, intervals=intervals, price_quality=price_quality)
        dataset_dir = write_synthetic_dataset(tmp_path, table)

        config = make_tariff_config()
        config_path = tmp_path / "tariff.yaml"
        write_tariff_config(config_path, config)

        train_end = start + timedelta(hours=12)

        # Without the flag, the forward-filled intervals are excluded.
        run_strict = bm.benchmark_dataset(
            dataset=dataset_dir,
            config_path=config_path,
            train_end=train_end,
            allow_reconstructed_quality=False,
            time_limit_s=5.0,
        )
        strict = json.loads((run_strict / bm.RESULTS_JSON_NAME).read_text())
        strict_excluded = strict["evaluation"]["excluded_interval_count"]
        assert strict_excluded > 0
        assert any(
            reason.startswith("price_quality=forward_filled")
            for reason in strict["quality"]["eval_exclusion_reasons"]
        )

        # With the flag they are admitted, so nothing is excluded.
        run_allowed = bm.benchmark_dataset(
            dataset=dataset_dir,
            config_path=config_path,
            train_end=train_end,
            allow_reconstructed_quality=True,
            time_limit_s=5.0,
        )
        allowed = json.loads((run_allowed / bm.RESULTS_JSON_NAME).read_text())
        assert allowed["evaluation"]["excluded_interval_count"] == 0
        assert strict_excluded > allowed["evaluation"]["excluded_interval_count"]


# ---------------------------------------------------------------------------
# New tests for the four fixes
# ---------------------------------------------------------------------------


class TestFix1UsabilityVocabulary:
    """Fix 1: estimated_boundary default-usable, flag gates price/energy reconstruction."""

    def test_estimated_boundary_is_default_usable(self) -> None:
        """A dataset that is 100% estimated_boundary is benchmarkable without the flag."""
        start = datetime(2026, 1, 1, tzinfo=UTC)
        intervals = 96
        table = make_analysis_table(
            start=start,
            intervals=intervals,
            allocation_quality=["estimated_boundary"] * intervals,
        )
        usable, _ = bm.usable_mask(table, allow_reconstructed=False)
        assert usable.all(), "estimated_boundary is default-usable, not opt-in"

    def test_unknown_gap_excluded_even_with_flag(self) -> None:
        """unknown_gap rows are excluded even with allow_reconstructed=True."""
        start = datetime(2026, 1, 1, tzinfo=UTC)
        intervals = 10
        table = make_analysis_table(
            start=start,
            intervals=intervals,
            allocation_quality=["unknown_gap"] * intervals,
        )
        usable, _ = bm.usable_mask(table, allow_reconstructed=True)
        assert not usable.any(), "unknown_gap is never benchmarkable, with or without the flag"

    def test_forward_filled_price_requires_flag(self) -> None:
        """forward_filled prices are excluded without the flag, included with it."""
        start = datetime(2026, 1, 1, tzinfo=UTC)
        intervals = 10
        table = make_analysis_table(
            start=start,
            intervals=intervals,
            price_quality=["forward_filled"] * intervals,
        )
        strict, _ = bm.usable_mask(table, allow_reconstructed=False)
        lenient, _ = bm.usable_mask(table, allow_reconstructed=True)
        assert not strict.any(), "forward_filled excluded without the flag"
        assert lenient.all(), "forward_filled included with the flag"

    def test_missing_and_ambiguous_never_benchmarkable(self) -> None:
        """missing and ambiguous prices are excluded either way."""
        start = datetime(2026, 1, 1, tzinfo=UTC)
        intervals = 10
        for quality in ("missing", "ambiguous"):
            table = make_analysis_table(
                start=start,
                intervals=intervals,
                price_quality=[quality] * intervals,
            )
            strict, _ = bm.usable_mask(table, allow_reconstructed=False)
            lenient, _ = bm.usable_mask(table, allow_reconstructed=True)
            assert not strict.any(), f"{quality} excluded without the flag"
            assert not lenient.any(), f"{quality} excluded even with the flag"


class TestFix2ConfigDrift:
    """Fix 2: benchmark_dataset hard-fails on config drift."""

    def test_config_drift_raises(self, tmp_path: Path) -> None:
        """Benchmarking with a different config than the build raises."""
        start = datetime(2026, 1, 1, tzinfo=UTC)
        intervals = 96
        table = make_analysis_table(start=start, intervals=intervals)

        # Build with config A
        config_a_path = tmp_path / "config_a.yaml"
        write_tariff_config(config_a_path, make_tariff_config())
        dataset_dir = write_synthetic_dataset(tmp_path, table, config_path=config_a_path)

        # Benchmark with config B (different)
        config_b_path = tmp_path / "config_b.yaml"
        write_tariff_config(config_b_path, make_tariff_config(round_trip_efficiency=0.85))

        with pytest.raises(bm.BenchmarkInputError, match="tariff config drift"):
            bm.benchmark_dataset(
                dataset=dataset_dir,
                config_path=config_b_path,
                train_end=start + timedelta(hours=12),
                time_limit_s=5.0,
            )

    def test_same_config_proceeds(self, tmp_path: Path) -> None:
        """Benchmarking with the same config as the build proceeds."""
        requires_core()
        start = datetime(2026, 1, 1, tzinfo=UTC)
        intervals = 96
        table = make_analysis_table(start=start, intervals=intervals)

        config_path = tmp_path / "tariff.yaml"
        write_tariff_config(config_path, make_tariff_config())
        dataset_dir = write_synthetic_dataset(tmp_path, table, config_path=config_path)

        # Should not raise
        run_dir = bm.benchmark_dataset(
            dataset=dataset_dir,
            config_path=config_path,
            train_end=start + timedelta(hours=12),
            time_limit_s=5.0,
        )
        assert run_dir.is_dir()


class TestFix3DatasetIdDirtyAware:
    """Fix 3: dataset_id is dirty-aware."""

    def test_clean_vs_dirty_different_id(self, monkeypatch) -> None:
        """Same inputs with clean vs dirty tree produce different dataset_id."""
        from energy_baseline import build as build_mod

        # Simulate clean tree
        monkeypatch.setattr(
            "energy_baseline.manifest._get_code_version",
            lambda: ("abc123" * 6 + "abcd", False),
        )
        clean_version = build_mod.code_version()
        clean_id = build_mod.compute_dataset_id(
            part_hashes=[{"part": "a.parquet", "sha256": "aa"}],
            config_hash="cfg" * 8,
            code_version=clean_version,
            schema_version=1,
            window_start=datetime(2026, 1, 1, tzinfo=UTC),
            window_end=datetime(2026, 1, 2, tzinfo=UTC),
            params={"interval_seconds": 900},
        )

        # Simulate dirty tree
        monkeypatch.setattr(
            "energy_baseline.manifest._get_code_version",
            lambda: ("abc123" * 6 + "abcd", True),
        )
        dirty_version = build_mod.code_version()
        dirty_id = build_mod.compute_dataset_id(
            part_hashes=[{"part": "a.parquet", "sha256": "aa"}],
            config_hash="cfg" * 8,
            code_version=dirty_version,
            schema_version=1,
            window_start=datetime(2026, 1, 1, tzinfo=UTC),
            window_end=datetime(2026, 1, 2, tzinfo=UTC),
            params={"interval_seconds": 900},
        )

        assert clean_id != dirty_id, "clean and dirty trees produce different dataset_id"
        assert "-dirty" in dirty_version
        assert "-dirty" not in clean_version

    def test_identical_tree_identical_id(self, monkeypatch) -> None:
        """Identical tree twice produces identical dataset_id."""
        from energy_baseline import build as build_mod

        monkeypatch.setattr(
            "energy_baseline.manifest._get_code_version",
            lambda: ("abc123" * 6 + "abcd", False),
        )
        version = build_mod.code_version()
        id1 = build_mod.compute_dataset_id(
            part_hashes=[{"part": "a.parquet", "sha256": "aa"}],
            config_hash="cfg" * 8,
            code_version=version,
            schema_version=1,
            window_start=datetime(2026, 1, 1, tzinfo=UTC),
            window_end=datetime(2026, 1, 2, tzinfo=UTC),
            params={"interval_seconds": 900},
        )
        id2 = build_mod.compute_dataset_id(
            part_hashes=[{"part": "a.parquet", "sha256": "aa"}],
            config_hash="cfg" * 8,
            code_version=version,
            schema_version=1,
            window_start=datetime(2026, 1, 1, tzinfo=UTC),
            window_end=datetime(2026, 1, 2, tzinfo=UTC),
            params={"interval_seconds": 900},
        )
        assert id1 == id2, "identical inputs produce identical dataset_id"


class TestFix4InitialSocProvenance:
    """Fix 4: initial_soc_frac reads config when arg is None."""

    def test_config_value_honored_when_arg_is_none(self) -> None:
        """Config value is used when the argument is None."""
        config = make_tariff_config(soc_window_min=0.1, soc_window_max=0.9)
        object.__setattr__(config.battery, "initial_soc_frac", 0.5)
        assumptions = bm.resolve_battery_assumptions(config)
        assert assumptions.initial_soc_frac == 0.5
        assert assumptions.initial_soc_source == "config: battery.initial_soc_frac"

    def test_cli_wins_over_config(self) -> None:
        """CLI argument wins over config value."""
        config = make_tariff_config(soc_window_min=0.1, soc_window_max=0.9)
        object.__setattr__(config.battery, "initial_soc_frac", 0.5)
        assumptions = bm.resolve_battery_assumptions(config, initial_soc_frac=0.7)
        assert assumptions.initial_soc_frac == 0.7
        assert "argument: initial_soc_frac" in assumptions.initial_soc_source

    def test_default_when_both_none(self) -> None:
        """Default to soc_window_min when both arg and config are None."""
        config = make_tariff_config(soc_window_min=0.1, soc_window_max=0.9)
        # initial_soc_frac is None by default in make_tariff_config
        assumptions = bm.resolve_battery_assumptions(config)
        assert assumptions.initial_soc_frac == 0.1
        assert "default: initial SOC = soc_window_min" in assumptions.initial_soc_source

    def test_out_of_window_config_raises(self) -> None:
        """Out-of-window config value raises."""
        config = make_tariff_config(soc_window_min=0.1, soc_window_max=0.9)
        object.__setattr__(config.battery, "initial_soc_frac", 0.95)  # outside [0.1, 0.9]
        with pytest.raises(bm.BenchmarkInputError, match="outside the configured SOC window"):
            bm.resolve_battery_assumptions(config)
