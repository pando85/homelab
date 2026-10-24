#!/usr/bin/env python3
"""Pre-migration validation: multi-arm, multi-window experiment runner.

Compares the dispatch rungs on ONE dataset, ONE usable mask, ONE battery spec
(same opening SOC and SOC window for every arm) and ONE scoring basis, across
calendar-anchored local windows and across replan cadences.

Arms — each is a real call into the numeric core; nothing is reimplemented here:

``day_ahead``
    ``policies.policy_day_ahead`` — the CANONICAL rung.  One MILP per local day on
    a per-local-day ADVANCING causal forecast built by
    ``policies.forecast_load_trailing_slot_mean`` over the whole dataset, exactly
    the way ``benchmark.prepare_day_ahead`` prepares it.  Deliberately NOT
    ``run_day_ahead_with_frozen_forecast``: that helper hands the day-ahead solve a
    *frozen* profile, which is a different load basis and would let the artifact
    claim a forecast sharing that does not hold.

``rolling_frozen_legacy``
    ``rolling_horizon.run_rolling_horizon`` with an EXPLICIT
    ``load_forecast_mode="frozen"`` on the 14:00/20:30 two-publication schedule.
    This is the only arm allowed to use the frozen basis, and the artifact says so.

``rolling_causal_legacy``
    ``run_rolling_horizon`` with ``load_forecast_mode="causal"`` on the legacy
    schedule.  The dispatcher builds a per-event ``CausalForecastProvider``, which
    calls the SAME ``forecast_load_trailing_slot_mean`` the day-ahead rung uses, so
    this arm and ``day_ahead`` share a like-for-like load basis and only price
    information and cadence differ.

``rolling_causal_h96_c96`` / ``_c4`` / ``_c1``
    causal mode with a FIXED 96-slot (24 h) horizon, replanning every 96 / 4 / 1
    intervals.  The horizon is constant so the cadence is the only variable — that
    is what makes the three a cadence ladder rather than three different problems.

``rolling_causal_h192_c1``  (``--include-192``)
    cadence 1 over a 192-slot (48 h) horizon.  Its own arm because it changes the
    horizon: a horizon sensitivity, not a fourth cadence point.

``causal_price``
    ``policies.policy_causal_price`` with thresholds frozen from training only.

``oracle``  (``--oracle``)
    ``oracle.solve_oracle`` over the evaluation window.  Certification is reported
    exactly as the solver returned it (``is_certified`` / ``bound_kind`` /
    ``solver_gap``); it is never assumed.  The existing certified full-year oracle
    run does not cover a sub-window, so any bound claim on these windows needs
    this arm.

Honesty rules:

* **Fail loudly.**  ``CausalForecastProvider``, ``LOAD_FORECAST_MODE_CAUSAL`` and
  ``forecast_load_trailing_slot_mean`` are imported at module scope.  A build in
  which the per-event causal forecast is absent dies at import with ``ImportError``
  — it cannot degrade into a recorded "not available".  Only the core's own
  declared dispatch errors (``OracleError`` / ``BatteryPhysicsError``) become a
  recorded failure; a ``TypeError`` (signature drift) or any other exception
  aborts with a traceback and a non-zero status.  After the matrix runs, every
  requested arm must be present and ``ran: true`` or the exit status is non-zero.
* **Terminal mean from training only.**  The planning terminal value is the mean of
  usable prices strictly before the window start.  Scoring terminal value is 0.0,
  so ``cost_eur == meter_cost_eur`` on the meter basis for every arm.
* **Unusable rows are never laundered.**  A null / non-finite value on a *usable*
  row aborts; zero-fill happens only where the dataset's own mask already excludes.
* **Nothing is silently dropped.**  The monthly gate runs in ``report`` mode at the
  declared 0.80: a low-quality month (e.g. a sparse April) is named in the
  artifact and still contributes every interval to the evaluation totals.
* **Windows are local calendar windows.**  Boundaries are Europe/Madrid midnights
  converted to UTC instants, and months are bucketed by the dataset's ``local_day``
  column, so a 23- or 25-hour DST day stays inside one month and the spring-forward
  and fall-back days land inside their DST windows.
* **Timing is private and separate.**  Wall time is nondeterministic, so it goes in
  ``timing.json``, hashed by its own sidecar (``timing_manifest.json``) and
  EXCLUDED from the deterministic ``outputs_manifest.json``.  ``timing.json``
  carries no digest of the deterministic artifacts; run identity lives in
  ``validation.json``.
* **The artifact names its own bytes.**  ``run_identity`` records ``code_version``
  from ``manifest.code_version()`` — the real ``git rev-parse HEAD`` plus
  ``-dirty`` — with ``git_head`` and ``git_dirty`` alongside it so the dirty state
  needs no suffix parsing, a ``source_hashes`` map of sha256 over the raw bytes of
  EVERY ``energy_baseline`` module the runner imports (traced from the import graph,
  not a hand-kept list) plus this file itself, and both ``config_sha256`` (raw
  bytes) and the canonical ``config_hash`` next to ``config_path``.  The identity is
  snapshotted at the START and again at the END of the run and cross-checked: code
  or config that moved while the run was computing aborts the run instead of
  writing an artifact that cannot say which bytes produced its numbers.
  ``--require-clean-tree`` is what a citable run uses: it refuses to start at all on
  a dirty tree, or when git cannot be asked (``nogit``) and so the revision is
  unknown.

Do NOT run this against the real dataset yet: main is still being stabilised
(per-event causal forecast, runtime, economic and forecast work are active in
parallel).  Wait for explicit approval.

Example (representative stage, bounded arm set, citable run):

    uv run python hack/validate_pre_migration.py \\
        --dataset /path/to/analysis/v1/<dataset_id> \\
        --config config/tariff.yaml \\
        --output /tmp/opencode/energy-validation/run-1 \\
        --stage representative \\
        --arms day_ahead,rolling_causal_legacy,rolling_causal_h96_c96,\\
rolling_causal_h96_c4,rolling_causal_h96_c1 \\
        --lookback-days 28 --time-limit 5 --require-clean-tree
"""

from __future__ import annotations

import argparse
import ast
import hashlib
import json
import math
import sys
import time
import traceback
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any
from zoneinfo import ZoneInfo

import numpy as np
import pyarrow as pa
import pyarrow.parquet as pq

# Add src to path for direct execution (the package is not pip-installed for this run)
_here = Path(__file__).resolve().parent
_project = _here.parent
_src = _project / "src"
_package = _src / "energy_baseline"
_runner_path = Path(__file__).resolve()
if str(_src) not in sys.path:
    sys.path.insert(0, str(_src))

from energy_baseline.artifact_integrity import (  # noqa: E402
    finalize_run_outputs,
    monthly_usable_fraction_gate,
)
from energy_baseline.battery import (  # noqa: E402
    BatteryPhysicsError,
    BatterySpec,
    Horizon,
)
from energy_baseline.benchmark import (  # noqa: E402
    BenchmarkInputError,
    load_dataset,
    resolve_battery_assumptions,
    usable_mask,
)
from energy_baseline.config import load_config  # noqa: E402
from energy_baseline.manifest import (  # noqa: E402
    DIRTY_SUFFIX,
    NO_GIT_MARKER,
)
from energy_baseline.manifest import (  # noqa: E402
    code_version as git_code_version,
)
from energy_baseline.manifest import (  # noqa: E402
    config_hash as canonical_config_hash,
)
from energy_baseline.oracle import OracleError, solve_oracle  # noqa: E402
from energy_baseline.policies import (  # noqa: E402
    DayAheadDay,
    contiguous_day_bounds,
    fit_thresholds,
    forecast_load_trailing_slot_mean,
    policy_causal_price,
    policy_day_ahead,
    policy_no_battery,
)
from energy_baseline.rolling_horizon import (  # noqa: E402
    LOAD_FORECAST_MODE_CAUSAL,
    LOAD_FORECAST_MODE_FROZEN,
    CausalForecastProvider,
    build_frozen_load_profile,
    compute_rolling_events,
    run_rolling_horizon,
)

MADRID = ZoneInfo("Europe/Madrid")

#: The core's declared dispatch outcomes.  Anything outside this tuple is a bug in
#: this runner or in the core and must abort, not be recorded.  A ``TypeError`` in
#: particular means a call signature drifted — laundering that into "this arm did
#: not run" would turn a broken runner into a plausible-looking artifact.
_POLICY_ERRORS: tuple[type[BaseException], ...] = (OracleError, BatteryPhysicsError)

#: The monthly usable-fraction value declared for continuity with the benchmark
#: ladder.  Applied with ``policy="report"``: it names, it does not drop.
QUALITY_THRESHOLD = 0.80

ARM_DAY_AHEAD = "day_ahead"
ARM_ROLLING_FROZEN_LEGACY = "rolling_frozen_legacy"
ARM_ROLLING_CAUSAL_LEGACY = "rolling_causal_legacy"
ARM_CAUSAL_PRICE = "causal_price"
ARM_ORACLE = "oracle"

#: horizon-96 cadence ladder: the horizon is constant, only the replan period moves
H96_CADENCES: tuple[tuple[str, int], ...] = (
    ("rolling_causal_h96_c96", 96),
    ("rolling_causal_h96_c4", 4),
    ("rolling_causal_h96_c1", 1),
)
#: horizon sensitivity at cadence 1, kept out of the ladder because it moves the
#: horizon rather than the replan period
ARM_H192_C1 = "rolling_causal_h192_c1"
H192_REPLAN_SLOTS = 1
H192_HORIZON_SLOTS = 192

CORE_ARMS: tuple[str, ...] = (
    ARM_DAY_AHEAD,
    ARM_ROLLING_FROZEN_LEGACY,
    ARM_ROLLING_CAUSAL_LEGACY,
    ARM_CAUSAL_PRICE,
    *(name for name, _ in H96_CADENCES),
)
OPTIONAL_ARMS: tuple[str, ...] = (ARM_H192_C1, ARM_ORACLE)
ALL_ARMS: tuple[str, ...] = CORE_ARMS + OPTIONAL_ARMS

#: arms whose load basis is the per-event causal provider (like-for-like day_ahead)
CAUSAL_ARMS: frozenset[str] = frozenset({ARM_ROLLING_CAUSAL_LEGACY, ARM_H192_C1}) | frozenset(
    name for name, _ in H96_CADENCES
)

#: the dispatcher hardcodes the causal provider's trailing window today; read back
#: from the artifact rather than assumed, and asserted here so a silent change is a
#: test failure rather than a quiet difference between arms
PROVIDER_WINDOW_DAYS = 28

_HOURS_PER_SLOT = 900.0 / 3600.0


# ---------------------------------------------------------------------------
# Argument parsing / validation
# ---------------------------------------------------------------------------


def _parse_iso(value: str, *, name: str) -> datetime:
    text = value.strip().replace(" ", "T")
    if text.endswith("Z"):
        text = text[:-1] + "+00:00"
    try:
        dt = datetime.fromisoformat(text)
    except ValueError as exc:
        raise SystemExit(f"ERROR: {name} is not an ISO 8601 timestamp: {value!r}") from exc
    if dt.tzinfo is None:
        raise SystemExit(f"ERROR: {name}={value!r} is naive; an absolute instant is required")
    return dt.astimezone(UTC)


def _parse_arms(value: str) -> list[str]:
    parts = [p.strip() for p in value.split(",") if p.strip()]
    if not parts:
        raise SystemExit("ERROR: --arms is empty")
    unknown = [p for p in parts if p not in ALL_ARMS]
    if unknown:
        raise SystemExit(f"ERROR: --arms has unknown entries {unknown}; known: {list(ALL_ARMS)}")
    out: list[str] = []
    for p in parts:
        if p not in out:
            out.append(p)
    return out


def _inside_git_worktree(path: Path) -> bool:
    probe = path
    while True:
        if (probe / ".git").exists():
            return True
        if probe.parent == probe:
            return False
        probe = probe.parent


# ---------------------------------------------------------------------------
# Code provenance: git identity, per-module source hashes, config digests
# ---------------------------------------------------------------------------

#: Modules that are not reachable from this runner's import graph today but belong
#: to the same dispatch contract this milestone scores: :mod:`energy_baseline.dispatch`
#: is the planner-to-writer seam that ``replay_runtime`` builds out of exactly these
#: schedules.  Recording it is one extra digest; *omitting* a module is how a
#: provenance map silently under-attests (the failure mode this runner is built to
#: avoid, and the one a sibling study shipped).
_MANDATORY_SOURCE_MODULES: tuple[str, ...] = ("energy_baseline.dispatch",)


def _file_sha256(path: Path) -> str:
    """sha256 of a file's raw bytes as 64 lowercase hex characters."""
    digest = hashlib.sha256()
    with open(path, "rb") as handle:
        for chunk in iter(lambda: handle.read(65536), b""):
            digest.update(chunk)
    return digest.hexdigest()


def _git_identity() -> tuple[str, str | None, bool | None]:
    """``(code_version, git_head, git_dirty)`` from ONE read of the shared identity.

    :func:`energy_baseline.manifest.code_version` is the facility ``build`` and
    ``benchmark`` already pin their artifacts with: the 40-character
    ``git rev-parse HEAD``, plus :data:`~energy_baseline.manifest.DIRTY_SUFFIX` when
    the working tree carries uncommitted changes, or
    :data:`~energy_baseline.manifest.NO_GIT_MARKER` when git cannot be asked at all.

    Deriving all three from that one string keeps ``code_version`` and ``git_head``
    / ``git_dirty`` from ever contradicting each other, while the explicit fields
    spare a reader the suffix parsing.  No git is a recorded state (``None``/
    ``None``), never an exception: an artifact that says ``nogit`` is honest, one
    that aborts on a developer machine is not.
    """
    version = git_code_version()
    if version == NO_GIT_MARKER or version.startswith(f"{NO_GIT_MARKER}@"):
        return version, None, None
    if version.endswith(DIRTY_SUFFIX):
        return version, version[: -len(DIRTY_SUFFIX)], True
    return version, version, False


def _module_to_path(name: str) -> Path | None:
    """Resolve a dotted first-party module name to its source file, or ``None``.

    ``None`` means "not a module we can hash": a third-party name, or an attribute
    named in a ``from ... import`` clause (``energy_baseline.battery.BatterySpec``
    is a class, not a file).
    """
    parts = name.split(".")
    if parts[0] != "energy_baseline":
        return None
    if len(parts) == 1:
        return _package / "__init__.py"
    candidate = _package.joinpath(*parts[1:])
    as_module = candidate.with_suffix(".py")
    if as_module.is_file():
        return as_module
    as_package = candidate / "__init__.py"
    if as_package.is_file():
        return as_package
    return None


def _relative_import_anchor(path: Path) -> str:
    """Dotted package that *path*'s relative imports resolve against (PEP 328)."""
    parts = list(path.relative_to(_src).with_suffix("").parts)
    if parts and parts[-1] == "__init__":
        parts.pop()  # a package __init__: the anchor is itself
    elif len(parts) > 1:
        parts.pop()  # a module: the anchor is its containing package
    return ".".join(parts)


def _relative_import_base(anchor: str, level: int) -> str:
    """Package a ``level``-deep relative import resolves against.

    ``from .x import y`` in a module inside package ``p`` is anchored at ``p``
    itself; ``from ..x import y`` walks one package up.
    """
    if level <= 1 or not anchor:
        return anchor
    parts = anchor.split(".")
    return ".".join(parts[: -(level - 1)])


def _names_imported_by(tree: ast.AST, anchor: str) -> set[str]:
    """Every dotted name an ``import`` statement in *tree* could refer to.

    Both the imported package and each alias are returned, because
    ``from energy_baseline import cost`` and ``from .battery import BatterySpec``
    name a module in one position and a symbol in the next — the resolver in
    :func:`_static_first_party_closure` keeps whichever one maps to a file.
    ``importlib.import_module("literal")`` is followed too, and function-level
    imports are included: ``ast.walk`` descends into bodies, which is what catches
    ``benchmark._modules()`` pulling in :mod:`energy_baseline.cost` only on call.
    """
    names: set[str] = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            names.update(alias.name for alias in node.names)
        elif isinstance(node, ast.ImportFrom):
            if node.level:
                base = _relative_import_base(anchor, node.level)
                prefix = f"{base}.{node.module}" if node.module else base
            elif node.module:
                prefix = node.module
            else:  # pragma: no cover - `from (bare) import x` is not valid syntax
                prefix = ""
            if prefix:
                names.add(prefix)
                names.update(f"{prefix}.{alias.name}" for alias in node.names)
        elif isinstance(node, ast.Call):
            callee = getattr(node.func, "id", None) or getattr(node.func, "attr", None)
            if callee in {"import_module", "__import__"} and node.args:
                first = node.args[0]
                if isinstance(first, ast.Constant) and isinstance(first.value, str):
                    names.add(first.value)
    return {name for name in names if name}


def _parent_package_inits(path: Path) -> list[tuple[str, Path]]:
    """The package ``__init__.py`` files importing *path* necessarily executes.

    ``energy_baseline/hack/x.py`` pulls in ``energy_baseline/hack/__init__.py`` and
    ``energy_baseline/__init__.py``; both sets of bytes are on the causal path and
    both have to be pinned, or a change that lives only in a package initialiser is
    invisible to the artifact.
    """
    inits: list[tuple[str, Path]] = []
    current = path.parent
    while current != _src and _src in current.parents:
        init = current / "__init__.py"
        if init.is_file():
            dotted = ".".join(current.relative_to(_src).parts)
            inits.append((dotted, init))
        current = current.parent
    return inits


def _static_first_party_closure(entry: Path) -> dict[str, Path]:
    """Transitive first-party import closure of *entry*, traced from the source.

    Walks ``import`` statements rather than trusting a hand-kept list, so an import
    added tomorrow is recorded tomorrow instead of quietly missing from the
    artifact today.
    """
    found: dict[str, Path] = {}
    pending = [entry]
    visited: set[Path] = set()
    while pending:
        path = pending.pop()
        if path in visited:
            continue
        visited.add(path)
        try:
            tree = ast.parse(path.read_text(encoding="utf-8"))
        except (OSError, SyntaxError, ValueError) as exc:
            raise SystemExit(
                f"ERROR: cannot trace imports in {path} to build the source-hash map: {exc}"
            ) from exc
        anchor = "" if path == entry else _relative_import_anchor(path)
        for name in sorted(_names_imported_by(tree, anchor)):
            target = _module_to_path(name)
            if target is None:
                continue
            found[name] = target
            pending.append(target)
        if path != entry:
            for dotted, init in _parent_package_inits(path):
                found.setdefault(dotted, init)
                if init not in visited:
                    pending.append(init)
    return found


def _runtime_first_party_modules() -> dict[str, Path]:
    """First-party modules present in :data:`sys.modules` right now.

    The second of the two derivations.  It catches anything a static read of the
    source could miss — a module imported by a package ``__init__``, or one pulled
    in by a computed import — and it is the set the sibling study's hand-kept list
    was measured against and found short.
    """
    found: dict[str, Path] = {}
    for name in list(sys.modules):
        if name != "energy_baseline" and not name.startswith("energy_baseline."):
            continue
        target = _module_to_path(name)
        if target is not None and target.is_file():
            found[name] = target
    return found


def _source_module_map() -> dict[str, Path]:
    """Every first-party source file whose bytes this run depends on."""
    modules = _static_first_party_closure(_runner_path)
    modules.update(_runtime_first_party_modules())
    for name in _MANDATORY_SOURCE_MODULES:
        target = _module_to_path(name)
        if target is None or not target.is_file():
            raise SystemExit(
                f"ERROR: {name} is declared mandatory provenance but does not resolve "
                "to a source file; the source-hash map would be short by one module"
            )
        modules[name] = target
    return modules


def _project_relative(path: Path) -> str:
    """*path* relative to the project root, for use as a map key."""
    try:
        return path.resolve().relative_to(_project).as_posix()
    except ValueError as exc:
        raise SystemExit(
            f"ERROR: {path} is outside the project root {_project}; its bytes could not "
            "be cited from this repository, so recording it as provenance would mislead"
        ) from exc


def _collect_source_hashes() -> dict[str, str]:
    """sha256 of the raw bytes of every module this run depends on, plus this file.

    Keys are project-relative paths (``src/energy_baseline/battery.py``), so a
    reviewer holding a commit can read exactly these bytes with
    ``git show <sha>:scripts/energy-baseline/<key>``.
    """
    paths = {_project_relative(path): path for path in _source_module_map().values()}
    paths[_project_relative(_runner_path)] = _runner_path
    missing = [rel for rel, path in paths.items() if not path.is_file()]
    if missing:  # pragma: no cover - a traced module that vanished mid-run
        raise SystemExit(
            f"ERROR: source files listed as provenance are unreadable: {sorted(missing)}"
        )
    return {rel: _file_sha256(path) for rel, path in sorted(paths.items())}


#: Fields the start/end snapshots carry; every one is cross-checked before the
#: artifact is written.
_PROVENANCE_SCALARS: tuple[str, ...] = (
    "code_version",
    "git_head",
    "git_dirty",
    "config_sha256",
    "config_hash",
)


def _collect_provenance(config_path: Path) -> dict[str, Any]:
    """One full provenance snapshot: git identity, source hashes, config digests.

    Deterministic by construction — no clock reads anywhere in it, so two identical
    invocations on an unchanged tree produce byte-identical artifacts.
    """
    version, head, dirty = _git_identity()
    source_hashes = _collect_source_hashes()
    return {
        "code_version": version,
        "git_head": head,
        "git_dirty": dirty,
        "config_path": str(config_path),
        "config_sha256": _file_sha256(config_path),
        # Canonical convention (manifest.config_hash): yaml.dump(sort_keys=True,
        # width=1000) then sha256 — semantic equality.  config_sha256 above is the
        # raw bytes, so a comment-only edit is visible here and invisible there, and
        # the pair says which kind of change a re-run actually is.
        "config_hash": canonical_config_hash(config_path),
        "source_hashes": source_hashes,
        "source_module_count": len(source_hashes),
    }


def _assert_provenance_stable(start: dict[str, Any], end: dict[str, Any]) -> dict[str, Any]:
    """Compare the run's two snapshots and abort if any pinned byte moved.

    Reports *every* difference rather than the first: an operator facing this needs
    the whole list to know what to freeze.  A run whose code or config shifted under
    it cannot say which bytes produced its numbers, so it writes nothing.
    """
    problems: list[str] = []
    for key in _PROVENANCE_SCALARS:
        if start.get(key) != end.get(key):
            problems.append(f"{key}: start={start.get(key)!r} end={end.get(key)!r}")

    start_hashes = start.get("source_hashes") or {}
    end_hashes = end.get("source_hashes") or {}
    for rel in sorted(set(start_hashes) | set(end_hashes)):
        before = start_hashes.get(rel)
        after = end_hashes.get(rel)
        if before is None:
            problems.append(
                f"source_hashes[{rel}]: first hashed at the END of the run — it was "
                "imported mid-flight, so the numbers above may not be its bytes"
            )
        elif after is None:
            problems.append(
                f"source_hashes[{rel}]: hashed at the start but gone at the end "
                "(deleted, moved, or replaced with a different module)"
            )
        elif before != after:
            problems.append(f"source_hashes[{rel}]: {before} -> {after}")

    if problems:
        raise SystemExit(
            "ERROR: code or config changed while this run was computing.  The artifact "
            "could not attest to the bytes that produced these numbers, so nothing is "
            "being written:\n  " + "\n  ".join(problems) + "\nRe-run on a tree that stays still."
        )

    return {
        "stable": True,
        "scalar_fields_compared": list(_PROVENANCE_SCALARS),
        "git_head_stable": True,
        "code_version_stable": True,
        "config_digests_stable": True,
        "source_hashes_stable": True,
        "source_modules_compared": len(start_hashes),
        "drift": [],
        "note": (
            "the start and end snapshots are identical, so the pinned revision and "
            "the pinned per-module bytes below are the ones every arm was solved "
            "against.  This is measured, not asserted: any change to git_head, either "
            "config digest, or any module digest during the run aborts before "
            "validation.json is written"
        ),
    }


def _report_provenance(snapshot: dict[str, Any], *, require_clean_tree: bool) -> None:
    """Echo the identity to stdout so an operator sees what they are about to cite."""
    head = snapshot["git_head"]
    print(f"code_version      {snapshot['code_version']}")
    print(
        f"git               head={head if head else 'unknown (nogit)'} "
        f"dirty={snapshot['git_dirty']}"
    )
    print(
        f"provenance        {snapshot['source_module_count']} source files hashed, "
        f"config sha256={snapshot['config_sha256'][:16]}... "
        f"canonical hash={snapshot['config_hash'][:16]}..."
    )
    if require_clean_tree:
        print("provenance        --require-clean-tree: this run is meant to be citable")


def _refuse_unattributable_run(snapshot: dict[str, Any]) -> None:
    """``--require-clean-tree``: no artifact from bytes that cannot be named.

    Refuses before the output directory is created, so a rejected run leaves no
    half-written directory behind for the next operator to mistake for a result.
    """
    version = str(snapshot["code_version"])
    if snapshot["git_head"] is None:
        raise SystemExit(
            f"ERROR: --require-clean-tree cannot be satisfied: git reported no revision "
            f"(code_version={version!r}, the {NO_GIT_MARKER} sentinel).  A run whose "
            "commit is unknown cannot be cited, and 'unknown' must never be read as "
            "'clean'."
        )
    dirty = snapshot["git_dirty"] is True or version.endswith(DIRTY_SUFFIX)
    if dirty:
        raise SystemExit(
            f"ERROR: --require-clean-tree refuses to run on a dirty working tree "
            f"(code_version={version!r}, git_dirty=true, suffix {DIRTY_SUFFIX!r}).  "
            f"The numbers would belong to commit {snapshot['git_head']} plus whatever "
            "uncommitted edits were on disk, and the artifact could not tell a reader "
            "what those were.  Commit or revert them, then re-run; drop the flag for a "
            "development run, which records git_dirty=true instead of refusing."
        )


# ---------------------------------------------------------------------------
# Dataset columns: zero-fill ONLY where the mask already excludes
# ---------------------------------------------------------------------------


def _numeric_column(table: Any, name: str, usable: np.ndarray) -> np.ndarray:
    """Read one numeric column, null-safe only on rows the mask already excludes.

    A null or non-finite value on a **usable** row aborts.  Mapping it to 0.0 would
    turn a measurement hole into free energy (load) or a zero price, and the dot
    product over it would print as an ordinary cost.  The dataset's own mask is the
    only thing allowed to exclude a row, and it excludes it identically for
    every arm.
    """
    raw = np.array(
        [np.nan if v is None else float(v) for v in table.column(name).to_pylist()],
        dtype=float,
    )
    non_finite = ~np.isfinite(raw)
    if (non_finite & usable).any():
        idx = int(np.flatnonzero(non_finite & usable)[0])
        raise SystemExit(
            f"ERROR: {name} is non-finite at usable index {idx} ({raw[idx]!r}); "
            "refusing to zero-fill a value the mask still trusts"
        )
    raw[non_finite] = 0.0
    return raw


def _local_month(day: Any, stamp: datetime) -> str:
    """``YYYY-MM`` on the local grid; ``local_day`` is authoritative over UTC."""
    if day is not None:
        return f"{day.year:04d}-{day.month:02d}"
    return f"{stamp.year:04d}-{stamp.month:02d}"


# ---------------------------------------------------------------------------
# Windows (Europe/Madrid calendar)
# ---------------------------------------------------------------------------


def _local_midnight_utc(year: int, month: int, day: int, days: int) -> tuple[datetime, datetime]:
    """UTC instants bounding *days* whole local days, starting at local midnight."""
    start_local = datetime(year, month, day, 0, 0, tzinfo=MADRID)
    end_local = start_local + timedelta(days=days)
    return start_local.astimezone(UTC), end_local.astimezone(UTC)


def representative_windows(args: argparse.Namespace) -> list[dict[str, Any]]:
    """Four calendar-anchored local windows: winter, summer, both DST edges."""
    specs = [
        ("winter2025Jan", 2025, 1, 15, args.winter_days, "cold season, no DST inside window"),
        ("summer2025Jul", 2025, 7, 15, args.summer_days, "hot season, no DST inside window"),
        # 2025-03-30 is the spring-forward (92-interval) local day in Europe/Madrid.
        ("dst2025Mar", 2025, 3, 29, args.dst_days, "contains the 2025-03-30 spring-forward day"),
        # 2025-10-26 is the fall-back (100-interval) local day in Europe/Madrid.
        ("dst2025Oct", 2025, 10, 25, args.dst_days, "contains the 2025-10-26 fall-back day"),
    ]
    out: list[dict[str, Any]] = []
    for name, y, m, d, n, why in specs:
        start, end = _local_midnight_utc(y, m, d, n)
        out.append(
            {
                "name": name,
                "start": start,
                "end": end,
                "local_days": int(n),
                "anchor_local_day": f"{y:04d}-{m:02d}-{d:02d}",
                "purpose": why,
            }
        )
    return out


def full_year_window(args: argparse.Namespace) -> list[dict[str, Any]]:
    if args.full_year_start and args.full_year_end:
        start = _parse_iso(args.full_year_start, name="--full-year-start")
        end = _parse_iso(args.full_year_end, name="--full-year-end")
        name = "full_year_custom"
    elif args.full_year_start or args.full_year_end:
        raise SystemExit(
            "ERROR: --full-year-start and --full-year-end must be given together; one of "
            "them alone would silently fall back to the shipped 2025 range and the artifact "
            "would claim the wrong dates"
        )
    else:
        # 2025 local year: Jan 1 midnight to Jan 1 2026 midnight, Europe/Madrid.
        start, end = _local_midnight_utc(2025, 1, 1, 365)
        name = "full_year2025"
    return [
        {
            "name": name,
            "start": start,
            "end": end,
            "local_days": (end - start).days,
            "anchor_local_day": start.astimezone(MADRID).date().isoformat(),
            "purpose": "whole local year",
        }
    ]


# ---------------------------------------------------------------------------
# Canonical day-ahead inputs (mirrors benchmark.prepare_day_ahead)
# ---------------------------------------------------------------------------


def build_day_ahead_days(
    *,
    load: np.ndarray,
    usable: np.ndarray,
    local_days: list[Any],
    eval_index: np.ndarray,
    window_days: int,
) -> tuple[list[DayAheadDay], dict[str, Any], np.ndarray]:
    """Per-local-day ADVANCING causal forecast, built the canonical way.

    The forecast is computed over the whole dataset (history may legitimately reach
    back before the window start — strictly-past measured load is what a controller
    standing at a local midnight has), and each target day reads only indices below
    its own first interval.  This is the same call ``benchmark.prepare_day_ahead``
    makes, which is why ``day_ahead`` and the causal rolling arms share a
    like-for-like load basis.

    Returns ``(days, meta, forecast_full)``; ``forecast_full`` is the whole-array
    forecast, exposed so the caller can audit it against the rolling arm's provider.
    """
    full_days = contiguous_day_bounds(list(local_days))
    forecast_values, forecast_diag = forecast_load_trailing_slot_mean(
        load, full_days, window_days=int(window_days), usable=usable
    )
    diag_by_label = {str(entry["local_day"]): entry for entry in forecast_diag}
    forecast_eval = forecast_values[np.asarray(eval_index, dtype=int)]
    eval_runs = contiguous_day_bounds([local_days[i] for i in eval_index])

    days: list[DayAheadDay] = []
    for label, lo, hi in eval_runs:
        text = str(label)
        days.append(
            DayAheadDay(
                label=text,
                start=int(lo),
                stop=int(hi),
                forecast_load=forecast_eval[lo:hi],
                forecast=dict(diag_by_label.get(text, {})),
            )
        )
    meta = {
        "method": "trailing_slot_mean",
        "window_days": int(window_days),
        "basis": "per_local_day_advancing_causal",
        "partition": "local_calendar_day",
        "day_count": len(days),
        "history_may_cross_train_end": True,
        "shared_function": "policies.forecast_load_trailing_slot_mean",
        "note": (
            "policy_day_ahead receives a forecast re-learned from the trailing window "
            "before each dispatch day, exactly as benchmark.prepare_day_ahead prepares "
            "it; no frozen profile is substituted for this arm"
        ),
    }
    return days, meta, forecast_values


def audit_causal_forecast_sharing(
    *,
    provider: CausalForecastProvider,
    forecast_canonical: np.ndarray,
    interval_starts_utc: list[datetime],
    local_days: list[Any],
    eval_start_idx: int,
    eval_end_idx: int,
) -> dict[str, Any]:
    """Prove the rolling arm's per-event provider uses the day-ahead forecast rule.

    Both compute a trailing slot mean, but they cut at different places on purpose:
    the provider cuts at the EVENT's local-day start (conservative, one vintage for a
    multi-day horizon) while day-ahead cuts at each TARGET day's own start.  So the
    honest audit is not "the arrays are equal" — it is the pair of structural facts
    that make the sharing claim true and its bound explicit:

    * same method name and same window length, and
    * the provider's cutoff sits strictly below the event's own local day, i.e. the
      provider never reads its own day's measured load.
    """
    events = compute_rolling_events(
        interval_starts_utc,
        local_days,
        eval_start_idx=eval_start_idx,
        eval_end_idx=eval_end_idx,
        tz=MADRID,
    )
    if not events:  # pragma: no cover - evaluate_window already refuses an empty window
        raise SystemExit("ERROR: no rolling events for the causal sharing audit")

    probe = events[0]
    forecast_event, meta = provider.forecast_for_event(probe)
    day_start = int(meta["cutoff_idx"])
    supports = bool(meta["training_usable_support"])
    if not supports:  # pragma: no cover - evaluate_window refuses a training-free window
        raise SystemExit(
            f"ERROR: causal provider had no usable training support at the first event "
            f"(cutoff_idx={day_start}): the forecast would be all-zero, which is a missing "
            "input, not a forecast"
        )

    # Where both vintages agree: the provider's horizon days, evaluated at the
    # provider's own cutoff, are the day-ahead values re-learned one day earlier.
    # Report the magnitude of that difference instead of pretending it is zero.
    horizon = forecast_event[probe.start_idx : probe.plan_end_idx]
    canonical = forecast_canonical[probe.start_idx : probe.plan_end_idx]
    if horizon.size and canonical.size:
        denom = float(np.abs(canonical).mean()) or 1.0
        mean_abs_gap = float(np.abs(horizon - canonical).mean())
    else:  # pragma: no cover - an event always spans at least one interval
        mean_abs_gap = None
        denom = 1.0

    return {
        "shared_function": "policies.forecast_load_trailing_slot_mean",
        "provider_method": str(meta["method"]),
        "provider_window_days": int(meta["window_days"]),
        "canonical_window_days": int(meta["window_days"]),
        "cutoff_below_event_local_day_start": bool(meta["data_cutoff_before_local_day_start"]),
        "first_event_cutoff_idx": day_start,
        "first_event_idx": int(probe.start_idx),
        "first_event_local_day": str(meta["event_local_day"]),
        "first_event_kind": str(meta["event_kind"]),
        "first_event_training_usable_intervals": int(meta["training_usable_intervals"]),
        "first_event_training_usable_support": supports,
        "cutoff_strictly_before_event_start": bool(day_start <= probe.start_idx),
        "first_event_horizon_vs_canonical_mean_abs_kwh": (
            None if mean_abs_gap is None else round(mean_abs_gap, 9)
        ),
        "first_event_horizon_vs_canonical_relative": (
            None if mean_abs_gap is None else round(mean_abs_gap / denom, 9)
        ),
        "difference_is_expected": (
            "the provider cuts at the event's local-day start while day-ahead cuts at "
            "each target day's own start, so a multi-day horizon differs by exactly the "
            "one day of history the provider refuses to read; equal method and equal "
            "window is the sharing claim, the residual above quantifies the bound"
        ),
    }


# ---------------------------------------------------------------------------
# Arm scoring card: meter basis + end-SOC accounting
# ---------------------------------------------------------------------------


def _finite(value: Any) -> float | None:
    if value is None:
        return None
    out = float(value)
    return out if math.isfinite(out) else None


def _arm_card(
    *,
    arm: str,
    schedule: Any,
    prices_eval: np.ndarray,
    usable_eval: np.ndarray,
    baseline_cost_eur: float,
    spec: BatterySpec,
    load_forecast_mode: str | None = None,
    replan_interval_slots: int | None = None,
    horizon_slots: int | None = None,
    telemetry: dict[str, Any] | None = None,
    forecast_metadata: dict[str, Any] | None = None,
    extra: dict[str, Any] | None = None,
) -> dict[str, Any]:
    """Deterministic result identity for one arm, with the accounting checks.

    ``meter_recompute_eur`` is an INDEPENDENT ``dot(true_price, actual_grid)`` over
    usable intervals only, computed here rather than trusted from the schedule, so
    the artifact proves the meter basis and the usable masking instead of asserting
    them.  The end-SOC block reports initial/terminal SOC, the inventory adjustment,
    and whether the terminal SOC is inside the declared window — the accounting an
    open-cycle arm is most likely to get quietly wrong.
    """
    tele = telemetry or {}
    grid = np.asarray(schedule.grid_kwh, dtype=float)
    charge = np.asarray(schedule.charge_kwh, dtype=float)
    discharge = np.asarray(schedule.discharge_kwh, dtype=float)
    soc = np.asarray(schedule.soc_kwh, dtype=float)

    recompute = float(np.dot(prices_eval[usable_eval], grid[usable_eval]))
    cost = float(schedule.cost_eur)
    meter = _finite(schedule.meter_cost_eur)
    inventory = _finite(schedule.inventory_adjustment_eur)

    terminal_soc = _finite(schedule.terminal_soc_kwh)
    soc_min_kwh = spec.soc_bound_kwh("min")
    soc_max_kwh = spec.soc_bound_kwh("max")

    card: dict[str, Any] = {
        "arm": arm,
        "ran": True,
        "cost_eur": cost,
        "meter_cost_eur": meter,
        "meter_recompute_eur": recompute,
        "meter_recompute_abs_dev_eur": abs(cost - recompute),
        "inventory_adjustment_eur": inventory,
        "cost_minus_meter_minus_inventory_eur": (
            None if meter is None or inventory is None else cost - meter - inventory
        ),
        "baseline_cost_eur": baseline_cost_eur,
        "saving_eur": baseline_cost_eur - cost,
        "efc": _finite(schedule.efc),
        "throughput_ac_kwh": _finite(schedule.throughput_kwh),
        "charge_kwh": float(charge.sum()),
        "discharge_throughput_kwh": float(discharge.sum()),
        "initial_soc_kwh": _finite(schedule.initial_soc_kwh),
        "terminal_soc_kwh": terminal_soc,
        "end_soc_accounting": {
            "soc_min_kwh": float(soc_min_kwh),
            "soc_max_kwh": float(soc_max_kwh),
            "soc_trace_min_kwh": float(soc.min()) if soc.size else None,
            "soc_trace_max_kwh": float(soc.max()) if soc.size else None,
            "terminal_within_window": (
                None
                if terminal_soc is None
                else bool(soc_min_kwh - 1e-6 <= terminal_soc <= soc_max_kwh + 1e-6)
            ),
            "terminal_minus_initial_kwh": (
                None if terminal_soc is None else terminal_soc - float(schedule.initial_soc_kwh)
            ),
            "scoring_terminal_value_eur_per_kwh": 0.0,
            "note": (
                "terminal SOC is reported, not valued (scoring terminal value 0.0), so "
                "cost_eur is pure metered cash; terminal_minus_initial_kwh is the energy "
                "an arm leaves in or takes out of the battery relative to its opening "
                "state and is the number to check when comparing an open cycle against a "
                "terminal-neutral one"
            ),
        },
        "solver_status": getattr(schedule, "solver_status", None),
        "bound_kind": getattr(schedule, "bound_kind", None),
        "is_certified": bool(getattr(schedule, "is_certified", False)),
        "solver_gap": _finite(getattr(schedule, "solver_gap", None)),
        "ceiling_exceeding_intervals": _finite(
            getattr(schedule, "ceiling_exceeding_intervals", None)
        ),
        "eval_intervals": int(usable_eval.size),
        "scored_usable_intervals": int(usable_eval.sum()),
        "planned_vs_executed_intervals_ratio": None,
    }
    if load_forecast_mode is not None:
        card["load_forecast_mode"] = load_forecast_mode
    if replan_interval_slots is not None:
        card["replan_interval_slots"] = int(replan_interval_slots)
    if horizon_slots is not None:
        card["horizon_slots"] = int(horizon_slots)
        card["horizon_hours_nominal"] = horizon_slots * _HOURS_PER_SLOT

    planned = tele.get("planned_interval_slots_total")
    executed = tele.get("executed_interval_slots_total")
    if isinstance(planned, (int, float)) and isinstance(executed, (int, float)) and executed:
        card["planned_vs_executed_intervals_ratio"] = round(float(planned) / float(executed), 4)
    for key in (
        "solve_count",
        "planning_events_not_solved",
        "events_solved",
        "events_certified",
        "events_infeasible",
        "events_no_incumbent",
        "events_time_limit",
        "events_skipped_no_usable",
        "events_with_clipping",
        "planning_multiplicity_mean",
        "planning_multiplicity_max",
        "executed_multiplicity_mean",
        "days_solved",
        "days_infeasible",
        "days_at_time_limit",
    ):
        if key in tele:
            card[key] = tele[key]

    if isinstance(forecast_metadata, dict) and forecast_metadata:
        # Per-solve causal forecast audit, only for fields the dispatcher surfaced.
        card["forecast_audit"] = {
            key: forecast_metadata[key]
            for key in (
                "method",
                "events_served",
                "first_event_cutoff_idx",
                "first_event_cutoff_time_utc",
                "first_event_local_day",
                "first_event_kind",
                "first_event_window_days",
                "first_event_training_usable_intervals",
                "first_event_training_usable_support",
                "first_event_data_cutoff_before_local_day_start",
            )
            if key in forecast_metadata
        }
        card["forecast_audit"]["per_solve_forecast_available"] = bool(
            forecast_metadata.get("events_served")
        )
    card.update(extra or {})
    return card


# ---------------------------------------------------------------------------
# One window
# ---------------------------------------------------------------------------


def _run_arm(arm: str, fn: Any) -> tuple[Any, dict[str, Any] | None]:
    """Run one arm, recording only the core's declared dispatch outcome as a failure.

    Anything outside :data:`_POLICY_ERRORS` propagates: that is the difference
    between a measurement and a fiction, and it is why a signature drift cannot be
    quietly reported as "this arm did not run".
    """
    try:
        return fn(), None
    except _POLICY_ERRORS as exc:
        return None, {"error": f"{type(exc).__name__}: {exc}"}


def evaluate_window(
    *,
    window: dict[str, Any],
    loaded: Any,
    spec: BatterySpec,
    assumptions: Any,
    prices: np.ndarray,
    load: np.ndarray,
    usable: np.ndarray,
    local_days: list[Any],
    starts: list[datetime],
    arms: list[str],
    lookback_days: int,
    time_limit_s: float,
    oracle_time_limit_s: float,
    mip_rel_gap: float,
    mip_abs_gap: float,
) -> tuple[dict[str, Any], list[dict[str, Any]]]:
    """Run every requested arm over one window.  Returns ``(record, timing_rows)``."""
    w_start, w_end = window["start"], window["end"]
    eval_index = np.flatnonzero(np.array([w_start <= s < w_end for s in starts], dtype=bool))
    if eval_index.size == 0:
        raise SystemExit(
            f"ERROR: window {window['name']!r} has no intervals in "
            f"[{w_start.isoformat()}, {w_end.isoformat()}): the dataset does not cover "
            "this calendar window. A window that cannot be scored is a configuration "
            "error, not an empty result — fix the dataset range or drop the window."
        )
    if np.any(np.diff(eval_index) != 1):
        raise SystemExit(
            f"ERROR: window {window['name']!r} is not contiguous in the dataset index "
            "space; a rolling roll cannot skip intervals without pretending a gap is a "
            "plan boundary"
        )

    eval_start_idx = int(eval_index[0])
    eval_end_idx = int(eval_index[-1]) + 1

    # ---- training split: the requested lookback, clamped to what exists ----------
    want = lookback_days * int(round(3600.0 / loaded.interval_seconds * 24.0))
    train_start_idx = max(0, eval_start_idx - want)
    train_mask = np.zeros(len(starts), dtype=bool)
    train_mask[train_start_idx:eval_start_idx] = True
    train_usable = train_mask & usable
    if not train_usable.any():
        raise SystemExit(
            f"ERROR: window {window['name']!r} has 0 usable training interval before "
            f"{w_start.isoformat()}: the planning terminal value and the causal-price "
            "thresholds cannot be frozen out of nothing"
        )

    prices_eval = prices[eval_start_idx:eval_end_idx]
    load_eval = load[eval_start_idx:eval_end_idx]
    usable_eval = usable[eval_start_idx:eval_end_idx]

    train_prices = prices[train_usable]
    terminal_value = float(train_prices.mean())  # train-only; no eval price enters

    charge_threshold, discharge_threshold = fit_thresholds(
        train_prices, charge_pct=25.0, discharge_pct=75.0
    )

    eval_horizon = Horizon(
        interval_seconds=loaded.interval_seconds,
        prices=prices_eval,
        load_kwh=load_eval,
        usable=usable_eval,
        import_limit_kw=assumptions.import_limit_kw,
    )
    no_battery = policy_no_battery(spec, eval_horizon)
    baseline_cost = float(no_battery.cost_eur)
    if not usable_eval.any():
        raise SystemExit(
            f"ERROR: window {window['name']!r} has 0 usable evaluation interval: every "
            "cost would be 0.0 and the comparison would be vacuous"
        )

    # Frozen profile — used ONLY by the explicitly-frozen arm.
    frozen_load, frozen_meta = build_frozen_load_profile(
        load,
        local_days,
        usable,
        training_end_idx=eval_start_idx,
        window_days=lookback_days,
        interval_starts_utc=starts,
        tz=MADRID,
    )

    # Canonical per-day advancing forecast — used by day_ahead and audited against
    # the causal provider so the sharing claim is evidenced, not asserted.
    da_days, da_meta, forecast_full = build_day_ahead_days(
        load=load,
        usable=usable,
        local_days=local_days,
        eval_index=eval_index,
        window_days=lookback_days,
    )
    provider = CausalForecastProvider(
        load,
        local_days,
        usable,
        starts,
        window_days=lookback_days,
        interval_seconds=float(loaded.interval_seconds),
        tz=MADRID,
    )
    causal_share = audit_causal_forecast_sharing(
        provider=provider,
        forecast_canonical=forecast_full,
        interval_starts_utc=starts,
        local_days=local_days,
        eval_start_idx=eval_start_idx,
        eval_end_idx=eval_end_idx,
    )

    # In causal mode the dispatcher builds its own provider and ignores frozen_load;
    # a zeros placeholder is what benchmark._prepare_rolling_horizon passes too.
    causal_placeholder = np.zeros_like(load)

    cards: dict[str, Any] = {}
    timing_rows: list[dict[str, Any]] = []

    def run(arm: str, fn: Any, *, attempts: Any = None) -> None:
        t0 = time.monotonic()
        outcome, failure = _run_arm(arm, fn)
        wall = time.monotonic() - t0
        # Timing is private nondeterministic telemetry; it never enters the
        # deterministic card.
        row: dict[str, Any] = {
            "window": window["name"],
            "arm": arm,
            "wall_time_s": round(float(wall), 6),
            "solve_count": attempts,
        }
        if failure is not None:
            cards[arm] = {"arm": arm, "ran": False, "failure": failure}
            timing_rows.append(row)
            return
        cards[arm] = outcome
        if row["solve_count"] is None:
            # Read the solve count back from the card rather than guessing it before
            # the arm ran: a cadence roll reports solve_count, the day-ahead rung
            # reports days_solved, and both are what a reader costs a run by.
            row["solve_count"] = outcome.get("solve_count", outcome.get("days_solved"))
        timing_rows.append(row)

    if ARM_DAY_AHEAD in arms:

        def _day_ahead() -> dict[str, Any]:
            sched, tele = policy_day_ahead(
                spec,
                eval_horizon,
                da_days,
                terminal_value_eur_per_kwh=terminal_value,
                scoring_terminal_value_eur_per_kwh=0.0,
                time_limit_s=time_limit_s,
                mip_rel_gap=mip_rel_gap,
                mip_abs_gap=mip_abs_gap,
            )
            return _arm_card(
                arm=ARM_DAY_AHEAD,
                schedule=sched,
                prices_eval=prices_eval,
                usable_eval=usable_eval,
                baseline_cost_eur=baseline_cost,
                spec=spec,
                load_forecast_mode="causal_advancing_per_local_day",
                telemetry=tele,
                extra={
                    "forecast_basis": da_meta,
                    "entry_point": "policies.policy_day_ahead",
                    "day_count": len(da_days),
                },
            )

        run(ARM_DAY_AHEAD, _day_ahead)

    if ARM_CAUSAL_PRICE in arms:

        def _causal_price() -> dict[str, Any]:
            sched = policy_causal_price(
                spec,
                eval_horizon,
                charge_threshold=charge_threshold,
                discharge_threshold=discharge_threshold,
            )
            return _arm_card(
                arm=ARM_CAUSAL_PRICE,
                schedule=sched,
                prices_eval=prices_eval,
                usable_eval=usable_eval,
                baseline_cost_eur=baseline_cost,
                spec=spec,
                load_forecast_mode="none_price_only",
                extra={
                    "entry_point": "policies.policy_causal_price",
                    "charge_threshold_eur_per_kwh": charge_threshold,
                    "discharge_threshold_eur_per_kwh": discharge_threshold,
                    "threshold_source": "train-only percentiles 25/75, frozen before eval",
                },
            )

        run(ARM_CAUSAL_PRICE, _causal_price)

    if ARM_ORACLE in arms:

        def _oracle() -> dict[str, Any]:
            sched = solve_oracle(
                spec,
                eval_horizon,
                time_limit_s=oracle_time_limit_s,
                mip_rel_gap=mip_rel_gap,
                mip_abs_gap=mip_abs_gap,
                terminal_mode="free",
                terminal_value_eur_per_kwh=0.0,
            )
            return _arm_card(
                arm=ARM_ORACLE,
                schedule=sched,
                prices_eval=prices_eval,
                usable_eval=usable_eval,
                baseline_cost_eur=baseline_cost,
                spec=spec,
                extra={
                    "entry_point": "oracle.solve_oracle",
                    "oracle_time_limit_s": float(oracle_time_limit_s),
                    "oracle_terminal_mode": "free",
                    "certification_note": (
                        "is_certified is what the solver reported for THIS window; the "
                        "existing certified full-year oracle run does not cover a "
                        "sub-window, so a bound claim needs this arm certified here"
                    ),
                },
            )

        run(ARM_ORACLE, _oracle, attempts=1)

    if ARM_ROLLING_FROZEN_LEGACY in arms:

        def _frozen_legacy() -> dict[str, Any]:
            result = run_rolling_horizon(
                spec,
                prices=prices,
                load_kwh=load,
                usable=usable,
                interval_seconds=loaded.interval_seconds,
                local_days=local_days,
                interval_starts_utc=starts,
                eval_start_idx=eval_start_idx,
                eval_end_idx=eval_end_idx,
                tz=MADRID,
                frozen_load=frozen_load,
                terminal_value_eur_per_kwh=terminal_value,
                time_limit_s=time_limit_s,
                mip_rel_gap=mip_rel_gap,
                mip_abs_gap=mip_abs_gap,
                import_limit_kw=assumptions.import_limit_kw,
                replan_interval_slots=None,
                horizon_slots=None,
                load_forecast_mode=LOAD_FORECAST_MODE_FROZEN,
            )
            return _arm_card(
                arm=ARM_ROLLING_FROZEN_LEGACY,
                schedule=result.schedule,
                prices_eval=prices_eval,
                usable_eval=usable_eval,
                baseline_cost_eur=baseline_cost,
                spec=spec,
                load_forecast_mode=LOAD_FORECAST_MODE_FROZEN,
                telemetry=result.telemetry,
                forecast_metadata=result.forecast_metadata,
                extra={
                    "entry_point": "rolling_horizon.run_rolling_horizon",
                    "cadence": "legacy two-publication (14:00 and 20:30 local)",
                    "frozen_profile_meta": frozen_meta,
                    "load_basis_note": (
                        "frozen: the profile is fixed at the training cut and reused by "
                        "every solve, so this arm does NOT share day_ahead's advancing "
                        "load basis. That asymmetry is the point of the ladder, not an "
                        "accident, and it is why the causal arms exist alongside it"
                    ),
                },
            )

        run(ARM_ROLLING_FROZEN_LEGACY, _frozen_legacy)

    for arm, replan, horizon in [
        (ARM_ROLLING_CAUSAL_LEGACY, None, None),
        *[(name, slots, 96) for name, slots in H96_CADENCES],
        (ARM_H192_C1, H192_REPLAN_SLOTS, H192_HORIZON_SLOTS),
    ]:
        if arm not in arms:
            continue

        def _causal_roll(arm=arm, replan=replan, horizon=horizon) -> dict[str, Any]:
            if arm == ARM_ROLLING_CAUSAL_LEGACY:
                cadence = "legacy two-publication (14:00 and 20:30 local)"
                slots: int | None = None
                hor: int | None = None
            else:
                cadence = (
                    f"every {replan} interval(s) over a fixed {horizon}-slot horizon "
                    f"({horizon * _HOURS_PER_SLOT:.0f} h)"
                )
                slots, hor = replan, horizon
            result = run_rolling_horizon(
                spec,
                prices=prices,
                load_kwh=load,
                usable=usable,
                interval_seconds=loaded.interval_seconds,
                local_days=local_days,
                interval_starts_utc=starts,
                eval_start_idx=eval_start_idx,
                eval_end_idx=eval_end_idx,
                tz=MADRID,
                frozen_load=causal_placeholder,
                terminal_value_eur_per_kwh=terminal_value,
                time_limit_s=time_limit_s,
                mip_rel_gap=mip_rel_gap,
                mip_abs_gap=mip_abs_gap,
                import_limit_kw=assumptions.import_limit_kw,
                replan_interval_slots=slots,
                horizon_slots=hor,
                load_forecast_mode=LOAD_FORECAST_MODE_CAUSAL,
            )
            if result.telemetry.get("load_forecast_mode") != LOAD_FORECAST_MODE_CAUSAL:
                raise SystemExit(
                    f"ERROR: arm {arm!r} asked for causal load forecast mode but the "
                    f"dispatcher reported {result.telemetry.get('load_forecast_mode')!r}"
                )
            served = (result.forecast_metadata or {}).get("events_served")
            if not served:
                raise SystemExit(
                    f"ERROR: arm {arm!r} ran in causal mode but the dispatcher served 0 "
                    "events through CausalForecastProvider: the per-event causal forecast "
                    "is not actually in this build, which is exactly the failure mode a "
                    "recorded 'not available' would hide"
                )
            window_used = (result.forecast_metadata or {}).get("first_event_window_days")
            if window_used is not None and int(window_used) != PROVIDER_WINDOW_DAYS:
                raise SystemExit(
                    f"ERROR: arm {arm!r}: the causal provider used window_days="
                    f"{window_used}, not the {PROVIDER_WINDOW_DAYS} the day-ahead rung "
                    "and this runner assume; the load bases are no longer like-for-like"
                )
            return _arm_card(
                arm=arm,
                schedule=result.schedule,
                prices_eval=prices_eval,
                usable_eval=usable_eval,
                baseline_cost_eur=baseline_cost,
                spec=spec,
                load_forecast_mode=LOAD_FORECAST_MODE_CAUSAL,
                replan_interval_slots=slots,
                horizon_slots=hor,
                telemetry=result.telemetry,
                forecast_metadata=result.forecast_metadata,
                extra={
                    "entry_point": "rolling_horizon.run_rolling_horizon",
                    "cadence": cadence,
                    "load_basis_note": (
                        "causal: the dispatcher built a per-event CausalForecastProvider "
                        "that calls the same forecast_load_trailing_slot_mean day_ahead "
                        "uses, so the load basis is like-for-like and only price "
                        "information and cadence differ"
                    ),
                },
            )

        run(arm, _causal_roll)

    # ---- per-month evaluation totals on the LOCAL grid (nothing dropped) --------
    per_month: dict[str, dict[str, int]] = {}
    for i in eval_index:
        month = _local_month(local_days[i], starts[i])
        bucket = per_month.setdefault(month, {"total": 0, "usable": 0})
        bucket["total"] += 1
        bucket["usable"] += int(bool(usable[i]))

    record = {
        "window": window["name"],
        "purpose": window["purpose"],
        "anchor_local_day": window["anchor_local_day"],
        "eval_start": w_start.isoformat().replace("+00:00", "Z"),
        "eval_end": w_end.isoformat().replace("+00:00", "Z"),
        "requested_local_days": int(window["local_days"]),
        "eval_intervals": int(eval_end_idx - eval_start_idx),
        "eval_usable": int(usable_eval.sum()),
        "eval_usable_fraction": float(usable_eval.mean()),
        "eval_start_idx": eval_start_idx,
        "eval_end_idx": eval_end_idx,
        "local_days_in_window": len({str(local_days[i]) for i in eval_index}),
        "training": {
            "lookback_days_requested": int(lookback_days),
            "lookback_intervals_requested": int(want),
            "train_start_idx": train_start_idx,
            "train_end_idx": eval_start_idx,
            "train_intervals_available": int(eval_start_idx - train_start_idx),
            "train_usable": int(train_usable.sum()),
            "clamped_to_dataset_start": bool(train_start_idx == 0),
        },
        "terminal_value_eur_per_kwh": terminal_value,
        "terminal_value_source": "mean of usable TRAINING prices only; no eval price enters",
        "scoring_terminal_value_eur_per_kwh": 0.0,
        "baseline_cost_eur": baseline_cost,
        "causal_forecast_sharing_audit": causal_share,
        "monthly_eval_totals": per_month,
        "arms": cards,
    }
    return record, timing_rows


# ---------------------------------------------------------------------------
# Main
# ---------------------------------------------------------------------------


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description="Pre-migration validation: multi-arm multi-window experiment runner",
    )
    parser.add_argument("--dataset", required=True, help="Built dataset directory (has a manifest)")
    parser.add_argument("--config", required=True, help="tariff.yaml used to build that dataset")
    parser.add_argument(
        "--output", required=True, help="Private output directory; must be outside the repo"
    )
    parser.add_argument(
        "--stage",
        choices=("representative", "full-year"),
        default="representative",
        help="representative = 4 short calendar windows (default, bounds the first run); "
        "full-year = the whole 2025 local year",
    )
    parser.add_argument(
        "--arms",
        default=",".join(CORE_ARMS),
        help=f"comma-separated arms to run; known arms: {', '.join(ALL_ARMS)}",
    )
    parser.add_argument("--include-192", action="store_true", help="add the horizon-192 arm")
    parser.add_argument("--oracle", action="store_true", help="add the certified oracle arm")
    parser.add_argument("--lookback-days", type=int, default=28, help="training lookback days")
    parser.add_argument("--time-limit", type=float, default=5.0, help="per-solve limit (s)")
    parser.add_argument(
        "--oracle-time-limit", type=float, default=600.0, help="oracle solve limit (s)"
    )
    parser.add_argument("--mip-rel-gap", type=float, default=1e-6)
    parser.add_argument("--mip-abs-gap", type=float, default=1e-9)
    parser.add_argument("--winter-days", type=int, default=3)
    parser.add_argument("--summer-days", type=int, default=3)
    parser.add_argument("--dst-days", type=int, default=3)
    parser.add_argument("--full-year-start", default=None)
    parser.add_argument("--full-year-end", default=None)
    parser.add_argument(
        "--require-clean-tree",
        action="store_true",
        help="refuse to run unless the working tree is clean at a recorded commit, so "
        "this artifact can be cited by SHA alone.  Default off: development runs and "
        "the test suite work on a dirty tree and record git_dirty=true instead of "
        "being blocked.  Also refuses when git cannot be asked at all (nogit), "
        "because then no revision exists to cite",
    )
    return parser


def main() -> int:
    args = build_parser().parse_args()

    arms = _parse_arms(args.arms)
    if args.include_192 and ARM_H192_C1 not in arms:
        arms.append(ARM_H192_C1)
    if args.oracle and ARM_ORACLE not in arms:
        arms.append(ARM_ORACLE)

    if args.lookback_days < 1:
        raise SystemExit(f"ERROR: --lookback-days must be >= 1, got {args.lookback_days}")
    if args.time_limit <= 0 or not math.isfinite(args.time_limit):
        raise SystemExit(f"ERROR: --time-limit must be > 0 and finite, got {args.time_limit!r}")
    if args.oracle_time_limit <= 0 or not math.isfinite(args.oracle_time_limit):
        raise SystemExit(
            f"ERROR: --oracle-time-limit must be > 0 and finite, got {args.oracle_time_limit!r}"
        )
    if args.winter_days < 1 or args.summer_days < 1 or args.dst_days < 1:
        raise SystemExit("ERROR: --winter-days/--summer-days/--dst-days must be >= 1")

    # Validate the date overrides before the expensive dataset load
    if args.full_year_start:
        _parse_iso(args.full_year_start, name="--full-year-start")
    if args.full_year_end:
        _parse_iso(args.full_year_end, name="--full-year-end")

    # ---- provenance, before anything is computed, created, or written -----------
    config_path = Path(args.config).expanduser()
    if not config_path.is_file():
        raise SystemExit(f"ERROR: tariff config not found: {config_path}")
    provenance_start = _collect_provenance(config_path)
    _report_provenance(provenance_start, require_clean_tree=args.require_clean_tree)
    if args.require_clean_tree:
        _refuse_unattributable_run(provenance_start)

    output_dir = Path(args.output).expanduser()
    if output_dir.exists() and any(output_dir.iterdir()):
        raise SystemExit(
            f"ERROR: output directory {output_dir} exists and is not empty. Refusing to "
            "overwrite: artifacts are compared across runs, so a clobbered directory "
            "silently mixes two measurements. Pick a fresh --output."
        )
    if _inside_git_worktree(output_dir.resolve()):
        raise SystemExit(
            f"ERROR: --output {output_dir} is inside the repository. Costs come from "
            "private interval data and must never be committed; choose a path outside "
            "the work tree (e.g. /tmp/opencode/energy-validation/run-1)."
        )
    output_dir.mkdir(parents=True, exist_ok=True)

    print(f"Loading dataset {args.dataset} ...", flush=True)
    try:
        loaded = load_dataset(args.dataset)
    except BenchmarkInputError as exc:
        raise SystemExit(f"ERROR: dataset cannot be loaded: {exc}") from exc

    config = load_config(config_path)

    table = loaded.table
    total = table.num_rows
    starts = table.column("interval_start").to_pylist()
    local_days = table.column("local_day").to_pylist()
    usable, reasons = usable_mask(table)
    load = _numeric_column(table, "gross_kwh", usable)
    prices = _numeric_column(table, "price_eur_per_kwh", usable)

    assumptions = resolve_battery_assumptions(config)
    spec = BatterySpec(**assumptions.spec_kwargs())

    windows = (
        representative_windows(args) if args.stage == "representative" else full_year_window(args)
    )

    print(f"dataset           {loaded.dataset_id} ({total} intervals)")
    print(
        f"usable            {int(usable.sum())}/{total} ({usable.mean():.3%})  "
        f"interval_seconds={loaded.interval_seconds}"
    )
    print(f"stage             {args.stage}")
    print(f"windows           {[w['name'] for w in windows]}")
    print(f"arms              {arms}")
    print(f"lookback_days     {args.lookback_days}", flush=True)

    records: list[dict[str, Any]] = []
    all_timing: list[dict[str, Any]] = []
    for window in windows:
        print(f"\n--- {window['name']} ({window['anchor_local_day']} +{window['local_days']}d) ---")
        record, timing_rows = evaluate_window(
            window=window,
            loaded=loaded,
            spec=spec,
            assumptions=assumptions,
            prices=prices,
            load=load,
            usable=usable,
            local_days=local_days,
            starts=starts,
            arms=arms,
            lookback_days=args.lookback_days,
            time_limit_s=args.time_limit,
            oracle_time_limit_s=args.oracle_time_limit,
            mip_rel_gap=args.mip_rel_gap,
            mip_abs_gap=args.mip_abs_gap,
        )
        records.append(record)
        all_timing.extend(timing_rows)
        for arm, card in record["arms"].items():
            if not card.get("ran"):
                print(f"    {arm:26s} DID NOT RUN: {card.get('failure')}")
                continue
            terminal = card["terminal_soc_kwh"]
            print(
                f"    {arm:26s} cost={card['cost_eur']:>10.4f} "
                f"save={card['saving_eur']:>9.4f} efc={(card['efc'] or 0.0):>7.3f} "
                f"endSOC={(0.0 if terminal is None else terminal):>6.3f}"
            )

    # ---- provenance again, and prove nothing moved while we were solving ---------
    provenance_end = _collect_provenance(config_path)
    provenance_check = _assert_provenance_stable(provenance_start, provenance_end)

    # ---- every requested arm must have run; otherwise fail loudly ---------------
    missing = [
        f"{r['window']}/{arm}: {r['arms'][arm].get('failure', 'absent')}"
        for r in records
        for arm in arms
        if arm not in r["arms"] or not r["arms"][arm].get("ran")
    ]
    if missing:
        raise SystemExit(
            "ERROR: requested arms did not all run — this is a hard failure, not a "
            "result:\n  " + "\n  ".join(missing)
        )

    # ---- declared monthly gate, report mode, nothing dropped --------------------
    merged: dict[str, dict[str, int]] = {}
    for r in records:
        for month, bucket in r["monthly_eval_totals"].items():
            acc = merged.setdefault(month, {"total": 0, "usable": 0})
            acc["total"] += bucket["total"]
            acc["usable"] += bucket["usable"]
    gate = monthly_usable_fraction_gate(merged, QUALITY_THRESHOLD, policy="report")
    flagged = [
        {
            "month": mf.month,
            "usable_fraction": mf.fraction,
            "usable": mf.usable,
            "total": mf.total,
        }
        for mf in gate.below_threshold
    ]

    # ---- accounting audit across the whole matrix -------------------------------
    max_meter_dev = max(
        card["meter_recompute_abs_dev_eur"]
        for r in records
        for card in r["arms"].values()
        if "meter_recompute_abs_dev_eur" in card
    )
    max_inventory_dev = max(
        abs(card["cost_minus_meter_minus_inventory_eur"] or 0.0)
        for r in records
        for card in r["arms"].values()
        if "cost_minus_meter_minus_inventory_eur" in card
    )
    end_soc_outside = [
        f"{r['window']}/{card['arm']}"
        for r in records
        for card in r["arms"].values()
        if card.get("end_soc_accounting", {}).get("terminal_within_window") is False
    ]

    summary = {
        "run_identity": {
            "runner": "hack/validate_pre_migration.py",
            "mode": "pre_migration_validation",
            # Git identity, not the distribution string: ``manifest.code_version()``
            # is what ``build`` and ``benchmark`` already pin with, so an artifact
            # from this runner can be checked out and re-run by SHA.
            "code_version": provenance_start["code_version"],
            "git_head": provenance_start["git_head"],
            "git_dirty": provenance_start["git_dirty"],
            "dataset_id": loaded.dataset_id,
            "build_manifest_sha256": loaded.manifest_sha256,
            "config_path": str(config_path),
            "config_sha256": provenance_start["config_sha256"],
            "config_hash": provenance_start["config_hash"],
            "source_hashes": provenance_start["source_hashes"],
            "provenance": {
                "start": provenance_start,
                "end": provenance_end,
                "cross_check": provenance_check,
                "note": (
                    "code_version/git_head/git_dirty above, config_sha256 and "
                    "config_hash, and source_hashes are the START snapshot; "
                    "cross_check proves the END snapshot is byte-for-byte the same, so "
                    "these digests describe the code every arm actually ran. Paths are "
                    f"relative to {_project.name}/, i.e. "
                    "git show <git_head>:scripts/energy-baseline/<path> reads back the "
                    "exact bytes hashed here. source_hashes is traced from the import "
                    "graph (static walk of every import statement plus the modules "
                    "present in sys.modules), not a hand-kept list, so a newly imported "
                    "module is pinned without anyone remembering to add it"
                ),
            },
            "interval_seconds": float(loaded.interval_seconds),
            "stage": args.stage,
            "arms_requested": arms,
            "parameters": {
                "lookback_days": int(args.lookback_days),
                "time_limit_s": float(args.time_limit),
                "oracle_time_limit_s": float(args.oracle_time_limit),
                "mip_rel_gap": float(args.mip_rel_gap),
                "mip_abs_gap": float(args.mip_abs_gap),
                "quality_threshold": QUALITY_THRESHOLD,
                "quality_policy": "report",
                "oracle": bool(args.oracle),
                "include_192": bool(args.include_192),
                "require_clean_tree": bool(args.require_clean_tree),
            },
            "cutoffs": [
                {
                    "window": r["window"],
                    "eval_start": r["eval_start"],
                    "eval_end": r["eval_end"],
                    "eval_start_idx": r["eval_start_idx"],
                    "eval_end_idx": r["eval_end_idx"],
                    "train_start_idx": r["training"]["train_start_idx"],
                    "train_end_idx": r["training"]["train_end_idx"],
                    "train_usable": r["training"]["train_usable"],
                }
                for r in records
            ],
        },
        "battery": assumptions.as_dict(),
        "battery_note": (
            "ONE BatterySpec instance is passed to every arm, so all arms open at the "
            "same initial SOC under the same soc_min/soc_max window. The rolling arms "
            "carry the ACTUAL clipped SOC between events — that is a property of the "
            "dispatcher being compared, not a different opening condition"
        ),
        "scoring_basis": {
            "terminal_value_scoring_eur_per_kwh": 0.0,
            "terminal_value_planning_source": "train-only mean of usable prices",
            "cost_is_meter_cost": True,
            "unusable_intervals_scored": False,
            "max_meter_recompute_abs_dev_eur": max_meter_dev,
            "max_cost_minus_meter_minus_inventory_eur": max_inventory_dev,
            "note": (
                "every euro is dot(true_price, actual_grid) over usable intervals only; "
                "meter_recompute_eur proves that independently of the schedule field and "
                "the two *_dev values above are the audit margins for the whole matrix"
            ),
        },
        "end_soc_accounting": {
            "arms_with_terminal_soc_outside_window": end_soc_outside,
            "checked": True,
        },
        "forecast_basis": {
            "day_ahead": "per-local-day advancing causal trailing slot mean (canonical)",
            "rolling_causal": "per-event CausalForecastProvider, same function as day_ahead",
            "rolling_frozen": "profile frozen at the training cut (NOT the day_ahead basis)",
            "shared_function": "policies.forecast_load_trailing_slot_mean",
            "arms_sharing_day_ahead_basis": sorted(CAUSAL_ARMS | {ARM_DAY_AHEAD}),
            "frozen_only_arms": [ARM_ROLLING_FROZEN_LEGACY],
            "per_window_audit": [
                {"window": r["window"], "audit": r["causal_forecast_sharing_audit"]}
                for r in records
            ],
        },
        "cadence_design": {
            "horizon96_ladder": [
                {"arm": name, "replan_interval_slots": slots, "horizon_slots": 96}
                for name, slots in H96_CADENCES
            ],
            "horizon192": {
                "arm": ARM_H192_C1,
                "replan_interval_slots": H192_REPLAN_SLOTS,
                "horizon_slots": H192_HORIZON_SLOTS,
                "role": "horizon sensitivity, separate from the cadence ladder",
            },
            "price_information_rule": (
                "the information set is a function of the decision instant, not of the "
                "cadence: prices for local day D+1 are exact from 20:30 on day D, so all "
                "arms see the same publication boundary"
            ),
        },
        "dominance": {
            "assumed": False,
            "note": (
                "no dominance is assumed. Costs are recorded side by side; a certified "
                "bound needs the --oracle arm with is_certified=true for the same "
                "window, and that is reported exactly as the solver returned it"
            ),
        },
        "monthly_quality_gate": {
            "threshold": QUALITY_THRESHOLD,
            "declared_mode": "report",
            "continuity_note": (
                "0.80 is the threshold declared for continuity with the benchmark "
                "ladder, applied in report mode so evaluation totals stay comparable"
            ),
            "dropped_months": [],
            "nothing_dropped": True,
            "per_month": [
                {
                    "month": mf.month,
                    "total": mf.total,
                    "usable": mf.usable,
                    "usable_fraction": mf.fraction,
                }
                for mf in gate.per_month
            ],
            "below_threshold": flagged,
        },
        "coverage": {
            "dataset_intervals": int(total),
            "dataset_usable": int(usable.sum()),
            "dataset_usable_fraction": float(usable.mean()),
            "exclusion_reasons": _count(reasons),
        },
        "windows": records,
    }

    validation_path = output_dir / "validation.json"
    validation_path.write_text(json.dumps(summary, indent=2, allow_nan=False, default=str))

    results_path = output_dir / "results.parquet"
    _write_results_parquet(results_path, records, arms)

    # ---- private nondeterministic telemetry, OUT of the deterministic set -------
    timing_payload = {
        "note": (
            "wall-clock timing only. Nondeterministic, so it is excluded from the "
            "deterministic outputs_manifest.json and hashed by its own sidecar. It "
            "deliberately carries no digest of the deterministic artifacts — run "
            "identity lives in validation.json"
        ),
        "deterministic_artifact_digests": None,
        "rows": all_timing,
    }
    timing_path = output_dir / "timing.json"
    timing_path.write_text(json.dumps(timing_payload, indent=2, allow_nan=False))

    manifest = finalize_run_outputs(output_dir, ("validation.json", "results.parquet"))
    timing_manifest = finalize_run_outputs(
        output_dir, ("timing.json",), manifest_name="timing_manifest.json"
    )

    print()
    if flagged:
        print(
            f"QUALITY (report mode, nothing dropped): below {QUALITY_THRESHOLD}: "
            + ", ".join(f"{m['month']}={m['usable_fraction']:.3f}" for m in flagged)
        )
    else:
        print(f"QUALITY: every evaluation month is at or above {QUALITY_THRESHOLD}")
    print(f"meter recompute max abs dev: {max_meter_dev:.3e} EUR")
    print(
        f"provenance: {provenance_start['code_version']} "
        f"({provenance_check['source_modules_compared']} source files, stable)"
    )
    print(f"wrote {validation_path}")
    print(f"wrote {results_path}")
    print(f"wrote {timing_path} (private timing, own manifest)")
    print(f"manifests: {manifest.name}, {timing_manifest.name}")
    return 0


def _write_results_parquet(path: Path, records: list[dict[str, Any]], arms: list[str]) -> None:
    """One row per (window, arm) with the deterministic comparison fields."""
    float_cols = (
        "cost_eur",
        "meter_cost_eur",
        "meter_recompute_eur",
        "meter_recompute_abs_dev_eur",
        "inventory_adjustment_eur",
        "baseline_cost_eur",
        "saving_eur",
        "efc",
        "throughput_ac_kwh",
        "charge_kwh",
        "discharge_throughput_kwh",
        "initial_soc_kwh",
        "terminal_soc_kwh",
        "solver_gap",
        "planned_vs_executed_intervals_ratio",
    )
    int_cols = (
        "solve_count",
        "replan_interval_slots",
        "horizon_slots",
        "eval_intervals",
        "scored_usable_intervals",
    )
    str_cols = ("arm", "solver_status", "bound_kind", "load_forecast_mode", "cadence")

    data: dict[str, list[Any]] = {"window": []}
    for key in (*str_cols, *float_cols, *int_cols, "is_certified"):
        data[key] = []

    for r in records:
        for arm in arms:
            card = r["arms"][arm]
            data["window"].append(r["window"])
            for key in str_cols:
                value = card.get(key)
                data[key].append(None if value is None else str(value))
            for key in float_cols:
                value = card.get(key)
                data[key].append(None if value is None else float(value))
            for key in int_cols:
                value = card.get(key)
                data[key].append(None if value is None else int(value))
            data["is_certified"].append(bool(card.get("is_certified", False)))

    arrays: dict[str, pa.Array] = {}
    for key, vals in data.items():
        if key in float_cols:
            arrays[key] = pa.array(vals, pa.float64())
        elif key in int_cols:
            arrays[key] = pa.array(vals, pa.int64())
        elif key == "is_certified":
            arrays[key] = pa.array(vals, pa.bool_())
        else:
            arrays[key] = pa.array(vals, pa.string())
    pq.write_table(pa.table(arrays), path)


def _count(reasons: list[str]) -> dict[str, int]:
    out: dict[str, int] = {}
    for reason in reasons:
        out[reason] = out.get(reason, 0) + 1
    return dict(sorted(out.items(), key=lambda kv: (-kv[1], kv[0])))


if __name__ == "__main__":
    try:
        sys.exit(main())
    except SystemExit:
        raise
    except KeyboardInterrupt:  # pragma: no cover - interactive only
        print("interrupted", file=sys.stderr)
        sys.exit(130)
    except Exception:
        # A signature drift, a bad keyword or an unexpected core failure is a BUG:
        # loud traceback and non-zero status, never a partial artifact that claims
        # an arm merely "did not run".
        traceback.print_exc()
        sys.exit(1)
