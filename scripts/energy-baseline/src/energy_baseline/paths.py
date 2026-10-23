"""Data-directory resolution and immutable part-file naming.

The data directory lives **outside** the git worktree.  Default is
``~/data/energy-baseline``, overridable via ``--data-dir`` or the
``ENERGY_BASELINE_DATA_DIR`` environment variable.

Part filename contract::

    <kind>-<start_utc_compact>-<end_utc_compact>-<content_sha256_8>.parquet

where compact UTC is ``%Y%m%dT%H%M%SZ`` (e.g. ``20260101T000000Z``)
and the hash suffix is the first 8 hex characters of the SHA-256 of the
file bytes.
"""

from __future__ import annotations

import hashlib
import os
import stat
import tempfile
from datetime import UTC, datetime
from pathlib import Path

_DEFAULT_DATA_DIR = "~/data/energy-baseline"


def _find_git_root(start: Path) -> Path | None:
    """Walk ancestors of *start* looking for a ``.git`` entry.

    Returns the directory containing ``.git``, or ``None`` if not found.
    """
    current = start.resolve()
    for parent in [current, *current.parents]:
        if (parent / ".git").exists():
            return parent
    return None


def _is_inside_git_tree(path: Path) -> bool:
    """Return True if *path* is inside a git worktree."""
    return _find_git_root(path) is not None


def resolve_data_dir(cli_arg: str | None = None) -> Path:
    """Resolve the data directory.

    Priority: *cli_arg* > ``ENERGY_BASELINE_DATA_DIR`` env > default.

    All paths are expanded (``~``) and resolved to absolute.  The result
    is rejected if it falls inside a git worktree (detected by the
    presence of a ``.git`` ancestor).
    """
    if cli_arg:
        resolved = Path(os.path.expanduser(cli_arg)).resolve()
    else:
        env = os.environ.get("ENERGY_BASELINE_DATA_DIR")
        if env:
            resolved = Path(os.path.expanduser(env)).resolve()
        else:
            resolved = Path(_DEFAULT_DATA_DIR).expanduser().resolve()

    if _is_inside_git_tree(resolved):
        git_root = _find_git_root(resolved)
        raise ValueError(
            f"data directory {resolved} is inside git worktree {git_root}; "
            "choose a path outside the repository"
        )
    return resolved


def parts_dir(data_dir: Path, source: str) -> Path:
    """Return the parts directory for a given source."""
    return data_dir / "raw" / source / "v1" / "parts"


def _format_utc(dt: datetime, name: str) -> str:
    """Format a timezone-aware datetime as compact UTC for filenames.

    Raises ``ValueError`` for naive datetimes — callers must provide
    explicit UTC-aware values.
    """
    if dt.tzinfo is None:
        raise ValueError(
            f"{name} must be timezone-aware (UTC); got naive datetime {dt!r}. "
            "Convert to UTC explicitly before calling part_path()."
        )
    utc_dt = dt.astimezone(UTC)
    return utc_dt.strftime("%Y%m%dT%H%M%SZ")


def part_path(
    data_dir: Path,
    source: str,
    kind: str,
    start: datetime,
    end: datetime,
    content_hash_8: str,
) -> Path:
    """Construct a part file path from its components.

    Args:
        data_dir: Root data directory.
        source: Source name (e.g. ``prometheus``, ``ha_history``).
        kind: Part kind (e.g. ``prom-energy-kwh``, ``ha-enum``).
        start: Start timestamp (must be timezone-aware UTC).
        end: End timestamp (must be timezone-aware UTC).
        content_hash_8: First 8 hex chars of content SHA-256.

    Raises:
        ValueError: If *start* or *end* is naive (no tzinfo).
    """
    start_compact = _format_utc(start, "start")
    end_compact = _format_utc(end, "end")
    name = f"{kind}-{start_compact}-{end_compact}-{content_hash_8}.parquet"
    return parts_dir(data_dir, source) / name


def iter_parts(data_dir: Path, source: str) -> list[Path]:
    """Return sorted list of part files for a source."""
    pdir = parts_dir(data_dir, source)
    if not pdir.exists():
        return []
    return sorted(pdir.glob("*.parquet"))


def _ensure_private_dir(path: Path) -> None:
    """Create *path* (and parents) with mode 0700.

    Only sets mode on directories we create ourselves; existing
    directories are left untouched to avoid clobbering user permissions.
    """
    # Walk from root to leaf, creating only missing dirs with 0700
    missing: list[Path] = []
    current = path.resolve()
    while not current.exists():
        missing.append(current)
        current = current.parent
    for d in reversed(missing):
        d.mkdir(mode=0o700, exist_ok=True)


def write_part_atomic(path: Path, data: bytes) -> str:
    """Write *data* atomically to *path* with no-clobber semantics.

    * Creates a uniquely-named temp file in the same parent directory,
      writes *data*, fsyncs, then publishes via :func:`os.link` for
      atomic no-clobber semantics.
    * If *path* already exists with **identical** content (same SHA-256),
      returns immediately (idempotent).
    * If *path* already exists with **different** content, raises
      ``FileExistsError``.
    * Concurrent writers racing to create the same path: the first
      ``os.link`` wins; losers detect the existing file and verify the
      hash matches (if not, raises ``FileExistsError``).
    * Sets file mode to 0o600 and directory mode to 0o700 (new dirs only).
    * Returns the full SHA-256 hex digest of *data*.
    """
    content_sha = hashlib.sha256(data).hexdigest()

    # Fast path: existing file with matching content
    if path.exists():
        existing_sha = hashlib.sha256(path.read_bytes()).hexdigest()
        if existing_sha == content_sha:
            return content_sha  # idempotent
        raise FileExistsError(
            f"refusing to overwrite {path} with different content "
            f"(existing={existing_sha[:8]}, new={content_sha[:8]})"
        )

    # Ensure parent dirs exist with private permissions
    _ensure_private_dir(path.parent)

    # Write to a unique temp file in the same parent (same filesystem)
    fd = None
    tmp_path: Path | None = None
    try:
        fd, tmp_str = tempfile.mkstemp(
            prefix=f".{path.name}.",
            suffix=".tmp",
            dir=str(path.parent),
        )
        tmp_path = Path(tmp_str)
        os.write(fd, data)
        os.fsync(fd)
        os.close(fd)
        fd = None  # closed above
        os.chmod(tmp_str, stat.S_IRUSR | stat.S_IWUSR)  # 0o600

        # Atomic no-clobber publish via hard link
        try:
            os.link(tmp_str, str(path))
        except FileExistsError:
            # Another writer won the race — verify content matches
            existing_sha = hashlib.sha256(path.read_bytes()).hexdigest()
            if existing_sha != content_sha:
                raise FileExistsError(
                    f"refusing to overwrite {path} with different content "
                    f"(existing={existing_sha[:8]}, new={content_sha[:8]})"
                )
            # Content matches — idempotent, just clean up temp
    except BaseException:
        if fd is not None:
            os.close(fd)
        raise
    finally:
        if tmp_path is not None:
            tmp_path.unlink(missing_ok=True)

    return content_sha
