"""Offline tests for the HA history reader (``ha_states``).

All fixtures are synthetic — no real household data appears in committed files.
The reader is read-only: tests verify it never writes anything.
"""

from __future__ import annotations

import json
from datetime import UTC, datetime, timedelta
from pathlib import Path

import pyarrow as pa
import pyarrow.parquet as pq
import pytest

from energy_baseline.ha_states import (
    UNAVAILABLE_STATES,
    HAState,
    HAStore,
    format_as_of_result,
    format_summary,
    load_store,
)
from energy_baseline.paths import parts_dir
from energy_baseline.schemas import RAW_ENUM_STATE

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

_T0 = datetime(2026, 10, 15, 0, 0, 0, tzinfo=UTC)
_WASHER = "sensor.lavadora_current_status"
_DISHWASHER = "sensor.lavavajillas_current_status"


def _make_state(
    entity: str,
    state: str,
    last_changed: datetime,
    last_updated: datetime,
    *,
    attributes_json: str | None = None,
    carry_in: bool = False,
    extraction_id: str = "ext-1",
    source_window_start: datetime | None = None,
    part_name: str = "part.parquet",
) -> HAState:
    """Build an HAState for testing."""
    attrs = None
    if attributes_json is not None:
        try:
            parsed = json.loads(attributes_json)
            if isinstance(parsed, dict):
                attrs = parsed
        except (json.JSONDecodeError, TypeError, ValueError):
            pass
    return HAState(
        entity=entity,
        state=state,
        last_changed=last_changed,
        last_updated=last_updated,
        attributes=attrs,
        attributes_json=attributes_json,
        carry_in=carry_in,
        extraction_id=extraction_id,
        source_window_start=source_window_start or _T0,
        part_name=part_name,
    )


def _write_part(
    data_dir: Path,
    rows: list[dict],
    *,
    part_name: str = "ha-enum-20261015T000000Z-20261016T000000Z-aaaaaaaa.parquet",
) -> Path:
    """Write a synthetic part file with the given rows."""
    pdir = parts_dir(data_dir, "ha_history")
    pdir.mkdir(parents=True, exist_ok=True)
    path = pdir / part_name

    if not rows:
        table = pa.table(
            {
                "entity": pa.array([], pa.string()),
                "state": pa.array([], pa.string()),
                "attributes_json": pa.array([], pa.string()),
                "last_changed": pa.array([], pa.timestamp("us", tz="UTC")),
                "last_updated": pa.array([], pa.timestamp("us", tz="UTC")),
                "extraction_id": pa.array([], pa.string()),
                "carry_in": pa.array([], pa.bool_()),
                "source_window_start": pa.array([], pa.timestamp("us", tz="UTC")),
            },
            schema=RAW_ENUM_STATE,
        )
    else:
        table = pa.table(
            {
                "entity": pa.array([r["entity"] for r in rows], pa.string()),
                "state": pa.array([r["state"] for r in rows], pa.string()),
                "attributes_json": pa.array([r.get("attributes_json") for r in rows], pa.string()),
                "last_changed": pa.array(
                    [r["last_changed"] for r in rows], pa.timestamp("us", tz="UTC")
                ),
                "last_updated": pa.array(
                    [r["last_updated"] for r in rows], pa.timestamp("us", tz="UTC")
                ),
                "extraction_id": pa.array(
                    [r.get("extraction_id", "ext-1") for r in rows], pa.string()
                ),
                "carry_in": pa.array([r.get("carry_in", False) for r in rows], pa.bool_()),
                "source_window_start": pa.array(
                    [r.get("source_window_start", _T0) for r in rows],
                    pa.timestamp("us", tz="UTC"),
                ),
            },
            schema=RAW_ENUM_STATE,
        )
    pq.write_table(table, path)
    return path


def _row(
    entity: str,
    state: str,
    last_changed: datetime,
    last_updated: datetime,
    **kwargs,
) -> dict:
    """Build a raw row dict for _write_part."""
    return {
        "entity": entity,
        "state": state,
        "last_changed": last_changed,
        "last_updated": last_updated,
        **kwargs,
    }


# ---------------------------------------------------------------------------
# Tests: dedup
# ---------------------------------------------------------------------------


class TestDedup:
    """Dedup collapses the documented duplicate pattern and is idempotent."""

    def test_dedup_collapses_duplicate_rows(self, tmp_path: Path) -> None:
        """Two parts with identical logical rows collapse to one copy each."""
        attrs = '{"device_class": "enum", "friendly_name": "Test"}'
        t1 = _T0 + timedelta(hours=12)
        t2 = _T0 + timedelta(hours=13)

        rows_a = [
            _row(_WASHER, "running", t1, t1, attributes_json=attrs),
            _row(_WASHER, "completed", t2, t2, attributes_json=attrs),
        ]
        rows_b = [
            # Same logical rows, different extraction_id (the duplicate pattern)
            _row(
                _WASHER,
                "running",
                t1,
                t1,
                attributes_json=attrs,
                extraction_id="ext-2",
            ),
            _row(
                _WASHER,
                "completed",
                t2,
                t2,
                attributes_json=attrs,
                extraction_id="ext-2",
            ),
        ]

        _write_part(tmp_path, rows_a, part_name="ha-enum-part-a-aaaaaaaa.parquet")
        _write_part(tmp_path, rows_b, part_name="ha-enum-part-b-bbbbbbbb.parquet")

        store = load_store(tmp_path)
        assert store.total_rows_before_dedup == 4
        assert store.total_rows_after_dedup == 2
        assert store.duplicate_rows == 2

    def test_dedup_is_idempotent(self, tmp_path: Path) -> None:
        """Loading twice yields the same result."""
        attrs = '{"device_class": "enum"}'
        t1 = _T0 + timedelta(hours=12)
        rows = [_row(_WASHER, "running", t1, t1, attributes_json=attrs)]
        _write_part(tmp_path, rows)

        store1 = load_store(tmp_path)
        store2 = load_store(tmp_path)
        assert store1.total_rows_after_dedup == store2.total_rows_after_dedup
        assert len(store1.states) == len(store2.states)

    def test_different_extraction_id_same_observation_is_duplicate(self, tmp_path: Path) -> None:
        """The documented pattern: same (entity, lc, lu, state, attrs) but
        different extraction_id → one copy after dedup."""
        t1 = _T0 + timedelta(hours=12)
        attrs = '{"friendly_name": "Washer"}'

        rows_a = [_row(_WASHER, "idle", t1, t1, attributes_json=attrs, extraction_id="aaa")]
        rows_b = [_row(_WASHER, "idle", t1, t1, attributes_json=attrs, extraction_id="bbb")]

        _write_part(tmp_path, rows_a, part_name="ha-enum-aaaa-aaaaaaaa.parquet")
        _write_part(tmp_path, rows_b, part_name="ha-enum-bbbb-bbbbbbbb.parquet")

        store = load_store(tmp_path)
        assert store.total_rows_before_dedup == 2
        assert store.total_rows_after_dedup == 1

    def test_different_carry_in_same_observation_is_still_duplicate(self, tmp_path: Path) -> None:
        """carry_in is excluded from identity — disagreement still dedups."""
        t1 = _T0 + timedelta(hours=12)
        attrs = '{"friendly_name": "Washer"}'

        rows_a = [
            _row(
                _WASHER,
                "idle",
                t1,
                t1,
                attributes_json=attrs,
                carry_in=False,
                extraction_id="aaa",
            )
        ]
        rows_b = [
            _row(
                _WASHER,
                "idle",
                t1,
                t1,
                attributes_json=attrs,
                carry_in=True,
                extraction_id="bbb",
            )
        ]

        _write_part(tmp_path, rows_a, part_name="ha-enum-aaaa-aaaaaaaa.parquet")
        _write_part(tmp_path, rows_b, part_name="ha-enum-bbbb-bbbbbbbb.parquet")

        store = load_store(tmp_path)
        assert store.total_rows_after_dedup == 1


# ---------------------------------------------------------------------------
# Tests: as-of
# ---------------------------------------------------------------------------


class TestAsOf:
    """As-of query correctness."""

    def _make_store_with_transitions(self) -> HAStore:
        """Build a store with known transitions for as-of testing."""
        t1 = datetime(2026, 10, 15, 10, 0, 0, tzinfo=UTC)
        t2 = datetime(2026, 10, 15, 12, 0, 0, tzinfo=UTC)
        t3 = datetime(2026, 10, 15, 14, 0, 0, tzinfo=UTC)

        states = [
            _make_state(_WASHER, "idle", t1, t1),
            _make_state(_WASHER, "running", t2, t2),
            _make_state(_WASHER, "completed", t3, t3),
        ]
        return HAStore(
            states=states,
            total_rows_before_dedup=3,
            total_rows_after_dedup=3,
            parts_read=1,
            part_names=["part.parquet"],
            entities=[_WASHER],
        )

    def test_as_of_between_transitions_returns_earlier(self) -> None:
        """An instant strictly between two transitions returns the earlier one."""
        store = self._make_store_with_transitions()
        t_between = datetime(2026, 10, 15, 11, 0, 0, tzinfo=UTC)
        result = store.as_of(_WASHER, t_between)
        assert result is not None
        assert result.state == "idle"

    def test_as_of_exactly_at_transition_boundary_is_inclusive(self) -> None:
        """At exactly a transition instant, the new state IS visible.

        Boundary convention: INCLUSIVE.  last_changed is when the state
        became effective in HA's model, so at that exact instant the new
        state is the correct answer.  This makes it structurally impossible
        to leak a later transition into an earlier snapshot — only rows
        with last_changed <= T are returned.
        """
        store = self._make_store_with_transitions()
        t_exact = datetime(2026, 10, 15, 12, 0, 0, tzinfo=UTC)
        result = store.as_of(_WASHER, t_exact)
        assert result is not None
        assert result.state == "running"

    def test_as_of_before_any_state_returns_none(self) -> None:
        """Before any state exists, as_of returns None."""
        store = self._make_store_with_transitions()
        t_before = datetime(2026, 10, 15, 9, 0, 0, tzinfo=UTC)
        result = store.as_of(_WASHER, t_before)
        assert result is None

    def test_as_of_after_all_states_returns_latest(self) -> None:
        """After all states, as_of returns the latest one."""
        store = self._make_store_with_transitions()
        t_after = datetime(2026, 10, 15, 20, 0, 0, tzinfo=UTC)
        result = store.as_of(_WASHER, t_after)
        assert result is not None
        assert result.state == "completed"

    def test_as_of_unknown_entity_returns_none(self) -> None:
        """An entity not in the store returns None."""
        store = self._make_store_with_transitions()
        t = datetime(2026, 10, 15, 11, 0, 0, tzinfo=UTC)
        result = store.as_of("sensor.nonexistent", t)
        assert result is None

    def test_as_of_rejects_naive_datetime(self) -> None:
        """A naive datetime raises ValueError."""
        store = self._make_store_with_transitions()
        with pytest.raises(ValueError, match="aware datetime"):
            store.as_of(_WASHER, datetime(2026, 10, 15, 11, 0, 0))


# ---------------------------------------------------------------------------
# Tests: out-of-order rows
# ---------------------------------------------------------------------------


class TestOutOfOrder:
    """Part files may carry rows in any order; the reader sorts them."""

    def test_out_of_order_rows_are_sorted(self, tmp_path: Path) -> None:
        """Rows written in reverse time order come back sorted."""
        t1 = datetime(2026, 10, 15, 14, 0, 0, tzinfo=UTC)
        t2 = datetime(2026, 10, 15, 12, 0, 0, tzinfo=UTC)
        t3 = datetime(2026, 10, 15, 10, 0, 0, tzinfo=UTC)

        # Written in reverse order
        rows = [
            _row(_WASHER, "completed", t1, t1),
            _row(_WASHER, "running", t2, t2),
            _row(_WASHER, "idle", t3, t3),
        ]
        _write_part(tmp_path, rows)

        store = load_store(tmp_path)
        assert len(store.states) == 3
        assert store.states[0].state == "idle"
        assert store.states[1].state == "running"
        assert store.states[2].state == "completed"


# ---------------------------------------------------------------------------
# Tests: unavailable/unknown states preserved
# ---------------------------------------------------------------------------


class TestUnavailableStates:
    """unavailable/unknown states are preserved, not dropped."""

    def test_unavailable_states_preserved(self, tmp_path: Path) -> None:
        t1 = _T0 + timedelta(hours=10)
        t2 = _T0 + timedelta(hours=11)
        t3 = _T0 + timedelta(hours=12)

        rows = [
            _row(_WASHER, "running", t1, t1),
            _row(_WASHER, "unavailable", t2, t2),
            _row(_WASHER, "idle", t3, t3),
        ]
        _write_part(tmp_path, rows)

        store = load_store(tmp_path)
        assert store.total_rows_after_dedup == 3
        states = store.states_for_entity(_WASHER)
        state_values = [s.state for s in states]
        assert "unavailable" in state_values

    def test_unknown_state_preserved(self, tmp_path: Path) -> None:
        t1 = _T0 + timedelta(hours=10)
        rows = [_row(_WASHER, "unknown", t1, t1)]
        _write_part(tmp_path, rows)

        store = load_store(tmp_path)
        assert store.total_rows_after_dedup == 1
        assert store.states[0].state == "unknown"
        assert store.states[0].is_unavailable

    def test_is_unavailable_property(self) -> None:
        t1 = _T0 + timedelta(hours=10)
        for state_str in UNAVAILABLE_STATES:
            s = _make_state(_WASHER, state_str, t1, t1)
            assert s.is_unavailable is True

        s = _make_state(_WASHER, "running", t1, t1)
        assert s.is_unavailable is False


# ---------------------------------------------------------------------------
# Tests: missing attributes degrade gracefully
# ---------------------------------------------------------------------------


class TestMissingAttributes:
    """Missing attributes must not raise — they degrade gracefully."""

    def test_null_attributes_json(self, tmp_path: Path) -> None:
        t1 = _T0 + timedelta(hours=10)
        rows = [_row(_WASHER, "running", t1, t1, attributes_json=None)]
        _write_part(tmp_path, rows)

        store = load_store(tmp_path)
        assert store.total_rows_after_dedup == 1
        assert store.states[0].attributes is None
        assert store.states[0].attributes_json is None

    def test_malformed_attributes_json(self, tmp_path: Path) -> None:
        t1 = _T0 + timedelta(hours=10)
        rows = [_row(_WASHER, "running", t1, t1, attributes_json="not valid json{{{")]
        _write_part(tmp_path, rows)

        store = load_store(tmp_path)
        assert store.total_rows_after_dedup == 1
        # attributes is None (parse failed), but attributes_json still has the raw string
        assert store.states[0].attributes is None
        assert store.states[0].attributes_json == "not valid json{{{"

    def test_valid_attributes_parsed(self, tmp_path: Path) -> None:
        t1 = _T0 + timedelta(hours=10)
        attrs = '{"device_class": "enum", "friendly_name": "Washer"}'
        rows = [_row(_WASHER, "running", t1, t1, attributes_json=attrs)]
        _write_part(tmp_path, rows)

        store = load_store(tmp_path)
        assert store.states[0].attributes == {
            "device_class": "enum",
            "friendly_name": "Washer",
        }


# ---------------------------------------------------------------------------
# Tests: multi-entity stores
# ---------------------------------------------------------------------------


class TestMultiEntity:
    """Multiple entities in the same store."""

    def test_multi_entity_store(self, tmp_path: Path) -> None:
        t1 = _T0 + timedelta(hours=10)
        t2 = _T0 + timedelta(hours=11)

        rows = [
            _row(_WASHER, "running", t1, t1),
            _row(_DISHWASHER, "idle", t2, t2),
        ]
        _write_part(tmp_path, rows)

        store = load_store(tmp_path)
        assert store.total_rows_after_dedup == 2
        assert store.entities == [_WASHER, _DISHWASHER]  # sorted alphabetically
        assert len(store.states_for_entity(_WASHER)) == 1
        assert len(store.states_for_entity(_DISHWASHER)) == 1

    def test_as_of_is_entity_scoped(self, tmp_path: Path) -> None:
        """as_of for one entity does not see another entity's states."""
        t1 = datetime(2026, 10, 15, 10, 0, 0, tzinfo=UTC)
        t2 = datetime(2026, 10, 15, 12, 0, 0, tzinfo=UTC)

        rows = [
            _row(_WASHER, "running", t1, t1),
            _row(_DISHWASHER, "idle", t2, t2),
        ]
        _write_part(tmp_path, rows)

        store = load_store(tmp_path)
        # At t2, washer should still show its t1 state
        result = store.as_of(_WASHER, t2)
        assert result is not None
        assert result.state == "running"
        assert result.entity == _WASHER


# ---------------------------------------------------------------------------
# Tests: empty store
# ---------------------------------------------------------------------------


class TestEmptyStore:
    """An empty store degrades gracefully."""

    def test_empty_store_no_parts(self, tmp_path: Path) -> None:
        store = load_store(tmp_path)
        assert store.total_rows_before_dedup == 0
        assert store.total_rows_after_dedup == 0
        assert store.duplicate_rows == 0
        assert store.parts_read == 0
        assert store.entities == []
        assert store.states == []

    def test_empty_store_empty_part(self, tmp_path: Path) -> None:
        _write_part(tmp_path, [])
        store = load_store(tmp_path)
        assert store.total_rows_before_dedup == 0
        assert store.total_rows_after_dedup == 0
        assert store.parts_read == 1  # part was readable, just empty


# ---------------------------------------------------------------------------
# Tests: reader never writes
# ---------------------------------------------------------------------------


class TestReaderNeverWrites:
    """The reader must never write anything to the data directory."""

    def test_load_store_does_not_create_files(self, tmp_path: Path) -> None:
        """Snapshot the directory before and after load_store."""
        t1 = _T0 + timedelta(hours=10)
        rows = [_row(_WASHER, "running", t1, t1)]
        _write_part(tmp_path, rows)

        # Snapshot all files before
        files_before = set()
        for p in tmp_path.rglob("*"):
            if p.is_file():
                files_before.add(p)
                # Also record mtime
        mtimes_before = {p: p.stat().st_mtime_ns for p in files_before}

        store = load_store(tmp_path)
        assert store.total_rows_after_dedup == 1

        # Snapshot all files after
        files_after = set()
        for p in tmp_path.rglob("*"):
            if p.is_file():
                files_after.add(p)

        # No new files
        new_files = files_after - files_before
        assert new_files == set(), f"reader created files: {new_files}"

        # No modified files
        for p in files_before:
            if p.exists():
                assert p.stat().st_mtime_ns == mtimes_before[p], f"reader modified file: {p}"


# ---------------------------------------------------------------------------
# Tests: format helpers
# ---------------------------------------------------------------------------


class TestFormatHelpers:
    """Summary and as-of result formatting."""

    def test_format_summary(self, tmp_path: Path) -> None:
        t1 = _T0 + timedelta(hours=10)
        rows = [_row(_WASHER, "running", t1, t1)]
        _write_part(tmp_path, rows)

        store = load_store(tmp_path)
        summary = format_summary(store)
        assert "parts=1" in summary
        assert "rows_before_dedup=1" in summary
        assert "rows_after_dedup=1" in summary
        assert "duplicates=0" in summary

    def test_format_as_of_result_none(self) -> None:
        t = datetime(2026, 10, 15, 10, 0, 0, tzinfo=UTC)
        result = format_as_of_result(None, entity=_WASHER, at=t)
        assert "no state" in result
        assert _WASHER in result

    def test_format_as_of_result_with_state(self) -> None:
        t = datetime(2026, 10, 15, 10, 0, 0, tzinfo=UTC)
        state = _make_state(_WASHER, "running", t, t)
        result = format_as_of_result(state, entity=_WASHER, at=t)
        assert "running" in result
        assert _WASHER in result


# ---------------------------------------------------------------------------
# Tests: provenance
# ---------------------------------------------------------------------------


class TestProvenance:
    """Per-row provenance is carried through."""

    def test_part_name_carried(self, tmp_path: Path) -> None:
        t1 = _T0 + timedelta(hours=10)
        rows = [_row(_WASHER, "running", t1, t1)]
        part_name = "ha-enum-20261015T000000Z-20261016T000000Z-abcd1234.parquet"
        _write_part(tmp_path, rows, part_name=part_name)

        store = load_store(tmp_path)
        assert store.states[0].part_name == part_name

    def test_extraction_id_carried(self, tmp_path: Path) -> None:
        t1 = _T0 + timedelta(hours=10)
        rows = [_row(_WASHER, "running", t1, t1, extraction_id="my-extraction-id")]
        _write_part(tmp_path, rows)

        store = load_store(tmp_path)
        assert store.states[0].extraction_id == "my-extraction-id"

    def test_carry_in_flag_carried(self, tmp_path: Path) -> None:
        t1 = _T0 + timedelta(hours=10)
        rows = [_row(_WASHER, "running", t1, t1, carry_in=True)]
        _write_part(tmp_path, rows)

        store = load_store(tmp_path)
        assert store.states[0].carry_in is True
