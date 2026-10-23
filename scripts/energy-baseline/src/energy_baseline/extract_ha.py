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

Cross-run deduplication — why re-extracting an overlap is free
--------------------------------------------------------------
The operational model is a short recurring pull of a window that deliberately
overlaps what was already pulled: 10-day retention leaves no room for exact,
gap-free scheduling, and ADR-0003 records that "overlapping windows are cheap
and idempotent to re-pull because the volume is negligible".  Without a
store-level rule that claim was false — two real extractions over
2026-10-14 → 2026-10-23 produced 51 rows of which only 39 were distinct
logical observations (12 duplicates, 0 genuinely new rows), and the manifest
of the second run said nothing about it.

Rows are therefore deduplicated **against the store**, not only against one
chunk's payload.  The logical identity of an observation is

    (entity, last_changed, last_updated, state, attributes_json)

— exactly the five ``RAW_ENUM_STATE`` columns that describe what Home Assistant
recorded.  The remaining three columns are excluded because they describe *this
pull*, not the observation:

* ``extraction_id`` is a fresh UUID per run, so two overlapping chunks from two
  runs are never byte-identical and the content-addressed no-clobber store
  cannot recognise them as duplicates on its own.
* ``source_window_start`` is the chunk query start — an artefact of how the
  requested window was chopped up.
* ``carry_in`` is derived from ``last_updated`` against that same chunk start
  (see above), so it is a statement about the query, not about the row.

Identity excludes run provenance by necessity (``extraction_id``) *and* by
choice: an incoming row whose ``carry_in`` classification disagrees with the
copy already stored is still the same observation, so it is suppressed like any
other duplicate — immutable parts are never rewritten — and the disagreement is
reported as :data:`CARRY_IN_DISAGREEMENT_FLAG_PREFIX` instead of being kept
silently.

Suppressed duplicates on an intentional overlap are **expected and benign**: the
observation stays stored exactly once and no information is lost.  That is
recorded by :data:`DUPLICATE_ROWS_FLAG_PREFIX`, an accounting disclosure rather
than an alarm, deliberately distinct in both prefix and wording from the
genuine-problem flags (``empty_chunk:``, ``empty_chunk_after_window_filter:``,
``chunk_accounting_mismatch``, ``no_rows_for_entity:``).

Prior coverage comes from the **parts on disk** (:func:`load_stored_row_index`),
not from manifests alone and not from window arithmetic: a row physically
present is a row that must not be written twice, and a run that died after
writing some parts still left those parts behind.  Manifests are read only to
report provenance — which parts a successful run claimed and which are orphans.
The requested window is never narrowed by prior coverage; chunk boundaries stay
a deterministic function of ``[start, end)`` so carry-in anchoring, part naming
and part content remain reproducible, and dedup happens per row on top.  The
consequence is the property the Prometheus path already had: re-running a window
that is already fully stored writes **zero** new parts, and its manifest says so
in numbers instead of looking like a clean, productive run.

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
survived the half-open window filter, how many of those were already present in
the store, how many were genuinely new, and was a part file written — and the
manifest carries ``params["chunk_accounting"]`` (declared vs written counts and
a ``mismatch`` boolean), ``params["row_accounting"]`` (those row-level totals)
plus ``params["chunk_outcomes"]`` (the per-chunk
ledger).  Zero-row chunks additionally raise an ``empty_chunk:[start, end)``
quality flag naming their window, and a declared/written mismatch raises
``chunk_accounting_mismatch`` so the gap is visible without diffing two numbers.

A chunk whose rows were *all* already present is recorded as fully suppressed: it
has no part file, yet it is not a gap, and it does not raise
``chunk_accounting_mismatch``.  Declared chunks are reconciled against
``parts_written + fully_suppressed_chunks``, so the mismatch flag keeps naming
only chunks that produced nothing that is not already stored.

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
from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta
from io import BytesIO
from pathlib import Path

import httpx
import pyarrow as pa
import pyarrow.parquet as pq

from energy_baseline.config import resolve_config_path
from energy_baseline.manifest import new_manifest, write_manifest
from energy_baseline.paths import (
    iter_parts,
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

DUPLICATE_ROWS_FLAG_PREFIX = "duplicate_rows_suppressed"
"""Prefix of the flag disclosing rows already present in the store.

This is an **accounting disclosure, not an alarm**: suppressing a duplicate on
an intentionally overlapping re-extraction is the expected outcome, and the
observation remains stored exactly once.  The prefix is deliberately distinct
from the genuine-problem flags (``empty_chunk:``,
``empty_chunk_after_window_filter:``, ``chunk_accounting_mismatch``,
``no_rows_for_entity:``) so a reader never confuses a productive overlap with a
coverage gap.
"""

PREEXISTING_DUPLICATE_ROWS_FLAG_PREFIX = "duplicate_rows_already_stored"
"""Prefix of the flag reporting duplicates found in what was *already* stored.

Set when the prior parts themselves carry a logical row key more than once —
the residue of re-extractions made before cross-run dedup existed.  This run
cannot repair it: immutable parts are never rewritten or deleted, so the flag
names the count and points at the documented offline audit instead.
"""

CARRY_IN_DISAGREEMENT_FLAG_PREFIX = "carry_in_classification_disagreement"
"""Prefix of the flag raised when a suppressed duplicate disagrees about carry-in.

Row identity excludes :data:`CARRY_IN_FIELD` (it is derived from the chunk
start), so a duplicate whose carry-in classification differs from the stored
copy is still suppressed — nothing already stored is rewritten.  The
disagreement is loud rather than silent because it means the chunk boundaries
of two runs did not line up, which is worth knowing.
"""

PRIOR_PART_UNREADABLE_FLAG_PREFIX = "prior_part_unreadable"
"""Prefix of the flag naming a stored part that could not be indexed.

An unreadable part cannot contribute its keys, so rows it holds may be written
again by this run — duplication is the acceptable failure mode, losing
not-re-extractable history is not.
"""

CARRY_IN_FIELD = "carry_in"

ROW_IDENTITY_FIELDS: tuple[str, ...] = (
    "entity",
    "last_changed",
    "last_updated",
    "state",
    "attributes_json",
)
"""The ``RAW_ENUM_STATE`` columns that constitute a row's logical identity.

What Home Assistant recorded, with every piece of this-pull provenance removed:
``extraction_id`` (a fresh UUID per run), ``source_window_start`` (the chunk
query start) and :data:`CARRY_IN_FIELD` (derived from that same chunk start).
Used by :func:`row_identity` and recorded verbatim in
``params["row_identity"]["fields"]`` so a reader of a manifest can verify the
key without reading code.
"""

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
# Row identity — the key that makes re-extraction of an overlap free
# ---------------------------------------------------------------------------

MANIFESTS_DIRNAME = "manifests"
"""Mirrors the layout written by :func:`manifest.write_manifest`; read-only here."""

_IDENTITY_COLUMNS: tuple[str, ...] = (
    "entity",
    "state",
    "attributes_json",
    "last_changed",
    "last_updated",
    CARRY_IN_FIELD,
)
"""Columns of a stored part needed to rebuild its logical keys.

``extraction_id`` and ``source_window_start`` are deliberately *not* read: they
are run provenance and play no part in row identity.
"""

_UNIX_EPOCH = datetime(1970, 1, 1, tzinfo=UTC)


def to_epoch_us(dt: datetime) -> int:
    """Exact whole microseconds since the Unix epoch for an aware datetime.

    Integer arithmetic, not ``int(dt.timestamp() * 1e6)``: a float in this range
    carries ~0.1 µs of error and truncation can collapse two timestamps one
    microsecond apart into one key.  Identity must never over-dedup.
    """
    if dt.tzinfo is None:
        raise ValueError(f"naive datetime: {dt!r}")
    delta = dt.astimezone(UTC) - _UNIX_EPOCH
    return delta.days * 86_400_000_000 + delta.seconds * 1_000_000 + delta.microseconds


def row_identity(row: dict) -> tuple[str, int, int, str, str | None]:
    """The logical identity of an assembled HA row.

    The five ``RAW_ENUM_STATE`` columns that describe **what Home Assistant
    recorded**: ``entity``, ``last_changed``, ``last_updated``, ``state`` and
    ``attributes_json``.  ``extraction_id``, ``source_window_start`` and
    ``carry_in`` are excluded — see the module docstring, "Cross-run
    deduplication".  Timestamps are normalised to whole microseconds so a value
    read back from Parquet and the value parsed from the API hash identically.
    """
    return (
        row["entity"],
        to_epoch_us(row["last_changed"]),
        to_epoch_us(row["last_updated"]),
        row["state"],
        row["attributes_json"],
    )


def identity_digest(identity: tuple[str, int, int, str, str | None]) -> str:
    """SHA-256 of a field-tagged canonical JSON rendering of *identity*.

    Tagged rather than positional so no two distinct rows can collide by
    re-arranging fields, and digested rather than kept as tuples so the index is
    a fixed 64 hex characters per row regardless of attribute-blob size.
    """
    entity, last_changed_us, last_updated_us, state, attributes_json = identity
    canonical = json.dumps(
        {
            "entity": entity,
            "last_changed_us": last_changed_us,
            "last_updated_us": last_updated_us,
            "state": state,
            "attributes_json": attributes_json,
        },
        sort_keys=True,
        separators=(",", ":"),
        ensure_ascii=True,
    )
    return hashlib.sha256(canonical.encode("utf-8")).hexdigest()


@dataclass
class DedupOutcome:
    """What happened to one chunk's candidate rows against the store.

    Attributes:
        new_rows: Rows to write — genuinely new observations.
        duplicate_rows: Candidates already present in the store, suppressed.
        carry_in_disagreements: Suppressed duplicates whose ``carry_in`` differs
            from the copy already stored (see
            :data:`CARRY_IN_DISAGREEMENT_FLAG_PREFIX`).
    """

    new_rows: list[dict]
    duplicate_rows: int = 0
    carry_in_disagreements: int = 0


@dataclass
class StoredRowIndex:
    """Logical keys already present in the data directory, plus provenance.

    Built by :func:`load_stored_row_index` from the **part files on disk**, so a
    row that exists cannot be written twice — including a row left behind by an
    earlier run that failed before writing its manifest.  The index then grows
    as this run accepts rows, which also suppresses an intra-run repeat.

    Attributes:
        keys: digest → ``carry_in`` as stored, the first copy seen wins.
        parts_scanned: HA parts read (successfully or not).
        rows_read: Rows those parts carried.
        prior_distinct_keys: Distinct keys after loading, i.e. before this run.
        preexisting_duplicate_rows: ``rows_read - prior_distinct_keys``: the
            residue of overlaps pulled before cross-run dedup existed.
        unreadable_parts: ``"<part name>: <exception type>"`` for parts that
            could not be indexed.  Part names only — a filename carries kind,
            chunk window and content hash, no endpoint or credential.
        manifests_read: HA manifests found.
        parts_claimed_by_manifests: Parts a successful HA manifest recorded.
        orphan_part_names: Parts on disk no HA manifest claims.
        rows_added_this_run: Keys this run inserted (mirrors rows written).
    """

    keys: dict[str, bool]
    parts_scanned: int = 0
    rows_read: int = 0
    prior_distinct_keys: int = 0
    preexisting_duplicate_rows: int = 0
    unreadable_parts: list[str] = field(default_factory=list)
    manifests_read: int = 0
    parts_claimed_by_manifests: int = 0
    orphan_part_names: list[str] = field(default_factory=list)
    rows_added_this_run: int = 0

    @property
    def prior_coverage_established(self) -> bool:
        """False when the prior store could not be fully indexed."""
        return not self.unreadable_parts

    def split(self, rows: list[dict]) -> DedupOutcome:
        """Partition candidate rows into genuinely new and already-stored.

        A candidate whose digest is absent is new and is inserted into the index
        immediately, so a repeat later in the same run is recognised too.  A
        candidate whose digest is present is **not written**: the observation is
        already stored, and stored parts are immutable — never rewritten,
        deleted, or appended to.
        """
        new_rows: list[dict] = []
        duplicates = 0
        disagreements = 0
        for row in rows:
            digest = identity_digest(row_identity(row))
            stored_carry_in = self.keys.get(digest)
            if stored_carry_in is None:
                self.keys[digest] = bool(row[CARRY_IN_FIELD])
                self.rows_added_this_run += 1
                new_rows.append(row)
                continue
            duplicates += 1
            if bool(row[CARRY_IN_FIELD]) != stored_carry_in:
                disagreements += 1
        return DedupOutcome(
            new_rows=new_rows,
            duplicate_rows=duplicates,
            carry_in_disagreements=disagreements,
        )

    def as_dict(self) -> dict[str, object]:
        """Structured prior-coverage record for the manifest."""
        return {
            "parts_scanned": self.parts_scanned,
            "rows_read": self.rows_read,
            "distinct_keys": self.prior_distinct_keys,
            "duplicate_rows_already_stored": self.preexisting_duplicate_rows,
            "unreadable_parts": list(self.unreadable_parts),
            "manifests_read": self.manifests_read,
            "parts_claimed_by_manifests": self.parts_claimed_by_manifests,
            "orphan_parts": len(self.orphan_part_names),
            "orphan_part_names": list(self.orphan_part_names[:10]),
            "rows_added_by_this_run": self.rows_added_this_run,
            "coverage_established": self.prior_coverage_established,
        }


def _part_name(path: Path) -> str:
    """Basename of a part file — the only part identity ever written to a manifest."""
    return path.name


def _manifest_claimed_parts(root: Path, index: StoredRowIndex) -> set[str]:
    """Part names recorded by successful HA manifests, and manifest count.

    Provenance only: coverage comes from the parts themselves, so a manifest
    that is missing, unreadable or incomplete can never make this run skip rows
    it should write.
    """
    claimed: set[str] = set()
    manifests_dir = root / MANIFESTS_DIRNAME
    if not manifests_dir.is_dir():
        return claimed
    for path in sorted(manifests_dir.glob("*.json")):
        try:
            data = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            continue
        if not isinstance(data, dict) or data.get("source") != SOURCE:
            continue
        index.manifests_read += 1
        for entry in data.get("part_files") or []:
            if not isinstance(entry, dict):
                continue
            name = entry.get("name") or (
                _part_name(Path(entry["path"])) if entry.get("path") else None
            )
            if isinstance(name, str) and name.endswith(".parquet"):
                claimed.add(name)
    index.parts_claimed_by_manifests = len(claimed)
    return claimed


def load_stored_row_index(root: Path) -> StoredRowIndex:
    """Index every HA part already in *root*, keyed by logical row identity.

    Reads parts, not manifests: what is physically stored is what must not be
    stored twice.  Each part is read for the identity columns only, so memory
    stays bounded by row count rather than by attribute size (and the digests
    themselves are fixed width).

    A part that cannot be read or lacks the identity columns is named in
    :attr:`StoredRowIndex.unreadable_parts` and its keys are simply absent — the
    consequence is a possible duplicate, never a gap in what gets written.
    """
    index = StoredRowIndex(keys={})
    claimed = _manifest_claimed_parts(root, index)

    files = iter_parts(root, SOURCE)
    on_disk = {_part_name(p) for p in files}
    index.orphan_part_names = sorted(on_disk - claimed)
    index.parts_scanned = len(files)

    for path in files:
        name = _part_name(path)
        try:
            stored_columns = set(pq.read_schema(path).names)
        except Exception as exc:  # a damaged part must not block not-re-extractable history
            index.unreadable_parts.append(f"{name}: {type(exc).__name__}")
            continue
        missing = [c for c in _IDENTITY_COLUMNS if c not in stored_columns]
        if missing:
            index.unreadable_parts.append(f"{name}: missing identity columns")
            continue
        try:
            table = pq.read_table(path, columns=list(_IDENTITY_COLUMNS))
        except Exception as exc:  # a damaged part must not block not-re-extractable history
            index.unreadable_parts.append(f"{name}: {type(exc).__name__}")
            continue
        columns = [table.column(column).to_pylist() for column in _IDENTITY_COLUMNS]
        for entity, state, attributes_json, last_changed, last_updated, carry_in in zip(
            *columns, strict=True
        ):
            if entity is None or state is None or last_changed is None or last_updated is None:
                index.unreadable_parts.append(f"{name}: null identity column")
                break
            index.rows_read += 1
            digest = identity_digest(
                (
                    str(entity),
                    to_epoch_us(last_changed),
                    to_epoch_us(last_updated),
                    str(state),
                    None if attributes_json is None else str(attributes_json),
                )
            )
            index.keys.setdefault(digest, bool(carry_in))

    index.prior_distinct_keys = len(index.keys)
    index.preexisting_duplicate_rows = index.rows_read - index.prior_distinct_keys
    return index


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
    """What the recorder returned for one **declared** chunk, and what was kept.

    One record exists per declared chunk, created before the first request is
    attempted, so a chunk that yields nothing still shows up in the manifest
    accounting rather than vanishing from it.  The fields record only what the
    extractor can prove (was it requested, did the request succeed, how many
    raw rows came back, how many survived the carry-in and half-open-window
    filters, how many of those were already present in the store, how many were
    genuinely new and therefore written); *why* a chunk is empty is not asserted
    — see :data:`EMPTY_CHUNK_CAUSE_NOTE`.

    Attributes:
        index: 1-based position among the declared chunks.
        chunk_start: Inclusive start of the chunk's half-open window.
        chunk_end: Exclusive end of the chunk's half-open window.
        requested: Whether a history request was issued for this chunk.
        request_succeeded: Whether a well-formed response came back for it.
        raw_rows: Rows the response carried for the requested entities.
        kept_rows: Candidate rows after carry-in detection, the half-open window
            filter and intra-chunk dedup — i.e. rows this chunk *saw*.
        duplicate_rows: Of those, rows already present in the store from prior
            extractions, suppressed rather than re-written.  Expected on an
            intentional overlap, not an error.
        new_rows: Genuinely new observations, equal to the rows in this chunk's
            part file (0 when nothing new was found, so no part is written).
        carry_in_disagreements: Suppressed duplicates whose ``carry_in``
            classification differs from the copy already stored.
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
    duplicate_rows: int = 0
    new_rows: int = 0
    carry_in_disagreements: int = 0
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

    @property
    def fully_suppressed(self) -> bool:
        """True when every candidate row was already stored, so nothing was new.

        Such a chunk writes no part file but is **not** a coverage gap: the data
        it asked for is in the store, which is exactly what an overlapping
        re-pull is supposed to produce.
        """
        return self.request_succeeded and self.kept_rows > 0 and self.new_rows == 0

    def flags(self) -> list[str]:
        """Quality flags for this chunk; empty when it produced new rows.

        The window is always named, and the cause of an empty chunk is stated as
        undetermined rather than guessed.  A chunk carrying rows that all fell
        outside the half-open window gets its own prefix, because that much *is*
        distinguishable from a payload that was empty in the first place.

        Suppressed duplicates raise :data:`DUPLICATE_ROWS_FLAG_PREFIX`, an
        accounting disclosure of what was not re-written — expected and benign on
        an overlap — and a carry-in disagreement raises
        :data:`CARRY_IN_DISAGREEMENT_FLAG_PREFIX`, which is a real problem worth
        investigating.
        """
        if not self.request_succeeded:
            return []
        out: list[str] = []
        if self.kept_rows == 0:
            if self.raw_rows == 0:
                prefix = EMPTY_CHUNK_FLAG_PREFIX
                detail = "the response carried no rows for the requested entities"
            else:
                prefix = FILTERED_CHUNK_FLAG_PREFIX
                detail = (
                    f"the response carried {self.raw_rows} raw row(s), none of which fell "
                    "inside the half-open chunk window"
                )
            out.append(
                f"{prefix}:{self.window}: 0 rows kept, no part file written "
                f"({detail}); {EMPTY_CHUNK_CAUSE_NOTE}. "
                "HA recorder history is not re-extractable once purged, so this gap "
                "must be re-covered while the window is still available."
            )
        if self.duplicate_rows:
            out.append(
                f"{DUPLICATE_ROWS_FLAG_PREFIX}:{self.window}: "
                f"seen={self.kept_rows} already_present={self.duplicate_rows} "
                f"new={self.new_rows} rows_written={self.new_rows} - "
                f"{self.duplicate_rows} row(s) were already in the store from prior "
                "extraction(s) and were deliberately not re-written. Expected and "
                "benign on an intentional overlapping re-pull: each observation stays "
                "stored exactly once, nothing was overwritten or deleted, and no "
                "information was lost. Not a coverage gap."
            )
        if self.carry_in_disagreements:
            out.append(
                f"{CARRY_IN_DISAGREEMENT_FLAG_PREFIX}:{self.window}: "
                f"{self.carry_in_disagreements} suppressed duplicate row(s) whose "
                f"{CARRY_IN_FIELD} classification differs from the copy already stored, "
                "which means chunk boundaries did not line up between runs. The stored "
                "row is kept as-is (parts are immutable) and the incoming copy was not "
                "written; check the chunk width and window start used by these runs."
            )
        return out

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
            "duplicate_rows": self.duplicate_rows,
            "new_rows": self.new_rows,
            "rows_written": self.new_rows,
            "carry_in_disagreements": self.carry_in_disagreements,
            "empty": self.empty,
            "payload_empty": self.payload_empty,
            "fully_suppressed": self.fully_suppressed,
            "entities_in_response": list(self.entities_in_response),
            "part_file": self.part_file,
            "flags": self.flags(),
        }


def summarize_chunks(outcomes: list[ChunkOutcome], part_files: list[dict[str, object]]) -> dict:
    """Declared-vs-written accounting for the whole run.

    Both counts are always present so a consumer can compare them without
    inferring the declared count from the length of ``part_files``.  A chunk that
    found nothing new is netted out of ``mismatch`` the way a written chunk is:
    only a declared chunk that is neither written, nor empty, nor fully
    suppressed counts as a gap.
    """
    declared = len(outcomes)
    written = len(part_files)
    empty_windows = [o.window for o in outcomes if o.empty]
    suppressed_windows = [o.window for o in outcomes if o.fully_suppressed]
    duplicates = sum(o.duplicate_rows for o in outcomes)
    new_rows = sum(o.new_rows for o in outcomes)
    rows_in_parts = sum(int(entry["rows"]) for entry in part_files)
    return {
        "declared_chunks": declared,
        "requested_chunks": sum(1 for o in outcomes if o.requested),
        "succeeded_chunks": sum(1 for o in outcomes if o.request_succeeded),
        "chunks_with_rows": sum(1 for o in outcomes if o.kept_rows),
        "empty_chunks": len(empty_windows),
        "empty_chunk_windows": empty_windows,
        "payload_empty_chunks": sum(1 for o in outcomes if o.payload_empty),
        "parts_written": written,
        "raw_rows": sum(o.raw_rows for o in outcomes),
        "candidate_rows": sum(o.kept_rows for o in outcomes),
        "duplicate_rows_suppressed": duplicates,
        "new_rows": new_rows,
        "rows_written": rows_in_parts,
        "chunks_with_duplicates": sum(1 for o in outcomes if o.duplicate_rows),
        "fully_suppressed_chunks": len(suppressed_windows),
        "fully_suppressed_windows": suppressed_windows,
        "carry_in_disagreements": sum(o.carry_in_disagreements for o in outcomes),
        "mismatch": declared != written + len(suppressed_windows),
    }


def _mismatch_flags(accounting: dict) -> list[str]:
    """Flag making a declared/written disagreement visible without a diff."""
    if not accounting["mismatch"]:
        return []
    return [
        f"{CHUNK_ACCOUNTING_FLAG_PREFIX}: declared={accounting['declared_chunks']} "
        f"parts_written={accounting['parts_written']} "
        f"empty_chunks={accounting['empty_chunks']} "
        f"fully_suppressed={accounting['fully_suppressed_chunks']} "
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
        ``(entity, last_updated, state, attributes_json)``.  This is **within one
        chunk's payload only**; duplicates of rows already in the store from
        other runs or other chunks are handled by
        :func:`load_stored_row_index` / :meth:`StoredRowIndex.split`, keyed on
        :func:`row_identity`.

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

    Re-extracting an overlapping window is safe and cheap: candidate rows are
    matched against the rows already in the store by :func:`row_identity`, so
    duplicates are **not written** (stored parts are never rewritten or deleted)
    and the suppression is disclosed per chunk and in the manifest by
    :data:`DUPLICATE_ROWS_FLAG_PREFIX`.  A run whose window is already fully
    stored therefore writes zero parts and zero rows, and its manifest says so.

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
            # How re-extraction is reconciled against what is already stored.
            "row_identity": {
                "hash": "sha256(canonical JSON, field-tagged, sorted keys, ASCII)",
                "fields": list(ROW_IDENTITY_FIELDS),
                "excluded_provenance": ["extraction_id", CARRY_IN_FIELD, "source_window_start"],
                "coverage_source": (
                    "part files already in the data directory, not window arithmetic"
                ),
                "window_narrowing": "none — chunks stay a deterministic function of [start, end)",
            },
        },
        entities=list(entity_ids),
        source_window_start=iso_z(t_start),
        source_window_end=iso_z(t_end),
        started_at=started_at,
    )
    extraction_id = manifest.extraction_id

    # Prior coverage is read from the parts on disk *before* anything is fetched,
    # so a row that is already stored cannot be written twice by this run — even
    # if the run that stored it failed before writing a manifest.
    index = load_stored_row_index(root)
    if index.unreadable_parts:
        log.warning(
            "%d stored HA part(s) could not be indexed for dedup: %s — rows they "
            "hold may be written again by this run; duplication is the acceptable "
            "failure mode, a gap is not",
            len(index.unreadable_parts),
            ", ".join(index.unreadable_parts),
        )
    if index.orphan_part_names:
        log.info(
            "%d HA part(s) on disk are claimed by no manifest (left by a failed run): %s",
            len(index.orphan_part_names),
            ", ".join(index.orphan_part_names[:10]),
        )
    log.info(
        "prior HA store: parts=%d rows=%d distinct_keys=%d already_stored_duplicates=%d",
        index.parts_scanned,
        index.rows_read,
        index.prior_distinct_keys,
        index.preexisting_duplicate_rows,
    )

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

                # Cross-run/cross-chunk dedup: everything already present in the
                # store (including rows this very run wrote for an earlier chunk)
                # is left out of the part file instead of re-stored.
                dedup = index.split(chunk_rows)
                outcome.duplicate_rows = dedup.duplicate_rows
                outcome.carry_in_disagreements = dedup.carry_in_disagreements
                outcome.new_rows = len(dedup.new_rows)

                # Track which entities had rows
                for eid in entity_ids:
                    if any(r["entity"] == eid for r in chunk_rows):
                        entities_with_no_rows.discard(eid)

                log.info(
                    "chunk %d/%d [%s, %s): raw_rows=%d rows=%d already_present=%d "
                    "new=%d empty=%s fully_suppressed=%s entities=%s",
                    outcome.index,
                    len(outcomes),
                    iso_z(c_start),
                    iso_z(c_end),
                    outcome.raw_rows,
                    outcome.kept_rows,
                    outcome.duplicate_rows,
                    outcome.new_rows,
                    outcome.empty,
                    outcome.fully_suppressed,
                    ",".join(entity_ids),
                )

                quality_flags.extend(outcome.flags())
                if not chunk_rows:
                    continue  # empty chunk: accounted for in the ledger, no part file
                if not dedup.new_rows:
                    log.info(
                        "chunk %d/%d %s: all %d row(s) were already in the store — "
                        "nothing new to write (expected on an overlapping re-pull)",
                        outcome.index,
                        len(outcomes),
                        outcome.window,
                        outcome.duplicate_rows,
                    )
                    continue  # nothing new: no part file, and not a coverage gap

                all_rows.extend(dedup.new_rows)

                table = rows_to_table(dedup.new_rows, extraction_id)
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
                        "rows": outcome.new_rows,
                        "rows_seen": outcome.kept_rows,
                        "duplicate_rows_suppressed": outcome.duplicate_rows,
                        "new_rows": outcome.new_rows,
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
    rows_seen = accounting["candidate_rows"]
    rows_new = accounting["new_rows"]
    rows_suppressed = accounting["duplicate_rows_suppressed"]

    # Entities that had no rows across all chunks
    for eid in sorted(entities_with_no_rows):
        quality_flags.append(f"no_rows_for_entity:{eid}")

    quality_flags.extend(_mismatch_flags(accounting))

    # Duplicate accounting: a disclosure of what was *not* re-written, phrased so
    # it can never be mistaken for a coverage problem, and loud enough that a
    # re-extraction can never again read as a clean, fully productive run.
    if rows_suppressed:
        quality_flags.append(
            f"{DUPLICATE_ROWS_FLAG_PREFIX}:total: "
            f"rows_seen={rows_seen} already_present={rows_suppressed} new={rows_new} "
            f"rows_written={accounting['rows_written']} - {rows_suppressed} row(s) across "
            f"{accounting['chunks_with_duplicates']} chunk(s) were already stored by prior "
            "HA extraction(s) and were not re-written, so this run added no duplicate of an "
            "observation already in the immutable store. Expected and benign on an "
            "intentional overlapping re-pull, not a failure: nothing was overwritten, "
            "nothing was deleted, no information was lost, and no declared chunk was left "
            "uncovered. "
            f"fully_suppressed_chunks={accounting['fully_suppressed_chunks']} "
            f"({'; '.join(accounting['fully_suppressed_windows']) or 'none'}). "
            f"Prior store: parts={index.parts_scanned} rows={index.rows_read} "
            f"distinct_keys={index.prior_distinct_keys}. "
            "See params.row_accounting and params.chunk_outcomes for the per-chunk ledger."
        )
        log.info(
            "dedup: %d of %d candidate row(s) were already present in the store and were "
            "not re-written (%d new row(s), %d fully suppressed chunk(s)) — expected on an "
            "overlapping re-pull",
            rows_suppressed,
            rows_seen,
            rows_new,
            accounting["fully_suppressed_chunks"],
        )
    if accounting["carry_in_disagreements"]:
        log.warning(
            "%d suppressed duplicate(s) disagreed about the carry_in classification with "
            "the copy already stored — chunk boundaries differed between runs; see the "
            "%s flags",
            accounting["carry_in_disagreements"],
            CARRY_IN_DISAGREEMENT_FLAG_PREFIX,
        )
    if index.preexisting_duplicate_rows:
        quality_flags.append(
            f"{PREEXISTING_DUPLICATE_ROWS_FLAG_PREFIX}:{index.preexisting_duplicate_rows}: "
            f"{index.rows_read} row(s) in {index.parts_scanned} stored part(s) share "
            f"{index.prior_distinct_keys} distinct logical key(s), i.e. the store already "
            "held these duplicates before this run. They pre-date cross-run dedup. This run "
            "cannot repair them and will not try to: stored parts are immutable, never "
            "rewritten, deleted or appended to. Auditable offline; see the energy-baseline "
            "README, 'Overlapping re-extraction'."
        )
        log.warning(
            "the store already holds %d duplicate HA row(s) from extractions made before "
            "cross-run dedup; this run writes none of them again",
            index.preexisting_duplicate_rows,
        )
    for part in index.unreadable_parts:
        quality_flags.append(f"{PRIOR_PART_UNREADABLE_FLAG_PREFIX}:{part}")

    total_rows = len(all_rows)
    if rows_seen == 0:
        quality_flags.append("no_rows_extracted:all")
        log.warning(
            "no rows found for %s in [%s, %s) — coverage is NOT established",
            ",".join(entity_ids),
            iso_z(t_start),
            iso_z(t_end),
        )
    elif total_rows == 0:
        log.info(
            "no new rows for %s in [%s, %s): all %d candidate row(s) were already stored — "
            "coverage IS established by the store, this run added nothing",
            ",".join(entity_ids),
            iso_z(t_start),
            iso_z(t_end),
            rows_seen,
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
    manifest.params["row_accounting"] = {
        "raw_rows": accounting["raw_rows"],
        "rows_seen": rows_seen,
        "duplicate_rows_suppressed": rows_suppressed,
        "new_rows": rows_new,
        "rows_written": accounting["rows_written"],
        "chunks_with_duplicates": accounting["chunks_with_duplicates"],
        "fully_suppressed_chunks": accounting["fully_suppressed_chunks"],
        "fully_suppressed_windows": accounting["fully_suppressed_windows"],
        "carry_in_disagreements": accounting["carry_in_disagreements"],
    }
    manifest.params["prior_coverage"] = index.as_dict()
    manifest.coverage_notes = (
        f"{accounting['declared_chunks']} chunk(s) of <= {chunk_seconds}s covering "
        f"[{iso_z(t_start)}, {iso_z(t_end)}) via HA history API; "
        f"{total_rows} row(s) in {accounting['parts_written']} part file(s). "
        f"{agreement}"
        f"Row accounting: raw_rows={accounting['raw_rows']} rows_seen={rows_seen} "
        f"already_present={rows_suppressed} new={rows_new} "
        f"rows_written={accounting['rows_written']} "
        f"fully_suppressed_chunks={accounting['fully_suppressed_chunks']} — duplicates of "
        "observations already in the immutable store are not re-written, which is the "
        "expected and benign outcome of an intentionally overlapping re-pull and is "
        "disclosed by the duplicate_rows_suppressed flags rather than presented as a "
        "clean, fully productive run. Prior coverage was read from "
        f"{index.parts_scanned} stored part(s) ({index.prior_distinct_keys} distinct "
        "key(s)), not from window arithmetic, and the requested window was not narrowed. "
        "An empty chunk is never treated as continuity, and its cause is not "
        "inferred: no state transitions, a recorder purge and a degenerate empty "
        "response are indistinguishable from the API alone, so each zero-row chunk "
        "is named in quality_flags (empty_chunk / empty_chunk_after_window_filter) "
        "and recorded per chunk in params.chunk_outcomes. Empty entity responses "
        "are recorded as no_rows_for_entity flags and never treated as continuity."
    )

    path = write_manifest(root, manifest)
    log.info(
        "wrote manifest %s (extraction_id=%s, rows=%d, parts=%d, declared_chunks=%d, "
        "already_present=%d)",
        path,
        extraction_id,
        total_rows,
        accounting["parts_written"],
        accounting["declared_chunks"],
        rows_suppressed,
    )
    return path
