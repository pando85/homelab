"""Home Assistant history extraction — ingest enum state telemetry.

Reads the HA recorder history API for non-numeric state entities (washer and
dishwasher status sensors) and writes immutable Parquet parts under
``raw/ha_history/v1/parts/``, recording an extraction manifest on success.

Why this extraction exists
--------------------------
HA's recorder keeps only a short window of raw state history.  The deployed
configuration declares no ``purge_keep_days`` override
(``apps/home-assistant/config/configuration.yaml`` sets only ``recorder.db_url``),
so Home Assistant's default of 10 days applies; the *effective* coverage
boundary is unverified and may be shorter, and this module does not guess it.

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
module detects carry-in rows by checking whether a row's **``last_updated``**
is at or before the chunk start (NOT ``last_changed``), and sets
``carry_in=True`` for the **first** such row of each entity only.  Using
``last_updated`` correctly preserves attribute-only updates — events whose
``last_changed`` is old but ``last_updated`` is within the window are real
events, not carry-in.

The raw returned timestamp values are preserved verbatim (nothing is
invented), but the ``carry_in`` flag tells consumers not to treat them as
real event times.  The boundary ``last_updated == chunk_start`` is
ambiguous; we conservatively flag the first row there as carry-in rather
than asserting a confirmed transition.

``source_window_start`` records the **chunk** query start per chunk, so
carry-in rows correctly reference the chunk they were synthesised for
rather than the overall extraction window start.

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

Per-chunk accounting — why an empty chunk is never silent
---------------------------------------------------------
This source is **not re-extractable**: recorder history is purged within the
retention window, so a chunk that is skipped or comes back empty is gone
within days, unlike Prometheus long-term storage which can be re-queried at
any time.  A run that declares ten chunks and writes nine part files must not
look identical to a run that asked for nine.

Therefore every **declared** chunk gets a :class:`ChunkOutcome` record, created
before its first request, so a chunk that yields nothing still appears in the
manifest accounting.  The record holds what the extractor can prove — was the
chunk requested, did the request succeed, how many raw rows came back, how many
survived the half-open window filter, was a part file written — and the
manifest carries ``params["chunk_accounting"]`` (declared vs written counts and
a ``mismatch`` boolean) plus ``params["chunk_outcomes"]`` (the per-chunk
ledger).  Zero-row chunks additionally raise an ``empty_chunk:[start, end)``
quality flag naming their window, and a declared/written mismatch raises
``chunk_accounting_mismatch`` so the gap is visible without diffing two numbers.

What is deliberately **not** claimed: the *cause* of an empty chunk.  An empty
payload is equally consistent with (a) no state transitions in that window,
(b) the recorder having already purged it, or (c) a degenerate empty response,
and the HA history API does not distinguish them.  This module says so in the
flag text (:data:`EMPTY_CHUNK_CAUSE_NOTE`) instead of asserting a cause, and it
does not probe retention or write synthetic rows to find out.

What is distinguishable is recorded as such: a chunk whose payload carried rows
that all fell outside the half-open window is flagged separately from a chunk
whose payload carried nothing at all.
"""

from __future__ import annotations

import hashlib
import json
import logging
import os
import time
from collections.abc import Iterator
from dataclasses import dataclass
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
"""Never recorded in a manifest: the URL itself is replaced by a one-way digest."""

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
# Per-chunk quality flags
# ---------------------------------------------------------------------------

EMPTY_CHUNK_FLAG_PREFIX = "empty_chunk"
"""Prefix of the flag naming a declared chunk that came back with zero rows."""

FILTERED_CHUNK_FLAG_PREFIX = "empty_chunk_after_window_filter"
"""A chunk whose payload carried rows, none of which survived the window filter."""

CHUNK_ACCOUNTING_FLAG_PREFIX = "chunk_accounting_mismatch"
"""Prefix of the flag raised when declared chunks and written parts disagree."""

EMPTY_CHUNK_CAUSE_NOTE = (
    "cause not determinable from this response: an empty chunk is equally "
    "consistent with (a) no state transitions recorded in the window, (b) the "
    "recorder having already purged the window, or (c) a degenerate empty "
    "API response"
)
"""Stated in every empty-chunk flag: the API cannot tell these three apart.

Recorded instead of guessed.  This extractor does not probe recorder retention
and does not write a synthetic state to test availability — doing either would
change live Home Assistant configuration, which ADR-0003 forbids in this phase.
"""

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


def _suppress_httpx_info_logging() -> None:
    """Keep httpx's per-request INFO log out of this package's output.

    httpx logs every request at INFO with the **full URL**, e.g.::

        INFO httpx: HTTP Request: GET https://<host>/api/history/period/...?filter_entity_id=...

    The CLI configures root logging at INFO, so without this the recorder's base
    URL and the entity filter would reach stdout/stderr and, in production, Loki.
    Nothing here needs those lines: chunk progress is logged by this module and
    failures surface as sanitized exceptions.  Applied at the start of
    :func:`extract_ha_history` so it holds for every request this extractor makes.
    """
    logging.getLogger("httpx").setLevel(logging.WARNING)


def _sanitize_error(message: str, token: str, *, base_url: str | None = None) -> str:
    """Remove the token and the base URL (and its host) from an error message.

    Transport-level exceptions from :mod:`httpx` can embed the request URL, and a
    misbehaving proxy can echo the ``Authorization`` header into a response body.
    Both are scrubbed before the text reaches an exception message or a log line,
    so no credential or endpoint identity escapes into stdout, stderr or logs.
    """
    out = message
    if token:
        out = out.replace(token, "<REDACTED>")
    if base_url:
        out = out.replace(base_url, "<REDACTED>")
        host = httpx.URL(base_url).host
        if host:
            out = out.replace(host, "<REDACTED>")
    return out


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
                _sanitize_error(str(last_error), token, base_url=base_url),
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
                _sanitize_error(
                    f"HA returned HTTP {response.status_code}: {response.text[:500]}",
                    token,
                    base_url=base_url,
                ),
                status_code=response.status_code,
            )
            continue
        if response.status_code >= 400:
            raise HAHTTPError(
                _sanitize_error(
                    f"HA returned HTTP {response.status_code} (not retried): {response.text[:500]}",
                    token,
                    base_url=base_url,
                ),
                status_code=response.status_code,
            )

        try:
            payload = response.json()
        except ValueError as exc:
            raise HAResponseError(
                _sanitize_error(
                    f"HA response is not JSON: {response.text[:500]}", token, base_url=base_url
                )
            ) from exc

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
        _sanitize_error(
            f"gave up after {attempts} attempt(s) fetching HA history "
            f"(chunk_start={start_iso}): {last_error}",
            token,
            base_url=base_url,
        )
    )


# ---------------------------------------------------------------------------
# Per-chunk accounting
# ---------------------------------------------------------------------------


@dataclass
class ChunkOutcome:
    """What the recorder actually returned for one **declared** chunk.

    One record exists per declared chunk, created before the first request is
    attempted, so a chunk that yields nothing still shows up in the manifest
    accounting rather than vanishing from it.  The fields record only what the
    extractor can prove (was it requested, did the request succeed, how many
    raw rows came back, how many survived the half-open window filter, was a
    part file written); *why* a chunk is empty is not asserted — see
    :data:`EMPTY_CHUNK_CAUSE_NOTE`.

    Attributes:
        index: 1-based position among the declared chunks.
        chunk_start: Inclusive start of the chunk's half-open window.
        chunk_end: Exclusive end of the chunk's half-open window.
        requested: Whether a history request was issued for this chunk.
        request_succeeded: Whether a well-formed response came back for it.
        raw_rows: Rows the response carried for the requested entities.
        kept_rows: Rows kept after carry-in detection and the window filter.
        entities_in_response: Requested entities that carried at least one row.
        part_file: Data-dir-relative path of the part written, else ``None``.
    """

    index: int
    chunk_start: datetime
    chunk_end: datetime
    requested: bool = False
    request_succeeded: bool = False
    raw_rows: int = 0
    kept_rows: int = 0
    entities_in_response: tuple[str, ...] = ()
    part_file: str | None = None

    @property
    def window(self) -> str:
        """The chunk's half-open window, rendered for flag and note text."""
        return f"[{iso_z(self.chunk_start)}, {iso_z(self.chunk_end)})"

    @property
    def empty(self) -> bool:
        """True when a successful response yielded no rows for this chunk."""
        return self.request_succeeded and self.kept_rows == 0

    @property
    def payload_empty(self) -> bool:
        """True when the response carried no rows at all."""
        return self.request_succeeded and self.raw_rows == 0

    def flags(self) -> list[str]:
        """Quality flags for this chunk; empty when it produced rows.

        The window is always named, and the cause is stated as undetermined
        rather than guessed.  A chunk carrying rows that all fell outside the
        half-open window gets its own prefix, because that much *is*
        distinguishable from a payload that was empty in the first place.
        """
        if not self.request_succeeded or self.kept_rows:
            return []
        if self.raw_rows == 0:
            prefix = EMPTY_CHUNK_FLAG_PREFIX
            detail = "the response carried no rows for the requested entities"
        else:
            prefix = FILTERED_CHUNK_FLAG_PREFIX
            detail = (
                f"the response carried {self.raw_rows} raw row(s), none of which fell "
                "inside the half-open chunk window"
            )
        return [
            f"{prefix}:{self.window}: 0 rows kept, no part file written "
            f"({detail}); {EMPTY_CHUNK_CAUSE_NOTE}. "
            "HA recorder history is not re-extractable once purged, so this gap "
            "must be re-covered while the window is still available."
        ]

    def as_dict(self) -> dict[str, object]:
        """Structured ledger entry for the manifest."""
        return {
            "index": self.index,
            "window": self.window,
            "chunk_start": iso_z(self.chunk_start),
            "chunk_end": iso_z(self.chunk_end),
            "requested": self.requested,
            "request_succeeded": self.request_succeeded,
            "raw_rows": self.raw_rows,
            "kept_rows": self.kept_rows,
            "empty": self.empty,
            "payload_empty": self.payload_empty,
            "entities_in_response": list(self.entities_in_response),
            "part_file": self.part_file,
            "flags": self.flags(),
        }


def summarize_chunks(outcomes: list[ChunkOutcome], part_files: list[dict[str, object]]) -> dict:
    """Declared-vs-written accounting for the whole run.

    Both counts are always present so a consumer can compare them without
    inferring the declared count from the length of ``part_files``.
    """
    declared = len(outcomes)
    written = len(part_files)
    empty_windows = [o.window for o in outcomes if o.empty]
    return {
        "declared_chunks": declared,
        "requested_chunks": sum(1 for o in outcomes if o.requested),
        "succeeded_chunks": sum(1 for o in outcomes if o.request_succeeded),
        "chunks_with_rows": sum(1 for o in outcomes if o.kept_rows),
        "empty_chunks": len(empty_windows),
        "empty_chunk_windows": empty_windows,
        "payload_empty_chunks": sum(1 for o in outcomes if o.payload_empty),
        "parts_written": written,
        "mismatch": declared != written,
    }


def _mismatch_flags(accounting: dict) -> list[str]:
    """Flag making a declared/written disagreement visible without a diff."""
    if not accounting["mismatch"]:
        return []
    return [
        f"{CHUNK_ACCOUNTING_FLAG_PREFIX}: declared={accounting['declared_chunks']} "
        f"parts_written={accounting['parts_written']} "
        f"empty_chunks={accounting['empty_chunks']} "
        f"payload_empty={accounting['payload_empty_chunks']} "
        f"windows={'; '.join(accounting['empty_chunk_windows']) or 'none'} - at least one "
        "declared chunk returned zero rows and has no part file"
    ]


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

    Carry-in detection:
        HA's history API returns a *carry-in* state — the last known state from
        **before** the requested window.  HA **synthesises** the carry-in row's
        ``last_changed`` and ``last_updated`` to the query start time.  We
        identify carry-in by ``last_updated <= chunk_start``: only the **first**
        row of each entity whose ``last_updated`` is at or before the chunk start
        is a carry-in.  Attribute-only updates within the window have a *new*
        ``last_updated`` (real event time) even when ``last_changed`` is old,
        so they are correctly kept as real events.

        The exact ``last_updated == chunk_start`` boundary is ambiguous (the
        synthetic timestamp and a real event could coincide).  We conservatively
        flag the first row at that timestamp as carry-in and add a coverage note
        rather than asserting it is a confirmed transition.

    Half-open filter:
        Rows with ``last_updated >= chunk_end`` are excluded (future / out of
        window).  Rows with ``last_updated < chunk_start`` (other than the
        single carry-in) are real but stale and dropped.

    Deduplication:
        Two attribute-update events sharing the same ``last_changed`` but with
        different ``last_updated`` timestamps are both preserved — they are
        distinct events.  Rows are deduplicated only on the exact tuple
        ``(entity, last_updated, state, attributes_json)``.

    Returns rows sorted by (entity, last_updated, last_changed).
    """
    rows: list[dict] = []
    chunk_start_us = int(chunk_start.timestamp() * 1_000_000)
    chunk_end_us = int(chunk_end.timestamp() * 1_000_000)

    for entity_id in entities:
        entity_rows = response.get(entity_id, [])
        carry_in_found = False
        for raw_row in entity_rows:
            last_changed = _parse_ha_timestamp(
                raw_row["last_changed"], entity=entity_id, field_name="last_changed"
            )
            last_updated = _parse_ha_timestamp(
                raw_row["last_updated"], entity=entity_id, field_name="last_updated"
            )
            last_updated_us = int(last_updated.timestamp() * 1_000_000)

            # Half-open filter based on last_updated:
            # Exclude rows whose real event timestamp (last_updated) is at or
            # beyond chunk_end — those belong to the next chunk.
            if last_updated_us >= chunk_end_us:
                continue

            # Carry-in detection based on last_updated (NOT last_changed):
            # The first row with last_updated <= chunk_start is the carry-in.
            # An attribute-only update at time T has old last_changed but
            # NEW last_updated > chunk_start — it is a real event, not carry-in.
            is_carry_in = False
            if last_updated_us <= chunk_start_us:
                if not carry_in_found:
                    is_carry_in = True
                    carry_in_found = True
                else:
                    # Stale row from before the window (should not happen
                    # from HA but is possible with clock skew): drop.
                    continue

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

    # Deduplicate on exact (entity, last_updated, state, attributes_json) tuple.
    # Two attribute-update events sharing the same last_changed but different
    # last_updated are distinct and both kept.
    seen: set[tuple[str, int, str, str | None]] = set()
    deduped: list[dict] = []
    for r in rows:
        lu_us = int(r["last_updated"].timestamp() * 1_000_000)
        key = (r["entity"], lu_us, r["state"], r["attributes_json"])
        if key in seen:
            continue
        seen.add(key)
        deduped.append(r)
    rows = deduped

    # Deterministic ordering: entity, last_updated, last_changed.
    # Using last_updated as the primary time axis reflects real event order.
    rows.sort(key=lambda r: (r["entity"], r["last_updated"], r["last_changed"]))
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

    Every **declared** chunk is accounted for in the manifest, including the
    ones that return nothing: see :class:`ChunkOutcome` and
    :func:`summarize_chunks`.  A zero-row chunk raises a quality flag naming its
    window without asserting a cause, and a declared-vs-written part-file
    disagreement raises ``chunk_accounting_mismatch``.  This source cannot be
    re-extracted once the recorder purges it, so silence here would be permanent
    data loss.

    Args:
        data_dir: Data directory root; ``None`` resolves env/default.
        config_path: Tariff config used for ``config_hash``; ``None`` resolves
            via :func:`config.resolve_config_path`.
        start: ISO-8601 timestamp with explicit UTC offset (inclusive).
        end: ISO-8601 timestamp with explicit UTC offset (exclusive).
        entities: Entity IDs to extract; falsy selects defaults (washer and
            dishwasher status sensors).
        base_url: HA base URL; ``None`` resolves ``ENERGY_BASELINE_HA_URL``
            then the default.  Never recorded — the manifest keeps only a
            one-way digest of it.
        token: HA bearer token; ``None`` resolves ``ENERGY_BASELINE_HA_TOKEN``.
            Never recorded, logged, or included in an error message.
        client: Pre-configured :class:`httpx.Client` for test injection.
        chunk_seconds: Chunk width in seconds (default 24h).
        max_retries: Additional attempts after the first for 429/5xx/timeouts.

    Returns:
        The :class:`~pathlib.Path` of the written success manifest.

    Raises:
        ValueError: bad window, bad chunk width, missing token.
        HAHTTPError / HAResponseError: query or response failures.  A failure in
            any chunk aborts the run before a manifest is written, and the log
            names which declared chunks were already covered.
    """
    _suppress_httpx_info_logging()
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

    # Declared chunks are materialised up front: the count is part of the
    # contract of the run, so a chunk that never yields rows is still declared,
    # still accounted for, and still named in the manifest.
    chunks = list(iter_chunks(t_start, t_end, chunk_seconds))
    outcomes = [
        ChunkOutcome(index=i, chunk_start=c_start, chunk_end=c_end)
        for i, (c_start, c_end) in enumerate(chunks, start=1)
    ]

    manifest = new_manifest(
        source=SOURCE,
        config_path=cfg,
        params={
            "start": iso_z(t_start),
            "end": iso_z(t_end),
            "interval_semantics": "half-open [start, end)",
            # The base URL is deliberately NOT recorded: no endpoint identity
            # belongs in a written file.  A one-way digest keeps enough
            # provenance to tell two instances apart across runs.
            "endpoint": "home-assistant-history-api",
            "endpoint_instance_sha256_16": hashlib.sha256(url.encode("utf-8")).hexdigest()[:16],
            "api": f"{HISTORY_PATH}/{{start}} with filter_entity_id",
            "chunk_seconds": chunk_seconds,
            "chunks_declared": len(chunks),
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
    entities_with_no_rows: set[str] = set(entity_ids)

    own_client = client is None
    http_client = client if client is not None else httpx.Client(timeout=_TIMEOUT)
    try:
        for outcome, (c_start, c_end) in zip(outcomes, chunks, strict=True):
            outcome.requested = True
            try:
                response = fetch_history_chunk(
                    http_client,
                    base_url=url,
                    token=bearer_token,
                    entities=entity_ids,
                    chunk_start=c_start,
                    chunk_end=c_end,
                    max_retries=max_retries,
                )

                outcome.request_succeeded = True
                outcome.raw_rows = sum(len(rows) for rows in response.values())
                outcome.entities_in_response = tuple(
                    sorted(eid for eid, rows in response.items() if rows)
                )

                chunk_rows = rows_from_response(
                    response,
                    entities=entity_ids,
                    chunk_start=c_start,
                    chunk_end=c_end,
                    # Per-chunk: carry-in provenance is the chunk query start,
                    # not the overall extraction window start.
                    source_window_start=c_start,
                    extraction_id=extraction_id,
                )
                outcome.kept_rows = len(chunk_rows)

                # Track which entities had rows
                for eid in entity_ids:
                    if any(r["entity"] == eid for r in chunk_rows):
                        entities_with_no_rows.discard(eid)

                log.info(
                    "chunk %d/%d [%s, %s): raw_rows=%d rows=%d empty=%s entities=%s",
                    outcome.index,
                    len(outcomes),
                    iso_z(c_start),
                    iso_z(c_end),
                    outcome.raw_rows,
                    outcome.kept_rows,
                    outcome.empty,
                    ",".join(entity_ids),
                )

                quality_flags.extend(outcome.flags())
                if not chunk_rows:
                    continue  # empty chunk: accounted for in the ledger, no part file

                all_rows.extend(chunk_rows)

                table = rows_to_table(chunk_rows, extraction_id)
                validate_table("RAW_ENUM_STATE", table)
                payload = table_to_bytes(table)
                content_sha = hashlib.sha256(payload).hexdigest()
                part_file_path = part_path(root, SOURCE, PART_KIND, c_start, c_end, content_sha[:8])
                write_part_atomic(part_file_path, payload)
                outcome.part_file = str(part_file_path.relative_to(root))
                part_files.append(
                    {
                        "path": str(part_file_path.relative_to(root)),
                        "name": part_file_path.name,
                        "kind": PART_KIND,
                        "sha256": content_sha,
                        "rows": len(chunk_rows),
                        "chunk_start": iso_z(c_start),
                        "chunk_end": iso_z(c_end),
                        "entities": list(entity_ids),
                    }
                )
            except BaseException:
                # No success manifest is written, but partial coverage is the one
                # thing an operator cannot reconstruct later: name the declared
                # chunks already covered before re-raising.
                done = [o.window for o in outcomes if o.request_succeeded]
                log.error(
                    "HA extraction aborted at declared chunk %d/%d %s - covered before "
                    "abort: %s; uncovered: %s; NO manifest written",
                    outcome.index,
                    len(outcomes),
                    outcome.window,
                    ", ".join(done) or "none",
                    ", ".join(o.window for o in outcomes if not o.request_succeeded) or "none",
                )
                raise
    finally:
        if own_client:
            http_client.close()

    accounting = summarize_chunks(outcomes, part_files)

    # Entities that had no rows across all chunks
    for eid in sorted(entities_with_no_rows):
        quality_flags.append(f"no_rows_for_entity:{eid}")

    quality_flags.extend(_mismatch_flags(accounting))

    total_rows = len(all_rows)
    if total_rows == 0:
        quality_flags.append("no_rows_extracted:all")
        log.warning(
            "no rows found for %s in [%s, %s) — coverage is NOT established",
            ",".join(entity_ids),
            iso_z(t_start),
            iso_z(t_end),
        )

    if accounting["empty_chunks"]:
        log.warning(
            "%d of %d declared chunk(s) returned zero rows: %s — for HA recorder "
            "history this gap is only re-coverable while the recorder still holds it",
            accounting["empty_chunks"],
            accounting["declared_chunks"],
            ", ".join(accounting["empty_chunk_windows"]),
        )
    if accounting["mismatch"]:
        log.warning(
            "declared/written chunk mismatch: declared=%d parts_written=%d",
            accounting["declared_chunks"],
            accounting["parts_written"],
        )

    if accounting["mismatch"]:
        agreement = (
            f"MISMATCH declared_chunks={accounting['declared_chunks']} != "
            f"parts_written={accounting['parts_written']} "
            f"({accounting['empty_chunks']} chunk(s) returned zero rows: "
            f"{'; '.join(accounting['empty_chunk_windows'])}). "
        )
    else:
        agreement = (
            f"declared_chunks={accounting['declared_chunks']} "
            f"parts_written={accounting['parts_written']} (agree). "
        )

    manifest.row_count = total_rows
    manifest.finished_at = iso_z(datetime.now(UTC))
    manifest.part_files = part_files
    manifest.quality_flags = sorted(set(quality_flags))
    manifest.params["parts_written"] = accounting["parts_written"]
    manifest.params["chunk_accounting"] = accounting
    manifest.params["chunk_outcomes"] = [o.as_dict() for o in outcomes]
    manifest.coverage_notes = (
        f"{accounting['declared_chunks']} chunk(s) of <= {chunk_seconds}s covering "
        f"[{iso_z(t_start)}, {iso_z(t_end)}) via HA history API; "
        f"{total_rows} row(s) in {accounting['parts_written']} part file(s). "
        f"{agreement}"
        "An empty chunk is never treated as continuity, and its cause is not "
        "inferred: no state transitions, a recorder purge and a degenerate empty "
        "response are indistinguishable from the API alone, so each zero-row chunk "
        "is named in quality_flags (empty_chunk / empty_chunk_after_window_filter) "
        "and recorded per chunk in params.chunk_outcomes. Empty entity responses "
        "are recorded as no_rows_for_entity flags and never treated as continuity."
    )

    path = write_manifest(root, manifest)
    log.info(
        "wrote manifest %s (extraction_id=%s, rows=%d, parts=%d, declared_chunks=%d)",
        path,
        extraction_id,
        total_rows,
        accounting["parts_written"],
        accounting["declared_chunks"],
    )
    return path
