"""Home Assistant history extraction — ingest enum state telemetry.

Reads the HA recorder history API for non-numeric state entities (washer and
dishwasher status sensors) and writes immutable Parquet parts under
``raw/ha_history/v1/parts/``, recording an extraction manifest on success.

Why this extraction exists
--------------------------
HA's recorder keeps only ~10 days of raw state history (``purge_keep_days``).
Enum state strings for appliances exist **nowhere else** — the ``sensor``
domain does not export non-numeric states as Prometheus gauges.  History not
extracted within the purge window is permanently lost.

The synthetic carry-in problem
------------------------------
When querying ``GET /api/history/period/{start}``, HA returns a *carry-in*
state: the last known state from **before** the requested window, so the
pipeline knows the state at window start.  However, HA **synthesises its
timestamps**: the carry-in row's ``last_changed`` and ``last_updated`` are
both set to the *query start time*, not the real time the state began.

If downstream consumers treated that timestamp as a real transition they
would fabricate an appliance event on every single extraction run.  This
module detects carry-in rows by checking whether a row's ``last_changed``
is at or before the chunk start, and sets ``carry_in=True`` for those rows.
The raw returned timestamp values are preserved verbatim (nothing is
invented), but the ``carry_in`` flag tells consumers not to treat them as
real event times.

``source_window_start`` records the requested window start for every row,
so consumers can always tell which extraction window a row came from.

Half-open window semantics
--------------------------
The requested interval is ``[start, end)``.  HA's ``end_time`` parameter is
inclusive, so rows with a genuine timestamp ``>= end`` are filtered out
after the response is received.  The window is split into bounded chunks
(default 24h) so a 10-day backfill does not become one giant response.

Empty results
-------------
An empty list for a requested entity is NOT proof it was continuously in
one state for the whole window — the recorder may have purged the window,
or the entity may not have existed.  This is recorded as a quality flag
(``no_rows_for_entity:<entity_id>``) and never silently treated as
continuity.
"""

from __future__ import annotations

import hashlib
import json
import logging
import os
import time
from collections.abc import Iterator
from datetime import UTC, datetime, timedelta
from io import BytesIO
from pathlib import Path

import httpx
import pyarrow as pa
import pyarrow.parquet as pq

from energy_baseline.config import resolve_config_path
from energy_baseline.manifest import new_manifest, write_manifest
from energy_baseline.paths import (
    part_path,
    resolve_data_dir,
    write_part_atomic,
)
from energy_baseline.schemas import RAW_ENUM_STATE, validate_table

log = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# Constants
# ---------------------------------------------------------------------------

SOURCE = "ha_history"
"""Source name for the parts directory and manifest ``source`` field."""

PART_KIND = "ha-enum"
"""``kind`` component of part filenames."""

BASE_URL_ENV_VAR = "ENERGY_BASELINE_HA_URL"
TOKEN_ENV_VAR = "ENERGY_BASELINE_HA_TOKEN"

DEFAULT_BASE_URL = "https://hass.grigri.cloud"

DEFAULT_ENTITIES: tuple[str, ...] = (
    "sensor.lavadora_current_status",
    "sensor.lavavajillas_current_status",
)

DEFAULT_CHUNK_SECONDS = 86400

HISTORY_PATH = "/api/history/period"

_TIMEOUT = httpx.Timeout(60.0, connect=10.0)
_BACKOFF_BASE_SECONDS = 0.5
_BACKOFF_MAX_SECONDS = 30.0

# ---------------------------------------------------------------------------
# Exceptions
# ---------------------------------------------------------------------------


class HAExtractionError(RuntimeError):
    """Base class for HA extraction failures (no success manifest is written)."""


class HAHTTPError(HAExtractionError):
    """Non-retryable HTTP failure, or retryable after exhausting retries."""

    def __init__(self, message: str, *, status_code: int | None = None) -> None:
        super().__init__(message)
        self.status_code = status_code


class HAResponseError(HAExtractionError):
    """The response payload violates the HA history API contract."""


# ---------------------------------------------------------------------------
# Pure helpers
# ---------------------------------------------------------------------------


def parse_utc(value: str, *, name: str) -> datetime:
    """Parse an ISO-8601 timestamp, requiring an explicit offset, and make it UTC.

    Raises:
        ValueError: *value* is not ISO-8601, or is naive (no offset/``Z``).
    """
    if isinstance(value, datetime):
        if value.tzinfo is None:
            raise ValueError(
                f"{name}={value!r} is a naive datetime — an explicit UTC offset or "
                "'Z' suffix is required"
            )
        return value.astimezone(UTC)
    text = (value or "").strip()
    if not text:
        raise ValueError(f"{name} must be a non-empty ISO-8601 string with a UTC offset")
    candidate = text[:-1] + "+00:00" if text.endswith(("Z", "z")) else text
    try:
        dt = datetime.fromisoformat(candidate)
    except ValueError as exc:
        raise ValueError(f"{name}={value!r} is not valid ISO-8601: {exc}") from exc
    if dt.tzinfo is None:
        raise ValueError(
            f"{name}={value!r} is a naive timestamp — an explicit UTC offset or "
            "'Z' suffix is required"
        )
    return dt.astimezone(UTC)


def iso_z(dt: datetime) -> str:
    """Render an aware datetime as ISO-8601 UTC with a ``Z`` suffix."""
    if dt.tzinfo is None:
        raise ValueError(f"naive datetime: {dt!r}")
    utc_dt = dt.astimezone(UTC)
    frac = f".{utc_dt.microsecond:06d}".rstrip("0") if utc_dt.microsecond else ""
    return utc_dt.strftime("%Y-%m-%dT%H:%M:%S") + frac + "Z"


def iter_chunks(
    start: datetime, end: datetime, chunk_seconds: int | float
) -> Iterator[tuple[datetime, datetime]]:
    """Yield ``[c_start, c_end)`` chunks partitioning ``[start, end)`` exactly."""
    if chunk_seconds <= 0:
        raise ValueError(f"chunk_seconds must be > 0, got {chunk_seconds!r}")
    k = 0
    while True:
        c_start = start + timedelta(seconds=chunk_seconds * k)
        if c_start >= end:
            break
        c_end = min(start + timedelta(seconds=chunk_seconds * (k + 1)), end)
        yield c_start, c_end
        k += 1


def resolve_base_url(base_url: str | None = None) -> str:
    """``base_url`` arg > ``ENERGY_BASELINE_HA_URL`` env > default."""
    url = base_url or os.environ.get(BASE_URL_ENV_VAR) or DEFAULT_BASE_URL
    url = url.strip().rstrip("/")
    parsed = httpx.URL(url)
    if parsed.scheme not in ("http", "https"):
        raise ValueError(f"HA base_url must be http(s), got {url!r}")
    if not parsed.host:
        raise ValueError(f"HA base_url has no host: {url!r}")
    if parsed.username or parsed.password:
        raise ValueError("HA base_url must not embed credentials")
    return url


def resolve_token(token: str | None = None) -> str:
    """Resolve the bearer token from arg > env.

    Raises:
        ValueError: if no token is available.
    """
    if token is not None:
        if not token.strip():
            raise ValueError("HA token must not be empty")
        return token
    env_token = os.environ.get(TOKEN_ENV_VAR)
    if env_token:
        return env_token
    raise ValueError(f"HA bearer token not provided: pass token= or set {TOKEN_ENV_VAR}")


def resolve_entities(entities: list[str] | None) -> tuple[str, ...]:
    """Resolve requested entities or fall back to defaults."""
    if not entities:
        return DEFAULT_ENTITIES
    resolved = tuple(str(e).strip() for e in entities if str(e).strip())
    if not resolved:
        return DEFAULT_ENTITIES
    return resolved


def _parse_ha_timestamp(raw: str, *, entity: str, field_name: str) -> datetime:
    """Parse an HA timestamp string into a UTC datetime."""
    text = (raw or "").strip()
    if not text:
        raise HAResponseError(f"empty {field_name} for entity {entity!r}")
    # HA uses ISO-8601 with 'Z' suffix or +00:00
    candidate = text[:-1] + "+00:00" if text.endswith(("Z", "z")) else text
    try:
        dt = datetime.fromisoformat(candidate)
    except ValueError as exc:
        raise HAResponseError(
            f"invalid {field_name}={text!r} for entity {entity!r}: {exc}"
        ) from exc
    if dt.tzinfo is None:
        # Assume UTC if HA sends a naive timestamp
        dt = dt.replace(tzinfo=UTC)
    return dt.astimezone(UTC)


def _is_retryable_status(status_code: int) -> bool:
    return status_code == 429 or status_code >= 500


def _sanitize_error(message: str, token: str) -> str:
    """Remove any occurrence of the token from error messages."""
    if token and token in message:
        return message.replace(token, "<REDACTED>")
    return message


# ---------------------------------------------------------------------------
# HTTP query layer
# ---------------------------------------------------------------------------


def fetch_history_chunk(
    client: httpx.Client,
    *,
    base_url: str,
    token: str,
    entities: tuple[str, ...],
    chunk_start: datetime,
    chunk_end: datetime,
    max_retries: int = 3,
) -> dict[str, list[dict]]:
    """Fetch one chunk of HA history, returning ``{entity_id: [rows]}``.

    Retries 429/5xx/transport errors with exponential backoff.
    401/403 fail immediately with no retry; the token is never in the error.

    Raises:
        HAHTTPError: non-retryable HTTP error or retries exhausted.
    """
    start_iso = iso_z(chunk_start)
    url = f"{base_url}{HISTORY_PATH}/{start_iso}"
    params: dict[str, str] = {
        "filter_entity_id": ",".join(entities),
        "end_time": iso_z(chunk_end),
        "significant_changes_only": "0",
    }
    headers = {"Authorization": f"Bearer {token}"}

    attempts = max(0, int(max_retries)) + 1
    last_error: Exception | None = None

    for attempt in range(attempts):
        if attempt:
            delay = min(_BACKOFF_BASE_SECONDS * 2 ** (attempt - 1), _BACKOFF_MAX_SECONDS)
            log.warning(
                "HA history retry %d/%d in %.1fs (chunk_start=%s): %s",
                attempt,
                attempts - 1,
                delay,
                start_iso,
                last_error,
            )
            time.sleep(delay)
        try:
            response = client.get(url, params=params, headers=headers)
        except httpx.TransportError as exc:
            last_error = exc
            continue

        # Auth failures: fail immediately, never retry, never leak token
        if response.status_code in (401, 403):
            raise HAHTTPError(
                f"HA authentication failed (HTTP {response.status_code}) — "
                "check the bearer token; not retried",
                status_code=response.status_code,
            )

        if _is_retryable_status(response.status_code):
            last_error = HAHTTPError(
                f"HA returned HTTP {response.status_code}: {response.text[:500]}",
                status_code=response.status_code,
            )
            continue
        if response.status_code >= 400:
            raise HAHTTPError(
                f"HA returned HTTP {response.status_code} (not retried): {response.text[:500]}",
                status_code=response.status_code,
            )

        try:
            payload = response.json()
        except ValueError as exc:
            raise HAResponseError(f"HA response is not JSON: {response.text[:500]}") from exc

        if not isinstance(payload, list):
            raise HAResponseError(
                f"HA history API returned non-list payload: {type(payload).__name__}"
            )

        # Parse the per-entity lists
        result: dict[str, list[dict]] = {e: [] for e in entities}
        for entity_list in payload:
            if not isinstance(entity_list, list):
                raise HAResponseError(
                    f"HA history entry is not a list: {type(entity_list).__name__}"
                )
            for row in entity_list:
                if not isinstance(row, dict):
                    raise HAResponseError(f"HA history row is not an object: {type(row).__name__}")
                eid = row.get("entity_id")
                if not eid:
                    raise HAResponseError("HA history row missing 'entity_id'")
                if eid not in result:
                    raise HAHTTPError(
                        f"HA returned unexpected entity {eid!r} not in requested set "
                        f"{sorted(entities)}"
                    )
                result[eid].append(row)

        return result

    raise HAHTTPError(
        f"gave up after {attempts} attempt(s) fetching HA history "
        f"(chunk_start={start_iso}): {last_error}"
    )


# ---------------------------------------------------------------------------
# Row assembly
# ---------------------------------------------------------------------------


def rows_from_response(
    response: dict[str, list[dict]],
    *,
    entities: tuple[str, ...],
    chunk_start: datetime,
    chunk_end: datetime,
    source_window_start: datetime,
    extraction_id: str,
) -> list[dict]:
    """Convert raw HA response rows into schema-compatible dicts.

    Handles carry-in detection, half-open filtering, and entity validation.
    Returns rows sorted by (entity, last_changed, last_updated).
    """
    rows: list[dict] = []
    chunk_start_us = chunk_start.timestamp() * 1_000_000

    for entity_id in entities:
        entity_rows = response.get(entity_id, [])
        for raw_row in entity_rows:
            last_changed = _parse_ha_timestamp(
                raw_row["last_changed"], entity=entity_id, field_name="last_changed"
            )
            last_updated = _parse_ha_timestamp(
                raw_row["last_updated"], entity=entity_id, field_name="last_updated"
            )

            # Half-open filter: exclude rows with genuine timestamp >= chunk_end.
            # The carry-in row has last_changed == chunk_start (synthetic), so it
            # passes the >= chunk_start check.  Real events at exactly chunk_start
            # are included (half-open [start, end)).
            last_changed_us = last_changed.timestamp() * 1_000_000
            if last_changed_us >= chunk_end.timestamp() * 1_000_000:
                continue

            # Carry-in detection: HA synthesises the carry-in row's timestamps
            # to the query start time.  A row whose last_changed <= chunk_start
            # is a carry-in (the state existed before the window).
            is_carry_in = last_changed_us <= chunk_start_us

            # Serialize attributes to JSON exactly as HA returned them
            attrs = raw_row.get("attributes")
            if attrs is not None:
                attributes_json = json.dumps(attrs, sort_keys=True, ensure_ascii=False)
            else:
                attributes_json = None

            state = raw_row.get("state")
            if state is None:
                raise HAResponseError(f"HA history row for {entity_id!r} missing 'state'")

            rows.append(
                {
                    "entity": entity_id,
                    "state": str(state),
                    "attributes_json": attributes_json,
                    "last_changed": last_changed,
                    "last_updated": last_updated,
                    "extraction_id": extraction_id,
                    "carry_in": is_carry_in,
                    "source_window_start": source_window_start,
                }
            )

    # Deterministic ordering: entity, last_changed, last_updated
    rows.sort(key=lambda r: (r["entity"], r["last_changed"], r["last_updated"]))
    return rows


def rows_to_table(rows: list[dict], extraction_id: str) -> pa.Table:
    """Build a ``RAW_ENUM_STATE``-typed table from assembled rows."""
    if not rows:
        return pa.table(
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

    return pa.table(
        {
            "entity": pa.array([r["entity"] for r in rows], pa.string()),
            "state": pa.array([r["state"] for r in rows], pa.string()),
            "attributes_json": pa.array([r["attributes_json"] for r in rows], pa.string()),
            "last_changed": pa.array(
                [r["last_changed"] for r in rows], pa.timestamp("us", tz="UTC")
            ),
            "last_updated": pa.array(
                [r["last_updated"] for r in rows], pa.timestamp("us", tz="UTC")
            ),
            "extraction_id": pa.array([r["extraction_id"] for r in rows], pa.string()),
            "carry_in": pa.array([r["carry_in"] for r in rows], pa.bool_()),
            "source_window_start": pa.array(
                [r["source_window_start"] for r in rows],
                pa.timestamp("us", tz="UTC"),
            ),
        },
        schema=RAW_ENUM_STATE,
    )


def table_to_bytes(table: pa.Table) -> bytes:
    """Serialise a table to Parquet bytes."""
    sink = BytesIO()
    pq.write_table(table, sink)
    return sink.getvalue()


# ---------------------------------------------------------------------------
# Public API
# ---------------------------------------------------------------------------


def extract_ha_history(
    *,
    data_dir: str | None,
    config_path: str | None,
    start: str,
    end: str,
    entities: list[str],
    base_url: str | None = None,
    token: str | None = None,
    client: httpx.Client | None = None,
    chunk_seconds: int = DEFAULT_CHUNK_SECONDS,
    max_retries: int = 3,
) -> Path:
    """Extract enum state history from the Home Assistant recorder API.

    Reads the HA history API for the given entities and time window,
    writes immutable Parquet parts under ``raw/ha_history/v1/parts/``,
    and records an extraction manifest.

    Args:
        data_dir: Data directory root; ``None`` resolves env/default.
        config_path: Tariff config used for ``config_hash``; ``None`` resolves
            via :func:`config.resolve_config_path`.
        start: ISO-8601 timestamp with explicit UTC offset (inclusive).
        end: ISO-8601 timestamp with explicit UTC offset (exclusive).
        entities: Entity IDs to extract; falsy selects defaults (washer and
            dishwasher status sensors).
        base_url: HA base URL; ``None`` resolves ``ENERGY_BASELINE_HA_URL``
            then the default.
        token: HA bearer token; ``None`` resolves ``ENERGY_BASELINE_HA_TOKEN``.
        client: Pre-configured :class:`httpx.Client` for test injection.
        chunk_seconds: Chunk width in seconds (default 24h).
        max_retries: Additional attempts after the first for 429/5xx/timeouts.

    Returns:
        The :class:`~pathlib.Path` of the written success manifest.

    Raises:
        ValueError: bad window, bad chunk width, missing token.
        HAHTTPError / HAResponseError: query or response failures.
    """
    root = resolve_data_dir(None if data_dir is None else str(data_dir))
    cfg = resolve_config_path(None if config_path is None else str(config_path))
    if not cfg.is_file():
        raise FileNotFoundError(f"tariff config not found: {cfg}")

    t_start = parse_utc(start, name="start")
    t_end = parse_utc(end, name="end")
    if t_start >= t_end:
        raise ValueError(f"start must be before end: [{iso_z(t_start)}, {iso_z(t_end)})")
    if chunk_seconds <= 0:
        raise ValueError(f"chunk_seconds must be > 0, got {chunk_seconds!r}")
    if max_retries < 0:
        raise ValueError(f"max_retries must be >= 0, got {max_retries!r}")

    url = resolve_base_url(base_url)
    bearer_token = resolve_token(token)
    entity_ids = resolve_entities(entities)
    started_at = iso_z(datetime.now(UTC))

    manifest = new_manifest(
        source=SOURCE,
        config_path=cfg,
        params={
            "start": iso_z(t_start),
            "end": iso_z(t_end),
            "interval_semantics": "half-open [start, end)",
            "base_url": url,
            "api": f"{HISTORY_PATH}/{{start}} with filter_entity_id",
            "chunk_seconds": chunk_seconds,
            "max_retries": max_retries,
            "significant_changes_only": 0,
            "entities": list(entity_ids),
        },
        entities=list(entity_ids),
        source_window_start=iso_z(t_start),
        source_window_end=iso_z(t_end),
        started_at=started_at,
    )
    extraction_id = manifest.extraction_id

    all_rows: list[dict] = []
    quality_flags: list[str] = []
    part_files: list[dict[str, object]] = []
    chunk_count = 0
    entities_with_no_rows: set[str] = set(entity_ids)

    own_client = client is None
    http_client = client if client is not None else httpx.Client(timeout=_TIMEOUT)
    try:
        for c_start, c_end in iter_chunks(t_start, t_end, chunk_seconds):
            chunk_count += 1
            response = fetch_history_chunk(
                http_client,
                base_url=url,
                token=bearer_token,
                entities=entity_ids,
                chunk_start=c_start,
                chunk_end=c_end,
                max_retries=max_retries,
            )

            chunk_rows = rows_from_response(
                response,
                entities=entity_ids,
                chunk_start=c_start,
                chunk_end=c_end,
                source_window_start=t_start,
                extraction_id=extraction_id,
            )

            # Track which entities had rows
            for eid in entity_ids:
                if any(r["entity"] == eid for r in chunk_rows):
                    entities_with_no_rows.discard(eid)

            log.info(
                "chunk %d [%s, %s): rows=%d entities=%s",
                chunk_count,
                iso_z(c_start),
                iso_z(c_end),
                len(chunk_rows),
                ",".join(entity_ids),
            )

            if chunk_rows:
                all_rows.extend(chunk_rows)

                table = rows_to_table(chunk_rows, extraction_id)
                validate_table("RAW_ENUM_STATE", table)
                payload = table_to_bytes(table)
                content_sha = hashlib.sha256(payload).hexdigest()
                path = part_path(root, SOURCE, PART_KIND, c_start, c_end, content_sha[:8])
                write_part_atomic(path, payload)
                part_files.append(
                    {
                        "path": str(path.relative_to(root)),
                        "name": path.name,
                        "kind": PART_KIND,
                        "sha256": content_sha,
                        "rows": len(chunk_rows),
                        "chunk_start": iso_z(c_start),
                        "chunk_end": iso_z(c_end),
                        "entities": list(entity_ids),
                    }
                )
    finally:
        if own_client:
            http_client.close()

    # Entities that had no rows across all chunks
    for eid in sorted(entities_with_no_rows):
        quality_flags.append(f"no_rows_for_entity:{eid}")

    total_rows = len(all_rows)
    if total_rows == 0:
        quality_flags.append("no_rows_extracted:all")
        log.warning(
            "no rows found for %s in [%s, %s) — coverage is NOT established",
            ",".join(entity_ids),
            iso_z(t_start),
            iso_z(t_end),
        )

    manifest.row_count = total_rows
    manifest.finished_at = iso_z(datetime.now(UTC))
    manifest.part_files = part_files
    manifest.quality_flags = sorted(set(quality_flags))
    manifest.coverage_notes = (
        f"{chunk_count} chunk(s) of <= {chunk_seconds}s covering "
        f"[{iso_z(t_start)}, {iso_z(t_end)}) via HA history API; "
        f"{total_rows} row(s) in {len(part_files)} part file(s). "
        "Empty entity responses are recorded as quality flags and never "
        "treated as continuity."
    )

    path = write_manifest(root, manifest)
    log.info(
        "wrote manifest %s (extraction_id=%s, rows=%d, parts=%d)",
        path,
        extraction_id,
        total_rows,
        len(part_files),
    )
    return path
