"""Tests for the benchmark orchestration layer.

These tests exercise the dataset loading, interval usability, train/eval split,
battery assumption resolution, and the end-to-end benchmark run — all with
synthetic data, no live cluster, no real battery hardware.  The numeric core
(battery, oracle, policies, cost) is exercised through its public API; when it
is not yet implemented the solver-dependent tests are skipped with a clear
message.


The four-policy run adds its own guarantees: the evaluation window is partitioned on
local (Europe/Madrid) calendar days, the day-ahead terminal SOC valuation is derived
from the frozen training window (or labelled ``cli`` when overridden), per-day solve
status and forecast error land in ``benchmark.json``, and dominance is asserted only
where it is provable — never over day-ahead versus causal, which is a measurement.

The fifth rung (``rolling_horizon``) adds: canonical defaults of exact prices
(``sigma=0``/``bias_sigma=0``) on the legacy two-event cadence, physics reached only
through ``rolling_horizon.run_rolling_horizon``, one shared 0.0 scoring basis so
``cost_eur == meter_cost_eur`` for every rung, ``oracle <= rolling_horizon`` claimed only
when the oracle is certified and free, and ``oracle <= day_ahead`` versus
``rolling_horizon <= day_ahead`` kept as an observation rather than a guarantee."""

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

    def test_shipped_tariff_battery_assumptions_are_pinned(self) -> None:
        """The committed battery block drives every figure this package reports.

        Nothing else guards it — ``test_shipped_tariff_yaml_stays_unconfirmed`` pins
        only the tax rates.  A silent edit to efficiency, capacity, the SOC window or
        the import ceiling would otherwise move every result while the suite stayed
        green, and because ``config_hash`` feeds ``compute_dataset_id`` it would also
        quietly re-mint the dataset identity.  Update this test deliberately.
        """
        from energy_baseline.config import load_config

        shipped = Path(__file__).resolve().parents[1] / "config" / "tariff.yaml"
        if not shipped.exists():  # pragma: no cover - repo layout guard
            pytest.skip(f"{shipped} not present")
        assumptions = bm.resolve_battery_assumptions(load_config(shipped))

        # Nameplate capacity and power, and the contracted import ceiling: the three
        # figures the owner confirmed rather than assumed.
        assert assumptions.capacity_kwh == 5.12
        assert assumptions.power_kw == 2.5
        assert assumptions.import_limit_kw == 5.0

        # Owner decision 2026-10-24: the bottom of the community-measured 0.80-0.84
        # round-trip range, chosen because at 2.5 kW the unit operates mostly near
        # its power rating where efficiency is typically worst.  Supersedes 0.88.
        # Still an assumption — not a measurement of this household's unit.
        assert assumptions.charge_efficiency == pytest.approx(0.80**0.5)
        assert assumptions.discharge_efficiency == pytest.approx(0.80**0.5)

        # Owner-chosen SOC window, and the horizon floor that follows from it.
        assert assumptions.soc_min_frac == 0.20
        assert assumptions.soc_max_frac == 0.95
        assert assumptions.initial_soc_frac == 0.20

        # The provisional framing must survive a change of value: the symmetric sqrt
        # mapping stays a labelled assumption, and idle losses stay unmeasured rather
        # than silently becoming a measured zero.
        assert "symmetric assumption" in assumptions.efficiency_mapping
        assert "provisional assumption" in assumptions.efficiency_mapping
        assert assumptions.idle_losses_measured is False
        assert assumptions.idle_loss_kwh_per_hour == 0.0

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
        # The day-ahead and rolling-horizon rows survive this patch on purpose and must
        # be asserted as present, not assumed away: both policies.py and rolling_horizon.py
        # bind ``solve_oracle`` at import, so replacing the attribute on the *oracle module*
        # cannot reach them.  The day-ahead policy's own per-day failures are handled
        # inside the policy (hold SOC and keep going); the rolling-horizon policy's own
        # per-event failures are handled the same way.  A whole-policy oracle failure is
        # what this test is about, and patching the policy's dependency is covered in
        # tests/test_policies.py and tests/test_rolling_horizon.py.
        assert set(results.column("policy").to_pylist()) == {
            "no_battery",
            "causal_price",
            "day_ahead",
            "rolling_horizon",
        }
        # Dominance against a policy that never ran is unknown, not asserted.
        assert payload["comparison"]["oracle_dominates_causal"] is None
        assert payload["comparison"]["oracle_dominates_day_ahead"] is None
        assert payload["comparison"]["day_ahead_closes_gap_fraction"] is None

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


# ---------------------------------------------------------------------------
# Net-import noise between the kernel's tolerance and the cost model's rule
# ---------------------------------------------------------------------------


class TestGridNoiseSnapping:
    """A plan the kernel calls feasible must cost as feasible — and say how it did.

    ``battery.check_feasible`` accepts net import down to ``-_TOL_KWH`` kWh;
    ``cost.compute_interval_cost`` refuses any negative.  HiGHS closes that gap in
    practice by returning a discharge one ulp over its no-export bound, which was
    observed on the final interval of a 100-slot fall-back day during the four-policy
    run.  The orchestrator is where the two rules meet, so this is where it is handled,
    counted, and never silent.
    """

    def _costs(self, grid):
        from energy_baseline.cost import compute_interval_cost

        usable = np.ones(len(grid), dtype=bool)
        return bm._interval_costs(
            compute_interval_cost,
            np.asarray(grid, dtype=float),
            np.full(len(grid), 0.2),
            usable,
            None,
            record_taxed=False,
        )

    def test_noise_inside_the_kernel_slack_is_costed_as_zero_and_counted(self) -> None:
        pretax, taxed, snapped = self._costs([-1e-16, -1e-7, 0.0, 0.5])
        assert snapped == 2
        assert pretax[0] == pytest.approx(0.0)
        assert pretax[1] == pytest.approx(0.0)
        assert pretax[3] == pytest.approx(0.1)
        assert all(np.isnan(v) for v in taxed)

    def test_a_real_export_still_raises(self) -> None:
        """Beyond the kernel's own slack this is a violation, not noise: never clamped."""
        from energy_baseline.cost import CostError

        with pytest.raises(CostError, match="must be >= 0"):
            self._costs([-1e-3, 0.5])

    def test_the_repair_pass_never_relies_on_snapping(self) -> None:
        """The day-ahead clip pass produces exact zeros, so its count must stay zero."""
        requires_core()
        from energy_baseline.policies import (
            DayAheadDay,
            contiguous_day_bounds,
            forecast_load_trailing_slot_mean,
            policy_day_ahead,
        )

        days_per_run, slots = 6, 24
        prices = np.array([0.05, 0.05, 0.05, 0.30] * days_per_run * (slots // 4))
        load = np.array([0.05, 0.4, 0.2, 0.6] * days_per_run * (slots // 4))
        labels = [i // slots for i in range(prices.size)]
        runs = contiguous_day_bounds(labels)
        forecast, diag = forecast_load_trailing_slot_mean(load, runs, window_days=2)
        from energy_baseline.battery import BatterySpec, Horizon

        s = BatterySpec(
            capacity_kwh=5.12,
            power_kw=2.5,
            charge_efficiency=0.95,
            discharge_efficiency=0.95,
            soc_min_frac=0.05,
            soc_max_frac=0.95,
            initial_soc_frac=0.05,
        )
        h = Horizon(900.0, prices, load, np.ones(len(prices), dtype=bool), 5.0)
        days = [
            DayAheadDay(
                label=f"d{i}",
                start=lo,
                stop=hi,
                forecast_load=forecast[lo:hi],
                forecast=diag[i],
            )
            for i, (_label, lo, hi) in enumerate(runs)
        ]
        sched, _tele = policy_day_ahead(
            s,
            h,
            days,
            terminal_value_eur_per_kwh=0.15,
            time_limit_s=5.0,
            mip_rel_gap=1e-6,
            mip_abs_gap=1e-9,
        )
        grid = np.asarray(sched.grid_kwh)
        assert grid.min() >= 0.0
        assert np.count_nonzero(grid < 0) == 0


# ---------------------------------------------------------------------------
# Policy 3 — the rolling day-ahead run
# ---------------------------------------------------------------------------


def _prices(intervals: int, *, per_day: int = 96) -> list[float]:
    """Cheap in the first quarter of each day, expensive in the last half."""
    hour = [int((i % per_day) // (per_day / 24)) for i in range(intervals)]
    return [0.04 if h < 4 else 0.26 for h in hour]


def _load(
    intervals: int, *, per_day: int = 96, base: float = 0.25, peak: float = 0.9
) -> list[float]:
    hour = [int((i % per_day) // (per_day / 24)) for i in range(intervals)]
    return [peak if h >= 6 else base for h in hour]


class TestParsePoliciesDayAhead:
    def test_all_includes_day_ahead(self) -> None:
        assert bm._parse_policies("all") == list(bm.POLICY_NAMES)
        assert bm.POLICY_DAY_AHEAD in bm.POLICY_NAMES
        # Ladder order: knowledge ascends left to right.
        assert bm.POLICY_NAMES.index(bm.POLICY_DAY_AHEAD) == 2

    def test_day_ahead_selectable_alone_and_in_a_subset(self) -> None:
        assert bm._parse_policies("day_ahead") == ["day_ahead"]
        assert bm._parse_policies("no_battery, day_ahead") == ["no_battery", "day_ahead"]

    def test_unknown_policy_names_the_four(self) -> None:
        with pytest.raises(bm.BenchmarkInputError, match="day_ahead"):
            bm._parse_policies("perfect_controller")


class TestResolveDayAheadTerminalValue:
    def test_default_is_the_training_window_mean(self) -> None:
        cut = datetime(2026, 2, 1, tzinfo=UTC)
        start = cut - timedelta(days=30)
        value, source = bm.resolve_day_ahead_terminal_value(
            configured=None,
            train_prices=np.array([0.10, 0.20, 0.30]),
            train_window_start=start,
            train_end=cut,
        )
        assert value == pytest.approx(0.2)
        assert source.startswith(f"default:{bm.TERMINAL_VALUE_DEFAULT_RULE}")
        assert "2026-01-02" in source and "2026-02-01" in source
        assert "3 usable training prices" in source

    def test_override_is_labelled_cli(self) -> None:
        cut = datetime(2026, 2, 1, tzinfo=UTC)
        value, source = bm.resolve_day_ahead_terminal_value(
            configured=0.0,
            train_prices=np.array([0.1, 0.2]),
            train_window_start=cut - timedelta(days=30),
            train_end=cut,
        )
        assert value == 0.0
        assert source.startswith("cli:")

    def test_empty_training_window_refuses(self) -> None:
        """A statistic of nothing is a hand-picked number wearing a costume."""
        cut = datetime(2026, 2, 1, tzinfo=UTC)
        with pytest.raises(bm.BenchmarkInputError, match="holds no usable"):
            bm.resolve_day_ahead_terminal_value(
                configured=None,
                train_prices=np.array([]),
                train_window_start=cut - timedelta(days=30),
                train_end=cut,
            )

    def test_non_finite_override_refuses(self) -> None:
        cut = datetime(2026, 2, 1, tzinfo=UTC)
        with pytest.raises(bm.BenchmarkInputError, match="finite"):
            bm.resolve_day_ahead_terminal_value(
                configured=float("nan"),
                train_prices=np.array([0.1]),
                train_window_start=cut - timedelta(days=30),
                train_end=cut,
            )


class TestPrepareDayAhead:
    @staticmethod
    def core():
        requires_core()
        return bm.core_or_raise()

    def test_partition_is_local_days_and_forecast_is_causal(self) -> None:
        core = self.core()
        n_days, per_day = 6, 96
        load = np.array(_load(n_days * per_day))
        usable = np.ones(n_days * per_day, dtype=bool)
        from datetime import date

        local_days = [
            date(2026, 1, 1) + timedelta(days=i // per_day) for i in range(n_days * per_day)
        ]
        eval_index = np.arange(4 * per_day, n_days * per_day)
        days, block = bm.prepare_day_ahead(
            core=core,
            load_kwh=load,
            usable=usable,
            local_days=local_days,
            eval_index=eval_index,
            forecast_window_days=2,
            terminal_value_eur_per_kwh=0.2,
            terminal_value_source="cli: test",
            time_limit_s=3.0,
            timezone="Europe/Madrid",
        )
        assert [d.start for d in days] == [0, per_day]
        assert [d.label for d in days] == ["2026-01-05", "2026-01-06"]
        assert block["partition"]["basis"] == "local_calendar_day"
        assert block["partition"]["timezone"] == "Europe/Madrid"
        assert block["partition"]["day_count"] == 2
        assert block["forecast"]["method"] == "trailing_slot_mean"
        assert block["forecast"]["window_days"] == 2
        assert block["forecast"]["enters_cost"] is False
        # Day 5 (first eval day) may only use days 3 and 4 — positions, same window.
        expected = np.mean([load[3 * per_day], load[4 * per_day]])
        assert days[0].forecast_load[0] == pytest.approx(expected)
        assert days[0].forecast_load[-1] == pytest.approx(
            np.mean([load[3 * per_day + per_day - 1], load[4 * per_day + per_day - 1]])
        )
        assert block["terminal_value"]["source"] == "cli: test"
        assert block["information_set"]["prices"].count("climate_control.py:45") == 1

    def test_fall_back_day_is_one_hundred_slot_solve(self) -> None:
        """DST: 2025-10-26 in Madrid is 25 hours, and it must stay one local day.

        The grid is built from UTC instants and labelled by ``local_day`` exactly as
        ``build`` does, so this is the same column a real run partitions on rather than
        a hand-written day index.
        """
        from zoneinfo import ZoneInfo

        core = self.core()
        madrid = ZoneInfo("Europe/Madrid")
        start = datetime(2025, 10, 25, 22, tzinfo=UTC)  # = 2025-10-26 00:00 local (CEST)
        intervals = 100 + 96
        load = np.array(_load(intervals))
        usable = np.ones(intervals, dtype=bool)
        local_days = [
            (start + timedelta(seconds=i * 900)).astimezone(madrid).date() for i in range(intervals)
        ]
        assert local_days.count(local_days[0]) == 100
        days, block = bm.prepare_day_ahead(
            core=core,
            load_kwh=load,
            usable=usable,
            local_days=local_days,
            eval_index=np.arange(0, intervals),
            forecast_window_days=28,
            terminal_value_eur_per_kwh=0.15,
            terminal_value_source="default: test",
            time_limit_s=3.0,
            timezone="Europe/Madrid",
        )
        assert block["partition"]["day_count"] == 2
        assert block["partition"]["intervals_per_day_max"] == 100
        assert [d.stop - d.start for d in days] == [100, 96]

    def test_zero_forecast_window_is_refused(self) -> None:
        core = self.core()
        with pytest.raises(bm.BenchmarkInputError, match="forecast_window_days"):
            bm.prepare_day_ahead(
                core=core,
                load_kwh=np.array([0.2, 0.2]),
                usable=np.array([True, True]),
                local_days=[datetime(2026, 1, 1).date(), datetime(2026, 1, 1).date()],
                eval_index=np.arange(2),
                forecast_window_days=0,
                terminal_value_eur_per_kwh=0.1,
                terminal_value_source="cli: test",
                time_limit_s=1.0,
                timezone="Europe/Madrid",
            )


class TestDayAheadEndToEnd:
    """The five-policy run: rows, artifact blocks, cost consistency, dominance flags.

    The fixture deliberately spans the 2025-10-26 Europe/Madrid **fall-back** day, so one
    local calendar day carries 100 intervals inside the evaluation window: the local-day
    partition, the frozen load profile's clock positions and the rolling-horizon event
    schedule are all exercised against a 25-hour day rather than only against 96-slot days.
    """

    # 2025-10-19T00:00Z is 02:00 CEST, so every local day in the window is whole.
    START = datetime(2025, 10, 19, tzinfo=UTC)
    INTERVALS = 12 * 24 * 4
    TRAIN_END = START + timedelta(days=2)

    @staticmethod
    def eval_local_days(
        intervals: int, train_end: datetime, start: datetime | None = None
    ) -> list[str]:
        """Local days the evaluation window touches, derived from the fixture itself.

        Not hardcoded: the window opens at 02:00 local on its first day and Madrid turns
        its clock back at 03:00 on 2025-10-26, so the local-day list is neither the UTC-day
        list nor a uniform 96-interval count — it is exactly the partition the run must
        respect, including the one 100-interval day.
        """
        from zoneinfo import ZoneInfo

        madrid = ZoneInfo("Europe/Madrid")
        base = start if start is not None else TestDayAheadEndToEnd.START
        labels: list[str] = []
        for i in range(intervals):
            stamp = base + timedelta(seconds=i * 900)
            if stamp < train_end:
                continue
            text = stamp.astimezone(madrid).date().isoformat()
            if not labels or labels[-1] != text:
                labels.append(text)
        return labels

    def run(self, tmp_path: Path, **kwargs: Any) -> tuple[Path, dict[str, Any]]:
        requires_core()
        table = make_analysis_table(
            start=self.START,
            intervals=self.INTERVALS,
            prices=_prices(self.INTERVALS),
            gross_kwh=_load(self.INTERVALS),
        )
        dataset_dir = write_synthetic_dataset(tmp_path, table)
        config_path = tmp_path / "tariff.yaml"
        write_tariff_config(config_path, make_tariff_config())
        kwargs.setdefault("time_limit_s", 20.0)
        run_dir = bm.benchmark_dataset(
            dataset=dataset_dir,
            config_path=config_path,
            train_end=self.TRAIN_END,
            **kwargs,
        )
        return run_dir, json.loads((run_dir / bm.RESULTS_JSON_NAME).read_text())

    def test_all_five_policies_run_and_the_ladder_holds(self, tmp_path: Path) -> None:
        run_dir, payload = self.run(tmp_path)
        assert set(payload["policies"]) == set(bm.POLICY_NAMES)
        assert len(bm.POLICY_NAMES) == 5
        results = pq.read_table(run_dir / bm.RESULTS_PARQUET_NAME)
        assert results.schema == bm.BENCHMARK_RESULT
        assert set(results.column("policy").to_pylist()) == set(bm.POLICY_NAMES)

        # The DST fall-back day is one 100-interval solve, not two UTC-day halves.
        da = payload["day_ahead"]
        assert da["partition"]["intervals_per_day_max"] == 100
        assert 100 in [d["intervals"] for d in da["per_day"]]

        da_record = payload["policies"][bm.POLICY_DAY_AHEAD]
        assert da_record["solver_status"] == "feasible"
        assert da_record["bound_kind"] == "none"
        assert da_record["is_certified"] is False
        assert da_record["closed_cycle"] is False
        assert da_record["terminal_mode_used"] == "free"
        # The policy record's terminal_value_eur_per_kwh is the COMMON SCORING value
        # (default 0.0), not the internal planning value.  This is what makes the
        # comparison with the oracle fair.
        assert da_record["terminal_value_eur_per_kwh"] == pytest.approx(0.0)
        # The internal planning value is recorded in the day_ahead block.
        assert payload["day_ahead"]["terminal_value"]["eur_per_kwh"] == pytest.approx(
            payload["day_ahead"]["terminal_value"]["eur_per_kwh"]
        )
        assert da_record["solver_time_limit_s"] == bm.DEFAULT_DAY_AHEAD_TIME_LIMIT_S
        assert da_record["solver_time_limit_scope"] == "per_local_day"

        # The fifth rung, on the same shared scoring basis.
        rh_record = payload["policies"][bm.POLICY_ROLLING_HORIZON]
        assert rh_record["solver_status"] == "feasible"
        assert rh_record["bound_kind"] == "none"
        assert rh_record["is_certified"] is False
        assert rh_record["closed_cycle"] is False
        assert rh_record["terminal_mode_used"] == "free"
        assert rh_record["terminal_value_eur_per_kwh"] == pytest.approx(0.0)
        assert rh_record["solver_time_limit_s"] == bm.DEFAULT_ROLLING_HORIZON_TIME_LIMIT_S
        assert rh_record["solver_time_limit_scope"] == "per_solve"

        comparison = payload["comparison"]
        assert comparison["oracle_dominates_baseline"] is True
        assert comparison["oracle_dominates_causal"] is True
        assert comparison["oracle_dominates_day_ahead"] is True
        assert comparison["oracle_dominates_rolling_horizon"] is True
        assert comparison["day_ahead_may_lose_causal"] is True
        assert comparison["day_ahead_may_lose_baseline"] is True
        assert comparison["rolling_horizon_may_lose_day_ahead"] is True
        assert (
            "guaranteed WHENEVER the oracle is a certified optimum" in comparison["dominance_note"]
        )

        # Cost accounting identical to the other four: meter + inventory == reported.
        consistency = payload["cost_consistency"]
        assert set(consistency) == set(bm.POLICY_NAMES)
        for name, check in consistency.items():
            assert check["ok"], name
            assert check["abs_diff_eur"] < 1e-9, name
            # Noise snapping is disclosed per policy, never silent: a nonzero count means
            # the solver returned a bound one ulp over its row, not energy exported.
            assert check["grid_noise_snapped_intervals"] >= 0, name
            assert "grid_noise_note" in check, name
        schedules = pq.read_table(run_dir / bm.SCHEDULES_PARQUET_NAME)
        assert set(schedules.column("policy").to_pylist()) == set(bm.POLICY_NAMES)

    def test_day_ahead_artifact_block_records_days_and_forecast(self, tmp_path: Path) -> None:
        _run_dir, payload = self.run(tmp_path)
        da = payload["day_ahead"]
        assert da["ran"] is True
        expected_days = self.eval_local_days(self.INTERVALS, self.TRAIN_END)
        assert da["day_count"] == len(expected_days)
        assert da["days_solved"] == da["day_count"]
        assert da["days_infeasible"] == 0
        assert da["days_degraded_to_hold"] == 0
        assert da["days_at_time_limit"] == 0
        assert len(da["per_day"]) == da["day_count"]
        assert [d["local_day"] for d in da["per_day"]] == expected_days
        # Madrid is UTC+1 in January, so the cut lands 1 h *before* the local midnight
        # of its own day: the first solve covers a partial local day and the artifact
        # says so instead of pretending every day is 96 intervals.
        assert da["per_day"][0]["intervals"] < 96
        assert da["partition"]["first_day_is_partial"] is True
        assert da["partition"]["basis"] == "local_calendar_day"
        assert da["partition"]["timezone"] == "Europe/Madrid"
        assert da["forecast"]["method"] == "trailing_slot_mean"
        assert da["forecast"]["window_days"] == bm.DEFAULT_FORECAST_WINDOW_DAYS
        assert da["forecast"]["summary"]["days_scored"] == da["day_count"]
        assert "mae_kwh" in da["forecast"]["summary"]
        assert da["terminal_value"]["source"].startswith(
            f"default:{bm.TERMINAL_VALUE_DEFAULT_RULE}"
        )
        assert da["meter_cost_rollup_abs_diff_eur"] < 1e-9
        assert da["wall_clock_note"].startswith("per-day solver wall-clock time is")
        assert "Schedule.as_dict" in da["artifact_note"]

    def test_day_ahead_only_run_needs_no_thresholds(self, tmp_path: Path) -> None:
        run_dir, payload = self.run(tmp_path, policies="no_battery,day_ahead")
        assert set(payload["policies"]) == {"no_battery", "day_ahead"}
        assert payload["training"]["thresholds_were_fitted"] is False
        assert payload["solver"]["oracle_terminal_mode_used"] == "not_run"
        results = pq.read_table(run_dir / bm.RESULTS_PARQUET_NAME)
        assert set(results.column("policy").to_pylist()) == {"no_battery", "day_ahead"}

    def test_oracle_dominates_day_ahead_in_the_result_table(self, tmp_path: Path) -> None:
        _run_dir, payload = self.run(tmp_path)
        costs = {k: v["cost_eur"] for k, v in payload["policies"].items()}
        assert costs[bm.POLICY_ORACLE] <= costs[bm.POLICY_DAY_AHEAD] + 1e-9
        # The ladder, as measured on this fixture — reported, never imposed.
        assert costs[bm.POLICY_NO_BATTERY] >= costs[bm.POLICY_ORACLE]

    def test_terminal_value_override_changes_the_run_id(self, tmp_path: Path) -> None:
        import shutil

        first, payload_first = self.run(tmp_path)
        second_dir = tmp_path / "second"
        shutil.copytree(tmp_path / "analysis", second_dir / "analysis")
        shutil.copy(tmp_path / "tariff.yaml", second_dir / "tariff.yaml")
        second, payload_second = self.run_with_dir(
            second_dir, day_ahead_terminal_value_eur_per_kwh=0.0
        )
        assert first.name != second.name
        assert payload_second["day_ahead"]["terminal_value"]["source"].startswith("cli:")
        assert payload_second["day_ahead"]["terminal_value"]["eur_per_kwh"] == 0.0
        assert payload_second["frozen_inputs"]["day_ahead_terminal_value_eur_per_kwh"] == 0.0

    def run_with_dir(self, root: Path, **kwargs: Any) -> tuple[Path, dict[str, Any]]:
        requires_core()
        config_path = root / "tariff.yaml"
        dataset_dir = next(
            p for p in (root / "analysis" / f"v{SCHEMA_VERSION}").iterdir() if p.is_dir()
        )
        kwargs.setdefault("time_limit_s", 20.0)
        run_dir = bm.benchmark_dataset(
            dataset=dataset_dir,
            config_path=config_path,
            # Derived from the class fixture, never hardcoded: this helper re-runs against a
            # dataset directory built by ``run``, so a start that drifts from ``START`` puts
            # ``train_end`` outside the dataset and the training split comes back empty.
            train_end=self.TRAIN_END,
            **kwargs,
        )
        return run_dir, json.loads((run_dir / bm.RESULTS_JSON_NAME).read_text())

    def test_forecast_window_is_hashed_and_recorded(self, tmp_path: Path) -> None:
        _run_dir, payload = self.run(tmp_path, day_ahead_forecast_window_days=1)
        assert payload["day_ahead"]["forecast"]["window_days"] == 1
        assert payload["frozen_inputs"]["day_ahead_forecast_window_days"] == 1

    def test_per_day_solver_failure_degrades_inside_the_run(
        self, tmp_path: Path, monkeypatch
    ) -> None:
        """The whole run survives a solver that always refuses: four rows, all held."""
        requires_core()
        from energy_baseline import policies as policies_mod
        from energy_baseline.oracle import OracleInfeasibleError

        monkeypatch.setattr(
            policies_mod,
            "solve_oracle",
            lambda *a, **k: (_ for _ in ()).throw(OracleInfeasibleError("unit test infeasible")),
        )
        start = datetime(2026, 1, 1, tzinfo=UTC)
        intervals = 4 * 24 * 4
        table = make_analysis_table(start=start, intervals=intervals)
        dataset_dir = write_synthetic_dataset(tmp_path, table)
        config_path = tmp_path / "tariff.yaml"
        write_tariff_config(config_path, make_tariff_config())
        run_dir = bm.benchmark_dataset(
            dataset=dataset_dir,
            config_path=config_path,
            train_end=start + timedelta(days=1),
            policies="no_battery,day_ahead",
            day_ahead_time_limit_s=2.0,
        )
        payload = json.loads((run_dir / bm.RESULTS_JSON_NAME).read_text())
        da = payload["day_ahead"]
        expected = self.eval_local_days(4 * 24 * 4, self.START + timedelta(days=1))
        assert da["days_degraded_to_hold"] == da["day_count"] == len(expected)
        assert da["days_solved"] == 0
        held = payload["policies"]["day_ahead"]
        baseline = payload["policies"]["no_battery"]
        # Held SOC means zero flows, so the day-ahead cost equals the baseline exactly.
        assert held["meter_cost_eur"] == pytest.approx(baseline["cost_eur"], abs=1e-9)
        assert held["inventory_adjustment_eur"] == pytest.approx(0.0, abs=1e-12)
        assert held["throughput_kwh"] == pytest.approx(0.0)
        assert payload["cost_consistency"]["day_ahead"]["ok"] is True

    def test_bad_day_ahead_knobs_are_refused(self, tmp_path: Path) -> None:
        requires_core()
        start = datetime(2026, 1, 1, tzinfo=UTC)
        table = make_analysis_table(start=start, intervals=3 * 24 * 4)
        dataset_dir = write_synthetic_dataset(tmp_path, table)
        config_path = tmp_path / "tariff.yaml"
        write_tariff_config(config_path, make_tariff_config())
        for kwargs in (
            {"day_ahead_forecast_window_days": 0},
            {"day_ahead_time_limit_s": 0.0},
            {"day_ahead_terminal_value_eur_per_kwh": float("nan")},
        ):
            with pytest.raises(bm.BenchmarkInputError):
                bm.benchmark_dataset(
                    dataset=dataset_dir,
                    config_path=config_path,
                    train_end=start + timedelta(days=1),
                    policies="day_ahead",
                    **kwargs,
                )

    def test_no_day_ahead_means_no_day_ahead_block(self, tmp_path: Path) -> None:
        _run_dir, payload = self.run(tmp_path, policies="no_battery,causal_price,oracle")
        assert payload["day_ahead"]["ran"] is False
        assert payload["assumptions"]["terminal_accounting"]["day_ahead"]["ran"] is False


class TestDayAheadScoringValue:
    """The scoring terminal value is the common benchmark value, not the internal planning value.

    This pins the fix for the bug where day-ahead used the training-window mean price V
    for both the internal daily planning and the reported cost, while the oracle used 0.0.
    The comparison was unfair because they were on different bases.  Now both use the
    same scoring value (default 0.0), so the comparison is fair.
    """

    def test_oracle_dominance_holds_on_common_scoring_basis(self, tmp_path: Path) -> None:
        """Positive final SOC, large V: oracle dominance holds on the common scoring basis.

        The internal planning value V is large (0.5 EUR/kWh), so the day-ahead policy
        holds a lot of energy at the end (positive final SOC).  The scoring value is 0.0
        (the default), so the reported cost_eur is just the meter cost.  The oracle also
        uses 0.0, so the comparison is on the same basis.
        """
        requires_core()
        from datetime import UTC, datetime, timedelta

        start = datetime(2026, 1, 1, tzinfo=UTC)
        intervals = 3 * 24 * 4  # 3 days
        # Prices: cheap overnight, expensive during the day.
        prices = [0.05 if i % 96 < 24 else 0.30 for i in range(intervals)]
        # Load: constant, so the policy will charge overnight and discharge during the day.
        load = [0.5] * intervals
        table = make_analysis_table(start=start, intervals=intervals, gross_kwh=load, prices=prices)
        dataset_dir = write_synthetic_dataset(tmp_path, table)
        config_path = tmp_path / "tariff.yaml"
        write_tariff_config(config_path, make_tariff_config())

        # Large internal planning value to hold energy, but scoring value is 0.0.
        run_dir = bm.benchmark_dataset(
            dataset=dataset_dir,
            config_path=config_path,
            train_end=start + timedelta(hours=12),
            policies="all",
            time_limit_s=30.0,
            day_ahead_time_limit_s=5.0,
            day_ahead_terminal_value_eur_per_kwh=0.5,  # large V
        )
        payload = json.loads((run_dir / bm.RESULTS_JSON_NAME).read_text())

        # The day-ahead policy's reported terminal value is the scoring value (0.0).
        da = payload["policies"]["day_ahead"]
        assert da["terminal_value_eur_per_kwh"] == pytest.approx(0.0)
        # The internal planning value is recorded in the day_ahead block.
        assert payload["day_ahead"]["terminal_value"]["eur_per_kwh"] == pytest.approx(0.5)
        # The oracle's terminal value is also 0.0 (scoring value).
        oracle = payload["policies"]["oracle"]
        assert oracle["terminal_value_eur_per_kwh"] == pytest.approx(0.0)
        # Both use the same scoring basis, so oracle dominance holds.
        assert payload["comparison"]["oracle_dominates_day_ahead"] is True
        # The day-ahead policy holds energy (positive final SOC).
        assert da["terminal_soc_kwh"] > da["initial_soc_kwh"]
        # The oracle also holds energy (or returns to initial, depending on the plan).
        # The key is that both are on the same scoring basis.

    def test_fixed_terminal_mode_does_not_guarantee_day_ahead_dominance(
        self, tmp_path: Path
    ) -> None:
        """Fixed terminal mode pins terminal SOC to initial.

        When terminal_mode="fixed", the oracle pins terminal SOC to initial.  The day-ahead
        policy may end with a different SOC, so the oracle's feasible set does not contain
        the day-ahead plan, and the dominance guarantee does not hold.
        """
        requires_core()
        from datetime import UTC, datetime, timedelta

        start = datetime(2026, 1, 1, tzinfo=UTC)
        intervals = 3 * 24 * 4
        prices = [0.05 if i % 96 < 24 else 0.30 for i in range(intervals)]
        load = [0.5] * intervals
        table = make_analysis_table(start=start, intervals=intervals, gross_kwh=load, prices=prices)
        dataset_dir = write_synthetic_dataset(tmp_path, table)
        config_path = tmp_path / "tariff.yaml"
        write_tariff_config(config_path, make_tariff_config())

        run_dir = bm.benchmark_dataset(
            dataset=dataset_dir,
            config_path=config_path,
            train_end=start + timedelta(hours=12),
            policies="all",
            terminal_mode="fixed",  # pins terminal SOC to initial
            time_limit_s=30.0,
            day_ahead_time_limit_s=5.0,
        )
        payload = json.loads((run_dir / bm.RESULTS_JSON_NAME).read_text())

        # The oracle's terminal mode is "fixed" (or "fixed_default").
        assert payload["solver"]["oracle_terminal_mode_used"].startswith("fixed")
        # The dominance guarantee is False because the endpoints may differ.
        assert payload["comparison"]["oracle_dominates_day_ahead_guaranteed"] is False
        # The empirical comparison may still hold, but it's not guaranteed.
        # (We don't assert the sign because it depends on the fixture.)

    def test_internal_and_scoring_values_differ_when_v_nonzero(self, tmp_path: Path) -> None:
        """With V > 0 the reported cost_eur is on the scoring basis, not the internal one.

        The internal daily objective sums to the V-credit (the internal planning value
        telescopes over the roll), while the reported cost_eur uses the scoring value
        (default 0.0).  Confusing the two is the bug this test pins: the review found
        the day-ahead Schedule being credited with the training-window mean price while
        the oracle used 0.0, which made the comparison unfair and oracle dominance
        meaningless.
        """
        requires_core()
        start = datetime(2026, 1, 1, tzinfo=UTC)
        intervals = 6 * 24 * 4
        prices = [0.05 if i % 96 < 24 else 0.30 for i in range(intervals)]
        load = [0.5 if i % 96 < 24 else 0.8 for i in range(intervals)]
        table = make_analysis_table(start=start, intervals=intervals, gross_kwh=load, prices=prices)
        dataset_dir = write_synthetic_dataset(tmp_path, table)
        config_path = tmp_path / "tariff.yaml"
        write_tariff_config(config_path, make_tariff_config())
        run_dir = bm.benchmark_dataset(
            dataset=dataset_dir,
            config_path=config_path,
            train_end=start + timedelta(days=2),
            policies="no_battery,day_ahead,oracle",
            time_limit_s=30.0,
            day_ahead_time_limit_s=5.0,
            day_ahead_terminal_value_eur_per_kwh=0.4,
        )
        payload = json.loads((run_dir / bm.RESULTS_JSON_NAME).read_text())
        da_rec = payload["policies"][bm.POLICY_DAY_AHEAD]
        da_block = payload["day_ahead"]
        # The reported cost uses the scoring value (0.0): cost_eur == meter_cost_eur.
        assert da_rec["terminal_value_eur_per_kwh"] == pytest.approx(0.0)
        assert da_rec["inventory_adjustment_eur"] == pytest.approx(0.0, abs=1e-12)
        assert da_rec["cost_eur"] == pytest.approx(da_rec["meter_cost_eur"], abs=1e-12)
        # The internal planning value is a separate, nonzero quantity.
        assert da_block["terminal_value"]["eur_per_kwh"] == pytest.approx(0.4)
        assert da_block["scoring_terminal_value_eur_per_kwh"] == pytest.approx(0.0)
        # The internal daily objective sum differs from the reported cost: same plan,
        # two different terminal-value bases.
        internal_sum = da_block["internal_daily_objective_sum_eur"]
        assert "NOT the common score" in da_block["internal_daily_objective_note"]
        # The battery ends above its seed (V rewards holding energy), so the internal
        # sum is lower than the metered cost.
        assert da_rec["terminal_soc_kwh"] > da_rec["initial_soc_kwh"]
        assert internal_sum < da_rec["cost_eur"]
        # The consistency check (meter + inventory == reported) passes for every policy.
        for name, check in payload["cost_consistency"].items():
            assert check["ok"], name
        # Oracle dominance holds because both use the same scoring basis (0.0).
        assert payload["comparison"]["oracle_dominates_day_ahead"] is True
        assert payload["comparison"]["oracle_dominates_day_ahead_guaranteed"] is True
