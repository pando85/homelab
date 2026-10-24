"""Tests for the rolling horizon dispatcher.

Pins the invariants the two-event policy is claimed to satisfy:

* Deterministic price noise (same seed → same result; different seed → different).
* No future contamination: scoring uses TRUE prices, not noisy ones.
* Each interval executed exactly once across all events.
* SOC carry-forward: the overnight coupling changes the starting SOC, so the
  rolling horizon is NOT bit-identical to a day-ahead with the same data.
* Future price perturbations do NOT change already-committed current actions.
* Solver failure degrades to zero flows, not substitution.
* Seed floor is respected throughout the roll.
* DST events are placed at the correct local time.
* Cost is ``dot(true_prices, actual_grid)`` — no fake credit from noise.
"""

from __future__ import annotations

import hashlib
import json
from datetime import UTC, date, datetime, timedelta
from typing import Any
from zoneinfo import ZoneInfo

import numpy as np
import pytest

from energy_baseline.battery import (
    BatterySpec,
)
from energy_baseline.policies import contiguous_day_bounds
from energy_baseline.rolling_horizon import (
    AFTERNOON_EVENT,
    EVENING_EVENT,
    _clock_slot_position,
    build_frozen_load_profile,
    compute_rolling_events,
    generate_price_noise,
    run_rolling_horizon,
)

QUARTER = 900.0
MADRID = ZoneInfo("Europe/Madrid")


def _spec(**overrides) -> BatterySpec:
    base = dict(
        capacity_kwh=5.12,
        power_kw=2.5,
        charge_efficiency=0.95,
        discharge_efficiency=0.95,
        soc_min_frac=0.05,
        soc_max_frac=0.95,
        initial_soc_frac=0.5,
        terminal_soc_frac=None,
    )
    base.update(overrides)
    return BatterySpec(**base)


def _make_dataset(
    start_utc: datetime,
    n_days: int,
    *,
    intervals_per_day: int = 96,
    base_price: float = 0.15,
    base_load: float = 0.2,
    interval_seconds: float = QUARTER,
) -> tuple[np.ndarray, np.ndarray, np.ndarray, list[date], list[datetime]]:
    """Synthetic dataset: n_days of uniform intervals starting at start_utc."""
    n = n_days * intervals_per_day
    prices = np.full(n, base_price)
    load = np.full(n, base_load)
    usable = np.ones(n, dtype=bool)
    interval_starts = [start_utc + timedelta(seconds=i * interval_seconds) for i in range(n)]
    local_days = []
    for i in range(n):
        local_dt = interval_starts[i].astimezone(MADRID)
        local_days.append(local_dt.date())
    return prices, load, usable, local_days, interval_starts


def _dst_fall_back_window() -> tuple[
    np.ndarray, np.ndarray, np.ndarray, list[date], list[datetime]
]:
    """Whole local days 2025-10-25/26/27 in Europe/Madrid (the fall-back weekend).

    Start at 2025-10-24T22:00Z — which is 2025-10-25 00:00 CEST — and generate five
    local days' worth of intervals so the day after the returned slice exists in the
    grid (the events on the 27th plan to that midnight).  The slice keeps days 25, 26
    and 27 **whole**: starting at UTC midnight would cut the 25-hour fall-back day in
    half and make its interval count meaningless.
    """
    start = datetime(2025, 10, 24, 22, 0, tzinfo=UTC)
    prices, load, usable, local_days, starts = _make_dataset(start, 5)
    first_of_25 = next(i for i, d in enumerate(local_days) if d == date(2025, 10, 25))
    first_of_28 = next(i for i, d in enumerate(local_days) if d == date(2025, 10, 28))
    assert first_of_25 == 0, "fixture should open exactly at local midnight on the 25th"
    return (
        prices[first_of_25:first_of_28],
        load[first_of_25:first_of_28],
        usable[first_of_25:first_of_28],
        local_days[first_of_25:first_of_28],
        starts[first_of_25:first_of_28],
    )


def _dst_spring_forward_window() -> tuple[
    np.ndarray, np.ndarray, np.ndarray, list[date], list[datetime]
]:
    """Whole local days 2026-03-28/29/30 in Europe/Madrid (the spring-forward weekend).

    The mirror of :func:`_dst_fall_back_window` for the short direction: 2026-03-29 holds
    **92** intervals because 02:00 CET never happens.  Starts at 2026-03-27T23:00Z — which
    is 2026-03-28 00:00 CET — so the slice opens exactly at local midnight and the truncated
    day is whole rather than cut in half, and generates five days so the day *after* the
    returned slice exists in the grid (events on the 29th plan through to the end of the
    30th).  End-to-end coverage of this direction was the gap the fall-back tests left.
    """
    start = datetime(2026, 3, 27, 23, 0, tzinfo=UTC)
    prices, load, usable, local_days, starts = _make_dataset(start, 5)
    first_of_28 = next(i for i, d in enumerate(local_days) if d == date(2026, 3, 28))
    first_of_31 = next(i for i, d in enumerate(local_days) if d == date(2026, 3, 31))
    assert first_of_28 == 0, "fixture should open exactly at local midnight on the 28th"
    return (
        prices[first_of_28:first_of_31],
        load[first_of_28:first_of_31],
        usable[first_of_28:first_of_31],
        local_days[first_of_28:first_of_31],
        starts[first_of_28:first_of_31],
    )


# ---------------------------------------------------------------------------
# Price noise
# ---------------------------------------------------------------------------


class TestPriceNoise:
    def test_sigma_zero_is_exact(self) -> None:
        prices = np.array([0.1, 0.2, -0.05, 0.0, 0.3])
        days = [(date(2025, 1, 1), 0, 5)]
        noisy, meta = generate_price_noise(prices, None, 0.0, seed=42, day_bounds=days)
        assert np.array_equal(noisy, prices)
        assert meta["noise_applied"] is False
        assert meta["sign_flips"] == 0

    def test_deterministic_same_seed(self) -> None:
        prices = np.linspace(0.1, 0.3, 96)
        days = [(date(2025, 6, 15), 0, 96)]
        noisy1, _ = generate_price_noise(prices, None, 0.1, seed=7, day_bounds=days)
        noisy2, _ = generate_price_noise(prices, None, 0.1, seed=7, day_bounds=days)
        assert np.array_equal(noisy1, noisy2)

    def test_different_seeds_differ(self) -> None:
        prices = np.linspace(0.1, 0.3, 96)
        days = [(date(2025, 6, 15), 0, 96)]
        noisy1, _ = generate_price_noise(prices, None, 0.1, seed=0, day_bounds=days)
        noisy2, _ = generate_price_noise(prices, None, 0.1, seed=1, day_bounds=days)
        assert not np.array_equal(noisy1, noisy2)

    def test_different_dates_differ(self) -> None:
        prices = np.full(192, 0.15)
        days = [(date(2025, 6, 15), 0, 96), (date(2025, 6, 16), 96, 192)]
        noisy, meta = generate_price_noise(prices, None, 0.1, seed=0, day_bounds=days)
        # The two days should have different noise patterns
        assert not np.array_equal(noisy[:96], noisy[96:])

    def test_negative_prices_no_clamping(self) -> None:
        prices = np.array([-0.05, -0.02, 0.0, 0.1, 0.2])
        days = [(date(2025, 1, 1), 0, 5)]
        noisy, meta = generate_price_noise(prices, None, 0.1, seed=0, day_bounds=days)
        # Negative prices can become positive and vice versa (sign flip)
        # No clamping: noisy values can be anything
        assert meta["sign_flips"] >= 0  # may or may not flip with small sigma

    def test_zero_prices_stay_zero(self) -> None:
        prices = np.array([0.0, 0.0, 0.1, 0.2])
        days = [(date(2025, 1, 1), 0, 4)]
        noisy, meta = generate_price_noise(prices, None, 0.1, seed=0, day_bounds=days)
        # 0 * (1 + noise) = 0
        assert noisy[0] == 0.0
        assert noisy[1] == 0.0
        assert meta["zero_price_count"] == 2

    def test_mape_excludes_zero_denominator(self) -> None:
        prices = np.array([0.0, 0.1, 0.2, 0.3])
        days = [(date(2025, 1, 1), 0, 4)]
        _, meta = generate_price_noise(prices, None, 0.1, seed=0, day_bounds=days)
        assert meta["mape"] is not None  # computed over non-zero prices only
        assert meta["zero_price_count"] == 1

    def test_full_day_generated_regardless_of_horizon(self) -> None:
        """Noise is generated for the complete day, not just the test horizon."""
        prices = np.full(96, 0.15)
        days = [(date(2025, 6, 15), 0, 96)]
        noisy, meta = generate_price_noise(prices, None, 0.1, seed=0, day_bounds=days)
        assert noisy.size == 96
        assert len(meta["per_day"]) == 1
        assert meta["per_day"][0]["intervals"] == 96


# ---------------------------------------------------------------------------
# Day-level multiplicative bias
#
# The i.i.d. per-slot model above largely preserves the ordering of tomorrow's hours
# and averages out over a day, so an optimiser can shrug it off.  A persistent
# day-level bias instead moves tomorrow as a whole relative to today — which is how a
# real day-ahead proxy errs when a weather regime or a forward component is misjudged —
# and attacks the cross-day hold-versus-spend decision, where averaging does not help.
# These tests pin that second channel *without* deciding whether it matters: nothing
# here tunes a sigma to make any conclusion come out either way.
# ---------------------------------------------------------------------------


def _golden_digest(values: np.ndarray) -> str:
    """Order- and width-explicit digest of an array, for pinning bit-identical output."""
    return hashlib.sha256(np.asarray(values, dtype=">f8").tobytes()).hexdigest()


class TestPriceNoiseBitCompat:
    """``bias_sigma=0`` must reproduce the pre-bias model *bit-for-bit*.

    The holdout runs already on disk were produced by the i.i.d.-only model.  If adding
    a bias channel perturbed that stream — even by consuming one extra variate from the
    same generator — the old and new rows would silently describe different noise
    processes while carrying the same sigma.  The digests below were captured from the
    implementation *before* the bias existed and are pinned, not recomputed at runtime,
    so this test fails loudly on any re-derivation of the stream.
    """

    def test_bias_sigma_zero_reproduces_pre_bias_array(self) -> None:
        prices = np.linspace(0.02, 0.31, 288)
        days = [
            (date(2025, 6, 15), 0, 96),
            (date(2025, 6, 16), 96, 192),
            (date(2025, 6, 17), 192, 288),
        ]
        noisy_default, _ = generate_price_noise(prices, None, 0.1, seed=1234, day_bounds=days)
        noisy_explicit, _ = generate_price_noise(
            prices, None, 0.1, seed=1234, bias_sigma=0.0, day_bounds=days
        )

        assert _golden_digest(noisy_default) == (
            "1b241f5aebfe3d898ec1c722070a1ed1e0dd087294feb8258397a4e4726db146"
        )
        # Omitting the argument and passing 0.0 are the same run.
        assert np.array_equal(noisy_default, noisy_explicit)

    def test_bias_sigma_zero_reproduces_pre_bias_array_with_zeroes_and_negatives(self) -> None:
        """The pinned stream also covers the zero/negative paths, which a bias would scale."""
        prices = np.array([-0.05, 0.0, 0.07, -0.001, 0.2, 0.0, 0.13])
        days = [(date(2025, 1, 1), 0, prices.size)]
        noisy, _ = generate_price_noise(prices, None, 0.2, seed=7, day_bounds=days)

        assert _golden_digest(noisy) == (
            "604f21315c01817a83145cfaa5251405a5b0becec315dd140a2da34b77b94b89"
        )
        assert noisy[1] == 0.0 and noisy[5] == 0.0, "a zero true price must stay exactly zero"

    def test_pre_bias_stream_is_the_documented_derivation(self) -> None:
        """Pin the stream against an inline replica of the pre-bias algorithm.

        A bare digest only proves "unchanged since I recorded it".  This re-implements
        the *original* derivation — one ``default_rng(sha256(seed:date)[:8])`` per local
        day, ``m`` normal draws, ``p * (1 + e)`` — and requires the shipped function to
        agree exactly.  Together with the digests above it pins both ends: the stream is
        what the old code produced, and it is still that recipe.
        """
        prices = np.linspace(0.02, 0.31, 288)
        days = [
            (date(2025, 6, 15), 0, 96),
            (date(2025, 6, 16), 96, 192),
            (date(2025, 6, 17), 192, 288),
        ]
        reference = prices.copy()
        for label, start, stop in days:
            key = f"{1234}:{label}".encode()
            rng_seed = int.from_bytes(hashlib.sha256(key).digest()[:8], "big")
            noise = np.random.default_rng(rng_seed).normal(0.0, 0.1, size=stop - start)
            reference[start:stop] = prices[start:stop] * (1.0 + noise)

        noisy, _ = generate_price_noise(prices, None, 0.1, seed=1234, day_bounds=days)
        assert np.array_equal(noisy, reference)

    def test_bias_stream_is_the_other_digest_slice(self) -> None:
        """The bias must come from ``digest[8:16]`` of the same key, not a new derivation."""
        prices = np.full(96, 0.2)
        days = [(date(2025, 6, 15), 0, 96)]
        noisy, meta = generate_price_noise(
            prices, None, 0.0, seed=3, bias_sigma=0.15, day_bounds=days
        )
        digest = hashlib.sha256(b"3:2025-06-15").digest()
        expected_bias = float(
            np.random.default_rng(int.from_bytes(digest[8:16], "big")).normal(0.0, 0.15)
        )

        assert meta["per_day"][0]["bias_applied"] == pytest.approx(expected_bias, abs=0.0)
        assert noisy[0] == pytest.approx(0.2 * (1.0 + expected_bias), rel=1e-15, abs=0.0)


class TestPriceNoiseDayBias:
    def test_sigma_zero_bias_only_is_pure_day_scaling(self) -> None:
        """``sigma=0, bias_sigma>0``: the estimate is a per-day scaling of truth.

        ``p_hat/p_true - 1`` must be exactly one constant per local day (no within-day
        distortion at all), and a different constant on another day.
        """
        prices = np.concatenate([np.full(96, 0.10) + 0.001 * np.arange(96), np.full(96, -0.04)])
        days = [(date(2025, 6, 15), 0, 96), (date(2025, 6, 16), 96, 192)]
        noisy, meta = generate_price_noise(
            prices, None, 0.0, seed=3, bias_sigma=0.15, day_bounds=days
        )

        rel = noisy / prices - 1.0
        assert np.all(np.isfinite(rel))
        first_day, second_day = rel[:96], rel[96:]
        assert float(first_day.max() - first_day.min()) == 0.0, "within-day spread must be zero"
        assert float(second_day.max() - second_day.min()) == 0.0
        # Both days are scaled, and not by the same factor (otherwise the bias would be
        # a global constant rather than a per-day draw).
        assert first_day[0] != pytest.approx(second_day[0], abs=1e-12)
        assert abs(float(first_day[0])) > 1e-9 and abs(float(second_day[0])) > 1e-9

        # The realised bias is reported, and it IS the relative deviation drawn above.
        assert meta["sigma"] == 0.0 and meta["bias_sigma"] == 0.15
        assert meta["noise_applied"] is True
        applied = [rec["bias_applied"] for rec in meta["per_day"]]
        assert applied == pytest.approx([float(first_day[0]), float(second_day[0])])
        assert meta["bias_days_applied"] == 2
        assert meta["bias_mean_applied"] == pytest.approx(np.mean(applied))
        assert meta["bias_std_applied"] == pytest.approx(np.std(applied))

    def test_deterministic_same_seed_and_bias_sigma(self) -> None:
        prices = np.linspace(0.1, 0.3, 192)
        days = [(date(2025, 6, 15), 0, 96), (date(2025, 6, 16), 96, 192)]
        kwargs: dict[str, Any] = {"bias_sigma": 0.2, "day_bounds": days}
        noisy1, meta1 = generate_price_noise(prices, None, 0.1, seed=11, **kwargs)
        noisy2, meta2 = generate_price_noise(prices, None, 0.1, seed=11, **kwargs)
        assert np.array_equal(noisy1, noisy2)
        assert meta1["per_day"] == meta2["per_day"]

    def test_bias_only_is_also_deterministic(self) -> None:
        """Determinism holds on the bias-only path too (``sigma=0`` draws no slot noise)."""
        prices = np.full(96, 0.15)
        days = [(date(2025, 6, 15), 0, 96)]
        noisy1, _ = generate_price_noise(prices, None, 0.0, seed=5, bias_sigma=0.3, day_bounds=days)
        noisy2, _ = generate_price_noise(prices, None, 0.0, seed=5, bias_sigma=0.3, day_bounds=days)
        assert np.array_equal(noisy1, noisy2)

    def test_different_seeds_give_different_bias(self) -> None:
        prices = np.full(96, 0.15)
        days = [(date(2025, 6, 15), 0, 96)]
        rel = []
        for seed in (0, 1):
            noisy, meta = generate_price_noise(
                prices, None, 0.0, seed=seed, bias_sigma=0.2, day_bounds=days
            )
            rel.append(float(noisy[0] / prices[0] - 1.0))
            assert meta["bias_sigma"] == 0.2
        assert rel[0] != pytest.approx(rel[1], abs=1e-12)

    def test_bias_differs_across_local_days_for_one_seed(self) -> None:
        """A single seed must not apply one global bias, or the design tests nothing."""
        prices = np.full(3 * 96, 0.15)
        days = [
            (date(2025, 6, 15), 0, 96),
            (date(2025, 6, 16), 96, 192),
            (date(2025, 6, 17), 192, 288),
        ]
        _, meta = generate_price_noise(prices, None, 0.0, seed=0, bias_sigma=0.2, day_bounds=days)
        applied = [rec["bias_applied"] for rec in meta["per_day"]]
        assert len(set(applied)) == 3, f"per-day draws collapsed to {applied}"

    def test_bias_and_slot_noise_are_independent_streams(self) -> None:
        """With both channels on, within-day spread is non-zero: ``e`` still varies.

        If the bias were drawn from the *same* generator as the slot noise, or if a
        day-level term had replaced the per-slot draws, the within-day spread of the
        relative deviation would collapse (or become an order-dependent function of how
        many slots the day has).
        """
        prices = np.full(96, 0.15)
        days = [(date(2025, 6, 15), 0, 96)]
        noisy, meta = generate_price_noise(
            prices, None, 0.1, seed=8, bias_sigma=0.2, day_bounds=days
        )
        rel = noisy / prices - 1.0
        bias = meta["per_day"][0]["bias_applied"]
        within_day_spread = float(rel.max() - rel.min())
        assert within_day_spread > 0.0, "per-slot noise vanished inside the day"
        # The day's mean deviation is the bias; its spread is the slot noise.  They are
        # different numbers of different origins, and neither dominates the other here.
        assert abs(bias) < within_day_spread
        assert float(np.mean(rel)) == pytest.approx(bias, abs=0.05)

    def test_bias_does_not_reuse_the_slot_noise_stream(self) -> None:
        """With a shared slot stream, the biased run is exactly the unbiased one + ``p*b``.

        ``p*(1+b+e)`` versus ``p*(1+e)`` differ by precisely ``p*b``, so if both channels
        draw the same per-slot sequence (the design here), subtracting the reported bias
        term reproduces the unbiased run to floating-point noise.  A bias drawn from the
        *same* generator instead would shift the slot sequence itself and miss this by an
        amount on the order of ``p*e``, orders of magnitude larger than the tolerance.
        """
        prices = np.linspace(0.05, 0.25, 192)
        days = [(date(2025, 6, 15), 0, 96), (date(2025, 6, 16), 96, 192)]
        plain, _ = generate_price_noise(prices, None, 0.1, seed=17, day_bounds=days)
        biased, meta = generate_price_noise(
            prices, None, 0.1, seed=17, bias_sigma=0.25, day_bounds=days
        )
        rebuilt = plain.copy()
        for rec, (start, stop) in zip(meta["per_day"], [(d[1], d[2]) for d in days], strict=True):
            rebuilt[start:stop] = plain[start:stop] + prices[start:stop] * rec["bias_applied"]
        assert np.allclose(biased, rebuilt, rtol=1e-12, atol=1e-15), (
            "biased run is not the unbiased run shifted by its own day bias: the two "
            "channels are sharing one draw sequence"
        )

    def test_bias_preserves_zero_and_negative_prices_without_nan(self) -> None:
        """A multiplicative model keeps ``p_true = 0`` at exactly 0 and never NaNs."""
        prices = np.array([-0.30, -0.05, 0.0, 0.0, 0.02, 0.40])
        days = [(date(2025, 1, 1), 0, prices.size)]
        noisy, meta = generate_price_noise(
            prices, None, 0.1, seed=2, bias_sigma=0.4, day_bounds=days
        )
        assert np.all(np.isfinite(noisy))
        assert noisy[2] == 0.0 and noisy[3] == 0.0
        # A negative price stays on its own side of zero for a bias smaller than 1: no
        # clamping is applied, so only the multiplier acts.
        assert noisy[0] < 0.0 and noisy[1] < 0.0 and noisy[4] > 0.0
        # Zero denominators are excluded from the relative metrics, which therefore stay
        # finite while still reporting how many slots were excluded.
        assert meta["zero_price_count"] == 2
        for key in ("mape", "mae", "frac_within_10pct", "frac_within_20pct"):
            value = meta[key]
            assert value is not None and np.isfinite(value), key

    @pytest.mark.parametrize("bad_bias", [-0.1, float("nan"), float("inf")])
    def test_rejects_an_unusable_bias_sigma(self, bad_bias: float) -> None:
        prices = np.full(96, 0.15)
        days = [(date(2025, 6, 15), 0, 96)]
        with pytest.raises(ValueError, match="bias_sigma"):
            generate_price_noise(prices, None, 0.1, bias_sigma=bad_bias, day_bounds=days)

    def test_no_estimated_slots_reports_zero_bias_without_nan(self) -> None:
        """A mask that estimates nothing must not produce a NaN mean over an empty list."""
        prices = np.full(192, 0.15)
        days = [(date(2025, 6, 15), 0, 96), (date(2025, 6, 16), 96, 192)]
        mask = np.zeros(192, dtype=bool)
        _, meta = generate_price_noise(
            prices, None, 0.1, seed=4, bias_sigma=0.2, day_bounds=days, estimated_slots_mask=mask
        )
        assert meta["per_day"] == []
        assert meta["bias_days_applied"] == 0
        assert meta["bias_mean_applied"] == 0.0
        assert meta["bias_std_applied"] == 0.0
        assert json.dumps(meta, allow_nan=False)  # the runner writes with allow_nan=False

    def test_exact_control_shares_the_noisy_schema(self) -> None:
        """``sigma=0, bias_sigma=0`` keeps every bias key present so rows have one schema."""
        prices = np.full(96, 0.15)
        days = [(date(2025, 6, 15), 0, 96)]
        _, exact = generate_price_noise(prices, None, 0.0, day_bounds=days)
        _, noisy = generate_price_noise(prices, None, 0.1, bias_sigma=0.1, day_bounds=days)
        missing = set(exact) - set(noisy)
        assert not missing, f"schema drift: {missing}"
        assert exact["noise_applied"] is False
        assert exact["bias_sigma"] == 0.0
        assert exact["bias_mean_applied"] == 0.0


# ---------------------------------------------------------------------------
# Frozen load profile
# ---------------------------------------------------------------------------


def _clock_only_grid(
    start_utc: datetime,
    n_intervals: int,
) -> tuple[list[datetime], list[date]]:
    """Interval grid and its local-day labels for a slice starting at *start_utc*."""
    starts = [start_utc + timedelta(seconds=i * QUARTER) for i in range(n_intervals)]
    return starts, [s.astimezone(MADRID).date() for s in starts]


def _load_by_wall_clock(starts: list[datetime], tz: ZoneInfo = MADRID) -> np.ndarray:
    """Load that is a pure function of the local wall clock: slots since local midnight.

    Integer-valued (one unit per slot), so every trailing mean over identical values is
    exact in binary floating point and an assertion like "the forecast equals the truth"
    is a statement about positioning rather than about rounding.

    It makes a sharp fixture for positioning: all local days share one shape *on the
    clock*, so a correctly-positioned slot mean reproduces the truth exactly, while a
    profile that reads position off by *n* slots predicts a value *n* slots away from it —
    visibly, instead of accidentally matching a merely periodic series.  Wall-clock slots
    collide on a DST fall-back day (the repeated hour has one wall time but two elapsed
    positions), so use it only over regions without a transition.
    """
    load = np.empty(len(starts), dtype=float)
    for i, s in enumerate(starts):
        local = s.astimezone(tz)
        load[i] = float(local.hour * 4 + local.minute // 15)
    return load


class TestFrozenLoadProfile:
    def test_uses_only_prior_data(self) -> None:
        """Forecast for day D uses only data strictly before day D."""
        rng = np.random.default_rng(42)
        n_days = 60
        n = n_days * 96
        load = rng.uniform(0.1, 0.5, n)
        usable = np.ones(n, dtype=bool)
        local_days = []
        base = datetime(2025, 1, 1, tzinfo=UTC)
        interval_starts = [base + timedelta(seconds=i * QUARTER) for i in range(n)]
        for i in range(n):
            dt = base + timedelta(seconds=i * QUARTER)
            local_days.append(dt.astimezone(MADRID).date())

        forecast, meta = build_frozen_load_profile(
            load,
            local_days,
            usable,
            training_end_idx=96,
            window_days=28,
            interval_starts_utc=interval_starts,
        )
        assert forecast.size == n
        # The forecast should not be all zeros (except possibly the very first day)
        # Check that later days have non-zero forecasts
        assert np.any(forecast[96:] > 0)

    def test_window_days_respected(self) -> None:
        """Wider window uses more prior days."""
        rng = np.random.default_rng(42)
        n_days = 60
        n = n_days * 96
        load = rng.uniform(0.1, 0.5, n)
        usable = np.ones(n, dtype=bool)
        local_days = []
        base = datetime(2025, 1, 1, tzinfo=UTC)
        interval_starts = [base + timedelta(seconds=i * QUARTER) for i in range(n)]
        for i in range(n):
            dt = base + timedelta(seconds=i * QUARTER)
            local_days.append(dt.astimezone(MADRID).date())

        # Use 30 days of training data (indices 0 to 30*96)
        training_end = 30 * 96
        f28, _ = build_frozen_load_profile(
            load,
            local_days,
            usable,
            training_end_idx=training_end,
            window_days=28,
            interval_starts_utc=interval_starts,
        )
        f7, _ = build_frozen_load_profile(
            load,
            local_days,
            usable,
            training_end_idx=training_end,
            window_days=7,
            interval_starts_utc=interval_starts,
        )
        # Different windows should give different forecasts (at least for some days)
        # because window_days=28 uses more historical data than window_days=7
        assert not np.array_equal(f28, f7)

    def test_eval_load_change_leaves_forecast_unchanged(self) -> None:
        """Strict train-cut check: perturbing data after the cut changes nothing.

        The weak version above only asks for non-zero values, which a profile fitted on
        the test window would also pass.  A frozen profile by definition cannot react to
        anything at or after ``training_end_idx``, so mutating that region and rebuilding
        must reproduce the evaluation forecast bit-for-bit.

        This fixture opens at 01:00 CET, so its local days are truncated at both ends and it
        does already exercise the partial-day path; the strengthened
        :meth:`test_strict_train_cut_holds_on_partial_first_local_day` below targets that
        region specifically, with a hand-derived mean to prove the cut was applied to the
        right index set.
        """
        rng = np.random.default_rng(7)
        n_days = 40
        n = n_days * 96
        load = rng.uniform(0.1, 0.5, n)
        usable = np.ones(n, dtype=bool)
        base = datetime(2025, 1, 1, tzinfo=UTC)
        interval_starts = [base + timedelta(seconds=i * QUARTER) for i in range(n)]
        local_days = [
            (base + timedelta(seconds=i * QUARTER)).astimezone(MADRID).date() for i in range(n)
        ]

        cut = 30 * 96
        forecast, meta = build_frozen_load_profile(
            load,
            local_days,
            usable,
            training_end_idx=cut,
            window_days=28,
            interval_starts_utc=interval_starts,
        )
        perturbed = load.copy()
        perturbed[cut:] = rng.uniform(5.0, 9.0, n - cut)  # absurdly different test-period load
        forecast_p, _ = build_frozen_load_profile(
            perturbed,
            local_days,
            usable,
            training_end_idx=cut,
            window_days=28,
            interval_starts_utc=interval_starts,
        )
        assert np.array_equal(forecast[cut:], forecast_p[cut:]), (
            "the frozen profile moved when only evaluation-period load changed: it is "
            "reading test data"
        )
        # Provenance describes the real window that was learned, not an estimate of it.
        # (The fixture opens at 01:00 CET, so local days do not land on multiples of 96.)
        train_days = contiguous_day_bounds(local_days[:cut])
        assert meta["training_end_idx"] == cut
        assert meta["training_days_used"] == 28
        assert meta["training_start_idx"] == train_days[-28][1]
        assert meta["slot_positions_learned"] == 96

    def test_dst_slot_offsets_fall_back_to_pooled_mean(self) -> None:
        """A fall-back day's extra slot offsets have no learned clock on either side.

        Training on the 96-slot day before the transition and evaluating across it is the
        case a DST day silently breaks: slot position is the offset within its own local
        day, so the long day's slots 96..99 were never learned, and neither can they be
        borrowed from another day without landing on the wrong clock time.  They get the
        pooled mean, and the metadata counts them rather than hiding it.
        """
        start = datetime(2025, 10, 24, 22, 0, tzinfo=UTC)  # == 2025-10-25 00:00 CEST
        _, load, usable, local_days, starts = _make_dataset(start, 5, base_load=0.0)
        rng = np.random.default_rng(11)
        n = load.size
        load = rng.uniform(0.1, 0.5, n)

        first_of_26 = next(i for i, d in enumerate(local_days) if d == date(2025, 10, 26))
        cut = first_of_26  # train on day 25 only (96 slots), evaluate the DST days
        forecast, meta = build_frozen_load_profile(
            load,
            local_days,
            usable,
            training_end_idx=cut,
            window_days=1,
            interval_starts_utc=starts,
        )

        # Offsets 0..95 carry their learned mean on every day, DST included.
        oct26 = np.array([d == date(2025, 10, 26) for d in local_days], dtype=bool)
        assert oct26.sum() == 100
        pooled = meta["pooled_mean_kwh"]
        head = forecast[cut:][oct26[cut:]][:96]
        tail = forecast[cut:][oct26[cut:]][96:]
        assert head.size == 96 and tail.size == 4
        # Per-slot learning really did produce distinct clock values, so "the tail equals
        # the pooled mean" below is a statement about the fallback, not a tautology.  The
        # pooled mean is the average of the learned slots (one training day, one sample
        # per slot), which is exactly the relation asserted here.
        assert np.ptp(head) > 1e-9
        assert pooled == pytest.approx(float(head.mean()))
        assert np.all(tail == pytest.approx(pooled)), (
            "the fall-back day's unlearned offsets did not get the pooled fallback"
        )
        # The four pooled intervals are reported, and they are exactly the unlearned ones.
        assert meta["eval_slots_pooled_fallback"] == 4

    def test_missing_interval_starts_is_refused(self) -> None:
        """No timestamps means no correct positioning, so refuse instead of guessing.

        ``interval_starts_utc`` used to be optional and only fed the training hash; when
        it was absent the slot position silently fell back to the offset within each run
        of the sliced array.  That is what produced the misalignment below, so the silent
        path is gone: an omitted argument raises rather than returning a plausible profile
        whose first partial day is shifted by hours.
        """
        start = datetime(2025, 6, 14, 22, 0, tzinfo=UTC)  # 2025-06-15 00:00 CEST
        starts, local_days = _clock_only_grid(start, 3 * 96)
        load = _load_by_wall_clock(starts)
        usable = np.ones(len(starts), dtype=bool)

        with pytest.raises(ValueError, match="interval_starts_utc is required"):
            build_frozen_load_profile(load, local_days, usable, training_end_idx=2 * 96)

    def test_partial_first_local_day_gets_clock_positions(self) -> None:
        """A slice edge that is not local midnight must not shift the first day's slots.

        This is the regression that real data hit.  The evaluation slice opened at
        ``2025-10-01T00:00:00Z``, which is **02:00 CEST**, so local day 2025-10-01 exists
        in the slice with only its last 88 intervals.  Position was the offset *within that
        run*, so those intervals were labelled 0..87 while their clock says 8..95: every
        forecast on the first partial day was the mean of a slot two hours earlier, and the
        profile shifted by exactly the 8 missing morning slots.

        The fixture reproduces that edge and uses a load that is a pure function of the wall
        clock, so the correct answer is known exactly: interval ``i`` must forecast the value
        of its own clock position.  Eight positions = two hours, asserted numerically.
        """
        # Opens at 00:00 UTC on 2025-10-01 == 02:00 CEST, exactly like the real eval slice.
        start = datetime(2025, 9, 24, 0, 0, tzinfo=UTC)
        starts, local_days = _clock_only_grid(start, 12 * 96)
        load = _load_by_wall_clock(starts)
        usable = np.ones(len(starts), dtype=bool)

        # The cut is the first interval of the evaluation slice: 2025-10-01T00:00Z.
        cut = next(i for i, s in enumerate(starts) if s == datetime(2025, 10, 1, tzinfo=UTC))
        assert local_days[cut] == date(2025, 10, 1), "fixture must open inside a local day"

        # How truncated the first local day is, measured independently of the code:
        # midnight of that local day sits 8 intervals before the cut.
        local_midnight = datetime(2025, 10, 1, 0, 0, tzinfo=MADRID).astimezone(UTC)
        expected_shift = round((starts[cut] - local_midnight).total_seconds() / QUARTER)
        assert expected_shift == 8, "fixture drifted: 2025-10-01T00:00Z is 02:00 CEST"

        forecast, meta = build_frozen_load_profile(
            load,
            local_days,
            usable,
            training_end_idx=cut,
            window_days=7,
            interval_starts_utc=starts,
        )

        # Positions 8..11, not 0..3 — the exact before/after pair for this defect.
        assert list(forecast[cut : cut + 4]) == [8.0, 9.0, 10.0, 11.0], (
            "the first partial local day was positioned from its offset within the slice "
            f"run (would give {list(np.arange(4, dtype=float))}), not from the clock"
        )

        # The shift is exactly 8 positions across the whole partial day: what the old rule
        # assigned (0..87) is this array shifted by 8, so asserting the unshifted form pins
        # both the magnitude and that nothing else moved.
        first_run_len = 96 - expected_shift
        assert list(forecast[cut : cut + first_run_len]) == [
            float(k) for k in range(expected_shift, 96)
        ]

        # And every later evaluation interval is right too: with a wall-clock-only load a
        # correctly positioned slot mean is exact, so this fails loudly on any offset error.
        assert np.array_equal(forecast[cut:], load[cut:]), (
            "positions past the first day drifted as well; the clock rule must be uniform"
        )
        assert meta["eval_slots_pooled_fallback"] == 0, "no DST here, so nothing may fall back"
        # The training side is positioned by the same rule, so it too is exact.
        assert np.array_equal(forecast[:cut], load[:cut])

    def test_trailing_partial_local_day_is_not_shifted(self) -> None:
        """Pin the trailing edge as CORRECT, so it is not "fixed" later.

        A previous analysis claimed the end of the evaluation window was misaligned by 92
        positions too.  It is not.  The last interval of the real dataset is
        ``2025-12-31T23:00:00Z`` = **2026-01-01 00:00 CET**, whose true slot position is 0 —
        and a run-based offset also gives 0 there, because the trailing partial day *starts*
        at the slice edge rather than ending mid-way through it.  Both rules agree, which is
        why this boundary was never broken.

        Asserting it explicitly costs nothing and prevents a well-meaning future "fix" from
        shifting a boundary that is already right.
        """
        # Ends at 2025-12-31T23:45Z, so local day 2026-01-01 exists with only 4 intervals:
        # the mirror image of the leading-edge case, at the other end of the array.
        # Opens at local midnight (22:00Z == 00:00 CET on the 17th) so the 15 interior days
        # are whole and only the final day is truncated.
        start = datetime(2025, 12, 16, 23, 0, tzinfo=UTC)
        starts, local_days = _clock_only_grid(start, 15 * 96 + 4)
        load = _load_by_wall_clock(starts)
        usable = np.ones(len(starts), dtype=bool)

        trailing = [i for i, d in enumerate(local_days) if d == date(2026, 1, 1)]
        assert len(trailing) == 4, "trailing day must be the partial one"
        assert starts[trailing[0]] == datetime(2025, 12, 31, 23, 0, tzinfo=UTC)
        # Cut mid-local-day (07:00 CET on the 29th), so the evaluation region carries a
        # partial leading day AND this trailing one — both slice edges in a single call.
        cut = next(i for i, s in enumerate(starts) if s == datetime(2025, 12, 29, 6, 0, tzinfo=UTC))

        forecast, meta = build_frozen_load_profile(
            load,
            local_days,
            usable,
            training_end_idx=cut,
            window_days=7,
            interval_starts_utc=starts,
        )

        # True clock positions 0..3 (local midnight is exactly at starts[trailing[0]]).
        assert list(forecast[trailing]) == [0.0, 1.0, 2.0, 3.0]
        # Explicitly NOT the alleged 92-position shift.
        assert list(forecast[trailing]) != [92.0, 93.0, 94.0, 95.0]
        assert np.array_equal(forecast[cut:], load[cut:])
        assert meta["eval_slots_pooled_fallback"] == 0

    def test_strict_train_cut_holds_on_partial_first_local_day(self) -> None:
        """The train cut must stay exact in the region where the positioning bug lived.

        ``test_eval_load_change_leaves_forecast_unchanged`` uses a fixture whose first day is
        already partial, but it only checks the aggregate array.  Rewriting positions means a
        post-cutoff interval could now be *averaged into* a pre-cutoff slot mean if the cut
        were applied to the wrong quantity.  So: perturb every single post-cutoff load value,
        on a slice opening mid-local-day, and require bit-identical output everywhere —
        including inside the shifted first run, where the bug actually was.
        """
        start = datetime(2025, 9, 24, 0, 0, tzinfo=UTC)
        starts, local_days = _clock_only_grid(start, 12 * 96)
        rng = np.random.default_rng(23)
        load = rng.uniform(0.1, 0.5, len(starts))
        usable = np.ones(len(starts), dtype=bool)

        cut = next(i for i, s in enumerate(starts) if s == datetime(2025, 10, 1, tzinfo=UTC))
        baseline, meta = build_frozen_load_profile(
            load,
            local_days,
            usable,
            training_end_idx=cut,
            window_days=7,
            interval_starts_utc=starts,
        )

        perturbed = load.copy()
        perturbed[cut:] = rng.uniform(50.0, 90.0, len(starts) - cut)
        after, _ = build_frozen_load_profile(
            perturbed,
            local_days,
            usable,
            training_end_idx=cut,
            window_days=7,
            interval_starts_utc=starts,
        )

        assert np.array_equal(baseline, after), (
            "evaluation-period load leaked into the profile on a slice that opens at a "
            "non-midnight boundary"
        )
        # Non-vacuity: the perturbed region really does cover the whole partial first day.
        assert cut < cut + 8 <= len(starts)
        assert np.any(perturbed[cut : cut + 88] != load[cut : cut + 88])

        # The strict means come only from indices < training_end_idx, and only from the
        # trailing window that was actually learned.  Re-derive position 8's mean by hand
        # over exactly those indices and require the profile to agree with THAT rather than
        # with any value at or after the cut — a leak through the rewritten positioning
        # would move this number, since position 8 now lives inside the first partial day.
        train_days = contiguous_day_bounds(list(local_days[:cut]))
        window_start = train_days[-min(7, len(train_days))][1]
        assert window_start == meta["training_start_idx"] < cut
        positions = [_clock_slot_position(s, MADRID, QUARTER) for s in starts]
        members = [i for i in range(window_start, cut) if positions[i] == 8]
        assert members and members[-1] < cut, "the hand-derived mean must be pre-cutoff only"
        expected_mean_8 = float(np.mean([load[i] for i in members]))
        # Non-vacuity again: post-cutoff intervals share position 8, so including them (a
        # leak) would give a different number.
        post_members = [i for i in range(cut, len(starts)) if positions[i] == 8]
        assert post_members
        leaked = float(np.mean([load[i] for i in members + post_members]))
        assert expected_mean_8 != pytest.approx(leaked, abs=1e-9)
        assert baseline[cut] == pytest.approx(expected_mean_8, rel=0.0, abs=1e-12)
        assert meta["training_end_idx"] == cut

    def test_dst_fall_back_clock_positions_run_zero_to_ninety_nine(self) -> None:
        """The 2025-10-26 fall-back day must yield positions 0..99, repeated hour included.

        Stated convention: position is measured as elapsed intervals since the **fold=0**
        local midnight (the first instant whose local date is that day), and each UTC instant
        maps to exactly one position.  The repeated 02:00 hour therefore gets two positions —
        02:00 CEST is 8, 02:00 CET an hour later in real time is 12 — because position counts
        real elapsed time, not wall-clock readings.

        This matters for correctness of the means too: computing "offset from local midnight"
        as ``local_dt - local_dt.replace(hour=0)`` keeps the current utcoffset, so after the
        fall-back it re-anchors to 01:00 CET and pushes the whole post-transition afternoon
        back by an hour — collapsing the 25-hour day onto 0..95, duplicating 8..11 and quietly
        serving those slots' evening means at the wrong clock time.  Asserting 0..99 rules that
        out, and so does the ordering check below.
        """
        _, load, usable, local_days, starts = _dst_fall_back_window()
        oct26 = [i for i, d in enumerate(local_days) if d == date(2025, 10, 26)]
        assert len(oct26) == 100

        positions = [_clock_slot_position(starts[i], MADRID, QUARTER) for i in oct26]
        assert positions == list(range(100)), (
            f"fall-back day positions are not the contiguous 0..99: {positions}"
        )

        # The repeated hour, one position per real instant rather than one per reading.
        first_0200 = datetime(2025, 10, 26, 2, 0, tzinfo=MADRID, fold=0)
        second_0200 = datetime(2025, 10, 26, 2, 0, tzinfo=MADRID, fold=1)
        assert _clock_slot_position(first_0200.astimezone(UTC), MADRID, QUARTER) == 8
        assert _clock_slot_position(second_0200.astimezone(UTC), MADRID, QUARTER) == 12

        # Behaviour through the public API: encode position into the training day's load so
        # each learned mean IS its position, then check the long day comes out ordered.
        cut = oct26[0]
        shaped = _load_by_wall_clock(starts)
        forecast, meta = build_frozen_load_profile(
            shaped,
            local_days,
            usable,
            training_end_idx=cut,
            window_days=1,
            interval_starts_utc=starts,
        )
        pooled = meta["pooled_mean_kwh"]
        # 0..95 keep their own clock mean; only the four offsets no normal day reaches (the
        # extra hour's tail) fall back.  The repeated hour is NOT among them.
        assert list(forecast[oct26]) == [float(k) for k in range(96)] + [pooled] * 4
        assert meta["eval_slots_pooled_fallback"] == 4

    def test_dst_spring_forward_clock_positions_run_zero_to_ninety_one(self) -> None:
        """The 2026-03-29 spring-forward day is 92 intervals and must run 0..91.

        Pinning the short direction of the same transition, because it shows what the chosen
        convention really counts: positions are **elapsed intervals since the local midnight
        instant**, so the skipped 02:00 hour leaves no hole (positions stay contiguous) and
        nothing needs the pooled fallback — a 92-slot day is a prefix of a 96-slot one.

        The consequence, stated rather than left to be discovered: on that day position 8 is
        wall-clock 03:00 CEST while position 8 on an ordinary day is 02:00, so the afternoon
        means are applied one hour early in wall-clock terms.  That is unavoidable for any
        contiguous-position scheme and it is what this function has always done — offsets
        within a full local day give exactly these numbers — so no behaviour changes here;
        only truncated days move, which is the defect being fixed.
        """
        # 2026-03-27T23:00Z == 2026-03-28 00:00 CET, so day 29 is whole and 23 h long.
        start = datetime(2026, 3, 27, 23, 0, tzinfo=UTC)
        starts, local_days = _clock_only_grid(start, 5 * 96)
        usable = np.ones(len(starts), dtype=bool)

        day29 = [i for i, d in enumerate(local_days) if d == date(2026, 3, 29)]
        assert len(day29) == 92
        # The skipped hour really is absent from the wall-clock readings of that day.
        assert 2 not in {starts[i].astimezone(MADRID).hour for i in day29}

        positions = [_clock_slot_position(starts[i], MADRID, QUARTER) for i in day29]
        assert positions == list(range(92))

        # Behaviour: trained on the preceding normal day, whose load is its own position, so
        # the short day must come back as 0..91 with no fallback at all.
        cut = day29[0]
        shaped = np.arange(len(starts), dtype=float)
        forecast, meta = build_frozen_load_profile(
            shaped,
            local_days,
            usable,
            training_end_idx=cut,
            window_days=1,
            interval_starts_utc=starts,
        )
        assert list(forecast[day29]) == [float(k) for k in range(92)]
        assert meta["eval_slots_pooled_fallback"] == 0

    def test_full_local_day_positions_are_unchanged_from_offset_within_run(self) -> None:
        """Only truncated days may move: a whole local day keeps its old offsets.

        The fix changes position from "offset within the run of the sliced array" to
        "elapsed intervals since the local midnight instant".  For a day that is present in
        full, those are the same numbers by construction — and they must stay the same, or
        this would silently re-index every learned mean in the whole experiment rather than
        repair the partial-day boundary.  DST days are included: a whole 100- or 92-slot day
        keeps offsets 0..99 / 0..91 under both rules.
        """
        # The fall-back weekend from local midnight: 25th (96) + 26th (100) + 27th (96),
        # sized so that all three local days are present in full and none is a slice edge.
        start = datetime(2025, 10, 24, 22, 0, tzinfo=UTC)  # 2025-10-25 00:00 CEST
        starts, local_days = _clock_only_grid(start, 96 + 100 + 96)

        day_bounds = contiguous_day_bounds(list(local_days))
        assert [hi - lo for _l, lo, hi in day_bounds] == [96, 100, 96], (
            "fixture drifted: the whole-day premise needs one normal day either side of "
            "the 25-hour fall-back day"
        )
        for label, lo, hi in day_bounds:
            del label
            offset_positions = list(range(hi - lo))
            clock_positions = [
                _clock_slot_position(starts[i], MADRID, QUARTER) for i in range(lo, hi)
            ]
            assert clock_positions == offset_positions, (
                f"local day {local_days[lo]} re-indexed under the clock rule: "
                f"{clock_positions[:8]} vs {offset_positions[:8]}"
            )


# ---------------------------------------------------------------------------
# Event computation
# ---------------------------------------------------------------------------


class TestComputeEvents:
    def test_basic_two_events_per_day(self) -> None:
        """Normal day: 14:00 and 20:30 events."""
        start = datetime(2025, 6, 15, 0, 0, tzinfo=UTC)  # CEST = UTC+2
        prices, load, usable, local_days, starts = _make_dataset(start, 3)
        # 14:00 CEST = 12:00 UTC, 20:30 CEST = 18:30 UTC
        eval_start = 0
        eval_end = len(starts)

        events = compute_rolling_events(
            starts,
            local_days,
            eval_start_idx=eval_start,
            eval_end_idx=eval_end,
            tz=MADRID,
        )
        # Should have warmup + 2 events per day for 3 days = 1 + 6 = 7
        kinds = [e.kind for e in events]
        assert kinds[0] == "warmup"
        assert kinds.count("afternoon") == 3
        assert kinds.count("evening") == 3

    def test_warmup_only_current_day_prices(self) -> None:
        """Warmup event plans only current day, no tomorrow."""
        start = datetime(2025, 6, 15, 0, 0, tzinfo=UTC)
        _, _, _, local_days, starts = _make_dataset(start, 2)

        events = compute_rolling_events(
            starts,
            local_days,
            eval_start_idx=0,
            eval_end_idx=len(starts),
            tz=MADRID,
        )
        warmup = events[0]
        assert warmup.kind == "warmup"
        # Warmup prices_exact_end == plan_end (all exact, no noisy portion)
        assert warmup.prices_exact_end_idx == warmup.plan_end_idx

    def test_afternoon_has_noisy_portion(self) -> None:
        """Afternoon event: exact for rest of D, noisy for D+1."""
        start = datetime(2025, 6, 15, 0, 0, tzinfo=UTC)
        _, _, _, local_days, starts = _make_dataset(start, 3)

        events = compute_rolling_events(
            starts,
            local_days,
            eval_start_idx=0,
            eval_end_idx=len(starts),
            tz=MADRID,
        )
        afternoon = [e for e in events if e.kind == "afternoon"][0]
        # prices_exact_end should be before plan_end (there's a noisy portion)
        assert afternoon.prices_exact_end_idx < afternoon.plan_end_idx

    def test_evening_all_exact(self) -> None:
        """Evening event: all prices exact (D+1 now available)."""
        start = datetime(2025, 6, 15, 0, 0, tzinfo=UTC)
        _, _, _, local_days, starts = _make_dataset(start, 3)

        events = compute_rolling_events(
            starts,
            local_days,
            eval_start_idx=0,
            eval_end_idx=len(starts),
            tz=MADRID,
        )
        evening = [e for e in events if e.kind == "evening"][0]
        assert evening.prices_exact_end_idx == evening.plan_end_idx

    def test_each_interval_executed_once(self) -> None:
        """Execution windows tile the evaluation window without gaps or overlaps."""
        start = datetime(2025, 6, 15, 0, 0, tzinfo=UTC)
        _, _, _, local_days, starts = _make_dataset(start, 5)

        events = compute_rolling_events(
            starts,
            local_days,
            eval_start_idx=0,
            eval_end_idx=len(starts),
            tz=MADRID,
        )
        # Check that execution windows tile [0, eval_end)
        covered = set()
        for e in events:
            for i in range(e.start_idx, e.exec_end_idx):
                assert i not in covered, f"interval {i} executed twice"
                covered.add(i)
        assert covered == set(range(len(starts)))

    def test_no_warmup_when_eval_starts_after_14(self) -> None:
        """If eval starts after 14:00, no warmup needed."""
        start = datetime(2025, 6, 15, 0, 0, tzinfo=UTC)
        _, _, _, local_days, starts = _make_dataset(start, 3)
        # 14:00 CEST = 12:00 UTC = interval 48
        eval_start = 48  # start at 14:00 local

        events = compute_rolling_events(
            starts,
            local_days,
            eval_start_idx=eval_start,
            eval_end_idx=len(starts),
            tz=MADRID,
        )
        kinds = [e.kind for e in events]
        # First event should be evening (20:30), not warmup
        assert kinds[0] in ("afternoon", "evening")


def _assert_events_tile(
    events: list[Any],
    eval_start_idx: int,
    eval_end_idx: int,
) -> None:
    """Shared invariant check for every event-computation test.

    Each interval in the window is executed exactly once, no event executes past its
    own plan, prices are never exact past a plan's end, and indices are sequential.
    """
    assert [e.index for e in events] == list(range(len(events))), "event indices must be 0..n-1"
    coverage = np.zeros(eval_end_idx, dtype=int)
    prev_stop = eval_start_idx
    for e in events:
        assert e.start_idx == prev_stop, (
            f"event {e.index} ({e.kind}) starts at {e.start_idx}, previous execution "
            f"stopped at {prev_stop}: the roll would skip or repeat intervals"
        )
        assert e.start_idx < e.exec_end_idx, f"event {e.index} ({e.kind}) executes nothing"
        assert e.exec_end_idx <= e.plan_end_idx, (
            f"event {e.index} ({e.kind}) executes [{e.start_idx}, {e.exec_end_idx}) from a "
            f"plan ending at {e.plan_end_idx}: those intervals have no plan"
        )
        assert e.prices_exact_end_idx <= e.plan_end_idx, (
            f"event {e.index} ({e.kind}) marks prices exact past its own plan end"
        )
        coverage[e.start_idx : e.exec_end_idx] += 1
        prev_stop = e.exec_end_idx
    assert prev_stop == eval_end_idx, (
        f"execution stops at {prev_stop}, window ends at {eval_end_idx}: "
        f"{eval_end_idx - prev_stop} intervals are left unexecuted"
    )
    assert np.all(coverage[eval_start_idx:eval_end_idx] == 1), "gap or overlap in coverage"


class TestWarmupSplitting:
    """Warmups cover what the window opens with, but never plan past a local midnight.

    This is where the two requirements collide: an evaluation window that opens after
    the 20:30 event must still be executed until the next day's 14:00, yet a warmup may
    not plan (let alone price) tomorrow.  Splitting at midnight satisfies both; clamping
    the execution to the current day instead would have left the rest of that night with
    no plan at all.
    """

    @staticmethod
    def _local_idx(starts: list[datetime], local: datetime) -> int:
        target = local.astimezone(UTC)
        return next(i for i, s in enumerate(starts) if s >= target)

    def test_start_after_evening_event_splits_at_midnight(self) -> None:
        """A 21:00 start (after both daily events) yields one warmup per night day."""
        start = datetime(2025, 6, 15, 0, 0, tzinfo=UTC)
        _, _, _, local_days, starts = _make_dataset(start, 3)
        eval_start = self._local_idx(starts, datetime(2025, 6, 15, 21, 0, tzinfo=MADRID))

        events = compute_rolling_events(
            starts,
            local_days,
            eval_start_idx=eval_start,
            eval_end_idx=len(starts),
            tz=MADRID,
        )

        warmups = [e for e in events if e.kind == "warmup"]
        assert [e.local_day for e in warmups] == [date(2025, 6, 15), date(2025, 6, 16)]
        # The first stops exactly at local midnight; the second runs to the 14:00 event.
        # (The fixture opens at 02:00 CEST, so the first midnight is index 88, not 96.)
        midnight = next(i for i, d in enumerate(local_days) if d == date(2025, 6, 16))
        assert warmups[0].start_idx == eval_start
        assert warmups[0].plan_end_idx == midnight
        assert warmups[0].exec_end_idx == midnight
        assert warmups[1].start_idx == midnight
        assert warmups[1].plan_end_idx == self._local_idx(
            starts, datetime(2025, 6, 16, 14, 0, tzinfo=MADRID)
        )
        assert warmups[1].exec_end_idx == warmups[1].plan_end_idx

        # Neither warmup plans past its own local day, so neither has tomorrow's prices.
        day_end = {label: stop for label, _, stop in contiguous_day_bounds(list(local_days))}
        for w in warmups:
            assert w.prices_exact_end_idx == w.plan_end_idx
            assert w.plan_end_idx <= day_end[w.local_day], (
                f"warmup on {w.local_day} plans to {w.plan_end_idx}, past its local "
                f"midnight at {day_end[w.local_day]}"
            )

        _assert_events_tile(events, eval_start, len(starts))

    def test_short_window_with_no_events_is_still_executed(self) -> None:
        """A window between two events (no event inside it) must not vanish."""
        start = datetime(2025, 6, 15, 0, 0, tzinfo=UTC)
        _, _, _, local_days, starts = _make_dataset(start, 3)
        # 09:00 → 13:00 local on the first day: no 14:00 or 20:30 boundary inside.
        lo = self._local_idx(starts, datetime(2025, 6, 15, 9, 0, tzinfo=MADRID))
        hi = self._local_idx(starts, datetime(2025, 6, 15, 13, 0, tzinfo=MADRID))

        events = compute_rolling_events(
            starts,
            local_days,
            eval_start_idx=lo,
            eval_end_idx=hi,
            tz=MADRID,
        )

        assert len(events) == 1
        assert events[0].kind == "warmup"
        _assert_events_tile(events, lo, hi)

    def test_warmup_spanning_several_midnights(self) -> None:
        """No event on the grid at all: warmups tile the window across every midnight.

        A misaligned interval grid (starts :05 past the hour) means neither 14:00 nor
        20:30 exists as an interval start, so nothing is scheduled inside the window.
        The fallback must still execute it — one exact-price warmup per local day —
        rather than return no events and leave the whole window unexecuted.
        """
        # 00:05 local on each day, so wall-clock event times never land on a boundary.
        start = datetime(2025, 6, 14, 22, 5, tzinfo=UTC)  # == 2025-06-15 00:05 CEST
        _, _, _, local_days, starts = _make_dataset(start, 4)
        lo = self._local_idx(starts, datetime(2025, 6, 15, 23, 0, tzinfo=MADRID))
        hi = self._local_idx(starts, datetime(2025, 6, 18, 1, 0, tzinfo=MADRID))

        events = compute_rolling_events(
            starts,
            local_days,
            eval_start_idx=lo,
            eval_end_idx=hi,
            tz=MADRID,
        )

        assert all(e.kind == "warmup" for e in events), [e.kind for e in events]
        assert len(events) == 4  # the tail of the 15th, two whole days, head of the 18th
        assert [e.local_day for e in events] == [
            date(2025, 6, 15),
            date(2025, 6, 16),
            date(2025, 6, 17),
            date(2025, 6, 18),
        ]
        # Interior warmups are whole local days, and none reaches into the next one.
        assert [e.plan_end_idx for e in events[1:3]] == [events[2].start_idx, events[3].start_idx]
        _assert_events_tile(events, lo, hi)

    def test_missing_local_day_continues_instead_of_clamping(self) -> None:
        """A day missing from the data must not be papered over by clamping execution.

        With a two-day hole (17th and 18th absent), the 16th's 20:30 plan cannot reach a
        real D+1 midnight, while the next scheduled event sits on the 19th — past that
        plan.  Clamping execution to the plan would silently drop the 19th's morning; the
        dispatcher instead continues with a day-only warmup, so coverage stays exact.
        """
        origin = datetime(2025, 6, 14, 22, 0, tzinfo=UTC)  # == 2025-06-15 00:00 CEST
        starts: list[datetime] = []
        for offset in (0, 1, 4, 5):  # Jun 15, 16, then a hole, then Jun 19, 20
            day_start = origin + timedelta(days=offset)
            starts += [day_start + timedelta(seconds=i * QUARTER) for i in range(96)]
        local_days = [s.astimezone(MADRID).date() for s in starts]
        hole_end = 2 * 96  # first interval after the hole (local midnight of Jun 19)

        events = compute_rolling_events(
            starts,
            local_days,
            eval_start_idx=0,
            eval_end_idx=len(starts),
            tz=MADRID,
        )

        before_hole = [e for e in events if e.start_idx < hole_end]
        last_before = before_hole[-1]
        assert last_before.kind == "evening"
        assert last_before.plan_end_idx <= hole_end
        continuation = [e for e in events if e.start_idx == hole_end]
        assert len(continuation) == 1
        assert continuation[0].kind == "warmup"
        # Day-only plan with exact prices: the continuation has no tomorrow either.
        assert continuation[0].prices_exact_end_idx == continuation[0].plan_end_idx
        _assert_events_tile(events, 0, len(starts))

    def test_run_rolling_horizon_after_evening_event(self) -> None:
        """End-to-end: a window that opens at 21:00 local dispatches without crashing.

        The dispatcher used to build a warmup whose execution ran past its own plan
        here, and the clipped-plan slice died on a broadcast error.
        """
        # Opens at 2025-06-15 00:00 CEST so local days land on multiples of 96.
        start = datetime(2025, 6, 14, 22, 0, tzinfo=UTC)
        prices, load, usable, local_days, starts = _make_dataset(start, 4, base_load=0.2)
        for i in range(len(prices)):
            hour = (i % 96) // 4
            prices[i] = 0.05 if hour < 6 else 0.25 if 18 <= hour < 22 else 0.12

        frozen_load, _ = build_frozen_load_profile(
            load,
            local_days,
            usable,
            training_end_idx=96,
            window_days=28,
            interval_starts_utc=starts,
        )
        # Evaluate from 21:00 local on the third local day: after both events that day,
        # so the roll must warm up through the night before its first scheduled event.
        eval_start = 2 * 96 + 84
        assert local_days[eval_start] == date(2025, 6, 17)
        assert frozen_load[eval_start] > 0, "the window opened inside the training region"

        result = run_rolling_horizon(
            _spec(),
            prices=prices,
            load_kwh=load,
            usable=usable,
            interval_seconds=QUARTER,
            local_days=local_days,
            interval_starts_utc=starts,
            eval_start_idx=eval_start,
            eval_end_idx=len(starts),
            tz=MADRID,
            frozen_load=frozen_load,
            terminal_value_eur_per_kwh=0.10,
            sigma=0.1,
            seed=3,
        )

        n_eval = len(starts) - eval_start
        covered = np.zeros(n_eval, dtype=int)
        for rec in result.per_event:
            covered[rec["start_idx"] - eval_start : rec["exec_end_idx"] - eval_start] += 1
        assert np.all(covered == 1), (
            f"post-20:30 coverage violation: {int((covered == 0).sum())} gaps, "
            f"{int((covered > 1).sum())} overlaps"
        )
        # The night was dispatched by warmups, and they were charged exact prices.
        warmup_recs = [r for r in result.per_event if r["kind"] == "warmup"]
        assert len(warmup_recs) == 2
        assert all(r["solver_status"] != "not_solved" for r in warmup_recs)
        assert result.schedule.grid_kwh.size == n_eval


# ---------------------------------------------------------------------------
# Rolling horizon integration
# ---------------------------------------------------------------------------


class TestRollingHorizon:
    def _simple_setup(self, n_days: int = 5):
        start = datetime(2025, 6, 15, 0, 0, tzinfo=UTC)
        prices, load, usable, local_days, starts = _make_dataset(
            start,
            n_days,
            base_price=0.15,
            base_load=0.2,
        )
        # Add some price variation so the solver has something to optimise
        for i in range(len(prices)):
            hour = (i % 96) // 4
            if 0 <= hour < 6:
                prices[i] = 0.05  # cheap at night
            elif 6 <= hour < 10:
                prices[i] = 0.20  # expensive morning
            elif 10 <= hour < 14:
                prices[i] = 0.12  # moderate
            elif 14 <= hour < 18:
                prices[i] = 0.08  # cheap afternoon (solar)
            elif 18 <= hour < 22:
                prices[i] = 0.25  # expensive evening
            else:
                prices[i] = 0.06  # cheap night

        spec = _spec()
        frozen_load, _ = build_frozen_load_profile(
            load,
            local_days,
            usable,
            training_end_idx=96,
            window_days=28,
            interval_starts_utc=starts,
        )
        return spec, prices, load, usable, local_days, starts, frozen_load

    def test_deterministic_same_seed(self) -> None:
        """Same seed → identical result."""
        spec, prices, load, usable, local_days, starts, frozen = self._simple_setup()
        r1 = run_rolling_horizon(
            spec,
            prices=prices,
            load_kwh=load,
            usable=usable,
            interval_seconds=QUARTER,
            local_days=local_days,
            interval_starts_utc=starts,
            eval_start_idx=0,
            eval_end_idx=len(starts),
            tz=MADRID,
            frozen_load=frozen,
            terminal_value_eur_per_kwh=0.10,
            sigma=0.1,
            seed=42,
        )
        r2 = run_rolling_horizon(
            spec,
            prices=prices,
            load_kwh=load,
            usable=usable,
            interval_seconds=QUARTER,
            local_days=local_days,
            interval_starts_utc=starts,
            eval_start_idx=0,
            eval_end_idx=len(starts),
            tz=MADRID,
            frozen_load=frozen,
            terminal_value_eur_per_kwh=0.10,
            sigma=0.1,
            seed=42,
        )
        assert r1.cost_eur == r2.cost_eur
        assert np.array_equal(r1.schedule.charge_kwh, r2.schedule.charge_kwh)

    def test_different_seeds_differ(self) -> None:
        """Different seeds → different plans (noise changes the solve)."""
        spec, prices, load, usable, local_days, starts, frozen = self._simple_setup()
        r1 = run_rolling_horizon(
            spec,
            prices=prices,
            load_kwh=load,
            usable=usable,
            interval_seconds=QUARTER,
            local_days=local_days,
            interval_starts_utc=starts,
            eval_start_idx=0,
            eval_end_idx=len(starts),
            tz=MADRID,
            frozen_load=frozen,
            terminal_value_eur_per_kwh=0.10,
            sigma=0.2,
            seed=0,
        )
        r2 = run_rolling_horizon(
            spec,
            prices=prices,
            load_kwh=load,
            usable=usable,
            interval_seconds=QUARTER,
            local_days=local_days,
            interval_starts_utc=starts,
            eval_start_idx=0,
            eval_end_idx=len(starts),
            tz=MADRID,
            frozen_load=frozen,
            terminal_value_eur_per_kwh=0.10,
            sigma=0.2,
            seed=1,
        )
        # Different noise → different plans → different costs
        # (not guaranteed to differ in cost, but plans should differ)
        assert not np.array_equal(r1.schedule.charge_kwh, r2.schedule.charge_kwh)

    def test_cost_uses_true_prices(self) -> None:
        """Scoring cost is dot(true_prices, actual_grid), not noisy prices."""
        spec, prices, load, usable, local_days, starts, frozen = self._simple_setup()
        result = run_rolling_horizon(
            spec,
            prices=prices,
            load_kwh=load,
            usable=usable,
            interval_seconds=QUARTER,
            local_days=local_days,
            interval_starts_utc=starts,
            eval_start_idx=0,
            eval_end_idx=len(starts),
            tz=MADRID,
            frozen_load=frozen,
            terminal_value_eur_per_kwh=0.10,
            sigma=0.2,
            seed=0,
        )
        # Recompute cost from schedule and true prices
        grid = result.schedule.grid_kwh
        true_p = prices[0 : len(starts)]
        expected_cost = float(np.dot(true_p[usable], grid[usable]))
        assert result.cost_eur == pytest.approx(expected_cost, rel=1e-6)

    def test_soc_never_below_floor(self) -> None:
        """SOC stays within the physical window throughout the roll."""
        spec, prices, load, usable, local_days, starts, frozen = self._simple_setup()
        result = run_rolling_horizon(
            spec,
            prices=prices,
            load_kwh=load,
            usable=usable,
            interval_seconds=QUARTER,
            local_days=local_days,
            interval_starts_utc=starts,
            eval_start_idx=0,
            eval_end_idx=len(starts),
            tz=MADRID,
            frozen_load=frozen,
            terminal_value_eur_per_kwh=0.10,
            sigma=0.1,
            seed=0,
        )
        soc = result.schedule.soc_kwh
        floor = spec.soc_bound_kwh("min")
        ceil = spec.soc_bound_kwh("max")
        assert np.all(soc >= floor - 1e-9)
        assert np.all(soc <= ceil + 1e-9)

    def test_no_simultaneous_charge_discharge(self) -> None:
        """Binary mode: charge and discharge never both positive."""
        spec, prices, load, usable, local_days, starts, frozen = self._simple_setup()
        result = run_rolling_horizon(
            spec,
            prices=prices,
            load_kwh=load,
            usable=usable,
            interval_seconds=QUARTER,
            local_days=local_days,
            interval_starts_utc=starts,
            eval_start_idx=0,
            eval_end_idx=len(starts),
            tz=MADRID,
            frozen_load=frozen,
            terminal_value_eur_per_kwh=0.10,
            sigma=0.1,
            seed=0,
        )
        c = result.schedule.charge_kwh
        d = result.schedule.discharge_kwh
        assert not np.any((c > 1e-9) & (d > 1e-9))

    def test_grid_non_negative(self) -> None:
        """No intentional export: grid >= 0."""
        spec, prices, load, usable, local_days, starts, frozen = self._simple_setup()
        result = run_rolling_horizon(
            spec,
            prices=prices,
            load_kwh=load,
            usable=usable,
            interval_seconds=QUARTER,
            local_days=local_days,
            interval_starts_utc=starts,
            eval_start_idx=0,
            eval_end_idx=len(starts),
            tz=MADRID,
            frozen_load=frozen,
            terminal_value_eur_per_kwh=0.10,
            sigma=0.1,
            seed=0,
        )
        assert np.all(result.schedule.grid_kwh >= -1e-9)

    def test_solver_failure_zero_flows(self, monkeypatch) -> None:
        """If the solver fails, that event gets zero flows, not substitution."""
        from energy_baseline import rolling_horizon

        spec, prices, load, usable, local_days, starts, frozen = self._simple_setup()

        # Monkeypatch solve_oracle to raise OracleError on first call
        original_solve = rolling_horizon.solve_oracle
        call_count = [0]

        def failing_solve(*args, **kwargs):
            call_count[0] += 1
            if call_count[0] == 1:
                from energy_baseline.oracle import OracleError

                raise OracleError("Simulated solver failure")
            return original_solve(*args, **kwargs)

        monkeypatch.setattr(rolling_horizon, "solve_oracle", failing_solve)

        result = run_rolling_horizon(
            spec,
            prices=prices,
            load_kwh=load,
            usable=usable,
            interval_seconds=QUARTER,
            local_days=local_days,
            interval_starts_utc=starts,
            eval_start_idx=0,
            eval_end_idx=len(starts),
            tz=MADRID,
            frozen_load=frozen,
            terminal_value_eur_per_kwh=0.10,
            sigma=0.0,
            seed=0,
        )

        # First event should have degraded to zero flows
        assert result.per_event[0]["degradation"] != "none"
        assert result.per_event[0]["charge_kwh"] == 0.0
        assert result.per_event[0]["discharge_kwh"] == 0.0

        # But still produce a valid schedule
        assert result.schedule is not None
        assert result.cost_eur is not None

    def test_not_bit_identical_to_day_ahead(self) -> None:
        """Rolling horizon with sigma=0 is NOT identical to day-ahead.

        Zero noise does NOT imply the same plan, but the honest reason is worth
        stating precisely, because an earlier version of this test attributed the
        difference to the wrong thing.  Both policies carry their **actual** clipped
        SOC across the boundary — the day-ahead roll in :func:`policy_day_ahead`
        rolls the end-of-day SOC forward exactly like the rolling dispatcher — so
        "rolling carries actual, day-ahead carries planned" is not what separates
        them.  With sigma=0 both also see identical prices; warmup events are exact
        too, so there is no information advantage on either side.

        What actually differs is *when a plan can be revised*:

        * **horizon** — a rolling solve plans to midnight ending D+1 (~34 h), a
          day-ahead solve sees one local day (24 h);
        * **cadence** — the rolling roll re-plans at 14:00 and 20:30, so it can
          still act on information about the coming day inside the current one,
          while a day-ahead plan is frozen for its whole day.

        Those only show up in cost when prices are not constant within a solve's own
        day *and* differ between days, so this fixture gives each local day its own
        price level plus a within-day tilt on top of the night/morning/evening
        shape.  With flat intra-day buckets the two policies agree to the cent and
        an earlier version of this test failed for that reason, not because the
        invariant was broken.

        The **load level** must also move day to day, for the same kind of reason and
        for one the slot-position fix exposed.  A frozen trailing-mean profile is only
        a forecast if there is something to predict: on a load that is identical at the
        same clock position of every day, the per-slot mean reproduces the truth
        exactly, and once both policies are handed a perfect forecast and (at sigma=0)
        perfect prices they plan the same dispatch.  Before the fix they did *not* —
        only because positions were taken as offsets within each sliced run, which on
        this fixture (starting at 02:00 CEST, so local days are 88- and 96-slot runs)
        mis-assigned every interval below the cut by up to eight slots.  That shift was
        itself a forecast error, and the test was passing for the wrong reason.  The
        per-day load factor here supplies the imperfection deliberately.
        """
        from energy_baseline.battery import Horizon
        from energy_baseline.rolling_horizon import run_day_ahead_with_frozen_forecast

        # Create a scenario with varying load and prices to expose differences
        spec = _spec()
        n_days = 5
        n = n_days * 96

        base = datetime(2025, 6, 15, 0, 0, tzinfo=UTC)
        starts = [base + timedelta(seconds=i * QUARTER) for i in range(n)]
        local_days = [s.astimezone(MADRID).date() for s in starts]

        # Prices: the usual intra-day shape, a small tilt so no two intervals inside a
        # single solve are interchangeable, and a per-day offset so *which* day a kWh is
        # bought for changes the cost.  Loads vary by hour AND carry a per-day level, so
        # the frozen profile is genuinely imperfect (see the docstring).
        local_day_set = sorted(set(local_days))
        day_offset = {d: 0.04 * (i % 3) - 0.04 for i, d in enumerate(local_day_set)}
        day_level = {d: [1.0, 1.4, 0.7][i % 3] for i, d in enumerate(local_day_set)}
        prices = np.zeros(n)
        load = np.zeros(n)
        for i in range(n):
            hour = (i % 96) // 4
            if 0 <= hour < 6:
                price, day_load = 0.05, 0.1  # cheap and quiet at night
            elif 6 <= hour < 10:
                price, day_load = 0.20, 0.4  # expensive morning peak
            elif 10 <= hour < 14:
                price, day_load = 0.12, 0.3  # moderate
            elif 14 <= hour < 18:
                price, day_load = 0.08, 0.25  # cheap afternoon (solar)
            elif 18 <= hour < 22:
                price, day_load = 0.25, 0.5  # expensive evening peak
            else:
                price, day_load = 0.06, 0.15  # cheap night
            tilt = 0.02 * ((i % 96) / 96 - 0.5)
            prices[i] = price + tilt + day_offset[local_days[i]]
            load[i] = day_load * day_level[local_days[i]]

        usable = np.ones(n, dtype=bool)

        # Build frozen forecast
        frozen_load, _ = build_frozen_load_profile(
            load,
            local_days,
            usable,
            training_end_idx=96,
            window_days=28,
            interval_starts_utc=starts,
        )

        # Run rolling horizon
        rh_result = run_rolling_horizon(
            spec,
            prices=prices,
            load_kwh=load,
            usable=usable,
            interval_seconds=QUARTER,
            local_days=local_days,
            interval_starts_utc=starts,
            eval_start_idx=96,  # Start from day 2
            eval_end_idx=n,
            tz=MADRID,
            frozen_load=frozen_load,
            terminal_value_eur_per_kwh=0.10,
            sigma=0.0,
            seed=0,
        )

        # Run day-ahead with same frozen forecast
        eval_horizon = Horizon(
            interval_seconds=QUARTER,
            prices=prices[96:n],
            load_kwh=load[96:n],
            usable=usable[96:n],
        )
        da_schedule, _da_telemetry = run_day_ahead_with_frozen_forecast(
            spec,
            eval_horizon,
            frozen_load=frozen_load,
            eval_start_idx=0,
            eval_end_idx=n - 96,
            local_days=local_days[96:],
            terminal_value_eur_per_kwh=0.10,
            scoring_terminal_value_eur_per_kwh=0.0,
        )

        # Verify both ran successfully
        assert rh_result.schedule is not None
        assert da_schedule is not None

        # The costs should differ (not necessarily which is better, just different):
        # a longer horizon and two re-plans per day buy the rolling roll opportunities
        # the day-ahead plan does not have.
        cost_diff = abs(rh_result.cost_eur - da_schedule.cost_eur)
        assert cost_diff > 1e-6, (
            f"Rolling horizon and day-ahead produced identical costs ({rh_result.cost_eur}), "
            "but they plan different horizons at different cadence; if this fixture ever stops "
            "exposing that, vary prices within a day as well as between days"
        )

        # The charge/discharge patterns should also differ
        charge_diff = np.abs(rh_result.schedule.charge_kwh - da_schedule.charge_kwh).sum()
        discharge_diff = np.abs(rh_result.schedule.discharge_kwh - da_schedule.discharge_kwh).sum()
        total_flow_diff = charge_diff + discharge_diff
        assert total_flow_diff > 1e-6, (
            "Rolling horizon and day-ahead produced identical flows, "
            "but they should differ due to different execution patterns"
        )

        # And both start the window from what they were handed: the difference is in
        # the dispatch, not in one policy getting a different battery.
        assert rh_result.schedule.soc_kwh[0] == pytest.approx(float(da_schedule.soc_kwh[0]))

    def test_dst_fall_back_2025_oct_26(self) -> None:
        """DST fall-back (2025-10-26, Europe/Madrid) is a 100-interval local day.

        Madrid goes from CEST (UTC+2) to CET (UTC+1) at 03:00 local, so that local
        day is 25 hours long. This pins four things that silently break on a DST day:

        * events are placed by **local** wall time (14:00/20:30), not UTC;
        * a plan horizon spans real elapsed time, so an afternoon solve the day
          **before** the transition is 35 h and one **on** it is 34 h;
        * a full 100-slot noise curve is drawn for that day even though only part of
          it is estimated;
        * every interval — including the repeated hour — executes exactly once.
        """
        # Whole local days 25, 26 and 27 (see the fixture helper for why the slice is
        # built this way); day 26 is the 25-hour fall-back day.
        prices, load, usable, local_days, starts = _dst_fall_back_window()

        counts: dict[date, int] = {}
        for day in local_days:
            counts[day] = counts.get(day, 0) + 1
        assert counts == {
            date(2025, 10, 25): 96,
            date(2025, 10, 26): 100,  # the 25-hour fall-back day
            date(2025, 10, 27): 96,
        }

        events = compute_rolling_events(
            starts,
            local_days,
            eval_start_idx=0,
            eval_end_idx=len(starts),
            tz=MADRID,
        )

        def afternoon(day: date) -> Any:
            found = [e for e in events if e.kind == "afternoon" and e.local_day == day]
            assert len(found) == 1, found
            return found[0]

        # Events sit at local wall time, verified against the interval they landed on.
        for event in events:
            if event.kind == "warmup":
                continue
            local = starts[event.start_idx].astimezone(MADRID)
            assert (local.hour, local.minute) == (
                (14, 0) if event.kind == "afternoon" else (20, 30)
            ), f"{event.kind} on {event.local_day} landed at {local:%H:%M} local"

        # Horizon length follows real elapsed time across the transition: solving at
        # 14:00 the day BEFORE a 25-hour day plans 10 h + 25 h = 35 h; solving ON the
        # long day leaves 10 h of it and plans 10 h + 24 h = 34 h.
        assert (
            afternoon(date(2025, 10, 25)).plan_end_idx - afternoon(date(2025, 10, 25)).start_idx
        ) == 35 * 4
        assert (
            afternoon(date(2025, 10, 26)).plan_end_idx - afternoon(date(2025, 10, 26)).start_idx
        ) == 34 * 4

        # An execution window never runs past its own plan.
        for event in events:
            assert event.exec_end_idx <= event.plan_end_idx, event

        frozen_load, _ = build_frozen_load_profile(
            load,
            local_days,
            usable,
            training_end_idx=96,
            window_days=1,
            interval_starts_utc=starts,
        )

        result = run_rolling_horizon(
            _spec(),
            prices=prices,
            load_kwh=load,
            usable=usable,
            interval_seconds=QUARTER,
            local_days=local_days,
            interval_starts_utc=starts,
            eval_start_idx=0,
            eval_end_idx=len(starts),
            tz=MADRID,
            frozen_load=frozen_load,
            terminal_value_eur_per_kwh=0.10,
            sigma=0.1,
            seed=42,
        )

        # Every interval executed exactly once — the 100 slots of the long day included.
        coverage = np.zeros(len(starts), dtype=int)
        for record in result.per_event:
            coverage[record["start_idx"] : record["exec_end_idx"]] += 1
        assert np.all(coverage == 1), (
            f"DST coverage violation: {int(np.sum(coverage == 0))} gaps, "
            f"{int(np.sum(coverage > 1))} overlaps"
        )

        # A complete day of noise was drawn for the fall-back day, including its
        # repeated hour, even though only the D+1 tails are actually estimated.
        noisy, meta = generate_price_noise(
            prices,
            local_days,
            0.1,
            seed=42,
            day_bounds=contiguous_day_bounds(list(local_days)),
        )
        oct26 = np.array([d == date(2025, 10, 26) for d in local_days], dtype=bool)
        assert oct26.sum() == 100
        changed = np.abs(noisy - prices) > 0.0
        assert changed[oct26].all(), "the fall-back day's noise curve is incomplete"
        assert meta["noise_applied"]

        # Accuracy stats describe only the estimated slots, never all three days.
        assert result.noise_metadata["stats_on_estimated_slots_only"] is True
        assert 0 < result.noise_metadata["stats_slots_considered"] < len(starts)

        assert result.schedule is not None
        assert np.all(result.schedule.grid_kwh >= -1e-9)

    def test_warmup_after_evening_event_on_dst_day(self) -> None:
        """DST and the after-20:30 split together: the long day's tail is dispatched.

        Starting on 2025-10-26 (the 25-hour fall-back day) after its 20:30 event is
        where both bugs land at once: that night crosses a midnight that is 25 hours
        after the day started, so a single warmup either plans past its own local
        midnight or leaves the night unexecuted.
        """
        prices, load, usable, local_days, starts = _dst_fall_back_window()
        oct26_start = next(i for i, d in enumerate(local_days) if d == date(2025, 10, 26))
        oct27_start = next(i for i, d in enumerate(local_days) if d == date(2025, 10, 27))

        # 21:30 local on the fall-back day (i.e. CET, after the repeated hour).
        target = datetime(2025, 10, 26, 21, 30, tzinfo=MADRID).astimezone(UTC)
        eval_start = next(i for i, s in enumerate(starts) if s >= target)
        assert local_days[eval_start] == date(2025, 10, 26)

        events = compute_rolling_events(
            starts,
            local_days,
            eval_start_idx=eval_start,
            eval_end_idx=len(starts),
            tz=MADRID,
        )
        warmups = [e for e in events if e.kind == "warmup"]
        assert [w.local_day for w in warmups] == [date(2025, 10, 26), date(2025, 10, 27)]
        # The first warmup stops at the fall-back day's midnight, which is index
        # oct26_start + 100 — not +96 — because that local day really is 25 hours long.
        assert warmups[0].plan_end_idx == oct27_start == oct26_start + 100
        _assert_events_tile(events, eval_start, len(starts))

        # And the roll itself survives the same window end to end.
        frozen_load, _ = build_frozen_load_profile(
            load,
            local_days,
            usable,
            training_end_idx=oct26_start,
            window_days=1,
            interval_starts_utc=starts,
        )
        result = run_rolling_horizon(
            _spec(),
            prices=prices,
            load_kwh=load,
            usable=usable,
            interval_seconds=QUARTER,
            local_days=local_days,
            interval_starts_utc=starts,
            eval_start_idx=eval_start,
            eval_end_idx=len(starts),
            tz=MADRID,
            frozen_load=frozen_load,
            terminal_value_eur_per_kwh=0.10,
            sigma=0.1,
            seed=5,
        )
        covered = np.zeros(len(starts) - eval_start, dtype=int)
        for rec in result.per_event:
            covered[rec["start_idx"] - eval_start : rec["exec_end_idx"] - eval_start] += 1
        assert np.all(covered == 1), (
            f"DST + after-20:30 coverage violation: {int((covered == 0).sum())} gaps, "
            f"{int((covered > 1).sum())} overlaps"
        )

    def test_future_prices_dont_change_past_execution(self) -> None:
        """Changing future prices does NOT change already-committed actions.

        The first event's execution is based on its own plan horizon.  Changing
        prices after the execution window should not retroactively change what
        was executed.
        """
        spec, prices, load, usable, local_days, starts, frozen = self._simple_setup(5)
        r1 = run_rolling_horizon(
            spec,
            prices=prices,
            load_kwh=load,
            usable=usable,
            interval_seconds=QUARTER,
            local_days=local_days,
            interval_starts_utc=starts,
            eval_start_idx=0,
            eval_end_idx=len(starts),
            tz=MADRID,
            frozen_load=frozen,
            terminal_value_eur_per_kwh=0.10,
            sigma=0.0,
            seed=0,
        )
        # Change prices in the last day
        prices2 = prices.copy()
        prices2[-96:] = 0.50  # very expensive
        r2 = run_rolling_horizon(
            spec,
            prices=prices2,
            load_kwh=load,
            usable=usable,
            interval_seconds=QUARTER,
            local_days=local_days,
            interval_starts_utc=starts,
            eval_start_idx=0,
            eval_end_idx=len(starts),
            tz=MADRID,
            frozen_load=frozen,
            terminal_value_eur_per_kwh=0.10,
            sigma=0.0,
            seed=0,
        )
        # The first event's execution should be identical
        first_exec_end = r1.per_event[0]["exec_end_idx"]
        assert np.array_equal(
            r1.schedule.charge_kwh[:first_exec_end],
            r2.schedule.charge_kwh[:first_exec_end],
        )

    def test_each_interval_executed_exactly_once(self) -> None:
        """Every interval in the eval window is covered by exactly one event."""
        spec, prices, load, usable, local_days, starts, frozen = self._simple_setup(3)
        result = run_rolling_horizon(
            spec,
            prices=prices,
            load_kwh=load,
            usable=usable,
            interval_seconds=QUARTER,
            local_days=local_days,
            interval_starts_utc=starts,
            eval_start_idx=0,
            eval_end_idx=len(starts),
            tz=MADRID,
            frozen_load=frozen,
            terminal_value_eur_per_kwh=0.10,
            sigma=0.0,
            seed=0,
        )
        n_eval = len(starts)
        covered = np.zeros(n_eval, dtype=int)
        for ev in result.per_event:
            lo = ev["start_idx"]
            hi = ev["exec_end_idx"]
            covered[lo:hi] += 1
        # Every interval should be covered exactly once
        assert np.all(covered == 1), f"coverage: min={covered.min()}, max={covered.max()}"

    def test_negative_prices_handled(self) -> None:
        """Negative prices are valid and don't crash the dispatcher."""
        start = datetime(2025, 6, 15, 0, 0, tzinfo=UTC)
        prices, load, usable, local_days, starts = _make_dataset(
            start,
            3,
            base_price=0.05,
            base_load=0.2,
        )
        # Add some negative prices
        prices[10:20] = -0.02
        spec = _spec()
        frozen_load, _ = build_frozen_load_profile(
            load,
            local_days,
            usable,
            training_end_idx=96,
            window_days=28,
            interval_starts_utc=starts,
        )
        result = run_rolling_horizon(
            spec,
            prices=prices,
            load_kwh=load,
            usable=usable,
            interval_seconds=QUARTER,
            local_days=local_days,
            interval_starts_utc=starts,
            eval_start_idx=0,
            eval_end_idx=len(starts),
            tz=MADRID,
            frozen_load=frozen_load,
            terminal_value_eur_per_kwh=0.10,
            sigma=0.0,
            seed=0,
        )
        assert result.cost_eur is not None
        assert np.all(result.schedule.grid_kwh >= -1e-9)


# ---------------------------------------------------------------------------
# Paired design: one bias realisation shared by every load-window variant
# ---------------------------------------------------------------------------


class TestDayBiasSharedAcrossVariants:
    """The day-level bias must be a function of ``(seed, local_date)`` **only**.

    The whole point of the paired experiment is that two runs differing only in their
    load-forecast window face the *same* price realisation.  If the bias draw depended on
    anything else — the profile, the estimated mask, how many variants ran before it — a
    window comparison would be measuring two different price worlds and the profile
    effect could not be isolated.
    """

    def _setup(self, n_days: int = 8):
        """3 training days + 5 evaluation days on a realistic daily price shape.

        Two details are load-bearing for the claims made below:

        * The **price shape** is the cheap-night / expensive-evening profile (the same
          one ``tests/test_compare_forecasts_cli.py`` builds), not a flat or repeating
          level.  A pure multiplicative day bias rescales a whole day, so on a fixture
          where every day has an identical profile there is nothing for it to reorder —
          the variant would be measuring nothing and the test below would pass only by
          accident of the arithmetic.
        * The **load level** moves day to day, because ``window_days`` selects the
          trailing N training days.  On a flat load a 1-day and a 2-day profile are
          identical and "the two arms differ" would be unprovable.

        Three training days is also the reason both windows are requested as ``1`` and
        ``2``: requesting more than exists clamps to the same thing.
        """
        start = datetime(2025, 6, 15, 0, 0, tzinfo=UTC)
        prices, load, usable, local_days, starts = _make_dataset(
            start, n_days, base_price=0.15, base_load=0.2
        )
        for i in range(len(prices)):
            hour = (i % 96) / 4.0
            if hour < 6:
                prices[i] = 0.04
            elif hour < 10:
                prices[i] = 0.19
            elif hour < 14:
                prices[i] = 0.11
            elif hour < 18:
                prices[i] = 0.07
            elif hour < 22:
                prices[i] = 0.24
            else:
                prices[i] = 0.05
        for i in range(len(load)):
            load[i] = 0.10 + 0.05 * ((i // 96) % 3)
        return prices, load, usable, local_days, starts

    TRAIN_END = 3 * 96

    def test_bias_identical_across_load_window_variants(self) -> None:
        """Two window variants at the same ``(seed, bias_sigma)`` see one price array."""
        prices, load, usable, local_days, starts = self._setup()
        day_bounds = contiguous_day_bounds(list(local_days))
        profiles = {
            window: build_frozen_load_profile(
                load,
                local_days,
                usable,
                training_end_idx=self.TRAIN_END,
                window_days=window,
                interval_starts_utc=starts,
            )[0]
            for window in (1, 2)
        }
        # Non-vacuity: the two arms really are different forecasts, or "identical noise"
        # would be trivially true of two identical runs.
        assert not np.array_equal(profiles[1], profiles[2])

        realised: dict[int, list[dict[str, Any]]] = {}
        arrays: dict[int, np.ndarray] = {}
        for window in profiles:
            # The estimated region is identical on purpose: what varies is only the load
            # profile the caller would otherwise hand to the dispatcher.
            mask = np.zeros(len(prices), dtype=bool)
            mask[200:400] = True
            noisy, meta = generate_price_noise(
                prices,
                local_days,
                0.05,
                seed=9,
                bias_sigma=0.2,
                day_bounds=day_bounds,
                estimated_slots_mask=mask,
            )
            arrays[window] = noisy
            realised[window] = meta["per_day"]

        assert np.array_equal(arrays[1], arrays[2]), "variants saw different price draws"
        bias_1 = [r["bias_applied"] for r in realised[1]]
        assert bias_1 == [r["bias_applied"] for r in realised[2]]
        assert len(set(bias_1)) > 1, "one global bias, not per-day"

    def test_bias_shared_through_the_dispatcher_for_different_profiles(self) -> None:
        """End to end: two windows' ``noise_metadata`` report the same applied bias."""
        prices, load, usable, local_days, starts = self._setup()
        runs = []
        profiles = {}
        for window in (1, 2):
            frozen, _ = build_frozen_load_profile(
                load,
                local_days,
                usable,
                training_end_idx=self.TRAIN_END,
                window_days=window,
                interval_starts_utc=starts,
            )
            profiles[window] = frozen
            runs.append(
                run_rolling_horizon(
                    _spec(),
                    prices=prices,
                    load_kwh=load,
                    usable=usable,
                    interval_seconds=QUARTER,
                    local_days=local_days,
                    interval_starts_utc=starts,
                    eval_start_idx=self.TRAIN_END,
                    eval_end_idx=len(starts),
                    tz=MADRID,
                    frozen_load=frozen,
                    terminal_value_eur_per_kwh=0.10,
                    sigma=0.05,
                    bias_sigma=0.2,
                    seed=9,
                )
            )
        assert not np.array_equal(profiles[1], profiles[2]), "the two arms are the same arm"
        a, b = runs
        # Same noise realisation on both sides: only the load forecast differed.
        assert a.noise_metadata["per_day"] == b.noise_metadata["per_day"]
        assert a.noise_metadata["bias_mean_applied"] == pytest.approx(
            b.noise_metadata["bias_mean_applied"]
        )
        assert a.noise_metadata["bias_std_applied"] == pytest.approx(
            b.noise_metadata["bias_std_applied"]
        )
        assert a.noise_metadata["bias_sigma"] == 0.2
        # And the bias is recorded on the dispatcher's telemetry too, so a reader can
        # tell which arm of the matrix a row came from without opening the noise block.
        assert a.telemetry["bias_sigma"] == 0.2
        assert b.telemetry["bias_sigma"] == 0.2

    def test_bias_only_run_changes_the_plan_but_never_the_scoring(self) -> None:
        """A pure day-level scaling is a real planning input, yet cost stays true-priced."""
        prices, load, usable, local_days, starts = self._setup()
        frozen, _ = build_frozen_load_profile(
            load,
            local_days,
            usable,
            training_end_idx=self.TRAIN_END,
            window_days=2,
            interval_starts_utc=starts,
        )

        def run(bias_sigma: float, seed: int):
            return run_rolling_horizon(
                _spec(),
                prices=prices,
                load_kwh=load,
                usable=usable,
                interval_seconds=QUARTER,
                local_days=local_days,
                interval_starts_utc=starts,
                eval_start_idx=self.TRAIN_END,
                eval_end_idx=len(starts),
                tz=MADRID,
                frozen_load=frozen,
                terminal_value_eur_per_kwh=0.10,
                sigma=0.0,
                bias_sigma=bias_sigma,
                seed=seed,
            )

        exact = run(0.0, 9)
        assert exact.noise_metadata["noise_applied"] is False
        # Not every draw lands near zero, and a near-zero day bias legitimately leaves the
        # plan alone.  What must not happen is that NO seed ever changes anything: with
        # sigma=0 the per-day multiplier is the only difference between the two runs, so a
        # variant that can never move the plan would be measuring nothing.
        changed = [
            seed
            for seed in range(6)
            if not np.array_equal(exact.schedule.charge_kwh, run(0.3, seed).schedule.charge_kwh)
        ]
        assert changed, "a 30 % day-level bias never changed the executed plan"

        biased = run(0.3, 9)
        assert biased.noise_metadata["noise_applied"] is True
        # Scoring is still dot(true price, actual grid) — the biased estimate never
        # enters the bill, however much it moved the plan.
        grid = np.asarray(biased.schedule.grid_kwh, dtype=float)
        true_slice = prices[self.TRAIN_END : len(starts)]
        assert biased.cost_eur == pytest.approx(
            float(np.dot(true_slice, grid[: true_slice.size])), rel=1e-9, abs=1e-9
        )


# ---------------------------------------------------------------------------
# Golden values: the legacy two-publication schedule must stay bit-identical
# ---------------------------------------------------------------------------

#: Golden values for the cadence parameters' DEFAULT configuration, captured by
#: running the implementation at ``git HEAD`` (before ``replan_interval_slots`` and
#: ``horizon_slots`` existed) over the fixtures below.  They are literals, not
#: recomputed at runtime, so a refactor that silently shifts the schedule, the
#: information set, the clipping accounting or the executed dispatch fails here —
#: the same technique that pinned the pre-bias price-noise stream.
#:
#: Both fixtures are pinned at four noise settings, because a golden that only
#: covers ``sigma=0`` would not notice a perturbed noise stream: ``control`` is the
#: exact-price arm, ``noisy_seed0``/``noisy_seed1`` are the same sigma at different
#: seeds (their executed plans differ from each other AND from the control, so the
#: digests are not vacuous), and ``bias_seed4`` exercises the day-level channel.
_GOLDEN: dict[str, Any] = {
    "simple": {
        "eval_start_idx": 0,
        "eval_end_idx": 480,
        "n_events": 11,
        "events_digest": "59bb99ea82ea3a956c8d92841934bdb3df000a9e538bf482f4e8511c3761eb2b",
        "frozen_sha_eval": "2664e1f3d61225f33435a84c896b5c98ef16ac05e4d551ef6081cf874daad9f0",
        "runs": {
            "control": {
                "cost_eur": 8.877136842105266,
                "charge_sha": "6b97722ee240768a588feab902854e812d25254cef84b72af6c67d89dd982054",
                "discharge_sha": "88d54d722e5352b6a05e428fcf536dd48fcb85c4123eac62795967ff176f8218",
                "soc_sha": "cff44ece9e4a6597cda536a37583cf1f7f3e04c59fa8c1316cc3457771b8697f",
                "grid_sha": "aed90b257d02b1c8cf1ebd4467e546e5dff609cc9b36a4b0b165c64905249293",
                "event_count": 11,
                "events_solved": 11,
                "events_with_clipping": 0,
                "charge_clipped_kwh": 0.0,
                "discharge_clipped_kwh": 0.0,
                "terminal_soc_kwh": 4.8640000000000025,
                "efc": 4.999999999999998,
                "noise_mape": 0.0,
                "noise_sign_flips": 0,
                "noise_stats_slots": 392,
                "noise_bias_mean_applied": 0.0,
            },
            "noisy_seed0": {
                "cost_eur": 8.877136842105267,
                "charge_sha": "ccc6ad8134278c4bb9d5c35c81e8cffd477b56f0ad2db7532fa80fbf4a776d77",
                "discharge_sha": "abaef15c323baadbe474b5f5d784df60dff7be2388040c31739510b0f0306250",
                "soc_sha": "8a10f5fb0c4dc58af28ad2860e057437f8214737c77aebba3849ee46c9dce38c",
                "grid_sha": "8ffbb7266f5ce17a97c198ca9ddb746c92c3aabba03f681e8130261e44cd8a07",
                "event_count": 11,
                "events_solved": 11,
                "events_with_clipping": 0,
                "charge_clipped_kwh": 0.0,
                "discharge_clipped_kwh": 0.0,
                "terminal_soc_kwh": 4.864000000000006,
                "efc": 4.999999999999999,
                "noise_mape": 0.1717510091292121,
                "noise_sign_flips": 0,
                "noise_stats_slots": 392,
                "noise_bias_mean_applied": 0.0,
            },
            "noisy_seed1": {
                "cost_eur": 8.877136842105267,
                "charge_sha": "978ba2f4e9cdb11f9fced7f93cd48a0d3ac7ad48fb13aa5867f0812b4a0883f2",
                "discharge_sha": "4e09c6221ea453bc1ad3685441421a51145e78736081d89739bd96fec3c969bf",
                "soc_sha": "3b109f803de4438f11c6e033d1b10a324a5645e24e9093685744f0cc746e9cbe",
                "grid_sha": "1f8edf627033466565804c7ecf1eaad482a1a02970b7d9617cca7f27de7ddb31",
                "event_count": 11,
                "events_solved": 11,
                "events_with_clipping": 0,
                "charge_clipped_kwh": 0.0,
                "discharge_clipped_kwh": 0.0,
                "terminal_soc_kwh": 4.864000000000008,
                "efc": 4.999999999999998,
                "noise_mape": 0.16270968882196465,
                "noise_sign_flips": 0,
                "noise_stats_slots": 392,
                "noise_bias_mean_applied": 0.0,
            },
            "bias_seed4": {
                "cost_eur": 8.877136842105267,
                "charge_sha": "bef782884d9102fa544c65e98b560fdb8b13ec853017c91b98b8285cc227caa3",
                "discharge_sha": "abcdd68bd4b327c53f7c6027c458c9038fd4f6131064a309177482379f6f3fa8",
                "soc_sha": "cd519d37f685f72469cc432231c23d8897f5e3c25da5c53578343122d4b9287b",
                "grid_sha": "1a912b846837311b7085c8d2370f3b7a8722525891269bac6f62331a45cffe80",
                "event_count": 11,
                "events_solved": 11,
                "events_with_clipping": 0,
                "charge_clipped_kwh": 0.0,
                "discharge_clipped_kwh": 0.0,
                "terminal_soc_kwh": 4.864000000000004,
                "efc": 5.0,
                "noise_mape": 0.14377740897324268,
                "noise_sign_flips": 0,
                "noise_stats_slots": 392,
                "noise_bias_mean_applied": -0.12120637443466142,
            },
        },
    },
    "dst": {
        "eval_start_idx": 0,
        "eval_end_idx": 292,
        "n_events": 7,
        "events_digest": "5326a4fc79a7fd327d63edad4588608c00407ff72bf63c2810983671cbadbe23",
        "frozen_sha_eval": "6622ab694a2d52e04199e5616c13e1b62ac44a9b89554a3191ca656be6ea3ad3",
        "runs": {
            "control": {
                "cost_eur": 6.063828210526316,
                "charge_sha": "d9218c0d66bd2ff76da0e346808b53e62474c8e464d8e6edf507b1e1732f0a7d",
                "discharge_sha": "0cbaa480e6509fee96082a88921c6177b377eafdc5439076888eb282cf1fd1e7",
                "soc_sha": "84775453203422a2fa0a447977e07e0f640b3296612affd3a60e7e4239c09e62",
                "grid_sha": "8466024a4cf2a847684177d6dc85ec20a2142c19864540c5d04625b89d7935e2",
                "event_count": 7,
                "events_solved": 7,
                "events_with_clipping": 0,
                "charge_clipped_kwh": 0.0,
                "discharge_clipped_kwh": 0.0,
                "terminal_soc_kwh": 4.864000000000002,
                "efc": 2.0,
                "noise_mape": 0.0,
                "noise_sign_flips": 0,
                "noise_stats_slots": 196,
                "noise_bias_mean_applied": 0.0,
            },
            "noisy_seed0": {
                "cost_eur": 6.063828210526316,
                "charge_sha": "62d09cd101511a3cf7d6af59eb340ee8dc7e9f140bd2137d107657fbebcf1b05",
                "discharge_sha": "f4c62f2417b0552a8fac5c0cc371430cbff2173291065af5cfeda756f6ac4e4f",
                "soc_sha": "265b6611c57f91388191935d33bdbc6a472e090c2d951bee8f04660e294522a3",
                "grid_sha": "1f809f41dac3c3039cb227dc664a5c45d8053b303acc884736e2dd3e667bc7ff",
                "event_count": 7,
                "events_solved": 7,
                "events_with_clipping": 0,
                "charge_clipped_kwh": 0.0,
                "discharge_clipped_kwh": 0.0,
                "terminal_soc_kwh": 4.863999999999999,
                "efc": 2.0000000000000004,
                "noise_mape": 0.16040380367426837,
                "noise_sign_flips": 0,
                "noise_stats_slots": 196,
                "noise_bias_mean_applied": 0.0,
            },
            "noisy_seed1": {
                "cost_eur": 6.063828210526316,
                "charge_sha": "62d09cd101511a3cf7d6af59eb340ee8dc7e9f140bd2137d107657fbebcf1b05",
                "discharge_sha": "a033a965db787c957291a3c142c5a92e130bdaaca04ca3fa76669b3051325706",
                "soc_sha": "f6c22bb12c054acd3ef24b1845ab2adc8599587b6cac8c881af5264827bdc881",
                "grid_sha": "996774eb4abfa5690a5b7c45f516a0fa46f4e409df66f5d0abaa8a9eccde34cf",
                "event_count": 7,
                "events_solved": 7,
                "events_with_clipping": 0,
                "charge_clipped_kwh": 0.0,
                "discharge_clipped_kwh": 0.0,
                "terminal_soc_kwh": 4.864000000000001,
                "efc": 2.0,
                "noise_mape": 0.16198595418642733,
                "noise_sign_flips": 0,
                "noise_stats_slots": 196,
                "noise_bias_mean_applied": 0.0,
            },
            "bias_seed4": {
                "cost_eur": 6.063828210526316,
                "charge_sha": "62d09cd101511a3cf7d6af59eb340ee8dc7e9f140bd2137d107657fbebcf1b05",
                "discharge_sha": "f4c62f2417b0552a8fac5c0cc371430cbff2173291065af5cfeda756f6ac4e4f",
                "soc_sha": "265b6611c57f91388191935d33bdbc6a472e090c2d951bee8f04660e294522a3",
                "grid_sha": "1f809f41dac3c3039cb227dc664a5c45d8053b303acc884736e2dd3e667bc7ff",
                "event_count": 7,
                "events_solved": 7,
                "events_with_clipping": 0,
                "charge_clipped_kwh": 0.0,
                "discharge_clipped_kwh": 0.0,
                "terminal_soc_kwh": 4.863999999999999,
                "efc": 2.0000000000000004,
                "noise_mape": 0.25680243875573194,
                "noise_sign_flips": 0,
                "noise_stats_slots": 196,
                "noise_bias_mean_applied": -0.24573319248850523,
            },
        },
    },
}

_GOLDEN_CONFIGS: dict[str, dict[str, float | int]] = {
    "control": {"sigma": 0.0, "bias_sigma": 0.0, "seed": 0},
    "noisy_seed0": {"sigma": 0.2, "bias_sigma": 0.0, "seed": 0},
    "noisy_seed1": {"sigma": 0.2, "bias_sigma": 0.0, "seed": 1},
    "bias_seed4": {"sigma": 0.1, "bias_sigma": 0.2, "seed": 4},
}


def _golden_fixture(
    name: str,
) -> tuple[np.ndarray, np.ndarray, np.ndarray, list[date], list[datetime], np.ndarray]:
    """The two fixtures the golden values were captured on, rebuilt here.

    ``simple`` is the cheap-night / expensive-evening shape used by
    :class:`TestRollingHorizon` over five local days opening at 02:00 CEST (so the
    first local day is partial and a warmup event is required — the golden covers the
    warmup path too).  ``dst`` is the whole 2025-10-25/26/27 fall-back weekend, where
    day 26 is 100 intervals.

    Both the fixture construction and the digests must agree with HEAD's numbers, so
    this builder is deliberately written out in full rather than reusing the helper
    methods above: if one of those fixtures is ever edited, the golden test fails
    loudly instead of silently re-baselining.
    """
    if name == "simple":
        start = datetime(2025, 6, 15, 0, 0, tzinfo=UTC)
        n = 5 * 96
        prices = np.full(n, 0.15)
        load = np.full(n, 0.2)
        starts = [start + timedelta(seconds=i * QUARTER) for i in range(n)]
        for i in range(n):
            hour = (i % 96) // 4
            prices[i] = (
                0.05
                if hour < 6
                else 0.20
                if hour < 10
                else 0.12
                if hour < 14
                else 0.08
                if hour < 18
                else 0.25
                if hour < 22
                else 0.06
            )
        window = 28
    elif name == "dst":
        raw_start = datetime(2025, 10, 24, 22, 0, tzinfo=UTC)
        raw_n = 5 * 96
        raw_prices = np.full(raw_n, 0.15)
        raw_load = np.full(raw_n, 0.2)
        raw_starts = [raw_start + timedelta(seconds=i * QUARTER) for i in range(raw_n)]
        for i in range(raw_n):
            hour = (i % 96) // 4
            raw_prices[i] = 0.05 if hour < 6 else 0.25 if 18 <= hour < 22 else 0.12
        raw_days = [s.astimezone(MADRID).date() for s in raw_starts]
        lo = next(i for i, d in enumerate(raw_days) if d == date(2025, 10, 25))
        hi = next(i for i, d in enumerate(raw_days) if d == date(2025, 10, 28))
        prices, load = raw_prices[lo:hi], raw_load[lo:hi]
        starts = raw_starts[lo:hi]
        window = 1
    else:  # pragma: no cover - a typo in the table above must not pass silently
        raise AssertionError(f"unknown golden fixture {name!r}")

    usable = np.ones(len(starts), dtype=bool)
    local_days = [s.astimezone(MADRID).date() for s in starts]
    frozen, _ = build_frozen_load_profile(
        load,
        local_days,
        usable,
        training_end_idx=96,
        window_days=window,
        interval_starts_utc=starts,
    )
    return prices, load, usable, local_days, starts, frozen


def _run_digest(values: np.ndarray) -> str:
    """SHA-256 of an array as big-endian float64 — same encoding as ``_golden_digest``."""
    return hashlib.sha256(np.asarray(values, dtype=">f8").tobytes()).hexdigest()


class TestLegacyCadenceGolden:
    """The default configuration must reproduce HEAD bit-for-bit.

    Requirement: adding a configurable replan cadence must not move the already
    measured two-publication results.  The digests in :data:`_GOLDEN` came from the
    implementation at ``git HEAD``, so this test compares against a reference the
    current code cannot influence.
    """

    @pytest.mark.parametrize("fixture", sorted(_GOLDEN))
    @pytest.mark.parametrize("config", sorted(_GOLDEN_CONFIGS))
    def test_executed_dispatch_matches_golden(self, fixture: str, config: str) -> None:
        golden = _GOLDEN[fixture]["runs"][config]
        prices, load, usable, local_days, starts, frozen = _golden_fixture(fixture)
        block = _GOLDEN[fixture]
        kwargs = _GOLDEN_CONFIGS[config]

        result = run_rolling_horizon(
            _spec(),
            prices=prices,
            load_kwh=load,
            usable=usable,
            interval_seconds=QUARTER,
            local_days=local_days,
            interval_starts_utc=starts,
            eval_start_idx=block["eval_start_idx"],
            eval_end_idx=block["eval_end_idx"],
            tz=MADRID,
            frozen_load=frozen,
            terminal_value_eur_per_kwh=0.10,
            **kwargs,
        )

        assert _run_digest(result.schedule.charge_kwh) == golden["charge_sha"], (
            f"{fixture}/{config}: the executed charge profile moved. The cadence "
            "parameters were supposed to default to the legacy schedule exactly"
        )
        assert _run_digest(result.schedule.discharge_kwh) == golden["discharge_sha"]
        assert _run_digest(result.schedule.soc_kwh) == golden["soc_sha"]
        assert _run_digest(result.schedule.grid_kwh) == golden["grid_sha"]
        assert result.cost_eur == golden["cost_eur"]
        assert result.meter_cost_eur == golden["cost_eur"]
        assert float(result.schedule.efc) == pytest.approx(golden["efc"], rel=1e-12)
        assert result.telemetry["terminal_soc_kwh"] == golden["terminal_soc_kwh"]

        # Event geometry and stage counters.
        assert result.telemetry["event_count"] == golden["event_count"]
        assert result.telemetry["events_solved"] == golden["events_solved"]
        assert result.telemetry["events_with_clipping"] == golden["events_with_clipping"]
        assert result.telemetry["charge_clipped_kwh"] == golden["charge_clipped_kwh"]
        assert result.telemetry["discharge_clipped_kwh"] == golden["discharge_clipped_kwh"]

        # Which slots were estimated, and how wrong they were: the information set is
        # part of the golden, so a cadence refactor that let day D+1 be treated as
        # published before 20:30 would move noise_stats_slots and the error metrics.
        assert result.noise_metadata["stats_slots_considered"] == golden["noise_stats_slots"]
        assert result.noise_metadata["mape"] == pytest.approx(golden["noise_mape"], rel=1e-12)
        assert result.noise_metadata["sign_flips"] == golden["noise_sign_flips"]
        assert result.noise_metadata["bias_mean_applied"] == pytest.approx(
            golden["noise_bias_mean_applied"], rel=1e-12, abs=1e-15
        )

    @pytest.mark.parametrize("fixture", sorted(_GOLDEN))
    def test_event_schedule_matches_golden(self, fixture: str) -> None:
        """The event table itself is pinned, not only the dispatch it produces."""
        block = _GOLDEN[fixture]
        _p, _l, _u, local_days, starts, frozen = _golden_fixture(fixture)
        events = compute_rolling_events(
            starts,
            local_days,
            eval_start_idx=block["eval_start_idx"],
            eval_end_idx=block["eval_end_idx"],
            tz=MADRID,
        )
        table = [
            [
                e.index,
                str(e.local_day),
                e.kind,
                e.start_idx,
                e.plan_end_idx,
                e.exec_end_idx,
                e.prices_exact_end_idx,
            ]
            for e in events
        ]
        assert len(table) == block["n_events"]
        assert hashlib.sha256(json.dumps(table).encode()).hexdigest() == block["events_digest"]
        # And the new parameters left unset are the same run as them set to None
        # explicitly: the default IS the legacy schedule, not a close relative.
        explicit = compute_rolling_events(
            starts,
            local_days,
            eval_start_idx=block["eval_start_idx"],
            eval_end_idx=block["eval_end_idx"],
            tz=MADRID,
            replan_interval_slots=None,
            horizon_slots=None,
        )
        assert [
            [
                e.index,
                str(e.local_day),
                e.kind,
                e.start_idx,
                e.plan_end_idx,
                e.exec_end_idx,
                e.prices_exact_end_idx,
            ]
            for e in explicit
        ] == table

    @pytest.mark.parametrize("fixture", sorted(_GOLDEN))
    def test_frozen_forecast_matches_golden(self, fixture: str) -> None:
        """The frozen load profile is unchanged too (no leakage path was added)."""
        block = _GOLDEN[fixture]
        _p, _l, _u, _ld, _st, frozen = _golden_fixture(fixture)
        lo, hi = block["eval_start_idx"], block["eval_end_idx"]
        assert _run_digest(frozen[lo:hi]) == block["frozen_sha_eval"]

    def test_golden_arm_labels_are_distinguishable(self) -> None:
        """Non-vacuity: the pinned digests actually depend on the seed.

        Four arms of the same fixture would all pass a pin of "nothing moved" if the
        noise had no effect at all, so at least one fixture must show different
        executed plans for different seeds and for the exact-price control.  If this
        ever fails, the golden is no longer testing the noise stream and the fixture
        needs to be made price-sensitive again.
        """
        runs = _GOLDEN["simple"]["runs"]
        assert runs["control"]["charge_sha"] != runs["noisy_seed0"]["charge_sha"]
        assert runs["noisy_seed0"]["charge_sha"] != runs["noisy_seed1"]["charge_sha"]
        assert runs["control"]["noise_mape"] == 0.0
        assert runs["noisy_seed0"]["noise_mape"] > 0.0

    def test_default_run_matches_explicit_none_run(self) -> None:
        """Omitting the new arguments and passing ``None`` are one and the same run."""
        prices, load, usable, local_days, starts, frozen = _golden_fixture("simple")
        common: dict[str, Any] = dict(
            prices=prices,
            load_kwh=load,
            usable=usable,
            interval_seconds=QUARTER,
            local_days=local_days,
            interval_starts_utc=starts,
            eval_start_idx=0,
            eval_end_idx=len(starts),
            tz=MADRID,
            frozen_load=frozen,
            terminal_value_eur_per_kwh=0.10,
            sigma=0.2,
            seed=0,
        )
        implicit = run_rolling_horizon(_spec(), **common)
        explicit = run_rolling_horizon(
            _spec(), **common, replan_interval_slots=None, horizon_slots=None
        )
        assert implicit.cost_eur == explicit.cost_eur
        assert np.array_equal(implicit.schedule.charge_kwh, explicit.schedule.charge_kwh)
        for key in ("replan_interval_slots", "horizon_slots", "horizon_hours_nominal"):
            assert implicit.telemetry[key] is None
            assert explicit.telemetry[key] is None
        assert "two daily publications" in implicit.telemetry["replan_cadence"]


# ---------------------------------------------------------------------------
# Configurable replan cadence
# ---------------------------------------------------------------------------


def _daily_shape_window(
    start: datetime = datetime(2025, 6, 10, 0, 0, tzinfo=UTC),
    n_days: int = 4,
) -> tuple[np.ndarray, np.ndarray, np.ndarray, list[date], list[datetime]]:
    """A DST-free quarter-hour grid with a daily price swing and a ramping load.

    The default start is 00:00 UTC = **02:00 CEST**, so the first local day is partial
    (88 intervals) and the interior days are whole 96-interval ones — a shape in which a
    slot-counted cadence and a wall-clock-counted schedule can be compared without a DST
    day confounding the interval arithmetic.  Prices follow the cheap-night /
    expensive-evening profile used by the other rolling-horizon fixtures.
    """
    n = n_days * 96
    starts = [start + timedelta(seconds=i * QUARTER) for i in range(n)]
    local_days = [s.astimezone(MADRID).date() for s in starts]
    prices = np.empty(n)
    load = np.empty(n)
    for i in range(n):
        hour = (i % 96) // 4
        prices[i] = (
            0.05
            if hour < 6
            else 0.20
            if hour < 10
            else 0.12
            if hour < 14
            else 0.08
            if hour < 18
            else 0.25
            if hour < 22
            else 0.06
        )
        load[i] = 0.10 + 0.02 * (i % 96) / 4.0
    usable = np.ones(n, dtype=bool)
    return prices, load, usable, local_days, starts


def _estimated_days(
    event: Any,
    local_days: list[date],
) -> list[date]:
    """Local days whose prices this event's plan had to estimate.

    Read off ``prices_exact_end_idx`` — the interval the dispatcher itself uses —
    rather than recomputed from the clock, so the assertion is about what the solver
    was actually handed.
    """
    return sorted({local_days[i] for i in range(event.prices_exact_end_idx, event.plan_end_idx)})


class TestPublicationRuleAtEveryCadence:
    """The information set is a function of the decision instant, not of the cadence.

    Specification restated from the ADR for verification: day-ahead prices for local
    day ``D+1`` are published in the 20:15–20:20 window on day ``D``, and this module
    treats them as available from 20:30 (the first quarter-hour boundary after the
    window).  So a solve at any local time before 20:30 on day ``D`` sees ``D`` exact
    and ``D+1`` estimated, and a solve at 20:30 or later sees ``D`` and ``D+1`` exact.
    The expectations below are built from that statement of the rule, independently of
    ``_last_published_day``, which is what makes them a check on it rather than an
    echo of it.
    """

    #: Six local days starting 2025-06-10T00:00Z (= 02:00 CEST), so days 11–14 are
    #: whole and a 48 h horizon from any instant of day 11 stays inside the data.
    _SWEEP_DAYS = 6
    _SWEEP_DAY = date(2025, 6, 11)

    def _local_midnight_idx(self, local_days: list[date], day: date) -> int:
        return next(i for i, d in enumerate(local_days) if d == day)

    def test_sweep_of_a_full_day_at_15_minute_cadence(self) -> None:
        """Every decision instant of one local day, across the 20:30 boundary."""
        _p, _l, _u, local_days, starts = _daily_shape_window(n_days=self._SWEEP_DAYS)
        del _p, _l, _u
        day = self._SWEEP_DAY
        day_next = day + timedelta(days=1)
        first_of_day = self._local_midnight_idx(local_days, day)
        assert local_days[first_of_day - 1] != day, "fixture must open the sweep day at midnight"

        # 48 h horizon, the top of ADR-0001's band, so a plan reaches past tomorrow: a
        # solve before 20:30 must estimate every day after D, one after that must
        # estimate every day after D+1.  The window runs to the end of the data so no
        # plan is clipped short of those days.
        events = compute_rolling_events(
            starts,
            local_days,
            eval_start_idx=first_of_day,
            eval_end_idx=len(starts),
            tz=MADRID,
            replan_interval_slots=1,
            horizon_slots=192,
        )
        day_events = [ev for ev in events if local_days[ev.start_idx] == day]
        assert len(day_events) == 96, "one solve per quarter-hour of the local day"

        boundary = None
        for ev in day_events:
            local = starts[ev.start_idx].astimezone(MADRID)
            assert ev.local_day == day
            assert ev.plan_end_idx == ev.start_idx + 192, "the horizon must not be clipped"
            before_publication = (local.hour, local.minute) < EVENING_EVENT
            estimated = _estimated_days(ev, local_days)

            # Expected set, derived from the rule as stated in this class's docstring:
            # the plan's own local days, minus those already published at the decision
            # instant.  Days the horizon does not reach are not estimated, so this is
            # a statement about the split point rather than about horizon length.
            last_exact = _expected_last_exact_for_event(starts[ev.start_idx])
            plan_days = sorted({local_days[i] for i in range(ev.start_idx, ev.plan_end_idx)})
            assert day in plan_days and day_next in plan_days, (
                f"fixture drifted: a 48 h plan from {local:%H:%M} must cover D and D+1"
            )
            assert estimated == [d for d in plan_days if d > last_exact], (
                f"solve at {local:%H:%M} on {day}: expected {last_exact} to be the last "
                f"published day, so it should estimate {[d for d in plan_days if d > last_exact]}, "
                f"got {estimated}"
            )
            if before_publication:
                assert day not in estimated and day_next in estimated, (
                    f"solve at {local:%H:%M} on {day} estimated {estimated}: day {day} "
                    "must be exact and D+1 must be estimated before the 20:30 boundary"
                )
            else:
                assert day_next not in estimated, (
                    f"solve at {local:%H:%M} on {day} estimated {estimated}: D+1 prices "
                    "are published from 20:30 on D, so they must be exact afterwards"
                )
            if boundary is None and not before_publication:
                boundary = (local.hour, local.minute)
        assert boundary == EVENING_EVENT, f"first exact-tomorrow solve was at {boundary}"

    def test_just_before_and_just_after_the_publication_boundary(self) -> None:
        """The two quarter-hours that straddle the window are the whole test."""
        _p, _l, _u, local_days, starts = _daily_shape_window(n_days=self._SWEEP_DAYS)
        del _p, _l, _u
        day = self._SWEEP_DAY
        day_next = day + timedelta(days=1)
        for minute, expect_estimated in ((15, True), (30, False)):
            instant = datetime(2025, 6, 11, 20, minute, tzinfo=MADRID)
            idx = next(i for i, s in enumerate(starts) if s >= instant.astimezone(UTC))
            assert starts[idx].astimezone(MADRID).minute == minute
            assert local_days[idx] == day
            events = compute_rolling_events(
                starts,
                local_days,
                eval_start_idx=idx,
                eval_end_idx=len(starts),
                tz=MADRID,
                replan_interval_slots=1,
                horizon_slots=96,
            )
            estimated = _estimated_days(events[0], local_days)
            if expect_estimated:
                assert estimated == [day_next], (
                    f"20:15 is inside/before the 20:15–20:20 publication window, so "
                    f"{day_next} must still be estimated; got {estimated}"
                )
            else:
                assert estimated == [], (
                    f"20:30 is the first boundary after the publication window, so "
                    f"{day_next} must be exact; got {estimated}"
                )

    @pytest.mark.parametrize("cadence", [1, 4, 24, 96])
    def test_information_set_does_not_depend_on_cadence(self, cadence: int) -> None:
        """Same decision instants, same information, whatever the replan spacing.

        The legacy afternoon solve at 14:00 and a fine-cadence solve that happens to
        land at 14:00 must agree on which days are estimated.  If a cadence mode
        quietly widened the information set, the comparison against day-ahead would be
        measuring an unrealisable oracle.
        """
        _p, _l, _u, local_days, starts = _daily_shape_window(n_days=self._SWEEP_DAYS)
        del _p, _l, _u
        # 14:00 is 56 quarter-hours after local midnight on a normal CEST day.
        four_pm = self._local_midnight_idx(local_days, self._SWEEP_DAY) + 56
        assert starts[four_pm].astimezone(MADRID).hour == AFTERNOON_EVENT[0]

        # Legacy schedule: the 14:00 afternoon event, planned to midnight ending D+1.
        legacy_events = compute_rolling_events(
            starts,
            local_days,
            eval_start_idx=four_pm,
            eval_end_idx=len(starts),
            tz=MADRID,
        )
        legacy = [ev for ev in legacy_events if ev.kind == "afternoon" and ev.start_idx == four_pm]
        assert len(legacy) == 1, "the 14:00 event must be the first legacy event in the window"
        legacy_end = legacy[0].plan_end_idx
        assert legacy_end > four_pm

        # Same horizon, so the comparison is about the information set and not about
        # how far each mode looks ahead.
        events = compute_rolling_events(
            starts,
            local_days,
            eval_start_idx=four_pm,
            eval_end_idx=legacy_end,
            tz=MADRID,
            replan_interval_slots=cadence,
            horizon_slots=legacy_end - four_pm,
        )
        at_two_pm = [ev for ev in events if ev.start_idx == four_pm]
        assert at_two_pm, f"no solve at 14:00 with cadence {cadence}"
        assert _estimated_days(at_two_pm[0], local_days) == _estimated_days(
            legacy[0], local_days
        ), f"cadence {cadence} changed which days are estimated at 14:00"

    def test_cadence_mode_never_credits_an_unpublished_tomorrow(self) -> None:
        """No interval may be priced exact unless its day was published at the instant."""
        prices, load, usable, local_days, starts = _daily_shape_window()
        del prices, load, usable
        events = compute_rolling_events(
            starts,
            local_days,
            eval_start_idx=96,
            eval_end_idx=len(starts),
            tz=MADRID,
            replan_interval_slots=4,
            horizon_slots=96,
        )
        for ev in events:
            assert ev.prices_exact_end_idx <= ev.plan_end_idx, ev
            assert ev.exec_end_idx <= ev.plan_end_idx, ev
            last_exact = _expected_last_exact_for_event(starts[ev.start_idx])
            # Every interval marked exact belongs to a day at or before the boundary,
            # and every interval past it belongs to a later (unpublished) day.
            for i in range(ev.start_idx, ev.plan_end_idx):
                interval_day = local_days[i]
                if i < ev.prices_exact_end_idx:
                    assert interval_day <= last_exact, (
                        f"interval {i} ({interval_day}) was priced exact by the solve at "
                        f"{starts[ev.start_idx].astimezone(MADRID):%H:%M}, after day "
                        f"{last_exact} was the last one published"
                    )
                else:
                    assert interval_day > last_exact, (
                        f"interval {i} ({interval_day}) was estimated although day "
                        f"{last_exact} was the last published one"
                    )


def _expected_last_exact_for_event(ev_start_utc: datetime) -> date:
    """Independent restatement of the publication rule for the sweep tests."""
    local = ev_start_utc.astimezone(MADRID)
    if (local.hour, local.minute) >= EVENING_EVENT:
        return local.date() + timedelta(days=1)
    return local.date()


# ---------------------------------------------------------------------------
# End-to-end dispatch at a configurable cadence
# ---------------------------------------------------------------------------


def _cadence_data(
    n_days: int = 2,
    start: datetime = datetime(2025, 6, 10, 0, 0, tzinfo=UTC),
    *,
    window_days: int = 1,
) -> tuple[np.ndarray, np.ndarray, np.ndarray, list[date], list[datetime], np.ndarray]:
    """Small synthetic fixture plus its frozen profile, for cadence tests.

    The window opens at index 96 so one local day sits before the cut and the frozen
    profile is a real trailing mean rather than a degenerate one.
    """
    prices, load, usable, local_days, starts = _daily_shape_window(start, n_days)
    frozen, _ = build_frozen_load_profile(
        load,
        local_days,
        usable,
        training_end_idx=96,
        window_days=window_days,
        interval_starts_utc=starts,
    )
    return prices, load, usable, local_days, starts, frozen


def _cadence_arguments(
    *,
    n_days: int = 2,
    start: datetime = datetime(2025, 6, 10, 0, 0, tzinfo=UTC),
    sigma: float = 0.0,
    seed: int = 0,
) -> dict[str, Any]:
    """Keyword arguments for :func:`run_rolling_horizon` on a small cadence fixture."""
    prices, load, usable, local_days, starts, frozen = _cadence_data(n_days, start)
    return dict(
        prices=prices,
        load_kwh=load,
        usable=usable,
        interval_seconds=QUARTER,
        local_days=local_days,
        interval_starts_utc=starts,
        eval_start_idx=96,
        eval_end_idx=len(starts),
        tz=MADRID,
        frozen_load=frozen,
        terminal_value_eur_per_kwh=0.10,
        sigma=sigma,
        seed=seed,
    )


def _cadence_run(
    *,
    n_days: int = 2,
    cadence: int = 1,
    horizon_slots: int | None = 96,
    sigma: float = 0.0,
    seed: int = 0,
    start: datetime = datetime(2025, 6, 10, 0, 0, tzinfo=UTC),
) -> Any:
    """Run the dispatcher on a small synthetic fixture at the given cadence.

    Two local days by default (96 executed intervals): enough to prove the roll
    actually dispatches at a fine cadence, small enough that the suite stays fast.
    """
    return run_rolling_horizon(
        _spec(),
        **_cadence_arguments(n_days=n_days, start=start, sigma=sigma, seed=seed),
        replan_interval_slots=cadence,
        horizon_slots=horizon_slots,
    )


class TestReplanCadenceDispatch:
    """The 15-minute-cadence mode rolls a real dispatch, at every DST shape."""

    def test_15_minute_cadence_end_to_end(self) -> None:
        result = _cadence_run(cadence=1, horizon_slots=96)
        tele = result.telemetry
        n_eval = result.schedule.grid_kwh.size

        # One solve per executed interval, and every interval executed exactly once.
        assert tele["replan_interval_slots"] == 1
        assert tele["horizon_slots"] == 96
        assert tele["solve_count"] == n_eval == 96
        assert tele["event_count"] == 96
        coverage = np.zeros(n_eval, dtype=int)
        for rec in result.per_event:
            assert rec["kind"] == "replan"
            assert rec["exec_end_idx"] <= rec["plan_end_idx"]
            coverage[rec["start_idx"] - 96 : rec["exec_end_idx"] - 96] += 1
        assert np.all(coverage == 1), (
            f"15-minute cadence coverage: {int((coverage == 0).sum())} gaps, "
            f"{int((coverage > 1).sum())} overlaps"
        )

        # Physics still holds: the battery is never outside its window, never
        # simultaneous, never exporting.
        soc = result.schedule.soc_kwh
        assert np.all(soc >= _spec().soc_bound_kwh("min") - 1e-9)
        assert np.all(soc <= _spec().soc_bound_kwh("max") + 1e-9)
        charge = np.asarray(result.schedule.charge_kwh, dtype=float)
        discharge = np.asarray(result.schedule.discharge_kwh, dtype=float)
        assert not np.any((charge > 1e-9) & (discharge > 1e-9))
        assert np.all(result.schedule.grid_kwh >= -1e-9)

    def test_15_minute_cadence_over_a_dst_fall_back_day(self) -> None:
        """2025-10-26 is 100 intervals; a slot-counted cadence must still tile it."""
        prices, load, usable, local_days, starts = _dst_fall_back_window()
        oct26 = next(i for i, d in enumerate(local_days) if d == date(2025, 10, 26))
        oct27 = next(i for i, d in enumerate(local_days) if d == date(2025, 10, 27))
        assert oct27 - oct26 == 100, "fixture drifted: the fall-back day is 100 intervals"

        frozen, _ = build_frozen_load_profile(
            load,
            local_days,
            usable,
            training_end_idx=oct26,
            window_days=1,
            interval_starts_utc=starts,
        )
        result = run_rolling_horizon(
            _spec(),
            prices=prices,
            load_kwh=load,
            usable=usable,
            interval_seconds=QUARTER,
            local_days=local_days,
            interval_starts_utc=starts,
            eval_start_idx=oct26,
            eval_end_idx=oct26 + 100,  # exactly the 25-hour local day
            tz=MADRID,
            frozen_load=frozen,
            terminal_value_eur_per_kwh=0.10,
            sigma=0.1,
            seed=42,
            replan_interval_slots=1,
            horizon_slots=96,
        )
        n_eval = result.schedule.grid_kwh.size
        assert n_eval == 100
        assert result.telemetry["event_count"] == 100
        assert result.telemetry["solve_count"] == 100

        # Every interval of the long day, repeated hour included, executed once.
        coverage = np.zeros(n_eval, dtype=int)
        for rec in result.per_event:
            coverage[rec["start_idx"] - oct26 : rec["exec_end_idx"] - oct26] += 1
        assert np.all(coverage == 1), (
            f"DST 15-minute cadence: {int((coverage == 0).sum())} gaps, "
            f"{int((coverage > 1).sum())} overlaps"
        )
        # No solve may be shorter than the slice it dispatches.
        for rec in result.per_event:
            assert rec["exec_intervals"] >= 1
            assert rec["exec_intervals"] <= rec["plan_intervals"]
        # The horizon is a fixed number of *intervals*, so it spans real elapsed time
        # uniformly across the transition — 96 slots before midnight, fewer after,
        # because the run ends with the day.
        assert max(rec["plan_intervals"] for rec in result.per_event) == 96

    def test_legacy_cadence_over_a_dst_spring_forward_day(self) -> None:
        """2026-03-29 is 92 intervals; the two-publication schedule must still tile it.

        The fall-back direction was covered end to end and this one was not, which matters
        because the two transitions fail differently: a 25-hour day tests whether the
        repeated hour is planned twice, a 23-hour day tests whether a horizon counted in
        *intervals* still reaches the next local midnight when the day is short.
        """
        prices, load, usable, local_days, starts = _dst_spring_forward_window()
        mar29 = next(i for i, d in enumerate(local_days) if d == date(2026, 3, 29))
        mar30 = next(i for i, d in enumerate(local_days) if d == date(2026, 3, 30))
        assert mar30 - mar29 == 92, "fixture drifted: the spring-forward day is 92 intervals"
        # The skipped hour is genuinely absent from the wall clock, not merely unlabelled.
        assert 2 not in {starts[i].astimezone(MADRID).hour for i in range(mar29, mar30)}

        frozen, _ = build_frozen_load_profile(
            load,
            local_days,
            usable,
            training_end_idx=mar29,
            window_days=1,
            interval_starts_utc=starts,
        )
        # Evaluate the short day *and* the day after it.  Ending the window at the close of
        # the 29th would clip every horizon there and hide the property worth testing: that
        # an evening solve on a 23-hour day still plans across midnight into a 24-hour one.
        eval_end = len(local_days)
        result = run_rolling_horizon(
            _spec(),
            prices=prices,
            load_kwh=load,
            usable=usable,
            interval_seconds=QUARTER,
            local_days=local_days,
            interval_starts_utc=starts,
            eval_start_idx=mar29,
            eval_end_idx=eval_end,  # the 23-hour day plus the whole day after it
            tz=MADRID,
            frozen_load=frozen,
            terminal_value_eur_per_kwh=0.10,
            sigma=0.1,
            seed=42,
        )
        n_eval = result.schedule.grid_kwh.size
        assert n_eval == eval_end - mar29 == 92 + 96
        assert result.telemetry["events_infeasible"] == 0
        assert result.telemetry["events_no_incumbent"] == 0

        # Every interval of both days executed exactly once: no gap where 02:00 would
        # have been, and no double-dispatch either side of it.
        coverage = np.zeros(n_eval, dtype=int)
        for rec in result.per_event:
            coverage[rec["start_idx"] - mar29 : rec["exec_end_idx"] - mar29] += 1
        assert np.all(coverage == 1), (
            f"spring-forward legacy cadence: {int((coverage == 0).sum())} gaps, "
            f"{int((coverage > 1).sum())} overlaps"
        )
        for rec in result.per_event:
            assert rec["exec_intervals"] >= 1
            assert rec["exec_intervals"] <= rec["plan_intervals"]

        # Both publications happen on the 23-hour day, and the longest plan reaches past
        # the end of that day into the next one — a horizon counted in local days, not in
        # a fixed 96 slots, so the short day does not truncate it.
        assert result.telemetry["event_count"] >= 2
        assert max(rec["plan_intervals"] for rec in result.per_event) > 92
        assert any(rec["plan_end_idx"] > mar30 for rec in result.per_event), (
            "no solve planned across the spring-forward day's midnight"
        )

    def test_15_minute_cadence_over_a_dst_spring_forward_day(self) -> None:
        """A slot-counted cadence must tile a 92-interval day exactly as it tiles a 100 one."""
        prices, load, usable, local_days, starts = _dst_spring_forward_window()
        mar29 = next(i for i, d in enumerate(local_days) if d == date(2026, 3, 29))
        mar30 = next(i for i, d in enumerate(local_days) if d == date(2026, 3, 30))

        frozen, _ = build_frozen_load_profile(
            load,
            local_days,
            usable,
            training_end_idx=mar29,
            window_days=1,
            interval_starts_utc=starts,
        )
        result = run_rolling_horizon(
            _spec(),
            prices=prices,
            load_kwh=load,
            usable=usable,
            interval_seconds=QUARTER,
            local_days=local_days,
            interval_starts_utc=starts,
            eval_start_idx=mar29,
            eval_end_idx=mar30,
            tz=MADRID,
            frozen_load=frozen,
            terminal_value_eur_per_kwh=0.10,
            sigma=0.1,
            seed=42,
            replan_interval_slots=1,
            horizon_slots=96,
        )
        n_eval = result.schedule.grid_kwh.size
        assert n_eval == 92
        # One solve per interval of the short day: the cadence counts intervals, so a
        # 23-hour day gets 92 solves and not the 96 a wall-clock reading would imply.
        assert result.telemetry["event_count"] == 92
        assert result.telemetry["solve_count"] == 92

        coverage = np.zeros(n_eval, dtype=int)
        for rec in result.per_event:
            coverage[rec["start_idx"] - mar29 : rec["exec_end_idx"] - mar29] += 1
        assert np.all(coverage == 1), (
            f"spring-forward 15-minute cadence: {int((coverage == 0).sum())} gaps, "
            f"{int((coverage > 1).sum())} overlaps"
        )
        for rec in result.per_event:
            assert rec["exec_intervals"] == 1
            assert rec["plan_intervals"] <= 96
        # Executed once, planned many times: the multiplicity that makes clipping totals
        # incomparable across cadences, restated on the short day.
        assert result.telemetry["executed_multiplicity_mean"] == pytest.approx(1.0)
        assert result.telemetry["planning_multiplicity_mean"] > 1.0
        assert result.telemetry["clipping_totals_comparable_across_cadences"] is False

    def test_cadence_coarser_than_one_still_tiles(self) -> None:
        for cadence in (2, 4, 12, 96):
            result = _cadence_run(cadence=cadence, horizon_slots=96)
            n_eval = result.schedule.grid_kwh.size
            coverage = np.zeros(n_eval, dtype=int)
            for rec in result.per_event:
                assert rec["exec_intervals"] == min(cadence, n_eval - (rec["start_idx"] - 96))
                coverage[rec["start_idx"] - 96 : rec["exec_end_idx"] - 96] += 1
            assert np.all(coverage == 1), f"cadence {cadence} left gaps or overlaps"
            assert result.telemetry["event_count"] == int(np.ceil(n_eval / cadence))

    def test_execution_is_clamped_to_the_plan_never_past_it(self) -> None:
        """A cadence coarser than the horizon cannot execute unplaned intervals.

        ``exec_end_idx`` is clamped to ``plan_end_idx``, so with ``replan_interval_slots``
        larger than ``horizon_slots`` each solve plans and executes the same short slice:
        the roll degenerates towards a fixed non-overlapping plan, and coverage still
        tiles exactly once.  The clamp is what keeps the invariant honest rather than
        pretending a longer horizon exists.
        """
        result = _cadence_run(cadence=24, horizon_slots=8)
        records = result.per_event
        for rec in records:
            assert rec["exec_end_idx"] == rec["plan_end_idx"], rec
            assert rec["plan_intervals"] == min(
                8, result.schedule.grid_kwh.size - (rec["start_idx"] - 96)
            ), rec
            assert rec["exec_intervals"] == rec["plan_intervals"]
        coverage = np.zeros(result.schedule.grid_kwh.size, dtype=int)
        for rec in records:
            coverage[rec["start_idx"] - 96 : rec["exec_end_idx"] - 96] += 1
        assert np.all(coverage == 1)
        # Planned multiplicity is exactly 1 here: no horizon overlaps the next one.
        assert result.telemetry["planning_multiplicity_max"] == 1
        assert result.telemetry["planning_multiplicity_mean"] == 1.0

    @pytest.mark.parametrize("horizon_slots", [24, 48, 96, 144, 192])
    def test_horizon_length_is_configurable(self, horizon_slots: int) -> None:
        """The plan length follows the parameter, clipped by the end of the window."""
        result = _cadence_run(cadence=4, horizon_slots=horizon_slots)
        n_eval = result.schedule.grid_kwh.size
        tele = result.telemetry
        assert tele["horizon_slots"] == horizon_slots
        assert tele["horizon_hours_nominal"] == pytest.approx(horizon_slots * QUARTER / 3600.0)
        planned = [rec["plan_intervals"] for rec in result.per_event]
        # Interior solves get the full horizon; only the tail of the window is clipped.
        assert max(planned) == min(horizon_slots, n_eval)
        for rec in result.per_event:
            expected = min(horizon_slots, n_eval - (rec["start_idx"] - 96))
            assert rec["plan_intervals"] == expected, rec
            assert rec["exec_end_idx"] <= rec["plan_end_idx"]
        # And a longer horizon really is a different plan, not the same one relabelled:
        # the reported planned-slot total grows with it.
        assert tele["planned_interval_slots_total"] == int(sum(planned))

    def test_horizon_default_is_the_adr_band(self) -> None:
        """Asking for a cadence without a horizon gives 24 h, inside ADR-0001's band."""
        result = _cadence_run(cadence=1, horizon_slots=None)
        assert result.telemetry["horizon_slots"] == 96
        assert result.telemetry["horizon_hours_nominal"] == pytest.approx(24.0)

    def test_solve_count_is_reported_and_drives_cost(self) -> None:
        """Solve count is in the artifact, because it is what a run costs."""
        fine = _cadence_run(cadence=1, horizon_slots=48)
        coarse = _cadence_run(cadence=4, horizon_slots=48)
        assert fine.telemetry["solve_count"] == 4 * coarse.telemetry["solve_count"] == 96
        assert fine.telemetry["solve_count"] <= fine.telemetry["event_count"]
        # Events that never reach the solver (skipped or frozen) are counted as such,
        # so solve_count is a lower bound a reader can trust.
        assert (
            fine.telemetry["planning_events_not_solved"]
            == fine.telemetry["event_count"] - fine.telemetry["solve_count"]
        )
        assert json.dumps(fine.telemetry, allow_nan=False)

    def test_planned_versus_executed_multiplicity_is_reported(self) -> None:
        """Multiplicity is stated explicitly so clipping is not read as curtailment."""
        fine = _cadence_run(cadence=1, horizon_slots=48)
        coarse = _cadence_run(cadence=4, horizon_slots=48)
        for result in (fine, coarse):
            tele = result.telemetry
            assert tele["executed_multiplicity_mean"] == 1.0
            assert tele["clipping_totals_comparable_across_cadences"] is False
            assert "NOT curtailment" in tele["clipping_note"]
            assert tele["planned_interval_slots_total"] >= tele["executed_interval_slots_total"]
            assert tele["planning_multiplicity_mean"] >= 1.0
            assert tele["planning_multiplicity_max"] >= tele["planning_multiplicity_mean"]

        # The inflation is real and it tracks the cadence, which is exactly why the
        # clipping kWh must not be compared across runs: same intervals executed, many
        # more overlapping plans.
        assert (
            fine.telemetry["executed_interval_slots_total"]
            == (coarse.telemetry["executed_interval_slots_total"])
        )
        assert fine.telemetry["planning_multiplicity_mean"] > (
            3 * coarse.telemetry["planning_multiplicity_mean"]
        )
        # At cadence 1 with a 48-slot horizon every interval is planned by up to 48
        # overlapping horizons.
        assert fine.telemetry["planning_multiplicity_max"] == 48

    def test_legacy_multiplicity_is_the_documented_two_and_a_half(self) -> None:
        """The control configuration reports the multiplicity the status doc recorded.

        ``hack/ROLLING_HORIZON_STATUS.md`` documents a measured planning multiplicity
        mean of ~2.55 for the two-event schedule.  A five-day run is too short for that
        mean to have converged (the warmup and the final clipped plans pull it down),
        so this asserts the same order of magnitude and the exact maximum of 4 — an
        interval inside the 14:00 and 20:30 plans of its own day and of the previous
        day's two plans.
        """
        prices, load, usable, local_days, starts = _daily_shape_window(
            datetime(2025, 6, 15, 0, 0, tzinfo=UTC), n_days=7
        )
        frozen, _ = build_frozen_load_profile(
            load,
            local_days,
            usable,
            training_end_idx=96,
            window_days=28,
            interval_starts_utc=starts,
        )
        result = run_rolling_horizon(
            _spec(),
            prices=prices,
            load_kwh=load,
            usable=usable,
            interval_seconds=QUARTER,
            local_days=local_days,
            interval_starts_utc=starts,
            eval_start_idx=96,
            eval_end_idx=len(starts),
            tz=MADRID,
            frozen_load=frozen,
            terminal_value_eur_per_kwh=0.10,
        )
        tele = result.telemetry
        assert tele["replan_interval_slots"] is None
        assert tele["solve_count"] == tele["event_count"]
        assert 2.0 < tele["planning_multiplicity_mean"] < 3.0, tele["planning_multiplicity_mean"]
        assert tele["planning_multiplicity_max"] == 4

    @pytest.mark.parametrize("bad", [0, -1])
    def test_rejects_a_nonsensical_cadence(self, bad: int) -> None:
        prices, load, usable, local_days, starts = _daily_shape_window()
        del prices, load, usable
        with pytest.raises(ValueError, match="replan_interval_slots"):
            compute_rolling_events(
                starts,
                local_days,
                eval_start_idx=0,
                eval_end_idx=96,
                tz=MADRID,
                replan_interval_slots=bad,
            )
        with pytest.raises(ValueError, match="replan_interval_slots"):
            _cadence_run(cadence=bad, horizon_slots=96)

    @pytest.mark.parametrize("bad", [0, -5])
    def test_rejects_a_nonsensical_horizon(self, bad: int) -> None:
        prices, load, usable, local_days, starts = _daily_shape_window()
        del prices, load, usable
        with pytest.raises(ValueError, match="horizon_slots"):
            compute_rolling_events(
                starts,
                local_days,
                eval_start_idx=0,
                eval_end_idx=96,
                tz=MADRID,
                replan_interval_slots=1,
                horizon_slots=bad,
            )
        with pytest.raises(ValueError, match="horizon_slots"):
            _cadence_run(cadence=1, horizon_slots=bad)

    def test_frozen_forecast_ignores_realized_future_load_at_cadence(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """No cadence may let measured future load into a plan.

        The load the solver sees comes from the frozen profile alone, which
        :meth:`TestFrozenLoadProfile.test_strict_train_cut_holds_on_partial_first_local_day`
        pins at the builder.  Here the same guarantee is checked through the dispatcher
        at a fine cadence: rewriting every post-cut measured load leaves each solve's
        forecast input bit-identical.  Realized load still bounds what a plan can do
        (clipping), so the executed dispatch is allowed to differ — the plan inputs are
        not.
        """
        from energy_baseline import rolling_horizon as rh

        prices, load, usable, local_days, starts = _daily_shape_window(
            datetime(2025, 6, 10, 0, 0, tzinfo=UTC), 3
        )
        cut = 96
        original = rh.solve_oracle

        def run(loads: np.ndarray, capture: list[np.ndarray]) -> Any:
            frozen, _ = build_frozen_load_profile(
                loads,
                local_days,
                usable,
                training_end_idx=cut,
                window_days=1,
                interval_starts_utc=starts,
            )

            def spy(day_spec, horizon, **kw):  # noqa: ANN001 - mirrors solve_oracle
                capture.append(np.asarray(horizon.load_kwh, dtype=float).copy())
                capture.append(np.asarray(horizon.prices, dtype=float).copy())
                return original(day_spec, horizon, **kw)

            monkeypatch.setattr(rh, "solve_oracle", spy)
            return rh.run_rolling_horizon(
                _spec(),
                prices=prices,
                load_kwh=loads,
                usable=usable,
                interval_seconds=QUARTER,
                local_days=local_days,
                interval_starts_utc=starts,
                eval_start_idx=cut,
                eval_end_idx=cut + 96,
                tz=MADRID,
                frozen_load=frozen,
                terminal_value_eur_per_kwh=0.10,
                replan_interval_slots=4,
                horizon_slots=48,
            )

        baseline_inputs: list[np.ndarray] = []
        baseline = run(load, baseline_inputs)
        perturbed = load.copy()
        perturbed[cut:] = 12.0
        perturbed_inputs: list[np.ndarray] = []
        run(perturbed, perturbed_inputs)

        # (forecast, prices) pairs per solve; cadence 4 over 96 intervals is 24 solves.
        assert len(baseline_inputs) == 2 * baseline.telemetry["solve_count"]
        assert baseline.telemetry["solve_count"] == 24
        for a, b in zip(baseline_inputs, perturbed_inputs, strict=True):
            assert np.array_equal(a, b), (
                "a solve's forecast or price input changed when only post-cutoff "
                "measured load changed: the plan read test data"
            )
        # Non-vacuity: the measured load really did move the executed dispatch, so the
        # equality above is about the plan inputs, not about two identical runs.
        after = run(perturbed, [])
        assert not np.array_equal(baseline.schedule.charge_kwh, after.schedule.charge_kwh)


class TestReplanCadenceInvariants:
    """The invariants the legacy schedule was debugged to satisfy hold at any cadence.

    Each check is the dispatcher's own guarantee (see ``hack/ROLLING_HORIZON_STATUS.md``
    *Defects Found And Fixed*), re-asserted with ``replan_interval_slots`` set, because
    a fine cadence changes the number of solves, the horizon overlap and the clipping
    accounting — and those are exactly the places a bug of this family hides.
    """

    def test_soc_carried_is_actual_not_planned(self) -> None:
        """Each solve starts from the real post-clipping SOC, not the plan's own.

        Verified against the traced schedule: the SOC the dispatcher records as the end
        of one event must equal the measured schedule at that interval boundary.  If the
        planned end-of-horizon SOC were carried instead, the two would diverge whenever
        clipping moved energy.
        """
        result = _cadence_run(cadence=1, horizon_slots=96, sigma=0.3, seed=2)
        records = result.per_event
        assert any(
            rec["charge_clipped_kwh"] > 0 or rec["discharge_clipped_kwh"] > 0 for rec in records
        ), "fixture produced no clipping, so actual-versus-planned is not being tested"
        # ``simulate`` reports the SOC reached at the END of each interval, so an
        # event's end-of-execution SOC must equal the schedule at its last executed
        # interval — the real state, not the plan's end-of-horizon state.
        soc = np.asarray(result.schedule.soc_kwh, dtype=float)
        assert records[0]["soc_start_kwh"] == pytest.approx(
            float(result.schedule.initial_soc_kwh), abs=1e-9
        )
        for rec in records:
            last_executed = rec["exec_end_idx"] - 1 - 96
            assert rec["soc_end_kwh"] == pytest.approx(float(soc[last_executed]), abs=1e-9), (
                f"event {rec['event_index']} carried {rec['soc_end_kwh']} kWh but the "
                f"measured schedule says {float(soc[last_executed])} at its own end: a "
                "planned SOC was carried forward instead of the actual one"
            )
        for prev, nxt in zip(records, records[1:], strict=False):
            assert nxt["soc_start_kwh"] == prev["soc_end_kwh"], (
                "the roll did not carry the SOC it actually reached"
            )
        # A planned SOC would be the plan's own end-of-horizon value, which differs
        # whenever the horizon extends past what was executed — always true here.
        assert any(rec["plan_intervals"] > rec["exec_intervals"] for rec in records), (
            "fixture never executed a short slice of a plan, so the check is vacuous"
        )

    def test_seed_floor_is_horizon_level_never_per_day(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """The causal floor stays at ``max(soc_min, initial SOC of the window)``.

        A per-event floor would let a solve spend charge that an earlier solve paid for.
        The floor handed to the solver must therefore be the SAME constant for every
        event of the roll, derived from the evaluation window's initial SOC.
        """
        from energy_baseline import rolling_horizon as rh

        captured: list[float] = []
        original = rh.solve_oracle

        def spy(day_spec, horizon, **kw):  # noqa: ANN001 - mirrors solve_oracle
            captured.append(float(kw["floor_kwh"]))
            return original(day_spec, horizon, **kw)

        monkeypatch.setattr(rh, "solve_oracle", spy)
        result = _cadence_run(cadence=1, horizon_slots=96)

        spec = _spec()
        assert captured and len(captured) == result.telemetry["solve_count"]
        assert set(captured) == {spec.causal_floor_kwh()}, (
            f"the floor varied per solve: {sorted(set(captured))}"
        )
        assert result.telemetry["horizon_floor_kwh"] == pytest.approx(spec.causal_floor_kwh())

    def test_solver_failure_degrades_and_still_tiles(self, monkeypatch: pytest.MonkeyPatch) -> None:
        """A failed solve holds SOC for its own slice; it is never back-filled."""
        from energy_baseline import rolling_horizon as rh

        prices, load, usable, local_days, starts = _daily_shape_window(n_days=2)
        frozen, _ = build_frozen_load_profile(
            load,
            local_days,
            usable,
            training_end_idx=96,
            window_days=1,
            interval_starts_utc=starts,
        )

        calls = {"n": 0}
        original = rh.solve_oracle

        def failing(day_spec, horizon, **kw):  # noqa: ANN001 - mirrors solve_oracle
            calls["n"] += 1
            if calls["n"] in (1, 2, 5):
                from energy_baseline.oracle import OracleError

                raise OracleError("Simulated solver infeasible")
            return original(day_spec, horizon, **kw)

        monkeypatch.setattr(rh, "solve_oracle", failing)
        result = rh.run_rolling_horizon(
            _spec(),
            prices=prices,
            load_kwh=load,
            usable=usable,
            interval_seconds=QUARTER,
            local_days=local_days,
            interval_starts_utc=starts,
            eval_start_idx=96,
            eval_end_idx=len(starts),
            tz=MADRID,
            frozen_load=frozen,
            terminal_value_eur_per_kwh=0.10,
            replan_interval_slots=1,
            horizon_slots=96,
        )

        failed = [rec for rec in result.per_event if rec["solver_status"] == "failed"]
        assert len(failed) == 3
        assert {rec["event_index"] for rec in failed} == {0, 1, 4}
        for rec in failed:
            assert rec["degradation"].endswith("_hold_soc"), rec["degradation"]
            assert rec["note"], "a degraded event must record why it degraded"
            assert rec["charge_kwh"] == 0.0 and rec["discharge_kwh"] == 0.0
            assert rec["soc_end_kwh"] == rec["soc_start_kwh"], (
                "a degraded event moved the SOC it did not earn"
            )
        assert result.telemetry["events_infeasible"] == 3
        # Every event still reached the solver: a failure is counted, not substituted.
        assert result.telemetry["solve_count"] == result.telemetry["event_count"] == 96

        # Failed slices are executed as zero flows, not re-planned: coverage once more.
        coverage = np.zeros(len(starts) - 96, dtype=int)
        for rec in result.per_event:
            coverage[rec["start_idx"] - 96 : rec["exec_end_idx"] - 96] += 1
        assert np.all(coverage == 1)

    def test_planning_and_scoring_terminal_values_stay_separate(self) -> None:
        """Planning values leftover charge; scoring does not."""
        result = _cadence_run(cadence=4, horizon_slots=96)
        tele = result.telemetry
        assert tele["terminal_value_eur_per_kwh"] == pytest.approx(0.10)
        # Scoring is the metered cost against TRUE prices: the shared zero scoring
        # terminal value means no inventory adjustment ever enters the reported figure.
        assert result.cost_eur == pytest.approx(result.meter_cost_eur, rel=0.0, abs=0.0)
        prices, _l, usable, _ld, _st, _fz = _cadence_data()
        grid = np.asarray(result.schedule.grid_kwh, dtype=float)
        expected = float(np.dot(prices[96:][usable[96:]], grid[usable[96:]]))
        assert result.cost_eur == pytest.approx(expected, rel=1e-9, abs=1e-9)

    def test_per_solve_time_limit_is_configurable_and_threaded(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """A fine cadence multiplies solves, so each one's budget must be settable.

        The runner's ``--time-limit`` is the knob; this checks it really reaches every
        solve at the new cadence rather than being applied once per day as before.
        """
        from energy_baseline import rolling_horizon as rh

        captured: list[float] = []
        original = rh.solve_oracle

        def spy(day_spec, horizon, **kw):  # noqa: ANN001 - mirrors solve_oracle
            captured.append(float(kw["time_limit_s"]))
            return original(day_spec, horizon, **kw)

        monkeypatch.setattr(rh, "solve_oracle", spy)
        result = _cadence_run(cadence=2, horizon_slots=24)
        assert captured and len(captured) == result.telemetry["solve_count"]
        assert set(captured) == {5.0}, "the default per-solve budget did not reach every solve"

    def test_no_wall_clock_in_the_artifact(self) -> None:
        """A fine cadence multiplies solves, but no timing may leak into the record."""
        result = _cadence_run(cadence=4, horizon_slots=48)
        payload = json.dumps(result.telemetry, allow_nan=False)
        for forbidden in ("wall_clock", "elapsed", "duration", "seconds_spent", "timestamp"):
            assert forbidden not in payload, forbidden
        assert "interval_seconds" not in result.telemetry
        # Every new cadence key is a count or a parameter, never a measurement of time.
        for key in (
            "solve_count",
            "planning_events_not_solved",
            "replan_interval_slots",
            "horizon_slots",
            "planning_multiplicity_mean",
            "planning_multiplicity_max",
            "planned_interval_slots_total",
            "executed_interval_slots_total",
            "executed_multiplicity_mean",
        ):
            assert key in result.telemetry, key

    def test_deterministic_at_a_fine_cadence(self) -> None:
        """Same seed, same run — the cadence adds no new source of randomness."""
        a = _cadence_run(cadence=2, horizon_slots=48, sigma=0.15, seed=6)
        b = _cadence_run(cadence=2, horizon_slots=48, sigma=0.15, seed=6)
        assert a.telemetry["solve_count"] == 48, "keep this fixture small"
        assert a.cost_eur == b.cost_eur
        assert np.array_equal(a.schedule.charge_kwh, b.schedule.charge_kwh)
        assert a.telemetry == b.telemetry

    def test_cadence_changes_the_plan_so_the_two_modes_are_not_the_same_policy(self) -> None:
        """Non-vacuity: a fine cadence really is a different controller.

        Without this, every cadence test above could be passing because the new mode
        silently ignored its parameters and re-ran the legacy schedule.
        """
        common = _cadence_arguments(n_days=3)
        legacy = run_rolling_horizon(_spec(), **common)
        fine = run_rolling_horizon(_spec(), **common, replan_interval_slots=2, horizon_slots=96)
        assert fine.telemetry["replan_interval_slots"] == 2
        assert fine.telemetry["solve_count"] > legacy.telemetry["solve_count"]
        assert (
            fine.telemetry["planning_multiplicity_mean"]
            > legacy.telemetry["planning_multiplicity_mean"]
        )
        assert not np.array_equal(fine.schedule.charge_kwh, legacy.schedule.charge_kwh), (
            "an exact-price fine cadence planned the same dispatch as the two-event "
            "schedule; the cadence parameter is not reaching the solver"
        )
