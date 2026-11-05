"""Heat-pump comfort controller.

Meets a comfort schedule with the fewest kWh and ignores price for space heating; hot water stays
price-optimised. Design: home-energy-optimizer docs/planning/comfort-controller-design.md
(sections 2 to 11) and ADR-0006 with its 2026-10-31 amendment. User guide:
docs/user-guide/heat-pump-comfort-controller.md.

Everything is evaluated on a one-minute tick:

1. setpoints, written only at schedule boundaries (early start, window end) and for away mode;
2. the space-heating water temperature (software curve, direct mode only, rate limited);
3. the on/off run logic (room demand with hysteresis and compressor minimum on/off times);
4. the DHW window (cheapest 2 h by price / expected COP, tank target capped at 50 degC);
5. the compressor starts-per-day alarm.

Boost (design section 8) is phase 2 and deliberately not implemented, see BOOST_ENABLED.

Climate optimizer plan (ADR-0008, Proposed): the controller reads the optimizer's advisory plan
(an HTTP GET of its in-cluster service) and compares it with what the rules do. The plan reaches
the heat pump only if the owner's select says `live` AND `optimizer_live_allowed` is true in
apps.yaml; that lock is false in git, so today every plan is telemetry only (shadow).
"""

import json
import math
import re
from collections import deque
from dataclasses import dataclass
from datetime import date, datetime, time, timedelta, timezone

import appdaemon.plugins.hass.hassapi as hass

from utils import escape_markdownv2, send_notification

# Machine limits stay in code, not in helpers (design section 11).
MIN_ON = timedelta(minutes=30)
MIN_OFF = timedelta(minutes=20)
MAX_STARTS_PER_DAY = 12  # compressor starts per local day before the alarm notification
WATER_MIN = 20  # degC, direct-mode water request clamp
WATER_MAX = 35
WATER_MIN_CHANGE_K = 1.0
WATER_MIN_INTERVAL = timedelta(minutes=15)
DIRECT_HEATING_MODE = 1  # sensor.aquarea_heating_mode: 0 curve (request = shift), 1 direct
TANK_MAX = 50  # degC: the heat pump alone reaches it, the 3 kW heater never starts
SETPOINT_STEP = 0.5  # thermostat target resolution

# Optimum-start lead-time bands by outdoor temperature (degC): cold < 8 <= mid < 12 <= mild.
# The lead times themselves are HA helpers (B0 warm-up analysis, 2026-10-31, p75).
LEAD_BAND_EDGES = (8.0, 12.0)
# The band input is the offset-corrected outdoor temperature averaged over the hour before the
# previous window end (21:00-22:00 by default) and frozen for the next window.
BAND_OUTDOOR_MEAN = timedelta(hours=1)

# DHW scheduler. Expected COP is a linear proxy in the outdoor temperature (degC), only used to
# rank hours: about 1.9 at -5, 2.6 at 7 and 3.4 at 20 degC for a 50 degC tank.
DHW_WINDOW_HOURS = 2
DHW_LATEST_END = time(23)  # local: a window never runs into the next day (day rollover, restart)
COP_AT_0C = 2.2
COP_PER_K = 0.06
COP_MIN = 1.5
COP_MAX = 4.5
PLAN_RETRY = timedelta(minutes=10)
MANUAL_FORCE_MAX = timedelta(hours=2)  # a force DHW the app did not set is held this long, then released

# Manual overrides: a suggestion when OVERRIDE_REPEAT similar ones fall within OVERRIDE_WINDOW.
OVERRIDE_WINDOW = timedelta(days=14)
OVERRIDE_REPEAT = 3
OVERRIDE_TIME_TOLERANCE = timedelta(minutes=60)
OVERRIDE_HISTORY_MAX = 200
OWN_WRITE_TTL = timedelta(minutes=5)  # a target change matching our write within this is ours

TICK_SECONDS = 60
MODE_RETRY = timedelta(minutes=2)  # re-send when the Aquarea status has not confirmed the command
DHW_COMMAND_RETRY = timedelta(minutes=2)  # actuator retries, never just helper-state retries
DHW_TARGET_NOTIFY_ATTEMPTS = 3  # unconfirmed tank-target writes before the owner is told

# TODO(phase 2): boost for very cheap hours (design section 8). Built only after normal mode has
# run in production: space-heating soak (room ceiling ~25.5 degC, water ~30-32 degC) started on
# battery SoC ~90-95 %, falling charge power or window end minus soak time; DHW still <= TANK_MAX.
BOOST_ENABLED = False

# Per-room comfort helpers: `floor_<room>` (the always-minimum) and `day_minimum_<room>` (the
# minimum inside the shared day window). Defaults per room; the hall/kitchen is the only room that
# is warmer than its floor in the day window by default.
ROOM_FLOOR_DEFAULT = 21.0
ROOM_DAY_MINIMUM_DEFAULTS = {"hall_kitchen": 23.5, "bedroom": 21.0, "office_a": 21.0, "office_t": 21.0}
ROOM_HELPER_DEFAULTS = {
    **{f"floor_{room}": ROOM_FLOOR_DEFAULT for room in ROOM_DAY_MINIMUM_DEFAULTS},
    **{f"day_minimum_{room}": value for room, value in ROOM_DAY_MINIMUM_DEFAULTS.items()},
}
FLOOR_RANGE = (18.0, 23.0)
DAY_MINIMUM_RANGE = (18.0, 26.0)

# Default helper values, written once when the seeded flag is off (HA creates an input_number at
# its minimum, and `initial:` would reset owner edits at every HA restart).
HELPER_DEFAULTS = {
    **ROOM_HELPER_DEFAULTS,
    "margin": 0.5,
    "weekday_start": "12:00:00",
    "weekend_start": "09:00:00",
    "window_end": "22:00:00",
    "hysteresis": 0.3,
    # B0 warm-up analysis, 2026-10-31, p75 (minutes before the window start).
    "lead_weekday_cold": 675,
    "lead_weekday_mid": 545,
    "lead_weekday_mild": 275,
    "lead_weekend_cold": 530,
    "lead_weekend_mid": 430,
    "lead_weekend_mild": 215,
    "curve_cold_outdoor": -5,
    "curve_cold_water": 32,
    "curve_mild_outdoor": 15,
    "curve_mild_water": 25,
    "early_start_raise": 0,
    "outdoor_offset": 0.0,
    "dhw_target": 49,
}
TIME_HELPERS = ("weekday_start", "weekend_start", "window_end")
# Range of each numeric helper. It is IDENTICAL to the min/max of the input_number in
# config/integrations/appdaemon_comfort.yaml (pinned by a test), so every value HA lets the owner
# set is accepted and anything else (a hand-edited state, garbage) is replaced by the default with
# a warning. A helper that HA re-created at its minimum is a valid value: that case is caught by
# comfort_helpers_seeded going off (see _tick), not by this range.
HELPER_RANGES = {
    **{f"floor_{room}": FLOOR_RANGE for room in ROOM_DAY_MINIMUM_DEFAULTS},
    **{f"day_minimum_{room}": DAY_MINIMUM_RANGE for room in ROOM_DAY_MINIMUM_DEFAULTS},
    "margin": (0.0, 2.0),
    "hysteresis": (0.1, 1.5),
    **{f"lead_{day}_{band}": (0.0, 840.0) for day in ("weekday", "weekend") for band in ("cold", "mid", "mild")},
    "curve_cold_outdoor": (-20.0, 10.0),
    "curve_cold_water": (20.0, 35.0),
    "curve_mild_outdoor": (0.0, 25.0),
    "curve_mild_water": (20.0, 35.0),
    "early_start_raise": (0.0, 5.0),
    "outdoor_offset": (-5.0, 5.0),
    "dhw_target": (40.0, 50.0),
}
# Boolean helpers written while seeding: the water dry run starts on, so the first deployment only
# logs the water writes until the owner has done the first-write test of the go-live checklist.
BOOLEAN_DEFAULTS = {"water_dry_run": True}
BOUNDARY_RETRY = timedelta(minutes=5)  # an unconfirmed boundary setpoint write is repeated after this
BANDS = ("cold", "mid", "mild")

# --- Climate optimizer plan (ADR-0008, ClimatePlan contract v1) ------------------------------
# The plan is advisory. It is read and compared (shadow) and can gate the run decision and replace
# the water target only when plan control is live, see ComfortControl._live_effective.
PLAN_MODES = ("off", "shadow", "live")  # input_select.climate_optimizer_mode: Rules / shadow / live
PLAN_VERSION = 1
PLAN_STEP = timedelta(minutes=15)
PLAN_MAX_SLOTS = 192
PLAN_MAX_BYTES = 256 * 1024  # a plan is about 5 KB: anything much larger is not a plan
PLAN_FETCH_INTERVAL = timedelta(minutes=2)  # the optimizer replans hourly; expiry is checked every tick
PLAN_RETRY_INTERVAL = timedelta(minutes=1)  # after any failed or unusable answer
PLAN_TIMEOUT_S = 5
PLAN_BURNED_KEEP = 48  # plan ids that were superseded or revoked and are never applied again
PLAN_REQUIRED_KEYS = (
    "version", "advisory", "plan_id", "generated_at", "valid_until", "t0", "code_version", "regime",
    "status", "reason", "model", "inputs", "slots", "requested_mode", "effective_mode", "live_locked",
    "live_lock_reason", "mode_source", "summary",
)
PLAN_ID_RE = re.compile(r"^[0-9a-f]{16}$")
PLAN_INSTANT_RE = re.compile(r"^[0-9]{4}-[0-9]{2}-[0-9]{2}T[0-9]{2}:[0-9]{2}:[0-9]{2}(\.[0-9]+)?\+00:00$")


# --- Pure functions -------------------------------------------------------------------------


def parse_number(value):
    """Return a finite float, or None for anything else (None, unknown, unavailable, garbage)."""
    if value is None or isinstance(value, bool):
        return None
    try:
        number = float(value)
    except (TypeError, ValueError):
        return None
    return number if math.isfinite(number) else None


def parse_time(value):
    if not isinstance(value, str):
        return None
    for fmt in ("%H:%M:%S", "%H:%M"):
        try:
            return datetime.strptime(value, fmt).time()
        except ValueError:
            continue
    return None


def parse_datetime(value):
    if isinstance(value, datetime):
        return value if value.tzinfo else value.replace(tzinfo=timezone.utc)
    if isinstance(value, str):
        try:
            parsed = datetime.fromisoformat(value)
        except ValueError:
            return None
        return parsed if parsed.tzinfo else parsed.replace(tzinfo=timezone.utc)
    return None


def localize(tz, naive):
    """Attach a timezone to a naive local datetime (pytz and zoneinfo)."""
    if hasattr(tz, "localize"):
        return tz.localize(naive)
    return naive.replace(tzinfo=tz)


def round_step(value, step=SETPOINT_STEP):
    return math.floor(value / step + 0.5) * step


def curve_water(outdoor, cold_outdoor, cold_water, mild_outdoor, mild_water):
    """Linear software heating curve, clamped outside its two points. None if the points are invalid."""
    if None in (outdoor, cold_outdoor, cold_water, mild_outdoor, mild_water) or cold_outdoor >= mild_outdoor:
        return None
    if outdoor <= cold_outdoor:
        return float(cold_water)
    if outdoor >= mild_outdoor:
        return float(mild_water)
    fraction = (outdoor - cold_outdoor) / (mild_outdoor - cold_outdoor)
    return cold_water + fraction * (mild_water - cold_water)


def water_request(curve_value, raise_k=0.0):
    """Return (clamped float target, integer request) for the direct-mode Z1 request."""
    target = min(max(curve_value + raise_k, WATER_MIN), WATER_MAX)
    request = int(math.floor(target + 0.5))
    return target, min(max(request, WATER_MIN), WATER_MAX)


def water_write_due(target, request, current, last_write, now):
    """A write is due on a change of at least 1 K (on the unrounded target, so rounding noise
    does not flip-flop) and at most once per WATER_MIN_INTERVAL."""
    if current is None or request == current:
        return False
    if abs(target - current) < WATER_MIN_CHANGE_K:
        return False
    return last_write is None or now - last_write >= WATER_MIN_INTERVAL


def lead_band(outdoor):
    if outdoor < LEAD_BAND_EDGES[0]:
        return "cold"
    if outdoor < LEAD_BAND_EDGES[1]:
        return "mid"
    return "mild"


def is_weekend(day):
    return day.weekday() >= 5


def expected_cop(outdoor):
    return min(max(COP_AT_0C + COP_PER_K * outdoor, COP_MIN), COP_MAX)


def choose_dhw_window(prices, temperatures, allowed_starts):
    """Cheapest DHW window by mean price / expected COP over its hours.

    prices and temperatures map an hour index of the day (the local hour on a 24 h day, the slot
    index of day_hour_slots on DST days) to a value; allowed_starts is an iterable of start
    indexes. A window needs a price for each of its hours. Ties keep the earliest start.
    """
    best = None
    for start in sorted(allowed_starts):
        hours = range(start, start + DHW_WINDOW_HOURS)
        if any(prices.get(h) is None or temperatures.get(h) is None for h in hours):
            continue
        score = sum(prices[h] / expected_cop(temperatures[h]) for h in hours) / DHW_WINDOW_HOURS
        if best is None or score < best[1]:
            best = (start, score)
    return best


def day_hour_slots(tz, day):
    """The hours of a local day in elapsed time, as (start, local hour, PVPC attribute key).

    23 slots on the spring-forward day, 25 on the fall-back day. The keys follow the PVPC
    integration: `price_HHh` by local hour, and `price_HHh_d` for the second occurrence of the
    repeated hour.
    """
    start = localize(tz, datetime.combine(day, time(0))).astimezone(timezone.utc)
    end = localize(tz, datetime.combine(day + timedelta(days=1), time(0))).astimezone(timezone.utc)
    slots, seen = [], set()
    while start < end:
        local_start = start.astimezone(tz)
        key = f"price_{local_start.hour:02d}h" + ("_d" if local_start.hour in seen else "")
        seen.add(local_start.hour)
        slots.append((local_start, local_start.hour, key))
        start += timedelta(hours=1)
    return slots


def similar_overrides(history, room, direction, at):
    """Overrides of the same room and direction within OVERRIDE_WINDOW whose time of day is
    within OVERRIDE_TIME_TOLERANCE of `at` (any day)."""
    minute = at.hour * 60 + at.minute
    tolerance = OVERRIDE_TIME_TOLERANCE.total_seconds() / 60
    matches = []
    for item in history:
        if item["room"] != room or item["direction"] != direction or at - item["at"] > OVERRIDE_WINDOW:
            continue
        other = item["at"].hour * 60 + item["at"].minute
        distance = abs(minute - other)
        if min(distance, 1440 - distance) <= tolerance:
            matches.append(item)
    return matches


# --- Climate plan: pure functions -------------------------------------------------------------


@dataclass(frozen=True)
class ClimatePlan:
    """A ClimatePlan v1 that passed validation (the fields the controller uses)."""

    plan_id: str
    generated_at: datetime
    valid_until: datetime
    start: datetime
    n: int
    heating: tuple
    water_c: tuple


@dataclass(frozen=True)
class PlanSlot:
    index: int
    start: datetime
    heating: bool
    water_c: object  # integer degC 20..35, or None


def _instant(value):
    """A UTC instant of the contract (`...+00:00`), or None."""
    if not isinstance(value, str) or not PLAN_INSTANT_RE.match(value):
        return None
    try:
        return datetime.fromisoformat(value)
    except ValueError:
        return None


def _not_served_reason(status, text):
    """The fallback reason of a non-200 answer; a 404 names the optimizer's own state."""
    if status == 404:
        try:
            state = json.loads(text).get("state")
        except (TypeError, ValueError, AttributeError):
            state = None
        return f"not_served:{state}" if isinstance(state, str) and state else "http_404"
    return f"http_{status}"


def parse_plan(status, text):
    """Validate an answer of GET /climate/plan: (ClimatePlan, None) or (None, reason).

    Mirrors the contract's consumer rules (docs/climate-plan-contract.md in the optimizer repo): a
    failed request or 404, JSON that does not parse, a schema violation, version != 1, advisory !=
    true, status != planned. Unknown fields are ignored. Expiry and slot coverage are checked at
    every use by plan_current, not here.
    """
    if type(status) is not int or status != 200:
        return None, _not_served_reason(status, text)
    try:
        doc = json.loads(text)
    except (TypeError, ValueError, RecursionError):
        return None, "bad_json"
    if not isinstance(doc, dict):
        return None, "bad_json"
    if type(doc.get("version")) is not int or doc["version"] != PLAN_VERSION:
        return None, "version"
    if doc.get("advisory") is not True:
        return None, "advisory"
    if doc.get("status") != "planned":
        return None, "not_planned"
    missing = [key for key in PLAN_REQUIRED_KEYS if key not in doc]
    if missing:
        return None, f"schema:missing {missing[0]}"
    plan_id = doc["plan_id"]
    if not isinstance(plan_id, str) or not PLAN_ID_RE.match(plan_id):
        return None, "schema:plan_id"
    generated_at, valid_until = _instant(doc["generated_at"]), _instant(doc["valid_until"])
    if generated_at is None or valid_until is None:
        return None, "schema:instant"
    slots = doc["slots"]
    if not isinstance(slots, dict):
        return None, "schema:slots"
    start, n = _instant(slots.get("start")), slots.get("n")
    if start is None or slots.get("step_minutes") != 15 or type(slots.get("step_minutes")) is not int:
        return None, "schema:slots.start"
    if type(n) is not int or not 1 <= n <= PLAN_MAX_SLOTS:
        return None, "schema:slots.n"
    heating, water = slots.get("heating"), slots.get("water_c")
    if not isinstance(heating, list) or len(heating) != n or any(type(v) is not int or v not in (0, 1) for v in heating):
        return None, "schema:slots.heating"
    if not isinstance(water, list) or len(water) != n or any(
        v is not None and (type(v) is not int or not WATER_MIN <= v <= WATER_MAX) for v in water
    ):
        return None, "schema:slots.water_c"
    return ClimatePlan(plan_id, generated_at, valid_until, start, n, tuple(heating), tuple(water)), None


def plan_current(plan, now):
    """The slot of `now` in a validated plan: (PlanSlot, None) or (None, reason). Called at every use:
    a plan is absent from valid_until on, and when no slot covers `now` (found by time)."""
    now_utc = now.astimezone(timezone.utc)
    if now_utc >= plan.valid_until:
        return None, "expired"
    offset = now_utc - plan.start
    if offset < timedelta(0) or offset // PLAN_STEP >= plan.n:
        return None, "slot_not_covered"
    index = offset // PLAN_STEP
    return PlanSlot(index, plan.start + index * PLAN_STEP, bool(plan.heating[index]), plan.water_c[index]), None


def run_want(mode_on, cold, missing, satisfied, plan_heating=None, floor_forced=False):
    """What the run logic wants: "on", "off" or "hold" (the compressor timers apply afterwards).

    Rules (plan_heating None): on when a room is cold or unreadable while off, off when every room is
    at its setpoint while on. With a plan, heating 0 holds heating off unless a comfort floor is at
    stake or a room is unreadable; heating 1 changes nothing (it permits, the thermostats decide);
    a room below its floor minus the hysteresis forces heating whatever the plan says.
    """
    if plan_heating is False and not floor_forced and not missing:
        return "off"
    if not mode_on and (cold or missing or floor_forced):
        return "on"
    if mode_on and satisfied and not floor_forced:
        return "off"
    return "hold"


@dataclass(frozen=True)
class Phase:
    kind: str  # "day" (from the early start) or "night" (from the window end)
    day: date  # the day of the window the boundary belongs to
    at: datetime  # when the boundary happened

    @property
    def key(self):
        # Monotonic: night after day D (2D+1) < day D+1 (2D+2).
        return self.day.toordinal() * 2 + (1 if self.kind == "night" else 0)


@dataclass(frozen=True)
class Schedule:
    weekday_start: time
    weekend_start: time
    window_end: time
    tz: object

    def window(self, day):
        start = self.weekend_start if is_weekend(day) else self.weekday_start
        return (
            localize(self.tz, datetime.combine(day, start)),
            localize(self.tz, datetime.combine(day, self.window_end)),
        )

    def early_start(self, day, lead_minutes):
        """The day-window setpoint write: window start minus the lead time (elapsed minutes, so
        DST days are right), never before the previous day's window end. When the lead time is
        longer than the night, the rooms stay at their day setpoints overnight. One early start
        serves every room (the lead times are calibrated for the hall/kitchen)."""
        start, _ = self.window(day)
        _, previous_end = self.window(day - timedelta(days=1))
        early = start.astimezone(timezone.utc) - timedelta(minutes=lead_minutes)
        return max(early, previous_end.astimezone(timezone.utc)).astimezone(self.tz)

    def phase_at(self, now, lead_for):
        """The last boundary at or before `now`. A capped early start coincides with the previous
        window end; the day boundary wins the tie (higher key)."""
        today = now.astimezone(self.tz).date()
        events = []
        for offset in (-2, -1, 0, 1):
            day = today + timedelta(days=offset)
            events.append(Phase("day", day, self.early_start(day, lead_for(day))))
            events.append(Phase("night", day, self.window(day)[1]))
        past = [phase for phase in events if phase.at <= now]
        return max(past, key=lambda phase: (phase.at, phase.key))


# --- The app --------------------------------------------------------------------------------


class ComfortControl(hass.Hass):
    async def initialize(self):
        self.log("Starting")
        self._init_state()
        helpers = self.args["helpers"]
        await self._load_status()
        await self._load_plan_status()
        try:
            await self._seed_helpers()
        except Exception as error:
            self.log(f"Seeding the helpers failed: {error!r}", level="ERROR")

        await self.listen_state(self._on_enable, helpers["enable"], new="on", old="off")
        await self.listen_state(self._on_disable, helpers["enable"], new="off", old="on")
        await self.listen_state(self._on_away, helpers["away"], new="on", old="off")
        await self.listen_state(self._on_away_off, helpers["away"], new="off", old="on")
        await self.listen_state(self._on_seeded_off, helpers["seeded"], new="off", old="on")
        for room, entity in self.args["rooms"].items():
            await self.listen_state(self._on_target_change, entity, attribute="temperature", room=room)
        plan_args = self.args.get("climate_plan") or {}
        if plan_args.get("mode_entity"):
            await self.listen_state(self._on_plan_mode, plan_args["mode_entity"])

        mode_state = await self.get_state(self.args["heat_mode"]["entity"], attribute="all")
        if isinstance(mode_state, dict):
            self.last_mode_change = parse_datetime(mode_state.get("last_changed"))
        await self._startup_checks()
        await self._release_force_dhw_safely(self._now(), startup=True)
        await self.run_every(self._tick, "now", TICK_SECONDS)
        await self.create_task(self._tick({}))

    def _init_state(self):
        self.busy = False
        self.last_phase_key = None
        self.boundary = None  # dict: key, phase, targets {room: value}, written (room -> at)
        self.raise_active = False
        self.pending_writes = {}  # room -> (value, written at)
        self.overrides = []  # dicts: room, at, old, new, direction
        self.suggested = set()
        self.last_mode_change = None
        self.pending_mode = None  # target / at; a helper update is not physical confirmation
        self.mode_warning = None
        self.missing_warning = []
        self.last_water_write = None
        self.water_block_reason = None
        self.outdoor_samples = deque()
        self.band_outdoor = {}  # window day (ISO) -> frozen offset-corrected outdoor degC
        self.dhw_plan = None  # dict: day, start, end, target_written, forced, blocked
        self.dhw_done = None  # ISO date of the last finished DHW window
        self.dhw_last_attempt = None
        self.dhw_target_attempt = None
        self.dhw_target_attempts = 0
        self.dhw_force_attempt = None
        self.dhw_warning = None
        self.force_owned = False  # the app switched force DHW on and has not seen it off since
        self.manual_force_since = None  # when the app first saw a force DHW it did not set
        self.dhw_plan_written = None  # last value written to the DHW plan helper
        self.starts_day = None
        self.starts_baseline = None
        self.starts_alarmed = False
        self.status_published = None
        self._init_plan_state()

    def _init_plan_state(self):
        self.plan = None  # the ClimatePlan of the last successful fetch (dropped on any failure)
        self.plan_fetch_error = None  # reason of the last failed or unusable answer
        self.plan_last_fetch = None
        self.plan_next_fetch = None
        self.plan_requested = "off"  # the owner's select: off / shadow / live
        self.plan_slot = None  # the PlanSlot of now, None when the plan is absent
        self.plan_reason = None  # why there is no slot (the fallback reason)
        self.plan_control = False  # live effective and the plan may drive this tick
        self.plan_blocked = None  # why a live-effective plan does not drive (already applied)
        self.plan_active_id = None
        self.plan_burned = deque(maxlen=PLAN_BURNED_KEEP)  # superseded or revoked ids
        self.plan_live_applied = None  # id applied in live, until revoked or superseded
        self.plan_run = None  # this tick's run comparison (rules vs plan), set by _update_run
        self.plan_water = None  # this tick's water comparison, set by _update_water
        self.plan_today = self._new_plan_counters(None)
        self.plan_counted_minute = None
        self.plan_status_published = None
        self.plan_logged = None
        self.plan_warned = None

    @staticmethod
    def _new_plan_counters(day):
        return {"day": day, "samples": 0, "agree": 0, "heating_disagree": 0, "water_disagree": 0,
                "would_differ": 0, "unusable": 0, "skipped": 0, "plans": 0}

    # --- HA access ------------------------------------------------------------------------

    def _tz(self):
        return self.get_timezone()

    def _now(self):
        return datetime.now(self._tz())

    def _actuate(self, area):
        return bool(self.args.get("actuate", {}).get(area, False))

    async def _number(self, entity):
        return parse_number(await self.get_state(entity))

    async def _helper(self, role):
        """Helper value, or its default (with a warning) when the helper is missing or garbage."""
        raw = await self.get_state(self.args["helpers"][role])
        value = parse_time(raw) if role in TIME_HELPERS else parse_number(raw)
        low, high = HELPER_RANGES.get(role, (-math.inf, math.inf))
        if value is not None and role not in TIME_HELPERS and not low <= value <= high:
            value = None  # outside its sane range, for example re-created at its HA minimum
        if value is None:
            default = HELPER_DEFAULTS[role]
            self.log(f"Helper {role} is {raw!r}: using the default {default}", level="WARNING")
            value = parse_time(default) if role in TIME_HELPERS else default
        return value

    async def _notify(self, message):
        self.log(f"Notify: {message}")
        notify = self.args.get("notify", {})
        if notify.get("enabled"):
            try:
                await send_notification(self, escape_markdownv2(message), notify["entity_id"])
            except Exception as error:  # a notification must never stop the control loop
                self.log(f"Notification failed: {error}", level="ERROR")

    async def _is_on(self, role):
        return await self.get_state(self.args["helpers"][role]) == "on"

    # --- Startup ------------------------------------------------------------------------------

    async def _seed_helpers(self, force=False):
        if not force and await self._is_on("seeded"):
            return
        for role, default in HELPER_DEFAULTS.items():
            entity = self.args["helpers"][role]
            if role in TIME_HELPERS:
                await self.call_service("input_datetime/set_datetime", entity_id=entity, time=default)
            else:
                await self.call_service("input_number/set_value", entity_id=entity, value=default)
        for role, default in BOOLEAN_DEFAULTS.items():
            service = "input_boolean/turn_on" if default else "input_boolean/turn_off"
            await self.call_service(service, entity_id=self.args["helpers"][role])
        await self.call_service("input_boolean/turn_on", entity_id=self.args["helpers"]["seeded"])
        await self._notify("Comfort control: helpers set to their defaults")

    async def _on_seeded_off(self, _entity="", _attribute="", _old="", _new="", _kwargs={}):
        await self._seed_helpers(force=True)

    async def _startup_checks(self):
        for room, entity in self.args["rooms"].items():
            if await self.get_state(entity) == "auto":
                await self._notify(
                    f"Comfort control: thermostat {room} runs its device program (auto); the setpoint "
                    "can change behind the controller. Set it to heat"
                )
        if await self.get_state(self.args["sensors"]["sterilization"]) == "on":
            await self._notify("Comfort control: sterilization is enabled on the unit; it uses the 3 kW heater")
        if not await self._is_on("enable"):
            mode = await self.get_state(self.args["heat_mode"]["entity"])
            if mode == self.args["heat_mode"]["off_mode"]:
                await self._notify(
                    f"Comfort control is disabled and the heat pump is in {mode}: space heating is off "
                    "until the controller is enabled or the mode is changed by hand"
                )

    async def _load_status(self):
        state = await self.get_state(self.args["status_entity"], attribute="all")
        attributes = (state or {}).get("attributes", {}) if isinstance(state, dict) else {}
        key = attributes.get("phase_key")
        self.last_phase_key = key if isinstance(key, int) else None
        self.dhw_done = attributes.get("dhw_done")
        band = attributes.get("band_outdoor")
        self.band_outdoor = dict(band) if isinstance(band, dict) else {}
        for item in attributes.get("overrides") or []:
            at = parse_datetime(item.get("at")) if isinstance(item, dict) else None
            if at is not None:
                self.overrides.append({**item, "at": at})
        self.dhw_plan = self._restore_dhw_plan(attributes.get("dhw_plan"))
        if not attributes:
            # Home Assistant restarted (the status sensor is not restored): the plan helper is.
            await self._load_dhw_plan_helper()

    async def _load_dhw_plan_helper(self):
        entity = self.args["helpers"].get("dhw_plan")
        raw = await self.get_state(entity) if entity else None
        try:
            stored = json.loads(raw) if isinstance(raw, str) else None
        except ValueError:
            stored = None
        if not isinstance(stored, dict):
            return
        self.dhw_plan_written = raw
        self.dhw_plan = self._restore_dhw_plan(stored)
        done = stored.get("done")
        if isinstance(done, str) and done == self._now().date().isoformat():
            self.dhw_done = done

    def _restore_dhw_plan(self, stored):
        """Today's DHW plan from the status sensor, so an AppDaemon restart in the window keeps it
        (one window per day, force DHW released at its end)."""
        if not isinstance(stored, dict):
            return None
        try:
            day = date.fromisoformat(stored["day"])
        except (KeyError, TypeError, ValueError):
            return None
        start, end = parse_datetime(stored.get("start")), parse_datetime(stored.get("end"))
        if day != self._now().date() or (start is None) != (end is None):
            return None
        tz = self._tz()
        return {
            "day": day,
            "start": start.astimezone(tz) if start else None,
            "end": end.astimezone(tz) if end else None,
            "forced": bool(stored.get("forced")),
            "force_requested": bool(stored.get("force_requested")),
            "target_written": bool(stored.get("target_written")),
        }

    async def _publish_status(self, state, attributes):
        if (state, attributes) == self.status_published:
            return
        self.status_published = (state, attributes)
        overrides = [{**item, "at": item["at"].isoformat()} for item in self.overrides]
        await self.set_state(
            self.args["status_entity"],
            state=state,
            attributes={**attributes, "overrides": overrides, "friendly_name": "Comfort control"},
        )

    # --- Schedule -----------------------------------------------------------------------------

    async def _schedule(self):
        times = {role: await self._helper(role) for role in TIME_HELPERS}
        if times["window_end"] <= max(times["weekday_start"], times["weekend_start"]):
            self.log(f"Window end {times['window_end']} is not after the window starts: using defaults", level="WARNING")
            times = {role: parse_time(HELPER_DEFAULTS[role]) for role in TIME_HELPERS}
        return Schedule(times["weekday_start"], times["weekend_start"], times["window_end"], self._tz())

    async def _corrected_outdoor(self):
        outdoor = await self._number(self.args["sensors"]["outdoor"])
        if outdoor is None:
            return None
        return outdoor + await self._helper("outdoor_offset")

    async def _lead_minutes(self, day, live_outdoor):
        day_type = "weekend" if is_weekend(day) else "weekday"
        table = {band: await self._helper(f"lead_{day_type}_{band}") for band in BANDS}
        outdoor = self.band_outdoor.get(day.isoformat(), live_outdoor)
        if outdoor is None:
            return max(table.values())  # no outdoor reading: the most conservative lead time
        return max(table[lead_band(outdoor)], 0)

    async def _current_phase(self, schedule, now, live_outdoor):
        today = now.astimezone(self._tz()).date()
        leads = {}
        for offset in (-2, -1, 0, 1):
            day = today + timedelta(days=offset)
            leads[day] = await self._lead_minutes(day, live_outdoor)
        return schedule.phase_at(now, leads.__getitem__), leads

    def _sample_outdoor(self, now, outdoor):
        if outdoor is not None:
            self.outdoor_samples.append((now, outdoor))
        while self.outdoor_samples and now - self.outdoor_samples[0][0] > BAND_OUTDOOR_MEAN:
            self.outdoor_samples.popleft()

    def _freeze_band_outdoor(self, schedule, now, live_outdoor):
        """Freeze the band input of each window once the previous window has ended."""
        today = now.astimezone(self._tz()).date()
        for day in (today, today + timedelta(days=1)):
            key = day.isoformat()
            previous_end = schedule.window(day - timedelta(days=1))[1]
            if key in self.band_outdoor or now < previous_end:
                continue
            # [end - 1 h, end): after a restart past the window end there are none, so the live
            # reading is used and frozen.
            values = [v for at, v in self.outdoor_samples if previous_end - BAND_OUTDOOR_MEAN <= at < previous_end]
            outdoor = sum(values) / len(values) if values else live_outdoor
            if outdoor is None:
                continue
            self.band_outdoor[key] = round(outdoor, 2)
            self.log(f"Lead-time band input for {key}: {outdoor:.2f} degC ({lead_band(outdoor)})")
        keep = {(today + timedelta(days=offset)).isoformat() for offset in (-1, 0, 1)}
        self.band_outdoor = {k: v for k, v in self.band_outdoor.items() if k in keep}

    async def _room_minimum(self, room, phase_kind):
        """The comfort minimum of a room: its floor, or inside the day window the higher of its
        floor and its day minimum (a day minimum below the floor never lowers the floor)."""
        floor = await self._helper(f"floor_{room}")
        if phase_kind == "day":
            return max(floor, await self._helper(f"day_minimum_{room}"))
        return floor

    async def _schedule_setpoints(self, phase_kind):
        margin = await self._helper("margin")
        return {room: round_step(await self._room_minimum(room, phase_kind) + margin) for room in self.args["rooms"]}

    async def _write_setpoints(self, setpoints, reason):
        """Write the setpoints that differ from the thermostat targets; return the written ones."""
        now = self._now()
        written = {}
        for room, value in setpoints.items():
            entity = self.args["rooms"][room]
            old = await self.get_state(entity, attribute="temperature")
            if parse_number(old) == value:
                continue
            dry_run = "" if self._actuate("setpoints") else " (dry run)"
            self.log(f"Setpoint {room}: {old} -> {value} ({reason}){dry_run}")
            if self._actuate("setpoints"):
                self.pending_writes[room] = (value, now)
                written[room] = value
                await self.call_service("climate/set_temperature", entity_id=entity, temperature=value)
        return written

    async def _apply_phase(self, phase, reason):
        """Write a boundary. The boundary counts as applied (last_phase_key) only once every written
        thermostat reports its new target; until then _confirm_boundary retries the writes."""
        self.raise_active = phase.kind == "day"
        self.boundary = None
        if await self._is_on("away"):
            self.raise_active = False
            self.last_phase_key = phase.key
            self.log(f"Boundary {phase.kind} {phase.day} ({reason}) while away: setpoints unchanged")
            return
        written = await self._write_setpoints(await self._schedule_setpoints(phase.kind), f"{phase.kind} {reason}")
        if written:
            self.boundary = {"key": phase.key, "kind": phase.kind, "targets": written, "at": self._now()}
        else:
            self.last_phase_key = phase.key

    async def _confirm_boundary(self):
        boundary, now = self.boundary, self._now()
        for room, value in list(boundary["targets"].items()):
            target = parse_number(await self.get_state(self.args["rooms"][room], attribute="temperature"))
            if target is not None and abs(target - value) < 0.01:
                del boundary["targets"][room]
        if not boundary["targets"]:
            self.last_phase_key, self.boundary = boundary["key"], None
            return
        if now - boundary["at"] >= BOUNDARY_RETRY:
            rooms = ", ".join(boundary["targets"])
            self.log(f"Boundary setpoints not confirmed after {BOUNDARY_RETRY}: {rooms}; writing again", level="WARNING")
            boundary["at"] = now
            await self._write_setpoints(dict(boundary["targets"]), f"{boundary['kind']} retry")

    # --- Event handlers -----------------------------------------------------------------------

    async def _on_enable(self, _entity="", _attribute="", _old="", _new="", _kwargs={}):
        self.log("Comfort control enabled: writing the current schedule")
        self.last_phase_key = None
        self.boundary = None
        await self._tick({})

    async def _on_disable(self, _entity="", _attribute="", _old="", _new="", _kwargs={}):
        # As the C0 fix: nobody leaves the off mode without the controller, so restore heating.
        on_mode = self.args["heat_mode"]["on_mode"]
        await self._set_mode(on_mode, "controller disabled")
        if self.dhw_plan is not None:
            if self.dhw_plan.get("forced") or self.dhw_plan.get("force_requested"):
                self.force_owned = True  # its own force, even if not yet seen on: release it at once
            self.dhw_plan = {**self.dhw_plan, "forced": False, "force_requested": False}
        await self._release_force_dhw(self._now())  # its own force at once, a manual one at the cap
        await self._notify(f"Comfort control disabled: heating restored to {on_mode}; the mode is now manual")

    async def _on_away(self, _entity="", _attribute="", _old="", _new="", _kwargs={}):
        if not await self._is_on("enable"):
            return
        self.raise_active = False
        self.boundary = None  # a pending boundary retry must not undo the away setpoints
        await self._write_setpoints(await self._schedule_setpoints("night"), "away on")

    async def _on_away_off(self, _entity="", _attribute="", _old="", _new="", _kwargs={}):
        if not await self._is_on("enable"):
            return
        schedule = await self._schedule()
        now = self._now()
        phase, _ = await self._current_phase(schedule, now, await self._corrected_outdoor())
        self.last_phase_key = phase.key
        self.boundary = None
        self.raise_active = phase.kind == "day"
        await self._write_setpoints(await self._schedule_setpoints(phase.kind), f"away off ({phase.kind})")

    async def _on_target_change(self, _entity="", _attribute="", old=None, new=None, kwargs={}):
        room = kwargs.get("room")
        old_value, new_value = parse_number(old), parse_number(new)
        if room is None or old_value is None or new_value is None or old_value == new_value:
            return
        now = self._now()
        pending = self.pending_writes.get(room)
        if pending is not None and now - pending[1] <= OWN_WRITE_TTL and abs(pending[0] - new_value) < 0.01:
            del self.pending_writes[room]
            return
        direction = "raise" if new_value > old_value else "lower"
        self.log(f"Manual override {room} at {now.isoformat()}: {old_value} -> {new_value}")
        if self.boundary is not None:
            self.boundary["targets"].pop(room, None)  # the manual change wins over a pending retry
        self.overrides = [o for o in self.overrides if now - o["at"] <= OVERRIDE_WINDOW]
        self.overrides.append({"room": room, "at": now, "old": old_value, "new": new_value, "direction": direction})
        self.overrides = self.overrides[-OVERRIDE_HISTORY_MAX:]
        await self._suggest(room, direction, now)

    async def _suggest(self, room, direction, now):
        matches = similar_overrides(self.overrides, room, direction, now)
        if len(matches) < OVERRIDE_REPEAT:
            return
        hour = now.hour
        notification_id = f"comfort_control_{room}_{direction}_{hour:02d}"
        if notification_id in self.suggested:
            return
        self.suggested.add(notification_id)
        values = ", ".join(f"{m['at']:%a %H:%M} {m['old']}->{m['new']}" for m in matches)
        mean_new = sum(m["new"] for m in matches) / len(matches)
        floor = await self._helper(f"floor_{room}")
        day_minimum = await self._helper(f"day_minimum_{room}")
        message = (
            f"{room}: setpoint {'raised' if direction == 'raise' else 'lowered'} by hand {len(matches)} times "
            f"around {hour:02d}:00 in the last 14 days ({values}; mean {mean_new:.1f} degC). Consider "
            f"changing the comfort helpers of {room} (floor {floor:g}, day minimum {day_minimum:g} degC) "
            "or the window times. Nothing was changed."
        )
        self.log(f"Suggestion: {message}")
        await self.call_service(
            "persistent_notification/create",
            title="Comfort control suggestion",
            message=message,
            notification_id=notification_id,
        )

    # --- Climate optimizer plan (ADR-0008) ----------------------------------------------------

    def _live_effective(self, requested):
        """THE hard lock. Plan control is live only if the owner's select says live AND
        optimizer_live_allowed is true in apps.yaml (a reviewed git commit, false by default, never a
        helper). Everything that lets a plan reach the heat pump goes through _plan_controls, which
        asks this at the point of use, every tick."""
        return requested == "live" and self.args.get("optimizer_live_allowed") is True

    def _plan_controls(self):
        return self.plan_control and self._live_effective(self.plan_requested)

    def _effective_plan_mode(self):
        if self.plan_requested == "live" and not self._live_effective("live"):
            return "shadow"
        return self.plan_requested

    async def _http_get(self, url, timeout):
        """GET `url`: (status, body text), the body capped at PLAN_MAX_BYTES (a test seam)."""
        import aiohttp

        async with aiohttp.ClientSession(timeout=aiohttp.ClientTimeout(total=timeout)) as session:
            async with session.get(url, headers={"Accept": "application/json"}) as response:
                body = await response.content.read(PLAN_MAX_BYTES + 1)
                if len(body) > PLAN_MAX_BYTES:
                    return response.status, ""
                return response.status, body.decode("utf-8", errors="replace")

    async def _plan_requested_mode(self):
        entity = (self.args.get("climate_plan") or {}).get("mode_entity")
        raw = await self.get_state(entity) if entity else None
        return raw if raw in PLAN_MODES else "off"  # missing, unavailable or garbage: Rules

    async def _on_plan_mode(self, _entity="", _attribute="", old="", new="", _kwargs={}):
        # A change of the owner's select revokes plan-derived control at once, not at the next tick
        # (the tick then re-reads the select and publishes). Nothing else is done here.
        self.log(f"Climate optimizer mode select: {old} -> {new}")
        if not self._live_effective(new if new in PLAN_MODES else "off"):
            self.plan_control = False

    async def _plan_fetch(self, now):
        config = self.args.get("climate_plan") or {}
        self.plan_last_fetch = now
        self.plan_next_fetch = now + PLAN_RETRY_INTERVAL
        url = config.get("url")
        if not url:
            self.plan, self.plan_fetch_error = None, "not_configured"
            return
        try:
            status, text = await self._http_get(url, config.get("timeout_s", PLAN_TIMEOUT_S))
        except Exception as error:  # timeout, DNS, refused: the plan is absent, never kept
            self.plan, self.plan_fetch_error = None, "fetch_failed"
            self._plan_warn("fetch_failed", f"Climate plan fetch failed: {error!r}")
            return
        plan, reason = parse_plan(status, text)
        if plan is None:
            self.plan, self.plan_fetch_error = None, reason
            self._plan_warn(reason, f"Climate plan unusable: {reason}")
            return
        self.plan, self.plan_fetch_error = plan, None
        self.plan_next_fetch = now + PLAN_FETCH_INTERVAL
        self.plan_warned = None
        if plan.plan_id != self.plan_active_id:
            if self.plan_active_id is not None:
                self.plan_burned.append(self.plan_active_id)  # a replan supersedes the previous plan entirely
            self.plan_active_id, self.plan_live_applied = plan.plan_id, None
            self._plan_counters(now)["plans"] += 1
            self.log(
                f"Climate plan {plan.plan_id} received: generated {plan.generated_at:%H:%M}Z, valid until "
                f"{plan.valid_until:%H:%M}Z, {sum(plan.heating)} heating slots of {plan.n}"
            )

    def _plan_warn(self, reason, message):
        if reason != self.plan_warned:  # once per reason, not every retry
            self.log(message, level="WARNING")
        self.plan_warned = reason

    def _plan_counters(self, now):
        day = now.astimezone(self._tz()).date().isoformat()
        if self.plan_today["day"] != day:
            self.plan_today = self._new_plan_counters(day)
        return self.plan_today

    async def _plan_cycle(self, now):
        """Read the select, fetch the plan when due, and resolve the slot of now. Nothing here writes
        to Home Assistant. Any failure leaves the plan absent: the rules decide."""
        self.plan_run = self.plan_water = None
        self.plan_slot, self.plan_reason, self.plan_control, self.plan_blocked = None, None, False, None
        try:
            requested = await self._plan_requested_mode()
            if requested != self.plan_requested:
                self.log(f"Climate optimizer mode: {self.plan_requested} -> {requested}")
            self.plan_requested = requested
            live = self._live_effective(requested)
            if self.plan_live_applied is not None and not live:
                self.log(f"Climate plan {self.plan_live_applied} revoked: plan control is no longer live")
                self.plan_burned.append(self.plan_live_applied)
                self.plan_live_applied = None
            if requested == "off":
                self.plan, self.plan_fetch_error, self.plan_next_fetch = None, None, None
                self.plan_reason = "mode_off"
                return
            if self.plan_next_fetch is None or now >= self.plan_next_fetch:
                await self._plan_fetch(now)
            if self.plan is None:
                self.plan_reason = self.plan_fetch_error or "no_plan"
                return
            self.plan_slot, self.plan_reason = plan_current(self.plan, now)
            if self.plan_slot is None:
                return
            if live:
                if self.plan.plan_id in self.plan_burned:
                    self.plan_blocked = "plan_id_already_applied"
                else:
                    self.plan_control = True
                    if self.plan_live_applied != self.plan.plan_id:
                        self.plan_live_applied = self.plan.plan_id
                        self.log(f"Climate plan {self.plan.plan_id} applied (live)")
        except Exception as error:
            self.plan_slot, self.plan_control, self.plan_reason = None, False, "internal_error"
            self.log(f"Climate plan cycle failed: {error!r}", level="ERROR")

    async def _plan_idle(self, now, reason):
        """The controller does not run (disabled, seeding): no plan is evaluated or used."""
        self.plan_slot, self.plan_control, self.plan_run, self.plan_water = None, False, None, None
        self.plan_reason = reason
        await self._plan_report(now, idle=True)

    def _plan_compare(self):
        """(heating agrees, water agrees, both agree) of this tick; None where not comparable."""
        run, water = self.plan_run, self.plan_water
        agree_heating = run["plan"] == run["rules"] if run is not None and self.plan_slot is not None else None
        agree_water = water["plan"] == water["rules"] if water and water["plan"] is not None else None
        both = None if agree_heating is None else bool(agree_heating and agree_water is not False)
        return agree_heating, agree_water, both

    def _plan_attributes(self, now, idle):
        requested, effective = self.plan_requested, self._effective_plan_mode()
        plan, slot = self.plan, self.plan_slot
        run, water = self.plan_run, self.plan_water
        compared = run is not None and slot is not None
        agree_heating, agree_water, agree = self._plan_compare()
        today = self.plan_today
        attributes = {
            "requested_mode": requested,
            "effective_mode": effective,
            "live_allowed_in_git": self.args.get("optimizer_live_allowed") is True,
            "live_locked": requested == "live" and effective != "live",
            "plan_controls": self._plan_controls() if not idle else False,
            "plan_id": plan.plan_id if plan else None,
            "plan_generated_at": plan.generated_at.isoformat() if plan else None,
            "plan_valid_until": plan.valid_until.isoformat() if plan else None,
            "plan_age_min": round((now - plan.generated_at).total_seconds() / 60) if plan else None,
            "plan_expires_in_min": round((plan.valid_until - now).total_seconds() / 60) if plan else None,
            "plan_usable": slot is not None,
            "fallback_reason": None if slot is not None else self.plan_reason,
            "control_blocked": self.plan_blocked,
            "slot_start": slot.start.isoformat() if slot else None,
            "plan_heating_now": slot.heating if slot else None,
            "plan_water_c_now": slot.water_c if slot else None,
            "rules_heating_now": run["rules"] if compared else None,
            "rules_water_c_now": water["rules"] if water else None,
            "plan_effect_heating_now": run["would"] if compared else None,
            "floor_forced": list(run["floor_forced"]) if compared else None,
            "agree_heating": agree_heating,
            "agree_water": agree_water,
            "agree": agree,
            "last_fetch": self.plan_last_fetch.isoformat() if self.plan_last_fetch else None,
            "today": today["day"],
            "today_samples": today["samples"],
            "today_agree": today["agree"],
            "today_agree_pct": round(100 * today["agree"] / today["samples"]) if today["samples"] else None,
            "today_heating_disagree": today["heating_disagree"],
            "today_water_disagree": today["water_disagree"],
            "today_would_differ": today["would_differ"],
            "today_unusable": today["unusable"],
            "today_skipped": today["skipped"],
            "today_plans": today["plans"],
        }
        return attributes

    def _plan_count(self, now):
        """One sample per minute of the comparison of the plan with the rules (telemetry only)."""
        minute = now.replace(second=0, microsecond=0)
        if self.plan_requested == "off" or minute == self.plan_counted_minute:
            return
        self.plan_counted_minute = minute
        today = self._plan_counters(now)
        if self.plan_slot is None:
            today["unusable"] += 1
        elif self.plan_run is None:
            today["skipped"] += 1  # a usable plan, but the run step did not reach its comparison
        else:
            agree_heating, agree_water, agree = self._plan_compare()
            today["samples"] += 1
            today["agree"] += bool(agree)
            today["heating_disagree"] += agree_heating is False
            today["water_disagree"] += agree_water is False
            today["would_differ"] += self.plan_run["would"] != self.plan_run["rules"]

    async def _plan_report(self, now, idle=False):
        """Publish sensor.comfort_climate_plan_status (state: off / shadow / live_locked / live) and log
        changes. Telemetry: the only thing the plan writes while it is not live is this sensor."""
        try:
            if not idle:
                self._plan_count(now)
            attributes = self._plan_attributes(now, idle)
            requested, effective = attributes["requested_mode"], attributes["effective_mode"]
            state = "live_locked" if attributes["live_locked"] else effective
            text = {
                "off": "Rules (optimizer plan off)",
                "shadow": "Optimizer shadow (plan compared, nothing written from it)",
                "live_locked": "Live requested, locked in git: behaving as shadow",
                "live": "Optimizer live (plan drives the run decision and the water target)",
            }[state]
            attributes = {**attributes, "status_text": text, "friendly_name": "Comfort climate plan"}
            logged = (state, attributes["plan_id"], attributes["fallback_reason"], attributes["plan_heating_now"],
                      attributes["rules_heating_now"], attributes["plan_water_c_now"])
            if logged != self.plan_logged and requested != "off":
                self.plan_logged = logged
                self.log(
                    f"Climate plan [{state}] plan {attributes['plan_id']}: "
                    + (
                        f"plan heating={attributes['plan_heating_now']} water={attributes['plan_water_c_now']}, "
                        f"rules heating={attributes['rules_heating_now']} water={attributes['rules_water_c_now']}, "
                        f"agree={attributes['agree']}"
                        if attributes["plan_usable"]
                        else f"not usable ({attributes['fallback_reason']}): rules decide"
                    )
                )
            if (state, attributes) == self.plan_status_published:
                return
            self.plan_status_published = (state, attributes)
            await self.set_state(self.args["climate_plan"]["status_entity"], state=state, attributes=attributes)
        except Exception as error:  # telemetry must never stop the control loop
            self.log(f"Climate plan status failed: {error!r}", level="ERROR")

    async def _load_plan_status(self):
        """Restore today's counters after an AppDaemon restart (the sensor survives it)."""
        entity = (self.args.get("climate_plan") or {}).get("status_entity")
        state = await self.get_state(entity, attribute="all") if entity else None
        attributes = (state or {}).get("attributes", {}) if isinstance(state, dict) else {}
        if attributes.get("today") != self._now().astimezone(self._tz()).date().isoformat():
            return
        counters = self._new_plan_counters(attributes["today"])
        for key, name in (("samples", "samples"), ("agree", "agree"), ("heating_disagree", "heating_disagree"),
                          ("water_disagree", "water_disagree"), ("would_differ", "would_differ"),
                          ("unusable", "unusable"), ("skipped", "skipped"), ("plans", "plans")):
            value = attributes.get(f"today_{name}")
            counters[key] = value if isinstance(value, int) and not isinstance(value, bool) and value >= 0 else 0
        self.plan_today = counters

    # --- Tick ---------------------------------------------------------------------------------

    async def _tick(self, kwargs=None):
        if self.busy:
            return
        self.busy = True
        try:
            now = self._now()
            if not await self._is_on("enable"):
                await self._release_force_dhw_safely(now)
                await self._publish_status("disabled", {})
                await self._plan_idle(now, "controller_disabled")
                return
            if not await self._is_on("seeded"):
                # Helpers re-created at their HA minimums would give 18 degC setpoints (the floor
                # minimum is a valid value): no control until the defaults are written. No heartbeat either, so the HA watchdog restores heating.
                try:
                    await self._seed_helpers()
                except Exception as error:
                    self.log(f"Seeding the helpers failed: {error!r}", level="ERROR")
                if not await self._is_on("seeded"):
                    self.log("Helpers not seeded: control skipped this tick", level="WARNING")
                    await self._release_force_dhw_safely(now)
                    await self._publish_status("seeding", {})
                    await self._plan_idle(now, "controller_seeding")
                    return
            outdoor = await self._corrected_outdoor()
            self._sample_outdoor(now, outdoor)
            schedule = await self._schedule()
            self._freeze_band_outdoor(schedule, now, outdoor)
            phase, leads = await self._current_phase(schedule, now, outdoor)
            await self._plan_cycle(now)
            run_ok = False
            for name, step in (
                ("setpoints", lambda: self._update_setpoints(phase)),
                ("raise", lambda: self._update_raise()),
                ("water", lambda: self._update_water(now, outdoor)),
                ("run", lambda: self._update_run(now, phase)),
                ("dhw", lambda: self._update_dhw(now, schedule, leads)),
                ("starts", lambda: self._check_starts(now)),
                ("dhw plan", lambda: self._persist_dhw_plan()),
            ):
                try:
                    result = await step()
                    run_ok = run_ok or (name == "run" and result is True)
                except Exception as error:  # one failing part must not stop the others
                    self.log(f"Tick step {name} failed: {error!r}", level="ERROR")
            await self._release_force_dhw_safely(now)
            await self._publish_status(await self._status_state(), self._status_attributes(phase, schedule, leads))
            await self._plan_report(now)
            if run_ok:  # the HA watchdog restores heating when this goes stale
                await self.set_state(
                    self.args["heartbeat_entity"],
                    state=now.isoformat(),
                    attributes={"friendly_name": "Comfort control heartbeat", "device_class": "timestamp"},
                )
        finally:
            self.busy = False

    async def _release_force_dhw_safely(self, now, startup=False):
        # Every tick, enabled or not: force DHW is never left on beyond its window or the cap.
        try:
            await self._release_force_dhw(now, startup)
        except Exception as error:
            self.log(f"Force DHW release failed: {error!r}", level="ERROR")

    async def _status_state(self):
        if await self._is_on("away"):
            return "away"
        mode = await self.get_state(self.args["heat_mode"]["entity"])
        return "heating" if mode == self.args["heat_mode"]["on_mode"] else "idle"

    def _status_attributes(self, phase, schedule, leads):
        today = phase.at.astimezone(self._tz()).date()
        upcoming = {
            day.isoformat(): schedule.early_start(day, leads[day]).isoformat()
            for day in (today, today + timedelta(days=1))
            if day in leads
        }
        plan = self.dhw_plan or {}
        stored_plan = self._stored_dhw_plan()
        return {
            "phase": phase.kind,
            "phase_key": self.last_phase_key,
            "boundary_pending": sorted(self.boundary["targets"]) if self.boundary else [],
            "mode_pending": self.pending_mode["target"] if self.pending_mode else None,
            "dhw_plan": stored_plan,
            "early_starts": upcoming,
            "raise_active": self.raise_active,
            "band_outdoor": dict(self.band_outdoor),
            "dhw_window": f"{plan['start']:%H:%M}-{plan['end']:%H:%M}" if plan.get("start") else None,
            "dhw_done": self.dhw_done,
            "overrides_14d": len(self.overrides),
        }

    def _stored_dhw_plan(self):
        plan = self.dhw_plan
        if plan is None:
            return None
        return {
            "day": plan["day"].isoformat(),
            "start": plan["start"].isoformat() if plan.get("start") else None,
            "end": plan["end"].isoformat() if plan.get("end") else None,
            "forced": bool(plan.get("forced")),
            "force_requested": bool(plan.get("force_requested")),
            "target_written": bool(plan.get("target_written")),
        }

    async def _persist_dhw_plan(self):
        """Today's DHW plan and the last finished day in an input_text helper, which Home Assistant
        restores after a restart (the status sensor is not restored): one window per day."""
        entity = self.args["helpers"].get("dhw_plan")
        stored = self._stored_dhw_plan()
        if not entity or (stored is None and self.dhw_done is None):
            return
        value = json.dumps({**(stored or {}), "done": self.dhw_done}, separators=(",", ":"))
        if value == self.dhw_plan_written:
            return
        self.dhw_plan_written = value
        await self.call_service("input_text/set_value", entity_id=entity, value=value)

    async def _update_setpoints(self, phase):
        if self.last_phase_key is not None and phase.key <= self.last_phase_key:
            return
        if self.boundary is not None and self.boundary["key"] == phase.key:
            await self._confirm_boundary()
        else:
            await self._apply_phase(phase, f"boundary at {phase.at:%Y-%m-%d %H:%M}")

    async def _room_state(self, entity):
        """(current, target), "off" for a thermostat switched off, or None when it reports nothing usable."""
        state = await self.get_state(entity, attribute="all")
        if isinstance(state, dict) and state.get("state") == "off":
            return "off"
        if not isinstance(state, dict) or state.get("state") in (None, "unknown", "unavailable"):
            return None
        attributes = state.get("attributes", {})
        current = parse_number(attributes.get("current_temperature"))
        target = parse_number(attributes.get("temperature"))
        return None if current is None or target is None else (current, target)

    async def _update_raise(self):
        if not self.raise_active:
            return
        room = await self._room_state(self.args["rooms"][self.args["day_room"]])
        if isinstance(room, tuple) and room[0] >= room[1]:
            self.raise_active = False
            self.log(f"{self.args['day_room']} reached its setpoint ({room[0]} >= {room[1]}): early-start raise ends")

    # --- Water temperature --------------------------------------------------------------------

    async def _block_water(self, reason):
        if reason != self.water_block_reason:
            self.log(f"Water temperature not written: {reason}", level="WARNING")
        self.water_block_reason = reason

    async def _update_water(self, now, outdoor):
        sensors = self.args["sensors"]
        heating_mode = await self._number(sensors["heating_mode"])
        if heating_mode != DIRECT_HEATING_MODE:
            return await self._block_water(f"heating mode is {heating_mode} (direct is {DIRECT_HEATING_MODE})")
        if outdoor is None:
            return await self._block_water("outdoor temperature unavailable")
        points = [await self._helper(role) for role in ("curve_cold_outdoor", "curve_cold_water", "curve_mild_outdoor", "curve_mild_water")]
        curve = curve_water(outdoor, *points)
        if curve is None:
            return await self._block_water(f"invalid curve points {points}")
        raise_k = await self._helper("early_start_raise") if self.raise_active else 0.0
        target, request = water_request(curve, raise_k)
        reason = f"curve {curve:.1f}" + (f" + raise {raise_k:g} K" if raise_k else "")
        slot = self.plan_slot
        if slot is not None:
            # The plan's water target (heating slots only), clamped like the curve's. It is only
            # compared unless plan control is live (never while locked): then it replaces the curve.
            plan_target = water_request(slot.water_c) if slot.heating and slot.water_c is not None else None
            self.plan_water = {"rules": request, "plan": plan_target[1] if plan_target else None}
            if plan_target is not None and self._plan_controls():
                (target, request), reason = plan_target, f"plan water_c {slot.water_c} (curve {curve:.1f})"
        current = await self._number(sensors["z1_request"])
        if current is None:
            return await self._block_water("current Z1 request unavailable")
        self.water_block_reason = None
        if not water_write_due(target, request, current, self.last_water_write, now):
            return
        # The water dry-run helper is on until the owner has checked the first write on the unit;
        # anything but "off" (unknown, unavailable) keeps it a dry run.
        actuate = self._actuate("water_temperature") and await self.get_state(self.args["helpers"]["water_dry_run"]) == "off"
        dry_run = "" if actuate else " (dry run)"
        self.log(
            f"Water request write at {now.isoformat()}: {current:g} -> {request} degC "
            f"(outdoor {outdoor:.1f} degC, {reason}){dry_run}"
        )
        self.last_water_write = now
        if actuate:
            await self.call_service(
                "input_number/set_value", entity_id=self.args["commands"]["water_request"], value=request
            )

    # --- Run logic ----------------------------------------------------------------------------

    async def _confirm_mode(self):
        """Only the read-only Aquarea operating-mode sensor can acknowledge a mode write."""
        pending = self.pending_mode
        if pending is None:
            return True
        actual = await self.get_state(self.args["sensors"]["operating_mode"])
        if actual != pending["target"]:
            return False
        self.last_mode_change = self._now()  # conservative minimum on/off start
        self.pending_mode = None
        self.log(f"Heat mode confirmed by Aquarea: {actual}")
        return True

    async def _set_mode(self, mode, reason):
        entity = self.args["heat_mode"]["entity"]
        actual = await self.get_state(self.args["sensors"]["operating_mode"])
        dry_run = "" if self._actuate("mode") else " (dry run)"
        self.log(f"Heat mode -> {mode} ({reason}){dry_run}")
        if not self._actuate("mode"):
            return False
        # Re-selecting an unchanged helper does not fire the state-triggered MQTT automation.
        # A dropped command therefore needs an explicit retry through the same HA automation.
        self.pending_mode = {"target": mode, "at": self._now()}
        if await self.get_state(entity) == mode and actual != mode:
            await self.call_service(
                "automation/trigger", entity_id="automation.set_heatmode", skip_condition=True
            )
        else:
            await self.set_state(entity, state=mode)
        return await self._confirm_mode()

    async def _floor_forced(self, rooms, phase, hysteresis):
        """Rooms below their own comfort minimum minus the hysteresis: the floor outside the day
        window (and while away), the higher of floor and day minimum inside it. Comfort wins over
        the plan (ADR-0008 decision 5)."""
        kind = "night" if phase is None or await self._is_on("away") else phase.kind
        forced = []
        for room, (current, _target) in rooms.items():
            if current < await self._room_minimum(room, kind) - hysteresis:
                forced.append(room)
        return forced

    async def _update_run(self, now, phase=None):
        modes = self.args["heat_mode"]
        if self.pending_mode is not None:
            if await self._confirm_mode():
                return True
            pending = self.pending_mode
            if now - pending["at"] < MODE_RETRY:
                return False  # withhold heartbeat; let HA watchdog restore heating if necessary
            self.log(
                f"Aquarea did not confirm {pending['target']} for {MODE_RETRY}: retrying",
                level="WARNING",
            )
            self.pending_mode = None
        mode = await self.get_state(self.args["sensors"]["operating_mode"])
        if mode not in (modes["on_mode"], modes["off_mode"]):
            if mode != self.mode_warning:
                self.log(
                    f"Aquarea mode is {mode}, not {modes['on_mode']}/{modes['off_mode']}: left as is",
                    level="WARNING",
                )
            self.mode_warning = mode
            # Manual cooling/other known mode is intentional; an absent reading is not healthy.
            return mode not in (None, "unknown", "unavailable")
        self.mode_warning = None
        hysteresis = await self._helper("hysteresis")
        rooms, missing = {}, []
        for room, entity in self.args["rooms"].items():
            state = await self._room_state(entity)
            if state is None:
                missing.append(room)
            elif state != "off":
                rooms[room] = state
        cold = [room for room, (current, target) in rooms.items() if current < target - hysteresis]
        satisfied = not missing and all(current >= target for current, target in rooms.values())
        if missing and missing != self.missing_warning:
            self.log(f"No usable temperature or setpoint from {', '.join(missing)}: heating kept on", level="WARNING")
        self.missing_warning = missing
        elapsed = None if self.last_mode_change is None else now - self.last_mode_change
        detail = ", ".join(f"{room} {current}/{target}" for room, (current, target) in rooms.items())
        mode_on = mode == modes["on_mode"]
        want = rules_want = run_want(mode_on, cold, missing, satisfied)
        slot, floor_forced = self.plan_slot, []
        controlling = False
        if slot is not None:
            # Always computed while a plan is current (telemetry); it decides only under plan control.
            floor_forced = await self._floor_forced(rooms, phase, hysteresis)
            plan_want = run_want(mode_on, cold, missing, satisfied, plan_heating=slot.heating, floor_forced=bool(floor_forced))
            heats = lambda wanted: wanted == "on" or (wanted == "hold" and mode_on)  # noqa: E731
            self.plan_run = {
                "rules": heats(rules_want),
                "plan": slot.heating,
                "would": heats(plan_want),
                "floor_forced": floor_forced,
            }
            controlling = self._plan_controls()
            if controlling:
                want = plan_want
        if want == "on" and not mode_on:
            if elapsed is not None and elapsed < MIN_OFF:
                return True
            if floor_forced and controlling and not slot.heating:
                reason = f"comfort floor beats the plan: {', '.join(floor_forced)}"
            elif cold:
                reason = f"below setpoint: {', '.join(cold)}"
            elif floor_forced and controlling:
                reason = f"below the comfort floor: {', '.join(floor_forced)}"
            else:
                reason = f"no reading from {', '.join(missing)}"
            return await self._set_mode(modes["on_mode"], f"{reason} ({detail})")
        if want == "off" and mode_on:
            if elapsed is not None and elapsed < MIN_ON:
                return True
            plan_denied = controlling and rules_want != "off"
            reason = f"plan: heating not permitted in the slot of {slot.start:%H:%M} UTC" if plan_denied else "every room at setpoint"
            return await self._set_mode(modes["off_mode"], f"{reason} ({detail})")
        return True

    async def _check_starts(self, now):
        counter = await self._number(self.args["sensors"]["compressor_starts"])
        if counter is None:
            return
        today = now.date().isoformat()
        if self.starts_day != today or self.starts_baseline is None or counter < self.starts_baseline:
            self.starts_day, self.starts_baseline, self.starts_alarmed = today, counter, False
            return
        starts = int(counter - self.starts_baseline)
        if starts > MAX_STARTS_PER_DAY and not self.starts_alarmed:
            self.starts_alarmed = True
            await self._notify(f"Comfort control: {starts} compressor starts today (alarm above {MAX_STARTS_PER_DAY})")

    # --- DHW ----------------------------------------------------------------------------------

    async def _prices(self, slots):
        """(day of the prices, {slot index: price}). The PVPC attributes are today's prices from
        the hourly update after midnight on: their day is the local date of last_updated."""
        state = await self.get_state(self.args["sensors"]["pvpc_price"], attribute="all")
        if not isinstance(state, dict):
            return None, {}
        updated = parse_datetime(state.get("last_updated"))
        day = updated.astimezone(self._tz()).date() if updated else None
        attributes = state.get("attributes") or {}
        prices = {index: parse_number(attributes.get(key)) for index, (_, _, key) in enumerate(slots)}
        return day, {index: price for index, price in prices.items() if price is not None}

    async def _hourly_outdoor(self, live_outdoor):
        """Expected outdoor temperature per local hour: yesterday's hourly means (persistence) plus
        the offset; the live reading fills hours without history."""
        offset = await self._helper("outdoor_offset")
        samples = {}
        try:
            history = await self.get_history(
                entity_id=self.args["sensors"]["outdoor"], days=1, minimal_response=True, no_attributes=True
            )
        except Exception as error:
            self.log(f"Outdoor history unavailable: {error!r}", level="WARNING")
            history = None
        for series in history or []:
            for item in series:
                value = parse_number(item.get("state"))
                at = parse_datetime(item.get("last_changed"))
                if value is not None and at is not None:
                    samples.setdefault(at.astimezone(self._tz()).hour, []).append(value + offset)
        hourly = {hour: sum(values) / len(values) for hour, values in samples.items()}
        if live_outdoor is not None:
            for hour in range(24):
                hourly.setdefault(hour, live_outdoor)
        return hourly

    async def _plan_dhw(self, now, schedule, leads):
        today = now.date()
        tz = self._tz()
        self.dhw_last_attempt = now
        slots = day_hour_slots(tz, today)  # 23, 24 or 25 hours
        early = schedule.early_start(today, leads[today])
        start_of_window, _ = schedule.window(today)
        latest_end = localize(tz, datetime.combine(today, DHW_LATEST_END))
        allowed = []
        for index, (begin, _, _) in enumerate(slots):
            end = begin + timedelta(hours=DHW_WINDOW_HOURS)
            if now - begin >= timedelta(minutes=5) or end > latest_end:
                continue  # already started, or would run into the next day
            if begin < start_of_window and end > early:
                continue  # overlaps the optimum-start warm-up
            allowed.append(index)
        price_day, prices = await self._prices(slots)
        if price_day != today or len(prices) < len(slots):
            self.log(
                f"PVPC prices not usable for {today} (day {price_day}, {len(prices)}/{len(slots)} hours): "
                f"retrying in {PLAN_RETRY}",
                level="WARNING",
            )
            # The integration rolls the prices over on the hour: only warn when it is late.
            if self.dhw_warning != today and now.hour >= 1:
                self.dhw_warning = today
                await self._notify("Comfort control: no full PVPC price curve for today; DHW window not planned yet")
            return
        hourly = await self._hourly_outdoor(await self._corrected_outdoor())
        temperatures = {index: hourly.get(hour) for index, (_, hour, _) in enumerate(slots)}
        best = choose_dhw_window(prices, temperatures, allowed)
        if best is None:
            self.log("No DHW window left today", level="WARNING")
            self.dhw_plan = {"day": today, "start": None, "end": None, "forced": False}
            return
        begin = slots[best[0]][0]
        self.dhw_plan = {
            "day": today,
            "start": begin,
            "end": (begin + timedelta(hours=DHW_WINDOW_HOURS)).astimezone(tz),
            "forced": False,
        }
        await self._notify(
            f"Comfort control: DHW window {begin:%H:%M}-{self.dhw_plan['end']:%H:%M} "
            f"(price/COP {best[1]:.4f} EUR/kWh of heat)"
        )

    async def _update_dhw(self, now, schedule, leads):
        today = now.date()
        if self.dhw_plan is not None and self.dhw_plan["day"] != today:
            if self.dhw_plan.get("start") is not None:
                await self._finish_dhw(self.dhw_plan)  # a window left over from a previous day
            self.dhw_plan = None
        if self.dhw_plan is None:
            if self.dhw_done == today.isoformat():
                return
            if self.dhw_last_attempt is None or now - self.dhw_last_attempt >= PLAN_RETRY:
                await self._plan_dhw(now, schedule, leads)
            if self.dhw_plan is None:
                return
        plan = self.dhw_plan
        if plan["start"] is None:
            return
        if now >= plan["end"]:
            await self._finish_dhw(plan)
        elif now >= plan["start"] and not plan["forced"]:
            await self._start_dhw(plan)

    def _forced_window_active(self, now):
        plan = self.dhw_plan
        return bool(plan and (plan.get("forced") or plan.get("force_requested")) and plan.get("start") and plan["start"] <= now < plan["end"])

    async def _release_force_dhw(self, now, startup=False):
        """Force DHW is released when it is on outside an active forced window and:

        - the app set it (its window is over: day rollover, disable, a lost turn-off);
        - the app did not set it (manual): it is held like a manual setpoint, for at most
          MANUAL_FORCE_MAX from when the app first sees it;
        - at startup the owner is unknown: released unless a stored forced window is active.
        The unit's own reheat is not affected."""
        entity = self.args["commands"]["force_dhw"]
        if not self._actuate("dhw"):
            return
        if await self.get_state(entity) != "on":
            self.force_owned, self.manual_force_since = False, None
            return
        if self._forced_window_active(now):
            return
        if startup:
            reason = "startup: owner unknown and no forced DHW window active"
        elif self.force_owned or (self.dhw_plan and (self.dhw_plan.get("forced") or self.dhw_plan.get("force_requested"))):
            reason = "set by the controller and its window is over"
        else:
            if self.manual_force_since is None:
                self.manual_force_since = now
                self.log(
                    f"Force DHW on outside the controller at {now.isoformat()}: held for at most {MANUAL_FORCE_MAX}",
                    level="WARNING",
                )
                return
            if now - self.manual_force_since < MANUAL_FORCE_MAX:
                return
            reason = f"manual force DHW held since {self.manual_force_since.isoformat()}"
        self.log(f"Force DHW off ({reason})", level="WARNING")
        await self.call_service("switch/turn_off", entity_id=entity)
        self.manual_force_since = None

    async def _start_dhw(self, plan):
        sensors, commands = self.args["sensors"], self.args["commands"]
        now = self._now()
        actuate = self._actuate("dhw")
        dry_run = "" if actuate else " (dry run)"
        if await self.get_state(sensors["force_heater"]) == "on":
            await self._block_dhw(plan, "force heater is on; forcing DHW could start the 3 kW heater")
            return
        desired = int(min(await self._helper("dhw_target"), TANK_MAX))
        tank_target = await self._number(sensors["tank_target"])
        if tank_target != desired:
            plan["target_written"] = False
            # Never force DHW with the old tank target.  An HA helper write may not
            # reach the unit; retry against the observed tank target, not a local flag.
            if self.dhw_target_attempt is None or now - self.dhw_target_attempt >= DHW_COMMAND_RETRY:
                self.dhw_target_attempts += 1
                if self.dhw_target_attempts >= DHW_TARGET_NOTIFY_ATTEMPTS:
                    await self._block_dhw(
                        plan,
                        f"the unit did not confirm tank target {desired} degC (reads {tank_target:g})",
                    )
                self.log(f"Tank target {tank_target} -> {desired} degC before forcing DHW{dry_run}")
                if actuate:
                    helper = await self._number(commands["tank_target"])
                    if helper == desired:
                        # The previous command may have been lost while the helper already
                        # contains the target: explicitly replay its MQTT automation.
                        await self.call_service(
                            "automation/trigger",
                            entity_id="automation.set_tank_target_temperature",
                            skip_condition=True,
                        )
                    else:
                        await self.call_service(
                            "input_number/set_value", entity_id=commands["tank_target"], value=desired
                        )
                self.dhw_target_attempt = now
            return
        plan["target_written"] = True  # observed on Aquarea, not inferred from the helper
        self.dhw_target_attempt = None
        self.dhw_target_attempts = 0
        if tank_target is None or tank_target > TANK_MAX:
            await self._block_dhw(plan, f"tank target {tank_target} is not <= {TANK_MAX} degC")
            return
        if not actuate:
            plan["forced"] = True
            return
        forced = await self.get_state(commands["force_dhw"])
        if forced == "on":
            # A manual force is not ours.  Claim only an explicitly recorded request.
            if plan.get("force_requested"):
                plan["forced"] = True
                self.force_owned = True
                self.dhw_force_attempt = None
            return
        if self.dhw_force_attempt is not None and now - self.dhw_force_attempt < DHW_COMMAND_RETRY:
            return
        self.log(f"Request force DHW until {plan['end']:%H:%M} (tank target {tank_target:g} degC)")
        plan["force_requested"] = True  # intent, not an acknowledgement; survive lost replies
        self.dhw_force_attempt = now
        await self.call_service("switch/turn_on", entity_id=commands["force_dhw"])

    async def _block_dhw(self, plan, reason):
        if not plan.get("blocked"):
            plan["blocked"] = True
            await self._notify(f"Comfort control: DHW not forced: {reason}")

    async def _finish_dhw(self, plan):
        if (plan.get("forced") or plan.get("force_requested")) and self._actuate("dhw") and await self.get_state(self.args["commands"]["force_dhw"]) == "on":
            self.log("DHW window over: force DHW off")
            await self.call_service("switch/turn_off", entity_id=self.args["commands"]["force_dhw"])
        self.dhw_done = plan["day"].isoformat()
        self.dhw_plan = {**plan, "start": None, "end": None}

    async def terminate(self):
        # AppDaemon shutdown or reload: never change the mode. The HA watchdog restores heating
        # if AppDaemon stays down; the unit keeps the last water temperature.
        self.log("Terminating: heat mode and water temperature left as they are")
