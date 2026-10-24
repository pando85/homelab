"""Read-side module for Home Assistant enum state history.

Makes the already-preserved appliance enum history queryable with correct
deduplication and correct as-of semantics.  This is ADR-0002 Phase 1 ingest
made readable — it does not detect appliance cycles, run NILM, forecast,
engineer features, train models, merge states into the 15-minute grid, or
change what ``build`` produces.

Why a separate reader instead of querying parts directly
---------------------------------------------------------
The extraction pipeline (:mod:`extract_ha`) deduplicates across runs on the
key ``(entity, last_changed, last_updated, state, attributes_json)`` —
deliberately excluding ``extraction_id``, ``carry_in`` and
``source_window_start``.  But part files are immutable and 12 known
pre-dedup duplicate rows were deliberately left in place rather than
rewriting history.  This reader applies that dedup at read time so every
downstream consumer sees exactly one copy of each logical observation.

As-of semantics
----------------
ADR-0002 requires historical as-of snapshots that never use later cycle
outcomes as input.  :func:`as_of` returns only states whose ``last_changed``
is at or before the query instant.  The boundary is **inclusive**: at exactly
a transition instant ``T``, the new state is visible.  This matches HA's
semantics — ``last_changed`` is when the state became effective — and makes
it structurally impossible to leak a later transition into an earlier
snapshot, because only rows with ``last_changed <= T`` are returned.

Event time vs observation time
-------------------------------
The raw data carries both: ``last_changed`` is when the state transition
actually occurred (event time), and ``last_updated`` is when HA last
touched the row (observation time — may be later for attribute-only
updates).  Both are preserved verbatim; neither is silently coerced into
the other.  The ``carry_in`` flag from extraction is preserved too, so
consumers can distinguish synthesised anchor rows from real transitions.
"""

from __future__ import annotations

import json
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path

import pyarrow.parquet as pq

from energy_baseline.extract_ha import identity_digest, to_epoch_us
from energy_baseline.paths import iter_parts, resolve_data_dir
from energy_baseline.schemas import RAW_ENUM_STATE

# ---------------------------------------------------------------------------
# Constants
# ---------------------------------------------------------------------------

SOURCE = "ha_history"
"""Source name matching :mod:`extract_ha`."""

#: States that indicate the entity is not reporting a real value.
#: Preserved, not dropped — ADR-0002 requires availability/unknown states
#: to be surfaced rather than silently filtered.
UNAVAILABLE_STATES = frozenset({"unavailable", "unknown"})


# ---------------------------------------------------------------------------
# Data types
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class HAState:
    """One deduplicated HA enum state observation.

    All timestamps are UTC-aware.  ``attributes`` is parsed from
    ``attributes_json`` on a best-effort basis — if parsing fails, it is
    ``None`` and ``attributes_json`` still holds the raw string.

    Attributes:
        entity: HA entity ID (e.g. ``sensor.lavadora_current_status``).
        state: The enum state string (e.g. ``running``, ``idle``).
        last_changed: When the state transition occurred (event time).
        last_updated: When HA last touched this row (observation time).
        attributes: Parsed attribute dict, or ``None`` if missing/unparseable.
        attributes_json: Raw JSON string, or ``None`` if no attributes.
        carry_in: Whether this row was a carry-in anchor from extraction.
        extraction_id: Which extraction run wrote this row.
        source_window_start: The chunk query start for this row.
        part_name: Filename of the part file this row was read from.
    """

    entity: str
    state: str
    last_changed: datetime
    last_updated: datetime
    attributes: dict | None
    attributes_json: str | None
    carry_in: bool
    extraction_id: str
    source_window_start: datetime
    part_name: str

    @property
    def is_unavailable(self) -> bool:
        """Whether this state indicates the entity is not reporting."""
        return self.state in UNAVAILABLE_STATES


@dataclass
class HAStore:
    """A deduplicated, read-only view of the HA enum state store.

    Constructed by :func:`load_store`.  Rows are sorted by
    ``(entity, last_changed, last_updated)`` after dedup.

    Attributes:
        states: Deduplicated state observations, sorted by entity and time.
        total_rows_before_dedup: Raw row count across all parts before dedup.
        total_rows_after_dedup: Distinct row count after dedup.
        duplicate_rows: ``total_rows_before_dedup - total_rows_after_dedup``.
        parts_read: Number of part files successfully read.
        part_names: Sorted list of part filenames that contributed rows.
        entities: Sorted list of distinct entity IDs in the store.
    """

    states: list[HAState]
    total_rows_before_dedup: int
    total_rows_after_dedup: int
    parts_read: int
    part_names: list[str]
    entities: list[str]

    @property
    def duplicate_rows(self) -> int:
        return self.total_rows_before_dedup - self.total_rows_after_dedup

    def as_of(
        self,
        entity: str,
        at: datetime,
    ) -> HAState | None:
        """What was known about *entity* at instant *at*.

        Returns the latest state whose ``last_changed <= at``, or ``None``
        if no state exists at or before that instant.

        The boundary is **inclusive**: at exactly a transition instant,
        the new state is returned.  This matches HA's semantics —
        ``last_changed`` is when the state became effective.

        Only rows with ``last_changed <= at`` are considered, so it is
        structurally impossible to leak a later transition into an
        earlier snapshot.

        Args:
            entity: HA entity ID.
            at: UTC-aware query instant.

        Raises:
            ValueError: *at* is naive (no tzinfo).
        """
        if at.tzinfo is None:
            raise ValueError(f"as_of requires an aware datetime, got naive: {at!r}")
        at_utc = at.astimezone(UTC)

        # Binary search would be faster for large stores, but the store is
        # small (tens of rows) and linear scan is clearer.
        best: HAState | None = None
        for s in self.states:
            if s.entity != entity:
                continue
            if s.last_changed > at_utc:
                continue
            if best is None or s.last_changed > best.last_changed:
                best = s
            elif s.last_changed == best.last_changed and s.last_updated > best.last_updated:
                # Same last_changed but later last_updated: attribute-only update.
                best = s
        return best

    def states_for_entity(self, entity: str) -> list[HAState]:
        """All deduplicated states for *entity*, sorted by time."""
        return [s for s in self.states if s.entity == entity]


# ---------------------------------------------------------------------------
# Loading
# ---------------------------------------------------------------------------


def _parse_attributes(attributes_json: str | None) -> dict | None:
    """Best-effort parse of ``attributes_json``.

    Returns ``None`` when the input is ``None`` or unparseable — missing
    attributes degrade gracefully, they do not raise.
    """
    if attributes_json is None:
        return None
    try:
        result = json.loads(attributes_json)
        if isinstance(result, dict):
            return result
        return None
    except (json.JSONDecodeError, TypeError, ValueError):
        return None


def load_store(data_dir: str | Path | None = None) -> HAStore:
    """Load and deduplicate all HA enum states from the raw store.

    Reads every part file under ``raw/ha_history/v1/parts/``, applies
    the same dedup key as :mod:`extract_ha` (the five
    :data:`~extract_ha.ROW_IDENTITY_FIELDS` columns), and returns a
    sorted, deduplicated view.

    The reader never writes anything — no part files, no manifests, no
    temporary files.  It is purely read-side.

    Args:
        data_dir: Data directory root; ``None`` resolves via
            :func:`paths.resolve_data_dir`.

    Returns:
        A :class:`HAStore` with deduplicated states and accounting.
    """
    root = resolve_data_dir(None if data_dir is None else str(data_dir))
    files = iter_parts(root, SOURCE)

    all_rows: list[tuple[HAState, tuple]] = []  # (state, identity_key)
    total_before = 0
    parts_read = 0
    part_names: list[str] = []

    for path in files:
        try:
            table = pq.read_table(path, schema=RAW_ENUM_STATE)
        except Exception:
            # Unreadable parts are skipped — the reader degrades gracefully.
            continue
        parts_read += 1
        part_names.append(path.name)

        entities = table.column("entity").to_pylist()
        states = table.column("state").to_pylist()
        attrs_json = table.column("attributes_json").to_pylist()
        last_changeds = table.column("last_changed").to_pylist()
        last_updateds = table.column("last_updated").to_pylist()
        extraction_ids = table.column("extraction_id").to_pylist()
        carry_ins = table.column("carry_in").to_pylist()
        source_windows = table.column("source_window_start").to_pylist()

        for i in range(table.num_rows):
            total_before += 1
            entity = entities[i]
            state = states[i]
            aj = attrs_json[i]
            lc = last_changeds[i]
            lu = last_updateds[i]

            identity = (
                entity,
                to_epoch_us(lc),
                to_epoch_us(lu),
                state,
                None if aj is None else str(aj),
            )
            digest = identity_digest(identity)

            ha_state = HAState(
                entity=entity,
                state=state,
                last_changed=lc,
                last_updated=lu,
                attributes=_parse_attributes(aj),
                attributes_json=None if aj is None else str(aj),
                carry_in=bool(carry_ins[i]),
                extraction_id=extraction_ids[i],
                source_window_start=source_windows[i],
                part_name=path.name,
            )
            all_rows.append((ha_state, digest))

    # Dedup: first occurrence wins (parts are sorted by name, which is
    # chronological by chunk start).
    seen: set[str] = set()
    deduped: list[HAState] = []
    for ha_state, digest in all_rows:
        if digest in seen:
            continue
        seen.add(digest)
        deduped.append(ha_state)

    # Sort by (entity, last_changed, last_updated)
    deduped.sort(key=lambda s: (s.entity, s.last_changed, s.last_updated))

    entities_sorted = sorted({s.entity for s in deduped})

    return HAStore(
        states=deduped,
        total_rows_before_dedup=total_before,
        total_rows_after_dedup=len(deduped),
        parts_read=parts_read,
        part_names=sorted(part_names),
        entities=entities_sorted,
    )


# ---------------------------------------------------------------------------
# CLI-oriented summary
# ---------------------------------------------------------------------------


def format_summary(store: HAStore) -> str:
    """One-line summary of the store contents and dedup counts."""
    return (
        f"parts={store.parts_read} "
        f"rows_before_dedup={store.total_rows_before_dedup} "
        f"rows_after_dedup={store.total_rows_after_dedup} "
        f"duplicates={store.duplicate_rows} "
        f"entities={len(store.entities)}"
    )


def format_as_of_result(state: HAState | None, *, entity: str, at: datetime) -> str:
    """Format an as-of query result for display."""
    if state is None:
        return f"no state for {entity} at or before {at.isoformat()}"
    return (
        f"entity={state.entity} "
        f"state={state.state} "
        f"last_changed={state.last_changed.isoformat()} "
        f"last_updated={state.last_updated.isoformat()} "
        f"carry_in={state.carry_in} "
        f"part={state.part_name}"
    )
