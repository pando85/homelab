"""Tests for paths.py — data-dir resolution, part naming, atomic write."""

from __future__ import annotations

import os
import stat
import threading
from datetime import UTC, datetime, timedelta, timezone
from pathlib import Path

import pytest

from energy_baseline import paths as paths_module
from energy_baseline.paths import (
    _find_git_root,
    _is_inside_git_tree,
    iter_parts,
    part_path,
    parts_dir,
    resolve_data_dir,
    write_part_atomic,
)

# ---------------------------------------------------------------------------
# resolve_data_dir
# ---------------------------------------------------------------------------


class TestResolveDataDir:
    def test_default_not_under_repo(self, monkeypatch: pytest.MonkeyPatch) -> None:
        """Default data dir must NOT be inside the git worktree."""
        monkeypatch.delenv("ENERGY_BASELINE_DATA_DIR", raising=False)
        result = resolve_data_dir()
        repo_root = Path("/home/agil/homelab").resolve()
        assert not str(result).startswith(str(repo_root)), (
            f"default data dir {result} is under repo root {repo_root}"
        )

    def test_default_expands_home(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.delenv("ENERGY_BASELINE_DATA_DIR", raising=False)
        result = resolve_data_dir()
        assert "~" not in str(result)
        assert result.name == "energy-baseline"

    def test_env_override(self, monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
        monkeypatch.setenv("ENERGY_BASELINE_DATA_DIR", str(tmp_path / "custom"))
        result = resolve_data_dir()
        assert result == (tmp_path / "custom").resolve()

    def test_cli_arg_wins(self, monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
        monkeypatch.setenv("ENERGY_BASELINE_DATA_DIR", str(tmp_path / "env"))
        result = resolve_data_dir(cli_arg=str(tmp_path / "cli"))
        assert result == (tmp_path / "cli").resolve()

    def test_expanduser_in_env(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setenv("ENERGY_BASELINE_DATA_DIR", "~/test-energy-data")
        result = resolve_data_dir()
        assert "~" not in str(result)

    def test_expanduser_in_cli_arg(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.delenv("ENERGY_BASELINE_DATA_DIR", raising=False)
        result = resolve_data_dir(cli_arg="~/test-energy-data")
        assert "~" not in str(result)

    def test_rejects_path_inside_git_tree(
        self, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
    ) -> None:
        """data_dir inside a git worktree must be rejected."""
        # Create a synthetic git repo
        repo = tmp_path / "synthetic-repo"
        repo.mkdir()
        (repo / ".git").mkdir()
        inside = repo / "data"
        monkeypatch.setenv("ENERGY_BASELINE_DATA_DIR", str(inside))
        with pytest.raises(ValueError, match="inside git worktree"):
            resolve_data_dir()

    def test_rejects_cli_inside_git_tree(self, tmp_path: Path) -> None:
        repo = tmp_path / "synthetic-repo"
        repo.mkdir()
        (repo / ".git").mkdir()
        inside = repo / "data"
        with pytest.raises(ValueError, match="inside git worktree"):
            resolve_data_dir(cli_arg=str(inside))

    def test_accepts_path_outside_git_tree(self, tmp_path: Path) -> None:
        """A path with no .git ancestor should be accepted."""
        outside = tmp_path / "safe-data"
        result = resolve_data_dir(cli_arg=str(outside))
        assert result == outside.resolve()


# ---------------------------------------------------------------------------
# _find_git_root / _is_inside_git_tree
# ---------------------------------------------------------------------------


class TestGitRootDetection:
    def test_find_git_root(self, tmp_path: Path) -> None:
        repo = tmp_path / "repo"
        repo.mkdir()
        (repo / ".git").mkdir()
        sub = repo / "a" / "b"
        sub.mkdir(parents=True)
        assert _find_git_root(sub) == repo.resolve()

    def test_no_git_root(self, tmp_path: Path) -> None:
        assert _find_git_root(tmp_path) is None

    def test_is_inside_git_tree(self, tmp_path: Path) -> None:
        repo = tmp_path / "repo"
        repo.mkdir()
        (repo / ".git").mkdir()
        assert _is_inside_git_tree(repo)
        assert not _is_inside_git_tree(tmp_path)


# ---------------------------------------------------------------------------
# part_path
# ---------------------------------------------------------------------------


class TestPartPath:
    def test_naming_format(self) -> None:
        start = datetime(2026, 1, 1, 0, 0, 0, tzinfo=UTC)
        end = datetime(2026, 1, 2, 0, 0, 0, tzinfo=UTC)
        p = part_path(
            data_dir=Path("/data"),
            source="prometheus",
            kind="prom-energy-kwh",
            start=start,
            end=end,
            content_hash_8="abcdef01",
        )
        assert p.name == "prom-energy-kwh-20260101T000000Z-20260102T000000Z-abcdef01.parquet"
        assert p.parent == parts_dir(Path("/data"), "prometheus")

    def test_naive_datetime_rejected(self) -> None:
        """Naive datetimes must be rejected, not silently treated as UTC."""
        start = datetime(2026, 6, 15, 12, 0, 0)  # naive
        end = datetime(2026, 6, 15, 13, 0, 0, tzinfo=UTC)
        with pytest.raises(ValueError, match="timezone-aware"):
            part_path(Path("/d"), "prometheus", "test", start, end, "00000000")

    def test_non_utc_timezone_converted(self) -> None:
        """Non-UTC timezones must be converted to UTC for filenames."""
        # UTC+2 -> should shift back 2 hours
        tz_plus2 = timezone(timedelta(hours=2))
        start = datetime(2026, 6, 15, 14, 0, 0, tzinfo=tz_plus2)  # = 12:00 UTC
        end = datetime(2026, 6, 15, 15, 0, 0, tzinfo=tz_plus2)  # = 13:00 UTC
        p = part_path(Path("/d"), "prometheus", "test", start, end, "00000000")
        assert "20260615T120000Z" in p.name
        assert "20260615T130000Z" in p.name


# ---------------------------------------------------------------------------
# iter_parts
# ---------------------------------------------------------------------------


class TestIterParts:
    def test_empty_when_missing(self, tmp_path: Path) -> None:
        assert iter_parts(tmp_path, "prometheus") == []

    def test_sorted_order(self, tmp_path: Path) -> None:
        pdir = parts_dir(tmp_path, "prometheus")
        pdir.mkdir(parents=True)
        names = [
            "prom-energy-kwh-20260103T000000Z-20260104T000000Z-aaaaaaaa.parquet",
            "prom-energy-kwh-20260101T000000Z-20260102T000000Z-bbbbbbbb.parquet",
            "prom-energy-kwh-20260102T000000Z-20260103T000000Z-cccccccc.parquet",
        ]
        for name in names:
            (pdir / name).write_bytes(b"x")

        result = iter_parts(tmp_path, "prometheus")
        assert [p.name for p in result] == sorted(names)


# ---------------------------------------------------------------------------
# write_part_atomic
# ---------------------------------------------------------------------------


class TestWritePartAtomic:
    def test_writes_and_returns_sha(self, tmp_path: Path) -> None:
        target = tmp_path / "sub" / "test.parquet"
        data = b"hello world"
        sha = write_part_atomic(target, data)
        assert target.read_bytes() == data
        assert len(sha) == 64  # full sha256 hex

    def test_idempotent_same_content(self, tmp_path: Path) -> None:
        target = tmp_path / "test.parquet"
        data = b"same content"
        sha1 = write_part_atomic(target, data)
        sha2 = write_part_atomic(target, data)
        assert sha1 == sha2
        assert target.read_bytes() == data

    def test_refuses_different_content(self, tmp_path: Path) -> None:
        target = tmp_path / "test.parquet"
        write_part_atomic(target, b"original")
        with pytest.raises(FileExistsError, match="refusing to overwrite"):
            write_part_atomic(target, b"different")

    def test_no_leftover_tmp_on_success(self, tmp_path: Path) -> None:
        target = tmp_path / "test.parquet"
        write_part_atomic(target, b"data")
        tmp_files = list(tmp_path.glob("*.tmp"))
        assert tmp_files == []
        # Also check hidden temp files
        hidden_tmps = list(tmp_path.glob(".*tmp"))
        assert hidden_tmps == []

    def test_file_permissions_0600(self, tmp_path: Path) -> None:
        target = tmp_path / "test.parquet"
        write_part_atomic(target, b"data")
        mode = stat.S_IMODE(os.stat(target).st_mode)
        assert mode == 0o600, f"expected 0o600, got {oct(mode)}"

    def test_new_dir_permissions_0700(self, tmp_path: Path) -> None:
        target = tmp_path / "new_dir" / "sub" / "test.parquet"
        write_part_atomic(target, b"data")
        for d in [tmp_path / "new_dir", tmp_path / "new_dir" / "sub"]:
            mode = stat.S_IMODE(os.stat(d).st_mode)
            assert mode == 0o700, f"expected 0o700 for {d}, got {oct(mode)}"

    def test_concurrent_no_clobber(self, tmp_path: Path) -> None:
        """Multiple threads writing same content must not corrupt."""
        target = tmp_path / "concurrent.parquet"
        data = b"concurrent content"
        errors: list[Exception] = []
        results: list[str] = []

        def writer() -> None:
            try:
                sha = write_part_atomic(target, data)
                results.append(sha)
            except Exception as e:
                errors.append(e)

        threads = [threading.Thread(target=writer) for _ in range(8)]
        for t in threads:
            t.start()
        for t in threads:
            t.join()

        assert errors == [], f"unexpected errors: {errors}"
        assert len(set(results)) == 1, "all threads should get same hash"
        assert target.read_bytes() == data

    def test_concurrent_different_content_raises(self, tmp_path: Path) -> None:
        """Concurrent writers with different content: one wins, others raise."""
        target = tmp_path / "race.parquet"
        errors: list[Exception] = []
        successes: list[str] = []
        barrier = threading.Barrier(4)

        def writer(data: bytes) -> None:
            try:
                barrier.wait(timeout=5)
                sha = write_part_atomic(target, data)
                successes.append(sha)
            except FileExistsError:
                pass  # expected for losers
            except Exception as e:
                errors.append(e)

        threads = [threading.Thread(target=writer, args=(f"data-{i}".encode(),)) for i in range(4)]
        for t in threads:
            t.start()
        for t in threads:
            t.join()

        assert errors == [], f"unexpected errors: {errors}"
        assert len(successes) >= 1, "at least one writer should succeed"


# ---------------------------------------------------------------------------
# Partial-write handling (private helper _write_all)
# ---------------------------------------------------------------------------


class TestPartialWrite:
    """_write_all must loop until every byte is flushed.

    ``os.write`` is not guaranteed to write the whole buffer in one call —
    the previous ``os.write(fd, data)`` silently truncated payloads on a short
    write.  These tests monkeypatch :func:`os.write` to return short counts and
    verify the published file still contains *all* bytes (no-clobber semantics
    preserved: hash must match).
    """

    def test_os_write_short_write_loops_until_complete(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        real_write = os.write
        calls: list[int] = []

        def short_write(fd: int, buf: bytes, *_args: object) -> int:
            calls.append(len(buf))
            # Never write more than 1 byte at a time (pathological case).
            return real_write(fd, buf[:1])

        monkeypatch.setattr(paths_module.os, "write", short_write)
        target = tmp_path / "partial.parquet"
        data = bytes(range(256)) * 4  # 1 KiB of structured content
        sha = write_part_atomic(target, data)

        assert target.read_bytes() == data, "short write must not truncate the file"
        import hashlib

        assert sha == hashlib.sha256(data).hexdigest()
        assert len(calls) >= len(data), "must have made at least one write per byte"

    def test_os_write_zero_progress_raises(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """A zero-byte return from os.write signals a dead fd and must raise."""

        def dead_write(*_args: object, **_kw: object) -> int:
            return 0

        monkeypatch.setattr(paths_module.os, "write", dead_write)
        target = tmp_path / "dead.parquet"
        with pytest.raises(OSError, match="returned 0"):
            write_part_atomic(target, b"never written")
        # No-clobber: nothing was published.
        assert not target.exists()

    def test_short_write_does_not_clobber_existing_different_content(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """Integrity holds under short writes: conflicting content still raises."""
        target = tmp_path / "noclobber.parquet"
        write_part_atomic(target, b"original content")

        real_write = os.write

        def short_write(fd: int, buf: bytes, *_args: object) -> int:
            return real_write(fd, buf[:2])

        monkeypatch.setattr(paths_module.os, "write", short_write)
        with pytest.raises(FileExistsError, match="refusing to overwrite"):
            write_part_atomic(target, b"totally different")
        assert target.read_bytes() == b"original content"

    def test_directory_fsync_is_best_effort(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """A failing directory fsync must not break the write (content is already durable)."""
        real_fsync = os.fsync
        fsynced: list[int] = []

        def selective_fsync(fd: int) -> None:
            fsynced.append(fd)
            # Directory fds fail; file fds succeed.
            try:
                mode = os.fstat(fd).st_mode
            except OSError:
                return
            import stat as stat_mod

            if stat_mod.S_ISDIR(mode):
                raise OSError("simulated fsync failure on directory")
            real_fsync(fd)

        monkeypatch.setattr(paths_module.os, "fsync", selective_fsync)
        target = tmp_path / "dir" / "fsync.parquet"
        sha = write_part_atomic(target, b"payload")
        assert target.read_bytes() == b"payload"
        assert len(sha) == 64
        assert len(fsynced) >= 2, "file fsync and directory fsync should both be attempted"
