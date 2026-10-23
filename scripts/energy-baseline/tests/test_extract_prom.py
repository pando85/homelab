"""Offline tests for ``extract_prom`` — ``httpx.MockTransport`` only, never the live server.

The fake Prometheus below emulates the semantics the extractor depends on: an
instant query whose selector is a range vector ``metric{entity="..."}[W]``
evaluated at ``time=T`` returns the raw samples in the **left-open,
right-closed** interval ``(T - W, T]``.  That makes these tests sensitive to the
exact defect the padding second exists to prevent (a sample sitting precisely on
``c_start``) and to any accidental ``query_range``-style step grid.
"""

from __future__ import annotations

import hashlib
import json
import logging
import re
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any

import httpx
import pyarrow as pa
import pyarrow.parquet as pq
import pytest

from energy_baseline import extract_prom
from energy_baseline.extract_prom import (
    BASE_URL_ENV_VAR,
    DEAD_DUPLICATE_ENTITY_IDS,
    DEFAULT_BASE_URL,
    DEFAULT_CHUNK_SECONDS,
    DEFAULT_ENTITY_IDS,
    ENTITY_SPECS,
    PART_KIND,
    RANGE_PADDING_SECONDS,
    SOURCE,
    EntitySpec,
    NonFiniteSampleError,
    PrometheusHTTPError,
    PrometheusQueryError,
    build_range_query,
    canonical_labels_json,
    compute_series_key,
    escape_promql_string,
    extract_prometheus,
    format_epoch_time,
    iso_z,
    iter_chunks,
    matrix_to_rows,
    parse_epoch_time,
    parse_utc,
    range_window_seconds,
    resolve_base_url,
    resolve_entity_specs,
    to_micros,
    validate_raw,
)
from energy_baseline.manifest import read_manifest
from energy_baseline.paths import iter_parts, part_path, parts_dir
from energy_baseline.schemas import RAW_SAMPLE

# ---------------------------------------------------------------------------
# Fake server + helpers
# ---------------------------------------------------------------------------

_T0 = datetime(2024, 1, 1, tzinfo=UTC)
_BASE = to_micros(_T0) / 1_000_000  # 1704067200.0

_QUERY_RE = re.compile(
    r'^(?P<metric>[a-zA-Z_:][a-zA-Z0-9_:]*)\{entity="(?P<entity>[^"]*)"\}\[(?P<window>\d+)s\]$'
)


class FakePrometheus:
    """Serves raw samples from memory with true Prometheus range-vector semantics."""

    def __init__(
        self,
        series: list[tuple[dict[str, str], list[tuple[float, Any]]]],
        *,
        responder: Any = None,
    ) -> None:
        self.series = series
        self.responder = responder
        self.requests: list[httpx.Request] = []

    @property
    def client(self) -> httpx.Client:
        return httpx.Client(transport=httpx.MockTransport(self.handle))

    def handle(self, request: httpx.Request) -> httpx.Response:
        self.requests.append(request)
        if self.responder is not None:
            return self.responder(request, len(self.requests) - 1)
        if request.url.path != "/api/v1/query":
            return httpx.Response(404, json={"status": "error", "error": "unknown path"})
        query = request.url.params.get("query", "")
        match = _QUERY_RE.match(query)
        if match is None:
            return httpx.Response(
                400,
                json={
                    "status": "error",
                    "errorType": "bad_data",
                    "error": f"invalid query {query!r}",
                },
            )
        end = float(request.url.params.get("time", "0"))
        start = end - int(match["window"])
        result: list[dict[str, Any]] = []
        for labels, samples in self.series:
            if labels.get("__name__") != match["metric"] or labels.get("entity") != match["entity"]:
                continue
            values = sorted([ts, str(value)] for ts, value in samples if start < ts <= end)
            if values:
                result.append({"metric": labels, "values": values})
        return httpx.Response(
            200, json={"status": "success", "data": {"resultType": "matrix", "result": result}}
        )


def labels_for(
    entity: str,
    *,
    metric: str,
    instance: str,
    job: str,
    pod: str = "home-assistant-0",
) -> dict[str, str]:
    """A realistic Home Assistant Prometheus label set."""
    return {
        "__name__": metric,
        "entity": entity,
        "instance": instance,
        "job": job,
        "pod": pod,
        "pod_ip": instance.rsplit(":", 1)[0],
        "service": "home-assistant",
        "namespace": "home-assistant",
        "endpoint": "http",
        "domain": "home-assistant",
        "friendly_name": entity.removeprefix("sensor."),
    }


def series_for(
    entity_id: str, samples: list[tuple[float, Any]], **label_overrides: str
) -> list[Any]:
    """``[(labels, [(epoch_seconds, value), ...])]`` for one registered entity."""
    spec = ENTITY_SPECS[entity_id]
    labels = labels_for(
        entity_id,
        metric=spec.metric,
        instance=label_overrides.pop("instance", "10.42.0.9:8123"),
        job=label_overrides.pop("job", "home-assistant"),
        **label_overrides,
    )
    return [(labels, samples)]


def matrix_for(entity_id: str, samples: list[tuple[float, Any]]) -> list[dict[str, Any]]:
    """A single-series matrix, exactly as the API would return it."""
    labels = series_for(entity_id, [(_BASE, 0)])[0][0]
    return [{"metric": labels, "values": [[ts, str(v)] for ts, v in samples]}]


def read_rows(data_dir: Path) -> pa.Table:
    """Concatenate every part of the prometheus source (these tests use few)."""
    frames = [pq.read_table(p) for p in iter_parts(data_dir, SOURCE)]
    assert frames, "no parts were written"
    return frames[0] if len(frames) == 1 else pa.concat_tables(frames)


def timestamps(table: pa.Table) -> list[int]:
    return [to_micros(t) for t in table.column("ts").to_pylist()]


def epoch_us(offset: float) -> int:
    """Exact micros since epoch at ``_BASE + offset`` (Prometheus uses ms precision)."""
    return int(round((_BASE + offset) * 1_000_000))


@pytest.fixture
def config(tmp_path: Path) -> Path:
    path = tmp_path / "tariff.yaml"
    path.write_text("version: 1\nbattery:\n  capacity_kwh: 5.12\n  power_kw: 2.5\n")
    return path


def run_extract(tmp_path: Path, client: httpx.Client, config: Path, **overrides) -> Path:
    """Call the public entry point with test-friendly defaults (1h chunks)."""
    kwargs: dict[str, Any] = {
        "data_dir": str(tmp_path / "data"),
        "config_path": str(config),
        "start": "2024-01-01T00:00:00Z",
        "end": "2024-01-02T00:00:00Z",
        "entities": [DEFAULT_ENTITY_IDS[0]],
        "client": client,
        "chunk_seconds": 3600,
    }
    kwargs.update(overrides)
    return extract_prometheus(**kwargs)


def manifest_of(path: Path):
    """Load the manifest the extractor returned by its own extraction id."""
    return read_manifest(path.parent.parent, json.loads(path.read_text())["extraction_id"])


class TestFakeServerSemantics:
    """The fake must be faithful before any assertion built on it means anything."""

    def test_range_vector_is_left_open_right_closed(self, tmp_path: Path) -> None:
        spec = ENTITY_SPECS[DEFAULT_ENTITY_IDS[0]]
        # Sample at T is included (right-closed)
        fake = FakePrometheus(series_for(spec.entity_id, [(_BASE, 1.0)]))
        client = fake.client
        try:
            exactly_span = extract_prom.query_range_vector(
                client,
                base_url="http://x",
                query=build_range_query(spec, 1),
                time_us=epoch_us(0),
                max_retries=0,
            )
            # Sample at T-1 would be excluded (left-open)
            fake_left = FakePrometheus(series_for(spec.entity_id, [(_BASE - 1, 2.0)]))
            left_edge = extract_prom.query_range_vector(
                fake_left.client,
                base_url="http://x",
                query=build_range_query(spec, 1),
                time_us=epoch_us(0),
                max_retries=0,
            )
            # Sample at T+1 is never returned (past the query time)
            fake_right = FakePrometheus(series_for(spec.entity_id, [(_BASE + 1, 3.0)]))
            right_edge = extract_prom.query_range_vector(
                fake_right.client,
                base_url="http://x",
                query=build_range_query(spec, 1),
                time_us=epoch_us(0),
                max_retries=0,
            )
        finally:
            client.close()
        assert len(exactly_span[0]["values"]) == 1  # sample at T is included
        assert left_edge == []  # sample at T-1 is excluded (left-open)
        assert right_edge == []  # sample at T+1 is excluded (past query time)

    def test_chunk_window_is_one_second_wider_than_the_chunk(self) -> None:
        assert range_window_seconds(_T0, _T0 + timedelta(hours=24)) == (
            DEFAULT_CHUNK_SECONDS + RANGE_PADDING_SECONDS
        )


# ---------------------------------------------------------------------------
# 1. True irregular sample timestamps are preserved
# ---------------------------------------------------------------------------


class TestRawTimestampsPreserved:
    def test_non_uniform_offsets_survive_exactly(self, tmp_path: Path, config: Path) -> None:
        spec = ENTITY_SPECS[DEFAULT_ENTITY_IDS[0]]
        offsets = [0.0, 3.7, 41.25, 68.9]
        values = [10.0, 10.5, 10.5, 11.0]
        fake = FakePrometheus(
            series_for(
                spec.entity_id,
                [(_BASE + o, v) for o, v in zip(offsets, values, strict=True)],
                instance="10.42.0.7:8123",
            )
        )
        run_extract(tmp_path, fake.client, config, entities=[spec.entity_id])

        table = read_rows(tmp_path / "data")
        assert timestamps(table) == [epoch_us(o) for o in offsets]
        assert table.num_rows == 4  # no fabricated lookback rows, none dropped
        assert table.column("value").to_pylist() == values
        gaps = {b - a for a, b in zip(timestamps(table), timestamps(table)[1:], strict=False)}
        assert len(gaps) == 3  # genuinely irregular, not a fixed evaluation step

    def test_uses_instant_query_with_range_vector_never_query_range(
        self, tmp_path: Path, config: Path
    ) -> None:
        spec = ENTITY_SPECS["sensor.shellyem_main_channel_1_power"]
        fake = FakePrometheus(series_for(spec.entity_id, [(_BASE + 10, 240.0)]))
        run_extract(tmp_path, fake.client, config, entities=[spec.entity_id])

        assert fake.requests
        for req in fake.requests:
            assert req.url.path == "/api/v1/query"
            assert set(req.url.params.keys()) == {"query", "time"}
            assert req.url.params["time"].endswith(".000000")
            assert re.match(
                r'^hass_sensor_power_w\{entity="[^"]+"\}\[\d+s\]$', req.url.params["query"]
            )

    def test_every_entity_is_queried_once_per_chunk(self, tmp_path: Path, config: Path) -> None:
        fake = FakePrometheus([])
        run_extract(
            tmp_path,
            fake.client,
            config,
            entities=None,
            chunk_seconds=86400,
            start="2024-01-01T00:00:00Z",
            end="2024-01-03T00:00:00Z",
        )
        assert len(fake.requests) == 2 * len(DEFAULT_ENTITY_IDS)
        queries = sorted(r.url.params["query"] for r in fake.requests)
        assert queries == sorted(
            build_range_query(ENTITY_SPECS[e], 86401) for e in DEFAULT_ENTITY_IDS for _ in (0, 1)
        )


# ---------------------------------------------------------------------------
# 2. Half-open ``[start, end)`` edges
# ---------------------------------------------------------------------------


class TestHalfOpenEdges:
    def test_sample_at_start_included_sample_at_end_excluded(
        self, tmp_path: Path, config: Path
    ) -> None:
        spec = ENTITY_SPECS[DEFAULT_ENTITY_IDS[0]]
        fake = FakePrometheus(
            series_for(spec.entity_id, [(_BASE, 1.0), (_BASE + 43200, 2.0), (_BASE + 86400, 3.0)])
        )
        run_extract(tmp_path, fake.client, config, entities=[spec.entity_id], chunk_seconds=86400)

        table = read_rows(tmp_path / "data")
        assert timestamps(table) == [epoch_us(0), epoch_us(43200)]
        assert 3.0 not in table.column("value").to_pylist()

    def test_boundary_samples_are_never_lost_or_duplicated(
        self, tmp_path: Path, config: Path
    ) -> None:
        spec = ENTITY_SPECS[DEFAULT_ENTITY_IDS[0]]
        fake = FakePrometheus(
            series_for(spec.entity_id, [(_BASE, 1.0), (_BASE + 86400, 2.0), (_BASE + 172800, 3.0)])
        )
        path = run_extract(
            tmp_path,
            fake.client,
            config,
            entities=[spec.entity_id],
            chunk_seconds=86400,
            start="2024-01-01T00:00:00Z",
            end="2024-01-03T00:00:00Z",
        )

        # Without the padding second, chunk 1's left-open window would eat the
        # sample sitting exactly on c_start == 2024-01-02T00:00:00Z.
        assert timestamps(read_rows(tmp_path / "data")) == [epoch_us(0), epoch_us(86400)]
        stats = manifest_of(path).params["entity_stats"][0]
        assert stats["samples"] == 2
        assert stats["duplicates_dropped"] == 0
        assert stats["samples_outside_window"] == 2  # padding returned them, filter dropped them

    def test_iter_chunks_partitions_the_interval_exactly(self) -> None:
        start = datetime(2024, 3, 1, tzinfo=UTC)
        end = start + timedelta(days=30, hours=5, milliseconds=500)
        chunks = list(iter_chunks(start, end, 86400))
        assert chunks[0][0] == start and chunks[-1][1] == end
        for (_, a_end), (b_start, _) in zip(chunks, chunks[1:], strict=False):
            assert a_end == b_start
        assert chunks[-1][1] - chunks[-1][0] == timedelta(hours=5, milliseconds=500)

    def test_rejects_zero_width_and_bad_arguments(self, tmp_path: Path, config: Path) -> None:
        fake = FakePrometheus([])
        with pytest.raises(ValueError, match="chunk_seconds"):
            run_extract(tmp_path, fake.client, config, chunk_seconds=0)
        with pytest.raises(ValueError, match="start must be before end"):
            run_extract(
                tmp_path,
                fake.client,
                config,
                start="2024-01-02T00:00:00Z",
                end="2024-01-02T00:00:00Z",
            )
        with pytest.raises(ValueError, match="max_retries"):
            run_extract(tmp_path, fake.client, config, max_retries=-1)


# ---------------------------------------------------------------------------
# 3. Chunking across non-aligned / fractional boundaries
# ---------------------------------------------------------------------------


class TestChunkBoundaries:
    @pytest.mark.parametrize(
        ("chunk_seconds", "offsets"),
        [
            (2, [0.0, 0.5, 1.0, 1.5, 2.0, 2.25, 3.0, 3.5, 4.0, 4.999]),
            (7, [0.0, 6.999, 7.0, 13.5, 13.999, 20.0]),
            (0.5, [0.0, 0.1, 0.499, 0.5, 0.9, 1.25]),
            (3600, [0.0, 3599.999, 3600.0, 5400.5, 7199.0, 7200.0]),
        ],
    )
    def test_nothing_lost_or_duplicated(
        self, tmp_path: Path, config: Path, chunk_seconds, offsets
    ) -> None:
        spec = ENTITY_SPECS["sensor.shellyem_main_channel_1_power"]
        span = max(offsets)
        inside = sorted(o for o in offsets if o < span)
        fake = FakePrometheus(series_for(spec.entity_id, [(_BASE + o, 100.0 + o) for o in offsets]))
        run_extract(
            tmp_path,
            fake.client,
            config,
            entities=[spec.entity_id],
            chunk_seconds=chunk_seconds,
            end=iso_z(_T0 + timedelta(seconds=span)),
        )

        table = read_rows(tmp_path / "data")
        assert timestamps(table) == [epoch_us(o) for o in inside]
        assert table.column("value").to_pylist() == [100.0 + o for o in inside]

    def test_fractional_end_is_respected(self, tmp_path: Path, config: Path) -> None:
        spec = ENTITY_SPECS["sensor.shellyem_main_channel_1_power"]
        fake = FakePrometheus(
            series_for(spec.entity_id, [(_BASE + 3599.25, 1.0), (_BASE + 3599.75, 2.0)])
        )
        run_extract(
            tmp_path,
            fake.client,
            config,
            entities=[spec.entity_id],
            end="2024-01-01T00:59:59.500Z",
            chunk_seconds=86400,
        )
        assert timestamps(read_rows(tmp_path / "data")) == [epoch_us(3599.25)]

    def test_default_chunk_is_24h(self, tmp_path: Path, config: Path) -> None:
        assert DEFAULT_CHUNK_SECONDS == 86400
        spec = ENTITY_SPECS[DEFAULT_ENTITY_IDS[0]]
        samples = [(_BASE + n * 60, float(n)) for n in range(0, 3 * 24 * 60, 37)]
        fake = FakePrometheus(series_for(spec.entity_id, samples))
        path = run_extract(
            tmp_path,
            fake.client,
            config,
            entities=[spec.entity_id],
            end="2024-01-04T00:00:00Z",
            chunk_seconds=86400,
        )
        manifest = manifest_of(path)
        assert manifest.params["chunk_seconds"] == 86400
        assert manifest.params["chunks"] == 3
        assert len(manifest.part_files) == 3
        assert manifest.row_count == len(samples)

    def test_each_part_holds_only_its_own_chunk(self, tmp_path: Path, config: Path) -> None:
        spec = ENTITY_SPECS[DEFAULT_ENTITY_IDS[0]]
        samples = [(_BASE + n * 86400 + 60, float(n)) for n in range(3)]
        fake = FakePrometheus(series_for(spec.entity_id, samples))
        run_extract(
            tmp_path,
            fake.client,
            config,
            entities=[spec.entity_id],
            end="2024-01-04T00:00:00Z",
            chunk_seconds=86400,
        )
        parts = iter_parts(tmp_path / "data", SOURCE)
        assert len(parts) == 3
        for table in (pq.read_table(p) for p in parts):
            assert table.num_rows == 1


# ---------------------------------------------------------------------------
# 4 & 8. Series identity
# ---------------------------------------------------------------------------


class TestSeriesIdentity:
    def test_two_deployment_eras_get_distinct_series_keys(
        self, tmp_path: Path, config: Path
    ) -> None:
        spec = ENTITY_SPECS["sensor.esios_pvpc"]
        era_a = labels_for(
            spec.entity_id, metric=spec.metric, instance="10.42.0.11:8123", job="home-assistant"
        )
        era_b = labels_for(
            spec.entity_id,
            metric=spec.metric,
            instance="10.42.7.3:8123",
            job="home-assistant-hass",
            pod="home-assistant-hass-0",
        )
        fake = FakePrometheus([(era_a, [(_BASE + 1, 0.15)]), (era_b, [(_BASE + 2, 0.11)])])
        run_extract(tmp_path, fake.client, config, entities=[spec.entity_id])

        table = read_rows(tmp_path / "data")
        keys = set(table.column("series_key").to_pylist())
        assert keys == {compute_series_key(era_a), compute_series_key(era_b)}
        assert len(keys) == 2  # collapsing the eras would corrupt counter stitching

        stored = {}
        for row in table.to_pylist():
            stored[row["series_key"]] = json.loads(row["labels_json"])
        assert stored[compute_series_key(era_a)] == era_a  # full label set, instance included
        assert stored[compute_series_key(era_b)] == era_b
        assert stored[compute_series_key(era_a)]["job"] == "home-assistant"
        assert stored[compute_series_key(era_b)]["job"] == "home-assistant-hass"
        assert sorted(table.column("scrape_instance").to_pylist()) == sorted(
            [era_a["instance"], era_b["instance"]]
        )

    def test_series_key_excludes_instance_includes_pod_and_job(self) -> None:
        stable = {
            "__name__": "hass_sensor_unit_u0x20ac_per_kwh",
            "entity": "sensor.esios_pvpc",
            "job": "home-assistant",
            "pod": "home-assistant-0",
            "service": "home-assistant",
        }
        a = compute_series_key({**stable, "instance": "10.42.0.1:8123", "pod_ip": "10.42.0.1"})
        b = compute_series_key({**stable, "instance": "10.42.0.99:8123", "pod_ip": "10.42.0.99"})
        assert a == b  # pod-IP churn alone must not mint a new identity
        assert len(a) == 64
        assert compute_series_key({**stable, "job": "home-assistant-hass"}) != a
        assert compute_series_key({**stable, "pod": "home-assistant-1"}) != a
        assert compute_series_key({**stable, "service": "hass"}) != a

    def test_series_key_is_canonical_json_sha256(self) -> None:
        labels = {"b": "2", "a": "1", "instance": "x:8123"}
        assert compute_series_key(labels) == hashlib.sha256(b'{"a":"1","b":"2"}').hexdigest()
        assert json.loads(canonical_labels_json(labels)) == labels
        assert '"instance":"x:8123"' in canonical_labels_json(labels)

    def test_two_targets_one_logical_series_yield_one_row(
        self, tmp_path: Path, config: Path
    ) -> None:
        """The SD race that exports one sample twice is deduped, not doubled."""
        spec = ENTITY_SPECS[DEFAULT_ENTITY_IDS[0]]
        live = labels_for(
            spec.entity_id, metric=spec.metric, instance="10.42.0.1:8123", job="home-assistant"
        )
        stale = {**live, "instance": "10.42.0.2:8123", "pod_ip": "10.42.0.2"}
        fake = FakePrometheus([(live, [(_BASE + 5, 3.0)]), (stale, [(_BASE + 5, 3.0)])])
        path = run_extract(tmp_path, fake.client, config, entities=[spec.entity_id])

        table = read_rows(tmp_path / "data")
        assert table.num_rows == 1
        assert (
            table.column("series_key")[0].as_py()
            == compute_series_key(live)
            == compute_series_key(stale)
        )
        assert table.column("scrape_instance")[0].as_py() in (live["instance"], stale["instance"])
        assert manifest_of(path).params["entity_stats"][0]["duplicates_dropped"] == 1
        assert any(
            f.startswith("duplicate_samples_dropped:") for f in manifest_of(path).quality_flags
        )

    def test_same_timestamp_different_identity_keeps_both(
        self, tmp_path: Path, config: Path
    ) -> None:
        """Distinct series (different ``pod``/``job``) sharing an instant are not conflated."""
        spec = ENTITY_SPECS[DEFAULT_ENTITY_IDS[0]]
        a = labels_for(
            spec.entity_id, metric=spec.metric, instance="10.42.0.1:8123", job="home-assistant"
        )
        b = labels_for(
            spec.entity_id,
            metric=spec.metric,
            instance="10.42.0.2:8123",
            job="home-assistant-hass",
            pod="home-assistant-hass-0",
        )
        fake = FakePrometheus([(a, [(_BASE + 5, 3.0)]), (b, [(_BASE + 5, 4.0)])])
        run_extract(tmp_path, fake.client, config, entities=[spec.entity_id])
        table = read_rows(tmp_path / "data")
        assert table.num_rows == 2
        assert sorted(table.column("value").to_pylist()) == [3.0, 4.0]


# ---------------------------------------------------------------------------
# 5. Negative prices and counter deltas
# ---------------------------------------------------------------------------


class TestNegativeValuesRetained:
    def test_negative_prices_are_not_filtered_or_clamped(
        self, tmp_path: Path, config: Path
    ) -> None:
        spec = ENTITY_SPECS["sensor.esios_pvpc"]
        samples = [(_BASE, 0.305), (_BASE + 1, -0.032), (_BASE + 2, 0.0), (_BASE + 3, -0.0)]
        fake = FakePrometheus(series_for(spec.entity_id, samples))
        run_extract(tmp_path, fake.client, config, entities=[spec.entity_id])

        values = read_rows(tmp_path / "data").column("value").to_pylist()
        assert values == [0.305, -0.032, 0.0, -0.0]
        assert min(values) == -0.032  # the dashboard's ``>= 0`` guard is not repeated here

    def test_negative_counter_delta_retained(self, tmp_path: Path, config: Path) -> None:
        spec = ENTITY_SPECS["sensor.shellyem_main_channel_1_energy_returned"]
        fake = FakePrometheus(series_for(spec.entity_id, [(_BASE + 1, -12.5)]))
        run_extract(tmp_path, fake.client, config, entities=[spec.entity_id])
        assert read_rows(tmp_path / "data").column("value").to_pylist() == [-12.5]


# ---------------------------------------------------------------------------
# 6. Failure modes
# ---------------------------------------------------------------------------


def manifests_of(data_dir: Path) -> list[Path]:
    return sorted((data_dir / "manifests").glob("*.json"))


class TestFailures:
    def test_status_error_raises_and_writes_no_manifest(self, tmp_path: Path, config: Path) -> None:
        fake = FakePrometheus(
            [],
            responder=lambda request, index: httpx.Response(
                400, json={"status": "error", "errorType": "bad_data", "error": "parse error"}
            ),
        )
        with pytest.raises(PrometheusHTTPError, match="HTTP 400"):
            run_extract(tmp_path, fake.client, config)
        assert manifests_of(tmp_path / "data") == []

    def test_http_500_exhausts_retries_then_raises(
        self, tmp_path: Path, config: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        slept: list[float] = []
        monkeypatch.setattr(extract_prom.time, "sleep", slept.append)
        fake = FakePrometheus([], responder=lambda request, index: httpx.Response(500, text="boom"))
        with pytest.raises(PrometheusHTTPError, match="gave up after 2 attempt"):
            run_extract(
                tmp_path,
                fake.client,
                config,
                max_retries=1,
                start="2024-01-01T00:00:00Z",
                end="2024-01-01T01:00:00Z",
            )
        assert len(fake.requests) == 2  # initial attempt + exactly one retry
        assert slept == [0.5]
        assert manifests_of(tmp_path / "data") == []
        assert iter_parts(tmp_path / "data", SOURCE) == []

    def test_backoff_is_exponential_and_capped(
        self, tmp_path: Path, config: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        slept: list[float] = []
        monkeypatch.setattr(extract_prom.time, "sleep", slept.append)
        monkeypatch.setattr(extract_prom, "_BACKOFF_MAX_SECONDS", 1.0)
        fake = FakePrometheus(
            [], responder=lambda request, index: httpx.Response(503, text="unavailable")
        )
        with pytest.raises(PrometheusHTTPError):
            run_extract(
                tmp_path,
                fake.client,
                config,
                max_retries=3,
                start="2024-01-01T00:00:00Z",
                end="2024-01-01T01:00:00Z",
            )
        assert slept == [0.5, 1.0, 1.0]  # 0.5 * 2**n, clamped
        assert len(fake.requests) == 4

    def test_auth_error_is_not_retried(self, tmp_path: Path, config: Path) -> None:
        fake = FakePrometheus(
            [], responder=lambda request, index: httpx.Response(401, text="Unauthorized")
        )
        with pytest.raises(PrometheusHTTPError, match="not retried"):
            run_extract(tmp_path, fake.client, config, max_retries=5)
        assert len(fake.requests) == 1

    def test_4xx_without_error_status_is_not_retried(self, tmp_path: Path, config: Path) -> None:
        fake = FakePrometheus([], responder=lambda request, index: httpx.Response(422, text="nope"))
        with pytest.raises(PrometheusHTTPError, match="HTTP 422"):
            run_extract(tmp_path, fake.client, config, max_retries=4)
        assert len(fake.requests) == 1

    def test_transport_timeout_is_retried_then_succeeds(
        self, tmp_path: Path, config: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.setattr(extract_prom.time, "sleep", lambda s: None)
        spec = ENTITY_SPECS[DEFAULT_ENTITY_IDS[0]]
        good = matrix_for(spec.entity_id, [(_BASE + 7, 1.0)])
        seen: list[int] = []

        def responder(request: httpx.Request, index: int) -> httpx.Response:
            seen.append(index)
            if len(seen) == 1:
                raise httpx.ConnectTimeout("timed out")
            return httpx.Response(
                200, json={"status": "success", "data": {"resultType": "matrix", "result": good}}
            )

        fake = FakePrometheus([], responder=responder)
        path = run_extract(tmp_path, fake.client, config, entities=[spec.entity_id])
        assert path.exists()
        assert read_rows(tmp_path / "data").num_rows == 1

    @pytest.mark.parametrize("bad", ["NaN", "nan", "Inf", "-Inf", "+Inf"])
    def test_non_finite_value_raises_and_writes_nothing(
        self, tmp_path: Path, config: Path, bad: str
    ) -> None:
        spec = ENTITY_SPECS["sensor.shellyem_main_channel_1_power"]
        fake = FakePrometheus(series_for(spec.entity_id, [(_BASE + 1, bad), (_BASE + 61, 240.0)]))
        data_dir = tmp_path / "data"
        with pytest.raises(NonFiniteSampleError, match="non-finite"):
            run_extract(
                tmp_path, fake.client, config, entities=[spec.entity_id], data_dir=str(data_dir)
            )
        assert manifests_of(data_dir) == []
        assert not list(data_dir.rglob("*.parquet"))

    def test_non_finite_can_be_downgraded_to_a_quality_flag(
        self, tmp_path: Path, config: Path
    ) -> None:
        spec = ENTITY_SPECS["sensor.shellyem_main_channel_1_power"]
        fake = FakePrometheus(series_for(spec.entity_id, [(_BASE + 1, "NaN"), (_BASE + 61, 240.0)]))
        path = run_extract(
            tmp_path,
            fake.client,
            config,
            entities=[spec.entity_id],
            allow_non_finite=True,
            start="2024-01-01T00:00:00Z",
            end="2024-01-01T01:00:00Z",
        )
        manifest = manifest_of(path)
        assert f"non_finite_samples_dropped:{spec.entity_id}:1" in manifest.quality_flags
        table = read_rows(tmp_path / "data")
        assert table.column("value").to_pylist() == [240.0]  # never written to the store

    def test_non_numeric_value_is_a_query_error(self, tmp_path: Path, config: Path) -> None:
        spec = ENTITY_SPECS["sensor.shellyem_main_channel_1_power"]
        fake = FakePrometheus(series_for(spec.entity_id, [(_BASE + 1, "not-a-number")]))
        with pytest.raises(PrometheusQueryError, match="not numeric"):
            run_extract(tmp_path, fake.client, config, entities=[spec.entity_id])

    def test_wrong_result_type_is_an_error(self, tmp_path: Path, config: Path) -> None:
        fake = FakePrometheus(
            [],
            responder=lambda request, index: httpx.Response(
                200, json={"status": "success", "data": {"resultType": "vector", "result": []}}
            ),
        )
        with pytest.raises(PrometheusQueryError, match="resultType"):
            run_extract(tmp_path, fake.client, config)

    def test_malformed_sample_pair_is_an_error(self, tmp_path: Path, config: Path) -> None:
        fake = FakePrometheus(
            [],
            responder=lambda request, index: httpx.Response(
                200,
                json={
                    "status": "success",
                    "data": {
                        "resultType": "matrix",
                        "result": [
                            {
                                "metric": {
                                    "entity": "sensor.x",
                                    "__name__": "hass_sensor_energy_kwh",
                                },
                                "values": [["only-one-element"]],
                            }
                        ],
                    },
                },
            ),
        )
        with pytest.raises(PrometheusQueryError, match=r"not a \[timestamp, value\] pair"):
            run_extract(
                tmp_path, fake.client, config, entities=["sensor.x:hass_sensor_energy_kwh:kWh"]
            )

    def test_non_json_body_is_an_error(self, tmp_path: Path, config: Path) -> None:
        fake = FakePrometheus(
            [],
            responder=lambda request, index: httpx.Response(200, text="<html>proxy error</html>"),
        )
        with pytest.raises(PrometheusQueryError, match="not JSON"):
            run_extract(tmp_path, fake.client, config)

    def test_labels_of_another_entity_are_rejected(self, tmp_path: Path, config: Path) -> None:
        spec = ENTITY_SPECS[DEFAULT_ENTITY_IDS[0]]
        wrong = labels_for(
            "sensor.shellyem_main_channel_2_energy", metric=spec.metric, instance="i:1", job="j"
        )

        def responder(request: httpx.Request, index: int) -> httpx.Response:
            return httpx.Response(
                200,
                json={
                    "status": "success",
                    "data": {
                        "resultType": "matrix",
                        "result": [{"metric": wrong, "values": [[_BASE + 1, "1.0"]]}],
                    },
                },
            )

        fake = FakePrometheus([], responder=responder)
        with pytest.raises(PrometheusQueryError, match="contradict the requested entity"):
            run_extract(tmp_path, fake.client, config, entities=[spec.entity_id])

    def test_orphaned_parts_from_a_failed_run_have_no_manifest(
        self, tmp_path: Path, config: Path
    ) -> None:
        """Chunk 1 succeeds, chunk 2 fails: the part survives but is unreferenced."""
        spec = ENTITY_SPECS[DEFAULT_ENTITY_IDS[0]]
        labels = labels_for(spec.entity_id, metric=spec.metric, instance="i:1", job="j")

        def responder(request: httpx.Request, index: int) -> httpx.Response:
            if index >= 1:
                return httpx.Response(500, text="kaboom")
            t = float(request.url.params["time"])
            return httpx.Response(
                200,
                json={
                    "status": "success",
                    "data": {
                        "resultType": "matrix",
                        "result": [{"metric": labels, "values": [[t - 1, "5.0"]]}],
                    },
                },
            )

        fake = FakePrometheus([], responder=responder)
        data_dir = tmp_path / "data"
        with pytest.raises(PrometheusHTTPError):
            run_extract(
                tmp_path,
                fake.client,
                config,
                entities=[spec.entity_id],
                start="2024-01-01T00:00:00Z",
                end="2024-01-01T02:00:00Z",
                max_retries=0,
            )
        assert len(iter_parts(data_dir, SOURCE)) == 1
        assert manifests_of(data_dir) == []


# ---------------------------------------------------------------------------
# 7. The three empty cases
# ---------------------------------------------------------------------------


class TestEmptyCases:
    def test_case_a_no_series_is_flagged_not_silent(self, tmp_path: Path, config: Path) -> None:
        fake = FakePrometheus([])
        path = run_extract(
            tmp_path,
            fake.client,
            config,
            entities=[DEFAULT_ENTITY_IDS[0]],
            start="2024-01-01T00:00:00Z",
            end="2024-01-01T02:00:00Z",
        )
        manifest = manifest_of(path)
        assert manifest.row_count == 0
        assert manifest.part_files == []
        assert f"no_samples_extracted:{DEFAULT_ENTITY_IDS[0]}" in manifest.quality_flags
        assert "no_samples_extracted:all" in manifest.quality_flags
        assert f"no_series_chunks:{DEFAULT_ENTITY_IDS[0]}:2/2" in manifest.quality_flags
        assert "never treated as continuity" in manifest.coverage_notes
        assert iter_parts(tmp_path / "data", SOURCE) == []

    def test_case_b_series_present_with_no_values(self) -> None:
        spec = ENTITY_SPECS["sensor.esios_pvpc"]
        stats = extract_prom._EntityStats(spec)
        matrix = [
            {
                "metric": labels_for(spec.entity_id, metric=spec.metric, instance="i:1", job="j"),
                "values": [],
            }
        ]
        rows = matrix_to_rows(
            matrix,
            spec=spec,
            stats=stats,
            query=build_range_query(spec, 2),
            lo_us=epoch_us(0),
            hi_us=epoch_us(1),
        )
        assert rows == []
        assert stats.chunks_no_samples == 1 and stats.chunks_no_series == 0
        assert stats.flags(1) == [
            f"no_samples_extracted:{spec.entity_id}",
            f"series_without_samples_chunks:{spec.entity_id}:1/1",
        ]

    def test_case_b_when_only_out_of_window_samples_come_back(self) -> None:
        """Padding returned a sample belonging to the previous chunk — case (b)."""
        spec = ENTITY_SPECS["sensor.esios_pvpc"]
        stats = extract_prom._EntityStats(spec)
        matrix = matrix_for(spec.entity_id, [(_BASE - 2, 0.1)])
        rows = matrix_to_rows(
            matrix,
            spec=spec,
            stats=stats,
            query=build_range_query(spec, 3),
            lo_us=epoch_us(0),
            hi_us=epoch_us(1),
        )
        assert rows == []
        assert stats.samples_outside_window == 1
        assert stats.chunks_no_samples == 1

    def test_partial_coverage_flags_only_the_empty_chunks(
        self, tmp_path: Path, config: Path
    ) -> None:
        spec = ENTITY_SPECS[DEFAULT_ENTITY_IDS[0]]

        def responder(request: httpx.Request, index: int) -> httpx.Response:
            t = float(request.url.params["time"])
            populated = t - 3600 <= _BASE + 60 < t
            result = matrix_for(spec.entity_id, [(_BASE + 60, 1.0)]) if populated else []
            return httpx.Response(
                200, json={"status": "success", "data": {"resultType": "matrix", "result": result}}
            )

        fake = FakePrometheus([], responder=responder)
        path = run_extract(
            tmp_path,
            fake.client,
            config,
            entities=[spec.entity_id],
            start="2024-01-01T00:00:00Z",
            end="2024-01-01T02:00:00Z",
        )
        manifest = manifest_of(path)
        assert manifest.row_count == 1
        assert len(manifest.part_files) == 1
        assert f"no_series_chunks:{spec.entity_id}:1/2" in manifest.quality_flags
        assert f"no_samples_extracted:{spec.entity_id}" not in manifest.quality_flags
        stats = manifest.params["entity_stats"][0]
        assert stats["samples"] == 1 and stats["chunks_no_series"] == 1
        assert stats["first_ts"] == stats["last_ts"] == "2024-01-01T00:01:00+00:00"

    def test_case_c_query_error_raises(self, tmp_path: Path, config: Path) -> None:
        fake = FakePrometheus([], responder=lambda request, index: httpx.Response(404, text="nope"))
        with pytest.raises(PrometheusHTTPError, match="HTTP 404"):
            run_extract(tmp_path, fake.client, config)


# ---------------------------------------------------------------------------
# 9. Schema contract and validate_raw
# ---------------------------------------------------------------------------


class TestSchemaAndValidation:
    def test_written_parts_match_raw_sample_schema(self, tmp_path: Path, config: Path) -> None:
        spec = ENTITY_SPECS[DEFAULT_ENTITY_IDS[0]]
        samples = [(_BASE + n * 60, float(n)) for n in range(5)]
        fake = FakePrometheus(series_for(spec.entity_id, samples))
        run_extract(
            tmp_path,
            fake.client,
            config,
            entities=[spec.entity_id],
            start="2024-01-01T00:00:00Z",
            end="2024-01-01T01:00:00Z",
        )

        parts = iter_parts(tmp_path / "data", SOURCE)
        assert len(parts) == 1
        table = pq.read_table(parts[0])
        assert table.schema == RAW_SAMPLE
        for field_def in RAW_SAMPLE:
            if not field_def.nullable:
                assert table.column(field_def.name).null_count == 0

        summary = validate_raw(data_dir=str(tmp_path / "data"), source=SOURCE)
        assert summary["ok"] is True
        assert summary["violations"] == []
        assert summary["row_count"] == 5
        assert summary["file_count"] == 1
        assert summary["series_count"] == 1
        assert summary["entities"] == [spec.entity_id]
        assert summary["min_ts"] == "2024-01-01T00:00:00+00:00"
        assert summary["max_ts"] == "2024-01-01T00:04:00+00:00"
        assert summary["parts_dir"] == str(parts_dir(tmp_path / "data", SOURCE))
        assert summary["files"][0]["schema_ok"] is True
        assert summary["files"][0]["rows"] == 5

    def test_validate_raw_accepts_multi_entity_parts(self, tmp_path: Path, config: Path) -> None:
        series = [
            (
                labels_for(
                    eid,
                    metric=ENTITY_SPECS[eid].metric,
                    instance="10.42.0.1:8123",
                    job="home-assistant",
                ),
                [(_BASE + 10, 1.0), (_BASE + 70, 2.0)],
            )
            for eid in DEFAULT_ENTITY_IDS
        ]
        fake = FakePrometheus(series)
        path = run_extract(
            tmp_path,
            fake.client,
            config,
            entities=None,
            chunk_seconds=86400,
            start="2024-01-01T00:00:00Z",
            end="2024-01-02T00:00:00Z",
        )
        manifest = manifest_of(path)
        assert manifest.row_count == 8
        assert manifest.quality_flags == []
        assert len(manifest.part_files) == 1

        summary = validate_raw(data_dir=str(tmp_path / "data"), source=SOURCE)
        assert summary["ok"] is True
        assert summary["violations"] == []
        assert summary["row_count"] == 8
        assert summary["series_count"] == 4
        assert len(summary["entities"]) == 4

    def test_validate_raw_reports_null_violations(self, tmp_path: Path) -> None:
        """PyArrow refuses to write nulls in non-nullable columns; test schema mismatch instead."""
        wrong = pa.table(
            {
                "entity": pa.array(["e"]),
                "metric": pa.array(["m"]),
                "unit": pa.array(["kWh"]),
                "series_key": pa.array(["k"]),
                "ts": pa.array([1704067200000000], pa.timestamp("us", tz="UTC")),
                "value": pa.array([1.0], pa.float64()),
                "scrape_instance": pa.array(["i:1"], pa.string()),
                "extraction_id": pa.array(["e1"], pa.string()),
                "labels_json": pa.array(['{"a":"b"}'], pa.string()),
                "extra_column": pa.array([1], pa.int64()),
            }
        )
        pdir = parts_dir(tmp_path, SOURCE)
        pdir.mkdir(parents=True)
        pq.write_table(wrong, pdir / "prom-samples-deadbeef.parquet")
        summary = validate_raw(data_dir=str(tmp_path), source=SOURCE)
        assert summary["ok"] is False
        assert any("schema mismatch" in v for v in summary["violations"])
        assert summary["files"][0]["schema_ok"] is False

    def test_validate_raw_reports_filename_hash_mismatch(
        self, tmp_path: Path, config: Path
    ) -> None:
        spec = ENTITY_SPECS[DEFAULT_ENTITY_IDS[0]]
        fake = FakePrometheus(series_for(spec.entity_id, [(_BASE + 5, 1.0)]))
        run_extract(
            tmp_path,
            fake.client,
            config,
            entities=[spec.entity_id],
            start="2024-01-01T00:00:00Z",
            end="2024-01-01T01:00:00Z",
        )
        real = iter_parts(tmp_path / "data", SOURCE)[0]
        good = real.read_bytes()
        real.unlink()
        (
            real.parent / "prom-samples-20240101T000000Z-20240101T010000Z-00000000.parquet"
        ).write_bytes(good)

        summary = validate_raw(data_dir=str(tmp_path / "data"), source=SOURCE)
        assert summary["ok"] is False
        assert any("does not match" in v for v in summary["violations"])
        assert summary["row_count"] == 1  # content itself still parsed

    def test_validate_raw_reports_schema_mismatch(self, tmp_path: Path) -> None:
        wrong = pa.table({"entity": pa.array(["e"])})
        pdir = parts_dir(tmp_path, SOURCE)
        pdir.mkdir(parents=True)
        pq.write_table(wrong, pdir / "prom-samples-wrong.parquet")
        summary = validate_raw(data_dir=str(tmp_path), source=SOURCE)
        assert summary["ok"] is False
        assert any("schema mismatch" in v for v in summary["violations"])

    def test_validate_raw_reports_unreadable_part(self, tmp_path: Path) -> None:
        pdir = parts_dir(tmp_path, SOURCE)
        pdir.mkdir(parents=True)
        (pdir / "prom-samples-junk.parquet").write_bytes(b"not parquet at all")
        summary = validate_raw(data_dir=str(tmp_path), source=SOURCE)
        assert summary["ok"] is False
        assert any("unreadable part" in v for v in summary["violations"])

    def test_validate_raw_without_parts_is_not_ok(self, tmp_path: Path) -> None:
        summary = validate_raw(data_dir=str(tmp_path), source=SOURCE)
        assert summary["ok"] is False
        assert summary["file_count"] == 0
        assert any("no part files" in v for v in summary["violations"])


# ---------------------------------------------------------------------------
# 10. Determinism
# ---------------------------------------------------------------------------


def build_series(entity_index: int) -> list[tuple[dict[str, str], list[tuple[float, Any]]]]:
    eid = DEFAULT_ENTITY_IDS[entity_index]
    samples = [(_BASE + n * 61 + entity_index, float(n) / 7) for n in range(40)]
    if eid == "sensor.esios_pvpc":
        samples.append((_BASE + 900, -0.5))
    return series_for(
        eid, samples, instance=f"10.42.0.{3 + entity_index}:8123", pod=f"ha-{entity_index}"
    )


def all_series() -> list[tuple[dict[str, str], list[tuple[float, Any]]]]:
    return [s for i in range(len(DEFAULT_ENTITY_IDS)) for s in build_series(i)]


def dump_parts(data_dir: Path) -> tuple[list[str], list[tuple]]:
    parts = iter_parts(data_dir, SOURCE)
    order = [
        (r["series_key"], r["ts"], r["value"])
        for p in parts
        for r in pq.read_table(p).select(["series_key", "ts", "value"]).to_pylist()
    ]
    return [p.name for p in parts], order


class TestDeterminism:
    def test_row_bytes_are_reproducible_for_a_fixed_extraction_id(self) -> None:
        """With a constant extraction id, identical rows produce identical bytes."""
        spec = ENTITY_SPECS[DEFAULT_ENTITY_IDS[0]]
        stats = extract_prom._EntityStats(spec)
        matrix = matrix_for(spec.entity_id, [(_BASE + n, float(n)) for n in range(10)])
        rows = extract_prom.dedupe_rows(
            extract_prom.matrix_to_rows(
                matrix, spec=spec, stats=stats, query="q", lo_us=epoch_us(0), hi_us=epoch_us(20)
            ),
            {spec.entity_id: stats},
            {},
        )
        a = extract_prom.table_to_bytes(extract_prom.rows_to_table(rows, "fixed-id"))
        b = extract_prom.table_to_bytes(extract_prom.rows_to_table(rows, "fixed-id"))
        assert a == b
        assert hashlib.sha256(a).hexdigest() == hashlib.sha256(b).hexdigest()

    def test_response_order_does_not_affect_rows(self, tmp_path: Path, config: Path) -> None:
        """Shuffled series and shuffled sample lists within each series → same rows."""
        forward = all_series()
        shuffled = [(labels, list(reversed(samples))) for labels, samples in reversed(forward)]
        run_extract(
            tmp_path,
            FakePrometheus(forward).client,
            config,
            entities=None,
            chunk_seconds=86400,
            data_dir=str(tmp_path / "a"),
        )
        run_extract(
            tmp_path,
            FakePrometheus(shuffled).client,
            config,
            entities=None,
            chunk_seconds=86400,
            data_dir=str(tmp_path / "b"),
        )
        _, order_a = dump_parts(tmp_path / "a")
        _, order_b = dump_parts(tmp_path / "b")
        assert order_a == order_b
        assert order_a == sorted(order_a, key=lambda r: (r[0], r[1]))

    def test_two_runs_agree_on_everything_except_the_run_id(
        self, tmp_path: Path, config: Path
    ) -> None:
        """Each run mints a fresh extraction id, so part hashes differ — but the content agrees."""
        run_extract(
            tmp_path,
            FakePrometheus(all_series()).client,
            config,
            entities=None,
            chunk_seconds=600,
            data_dir=str(tmp_path / "a"),
        )
        run_extract(
            tmp_path,
            FakePrometheus(all_series()).client,
            config,
            entities=None,
            chunk_seconds=600,
            data_dir=str(tmp_path / "b"),
        )
        _, order_a = dump_parts(tmp_path / "a")
        _, order_b = dump_parts(tmp_path / "b")
        assert order_a == order_b
        assert len(order_a) > 1
        # Chunk boundaries match across runs
        ma = manifest_of(sorted((tmp_path / "a" / "manifests").glob("*.json"))[0])
        mb = manifest_of(sorted((tmp_path / "b" / "manifests").glob("*.json"))[0])
        assert ma.params["chunks"] == mb.params["chunks"]
        assert [e["chunk_start"] for e in ma.part_files] == [
            e["chunk_start"] for e in mb.part_files
        ]
        assert [e["rows"] for e in ma.part_files] == [e["rows"] for e in mb.part_files]
        # But the extraction ids and part hashes differ (each run is a new lineage)
        assert ma.extraction_id != mb.extraction_id
        assert [e["sha256"] for e in ma.part_files] != [e["sha256"] for e in mb.part_files]


# ---------------------------------------------------------------------------
# Public surface: entities, base URL, escaping, arguments, logging
# ---------------------------------------------------------------------------


class TestEntityRegistry:
    def test_defaults_are_the_four_live_entities(self) -> None:
        assert DEFAULT_ENTITY_IDS == (
            "sensor.shellyem_main_channel_1_energy",
            "sensor.shellyem_main_channel_1_energy_returned",
            "sensor.shellyem_main_channel_1_power",
            "sensor.esios_pvpc",
        )
        assert "sensor.esios_pvpc_2" not in DEFAULT_ENTITY_IDS
        assert DEAD_DUPLICATE_ENTITY_IDS == frozenset({"sensor.esios_pvpc_2"})

    def test_verified_metric_and_unit_mapping(self) -> None:
        assert (
            ENTITY_SPECS["sensor.shellyem_main_channel_1_energy"].metric == "hass_sensor_energy_kwh"
        )
        assert ENTITY_SPECS["sensor.shellyem_main_channel_1_energy"].unit == "kWh"
        assert (
            ENTITY_SPECS["sensor.shellyem_main_channel_1_energy_returned"].metric
            == "hass_sensor_energy_kwh"
        )
        assert ENTITY_SPECS["sensor.shellyem_main_channel_1_energy_returned"].unit == "kWh"
        assert ENTITY_SPECS["sensor.shellyem_main_channel_1_power"].metric == "hass_sensor_power_w"
        assert ENTITY_SPECS["sensor.shellyem_main_channel_1_power"].unit == "W"
        assert ENTITY_SPECS["sensor.esios_pvpc"].metric == "hass_sensor_unit_u0x20ac_per_kwh"
        assert ENTITY_SPECS["sensor.esios_pvpc"].unit == "EUR/kWh"

    def test_none_and_empty_resolve_to_the_defaults(self) -> None:
        expected = tuple(ENTITY_SPECS[e] for e in DEFAULT_ENTITY_IDS)
        assert resolve_entity_specs(None) == expected
        assert resolve_entity_specs([]) == expected
        with pytest.raises(ValueError, match="resolved to empty"):
            resolve_entity_specs(["", "  "])

    def test_unknown_entity_fails_loudly(self) -> None:
        with pytest.raises(ValueError, match="no metric/unit mapping"):
            resolve_entity_specs(["sensor.made_up"])

    def test_explicit_triple_is_accepted(self) -> None:
        assert resolve_entity_specs(["sensor.new:hass_sensor_energy_kwh:kWh"]) == (
            EntitySpec("sensor.new", "hass_sensor_energy_kwh", "kWh"),
        )

    @pytest.mark.parametrize("bad", ["sensor.new:kWh", "sensor.new::kWh", ":m:u", "a:b:c:d"])
    def test_malformed_triple_rejected(self, bad: str) -> None:
        with pytest.raises(ValueError, match="entity:metric:unit"):
            resolve_entity_specs([bad])

    @pytest.mark.parametrize("bad", ['sensor.bad"x', "sensor.bad\\x", "sensor bad", "sensor;drop"])
    def test_unsafe_entity_id_rejected(self, bad: str) -> None:
        with pytest.raises(ValueError, match="(unsupported characters|no metric/unit mapping)"):
            resolve_entity_specs([bad])

    def test_dead_duplicate_must_be_requested_and_is_flagged(
        self, tmp_path: Path, config: Path
    ) -> None:
        fake = FakePrometheus(series_for("sensor.esios_pvpc_2", [(_BASE + 1, 0.2)]))
        path = run_extract(tmp_path, fake.client, config, entities=["sensor.esios_pvpc_2"])
        manifest = manifest_of(path)
        assert "dead_duplicate_entity:sensor.esios_pvpc_2" in manifest.quality_flags
        assert manifest.entities == ["sensor.esios_pvpc_2"]

    def test_promql_escaping(self) -> None:
        assert escape_promql_string('a"b') == 'a\\"b'
        assert escape_promql_string("a\\b") == "a\\\\b"
        with pytest.raises(ValueError, match="control characters"):
            escape_promql_string("a\nb")

    def test_injection_attempt_stays_inside_the_quoted_matcher(self) -> None:
        hostile = EntitySpec('x"} or 1', "hass_sensor_energy_kwh", "kWh")
        query = build_range_query(hostile, 60)
        assert query == 'hass_sensor_energy_kwh{entity="x\\"} or 1"}[60s]'
        # The escaped quote prevents the injection from closing the matcher early.
        assert '\\"' in query
        # The query is syntactically valid PromQL (the regex doesn't match escaped quotes,
        # but that's a limitation of the test regex, not the query itself).
        assert "entity=" in query

    def test_dots_in_entity_ids_need_no_escaping(self) -> None:
        query = build_range_query(ENTITY_SPECS["sensor.esios_pvpc"], 86401)
        assert query == 'hass_sensor_unit_u0x20ac_per_kwh{entity="sensor.esios_pvpc"}[86401s]'


class TestBaseUrl:
    def test_default_is_the_live_archive(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.delenv(BASE_URL_ENV_VAR, raising=False)
        assert DEFAULT_BASE_URL == "https://prometheus-long-term.internal.grigri.cloud"
        assert resolve_base_url(None) == DEFAULT_BASE_URL

    def test_env_then_argument_precedence(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setenv(BASE_URL_ENV_VAR, "http://prom.internal:9090/")
        assert resolve_base_url(None) == "http://prom.internal:9090"
        assert resolve_base_url("http://other:1") == "http://other:1"

    def test_credentials_and_bad_scheme_rejected(self) -> None:
        with pytest.raises(ValueError, match="credentials"):
            resolve_base_url("https://user:pass@prom.internal")
        with pytest.raises(ValueError, match="http"):
            resolve_base_url("ftp://prom.internal")

    def test_requests_go_to_the_resolved_host(
        self, tmp_path: Path, config: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.delenv(BASE_URL_ENV_VAR, raising=False)
        fake = FakePrometheus([])
        path = run_extract(tmp_path, fake.client, config, chunk_seconds=86400)
        assert fake.requests
        assert all(
            r.url.host == "prometheus-long-term.internal.grigri.cloud" for r in fake.requests
        )
        assert all(r.url.scheme == "https" for r in fake.requests)
        assert DEFAULT_BASE_URL in path.read_text()

    def test_no_authorization_header_is_sent(self, tmp_path: Path, config: Path) -> None:
        fake = FakePrometheus([])
        run_extract(tmp_path, fake.client, config, chunk_seconds=86400)
        assert all("authorization" not in r.headers for r in fake.requests)

    def test_injected_client_is_not_closed(self, tmp_path: Path, config: Path) -> None:
        client = FakePrometheus([]).client
        run_extract(tmp_path, client, config, chunk_seconds=86400)
        run_extract(tmp_path, client, config, chunk_seconds=86400)  # still usable
        assert not client.is_closed
        client.close()


class TestTimestampParsing:
    def test_naive_rejected(self) -> None:
        with pytest.raises(ValueError, match="naive timestamp"):
            parse_utc("2024-01-01T00:00:00", name="start")

    def test_non_iso_rejected(self) -> None:
        with pytest.raises(ValueError, match="not valid ISO-8601"):
            parse_utc("01/01/2024", name="start")
        with pytest.raises(ValueError, match="non-empty"):
            parse_utc("", name="end")

    def test_offsets_normalised_to_utc(self) -> None:
        assert parse_utc("2024-01-01T02:00:00+02:00", name="start") == _T0
        assert parse_utc("2024-01-01T00:00:00z", name="start") == _T0
        assert parse_utc("2024-01-01T00:00:00Z", name="start").tzinfo is UTC
        assert (
            iso_z(parse_utc("2024-01-01T00:00:00.500Z", name="start")) == "2024-01-01T00:00:00.5Z"
        )

    def test_epoch_time_formatting_is_exact(self) -> None:
        assert format_epoch_time(to_micros(_T0)) == "1704067200.000000"
        assert format_epoch_time(to_micros(_T0) + 123456) == "1704067200.123456"
        assert parse_epoch_time("1704067200.123456") == to_micros(_T0) + 123456
        assert parse_epoch_time(1704067200.123456) == to_micros(_T0) + 123456
        with pytest.raises(PrometheusQueryError, match="not finite"):
            parse_epoch_time(float("nan"))
        with pytest.raises(PrometheusQueryError, match="not numeric"):
            parse_epoch_time("soon")

    def test_naive_datetimes_rejected_by_part_path(self) -> None:
        with pytest.raises(ValueError, match="timezone-aware"):
            part_path(Path("/d"), SOURCE, PART_KIND, datetime(2024, 1, 1), _T0, "00000000")

    def test_missing_config_raises(self, tmp_path: Path) -> None:
        with pytest.raises(FileNotFoundError, match="tariff config"):
            extract_prometheus(
                data_dir=str(tmp_path / "d"),
                config_path=str(tmp_path / "nope.yaml"),
                start="2024-01-01T00:00:00Z",
                end="2024-01-02T00:00:00Z",
                entities=[DEFAULT_ENTITY_IDS[0]],
                client=FakePrometheus([]).client,
            )


class TestManifestContent:
    def test_params_carry_no_credentials_or_query_strings(
        self, tmp_path: Path, config: Path
    ) -> None:
        path = run_extract(tmp_path, FakePrometheus([]).client, config, chunk_seconds=86400)
        manifest = manifest_of(path)
        blob = json.dumps(manifest.params)
        for forbidden in ("token", "password", "credential", "Bearer", "?"):
            assert forbidden not in blob
        assert manifest.params["interval_semantics"] == "half-open [start, end)"
        assert manifest.params["series_identity"]["excluded_labels"] == ["instance", "pod_ip"]
        assert manifest.params["api"].startswith("/api/v1/query")
        assert manifest.params["range_padding_seconds"] == 1
        assert manifest.source == SOURCE
        assert manifest.schema_version == 1
        assert manifest.source_window_start == "2024-01-01T00:00:00Z"
        assert manifest.source_window_end == "2024-01-02T00:00:00Z"
        assert manifest.finished_at is not None and manifest.started_at <= manifest.finished_at
        assert manifest.config_hash and len(manifest.config_hash) == 64

    def test_part_entries_are_self_describing(self, tmp_path: Path, config: Path) -> None:
        spec = ENTITY_SPECS[DEFAULT_ENTITY_IDS[0]]
        fake = FakePrometheus(series_for(spec.entity_id, [(_BASE + 5, 1.0)]))
        path = run_extract(
            tmp_path, fake.client, config, entities=[spec.entity_id], chunk_seconds=86400
        )
        entry = manifest_of(path).part_files[0]
        assert entry["kind"] == PART_KIND
        assert entry["chunk_start"] == "2024-01-01T00:00:00Z"
        assert entry["chunk_end"] == "2024-01-02T00:00:00Z"
        assert entry["rows"] == 1
        assert len(entry["sha256"]) == 64
        assert entry["path"].startswith(f"raw/{SOURCE}/v1/parts/")
        on_disk = tmp_path / "data" / entry["path"]
        assert on_disk.exists()
        assert entry["sha256"] == hashlib.sha256(on_disk.read_bytes()).hexdigest()

    def test_extraction_id_links_rows_and_manifest(self, tmp_path: Path, config: Path) -> None:
        spec = ENTITY_SPECS[DEFAULT_ENTITY_IDS[0]]
        fake = FakePrometheus(series_for(spec.entity_id, [(_BASE + 5, 1.0), (_BASE + 65, 2.0)]))
        path = run_extract(
            tmp_path, fake.client, config, entities=[spec.entity_id], chunk_seconds=86400
        )
        manifest = manifest_of(path)
        table = read_rows(tmp_path / "data")
        assert set(table.column("extraction_id").to_pylist()) == {manifest.extraction_id}
        assert table.num_rows == manifest.row_count == 2

    def test_re_run_writes_new_parts_because_rows_carry_the_run_id(
        self, tmp_path: Path, config: Path
    ) -> None:
        """Each run mints a fresh ``extraction_id`` (required by ``RAW_SAMPLE``),
        so a re-run writes new part files even when the input is identical.
        Consumers must resolve coverage from exactly one manifest — never union
        parts across runs, or they would double-count."""
        spec = ENTITY_SPECS[DEFAULT_ENTITY_IDS[0]]
        for _ in range(2):
            fake = FakePrometheus(series_for(spec.entity_id, [(_BASE + 5, 1.0)]))
            run_extract(
                tmp_path, fake.client, config, entities=[spec.entity_id], chunk_seconds=86400
            )
        parts = iter_parts(tmp_path / "data", SOURCE)
        assert len(parts) == 2  # one part per run, each with a different hash
        assert len(manifests_of(tmp_path / "data")) == 2
        for m_path in manifests_of(tmp_path / "data"):
            manifest = read_manifest(
                tmp_path / "data", json.loads(m_path.read_text())["extraction_id"]
            )
            assert len(manifest.part_files) == 1
            assert manifest.row_count == 1


class TestLogging:
    def test_window_before_the_archive_is_warned_about(
        self, tmp_path: Path, config: Path, caplog
    ) -> None:
        with caplog.at_level(logging.WARNING, logger="energy_baseline.extract_prom"):
            run_extract(
                tmp_path,
                FakePrometheus([]).client,
                config,
                start="2020-01-01T00:00:00Z",
                end="2020-01-02T00:00:00Z",
            )
        assert any("precedes the archive start" in r.getMessage() for r in caplog.records)

    def test_chunks_rows_and_entity_summaries_are_logged(
        self, tmp_path: Path, config: Path, caplog
    ) -> None:
        spec = ENTITY_SPECS[DEFAULT_ENTITY_IDS[0]]
        fake = FakePrometheus(series_for(spec.entity_id, [(_BASE + 5, 1.0)]))
        with caplog.at_level(logging.INFO, logger="energy_baseline.extract_prom"):
            run_extract(
                tmp_path, fake.client, config, entities=[spec.entity_id], chunk_seconds=86400
            )
        messages = [r.getMessage() for r in caplog.records]
        assert any(
            m.startswith("chunk 1 [2024-01-01T00:00:00Z, 2024-01-02T00:00:00Z)") for m in messages
        )
        assert any(
            spec.entity_id in m and "samples=1" in m and "first=2024-01-01T00:00:05Z" in m
            for m in messages
        )
        assert any("wrote manifest" in m for m in messages)

    def test_empty_run_warns_loudly(self, tmp_path: Path, config: Path, caplog) -> None:
        with caplog.at_level(logging.WARNING, logger="energy_baseline.extract_prom"):
            run_extract(
                tmp_path, FakePrometheus([]).client, config, entities=None, chunk_seconds=86400
            )
        assert any("NOT established by an empty response" in r.getMessage() for r in caplog.records)

    def test_query_text_is_logged_on_retry_but_never_a_token(
        self, tmp_path: Path, config: Path, caplog, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.setattr(extract_prom.time, "sleep", lambda s: None)
        fake = FakePrometheus(
            [], responder=lambda request, index: httpx.Response(429, text="slow down")
        )
        with caplog.at_level(logging.WARNING, logger="energy_baseline.extract_prom"):
            with pytest.raises(PrometheusHTTPError):
                run_extract(tmp_path, fake.client, config, max_retries=1, chunk_seconds=3600)
        text = "\n".join(r.getMessage() for r in caplog.records)
        assert "hass_sensor_energy_kwh" in text
        assert "token" not in text.lower()
