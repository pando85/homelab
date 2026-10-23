"""Tests for ``energy_baseline.cost`` — pre-tax cost and effective-dated taxes.

Everything here is synthetic: no network, no cluster, no real invoice and no
real tariff rates.  The tax bands and dates are invented for the test and named
as such; the shipped ``tariff.yaml`` keeps null (unconfirmed) rates.

The behaviours pinned down are the normative ones for the cost model:

* the tax band is chosen from the **local** day in the configured timezone, not
  the UTC day — a Madrid midnight lands on the previous UTC date;
* unconfirmed taxes (null rates, or a placeholder ``source``) make taxed
  ("billed") mode refuse outright; there is no partial sum, while pre-tax cost
  stays available and must be labelled as such;
* energy is ``>= 0`` and finite, prices are finite and may be negative (a
  negative price gives a negative cost, never a clamped zero);
* missing energy/price and non-exact/non-measured quality flags are *excluded and
  counted*; a measured ``0.0 kWh`` is data, not a gap;
* months are local months, so DST gives 23/25-hour days and the missing-interval
  count stays honest;
* money is never rounded per interval, taxes are applied at most once, and no
  tolls are added.

The tariff configuration is duck-typed by ``cost`` (it reads ``cfg.taxes`` and
``cfg.tariff_periods.timezone``), so most tests use local stand-ins and stay
independent of ``config.py`` churn.  One test loads a real synthetic YAML through
``config.load_config`` as a canary that the two shapes still agree.
"""

from __future__ import annotations

import inspect
import math
from dataclasses import dataclass
from datetime import UTC, date, datetime, timedelta
from pathlib import Path
from typing import Any
from zoneinfo import ZoneInfo

import numpy as np
import pyarrow as pa
import pytest

from energy_baseline import cost as cost_mod
from energy_baseline.cost import (
    ALLOCATION_EXACT,
    COST_BASIS_TAXED,
    COST_BASIS_UNTAXED,
    ENERGY_MEASURED,
    PRICE_FORWARD_FILLED,
    PRICE_MEASURED,
    PRICE_MISSING,
    USABLE_PRICE_QUALITY,
    CostError,
    CostSummary,
    TaxBoundaryError,
    TaxesUnconfirmedError,
    account_intervals,
    applicable_tax_entry,
    classify_interval,
    compute_interval_cost,
    compute_period_cost,
    compute_period_summary,
    config_timezone,
    effective_prices,
    effective_tax_multiplier,
    effective_tax_multipliers,
    local_day,
    require_constant_tax_multiplier,
    require_tax_multiplier,
    split_span_at_tax_changes,
    tax_confidence,
    tax_rate_changes,
    usable_intervals,
)
from energy_baseline.schemas import ANALYSIS_INTERVAL

MADRID = "Europe/Madrid"
JAN_BAND = date(2026, 1, 1)
FEB_BAND = date(2026, 2, 1)

#: Synthetic rates.  They are test fixtures, not claimed real Spanish taxes.
JAN_MULT = (1 + 5.0 / 100) * (1 + 21.0 / 100)  # iee 5%, iva 21%
FEB_MULT = (1 + 10.0 / 100) * (1 + 21.0 / 100)  # iee 10%, iva 21%


# ---------------------------------------------------------------------------
# Synthetic configuration stand-ins (duck-typed by cost.py)
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class Band:
    valid_from: date
    iee_pct: float | None
    iva_pct: float | None
    source: str


@dataclass(frozen=True)
class Periods:
    timezone: str = MADRID


@dataclass(frozen=True)
class Config:
    taxes: list[Band]
    tariff_periods: Periods = Periods()


def confirmed_config(timezone: str = MADRID) -> Config:
    """Two dated bands with different rates, both carrying a real source string."""
    return Config(
        taxes=[
            Band(JAN_BAND, 5.0, 21.0, "synthetic-test-band"),
            Band(FEB_BAND, 10.0, 21.0, "synthetic-test-band"),
        ],
        tariff_periods=Periods(timezone),
    )


def unconfirmed_config(timezone: str = MADRID) -> Config:
    """The shipped shape: one band with null rates and the placeholder source."""
    return Config(
        taxes=[Band(JAN_BAND, None, None, "unconfirmed")],
        tariff_periods=Periods(timezone),
    )


def utc_instant(*args: Any) -> datetime:
    return datetime(*args, tzinfo=UTC)


# ---------------------------------------------------------------------------
# A full ANALYSIS_INTERVAL-shaped row, as the analysis grid delivers it
# ---------------------------------------------------------------------------


def interval_row(**overrides: Any) -> dict[str, Any]:
    """One usable 15-minute row on the 2026-01-01 Madrid grid (00:00Z = 01:00 local)."""
    start = overrides.pop("interval_start", utc_instant(2026, 1, 1, 0, 0))
    row: dict[str, Any] = {
        "interval_start": start,
        "interval_end": start + timedelta(seconds=900),
        "tz_local": MADRID,
        "tariff_period": 3,
        "local_day": start.astimezone(ZoneInfo(MADRID)).date(),
        "dst_transition": None,
        "raw_grid_kwh": 0.25,
        "gross_kwh": 0.25,
        "battery_absent": True,
        "energy_quality": ENERGY_MEASURED,
        "allocation_quality": ALLOCATION_EXACT,
        "sample_count": 4,
        "gap_seconds": 0.0,
        "price_eur_per_kwh": 0.15,
        "price_quality": PRICE_MEASURED,
        "dataset_id": "dataset-test",
    }
    row.update(overrides)
    return row


def analysis_table(rows: list[dict[str, Any]]) -> pa.Table:
    """A pyarrow table validated against the real ANALYSIS_INTERVAL schema."""
    return pa.Table.from_pylist(rows, schema=ANALYSIS_INTERVAL)


# ===========================================================================
# 1. Single-interval arithmetic
# ===========================================================================


class TestIntervalCost:
    def test_pretax_cost_is_energy_times_price(self) -> None:
        assert compute_interval_cost(energy_kwh=2.0, price_eur_per_kwh=0.1) == pytest.approx(0.2)

    def test_zero_energy_is_data_not_a_gap(self) -> None:
        """A genuinely idle interval costs 0 EUR; it is never 'missing'."""
        assert compute_interval_cost(energy_kwh=0.0, price_eur_per_kwh=0.4) == 0.0

    def test_negative_price_gives_negative_cost(self) -> None:
        """PVPC negative prices are real and must not be clamped to zero."""
        assert compute_interval_cost(energy_kwh=2.0, price_eur_per_kwh=-0.05) == pytest.approx(-0.1)

    def test_tax_multiplier_applied_exactly_once(self) -> None:
        pretax = compute_interval_cost(energy_kwh=1.5, price_eur_per_kwh=0.2)
        taxed = compute_interval_cost(energy_kwh=1.5, price_eur_per_kwh=0.2, tax_multiplier=1.271)
        assert taxed == pytest.approx(pretax * 1.271)
        # No tolls are added on top of the multiplier: the ratio is the multiplier.
        assert taxed / pretax == pytest.approx(1.271)

    def test_negative_energy_is_rejected(self) -> None:
        """Import only: export credits are not this function's job."""
        with pytest.raises(CostError, match="grid import only"):
            compute_interval_cost(energy_kwh=-0.1, price_eur_per_kwh=0.1)

    @pytest.mark.parametrize("bad", [math.nan, math.inf, -math.inf, None, True, "not-a-number"])
    def test_non_finite_energy_is_rejected(self, bad: Any) -> None:
        with pytest.raises(CostError, match="energy_kwh"):
            compute_interval_cost(energy_kwh=bad, price_eur_per_kwh=0.1)

    @pytest.mark.parametrize("bad", [math.nan, math.inf, None, object()])
    def test_non_finite_price_is_rejected(self, bad: Any) -> None:
        with pytest.raises(CostError, match="price_eur_per_kwh"):
            compute_interval_cost(energy_kwh=1.0, price_eur_per_kwh=bad)

    def test_multiplier_below_one_is_rejected(self) -> None:
        """Non-negative fiscal rates cannot produce a multiplier under 1."""
        with pytest.raises(CostError, match="must be >= 1"):
            compute_interval_cost(energy_kwh=1.0, price_eur_per_kwh=0.1, tax_multiplier=0.9)

    def test_taxed_basis_without_a_multiplier_fails(self) -> None:
        with pytest.raises(TaxesUnconfirmedError, match="refusing to call a pre-tax figure billed"):
            compute_interval_cost(energy_kwh=1.0, price_eur_per_kwh=0.1, basis=COST_BASIS_TAXED)

    def test_untaxed_basis_with_a_multiplier_fails(self) -> None:
        """A contradiction is refused instead of silently picking one meaning."""
        with pytest.raises(CostError, match="never tax an already-taxed price"):
            compute_interval_cost(
                energy_kwh=1.0,
                price_eur_per_kwh=0.1,
                tax_multiplier=1.271,
                basis=COST_BASIS_UNTAXED,
            )

    def test_basis_inferred_matches_explicit_basis(self) -> None:
        kwargs = {"energy_kwh": 3.0, "price_eur_per_kwh": 0.2, "tax_multiplier": FEB_MULT}
        assert compute_interval_cost(**kwargs) == compute_interval_cost(
            **kwargs, basis=COST_BASIS_TAXED
        )
        assert compute_interval_cost(
            energy_kwh=3.0, price_eur_per_kwh=0.2
        ) == compute_interval_cost(energy_kwh=3.0, price_eur_per_kwh=0.2, basis=COST_BASIS_UNTAXED)

    def test_stub_signature_stays_keyword_only(self) -> None:
        """Back-compat: the stub's parameter names and keyword-only form survive."""
        params = inspect.signature(compute_interval_cost).parameters
        assert list(params) == ["energy_kwh", "price_eur_per_kwh", "tax_multiplier", "basis"]
        assert all(p.kind is p.KEYWORD_ONLY for p in params.values())
        assert params["tax_multiplier"].default is None
        with pytest.raises(TypeError):
            compute_interval_cost(1.0, 0.1)  # type: ignore[misc]

    def test_numpy_scalars_are_accepted(self) -> None:
        """The analysis grid hands over numpy/pyarrow scalars, not Python floats."""
        value = compute_interval_cost(energy_kwh=np.float64(2.0), price_eur_per_kwh=np.float64(0.1))
        assert value == pytest.approx(0.2)


# ===========================================================================
# 2. Effective-dated taxes use the local day, not the UTC day
# ===========================================================================


class TestEffectiveTaxMultiplier:
    def test_band_selected_by_local_day_not_utc_day(self) -> None:
        """2026-01-31T23:30Z is already Feb 1 in Madrid, so it uses the Feb band.

        This is the UTC bug the cost model must not inherit: the UTC date of this
        instant is January, and a UTC-day lookup would bill the January rate.
        """
        instant = utc_instant(2026, 1, 31, 23, 30)
        assert instant.date() == date(2026, 1, 31)
        assert local_day(instant, MADRID) == FEB_BAND
        assert effective_tax_multiplier(confirmed_config(), instant) == pytest.approx(FEB_MULT)

    def test_last_instant_before_local_midnight_keeps_the_earlier_band(self) -> None:
        """23:00Z Jan 31 is 00:00 local Feb 1, so 22:59:59Z is the last Jan-31 local instant."""
        instant = utc_instant(2026, 1, 31, 22, 59, 59)
        assert local_day(instant, MADRID) == date(2026, 1, 31)
        assert effective_tax_multiplier(confirmed_config(), instant) == pytest.approx(JAN_MULT)

    def test_band_lookup_uses_the_configured_timezone(self) -> None:
        """The same UTC instant resolves to different bands in different zones."""
        instant = utc_instant(2026, 1, 31, 23, 30)
        assert effective_tax_multiplier(confirmed_config("UTC"), instant) == pytest.approx(JAN_MULT)
        assert effective_tax_multiplier(
            confirmed_config("America/New_York"), instant
        ) == pytest.approx(JAN_MULT)
        assert config_timezone(confirmed_config()).key == MADRID

    def test_dst_day_boundary_is_the_local_midnight(self) -> None:
        """Madrid springs forward at 02:00 on 2026-03-29, still a CET midnight."""
        just_before = utc_instant(2026, 3, 28, 22, 59, 59)
        at_midnight = utc_instant(2026, 3, 28, 23, 0)
        assert local_day(just_before, MADRID) == date(2026, 3, 28)
        assert local_day(at_midnight, MADRID) == date(2026, 3, 29)

    def test_naive_datetime_is_refused(self) -> None:
        """Guessing a zone for a naive stamp is how the UTC bug happens."""
        with pytest.raises(CostError, match="naive"):
            effective_tax_multiplier(confirmed_config(), datetime(2026, 1, 31, 23, 30))
        with pytest.raises(CostError, match="naive"):
            local_day(datetime(2026, 1, 1), MADRID)

    def test_bare_date_is_treated_as_local_day(self) -> None:
        assert effective_tax_multiplier(confirmed_config(), FEB_BAND) == pytest.approx(FEB_MULT)
        assert effective_tax_multiplier(confirmed_config(), date(2026, 1, 15)) == pytest.approx(
            JAN_MULT
        )

    def test_date_before_the_earliest_band_has_no_rate(self) -> None:
        """Never extrapolate backwards: no band means no multiplier, not a guess."""
        assert effective_tax_multiplier(confirmed_config(), utc_instant(2025, 12, 31, 12)) is None
        assert applicable_tax_entry(confirmed_config(), utc_instant(2025, 12, 31, 12)) is None

    def test_null_rates_are_unconfirmed(self) -> None:
        assert effective_tax_multiplier(unconfirmed_config(), utc_instant(2026, 1, 15)) is None
        partial = Config(taxes=[Band(JAN_BAND, 5.0, None, "invoice 2026-01")])
        assert effective_tax_multiplier(partial, utc_instant(2026, 1, 15)) is None

    def test_unverified_source_is_unconfirmed_even_with_rates(self) -> None:
        """Rates without a meaningful source are not evidence, so taxed mode fails."""
        for source in ["unconfirmed", "unknown", "", "   ", "TODO", "placeholder", "assumed"]:
            cfg = Config(taxes=[Band(JAN_BAND, 5.0, 21.0, source)])
            assert effective_tax_multiplier(cfg, utc_instant(2026, 1, 15)) is None, source
        # The default config.load_config fills in when the key is absent:
        assert (
            effective_tax_multiplier(
                Config(taxes=[Band(JAN_BAND, 5.0, 21.0, "unknown")]), utc_instant(2026, 1, 15)
            )
            is None
        )

    def test_a_named_source_is_accepted(self) -> None:
        cfg = Config(taxes=[Band(JAN_BAND, 5.0, 21.0, "invoice 2026-01 no. 12345")])
        assert effective_tax_multiplier(cfg, utc_instant(2026, 1, 15)) == pytest.approx(JAN_MULT)

    @pytest.mark.parametrize(
        "iee,iva,match",
        [
            (-5.0, 21.0, "percentage in 0..100"),
            (5.0, -21.0, "percentage in 0..100"),
            (101.0, 21.0, "percentage in 0..100"),
            (math.nan, 21.0, "must be finite"),
        ],
    )
    def test_invalid_rates_are_a_config_error_not_an_absence(
        self, iee: float, iva: float, match: str
    ) -> None:
        """A negative/absurd rate is a bug to surface, not something to treat as null."""
        cfg = Config(taxes=[Band(JAN_BAND, iee, iva, "invoice")])
        with pytest.raises(CostError, match=match):
            effective_tax_multiplier(cfg, utc_instant(2026, 1, 15))

    def test_zero_rates_are_confirmed_and_give_one(self) -> None:
        cfg = Config(taxes=[Band(JAN_BAND, 0.0, 0.0, "invoice")])
        assert effective_tax_multiplier(cfg, utc_instant(2026, 1, 15)) == pytest.approx(1.0)

    def test_require_tax_multiplier_names_the_reason(self) -> None:
        with pytest.raises(TaxesUnconfirmedError, match="null rates"):
            require_tax_multiplier(unconfirmed_config(), utc_instant(2026, 1, 15))
        with pytest.raises(TaxesUnconfirmedError, match="unverified source"):
            require_tax_multiplier(
                Config(taxes=[Band(JAN_BAND, 5.0, 21.0, "unconfirmed")]), utc_instant(2026, 1, 15)
            )
        with pytest.raises(TaxesUnconfirmedError, match="no taxes band"):
            require_tax_multiplier(confirmed_config(), utc_instant(2025, 6, 1, 12))
        assert require_tax_multiplier(
            confirmed_config(), utc_instant(2026, 2, 1, 0, 0)
        ) == pytest.approx(FEB_MULT)

    def test_empty_tax_list_is_unconfirmed(self) -> None:
        cfg = Config(taxes=[])
        assert effective_tax_multiplier(cfg, utc_instant(2026, 1, 15)) is None
        assert tax_rate_changes(cfg) == ()

    def test_tax_confidence_describes_billed_feasibility(self) -> None:
        assert tax_confidence(unconfirmed_config())["taxed_mode_possible"] is False
        assert tax_confidence(unconfirmed_config())["confirmed_from"] is None
        confirmed = tax_confidence(confirmed_config())
        assert confirmed["taxed_mode_possible"] is True
        assert confirmed["confirmed_from"] == JAN_BAND.isoformat()
        assert confirmed["currency"] == "EUR"

    def test_tax_rate_changes_only_reports_real_changes(self) -> None:
        assert tax_rate_changes(confirmed_config()) == (JAN_BAND, FEB_BAND)
        same = Config(
            taxes=[
                Band(JAN_BAND, 5.0, 21.0, "invoice A"),
                Band(FEB_BAND, 5.0, 21.0, "invoice B"),
            ]
        )
        assert tax_rate_changes(same) == (JAN_BAND,)

    def test_stub_signature_of_tax_helper_is_pure(self) -> None:
        """cost.py owns the local-day fix; config.tax_multiplier stays untouched."""
        params = list(inspect.signature(effective_tax_multiplier).parameters)
        assert params == ["cfg", "at_datetime"]
        assert callable(cost_mod._multiplier_from_entry)


# ===========================================================================
# 3. Rate changes inside a span: split, or be refused
# ===========================================================================


class TestTaxBoundaries:
    def test_span_crossing_local_midnight_at_a_rate_change_splits(self) -> None:
        cfg = confirmed_config()
        segments = split_span_at_tax_changes(
            cfg, utc_instant(2026, 1, 31, 20), utc_instant(2026, 2, 1, 4)
        )
        assert [s.local_day for s in segments] == [date(2026, 1, 31), FEB_BAND]
        assert [s.tax_multiplier for s in segments] == [
            pytest.approx(JAN_MULT),
            pytest.approx(FEB_MULT),
        ]
        # Segments tile the span with no gap and no overlap.
        assert segments[0].start == utc_instant(2026, 1, 31, 20)
        assert segments[0].end == segments[1].start
        assert segments[1].end == utc_instant(2026, 2, 1, 4)
        # The split instant is local midnight, i.e. the previous UTC date.
        assert segments[0].end == utc_instant(2026, 1, 31, 23)

    def test_single_multiplier_across_a_rate_change_is_refused(self) -> None:
        cfg = confirmed_config()
        with pytest.raises(TaxBoundaryError, match="different multipliers"):
            require_constant_tax_multiplier(
                cfg, utc_instant(2026, 1, 31, 20), utc_instant(2026, 2, 1, 4)
            )

    def test_single_multiplier_within_one_band_is_allowed(self) -> None:
        cfg = confirmed_config()
        assert require_constant_tax_multiplier(
            cfg, utc_instant(2026, 2, 1, 4), utc_instant(2026, 2, 28, 4)
        ) == pytest.approx(FEB_MULT)

    def test_split_can_demand_confirmed_bands(self) -> None:
        cfg = Config(
            taxes=[Band(JAN_BAND, 5.0, 21.0, "invoice"), Band(FEB_BAND, None, None, "unconfirmed")]
        )
        # 22:00Z Jan 31 = 23:00 local Jan 31; 00:30Z Feb 1 = 01:30 local Feb 1
        segments = split_span_at_tax_changes(
            cfg, utc_instant(2026, 1, 31, 22), utc_instant(2026, 2, 1, 0, 30)
        )
        assert [s.local_day for s in segments] == [date(2026, 1, 31), FEB_BAND]
        assert [s.confirmed for s in segments] == [True, False]
        with pytest.raises(TaxesUnconfirmedError, match="partial taxed sum"):
            split_span_at_tax_changes(
                cfg,
                utc_instant(2026, 1, 31, 22),
                utc_instant(2026, 2, 1, 0, 30),
                require_confirmed=True,
            )

    def test_empty_span_and_naive_stamps_are_refused(self) -> None:
        cfg = confirmed_config()
        with pytest.raises(CostError, match="forward in time"):
            split_span_at_tax_changes(cfg, utc_instant(2026, 2, 1), utc_instant(2026, 2, 1))
        with pytest.raises(CostError, match="tz-aware"):
            split_span_at_tax_changes(cfg, datetime(2026, 2, 1), utc_instant(2026, 2, 2))

    def test_segments_expose_a_dict_form(self) -> None:
        segment = split_span_at_tax_changes(
            confirmed_config(), utc_instant(2026, 2, 1, 0), utc_instant(2026, 2, 2, 0)
        )[0]
        assert segment.as_dict()["local_day"] == FEB_BAND.isoformat()
        assert segment.as_dict()["confirmed"] is True


# ===========================================================================
# 4. Period sums: mask, denominator, exclusions, no partial taxed sums
# ===========================================================================


class TestPeriodCost:
    def test_sum_matches_an_independent_dot_product(self) -> None:
        energies = [0.25, 1.0, 2.0, 0.0]
        prices = [0.1, -0.05, 0.3, 0.2]
        expected = sum(e * p for e, p in zip(energies, prices, strict=True))
        assert compute_period_cost(
            energies_kwh=energies, prices_eur_per_kwh=prices
        ) == pytest.approx(expected)
        assert expected == pytest.approx(0.575)

    def test_stub_signature_stays_compatible(self) -> None:
        params = inspect.signature(compute_period_cost).parameters
        assert list(params)[:3] == ["energies_kwh", "prices_eur_per_kwh", "tax_multiplier"]
        assert all(p.kind is p.KEYWORD_ONLY for p in params.values())
        assert params["tax_multiplier"].default is None
        with pytest.raises(TypeError):
            compute_period_cost([1.0], [0.1])  # type: ignore[misc]

    def test_length_mismatch_is_refused(self) -> None:
        with pytest.raises(CostError, match="entries"):
            compute_period_cost(energies_kwh=[1.0, 2.0], prices_eur_per_kwh=[0.1])

    def test_usable_mask_selects_and_counts(self) -> None:
        summary = compute_period_summary(
            energies_kwh=[1.0, 2.0, 3.0],
            prices_eur_per_kwh=[0.1, 0.1, 0.1],
            usable=[True, False, True],
        )
        assert summary.cost_eur == pytest.approx(0.4)
        assert (summary.n_intervals, summary.n_counted, summary.n_excluded) == (3, 2, 1)
        assert summary.excluded_reasons == {"not_selected": 1}
        assert summary.coverage == pytest.approx(2 / 3)

    def test_missing_values_are_excluded_never_read_as_zero(self) -> None:
        """None means unknown; treating it as 0 kWh would fabricate a cheap month."""
        summary = compute_period_summary(
            energies_kwh=[1.0, None, 3.0], prices_eur_per_kwh=[0.1, 0.1, None]
        )
        assert summary.n_counted == 1
        assert summary.n_excluded == 2
        assert summary.excluded_reasons == {"energy_missing": 1, "price_missing": 1}
        assert summary.energy_kwh == pytest.approx(1.0)

    def test_zero_energy_is_counted_not_excluded(self) -> None:
        summary = compute_period_summary(
            energies_kwh=[0.0, 0.0, 1.0], prices_eur_per_kwh=[0.1, 0.1, 0.1]
        )
        assert summary.n_counted == 3
        assert summary.excluded_reasons == {}
        assert summary.cost_eur == pytest.approx(0.1)

    def test_negative_energy_in_a_counted_row_fails(self) -> None:
        with pytest.raises(CostError, match="must be >= 0"):
            compute_period_cost(energies_kwh=[1.0, -0.5], prices_eur_per_kwh=[0.1, 0.1])

    def test_negative_energy_selected_out_is_still_refused(self) -> None:
        """A negative import is a data error, mask or no mask: it never sums."""
        with pytest.raises(CostError, match="must be >= 0"):
            compute_period_cost(
                energies_kwh=[1.0, -0.5], prices_eur_per_kwh=[0.1, 0.1], usable=[True, False]
            )

    def test_negative_prices_keep_the_total_negative(self) -> None:
        total = compute_period_cost(energies_kwh=[2.0, 1.0], prices_eur_per_kwh=[-0.1, -0.05])
        assert total == pytest.approx(-0.25)

    def test_taxed_and_pretax_are_reported_separately(self) -> None:
        summary = compute_period_summary(
            energies_kwh=[1.0, 2.0], prices_eur_per_kwh=[0.1, 0.1], tax_multiplier=FEB_MULT
        )
        assert summary.basis == COST_BASIS_TAXED
        assert summary.tax_applied is True
        assert summary.billed is True
        assert summary.pretax_cost_eur == pytest.approx(0.3)
        assert summary.cost_eur == pytest.approx(0.3 * FEB_MULT)
        assert summary.tax_uplift_eur == pytest.approx(0.3 * (FEB_MULT - 1))
        assert summary.is_full_invoice is False
        assert summary.currency == "EUR"

    def test_taxed_basis_without_any_multiplier_fails(self) -> None:
        with pytest.raises(TaxesUnconfirmedError, match="confirmed tax multiplier"):
            compute_period_cost(
                energies_kwh=[1.0], prices_eur_per_kwh=[0.1], basis=COST_BASIS_TAXED
            )

    def test_no_partial_sum_when_a_counted_interval_is_unconfirmed(self) -> None:
        """NaN multiplier on a counted row: refuse, do not sum the confirmed rest."""
        with pytest.raises(TaxesUnconfirmedError, match="no confirmed tax multiplier"):
            compute_period_cost(
                energies_kwh=[1.0, 2.0],
                prices_eur_per_kwh=[0.1, 0.1],
                tax_multipliers=[FEB_MULT, math.nan],
            )

    def test_unconfirmed_multiplier_on_an_excluded_interval_does_not_block_taxed_mode(self) -> None:
        total = compute_period_cost(
            energies_kwh=[1.0, 2.0],
            prices_eur_per_kwh=[0.1, 0.1],
            tax_multipliers=[FEB_MULT, math.nan],
            usable=[True, False],
            basis=COST_BASIS_TAXED,
        )
        assert total == pytest.approx(1.0 * 0.1 * FEB_MULT)

    def test_per_interval_multipliers_split_a_rate_change(self) -> None:
        """Two intervals either side of a Madrid midnight use two bands."""
        # 22:45Z Jan 31 = 23:45 local Jan 31; 23:15Z Jan 31 = 00:15 local Feb 1
        starts = [utc_instant(2026, 1, 31, 22, 45), utc_instant(2026, 1, 31, 23, 15)]
        multipliers = effective_tax_multipliers(confirmed_config(), starts)
        assert multipliers[0] == pytest.approx(JAN_MULT)  # local day Jan 31
        assert multipliers[1] == pytest.approx(FEB_MULT)  # local day Feb 1
        total = compute_period_cost(
            energies_kwh=[1.0, 1.0], prices_eur_per_kwh=[0.1, 0.1], tax_multipliers=multipliers
        )
        assert total == pytest.approx(0.1 * JAN_MULT + 0.1 * FEB_MULT)
        assert total != pytest.approx(0.2 * FEB_MULT)  # not blended, not extrapolated

    def test_scalar_and_array_multipliers_are_mutually_exclusive(self) -> None:
        with pytest.raises(CostError, match="not both"):
            compute_period_cost(
                energies_kwh=[1.0],
                prices_eur_per_kwh=[0.1],
                tax_multiplier=FEB_MULT,
                tax_multipliers=[FEB_MULT],
            )

    def test_multiplier_array_length_is_checked(self) -> None:
        with pytest.raises(CostError, match="tax_multipliers must have 2 entries"):
            compute_period_cost(
                energies_kwh=[1.0, 1.0], prices_eur_per_kwh=[0.1, 0.1], tax_multipliers=[FEB_MULT]
            )

    def test_mask_length_is_checked(self) -> None:
        with pytest.raises(CostError, match="usable mask"):
            compute_period_cost(
                energies_kwh=[1.0, 1.0], prices_eur_per_kwh=[0.1, 0.1], usable=[True]
            )

    def test_money_is_not_rounded_per_interval(self) -> None:
        """2880 intervals of a third of a cent must not lose a rounding step each."""
        tiny = 1 / 3000
        total = compute_period_cost(energies_kwh=[tiny] * 3000, prices_eur_per_kwh=[0.1] * 3000)
        assert total == pytest.approx(0.1 * tiny * 3000, rel=1e-15)
        assert total > 0.0
        assert "round(" not in inspect.getsource(cost_mod.compute_period_summary)

    def test_empty_period_is_zero_with_an_explicit_denominator(self) -> None:
        summary = compute_period_summary(energies_kwh=[], prices_eur_per_kwh=[])
        assert summary.cost_eur == 0.0
        assert summary.n_intervals == 0
        assert summary.coverage == 0.0

    def test_summary_dict_form_is_report_ready(self) -> None:
        summary = compute_period_summary(energies_kwh=[1.0], prices_eur_per_kwh=[0.1])
        as_dict = summary.as_dict()
        assert as_dict["billed"] is False
        assert as_dict["basis"] == COST_BASIS_UNTAXED
        assert {
            "cost_eur",
            "pretax_cost_eur",
            "tax_uplift_eur",
            "excluded_reasons",
        } <= as_dict.keys()
        assert isinstance(summary, CostSummary)


# ===========================================================================
# 5. effective_prices() — array form, basis, numpy/pyarrow inputs
# ===========================================================================


class TestEffectivePrices:
    def test_untaxed_prices_come_back_unchanged(self) -> None:
        """No tolls added, no taxes applied: the PVPC price is the price paid."""
        starts = [utc_instant(2026, 1, 31, 23, 30), utc_instant(2026, 2, 1, 0)]
        prices = [0.1, -0.05]
        out = effective_prices(confirmed_config(), starts, prices)
        assert isinstance(out, np.ndarray)
        assert out.dtype == np.float64
        assert out.tolist() == prices

    def test_taxed_prices_use_each_intervals_local_day(self) -> None:
        # 22:45Z Jan 31 = 23:45 local Jan 31; 23:15Z Jan 31 = 00:15 local Feb 1
        starts = [utc_instant(2026, 1, 31, 22, 45), utc_instant(2026, 1, 31, 23, 15)]
        out = effective_prices(confirmed_config(), starts, [0.1, 0.1], basis=COST_BASIS_TAXED)
        assert out[0] == pytest.approx(0.1 * JAN_MULT)
        assert out[1] == pytest.approx(0.1 * FEB_MULT)

    def test_taxed_negative_price_stays_negative(self) -> None:
        out = effective_prices(
            confirmed_config(), [utc_instant(2026, 2, 1, 12)], [-0.2], basis=COST_BASIS_TAXED
        )
        assert out[0] == pytest.approx(-0.2 * FEB_MULT)
        assert out[0] < 0

    def test_taxed_mode_refuses_when_taxes_are_unconfirmed(self) -> None:
        cfg = unconfirmed_config()
        with pytest.raises(TaxesUnconfirmedError, match="unconfirmed taxes"):
            effective_prices(cfg, [utc_instant(2026, 1, 15)], [0.1], basis=COST_BASIS_TAXED)
        # ...while the pre-tax basis still works, so the pipeline is not blocked.
        assert effective_prices(cfg, [utc_instant(2026, 1, 15)], [0.1]).tolist() == [0.1]

    def test_missing_price_must_be_filtered_not_assumed(self) -> None:
        starts = [utc_instant(2026, 1, 15), utc_instant(2026, 1, 15, 0, 15)]
        with pytest.raises(CostError, match="finite"):
            effective_prices(confirmed_config(), starts, [0.1, math.nan])

    def test_pyarrow_columns_are_accepted(self) -> None:
        table = analysis_table([interval_row(), interval_row(price_eur_per_kwh=-0.03)])
        out = effective_prices(
            confirmed_config(), table.column("interval_start"), table.column("price_eur_per_kwh")
        )
        assert out.tolist() == [0.15, -0.03]

    def test_iso_strings_are_accepted(self) -> None:
        starts = ["2026-01-31T23:30:00+00:00", "2026-02-01T00:00:00+01:00"]
        out = effective_prices(confirmed_config(), starts, [0.1, 0.1], basis=COST_BASIS_TAXED)
        # Both instants are the same local day (Feb 1 in Madrid): one band.
        assert out.tolist() == [pytest.approx(0.1 * FEB_MULT)] * 2

    def test_length_mismatch_and_bad_basis_are_refused(self) -> None:
        with pytest.raises(CostError, match="starts has 1 entries, prices has 2"):
            effective_prices(confirmed_config(), [utc_instant(2026, 1, 15)], [0.1, 0.2])
        with pytest.raises(CostError, match="basis must be one of"):
            effective_prices(confirmed_config(), [utc_instant(2026, 1, 15)], [0.1], basis="billed")

    def test_numpy_datetime64_array_is_refused_rather_than_assumed_utc(self) -> None:
        stamps = np.array(["2026-01-31T23:30", "2026-02-01T00:00"], dtype="datetime64[us]")
        with pytest.raises(CostError, match="tz-less numpy datetime64"):
            effective_prices(confirmed_config(), stamps, [0.1, 0.1], basis=COST_BASIS_TAXED)

    def test_effective_tax_multipliers_marks_unconfirmed_as_nan(self) -> None:
        starts = [utc_instant(2026, 1, 15), utc_instant(2026, 2, 15)]
        cfg = Config(
            taxes=[
                Band(JAN_BAND, 5.0, 21.0, "invoice"),
                Band(FEB_BAND, None, None, "unconfirmed"),
            ]
        )
        out = effective_tax_multipliers(cfg, starts)
        assert out[0] == pytest.approx(JAN_MULT)
        assert math.isnan(out[1])


# ===========================================================================
# 6. usable_intervals() — what may enter a cost total
# ===========================================================================


class TestUsableIntervals:
    def test_default_accepts_a_strictly_clean_row(self) -> None:
        table = analysis_table([interval_row()])
        assert usable_intervals(table).tolist() == [True]

    def test_forward_filled_price_is_excluded_by_default(self) -> None:
        rows = [interval_row(), interval_row(price_quality=PRICE_FORWARD_FILLED)]
        assert usable_intervals(rows).tolist() == [True, False]

    def test_forward_filled_price_can_be_opted_in(self) -> None:
        rows = [interval_row(price_quality=PRICE_FORWARD_FILLED)]
        widened = USABLE_PRICE_QUALITY | {PRICE_FORWARD_FILLED}
        assert usable_intervals(rows, price_quality=widened).tolist() == [True]
        assert classify_interval(rows[0], price_quality=widened) is None

    def test_missing_price_is_excluded(self) -> None:
        """price_quality='missing' is excluded; a measured price that is None is also excluded."""
        rows = [interval_row(price_quality=PRICE_MISSING, price_eur_per_kwh=None)]
        assert usable_intervals(rows).tolist() == [False]
        assert classify_interval(rows[0]) == "price_quality"
        # A measured-quality row with a null price is also excluded (data missing).
        rows2 = [interval_row(price_quality=PRICE_MEASURED, price_eur_per_kwh=None)]
        assert usable_intervals(rows2).tolist() == [False]
        assert classify_interval(rows2[0]) == "price_missing"

    def test_non_measured_energy_is_excluded(self) -> None:
        for quality in ["gap_partial", "gap_total_unknown", "counter_reset_bridged"]:
            rows = [interval_row(energy_quality=quality, gross_kwh=None)]
            assert usable_intervals(rows).tolist() == [False], quality
            assert classify_interval(rows[0]) == "energy_quality"

    def test_unknown_allocation_is_excluded(self) -> None:
        rows = [interval_row(allocation_quality="unknown_gap")]
        assert usable_intervals(rows).tolist() == [False]
        assert classify_interval(rows[0]) == "allocation_quality"

    def test_null_energy_is_missing_not_zero(self) -> None:
        rows = [interval_row(gross_kwh=None)]
        assert usable_intervals(rows).tolist() == [False]
        assert classify_interval(rows[0]) == "energy_missing"

    def test_zero_energy_stays_usable(self) -> None:
        rows = [interval_row(gross_kwh=0.0)]
        assert usable_intervals(rows).tolist() == [True]
        assert classify_interval(rows[0]) is None

    def test_negative_energy_is_invalid_while_negative_price_is_not(self) -> None:
        assert classify_interval(interval_row(gross_kwh=-0.1)) == "energy_invalid"
        assert classify_interval(interval_row(price_eur_per_kwh=-0.4)) is None

    def test_negative_price_flows_through_to_a_negative_cost(self) -> None:
        rows = [interval_row(gross_kwh=2.0, price_eur_per_kwh=-0.05)]
        mask = usable_intervals(rows)
        summary = compute_period_summary(
            energies_kwh=[r["gross_kwh"] for r, keep in zip(rows, mask, strict=True) if keep],
            prices_eur_per_kwh=[
                r["price_eur_per_kwh"] for r, keep in zip(rows, mask, strict=True) if keep
            ],
        )
        assert summary.cost_eur == pytest.approx(-0.1)

    def test_mask_drives_the_period_denominator(self) -> None:
        rows = [
            interval_row(),
            interval_row(price_quality=PRICE_FORWARD_FILLED),
            interval_row(gross_kwh=0.0),
            interval_row(energy_quality="gap_total_unknown", gross_kwh=None),
        ]
        mask = usable_intervals(rows)
        assert mask.tolist() == [True, False, True, False]
        summary = compute_period_summary(
            energies_kwh=[r["gross_kwh"] for r in rows],
            prices_eur_per_kwh=[r["price_eur_per_kwh"] for r in rows],
            usable=mask,
        )
        assert summary.n_intervals == 4
        assert summary.n_counted == 2
        assert summary.n_excluded == 2

    def test_missing_quality_column_is_excluded_not_assumed_measured(self) -> None:
        row = interval_row()
        del row["price_quality"]
        assert classify_interval(row) == "price_quality"

    def test_columnar_mapping_input_is_accepted(self) -> None:
        columns = {
            "gross_kwh": [0.25, 0.5],
            "price_eur_per_kwh": [0.1, 0.2],
            "energy_quality": [ENERGY_MEASURED, ENERGY_MEASURED],
            "allocation_quality": [ALLOCATION_EXACT, "unknown_gap"],
            "price_quality": [PRICE_MEASURED, PRICE_MEASURED],
        }
        assert usable_intervals(columns).tolist() == [True, False]

    def test_nonzero_length_mismatch_is_refused(self) -> None:
        with pytest.raises(CostError, match="different lengths"):
            usable_intervals({"gross_kwh": [0.25], "price_eur_per_kwh": [0.1, 0.2]})

    def test_bad_input_shapes_are_refused(self) -> None:
        for bad in [None, 42, "rows"]:
            with pytest.raises(CostError):
                usable_intervals(bad)
        with pytest.raises(CostError, match="expected row mappings"):
            usable_intervals([[0.25, 0.1]])


# ===========================================================================
# 7. account_intervals() — local monthly accounts for the report
# ===========================================================================


class TestAccountIntervals:
    def grid(self, first: datetime, n: int, **overrides: Any) -> list[dict[str, Any]]:
        return [
            interval_row(
                interval_start=first + timedelta(seconds=900 * i),
                **overrides,
            )
            for i in range(n)
        ]

    def test_shape_and_honest_flags_for_untaxed(self) -> None:
        result = account_intervals(self.grid(utc_instant(2026, 1, 1, 0), 4), confirmed_config())
        assert result["schema"] == "cost.account.v1"
        assert result["currency"] == "EUR"
        assert result["basis"] == COST_BASIS_UNTAXED
        assert result["billed"] is False
        assert result["is_full_invoice"] is False
        assert result["timezone"] == MADRID
        assert result["interval_seconds"] == 900
        assert set(result) >= {"months", "totals", "warnings", "caveats"}
        assert result["months"]["2026-01"]["cost_eur"] == pytest.approx(4 * 0.25 * 0.15)

    def test_months_are_local_not_utc(self) -> None:
        """22:45Z Jan 31 is 23:45 local Jan 31; 23:15Z Jan 31 is 00:15 local Feb 1."""
        rows = [
            interval_row(interval_start=utc_instant(2026, 1, 31, 22, 45)),
            interval_row(interval_start=utc_instant(2026, 1, 31, 23, 15)),
        ]
        result = account_intervals(rows, confirmed_config())
        assert sorted(result["months"]) == ["2026-01", "2026-02"]
        assert result["months"]["2026-01"]["observed_intervals"] == 1
        assert result["months"]["2026-02"]["observed_intervals"] == 1
        assert result["months"]["2026-02"]["first_local_day"] == FEB_BAND.isoformat()

    def test_expected_intervals_are_dst_correct_for_local_months(self) -> None:
        """March 2026 loses an hour in Madrid, October gains one; 31*96 is wrong."""
        result = account_intervals([], confirmed_config())
        # Empty input has no months; ask for the counts through the helpers below.
        assert result["months"] == {}
        from energy_baseline.cost import _expected_intervals  # noqa: PLC0415 - white-box check

        tz = ZoneInfo(MADRID)
        assert _expected_intervals(tz, 2026, 1, 900) == 31 * 96
        # 23-hour day loses 1h = 4 intervals
        assert _expected_intervals(tz, 2026, 3, 900) == 31 * 96 - 4
        # 25-hour day gains 1h = 4 intervals
        assert _expected_intervals(tz, 2026, 10, 900) == 31 * 96 + 4
        assert _expected_intervals(ZoneInfo("UTC"), 2026, 3, 900) == 31 * 96

    def test_missing_time_is_declared_not_zeroed(self) -> None:
        full = self.grid(utc_instant(2026, 1, 1, 0), 4)
        # Drop the middle interval: its time has no row at all.
        rows = [full[0], full[2], full[3]]
        result = account_intervals(rows, confirmed_config())
        january = result["months"]["2026-01"]
        assert january["observed_intervals"] == 3
        assert january["missing_intervals"] == january["expected_intervals"] - 3
        assert january["missing_hours"] == pytest.approx(
            (january["missing_intervals"] * 900) / 3600
        )
        assert any("no row at all" in warning for warning in result["warnings"])

    def test_gaps_are_excluded_with_reason_counts(self) -> None:
        rows = [
            interval_row(interval_start=utc_instant(2026, 1, 1, 0)),
            interval_row(
                interval_start=utc_instant(2026, 1, 1, 0, 15), price_quality=PRICE_FORWARD_FILLED
            ),
            interval_row(
                interval_start=utc_instant(2026, 1, 1, 0, 30),
                energy_quality="gap_total_unknown",
                gross_kwh=None,
            ),
            interval_row(interval_start=utc_instant(2026, 1, 1, 0, 45), gross_kwh=0.0),
        ]
        result = account_intervals(rows, confirmed_config())
        january = result["months"]["2026-01"]
        assert january["observed_intervals"] == 4
        assert january["usable_intervals"] == 2
        assert january["excluded_intervals"] == 2
        assert january["excluded_reasons"] == {"price_quality": 1, "energy_quality": 1}
        assert january["energy_kwh"] == pytest.approx(0.25)
        assert any("excluded" in warning for warning in result["warnings"])

    def test_zero_energy_row_is_accounted_as_data(self) -> None:
        rows = [
            interval_row(interval_start=utc_instant(2026, 1, 1, 0), gross_kwh=0.0),
            interval_row(interval_start=utc_instant(2026, 1, 1, 0, 15), gross_kwh=None),
        ]
        result = account_intervals(rows, confirmed_config())
        january = result["months"]["2026-01"]
        assert january["usable_intervals"] == 1
        assert january["excluded_reasons"] == {"energy_missing": 1}
        assert january["mean_price_eur_per_kwh"] is None  # no energy to weight by

    def test_negative_price_month_can_cost_negative(self) -> None:
        rows = self.grid(utc_instant(2026, 2, 1, 0), 3, price_eur_per_kwh=-0.05)
        result = account_intervals(rows, confirmed_config())
        assert result["months"]["2026-02"]["cost_eur"] == pytest.approx(3 * 0.25 * -0.05)
        assert result["months"]["2026-02"]["cost_eur"] < 0

    def test_taxed_basis_splits_at_the_rate_change_day(self) -> None:
        # 22:45Z Jan 31 = 23:45 local Jan 31; 23:15Z Jan 31 = 00:15 local Feb 1
        rows = [
            interval_row(interval_start=utc_instant(2026, 1, 31, 22, 45)),
            interval_row(interval_start=utc_instant(2026, 1, 31, 23, 15)),
        ]
        result = account_intervals(rows, confirmed_config(), basis=COST_BASIS_TAXED)
        jan = result["months"]["2026-01"]
        feb = result["months"]["2026-02"]
        assert jan["tax_multipliers"] == [pytest.approx(JAN_MULT)]
        assert feb["tax_multipliers"] == [pytest.approx(FEB_MULT)]
        assert jan["cost_eur"] == pytest.approx(0.25 * 0.15 * JAN_MULT)
        assert feb["cost_eur"] == pytest.approx(0.25 * 0.15 * FEB_MULT)

    def test_taxed_basis_within_one_month_uses_two_bands_and_warns(self) -> None:
        rows = self.grid(utc_instant(2026, 1, 31, 0), 96)  # a whole local Feb 1, crossing midnight
        result = account_intervals(rows, confirmed_config(), basis=COST_BASIS_TAXED)
        assert sorted(result["months"]) == ["2026-01", "2026-02"]

    def test_taxed_basis_refuses_when_taxes_are_unconfirmed(self) -> None:
        rows = self.grid(utc_instant(2026, 1, 1, 0), 3)
        with pytest.raises(TaxesUnconfirmedError, match="null rates"):
            account_intervals(rows, unconfirmed_config(), basis=COST_BASIS_TAXED)

    def test_taxed_mode_never_partially_sums_a_month(self) -> None:
        """A month straddling a confirmed and an unconfirmed band fails as a whole."""
        cfg = Config(
            taxes=[Band(JAN_BAND, 5.0, 21.0, "invoice"), Band(FEB_BAND, None, None, "unconfirmed")]
        )
        rows = [
            interval_row(interval_start=utc_instant(2026, 1, 31, 23, 15)),
            interval_row(interval_start=utc_instant(2026, 1, 31, 23, 30)),
        ]
        with pytest.raises(TaxesUnconfirmedError):
            account_intervals(rows, cfg, basis=COST_BASIS_TAXED)

    def test_no_double_taxing_between_basis_and_multiplier(self) -> None:
        rows = self.grid(utc_instant(2026, 2, 1, 0), 4)
        taxed = account_intervals(rows, confirmed_config(), basis=COST_BASIS_TAXED)
        untaxed = account_intervals(rows, confirmed_config())
        fee = taxed["months"]["2026-02"]
        base = untaxed["months"]["2026-02"]
        assert fee["pretax_cost_eur"] == pytest.approx(base["cost_eur"])
        assert fee["cost_eur"] == pytest.approx(base["cost_eur"] * FEB_MULT)
        assert fee["tax_uplift_eur"] == pytest.approx(fee["cost_eur"] - fee["pretax_cost_eur"])

    def test_totals_reconcile_with_the_months(self) -> None:
        rows = self.grid(utc_instant(2026, 1, 1, 0), 200) + self.grid(
            utc_instant(2026, 2, 1, 0), 50
        )
        result = account_intervals(rows, confirmed_config(), basis=COST_BASIS_TAXED)
        totals = result["totals"]
        assert totals["observed_intervals"] == 250
        assert totals["usable_intervals"] == 250
        assert totals["cost_eur"] == pytest.approx(
            sum(m["cost_eur"] for m in result["months"].values())
        )
        assert totals["pretax_cost_eur"] == pytest.approx(
            sum(m["pretax_cost_eur"] for m in result["months"].values())
        )
        assert totals["energy_kwh"] == pytest.approx(250 * 0.25)
        assert totals["tax_multipliers"] == [pytest.approx(JAN_MULT), pytest.approx(FEB_MULT)]

    def test_months_are_sorted_and_local_days_are_iso_strings(self) -> None:
        rows = self.grid(utc_instant(2026, 3, 1, 0), 200) + self.grid(
            utc_instant(2026, 1, 1, 0), 10
        )
        result = account_intervals(rows, confirmed_config())
        assert list(result["months"]) == sorted(result["months"])
        for account in result["months"].values():
            date.fromisoformat(account["first_local_day"])
            date.fromisoformat(account["last_local_day"])

    def test_a_pyarrow_table_input_works_like_rows(self) -> None:
        rows = self.grid(utc_instant(2026, 1, 1, 0), 4)
        from_table = account_intervals(analysis_table(rows), confirmed_config())
        from_rows = account_intervals(rows, confirmed_config())
        assert from_table["totals"]["cost_eur"] == pytest.approx(from_rows["totals"]["cost_eur"])

    def test_duplicate_interval_start_is_refused(self) -> None:
        """Double-counted time would silently inflate the bill."""
        rows = [
            interval_row(interval_start=utc_instant(2026, 1, 1, 0)),
            interval_row(interval_start=utc_instant(2026, 1, 1, 0)),
        ]
        with pytest.raises(CostError, match="appears twice"):
            account_intervals(rows, confirmed_config())

    def test_inconsistent_interval_length_is_refused(self) -> None:
        rows = [
            interval_row(
                interval_start=utc_instant(2026, 1, 1, 0),
                interval_end=utc_instant(2026, 1, 1, 0, 30),
            )
        ]
        with pytest.raises(CostError, match=r"lasts 1800\.0s"):
            account_intervals(rows, confirmed_config())

    def test_naive_or_missing_timestamps_are_refused(self) -> None:
        with pytest.raises(CostError, match="tz-aware"):
            account_intervals(
                [interval_row(interval_start=datetime(2026, 1, 1))], confirmed_config()
            )
        row = interval_row()
        del row["interval_start"]
        with pytest.raises(CostError, match="interval_start"):
            account_intervals([row], confirmed_config())

    def test_bad_basis_and_grid_length_are_refused(self) -> None:
        with pytest.raises(CostError, match="basis must be one of"):
            account_intervals([], confirmed_config(), basis="gross")
        with pytest.raises(CostError, match="interval_seconds must be positive"):
            account_intervals([], confirmed_config(), interval_seconds=0)

    def test_a_grid_that_does_not_tile_the_timezone_is_refused(self) -> None:
        """A non-standard interval_seconds that doesn't divide the month is refused."""
        row = interval_row(interval_start=utc_instant(2026, 1, 1, 0))
        del row["interval_end"]  # skip the per-row length check; hit the tile check
        with pytest.raises(CostError, match="does not tile"):
            account_intervals([row], confirmed_config(), interval_seconds=7)

    def test_empty_input_is_honest_about_having_nothing(self) -> None:
        result = account_intervals([], confirmed_config())
        assert result["months"] == {}
        assert result["totals"]["cost_eur"] == 0.0
        assert result["totals"]["observed_intervals"] == 0
        assert any("empty by absence" in warning for warning in result["warnings"])

    def test_caveats_cover_the_limits_the_report_must_state(self) -> None:
        joined = " ".join(account_intervals([], confirmed_config())["caveats"])
        assert "fixed charges" in joined
        assert "invoice" in joined
        assert "tolls" in joined
        assert "never estimated" in joined


# ===========================================================================
# 8. Integration with the real config loader (canary on the shared shapes)
# ===========================================================================

SYNTHETIC_YAML = """\
version: 1
taxes:
  # Synthetic rates for tests only — not real Spanish tax rates.
  - valid_from: "2026-01-01"
    iee_pct: 5.0
    iva_pct: 21.0
    source: "synthetic test band"
  - valid_from: "2026-02-01"
    iee_pct: null
    iva_pct: null
    source: "unconfirmed"
tariff_periods:
  timezone: "Europe/Madrid"
  rules:
    weekday:
      - { period: 3, start_hour: 0, end_hour: 24 }
    saturday:
      - { period: 3, start_hour: 0, end_hour: 24 }
    sunday:
      - { period: 3, start_hour: 0, end_hour: 24 }
  holidays: []
  holiday_day_type: "sunday"
battery:
  capacity_kwh: 5.12
  power_kw: 2.5
  round_trip_efficiency: null
  soc_window_min: null
  soc_window_max: null
  import_limit_kw: null
"""


class TestWithRealConfig:
    @pytest.fixture
    def synthetic_cfg(self, tmp_path: Path) -> Any:
        from energy_baseline.config import load_config

        path = tmp_path / "tariff.yaml"
        path.write_text(SYNTHETIC_YAML)
        return load_config(path)

    def test_loader_shape_satisfies_the_duck_typed_contract(self, synthetic_cfg: Any) -> None:
        assert config_timezone(synthetic_cfg).key == MADRID
        assert effective_tax_multiplier(synthetic_cfg, utc_instant(2026, 1, 15)) == pytest.approx(
            JAN_MULT
        )
        assert effective_tax_multiplier(synthetic_cfg, utc_instant(2026, 2, 15)) is None

    def test_taxed_mode_stops_at_the_unconfirmed_band(self, synthetic_cfg: Any) -> None:
        rows = [
            interval_row(interval_start=utc_instant(2026, 1, 31, 23, 15)),
            interval_row(interval_start=utc_instant(2026, 1, 31, 23, 30)),
        ]
        assert account_intervals(rows, synthetic_cfg)["totals"]["cost_eur"] > 0
        with pytest.raises(TaxesUnconfirmedError, match="null rates"):
            account_intervals(rows, synthetic_cfg, basis=COST_BASIS_TAXED)

    def test_shipped_tariff_yaml_stays_unconfirmed(self) -> None:
        """The committed config must still refuse billed figures: no invented rates."""
        from energy_baseline.config import load_config

        shipped = Path(__file__).resolve().parents[1] / "config" / "tariff.yaml"
        if not shipped.exists():  # pragma: no cover - repo layout guard
            pytest.skip(f"{shipped} not present")
        cfg = load_config(shipped)
        assert tax_confidence(cfg)["taxed_mode_possible"] is False, (
            "tariff.yaml now carries confirmed rates; billed reporting is only honest once an "
            "actual invoice was reconciled — update this test deliberately"
        )
        rows = [interval_row(interval_start=utc_instant(2026, 1, 15, 10))]
        result = account_intervals(rows, cfg)
        assert result["billed"] is False
        assert result["totals"]["cost_eur"] == pytest.approx(0.25 * 0.15)
        with pytest.raises(TaxesUnconfirmedError):
            account_intervals(rows, cfg, basis=COST_BASIS_TAXED)

    def test_public_surface_is_exported(self) -> None:
        assert set(cost_mod.__all__) <= set(dir(cost_mod))
        for name in ("compute_interval_cost", "compute_period_cost", "effective_tax_multiplier"):
            assert name in cost_mod.__all__

    def test_reason_codes_are_documented(self) -> None:
        assert set(cost_mod.EXCLUSION_REASONS) == {
            "energy_missing",
            "energy_invalid",
            "energy_quality",
            "allocation_quality",
            "price_missing",
            "price_invalid",
            "price_quality",
            "not_selected",
        }
        assert cost_mod.USABLE_ENERGY_QUALITY == frozenset({ENERGY_MEASURED})
        assert cost_mod.INTERVAL_SECONDS == 900

    def test_no_private_cross_module_reach(self) -> None:
        """cost.py reads only cfg.taxes and cfg.tariff_periods.timezone."""
        source = inspect.getsource(cost_mod)
        for forbidden in ("cfg.battery", "cfg.tariff_periods.rules", "cfg.version"):
            assert forbidden not in source, forbidden

    def test_module_does_not_mutate_config_or_round_money(self) -> None:
        source = inspect.getsource(cost_mod)
        assert "import energy_baseline.config" not in source  # TYPE_CHECKING only
        assert "from energy_baseline.config import" in source  # guarded, typing only
        assert "round(" not in source.replace("rounding", "")
