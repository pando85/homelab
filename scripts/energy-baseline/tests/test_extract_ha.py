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
    """Carry-in row is marked carry_in=True; synthetic timestamp not a real transition."""

    def test_carry_in_row_detected(self, tmp_path: Path, config: Path) -> None:
        """HA returns a carry-in row with last_changed == chunk_start."""
        start = "2026-01-01T00:00:00Z"
        end = "2026-01-01T06:00:00Z"
        # Carry-in: last_changed set to window start by HA
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
        # This is NOT a carry-in because last_changed == start (it's the boundary)
        # Actually: last_changed_us <= chunk_start_us → carry_in. The carry-in
        # detection is <=, so a row at exactly start is a carry-in.
        # This is correct: HA returns it as the carry-in.
        assert table.column("carry_in").to_pylist()[0] is True

    def test_event_at_end_excluded(self, tmp_path: Path, config: Path) -> None:
        start = "2026-01-01T00:00:00Z"
        end = "2026-01-01T06:00:00Z"
        history = {
            _WASHER: [
                _ha_row(_WASHER, "idle", start, start),
                _ha_row(_WASHER, "running", end, end),  # exactly at end → excluded
            ]
        }
        fake = FakeHA(history=history)
        _run_extract(tmp_path, fake.client, config, start=start, end=end)

        table = _read_parts(tmp_path / "data")
        states = table.column("state").to_pylist()
        assert "running" not in states
        assert "idle" in states

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

    def test_source_window_start_set(self, tmp_path: Path, config: Path) -> None:
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
        sws = table.column("source_window_start").to_pylist()
        assert sws[0] == datetime(2026, 1, 1, 0, 0, 0, tzinfo=UTC)


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

    def test_rows_sorted_by_entity_then_timestamps(self) -> None:
        """rows_from_response returns rows in deterministic order."""
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
        # Within washer, sorted by last_changed
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
