"""Tests for the replay-only dispatch runtime.

Covers:
- Stale SOC/meter/temp invalidation
- HA disconnect/reconnect and out-of-order events
- Missing/unpublished prices
- Failed/late solver
- Invalid plan expiry
- Duplicate/superseded events
- Event loop responsive while solver busy (Event-based sync, no sleep)
- Restart persisted plan ignored
- Sink has no device writes (AST import guard)
- Fake time, no sleeps (except bounded concurrency synchronization)
- Deterministic replay
- Trigger coalescing with actual concurrent solve
- Planner bridge to solve_oracle / shared battery kernel
- Worker pool: max 1 real worker, generation fencing
- Explicit load forecast required
- Price interval validation (ordered, contiguous)
- Revalidation at acceptance time
- Plan sequence monotonic (never resets)

Synthetic fixtures only — every number is invented, none is household data.
"""

from __future__ import annotations

import ast
import threading
from collections.abc import Sequence
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any

import anyio
import numpy as np
import pytest

from energy_baseline.battery import BatterySpec, Horizon, Schedule
from energy_baseline.dispatch import DispatchPlan, intents_from_schedule
from energy_baseline.oracle import OracleError
from energy_baseline.replay_runtime import (
    ConstantLoadProvider,
    EventType,
    InvalidationReason,
    LoadForecastProvider,
    OraclePlannerAdapter,
    PricePublication,
    RecordingSink,
    ReplayEvent,
    ReplayRecord,
    ReplayRecordKind,
    ReplayRuntime,
    ReplayRuntimeError,
    StateSnapshot,
    SyncPlanner,
    TelemetrySnapshot,
)

QUARTER = 900.0
EPOCH = datetime(2026, 6, 15, 12, 0, 0, tzinfo=UTC)


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _spec(**overrides) -> BatterySpec:
    """Provisional 5.12 kWh / 2.5 kW nameplate."""
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


def _starts(n: int, interval_seconds: float = QUARTER, origin: datetime = EPOCH) -> list[datetime]:
    """A contiguous aware-UTC grid."""
    return [origin + timedelta(seconds=interval_seconds * i) for i in range(n)]


def _prices(n: int = 96, *, origin: datetime = EPOCH) -> PricePublication:
    """A synthetic price publication covering n intervals."""
    starts = _starts(n, origin=origin)
    prices = []
    for i in range(n):
        hour = (origin.hour + i * 0.25) % 24
        if 0 <= hour < 8:
            prices.append(0.05)
        elif 8 <= hour < 14:
            prices.append(0.15)
        elif 14 <= hour < 21:
            prices.append(0.25)
        else:
            prices.append(0.10)
    return PricePublication(
        prices=tuple(prices),
        interval_starts=tuple(starts),
        published_at=origin,
        valid_from=origin,
        valid_to=starts[-1] + timedelta(seconds=QUARTER),
        interval_seconds=QUARTER,
    )


def _telemetry(
    *,
    soc_frac: float = 0.5,
    meter_w: float = 400.0,
    temp_c: float = 25.0,
    at: datetime = EPOCH,
) -> TelemetrySnapshot:
    return TelemetrySnapshot(soc_frac=soc_frac, meter_w=meter_w, temp_c=temp_c, at=at)


def _state_snapshot(
    *,
    soc_frac: float = 0.5,
    meter_w: float = 400.0,
    temp_c: float = 25.0,
    prices: PricePublication | None = None,
    at: datetime = EPOCH,
) -> StateSnapshot:
    return StateSnapshot(
        telemetry=_telemetry(soc_frac=soc_frac, meter_w=meter_w, temp_c=temp_c, at=at),
        prices=prices or _prices(origin=at),
        at=at,
    )


def _idle_schedule(spec: BatterySpec, horizon: Horizon, n: int) -> Schedule:
    """Build a trivial idle schedule for stub planners."""
    charge = np.zeros(n)
    discharge = np.zeros(n)
    soc = np.full(n, spec.soc_bound_kwh("init"))
    return Schedule(
        charge_kwh=charge,
        discharge_kwh=discharge,
        soc_kwh=soc,
        grid_kwh=horizon.load_kwh[:n],
        cost_eur=0.0,
        efc=0.0,
        throughput_kwh=0.0,
        soc_min_frac=spec.initial_soc_frac,
        soc_max_frac=spec.initial_soc_frac,
    )


class _StubPlanner:
    """A synchronous planner stub that returns a fixed plan."""

    def __init__(
        self,
        spec: BatterySpec,
        *,
        n_intervals: int = 4,
        fail: bool = False,
        fail_with: type[Exception] | None = None,
        block_event: threading.Event | None = None,
        signal_event: threading.Event | None = None,
    ) -> None:
        self._spec = spec
        self._n = n_intervals
        self._fail = fail
        self._fail_with = fail_with or OracleError("stub planner failure")
        self._block_event = block_event
        self._signal_event = signal_event
        self.call_count = 0
        self.last_args: dict[str, Any] = {}

    def __call__(
        self,
        spec: BatterySpec,
        horizon: Horizon,
        interval_starts: Sequence[datetime],
        generated_at: datetime,
        expires_at: datetime,
        plan_sequence: int,
    ) -> DispatchPlan:
        self.call_count += 1
        self.last_args = {
            "spec": spec,
            "horizon": horizon,
            "interval_starts": list(interval_starts),
            "generated_at": generated_at,
            "expires_at": expires_at,
            "plan_sequence": plan_sequence,
        }

        if self._block_event is not None:
            self._block_event.wait()

        if self._signal_event is not None:
            self._signal_event.set()

        if self._fail:
            raise self._fail_with

        n = min(self._n, horizon.n)
        schedule = _idle_schedule(spec, horizon, n)
        return intents_from_schedule(
            schedule,
            Horizon(
                interval_seconds=horizon.interval_seconds,
                prices=horizon.prices[:n],
                load_kwh=horizon.load_kwh[:n],
                usable=horizon.usable[:n],
            ),
            battery_spec=spec,
            interval_starts_utc=list(interval_starts[:n]),
            generated_at=generated_at,
            expires_at=expires_at,
            plan_sequence=plan_sequence,
        )


class _SlowPlanner:
    """A planner that blocks until released, for timeout testing."""

    def __init__(
        self,
        spec: BatterySpec,
        *,
        n_intervals: int = 4,
        release_event: threading.Event | None = None,
        started_event: threading.Event | None = None,
    ) -> None:
        self._spec = spec
        self._n = n_intervals
        self._release = release_event or threading.Event()
        self._started = started_event or threading.Event()
        self.call_count = 0

    def __call__(
        self,
        spec: BatterySpec,
        horizon: Horizon,
        interval_starts: Sequence[datetime],
        generated_at: datetime,
        expires_at: datetime,
        plan_sequence: int,
    ) -> DispatchPlan:
        self.call_count += 1
        self._started.set()
        # Block until released (or forever if never released)
        self._release.wait(timeout=30)
        if not self._release.is_set():
            raise OracleError("slow planner: still blocked")

        n = min(self._n, horizon.n)
        schedule = _idle_schedule(spec, horizon, n)
        return intents_from_schedule(
            schedule,
            Horizon(
                interval_seconds=horizon.interval_seconds,
                prices=horizon.prices[:n],
                load_kwh=horizon.load_kwh[:n],
                usable=horizon.usable[:n],
            ),
            battery_spec=spec,
            interval_starts_utc=list(interval_starts[:n]),
            generated_at=generated_at,
            expires_at=expires_at,
            plan_sequence=plan_sequence,
        )


class _ConcurrencyProbePlanner:
    """A planner that blocks on entry and measures how many ran *simultaneously*.

    Every call records itself as in-flight under a lock before waiting on a
    per-call gate, so ``peak`` is the maximum number of real planner threads
    ever live at the same instant — the direct measurement of the runtime's
    "at most one real worker" bound.  ``release_all`` opens the gate for the
    calls already blocked *and* any later one, so a test can unblock a stuck
    worker and still let the queue drain without racing.
    """

    def __init__(self, spec: BatterySpec, *, n_intervals: int = 4) -> None:
        self._spec = spec
        self._n = n_intervals
        self._cond = threading.Condition()
        self._gates: list[threading.Event] = []
        self._open = False
        self._inflight = 0
        self.peak = 0
        self.call_count = 0
        #: ``initial_soc_frac`` of the spec each solve was really handed.
        self.observed_socs: list[float] = []
        #: First price of the horizon each solve was really handed (EUR/kWh).
        self.observed_price_head: list[float] = []

    def wait_for_calls(self, n: int, timeout: float = 2.0) -> bool:
        """Block until at least ``n`` calls are in flight."""
        with self._cond:
            return self._cond.wait_for(lambda: self.call_count >= n, timeout=timeout)

    def release_all(self) -> None:
        """Let every blocked and every future call proceed."""
        with self._cond:
            self._open = True
            for gate in self._gates:
                gate.set()
            self._cond.notify_all()

    @property
    def inflight(self) -> int:
        with self._cond:
            return self._inflight

    def __call__(
        self,
        spec: BatterySpec,
        horizon: Horizon,
        interval_starts: Sequence[datetime],
        generated_at: datetime,
        expires_at: datetime,
        plan_sequence: int,
    ) -> DispatchPlan:
        gate = threading.Event()
        with self._cond:
            self.call_count += 1
            self._inflight += 1
            self.peak = max(self.peak, self._inflight)
            self.observed_socs.append(spec.initial_soc_frac)
            self.observed_price_head.append(float(horizon.prices[0]))
            self._gates.append(gate)
            gate_started_open = self._open
            self._cond.notify_all()

        if not gate_started_open:
            # Blocked until the test releases; bounded so a defect cannot hang
            # the suite forever.
            gate.wait(timeout=30)

        with self._cond:
            self._inflight -= 1
            self._cond.notify_all()

        n = min(self._n, horizon.n)
        schedule = _idle_schedule(spec, horizon, n)
        return intents_from_schedule(
            schedule,
            Horizon(
                interval_seconds=horizon.interval_seconds,
                prices=horizon.prices[:n],
                load_kwh=horizon.load_kwh[:n],
                usable=horizon.usable[:n],
            ),
            battery_spec=spec,
            interval_starts_utc=list(interval_starts[:n]),
            generated_at=generated_at,
            expires_at=expires_at,
            plan_sequence=plan_sequence,
        )


async def _make_runtime(
    spec: BatterySpec | None = None,
    planner: SyncPlanner | None = None,
    load_forecast: LoadForecastProvider | None = None,
    **kwargs,
) -> ReplayRuntime:
    """Create and start a runtime with sensible defaults."""
    spec = spec or _spec()
    planner = planner or _StubPlanner(spec)
    load_forecast = load_forecast or ConstantLoadProvider(0.3)
    sink = RecordingSink()
    runtime = ReplayRuntime(
        battery_spec=spec,
        planner=planner,
        load_forecast=load_forecast,
        sink=sink,
        telemetry_freshness_s=kwargs.pop("telemetry_freshness_s", 120.0),
        price_freshness_s=kwargs.pop("price_freshness_s", 3600.0),
        solve_timeout_s=kwargs.pop("solve_timeout_s", 10.0),
        coalesce_window_s=kwargs.pop("coalesce_window_s", 5.0),
        plan_ttl_s=kwargs.pop("plan_ttl_s", 900.0),
        interval_seconds=kwargs.pop("interval_seconds", QUARTER),
    )
    await runtime.start(at=EPOCH)
    return runtime


async def _feed_and_solve(runtime: ReplayRuntime, at: datetime = EPOCH) -> None:
    """Feed telemetry + prices and trigger a solve, then drain."""
    await runtime.push_telemetry(soc_frac=0.5, meter_w=400.0, temp_c=25.0, at=at)
    prices = _prices(origin=at)
    await runtime.push_price_publication(
        prices.prices,
        interval_starts=prices.interval_starts,
        published_at=prices.published_at,
        valid_from=prices.valid_from,
        valid_to=prices.valid_to,
        at=at,
    )
    await runtime.push_scheduled_trigger(at=at)
    await runtime.drain(timeout_s=5.0)


async def _feed_inputs(
    runtime: ReplayRuntime, at: datetime, *, soc_frac: float = 0.5
) -> PricePublication:
    """Feed telemetry + a matching price publication at ``at`` (no trigger)."""
    await runtime.push_telemetry(soc_frac=soc_frac, meter_w=400.0, temp_c=25.0, at=at)
    prices = _prices(origin=at)
    await runtime.push_price_publication(
        prices.prices,
        interval_starts=prices.interval_starts,
        published_at=prices.published_at,
        valid_from=prices.valid_from,
        valid_to=prices.valid_to,
        at=at,
    )
    return prices


def _planner_thread_count() -> int:
    """Live threads belonging to any replay planner executor."""
    return sum(1 for t in threading.enumerate() if t.name.startswith("replay-planner"))


# ---------------------------------------------------------------------------
# Construction and validation
# ---------------------------------------------------------------------------


class TestConstruction:
    """Runtime construction and parameter validation."""

    def test_valid_construction(self) -> None:
        spec = _spec()
        planner = _StubPlanner(spec)
        sink = RecordingSink()
        runtime = ReplayRuntime(spec, planner, ConstantLoadProvider(0.3), sink)
        assert not runtime.is_started
        assert not runtime.is_connected
        assert runtime.now is None
        assert runtime.active_plan is None

    def test_negative_telemetry_freshness_rejected(self) -> None:
        with pytest.raises(ReplayRuntimeError, match="telemetry_freshness_s"):
            ReplayRuntime(
                _spec(),
                _StubPlanner(_spec()),
                ConstantLoadProvider(0.3),
                RecordingSink(),
                telemetry_freshness_s=-1,
            )

    def test_negative_price_freshness_rejected(self) -> None:
        with pytest.raises(ReplayRuntimeError, match="price_freshness_s"):
            ReplayRuntime(
                _spec(),
                _StubPlanner(_spec()),
                ConstantLoadProvider(0.3),
                RecordingSink(),
                price_freshness_s=0,
            )

    def test_negative_solve_timeout_rejected(self) -> None:
        with pytest.raises(ReplayRuntimeError, match="solve_timeout_s"):
            ReplayRuntime(
                _spec(),
                _StubPlanner(_spec()),
                ConstantLoadProvider(0.3),
                RecordingSink(),
                solve_timeout_s=0,
            )

    def test_invalid_battery_spec_rejected(self) -> None:
        bad_spec = BatterySpec(
            capacity_kwh=-1,
            power_kw=2.5,
            charge_efficiency=0.9,
            discharge_efficiency=0.9,
            soc_min_frac=0.1,
            soc_max_frac=0.95,
            initial_soc_frac=0.5,
        )
        with pytest.raises(ValueError):
            ReplayRuntime(
                bad_spec, _StubPlanner(bad_spec), ConstantLoadProvider(0.3), RecordingSink()
            )

    def test_constant_load_negative_rejected(self) -> None:
        with pytest.raises(ReplayRuntimeError, match="load_kwh"):
            ConstantLoadProvider(-0.1)

    def test_constant_load_nan_rejected(self) -> None:
        with pytest.raises(ReplayRuntimeError, match="load_kwh"):
            ConstantLoadProvider(float("nan"))


# ---------------------------------------------------------------------------
# Lifecycle: start, stop, restart
# ---------------------------------------------------------------------------


class TestLifecycle:
    """Runtime start/stop/restart behavior."""

    @pytest.mark.anyio
    async def test_start_sets_connected(self) -> None:
        runtime = await _make_runtime()
        assert runtime.is_started
        assert runtime.is_connected
        assert runtime.now == EPOCH

    @pytest.mark.anyio
    async def test_start_records_event(self) -> None:
        runtime = await _make_runtime()
        records = runtime.sink.records_of(ReplayRecordKind.RUNTIME_STARTED)
        assert len(records) == 1
        assert records[0].at == EPOCH

    @pytest.mark.anyio
    async def test_stop_clears_connected(self) -> None:
        runtime = await _make_runtime()
        await runtime.stop(at=EPOCH + timedelta(minutes=5))
        assert not runtime.is_started
        assert not runtime.is_connected

    @pytest.mark.anyio
    async def test_start_after_stop_reopens_pool_and_solves(self) -> None:
        """A stopped runtime restarts: only a joined, closed pool is replaced."""
        spec = _spec()
        planner = _StubPlanner(spec)
        runtime = await _make_runtime(spec=spec, planner=planner, telemetry_freshness_s=1e9)
        await _feed_and_solve(runtime, EPOCH)
        assert runtime.active_plan is not None
        calls = planner.call_count

        await runtime.stop(at=EPOCH + timedelta(seconds=1))
        assert not runtime.is_started

        restart = EPOCH + timedelta(minutes=2)
        await runtime.start(at=restart)
        assert runtime.is_started and runtime.is_connected
        assert runtime._worker_busy is False, "stop() joined the worker"

        await _feed_and_solve(runtime, restart)
        assert planner.call_count == calls + 1
        assert runtime.active_plan is not None

    @pytest.mark.anyio
    async def test_restart_invalidates_plan(self) -> None:
        runtime = await _make_runtime()
        await _feed_and_solve(runtime)
        assert runtime.active_plan is not None

        restart_time = EPOCH + timedelta(minutes=5)
        await runtime.push_restart(at=restart_time)
        assert runtime.active_plan is None

        invalidated = runtime.sink.records_of(ReplayRecordKind.PLAN_INVALIDATED)
        reasons = [r.details.get("reason") for r in invalidated]
        assert InvalidationReason.RESTART.value in reasons

    @pytest.mark.anyio
    async def test_restart_clears_observations(self) -> None:
        """Restart clears all observations and requires new full snapshot."""
        runtime = await _make_runtime()
        await _feed_and_solve(runtime)
        assert runtime._state.latest_telemetry is not None
        assert runtime._state.latest_prices is not None

        await runtime.push_restart(at=EPOCH + timedelta(minutes=5))
        assert runtime._state.latest_telemetry is None
        assert runtime._state.latest_prices is None

    @pytest.mark.anyio
    async def test_restart_ignores_persisted_plan(self) -> None:
        """A restart must never resume a persisted plan as authority."""
        runtime = await _make_runtime()
        await _feed_and_solve(runtime)
        plan_before = runtime.active_plan
        assert plan_before is not None

        await runtime.push_restart(at=EPOCH + timedelta(minutes=10))
        assert runtime.active_plan is None

    @pytest.mark.anyio
    async def test_push_before_start_rejected(self) -> None:
        spec = _spec()
        runtime = ReplayRuntime(
            spec, _StubPlanner(spec), ConstantLoadProvider(0.3), RecordingSink()
        )
        with pytest.raises(ReplayRuntimeError, match="not started"):
            await runtime.push_telemetry(soc_frac=0.5, meter_w=400.0, temp_c=25.0, at=EPOCH)

    @pytest.mark.anyio
    async def test_naive_datetime_rejected(self) -> None:
        runtime = await _make_runtime()
        with pytest.raises(ReplayRuntimeError, match="aware UTC"):
            await runtime.push_event(
                ReplayEvent(EventType.TELEMETRY, at=datetime(2026, 1, 1), payload=None)
            )


# ---------------------------------------------------------------------------
# Plan sequence monotonic
# ---------------------------------------------------------------------------


class TestPlanSequenceMonotonic:
    """Plan sequence is monotonic across generations (never resets)."""

    @pytest.mark.anyio
    async def test_sequence_increments(self) -> None:
        runtime = await _make_runtime()
        await _feed_and_solve(runtime)
        seq1 = runtime.plan_sequence
        assert seq1 >= 1

        # Trigger another solve
        t2 = EPOCH + timedelta(seconds=10)
        await runtime.push_telemetry(soc_frac=0.6, meter_w=400.0, temp_c=25.0, at=t2)
        await runtime.push_scheduled_trigger(at=t2)
        await runtime.drain(timeout_s=5.0)
        seq2 = runtime.plan_sequence
        assert seq2 > seq1

    @pytest.mark.anyio
    async def test_sequence_monotonic_across_restart(self) -> None:
        """Sequence never resets across restart."""
        runtime = await _make_runtime()
        await _feed_and_solve(runtime)
        seq_before = runtime.plan_sequence

        await runtime.push_restart(at=EPOCH + timedelta(minutes=5))
        # Sequence is preserved (monotonic)
        assert runtime.plan_sequence == seq_before


# ---------------------------------------------------------------------------
# Telemetry freshness
# ---------------------------------------------------------------------------


class TestTelemetryFreshness:
    """Stale/missing telemetry invalidates the active plan."""

    @pytest.mark.anyio
    async def test_stale_soc_invalidates_plan(self) -> None:
        runtime = await _make_runtime(telemetry_freshness_s=60.0)
        await _feed_and_solve(runtime)
        assert runtime.active_plan is not None

        stale_time = EPOCH + timedelta(seconds=121)
        await runtime.advance_to(stale_time)
        assert runtime.active_plan is None

        invalidated = runtime.sink.records_of(ReplayRecordKind.PLAN_INVALIDATED)
        reasons = [r.details.get("reason") for r in invalidated]
        assert InvalidationReason.STALE_TELEMETRY.value in reasons

    @pytest.mark.anyio
    async def test_missing_telemetry_blocks_solve(self) -> None:
        runtime = await _make_runtime()
        prices = _prices(origin=EPOCH)
        await runtime.push_price_publication(
            prices.prices,
            interval_starts=prices.interval_starts,
            published_at=prices.published_at,
            valid_from=prices.valid_from,
            valid_to=prices.valid_to,
            at=EPOCH,
        )
        await runtime.push_scheduled_trigger(at=EPOCH)
        await runtime.drain(timeout_s=2.0)
        assert runtime.active_plan is None

    @pytest.mark.anyio
    async def test_fresh_telemetry_keeps_plan(self) -> None:
        runtime = await _make_runtime(telemetry_freshness_s=600.0)
        await _feed_and_solve(runtime)
        assert runtime.active_plan is not None

        await runtime.advance_to(EPOCH + timedelta(seconds=300))
        assert runtime.active_plan is not None


# ---------------------------------------------------------------------------
# Disconnect / reconnect
# ---------------------------------------------------------------------------


class TestDisconnectReconnect:
    """HA disconnect/reconnect invalidates plans and requires fresh snapshot."""

    @pytest.mark.anyio
    async def test_disconnect_invalidates_plan(self) -> None:
        runtime = await _make_runtime()
        await _feed_and_solve(runtime)
        assert runtime.active_plan is not None

        await runtime.push_disconnect(at=EPOCH + timedelta(minutes=1))
        assert not runtime.is_connected
        assert runtime.active_plan is None

    @pytest.mark.anyio
    async def test_reconnect_without_snapshot_no_plan(self) -> None:
        runtime = await _make_runtime()
        await _feed_and_solve(runtime)

        await runtime.push_disconnect(at=EPOCH + timedelta(minutes=1))
        await runtime.push_reconnect(at=EPOCH + timedelta(minutes=2))
        assert runtime.is_connected
        assert runtime.active_plan is None

    @pytest.mark.anyio
    async def test_reconnect_with_snapshot_restores_state(self) -> None:
        runtime = await _make_runtime()
        await runtime.push_disconnect(at=EPOCH + timedelta(minutes=1))

        snapshot = _state_snapshot(soc_frac=0.7, at=EPOCH + timedelta(minutes=2))
        await runtime.push_reconnect(at=EPOCH + timedelta(minutes=2), snapshot=snapshot)
        assert runtime.is_connected
        assert runtime._state.latest_telemetry is not None
        assert runtime._state.latest_telemetry.soc_frac == 0.7

    @pytest.mark.anyio
    async def test_events_ignored_while_disconnected(self) -> None:
        runtime = await _make_runtime()
        await runtime.push_disconnect(at=EPOCH + timedelta(seconds=1))

        await runtime.push_telemetry(
            soc_frac=0.5, meter_w=400.0, temp_c=25.0, at=EPOCH + timedelta(seconds=2)
        )
        assert runtime._state.latest_telemetry is None

    @pytest.mark.anyio
    async def test_out_of_order_telemetry_rejected(self) -> None:
        """A late-arriving older reading is rejected, not accepted as current."""
        runtime = await _make_runtime()
        await runtime.push_telemetry(soc_frac=0.5, meter_w=400.0, temp_c=25.0, at=EPOCH)

        # Arrives now, but its source time is older than the latest reading
        await runtime.push_telemetry(
            soc_frac=0.6,
            meter_w=500.0,
            temp_c=25.0,
            at=EPOCH,
            source_at=EPOCH - timedelta(seconds=30),
        )
        # Rejected — latest still the original
        assert runtime._state.latest_telemetry.soc_frac == 0.5

        # ...and it is recorded with an accurate superseded-event reason
        received = runtime.sink.records_of(ReplayRecordKind.EVENT_RECEIVED)
        rejected = [r for r in received if r.details.get("rejected")]
        assert any(
            r.details["rejected"] == InvalidationReason.SUPERSEDED_EVENT.value for r in rejected
        )

    @pytest.mark.anyio
    async def test_duplicate_telemetry_recorded_not_reprocessed(self) -> None:
        """An identical repeated reading is recorded as a duplicate event."""
        runtime = await _make_runtime()
        await runtime.push_telemetry(soc_frac=0.5, meter_w=400.0, temp_c=25.0, at=EPOCH)
        await runtime.push_telemetry(soc_frac=0.5, meter_w=400.0, temp_c=25.0, at=EPOCH)

        received = runtime.sink.records_of(ReplayRecordKind.EVENT_RECEIVED)
        rejected = [r for r in received if r.details.get("rejected")]
        assert any(
            r.details["rejected"] == InvalidationReason.DUPLICATE_EVENT.value for r in rejected
        )

    @pytest.mark.anyio
    async def test_superseded_telemetry_does_not_invalidate_plan(self) -> None:
        """A late older reading must not knock down a plan built on newer data."""
        runtime = await _make_runtime(telemetry_freshness_s=1e9)
        await _feed_and_solve(runtime)
        assert runtime.active_plan is not None

        await runtime.push_telemetry(
            soc_frac=0.9,
            meter_w=100.0,
            temp_c=20.0,
            at=EPOCH,
            source_at=EPOCH - timedelta(seconds=60),
        )
        # Plan survives: the newest snapshot is still authoritative
        assert runtime.active_plan is not None

    @pytest.mark.anyio
    async def test_future_source_telemetry_rejected(self) -> None:
        """A reading whose source time is in the future is forbidden."""
        runtime = await _make_runtime()
        await runtime.push_telemetry(soc_frac=0.5, meter_w=400.0, temp_c=25.0, at=EPOCH)

        await runtime.push_telemetry(
            soc_frac=0.7,
            meter_w=500.0,
            temp_c=25.0,
            at=EPOCH,
            source_at=EPOCH + timedelta(minutes=5),
        )
        assert runtime._state.latest_telemetry.soc_frac == 0.5

    @pytest.mark.anyio
    async def test_out_of_order_events_rejected(self) -> None:
        runtime = await _make_runtime()
        await runtime.push_telemetry(soc_frac=0.5, meter_w=400.0, temp_c=25.0, at=EPOCH)

        with pytest.raises(ReplayRuntimeError, match="before current time"):
            await runtime.push_telemetry(
                soc_frac=0.6,
                meter_w=500.0,
                temp_c=25.0,
                at=EPOCH - timedelta(seconds=1),
            )


# ---------------------------------------------------------------------------
# Price publication validation
# ---------------------------------------------------------------------------


class TestPricePublication:
    """Missing/unpublished prices invalidate the active plan."""

    @pytest.mark.anyio
    async def test_missing_prices_blocks_solve(self) -> None:
        runtime = await _make_runtime()
        await runtime.push_telemetry(soc_frac=0.5, meter_w=400.0, temp_c=25.0, at=EPOCH)
        await runtime.push_scheduled_trigger(at=EPOCH)
        await runtime.drain(timeout_s=2.0)
        assert runtime.active_plan is None

    @pytest.mark.anyio
    async def test_stale_prices_invalidates_plan(self) -> None:
        # plan_ttl far beyond the price window so price staleness is the
        # binding reason, not plan expiry.
        runtime = await _make_runtime(plan_ttl_s=86400.0, telemetry_freshness_s=1_000_000.0)
        await runtime.push_telemetry(soc_frac=0.5, meter_w=400.0, temp_c=25.0, at=EPOCH)
        prices = _prices(n=4, origin=EPOCH)  # 4 intervals = 1 h window
        await runtime.push_price_publication(
            prices.prices,
            interval_starts=prices.interval_starts,
            published_at=prices.published_at,
            valid_from=prices.valid_from,
            valid_to=prices.valid_to,
            at=EPOCH,
        )
        await runtime.push_scheduled_trigger(at=EPOCH)
        await runtime.drain(timeout_s=5.0)
        assert runtime.active_plan is not None

        await runtime.advance_to(prices.valid_to + timedelta(seconds=1))
        assert runtime.active_plan is None

        invalidated = runtime.sink.records_of(ReplayRecordKind.PLAN_INVALIDATED)
        reasons = [r.details.get("reason") for r in invalidated]
        assert InvalidationReason.STALE_PRICES.value in reasons

    @pytest.mark.anyio
    async def test_price_publication_validated_on_construction(self) -> None:
        starts = _starts(4)
        with pytest.raises(ReplayRuntimeError, match="published_at.*must not be after"):
            PricePublication(
                prices=(0.1, 0.2, 0.3, 0.15),
                interval_starts=tuple(starts),
                published_at=starts[0] + timedelta(hours=1),
                valid_from=starts[0],
                valid_to=starts[-1] + timedelta(seconds=QUARTER),
            )

    @pytest.mark.anyio
    async def test_empty_prices_rejected(self) -> None:
        with pytest.raises(ReplayRuntimeError, match="at least one price"):
            PricePublication(
                prices=(),
                interval_starts=(),
                published_at=EPOCH,
                valid_from=EPOCH,
                valid_to=EPOCH + timedelta(hours=1),
            )

    @pytest.mark.anyio
    async def test_mismatched_lengths_rejected(self) -> None:
        starts = _starts(4)
        with pytest.raises(ReplayRuntimeError, match="entries"):
            PricePublication(
                prices=(0.1, 0.2),
                interval_starts=tuple(starts),
                published_at=EPOCH - timedelta(hours=1),
                valid_from=starts[0],
                valid_to=starts[-1] + timedelta(seconds=QUARTER),
            )

    @pytest.mark.anyio
    async def test_non_contiguous_intervals_rejected(self) -> None:
        """Price intervals must be contiguous at interval_seconds spacing."""
        starts = _starts(4)
        # Make a gap
        bad_starts = [starts[0], starts[1], starts[2] + timedelta(seconds=1800), starts[3]]
        with pytest.raises(ReplayRuntimeError, match="not contiguous"):
            PricePublication(
                prices=(0.1, 0.2, 0.3, 0.15),
                interval_starts=tuple(bad_starts),
                published_at=EPOCH - timedelta(hours=1),
                valid_from=starts[0],
                valid_to=bad_starts[-1] + timedelta(seconds=QUARTER),
            )

    @pytest.mark.anyio
    async def test_unordered_intervals_rejected(self) -> None:
        """Price intervals must be ordered."""
        starts = _starts(4)
        bad_starts = [starts[0], starts[2], starts[1], starts[3]]
        with pytest.raises(ReplayRuntimeError, match="not contiguous"):
            PricePublication(
                prices=(0.1, 0.2, 0.3, 0.15),
                interval_starts=tuple(bad_starts),
                published_at=EPOCH - timedelta(hours=1),
                valid_from=starts[0],
                valid_to=starts[-1] + timedelta(seconds=QUARTER),
            )

    @pytest.mark.anyio
    async def test_first_start_must_match_valid_from(self) -> None:
        starts = _starts(4)
        with pytest.raises(ReplayRuntimeError, match="first interval start"):
            PricePublication(
                prices=(0.1, 0.2, 0.3, 0.15),
                interval_starts=tuple(starts),
                published_at=EPOCH - timedelta(hours=1),
                valid_from=starts[0] + timedelta(seconds=QUARTER),  # Mismatch
                valid_to=starts[-1] + timedelta(seconds=QUARTER),
            )


# ---------------------------------------------------------------------------
# Solver failures and timeouts
# ---------------------------------------------------------------------------


class TestSolverFailures:
    """Failed/late solver invalidates the active plan."""

    @pytest.mark.anyio
    async def test_solver_failure_invalidates(self) -> None:
        spec = _spec()
        planner = _StubPlanner(spec, fail=True)
        runtime = await _make_runtime(spec=spec, planner=planner)
        await runtime.push_telemetry(soc_frac=0.5, meter_w=400.0, temp_c=25.0, at=EPOCH)
        prices = _prices(origin=EPOCH)
        await runtime.push_price_publication(
            prices.prices,
            interval_starts=prices.interval_starts,
            published_at=prices.published_at,
            valid_from=prices.valid_from,
            valid_to=prices.valid_to,
            at=EPOCH,
        )
        await runtime.push_scheduled_trigger(at=EPOCH)
        await runtime.drain(timeout_s=5.0)

        assert runtime.active_plan is None
        failed = runtime.sink.records_of(ReplayRecordKind.SOLVE_FAILED)
        assert len(failed) >= 1

    @pytest.mark.anyio
    async def test_solver_timeout_invalidates(self) -> None:
        spec = _spec()
        release = threading.Event()
        started = threading.Event()
        planner = _SlowPlanner(spec, release_event=release, started_event=started)
        runtime = await _make_runtime(spec=spec, planner=planner, solve_timeout_s=0.3)
        await runtime.push_telemetry(soc_frac=0.5, meter_w=400.0, temp_c=25.0, at=EPOCH)
        prices = _prices(origin=EPOCH)
        await runtime.push_price_publication(
            prices.prices,
            interval_starts=prices.interval_starts,
            published_at=prices.published_at,
            valid_from=prices.valid_from,
            valid_to=prices.valid_to,
            at=EPOCH,
        )
        await runtime.push_scheduled_trigger(at=EPOCH)
        # Wait for timeout
        await runtime.drain(timeout_s=3.0)

        assert runtime.active_plan is None
        timeouts = runtime.sink.records_of(ReplayRecordKind.SOLVE_TIMEOUT)
        assert len(timeouts) >= 1

        # Release the worker so it can finish
        release.set()
        await anyio.sleep(0.2)

    @pytest.mark.anyio
    async def test_late_solve_fenced_by_generation(self) -> None:
        """A solve that completes after timeout is fenced by generation ID."""
        spec = _spec()
        release = threading.Event()
        started = threading.Event()
        planner = _SlowPlanner(spec, release_event=release, started_event=started)
        runtime = await _make_runtime(spec=spec, planner=planner, solve_timeout_s=0.3)
        await runtime.push_telemetry(soc_frac=0.5, meter_w=400.0, temp_c=25.0, at=EPOCH)
        prices = _prices(origin=EPOCH)
        await runtime.push_price_publication(
            prices.prices,
            interval_starts=prices.interval_starts,
            published_at=prices.published_at,
            valid_from=prices.valid_from,
            valid_to=prices.valid_to,
            at=EPOCH,
        )
        await runtime.push_scheduled_trigger(at=EPOCH)
        await runtime.drain(timeout_s=3.0)
        assert runtime.active_plan is None

        # Release the late worker
        release.set()
        await anyio.sleep(0.5)
        # Still no plan (late completion was fenced)
        assert runtime.active_plan is None

        # Verify generation IDs are different
        timeouts = runtime.sink.records_of(ReplayRecordKind.SOLVE_TIMEOUT)
        completed = runtime.sink.records_of(ReplayRecordKind.SOLVE_COMPLETED)
        assert len(timeouts) >= 1
        assert len(completed) == 0  # Late result was not accepted


# ---------------------------------------------------------------------------
# Worker pool: max 1 real worker
# ---------------------------------------------------------------------------


class TestWorkerPool:
    """At most one real worker at a time; no new solve until old finishes."""

    @pytest.mark.anyio
    async def test_no_concurrent_workers(self) -> None:
        """Second trigger while worker busy is coalesced, not a second worker."""
        spec = _spec()
        release = threading.Event()
        started = threading.Event()
        planner = _SlowPlanner(spec, n_intervals=4, release_event=release, started_event=started)
        runtime = await _make_runtime(spec=spec, planner=planner, solve_timeout_s=30.0)
        await runtime.push_telemetry(soc_frac=0.5, meter_w=400.0, temp_c=25.0, at=EPOCH)
        prices = _prices(origin=EPOCH)
        await runtime.push_price_publication(
            prices.prices,
            interval_starts=prices.interval_starts,
            published_at=prices.published_at,
            valid_from=prices.valid_from,
            valid_to=prices.valid_to,
            at=EPOCH,
        )

        # First trigger starts solve
        await runtime.push_scheduled_trigger(at=EPOCH)
        # Wait for worker to actually start
        started.wait(timeout=2.0)
        assert runtime.is_solving

        # Second trigger while worker busy — coalesced
        await runtime.push_scheduled_trigger(at=EPOCH + timedelta(seconds=1))
        triggers = runtime.sink.records_of(ReplayRecordKind.TRIGGER_FIRED)
        coalesced = [t for t in triggers if t.details.get("coalesced")]
        assert len(coalesced) >= 1

        # Only one solve requested
        requested = runtime.sink.records_of(ReplayRecordKind.SOLVE_REQUESTED)
        assert len(requested) == 1

        # Release worker
        release.set()
        await runtime.drain(timeout_s=5.0)

    @pytest.mark.anyio
    async def test_worker_saturation_recorded_after_timeout(self) -> None:
        """After a timeout the worker is still running; a new trigger records saturation."""
        spec = _spec()
        release = threading.Event()
        started = threading.Event()
        planner = _SlowPlanner(spec, release_event=release, started_event=started)
        # Timeout well under the planner's 30 s internal wait, so the worker is
        # still blocked after the runtime has given up on it.
        runtime = await _make_runtime(spec=spec, planner=planner, solve_timeout_s=0.2)
        await runtime.push_telemetry(soc_frac=0.5, meter_w=400.0, temp_c=25.0, at=EPOCH)
        prices = _prices(origin=EPOCH)
        await runtime.push_price_publication(
            prices.prices,
            interval_starts=prices.interval_starts,
            published_at=prices.published_at,
            valid_from=prices.valid_from,
            valid_to=prices.valid_to,
            at=EPOCH,
        )

        await runtime.push_scheduled_trigger(at=EPOCH)
        started.wait(timeout=2.0)

        # Let the timeout fire — pending_solve is cleared but the worker is not done
        await runtime.drain(timeout_s=3.0)
        assert not runtime.is_solving  # timeout cleared the request
        assert planner.call_count == 1

        # Now a new trigger arrives while the real worker is still blocked.
        await runtime.push_scheduled_trigger(at=EPOCH + timedelta(seconds=1))

        saturations = runtime.sink.records_of(ReplayRecordKind.WORKER_SATURATION)
        assert len(saturations) == 1, (
            "no new solve may start while the previous worker is still running; "
            "the trigger must be recorded as saturation"
        )
        # And crucially: no second worker was launched
        assert planner.call_count == 1

        release.set()
        await runtime.drain(timeout_s=5.0)

    @pytest.mark.anyio
    async def test_restart_via_start_does_not_launch_second_worker(self) -> None:
        """BLOCKING #1 regression: ``start()`` over a stuck worker keeps one worker.

        ``start()`` is the executor-replacing restart path (``push_restart`` is a
        different, already-safe path).  It used to time out waiting for the
        worker, then replace the pool and clear worker occupancy — so a solve
        that was still executing kept its thread while the fresh pool accepted a
        *second* real worker.  A restart must fence the old generation without
        pretending its thread is gone.
        """
        spec = _spec()
        planner = _ConcurrencyProbePlanner(spec)
        runtime = await _make_runtime(
            spec=spec,
            planner=planner,
            solve_timeout_s=0.2,
            telemetry_freshness_s=1e9,
        )
        await _feed_inputs(runtime, EPOCH, soc_frac=0.5)
        await runtime.push_scheduled_trigger(at=EPOCH)
        assert planner.wait_for_calls(1), "worker never entered the planner"
        assert planner.call_count == 1
        assert planner.inflight == 1
        executor_before = runtime._executor
        stuck_generation = runtime._active_generation
        assert stuck_generation, "the in-flight generation should be recorded"
        threads_before = _planner_thread_count()

        # Let the runtime give up on the solve; its thread stays alive.  This is
        # the exact state the old code mis-reported as "worker free".
        await anyio.sleep(0.4)
        assert runtime.sink.records_of(ReplayRecordKind.SOLVE_TIMEOUT)
        assert runtime._worker_busy, "a timed-out worker still owns the slot"

        # Restart through start() while that worker is still executing.
        restart = EPOCH + timedelta(minutes=5)
        await runtime.start(at=restart)

        # A restart clears observations, so re-feed everything, then trigger.
        await _feed_inputs(runtime, restart, soc_frac=0.6)
        await runtime.push_scheduled_trigger(at=restart)

        # The stuck worker still owns the single slot: no second worker, and the
        # restarting runtime must not have abandoned a pool thread either.
        assert planner.call_count == 1, "restart via start() launched a second worker"
        assert planner.peak == 1
        assert planner.inflight == 1
        assert _planner_thread_count() <= threads_before, "restart created a new pool thread"
        assert runtime._executor is executor_before, "restart must not replace the executor"
        assert runtime._active_generation == stuck_generation, (
            "the stuck generation must stay fenced in until its thread ends"
        )
        assert runtime._worker_busy is True
        assert runtime.sink.records_of(ReplayRecordKind.WORKER_SATURATION)

        # Once the stuck worker truly ends, the deferred trigger may run.
        planner.release_all()
        await runtime.drain(timeout_s=10.0)
        assert planner.peak == 1, "two real workers ran concurrently"
        assert planner.call_count == 2, "the deferred trigger must re-solve"
        assert planner.inflight == 0
        assert runtime.active_plan is not None
        assert planner.observed_socs == [0.5, 0.6], (
            "the post-restart solve must use the post-restart snapshot"
        )

    @pytest.mark.anyio
    async def test_repeated_restarts_never_exceed_one_worker(self) -> None:
        """BLOCKING #1 regression: N restarts over blocked workers stay at one worker."""
        spec = _spec()
        planner = _ConcurrencyProbePlanner(spec)
        runtime = await _make_runtime(
            spec=spec,
            planner=planner,
            solve_timeout_s=0.2,
            telemetry_freshness_s=1e9,
        )
        await _feed_inputs(runtime, EPOCH, soc_frac=0.5)
        await runtime.push_scheduled_trigger(at=EPOCH)
        assert planner.wait_for_calls(1)
        await anyio.sleep(0.4)
        threads_before = _planner_thread_count()

        for i in range(4):
            restart = EPOCH + timedelta(minutes=5 * (i + 1))
            await runtime.start(at=restart)
            await _feed_inputs(runtime, restart, soc_frac=0.5 + 0.1 * i)
            await runtime.push_scheduled_trigger(at=restart)
            # Every restart must saturate, never spawn: still exactly one worker
            # in flight and never more than one at any instant.
            assert planner.call_count == 1, f"restart {i} launched an extra worker"
            assert planner.peak == 1, f"restart {i} exceeded the max-1 worker bound"
            assert planner.inflight == 1
            assert runtime._worker_busy is True

        assert _planner_thread_count() == threads_before, (
            "repeated restarts leaked executor threads"
        )

        planner.release_all()
        await runtime.drain(timeout_s=10.0)
        assert planner.peak == 1, "concurrency exceeded one across the restart storm"
        assert planner.call_count == 2, "only the final deferred trigger re-solves"
        assert runtime.active_plan is not None
        # The one surviving re-solve used the *latest* restart's snapshot.
        assert planner.observed_socs == [0.5, 0.8]

    @pytest.mark.anyio
    async def test_restart_while_worker_busy_does_not_launch_second_worker(self) -> None:
        """A blocked worker survives restart; no second real worker is launched."""
        spec = _spec()
        release = threading.Event()
        started = threading.Event()
        planner = _SlowPlanner(spec, release_event=release, started_event=started)
        runtime = await _make_runtime(spec=spec, planner=planner, solve_timeout_s=30.0)
        await runtime.push_telemetry(soc_frac=0.5, meter_w=400.0, temp_c=25.0, at=EPOCH)
        prices = _prices(origin=EPOCH)
        await runtime.push_price_publication(
            prices.prices,
            interval_starts=prices.interval_starts,
            published_at=prices.published_at,
            valid_from=prices.valid_from,
            valid_to=prices.valid_to,
            at=EPOCH,
        )
        await runtime.push_scheduled_trigger(at=EPOCH)
        started.wait(timeout=2.0)
        assert planner.call_count == 1

        # Restart while the worker is blocked.
        await runtime.push_restart(at=EPOCH + timedelta(seconds=5))
        # Restart clears observations, so a new solve cannot even start.
        assert planner.call_count == 1
        assert runtime.active_plan is None
        assert runtime._state.latest_telemetry is None
        assert runtime._state.latest_prices is None

        release.set()
        await anyio.sleep(0.3)
        # Still exactly one real worker ever ran.
        assert planner.call_count == 1

    @pytest.mark.anyio
    async def test_disconnected_during_solve_result_is_fenced(self) -> None:
        """A solve finishing after disconnect cannot install a plan."""
        spec = _spec()
        release = threading.Event()
        started = threading.Event()
        planner = _SlowPlanner(spec, release_event=release, started_event=started)
        runtime = await _make_runtime(spec=spec, planner=planner, solve_timeout_s=30.0)
        await runtime.push_telemetry(soc_frac=0.5, meter_w=400.0, temp_c=25.0, at=EPOCH)
        prices = _prices(origin=EPOCH)
        await runtime.push_price_publication(
            prices.prices,
            interval_starts=prices.interval_starts,
            published_at=prices.published_at,
            valid_from=prices.valid_from,
            valid_to=prices.valid_to,
            at=EPOCH,
        )
        await runtime.push_scheduled_trigger(at=EPOCH)
        started.wait(timeout=2.0)

        await runtime.push_disconnect(at=EPOCH + timedelta(seconds=1))
        assert runtime.active_plan is None

        # Late result arrives while disconnected
        release.set()
        await runtime.drain(timeout_s=5.0)
        assert runtime.active_plan is None
        completed = runtime.sink.records_of(ReplayRecordKind.PLAN_ACCEPTED)
        assert len(completed) == 0, "a fenced generation must never install a plan"


# ---------------------------------------------------------------------------
# Plan expiry
# ---------------------------------------------------------------------------


class TestPlanExpiry:
    """Invalid/expired plans are rejected."""

    @pytest.mark.anyio
    async def test_expired_plan_invalidated_on_advance(self) -> None:
        runtime = await _make_runtime(plan_ttl_s=60.0)
        await _feed_and_solve(runtime)
        assert runtime.active_plan is not None

        await runtime.advance_to(EPOCH + timedelta(seconds=62))
        assert runtime.active_plan is None

        invalidated = runtime.sink.records_of(ReplayRecordKind.PLAN_INVALIDATED)
        reasons = [r.details.get("reason") for r in invalidated]
        assert InvalidationReason.PLAN_EXPIRED.value in reasons


# ---------------------------------------------------------------------------
# Trigger coalescing with actual concurrent solve
# ---------------------------------------------------------------------------


class TestTriggerCoalescing:
    """Scheduled and state-event triggers coalesce during active solve."""

    @pytest.mark.anyio
    async def test_concurrent_trigger_coalesced_during_solve(self) -> None:
        """First trigger starts the only solve; second coalesces; result lands once.

        Non-vacuity: this asserts a *real* second event arriving while the first
        worker is genuinely blocked, and that the coalesced trigger then re-solves
        with the latest snapshot once the worker frees up.
        """
        spec = _spec()
        release = threading.Event()
        started = threading.Event()
        planner = _SlowPlanner(spec, release_event=release, started_event=started)
        runtime = await _make_runtime(
            spec=spec,
            planner=planner,
            solve_timeout_s=30.0,
            telemetry_freshness_s=1e9,
        )
        await runtime.push_telemetry(soc_frac=0.5, meter_w=400.0, temp_c=25.0, at=EPOCH)
        prices = _prices(origin=EPOCH)
        await runtime.push_price_publication(
            prices.prices,
            interval_starts=prices.interval_starts,
            published_at=prices.published_at,
            valid_from=prices.valid_from,
            valid_to=prices.valid_to,
            at=EPOCH,
        )

        # First trigger: starts solve, worker genuinely blocks here.
        await runtime.push_scheduled_trigger(at=EPOCH)
        started.wait(timeout=2.0)
        assert planner.call_count == 1

        # Second trigger arrives while the worker is still blocked.
        await runtime.push_state_event_trigger(at=EPOCH + timedelta(seconds=1))

        triggers = runtime.sink.records_of(ReplayRecordKind.TRIGGER_FIRED)
        assert len(triggers) == 2
        coalesced = [t for t in triggers if t.details.get("coalesced")]
        assert len(coalesced) == 1

        # Still exactly ONE real worker launched while the first is blocked.
        assert planner.call_count == 1
        requested = runtime.sink.records_of(ReplayRecordKind.SOLVE_REQUESTED)
        assert len(requested) == 1

        # Release worker; the coalesced trigger then re-solves.
        release.set()
        await runtime.drain(timeout_s=5.0)

        completed = runtime.sink.records_of(ReplayRecordKind.SOLVE_COMPLETED)
        accepted = runtime.sink.records_of(ReplayRecordKind.PLAN_ACCEPTED)
        # The coalesced trigger re-solved, so two workers ran.  The *first*
        # generation is now refused at acceptance: the second trigger advanced
        # simulated time to 12:00:01, leaving its 12:00:00 anchor interval only
        # partially ahead (see TestAnchorIntervalFreshness).  Exactly one plan —
        # the re-solve — is therefore completed and accepted.
        assert planner.call_count == 2
        assert len(completed) == len(accepted) == 1
        assert runtime.active_plan is not None
        rejected = [
            r
            for r in runtime.sink.records_of(ReplayRecordKind.SOLVE_FAILED)
            if r.details.get("partially_elapsed")
        ]
        assert len(rejected) == 1
        assert rejected[0].details["generation_id"] == requested[0].details["generation_id"]
        # The installed plan is the newer generation and starts in the future.
        assert accepted[0].details["generation_id"] != requested[0].details["generation_id"]
        assert runtime.active_plan.intents[0].start >= runtime.now

    @pytest.mark.anyio
    async def test_changed_input_while_busy_uses_latest_snapshot(self) -> None:
        """A trigger coalesced during a busy solve re-solves from the newest snapshot."""
        spec = _spec()
        release = threading.Event()
        started = threading.Event()
        planner = _SlowPlanner(spec, release_event=release, started_event=started)
        runtime = await _make_runtime(
            spec=spec,
            planner=planner,
            solve_timeout_s=30.0,
            telemetry_freshness_s=1e9,
        )
        await runtime.push_telemetry(soc_frac=0.5, meter_w=400.0, temp_c=25.0, at=EPOCH)
        prices = _prices(origin=EPOCH)
        await runtime.push_price_publication(
            prices.prices,
            interval_starts=prices.interval_starts,
            published_at=prices.published_at,
            valid_from=prices.valid_from,
            valid_to=prices.valid_to,
            at=EPOCH,
        )
        await runtime.push_scheduled_trigger(at=EPOCH)
        started.wait(timeout=2.0)

        # Change telemetry and prices while the first solve is in flight.
        t2 = EPOCH + timedelta(seconds=2)
        await runtime.push_telemetry(soc_frac=0.8, meter_w=900.0, temp_c=25.0, at=t2)
        await runtime.push_state_event_trigger(at=t2)

        release.set()
        await runtime.drain(timeout_s=5.0)

        # The second generation must be re-solved (not dropped), so the old
        # snapshot is suppressed rather than reused as authority.
        assert planner.call_count >= 2
        assert runtime.active_plan is not None
        # First intent of the accepted plan is still valid (not in the past)
        assert runtime.active_plan.intents[0].end > runtime.now

    @pytest.mark.anyio
    async def test_single_trigger_not_coalesced(self) -> None:
        runtime = await _make_runtime()
        await runtime.push_telemetry(soc_frac=0.5, meter_w=400.0, temp_c=25.0, at=EPOCH)
        prices = _prices(origin=EPOCH)
        await runtime.push_price_publication(
            prices.prices,
            interval_starts=prices.interval_starts,
            published_at=prices.published_at,
            valid_from=prices.valid_from,
            valid_to=prices.valid_to,
            at=EPOCH,
        )

        await runtime.push_scheduled_trigger(at=EPOCH)
        await runtime.drain(timeout_s=5.0)
        triggers = runtime.sink.records_of(ReplayRecordKind.TRIGGER_FIRED)
        assert len(triggers) == 1
        assert not triggers[0].details.get("coalesced", False)


# ---------------------------------------------------------------------------
# Event loop responsiveness
# ---------------------------------------------------------------------------


class TestEventLoopResponsiveness:
    """Event loop stays responsive while solver is busy."""

    @pytest.mark.anyio
    async def test_events_processed_during_solve(self) -> None:
        """Other events can be processed while a solve is in flight."""
        spec = _spec()
        release = threading.Event()
        started = threading.Event()
        planner = _SlowPlanner(spec, release_event=release, started_event=started)
        runtime = await _make_runtime(spec=spec, planner=planner, solve_timeout_s=30.0)
        await runtime.push_telemetry(soc_frac=0.5, meter_w=400.0, temp_c=25.0, at=EPOCH)
        prices = _prices(origin=EPOCH)
        await runtime.push_price_publication(
            prices.prices,
            interval_starts=prices.interval_starts,
            published_at=prices.published_at,
            valid_from=prices.valid_from,
            valid_to=prices.valid_to,
            at=EPOCH,
        )

        await runtime.push_scheduled_trigger(at=EPOCH)
        started.wait(timeout=2.0)

        # Event loop still responsive — push more events
        await runtime.push_telemetry(
            soc_frac=0.6, meter_w=500.0, temp_c=25.0, at=EPOCH + timedelta(seconds=2)
        )
        assert runtime._state.latest_telemetry is not None
        assert runtime._state.latest_telemetry.soc_frac == 0.6

        release.set()
        await runtime.drain(timeout_s=5.0)


# ---------------------------------------------------------------------------
# Sink properties and AST guard
# ---------------------------------------------------------------------------


class TestSinkProperties:
    """Recording sink has no device writes."""

    def test_sink_has_no_device_writes(self) -> None:
        sink = RecordingSink()
        assert not sink.has_device_writes

    def test_sink_records_are_immutable_copy(self) -> None:
        sink = RecordingSink()
        sink.record(ReplayRecordKind.RUNTIME_STARTED, at=EPOCH)
        records = sink.records
        records.clear()
        assert len(sink.records) == 1

    def test_sink_clear(self) -> None:
        sink = RecordingSink()
        sink.record(ReplayRecordKind.RUNTIME_STARTED, at=EPOCH)
        sink.record(ReplayRecordKind.RUNTIME_STOPPED, at=EPOCH)
        assert len(sink.records) == 2
        sink.clear()
        assert len(sink.records) == 0

    def test_sink_filter_by_kind(self) -> None:
        sink = RecordingSink()
        sink.record(ReplayRecordKind.RUNTIME_STARTED, at=EPOCH)
        sink.record(ReplayRecordKind.SOLVE_REQUESTED, at=EPOCH)
        sink.record(ReplayRecordKind.RUNTIME_STOPPED, at=EPOCH)
        started = sink.records_of(ReplayRecordKind.RUNTIME_STARTED)
        assert len(started) == 1

    def test_no_device_library_imports_ast_guard(self) -> None:
        """AST-level guard: replay_runtime module must not import device libraries."""
        source_path = Path(
            __import__("energy_baseline.replay_runtime", fromlist=["__file__"]).__file__
        )
        source = source_path.read_text(encoding="utf-8")
        tree = ast.parse(source)

        forbidden_modules = {
            "homeassistant",
            "home_assistant",
            "paho",
            "aiohttp",
            "requests",
            "httpx",
            "subprocess",
            "socket",
            "serial",
            "modbus",
            "mqtt",
            "websockets",
            "pyudev",
            "smbus2",
            "gpiozero",
        }

        imported: list[str] = []
        for node in ast.walk(tree):
            if isinstance(node, ast.Import):
                for alias in node.names:
                    imported.append(alias.name)
            elif isinstance(node, ast.ImportFrom):
                if node.level > 0:
                    # Relative import inside the energy_baseline package
                    imported.append("energy_baseline." + (node.module or ""))
                elif node.module:
                    imported.append(node.module)

        for name in imported:
            root = name.split(".")[0]
            for forbidden in sorted(forbidden_modules):
                assert root != forbidden and not name.startswith(forbidden + "."), (
                    f"replay_runtime.py imports {name!r} — device/network libraries "
                    "are forbidden in a replay-only, recording-only runtime"
                )

        # Every import must resolve to stdlib or the project's own modules.
        allowed_roots = {
            "asyncio",
            "enum",
            "math",
            "uuid",
            "collections",
            "concurrent",
            "dataclasses",
            "datetime",
            "typing",
            "numpy",
            "energy_baseline",
            "__future__",
        }
        for name in imported:
            root = name.split(".")[0]
            assert root in allowed_roots, (
                f"replay_runtime.py imports non-allowlisted module {name!r}"
            )

    def test_no_device_call_expressions_ast_guard(self) -> None:
        """No subprocess/socket/requests-style call expressions in the module."""
        source_path = Path(
            __import__("energy_baseline.replay_runtime", fromlist=["__file__"]).__file__
        )
        tree = ast.parse(source_path.read_text(encoding="utf-8"))

        forbidden_attr_roots = {"subprocess", "socket", "requests", "aiohttp", "httpx", "paho"}

        for node in ast.walk(tree):
            if isinstance(node, ast.Attribute):
                base = node
                parts: list[str] = []
                while isinstance(base, ast.Attribute):
                    parts.append(base.attr)
                    base = base.value
                if isinstance(base, ast.Name):
                    parts.append(base.id)
                    root = parts[-1]
                    assert root not in forbidden_attr_roots, (
                        f"replay_runtime.py calls into {root!r} — forbidden"
                    )

    def test_module_defines_no_writer_symbols(self) -> None:
        """The module exposes nothing that could send a command to a device."""
        import energy_baseline.replay_runtime as rr

        hints = ("write", "send", "command_", "actuat", "publish")
        writer_like = [name for name in dir(rr) if any(hint in name.lower() for hint in hints)]
        # CommandPolicy lives in dispatch.py, not here; nothing writer-shaped is exported.
        assert writer_like == [], f"unexpected writer-shaped exports: {writer_like}"

    def test_sink_record_carries_only_observation_data(self) -> None:
        """Sink records hold values only — no callables, no device handles."""
        sink = RecordingSink()
        sink.record(
            ReplayRecordKind.PLAN_ACCEPTED,
            at=EPOCH,
            plan_sequence=0,
            expires_at=EPOCH.isoformat(),
        )
        for rec in sink.records:
            for key, value in rec.details.items():
                assert not callable(value), f"record detail {key!r} is callable"


# ---------------------------------------------------------------------------
# Oracle planner adapter
# ---------------------------------------------------------------------------


class TestOraclePlannerAdapter:
    """Bridge to solve_oracle producing validated DispatchPlan."""

    def test_adapter_produces_valid_plan(self) -> None:
        spec = _spec()
        adapter = OraclePlannerAdapter(spec, horizon_slots=4, time_limit_s=10.0)
        starts = _starts(4)
        horizon = Horizon(
            interval_seconds=QUARTER,
            prices=[0.05, 0.15, 0.25, 0.10],
            load_kwh=[0.3, 0.3, 0.3, 0.3],
            usable=[True, True, True, True],
        )
        plan = adapter(spec, horizon, starts, EPOCH, EPOCH + timedelta(seconds=QUARTER), 0)

        assert isinstance(plan, DispatchPlan)
        assert plan.plan_sequence == 0
        assert plan.n == 4
        assert plan.content_hash != ""

    def test_adapter_with_correct_soc(self) -> None:
        """Oracle adapter uses the spec's initial SOC correctly."""
        spec = _spec(initial_soc_frac=0.8)
        adapter = OraclePlannerAdapter(spec, horizon_slots=4, time_limit_s=10.0)
        starts = _starts(4)
        horizon = Horizon(
            interval_seconds=QUARTER,
            prices=[0.05, 0.15, 0.25, 0.10],
            load_kwh=[0.3, 0.3, 0.3, 0.3],
            usable=[True, True, True, True],
        )
        plan = adapter(spec, horizon, starts, EPOCH, EPOCH + timedelta(seconds=QUARTER), 0)
        assert isinstance(plan, DispatchPlan)
        assert plan.n == 4


# ---------------------------------------------------------------------------
# Load forecast
# ---------------------------------------------------------------------------


class TestLoadForecast:
    """Explicit load forecast required; missing forecast refuses planning."""

    @pytest.mark.anyio
    async def test_constant_load_provider(self) -> None:
        provider = ConstantLoadProvider(0.5)
        starts = _starts(4)
        forecast = provider.forecast(starts, EPOCH)
        assert len(forecast) == 4
        assert all(f == 0.5 for f in forecast)

    @pytest.mark.anyio
    async def test_custom_forecast_provider(self) -> None:
        class CustomProvider:
            def forecast(self, interval_starts, at):
                return tuple(0.1 * (i + 1) for i in range(len(interval_starts)))

        spec = _spec()
        planner = _StubPlanner(spec)
        runtime = await _make_runtime(spec=spec, planner=planner, load_forecast=CustomProvider())
        await runtime.push_telemetry(soc_frac=0.5, meter_w=400.0, temp_c=25.0, at=EPOCH)
        prices = _prices(origin=EPOCH)
        await runtime.push_price_publication(
            prices.prices,
            interval_starts=prices.interval_starts,
            published_at=prices.published_at,
            valid_from=prices.valid_from,
            valid_to=prices.valid_to,
            at=EPOCH,
        )
        await runtime.push_scheduled_trigger(at=EPOCH)
        await runtime.drain(timeout_s=5.0)
        assert runtime.active_plan is not None

    @pytest.mark.anyio
    async def test_misaligned_forecast_refuses_planning(self) -> None:
        """A forecast of the wrong length refuses planning rather than inventing load."""

        class ShortProvider:
            def forecast(self, interval_starts, at):
                return (0.3,)  # always one entry — misaligned with the horizon

        spec = _spec()
        runtime = await _make_runtime(
            spec=spec,
            planner=_StubPlanner(spec),
            load_forecast=ShortProvider(),
            telemetry_freshness_s=1e9,
        )
        await runtime.push_telemetry(soc_frac=0.5, meter_w=400.0, temp_c=25.0, at=EPOCH)
        prices = _prices(n=8, origin=EPOCH)
        await runtime.push_price_publication(
            prices.prices,
            interval_starts=prices.interval_starts,
            published_at=prices.published_at,
            valid_from=prices.valid_from,
            valid_to=prices.valid_to,
            at=EPOCH,
        )
        await runtime.push_scheduled_trigger(at=EPOCH)
        await runtime.drain(timeout_s=5.0)

        assert runtime.active_plan is None
        # No plan existed to invalidate, so the refusal is recorded on the
        # failed-solve record with an accurate reason.
        failed = runtime.sink.records_of(ReplayRecordKind.SOLVE_FAILED)
        reasons = [r.details.get("reason") for r in failed]
        assert InvalidationReason.MISSING_FORECAST.value in reasons
        # The planner was never handed a bogus load number
        assert runtime.sink.records_of(ReplayRecordKind.SOLVE_COMPLETED) == []

    @pytest.mark.anyio
    async def test_forecast_provider_exception_refuses_planning(self) -> None:
        """A raising forecast provider refuses planning, not crashes the runtime."""

        class BrokenProvider:
            def forecast(self, interval_starts, at):
                raise RuntimeError("forecast service unavailable")

        spec = _spec()
        runtime = await _make_runtime(
            spec=spec,
            planner=_StubPlanner(spec),
            load_forecast=BrokenProvider(),
            telemetry_freshness_s=1e9,
        )
        await runtime.push_telemetry(soc_frac=0.5, meter_w=400.0, temp_c=25.0, at=EPOCH)
        prices = _prices(n=8, origin=EPOCH)
        await runtime.push_price_publication(
            prices.prices,
            interval_starts=prices.interval_starts,
            published_at=prices.published_at,
            valid_from=prices.valid_from,
            valid_to=prices.valid_to,
            at=EPOCH,
        )
        await runtime.push_scheduled_trigger(at=EPOCH)
        await runtime.drain(timeout_s=5.0)
        assert runtime.active_plan is None

    @pytest.mark.anyio
    async def test_horizon_load_is_forecast_not_meter(self) -> None:
        """The horizon load comes from the forecast, never from net grid import."""
        spec = _spec()
        planner = _StubPlanner(spec)
        runtime = await _make_runtime(
            spec=spec,
            planner=planner,
            load_forecast=ConstantLoadProvider(0.77),
            telemetry_freshness_s=1e9,
        )
        # A meter reading that would imply a very different load if inferred
        await runtime.push_telemetry(soc_frac=0.5, meter_w=50.0, temp_c=25.0, at=EPOCH)
        prices = _prices(n=8, origin=EPOCH)
        await runtime.push_price_publication(
            prices.prices,
            interval_starts=prices.interval_starts,
            published_at=prices.published_at,
            valid_from=prices.valid_from,
            valid_to=prices.valid_to,
            at=EPOCH,
        )
        await runtime.push_scheduled_trigger(at=EPOCH)
        await runtime.drain(timeout_s=5.0)

        horizon = planner.last_args["horizon"]
        assert np.allclose(np.asarray(horizon.load_kwh), 0.77), (
            "planner must receive the explicit forecast, not a value inferred "
            "from the 50 W meter reading"
        )

    @pytest.mark.anyio
    async def test_planner_gets_sliced_horizon_aligned_starts(self) -> None:
        """interval_starts passed to the planner match the sliced horizon exactly."""
        spec = _spec()
        planner = _StubPlanner(spec)
        runtime = await _make_runtime(
            spec=spec,
            planner=planner,
            telemetry_freshness_s=1e9,
        )
        await runtime.push_telemetry(soc_frac=0.5, meter_w=400.0, temp_c=25.0, at=EPOCH)
        prices = _prices(n=8, origin=EPOCH)
        await runtime.push_price_publication(
            prices.prices,
            interval_starts=prices.interval_starts,
            published_at=prices.published_at,
            valid_from=prices.valid_from,
            valid_to=prices.valid_to,
            at=EPOCH,
        )
        await runtime.push_scheduled_trigger(at=EPOCH)
        await runtime.drain(timeout_s=5.0)

        horizon = planner.last_args["horizon"]
        starts = planner.last_args["interval_starts"]
        # Same length, and every start aligns with the sliced horizon grid
        assert len(starts) == horizon.n
        step = timedelta(seconds=horizon.interval_seconds)
        for i, start in enumerate(starts):
            assert start >= EPOCH, "only future intervals may be planned"
            assert start == starts[0] + i * step, "planner starts must be contiguous"


# ---------------------------------------------------------------------------
# Deterministic replay
# ---------------------------------------------------------------------------


class TestDeterministicReplay:
    """Replay outputs are deterministic apart from execution timing."""

    @pytest.mark.anyio
    async def test_same_inputs_same_plan(self) -> None:
        spec = _spec()

        async def run() -> list[ReplayRecord]:
            planner = _StubPlanner(spec)
            sink = RecordingSink()
            runtime = ReplayRuntime(spec, planner, ConstantLoadProvider(0.3), sink)
            await runtime.start(at=EPOCH)
            await _feed_and_solve(runtime)
            return sink.records

        records1 = await run()
        records2 = await run()

        assert len(records1) == len(records2)

        hashes1 = [
            r.details.get("content_hash")
            for r in records1
            if r.kind is ReplayRecordKind.SOLVE_COMPLETED
        ]
        hashes2 = [
            r.details.get("content_hash")
            for r in records2
            if r.kind is ReplayRecordKind.SOLVE_COMPLETED
        ]
        assert hashes1 == hashes2


# ---------------------------------------------------------------------------
# Integration: full replay scenario
# ---------------------------------------------------------------------------


class TestIntegration:
    """End-to-end replay scenarios."""

    @pytest.mark.anyio
    async def test_full_replay_produces_plan(self) -> None:
        spec = _spec()
        planner = _StubPlanner(spec)
        runtime = await _make_runtime(spec=spec, planner=planner)

        await _feed_and_solve(runtime)

        assert runtime.active_plan is not None
        plan = runtime.active_plan
        assert plan.plan_sequence == 0
        assert plan.n > 0
        assert plan.content_hash != ""

        records = runtime.sink.records
        kinds = [r.kind for r in records]
        assert ReplayRecordKind.RUNTIME_STARTED in kinds
        assert ReplayRecordKind.SOLVE_REQUESTED in kinds
        assert ReplayRecordKind.SOLVE_COMPLETED in kinds
        assert ReplayRecordKind.PLAN_ACCEPTED in kinds

    @pytest.mark.anyio
    async def test_disconnect_reconnect_cycle(self) -> None:
        spec = _spec()
        planner = _StubPlanner(spec)
        runtime = await _make_runtime(spec=spec, planner=planner)

        await _feed_and_solve(runtime)
        assert runtime.active_plan is not None

        await runtime.push_disconnect(at=EPOCH + timedelta(minutes=1))
        assert runtime.active_plan is None
        assert not runtime.is_connected

        new_time = EPOCH + timedelta(minutes=2)
        snapshot = _state_snapshot(soc_frac=0.6, at=new_time)
        await runtime.push_reconnect(at=new_time, snapshot=snapshot)
        assert runtime.is_connected

        await runtime.push_scheduled_trigger(at=new_time)
        await runtime.drain(timeout_s=5.0)
        assert runtime.active_plan is not None

    @pytest.mark.anyio
    async def test_oracle_adapter_integration(self) -> None:
        """Full cycle with the real oracle adapter."""
        spec = _spec()
        adapter = OraclePlannerAdapter(spec, horizon_slots=4, time_limit_s=10.0)
        sink = RecordingSink()
        runtime = ReplayRuntime(
            spec,
            adapter,
            ConstantLoadProvider(0.3),
            sink,
            telemetry_freshness_s=600.0,
            price_freshness_s=7200.0,
            solve_timeout_s=30.0,
            plan_ttl_s=3600.0,
        )
        await runtime.start(at=EPOCH)

        await runtime.push_telemetry(soc_frac=0.5, meter_w=400.0, temp_c=25.0, at=EPOCH)

        starts = _starts(4)
        prices = PricePublication(
            prices=(0.05, 0.15, 0.25, 0.10),
            interval_starts=tuple(starts),
            published_at=EPOCH,
            valid_from=starts[0],
            valid_to=starts[-1] + timedelta(seconds=QUARTER),
        )
        await runtime.push_price_publication(
            prices.prices,
            interval_starts=prices.interval_starts,
            published_at=prices.published_at,
            valid_from=prices.valid_from,
            valid_to=prices.valid_to,
            at=EPOCH,
        )

        await runtime.push_scheduled_trigger(at=EPOCH)
        await runtime.drain(timeout_s=10.0)

        assert runtime.active_plan is not None
        plan = runtime.active_plan
        assert plan.n == 4
        assert plan.content_hash != ""


# ---------------------------------------------------------------------------
# Edge cases
# ---------------------------------------------------------------------------


class TestEdgeCases:
    """Edge cases and boundary conditions."""

    @pytest.mark.anyio
    async def test_zero_soc_telemetry(self) -> None:
        runtime = await _make_runtime()
        await runtime.push_telemetry(soc_frac=0.0, meter_w=400.0, temp_c=25.0, at=EPOCH)
        assert runtime._state.latest_telemetry.soc_frac == 0.0

    @pytest.mark.anyio
    async def test_full_soc_telemetry(self) -> None:
        runtime = await _make_runtime()
        await runtime.push_telemetry(soc_frac=1.0, meter_w=400.0, temp_c=25.0, at=EPOCH)
        assert runtime._state.latest_telemetry.soc_frac == 1.0

    @pytest.mark.anyio
    async def test_negative_meter_power(self) -> None:
        runtime = await _make_runtime()
        await runtime.push_telemetry(soc_frac=0.5, meter_w=-200.0, temp_c=25.0, at=EPOCH)
        assert runtime._state.latest_telemetry.meter_w == -200.0

    @pytest.mark.anyio
    async def test_zero_price(self) -> None:
        runtime = await _make_runtime()
        starts = _starts(4)
        await runtime.push_price_publication(
            [0.0, 0.0, 0.0, 0.0],
            interval_starts=starts,
            published_at=EPOCH,
            valid_from=starts[0],
            valid_to=starts[-1] + timedelta(seconds=QUARTER),
            at=EPOCH,
        )
        assert runtime._state.latest_prices is not None

    @pytest.mark.anyio
    async def test_negative_price(self) -> None:
        runtime = await _make_runtime()
        starts = _starts(4)
        await runtime.push_price_publication(
            [-0.05, 0.10, -0.02, 0.15],
            interval_starts=starts,
            published_at=EPOCH,
            valid_from=starts[0],
            valid_to=starts[-1] + timedelta(seconds=QUARTER),
            at=EPOCH,
        )
        assert runtime._state.latest_prices is not None
        assert runtime._state.latest_prices.prices[0] == -0.05


# ---------------------------------------------------------------------------
# Revalidation at acceptance
# ---------------------------------------------------------------------------


class TestRevalidationAtAcceptance:
    """Freshness re-validated at plan acceptance time."""

    @pytest.mark.anyio
    async def test_stale_telemetry_at_acceptance_rejects_plan(self) -> None:
        """If telemetry goes stale while solve is in flight, plan is rejected."""
        spec = _spec()
        release = threading.Event()
        started = threading.Event()
        planner = _SlowPlanner(spec, release_event=release, started_event=started)
        runtime = await _make_runtime(
            spec=spec,
            planner=planner,
            solve_timeout_s=30.0,
            telemetry_freshness_s=5.0,
        )
        await runtime.push_telemetry(soc_frac=0.5, meter_w=400.0, temp_c=25.0, at=EPOCH)
        prices = _prices(origin=EPOCH)
        await runtime.push_price_publication(
            prices.prices,
            interval_starts=prices.interval_starts,
            published_at=prices.published_at,
            valid_from=prices.valid_from,
            valid_to=prices.valid_to,
            at=EPOCH,
        )

        await runtime.push_scheduled_trigger(at=EPOCH)
        started.wait(timeout=2.0)

        # Advance time past freshness threshold while solve is in flight
        await runtime.advance_to(EPOCH + timedelta(seconds=10))

        # Release worker — solve completes but telemetry is now stale
        release.set()
        await runtime.drain(timeout_s=5.0)

        # Plan should be rejected due to stale telemetry at acceptance
        assert runtime.active_plan is None
        failed = runtime.sink.records_of(ReplayRecordKind.SOLVE_FAILED)
        stale_msgs = [f for f in failed if "stale" in str(f.details.get("error", ""))]
        assert len(stale_msgs) >= 1


# ---------------------------------------------------------------------------
# Superseded-input fence at acceptance
# ---------------------------------------------------------------------------


class TestSupersededInputFence:
    """HIGH #2: a plan solved on inputs superseded mid-solve must not install."""

    @pytest.mark.anyio
    async def test_newer_telemetry_mid_solve_is_not_accepted(self) -> None:
        """Newer SOC arriving during a solve fences the stale plan and replans."""
        spec = _spec()
        planner = _ConcurrencyProbePlanner(spec)
        runtime = await _make_runtime(
            spec=spec,
            planner=planner,
            solve_timeout_s=30.0,
            telemetry_freshness_s=1e9,
        )
        await _feed_inputs(runtime, EPOCH, soc_frac=0.5)
        await runtime.push_scheduled_trigger(at=EPOCH)
        assert planner.wait_for_calls(1)

        requested = runtime.sink.records_of(ReplayRecordKind.SOLVE_REQUESTED)
        gen1 = requested[0].details["generation_id"]
        assert requested[0].details["soc_frac"] == 0.5

        # A *newer* reading arrives mid-solve and queues no trigger of its own:
        # the solve in flight is now computing against a superseded snapshot.
        await runtime.push_telemetry(
            soc_frac=0.9, meter_w=100.0, temp_c=25.0, at=EPOCH + timedelta(seconds=1)
        )
        assert runtime._state.latest_telemetry.soc_frac == 0.9

        planner.release_all()
        await runtime.drain(timeout_s=10.0)

        fenced = [
            r
            for r in runtime.sink.records_of(ReplayRecordKind.SOLVE_FAILED)
            if r.details.get("stale_inputs")
        ]
        assert len(fenced) == 1, "the superseded plan must be fenced exactly once"
        assert fenced[0].details["generation_id"] == gen1
        assert "telemetry=changed" in fenced[0].details["error"]

        accepted = runtime.sink.records_of(ReplayRecordKind.PLAN_ACCEPTED)
        assert all(r.details["generation_id"] != gen1 for r in accepted), (
            "a plan solved on superseded telemetry must never be installed"
        )
        # The fence queues a re-solve, and it runs against the authoritative SOC.
        assert planner.observed_socs == [0.5, 0.9]
        assert runtime.active_plan is not None
        assert len(accepted) == 1
        assert accepted[0].details["generation_id"] != gen1

    @pytest.mark.anyio
    async def test_newer_prices_mid_solve_are_not_accepted(self) -> None:
        """A newer price curve arriving during a solve fences the stale plan."""
        spec = _spec()
        planner = _ConcurrencyProbePlanner(spec)
        runtime = await _make_runtime(
            spec=spec,
            planner=planner,
            solve_timeout_s=30.0,
            telemetry_freshness_s=1e9,
        )
        await _feed_inputs(runtime, EPOCH, soc_frac=0.5)
        await runtime.push_scheduled_trigger(at=EPOCH)
        assert planner.wait_for_calls(1)

        gen1 = runtime.sink.records_of(ReplayRecordKind.SOLVE_REQUESTED)[0].details["generation_id"]

        # A replacement publication for the same window arrives mid-solve.
        revised = PricePublication(
            prices=tuple([0.99] * 96),
            interval_starts=tuple(_starts(96)),
            published_at=EPOCH,
            valid_from=EPOCH,
            valid_to=EPOCH + timedelta(seconds=900 * 96),
            interval_seconds=QUARTER,
        )
        await runtime.push_price_publication(
            revised.prices,
            interval_starts=revised.interval_starts,
            published_at=revised.published_at,
            valid_from=revised.valid_from,
            valid_to=revised.valid_to,
            at=EPOCH + timedelta(seconds=1),
        )

        planner.release_all()
        await runtime.drain(timeout_s=10.0)

        fenced = [
            r
            for r in runtime.sink.records_of(ReplayRecordKind.SOLVE_FAILED)
            if r.details.get("stale_inputs")
        ]
        assert len(fenced) == 1
        assert fenced[0].details["generation_id"] == gen1
        assert "prices=changed" in fenced[0].details["error"]
        accepted = runtime.sink.records_of(ReplayRecordKind.PLAN_ACCEPTED)
        assert all(r.details["generation_id"] != gen1 for r in accepted)
        # The re-solve really received the revised curve.
        assert planner.observed_price_head == [0.15, 0.99]

    @pytest.mark.anyio
    async def test_unchanged_inputs_are_accepted(self) -> None:
        """Boundary: with no superseded input the fence must not fire."""
        runtime = await _make_runtime(telemetry_freshness_s=1e9)
        await _feed_and_solve(runtime, EPOCH)
        assert runtime.active_plan is not None
        assert runtime.sink.records_of(ReplayRecordKind.PLAN_ACCEPTED)
        assert not [
            r
            for r in runtime.sink.records_of(ReplayRecordKind.SOLVE_FAILED)
            if r.details.get("stale_inputs")
        ]


# ---------------------------------------------------------------------------
# Anchor interval freshness at acceptance
# ---------------------------------------------------------------------------


class TestAnchorIntervalFreshness:
    """HIGH #3: an already-started anchor interval must not be installed."""

    @pytest.mark.anyio
    async def test_partially_elapsed_anchor_interval_is_rejected(self) -> None:
        """Time advancing into the anchor interval fences the plan and replans."""
        spec = _spec()
        planner = _ConcurrencyProbePlanner(spec)
        runtime = await _make_runtime(
            spec=spec,
            planner=planner,
            solve_timeout_s=30.0,
            telemetry_freshness_s=1e9,
            price_freshness_s=1e9,
            plan_ttl_s=3600.0,
        )
        await _feed_inputs(runtime, EPOCH, soc_frac=0.5)
        await runtime.push_scheduled_trigger(at=EPOCH)
        assert planner.wait_for_calls(1)

        gen1 = runtime.sink.records_of(ReplayRecordKind.SOLVE_REQUESTED)[0].details["generation_id"]

        # The solve was requested at 12:00:00, so its anchor is [12:00, 12:15).
        # Simulated time now moves *inside* that interval: half of it is gone,
        # yet the old check (``end <= now``) still waved it through.
        halfway = EPOCH + timedelta(seconds=450)
        await runtime.advance_to(halfway)

        planner.release_all()
        await runtime.drain(timeout_s=10.0)

        rejected = [
            r
            for r in runtime.sink.records_of(ReplayRecordKind.SOLVE_FAILED)
            if r.details.get("partially_elapsed")
        ]
        assert len(rejected) == 1, "the half-elapsed anchor must be refused once"
        assert rejected[0].details["generation_id"] == gen1
        assert rejected[0].details["first_start"] == EPOCH.isoformat()
        assert rejected[0].details["acceptance_at"] == halfway.isoformat()

        accepted = runtime.sink.records_of(ReplayRecordKind.PLAN_ACCEPTED)
        assert all(r.details["generation_id"] != gen1 for r in accepted)
        # The queued re-solve lands, anchored on the next whole interval.
        assert runtime.active_plan is not None
        assert runtime.active_plan.intents[0].start == EPOCH + timedelta(seconds=900)
        assert runtime.active_plan.intents[0].start >= runtime.now

    @pytest.mark.anyio
    async def test_anchor_starting_exactly_at_now_is_accepted(self) -> None:
        """Boundary: ``start == now`` is a whole interval ahead and must land."""
        runtime = await _make_runtime(telemetry_freshness_s=1e9)
        await _feed_and_solve(runtime, EPOCH)
        assert runtime.active_plan is not None
        assert runtime.active_plan.intents[0].start == runtime.now
        assert not [
            r
            for r in runtime.sink.records_of(ReplayRecordKind.SOLVE_FAILED)
            if r.details.get("partially_elapsed")
        ]

    @pytest.mark.anyio
    async def test_fully_elapsed_anchor_interval_is_rejected(self) -> None:
        """The stricter check still refuses an anchor that ended outright."""
        spec = _spec()
        planner = _ConcurrencyProbePlanner(spec)
        runtime = await _make_runtime(
            spec=spec,
            planner=planner,
            solve_timeout_s=30.0,
            telemetry_freshness_s=1e9,
            price_freshness_s=1e9,
            plan_ttl_s=86400.0,
        )
        await _feed_inputs(runtime, EPOCH, soc_frac=0.5)
        await runtime.push_scheduled_trigger(at=EPOCH)
        assert planner.wait_for_calls(1)

        # Past the end of the anchor interval, but still a future interval left.
        await runtime.advance_to(EPOCH + timedelta(seconds=901))

        planner.release_all()
        await runtime.drain(timeout_s=10.0)

        rejected = [
            r
            for r in runtime.sink.records_of(ReplayRecordKind.SOLVE_FAILED)
            if r.details.get("partially_elapsed")
        ]
        assert len(rejected) == 1
        assert runtime.active_plan is not None
        assert runtime.active_plan.intents[0].start >= runtime.now
