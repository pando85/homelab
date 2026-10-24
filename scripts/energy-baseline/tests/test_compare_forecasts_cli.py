"""CLI smoke test for ``hack/compare_forecasts.py`` against a synthetic BUILT dataset.

Unit tests cannot catch API drift between the runner and the library (a renamed
attribute or a changed signature fails only when the script actually executes).
This module builds a small but real on-disk dataset — manifest + parquet part, the
same shape ``build`` writes and :func:`energy_baseline.benchmark.load_dataset`
verifies — and runs the runner as a subprocess.

The synthetic dataset/table/manifest helpers mirror the ones in
``tests/test_benchmark.py`` (that file keeps them module-local and there is no
cross-test import mechanism, so ``tests/test_report.py`` duplicates them too).
They are duplicated here for the same reason rather than edited into a shared
fixture, which would mean modifying an existing file.

Everything is synthetic: no network, no credentials, no real data directory.
"""

from __future__ import annotations

import hashlib
import json
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

from energy_baseline.schemas import SCHEMA_VERSION

REPO_ROOT = Path(__file__).resolve().parents[1]
RUNNER = REPO_ROOT / "hack" / "compare_forecasts.py"
MADRID = ZoneInfo("Europe/Madrid")

_INTERVAL_SECONDS = 900


# ---------------------------------------------------------------------------
# Synthetic dataset helpers (mirrors tests/test_benchmark.py)
# ---------------------------------------------------------------------------


def _write_tariff(path: Path) -> Path:
    """Write a tariff.yaml whose battery block satisfies ``resolve_battery_assumptions``.

    Only four battery fields are mandatory (capacity, power, an efficiency, the SOC
    window); ``initial_soc_frac`` and ``import_limit_kw`` are optional and supplied
    here so the runner exercises a real ceiling rather than the unknown-ceil path.
    """
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
            "round_trip_efficiency": 0.88,
            "soc_window_min": 0.05,
            "soc_window_max": 0.95,
            "initial_soc_frac": 0.20,
            "import_limit_kw": 5.0,
        },
    }
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(yaml.safe_dump(data, sort_keys=False))
    return path


def _daily_profile(day: date, *, intervals: int = 96) -> tuple[np.ndarray, np.ndarray]:
    """Deterministic daily price/load shape with a cheap night and an expensive evening."""
    prices = np.zeros(intervals)
    load = np.zeros(intervals)
    for i in range(intervals):
        hour = (i * 15) // 60
        # A small deterministic per-day wobble so no two days are identical.
        wobble = 0.02 * ((day.toordinal() % 7) - 3) / 3.0
        if hour < 6:
            prices[i] = 0.04 + wobble
        elif hour < 10:
            prices[i] = 0.19 + wobble
        elif hour < 14:
            prices[i] = 0.11 + wobble
        elif hour < 18:
            prices[i] = 0.07 + wobble
        elif hour < 22:
            prices[i] = 0.24 + wobble
        else:
            prices[i] = 0.05 + wobble
        # Load tracks the day: baseload overnight, peaks morning and evening.
        if hour < 6:
            load[i] = 0.030
        elif hour < 10:
            load[i] = 0.120
        elif hour < 14:
            load[i] = 0.080
        elif hour < 18:
            load[i] = 0.060
        elif hour < 22:
            load[i] = 0.150
        else:
            load[i] = 0.045
    return prices, load


def _analysis_table(
    *,
    start: datetime,
    days: int,
    dataset_id: str,
    negative_price_days: tuple[int, ...] = (),
) -> pa.Table:
    """Build an ANALYSIS_INTERVAL-shaped table spanning *days* local days from *start*.

    ``start`` must be local midnight in Europe/Madrid so every local day carries
    exactly 96 intervals (the DST days are deliberately outside the window this
    smoke test uses; DST itself is pinned in ``test_rolling_horizon.py``).
    """
    starts: list[datetime] = []
    ends: list[datetime] = []
    local_day: list[date] = []
    gross: list[float] = []
    prices: list[float] = []

    cursor = start
    for d in range(days):
        day = start.astimezone(MADRID).date() + timedelta(days=d)
        day_prices, day_load = _daily_profile(day)
        if d in negative_price_days:
            # A surplus-solar morning: genuinely negative prices must not be clamped.
            day_prices[:24] -= 0.10
        for i in range(96):
            starts.append(cursor)
            ends.append(cursor + timedelta(seconds=_INTERVAL_SECONDS))
            local_day.append(cursor.astimezone(MADRID).date())
            gross.append(float(day_load[i]))
            prices.append(float(day_prices[i]))
            cursor = cursor + timedelta(seconds=_INTERVAL_SECONDS)

    n = len(starts)
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
            "energy_quality": pa.array(["measured"] * n, pa.string()),
            "allocation_quality": pa.array(["exact"] * n, pa.string()),
            "sample_count": pa.array([4] * n, pa.int32()),
            "gap_seconds": pa.array([0.0] * n, pa.float64()),
            "price_eur_per_kwh": pa.array(prices, pa.float64()),
            "price_quality": pa.array(["measured"] * n, pa.string()),
            "dataset_id": pa.array([dataset_id] * n, pa.string()),
        },
        schema=_analysis_schema(),
    )


def _analysis_schema() -> pa.Schema:
    """The exact ANALYSIS_INTERVAL schema ``validate_table`` requires."""
    from energy_baseline.schemas import ANALYSIS_INTERVAL

    return ANALYSIS_INTERVAL


def _write_dataset(
    root: Path,
    table: pa.Table,
    *,
    config_path: Path,
    dataset_id: str = "smoke-dataset",
) -> Path:
    """Write ``<root>/analysis/v<schema>/<dataset_id>`` with a valid build manifest."""
    from energy_baseline.manifest import config_hash

    dataset_dir = root / "analysis" / f"v{SCHEMA_VERSION}" / dataset_id
    parts_dir = dataset_dir / "parts"
    parts_dir.mkdir(parents=True, exist_ok=True)

    part_path = parts_dir / f"analysis-{dataset_id}-00000000.parquet"
    pq.write_table(table, part_path)
    sha = hashlib.sha256(part_path.read_bytes()).hexdigest()

    manifest: dict[str, Any] = {
        "manifest_version": 1,
        "dataset_id": dataset_id,
        "schema_version": SCHEMA_VERSION,
        "config_hash": config_hash(config_path),
        "code_version": "smoke-test",
        "part_files": [
            {
                "path": str(part_path.relative_to(dataset_dir)),
                "sha256": sha,
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


@pytest.fixture
def smoke_setup(tmp_path: Path) -> dict[str, Any]:
    """A 40-day synthetic dataset: 32 days train + 8 days evaluation."""
    tariff = _write_tariff(tmp_path / "config" / "tariff.yaml")
    start = datetime(2025, 4, 1, 0, 0, tzinfo=MADRID)  # local midnight, no DST inside window
    table = _analysis_table(
        start=start,
        days=40,
        dataset_id="smoke-dataset",
        negative_price_days=(35,),  # inside the evaluation window
    )
    dataset_dir = _write_dataset(tmp_path / "data", table, config_path=tariff)
    test_start = start + timedelta(days=32)
    test_end = start + timedelta(days=40)
    return {
        "tmp": tmp_path,
        "tariff": tariff,
        "dataset": dataset_dir,
        "output": tmp_path / "out",
        "test_start": test_start.astimezone(UTC).isoformat().replace("+00:00", "Z"),
        "test_end": test_end.astimezone(UTC).isoformat().replace("+00:00", "Z"),
    }


def _run(args: dict[str, Any], *extra: str) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        [
            sys.executable,
            str(RUNNER),
            "--dataset",
            str(args["dataset"]),
            "--config",
            str(args["tariff"]),
            "--output",
            str(args["output"]),
            "--test-start",
            args["test_start"],
            "--test-end",
            args["test_end"],
            *extra,
        ],
        capture_output=True,
        text=True,
        timeout=900,
    )


# ---------------------------------------------------------------------------
# Smoke tests
# ---------------------------------------------------------------------------


class TestCliSmoke:
    def test_runner_executes_and_writes_summary(self, smoke_setup: dict[str, Any]) -> None:
        """The runner imports the library, loads the dataset and writes a summary.

        This is the API-drift guard: ``loaded.interval_seconds``,
        ``resolve_battery_assumptions(config)``/``spec_kwargs()``, ``usable_mask`` and
        both policy entry points are exercised for real, not mocked.
        """
        proc = _run(smoke_setup, "--windows", "28", "--sigmas", "0", "--seeds", "0")
        assert proc.returncode == 0, (
            f"runner exited {proc.returncode}\nSTDOUT:\n{proc.stdout}\nSTDERR:\n{proc.stderr}"
        )

        summary_path = smoke_setup["output"] / "summary.json"
        assert summary_path.is_file(), "summary.json was not written"
        summary = json.loads(summary_path.read_text())

        assert summary["dataset_id"] == "smoke-dataset"
        assert summary["interval_seconds"] == 900.0
        assert summary["coverage"]["evaluation_intervals"] == 8 * 96
        assert summary["coverage"]["training_local_days"] == 32
        assert summary["coverage"]["dataset_usable_fraction"] == pytest.approx(1.0)

        # Frozen provenance: trained strictly before the cut, from a fixed statistic.
        assert np.isfinite(summary["terminal_value_eur_per_kwh"])
        assert "TRAINING" in summary["terminal_value_source"]

        assert len(summary["runs"]) == 1
        run = summary["runs"][0]
        assert run["sigma"] == 0.0
        assert run["seed"] == 0
        assert run["window_days_used"] == 28

        # Every policy must have produced a cost on the held-out window.
        for policy in ("baseline", "day_ahead", "rolling_horizon"):
            block = run[policy]
            assert block["ran"] is True, f"{policy}: {block.get('failure')}"
            assert np.isfinite(block["cost_eur"])

        # Baseline has no battery by construction.
        assert run["baseline"]["throughput_kwh"] == 0.0
        assert run["baseline"]["saving_eur"] == pytest.approx(0.0)

        # Detailed stage status, not just a cost.
        rh = run["rolling_horizon"]
        assert "events_solved" in rh and "events_with_clipping" in rh
        assert rh["events_infeasible"] == 0
        da = run["day_ahead"]
        assert "days_solved" in da

        # Forecast never enters cost.
        assert run["load_forecast"]["enters_cost"] is False
        assert run["load_forecast"]["training_end_idx"] == 32 * 96

    def test_outputs_include_both_policies_with_full_columns(
        self, smoke_setup: dict[str, Any]
    ) -> None:
        """Every policy schedule carries timestamps, mask, true prices/load and flows."""
        proc = _run(smoke_setup, "--windows", "28", "--sigmas", "0", "--seeds", "0")
        assert proc.returncode == 0, proc.stderr

        sched_dir = smoke_setup["output"] / "schedules"
        files = sorted(p.name for p in sched_dir.glob("*.json"))
        assert any(name.endswith("_day_ahead.json") for name in files), files
        assert any(name.endswith("_rolling.json") for name in files), files

        required = {
            "timestamps_utc",
            "usable",
            "true_price_eur_per_kwh",
            "true_load_kwh",
            "charge_kwh",
            "discharge_kwh",
            "soc_kwh",
            "grid_kwh",
        }
        for name in files:
            payload = json.loads((sched_dir / name).read_text())
            missing = required - set(payload)
            assert not missing, f"{name} is missing {missing}"
            length = len(payload["timestamps_utc"])
            assert length == 8 * 96
            for column in (
                "true_price_eur_per_kwh",
                "true_load_kwh",
                "charge_kwh",
                "discharge_kwh",
                "soc_kwh",
                "grid_kwh",
            ):
                assert len(payload[column]) == length, f"{name}:{column} length"
            datetime.fromisoformat(payload["timestamps_utc"][0].replace("Z", "+00:00"))
            assert all(bool(u) for u in payload["usable"])

    def test_summary_is_json_strict_and_cost_matches_flows(
        self, smoke_setup: dict[str, Any]
    ) -> None:
        """No NaN/inf escapes into the summary, and the reported cost is the dot product."""
        proc = _run(smoke_setup, "--windows", "28", "--sigmas", "0", "--seeds", "0")
        assert proc.returncode == 0, proc.stderr

        out = smoke_setup["output"]
        raw = (out / "summary.json").read_text()
        assert "NaN" not in raw and "Infinity" not in raw
        json.loads(raw, parse_constant=_reject_constant)

        sched = next(iter(sorted((out / "schedules").glob("*_rolling.json"))))
        payload = json.loads(sched.read_text())
        grid = np.asarray(payload["grid_kwh"], dtype=float)
        prices = np.asarray(payload["true_price_eur_per_kwh"], dtype=float)
        usable = np.asarray(payload["usable"], dtype=bool)
        # Scoring is dot(true price, actual grid) over usable intervals — never the
        # noisy price the plan was built on.
        expected = float(np.dot(prices[usable], grid[usable]))
        run = json.loads((out / "summary.json").read_text())["runs"][0]
        assert run["rolling_horizon"]["cost_eur"] == pytest.approx(expected, rel=1e-9, abs=1e-9)

    def test_negative_price_days_flow_through_without_clamping(
        self, smoke_setup: dict[str, Any]
    ) -> None:
        """A negative-price evaluation day is scored negative, not floored to zero."""
        proc = _run(smoke_setup, "--windows", "28", "--sigmas", "0", "--seeds", "0")
        assert proc.returncode == 0, proc.stderr

        sched = next(iter(sorted((smoke_setup["output"] / "schedules").glob("*_rolling.json"))))
        payload = json.loads(sched.read_text())
        prices = np.asarray(payload["true_price_eur_per_kwh"], dtype=float)
        assert (prices < 0).any(), "fixture lost its negative-price day"
        # No clamping, and grid stays non-negative even where the price is negative.
        assert np.all(np.asarray(payload["grid_kwh"]) >= -1e-9)

    def test_refuses_to_clobber_a_nonempty_output_dir(self, smoke_setup: dict[str, Any]) -> None:
        """A fresh run is required: an occupied --output must abort non-zero."""
        first = _run(smoke_setup, "--windows", "28", "--sigmas", "0", "--seeds", "0")
        assert first.returncode == 0, first.stderr

        second = _run(smoke_setup, "--windows", "28", "--sigmas", "0", "--seeds", "0")
        assert second.returncode != 0
        assert "not empty" in (second.stdout + second.stderr)

    @pytest.mark.parametrize("bad_sigma", ["-0.1", "nan", "inf"])
    def test_rejects_an_unusable_sigma(self, smoke_setup: dict[str, Any], bad_sigma: str) -> None:
        """A negative or non-finite sigma would make the run meaningless but still 'succeed'."""
        proc = _run(smoke_setup, "--windows", "28", "--sigmas", bad_sigma, "--seeds", "0")
        assert proc.returncode != 0
        assert "sigma" in (proc.stdout + proc.stderr)

    def test_noise_stats_cover_only_estimated_slots(self, smoke_setup: dict[str, Any]) -> None:
        """With sigma>0 accuracy describes the estimated tomorrow slots, not all history.

        The whole-dataset population would dilute the figure with days whose prices were
        known exactly; ``sigma=0`` keeps every metric key present at its ideal value so a
        reader compares rows on one schema.
        """
        proc = _run(smoke_setup, "--windows", "28", "--sigmas", "0,0.1", "--seeds", "0")
        assert proc.returncode == 0, proc.stderr

        runs = json.loads((smoke_setup["output"] / "summary.json").read_text())["runs"]
        by_sigma = {float(r["sigma"]): r for r in runs}
        assert set(by_sigma) == {0.0, 0.1}

        exact = by_sigma[0.0]["price_noise"]
        assert exact["noise_applied"] is False
        assert exact["mape"] == 0.0
        assert exact["mae"] == 0.0
        assert exact["frac_within_10pct"] == 1.0
        assert exact["sign_flips"] == 0

        noisy = by_sigma[0.1]["price_noise"]
        # One schema for both rows.
        assert set(exact) <= set(noisy)
        assert noisy["noise_applied"] is True
        assert noisy["stats_on_estimated_slots_only"] is True

        # The estimated population is the D+1 tail of each afternoon event: a minority of
        # the dataset, and far smaller than it.
        total_intervals = 40 * 96
        assert 0 < noisy["stats_slots_considered"] < total_intervals

        assert noisy["mape"] is not None and noisy["mape"] > 0.0
        assert 0.0 <= noisy["frac_within_10pct"] <= 1.0
        # Per-day records are only kept for days that were actually estimated.
        assert 0 < len(noisy["per_day"]) <= 8


class TestCliBiasSigmas:
    """``--bias-sigmas`` must thread from the CLI into the model, the record and the name."""

    def test_bias_sigmas_default_to_the_iid_only_run(self, smoke_setup: dict[str, Any]) -> None:
        """Omitting the flag reproduces today's matrix exactly, one row, bias_sigma 0."""
        proc = _run(smoke_setup, "--windows", "28", "--sigmas", "0.1", "--seeds", "0")
        assert proc.returncode == 0, proc.stderr

        runs = json.loads((smoke_setup["output"] / "summary.json").read_text())["runs"]
        assert len(runs) == 1
        assert runs[0]["bias_sigma"] == 0.0
        assert runs[0]["price_noise"]["bias_sigma"] == 0.0
        assert runs[0]["price_noise"]["bias_days_applied"] == len(runs[0]["price_noise"]["per_day"])
        # A bias sigma of 0 applied no bias at all, so the accuracy block is exactly the
        # i.i.d. model's — the property that keeps the 27 runs already on disk comparable.
        assert runs[0]["price_noise"]["bias_mean_applied"] == 0.0
        assert runs[0]["price_noise"]["bias_std_applied"] == 0.0

    def test_bias_sigma_grid_multiplies_runs_and_is_recorded(
        self, smoke_setup: dict[str, Any]
    ) -> None:
        """``--bias-sigmas 0,0.1`` gives the cross product and records each arm."""
        proc = _run(
            smoke_setup,
            "--windows",
            "28",
            "--sigmas",
            "0",
            "--bias-sigmas",
            "0,0.1",
            "--seeds",
            "0",
        )
        assert proc.returncode == 0, proc.stderr

        summary = json.loads((smoke_setup["output"] / "summary.json").read_text())
        runs = summary["runs"]
        assert [(r["sigma"], r["bias_sigma"]) for r in runs] == [(0.0, 0.0), (0.0, 0.1)]

        exact, biased = runs
        # The exact-price control is unaffected by a bias sigma it did not ask for.
        assert exact["price_noise"]["noise_applied"] is False
        # With sigma=0 and bias_sigma>0 the estimate is a pure day-level scaling, so the
        # MAPE over estimated slots must equal the mean absolute applied day bias: an
        # independent read that the flag reached the model rather than just the record.
        noise = biased["price_noise"]
        assert noise["noise_applied"] is True
        assert noise["bias_sigma"] == 0.1
        assert 0 < noise["bias_days_applied"] <= 8
        assert all("bias_applied" in rec for rec in noise["per_day"])
        # Every estimated slot of a day carries that day's bias, so the MAPE is the
        # per-slot-weighted mean of |bias| over the days actually estimated.  Recomputing
        # it from the reported per-day records is an independent read that the flag
        # reached the model and not merely the run record.
        per_day = noise["per_day"]
        weights = np.array([rec["estimated_intervals"] for rec in per_day], dtype=float)
        magnitudes = np.array([abs(rec["bias_applied"]) for rec in per_day], dtype=float)
        assert noise["zero_price_count"] == 0, "fixture gained a zero price; weighting changes"
        assert noise["mape"] == pytest.approx(
            float((weights * magnitudes).sum() / weights.sum()), rel=1e-9
        )
        # sigma=0 means no within-day error: the day's bias must be inside the spread.
        assert magnitudes.max() > 0.0

    def test_schedule_filenames_carry_bias_sigma_so_arms_cannot_be_confused(
        self, smoke_setup: dict[str, Any]
    ) -> None:
        """Two arms differing only by bias must not land in one artifact name."""
        proc = _run(
            smoke_setup,
            "--windows",
            "28",
            "--sigmas",
            "0.1",
            "--bias-sigmas",
            "0,0.1",
            "--seeds",
            "0",
        )
        assert proc.returncode == 0, proc.stderr

        names = sorted(p.name for p in (smoke_setup["output"] / "schedules").glob("*_rolling.json"))
        assert len(names) == 2, names
        assert all("_bias_" in name for name in names), names
        assert len(set(names)) == 2, names
        # And each file's own payload agrees with the arm its name advertises.
        for name in names:
            payload = json.loads((smoke_setup["output"] / "schedules" / name).read_text())
            advertised = float(name.split("_bias_")[1].split("_")[0])
            assert payload["bias_sigma"] == advertised, name

    @pytest.mark.parametrize("bad_bias", ["-0.1", "nan", "inf"])
    def test_rejects_an_unusable_bias_sigma(
        self, smoke_setup: dict[str, Any], bad_bias: str
    ) -> None:
        """Same validity rules as sigma: a non-finite bias poisons every price silently."""
        proc = _run(
            smoke_setup,
            "--windows",
            "28",
            "--sigmas",
            "0",
            "--bias-sigmas",
            bad_bias,
            "--seeds",
            "0",
        )
        assert proc.returncode != 0
        assert "bias-sigmas" in (proc.stdout + proc.stderr)

    def test_bias_survives_a_nonempty_output_refusal_and_json_strictness(
        self, smoke_setup: dict[str, Any]
    ) -> None:
        """The existing guards still hold on a bias run, and no NaN reaches summary.json."""
        first = _run(smoke_setup, "--windows", "28", "--sigmas", "0,0.1", "--bias-sigmas", "0,0.1")
        assert first.returncode == 0, first.stderr
        raw = (smoke_setup["output"] / "summary.json").read_text()
        assert "NaN" not in raw and "Infinity" not in raw
        json.loads(raw, parse_constant=_reject_constant)

        second = _run(smoke_setup, "--windows", "28", "--sigmas", "0,0.1", "--bias-sigmas", "0,0.1")
        assert second.returncode != 0
        assert "not empty" in (second.stdout + second.stderr)


def _reject_constant(token: str) -> None:
    raise ValueError(f"non-standard JSON constant: {token}")
