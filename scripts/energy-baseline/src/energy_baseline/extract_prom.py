"""Prometheus extraction — ingest raw numeric samples from ``prometheus-long-term``.

Reads the numeric archive (1-minute scrape interval, retention 9999d) into
immutable Parquet parts under ``raw/prometheus/v1/parts/`` and records one
extraction manifest per successful run.

Why ``query_range`` is not used
------------------------------
``/api/v1/query_range`` evaluates expressions on a fixed *step* grid and fills
every evaluation point from the most recent sample within Prometheus's
5-minute lookback delta.  That fabricates coverage (invented rows at grid
offsets that were never scraped) and destroys the real sample timestamps the
energy pipeline depends on — see ADR-0003 §"Prometheus data characteristics"
("use raw range vectors in bounded chunks").

Instead this module issues **instant queries whose selector is a range
vector**, which returns the actual raw samples in a left-open / right-closed
window:

    GET /api/v1/query?query=<metric>{entity="<id>"}[<window>]&time=<chunk_end>

Chunking and the left-open edge
-------------------------------
The requested interval is ``[start, end)`` (half-open).  It is split into
chunks ``[c_start, c_end)``.  For each chunk we query at ``time = c_end`` with
``window = ceil(c_end - c_start) + RANGE_PADDING_SECONDS`` seconds, then drop
any returned sample outside ``[c_start, c_end)``.  The extra second is
**required**: range vectors are left-open, so a window exactly as wide as the
chunk would silently drop a sample whose timestamp lands precisely on
``c_start``.  The padding only widens the query; the ``[c_start, c_end)``
filter keeps the chunks a strict partition of time, so nothing is duplicated
either (a defensive cross-chunk watermark check backs this up).

Series identity
---------------
``series_key = sha256(canonical JSON of the label set minus volatile labels)``.
Only ``instance`` (and ``pod_ip``, which this archive never exports) is
excluded — every other label is kept, including ``pod``, ``job``, ``service``,
``namespace``, ``endpoint`` and ``__name__``.  The price metric has 281
lifetime series of which ``instance`` accounts for 238 distinct pod IPs, while
``pod``/``job`` are stable and mark two deployment eras (``job=home-assistant``
and ``job=home-assistant-hass``).  Collapsing those eras into one identity
would corrupt counter stitching later, so they stay distinct.  ``instance`` is
kept as the ``scrape_instance`` column and the **full** original label set
(including ``instance``) is stored in ``labels_json``.

Note: the module docstring in ``schemas.py`` lists a broader volatile set
(``pod``, ``pod_template_hash``, ``node``).  This extractor intentionally
narrows that to ``instance``/``pod_ip`` per the reasoning above.

Empty results are never proof of continuity
-------------------------------------------
Three empty cases are distinguished and recorded per entity in the manifest:

``a`` HTTP success with **zero series** → ``no_series_chunks:<entity>``
``b`` series present but **zero samples inside the chunk window** →
    ``series_without_samples_chunks:<entity>``
``c`` **query error** (HTTP error, ``status: "error"``, malformed payload) →
    raises, and no success manifest is written.

Additionally, identical ``(labels_json, ts)`` samples (same full label set,
same timestamp) are deduplicated; when such a sample carries **different**
values, both rows are kept and the manifest flags ``overlapping_target_samples``.

Other guarantees
----------------
* Naive datetime strings are rejected; ISO-8601 is required and normalised to
  UTC.  Half-open ``[start, end)`` semantics throughout.
* Entity IDs are escaped into a quoted PromQL matcher; metric names and
  entity IDs are charset-validated so a CLI-supplied ID cannot inject a query.
* Non-finite values (``NaN``/``±Inf``) abort the run by default (never written
  silently); ``allow_non_finite=True`` drops them and records a quality flag.
* Bounded exponential-backoff retry on 429/5xx/transport timeouts only —
  4xx (including auth errors) and PromQL errors are raised immediately.
* Row order is deterministic (``series_key``, then ``ts``, then
  ``scrape_instance``), so identical inputs produce byte-identical parts.
* Dedup is keyed on ``(labels_json, ts)`` — the **full** label set including
  ``instance``, so samples from different scrape targets are preserved and the
  counter stitcher reconciles them.  An identical full-series sample appearing
  twice (the SD race) is deduped; a conflicting value at the same timestamp is
  retained and flagged as ``overlapping_target_samples``.
* Parts are written with :func:`paths.write_part_atomic` (no-clobber,
  content-addressed).  A manifest is written **only** after every chunk
  succeeds, so orphaned parts from a failed run are ignored by consumers that
  resolve coverage from manifests.
* Memory stays bounded: one chunk is materialised at a time, and the only
  cross-chunk state is a per-series watermark plus small counters.
"""

from __future__ import annotations

import hashlib
import json
import logging
import math
import os
import re
import time
from collections.abc import Iterable, Iterator
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
    parts_dir,
    resolve_data_dir,
    write_part_atomic,
)
from energy_baseline.schemas import RAW_SAMPLE, validate_table

log = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# Constants
# ---------------------------------------------------------------------------

SOURCE = "prometheus"
"""Source name used for the parts directory and the manifest ``source`` field."""

PART_KIND = "prom-samples"
"""``kind`` component of part filenames for this extractor."""

BASE_URL_ENV_VAR = "ENERGY_BASELINE_PROMETHEUS_URL"

DEFAULT_BASE_URL = "https://prometheus-long-term.internal.grigri.cloud"
"""The archive is publicly reachable inside the cluster and needs no auth."""

ARCHIVE_START_UTC = datetime(2023, 11, 7, 17, 25, tzinfo=UTC)
"""Oldest data in ``prometheus-long-term`` (verified against the live server)."""

DEFAULT_CHUNK_SECONDS = 86400
"""Default chunk width: 24h, i.e. ~1440 samples per entity per query."""

RANGE_PADDING_SECONDS = 1
"""Extra second added to the range vector to defeat left-open edge loss."""

VOLATILE_IDENTITY_LABELS = frozenset({"instance", "pod_ip"})
"""Labels removed from ``series_key``; still retained in ``labels_json``."""

QUERY_PATH = "/api/v1/query"

_TIMEOUT = httpx.Timeout(60.0, connect=10.0)
_BACKOFF_BASE_SECONDS = 0.5
_BACKOFF_MAX_SECONDS = 30.0

# ---------------------------------------------------------------------------
# Entity registry (verified against the live archive on 2026-10-23)
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class EntitySpec:
    """A Prometheus-backed numeric entity: which metric carries it, its unit."""

    entity_id: str
    metric: str
    unit: str


ENTITY_SPECS: dict[str, EntitySpec] = {
    spec.entity_id: spec
    for spec in (
        EntitySpec(
            "sensor.shellyem_main_channel_1_energy",
            "hass_sensor_energy_kwh",
            "kWh",
        ),
        EntitySpec(
            "sensor.shellyem_main_channel_1_energy_returned",
            "hass_sensor_energy_kwh",
            "kWh",
        ),
        EntitySpec(
            "sensor.shellyem_main_channel_1_power",
            "hass_sensor_power_w",
            "W",
        ),
        EntitySpec(
            "sensor.esios_pvpc",
            "hass_sensor_unit_u0x20ac_per_kwh",
            "EUR/kWh",
        ),
        # Dead duplicate of sensor.esios_pvpc with 11 lifetime samples.  It is
        # *not* in DEFAULT_ENTITY_IDS and must never be merged into the price
        # series; it stays in the registry only so it can be audited on
        # request, and any explicit request for it is flagged in the manifest.
        EntitySpec(
            "sensor.esios_pvpc_2",
            "hass_sensor_unit_u0x20ac_per_kwh",
            "EUR/kWh",
        ),
    )
}

DEFAULT_ENTITY_IDS: tuple[str, ...] = (
    "sensor.shellyem_main_channel_1_energy",
    "sensor.shellyem_main_channel_1_energy_returned",
    "sensor.shellyem_main_channel_1_power",
    "sensor.esios_pvpc",
)

DEAD_DUPLICATE_ENTITY_IDS = frozenset({"sensor.esios_pvpc_2"})

# ---------------------------------------------------------------------------
# Exceptions
# ---------------------------------------------------------------------------


class PrometheusExtractionError(RuntimeError):
    """Base class for extraction failures (no success manifest is written)."""


class PrometheusHTTPError(PrometheusExtractionError):
    """Non-retryable HTTP failure, or a retryable one after exhausting retries."""

    def __init__(self, message: str, *, status_code: int | None = None) -> None:
        super().__init__(message)
        self.status_code = status_code


class PrometheusQueryError(PrometheusExtractionError):
    """``status: "error"`` from the API, or a payload that violates the contract."""


class NonFiniteSampleError(PrometheusExtractionError):
    """A ``NaN``/``±Inf`` sample was returned and ``allow_non_finite`` is false."""


# ---------------------------------------------------------------------------
# Pure helpers
# ---------------------------------------------------------------------------

_ENTITY_ID_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9_.:@\-]*$")
_METRIC_RE = re.compile(r"^[a-zA-Z_:][a-zA-Z0-9_:]*$")
_UNIT_RE = re.compile(r"^[A-Za-z0-9_%/\-.*+ ]+$")
_HASH8_RE = re.compile(r"[0-9a-f]{8}")


def _canonical_json(labels: dict[str, str]) -> str:
    """Canonical JSON for a label set: sorted keys, no whitespace, ASCII-only."""
    return json.dumps(labels, sort_keys=True, separators=(",", ":"), ensure_ascii=True)


def canonical_labels_json(labels: dict[str, str]) -> str:
    """Full original label set as canonical JSON (for the ``labels_json`` column)."""
    return _canonical_json(dict(labels))


def compute_series_key(labels: dict[str, str]) -> str:
    """SHA-256 hex of the canonical JSON label set minus volatile identity labels.

    ``instance`` and ``pod_ip`` are dropped; every other label (including
    ``pod``, ``job``, ``service``, ``__name__``) is part of the identity.
    """
    identity = {k: v for k, v in labels.items() if k not in VOLATILE_IDENTITY_LABELS}
    return hashlib.sha256(_canonical_json(identity).encode("utf-8")).hexdigest()


def iso_z(dt: datetime) -> str:
    """Render an aware datetime as ISO-8601 UTC with a ``Z`` suffix."""
    if dt.tzinfo is None:
        raise ValueError(f"naive datetime: {dt!r}")
    return dt.astimezone(UTC).strftime("%Y-%m-%dT%H:%M:%S") + _fraction(dt) + "Z"


def _fraction(dt: datetime) -> str:
    return f".{dt.microsecond:06d}".rstrip("0") if dt.microsecond else ""


def parse_utc(value: str, *, name: str) -> datetime:
    """Parse an ISO-8601 timestamp, requiring an explicit offset, and make it UTC.

    Raises:
        ValueError: *value* is not ISO-8601, or is naive (no offset/``Z``).
    """
    if isinstance(value, datetime):  # defensive: still enforce the naive rule
        value = iso_z(value) if value.tzinfo else ""
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
            "'Z' suffix is required (half-open [start, end) semantics are absolute)"
        )
    return dt.astimezone(UTC)


def to_micros(dt: datetime) -> int:
    """Whole microseconds since the epoch for an aware datetime."""
    if dt.tzinfo is None:
        raise ValueError(f"naive datetime: {dt!r}")
    delta = dt.astimezone(UTC) - datetime(1970, 1, 1, tzinfo=UTC)
    return delta.days * 86_400_000_000 + delta.seconds * 1_000_000 + delta.microseconds


def from_micros(us: int) -> datetime:
    """Aware UTC datetime from whole microseconds since the epoch."""
    return datetime(1970, 1, 1, tzinfo=UTC) + timedelta(microseconds=us)


def format_epoch_time(us: int) -> str:
    """Format micros-since-epoch as the ``time`` query parameter (fixed 6 decimals)."""
    secs, micro = divmod(int(us), 1_000_000)
    return f"{secs}.{micro:06d}"


def parse_epoch_time(raw: float | str) -> int:
    """Convert a Prometheus sample timestamp to whole micros since the epoch.

    Raises:
        PrometheusQueryError: non-numeric, or non-finite, timestamp.
    """
    try:
        seconds = float(raw)
    except (TypeError, ValueError) as exc:
        raise PrometheusQueryError(f"sample timestamp is not numeric: {raw!r}") from exc
    if not math.isfinite(seconds):
        raise PrometheusQueryError(f"sample timestamp is not finite: {raw!r}")
    return int(round(seconds * 1_000_000))


def escape_promql_string(value: str) -> str:
    """Escape a string for use inside a double-quoted PromQL matcher value."""
    if re.search(r"[\x00-\x1f\x7f]", value):
        raise ValueError(f"matcher value contains control characters: {value!r}")
    return value.replace("\\", "\\\\").replace('"', '\\"')


def resolve_base_url(base_url: str | None = None) -> str:
    """``base_url`` arg > ``ENERGY_BASELINE_PROMETHEUS_URL`` env > live default.

    Rejects non-HTTP schemes and embedded credentials (the archive is
    unauthenticated, so a URL with userinfo is a misconfiguration).
    """
    url = base_url or os.environ.get(BASE_URL_ENV_VAR) or DEFAULT_BASE_URL
    url = url.strip().rstrip("/")
    parsed = httpx.URL(url)
    if parsed.scheme not in ("http", "https"):
        raise ValueError(f"prometheus base_url must be http(s), got {url!r}")
    if not parsed.host:
        raise ValueError(f"prometheus base_url has no host: {url!r}")
    if parsed.username or parsed.password:
        raise ValueError(
            "prometheus base_url must not embed credentials (archive is unauthenticated)"
        )
    return url


def resolve_entity_specs(entities: Iterable[str] | None) -> tuple[EntitySpec, ...]:
    """Resolve requested entity IDs (or the defaults) into :class:`EntitySpec`.

    Accepts bare entity IDs, which must exist in :data:`ENTITY_SPECS`, or an
    explicit ``entity:metric:unit`` triple for entities not yet registered —
    never a guessed metric.  Order is preserved and duplicates collapse.

    Raises:
        ValueError: unknown entity without an explicit triple, or a malformed
            / disallowed entity, metric or unit.
    """
    requested = DEFAULT_ENTITY_IDS if not entities else tuple(str(e).strip() for e in entities)
    specs: dict[str, EntitySpec] = {}
    for raw in requested:
        if not raw:
            continue
        if ":" in raw:
            parts = raw.split(":")
            if len(parts) != 3 or not all(parts):
                raise ValueError(
                    f"entity spec {raw!r} must be 'entity:metric:unit' "
                    "(three colon-separated parts)"
                )
            entity_id, metric, unit = parts
            spec = EntitySpec(entity_id, metric, unit)
        elif raw in ENTITY_SPECS:
            spec = ENTITY_SPECS[raw]
        else:
            raise ValueError(
                f"no metric/unit mapping for entity {raw!r}: either add it to "
                f"ENTITY_SPECS or pass 'entity:metric:unit' explicitly "
                f"(known: {', '.join(sorted(ENTITY_SPECS))})"
            )
        if not _ENTITY_ID_RE.match(spec.entity_id):
            raise ValueError(f"entity id {spec.entity_id!r} contains unsupported characters")
        if not _METRIC_RE.match(spec.metric):
            raise ValueError(f"metric name {spec.metric!r} is not a valid Prometheus metric name")
        if not _UNIT_RE.match(spec.unit):
            raise ValueError(f"unit {spec.unit!r} contains unsupported characters")
        specs.setdefault(spec.entity_id, spec)
    if not specs:
        raise ValueError("entity list resolved to empty — nothing to extract")
    return tuple(specs.values())


def build_range_query(spec: EntitySpec, window_seconds: int) -> str:
    """Range-vector selector for one entity: ``metric{entity="id"}[<window>s]``."""
    return f'{spec.metric}{{entity="{escape_promql_string(spec.entity_id)}"}}[{window_seconds}s]'


def range_window_seconds(c_start: datetime, c_end: datetime) -> int:
    """Range-vector width covering ``[c_start, c_end)`` plus the padding second."""
    span = (c_end - c_start).total_seconds()
    if span <= 0:
        raise ValueError(f"chunk window must be positive, got {span}s")
    return int(math.ceil(span)) + RANGE_PADDING_SECONDS


def iter_chunks(
    start: datetime, end: datetime, chunk_seconds: int | float
) -> Iterator[tuple[datetime, datetime]]:
    """Yield ``[c_start, c_end)`` chunks partitioning ``[start, end)`` exactly.

    Boundaries are computed from ``start`` (``start + k * chunk_seconds``) so
    fractional widths cannot accumulate drift and the last chunk simply clips
    to ``end``.
    """
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


# ---------------------------------------------------------------------------
# HTTP query layer with bounded retry
# ---------------------------------------------------------------------------


def _is_retryable_status(status_code: int) -> bool:
    return status_code == 429 or status_code >= 500


def query_range_vector(
    client: httpx.Client,
    *,
    base_url: str,
    query: str,
    time_us: int,
    max_retries: int = 3,
) -> list[dict]:
    """One instant query with a range-vector selector; returns the raw matrix.

    Retries transport errors and 429/5xx with exponential backoff, at most
    *max_retries* additional attempts.  Other HTTP errors, ``status: "error"``
    payloads and contract violations raise immediately without retrying.

    Returns:
        The ``data.result`` list (possibly empty) of ``{"metric": {...},
        "values": [[ts, "value"], ...]}`` entries.
    """
    url = f"{base_url}{QUERY_PATH}"
    params = {"query": query, "time": format_epoch_time(time_us)}
    attempts = max(0, int(max_retries)) + 1
    last_error: Exception | None = None

    for attempt in range(attempts):
        if attempt:
            delay = min(_BACKOFF_BASE_SECONDS * 2 ** (attempt - 1), _BACKOFF_MAX_SECONDS)
            log.warning(
                "prometheus query retry %d/%d in %.1fs (query=%r): %s",
                attempt,
                attempts - 1,
                delay,
                query,
                last_error,
            )
            time.sleep(delay)
        try:
            response = client.get(url, params=params)
        except httpx.TransportError as exc:  # timeouts, connection failures
            last_error = exc
            continue

        if _is_retryable_status(response.status_code):
            last_error = PrometheusHTTPError(
                f"prometheus returned HTTP {response.status_code} (query={query!r}): "
                f"{response.text[:500]}",
                status_code=response.status_code,
            )
            continue
        if response.status_code >= 400:
            raise PrometheusHTTPError(
                f"prometheus returned HTTP {response.status_code} (not retried, query={query!r}): "
                f"{response.text[:500]}",
                status_code=response.status_code,
            )

        try:
            payload = response.json()
        except ValueError as exc:
            raise PrometheusQueryError(
                f"prometheus response is not JSON (query={query!r}): {response.text[:500]}"
            ) from exc
        if not isinstance(payload, dict):
            raise PrometheusQueryError(f"prometheus response is not an object (query={query!r})")
        if payload.get("status") != "success":
            raise PrometheusQueryError(
                f"prometheus query failed (status={payload.get('status')!r}, "
                f"errorType={payload.get('errorType')!r}, query={query!r}): "
                f"{payload.get('error')!r}"
            )
        data = payload.get("data")
        if not isinstance(data, dict):
            raise PrometheusQueryError(
                f"prometheus response has no 'data' object (query={query!r})"
            )
        if data.get("resultType") != "matrix":
            raise PrometheusQueryError(
                f"expected resultType='matrix' for range-vector selector, got "
                f"{data.get('resultType')!r} (query={query!r})"
            )
        result = data.get("result")
        if result is None:
            result = []
        if not isinstance(result, list):
            raise PrometheusQueryError(f"'data.result' is not a list (query={query!r})")
        return result

    raise PrometheusHTTPError(
        f"gave up after {attempts} attempt(s) querying prometheus (query={query!r}): {last_error}"
    )


# ---------------------------------------------------------------------------
# Row assembly
# ---------------------------------------------------------------------------


@dataclass(frozen=True, order=True)
class _Row:
    """One raw sample, ordered so that sorting is deterministic."""

    series_key: str
    ts_us: int
    scrape_instance: str
    entity: str
    metric: str
    unit: str
    value: float
    labels_json: str

    @property
    def dedup_key(self) -> tuple[str, int]:
        """Logical identity: one row per ``(labels_json, ts)``.

        The **full** label set (including ``instance``) is the dedup key so that
        observations from different scrape targets at the same timestamp are
        preserved — collapsing them would silently discard raw data.  Only
        truly identical series (same full labels, same timestamp) are deduped.
        Value conflicts (same labels + same ts, different value) are retained
        and flagged as ``overlapping_target_samples`` for the stitch step.
        """
        return (self.labels_json, self.ts_us)


@dataclass
class _EntityStats:
    """Per-entity counters used for manifest flags and INFO logging."""

    spec: EntitySpec
    samples: int = 0
    series_keys: set[str] = field(default_factory=set)
    first_us: int | None = None
    last_us: int | None = None
    chunks: int = 0
    chunks_no_series: int = 0
    chunks_no_samples: int = 0
    samples_outside_window: int = 0
    duplicates: int = 0
    boundary_duplicates: int = 0
    non_finite: int = 0
    overlapping_target_samples: int = 0

    @property
    def entity_id(self) -> str:
        return self.spec.entity_id

    def record_kept(self, row: _Row) -> None:
        self.samples += 1
        self.series_keys.add(row.series_key)
        self.first_us = row.ts_us if self.first_us is None else min(self.first_us, row.ts_us)
        self.last_us = row.ts_us if self.last_us is None else max(self.last_us, row.ts_us)

    def flags(self, chunks_total: int) -> list[str]:
        """Manifest quality flags describing exactly what was empty, and why."""
        e = self.entity_id
        out: list[str] = []
        if self.samples == 0:
            out.append(f"no_samples_extracted:{e}")
        if self.chunks_no_series:
            out.append(f"no_series_chunks:{e}:{self.chunks_no_series}/{chunks_total}")
        if self.chunks_no_samples:
            out.append(f"series_without_samples_chunks:{e}:{self.chunks_no_samples}/{chunks_total}")
        if self.duplicates:
            out.append(f"duplicate_samples_dropped:{e}:{self.duplicates}")
        if self.boundary_duplicates:
            out.append(f"boundary_duplicates_dropped:{e}:{self.boundary_duplicates}")
        if self.non_finite:
            out.append(f"non_finite_samples_dropped:{e}:{self.non_finite}")
        if self.overlapping_target_samples:
            out.append(f"overlapping_target_samples:{e}:{self.overlapping_target_samples}")
        if e in DEAD_DUPLICATE_ENTITY_IDS:
            out.append(f"dead_duplicate_entity:{e}")
        return out

    def as_dict(self, chunks_total: int) -> dict[str, object]:
        """Structured audit summary (per-series completeness, ADR-0003)."""
        return {
            "entity": self.entity_id,
            "metric": self.spec.metric,
            "unit": self.spec.unit,
            "samples": self.samples,
            "series": len(self.series_keys),
            "first_ts": from_micros(self.first_us).isoformat()
            if self.first_us is not None
            else None,
            "last_ts": from_micros(self.last_us).isoformat() if self.last_us is not None else None,
            "chunks": chunks_total,
            "chunks_no_series": self.chunks_no_series,
            "chunks_no_samples": self.chunks_no_samples,
            "samples_outside_window": self.samples_outside_window,
            "duplicates_dropped": self.duplicates + self.boundary_duplicates,
            "non_finite_dropped": self.non_finite,
            "overlapping_target_samples": self.overlapping_target_samples,
        }


def _coerce_value(
    raw: object,
    *,
    query: str,
    labels_json: str,
    ts_us: int,
    allow_non_finite: bool,
) -> float | None:
    """Parse a Prometheus sample value; ``None`` means "drop and flag".

    Raises:
        PrometheusQueryError: the value is not numeric at all.
        NonFiniteSampleError: ``NaN``/``±Inf`` and *allow_non_finite* is false.
    """
    try:
        value = float(raw)  # Prometheus always sends a JSON string
    except (TypeError, ValueError) as exc:
        raise PrometheusQueryError(
            f"sample value is not numeric: {raw!r} (query={query!r}, labels={labels_json}, "
            f"ts={format_epoch_time(ts_us)})"
        ) from exc
    if math.isfinite(value):
        return value
    detail = (
        f"non-finite sample value {raw!r} for query={query!r} at "
        f"ts={iso_z(from_micros(ts_us))} labels={labels_json}"
    )
    if not allow_non_finite:
        log.error("%s", detail)
        raise NonFiniteSampleError(
            f"{detail} — refusing to write it silently; re-run with "
            "allow_non_finite=True to drop such samples and record a quality flag"
        )
    log.warning("%s — dropped and recorded as a quality flag", detail)
    return None


def matrix_to_rows(
    matrix: list[dict],
    *,
    spec: EntitySpec,
    stats: _EntityStats,
    query: str,
    lo_us: int,
    hi_us: int,
    allow_non_finite: bool = False,
) -> list[_Row]:
    """Flatten a matrix response into rows inside the half-open ``[lo_us, hi_us)``.

    Records case (a) *no series* / case (b) *series without in-window samples*,
    counts samples dropped by the half-open filter, and counts non-finite values
    dropped when ``allow_non_finite`` is set.

    Raises:
        PrometheusQueryError: the response violates the matrix contract.
        NonFiniteSampleError: a ``NaN``/``±Inf`` value was returned and
            ``allow_non_finite`` is false.
    """
    rows: list[_Row] = []
    for entry in matrix:
        if not isinstance(entry, dict):
            raise PrometheusQueryError(
                f"matrix entry is not an object: {entry!r} (query={query!r})"
            )
        labels = entry.get("metric")
        if not isinstance(labels, dict):
            raise PrometheusQueryError(
                f"matrix entry has no label map: {entry!r} (query={query!r})"
            )
        clean = {str(k): str(v) for k, v in labels.items()}
        if clean.get("entity", spec.entity_id) != spec.entity_id:
            raise PrometheusQueryError(
                f"series labels contradict the requested entity {spec.entity_id!r}: {clean!r}"
            )
        series_key = compute_series_key(clean)
        labels_json = canonical_labels_json(clean)
        scrape_instance = clean.get("instance", "")
        values = entry.get("values")
        if values is None:
            values = []
        if not isinstance(values, list):
            raise PrometheusQueryError(f"'values' is not a list for series {clean!r}")
        for pair in values:
            if not isinstance(pair, (list, tuple)) or len(pair) != 2:
                raise PrometheusQueryError(
                    f"sample is not a [timestamp, value] pair: {pair!r} (series={clean!r})"
                )
            ts_us = parse_epoch_time(pair[0])
            if ts_us < lo_us or ts_us >= hi_us:
                stats.samples_outside_window += 1
                continue
            value = _coerce_value(
                pair[1],
                query=query,
                labels_json=labels_json,
                ts_us=ts_us,
                allow_non_finite=allow_non_finite,
            )
            if value is None:
                stats.non_finite += 1
                continue
            rows.append(
                _Row(
                    series_key=series_key,
                    ts_us=ts_us,
                    scrape_instance=scrape_instance,
                    entity=spec.entity_id,
                    metric=spec.metric,
                    unit=spec.unit,
                    value=value,
                    labels_json=labels_json,
                )
            )
    if not matrix:
        stats.chunks_no_series += 1
    elif not rows:
        stats.chunks_no_samples += 1
    return rows


def dedupe_rows(
    rows: list[_Row],
    stats_by_entity: dict[str, _EntityStats],
    watermark: dict[str, int],
) -> list[_Row]:
    """Drop repeated ``(labels_json, ts)`` samples and stale boundary rows.

    * Within a chunk: one row per **full** label set per timestamp.  Two targets
      whose labels differ in any label (including ``instance``) produce distinct
      rows — collapsing them would silently discard raw data.  A truly identical
      sample (same full labels, same timestamp, same value) is deduped.  When
      the same full series reports the same timestamp with *different* values the
      conflict is retained and flagged as ``overlapping_target_samples``.
    * Across chunks: anything at or below the per-``labels_json`` watermark was
      already emitted, which the half-open partition makes impossible for
      legitimate samples.  The watermark is read from a snapshot taken before
      this chunk is applied, so two distinct series sharing one timestamp both
      survive.
    """
    ordered = sorted(rows)
    previous = dict(watermark)
    seen_values: dict[tuple[str, int], set[float]] = {}
    kept: list[_Row] = []
    for row in ordered:
        stats = stats_by_entity[row.entity]
        if row.ts_us <= previous.get(row.labels_json, -1):
            stats.boundary_duplicates += 1
            continue
        if row.dedup_key in seen_values:
            if row.value in seen_values[row.dedup_key]:
                stats.duplicates += 1
                continue
            # Value conflict: same full labels + timestamp, different value.
            # Retain both and flag for the stitch step.
            stats.overlapping_target_samples += 1
            seen_values[row.dedup_key].add(row.value)
            kept.append(row)
            stats.record_kept(row)
            continue
        seen_values[row.dedup_key] = {row.value}
        kept.append(row)
        stats.record_kept(row)
    for row in kept:
        current = watermark.get(row.labels_json, -1)
        if row.ts_us > current:
            watermark[row.labels_json] = row.ts_us
    return kept


def rows_to_table(rows: list[_Row], extraction_id: str) -> pa.Table:
    """Build a ``RAW_SAMPLE``-typed table from already-ordered rows."""
    return pa.table(
        {
            "entity": pa.array([r.entity for r in rows], pa.string()),
            "metric": pa.array([r.metric for r in rows], pa.string()),
            "unit": pa.array([r.unit for r in rows], pa.string()),
            "series_key": pa.array([r.series_key for r in rows], pa.string()),
            "ts": pa.array([r.ts_us for r in rows], pa.timestamp("us", tz="UTC")),
            "value": pa.array([r.value for r in rows], pa.float64()),
            "scrape_instance": pa.array([r.scrape_instance or None for r in rows], pa.string()),
            "extraction_id": pa.array([extraction_id] * len(rows), pa.string()),
            "labels_json": pa.array([r.labels_json for r in rows], pa.string()),
        },
        schema=RAW_SAMPLE,
    )


def table_to_bytes(table: pa.Table) -> bytes:
    """Serialise a table to Parquet bytes (deterministic for identical input)."""
    sink = BytesIO()
    pq.write_table(table, sink)
    return sink.getvalue()


# ---------------------------------------------------------------------------
# Public API
# ---------------------------------------------------------------------------


def extract_prometheus(
    *,
    data_dir: str | os.PathLike[str] | None,
    config_path: str | os.PathLike[str] | None,
    start: str,
    end: str,
    entities: list[str] | None,
    base_url: str | None = None,
    client: httpx.Client | None = None,
    chunk_seconds: int | float = DEFAULT_CHUNK_SECONDS,
    max_retries: int = 3,
    allow_non_finite: bool = False,
) -> Path:
    """Extract raw Prometheus samples for *entities* over ``[start, end)``.

    Uses instant queries with range-vector selectors (never ``query_range``) so
    true scrape timestamps are preserved, one chunk in memory at a time, and
    writes one content-addressed Parquet part per non-empty chunk plus a single
    success manifest.

    Args:
        data_dir: Data directory root; ``None`` resolves env/default
            (:func:`paths.resolve_data_dir`).
        config_path: Tariff config used for ``config_hash``; ``None`` resolves
            via :func:`config.resolve_config_path`.
        start: ISO-8601 timestamp with an explicit UTC offset (inclusive).
        end: ISO-8601 timestamp with an explicit UTC offset (exclusive).
        entities: Entity IDs, or ``entity:metric:unit`` triples; ``None`` or
            empty selects :data:`DEFAULT_ENTITY_IDS` (``sensor.esios_pvpc_2``
            is never a default — it is a dead duplicate of the price sensor).
        base_url: Prometheus base URL; ``None`` resolves
            ``ENERGY_BASELINE_PROMETHEUS_URL`` then :data:`DEFAULT_BASE_URL`.
        client: Pre-configured :class:`httpx.Client` to use (test injection).
            When omitted an unauthenticated client is created and closed here.
        chunk_seconds: Chunk width in seconds (default 24h).  Each chunk is
            queried with ``ceil(span) + 1`` second range vector so the
            left-open edge cannot drop a sample sitting exactly on ``c_start``.
        max_retries: Additional attempts after the first for 429/5xx/timeouts
            (so at most ``max_retries + 1`` requests per chunk per entity).
        allow_non_finite: Drop ``NaN``/``±Inf`` samples with a quality flag
            instead of aborting (default: abort, write nothing, no manifest).

    Returns:
        The :class:`~pathlib.Path` of the written success manifest
        (``<data_dir>/manifests/<extraction_id>.json``).  Nothing is returned
        on failure: any error propagates before the manifest exists.

    Raises:
        ValueError: bad window (naive/non-ISO/empty), bad chunk width, or an
            entity with no known metric mapping.
        PrometheusHTTPError / PrometheusQueryError / NonFiniteSampleError:
            query failures (case c).  A failed run leaves at most orphaned
            parts and **no** manifest.
        FileExistsError: a part with the same chunk boundaries but different
            content already exists (no-clobber store).
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

    if t_start < ARCHIVE_START_UTC:
        log.warning(
            "start %s precedes the archive start %s — those chunks will be empty "
            "and flagged, not silently continuous",
            iso_z(t_start),
            iso_z(ARCHIVE_START_UTC),
        )

    specs = resolve_entity_specs(entities)
    url = resolve_base_url(base_url)
    entity_ids = [s.entity_id for s in specs]
    started_at = iso_z(datetime.now(UTC))

    manifest = new_manifest(
        source=SOURCE,
        config_path=cfg,
        params={
            "start": iso_z(t_start),
            "end": iso_z(t_end),
            "interval_semantics": "half-open [start, end)",
            "base_url": url,
            "api": f"{QUERY_PATH} instant query with range-vector selector (query_range unused)",
            "chunk_seconds": chunk_seconds,
            "range_padding_seconds": RANGE_PADDING_SECONDS,
            "max_retries": max_retries,
            "allow_non_finite": allow_non_finite,
            "series_identity": {
                "hash": "sha256(canonical JSON of labels, sorted keys, ASCII)",
                "excluded_labels": sorted(VOLATILE_IDENTITY_LABELS),
                "retained_in_labels_json": True,
            },
            "entity_specs": [
                {"entity": s.entity_id, "metric": s.metric, "unit": s.unit} for s in specs
            ],
        },
        entities=entity_ids,
        source_window_start=iso_z(t_start),
        source_window_end=iso_z(t_end),
        started_at=started_at,
    )
    extraction_id = manifest.extraction_id

    stats = {s.entity_id: _EntityStats(s) for s in specs}
    watermark: dict[str, int] = {}
    part_files: list[dict[str, object]] = []
    total_rows = 0
    chunk_count = 0

    own_client = client is None
    http_client = client if client is not None else httpx.Client(timeout=_TIMEOUT)
    try:
        for c_start, c_end in iter_chunks(t_start, t_end, chunk_seconds):
            chunk_count += 1
            window_seconds = range_window_seconds(c_start, c_end)
            lo_us, hi_us = to_micros(c_start), to_micros(c_end)
            chunk_rows: list[_Row] = []

            for spec in specs:
                st = stats[spec.entity_id]
                st.chunks += 1
                query = build_range_query(spec, window_seconds)
                matrix = query_range_vector(
                    http_client,
                    base_url=url,
                    query=query,
                    time_us=hi_us,
                    max_retries=max_retries,
                )
                chunk_rows.extend(
                    matrix_to_rows(
                        matrix,
                        spec=spec,
                        stats=st,
                        query=query,
                        lo_us=lo_us,
                        hi_us=hi_us,
                        allow_non_finite=allow_non_finite,
                    )
                )

            kept = dedupe_rows(chunk_rows, stats, watermark)
            rows_written = len(kept)
            total_rows += rows_written
            log.info(
                "chunk %d [%s, %s): window=%ds rows=%d entities=%s",
                chunk_count,
                iso_z(c_start),
                iso_z(c_end),
                window_seconds,
                rows_written,
                ",".join(entity_ids),
            )
            if not kept:
                continue  # empty chunk: recorded via flags, no part file

            table = rows_to_table(kept, extraction_id)
            validate_table("RAW_SAMPLE", table)
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
                    "rows": rows_written,
                    "chunk_start": iso_z(c_start),
                    "chunk_end": iso_z(c_end),
                    "entities": entity_ids,
                }
            )
    finally:
        if own_client:
            http_client.close()

    flags: list[str] = []
    for spec in specs:
        st = stats[spec.entity_id]
        flags.extend(st.flags(chunk_count))
        log.info(
            "entity %s metric=%s unit=%s samples=%d series=%d first=%s last=%s "
            "chunks_no_series=%d chunks_no_samples=%d outside_window=%d dropped_dup=%d",
            spec.entity_id,
            spec.metric,
            spec.unit,
            st.samples,
            len(st.series_keys),
            iso_z(from_micros(st.first_us)) if st.first_us is not None else "none",
            iso_z(from_micros(st.last_us)) if st.last_us is not None else "none",
            st.chunks_no_series,
            st.chunks_no_samples,
            st.samples_outside_window,
            st.duplicates + st.boundary_duplicates,
        )
    if total_rows == 0:
        flags.append("no_samples_extracted:all")
        log.warning(
            "no samples found for %s in [%s, %s) — coverage is NOT established by an "
            "empty response",
            ",".join(entity_ids),
            iso_z(t_start),
            iso_z(t_end),
        )

    manifest.row_count = total_rows
    manifest.finished_at = iso_z(datetime.now(UTC))
    manifest.part_files = part_files
    manifest.quality_flags = sorted(set(flags))
    manifest.params["chunks"] = chunk_count
    manifest.params["entity_stats"] = [stats[e].as_dict(chunk_count) for e in entity_ids]
    manifest.coverage_notes = (
        f"{chunk_count} chunk(s) of <= {chunk_seconds}s covering "
        f"[{iso_z(t_start)}, {iso_z(t_end)}) via instant range-vector queries; "
        f"{total_rows} sample(s) in {len(part_files)} part file(s). "
        "Empty responses are recorded as flags (no_series_chunks / "
        "series_without_samples_chunks) and are never treated as continuity. "
        "Chunks with no rows have no part file."
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


def validate_raw(
    *,
    data_dir: str | os.PathLike[str] | None,
    source: str = SOURCE,
) -> dict[str, object]:
    """Validate every raw part for *source* against ``RAW_SAMPLE``.

    Reads each part, checks schema equality and the non-null invariants via
    :func:`schemas.validate_table`, and additionally reports non-finite values,
    a filename content hash that does not match the file bytes, ``labels_json``
    that is not a JSON object (spot-checked on the first row), duplicate
    ``(labels_json, ts, value)`` rows (exact same sample twice — a different
    ``series_key`` at the same ``ts`` is legitimate; different instances sharing
    a ``series_key`` are distinct raw rows), and parts whose rows are not in the
    deterministic ``(series_key, ts, scrape_instance)`` order the extractor
    guarantees.

    Args:
        data_dir: Data directory root (``None`` resolves env/default).
        source: Source directory name, e.g. ``prometheus`` or ``ha_history``.

    Returns:
        Summary dict::

            {
              "source": str, "parts_dir": str, "file_count": int,
              "row_count": int, "series_count": int, "entities": [str, ...],
              "min_ts": str | None, "max_ts": str | None,
              "files": [{"file": str, "rows": int | None, "schema_ok": bool,
                         "error": str | None}, ...],
              "violations": [str, ...], "ok": bool,
            }

    Raises:
        ValueError: from :func:`paths.resolve_data_dir` when the resolved data
            directory lies inside a git worktree.  Content problems are never
            raised — they come back in ``violations``.
    """
    root = resolve_data_dir(None if data_dir is None else str(data_dir))
    pdir = parts_dir(root, source)
    files = iter_parts(root, source)
    summary: dict[str, object] = {
        "source": source,
        "parts_dir": str(pdir),
        "file_count": len(files),
        "row_count": 0,
        "series_count": 0,
        "entities": [],
        "min_ts": None,
        "max_ts": None,
        "files": [],
        "violations": [],
        "ok": True,
    }
    if not files:
        summary["violations"] = [f"no part files found in {pdir}"]
        summary["ok"] = False
        log.warning("validate_raw: no parts found under %s", pdir)
        return summary

    series: set[str] = set()
    entity_set: set[str] = set()
    ts_bounds: list[int] = []
    rows_out: list[dict[str, object]] = []
    violations: list[str] = []
    total = 0

    for path in files:
        entry: dict[str, object] = {
            "file": path.name,
            "rows": None,
            "schema_ok": False,
            "error": None,
        }
        # File-level integrity first: it applies even to an unreadable/mismatched payload.
        declared_hash = path.stem.rsplit("-", 1)[-1]
        actual_hash = hashlib.sha256(path.read_bytes()).hexdigest()
        if not _HASH8_RE.fullmatch(declared_hash) or declared_hash != actual_hash[:8]:
            violations.append(
                f"{path.name}: filename content hash {declared_hash!r} does not match "
                f"sha256 of the file bytes ({actual_hash[:8]})"
            )
        try:
            table = pq.read_table(path)
        except Exception as exc:  # unreadable part is a violation, not a crash
            entry["error"] = f"unreadable part: {type(exc).__name__}: {exc}"
            violations.append(f"{path.name}: {entry['error']}")
            rows_out.append(entry)
            continue

        try:
            validate_table("RAW_SAMPLE", table)
            entry["schema_ok"] = True
        except (TypeError, ValueError) as exc:
            entry["error"] = str(exc)
            violations.append(f"{path.name}: {exc}")
            rows_out.append(entry)
            continue

        n = table.num_rows
        total += n
        entry["rows"] = n
        if n == 0:
            violations.append(f"{path.name}: part file contains zero rows")

        values = table.column("value").to_pylist()
        bad = [i for i, v in enumerate(values) if v is None or not math.isfinite(v)]
        if bad:
            violations.append(f"{path.name}: {len(bad)} non-finite value(s) at row(s) {bad[:5]}")

        ts_us = [to_micros(t) for t in table.column("ts").to_pylist()]
        ts_bounds.extend([min(ts_us), max(ts_us)] if ts_us else [])
        keys = table.column("series_key").to_pylist()
        inst = [s or "" for s in table.column("scrape_instance").to_pylist()]
        series.update(keys)
        entity_set.update(table.column("entity").to_pylist())

        order = list(zip(keys, ts_us, inst, strict=True))
        if order != sorted(order):
            violations.append(f"{path.name}: rows are not in deterministic order")
        # Check for exact-duplicate raw rows (same full label set + timestamp + value).
        # Different ``labels_json`` values sharing one ``series_key`` at the same
        # timestamp are legitimate (distinct scrape targets) and must not be
        # flagged.  Overlapping values (same labels_json, same ts, different value)
        # are also expected — the extractor retains both.  Only an exact
        # ``(labels_json, ts, value)`` triple that appears more than once is a bug.
        raw_triples = [
            (lj, ts, v)
            for lj, ts, v in zip(
                table.column("labels_json").to_pylist(), ts_us, values, strict=True
            )
        ]
        if len(set(raw_triples)) != len(raw_triples):
            violations.append(
                f"{path.name}: duplicate (labels_json, ts, value) rows — exact same sample twice"
            )

        if n:
            first_labels = table.column("labels_json")[0].as_py()
            try:
                parsed = json.loads(first_labels)
            except (TypeError, ValueError) as exc:
                violations.append(f"{path.name}: labels_json is not JSON: {exc}")
            else:
                if not isinstance(parsed, dict) or not parsed:
                    violations.append(f"{path.name}: labels_json is not a non-empty object")

        rows_out.append(entry)

    summary["row_count"] = total
    summary["series_count"] = len(series)
    summary["entities"] = sorted(entity_set)
    summary["min_ts"] = from_micros(min(ts_bounds)).isoformat() if ts_bounds else None
    summary["max_ts"] = from_micros(max(ts_bounds)).isoformat() if ts_bounds else None
    summary["files"] = rows_out
    summary["violations"] = violations
    summary["ok"] = not violations
    log.info(
        "validate_raw source=%s parts=%d rows=%d series=%d violations=%d",
        source,
        len(files),
        total,
        len(series),
        len(violations),
    )
    return summary
