"""Tests for artifact_integrity.py — output manifests, verification, and gate."""

from __future__ import annotations

import hashlib
import json
import os
from pathlib import Path

import pytest

from energy_baseline.artifact_integrity import (
    BENCHMARK_MANIFEST_NAME,
    BENCHMARK_REQUIRED_FILES,
    MANIFEST_SCHEMA_VERSION,
    REPORT_MANIFEST_NAME,
    REPORT_REQUIRED_FILES,
    ArtifactMissingError,
    FileVerification,
    ManifestSchemaError,
    ManifestSelfReferenceError,
    MissingManifestError,
    MonthlyGateResult,
    MonthlyQualityGateError,
    UnsafePathError,
    VerificationResult,
    finalize_run_outputs,
    monthly_usable_fraction_gate,
    verify_run_outputs,
)

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _write_file(path: Path, content: bytes | str) -> None:
    """Write content to a file, creating parent directories."""
    path.parent.mkdir(parents=True, exist_ok=True)
    if isinstance(content, str):
        content = content.encode("utf-8")
    path.write_bytes(content)


def _make_benchmark_run(run_dir: Path) -> Path:
    """Create a minimal benchmark run directory with all three files."""
    run_dir.mkdir(parents=True, exist_ok=True)
    _write_file(run_dir / "benchmark.json", '{"run_id": "test"}')
    _write_file(run_dir / "results.parquet", b"parquet-data-results")
    _write_file(run_dir / "schedules.parquet", b"parquet-data-schedules")
    return run_dir


def _make_full_run(run_dir: Path) -> Path:
    """Create a run with benchmark + report files."""
    _make_benchmark_run(run_dir)
    _write_file(run_dir / "report.json", '{"report": true}')
    _write_file(run_dir / "report.md", "# Report\n\nContent.")
    return run_dir


# ---------------------------------------------------------------------------
# finalize_run_outputs — benchmark stage
# ---------------------------------------------------------------------------


class TestFinalizeBenchmarkStage:
    def test_writes_benchmark_manifest(self, tmp_path: Path) -> None:
        run_dir = _make_benchmark_run(tmp_path / "run")
        manifest_path = finalize_run_outputs(run_dir, BENCHMARK_REQUIRED_FILES)

        assert manifest_path == run_dir / BENCHMARK_MANIFEST_NAME
        assert manifest_path.is_file()

        payload = json.loads(manifest_path.read_text())
        assert payload["schema_version"] == MANIFEST_SCHEMA_VERSION
        assert len(payload["files"]) == 3

        filenames = {e["filename"] for e in payload["files"]}
        assert filenames == set(BENCHMARK_REQUIRED_FILES)

    def test_hashes_are_correct(self, tmp_path: Path) -> None:
        run_dir = _make_benchmark_run(tmp_path / "run")
        finalize_run_outputs(run_dir, BENCHMARK_REQUIRED_FILES)

        payload = json.loads((run_dir / BENCHMARK_MANIFEST_NAME).read_text())
        for entry in payload["files"]:
            file_path = run_dir / entry["filename"]
            actual_sha = hashlib.sha256(file_path.read_bytes()).hexdigest()
            actual_size = file_path.stat().st_size
            assert entry["sha256"] == actual_sha
            assert entry["bytes"] == actual_size

    def test_excludes_itself(self, tmp_path: Path) -> None:
        """The manifest must NOT list itself as a hashed artifact."""
        run_dir = _make_benchmark_run(tmp_path / "run")
        finalize_run_outputs(run_dir, BENCHMARK_REQUIRED_FILES)

        payload = json.loads((run_dir / BENCHMARK_MANIFEST_NAME).read_text())
        filenames = {e["filename"] for e in payload["files"]}
        assert BENCHMARK_MANIFEST_NAME not in filenames

    def test_rejects_self_reference_in_required(self, tmp_path: Path) -> None:
        """Explicitly listing the manifest as required must raise."""
        run_dir = _make_benchmark_run(tmp_path / "run")
        with pytest.raises(ManifestSelfReferenceError, match="cannot hash itself"):
            finalize_run_outputs(
                run_dir,
                (*BENCHMARK_REQUIRED_FILES, BENCHMARK_MANIFEST_NAME),
            )

    def test_missing_required_file_raises(self, tmp_path: Path) -> None:
        run_dir = tmp_path / "run"
        run_dir.mkdir()
        _write_file(run_dir / "benchmark.json", "{}")
        # Missing results.parquet and schedules.parquet

        with pytest.raises(ArtifactMissingError, match="results.parquet"):
            finalize_run_outputs(run_dir, BENCHMARK_REQUIRED_FILES)

    def test_nonexistent_run_dir_raises(self, tmp_path: Path) -> None:
        with pytest.raises(FileNotFoundError, match="does not exist"):
            finalize_run_outputs(tmp_path / "nonexistent", BENCHMARK_REQUIRED_FILES)

    def test_empty_required_files_raises(self, tmp_path: Path) -> None:
        run_dir = _make_benchmark_run(tmp_path / "run")
        with pytest.raises(ValueError, match="must not be empty"):
            finalize_run_outputs(run_dir, [])

    def test_duplicate_required_files_raises(self, tmp_path: Path) -> None:
        run_dir = _make_benchmark_run(tmp_path / "run")
        with pytest.raises(ValueError, match="duplicate"):
            finalize_run_outputs(
                run_dir,
                ("benchmark.json", "benchmark.json", "results.parquet"),
            )

    def test_idempotent_same_content(self, tmp_path: Path) -> None:
        """Calling finalize twice with same content should not raise."""
        run_dir = _make_benchmark_run(tmp_path / "run")
        finalize_run_outputs(run_dir, BENCHMARK_REQUIRED_FILES)
        # Second call — same content, should be idempotent
        finalize_run_outputs(run_dir, BENCHMARK_REQUIRED_FILES)

    def test_no_clobber_different_content(self, tmp_path: Path) -> None:
        """If the manifest already exists with different content, raise."""
        run_dir = _make_benchmark_run(tmp_path / "run")
        finalize_run_outputs(run_dir, BENCHMARK_REQUIRED_FILES)

        # Tamper with an artifact file
        (run_dir / "benchmark.json").write_bytes(b'{"run_id": "changed"}')

        with pytest.raises(FileExistsError, match="refusing to overwrite"):
            finalize_run_outputs(run_dir, BENCHMARK_REQUIRED_FILES)

    def test_schema_version_in_manifest(self, tmp_path: Path) -> None:
        run_dir = _make_benchmark_run(tmp_path / "run")
        finalize_run_outputs(run_dir, BENCHMARK_REQUIRED_FILES)

        payload = json.loads((run_dir / BENCHMARK_MANIFEST_NAME).read_text())
        assert payload["schema_version"] == MANIFEST_SCHEMA_VERSION

    def test_no_absolute_run_dir_in_manifest(self, tmp_path: Path) -> None:
        """Manifest must NOT contain absolute run_dir (portable)."""
        run_dir = _make_benchmark_run(tmp_path / "run")
        finalize_run_outputs(run_dir, BENCHMARK_REQUIRED_FILES)

        payload = json.loads((run_dir / BENCHMARK_MANIFEST_NAME).read_text())
        assert "run_dir" not in payload

    def test_filenames_are_relative(self, tmp_path: Path) -> None:
        """All filenames in the manifest must be plain relative names."""
        run_dir = _make_benchmark_run(tmp_path / "run")
        finalize_run_outputs(run_dir, BENCHMARK_REQUIRED_FILES)

        payload = json.loads((run_dir / BENCHMARK_MANIFEST_NAME).read_text())
        for entry in payload["files"]:
            name = entry["filename"]
            assert "/" not in name
            assert "\\" not in name
            assert not os.path.isabs(name)
            assert ".." not in name

    def test_bytes_are_required_field(self, tmp_path: Path) -> None:
        """Every manifest entry must have filename, sha256, and bytes."""
        run_dir = _make_benchmark_run(tmp_path / "run")
        finalize_run_outputs(run_dir, BENCHMARK_REQUIRED_FILES)

        payload = json.loads((run_dir / BENCHMARK_MANIFEST_NAME).read_text())
        for entry in payload["files"]:
            assert "filename" in entry
            assert "sha256" in entry
            assert "bytes" in entry
            assert isinstance(entry["bytes"], int)
            assert isinstance(entry["sha256"], str)
            assert len(entry["sha256"]) == 64  # SHA-256 hex


# ---------------------------------------------------------------------------
# finalize_run_outputs — report stage (separate sidecar)
# ---------------------------------------------------------------------------


class TestFinalizeReportStage:
    def test_writes_report_manifest(self, tmp_path: Path) -> None:
        run_dir = _make_full_run(tmp_path / "run")
        manifest_path = finalize_run_outputs(
            run_dir,
            REPORT_REQUIRED_FILES,
            manifest_name=REPORT_MANIFEST_NAME,
        )

        assert manifest_path == run_dir / REPORT_MANIFEST_NAME
        assert manifest_path.is_file()

        payload = json.loads(manifest_path.read_text())
        filenames = {e["filename"] for e in payload["files"]}
        assert filenames == set(REPORT_REQUIRED_FILES)

    def test_report_manifest_excludes_itself(self, tmp_path: Path) -> None:
        run_dir = _make_full_run(tmp_path / "run")
        finalize_run_outputs(
            run_dir,
            REPORT_REQUIRED_FILES,
            manifest_name=REPORT_MANIFEST_NAME,
        )

        payload = json.loads((run_dir / REPORT_MANIFEST_NAME).read_text())
        filenames = {e["filename"] for e in payload["files"]}
        assert REPORT_MANIFEST_NAME not in filenames

    def test_report_manifest_does_not_hash_benchmark_files(self, tmp_path: Path) -> None:
        """Report sidecar hashes ONLY report files, not benchmark files."""
        run_dir = _make_full_run(tmp_path / "run")
        finalize_run_outputs(
            run_dir,
            REPORT_REQUIRED_FILES,
            manifest_name=REPORT_MANIFEST_NAME,
        )

        payload = json.loads((run_dir / REPORT_MANIFEST_NAME).read_text())
        filenames = {e["filename"] for e in payload["files"]}
        # Must NOT contain benchmark files
        assert "benchmark.json" not in filenames
        assert "results.parquet" not in filenames
        assert "schedules.parquet" not in filenames

    def test_invalid_manifest_name_rejected(self, tmp_path: Path) -> None:
        run_dir = _make_benchmark_run(tmp_path / "run")
        with pytest.raises(UnsafePathError, match="path separator"):
            finalize_run_outputs(
                run_dir,
                BENCHMARK_REQUIRED_FILES,
                manifest_name="subdir/manifest.json",
            )

    def test_manifest_name_with_traversal_rejected(self, tmp_path: Path) -> None:
        run_dir = _make_benchmark_run(tmp_path / "run")
        with pytest.raises(UnsafePathError, match="path separator|path traversal"):
            finalize_run_outputs(
                run_dir,
                BENCHMARK_REQUIRED_FILES,
                manifest_name="../manifest.json",
            )


# ---------------------------------------------------------------------------
# verify_run_outputs — benchmark stage
# ---------------------------------------------------------------------------


class TestVerifyBenchmarkStage:
    def test_clean_run_passes(self, tmp_path: Path) -> None:
        run_dir = _make_benchmark_run(tmp_path / "run")
        finalize_run_outputs(run_dir, BENCHMARK_REQUIRED_FILES)

        result = verify_run_outputs(run_dir, BENCHMARK_REQUIRED_FILES)
        assert result.ok is True
        assert result.missing_required == []
        assert result.hash_mismatches == []
        assert result.size_mismatches == []

    def test_missing_manifest_raises(self, tmp_path: Path) -> None:
        run_dir = _make_benchmark_run(tmp_path / "run")
        # No manifest written

        with pytest.raises(MissingManifestError, match="no outputs_manifest.json"):
            verify_run_outputs(run_dir, BENCHMARK_REQUIRED_FILES)

    def test_missing_file_detected(self, tmp_path: Path) -> None:
        run_dir = _make_benchmark_run(tmp_path / "run")
        finalize_run_outputs(run_dir, BENCHMARK_REQUIRED_FILES)

        # Delete one of the artifacts
        (run_dir / "results.parquet").unlink()

        result = verify_run_outputs(run_dir, BENCHMARK_REQUIRED_FILES)
        assert result.ok is False
        assert "results.parquet" in result.missing_required

    def test_hash_drift_detected(self, tmp_path: Path) -> None:
        run_dir = _make_benchmark_run(tmp_path / "run")
        finalize_run_outputs(run_dir, BENCHMARK_REQUIRED_FILES)

        # Tamper with file content
        (run_dir / "benchmark.json").write_bytes(b'{"tampered": true}')

        result = verify_run_outputs(run_dir, BENCHMARK_REQUIRED_FILES)
        assert result.ok is False
        assert "benchmark.json" in result.hash_mismatches

    def test_truncation_detected(self, tmp_path: Path) -> None:
        run_dir = _make_benchmark_run(tmp_path / "run")
        finalize_run_outputs(run_dir, BENCHMARK_REQUIRED_FILES)

        # Truncate a file (different content AND different size)
        (run_dir / "schedules.parquet").write_bytes(b"short")

        result = verify_run_outputs(run_dir, BENCHMARK_REQUIRED_FILES)
        assert result.ok is False
        assert "schedules.parquet" in result.hash_mismatches
        assert "schedules.parquet" in result.size_mismatches

    def test_unexpected_files_reported(self, tmp_path: Path) -> None:
        run_dir = _make_benchmark_run(tmp_path / "run")
        finalize_run_outputs(run_dir, BENCHMARK_REQUIRED_FILES)

        # Add an unexpected file
        _write_file(run_dir / "extra.log", "some log data")

        result = verify_run_outputs(run_dir, BENCHMARK_REQUIRED_FILES)
        assert "extra.log" in result.unexpected_files
        # Unexpected files are warnings, not failures
        assert result.ok is True

    def test_manifest_excludes_itself_from_verification(self, tmp_path: Path) -> None:
        """The manifest itself should not appear in file verifications."""
        run_dir = _make_benchmark_run(tmp_path / "run")
        finalize_run_outputs(run_dir, BENCHMARK_REQUIRED_FILES)

        result = verify_run_outputs(run_dir, BENCHMARK_REQUIRED_FILES)
        verified_names = {fv.filename for fv in result.files}
        assert BENCHMARK_MANIFEST_NAME not in verified_names

    def test_symlink_rejected(self, tmp_path: Path) -> None:
        """A symlinked artifact must be rejected during verification."""
        run_dir = _make_benchmark_run(tmp_path / "run")
        finalize_run_outputs(run_dir, BENCHMARK_REQUIRED_FILES)

        # Replace a real file with a symlink
        real_file = tmp_path / "real_results.parquet"
        real_file.write_bytes(b"parquet-data-results")
        (run_dir / "results.parquet").unlink()
        os.symlink(real_file, run_dir / "results.parquet")

        with pytest.raises(UnsafePathError, match="symlink"):
            verify_run_outputs(run_dir, BENCHMARK_REQUIRED_FILES)

    def test_manifest_with_self_reference_rejected(self, tmp_path: Path) -> None:
        """A manifest that lists itself must be rejected."""
        run_dir = _make_benchmark_run(tmp_path / "run")

        # Manually write a bad manifest that includes itself
        payload = {
            "schema_version": MANIFEST_SCHEMA_VERSION,
            "files": [
                {"filename": BENCHMARK_MANIFEST_NAME, "sha256": "a" * 64, "bytes": 100},
            ],
        }
        _write_file(run_dir / BENCHMARK_MANIFEST_NAME, json.dumps(payload))

        with pytest.raises(ManifestSelfReferenceError, match="lists itself"):
            verify_run_outputs(run_dir, BENCHMARK_REQUIRED_FILES)

    def test_unsupported_schema_version(self, tmp_path: Path) -> None:
        run_dir = _make_benchmark_run(tmp_path / "run")
        payload = {
            "schema_version": 999,
            "files": [],
        }
        _write_file(run_dir / BENCHMARK_MANIFEST_NAME, json.dumps(payload))

        with pytest.raises(ManifestSchemaError, match="unsupported.*schema version"):
            verify_run_outputs(run_dir, BENCHMARK_REQUIRED_FILES)

    def test_malformed_json(self, tmp_path: Path) -> None:
        run_dir = _make_benchmark_run(tmp_path / "run")
        _write_file(run_dir / BENCHMARK_MANIFEST_NAME, "not json {{{")

        with pytest.raises(ManifestSchemaError, match="not valid JSON"):
            verify_run_outputs(run_dir, BENCHMARK_REQUIRED_FILES)

    def test_path_traversal_in_manifest_rejected(self, tmp_path: Path) -> None:
        """A manifest entry with '..' must be rejected."""
        run_dir = _make_benchmark_run(tmp_path / "run")
        payload = {
            "schema_version": MANIFEST_SCHEMA_VERSION,
            "files": [
                {"filename": "../../../etc/passwd", "sha256": "a" * 64, "bytes": 100},
            ],
        }
        _write_file(run_dir / BENCHMARK_MANIFEST_NAME, json.dumps(payload))

        with pytest.raises(UnsafePathError, match="directory separator|path traversal"):
            verify_run_outputs(run_dir, BENCHMARK_REQUIRED_FILES)

    def test_absolute_path_in_manifest_rejected(self, tmp_path: Path) -> None:
        """A manifest entry with an absolute path must be rejected."""
        run_dir = _make_benchmark_run(tmp_path / "run")
        payload = {
            "schema_version": MANIFEST_SCHEMA_VERSION,
            "files": [
                {"filename": "/etc/passwd", "sha256": "a" * 64, "bytes": 100},
            ],
        }
        _write_file(run_dir / BENCHMARK_MANIFEST_NAME, json.dumps(payload))

        with pytest.raises(UnsafePathError, match="absolute"):
            verify_run_outputs(run_dir, BENCHMARK_REQUIRED_FILES)

    def test_subdirectory_in_manifest_rejected(self, tmp_path: Path) -> None:
        """A manifest entry with a subdirectory must be rejected."""
        run_dir = _make_benchmark_run(tmp_path / "run")
        payload = {
            "schema_version": MANIFEST_SCHEMA_VERSION,
            "files": [
                {"filename": "subdir/file.json", "sha256": "a" * 64, "bytes": 100},
            ],
        }
        _write_file(run_dir / BENCHMARK_MANIFEST_NAME, json.dumps(payload))

        with pytest.raises(UnsafePathError, match="directory separator"):
            verify_run_outputs(run_dir, BENCHMARK_REQUIRED_FILES)

    def test_verification_result_structure(self, tmp_path: Path) -> None:
        """Verify the structure of VerificationResult."""
        run_dir = _make_benchmark_run(tmp_path / "run")
        finalize_run_outputs(run_dir, BENCHMARK_REQUIRED_FILES)

        result = verify_run_outputs(run_dir, BENCHMARK_REQUIRED_FILES)
        assert isinstance(result, VerificationResult)
        assert result.run_dir == run_dir.resolve()
        assert result.manifest_name == BENCHMARK_MANIFEST_NAME
        assert result.manifest_schema_version == MANIFEST_SCHEMA_VERSION
        assert result.required_files == BENCHMARK_REQUIRED_FILES
        assert isinstance(result.files, list)
        assert all(isinstance(fv, FileVerification) for fv in result.files)

        for fv in result.files:
            assert fv.exists is True
            assert fv.ok is True
            assert fv.hash_ok is True
            assert fv.size_ok is True
            assert fv.actual_sha256 is not None
            assert fv.actual_bytes is not None

    def test_duplicate_entry_rejected(self, tmp_path: Path) -> None:
        """A manifest with duplicate filenames must be rejected."""
        run_dir = _make_benchmark_run(tmp_path / "run")
        payload = {
            "schema_version": MANIFEST_SCHEMA_VERSION,
            "files": [
                {"filename": "benchmark.json", "sha256": "a" * 64, "bytes": 100},
                {"filename": "benchmark.json", "sha256": "b" * 64, "bytes": 200},
            ],
        }
        _write_file(run_dir / BENCHMARK_MANIFEST_NAME, json.dumps(payload))

        with pytest.raises(ManifestSchemaError, match="duplicate"):
            verify_run_outputs(run_dir, BENCHMARK_REQUIRED_FILES)

    def test_invalid_sha256_rejected(self, tmp_path: Path) -> None:
        """SHA-256 must be exactly 64 lowercase hex chars."""
        run_dir = _make_benchmark_run(tmp_path / "run")
        payload = {
            "schema_version": MANIFEST_SCHEMA_VERSION,
            "files": [
                {"filename": "benchmark.json", "sha256": "not-a-valid-sha", "bytes": 100},
            ],
        }
        _write_file(run_dir / BENCHMARK_MANIFEST_NAME, json.dumps(payload))

        with pytest.raises(ManifestSchemaError, match="invalid sha256"):
            verify_run_outputs(run_dir, ("benchmark.json",))

    def test_bool_bytes_rejected(self, tmp_path: Path) -> None:
        """bytes must be int, not bool."""
        run_dir = _make_benchmark_run(tmp_path / "run")
        payload = {
            "schema_version": MANIFEST_SCHEMA_VERSION,
            "files": [
                {"filename": "benchmark.json", "sha256": "a" * 64, "bytes": True},
            ],
        }
        _write_file(run_dir / BENCHMARK_MANIFEST_NAME, json.dumps(payload))

        with pytest.raises(ManifestSchemaError, match="invalid bytes type"):
            verify_run_outputs(run_dir, ("benchmark.json",))

    def test_required_file_not_in_manifest_raises(self, tmp_path: Path) -> None:
        """If a required file is not in the manifest, raise."""
        run_dir = _make_benchmark_run(tmp_path / "run")
        # Write a manifest that only covers benchmark.json
        payload = {
            "schema_version": MANIFEST_SCHEMA_VERSION,
            "files": [
                {
                    "filename": "benchmark.json",
                    "sha256": hashlib.sha256(b'{"run_id": "test"}').hexdigest(),
                    "bytes": 17,
                },
            ],
        }
        _write_file(run_dir / BENCHMARK_MANIFEST_NAME, json.dumps(payload))

        # But require all three files
        with pytest.raises(ManifestSchemaError, match="not in the manifest"):
            verify_run_outputs(run_dir, BENCHMARK_REQUIRED_FILES)


# ---------------------------------------------------------------------------
# verify_run_outputs — report stage (separate sidecar)
# ---------------------------------------------------------------------------


class TestVerifyReportStage:
    def test_clean_report_stage_passes(self, tmp_path: Path) -> None:
        run_dir = _make_full_run(tmp_path / "run")
        finalize_run_outputs(
            run_dir,
            REPORT_REQUIRED_FILES,
            manifest_name=REPORT_MANIFEST_NAME,
        )

        result = verify_run_outputs(
            run_dir,
            REPORT_REQUIRED_FILES,
            manifest_name=REPORT_MANIFEST_NAME,
        )
        assert result.ok is True
        assert len(result.files) == 2

    def test_report_verification_ignores_benchmark_manifest(self, tmp_path: Path) -> None:
        """Report verification reads report_outputs_manifest.json, not outputs_manifest.json."""
        run_dir = _make_full_run(tmp_path / "run")
        # Only finalize benchmark stage
        finalize_run_outputs(run_dir, BENCHMARK_REQUIRED_FILES)

        # Report manifest doesn't exist yet
        with pytest.raises(MissingManifestError, match="report_outputs_manifest.json"):
            verify_run_outputs(
                run_dir,
                REPORT_REQUIRED_FILES,
                manifest_name=REPORT_MANIFEST_NAME,
            )


# ---------------------------------------------------------------------------
# Adversarial: benchmark then report stage extension
# ---------------------------------------------------------------------------


class TestStageExtension:
    """Benchmark stage finalized, then report stage added — both unchanged."""

    def test_benchmark_manifest_unchanged_after_report(self, tmp_path: Path) -> None:
        """After report stage, benchmark manifest still verifies."""
        run_dir = _make_full_run(tmp_path / "run")

        # Finalize benchmark stage
        finalize_run_outputs(run_dir, BENCHMARK_REQUIRED_FILES)
        benchmark_manifest_before = (run_dir / BENCHMARK_MANIFEST_NAME).read_bytes()

        # Finalize report stage
        finalize_run_outputs(
            run_dir,
            REPORT_REQUIRED_FILES,
            manifest_name=REPORT_MANIFEST_NAME,
        )

        # Benchmark manifest unchanged
        benchmark_manifest_after = (run_dir / BENCHMARK_MANIFEST_NAME).read_bytes()
        assert benchmark_manifest_before == benchmark_manifest_after

        # Both stages verify
        bench_result = verify_run_outputs(run_dir, BENCHMARK_REQUIRED_FILES)
        assert bench_result.ok is True

        report_result = verify_run_outputs(
            run_dir,
            REPORT_REQUIRED_FILES,
            manifest_name=REPORT_MANIFEST_NAME,
        )
        assert report_result.ok is True

    def test_report_manifest_does_not_affect_benchmark_verification(self, tmp_path: Path) -> None:
        """Adding report manifest doesn't change benchmark verification."""
        run_dir = _make_full_run(tmp_path / "run")
        finalize_run_outputs(run_dir, BENCHMARK_REQUIRED_FILES)

        # Benchmark verifies
        result1 = verify_run_outputs(run_dir, BENCHMARK_REQUIRED_FILES)
        assert result1.ok is True

        # Add report stage
        finalize_run_outputs(
            run_dir,
            REPORT_REQUIRED_FILES,
            manifest_name=REPORT_MANIFEST_NAME,
        )

        # Benchmark still verifies (report manifest is "unexpected" but OK)
        result2 = verify_run_outputs(run_dir, BENCHMARK_REQUIRED_FILES)
        assert result2.ok is True
        # Report manifest shows up as unexpected in benchmark verification
        assert REPORT_MANIFEST_NAME in result2.unexpected_files


# ---------------------------------------------------------------------------
# Adversarial: symlinks
# ---------------------------------------------------------------------------


class TestSymlinkAdversarial:
    def test_symlinked_run_dir_rejected(self, tmp_path: Path) -> None:
        """A symlinked run directory must be rejected."""
        real_dir = _make_benchmark_run(tmp_path / "real")
        link_dir = tmp_path / "link"
        os.symlink(real_dir, link_dir)

        with pytest.raises(UnsafePathError, match="symlink"):
            finalize_run_outputs(link_dir, BENCHMARK_REQUIRED_FILES)

    def test_symlinked_artifact_rejected_in_finalize(self, tmp_path: Path) -> None:
        """A symlinked artifact must be rejected during finalize."""
        run_dir = tmp_path / "run"
        run_dir.mkdir()
        _write_file(run_dir / "benchmark.json", "{}")
        _write_file(run_dir / "schedules.parquet", b"data")

        # Create a symlink for results.parquet
        real_file = tmp_path / "real_results.parquet"
        real_file.write_bytes(b"data")
        os.symlink(real_file, run_dir / "results.parquet")

        with pytest.raises(UnsafePathError, match="symlink"):
            finalize_run_outputs(run_dir, BENCHMARK_REQUIRED_FILES)

    def test_symlinked_manifest_rejected_in_verify(self, tmp_path: Path) -> None:
        """A symlinked manifest must be rejected during verify."""
        run_dir = _make_benchmark_run(tmp_path / "run")
        finalize_run_outputs(run_dir, BENCHMARK_REQUIRED_FILES)

        # Replace manifest with symlink
        real_manifest = tmp_path / "real_manifest.json"
        real_manifest.write_bytes((run_dir / BENCHMARK_MANIFEST_NAME).read_bytes())
        (run_dir / BENCHMARK_MANIFEST_NAME).unlink()
        os.symlink(real_manifest, run_dir / BENCHMARK_MANIFEST_NAME)

        with pytest.raises(UnsafePathError, match="symlink"):
            verify_run_outputs(run_dir, BENCHMARK_REQUIRED_FILES)


# ---------------------------------------------------------------------------
# Adversarial: no-vacuous manifests
# ---------------------------------------------------------------------------


class TestNoVacuousManifests:
    def test_empty_files_list_in_manifest(self, tmp_path: Path) -> None:
        """A manifest with no files is vacuous and must be rejected."""
        run_dir = _make_benchmark_run(tmp_path / "run")
        payload = {
            "schema_version": MANIFEST_SCHEMA_VERSION,
            "files": [],
        }
        _write_file(run_dir / BENCHMARK_MANIFEST_NAME, json.dumps(payload))

        # Require at least one file
        with pytest.raises(ManifestSchemaError, match="not in the manifest"):
            verify_run_outputs(run_dir, ("benchmark.json",))

    def test_manifest_with_only_unknown_files(self, tmp_path: Path) -> None:
        """A manifest with files not in required set is incomplete."""
        run_dir = _make_benchmark_run(tmp_path / "run")
        payload = {
            "schema_version": MANIFEST_SCHEMA_VERSION,
            "files": [
                {
                    "filename": "unknown.json",
                    "sha256": "a" * 64,
                    "bytes": 100,
                },
            ],
        }
        _write_file(run_dir / BENCHMARK_MANIFEST_NAME, json.dumps(payload))

        with pytest.raises(ManifestSchemaError, match="not in the manifest"):
            verify_run_outputs(run_dir, BENCHMARK_REQUIRED_FILES)


# ---------------------------------------------------------------------------
# monthly_usable_fraction_gate
# ---------------------------------------------------------------------------


class TestMonthlyUsableFractionGate:
    """Tests for the standalone monthly usable-fraction gate helper."""

    def _per_month(self, data: dict[str, tuple[int, int]]) -> dict[str, dict[str, int]]:
        """Helper to build per_month dict from {month: (usable, total)}."""
        return {
            month: {"usable": usable, "total": total} for month, (usable, total) in data.items()
        }

    # --- parameter validation ---

    def test_threshold_zero_rejected(self) -> None:
        with pytest.raises(ValueError, match="\\(0, 1\\]"):
            monthly_usable_fraction_gate({}, threshold=0.0)

    def test_threshold_negative_rejected(self) -> None:
        with pytest.raises(ValueError, match="\\(0, 1\\]"):
            monthly_usable_fraction_gate({}, threshold=-0.5)

    def test_threshold_above_one_rejected(self) -> None:
        with pytest.raises(ValueError, match="\\(0, 1\\]"):
            monthly_usable_fraction_gate({}, threshold=1.5)

    def test_threshold_nan_rejected(self) -> None:
        with pytest.raises(ValueError, match="finite"):
            monthly_usable_fraction_gate({}, threshold=float("nan"))

    def test_threshold_inf_rejected(self) -> None:
        with pytest.raises(ValueError, match="finite"):
            monthly_usable_fraction_gate({}, threshold=float("inf"))

    def test_threshold_string_rejected(self) -> None:
        with pytest.raises(ValueError, match="must be a number"):
            monthly_usable_fraction_gate({}, threshold="0.8")  # type: ignore[arg-type]

    def test_threshold_bool_rejected(self) -> None:
        with pytest.raises(ValueError, match="must be a number"):
            monthly_usable_fraction_gate({}, threshold=True)  # type: ignore[arg-type]

    def test_invalid_policy_rejected(self) -> None:
        with pytest.raises(ValueError, match="policy must be one of"):
            monthly_usable_fraction_gate({}, threshold=0.5, policy="invalid")

    def test_per_month_not_dict_rejected(self) -> None:
        with pytest.raises(ValueError, match="must be a dict"):
            monthly_usable_fraction_gate([], threshold=0.5)  # type: ignore[arg-type]

    # --- boundary: exactly at threshold ---

    def test_exactly_at_threshold_passes(self) -> None:
        """A month with fraction exactly equal to threshold passes."""
        per_month = self._per_month({"2026-01": (80, 100)})
        result = monthly_usable_fraction_gate(per_month, threshold=0.80, policy="reject")
        assert result.below_threshold == []
        assert result.rejected_months == []

    def test_just_below_threshold_rejected(self) -> None:
        """A month with fraction just below threshold is rejected."""
        per_month = self._per_month({"2026-01": (79, 100)})
        with pytest.raises(MonthlyQualityGateError, match="2026-01"):
            monthly_usable_fraction_gate(per_month, threshold=0.80, policy="reject")

    def test_threshold_one_requires_perfect(self) -> None:
        """threshold=1.0 requires 100% usable."""
        per_month = self._per_month({"2026-01": (99, 100)})
        with pytest.raises(MonthlyQualityGateError):
            monthly_usable_fraction_gate(per_month, threshold=1.0, policy="reject")

    def test_threshold_one_perfect_passes(self) -> None:
        per_month = self._per_month({"2026-01": (100, 100)})
        result = monthly_usable_fraction_gate(per_month, threshold=1.0, policy="reject")
        assert result.below_threshold == []

    # --- policies ---

    def test_reject_policy_raises(self) -> None:
        per_month = self._per_month({"2026-01": (50, 100)})
        with pytest.raises(MonthlyQualityGateError, match="policy=reject"):
            monthly_usable_fraction_gate(per_month, threshold=0.80, policy="reject")

    def test_exclude_policy_returns_excluded(self) -> None:
        per_month = self._per_month({"2026-01": (50, 100), "2026-02": (90, 100)})
        result = monthly_usable_fraction_gate(per_month, threshold=0.80, policy="exclude")
        assert len(result.excluded_months) == 1
        assert result.excluded_months[0].month == "2026-01"
        assert result.excluded_months[0].fraction == 0.50

    def test_report_policy_no_raise(self) -> None:
        """Report policy returns detail without raising."""
        per_month = self._per_month({"2026-01": (50, 100)})
        result = monthly_usable_fraction_gate(per_month, threshold=0.80, policy="report")
        assert len(result.below_threshold) == 1
        assert result.rejected_months == []
        assert result.excluded_months == []

    # --- eval_months filtering ---

    def test_only_eval_months_assessed(self) -> None:
        """Only months in eval_months are assessed."""
        per_month = self._per_month({"2026-01": (50, 100), "2026-02": (90, 100)})
        # Only assess February (which passes)
        result = monthly_usable_fraction_gate(
            per_month,
            threshold=0.80,
            policy="reject",
            eval_months={"2026-02"},
        )
        assert result.below_threshold == []
        assert result.eval_months_assessed == ["2026-02"]

    def test_eval_months_filters_correctly(self) -> None:
        per_month = self._per_month(
            {"2026-01": (50, 100), "2026-02": (90, 100), "2026-03": (30, 100)}
        )
        result = monthly_usable_fraction_gate(
            per_month,
            threshold=0.80,
            policy="report",
            eval_months={"2026-01", "2026-03"},
        )
        assert len(result.below_threshold) == 2
        assert result.eval_months_assessed == ["2026-01", "2026-03"]

    def test_eval_months_missing_bucket_errors(self) -> None:
        """eval_months referencing a month not in per_month must error."""
        per_month = self._per_month({"2026-01": (90, 100)})
        with pytest.raises(ValueError, match="not in per_month"):
            monthly_usable_fraction_gate(
                per_month,
                threshold=0.80,
                eval_months={"2026-01", "2026-02"},
            )

    # --- strict bucket validation ---

    def test_missing_total_key_rejected(self) -> None:
        per_month = {"2026-01": {"usable": 50}}
        with pytest.raises(ValueError, match="missing required key 'total'"):
            monthly_usable_fraction_gate(per_month, threshold=0.80)

    def test_missing_usable_key_rejected(self) -> None:
        per_month = {"2026-01": {"total": 100}}
        with pytest.raises(ValueError, match="missing required key 'usable'"):
            monthly_usable_fraction_gate(per_month, threshold=0.80)

    def test_bool_total_rejected(self) -> None:
        per_month = {"2026-01": {"total": True, "usable": 50}}
        with pytest.raises(ValueError, match="must be int"):
            monthly_usable_fraction_gate(per_month, threshold=0.80)

    def test_bool_usable_rejected(self) -> None:
        per_month = {"2026-01": {"total": 100, "usable": False}}
        with pytest.raises(ValueError, match="must be int"):
            monthly_usable_fraction_gate(per_month, threshold=0.80)

    def test_negative_total_rejected(self) -> None:
        per_month = {"2026-01": {"total": -1, "usable": 0}}
        with pytest.raises(ValueError, match="must be >= 0"):
            monthly_usable_fraction_gate(per_month, threshold=0.80)

    def test_negative_usable_rejected(self) -> None:
        per_month = {"2026-01": {"total": 100, "usable": -5}}
        with pytest.raises(ValueError, match="must be >= 0"):
            monthly_usable_fraction_gate(per_month, threshold=0.80)

    def test_usable_greater_than_total_rejected(self) -> None:
        per_month = {"2026-01": {"total": 50, "usable": 100}}
        with pytest.raises(ValueError, match="usable.*> total"):
            monthly_usable_fraction_gate(per_month, threshold=0.80)

    def test_non_dict_bucket_rejected(self) -> None:
        per_month = {"2026-01": [50, 100]}  # type: ignore[dict-item]
        with pytest.raises(ValueError, match="must be a dict"):
            monthly_usable_fraction_gate(per_month, threshold=0.80)

    # --- incomplete month ---

    def test_incomplete_month_below_threshold_rejected(self) -> None:
        """An incomplete month (partial data) below threshold is rejected,
        not silently accepted."""
        # 10 usable out of 50 total = 0.20, well below 0.80
        per_month = self._per_month({"2026-01": (10, 50)})
        with pytest.raises(MonthlyQualityGateError, match="2026-01"):
            monthly_usable_fraction_gate(per_month, threshold=0.80, policy="reject")

    def test_incomplete_month_excluded_not_silently_accepted(self) -> None:
        """An incomplete month below threshold is explicitly excluded,
        not silently accepted."""
        per_month = self._per_month({"2026-01": (10, 50)})
        result = monthly_usable_fraction_gate(per_month, threshold=0.80, policy="exclude")
        assert len(result.excluded_months) == 1
        assert result.excluded_months[0].month == "2026-01"

    def test_empty_month_zero_total(self) -> None:
        """A month with total=0 has fraction=0.0, which is below any threshold."""
        per_month = self._per_month({"2026-01": (0, 0)})
        with pytest.raises(MonthlyQualityGateError):
            monthly_usable_fraction_gate(per_month, threshold=0.80, policy="reject")

    # --- no silent default ---

    def test_no_silent_default_threshold(self) -> None:
        """The gate requires an explicit threshold — no default 0.80."""
        import inspect

        sig = inspect.signature(monthly_usable_fraction_gate)
        threshold_param = sig.parameters["threshold"]
        assert threshold_param.default is inspect.Parameter.empty, (
            "threshold must not have a default value — no silent universal 0.80"
        )

    # --- partial month coverage ---

    def test_expected_intervals_coverage(self) -> None:
        """expected_intervals computes coverage for partial months."""
        per_month = self._per_month({"2026-01": (80, 100)})
        # Expect 200 intervals for a full month, but only have 100
        result = monthly_usable_fraction_gate(
            per_month,
            threshold=0.80,
            expected_intervals={"2026-01": 200},
        )
        assert len(result.per_month) == 1
        mf = result.per_month[0]
        assert mf.expected_intervals == 200
        assert mf.coverage == 0.5  # 100/200

    def test_coverage_none_without_expected_intervals(self) -> None:
        """Without expected_intervals, coverage is None."""
        per_month = self._per_month({"2026-01": (80, 100)})
        result = monthly_usable_fraction_gate(per_month, threshold=0.80)
        assert result.per_month[0].coverage is None
        assert result.per_month[0].expected_intervals is None

    # --- result structure ---

    def test_result_structure(self) -> None:
        per_month = self._per_month({"2026-01": (90, 100), "2026-02": (70, 100)})
        result = monthly_usable_fraction_gate(per_month, threshold=0.80, policy="report")
        assert isinstance(result, MonthlyGateResult)
        assert result.threshold == 0.80
        assert result.policy == "report"
        assert len(result.per_month) == 2
        assert len(result.below_threshold) == 1
        assert result.below_threshold[0].month == "2026-02"
        assert result.below_threshold[0].usable == 70
        assert result.below_threshold[0].total == 100
        assert abs(result.below_threshold[0].fraction - 0.70) < 1e-9

    # --- exception carries result ---

    def test_rejected_exception_carries_result(self) -> None:
        """MonthlyQualityGateError must carry .result with full detail."""
        per_month = self._per_month({"2026-01": (50, 100)})
        with pytest.raises(MonthlyQualityGateError) as exc_info:
            monthly_usable_fraction_gate(per_month, threshold=0.80, policy="reject")

        exc = exc_info.value
        assert hasattr(exc, "result")
        assert isinstance(exc.result, MonthlyGateResult)
        assert exc.result.threshold == 0.80
        assert exc.result.policy == "reject"
        assert len(exc.result.rejected_months) == 1
        assert exc.result.rejected_months[0].month == "2026-01"

    # --- multiple months ---

    def test_multiple_months_all_pass(self) -> None:
        per_month = self._per_month(
            {"2026-01": (90, 100), "2026-02": (85, 100), "2026-03": (100, 100)}
        )
        result = monthly_usable_fraction_gate(per_month, threshold=0.80, policy="reject")
        assert result.below_threshold == []
        assert result.rejected_months == []

    def test_multiple_months_some_fail(self) -> None:
        per_month = self._per_month(
            {"2026-01": (90, 100), "2026-02": (50, 100), "2026-03": (30, 100)}
        )
        with pytest.raises(MonthlyQualityGateError) as exc_info:
            monthly_usable_fraction_gate(per_month, threshold=0.80, policy="reject")
        # Both failing months should be in the error message
        assert "2026-02" in str(exc_info.value)
        assert "2026-03" in str(exc_info.value)


# ---------------------------------------------------------------------------
# Adversarial: malformed manifest field types (compact, parameterized)
# ---------------------------------------------------------------------------

# (id, manifest_payload, expected_error, match) for a benchmark run on disk.
# These cover the reviewer-missing field-type cases the earlier suite skipped.
_BAD_MANIFESTS = [
    ("files-null", {"schema_version": 1, "files": None}, ManifestSchemaError, "not a list"),
    ("sv-string", {"schema_version": "1", "files": []}, ManifestSchemaError, "schema version"),
    ("sv-null", {"schema_version": None, "files": []}, ManifestSchemaError, "schema version"),
    # bool is an int subclass; True == 1 must NOT sneak past the equality check.
    ("sv-true", {"schema_version": True, "files": []}, ManifestSchemaError, "schema version"),
    ("sv-false", {"schema_version": False, "files": []}, ManifestSchemaError, "schema version"),
    (
        "entry-not-dict",
        {"schema_version": 1, "files": ["x"]},
        ManifestSchemaError,
        "not an object",
    ),
    (
        "fn-empty",
        {"schema_version": 1, "files": [{"filename": "", "sha256": "a" * 64, "bytes": 4}]},
        ManifestSchemaError,
        "missing 'filename'",
    ),
    (
        "fn-dot",
        {"schema_version": 1, "files": [{"filename": ".", "sha256": "a" * 64, "bytes": 4}]},
        UnsafePathError,
        "special directory",
    ),
    (
        "fn-dotdot",
        {"schema_version": 1, "files": [{"filename": "..", "sha256": "a" * 64, "bytes": 4}]},
        UnsafePathError,
        "special directory",
    ),
    (
        "fn-null",
        {"schema_version": 1, "files": [{"filename": None, "sha256": "a" * 64, "bytes": 4}]},
        ManifestSchemaError,
        "missing 'filename'",
    ),
    (
        "bytes-float",
        {
            "schema_version": 1,
            "files": [{"filename": "benchmark.json", "sha256": "a" * 64, "bytes": 4.0}],
        },
        ManifestSchemaError,
        "invalid bytes type",
    ),
    (
        "bytes-negative",
        {
            "schema_version": 1,
            "files": [{"filename": "benchmark.json", "sha256": "a" * 64, "bytes": -1}],
        },
        ManifestSchemaError,
        "negative bytes",
    ),
    (
        "bytes-bool",
        {
            "schema_version": 1,
            "files": [{"filename": "benchmark.json", "sha256": "a" * 64, "bytes": True}],
        },
        ManifestSchemaError,
        "invalid bytes type",
    ),
    (
        "bytes-missing",
        {
            "schema_version": 1,
            "files": [{"filename": "benchmark.json", "sha256": "a" * 64}],
        },
        ManifestSchemaError,
        "invalid bytes type",
    ),
    (
        "sha-missing",
        {"schema_version": 1, "files": [{"filename": "benchmark.json", "bytes": 4}]},
        ManifestSchemaError,
        "invalid sha256",
    ),
    (
        "sha-int",
        {"schema_version": 1, "files": [{"filename": "benchmark.json", "sha256": 123, "bytes": 4}]},
        ManifestSchemaError,
        "invalid sha256",
    ),
    (
        "sha-bool",
        {
            "schema_version": 1,
            "files": [{"filename": "benchmark.json", "sha256": True, "bytes": 4}],
        },
        ManifestSchemaError,
        "invalid sha256",
    ),
]


@pytest.mark.parametrize(
    "payload,exc,match",
    [(p, e, m) for _, p, e, m in _BAD_MANIFESTS],
    ids=[name for name, _, _, _ in _BAD_MANIFESTS],
)
def test_malformed_manifest_rejected(
    tmp_path: Path,
    payload: dict[str, object],
    exc: type[Exception],
    match: str,
) -> None:
    run_dir = _make_benchmark_run(tmp_path / "run")
    _write_file(run_dir / BENCHMARK_MANIFEST_NAME, json.dumps(payload))

    with pytest.raises(exc, match=match):
        verify_run_outputs(run_dir, BENCHMARK_REQUIRED_FILES)


# Non-string required_files entries must be rejected at the API boundary,
# before any filesystem access, for both finalize and verify.
_BAD_REQUIRED = [
    ("int", [1]),
    ("none", [None]),
    ("bool", [True]),
    ("nested-tuple", [("benchmark.json",)]),
]


@pytest.mark.parametrize("bad", [b for _, b in _BAD_REQUIRED], ids=[n for n, _ in _BAD_REQUIRED])
def test_required_files_non_str_rejected_finalize(tmp_path: Path, bad: list[object]) -> None:
    run_dir = _make_benchmark_run(tmp_path / "run")
    with pytest.raises(ValueError, match="must be str"):
        finalize_run_outputs(run_dir, bad)  # type: ignore[arg-type]


@pytest.mark.parametrize("bad", [b for _, b in _BAD_REQUIRED], ids=[n for n, _ in _BAD_REQUIRED])
def test_required_files_non_str_rejected_verify(tmp_path: Path, bad: list[object]) -> None:
    run_dir = _make_benchmark_run(tmp_path / "run")
    finalize_run_outputs(run_dir, BENCHMARK_REQUIRED_FILES)
    with pytest.raises(ValueError, match="must be str"):
        verify_run_outputs(run_dir, bad)  # type: ignore[arg-type]


# ---------------------------------------------------------------------------
# Integration: finalize then verify round-trip
# ---------------------------------------------------------------------------


class TestRoundTrip:
    """End-to-end: finalize then verify, including tampering detection."""

    def test_finalize_verify_clean(self, tmp_path: Path) -> None:
        run_dir = _make_benchmark_run(tmp_path / "run")
        finalize_run_outputs(run_dir, BENCHMARK_REQUIRED_FILES)
        result = verify_run_outputs(run_dir, BENCHMARK_REQUIRED_FILES)
        assert result.ok is True

    def test_finalize_verify_after_tamper(self, tmp_path: Path) -> None:
        run_dir = _make_benchmark_run(tmp_path / "run")
        finalize_run_outputs(run_dir, BENCHMARK_REQUIRED_FILES)

        # Tamper
        (run_dir / "benchmark.json").write_bytes(b'{"hacked": true}')

        result = verify_run_outputs(run_dir, BENCHMARK_REQUIRED_FILES)
        assert result.ok is False
        assert "benchmark.json" in result.hash_mismatches

    def test_finalize_verify_after_delete(self, tmp_path: Path) -> None:
        run_dir = _make_benchmark_run(tmp_path / "run")
        finalize_run_outputs(run_dir, BENCHMARK_REQUIRED_FILES)

        (run_dir / "results.parquet").unlink()

        result = verify_run_outputs(run_dir, BENCHMARK_REQUIRED_FILES)
        assert result.ok is False
        assert "results.parquet" in result.missing_required

    def test_finalize_verify_with_extra_files(self, tmp_path: Path) -> None:
        run_dir = _make_benchmark_run(tmp_path / "run")
        finalize_run_outputs(run_dir, BENCHMARK_REQUIRED_FILES)

        _write_file(run_dir / "debug.log", "debug info")

        result = verify_run_outputs(run_dir, BENCHMARK_REQUIRED_FILES)
        assert result.ok is True  # extra files are warnings
        assert "debug.log" in result.unexpected_files

    def test_manifest_written_after_artifacts(self, tmp_path: Path) -> None:
        """The manifest must be written AFTER all artifact files exist."""
        run_dir = _make_benchmark_run(tmp_path / "run")

        # Manifest does not exist before finalize
        assert not (run_dir / BENCHMARK_MANIFEST_NAME).exists()

        finalize_run_outputs(run_dir, BENCHMARK_REQUIRED_FILES)

        # Now it exists
        assert (run_dir / BENCHMARK_MANIFEST_NAME).exists()

        # And it correctly hashes the existing files
        result = verify_run_outputs(run_dir, BENCHMARK_REQUIRED_FILES)
        assert result.ok is True
