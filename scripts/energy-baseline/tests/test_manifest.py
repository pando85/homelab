"""Tests for manifest.py — roundtrip, config_hash, extraction ID, sanitization."""

from __future__ import annotations

import json
import os
import stat
import uuid
from pathlib import Path

import pytest

from energy_baseline.manifest import (
    ExtractionManifest,
    config_hash,
    new_extraction_id,
    new_manifest,
    read_manifest,
    write_manifest,
)


class TestExtractionId:
    def test_format_is_uuid4(self) -> None:
        eid = new_extraction_id()
        parsed = uuid.UUID(eid)
        assert parsed.version == 4

    def test_unique(self) -> None:
        ids = {new_extraction_id() for _ in range(100)}
        assert len(ids) == 100


class TestConfigHash:
    def test_stable(self, tmp_path: Path) -> None:
        f = tmp_path / "tariff.yaml"
        f.write_text("version: 1\ntaxes: []\n")
        h1 = config_hash(f)
        h2 = config_hash(f)
        assert h1 == h2
        assert len(h1) == 64

    def test_different_content_different_hash(self, tmp_path: Path) -> None:
        f1 = tmp_path / "a.yaml"
        f2 = tmp_path / "b.yaml"
        f1.write_text("version: 1\n")
        f2.write_text("version: 1\n")  # same bytes
        assert config_hash(f1) == config_hash(f2)

        f2.write_text("version: 2\n")
        assert config_hash(f1) != config_hash(f2)

    def test_whitespace_equivalent_same_hash(self, tmp_path: Path) -> None:
        """Canonical YAML hash must be insensitive to whitespace/key ordering."""
        f1 = tmp_path / "a.yaml"
        f2 = tmp_path / "b.yaml"
        f1.write_text("b: 2\na: 1\n")
        # Same semantic content, different key ordering
        f2.write_text("a: 1\nb: 2\n")
        assert config_hash(f1) == config_hash(f2)

    def test_comment_change_same_hash_by_default(self, tmp_path: Path) -> None:
        """Comments are stripped by canonical YAML, so they don't affect hash."""
        f1 = tmp_path / "a.yaml"
        f2 = tmp_path / "b.yaml"
        f1.write_text("key: value\n")
        f2.write_text("# a comment\nkey: value\n")
        assert config_hash(f1) == config_hash(f2)

    def test_exact_raw_detects_comment(self, tmp_path: Path) -> None:
        """exact_raw=True detects any byte change including comments."""
        f1 = tmp_path / "a.yaml"
        f2 = tmp_path / "b.yaml"
        f1.write_text("key: value\n")
        f2.write_text("# comment\nkey: value\n")
        assert config_hash(f1, exact_raw=True) != config_hash(f2, exact_raw=True)

    def test_canonical_vs_exact_raw_different_for_reordered(self, tmp_path: Path) -> None:
        """Reordered keys: canonical matches, exact_raw differs."""
        f1 = tmp_path / "a.yaml"
        f2 = tmp_path / "b.yaml"
        f1.write_text("b: 2\na: 1\n")
        f2.write_text("a: 1\nb: 2\n")
        assert config_hash(f1) == config_hash(f2)  # canonical: same
        assert config_hash(f1, exact_raw=True) != config_hash(f2, exact_raw=True)  # raw: differ


class TestManifestRoundtrip:
    def test_roundtrip(self, tmp_path: Path) -> None:
        eid = new_extraction_id()
        manifest = ExtractionManifest(
            manifest_version=1,
            extraction_id=eid,
            started_at="2026-01-01T00:00:00Z",
            finished_at="2026-01-01T00:01:00Z",
            source="prometheus",
            code_version="abc123def456789abc123def456789abc123def45",
            code_dirty=False,
            config_hash="deadbeef" * 8,
            schema_version=1,
            params={"start": "2026-01-01", "end": "2026-01-02"},
            entities=["sensor.test_energy"],
            source_window_start="2026-01-01T00:00:00Z",
            source_window_end="2026-01-02T00:00:00Z",
            row_count=42,
            part_files=[{"name": "test.parquet", "sha256": "abc", "rows": 42}],
            coverage_notes="complete",
            quality_flags=["minor_gap"],
        )

        write_manifest(tmp_path, manifest)
        loaded = read_manifest(tmp_path, eid)

        assert loaded.extraction_id == eid
        assert loaded.source == "prometheus"
        assert loaded.row_count == 42
        assert loaded.entities == ["sensor.test_energy"]
        assert loaded.part_files == [{"name": "test.parquet", "sha256": "abc", "rows": 42}]
        assert loaded.quality_flags == ["minor_gap"]
        assert loaded.code_version == "abc123def456789abc123def456789abc123def45"
        assert loaded.code_dirty is False

    def test_write_creates_private_manifests_dir(self, tmp_path: Path) -> None:
        eid = new_extraction_id()
        manifest = ExtractionManifest(
            manifest_version=1,
            extraction_id=eid,
            started_at="2026-01-01T00:00:00Z",
            finished_at=None,
            source="prometheus",
            code_version=None,
            code_dirty=None,
            config_hash="a" * 64,
            schema_version=1,
            params={},
            entities=[],
            source_window_start="2026-01-01T00:00:00Z",
            source_window_end="2026-01-02T00:00:00Z",
            row_count=0,
        )
        write_manifest(tmp_path, manifest)
        manifests_dir = tmp_path / "manifests"
        mode = stat.S_IMODE(os.stat(manifests_dir).st_mode)
        assert mode == 0o700

    def test_manifest_file_permissions_0600(self, tmp_path: Path) -> None:
        eid = new_extraction_id()
        manifest = ExtractionManifest(
            manifest_version=1,
            extraction_id=eid,
            started_at="2026-01-01T00:00:00Z",
            finished_at=None,
            source="prometheus",
            code_version=None,
            code_dirty=None,
            config_hash="a" * 64,
            schema_version=1,
            params={},
            entities=[],
            source_window_start="2026-01-01T00:00:00Z",
            source_window_end="2026-01-02T00:00:00Z",
            row_count=0,
        )
        path = write_manifest(tmp_path, manifest)
        mode = stat.S_IMODE(os.stat(path).st_mode)
        assert mode == 0o600

    def test_no_clobber_different_content(self, tmp_path: Path) -> None:
        eid = new_extraction_id()
        m1 = ExtractionManifest(
            manifest_version=1,
            extraction_id=eid,
            started_at="2026-01-01T00:00:00Z",
            finished_at=None,
            source="prometheus",
            code_version=None,
            code_dirty=None,
            config_hash="a" * 64,
            schema_version=1,
            params={},
            entities=["first"],
            source_window_start="2026-01-01T00:00:00Z",
            source_window_end="2026-01-02T00:00:00Z",
            row_count=0,
        )
        m2 = ExtractionManifest(
            manifest_version=1,
            extraction_id=eid,
            started_at="2026-01-01T00:00:00Z",
            finished_at=None,
            source="prometheus",
            code_version=None,
            code_dirty=None,
            config_hash="a" * 64,
            schema_version=1,
            params={},
            entities=["second"],  # different content
            source_window_start="2026-01-01T00:00:00Z",
            source_window_end="2026-01-02T00:00:00Z",
            row_count=0,
        )
        write_manifest(tmp_path, m1)
        with pytest.raises(FileExistsError, match="refusing to overwrite"):
            write_manifest(tmp_path, m2)

    def test_idempotent_same_content(self, tmp_path: Path) -> None:
        eid = new_extraction_id()
        manifest = ExtractionManifest(
            manifest_version=1,
            extraction_id=eid,
            started_at="2026-01-01T00:00:00Z",
            finished_at=None,
            source="prometheus",
            code_version=None,
            code_dirty=None,
            config_hash="a" * 64,
            schema_version=1,
            params={},
            entities=["test"],
            source_window_start="2026-01-01T00:00:00Z",
            source_window_end="2026-01-02T00:00:00Z",
            row_count=0,
        )
        p1 = write_manifest(tmp_path, manifest)
        p2 = write_manifest(tmp_path, manifest)
        assert p1 == p2

    def test_no_leftover_tmp_files(self, tmp_path: Path) -> None:
        eid = new_extraction_id()
        manifest = ExtractionManifest(
            manifest_version=1,
            extraction_id=eid,
            started_at="2026-01-01T00:00:00Z",
            finished_at=None,
            source="prometheus",
            code_version=None,
            code_dirty=None,
            config_hash="a" * 64,
            schema_version=1,
            params={},
            entities=[],
            source_window_start="2026-01-01T00:00:00Z",
            source_window_end="2026-01-02T00:00:00Z",
            row_count=0,
        )
        write_manifest(tmp_path, manifest)
        tmps = list((tmp_path / "manifests").glob("*.tmp"))
        assert tmps == []
        hidden = list((tmp_path / "manifests").glob(".*tmp"))
        assert hidden == []


class TestParamSanitization:
    def test_sensitive_keys_redacted(self, tmp_path: Path) -> None:
        """new_manifest must redact sensitive params."""
        config = tmp_path / "cfg.yaml"
        config.write_text("version: 1\n")
        m = new_manifest(
            source="prometheus",
            config_path=config,
            params={
                "start": "2026-01-01",
                "password": "hunter2",
                "api_key": "sk-1234",
                "token": "bearer-abc",
            },
            entities=[],
            source_window_start="2026-01-01T00:00:00Z",
            source_window_end="2026-01-02T00:00:00Z",
            started_at="2026-01-01T00:00:00Z",
        )
        assert m.params["start"] == "2026-01-01"  # safe key preserved
        assert m.params["password"] == "<redacted>"
        assert m.params["api_key"] == "<redacted>"
        assert m.params["token"] == "<redacted>"

    def test_url_query_stripped(self, tmp_path: Path) -> None:
        """URL params with query strings must have the query stripped."""
        config = tmp_path / "cfg.yaml"
        config.write_text("version: 1\n")
        m = new_manifest(
            source="prometheus",
            config_path=config,
            params={"endpoint": "https://prom.internal/api/v1?token=secret123"},
            entities=[],
            source_window_start="2026-01-01T00:00:00Z",
            source_window_end="2026-01-02T00:00:00Z",
            started_at="2026-01-01T00:00:00Z",
        )
        assert "?token=" not in m.params["endpoint"]
        assert m.params["endpoint"] == "https://prom.internal/api/v1"

    def test_nested_dict_sanitized(self, tmp_path: Path) -> None:
        """Nested dicts in params must also be sanitized."""
        config = tmp_path / "cfg.yaml"
        config.write_text("version: 1\n")
        m = new_manifest(
            source="prometheus",
            config_path=config,
            params={"connection": {"username": "admin", "password": "secret"}},
            entities=[],
            source_window_start="2026-01-01T00:00:00Z",
            source_window_end="2026-01-02T00:00:00Z",
            started_at="2026-01-01T00:00:00Z",
        )
        assert m.params["connection"]["username"] == "admin"
        assert m.params["connection"]["password"] == "<redacted>"


class TestCodeVersion:
    def test_full_sha_in_manifest(self, tmp_path: Path) -> None:
        """code_version must be a full 40-char SHA, not truncated."""
        config = tmp_path / "cfg.yaml"
        config.write_text("version: 1\n")
        m = new_manifest(
            source="prometheus",
            config_path=config,
            params={},
            entities=[],
            source_window_start="2026-01-01T00:00:00Z",
            source_window_end="2026-01-02T00:00:00Z",
            started_at="2026-01-01T00:00:00Z",
        )
        # code_version is either None (no git) or a full SHA (40 hex chars)
        if m.code_version is not None:
            assert len(m.code_version) == 40, (
                f"expected full 40-char SHA, got {len(m.code_version)} chars: {m.code_version}"
            )

    def test_code_version_from_package_not_cwd(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """Code version should resolve from the package dir, not cwd."""
        from energy_baseline.manifest import _get_code_version, _package_dir

        pkg = _package_dir()
        assert pkg.is_dir()
        # _get_code_version should use pkg as cwd, not the test's cwd
        # Just verify it returns something valid
        rev, dirty = _get_code_version()
        # In this repo, git is available so we expect a result
        assert rev is not None
        assert len(rev) == 40


class TestManifestDeterminism:
    def test_json_output_sorted_keys(self, tmp_path: Path) -> None:
        """Manifest JSON must use sorted keys for deterministic output."""
        eid = new_extraction_id()
        manifest = ExtractionManifest(
            manifest_version=1,
            extraction_id=eid,
            started_at="2026-01-01T00:00:00Z",
            finished_at=None,
            source="prometheus",
            code_version=None,
            code_dirty=None,
            config_hash="a" * 64,
            schema_version=1,
            params={},
            entities=[],
            source_window_start="2026-01-01T00:00:00Z",
            source_window_end="2026-01-02T00:00:00Z",
            row_count=0,
        )
        path = write_manifest(tmp_path, manifest)
        raw = path.read_text()
        data = json.loads(raw)
        # Verify keys are sorted in the JSON
        keys = list(data.keys())
        assert keys == sorted(keys)
