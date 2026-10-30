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
"""

import math
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
COP_AT_0C = 2.2
COP_PER_K = 0.06
COP_MIN = 1.5
COP_MAX = 4.5
PLAN_RETRY = timedelta(minutes=10)

# Manual overrides: a suggestion when OVERRIDE_REPEAT similar ones fall within OVERRIDE_WINDOW.
OVERRIDE_WINDOW = timedelta(days=14)
OVERRIDE_REPEAT = 3
OVERRIDE_TIME_TOLERANCE = timedelta(minutes=60)
OVERRIDE_HISTORY_MAX = 200
OWN_WRITE_TTL = timedelta(minutes=5)  # a target change matching our write within this is ours

TICK_SECONDS = 60
MODE_CONFIRM_SECONDS = 30

# TODO(phase 2): boost for very cheap hours (design section 8). Built only after normal mode has
# run in production: space-heating soak (room ceiling ~25.5 degC, water ~30-32 degC) started on
# battery SoC ~90-95 %, falling charge power or window end minus soak time; DHW still <= TANK_MAX.
BOOST_ENABLED = False

# Default helper values, written once when the seeded flag is off (HA creates an input_number at
# its minimum, and `initial:` would reset owner edits at every HA restart).
HELPER_DEFAULTS = {
    "day_minimum": 23.5,
    "night_floor": 21.0,
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
BANDS = ("cold", "mid", "mild")


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

    prices and temperatures map the local hour (0-23) to a value; allowed_starts is an iterable of
    start hours. A window needs a price for each of its hours. Ties keep the earliest start.
    """
    best = None
    for start in sorted(allowed_starts):
        hours = range(start, start + DHW_WINDOW_HOURS)
        if hours[-1] > 23 or any(prices.get(h) is None or temperatures.get(h) is None for h in hours):
            continue
        score = sum(prices[h] / expected_cop(temperatures[h]) for h in hours) / DHW_WINDOW_HOURS
        if best is None or score < best[1]:
            best = (start, score)
    return best


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
        longer than the night, the hall/kitchen stays at its day setpoint overnight."""
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

        mode_state = await self.get_state(self.args["heat_mode"]["entity"], attribute="all")
        if isinstance(mode_state, dict):
            self.last_mode_change = parse_datetime(mode_state.get("last_changed"))
        await self._startup_checks()
        await self.run_every(self._tick, "now", TICK_SECONDS)
        await self.create_task(self._tick({}))

    def _init_state(self):
        self.busy = False
        self.last_phase_key = None
        self.raise_active = False
        self.pending_writes = {}  # room -> (value, written at)
        self.overrides = []  # dicts: room, at, old, new, direction
        self.suggested = set()
        self.last_mode_change = None
        self.mode_warning = None
        self.last_water_write = None
        self.water_block_reason = None
        self.outdoor_samples = deque()
        self.band_outdoor = {}  # window day (ISO) -> frozen offset-corrected outdoor degC
        self.dhw_plan = None  # dict: day, start, end, target_written, forced, blocked
        self.dhw_done = None  # ISO date of the last finished DHW window
        self.dhw_last_attempt = None
        self.dhw_warning = None
        self.starts_day = None
        self.starts_baseline = None
        self.starts_alarmed = False
        self.status_published = None

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

    async def _schedule_setpoints(self, phase_kind):
        floor = round_step(await self._helper("night_floor") + await self._helper("margin"))
        setpoints = {room: floor for room in self.args["rooms"]}
        if phase_kind == "day":
            day = await self._helper("day_minimum") + await self._helper("margin")
            setpoints[self.args["day_room"]] = round_step(day)
        return setpoints

    async def _write_setpoints(self, setpoints, reason):
        now = self._now()
        for room, value in setpoints.items():
            entity = self.args["rooms"][room]
            old = await self.get_state(entity, attribute="temperature")
            if parse_number(old) == value:
                continue
            dry_run = "" if self._actuate("setpoints") else " (dry run)"
            self.log(f"Setpoint {room}: {old} -> {value} ({reason}){dry_run}")
            if self._actuate("setpoints"):
                self.pending_writes[room] = (value, now)
                await self.call_service("climate/set_temperature", entity_id=entity, temperature=value)

    async def _apply_phase(self, phase, reason):
        self.last_phase_key = phase.key
        self.raise_active = phase.kind == "day"
        if await self._is_on("away"):
            self.raise_active = False
            self.log(f"Boundary {phase.kind} {phase.day} ({reason}) while away: setpoints unchanged")
            return
        await self._write_setpoints(await self._schedule_setpoints(phase.kind), f"{phase.kind} {reason}")

    # --- Event handlers -----------------------------------------------------------------------

    async def _on_enable(self, _entity="", _attribute="", _old="", _new="", _kwargs={}):
        self.log("Comfort control enabled: writing the current schedule")
        self.last_phase_key = None
        await self._tick({})

    async def _on_disable(self, _entity="", _attribute="", _old="", _new="", _kwargs={}):
        # As the C0 fix: nobody leaves the off mode without the controller, so restore heating.
        on_mode = self.args["heat_mode"]["on_mode"]
        await self._set_mode(on_mode, "controller disabled")
        await self._notify(f"Comfort control disabled: heating restored to {on_mode}; the mode is now manual")

    async def _on_away(self, _entity="", _attribute="", _old="", _new="", _kwargs={}):
        if not await self._is_on("enable"):
            return
        self.raise_active = False
        await self._write_setpoints(await self._schedule_setpoints("night"), "away on")

    async def _on_away_off(self, _entity="", _attribute="", _old="", _new="", _kwargs={}):
        if not await self._is_on("enable"):
            return
        schedule = await self._schedule()
        now = self._now()
        phase, _ = await self._current_phase(schedule, now, await self._corrected_outdoor())
        self.last_phase_key = phase.key
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
        message = (
            f"{room}: setpoint {'raised' if direction == 'raise' else 'lowered'} by hand {len(matches)} times "
            f"around {hour:02d}:00 in the last 14 days ({values}; mean {mean_new:.1f} degC). Consider "
            "changing the comfort helpers (day minimum, window times, night floor). Nothing was changed."
        )
        self.log(f"Suggestion: {message}")
        await self.call_service(
            "persistent_notification/create",
            title="Comfort control suggestion",
            message=message,
            notification_id=notification_id,
        )

    # --- Tick ---------------------------------------------------------------------------------

    async def _tick(self, kwargs=None):
        if self.busy:
            return
        self.busy = True
        try:
            if not await self._is_on("enable"):
                await self._publish_status("disabled", {})
                return
            now = self._now()
            outdoor = await self._corrected_outdoor()
            self._sample_outdoor(now, outdoor)
            schedule = await self._schedule()
            self._freeze_band_outdoor(schedule, now, outdoor)
            phase, leads = await self._current_phase(schedule, now, outdoor)
            for step in (
                lambda: self._update_setpoints(phase),
                lambda: self._update_raise(),
                lambda: self._update_water(now, outdoor),
                lambda: self._update_run(now),
                lambda: self._update_dhw(now, schedule, leads),
                lambda: self._check_starts(now),
            ):
                try:
                    await step()
                except Exception as error:  # one failing part must not stop the others
                    self.log(f"Tick step failed: {error!r}", level="ERROR")
            await self._publish_status(await self._status_state(), self._status_attributes(phase, schedule, leads))
        finally:
            self.busy = False

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
        return {
            "phase": phase.kind,
            "phase_key": self.last_phase_key,
            "early_starts": upcoming,
            "raise_active": self.raise_active,
            "band_outdoor": dict(self.band_outdoor),
            "dhw_window": f"{plan['start']:%H:%M}-{plan['end']:%H:%M}" if plan.get("start") else None,
            "dhw_done": self.dhw_done,
            "overrides_14d": len(self.overrides),
        }

    async def _update_setpoints(self, phase):
        if self.last_phase_key is None or phase.key > self.last_phase_key:
            await self._apply_phase(phase, f"boundary at {phase.at:%Y-%m-%d %H:%M}")

    async def _room_state(self, entity):
        state = await self.get_state(entity, attribute="all")
        if not isinstance(state, dict) or state.get("state") in (None, "off", "unknown", "unavailable"):
            return None
        attributes = state.get("attributes", {})
        current = parse_number(attributes.get("current_temperature"))
        target = parse_number(attributes.get("temperature"))
        return None if current is None or target is None else (current, target)

    async def _update_raise(self):
        if not self.raise_active:
            return
        room = await self._room_state(self.args["rooms"][self.args["day_room"]])
        if room is not None and room[0] >= room[1]:
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
        current = await self._number(sensors["z1_request"])
        if current is None:
            return await self._block_water("current Z1 request unavailable")
        self.water_block_reason = None
        if not water_write_due(target, request, current, self.last_water_write, now):
            return
        reason = f"curve {curve:.1f}" + (f" + raise {raise_k:g} K" if raise_k else "")
        dry_run = "" if self._actuate("water_temperature") else " (dry run)"
        self.log(
            f"Water request write at {now.isoformat()}: {current:g} -> {request} degC "
            f"(outdoor {outdoor:.1f} degC, {reason}){dry_run}"
        )
        self.last_water_write = now
        if self._actuate("water_temperature"):
            await self.call_service(
                "input_number/set_value", entity_id=self.args["commands"]["water_request"], value=request
            )

    # --- Run logic ----------------------------------------------------------------------------

    async def _set_mode(self, mode, reason):
        entity = self.args["heat_mode"]["entity"]
        dry_run = "" if self._actuate("mode") else " (dry run)"
        self.log(f"Heat mode -> {mode} ({reason}){dry_run}")
        self.last_mode_change = self._now()
        if not self._actuate("mode"):
            return
        # The proven C0 path: the selector state triggers the "Set heatmode" automation.
        await self.set_state(entity, state=mode)
        for _ in range(MODE_CONFIRM_SECONDS):
            if await self.get_state(entity) == mode:
                return
            await self.sleep(1)
        self.log(f"Heat mode {mode} not confirmed after {MODE_CONFIRM_SECONDS} s", level="WARNING")

    async def _update_run(self, now):
        modes = self.args["heat_mode"]
        mode = await self.get_state(modes["entity"])
        if mode not in (modes["on_mode"], modes["off_mode"]):
            if mode != self.mode_warning:
                self.log(f"Heat mode is {mode}, not {modes['on_mode']}/{modes['off_mode']}: left as is", level="WARNING")
            self.mode_warning = mode
            return
        self.mode_warning = None
        hysteresis = await self._helper("hysteresis")
        rooms = {}
        for room, entity in self.args["rooms"].items():
            state = await self._room_state(entity)
            if state is not None:
                rooms[room] = state
        if not rooms:
            self.log("No thermostat reports a usable temperature: heat mode unchanged", level="WARNING")
            return
        cold = [room for room, (current, target) in rooms.items() if current < target - hysteresis]
        satisfied = all(current >= target for current, target in rooms.values())
        elapsed = None if self.last_mode_change is None else now - self.last_mode_change
        detail = ", ".join(f"{room} {current}/{target}" for room, (current, target) in rooms.items())
        if mode == modes["off_mode"] and cold:
            if elapsed is not None and elapsed < MIN_OFF:
                return
            await self._set_mode(modes["on_mode"], f"below setpoint: {', '.join(cold)} ({detail})")
        elif mode == modes["on_mode"] and satisfied:
            if elapsed is not None and elapsed < MIN_ON:
                return
            await self._set_mode(modes["off_mode"], f"every room at setpoint ({detail})")

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

    async def _prices(self):
        state = await self.get_state(self.args["sensors"]["pvpc_price"], attribute="all")
        attributes = state.get("attributes", {}) if isinstance(state, dict) else {}
        prices = {hour: parse_number(attributes.get(f"price_{hour:02d}h")) for hour in range(24)}
        return {hour: price for hour, price in prices.items() if price is not None}

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
        self.dhw_last_attempt = now
        first = now.hour + (1 if now.minute >= 5 else 0)
        early = schedule.early_start(today, leads[today])
        start_of_window, _ = schedule.window(today)
        allowed = []
        for hour in range(first, 24 - DHW_WINDOW_HOURS + 1):
            begin = localize(self._tz(), datetime.combine(today, time(hour)))
            end = begin + timedelta(hours=DHW_WINDOW_HOURS)
            if begin < start_of_window and end > early:
                continue  # overlaps the optimum-start warm-up
            allowed.append(hour)
        prices = await self._prices()
        if len(prices) < 24:
            self.log(f"Only {len(prices)} PVPC prices for today: retrying in {PLAN_RETRY}", level="WARNING")
            if self.dhw_warning != today:
                self.dhw_warning = today
                await self._notify("Comfort control: no full PVPC price curve for today; DHW window not planned yet")
            return
        temperatures = await self._hourly_outdoor(await self._corrected_outdoor())
        best = choose_dhw_window(prices, temperatures, allowed)
        if best is None:
            self.log("No DHW window left today", level="WARNING")
            self.dhw_plan = {"day": today, "start": None, "end": None, "forced": False}
            return
        begin = localize(self._tz(), datetime.combine(today, time(best[0])))
        self.dhw_plan = {
            "day": today,
            "start": begin,
            "end": begin + timedelta(hours=DHW_WINDOW_HOURS),
            "forced": False,
        }
        await self._notify(
            f"Comfort control: DHW window {begin:%H:%M}-{self.dhw_plan['end']:%H:%M} "
            f"(price/COP {best[1]:.4f} EUR/kWh of heat)"
        )

    async def _update_dhw(self, now, schedule, leads):
        today = now.date()
        if self.dhw_plan is None or self.dhw_plan["day"] != today:
            self.dhw_plan = None
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

    async def _start_dhw(self, plan):
        sensors, commands = self.args["sensors"], self.args["commands"]
        actuate = self._actuate("dhw")
        dry_run = "" if actuate else " (dry run)"
        if await self.get_state(sensors["force_heater"]) == "on":
            await self._block_dhw(plan, "force heater is on; forcing DHW could start the 3 kW heater")
            return
        desired = int(min(await self._helper("dhw_target"), TANK_MAX))
        tank_target = await self._number(sensors["tank_target"])
        if tank_target != desired and not plan.get("target_written"):
            plan["target_written"] = True
            self.log(f"Tank target {tank_target} -> {desired} degC before forcing DHW{dry_run}")
            if actuate:
                await self.call_service("input_number/set_value", entity_id=commands["tank_target"], value=desired)
                return  # force on a later tick, once the unit reports the new target
            tank_target = desired
        if tank_target is None or tank_target > TANK_MAX:
            await self._block_dhw(plan, f"tank target {tank_target} is not <= {TANK_MAX} degC")
            return
        plan["forced"] = True
        self.log(f"Force DHW until {plan['end']:%H:%M} (tank target {tank_target:g} degC){dry_run}")
        if actuate:
            await self.call_service("switch/turn_on", entity_id=commands["force_dhw"])

    async def _block_dhw(self, plan, reason):
        if not plan.get("blocked"):
            plan["blocked"] = True
            await self._notify(f"Comfort control: DHW not forced: {reason}")

    async def _finish_dhw(self, plan):
        if plan["forced"] and self._actuate("dhw") and await self.get_state(self.args["commands"]["force_dhw"]) == "on":
            self.log("DHW window over: force DHW off")
            await self.call_service("switch/turn_off", entity_id=self.args["commands"]["force_dhw"])
        self.dhw_done = plan["day"].isoformat()
        self.dhw_plan = {**plan, "start": None}

    async def terminate(self):
        # AppDaemon shutdown or reload: never change the mode. The HA watchdog restores heating
        # if AppDaemon stays down; the unit keeps the last water temperature.
        self.log("Terminating: heat mode and water temperature left as they are")
