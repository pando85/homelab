"""Tests for ``energy_baseline.build`` — the raw-archive → 15-minute grid builder.

Everything here is synthetic: no network, no cluster, and Parquet is written only
under ``pytest``'s ``tmp_path``.  The behaviours pinned by this file are the
normative ones from ``docs/adr/0003-phase1-measurement-and-baseline.md`` (data
contract + Prometheus data characteristics) and ``schemas.py`` (the output
contract and its allowed categorical values), plus the properties of the real
archive that the builder must satisfy:

* ``series_key`` keeps ``job``, so the 2026-05-12 Home Assistant ServiceMonitor
  rename mints **two** keys for **one** physical counter — eras must be stitched
  by concatenating in timestamp order, never summed as independent counters.
* the counter is continuous across that rename, so a delta spanning the boundary
  is legitimate rather than a reset;
* the true cadence is ~1/min at irregular sub-second offsets, so nothing may
  assume aligned timestamps;
* ``sensor.esios_pvpc_2`` is a dead duplicate that must never merge into the
  price series.
"""

from __future__ import annotations

import copy
import hashlib
import inspect
import json
import re
import time
from collections import Counter
from datetime import UTC, date, datetime, timedelta
from pathlib import Path
from typing import Any
from zoneinfo import ZoneInfo

import pyarrow as pa
import pyarrow.parquet as pq
import pytest

from energy_baseline import build as build_mod
from energy_baseline import schemas
from energy_baseline.build import (
    ALLOCATION_ESTIMATED_BOUNDARY,
    ALLOCATION_EXACT,
    ALLOCATION_UNKNOWN_GAP,
    ANALYSIS_INTERVAL,
    BATTERY_TELEMETRY_ENTITIES,
    COUNTER_QUANTISATION_KWH,
    ENERGY_COUNTER_RESET_BRIDGED,
    ENERGY_GAP_PARTIAL,
    ENERGY_GAP_TOTAL_UNKNOWN,
    ENERGY_MEASURED,
    GRID_COUNTER_ENTITIES,
    INTERVAL_SECONDS,
    NESTED_SUBMETER_ENTITIES,
    PRICE_AMBIGUOUS,
    PRICE_ENTITIES,
    PRICE_FORWARD_FILLED,
    PRICE_MEASURED,
    PRICE_MISSING,
    PRICE_PERIOD_SECONDS,
    BuildError,
    ConflictingSampleError,
    DeltaSpan,
    GridSeriesMissingError,
    IntervalEnergy,
    OverlappingSeriesError,
    RawPoint,
    StitchedSeries,
    align_price_hours,
    align_prices,
    allocate_energy,
    build_dataset,
    build_interval_table,
    code_version,
    compute_dataset_id,
    counter_deltas,
    detect_battery_active,
    dst_transition_for,
    load_raw_inputs,
    local_day_length_seconds,
    make_utc_grid,
    resolve_gross_load,
    stitch_series,
)
from energy_baseline.config import PeriodRule, TariffPeriods, load_config, tariff_period_for
from energy_baseline.extract_prom import (
    DEAD_DUPLICATE_ENTITY_IDS,
    VOLATILE_IDENTITY_LABELS,
    canonical_labels_json,
    compute_series_key,
)
from energy_baseline.paths import parts_dir
from energy_baseline.schemas import RAW_SAMPLE, SCHEMA_VERSION, validate_table

# ---------------------------------------------------------------------------
# Entities, label sets and row helpers (shaped like the real archive)
# ---------------------------------------------------------------------------

GRID = "sensor.shellyem_main_channel_1_energy"
RETURNED = "sensor.shellyem_main_channel_1_energy_returned"
POWER = "sensor.shellyem_main_channel_1_power"
CH2 = "sensor.shellyem_main_channel_2_energy"
PRICE = "sensor.esios_pvpc"
DEAD_PRICE = "sensor.esios_pvpc_2"

JOB_OLD = "home-assistant-hass"  # ServiceMonitor name before 2026-05-12
JOB_NEW = "home-assistant"

_T0 = datetime(2026, 1, 5, tzinfo=UTC)
MADRID = ZoneInfo("Europe/Madrid")


def _labels(
    entity: str, *, job: str = JOB_NEW, instance: str = "10.42.0.7:8080", **extra: str
) -> dict[str, str]:
    labels = {
        "__name__": "hass_sensor_value_gauge",
        "entity": entity,
        "instance": instance,
        "job": job,
        "namespace": "home-operations",
        "pod": f"home-assistant-{job}",
        "service": "home-assistant",
    }
    labels.update(extra)
    return labels


def raw_row(
    entity: str,
    metric: str,
    unit: str,
    ts: datetime,
    value: float,
    *,
    job: str = JOB_NEW,
    instance: str = "10.42.0.7:8080",
    extraction_id: str = "11111111-1111-1111-1111-111111111111",
    **extra: str,
) -> dict[str, Any]:
    """One ``RAW_SAMPLE`` row, with ``series_key`` derived exactly as the extractor does."""
    labels = _labels(entity, job=job, instance=instance, **extra)
    return {
        "entity": entity,
        "metric": metric,
        "unit": unit,
        "series_key": compute_series_key(labels),
        "ts": ts,
        "value": float(value),
        "scrape_instance": instance,
        "extraction_id": extraction_id,
        "labels_json": canonical_labels_json(labels),
    }


def grid_row(ts: datetime, value: float, **kwargs: Any) -> dict[str, Any]:
    return raw_row(GRID, "energy_kwh", "kWh", ts, value, **kwargs)


def price_row(ts: datetime, value: float, **kwargs: Any) -> dict[str, Any]:
    return raw_row(PRICE, "price_eur_per_kwh", "EUR/kWh", ts, value, **kwargs)


def ch2_row(ts: datetime, value: float, **kwargs: Any) -> dict[str, Any]:
    return raw_row(CH2, "energy_kwh", "kWh", ts, value, **kwargs)


def power_row(ts: datetime, value: float, **kwargs: Any) -> dict[str, Any]:
    return raw_row(POWER, "power_w", "W", ts, value, **kwargs)


def returned_row(ts: datetime, value: float, **kwargs: Any) -> dict[str, Any]:
    return raw_row(RETURNED, "energy_kwh", "kWh", ts, value, **kwargs)


def to_raw_table(rows: list[dict[str, Any]]) -> pa.Table:
    """Materialise rows as an in-memory table matching ``RAW_SAMPLE``."""
    return pa.table(
        {
            "entity": pa.array([r["entity"] for r in rows], pa.string()),
            "metric": pa.array([r["metric"] for r in rows], pa.string()),
            "unit": pa.array([r["unit"] for r in rows], pa.string()),
            "series_key": pa.array([r["series_key"] for r in rows], pa.string()),
            "ts": pa.array([r["ts"] for r in rows], pa.timestamp("us", tz="UTC")),
            "value": pa.array([r["value"] for r in rows], pa.float64()),
            "scrape_instance": pa.array([r["scrape_instance"] for r in rows], pa.string()),
            "extraction_id": pa.array([r["extraction_id"] for r in rows], pa.string()),
            "labels_json": pa.array([r["labels_json"] for r in rows], pa.string()),
        },
        schema=RAW_SAMPLE,
    )


def _iso(dt: datetime) -> str:
    """ISO-8601 UTC with ``Z`` for manifest window fields (no ``build`` import needed)."""
    return dt.astimezone(UTC).strftime("%Y-%m-%dT%H:%M:%S") + "Z"


def _write_manifest_for_part(
    data_dir: Path,
    *,
    name: str,
    sha256: str,
    rows: int,
    chunk_start: datetime,
    chunk_end: datetime,
    entities: list[str],
    source: str = "prometheus",
    extraction_id: str | None = None,
    finished_at: str | None = "2026-01-05T02:00:00Z",
    path_override: str | None = None,
) -> str:
    """Record a synthetic **success manifest** referencing the part just written.

    :func:`load_raw_inputs` resolves coverage from ``manifests/*.json`` rather than
    globbing the part directory, so every test input needs a manifest that names its
    part with the full SHA-256, the row count and the chunk window.  The extraction
    id is derived from the content hash so identical synthetic inputs replay
    identically (determinism tests rely on that).  Pass ``source="ha_history"`` or a
    ``finished_at=None`` to build a manifest that must NOT be treated as raw input,
    or a ``path_override`` to aim the reference outside the data dir (sandbox test).
    """
    eid = extraction_id or f"ex-{sha256[:12]}"
    rel_path = path_override if path_override is not None else f"raw/prometheus/v1/parts/{name}"
    manifest = {
        "manifest_version": 1,
        "extraction_id": eid,
        "started_at": _iso(chunk_start),
        "finished_at": finished_at,
        "source": source,
        "code_version": "c" * 40,
        "code_dirty": False,
        "config_hash": "a" * 64,
        "schema_version": 1,
        "params": {},
        "entities": sorted(entities),
        "source_window_start": _iso(chunk_start),
        "source_window_end": _iso(chunk_end),
        "row_count": rows,
        "part_files": [
            {
                "path": rel_path,
                "name": name,
                "kind": "prom-samples",
                "sha256": sha256,
                "rows": rows,
                "chunk_start": _iso(chunk_start),
                "chunk_end": _iso(chunk_end),
                "entities": sorted(entities),
            }
        ],
        "coverage_notes": "synthetic test manifest",
        "quality_flags": [],
    }
    manifests_dir = data_dir / "manifests"
    manifests_dir.mkdir(parents=True, exist_ok=True)
    (manifests_dir / f"{eid}.json").write_text(json.dumps(manifest, sort_keys=True))
    return eid


def _publish_prom_part(
    data_dir: Path,
    table: pa.Table,
    name: str,
    *,
    entities: list[str],
    chunk: tuple[datetime, datetime] | None = None,
    source: str = "prometheus",
    finished_at: str | None = "2026-01-05T02:00:00Z",
    sha_override: str | None = None,
    rows_override: int | None = None,
    path_override: str | None = None,
    extraction_id: str | None = None,
) -> Path:
    """Write one Parquet part and the manifest that references it.

    This is the single fixture entry point the builder reads through: a part with no
    manifest is an orphan from a failed extraction and must be ignored, so tests can
    exercise that by calling ``_publish_prom_part`` and then deleting or not writing
    the manifest, or by writing one that is referenced-but-corrupt.
    """
    path = parts_dir(data_dir, "prometheus") / name
    path.parent.mkdir(parents=True, exist_ok=True)
    pq.write_table(table, path)
    sha = sha_override or hashlib.sha256(path.read_bytes()).hexdigest()
    rows = table.num_rows if rows_override is None else rows_override
    start, end = chunk if chunk is not None else (_T0 - timedelta(days=1), _T0 + timedelta(days=2))
    _write_manifest_for_part(
        data_dir,
        name=name,
        sha256=sha,
        rows=rows,
        chunk_start=start,
        chunk_end=end,
        entities=entities,
        source=source,
        finished_at=finished_at,
        path_override=path_override,
        extraction_id=extraction_id,
    )
    return path


def write_raw_part(
    data_dir: Path, rows: list[dict[str, Any]], name: str = "samples-a.parquet"
) -> Path:
    """Write one raw part **and its success manifest** where ``load_raw_inputs`` reads.

    The builder resolves coverage from ``manifests/*.json``, so writing only the
    Parquet file is no longer enough: this emits a valid synthetic manifest naming
    the part with its full SHA-256, row count and the chunk window spanning the
    rows' real timestamps (never a fabricated "current" end).
    """
    if rows:
        stamps = [r["ts"] for r in rows]
        chunk = (min(stamps), max(stamps) + timedelta(microseconds=1))
        entities = sorted({r["entity"] for r in rows})
    else:
        chunk = (_T0 - timedelta(days=1), _T0 + timedelta(days=2))
        entities = [GRID]
    return _publish_prom_part(data_dir, to_raw_table(rows), name, entities=entities, chunk=chunk)


#: The live archive samples at ~1/min but never on a round second: the first
#: verified sample of a day looks like ``00:00:39.219Z``.  Generated data uses the
#: same shape so nothing in the builder can lean on aligned timestamps.  The
#: offsets stay within a second of the nominal cadence, as real scrapes do;
#: :class:`TestGapThresholdSensitivity` covers a jitter-heavy cadence separately.
IRREGULAR_OFFSETS: tuple[float, ...] = (39.219, 39.875, 40.125, 38.90625)


def counter_times(
    start: datetime,
    count: int,
    step_seconds: float = 60.0,
    *,
    offset_pattern: tuple[float, ...] | None = IRREGULAR_OFFSETS,
) -> list[datetime]:
    """Sample times ``step_seconds`` apart with irregular (un-aligned) offsets."""
    if not offset_pattern:
        return [start + timedelta(seconds=step_seconds * i) for i in range(count)]
    return [
        start + timedelta(seconds=step_seconds * i + offset_pattern[i % len(offset_pattern)])
        for i in range(count)
    ]


def aligned_times(start: datetime, count: int, step_seconds: float = 60.0) -> list[datetime]:
    """Grid-aligned sample times — an edge case, not the normal archive shape."""
    return counter_times(start, count, step_seconds, offset_pattern=None)


def counter_rows(
    start: datetime,
    count: int,
    *,
    first: float = 14612.0,
    per_sample: float = 0.004,
    step_seconds: float = 60.0,
    job: str = JOB_NEW,
    times: list[datetime] | None = None,
    row_factory=grid_row,
    **kwargs: Any,
) -> list[dict[str, Any]]:
    """A strictly monotonic energy counter (as verified over 72,946 real samples)."""
    stamps = times if times is not None else counter_times(start, count, step_seconds)
    assert len(stamps) == count
    return [row_factory(stamps[i], first + per_sample * i, job=job, **kwargs) for i in range(count)]


def price_rows(
    start: datetime,
    hours: dict[int, float],
    *,
    per_minute: int = 6,
    step_seconds: float = 10.0,
    job: str = JOB_NEW,
) -> list[dict[str, Any]]:
    """Hourly-priced step function re-published every ``step_seconds`` within the hour."""
    rows: list[dict[str, Any]] = []
    for hour, value in hours.items():
        hour_start = start + timedelta(hours=hour)
        for i in range(per_minute):
            rows.append(price_row(hour_start + timedelta(seconds=step_seconds * i), value, job=job))
    return rows


def points_from_rows(rows: list[dict[str, Any]]) -> list[RawPoint]:
    return [RawPoint(ts=r["ts"], value=r["value"], series_key=r["series_key"]) for r in rows]


def timeline(
    rows: list[dict[str, Any]], *, entity: str = GRID, metric: str = "energy_kwh", unit: str = "kWh"
) -> StitchedSeries:
    return stitch_series(entity=entity, metric=metric, unit=unit, points=points_from_rows(rows))


def grid_times(start: datetime, end: datetime) -> list[datetime]:
    return make_utc_grid(start, end, interval_seconds=INTERVAL_SECONDS)


def values_for(table: pa.Table, column: str) -> list[Any]:
    return table.column(column).to_pylist()


def rows_as_dicts(table: pa.Table) -> list[dict[str, Any]]:
    return table.to_pylist()


def row_at(table: pa.Table, start: datetime) -> dict[str, Any]:
    """The output row whose ``interval_start`` equals *start*."""
    for row in rows_as_dicts(table):
        if row["interval_start"] == start:
            return row
    raise AssertionError(f"no interval starting at {start!r}")


# ---------------------------------------------------------------------------
# Tariff configuration fixture helpers
# ---------------------------------------------------------------------------

WEEKDAY_RULES: list[dict[str, int]] = [
    {"period": 3, "start_hour": 0, "end_hour": 8},
    {"period": 2, "start_hour": 8, "end_hour": 10},
    {"period": 1, "start_hour": 10, "end_hour": 14},
    {"period": 2, "start_hour": 14, "end_hour": 18},
    {"period": 1, "start_hour": 18, "end_hour": 22},
    {"period": 2, "start_hour": 22, "end_hour": 24},
]
ALL_P3: list[dict[str, int]] = [{"period": 3, "start_hour": 0, "end_hour": 24}]


def tariff_document(
    *,
    timezone: str = "Europe/Madrid",
    weekday: list[dict[str, int]] | None = None,
    saturday: list[dict[str, int]] | None = None,
    sunday: list[dict[str, int]] | None = None,
    holidays: list[str] | None = None,
    holiday_day_type: str = "sunday",
) -> dict[str, Any]:
    return {
        "version": 1,
        "taxes": [
            {"valid_from": "2024-01-01", "iee_pct": None, "iva_pct": None, "source": "synthetic"}
        ],
        "tariff_periods": {
            "timezone": timezone,
            "rules": {
                "weekday": copy.deepcopy(WEEKDAY_RULES if weekday is None else weekday),
                "saturday": copy.deepcopy(ALL_P3 if saturday is None else saturday),
                "sunday": copy.deepcopy(ALL_P3 if sunday is None else sunday),
            },
            "holidays": list(holidays or []),
            "holiday_day_type": holiday_day_type,
        },
        "battery": {
            "capacity_kwh": 5.12,
            "power_kw": 2.5,
            "round_trip_efficiency": None,
            "soc_window_min": None,
            "soc_window_max": None,
            "import_limit_kw": None,
        },
    }


def write_tariff(
    directory: Path,
    *,
    name: str = "tariff.yaml",
    **document_kwargs: Any,
) -> Path:
    import yaml

    directory.mkdir(parents=True, exist_ok=True)
    path = directory / name
    path.write_text(yaml.safe_dump(tariff_document(**document_kwargs), sort_keys=False))
    return path


@pytest.fixture
def config(tmp_path: Path) -> Path:
    """A complete, loadable ``tariff.yaml`` for the tests that need one."""
    return write_tariff(_cfg_dir(tmp_path))


def _cfg_dir(tmp_path: Path) -> Path:
    directory = tmp_path / "cfg"
    directory.mkdir(parents=True, exist_ok=True)
    return directory


@pytest.fixture
def data_dir(tmp_path: Path) -> Path:
    directory = tmp_path / "data"
    directory.mkdir(parents=True, exist_ok=True)
    return directory


def build(data_dir: Path, config_path: Path, start: datetime, end: datetime, **kwargs: Any) -> Path:
    return build_dataset(
        data_dir=str(data_dir), config_path=str(config_path), start=start, end=end, **kwargs
    )


def build_rows(
    data_dir: Path, config_path: Path, start: datetime, end: datetime, **kwargs: Any
) -> tuple[pa.Table, dict[str, Any]]:
    """Run a build and return ``(analysis table, build manifest)``."""
    directory = build(data_dir, config_path, start, end, **kwargs)
    parts = sorted((directory / "parts").glob("*.parquet"))
    assert len(parts) == 1, f"expected exactly one output part, got {[p.name for p in parts]}"
    manifest = json.loads((directory / "build_manifest.json").read_text())
    return pq.read_table(parts[0]), manifest


# ---------------------------------------------------------------------------
# Allowed categorical values, read back out of the schemas.py documentation
# ---------------------------------------------------------------------------


def documented_values(field: str) -> set[str]:
    """The values ``schemas.py`` documents as allowed for one ANALYSIS_INTERVAL field."""
    doc = inspect.getdoc(schemas) or ""
    match = re.search(rf"``{field}`` \(ANALYSIS_INTERVAL\):\n((?:.*\n)*?)\n", doc)
    assert match, f"schemas.py no longer documents the allowed values of {field!r}"
    return set(re.findall(r"``([a-z_]+)``", match.group(1)))


ENERGY_QUALITY_VALUES = documented_values("energy_quality")
ALLOCATION_QUALITY_VALUES = documented_values("allocation_quality")
PRICE_QUALITY_VALUES = documented_values("price_quality")
DST_VALUES = documented_values("dst_transition")


# ===========================================================================
# 1. Series identity premise (verified fact 1: ``job`` stays in the identity)
# ===========================================================================


class TestSeriesIdentityPremise:
    def test_only_volatile_labels_are_excluded(self) -> None:
        assert set(VOLATILE_IDENTITY_LABELS) == {"instance", "pod_ip"}

    def test_job_change_mints_a_new_series_key(self) -> None:
        """The 2026-05-12 rename gives two series_keys for one physical counter."""
        old = compute_series_key(_labels(GRID, job=JOB_OLD))
        new = compute_series_key(_labels(GRID, job=JOB_NEW))
        assert old != new

    def test_instance_change_does_not_change_series_key(self) -> None:
        a = compute_series_key(_labels(GRID, instance="10.42.0.7:8080"))
        b = compute_series_key(_labels(GRID, instance="10.42.9.9:8080"))
        assert a == b

    def test_dead_duplicate_entity_is_declared(self) -> None:
        assert DEAD_PRICE in DEAD_DUPLICATE_ENTITY_IDS


# ===========================================================================
# 2. Era stitching
# ===========================================================================


class TestStitchSeries:
    def test_two_eras_concatenate_in_timestamp_order(self) -> None:
        old = counter_rows(_T0, 10, job=JOB_OLD, times=counter_times(_T0, 10))
        new = counter_rows(
            _T0 + timedelta(minutes=20),
            10,
            first=14612.0 + 0.004 * 10,
            job=JOB_NEW,
            times=counter_times(_T0 + timedelta(minutes=20), 10),
        )
        series = timeline(old + new)
        assert len(series) == 20
        assert series.times == tuple(sorted(series.times, key=lambda t: t))
        assert set(series.series_keys) == {
            compute_series_key(_labels(GRID, job=JOB_OLD)),
            compute_series_key(_labels(GRID, job=JOB_NEW)),
        }

    def test_input_order_is_irrelevant(self) -> None:
        rows = counter_rows(_T0, 6) + counter_rows(_T0 + timedelta(minutes=10), 6, first=14612.024)
        forward = timeline(rows)
        backward = timeline(list(reversed(rows)))
        assert forward.times == backward.times
        assert forward.values == backward.values

    def test_era_boundary_detected_and_reported(self) -> None:
        """The verified cutover: 14612.9732 → 14612.9852 kWh across a ~112 s gap."""
        at = datetime(2026, 5, 12, 21, 13, 42, tzinfo=UTC)
        rows = [
            grid_row(at, 14612.9732, job=JOB_OLD),
            grid_row(at + timedelta(seconds=112), 14612.9852, job=JOB_NEW),
        ]
        series = timeline(rows)
        assert len(series.era_boundaries) == 1
        boundary = series.era_boundaries[0]
        assert boundary.from_series_key != boundary.to_series_key
        assert boundary.previous_at == at
        assert boundary.at == at + timedelta(seconds=112)
        assert boundary.gap_seconds == pytest.approx(112.0)
        assert boundary.delta_kwh == pytest.approx(0.012, abs=1e-9)
        as_dict = boundary.as_dict()
        assert as_dict["era_boundary_at"] == "2026-05-12T21:15:34Z"
        assert as_dict["previous_sample_at"] == "2026-05-12T21:13:42Z"
        assert as_dict["delta_at_boundary"] == pytest.approx(0.012, abs=1e-9)

    def test_era_boundary_is_not_a_counter_reset(self) -> None:
        """A delta across the rename is legitimate: the counter is continuous."""
        at = datetime(2026, 5, 12, 21, 13, 42, tzinfo=UTC)
        rows = [
            grid_row(at - timedelta(minutes=2), 14612.9, job=JOB_OLD),
            grid_row(at, 14612.9732, job=JOB_OLD),
            grid_row(at + timedelta(seconds=112), 14612.9852, job=JOB_NEW),
            grid_row(at + timedelta(minutes=2), 14613.05, job=JOB_NEW),
        ]
        series = timeline(rows)
        spans = counter_deltas(series, gap_threshold_seconds=10_000)
        assert [span.is_reset for span in spans] == [False, False, False]
        assert all(span.delta is not None and span.delta >= 0 for span in spans)

    @staticmethod
    def _two_era_counter(at: datetime) -> tuple[list[dict[str, Any]], list[dict[str, Any]]]:
        """12 samples/hour of one counter, renamed scrape config at the hour mark.

        Era 1 reads 1000.0 → 1011.0, era 2 continues 1011.0 → 1022.0, so the whole
        home consumed 22 kWh and never 2033 kWh (the sum of two cumulative
        readings) nor 11 kWh (their average).
        """
        old = counter_rows(
            at, 12, first=1000.0, per_sample=1.0, times=counter_times(at, 12, 300.0), job=JOB_OLD
        )
        new = counter_rows(
            at + timedelta(hours=1),
            12,
            first=1011.0,
            per_sample=1.0,
            times=counter_times(at + timedelta(hours=1), 12, 300.0),
            job=JOB_NEW,
        )
        return old, new

    def test_eras_are_never_summed_as_independent_counters(self) -> None:
        at = datetime(2026, 5, 12, 0, 0, tzinfo=UTC)
        old, new = self._two_era_counter(at)
        series = timeline(old + new)
        stitched_total = series.values[-1] - series.values[0]
        assert stitched_total == pytest.approx(22.0)
        # What a naive per-series sum/average of the cumulative counters yields.
        assert old[-1]["value"] + new[-1]["value"] == pytest.approx(2033.0)
        assert len(series.era_boundaries) == 1
        assert all(
            not span.is_reset for span in counter_deltas(series, gap_threshold_seconds=10_000)
        )

    def test_eras_do_not_overlap(self) -> None:
        old = counter_rows(_T0, 20, times=counter_times(_T0, 20), job=JOB_OLD)
        new = counter_rows(
            _T0 + timedelta(minutes=20),
            20,
            first=14612.08,
            times=counter_times(_T0 + timedelta(minutes=20), 20),
        )
        old_ts = {r["ts"] for r in old}
        new_ts = {r["ts"] for r in new}
        assert not (old_ts & new_ts)
        series = timeline(old + new)  # stitching must succeed exactly because of that
        assert len(series) == 40

    def test_overlapping_timestamps_across_series_keys_raise(self) -> None:
        same_instant = _T0 + timedelta(seconds=39.219)
        rows = [
            grid_row(_T0, 1.0, job=JOB_OLD),
            grid_row(same_instant, 2.0, job=JOB_OLD),
            grid_row(same_instant, 2.1, job=JOB_NEW),  # two scrape configs at once
        ]
        with pytest.raises(OverlappingSeriesError) as excinfo:
            timeline(rows)
        assert "same instant" in str(excinfo.value)

    def test_overlap_error_is_a_build_error_and_not_swallowed(self) -> None:
        assert issubclass(OverlappingSeriesError, BuildError)
        rows = [grid_row(_T0, 1.0, job=JOB_OLD), grid_row(_T0, 1.0, job=JOB_NEW)]
        with pytest.raises(OverlappingSeriesError):
            timeline(rows)

    def test_identical_repeated_sample_is_deduplicated(self) -> None:
        """Overlapping extraction windows are expected and idempotent."""
        rows = [grid_row(_T0, 5.0), grid_row(_T0, 5.0)]
        series = timeline(rows)
        assert len(series) == 1
        assert series.duplicates_dropped == 1

    def test_same_key_same_instant_different_value_raises(self) -> None:
        rows = [grid_row(_T0, 5.0), grid_row(_T0, 5.5)]
        with pytest.raises(ConflictingSampleError):
            timeline(rows)

    @pytest.mark.parametrize("bad", [float("nan"), float("inf"), -float("inf")])
    def test_non_finite_value_raises(self, bad: float) -> None:
        with pytest.raises(BuildError, match="non-finite"):
            timeline([grid_row(_T0, bad)])

    def test_naive_timestamp_is_rejected(self) -> None:
        with pytest.raises(ValueError, match="timezone-aware"):
            RawPoint(ts=datetime(2026, 1, 5, 0, 0), value=1.0, series_key="k")

    def test_median_cadence_is_measured_from_the_samples(self) -> None:
        """``median_interval_seconds`` is the observed cadence, not an assumed 60."""
        import statistics

        times = counter_times(_T0, 21)
        series = timeline(counter_rows(_T0, 21, times=times))
        expected = statistics.median(
            [(times[i] - times[i - 1]).total_seconds() for i in range(1, len(times))]
        )
        assert series.median_interval_seconds == pytest.approx(expected)
        # The offsets make the median drift off the nominal 60 s cadence, which is
        # exactly why a hardcoded gap threshold would be wrong on the real archive.
        assert series.median_interval_seconds != pytest.approx(60.0, abs=1e-9)

    def test_empty_input_produces_empty_series(self) -> None:
        series = timeline([])
        assert len(series) == 0
        assert series.median_interval_seconds is None
        assert series.times_us() == ()
        assert series.as_dict()["samples"] == 0
        assert series.as_dict()["eras"] == 0

    def test_three_eras_back_and_forth(self) -> None:
        rows = (
            counter_rows(_T0, 3, first=1.0, job=JOB_OLD, times=counter_times(_T0, 3))
            + counter_rows(
                _T0 + timedelta(minutes=3),
                3,
                first=1.012,
                times=counter_times(_T0 + timedelta(minutes=3), 3),
            )
            + counter_rows(
                _T0 + timedelta(minutes=6),
                3,
                first=1.024,
                job=JOB_OLD,
                times=counter_times(_T0 + timedelta(minutes=6), 3),
            )
        )
        series = timeline(rows)
        assert len(series.era_boundaries) == 2
        assert series.as_dict()["eras"] == 3
        assert series.as_dict()["distinct_series_keys"] == 2

    def test_stitching_does_not_become_quadratic(self) -> None:
        """~1.5M samples exist per counter; an O(n²) scan cannot get there.

        The guard is deliberately loose: the previous implementation needed
        minutes for this many points, the linear one needs well under a second.
        """
        count = 200_000
        step = timedelta(seconds=60)
        points = [
            RawPoint(
                ts=_T0 + i * step + timedelta(milliseconds=(i * 137) % 1000),
                value=float(i),
                series_key="k",
            )
            for i in range(count)
        ]
        started = time.perf_counter()
        series = stitch_series(entity=GRID, metric="energy_kwh", unit="kWh", points=points)
        elapsed = time.perf_counter() - started
        assert len(series) == count
        assert elapsed < 15.0, f"stitching {count} points took {elapsed:.1f}s — not linear"

    def test_unit_is_part_of_the_group_key(self) -> None:
        """Two units for one (entity, metric) stay separate, never merged."""
        rows = [
            raw_row(GRID, "energy_kwh", "kWh", _T0, 1.0),
            raw_row(GRID, "energy_kwh", "Wh", _T0, 1000.0),
        ]
        first = stitch_series(
            entity=GRID, metric="energy_kwh", unit="kWh", points=points_from_rows([rows[0]])
        )
        second = stitch_series(
            entity=GRID, metric="energy_kwh", unit="Wh", points=points_from_rows([rows[1]])
        )
        assert first.values == (1.0,)
        assert second.values == (1000.0,)


# ===========================================================================
# 3. Counter deltas and the gap threshold
# ===========================================================================


class TestCounterDeltas:
    def test_spans_are_half_open_and_carry_the_delta(self) -> None:
        series = timeline(
            counter_rows(_T0, 3, first=10.0, per_sample=0.5, times=aligned_times(_T0, 3))
        )
        spans = counter_deltas(series)
        assert len(spans) == 2
        assert spans[0].t0 == _T0
        assert spans[0].t1 == _T0 + timedelta(minutes=1)
        assert spans[0].start_value == pytest.approx(10.0)
        assert spans[0].end_value == pytest.approx(10.5)
        assert spans[0].delta == pytest.approx(0.5)
        assert spans[0].known_total() == pytest.approx(0.5)
        assert spans[0].duration_seconds == pytest.approx(60.0)
        assert spans[0].is_gap is False
        assert spans[0].is_reset is False

    def test_gap_threshold_follows_measured_cadence(self) -> None:
        """The threshold is derived from the archive's median interval, not hardcoded."""
        series = timeline(counter_rows(_T0, 40, times=aligned_times(_T0, 40, 60.0)))
        assert series.median_interval_seconds == pytest.approx(60.0)
        spans = counter_deltas(series)
        assert spans[0].gap_threshold_seconds == pytest.approx(60.0 * build_mod.DEFAULT_GAP_FACTOR)

    def test_gap_factor_scales_the_derived_threshold(self) -> None:
        series = timeline(counter_rows(_T0, 40, times=aligned_times(_T0, 40, 60.0)))
        spans = counter_deltas(series, gap_factor=10.0)
        assert spans[0].gap_threshold_seconds == pytest.approx(600.0)
        assert all(not span.is_gap for span in spans)

    def test_explicit_threshold_overrides_the_measured_median(self) -> None:
        series = timeline(
            [
                grid_row(_T0, 1.0),
                grid_row(_T0 + timedelta(seconds=120), 1.5),
                grid_row(_T0 + timedelta(seconds=180), 2.0),
            ]
        )
        # median interval is 90 s → derived threshold 270 s → the 120 s span is no gap.
        assert series.median_interval_seconds == pytest.approx(90.0)
        assert not any(span.is_gap for span in counter_deltas(series))
        strict = counter_deltas(series, gap_threshold_seconds=60.0)
        assert [span.is_gap for span in strict] == [True, False]
        assert strict[0].gap_threshold_seconds == 60.0

    def test_gap_span_keeps_its_known_total(self) -> None:
        series = timeline(
            [
                grid_row(_T0, 1.0),
                grid_row(_T0 + timedelta(hours=2), 3.0),
                grid_row(_T0 + timedelta(hours=2, minutes=1), 3.1),
            ]
        )
        spans = counter_deltas(series, gap_threshold_seconds=60.0)
        gap = spans[0]
        assert gap.is_gap is True
        assert gap.duration_seconds == pytest.approx(7200.0)
        assert gap.delta == pytest.approx(2.0)

    def test_counter_reset_has_no_invented_value(self) -> None:
        series = timeline(
            [
                grid_row(_T0, 5.0),
                grid_row(_T0 + timedelta(seconds=30), 5.5),
                grid_row(_T0 + timedelta(seconds=60), 1.0),  # reset
                grid_row(_T0 + timedelta(seconds=90), 1.25),
            ]
        )
        spans = counter_deltas(series)
        assert [span.is_reset for span in spans] == [False, True, False]
        assert spans[1].delta is None
        assert spans[1].known_total() is None
        assert spans[1].start_value == pytest.approx(5.5)
        assert spans[1].end_value == pytest.approx(1.0)
        # The timeline continues from the bridge, so the post-reset delta is valid.
        assert spans[2].delta == pytest.approx(0.25)

    def test_reset_longer_than_the_gap_threshold_flags_both(self) -> None:
        series = timeline([grid_row(_T0, 9.0), grid_row(_T0 + timedelta(hours=3), 1.0)])
        spans = counter_deltas(series, gap_threshold_seconds=60.0)
        assert spans[0].is_reset is True
        assert spans[0].is_gap is True

    def test_flat_counter_is_not_a_reset(self) -> None:
        """Repeated identical readings (the HA exporter re-exports unchanged values)."""
        series = timeline([grid_row(_T0, 4.0), grid_row(_T0 + timedelta(seconds=60), 4.0)])
        spans = counter_deltas(series)
        assert spans[0].is_reset is False
        assert spans[0].delta == pytest.approx(0.0)

    def test_monotonic_archive_yields_no_resets(self) -> None:
        """Verified fact 4: 0 decreases over 72,946 real samples."""
        series = timeline(counter_rows(_T0, 500, per_sample=0.001))
        assert not any(span.is_reset for span in counter_deltas(series))

    def test_rejects_non_positive_gap_factor(self) -> None:
        series = timeline(counter_rows(_T0, 3))
        for bad in (0.0, -1.0):
            with pytest.raises(BuildError, match="gap_factor"):
                counter_deltas(series, gap_factor=bad)

    def test_rejects_non_positive_explicit_threshold(self) -> None:
        series = timeline(counter_rows(_T0, 3))
        with pytest.raises(BuildError, match="gap threshold"):
            counter_deltas(series, gap_threshold_seconds=0.0)

    def test_single_sample_has_no_measurable_cadence(self) -> None:
        series = timeline([grid_row(_T0, 1.0)])
        assert series.median_interval_seconds is None
        assert counter_deltas(series) == []

    def test_crosses_boundary_flags_spans_that_straddle_an_interval(self) -> None:
        series = timeline(
            [
                grid_row(_T0, 1.0),  # 00:00:00 — interval boundary
                grid_row(_T0 + timedelta(minutes=14), 1.1),
                grid_row(_T0 + timedelta(minutes=16), 1.2),  # crosses :15
            ]
        )
        spans = counter_deltas(series)
        assert [span.crosses_boundary for span in spans] == [False, True]

    def test_span_dict_is_manifest_friendly(self) -> None:
        series = timeline([grid_row(_T0, 1.0), grid_row(_T0 + timedelta(hours=1), 2.0)])
        payload = counter_deltas(series, gap_threshold_seconds=60.0)[0].as_dict()
        assert payload["duration_seconds"] == pytest.approx(3600.0)
        assert payload["kind"] == "gap"
        assert payload["span_start"] == build_mod.iso_z(_T0)
        assert payload["known_total"] == pytest.approx(1.0)
        assert payload["gap_threshold_seconds"] == 60.0

    def test_short_span_is_reported_as_normal(self) -> None:
        series = timeline([grid_row(_T0, 1.0), grid_row(_T0 + timedelta(seconds=90), 2.0)])
        payload = counter_deltas(series)[0].as_dict()
        assert payload["kind"] == "normal"


# ===========================================================================
# 4. UTC grid, local days and DST
# ===========================================================================


def local_day_utc_bounds(day: date, tz: ZoneInfo = MADRID) -> tuple[datetime, datetime]:
    """[local midnight, next local midnight) converted to absolute UTC."""
    start = datetime(day.year, day.month, day.day, tzinfo=tz).astimezone(UTC)
    following = day + timedelta(days=1)
    end = datetime(following.year, following.month, following.day, tzinfo=tz).astimezone(UTC)
    return start, end


class TestUtcGridAndDst:
    def test_local_day_length_measures_absolute_time(self) -> None:
        assert local_day_length_seconds(date(2026, 3, 29), MADRID) == pytest.approx(82800.0)
        assert local_day_length_seconds(date(2025, 10, 26), MADRID) == pytest.approx(90000.0)
        assert local_day_length_seconds(date(2026, 1, 5), MADRID) == pytest.approx(86400.0)

    def test_local_day_length_is_not_the_wall_clock_difference(self) -> None:
        """Two datetimes sharing one ``tzinfo`` subtract as wall clock in CPython.

        That silently returns 86400 for every day, which is exactly how a
        ``dst_transition`` column ends up always null.
        """
        spring = datetime(2026, 3, 29, tzinfo=MADRID)
        autumn = datetime(2026, 3, 30, tzinfo=MADRID)
        assert (autumn - spring).total_seconds() == pytest.approx(86400.0)  # the trap
        assert local_day_length_seconds(date(2026, 3, 29), MADRID) != pytest.approx(86400.0)

    def test_dst_labels_from_day_length(self) -> None:
        assert dst_transition_for(date(2026, 3, 29), MADRID) == build_mod.DST_SPRING_FORWARD
        assert dst_transition_for(date(2025, 10, 26), MADRID) == build_mod.DST_FALL_BACK
        assert dst_transition_for(date(2026, 3, 28), MADRID) is None
        assert dst_transition_for(date(2026, 3, 30), MADRID) is None

    def test_dst_labels_are_not_a_date_table(self) -> None:
        """Lord Howe shifts by 30 minutes: derived lengths, not hardcoded dates."""
        lord_howe = ZoneInfo("Australia/Lord_Howe")
        assert local_day_length_seconds(date(2026, 10, 4), lord_howe) == pytest.approx(84600.0)
        assert dst_transition_for(date(2026, 10, 4), lord_howe) == build_mod.DST_SPRING_FORWARD
        assert local_day_length_seconds(date(2026, 4, 5), lord_howe) == pytest.approx(88200.0)
        assert dst_transition_for(date(2026, 4, 5), lord_howe) == build_mod.DST_FALL_BACK
        assert local_day_length_seconds(date(2026, 10, 3), lord_howe) == pytest.approx(86400.0)

    def test_interval_count_is_day_length_over_900(self) -> None:
        """A 23-hour Spanish day is 92 intervals, a 25-hour day is 100, always."""
        for day in (date(2026, 3, 29), date(2025, 10, 26), date(2026, 1, 5)):
            start, end = local_day_utc_bounds(day)
            grid = grid_times(start, end)
            assert len(grid) == int(local_day_length_seconds(day, MADRID) // INTERVAL_SECONDS)
        assert len(grid_times(*local_day_utc_bounds(date(2026, 3, 29)))) == 92
        assert len(grid_times(*local_day_utc_bounds(date(2025, 10, 26)))) == 100
        assert len(grid_times(*local_day_utc_bounds(date(2026, 1, 5)))) == 96

    def test_grid_is_utc_aligned_and_contiguous(self) -> None:
        start, end = local_day_utc_bounds(date(2026, 3, 29))
        grid = grid_times(start, end)
        assert grid[0] == start
        assert all(t.tzinfo is not None for t in grid)
        assert all((b - a) == timedelta(seconds=INTERVAL_SECONDS) for a, b in zip(grid, grid[1:]))
        assert grid[-1] + timedelta(seconds=INTERVAL_SECONDS) == end
        assert all(int(t.timestamp()) % INTERVAL_SECONDS == 0 for t in grid)

    def test_grid_floors_an_unaligned_start(self) -> None:
        aligned = _T0
        for extra in (1, 39_219_000, 899):
            start = aligned + timedelta(microseconds=extra)
            grid = grid_times(start, start + timedelta(minutes=45))
            assert grid[0] == aligned, extra
            assert len(grid) == 4, extra

    def test_grid_excludes_the_end_bound(self) -> None:
        grid = grid_times(_T0, _T0 + timedelta(minutes=30))
        assert grid == [_T0, _T0 + timedelta(minutes=15)]
        assert _T0 + timedelta(minutes=30) not in grid

    def test_grid_accepts_non_utc_offsets(self) -> None:
        """An offset-aware, non-UTC instant denotes the same absolute time."""
        madrid_start = datetime(2026, 1, 5, 1, 0, tzinfo=MADRID)  # 00:00Z
        grid = make_utc_grid(madrid_start, madrid_start + timedelta(minutes=30))
        assert grid[0] == _T0

    @pytest.mark.parametrize("step", [0, -900])
    def test_grid_rejects_bad_interval(self, step: int) -> None:
        with pytest.raises(ValueError, match="interval_seconds"):
            make_utc_grid(_T0, _T0 + timedelta(hours=1), interval_seconds=step)

    def test_grid_rejects_naive_bounds(self) -> None:
        naive = datetime(2026, 1, 5, 0, 0)
        with pytest.raises(ValueError, match="timezone-aware"):
            make_utc_grid(naive, _T0 + timedelta(hours=1))
        with pytest.raises(ValueError, match="timezone-aware"):
            make_utc_grid(_T0, naive + timedelta(hours=1))

    def test_grid_rejects_empty_window(self) -> None:
        with pytest.raises(ValueError, match="before end"):
            make_utc_grid(_T0, _T0)
        with pytest.raises(ValueError, match="before end"):
            make_utc_grid(_T0 + timedelta(hours=1), _T0)

    def test_allocate_rejects_bad_interval_seconds(self) -> None:
        series = timeline(counter_rows(_T0, 3))
        with pytest.raises(BuildError, match="interval_seconds"):
            allocate_energy(
                series,
                counter_deltas(series),
                grid_times(_T0, _T0 + timedelta(hours=1)),
                interval_seconds=0,
            )


# ===========================================================================
# 5. Tariff periods come from tariff.yaml
# ===========================================================================

REPO_TARIFF = Path(__file__).resolve().parents[1] / "config" / "tariff.yaml"


class TestTariffPeriods:
    def test_repo_config_loads_and_is_timezone_aware(self) -> None:
        """The shipped config must parse: it is the default CLI path."""
        cfg = load_config(REPO_TARIFF)
        assert cfg.tariff_periods.timezone == "Europe/Madrid"
        assert cfg.tariff_periods.holidays == ()

    def test_repo_weekday_bands(self) -> None:
        periods = load_config(REPO_TARIFF).tariff_periods
        expected = {
            **{h: 3 for h in range(0, 8)},
            **{h: 2 for h in range(8, 10)},
            **{h: 1 for h in range(10, 14)},
            **{h: 2 for h in range(14, 18)},
            **{h: 1 for h in range(18, 22)},
            **{h: 2 for h in range(22, 24)},
        }
        monday = date(2026, 1, 5)
        for hour, want in expected.items():
            assert tariff_period_for(periods, monday, hour) == want, f"weekday hour {hour}"

    def test_repo_weekend_bands_are_valle(self) -> None:
        periods = load_config(REPO_TARIFF).tariff_periods
        for day in (date(2026, 1, 10), date(2026, 1, 11)):  # Saturday, Sunday
            for hour in range(24):
                assert tariff_period_for(periods, day, hour) == 3, f"{day} hour {hour}"

    def test_holiday_handling_is_the_documented_fallback(self) -> None:
        """No invented holiday list: an unsourced holiday keeps the weekday bands."""
        periods = load_config(REPO_TARIFF).tariff_periods
        assert periods.holidays == (), "holidays must stay empty until sourced"
        national_holiday = date(2026, 1, 6)  # Epiphany, a Tuesday
        assert periods.day_type(national_holiday) == "weekday"
        assert tariff_period_for(periods, national_holiday, 19) == 1

    def test_sourced_holiday_uses_the_configured_day_type(self, tmp_path: Path) -> None:
        path = write_tariff(tmp_path, holidays=["2026-01-06"])
        periods = load_config(path).tariff_periods
        assert periods.day_type(date(2026, 1, 6)) == "sunday"
        assert tariff_period_for(periods, date(2026, 1, 6), 19) == 3
        # and the neighbouring ordinary Tuesday still uses the weekday bands
        assert tariff_period_for(periods, date(2026, 1, 7), 19) == 1

    def test_holiday_day_type_is_configurable(self, tmp_path: Path) -> None:
        path = write_tariff(tmp_path, holidays=["2026-01-06"], holiday_day_type="saturday")
        periods = load_config(path).tariff_periods
        assert periods.day_type(date(2026, 1, 6)) == "saturday"

    def test_missing_rules_refuse_to_guess(self, tmp_path: Path) -> None:
        """Periods are data, never a hardcoded fallback."""
        empty = TariffPeriods(timezone="Europe/Madrid")
        with pytest.raises(ValueError, match="never hardcoded"):
            tariff_period_for(empty, date(2026, 1, 5), 12)

    def test_rules_must_tile_the_day(self, tmp_path: Path) -> None:
        import yaml

        hole = tariff_document(weekday=[{"period": 3, "start_hour": 0, "end_hour": 23}])
        path = tmp_path / "hole.yaml"
        path.write_text(yaml.safe_dump(hole))
        with pytest.raises(ValueError, match="uncovered hours=\\[23\\]"):
            load_config(path)

        overlap = tariff_document(
            weekday=[
                {"period": 3, "start_hour": 0, "end_hour": 12},
                {"period": 1, "start_hour": 6, "end_hour": 24},
            ]
        )
        path2 = tmp_path / "overlap.yaml"
        path2.write_text(yaml.safe_dump(overlap))
        with pytest.raises(ValueError, match="doubly-covered hours"):
            load_config(path2)

    def test_period_rules_are_local_hours_not_utc_hours(self) -> None:
        """10:00 in Madrid is 09:00Z in winter: the local hour must win."""
        bands = [3] * 8 + [2] * 2 + [1] * 4 + [2] * 4 + [1] * 4 + [2] * 2
        periods = TariffPeriods(
            timezone="Europe/Madrid",
            weekday=tuple(
                PeriodRule(period=period, start_hour=hour, end_hour=hour + 1)
                for hour, period in enumerate(bands)
            ),
            saturday=(PeriodRule(period=3, start_hour=0, end_hour=24),),
            sunday=(PeriodRule(period=3, start_hour=0, end_hour=24),),
        )
        utc_instant = datetime(2026, 1, 5, 9, 30, tzinfo=UTC)  # 10:30 in Madrid
        local = utc_instant.astimezone(MADRID)
        assert local.hour == 10
        assert tariff_period_for(periods, local.date(), local.hour) == 1
        # Reading the UTC hour instead would label the same instant P2.
        assert tariff_period_for(periods, utc_instant.date(), utc_instant.hour) == 2

    def test_output_changes_when_the_config_changes(self, tmp_path: Path, data_dir: Path) -> None:
        """Prove the periods are config-driven by editing the config, not the code."""
        start, end = _T0, _T0 + timedelta(hours=1)
        write_raw_part(data_dir, counter_rows(_T0, 10, times=aligned_times(_T0, 10, 300.0)))
        default_cfg = write_tariff(tmp_path / "cfg1")
        # Peak shifted onto every hour of the weekday: same data, different labels.
        flat_cfg = write_tariff(
            tmp_path / "cfg2", weekday=[{"period": 1, "start_hour": 0, "end_hour": 24}]
        )

        table_default, _ = build_rows(
            data_dir, default_cfg, start, end, lookback=timedelta(minutes=5)
        )
        table_flat, _ = build_rows(data_dir, flat_cfg, start, end, lookback=timedelta(minutes=5))
        # 00:00Z–01:00Z is local hour 01 (night) → P3 under the repo bands.
        assert set(values_for(table_default, "tariff_period")) == {3}
        assert set(values_for(table_flat, "tariff_period")) == {1}
        assert (
            table_default.column("dataset_id")[0].as_py()
            != table_flat.column("dataset_id")[0].as_py()
        )

    def test_dst_day_tariff_uses_local_wall_clock(self, tmp_path: Path, data_dir: Path) -> None:
        """The spring-forward day keeps 24 local hours' worth of labels."""
        day = date(2026, 3, 29)
        start, end = local_day_utc_bounds(day)
        count = int((end - start).total_seconds() // 60)
        write_raw_part(
            data_dir,
            counter_rows(start, count, per_sample=0.0005, times=counter_times(start, count, 60.0)),
        )
        cfg = write_tariff(tmp_path)
        table, _ = build_rows(data_dir, cfg, start, end, lookback=timedelta(minutes=5))
        hours = [row["interval_start"].astimezone(MADRID).hour for row in rows_as_dicts(table)]
        # Local 02:00 does not exist that day, so no interval may claim it.
        assert 2 not in hours
        assert len(hours) == 92
        assert hours[0] == 0 and hours[-1] == 23


# ===========================================================================
# 6. Attributing counter deltas onto the grid
# ===========================================================================


def allocate(
    rows: list[dict[str, Any]],
    *,
    grid_start: datetime = _T0,
    grid_hours: float = 1.0,
    gap_threshold_seconds: float | None = None,
    gap_factor: float = build_mod.DEFAULT_GAP_FACTOR,
) -> tuple[
    StitchedSeries, list[DeltaSpan], list[datetime], list[IntervalEnergy], build_mod.SpanLedger
]:
    """Stitch → deltas → allocate, for the pure-logic tests below."""
    series = timeline(rows)
    spans = counter_deltas(
        series, gap_factor=gap_factor, gap_threshold_seconds=gap_threshold_seconds
    )
    grid = grid_times(grid_start, grid_start + timedelta(hours=grid_hours))
    energies, ledger = allocate_energy(series, spans, grid)
    return series, spans, grid, energies, ledger


def emitted(energies: list[IntervalEnergy]) -> float:
    return sum(e.raw_grid_kwh for e in energies if e.raw_grid_kwh is not None)


class TestAllocateEnergy:
    def test_estimated_boundary_never_claims_exact_and_never_null(self) -> None:
        """The invariants that make the estimate safe to consume.

        ``estimated_boundary`` must always carry a number (it is a real counter
        delta), must never be spelled ``exact``, and must never carry
        ``gap_seconds`` — an ordinary boundary crossing is not a data gap.
        """
        rows = counter_rows(_T0, 121, times=counter_times(_T0, 121, 60.0))
        _series, _spans, _grid, energies, ledger = allocate(rows, grid_hours=2)
        estimated = [e for e in energies if e.allocation_quality == ALLOCATION_ESTIMATED_BOUNDARY]
        assert estimated, "irregular cadence must produce estimated intervals"
        for energy in estimated:
            assert energy.raw_grid_kwh is not None
            assert energy.raw_grid_kwh >= 0.0
            assert energy.energy_quality == ENERGY_MEASURED
            assert energy.gap_seconds == 0.0, "a boundary split is not a gap"
        # Ledger counts agree with what the intervals claim.
        assert ledger.estimated_interval_count == len(estimated)

    def test_unknown_gap_never_carries_a_number(self) -> None:
        """``unknown_gap`` is only ever reported for an interval with no value."""
        before = counter_rows(_T0, 20, times=counter_times(_T0, 20, 60.0))
        after = counter_rows(
            _T0 + timedelta(hours=1),
            20,
            first=before[-1]["value"] + 0.4,
            times=counter_times(_T0 + timedelta(hours=1), 20, 60.0),
        )
        _series, _spans, _grid, energies, ledger = allocate(
            before + after, grid_hours=2, gap_threshold_seconds=600.0
        )
        for energy in energies:
            if energy.allocation_quality == ALLOCATION_UNKNOWN_GAP:
                assert energy.raw_grid_kwh is None, (
                    "unknown_gap is never-benchmarked: a number here would be invented"
                )
        assert ledger.gap_span_count == 1
        assert ledger.unattributed_interval_count == sum(
            1 for e in energies if e.raw_grid_kwh is None
        )

    def test_contained_spans_are_measured_and_exact(self) -> None:
        # 61 samples at 60 s cover the whole hour exactly; every interval has 15 spans.
        rows = counter_rows(
            _T0, 61, first=100.0, per_sample=0.01, times=aligned_times(_T0, 61, 60.0)
        )
        _series, _spans, _grid, energies, ledger = allocate(rows, grid_hours=1)
        assert all(e.energy_quality == ENERGY_MEASURED for e in energies)
        assert all(e.allocation_quality == ALLOCATION_EXACT for e in energies)
        assert all(e.raw_grid_kwh == pytest.approx(0.15) for e in energies)
        assert ledger.gap_span_count == ledger.reset_span_count == ledger.boundary_span_count == 0

    def test_three_contained_spans_sum_into_one_interval(self) -> None:
        rows = counter_rows(_T0, 4, first=0.0, per_sample=1.0, times=aligned_times(_T0, 4, 300.0))
        _series, _spans, _grid, energies, _ledger = allocate(rows, grid_hours=1)
        assert energies[0].raw_grid_kwh == pytest.approx(3.0)

    def test_boundary_crossing_span_is_split_proportionally_and_flagged(self) -> None:
        """Normal cadence across a boundary: proportional split, ``estimated_boundary``.

        A 20-minute span (normal cadence for a sparse archive) carries a known
        total whose distribution is unknown, so the counter is linearly
        interpolated at the ``:15`` boundary: 900/1200 of the delta stays in the
        first interval and the remaining 300/1200 goes to the second.  Neither
        interval may claim ``exact``.
        """
        rows = [grid_row(_T0, 1.0), grid_row(_T0 + timedelta(minutes=20), 1.1)]
        _series, spans, _grid, energies, _ledger = allocate(rows, grid_hours=1)
        assert spans[0].delta == pytest.approx(0.1)
        assert energies[0].raw_grid_kwh == pytest.approx(0.1 * 900 / 1200)
        assert energies[0].energy_quality == ENERGY_MEASURED
        assert energies[0].allocation_quality == ALLOCATION_ESTIMATED_BOUNDARY
        assert energies[1].raw_grid_kwh == pytest.approx(0.1 * 300 / 1200)
        assert energies[1].energy_quality == ENERGY_MEASURED
        assert energies[1].allocation_quality == ALLOCATION_ESTIMATED_BOUNDARY
        assert emitted(energies) == pytest.approx(0.1), "the span total must not be lost"

    def test_boundary_split_telescopes_and_never_claims_exact(self) -> None:
        """Splitting is an estimate of placement, never an invention of a total."""
        rows = [grid_row(_T0, 1.0), grid_row(_T0 + timedelta(minutes=20), 1.1)]
        _series, _spans, _grid, energies, ledger = allocate(rows, grid_hours=1)
        values = [e.raw_grid_kwh for e in energies if e.raw_grid_kwh is not None]
        assert sum(values) == pytest.approx(0.1)
        assert ALLOCATION_EXACT not in [e.allocation_quality for e in energies[:2]]
        assert ledger.boundary_span_count == 1
        assert ledger.boundary_piece_count == 2
        assert ledger.estimated_interval_count == 2

    def test_span_contained_in_one_interval_stays_exact(self) -> None:
        """``exact`` survives only for spans that never cross a boundary."""
        rows = [
            grid_row(_T0, 1.0),
            grid_row(_T0 + timedelta(minutes=5), 1.1),
            grid_row(_T0 + timedelta(minutes=10), 1.2),
            grid_row(_T0 + timedelta(minutes=14), 1.3),
        ]
        _series, _spans, _grid, energies, ledger = allocate(rows, grid_hours=1)
        assert energies[0].allocation_quality == ALLOCATION_EXACT
        assert energies[0].raw_grid_kwh == pytest.approx(0.3)
        assert ledger.boundary_span_count == 0

    def test_span_total_is_recorded_once_not_once_per_interval(self) -> None:
        """One 20-minute span touches two intervals; the ledger must not double it."""
        rows = [grid_row(_T0, 1.0), grid_row(_T0 + timedelta(minutes=20), 1.1)]
        _series, _spans, _grid, _energies, ledger = allocate(rows, grid_hours=1)
        assert ledger.boundary_span_count == 1
        assert ledger.boundary_span_total_kwh == pytest.approx(0.1)

    def test_long_gap_total_is_recorded_once(self) -> None:
        rows = [
            grid_row(_T0, 1.0),
            grid_row(_T0 + timedelta(hours=2), 3.0),
            grid_row(_T0 + timedelta(hours=2, minutes=1), 3.1),
        ]
        _series, _spans, _grid, _energies, ledger = allocate(
            rows, grid_hours=3, gap_threshold_seconds=600.0
        )
        assert ledger.gap_span_count == 1, "a 2 h gap touches 8 intervals but is one span"
        assert ledger.gap_span_total_kwh == pytest.approx(2.0)

    def test_gap_flags_intervals_with_real_gap_seconds_and_no_zeroes(self) -> None:
        before = counter_rows(_T0, 30, times=counter_times(_T0, 30, 60.0))
        after = counter_rows(
            _T0 + timedelta(hours=2, minutes=30),
            30,
            first=before[-1]["value"] + 0.5,
            times=counter_times(_T0 + timedelta(hours=2, minutes=30), 30, 60.0),
        )
        _series, _spans, grid, energies, ledger = allocate(
            before + after, grid_hours=4, gap_threshold_seconds=600.0
        )
        by_start = dict(zip(grid, energies, strict=True))
        outage = [
            start
            for start in grid
            if timedelta(minutes=30) <= start - _T0 < timedelta(hours=2, minutes=30)
        ]
        assert len(outage) > 5, "the outage must cover several intervals"
        for start in outage:
            row = by_start[start]
            assert row.raw_grid_kwh is None, f"{start} must not fabricate consumption"
            assert row.gap_seconds > 0
            assert row.gap_seconds >= 7200.0 - 60.0
            assert row.energy_quality in {ENERGY_GAP_PARTIAL, ENERGY_GAP_TOTAL_UNKNOWN}
            assert row.allocation_quality == ALLOCATION_UNKNOWN_GAP
        # Intervals inside the outage with no reading at all are fully unknown.
        assert by_start[_T0 + timedelta(hours=1)].energy_quality == ENERGY_GAP_TOTAL_UNKNOWN
        assert by_start[_T0 + timedelta(minutes=30)].energy_quality == ENERGY_GAP_TOTAL_UNKNOWN
        assert ledger.gap_span_count == 1

    def test_gap_detection_uses_the_measured_median_by_default(self) -> None:
        rows = counter_rows(_T0, 30, times=aligned_times(_T0, 30, 60.0))
        rows += counter_rows(
            _T0 + timedelta(hours=1),
            5,
            first=rows[-1]["value"] + 0.2,
            times=aligned_times(_T0 + timedelta(hours=1), 5, 60.0),
        )
        _series, spans, _grid, _energies, ledger = allocate(rows, grid_hours=2)
        assert spans[29].duration_seconds == pytest.approx(1860.0)
        assert spans[29].is_gap is True
        assert ledger.gap_span_count == 1

    def test_contained_gap_span_keeps_its_known_energy(self) -> None:
        """A gap fully inside one interval: flagged, but its energy is not lost."""
        times = [
            _T0,
            _T0 + timedelta(seconds=60),
            _T0 + timedelta(seconds=120),
            _T0 + timedelta(seconds=420),
        ]
        times += [_T0 + timedelta(seconds=480 + 60 * i) for i in range(6)]
        rows = counter_rows(_T0, len(times), first=0.0, per_sample=0.1, times=times)
        _series, spans, _grid, energies, ledger = allocate(
            rows, grid_hours=1, gap_threshold_seconds=180.0
        )
        assert spans[2].is_gap is True
        assert spans[2].duration_seconds == pytest.approx(300.0)
        # All spans (including the contained gap) complete inside the first interval.
        assert energies[0].raw_grid_kwh == pytest.approx(0.9)
        assert energies[0].energy_quality == ENERGY_GAP_PARTIAL
        assert energies[0].gap_seconds == pytest.approx(300.0)
        assert emitted(energies) == pytest.approx(rows[-1]["value"] - rows[0]["value"])
        assert ledger.gap_span_count == 0

    def test_telescoping_sum_on_irregular_offsets(self) -> None:
        """Boundary splitting preserves telescoping: the sum equals last − first.

        Every span of a ~60 s irregular-offset series crosses a 15-minute
        boundary except those fully inside an interval, so this is the case that
        the split must not damage: the pieces of a span re-sum to its delta, and
        therefore the sum over the whole grid is the exact counter difference.
        """
        rows = counter_rows(_T0, 120, first=14612.0, per_sample=0.004)
        series, _spans, _grid, energies, _ledger = allocate(rows, grid_hours=2)
        # Every sample lies inside the 2 h grid, so no piece is truncated at the
        # grid edge and the identity is the plain counter difference.
        assert series.times[-1] < _grid[-1] + timedelta(seconds=INTERVAL_SECONDS)
        assert emitted(energies) == pytest.approx(series.values[-1] - series.values[0], abs=1e-12)
        assert all(e.raw_grid_kwh is not None for e in energies)

    def test_telescoping_is_bit_exact_for_dyadic_splits(self) -> None:
        """The re-summation is exact in binary floating point, not just close.

        Deltas that are powers of two split on quarter boundaries are exactly
        representable, so the sum of allocated pieces must equal the counter
        difference with no tolerance at all.
        """
        times = [
            _T0,
            _T0 + timedelta(seconds=675),  # 11m15s: 225 s before the :15 boundary
            _T0 + timedelta(seconds=1350),  # 22m30s
            _T0 + timedelta(seconds=2025),  # 33m45s
            _T0 + timedelta(seconds=2700),  # 45m — exactly the last interval's end
        ]
        ticks = [0.0, 0.25, 0.5, 0.75, 1.0]  # exact in binary floating point
        rows = [grid_row(t, v) for t, v in zip(times, ticks, strict=True)]
        series = timeline(rows)
        spans = counter_deltas(series, gap_threshold_seconds=3600.0)
        grid = grid_times(_T0, _T0 + timedelta(minutes=45))
        energies, _ledger = allocate_energy(series, spans, grid)
        assert all(e.raw_grid_kwh is not None for e in energies)
        assert sum(e.raw_grid_kwh for e in energies) == series.values[-1] - series.values[0]

    def test_telescoping_sum_on_aligned_samples(self) -> None:
        """A span ending exactly on a boundary belongs to that interval.

        Samples sitting on the grid (a natural synthetic shape) must not fall
        between two intervals and vanish.
        """
        rows = counter_rows(
            _T0, 61, first=500.0, per_sample=0.02, times=aligned_times(_T0, 61, 60.0)
        )
        series, _spans, _grid, energies, _ledger = allocate(rows, grid_hours=1)
        assert emitted(energies) == pytest.approx(series.values[-1] - series.values[0], abs=1e-9)
        assert all(e.allocation_quality == ALLOCATION_EXACT for e in energies)

    def test_no_fabricated_zero_after_the_last_reading(self) -> None:
        rows = [grid_row(_T0, 1.0), grid_row(_T0 + timedelta(minutes=20), 1.2)]
        _series, _spans, grid, energies, _ledger = allocate(rows, grid_hours=1)
        tail = [
            e
            for start, e in zip(grid, energies, strict=True)
            if start >= _T0 + timedelta(minutes=30)
        ]
        assert len(tail) == 2
        for row in tail:
            assert row.raw_grid_kwh is None
            assert row.energy_quality == ENERGY_GAP_TOTAL_UNKNOWN
        # Even the interval holding only the final sample has no touching span.
        assert energies[2].raw_grid_kwh is None

    def test_counter_reset_emits_no_negative_energy(self) -> None:
        rows = [
            grid_row(_T0, 5.0),
            grid_row(_T0 + timedelta(seconds=300), 5.5),
            grid_row(_T0 + timedelta(seconds=600), 1.0),
            grid_row(_T0 + timedelta(seconds=900), 1.5),
            grid_row(_T0 + timedelta(seconds=1200), 2.0),
        ]
        _series, spans, _grid, energies, ledger = allocate(rows, grid_hours=1)
        assert any(span.is_reset for span in spans)
        assert all(e.raw_grid_kwh is None or e.raw_grid_kwh >= 0.0 for e in energies)
        assert energies[0].energy_quality == ENERGY_COUNTER_RESET_BRIDGED
        assert energies[0].raw_grid_kwh is None
        assert energies[0].allocation_quality == ALLOCATION_UNKNOWN_GAP
        assert ledger.reset_span_count == 1

    def test_reset_span_touching_two_intervals_is_counted_once(self) -> None:
        rows = [
            grid_row(_T0 + timedelta(minutes=10), 5.0),
            grid_row(_T0 + timedelta(minutes=20), 1.0),  # the reset crosses :15
        ]
        _series, _spans, _grid, energies, ledger = allocate(rows, grid_hours=1)
        assert ledger.reset_span_count == 1
        assert {e.energy_quality for e in energies[:2]} == {ENERGY_COUNTER_RESET_BRIDGED}
        assert all(e.raw_grid_kwh is None for e in energies[:2])

    def test_timeline_continues_after_a_reset(self) -> None:
        """Post-reset deltas stay valid, so the series does not die at the reset."""
        rows = [
            grid_row(_T0, 5.0),
            grid_row(_T0 + timedelta(minutes=30), 1.0),
            grid_row(_T0 + timedelta(minutes=45), 1.25),
            grid_row(_T0 + timedelta(minutes=60), 1.5),
        ]
        _series, _spans, grid, energies, _ledger = allocate(rows, grid_hours=2)
        after = [
            e
            for start, e in zip(grid, energies, strict=True)
            if _T0 + timedelta(minutes=30) <= start < _T0 + timedelta(minutes=60)
        ]
        assert len(after) == 2
        assert all(e.raw_grid_kwh == pytest.approx(0.25) for e in after)

    def test_sample_count_is_half_open(self) -> None:
        rows = counter_rows(_T0, 31, times=aligned_times(_T0, 31, 60.0))
        _series, _spans, _grid, energies, _ledger = allocate(rows, grid_hours=1)
        assert [e.sample_count for e in energies] == [15, 15, 1, 0]
        assert sum(e.sample_count for e in energies) == 31, (
            "each sample counted in [start, end) only"
        )

    def test_sample_count_with_sub_second_offsets(self) -> None:
        rows = counter_rows(_T0, 61, times=counter_times(_T0, 61, 60.0))
        _series, _spans, grid, energies, _ledger = allocate(rows, grid_hours=1)
        stamps = [r["ts"] for r in rows]
        for start, energy in zip(grid, energies, strict=True):
            expected = sum(
                1 for ts in stamps if start <= ts < start + timedelta(seconds=INTERVAL_SECONDS)
            )
            assert energy.sample_count == expected, start

    def test_empty_spans_leave_every_interval_unknown(self) -> None:
        rows = [grid_row(_T0, 1.0)]
        _series, _spans, _grid, energies, ledger = allocate(rows, grid_hours=1)
        assert all(e.raw_grid_kwh is None for e in energies)
        assert all(e.energy_quality == ENERGY_GAP_TOTAL_UNKNOWN for e in energies)
        assert ledger.unattributed_interval_count == len(energies)

    def test_dense_cadence_records_no_gap_seconds(self) -> None:
        rows = counter_rows(_T0, 121, times=counter_times(_T0, 121, 60.0))
        _series, _spans, _grid, energies, _ledger = allocate(rows, grid_hours=2)
        assert all(e.gap_seconds == 0.0 for e in energies)

    def test_jitter_heavy_cadence_over_flags_under_default_gap_factor(self) -> None:
        """A cadence with actual gaps over-flags under the default gap factor.

        With :data:`build_mod.DEFAULT_GAP_FACTOR` (3), any span longer than
        three times the median interval is a gap.  A cadence with 60-second
        samples and occasional 5-sample gaps (300 seconds) has median 60,
        threshold 180, and gap duration 360 seconds.  Those intervals are
        flagged ``energy_quality="gap_partial"`` — the builder still attributes
        the energy (because the gap is contained within the interval), but
        the flag warns consumers that the cadence was irregular.  An archive
        with frequent gaps must therefore pass an explicit
        ``gap_threshold_seconds`` to suppress the flags.
        """
        # Create a sequence with explicit gaps by skipping samples
        times = []
        values = []
        for i in range(121):
            # Skip some samples to create 3 gaps of 5 samples each
            if 20 <= i < 25 or 50 <= i < 55 or 80 <= i < 85:
                continue
            times.append(_T0 + timedelta(seconds=i * 60))
            values.append(14612.0 + i * 0.004)

        rows = [grid_row(t, v) for t, v in zip(times, values)]
        _series, _spans, _grid, energies, _ledger = allocate(rows, grid_hours=2)
        gap_count = sum(1 for e in energies if e.energy_quality == ENERGY_GAP_PARTIAL)
        assert gap_count == 3, "the default gap factor flags 3 intervals"
        # The energy is still attributed (gaps are contained within intervals)
        assert all(
            e.raw_grid_kwh is not None for e in energies if e.energy_quality == ENERGY_GAP_PARTIAL
        )
        # With an explicit threshold that exceeds the max delta, no spans are
        # flagged and the full telescoping sum is emitted.
        _series2, _spans2, _grid2, energies2, _ledger2 = allocate(
            rows, grid_hours=2, gap_threshold_seconds=400.0
        )
        assert all(e.energy_quality == ENERGY_MEASURED for e in energies2)
        assert emitted(energies) == emitted(energies2)
        assert emitted(energies2) == pytest.approx(
            _series2.values[-1] - _series2.values[0], abs=1e-9
        )

    @pytest.mark.parametrize(
        "make_rows",
        [
            pytest.param(
                lambda: counter_rows(_T0, 61, times=aligned_times(_T0, 61, 60.0)), id="aligned"
            ),
            pytest.param(
                lambda: counter_rows(_T0, 61, times=counter_times(_T0, 61, 60.0)), id="irregular"
            ),
            pytest.param(
                lambda: [
                    grid_row(_T0, 1.0),
                    grid_row(_T0 + timedelta(hours=3), 2.0),
                    grid_row(_T0 + timedelta(hours=4), 2.5),
                ],
                id="outage",
            ),
            pytest.param(
                lambda: [
                    grid_row(_T0, 5.0),
                    grid_row(_T0 + timedelta(minutes=20), 1.0),
                    grid_row(_T0 + timedelta(minutes=40), 1.2),
                ],
                id="reset",
            ),
        ],
    )
    def test_measured_quality_always_carries_a_number(self, make_rows) -> None:
        """``energy_quality="measured"`` with a null energy would be a lie."""
        _series, _spans, _grid, energies, _ledger = allocate(make_rows(), grid_hours=5)
        for energy in energies:
            if energy.energy_quality == ENERGY_MEASURED:
                assert energy.raw_grid_kwh is not None
            if energy.raw_grid_kwh is None:
                assert energy.energy_quality != ENERGY_MEASURED
            assert energy.energy_quality in ENERGY_QUALITY_VALUES
            assert energy.allocation_quality in ALLOCATION_QUALITY_VALUES


# ===========================================================================
# 7. Price alignment: a step function, backward filled
# ===========================================================================


def price_series(rows: list[dict[str, Any]]) -> StitchedSeries:
    return timeline(rows, entity=PRICE, metric="price_eur_per_kwh", unit="EUR/kWh")


class TestAlignPrices:
    """PVPC is hourly: the clock hour's price covers all four of its intervals."""

    def test_hourly_price_applies_to_all_four_intervals(self) -> None:
        """A price observed inside hour H is the price of all four intervals."""
        prices = price_series(price_rows(_T0, {0: 0.151, 1: 0.2}, per_minute=60, step_seconds=1.0))
        grid = grid_times(_T0, _T0 + timedelta(hours=2))
        aligned = align_prices(prices, grid, timezone=MADRID)
        assert [a.value for a in aligned[:4]] == [0.151] * 4
        assert [a.value for a in aligned[4:8]] == [0.2] * 4
        assert all(a.quality == PRICE_MEASURED for a in aligned)

    def test_price_is_never_lagged_by_one_hour(self) -> None:
        """Regression for the backward-fill defect: ``14:59:39`` is not hour 15's price.

        The verified archive samples at ``:39``, so the old rule (last sample at or
        before the interval start) read the *previous* hour's sample for the
        interval beginning at the hour top: replaying it on this exact shape gives
        ``0.10`` (hour 14) for ``[15:00,15:15)`` instead of ``0.90``.  Every
        interval of the hour must take the hour's own price, even though it is
        first observed after the hour began.
        """
        hour_14 = _T0 + timedelta(hours=14)  # 14:00Z = local 15:00 in winter Madrid
        hour_15 = _T0 + timedelta(hours=15)
        rows = [
            price_row(hour_14 + timedelta(seconds=39), 0.10),
            price_row(hour_14 + timedelta(seconds=99), 0.10),
            price_row(hour_15 + timedelta(seconds=39), 0.90),
            price_row(hour_15 + timedelta(seconds=99), 0.90),
        ]
        grid = grid_times(hour_15, hour_15 + timedelta(hours=1))
        aligned = align_prices(price_series(rows), grid, timezone=MADRID)
        assert len(aligned) == 4
        assert [a.value for a in aligned] == [0.90] * 4, (
            "all four intervals of hour 15 carry hour 15's price, not 0.10"
        )
        assert [a.quality for a in aligned] == [PRICE_MEASURED] * 4
        assert all(a.hour_start == hour_15 for a in aligned)
        assert 0.10 not in [a.value for a in aligned], "hour 14 must not leak into hour 15"

    def test_first_sample_of_the_hour_lands_in_that_hour_not_the_previous(self) -> None:
        """A value first observed at ``H:00:39`` belongs to hour H's four intervals."""
        rows = [price_row(_T0 + timedelta(hours=1, seconds=39), 0.05)]
        rows += [price_row(_T0 + timedelta(hours=2, seconds=39), 0.55)]
        aligned = align_prices(
            price_series(rows),
            grid_times(_T0 + timedelta(hours=1), _T0 + timedelta(hours=3)),
            timezone=MADRID,
        )
        assert [a.value for a in aligned[:4]] == [0.05] * 4
        assert [a.value for a in aligned[4:8]] == [0.55] * 4
        # Nothing of hour 1 leaked into hour 0 (which has no sample of its own).
        assert aligned[0].hour_start == _T0 + timedelta(hours=1)

    def test_no_lookahead_beyond_the_hour_itself(self) -> None:
        """An interval never receives a price published in a later hour."""
        prices = price_series([price_row(_T0 + timedelta(hours=1), 0.3)])
        grid = grid_times(_T0, _T0 + timedelta(hours=2))
        aligned = align_prices(prices, grid, timezone=MADRID)
        assert all(a.value is None and a.quality == PRICE_MISSING for a in aligned[:4])
        assert aligned[4].value == pytest.approx(0.3)
        assert aligned[4].quality == PRICE_MEASURED

    def test_sample_exactly_at_hour_start_is_in_force(self) -> None:
        """A sample landing on the hour boundary belongs to the hour beginning there."""
        prices = price_series([price_row(_T0, 0.1), price_row(_T0 + timedelta(hours=1), 0.2)])
        aligned = align_prices(prices, grid_times(_T0, _T0 + timedelta(hours=2)), timezone=MADRID)
        assert [a.value for a in aligned] == [0.1] * 4 + [0.2] * 4

    def test_carried_price_becomes_forward_filled_not_measured(self) -> None:
        """An hour with no sample of its own may only *carry* a fresh value."""
        prices = price_series([price_row(_T0, 0.1)])
        grid = grid_times(_T0, _T0 + timedelta(hours=3))
        aligned = align_prices(prices, grid, timezone=MADRID, staleness_seconds=3600.0)
        assert [a.quality for a in aligned[:4]] == [PRICE_MEASURED] * 4
        # The next hour has no sample: the last known value is carried, and it is
        # 3600 s old at the hour start — still inside the staleness bound.
        assert [a.quality for a in aligned[4:8]] == [PRICE_FORWARD_FILLED] * 4
        assert aligned[4].value == pytest.approx(0.1), "filled with the last known price"
        # Two hours old is outside the bound: missing, never a stale guess.
        assert [a.quality for a in aligned[8:]] == [PRICE_MISSING] * 4
        assert [a.value for a in aligned[8:]] == [None] * 4

    def test_zero_staleness_refuses_any_carry(self) -> None:
        """staleness_seconds=0 admits only values observed inside the hour."""
        prices = price_series([price_row(_T0, 0.1)])
        aligned = align_prices(
            prices,
            grid_times(_T0, _T0 + timedelta(hours=1)),
            timezone=MADRID,
            staleness_seconds=0.0,
        )
        assert [a.quality for a in aligned] == [PRICE_MEASURED] * 4

    def test_negative_prices_are_never_clamped_or_filtered(self) -> None:
        """The dashboard's ``>= 0`` guard is the defect ADR-0003 names."""
        prices = price_series(
            [
                price_row(_T0, -0.153),
                price_row(_T0 + timedelta(hours=1), -0.001),
                price_row(_T0 + timedelta(hours=2), 0.0),
            ]
        )
        aligned = align_prices(prices, grid_times(_T0, _T0 + timedelta(hours=3)), timezone=MADRID)
        assert [a.value for a in aligned] == [-0.153] * 4 + [-0.001] * 4 + [0.0] * 4
        assert all(a.quality == PRICE_MEASURED for a in aligned)

    def test_agreeing_samples_yield_the_observed_value_not_an_average(self) -> None:
        """Repeating the same value every minute is a step function, not noise."""
        rows = [price_row(_T0 + timedelta(seconds=i * 20), 0.9) for i in range(6)]
        aligned = align_prices(
            price_series(rows), grid_times(_T0, _T0 + timedelta(minutes=1)), timezone=MADRID
        )
        assert {a.value for a in aligned} == {0.9}
        assert aligned[0].samples == 6

    def test_missing_series_yields_missing_not_zero(self) -> None:
        grid = grid_times(_T0, _T0 + timedelta(hours=1))
        for absent in (None, price_series([])):
            aligned = align_prices(absent, grid, timezone=MADRID)
            assert [a.value for a in aligned] == [None] * 4
            assert {a.quality for a in aligned} == {PRICE_MISSING}

    def test_rejects_negative_staleness(self) -> None:
        with pytest.raises(BuildError, match="staleness_seconds"):
            align_prices(
                None,
                grid_times(_T0, _T0 + timedelta(hours=1)),
                timezone=MADRID,
                staleness_seconds=-1.0,
            )

    def test_qualities_stay_in_documented_sets(self) -> None:
        rows = price_rows(_T0 + timedelta(minutes=30), {0: 0.1}, per_minute=6, step_seconds=10.0)
        rows += [price_row(_T0 + timedelta(hours=5), 0.4)]
        aligned = align_prices(
            price_series(rows),
            grid_times(_T0, _T0 + timedelta(hours=6)),
            timezone=MADRID,
            staleness_seconds=3600.0,
        )
        assert {a.quality for a in aligned} <= PRICE_QUALITY_VALUES
        qualities = {a.quality for a in aligned}
        assert PRICE_MEASURED in qualities
        assert PRICE_MISSING in qualities
        assert PRICE_FORWARD_FILLED in qualities, (
            "hour 1 carries hour 0's last known value, it is not measured"
        )

    def test_lookahead_is_measured_per_hour_and_reported_exactly(self) -> None:
        """The residual look-ahead is disclosed with the value the data shows.

        A 39 s first observation is *measured* and reported as-is; nothing here
        claims 39 s is typical or that a ~60 s bound holds — the year-wide
        distribution is what decides that (see
        ``TestYearWideLookaheadDistribution``).
        """
        rows = [price_row(_T0 + timedelta(seconds=39 + 60 * i), 0.1) for i in range(3)]
        _aligned, ledger = align_price_hours(
            price_series(rows), grid_times(_T0, _T0 + timedelta(hours=1)), timezone=MADRID
        )
        assert ledger.measured_hours == 1
        assert ledger.max_lookahead_seconds == pytest.approx(39.0)
        offsets = ledger.as_dict()["first_observation_offset_seconds"]
        assert offsets["hours_with_a_sample"] == 1
        assert offsets["median_seconds"] == pytest.approx(39.0)
        assert offsets["max_seconds"] == pytest.approx(39.0)


# ===========================================================================
# 8. Gross load and the battery seam
# ===========================================================================


class TestGrossLoadSeam:
    def test_gross_equals_raw_while_no_battery_exists(self, tmp_path: Path, data_dir: Path) -> None:
        cfg = write_tariff(tmp_path)
        rows = counter_rows(_T0, 121, times=counter_times(_T0, 121, 60.0))
        rows += price_rows(_T0, {0: 0.1, 1: 0.12}, per_minute=6, step_seconds=10.0)
        write_raw_part(data_dir, rows)
        table, manifest = build_rows(
            data_dir, cfg, _T0, _T0 + timedelta(hours=2), lookback=timedelta(minutes=5)
        )
        out = rows_as_dicts(table)
        assert {r["battery_absent"] for r in out} == {True}
        for row in out:
            assert row["gross_kwh"] == row["raw_grid_kwh"]
        # Absence is a recorded *assumption*, never a detection: the registry is
        # empty so presence is unknown, and the default ``battery_absent=True``
        # is what lets gross equate the feed reading.
        params = manifest["params"]
        assert params["battery_absent"] is True
        assert params["battery_detection"] == "unknown"
        assert params["battery_absent_is_assumption"] is True
        assert params["battery_telemetry_entities"] == []

    def test_resolve_gross_load_is_the_identity_without_a_battery(self) -> None:
        assert resolve_gross_load(0.25, battery_active=False) == pytest.approx(0.25)
        assert resolve_gross_load(None, battery_active=False) is None

    def test_resolve_gross_load_refuses_to_guess_with_a_battery(self) -> None:
        """The equality is a property of today's installation, not a general truth."""
        with pytest.raises(NotImplementedError, match="gross-load reconstruction"):
            resolve_gross_load(0.25, battery_active=True)

    def test_no_battery_telemetry_is_registered_yet(self) -> None:
        assert BATTERY_TELEMETRY_ENTITIES == ()

    def test_detect_battery_active_is_none_when_registry_is_empty(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """Absence of an unregistered metric proves nothing — presence is unknown.

        An empty :data:`BATTERY_TELEMETRY_ENTITIES` cannot yield ``False`` (that
        would assert the hardware is absent from ignorance); only a *non-empty*
        registry that saw none of its entities is a real "absent".
        """
        assert detect_battery_active([GRID, PRICE]) is None
        monkeypatch.setattr(
            build_mod, "BATTERY_TELEMETRY_ENTITIES", ("sensor.omnibattery_charge_energy",)
        )
        assert detect_battery_active([GRID, PRICE]) is False
        assert detect_battery_active([GRID, "sensor.omnibattery_charge_energy"]) is True

    def test_battery_absent_false_refuses_when_presence_is_unknown(
        self, tmp_path: Path, data_dir: Path
    ) -> None:
        """Opting out of the absence assumption with nothing detected cannot build.

        gross ≠ raw once a battery may dispatch; with the registry empty the builder
        refuses to invent gross rather than silently equating it.
        """
        cfg = write_tariff(tmp_path)
        write_raw_part(data_dir, counter_rows(_T0, 13, times=aligned_times(_T0, 13, 300.0)))
        with pytest.raises(BuildError, match="battery presence is unknown"):
            build(
                data_dir,
                cfg,
                _T0,
                _T0 + timedelta(hours=1),
                lookback=timedelta(minutes=5),
                battery_absent=False,
            )

    def test_battery_absent_false_builds_when_absence_is_confirmed_by_registry(
        self, tmp_path: Path, data_dir: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """A non-empty registry that saw no battery is a *fact*, so the build proceeds."""
        monkeypatch.setattr(
            build_mod, "BATTERY_TELEMETRY_ENTITIES", ("sensor.omnibattery_charge_energy",)
        )
        cfg = write_tariff(tmp_path)
        write_raw_part(data_dir, counter_rows(_T0, 13, times=aligned_times(_T0, 13, 300.0)))
        table, manifest = build_rows(
            data_dir,
            cfg,
            _T0,
            _T0 + timedelta(hours=1),
            lookback=timedelta(minutes=5),
            battery_absent=False,
        )
        assert manifest["params"]["battery_detection"] == "absent"
        assert manifest["params"]["battery_absent_is_assumption"] is False
        assert {r["battery_absent"] for r in rows_as_dicts(table)} == {True}

    def test_build_refuses_loudly_once_battery_telemetry_exists(
        self, tmp_path: Path, data_dir: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """A silent ``gross == raw`` under an active battery is the failure mode."""
        monkeypatch.setattr(
            build_mod, "BATTERY_TELEMETRY_ENTITIES", ("sensor.omnibattery_charge_energy",)
        )
        cfg = write_tariff(tmp_path)
        rows = counter_rows(_T0, 13, times=aligned_times(_T0, 13, 300.0))
        rows += [
            raw_row("sensor.omnibattery_charge_energy", "energy_kwh", "kWh", _T0, 0.0),
            raw_row(
                "sensor.omnibattery_charge_energy",
                "energy_kwh",
                "kWh",
                _T0 + timedelta(minutes=5),
                0.1,
            ),
        ]
        write_raw_part(data_dir, rows)
        with pytest.raises(NotImplementedError, match="ADR-0003"):
            build(data_dir, cfg, _T0, _T0 + timedelta(hours=1), lookback=timedelta(minutes=5))


# ===========================================================================
# 9. Nested submeters, export counters, power, dead duplicates
# ===========================================================================


class TestNestedSubmetersAndDuplicates:
    def test_constants_declare_the_metering_basis(self) -> None:
        assert GRID_COUNTER_ENTITIES == (GRID,)
        assert PRICE_ENTITIES == (PRICE,)
        assert {CH2, POWER, RETURNED} <= set(NESTED_SUBMETER_ENTITIES)

    def test_nested_submeters_do_not_inflate_gross_load(
        self, tmp_path: Path, data_dir: Path
    ) -> None:
        """servers/ch2 are inside ch1: adding them would double-count the home."""
        cfg = write_tariff(tmp_path)
        # 24 samples at 300 s cover [T0, T0+1h57m30s); the 25th would land exactly on
        # T0+2h and be excluded by the half-open read window.
        grid = counter_rows(
            _T0, 24, first=1000.0, per_sample=0.2, times=aligned_times(_T0, 24, 300.0)
        )
        nested = [
            # Channel 2 (aerotherm branch) carries a *bigger* counter than the feed.
            *ch2_row_times(_T0, 24, first=4000.0, per_sample=0.8),
            *power_row_times(_T0, 24, watts=3000.0),
            *returned_row_times(_T0, 24, first=10.0, per_sample=0.5),
        ]
        write_raw_part(
            data_dir, grid + nested + price_rows(_T0, {0: 0.1}, per_minute=6, step_seconds=10.0)
        )
        table, _manifest = build_rows(
            data_dir, cfg, _T0, _T0 + timedelta(hours=2), lookback=timedelta(minutes=5)
        )
        total = sum(r["gross_kwh"] for r in rows_as_dicts(table) if r["gross_kwh"] is not None)
        assert total == pytest.approx(grid[-1]["value"] - grid[0]["value"]), "ch1 delta only"
        assert total == pytest.approx(4.6)

    def test_power_is_a_crosscheck_and_never_integrated(
        self, tmp_path: Path, data_dir: Path
    ) -> None:
        """3 kW of reported power for 10 minutes is 0.5 kWh — the builder must ignore it."""
        cfg = write_tariff(tmp_path)
        grid = counter_rows(_T0, 5, first=10.0, per_sample=0.01, times=aligned_times(_T0, 5, 300.0))
        power = power_row_times(_T0, 5, watts=3000.0)
        write_raw_part(data_dir, grid + power)
        table, _manifest = build_rows(
            data_dir, cfg, _T0, _T0 + timedelta(hours=1, minutes=30), lookback=timedelta(minutes=5)
        )
        values = [r["raw_grid_kwh"] for r in rows_as_dicts(table) if r["raw_grid_kwh"] is not None]
        assert sum(values) == pytest.approx(0.04)
        # 3 spans complete inside the first 15-min interval (at 5, 10, 15 min) → 0.03;
        # the fourth span completes in the next interval → 0.01.  Neither is anywhere
        # near the 0.5 kWh that integrating 3 kW for 10 minutes would yield.
        assert sorted(values) == [pytest.approx(0.01), pytest.approx(0.03)]

    def test_entity_audit_labels_every_series_with_a_reason(
        self, tmp_path: Path, data_dir: Path
    ) -> None:
        cfg = write_tariff(tmp_path)
        rows = counter_rows(_T0, 5, times=aligned_times(_T0, 5, 300.0))
        rows += price_rows(_T0, {0: 0.1}, per_minute=6, step_seconds=10.0)
        rows += ch2_row_times(_T0, 5, first=1.0, per_sample=0.1)
        rows += dead_price_rows(_T0, 5, value=0.999)
        write_raw_part(data_dir, rows)
        _table, manifest = build_rows(
            data_dir, cfg, _T0, _T0 + timedelta(hours=1, minutes=30), lookback=timedelta(minutes=5)
        )
        audit = {str(item["entity"]): item for item in manifest["entity_audit"]}
        assert audit[GRID]["used_for"] == "grid"
        assert audit[PRICE]["used_for"] == "price"
        assert audit[CH2]["used_for"] == "not_used"
        assert audit[DEAD_PRICE]["used_for"] == "not_used"
        # Both paths of the exclusion disclosure in one fixture: CH2 and the dead
        # duplicate were really present and really dropped, while POWER and the
        # export channel are configured exclusions this build never saw.
        params = manifest["params"]
        assert params["nested_submeters_excluded_from_gross"] == [CH2]
        assert params["dead_duplicates_excluded"] == [DEAD_PRICE]
        absent = params["configured_but_absent_from_inputs"]
        assert sorted(str(item["entity"]) for item in absent) == sorted([POWER, RETURNED])
        assert all(item["status"] == "never_observed_in_any_input" for item in absent), (
            "absent entities must never be recorded as exclusions"
        )
        exclusions = manifest["input_exclusions"]
        dropped = {str(item["entity"]) for item in exclusions["observed_and_dropped"]}
        assert dropped == {CH2, DEAD_PRICE}
        assert exclusions["entities_observed_in_inputs"] == sorted([GRID, PRICE, CH2, DEAD_PRICE])

    def test_dead_duplicate_price_never_merges_into_the_price_series(
        self, tmp_path: Path, data_dir: Path
    ) -> None:
        """``sensor.esios_pvpc_2`` has 11 lifetime samples and must not leak in."""
        cfg = write_tariff(tmp_path)
        grid = counter_rows(_T0, 13, times=aligned_times(_T0, 13, 300.0))
        real = [price_row(_T0 + timedelta(minutes=5 * i), 0.1) for i in range(13)]
        # The dead duplicate reports a *different* value at the same instants: had
        # it merged, this would either overlap-raise or corrupt the price column.
        dead = dead_price_rows(_T0, 13, value=12.34)
        write_raw_part(data_dir, grid + real + dead)
        table, _manifest = build_rows(
            data_dir, cfg, _T0, _T0 + timedelta(hours=1), lookback=timedelta(minutes=5)
        )
        assert {r["price_eur_per_kwh"] for r in rows_as_dicts(table)} == {0.1}

    def test_dead_duplicate_is_not_stitched_as_a_second_price_era(
        self, data_dir: Path, tmp_path: Path
    ) -> None:
        write_tariff(tmp_path)
        rows = counter_rows(_T0, 5, times=aligned_times(_T0, 5, 300.0))
        rows += [price_row(_T0, 0.1), price_row(_T0 + timedelta(minutes=30), 0.2)]
        rows += dead_price_rows(_T0, 3, value=9.9)
        write_raw_part(data_dir, rows)
        inputs = load_raw_inputs(
            data_dir,
            entities=[GRID, PRICE],
            window_start=_T0,
            window_end=_T0 + timedelta(hours=1),
            lookback=timedelta(minutes=5),
        )
        assert set(inputs.series) == {(GRID, "energy_kwh"), (PRICE, "price_eur_per_kwh")}
        assert inputs.timeline(PRICE).values == (0.1, 0.2)


def ch2_row_times(
    start: datetime, count: int, *, first: float, per_sample: float
) -> list[dict[str, Any]]:
    return [
        ch2_row(start + timedelta(seconds=300 * i), first + per_sample * i) for i in range(count)
    ]


def power_row_times(start: datetime, count: int, *, watts: float) -> list[dict[str, Any]]:
    return [power_row(start + timedelta(seconds=300 * i), watts) for i in range(count)]


def returned_row_times(
    start: datetime, count: int, *, first: float, per_sample: float
) -> list[dict[str, Any]]:
    return [
        returned_row(start + timedelta(seconds=300 * i), first + per_sample * i)
        for i in range(count)
    ]


def dead_price_rows(start: datetime, count: int, *, value: float) -> list[dict[str, Any]]:
    return [
        raw_row(
            DEAD_PRICE,
            "price_eur_per_kwh",
            "EUR/kWh",
            start + timedelta(seconds=300 * i),
            value,
            metric_id="sensor_esios_pvpc_2",
        )
        for i in range(count)
    ]


# ===========================================================================
# 10. Reading the raw store
# ===========================================================================


class TestLoadRawInputs:
    def test_no_parts_fails_loudly(self, data_dir: Path) -> None:
        with pytest.raises(BuildError, match="no raw parts"):
            load_raw_inputs(
                data_dir, entities=[GRID], window_start=_T0, window_end=_T0 + timedelta(hours=1)
            )

    def test_part_with_a_foreign_schema_is_rejected(self, data_dir: Path) -> None:
        path = parts_dir(data_dir, "prometheus") / "bad.parquet"
        path.parent.mkdir(parents=True, exist_ok=True)
        bad = pa.table(
            {
                "entity": pa.array(["sensor.x"]),
                "metric": pa.array(["energy_kwh"]),
                "unit": pa.array(["kWh"]),
                "series_key": pa.array(["k"]),
                # ts stored as raw epoch integers: not the contract.
                "ts": pa.array([1767225600000000], pa.int64()),
                "value": pa.array([1.0]),
                "scrape_instance": pa.array(["i"]),
                "extraction_id": pa.array(["e"]),
                "labels_json": pa.array(["{}"]),
            }
        )
        pq.write_table(bad, path)
        # The bad part must be *referenced* by a manifest to be read at all: an
        # orphan is ignored, so an unreferenced bad file is not the failure the
        # builder guards. The window is set to overlap the read so it is demanded.
        _write_manifest_for_part(
            data_dir,
            name="bad.parquet",
            sha256=hashlib.sha256(path.read_bytes()).hexdigest(),
            rows=bad.num_rows,
            chunk_start=_T0 - timedelta(minutes=1),
            chunk_end=_T0 + timedelta(hours=1),
            entities=["sensor.x"],
        )
        with pytest.raises(BuildError, match="does not match RAW_SAMPLE"):
            load_raw_inputs(
                data_dir, entities=[GRID], window_start=_T0, window_end=_T0 + timedelta(hours=1)
            )

    def test_null_in_a_non_nullable_column_is_rejected(self, data_dir: Path) -> None:
        """PyArrow refuses to write a table with a null in a non-nullable field.

        The builder's schema check is defensive: it can only fire if a malformed
        part is produced by some other tool.  We verify the schema-mismatch path
        (which is what actually happens when you try to write a nullable column).
        """
        rows = counter_rows(_T0, 3, times=aligned_times(_T0, 3, 300.0))
        table = to_raw_table(rows)
        # Make the field nullable → schema mismatch.
        broken = table.set_column(
            table.schema.get_field_index("value"),
            pa.field("value", pa.float64(), nullable=True),
            pa.array([rows[0]["value"], None, rows[2]["value"]], pa.float64()),
        )
        _publish_prom_part(
            data_dir,
            broken,
            "nulls.parquet",
            entities=[GRID],
            chunk=(_T0 - timedelta(minutes=1), _T0 + timedelta(hours=1)),
        )
        with pytest.raises(BuildError, match="does not match RAW_SAMPLE"):
            load_raw_inputs(
                data_dir,
                entities=[GRID],
                window_start=_T0,
                window_end=_T0 + timedelta(hours=1),
                lookback=timedelta(minutes=5),
            )

    def test_window_and_lookback_limit_the_timeline(self, data_dir: Path) -> None:
        early = counter_rows(
            _T0 - timedelta(days=2), 10, times=aligned_times(_T0 - timedelta(days=2), 10, 300.0)
        )
        inside = counter_rows(_T0, 10, first=14612.06, times=aligned_times(_T0, 10, 300.0))
        late = counter_rows(
            _T0 + timedelta(hours=2),
            10,
            first=14612.16,
            times=aligned_times(_T0 + timedelta(hours=2), 10, 300.0),
        )
        write_raw_part(data_dir, early + inside + late)
        inputs = load_raw_inputs(
            data_dir,
            entities=[GRID],
            window_start=_T0,
            window_end=_T0 + timedelta(hours=1),
            lookback=timedelta(minutes=30),
        )
        series = inputs.timeline(GRID)
        assert series.times[0] >= _T0 - timedelta(minutes=30)
        assert series.times[-1] < _T0 + timedelta(hours=1)
        assert len(series) == 10
        # the audit still describes the whole archive, not just the window
        audit = {str(item["entity"]): item for item in inputs.entity_audit}
        assert audit[GRID]["samples"] == 30

    def test_eras_can_arrive_in_separate_parts(self, data_dir: Path) -> None:
        old = counter_rows(_T0, 6, job=JOB_OLD, times=aligned_times(_T0, 6, 300.0))
        new = counter_rows(
            _T0 + timedelta(minutes=30),
            6,
            first=14612.024,
            times=aligned_times(_T0 + timedelta(minutes=30), 6, 300.0),
        )
        write_raw_part(data_dir, old, "part-a.parquet")
        write_raw_part(data_dir, new, "part-b.parquet")
        inputs = load_raw_inputs(
            data_dir,
            entities=[GRID],
            window_start=_T0,
            window_end=_T0 + timedelta(hours=1),
            lookback=timedelta(minutes=5),
        )
        series = inputs.timeline(GRID)
        assert len(series) == 12
        assert len(series.era_boundaries) == 1
        assert len(inputs.part_hashes) == 2

    def test_overlapping_extraction_windows_are_idempotent(self, data_dir: Path) -> None:
        rows = counter_rows(_T0, 8, times=aligned_times(_T0, 8, 300.0))
        write_raw_part(data_dir, rows, "part-a.parquet")
        write_raw_part(data_dir, rows[:4], "part-b.parquet")  # re-extracted overlap
        inputs = load_raw_inputs(
            data_dir,
            entities=[GRID],
            window_start=_T0,
            window_end=_T0 + timedelta(hours=1),
            lookback=timedelta(minutes=5),
        )
        series = inputs.timeline(GRID)
        assert len(series) == 8
        assert series.duplicates_dropped == 4

    def test_overlap_between_parts_raises(self, data_dir: Path) -> None:
        at = _T0 + timedelta(minutes=10)
        write_raw_part(data_dir, [grid_row(at, 1.0, job=JOB_OLD)], "part-a.parquet")
        write_raw_part(data_dir, [grid_row(at, 1.0, job=JOB_NEW)], "part-b.parquet")
        with pytest.raises(OverlappingSeriesError):
            load_raw_inputs(
                data_dir,
                entities=[GRID],
                window_start=_T0,
                window_end=_T0 + timedelta(hours=1),
                lookback=timedelta(minutes=5),
            )

    def test_part_hashes_digest_content(self, data_dir: Path) -> None:
        rows = counter_rows(_T0, 5, times=aligned_times(_T0, 5, 300.0))
        path = write_raw_part(data_dir, rows)
        inputs = load_raw_inputs(
            data_dir,
            entities=[GRID],
            window_start=_T0,
            window_end=_T0 + timedelta(hours=1),
            lookback=timedelta(minutes=5),
        )
        assert inputs.part_hashes == [
            {"part": path.name, "sha256": hashlib.sha256(path.read_bytes()).hexdigest(), "rows": 5}
        ]
        assert inputs.rows_read == 5

    def test_unwanted_entities_are_not_loaded_as_timelines(self, data_dir: Path) -> None:
        rows = counter_rows(_T0, 5, times=aligned_times(_T0, 5, 300.0)) + ch2_row_times(
            _T0, 5, first=1.0, per_sample=0.1
        )
        write_raw_part(data_dir, rows)
        inputs = load_raw_inputs(
            data_dir,
            entities=[GRID],
            window_start=_T0,
            window_end=_T0 + timedelta(hours=1),
            lookback=timedelta(minutes=5),
        )
        assert list(inputs.series) == [(GRID, "energy_kwh")]

    def test_no_entities_still_audits_the_archive(self, data_dir: Path) -> None:
        write_raw_part(data_dir, counter_rows(_T0, 5, times=aligned_times(_T0, 5, 300.0)))
        inputs = load_raw_inputs(
            data_dir,
            entities=[],
            window_start=_T0,
            window_end=_T0 + timedelta(hours=1),
            lookback=timedelta(minutes=5),
        )
        assert inputs.series == {}
        assert inputs.entity_audit and inputs.entity_audit[0]["samples"] == 5

    def test_timeline_helper_returns_none_for_unknown_entity(self, data_dir: Path) -> None:
        write_raw_part(data_dir, counter_rows(_T0, 5, times=aligned_times(_T0, 5, 300.0)))
        inputs = load_raw_inputs(
            data_dir,
            entities=[GRID],
            window_start=_T0,
            window_end=_T0 + timedelta(hours=1),
            lookback=timedelta(minutes=5),
        )
        assert inputs.timeline("sensor.nothere") is None


# ===========================================================================
# 10b. Manifest-driven input integrity: resolve coverage from success manifests
# ===========================================================================


class TestLoadRawInputIntegrity:
    """A part is trusted only through the manifest that references it.

    Globbing the part directory lets a failed extraction's orphan leak into the
    build; these tests pin that coverage is resolved from ``manifests/*.json`` and
    that every referenced part is checked by **full** SHA-256, row count, schema and
    path containment before a single row is read.
    """

    def _load(self, data_dir: Path) -> build_mod.RawInputs:
        return load_raw_inputs(
            data_dir,
            entities=[GRID],
            window_start=_T0,
            window_end=_T0 + timedelta(hours=1),
            lookback=timedelta(minutes=5),
        )

    def test_failed_extraction_orphan_is_ignored(self, data_dir: Path) -> None:
        """A part left behind by a run that never wrote a manifest is not an input."""
        write_raw_part(data_dir, counter_rows(_T0, 10, times=aligned_times(_T0, 10, 300.0)))
        orphan = parts_dir(data_dir, "prometheus") / "orphan-of-failed-run.parquet"
        orphan.write_bytes(b"this is not a parquet file")
        inputs = self._load(data_dir)
        assert [p["part"] for p in inputs.part_hashes] == ["samples-a.parquet"]
        assert all(item["part"] != "orphan-of-failed-run.parquet" for item in inputs.lineage)

    def test_referenced_but_missing_part_raises(self, data_dir: Path) -> None:
        path = write_raw_part(data_dir, counter_rows(_T0, 10, times=aligned_times(_T0, 10, 300.0)))
        path.unlink()  # the manifest still names it, and its window overlaps the read
        with pytest.raises(BuildError, match="missing|unreadable"):
            self._load(data_dir)

    def test_referenced_part_corrupt_sha_raises(self, data_dir: Path) -> None:
        """The 8-char filename hash is not the integrity check — the full SHA is."""
        _publish_prom_part(
            data_dir,
            to_raw_table(counter_rows(_T0, 5, times=aligned_times(_T0, 5, 300.0))),
            "samples-a.parquet",
            entities=[GRID],
            sha_override="0" * 64,
        )
        with pytest.raises(BuildError, match="sha256 mismatch"):
            self._load(data_dir)

    def test_referenced_part_row_count_mismatch_raises(self, data_dir: Path) -> None:
        _publish_prom_part(
            data_dir,
            to_raw_table(counter_rows(_T0, 5, times=aligned_times(_T0, 5, 300.0))),
            "samples-a.parquet",
            entities=[GRID],
            rows_override=999,
        )
        with pytest.raises(BuildError, match="row count mismatch"):
            self._load(data_dir)

    def test_path_sandbox_traversal_is_rejected(self, data_dir: Path) -> None:
        write_raw_part(data_dir, counter_rows(_T0, 5, times=aligned_times(_T0, 5, 300.0)))
        _write_manifest_for_part(
            data_dir,
            name="escape.parquet",
            sha256="0" * 64,
            rows=1,
            chunk_start=_T0,
            chunk_end=_T0 + timedelta(minutes=10),
            entities=[GRID],
            extraction_id="traversal",
            path_override="../../outside.parquet",
        )
        with pytest.raises(BuildError, match="traversal"):
            load_raw_inputs(
                data_dir,
                entities=[GRID],
                window_start=_T0 + timedelta(minutes=1),
                window_end=_T0 + timedelta(hours=2),
                lookback=timedelta(0),
            )

    def test_absolute_part_path_is_rejected(self, data_dir: Path) -> None:
        _write_manifest_for_part(
            data_dir,
            name="abs.parquet",
            sha256="0" * 64,
            rows=1,
            chunk_start=_T0,
            chunk_end=_T0 + timedelta(minutes=10),
            entities=[GRID],
            extraction_id="absolute",
            path_override="/etc/passwd",
        )
        with pytest.raises(BuildError, match="absolute"):
            self._load(data_dir)

    def test_two_identical_manifests_are_logically_deduplicated(self, data_dir: Path) -> None:
        """A re-extraction under a new id is the *same* logical samples, not a new era.

        The parts differ only in the ``extraction_id`` column, so they hash
        differently and both are read; the stitcher collapses identical
        ``(series_key, ts, value)`` triples and sees no era boundary.
        """
        eid_a = "a" * 8 + "-1111-1111-1111-111111111111"
        eid_b = "b" * 8 + "-2222-2222-2222-222222222222"
        rows_a = counter_rows(_T0, 8, times=aligned_times(_T0, 8, 300.0), extraction_id=eid_a)
        rows_b = counter_rows(_T0, 8, times=aligned_times(_T0, 8, 300.0), extraction_id=eid_b)
        _publish_prom_part(data_dir, to_raw_table(rows_a), "run-a.parquet", entities=[GRID])
        _publish_prom_part(data_dir, to_raw_table(rows_b), "run-b.parquet", entities=[GRID])
        inputs = self._load(data_dir)
        assert len(inputs.part_hashes) == 2 and len(inputs.lineage) == 2
        series = inputs.timeline(GRID)
        assert len(series) == 8
        assert series.duplicates_dropped == 8
        assert series.era_boundaries == ()

    def test_ha_history_manifest_is_not_a_prometheus_input(self, data_dir: Path) -> None:
        """A valid part is still ignored when its manifest says ``ha_history``."""
        _publish_prom_part(
            data_dir,
            to_raw_table(counter_rows(_T0, 10, times=aligned_times(_T0, 10, 300.0))),
            "ha.parquet",
            entities=[GRID],
            source="ha_history",
        )
        with pytest.raises(BuildError, match="no raw parts"):
            self._load(data_dir)

    def test_incomplete_manifest_is_not_an_input(self, data_dir: Path) -> None:
        """A manifest without ``finished_at`` records no successful coverage."""
        _publish_prom_part(
            data_dir,
            to_raw_table(counter_rows(_T0, 10, times=aligned_times(_T0, 10, 300.0))),
            "running.parquet",
            entities=[GRID],
            finished_at=None,
        )
        with pytest.raises(BuildError, match="no raw parts"):
            self._load(data_dir)


# ===========================================================================
# 11. Deterministic dataset identity
# ===========================================================================

_ID_BASE: dict[str, Any] = {
    "part_hashes": [{"part": "b.parquet", "sha256": "bb"}, {"part": "a.parquet", "sha256": "aa"}],
    "config_hash": "cfg" * 8,
    "code_version": "abcdef0",
    "schema_version": SCHEMA_VERSION,
    "window_start": _T0,
    "window_end": _T0 + timedelta(hours=1),
    "params": {"interval_seconds": INTERVAL_SECONDS},
}


class TestDatasetId:
    def test_identical_inputs_identical_id(self) -> None:
        first = compute_dataset_id(**_ID_BASE)
        second = compute_dataset_id(**copy.deepcopy(_ID_BASE))
        assert first == second
        assert re.fullmatch(r"[0-9a-f]{64}", first)

    def test_part_order_does_not_matter(self) -> None:
        shuffled = copy.deepcopy(_ID_BASE)
        shuffled["part_hashes"] = list(reversed(shuffled["part_hashes"]))
        assert compute_dataset_id(**shuffled) == compute_dataset_id(**_ID_BASE)

    @pytest.mark.parametrize(
        "mutation",
        [
            pytest.param(lambda d: d.update(config_hash="ff" * 32), id="config"),
            pytest.param(lambda d: d.update(code_version="other"), id="code"),
            pytest.param(lambda d: d.update(schema_version=SCHEMA_VERSION + 1), id="schema"),
            pytest.param(
                lambda d: d.update(window_start=_T0 + timedelta(hours=1)), id="window-start"
            ),
            pytest.param(lambda d: d.update(window_end=_T0 + timedelta(hours=2)), id="window-end"),
            pytest.param(lambda d: d["params"].update(gap_threshold_seconds=123.0), id="params"),
            pytest.param(lambda d: d["part_hashes"][0].update(sha256="changed"), id="part-content"),
            pytest.param(
                lambda d: d["part_hashes"].append({"part": "c.parquet", "sha256": "cc"}),
                id="extra-part",
            ),
        ],
    )
    def test_any_input_change_makes_a_new_id(self, mutation) -> None:
        changed = copy.deepcopy(_ID_BASE)
        mutation(changed)
        assert compute_dataset_id(**changed) != compute_dataset_id(**_ID_BASE)

    def test_id_is_not_derived_from_wall_clock(self) -> None:
        """Repeated calls (seconds apart) return the same value."""
        assert compute_dataset_id(**_ID_BASE) == compute_dataset_id(**copy.deepcopy(_ID_BASE))

    def test_code_version_is_reported(self) -> None:
        version = code_version()
        assert version
        assert version == code_version()

    def test_build_stamps_every_row_and_the_directory(self, tmp_path: Path, data_dir: Path) -> None:
        cfg = write_tariff(tmp_path)
        write_raw_part(data_dir, counter_rows(_T0, 13, times=aligned_times(_T0, 13, 300.0)))
        directory = build(
            data_dir, cfg, _T0, _T0 + timedelta(hours=1), lookback=timedelta(minutes=5)
        )
        table = pq.read_table(sorted((directory / "parts").glob("*.parquet"))[0])
        ids = set(values_for(table, "dataset_id"))
        assert len(ids) == 1
        dataset_id = ids.pop()
        assert directory.name == dataset_id
        assert directory.parent.name == f"v{SCHEMA_VERSION}"
        manifest = json.loads((directory / "build_manifest.json").read_text())
        assert manifest["dataset_id"] == dataset_id

    def test_changed_window_changes_the_id(self, tmp_path: Path, data_dir: Path) -> None:
        cfg = write_tariff(tmp_path)
        write_raw_part(data_dir, counter_rows(_T0, 25, times=aligned_times(_T0, 25, 300.0)))
        first = build(data_dir, cfg, _T0, _T0 + timedelta(hours=1), lookback=timedelta(minutes=5))
        second = build(data_dir, cfg, _T0, _T0 + timedelta(hours=2), lookback=timedelta(minutes=5))
        assert first != second
        assert first.name != second.name

    def test_changed_raw_content_changes_the_id(self, tmp_path: Path, data_dir: Path) -> None:
        cfg = write_tariff(tmp_path)
        write_raw_part(
            data_dir, counter_rows(_T0, 13, times=aligned_times(_T0, 13, 300.0)), "part-a.parquet"
        )
        first = build(data_dir, cfg, _T0, _T0 + timedelta(hours=1), lookback=timedelta(minutes=5))
        # A second part that covers a *later* period: the first window's timeline is
        # unchanged, but the archive's part list is not — so the id must differ.
        write_raw_part(
            data_dir,
            counter_rows(
                _T0 + timedelta(hours=1),
                13,
                first=14612.048,
                times=aligned_times(_T0 + timedelta(hours=1), 13, 300.0),
            ),
            "part-b.parquet",
        )
        second = build(data_dir, cfg, _T0, _T0 + timedelta(hours=1), lookback=timedelta(minutes=5))
        assert first.name != second.name


# ===========================================================================
# 12. Deterministic output
# ===========================================================================


class TestDeterminism:
    def _rows(self) -> list[dict[str, Any]]:
        grid = counter_rows(_T0, 61, times=counter_times(_T0, 61, 60.0))
        prices = price_rows(_T0, {0: 0.1234, 1: -0.02}, per_minute=6, step_seconds=10.0)
        return grid + prices

    def test_two_builds_are_byte_identical(self, tmp_path: Path, data_dir: Path) -> None:
        cfg = write_tariff(tmp_path)
        write_raw_part(data_dir, self._rows())
        args = (_T0, _T0 + timedelta(hours=2))
        first = build(data_dir, cfg, *args, lookback=timedelta(minutes=5))
        second = build(data_dir, cfg, *args, lookback=timedelta(minutes=5))
        assert first == second
        parts = sorted((first / "parts").glob("*.parquet"))
        assert len(parts) == 1
        assert len(sorted((second / "parts").glob("*.parquet"))) == 1
        assert parts[0].read_bytes() == sorted((second / "parts").glob("*.parquet"))[0].read_bytes()

    def test_dataset_directory_holds_no_wall_clock(self, tmp_path: Path, data_dir: Path) -> None:
        cfg = write_tariff(tmp_path)
        write_raw_part(data_dir, self._rows())
        directory = build(
            data_dir, cfg, _T0, _T0 + timedelta(hours=2), lookback=timedelta(minutes=5)
        )
        manifest = json.loads((directory / "build_manifest.json").read_text())
        assert "started_at" not in manifest
        assert "finished_at" not in manifest
        assert "timestamp" not in {key for key in manifest}
        payload = json.dumps(manifest, sort_keys=True)
        assert not any(key in payload for key in ("started_at", "finished_at"))

    def test_manifest_bytes_are_identical_across_runs(self, tmp_path: Path, data_dir: Path) -> None:
        cfg = write_tariff(tmp_path)
        write_raw_part(data_dir, self._rows())
        first = build(data_dir, cfg, _T0, _T0 + timedelta(hours=2), lookback=timedelta(minutes=5))
        text = (first / "build_manifest.json").read_bytes()
        second = build(data_dir, cfg, _T0, _T0 + timedelta(hours=2), lookback=timedelta(minutes=5))
        assert (second / "build_manifest.json").read_bytes() == text

    def test_part_filename_carries_the_content_hash(self, tmp_path: Path, data_dir: Path) -> None:
        cfg = write_tariff(tmp_path)
        write_raw_part(data_dir, self._rows())
        directory = build(
            data_dir, cfg, _T0, _T0 + timedelta(hours=2), lookback=timedelta(minutes=5)
        )
        (part,) = list((directory / "parts").glob("*.parquet"))
        digest = hashlib.sha256(part.read_bytes()).hexdigest()
        assert part.name.startswith(f"{build_mod.PART_KIND}-20260105T000000Z-20260105T020000Z-")
        assert part.name.endswith(f"{digest[:8]}.parquet")
        manifest = json.loads((directory / "build_manifest.json").read_text())
        assert manifest["part_files"][0]["sha256"] == digest
        assert manifest["part_files"][0]["rows"] == pq.read_table(part).num_rows

    def test_a_different_data_directory_reproduces_the_same_bytes(
        self, tmp_path: Path, data_dir: Path
    ) -> None:
        cfg = write_tariff(tmp_path)
        rows = self._rows()
        write_raw_part(data_dir, rows)
        other = tmp_path / "other-data"
        write_raw_part(other, rows)
        first = build(data_dir, cfg, _T0, _T0 + timedelta(hours=2), lookback=timedelta(minutes=5))
        second = build(other, cfg, _T0, _T0 + timedelta(hours=2), lookback=timedelta(minutes=5))
        assert first.name == second.name
        left = sorted((first / "parts").glob("*.parquet"))[0].read_bytes()
        right = sorted((second / "parts").glob("*.parquet"))[0].read_bytes()
        assert left == right


# ===========================================================================
# 13. The output contract
# ===========================================================================


class TestOutputContract:
    @pytest.fixture
    def built(self, tmp_path: Path, data_dir: Path) -> tuple[pa.Table, dict[str, Any]]:
        cfg = write_tariff(tmp_path)
        grid = counter_rows(_T0, 61, times=counter_times(_T0, 61, 60.0))
        prices = price_rows(_T0, {0: 0.1, 1: -0.05}, per_minute=6, step_seconds=10.0)
        write_raw_part(data_dir, grid + prices)
        return build_rows(
            data_dir, cfg, _T0, _T0 + timedelta(hours=2), lookback=timedelta(minutes=5)
        )

    def test_table_matches_analysis_interval(self, built: tuple[pa.Table, dict[str, Any]]) -> None:
        table, _manifest = built
        validate_table("ANALYSIS_INTERVAL", table)  # raises TypeError/ValueError on drift
        assert table.schema == ANALYSIS_INTERVAL

    def test_documented_columns_are_present(self, built: tuple[pa.Table, dict[str, Any]]) -> None:
        table, _manifest = built
        assert table.column_names == ANALYSIS_INTERVAL.names

    def test_required_columns_have_no_nulls(self, built: tuple[pa.Table, dict[str, Any]]) -> None:
        table, _manifest = built
        for field_def in ANALYSIS_INTERVAL:
            if not field_def.nullable:
                assert table.column(field_def.name).null_count == 0, field_def.name

    def test_nullable_columns_are_the_declared_four(
        self, built: tuple[pa.Table, dict[str, Any]]
    ) -> None:
        table, _manifest = built
        nullable = {f.name for f in ANALYSIS_INTERVAL if f.nullable}
        assert nullable == {"dst_transition", "raw_grid_kwh", "gross_kwh", "price_eur_per_kwh"}

    def test_categorical_values_are_the_documented_sets(
        self, built: tuple[pa.Table, dict[str, Any]]
    ) -> None:
        table, _manifest = built
        assert set(values_for(table, "energy_quality")) <= ENERGY_QUALITY_VALUES
        assert set(values_for(table, "allocation_quality")) <= ALLOCATION_QUALITY_VALUES
        assert set(values_for(table, "price_quality")) <= PRICE_QUALITY_VALUES
        assert set(values_for(table, "dst_transition")) <= DST_VALUES | {None}

    def test_builder_constants_are_documented_values(self) -> None:
        for value in (
            build_mod.ENERGY_MEASURED,
            build_mod.ENERGY_GAP_PARTIAL,
            build_mod.ENERGY_GAP_TOTAL_UNKNOWN,
            build_mod.ENERGY_COUNTER_RESET_BRIDGED,
        ):
            assert value in ENERGY_QUALITY_VALUES
        assert build_mod.ALLOCATION_EXACT in ALLOCATION_QUALITY_VALUES
        assert build_mod.ALLOCATION_ESTIMATED_BOUNDARY in ALLOCATION_QUALITY_VALUES
        assert build_mod.ALLOCATION_UNKNOWN_GAP in ALLOCATION_QUALITY_VALUES
        for value in (
            build_mod.PRICE_MEASURED,
            build_mod.PRICE_FORWARD_FILLED,
            build_mod.PRICE_MISSING,
            build_mod.PRICE_AMBIGUOUS,
        ):
            assert value in PRICE_QUALITY_VALUES
        assert build_mod.DST_SPRING_FORWARD in DST_VALUES
        assert build_mod.DST_FALL_BACK in DST_VALUES

    def test_quality_literals_match_the_benchmark_vocabulary(self) -> None:
        """``benchmark.py`` is the consumer: its literal sets must agree exactly.

        The two defects were only fixable if the new strings are the ones the
        benchmark's usable-mask already recognises, so this pins the shared
        vocabulary from the builder side.
        """
        from energy_baseline import benchmark

        assert ALLOCATION_EXACT in benchmark.USABLE_ALLOCATION_QUALITY
        assert ALLOCATION_ESTIMATED_BOUNDARY in benchmark.USABLE_ALLOCATION_QUALITY
        assert ALLOCATION_UNKNOWN_GAP in benchmark.NEVER_ALLOCABLE
        assert ALLOCATION_ESTIMATED_BOUNDARY not in benchmark.NEVER_ALLOCABLE
        assert PRICE_MEASURED in benchmark.MEASURED_PRICE_QUALITY
        assert PRICE_FORWARD_FILLED in benchmark.RECONSTRUCTED_PRICE_QUALITY
        assert PRICE_MISSING in benchmark.NEVER_PRICE
        assert PRICE_AMBIGUOUS in benchmark.NEVER_PRICE, "a disputed hour is never benchmarkable"
        assert ENERGY_MEASURED in benchmark.MEASURED_ENERGY_QUALITY
        assert ENERGY_GAP_PARTIAL in benchmark.RECONSTRUCTED_ENERGY_QUALITY
        assert ENERGY_GAP_TOTAL_UNKNOWN in benchmark.NEVER_ALLOCABLE

    def test_half_open_intervals(self, built: tuple[pa.Table, dict[str, Any]]) -> None:
        table, _manifest = built
        out = rows_as_dicts(table)
        assert out[0]["interval_start"] == _T0
        assert out[-1]["interval_end"] == _T0 + timedelta(hours=2)
        for row in out:
            assert row["interval_end"] - row["interval_start"] == timedelta(
                seconds=INTERVAL_SECONDS
            )
        for previous, current in zip(out, out[1:]):
            assert current["interval_start"] == previous["interval_end"]

    def test_row_count_matches_the_window(self, built: tuple[pa.Table, dict[str, Any]]) -> None:
        table, manifest = built
        assert table.num_rows == 2 * 60 * 60 // INTERVAL_SECONDS
        assert manifest["coverage"]["interval_count"] == table.num_rows
        assert manifest["coverage"]["grid_last_interval_end"] == build_mod.iso_z(
            _T0 + timedelta(hours=2)
        )

    def test_tariff_periods_are_one_two_three(self, built: tuple[pa.Table, dict[str, Any]]) -> None:
        table, _manifest = built
        assert set(values_for(table, "tariff_period")) <= {1, 2, 3}

    def test_tz_local_follows_the_configured_timezone(
        self, built: tuple[pa.Table, dict[str, Any]]
    ) -> None:
        table, manifest = built
        assert set(values_for(table, "tz_local")) == {"Europe/Madrid"}
        assert manifest["params"]["timezone"] == "Europe/Madrid"
        for row in rows_as_dicts(table):
            assert row["local_day"] == row["interval_start"].astimezone(MADRID).date()

    def test_local_day_and_period_agree_with_the_test_config(
        self, built: tuple[pa.Table, dict[str, Any]]
    ) -> None:
        table, _manifest = built
        periods = TariffPeriods(
            timezone="Europe/Madrid",
            weekday=tuple(PeriodRule(**rule) for rule in WEEKDAY_RULES),
            saturday=(PeriodRule(period=3, start_hour=0, end_hour=24),),
            sunday=(PeriodRule(period=3, start_hour=0, end_hour=24),),
        )
        for row in rows_as_dicts(table):
            local = row["interval_start"].astimezone(MADRID)
            assert row["local_day"] == local.date()
            assert row["tariff_period"] == tariff_period_for(periods, local.date(), local.hour)

    def test_gap_seconds_is_never_negative(self, built: tuple[pa.Table, dict[str, Any]]) -> None:
        table, _manifest = built
        assert all(v >= 0.0 for v in values_for(table, "gap_seconds"))

    def test_sample_counts_add_up_to_the_window_samples(
        self, built: tuple[pa.Table, dict[str, Any]]
    ) -> None:
        table, manifest = built
        assert sum(values_for(table, "sample_count")) == manifest["coverage"]["grid_samples"]

    def test_battery_flag_is_never_null(self, built: tuple[pa.Table, dict[str, Any]]) -> None:
        table, _manifest = built
        assert set(values_for(table, "battery_absent")) == {True}

    def test_dataset_id_column_is_uniform(self, built: tuple[pa.Table, dict[str, Any]]) -> None:
        table, _manifest = built
        assert len(set(values_for(table, "dataset_id"))) == 1

    def test_gross_and_raw_agree_without_a_battery(
        self, built: tuple[pa.Table, dict[str, Any]]
    ) -> None:
        table, _manifest = built
        for row in rows_as_dicts(table):
            assert row["gross_kwh"] == row["raw_grid_kwh"]

    def test_interval_seconds_is_configurable(self, tmp_path: Path, data_dir: Path) -> None:
        cfg = write_tariff(tmp_path)
        write_raw_part(
            data_dir,
            counter_rows(_T0, 13, first=0.0, per_sample=0.01, times=aligned_times(_T0, 13, 300.0)),
        )
        table, _manifest = build_rows(
            data_dir,
            cfg,
            _T0,
            _T0 + timedelta(hours=1),
            interval_seconds=3600,
            lookback=timedelta(minutes=5),
        )
        assert table.num_rows == 1
        # The 13th sample sits exactly on the window end and is excluded by the
        # half-open read window, so 12 samples → 11 spans → 0.11 kWh.
        assert table.column("raw_grid_kwh")[0].as_py() == pytest.approx(0.11)


# ===========================================================================
# 14. Builder input validation
# ===========================================================================


class TestBuildDatasetValidation:
    def test_no_raw_parts_at_all(self, tmp_path: Path, data_dir: Path) -> None:
        cfg = write_tariff(tmp_path)
        with pytest.raises(BuildError, match="run the extract command first"):
            build(data_dir, cfg, _T0, _T0 + timedelta(hours=1))

    def test_missing_grid_counter_series(self, tmp_path: Path, data_dir: Path) -> None:
        cfg = write_tariff(tmp_path)
        write_raw_part(data_dir, price_rows(_T0, {0: 0.1}, per_minute=6, step_seconds=10.0))
        with pytest.raises(GridSeriesMissingError, match="no usable whole-home counter"):
            build(data_dir, cfg, _T0, _T0 + timedelta(hours=1))

    def test_single_sample_cannot_produce_a_delta(self, tmp_path: Path, data_dir: Path) -> None:
        cfg = write_tariff(tmp_path)
        write_raw_part(data_dir, [grid_row(_T0, 1.0)])
        with pytest.raises(GridSeriesMissingError, match="1 sample"):
            build(data_dir, cfg, _T0, _T0 + timedelta(hours=1), lookback=timedelta(minutes=5))

    def test_overlap_raises_through_the_public_api(self, tmp_path: Path, data_dir: Path) -> None:
        cfg = write_tariff(tmp_path)
        at = _T0 + timedelta(minutes=10)
        write_raw_part(data_dir, [grid_row(at, 1.0, job=JOB_OLD), grid_row(at, 1.0, job=JOB_NEW)])
        with pytest.raises(OverlappingSeriesError):
            build(data_dir, cfg, _T0, _T0 + timedelta(hours=1), lookback=timedelta(minutes=5))

    def test_naive_window_is_rejected(self, tmp_path: Path, data_dir: Path) -> None:
        cfg = write_tariff(tmp_path)
        write_raw_part(data_dir, counter_rows(_T0, 5))
        with pytest.raises(ValueError, match="naive"):
            build_dataset(
                data_dir=str(data_dir),
                config_path=str(cfg),
                start="2026-01-05T00:00:00",
                end="2026-01-05T01:00:00",
            )

    def test_inverted_window_is_rejected(self, tmp_path: Path, data_dir: Path) -> None:
        cfg = write_tariff(tmp_path)
        write_raw_part(data_dir, counter_rows(_T0, 5))
        with pytest.raises(ValueError, match="before end"):
            build(data_dir, cfg, _T0 + timedelta(hours=1), _T0)

    def test_missing_config_file(self, tmp_path: Path, data_dir: Path) -> None:
        with pytest.raises(FileNotFoundError, match="tariff config not found"):
            build(data_dir, tmp_path / "nope.yaml", _T0, _T0 + timedelta(hours=1))

    def test_unknown_timezone_is_a_build_error(self, tmp_path: Path, data_dir: Path) -> None:
        cfg = write_tariff(tmp_path, timezone="Mars/Olympus_Mons")
        write_raw_part(data_dir, counter_rows(_T0, 5))
        with pytest.raises(BuildError, match="is not usable"):
            build(data_dir, cfg, _T0, _T0 + timedelta(hours=1))

    def test_iso_string_window_is_accepted(self, tmp_path: Path, data_dir: Path) -> None:
        cfg = write_tariff(tmp_path)
        write_raw_part(data_dir, counter_rows(_T0, 13, times=aligned_times(_T0, 13, 300.0)))
        directory = build_dataset(
            data_dir=str(data_dir),
            config_path=str(cfg),
            start="2026-01-05T00:00:00Z",
            end="2026-01-05T01:00:00Z",
            lookback=timedelta(minutes=5),
        )
        assert directory.is_dir()

    def test_data_dir_inside_a_git_tree_is_refused(self, tmp_path: Path, config: Path) -> None:
        repo_dir = Path(__file__).resolve().parents[2] / "energy-baseline"
        with pytest.raises(ValueError, match="inside git worktree"):
            build_dataset(
                data_dir=str(repo_dir),
                config_path=str(config),
                start=_T0,
                end=_T0 + timedelta(hours=1),
            )

    def test_builder_is_pure_off_the_filesystem(self) -> None:
        """``build_interval_table`` assembles rows without touching disk."""
        series = timeline(counter_rows(_T0, 13, times=aligned_times(_T0, 13, 300.0)))
        prices = price_series([price_row(_T0, 0.1)])
        periods = TariffPeriods(
            timezone="Europe/Madrid",
            weekday=tuple(PeriodRule(**rule) for rule in WEEKDAY_RULES),
            saturday=(PeriodRule(period=3, start_hour=0, end_hour=24),),
            sunday=(PeriodRule(period=3, start_hour=0, end_hour=24),),
        )
        table, ledger, price_ledger = build_interval_table(
            grid=series,
            price=prices,
            interval_starts=grid_times(_T0, _T0 + timedelta(hours=1)),
            periods=periods,
            timezone=MADRID,
            dataset_id="d" * 64,
        )
        assert table.num_rows == 4
        assert ledger is not None
        # The price ledger buckets by the tariff timezone's clock hour: four
        # intervals, one hour, recorded once.
        assert price_ledger.hours_total == 1
        assert price_ledger.measured_hours == 1
        validate_table("ANALYSIS_INTERVAL", table)

    def test_empty_grid_still_validates(self) -> None:
        """A window with no counter data yields null energy, not a crash."""
        periods = TariffPeriods(
            timezone="Europe/Madrid",
            weekday=tuple(PeriodRule(**rule) for rule in WEEKDAY_RULES),
            saturday=(PeriodRule(period=3, start_hour=0, end_hour=24),),
            sunday=(PeriodRule(period=3, start_hour=0, end_hour=24),),
        )
        table, ledger, price_ledger = build_interval_table(
            grid=None,
            price=None,
            interval_starts=grid_times(_T0, _T0 + timedelta(hours=1)),
            periods=periods,
            timezone=MADRID,
            dataset_id="d" * 64,
        )
        assert all(v is None for v in values_for(table, "raw_grid_kwh"))
        assert set(values_for(table, "energy_quality")) == {ENERGY_GAP_TOTAL_UNKNOWN}
        assert set(values_for(table, "price_quality")) == {PRICE_MISSING}
        validate_table("ANALYSIS_INTERVAL", table)
        assert ledger.unattributed_interval_count == 4
        assert price_ledger.missing_hours == 1, "four intervals, one clock hour, no samples"


# ===========================================================================
# 15. End-to-end scenarios on the public entry point
# ===========================================================================


class TestEndToEnd:
    def test_stitched_eras_reproduce_the_counter_total(
        self, tmp_path: Path, data_dir: Path
    ) -> None:
        """Two series_keys, one physical counter: the grid sums to last − first."""
        cfg = write_tariff(tmp_path)
        old, new = TestStitchSeries._two_era_counter(_T0)
        prices = price_rows(_T0, {0: 0.1, 1: 0.2}, per_minute=6, step_seconds=10.0)
        write_raw_part(data_dir, old + new + prices)
        table, manifest = build_rows(
            data_dir, cfg, _T0, _T0 + timedelta(hours=2), lookback=timedelta(minutes=5)
        )
        out = rows_as_dicts(table)
        total = sum(r["gross_kwh"] for r in out if r["gross_kwh"] is not None)
        assert total == pytest.approx(22.0)
        assert len(out) == 8
        assert ENERGY_COUNTER_RESET_BRIDGED not in {r["energy_quality"] for r in out}
        assert 2033.0 not in (total,)
        boundaries = manifest["era_boundaries"]
        assert len(boundaries) == 1
        assert boundaries[0]["entity"] == GRID
        assert boundaries[0]["from_series_key"] != boundaries[0]["to_series_key"]
        assert boundaries[0]["delta_at_boundary"] == pytest.approx(0.0)
        assert manifest["series"][0]["distinct_series_keys"] == 2
        assert manifest["series"][0]["eras"] == 2

    def test_two_parts_carry_the_two_eras(self, tmp_path: Path, data_dir: Path) -> None:
        cfg = write_tariff(tmp_path)
        old, new = TestStitchSeries._two_era_counter(_T0)
        write_raw_part(data_dir, old, "part-a.parquet")
        write_raw_part(data_dir, new, "part-b.parquet")
        table, manifest = build_rows(
            data_dir, cfg, _T0, _T0 + timedelta(hours=2), lookback=timedelta(minutes=5)
        )
        total = sum(
            r["raw_grid_kwh"] for r in rows_as_dicts(table) if r["raw_grid_kwh"] is not None
        )
        assert total == pytest.approx(22.0)
        assert len(manifest["era_boundaries"]) == 1

    def test_spring_forward_local_day_is_92_intervals(self, tmp_path: Path, data_dir: Path) -> None:
        cfg = write_tariff(tmp_path)
        day = date(2026, 3, 29)
        start, end = local_day_utc_bounds(day)
        count = int((end - start).total_seconds() // 300) + 1
        rows = counter_rows(
            start, count, per_sample=0.005, times=counter_times(start, count, 300.0)
        )
        rows += price_rows(start, {h: 0.1 for h in range(24)}, per_minute=6, step_seconds=10.0)
        write_raw_part(data_dir, rows)
        table, manifest = build_rows(data_dir, cfg, start, end, lookback=timedelta(minutes=5))
        out = rows_as_dicts(table)
        assert len(out) == 92
        assert manifest["requested_window"] == {
            "start": build_mod.iso_z(start),
            "end": build_mod.iso_z(end),
        }
        assert {r["local_day"] for r in out} == {day}
        assert {r["dst_transition"] for r in out} == {"spring_forward"}
        local_hours = [r["interval_start"].astimezone(MADRID).hour for r in out]
        assert 2 not in local_hours  # 02:00 never existed that day
        assert local_hours.count(3) == 4

    def test_fall_back_local_day_is_100_intervals(self, tmp_path: Path, data_dir: Path) -> None:
        cfg = write_tariff(tmp_path)
        day = date(2025, 10, 26)
        start, end = local_day_utc_bounds(day)
        count = int((end - start).total_seconds() // 300) + 1
        rows = counter_rows(
            start, count, per_sample=0.005, times=counter_times(start, count, 300.0)
        )
        write_raw_part(data_dir, rows)
        table, _manifest = build_rows(data_dir, cfg, start, end, lookback=timedelta(minutes=5))
        out = rows_as_dicts(table)
        assert len(out) == 100
        assert {r["dst_transition"] for r in out} == {"fall_back"}
        # The duplicated local hour appears twice on the grid, in absolute order.
        local_hours = [r["interval_start"].astimezone(MADRID).hour for r in out]
        assert local_hours.count(2) == 8
        assert len(out) == len(set(r["interval_start"] for r in out))
        assert out[0]["interval_start"] < out[-1]["interval_start"]

    def test_neighbouring_days_carry_no_dst_flag(self, tmp_path: Path, data_dir: Path) -> None:
        cfg = write_tariff(tmp_path)
        start, _ = local_day_utc_bounds(date(2026, 3, 28))
        _, end = local_day_utc_bounds(date(2026, 3, 30))
        count = int((end - start).total_seconds() // 300) + 1
        write_raw_part(
            data_dir,
            counter_rows(start, count, per_sample=0.005, times=counter_times(start, count, 300.0)),
        )
        table, _manifest = build_rows(data_dir, cfg, start, end, lookback=timedelta(minutes=5))
        by_day: dict[Any, set[Any]] = {}
        for row in rows_as_dicts(table):
            by_day.setdefault(row["local_day"], set()).add(row["dst_transition"])
        assert by_day[date(2026, 3, 28)] == {None}
        assert by_day[date(2026, 3, 29)] == {"spring_forward"}
        assert by_day[date(2026, 3, 30)] == {None}
        assert len(by_day[date(2026, 3, 29)]) == 1

    def test_negative_prices_survive_end_to_end(self, tmp_path: Path, data_dir: Path) -> None:
        cfg = write_tariff(tmp_path)
        rows = counter_rows(_T0, 13, times=aligned_times(_T0, 13, 300.0))
        rows += [price_row(_T0 + timedelta(seconds=10 * i), -0.153) for i in range(180)]
        rows += [price_row(_T0 + timedelta(hours=1, seconds=10 * i), 0.002) for i in range(180)]
        write_raw_part(data_dir, rows)
        table, _manifest = build_rows(
            data_dir, cfg, _T0, _T0 + timedelta(hours=2), lookback=timedelta(minutes=5)
        )
        prices = [r["price_eur_per_kwh"] for r in rows_as_dicts(table)]
        assert min(prices) == pytest.approx(-0.153)
        assert {p for p in prices if p is not None} == {-0.153, 0.002}
        assert all(p is not None for p in prices)
        assert sum(1 for p in prices if p != 0) == 8

    def test_price_outage_becomes_forward_filled_not_silently_measured(
        self, tmp_path: Path, data_dir: Path
    ) -> None:
        cfg = write_tariff(tmp_path)
        rows = counter_rows(_T0, 25, times=aligned_times(_T0, 25, 300.0))
        rows += [
            price_row(_T0 + timedelta(seconds=10 * i), 0.2) for i in range(60)
        ]  # only the first hour
        write_raw_part(data_dir, rows)
        table, manifest = build_rows(
            data_dir, cfg, _T0, _T0 + timedelta(hours=2), lookback=timedelta(minutes=5)
        )
        out = rows_as_dicts(table)
        qualities = [r["price_quality"] for r in out]
        # Hour 0 really was observed; hour 1 has no sample of its own, so all four
        # of its intervals carry the stale value — the hour boundary, not the
        # interval boundary, is what separates measured from forward-filled.
        assert qualities[:4] == [PRICE_MEASURED] * 4
        assert qualities[4:] == [PRICE_FORWARD_FILLED] * 4
        assert PRICE_MISSING not in qualities
        # the value is still the last known price, never zero
        for row in out:
            if row["price_quality"] == PRICE_FORWARD_FILLED:
                assert row["price_eur_per_kwh"] == pytest.approx(0.2)
        block = manifest["price_alignment"]
        assert block["measured_hours"] == 1
        assert block["forward_filled_hours"] == 1
        assert block["hour_timezone"] == "Europe/Madrid"
        assert block["max_carried_age_seconds"] == pytest.approx(3010.0)

    def test_no_price_series_at_all_marks_every_interval_missing(
        self, tmp_path: Path, data_dir: Path
    ) -> None:
        cfg = write_tariff(tmp_path)
        write_raw_part(data_dir, counter_rows(_T0, 13, times=aligned_times(_T0, 13, 300.0)))
        table, _manifest = build_rows(
            data_dir, cfg, _T0, _T0 + timedelta(hours=1), lookback=timedelta(minutes=5)
        )
        assert set(values_for(table, "price_quality")) == {PRICE_MISSING}
        assert all(v is None for v in values_for(table, "price_eur_per_kwh"))
        assert all(r["raw_grid_kwh"] is not None for r in rows_as_dicts(table))

    def test_device_outage_is_flagged_and_its_total_preserved(
        self, tmp_path: Path, data_dir: Path
    ) -> None:
        cfg = write_tariff(tmp_path)
        before = counter_rows(
            _T0, 20, first=100.0, per_sample=0.01, times=aligned_times(_T0, 20, 60.0)
        )
        outage_start = _T0 + timedelta(minutes=19)
        after = counter_rows(
            outage_start + timedelta(hours=2),
            20,
            first=100.19 + 0.5,
            per_sample=0.01,
            times=aligned_times(outage_start + timedelta(hours=2), 20, 60.0),
        )
        write_raw_part(data_dir, before + after)
        table, manifest = build_rows(
            data_dir,
            cfg,
            _T0,
            outage_start + timedelta(hours=2, minutes=20),
            lookback=timedelta(minutes=5),
        )
        out = rows_as_dicts(table)
        during = [
            r
            for r in out
            if outage_start <= r["interval_start"] < outage_start + timedelta(hours=2)
        ]
        assert during
        assert all(r["raw_grid_kwh"] is None for r in during), (
            "no invented consumption inside the outage"
        )
        assert all(
            r["energy_quality"] in {ENERGY_GAP_PARTIAL, ENERGY_GAP_TOTAL_UNKNOWN} for r in during
        )
        assert all(r["gap_seconds"] > 0 for r in during)
        assert len(manifest["gaps"]) == 1
        assert manifest["gaps"][0]["duration_seconds"] == pytest.approx(7200.0 + 60.0, abs=120.0)
        assert manifest["gaps"][0]["known_total"] == pytest.approx(0.5, abs=0.02)
        ledger = manifest["unattributed_spans"]
        assert ledger["gap_span_count"] == 1
        assert ledger["gap_span_total_kwh"] == pytest.approx(0.5, abs=0.02)
        # The table's null count equals the manifest's excluded count.
        assert manifest["excluded_intervals"]["count"] == sum(
            1 for r in out if r["raw_grid_kwh"] is None
        )
        assert manifest["excluded_intervals"]["count"] >= len(during)
        ranges = manifest["excluded_intervals"]["ranges"]
        assert len(ranges) >= 1
        # the energy that *is* known still adds up on both sides of the outage
        known = sum(r["raw_grid_kwh"] for r in out if r["raw_grid_kwh"] is not None)
        assert known < (after[-1]["value"] - before[0]["value"])

    def test_counter_reset_is_disclosed_and_never_negative(
        self, tmp_path: Path, data_dir: Path
    ) -> None:
        cfg = write_tariff(tmp_path)
        rows = [
            grid_row(_T0, 100.0),
            grid_row(_T0 + timedelta(minutes=10), 101.0),
            grid_row(_T0 + timedelta(minutes=20), 3.0),  # counter replaced
            grid_row(_T0 + timedelta(minutes=30), 3.5),
            grid_row(_T0 + timedelta(minutes=40), 4.0),
        ]
        write_raw_part(data_dir, rows)
        table, manifest = build_rows(
            data_dir, cfg, _T0, _T0 + timedelta(hours=1), lookback=timedelta(minutes=5)
        )
        out = rows_as_dicts(table)
        # The reset span (00:10→00:20) crosses the :15 boundary, so both intervals
        # it touches are flagged as counter_reset_bridged.
        assert [r["energy_quality"] for r in out] == [
            ENERGY_COUNTER_RESET_BRIDGED,
            ENERGY_COUNTER_RESET_BRIDGED,
            ENERGY_MEASURED,
            ENERGY_GAP_TOTAL_UNKNOWN,
        ]
        assert all(r["raw_grid_kwh"] is None or r["raw_grid_kwh"] >= 0 for r in out)
        assert out[0]["raw_grid_kwh"] is None
        assert out[1]["raw_grid_kwh"] is None
        assert out[2]["raw_grid_kwh"] == pytest.approx(0.5)
        assert len(manifest["counter_resets"]) == 1
        assert manifest["counter_resets"][0]["kind"] == "counter_reset"
        assert manifest["unattributed_spans"]["reset_span_count"] == 1

    def test_quality_counts_and_coverage_match_the_table(
        self, tmp_path: Path, data_dir: Path
    ) -> None:
        cfg = write_tariff(tmp_path)
        rows = counter_rows(_T0, 61, times=counter_times(_T0, 61, 60.0))
        rows += price_rows(_T0, {0: 0.1, 1: 0.2}, per_minute=6, step_seconds=10.0)
        write_raw_part(data_dir, rows)
        table, manifest = build_rows(
            data_dir, cfg, _T0, _T0 + timedelta(hours=2), lookback=timedelta(minutes=5)
        )
        out = rows_as_dicts(table)
        counts = manifest["quality_counts"]
        for column in ("energy_quality", "allocation_quality", "price_quality"):
            assert sum(counts[column].values()) == len(out)
            for value, size in counts[column].items():
                assert sum(1 for r in out if r[column] == value) == size
        coverage = manifest["coverage"]
        assert coverage["grid_samples"] == len(rows_of_counter(rows))
        assert coverage["intervals_with_energy"] == sum(
            1 for r in out if r["raw_grid_kwh"] is not None
        )
        assert coverage["interval_count"] == len(out)
        assert coverage["raw_rows_read"] == len(rows)
        # 6 samples per hour × 2 hours = 12 price samples.
        assert coverage["price_samples"] == 12

    def test_run_manifest_is_recorded_outside_the_dataset(
        self, tmp_path: Path, data_dir: Path
    ) -> None:
        cfg = write_tariff(tmp_path)
        write_raw_part(data_dir, counter_rows(_T0, 13, times=aligned_times(_T0, 13, 300.0)))
        directory = build(
            data_dir, cfg, _T0, _T0 + timedelta(hours=1), lookback=timedelta(minutes=5)
        )
        # The build writes one *analysis* run manifest alongside the raw extraction
        # manifest(s) the fixture produced; select the analysis one by source.
        all_manifests = [json.loads(p.read_text()) for p in (data_dir / "manifests").glob("*.json")]
        runs = [m for m in all_manifests if m["source"] == build_mod.SOURCE]
        assert len(runs) == 1
        run = runs[0]
        assert run["source"] == build_mod.SOURCE
        assert run["row_count"] == 4
        assert run["source_window_start"] == build_mod.iso_z(_T0)
        assert any(flag.startswith("energy_quality=") for flag in run["quality_flags"])
        assert "dataset_id" in run["coverage_notes"]
        # the dataset directory itself never carries the run time
        assert not (directory / "manifest.json").exists()

    def test_irregular_offsets_do_not_break_the_daily_total(
        self, tmp_path: Path, data_dir: Path
    ) -> None:
        """Real cadence (never aligned) across a full local day still telescopes.

        This is the primary defect's regression test: samples arrive at ``:39``
        offsets, so essentially every counter span straddles a 15-minute
        boundary, and right-endpoint attribution therefore flagged **all**
        intervals ``unknown_gap`` — a useless signal that no benchmark could run
        on.  Bounded boundary splitting must make the majority of intervals
        usable while keeping the daily total an exact counter difference.
        """
        cfg = write_tariff(tmp_path)
        start, end = local_day_utc_bounds(date(2026, 1, 5))
        count = int((end - start).total_seconds() // 60)
        rows = counter_rows(
            start, count, first=14612.0, per_sample=0.0004, times=counter_times(start, count, 60.0)
        )
        write_raw_part(data_dir, rows)
        table, manifest = build_rows(data_dir, cfg, start, end, lookback=timedelta(minutes=5))
        out = rows_as_dicts(table)
        assert len(out) == 96
        total = sum(r["raw_grid_kwh"] for r in out if r["raw_grid_kwh"] is not None)
        # The last sample at start + (count-1)*60 + offset may exceed `end` by the
        # irregular offset and be excluded by the half-open read window; the
        # telescoping sum equals the last *in-window* reading minus the first.
        last_in_window = max(r["ts"] for r in rows if r["ts"] < end)
        first = rows[0]["value"]
        last_value = next(r["value"] for r in rows if r["ts"] == last_in_window)
        assert total == pytest.approx(last_value - first, rel=1e-9)
        assert manifest["coverage"]["grid_samples"] == count

        qualities = Counter(r["allocation_quality"] for r in out)
        assert qualities[ALLOCATION_UNKNOWN_GAP] == 0, (
            "normal cadence is not a gap: nothing here may be reported as unknown"
        )
        usable = qualities[ALLOCATION_EXACT] + qualities[ALLOCATION_ESTIMATED_BOUNDARY]
        assert usable == len(out), "every interval of the day carries a number"
        # Irregular offsets mean a boundary crossing is the rule, not the exception.
        assert qualities[ALLOCATION_ESTIMATED_BOUNDARY] > len(out) * 0.9
        assert all(r["energy_quality"] == ENERGY_MEASURED for r in out)
        assert all(r["raw_grid_kwh"] is not None for r in out)
        # The approximation is visible in the manifest, not buried.
        block = manifest["boundary_allocation"]
        assert block["rule"] == "proportional_split_at_boundary"
        assert block["intervals_estimated"] == qualities[ALLOCATION_ESTIMATED_BOUNDARY]
        assert block["spans_split"] > 0
        # Measured from this fixture's own lattice (per_sample=0.0004 kWh), not from
        # a constant — the archive's measured lattice is 0.0001 kWh.
        assert block["quantisation_kwh"] == pytest.approx(0.0004)
        assert block["quantisation_measurement"]["basis"] == "measured_gcd"
        assert not block["quantisation_fallback_used"]

    def test_build_is_repeatable_after_an_intervening_change(
        self, tmp_path: Path, data_dir: Path
    ) -> None:
        """Re-running the original window after a new part lands keeps the old bytes.

        The *id* changes because the source archive grew (new part hashed), but
        the original dataset directory is never mutated — it is immutable.
        """
        cfg = write_tariff(tmp_path)
        rows = counter_rows(_T0, 13, times=aligned_times(_T0, 13, 300.0))
        write_raw_part(data_dir, rows, "part-a.parquet")
        first = build(data_dir, cfg, _T0, _T0 + timedelta(hours=1), lookback=timedelta(minutes=5))
        original = (
            first / "parts" / sorted(p.name for p in (first / "parts").glob("*.parquet"))[0]
        ).read_bytes()
        # A later window arrives; the earlier dataset must not be rewritten.
        write_raw_part(
            data_dir,
            counter_rows(
                _T0 + timedelta(hours=1),
                13,
                first=14612.048,
                times=aligned_times(_T0 + timedelta(hours=1), 13, 300.0),
            ),
            "part-b.parquet",
        )
        second = build(data_dir, cfg, _T0, _T0 + timedelta(hours=2), lookback=timedelta(minutes=5))
        # The new build created a *different* dataset (new part hashed) and did
        # not touch the original one.
        assert second != first
        assert (
            first / "parts" / sorted(p.name for p in (first / "parts").glob("*.parquet"))[0]
        ).read_bytes() == original
        # Re-running the original window with the same archive produces a
        # different id (parts changed) but the original directory is still intact.
        again = build(data_dir, cfg, _T0, _T0 + timedelta(hours=1), lookback=timedelta(minutes=5))
        assert again != first
        assert (
            first / "parts" / sorted(p.name for p in (first / "parts").glob("*.parquet"))[0]
        ).read_bytes() == original


def rows_of_counter(rows: list[dict[str, Any]]) -> list[dict[str, Any]]:
    return [row for row in rows if row["entity"] == GRID]


# ===========================================================================
# 16. Fix 1: bounded boundary allocation (the ``unknown_gap`` flood)
# ===========================================================================

#: A synthetic tick size used to *exercise* boundary splitting and aliasing.  It is
#: deliberately **not** the archive's quantum: the real whole-home counter advances
#: on a 0.0001 kWh lattice (measured GCD over 466,460 positive increments in 2025).
#: This fixture's 0.0016 kWh steps are the shape that once got mistaken for a
#: verified fact on a small sample, so the tests below assert that the manifest
#: reports what it *measured from these rows* instead of repeating that mistake.
TICK_KWH = 0.0016


class TestBoundaryAllocationRegression:
    """Normal cadence crossing a boundary is an estimate, never an unknown gap."""

    def test_quantised_counter_allocates_without_negatives(self) -> None:
        """Synthetic 1.6 Wh ticks (and repeated readings) split correctly and stay ≥ 0.

        Shape only: these readings are quantised at 0.0016 kWh because flat minutes
        and straddling spans are what the allocator must survive.  The archive's own
        lattice is 0.0001 kWh and is measured, never assumed — see
        ``TestQuantisationIsMeasured``.  A span with a zero delta must still allocate
        0.0 to both sides rather than a negative or a null.
        """
        times = counter_times(_T0, 61, 60.0)
        values: list[float] = []
        total = 14_612.0
        for i in range(len(times)):
            # Flat stretches (no tick) alternating with 1–3 tick steps.
            if i % 4 != 0:
                total += TICK_KWH * (1 + (i % 3))
            values.append(total)
        rows = [grid_row(t, v) for t, v in zip(times, values, strict=True)]
        series = timeline(rows)
        spans = counter_deltas(series)
        assert all(span.delta is not None and span.delta >= 0.0 for span in spans)
        assert any(span.delta == 0.0 for span in spans), "the flat minutes are the point"
        # Quantisation holds: every delta is an integer number of ticks.
        assert all(
            abs(round((span.delta or 0.0) / TICK_KWH) - (span.delta or 0.0) / TICK_KWH) < 1e-6
            for span in spans
        )

        grid = grid_times(_T0, _T0 + timedelta(hours=1))
        energies, ledger = allocate_energy(series, spans, grid)
        assert all(e.raw_grid_kwh is not None for e in energies)
        assert all(e.raw_grid_kwh >= 0.0 for e in energies), "no negative energy, ever"
        assert all(e.energy_quality == ENERGY_MEASURED for e in energies)
        assert emitted(energies) == pytest.approx(values[-1] - values[0], abs=1e-12)
        # The quantisation step is disclosed, not buried — and it is disclosed as a
        # measurement of *these* rows, whose lattice is the synthetic 0.0016 kWh.
        assert ledger.boundary_span_count > 0
        assert build_mod.measure_counter_quantisation(series)["quantisation_kwh"] == pytest.approx(
            TICK_KWH
        )

    def test_uniform_load_is_not_distorted_by_the_offset_phase(self) -> None:
        """Constant power with an irregular sampling phase yields flat intervals.

        The bound that makes boundary splitting safe: the error per interval is at
        most the share of one sample cadence (≤60 s of a 900 s interval), so a
        steady load cannot be turned into a saw-tooth by ``:39`` offsets.  Under
        the old right-endpoint rule this shape was reported as 100 % unknown.
        """
        rows = counter_rows(_T0, 61, first=14_612.0, per_sample=TICK_KWH)
        series = timeline(rows)
        spans = counter_deltas(series)
        grid = grid_times(_T0, _T0 + timedelta(hours=1))
        energies, ledger = allocate_energy(series, spans, grid)
        values = [e.raw_grid_kwh for e in energies]
        assert all(v is not None for v in values)
        assert all(e.allocation_quality == ALLOCATION_ESTIMATED_BOUNDARY for e in energies)
        # 15 samples per interval × one tick each is the steady expectation, and
        # the spread between intervals never exceeds a single tick.
        assert max(values) - min(values) <= TICK_KWH + 1e-12
        assert ledger.boundary_span_count == 4, "one straddling span per boundary"

    def test_genuine_gap_is_never_interpolated_alongside_split_spans(self) -> None:
        """A real outage keeps ``unknown_gap`` even though neighbours now split.

        The two behaviours must not be confused: normal cadence is interpolated at
        a boundary because the total is known and the cadence is dense; a gap
        longer than the threshold gets no invented allocation at all.
        """
        before = counter_rows(
            _T0, 20, first=100.0, per_sample=TICK_KWH, times=counter_times(_T0, 20)
        )
        after = counter_rows(
            _T0 + timedelta(hours=1),
            20,
            first=100.0 + 19 * TICK_KWH + 0.5,
            per_sample=TICK_KWH,
            times=counter_times(_T0 + timedelta(hours=1), 20),
        )
        series = timeline(before + after)
        spans = counter_deltas(series)
        assert sum(1 for span in spans if span.is_gap) == 1
        grid = grid_times(_T0, _T0 + timedelta(hours=2))
        energies, ledger = allocate_energy(series, spans, grid)
        by_interval = dict(zip(grid, energies, strict=True))

        during = [
            start
            for start in grid
            if _T0 + timedelta(minutes=20) <= start < _T0 + timedelta(hours=1)
        ]
        assert during, "the outage covers several intervals"
        for start in during:
            row = by_interval[start]
            assert row.raw_grid_kwh is None, f"{start}: a gap must not be interpolated"
            assert row.allocation_quality == ALLOCATION_UNKNOWN_GAP
            assert row.energy_quality in {ENERGY_GAP_PARTIAL, ENERGY_GAP_TOTAL_UNKNOWN}
            assert row.gap_seconds > 0.0
        # Outside the outage, the same cadence is estimated-boundary and usable.
        edge = _T0 + timedelta(minutes=15)
        before_outage = [row for start, row in by_interval.items() if start < edge]
        assert before_outage and all(
            row.allocation_quality in {ALLOCATION_EXACT, ALLOCATION_ESTIMATED_BOUNDARY}
            for row in before_outage
        )
        assert ledger.gap_span_count == 1
        assert ledger.gap_span_total_kwh == pytest.approx(0.5, abs=1e-9)

    def test_reset_is_bridged_and_never_a_huge_negative(self) -> None:
        """A replacement counter reading cannot look like negative consumption."""
        rows = [
            grid_row(_T0, 9_000.0),
            grid_row(_T0 + timedelta(seconds=59.219), 9_000.0 + TICK_KWH),
            grid_row(_T0 + timedelta(seconds=119.875), 12.0),  # counter replaced
            grid_row(_T0 + timedelta(seconds=179.125), 12.0 + TICK_KWH),
            grid_row(_T0 + timedelta(seconds=239.906), 12.0 + 2 * TICK_KWH),
        ]
        series = timeline(rows)
        spans = counter_deltas(series)
        assert [span.is_reset for span in spans] == [False, True, False, False]
        assert spans[1].delta is None, "a reset carries no invented total"
        # 120 s intervals so the post-reset readings fall in a later interval.
        grid = make_utc_grid(_T0, _T0 + timedelta(minutes=6), interval_seconds=120)
        energies, ledger = allocate_energy(series, spans, grid, interval_seconds=120)
        assert all(e.raw_grid_kwh is None or e.raw_grid_kwh >= 0.0 for e in energies)
        numbers = [e.raw_grid_kwh for e in energies if e.raw_grid_kwh is not None]
        assert all(v >= 0.0 for v in numbers)
        bridged = [e for e in energies if e.energy_quality == ENERGY_COUNTER_RESET_BRIDGED]
        assert bridged, "the intervals the reset span touches are flagged"
        assert all(e.raw_grid_kwh is None for e in bridged)
        assert all(e.allocation_quality == ALLOCATION_UNKNOWN_GAP for e in bridged)
        assert ledger.reset_span_count == 1
        # Energy after the reset is still reported normally, in tick units.
        tail = [e for e in energies if e.energy_quality == ENERGY_MEASURED]
        assert tail, "the timeline continues after the bridge"
        assert all(0.0 < e.raw_grid_kwh <= 2 * TICK_KWH for e in tail)


class TestBuildManifestDisclosesApproximations:
    """The two fixes must be visible in the manifest, and identity stays deterministic."""

    def _rows(self) -> list[dict[str, Any]]:
        rows = counter_rows(_T0, 61, times=counter_times(_T0, 61, 60.0))
        rows += price_rows(_T0, {0: 0.1, 1: -0.03}, per_minute=6, step_seconds=10.0)
        return rows

    def test_manifest_records_the_new_counts(self, tmp_path: Path, data_dir: Path) -> None:
        cfg = write_tariff(tmp_path)
        write_raw_part(data_dir, self._rows())
        table, manifest = build_rows(
            data_dir, cfg, _T0, _T0 + timedelta(hours=2), lookback=timedelta(minutes=5)
        )
        out = rows_as_dicts(table)
        qualities = Counter(r["allocation_quality"] for r in out)

        block = manifest["boundary_allocation"]
        assert block["rule"] == "proportional_split_at_boundary"
        assert block["intervals_estimated"] == qualities[ALLOCATION_ESTIMATED_BOUNDARY]
        assert block["spans_split"] == manifest["unattributed_spans"]["boundary_span_count"]
        assert block["pieces_written"] == manifest["unattributed_spans"]["boundary_piece_count"]
        assert block["kwh_through_split_spans"] == pytest.approx(
            manifest["unattributed_spans"]["boundary_span_total_kwh"]
        )
        assert block["spans_split"] > 0, "irregular cadence must produce split spans"
        assert block["telescoping"]
        # The published quantum is *measured from the loaded counter*, never copied
        # off a constant: this fixture ticks in 0.004 kWh, so that is what the
        # manifest says, even though the archive's own lattice is 0.0001 kWh.  A
        # synthetic lattice can never become a fact about the real data.
        assert block["quantisation_kwh"] == pytest.approx(0.004)
        measurement = block["quantisation_measurement"]
        assert measurement["basis"] == "measured_gcd"
        assert measurement["method"] == "gcd_of_positive_increments_in_micro_kwh"
        assert measurement["positive_increment_count"] > 0
        assert measurement["negative_increment_count"] == 0
        assert not block["quantisation_fallback_used"]
        assert COUNTER_QUANTISATION_KWH == pytest.approx(0.0001)

        price_block = manifest["price_alignment"]
        assert price_block["rule"] == "clock_hour_aligned"
        assert price_block["hour_timezone"] == "Europe/Madrid"
        assert price_block["hours_total"] == 2
        assert price_block["measured_hours"] == 2
        assert price_block["disagreement_count"] == 0
        assert price_block["lookahead_note"] == build_mod.PRICE_LOOKAHEAD_NOTE
        # The note must not assert a fixed scrape offset: "~:39" was one day's
        # phase, and the year-wide measurement contradicts it.
        assert "39" not in price_block["lookahead_note"]
        assert ":00:39" not in price_block["lookahead_note"]
        offsets = price_block["first_observation_offset_seconds"]
        assert offsets["hours_with_a_sample"] == 2
        assert offsets["max_seconds"] == pytest.approx(price_block["max_lookahead_seconds"])
        assert price_block["publication_provenance"] == build_mod.PRICE_PUBLICATION_PROVENANCE
        assert manifest["quality_counts"]["allocation_quality"] == dict(qualities)

    def test_dataset_id_is_deterministic_and_changes_with_inputs(
        self, tmp_path: Path, data_dir: Path
    ) -> None:
        cfg = write_tariff(tmp_path)
        write_raw_part(data_dir, self._rows())
        first = build(data_dir, cfg, _T0, _T0 + timedelta(hours=2), lookback=timedelta(minutes=5))
        again = build(data_dir, cfg, _T0, _T0 + timedelta(hours=2), lookback=timedelta(minutes=5))
        assert first == again, "identical inputs reproduce the same dataset id"

        # A genuinely *new* source part covering a later period: same window, but
        # the archive lineage grew, so the id must change.
        later = counter_rows(
            _T0 + timedelta(hours=2),
            13,
            first=999.0,
            times=aligned_times(_T0 + timedelta(hours=2), 13, 300.0),
        )
        later += price_rows(_T0 + timedelta(hours=2), {0: 0.1}, per_minute=6, step_seconds=10.0)
        write_raw_part(data_dir, later, "part-b.parquet")
        changed = build(data_dir, cfg, _T0, _T0 + timedelta(hours=2), lookback=timedelta(minutes=5))
        assert changed != first, "a new source part must mint a new dataset id"
        assert (first / "build_manifest.json").read_bytes() == (
            again / "build_manifest.json"
        ).read_bytes(), "the manifest carries no wall clock"


# ===========================================================================
# 17. Fix 2: clock-hour price alignment (the systematic one-hour lag)
# ===========================================================================


class TestHourAlignedPrices:
    def test_disagreement_inside_an_hour_is_flagged_not_averaged(
        self, tmp_path: Path, data_dir: Path
    ) -> None:
        """Two different prices in one clock hour: no silent pick, no mean."""
        cfg = write_tariff(tmp_path)
        rows = counter_rows(_T0, 13, times=aligned_times(_T0, 13, 300.0))
        rows += [price_row(_T0 + timedelta(seconds=39), 0.10)]
        rows += [price_row(_T0 + timedelta(seconds=99), 0.90)]
        rows += [price_row(_T0 + timedelta(hours=1, seconds=39), 0.20)]
        write_raw_part(data_dir, rows)
        table, manifest = build_rows(
            data_dir, cfg, _T0, _T0 + timedelta(hours=2), lookback=timedelta(minutes=5)
        )
        out = rows_as_dicts(table)
        by_hour = [r["price_quality"] for r in out]
        assert by_hour[:4] == [PRICE_AMBIGUOUS] * 4, "every interval of a disputed hour is disputed"
        assert by_hour[4:] == [PRICE_MEASURED] * 4
        assert [r["price_eur_per_kwh"] for r in out[:4]] == [None] * 4
        assert 0.5 not in [r["price_eur_per_kwh"] for r in out], "never the mean"
        block = manifest["price_alignment"]
        assert block["ambiguous_hours"] == 1
        assert block["disagreement_count"] == 1
        assert block["intervals_ambiguous"] == 4
        assert block["measured_hours"] == 1
        disputed = [d for d in block["details"] if d["quality"] == PRICE_AMBIGUOUS]
        assert len(disputed) == 1
        assert disputed[0]["samples"] == 2
        assert sorted(disputed[0]["values"]) == [0.10, 0.90]

    @pytest.mark.parametrize(
        ("day", "intervals", "hours", "transition"),
        [
            pytest.param(date(2026, 3, 29), 92, 23, "spring_forward", id="spring-forward"),
            pytest.param(date(2025, 10, 26), 100, 25, "fall_back", id="fall-back"),
            pytest.param(date(2026, 1, 5), 96, 24, None, id="ordinary"),
        ],
    )
    def test_hour_bucketing_across_dst(
        self,
        tmp_path: Path,
        data_dir: Path,
        day: date,
        intervals: int,
        hours: int,
        transition: str | None,
    ) -> None:
        """No hour is duplicated or dropped when the clock shifts by an hour.

        Each clock hour carries a distinct price equal to its UTC hour index, so a
        bucketing mistake is visible immediately: an interval showing its
        neighbour's index means the price moved an hour.
        """
        cfg = write_tariff(tmp_path)
        start, end = local_day_utc_bounds(day)
        count = int((end - start).total_seconds() // PRICE_PERIOD_SECONDS)
        stamps = [start + timedelta(seconds=PRICE_PERIOD_SECONDS * i) for i in range(count)]
        rows: list[dict[str, Any]] = []
        for i, stamp in enumerate(stamps):
            # Verified publication shape: the hour's price first appears ~39 s in.
            rows.append(price_row(stamp + timedelta(seconds=39), float(i)))
            rows.append(price_row(stamp + timedelta(seconds=99), float(i)))
        rows += counter_rows(
            start,
            int((end - start).total_seconds() // 300) + 1,
            per_sample=0.0016,
            times=counter_times(start, int((end - start).total_seconds() // 300) + 1, 300.0),
        )
        write_raw_part(data_dir, rows)
        table, manifest = build_rows(data_dir, cfg, start, end, lookback=timedelta(minutes=5))
        out = rows_as_dicts(table)
        assert len(out) == intervals

        expected = {stamp.astimezone(UTC): float(i) for i, stamp in enumerate(stamps)}
        for row in out:
            stamp = row["interval_start"]
            hour = stamp.replace(minute=0, second=0, microsecond=0)
            assert row["price_eur_per_kwh"] == pytest.approx(expected[hour]), stamp
            assert row["price_quality"] == PRICE_MEASURED, stamp

        # Distinct hour buckets: the repeated fall-back hour counts twice, the
        # skipped spring-forward hour never appears.
        buckets = {r["interval_start"].replace(minute=0, second=0, microsecond=0) for r in out}
        assert len(buckets) == hours
        assert len(buckets) * 4 == intervals
        assert manifest["price_alignment"]["hours_total"] == hours
        assert manifest["price_alignment"]["measured_hours"] == hours
        assert manifest["price_alignment"]["disagreement_count"] == 0
        assert {r["dst_transition"] for r in out} == ({transition} if transition else {None})

    def test_negative_prices_survive_both_fixes(self, tmp_path: Path, data_dir: Path) -> None:
        """Quantised irregular-offset energy plus hour-aligned negative prices."""
        cfg = write_tariff(tmp_path)
        # 121 samples at ~60 s cover the whole 2 h window with irregular offsets.
        rows = counter_rows(
            _T0, 121, first=14_612.0, per_sample=TICK_KWH, times=counter_times(_T0, 121, 60.0)
        )
        rows += price_rows(_T0, {0: -0.153, 1: -0.001}, per_minute=6, step_seconds=10.0)
        write_raw_part(data_dir, rows)
        table, manifest = build_rows(
            data_dir, cfg, _T0, _T0 + timedelta(hours=2), lookback=timedelta(minutes=5)
        )
        out = rows_as_dicts(table)
        prices = [r["price_eur_per_kwh"] for r in out]
        assert prices == [-0.153] * 4 + [-0.001] * 4
        assert min(prices) == pytest.approx(-0.153), "negative prices are never clamped"
        assert all(r["price_quality"] == PRICE_MEASURED for r in out)
        assert all(r["raw_grid_kwh"] is not None for r in out)
        assert all(r["raw_grid_kwh"] >= 0.0 for r in out)
        assert {r["allocation_quality"] for r in out} <= {
            ALLOCATION_EXACT,
            ALLOCATION_ESTIMATED_BOUNDARY,
        }
        # Every counter reading that the half-open read window admits is inside the
        # grid, so the grid telescopes to the exact quantised counter difference.
        window_end = _T0 + timedelta(hours=2)
        counter = [r for r in rows if r["entity"] == GRID and r["ts"] < window_end]
        total = sum(r["raw_grid_kwh"] for r in out)
        assert total == pytest.approx(counter[-1]["value"] - counter[0]["value"], abs=1e-12)
        assert manifest["price_alignment"]["ambiguous_hours"] == 0

    def test_benchmark_can_consume_the_output(self, tmp_path: Path, data_dir: Path) -> None:
        """The quality literals must match what ``benchmark.usable_mask`` accepts.

        The two defects made the dataset unbenchmarkable: everything was
        ``unknown_gap``.  This is the cross-check that the vocabulary agrees,
        without the builder importing benchmark policy.
        """
        from energy_baseline import benchmark

        cfg = write_tariff(tmp_path)
        rows = counter_rows(_T0, 61, times=counter_times(_T0, 61, 60.0))
        rows += price_rows(_T0, {0: 0.1, 1: 0.2}, per_minute=6, step_seconds=10.0)
        write_raw_part(data_dir, rows)
        table, _manifest = build_rows(
            data_dir, cfg, _T0, _T0 + timedelta(hours=2), lookback=timedelta(minutes=5)
        )

        strict, _reasons = benchmark.usable_mask(table)
        lenient, _ = benchmark.usable_mask(table, allow_reconstructed=True)
        assert ALLOCATION_ESTIMATED_BOUNDARY in benchmark.USABLE_ALLOCATION_QUALITY
        assert ALLOCATION_UNKNOWN_GAP in benchmark.NEVER_ALLOCABLE
        assert lenient.sum() > 0, "the dataset must be benchmarkable at all"
        assert lenient.sum() >= strict.sum(), "lenient admits at least what strict does"
        unknown = [
            i
            for i, r in enumerate(rows_as_dicts(table))
            if r["allocation_quality"] == ALLOCATION_UNKNOWN_GAP
        ]
        assert all(not lenient[i] for i in unknown), "unknown_gap stays never-benchmarked"
        assert all(not strict[i] for i in unknown), "unknown_gap stays never-benchmarked (strict)"


# ===========================================================================
# 19. Item A: the counter quantum is MEASURED, and 0.0016 kWh was false
# ===========================================================================


#: The archive's measured lattice (GCD of all 466,460 positive 2025 increments).
#: 0.0016 kWh was a small-sample coincidence: zero real increments equal it.
ARCHIVE_LATTICE_KWH = 0.0001


class TestQuantisationIsMeasured:
    def test_constant_is_the_measured_lattice_not_the_old_guess(self) -> None:
        """The fallback constant documents a measurement, and says so.

        ``0.0016`` with the word "verified" is the claim this test forbids:
        asserting it here would let the old value sneak back in as a default.
        """
        assert COUNTER_QUANTISATION_KWH == pytest.approx(ARCHIVE_LATTICE_KWH)
        doc = inspect.getdoc(build_mod.COUNTER_QUANTISATION_KWH) or ""
        assert "verified" not in doc.lower(), "a constant is not a verification"
        module_doc = build_mod.__doc__ or ""
        assert "0.0001 kWh" in module_doc
        assert "every observed delta is an integer multiple of\nit" not in module_doc

    def test_gcd_is_measured_from_the_loaded_increments(self) -> None:
        """A 0.0001 kWh lattice measures 0.0001, and 0.0016 divides almost nothing.

        This is the full-population result compressed into 300 samples: every
        increment is an integer multiple of the measured GCD, while a divisor that
        is not the lattice reaches only its chance share.  Asserting the *share*
        rather than a number is what makes the old 1.6 Wh reasoning impossible.
        """
        values = [14_612.0]
        for i in range(300):
            # Steps of 1, 2 and 3 lattice units — all multiples of 0.0001 kWh.
            values.append(values[-1] + ARCHIVE_LATTICE_KWH * (1 + (i % 3)))
        series = timeline(
            [grid_row(_T0 + timedelta(seconds=60 * i), v) for i, v in enumerate(values)]
        )
        measurement = build_mod.measure_counter_quantisation(series)
        assert measurement["basis"] == "measured_gcd"
        assert measurement["quantisation_kwh"] == pytest.approx(ARCHIVE_LATTICE_KWH)
        assert measurement["positive_increment_count"] == 300
        assert measurement["negative_increment_count"] == 0, "strictly monotonic, measured"
        assert measurement["monotonic"] is True
        by_candidate = measurement["candidate_divisibility"]
        assert by_candidate["0.000100"]["share_divisible_percent"] == pytest.approx(100.0)
        # 0.0016 is not the lattice: it divides a minority of increments, and no
        # increment equals it.  Chance on this lattice is 100/1600 = 6.25 %.
        assert by_candidate["0.001600"]["increments_exactly_equal"] == 0
        assert by_candidate["0.001600"]["share_divisible_percent"] < 100.0
        assert by_candidate["0.001600"][
            "chance_share_percent_on_measured_lattice"
        ] == pytest.approx(6.25)

    def test_a_divisor_that_is_not_the_lattice_never_reaches_full_share(self) -> None:
        """The exact test that refuted 0.0016: divisibility is not the same as quantum.

        With increments of 0.0003 kWh (three lattice units), a larger divisor can
        divide a minority share even though it looks "special" on a short sample.
        """
        values = [100.0, 100.0 + 0.0003]
        for _ in range(40):
            values.append(values[-1] + 0.0003)
        series = timeline(
            [grid_row(_T0 + timedelta(seconds=60 * i), v) for i, v in enumerate(values)]
        )
        measurement = build_mod.measure_counter_quantisation(series)
        assert measurement["quantisation_kwh"] == pytest.approx(0.0003)
        candidates = measurement["candidate_divisibility"]
        assert candidates["0.001600"]["share_divisible_percent"] < 100.0
        assert candidates["0.001600"]["increments_exactly_equal"] == 0

    def test_manifest_publishes_the_measurement_with_its_sample_size(
        self, tmp_path: Path, data_dir: Path
    ) -> None:
        cfg = write_tariff(tmp_path)
        write_raw_part(data_dir, counter_rows(_T0, 121, per_sample=0.0002))
        _table, manifest = build_rows(
            data_dir, cfg, _T0, _T0 + timedelta(hours=2), lookback=timedelta(minutes=5)
        )
        block = manifest["boundary_allocation"]
        measurement = block["quantisation_measurement"]
        assert measurement["method"] == "gcd_of_positive_increments_in_micro_kwh"
        # 121 samples at 60 s, but the half-open read window drops the last one at
        # exactly T0+2h → 120 samples → 119 increments, all positive.
        assert measurement["positive_increment_count"] == 119
        assert measurement["negative_increment_count"] == 0
        assert block["quantisation_kwh"] == pytest.approx(measurement["quantisation_kwh"])
        assert block["quantisation_kwh"] == pytest.approx(0.0002)
        assert block["quantisation_fallback_used"] is False

    def test_manifest_falls_back_only_when_nothing_can_be_measured(
        self, tmp_path: Path, data_dir: Path
    ) -> None:
        """A flat counter has no positive increment: the constant is used, and labelled.

        The fallback is disclosed instead of masquerading as a measurement, which
        is exactly the distinction the 0.0016 claim destroyed.
        """
        cfg = write_tariff(tmp_path)
        flat = [grid_row(_T0 + timedelta(seconds=300 * i), 14_612.0) for i in range(13)]
        write_raw_part(data_dir, flat)
        _table, manifest = build_rows(
            data_dir, cfg, _T0, _T0 + timedelta(hours=1), lookback=timedelta(minutes=5)
        )
        block = manifest["boundary_allocation"]
        assert block["quantisation_measurement"]["quantisation_kwh"] is None
        assert block["quantisation_fallback_used"] is True
        assert block["quantisation_kwh"] == pytest.approx(COUNTER_QUANTISATION_KWH)

    def test_zero_increments_do_not_break_the_lattice_measurement(self) -> None:
        """Repeated readings (the aliased minutes) are excluded from the GCD."""
        values = [500.0]
        for i in range(1, 60):
            values.append(values[-1] + (0.0002 if i % 5 else 0.0))
        series = timeline(
            [grid_row(_T0 + timedelta(seconds=60 * i), v) for i, v in enumerate(values)]
        )
        measurement = build_mod.measure_counter_quantisation(series)
        assert measurement["zero_increment_count"] == 11
        assert measurement["quantisation_kwh"] == pytest.approx(0.0002)


# ===========================================================================
# 20. Item B: every kWh that enters the build is declared or the build fails
# ===========================================================================


class TestEnergyReconciliation:
    def test_a_clean_build_closes_the_identity(self, tmp_path: Path, data_dir: Path) -> None:
        cfg = write_tariff(tmp_path)
        rows = counter_rows(_T0, 121, times=counter_times(_T0, 121, 60.0))
        rows += price_rows(_T0, {0: 0.1, 1: 0.2}, per_minute=6, step_seconds=10.0)
        write_raw_part(data_dir, rows)
        table, manifest = build_rows(
            data_dir, cfg, _T0, _T0 + timedelta(hours=2), lookback=timedelta(minutes=5)
        )
        recon = manifest["energy_reconciliation"]
        values = [v for v in values_for(table, "raw_grid_kwh") if v is not None]
        assert recon["allocated_kwh"] == pytest.approx(sum(values), abs=1e-12)
        assert abs(recon["residual_kwh"]) <= recon["residual_tolerance_kwh"]
        assert recon["within_tolerance"] is True
        assert recon["quality_flag"] == "ok"
        terms = recon["terms"]
        closed = recon["allocated_kwh"] + sum(terms.values()) + recon["residual_kwh"]
        assert closed == pytest.approx(recon["counter_endpoint_kwh"], abs=1e-9)
        # The tolerance is documented, not implied.
        assert recon["residual_tolerance_kwh"] == build_mod.DEFAULT_RECONCILIATION_TOLERANCE_KWH
        assert "identity" in recon and "term_meanings" in recon

    def test_energy_nulled_by_a_gap_is_declared_not_dropped(
        self, tmp_path: Path, data_dir: Path
    ) -> None:
        """The exact 8.1229 kWh shape: a gap nulls an interval that held real energy.

        Two ordinary minutes land inside ``[00:15, 00:30)``, then a multi-interval
        outage touches that same interval, which therefore publishes nothing.  The
        energy it had been given used to vanish; now it is its own reconciliation
        term, and the identity still closes.
        """
        cfg = write_tariff(tmp_path)
        before = [
            grid_row(_T0, 100.0),
            grid_row(_T0 + timedelta(seconds=60), 100.1),
            grid_row(_T0 + timedelta(seconds=900), 100.2),  # 00:15 → inside interval 1
            grid_row(_T0 + timedelta(seconds=960), 100.3),
        ]
        after = [
            grid_row(_T0 + timedelta(seconds=2760), 100.9),  # 00:46 → a ~30 min outage
            grid_row(_T0 + timedelta(seconds=2820), 101.0),
        ]
        write_raw_part(data_dir, before + after)
        table, manifest = build_rows(
            data_dir,
            cfg,
            _T0,
            _T0 + timedelta(minutes=50),
            lookback=timedelta(minutes=5),
            gap_threshold_seconds=1200.0,
        )
        recon = manifest["energy_reconciliation"]
        ledger = manifest["unattributed_spans"]
        assert ledger["gap_span_count"] >= 1
        assert ledger["unattributed_interval_count"] >= 1
        nulled = recon["terms"]["nulled_covered_kwh"]
        assert nulled > 0.0, "energy attributed inside an interval later nulled must be declared"
        assert recon["terms"]["gap_span_kwh"] > 0.0
        assert abs(recon["residual_kwh"]) <= recon["residual_tolerance_kwh"]
        assert recon["quality_flag"] == "ok"
        # The declared leak is exactly what a reader would otherwise lose.
        endpoint = recon["counter_endpoint_kwh"]
        assert recon["allocated_kwh"] < endpoint
        assert recon["declared_unattributed_kwh"] > 0.0

    def test_reconciliation_catches_a_known_leak_when_a_term_is_omitted(self) -> None:
        """Drop one declared category and the residual *is* the leak.

        2025 numbers: endpoint 5,546.7085 kWh, allocated 5,521.2218 kWh, gap spans
        17.3638 kWh.  Declaring only the gap spans leaves 8.1229 kWh unexplained —
        the silent leak the manifest used to ship — so the check must reject it.
        """
        complete = build_mod.reconcile_energy(
            counter_endpoint_kwh=5546.7085,
            allocated_kwh=5521.2218,
            nulled_covered_kwh=8.1229,
            gap_span_kwh=17.3638,
        )
        assert complete.residual_kwh == pytest.approx(0.0, abs=1e-9)
        assert complete.within_tolerance
        build_mod.require_reconciled(complete)  # must not raise

        leaking = build_mod.reconcile_energy(
            counter_endpoint_kwh=5546.7085,
            allocated_kwh=5521.2218,
            gap_span_kwh=17.3638,  # the nulled term is missing, as it used to be
        )
        assert leaking.residual_kwh == pytest.approx(8.1229, abs=1e-9)
        assert not leaking.within_tolerance
        assert leaking.quality_flag == "residual_out_of_tolerance"
        with pytest.raises(build_mod.ReconciliationError, match="unexplained residual"):
            build_mod.require_reconciled(leaking)

    def test_out_of_window_and_partial_terms_are_declared(self) -> None:
        """Spans outside the grid appear as their own term, never as a residual."""
        series = timeline(
            [
                grid_row(_T0 - timedelta(minutes=30), 10.0),
                grid_row(_T0 - timedelta(minutes=20), 10.5),  # wholly before the grid
                grid_row(_T0, 10.6),
                grid_row(_T0 + timedelta(minutes=10), 10.7),
            ]
        )
        spans = counter_deltas(series, gap_threshold_seconds=1e9)
        energies, ledger = allocate_energy(
            series, spans, grid_times(_T0, _T0 + timedelta(minutes=15))
        )
        recon = ledger.reconciliation()
        # Both pre-grid spans (10.0→10.5 and 10.5→10.6) are outside the half-open
        # grid, so 0.6 kWh is declared as outside-window rather than dropped.
        assert ledger.outside_grid_kwh == pytest.approx(0.6, abs=1e-12)
        assert ledger.outside_grid_span_count == 2
        assert recon.residual_kwh == pytest.approx(0.0, abs=1e-12), (
            "the pre-window 0.5 kWh is declared, so nothing is left unexplained"
        )
        build_mod.require_reconciled(recon)

    def test_reset_counter_change_is_a_declared_term(self) -> None:
        """A replaced counter contributes its step to the identity, not silence."""
        series = timeline(
            [
                grid_row(_T0, 9000.0),
                grid_row(_T0 + timedelta(seconds=60), 9001.0),
                grid_row(_T0 + timedelta(seconds=120), 12.0),  # counter replaced
                grid_row(_T0 + timedelta(seconds=180), 12.5),
            ]
        )
        spans = counter_deltas(series, gap_threshold_seconds=1e9)
        energies, ledger = allocate_energy(
            series, spans, grid_times(_T0, _T0 + timedelta(minutes=3))
        )
        recon = ledger.reconciliation()
        assert ledger.reset_counter_change_kwh == pytest.approx(-9001.0 + 12.0, abs=1e-9)
        assert ledger.reset_span_count == 1
        assert all(e.raw_grid_kwh is None or e.raw_grid_kwh >= 0.0 for e in energies)
        assert recon.residual_kwh == pytest.approx(0.0, abs=1e-9), (
            "the negative counter step is declared; no energy was invented from it"
        )
        build_mod.require_reconciled(recon)

    def test_build_is_refused_and_nothing_is_published_when_energy_does_not_close(
        self,
        tmp_path: Path,
        data_dir: Path,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        """Fail loudly, before writing: an unexplained kWh cannot reach a dataset.

        A content-addressed part is no-clobber, so publishing first and checking
        after would leave the leaking dataset on disk forever — the patched ledger
        below reproduces the historical silent drop, and the run must abort.
        """
        cfg = write_tariff(tmp_path)
        # Data that genuinely produces nulled covered energy: ordinary minutes plus
        # an outage that touches the same intervals.
        rows = [
            grid_row(_T0, 100.0),
            grid_row(_T0 + timedelta(seconds=60), 100.1),
            grid_row(_T0 + timedelta(seconds=900), 100.2),
            grid_row(_T0 + timedelta(seconds=960), 100.3),
            grid_row(_T0 + timedelta(seconds=2760), 100.9),
            grid_row(_T0 + timedelta(seconds=2820), 101.0),
        ]
        rows += price_rows(_T0, {0: 0.1}, per_minute=3, step_seconds=20.0)
        write_raw_part(data_dir, rows)

        real = build_mod.SpanLedger.reconciliation

        def dropping_nulled(self: Any, **kwargs: Any) -> Any:
            recon = real(self, **kwargs)
            # Simulate the old behaviour: the nulled category is never declared.
            return build_mod.reconcile_energy(
                counter_endpoint_kwh=recon.counter_endpoint_kwh,
                allocated_kwh=recon.allocated_kwh,
                gap_span_kwh=recon.gap_span_kwh,
                outside_grid_kwh=recon.outside_grid_kwh,
                partial_coverage_kwh=recon.partial_coverage_kwh,
                reset_counter_change_kwh=recon.reset_counter_change_kwh,
                tolerance_kwh=recon.tolerance_kwh,
            )

        monkeypatch.setattr(build_mod.SpanLedger, "reconciliation", dropping_nulled)
        with pytest.raises(build_mod.ReconciliationError):
            build(data_dir, cfg, _T0, _T0 + timedelta(hours=1), lookback=timedelta(minutes=5))
        assert not (data_dir / "analysis").exists(), (
            "a refused build must not leave a dataset directory behind"
        )

    def test_reconciliation_survives_the_gap_and_reset_paths(
        self, tmp_path: Path, data_dir: Path
    ) -> None:
        """Gaps, resets, nulled intervals and edge spans together still balance."""
        cfg = write_tariff(tmp_path)
        rows = counter_rows(
            _T0, 41, first=100.0, per_sample=0.01, times=aligned_times(_T0, 41, 60.0)
        )
        rows += counter_rows(
            _T0 + timedelta(minutes=50),
            21,
            first=101.5,
            per_sample=0.01,
            times=aligned_times(_T0 + timedelta(minutes=50), 21, 60.0),
        )
        rows += price_rows(_T0, {0: 0.1, 1: 0.2}, per_minute=3, step_seconds=20.0)
        write_raw_part(data_dir, rows)
        _table, manifest = build_rows(
            data_dir,
            cfg,
            _T0,
            _T0 + timedelta(hours=1, minutes=30),
            lookback=timedelta(minutes=5),
            gap_threshold_seconds=900.0,
        )
        recon = manifest["energy_reconciliation"]
        assert recon["quality_flag"] == "ok"
        assert abs(recon["residual_kwh"]) <= recon["residual_tolerance_kwh"]
        assert manifest["coverage"]["table_allocated_kwh"] == pytest.approx(
            manifest["coverage"]["ledger_allocated_kwh"], abs=1e-9
        ), "declared allocation must equal what the Parquet column actually holds"


# ===========================================================================
# 21. Item C: an exclusion is recorded only if the entity was really present
# ===========================================================================


class TestExclusionTruthfulness:
    def test_absent_configured_exclusions_are_not_claimed_as_exclusions(
        self, tmp_path: Path, data_dir: Path
    ) -> None:
        """The real archive's shape: four entities exist, so only those can be excluded.

        ``sensor.shellyem_main_channel_2_energy`` and ``sensor.esios_pvpc_2`` appear
        in no raw manifest and in no part, and ``raw_rows_read`` is exactly the
        energy plus price row count.  Claiming to have excluded them was vacuous, so
        an entity that was never in the inputs must land in a different key.
        """
        cfg = write_tariff(tmp_path)
        rows = counter_rows(_T0, 13, times=aligned_times(_T0, 13, 300.0))
        rows += price_rows(_T0, {0: 0.1}, per_minute=6, step_seconds=10.0)
        write_raw_part(data_dir, rows)
        _table, manifest = build_rows(
            data_dir, cfg, _T0, _T0 + timedelta(hours=1), lookback=timedelta(minutes=5)
        )
        params = manifest["params"]
        assert params["nested_submeters_excluded_from_gross"] == []
        assert params["dead_duplicates_excluded"] == []
        absent = {str(item["entity"]) for item in params["configured_but_absent_from_inputs"]}
        assert absent == set(NESTED_SUBMETER_ENTITIES) | set(DEAD_DUPLICATE_ENTITY_IDS)
        for item in params["configured_but_absent_from_inputs"]:
            assert item["status"] == "never_observed_in_any_input"
        exclusions = manifest["input_exclusions"]
        assert exclusions["observed_and_dropped"] == []
        assert exclusions["entities_observed_in_inputs"] == [PRICE, GRID]
        assert exclusions["entities_declared_by_raw_manifests"] == [PRICE, GRID]
        assert manifest["coverage"]["raw_rows_read"] == (
            manifest["coverage"]["grid_samples"] + manifest["coverage"]["price_samples"]
        ), "nothing else was read, so nothing else could have been dropped"

    def test_observed_exclusions_are_recorded_as_real_exclusions(
        self, tmp_path: Path, data_dir: Path
    ) -> None:
        """The other path: the entity really is in the inputs, so the claim holds."""
        cfg = write_tariff(tmp_path)
        rows = counter_rows(_T0, 13, times=aligned_times(_T0, 13, 300.0))
        rows += price_rows(_T0, {0: 0.1}, per_minute=6, step_seconds=10.0)
        rows += ch2_row_times(_T0, 13, first=1.0, per_sample=0.1)
        rows += dead_price_rows(_T0, 13, value=0.999)
        write_raw_part(data_dir, rows)
        _table, manifest = build_rows(
            data_dir, cfg, _T0, _T0 + timedelta(hours=1), lookback=timedelta(minutes=5)
        )
        params = manifest["params"]
        assert params["nested_submeters_excluded_from_gross"] == [CH2]
        assert params["dead_duplicates_excluded"] == [DEAD_PRICE]
        absent = {str(item["entity"]) for item in params["configured_but_absent_from_inputs"]}
        assert CH2 not in absent and DEAD_PRICE not in absent
        assert absent == {POWER, RETURNED}, "only what this build never saw"
        dropped = {
            str(item["entity"]) for item in manifest["input_exclusions"]["observed_and_dropped"]
        }
        assert dropped == {CH2, DEAD_PRICE}

    def test_declared_but_out_of_window_entity_is_a_third_state(
        self, tmp_path: Path, data_dir: Path
    ) -> None:
        """Declared by a manifest yet unread by this window: neither exclusion nor silence."""
        cfg = write_tariff(tmp_path)
        in_window = counter_rows(_T0, 13, times=aligned_times(_T0, 13, 300.0))
        in_window += price_rows(_T0, {0: 0.1}, per_minute=6, step_seconds=10.0)
        write_raw_part(data_dir, in_window, "part-in-window.parquet")
        # A second manifest declares the power entity, but its chunk lies entirely
        # outside [start - lookback, end), so this build never reads those rows.
        late_start = _T0 + timedelta(days=40)
        late = counter_rows(late_start, 5, times=aligned_times(late_start, 5, 300.0))
        late += power_row_times(late_start, 5, watts=250.0)
        write_raw_part(data_dir, late, "part-out-of-window.parquet")
        _table, manifest = build_rows(
            data_dir, cfg, _T0, _T0 + timedelta(hours=1), lookback=timedelta(minutes=5)
        )
        items = {
            str(item["entity"]): item
            for item in manifest["params"]["configured_but_absent_from_inputs"]
        }
        assert items[POWER]["status"] == "declared_by_a_raw_manifest_but_not_in_this_window"
        assert items[RETURNED]["status"] == "never_observed_in_any_input"
        assert POWER not in manifest["params"]["nested_submeters_excluded_from_gross"]


# ===========================================================================
# 22. Item D: look-ahead maxima are measured over every hour, never the cap
# ===========================================================================


class TestYearWideLookaheadDistribution:
    def _hours(self, count: int, offsets: dict[int, float]) -> list[dict[str, Any]]:
        rows: list[dict[str, Any]] = []
        for hour in range(count):
            top = _T0 + timedelta(hours=hour)
            offset = offsets.get(hour, 20.0 + (hour % 7))
            rows.append(price_row(top + timedelta(seconds=offset), 0.1 + 0.001 * hour))
            rows.append(price_row(top + timedelta(seconds=offset + 30), 0.1 + 0.001 * hour))
        return rows

    def test_maximum_is_found_even_when_it_falls_outside_the_capped_details(self) -> None:
        """A peak in hour 590 of 600 is still the reported maximum.

        ``details`` is capped at ``MAX_MANIFEST_DETAILS`` rows, all from the front
        of the window; the 2025 manifest showed 500 rows from 2025-01-01, every one
        of them at the same 34.733 s scrape phase.  A maximum taken from that
        sample describes one day, not the year — so the ledger accumulates every
        hour and this test puts the true peak beyond the cap.
        """
        peak = 2708.684
        offsets = {590: peak}
        rows = self._hours(600, offsets)
        grid = grid_times(_T0, _T0 + timedelta(hours=600))
        _aligned, ledger = align_price_hours(price_series(rows), grid, timezone=MADRID)
        block = ledger.as_dict()
        assert len(block["details"]) == build_mod.MAX_MANIFEST_DETAILS
        assert block["details_truncated"] is True
        assert block["hours_total"] == 600
        # The truncated sample contains only the front of the window: the peak hour
        # is provably not in it, yet the maximum is still reported.
        peak_hour = (_T0 + timedelta(hours=590)).isoformat().replace("+00:00", "Z")
        assert peak_hour not in {str(d["hour_start"]) for d in block["details"]}
        assert block["max_lookahead_seconds"] == pytest.approx(peak)
        percent = block["first_observation_offset_seconds"]
        assert percent["max_seconds"] == pytest.approx(peak)
        assert percent["hours_with_a_sample"] == 600
        assert percent["min_seconds"] < percent["median_seconds"] < peak
        assert percent["hours_first_observed_after_60_seconds"] == 1
        assert block["max_lookahead_basis"].startswith("measured over every hour")

    def test_details_are_labelled_as_a_sample_and_never_a_population(self) -> None:
        rows = self._hours(3, {})
        _aligned, ledger = align_price_hours(
            price_series(rows), grid_times(_T0, _T0 + timedelta(hours=3)), timezone=MADRID
        )
        block = ledger.as_dict()
        assert block["details_are_sample_not_population"] is True
        assert "must not be inferred" in block["details_note"]
        assert "NOT derived" in block["max_lookahead_basis"]
        assert block["first_observation_offset_seconds"]["method"].startswith("every hour")

    def test_percentiles_and_distinct_values_are_measured_over_all_hours(self) -> None:
        offsets = {hour: float(hour % 60) for hour in range(120)}
        rows = self._hours(120, offsets)
        _aligned, ledger = align_price_hours(
            price_series(rows), grid_times(_T0, _T0 + timedelta(hours=120)), timezone=MADRID
        )
        percent = ledger.as_dict()["first_observation_offset_seconds"]
        assert percent["distinct_whole_second_values"] == 60
        assert percent["min_seconds"] == pytest.approx(0.0)
        assert percent["max_seconds"] == pytest.approx(59.0)
        agreement = ledger.as_dict()["hour_value_agreement"]
        assert agreement["hours_with_samples"] == 120
        assert agreement["single_valued_hours"] == 120
        assert agreement["max_distinct_values_observed_in_one_hour"] == 1

    def test_hours_that_carry_the_previous_price_are_counted(self) -> None:
        """The shape that makes the hourly assumption real: old price visible first.

        Hour 1's first sample still reports hour 0's price and the new value appears
        at +61 s, so the interval grid is priced from data that had not yet switched
        for the first minute — declared in the transitions block, not smoothed over.
        """
        rows: list[dict[str, Any]] = []
        rows += [price_row(_T0 + timedelta(seconds=20), 0.10)]
        rows += [price_row(_T0 + timedelta(seconds=80), 0.10)]
        rows += [
            price_row(_T0 + timedelta(hours=1, seconds=5), 0.10),  # still the OLD price
            price_row(_T0 + timedelta(hours=1, seconds=61), 0.20),
            price_row(_T0 + timedelta(hours=1, seconds=121), 0.20),
        ]
        rows += [price_row(_T0 + timedelta(hours=2, seconds=25), 0.30)]
        _aligned, ledger = align_price_hours(
            price_series(rows), grid_times(_T0, _T0 + timedelta(hours=3)), timezone=MADRID
        )
        transitions = ledger.as_dict()["transitions"]
        assert transitions["price_changing_hours"] == 2
        assert transitions["hours_whose_first_sample_carried_the_previous_price"] == 1
        # Hour 1's new price becomes visible at +61 s; hour 2's arrives at +25 s.
        assert transitions["offset_from_hour_top_buckets"] == {
            "6_30_seconds": 1,
            "61_120_seconds": 1,
        }
        assert ledger.as_dict()["hour_value_agreement"]["multi_valued_hours_by_count"] == {"2": 1}

    def test_no_published_text_claims_a_fixed_scrape_offset(self) -> None:
        """The falsified ":39" and "~60 s" wording must not come back."""
        note = build_mod.PRICE_LOOKAHEAD_NOTE
        assert ":00:39" not in note and "39 s" not in note and "~60 s" not in note
        doc = build_mod.__doc__ or ""
        assert "H:00:39" not in doc
        assert "truncated sample" in doc, "the cap on details is documented"
        assert "effective" in doc, "the missing effective-time provenance is documented"

    def test_publication_provenance_is_recorded_in_the_manifest(
        self, tmp_path: Path, data_dir: Path
    ) -> None:
        cfg = write_tariff(tmp_path)
        rows = counter_rows(_T0, 13, times=aligned_times(_T0, 13, 300.0))
        rows += price_rows(_T0, {0: 0.1}, per_minute=6, step_seconds=10.0)
        write_raw_part(data_dir, rows)
        _table, manifest = build_rows(
            data_dir, cfg, _T0, _T0 + timedelta(hours=1), lookback=timedelta(minutes=5)
        )
        block = manifest["price_alignment"]
        assert block["publication_provenance"] == build_mod.PRICE_PUBLICATION_PROVENANCE
        # Measured from the raw rows, not transcribed from a docstring.
        assert "entity" in block["observed_label_keys"]
        assert "instance" in block["observed_label_keys"]
        assert not any(
            "effective" in key or "staleness" in key for key in block["observed_label_keys"]
        )


# ===========================================================================
# 23. Item E: meter identity is a dated user assertion, and the export channel
#     is measured to be dead
# ===========================================================================


class TestMeterIdentityProvenance:
    def test_meter_identity_is_recorded_as_an_assertion_not_a_measurement(
        self, tmp_path: Path, data_dir: Path
    ) -> None:
        cfg = write_tariff(tmp_path)
        rows = counter_rows(_T0, 13, times=aligned_times(_T0, 13, 300.0))
        rows += price_rows(_T0, {0: 0.1}, per_minute=6, step_seconds=10.0)
        write_raw_part(data_dir, rows)
        _table, manifest = build_rows(
            data_dir, cfg, _T0, _T0 + timedelta(hours=1), lookback=timedelta(minutes=5)
        )
        identity = manifest["meter_identity"]
        assert identity["kind"] == "user_assertion", "not a measurement, not a detection"
        assert identity["verified_from_data"] is False
        assert identity["asserted_on"] == "2026-10-23"
        assert identity["mapping"][GRID].startswith("whole-home")
        assert "subcircuit" in identity["not_verified_because"]
        assert build_mod.METER_IDENTITY_ASSERTION["kind"] == "user_assertion"

    def test_constant_export_channel_is_measured_and_marked_unused(
        self, tmp_path: Path, data_dir: Path
    ) -> None:
        """An export counter stuck at 0.0 is proven by value range, not asserted.

        ``value_min == value_max`` across every part that carries the entity is an
        exact statement of "one distinct value": in the real archive that channel
        read 0.0 kWh in all 520,151 samples, i.e. no export was measured all year.
        """
        cfg = write_tariff(tmp_path)
        rows = counter_rows(_T0, 13, times=aligned_times(_T0, 13, 300.0))
        rows += price_rows(_T0, {0: 0.1}, per_minute=6, step_seconds=10.0)
        rows += [returned_row(_T0 + timedelta(seconds=300 * i), 0.0) for i in range(13)]
        write_raw_part(data_dir, rows)
        _table, manifest = build_rows(
            data_dir, cfg, _T0, _T0 + timedelta(hours=1), lookback=timedelta(minutes=5)
        )
        audit = {str(item["entity"]): item for item in manifest["entity_audit"]}
        entry = audit[RETURNED]
        assert entry["constant_value"] is True
        assert entry["value_min"] == pytest.approx(0.0)
        assert entry["value_max"] == pytest.approx(0.0)
        assert entry["used_for"] == "not_used"
        assert entry["samples"] == 13
        identity = manifest["meter_identity"]
        assert identity["observed_channels"][RETURNED]["constant_value"] is True
        assert identity["export_channel_note"] == build_mod.EXPORT_CHANNEL_PROVENANCE_NOTE

    def test_a_moving_channel_is_not_reported_as_constant(
        self, tmp_path: Path, data_dir: Path
    ) -> None:
        cfg = write_tariff(tmp_path)
        rows = counter_rows(_T0, 13, times=aligned_times(_T0, 13, 300.0))
        rows += price_rows(_T0, {0: 0.1}, per_minute=6, step_seconds=10.0)
        rows += [returned_row(_T0 + timedelta(seconds=300 * i), 0.5 * i) for i in range(13)]
        write_raw_part(data_dir, rows)
        _table, manifest = build_rows(
            data_dir, cfg, _T0, _T0 + timedelta(hours=1), lookback=timedelta(minutes=5)
        )
        entry = {str(item["entity"]): item for item in manifest["entity_audit"]}[RETURNED]
        assert entry["constant_value"] is False
        assert entry["value_min"] == pytest.approx(0.0)
        assert entry["value_max"] == pytest.approx(6.0)

    def test_entity_audit_records_observed_label_keys(self, tmp_path: Path, data_dir: Path) -> None:
        cfg = write_tariff(tmp_path)
        rows = counter_rows(_T0, 13, times=aligned_times(_T0, 13, 300.0))
        write_raw_part(data_dir, rows)
        _table, manifest = build_rows(
            data_dir, cfg, _T0, _T0 + timedelta(hours=1), lookback=timedelta(minutes=5)
        )
        entry = {str(item["entity"]): item for item in manifest["entity_audit"]}[GRID]
        assert entry["label_keys"] == sorted(entry["label_keys"])
        assert {"entity", "job", "pod"} <= set(entry["label_keys"])


# ===========================================================================
# 24. Item F: per-minute aliasing cannot move 15-minute energy
# ===========================================================================


def aliased_counter_rows(
    start: datetime,
    intervals: int,
    *,
    first: float = 14_612.0,
    tick: float = 0.0001,
) -> list[dict[str, Any]]:
    """15 samples per interval where one minute reads 0 and the next reads double.

    Each 15-minute block still accumulates exactly 15 ticks, so the counter readings
    at every interval boundary are identical to a smooth counter: this is the
    archive's aliasing shape (10.36 % of 60 s steps read zero, the next carrying two
    minutes) with the boundary deltas held fixed.
    """
    values: list[float] = [first]
    for block in range(intervals):
        for step in range(15):
            if step == 7:
                values.append(values[-1])  # no tick reported this minute
            elif step == 8:
                values.append(values[-1] + 2 * tick)  # the next minute carries two
            else:
                values.append(values[-1] + tick)
    stamps = [start + timedelta(seconds=60 * i) for i in range(len(values))]
    return [grid_row(t, v) for t, v in zip(stamps, values, strict=True)]


class TestPerMinuteAliasing:
    def test_aliased_minutes_leave_fifteen_minute_totals_untouched(self) -> None:
        """Zero-then-double minutes redistribute nothing across interval edges.

        Because each interval sums whole spans and the aliased pattern preserves the
        reading at every boundary, the per-interval energy must equal the counter
        difference across that interval exactly: a build that derived load by
        scaling a per-minute delta would put 0 W into one minute and double into the
        next, and this test would catch it.
        """
        rows = aliased_counter_rows(_T0, 4)
        series = timeline(rows)
        spans = counter_deltas(series, gap_threshold_seconds=1e9)
        zeros = sum(1 for s in spans if s.delta == 0.0)
        assert zeros == 4, "one aliased minute per interval: the pattern really is fed in"
        grid = grid_times(_T0, _T0 + timedelta(hours=1))
        energies, ledger = allocate_energy(series, spans, grid)
        boundaries = [first["value"] for first in (rows[0], rows[15], rows[30], rows[45], rows[60])]
        for k, energy in enumerate(energies):
            expected = boundaries[k + 1] - boundaries[k]
            assert energy.raw_grid_kwh == pytest.approx(expected, abs=1e-12), (
                f"interval {k}: boundary delta, not a scaled per-minute rate"
            )
            assert energy.energy_quality == ENERGY_MEASURED
        assert emitted(energies) == pytest.approx(boundaries[-1] - boundaries[0], abs=1e-12)
        # All spans sit inside intervals here, so nothing needed a boundary estimate.
        assert ledger.boundary_span_count == 0
        recon = ledger.reconciliation()
        assert recon.residual_kwh == pytest.approx(0.0, abs=1e-12)
        build_mod.require_reconciled(recon)

    def test_hourly_total_is_exact_under_irregular_offsets_too(self) -> None:
        """With straddling spans the whole window still telescopes to the counter.

        The per-interval figures may shift by at most the one straddling span, which
        is the disclosed ``estimated_boundary`` approximation; the total over any
        whole number of intervals is a counter difference, aliasing or not.
        """
        rows = aliased_counter_rows(_T0, 4)
        for row in rows:
            row["ts"] = row["ts"] + timedelta(seconds=34.733)
        series = timeline(rows)
        spans = counter_deltas(series, gap_threshold_seconds=1e9)
        grid = grid_times(_T0 + timedelta(minutes=1), _T0 + timedelta(hours=1, minutes=1))
        energies, ledger = allocate_energy(series, spans, grid)
        total = emitted(energies)
        assert total == pytest.approx(series.values[-1] - series.values[0], abs=1e-12)
        assert all(e.raw_grid_kwh is not None for e in energies)
        assert all(e.raw_grid_kwh >= 0.0 for e in energies)
        build_mod.require_reconciled(ledger.reconciliation())

    def test_nothing_in_the_energy_path_reads_the_power_series(
        self, tmp_path: Path, data_dir: Path
    ) -> None:
        """Wildly different power readings, byte-identical energy columns.

        The strongest form of "energy comes from counter deltas": the power series
        is present in the inputs but cannot influence a single kWh, so perturbing it
        by orders of magnitude must not move the output at all.
        """
        cfg = write_tariff(tmp_path)
        base = counter_rows(_T0, 61, times=counter_times(_T0, 61, 60.0))
        base += price_rows(_T0, {0: 0.1}, per_minute=6, step_seconds=10.0)
        write_raw_part(data_dir, base + power_row_times(_T0, 61, watts=5.0), "part-low.parquet")
        low_dir = build(data_dir, cfg, _T0, _T0 + timedelta(hours=1), lookback=timedelta(minutes=5))
        low = pq.read_table(next((low_dir / "parts").glob("*.parquet")))
        sorted((low_dir / "parts").glob("*.parquet"))[0].read_bytes()

        write_raw_part(
            data_dir,
            base + power_row_times(_T0, 61, watts=99_999.0),
            "part-high.parquet",
        )
        # Same window, same counter part, a second part that only adds power rows.
        high_dir = build(
            data_dir, cfg, _T0, _T0 + timedelta(hours=1), lookback=timedelta(minutes=5)
        )
        high = pq.read_table(next((high_dir / "parts").glob("*.parquet")))
        assert values_for(high, "raw_grid_kwh") == values_for(low, "raw_grid_kwh")
        assert values_for(high, "gross_kwh") == values_for(low, "gross_kwh")
        assert values_for(high, "energy_quality") == values_for(low, "energy_quality")
        assert _T0 + timedelta(hours=1) > _T0
        assert (low_dir / "parts").exists() and high_dir != low_dir

    def test_module_docstring_documents_the_measured_aliasing_rate(self) -> None:
        """The 10.36 % zero-minute rate and its cause are stated, not hand-waved."""
        doc = build_mod.__doc__ or ""
        assert "10.36" in doc, "aliasing rate measured on the full archive"
        assert "53,685" in doc or "53,684" in doc
        assert "update aliasing" in doc

    def test_module_docstring_states_the_single_counter_split(self) -> None:
        """The two 2025 series are one physical counter, and the docstring says so."""
        doc = build_mod.__doc__ or ""
        assert "2025-06-16T05:00:09.902Z" in doc
        assert "144.236" in doc
        assert "344 W" in doc
        assert "not a reset" in doc
