#!/usr/bin/env python3
"""Standalone demo: replay a synthetic energy baseline with the replay runtime.

Demonstrates the replay runtime without modifying any existing files (no
``cli.py`` edits — that module is owned by another agent).  It exercises:

1. A battery spec, the real oracle planner bridge (``solve_oracle`` +
   ``intents_from_schedule``), and an **explicit synthetic load forecast**
   (the runtime never infers gross load from net grid import).
2. Telemetry, price publications with publication metadata, and triggers.
3. Plan expiry, telemetry/price staleness.
4. Disconnect/reconnect and both restart paths: a restart clears observations
   (and requires a fresh complete snapshot) but never the plan sequence, which is
   monotonic across ``push_restart`` *and* ``start()``.
5. Bounded worker concurrency: a slow solve shows that at most one real
   worker runs, and a late result is fenced by generation identity.
6. Restart through ``start()`` over a *stuck* worker: worker occupancy is not
   released and the executor is not replaced, so the max-one-worker bound and
   the planner thread count survive a restart.
7. The two acceptance fences: a plan solved on inputs that were superseded
   mid-solve, and a plan whose anchor interval has already begun, are both
   refused — and a re-solve against the authoritative inputs lands instead.
8. Collection of records from the recording-only sink.

Usage::

    python -m energy_baseline.hack.replay_runtime_demo

Dependencies: nothing beyond the project's existing dependencies.  No network,
no writer, no actuation, no deployment, no wall-clock reads.
"""

from __future__ import annotations

import asyncio
import threading
from collections.abc import Sequence
from datetime import UTC, datetime, timedelta

from energy_baseline.battery import BatterySpec
from energy_baseline.dispatch import DispatchPlan
from energy_baseline.oracle import OracleError
from energy_baseline.replay_runtime import (
    ConstantLoadProvider,
    OraclePlannerAdapter,
    PricePublication,
    RecordingSink,
    ReplayRecordKind,
    ReplayRuntime,
    StateSnapshot,
    TelemetrySnapshot,
)

QUARTER = 900.0
EPOCH = datetime(2026, 6, 15, 12, 0, 0, tzinfo=UTC)


def _spec() -> BatterySpec:
    return BatterySpec(
        capacity_kwh=5.12,
        power_kw=2.5,
        charge_efficiency=0.9,
        discharge_efficiency=0.9,
        soc_min_frac=0.1,
        soc_max_frac=0.95,
        initial_soc_frac=0.5,
    )


def _starts(n: int, origin: datetime = EPOCH) -> list[datetime]:
    return [origin + timedelta(seconds=QUARTER * i) for i in range(n)]


def _price_publication(n: int, origin: datetime) -> PricePublication:
    """A varied price curve so the oracle actually finds arbitrage.

    Cheap overnight, expensive morning/evening peaks.  The default demo window
    is shifted onto a cheap-then-expensive sequence rather than a flat price,
    which would correctly produce an all-idle plan.
    """
    starts = _starts(n, origin)
    # Explicit day-ahead shape: cheap hours first, expensive hours later.
    shape = [0.05, 0.04, 0.06, 0.10, 0.18, 0.26, 0.24, 0.14]
    prices = [shape[i % len(shape)] for i in range(n)]
    return PricePublication(
        prices=tuple(prices),
        interval_starts=tuple(starts),
        published_at=origin,
        valid_from=starts[0],
        valid_to=starts[-1] + timedelta(seconds=QUARTER),
        interval_seconds=QUARTER,
    )


class SyntheticDayProfileForecast:
    """Explicit synthetic gross-load forecast (kWh per interval).

    Deliberately supplied by the caller: the runtime refuses to plan without a
    forecast, and never derives gross load from a net meter reading.
    """

    def __init__(self, base_kwh: float = 0.30, peak_kwh: float = 0.90) -> None:
        self._base = base_kwh
        self._peak = peak_kwh

    def forecast(self, interval_starts: Sequence[datetime], at: datetime) -> tuple[float, ...]:
        out: list[float] = []
        for start in interval_starts:
            hour = start.hour + start.minute / 60.0
            # Morning and evening peaks, flat overnight base.
            morning = 1.0 if 7 <= hour < 10 else 0.0
            evening = 1.0 if 18 <= hour < 22 else 0.0
            factor = max(morning, evening)
            out.append(self._base + factor * (self._peak - self._base))
        return tuple(out)


class _BlockingPlanner:
    """A planner that blocks until released — used to show worker fencing.

    It also measures the peak number of *simultaneously executing* calls, which
    is the direct evidence for the runtime's max-one-worker bound.
    """

    def __init__(self, spec: BatterySpec, release: threading.Event) -> None:
        self._spec = spec
        self._release = release
        self._lock = threading.Lock()
        self._inflight = 0
        self.call_count = 0
        self.peak_concurrent = 0
        self.entered = threading.Event()

    def __call__(
        self,
        spec: BatterySpec,
        horizon,
        interval_starts: Sequence[datetime],
        generated_at: datetime,
        expires_at: datetime,
        plan_sequence: int,
    ) -> DispatchPlan:
        with self._lock:
            self.call_count += 1
            self._inflight += 1
            self.peak_concurrent = max(self.peak_concurrent, self._inflight)
        self.entered.set()
        try:
            # Block well past the runtime's solve timeout, then report a late failure.
            self._release.wait(timeout=30.0)
        finally:
            with self._lock:
                self._inflight -= 1
        raise OracleError("blocking planner: deliberately produced after timeout")


class _GatedPlanner:
    """Wraps a real planner so a solve stops at a caller-owned gate.

    The gate lets the demo move simulated time or deliver newer inputs *while* a
    solve is genuinely in flight — the only way to reach the acceptance fences.
    """

    def __init__(self, inner: OraclePlannerAdapter, gate: threading.Event) -> None:
        self._inner = inner
        self._gate = gate
        self.call_count = 0
        self.entered = threading.Event()

    def __call__(self, *args, **kwargs) -> DispatchPlan:
        self.call_count += 1
        self.entered.set()
        self._gate.wait(timeout=30.0)
        return self._inner(*args, **kwargs)


def _print_plan(plan: DispatchPlan | None) -> None:
    if plan is None:
        print("  No active plan.")
        return
    print(f"  Plan #{plan.plan_sequence} — {plan.n} intents, hash={plan.content_hash[:12]}...")
    print(
        f"    Horizon: {plan.horizon_start.strftime('%H:%M')} -> "
        f"{plan.horizon_end.strftime('%H:%M')}"
    )
    print(f"    Expires: {plan.expires_at.strftime('%H:%M:%S')}")
    for intent in plan.intents:
        power = f"{intent.target_power_w:6.0f} W" if intent.target_power_w else "     idle"
        print(f"    [{intent.start.strftime('%H:%M')}] {intent.mode.value:9s} {power}")


def _section(title: str) -> None:
    print(f"\n{'=' * 66}\n  {title}\n{'=' * 66}")


async def main() -> None:
    _section("Energy Baseline Replay Runtime Demo")
    print("  replay-only | recording-only sink | caller-supplied time")
    print("  no writer, no actuation, no network, no wall-clock reads")

    spec = _spec()
    sink = RecordingSink()

    runtime = ReplayRuntime(
        battery_spec=spec,
        planner=OraclePlannerAdapter(spec, horizon_slots=8, time_limit_s=10.0),
        load_forecast=SyntheticDayProfileForecast(),
        sink=sink,
        telemetry_freshness_s=60.0,
        price_freshness_s=7200.0,
        solve_timeout_s=30.0,
        coalesce_window_s=5.0,
        plan_ttl_s=3600.0,
    )

    # 1. Start
    await runtime.start(at=EPOCH)
    print(f"\n[START] {EPOCH.strftime('%H:%M:%S')}  connected={runtime.is_connected}")

    # 2. Telemetry
    await runtime.push_telemetry(soc_frac=0.5, meter_w=400.0, temp_c=25.0, at=EPOCH)
    print("[TELEMETRY] SOC=50%  meter=400 W  temp=25 C")

    # 3. Prices (with publication metadata)
    pub = _price_publication(8, EPOCH)
    await runtime.push_price_publication(
        pub.prices,
        interval_starts=pub.interval_starts,
        published_at=pub.published_at,
        valid_from=pub.valid_from,
        valid_to=pub.valid_to,
        at=EPOCH,
    )
    print(
        f"[PRICES] {len(pub.prices)} intervals  "
        f"{min(pub.prices):.2f}-{max(pub.prices):.2f} EUR/kWh  "
        f"published_at={pub.published_at.strftime('%H:%M')}"
    )
    print("[FORECAST] explicit synthetic day profile (never inferred from meter)")

    # 4. Scheduled trigger -> real oracle solve
    await runtime.push_scheduled_trigger(at=EPOCH)
    await runtime.drain(timeout_s=30.0)
    print("\n[AFTER SOLVE]")
    _print_plan(runtime.active_plan)
    print(f"  next plan_sequence={runtime.plan_sequence}")

    # 5. Advance past telemetry freshness -> stale
    stale_t = EPOCH + timedelta(seconds=61)
    await runtime.advance_to(stale_t)
    print(f"\n[STALE TELEMETRY] advanced to {stale_t.strftime('%H:%M:%S')} (>60 s old)")
    _print_plan(runtime.active_plan)

    # 6. Fresh telemetry + re-solve
    await runtime.push_telemetry(soc_frac=0.6, meter_w=500.0, temp_c=26.0, at=stale_t)
    await runtime.push_scheduled_trigger(at=stale_t)
    await runtime.drain(timeout_s=30.0)
    print("\n[RE-SOLVE with fresh telemetry]")
    _print_plan(runtime.active_plan)

    # 7. Disconnect -> plan invalidated; reconnect needs a fresh full snapshot
    disc_t = stale_t + timedelta(seconds=10)
    await runtime.push_disconnect(at=disc_t)
    print(f"\n[DISCONNECT] {disc_t.strftime('%H:%M:%S')}  connected={runtime.is_connected}")
    _print_plan(runtime.active_plan)

    rec_t = disc_t + timedelta(seconds=5)
    snapshot = StateSnapshot(
        telemetry=TelemetrySnapshot(soc_frac=0.7, meter_w=300.0, temp_c=26.0, at=rec_t),
        prices=pub,
        at=rec_t,
    )
    await runtime.push_reconnect(at=rec_t, snapshot=snapshot)
    print(f"[RECONNECT] {rec_t.strftime('%H:%M:%S')} with fresh full state snapshot")

    await runtime.push_scheduled_trigger(at=rec_t)
    await runtime.drain(timeout_s=30.0)
    print("\n[POST-RECONNECT SOLVE]")
    _print_plan(runtime.active_plan)

    # 8. Restart clears observations but never the plan sequence (principle 10)
    restart_t = rec_t + timedelta(seconds=30)
    await runtime.push_restart(at=restart_t)
    print(f"\n[RESTART via event] {restart_t.strftime('%H:%M:%S')}")
    print(
        f"  observations cleared: telemetry={runtime._state.latest_telemetry} "
        f"prices={runtime._state.latest_prices}"
    )
    _print_plan(runtime.active_plan)
    print(f"  plan_sequence preserved (monotonic): {runtime.plan_sequence}")

    await runtime.push_telemetry(soc_frac=0.65, meter_w=350.0, temp_c=26.0, at=restart_t)
    await runtime.push_price_publication(
        pub.prices,
        interval_starts=pub.interval_starts,
        published_at=pub.published_at,
        valid_from=pub.valid_from,
        valid_to=pub.valid_to,
        at=restart_t,
    )
    await runtime.push_scheduled_trigger(at=restart_t)
    await runtime.drain(timeout_s=30.0)
    print("\n[POST-RESTART SOLVE — event path]")
    _print_plan(runtime.active_plan)

    # The other restart path replaces the whole state object, so it must carry the
    # counter explicitly: numbered onward, never back to 0.
    hard_restart_t = restart_t + timedelta(seconds=30)
    before_hard_restart = runtime.plan_sequence
    await runtime.start(at=hard_restart_t)
    print(f"\n[RESTART via start()] {hard_restart_t.strftime('%H:%M:%S')}")
    print(
        f"  observations cleared: telemetry={runtime._state.latest_telemetry} "
        f"prices={runtime._state.latest_prices}"
    )
    print(
        f"  plan_sequence carried, not reset: {runtime.plan_sequence} (was {before_hard_restart})"
    )
    assert runtime.plan_sequence == before_hard_restart, "start() reset the plan sequence"

    await runtime.push_telemetry(soc_frac=0.7, meter_w=300.0, temp_c=26.0, at=hard_restart_t)
    await runtime.push_price_publication(
        pub.prices,
        interval_starts=pub.interval_starts,
        published_at=pub.published_at,
        valid_from=pub.valid_from,
        valid_to=pub.valid_to,
        at=hard_restart_t,
    )
    await runtime.push_scheduled_trigger(at=hard_restart_t)
    await runtime.drain(timeout_s=30.0)
    print("\n[POST-RESTART SOLVE — start() path]")
    _print_plan(runtime.active_plan)
    assert runtime.active_plan is not None
    assert runtime.active_plan.plan_sequence == before_hard_restart, (
        "the post-start() generation must be numbered onward"
    )

    # 9. Worker fencing: a timed-out worker is not replaced by a second one
    _section("WORKER FENCING (max one real worker, late result discarded)")
    release = threading.Event()
    blocking = _BlockingPlanner(spec, release)
    fence_sink = RecordingSink()
    fence_runtime = ReplayRuntime(
        battery_spec=spec,
        planner=blocking,
        load_forecast=ConstantLoadProvider(0.3),
        sink=fence_sink,
        solve_timeout_s=0.2,
        telemetry_freshness_s=1e9,
    )
    await fence_runtime.start(at=EPOCH)
    await fence_runtime.push_telemetry(soc_frac=0.5, meter_w=400.0, temp_c=25.0, at=EPOCH)
    await fence_runtime.push_price_publication(
        pub.prices,
        interval_starts=pub.interval_starts,
        published_at=pub.published_at,
        valid_from=pub.valid_from,
        valid_to=pub.valid_to,
        at=EPOCH,
    )

    await fence_runtime.push_scheduled_trigger(at=EPOCH)
    blocking.entered.wait(timeout=2.0)
    print(f"  worker entered  (planner calls={blocking.call_count})")

    # Let the runtime give up on the solve (timeout) while the thread keeps running.
    # drain() refuses to release: it only unblocks when the real worker ends, so
    # this returns via its own timeout — which itself proves the worker is busy.
    await fence_runtime.drain(timeout_s=1.0)
    timeouts = fence_sink.records_of(ReplayRecordKind.SOLVE_TIMEOUT)
    print(
        f"  solve timed out   (timeouts={len(timeouts)}); drain() did not release, "
        f"proving the thread is still running"
    )

    # Trigger again: the runtime is not awaiting anything, yet the worker is
    # genuinely occupied -> this must record saturation, not launch worker #2.
    await fence_runtime.push_scheduled_trigger(at=EPOCH + timedelta(seconds=1))
    sats = fence_sink.records_of(ReplayRecordKind.WORKER_SATURATION)
    print(
        f"  second trigger    -> planner calls={blocking.call_count} (must stay 1), "
        f"saturation records={len(sats)}"
    )
    assert blocking.call_count == 1, "a second real worker must not start"
    assert len(sats) == 1, "an occupied worker must record saturation"
    print(f"  active plan       : {fence_runtime.active_plan}")

    # 10. Restart *through start()* over the stuck worker.  This path used to
    # replace the executor and clear worker occupancy after a bounded wait, so
    # the abandoned thread ran beside a freshly launched one.
    _section("RESTART VIA start() OVER A STUCK WORKER (occupancy survives)")
    restart_t = EPOCH + timedelta(minutes=5)
    await fence_runtime.start(at=restart_t)
    await fence_runtime.push_telemetry(soc_frac=0.6, meter_w=400.0, temp_c=25.0, at=restart_t)
    await fence_runtime.push_price_publication(
        pub.prices,
        interval_starts=pub.interval_starts,
        published_at=pub.published_at,
        valid_from=pub.valid_from,
        valid_to=pub.valid_to,
        at=restart_t,
    )
    await fence_runtime.push_scheduled_trigger(at=restart_t)
    print(
        f"  restarted at {restart_t.strftime('%H:%M:%S')} with a fresh snapshot + trigger:"
        f" planner calls={blocking.call_count} (must stay 1),"
        f" peak_concurrent={blocking.peak_concurrent}"
    )
    assert blocking.call_count == 1, "restart must not launch a second real worker"
    assert blocking.peak_concurrent == 1, "two planner threads ran at once"
    assert fence_runtime.is_started and fence_runtime.is_connected

    release.set()
    await fence_runtime.drain(timeout_s=30.0)
    print(
        f"  after release     -> planner calls={blocking.call_count}, "
        f"peak_concurrent={blocking.peak_concurrent} (never exceeded one)"
    )
    assert blocking.peak_concurrent == 1

    # 11. Acceptance fences: superseded inputs and a partially elapsed anchor.
    _section("ACCEPTANCE FENCES (stale inputs, half-elapsed anchor interval)")
    for label, mutate in (
        ("newer telemetry arrives mid-solve", "telemetry"),
        ("clock advances into the anchor interval", "anchor"),
    ):
        gate = threading.Event()
        gated = _GatedPlanner(OraclePlannerAdapter(spec, horizon_slots=4, time_limit_s=10.0), gate)
        fence_sink2 = RecordingSink()
        gated_runtime = ReplayRuntime(
            battery_spec=spec,
            planner=gated,
            load_forecast=ConstantLoadProvider(0.3),
            sink=fence_sink2,
            telemetry_freshness_s=1e9,
            price_freshness_s=1e9,
            solve_timeout_s=30.0,
            plan_ttl_s=3600.0,
        )
        await gated_runtime.start(at=EPOCH)
        await gated_runtime.push_telemetry(soc_frac=0.5, meter_w=400.0, temp_c=25.0, at=EPOCH)
        await gated_runtime.push_price_publication(
            pub.prices,
            interval_starts=pub.interval_starts,
            published_at=pub.published_at,
            valid_from=pub.valid_from,
            valid_to=pub.valid_to,
            at=EPOCH,
        )
        await gated_runtime.push_scheduled_trigger(at=EPOCH)
        assert gated.entered.wait(timeout=5.0), "worker never reached the gate"

        if mutate == "telemetry":
            await gated_runtime.push_telemetry(
                soc_frac=0.8, meter_w=200.0, temp_c=25.0, at=EPOCH + timedelta(seconds=1)
            )
        else:
            await gated_runtime.advance_to(EPOCH + timedelta(seconds=450))

        gate.set()
        await gated_runtime.drain(timeout_s=30.0)
        failed = fence_sink2.records_of(ReplayRecordKind.SOLVE_FAILED)
        fenced = [
            r for r in failed if r.details.get("stale_inputs") or r.details.get("partially_elapsed")
        ]
        reasons = [
            "superseded inputs" if r.details.get("stale_inputs") else "elapsed anchor interval"
            for r in fenced
        ]
        accepted = fence_sink2.records_of(ReplayRecordKind.PLAN_ACCEPTED)
        landed = gated_runtime.active_plan
        print(f"  {label}:")
        print(f"    fenced {len(fenced)} generation(s): {', '.join(reasons)}")
        print(
            f"    solves={gated.call_count}, accepted={len(accepted)}, "
            f"anchor={landed.intents[0].start.strftime('%H:%M:%S')} "
            f"now={gated_runtime.now.strftime('%H:%M:%S')}"
        )
        assert len(fenced) == 1, f"{label}: the defective plan must be fenced"
        assert len(accepted) == 1, f"{label}: a clean re-solve must still land"
        assert landed.intents[0].start >= gated_runtime.now
        await gated_runtime.stop(at=gated_runtime.now)

    # Summary of the main runtime
    _section("RECORD SUMMARY (main runtime)")
    print(f"  total records        : {len(sink.records)}")
    print(f"  solves requested     : {len(sink.records_of(ReplayRecordKind.SOLVE_REQUESTED))}")
    print(f"  solves completed     : {len(sink.records_of(ReplayRecordKind.SOLVE_COMPLETED))}")
    print(f"  solves failed        : {len(sink.records_of(ReplayRecordKind.SOLVE_FAILED))}")
    print(f"  plans accepted       : {len(sink.records_of(ReplayRecordKind.PLAN_ACCEPTED))}")
    print(f"  plans invalidated    : {len(sink.records_of(ReplayRecordKind.PLAN_INVALIDATED))}")
    print(f"  triggers fired       : {len(sink.records_of(ReplayRecordKind.TRIGGER_FIRED))}")
    print(f"  sink device writes   : {sink.has_device_writes}")

    print("\n  Full record log:")
    for r in sink.records:
        details = ", ".join(f"{k}={v}" for k, v in r.details.items())
        print(f"    [{r.at.strftime('%H:%M:%S')}] {r.kind.value:22s} {details[:96]}")

    print(f"\n{'=' * 66}\n  DONE\n{'=' * 66}")


if __name__ == "__main__":
    asyncio.run(main())
