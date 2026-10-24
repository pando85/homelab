"""Tests for ``hack/validate_pre_migration.py`` against a synthetic BUILT dataset.

The runner is an experiment, not a library, so these tests exercise it the way an
operator does — as a subprocess over a real on-disk dataset with a real build
manifest — and add in-process unit tests for the two claims the artifacts make
about themselves:

* the causal rolling arm and the day-ahead arm share ONE forecast function, and
* a future perturbation leaves a past day's per-event forecast bit-identical
  (and, to show the distinction is real, DOES move the precomputed whole-array
  vintage the runner deliberately refuses to use).

The synthetic dataset spans 2024-12-01 .. 2025-12-31 on a uniform 15-minute UTC
grid, so:

* all four calendar-anchored representative windows (Jan / Mar-DST / Jul / Oct-DST)
  exist inside it and every requested arm must genuinely run — there is no
  "window not covered" escape, and the runner aborts if an arm does not run;
* the 2025-03-30 spring-forward day carries 92 intervals and the 2025-10-26
  fall-back day 100, which is what makes the DST windows real tests;
* April carries a non-usable energy quality, so the 0.80 gate has something to
  name while still counting every interval.

Everything is synthetic: no network, no credentials, no real data directory.
"""

from __future__ import annotations

import ast
import copy
import hashlib
import importlib.util
import json
import re
import shutil
import subprocess
import sys
from datetime import UTC, date, datetime, timedelta
from pathlib import Path
from typing import Any
from zoneinfo import ZoneInfo

import numpy as np
import pyarrow as pa
import pyarrow.parquet as pq
import pytest

REPO_ROOT = Path(__file__).resolve().parents[1]
RUNNER = REPO_ROOT / "hack" / "validate_pre_migration.py"
SRC_ROOT = REPO_ROOT / "src"
PKG_ROOT = SRC_ROOT / "energy_baseline"
MADRID = ZoneInfo("Europe/Madrid")

_INTERVAL_SECONDS = 900
_INTERVALS_PER_DAY = 96

# The dataset spans this range: enough history before Jan 15 for a 28-day lookback,
# and every representative window inside it.
_DATA_START = datetime(2024, 12, 1, 0, 0, tzinfo=UTC)
_DATA_DAYS = 396  # through 2025-12-31

#: arms that are cheap enough to run over all four windows in one test pass while
#: still including a genuine causal arm (rolling_causal_legacy)
_CHEAP_ARMS = (
    "day_ahead,rolling_frozen_legacy,rolling_causal_legacy,causal_price,rolling_causal_h96_c96"
)


# ---------------------------------------------------------------------------
# Runner module loaded in-process (importlib pattern used for hack scripts)
# ---------------------------------------------------------------------------


def _load_runner() -> Any:
    spec = importlib.util.spec_from_file_location("validate_pre_migration", str(RUNNER))
    assert spec is not None and spec.loader is not None
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


def _load_runner_at(path: Path, name: str) -> Any:
    """Load a *copy* of the runner (used to prove the source-hash map is derived)."""
    spec = importlib.util.spec_from_file_location(name, str(path))
    assert spec is not None and spec.loader is not None
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


# ---------------------------------------------------------------------------
# Provenance helpers
# ---------------------------------------------------------------------------

#: Every module the headline numbers depend on, named here on purpose.  A map that
#: silently drops one of them — the ``manifest.py`` omission a sibling study shipped,
#: which re-hashed 7 of 8 modules and noticed nothing — has to fail this list.
_REQUIRED_SOURCE_PATHS: tuple[str, ...] = (
    "hack/validate_pre_migration.py",
    "src/energy_baseline/__init__.py",
    "src/energy_baseline/artifact_integrity.py",
    "src/energy_baseline/battery.py",
    "src/energy_baseline/benchmark.py",
    "src/energy_baseline/config.py",
    "src/energy_baseline/cost.py",
    "src/energy_baseline/dispatch.py",
    "src/energy_baseline/manifest.py",
    "src/energy_baseline/oracle.py",
    "src/energy_baseline/paths.py",
    "src/energy_baseline/policies.py",
    "src/energy_baseline/rolling_horizon.py",
    "src/energy_baseline/schemas.py",
)

#: A module that reaches the solver only through a function-level import
#: (``benchmark._modules()``), so a snapshot of ``sys.modules`` taken after the
#: runner's own imports cannot see it.  Its presence is what proves the map is
#: traced from the source rather than scraped off the live interpreter.
_LAZY_IMPORT_PATH = "src/energy_baseline/cost.py"

_HEX64 = re.compile(r"^[0-9a-f]{64}$")
_GIT_SHA = re.compile(r"^[0-9a-f]{40}$")


def _git(repo: Path, *args: str) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        ["git", "-C", str(repo), *args],
        capture_output=True,
        text=True,
        timeout=120,
    )


def _resolve_first_party(name: str) -> Path | None:
    """Project-relative source file for a dotted ``energy_baseline`` name."""
    if name == "energy_baseline":
        return PKG_ROOT / "__init__.py"
    if not name.startswith("energy_baseline."):
        return None
    base = PKG_ROOT.joinpath(*name.split(".")[1:])
    for candidate in (base.with_suffix(".py"), base / "__init__.py"):
        if candidate.is_file():
            return candidate
    return None


def _test_side_import_closure(entry: Path = RUNNER) -> set[str]:
    """Re-derive the runner's transitive first-party import closure, in the test.

    A second, independent implementation of the trace on purpose: coverage asserted
    against the runner's own tracer would only prove it agrees with itself.
    ``ast.walk`` descends into function bodies, so a call-time import is caught.
    """
    found: set[Path] = set()
    pending = [entry]
    seen: set[Path] = set()
    while pending:
        path = pending.pop()
        if path in seen:
            continue
        seen.add(path)
        # A file's relative imports anchor on the package that contains it.
        anchor = (
            "" if path == entry else ".".join(path.relative_to(SRC_ROOT).with_suffix("").parts[:-1])
        )
        names: set[str] = set()
        for node in ast.walk(ast.parse(path.read_text(encoding="utf-8"))):
            if isinstance(node, ast.Import):
                names |= {alias.name for alias in node.names}
            elif isinstance(node, ast.ImportFrom):
                parts = anchor.split(".") if anchor else []
                if node.level:
                    if node.level > 1:
                        parts = parts[: -(node.level - 1)]
                    prefix = ".".join([*parts, *([node.module] if node.module else [])])
                else:
                    prefix = node.module or ""
                if prefix:
                    names.add(prefix)
                    names |= {f"{prefix}.{alias.name}" for alias in node.names}
            elif isinstance(node, ast.Call):
                callee = getattr(node.func, "id", None) or getattr(node.func, "attr", None)
                if callee in {"import_module", "__import__"} and node.args:
                    first = node.args[0]
                    if isinstance(first, ast.Constant) and isinstance(first.value, str):
                        names.add(first.value)
        for name in names:
            target = _resolve_first_party(name)
            if target is None:
                continue
            found.add(target)
            # Importing a submodule executes every parent package's __init__.
            parent = target.parent
            while parent != SRC_ROOT and SRC_ROOT in parent.parents:
                init = parent / "__init__.py"
                if init.is_file():
                    found.add(init)
                parent = parent.parent
            pending.append(target)
    return {p.relative_to(REPO_ROOT).as_posix() for p in found}


def _sys_module_source_paths(runner: Any) -> set[str]:
    """Project-relative paths of the ``energy_baseline`` modules live in this process."""
    paths: set[str] = set()
    for name in list(sys.modules):
        if name != "energy_baseline" and not name.startswith("energy_baseline."):
            continue
        target = _resolve_first_party(name)
        if target is not None:
            paths.add(target.relative_to(REPO_ROOT).as_posix())
    return paths


@pytest.fixture(scope="module")
def scratch_repo(tmp_path_factory: pytest.TempPathFactory) -> dict[str, Any]:
    """A throwaway COMMITTED git repo holding a copy of the package and the runner.

    ``--require-clean-tree`` can only be tested against a tree this fixture owns:
    the real working tree is shared with whatever else is in flight, and a test that
    asserted "the repo is clean" would be asserting about somebody else's checkout.
    """
    root = tmp_path_factory.mktemp("prov-repo")
    repo = root / "repo"
    repo.mkdir(parents=True)
    for cmd in (
        ["init", "-q"],
        ["config", "user.email", "prov@test.invalid"],
        ["config", "user.name", "provenance"],
    ):
        probe = subprocess.run(
            ["git", "-C", str(repo), *cmd],
            capture_output=True,
            text=True,
            timeout=120,
        )
        assert probe.returncode == 0, f"git command failed: {probe.stderr}"
    shutil.copytree(
        PKG_ROOT,
        repo / "src" / "energy_baseline",
        ignore=shutil.ignore_patterns("__pycache__", "*.pyc"),
    )
    (repo / "hack").mkdir(parents=True)
    shutil.copy2(RUNNER, repo / "hack" / "validate_pre_migration.py")
    # Byte-compiled files land in the copy as soon as it runs; they are not source.
    (repo / ".gitignore").write_text("__pycache__/\n*.pyc\n")
    _git(repo, "add", "-A")
    committed = _git(repo, "commit", "-q", "-m", "provenance fixture")
    assert committed.returncode == 0, committed.stderr
    head = _git(repo, "rev-parse", "HEAD").stdout.strip()
    assert _GIT_SHA.match(head), head
    return {
        "root": root,
        "repo": repo,
        "runner": repo / "hack" / "validate_pre_migration.py",
        "head": head,
    }


def _scratch_run(
    scratch: dict[str, Any],
    dataset: dict[str, Any],
    output: Path,
    *extra: str,
    stage: str = "full-year",
    arms: str = "causal_price",
) -> subprocess.CompletedProcess[str]:
    """Run the COPIED runner over the shared synthetic dataset."""
    cmd = [
        sys.executable,
        str(scratch["runner"]),
        "--dataset",
        str(dataset["dataset"]),
        "--config",
        str(dataset["tariff"]),
        "--output",
        str(output),
        "--stage",
        stage,
        "--arms",
        arms,
        "--time-limit",
        "2",
    ]
    if stage == "full-year":
        cmd.extend(
            [
                "--full-year-start",
                "2025-06-10T00:00:00+02:00",
                "--full-year-end",
                "2025-06-11T00:00:00+02:00",
            ]
        )
    elif stage == "representative":
        # Match the same window sizes as _run for consistency
        cmd.extend(
            [
                "--winter-days",
                "2",
                "--summer-days",
                "2",
                "--dst-days",
                "2",
            ]
        )
    cmd.extend(extra)
    return subprocess.run(
        cmd,
        capture_output=True,
        text=True,
        timeout=900,
    )


# ---------------------------------------------------------------------------
# Synthetic dataset helpers
# ---------------------------------------------------------------------------


def _write_tariff(path: Path) -> Path:
    """A tariff.yaml whose battery block satisfies ``resolve_battery_assumptions``."""
    import yaml

    data = {
        "version": 1,
        "taxes": [
            {
                "valid_from": "2024-01-01",
                "iee_pct": None,
                "iva_pct": None,
                "source": "synthetic-test",
            }
        ],
        "tariff_periods": {"timezone": "Europe/Madrid"},
        "battery": {
            "capacity_kwh": 5.12,
            "power_kw": 2.5,
            "round_trip_efficiency": 0.80,
            "soc_window_min": 0.05,
            "soc_window_max": 0.95,
            "initial_soc_frac": 0.20,
            "import_limit_kw": 5.0,
        },
    }
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(yaml.safe_dump(data, sort_keys=False))
    return path


def _price_load(day: date, hour: float) -> tuple[float, float]:
    """Deterministic (price, load) from the local date and clock hour.

    The per-day wobble makes no two days identical, so a forecast that reads its own
    target day is detectably different from one that does not.
    """
    wobble = 0.02 * ((day.toordinal() % 7) - 3) / 3.0
    if hour < 6:
        return 0.04 + wobble, 0.030
    if hour < 10:
        return 0.19 + wobble, 0.120
    if hour < 14:
        return 0.11 + wobble, 0.080
    if hour < 18:
        return 0.07 + wobble, 0.060
    if hour < 22:
        return 0.24 + wobble, 0.150
    return 0.05 + wobble, 0.045


def _analysis_table(*, start: datetime, days: int, dataset_id: str) -> pa.Table:
    """Build the ANALYSIS_INTERVAL table on a uniform 15-minute **UTC** grid.

    Timestamps advance by a fixed 900 s in UTC, so local (Europe/Madrid) calendar
    days come out at their true lengths: 92 on 2025-03-30 and 100 on 2025-10-26.
    Generating from a UTC instant rather than a Madrid one is deliberate — a
    Madrid-local start plus wall-clock arithmetic collapses the spring-forward hour
    and produces duplicate ``interval_start`` values, which is exactly what
    ``load_dataset`` refuses.

    April 2025 carries ``energy_quality="estimated"``, which the dataset's own
    usability mask rejects: it is the sparse month the quality gate must name.
    """
    n = days * _INTERVALS_PER_DAY
    starts: list[datetime] = []
    local_day: list[date] = []
    gross: list[float] = []
    prices: list[float] = []
    energy_quality: list[str] = []
    price_quality: list[str] = []

    cursor = start
    for _ in range(n):
        local = cursor.astimezone(MADRID)
        day = local.date()
        hour = local.hour + local.minute / 60.0
        price, load = _price_load(day, hour)
        starts.append(cursor)
        local_day.append(day)
        gross.append(load)
        prices.append(price)
        # April 2025: every other interval is "estimated" → ~50% usable, below 0.80
        if day.month == 4 and day.year == 2025 and (hour * 4) % 2 == 0:
            energy_quality.append("estimated")
            price_quality.append("interpolated")
        else:
            energy_quality.append("measured")
            price_quality.append("measured")
        cursor += timedelta(seconds=_INTERVAL_SECONDS)

    ends = [s + timedelta(seconds=_INTERVAL_SECONDS) for s in starts]
    return pa.table(
        {
            "interval_start": pa.array(starts, pa.timestamp("us", tz="UTC")),
            "interval_end": pa.array(ends, pa.timestamp("us", tz="UTC")),
            "tz_local": pa.array(["Europe/Madrid"] * n, pa.string()),
            "tariff_period": pa.array([3] * n, pa.int8()),
            "local_day": pa.array(local_day, pa.date32()),
            "dst_transition": pa.array([None] * n, pa.string()),
            "raw_grid_kwh": pa.array(gross, pa.float64()),
            "gross_kwh": pa.array(gross, pa.float64()),
            "battery_absent": pa.array([True] * n, pa.bool_()),
            "energy_quality": pa.array(energy_quality, pa.string()),
            "allocation_quality": pa.array(["exact"] * n, pa.string()),
            "sample_count": pa.array([4] * n, pa.int32()),
            "gap_seconds": pa.array([0.0] * n, pa.float64()),
            "price_eur_per_kwh": pa.array(prices, pa.float64()),
            "price_quality": pa.array(price_quality, pa.string()),
            "dataset_id": pa.array([dataset_id] * n, pa.string()),
        },
        schema=_analysis_schema(),
    )


def _analysis_schema() -> pa.Schema:
    from energy_baseline.schemas import ANALYSIS_INTERVAL

    return ANALYSIS_INTERVAL


def _write_dataset(root: Path, table: pa.Table, *, config_path: Path, dataset_id: str) -> Path:
    from energy_baseline.manifest import config_hash
    from energy_baseline.schemas import SCHEMA_VERSION

    dataset_dir = root / "analysis" / f"v{SCHEMA_VERSION}" / dataset_id
    parts = dataset_dir / "parts"
    parts.mkdir(parents=True, exist_ok=True)
    part_path = parts / f"analysis-{dataset_id}-00000000.parquet"
    pq.write_table(table, part_path)
    manifest: dict[str, Any] = {
        "manifest_version": 1,
        "dataset_id": dataset_id,
        "schema_version": SCHEMA_VERSION,
        "config_hash": config_hash(config_path),
        "code_version": "validate-pre-migration-test",
        "part_files": [
            {
                "path": str(part_path.relative_to(dataset_dir)),
                "sha256": hashlib.sha256(part_path.read_bytes()).hexdigest(),
                "rows": table.num_rows,
            }
        ],
        "requested_window": {
            "start": table.column("interval_start")[0].as_py().isoformat(),
            "end": table.column("interval_end")[-1].as_py().isoformat(),
        },
        "coverage": {"interval_count": table.num_rows, "grid_samples": table.num_rows * 4},
        "params": {"interval_seconds": _INTERVAL_SECONDS, "timezone": "Europe/Madrid"},
    }
    (dataset_dir / "build_manifest.json").write_text(json.dumps(manifest, indent=2))
    return dataset_dir


@pytest.fixture(scope="module")
def dataset(tmp_path_factory: pytest.TempPathFactory) -> dict[str, Any]:
    """One module-wide synthetic dataset (building it is the slow part)."""
    root = tmp_path_factory.mktemp("validate-pm")
    tariff = _write_tariff(root / "config" / "tariff.yaml")
    table = _analysis_table(start=_DATA_START, days=_DATA_DAYS, dataset_id="validate-pm-smoke")
    dataset_dir = _write_dataset(
        root / "data", table, config_path=tariff, dataset_id="validate-pm-smoke"
    )
    return {"root": root, "tariff": tariff, "dataset": dataset_dir, "table": table}


# ---------------------------------------------------------------------------
# Subprocess driver
# ---------------------------------------------------------------------------


def _run(
    setup: dict[str, Any],
    output: Path,
    *extra: str,
    stage: str = "representative",
    arms: str = _CHEAP_ARMS,
) -> subprocess.CompletedProcess[str]:
    cmd = [
        sys.executable,
        str(RUNNER),
        "--dataset",
        str(setup["dataset"]),
        "--config",
        str(setup["tariff"]),
        "--output",
        str(output),
        "--stage",
        stage,
        "--arms",
        arms,
        "--winter-days",
        "2",
        "--summer-days",
        "2",
        "--dst-days",
        "2",
        "--time-limit",
        "2",
        *extra,
    ]
    return subprocess.run(cmd, capture_output=True, text=True, timeout=1800)


def _assert_ok(proc: subprocess.CompletedProcess[str]) -> None:
    assert proc.returncode == 0, (
        f"runner exited {proc.returncode}\nSTDOUT:\n{proc.stdout}\nSTDERR:\n{proc.stderr}"
    )


@pytest.fixture(scope="module")
def representative_run(dataset: dict[str, Any], scratch_repo: dict[str, Any]) -> dict[str, Any]:
    """A full representative-stage run over all four windows with a causal arm."""
    out = dataset["root"] / "out-representative"
    proc = _scratch_run(scratch_repo, dataset, out, stage="representative", arms=_CHEAP_ARMS)
    _assert_ok(proc)
    return {
        "proc": proc,
        "output": out,
        "summary": json.loads((out / "validation.json").read_text()),
    }


# ---------------------------------------------------------------------------
# Parser / refusal behaviour
# ---------------------------------------------------------------------------


class TestOutputRefusal:
    def test_refuses_output_inside_repo(self, dataset: dict[str, Any]) -> None:
        forbidden = REPO_ROOT / "hack" / "_forbidden_output"
        proc = subprocess.run(
            [
                sys.executable,
                str(RUNNER),
                "--dataset",
                str(dataset["dataset"]),
                "--config",
                str(dataset["tariff"]),
                "--output",
                str(forbidden),
                "--stage",
                "representative",
                "--arms",
                "causal_price",
            ],
            capture_output=True,
            text=True,
            timeout=120,
        )
        assert proc.returncode != 0
        assert "inside the repository" in proc.stderr + proc.stdout
        if forbidden.exists():  # pragma: no cover - should never be created
            import shutil

            shutil.rmtree(forbidden)

    def test_refuses_nonempty_output(self, dataset: dict[str, Any]) -> None:
        out = dataset["root"] / "out-nonempty"
        out.mkdir(parents=True, exist_ok=True)
        (out / "stale.json").write_text("{}")
        proc = subprocess.run(
            [
                sys.executable,
                str(RUNNER),
                "--dataset",
                str(dataset["dataset"]),
                "--config",
                str(dataset["tariff"]),
                "--output",
                str(out),
                "--arms",
                "causal_price",
            ],
            capture_output=True,
            text=True,
            timeout=120,
        )
        assert proc.returncode != 0
        assert "not empty" in proc.stderr + proc.stdout

    def test_rejects_unknown_arm(self, dataset: dict[str, Any]) -> None:
        proc = subprocess.run(
            [
                sys.executable,
                str(RUNNER),
                "--dataset",
                str(dataset["dataset"]),
                "--config",
                str(dataset["tariff"]),
                "--output",
                str(dataset["root"] / "out-unknown-arm"),
                "--arms",
                "day_ahead,not_an_arm",
            ],
            capture_output=True,
            text=True,
            timeout=120,
        )
        assert proc.returncode != 0
        assert "unknown entries" in proc.stderr + proc.stdout

    def test_rejects_naive_iso_date(self, dataset: dict[str, Any]) -> None:
        proc = subprocess.run(
            [
                sys.executable,
                str(RUNNER),
                "--dataset",
                str(dataset["dataset"]),
                "--config",
                str(dataset["tariff"]),
                "--output",
                str(dataset["root"] / "out-naive"),
                "--stage",
                "full-year",
                "--full-year-start",
                "2025-04-01T00:00:00",
                "--full-year-end",
                "2025-04-08T00:00:00Z",
                "--arms",
                "causal_price",
            ],
            capture_output=True,
            text=True,
            timeout=120,
        )
        assert proc.returncode != 0
        assert "naive" in proc.stderr + proc.stdout

    def test_rejects_half_given_full_year_range(self, dataset: dict[str, Any]) -> None:
        """One endpoint alone must not silently fall back to the shipped range."""
        proc = subprocess.run(
            [
                sys.executable,
                str(RUNNER),
                "--dataset",
                str(dataset["dataset"]),
                "--config",
                str(dataset["tariff"]),
                "--output",
                str(dataset["root"] / "out-halfrange"),
                "--stage",
                "full-year",
                "--full-year-start",
                "2025-04-01T00:00:00Z",
                "--arms",
                "causal_price",
            ],
            capture_output=True,
            text=True,
            timeout=120,
        )
        assert proc.returncode != 0
        assert "given together" in proc.stderr + proc.stdout


# ---------------------------------------------------------------------------
# Every requested arm runs — including the causal ones
# ---------------------------------------------------------------------------


class TestAllRequestedArmsRun:
    def test_causal_arm_present_and_ran(self, representative_run: dict[str, Any]) -> None:
        summary = representative_run["summary"]
        assert len(summary["windows"]) == 4
        for record in summary["windows"]:
            arms = record["arms"]
            assert arms, f"{record['window']} produced no arms at all"
            # No arm may be a recorded "not available": the runner aborts instead,
            # so reaching this assertion at all is the evidence.
            for name, card in arms.items():
                assert card.get("ran") is True, f"{record['window']}/{name}: {card}"
            assert "rolling_causal_legacy" in arms

    def test_causal_arms_report_per_solve_forecast_audit(
        self, representative_run: dict[str, Any]
    ) -> None:
        """Causal arms must show the provider actually served events, per solve."""
        for record in representative_run["summary"]["windows"]:
            card = record["arms"]["rolling_causal_legacy"]
            audit = card["forecast_audit"]
            assert audit["method"] == "trailing_slot_mean_causal_event_specific"
            assert int(audit["events_served"]) > 0, "causal mode served no provider events"
            assert audit["first_event_training_usable_support"] is True
            assert int(audit["first_event_window_days"]) == 28
            assert audit["first_event_data_cutoff_before_local_day_start"] is True
            assert card["load_forecast_mode"] == "causal"

    def test_frozen_arm_is_explicitly_frozen(self, representative_run: dict[str, Any]) -> None:
        for record in representative_run["summary"]["windows"]:
            card = record["arms"]["rolling_frozen_legacy"]
            assert card["load_forecast_mode"] == "frozen"
            assert card["frozen_profile_meta"]["method"] == "trailing_slot_mean_frozen"

    def test_cadence_ladder_runs_h96_only(
        self, dataset: dict[str, Any], scratch_repo: dict[str, Any]
    ) -> None:
        """The 96-slot ladder arms run and share one horizon; solve counts ascend."""
        out = dataset["root"] / "out-ladder"
        proc = _scratch_run(
            scratch_repo,
            dataset,
            out,
            "--stage",
            "full-year",
            "--full-year-start",
            "2025-06-10T00:00:00+02:00",
            "--full-year-end",
            "2025-06-13T00:00:00+02:00",
            "--arms",
            "rolling_causal_h96_c96,rolling_causal_h96_c4,rolling_causal_h96_c1",
        )
        _assert_ok(proc)
        summary = json.loads((out / "validation.json").read_text())
        record = summary["windows"][0]
        arms = record["arms"]
        counts = {}
        for name in ("rolling_causal_h96_c96", "rolling_causal_h96_c4", "rolling_causal_h96_c1"):
            card = arms[name]
            assert card["ran"] is True, card
            assert card["horizon_slots"] == 96, "ladder must hold the horizon constant"
            assert card["load_forecast_mode"] == "causal"
            counts[name] = int(card["solve_count"])
        assert counts["rolling_causal_h96_c96"] < counts["rolling_causal_h96_c4"]
        assert counts["rolling_causal_h96_c4"] < counts["rolling_causal_h96_c1"]

    def test_horizon192_is_separate_from_the_ladder(
        self, dataset: dict[str, Any], scratch_repo: dict[str, Any]
    ) -> None:
        out = dataset["root"] / "out-h192"
        proc = _scratch_run(
            scratch_repo,
            dataset,
            out,
            "--include-192",
            "--stage",
            "full-year",
            "--full-year-start",
            "2025-06-10T00:00:00+02:00",
            "--full-year-end",
            "2025-06-12T00:00:00+02:00",
            "--arms",
            "rolling_causal_h96_c1",
        )
        _assert_ok(proc)
        arms = json.loads((out / "validation.json").read_text())["windows"][0]["arms"]
        assert arms["rolling_causal_h192_c1"]["horizon_slots"] == 192
        assert arms["rolling_causal_h192_c1"]["replan_interval_slots"] == 1
        assert arms["rolling_causal_h96_c1"]["horizon_slots"] == 96
        assert "horizon192" in json.dumps(
            json.loads((out / "validation.json").read_text())["cadence_design"]
        )

    def test_uncovered_window_aborts(self, dataset: dict[str, Any]) -> None:
        """A window outside the dataset is a hard error, never an empty result."""
        out = dataset["root"] / "out-uncovered"
        proc = _run(
            dataset,
            out,
            "--stage",
            "full-year",
            "--full-year-start",
            "2031-01-01T00:00:00Z",
            "--full-year-end",
            "2031-01-04T00:00:00Z",
            arms="causal_price",
        )
        assert proc.returncode != 0
        assert "no intervals" in proc.stderr + proc.stdout


# ---------------------------------------------------------------------------
# Forecast sharing: reviewer evidence
# ---------------------------------------------------------------------------


class TestForecastSharing:
    def test_provider_and_day_ahead_use_the_same_function(self) -> None:
        """Directly: the provider's forecast IS the canonical trailing-slot mean."""
        from energy_baseline.policies import contiguous_day_bounds, forecast_load_trailing_slot_mean
        from energy_baseline.rolling_horizon import CausalForecastProvider, RollingEvent

        n_days = 40
        starts = [
            _DATA_START + timedelta(seconds=_INTERVAL_SECONDS * i)
            for i in range(n_days * _INTERVALS_PER_DAY)
        ]
        local_days = [s.astimezone(MADRID).date() for s in starts]
        rng = np.random.default_rng(7)
        load = rng.random(len(starts)) * 0.2 + 0.03
        usable = np.ones(len(starts), dtype=bool)

        days = contiguous_day_bounds(local_days)
        canonical, _diag = forecast_load_trailing_slot_mean(
            load, days, window_days=28, usable=usable
        )
        provider = CausalForecastProvider(
            load, local_days, usable, starts, window_days=28, interval_seconds=900.0, tz=MADRID
        )
        target = days[30]
        event = RollingEvent(
            index=0,
            local_day=target[0],
            kind="afternoon",
            start_idx=target[1],
            plan_end_idx=target[2],
            exec_end_idx=target[2],
            prices_exact_end_idx=target[2],
        )
        per_event, meta = provider.forecast_for_event(event)

        # Same method name, same window, same function object on both sides.
        assert meta["shared_function_with_day_ahead"] is True
        assert int(meta["window_days"]) == 28
        # The cutoff is the event's own local-day start, so the provider reads one
        # day LESS history than the canonical per-target-day vintage does.
        assert int(meta["cutoff_idx"]) == target[1]
        assert bool(meta["data_cutoff_before_local_day_start"]) is True

        # Day 29 is inside BOTH vintages' trailing windows in the same way, so the
        # arrays agree everywhere the cutoff cannot matter.
        prior = days[29]
        assert np.allclose(per_event[prior[1] : prior[2]], canonical[prior[1] : prior[2]])
        # The provider must NOT equal the canonical value on the target day itself:
        # canonical day-30 sees day 29's data at day-29's cutoff, the provider sees
        # the same data truncated at day 30's start.  For the FIRST horizon day the
        # two cutoffs coincide, so agreement there is the real sharing proof.
        assert np.allclose(
            per_event[target[1] : target[2]], canonical[target[1] : target[2]], atol=1e-12
        )

    def test_artifact_records_the_sharing_audit(self, representative_run: dict[str, Any]) -> None:
        for record in representative_run["summary"]["windows"]:
            audit = record["causal_forecast_sharing_audit"]
            assert audit["shared_function"] == "policies.forecast_load_trailing_slot_mean"
            assert audit["provider_window_days"] == 28
            assert audit["first_event_training_usable_support"] is True
            assert audit["cutoff_strictly_before_event_start"] is True
            assert audit["difference_is_expected"]

    def test_no_frozen_profile_is_used_for_day_ahead(
        self, representative_run: dict[str, Any]
    ) -> None:
        """day_ahead must report the canonical entry point, not the frozen helper."""
        for record in representative_run["summary"]["windows"]:
            card = record["arms"]["day_ahead"]
            assert card["entry_point"] == "policies.policy_day_ahead"
            assert card["load_forecast_mode"] == "causal_advancing_per_local_day"
            assert card["forecast_basis"]["basis"] == "per_local_day_advancing_causal"
            assert card["forecast_basis"]["day_count"] >= 1
            assert "trailing_slot_mean" in card["forecast_basis"]["method"]
            # The frozen helper must not be reachable from this arm's record at all.
            assert "run_day_ahead_with_frozen_forecast" not in json.dumps(card)


# ---------------------------------------------------------------------------
# Causality: future perturbation
# ---------------------------------------------------------------------------


class TestFuturePerturbation:
    def _arrays(self, n_days: int = 40) -> tuple[list, list, np.ndarray, np.ndarray]:
        starts = [
            _DATA_START + timedelta(seconds=_INTERVAL_SECONDS * i)
            for i in range(n_days * _INTERVALS_PER_DAY)
        ]
        local_days = [s.astimezone(MADRID).date() for s in starts]
        rng = np.random.default_rng(11)
        load = rng.random(len(starts)) * 0.2 + 0.03
        usable = np.ones(len(starts), dtype=bool)
        return starts, local_days, load, usable

    def test_per_event_provider_forecast_is_insensitive_to_future_load(self) -> None:
        """Perturbing load at or after the event's day leaves that event's forecast equal."""
        from energy_baseline.policies import contiguous_day_bounds
        from energy_baseline.rolling_horizon import CausalForecastProvider, RollingEvent

        starts, local_days, load, usable = self._arrays()
        days = contiguous_day_bounds(local_days)
        target = days[30]

        def provider_forecast(series: np.ndarray) -> np.ndarray:
            provider = CausalForecastProvider(
                series,
                local_days,
                usable,
                starts,
                window_days=28,
                interval_seconds=900.0,
                tz=MADRID,
            )
            event = RollingEvent(
                index=0,
                local_day=target[0],
                kind="afternoon",
                start_idx=target[1],
                plan_end_idx=target[2],
                exec_end_idx=target[2],
                prices_exact_end_idx=target[2],
            )
            fc, _meta = provider.forecast_for_event(event)
            return fc[target[1] : target[2]]

        perturbed = load.copy()
        perturbed[target[2] :] += 0.5  # everything AFTER the target day

        base = provider_forecast(load)
        moved = provider_forecast(perturbed)
        assert np.array_equal(base, moved), (
            "a future perturbation changed the event forecast — the per-event causal "
            "provider is not actually causal"
        )

    def test_precomputed_whole_array_vintage_leaks_the_previous_day(self) -> None:
        """The unsafe precomputed profile is named and measured, not silently used.

        An event standing on day D with a horizon spanning D+1 must not know D's
        measured load.  A single whole-array per-target-day profile fitted up front
        does know it — D+1's slot means are fit over the window ending at D — so
        perturbing D moves the slice such a profile would hand to that event.  The
        per-event provider truncates history at the event's own local-day start, so
        the same perturbation leaves its D+1 slice bit-identical.

        This is the leak the runner refuses; the test pins the difference rather
        than trusting a comment, and it also pins that the day-ahead rung's own
        cutoff is still respected (perturbing D+1 never moves D+1's forecast).
        """
        from energy_baseline.policies import contiguous_day_bounds, forecast_load_trailing_slot_mean
        from energy_baseline.rolling_horizon import CausalForecastProvider, RollingEvent

        starts, local_days, load, usable = self._arrays()
        days = contiguous_day_bounds(local_days)
        event_day = days[29]  # the day the controller is standing in
        target = days[30]  # the day its horizon reaches

        perturbed = load.copy()
        perturbed[event_day[1] : event_day[2]] += 0.5

        def provider_slice(series: np.ndarray) -> np.ndarray:
            provider = CausalForecastProvider(
                series,
                local_days,
                usable,
                starts,
                window_days=28,
                interval_seconds=900.0,
                tz=MADRID,
            )
            event = RollingEvent(
                index=0,
                local_day=event_day[0],
                kind="evening",  # 20:30 on day D, planning across into D+1
                start_idx=event_day[1] + 26,
                plan_end_idx=target[2],
                exec_end_idx=target[1],
                prices_exact_end_idx=target[1],
            )
            fc, meta = provider.forecast_for_event(event)
            assert int(meta["cutoff_idx"]) == event_day[1]
            return fc[target[1] : target[2]]

        def whole_array_slice(series: np.ndarray) -> np.ndarray:
            fc, _diag = forecast_load_trailing_slot_mean(
                series, days, window_days=28, usable=usable
            )
            return fc[target[1] : target[2]]

        # The precomputed vintage READS the event's own day: perturbing day D moves
        # the D+1 slice it would hand to that event.
        base_array = whole_array_slice(load)
        moved_array = whole_array_slice(perturbed)
        assert not np.allclose(base_array, moved_array), (
            "expected the whole-array vintage to leak day D into D+1's estimate; if it "
            "no longer does, the sharing argument in the runner needs re-stating"
        )

        # The per-event provider refuses it: identical slice before and after.
        assert np.array_equal(provider_slice(load), provider_slice(perturbed)), (
            "the per-event causal provider moved when the event's own day was "
            "perturbed — that is the leak, not a fix"
        )

        # And the two vintages genuinely differ, so "shared function" is not being
        # proven against an identical array.
        assert not np.allclose(provider_slice(load), base_array)

        # Day-ahead's own cutoff still holds: perturbing the TARGET day leaves the
        # target's forecast alone in both vintages.
        on_target = load.copy()
        on_target[target[1] : target[2]] += 0.5
        assert np.allclose(whole_array_slice(load), whole_array_slice(on_target))
        assert np.array_equal(provider_slice(load), provider_slice(on_target))

    def test_runner_proves_causal_mode_served_events(self, dataset: dict[str, Any]) -> None:
        """Runner aborts if causal mode reports zero provider-served events (no fakes)."""
        src = RUNNER.read_text()
        assert "served 0" in src
        assert "events_served" in src
        assert "ImportError" in src or "imported at module scope" in src.lower()
        # No graceful-degradation path survived the rewrite.
        assert "build_causal_load_profile" not in src
        assert "_CAUSAL_LOAD_AVAILABLE" not in src
        assert "not available (per-event bug fix pending)" not in src


# ---------------------------------------------------------------------------
# Fairness: one spec, one mask, one baseline
# ---------------------------------------------------------------------------


class TestFairnessSharing:
    def test_one_battery_spec_and_opening_soc_for_every_arm(
        self, representative_run: dict[str, Any]
    ) -> None:
        summary = representative_run["summary"]
        battery = summary["battery"]
        assert battery["soc_min_frac"] == 0.05
        assert battery["soc_max_frac"] == 0.95
        assert battery["initial_soc_frac"] == 0.20
        assert "ONE BatterySpec" in summary["battery_note"]
        for record in summary["windows"]:
            opens = {card["initial_soc_kwh"] for card in record["arms"].values() if card.get("ran")}
            assert len(opens) == 1, f"arms opened at different SOC: {opens}"

    def test_every_arm_shares_the_same_baseline_and_mask(
        self, representative_run: dict[str, Any]
    ) -> None:
        for record in representative_run["summary"]["windows"]:
            baseline = record["baseline_cost_eur"]
            usable = record["eval_usable"]
            for card in record["arms"].values():
                assert card["baseline_cost_eur"] == pytest.approx(baseline)
                assert card["scored_usable_intervals"] == usable

    def test_meter_basis_holds_within_a_tolerance(self, representative_run: dict[str, Any]) -> None:
        basis = representative_run["summary"]["scoring_basis"]
        assert basis["max_meter_recompute_abs_dev_eur"] < 1e-6
        assert basis["max_cost_minus_meter_minus_inventory_eur"] < 1e-6
        assert basis["cost_is_meter_cost"] is True

    def test_end_soc_accounting_is_reported_and_inside_window(
        self, representative_run: dict[str, Any]
    ) -> None:
        acct = representative_run["summary"]["end_soc_accounting"]
        assert acct["checked"] is True
        assert acct["arms_with_terminal_soc_outside_window"] == []
        for record in representative_run["summary"]["windows"]:
            for card in record["arms"].values():
                block = card["end_soc_accounting"]
                assert block["terminal_within_window"] is True
                assert block["scoring_terminal_value_eur_per_kwh"] == 0.0
                assert block["soc_trace_min_kwh"] >= block["soc_min_kwh"] - 1e-9
                assert block["soc_trace_max_kwh"] <= block["soc_max_kwh"] + 1e-9

    def test_no_dominance_is_assumed(self, representative_run: dict[str, Any]) -> None:
        dom = representative_run["summary"]["dominance"]
        assert dom["assumed"] is False
        assert "certified" in dom["note"]


# ---------------------------------------------------------------------------
# Quality gate: report mode names April and drops nothing
# ---------------------------------------------------------------------------


class TestQualityReport:
    def test_full_year_range_covers_april_and_names_it(
        self, dataset: dict[str, Any], scratch_repo: dict[str, Any]
    ) -> None:
        out = dataset["root"] / "out-april"
        proc = _scratch_run(
            scratch_repo,
            dataset,
            out,
            "--stage",
            "full-year",
            "--full-year-start",
            "2025-04-10T00:00:00+02:00",
            "--full-year-end",
            "2025-04-16T00:00:00+02:00",
            "--arms",
            "causal_price",
        )
        _assert_ok(proc)
        summary = json.loads((out / "validation.json").read_text())
        gate = summary["monthly_quality_gate"]
        assert gate["threshold"] == 0.80
        assert gate["declared_mode"] == "report"
        assert gate["nothing_dropped"] is True
        assert gate["dropped_months"] == []
        flagged = {m["month"] for m in gate["below_threshold"]}
        assert "2025-04" in flagged, f"April not named: {gate}"
        # Every April interval still counts in the totals: nothing was dropped.
        month = next(m for m in gate["per_month"] if m["month"] == "2025-04")
        assert month["total"] == 6 * _INTERVALS_PER_DAY
        # ~50% usable (every other interval), below 0.80
        assert 0 < month["usable"] < month["total"]

    def test_gate_never_aborts_in_report_mode(self, representative_run: dict[str, Any]) -> None:
        # The representative windows never touch April, but the run still completes
        # with a declared report-mode gate over its own months.
        gate = representative_run["summary"]["monthly_quality_gate"]
        assert gate["declared_mode"] == "report"
        months = {m["month"] for m in gate["per_month"]}
        assert months  # the gate saw the windows it actually evaluated


# ---------------------------------------------------------------------------
# Local calendar / DST correctness
# ---------------------------------------------------------------------------


class TestLocalWindowsAndDST:
    def test_windows_are_madrid_local_midnights(self, representative_run: dict[str, Any]) -> None:
        for record in representative_run["summary"]["windows"]:
            start = datetime.fromisoformat(record["eval_start"].replace("Z", "+00:00"))
            local = start.astimezone(MADRID)
            assert (local.hour, local.minute) == (0, 0), record["window"]
            assert record["anchor_local_day"] == local.date().isoformat()

    def test_dst_windows_contain_days_of_the_expected_length(
        self, representative_run: dict[str, Any]
    ) -> None:
        by_name = {r["window"]: r for r in representative_run["summary"]["windows"]}
        spring = by_name["dst2025Mar"]
        # Mar 29 (96) + Mar 30 spring-forward (92) with --dst-days 2
        assert spring["eval_intervals"] == 96 + 92
        assert spring["local_days_in_window"] == 2
        autumn = by_name["dst2025Oct"]
        assert autumn["eval_intervals"] == 96 + 100
        assert autumn["local_days_in_window"] == 2
        winter = by_name["winter2025Jan"]
        assert winter["eval_intervals"] == 2 * _INTERVALS_PER_DAY

    def test_months_are_bucketed_on_the_local_grid(
        self, representative_run: dict[str, Any]
    ) -> None:
        for record in representative_run["summary"]["windows"]:
            totals = record["monthly_eval_totals"]
            assert totals
            assert sum(b["total"] for b in totals.values()) == record["eval_intervals"]
        spring = next(
            r for r in representative_run["summary"]["windows"] if r["window"] == "dst2025Mar"
        )
        assert "2025-03" in spring["monthly_eval_totals"]

    def test_lookback_is_recorded_and_clamped_honestly(
        self, representative_run: dict[str, Any]
    ) -> None:
        for record in representative_run["summary"]["windows"]:
            training = record["training"]
            assert training["lookback_days_requested"] == 28
            assert training["train_usable"] > 0
            if training["clamped_to_dataset_start"]:
                assert (
                    training["train_intervals_available"] < training["lookback_intervals_requested"]
                )


# ---------------------------------------------------------------------------
# Identity, outputs, and manifest separation
# ---------------------------------------------------------------------------


class TestArtifacts:
    def test_run_identity_is_complete(self, representative_run: dict[str, Any]) -> None:
        identity = representative_run["summary"]["run_identity"]
        assert identity["dataset_id"] == "validate-pm-smoke"
        assert identity["mode"] == "pre_migration_validation"
        assert identity["stage"] == "representative"
        assert len(identity["build_manifest_sha256"]) == 64
        assert identity["code_version"]
        assert identity["parameters"]["lookback_days"] == 28
        assert identity["cutoffs"]
        for cut in identity["cutoffs"]:
            assert cut["train_end_idx"] == cut["eval_start_idx"]

    def test_results_parquet_is_readable_and_covers_every_arm(
        self, representative_run: dict[str, Any]
    ) -> None:
        out = representative_run["output"]
        table = pq.read_table(out / "results.parquet")
        assert table.num_rows == 4 * len(
            representative_run["summary"]["run_identity"]["arms_requested"]
        )
        assert set(table.column("arm").to_pylist()) == set(
            representative_run["summary"]["run_identity"]["arms_requested"]
        )
        costs = table.column("cost_eur").to_pylist()
        assert all(c is not None and math.isfinite(c) for c in costs)

    def test_timing_is_separated_from_the_deterministic_manifest(
        self, representative_run: dict[str, Any]
    ) -> None:
        out = representative_run["output"]
        det = json.loads((out / "outputs_manifest.json").read_text())
        det_files = {entry["filename"] for entry in det["files"]}
        assert det_files == {"validation.json", "results.parquet"}
        assert "timing.json" not in det_files

        timing_manifest = json.loads((out / "timing_manifest.json").read_text())
        assert {e["filename"] for e in timing_manifest["files"]} == {"timing.json"}

        timing = json.loads((out / "timing.json").read_text())
        assert timing["deterministic_artifact_digests"] is None
        rows = timing["rows"]
        assert rows
        for row in rows:
            assert row["wall_time_s"] >= 0.0
            # one solve count per row where the arm reports one
        # deterministic artifacts must not carry wall time
        text = (out / "validation.json").read_text()
        assert "wall_time_s" not in text

    def test_manifest_hashes_match_the_files(self, representative_run: dict[str, Any]) -> None:
        out = representative_run["output"]
        for name in ("outputs_manifest.json", "timing_manifest.json"):
            manifest = json.loads((out / name).read_text())
            for entry in manifest["files"]:
                data = (out / entry["filename"]).read_bytes()
                assert hashlib.sha256(data).hexdigest() == entry["sha256"]
                assert len(data) == entry["bytes"]

    def test_no_private_output_written_into_the_repo(self) -> None:
        # Nothing from a run may land in the work tree; the default output hint is
        # outside it, and the docstring must say so.
        src = RUNNER.read_text()
        assert "/tmp/opencode/" in src or "outside the work tree" in src


# ---------------------------------------------------------------------------
# Column guard: zero-fill only where the mask already excludes
# ---------------------------------------------------------------------------


class TestNumericColumnGuard:
    def test_aborts_on_non_finite_usable_row(self) -> None:
        runner = _load_runner()
        table = pa.table(
            {
                "gross_kwh": pa.array([0.1, float("nan"), 0.3], pa.float64()),
            }
        )
        usable = np.array([True, True, True])
        with pytest.raises(SystemExit, match="non-finite at usable index 1"):
            runner._numeric_column(table, "gross_kwh", usable)

    def test_zero_fills_only_mask_excluded_rows(self) -> None:
        runner = _load_runner()
        table = pa.table({"gross_kwh": pa.array([0.1, None, float("inf"), 0.4], pa.float64())})
        usable = np.array([True, False, False, True])
        out = runner._numeric_column(table, "gross_kwh", usable)
        assert out[0] == pytest.approx(0.1)
        assert out[1] == 0.0 and out[2] == 0.0
        assert out[3] == pytest.approx(0.4)

    def test_null_on_a_usable_row_still_aborts(self) -> None:
        runner = _load_runner()
        table = pa.table({"gross_kwh": pa.array([None, 0.2], pa.float64())})
        with pytest.raises(SystemExit, match="non-finite at usable index 0"):
            runner._numeric_column(table, "gross_kwh", np.array([True, True]))


# ---------------------------------------------------------------------------
# Static guarantees about the runner itself
# ---------------------------------------------------------------------------


class TestRunnerContract:
    def test_no_duplicate_solver_or_physics(self) -> None:
        src = RUNNER.read_text()
        assert "def solve_" not in src
        assert "scipy" not in src.lower()
        for needed in (
            "from energy_baseline.battery import",
            "from energy_baseline.oracle import",
            "from energy_baseline.rolling_horizon import",
            "from energy_baseline.policies import",
        ):
            assert needed in src

    def test_causal_symbols_imported_at_module_scope(self) -> None:
        """No try/except around the causal API: a missing build dies at import."""
        src = RUNNER.read_text()
        assert "CausalForecastProvider" in src
        assert "LOAD_FORECAST_MODE_CAUSAL" in src
        assert "forecast_load_trailing_slot_mean" in src
        assert "try:\n    from energy_baseline.rolling_horizon import" not in src
        mod = _load_runner()
        assert mod.PROVIDER_WINDOW_DAYS == 28

    def test_only_declared_core_errors_are_recorded(self) -> None:
        mod = _load_runner()
        assert mod._POLICY_ERRORS and TypeError not in mod._POLICY_ERRORS
        assert any(name == "OracleError" for name in [e.__name__ for e in mod._POLICY_ERRORS])

    def test_quality_threshold_is_the_declared_gate(self) -> None:
        assert _load_runner().QUALITY_THRESHOLD == 0.80


# ---------------------------------------------------------------------------
# Provenance: the artifact must name the bytes that produced it
# ---------------------------------------------------------------------------


class TestCodeProvenance:
    """``code_version`` used to be the hardcoded distribution string.

    That made the two headline artifacts untieable from any commit: ``0.1.0`` is
    what ``pyproject.toml`` says, and it says the same thing at every revision.
    """

    def test_code_version_is_the_git_revision_not_the_distribution_string(
        self,
        representative_run: dict[str, Any],
        dataset: dict[str, Any],
        scratch_repo: dict[str, Any],
    ) -> None:
        from energy_baseline import __version__ as static_version

        # The run is in the scratch repo, so check its HEAD, not the real repo's
        head = _git(scratch_repo["repo"], "rev-parse", "HEAD")
        assert head.returncode == 0, head.stderr
        identity = representative_run["summary"]["run_identity"]
        # The exact string the defect recorded — the value a reader could pin nothing to.
        assert static_version == "0.1.0"
        assert identity["code_version"] != static_version
        assert re.fullmatch(r"[0-9a-f]{40}(-dirty)?", identity["code_version"]), identity
        assert identity["git_head"] == head.stdout.strip()

    def test_git_head_and_git_dirty_are_present_and_correctly_typed(
        self, representative_run: dict[str, Any]
    ) -> None:
        identity = representative_run["summary"]["run_identity"]
        assert "git_head" in identity and "git_dirty" in identity
        assert _GIT_SHA.match(str(identity["git_head"])), identity["git_head"]
        assert isinstance(identity["git_dirty"], bool)
        # The explicit fields must agree with the suffix, so nobody has to parse it.
        assert (identity["code_version"].endswith("-dirty")) is identity["git_dirty"]
        assert identity["code_version"] == identity["git_head"] + (
            "-dirty" if identity["git_dirty"] else ""
        )

    def test_git_dirty_reflects_the_real_working_tree(
        self, representative_run: dict[str, Any], scratch_repo: dict[str, Any]
    ) -> None:
        # The run is in the scratch repo, so check its dirty state, not the real repo's
        status = _git(scratch_repo["repo"], "status", "--porcelain", "--untracked-files=all")
        assert status.returncode == 0, status.stderr
        expected = bool(status.stdout.strip())
        identity = representative_run["summary"]["run_identity"]
        # Same convention as manifest._get_code_version: untracked files count as dirty.
        assert identity["git_dirty"] is expected

    def test_runner_no_longer_imports_the_distribution_version(self) -> None:
        src = RUNNER.read_text()
        assert "from energy_baseline import __version__ as code_version" not in src
        assert "from energy_baseline.manifest import" in src

    def test_no_git_records_the_sentinel_instead_of_raising(self, monkeypatch) -> None:
        """``nogit`` degrades to unknown fields; it never pretends to be clean."""
        runner = _load_runner()
        marker = runner.NO_GIT_MARKER
        monkeypatch.setattr(runner, "git_code_version", lambda: f"{marker}@energy-baseline==0.1.0")
        version, head, dirty = runner._git_identity()
        assert version.startswith(marker)
        assert head is None
        assert dirty is None
        with pytest.raises(SystemExit, match="nogit"):
            runner._refuse_unattributable_run(
                {"code_version": version, "git_head": head, "git_dirty": dirty}
            )


class TestSourceHashCoverage:
    def test_covers_every_module_the_runner_imports(
        self, representative_run: dict[str, Any]
    ) -> None:
        recorded = representative_run["summary"]["run_identity"]["source_hashes"]
        # Use the static closure trace, not sys.modules, because other tests in the
        # same pytest session may have imported modules the runner doesn't use.
        expected = _test_side_import_closure() | set(_REQUIRED_SOURCE_PATHS)
        missing = sorted(expected - set(recorded))
        assert not missing, f"imported by the runner but absent from source_hashes: {missing}"
        # A map trimmed to one hand-picked list is the defect this closes.
        assert len(recorded) >= len(_REQUIRED_SOURCE_PATHS)

    def test_catches_a_function_level_import_a_runtime_snapshot_misses(
        self, representative_run: dict[str, Any]
    ) -> None:
        """``cost`` arrives only through ``benchmark._modules()``, at call time.

        Its presence in the map is therefore evidence the trace reads the source:
        the probe below shows a snapshot of the live interpreter cannot see it.
        """
        recorded = representative_run["summary"]["run_identity"]["source_hashes"]
        assert _LAZY_IMPORT_PATH in recorded, "the trace must reach call-time imports"
        probe = subprocess.run(
            [
                sys.executable,
                "-c",
                "import importlib.util, sys;"
                f"spec = importlib.util.spec_from_file_location('p', {str(RUNNER)!r});"
                "mod = importlib.util.module_from_spec(spec); spec.loader.exec_module(mod);"
                "print('cost_imported=' + str('energy_baseline.cost' in sys.modules))",
            ],
            capture_output=True,
            text=True,
            timeout=300,
        )
        assert probe.returncode == 0, probe.stderr
        assert "cost_imported=False" in probe.stdout, (
            "the probe assumed a module-scope import never reaches cost.py; if it now "
            "does, this test's evidence needs re-stating, not deleting"
        )

    def test_includes_the_runner_itself_and_every_digest_is_a_full_sha256(
        self, representative_run: dict[str, Any]
    ) -> None:
        recorded = representative_run["summary"]["run_identity"]["source_hashes"]
        assert "hack/validate_pre_migration.py" in recorded
        for rel, digest in recorded.items():
            assert _HEX64.match(str(digest)), f"{rel}: {digest!r} is not 64 lowercase hex"
            # The digest is of the bytes at snapshot time, not necessarily current disk state.
            # If the file changed during the run (e.g., concurrent edits), the cross-check
            # will catch it; this test only verifies the format is correct.

    def test_a_new_import_is_pinned_without_anyone_editing_a_list(
        self, scratch_repo: dict[str, Any], dataset: dict[str, Any]
    ) -> None:
        """The map is derived: give the copy a new import and the map grows."""
        runner_copy = scratch_repo["runner"]
        original = runner_copy.read_text()
        probe_rel = "src/energy_baseline/_prov_probe.py"

        # First run: get baseline source hashes
        out_before = scratch_repo["root"] / "out-before"
        proc_before = _scratch_run(scratch_repo, dataset, out_before)
        _assert_ok(proc_before)
        before = json.loads((out_before / "validation.json").read_text())["run_identity"][
            "source_hashes"
        ]
        assert probe_rel not in before

        try:
            # Add the probe module
            (scratch_repo["repo"] / probe_rel).write_text("PROBE = 1\n")

            # Add an import of the probe module to the runner
            anchor = "from energy_baseline.config import load_config  # noqa: E402\n"
            assert anchor in original
            mutated = original.replace(
                anchor,
                anchor + "from energy_baseline import _prov_probe  # noqa: F401\n",
                1,
            )
            runner_copy.write_text(mutated)

            # Second run: the probe should now be in the source hashes
            out_after = scratch_repo["root"] / "out-after"
            proc_after = _scratch_run(scratch_repo, dataset, out_after)
            _assert_ok(proc_after)
            after = json.loads((out_after / "validation.json").read_text())["run_identity"][
                "source_hashes"
            ]

            assert probe_rel in after, "a freshly imported module went unrecorded"
            assert (
                after[probe_rel]
                == hashlib.sha256((scratch_repo["repo"] / probe_rel).read_bytes()).hexdigest()
            )
            assert set(after) - set(before) == {probe_rel}
        finally:
            runner_copy.write_text(original)
            (scratch_repo["repo"] / probe_rel).unlink(missing_ok=True)


class TestConfigDigests:
    def test_config_sha256_and_config_hash_match_an_independent_computation(
        self, representative_run: dict[str, Any], dataset: dict[str, Any]
    ) -> None:
        import io

        import yaml

        identity = representative_run["summary"]["run_identity"]
        tariff: Path = dataset["tariff"]
        raw = tariff.read_bytes()

        assert identity["config_path"]  # the bare path is still recorded
        assert identity["config_sha256"] == hashlib.sha256(raw).hexdigest()

        from energy_baseline.manifest import config_hash as canonical_config_hash

        assert identity["config_hash"] == canonical_config_hash(tariff)
        # ...and that function really is the declared convention, re-derived here.
        buf = io.StringIO()
        yaml.dump(
            yaml.safe_load(raw.decode("utf-8")),
            buf,
            default_flow_style=False,
            sort_keys=True,
            allow_unicode=True,
            width=1000,
        )
        assert identity["config_hash"] == hashlib.sha256(buf.getvalue().encode("utf-8")).hexdigest()
        # The two digests answer different questions, so they must not be the same value.
        assert identity["config_hash"] != identity["config_sha256"]

    def test_canonical_hash_survives_a_key_reorder_and_the_raw_digest_does_not(
        self, dataset: dict[str, Any], tmp_path: Path
    ) -> None:
        import yaml

        from energy_baseline.manifest import config_hash as canonical_config_hash

        original = Path(dataset["tariff"])
        data = yaml.safe_load(original.read_text())
        reordered_path = tmp_path / "tariff-reordered.yaml"
        reordered_path.write_text(
            yaml.safe_dump(dict(reversed(list(data.items()))), sort_keys=False)
        )
        assert reordered_path.read_bytes() != original.read_bytes()
        assert canonical_config_hash(reordered_path) == canonical_config_hash(original)
        assert (
            hashlib.sha256(reordered_path.read_bytes()).hexdigest()
            != hashlib.sha256(original.read_bytes()).hexdigest()
        )


class TestProvenanceCrossCheck:
    def test_artifact_records_start_and_end_snapshots_that_agree(
        self, representative_run: dict[str, Any]
    ) -> None:
        provenance = representative_run["summary"]["run_identity"]["provenance"]
        start, end, check = provenance["start"], provenance["end"], provenance["cross_check"]
        assert start["source_hashes"] == end["source_hashes"]
        assert start["git_head"] == end["git_head"]
        assert start["code_version"] == end["code_version"]
        assert start["config_sha256"] == end["config_sha256"]
        assert check["stable"] is True
        assert check["drift"] == []
        assert int(check["source_modules_compared"]) == len(start["source_hashes"])
        # The headline fields are the start snapshot, which the check just proved
        # is also the end snapshot.
        identity = representative_run["summary"]["run_identity"]
        assert identity["source_hashes"] == start["source_hashes"]
        assert identity["git_head"] == start["git_head"]

    def test_unchanged_snapshots_pass(self, dataset: dict[str, Any]) -> None:
        runner = _load_runner()
        start = runner._collect_provenance(Path(dataset["tariff"]))
        end = runner._collect_provenance(Path(dataset["tariff"]))
        assert runner._assert_provenance_stable(start, end)["stable"] is True

    @pytest.mark.parametrize(
        "field",
        ["code_version", "git_head", "config_sha256", "config_hash"],
    )
    def test_detects_a_changed_scalar(self, dataset: dict[str, Any], field: str) -> None:
        runner = _load_runner()
        start = runner._collect_provenance(Path(dataset["tariff"]))
        end = copy.deepcopy(start)
        end[field] = f"{end[field]}-moved"
        with pytest.raises(SystemExit, match=re.escape(field)):
            runner._assert_provenance_stable(start, end)

    def test_detects_a_simulated_mid_run_source_change(self, dataset: dict[str, Any]) -> None:
        runner = _load_runner()
        start = runner._collect_provenance(Path(dataset["tariff"]))
        end = copy.deepcopy(start)
        target = "src/energy_baseline/battery.py"
        assert _HEX64.match(start["source_hashes"][target])
        end["source_hashes"][target] = "0" * 64  # the monkeypatched "file was rewritten"
        with pytest.raises(SystemExit) as excinfo:
            runner._assert_provenance_stable(start, end)
        message = str(excinfo.value)
        assert target in message
        assert "0" * 64 in message, "must name the digest it found, not just complain"
        assert start["source_hashes"][target] in message

    def test_detects_a_module_imported_for_the_first_time_mid_run(
        self, dataset: dict[str, Any]
    ) -> None:
        runner = _load_runner()
        start = runner._collect_provenance(Path(dataset["tariff"]))
        end = copy.deepcopy(start)
        end["source_hashes"]["src/energy_baseline/late_arrival.py"] = "1" * 64
        with pytest.raises(SystemExit, match="first hashed at the END"):
            runner._assert_provenance_stable(start, end)

    def test_detects_a_module_that_disappears_mid_run(self, dataset: dict[str, Any]) -> None:
        runner = _load_runner()
        start = runner._collect_provenance(Path(dataset["tariff"]))
        end = copy.deepcopy(start)
        gone = sorted(end["source_hashes"])[1]
        del end["source_hashes"][gone]
        with pytest.raises(SystemExit, match="gone at the end"):
            runner._assert_provenance_stable(start, end)

    def test_drift_aborts_the_run_and_writes_nothing(
        self, dataset: dict[str, Any], tmp_path: Path, monkeypatch
    ) -> None:
        """The check is wired into the run, not merely available to it."""
        runner = _load_runner()
        real = runner._collect_provenance
        calls: list[str] = []

        def drifting(config_path: Path) -> dict[str, Any]:
            snapshot = dict(real(config_path))
            calls.append("snapshot")
            if len(calls) > 1:  # the END snapshot sees one file rewritten
                key = sorted(snapshot["source_hashes"])[1]
                snapshot["source_hashes"] = {**snapshot["source_hashes"], key: "f" * 64}
            return snapshot

        out = tmp_path / "out-drift"
        monkeypatch.setattr(runner, "_collect_provenance", drifting)
        monkeypatch.setattr(
            sys,
            "argv",
            [
                str(RUNNER),
                "--dataset",
                str(dataset["dataset"]),
                "--config",
                str(dataset["tariff"]),
                "--output",
                str(out),
                "--stage",
                "full-year",
                "--full-year-start",
                "2025-06-10T00:00:00+02:00",
                "--full-year-end",
                "2025-06-11T00:00:00+02:00",
                "--arms",
                "causal_price",
                "--time-limit",
                "2",
            ],
        )
        with pytest.raises(SystemExit) as excinfo:
            runner.main()
        assert "changed while this run was computing" in str(excinfo.value)
        assert len(calls) == 2, "both snapshots must be taken inside the run"
        assert out.exists() and list(out.iterdir()) == [], (
            "a drifted run must leave an empty directory, never a partial artifact"
        )


class TestRequireCleanTree:
    def test_refuses_a_dirty_tree_and_creates_no_output_directory(
        self, scratch_repo: dict[str, Any], dataset: dict[str, Any], tmp_path: Path
    ) -> None:
        repo: Path = scratch_repo["repo"]
        stray = repo / "untracked-work-in-progress.md"
        stray.write_text("edits that are in no commit\n")
        out = tmp_path / "out-refused"
        try:
            proc = _scratch_run(scratch_repo, dataset, out, "--require-clean-tree")
            assert proc.returncode != 0, proc.stdout + proc.stderr
            assert "dirty working tree" in proc.stderr + proc.stdout
            assert not out.exists(), (
                "--require-clean-tree must fail before the output directory exists"
            )
        finally:
            stray.unlink(missing_ok=True)

    def test_accepted_on_a_clean_tree_and_citable_from_the_commit_alone(
        self, scratch_repo: dict[str, Any], dataset: dict[str, Any], tmp_path: Path
    ) -> None:
        out = tmp_path / "out-clean"
        proc = _scratch_run(scratch_repo, dataset, out, "--require-clean-tree")
        assert proc.returncode == 0, proc.stdout + proc.stderr
        identity = json.loads((out / "validation.json").read_text())["run_identity"]
        head = scratch_repo["head"]
        assert identity["git_dirty"] is False
        assert identity["git_head"] == head
        assert identity["code_version"] == head  # no suffix on a clean tree
        assert identity["parameters"]["require_clean_tree"] is True

        # The whole point: a reviewer holding only this SHA can read back every byte.
        shown_head = _git(scratch_repo["repo"], "rev-parse", "HEAD").stdout.strip()
        assert shown_head == head
        for rel, digest in identity["source_hashes"].items():
            blob = subprocess.run(
                ["git", "-C", str(scratch_repo["repo"]), "show", f"HEAD:{rel}"],
                capture_output=True,
                timeout=120,
            )
            assert blob.returncode == 0, f"{rel}: not in the commit the artifact names"
            assert digest == hashlib.sha256(blob.stdout).hexdigest(), rel

    def test_flag_is_default_off_and_a_dirty_run_says_so(
        self, scratch_repo: dict[str, Any], dataset: dict[str, Any], tmp_path: Path
    ) -> None:
        """Development keeps working: no flag, dirty tree, artifact admits it."""
        repo: Path = scratch_repo["repo"]
        stray = repo / "dirty_dev_helper.py"
        stray.write_text("HELPER = 1\n")
        out = tmp_path / "out-dirty-ok"
        try:
            proc = _scratch_run(scratch_repo, dataset, out)
            assert proc.returncode == 0, proc.stdout + proc.stderr
            identity = json.loads((out / "validation.json").read_text())["run_identity"]
            assert identity["parameters"]["require_clean_tree"] is False
            assert identity["git_dirty"] is True
            assert identity["code_version"] == scratch_repo["head"] + "-dirty"
            assert identity["provenance"]["cross_check"]["stable"] is True
        finally:
            stray.unlink(missing_ok=True)


class TestByteReproducibility:
    def test_two_identical_invocations_produce_identical_deterministic_artifacts(
        self, dataset: dict[str, Any], scratch_repo: dict[str, Any]
    ) -> None:
        """Provenance is pinned content, so it must not break bit-for-bit reruns.

        Nothing here reads a clock: if a timestamp ever leaks into ``validation.json``
        the two digests below stop matching, which is exactly the regression this
        guards against alongside the separated ``timing.json``.
        """
        arms = "day_ahead,causal_price"
        first = dataset["root"] / "out-repro-a"
        second = dataset["root"] / "out-repro-b"
        for out in (first, second):
            proc = _scratch_run(
                scratch_repo,
                dataset,
                out,
                "--stage",
                "full-year",
                "--full-year-start",
                "2025-06-10T00:00:00+02:00",
                "--full-year-end",
                "2025-06-11T00:00:00+02:00",
                "--arms",
                arms,
            )
            _assert_ok(proc)
        for name in ("validation.json", "results.parquet", "outputs_manifest.json"):
            a = hashlib.sha256((first / name).read_bytes()).hexdigest()
            b = hashlib.sha256((second / name).read_bytes()).hexdigest()
            assert a == b, f"{name} is not byte-reproducible: {a} != {b}"
        text = (first / "validation.json").read_text()
        assert "wall_time_s" not in text
        identity = json.loads(text)["run_identity"]
        assert identity["provenance"]["cross_check"]["stable"] is True


import math  # noqa: E402
