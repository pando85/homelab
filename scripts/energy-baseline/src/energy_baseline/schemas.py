"""PyArrow schemas for the energy-baseline canonical data store.

All timestamps are ``timestamp("us", tz="UTC")``.

Series identity (RAW_SAMPLE.series_key)
---------------------------------------
``series_key`` is a stable hash of the Prometheus label set **excluding only
the volatile scrape-target labels**: ``instance`` and ``pod_ip``.  ``instance``
carries the pod IP (verified: 238 distinct values for one entity over ~3 years,
churning on every pod restart/reschedule), so including it would fragment one
logical series into hundreds.

All other labels — including ``pod``, ``job``, ``service``, ``namespace``,
``endpoint``, ``domain`` and ``friendly_name`` — are **retained** in the
identity.  This is deliberate: those labels are stable and mark genuine
deployment eras.  Verified against the live archive, the Home Assistant
ServiceMonitor was renamed on 2026-05-12 (``job=home-assistant-hass`` →
``job=home-assistant``), and the two eras have **no overlapping timestamps**.
Keeping them distinct preserves that boundary instead of silently merging two
scrape configurations.

The full original label set (volatile labels included) is retained in the
``labels_json`` column for diagnostics and for later era stitching.

Consequence for consumers: two different ``series_key`` values may still
represent the **same physical counter** across an era boundary.  The underlying
counter was verified continuous across the 2026-05-12 rename
(14612.9732 → 14612.9852 kWh), so era stitching must concatenate series in
time order — it must NOT sum them as independent counters.

Allowed categorical values
--------------------------
``energy_quality`` (ANALYSIS_INTERVAL):
    - ``measured``           — actual counter delta
    - ``gap_partial``        — partial gap in counter data within interval
    - ``gap_total_unknown``  — total gap; energy for span is unknown
    - ``counter_reset_bridged`` — counter reset detected and bridged

``allocation_quality`` (ANALYSIS_INTERVAL):
    - ``exact``       — energy allocated exactly to 15-min intervals
    - ``unknown_gap`` — gap in allocation; unknown where energy falls

``price_quality`` (ANALYSIS_INTERVAL):
    - ``measured``      — actual price from PVPC sensor
    - ``forward_filled`` — price forward-filled from last known value
    - ``missing``       — no price available

``dst_transition`` (ANALYSIS_INTERVAL):
    - null / empty  — normal interval
    - ``spring_forward`` — spring-forward DST (23-hour day)
    - ``fall_back``     — fall-back DST (25-hour day)

``cost_basis`` (BENCHMARK_RESULT):
    - ``untaxed``  — pure energy cost without tax multiplier
    - ``taxed``    — cost with confirmed tax multiplier applied

``bound_kind`` (BENCHMARK_RESULT):
    - ``none``       — no bound computed (e.g. no-battery policy)
    - ``upper``      — feasible solution provides upper bound on optimal cost
    - ``lower``      — dual provides lower bound on optimal cost
    - ``certified``  — gap within tolerance; result is certified optimum

``solver_status`` (BENCHMARK_RESULT):
    - ``not_applicable`` — policy does not use a solver
    - ``optimal``        — solver converged to optimal
    - ``feasible``       — feasible solution found, gap exceeds tolerance
    - ``infeasible``     — problem is infeasible
    - ``time_limit``     — solver hit time limit
"""

from __future__ import annotations

import pyarrow as pa

SCHEMA_VERSION = 1

# ---------------------------------------------------------------------------
# Raw schemas
# ---------------------------------------------------------------------------

RAW_SAMPLE = pa.schema(
    [
        pa.field("entity", pa.string(), nullable=False),
        pa.field("metric", pa.string(), nullable=False),
        pa.field("unit", pa.string(), nullable=False),
        pa.field("series_key", pa.string(), nullable=False),
        pa.field("ts", pa.timestamp("us", tz="UTC"), nullable=False),
        pa.field("value", pa.float64(), nullable=False),
        pa.field("scrape_instance", pa.string(), nullable=True),
        pa.field("extraction_id", pa.string(), nullable=False),
        pa.field(
            "labels_json",
            pa.string(),
            nullable=False,
            metadata={"purpose": "full_label_set_diagnostics"},
        ),
    ]
)

RAW_ENUM_STATE = pa.schema(
    [
        pa.field("entity", pa.string(), nullable=False),
        pa.field("state", pa.string(), nullable=False),
        pa.field("attributes_json", pa.string(), nullable=True),
        pa.field("last_changed", pa.timestamp("us", tz="UTC"), nullable=False),
        pa.field("last_updated", pa.timestamp("us", tz="UTC"), nullable=False),
        pa.field("extraction_id", pa.string(), nullable=False),
        pa.field("carry_in", pa.bool_(), nullable=False),
        pa.field("source_window_start", pa.timestamp("us", tz="UTC"), nullable=False),
    ]
)

# ---------------------------------------------------------------------------
# Analysis schema (15-min grid)
# ---------------------------------------------------------------------------

ANALYSIS_INTERVAL = pa.schema(
    [
        pa.field("interval_start", pa.timestamp("us", tz="UTC"), nullable=False),
        pa.field("interval_end", pa.timestamp("us", tz="UTC"), nullable=False),
        pa.field("tz_local", pa.string(), nullable=False),
        pa.field("tariff_period", pa.int8(), nullable=False),
        pa.field("local_day", pa.date32(), nullable=False),
        pa.field("dst_transition", pa.string(), nullable=True),
        pa.field("raw_grid_kwh", pa.float64(), nullable=True),
        pa.field("gross_kwh", pa.float64(), nullable=True),
        pa.field("battery_absent", pa.bool_(), nullable=False),
        pa.field("energy_quality", pa.string(), nullable=False),
        pa.field("allocation_quality", pa.string(), nullable=False),
        pa.field("sample_count", pa.int32(), nullable=False),
        pa.field("gap_seconds", pa.float64(), nullable=False),
        pa.field("price_eur_per_kwh", pa.float64(), nullable=True),
        pa.field("price_quality", pa.string(), nullable=False),
        pa.field("dataset_id", pa.string(), nullable=False),
    ]
)

# ---------------------------------------------------------------------------
# Benchmark schema
# ---------------------------------------------------------------------------

BENCHMARK_RESULT = pa.schema(
    [
        pa.field("policy", pa.string(), nullable=False),
        pa.field("dataset_id", pa.string(), nullable=False),
        pa.field("horizon_start", pa.timestamp("us", tz="UTC"), nullable=False),
        pa.field("horizon_end", pa.timestamp("us", tz="UTC"), nullable=False),
        pa.field("cost_eur", pa.float64(), nullable=False),
        pa.field("cost_basis", pa.string(), nullable=False),
        pa.field("efc", pa.float64(), nullable=False),
        pa.field("throughput_kwh", pa.float64(), nullable=False),
        pa.field("soc_min", pa.float64(), nullable=False),
        pa.field("soc_max", pa.float64(), nullable=False),
        pa.field("solver_gap", pa.float64(), nullable=True),
        pa.field("solver_status", pa.string(), nullable=False),
        pa.field("bound_kind", pa.string(), nullable=False),
        pa.field("is_certified", pa.bool_(), nullable=False),
    ]
)

# ---------------------------------------------------------------------------
# Registry
# ---------------------------------------------------------------------------

SCHEMAS: dict[str, pa.Schema] = {
    "RAW_SAMPLE": RAW_SAMPLE,
    "RAW_ENUM_STATE": RAW_ENUM_STATE,
    "ANALYSIS_INTERVAL": ANALYSIS_INTERVAL,
    "BENCHMARK_RESULT": BENCHMARK_RESULT,
}


def validate_table(name: str, table: pa.Table) -> None:
    """Raise if *table* does not match the named schema.

    Checks:
    * Schema equality (field names, types, nullability metadata).
    * Null invariant: columns declared ``nullable=False`` must contain
      no null values.

    Raises:
        ValueError: If *name* is not a registered schema.
        TypeError: If the table schema does not match.
        ValueError: If a non-nullable column contains nulls.
    """
    if name not in SCHEMAS:
        raise ValueError(f"unknown schema name: {name!r}")
    expected = SCHEMAS[name]
    if table.schema != expected:
        raise TypeError(
            f"schema mismatch for {name!r}:\n  expected: {expected}\n  got:      {table.schema}"
        )

    # Null invariant check: non-nullable fields must have no nulls
    for field_def in expected:
        if not field_def.nullable:
            col = table.column(field_def.name)
            null_count = col.null_count
            if null_count > 0:
                raise ValueError(
                    f"null violation in {name!r}.{field_def.name}: "
                    f"field is nullable=False but contains {null_count} null(s)"
                )
