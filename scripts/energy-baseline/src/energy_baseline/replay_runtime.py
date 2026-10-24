"""Replay-only dispatch runtime — asyncio orchestration with caller-supplied time.

This module provides a deterministic, replay-capable dispatch runtime that
exercises the existing planner (``solve_oracle`` / shared battery kernel) and
the dispatch contract (``DispatchPlan`` / ``DispatchIntent``) without any
network, writer, actuation, or wall-clock dependency.

Design principles
-----------------
1. **Caller-supplied time.**  Every timestamp in this module is an aware-UTC
   ``datetime`` provided by the caller.  No ``datetime.now()``, ``utcnow()``
   or ``time.time()`` calls anywhere.  Replay is deterministic apart from
   execution timing.

2. **No writer, no actuation.**  The sink is recording-only.  It captures
   solve requests, plan acceptances, invalidations and errors, but never
   sends commands to any device.  No HA adapter credentials, no writer
   abstractions capable of sending, no manual controller settings.  This
   module does not import any device-control library (no ``homeassistant``,
   ``paho.mqtt``, ``aiohttp`` client sessions, ``subprocess`` for device
   commands).

3. **Injectable planner.**  The planner is a synchronous callable run in a
   **dedicated single-thread executor** (``ThreadPoolExecutor(max_workers=1)``).
   At most one real worker runs at a time.  A generation identity fences
   late results: after timeout/restart/disconnect the old worker's result
   is discarded even if the thread eventually completes.  The default bridge
   uses ``solve_oracle`` + ``intents_from_schedule`` to produce validated
   ``DispatchPlan`` objects.

4. **Freshness and availability rules.**  Telemetry (SOC, meter power,
   temperature) has configurable staleness thresholds.  Prices carry
   publication metadata: a price for interval ``t`` is only usable after its
   ``published_at`` instant, and never before.  Future prices are never
   exposed to the planner.  Freshness is re-validated at plan acceptance
   time, not just at solve-request time, and acceptance additionally fences
   any plan whose input snapshots no longer match the authoritative ones (a
   mid-solve update queues a re-solve) or whose first interval has already
   begun.

5. **Invalidation on every defect.**  Stale/missing telemetry, missing/
   unpublished prices, solver errors, expired plans, and duplicate/
   superseded events all invalidate the active plan.  Reconnection, start
   and restart always invalidate — a persisted plan is never resumed as
   authority.  Restart clears all observations (telemetry, prices, forecast)
   and requires a new full snapshot before planning can resume.  What it never
   clears is the generation numbering: the plan sequence is provenance about
   what this runtime has already issued, not an observation, so it carries
   forward (principle 10).

6. **Bounded solve timeout.**  Solves run in a dedicated single-thread
   executor with a configurable timeout.  Late completions are safely
   fenced by generation identity and discarded.  No new solve starts until
   the previous worker has actually finished — including across restarts,
   which never replace the executor or release worker occupancy early.

7. **Trigger coalescing.**  Scheduled and state-event triggers that arrive
   while a solve is in flight are coalesced: the second trigger is recorded
   but no new solve starts until the first completes.  If inputs changed
   while the first solve was busy, the coalesced trigger re-solves with the
   latest snapshot.

8. **Explicit load forecast.**  The runtime does not infer gross load from
   net grid import.  A caller must supply a load forecast (per-interval kWh
   aligned with the price publication intervals) or an injectable
   ``LoadForecastProvider``.  Missing forecast refuses planning.

9. **Price interval validation.**  Price publications must have ordered,
   contiguous intervals at exactly ``interval_seconds`` spacing, with
   ``published_at <= decision_time`` and full coverage of the requested
   planning window.  Gaps or non-contiguous intervals fail validation.

10. **Plan sequence is monotonic.**  The plan sequence never resets across
    restart/disconnect/reconnect — on *either* restart path.  ``push_restart``
    keeps the live state object, so the counter survives it untouched; ``start()``
    installs a fresh state and therefore seeds it with the outgoing counter.
    It is a strictly increasing counter across all generations from one runtime
    instance, ensuring the ``accepts_successor`` contract is always satisfiable
    and that one recording sink can never hold two distinct plans numbered
    alike.  Event-record numbering (``_event_counter``) is likewise instance-level
    and survives every restart.

Caller API
----------
The main entry point is :class:`ReplayRuntime`.  Construct it with a
:class:`BatterySpec`, a planner callable, a load forecast provider, and
freshness/timeout configuration.  Feed it events via
:meth:`ReplayRuntime.push_event` (or the convenience methods).  Advance
simulated time with :meth:`ReplayRuntime.advance_to`.  Collect results
from the :class:`RecordingSink`.

Example::

    runtime = ReplayRuntime(
        battery_spec=spec,
        planner=OraclePlannerAdapter(spec, horizon_slots=96),
        load_forecast=ConstantLoadProvider(0.3),
        sink=RecordingSink(),
        telemetry_freshness_s=120.0,
        price_freshness_s=3600.0,
        solve_timeout_s=30.0,
    )
    await runtime.start(at=epoch)
    await runtime.push_telemetry(soc_frac=0.5, meter_w=400.0, temp_c=25.0, at=t0)
    await runtime.push_price_publication(prices, published_at=t0, valid_from=t0, ...)
    await runtime.push_scheduled_trigger(at=t1)
    await runtime.drain()  # wait for pending solve
    records = runtime.sink.records

Limitations
-----------
- No persistence: the runtime holds no durable state between runs.
- No real device communication.
- The planner bridge currently supports oracle-based solves only; extending
  to other planners requires implementing the ``SyncPlanner`` protocol.
- Not a production HA consumer — this is a replay/baseline tool.
"""

from __future__ import annotations

import asyncio
import enum
import math
import uuid
from collections.abc import Sequence
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass, field, replace
from datetime import datetime, timedelta
from typing import Any, Protocol, runtime_checkable

import numpy as np

from .battery import BatterySpec, Horizon, validate_spec
from .dispatch import (
    DispatchError,
    DispatchPlan,
    intents_from_schedule,
)
from .oracle import OracleError, solve_oracle

__all__ = [
    "ConstantLoadProvider",
    "EventType",
    "InvalidationReason",
    "LoadForecastProvider",
    "OraclePlannerAdapter",
    "PricePublication",
    "RecordingSink",
    "ReplayEvent",
    "ReplayRecord",
    "ReplayRecordKind",
    "ReplayRuntime",
    "ReplayRuntimeError",
    "SinkRecord",
    "StateSnapshot",
    "SyncPlanner",
    "TelemetrySnapshot",
    "TriggerKind",
]


# ---------------------------------------------------------------------------
# Errors
# ---------------------------------------------------------------------------


class ReplayRuntimeError(RuntimeError):
    """A replay runtime invariant was violated."""


class _MissingForecastError(ReplayRuntimeError):
    """No usable load forecast for the planning window.

    Distinct from a solver error: planning is refused because an *input* is
    absent, not because the optimizer failed.  Carries its own invalidation
    reason so the sink records an accurate cause.
    """


# ---------------------------------------------------------------------------
# Enumerations
# ---------------------------------------------------------------------------


class EventType(enum.Enum):
    """Types of replay events the runtime accepts."""

    TELEMETRY = "telemetry"
    PRICE_PUBLICATION = "price_publication"
    STATE_SNAPSHOT = "state_snapshot"
    SCHEDULED_TRIGGER = "scheduled_trigger"
    STATE_EVENT_TRIGGER = "state_event_trigger"
    DISCONNECT = "disconnect"
    RECONNECT = "reconnect"
    RESTART = "restart"


class TriggerKind(enum.Enum):
    """What caused a replan."""

    SCHEDULED = "scheduled"
    STATE_EVENT = "state_event"
    COALESCED = "coalesced"


class InvalidationReason(enum.Enum):
    """Why the active plan was invalidated."""

    STALE_TELEMETRY = "stale_telemetry"
    MISSING_TELEMETRY = "missing_telemetry"
    MISSING_PRICES = "missing_prices"
    STALE_PRICES = "stale_prices"
    UNPUBLISHED_PRICES = "unpublished_prices"
    MISSING_FORECAST = "missing_forecast"
    SOLVER_ERROR = "solver_error"
    SOLVER_TIMEOUT = "solver_timeout"
    PLAN_EXPIRED = "plan_expired"
    DUPLICATE_EVENT = "duplicate_event"
    SUPERSEDED_EVENT = "superseded_event"
    DISCONNECT = "disconnect"
    RESTART = "restart"
    INVALID_PLAN = "invalid_plan"


class ReplayRecordKind(enum.Enum):
    """Types of records the sink captures."""

    SOLVE_REQUESTED = "solve_requested"
    SOLVE_COMPLETED = "solve_completed"
    SOLVE_FAILED = "solve_failed"
    SOLVE_TIMEOUT = "solve_timeout"
    PLAN_ACCEPTED = "plan_accepted"
    PLAN_INVALIDATED = "plan_invalidated"
    TRIGGER_FIRED = "trigger_fired"
    EVENT_RECEIVED = "event_received"
    RUNTIME_STARTED = "runtime_started"
    RUNTIME_STOPPED = "runtime_stopped"
    WORKER_SATURATION = "worker_saturation"


# ---------------------------------------------------------------------------
# Data carriers
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class TelemetrySnapshot:
    """A telemetry reading at a specific instant.

    All fields are required for a complete snapshot.  Partial updates are
    represented as separate events.
    """

    soc_frac: float
    """Battery state of charge as fraction of nameplate capacity [0, 1]."""

    meter_w: float
    """Instantaneous grid meter power in Watts (positive = import)."""

    temp_c: float
    """Battery or ambient temperature in Celsius."""

    at: datetime
    """Aware-UTC timestamp of the reading."""

    def __post_init__(self) -> None:
        if self.at.tzinfo is None or self.at.tzinfo.utcoffset(self.at) is None:
            raise ReplayRuntimeError("TelemetrySnapshot.at must be aware UTC")
        if not (0.0 <= self.soc_frac <= 1.0):
            raise ReplayRuntimeError(f"soc_frac must be in [0, 1], got {self.soc_frac}")
        if not math.isfinite(self.meter_w):
            raise ReplayRuntimeError(f"meter_w must be finite, got {self.meter_w}")
        if not math.isfinite(self.temp_c):
            raise ReplayRuntimeError(f"temp_c must be finite, got {self.temp_c}")


@dataclass(frozen=True)
class PricePublication:
    """A batch of prices published at a specific instant.

    Prices carry publication metadata: they are only usable after
    ``published_at`` and cover the interval range
    ``[valid_from, valid_to)``.  The runtime never exposes prices before
    their publication instant.

    Intervals must be ordered, contiguous, and spaced exactly
    ``interval_seconds`` apart.  Gaps or non-contiguous intervals fail
    validation.
    """

    prices: tuple[float, ...]
    """Per-interval prices in EUR/kWh, pretax."""

    interval_starts: tuple[datetime, ...]
    """Aware-UTC start of each interval, same length as ``prices``."""

    published_at: datetime
    """When these prices became known (aware UTC)."""

    valid_from: datetime
    """First interval start covered (aware UTC)."""

    valid_to: datetime
    """One past the last interval start covered (aware UTC)."""

    interval_seconds: float = 900.0
    """Duration of each interval in seconds."""

    def __post_init__(self) -> None:
        if len(self.prices) != len(self.interval_starts):
            raise ReplayRuntimeError(
                f"prices has {len(self.prices)} entries but interval_starts has "
                f"{len(self.interval_starts)}"
            )
        if not self.prices:
            raise ReplayRuntimeError("PricePublication must have at least one price")
        for dt in (self.published_at, self.valid_from, self.valid_to):
            if dt.tzinfo is None or dt.tzinfo.utcoffset(dt) is None:
                raise ReplayRuntimeError("PricePublication timestamps must be aware UTC")
        if self.valid_to <= self.valid_from:
            raise ReplayRuntimeError(
                f"valid_to ({self.valid_to}) must be after valid_from ({self.valid_from})"
            )
        if self.published_at > self.valid_from:
            raise ReplayRuntimeError(
                f"published_at ({self.published_at}) must not be after "
                f"valid_from ({self.valid_from}): prices published after their "
                "validity start would expose future data"
            )

        # Validate intervals are ordered, contiguous, and evenly spaced
        step = timedelta(seconds=self.interval_seconds)
        for i in range(len(self.interval_starts)):
            if self.interval_starts[i].tzinfo is None:
                raise ReplayRuntimeError(f"interval_starts[{i}] must be aware UTC")
        for i in range(1, len(self.interval_starts)):
            gap = self.interval_starts[i] - self.interval_starts[i - 1]
            if gap != step:
                raise ReplayRuntimeError(
                    f"price intervals not contiguous: gap between [{i - 1}] and "
                    f"[{i}] is {gap}, expected {step}"
                )
        # First interval must match valid_from
        if self.interval_starts[0] != self.valid_from:
            raise ReplayRuntimeError(
                f"first interval start {self.interval_starts[0]} does not match "
                f"valid_from {self.valid_from}"
            )


@dataclass(frozen=True)
class StateSnapshot:
    """A complete system state for replanning.

    Combines telemetry with the current price curve.  Used to seed the
    runtime after a reconnect or restart.
    """

    telemetry: TelemetrySnapshot
    prices: PricePublication
    at: datetime

    def __post_init__(self) -> None:
        if self.at.tzinfo is None or self.at.tzinfo.utcoffset(self.at) is None:
            raise ReplayRuntimeError("StateSnapshot.at must be aware UTC")


@dataclass(frozen=True)
class ReplayEvent:
    """A typed replay event.

    Use the convenience constructors or push directly via the runtime's
    typed methods.
    """

    event_type: EventType
    at: datetime
    payload: Any = None

    def __post_init__(self) -> None:
        if self.at.tzinfo is None or self.at.tzinfo.utcoffset(self.at) is None:
            raise ReplayRuntimeError("ReplayEvent.at must be aware UTC")


@dataclass(frozen=True)
class ReplayRecord:
    """A single record captured by the sink."""

    kind: ReplayRecordKind
    at: datetime
    details: dict[str, Any] = field(default_factory=dict)


@dataclass(frozen=True)
class SinkRecord:
    """Alias for :class:`ReplayRecord` for backward compatibility."""

    kind: ReplayRecordKind
    at: datetime
    details: dict[str, Any] = field(default_factory=dict)


# ---------------------------------------------------------------------------
# Load forecast protocol and providers
# ---------------------------------------------------------------------------


@runtime_checkable
class LoadForecastProvider(Protocol):
    """Provides per-interval load forecasts aligned with price intervals.

    The runtime does not infer gross load from net grid import.  A caller
    must supply a load forecast.  Missing forecast refuses planning.
    """

    def forecast(
        self,
        interval_starts: Sequence[datetime],
        at: datetime,
    ) -> tuple[float, ...]:
        """Return per-interval load forecast in kWh.

        Args:
            interval_starts: The intervals to forecast for.
            at: The decision instant.

        Returns:
            Tuple of load forecasts in kWh, one per interval.
        """
        ...


class ConstantLoadProvider:
    """A simple load forecast provider that returns a constant value.

    Useful for testing and synthetic replays.
    """

    def __init__(self, load_kwh: float) -> None:
        if not math.isfinite(load_kwh) or load_kwh < 0:
            raise ReplayRuntimeError(f"load_kwh must be finite and >= 0, got {load_kwh}")
        self._load_kwh = load_kwh

    def forecast(
        self,
        interval_starts: Sequence[datetime],
        at: datetime,
    ) -> tuple[float, ...]:
        """Return constant load for all intervals."""
        return tuple(self._load_kwh for _ in interval_starts)


# ---------------------------------------------------------------------------
# Planner protocol and adapters
# ---------------------------------------------------------------------------


@runtime_checkable
class SyncPlanner(Protocol):
    """Synchronous planner callable.

    Receives the battery spec, a horizon, and the decision instant; returns
    a validated :class:`DispatchPlan`.  Run in a dedicated single-thread
    executor (at most one worker at a time).
    """

    def __call__(
        self,
        spec: BatterySpec,
        horizon: Horizon,
        interval_starts: Sequence[datetime],
        generated_at: datetime,
        expires_at: datetime,
        plan_sequence: int,
    ) -> DispatchPlan: ...


class OraclePlannerAdapter:
    """Bridge from ``solve_oracle`` + ``intents_from_schedule`` to :class:`SyncPlanner`.

    Produces validated ``DispatchPlan`` objects by solving the MILP oracle
    and converting the resulting ``Schedule`` to dispatch intents.

    Args:
        battery_spec: The battery specification (used for feasibility checks).
        horizon_slots: Number of intervals in the planning horizon.
        time_limit_s: Solver time limit passed to HiGHS.
        terminal_mode: ``"fixed"`` or ``"free"`` (see :func:`solve_oracle`).
        terminal_value_eur_per_kwh: Required for ``"free"`` mode.
    """

    def __init__(
        self,
        battery_spec: BatterySpec,
        horizon_slots: int = 96,
        *,
        time_limit_s: float = 30.0,
        terminal_mode: str = "fixed",
        terminal_value_eur_per_kwh: float | None = None,
    ) -> None:
        self._spec = battery_spec
        self._horizon_slots = horizon_slots
        self._time_limit_s = time_limit_s
        self._terminal_mode = terminal_mode
        self._terminal_value = terminal_value_eur_per_kwh

    def __call__(
        self,
        spec: BatterySpec,
        horizon: Horizon,
        interval_starts: Sequence[datetime],
        generated_at: datetime,
        expires_at: datetime,
        plan_sequence: int,
    ) -> DispatchPlan:
        """Solve the oracle and convert to a DispatchPlan."""
        schedule = solve_oracle(
            spec,
            horizon,
            time_limit_s=self._time_limit_s,
            terminal_mode=self._terminal_mode,
            terminal_value_eur_per_kwh=self._terminal_value,
        )
        return intents_from_schedule(
            schedule,
            horizon,
            battery_spec=spec,
            interval_starts_utc=list(interval_starts),
            generated_at=generated_at,
            expires_at=expires_at,
            plan_sequence=plan_sequence,
            expected_buy_prices=horizon.prices.tolist(),
        )


# ---------------------------------------------------------------------------
# Recording sink
# ---------------------------------------------------------------------------


class RecordingSink:
    """A recording-only sink that captures all runtime events.

    No device writes, no network, no actuation.  Purely observational.
    This module does not import any device-control library.
    """

    def __init__(self) -> None:
        self._records: list[ReplayRecord] = []

    @property
    def records(self) -> list[ReplayRecord]:
        """All captured records, in order."""
        return list(self._records)

    def record(self, kind: ReplayRecordKind, at: datetime, **details: Any) -> None:
        """Append a record."""
        self._records.append(ReplayRecord(kind=kind, at=at, details=dict(details)))

    def records_of(self, kind: ReplayRecordKind) -> list[ReplayRecord]:
        """Filter records by kind."""
        return [r for r in self._records if r.kind is kind]

    def clear(self) -> None:
        """Remove all records."""
        self._records.clear()

    @property
    def has_device_writes(self) -> bool:
        """Convenience flag — always False for this recording-only sink.

        The normative evidence that the runtime never writes to devices is the
        AST import-allowlist guard in
        :meth:`TestSinkProperties.test_no_device_library_imports_ast_guard`
        and the call-scan guard in
        :meth:`TestSinkProperties.test_no_device_call_expressions_ast_guard`.
        """
        return False


# ---------------------------------------------------------------------------
# Runtime state
# ---------------------------------------------------------------------------


@dataclass
class _SolveRequest:
    """Identity of a solve request for fencing late results *and* stale inputs.

    The snapshots are not bookkeeping: acceptance compares them against the
    runtime's authoritative ``latest_*`` so a plan solved on superseded inputs
    can never be installed.
    """

    generation_id: str
    """Unique ID for this solve generation."""

    plan_sequence: int
    """Plan sequence at request time."""

    trigger_kind: TriggerKind
    """What triggered this solve."""

    requested_at: datetime
    """Simulated time when the solve was requested."""

    telemetry_snapshot: TelemetrySnapshot
    """Telemetry snapshot this solve was really handed (fence input)."""

    prices_snapshot: PricePublication
    """Price publication this solve was really handed (fence input)."""


@dataclass
class _RuntimeState:
    """Mutable internal state of the replay runtime."""

    now: datetime | None = None
    """Current simulated time."""

    started: bool = False
    """Whether the runtime has been started."""

    connected: bool = False
    """Whether the runtime considers itself connected."""

    plan_sequence: int = 0
    """Next plan sequence number to assign (monotonic, never resets)."""

    active_plan: DispatchPlan | None = None
    """The currently active dispatch plan, if any."""

    latest_telemetry: TelemetrySnapshot | None = None
    """Most recent telemetry reading."""

    latest_prices: PricePublication | None = None
    """Most recent price publication."""

    pending_solve: _SolveRequest | None = None
    """Identity of the in-flight solve, if any."""

    pending_trigger: TriggerKind | None = None
    """A trigger waiting to fire (for coalescing)."""

    invalidation_reason: InvalidationReason | None = None
    """Why the active plan was last invalidated, if any."""


# ---------------------------------------------------------------------------
# Replay runtime
# ---------------------------------------------------------------------------


class ReplayRuntime:
    """Asyncio orchestration for replay-only dispatch.

    Args:
        battery_spec: Battery physical parameters.
        planner: Synchronous planner callable (see :class:`SyncPlanner`).
        load_forecast: Load forecast provider (see :class:`LoadForecastProvider`).
        sink: Recording sink for capturing events.
        telemetry_freshness_s: Maximum age of telemetry before it is stale.
        price_freshness_s: Maximum age of prices before they are stale.
        solve_timeout_s: Maximum time to wait for a solve to complete.
        coalesce_window_s: Window within which triggers are coalesced.
        plan_ttl_s: Time-to-live for a plan after generation.
        interval_seconds: Duration of each dispatch interval.
    """

    def __init__(
        self,
        battery_spec: BatterySpec,
        planner: SyncPlanner,
        load_forecast: LoadForecastProvider,
        sink: RecordingSink,
        *,
        telemetry_freshness_s: float = 120.0,
        price_freshness_s: float = 3600.0,
        solve_timeout_s: float = 30.0,
        coalesce_window_s: float = 5.0,
        plan_ttl_s: float = 900.0,
        interval_seconds: float = 900.0,
    ) -> None:
        validate_spec(battery_spec)
        if telemetry_freshness_s <= 0:
            raise ReplayRuntimeError(
                f"telemetry_freshness_s must be > 0, got {telemetry_freshness_s}"
            )
        if price_freshness_s <= 0:
            raise ReplayRuntimeError(f"price_freshness_s must be > 0, got {price_freshness_s}")
        if solve_timeout_s <= 0:
            raise ReplayRuntimeError(f"solve_timeout_s must be > 0, got {solve_timeout_s}")
        if coalesce_window_s < 0:
            raise ReplayRuntimeError(f"coalesce_window_s must be >= 0, got {coalesce_window_s}")
        if plan_ttl_s <= 0:
            raise ReplayRuntimeError(f"plan_ttl_s must be > 0, got {plan_ttl_s}")
        if interval_seconds <= 0:
            raise ReplayRuntimeError(f"interval_seconds must be > 0, got {interval_seconds}")

        self._spec = battery_spec
        self._planner = planner
        self._load_forecast = load_forecast
        self.sink = sink
        self._telemetry_freshness_s = telemetry_freshness_s
        self._price_freshness_s = price_freshness_s
        self._solve_timeout_s = solve_timeout_s
        self._coalesce_window_s = coalesce_window_s
        self._plan_ttl_s = plan_ttl_s
        self._interval_seconds = interval_seconds
        self._state = _RuntimeState()
        self._event_counter = 0
        self._loop: asyncio.AbstractEventLoop | None = None
        self._active_generation = ""
        self._result_task: asyncio.Task[None] | None = None

        # Dedicated single-thread executor for planner solves.
        # At most one real worker at a time.
        self._executor = ThreadPoolExecutor(max_workers=1, thread_name_prefix="replay-planner")
        self._executor_open = True
        self._worker_busy = False
        self._solve_done_event = asyncio.Event()
        self._solve_done_event.set()  # Initially no solve in progress

    @property
    def now(self) -> datetime | None:
        """Current simulated time."""
        return self._state.now

    @property
    def is_started(self) -> bool:
        """Whether the runtime has been started."""
        return self._state.started

    @property
    def is_connected(self) -> bool:
        """Whether the runtime considers itself connected."""
        return self._state.connected

    @property
    def active_plan(self) -> DispatchPlan | None:
        """The currently active dispatch plan, if any."""
        return self._state.active_plan

    @property
    def plan_sequence(self) -> int:
        """Next plan sequence number to assign (monotonic, never resets)."""
        return self._state.plan_sequence

    @property
    def is_solving(self) -> bool:
        """Whether a solve is currently in progress."""
        return self._state.pending_solve is not None

    # -- lifecycle -----------------------------------------------------------

    async def start(self, *, at: datetime) -> None:
        """Start the runtime at the given simulated time.

        Invalidates any previous plan and drops all observed state.  A started
        runtime is connected.

        Restart does **not** replace the executor and does **not** clear worker
        occupancy.  A planner thread that is still running cannot be killed, so
        dropping ``_worker_busy`` here would let a restart launch a *second* real
        worker beside the stuck first one — breaking the global max-one-worker
        bound.  Instead the single dedicated executor is kept and
        :meth:`_on_worker_done`'s generation fence frees the worker slot exactly
        when the stuck solve's thread actually ends; until then every trigger is
        refused as saturation.  :meth:`stop` is the only path that shuts the pool
        down, and it joins the worker first, so restarting after a stop gets a
        fresh pool with no worker left behind.

        Restart does **not** reset the plan sequence: it belongs to this runtime
        instance, not to the observed system, so the outgoing counter is carried
        into the fresh state (see design principle 10).
        """
        if at.tzinfo is None or at.tzinfo.utcoffset(at) is None:
            raise ReplayRuntimeError("start time must be aware UTC")

        # Restart invalidates everything.  Nothing waits for an in-flight worker
        # here: occupancy is not cleared below, so a stuck worker keeps holding
        # the single slot and the restart itself is instantaneous.
        was_started = self._state.started
        carried_plan_sequence = self._state.plan_sequence
        if was_started:
            self._invalidate(InvalidationReason.RESTART, at)

        self._state = _RuntimeState()
        self._state.now = at
        self._state.started = True
        self._state.connected = True
        # Principle 10: the plan sequence is a counter of *generations produced by
        # this runtime*, not observed system state, so a restart must carry it.
        # ``push_restart`` keeps the live state object and therefore preserves it
        # implicitly; this path replaces the state, so it is seeded explicitly —
        # otherwise one sink can hold two distinct plans both numbered 0 and the
        # successor check would compare against a restarted counter rather than a
        # globally monotonic one.
        self._state.plan_sequence = carried_plan_sequence
        # The invalidation reason is provenance about why no plan is active, not an
        # observation either: after a restart it reads RESTART on both paths.
        if was_started:
            self._state.invalidation_reason = InvalidationReason.RESTART
        if not self._executor_open:
            # Safe to build a new pool only here: stop() joined the previous
            # worker with ``shutdown(wait=True)`` before closing this flag.
            self._executor = ThreadPoolExecutor(max_workers=1, thread_name_prefix="replay-planner")
            self._executor_open = True
        # ``_worker_busy`` and ``_active_generation`` are deliberately preserved
        # across a restart: a worker still running from a previous generation
        # keeps the single slot occupied until its thread actually ends, which is
        # what bounds concurrency to one.  The drain gate is re-armed only when
        # nothing is outstanding, so a stuck worker never releases a new drain.
        if not self._worker_busy:
            self._solve_done_event = asyncio.Event()
            self._solve_done_event.set()
        self._loop = asyncio.get_running_loop()
        self.sink.record(ReplayRecordKind.RUNTIME_STARTED, at=at)

    async def stop(self, *, at: datetime | None = None) -> None:
        """Stop the runtime."""
        when = at or self._state.now
        if when is None:
            raise ReplayRuntimeError("cannot stop before start")
        await self._wait_for_worker()
        self._executor.shutdown(wait=True)
        self._executor_open = False
        self._state.started = False
        self._state.connected = False
        self._state.pending_solve = None
        self.sink.record(ReplayRecordKind.RUNTIME_STOPPED, at=when)

    async def drain(self, *, timeout_s: float = 30.0) -> None:
        """Wait until the worker has finished *and* its result has been handled.

        This is the synchronization primitive tests should use instead of
        sleeping.  Two things must both happen: the real worker thread ends
        (``_solve_done_event``), and the result-processing task completes
        (so the plan is accepted or the failure recorded).
        """

        async def _settle() -> None:
            await self._solve_done_event.wait()
            task = self._result_task
            if task is not None and not task.done():
                try:
                    await asyncio.shield(task)
                except (asyncio.CancelledError, Exception):
                    pass

        try:
            await asyncio.wait_for(_settle(), timeout=timeout_s)
        except TimeoutError:
            pass  # Caller can inspect is_solving / the sink

    # -- event ingestion -----------------------------------------------------

    async def push_event(self, event: ReplayEvent) -> None:
        """Push a typed replay event."""
        if not self._state.started:
            raise ReplayRuntimeError("runtime not started")

        # Advance simulated time to event time
        if self._state.now is not None and event.at < self._state.now:
            raise ReplayRuntimeError(
                f"event time {event.at} is before current time {self._state.now}"
            )
        self._state.now = event.at

        self._event_counter += 1
        self.sink.record(
            ReplayRecordKind.EVENT_RECEIVED,
            at=event.at,
            event_type=event.event_type.value,
            sequence=self._event_counter,
        )

        # Handle lifecycle events
        if event.event_type is EventType.DISCONNECT:
            self._handle_disconnect(event.at)
            return
        if event.event_type is EventType.RECONNECT:
            self._handle_reconnect(event.at, event.payload)
            return
        if event.event_type is EventType.RESTART:
            self._invalidate(InvalidationReason.RESTART, event.at)
            self._state.connected = True
            # Restart clears all observations
            self._state.latest_telemetry = None
            self._state.latest_prices = None
            return

        if not self._state.connected:
            return  # ignore non-lifecycle events when disconnected

        # Handle data events
        if event.event_type is EventType.TELEMETRY:
            self._handle_telemetry(event.payload, event.at)
        elif event.event_type is EventType.PRICE_PUBLICATION:
            self._handle_price_publication(event.payload, event.at)
        elif event.event_type is EventType.STATE_SNAPSHOT:
            self._handle_state_snapshot(event.payload, event.at)
        elif event.event_type is EventType.SCHEDULED_TRIGGER:
            self._handle_trigger(TriggerKind.SCHEDULED, event.at)
        elif event.event_type is EventType.STATE_EVENT_TRIGGER:
            self._handle_trigger(TriggerKind.STATE_EVENT, event.at)

    async def push_telemetry(
        self,
        *,
        soc_frac: float,
        meter_w: float,
        temp_c: float,
        at: datetime,
        source_at: datetime | None = None,
    ) -> None:
        """Convenience: push a telemetry snapshot.

        ``at`` is the arrival time (when the runtime receives the reading).
        ``source_at`` is when the reading was actually taken; it defaults to
        ``at``.  Separating them lets a caller deliver a late-arriving, older
        observation — which the runtime rejects as out of order rather than
        silently accepting as current.
        """
        snap = TelemetrySnapshot(
            soc_frac=soc_frac, meter_w=meter_w, temp_c=temp_c, at=source_at or at
        )
        await self.push_event(ReplayEvent(EventType.TELEMETRY, at=at, payload=snap))

    async def push_price_publication(
        self,
        prices: Sequence[float],
        *,
        interval_starts: Sequence[datetime],
        published_at: datetime,
        valid_from: datetime,
        valid_to: datetime,
        interval_seconds: float = 900.0,
        at: datetime | None = None,
    ) -> None:
        """Convenience: push a price publication.

        ``at`` is the event time (when the runtime receives this).  Defaults
        to ``published_at`` if not given.
        """
        pub = PricePublication(
            prices=tuple(prices),
            interval_starts=tuple(interval_starts),
            published_at=published_at,
            valid_from=valid_from,
            valid_to=valid_to,
            interval_seconds=interval_seconds,
        )
        event_at = at if at is not None else published_at
        await self.push_event(ReplayEvent(EventType.PRICE_PUBLICATION, at=event_at, payload=pub))

    async def push_state_snapshot(
        self,
        telemetry: TelemetrySnapshot,
        prices: PricePublication,
        *,
        at: datetime,
    ) -> None:
        """Convenience: push a complete state snapshot."""
        snap = StateSnapshot(telemetry=telemetry, prices=prices, at=at)
        await self.push_event(ReplayEvent(EventType.STATE_SNAPSHOT, at=at, payload=snap))

    async def push_scheduled_trigger(self, at: datetime) -> None:
        """Convenience: push a scheduled trigger."""
        await self.push_event(ReplayEvent(EventType.SCHEDULED_TRIGGER, at=at))

    async def push_state_event_trigger(self, at: datetime) -> None:
        """Convenience: push a state-event trigger."""
        await self.push_event(ReplayEvent(EventType.STATE_EVENT_TRIGGER, at=at))

    async def push_disconnect(self, at: datetime) -> None:
        """Convenience: push a disconnect event."""
        await self.push_event(ReplayEvent(EventType.DISCONNECT, at=at))

    async def push_reconnect(self, at: datetime, snapshot: StateSnapshot | None = None) -> None:
        """Convenience: push a reconnect event with optional state snapshot."""
        await self.push_event(ReplayEvent(EventType.RECONNECT, at=at, payload=snapshot))

    async def push_restart(self, at: datetime) -> None:
        """Convenience: push a restart event."""
        await self.push_event(ReplayEvent(EventType.RESTART, at=at))

    # -- time advancement ----------------------------------------------------

    async def advance_to(self, target: datetime) -> None:
        """Advance simulated time to ``target``, checking plan expiry."""
        if not self._state.started:
            raise ReplayRuntimeError("runtime not started")
        if self._state.now is not None and target < self._state.now:
            raise ReplayRuntimeError(
                f"target time {target} is before current time {self._state.now}"
            )
        self._state.now = target

        # Check if active plan has expired
        if self._state.active_plan is not None:
            if self._state.active_plan.is_expired(target):
                self._invalidate(InvalidationReason.PLAN_EXPIRED, target)

        # Check telemetry freshness
        self._check_telemetry_freshness(target)

        # Check price freshness
        self._check_price_freshness(target)

    # -- internal handlers ---------------------------------------------------

    def _handle_disconnect(self, at: datetime) -> None:
        """Handle a disconnect event."""
        self._state.connected = False
        self._invalidate(InvalidationReason.DISCONNECT, at)

    def _handle_reconnect(self, at: datetime, payload: StateSnapshot | None) -> None:
        """Handle a reconnect event.

        Reconnection always invalidates any previous plan — a persisted plan
        is never resumed as authority.  A fresh complete snapshot is
        recommended; if none is supplied (``payload is None``), the
        pre-disconnect telemetry and prices remain and are still subject to
        the normal solve-time freshness checks before any new solve.
        """
        self._state.connected = True
        # The disconnect before reconnect already invalidated; record reconnect
        # as a state restoration, not a separate invalidation reason
        self._state.active_plan = None
        self._state.invalidation_reason = InvalidationReason.DISCONNECT

        if payload is not None:
            # Accept the snapshot as fresh state
            self._state.latest_telemetry = payload.telemetry
            self._state.latest_prices = payload.prices

    def _handle_telemetry(self, snap: TelemetrySnapshot, at: datetime) -> None:
        """Handle a telemetry update.

        Source time and arrival time are separate: a reading whose source time
        is in the future relative to the runtime clock is forbidden, and one
        older than the current latest is rejected as superseded.
        """
        # A future-source observation is never admissible
        if snap.at > at:
            self.sink.record(
                ReplayRecordKind.EVENT_RECEIVED,
                at=at,
                rejected="future_source_telemetry",
                snap_at=snap.at.isoformat(),
                arrival_at=at.isoformat(),
            )
            return

        # Reject if older than current latest: a late, superseded reading must
        # not silently overwrite newer data, and it must not invalidate the plan
        # either (the plan was built from the newest snapshot, which still holds).
        if self._state.latest_telemetry is not None and snap.at < self._state.latest_telemetry.at:
            self.sink.record(
                ReplayRecordKind.EVENT_RECEIVED,
                at=at,
                rejected=InvalidationReason.SUPERSEDED_EVENT.value,
                snap_at=snap.at.isoformat(),
                latest_at=self._state.latest_telemetry.at.isoformat(),
            )
            return

        # Exact duplicate of the current reading: recorded, not re-processed.
        if self._state.latest_telemetry == snap:
            self.sink.record(
                ReplayRecordKind.EVENT_RECEIVED,
                at=at,
                rejected=InvalidationReason.DUPLICATE_EVENT.value,
                snap_at=snap.at.isoformat(),
            )
            return

        self._state.latest_telemetry = snap

    def _handle_price_publication(self, pub: PricePublication, at: datetime) -> None:
        """Handle a price publication."""
        # Validate publication metadata at arrival time
        if pub.published_at > at:
            self.sink.record(
                ReplayRecordKind.PLAN_INVALIDATED,
                at=at,
                reason=InvalidationReason.UNPUBLISHED_PRICES.value,
                detail="prices published_at is in the future relative to arrival time",
            )
            return

        self._state.latest_prices = pub

    def _handle_state_snapshot(self, snap: StateSnapshot, at: datetime) -> None:
        """Handle a complete state snapshot."""
        self._state.latest_telemetry = snap.telemetry
        self._state.latest_prices = snap.prices

    def _handle_trigger(self, kind: TriggerKind, at: datetime) -> None:
        """Handle a trigger event, with coalescing."""
        if self._state.pending_trigger is not None or self._state.pending_solve is not None:
            # Coalesce: merge into a single coalesced trigger
            self._state.pending_trigger = TriggerKind.COALESCED
            self.sink.record(
                ReplayRecordKind.TRIGGER_FIRED,
                at=at,
                trigger_kind=TriggerKind.COALESCED.value,
                coalesced=True,
            )
        else:
            self._state.pending_trigger = kind
            self.sink.record(
                ReplayRecordKind.TRIGGER_FIRED,
                at=at,
                trigger_kind=kind.value,
                coalesced=False,
            )

        # Attempt to fire the trigger (replan)
        self._try_fire_trigger(at)

    def _try_fire_trigger(self, at: datetime) -> None:
        """Attempt to fire a pending trigger by starting a solve."""
        if self._state.pending_trigger is None:
            return
        if self._state.pending_solve is not None:
            # Already solving — trigger is coalesced, will re-solve after current completes
            return
        if self._worker_busy:
            # Worker still finishing previous solve — record saturation
            self.sink.record(
                ReplayRecordKind.WORKER_SATURATION,
                at=at,
                detail="worker still finishing previous solve",
            )
            return

        # Validate preconditions
        if not self._can_solve(at):
            return

        trigger_kind = self._state.pending_trigger
        self._state.pending_trigger = None
        self._start_solve(at, trigger_kind)

    def _can_solve(self, at: datetime) -> bool:
        """Check whether we have valid inputs to solve.

        Re-validates freshness and availability at the decision time.
        """
        # Need telemetry
        if self._state.latest_telemetry is None:
            self._invalidate(InvalidationReason.MISSING_TELEMETRY, at)
            return False
        if self._is_telemetry_stale(at):
            self._invalidate(InvalidationReason.STALE_TELEMETRY, at)
            return False

        # Need prices
        if self._state.latest_prices is None:
            self._invalidate(InvalidationReason.MISSING_PRICES, at)
            return False
        if self._is_price_stale(at):
            self._invalidate(InvalidationReason.STALE_PRICES, at)
            return False

        # Check prices are published (not future)
        prices = self._state.latest_prices
        if prices.published_at > at:
            self._invalidate(InvalidationReason.UNPUBLISHED_PRICES, at)
            return False

        # Prices must still cover a *future* interval at the decision instant;
        # a curve that has run out cannot be planned against.
        step = timedelta(seconds=prices.interval_seconds)
        if not any(start + step > at for start in prices.interval_starts):
            self._invalidate(InvalidationReason.STALE_PRICES, at)
            return False

        return True

    def _start_solve(self, at: datetime, trigger_kind: TriggerKind) -> None:
        """Start a solve in the dedicated single-thread executor."""
        telemetry = self._state.latest_telemetry
        prices = self._state.latest_prices
        assert telemetry is not None
        assert prices is not None

        plan_seq = self._state.plan_sequence
        generation_id = uuid.uuid4().hex[:12]

        self.sink.record(
            ReplayRecordKind.SOLVE_REQUESTED,
            at=at,
            plan_sequence=plan_seq,
            trigger_kind=trigger_kind.value,
            soc_frac=telemetry.soc_frac,
            generation_id=generation_id,
        )

        # Build horizon from available prices
        try:
            spec = replace(self._spec, initial_soc_frac=telemetry.soc_frac)
            horizon, interval_starts = self._build_horizon(prices, at)
            expires_at = at + timedelta(seconds=self._plan_ttl_s)

            # Get load forecast.  The runtime never infers gross load from a net
            # meter reading: a missing or misaligned forecast refuses planning.
            try:
                load_forecast = self._load_forecast.forecast(interval_starts, at)
            except Exception as exc:  # provider failure => no planning
                raise _MissingForecastError(f"load forecast provider failed: {exc}") from exc
            if load_forecast is None or len(load_forecast) != horizon.n:
                raise _MissingForecastError(
                    f"load forecast has {0 if load_forecast is None else len(load_forecast)} "
                    f"entries, horizon has {horizon.n} intervals: planning is refused "
                    "rather than inventing a load number"
                )
            if any(not math.isfinite(v) or v < 0 for v in load_forecast):
                raise _MissingForecastError(
                    "load forecast contains a non-finite or negative value: gross load "
                    "must be a finite non-negative kWh per interval"
                )
            # Override horizon load with forecast
            horizon = Horizon(
                interval_seconds=horizon.interval_seconds,
                prices=horizon.prices,
                load_kwh=np.array(load_forecast),
                usable=horizon.usable,
                import_limit_kw=horizon.import_limit_kw,
            )

            # Create solve request identity
            request = _SolveRequest(
                generation_id=generation_id,
                plan_sequence=plan_seq,
                trigger_kind=trigger_kind,
                requested_at=at,
                telemetry_snapshot=telemetry,
                prices_snapshot=prices,
            )
            self._state.pending_solve = request
            self._worker_busy = True
            self._active_generation = generation_id
            self._solve_done_event.clear()

            # Submit directly to the dedicated single-thread executor so we hold
            # the ``concurrent.futures.Future``, whose done-callback fires when
            # the *thread actually finishes* — not when ``wait_for`` gives up.
            #
            # ``asyncio.wait_for`` cannot kill a running thread: cancelling the
            # asyncio wrapper leaves the worker executing.  If we cleared
            # ``_worker_busy`` on that cancellation we would launch a second
            # real worker on top of the first.  So worker occupancy is owned by
            # :meth:`_on_worker_done` below, keeping the pool at max one real
            # worker *including* abandoned/timed-out ones.
            loop = asyncio.get_running_loop()
            self._loop = loop
            cf_future = self._executor.submit(
                self._planner,
                spec,
                horizon,
                list(interval_starts),
                at,
                expires_at,
                plan_seq,
            )
            cf_future.add_done_callback(lambda _f, gen=generation_id: self._on_worker_done(gen))

            # Wrap with timeout (cancelling this wrapper does not stop the thread)
            async def _solve_with_timeout() -> DispatchPlan:
                return await asyncio.wait_for(
                    asyncio.wrap_future(cf_future), timeout=self._solve_timeout_s
                )

            task = asyncio.ensure_future(_solve_with_timeout())
            self._result_task = asyncio.ensure_future(self._await_solve(task, request))

        except _MissingForecastError as exc:
            self.sink.record(
                ReplayRecordKind.SOLVE_FAILED,
                at=at,
                plan_sequence=plan_seq,
                error=str(exc),
                reason=InvalidationReason.MISSING_FORECAST.value,
                generation_id=generation_id,
            )
            self._invalidate(InvalidationReason.MISSING_FORECAST, at)
            self._state.pending_trigger = None
            self._worker_busy = False
            self._active_generation = ""
            self._solve_done_event.set()

        except (DispatchError, OracleError, ValueError, ReplayRuntimeError) as exc:
            self.sink.record(
                ReplayRecordKind.SOLVE_FAILED,
                at=at,
                plan_sequence=plan_seq,
                error=str(exc),
                generation_id=generation_id,
            )
            self._invalidate(InvalidationReason.SOLVER_ERROR, at)
            self._state.pending_trigger = None
            self._worker_busy = False
            self._active_generation = ""
            self._solve_done_event.set()

    def _on_worker_done(self, generation_id: str) -> None:
        """Real worker completion.  Runs on the executor thread.

        Called whether the solve succeeded, failed, or was abandoned by a
        timeout.  Only here does ``_worker_busy`` clear — so a timed-out solve
        still occupies the worker until its thread actually ends.  This is what
        bounds the runtime to one real worker: a restart leaves occupancy
        untouched, so a stuck worker keeps the slot until it truly finishes.

        Fenced by generation: a callback from a superseded generation must not
        clear the current worker's occupancy flag.
        """
        if generation_id != self._active_generation:
            # Stale callback from a superseded/restarted generation.
            return
        self._worker_busy = False
        self._active_generation = ""
        loop = self._loop
        if loop is None:
            return
        try:
            loop.call_soon_threadsafe(self._after_worker)
        except RuntimeError:
            # Loop already closed (stop/shutdown path); nothing left to schedule.
            pass

    def _after_worker(self) -> None:
        """Event-loop side of worker completion."""
        self._try_fire_pending()
        self._release_drain_if_idle()

    def _try_fire_pending(self) -> None:
        """Fire a coalesced trigger if the runtime is actually free to solve.

        Called from both :meth:`_after_worker` and the tail of
        :meth:`_await_solve`, because the worker frees before the result is
        handled while ``pending_solve`` is only cleared during handling.  One of
        the two calls sees the runtime genuinely idle and starts the re-solve.
        """
        if self._state.pending_trigger is None or not self._state.connected:
            return
        if self._worker_busy or self._state.pending_solve is not None:
            return
        now = self._state.now
        if now is None:
            return
        self._try_fire_trigger(now)

    def _release_drain_if_idle(self) -> None:
        """Release :meth:`drain` waiters only when truly nothing is outstanding."""
        if (
            self._worker_busy
            or self._state.pending_solve is not None
            or self._state.pending_trigger is not None
        ):
            return
        self._solve_done_event.set()

    async def _await_solve(self, task: asyncio.Task[DispatchPlan], request: _SolveRequest) -> None:
        """Wait for a solve to complete and handle the result.

        This coroutine observes the *async* boundary only.  Worker occupancy is
        owned by :meth:`_on_worker_done`, so a timeout here does not pretend the
        thread was killed.
        """
        try:
            plan = await task
            # Generation fence: is this still the pending solve?
            if (
                self._state.pending_solve is None
                or self._state.pending_solve.generation_id != request.generation_id
            ):
                # Late completion after timeout/restart/disconnect — ignored.
                self.sink.record(
                    ReplayRecordKind.SOLVE_FAILED,
                    at=self._state.now or request.requested_at,
                    plan_sequence=request.plan_sequence,
                    error="ignored: generation fenced (timeout/restart/disconnect)",
                    generation_id=request.generation_id,
                    fenced=True,
                )
                return

            self._state.pending_solve = None
            self._accept_plan(plan, request)

        except TimeoutError:
            # The timeout does NOT kill the worker thread.  We clear the request
            # so no new generation is *awaited*, but ``_worker_busy`` stays True
            # and any further trigger records saturation until the thread ends.
            if (
                self._state.pending_solve is not None
                and self._state.pending_solve.generation_id == request.generation_id
            ):
                self._state.pending_solve = None
            self.sink.record(
                ReplayRecordKind.SOLVE_TIMEOUT,
                at=self._state.now or request.requested_at,
                plan_sequence=request.plan_sequence,
                timeout_s=self._solve_timeout_s,
                generation_id=request.generation_id,
            )
            self._invalidate(
                InvalidationReason.SOLVER_TIMEOUT,
                self._state.now or request.requested_at,
            )

        except (DispatchError, OracleError, ValueError) as exc:
            if (
                self._state.pending_solve is not None
                and self._state.pending_solve.generation_id == request.generation_id
            ):
                self._state.pending_solve = None
            self.sink.record(
                ReplayRecordKind.SOLVE_FAILED,
                at=self._state.now or request.requested_at,
                plan_sequence=request.plan_sequence,
                error=str(exc),
                generation_id=request.generation_id,
            )
            self._invalidate(
                InvalidationReason.SOLVER_ERROR,
                self._state.now or request.requested_at,
            )

        except asyncio.CancelledError:
            if (
                self._state.pending_solve is not None
                and self._state.pending_solve.generation_id == request.generation_id
            ):
                self._state.pending_solve = None

        finally:
            # Only drop the handle if it still refers to *this* result task: a
            # re-solve started by the fenced path above may already own it.
            if self._result_task is asyncio.current_task():
                self._result_task = None
            # Result is now handled: a coalesced trigger may re-solve with the
            # latest snapshot, and drain waiters are released only if idle.
            self._try_fire_pending()
            self._release_drain_if_idle()

    def _accept_plan(self, plan: DispatchPlan, request: _SolveRequest) -> None:
        """Accept a solved plan.

        Re-validates freshness, availability **and input identity** at acceptance
        time.  Source time (plan generation) and arrival time (acceptance) are
        separate.
        """
        at = self._state.now or request.requested_at

        # Re-validate freshness at acceptance time
        if self._is_telemetry_stale(at):
            self.sink.record(
                ReplayRecordKind.SOLVE_FAILED,
                at=at,
                plan_sequence=plan.plan_sequence,
                error="telemetry stale at acceptance time",
                generation_id=request.generation_id,
            )
            self._invalidate(InvalidationReason.STALE_TELEMETRY, at)
            return

        if self._is_price_stale(at):
            self.sink.record(
                ReplayRecordKind.SOLVE_FAILED,
                at=at,
                plan_sequence=plan.plan_sequence,
                error="prices stale at acceptance time",
                generation_id=request.generation_id,
            )
            self._invalidate(InvalidationReason.STALE_PRICES, at)
            return

        # Inputs must still be the ones this plan was solved against.  A plain
        # freshness check passes for a *newer* reading that arrived mid-solve, so
        # without this identity fence the runtime would install a plan computed
        # from superseded telemetry or prices.  Data events do not queue a
        # re-solve themselves, so the fence queues one: the coalesced trigger
        # below re-solves against the authoritative snapshot.
        current_telemetry = self._state.latest_telemetry
        current_prices = self._state.latest_prices
        superseded_telemetry = current_telemetry is not request.telemetry_snapshot
        superseded_prices = current_prices is not request.prices_snapshot
        if superseded_telemetry or superseded_prices:
            self.sink.record(
                ReplayRecordKind.SOLVE_FAILED,
                at=at,
                plan_sequence=plan.plan_sequence,
                error=(
                    "inputs superseded during solve: "
                    f"telemetry={'changed' if superseded_telemetry else 'same'}, "
                    f"prices={'changed' if superseded_prices else 'same'}"
                ),
                generation_id=request.generation_id,
                stale_inputs=True,
                replan_queued=True,
            )
            self._invalidate(InvalidationReason.SUPERSEDED_EVENT, at)
            self._state.pending_trigger = TriggerKind.COALESCED
            return

        # Validate successor relationship
        if self._state.active_plan is not None:
            try:
                self._state.active_plan.accepts_successor(plan)
            except DispatchError as exc:
                self.sink.record(
                    ReplayRecordKind.SOLVE_FAILED,
                    at=at,
                    plan_sequence=plan.plan_sequence,
                    error=f"successor validation failed: {exc}",
                    generation_id=request.generation_id,
                )
                self._invalidate(InvalidationReason.INVALID_PLAN, at)
                return

        # Check plan is not already expired
        if plan.is_expired(at):
            self.sink.record(
                ReplayRecordKind.PLAN_INVALIDATED,
                at=at,
                reason=InvalidationReason.PLAN_EXPIRED.value,
                plan_sequence=plan.plan_sequence,
                generation_id=request.generation_id,
            )
            self._invalidate(InvalidationReason.PLAN_EXPIRED, at)
            return

        # The anchor interval must still be entirely ahead of the acceptance
        # instant.  ``_build_horizon`` only hands the planner strict-future
        # intervals (``start >= requested_at``), but simulated time can advance
        # past the anchor start before the result lands.  An already-started
        # interval is partially elapsed: its ``target_power_w`` was sized for the
        # full interval, so it under-delivers over the sliver that remains.
        # Reject it and queue a re-solve so the runtime replans from *now*
        # instead of going without a plan until the next trigger.
        if plan.intents and plan.intents[0].start < at:
            self.sink.record(
                ReplayRecordKind.SOLVE_FAILED,
                at=at,
                plan_sequence=plan.plan_sequence,
                error=(
                    "first intent already started at acceptance time: "
                    f"start {plan.intents[0].start.isoformat()} < now {at.isoformat()}"
                ),
                generation_id=request.generation_id,
                first_start=plan.intents[0].start.isoformat(),
                acceptance_at=at.isoformat(),
                partially_elapsed=True,
                replan_queued=True,
            )
            self._invalidate(InvalidationReason.INVALID_PLAN, at)
            self._state.pending_trigger = TriggerKind.COALESCED
            return

        self._state.active_plan = plan
        self._state.plan_sequence = plan.next_plan_sequence
        self._state.invalidation_reason = None

        self.sink.record(
            ReplayRecordKind.SOLVE_COMPLETED,
            at=at,
            plan_sequence=plan.plan_sequence,
            n_intents=plan.n,
            content_hash=plan.content_hash,
            generation_id=request.generation_id,
        )
        self.sink.record(
            ReplayRecordKind.PLAN_ACCEPTED,
            at=at,
            plan_sequence=plan.plan_sequence,
            expires_at=plan.expires_at.isoformat(),
            horizon_start=plan.horizon_start.isoformat(),
            horizon_end=plan.horizon_end.isoformat(),
            generation_id=request.generation_id,
        )

    def _invalidate(self, reason: InvalidationReason, at: datetime) -> None:
        """Invalidate the active plan.

        Also fences the in-flight generation, so a solve that finishes after a
        disconnect/restart/timeout can never install a plan: the request
        identity it carries no longer matches ``pending_solve``.
        """
        if self._state.active_plan is not None:
            self.sink.record(
                ReplayRecordKind.PLAN_INVALIDATED,
                at=at,
                reason=reason.value,
                plan_sequence=self._state.active_plan.plan_sequence,
            )
        self._state.active_plan = None
        self._state.invalidation_reason = reason
        if reason in (
            InvalidationReason.DISCONNECT,
            InvalidationReason.RESTART,
            InvalidationReason.SOLVER_TIMEOUT,
            InvalidationReason.SOLVER_ERROR,
            InvalidationReason.INVALID_PLAN,
        ):
            self._state.pending_solve = None

    async def _wait_for_worker(self) -> None:
        """Best-effort bounded wait for the worker to finish an in-progress solve.

        A timeout here does **not** mean the planner thread ended — a running
        thread cannot be killed.  Callers must therefore never read this returning
        as permission to start another solve or to release worker occupancy; the
        authoritative release is :meth:`_on_worker_done`, and :meth:`stop` follows
        this wait with ``shutdown(wait=True)``, which does join the thread.
        """
        try:
            await asyncio.wait_for(self._solve_done_event.wait(), timeout=self._solve_timeout_s + 5)
        except TimeoutError:
            # The worker may still be running; occupancy stays as recorded.
            pass

    def _build_horizon(
        self, prices: PricePublication, at: datetime
    ) -> tuple[Horizon, list[datetime]]:
        """Build a Horizon from available prices.

        Only includes complete FUTURE intervals: start >= decision time
        rounded to the next interval boundary.  The planner receives
        interval_starts matching the sliced horizon, not the unsliced
        original.
        """
        # Round up to next interval boundary
        # Find the first interval whose start >= at
        valid_indices = []
        valid_starts = []
        for i, start in enumerate(prices.interval_starts):
            if start >= at:
                valid_indices.append(i)
                valid_starts.append(start)

        if not valid_indices:
            raise ReplayRuntimeError(
                f"no future price intervals available at {at}: "
                f"price range [{prices.valid_from}, {prices.valid_to})"
            )

        filtered_prices = np.array([prices.prices[i] for i in valid_indices])
        n = len(filtered_prices)

        horizon = Horizon(
            interval_seconds=prices.interval_seconds,
            prices=filtered_prices,
            load_kwh=np.zeros(n),  # Placeholder — overridden by forecast
            usable=np.ones(n, dtype=bool),
        )

        return horizon, valid_starts

    def _is_telemetry_stale(self, at: datetime) -> bool:
        """Check if the latest telemetry is stale."""
        if self._state.latest_telemetry is None:
            return True
        age = (at - self._state.latest_telemetry.at).total_seconds()
        return age > self._telemetry_freshness_s

    def _is_price_stale(self, at: datetime) -> bool:
        """Check if the latest prices are stale.

        Prices are stale when their covered window has ended (``valid_to`` has
        passed) or when they are older than ``price_freshness_s`` relative to
        their publication instant — so a long-dated but never-refreshed curve
        still expires rather than being treated as authoritative forever.
        """
        if self._state.latest_prices is None:
            return True
        if at >= self._state.latest_prices.valid_to:
            return True
        age = (at - self._state.latest_prices.published_at).total_seconds()
        return age > self._price_freshness_s

    def _check_telemetry_freshness(self, at: datetime) -> None:
        """Check and invalidate if telemetry is stale."""
        if self._state.active_plan is not None and self._is_telemetry_stale(at):
            self._invalidate(InvalidationReason.STALE_TELEMETRY, at)

    def _check_price_freshness(self, at: datetime) -> None:
        """Check and invalidate if prices are stale."""
        if self._state.active_plan is not None and self._is_price_stale(at):
            self._invalidate(InvalidationReason.STALE_PRICES, at)
