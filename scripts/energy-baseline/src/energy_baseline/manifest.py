"""Extraction manifest read/write with code and config hashing.

Each extraction run produces a JSON manifest recording schema version,
code version, config hash, timing, parameters, and quality flags.

Config hashing
--------------
``config_hash`` computes a **deterministic canonical YAML hash**: the YAML
is loaded, then re-serialized with sorted keys and consistent formatting,
and the resulting bytes are SHA-256 hashed.  This means semantically
equivalent configs (e.g. different whitespace or key ordering) produce
the same hash.

An optional ``exact_raw=True`` flag hashes the raw file bytes instead,
which is useful for detecting any byte-level change (e.g. comment edits).
"""

from __future__ import annotations

import hashlib
import io
import json
import os
import re
import stat
import subprocess
import uuid
from dataclasses import asdict, dataclass, field
from pathlib import Path

import yaml

# ---------------------------------------------------------------------------
# Sensitive-key patterns for param sanitization
# ---------------------------------------------------------------------------

_SENSITIVE_KEY_RE = re.compile(
    r"(password|passwd|token|secret|api_key|apikey|auth|credential|"
    r"private_key|access_key|session)",
    re.IGNORECASE,
)

_URL_RE = re.compile(r"https?://", re.IGNORECASE)


def _sanitize_params(params: dict) -> dict:
    """Return a copy of *params* with sensitive values redacted.

    Redacts values whose keys match sensitive patterns (passwords, tokens,
    etc.) and strips URL/query parameters that might embed credentials.
    """
    clean: dict = {}
    for key, value in params.items():
        if _SENSITIVE_KEY_RE.search(key):
            clean[key] = "<redacted>"
        elif isinstance(value, str) and _URL_RE.search(value):
            # Strip query strings that may contain tokens
            clean[key] = value.split("?")[0]
        elif isinstance(value, dict):
            clean[key] = _sanitize_params(value)
        else:
            clean[key] = value
    return clean


@dataclass
class ExtractionManifest:
    """Reproducible record of a single extraction run."""

    manifest_version: int
    extraction_id: str
    started_at: str
    finished_at: str | None
    source: str
    code_version: str | None
    code_dirty: bool | None
    config_hash: str
    schema_version: int
    params: dict
    entities: list[str]
    source_window_start: str
    source_window_end: str
    row_count: int
    part_files: list[dict] = field(default_factory=list)
    coverage_notes: str = ""
    quality_flags: list[str] = field(default_factory=list)


def new_extraction_id() -> str:
    """Generate a new UUID4 extraction identifier."""
    return str(uuid.uuid4())


def config_hash(config_path: Path, *, exact_raw: bool = False) -> str:
    """SHA-256 hex digest of the config file.

    By default, computes a **deterministic canonical YAML hash**: loads
    the YAML, re-serializes with sorted keys and consistent formatting,
    then hashes the canonical bytes.  Semantically equivalent configs
    (different whitespace, key order) produce the same hash.

    If *exact_raw* is ``True``, hashes the raw file bytes instead — any
    byte-level change (including comments) produces a different hash.
    """
    if exact_raw:
        return hashlib.sha256(config_path.read_bytes()).hexdigest()

    # Canonical YAML: load then re-dump with deterministic settings
    raw = config_path.read_text(encoding="utf-8")
    data = yaml.safe_load(raw)
    buf = io.StringIO()
    yaml.dump(
        data,
        buf,
        default_flow_style=False,
        sort_keys=True,
        allow_unicode=True,
        width=1000,
    )
    return hashlib.sha256(buf.getvalue().encode("utf-8")).hexdigest()


def _package_dir() -> Path:
    """Return the directory containing this module (the package root)."""
    return Path(__file__).resolve().parent


def _get_code_version() -> tuple[str | None, bool | None]:
    """Best-effort git rev-parse HEAD using the **package** directory.

    Uses the package location (not cwd) so the version is deterministic
    regardless of where the process is invoked from.  Dirty check
    includes untracked files (``--untracked-files=all``).
    """
    pkg_dir = _package_dir()
    try:
        rev = (
            subprocess.check_output(
                ["git", "rev-parse", "HEAD"],
                stderr=subprocess.DEVNULL,
                cwd=str(pkg_dir),
            )
            .decode()
            .strip()
        )
        dirty_output = subprocess.check_output(
            ["git", "status", "--porcelain", "--untracked-files=all"],
            stderr=subprocess.DEVNULL,
            cwd=str(pkg_dir),
        )
        dirty = bool(dirty_output.strip())
        return rev, dirty
    except (subprocess.CalledProcessError, FileNotFoundError):
        return None, None


def _ensure_private_dir(path: Path) -> None:
    """Create *path* (and parents) with mode 0o700, new dirs only."""
    missing: list[Path] = []
    current = path.resolve()
    while not current.exists():
        missing.append(current)
        current = current.parent
    for d in reversed(missing):
        d.mkdir(mode=0o700, exist_ok=True)


def _write_all(fd: int, data: bytes) -> None:
    """Write *all* of *data* to file descriptor *fd*, handling partial writes.

    ``os.write`` is not guaranteed to write the full buffer in a single call
    (especially on network filesystems, pipes, or under memory pressure). This
    helper loops until every byte is flushed. A zero-byte return from
    ``os.write`` is treated as a fatal signal — it means the descriptor is
    broken (closed, pipe drained, or otherwise unable to make progress) and
    continuing would silently truncate the manifest.

    Raises:
        OSError: zero-byte progress before the full buffer was written.
    """
    offset = 0
    length = len(data)
    while offset < length:
        written = os.write(fd, data[offset:])
        if written == 0:
            raise OSError(
                f"os.write returned 0 after {offset}/{length} bytes — "
                "refusing to continue writing a truncated manifest"
            )
        offset += written


def _fsync_directory(path: Path) -> None:
    """Best-effort fsync of the directory entry at *path*.

    After a hard-link publish the file content is already fsynced, but the
    directory entry itself is only crash-durable if the directory is fsynced
    too. Failures are silent — the content is already durable, and not every
    filesystem exposes a directory fd that can be fsynced.
    """
    try:
        dir_fd = os.open(str(path), os.O_RDONLY | os.O_DIRECTORY)
    except OSError:
        return
    try:
        os.fsync(dir_fd)
    except OSError:
        pass
    finally:
        os.close(dir_fd)


def write_manifest(data_dir: Path, manifest: ExtractionManifest) -> Path:
    """Write a manifest JSON file atomically with no-clobber semantics.

    * Uses the same atomic-write pattern as :func:`paths.write_part_atomic`:
      unique temp file, fsync, hard-link publish.
    * The content is written through :func:`_write_all`, which loops on
      partial writes and raises on zero-byte progress, so a truncated
      manifest can never be silently published.
    * After the hard link is published, the **directory** is fsynced
      (best-effort) so the link is crash-durable.
    * Refuses to overwrite an existing manifest (raises ``FileExistsError``).
    * Sets file mode 0o600 and directory mode 0o700 (new dirs only).
    * Returns the path written.
    """
    import tempfile

    manifests_dir = data_dir / "manifests"
    _ensure_private_dir(manifests_dir)
    path = manifests_dir / f"{manifest.extraction_id}.json"

    content = json.dumps(asdict(manifest), indent=2, sort_keys=True, default=str)
    content_bytes = content.encode("utf-8")
    content_sha = hashlib.sha256(content_bytes).hexdigest()

    # Check existing
    if path.exists():
        existing_sha = hashlib.sha256(path.read_bytes()).hexdigest()
        if existing_sha == content_sha:
            return path  # idempotent
        raise FileExistsError(f"refusing to overwrite manifest {path} with different content")

    # Atomic write via temp + link
    fd = None
    tmp_path: Path | None = None
    try:
        fd, tmp_str = tempfile.mkstemp(
            prefix=f".{path.name}.",
            suffix=".tmp",
            dir=str(manifests_dir),
        )
        tmp_path = Path(tmp_str)
        _write_all(fd, content_bytes)
        os.fsync(fd)
        os.close(fd)
        fd = None
        os.chmod(tmp_str, stat.S_IRUSR | stat.S_IWUSR)  # 0o600

        try:
            os.link(tmp_str, str(path))
        except FileExistsError:
            existing_sha = hashlib.sha256(path.read_bytes()).hexdigest()
            if existing_sha != content_sha:
                raise FileExistsError(
                    f"refusing to overwrite manifest {path} with different content"
                )
        else:
            # Best-effort directory fsync so the hard link is crash-durable.
            _fsync_directory(manifests_dir)
    except BaseException:
        if fd is not None:
            os.close(fd)
        raise
    finally:
        if tmp_path is not None:
            tmp_path.unlink(missing_ok=True)

    return path


def read_manifest(data_dir: Path, extraction_id: str) -> ExtractionManifest:
    """Read a manifest JSON file by extraction ID."""
    path = data_dir / "manifests" / f"{extraction_id}.json"
    data = json.loads(path.read_text())
    return ExtractionManifest(**data)


#: Suffix appended to a code version whose working tree carries uncommitted changes.
DIRTY_SUFFIX = "-dirty"

#: Prefix of the code version when git itself is unavailable.  A sentinel, never a
#: real commit, so "nothing is known" can never be mistaken for a clean tree.
NO_GIT_MARKER = "nogit"


def code_version() -> str:
    """Single shared code identity for lineage, including an unambiguous dirty marker.

    Clean working tree: the bare 40-character commit SHA.  Dirty tree: the SHA
    followed by :data:`DIRTY_SUFFIX` (``-dirty``).  Git unavailable: the sentinel
    :data:`NO_GIT_MARKER` (``nogit``) followed by the package distribution
    version, so "nothing is known" can never be mistaken for a clean tree.

    Used by both :mod:`build` and :mod:`benchmark` so a dirty tree produces a
    different ``dataset_id`` from a clean one — the reproducibility guarantee
    the id exists to provide.
    """
    rev, dirty = _get_code_version()
    if rev is None:
        from energy_baseline import __version__

        return f"{NO_GIT_MARKER}@energy-baseline=={__version__}"
    return f"{rev}{DIRTY_SUFFIX if dirty else ''}"


def new_manifest(
    *,
    source: str,
    config_path: Path,
    params: dict,
    entities: list[str],
    source_window_start: str,
    source_window_end: str,
    started_at: str,
) -> ExtractionManifest:
    """Create a new manifest with code version and config hash populated.

    *params* is sanitized to redact sensitive keys (passwords, tokens,
    URLs with query strings) before storage.
    """
    code_ver, code_dirty = _get_code_version()
    from energy_baseline.schemas import SCHEMA_VERSION

    return ExtractionManifest(
        manifest_version=1,
        extraction_id=new_extraction_id(),
        started_at=started_at,
        finished_at=None,
        source=source,
        code_version=code_ver,
        code_dirty=code_dirty,
        config_hash=config_hash(config_path),
        schema_version=SCHEMA_VERSION,
        params=_sanitize_params(params),
        entities=entities,
        source_window_start=source_window_start,
        source_window_end=source_window_end,
        row_count=0,
    )
