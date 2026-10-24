"""Dispatch intent contract — the seam between the planner and the device writer.

ADR-0001 §"Logical architecture" items 3–6 and §"Optimization formulation" draw
the line this module implements: *"The economic planner emits **requested**
setpoints.  Actual output is capped by live load, grid meter feedback, hardware
protections and command acknowledgement.  A 15-minute plan is not an
authorization to inject into the grid for 15 minutes."*

So everything here is a **request**, expressed as values, with no I/O:

* :class:`DispatchIntent` — one interval's intent (start/end UTC, mode, target
  power W, SOC target, expected buy price, confidence, generation timestamp,
  expiry, sequence ID).
* :class:`DispatchPlan` — an ordered, gapless sequence of intents plus the
  plan-level identity a consumer needs to reject it without trusting the
  producer (generation instant, strictly increasing plan sequence, absolute
  expiry, content hash).
* :class:`CommandPolicy` — the shape of the TODO-6.3 writer knobs (bounded
  retries, minimum setpoint change, rate limit, safe-idle lease).  Data only:
  this module never writes to a device and owns no writer.
* :func:`intents_from_schedule` — the bridge from the plans the offline pipeline
  already produces (:mod:`energy_baseline.policies`,
  :mod:`energy_baseline.rolling_horizon`) to contract intents.

Design decisions that are load-bearing
--------------------------------------
1. **The physics is not re-implemented here.**  Power limits, the SOC window,
   one-binary-mode-per-interval and the import-ceiling headroom rule are checked
   by :func:`energy_baseline.battery.check_feasible` on the flows each intent was
   derived from, using the caller's :class:`~energy_baseline.battery.BatterySpec`.
   The **import ceiling constrains charging only**: net import above it because
   the *measured load* already exceeds it is an observation
   (:func:`energy_baseline.battery.ceiling_exceedance_indices`,
   :attr:`~energy_baseline.battery.Schedule.ceiling_exceeding_intervals`), never a
   rejection.  That framing comes from ``battery.py`` invariants 6 and 7 and is
   deliberately not tightened here — the ceiling constrains a decision we make,
   not history we measured.

2. **A plan is a request.**  ``expected_buy_price_eur_per_kwh`` and ``confidence``
   are *planner beliefs*, and both are optional: an absent value (``None``) is
   distinguishable from a believed or published ``0.0``.  Nothing here asserts
   that the battery did, will, or may do what the plan says.

3. **Expiry and sequence are safety fields.**  ``expires_at`` is an absolute
   aware-UTC instant rather than a TTL, so a consumer can reject on expiry alone
   without trusting anything the producer said.  Every intent's ``sequence_id``
   equals its position in the plan (so a gap, overlap or replay is
   unrepresentable), and every plan carries a strictly increasing
   ``plan_sequence`` that :meth:`DispatchPlan.accepts_successor` enforces.

4. **No wall clock.**  Nothing in this module calls ``datetime.now()``,
   ``utcnow()`` or ``time.time()``.  The caller supplies ``generated_at``; in
   offline replay that is the *simulated decision instant*, so re-running a
   replay produces byte-identical plans and therefore stable content hashes for a
   no-clobber store.  (Same reason :mod:`energy_baseline.policies` refuses to
   record solver wall-clock time.)

5. **Canonical JSON + SHA-256**, the convention :mod:`energy_baseline.manifest`
   already uses: ``json.dumps(..., sort_keys=True)`` hashed over its UTF-8 bytes.
   Compact separators rather than ``indent=2``, because these bytes are hashed
   content and not a human-facing report, and **no** ``default=str``, so an
   unserializable value raises instead of being quietly stringified into a hash.

6. **Interval discipline.**  All instants are aware UTC, intervals are half-open
   ``[start, end)``, ``interval_seconds`` is explicit and cross-checked against
   ``end - start``, and nothing here assumes how many intervals a local day has:
   a spring-forward day contributes 92 quarter-hours and a fall-back day 100, and
   both are simply "whatever the caller handed over".

Errors
------
Structural problems raise :class:`DispatchError` (a :class:`ValueError`) with a
one-line message, following the precedent of the ``build`` no-clobber collision
and the missing-benchmark-view exit: one clean line, not a traceback.  Physical
infeasibility raises :class:`~energy_baseline.battery.BatteryPhysicsError` from
the shared kernel.  There is no blanket ``except Exception``, and a programmer
error is never swallowed.
"""

from __future__ import annotations

import hashlib
import json
import math
from collections.abc import Mapping, Sequence
from dataclasses import dataclass, replace
from datetime import UTC, datetime, timedelta
from enum import StrEnum
from typing import Any

import numpy as np

from .battery import (
    BatteryPhysicsError,
    BatterySpec,
    Horizon,
    Schedule,
    check_feasible,
    validate_spec,
)

__all__ = [
    "CommandPolicy",
    "DispatchError",
    "DispatchIntent",
    "DispatchMode",
    "DispatchPlan",
    "INTENT_FIELDS",
    "canonical_json",
    "content_hash",
    "dispatch_intents_arrow_schema",
    "intents_from_schedule",
    "intents_to_tabular",
    "plan_from_canonical_json",
    "plan_to_canonical_json",
]

#: Slack for comparing two durations in seconds.  Interval lengths are integral
#: on every grid this codebase uses (900 s), so this only absorbs float
#: subtraction noise, never a real mismatch.
_SECONDS_TOL = 1e-6

#: Slack for comparing SOC in kWh against the caller's own trajectory.  Large
#: enough to survive solver denoising (``battery._denoise``, which the oracle
#: applies at 1e-6 kWh per flow and which accumulates over a horizon), and tiny
#: next to a ~5 kWh nameplate: 2e-5 of it.
_SOC_TOL_KWH = 1e-4

#: Slack for treating a requested power as "no request at all".  Sub-milliwatt
#: is solver residue, not a setpoint.
_W_TOL = 1e-9


class DispatchError(ValueError):
    """A dispatch intent or plan is structurally invalid.

    Subclasses :class:`ValueError` so callers can catch either the specific
    contract error or ordinary argument validation — and never a blanket
    ``Exception``.
    """


class DispatchMode(StrEnum):
    """The one binary mode of one interval (battery invariant 4).

    ``CHARGE`` and ``DISCHARGE`` are mutually exclusive **by construction**: an
    intent holds exactly one mode, so "charge and discharge simultaneously"
    cannot be written down, let alone dispatched.  That is the type-level form of
    the negative-price pathology guard in ``battery.py`` invariant 4 — it holds
    even for a caller that never runs the numeric checks.
    """

    IDLE = "idle"
    CHARGE = "charge"
    DISCHARGE = "discharge"


def _coerce_mode(value: DispatchMode | str) -> DispatchMode:
    """Accept the enum or its wire string; reject anything else loudly."""
    if isinstance(value, DispatchMode):
        return value
    try:
        return DispatchMode(value)
    except ValueError as exc:
        choices = "/".join(member.value for member in DispatchMode)
        raise DispatchError(f"mode must be one of {choices}, got {value!r}") from exc


def _aware_utc(value: Any, field: str) -> datetime:
    """Require an aware instant and normalise it to UTC.

    A naive instant is rejected rather than assumed-UTC: assuming would let a
    local-time bug shift every interval of a plan by the UTC offset while still
    looking internally consistent.
    """
    if not isinstance(value, datetime):
        raise DispatchError(f"{field} must be an aware datetime, got {type(value).__name__}")
    if value.tzinfo is None or value.tzinfo.utcoffset(value) is None:
        raise DispatchError(f"{field} must be timezone-aware UTC, got a naive datetime")
    return value.astimezone(UTC)


def _as_instant(value: Any, field: str) -> datetime:
    """An aware instant, given either as a ``datetime`` or as its ISO-8601 text.

    Both spellings occur legitimately: canonical JSON carries text, while the
    columnar form hands back the ``datetime`` objects it was built from.  A naive
    string is still rejected, because ``fromisoformat`` yields a naive datetime and
    :func:`_aware_utc` then refuses it.
    """
    if isinstance(value, str):
        try:
            parsed = datetime.fromisoformat(value)
        except ValueError as exc:
            raise DispatchError(f"{field} is not an ISO-8601 instant: {value!r}") from exc
        return _aware_utc(parsed, field)
    return _aware_utc(value, field)


def _finite(value: Any, field: str) -> float:
    """Coerce to float and reject NaN/inf, which canonical JSON cannot carry."""
    try:
        number = float(value)
    except (TypeError, ValueError) as exc:
        raise DispatchError(f"{field} must be a real number, got {value!r}") from exc
    if not math.isfinite(number):
        raise DispatchError(f"{field} must be finite, got {number}")
    return number


def _non_negative_int(value: Any, field: str) -> int:
    """Reject negatives and ``bool`` (which is an ``int`` in Python, and never meant here)."""
    if isinstance(value, bool) or not isinstance(value, int):
        raise DispatchError(f"{field} must be an int, got {value!r}")
    if value < 0:
        raise DispatchError(f"{field} must be >= 0, got {value}")
    return value


def _mode_from_flows(charge_kwh: float, discharge_kwh: float, tol: float = _W_TOL) -> DispatchMode:
    """Derive one interval's binary mode from its AC-side flows (kWh).

    Simultaneous positive charge and discharge is **rejected, not resolved**: it
    is battery invariant 4, and silently keeping one leg would hide the very
    negative-price pathology that invariant exists to prevent.
    """
    charging = charge_kwh > tol
    discharging = discharge_kwh > tol
    if charging and discharging:
        raise DispatchError(
            f"interval requests both charge ({charge_kwh} kWh) and discharge "
            f"({discharge_kwh} kWh): one binary mode per interval is required"
        )
    if charging:
        return DispatchMode.CHARGE
    if discharging:
        return DispatchMode.DISCHARGE
    return DispatchMode.IDLE


def _power_w(energy_kwh: float, interval_seconds: float) -> float:
    """Average power over an interval (W) from its energy (kWh)."""
    return energy_kwh * 3_600_000.0 / interval_seconds


# ---------------------------------------------------------------------------
# Canonical serialization
# ---------------------------------------------------------------------------


def canonical_json(payload: Mapping[str, Any]) -> str:
    """Deterministic canonical JSON for a dispatch payload.

    Same convention as :mod:`energy_baseline.manifest` (``sort_keys=True``, hash
    over UTF-8 bytes) with two deliberate moves toward strictness: compact
    separators, because these bytes are hashed content rather than a report, and
    no ``default=str``, so an unserializable value raises instead of being
    quietly stringified into a content hash.

    Floats use Python's shortest-round-trip repr, a pure function of the binary
    value, so equal floats always produce equal bytes.  NaN or infinity raise:
    they are not valid JSON, and a "canonical" document that cannot be parsed
    back is not canonical.
    """
    return json.dumps(payload, sort_keys=True, separators=(",", ":"), allow_nan=False)


def content_hash(canonical: str) -> str:
    """SHA-256 hex digest of canonical JSON bytes — the manifest module's hash space."""
    return hashlib.sha256(canonical.encode("utf-8")).hexdigest()


# ---------------------------------------------------------------------------
# The contract's field list, declared once
# ---------------------------------------------------------------------------

#: One interval's intent fields, in declaration order (TODO-4.6's list, plus the
#: plan generation the intent belongs to).  These names are simultaneously the
#: constructor arguments, the canonical JSON keys and the Parquet column names,
#: so the three representations cannot drift apart.
INTENT_FIELDS: tuple[str, ...] = (
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

#: Fields whose absence is a defect.  The two belief fields are excluded: for
#: them, absence is the honest state (see :func:`intents_from_schedule`).
_REQUIRED_INTENT_FIELDS: tuple[str, ...] = tuple(
    name for name in INTENT_FIELDS if name not in ("expected_buy_price_eur_per_kwh", "confidence")
)

#: Optional intent fields, named so a reader of the archive knows ``null`` here
#: means "the producer did not say" and not "zero".
OPTIONAL_INTENT_FIELDS: tuple[str, ...] = ("expected_buy_price_eur_per_kwh", "confidence")


# ---------------------------------------------------------------------------
# One interval's intent
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class DispatchIntent:
    """One interval's **requested** dispatch — a request, never an authorization.

    Fields (TODO-4.6)
    -----------------
    ``start`` / ``end``
        Half-open interval ``[start, end)``, aware UTC, ``end > start``, and
        ``end - start`` must equal ``interval_seconds``.  The duration is stated
        rather than inferred: a DST day changes how many intervals exist, not how
        long each one is.
    ``mode``
        :class:`DispatchMode` — ``idle`` / ``charge`` / ``discharge``.  One
        binary mode per interval (battery invariant 4).
    ``target_power_w``
        Requested average AC-terminal power in Watts, ``>= 0``, and exactly
        ``0`` when the mode is ``idle``.  Direction lives in ``mode``, not in the
        sign of a number, so a consumer cannot misread a negative power as "that
        is our export convention".  It is a *request*: live load, grid-meter
        feedback, BMS protections and acknowledgement all cap it (ADR-0001).
    ``soc_target_frac``
        Requested end-of-interval SOC as a **fraction of nameplate capacity**, in
        ``[0, 1]``.  A fraction rather than kWh so the intent can be read against
        the spec the plan carries.
    ``expected_buy_price_eur_per_kwh`` *(optional)*
        **Planner belief**, pretax EUR/kWh; may be zero or negative because PVPC
        publishes both.  ``None`` means "no price belief", which must stay
        distinguishable from a believed ``0.0``.
    ``confidence`` *(optional)*
        **Planner belief** in ``[0, 1]``, or ``None`` when the producer reports no
        confidence.  Never defaulted to ``1.0``: an invented perfect confidence is
        the least honest number available.
    ``generated_at``
        Aware UTC generation instant, supplied by the caller.  During replay this
        is the simulated decision instant; this module never reads a clock.
    ``expires_at``
        Absolute aware-UTC expiry, strictly after ``generated_at``.  A consumer may
        reject on this field alone — that is why it is an absolute instant and a
        first-class field instead of a TTL the writer re-derives.
    ``sequence_id``
        Position within this plan generation, ``0``-based.  :class:`DispatchPlan`
        forces it to equal the position, so gaps and replays cannot be constructed.
    ``plan_sequence``
        Generation this intent belongs to; ``-1`` means "not bound to a plan yet",
        which no consumer should accept as a command.  Together with
        :attr:`command_key` it is the identity a single writer deduplicates on.

    Deliberately **not** fields: acknowledgements, measured power, SOC readings,
    retry counts or ownership state.  Those are observations a writer makes about
    the physical world; carrying them here would let a plan pretend to hold proof
    that its own request was fulfilled.
    """

    start: datetime
    end: datetime
    interval_seconds: float
    mode: DispatchMode | str
    target_power_w: float
    soc_target_frac: float
    generated_at: datetime
    expires_at: datetime
    sequence_id: int = 0
    plan_sequence: int = -1
    expected_buy_price_eur_per_kwh: float | None = None
    confidence: float | None = None

    def __post_init__(self) -> None:
        start = _aware_utc(self.start, "start")
        end = _aware_utc(self.end, "end")
        generated = _aware_utc(self.generated_at, "generated_at")
        expires = _aware_utc(self.expires_at, "expires_at")

        interval_seconds = _finite(self.interval_seconds, "interval_seconds")
        if interval_seconds <= 0:
            raise DispatchError(f"interval_seconds must be > 0, got {interval_seconds}")
        span = (end - start).total_seconds()
        if span <= 0:
            raise DispatchError(
                f"end ({end.isoformat()}) must be after start ({start.isoformat()})"
            )
        if abs(span - interval_seconds) > _SECONDS_TOL:
            raise DispatchError(
                f"interval_seconds={interval_seconds} disagrees with end-start={span}: a "
                "half-open interval's length is stated, never inferred"
            )

        mode = _coerce_mode(self.mode)
        power_w = _finite(self.target_power_w, "target_power_w")
        if power_w < 0.0:
            raise DispatchError(f"target_power_w must be >= 0, got {power_w}")
        if mode is DispatchMode.IDLE and power_w > _W_TOL:
            raise DispatchError(f"mode=idle requires target_power_w == 0, got {power_w}")
        if mode is not DispatchMode.IDLE and power_w <= _W_TOL:
            raise DispatchError(
                f"mode={mode.value} requires target_power_w > 0: a zero request is idle, and a "
                "charge/discharge intent that commands nothing would be counted as work done"
            )

        soc_target = _finite(self.soc_target_frac, "soc_target_frac")
        if not 0.0 <= soc_target <= 1.0:
            raise DispatchError(f"soc_target_frac must be in [0, 1], got {soc_target}")

        price = self.expected_buy_price_eur_per_kwh
        if price is not None:
            price = _finite(price, "expected_buy_price_eur_per_kwh")

        confidence = self.confidence
        if confidence is not None:
            confidence = _finite(confidence, "confidence")
            if not 0.0 <= confidence <= 1.0:
                raise DispatchError(f"confidence must be in [0, 1] or absent, got {confidence}")

        sequence_id = _non_negative_int(self.sequence_id, "sequence_id")
        plan_sequence = self.plan_sequence
        if isinstance(plan_sequence, bool) or not isinstance(plan_sequence, int):
            raise DispatchError(f"plan_sequence must be an int, got {plan_sequence!r}")
        if plan_sequence < -1:
            raise DispatchError(
                f"plan_sequence must be >= 0, or -1 when bound to no plan, got {plan_sequence}"
            )

        if expires <= generated:
            raise DispatchError(
                f"expires_at ({expires.isoformat()}) must be after "
                f"generated_at ({generated.isoformat()})"
            )

        object.__setattr__(self, "start", start)
        object.__setattr__(self, "end", end)
        object.__setattr__(self, "interval_seconds", interval_seconds)
        object.__setattr__(self, "mode", mode)
        object.__setattr__(self, "target_power_w", power_w)
        object.__setattr__(self, "soc_target_frac", soc_target)
        object.__setattr__(self, "generated_at", generated)
        object.__setattr__(self, "expires_at", expires)
        object.__setattr__(self, "sequence_id", sequence_id)
        object.__setattr__(self, "plan_sequence", plan_sequence)
        object.__setattr__(self, "expected_buy_price_eur_per_kwh", price)
        object.__setattr__(self, "confidence", confidence)

    # -- derived views -------------------------------------------------------

    @property
    def command_key(self) -> tuple[int, int]:
        """Idempotency identity of one request: ``(plan_sequence, sequence_id)``.

        A single writer uses this to recognise a replayed or duplicated intent as
        the *same* command rather than a new one — the precondition for keeping
        exactly one command in flight (TODO-6.3).  Derived, never stored, so it
        cannot disagree with the fields it names.  A ``plan_sequence`` of ``-1``
        means unbound and must be refused.
        """
        return (self.plan_sequence, self.sequence_id)

    @property
    def target_energy_kwh(self) -> float:
        """Requested AC energy for the interval (kWh): power x duration.

        The exact quantity :meth:`BatterySpec.interval_energy_limit_kwh` bounds,
        so an intent can be handed back to the physics with no second conversion
        rule to drift from.
        """
        return self.target_power_w * self.interval_seconds / 3_600_000.0

    def is_expired(self, now: datetime) -> bool:
        """Whether a consumer should reject this intent at instant ``now``.

        ``now`` is supplied by the caller — this module reads no clock — and the
        decision needs nothing but ``expires_at``: rejecting on expiry alone is
        the point.  An intent whose expiry is exactly ``now`` counts as expired;
        the boundary goes to the safe side.
        """
        return _aware_utc(now, "now") >= self.expires_at

    def safe_idle(self) -> DispatchIntent:
        """The 0 W request that replaces this one when its command lease lapses.

        Everything is preserved — interval, sequence numbers, generation instant
        and validity window — and only the demand changes, to ``idle`` at 0 W.
        Keeping ``expires_at`` untouched is deliberate: this is a *substitute*
        request for the same interval, not an extension of it, and it is what lets
        a whole plan be rewritten as a stop request while still satisfying
        :class:`DispatchPlan`'s rule that the plan expiry equals its earliest
        intent expiry.  When the interval itself is over, the answer is a replan,
        not a longer-lived stop command.

        So TODO-6.3's "safe 0 W lease timeout" needs no field the contract does
        not already define and no clock read: the timeout instant comes from
        :meth:`CommandPolicy.lease_deadline` and the payload comes from here.  And
        it stays a *request* to stop — never a claim that the hardware is at zero
        (ADR-0001: "do not assume an API timeout guarantees the physical output is
        zero"; TODO-6.1 requires the 0 W acknowledgement *and* a measured near-zero
        power before ownership changes).
        """
        return replace(self, mode=DispatchMode.IDLE, target_power_w=0.0)

    def bound_to(self, plan_sequence: int) -> DispatchIntent:
        """A copy stamped as belonging to plan generation ``plan_sequence``."""
        if isinstance(plan_sequence, bool) or not isinstance(plan_sequence, int):
            raise DispatchError(f"plan_sequence must be an int, got {plan_sequence!r}")
        if plan_sequence < 0:
            raise DispatchError(
                f"plan_sequence must be >= 0 to bind an intent, got {plan_sequence}"
            )
        return replace(self, plan_sequence=plan_sequence)

    # -- serialization -------------------------------------------------------

    def to_dict(self) -> dict[str, Any]:
        """Plain-data form; keys are exactly :data:`INTENT_FIELDS`."""
        return {
            "start": self.start.isoformat(),
            "end": self.end.isoformat(),
            "interval_seconds": self.interval_seconds,
            "mode": self.mode.value,
            "target_power_w": self.target_power_w,
            "soc_target_frac": self.soc_target_frac,
            "expected_buy_price_eur_per_kwh": self.expected_buy_price_eur_per_kwh,
            "confidence": self.confidence,
            "generated_at": self.generated_at.isoformat(),
            "expires_at": self.expires_at.isoformat(),
            "sequence_id": self.sequence_id,
            "plan_sequence": self.plan_sequence,
        }

    @classmethod
    def from_dict(cls, data: Mapping[str, Any]) -> DispatchIntent:
        """Rebuild an intent from :meth:`to_dict`.

        Unknown keys are rejected rather than ignored: a silently dropped field is
        how a contract change becomes a silent behaviour change.
        """
        unknown = sorted(set(data) - set(INTENT_FIELDS))
        if unknown:
            raise DispatchError(f"unknown intent field(s): {', '.join(unknown)}")
        missing = sorted(set(_REQUIRED_INTENT_FIELDS) - set(data))
        if missing:
            raise DispatchError(f"missing intent field(s): {', '.join(missing)}")
        return cls(
            start=_as_instant(data["start"], "start"),
            end=_as_instant(data["end"], "end"),
            interval_seconds=data["interval_seconds"],
            mode=data["mode"],
            target_power_w=data["target_power_w"],
            soc_target_frac=data["soc_target_frac"],
            generated_at=_as_instant(data["generated_at"], "generated_at"),
            expires_at=_as_instant(data["expires_at"], "expires_at"),
            sequence_id=data["sequence_id"],
            plan_sequence=data["plan_sequence"],
            expected_buy_price_eur_per_kwh=data.get("expected_buy_price_eur_per_kwh"),
            confidence=data.get("confidence"),
        )


# ---------------------------------------------------------------------------
# A plan: an ordered sequence of intents
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class DispatchPlan:
    """An ordered, gapless sequence of intents from one planning generation.

    A plan is a **request set** and the type keeps it one: it holds no
    acknowledgement, no measured power, no SOC reading, and exposes nothing that
    could be read as "these kWh happened".  What it carries is exactly what a
    consumer needs in order to *refuse* it.

    ``intents``
        Chronological, contiguous half-open intervals whose ``sequence_id``
        equals position (0, 1, 2, …) — so a gap, overlap or replayed intent is
        unrepresentable rather than merely discouraged.
    ``generated_at``
        Aware UTC instant of this generation, supplied by the caller: the
        simulated decision instant during replay, never a clock read.
    ``plan_sequence``
        Strictly increasing across generations from one producer;
        :meth:`accepts_successor` is the check.  ``0`` is a legal first
        generation so that "the first plan" and "no information" stay distinct.
    ``expires_at``
        Absolute expiry of the whole plan, forced equal to the earliest intent
        expiry: once one interval has lapsed the remainder is no longer something
        to act on contiguously, and replanning is cheaper than half-executing.
    ``interval_seconds``
        One uniform grid per plan, cross-checked against every intent.
    ``battery_spec`` *(optional)*
        The spec the request was computed against, so a consumer can confirm it is
        about to command the hardware the plan assumed.  ``None`` means "the
        producer did not say" — never "any battery will do".
    ``horizon_start`` / ``horizon_end``
        Derived from the first and last interval bounds (not constructor inputs),
        so they cannot contradict the intervals they summarize.
    ``content_hash``
        SHA-256 of the canonical JSON of everything above (excluding the hash
        itself).  Content-addressed identity for a no-clobber archive: two plans
        with the same digest are the same request.
    """

    intents: tuple[DispatchIntent, ...]
    generated_at: datetime
    plan_sequence: int
    expires_at: datetime
    interval_seconds: float
    battery_spec: BatterySpec | None = None
    content_hash: str = ""

    def __post_init__(self) -> None:
        generated = _aware_utc(self.generated_at, "generated_at")
        plan_sequence = _non_negative_int(self.plan_sequence, "plan_sequence")
        interval_seconds = _finite(self.interval_seconds, "interval_seconds")
        if interval_seconds <= 0:
            raise DispatchError(f"interval_seconds must be > 0, got {interval_seconds}")

        intents = tuple(self.intents)
        if not intents:
            raise DispatchError("a dispatch plan needs at least one intent")

        for position, intent in enumerate(intents):
            if abs(intent.interval_seconds - interval_seconds) > _SECONDS_TOL:
                raise DispatchError(
                    f"intent {position} spans {intent.interval_seconds} s while the plan's "
                    f"interval_seconds is {interval_seconds}: one plan drives one uniform grid"
                )
            if intent.sequence_id != position:
                raise DispatchError(
                    f"intent at position {position} has sequence_id={intent.sequence_id}: "
                    "sequence numbers must be strictly increasing from 0 with no gaps"
                )
            if intent.generated_at != generated:
                raise DispatchError(
                    f"intent {position} was generated at {intent.generated_at.isoformat()} but "
                    f"this plan is {generated.isoformat()}: one generation, one instant"
                )
            if intent.plan_sequence != plan_sequence:
                raise DispatchError(
                    f"intent {position} is bound to plan_sequence={intent.plan_sequence}, this "
                    f"plan is {plan_sequence}: use DispatchIntent.bound_to({plan_sequence})"
                )

        # Adjacent-pair walk: deliberately not ``strict=True``, because a one-interval
        # plan has no pair at all, and ``zip(intents, intents[1:])`` is the pairing.
        for previous, following in zip(intents, intents[1:]):
            if following.start != previous.end:
                raise DispatchError(
                    f"gap or overlap between sequence_id {previous.sequence_id} and "
                    f"{following.sequence_id}: {previous.end.isoformat()} -> "
                    f"{following.start.isoformat()} must be contiguous half-open intervals"
                )

        expires_at = _aware_utc(self.expires_at, "expires_at")
        earliest = min(intent.expires_at for intent in intents)
        if expires_at != earliest:
            raise DispatchError(
                f"plan expires_at ({expires_at.isoformat()}) must equal the earliest intent "
                f"expiry ({earliest.isoformat()}): an expired interval leaves a sequence that "
                "cannot be executed contiguously, so replanning wins over half-executing"
            )

        # When the producer says which hardware this request is for, the request
        # is checked against that hardware — through ``BatterySpec``'s own derived
        # limits, never a re-derivation here.  Without a spec these two claims are
        # simply uncheckable, which is why the spec is carried and not assumed.
        # The import ceiling is NOT checked here: it bounds charging against the
        # measured load of an interval, and a plan alone has no load.  That check
        # belongs to ``intents_from_schedule``, which has the horizon.
        spec = self.battery_spec
        if spec is not None:
            validate_spec(spec)
            limit_kwh = spec.interval_energy_limit_kwh(interval_seconds)
            # SOC here is a fraction of capacity, so the kWh slack becomes a
            # fraction slack by dividing: the same physical tolerance, one scale.
            tol = _SOC_TOL_KWH / spec.capacity_kwh
            for position, intent in enumerate(intents):
                flowing = intent.mode is not DispatchMode.IDLE
                if flowing and intent.target_energy_kwh > limit_kwh + tol:
                    raise DispatchError(
                        f"intent {position} requests {intent.target_energy_kwh:.6g} kWh, above "
                        f"the {spec.power_kw:.6g} kW nameplate limit of {limit_kwh:.6g} kWh per "
                        f"{interval_seconds:.6g} s interval (battery invariant 3)"
                    )
                if not (
                    spec.soc_min_frac - tol <= intent.soc_target_frac <= spec.soc_max_frac + tol
                ):
                    raise DispatchError(
                        f"intent {position} targets SOC {intent.soc_target_frac:.6g}, outside "
                        f"the spec window [{spec.soc_min_frac:.6g}, {spec.soc_max_frac:.6g}] "
                        "(battery invariant 2)"
                    )

        object.__setattr__(self, "intents", intents)
        object.__setattr__(self, "generated_at", generated)
        object.__setattr__(self, "plan_sequence", plan_sequence)
        object.__setattr__(self, "expires_at", expires_at)
        object.__setattr__(self, "interval_seconds", interval_seconds)

        computed = content_hash(canonical_json(self._payload()))
        if self.content_hash and self.content_hash != computed:
            raise DispatchError(
                f"content_hash mismatch: given {self.content_hash!r}, canonical bytes hash to "
                f"{computed!r}"
            )
        object.__setattr__(self, "content_hash", computed)

    # -- derived views -------------------------------------------------------

    @property
    def n(self) -> int:
        """Number of intents."""
        return len(self.intents)

    @property
    def horizon_start(self) -> datetime:
        """First interval's start (derived — cannot contradict the intents)."""
        return self.intents[0].start

    @property
    def horizon_end(self) -> datetime:
        """Last interval's end (derived — cannot contradict the intents)."""
        return self.intents[-1].end

    @property
    def duration_seconds(self) -> float:
        """Covered span in seconds; equals ``n * interval_seconds`` by contiguity."""
        return (self.horizon_end - self.horizon_start).total_seconds()

    @property
    def next_plan_sequence(self) -> int:
        """The sequence number the same producer must use for its next generation."""
        return self.plan_sequence + 1

    # -- safety affordances (values only: no writer, no I/O) -----------------

    def is_expired(self, now: datetime) -> bool:
        """Reject-on-expiry-alone.  ``now`` comes from the caller; no clock is read."""
        return _aware_utc(now, "now") >= self.expires_at

    def accepts_successor(self, successor: DispatchPlan) -> None:
        """Reject a successor generation that is not strictly newer than this one.

        A writer holding ``self`` calls this before acting on ``successor``, so a
        replayed or out-of-order plan is refused from its header alone.  Sequence
        monotonicity is checked independently of time: an unexpired but stale
        generation is still refused, because acting on it would mean executing two
        planners' intentions over one interval.

        Raises:
            DispatchError: If *successor* is not a plan or its sequence does not
                strictly exceed this plan's.
        """
        if not isinstance(successor, DispatchPlan):
            raise DispatchError(f"successor must be a DispatchPlan, got {type(successor).__name__}")
        if successor.plan_sequence <= self.plan_sequence:
            raise DispatchError(
                f"successor plan_sequence={successor.plan_sequence} does not exceed in-flight "
                f"plan_sequence={self.plan_sequence}: refusing a replayed or out-of-order "
                "generation"
            )

    def actionable_intents(self, now: datetime) -> list[DispatchIntent]:
        """Intervals that may still be acted on at ``now`` — unexpired ones only.

        The writer dispatches these and no others, so a late-arriving plan cannot
        issue a request for an interval that has already run.
        """
        cutoff = _aware_utc(now, "now")
        return [
            intent
            for intent in self.intents
            if not intent.is_expired(cutoff) and intent.end > cutoff
        ]

    def safe_idle_plan(self) -> DispatchPlan:
        """The all-0 W plan that replaces this one (TODO-6.3's lease timeout).

        Same intervals, same generation number, same validity window, every intent
        idle at 0 W — so the stop request needs no field the contract does not
        already define, which is what makes "safe idle" reachable from any plan
        without inventing data.  It is a new plan object with its own digest (the
        content differs), and it remains a *request*: confirming that the hardware
        actually stopped is TODO-6.1's job, measured against telemetry, never
        implied by constructing this object.
        """
        return replace(
            self,
            intents=tuple(intent.safe_idle() for intent in self.intents),
            content_hash="",
        )

    # -- serialization -------------------------------------------------------

    def _payload(self) -> dict[str, Any]:
        """The exact mapping that is canonicalized and hashed.

        ``content_hash`` is excluded (a value cannot contain its own digest) and
        so are the derived horizon bounds, which are restatements of the first and
        last interval and would give one fact two chances to disagree in the hash.
        """
        return {
            "intents": [intent.to_dict() for intent in self.intents],
            "generated_at": self.generated_at.isoformat(),
            "plan_sequence": self.plan_sequence,
            "expires_at": self.expires_at.isoformat(),
            "interval_seconds": self.interval_seconds,
            "battery_spec": None if self.battery_spec is None else _spec_to_dict(self.battery_spec),
        }

    def to_dict(self) -> dict[str, Any]:
        """Full canonical mapping, including derived views and the digest."""
        payload = self._payload()
        payload["horizon_start"] = self.horizon_start.isoformat()
        payload["horizon_end"] = self.horizon_end.isoformat()
        payload["content_hash"] = self.content_hash
        return payload

    @classmethod
    def from_dict(cls, data: Mapping[str, Any]) -> DispatchPlan:
        """Rebuild a plan from :meth:`to_dict` and verify its digest.

        A stored ``content_hash`` that does not match the canonical bytes is
        fatal: a plan that cannot prove what it is must never reach a writer.
        """
        if "intents" not in data:
            raise DispatchError("plan payload has no 'intents'")
        intents = tuple(DispatchIntent.from_dict(item) for item in data["intents"])
        raw_spec = data.get("battery_spec")
        spec = None if raw_spec is None else _spec_from_dict(raw_spec)
        return cls(
            intents=intents,
            generated_at=_as_instant(data["generated_at"], "generated_at"),
            plan_sequence=data["plan_sequence"],
            expires_at=_as_instant(data["expires_at"], "expires_at"),
            interval_seconds=data["interval_seconds"],
            battery_spec=spec,
            content_hash=data.get("content_hash") or "",
        )


#: Battery spec fields as the plan serializes them.  Declared here rather than
#: borrowed from ``battery.py`` so a spec field added later shows up as a loud
#: "missing battery_spec field(s)" instead of silently defaulting.
_SPEC_FIELDS: tuple[str, ...] = (
    "capacity_kwh",
    "power_kw",
    "charge_efficiency",
    "discharge_efficiency",
    "soc_min_frac",
    "soc_max_frac",
    "initial_soc_frac",
    "terminal_soc_frac",
)


def _spec_to_dict(spec: BatterySpec) -> dict[str, Any]:
    """Battery spec as plain data (the plan's own copy; ``battery.py`` owns none)."""
    return {name: getattr(spec, name) for name in _SPEC_FIELDS}


def _spec_from_dict(data: Mapping[str, Any]) -> BatterySpec:
    unknown = sorted(set(data) - set(_SPEC_FIELDS))
    if unknown:
        raise DispatchError(f"unknown battery_spec field(s): {', '.join(unknown)}")
    missing = sorted(set(_SPEC_FIELDS) - set(data))
    if missing:
        raise DispatchError(f"missing battery_spec field(s): {', '.join(missing)}")
    return BatterySpec(**{name: data[name] for name in _SPEC_FIELDS})


def plan_to_canonical_json(plan: DispatchPlan) -> str:
    """Serialize a plan to canonical JSON — the bytes its ``content_hash`` covers."""
    return canonical_json(plan.to_dict())


def plan_from_canonical_json(text: str) -> DispatchPlan:
    """Parse canonical JSON back into a plan, verifying the digest.

    Raises:
        DispatchError: Invalid JSON, a missing or unknown field, a value that
            violates the contract, or a stored ``content_hash`` that does not
            match the canonical bytes — the one-line "this was edited" signal.
    """
    try:
        payload = json.loads(text)
    except json.JSONDecodeError as exc:
        raise DispatchError(
            f"dispatch plan is not valid JSON: {exc.msg} at offset {exc.pos}"
        ) from exc
    if not isinstance(payload, dict):
        raise DispatchError(f"dispatch plan must be a JSON object, got {type(payload).__name__}")
    return DispatchPlan.from_dict(payload)


# ---------------------------------------------------------------------------
# Writer policy knobs (TODO-6.3): validated data, no writer, no I/O
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class CommandPolicy:
    """The TODO-6.3 single-writer parameters, as a value the contract can carry.

    Deliberately **not** a writer: it performs no I/O, reads no clock and decides
    nothing about a device.  It exists so planner, writer and tests share one
    validated shape for knobs that would otherwise be loose constants, and so the
    "capable of supporting one-in-flight and bounded retries" requirement is
    something a test can check rather than assert.

    No default instance is provided on purpose.  Every value is a live-integration
    decision gated by TODO-5.2/6.3, not a measurement this repository holds, and
    inventing defaults would put unmeasured safety numbers into a validated type.

    ``max_attempts``
        Total sends per intent **including the first**, ``>= 1``.  TODO-6.3's
        "bounded retries" — and its "do not endlessly retry".
    ``min_setpoint_change_w``
        Smallest requested-power delta worth sending (anti-thrash).
    ``min_command_interval_seconds``
        Shortest spacing between two commands to the same device (rate limit).
    ``lease_seconds``
        How long an unacknowledged command may stay in flight before the writer
        must fall back to safe idle.  Bounded by the contract, because acting
        past the plan's own expiry is exactly what ADR-0001 forbids.
    ``allow_concurrent_in_flight``
        Always ``False``: TODO-5.2 admits one writer and TODO-6.3 one in-flight
        command.  The field exists so "two commands at once" is a *rejected*
        configuration rather than an unconsidered one.
    """

    max_attempts: int
    min_setpoint_change_w: float
    min_command_interval_seconds: float
    lease_seconds: float
    allow_concurrent_in_flight: bool = False

    def __post_init__(self) -> None:
        if isinstance(self.max_attempts, bool) or not isinstance(self.max_attempts, int):
            raise DispatchError(f"max_attempts must be an int, got {self.max_attempts!r}")
        if self.max_attempts < 1:
            raise DispatchError(
                f"max_attempts must be >= 1 (the first send plus retries), got {self.max_attempts}"
            )
        change_w = _finite(self.min_setpoint_change_w, "min_setpoint_change_w")
        if change_w < 0:
            raise DispatchError(f"min_setpoint_change_w must be >= 0, got {change_w}")
        spacing = _finite(self.min_command_interval_seconds, "min_command_interval_seconds")
        if spacing < 0:
            raise DispatchError(f"min_command_interval_seconds must be >= 0, got {spacing}")
        lease = _finite(self.lease_seconds, "lease_seconds")
        if lease <= 0:
            raise DispatchError(f"lease_seconds must be > 0, got {lease}")
        if self.allow_concurrent_in_flight:
            raise DispatchError(
                "allow_concurrent_in_flight must stay False: one writer, one command in flight"
            )
        object.__setattr__(self, "min_setpoint_change_w", change_w)
        object.__setattr__(self, "min_command_interval_seconds", spacing)
        object.__setattr__(self, "lease_seconds", lease)

    def attempt_allowed(self, attempt_index: int) -> bool:
        """Whether send ``attempt_index`` (0-based = the first send) may go out."""
        if isinstance(attempt_index, bool) or not isinstance(attempt_index, int):
            raise DispatchError(f"attempt_index must be an int, got {attempt_index!r}")
        return 0 <= attempt_index < self.max_attempts

    def remaining_attempts(self, attempt_index: int) -> int:
        """Sends left after ``attempt_index`` has been used.  Never negative."""
        if not self.attempt_allowed(attempt_index):
            raise DispatchError(
                f"attempt_index={attempt_index} is outside the retry budget of "
                f"{self.max_attempts} send(s)"
            )
        return self.max_attempts - attempt_index - 1

    def change_is_worth_sending(self, current_power_w: float, requested_power_w: float) -> bool:
        """Whether a setpoint change clears ``min_setpoint_change_w``."""
        delta = abs(
            _finite(requested_power_w, "requested_power_w")
            - _finite(current_power_w, "current_power_w")
        )
        return delta >= self.min_setpoint_change_w

    def lease_deadline(self, sent_at: datetime) -> datetime:
        """The instant at which an unacknowledged command must be replaced by safe idle."""
        return _aware_utc(sent_at, "sent_at") + timedelta(seconds=self.lease_seconds)


# ---------------------------------------------------------------------------
# Tabular (Parquet-shaped) form
# ---------------------------------------------------------------------------


def intents_to_tabular(intents: Sequence[DispatchIntent]) -> dict[str, list[Any]]:
    """Columnar form of intents, shaped for a Parquet write.

    Column names are exactly :data:`INTENT_FIELDS`, so the JSON and tabular views
    of an intent cannot disagree about what its fields are called.  ``mode`` is
    its wire string (the store's other categorical columns are strings too) and
    the two belief columns hold ``None``, i.e. a Parquet null, when absent — an
    absent confidence stays visibly absent in the archive.

    JSON remains canonical.  This form exists so plans can be archived next to
    schedules; it is not a second identity for a plan (see
    :func:`dispatch_intents_arrow_schema`).
    """
    columns: dict[str, list[Any]] = {name: [] for name in INTENT_FIELDS}
    for position, intent in enumerate(intents):
        if intent.sequence_id != position:
            raise DispatchError(
                f"intent at row {position} has sequence_id={intent.sequence_id}: a tabular "
                "archive would silently reorder the contract"
            )
        payload = intent.to_dict()
        for name in INTENT_FIELDS:
            columns[name].append(payload[name])
    return columns


def dispatch_intents_arrow_schema() -> Any:
    """PyArrow schema matching :func:`intents_to_tabular` (pyarrow imported lazily).

    Timestamps are ``timestamp("us", tz="UTC")`` as everywhere else in this store.
    The belief columns are nullable; nothing else is, because the contract has no
    legitimate way to omit them.

    Returned rather than registered in :mod:`energy_baseline.schemas` on purpose:
    registering a schema implies a writer that TODO-4.6 does not include, and this
    module can then own its own shape without touching shared files.
    """
    import pyarrow as pa

    return pa.schema(
        [
            pa.field("start", pa.timestamp("us", tz="UTC"), nullable=False),
            pa.field("end", pa.timestamp("us", tz="UTC"), nullable=False),
            pa.field("interval_seconds", pa.float64(), nullable=False),
            pa.field("mode", pa.string(), nullable=False),
            pa.field("target_power_w", pa.float64(), nullable=False),
            pa.field("soc_target_frac", pa.float64(), nullable=False),
            pa.field("expected_buy_price_eur_per_kwh", pa.float64(), nullable=True),
            pa.field("confidence", pa.float64(), nullable=True),
            pa.field("generated_at", pa.timestamp("us", tz="UTC"), nullable=False),
            pa.field("expires_at", pa.timestamp("us", tz="UTC"), nullable=False),
            pa.field("sequence_id", pa.int64(), nullable=False),
            pa.field("plan_sequence", pa.int64(), nullable=False),
        ]
    )


# ---------------------------------------------------------------------------
# Bridge from the plans this pipeline already produces
# ---------------------------------------------------------------------------


def intents_from_schedule(
    schedule: Schedule,
    horizon: Horizon,
    *,
    battery_spec: BatterySpec,
    interval_starts_utc: Sequence[datetime],
    generated_at: datetime,
    expires_at: datetime,
    plan_sequence: int = 0,
    expected_buy_prices: Sequence[float] | None = None,
    confidence: Sequence[float | None] | None = None,
) -> DispatchPlan:
    """Turn an existing per-interval plan into contract intents.

    The input shape is what :mod:`energy_baseline.policies` and
    :mod:`energy_baseline.rolling_horizon` actually produce: a
    :class:`~energy_baseline.battery.Schedule` whose ``charge_kwh`` /
    ``discharge_kwh`` are AC-side kWh per interval, over the
    :class:`~energy_baseline.battery.Horizon` it was traced against, plus each
    interval's UTC start (the analysis grid's ``interval_start``).  Modes come
    from which leg is positive, powers from energy over the interval length, and
    SOC targets from the schedule's own kWh trajectory divided by nameplate
    capacity.

    What the existing plans lack, and how that absence is shown
    ----------------------------------------------------------
    * ``confidence`` — no policy or oracle reports one.  Omit the argument and
      every intent gets ``confidence=None``; pass a per-interval sequence and any
      element may itself be ``None`` (e.g. "price was published, load forecast
      fell back to the pooled mean").  Never defaulted to ``1.0``.
    * ``expected_buy_price_eur_per_kwh`` — the *planning* prices are not on a
      :class:`~energy_baseline.battery.Schedule`:
      :func:`energy_baseline.policies.policy_day_ahead` scores against measured
      prices and keeps its noisy planning curve in separate metadata, and
      :func:`energy_baseline.rolling_horizon.run_rolling_horizon` does the same.
      So this is opt-in: hand in the forecast array the solve actually used and it
      is recorded as a belief; omit it and every intent carries ``None``.  Passing
      ``horizon.prices`` is legitimate but is then labelled by *your* call — this
      function never launders a measured price into a belief field by itself.

    Validation is delegated, not duplicated
    ---------------------------------------
    The whole plan is re-checked by :func:`energy_baseline.battery.check_feasible`
    with ``enforce_terminal_soc=False``: an emitted intent sequence is an open
    cycle whose endpoint the next replan owns, exactly as the causal policy and
    both rolling variants already assume.  Per-interval SOC bounds are additionally
    checked against :meth:`BatterySpec.soc_bound_kwh`, because SOC — not the flows
    — is what the contract publishes, so a target outside the window must be caught
    even though no single flow violates a bound.

    The import ceiling shows up here **only** as charging headroom, via the
    kernel's own rule.  Net import above the ceiling is never a rejection:
    :attr:`Schedule.ceiling_exceeding_intervals` counts those intervals and a plan
    is still produced for them, because the ceiling constrains a decision we make
    and not history we measured (``battery.py`` invariants 6/7).

    Args:
        schedule: The plan to convert; its arrays must match *horizon*.
        horizon: The horizon the plan was traced against (supplies
            ``interval_seconds``, the usable mask and the import ceiling).
        battery_spec: The spec **this schedule was produced with**.  For a sliced
            window, re-anchor it the way ``rolling_horizon.py`` does —
            ``dataclasses.replace(spec, initial_soc_frac=<SOC at the slice edge>)``
            — otherwise the kernel's SOC recurrence starts from another state and
            the mismatch is reported rather than absorbed.
        interval_starts_utc: Aware UTC start of each interval, same length.
        generated_at: The decision instant; simulated time during replay.
        expires_at: Absolute expiry for every intent (and therefore for the plan).
            Must be after ``generated_at``.
        plan_sequence: Generation number of the produced plan, ``>= 0``.
        expected_buy_prices: Per-interval planner price beliefs, pretax EUR/kWh.
            ``None`` (default) records no belief for any interval.
        confidence: Per-interval confidences, each in ``[0, 1]`` or ``None``.
            ``None`` (default) means the producer reports no confidence at all.

    Returns:
        A :class:`DispatchPlan` with exactly one intent per interval.

    Raises:
        DispatchError: Structural mismatch (array lengths, naive instants, a
            non-contiguous interval grid, an inconsistent opening SOC, SOC outside
            the spec's window, an out-of-range belief).
        BatteryPhysicsError: The plan violates a battery invariant — power, SOC
            trajectory, one-binary-mode, no-export or charging headroom — as
            reported by the shared kernel.
    """
    validate_spec(battery_spec)
    generated = _aware_utc(generated_at, "generated_at")
    expiry = _aware_utc(expires_at, "expires_at")
    if expiry <= generated:
        raise DispatchError(
            f"expires_at ({expiry.isoformat()}) must be after "
            f"generated_at ({generated.isoformat()})"
        )
    plan_sequence = _non_negative_int(plan_sequence, "plan_sequence")

    charge = np.asarray(schedule.charge_kwh, dtype=float).reshape(-1)
    discharge = np.asarray(schedule.discharge_kwh, dtype=float).reshape(-1)
    soc_kwh = np.asarray(schedule.soc_kwh, dtype=float).reshape(-1)
    n = horizon.n
    if charge.size != n or discharge.size != n:
        raise DispatchError(
            f"schedule carries {charge.size} charge / {discharge.size} discharge intervals, "
            f"the horizon has {n}"
        )
    if soc_kwh.size != n:
        raise DispatchError(f"schedule carries {soc_kwh.size} SOC values, the horizon has {n}")
    if len(interval_starts_utc) != n:
        raise DispatchError(
            f"interval_starts_utc has {len(interval_starts_utc)} entries, the horizon has {n}"
        )

    # The kernel owns power limits, the SOC trajectory, one-binary-mode, no-export
    # and charging headroom.  Nothing below re-derives any of them.
    violations = check_feasible(
        battery_spec, horizon, charge, discharge, enforce_terminal_soc=False
    )
    if violations:
        raise BatteryPhysicsError(
            f"schedule is not dispatchable ({len(violations)} violation class(es)):\n  "
            + "\n  ".join(violations)
        )

    capacity = battery_spec.capacity_kwh
    eta_c = battery_spec.charge_efficiency
    eta_d = battery_spec.discharge_efficiency
    # SOC is end-of-interval, so the interval's opening state is one recurrence step
    # back.  If that does not equal the spec's seed, this is not the spec the plan
    # was solved with — the classic cause being an un-reanchored sliced window.
    opening_kwh = float(soc_kwh[0] - (charge[0] * eta_c - discharge[0] / eta_d))
    seed_kwh = battery_spec.soc_bound_kwh("init")
    if abs(opening_kwh - seed_kwh) > _SOC_TOL_KWH:
        raise DispatchError(
            f"schedule implies an opening SOC of {opening_kwh:.9g} kWh but battery_spec starts "
            f"at {seed_kwh:.9g} kWh: pass the spec this plan was solved with, and for a sliced "
            "window re-anchor initial_soc_frac to the SOC at the slice edge"
        )

    floor_kwh = battery_spec.soc_bound_kwh("min")
    ceil_kwh = battery_spec.soc_bound_kwh("max")
    outside = np.flatnonzero(
        (soc_kwh < floor_kwh - _SOC_TOL_KWH) | (soc_kwh > ceil_kwh + _SOC_TOL_KWH)
    )
    if outside.size:
        first = int(outside[0])
        raise DispatchError(
            f"schedule SOC {float(soc_kwh[first]):.9g} kWh at t={first} is outside the spec "
            f"window [{floor_kwh:.9g}, {ceil_kwh:.9g}] kWh for {outside.size} interval(s)"
        )

    prices: np.ndarray | None = None
    if expected_buy_prices is not None:
        prices = np.asarray(expected_buy_prices, dtype=float).reshape(-1)
        if prices.size != n:
            raise DispatchError(
                f"expected_buy_prices has {prices.size} entries, the horizon has {n}"
            )
    if confidence is not None and len(confidence) != n:
        raise DispatchError(f"confidence has {len(confidence)} entries, the horizon has {n}")

    interval_seconds = horizon.interval_seconds
    step = timedelta(seconds=interval_seconds)
    starts = [
        _aware_utc(start, f"interval_starts_utc[{t}]")
        for t, start in enumerate(interval_starts_utc)
    ]
    # Adjacent-pair walk: a single-interval plan has no pair, so no strict zip.
    for previous, following in zip(starts, starts[1:]):
        if following - previous != step:
            raise DispatchError(
                f"interval grid is not contiguous from {previous.isoformat()} to "
                f"{following.isoformat()}: expected exactly {interval_seconds} s between starts"
            )

    intents: list[DispatchIntent] = []
    for t in range(n):
        charge_kwh = float(charge[t])
        discharge_kwh = float(discharge[t])
        mode = _mode_from_flows(charge_kwh, discharge_kwh)
        flowing_kwh = charge_kwh if mode is DispatchMode.CHARGE else discharge_kwh
        intents.append(
            DispatchIntent(
                start=starts[t],
                end=starts[t] + step,
                interval_seconds=interval_seconds,
                mode=mode,
                target_power_w=0.0
                if mode is DispatchMode.IDLE
                else _power_w(flowing_kwh, interval_seconds),
                soc_target_frac=float(soc_kwh[t]) / capacity,
                generated_at=generated,
                expires_at=expiry,
                sequence_id=t,
                plan_sequence=plan_sequence,
                expected_buy_price_eur_per_kwh=None if prices is None else float(prices[t]),
                confidence=None if confidence is None else confidence[t],
            )
        )

    return DispatchPlan(
        intents=intents,
        generated_at=generated,
        plan_sequence=plan_sequence,
        expires_at=expiry,
        interval_seconds=interval_seconds,
        battery_spec=battery_spec,
    )
