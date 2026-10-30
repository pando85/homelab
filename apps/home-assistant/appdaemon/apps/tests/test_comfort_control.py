from datetime import date, datetime, time, timedelta, timezone
from pathlib import Path
from unittest.mock import patch

import pytest
import pytz
import yaml
from comfort_control import (
    HELPER_DEFAULTS,
    MAX_STARTS_PER_DAY,
    TANK_MAX,
    ComfortControl,
    Schedule,
    choose_dhw_window,
    curve_water,
    expected_cop,
    lead_band,
    parse_number,
    similar_overrides,
    water_request,
    water_write_due,
)

TZ = pytz.timezone("Europe/Madrid")
APPS_DIR = Path(__file__).resolve().parents[1]
HA_PACKAGE = APPS_DIR.parents[1] / "config" / "integrations" / "appdaemon_comfort.yaml"
HEISHAMON = APPS_DIR.parents[1] / "config" / "integrations" / "heishamon.yaml"


def local(*args):
    return TZ.localize(datetime(*args))


# A Wednesday, a Saturday and a Monday in November 2026.
WED = date(2026, 11, 4)
SAT = date(2026, 11, 7)
MON = date(2026, 11, 9)


class FakeHA:
    """Dict-backed stand-in for the AppDaemon HA API used by the app."""

    def __init__(self, app, args):
        self.app = app
        self.services = []
        self.history = []
        h, s = args["helpers"], args["sensors"]
        self.states = {}
        for role, value in HELPER_DEFAULTS.items():
            self.put(h[role], str(value))
        self.put(h["enable"], "on")
        self.put(h["away"], "off")
        self.put(h["seeded"], "on")
        for entity in args["rooms"].values():
            self.put(entity, "heat", current_temperature=21.6, temperature=21.5)
        self.put(s["outdoor"], "5")
        self.put(s["heating_mode"], "1")
        self.put(s["z1_request"], "28")
        self.put(s["tank_target"], "49")
        self.put(s["compressor_starts"], "1000")
        self.put(s["sterilization"], "off")
        self.put(s["force_heater"], "off")
        self.put(s["pvpc_price"], "0.1", **{f"price_{hour:02d}h": 0.2 for hour in range(24)})
        self.put(args["heat_mode"]["entity"], "DHW")
        self.put(args["commands"]["force_dhw"], "off")

    def put(self, entity, state, **attributes):
        previous = self.states.get(entity, {}).get("attributes", {})
        self.states[entity] = {"state": state, "attributes": {**previous, **attributes}, "last_changed": None}

    def attr(self, entity, name, value):
        self.states[entity]["attributes"][name] = value

    async def get_state(self, entity, attribute=None, **kwargs):
        item = self.states.get(entity)
        if item is None:
            return None
        if attribute is None:
            return item["state"]
        if attribute == "all":
            return {**item, "attributes": dict(item["attributes"])}
        return item["attributes"].get(attribute)

    async def set_state(self, entity, state=None, attributes=None, **kwargs):
        self.states.setdefault(entity, {"attributes": {}, "last_changed": None})
        self.states[entity]["state"] = state
        if attributes is not None:
            self.states[entity]["attributes"] = attributes
        self.services.append(("set_state", entity, state))

    async def call_service(self, service, **data):
        self.services.append((service, data.get("entity_id"), data))
        entity = data.get("entity_id")
        if service == "climate/set_temperature":
            self.attr(entity, "temperature", data["temperature"])
        elif service == "input_number/set_value":
            self.put(entity, str(data["value"]))
        elif service in ("input_boolean/turn_on", "switch/turn_on"):
            self.put(entity, "on")
        elif service == "switch/turn_off":
            self.put(entity, "off")

    async def get_history(self, **kwargs):
        return self.history

    async def sleep(self, seconds):
        return None

    def calls(self, service, entity=None):
        return [c for c in self.services if c[0] == service and (entity is None or c[1] == entity)]

    def messages(self):
        return [c[2]["message"] for c in self.services if c[0] in ("notify/send_message", "persistent_notification/create")]


def make_app(ha=None):
    with patch.object(ComfortControl, "__init__", lambda self: None):
        app = ComfortControl()
    app.args = yaml.safe_load((APPS_DIR / "apps.yaml").read_text())["comfort_control"]
    app.log = lambda msg, level=None: print(f"{level or 'INFO'}: {msg}")
    app.get_timezone = lambda: TZ
    app._init_state()
    ha = ha or FakeHA(app, app.args)
    for name in ("get_state", "set_state", "call_service", "get_history", "sleep"):
        setattr(app, name, getattr(ha, name))
    app.ha = ha
    app.clock = [local(2026, 11, 4, 0, 30)]
    app._now = lambda: app.clock[0]
    return app


@pytest.fixture()
def app():
    return make_app()


async def tick_at(app, *when):
    app.clock[0] = local(*when)
    await app._tick({})


def setpoints(app):
    return {room: app.ha.states[e]["attributes"]["temperature"] for room, e in app.args["rooms"].items()}


def room(app, name, current=None, target=None):
    entity = app.args["rooms"][name]
    if current is not None:
        app.ha.attr(entity, "current_temperature", current)
    if target is not None:
        app.ha.attr(entity, "temperature", target)


# --- Pure functions ---------------------------------------------------------------------------


class TestParseNumber:
    @pytest.mark.parametrize("value", [None, "", "unknown", "unavailable", "abc", "nan", "inf", True, [1]])
    def test_garbage_is_none(self, value):
        assert parse_number(value) is None

    @pytest.mark.parametrize("value,expected", [("1", 1.0), ("1.0", 1.0), (-2.5, -2.5), ("28", 28.0)])
    def test_numbers(self, value, expected):
        assert parse_number(value) == expected


class TestCurve:
    POINTS = (-5, 32, 15, 25)

    @pytest.mark.parametrize("outdoor,expected", [(-5, 32), (15, 25), (5, 28.5), (0, 30.25), (10, 26.75)])
    def test_linear_between_points(self, outdoor, expected):
        assert curve_water(outdoor, *self.POINTS) == pytest.approx(expected)

    @pytest.mark.parametrize("outdoor,expected", [(-20, 32), (-5.1, 32), (15.1, 25), (30, 25)])
    def test_clamped_outside_points(self, outdoor, expected):
        assert curve_water(outdoor, *self.POINTS) == expected

    def test_invalid_points(self):
        assert curve_water(5, 15, 25, -5, 32) is None
        assert curve_water(5, 5, 25, 5, 32) is None
        assert curve_water(None, *self.POINTS) is None

    @pytest.mark.parametrize(
        "curve,raise_k,expected",
        [(28.5, 0, 29), (28.4, 0, 28), (32, 2, 34), (32, 5, 35), (25, 0, 25), (18, 0, 20), (40, 0, 35)],
    )
    def test_request_is_integer_clamped_20_to_35(self, curve, raise_k, expected):
        _, request = water_request(curve, raise_k)
        assert request == expected and isinstance(request, int)


class TestWaterWriteDue:
    NOW = datetime(2026, 11, 4, 12, tzinfo=timezone.utc)

    def test_change_of_one_kelvin(self):
        assert water_write_due(29.0, 29, 28, None, self.NOW)

    def test_rounding_noise_is_not_a_change(self):
        # 28.5 rounds to 29 but is only 0.5 K from the current 28.
        assert not water_write_due(28.5, 29, 28, None, self.NOW)

    def test_same_request(self):
        assert not water_write_due(28.2, 28, 28, None, self.NOW)

    def test_rate_limit_15_minutes(self):
        assert not water_write_due(30, 30, 28, self.NOW - timedelta(minutes=14), self.NOW)
        assert water_write_due(30, 30, 28, self.NOW - timedelta(minutes=15), self.NOW)

    def test_unknown_current(self):
        assert not water_write_due(30, 30, None, None, self.NOW)


class TestLeadBandAndCop:
    @pytest.mark.parametrize("outdoor,band", [(-3, "cold"), (7.99, "cold"), (8, "mid"), (11.9, "mid"), (12, "mild"), (20, "mild")])
    def test_bands(self, outdoor, band):
        assert lead_band(outdoor) == band

    def test_cop_rises_with_outdoor_and_is_bounded(self):
        assert expected_cop(-5) < expected_cop(7) < expected_cop(20)
        assert expected_cop(-40) == 1.5 and expected_cop(60) == 4.5


class TestChooseDhwWindow:
    FLAT = {hour: 5.0 for hour in range(24)}

    def test_cheapest_price(self):
        prices = {hour: 0.2 for hour in range(24)}
        prices[14] = prices[15] = 0.05
        assert choose_dhw_window(prices, self.FLAT, range(23))[0] == 14

    def test_warmer_hours_win_at_equal_price(self):
        prices = {hour: 0.1 for hour in range(24)}
        temperatures = {**self.FLAT, 15: 14.0, 16: 14.0}
        assert choose_dhw_window(prices, temperatures, range(23))[0] == 15

    def test_much_cheaper_cold_hours_still_win(self):
        prices = {hour: 0.2 for hour in range(24)}
        prices[3] = prices[4] = 0.02
        temperatures = {**self.FLAT, 3: -2.0, 4: -2.0, 15: 14.0, 16: 14.0}
        assert choose_dhw_window(prices, temperatures, range(23))[0] == 3

    def test_only_allowed_starts(self):
        prices = {hour: 0.2 for hour in range(24)}
        prices[3] = prices[4] = 0.01
        prices[20] = prices[21] = 0.1
        assert choose_dhw_window(prices, self.FLAT, range(12, 23))[0] == 20

    def test_missing_price_skips_window_and_none_when_empty(self):
        prices = {hour: 0.2 for hour in range(24) if hour != 23}
        assert choose_dhw_window(prices, self.FLAT, [22]) is None


class TestSimilarOverrides:
    def make(self, at, room="hall_kitchen", direction="raise"):
        return {"room": room, "at": at, "old": 21.5, "new": 23.0, "direction": direction}

    def test_same_room_direction_and_time_of_day(self):
        now = local(2026, 11, 4, 10, 30)
        history = [
            self.make(local(2026, 11, 1, 10, 0)),
            self.make(local(2026, 11, 2, 11, 15)),
            self.make(local(2026, 11, 3, 13, 0)),  # too far in time of day
            self.make(local(2026, 11, 3, 10, 0), room="bedroom"),
            self.make(local(2026, 11, 3, 10, 0), direction="lower"),
            self.make(local(2026, 10, 15, 10, 0)),  # older than 14 days
        ]
        assert len(similar_overrides(history, "hall_kitchen", "raise", now)) == 2

    def test_time_of_day_wraps_midnight(self):
        history = [self.make(local(2026, 11, 3, 23, 40))]
        assert len(similar_overrides(history, "hall_kitchen", "raise", local(2026, 11, 4, 0, 20))) == 1


# --- Schedule and boundaries ------------------------------------------------------------------


@pytest.fixture()
def schedule():
    return Schedule(time(12), time(9), time(22), TZ)


class TestSchedule:
    def test_weekday_and_weekend_windows(self, schedule):
        assert schedule.window(WED) == (local(2026, 11, 4, 12), local(2026, 11, 4, 22))
        assert schedule.window(SAT)[0] == local(2026, 11, 7, 9)
        assert schedule.window(date(2026, 11, 8))[0] == local(2026, 11, 8, 9)  # Sunday

    def test_early_start_after_midnight(self, schedule):
        assert schedule.early_start(WED, 675) == local(2026, 11, 4, 0, 45)
        assert schedule.early_start(SAT, 530) == local(2026, 11, 7, 0, 10)

    def test_early_start_before_midnight_on_the_previous_day(self, schedule):
        # Monday 12:00 minus 800 min = Sunday 22:40.
        assert schedule.early_start(MON, 800) == local(2026, 11, 8, 22, 40)

    def test_early_start_capped_at_previous_window_end(self, schedule):
        assert schedule.early_start(WED, 900) == local(2026, 11, 3, 22)

    def test_dst_spring_forward_uses_elapsed_minutes(self, schedule):
        day = date(2026, 3, 29)  # Sunday, 02:00 CET -> 03:00 CEST
        early = schedule.early_start(day, 530)
        assert (schedule.window(day)[0] - early) == timedelta(minutes=530)
        assert early.replace(tzinfo=None) == datetime(2026, 3, 28, 23, 10)

    def test_dst_fall_back_uses_elapsed_minutes(self, schedule):
        day = date(2026, 10, 25)  # Sunday, 03:00 CEST -> 02:00 CET
        early = schedule.early_start(day, 530)
        assert (schedule.window(day)[0] - early) == timedelta(minutes=530)
        assert early.replace(tzinfo=None) == datetime(2026, 10, 25, 1, 10)
        assert early.utcoffset() == timedelta(hours=2)

    def test_phase_transitions(self, schedule):
        def lead(day):
            return 675

        assert schedule.phase_at(local(2026, 11, 4, 0, 44), lead).kind == "night"
        phase = schedule.phase_at(local(2026, 11, 4, 0, 45), lead)
        assert (phase.kind, phase.day) == ("day", WED)
        assert schedule.phase_at(local(2026, 11, 4, 21, 59), lead).kind == "day"
        phase = schedule.phase_at(local(2026, 11, 4, 22, 0), lead)
        assert (phase.kind, phase.day) == ("night", WED)

    def test_phase_keys_are_monotonic(self, schedule):
        def lead(day):
            return 600

        keys = [schedule.phase_at(local(2026, 11, 4) + timedelta(hours=h), lead).key for h in range(0, 96, 1)]
        assert keys == sorted(keys)

    def test_capped_early_start_wins_the_tie_with_window_end(self, schedule):
        phase = schedule.phase_at(local(2026, 11, 3, 22, 0), lambda day: 900)
        assert (phase.kind, phase.day) == ("day", WED)

    def test_sunday_to_monday_uses_the_weekday_window(self, schedule):
        phase = schedule.phase_at(local(2026, 11, 9, 9, 30), lambda day: 120)
        assert phase.kind == "night"  # weekend start 09:00 does not apply on Monday
        phase = schedule.phase_at(local(2026, 11, 9, 10, 0), lambda day: 120)
        assert (phase.kind, phase.day) == ("day", MON)


class TestBoundaries:
    @pytest.mark.asyncio
    async def test_night_then_early_start_then_window_end(self, app):
        room(app, "hall_kitchen", target=23.0)
        await tick_at(app, 2026, 11, 4, 0, 30)  # first tick: applies the current (night) phase
        assert setpoints(app) == dict.fromkeys(app.args["rooms"], 21.5)

        # Outdoor 5 degC -> cold band -> weekday lead 675 min -> early start 00:45.
        await tick_at(app, 2026, 11, 4, 0, 45)
        assert setpoints(app)["hall_kitchen"] == 24.0
        assert setpoints(app)["bedroom"] == 21.5
        assert app.raise_active

        app.ha.services.clear()
        await tick_at(app, 2026, 11, 4, 12, 0)  # the window start itself writes nothing
        assert not app.ha.calls("climate/set_temperature")

        await tick_at(app, 2026, 11, 4, 22, 0)
        assert setpoints(app)["hall_kitchen"] == 21.5
        assert not app.raise_active

    @pytest.mark.asyncio
    async def test_weekend_early_start(self, app):
        await tick_at(app, 2026, 11, 7, 0, 5)
        assert setpoints(app)["hall_kitchen"] == 21.5
        await tick_at(app, 2026, 11, 7, 0, 10)  # 09:00 - 530 min
        assert setpoints(app)["hall_kitchen"] == 24.0

    @pytest.mark.asyncio
    async def test_mild_weather_shortens_the_lead_time(self, app):
        app.ha.put(app.args["sensors"]["outdoor"], "13")
        await tick_at(app, 2026, 11, 4, 7, 20)
        assert setpoints(app)["hall_kitchen"] == 21.5
        await tick_at(app, 2026, 11, 4, 7, 25)  # 12:00 - 275 min
        assert setpoints(app)["hall_kitchen"] == 24.0

    @pytest.mark.asyncio
    async def test_band_input_is_the_mean_of_the_hour_before_window_end(self, app):
        outdoor = app.args["sensors"]["outdoor"]
        app.ha.put(app.args["helpers"]["outdoor_offset"], "1.0")
        for minute, value in ((0, "14"), (30, "9"), (59, "7")):
            app.ha.put(outdoor, value)
            await tick_at(app, 2026, 11, 3, 21, minute)
        app.ha.put(outdoor, "2")  # a cold reading after the window end does not change the band
        await tick_at(app, 2026, 11, 3, 22, 0)
        # mean(15, 10, 8) = 11 -> mid band -> 545 min -> early start 02:55.
        assert app.band_outdoor["2026-11-04"] == pytest.approx(11.0)
        await tick_at(app, 2026, 11, 4, 2, 54)
        assert setpoints(app)["hall_kitchen"] == 21.5
        await tick_at(app, 2026, 11, 4, 2, 55)
        assert setpoints(app)["hall_kitchen"] == 24.0

    @pytest.mark.asyncio
    async def test_dst_day_boundary(self, app):
        # Fall-back Sunday: early start 09:00 CET - 530 min = 23:10 UTC = 01:10 CEST.
        app.clock[0] = datetime(2026, 10, 24, 23, 9, tzinfo=timezone.utc).astimezone(TZ)
        await app._tick({})
        assert setpoints(app)["hall_kitchen"] == 21.5
        app.clock[0] = datetime(2026, 10, 24, 23, 10, tzinfo=timezone.utc).astimezone(TZ)
        await app._tick({})
        assert setpoints(app)["hall_kitchen"] == 24.0

    @pytest.mark.asyncio
    async def test_restart_does_not_reapply_a_stored_boundary(self, app):
        await tick_at(app, 2026, 11, 4, 1, 0)
        status = app.ha.states[app.args["status_entity"]]
        room(app, "hall_kitchen", target=22.0)  # manual change
        restarted = make_app(app.ha)
        restarted._init_state()
        await restarted._load_status()
        assert restarted.last_phase_key == status["attributes"]["phase_key"]
        await tick_at(restarted, 2026, 11, 4, 1, 5)
        assert setpoints(app)["hall_kitchen"] == 22.0


class TestManualOverrides:
    @pytest.mark.asyncio
    async def test_override_holds_until_the_next_boundary(self, app):
        await tick_at(app, 2026, 11, 4, 1, 0)
        assert setpoints(app)["hall_kitchen"] == 24.0
        await app._on_target_change("", "temperature", 21.5, 24.0, {"room": "hall_kitchen"})  # our write
        assert app.overrides == []

        room(app, "hall_kitchen", target=22.5)
        await app._on_target_change("", "temperature", 24.0, 22.5, {"room": "hall_kitchen"})
        assert len(app.overrides) == 1
        override = app.overrides[0]
        assert (override["room"], override["old"], override["new"]) == ("hall_kitchen", 24.0, 22.5)

        await tick_at(app, 2026, 11, 4, 15, 0)
        assert setpoints(app)["hall_kitchen"] == 22.5
        await tick_at(app, 2026, 11, 4, 22, 0)
        assert setpoints(app)["hall_kitchen"] == 21.5

    @pytest.mark.asyncio
    async def test_override_before_early_start_is_overwritten_by_it(self, app):
        await tick_at(app, 2026, 11, 4, 0, 30)
        room(app, "hall_kitchen", target=20.0)  # below the floor: the owner's choice, not fought
        await tick_at(app, 2026, 11, 4, 0, 40)
        assert setpoints(app)["hall_kitchen"] == 20.0
        await tick_at(app, 2026, 11, 4, 0, 45)
        assert setpoints(app)["hall_kitchen"] == 24.0

    @pytest.mark.asyncio
    async def test_repeated_pattern_suggests_once_and_changes_nothing(self, app):
        for day in (2, 3, 4):
            app.clock[0] = local(2026, 11, day, 10, 10 + day)
            await app._on_target_change("", "temperature", 24.0, 25.0, {"room": "hall_kitchen"})
        notes = app.ha.calls("persistent_notification/create")
        assert len(notes) == 1
        assert "hall_kitchen" in notes[0][2]["message"] and "Nothing was changed" in notes[0][2]["message"]
        app.clock[0] = local(2026, 11, 5, 10, 20)
        await app._on_target_change("", "temperature", 24.0, 25.0, {"room": "hall_kitchen"})
        assert len(app.ha.calls("persistent_notification/create")) == 1
        assert not app.ha.calls("climate/set_temperature")

    @pytest.mark.asyncio
    async def test_unavailable_values_are_not_overrides(self, app):
        await app._on_target_change("", "temperature", None, 22.0, {"room": "bedroom"})
        await app._on_target_change("", "temperature", "unavailable", 22.0, {"room": "bedroom"})
        assert app.overrides == []


class TestAway:
    @pytest.mark.asyncio
    async def test_away_on_sets_every_room_and_boundaries_do_not_write(self, app):
        await tick_at(app, 2026, 11, 4, 1, 0)
        app.ha.put(app.args["helpers"]["away"], "on")
        await app._on_away()
        assert setpoints(app) == dict.fromkeys(app.args["rooms"], 21.5)
        app.ha.services.clear()
        await tick_at(app, 2026, 11, 5, 1, 0)  # next early start while away
        assert not app.ha.calls("climate/set_temperature")
        assert app.ha.states[app.args["status_entity"]]["state"] == "away"

    @pytest.mark.asyncio
    async def test_away_off_writes_the_current_schedule(self, app):
        app.ha.put(app.args["helpers"]["away"], "on")
        await tick_at(app, 2026, 11, 4, 0, 30)
        app.ha.put(app.args["helpers"]["away"], "off")
        app.clock[0] = local(2026, 11, 4, 1, 0)  # early start (00:45) already passed
        await app._on_away_off()
        assert setpoints(app)["hall_kitchen"] == 24.0
        app.ha.services.clear()
        await tick_at(app, 2026, 11, 4, 1, 1)
        assert not app.ha.calls("climate/set_temperature")


class TestRunLogic:
    @pytest.mark.asyncio
    async def test_on_when_a_room_is_below_setpoint_minus_hysteresis(self, app):
        room(app, "bedroom", current=21.3)  # 0.2 K below: inside the hysteresis
        await tick_at(app, 2026, 11, 4, 0, 30)
        assert app.ha.states["input_select.heishamon_heatmode"]["state"] == "DHW"
        room(app, "bedroom", current=21.1)
        await tick_at(app, 2026, 11, 4, 0, 31)
        assert app.ha.calls("set_state", "input_select.heishamon_heatmode")[-1][2] == "Heat+DHW"

    @pytest.mark.asyncio
    async def test_min_on_and_min_off(self, app):
        room(app, "hall_kitchen", current=24.5)  # satisfied by day and night
        room(app, "bedroom", current=21.0)
        await tick_at(app, 2026, 11, 4, 0, 30)
        assert app.ha.states["input_select.heishamon_heatmode"]["state"] == "Heat+DHW"
        room(app, "bedroom", current=21.6)
        await tick_at(app, 2026, 11, 4, 0, 59)  # 29 min on
        assert app.ha.states["input_select.heishamon_heatmode"]["state"] == "Heat+DHW"
        await tick_at(app, 2026, 11, 4, 1, 0)
        assert app.ha.states["input_select.heishamon_heatmode"]["state"] == "DHW"
        room(app, "bedroom", current=21.0)
        await tick_at(app, 2026, 11, 4, 1, 19)  # 19 min off
        assert app.ha.states["input_select.heishamon_heatmode"]["state"] == "DHW"
        await tick_at(app, 2026, 11, 4, 1, 20)
        assert app.ha.states["input_select.heishamon_heatmode"]["state"] == "Heat+DHW"

    @pytest.mark.asyncio
    async def test_stays_on_until_every_room_reaches_setpoint(self, app):
        app.ha.put("input_select.heishamon_heatmode", "Heat+DHW")
        room(app, "office_t", current=21.4)  # within hysteresis but not at setpoint
        await tick_at(app, 2026, 11, 4, 0, 30)
        assert app.ha.states["input_select.heishamon_heatmode"]["state"] == "Heat+DHW"

    @pytest.mark.asyncio
    async def test_garbage_and_off_rooms_are_ignored(self, app):
        room(app, "bedroom", current="unavailable")
        app.ha.put(app.args["rooms"]["office_a"], "off", current_temperature=15.0)
        await tick_at(app, 2026, 11, 4, 0, 30)
        assert app.ha.states["input_select.heishamon_heatmode"]["state"] == "DHW"

    @pytest.mark.asyncio
    async def test_other_modes_are_left_alone(self, app):
        app.ha.put("input_select.heishamon_heatmode", "Cool+DHW")
        room(app, "bedroom", current=19.0)
        await tick_at(app, 2026, 11, 4, 0, 30)
        assert not app.ha.calls("set_state", "input_select.heishamon_heatmode")

    @pytest.mark.asyncio
    async def test_starts_per_day_alarm(self, app):
        counter = app.args["sensors"]["compressor_starts"]
        await tick_at(app, 2026, 11, 4, 0, 30)
        app.ha.put(counter, str(1000 + MAX_STARTS_PER_DAY))
        await tick_at(app, 2026, 11, 4, 12, 0)
        assert not any("compressor starts" in m for m in app.ha.messages())
        app.ha.put(counter, str(1000 + MAX_STARTS_PER_DAY + 1))
        await tick_at(app, 2026, 11, 4, 13, 0)
        await tick_at(app, 2026, 11, 4, 14, 0)
        assert sum("compressor starts" in m for m in app.ha.messages()) == 1


class TestWaterTemperature:
    def water_writes(self, app):
        return [c[2]["value"] for c in app.ha.calls("input_number/set_value", "input_number.heishamon_heatshift_z1")]

    @pytest.mark.asyncio
    async def test_curve_write_is_an_integer_through_the_helper(self, app):
        app.ha.put(app.args["sensors"]["outdoor"], "-2")  # 30.95 -> 31
        await tick_at(app, 2026, 11, 4, 0, 30)
        assert self.water_writes(app) == [31]

    @pytest.mark.asyncio
    async def test_no_write_outside_direct_mode(self, app):
        app.ha.put(app.args["sensors"]["outdoor"], "-5")
        for mode in ("0", "unavailable", None):
            app.ha.put(app.args["sensors"]["heating_mode"], mode)
            await tick_at(app, 2026, 11, 4, 0, 30)
        assert self.water_writes(app) == []

    @pytest.mark.asyncio
    @pytest.mark.parametrize("entity_role,value", [("outdoor", "unknown"), ("outdoor", ""), ("z1_request", "unavailable")])
    async def test_no_write_on_garbage(self, app, entity_role, value):
        app.ha.put(app.args["sensors"]["outdoor"], "-5")
        app.ha.put(app.args["sensors"][entity_role], value)
        await tick_at(app, 2026, 11, 4, 0, 30)
        assert self.water_writes(app) == []

    @pytest.mark.asyncio
    async def test_invalid_curve_points_block_the_write(self, app):
        app.ha.put(app.args["sensors"]["outdoor"], "-5")
        app.ha.put(app.args["helpers"]["curve_cold_outdoor"], "20")
        await tick_at(app, 2026, 11, 4, 0, 30)
        assert self.water_writes(app) == []

    @pytest.mark.asyncio
    async def test_rate_limit_and_minimum_change(self, app):
        outdoor, request = app.args["sensors"]["outdoor"], app.args["sensors"]["z1_request"]
        app.ha.put(outdoor, "-5")
        await tick_at(app, 2026, 11, 4, 0, 30)
        app.ha.put(request, "32")
        app.ha.put(outdoor, "15")
        await tick_at(app, 2026, 11, 4, 0, 44)
        assert self.water_writes(app) == [32]
        await tick_at(app, 2026, 11, 4, 0, 45)
        assert self.water_writes(app) == [32, 25]
        app.ha.put(request, "25")
        app.ha.put(outdoor, "13.7")  # curve 25.455: under 1 K from 25
        await tick_at(app, 2026, 11, 4, 1, 30)
        assert self.water_writes(app) == [32, 25]

    @pytest.mark.asyncio
    async def test_raise_from_early_start_until_the_hall_reaches_setpoint(self, app):
        app.ha.put(app.args["sensors"]["outdoor"], "-5")
        app.ha.put(app.args["sensors"]["z1_request"], "32")
        app.ha.put(app.args["helpers"]["early_start_raise"], "2")
        room(app, "hall_kitchen", current=21.6)
        await tick_at(app, 2026, 11, 4, 0, 30)
        assert self.water_writes(app) == []
        await tick_at(app, 2026, 11, 4, 0, 45)  # early start: hall setpoint 24, raise on
        assert self.water_writes(app) == [34]
        app.ha.put(app.args["sensors"]["z1_request"], "34")
        room(app, "hall_kitchen", current=24.0)
        await tick_at(app, 2026, 11, 4, 1, 0)
        assert not app.raise_active
        assert self.water_writes(app) == [34, 32]

    @pytest.mark.asyncio
    async def test_dry_run_logs_only(self, app):
        app.args["actuate"]["water_temperature"] = False
        app.ha.put(app.args["sensors"]["outdoor"], "-5")
        await tick_at(app, 2026, 11, 4, 0, 30)
        assert self.water_writes(app) == []


class TestDhw:
    def prices(self, app, cheap):
        attributes = {f"price_{hour:02d}h": 0.2 for hour in range(24)}
        attributes.update({f"price_{hour:02d}h": price for hour, price in cheap.items()})
        app.ha.put(app.args["sensors"]["pvpc_price"], "0.2", **attributes)

    def warm_afternoon_history(self, app):
        base = local(2026, 11, 3, 0, 0)
        app.ha.history = [
            [
                {"state": "14" if 13 <= hour <= 16 else "5", "last_changed": (base + timedelta(hours=hour)).isoformat()}
                for hour in range(24)
            ]
        ]

    @pytest.mark.asyncio
    async def test_window_avoids_the_warm_up_and_prefers_warm_hours(self, app):
        # 03-05 is the cheapest but inside the 00:45-12:00 warm-up; 14-16 and 20-22 cost the same.
        self.prices(app, {3: 0.01, 4: 0.01, 14: 0.1, 15: 0.1, 20: 0.1, 21: 0.1})
        self.warm_afternoon_history(app)
        await tick_at(app, 2026, 11, 4, 0, 1)
        assert app.dhw_plan["start"] == local(2026, 11, 4, 14)
        assert app.dhw_plan["end"] == local(2026, 11, 4, 16)
        assert any("DHW window 14:00\\-16:00" in m for m in app.ha.messages())

    @pytest.mark.asyncio
    async def test_force_dhw_in_the_window_and_off_at_its_end(self, app):
        self.prices(app, {14: 0.1, 15: 0.1})
        await tick_at(app, 2026, 11, 4, 0, 1)
        await tick_at(app, 2026, 11, 4, 13, 59)
        assert not app.ha.calls("switch/turn_on")
        await tick_at(app, 2026, 11, 4, 14, 0)
        assert app.ha.calls("switch/turn_on", "switch.aquarea_force_dhw_mode")
        assert not app.ha.calls("input_number/set_value", "input_number.heishamon_tank_temp")
        await tick_at(app, 2026, 11, 4, 16, 0)
        assert app.ha.calls("switch/turn_off", "switch.aquarea_force_dhw_mode")
        assert app.dhw_done == "2026-11-04"
        app.ha.services.clear()
        await tick_at(app, 2026, 11, 4, 20, 0)
        assert not app.ha.calls("switch/turn_on")

    @pytest.mark.asyncio
    async def test_tank_target_capped_at_50(self, app):
        self.prices(app, {14: 0.1, 15: 0.1})
        app.ha.put(app.args["helpers"]["dhw_target"], "60")  # beyond the helper range on purpose
        await tick_at(app, 2026, 11, 4, 14, 0)
        writes = app.ha.calls("input_number/set_value", "input_number.heishamon_tank_temp")
        assert [c[2]["value"] for c in writes] == [TANK_MAX]
        assert not app.ha.calls("switch/turn_on")
        app.ha.put(app.args["sensors"]["tank_target"], str(TANK_MAX))  # the unit confirms
        await tick_at(app, 2026, 11, 4, 14, 1)
        assert app.ha.calls("switch/turn_on", "switch.aquarea_force_dhw_mode")

    @pytest.mark.asyncio
    async def test_tank_target_above_50_blocks_forcing(self, app):
        self.prices(app, {14: 0.1, 15: 0.1})
        app.ha.put(app.args["sensors"]["tank_target"], "55")
        await tick_at(app, 2026, 11, 4, 14, 0)  # writes 49, the unit does not confirm
        await tick_at(app, 2026, 11, 4, 14, 1)
        await tick_at(app, 2026, 11, 4, 14, 2)
        assert not app.ha.calls("switch/turn_on")
        assert sum("DHW not forced" in m for m in app.ha.messages()) == 1

    @pytest.mark.asyncio
    async def test_force_heater_blocks_forcing(self, app):
        self.prices(app, {14: 0.1, 15: 0.1})
        app.ha.put(app.args["sensors"]["force_heater"], "on")
        await tick_at(app, 2026, 11, 4, 14, 0)
        assert not app.ha.calls("switch/turn_on")

    @pytest.mark.asyncio
    async def test_never_touches_sterilization_or_heater_commands(self, app):
        self.prices(app, {14: 0.1, 15: 0.1})
        for hour in (0, 14, 15, 16):
            await tick_at(app, 2026, 11, 4, hour, 0)
        touched = {c[1] for c in app.ha.services if c[1]}
        assert not any("steril" in e or "heater" in e for e in touched)

    @pytest.mark.asyncio
    async def test_missing_prices_retry_without_forcing(self, app):
        app.ha.put(app.args["sensors"]["pvpc_price"], "unavailable", **{f"price_{h:02d}h": None for h in range(24)})
        await tick_at(app, 2026, 11, 4, 0, 1)
        assert app.dhw_plan is None
        assert any("no full PVPC price curve" in m for m in app.ha.messages())
        await tick_at(app, 2026, 11, 4, 0, 5)
        assert app.dhw_last_attempt == local(2026, 11, 4, 0, 1)  # retry after 10 min only
        self.prices(app, {20: 0.05, 21: 0.05})
        await tick_at(app, 2026, 11, 4, 0, 11)
        assert app.dhw_plan["start"] == local(2026, 11, 4, 20)

    @pytest.mark.asyncio
    async def test_plan_late_in_the_day_only_uses_remaining_hours(self, app):
        self.prices(app, {14: 0.01, 15: 0.01, 21: 0.1, 22: 0.1})
        await tick_at(app, 2026, 11, 4, 18, 30)
        assert app.dhw_plan["start"] == local(2026, 11, 4, 21)


class TestFailureBehaviour:
    @pytest.mark.asyncio
    async def test_initialize_registers_listeners_and_ticks(self, app):
        listeners, timers = [], []

        async def listen_state(callback, entity, **kwargs):
            listeners.append((callback.__name__, entity, kwargs))

        async def run_every(callback, start, interval):
            timers.append((callback.__name__, start, interval))

        async def create_task(coroutine):
            await coroutine

        app.listen_state, app.run_every, app.create_task = listen_state, run_every, create_task
        await app.initialize()
        rooms = [entity for name, entity, kwargs in listeners if name == "_on_target_change"]
        assert sorted(rooms) == sorted(app.args["rooms"].values())
        assert ("_tick", "now", 60) in timers
        assert app.ha.states[app.args["status_entity"]]["attributes"]["phase"] == "night"

    @pytest.mark.asyncio
    async def test_startup_warns_when_disabled_with_heating_off(self, app):
        app.ha.put(app.args["helpers"]["enable"], "off")
        app.ha.put(app.args["rooms"]["bedroom"], "auto")
        await app._startup_checks()
        messages = app.ha.messages()
        assert any("space heating is off" in m for m in messages)
        assert any("device program" in m for m in messages)
        assert not app.ha.calls("set_state", "input_select.heishamon_heatmode")

    @pytest.mark.asyncio
    async def test_disable_restores_heating_and_stops_writing(self, app):
        await app._on_disable()
        assert app.ha.calls("set_state", "input_select.heishamon_heatmode")[-1][2] == "Heat+DHW"
        assert any("mode is now manual" in m for m in app.ha.messages())
        app.ha.put(app.args["helpers"]["enable"], "off")
        app.ha.services.clear()
        room(app, "bedroom", current=18.0)
        await tick_at(app, 2026, 11, 4, 1, 0)
        assert [c for c in app.ha.services if c[0] != "set_state" or c[1] != app.args["status_entity"]] == []

    @pytest.mark.asyncio
    async def test_terminate_changes_nothing(self, app):
        await app.terminate()
        assert app.ha.services == []

    @pytest.mark.asyncio
    async def test_a_failing_step_does_not_stop_the_others(self, app):
        async def boom(*args, **kwargs):
            raise RuntimeError("thermostat API down")

        app._update_setpoints = boom
        room(app, "bedroom", current=20.0)
        await tick_at(app, 2026, 11, 4, 0, 30)
        assert app.ha.states["input_select.heishamon_heatmode"]["state"] == "Heat+DHW"
        assert not app.busy

    @pytest.mark.asyncio
    async def test_helper_garbage_falls_back_to_defaults(self, app):
        app.ha.put(app.args["helpers"]["day_minimum"], "unknown")
        await tick_at(app, 2026, 11, 4, 1, 0)
        assert setpoints(app)["hall_kitchen"] == 24.0

    @pytest.mark.asyncio
    async def test_seeding_writes_defaults_once(self, app):
        app.ha.put(app.args["helpers"]["seeded"], "off")
        await app._seed_helpers()
        assert app.ha.states[app.args["helpers"]["lead_weekday_cold"]]["state"] == "675"
        assert app.ha.calls("input_datetime/set_datetime", app.args["helpers"]["weekend_start"])[0][2]["time"] == "09:00:00"
        assert app.ha.states[app.args["helpers"]["seeded"]]["state"] == "on"
        app.ha.services.clear()
        await app._seed_helpers()
        assert app.ha.services == []

    def test_watchdog_restores_heat_dhw_only_when_appdaemon_is_silent(self):
        package = yaml.safe_load(HA_PACKAGE.read_text())
        (watchdog,) = package["automation"]
        conditions = watchdog["condition"]
        assert {"condition": "state", "entity_id": "input_boolean.comfort_control_enable", "state": "on"} in conditions
        assert {"condition": "state", "entity_id": "sensor.aquarea_mode", "state": "DHW"} in conditions
        assert any("last_appdaemon_connection" in c.get("value_template", "") for c in conditions)
        actions = watchdog["action"]
        assert actions[0]["target"]["entity_id"] == "input_select.heishamon_heatmode"
        assert actions[0]["data"]["option"] == "Heat+DHW"
        # The watchdog never touches the water temperature: the unit keeps the last request.
        assert "heatshift" not in HA_PACKAGE.read_text().split("automation:")[1]

    def test_every_configured_helper_exists_in_the_package(self, app):
        package = yaml.safe_load(HA_PACKAGE.read_text())
        defined = {f"{domain}.{name}" for domain in ("input_boolean", "input_number", "input_datetime") for name in package[domain]}
        assert set(app.args["helpers"].values()) == defined
        assert set(HELPER_DEFAULTS) <= set(app.args["helpers"])

    def test_command_automations_refuse_non_numeric_payloads(self):
        automations = {a["alias"]: a for a in yaml.safe_load(HEISHAMON.read_text())["automation"]}
        for alias in ("Set Z1 Heat Temperature", "Set tank target temperature"):
            assert "is_number(trigger.to_state.state)" in automations[alias]["condition"][0]["value_template"]
