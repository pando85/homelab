"""Shared test fixtures — synthetic in-memory PyArrow tables."""

from __future__ import annotations

from datetime import UTC, date, datetime

import pyarrow as pa
import pytest

from energy_baseline.schemas import (
    ANALYSIS_INTERVAL,
    BENCHMARK_RESULT,
    RAW_ENUM_STATE,
    RAW_SAMPLE,
)


@pytest.fixture
def raw_sample_table() -> pa.Table:
    """Minimal valid RAW_SAMPLE table (1 row)."""
    return pa.table(
        {
            "entity": pa.array(["sensor.shellyem_main_channel_1_energy"]),
            "metric": pa.array(["energy_kwh"]),
            "unit": pa.array(["kWh"]),
            "series_key": pa.array(["abc123def456"]),
            "ts": pa.array(
                [datetime(2026, 1, 1, 0, 0, 0, tzinfo=UTC)],
                type=pa.timestamp("us", tz="UTC"),
            ),
            "value": pa.array([1234.5], type=pa.float64()),
            "scrape_instance": pa.array(["prometheus-long-term:9090"]),
            "extraction_id": pa.array(["00000000-0000-0000-0000-000000000001"]),
            "labels_json": pa.array(
                ['{"__name__":"node_energy_kwh","instance":"10.0.0.1:9100","job":"node"}']
            ),
        },
        schema=RAW_SAMPLE,
    )


@pytest.fixture
def raw_enum_state_table() -> pa.Table:
    """Minimal valid RAW_ENUM_STATE table (1 row)."""
    return pa.table(
        {
            "entity": pa.array(["sensor.washer_status"]),
            "state": pa.array(["running"]),
            "attributes_json": pa.array(['{"cycle": "cotton"}']),
            "last_changed": pa.array(
                [datetime(2026, 1, 1, 10, 0, 0, tzinfo=UTC)],
                type=pa.timestamp("us", tz="UTC"),
            ),
            "last_updated": pa.array(
                [datetime(2026, 1, 1, 10, 5, 0, tzinfo=UTC)],
                type=pa.timestamp("us", tz="UTC"),
            ),
            "extraction_id": pa.array(["00000000-0000-0000-0000-000000000002"]),
            "carry_in": pa.array([False]),
            "source_window_start": pa.array(
                [datetime(2025, 12, 31, 23, 0, 0, tzinfo=UTC)],
                type=pa.timestamp("us", tz="UTC"),
            ),
        },
        schema=RAW_ENUM_STATE,
    )


@pytest.fixture
def analysis_interval_table() -> pa.Table:
    """Minimal valid ANALYSIS_INTERVAL table (1 row)."""
    return pa.table(
        {
            "interval_start": pa.array(
                [datetime(2026, 1, 1, 0, 0, 0, tzinfo=UTC)],
                type=pa.timestamp("us", tz="UTC"),
            ),
            "interval_end": pa.array(
                [datetime(2026, 1, 1, 0, 15, 0, tzinfo=UTC)],
                type=pa.timestamp("us", tz="UTC"),
            ),
            "tz_local": pa.array(["Europe/Madrid"]),
            "tariff_period": pa.array([3], type=pa.int8()),
            "local_day": pa.array([date(2026, 1, 1)], type=pa.date32()),
            "dst_transition": pa.array([None], type=pa.string()),
            "raw_grid_kwh": pa.array([0.25], type=pa.float64()),
            "gross_kwh": pa.array([0.25], type=pa.float64()),
            "battery_absent": pa.array([True]),
            "energy_quality": pa.array(["measured"]),
            "allocation_quality": pa.array(["exact"]),
            "sample_count": pa.array([4], type=pa.int32()),
            "gap_seconds": pa.array([0.0], type=pa.float64()),
            "price_eur_per_kwh": pa.array([0.15], type=pa.float64()),
            "price_quality": pa.array(["measured"]),
            "dataset_id": pa.array(["dataset-abc123"]),
        },
        schema=ANALYSIS_INTERVAL,
    )


@pytest.fixture
def benchmark_result_table() -> pa.Table:
    """Minimal valid BENCHMARK_RESULT table (1 row)."""
    return pa.table(
        {
            "policy": pa.array(["no_battery"]),
            "dataset_id": pa.array(["dataset-abc123"]),
            "horizon_start": pa.array(
                [datetime(2026, 1, 1, 0, 0, 0, tzinfo=UTC)],
                type=pa.timestamp("us", tz="UTC"),
            ),
            "horizon_end": pa.array(
                [datetime(2026, 1, 2, 0, 0, 0, tzinfo=UTC)],
                type=pa.timestamp("us", tz="UTC"),
            ),
            "cost_eur": pa.array([3.50], type=pa.float64()),
            "cost_basis": pa.array(["untaxed"]),
            "efc": pa.array([0.0], type=pa.float64()),
            "throughput_kwh": pa.array([0.0], type=pa.float64()),
            "soc_min": pa.array([0.0], type=pa.float64()),
            "soc_max": pa.array([0.0], type=pa.float64()),
            "solver_gap": pa.array([None], type=pa.float64()),
            "solver_status": pa.array(["not_applicable"]),
            "bound_kind": pa.array(["none"]),
            "is_certified": pa.array([False]),
        },
        schema=BENCHMARK_RESULT,
    )
