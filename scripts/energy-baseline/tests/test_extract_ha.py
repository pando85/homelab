"""Offline tests for ``extract_ha`` — ``httpx.MockTransport`` only, never the live server.

The fake HA server below emulates the history API semantics the extractor
depends on, including the synthetic carry-in timestamp behaviour.
"""

from __future__ import annotations

import hashlib
import json
import logging
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any

import httpx
import pyarrow as pa
import pyarrow.parquet as pq
import pytest

from energy_baseline.extract_ha import (
    DEFAULT_BASE_URL,
    DEFAULT_ENTITIES,
    PART_KIND,
    ROW_IDENTITY_FIELDS,
    SOURCE,
    ChunkOutcome,
    HAHTTPError,
    HAResponseError,
    StoredRowIndex,
    extract_ha_history,
    identity_digest,
    iso_z,
    iter_chunks,
    load_stored_row_index,
    parse_utc,
    resolve_base_url,
    resolve_entities,
    resolve_token,
    row_identity,
    rows_from_response,
    rows_to_table,
    summarize_chunks,
    table_to_bytes,
    to_epoch_us,
)
from energy_baseline.manifest import read_manifest
from energy_baseline.paths import iter_parts, part_path, write_part_atomic
from energy_baseline.schemas import RAW_ENUM_STATE, validate_table

# ---------------------------------------------------------------------------
# Constants & helpers
# ---------------------------------------------------------------------------

_T0 = datetime(2026, 1, 1, tzinfo=UTC)
_WASHER = "sensor.lavadora_current_status"
_DISHWASHER = "sensor.lavavajillas_current_status"
_SENTINEL_TOKEN = "SUPER_SECRET_SENTINEL_TOKEN_XYZ_9876"


def _ha_row(
    entity_id: str,
    state: str,
    last_changed: str,
    last_updated: str,
    attributes: dict | None = None,
) -> dict:
    """Build a single HA history row as the API returns it."""
    row: dict[str, Any] = {
        "entity_id": entity_id,
        "state": state,
        "last_changed": last_changed,
        "last_updated": last_updated,
    }
    if attributes is not None:
        row["attributes"] = attributes
    return row


def _read_parts(data_dir: Path) -> pa.Table:
    """Concatenate every HA history part."""
    frames = [pq.read_table(p) for p in iter_parts(data_dir, SOURCE)]
    assert frames, "no parts were written"
    return frames[0] if len(frames) == 1 else pa.concat_tables(frames)


@pytest.fixture
def config(tmp_path: Path) -> Path:
    path = tmp_path / "tariff.yaml"
    path.write_text("version: 1\nbattery:\n  capacity_kwh: 5.12\n  power_kw: 2.5\n")
    return path


@pytest.fixture(autouse=True)
def _restore_httpx_log_level():
    """Leave the shared ``httpx`` logger as it was found.

    The extractor raises ``httpx`` to WARNING to keep request URLs out of the
    log, and one test deliberately resets it first to prove the suppression is
    applied by the extractor rather than inherited.  Both mutate a
    process-global, so restore it for every test in this module.
    """
    logger = logging.getLogger("httpx")
    previous = logger.level
    yield
    logger.setLevel(previous)


def _run_extract(
    tmp_path: Path,
    client: httpx.Client,
    config: Path,
    **overrides: Any,
) -> Path:
    kwargs: dict[str, Any] = {
        "data_dir": str(tmp_path / "data"),
        "config_path": str(config),
        "start": "2026-01-01T00:00:00Z",
        "end": "2026-01-02T00:00:00Z",
        "entities": [_WASHER],
        "client": client,
        "token": _SENTINEL_TOKEN,
        "chunk_seconds": 86400,
    }
    kwargs.update(overrides)
    return extract_ha_history(**kwargs)


# ---------------------------------------------------------------------------
# Fake HA server
# ---------------------------------------------------------------------------


class FakeHA:
    """Serves HA history API responses from memory."""

    def __init__(
        self,
        *,
        history: dict[str, list[dict]] | None = None,
        responder: Any = None,
    ) -> None:
        # history: {entity_id: [row, ...]}
        self.history = history or {}
        self.responder = responder
        self.requests: list[httpx.Request] = []

    @property
    def client(self) -> httpx.Client:
        return httpx.Client(transport=httpx.MockTransport(self.handle))

    def handle(self, request: httpx.Request) -> httpx.Response:
        self.requests.append(request)
        if self.responder is not None:
            return self.responder(request, len(self.requests) - 1)

        if not request.url.path.startswith("/api/history/period/"):
            return httpx.Response(404, text="not found")

        # Check auth
        auth = request.headers.get("authorization", "")
        if not auth.startswith("Bearer "):
            return httpx.Response(401, text="unauthorized")

        result: list[list[dict]] = []
        for entity_id, rows in self.history.items():
            result.append(list(rows))
        return httpx.Response(200, json=result)


# ---------------------------------------------------------------------------
# Fake HA recorder that honours the requested window, like the real API
# ---------------------------------------------------------------------------


def _row_ts(row: dict, field: str) -> datetime:
    """A history row's timestamp field as an aware UTC datetime."""
    return parse_utc(row[field], name=field)


class StatefulFakeHA:
    """Serves history the way the recorder does, carry-in synthesis included.

    ``GET /api/history/period/{start}?end_time={end}`` returns, per entity, the
    last state at or before *start* with **both** timestamps rewritten to
    *start* — the synthetic carry-in — followed by every real row with
    ``start < last_updated <= end`` (the real ``end_time`` is inclusive, which is
    exactly why the extractor applies its own half-open filter).

    Unlike :class:`FakeHA` (which replays a fixed payload whatever the window),
    this makes overlapping re-extraction realistic: two runs see different
    carry-in rows for the same underlying states, and a row that a previous run
    already stored comes back with content that differs only in the
    ``extraction_id`` stamped by the extractor.
    """

    def __init__(self, timeline: dict[str, list[dict]]) -> None:
        self.timeline = {
            entity: sorted(rows, key=lambda row: _row_ts(row, "last_updated"))
            for entity, rows in timeline.items()
        }
        self.requests: list[httpx.Request] = []

    @property
    def client(self) -> httpx.Client:
        return httpx.Client(transport=httpx.MockTransport(self.handle))

    def handle(self, request: httpx.Request) -> httpx.Response:
        self.requests.append(request)
        if not request.url.path.startswith("/api/history/period/"):
            return httpx.Response(404, text="not found")
        auth = request.headers.get("authorization", "")
        if not auth.startswith("Bearer "):
            return httpx.Response(401, text="unauthorized")

        start = parse_utc(request.url.path.rsplit("/period/", 1)[-1], name="query start")
        end = parse_utc(request.url.params.get("end_time", ""), name="end_time")

        result: list[list[dict]] = []
        for rows in self.timeline.values():
            served: list[dict] = []
            prior = [row for row in rows if _row_ts(row, "last_updated") <= start]
            if prior:
                carry_in = dict(prior[-1])
                carry_in["last_changed"] = iso_z(start)
                carry_in["last_updated"] = iso_z(start)
                served.append(carry_in)
            served.extend(row for row in rows if start < _row_ts(row, "last_updated") <= end)
            result.append(served)
        return httpx.Response(200, json=result)


def _washer_timeline() -> dict[str, list[dict]]:
    """Four real transitions across 2026-01-01, none at a chunk boundary."""
    return {
        _WASHER: [
            _ha_row(_WASHER, "running", "2026-01-01T02:00:00Z", "2026-01-01T02:00:00Z"),
            _ha_row(_WASHER, "end", "2026-01-01T05:00:00Z", "2026-01-01T05:00:00Z"),
            _ha_row(_WASHER, "running", "2026-01-01T13:00:00Z", "2026-01-01T13:00:00Z"),
            _ha_row(_WASHER, "idle", "2026-01-01T20:00:00Z", "2026-01-01T20:00:00Z"),
        ]
    }


def _stored_rows(data_dir: Path) -> list[dict]:
    """Every row currently stored in HA parts, oldest chunk first."""
    return _read_parts(data_dir).to_pylist()


def _stored_keys(data_dir: Path) -> list[tuple]:
    """Logical identity of every stored HA row (duplicates included)."""
    return [row_identity(row) for row in _stored_rows(data_dir)]


def _flags_with_prefix(manifest: dict, prefix: str) -> list[str]:
    return [f for f in manifest["quality_flags"] if f.startswith(prefix)]


def _write_synthetic_part(
    data_dir: Path,
    rows: list[dict],
    *,
    chunk_start: datetime,
    chunk_end: datetime,
) -> Path:
    """Write a part directly, mimicking what a run would have stored.

    Used to construct store states the *current* extractor can no longer produce
    — notably the duplicate parts left by pre-dedup overlapping runs.  The rows
    carry their own ``extraction_id``, which is what makes two parts holding the
    same logical rows byte-different.
    """
    payload = table_to_bytes(rows_to_table(rows, rows[0]["extraction_id"]))
    path = part_path(
        data_dir,
        SOURCE,
        PART_KIND,
        chunk_start,
        chunk_end,
        hashlib.sha256(payload).hexdigest()[:8],
    )
    write_part_atomic(path, payload)
    return path


def _assembled_row(
    state: str,
    *,
    at: str,
    extraction_id: str,
    entity: str = _WASHER,
    attributes: dict | None = None,
    carry_in: bool = False,
) -> dict:
    """A schema-shaped row dict, as ``rows_from_response`` would return it."""
    dt = parse_utc(at, name="at")
    row: dict[str, Any] = {
        "entity": entity,
        "state": state,
        "attributes_json": (None if attributes is None else json.dumps(attributes, sort_keys=True)),
        "last_changed": dt,
        "last_updated": dt,
        "extraction_id": extraction_id,
        "carry_in": carry_in,
        "source_window_start": dt,
    }
    return row


# ---------------------------------------------------------------------------
# Tests: carry-in detection
# ---------------------------------------------------------------------------


class TestCarryIn:
    """Carry-in detection uses last_updated, not last_changed."""

    def test_carry_in_row_detected(self, tmp_path: Path, config: Path) -> None:
        """HA returns a carry-in row with last_updated == chunk_start."""
        start = "2026-01-01T00:00:00Z"
        end = "2026-01-01T06:00:00Z"
        # Carry-in: last_updated set to window start by HA
        history = {
            _WASHER: [
                _ha_row(_WASHER, "idle", start, start),
                _ha_row(_WASHER, "running", "2026-01-01T02:00:00Z", "2026-01-01T02:00:00Z"),
            ]
        }
        fake = FakeHA(history=history)
        _run_extract(tmp_path, fake.client, config, start=start, end=end)

        table = _read_parts(tmp_path / "data")
        states = table.column("state").to_pylist()
        carry_ins = table.column("carry_in").to_pylist()

        assert "idle" in states
        assert "running" in states
        # The carry-in row (idle at window start) must be flagged
        idle_idx = states.index("idle")
        assert carry_ins[idle_idx] is True
        # The real transition must not be flagged
        running_idx = states.index("running")
        assert carry_ins[running_idx] is False

    def test_carry_in_timestamp_preserved_verbatim(self, tmp_path: Path, config: Path) -> None:
        """The raw carry-in timestamp is stored, not replaced."""
        start = "2026-01-01T00:00:00Z"
        end = "2026-01-01T06:00:00Z"
        history = {
            _WASHER: [
                _ha_row(_WASHER, "idle", start, start),
            ]
        }
        fake = FakeHA(history=history)
        _run_extract(tmp_path, fake.client, config, start=start, end=end)

        table = _read_parts(tmp_path / "data")
        last_changed = table.column("last_changed").to_pylist()
        # The raw value is preserved (even though it's synthetic)
        assert last_changed[0] == datetime(2026, 1, 1, 0, 0, 0, tzinfo=UTC)

    def test_attribute_only_update_not_carry_in(self, tmp_path: Path, config: Path) -> None:
        """Attribute-only update: old last_changed but NEW last_updated.

        This is a real event within the window, not a carry-in.  Using
        last_updated (not last_changed) for carry-in detection correctly
        preserves it.
        """
        start = "2026-01-01T00:00:00Z"
        end = "2026-01-01T06:00:00Z"
        # State "running" changed at 01:00, attributes updated at 03:00 (old
        # last_changed = 01:00, new last_updated = 03:00)
        history = {
            _WASHER: [
                _ha_row(_WASHER, "idle", start, start),
                _ha_row(_WASHER, "running", "2026-01-01T01:00:00Z", "2026-01-01T01:00:00Z"),
                _ha_row(
                    _WASHER,
                    "running",
                    "2026-01-01T01:00:00Z",  # old last_changed — state did not change
                    "2026-01-01T03:00:00Z",  # new last_updated — real attribute update event
                ),
            ]
        }
        fake = FakeHA(history=history)
        _run_extract(tmp_path, fake.client, config, start=start, end=end)

        table = _read_parts(tmp_path / "data")
        states = table.column("state").to_pylist()
        carry_ins = table.column("carry_in").to_pylist()
        # idle is the carry-in
        assert carry_ins[states.index("idle")] is True
        # Both "running" rows are real events (not carry-in) because their
        # last_updated > chunk_start
        running_indices = [i for i, s in enumerate(states) if s == "running"]
        assert len(running_indices) == 2, "both attribute updates must be preserved"
        for i in running_indices:
            assert carry_ins[i] is False

    def test_multiple_events_same_last_changed_different_last_updated(
        self, tmp_path: Path, config: Path
    ) -> None:
        """Two attribute events sharing last_changed but different last_updated are both kept."""
        start = "2026-01-01T00:00:00Z"
        end = "2026-01-01T06:00:00Z"
        lc = "2026-01-01T02:00:00Z"  # same last_changed for both
        history = {
            _WASHER: [
                _ha_row(_WASHER, "idle", start, start),
                _ha_row(_WASHER, "running", lc, "2026-01-01T02:00:00Z"),
                _ha_row(
                    _WASHER,
                    "running",
                    lc,  # same last_changed
                    "2026-01-01T04:00:00Z",  # different last_updated
                    attributes={"phase": 2},
                ),
            ]
        }
        fake = FakeHA(history=history)
        _run_extract(tmp_path, fake.client, config, start=start, end=end)

        table = _read_parts(tmp_path / "data")
        running = [r for r in table.to_pylist() if r["state"] == "running"]
        assert len(running) == 2
        assert running[0]["last_updated"] != running[1]["last_updated"]

    def test_only_first_pre_window_row_is_carry_in(self, tmp_path: Path, config: Path) -> None:
        """Multiple rows with last_updated <= chunk_start: only the first is carry-in.

        Any subsequent stale rows (before chunk_start) are dropped as
        out-of-window, not flagged as carry-in transitions.
        """
        start = "2026-01-01T06:00:00Z"
        end = "2026-01-01T12:00:00Z"
        history = {
            _WASHER: [
                # Two stale rows from before chunk_start; only first is carry-in
                _ha_row(_WASHER, "old1", "2026-01-01T01:00:00Z", "2026-01-01T01:00:00Z"),
                _ha_row(_WASHER, "old2", "2026-01-01T02:00:00Z", "2026-01-01T02:00:00Z"),
                # Real event in window
                _ha_row(_WASHER, "running", start, "2026-01-01T08:00:00Z"),
            ]
        }
        fake = FakeHA(history=history)
        _run_extract(tmp_path, fake.client, config, start=start, end=end)

        table = _read_parts(tmp_path / "data")
        states = table.column("state").to_pylist()
        carry_ins = table.column("carry_in").to_pylist()
        # old1 is the carry-in
        assert "old1" in states
        assert carry_ins[states.index("old1")] is True
        # old2 is dropped (stale, not the first carry-in)
        assert "old2" not in states
        # running is a real event
        assert "running" in states
        assert carry_ins[states.index("running")] is False


# ---------------------------------------------------------------------------
# Tests: half-open window
# ---------------------------------------------------------------------------


class TestHalfOpenWindow:
    """Event at start is included; event at end is excluded."""

    def test_event_at_start_included(self, tmp_path: Path, config: Path) -> None:
        start = "2026-01-01T00:00:00Z"
        end = "2026-01-01T06:00:00Z"
        history = {
            _WASHER: [
                _ha_row(_WASHER, "end", start, start),
            ]
        }
        fake = FakeHA(history=history)
        _run_extract(tmp_path, fake.client, config, start=start, end=end)

        table = _read_parts(tmp_path / "data")
        assert table.num_rows == 1
        assert table.column("state").to_pylist()[0] == "end"
        # last_updated == chunk_start → ambiguous, conservatively carry_in
        assert table.column("carry_in").to_pylist()[0] is True

    def test_event_at_end_excluded(self, tmp_path: Path, config: Path) -> None:
        start = "2026-01-01T00:00:00Z"
        end = "2026-01-01T06:00:00Z"
        history = {
            _WASHER: [
                _ha_row(_WASHER, "idle", start, start),
                # last_updated == end → excluded (half-open [start, end))
                _ha_row(_WASHER, "running", end, end),
            ]
        }
        fake = FakeHA(history=history)
        _run_extract(tmp_path, fake.client, config, start=start, end=end)

        table = _read_parts(tmp_path / "data")
        states = table.column("state").to_pylist()
        assert "running" not in states
        assert "idle" in states

    def test_future_last_updated_excluded(self, tmp_path: Path, config: Path) -> None:
        """Row with last_updated > chunk_end but old last_changed is excluded."""
        start = "2026-01-01T00:00:00Z"
        end = "2026-01-01T06:00:00Z"
        history = {
            _WASHER: [
                _ha_row(_WASHER, "idle", start, start),
                # last_changed within window but last_updated is in next chunk
                _ha_row(
                    _WASHER,
                    "running",
                    "2026-01-01T05:00:00Z",
                    "2026-01-01T07:00:00Z",  # last_updated >= chunk_end → excluded
                ),
            ]
        }
        fake = FakeHA(history=history)
        _run_extract(tmp_path, fake.client, config, start=start, end=end)

        table = _read_parts(tmp_path / "data")
        states = table.column("state").to_pylist()
        assert "running" not in states

    def test_event_just_before_end_included(self, tmp_path: Path, config: Path) -> None:
        start = "2026-01-01T00:00:00Z"
        end = "2026-01-01T06:00:00Z"
        just_before = "2026-01-01T05:59:59Z"
        history = {
            _WASHER: [
                _ha_row(_WASHER, "idle", start, start),
                _ha_row(_WASHER, "running", just_before, just_before),
            ]
        }
        fake = FakeHA(history=history)
        _run_extract(tmp_path, fake.client, config, start=start, end=end)

        table = _read_parts(tmp_path / "data")
        states = table.column("state").to_pylist()
        assert "running" in states


# ---------------------------------------------------------------------------
# Tests: multi-chunk coverage
# ---------------------------------------------------------------------------


class TestMultiChunk:
    """Chunks cover [start, end) with no gaps and no duplicates."""

    def test_two_chunks_no_overlap(self, tmp_path: Path, config: Path) -> None:
        start = "2026-01-01T00:00:00Z"
        end = "2026-01-02T00:00:00Z"
        # Events in both halves of the day
        history = {
            _WASHER: [
                _ha_row(_WASHER, "idle", start, start),
                _ha_row(_WASHER, "running", "2026-01-01T10:00:00Z", "2026-01-01T10:00:00Z"),
                _ha_row(_WASHER, "end", "2026-01-01T20:00:00Z", "2026-01-01T20:00:00Z"),
            ]
        }
        fake = FakeHA(history=history)
        # Use 12h chunks → 2 chunks
        _run_extract(
            tmp_path,
            fake.client,
            config,
            start=start,
            end=end,
            chunk_seconds=43200,
        )

        # Both chunks should produce parts
        parts = list(iter_parts(tmp_path / "data", SOURCE))
        assert len(parts) == 2

        table = _read_parts(tmp_path / "data")
        states = table.column("state").to_pylist()
        # All 3 events should be present (carry-in from chunk 2 may duplicate
        # the chunk 1 last state — that's expected and correct)
        assert "idle" in states
        assert "running" in states
        assert "end" in states

    def test_chunks_cover_full_window(self) -> None:
        """iter_chunks partitions [start, end) exactly."""
        start = datetime(2026, 1, 1, tzinfo=UTC)
        end = datetime(2026, 1, 3, tzinfo=UTC)
        chunks = list(iter_chunks(start, end, 86400))
        assert len(chunks) == 2
        assert chunks[0] == (start, start + timedelta(days=1))
        assert chunks[1] == (start + timedelta(days=1), end)


# ---------------------------------------------------------------------------
# Tests: state strings and attributes preserved verbatim
# ---------------------------------------------------------------------------


class TestStatePreservation:
    """Full state strings and attributes_json preserved exactly."""

    def test_state_strings_preserved(self, tmp_path: Path, config: Path) -> None:
        start = "2026-01-01T00:00:00Z"
        end = "2026-01-01T06:00:00Z"
        history = {
            _WASHER: [
                _ha_row(_WASHER, "end", start, start),
                _ha_row(_WASHER, "running", "2026-01-01T01:00:00Z", "2026-01-01T01:00:00Z"),
                _ha_row(
                    _WASHER,
                    "funcionando",
                    "2026-01-01T02:00:00Z",
                    "2026-01-01T02:00:00Z",
                ),
            ]
        }
        fake = FakeHA(history=history)
        _run_extract(tmp_path, fake.client, config, start=start, end=end)

        table = _read_parts(tmp_path / "data")
        states = table.column("state").to_pylist()
        assert "end" in states
        assert "running" in states
        assert "funcionando" in states

    def test_unicode_state_preserved(self, tmp_path: Path, config: Path) -> None:
        start = "2026-01-01T00:00:00Z"
        end = "2026-01-01T06:00:00Z"
        history = {
            _WASHER: [
                _ha_row(_WASHER, "séchage", start, start),
                _ha_row(
                    _WASHER,
                    "lavado_con_ñ",
                    "2026-01-01T01:00:00Z",
                    "2026-01-01T01:00:00Z",
                ),
            ]
        }
        fake = FakeHA(history=history)
        _run_extract(tmp_path, fake.client, config, start=start, end=end)

        table = _read_parts(tmp_path / "data")
        states = table.column("state").to_pylist()
        assert "séchage" in states
        assert "lavado_con_ñ" in states

    def test_attributes_json_preserved(self, tmp_path: Path, config: Path) -> None:
        start = "2026-01-01T00:00:00Z"
        end = "2026-01-01T06:00:00Z"
        attrs = {"cycle": "algodón", "temp": 60, "remaining": "01:30"}
        history = {
            _WASHER: [
                _ha_row(_WASHER, "running", start, start, attributes=attrs),
            ]
        }
        fake = FakeHA(history=history)
        _run_extract(tmp_path, fake.client, config, start=start, end=end)

        table = _read_parts(tmp_path / "data")
        attrs_json = table.column("attributes_json").to_pylist()
        parsed = json.loads(attrs_json[0])
        assert parsed["cycle"] == "algodón"
        assert parsed["temp"] == 60
        assert parsed["remaining"] == "01:30"


# ---------------------------------------------------------------------------
# Tests: request parameters
# ---------------------------------------------------------------------------


class TestRequestParameters:
    """significant_changes_only=0 present; forbidden params absent."""

    def test_required_params_present(self, tmp_path: Path, config: Path) -> None:
        start = "2026-01-01T00:00:00Z"
        end = "2026-01-01T06:00:00Z"
        history = {_WASHER: [_ha_row(_WASHER, "idle", start, start)]}
        fake = FakeHA(history=history)
        _run_extract(tmp_path, fake.client, config, start=start, end=end)

        assert len(fake.requests) >= 1
        req = fake.requests[0]
        params = req.url.params
        assert params.get("significant_changes_only") == "0"
        assert "minimal_response" not in params
        assert "no_attributes" not in params
        assert "skip_initial_state" not in params

    def test_filter_entity_id_present(self, tmp_path: Path, config: Path) -> None:
        start = "2026-01-01T00:00:00Z"
        end = "2026-01-01T06:00:00Z"
        history = {_WASHER: [_ha_row(_WASHER, "idle", start, start)]}
        fake = FakeHA(history=history)
        _run_extract(
            tmp_path,
            fake.client,
            config,
            start=start,
            end=end,
            entities=[_WASHER, _DISHWASHER],
        )

        req = fake.requests[0]
        filter_val = req.url.params.get("filter_entity_id", "")
        assert _WASHER in filter_val
        assert _DISHWASHER in filter_val

    def test_auth_header_present(self, tmp_path: Path, config: Path) -> None:
        start = "2026-01-01T00:00:00Z"
        end = "2026-01-01T06:00:00Z"
        history = {_WASHER: [_ha_row(_WASHER, "idle", start, start)]}
        fake = FakeHA(history=history)
        _run_extract(tmp_path, fake.client, config, start=start, end=end)

        req = fake.requests[0]
        assert req.headers["authorization"] == f"Bearer {_SENTINEL_TOKEN}"


# ---------------------------------------------------------------------------
# Tests: empty entity list → coverage flag
# ---------------------------------------------------------------------------


class TestEmptyResults:
    """Empty entity list → explicit coverage flag, not silent success."""

    def test_empty_entity_flagged(self, tmp_path: Path, config: Path) -> None:
        start = "2026-01-01T00:00:00Z"
        end = "2026-01-01T06:00:00Z"
        # HA returns empty lists for both entities
        history = {_WASHER: [], _DISHWASHER: []}
        fake = FakeHA(history=history)
        manifest_path = _run_extract(
            tmp_path,
            fake.client,
            config,
            start=start,
            end=end,
            entities=[_WASHER, _DISHWASHER],
        )

        manifest_data = json.loads(manifest_path.read_text())
        manifest = read_manifest(tmp_path / "data", manifest_data["extraction_id"])
        assert manifest.row_count == 0
        assert any("no_rows_for_entity" in f for f in manifest.quality_flags)
        assert any("no_rows_extracted:all" in f for f in manifest.quality_flags)


# ---------------------------------------------------------------------------
# Tests: per-chunk accounting (an empty chunk must never be silent)
# ---------------------------------------------------------------------------


def _payload_for_chunk_start(payloads: dict[str, list]) -> Any:
    """Build a responder that serves a fixed payload per chunk query start.

    Keyed on the ``/api/history/period/<start>`` path, so one chunk can come
    back empty while a later one has rows — the shape of the verified 2026-10-14
    run that declared 10 chunks and wrote 9 parts with no flag anywhere.
    """

    def responder(request: httpx.Request, idx: int) -> httpx.Response:
        start = request.url.path.rsplit("/period/", 1)[-1]
        return httpx.Response(200, json=payloads.get(start, []))

    return responder


class TestChunkAccounting:
    """Every declared chunk is accounted for, including the ones that return nothing."""

    def _empty_then_full(self, tmp_path: Path, config: Path, **kw: Any) -> dict:
        """Run two 24h chunks where the first declares zero rows."""
        c1 = "2026-01-01T00:00:00Z"
        c2 = "2026-01-02T00:00:00Z"
        fake = FakeHA(
            responder=_payload_for_chunk_start({c1: [], c2: [[_ha_row(_WASHER, "idle", c2, c2)]]})
        )
        path = _run_extract(
            tmp_path,
            fake.client,
            config,
            start=c1,
            end="2026-01-03T00:00:00Z",
            chunk_seconds=86400,
            **kw,
        )
        return json.loads(path.read_text())

    def test_zero_row_chunk_flags_its_window(self, tmp_path: Path, config: Path) -> None:
        """A declared chunk with zero rows names that chunk's window in quality_flags."""
        data = self._empty_then_full(tmp_path, config)
        window = "[2026-01-01T00:00:00Z, 2026-01-02T00:00:00Z)"
        flags = data["quality_flags"]
        empty = [f for f in flags if f.startswith(f"empty_chunk:{window}")]
        assert len(empty) == 1, f"expected one empty_chunk flag naming {window}, got {flags}"
        assert "0 rows kept, no part file written" in empty[0]

    def test_flag_states_the_cause_is_not_determinable(self, tmp_path: Path, config: Path) -> None:
        """The flag names what it can prove and refuses to guess why the chunk was empty."""
        data = self._empty_then_full(tmp_path, config)
        flag = next(f for f in data["quality_flags"] if f.startswith("empty_chunk:"))
        for clause in (
            "cause not determinable from this response",
            "(a) no state transitions recorded in the window",
            "(b) the recorder having already purged the window",
            "(c) a degenerate empty API response",
        ):
            assert clause in flag, flag
        # No assertion of a single cause, and no invented retention measurement.
        assert "retention is" not in flag
        assert "days" not in flag

    def test_rows_outside_window_flagged_separately(self, tmp_path: Path, config: Path) -> None:
        """Raw rows returned but all filtered out is distinguishable, and is recorded."""
        c1 = "2026-01-01T00:00:00Z"
        c2 = "2026-01-02T00:00:00Z"
        # Chunk 1's payload carries a row timestamped at its own end -> filtered out.
        fake = FakeHA(
            responder=_payload_for_chunk_start(
                {
                    c1: [[_ha_row(_WASHER, "running", c2, c2)]],
                    c2: [[_ha_row(_WASHER, "idle", c2, c2)]],
                }
            )
        )
        path = _run_extract(
            tmp_path,
            fake.client,
            config,
            start=c1,
            end="2026-01-03T00:00:00Z",
            chunk_seconds=86400,
        )
        data = json.loads(path.read_text())
        prefix = "empty_chunk_after_window_filter:"
        flag = next(f for f in data["quality_flags"] if f.startswith(prefix))
        assert "1 raw row" in flag, flag
        assert "the response carried no rows" not in flag
        accounting = data["params"]["chunk_accounting"]
        assert accounting["empty_chunks"] == 1
        assert accounting["payload_empty_chunks"] == 0

    def test_declared_and_written_counts_both_recorded(self, tmp_path: Path, config: Path) -> None:
        """Both counts appear in the manifest, so the gap needs no eyeball diff."""
        data = self._empty_then_full(tmp_path, config)
        accounting = data["params"]["chunk_accounting"]
        assert accounting["declared_chunks"] == 2
        assert accounting["parts_written"] == 1
        assert accounting["requested_chunks"] == 2
        assert accounting["succeeded_chunks"] == 2
        assert accounting["chunks_with_rows"] == 1
        assert accounting["mismatch"] is True
        assert data["params"]["chunks_declared"] == 2
        assert data["params"]["parts_written"] == 1
        assert len(data["part_files"]) == accounting["parts_written"]

    def test_mismatch_is_surfaced_without_diffing_numbers(
        self, tmp_path: Path, config: Path
    ) -> None:
        """A dedicated flag and the coverage note both state the disagreement."""
        data = self._empty_then_full(tmp_path, config)
        mismatch = [f for f in data["quality_flags"] if f.startswith("chunk_accounting_mismatch:")]
        assert len(mismatch) == 1, data["quality_flags"]
        assert "declared=2" in mismatch[0]
        assert "parts_written=1" in mismatch[0]
        assert "[2026-01-01T00:00:00Z, 2026-01-02T00:00:00Z)" in mismatch[0]
        assert "MISMATCH declared_chunks=2 != parts_written=1" in data["coverage_notes"]

    def test_per_chunk_ledger_records_every_declared_chunk(
        self, tmp_path: Path, config: Path
    ) -> None:
        """Structured outcomes cover each declared chunk with requested/succeeded/rows."""
        data = self._empty_then_full(tmp_path, config)
        ledger = data["params"]["chunk_outcomes"]
        assert len(ledger) == data["params"]["chunks_declared"] == 2
        first, second = ledger
        assert first["index"] == 1
        assert first["window"] == "[2026-01-01T00:00:00Z, 2026-01-02T00:00:00Z)"
        assert first["requested"] is True
        assert first["request_succeeded"] is True
        assert first["raw_rows"] == 0
        assert first["kept_rows"] == 0
        assert first["empty"] is True
        assert first["part_file"] is None
        assert first["flags"]
        assert second["index"] == 2
        assert second["kept_rows"] == 1
        assert second["empty"] is False
        assert isinstance(second["part_file"], str)
        assert second["flags"] == []

    def test_empty_chunk_writes_no_part_but_is_not_lost(self, tmp_path: Path, config: Path) -> None:
        """The skipped window has no part file, yet its absence is recorded."""
        data = self._empty_then_full(tmp_path, config)
        parts = list(iter_parts(tmp_path / "data", SOURCE))
        assert len(parts) == 1
        assert "20260101T000000Z" not in parts[0].name
        assert data["row_count"] == 1
        assert data["params"]["chunk_accounting"]["empty_chunk_windows"] == [
            "[2026-01-01T00:00:00Z, 2026-01-02T00:00:00Z)"
        ]

    def test_successful_run_produces_no_empty_chunk_flag(
        self, tmp_path: Path, config: Path
    ) -> None:
        """A run where every declared chunk yields rows is clean by construction."""
        c1 = "2026-01-01T00:00:00Z"
        c2 = "2026-01-02T00:00:00Z"
        fake = FakeHA(
            responder=_payload_for_chunk_start(
                {
                    c1: [[_ha_row(_WASHER, "idle", c1, c1)]],
                    c2: [[_ha_row(_WASHER, "running", c2, c2)]],
                }
            )
        )
        path = _run_extract(
            tmp_path,
            fake.client,
            config,
            start=c1,
            end="2026-01-03T00:00:00Z",
            chunk_seconds=86400,
        )
        data = json.loads(path.read_text())
        assert data["quality_flags"] == []
        accounting = data["params"]["chunk_accounting"]
        assert accounting["mismatch"] is False
        assert accounting["declared_chunks"] == accounting["parts_written"] == 2
        assert accounting["empty_chunks"] == 0
        assert "declared_chunks=2 parts_written=2 (agree)" in data["coverage_notes"]
        assert not any("empty_chunk" in f for f in data["quality_flags"])
        assert all(o["flags"] == [] for o in data["params"]["chunk_outcomes"])

    def test_no_retention_probe_and_no_synthetic_write(self, tmp_path: Path, config: Path) -> None:
        """Causes are not resolved by guessing: GET history only, one per chunk."""
        c1 = "2026-01-01T00:00:00Z"
        c2 = "2026-01-02T00:00:00Z"
        fake = FakeHA(responder=_payload_for_chunk_start({c1: [], c2: [[]]}))
        _run_extract(
            tmp_path,
            fake.client,
            config,
            start=c1,
            end="2026-01-03T00:00:00Z",
            chunk_seconds=86400,
        )
        assert len(fake.requests) == 2, [r.url.path for r in fake.requests]
        assert {r.method for r in fake.requests} == {"GET"}
        assert all(r.url.path.startswith("/api/history/period/") for r in fake.requests)

    def test_mid_run_failure_still_writes_no_manifest(
        self, tmp_path: Path, config: Path, caplog: pytest.LogCaptureFixture
    ) -> None:
        """Chunk 1 succeeds, chunk 2 fails: parts may orphan, but no success manifest."""
        c1 = "2026-01-01T00:00:00Z"
        c2 = "2026-01-02T00:00:00Z"
        covered = f"[{c1}, {c2})"
        uncovered = f"[{c2}, 2026-01-03T00:00:00Z)"

        def responder(request: httpx.Request, idx: int) -> httpx.Response:
            start = request.url.path.rsplit("/period/", 1)[-1]
            if start == c1:
                return httpx.Response(200, json=[[_ha_row(_WASHER, "idle", c1, c1)]])
            return httpx.Response(503, text="unavailable")

        fake = FakeHA(responder=responder)
        manifests_dir = tmp_path / "data" / "manifests"
        with caplog.at_level(logging.WARNING):
            with pytest.raises(HAHTTPError):
                _run_extract(
                    tmp_path,
                    fake.client,
                    config,
                    start=c1,
                    end="2026-01-03T00:00:00Z",
                    chunk_seconds=86400,
                    max_retries=0,
                )

        assert not manifests_dir.exists() or list(manifests_dir.glob("*.json")) == []
        # Chunk 1's part may survive as an orphan; coverage comes from manifests.
        abort = [
            r.getMessage() for r in caplog.records if "aborted at declared chunk" in r.getMessage()
        ]
        assert len(abort) == 1, [r.getMessage() for r in caplog.records]
        assert covered in abort[0], abort[0]
        assert uncovered in abort[0], abort[0]
        assert "NO manifest written" in abort[0]

    def test_mid_run_response_violation_writes_no_manifest(
        self, tmp_path: Path, config: Path, caplog: pytest.LogCaptureFixture
    ) -> None:
        """A malformed row in a later chunk is a mid-run failure too: no manifest."""
        c1 = "2026-01-01T00:00:00Z"
        c2 = "2026-01-02T00:00:00Z"
        bad = _ha_row(_WASHER, "running", "not-a-timestamp", "not-a-timestamp")
        fake = FakeHA(
            responder=_payload_for_chunk_start(
                {c1: [[_ha_row(_WASHER, "idle", c1, c1)]], c2: [[bad]]}
            )
        )
        manifests_dir = tmp_path / "data" / "manifests"
        with caplog.at_level(logging.ERROR):
            with pytest.raises(HAResponseError):
                _run_extract(
                    tmp_path,
                    fake.client,
                    config,
                    start=c1,
                    end="2026-01-03T00:00:00Z",
                    chunk_seconds=86400,
                )
        assert not manifests_dir.exists() or list(manifests_dir.glob("*.json")) == []
        abort = [
            r.getMessage() for r in caplog.records if "aborted at declared chunk" in r.getMessage()
        ]
        assert len(abort) == 1, [r.getMessage() for r in caplog.records]
        assert f"[{c1}, {c2})" in abort[0]
        assert _SENTINEL_TOKEN not in abort[0]

    def test_empty_chunk_warns_in_logs(
        self, tmp_path: Path, config: Path, caplog: pytest.LogCaptureFixture
    ) -> None:
        """The gap is also logged, not only written to disk."""
        with caplog.at_level(logging.WARNING):
            self._empty_then_full(tmp_path, config)
        text = caplog.text
        assert "returned zero rows" in text
        assert "declared/written chunk mismatch" in text
        assert DEFAULT_BASE_URL not in text


# ---------------------------------------------------------------------------
# Tests: no endpoint or credential identity anywhere in the written record
# ---------------------------------------------------------------------------


class TestNoCredentialOrEndpointLeakage:
    """Neither the token nor the base URL may reach flags, manifest fields or logs."""

    _FAKE_URL = "https://ha-token-leak-canary.example.invalid"
    _FAKE_HOST = "ha-token-leak-canary.example.invalid"

    def _assert_clean(self, payload: str) -> None:
        assert _SENTINEL_TOKEN not in payload
        assert "Bearer" not in payload
        assert self._FAKE_URL not in payload
        assert self._FAKE_HOST not in payload
        assert "http://" not in payload
        assert "https://" not in payload

    def test_manifest_has_no_token_or_base_url(
        self, tmp_path: Path, config: Path, caplog: pytest.LogCaptureFixture
    ) -> None:
        """Every manifest field, including the new ledger and flags, is identity-free."""
        c1 = "2026-01-01T00:00:00Z"
        c2 = "2026-01-02T00:00:00Z"
        fake = FakeHA(responder=_payload_for_chunk_start({c1: [], c2: [[]]}))
        with caplog.at_level(logging.DEBUG):
            path = _run_extract(
                tmp_path,
                fake.client,
                config,
                start=c1,
                end="2026-01-03T00:00:00Z",
                chunk_seconds=86400,
                base_url=self._FAKE_URL,
            )
        self._assert_clean(path.read_text())
        # The default host must not be recorded either.
        assert DEFAULT_BASE_URL not in path.read_text()
        data = json.loads(path.read_text())
        for flag in data["quality_flags"]:
            self._assert_clean(flag)
        for outcome in data["params"]["chunk_outcomes"]:
            self._assert_clean(json.dumps(outcome))
        self._assert_clean(data["coverage_notes"])
        assert data["params"]["endpoint"] == "home-assistant-history-api"
        assert len(data["params"]["endpoint_instance_sha256_16"]) == 16
        assert "base_url" not in data["params"]
        # And nothing reached the log.
        joined = "\n".join(r.getMessage() for r in caplog.records)
        self._assert_clean(joined)

    def test_httpx_request_logging_stays_suppressed(
        self, tmp_path: Path, config: Path, caplog: pytest.LogCaptureFixture
    ) -> None:
        """httpx logs each request at INFO with the full URL; the extractor stops it."""
        logging.getLogger("httpx").setLevel(logging.NOTSET)
        start = "2026-01-01T00:00:00Z"
        end = "2026-01-01T06:00:00Z"
        fake = FakeHA(responder=lambda request, idx: httpx.Response(200, json=[]))
        with caplog.at_level(logging.INFO):
            _run_extract(tmp_path, fake.client, config, start=start, end=end)
        assert [r.getMessage() for r in caplog.records if r.name == "httpx"] == []
        assert "HTTP Request" not in caplog.text
        assert DEFAULT_BASE_URL not in caplog.text

    def test_url_in_transport_error_is_redacted(self, tmp_path: Path, config: Path) -> None:
        """A transport failure message that embeds the URL does not leak it."""
        start = "2026-01-01T00:00:00Z"
        end = "2026-01-01T06:00:00Z"
        url = self._FAKE_URL

        def responder(request: httpx.Request, idx: int) -> httpx.Response:
            raise httpx.ConnectError(f"failed to connect to {url} with {url[8:]}")

        fake = FakeHA(responder=responder)
        with pytest.raises(HAHTTPError) as exc_info:
            _run_extract(
                tmp_path,
                fake.client,
                config,
                start=start,
                end=end,
                base_url=url,
                max_retries=0,
            )
        msg = str(exc_info.value)
        self._assert_clean(msg)
        assert "<REDACTED>" in msg


# ---------------------------------------------------------------------------
# Tests: 401 fails immediately, token not in error
# ---------------------------------------------------------------------------


class TestAuthFailure:
    """401 fails immediately without retry; token never in error or logs."""

    def test_401_no_retry(self, tmp_path: Path, config: Path) -> None:
        call_count = 0

        def responder(request: httpx.Request, idx: int) -> httpx.Response:
            nonlocal call_count
            call_count += 1
            return httpx.Response(401, text="unauthorized")

        fake = FakeHA(responder=responder)
        with pytest.raises(HAHTTPError) as exc_info:
            _run_extract(tmp_path, fake.client, config)

        # Only one attempt — no retries for auth failures
        assert call_count == 1
        assert exc_info.value.status_code == 401

    def test_token_not_in_error(self, tmp_path: Path, config: Path) -> None:
        def responder(request: httpx.Request, idx: int) -> httpx.Response:
            return httpx.Response(401, text=f"bad token: {_SENTINEL_TOKEN}")

        fake = FakeHA(responder=responder)
        with pytest.raises(HAHTTPError) as exc_info:
            _run_extract(tmp_path, fake.client, config)

        error_text = str(exc_info.value)
        assert _SENTINEL_TOKEN not in error_text

    def test_token_not_in_logs(
        self, tmp_path: Path, config: Path, caplog: pytest.LogCaptureFixture
    ) -> None:
        def responder(request: httpx.Request, idx: int) -> httpx.Response:
            return httpx.Response(401, text="unauthorized")

        fake = FakeHA(responder=responder)
        with caplog.at_level(logging.WARNING):
            with pytest.raises(HAHTTPError):
                _run_extract(tmp_path, fake.client, config)

        for record in caplog.records:
            assert _SENTINEL_TOKEN not in record.getMessage()

    def test_token_not_in_5xx_error(self, tmp_path: Path, config: Path) -> None:
        """A 5xx body that echoes the auth header must not leak the token into the error.

        The token can appear in a response body if a proxy logs the request.
        ``_sanitize_error`` redacts it before the message reaches the exception.
        """

        def responder(request: httpx.Request, idx: int) -> httpx.Response:
            return httpx.Response(503, text=f"upstream echoed: Bearer {_SENTINEL_TOKEN}")

        fake = FakeHA(responder=responder)
        with pytest.raises(HAHTTPError) as exc_info:
            _run_extract(tmp_path, fake.client, config, max_retries=0)
        assert _SENTINEL_TOKEN not in str(exc_info.value)
        assert "<REDACTED>" in str(exc_info.value)


# ---------------------------------------------------------------------------
# Tests: unexpected entity rejected
# ---------------------------------------------------------------------------


class TestUnexpectedEntity:
    """Entity not in the requested set → rejected."""

    def test_unexpected_entity_raises(self, tmp_path: Path, config: Path) -> None:
        start = "2026-01-01T00:00:00Z"
        end = "2026-01-01T06:00:00Z"
        # HA returns an entity we didn't ask for
        history = {
            _WASHER: [_ha_row(_WASHER, "idle", start, start)],
            "sensor.unknown_entity": [_ha_row("sensor.unknown_entity", "on", start, start)],
        }
        fake = FakeHA(history=history)
        with pytest.raises(HAHTTPError, match="unexpected entity"):
            _run_extract(tmp_path, fake.client, config, start=start, end=end, entities=[_WASHER])


# ---------------------------------------------------------------------------
# Tests: schema equality
# ---------------------------------------------------------------------------


class TestSchemaEquality:
    """Written parts validate against RAW_ENUM_STATE."""

    def test_parts_match_schema(self, tmp_path: Path, config: Path) -> None:
        start = "2026-01-01T00:00:00Z"
        end = "2026-01-01T06:00:00Z"
        history = {
            _WASHER: [
                _ha_row(_WASHER, "idle", start, start),
                _ha_row(_WASHER, "running", "2026-01-01T02:00:00Z", "2026-01-01T02:00:00Z"),
            ]
        }
        fake = FakeHA(history=history)
        _run_extract(tmp_path, fake.client, config, start=start, end=end)

        table = _read_parts(tmp_path / "data")
        # This validates schema equality + null invariants
        validate_table("RAW_ENUM_STATE", table)
        assert table.schema == RAW_ENUM_STATE

    def test_source_window_start_is_chunk_start(self, tmp_path: Path, config: Path) -> None:
        """``source_window_start`` records the chunk query start, not the overall window start."""
        start = "2026-01-01T00:00:00Z"
        end = "2026-01-02T00:00:00Z"
        history = {
            _WASHER: [
                _ha_row(_WASHER, "idle", start, start),
                _ha_row(_WASHER, "running", "2026-01-01T10:00:00Z", "2026-01-01T10:00:00Z"),
                _ha_row(_WASHER, "end", "2026-01-01T20:00:00Z", "2026-01-01T20:00:00Z"),
            ],
        }
        fake = FakeHA(history=history)
        _run_extract(tmp_path, fake.client, config, start=start, end=end, chunk_seconds=43200)

        parts = sorted(iter_parts(tmp_path / "data", SOURCE))
        assert len(parts) == 2
        # Each chunk's rows carry that chunk's start, not the overall window start.
        # Chunk 1 starts at 00:00, chunk 2 starts at 12:00.
        sws_per_part = [
            sorted({r["source_window_start"] for r in pq.read_table(p).to_pylist()}) for p in parts
        ]
        assert sws_per_part[0] == [datetime(2026, 1, 1, 0, 0, 0, tzinfo=UTC)]
        assert sws_per_part[1] == [datetime(2026, 1, 1, 12, 0, 0, tzinfo=UTC)]


# ---------------------------------------------------------------------------
# Tests: deterministic ordering
# ---------------------------------------------------------------------------


class TestDeterministicOrdering:
    """Two identical runs produce byte-identical parts."""

    def test_ordering_across_runs(self, tmp_path: Path, config: Path) -> None:
        start = "2026-01-01T00:00:00Z"
        end = "2026-01-01T06:00:00Z"
        history = {
            _WASHER: [
                _ha_row(_WASHER, "idle", start, start),
                _ha_row(_WASHER, "running", "2026-01-01T02:00:00Z", "2026-01-01T02:00:00Z"),
                _ha_row(_WASHER, "end", "2026-01-01T04:00:00Z", "2026-01-01T04:00:00Z"),
            ],
            _DISHWASHER: [
                _ha_row(_DISHWASHER, "clean", start, start),
                _ha_row(_DISHWASHER, "running", "2026-01-01T01:00:00Z", "2026-01-01T01:00:00Z"),
            ],
        }

        # Run 1
        data_dir_1 = tmp_path / "data1"
        fake1 = FakeHA(history=history)
        _run_extract(
            tmp_path,
            fake1.client,
            config,
            start=start,
            end=end,
            data_dir=str(data_dir_1),
            entities=[_WASHER, _DISHWASHER],
        )

        # Run 2
        data_dir_2 = tmp_path / "data2"
        fake2 = FakeHA(history=history)
        _run_extract(
            tmp_path,
            fake2.client,
            config,
            start=start,
            end=end,
            data_dir=str(data_dir_2),
            entities=[_WASHER, _DISHWASHER],
        )

        parts1 = sorted(iter_parts(data_dir_1, SOURCE))
        parts2 = sorted(iter_parts(data_dir_2, SOURCE))
        assert len(parts1) == len(parts2)

        for p1, p2 in zip(parts1, parts2):
            t1 = pq.read_table(p1)
            t2 = pq.read_table(p2)
            # Same row order
            assert t1.column("entity").to_pylist() == t2.column("entity").to_pylist()
            assert t1.column("state").to_pylist() == t2.column("state").to_pylist()
            assert t1.column("last_changed").to_pylist() == t2.column("last_changed").to_pylist()

    def test_rows_sorted_by_entity_then_last_updated(self) -> None:
        """``rows_from_response`` returns rows in (entity, last_updated, last_changed) order."""
        start = datetime(2026, 1, 1, tzinfo=UTC)
        end = datetime(2026, 1, 1, 6, tzinfo=UTC)
        response = {
            _WASHER: [
                _ha_row(_WASHER, "b", "2026-01-01T03:00:00Z", "2026-01-01T03:00:00Z"),
                _ha_row(_WASHER, "a", "2026-01-01T01:00:00Z", "2026-01-01T01:00:00Z"),
            ],
            _DISHWASHER: [
                _ha_row(_DISHWASHER, "x", "2026-01-01T02:00:00Z", "2026-01-01T02:00:00Z"),
            ],
        }
        entities = (_DISHWASHER, _WASHER)
        rows = rows_from_response(
            response,
            entities=entities,
            chunk_start=start,
            chunk_end=end,
            source_window_start=start,
            extraction_id="test-id",
        )
        # "lavadora" < "lavavajillas" alphabetically, so washer comes first
        assert rows[0]["entity"] == _WASHER
        assert rows[1]["entity"] == _WASHER
        assert rows[2]["entity"] == _DISHWASHER
        # Within washer, sorted by last_updated (a=01:00 before b=03:00)
        assert rows[0]["state"] == "a"
        assert rows[1]["state"] == "b"


# ---------------------------------------------------------------------------
# Tests: pure helpers
# ---------------------------------------------------------------------------


class TestPureHelpers:
    """Unit tests for pure helper functions."""

    def test_parse_utc_rejects_naive(self) -> None:
        with pytest.raises(ValueError, match="naive"):
            parse_utc("2026-01-01T00:00:00", name="test")

    def test_parse_utc_accepts_z(self) -> None:
        dt = parse_utc("2026-01-01T00:00:00Z", name="test")
        assert dt.tzinfo is not None
        assert dt == datetime(2026, 1, 1, tzinfo=UTC)

    def test_parse_utc_accepts_offset(self) -> None:
        dt = parse_utc("2026-01-01T01:00:00+01:00", name="test")
        assert dt == datetime(2026, 1, 1, 0, 0, 0, tzinfo=UTC)

    def test_iso_z_roundtrip(self) -> None:
        dt = datetime(2026, 1, 1, 12, 30, 45, tzinfo=UTC)
        assert iso_z(dt) == "2026-01-01T12:30:45Z"

    def test_resolve_base_url_default(self) -> None:
        assert resolve_base_url(None) == DEFAULT_BASE_URL

    def test_resolve_base_url_rejects_embedded_creds(self) -> None:
        with pytest.raises(ValueError, match="credentials"):
            resolve_base_url("https://user:pass@hass.example.com")

    def test_resolve_token_from_env(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setenv("ENERGY_BASELINE_HA_TOKEN", "env-token")
        assert resolve_token(None) == "env-token"

    def test_resolve_token_missing_raises(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.delenv("ENERGY_BASELINE_HA_TOKEN", raising=False)
        with pytest.raises(ValueError, match="not provided"):
            resolve_token(None)

    def test_resolve_entities_defaults(self) -> None:
        assert resolve_entities(None) == DEFAULT_ENTITIES
        assert resolve_entities([]) == DEFAULT_ENTITIES

    def test_resolve_entities_explicit(self) -> None:
        result = resolve_entities(["sensor.foo", "sensor.bar"])
        assert result == ("sensor.foo", "sensor.bar")

    def test_iter_chunks_exact_partition(self) -> None:
        start = datetime(2026, 1, 1, tzinfo=UTC)
        end = datetime(2026, 1, 1, 3, tzinfo=UTC)
        chunks = list(iter_chunks(start, end, 3600))
        assert len(chunks) == 3
        assert chunks[0][0] == start
        assert chunks[-1][1] == end
        # No gaps
        for i in range(len(chunks) - 1):
            assert chunks[i][1] == chunks[i + 1][0]


# ---------------------------------------------------------------------------
# Tests: retry behaviour
# ---------------------------------------------------------------------------


class TestRetry:
    """429/5xx retry; 401/403 no retry."""

    def test_500_retries_then_succeeds(self, tmp_path: Path, config: Path) -> None:
        start = "2026-01-01T00:00:00Z"
        end = "2026-01-01T06:00:00Z"
        call_count = 0

        def responder(request: httpx.Request, idx: int) -> httpx.Response:
            nonlocal call_count
            call_count += 1
            if call_count < 3:
                return httpx.Response(500, text="internal error")
            return httpx.Response(
                200,
                json=[[_ha_row(_WASHER, "idle", start, start)]],
            )

        fake = FakeHA(responder=responder)
        _run_extract(tmp_path, fake.client, config, start=start, end=end, max_retries=3)
        assert call_count == 3

    def test_429_retries(self, tmp_path: Path, config: Path) -> None:
        start = "2026-01-01T00:00:00Z"
        end = "2026-01-01T06:00:00Z"
        call_count = 0

        def responder(request: httpx.Request, idx: int) -> httpx.Response:
            nonlocal call_count
            call_count += 1
            if call_count == 1:
                return httpx.Response(429, text="rate limited")
            return httpx.Response(
                200,
                json=[[_ha_row(_WASHER, "idle", start, start)]],
            )

        fake = FakeHA(responder=responder)
        _run_extract(tmp_path, fake.client, config, start=start, end=end)
        assert call_count == 2

    def test_retries_exhausted_raises(self, tmp_path: Path, config: Path) -> None:
        start = "2026-01-01T00:00:00Z"
        end = "2026-01-01T06:00:00Z"

        def responder(request: httpx.Request, idx: int) -> httpx.Response:
            return httpx.Response(503, text="unavailable")

        fake = FakeHA(responder=responder)
        with pytest.raises(HAHTTPError, match="gave up"):
            _run_extract(tmp_path, fake.client, config, start=start, end=end, max_retries=1)


# ---------------------------------------------------------------------------
# Tests: no manifest on failure
# ---------------------------------------------------------------------------


class TestNoManifestOnFailure:
    """A failed run must not produce a success manifest."""

    def test_no_manifest_on_auth_failure(self, tmp_path: Path, config: Path) -> None:
        def responder(request: httpx.Request, idx: int) -> httpx.Response:
            return httpx.Response(401, text="unauthorized")

        fake = FakeHA(responder=responder)
        with pytest.raises(HAHTTPError):
            _run_extract(tmp_path, fake.client, config)

        manifests_dir = tmp_path / "data" / "manifests"
        if manifests_dir.exists():
            assert list(manifests_dir.glob("*.json")) == []


# ---------------------------------------------------------------------------
# Tests: cross-run deduplication (the overlapping re-extraction case)
# ---------------------------------------------------------------------------


_CHUNK_1 = ("2026-01-01T00:00:00Z", "2026-01-01T12:00:00Z")
_CHUNK_2 = ("2026-01-01T12:00:00Z", "2026-01-02T00:00:00Z")
_CHUNK_1_START = datetime(2026, 1, 1, 0, 0, tzinfo=UTC)
_CHUNK_1_END = datetime(2026, 1, 1, 12, 0, tzinfo=UTC)
_HALF_DAY = 43200
_FULL_DAY_WINDOW = ("2026-01-01T00:00:00Z", "2026-01-02T00:00:00Z")


class _ChunkFailureHA(StatefulFakeHA):
    """Stateful server that fails every request for one specific chunk start."""

    def __init__(self, timeline: dict[str, list[dict]], fail_start: str) -> None:
        super().__init__(timeline)
        self.fail_start = fail_start

    def handle(self, request: httpx.Request) -> httpx.Response:
        if request.url.path.endswith(self.fail_start):
            self.requests.append(request)
            return httpx.Response(503, text="unavailable")
        return super().handle(request)


class TestCrossRunDedup:
    """A re-extraction that overlaps what was already pulled stores nothing twice."""

    def test_first_clean_run_records_no_dedup_disclosure(
        self, tmp_path: Path, config: Path
    ) -> None:
        """An empty prior store: everything is new, so no suppression is claimed."""
        data_dir = tmp_path / "data"
        path = _run_extract(
            tmp_path,
            StatefulFakeHA(_washer_timeline()).client,
            config,
            start=_CHUNK_1[0],
            end=_CHUNK_1[1],
            data_dir=str(data_dir),
        )
        data = json.loads(path.read_text())

        assert data["row_count"] == 2
        assert data["quality_flags"] == []
        assert data["params"]["row_accounting"] == {
            "raw_rows": 2,
            "rows_seen": 2,
            "duplicate_rows_suppressed": 0,
            "new_rows": 2,
            "rows_written": 2,
            "chunks_with_duplicates": 0,
            "fully_suppressed_chunks": 0,
            "fully_suppressed_windows": [],
            "carry_in_disagreements": 0,
        }
        assert data["params"]["prior_coverage"]["parts_scanned"] == 0
        assert data["params"]["prior_coverage"]["distinct_keys"] == 0
        assert data["params"]["prior_coverage"]["coverage_established"] is True

    def test_overlapping_reextraction_writes_only_new_rows(
        self, tmp_path: Path, config: Path
    ) -> None:
        """The second run's part carries the new transitions and nothing else."""
        data_dir = tmp_path / "data"
        first = json.loads(
            _run_extract(
                tmp_path,
                StatefulFakeHA(_washer_timeline()).client,
                config,
                start=_CHUNK_1[0],
                end=_CHUNK_1[1],
                data_dir=str(data_dir),
            ).read_text()
        )

        # A wider, overlapping window: the 00:00-12:00 half is re-asked for.
        second_path = _run_extract(
            tmp_path,
            StatefulFakeHA(_washer_timeline()).client,
            config,
            start=_FULL_DAY_WINDOW[0],
            end=_FULL_DAY_WINDOW[1],
            data_dir=str(data_dir),
        )
        second = json.loads(second_path.read_text())

        accounting = second["params"]["row_accounting"]
        assert accounting["rows_seen"] == 4
        assert accounting["duplicate_rows_suppressed"] == 2
        assert accounting["new_rows"] == 2
        assert accounting["rows_written"] == 2
        assert second["row_count"] == 2

        # The part this run wrote contains exactly the two genuinely new rows.
        known = {entry["name"] for entry in first["part_files"]}
        new_parts = [p for p in iter_parts(data_dir, SOURCE) if p.name not in known]
        assert len(new_parts) == 1
        assert sorted(pq.read_table(new_parts[0]).column("state").to_pylist()) == [
            "idle",
            "running",
        ]
        assert second["part_files"][0]["rows"] == 2
        assert second["part_files"][0]["rows_seen"] == 4
        assert second["part_files"][0]["duplicate_rows_suppressed"] == 2

        # Per-chunk ledger agrees with the totals.
        outcome = second["params"]["chunk_outcomes"][0]
        assert outcome["raw_rows"] == 4
        assert outcome["kept_rows"] == 4
        assert outcome["duplicate_rows"] == 2
        assert outcome["new_rows"] == 2
        assert outcome["rows_written"] == 2
        assert outcome["fully_suppressed"] is False

        # And the store now holds each observation exactly once.
        keys = _stored_keys(data_dir)
        assert len(keys) == 4
        assert len(set(keys)) == 4

    def test_suppression_is_disclosed_and_never_an_alarm(
        self, tmp_path: Path, config: Path
    ) -> None:
        """Duplicate flags exist, but no genuine-problem flag is raised by overlap."""
        data_dir = tmp_path / "data"
        _run_extract(
            tmp_path,
            StatefulFakeHA(_washer_timeline()).client,
            config,
            start=_CHUNK_1[0],
            end=_CHUNK_1[1],
            data_dir=str(data_dir),
        )
        data = json.loads(
            _run_extract(
                tmp_path,
                StatefulFakeHA(_washer_timeline()).client,
                config,
                start=_FULL_DAY_WINDOW[0],
                end=_FULL_DAY_WINDOW[1],
                chunk_seconds=_HALF_DAY,
                data_dir=str(data_dir),
            ).read_text()
        )
        # Chunk 1 of this run is the already-stored morning: all duplicates.
        dup_flags = _flags_with_prefix(data, "duplicate_rows_suppressed:")
        assert len(dup_flags) == 2, dup_flags  # one per chunk + the total
        morning = next(f for f in dup_flags if _CHUNK_1[1] in f or "12:00" in f)
        assert "already_present=2" in morning
        assert "new=0" in morning
        assert "rows_written=0" in morning
        assert "Expected and benign" in morning
        assert "Not a coverage gap" in morning
        # The genuine-problem flags stay silent, so the two are never confused.
        assert not _flags_with_prefix(data, "empty_chunk:")
        assert not _flags_with_prefix(data, "empty_chunk_after_window_filter:")
        assert not _flags_with_prefix(data, "chunk_accounting_mismatch")
        assert not _flags_with_prefix(data, "no_rows_for_entity:")
        assert not _flags_with_prefix(data, "no_rows_extracted:all")
        assert data["params"]["chunk_accounting"]["mismatch"] is False

    def test_third_identical_rerun_writes_zero_parts_and_zero_rows(
        self, tmp_path: Path, config: Path
    ) -> None:
        """Idempotence: a fully covered window stores nothing more, and says so."""
        data_dir = tmp_path / "data"
        for _ in range(2):
            _run_extract(
                tmp_path,
                StatefulFakeHA(_washer_timeline()).client,
                config,
                start=_FULL_DAY_WINDOW[0],
                end=_FULL_DAY_WINDOW[1],
                data_dir=str(data_dir),
            )
        parts_before = sorted(p.name for p in iter_parts(data_dir, SOURCE))
        # One part: the first run wrote all four rows, the second wrote nothing.
        assert len(parts_before) == 1

        third = json.loads(
            _run_extract(
                tmp_path,
                StatefulFakeHA(_washer_timeline()).client,
                config,
                start=_FULL_DAY_WINDOW[0],
                end=_FULL_DAY_WINDOW[1],
                data_dir=str(data_dir),
            ).read_text()
        )

        assert sorted(p.name for p in iter_parts(data_dir, SOURCE)) == parts_before
        assert third["row_count"] == 0
        assert third["part_files"] == []
        accounting = third["params"]["chunk_accounting"]
        assert accounting["declared_chunks"] == 1
        assert accounting["parts_written"] == 0
        assert accounting["fully_suppressed_chunks"] == 1
        assert accounting["empty_chunks"] == 0
        # Fully suppressed chunks are not a declared/written gap.
        assert accounting["mismatch"] is False
        assert not _flags_with_prefix(third, "chunk_accounting_mismatch")
        # Nothing was *seen* to be missing, so "no rows extracted" must not fire.
        assert not _flags_with_prefix(third, "no_rows_extracted")
        assert _flags_with_prefix(third, "duplicate_rows_suppressed:")
        rows = third["params"]["row_accounting"]
        assert rows["rows_seen"] == 4
        assert rows["duplicate_rows_suppressed"] == 4
        assert rows["new_rows"] == 0
        assert rows["rows_written"] == 0
        assert len(set(_stored_keys(data_dir))) == 4

    def test_carry_in_rows_are_suppressed_on_overlap_and_flags_agree(
        self, tmp_path: Path, config: Path
    ) -> None:
        """Synthetic carry-in anchors to the chunk start, so repeats collapse too."""
        data_dir = tmp_path / "data"
        first = json.loads(
            _run_extract(
                tmp_path,
                StatefulFakeHA(_washer_timeline()).client,
                config,
                start=_FULL_DAY_WINDOW[0],
                end=_FULL_DAY_WINDOW[1],
                chunk_seconds=_HALF_DAY,
                data_dir=str(data_dir),
            ).read_text()
        )
        stored = _stored_rows(data_dir)
        carry_rows = [r for r in stored if r["carry_in"]]
        assert len(carry_rows) == 1
        assert carry_rows[0]["state"] == "end"
        assert carry_rows[0]["last_updated"] == datetime(2026, 1, 1, 12, tzinfo=UTC)

        repeat = json.loads(
            _run_extract(
                tmp_path,
                StatefulFakeHA(_washer_timeline()).client,
                config,
                start=_FULL_DAY_WINDOW[0],
                end=_FULL_DAY_WINDOW[1],
                chunk_seconds=_HALF_DAY,
                data_dir=str(data_dir),
            ).read_text()
        )

        assert repeat["row_count"] == 0
        assert repeat["part_files"] == []
        outcomes = repeat["params"]["chunk_outcomes"]
        assert [o["duplicate_rows"] for o in outcomes] == [2, 3]
        assert [o["new_rows"] for o in outcomes] == [0, 0]
        # Carry-in classification never disagreed between the two runs.
        assert all(o["carry_in_disagreements"] == 0 for o in outcomes)
        assert not _flags_with_prefix(repeat, "carry_in_classification_disagreement")
        assert first["row_count"] == 5
        # The stored carry-in is still there, still flagged, and stored once only.
        assert len(_stored_keys(data_dir)) == 5
        assert [r["carry_in"] for r in _stored_rows(data_dir)].count(True) == 1

    def test_carry_in_disagreement_with_stored_row_is_disclosed(
        self, tmp_path: Path, config: Path
    ) -> None:
        """Mis-aligned chunks are reported, not silently kept under the old copy."""
        timeline = {
            _WASHER: [
                _ha_row(_WASHER, "running", "2026-01-01T02:00:00Z", "2026-01-01T02:00:00Z"),
                _ha_row(_WASHER, "end", "2026-01-01T05:00:00Z", "2026-01-01T05:00:00Z"),
                # A real transition sitting exactly on the second chunk's start.
                _ha_row(_WASHER, "drying", "2026-01-01T12:00:00Z", "2026-01-01T12:00:00Z"),
                _ha_row(_WASHER, "running", "2026-01-01T13:00:00Z", "2026-01-01T13:00:00Z"),
                _ha_row(_WASHER, "idle", "2026-01-01T20:00:00Z", "2026-01-01T20:00:00Z"),
            ]
        }
        data_dir = tmp_path / "data"
        _run_extract(
            tmp_path,
            StatefulFakeHA(timeline).client,
            config,
            start=_FULL_DAY_WINDOW[0],
            end=_FULL_DAY_WINDOW[1],
            chunk_seconds=_HALF_DAY,
            data_dir=str(data_dir),
        )
        stored = _stored_rows(data_dir)
        drying = [r for r in stored if r["state"] == "drying"]
        assert len(drying) == 1
        # Chunk-aligned run: this row arrived as the first row of the 12:00 chunk.
        assert drying[0]["carry_in"] is True

        # A differently aligned run sees the same row as a real mid-chunk event.
        misaligned = json.loads(
            _run_extract(
                tmp_path,
                StatefulFakeHA(timeline).client,
                config,
                start="2026-01-01T11:00:00Z",
                end="2026-01-01T23:00:00Z",
                data_dir=str(data_dir),
            ).read_text()
        )

        assert misaligned["params"]["row_accounting"]["carry_in_disagreements"] == 1
        conflict = _flags_with_prefix(misaligned, "carry_in_classification_disagreement:")
        assert len(conflict) == 1
        assert conflict[0].startswith(
            "carry_in_classification_disagreement:[2026-01-01T11:00:00Z, 2026-01-01T23:00:00Z):"
        )
        assert "chunk boundaries did not line up" in conflict[0]
        # Immutability holds: the stored copy is untouched, and no second copy was added.
        drying = [r for r in _stored_rows(data_dir) if r["state"] == "drying"]
        assert len(drying) == 1
        assert drying[0]["carry_in"] is True

    def test_different_state_at_same_timestamp_is_not_suppressed(
        self, tmp_path: Path, config: Path
    ) -> None:
        """Same entity and timestamps but a different state is a new observation."""
        data_dir = tmp_path / "data"
        morning = _ha_row(_WASHER, "running", "2026-01-01T02:00:00Z", "2026-01-01T02:00:00Z")
        _run_extract(
            tmp_path,
            StatefulFakeHA({_WASHER: [morning]}).client,
            config,
            start=_CHUNK_1[0],
            end=_CHUNK_1[1],
            data_dir=str(data_dir),
        )

        corrected = _ha_row(_WASHER, "idle", "2026-01-01T02:00:00Z", "2026-01-01T02:00:00Z")
        data = json.loads(
            _run_extract(
                tmp_path,
                StatefulFakeHA({_WASHER: [corrected]}).client,
                config,
                start=_CHUNK_1[0],
                end=_CHUNK_1[1],
                data_dir=str(data_dir),
            ).read_text()
        )

        assert data["row_count"] == 1
        assert data["params"]["row_accounting"]["duplicate_rows_suppressed"] == 0
        assert not _flags_with_prefix(data, "duplicate_rows_suppressed:")
        states = sorted(r["state"] for r in _stored_rows(data_dir))
        assert states == ["idle", "running"]

    def test_different_attributes_at_same_timestamp_is_not_suppressed(
        self, tmp_path: Path, config: Path
    ) -> None:
        """attributes_json is part of the identity: a changed blob is a new row."""
        data_dir = tmp_path / "data"
        row = _ha_row(
            _WASHER,
            "running",
            "2026-01-01T02:00:00Z",
            "2026-01-01T02:00:00Z",
            attributes={"phase": 1},
        )
        _run_extract(
            tmp_path,
            StatefulFakeHA({_WASHER: [row]}).client,
            config,
            start=_CHUNK_1[0],
            end=_CHUNK_1[1],
            data_dir=str(data_dir),
        )
        changed = _ha_row(
            _WASHER,
            "running",
            "2026-01-01T02:00:00Z",
            "2026-01-01T02:00:00Z",
            attributes={"phase": 2},
        )
        data = json.loads(
            _run_extract(
                tmp_path,
                StatefulFakeHA({_WASHER: [changed]}).client,
                config,
                start=_CHUNK_1[0],
                end=_CHUNK_1[1],
                data_dir=str(data_dir),
            ).read_text()
        )

        assert data["row_count"] == 1
        assert len(set(_stored_keys(data_dir))) == 2

    def test_different_last_changed_at_same_timestamp_is_not_suppressed(
        self, tmp_path: Path, config: Path
    ) -> None:
        """last_changed is in the identity, so it cannot over-dedup either."""
        base = _assembled_row("running", at="2026-01-01T02:00:00Z", extraction_id="run-1")
        other = dict(base, extraction_id="run-2")
        other["last_changed"] = parse_utc("2026-01-01T01:00:00Z", name="lc")
        assert identity_digest(row_identity(base)) != identity_digest(row_identity(other))

    def test_partially_failed_prior_run_neither_skips_rows_nor_rewrites_parts(
        self, tmp_path: Path, config: Path
    ) -> None:
        """Coverage comes from parts on disk: orphans dedup, but never narrow the run."""
        data_dir = tmp_path / "data"
        manifests_dir = data_dir / "manifests"
        with pytest.raises(HAHTTPError):
            _run_extract(
                tmp_path,
                _ChunkFailureHA(_washer_timeline(), _CHUNK_2[0]).client,
                config,
                start=_FULL_DAY_WINDOW[0],
                end=_FULL_DAY_WINDOW[1],
                chunk_seconds=_HALF_DAY,
                max_retries=0,
                data_dir=str(data_dir),
            )

        # The failed run left one orphan part and no manifest at all.
        assert not manifests_dir.exists() or list(manifests_dir.glob("*.json")) == []
        orphan_parts = sorted(p.name for p in iter_parts(data_dir, SOURCE))
        assert len(orphan_parts) == 1

        data = json.loads(
            _run_extract(
                tmp_path,
                StatefulFakeHA(_washer_timeline()).client,
                config,
                start=_FULL_DAY_WINDOW[0],
                end=_FULL_DAY_WINDOW[1],
                chunk_seconds=_HALF_DAY,
                data_dir=str(data_dir),
            ).read_text()
        )

        coverage = data["params"]["prior_coverage"]
        assert coverage["parts_scanned"] == 1
        assert coverage["manifests_read"] == 0
        assert coverage["orphan_parts"] == 1
        assert coverage["orphan_part_names"] == orphan_parts
        assert coverage["distinct_keys"] == 2
        # Chunk 1 deduped against the orphan; chunk 2 was never covered, so the run
        # still fetched it and wrote every one of its rows.
        outcomes = data["params"]["chunk_outcomes"]
        assert [o["duplicate_rows"] for o in outcomes] == [2, 0]
        assert [o["new_rows"] for o in outcomes] == [0, 3]
        assert data["row_count"] == 3
        assert data["params"]["chunk_accounting"]["mismatch"] is False
        parts = sorted(p.name for p in iter_parts(data_dir, SOURCE))
        assert len(parts) == 2, "the orphan part must not be rewritten or duplicated"
        assert len(set(_stored_keys(data_dir))) == 5

    def test_preexisting_duplicate_rows_are_disclosed_not_repaired(
        self, tmp_path: Path, config: Path
    ) -> None:
        """A store already holding duplicates is reported, never touched."""
        data_dir = tmp_path / "data"
        rows = [
            _assembled_row("running", at="2026-01-01T02:00:00Z", extraction_id="run-one"),
            _assembled_row("end", at="2026-01-01T05:00:00Z", extraction_id="run-one"),
        ]
        overlapping = [dict(row, extraction_id="run-two") for row in rows]
        _write_synthetic_part(data_dir, rows, chunk_start=_CHUNK_1_START, chunk_end=_CHUNK_1_END)
        _write_synthetic_part(
            data_dir, overlapping, chunk_start=_CHUNK_1_START, chunk_end=_CHUNK_1_END
        )
        assert len(list(iter_parts(data_dir, SOURCE))) == 2

        data = json.loads(
            _run_extract(
                tmp_path,
                StatefulFakeHA(_washer_timeline()).client,
                config,
                start=_CHUNK_1[0],
                end=_CHUNK_1[1],
                data_dir=str(data_dir),
            ).read_text()
        )

        disclosure = _flags_with_prefix(data, "duplicate_rows_already_stored:")
        assert len(disclosure) == 1
        assert ":2:" in disclosure[0]
        assert "never rewritten, deleted or appended to" in disclosure[0]
        assert data["params"]["prior_coverage"]["duplicate_rows_already_stored"] == 2
        assert data["params"]["prior_coverage"]["rows_read"] == 4
        assert data["params"]["prior_coverage"]["distinct_keys"] == 2
        assert data["row_count"] == 0
        assert len(list(iter_parts(data_dir, SOURCE))) == 2

    def test_unreadable_prior_part_is_disclosed_and_does_not_block_the_run(
        self, tmp_path: Path, config: Path
    ) -> None:
        """A damaged part cannot contribute keys: dedup degrades to duplication."""
        from energy_baseline.paths import parts_dir

        data_dir = tmp_path / "data"
        pdir = parts_dir(data_dir, SOURCE)
        pdir.mkdir(parents=True, mode=0o700)
        junk = pdir / "ha-enum-20251231T000000Z-20260101T000000Z-deadbeef.parquet"
        junk.write_bytes(b"not a parquet file")

        data = json.loads(
            _run_extract(
                tmp_path,
                StatefulFakeHA(_washer_timeline()).client,
                config,
                start=_CHUNK_1[0],
                end=_CHUNK_1[1],
                data_dir=str(data_dir),
            ).read_text()
        )

        unreadable = _flags_with_prefix(data, "prior_part_unreadable:")
        assert len(unreadable) == 1
        assert junk.name in unreadable[0]
        assert "/" not in unreadable[0], "part name only, never the absolute path"
        assert data["params"]["prior_coverage"]["coverage_established"] is False
        assert data["row_count"] == 2  # nothing was suppressed by guesswork

    def test_malformed_prior_manifest_is_ignored_not_fatal(
        self, tmp_path: Path, config: Path
    ) -> None:
        """Provenance scanning cannot break extraction of not-re-extractable data."""
        data_dir = tmp_path / "data"
        manifests = data_dir / "manifests"
        manifests.mkdir(parents=True, mode=0o700)
        (manifests / "broken.json").write_text("{not json")
        (manifests / "other-source.json").write_text(
            json.dumps({"source": "prometheus", "part_files": [{"name": "x.parquet"}]})
        )

        data = json.loads(
            _run_extract(
                tmp_path,
                StatefulFakeHA(_washer_timeline()).client,
                config,
                start=_CHUNK_1[0],
                end=_CHUNK_1[1],
                data_dir=str(data_dir),
            ).read_text()
        )

        assert data["row_count"] == 2
        assert data["params"]["prior_coverage"]["manifests_read"] == 0
        assert data["params"]["prior_coverage"]["orphan_parts"] == 0

    def test_empty_response_still_flags_no_rows_extracted(
        self, tmp_path: Path, config: Path
    ) -> None:
        """`no_rows_extracted` is about rows *seen*, so a productive overlap differs."""
        data_dir = tmp_path / "data"
        data = json.loads(
            _run_extract(
                tmp_path,
                StatefulFakeHA({_WASHER: []}).client,
                config,
                start=_CHUNK_1[0],
                end=_CHUNK_1[1],
                data_dir=str(data_dir),
            ).read_text()
        )
        assert _flags_with_prefix(data, "no_rows_extracted:all")
        assert data["params"]["row_accounting"]["rows_seen"] == 0


# ---------------------------------------------------------------------------
# Tests: row identity primitives
# ---------------------------------------------------------------------------


class TestRowIdentity:
    """The dedup key excludes run provenance and nothing that matters."""

    def test_identity_fields_are_the_observation_columns(self) -> None:
        assert ROW_IDENTITY_FIELDS == (
            "entity",
            "last_changed",
            "last_updated",
            "state",
            "attributes_json",
        )
        stored = {f.name for f in RAW_ENUM_STATE}
        assert set(ROW_IDENTITY_FIELDS) <= stored
        for provenance in ("extraction_id", "carry_in", "source_window_start"):
            assert provenance not in ROW_IDENTITY_FIELDS
            assert provenance in stored

    def test_provenance_only_differences_share_one_identity(self) -> None:
        """Byte-different rows from two runs are one logical observation."""
        run_one = _assembled_row("running", at="2026-01-01T02:00:00Z", extraction_id="750bce0e")
        run_two = dict(run_one, extraction_id="1ec3085f")
        assert identity_digest(row_identity(run_one)) == identity_digest(row_identity(run_two))

        run_two["carry_in"] = True
        assert identity_digest(row_identity(run_one)) == identity_digest(row_identity(run_two))
        run_two["source_window_start"] = parse_utc("2026-01-01T12:00:00Z", name="sws")
        assert identity_digest(row_identity(run_one)) == identity_digest(row_identity(run_two))

    def test_to_epoch_us_is_exact_and_discriminating(self) -> None:
        base = to_epoch_us(parse_utc("2026-01-01T02:00:00Z", name="base"))
        assert base == 1_767_232_800_000_000
        assert to_epoch_us(parse_utc("2026-01-01T02:00:00.000001Z", name="a")) == base + 1
        assert to_epoch_us(parse_utc("2026-01-01T02:00:00.000002Z", name="b")) == base + 2
        # Offset arithmetic is exact, so one microsecond never collides into zero.
        assert to_epoch_us(parse_utc("2026-01-01T03:00:00+01:00", name="c")) == base
        assert to_epoch_us(parse_utc("2026-01-01T02:01:00Z", name="d")) == base + 60_000_000

    def test_to_epoch_us_rejects_naive(self) -> None:
        with pytest.raises(ValueError, match="naive"):
            to_epoch_us(datetime(2026, 1, 1, 2, 0, 0))

    def test_null_attributes_differ_from_empty_json(self) -> None:
        plain = _assembled_row("idle", at="2026-01-01T02:00:00Z", extraction_id="a")
        with_attrs = dict(plain, extraction_id="b", attributes_json="{}")
        assert plain["attributes_json"] is None
        assert identity_digest(row_identity(plain)) != identity_digest(row_identity(with_attrs))

    def test_index_split_suppresses_repeat_and_counts_it(self) -> None:
        row = _assembled_row("running", at="2026-01-01T02:00:00Z", extraction_id="run-one")
        index = StoredRowIndex(keys={})
        first = index.split([row])
        assert len(first.new_rows) == 1 and first.duplicate_rows == 0
        again = index.split([dict(row, extraction_id="run-two", carry_in=True)])
        assert again.new_rows == []
        assert again.duplicate_rows == 1
        assert again.carry_in_disagreements == 1
        assert index.rows_added_this_run == 1

    def test_load_on_empty_store_is_empty_not_fatal(self, tmp_path: Path) -> None:
        index = load_stored_row_index(tmp_path / "nothing-here")
        assert index.keys == {}
        assert index.parts_scanned == 0
        assert index.preexisting_duplicate_rows == 0
        assert index.prior_coverage_established is True


# ---------------------------------------------------------------------------
# Tests: chunk ledger shape stays honest under dedup
# ---------------------------------------------------------------------------


class TestDedupLedgerShape:
    """The new counters are always present, so an overlap cannot look like a gap."""

    def test_chunk_outcome_records_all_four_row_counts(self) -> None:
        outcome = ChunkOutcome(
            index=1,
            chunk_start=parse_utc(_CHUNK_1[0], name="start"),
            chunk_end=parse_utc(_CHUNK_1[1], name="end"),
            requested=True,
            request_succeeded=True,
            raw_rows=6,
            kept_rows=4,
            duplicate_rows=3,
            new_rows=1,
        )
        entry = outcome.as_dict()
        assert entry["raw_rows"] == 6
        assert entry["kept_rows"] == 4
        assert entry["duplicate_rows"] == 3
        assert entry["new_rows"] == 1
        assert entry["rows_written"] == 1
        assert entry["fully_suppressed"] is False
        assert entry["empty"] is False
        assert entry["flags"]
        assert "seen=4 already_present=3 new=1 rows_written=1" in entry["flags"][0]

    def test_fully_suppressed_chunk_is_not_empty_and_not_a_mismatch(self) -> None:
        outcome = ChunkOutcome(
            index=1,
            chunk_start=parse_utc(_CHUNK_1[0], name="start"),
            chunk_end=parse_utc(_CHUNK_1[1], name="end"),
            requested=True,
            request_succeeded=True,
            raw_rows=2,
            kept_rows=2,
            duplicate_rows=2,
            new_rows=0,
        )
        assert outcome.empty is False
        assert outcome.payload_empty is False
        assert outcome.fully_suppressed is True
        accounting = summarize_chunks([outcome], [])
        assert accounting["declared_chunks"] == 1
        assert accounting["parts_written"] == 0
        assert accounting["fully_suppressed_chunks"] == 1
        assert accounting["mismatch"] is False
        assert accounting["rows_written"] == 0

    def test_zero_row_chunk_is_still_a_mismatch(self) -> None:
        outcome = ChunkOutcome(
            index=1,
            chunk_start=parse_utc(_CHUNK_1[0], name="start"),
            chunk_end=parse_utc(_CHUNK_1[1], name="end"),
            requested=True,
            request_succeeded=True,
            raw_rows=0,
            kept_rows=0,
        )
        accounting = summarize_chunks([outcome], [])
        assert accounting["empty_chunks"] == 1
        assert accounting["fully_suppressed_chunks"] == 0
        assert accounting["mismatch"] is True


# ---------------------------------------------------------------------------
# Tests: dedup provenance carries no identity
# ---------------------------------------------------------------------------


class TestNoIdentityLeakageInDedupFields:
    """New flags and counters name counts and part names, never endpoint or token."""

    _FAKE_URL = "https://ha-dedup-canary.example.invalid"
    _FAKE_HOST = "ha-dedup-canary.example.invalid"

    def _assert_clean(self, payload: str) -> None:
        assert _SENTINEL_TOKEN not in payload
        assert "Bearer" not in payload
        assert self._FAKE_URL not in payload
        assert self._FAKE_HOST not in payload
        assert "http://" not in payload
        assert "https://" not in payload

    def test_every_dedup_field_is_identity_free(self, tmp_path: Path, config: Path) -> None:
        data_dir = tmp_path / "data"
        for _ in range(2):
            path = _run_extract(
                tmp_path,
                StatefulFakeHA(_washer_timeline()).client,
                config,
                start=_FULL_DAY_WINDOW[0],
                end=_FULL_DAY_WINDOW[1],
                chunk_seconds=_HALF_DAY,
                data_dir=str(data_dir),
                base_url=self._FAKE_URL,
            )
        data = json.loads(path.read_text())

        for flag in data["quality_flags"]:
            self._assert_clean(flag)
        self._assert_clean(data["coverage_notes"])
        for key in ("row_accounting", "prior_coverage", "row_identity", "chunk_accounting"):
            self._assert_clean(json.dumps(data["params"][key]))
        for outcome in data["params"]["chunk_outcomes"]:
            self._assert_clean(json.dumps(outcome))
        for entry in data["part_files"]:
            self._assert_clean(json.dumps(entry))
        # Part names appear in orphan reporting; they carry kind, window and hash only.
        for name in data["params"]["prior_coverage"]["orphan_part_names"]:
            assert "/" not in name
            self._assert_clean(name)
