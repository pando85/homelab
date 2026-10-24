"""Tests for the dispatch intent contract (TODO-4.6).

Covers: the intent field list and its types; byte-identical canonical
round-trips; expiry rejection; strictly-increasing sequence enforcement; DST
92-/100-interval days; determinism with no wall-clock read anywhere in the
module; the bridge from the ``Schedule`` shapes the pipeline already produces;
the import ceiling as an observation rather than a rejection; one-line error
messages with nothing swallowed; and the TODO-6.3 command-policy affordances.

Synthetic fixtures only — every number here is invented, none is household data.
"""

from __future__ import annotations

import ast
import dataclasses
import json
from datetime import UTC, datetime, timedelta, timezone
from pathlib import Path
from zoneinfo import ZoneInfo

import numpy as np
import pytest

from energy_baseline.battery import BatteryPhysicsError, BatterySpec, Horizon, Schedule, simulate
from energy_baseline.dispatch import (
    INTENT_FIELDS,
    CommandPolicy,
    DispatchError,
    DispatchIntent,
    DispatchMode,
    DispatchPlan,
    canonical_json,
    content_hash,
    dispatch_intents_arrow_schema,
    intents_from_schedule,
    intents_to_tabular,
    plan_from_canonical_json,
    plan_to_canonical_json,
)

QUARTER = 900.0
EPOCH = datetime(2026, 6, 15, 12, 0, 0, tzinfo=UTC)

#: Source of the module under test, read once for the structural (AST) checks.
_DISPATCH_SOURCE = Path(
    __import__("energy_baseline.dispatch", fromlist=["__file__"]).__file__
).read_text(encoding="utf-8")


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _spec(**overrides) -> BatterySpec:
    """Provisional 5.12 kWh / 2.5 kW nameplate — the project's stated candidate."""
    base = dict(
        capacity_kwh=5.12,
        power_kw=2.5,
        charge_efficiency=0.9,
        discharge_efficiency=0.9,
        soc_min_frac=0.1,
        soc_max_frac=0.95,
        initial_soc_frac=0.5,
    )
    base.update(overrides)
    return BatterySpec(**base)


def _horizon(
    n: int = 4,
    *,
    interval_seconds: float = QUARTER,
    load: list[float] | None = None,
    prices: list[float] | None = None,
    import_limit_kw: float | None = None,
) -> Horizon:
    return Horizon(
        interval_seconds=interval_seconds,
        prices=[0.10] * n if prices is None else prices,
        load_kwh=[0.2] * n if load is None else load,
        usable=[True] * n,
        import_limit_kw=import_limit_kw,
    )


def _starts(n: int, interval_seconds: float = QUARTER, origin: datetime = EPOCH) -> list[datetime]:
    """A contiguous aware-UTC grid."""
    return [origin + timedelta(seconds=interval_seconds * i) for i in range(n)]


def _intent(
    *,
    offset_minutes: int = 0,
    interval_seconds: float = QUARTER,
    mode: DispatchMode = DispatchMode.IDLE,
    power_w: float = 0.0,
    soc_frac: float = 0.5,
    price: float | None = None,
    confidence: float | None = None,
    generated_at: datetime | None = None,
    expires_at: datetime | None = None,
    sequence_id: int = 0,
    plan_sequence: int = 0,
) -> DispatchIntent:
    gen = generated_at if generated_at is not None else EPOCH
    exp = expires_at if expires_at is not None else gen + timedelta(hours=1)
    start = gen + timedelta(minutes=offset_minutes)
    return DispatchIntent(
        start=start,
        end=start + timedelta(seconds=interval_seconds),
        interval_seconds=interval_seconds,
        mode=mode,
        target_power_w=power_w,
        soc_target_frac=soc_frac,
        generated_at=gen,
        expires_at=exp,
        sequence_id=sequence_id,
        plan_sequence=plan_sequence,
        expected_buy_price_eur_per_kwh=price,
        confidence=confidence,
    )


def _idle_run(
    n: int,
    *,
    plan_sequence: int = 0,
    generated_at: datetime = EPOCH,
    lifetime_minutes: int = 60,
    interval_seconds: float = QUARTER,
) -> DispatchPlan:
    """A gapless run of ``n`` idle intents, bound to one generation."""
    expiry = generated_at + timedelta(minutes=lifetime_minutes)
    intents = tuple(
        _intent(
            offset_minutes=int(index * interval_seconds / 60.0),
            interval_seconds=interval_seconds,
            sequence_id=index,
            plan_sequence=plan_sequence,
            generated_at=generated_at,
            expires_at=expiry,
        )
        for index in range(n)
    )
    return DispatchPlan(
        intents=intents,
        generated_at=generated_at,
        plan_sequence=plan_sequence,
        expires_at=expiry,
        interval_seconds=interval_seconds,
    )


def _bridge(sched, horizon, spec, **kw) -> DispatchPlan:
    """``intents_from_schedule`` with the mandatory arguments filled in."""
    n = horizon.n
    kwargs: dict = {
        "battery_spec": spec,
        "interval_starts_utc": _starts(n),
        "generated_at": EPOCH,
        "expires_at": EPOCH + timedelta(hours=2),
    }
    kwargs.update(kw)
    return intents_from_schedule(sched, horizon, **kwargs)


def _slice_schedule(sched: Schedule, start: int, stop: int) -> Schedule:
    """A schedule restricted to ``[start, stop)`` — every array sliced together.

    ``Schedule`` cross-checks its own array lengths, so slicing one array alone
    is not a thing that can be constructed.
    """
    return dataclasses.replace(
        sched,
        charge_kwh=sched.charge_kwh[start:stop],
        discharge_kwh=sched.discharge_kwh[start:stop],
        soc_kwh=sched.soc_kwh[start:stop],
        grid_kwh=sched.grid_kwh[start:stop],
    )


# ---------------------------------------------------------------------------
# 1. The contract's field list: every field present and typed
# ---------------------------------------------------------------------------


class TestFieldList:
    def test_field_list_is_the_declared_contract(self) -> None:
        assert INTENT_FIELDS == (
            "start",
            "end",
            "interval_seconds",
            "mode",
            "target_power_w",
            "soc_target_frac",
            "expected_buy_price_eur_per_kwh",
            "confidence",
            "generated_at",
            "expires_at",
            "sequence_id",
            "plan_sequence",
        )

    def test_constructor_arguments_are_exactly_the_declared_fields(self) -> None:
        """One field list drives constructor, JSON keys and Parquet columns."""
        declared = {field.name for field in dataclasses.fields(DispatchIntent)}
        assert declared == set(INTENT_FIELDS)

    @pytest.mark.parametrize(
        ("field", "expected"),
        [
            ("start", datetime),
            ("end", datetime),
            ("interval_seconds", float),
            ("mode", DispatchMode),
            ("target_power_w", float),
            ("soc_target_frac", float),
            ("generated_at", datetime),
            ("expires_at", datetime),
            ("sequence_id", int),
            ("plan_sequence", int),
        ],
    )
    def test_required_field_types(self, field: str, expected: type) -> None:
        intent = _intent(mode=DispatchMode.CHARGE, power_w=1000.0, price=0.15, confidence=0.8)
        assert isinstance(getattr(intent, field), expected)

    def test_optional_fields_are_none_when_absent(self) -> None:
        intent = _intent()
        assert intent.expected_buy_price_eur_per_kwh is None
        assert intent.confidence is None

    def test_every_instant_is_aware_utc(self) -> None:
        intent = _intent(mode=DispatchMode.CHARGE, power_w=1000.0)
        for name in ("start", "end", "generated_at", "expires_at"):
            value: datetime = getattr(intent, name)
            assert value.tzinfo is not None, f"{name} is naive"
            assert value.utcoffset() == timedelta(0), f"{name} is not UTC"

    def test_absent_price_is_not_a_zero_price(self) -> None:
        """A planner with no belief must not be readable as a published 0.0 EUR/kWh."""
        assert _intent().expected_buy_price_eur_per_kwh is None
        assert _intent(price=0.0).expected_buy_price_eur_per_kwh == 0.0

    def test_absent_confidence_is_not_zero_or_one(self) -> None:
        assert _intent().confidence is None
        assert _intent(confidence=0.0).confidence == 0.0
        assert _intent(confidence=1.0).confidence == 1.0

    def test_negative_price_belief_is_allowed(self) -> None:
        """PVPC publishes negative hours; a -0.05 belief is real information."""
        assert _intent(price=-0.05).expected_buy_price_eur_per_kwh == pytest.approx(-0.05)

    def test_absent_in_json_stays_absent(self) -> None:
        """Absent beliefs are JSON null, never a materialised default."""
        payload = json.loads(plan_to_canonical_json(_idle_run(1)))
        intent = payload["intents"][0]
        assert intent["expected_buy_price_eur_per_kwh"] is None
        assert intent["confidence"] is None


# ---------------------------------------------------------------------------
# 2. Intent validation
# ---------------------------------------------------------------------------


class TestIntentValidation:
    def test_naive_instant_rejected_not_assumed_utc(self) -> None:
        with pytest.raises(DispatchError, match="must be timezone-aware UTC"):
            DispatchIntent(
                start=datetime(2026, 1, 1),
                end=EPOCH + timedelta(seconds=QUARTER),
                interval_seconds=QUARTER,
                mode=DispatchMode.IDLE,
                target_power_w=0.0,
                soc_target_frac=0.5,
                generated_at=EPOCH,
                expires_at=EPOCH + timedelta(hours=1),
            )

    def test_non_datetime_rejected(self) -> None:
        with pytest.raises(DispatchError, match="start must be an aware datetime"):
            _intent().bound_to(0)  # sanity: binding a valid intent works
            DispatchIntent(
                start="2026-06-15T12:00:00Z",  # type: ignore[arg-type]
                end=EPOCH + timedelta(seconds=QUARTER),
                interval_seconds=QUARTER,
                mode=DispatchMode.IDLE,
                target_power_w=0.0,
                soc_target_frac=0.5,
                generated_at=EPOCH,
                expires_at=EPOCH + timedelta(hours=1),
            )

    def test_end_before_start_rejected(self) -> None:
        with pytest.raises(DispatchError, match="must be after start"):
            DispatchIntent(
                start=EPOCH + timedelta(minutes=15),
                end=EPOCH,
                interval_seconds=QUARTER,
                mode=DispatchMode.IDLE,
                target_power_w=0.0,
                soc_target_frac=0.5,
                generated_at=EPOCH,
                expires_at=EPOCH + timedelta(hours=1),
            )

    def test_interval_length_must_match_the_interval(self) -> None:
        with pytest.raises(DispatchError, match="disagrees with end-start"):
            DispatchIntent(
                start=EPOCH,
                end=EPOCH + timedelta(minutes=15),
                interval_seconds=1800.0,
                mode=DispatchMode.IDLE,
                target_power_w=0.0,
                soc_target_frac=0.5,
                generated_at=EPOCH,
                expires_at=EPOCH + timedelta(hours=1),
            )

    def test_negative_power_rejected(self) -> None:
        with pytest.raises(DispatchError, match=r"target_power_w must be >= 0"):
            _intent(mode=DispatchMode.CHARGE, power_w=-100.0)

    def test_idle_with_nonzero_power_rejected(self) -> None:
        with pytest.raises(DispatchError, match="idle requires target_power_w == 0"):
            DispatchIntent(
                start=EPOCH,
                end=EPOCH + timedelta(seconds=QUARTER),
                interval_seconds=QUARTER,
                mode=DispatchMode.IDLE,
                target_power_w=100.0,
                soc_target_frac=0.5,
                generated_at=EPOCH,
                expires_at=EPOCH + timedelta(hours=1),
            )

    def test_charge_with_zero_power_rejected(self) -> None:
        """A charge intent commanding nothing would be counted as work done."""
        with pytest.raises(DispatchError, match="charge requires target_power_w > 0"):
            DispatchIntent(
                start=EPOCH,
                end=EPOCH + timedelta(seconds=QUARTER),
                interval_seconds=QUARTER,
                mode=DispatchMode.CHARGE,
                target_power_w=0.0,
                soc_target_frac=0.5,
                generated_at=EPOCH,
                expires_at=EPOCH + timedelta(hours=1),
            )

    @pytest.mark.parametrize("soc", [-0.001, 1.001, 2.0])
    def test_soc_target_must_be_a_fraction(self, soc: float) -> None:
        with pytest.raises(DispatchError, match=r"soc_target_frac must be in \[0, 1\]"):
            _intent(soc_frac=soc)

    @pytest.mark.parametrize("confidence", [-0.01, 1.01])
    def test_confidence_must_be_a_probability(self, confidence: float) -> None:
        with pytest.raises(DispatchError, match=r"confidence must be in \[0, 1\]"):
            _intent(confidence=confidence)

    def test_non_finite_number_rejected(self) -> None:
        """NaN/inf cannot survive canonical JSON, so they are rejected at the door."""
        with pytest.raises(DispatchError, match="must be finite"):
            _intent(mode=DispatchMode.CHARGE, power_w=float("nan"))
        with pytest.raises(DispatchError, match="must be finite"):
            _intent(price=float("inf"))

    def test_expiry_must_follow_generation(self) -> None:
        with pytest.raises(DispatchError, match="expires_at .* must be after generated_at"):
            _intent(generated_at=EPOCH, expires_at=EPOCH - timedelta(seconds=1))

    def test_negative_sequence_id_rejected(self) -> None:
        with pytest.raises(DispatchError, match=r"sequence_id must be >= 0"):
            _intent(sequence_id=-1)

    def test_bool_is_not_a_sequence_id(self) -> None:
        """``True`` is an ``int`` in Python and would silently mean generation 1."""
        with pytest.raises(DispatchError, match="sequence_id must be an int"):
            _intent(sequence_id=True)  # type: ignore[arg-type]

    def test_unusable_mode_rejected(self) -> None:
        with pytest.raises(DispatchError, match="mode must be one of idle/charge/discharge"):
            DispatchIntent(
                start=EPOCH,
                end=EPOCH + timedelta(seconds=QUARTER),
                interval_seconds=QUARTER,
                mode="boost",  # type: ignore[arg-type]
                target_power_w=0.0,
                soc_target_frac=0.5,
                generated_at=EPOCH,
                expires_at=EPOCH + timedelta(hours=1),
            )

    def test_mode_accepts_its_wire_string(self) -> None:
        intent = DispatchIntent(
            start=EPOCH,
            end=EPOCH + timedelta(seconds=QUARTER),
            interval_seconds=QUARTER,
            mode="charge",
            target_power_w=1000.0,
            soc_target_frac=0.5,
            generated_at=EPOCH,
            expires_at=EPOCH + timedelta(hours=1),
        )
        assert intent.mode is DispatchMode.CHARGE

    def test_aware_non_utc_instant_is_normalised(self) -> None:
        """The stored instant is always UTC, so two spellings of one instant agree."""
        plus_two = timezone(timedelta(hours=2))
        local_noon = datetime(2026, 6, 15, 14, 0, tzinfo=plus_two)  # == 12:00 UTC
        intent = DispatchIntent(
            start=local_noon,
            end=local_noon + timedelta(seconds=QUARTER),
            interval_seconds=QUARTER,
            mode=DispatchMode.IDLE,
            target_power_w=0.0,
            soc_target_frac=0.5,
            generated_at=EPOCH,
            expires_at=EPOCH + timedelta(hours=1),
        )
        assert intent.start == EPOCH
        assert intent.start.utcoffset() == timedelta(0)

    def test_intent_is_immutable(self) -> None:
        """A request already handed to a consumer cannot be edited underneath it."""
        intent = _intent(mode=DispatchMode.CHARGE, power_w=1000.0)
        with pytest.raises(dataclasses.FrozenInstanceError):
            intent.target_power_w = 5000.0  # type: ignore[misc]

    def test_target_energy_kwh_is_the_kernel_quantity(self) -> None:
        """The power x duration product is what ``BatterySpec`` bounds."""
        spec = _spec()
        intent = _intent(mode=DispatchMode.CHARGE, power_w=2000.0)
        assert intent.target_energy_kwh == pytest.approx(0.5)
        assert intent.target_energy_kwh <= spec.interval_energy_limit_kwh(QUARTER)


# ---------------------------------------------------------------------------
# 3. No simultaneous charge and discharge (battery invariant 4)
# ---------------------------------------------------------------------------


class TestNoSimultaneousFlow:
    def test_one_mode_field_makes_both_unrepresentable(self) -> None:
        """Structurally impossible: an intent holds one mode, not two power fields."""
        intent = _intent(mode=DispatchMode.CHARGE, power_w=1000.0)
        assert intent.mode is DispatchMode.CHARGE
        assert not any("discharge" in field.name for field in dataclasses.fields(DispatchIntent))

    def test_mode_derivation_refuses_both_flows(self) -> None:
        from energy_baseline.dispatch import _mode_from_flows

        with pytest.raises(DispatchError, match="one binary mode per interval"):
            _mode_from_flows(0.5, 0.5)

    def test_bridge_refuses_a_schedule_with_both_flows(self) -> None:
        """Delegated: the kernel names the invariant, the bridge does not re-check it."""
        spec = _spec()
        h = _horizon(2, load=[1.0, 1.0])
        feasible = simulate(spec, h, [0.3, 0.0], [0.0, 0.0], enforce_terminal_soc=False)
        violating = dataclasses.replace(
            feasible, charge_kwh=np.array([0.3, 0.0]), discharge_kwh=np.array([0.2, 0.0])
        )
        with pytest.raises(BatteryPhysicsError, match="simultaneous charge and discharge"):
            _bridge(violating, h, spec)

    def test_kernel_is_the_authority_for_power_limits(self) -> None:
        """A request above the nameplate power is rejected by ``battery.py``."""
        spec = _spec(power_kw=0.25)  # 0.0625 kWh per 900 s interval
        h = _horizon(2, load=[1.0, 1.0])
        feasible = simulate(_spec(), h, [0.5, 0.0], [0.0, 0.0], enforce_terminal_soc=False)
        with pytest.raises(BatteryPhysicsError, match="above power limit"):
            _bridge(feasible, h, spec)

    def test_kernel_is_the_authority_for_the_soc_window(self) -> None:
        """A plan whose SOC trajectory leaves the window is rejected on the kernel's wording."""
        # Same seed, so the bridge's own opening-SOC check cannot be what fires:
        # only the kernel's per-step recurrence can see the floor breach.
        h = _horizon(2, load=[5.0, 5.0])
        wide_window = _spec(soc_min_frac=0.0, initial_soc_frac=0.5)
        tight_floor = _spec(soc_min_frac=0.4, initial_soc_frac=0.5)
        # 0.6 kWh is within the 0.625 kWh/interval power limit, so the only thing
        # wrong with it is the floor the tight spec sets.
        draining = simulate(wide_window, h, [0.0, 0.0], [0.6, 0.0], enforce_terminal_soc=False)
        with pytest.raises(BatteryPhysicsError, match="below window floor"):
            _bridge(draining, h, tight_floor)

    def test_a_schedule_disagreeing_with_its_own_spec_is_named_as_such(self) -> None:
        """A trajectory the spec cannot have produced is a mismatch, not a physics violation."""
        h = _horizon(2, load=[1.0, 1.0])
        settled = simulate(_spec(initial_soc_frac=0.57), h, [0.0, 0.0], [0.0, 0.0])
        with pytest.raises(DispatchError, match="re-anchor initial_soc_frac"):
            _bridge(settled, h, _spec(initial_soc_frac=0.5))


# ---------------------------------------------------------------------------
# 4. Plan structure
# ---------------------------------------------------------------------------


class TestPlanStructure:
    def test_empty_plan_rejected(self) -> None:
        with pytest.raises(DispatchError, match="at least one intent"):
            DispatchPlan(
                intents=(),
                generated_at=EPOCH,
                plan_sequence=0,
                expires_at=EPOCH + timedelta(hours=1),
                interval_seconds=QUARTER,
            )

    def test_gap_between_intents_rejected(self) -> None:
        expiry = EPOCH + timedelta(hours=1)
        i0 = _intent(sequence_id=0, expires_at=expiry)
        i1 = _intent(offset_minutes=30, sequence_id=1, expires_at=expiry)
        with pytest.raises(DispatchError, match="gap or overlap"):
            DispatchPlan(
                intents=[i0, i1],
                generated_at=EPOCH,
                plan_sequence=0,
                expires_at=expiry,
                interval_seconds=QUARTER,
            )

    def test_overlapping_intents_rejected(self) -> None:
        expiry = EPOCH + timedelta(hours=1)
        i0 = _intent(sequence_id=0, expires_at=expiry)
        i1 = _intent(offset_minutes=14, sequence_id=1, expires_at=expiry)
        with pytest.raises(DispatchError, match="gap or overlap"):
            DispatchPlan(
                intents=[i0, i1],
                generated_at=EPOCH,
                plan_sequence=0,
                expires_at=expiry,
                interval_seconds=QUARTER,
            )

    def test_replayed_sequence_id_rejected(self) -> None:
        expiry = EPOCH + timedelta(hours=1)
        i0 = _intent(sequence_id=0, expires_at=expiry)
        i1 = _intent(offset_minutes=15, sequence_id=0, expires_at=expiry)
        with pytest.raises(DispatchError, match="strictly increasing from 0"):
            DispatchPlan(
                intents=[i0, i1],
                generated_at=EPOCH,
                plan_sequence=0,
                expires_at=expiry,
                interval_seconds=QUARTER,
            )

    def test_skipped_sequence_id_rejected(self) -> None:
        expiry = EPOCH + timedelta(hours=1)
        i0 = _intent(sequence_id=0, expires_at=expiry)
        i1 = _intent(offset_minutes=15, sequence_id=2, expires_at=expiry)
        with pytest.raises(DispatchError, match="strictly increasing from 0"):
            DispatchPlan(
                intents=[i0, i1],
                generated_at=EPOCH,
                plan_sequence=0,
                expires_at=expiry,
                interval_seconds=QUARTER,
            )

    def test_mixed_interval_grids_rejected(self) -> None:
        """One plan drives one uniform grid; a 15-min/60-min mix is refused."""
        expiry = EPOCH + timedelta(hours=1)
        i0 = _intent(sequence_id=0, expires_at=expiry)
        i1 = _intent(offset_minutes=15, interval_seconds=3600.0, sequence_id=1, expires_at=expiry)
        with pytest.raises(DispatchError, match="one uniform grid"):
            DispatchPlan(
                intents=[i0, i1],
                generated_at=EPOCH,
                plan_sequence=0,
                expires_at=expiry,
                interval_seconds=QUARTER,
            )

    def test_intents_from_two_generations_cannot_share_a_plan(self) -> None:
        expiry = EPOCH + timedelta(hours=1)
        i0 = _intent(sequence_id=0, expires_at=expiry, plan_sequence=4)
        i1 = _intent(offset_minutes=15, sequence_id=1, expires_at=expiry, plan_sequence=5)
        with pytest.raises(DispatchError, match="bound to plan_sequence=5"):
            DispatchPlan(
                intents=[i0, i1],
                generated_at=EPOCH,
                plan_sequence=4,
                expires_at=expiry,
                interval_seconds=QUARTER,
            )

    def test_mixed_generation_instants_rejected(self) -> None:
        """One generation is one instant: an intent from another run cannot join."""
        later = EPOCH + timedelta(minutes=15)
        expiry = EPOCH + timedelta(hours=2)
        i0 = _intent(sequence_id=0, expires_at=expiry)
        i1 = _intent(offset_minutes=15, sequence_id=1, expires_at=expiry, generated_at=later)
        with pytest.raises(DispatchError, match="one generation, one instant"):
            DispatchPlan(
                intents=[i0, i1],
                generated_at=EPOCH,
                plan_sequence=0,
                expires_at=expiry,
                interval_seconds=QUARTER,
            )

    def test_derived_horizon_bounds_and_duration(self) -> None:
        plan = _idle_run(4)
        assert plan.n == 4
        assert plan.horizon_start == EPOCH
        assert plan.horizon_end == EPOCH + timedelta(seconds=4 * QUARTER)
        assert plan.duration_seconds == pytest.approx(4 * QUARTER)

    def test_horizon_bounds_are_not_constructor_arguments(self) -> None:
        """They are restatements of the intervals, so they cannot contradict them."""
        declared = {field.name for field in dataclasses.fields(DispatchPlan)}
        assert not {"horizon_start", "horizon_end"} & declared

    def test_next_plan_sequence_is_the_successor_number(self) -> None:
        assert _idle_run(1, plan_sequence=7).next_plan_sequence == 8


# ---------------------------------------------------------------------------
# 5. Expiry is a first-class safety field
# ---------------------------------------------------------------------------


class TestExpiry:
    def test_intent_expires_at_its_instant(self) -> None:
        intent = _intent(expires_at=EPOCH + timedelta(minutes=30))
        assert not intent.is_expired(EPOCH + timedelta(minutes=29, seconds=59))
        assert intent.is_expired(EPOCH + timedelta(minutes=30))
        assert intent.is_expired(EPOCH + timedelta(days=1))

    def test_expiry_needs_no_producer_trust(self) -> None:
        """Rejection uses ``expires_at`` and the consumer's own instant — nothing else."""
        stale = _intent(expires_at=EPOCH + timedelta(minutes=5), price=0.02, confidence=1.0)
        assert stale.is_expired(EPOCH + timedelta(hours=1))

    def test_plan_expires_when_any_intent_expires(self) -> None:
        plan = _idle_run(3, lifetime_minutes=30)
        assert not plan.is_expired(EPOCH + timedelta(minutes=29))
        assert plan.is_expired(EPOCH + timedelta(minutes=30))

    def test_plan_expiry_must_be_the_earliest_intent_expiry(self) -> None:
        """A later plan expiry would claim cover for an interval that has lapsed."""
        early = EPOCH + timedelta(minutes=20)
        late = EPOCH + timedelta(hours=2)
        i0 = _intent(sequence_id=0, expires_at=early)
        i1 = _intent(offset_minutes=15, sequence_id=1, expires_at=late)
        with pytest.raises(DispatchError, match="earliest intent expiry"):
            DispatchPlan(
                intents=[i0, i1],
                generated_at=EPOCH,
                plan_sequence=0,
                expires_at=late,
                interval_seconds=QUARTER,
            )

    def test_actionable_intents_drop_finished_intervals(self) -> None:
        plan = _idle_run(4, lifetime_minutes=30)
        now = EPOCH + timedelta(minutes=20)
        # Interval 0 covers 12:00-12:15, already past; 1..3 still have time to act.
        assert [intent.sequence_id for intent in plan.actionable_intents(now)] == [1, 2, 3]

    def test_actionable_intents_drop_expired_intervals(self) -> None:
        plan = _idle_run(4, lifetime_minutes=30)
        assert plan.actionable_intents(EPOCH + timedelta(minutes=31)) == []

    def test_safe_idle_plan_replaces_every_interval_with_zero_watts(self) -> None:
        """TODO-6.3's safe-idle lease timeout is reachable from any plan."""
        spec = _spec()
        h = _horizon(3, load=[1.0] * 3)
        sched = simulate(spec, h, [0.4, 0.0, 0.0], [0.0, 0.2, 0.0], enforce_terminal_soc=False)
        plan = _bridge(sched, h, spec)
        idle = plan.safe_idle_plan()
        assert idle.n == plan.n
        assert all(intent.mode is DispatchMode.IDLE for intent in idle.intents)
        assert all(intent.target_power_w == 0.0 for intent in idle.intents)
        assert [i.start for i in idle.intents] == [i.start for i in plan.intents]
        assert [i.sequence_id for i in idle.intents] == [i.sequence_id for i in plan.intents]

    def test_safe_idle_is_a_request_not_an_acknowledgement(self) -> None:
        """ADR-0001: the contract cannot claim the physical output is now zero."""
        idle = _idle_run(2).safe_idle_plan()
        payload = json.loads(plan_to_canonical_json(idle))
        assert not any("ack" in key for key in payload)
        assert not any("measured" in key for key in payload)


# ---------------------------------------------------------------------------
# 6. Sequence numbers are unforgeable per generation
# ---------------------------------------------------------------------------


class TestSequenceMonotonicity:
    def test_strictly_higher_successor_accepted(self) -> None:
        _idle_run(1, plan_sequence=5).accepts_successor(_idle_run(1, plan_sequence=6))

    def test_equal_successor_rejected_as_replay(self) -> None:
        plan = _idle_run(1, plan_sequence=5)
        with pytest.raises(DispatchError, match="replayed or out-of-order"):
            plan.accepts_successor(_idle_run(1, plan_sequence=5))

    def test_lower_successor_rejected(self) -> None:
        newer = _idle_run(1, plan_sequence=10)
        with pytest.raises(DispatchError, match="does not exceed in-flight"):
            newer.accepts_successor(_idle_run(1, plan_sequence=3))

    def test_stale_but_unexpired_successor_is_still_refused(self) -> None:
        """Monotonicity is independent of time: freshness does not license a replay."""
        current = _idle_run(1, plan_sequence=9)
        # Older generation, but its expiry still lies in the future.
        stale = _idle_run(
            1, plan_sequence=8, generated_at=EPOCH - timedelta(hours=1), lifetime_minutes=180
        )
        assert not stale.is_expired(EPOCH)
        with pytest.raises(DispatchError, match="does not exceed in-flight"):
            current.accepts_successor(stale)

    def test_non_plan_successor_rejected(self) -> None:
        with pytest.raises(DispatchError, match="must be a DispatchPlan"):
            _idle_run(1).accepts_successor(object())  # type: ignore[arg-type]

    def test_command_key_deduplicates_a_request(self) -> None:
        """One-in-flight needs an identity: (plan_sequence, sequence_id) is it."""
        first = _intent(mode=DispatchMode.CHARGE, power_w=1000.0, plan_sequence=4)
        again = _intent(mode=DispatchMode.CHARGE, power_w=1000.0, plan_sequence=4)
        other_generation = first.bound_to(5)
        assert first.command_key == again.command_key
        assert first.command_key != other_generation.command_key

    def test_unbound_intent_is_not_a_valid_command(self) -> None:
        """``plan_sequence == -1`` means "belongs to no generation" and must be refused."""
        unbound = DispatchIntent(
            start=EPOCH,
            end=EPOCH + timedelta(seconds=QUARTER),
            interval_seconds=QUARTER,
            mode=DispatchMode.IDLE,
            target_power_w=0.0,
            soc_target_frac=0.5,
            generated_at=EPOCH,
            expires_at=EPOCH + timedelta(hours=1),
        )
        assert unbound.plan_sequence == -1
        assert unbound.command_key[0] == -1

    def test_bound_to_cannot_renumber_into_the_past(self) -> None:
        with pytest.raises(DispatchError, match="must be >= 0 to bind"):
            _intent().bound_to(-2)


# ---------------------------------------------------------------------------
# 7. Canonical serialization and hashing
# ---------------------------------------------------------------------------


class TestCanonicalSerialization:
    def _plan(self) -> DispatchPlan:
        expiry = EPOCH + timedelta(hours=1)
        i0 = DispatchIntent(
            start=EPOCH,
            end=EPOCH + timedelta(seconds=QUARTER),
            interval_seconds=QUARTER,
            mode="charge",
            target_power_w=2500.0,
            soc_target_frac=0.6,
            generated_at=EPOCH,
            expires_at=expiry,
            sequence_id=0,
            plan_sequence=3,
            expected_buy_price_eur_per_kwh=0.08,
            confidence=0.9,
        )
        i1 = DispatchIntent(
            start=EPOCH + timedelta(seconds=QUARTER),
            end=EPOCH + timedelta(seconds=2 * QUARTER),
            interval_seconds=QUARTER,
            mode="discharge",
            target_power_w=1200.0,
            soc_target_frac=0.5,
            generated_at=EPOCH,
            expires_at=expiry,
            sequence_id=1,
            plan_sequence=3,
        )
        return DispatchPlan(
            intents=[i0, i1],
            generated_at=EPOCH,
            plan_sequence=3,
            expires_at=expiry,
            interval_seconds=QUARTER,
            battery_spec=_spec(),
        )

    def test_round_trip_is_byte_identical(self) -> None:
        text = plan_to_canonical_json(self._plan())
        assert plan_to_canonical_json(plan_from_canonical_json(text)) == text

    def test_round_trip_preserves_every_field(self) -> None:
        plan = self._plan()
        restored = plan_from_canonical_json(plan_to_canonical_json(plan))
        assert restored.content_hash == plan.content_hash
        assert restored.plan_sequence == plan.plan_sequence
        assert restored.expires_at == plan.expires_at
        for before, after in zip(plan.intents, restored.intents, strict=True):
            for name in INTENT_FIELDS:
                assert getattr(before, name) == getattr(after, name), name

    def test_serialized_keys_are_exactly_the_declared_fields(self) -> None:
        payload = json.loads(plan_to_canonical_json(self._plan()))
        assert set(payload["intents"][0]) == set(INTENT_FIELDS)

    def test_digest_is_sha256_of_the_hashed_payload(self) -> None:
        """Same convention as ``manifest.py``: SHA-256 over canonical UTF-8 bytes."""
        plan = self._plan()
        payload = json.loads(plan_to_canonical_json(plan))
        hashed = {
            key: value
            for key, value in payload.items()
            if key not in ("content_hash", "horizon_start", "horizon_end")
        }
        assert plan.content_hash == content_hash(canonical_json(hashed))

    def test_digest_is_64_hex_characters(self) -> None:
        digest = self._plan().content_hash
        assert len(digest) == 64
        assert int(digest, 16) >= 0

    def test_tampered_digest_rejected(self) -> None:
        payload = json.loads(plan_to_canonical_json(self._plan()))
        payload["content_hash"] = "0" * 64
        with pytest.raises(DispatchError, match="content_hash mismatch"):
            plan_from_canonical_json(canonical_json(payload))

    def test_tampered_field_with_the_old_digest_rejected(self) -> None:
        """Editing a power value without re-hashing is the attack the digest catches."""
        plan = self._plan()
        payload = json.loads(plan_to_canonical_json(plan))
        # Still a plausible request for the hardware, so only the digest can tell.
        payload["intents"][0]["target_power_w"] = 2450.0
        with pytest.raises(DispatchError, match="content_hash mismatch"):
            plan_from_canonical_json(canonical_json(payload))

    def test_an_edit_above_the_hardware_is_refused_even_with_a_recomputed_digest(self) -> None:
        """Two independent gates: the digest proves identity, the spec proves physics."""
        payload = json.loads(plan_to_canonical_json(self._plan()))
        payload["intents"][0]["target_power_w"] = 99_999.0
        payload["content_hash"] = ""  # a forger who re-hashes everything still fails here
        with pytest.raises(DispatchError, match="nameplate limit"):
            plan_from_canonical_json(canonical_json(payload))

    def test_stripping_the_digest_still_yields_a_verifiable_plan(self) -> None:
        """A hash-less document parses, and its own digest is then whatever it hashes to."""
        payload = json.loads(plan_to_canonical_json(self._plan()))
        payload.pop("content_hash")
        rebuilt = DispatchPlan.from_dict(payload)
        assert rebuilt.content_hash == content_hash(
            canonical_json(
                {
                    key: value
                    for key, value in json.loads(plan_to_canonical_json(rebuilt)).items()
                    if key not in ("content_hash", "horizon_start", "horizon_end")
                }
            )
        )

    def test_edited_digest_inside_a_document_is_caught(self) -> None:
        """Raising the power and recomputing nothing is detectable; that is the point."""
        original = plan_to_canonical_json(self._plan())
        forged = original.replace('"target_power_w":2500.0', '"target_power_w":1200.0')
        assert forged != original
        with pytest.raises(DispatchError, match="content_hash mismatch"):
            plan_from_canonical_json(forged)

    def test_infinity_rejected_by_canonical_json(self) -> None:
        """Not valid JSON; a document that will not parse back is not canonical."""
        with pytest.raises(ValueError, match="Out of range float"):
            canonical_json({"power": float("inf")})

    def test_canonical_form_sorts_keys_and_omits_whitespace(self) -> None:
        text = canonical_json({"z": 1, "a": {"y": 2, "b": [3, 4]}})
        assert text.startswith('{"a":')
        assert " " not in text and "\n" not in text

    def test_dict_order_does_not_change_the_digest(self) -> None:
        """Semantic equality is what the digest claims, matching manifest.config_hash."""
        one = canonical_json({"a": 1.5, "b": "x"})
        two = canonical_json({"b": "x", "a": 1.5})
        assert one == two and content_hash(one) == content_hash(two)

    def test_malformed_json_is_one_clean_line(self) -> None:
        with pytest.raises(DispatchError, match="not valid JSON"):
            plan_from_canonical_json('{"intents": [')

    def test_json_array_is_rejected(self) -> None:
        with pytest.raises(DispatchError, match="must be a JSON object"):
            plan_from_canonical_json("[]")

    def test_missing_and_unknown_intent_fields_rejected(self) -> None:
        payload = json.loads(plan_to_canonical_json(self._plan()))
        stripped = {key: value for key, value in payload["intents"][0].items() if key != "start"}
        with pytest.raises(DispatchError, match="missing intent field"):
            DispatchIntent.from_dict(stripped)
        with pytest.raises(DispatchError, match="unknown intent field"):
            DispatchIntent.from_dict({**payload["intents"][0], "boost": True})

    def test_unknown_spec_field_rejected(self) -> None:
        payload = json.loads(plan_to_canonical_json(self._plan()))
        payload["battery_spec"]["mystery"] = 1.0
        with pytest.raises(DispatchError, match="unknown battery_spec field"):
            plan_from_canonical_json(canonical_json({**payload, "content_hash": ""}))


# ---------------------------------------------------------------------------
# 8. Determinism against the no-clobber store
# ---------------------------------------------------------------------------


class TestDeterminism:
    def test_same_inputs_produce_identical_bytes(self) -> None:
        """The no-clobber property: a re-run must not collide with its own bytes."""
        spec = _spec()
        h = _horizon(4, load=[0.2] * 4)
        sched = simulate(spec, h, [0.5, 0.0, 0.0, 0.0], [0.0] * 4, enforce_terminal_soc=False)
        first = _bridge(sched, h, spec, expected_buy_prices=[0.1] * 4)
        second = _bridge(sched, h, spec, expected_buy_prices=[0.1] * 4)
        assert plan_to_canonical_json(first) == plan_to_canonical_json(second)
        assert first.content_hash == second.content_hash

    def test_a_different_decision_instant_is_a_different_plan(self) -> None:
        """The generation timestamp is content, so replays of different instants differ."""
        spec = _spec()
        h = _horizon(2, load=[0.2] * 2)
        sched = simulate(spec, h, [0.0, 0.0], [0.0, 0.0], enforce_terminal_soc=False)
        morning = _bridge(sched, h, spec)
        later_generated = EPOCH + timedelta(hours=6)
        evening = _bridge(
            sched,
            h,
            spec,
            generated_at=later_generated,
            expires_at=later_generated + timedelta(hours=2),
            interval_starts_utc=_starts(2, origin=later_generated),
        )
        assert morning.content_hash != evening.content_hash

    def test_module_reads_no_clock_anywhere(self) -> None:
        """Proven on the syntax tree, not by patching, which misses import-bound names.

        A generation instant taken from the wall clock would hash differently on
        every re-run and could not live in a content-addressed no-clobber store —
        the same reason ``policies.py`` refuses to record solver wall-clock time.
        """
        forbidden = {"now", "utcnow", "time", "monotonic", "perf_counter", "today"}
        offenders: list[str] = []
        for node in ast.walk(ast.parse(_DISPATCH_SOURCE)):
            if not isinstance(node, ast.Call):
                continue
            target = node.func
            name = target.attr if isinstance(target, ast.Attribute) else getattr(target, "id", None)
            if name in forbidden:
                offenders.append(f"line {node.lineno}: {name}()")
        assert not offenders, f"dispatch.py reads a clock at: {', '.join(offenders)}"

    def test_module_does_not_import_the_time_module(self) -> None:
        """``time`` is the other way to read a clock; the AST shows it is not imported."""
        imported: list[str] = []
        for node in ast.walk(ast.parse(_DISPATCH_SOURCE)):
            if isinstance(node, ast.Import):
                imported.extend(alias.name for alias in node.names)
            elif isinstance(node, ast.ImportFrom):
                imported.append(node.module or "")
        # ``datetime`` is imported (instants are its type); ``time`` must not be.
        assert "datetime" in imported
        assert not {"time", "timeit"} & set(imported)

    def test_generated_at_has_no_default(self) -> None:
        """A defaulted generation instant would be the wall clock in disguise."""
        assert "generated_at" in {f.name for f in dataclasses.fields(DispatchIntent)}
        with pytest.raises(TypeError):
            DispatchIntent(
                start=EPOCH,
                end=EPOCH + timedelta(seconds=QUARTER),
                interval_seconds=QUARTER,
                mode=DispatchMode.IDLE,
                target_power_w=0.0,
                soc_target_frac=0.5,
                expires_at=EPOCH + timedelta(hours=1),
            )

    def test_serialization_is_idempotent_across_repeated_calls(self) -> None:
        plan = _idle_run(3)
        texts = {plan_to_canonical_json(plan) for _ in range(5)}
        assert len(texts) == 1, "serializing the same plan twice produced different bytes"

    def test_deserialize_serialize_round_trips_the_whole_document(self) -> None:
        """A loaded plan re-serializes to the exact bytes it was loaded from."""
        text = plan_to_canonical_json(_idle_run(3))
        assert plan_to_canonical_json(plan_from_canonical_json(text)) == text


# ---------------------------------------------------------------------------
# 9. Interval and timezone discipline: DST days
# ---------------------------------------------------------------------------


class TestIntervalDiscipline:
    """A day contributes whatever intervals it has, never a fixed 96."""

    @pytest.mark.parametrize(("n", "hours"), [(92, 23.0), (96, 24.0), (100, 25.0)])
    def test_interval_counts_are_not_assumed(self, n: int, hours: float) -> None:
        spec = _spec()
        h = _horizon(n, load=[0.2] * n)
        sched = simulate(spec, h, [0.0] * n, [0.0] * n, enforce_terminal_soc=False)
        plan = _bridge(sched, h, spec)
        assert plan.n == n
        assert (plan.horizon_end - plan.horizon_start).total_seconds() == pytest.approx(
            hours * 3600
        )

    @staticmethod
    def _local_day_seconds(year: int, month: int, day: int) -> float:
        """Real elapsed seconds in one local calendar day.

        Subtracted on the UTC axis deliberately: CPython subtracts two aware
        datetimes that share the *same* ``tzinfo`` as a wall-clock difference, so
        ``(next_local_midnight - local_midnight)`` in a ``ZoneInfo`` answers "24
        hours" on a DST day — the exact bug that shortens a spring-forward day to
        96 slots again and hides a fall-back duplicate hour.
        """
        madrid = ZoneInfo("Europe/Madrid")
        start = datetime(year, month, day, tzinfo=madrid).astimezone(UTC)
        end = datetime(year, month, day + 1, tzinfo=madrid).astimezone(UTC)
        return (end - start).total_seconds()

    def test_spring_forward_madrid_day_is_92_intervals(self) -> None:
        """2026-03-29 in Europe/Madrid is a 23-hour day: 92 quarter-hour intervals."""
        day_seconds = self._local_day_seconds(2026, 3, 29)
        assert day_seconds == 23 * 3600

        n = int(day_seconds // QUARTER)
        assert n == 92
        origin = datetime(2026, 3, 29, tzinfo=ZoneInfo("Europe/Madrid")).astimezone(UTC)
        spec = _spec()
        h = _horizon(n, load=[0.2] * n)
        sched = simulate(spec, h, [0.0] * n, [0.0] * n, enforce_terminal_soc=False)
        plan = _bridge(
            sched,
            h,
            spec,
            interval_starts_utc=_starts(n, origin=origin),
            generated_at=origin,
            expires_at=origin + timedelta(hours=24),
        )
        assert plan.n == 92
        assert plan.duration_seconds == pytest.approx(23 * 3600)
        assert all(intent.start.utcoffset() == timedelta(0) for intent in plan.intents)

    def test_fall_back_madrid_day_is_100_intervals(self) -> None:
        """2025-10-26 in Europe/Madrid is a 25-hour day: 100 intervals, no duplicates."""
        day_seconds = self._local_day_seconds(2025, 10, 26)
        assert day_seconds == 25 * 3600

        n = int(day_seconds // QUARTER)
        assert n == 100
        origin = datetime(2025, 10, 26, tzinfo=ZoneInfo("Europe/Madrid")).astimezone(UTC)
        spec = _spec()
        h = _horizon(n, load=[0.2] * n)
        sched = simulate(spec, h, [0.0] * n, [0.0] * n, enforce_terminal_soc=False)
        plan = _bridge(
            sched,
            h,
            spec,
            interval_starts_utc=_starts(n, origin=origin),
            generated_at=origin,
            expires_at=origin + timedelta(hours=26),
        )
        assert plan.n == 100
        assert plan.duration_seconds == pytest.approx(25 * 3600)
        instants = [intent.start for intent in plan.intents]
        assert len(set(instants)) == n, "the repeated local hour must not duplicate an instant"

    def test_local_wall_clock_subtraction_is_the_trap_this_avoids(self) -> None:
        """Pinned because it is the failure mode, not a curiosity.

        Same-``tzinfo`` subtraction answers 24 h on a 23-hour day, which is exactly
        how a DST day silently regains 96 intervals; the contract sees only UTC.
        """
        madrid = ZoneInfo("Europe/Madrid")
        start = datetime(2026, 3, 29, tzinfo=madrid)
        end = datetime(2026, 3, 30, tzinfo=madrid)
        assert (end - start).total_seconds() == 24 * 3600  # wall clock: wrong by design
        assert self._local_day_seconds(2026, 3, 29) == 23 * 3600  # real elapsed time

    def test_madrid_local_instants_are_accepted_and_stored_as_utc(self) -> None:
        """A caller may hand Madrid-aware starts; they normalize, they are not assumed."""
        madrid = ZoneInfo("Europe/Madrid")
        spec = _spec()
        h = _horizon(4, load=[0.2] * 4)
        sched = simulate(spec, h, [0.0] * 4, [0.0] * 4, enforce_terminal_soc=False)
        local_origin = datetime(2026, 1, 15, 0, 0, tzinfo=madrid)
        starts = [local_origin + timedelta(seconds=QUARTER * i) for i in range(4)]
        plan = _bridge(
            sched,
            h,
            spec,
            interval_starts_utc=starts,
            generated_at=local_origin.astimezone(UTC),
        )
        assert [intent.start for intent in plan.intents] == [
            start.astimezone(UTC) for start in starts
        ]

    def test_non_quarter_hour_grid_is_fine(self) -> None:
        """One-minute checks and 15-minute analysis grids both work unchanged."""
        for interval_seconds in (60.0, 900.0, 3600.0):
            spec = _spec(power_kw=2.5)
            h = _horizon(3, interval_seconds=interval_seconds, load=[0.2] * 3)
            sched = simulate(spec, h, [0.0] * 3, [0.0] * 3, enforce_terminal_soc=False)
            plan = _bridge(
                sched,
                h,
                spec,
                interval_starts_utc=_starts(3, interval_seconds=interval_seconds),
                expires_at=EPOCH + timedelta(hours=2),
            )
            assert plan.interval_seconds == pytest.approx(interval_seconds)
            assert (plan.horizon_end - plan.horizon_start).total_seconds() == pytest.approx(
                3 * interval_seconds
            )

    def test_non_contiguous_grid_rejected(self) -> None:
        """A hole in the supplied grid would silently shift every later interval."""
        spec = _spec()
        h = _horizon(3, load=[0.2] * 3)
        sched = simulate(spec, h, [0.0] * 3, [0.0] * 3, enforce_terminal_soc=False)
        starts = _starts(3)
        starts[2] = starts[2] + timedelta(seconds=QUARTER)  # skip one slot
        with pytest.raises(DispatchError, match="grid is not contiguous"):
            _bridge(sched, h, spec, interval_starts_utc=starts)

    def test_naive_interval_start_rejected(self) -> None:
        spec = _spec()
        h = _horizon(2, load=[0.2] * 2)
        sched = simulate(spec, h, [0.0, 0.0], [0.0, 0.0], enforce_terminal_soc=False)
        starts = _starts(2)
        starts[1] = starts[1].replace(tzinfo=None)
        with pytest.raises(DispatchError, match="interval_starts_utc\\[1\\]"):
            _bridge(sched, h, spec, interval_starts_utc=starts)


# ---------------------------------------------------------------------------
# 10. Bridge from the plans the pipeline already produces
# ---------------------------------------------------------------------------


class TestBridgeFromSchedule:
    def test_no_battery_plan_is_all_idle(self) -> None:
        spec = _spec()
        h = _horizon(4)
        plan = _bridge(simulate(spec, h, [0.0] * 4, [0.0] * 4), h, spec)
        assert [intent.mode for intent in plan.intents] == [DispatchMode.IDLE] * 4
        assert all(intent.target_power_w == 0.0 for intent in plan.intents)

    def test_charge_energy_becomes_average_power(self) -> None:
        """0.5 kWh into a 900 s interval is a 2000 W average request."""
        spec = _spec()
        h = _horizon(2, load=[1.0, 1.0])
        sched = simulate(spec, h, [0.5, 0.0], [0.0, 0.0], enforce_terminal_soc=False)
        plan = _bridge(sched, h, spec)
        assert plan.intents[0].mode is DispatchMode.CHARGE
        assert plan.intents[0].target_power_w == pytest.approx(2000.0)
        assert plan.intents[1].mode is DispatchMode.IDLE

    def test_discharge_energy_becomes_average_power(self) -> None:
        spec = _spec(initial_soc_frac=0.8)
        h = _horizon(2, load=[1.0, 1.0])
        sched = simulate(spec, h, [0.0, 0.0], [0.0, 0.45], enforce_terminal_soc=False)
        plan = _bridge(sched, h, spec)
        assert plan.intents[1].mode is DispatchMode.DISCHARGE
        assert plan.intents[1].target_power_w == pytest.approx(1800.0)

    def test_soc_target_is_the_end_of_interval_fraction(self) -> None:
        spec = _spec()
        h = _horizon(2, load=[1.0, 1.0])
        sched = simulate(spec, h, [0.5, 0.0], [0.0, 0.0], enforce_terminal_soc=False)
        plan = _bridge(sched, h, spec)
        assert plan.intents[0].soc_target_frac == pytest.approx(
            float(sched.soc_kwh[0]) / spec.capacity_kwh
        )

    def test_confidence_is_absent_when_the_producer_reports_none(self) -> None:
        """No policy or oracle emits a confidence, so the bridge must invent none."""
        spec = _spec()
        h = _horizon(2)
        plan = _bridge(simulate(spec, h, [0.0] * 2, [0.0] * 2), h, spec)
        assert [intent.confidence for intent in plan.intents] == [None, None]

    def test_price_belief_is_absent_when_not_supplied(self) -> None:
        """Schedules carry no planning-price array, so beliefs are not fabricated."""
        spec = _spec()
        h = _horizon(2, prices=[0.31, 0.02])
        plan = _bridge(simulate(spec, h, [0.0] * 2, [0.0] * 2), h, spec)
        assert [intent.expected_buy_price_eur_per_kwh for intent in plan.intents] == [None, None]

    def test_explicit_beliefs_are_carried(self) -> None:
        spec = _spec()
        h = _horizon(2)
        plan = _bridge(
            simulate(spec, h, [0.0] * 2, [0.0] * 2),
            h,
            spec,
            expected_buy_prices=[-0.02, 0.25],
            confidence=[0.85, None],
        )
        assert plan.intents[0].expected_buy_price_eur_per_kwh == pytest.approx(-0.02)
        assert plan.intents[1].expected_buy_price_eur_per_kwh == pytest.approx(0.25)
        assert plan.intents[0].confidence == pytest.approx(0.85)
        assert plan.intents[1].confidence is None

    def test_absent_confidence_survives_serialization(self) -> None:
        spec = _spec()
        h = _horizon(2)
        plan = _bridge(simulate(spec, h, [0.0] * 2, [0.0] * 2), h, spec, confidence=[None, 0.5])
        restored = plan_from_canonical_json(plan_to_canonical_json(plan))
        assert restored.intents[0].confidence is None
        assert restored.intents[1].confidence == pytest.approx(0.5)

    def test_plan_sequence_is_stamped_on_every_intent(self) -> None:
        spec = _spec()
        h = _horizon(2)
        plan = _bridge(simulate(spec, h, [0.0] * 2, [0.0] * 2), h, spec, plan_sequence=42)
        assert plan.plan_sequence == 42
        assert [intent.plan_sequence for intent in plan.intents] == [42, 42]
        assert [intent.command_key for intent in plan.intents] == [(42, 0), (42, 1)]

    def test_sliced_window_needs_a_reanchored_spec(self) -> None:
        """A slice's opening SOC is not the window's seed, and that is caught."""
        spec = _spec()
        h = _horizon(3, load=[1.0] * 3)
        sched = simulate(spec, h, [0.4, 0.0, 0.0], [0.0, 0.0, 0.0], enforce_terminal_soc=False)
        sliced_horizon = Horizon(
            interval_seconds=h.interval_seconds,
            prices=h.prices[1:],
            load_kwh=h.load_kwh[1:],
            usable=h.usable[1:],
        )
        sliced = _slice_schedule(sched, 1, 3)
        grid = _starts(2, origin=EPOCH + timedelta(seconds=QUARTER))
        with pytest.raises(DispatchError, match="re-anchor initial_soc_frac"):
            _bridge(
                sliced,
                sliced_horizon,
                spec,
                interval_starts_utc=grid,
                generated_at=EPOCH + timedelta(seconds=QUARTER),
            )
        # Re-anchored the way rolling_horizon.py does it, the same slice converts.
        anchored = dataclasses.replace(
            spec, initial_soc_frac=float(sched.soc_kwh[0]) / spec.capacity_kwh
        )
        plan = _bridge(
            sliced,
            sliced_horizon,
            anchored,
            interval_starts_utc=grid,
            generated_at=EPOCH + timedelta(seconds=QUARTER),
        )
        assert plan.n == 2

    def test_length_mismatch_rejected(self) -> None:
        spec = _spec()
        h = _horizon(3, load=[1.0] * 3)
        short = simulate(spec, _horizon(2, load=[1.0, 1.0]), [0.0, 0.0], [0.0, 0.0])
        with pytest.raises(DispatchError, match="the horizon has 3"):
            _bridge(short, h, spec)

    def test_belief_array_length_mismatch_rejected(self) -> None:
        spec = _spec()
        h = _horizon(3)
        sched = simulate(spec, h, [0.0] * 3, [0.0] * 3, enforce_terminal_soc=False)
        with pytest.raises(DispatchError, match="expected_buy_prices has 2"):
            _bridge(sched, h, spec, expected_buy_prices=[0.1, 0.2])
        with pytest.raises(DispatchError, match="confidence has 4"):
            _bridge(sched, h, spec, confidence=[0.1, 0.2, 0.3, 0.4])

    def test_out_of_window_soc_target_rejected(self) -> None:
        """SOC is what the contract publishes, so its window is checked directly.

        The flows here are legal and the kernel's own recurrence is flat, so
        nothing else could catch a published target outside the operating window.
        """
        spec = _spec()  # window [0.512, 4.864] kWh, seed 2.56 kWh
        h = _horizon(2, load=[1.0, 1.0])
        inconsistent = Schedule(
            charge_kwh=[0.0, 0.0],
            discharge_kwh=[0.0, 0.0],
            soc_kwh=[2.56, 5.12],  # second interval published above the ceiling
            grid_kwh=[1.0, 1.0],
            cost_eur=0.0,
            efc=0.0,
            throughput_kwh=0.0,
            soc_min_frac=0.5,
            soc_max_frac=1.0,
        )
        with pytest.raises(DispatchError, match="outside the spec window"):
            _bridge(inconsistent, h, spec)

    def test_bridge_adds_no_power_rule_of_its_own(self) -> None:
        """The kernel's own wording surfaces, which is how delegation is visible."""
        spec = _spec()
        h = _horizon(2, load=[1.0, 1.0])
        feasible = simulate(spec, h, [0.5, 0.0], [0.0, 0.0], enforce_terminal_soc=False)
        with pytest.raises(BatteryPhysicsError) as info:
            _bridge(feasible, h, dataclasses.replace(spec, power_kw=0.5))
        assert "flow above power limit" in str(info.value)

    def test_bridge_adds_no_export_rule_of_its_own(self) -> None:
        """Same plan, thinner measured load: the no-export row is what fires."""
        spec = _spec(initial_soc_frac=0.8)
        generous = _horizon(2, load=[2.0, 2.0])
        plan = simulate(spec, generous, [0.0, 0.0], [0.0, 0.5], enforce_terminal_soc=False)
        thin = _horizon(2, load=[2.0, 0.1])
        with pytest.raises(BatteryPhysicsError, match="intentional export is not allowed"):
            _bridge(plan, thin, spec)

    def test_untrusted_interval_starts_length_rejected(self) -> None:
        spec = _spec()
        h = _horizon(3, load=[1.0] * 3)
        sched = simulate(spec, h, [0.0] * 3, [0.0] * 3, enforce_terminal_soc=False)
        with pytest.raises(DispatchError, match="interval_starts_utc has 2 entries"):
            _bridge(sched, h, spec, interval_starts_utc=_starts(2))

    def test_generation_and_expiry_order_rejected(self) -> None:
        spec = _spec()
        h = _horizon(1, load=[1.0])
        sched = simulate(spec, h, [0.0], [0.0])
        with pytest.raises(DispatchError, match="must be after generated_at"):
            _bridge(sched, h, spec, expires_at=EPOCH)


# ---------------------------------------------------------------------------
# 11. Import ceiling: a constraint on charging, and an observation otherwise
# ---------------------------------------------------------------------------


class TestImportCeiling:
    def test_net_import_above_the_ceiling_is_not_a_rejection(self) -> None:
        """battery.py invariant 7: the measured load alone topping the ceiling is history."""
        spec = _spec()
        # 6.0 kWh in 900 s is a 24 kW average draw against a 5 kW contract.
        h = _horizon(2, load=[6.0, 6.0], import_limit_kw=5.0)
        sched = simulate(spec, h, [0.0, 0.0], [0.0, 0.0])
        assert sched.ceiling_exceeding_intervals == 2

        plan = _bridge(sched, h, spec)  # must not raise
        assert plan.n == 2

    def test_no_battery_baseline_is_still_dispatchible_under_a_tight_ceiling(self) -> None:
        """Refusing these intervals would declare the household's past infeasible."""
        spec = _spec()
        h = _horizon(4, load=[0.1, 2.0, 1.6, 0.3], import_limit_kw=5.0)
        sched = simulate(spec, h, [0.0] * 4, [0.0] * 4)
        plan = _bridge(sched, h, spec)
        assert all(intent.mode is DispatchMode.IDLE for intent in plan.intents)

    def test_charging_beyond_the_residual_headroom_is_rejected(self) -> None:
        """Invariant 6: charging may only take headroom the load leaves."""
        spec = _spec()
        load = [4.5, 0.0]
        # Legitimate on an unconstrained site: 0.5 kWh in 900 s is 2000 W, under
        # the 2.5 kW rating, so nothing but the ceiling can object to this plan.
        unconstrained = _horizon(2, load=load, import_limit_kw=None)
        feasible = simulate(spec, unconstrained, [0.5, 0.0], [0.0, 0.0], enforce_terminal_soc=False)
        # On the contracted site the energy allowance is 5.0 * 0.25 = 1.25 kWh and
        # the load already took 4.5 kWh, so the headroom is zero.
        constrained = _horizon(2, load=load, import_limit_kw=5.0)
        with pytest.raises(BatteryPhysicsError, match="ceiling headroom"):
            _bridge(feasible, constrained, spec)

    def test_discharge_stays_available_when_the_ceiling_is_tightest(self) -> None:
        """The ceiling is a charging rule; discharge lowers the draw and is unaffected."""
        spec = _spec(initial_soc_frac=0.8)
        h = _horizon(2, load=[6.0, 0.5], import_limit_kw=5.0)
        sched = simulate(spec, h, [0.0, 0.0], [0.0, 0.4], enforce_terminal_soc=False)
        plan = _bridge(sched, h, spec)
        assert plan.intents[1].mode is DispatchMode.DISCHARGE

    def test_unconfigured_ceiling_is_not_treated_as_zero(self) -> None:
        """Unknown means unconstrained, never an invented number."""
        spec = _spec()
        h = _horizon(2, load=[0.2, 0.2], import_limit_kw=None)
        sched = simulate(spec, h, [0.5, 0.0], [0.0, 0.0], enforce_terminal_soc=False)
        plan = _bridge(sched, h, spec)
        assert plan.intents[0].mode is DispatchMode.CHARGE


# ---------------------------------------------------------------------------
# 12. Tabular form for archiving next to schedules
# ---------------------------------------------------------------------------


class TestTabularForm:
    def test_columns_are_exactly_the_declared_fields(self) -> None:
        intent = _intent(
            mode=DispatchMode.CHARGE, power_w=1000.0, price=0.1, confidence=0.8, sequence_id=0
        )
        assert set(intents_to_tabular([intent])) == set(INTENT_FIELDS)

    def test_rows_are_ordered_by_sequence(self) -> None:
        plan = _idle_run(3)
        columns = intents_to_tabular(plan.intents)
        assert columns["sequence_id"] == [0, 1, 2]
        assert len(columns["start"]) == 3

    def test_absent_beliefs_become_nulls_not_zeros(self) -> None:
        plan = _idle_run(2)
        columns = intents_to_tabular(plan.intents)
        assert columns["expected_buy_price_eur_per_kwh"] == [None, None]
        assert columns["confidence"] == [None, None]

    def test_out_of_order_rows_are_refused(self) -> None:
        """A silently reordered archive would misattribute every interval."""
        expiry = EPOCH + timedelta(hours=1)
        i0 = _intent(sequence_id=1, expires_at=expiry)
        with pytest.raises(DispatchError, match="reorder the contract"):
            intents_to_tabular([i0])

    def test_arrow_schema_shape(self) -> None:
        schema = dispatch_intents_arrow_schema()
        assert [field.name for field in schema] == list(INTENT_FIELDS)
        belief = {"expected_buy_price_eur_per_kwh", "confidence"}
        import pyarrow as pa

        for field in schema:
            if field.name in belief:
                assert field.nullable, f"{field.name} must be nullable"
            else:
                assert not field.nullable, f"{field.name} must not be nullable"
            if field.name in ("start", "end", "generated_at", "expires_at"):
                assert pa.types.is_timestamp(field.type)
                assert field.type.tz == "UTC"

    def test_tabular_round_trip_rebuilds_the_intents(self) -> None:
        """The tabular view loses nothing the JSON form keeps."""
        spec = _spec()
        h = _horizon(3, load=[1.0] * 3)
        sched = simulate(spec, h, [0.4, 0.0, 0.0], [0.0, 0.3, 0.0], enforce_terminal_soc=False)
        plan = _bridge(sched, h, spec, expected_buy_prices=[0.1, 0.2, 0.3])
        columns = intents_to_tabular(plan.intents)
        rebuilt = [
            DispatchIntent.from_dict({name: columns[name][row] for name in INTENT_FIELDS})
            for row in range(plan.n)
        ]
        assert [intent.to_dict() for intent in rebuilt] == [
            intent.to_dict() for intent in plan.intents
        ]


# ---------------------------------------------------------------------------
# 13. Error behaviour: one clean line, nothing swallowed
# ---------------------------------------------------------------------------


class TestErrorBehaviour:
    def test_dispatch_error_is_a_value_error(self) -> None:
        """Callers may catch the specific contract error or ordinary validation."""
        assert issubclass(DispatchError, ValueError)

    def test_messages_are_one_line(self) -> None:
        cases = [
            lambda: _intent(mode=DispatchMode.CHARGE, power_w=-1.0),
            lambda: _intent(soc_frac=3.0),
            lambda: _idle_run(2, plan_sequence=1).accepts_successor(_idle_run(1)),
            lambda: _intent().bound_to(-5),
        ]
        for case in cases:
            with pytest.raises(DispatchError) as info:
                case()
            message = str(info.value)
            assert "\n" not in message, f"multi-line error: {message!r}"
            assert len(message) < 300, f"long error: {message!r}"

    def test_no_blanket_except_handler_in_the_module(self) -> None:
        """Checked on the AST so documentation prose cannot create or hide a match."""
        offenders: list[str] = []
        for node in ast.walk(ast.parse(_DISPATCH_SOURCE)):
            if not isinstance(node, ast.ExceptHandler):
                continue
            named = getattr(node.type, "id", None) or getattr(node.type, "attr", None)
            if node.type is None or named in {"Exception", "BaseException"}:
                offenders.append(f"line {node.lineno}: except {named or '(bare)'}")
        assert not offenders, f"blanket handler in dispatch.py: {', '.join(offenders)}"

    def test_no_handler_swallows_what_it_catches(self) -> None:
        """Every handler converts and re-raises; a programmer error never disappears."""
        silent: list[str] = []
        for node in ast.walk(ast.parse(_DISPATCH_SOURCE)):
            if not isinstance(node, ast.ExceptHandler):
                continue
            raises = any(isinstance(stmt, ast.Raise) for stmt in node.body) or any(
                isinstance(child, ast.Raise) for stmt in node.body for child in ast.walk(stmt)
            )
            if not raises:
                silent.append(f"line {node.lineno}")
        assert not silent, f"handler that swallows the error at: {', '.join(silent)}"

    def test_converted_errors_chain_to_the_original(self) -> None:
        """A one-line message must not hide which underlying failure produced it."""
        with pytest.raises(DispatchError) as info:
            _intent(mode="boost")
        assert isinstance(info.value.__cause__, ValueError)
        with pytest.raises(DispatchError) as info:
            _intent(mode=DispatchMode.CHARGE, power_w="not-a-number")
        assert isinstance(info.value.__cause__, (TypeError, ValueError))

    def test_a_type_error_is_not_converted(self) -> None:
        """Missing required arguments stay a TypeError: that is a programmer error."""
        with pytest.raises(TypeError):
            DispatchPlan(intents=())  # type: ignore[call-arg]


# ---------------------------------------------------------------------------
# 14. Command policy: TODO-6.3 writer knobs as validated data (no I/O)
# ---------------------------------------------------------------------------


class TestCommandPolicy:
    def _policy(self, **overrides) -> CommandPolicy:
        base = dict(
            max_attempts=3,
            min_setpoint_change_w=50.0,
            min_command_interval_seconds=10.0,
            lease_seconds=60.0,
        )
        base.update(overrides)
        return CommandPolicy(**base)

    def test_retry_budget_is_bounded(self) -> None:
        policy = self._policy(max_attempts=3)
        assert [policy.attempt_allowed(i) for i in range(4)] == [True, True, True, False]
        assert policy.remaining_attempts(0) == 2
        assert policy.remaining_attempts(2) == 0

    def test_no_retries_at_all_is_legal(self) -> None:
        """max_attempts=1 is "send once, never retry" — a valid safety choice."""
        policy = self._policy(max_attempts=1)
        assert policy.attempt_allowed(0) and not policy.attempt_allowed(1)
        assert policy.remaining_attempts(0) == 0

    def test_empty_budget_rejected(self) -> None:
        with pytest.raises(DispatchError, match="max_attempts must be >= 1"):
            self._policy(max_attempts=0)

    def test_exhausted_budget_raises_not_wraps(self) -> None:
        with pytest.raises(DispatchError, match="outside the retry budget"):
            self._policy(max_attempts=2).remaining_attempts(5)

    def test_two_commands_in_flight_is_rejected_not_merely_discouraged(self) -> None:
        with pytest.raises(DispatchError, match="one writer, one command in flight"):
            self._policy(allow_concurrent_in_flight=True)

    def test_negative_knobs_rejected(self) -> None:
        with pytest.raises(DispatchError, match="min_setpoint_change_w"):
            self._policy(min_setpoint_change_w=-1.0)
        with pytest.raises(DispatchError, match="min_command_interval_seconds"):
            self._policy(min_command_interval_seconds=-1.0)
        with pytest.raises(DispatchError, match="lease_seconds must be > 0"):
            self._policy(lease_seconds=0.0)

    def test_anti_thrash_threshold(self) -> None:
        policy = self._policy(min_setpoint_change_w=100.0)
        assert policy.change_is_worth_sending(500.0, 620.0)
        assert not policy.change_is_worth_sending(500.0, 540.0)
        assert policy.change_is_worth_sending(500.0, 400.0)

    def test_lease_deadline_is_absolute(self) -> None:
        deadline = self._policy(lease_seconds=60.0).lease_deadline(EPOCH)
        assert deadline == EPOCH + timedelta(seconds=60)
        assert deadline.utcoffset() == timedelta(0)

    def test_policy_carries_no_writer_state(self) -> None:
        """Data only: no device handle, no socket, no clock — TODO-4.6 stops short."""
        declared = {field.name for field in dataclasses.fields(CommandPolicy)}
        assert declared == {
            "max_attempts",
            "min_setpoint_change_w",
            "min_command_interval_seconds",
            "lease_seconds",
            "allow_concurrent_in_flight",
        }

    def test_safe_idle_still_respects_the_sequence(self) -> None:
        """A lease timeout stops the request without breaking monotonicity or validity."""
        plan = _idle_run(2, plan_sequence=8, lifetime_minutes=30)
        stop = plan.safe_idle_plan()
        assert stop.plan_sequence == plan.plan_sequence
        assert stop.expires_at == plan.expires_at
        assert [intent.sequence_id for intent in stop.intents] == [0, 1]
        assert all(intent.mode is DispatchMode.IDLE for intent in stop.intents)

    def test_stop_request_is_valid_past_the_lease_but_not_past_the_interval(self) -> None:
        """The lease ends the *first* request; the stop request outlives it."""
        plan = _idle_run(2, plan_sequence=8, lifetime_minutes=30)
        policy = self._policy(lease_seconds=60.0)
        deadline = policy.lease_deadline(EPOCH)
        stop = plan.safe_idle_plan()
        assert not stop.is_expired(deadline)
        assert stop.actionable_intents(deadline)
        # Past the plan's own expiry both are gone, and only a replan helps.
        assert stop.is_expired(EPOCH + timedelta(minutes=31))
        assert stop.actionable_intents(EPOCH + timedelta(minutes=31)) == []


# ---------------------------------------------------------------------------
# 15. The battery spec travels with the plan
# ---------------------------------------------------------------------------


class TestBatterySpecOnPlan:
    def test_spec_round_trips(self) -> None:
        spec = _spec(terminal_soc_frac=0.4)
        expiry = EPOCH + timedelta(hours=1)
        plan = DispatchPlan(
            intents=[_intent(expires_at=expiry)],
            generated_at=EPOCH,
            plan_sequence=0,
            expires_at=expiry,
            interval_seconds=QUARTER,
            battery_spec=spec,
        )
        restored = plan_from_canonical_json(plan_to_canonical_json(plan))
        assert restored.battery_spec == spec

    def test_absent_spec_is_absent_not_a_default(self) -> None:
        """`None` means the producer did not say — never "any battery will do"."""
        expiry = EPOCH + timedelta(hours=1)
        plan = DispatchPlan(
            intents=[_intent(expires_at=expiry)],
            generated_at=EPOCH,
            plan_sequence=0,
            expires_at=expiry,
            interval_seconds=QUARTER,
        )
        assert plan.battery_spec is None
        payload = json.loads(plan_to_canonical_json(plan))
        assert payload["battery_spec"] is None

    def test_an_impossible_spec_rejected_by_the_kernel(self) -> None:
        """Inverted SOC bounds are refused by ``validate_spec``, not re-checked here."""
        expiry = EPOCH + timedelta(hours=1)
        with pytest.raises(ValueError, match="soc_min_frac"):
            DispatchPlan(
                intents=[_intent(expires_at=expiry)],
                generated_at=EPOCH,
                plan_sequence=0,
                expires_at=expiry,
                interval_seconds=QUARTER,
                battery_spec=_spec(soc_min_frac=0.9, soc_max_frac=0.2),
            )

    def test_a_different_spec_is_a_different_plan(self) -> None:
        """Two plans identical but for the hardware are not the same request."""
        expiry = EPOCH + timedelta(hours=1)
        intents = [_intent(expires_at=expiry)]
        one = DispatchPlan(
            intents=intents,
            generated_at=EPOCH,
            plan_sequence=0,
            expires_at=expiry,
            interval_seconds=QUARTER,
            battery_spec=_spec(capacity_kwh=5.12),
        )
        other = DispatchPlan(
            intents=intents,
            generated_at=EPOCH,
            plan_sequence=0,
            expires_at=expiry,
            interval_seconds=QUARTER,
            battery_spec=_spec(capacity_kwh=10.0),
        )
        assert one.content_hash != other.content_hash

    def test_power_above_the_nameplate_is_rejected_through_the_spec(self) -> None:
        """A hand-built plan carrying a spec is bounded by ``BatterySpec`` itself."""
        expiry = EPOCH + timedelta(hours=1)
        over = _intent(
            mode=DispatchMode.CHARGE, power_w=3600.0, expires_at=expiry
        )  # 3600 W for 900 s = 0.9 kWh, above the 2.5 kW x 0.25 h = 0.625 kWh limit
        with pytest.raises(DispatchError, match="nameplate limit"):
            DispatchPlan(
                intents=[over],
                generated_at=EPOCH,
                plan_sequence=0,
                expires_at=expiry,
                interval_seconds=QUARTER,
                battery_spec=_spec(),
            )

    def test_the_same_request_is_legal_on_a_bigger_rating(self) -> None:
        """The bound comes from the carried spec, not from a hardcoded number."""
        expiry = EPOCH + timedelta(hours=1)
        request = _intent(mode=DispatchMode.CHARGE, power_w=3600.0, expires_at=expiry)
        plan = DispatchPlan(
            intents=[request],
            generated_at=EPOCH,
            plan_sequence=0,
            expires_at=expiry,
            interval_seconds=QUARTER,
            battery_spec=_spec(power_kw=5.0),
        )
        assert plan.intents[0].target_power_w == pytest.approx(3600.0)

    def test_soc_target_outside_the_spec_window_is_rejected(self) -> None:
        expiry = EPOCH + timedelta(hours=1)
        with pytest.raises(DispatchError, match="outside the spec window"):
            DispatchPlan(
                intents=[_intent(expires_at=expiry, soc_frac=0.99)],
                generated_at=EPOCH,
                plan_sequence=0,
                expires_at=expiry,
                interval_seconds=QUARTER,
                battery_spec=_spec(soc_max_frac=0.95),
            )

    def test_the_import_ceiling_is_not_checked_at_plan_level(self) -> None:
        """The ceiling bounds charging against a measured load, which a plan lacks.

        So a plan carrying a spec must not invent one: this request is accepted
        here and refused only once a horizon supplies the load, in the bridge.
        """
        expiry = EPOCH + timedelta(hours=1)
        charge = _intent(mode=DispatchMode.CHARGE, power_w=2000.0, expires_at=expiry)
        plan = DispatchPlan(
            intents=[charge],
            generated_at=EPOCH,
            plan_sequence=0,
            expires_at=expiry,
            interval_seconds=QUARTER,
            battery_spec=_spec(),
        )
        assert plan.n == 1
        # 0.5 kWh is 2000 W: inside the rating, so the spec check above passes.
        # Against a measured load of 4.5 kWh in the same interval the site ceiling
        # leaves no headroom at all, and only the horizon can say so.
        unconstrained = _horizon(1, load=[4.5], import_limit_kw=None)
        request = simulate(_spec(), unconstrained, [0.5], [0.0], enforce_terminal_soc=False)
        with pytest.raises(BatteryPhysicsError, match="ceiling headroom"):
            _bridge(request, _horizon(1, load=[4.5], import_limit_kw=5.0), _spec())
