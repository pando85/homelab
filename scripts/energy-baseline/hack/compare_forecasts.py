#!/usr/bin/env python3
"""Compare the two-event rolling horizon against the existing day-ahead policy.

Standalone experiment runner. It loads a **built** dataset through its manifest,
freezes one load-forecast profile per training window from data strictly before
the test cut, runs the existing ``policy_day_ahead`` and the new two-event
rolling-horizon dispatcher on that same frozen profile, and writes private JSON
artifacts under ``--output`` (which must live outside the repository).

Usage:
    uv run python hack/compare_forecasts.py \\
        --dataset /path/to/analysis/v1/<dataset_id> \\
        --config config/tariff.yaml \\
        --output /tmp/energy-compare/run-1 \\
        --test-start 2025-10-01T00:00:00Z \\
        --test-end 2026-01-01T00:00:00Z \\
        --windows 28,365,all \\
        --sigmas 0,0.1,0.2 \\
        --bias-sigmas 0,0.1 \\
        --seeds 0,1,2

Cadence (ADR-0001's replan loop) is opt-in and defaults to the measured behaviour:

    uv run python hack/compare_forecasts.py ... \\
        --replan-interval-slots 1 --horizon-slots 96

``--replan-interval-slots`` omitted keeps the legacy two-publication schedule (14:00
and 20:30 local), bit-identical to every run already on disk. Giving it re-solves every
that many 15-minute intervals over a ``--horizon-slots`` horizon (default 96 = 24 h) and
executes only the next interval — the mode a real controller runs. **It multiplies the
solve count by roughly 96/cadence against a once-per-day baseline, which against the legacy
*two*-event schedule works out at ~48x:** over a 92-day holdout the legacy schedule is ~185
solves and a 15-minute cadence is ~8,832. Wall time rises *more slowly* than the count —
about 37x, not 48x — because a fine cadence solves faster per call (~20 ms against ~25 ms: a
legacy event plans to the end of D+1, ~34 h, while a cadence-1 solve plans a fixed 96 slots).
The count is reported per run as ``rolling_horizon.solve_count`` and summarised in
``replan_cadence.solve_counts_observed``, so a run's cost is visible in its own
artifact rather than discovered later.

Honesty rules this runner is written to:

* **Train before test.** The frozen load profile and the terminal-SOC valuation are
  derived from intervals strictly before ``--test-start``. Evaluation scores the
  held-out window only.
* **Noise is planning-only.** Scoring is always ``dot(true_price, actual_grid)``.
* **Paired designs share their randomness.** Price noise (per-slot and day-level bias)
  is drawn per ``(seed, local_date)``, so two runs that differ only in their load-forecast
  window see the *same* price realisation. That is what makes a window comparison a
  controlled comparison rather than two unrelated draws.
* **No silent swallowing.** Only the numeric core's declared solver/physics errors
  degrade a policy to a recorded failure; any other exception is a bug and aborts
  with a non-zero exit status and a traceback.
* **Costs are withheld from source.** Numbers are written only to ``--output``.

No euro figure, and no wall-clock timing, is written into the repository.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import math
import sys
import traceback
from datetime import UTC, datetime
from pathlib import Path
from typing import Any
from zoneinfo import ZoneInfo

import numpy as np

# Add src to path for direct execution (the package is not pip-installed for this run)
_here = Path(__file__).resolve().parent
_src = _here.parent / "src"
if str(_src) not in sys.path:
    sys.path.insert(0, str(_src))

from energy_baseline.artifact_integrity import finalize_run_outputs  # noqa: E402
from energy_baseline.battery import BatteryPhysicsError, BatterySpec, Horizon  # noqa: E402
from energy_baseline.benchmark import (  # noqa: E402
    BenchmarkInputError,
    load_dataset,
    resolve_battery_assumptions,
    usable_mask,
)
from energy_baseline.config import load_config  # noqa: E402

# _get_code_version is private, but necessary: the runner needs the (rev, dirty)
# tuple to record git_head and git_dirty as separate fields. The public code_version()
# returns a combined string like "abc123-dirty", which would require fragile parsing
# to extract the components. The private API is stable and used internally by
# code_version() itself.
from energy_baseline.manifest import (  # noqa: E402
    _get_code_version,
    code_version,
    config_hash,
)
from energy_baseline.oracle import OracleError  # noqa: E402
from energy_baseline.policies import policy_no_battery  # noqa: E402
from energy_baseline.rolling_horizon import (  # noqa: E402
    build_frozen_load_profile,
    run_day_ahead_with_frozen_forecast,
    run_rolling_horizon,
)

MADRID = ZoneInfo("Europe/Madrid")

#: Errors that represent a legitimate dispatch outcome rather than a programming
#: error. Anything else propagates: swallowing ``ValueError`` here would hide an
#: inverted argument as "that policy failed", which is the difference between a
#: measurement and a fiction.
_POLICY_ERRORS: tuple[type[BaseException], ...] = (OracleError, BatteryPhysicsError)

#: Project root (parent of hack/).
_PROJECT = _here.parent

#: Files whose bytes are hashed into ``source_hashes`` (relative to the project).
#: Derived by tracing every energy_baseline module the runner imports on the
#: measurement path: direct imports (battery, benchmark, config, oracle,
#: policies, rolling_horizon, manifest, artifact_integrity) plus transitive
#: imports discovered via benchmark (paths, schemas) and the package __init__.
#: cost.py and dispatch.py are NOT on this path — they are imported only by
#: replay_runtime_demo.py, which this runner does not use.
_SOURCE_FILES: tuple[str, ...] = (
    "hack/compare_forecasts.py",
    "src/energy_baseline/__init__.py",
    "src/energy_baseline/artifact_integrity.py",
    "src/energy_baseline/battery.py",
    "src/energy_baseline/benchmark.py",
    "src/energy_baseline/config.py",
    "src/energy_baseline/manifest.py",
    "src/energy_baseline/oracle.py",
    "src/energy_baseline/paths.py",
    "src/energy_baseline/policies.py",
    "src/energy_baseline/rolling_horizon.py",
    "src/energy_baseline/schemas.py",
)


def _file_sha256(path: Path) -> str:
    """SHA-256 of a file's raw bytes."""
    digest = hashlib.sha256()
    with open(path, "rb") as handle:
        for chunk in iter(lambda: handle.read(8192), b""):
            digest.update(chunk)
    return digest.hexdigest()


def _compute_source_hashes() -> dict[str, str | None]:
    """Hash every module on the measurement path. Missing files get None."""
    return {
        rel: _file_sha256(_PROJECT / rel) if (_PROJECT / rel).is_file() else None
        for rel in _SOURCE_FILES
    }


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


def _parse_int_list(value: str, *, name: str) -> list[int]:
    parts = [p.strip() for p in value.split(",") if p.strip()]
    try:
        out = [int(p) for p in parts]
    except ValueError as exc:
        raise SystemExit(f"ERROR: {name} must be comma-separated integers, got {value!r}") from exc
    if not out:
        raise SystemExit(f"ERROR: {name} is empty")
    return out


def _parse_sigmas(value: str, *, name: str = "--sigmas") -> list[float]:
    """Parse a comma-separated list of noise magnitudes (per-slot sigma or day bias).

    Both channels are standard deviations of a *relative* multiplier, so they share the
    same validity rules: a negative one is not a magnitude, and a NaN/inf would make
    every estimated price non-finite while the run still "succeeded".
    """
    parts = [p.strip() for p in value.split(",") if p.strip()]
    try:
        out = [float(p) for p in parts]
    except ValueError as exc:
        raise SystemExit(f"ERROR: {name} must be comma-separated floats, got {value!r}") from exc
    if not out:
        raise SystemExit(f"ERROR: {name} is empty")
    for sigma in out:
        # A noise magnitude cannot be negative, and a NaN sigma would make every
        # noisy price NaN and the whole run meaningless while still "succeeding".
        if not math.isfinite(sigma):
            raise SystemExit(f"ERROR: {name} entries must be finite, got {sigma!r}")
        if sigma < 0.0:
            raise SystemExit(
                f"ERROR: {name} entries must be >= 0 (a noise magnitude), got {sigma!r}"
            )
    return out


def _parse_windows(value: str) -> list[int]:
    parts = [p.strip() for p in value.split(",") if p.strip()]
    if not parts:
        raise SystemExit("ERROR: --windows is empty")
    out: list[int] = []
    for part in parts:
        if part == "all":
            out.append(-1)  # sentinel: every day available before the cut
            continue
        try:
            number = int(part)
        except ValueError as exc:
            raise SystemExit(
                f"ERROR: --windows entries must be positive integers or 'all', got {part!r}"
            ) from exc
        if number < 1:
            raise SystemExit(f"ERROR: --windows entry must be >= 1 or 'all', got {number}")
        out.append(number)
    return out


def _resolve_window(window: int, available_days: int) -> int:
    """Clamp a requested trailing window to what the training split actually holds."""
    if window == -1:
        return max(1, available_days)
    return min(window, max(1, available_days))


def _require_finite(name: str, values: np.ndarray) -> None:
    """Refuse a dataset column holding NaN/inf before it becomes a plausible-looking cost.

    ``usable_mask`` already refuses non-finite energy and price on the rows it marks
    unusable; this is the belt-and-braces check that no **usable** interval carries
    one, because a NaN load in an otherwise-usable row would silently poison a dot
    product into a NaN cost.
    """
    bad = ~np.isfinite(values)
    if bad.any():
        first = int(np.flatnonzero(bad)[0])
        raise SystemExit(
            f"ERROR: {name} holds a non-finite value at index {first} "
            f"({values[first]!r}); refusing to benchmark a series that is not finite"
        )


def _column(table: Any, name: str) -> np.ndarray:
    """Read one numeric column, mapping nulls to 0.0 (they are mask-excluded anyway)."""
    return np.array(
        [0.0 if v is None else float(v) for v in table.column(name).to_pylist()],
        dtype=float,
    )


# ---------------------------------------------------------------------------
# Small metric helpers
# ---------------------------------------------------------------------------


def _mean(values: np.ndarray) -> float | None:
    return float(values.mean()) if values.size else None


def _mape_within(
    predicted: np.ndarray, actual: np.ndarray
) -> tuple[float | None, float, float | None, float | None]:
    """MAPE + zero-denominator count + fraction within 10%/20%, over non-zero truths."""
    nonzero = actual != 0.0
    zero_denominator = int((~nonzero).sum())
    if not nonzero.any():
        return None, zero_denominator, None, None
    abs_err = np.abs(predicted[nonzero] - actual[nonzero])
    denom = np.abs(actual[nonzero])
    return (
        float((abs_err / denom).mean()),
        zero_denominator,
        float((abs_err <= 0.1 * denom).mean()),
        float((abs_err <= 0.2 * denom).mean()),
    )


def _error_block(predicted: np.ndarray, actual: np.ndarray) -> dict[str, Any]:
    """Accuracy of a forecast/estimate against the measured truth on usable intervals."""
    mae = _mean(np.abs(predicted - actual))
    mape, zero_denominator, within_10, within_20 = _mape_within(predicted, actual)
    return {
        "intervals": int(predicted.size),
        "mae": mae,
        "mape_excluding_zero_denominator": mape,
        "zero_denominator_intervals": zero_denominator,
        "fraction_within_10pct": within_10,
        "fraction_within_20pct": within_20,
    }


def _policy_block(
    *,
    ran: bool,
    cost_eur: float | None,
    baseline_cost_eur: float,
    schedule: Any = None,
    telemetry: dict[str, Any] | None = None,
    failure: str | None = None,
) -> dict[str, Any]:
    """One policy's score card: cost, saving, cycling, and the stage that failed."""
    if not ran or cost_eur is None:
        return {
            "ran": False,
            "cost_eur": None,
            "saving_eur": None,
            "failure": failure or "not-run",
        }

    block: dict[str, Any] = {
        "ran": True,
        "cost_eur": float(cost_eur),
        "saving_eur": float(baseline_cost_eur - cost_eur),
        "meter_cost_eur": _scalar(schedule, "meter_cost_eur"),
        "inventory_adjustment_eur": _scalar(schedule, "inventory_adjustment_eur"),
        "initial_soc_kwh": _scalar(schedule, "initial_soc_kwh"),
        "terminal_soc_kwh": _scalar(schedule, "terminal_soc_kwh"),
        "efc": _scalar(schedule, "efc"),
        "throughput_kwh": _scalar(schedule, "throughput_kwh"),
        "charge_kwh": float(np.asarray(schedule.charge_kwh).sum()) if schedule else None,
        "discharge_kwh": float(np.asarray(schedule.discharge_kwh).sum()) if schedule else None,
        "ceiling_exceeding_intervals": _scalar(schedule, "ceiling_exceeding_intervals"),
    }

    detail = telemetry or {}
    # Stage failures and clipping are what tell a reader WHY a policy is expensive;
    # a bare cost cannot distinguish "optimised well" from "half the days gave up".
    for key in (
        "solve_count",
        "planning_events_not_solved",
        "replan_interval_slots",
        "replan_cadence",
        "horizon_slots",
        "horizon_hours_nominal",
        "executed_multiplicity_mean",
        "planning_multiplicity_mean",
        "planning_multiplicity_max",
        "planned_interval_slots_total",
        "executed_interval_slots_total",
        "clipping_totals_comparable_across_cadences",
        "events_solved",
        "events_infeasible",
        "events_no_incumbent",
        "events_time_limit",
        "events_skipped_no_usable",
        "events_frozen_by_seed",
        "events_with_clipping",
        "charge_clipped_kwh",
        "discharge_clipped_kwh",
        "days_solved",
        "days_infeasible",
        "days_no_incumbent",
        "days_at_time_limit",
        "days_skipped_no_usable_intervals",
        "days_without_headroom",
        "days_with_clipping",
    ):
        if key in detail:
            block[key] = detail[key]
    return block


def _scalar(obj: Any, name: str) -> float | None:
    value = getattr(obj, name, None) if obj is not None else None
    if value is None:
        return None
    out = float(value)
    return out if math.isfinite(out) else None


# ---------------------------------------------------------------------------
# Main
# ---------------------------------------------------------------------------


def main() -> int:
    parser = argparse.ArgumentParser(
        description="Compare the two-event rolling horizon with the existing day-ahead policy"
    )
    parser.add_argument("--dataset", required=True, help="Built dataset directory (has a manifest)")
    parser.add_argument("--config", required=True, help="tariff.yaml used to build that dataset")
    parser.add_argument("--output", required=True, help="Output directory; must be fresh")
    parser.add_argument("--test-start", required=True, help="Evaluation start (aware ISO 8601)")
    parser.add_argument("--test-end", required=True, help="Evaluation end (aware ISO 8601)")
    parser.add_argument(
        "--windows", default="28", help="Trailing windows: comma-separated days or 'all'"
    )
    parser.add_argument("--sigmas", default="0", help="Price-noise sigmas, e.g. 0,0.1,0.2")
    parser.add_argument(
        "--bias-sigmas",
        default="0",
        help=(
            "Per-local-day multiplicative price-bias sigmas, e.g. 0,0.1. One draw per "
            "local day shared by every slot of that day and by every load-window variant "
            "with the same (seed, date)"
        ),
    )
    parser.add_argument("--seeds", default="0", help="Price-noise seeds")
    parser.add_argument(
        "--time-limit", type=float, default=5.0, help="Per-solve solver time limit in seconds"
    )
    parser.add_argument(
        "--replan-interval-slots",
        type=int,
        default=None,
        help=(
            "Re-solve every this-many 15-minute intervals (ADR-0001's replan cadence; "
            "1 is the 15-minute controller). Omit to keep the legacy two-publication "
            "schedule: the 14:00 and 20:30 events per local day, which is what the "
            "already-measured runs used. Solve count scales as roughly 96/this value "
            "against a once-per-day baseline (~48x against the legacy two-event schedule "
            "at 1); wall time scales more slowly (~37x) because each solve is cheaper"
        ),
    )
    parser.add_argument(
        "--horizon-slots",
        type=int,
        default=None,
        help=(
            "Planning horizon in 15-minute intervals for --replan-interval-slots runs "
            "(96 = 24 h, 192 = 48 h; ADR-0001 specifies a 24-48 h band, and 96 is the "
            "default). Ignored by the legacy schedule, whose horizon is midnight ending "
            "D+1 and therefore DST-dependent"
        ),
    )
    parser.add_argument(
        "--require-clean-tree",
        action="store_true",
        default=False,
        help=(
            "Refuse to run if the git working tree has uncommitted changes. "
            "Default off so development and tests keep working; enable for "
            "production runs that must be reproducible from a clean commit"
        ),
    )
    args = parser.parse_args()

    test_start = _parse_iso(args.test_start, name="--test-start")
    test_end = _parse_iso(args.test_end, name="--test-end")
    if test_end <= test_start:
        raise SystemExit(f"ERROR: --test-end {test_end} must be after --test-start {test_start}")
    windows = _parse_windows(args.windows)
    sigmas = _parse_sigmas(args.sigmas)
    bias_sigmas = _parse_sigmas(args.bias_sigmas, name="--bias-sigmas")
    seeds = _parse_int_list(args.seeds, name="--seeds")
    if args.time_limit <= 0 or not math.isfinite(args.time_limit):
        raise SystemExit(f"ERROR: --time-limit must be > 0 and finite, got {args.time_limit!r}")
    if args.replan_interval_slots is not None and args.replan_interval_slots < 1:
        raise SystemExit(
            f"ERROR: --replan-interval-slots must be >= 1 (an interval count), got "
            f"{args.replan_interval_slots}"
        )
    if args.horizon_slots is not None and args.horizon_slots < 1:
        raise SystemExit(
            f"ERROR: --horizon-slots must be >= 1 (an interval count), got {args.horizon_slots}"
        )
    if args.horizon_slots is not None and args.replan_interval_slots is None:
        raise SystemExit(
            "ERROR: --horizon-slots needs --replan-interval-slots: the legacy "
            "two-publication schedule plans to midnight ending D+1, so a slot-counted "
            "horizon would be silently ignored and the run would not be what its "
            "artifact claims to be"
        )

    # --require-clean-tree: refuse before creating anything if the tree is dirty.
    # Note: _get_code_version() shells out to git twice with no timeout; a stuck git
    # (index.lock contention, network filesystem) will block the runner indefinitely.
    # This is acceptable for now (low probability, user can kill the process).
    if args.require_clean_tree:
        _rev, _dirty = _get_code_version()
        if _dirty:
            raise SystemExit(
                "ERROR: --require-clean-tree is set but the working tree has uncommitted "
                "changes. Commit or stash first, then re-run. A dirty tree means the code "
                "version recorded in the artifact does not match any committed state, so "
                "the run cannot be reproduced from the repository alone"
            )

    output_dir = Path(args.output).expanduser()
    if output_dir.exists() and any(output_dir.iterdir()):
        raise SystemExit(
            f"ERROR: output directory {output_dir} exists and is not empty. Refusing to "
            "overwrite a previous run: artifacts are compared across runs, so a clobbered "
            "directory silently mixes two measurements. Pick a fresh --output."
        )
    if _inside_git_worktree(output_dir.resolve()):
        raise SystemExit(
            f"ERROR: --output {output_dir} is inside the repository. Results carry costs "
            "computed from private interval data and must not be committed; choose a path "
            "outside the work tree."
        )
    output_dir.mkdir(parents=True, exist_ok=True)

    # ---- load the dataset through its manifest (never a bare parquet glob) ------
    print(f"Loading dataset {args.dataset} ...", flush=True)
    try:
        loaded = load_dataset(args.dataset)
    except BenchmarkInputError as exc:
        raise SystemExit(f"ERROR: dataset cannot be loaded: {exc}") from exc
    config_path = Path(args.config).expanduser()
    if not config_path.is_file():
        raise SystemExit(f"ERROR: tariff config not found: {config_path}")
    config = load_config(config_path)

    table = loaded.table
    total = table.num_rows
    starts = table.column("interval_start").to_pylist()
    local_days = table.column("local_day").to_pylist()
    gross = _column(table, "gross_kwh")
    prices = _column(table, "price_eur_per_kwh")
    usable, reasons = usable_mask(table)

    # The mask is the only guarantee that a usable row has finite values; verify it
    # rather than trusting it, so a NaN can never reach a dot product.
    _require_finite("gross_kwh[usable]", gross[usable])
    _require_finite("price_eur_per_kwh[usable]", prices[usable])

    assumptions = resolve_battery_assumptions(config)
    spec = BatterySpec(**assumptions.spec_kwargs())

    # ---- train / test split ------------------------------------------------------
    eval_index = np.flatnonzero(np.array([test_start <= s < test_end for s in starts], dtype=bool))
    if eval_index.size == 0:
        raise SystemExit(f"ERROR: no intervals fall inside [{test_start}, {test_end})")
    eval_start_idx = int(eval_index[0])
    eval_end_idx = int(eval_index[-1]) + 1
    if np.any(np.diff(eval_index) != 1):
        raise SystemExit(
            "ERROR: the evaluation window is not contiguous in the dataset; a rolling roll "
            "cannot skip intervals without pretending a gap is a plan boundary"
        )
    train_mask = np.array([s < test_start for s in starts], dtype=bool)
    if not (train_mask & usable).any():
        raise SystemExit(
            f"ERROR: no usable interval before {test_start.isoformat()}: nothing to freeze a "
            "frozen forecast or terminal value from, so any score would be fitted on test data"
        )

    train_prices = prices[train_mask & usable]
    terminal_value = float(train_prices.mean())  # frozen training statistic, never test
    unique_train_days = len({local_days[i] for i in np.flatnonzero(train_mask)})

    print(f"dataset          {loaded.dataset_id} ({total} intervals)")
    print(
        f"interval_seconds {loaded.interval_seconds}, usable {int(usable.sum())}/{total} "
        f"({usable.mean():.3%})"
    )
    print(
        f"evaluation       {test_start.isoformat()} .. {test_end.isoformat()} "
        f"({eval_end_idx - eval_start_idx} intervals)"
    )
    print(
        f"training         {int((train_mask & usable).sum())} usable intervals, "
        f"{unique_train_days} local days"
    )
    print(
        f"windows          {windows}, sigmas {sigmas}, bias_sigmas {bias_sigmas}, seeds {seeds}",
        flush=True,
    )
    # Printed before the matrix starts, because it is the run's cost: 185 solves and
    # 8,832 solves are not the same experiment and nobody should find out afterwards.
    if args.replan_interval_slots is None:
        print(
            "cadence          legacy two-publication (14:00 and 20:30 local), "
            "~2 events per evaluation local day",
            flush=True,
        )
    else:
        horizon = args.horizon_slots or 96
        n_eval = eval_end_idx - eval_start_idx
        print(
            f"cadence          re-solve every {args.replan_interval_slots} interval(s) over a "
            f"{horizon}-interval horizon, execute only the first "
            f"({horizon * loaded.interval_seconds / 3600.0:.1f} h); "
            f"~{math.ceil(n_eval / args.replan_interval_slots)} solves for this window",
            flush=True,
        )

    # ---- provenance snapshot at start -------------------------------------------
    # Captured before the run loop so a mid-run edit is detectable as drift.
    # Note: _get_code_version() shells out to git twice with no timeout; a stuck git
    # will block the runner indefinitely. Accepted for now (low probability).
    _rev_start, _dirty_start = _get_code_version()
    code_version_at_start = code_version()
    git_head_at_start: str | None = _rev_start
    git_dirty_at_start: bool | None = _dirty_start
    source_hashes_at_start = _compute_source_hashes()

    # ---- baseline: the same mask, zero battery ----------------------------------
    eval_horizon = Horizon(
        interval_seconds=loaded.interval_seconds,
        prices=prices[eval_start_idx:eval_end_idx],
        load_kwh=gross[eval_start_idx:eval_end_idx],
        usable=usable[eval_start_idx:eval_end_idx],
        import_limit_kw=assumptions.import_limit_kw,
    )
    no_battery = policy_no_battery(spec, eval_horizon)
    baseline_cost = float(no_battery.cost_eur)

    schedule_dir = output_dir / "schedules"
    schedule_dir.mkdir(parents=True, exist_ok=True)
    shared_columns = {
        "timestamps_utc": [
            s.isoformat().replace("+00:00", "Z") for s in starts[eval_start_idx:eval_end_idx]
        ],
        "usable": [bool(v) for v in usable[eval_start_idx:eval_end_idx]],
        "true_price_eur_per_kwh": [float(v) for v in prices[eval_start_idx:eval_end_idx]],
        "true_load_kwh": [float(v) for v in gross[eval_start_idx:eval_end_idx]],
    }

    def write_schedule(name: str, payload: dict[str, Any]) -> None:
        path = schedule_dir / f"{name}.json"
        path.write_text(json.dumps(payload, allow_nan=False, default=str))

    runs: list[dict[str, Any]] = []
    run_index = 0

    for window in windows:
        resolved = _resolve_window(window, unique_train_days)
        label = "all" if window == -1 else str(window)

        # ONE frozen profile per window, learned strictly before the cut and reused by
        # every policy and every sigma/seed. Recomputing it per run would let the test
        # load drift into the forecast through any future refactor.
        frozen_load, frozen_meta = build_frozen_load_profile(
            gross,
            local_days,
            usable,
            training_end_idx=eval_start_idx,
            window_days=resolved,
            interval_starts_utc=starts,
            tz=MADRID,
        )
        eval_forecast = frozen_load[eval_start_idx:eval_end_idx]
        forecast_accuracy = _error_block(
            eval_forecast[usable[eval_start_idx:eval_end_idx]],
            gross[eval_start_idx:eval_end_idx][usable[eval_start_idx:eval_end_idx]],
        )

        # Day-ahead reference for this window (sigma/seed do not affect it).
        try:
            da_schedule, da_telemetry = run_day_ahead_with_frozen_forecast(
                spec,
                eval_horizon,
                frozen_load=frozen_load,
                eval_start_idx=eval_start_idx,
                eval_end_idx=eval_end_idx,
                local_days=local_days,
                terminal_value_eur_per_kwh=terminal_value,
                scoring_terminal_value_eur_per_kwh=0.0,
                time_limit_s=args.time_limit,
            )
            da_cost = float(da_schedule.cost_eur)
            da_failure = None
        except _POLICY_ERRORS as exc:
            da_schedule, da_telemetry, da_cost = None, None, None
            da_failure = f"{type(exc).__name__}: {exc}"

        write_schedule(
            f"window_{label}_day_ahead",
            {
                "policy": "day_ahead",
                "window_days_requested": label,
                "window_days_used": resolved,
                "cost_eur": da_cost,
                **shared_columns,
                **(_flow_columns(da_schedule) if da_schedule is not None else {}),
            },
        )

        for sigma in sigmas:
            for bias_sigma in bias_sigmas:
                for seed in seeds:
                    run_index += 1
                    print(
                        f"  run {run_index:>3}: window={label} sigma={sigma} "
                        f"bias_sigma={bias_sigma} seed={seed}",
                        flush=True,
                    )
                    try:
                        result = run_rolling_horizon(
                            spec,
                            prices=prices,
                            load_kwh=gross,
                            usable=usable,
                            interval_seconds=loaded.interval_seconds,
                            local_days=local_days,
                            interval_starts_utc=starts,
                            eval_start_idx=eval_start_idx,
                            eval_end_idx=eval_end_idx,
                            tz=MADRID,
                            frozen_load=frozen_load,
                            terminal_value_eur_per_kwh=terminal_value,
                            sigma=sigma,
                            bias_sigma=bias_sigma,
                            seed=seed,
                            time_limit_s=args.time_limit,
                            import_limit_kw=assumptions.import_limit_kw,
                            replan_interval_slots=args.replan_interval_slots,
                            horizon_slots=args.horizon_slots,
                        )
                        rh_cost = float(result.cost_eur)
                        rh_telemetry = result.telemetry
                        rh_noise = result.noise_metadata
                        rh_schedule = result.schedule
                        rh_failure = None
                    except _POLICY_ERRORS as exc:
                        rh_cost, rh_telemetry, rh_noise, rh_schedule = None, {}, {}, None
                        rh_failure = f"{type(exc).__name__}: {exc}"

                    # bias_sigma is in the filename on purpose: a day-level bias and an
                    # i.i.d. noise run of the same sigma otherwise differ only in a JSON
                    # field, and mixing them across artifacts would silently merge two
                    # different measurements into one comparison.
                    write_schedule(
                        f"run_{run_index:03d}_window_{label}_sigma_{sigma}"
                        f"_bias_{bias_sigma}_seed_{seed}_rolling",
                        {
                            "policy": "rolling_horizon",
                            "window_days_requested": label,
                            "window_days_used": resolved,
                            "sigma": sigma,
                            "bias_sigma": bias_sigma,
                            "seed": seed,
                            # The cadence belongs in the schedule too: a 15-minute-cadence
                            # file and a legacy file are different controllers over the same
                            # intervals, and a reader comparing two runs' per_event records
                            # has to be able to see which schedule produced them.
                            "replan_interval_slots": args.replan_interval_slots,
                            "horizon_slots": args.horizon_slots,
                            "solve_count": (rh_telemetry or {}).get("solve_count"),
                            "cost_eur": rh_cost,
                            **shared_columns,
                            **(_flow_columns(rh_schedule) if rh_schedule is not None else {}),
                            **(
                                {"per_event": rh_telemetry.get("per_event", [])}
                                if rh_telemetry
                                else {}
                            ),
                        },
                    )

                    runs.append(
                        {
                            "run_index": run_index,
                            "window_days_requested": label,
                            "window_days_used": resolved,
                            "sigma": sigma,
                            "bias_sigma": bias_sigma,
                            "seed": seed,
                            "baseline": _policy_block(
                                ran=True,
                                cost_eur=baseline_cost,
                                baseline_cost_eur=baseline_cost,
                                schedule=no_battery,
                            ),
                            "day_ahead": _policy_block(
                                ran=da_cost is not None,
                                cost_eur=da_cost,
                                baseline_cost_eur=baseline_cost,
                                schedule=da_schedule,
                                telemetry=da_telemetry,
                                failure=da_failure,
                            ),
                            "rolling_horizon": _policy_block(
                                ran=rh_cost is not None,
                                cost_eur=rh_cost,
                                baseline_cost_eur=baseline_cost,
                                schedule=rh_schedule,
                                telemetry=rh_telemetry,
                                failure=rh_failure,
                            ),
                            "load_forecast": {
                                **forecast_accuracy,
                                "method": frozen_meta["method"],
                                "training_end_idx": frozen_meta["training_end_idx"],
                                "training_days": frozen_meta["training_days"],
                                "training_usable_intervals": frozen_meta[
                                    "training_usable_intervals"
                                ],
                                # Non-zero on a DST day: its extra slot offsets were never
                                # learned, so they carry the pooled mean rather than a clock.
                                "eval_slots_pooled_fallback": frozen_meta[
                                    "eval_slots_pooled_fallback"
                                ],
                                # The frozen-profile builder returns this hash; without it
                                # there is no way to confirm two arms shared a training basis.
                                "training_hash": frozen_meta["training_hash"],
                                "enters_cost": False,
                            },
                            "price_noise": rh_noise,
                        }
                    )

    # ---- provenance snapshot at end + drift detection --------------------------
    # Note: _get_code_version() shells out to git twice with no timeout; a stuck git
    # will block the runner indefinitely. Accepted for now (low probability).
    _rev_end, _dirty_end = _get_code_version()
    code_version_at_end = code_version()
    source_hashes_at_end = _compute_source_hashes()
    source_drift = sorted(
        rel
        for rel in _SOURCE_FILES
        if source_hashes_at_start.get(rel) != source_hashes_at_end.get(rel)
    )

    summary = {
        "dataset_id": loaded.dataset_id,
        "build_manifest_sha256": loaded.manifest_sha256,
        "config_path": str(config_path),
        # --- Code identity (provenance) ---
        "code_version": code_version_at_end,
        "git_head": _rev_end,
        "git_dirty": _dirty_end,
        "config_hash": config_hash(config_path),
        "config_sha256_raw_bytes": _file_sha256(config_path),
        "source_hashes": source_hashes_at_end,
        "code_version_at_start": code_version_at_start,
        "git_head_at_start": git_head_at_start,
        "git_dirty_at_start": git_dirty_at_start,
        "source_hashes_at_start": source_hashes_at_start,
        "source_drift": source_drift,
        "interval_seconds": loaded.interval_seconds,
        "test_start": test_start.isoformat().replace("+00:00", "Z"),
        "test_end": test_end.isoformat().replace("+00:00", "Z"),
        "coverage": {
            "dataset_intervals": int(total),
            "dataset_usable": int(usable.sum()),
            "dataset_usable_fraction": float(usable.mean()),
            "evaluation_intervals": int(eval_end_idx - eval_start_idx),
            "evaluation_usable": int(usable[eval_start_idx:eval_end_idx].sum()),
            "training_usable": int((train_mask & usable).sum()),
            "training_local_days": int(unique_train_days),
            "exclusion_reasons": _count(reasons),
        },
        "battery": assumptions.as_dict(),
        "terminal_value_eur_per_kwh": terminal_value,
        "terminal_value_source": (
            "mean of the usable TRAINING prices only; no evaluation price enters it"
        ),
        # The cadence is a property of the whole run matrix, not of one arm, and it is
        # what the run COST: solve_count is counted per run and summarised here so a
        # 15-minute-cadence holdout cannot be mistaken for the ~185-solve legacy run.
        "replan_cadence": {
            "replan_interval_slots": args.replan_interval_slots,
            "horizon_slots": args.horizon_slots,
            "nominal": (
                "14:00 and 20:30 local (Europe/Madrid), horizon = midnight ending D+1 "
                "(DST-dependent: ~34 h at 14:00, ~27.5 h at 20:30)"
                if args.replan_interval_slots is None
                else f"every {args.replan_interval_slots} interval(s), horizon = "
                f"{args.horizon_slots or 96} intervals "
                f"={(args.horizon_slots or 96) * loaded.interval_seconds / 3600.0:.1f} h"
            ),
            "price_information_rule": (
                "the information set is a function of the decision instant, not of the "
                "cadence: prices for local day D+1 are treated as exact from 20:30 on "
                "day D (first quarter-hour boundary after the 20:15-20:20 publication "
                "window), so a 09:00 solve estimates D+1 and a 21:00 solve does not"
            ),
            "solve_counts_observed": _int_range(
                [
                    run["rolling_horizon"].get("solve_count")
                    for run in runs
                    if run["rolling_horizon"].get("ran")
                ]
            ),
            "planning_multiplicity_mean_range": _float_range(
                [
                    run["rolling_horizon"].get("planning_multiplicity_mean")
                    for run in runs
                    if run["rolling_horizon"].get("ran")
                ]
            ),
        },
        "runs": runs,
        "notes": [
            "rolling horizon plans at 14:00 and 20:30 local (Europe/Madrid) unless "
            "--replan-interval-slots is given, in which case it re-solves every that many "
            "intervals over a --horizon-slots horizon; either way it executes only the "
            "slice before its next replan and carries the ACTUAL clipped SOC. Day-ahead "
            "plans once per local day and executes the whole day",
            "clipping totals are counted PER PLAN, and one interval is planned by every "
            "overlapping horizon while being executed once: planning_multiplicity_mean says "
            "how many times (~2.5 at the legacy cadence, ~horizon/cadence at a uniform "
            "one). charge_clipped_kwh and discharge_clipped_kwh are therefore NOT "
            "curtailment and NOT comparable between runs of different cadence",
            "both policies share one frozen load profile learned strictly before --test-start, "
            "one usable mask, and a scoring terminal value of 0.0, so their costs are directly "
            "comparable against the same zero-battery baseline",
            "price noise (sigma>0) is synthetic: future truth is used only to construct the "
            "noisy forecast. It is not an empirical accuracy figure and proves nothing about "
            "a deployable estimator; sigma=0 is the exact-price control",
            "bias_sigma>0 adds a per-local-day multiplicative bias shared by every slot of "
            "that day, drawn from an independent stream of the same (seed, local_date) hash. "
            "Unlike per-slot noise it does not average out within a day and leaves the "
            "within-day ordering intact, so it stresses the cross-day hold-versus-spend "
            "decision; bias_sigma=0 reproduces the i.i.d.-only model bit-for-bit",
            "costs are always dot(true price, actual grid import); a noisy or wrong forecast "
            "can degrade the plan but can never change the price it is scored at",
            "the final events' horizons are clipped to the evaluation end, so the last day of "
            "every run sees a shorter horizon than the interior days",
        ],
    }
    (output_dir / "summary.json").write_text(json.dumps(summary, indent=2, allow_nan=False))

    # Integrity sidecar for summary.json.  The 84 schedule files live under
    # schedules/ and finalize_run_outputs only covers direct children of run_dir
    # (it rejects path separators in filenames), so the sidecar hashes
    # summary.json alone.  The schedules are pinned indirectly through the
    # source_hashes + code_version recorded above and the build_manifest_sha256
    # that pins the dataset.
    finalize_run_outputs(output_dir, required_files=("summary.json",))

    if source_drift:
        print(
            f"WARNING: source drift detected during run — {len(source_drift)} module(s) "
            f"changed between start and end: {source_drift}. The run's measurements were "
            f"computed against mixed code versions; treat the results as suspect",
            file=sys.stderr,
            flush=True,
        )

    # ---- console summary ---------------------------------------------------------
    print()
    print(
        f"{'win':>5} {'sig':>5} {'bias':>5} {'seed':>5}  {'baseline':>10} {'day_ahead':>10} "
        f"{'rolling':>10}  {'save_DA':>9} {'save_RH':>9}  {'solves':>7} {'plan_mult':>9}"
    )
    for run in runs:
        row = (
            f"{run['window_days_requested']:>5} {run['sigma']:>5} "
            f"{run['bias_sigma']:>5} {run['seed']:>5}"
        )
        row += f"  {_fmt(run['baseline']['cost_eur']):>10} {_fmt(run['day_ahead']['cost_eur']):>10}"
        row += f" {_fmt(run['rolling_horizon']['cost_eur']):>10}"
        row += f"  {_fmt(run['day_ahead']['saving_eur']):>9}"
        row += f" {_fmt(run['rolling_horizon']['saving_eur']):>9}"
        # Solved, and how many times each interval was planned: the two numbers that say
        # what this run cost and why its clipping kWh is not comparable with another
        # cadence's.
        rh = run["rolling_horizon"]
        solves = rh.get("solve_count")
        plan_mult = rh.get("planning_multiplicity_mean")
        row += f"  {'-' if solves is None else solves:>7}"
        row += f" {'-' if plan_mult is None else f'{plan_mult:.2f}':>9}"
        print(row)
    print(f"\nwrote {output_dir / 'summary.json'} and {len(runs)} schedules")
    return 0


def _flow_columns(schedule: Any) -> dict[str, list[float]]:
    """Per-interval decision columns for the schedule artifact."""
    return {
        "charge_kwh": [float(v) for v in np.asarray(schedule.charge_kwh)],
        "discharge_kwh": [float(v) for v in np.asarray(schedule.discharge_kwh)],
        "soc_kwh": [float(v) for v in np.asarray(schedule.soc_kwh)],
        "grid_kwh": [float(v) for v in np.asarray(schedule.grid_kwh)],
    }


def _fmt(value: float | None) -> str:
    return "n/a" if value is None else f"{value:.3f}"


def _count(reasons: list[str]) -> dict[str, int]:
    out: dict[str, int] = {}
    for reason in reasons:
        out[reason] = out.get(reason, 0) + 1
    return dict(sorted(out.items(), key=lambda kv: (-kv[1], kv[0])))


def _int_range(values: list[Any]) -> dict[str, int] | None:
    """min/max over the observed solve counts, or None when nothing ran.

    A range rather than a single number because the last events of a window have
    clipped horizons, so even a fixed cadence does not solve an identical shape
    everywhere.
    """
    present = [int(v) for v in values if isinstance(v, (int, float))]
    if not present:
        return None
    return {"min": min(present), "max": max(present)}


def _float_range(values: list[Any]) -> dict[str, float] | None:
    """min/max of an observed telemetry figure, rounded to a readable precision."""
    present = [float(v) for v in values if isinstance(v, (int, float))]
    if not present:
        return None
    return {"min": round(min(present), 4), "max": round(max(present), 4)}


def _inside_git_worktree(path: Path) -> bool:
    """True if *path* is under a directory containing a ``.git`` entry."""
    probe = path
    while True:
        if (probe / ".git").exists():
            return True
        if probe.parent == probe:
            return False
        probe = probe.parent


if __name__ == "__main__":
    try:
        sys.exit(main())
    except SystemExit:
        raise
    except KeyboardInterrupt:  # pragma: no cover - interactive only
        print("interrupted", file=sys.stderr)
        sys.exit(130)
    except Exception:
        # Programmer errors and unexpected core failures must be LOUD: a traceback on
        # stderr and a non-zero status, never a partial summary claiming success.
        traceback.print_exc()
        sys.exit(1)
