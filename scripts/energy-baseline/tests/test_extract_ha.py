"""Offline tests for ``extract_ha`` — ``httpx.MockTransport`` only, never the live server.

The fake HA server below emulates the history API semantics the extractor
depends on, including the synthetic carry-in timestamp behaviour.
"""

from __future__ import annotations

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
    SOURCE,
    HAHTTPError,
    HAResponseError,
    extract_ha_history,
    iso_z,
    iter_chunks,
    parse_utc,
    resolve_base_url,
    resolve_entities,
    resolve_token,
    rows_from_response,
)
from energy_baseline.manifest import read_manifest
from energy_baseline.paths import iter_parts
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
