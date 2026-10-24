#!/usr/bin/env python3
"""Economic sensitivity sweep — throughput cost and terminal value.

Standalone experiment runner.  Loads a **built** dataset through its manifest,
re-optimises the oracle once for every ``(planning_terminal_value,
throughput_cost)`` pair, and writes private JSON artifacts under
``--output-dir`` (which must live outside the repository).

Usage::

    uv run python hack/economic_sensitivity.py \\
        --dataset /path/to/analysis/v1/<dataset_id> \\
        --config config/tariff.yaml \\
        --output-dir /private/energy-sensitivity/run-1 \\
        --planning-terminal-values 0.0,0.05,0.10,0.15,0.20 \\
        --throughput-costs 0.0,0.01,0.02,0.05,0.10 \\
        --train-end 2025-10-01T00:00:00Z

Every monetary parameter is an **explicit, user-supplied array**.  Nothing here
is derived from the data being scored: no mean, no quantile, no fitted rate.  No
real degradation rates are invented or assumed — the throughput cost is an
economic convention (degradation valued as proportional to AC energy throughput
at the meter), not a physical aging model.

Honesty rules this runner is written to
----------------------------------------

* **Explicit windows.**  ``--train-end`` declares the cut and ``--window``
  selects what is scored.  ``--window evaluation`` requires **both**
  ``--eval-start`` and ``--eval-end``; the evaluation window must start at or
  after ``--train-end`` (out-of-sample) and both bounds must lie inside the
  dataset.  A window that runs off either end of the data is refused, never
  silently truncated, and "to the end of the dataset" is not an implicit default.
* **Explicit parameters only.**  Planning terminal values and throughput costs
  come from the command line.  The training window is recorded for provenance;
  no sweep parameter is computed from it, or from the scored window.
* **Free mode for terminal sweeps.**  The planning terminal value only enters
  the MILP objective in ``--terminal-mode free``.  In ``fixed`` mode the end SOC
  is pinned to the seed and there is no inventory to value, so a nonzero planning
  terminal value is refused rather than silently ignored.
* **Optimised responses, not rescoring.**  Each pair is its own solve through
  :func:`energy_baseline.economic_sensitivity.optimize_with_throughput`, so the
  solver minimises *that pair's* objective.  Repeating a parameter in an array is
  allowed and must produce an identical action digest; the artifact records
  digests per arm so the claim can be checked.
* **Executed-action evidence.**  Every arm records the planned arrays and their
  digests, plus ``first_nonidle_index`` / ``first_nonidle_interval_start`` — the
  first interval in which the plan actually moves energy.  The window's first
  interval is recorded separately and is *not* evidence that any action exists.
* **Both objective bases.**  Each arm records the planning objective (planning
  terminal value) and the degradation-adjusted objective under the single
  ``--scoring-terminal-value`` held constant across all arms, so arms stay
  comparable.  Scoring reuses :func:`score_fixed_schedule`; no physics or
  accounting is reimplemented here.
* **Canonical inputs.**  Interval usability comes from
  :func:`energy_baseline.benchmark.usable_mask` (allocation, energy *and* price
  quality, nulls, non-finite values), and the battery from
  :func:`energy_baseline.benchmark.resolve_battery_assumptions`, so every field
  of ``BatterySpec`` — the initial SOC, the efficiency mapping, the SOC window
  and the import ceiling, each with its provenance — is the config's, never a
  local constant.
* **No silent swallowing.**  Only the numeric core's declared solver/physics
  errors degrade a run to a recorded failure; any other exception aborts with a
  traceback and a non-zero exit status.
* **Deterministic artifacts.**  No wall-clock timestamp is written into any
  artifact: two runs over the same inputs produce byte-identical output.  Lineage
  is captured instead as source hashes, the code version, the config hash, the
  build-manifest digest and the dataset part-file digests.
* **Costs are withheld from source.**  Numbers are written only to
  ``--output-dir``, which must be outside the git work tree and must not already
  hold another run's files.

No euro figure and no wall-clock timing is written into the repository.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import math
import sys
import traceback
from dataclasses import asdict
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

import numpy as np

# Allow running from the repo root without installing.
_HERE = Path(__file__).resolve().parent
_PROJECT = _HERE.parent
_SRC = _PROJECT / "src"
if str(_SRC) not in sys.path:
    sys.path.insert(0, str(_SRC))

from energy_baseline.artifact_integrity import finalize_run_outputs  # noqa: E402
from energy_baseline.battery import BatteryPhysicsError, BatterySpec, Horizon  # noqa: E402
from energy_baseline.benchmark import (  # noqa: E402
    BenchmarkInputError,
    load_dataset,
    parse_train_end,
    resolve_battery_assumptions,
    usable_mask,
)
from energy_baseline.config import load_config  # noqa: E402
from energy_baseline.economic_sensitivity import (  # noqa: E402
    optimize_with_throughput,
    score_fixed_schedule,
)
from energy_baseline.manifest import code_version, config_hash  # noqa: E402
from energy_baseline.oracle import OracleError  # noqa: E402

ARTIFACT_NAME = "sensitivity_sweep.json"
MANIFEST_NAME = "sensitivity_outputs_manifest.json"

#: Errors the numeric core declares for a single arm.  Anything else aborts.
_ARM_ERRORS: tuple[type[BaseException], ...] = (OracleError, BatteryPhysicsError)

#: Flow at or below this is "the battery is idle in this interval".
_IDLE_TOLERANCE_KWH = 1e-12

#: A planning terminal value within this of zero counts as zero (fixed mode).
_ZERO_TOLERANCE = 1e-15

#: Files whose bytes are hashed into ``source_hashes`` (relative to the project).
_SOURCE_FILES = (
    "hack/economic_sensitivity.py",
    "src/energy_baseline/economic_sensitivity.py",
    "src/energy_baseline/oracle.py",
    "src/energy_baseline/battery.py",
    "src/energy_baseline/benchmark.py",
    "src/energy_baseline/config.py",
    "src/energy_baseline/manifest.py",
    "src/energy_baseline/artifact_integrity.py",
)

#: What each monetary knob is *called* — naming only.  None of these is a real
#: rate, and none is measured or fitted anywhere in this runner.
_UNITS = {
    "planning_terminal_value_eur_per_kwh": (
        "EUR per DC-kWh of terminal inventory, used as a coefficient inside the MILP "
        "objective. Naming convention only: not a quoted energy price, not a measured "
        "value of stored energy"
    ),
    "scoring_terminal_value_eur_per_kwh": (
        "EUR per DC-kWh of terminal inventory, used only to score optimized schedules on one "
        "common basis across arms. Naming convention only: not a measured value"
    ),
    "throughput_cost_eur_per_kwh": (
        "EUR per kWh of AC energy moved through the battery (charge plus discharge at the "
        "meter), an explicit economic convention standing in for degradation. Naming "
        "convention only: no real degradation rate is claimed, fitted, or assumed"
    ),
    "meter_cost_eur": "pretax energy cost at the meter over usable intervals, in EUR",
    "time_limit_s": (
        "solver wall-clock budget handed to HiGHS, in seconds. A declared limit, not a "
        "measurement: no observed duration is ever written to the artifacts"
    ),
}

_HONESTY_NOTE = (
    "all monetary values are user-supplied hypotheticals; no real degradation rates are "
    "assumed or fitted. The planning_terminal_value is the coefficient inside the MILP "
    "objective; the scoring_terminal_value is held constant across all arms for fair "
    "comparison. Units are naming conventions only"
)


# ---------------------------------------------------------------------------
# Small helpers
# ---------------------------------------------------------------------------


def _parse_float_list(raw: str) -> list[float]:
    """Parse a comma-separated list of finite floats."""
    parts = [p.strip() for p in raw.split(",") if p.strip()]
    if not parts:
        raise argparse.ArgumentTypeError(f"expected at least one value, got {raw!r}")
    try:
        values = [float(p) for p in parts]
    except ValueError as exc:
        raise argparse.ArgumentTypeError(f"{raw!r} is not a comma-separated float list: {exc}")
    bad = [v for v in values if not math.isfinite(v)]
    if bad:
        raise argparse.ArgumentTypeError(f"values must be finite, got {bad}")
    return values


def _iso_utc(moment: datetime) -> str:
    """Absolute UTC instant with an unambiguous ``Z`` suffix."""
    return moment.astimezone(UTC).isoformat().replace("+00:00", "Z")


def _parse_instant(value: str, *, name: str) -> datetime:
    """Parse an aware ISO-8601 instant.  Never consults the wall clock."""
    text = value.strip().replace(" ", "T")
    if text.endswith("Z"):
        text = text[:-1] + "+00:00"
    try:
        moment = datetime.fromisoformat(text)
    except ValueError as exc:
        raise SystemExit(f"error: {name} is not an ISO-8601 timestamp: {value!r}") from exc
    if moment.tzinfo is None:
        raise SystemExit(
            f"error: {name}={value!r} is naive — a window bound is an absolute instant, so an "
            "aware timestamp is required"
        )
    return moment.astimezone(UTC)


def _canonical_sha(payload: Any) -> str:
    """SHA-256 of a canonical JSON encoding of *payload*."""
    text = json.dumps(payload, sort_keys=True, separators=(",", ":"), default=str)
    return hashlib.sha256(text.encode("utf-8")).hexdigest()


def _file_sha256(path: Path) -> str:
    """SHA-256 of a file's raw bytes."""
    digest = hashlib.sha256()
    with open(path, "rb") as handle:
        for chunk in iter(lambda: handle.read(8192), b""):
            digest.update(chunk)
    return digest.hexdigest()


def _inside_git_worktree(path: Path) -> tuple[bool, Path | None]:
    """Walk parents looking for ``.git``; report the work-tree root if found."""
    probe = path
    while True:
        if (probe / ".git").exists():
            return True, probe
        if probe.parent == probe:
            return False, None
        probe = probe.parent


def _nearest_existing(path: Path) -> Path:
    """Nearest existing ancestor, so a not-yet-created path can be resolved."""
    probe = path
    while not probe.exists() and probe.parent != probe:
        probe = probe.parent
    return probe


def _resolve_output_dir(raw: Path) -> Path:
    """Absolute output path, refusing the repo and refusing to mix runs.

    Resolution walks up to the nearest existing directory and re-attaches the
    remaining components, because ``--output-dir`` may name a run directory that
    does not exist yet: the existing prefix is resolved (symlinks and ``..``
    included) while the not-yet-created tail is kept exactly as declared.
    """
    requested = Path(raw).expanduser()
    if requested.exists():
        resolved = requested.resolve()
    else:
        anchor = _nearest_existing(requested)
        resolved = anchor.resolve() / requested.relative_to(anchor)
    _reject_repo_output(resolved)
    _reject_dirty_output(resolved)
    return resolved


def _reject_repo_output(output_dir: Path) -> None:
    """Real outputs must never land inside the repository."""
    inside, root = _inside_git_worktree(output_dir)
    if inside:
        raise SystemExit(
            f"error: --output-dir {output_dir} is inside the git work tree ({root}); real "
            "results must be written outside the repository so no cost figure can ever be "
            "committed. Pass a private directory, e.g. /private/energy-sensitivity/run-1"
        )


def _reject_dirty_output(output_dir: Path) -> None:
    """An output directory must not already hold another run's files."""
    if output_dir.exists() and any(output_dir.iterdir()):
        raise SystemExit(
            f"error: --output-dir {output_dir} exists and is not empty; refusing to overwrite "
            "or mix runs. Choose a fresh directory"
        )


def _reject_output_inside_dataset(output_dir: Path, dataset_dir: Path) -> None:
    """A run's artifacts must not be written into the dataset they measure.

    The dataset directory is a verified, content-addressed build artifact.  A
    sweep writing there would leave cost figures sitting inside a pinned
    artifact, where the part-file digests cannot tell them apart from drift.
    """
    try:
        output_dir.relative_to(dataset_dir)
    except ValueError:
        return
    raise SystemExit(
        f"error: --output-dir {output_dir} is inside the dataset directory {dataset_dir}; "
        "run artifacts must not be written into a pinned build artifact. Choose a separate "
        "private directory"
    )


def _reason_counts(reasons: list[str]) -> dict[str, int]:
    """Histogram of usability reasons, most frequent first."""
    counts: dict[str, int] = {}
    for reason in reasons:
        counts[reason] = counts.get(reason, 0) + 1
    return dict(sorted(counts.items(), key=lambda kv: (-kv[1], kv[0])))


def _finite_or_none(value: Any) -> float | None:
    """JSON-safe float: a non-finite value becomes ``null``, never ``NaN``."""
    if value is None:
        return None
    out = float(value)
    return out if math.isfinite(out) else None


def _array(values: Any) -> list[float]:
    """A float list safe for ``json.dumps``."""
    return [float(v) for v in np.asarray(values, dtype=float).reshape(-1)]


# ---------------------------------------------------------------------------
# Argument parsing
# ---------------------------------------------------------------------------


def _parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    p = argparse.ArgumentParser(
        description="Economic sensitivity sweep — throughput cost and terminal value.",
        formatter_class=argparse.ArgumentDefaultsHelpFormatter,
    )
    p.add_argument(
        "--dataset",
        required=True,
        type=Path,
        help="Path to a built dataset directory (with build_manifest.json).",
    )
    p.add_argument(
        "--config",
        required=True,
        type=Path,
        help="Path to the tariff config YAML.  Its hash is pinned in the build manifest, and "
        "that pin is verified before anything is solved.",
    )
    p.add_argument(
        "--output-dir",
        required=True,
        type=Path,
        help="Directory for private output artifacts.  Must be outside the repo and empty.",
    )
    p.add_argument(
        "--planning-terminal-values",
        required=True,
        type=_parse_float_list,
        help="Comma-separated planning terminal values (EUR/DC-kWh) to sweep.  These are the "
        "coefficients inside the MILP objective.  Explicit user-supplied hypotheticals: none "
        "is derived from the data.  Only meaningful with --terminal-mode free.",
    )
    p.add_argument(
        "--throughput-costs",
        required=True,
        type=_parse_float_list,
        help="Comma-separated throughput costs (EUR/kWh of AC energy through the meter) to "
        "sweep.  Explicit user-supplied hypotheticals; no real degradation rate is assumed.",
    )
    p.add_argument(
        "--scoring-terminal-value",
        type=float,
        default=0.0,
        help="Scoring terminal value (EUR/DC-kWh) held constant across all arms and used for "
        "the reported degradation-adjusted objective, so arms stay comparable.  Default 0.0 "
        "(cash-flow objective).  A user-supplied hypothetical.",
    )
    p.add_argument(
        "--train-end",
        required=True,
        type=str,
        help="ISO-8601 UTC instant ending the training window.  Declares the cut: an evaluation "
        "window must start at or after it.  No sweep parameter is derived from this window.",
    )
    p.add_argument(
        "--train-start",
        type=str,
        default=None,
        help="ISO-8601 UTC instant starting the training window.  Default: the first interval "
        "of the dataset, recorded as such.",
    )
    p.add_argument(
        "--window",
        choices=("training", "evaluation"),
        default="training",
        help="Which window the sweep scores.  'evaluation' requires both --eval-start and "
        "--eval-end.",
    )
    p.add_argument(
        "--eval-start",
        type=str,
        default=None,
        help="ISO-8601 UTC instant: first interval of the evaluation window.",
    )
    p.add_argument(
        "--eval-end",
        type=str,
        default=None,
        help="ISO-8601 UTC instant: exclusive end of the evaluation window.  Required with "
        "--window evaluation; 'to the end of the dataset' is not a default, because it "
        "silently changes what is being measured.",
    )
    p.add_argument(
        "--terminal-mode",
        choices=("free", "fixed"),
        default="free",
        help="Terminal SOC mode for the oracle.  'free' lets the end SOC float and values it "
        "with the planning terminal value; 'fixed' pins the end SOC to the seed, so every "
        "planning terminal value must be 0.0.",
    )
    p.add_argument(
        "--time-limit-s",
        type=float,
        default=60.0,
        help="Wall-clock budget handed to each solver call (seconds).  A declared limit, not a "
        "measurement; no observed duration is written to the artifacts.",
    )
    p.add_argument(
        "--mip-rel-gap",
        type=float,
        default=1e-4,
        help="Relative optimality tolerance for certification.",
    )
    p.add_argument(
        "--mip-abs-gap",
        type=float,
        default=1e-6,
        help="Absolute (EUR) optimality tolerance for certification.",
    )
    p.add_argument(
        "--initial-soc-frac",
        type=float,
        default=None,
        help="Override the configured battery.initial_soc_frac.  Default: use the config, whose "
        "provenance is recorded.  An override must lie inside the configured SOC window.",
    )
    p.add_argument(
        "--allow-reconstructed-quality",
        action="store_true",
        help="Admit reconstructed energy/price qualities in the usable mask (default: measured "
        "only — the benchmark's own default).",
    )
    p.add_argument(
        "--omit-plan-arrays",
        action="store_true",
        help="Do not write each arm's planned arrays into the artifact; keep their digests and "
        "the non-idle evidence.  Use for long windows.",
    )
    return p.parse_args(argv)


# ---------------------------------------------------------------------------
# Windows
# ---------------------------------------------------------------------------


def _resolve_windows(
    args: argparse.Namespace,
    *,
    dataset_start: datetime,
    dataset_end: datetime,
) -> dict[str, Any]:
    """Turn the declared bounds into two explicit, checked windows.

    Returns the temporal provenance block, with the resolved datetimes under
    ``_scored_bounds`` / ``_training_bounds`` for the caller.  Every bound is
    either given by the user or recorded as coming from the dataset: nothing is
    clamped and nothing defaults to "the end of the data".
    """
    train_end = parse_train_end(args.train_end)
    if args.train_start is not None:
        train_start = _parse_instant(args.train_start, name="--train-start")
        train_start_source = "argument: --train-start"
    else:
        train_start = dataset_start
        train_start_source = "dataset: first interval_start of the loaded table"

    if train_start >= train_end:
        raise SystemExit(
            f"error: training window is empty: {train_start_source} {_iso_utc(train_start)} is "
            f"not before train_end {_iso_utc(train_end)}"
        )
    if train_start < dataset_start:
        raise SystemExit(
            f"error: --train-start {_iso_utc(train_start)} precedes the dataset "
            f"[{_iso_utc(dataset_start)}, {_iso_utc(dataset_end)}); refusing rather than "
            "clamping a declared bound to the dataset start"
        )
    if train_end > dataset_end:
        raise SystemExit(
            f"error: --train-end {_iso_utc(train_end)} is past the end of the dataset "
            f"[{_iso_utc(dataset_start)}, {_iso_utc(dataset_end)}); training data cannot be "
            "something the dataset does not contain. Refusing rather than clamping"
        )

    declared_eval = [
        name
        for name, value in (("--eval-start", args.eval_start), ("--eval-end", args.eval_end))
        if value is not None
    ]
    if args.window == "training" and declared_eval:
        raise SystemExit(
            f"error: {', '.join(declared_eval)} given with --window training would be silently "
            "ignored. Pass --window evaluation to score the evaluation window"
        )

    if args.window == "evaluation":
        missing = [
            name
            for name, given in (("--eval-start", args.eval_start), ("--eval-end", args.eval_end))
            if given is None
        ]
        if missing:
            raise SystemExit(
                f"error: --window evaluation requires {' and '.join(missing)}: both bounds of "
                "the evaluation window must be explicit. 'To the end of the dataset' is not a "
                "default, because it silently changes what is being measured"
            )
        eval_start = _parse_instant(args.eval_start, name="--eval-start")
        eval_end = _parse_instant(args.eval_end, name="--eval-end")
        if eval_start >= eval_end:
            raise SystemExit(
                f"error: evaluation window is empty: --eval-start {_iso_utc(eval_start)} is not "
                f"before --eval-end {_iso_utc(eval_end)}"
            )
        if eval_start < train_end:
            raise SystemExit(
                f"error: --eval-start {_iso_utc(eval_start)} precedes train_end "
                f"{_iso_utc(train_end)} — the sweep would score intervals the training window "
                "already claims. The cut is the out-of-sample rule: move --eval-start to or "
                "after it"
            )
        if eval_start < dataset_start or eval_end > dataset_end:
            raise SystemExit(
                f"error: evaluation window [{_iso_utc(eval_start)}, {_iso_utc(eval_end)}) is not "
                "covered by the dataset "
                f"[{_iso_utc(dataset_start)}, {_iso_utc(dataset_end)}); refusing rather than "
                "truncating a declared bound"
            )
        scored_start, scored_end, label = eval_start, eval_end, "evaluation"
    else:
        scored_start, scored_end, label = train_start, train_end, "training"

    return {
        "train_end": _iso_utc(train_end),
        "train_end_validation": (
            "argument: --train-end, normalised by energy_baseline.benchmark.parse_train_end "
            "(aware instant required, future cut rejected). Validation only: no wall-clock "
            "value is written into the artifacts"
        ),
        "training_window": {
            "start": _iso_utc(train_start),
            "end": _iso_utc(train_end),
            "start_source": train_start_source,
        },
        "window": args.window,
        "scored_window": {
            "label": label,
            "start": _iso_utc(scored_start),
            "end": _iso_utc(scored_end),
            "note": (
                "the sweep scores exactly these intervals. The bound is the user's, so 'what "
                "was measured' does not depend on how much data happens to be present"
            ),
        },
        "_scored_bounds": (scored_start, scored_end),
        "_training_bounds": (train_start, train_end),
    }


def _window_coverage(
    starts: list[datetime],
    usable: np.ndarray,
    bounds: tuple[datetime, datetime],
) -> dict[str, Any]:
    """Coverage of a declared window (recorded for provenance, never fitted)."""
    lo, hi = bounds
    index = [i for i, s in enumerate(starts) if lo <= s < hi]
    if not index:
        return {
            "interval_count": 0,
            "usable_interval_count": 0,
            "usable_fraction": 0.0,
            "first_interval_start": None,
            "last_interval_start": None,
        }
    window_usable = usable[index]
    return {
        "interval_count": len(index),
        "usable_interval_count": int(window_usable.sum()),
        "usable_fraction": float(window_usable.sum()) / float(len(index)),
        "first_interval_start": _iso_utc(starts[index[0]]),
        "last_interval_start": _iso_utc(starts[index[-1]]),
    }


# ---------------------------------------------------------------------------
# Horizon (canonical usable mask + canonical battery assumptions)
# ---------------------------------------------------------------------------


def _numeric_column(table: Any, name: str) -> np.ndarray:
    """Numeric column as floats, with nulls read as 0.0 and NaN left in place.

    The null placeholder is only ever consumed on intervals the usable mask has
    already rejected (see :func:`_build_horizon`, which refuses to substitute on a
    usable interval and records every substitution it makes).  NaN is deliberately
    preserved here so the audit can tell "absent" from "not a number".
    """
    return np.array(
        [0.0 if v is None else float(v) for v in table.column(name).to_pylist()],
        dtype=float,
    )


def _read_table(dataset: Any, *, allow_reconstructed: bool) -> dict[str, Any]:
    """Read the whole table once, with the canonical usable mask and its reasons."""
    table = dataset.table
    usable, reasons = usable_mask(table, allow_reconstructed=allow_reconstructed)
    return {
        "starts": table.column("interval_start").to_pylist(),
        "ends": table.column("interval_end").to_pylist(),
        "raw_prices": table.column("price_eur_per_kwh").to_pylist(),
        "raw_gross": table.column("gross_kwh").to_pylist(),
        "prices": _numeric_column(table, "price_eur_per_kwh"),
        "gross": _numeric_column(table, "gross_kwh"),
        "usable": usable,
        "reasons": reasons,
    }


def _build_horizon(
    dataset: Any,
    cols: dict[str, Any],
    *,
    start: datetime,
    end: datetime,
    import_limit_kw: float | None,
    allow_reconstructed: bool,
) -> tuple[Horizon, list[datetime], dict[str, Any]]:
    """Slice the dataset to ``[start, end)`` with the benchmark's own usable mask.

    An interval is rejected for the reasons :func:`usable_mask` rejects it:
    allocation quality, a null / non-finite / negative gross energy total, an
    unusable energy quality, and — the case a purely energy-based mask lets
    straight through into the solver — a null or non-finite price, or an
    unusable price quality.
    """
    starts = cols["starts"]
    window = [i for i, s in enumerate(starts) if start <= s < end]
    if not window:
        raise SystemExit(
            f"error: the scored window [{_iso_utc(start)}, {_iso_utc(end)}) contains no "
            f"intervals of dataset {dataset.dataset_id} [{_iso_utc(dataset.start)}, "
            f"{_iso_utc(dataset.end)}]"
        )

    usable = cols["usable"][window]
    reasons_all = cols["reasons"]
    raw_prices = cols["raw_prices"]
    raw_gross = cols["raw_gross"]
    index = np.array(window, dtype=int)
    prices_raw = cols["prices"][index]
    gross_raw = cols["gross"][index]
    excluded = ~usable
    window_starts = [starts[r] for r in window]

    if not usable.any():
        raise SystemExit(
            f"error: all {len(window)} intervals of [{_iso_utc(start)}, {_iso_utc(end)}) are "
            "unusable under the benchmark's own mask, so there is no cost basis on which to "
            f"compare arms. Rejections: {_reason_counts([reasons_all[r] for r in window])}"
        )

    # A usable interval must carry a real, finite number.  usable_mask already
    # demands it; this checks anyway, because silently substituting on an interval
    # that is *counted* would understate the bill and no placeholder is innocent there.
    bad_price = [
        int(index[pos])
        for pos in range(len(window))
        if usable[pos]
        and (raw_prices[index[pos]] is None or not math.isfinite(float(raw_prices[index[pos]])))
    ]
    if bad_price:
        raise SystemExit(  # pragma: no cover - usable_mask already rejects these
            "error: price_eur_per_kwh is null or non-finite on an interval the usable mask "
            f"accepted (dataset row {bad_price[0]}): the mask and the numeric core disagree "
            "about what is usable, so refusing to benchmark"
        )
    bad_load = [
        int(index[pos])
        for pos in range(len(window))
        if usable[pos]
        and (raw_gross[index[pos]] is None or not math.isfinite(float(raw_gross[index[pos]])))
    ]
    if bad_load:
        raise SystemExit(  # pragma: no cover - usable_mask already rejects these
            "error: gross_kwh is null or non-finite on an interval the usable mask accepted "
            f"(dataset row {bad_load[0]}): refusing to benchmark"
        )

    # Every usable interval now holds a finite price and a finite energy total, so
    # anything still non-finite below sits on an interval the mask excluded.  The
    # core's Horizon demands finite arrays end to end — an excluded interval is
    # forced to zero flow and contributes no cost, so a placeholder is sound — but
    # the substitution is counted and recorded rather than left invisible.
    prices = np.where(np.isfinite(prices_raw), prices_raw, 0.0)
    gross = np.where(np.isfinite(gross_raw), gross_raw, 0.0)

    null_masked = sum(
        1 for pos, row in enumerate(window) if excluded[pos] and raw_prices[row] is None
    )
    nonfinite_masked = int(np.count_nonzero(~np.isfinite(prices_raw[excluded])))

    horizon = Horizon(
        interval_seconds=dataset.interval_seconds,
        prices=prices,
        load_kwh=gross,
        usable=usable,
        import_limit_kw=import_limit_kw,
    )

    quality = {
        "usable_mask_source": (
            "energy_baseline.benchmark.usable_mask — allocation, energy and price quality plus "
            "null and non-finite checks — not an energy-only mask"
        ),
        "allow_reconstructed_quality": bool(allow_reconstructed),
        "window_interval_count": int(len(window)),
        "usable_interval_count": int(usable.sum()),
        "unusable_interval_count": int(excluded.sum()),
        "usable_fraction": float(usable.sum()) / float(len(window)),
        "usable_mask_sha256": _canonical_sha([bool(v) for v in usable]),
        "exclusion_reason_counts": _reason_counts([reasons_all[r] for r in window]),
        "first_window_interval_start": _iso_utc(window_starts[0]),
        "last_window_interval_start": _iso_utc(window_starts[-1]),
        "scored_window_end_exclusive": _iso_utc(cols["ends"][window[-1]]),
        "masked_price_substitutions": {
            "null_price_intervals": int(null_masked),
            "non_finite_price_intervals": int(nonfinite_masked),
            "note": (
                "these intervals are excluded by the usable mask, so the core forces zero flow "
                "and no cost contribution from them. Their price is carried as 0.0 in the arrays "
                "the solver sees only because the core requires finite arrays end to end and a "
                "null cannot be solved; they are never counted as free energy, and none of them "
                "is usable. A null or non-finite price on a USABLE interval is refused outright, "
                "never substituted"
            ),
        },
    }
    return horizon, window_starts, quality


# ---------------------------------------------------------------------------
# Per-arm executed-action evidence
# ---------------------------------------------------------------------------


def _action_evidence(
    schedule: Any,
    window_starts: list[datetime],
    *,
    include_arrays: bool,
) -> dict[str, Any]:
    """Evidence that this arm's plan exists, was solved on its own, and moves energy.

    ``first_nonidle_*`` is the first interval in which the plan actually charges
    or discharges.  It is deliberately distinct from the window's first interval:
    an interval that exists in the window says nothing about whether any action
    was planned in it.
    """
    charge = np.asarray(schedule.charge_kwh, dtype=float)
    discharge = np.asarray(schedule.discharge_kwh, dtype=float)
    soc = np.asarray(schedule.soc_kwh, dtype=float)
    moved = (np.abs(charge) > _IDLE_TOLERANCE_KWH) | (np.abs(discharge) > _IDLE_TOLERANCE_KWH)
    nonidle = [int(i) for i in np.flatnonzero(moved)]

    payload = {
        "charge_kwh": _array(charge),
        "discharge_kwh": _array(discharge),
        "soc_kwh": _array(soc),
    }
    first = nonidle[0] if nonidle else None
    last = nonidle[-1] if nonidle else None

    evidence: dict[str, Any] = {
        "action_sha256": _canonical_sha(payload),
        "charge_sha256": _canonical_sha(payload["charge_kwh"]),
        "discharge_sha256": _canonical_sha(payload["discharge_kwh"]),
        "soc_sha256": _canonical_sha(payload["soc_kwh"]),
        "nonidle_interval_count": len(nonidle),
        "nonidle_indices_sha256": _canonical_sha(nonidle),
        "idle_interval_count": int(charge.size - len(nonidle)),
        "battery_planned_nonidle": bool(nonidle),
        "first_nonidle_index": first,
        "first_nonidle_interval_start": (
            _iso_utc(window_starts[first]) if first is not None else None
        ),
        "last_nonidle_index": last,
        "last_nonidle_interval_start": (
            _iso_utc(window_starts[last]) if last is not None else None
        ),
        "note": (
            "first_nonidle_index / first_nonidle_interval_start are the first interval in which "
            "this plan actually moves energy; the window's own first interval is recorded under "
            "quality.first_window_interval_start and is not evidence of any action"
        ),
    }
    if include_arrays:
        evidence["charge_kwh"] = payload["charge_kwh"]
        evidence["discharge_kwh"] = payload["discharge_kwh"]
        evidence["soc_kwh"] = payload["soc_kwh"]
    else:
        evidence["arrays_omitted"] = True
    return evidence


def _run_pair(
    spec: BatterySpec,
    horizon: Horizon,
    window_starts: list[datetime],
    *,
    planning_terminal_value: float,
    throughput_cost: float,
    scoring_terminal_value: float,
    terminal_mode: str,
    time_limit_s: float,
    mip_rel_gap: float,
    mip_abs_gap: float,
    include_arrays: bool,
) -> dict[str, Any]:
    """Optimise one ``(planning_terminal_value, throughput_cost)`` pair and score it.

    The solve goes through :func:`optimize_with_throughput`, so this arm's own
    objective is what the solver minimises and the plan's arrays are kept.  The
    comparison basis comes from :func:`score_fixed_schedule` — pure rescoring,
    with no second implementation of the physics or the accounting here.
    """
    solve_terminal_value = planning_terminal_value if terminal_mode == "free" else None
    identity = {
        "planning_terminal_value_eur_per_kwh": float(planning_terminal_value),
        "throughput_cost_eur_per_kwh": float(throughput_cost),
        "scoring_terminal_value_eur_per_kwh": float(scoring_terminal_value),
        "terminal_mode": terminal_mode,
    }
    try:
        schedule, plan_info = optimize_with_throughput(
            spec,
            horizon,
            throughput_cost,
            terminal_mode=terminal_mode,
            terminal_value_eur_per_kwh=solve_terminal_value,
            time_limit_s=time_limit_s,
            mip_rel_gap=mip_rel_gap,
            mip_abs_gap=mip_abs_gap,
        )
        scored = score_fixed_schedule(schedule, throughput_cost, scoring_terminal_value)
    except _ARM_ERRORS as exc:
        return {
            **identity,
            "ran": False,
            "failure": f"{type(exc).__name__}: {exc}",
            "note": _HONESTY_NOTE,
        }

    return {
        **identity,
        "ran": True,
        "meter_cost_eur": _finite_or_none(schedule.meter_cost_eur),
        "total_ac_throughput_kwh": _finite_or_none(plan_info["total_ac_throughput_kwh"]),
        "throughput_penalty_eur": _finite_or_none(plan_info["throughput_penalty_eur"]),
        "efc": _finite_or_none(schedule.efc),
        "initial_soc_kwh": _finite_or_none(schedule.initial_soc_kwh),
        "end_soc_kwh": _finite_or_none(schedule.terminal_soc_kwh),
        "planning_inventory_adjustment_eur": _finite_or_none(plan_info["inventory_adjustment_eur"]),
        "planning_objective_eur": _finite_or_none(plan_info["degradation_adjusted_objective_eur"]),
        "scoring_inventory_adjustment_eur": _finite_or_none(scored["inventory_adjustment_eur"]),
        "degradation_adjusted_objective_eur": _finite_or_none(
            scored["degradation_adjusted_objective_eur"]
        ),
        "solver_status": str(schedule.solver_status),
        "is_certified": bool(schedule.is_certified),
        "solver_gap_eur": _finite_or_none(schedule.solver_gap),
        "bound_kind": str(schedule.bound_kind),
        "cost_lower_bound_eur": _finite_or_none(schedule.cost_lower_bound_eur),
        "cost_upper_bound_eur": _finite_or_none(schedule.cost_upper_bound_eur),
        "declared_time_limit_s": float(time_limit_s),
        "usable_interval_count": int(np.count_nonzero(horizon.usable)),
        "window_interval_count": int(horizon.n),
        "action": _action_evidence(schedule, window_starts, include_arrays=include_arrays),
        "note": _HONESTY_NOTE,
    }


# ---------------------------------------------------------------------------
# Lineage and grid summary
# ---------------------------------------------------------------------------


def _lineage(dataset: Any, config_path: Path) -> dict[str, Any]:
    """Everything needed to re-obtain these exact inputs — minus any euro figure."""
    source_hashes = {}
    for rel in _SOURCE_FILES:
        path = _PROJECT / rel
        source_hashes[rel] = _file_sha256(path) if path.is_file() else None
    return {
        "dataset_id": dataset.dataset_id,
        "dataset_directory": str(Path(dataset.directory).resolve()),
        "build_manifest_sha256": dataset.manifest_sha256,
        "build_manifest": dataset.manifest,
        "dataset_part_files": [
            {
                "path": str(entry.get("path")),
                "sha256": entry.get("verified_sha256") or entry.get("sha256"),
                "rows": entry.get("rows"),
            }
            for entry in dataset.part_files
        ],
        "dataset_unlisted_parts": list(dataset.unlisted_parts),
        "dataset_interval_seconds": float(dataset.interval_seconds),
        "dataset_coverage": {
            "start": _iso_utc(dataset.start),
            "end": _iso_utc(dataset.end),
            "intervals": int(dataset.table.num_rows),
        },
        "config_path": str(config_path.resolve()),
        "config_hash_canonical": config_hash(config_path),
        "config_sha256_raw_bytes": _file_sha256(config_path),
        "code_version": code_version(),
        "source_hashes": source_hashes,
        "pinning": (
            "the build manifest pins config_hash and code_version at build time; the config "
            "hash above is recomputed from the file actually used and must match, so these "
            "numbers cannot come from one build's tariff and another build's intervals"
        ),
    }


def _grid_summary(rows: list[dict[str, Any]]) -> dict[str, Any]:
    """Independent-solve and digest-collision evidence across the whole grid."""
    ran = [r for r in rows if r.get("ran")]
    by_params: dict[tuple[float, float], set[str]] = {}
    for row in ran:
        key = (
            float(row["planning_terminal_value_eur_per_kwh"]),
            float(row["throughput_cost_eur_per_kwh"]),
        )
        by_params.setdefault(key, set()).add(row["action"]["action_sha256"])
    return {
        "pairs_requested": len(rows),
        "pairs_solved": len(ran),
        "pairs_failed": len(rows) - len(ran),
        "distinct_parameter_pairs": len(by_params),
        "distinct_action_digests": len({r["action"]["action_sha256"] for r in ran}),
        "parameter_pairs_with_several_action_digests": sorted(
            f"planning={key[0]},throughput={key[1]}"
            for key, digests in by_params.items()
            if len(digests) > 1
        ),
        "note": (
            "every requested pair is its own solver call: no plan is shared or reused across "
            "arms, so repeating a parameter must reproduce the same digest. Anything listed "
            "under parameter_pairs_with_several_action_digests is genuine non-determinism in "
            "that run (the usual cause is a time limit reached mid-sweep), not a runner bug"
        ),
    }


# ---------------------------------------------------------------------------
# Validation of the knobs that must be checked before loading anything
# ---------------------------------------------------------------------------


def _validate_knobs(args: argparse.Namespace) -> None:
    """Reject impossible knobs before touching the dataset or the solver."""
    negatives = [v for v in args.throughput_costs if v < 0.0]
    if negatives:
        raise SystemExit(
            f"error: --throughput-costs must be >= 0 (a negative cost would pay the battery to "
            f"cycle); got {negatives}"
        )
    for name, value in (
        ("--time-limit-s", args.time_limit_s),
        ("--mip-rel-gap", args.mip_rel_gap),
        ("--mip-abs-gap", args.mip_abs_gap),
    ):
        if not math.isfinite(value) or value <= 0:
            raise SystemExit(f"error: {name} must be finite and > 0, got {value!r}")
    if not math.isfinite(args.scoring_terminal_value):
        raise SystemExit(
            f"error: --scoring-terminal-value must be finite, got {args.scoring_terminal_value!r}"
        )
    if args.initial_soc_frac is not None and not math.isfinite(args.initial_soc_frac):
        raise SystemExit(f"error: --initial-soc-frac must be finite, got {args.initial_soc_frac!r}")

    # A planning terminal value is inert outside free mode: the fixed cycle returns to
    # its seed SOC, leaving no inventory to value, and solve_oracle forbids the
    # coefficient outright.  Refuse rather than sweep a no-op and report it as a result.
    if args.terminal_mode == "fixed":
        nonzero = [v for v in args.planning_terminal_values if abs(v) > _ZERO_TOLERANCE]
        if nonzero:
            raise SystemExit(
                "error: --terminal-mode fixed pins the end SOC to the seed, so a planning "
                f"terminal value cannot enter the objective: {nonzero} would be silently "
                "ignored and the sweep would measure nothing. Use --terminal-mode free to sweep "
                "planning terminal values, or pass only 0.0 to sweep throughput costs alone"
            )


# ---------------------------------------------------------------------------
# Main
# ---------------------------------------------------------------------------


def main(argv: list[str] | None = None) -> int:
    args = _parse_args(argv)
    _validate_knobs(args)

    # Real outputs stay out of the repository, and out of another run's directory.
    output_dir = _resolve_output_dir(args.output_dir)

    # Load and pin the inputs.  BenchmarkInputError is the benchmark's own declared
    # refusal (no manifest, tampered part, unconfirmed config field, naive or future
    # cut); it is reported as a clean message rather than a stack trace.
    try:
        dataset = load_dataset(args.dataset)
    except (BenchmarkInputError, OSError) as exc:
        raise SystemExit(f"error: dataset cannot be loaded: {exc}") from exc
    _reject_output_inside_dataset(output_dir, Path(dataset.directory).resolve())
    config_path = Path(args.config).expanduser().resolve()
    if not config_path.is_file():
        raise SystemExit(f"error: tariff config not found: {config_path}")
    try:
        config = load_config(config_path)
    except (ValueError, KeyError) as exc:
        raise SystemExit(f"error: tariff config {config_path} is not valid: {exc}") from exc

    pinned = dataset.manifest.get("config_hash")
    actual = config_hash(config_path)
    if not pinned:
        raise SystemExit(
            f"error: dataset {dataset.dataset_id}: its build manifest pins no config_hash, so "
            "the tariff these intervals were built against is unknown — refusing to sweep "
            "battery economics on an unknown tariff"
        )
    if pinned != actual:
        raise SystemExit(
            f"error: tariff config drift for dataset {dataset.dataset_id}: the build manifest "
            f"pins config_hash={str(pinned)[:12]} but {config_path} hashes to {actual[:12]}. "
            "This dataset was built against a different tariff (and different battery "
            "assumptions) than the one being swept. Pass the config it was built with, or "
            "rebuild — there is no bypass"
        )

    cols = _read_table(dataset, allow_reconstructed=args.allow_reconstructed_quality)
    try:
        temporal = _resolve_windows(
            args,
            dataset_start=dataset.start,
            dataset_end=dataset.end,
        )
    except BenchmarkInputError as exc:
        raise SystemExit(f"error: the declared windows are not usable: {exc}") from exc
    scored_start, scored_end = temporal["_scored_bounds"]
    training_bounds = temporal["_training_bounds"]

    # Canonical config -> BatterySpec, every field, with provenance.  Passing
    # initial_soc_frac=None takes the config's own value; an override is validated
    # against the configured SOC window by the same helper.
    try:
        assumptions = resolve_battery_assumptions(config, initial_soc_frac=args.initial_soc_frac)
    except BenchmarkInputError as exc:
        raise SystemExit(f"error: the configured battery cannot be resolved: {exc}") from exc
    if assumptions.idle_losses_measured:
        raise SystemExit(
            "error: the config carries battery.idle_loss_kwh_per_hour, but the core BatterySpec "
            "has no idle-loss field, so this runner would silently drop a measured loss. Add "
            "the field to the core first — do not sweep with the loss ignored"
        )
    spec = BatterySpec(**assumptions.spec_kwargs())

    horizon, window_starts, quality = _build_horizon(
        dataset,
        cols,
        start=scored_start,
        end=scored_end,
        import_limit_kw=assumptions.import_limit_kw,
        allow_reconstructed=args.allow_reconstructed_quality,
    )

    pairs = len(args.planning_terminal_values) * len(args.throughput_costs)
    print(f"dataset           {dataset.dataset_id} ({dataset.table.num_rows} intervals)")
    print(
        f"scored window     {temporal['window']}: {temporal['scored_window']['start']} .. "
        f"{temporal['scored_window']['end']}"
    )
    print(
        f"usable            {quality['usable_interval_count']}/"
        f"{quality['window_interval_count']} ({quality['usable_fraction']:.3%})"
    )
    print(
        f"battery           capacity={spec.capacity_kwh} kWh, initial_soc_frac="
        f"{spec.initial_soc_frac} (source: {assumptions.initial_soc_source.split(':')[0]})"
    )
    print(f"grid              {pairs} independent solves", flush=True)

    rows: list[dict[str, Any]] = []
    for planning_value in args.planning_terminal_values:
        for throughput_cost in args.throughput_costs:
            row = _run_pair(
                spec,
                horizon,
                window_starts,
                planning_terminal_value=planning_value,
                throughput_cost=throughput_cost,
                scoring_terminal_value=args.scoring_terminal_value,
                terminal_mode=args.terminal_mode,
                time_limit_s=args.time_limit_s,
                mip_rel_gap=args.mip_rel_gap,
                mip_abs_gap=args.mip_abs_gap,
                include_arrays=not args.omit_plan_arrays,
            )
            rows.append(row)
            status = "ok" if row.get("ran") else f"FAILED ({row.get('failure')})"
            print(
                f"  planning={row['planning_terminal_value_eur_per_kwh']:<10} "
                f"throughput={row['throughput_cost_eur_per_kwh']:<10} {status}",
                flush=True,
            )

    # One scoring basis across all arms is what makes the arms comparable:
    # assert it in the runner rather than trusting the loop above.
    off_basis = [
        row
        for row in rows
        if row.get("ran")
        and float(row["scoring_terminal_value_eur_per_kwh"]) != float(args.scoring_terminal_value)
    ]
    if off_basis:
        raise SystemExit(
            f"error: {len(off_basis)} arms were scored under a terminal value other than "
            f"--scoring-terminal-value={args.scoring_terminal_value}; the arms would not be "
            "comparable, so the artifact is not written"
        )

    artifact: dict[str, Any] = {
        "artifact": ARTIFACT_NAME,
        "runner": "hack/economic_sensitivity.py",
        "determinism": {
            "wall_clock_in_artifact": False,
            "note": (
                "this artifact contains no reading of the system clock, so two runs over the "
                "same inputs produce byte-identical output. The solver's time_limit_s is a "
                "declared budget, not a measurement, and no observed duration appears anywhere. "
                "Determinism is checkable by re-running and diffing the digests"
            ),
        },
        "lineage": _lineage(dataset, config_path),
        "temporal": temporal,
        "training_window_coverage": _window_coverage(
            cols["starts"], cols["usable"], training_bounds
        ),
        "scored_window_coverage": _window_coverage(
            cols["starts"], cols["usable"], (scored_start, scored_end)
        ),
        "window_coverage_note": (
            "both windows are recorded with the canonical usable mask for provenance. The "
            "training window is never used to fit or derive a sweep parameter: there is no "
            "fitted threshold, mean, quantile, or rate anywhere in this runner"
        ),
        "parameters": {
            "planning_terminal_values_eur_per_kwh": [
                float(v) for v in args.planning_terminal_values
            ],
            "throughput_costs_eur_per_kwh": [float(v) for v in args.throughput_costs],
            "scoring_terminal_value_eur_per_kwh": float(args.scoring_terminal_value),
            "terminal_mode": args.terminal_mode,
            "time_limit_s": float(args.time_limit_s),
            "mip_rel_gap": float(args.mip_rel_gap),
            "mip_abs_gap": float(args.mip_abs_gap),
            "provenance": (
                "every monetary parameter above is an explicit command-line array. Nothing is "
                "derived from the training window, the scored window, or any other data"
            ),
            "fixed_mode_rule": (
                "terminal-mode fixed pins the end SOC to the seed and forbids a terminal "
                "valuation, so it is accepted only when every planning terminal value is 0.0; "
                "otherwise the sweep would measure nothing and still look like a result"
            ),
        },
        "units_and_naming": _UNITS,
        "battery": {
            "assumptions": assumptions.as_dict(),
            "spec": asdict(spec),
            "spec_source": (
                "energy_baseline.benchmark.resolve_battery_assumptions(config, "
                "initial_soc_frac=<--initial-soc-frac or None>): every BatterySpec field, the "
                "efficiency mapping, the SOC window and the import ceiling come from the config "
                "with their provenance recorded. --initial-soc-frac overrides the configured "
                "value and is validated against the configured SOC window; it is never a local "
                "default"
            ),
        },
        "quality": quality,
        "grid": _grid_summary(rows),
        "results": rows,
        "complete": all(row.get("ran") for row in rows),
        "notes": [
            _HONESTY_NOTE,
            "each (planning_terminal_value, throughput_cost) pair is its own solve via "
            "economic_sensitivity.optimize_with_throughput, so the arm's objective is what the "
            "solver minimised; the plan's arrays are kept and digested as executed-action "
            "evidence rather than discarded by an aggregate-only sweep",
            "the comparison basis is economic_sensitivity.score_fixed_schedule under the "
            "single --scoring-terminal-value; no physics or accounting is reimplemented here",
            "a degraded arm (declared solver or physics error) is recorded with ran=false and "
            "its failure message; any other exception aborts with a traceback instead of being "
            "swallowed",
            "if grid.pairs_failed is non-zero the exit status is 1, because a partial sweep "
            "must never read as a complete one",
        ],
    }

    payload = json.dumps(artifact, indent=2, allow_nan=False, default=str, sort_keys=True)
    output_dir.mkdir(parents=True, exist_ok=True)
    artifact_path = output_dir / ARTIFACT_NAME
    artifact_path.write_text(payload)
    print(f"\nwrote {artifact_path}")
    print(f"sha256 {hashlib.sha256(payload.encode('utf-8')).hexdigest()}")

    manifest_path = finalize_run_outputs(output_dir, [ARTIFACT_NAME], MANIFEST_NAME)
    print(f"wrote {manifest_path}")

    failed = artifact["grid"]["pairs_failed"]
    if failed:
        print(
            f"\n{failed} of {artifact['grid']['pairs_requested']} arms did not solve; the "
            "artifact records each failure. Exit status 1 so a partial sweep is never read as "
            "a complete one",
            file=sys.stderr,
        )
        return 1
    return 0


if __name__ == "__main__":
    try:
        raise SystemExit(main())
    except SystemExit:
        raise
    except KeyboardInterrupt:  # pragma: no cover - interactive use
        print("interrupted", file=sys.stderr)
        raise SystemExit(130)
    except Exception:
        traceback.print_exc()
        raise SystemExit(1)
