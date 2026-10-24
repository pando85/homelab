"""Output-manifest sidecars and run-artifact verification.

Two independent stages, each with its own sidecar:

* **Benchmark stage** — ``benchmark_dataset`` writes three immutable files
  (``benchmark.json``, ``results.parquet``, ``schedules.parquet``), then calls
  :func:`finalize_run_outputs` with ``required_files=BENCHMARK_REQUIRED_FILES``
  and the default ``manifest_name="outputs_manifest.json"``.

* **Report stage** — ``generate_report`` writes two more files
  (``report.json``, ``report.md``), then calls :func:`finalize_run_outputs`
  with ``required_files=REPORT_REQUIRED_FILES`` and
  ``manifest_name="report_outputs_manifest.json"``.

The two sidecars are **independent**.  The report sidecar hashes only the
report files; it does not depend on or re-hash the benchmark sidecar's
files.  Each stage is verifiable on its own.

The sidecar **excludes itself**: a manifest that hashes its own content would
be a fixed-point problem.  Verification detects:

* **truncation** — file byte length differs from the manifest's ``bytes``
* **tampering / corruption** — SHA-256 differs from the manifest's ``sha256``
* **missing required files** — a required file does not exist on disk
* **unexpected files** — files in the run directory not covered by the manifest
* **unsafe paths** — path traversal (``..``), absolute paths, symlinks
* **self-reference** — the manifest must not list itself

This module does **not** claim cryptographic signature authenticity.  It
detects accidental corruption, incomplete writes and post-hoc edits — not
adversarial tampering by an attacker who can rewrite the manifest.

Integration with ``benchmark.py``
---------------------------------
After the three ``write_part_atomic`` calls in ``benchmark_dataset``::

    from energy_baseline.artifact_integrity import (
        BENCHMARK_REQUIRED_FILES,
        finalize_run_outputs,
    )

    # run_dir already holds benchmark.json, results.parquet, schedules.parquet
    finalize_run_outputs(run_dir, required_files=BENCHMARK_REQUIRED_FILES)

Integration with ``report.py``
------------------------------
After writing ``report.json`` and ``report.md`` in ``generate_report``::

    from energy_baseline.artifact_integrity import (
        REPORT_REQUIRED_FILES,
        finalize_run_outputs,
    )

    finalize_run_outputs(
        run.directory,
        required_files=REPORT_REQUIRED_FILES,
        manifest_name="report_outputs_manifest.json",
    )

CLI integrity flag
------------------
The canonical CLI can expose ``--require-artifact-integrity`` that calls
:func:`verify_run_outputs` after each stage and aborts on failure.  This
preserves backwards compatibility: existing callers and tests that do not
pass the flag are unaffected.

Monthly usable-fraction gate
-----------------------------
:func:`monthly_usable_fraction_gate` is a standalone helper that evaluates
per-month usable fractions against an **explicit, finite** threshold in
``(0, 1]``.  It does NOT silently default to 0.80 or any other universal
value.  The caller must pass the threshold; the function validates it and
applies the declared policy (``reject``, ``exclude`` or ``report``).

``benchmark.py`` currently has ``min_usable_fraction=0.0`` (inert — aborts
on nothing).  The gate is **only called by the integration when the
threshold is > 0**.  A zero threshold means the gate is disabled (legacy
behaviour).  The helper itself refuses zero because a threshold of zero
would accept every month and is therefore vacuous.
"""

from __future__ import annotations

import hashlib
import json
import math
import os
import re
import stat
import tempfile
from dataclasses import dataclass
from pathlib import Path
from typing import Any

# ---------------------------------------------------------------------------
# Constants
# ---------------------------------------------------------------------------

#: Default sidecar manifest filename for the benchmark stage.
BENCHMARK_MANIFEST_NAME = "outputs_manifest.json"

#: Sidecar manifest filename for the report stage.
REPORT_MANIFEST_NAME = "report_outputs_manifest.json"

#: Schema version of the sidecar manifest.  Bump on breaking changes.
MANIFEST_SCHEMA_VERSION = 1

#: The three files ``benchmark_dataset`` writes.
BENCHMARK_REQUIRED_FILES: tuple[str, ...] = (
    "benchmark.json",
    "results.parquet",
    "schedules.parquet",
)

#: The two files ``generate_report`` writes.
REPORT_REQUIRED_FILES: tuple[str, ...] = (
    "report.json",
    "report.md",
)

#: Regex for a valid 64-character lowercase hex SHA-256 digest.
_SHA256_HEX_RE = re.compile(r"^[0-9a-f]{64}$")

#: Regex for a valid manifest basename (plain filename, no separators).
_MANIFEST_NAME_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]*\.json$")


# ---------------------------------------------------------------------------
# Errors
# ---------------------------------------------------------------------------


class ArtifactIntegrityError(RuntimeError):
    """Base class for artifact-integrity failures."""


class ArtifactMissingError(ArtifactIntegrityError):
    """A required artifact file does not exist."""


class UnsafePathError(ArtifactIntegrityError):
    """A path is unsafe: traversal, absolute, symlink, or escapes run_dir."""


class ManifestSelfReferenceError(ArtifactIntegrityError):
    """The manifest lists itself as a hashed artifact."""


class MissingManifestError(ArtifactIntegrityError):
    """No manifest sidecar found in the run directory."""


class ManifestSchemaError(ArtifactIntegrityError):
    """The manifest has an unexpected structure or unsupported schema version."""


# ---------------------------------------------------------------------------
# Structured results
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class FileVerification:
    """Verification outcome for a single artifact file."""

    filename: str
    exists: bool
    expected_sha256: str
    actual_sha256: str | None
    expected_bytes: int
    actual_bytes: int | None
    hash_ok: bool
    size_ok: bool

    @property
    def ok(self) -> bool:
        return self.exists and self.hash_ok and self.size_ok


@dataclass(frozen=True)
class VerificationResult:
    """Full verification outcome for a run directory."""

    run_dir: Path
    manifest_name: str
    manifest_schema_version: int
    required_files: tuple[str, ...]
    files: list[FileVerification]
    missing_required: list[str]
    unexpected_files: list[str]
    hash_mismatches: list[str]
    size_mismatches: list[str]

    @property
    def ok(self) -> bool:
        """True when every required file is present, intact, and no hash/size drift."""
        return not self.missing_required and not self.hash_mismatches and not self.size_mismatches


@dataclass(frozen=True)
class MonthFraction:
    """Per-month usable-fraction detail returned by the gate helper.

    ``expected_intervals`` is the expected interval count for a full calendar
    month at the dataset's grid (e.g. ~2880 for 15-min in a 31-day month).
    ``coverage`` is ``total / expected_intervals`` when both are known,
    ``None`` otherwise.  A coverage < 1.0 declares the month as partial —
    the usable fraction is computed over the intervals that exist, not over
    a full calendar month.
    """

    month: str
    usable: int
    total: int
    fraction: float
    expected_intervals: int | None = None
    coverage: float | None = None


@dataclass(frozen=True)
class MonthlyGateResult:
    """Result of :func:`monthly_usable_fraction_gate`.

    ``rejected_months`` is non-empty only when ``policy="reject"`` and at
    least one evaluation month is below threshold.  ``excluded_months`` is
    non-empty only when ``policy="exclude"``.  ``below_threshold`` lists
    every month below threshold regardless of policy (for ``report`` mode).
    """

    threshold: float
    policy: str
    per_month: list[MonthFraction]
    below_threshold: list[MonthFraction]
    rejected_months: list[MonthFraction]
    excluded_months: list[MonthFraction]
    eval_months_assessed: list[str]


class MonthlyQualityGateError(ArtifactIntegrityError):
    """A monthly usable fraction is below the declared threshold.

    The structured :class:`MonthlyGateResult` is attached as ``.result``
    so callers can inspect per-month detail without re-parsing the message.
    """

    def __init__(self, message: str, result: MonthlyGateResult) -> None:
        super().__init__(message)
        self.result = result


# ---------------------------------------------------------------------------
# Internal helpers
# ---------------------------------------------------------------------------


def _hash_file(path: Path) -> tuple[str, int]:
    """Return ``(sha256_hex, byte_length)`` of the file at *path*."""
    h = hashlib.sha256()
    size = 0
    with open(path, "rb") as fh:
        while True:
            chunk = fh.read(65536)
            if not chunk:
                break
            h.update(chunk)
            size += len(chunk)
    return h.hexdigest(), size


def _vet_manifest_name(name: str) -> None:
    """Validate that *name* is a safe manifest basename.

    Must match ``[A-Za-z0-9][A-Za-z0-9._-]*.json`` — a plain filename with
    no path separators, no traversal, no leading dot.
    """
    if not name:
        raise ValueError("manifest_name must not be empty")
    if "/" in name or "\\" in name:
        raise UnsafePathError(f"manifest_name contains path separator: {name!r}")
    if ".." in name:
        raise UnsafePathError(f"manifest_name contains path traversal: {name!r}")
    if not _MANIFEST_NAME_RE.match(name):
        raise ValueError(
            f"manifest_name {name!r} is not a valid basename: "
            "must match [A-Za-z0-9][A-Za-z0-9._-]*.json"
        )


def _validate_filename(name: str) -> None:
    """Validate that *name* is a safe plain filename (no path components).

    Rejects empty names, absolute paths, path traversal, backslashes,
    and directory separators.  Does NOT check the filesystem — that is
    the caller's job after resolution.
    """
    if not name:
        raise UnsafePathError("artifact filename is empty")
    if os.path.isabs(name):
        raise UnsafePathError(f"artifact filename is absolute: {name!r}")
    if "\\" in name:
        raise UnsafePathError(f"artifact filename contains backslash: {name!r}")
    if "/" in name:
        raise UnsafePathError(
            f"artifact filename contains directory separator: {name!r} — "
            "all artifacts must be direct children of the run directory"
        )
    if name in (".", ".."):
        raise UnsafePathError(f"artifact filename is a special directory: {name!r}")


def _check_run_dir_symlinks(run_dir: Path) -> None:
    """Raise if *run_dir* or any of its ancestors (up to root) is a symlink.

    Checked on the **unresolved** path — we refuse to follow symlinks at all.
    The caller must pass the path as given (before ``.resolve()``).
    """
    current = Path(run_dir)
    # Walk from the given path upward, checking each component
    # We need to check each prefix of the path
    parts_to_check: list[Path] = []
    c = current
    while True:
        parts_to_check.append(c)
        parent = c.parent
        if parent == c:
            break
        c = parent

    for p in parts_to_check:
        if p.is_symlink():
            raise UnsafePathError(f"run directory is or contains a symlink: {p}")


def _validate_required_files(
    required_files: tuple[str, ...] | list[str],
    manifest_name: str,
) -> tuple[str, ...]:
    """Validate the caller's required_files set.

    * Must be non-empty
    * No duplicates
    * Each name is a safe plain filename
    * No name matches the manifest_name (self-reference)
    * Each sha256 would be 64 hex (validated at hash time, not here)

    Returns the validated tuple.
    """
    if not required_files:
        raise ValueError("required_files must not be empty")

    seen: set[str] = set()
    for name in required_files:
        if not isinstance(name, str):
            raise ValueError(f"required_files entry must be str, got {type(name).__name__}")
        _validate_filename(name)
        if name == manifest_name:
            raise ManifestSelfReferenceError(
                f"required_files contains the manifest name {manifest_name!r} — "
                "a manifest cannot hash itself"
            )
        if name in seen:
            raise ValueError(f"required_files contains duplicate: {name!r}")
        seen.add(name)

    return tuple(required_files)


def _write_manifest_atomic(path: Path, data: bytes) -> None:
    """Write the manifest atomically with no-clobber semantics.

    Uses the same pattern as ``paths.write_part_atomic``: temp file, fsync,
    hard-link publish.  Refuses to overwrite with different content.
    """
    content_sha = hashlib.sha256(data).hexdigest()

    # Fast path: existing file with matching content (idempotent)
    if path.exists():
        existing_sha = hashlib.sha256(path.read_bytes()).hexdigest()
        if existing_sha == content_sha:
            return
        raise FileExistsError(
            f"refusing to overwrite manifest {path} with different content "
            f"(existing={existing_sha[:12]}, new={content_sha[:12]})"
        )

    # Ensure parent exists
    path.parent.mkdir(parents=True, exist_ok=True)

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
        fd = None
        os.chmod(tmp_str, stat.S_IRUSR | stat.S_IWUSR)

        try:
            os.link(tmp_str, str(path))
        except FileExistsError:
            existing_sha = hashlib.sha256(path.read_bytes()).hexdigest()
            if existing_sha != content_sha:
                raise FileExistsError(
                    f"refusing to overwrite manifest {path} with different content"
                )
        else:
            # Best-effort directory fsync
            try:
                dir_fd = os.open(str(path.parent), os.O_RDONLY | os.O_DIRECTORY)
                try:
                    os.fsync(dir_fd)
                finally:
                    os.close(dir_fd)
            except OSError:
                pass
    except BaseException:
        if fd is not None:
            os.close(fd)
        raise
    finally:
        if tmp_path is not None:
            tmp_path.unlink(missing_ok=True)


def _validate_bucket(month: str, bucket: Any) -> tuple[int, int]:
    """Validate a per_month bucket and return ``(total, usable)`` as ints.

    Strict rules:
    * bucket must be a dict
    * ``total`` must be present, an int (not bool), non-negative
    * ``usable`` must be present, an int (not bool), non-negative
    * ``usable`` must be <= ``total``
    """
    if not isinstance(bucket, dict):
        raise ValueError(f"per_month[{month!r}] must be a dict, got {type(bucket).__name__}")

    for key in ("total", "usable"):
        if key not in bucket:
            raise ValueError(f"per_month[{month!r}] missing required key {key!r}")

    total_raw = bucket["total"]
    usable_raw = bucket["usable"]

    # bool is a subclass of int in Python — reject it explicitly
    if isinstance(total_raw, bool) or not isinstance(total_raw, int):
        raise ValueError(
            f"per_month[{month!r}]['total'] must be int, "
            f"got {type(total_raw).__name__}: {total_raw!r}"
        )
    if isinstance(usable_raw, bool) or not isinstance(usable_raw, int):
        raise ValueError(
            f"per_month[{month!r}]['usable'] must be int, "
            f"got {type(usable_raw).__name__}: {usable_raw!r}"
        )

    if total_raw < 0:
        raise ValueError(f"per_month[{month!r}]['total'] must be >= 0, got {total_raw}")
    if usable_raw < 0:
        raise ValueError(f"per_month[{month!r}]['usable'] must be >= 0, got {usable_raw}")
    if usable_raw > total_raw:
        raise ValueError(f"per_month[{month!r}]: usable ({usable_raw}) > total ({total_raw})")

    return total_raw, usable_raw


# ---------------------------------------------------------------------------
# Public API: finalize
# ---------------------------------------------------------------------------


def finalize_run_outputs(
    run_dir: Path | str,
    required_files: tuple[str, ...] | list[str],
    manifest_name: str = BENCHMARK_MANIFEST_NAME,
) -> Path:
    """Write an outputs manifest sidecar for a completed stage.

    Must be called **after** all artifact files for this stage have been
    written.  The manifest records the SHA-256 and byte length of every
    required file.

    Two stages use separate sidecars:

    * Benchmark: ``finalize_run_outputs(run_dir, BENCHMARK_REQUIRED_FILES)``
      → writes ``outputs_manifest.json``
    * Report: ``finalize_run_outputs(run_dir, REPORT_REQUIRED_FILES,
      "report_outputs_manifest.json")`` → writes ``report_outputs_manifest.json``

    Args:
        run_dir: The benchmark run directory containing the artifact files.
        required_files: **Required.**  Explicit set of filenames to hash and
            record.  Must be non-empty, no duplicates, no self-reference.
        manifest_name: Basename of the sidecar file.  Default
            ``"outputs_manifest.json"`` for the benchmark stage.  Pass
            ``"report_outputs_manifest.json"`` for the report stage.  Must
            be a vetted basename (no path separators, must end in ``.json``).

    Returns:
        The path to the written manifest.

    Raises:
        ValueError: ``required_files`` is empty, has duplicates, or
            ``manifest_name`` is not a valid basename.
        ArtifactMissingError: a required file does not exist on disk.
        UnsafePathError: a filename is unsafe, or the run directory
            contains a symlink in its path.
        ManifestSelfReferenceError: ``required_files`` contains the
            manifest name.
        FileExistsError: a manifest already exists with different content
            (no-clobber).
    """
    _vet_manifest_name(manifest_name)
    required = _validate_required_files(required_files, manifest_name)

    run_path = Path(run_dir)

    # Check run_dir is not a symlink and doesn't contain one (on unresolved path)
    _check_run_dir_symlinks(run_path)

    run_path = run_path.resolve()

    if not run_path.is_dir():
        raise FileNotFoundError(f"run directory does not exist: {run_path}")

    manifest_path = run_path / manifest_name

    # Check manifest path itself is not a symlink
    if manifest_path.is_symlink():
        raise UnsafePathError(f"manifest path is a symlink: {manifest_path}")

    # Validate all filenames and check files exist (before hashing anything)
    for name in required:
        artifact_path = run_path / name
        # Check the unresolved path for symlinks
        if artifact_path.is_symlink():
            raise UnsafePathError(f"artifact is a symlink: {artifact_path}")
        if not artifact_path.is_file():
            raise ArtifactMissingError(
                f"required artifact missing: {name!r} not found in {run_path}"
            )

    # Hash all files
    entries: list[dict[str, Any]] = []
    for name in sorted(required):
        artifact_path = run_path / name
        sha256, size = _hash_file(artifact_path)
        entries.append(
            {
                "filename": name,
                "sha256": sha256,
                "bytes": size,
            }
        )

    payload = {
        "schema_version": MANIFEST_SCHEMA_VERSION,
        "files": entries,
    }

    data = json.dumps(payload, indent=2, sort_keys=True).encode("utf-8")
    _write_manifest_atomic(manifest_path, data)
    return manifest_path


# ---------------------------------------------------------------------------
# Public API: verify
# ---------------------------------------------------------------------------


def verify_run_outputs(
    run_dir: Path | str,
    required_files: tuple[str, ...] | list[str],
    manifest_name: str = BENCHMARK_MANIFEST_NAME,
) -> VerificationResult:
    """Verify a run directory stage against its outputs manifest.

    The required set is enforced from the **CODE caller**, not from the
    manifest content.  The manifest is read for hash/size values, but the
    set of files that must be present and intact comes from
    ``required_files``.

    Args:
        run_dir: The benchmark run directory.
        required_files: **Required.**  The set of files the caller expects
            to be present and verified.  Must be non-empty, no duplicates.
        manifest_name: Basename of the sidecar to read.  Default
            ``"outputs_manifest.json"`` for the benchmark stage.

    Returns:
        A :class:`VerificationResult` with per-file detail and aggregate
        lists of failures.  Check ``.ok`` for a quick pass/fail.

    Raises:
        ValueError: ``required_files`` is empty or has duplicates.
        MissingManifestError: the manifest sidecar is not found.
        ManifestSchemaError: unsupported schema version, malformed JSON,
            invalid entry types, or SHA-256 not 64 hex chars.
        UnsafePathError: a manifest entry or the run directory is unsafe.
        ManifestSelfReferenceError: the manifest lists itself.
    """
    _vet_manifest_name(manifest_name)
    required = _validate_required_files(required_files, manifest_name)

    run_path = Path(run_dir)

    # Check run_dir is not a symlink (on unresolved path)
    _check_run_dir_symlinks(run_path)

    run_path = run_path.resolve()

    manifest_path = run_path / manifest_name

    if manifest_path.is_symlink():
        raise UnsafePathError(f"manifest path is a symlink: {manifest_path}")

    if not manifest_path.is_file():
        raise MissingManifestError(f"no {manifest_name} in {run_path}")

    raw = manifest_path.read_bytes()
    try:
        manifest = json.loads(raw.decode("utf-8"))
    except (json.JSONDecodeError, UnicodeDecodeError) as exc:
        raise ManifestSchemaError(f"manifest is not valid JSON: {exc}") from exc

    if not isinstance(manifest, dict):
        raise ManifestSchemaError("manifest root is not a JSON object")

    schema_version = manifest.get("schema_version")
    # bool is a subclass of int; True == 1 would sneak past a bare != check.
    if isinstance(schema_version, bool) or schema_version != MANIFEST_SCHEMA_VERSION:
        raise ManifestSchemaError(
            f"unsupported manifest schema version: {schema_version!r} "
            f"(expected int {MANIFEST_SCHEMA_VERSION})"
        )

    file_entries = manifest.get("files")
    if not isinstance(file_entries, list):
        raise ManifestSchemaError("manifest 'files' is not a list")

    # Build a lookup from the manifest's file entries
    manifest_by_name: dict[str, dict[str, Any]] = {}
    for entry in file_entries:
        if not isinstance(entry, dict):
            raise ManifestSchemaError(f"manifest file entry is not an object: {entry!r}")

        name = entry.get("filename")
        if not isinstance(name, str) or not name:
            raise ManifestSchemaError(f"manifest entry missing 'filename': {entry!r}")

        if name == manifest_name:
            raise ManifestSelfReferenceError(
                f"manifest lists itself ({manifest_name!r}) — self-reference hashes are invalid"
            )

        _validate_filename(name)

        if name in manifest_by_name:
            raise ManifestSchemaError(f"duplicate manifest entry: {name!r}")

        expected_sha = entry.get("sha256")
        expected_bytes = entry.get("bytes")

        # SHA must be a 64-char lowercase hex string
        if not isinstance(expected_sha, str) or not _SHA256_HEX_RE.match(expected_sha):
            raise ManifestSchemaError(
                f"manifest entry for {name!r} has invalid sha256: "
                f"must be 64 lowercase hex chars, got {expected_sha!r}"
            )

        # bytes must be int (not bool) and non-negative
        if isinstance(expected_bytes, bool) or not isinstance(expected_bytes, int):
            raise ManifestSchemaError(
                f"manifest entry for {name!r} has invalid bytes type: "
                f"must be int, got {type(expected_bytes).__name__}"
            )
        if expected_bytes < 0:
            raise ManifestSchemaError(
                f"manifest entry for {name!r} has negative bytes: {expected_bytes}"
            )

        manifest_by_name[name] = entry

    # Verify each required file against the manifest
    verifications: list[FileVerification] = []
    missing_required: list[str] = []
    hash_mismatches: list[str] = []
    size_mismatches: list[str] = []

    for name in required:
        if name not in manifest_by_name:
            # Required by code but not in manifest — the manifest is incomplete
            raise ManifestSchemaError(
                f"required file {name!r} is not in the manifest — "
                "the manifest does not cover all required files"
            )

        entry = manifest_by_name[name]
        expected_sha = entry["sha256"]
        expected_bytes = entry["bytes"]

        artifact_path = run_path / name

        if artifact_path.is_symlink():
            raise UnsafePathError(f"artifact is a symlink: {artifact_path}")

        if not artifact_path.is_file():
            missing_required.append(name)
            verifications.append(
                FileVerification(
                    filename=name,
                    exists=False,
                    expected_sha256=expected_sha,
                    actual_sha256=None,
                    expected_bytes=expected_bytes,
                    actual_bytes=None,
                    hash_ok=False,
                    size_ok=False,
                )
            )
            continue

        actual_sha, actual_size = _hash_file(artifact_path)
        hash_ok = actual_sha == expected_sha
        size_ok = actual_size == expected_bytes

        if not hash_ok:
            hash_mismatches.append(name)
        if not size_ok:
            size_mismatches.append(name)

        verifications.append(
            FileVerification(
                filename=name,
                exists=True,
                expected_sha256=expected_sha,
                actual_sha256=actual_sha,
                expected_bytes=expected_bytes,
                actual_bytes=actual_size,
                hash_ok=hash_ok,
                size_ok=size_ok,
            )
        )

    # Detect unexpected files in the run directory
    manifest_names = set(manifest_by_name.keys()) | {manifest_name}
    unexpected: list[str] = []
    for child in sorted(run_path.iterdir()):
        if child.is_file() and child.name not in manifest_names:
            unexpected.append(child.name)

    return VerificationResult(
        run_dir=run_path,
        manifest_name=manifest_name,
        manifest_schema_version=MANIFEST_SCHEMA_VERSION,
        required_files=required,
        files=verifications,
        missing_required=missing_required,
        unexpected_files=unexpected,
        hash_mismatches=hash_mismatches,
        size_mismatches=size_mismatches,
    )


# ---------------------------------------------------------------------------
# Monthly usable-fraction gate
# ---------------------------------------------------------------------------


def monthly_usable_fraction_gate(
    per_month: dict[str, dict[str, Any]],
    threshold: float,
    *,
    policy: str = "reject",
    eval_months: set[str] | None = None,
    expected_intervals: dict[str, int] | None = None,
) -> MonthlyGateResult:
    """Evaluate per-month usable fractions against an explicit threshold.

    This is a **standalone helper** — it does not silently default to any
    universal threshold.  The caller must pass an explicit ``threshold`` in
    ``(0, 1]`` (exclusive zero, inclusive one).

    **The gate is only called by the integration when the threshold is > 0.**
    ``benchmark.py`` currently defaults ``min_usable_fraction=0.0`` (legacy
    inert behaviour — the gate is disabled).  When the integration decides
    to activate the gate (threshold > 0), it calls this helper.  The helper
    itself refuses zero because a threshold of zero would accept every month
    and is therefore vacuous.

    ``per_month`` keys are ``"YYYY-MM"`` strings, values must have ``"total"``
    and ``"usable"`` as non-negative ints (not bools) with ``usable <= total``.
    Missing keys, bools, negatives and ``usable > total`` all raise
    ``ValueError`` — strict validation prevents silent data errors.

    If ``eval_months`` is given, every month in it **must** have a
    corresponding bucket in ``per_month``.  A missing bucket is an error,
    not silently skipped — an evaluation month with no data is a data
    integrity problem, not a coverage gap.

    Args:
        per_month: ``{month_str: {"total": int, "usable": int}}`` mapping.
        threshold: Minimum usable fraction in ``(0, 1]``.  Must be explicit
            and finite — no silent defaults.  Zero is rejected (vacuous).
        policy: What to do with months below threshold:
            * ``"reject"`` — raise :class:`MonthlyQualityGateError` (with
              ``.result`` attached) listing every evaluation month below
              threshold.
            * ``"exclude"`` — return the below-threshold months in
              ``excluded_months`` for the caller to drop from evaluation.
            * ``"report"`` — return the detail without raising or excluding.
        eval_months: The set of months in the evaluation window.  Every
            month here must exist in ``per_month``.  ``None`` means all
            months in ``per_month`` are assessed.
        expected_intervals: Optional ``{month_str: int}`` giving the expected
            interval count for a full calendar month at the dataset's grid.
            Used to compute ``coverage`` on each :class:`MonthFraction`,
            declaring partial months explicitly rather than pretending
            full-month evidence.

    Returns:
        A :class:`MonthlyGateResult` with per-month detail.

    Raises:
        ValueError: ``threshold`` is not finite, not in ``(0, 1]``,
            ``policy`` is invalid, ``per_month`` buckets fail validation,
            or ``eval_months`` references a month not in ``per_month``.
        MonthlyQualityGateError: ``policy="reject"`` and at least one
            evaluation month is below threshold.  The ``.result`` attribute
            carries the full :class:`MonthlyGateResult`.
    """
    # --- parameter validation ------------------------------------------------
    if isinstance(threshold, bool) or not isinstance(threshold, (int, float)):
        raise ValueError(
            f"threshold must be a number in (0, 1], got {type(threshold).__name__}: {threshold!r}"
        )
    threshold = float(threshold)
    if not math.isfinite(threshold):
        raise ValueError(f"threshold must be finite, got {threshold!r}")
    if threshold <= 0.0 or threshold > 1.0:
        raise ValueError(
            f"threshold must be in (0, 1] (exclusive zero, inclusive one), got {threshold}"
        )

    valid_policies = ("reject", "exclude", "report")
    if policy not in valid_policies:
        raise ValueError(f"policy must be one of {valid_policies}, got {policy!r}")

    if not isinstance(per_month, dict):
        raise ValueError(f"per_month must be a dict, got {type(per_month).__name__}")

    if expected_intervals is not None and not isinstance(expected_intervals, dict):
        raise ValueError(
            f"expected_intervals must be a dict or None, got {type(expected_intervals).__name__}"
        )

    # --- eval_months must reference existing buckets -------------------------
    if eval_months is not None:
        missing_eval = sorted(m for m in eval_months if m not in per_month)
        if missing_eval:
            raise ValueError(f"eval_months references months not in per_month: {missing_eval}")

    # --- compute per-month fractions -----------------------------------------
    assessed_months = sorted(per_month.keys())
    if eval_months is not None:
        assessed_months = sorted(m for m in assessed_months if m in eval_months)

    per_month_results: list[MonthFraction] = []
    below: list[MonthFraction] = []

    for month in assessed_months:
        bucket = per_month[month]
        total, usable = _validate_bucket(month, bucket)
        fraction = usable / total if total > 0 else 0.0

        exp = None
        cov = None
        if expected_intervals is not None and month in expected_intervals:
            exp = expected_intervals[month]
            if exp > 0:
                cov = total / exp

        mf = MonthFraction(
            month=month,
            usable=usable,
            total=total,
            fraction=fraction,
            expected_intervals=exp,
            coverage=cov,
        )
        per_month_results.append(mf)

        if fraction < threshold:
            below.append(mf)

    # --- apply policy --------------------------------------------------------
    rejected: list[MonthFraction] = []
    excluded: list[MonthFraction] = []

    if policy == "reject":
        rejected = list(below)
    elif policy == "exclude":
        excluded = list(below)
    # "report" — no action, just return the detail

    result = MonthlyGateResult(
        threshold=threshold,
        policy=policy,
        per_month=per_month_results,
        below_threshold=below,
        rejected_months=rejected,
        excluded_months=excluded,
        eval_months_assessed=assessed_months,
    )

    if policy == "reject" and rejected:
        detail = "; ".join(
            f"{mf.month}: {mf.fraction:.4f} ({mf.usable}/{mf.total}) < {threshold}"
            for mf in rejected
        )
        raise MonthlyQualityGateError(
            f"monthly usable-fraction gate failed (threshold={threshold}, policy=reject): {detail}",
            result=result,
        )

    return result
