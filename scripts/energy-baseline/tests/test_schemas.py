"""Tests for schemas.py — field presence, dtype validation, UTC timestamps."""

from __future__ import annotations

from datetime import UTC, datetime

import pyarrow as pa
import pytest

from energy_baseline.schemas import (
    ANALYSIS_INTERVAL,
    BENCHMARK_RESULT,
    RAW_ENUM_STATE,
    RAW_SAMPLE,
    SCHEMA_VERSION,
    SCHEMAS,
    validate_table,
)


class TestSchemaVersion:
    def test_version_is_int(self) -> None:
        assert isinstance(SCHEMA_VERSION, int)
        assert SCHEMA_VERSION >= 1


class TestSchemaRegistry:
    def test_all_schemas_registered(self) -> None:
        expected = {"RAW_SAMPLE", "RAW_ENUM_STATE", "ANALYSIS_INTERVAL", "BENCHMARK_RESULT"}
        assert set(SCHEMAS.keys()) == expected


class TestRequiredFields:
    """Each schema must contain its documented fields."""

    def test_raw_sample_fields(self) -> None:
        names = {f.name for f in RAW_SAMPLE}
        for required in [
            "entity",
            "metric",
            "unit",
            "series_key",
            "ts",
            "value",
            "scrape_instance",
            "extraction_id",
            "labels_json",
        ]:
            assert required in names, f"RAW_SAMPLE missing field {required}"

    def test_raw_sample_labels_json_not_nullable(self) -> None:
        """labels_json must be non-nullable for FULL_LABELS diagnostics."""
        field = RAW_SAMPLE.field("labels_json")
        assert field.nullable is False
        assert field.type == pa.string()

    def test_raw_enum_state_fields(self) -> None:
        names = {f.name for f in RAW_ENUM_STATE}
        for required in [
            "entity",
            "state",
            "attributes_json",
            "last_changed",
            "last_updated",
            "extraction_id",
            "carry_in",
            "source_window_start",
        ]:
            assert required in names, f"RAW_ENUM_STATE missing field {required}"

    def test_analysis_interval_fields(self) -> None:
        names = {f.name for f in ANALYSIS_INTERVAL}
        for required in [
            "interval_start",
            "interval_end",
            "tz_local",
            "tariff_period",
            "local_day",
            "dst_transition",
            "raw_grid_kwh",
            "gross_kwh",
            "battery_absent",
            "energy_quality",
            "allocation_quality",
            "sample_count",
            "gap_seconds",
            "price_eur_per_kwh",
            "price_quality",
            "dataset_id",
        ]:
            assert required in names, f"ANALYSIS_INTERVAL missing field {required}"

    def test_benchmark_result_fields(self) -> None:
        names = {f.name for f in BENCHMARK_RESULT}
        for required in [
            "policy",
            "dataset_id",
            "horizon_start",
            "horizon_end",
            "cost_eur",
            "cost_basis",
            "efc",
            "throughput_kwh",
            "soc_min",
            "soc_max",
            "solver_gap",
            "solver_status",
            "bound_kind",
            "is_certified",
        ]:
            assert required in names, f"BENCHMARK_RESULT missing field {required}"


class TestTimestampsAreUtc:
    """All timestamp fields must be tz-aware UTC."""

    @pytest.mark.parametrize(
        "schema",
        [RAW_SAMPLE, RAW_ENUM_STATE, ANALYSIS_INTERVAL, BENCHMARK_RESULT],
        ids=["RAW_SAMPLE", "RAW_ENUM_STATE", "ANALYSIS_INTERVAL", "BENCHMARK_RESULT"],
    )
    def test_timestamp_fields_utc(self, schema: pa.Schema) -> None:
        for field in schema:
            if isinstance(field.type, pa.TimestampType):
                assert field.type.tz == "UTC", (
                    f"{field.name} in {schema} has tz={field.type.tz!r}, expected 'UTC'"
                )


class TestValidateTable:
    def test_valid_table_passes(self, raw_sample_table: pa.Table) -> None:
        validate_table("RAW_SAMPLE", raw_sample_table)  # should not raise

    def test_unknown_schema_raises(self, raw_sample_table: pa.Table) -> None:
        with pytest.raises(ValueError, match="unknown schema"):
            validate_table("NONEXISTENT", raw_sample_table)

    def test_wrong_dtype_raises(self) -> None:
        """A table with wrong field types must be rejected."""
        wrong = pa.table(
            {
                "entity": pa.array([123], type=pa.int64()),  # should be string
                "metric": pa.array(["m"]),
                "unit": pa.array(["u"]),
                "series_key": pa.array(["sk"]),
                "ts": pa.array(
                    [datetime(2026, 1, 1, tzinfo=UTC)],
                    type=pa.timestamp("us", tz="UTC"),
                ),
                "value": pa.array([1.0], type=pa.float64()),
                "scrape_instance": pa.array(["si"]),
                "extraction_id": pa.array(["eid"]),
                "labels_json": pa.array(['{"a":"b"}']),
            }
        )
        with pytest.raises(TypeError, match="schema mismatch"):
            validate_table("RAW_SAMPLE", wrong)

    def test_missing_field_raises(self) -> None:
        """A table missing a required field must be rejected."""
        incomplete = pa.table(
            {
                "entity": pa.array(["e"]),
                "metric": pa.array(["m"]),
            }
        )
        with pytest.raises(TypeError, match="schema mismatch"):
            validate_table("RAW_SAMPLE", incomplete)

    def test_null_in_non_nullable_field_raises(self) -> None:
        """A null in a non-nullable field must be caught by validate_table."""
        table = pa.table(
            {
                "entity": pa.array([None], type=pa.string()),  # nullable=False!
                "metric": pa.array(["m"]),
                "unit": pa.array(["u"]),
                "series_key": pa.array(["sk"]),
                "ts": pa.array(
                    [datetime(2026, 1, 1, tzinfo=UTC)],
                    type=pa.timestamp("us", tz="UTC"),
                ),
                "value": pa.array([1.0], type=pa.float64()),
                "scrape_instance": pa.array(["si"]),
                "extraction_id": pa.array(["eid"]),
                "labels_json": pa.array(["{}"]),
            },
            schema=RAW_SAMPLE,
        )
        with pytest.raises(ValueError, match="null violation"):
            validate_table("RAW_SAMPLE", table)

    def test_null_in_nullable_field_passes(self) -> None:
        """Null in a nullable field (scrape_instance) should pass validation."""
        table = pa.table(
            {
                "entity": pa.array(["e"]),
                "metric": pa.array(["m"]),
                "unit": pa.array(["u"]),
                "series_key": pa.array(["sk"]),
                "ts": pa.array(
                    [datetime(2026, 1, 1, tzinfo=UTC)],
                    type=pa.timestamp("us", tz="UTC"),
                ),
                "value": pa.array([1.0], type=pa.float64()),
                "scrape_instance": pa.array([None], type=pa.string()),  # nullable=True
                "extraction_id": pa.array(["eid"]),
                "labels_json": pa.array(["{}"]),
            },
            schema=RAW_SAMPLE,
        )
        validate_table("RAW_SAMPLE", table)  # should not raise

    def test_labels_json_null_raises(self) -> None:
        """labels_json is non-nullable; null must be rejected."""
        table = pa.table(
            {
                "entity": pa.array(["e"]),
                "metric": pa.array(["m"]),
                "unit": pa.array(["u"]),
                "series_key": pa.array(["sk"]),
                "ts": pa.array(
                    [datetime(2026, 1, 1, tzinfo=UTC)],
                    type=pa.timestamp("us", tz="UTC"),
                ),
                "value": pa.array([1.0], type=pa.float64()),
                "scrape_instance": pa.array(["si"]),
                "extraction_id": pa.array(["eid"]),
                "labels_json": pa.array([None], type=pa.string()),  # nullable=False!
            },
            schema=RAW_SAMPLE,
        )
        with pytest.raises(ValueError, match="null violation.*labels_json"):
            validate_table("RAW_SAMPLE", table)
