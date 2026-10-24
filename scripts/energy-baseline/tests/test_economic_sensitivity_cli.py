"""CLI tests for ``hack/economic_sensitivity.py`` — synthetic datasets only.

The runner is a driver, so these tests drive it the way an operator would: parse,
refuse, sweep, write.  Everything runs on small synthetic datasets and checks the
contract the driver promises:

* window bounds are explicit and honoured, never defaulted to "the end of the data";
* the battery comes from the config (or a validated override), not a local constant;
* usability comes from the canonical mask, so a missing price cannot be swept on;
* each grid pair is its own solve, and the resulting plan is recorded as evidence;
* the artifact holds no wall-clock value, so a repeat run is byte-identical.

Every monetary figure here is a synthetic fixture value, not a real rate, and
nothing in these tests reads a real dataset or writes inside the repository.
"""

from __future__ import annotations

import hashlib
import importlib.util
import json
import re
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
import yaml

REPO_ROOT = Path(__file__).resolve().parents[1]
RUNNER = REPO_ROOT / "hack" / "economic_sensitivity.py"
ARTIFACT_NAME = "sensitivity_sweep.json"
MANIFEST_NAME = "sensitivity_outputs_manifest.json"
MADRID = ZoneInfo("Europe/Madrid")

_INTERVAL_SECONDS = 900
SLOTS = 96  # intervals per day on the 15-minute grid
DATASET_START = datetime(2025, 1, 1, 0, 0, tzinfo=UTC)

MAIN_DATASET_ID = "econ-cli-main"
QUALITY_DATASET_ID = "econ-cli-quality"
DEAD_DATASET_ID = "econ-cli-dead"

# The quality dataset's scored window: days 2..7 of an 8-day table (Jan 3 .. Jan 9).
# Day 2 null prices, day 3 a never-admitted price quality, day 4 reconstructed
# values, day 5 an estimated energy quality, days 6 and 7 clean.
_QUALITY_TRAIN_END = "2025-01-03T00:00:00Z"
_QUALITY_WINDOW = [
    "--window",
    "evaluation",
    "--eval-start",
    "2025-01-03T00:00:00Z",
    "--eval-end",
    "2025-01-09T00:00:00Z",
]
# A window whose only data is day 4 (reconstructed): usable solely with the flag.
_RECON_ONLY = [
    "--window",
    "evaluation",
    "--eval-start",
    "2025-01-05T00:00:00Z",
    "--eval-end",
    "2025-01-06T00:00:00Z",
]


# ---------------------------------------------------------------------------
# Synthetic dataset construction (mirrors tests/test_benchmark.py)
# ---------------------------------------------------------------------------


def _write_tariff(path: Path, *, initial_soc: float = 0.20) -> Path:
    """A tariff.yaml whose battery block satisfies ``resolve_battery_assumptions``."""
    data = {
        "version": 1,
        "taxes": [
            {"valid_from": "2024-01-01", "iee_pct": None, "iva_pct": None, "source": "synthetic"}
        ],
        "tariff_periods": {"timezone": "Europe/Madrid"},
        "battery": {
            "capacity_kwh": 5.12,
            "power_kw": 2.5,
            "round_trip_efficiency": 0.80,
            "soc_window_min": 0.05,
            "soc_window_max": 0.95,
            # Deliberately NOT 0.5: a driver that hardcoded its initial SOC would
            # disagree with this number, which is exactly what these tests check.
            "initial_soc_frac": initial_soc,
            "import_limit_kw": 5.0,
        },
    }
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(yaml.safe_dump(data, sort_keys=False))
    return path


def _daily_profile(day: date) -> tuple[np.ndarray, np.ndarray]:
    """Deterministic daily price/load shape: cheap night, expensive evening."""
    prices = np.zeros(SLOTS)
    load = np.zeros(SLOTS)
    for i in range(SLOTS):
        hour = (i * 15) // 60
        wobble = 0.02 * ((day.toordinal() % 7) - 3) / 3.0
        if hour < 6:
            prices[i], load[i] = 0.04 + wobble, 0.030
        elif hour < 10:
            prices[i], load[i] = 0.19 + wobble, 0.120
        elif hour < 14:
            prices[i], load[i] = 0.11 + wobble, 0.080
        elif hour < 18:
            prices[i], load[i] = 0.07 + wobble, 0.060
        elif hour < 22:
            prices[i], load[i] = 0.24 + wobble, 0.150
        else:
            prices[i], load[i] = 0.05 + wobble, 0.045
    return prices, load


def _analysis_table(
    *,
    days: int,
    dataset_id: str,
    null_price_days: tuple[int, ...] = (),
    bad_price_quality_days: tuple[int, ...] = (),
    reconstructed_days: tuple[int, ...] = (),
    estimated_energy_days: tuple[int, ...] = (),
) -> pa.Table:
    """An ``ANALYSIS_INTERVAL`` table of *days* days from :data:`DATASET_START`.

    A bad day keeps ``energy_quality=measured`` unless named, so a price-side
    defect shows up as a price-side rejection — the distinction an energy-only
    mask cannot make.
    """
    starts: list[datetime] = []
    ends: list[datetime] = []
    local_days: list[date] = []
    gross: list[float] = []
    prices: list[float | None] = []
    energy_q: list[str] = []
    price_q: list[str] = []

    cursor = DATASET_START
    for d in range(days):
        day_prices, day_load = _daily_profile(cursor.astimezone(MADRID).date())
        for i in range(SLOTS):
            local = cursor.astimezone(MADRID)
            starts.append(cursor)
            ends.append(cursor + timedelta(seconds=_INTERVAL_SECONDS))
            local_days.append(local.date())
            gross.append(float(day_load[i]))
            price: float | None = float(day_prices[i])
            eq = "measured"
            pqual = "measured"
            if d in null_price_days:
                price = None
            if d in bad_price_quality_days:
                pqual = "interpolated"  # present and finite, but never admitted
            if d in reconstructed_days:
                eq, pqual = "gap_partial", "forward_filled"
            if d in estimated_energy_days:
                eq = "estimated"
            prices.append(price)
            energy_q.append(eq)
            price_q.append(pqual)
            cursor = cursor + timedelta(seconds=_INTERVAL_SECONDS)

    n = len(starts)
    schema = _analysis_schema()
    return pa.table(
        {
            "interval_start": pa.array(starts, pa.timestamp("us", tz="UTC")),
            "interval_end": pa.array(ends, pa.timestamp("us", tz="UTC")),
            "tz_local": pa.array(["Europe/Madrid"] * n, pa.string()),
            "tariff_period": pa.array([3] * n, pa.int8()),
            "local_day": pa.array(local_days, pa.date32()),
            "dst_transition": pa.array([None] * n, pa.string()),
            "raw_grid_kwh": pa.array(gross, pa.float64()),
            "gross_kwh": pa.array(gross, pa.float64()),
            "battery_absent": pa.array([True] * n, pa.bool_()),
            "energy_quality": pa.array(energy_q, pa.string()),
            "allocation_quality": pa.array(["exact"] * n, pa.string()),
            "sample_count": pa.array([4] * n, pa.int32()),
            "gap_seconds": pa.array([0.0] * n, pa.float64()),
            "price_eur_per_kwh": pa.array(prices, pa.float64()),
            "price_quality": pa.array(price_q, pa.string()),
            "dataset_id": pa.array([dataset_id] * n, pa.string()),
        },
        schema=schema,
    )


def _analysis_schema() -> pa.Schema:
    from energy_baseline.schemas import ANALYSIS_INTERVAL

    return ANALYSIS_INTERVAL


def _write_dataset(root: Path, table: pa.Table, *, config_path: Path, dataset_id: str) -> Path:
    """Write ``<root>/analysis/v<schema>/<dataset_id>`` with a valid build manifest."""
    from energy_baseline.manifest import config_hash
    from energy_baseline.schemas import SCHEMA_VERSION

    dataset_dir = root / "analysis" / f"v{SCHEMA_VERSION}" / dataset_id
    parts_dir = dataset_dir / "parts"
    parts_dir.mkdir(parents=True, exist_ok=True)
    part_path = parts_dir / f"analysis-{dataset_id}-00000000.parquet"
    pq.write_table(table, part_path)

    manifest: dict[str, Any] = {
        "manifest_version": 1,
        "dataset_id": dataset_id,
        "schema_version": SCHEMA_VERSION,
        "config_hash": config_hash(config_path),
        "code_version": "econ-cli-test",
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


# ---------------------------------------------------------------------------
# Driving the runner
# ---------------------------------------------------------------------------


@pytest.fixture(scope="module")
def runner():
    """The driver, loaded as a module: it is a script, not an installed entry point."""
    spec = importlib.util.spec_from_file_location("econ_cli_runner", RUNNER)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module


@pytest.fixture(scope="module")
def main_env(tmp_path_factory) -> dict[str, Any]:
    """A clean 12-day dataset plus the tariff its manifest pins."""
    root = tmp_path_factory.mktemp("econ-cli")
    tariff = _write_tariff(root / "config" / "tariff.yaml")
    dataset = _write_dataset(
        root / "data",
        _analysis_table(days=12, dataset_id=MAIN_DATASET_ID),
        config_path=tariff,
        dataset_id=MAIN_DATASET_ID,
    )
    return {
        "root": root,
        "tariff": tariff,
        "dataset": dataset,
        "train_end": "2025-01-03T00:00:00Z",
        "n": 0,
    }


@pytest.fixture(scope="module")
def quality_env(tmp_path_factory) -> dict[str, Any]:
    """A dataset with four distinct kinds of bad day and two clean days."""
    root = tmp_path_factory.mktemp("econ-cli-quality")
    tariff = _write_tariff(root / "config" / "tariff.yaml")
    dataset = _write_dataset(
        root / "data",
        _analysis_table(
            days=8,
            dataset_id=QUALITY_DATASET_ID,
            null_price_days=(2,),
            bad_price_quality_days=(3,),
            reconstructed_days=(4,),
            estimated_energy_days=(5,),
        ),
        config_path=tariff,
        dataset_id=QUALITY_DATASET_ID,
    )
    return {
        "root": root,
        "tariff": tariff,
        "dataset": dataset,
        "train_end": _QUALITY_TRAIN_END,
        "n": 0,
    }


def _fresh_out(env: dict[str, Any], label: str = "run") -> Path:
    """A never-used output directory — the driver refuses to mix runs."""
    env["n"] += 1
    return env["root"] / f"out-{label}-{env['n']}"


def _grid(*, planning: str = "0.0", throughput: str = "0.0") -> list[str]:
    return ["--planning-terminal-values", planning, "--throughput-costs", throughput]


def _args(env: dict[str, Any], out: Path, *extra: str) -> list[str]:
    return [
        "--dataset",
        str(env["dataset"]),
        "--config",
        str(env["tariff"]),
        "--output-dir",
        str(out),
        "--train-end",
        env["train_end"],
        "--time-limit-s",
        "5",
        *extra,
    ]


def _sweep(runner, env: dict[str, Any], out: Path, *extra: str) -> dict[str, Any]:
    """Run one sweep, require a clean exit, and return the parsed artifact."""
    code = runner.main(_args(env, out, *extra))
    assert code == 0, f"expected a clean sweep, got exit status {code}"
    return _read_artifact(out)


def _read_artifact(out: Path) -> dict[str, Any]:
    return json.loads((out / ARTIFACT_NAME).read_text())


def _refusal(excinfo: pytest.ExceptionInfo[SystemExit]) -> str:
    """The message a refusal exits with (a string code, never a silent zero)."""
    code = excinfo.value.code
    assert isinstance(code, str), f"expected a refusal message, got exit code {code!r}"
    assert code.startswith("error:"), code
    return code


# ---------------------------------------------------------------------------
# Explicit --eval-end and window bounds
# ---------------------------------------------------------------------------


class TestEvaluationWindowBounds:
    def test_evaluation_window_requires_both_bounds(self, runner, main_env) -> None:
        for partial, missing in (
            (["--eval-start", "2025-01-08T00:00:00Z"], "--eval-end"),
            (["--eval-end", "2025-01-09T00:00:00Z"], "--eval-start"),
        ):
            with pytest.raises(SystemExit) as excinfo:
                runner.main(
                    _args(
                        main_env, _fresh_out(main_env), *_grid(), "--window", "evaluation", *partial
                    )
                )
            message = _refusal(excinfo)
            assert "--window evaluation requires" in message
            assert missing in message

    def test_declared_bounds_are_honoured_exactly(self, runner, main_env) -> None:
        """The scored window is the declared one: 1 day = 96 intervals, not "the rest"."""
        artifact = _sweep(
            runner,
            main_env,
            _fresh_out(main_env),
            *_grid(),
            "--window",
            "evaluation",
            "--eval-start",
            "2025-01-08T00:00:00Z",
            "--eval-end",
            "2025-01-09T00:00:00Z",
        )
        window = artifact["temporal"]["scored_window"]
        assert window["label"] == "evaluation"
        assert window["start"] == "2025-01-08T00:00:00Z"
        assert window["end"] == "2025-01-09T00:00:00Z"
        assert artifact["quality"]["window_interval_count"] == SLOTS
        assert artifact["quality"]["usable_interval_count"] == SLOTS
        assert artifact["quality"]["scored_window_end_exclusive"] == "2025-01-09T00:00:00Z"
        # The end bound is honoured rather than open-ended: the data runs to Jan 13.
        assert artifact["lineage"]["dataset_coverage"]["end"] == "2025-01-13T00:00:00Z"
        assert artifact["quality"]["last_window_interval_start"] == "2025-01-08T23:45:00Z"

    def test_eval_end_beyond_the_dataset_is_refused(self, runner, main_env) -> None:
        with pytest.raises(SystemExit) as excinfo:
            _sweep(
                runner,
                main_env,
                _fresh_out(main_env),
                *_grid(),
                "--window",
                "evaluation",
                "--eval-start",
                "2025-01-12T00:00:00Z",
                "--eval-end",
                "2025-02-01T00:00:00Z",
            )
        message = _refusal(excinfo)
        assert "not covered by the dataset" in message
        assert "refusing rather than truncating" in message

    def test_eval_start_before_the_cut_is_refused(self, runner, main_env) -> None:
        """Out-of-sample is a rule, not a suggestion: the windows must not overlap."""
        with pytest.raises(SystemExit) as excinfo:
            _sweep(
                runner,
                main_env,
                _fresh_out(main_env),
                *_grid(),
                "--window",
                "evaluation",
                "--eval-start",
                "2025-01-02T00:00:00Z",
                "--eval-end",
                "2025-01-04T00:00:00Z",
            )
        assert "precedes train_end" in _refusal(excinfo)

    def test_eval_start_exactly_at_the_cut_is_allowed(self, runner, main_env) -> None:
        """``--eval-start == --train-end`` is exactly out-of-sample, so it must run."""
        artifact = _sweep(
            runner,
            main_env,
            _fresh_out(main_env),
            *_grid(),
            "--window",
            "evaluation",
            "--eval-start",
            main_env["train_end"],
            "--eval-end",
            "2025-01-05T00:00:00Z",
        )
        assert artifact["temporal"]["scored_window"]["start"] == main_env["train_end"]

    def test_reversed_eval_bounds_are_refused(self, runner, main_env) -> None:
        with pytest.raises(SystemExit) as excinfo:
            _sweep(
                runner,
                main_env,
                _fresh_out(main_env),
                *_grid(),
                "--window",
                "evaluation",
                "--eval-start",
                "2025-01-09T00:00:00Z",
                "--eval-end",
                "2025-01-09T00:00:00Z",
            )
        assert "evaluation window is empty" in _refusal(excinfo)

    def test_eval_bounds_with_training_window_are_refused(self, runner, main_env) -> None:
        """Bounds that would be silently ignored are an error instead."""
        with pytest.raises(SystemExit) as excinfo:
            _sweep(
                runner,
                main_env,
                _fresh_out(main_env),
                *_grid(),
                "--eval-end",
                "2025-01-09T00:00:00Z",
            )
        message = _refusal(excinfo)
        assert "--eval-end" in message
        assert "silently" in message

    def test_training_window_is_the_default_and_is_labelled(self, runner, main_env) -> None:
        artifact = _sweep(runner, main_env, _fresh_out(main_env), *_grid())
        assert artifact["temporal"]["window"] == "training"
        assert artifact["temporal"]["scored_window"]["label"] == "training"
        assert artifact["temporal"]["scored_window"]["end"] == main_env["train_end"]
        # The lower bound of the default window is recorded, not assumed.
        assert artifact["temporal"]["training_window"]["start_source"].startswith("dataset:")
        assert artifact["temporal"]["training_window"]["start"] == "2025-01-01T00:00:00Z"

    def test_explicit_train_start_is_recorded(self, runner, main_env) -> None:
        artifact = _sweep(
            runner,
            main_env,
            _fresh_out(main_env),
            *_grid(),
            "--train-start",
            "2025-01-02T00:00:00Z",
        )
        assert artifact["temporal"]["training_window"]["start"] == "2025-01-02T00:00:00Z"
        assert artifact["temporal"]["training_window"]["start_source"] == "argument: --train-start"
        assert artifact["quality"]["window_interval_count"] == SLOTS

    def test_train_start_before_the_dataset_is_refused(self, runner, main_env) -> None:
        with pytest.raises(SystemExit) as excinfo:
            _sweep(
                runner,
                main_env,
                _fresh_out(main_env),
                *_grid(),
                "--train-start",
                "2024-12-01T00:00:00Z",
            )
        message = _refusal(excinfo)
        assert "precedes the dataset" in message
        assert "clamping" in message

    def test_naive_window_bound_is_refused(self, runner, main_env) -> None:
        with pytest.raises(SystemExit) as excinfo:
            _sweep(
                runner,
                main_env,
                _fresh_out(main_env),
                *_grid(),
                "--window",
                "evaluation",
                "--eval-start",
                "2025-01-08 00:00:00",
                "--eval-end",
                "2025-01-09T00:00:00Z",
            )
        assert "naive" in _refusal(excinfo)

    def test_future_train_end_is_refused(self, runner, main_env) -> None:
        with pytest.raises(SystemExit) as excinfo:
            _sweep(
                runner,
                main_env,
                _fresh_out(main_env),
                *_grid(),
                "--train-end",
                "2099-01-01T00:00:00Z",
            )
        assert "has not happened yet" in _refusal(excinfo)


# ---------------------------------------------------------------------------
# Initial SOC: the config by default, a validated override when asked
# ---------------------------------------------------------------------------


class TestInitialSoc:
    def test_default_is_the_config_not_a_local_constant(self, runner, main_env) -> None:
        artifact = _sweep(runner, main_env, _fresh_out(main_env), *_grid())
        spec = artifact["battery"]["spec"]
        # tariff.yaml says 0.20; a hardcoded 0.5 would fail right here.
        assert spec["initial_soc_frac"] == pytest.approx(0.20)
        assert artifact["battery"]["assumptions"]["initial_soc_frac"] == pytest.approx(0.20)
        assert artifact["battery"]["assumptions"]["initial_soc_source"].startswith("config:")
        for row in artifact["results"]:
            assert row["initial_soc_kwh"] == pytest.approx(0.20 * spec["capacity_kwh"])

    def test_explicit_override_is_honoured_in_the_solve(self, runner, main_env) -> None:
        artifact = _sweep(
            runner, main_env, _fresh_out(main_env), *_grid(), "--initial-soc-frac", "0.60"
        )
        spec = artifact["battery"]["spec"]
        assert spec["initial_soc_frac"] == pytest.approx(0.60)
        assert artifact["battery"]["assumptions"]["initial_soc_source"].startswith("argument:")
        for row in artifact["results"]:
            # The plan really starts there, so the override reached the numeric core.
            assert row["initial_soc_kwh"] == pytest.approx(0.60 * spec["capacity_kwh"])
            assert row["action"]["soc_kwh"][0] == pytest.approx(0.60 * spec["capacity_kwh"])

    def test_override_outside_the_configured_window_is_refused(self, runner, main_env) -> None:
        with pytest.raises(SystemExit) as excinfo:
            _sweep(runner, main_env, _fresh_out(main_env), *_grid(), "--initial-soc-frac", "0.99")
        assert "outside the configured SOC window" in _refusal(excinfo)

    def test_every_spec_field_is_the_configs(self, runner, main_env) -> None:
        """No BatterySpec field is invented by the driver."""
        artifact = _sweep(runner, main_env, _fresh_out(main_env), *_grid())
        spec = artifact["battery"]["spec"]
        assumptions = artifact["battery"]["assumptions"]
        assert spec["capacity_kwh"] == pytest.approx(5.12)
        assert spec["power_kw"] == pytest.approx(2.5)
        assert spec["soc_min_frac"] == pytest.approx(0.05)
        assert spec["soc_max_frac"] == pytest.approx(0.95)
        # sqrt(RTE) on both legs, labelled as the symmetric assumption that it is.
        assert spec["charge_efficiency"] == pytest.approx(np.sqrt(0.80))
        assert spec["discharge_efficiency"] == pytest.approx(np.sqrt(0.80))
        assert "sqrt(RTE)" in assumptions["efficiency_mapping"]
        # The terminal condition is the solver's terminal-mode question, not the spec's.
        assert spec["terminal_soc_frac"] is None
        assert assumptions["import_limit_kw"] == pytest.approx(5.0)


# ---------------------------------------------------------------------------
# The canonical usable mask, not an energy-only one
# ---------------------------------------------------------------------------


@pytest.fixture(scope="module")
def quality_strict(runner, quality_env) -> dict[str, Any]:
    """The four-bad-day window, measured-only mask (the default)."""
    out = _fresh_out(quality_env, "strict")
    return _sweep(runner, quality_env, out, *_grid(), *_QUALITY_WINDOW)


@pytest.fixture(scope="module")
def quality_loose(runner, quality_env) -> dict[str, Any]:
    """The same window with reconstructed qualities explicitly admitted."""
    out = _fresh_out(quality_env, "loose")
    return _sweep(
        runner, quality_env, out, *_grid(), *_QUALITY_WINDOW, "--allow-reconstructed-quality"
    )


class TestUsableMask:
    def test_missing_prices_are_excluded_and_reported(self, quality_strict) -> None:
        quality = quality_strict["quality"]
        reasons = quality["exclusion_reason_counts"]
        assert reasons.get("price=null") == SLOTS
        assert reasons.get("price_quality=interpolated") == SLOTS
        assert reasons.get("energy_quality=gap_partial") == SLOTS
        assert reasons.get("energy_quality=estimated") == SLOTS
        assert quality["masked_price_substitutions"]["null_price_intervals"] == SLOTS
        assert quality["window_interval_count"] == 6 * SLOTS
        assert quality["usable_interval_count"] == 2 * SLOTS
        assert quality["unusable_interval_count"] == 4 * SLOTS
        # A masked interval is never *usable*, and the driver says so.
        assert "never counted as free energy" in quality["masked_price_substitutions"]["note"]

    def test_the_mask_is_named_and_digested(self, quality_strict) -> None:
        quality = quality_strict["quality"]
        assert "usable_mask" in quality["usable_mask_source"]
        assert "not an energy-only mask" in quality["usable_mask_source"]
        assert re.fullmatch(r"[0-9a-f]{64}", quality["usable_mask_sha256"])

    def test_arms_only_see_usable_intervals(self, quality_strict) -> None:
        row = quality_strict["results"][0]
        assert row["window_interval_count"] == 6 * SLOTS
        assert row["usable_interval_count"] == 2 * SLOTS
        assert row["usable_interval_count"] < row["window_interval_count"]

    def test_reconstructed_quality_is_off_by_default(self, quality_strict, quality_loose) -> None:
        """``gap_partial`` / ``forward_filled`` need the explicit flag, as in the benchmark."""
        assert quality_strict["quality"]["allow_reconstructed_quality"] is False
        assert quality_loose["quality"]["allow_reconstructed_quality"] is True
        assert (
            quality_loose["quality"]["usable_interval_count"]
            == quality_strict["quality"]["usable_interval_count"] + SLOTS
        )
        assert (
            quality_loose["quality"]["usable_mask_sha256"]
            != quality_strict["quality"]["usable_mask_sha256"]
        )

    def test_reconstructed_only_window_is_refused_then_admitted(self, runner, quality_env) -> None:
        """Day 4 alone: no cost basis by default, a real one once the flag is given."""
        with pytest.raises(SystemExit) as excinfo:
            runner.main(_args(quality_env, _fresh_out(quality_env, "dead"), *_grid(), *_RECON_ONLY))
        message = _refusal(excinfo)
        assert "unusable under the benchmark" in message
        assert "energy_quality=gap_partial" in message

        out = _fresh_out(quality_env, "loose-only")
        assert (
            runner.main(
                _args(quality_env, out, *_grid(), *_RECON_ONLY, "--allow-reconstructed-quality")
            )
            == 0
        )
        artifact = _read_artifact(out)
        assert artifact["quality"]["usable_interval_count"] == SLOTS

    def test_all_unusable_window_is_refused(self, runner, main_env) -> None:
        dead = _write_dataset(
            main_env["root"] / "data-dead",
            _analysis_table(days=3, dataset_id=DEAD_DATASET_ID, null_price_days=(0, 1, 2)),
            config_path=main_env["tariff"],
            dataset_id=DEAD_DATASET_ID,
        )
        with pytest.raises(SystemExit) as excinfo:
            runner.main(
                [
                    "--dataset",
                    str(dead),
                    "--config",
                    str(main_env["tariff"]),
                    "--output-dir",
                    str(_fresh_out(main_env, "allbad")),
                    "--train-end",
                    "2025-01-03T00:00:00Z",
                    *_grid(),
                ]
            )
        message = _refusal(excinfo)
        assert "unusable under the benchmark" in message
        assert "price=null" in message


# ---------------------------------------------------------------------------
# No wall-clock in the artifacts
# ---------------------------------------------------------------------------

_FORBIDDEN_KEYS = {
    "generated_at",
    "created_at",
    "timestamp",
    "now",
    "date",
    "elapsed_s",
    "duration_s",
    "wall_time_s",
    "runtime_s",
    "execution_time_s",
    "solve_time_s",
}


def _walk_keys(node: Any):
    if isinstance(node, dict):
        for key, value in node.items():
            yield key
            yield from _walk_keys(value)
    elif isinstance(node, list):
        for value in node:
            yield from _walk_keys(value)


class TestNoWallClock:
    def test_two_runs_are_byte_identical(self, runner, main_env) -> None:
        """Repeating a sweep must reproduce the artifact digest exactly."""
        grid = _grid(planning="0.0,0.20", throughput="0.0,0.05")
        first, second = _fresh_out(main_env, "rep1"), _fresh_out(main_env, "rep2")
        assert runner.main(_args(main_env, first, *grid)) == 0
        assert runner.main(_args(main_env, second, *grid)) == 0
        a = (first / ARTIFACT_NAME).read_bytes()
        b = (second / ARTIFACT_NAME).read_bytes()
        assert hashlib.sha256(a).hexdigest() == hashlib.sha256(b).hexdigest()
        assert (first / MANIFEST_NAME).read_bytes() == (second / MANIFEST_NAME).read_bytes()
        # And the per-arm action digests are the reproducible part of the record.
        artifact_a, artifact_b = _read_artifact(first), _read_artifact(second)
        assert [r["action"]["action_sha256"] for r in artifact_a["results"]] == [
            r["action"]["action_sha256"] for r in artifact_b["results"]
        ]

    def test_no_measured_duration_key_anywhere(self, runner, main_env) -> None:
        artifact = _sweep(runner, main_env, _fresh_out(main_env), *_grid())
        assert artifact["determinism"]["wall_clock_in_artifact"] is False
        offenders = _FORBIDDEN_KEYS & set(_walk_keys(artifact))
        assert offenders == set(), f"wall-clock-looking keys: {sorted(offenders)}"
        # The solver budget is a *declared* limit, echoed exactly, never a measurement.
        for row in artifact["results"]:
            assert row["declared_time_limit_s"] == pytest.approx(5.0)

    def test_driver_source_never_reads_the_clock(self) -> None:
        """A static guard: the driver must not call now() / time() / monotonic()."""
        source = RUNNER.read_text()
        for forbidden in (
            "datetime.now(",
            "datetime.utcnow(",
            "time.time(",
            "time.monotonic(",
            "perf_counter(",
        ):
            assert forbidden not in source, f"{forbidden} appeared in {RUNNER}"
        assert "import time" not in source

    def test_only_absolute_declared_instants_are_written(self, runner, main_env) -> None:
        """Every timestamp is a declared bound, so none can drift between runs."""
        artifact = _sweep(runner, main_env, _fresh_out(main_env), *_grid())
        temporal = artifact["temporal"]
        assert temporal["scored_window"]["start"] == "2025-01-01T00:00:00Z"
        assert temporal["scored_window"]["end"] == "2025-01-03T00:00:00Z"
        for stamp in _all_timestamps(temporal):
            assert stamp.endswith("Z") and stamp != "unknown"


def _all_timestamps(node: Any):
    if isinstance(node, dict):
        for value in node.values():
            yield from _all_timestamps(value)
    elif isinstance(node, list):
        for value in node:
            yield from _all_timestamps(value)
    elif isinstance(node, str) and re.fullmatch(r"\d{4}-\d{2}-\d{2}T.*", node):
        yield node


# ---------------------------------------------------------------------------
# Each pair planned independently, with executed-action evidence
# ---------------------------------------------------------------------------


class TestIndependentPlanning:
    def test_every_pair_is_its_own_solve(self, runner, main_env, monkeypatch) -> None:
        calls: list[dict[str, Any]] = []
        real = runner.optimize_with_throughput

        def spy(spec, horizon, throughput_cost, **kwargs):
            calls.append({"throughput_cost": float(throughput_cost), **kwargs})
            return real(spec, horizon, throughput_cost, **kwargs)

        monkeypatch.setattr(runner, "optimize_with_throughput", spy)
        artifact = _sweep(
            runner,
            main_env,
            _fresh_out(main_env),
            *_grid(planning="0.0,0.20", throughput="0.0,0.05"),
        )
        assert len(calls) == 4, "one solver call per (planning, throughput) pair"
        # Each call carries its own pair into the objective — nothing is reused or folded.
        assert sorted({c["throughput_cost"] for c in calls}) == [0.0, 0.05]
        assert sorted({float(c["terminal_value_eur_per_kwh"]) for c in calls}) == [0.0, 0.20]
        assert {c["terminal_mode"] for c in calls} == {"free"}
        grid = artifact["grid"]
        assert grid["pairs_requested"] == 4
        assert grid["pairs_solved"] == 4
        assert grid["pairs_failed"] == 0
        assert grid["distinct_parameter_pairs"] == 4

    def test_the_aggregate_only_sweep_is_not_the_driver_path(self, runner, main_env) -> None:
        """``sweep_sensitivity`` returns scalars only; the driver keeps the plan instead."""
        assert not hasattr(runner, "sweep_sensitivity"), (
            "importing the aggregate-only sweep would discard the action arrays"
        )
        artifact = _sweep(runner, main_env, _fresh_out(main_env), *_grid())
        row = artifact["results"][0]
        assert len(row["action"]["charge_kwh"]) == row["window_interval_count"]
        assert len(row["action"]["discharge_kwh"]) == row["window_interval_count"]
        assert len(row["action"]["soc_kwh"]) == row["window_interval_count"]

    def test_distinct_planning_values_give_distinct_plans(self, runner, main_env) -> None:
        artifact = _sweep(
            runner, main_env, _fresh_out(main_env), *_grid(planning="0.0,0.30", throughput="0.0")
        )
        rows = {r["planning_terminal_value_eur_per_kwh"]: r for r in artifact["results"]}
        assert len({r["action"]["action_sha256"] for r in artifact["results"]}) == 2
        assert artifact["grid"]["parameter_pairs_with_several_action_digests"] == []
        # Valuing the leftover inventory more highly is supposed to leave more behind.
        assert rows[0.30]["end_soc_kwh"] >= rows[0.0]["end_soc_kwh"]

    def test_repeated_parameter_reproduces_the_same_digest(self, runner, main_env) -> None:
        artifact = _sweep(
            runner, main_env, _fresh_out(main_env), *_grid(planning="0.0", throughput="0.0,0.0")
        )
        assert len(artifact["results"]) == 2
        assert len({r["action"]["action_sha256"] for r in artifact["results"]}) == 1
        assert artifact["grid"]["parameter_pairs_with_several_action_digests"] == []
        assert artifact["grid"]["distinct_parameter_pairs"] == 1


@pytest.fixture(scope="module")
def evidence_artifact(runner, main_env) -> dict[str, Any]:
    """One sweep whose arms are inspected as executed-action evidence."""
    return _sweep(runner, main_env, _fresh_out(main_env, "evidence"), *_grid())


class TestActionEvidence:
    def test_first_nonidle_is_a_real_action_and_the_earliest_one(self, evidence_artifact) -> None:
        row = evidence_artifact["results"][0]
        action = row["action"]
        index = action["first_nonidle_index"]
        assert index is not None, "this price shape must move energy"
        assert action["battery_planned_nonidle"] is True
        charge = np.array(action["charge_kwh"])
        discharge = np.array(action["discharge_kwh"])
        assert abs(charge[index]) + abs(discharge[index]) > 0.0
        if index:
            assert np.all(charge[:index] == 0.0)
            assert np.all(discharge[:index] == 0.0)
        assert action["nonidle_interval_count"] == int(
            np.count_nonzero((np.abs(charge) > 1e-12) | (np.abs(discharge) > 1e-12))
        )
        assert action["nonidle_interval_count"] + action["idle_interval_count"] == charge.size

    def test_first_nonidle_is_not_merely_the_first_interval(self, evidence_artifact) -> None:
        row = evidence_artifact["results"][0]
        action = row["action"]
        window_start = datetime.fromisoformat(
            evidence_artifact["temporal"]["scored_window"]["start"].replace("Z", "+00:00")
        )
        expected = window_start + timedelta(
            seconds=action["first_nonidle_index"] * _INTERVAL_SECONDS
        )
        expected_z = expected.astimezone(UTC).isoformat().replace("+00:00", "Z")
        assert action["first_nonidle_interval_start"] == expected_z
        # The window's own first interval is recorded elsewhere and labelled as such.
        assert (
            evidence_artifact["quality"]["first_window_interval_start"]
            == evidence_artifact["temporal"]["scored_window"]["start"]
        )
        assert action["note"].startswith("first_nonidle_index")

    def test_digests_are_sha256_and_distinct_per_array(self, evidence_artifact) -> None:
        action = evidence_artifact["results"][0]["action"]
        for key in (
            "action_sha256",
            "charge_sha256",
            "discharge_sha256",
            "soc_sha256",
            "nonidle_indices_sha256",
        ):
            assert re.fullmatch(r"[0-9a-f]{64}", action[key]), key
        per_array = {action["charge_sha256"], action["discharge_sha256"], action["soc_sha256"]}
        assert len(per_array) == 3, "the three arrays must not collapse to one digest"

    def test_omitting_arrays_keeps_the_digest_and_the_evidence(self, runner, main_env) -> None:
        artifact = _sweep(runner, main_env, _fresh_out(main_env), *_grid(), "--omit-plan-arrays")
        action = artifact["results"][0]["action"]
        assert action["arrays_omitted"] is True
        assert "charge_kwh" not in action
        assert re.fullmatch(r"[0-9a-f]{64}", action["action_sha256"])
        assert action["first_nonidle_index"] is not None

    def test_an_idle_plan_says_so_instead_of_looking_broken(self, runner, main_env) -> None:
        """A crushing throughput cost legitimately yields a plan that never moves."""
        artifact = _sweep(
            runner, main_env, _fresh_out(main_env), *_grid(planning="0.0", throughput="500.0")
        )
        row = artifact["results"][0]
        action = row["action"]
        assert action["battery_planned_nonidle"] is False
        assert action["first_nonidle_index"] is None
        assert action["first_nonidle_interval_start"] is None
        assert action["nonidle_interval_count"] == 0
        assert row["total_ac_throughput_kwh"] == pytest.approx(0.0, abs=1e-9)


# ---------------------------------------------------------------------------
# Planning and scoring bases, terminal mode, failure recording
# ---------------------------------------------------------------------------


class TestObjectiveBases:
    def test_both_bases_recorded_with_one_scoring_value(self, runner, main_env) -> None:
        artifact = _sweep(
            runner,
            main_env,
            _fresh_out(main_env),
            *_grid(planning="0.0,0.10", throughput="0.0,0.05"),
            "--scoring-terminal-value",
            "0.03",
        )
        rows = artifact["results"]
        assert {r["scoring_terminal_value_eur_per_kwh"] for r in rows} == {0.03}
        assert {r["planning_terminal_value_eur_per_kwh"] for r in rows} == {0.0, 0.10}
        for row in rows:
            delta = row["end_soc_kwh"] - row["initial_soc_kwh"]
            assert row["planning_inventory_adjustment_eur"] == pytest.approx(
                -row["planning_terminal_value_eur_per_kwh"] * delta
            )
            assert row["scoring_inventory_adjustment_eur"] == pytest.approx(-0.03 * delta)
            assert row["planning_objective_eur"] == pytest.approx(
                row["meter_cost_eur"]
                + row["planning_inventory_adjustment_eur"]
                + row["throughput_penalty_eur"]
            )
            assert row["degradation_adjusted_objective_eur"] == pytest.approx(
                row["meter_cost_eur"]
                + row["scoring_inventory_adjustment_eur"]
                + row["throughput_penalty_eur"]
            )
            assert row["throughput_penalty_eur"] == pytest.approx(
                row["throughput_cost_eur_per_kwh"] * row["total_ac_throughput_kwh"]
            )

    def test_solver_evidence_is_recorded_per_arm(self, runner, main_env) -> None:
        """Certification must reflect what the solver can actually prove per arm.

        A zero throughput cost is the historical objective, so it certifies; a
        nonzero cost changes what the objective means, so the core withholds the
        certificate and the driver records that rather than papering over it.
        """
        artifact = _sweep(
            runner, main_env, _fresh_out(main_env), *_grid(planning="0.0", throughput="0.0,0.05")
        )
        rows = {r["throughput_cost_eur_per_kwh"]: r for r in artifact["results"]}
        zero, nonzero = rows[0.0], rows[0.05]

        assert zero["solver_status"] == "optimal"
        assert zero["is_certified"] is True
        assert zero["bound_kind"] == "certified"
        assert zero["cost_lower_bound_eur"] == pytest.approx(zero["cost_upper_bound_eur"])
        assert zero["solver_gap_eur"] == pytest.approx(0.0)

        assert nonzero["is_certified"] is False
        assert nonzero["bound_kind"] == "upper"
        assert nonzero["cost_lower_bound_eur"] is None
        assert nonzero["cost_upper_bound_eur"] is not None
        assert nonzero["solver_gap_eur"] is None
        # Every arm still reports its own meter bill and both objective bases.
        for row in artifact["results"]:
            assert row["ran"] is True
            assert row["meter_cost_eur"] is not None
            assert row["planning_objective_eur"] is not None

    def test_parameters_are_echoed_verbatim_and_never_derived(self, runner, main_env) -> None:
        artifact = _sweep(
            runner,
            main_env,
            _fresh_out(main_env),
            *_grid(planning="0.0,0.125", throughput="0.0,0.0075"),
        )
        params = artifact["parameters"]
        assert params["planning_terminal_values_eur_per_kwh"] == [0.0, 0.125]
        assert params["throughput_costs_eur_per_kwh"] == [0.0, 0.0075]
        assert params["provenance"].startswith("every monetary parameter above is an explicit")
        assert "never used to fit" in artifact["window_coverage_note"]
        # The training window is recorded, but no parameter is computed from it.
        assert artifact["training_window_coverage"]["interval_count"] == 2 * SLOTS


class TestTerminalMode:
    def test_fixed_mode_rejects_a_nonzero_planning_value(self, runner, main_env) -> None:
        with pytest.raises(SystemExit) as excinfo:
            _sweep(
                runner,
                main_env,
                _fresh_out(main_env),
                *_grid(planning="0.0,0.05", throughput="0.0"),
                "--terminal-mode",
                "fixed",
            )
        message = _refusal(excinfo)
        assert "cannot enter the objective" in message
        assert "--terminal-mode free" in message

    def test_fixed_mode_accepts_an_all_zero_sweep(self, runner, main_env) -> None:
        artifact = _sweep(
            runner,
            main_env,
            _fresh_out(main_env),
            *_grid(planning="0.0", throughput="0.0,0.05"),
            "--terminal-mode",
            "fixed",
        )
        for row in artifact["results"]:
            assert row["terminal_mode"] == "fixed"
            # A fixed cycle returns to its seed, so there is no inventory to value.
            assert row["planning_inventory_adjustment_eur"] == pytest.approx(0.0)
            assert row["end_soc_kwh"] == pytest.approx(row["initial_soc_kwh"], abs=1e-6)

    def test_negative_throughput_cost_is_refused(self, runner, main_env) -> None:
        with pytest.raises(SystemExit) as excinfo:
            _sweep(
                runner, main_env, _fresh_out(main_env), *_grid(planning="0.0", throughput="-0.01")
            )
        assert "pay the battery to cycle" in _refusal(excinfo)

    def test_non_finite_parameter_is_rejected_by_the_parser(self, runner, main_env) -> None:
        with pytest.raises(SystemExit) as excinfo:
            runner.main(_args(main_env, _fresh_out(main_env), *_grid(throughput="nan")))
        assert excinfo.value.code == 2


class TestFailureRecording:
    def test_declared_solver_failure_is_recorded_and_exit_is_nonzero(
        self, runner, main_env, monkeypatch
    ) -> None:
        from energy_baseline.oracle import OracleInfeasibleError

        real = runner.optimize_with_throughput

        def flaky(spec, horizon, throughput_cost, **kwargs):
            if float(throughput_cost) == 0.05:
                raise OracleInfeasibleError("synthetic infeasibility for the test")
            return real(spec, horizon, throughput_cost, **kwargs)

        monkeypatch.setattr(runner, "optimize_with_throughput", flaky)
        out = _fresh_out(main_env, "partial")
        code = runner.main(_args(main_env, out, *_grid(planning="0.0", throughput="0.0,0.05")))
        assert code == 1, "a partial sweep must not read as a complete one"
        artifact = _read_artifact(out)
        assert artifact["complete"] is False
        assert artifact["grid"]["pairs_failed"] == 1
        failed = [r for r in artifact["results"] if not r["ran"]]
        assert len(failed) == 1
        assert failed[0]["throughput_cost_eur_per_kwh"] == pytest.approx(0.05)
        assert failed[0]["failure"].startswith("OracleInfeasibleError:")
        assert "no real degradation rates" in failed[0]["note"]
        # The healthy arm is untouched and still carries its evidence.
        ok = [r for r in artifact["results"] if r["ran"]]
        assert re.fullmatch(r"[0-9a-f]{64}", ok[0]["action"]["action_sha256"])

    def test_unexpected_error_is_not_swallowed(self, runner, main_env, monkeypatch) -> None:
        def explode(*args, **kwargs):
            raise RuntimeError("a bug, not a solver outcome")

        monkeypatch.setattr(runner, "optimize_with_throughput", explode)
        out = _fresh_out(main_env, "bug")
        with pytest.raises(RuntimeError, match="a bug"):
            runner.main(_args(main_env, out, *_grid()))
        assert not (out / ARTIFACT_NAME).exists()


# ---------------------------------------------------------------------------
# Output hygiene and lineage
# ---------------------------------------------------------------------------


class TestOutputHygiene:
    def test_output_inside_the_repository_is_refused(self, runner, main_env) -> None:
        inside = REPO_ROOT / "hack" / "_never_write_here"
        with pytest.raises(SystemExit) as excinfo:
            runner.main(_args(main_env, inside, *_grid()))
        assert "inside the git work tree" in _refusal(excinfo)
        assert not inside.exists()

    def test_output_inside_the_dataset_is_refused(self, runner, main_env) -> None:
        """A run must not write cost figures into the pinned build artifact."""
        inside = Path(main_env["dataset"]) / "sensitivity-output"
        with pytest.raises(SystemExit) as excinfo:
            runner.main(_args(main_env, inside, *_grid()))
        message = _refusal(excinfo)
        assert "inside the dataset directory" in message
        assert not inside.exists()

    def test_a_shared_string_prefix_is_not_mistaken_for_the_repo(self, runner) -> None:
        """The old lexical ``startswith`` check refused paths outside the work tree."""
        inside, worktree = runner._inside_git_worktree(REPO_ROOT)
        assert inside and worktree is not None
        sibling = Path(f"{worktree}-not-the-repo/econ-sensitivity")
        assert runner._resolve_output_dir(sibling) == sibling
        # Nothing was created on the way to deciding that.
        assert not sibling.exists()

    def test_real_output_outside_the_repo_is_written(self, runner, main_env) -> None:
        out = _fresh_out(main_env, "private")
        assert not str(out).startswith(str(REPO_ROOT))
        artifact = _sweep(runner, main_env, out, *_grid())
        assert artifact["artifact"] == ARTIFACT_NAME
        assert (out / MANIFEST_NAME).is_file()

    def test_non_empty_output_dir_is_refused(self, runner, main_env) -> None:
        out = _fresh_out(main_env, "clobber")
        _sweep(runner, main_env, out, *_grid())
        with pytest.raises(SystemExit) as excinfo:
            runner.main(_args(main_env, out, *_grid()))
        assert "refusing to overwrite" in _refusal(excinfo)

    def test_config_drift_against_the_build_manifest_is_refused(self, runner, main_env) -> None:
        other = _write_tariff(main_env["root"] / "config" / "tariff-drift.yaml", initial_soc=0.30)
        with pytest.raises(SystemExit) as excinfo:
            runner.main(
                [
                    "--dataset",
                    str(main_env["dataset"]),
                    "--config",
                    str(other),
                    "--output-dir",
                    str(_fresh_out(main_env, "drift")),
                    "--train-end",
                    main_env["train_end"],
                    *_grid(),
                ]
            )
        assert "tariff config drift" in _refusal(excinfo)

    def test_a_directory_with_no_manifest_is_refused(self, runner, main_env, tmp_path) -> None:
        with pytest.raises(SystemExit) as excinfo:
            runner.main(
                [
                    "--dataset",
                    str(tmp_path),
                    "--config",
                    str(main_env["tariff"]),
                    "--output-dir",
                    str(_fresh_out(main_env, "nomanifest")),
                    "--train-end",
                    main_env["train_end"],
                    *_grid(),
                ]
            )
        message = _refusal(excinfo)
        assert "dataset cannot be loaded" in message
        assert "build_manifest.json" in message


class TestLineage:
    def test_hashes_version_and_manifest_are_recorded(self, runner, main_env) -> None:
        artifact = _sweep(runner, main_env, _fresh_out(main_env), *_grid())
        lineage = artifact["lineage"]
        assert lineage["dataset_id"] == MAIN_DATASET_ID
        assert re.fullmatch(r"[0-9a-f]{64}", lineage["build_manifest_sha256"])
        assert re.fullmatch(r"[0-9a-f]{64}", lineage["config_hash_canonical"])
        assert re.fullmatch(r"[0-9a-f]{64}", lineage["config_sha256_raw_bytes"])
        assert lineage["config_hash_canonical"] == lineage["build_manifest"]["config_hash"]
        assert lineage["code_version"], "the code version must pin the source that ran"
        assert all(re.fullmatch(r"[0-9a-f]{64}", h) for h in lineage["source_hashes"].values())
        assert set(lineage["source_hashes"]) >= {
            "hack/economic_sensitivity.py",
            "src/energy_baseline/economic_sensitivity.py",
            "src/energy_baseline/oracle.py",
        }
        assert len(lineage["dataset_part_files"]) == 1
        assert lineage["dataset_part_files"][0]["rows"] == 12 * SLOTS
        assert lineage["dataset_unlisted_parts"] == []

    def test_the_source_digest_matches_the_file_on_disk(self, runner, main_env) -> None:
        artifact = _sweep(runner, main_env, _fresh_out(main_env), *_grid())
        recorded = artifact["lineage"]["source_hashes"]["hack/economic_sensitivity.py"]
        assert recorded == hashlib.sha256(RUNNER.read_bytes()).hexdigest()

    def test_units_block_is_naming_only(self, runner, main_env) -> None:
        artifact = _sweep(runner, main_env, _fresh_out(main_env), *_grid())
        units = artifact["units_and_naming"]
        assert "naming convention only" in units["throughput_cost_eur_per_kwh"].lower()
        assert "no real degradation rate" in units["throughput_cost_eur_per_kwh"].lower()
        assert "naming convention only" in units["planning_terminal_value_eur_per_kwh"].lower()
        assert "naming convention only" in units["scoring_terminal_value_eur_per_kwh"].lower()
        assert "declared limit, not a measurement" in units["time_limit_s"].lower()
        for row in artifact["results"]:
            assert "no real degradation rates are assumed or fitted" in row["note"]


class TestRunsAsAScript:
    def test_subprocess_invocation_writes_the_artifact(self, main_env, tmp_path) -> None:
        out = tmp_path / "subprocess-run"
        proc = subprocess.run(
            [sys.executable, str(RUNNER), *_args(main_env, out, *_grid())],
            capture_output=True,
            text=True,
            cwd=REPO_ROOT,
        )
        assert proc.returncode == 0, proc.stderr
        artifact = _read_artifact(out)
        assert artifact["grid"]["pairs_solved"] == 1
        assert "wrote" in proc.stdout

    def test_argparse_errors_exit_two(self, main_env, tmp_path) -> None:
        proc = subprocess.run(
            [
                sys.executable,
                str(RUNNER),
                "--dataset",
                str(main_env["dataset"]),
                "--config",
                str(main_env["tariff"]),
                "--output-dir",
                str(tmp_path / "unused"),
                "--train-end",
                main_env["train_end"],
                "--planning-terminal-values",
                "0.0",
                "--throughput-costs",
                "not-a-float",
            ],
            capture_output=True,
            text=True,
            cwd=REPO_ROOT,
        )
        assert proc.returncode == 2
        assert "not a comma-separated float list" in proc.stderr
