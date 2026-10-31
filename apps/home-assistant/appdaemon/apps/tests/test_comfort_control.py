from datetime import date, datetime, time, timedelta, timezone
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

import pytest
import pytz
import yaml
from comfort_control import (
    HELPER_DEFAULTS,
    HELPER_RANGES,
    MAX_STARTS_PER_DAY,
    TANK_MAX,
    ComfortControl,
    Schedule,
    choose_dhw_window,
    curve_water,
    day_hour_slots,
    expected_cop,
    lead_band,
    parse_number,
    similar_overrides,
    water_request,
    water_write_due,
)

from tests.ha_template import condition_true

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
        self.put(h["water_dry_run"], "off")  # the owner has done the first-write check
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
        clock = getattr(self.app, "clock", None)
        updated = (clock[0] if clock else local(2026, 11, 4, 0, 0)).isoformat()
        self.states[entity] = {
            "state": state,
            "attributes": {**previous, **attributes},
            "last_changed": None,
            "last_updated": updated,
        }

    def attr(self, entity, name, value):
        self.states[entity]["attributes"][name] = value

    async def get_state(self, entity, attribute=None, **kwargs):
        item = self.states.get(entity)
        if item is None and entity == self.app.args["sensors"]["operating_mode"]:
            # Legacy test harness has synchronous successful mode commands by default;
            # error tests set the sensor explicitly to simulate missing MQTT acknowledgements.
            item = self.states.get(self.app.args["heat_mode"]["entity"])
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
        elif service == "input_text/set_value":
            self.put(entity, data["value"])
        elif service in ("input_boolean/turn_on", "switch/turn_on"):
            self.put(entity, "on")
        elif service in ("input_boolean/turn_off", "switch/turn_off"):
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
        await tick_at(app, 2026, 11, 4, 1, 1)  # the thermostat confirmed the write: boundary stored
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
    async def test_rooms_switched_off_are_ignored(self, app):
        app.ha.put(app.args["rooms"]["office_a"], "off", current_temperature=15.0)
        await tick_at(app, 2026, 11, 4, 0, 30)
        assert app.ha.states["input_select.heishamon_heatmode"]["state"] == "DHW"

    @pytest.mark.asyncio
    @pytest.mark.parametrize("state,current", [("heat", "unavailable"), ("unavailable", 21.6), ("unknown", 21.6)])
    async def test_a_room_without_a_reading_switches_heating_on(self, app, state, current):
        # Review fix 1: a silent thermostat may be cold, so its room counts as demand.
        app.ha.put(app.args["rooms"]["bedroom"], state, current_temperature=current)
        await tick_at(app, 2026, 11, 4, 0, 30)
        assert app.ha.states["input_select.heishamon_heatmode"]["state"] == "Heat+DHW"

    @pytest.mark.asyncio
    async def test_never_off_while_a_room_is_missing_and_on_after_min_off(self, app):
        modes = app.ha.calls
        app.ha.put("input_select.heishamon_heatmode", "Heat+DHW")
        for entity in app.args["rooms"].values():
            app.ha.put(entity, "unavailable")
        for minute in range(30, 120):
            await tick_at(app, 2026, 11, 4, minute // 60, minute % 60)
        assert not modes("set_state", "input_select.heishamon_heatmode")
        # Off by hand while every thermostat is silent: back on once the minimum off time passed.
        app.ha.put("input_select.heishamon_heatmode", "DHW")
        app.last_mode_change = local(2026, 11, 4, 2, 0)
        await tick_at(app, 2026, 11, 4, 2, 19)
        assert app.ha.states["input_select.heishamon_heatmode"]["state"] == "DHW"
        await tick_at(app, 2026, 11, 4, 2, 20)
        assert app.ha.states["input_select.heishamon_heatmode"]["state"] == "Heat+DHW"

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
        app.ha.put(app.args["helpers"]["curve_cold_outdoor"], "10")  # each in range, but crossed
        app.ha.put(app.args["helpers"]["curve_mild_outdoor"], "5")
        await tick_at(app, 2026, 11, 4, 0, 30)
        assert self.water_writes(app) == []

    @pytest.mark.asyncio
    @pytest.mark.parametrize("state", ["on", "unavailable", None])
    async def test_water_dry_run_helper_logs_only(self, app, state):
        # Review fix 8: anything but "off" keeps the water request a dry run.
        app.ha.put(app.args["helpers"]["water_dry_run"], state)
        app.ha.put(app.args["sensors"]["outdoor"], "-5")
        await tick_at(app, 2026, 11, 4, 0, 30)
        assert self.water_writes(app) == []
        assert app.last_water_write == local(2026, 11, 4, 0, 30)  # logged and rate limited all the same

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
        app.ha.put(app.args["helpers"]["dhw_target"], "50")
        await tick_at(app, 2026, 11, 4, 14, 0)
        writes = app.ha.calls("input_number/set_value", "input_number.heishamon_tank_temp")
        assert [c[2]["value"] for c in writes] == [TANK_MAX]
        assert not app.ha.calls("switch/turn_on")
        app.ha.put(app.args["sensors"]["tank_target"], str(TANK_MAX))  # the unit confirms
        await tick_at(app, 2026, 11, 4, 14, 1)
        assert app.ha.calls("switch/turn_on", "switch.aquarea_force_dhw_mode")

    @pytest.mark.asyncio
    async def test_tank_target_helper_above_its_range_uses_the_default(self, app):
        self.prices(app, {14: 0.1, 15: 0.1})
        app.ha.put(app.args["helpers"]["dhw_target"], "60")  # beyond the helper range on purpose
        app.ha.put(app.args["sensors"]["tank_target"], "45")
        await tick_at(app, 2026, 11, 4, 14, 0)
        writes = app.ha.calls("input_number/set_value", "input_number.heishamon_tank_temp")
        assert [c[2]["value"] for c in writes] == [HELPER_DEFAULTS["dhw_target"]]
        assert HELPER_DEFAULTS["dhw_target"] <= TANK_MAX
        app.ha.put(app.args["sensors"]["tank_target"], str(HELPER_DEFAULTS["dhw_target"]))
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
        await tick_at(app, 2026, 11, 4, 1, 1)
        assert app.dhw_plan is None
        assert any("no full PVPC price curve" in m for m in app.ha.messages())
        await tick_at(app, 2026, 11, 4, 1, 5)
        assert app.dhw_last_attempt == local(2026, 11, 4, 1, 1)  # retry after 10 min only
        self.prices(app, {20: 0.05, 21: 0.05})
        await tick_at(app, 2026, 11, 4, 1, 11)
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

    def test_watchdog_restores_heat_dhw_when_the_comfort_heartbeat_is_stale(self):
        package = yaml.safe_load(HA_PACKAGE.read_text())
        (watchdog,) = package["automation"]
        conditions = watchdog["condition"]
        assert {"condition": "state", "entity_id": "input_boolean.comfort_control_enable", "state": "on"} in conditions
        assert {"condition": "state", "entity_id": "sensor.aquarea_mode", "state": "DHW"} in conditions
        # Review fix 5: the app's own per-tick heartbeat, not AppDaemon's connection sensor.
        (heartbeat,) = [c for c in conditions if "heartbeat" in c.get("alias", "")]
        template = heartbeat["value_template"]
        assert "sensor.comfort_control_heartbeat" in template
        now = local(2026, 11, 4, 12, 0)
        entity = "sensor.comfort_control_heartbeat"

        def stale(store):
            return condition_true(template, store, now)

        assert stale({})
        assert stale({entity: {"state": "unavailable", "last_updated": now}})
        assert stale({entity: {"state": "x", "last_updated": now - timedelta(minutes=15)}})
        assert not stale({entity: {"state": "x", "last_updated": now - timedelta(minutes=14)}})
        actions = watchdog["action"]
        assert actions[0]["target"]["entity_id"] == "input_select.heishamon_heatmode"
        assert actions[0]["data"]["option"] == "Heat+DHW"
        # The watchdog never touches the water temperature: the unit keeps the last request.
        assert "heatshift" not in HA_PACKAGE.read_text().split("automation:")[1]

    @pytest.mark.asyncio
    async def test_heartbeat_written_by_every_control_tick_only(self, app):
        heartbeat = app.args["heartbeat_entity"]
        await tick_at(app, 2026, 11, 4, 0, 30)
        assert app.ha.states[heartbeat]["state"] == local(2026, 11, 4, 0, 30).isoformat()

        async def boom(now):
            raise RuntimeError("run logic broken")

        app._update_run = boom  # the run step failing is as bad as no tick: no heartbeat
        await tick_at(app, 2026, 11, 4, 0, 31)
        assert app.ha.states[heartbeat]["state"] == local(2026, 11, 4, 0, 30).isoformat()

    def test_every_configured_helper_exists_in_the_package(self, app):
        package = yaml.safe_load(HA_PACKAGE.read_text())
        defined = {f"{domain}.{name}" for domain in ("input_boolean", "input_number", "input_datetime", "input_text") for name in package[domain]}
        assert set(app.args["helpers"].values()) == defined
        assert set(HELPER_DEFAULTS) <= set(app.args["helpers"])

    @pytest.mark.parametrize(
        "heating_mode,value,published",
        [
            ("1", "26.0", True),
            ("1", "20", True),
            ("1", "35.0", True),
            ("1", "36.0", False),
            ("1", "19", False),
            ("1", "3", False),  # a shift in direct mode would be a 3 degC water request
            ("0", "3.0", True),
            ("0", "-5", True),
            ("0", "26.0", False),  # an absolute value in curve mode is a +26 K shift
            ("unavailable", "26.0", False),
            (None, "26.0", False),
            ("1", "", False),
            ("1", "unknown", False),
            ("1", "unavailable", False),
        ],
    )
    def test_z1_command_publishes_only_values_valid_for_the_heating_mode(self, heating_mode, value, published):
        # Review fix 7: the HA guard in front of SetZ1HeatRequestTemperature.
        automations = {a["alias"]: a for a in yaml.safe_load(HEISHAMON.read_text())["automation"]}
        template = automations["Set Z1 Heat Temperature"]["condition"][0]["value_template"]
        store = {} if heating_mode is None else {"sensor.aquarea_heating_mode": {"state": heating_mode}}
        trigger = SimpleNamespace(to_state=SimpleNamespace(state=value))
        now = local(2026, 11, 4, 12, 0)
        assert condition_true(template, store, now, variables={"trigger": trigger}) is published

    @pytest.mark.parametrize(
        "value,published", [("49.0", True), ("40", True), ("50", True), ("51", False), ("65.0", False), ("39", False), ("", False), ("unknown", False)]
    )
    def test_tank_command_publishes_only_40_to_50(self, value, published):
        automations = {a["alias"]: a for a in yaml.safe_load(HEISHAMON.read_text())["automation"]}
        template = automations["Set tank target temperature"]["condition"][0]["value_template"]
        trigger = SimpleNamespace(to_state=SimpleNamespace(state=value))
        assert condition_true(template, {}, local(2026, 11, 4, 12, 0), variables={"trigger": trigger}) is published


# --- Fixes from the safety review of PR #4699 ---------------------------------------------------


def force_dhw_on(app):
    return app.ha.states[app.args["commands"]["force_dhw"]]["state"] == "on"


def set_prices(app, cheap, day=None, **extra):
    attributes = {f"price_{hour:02d}h": 0.2 for hour in range(24)}
    attributes.update({f"price_{hour:02d}h": price for hour, price in cheap.items()})
    attributes.update(extra)
    app.ha.put(app.args["sensors"]["pvpc_price"], "0.2", **attributes)
    if day is not None:
        app.ha.states[app.args["sensors"]["pvpc_price"]]["last_updated"] = TZ.localize(datetime.combine(day, time(0, 0, 5))).isoformat()


class TestForceDhwRelease:
    """Review fix 2: force DHW is released on every path."""

    @pytest.mark.asyncio
    async def test_no_window_runs_past_23(self, app):
        set_prices(app, {22: 0.01, 23: 0.01, 21: 0.05})
        await tick_at(app, 2026, 11, 4, 0, 1)
        assert app.dhw_plan["start"] == local(2026, 11, 4, 21)  # 21-23, not the cheaper 22-24
        assert app.dhw_plan["end"] <= local(2026, 11, 4, 23)

    @pytest.mark.asyncio
    async def test_a_window_left_from_yesterday_is_finished_at_rollover(self, app):
        app.dhw_plan = {"day": date(2026, 11, 3), "start": local(2026, 11, 3, 22), "end": local(2026, 11, 4, 0), "forced": True}
        app.ha.put(app.args["commands"]["force_dhw"], "on")
        await tick_at(app, 2026, 11, 4, 0, 0)
        assert not force_dhw_on(app)
        assert app.dhw_done == "2026-11-03"

    @pytest.mark.asyncio
    async def test_restart_mid_window_keeps_the_window_and_releases_at_its_end(self, app):
        set_prices(app, {14: 0.1, 15: 0.1})
        await tick_at(app, 2026, 11, 4, 0, 1)
        await tick_at(app, 2026, 11, 4, 14, 0)
        assert force_dhw_on(app)
        restarted = make_app(app.ha)
        restarted.clock[0] = local(2026, 11, 4, 14, 30)
        await restarted._load_status()
        await restarted._release_force_dhw_safely(restarted._now(), startup=True)
        assert force_dhw_on(restarted)  # still inside the forced window
        await tick_at(restarted, 2026, 11, 4, 15, 0)
        assert force_dhw_on(restarted)
        await tick_at(restarted, 2026, 11, 4, 16, 0)
        assert not force_dhw_on(restarted)
        restarted.ha.services.clear()
        for hour in (17, 20, 21, 22):
            await tick_at(restarted, 2026, 11, 4, hour, 0)
        assert not restarted.ha.calls("switch/turn_on")  # one window per day

    @pytest.mark.asyncio
    async def test_restart_without_a_stored_plan_releases_at_startup(self, app):
        app.ha.put(app.args["commands"]["force_dhw"], "on")
        app.clock[0] = local(2026, 11, 4, 14, 30)
        await app._load_status()
        await app._release_force_dhw_safely(app._now(), startup=True)
        assert not force_dhw_on(app)  # owner unknown, no forced window: released and logged

    @pytest.mark.asyncio
    async def test_disable_releases_force_dhw(self, app):
        set_prices(app, {14: 0.1, 15: 0.1})
        await tick_at(app, 2026, 11, 4, 14, 0)
        assert force_dhw_on(app)
        app.ha.put(app.args["helpers"]["enable"], "off")
        await app._on_disable()
        assert not force_dhw_on(app)

    @pytest.mark.asyncio
    async def test_manual_force_is_held_like_a_manual_setpoint(self, app):
        set_prices(app, {14: 0.1, 15: 0.1})
        await tick_at(app, 2026, 11, 4, 0, 1)
        app.ha.put(app.args["commands"]["force_dhw"], "on")  # by hand
        await tick_at(app, 2026, 11, 4, 9, 0)
        assert force_dhw_on(app)
        await tick_at(app, 2026, 11, 4, 10, 59)
        assert force_dhw_on(app)
        assert not app.ha.calls("switch/turn_off")

    @pytest.mark.asyncio
    async def test_manual_force_is_capped_at_two_hours(self, app, capsys):
        set_prices(app, {14: 0.1, 15: 0.1})
        await tick_at(app, 2026, 11, 4, 0, 1)
        app.ha.put(app.args["commands"]["force_dhw"], "on")
        await tick_at(app, 2026, 11, 4, 9, 0)  # first seen
        await tick_at(app, 2026, 11, 4, 11, 0)
        assert not force_dhw_on(app)
        assert "manual force DHW held since 2026-11-04T09:00:00+01:00" in capsys.readouterr().out
        # The day's own window still runs and is released at its end.
        await tick_at(app, 2026, 11, 4, 14, 0)
        assert force_dhw_on(app)
        await tick_at(app, 2026, 11, 4, 16, 0)
        assert not force_dhw_on(app)

    @pytest.mark.asyncio
    async def test_manual_force_while_disabled_is_capped_too(self, app):
        app.ha.put(app.args["helpers"]["enable"], "off")
        app.ha.put(app.args["commands"]["force_dhw"], "on")
        await tick_at(app, 2026, 11, 4, 10, 0)
        assert force_dhw_on(app)
        await tick_at(app, 2026, 11, 4, 12, 0)
        assert not force_dhw_on(app)

    @pytest.mark.asyncio
    async def test_app_set_force_is_released_at_the_window_end_even_if_the_unit_keeps_it(self, app):
        set_prices(app, {14: 0.1, 15: 0.1})
        await tick_at(app, 2026, 11, 4, 0, 1)
        await tick_at(app, 2026, 11, 4, 14, 0)
        assert app.force_owned and force_dhw_on(app)
        real_call = app.call_service
        lost = {"count": 2}

        async def call_service(service, **data):
            if service == "switch/turn_off" and lost["count"]:
                lost["count"] -= 1  # the window-end turn-off and the same-tick retry are lost
                return None
            return await real_call(service, **data)

        app.call_service = call_service
        await tick_at(app, 2026, 11, 4, 16, 0)
        assert force_dhw_on(app)
        await tick_at(app, 2026, 11, 4, 16, 1)  # its own force: released at once, not held 2 h
        assert not force_dhw_on(app)

    @pytest.mark.asyncio
    async def test_plan_survives_a_home_assistant_restart(self, app):
        set_prices(app, {14: 0.1, 15: 0.1})
        await tick_at(app, 2026, 11, 4, 0, 1)
        await tick_at(app, 2026, 11, 4, 14, 0)
        assert force_dhw_on(app)
        stored = app.ha.states[app.args["helpers"]["dhw_plan"]]["state"]
        assert len(stored) <= 255 and '"forced":true' in stored
        del app.ha.states[app.args["status_entity"]]  # not restored by HA
        restarted = make_app(app.ha)
        restarted.clock[0] = local(2026, 11, 4, 14, 30)
        await restarted._load_status()
        await restarted._release_force_dhw_safely(restarted._now(), startup=True)
        assert force_dhw_on(restarted)
        await tick_at(restarted, 2026, 11, 4, 16, 0)
        assert not force_dhw_on(restarted)
        assert '"done":"2026-11-04"' in restarted.ha.states[app.args["helpers"]["dhw_plan"]]["state"]
        again = make_app(app.ha)
        again.clock[0] = local(2026, 11, 4, 18, 0)
        del app.ha.states[app.args["status_entity"]]
        await again._load_status()
        assert again.dhw_done == "2026-11-04"
        again.ha.services.clear()
        for hour in (18, 20, 21, 22):
            await tick_at(again, 2026, 11, 4, hour, 0)
        assert not again.ha.calls("switch/turn_on")  # no second window that day

    @pytest.mark.asyncio
    async def test_dry_run_dhw_never_touches_the_switch(self, app):
        app.args["actuate"]["dhw"] = False
        app.ha.put(app.args["commands"]["force_dhw"], "on")
        await tick_at(app, 2026, 11, 4, 10, 0)
        assert force_dhw_on(app)


class TestSeeding:
    """Review fix 3: no control on HA-minimum helpers."""

    def minimums(self, app):
        package = yaml.safe_load(HA_PACKAGE.read_text())
        for name, spec in package["input_number"].items():
            app.ha.put(f"input_number.{name}", str(float(spec["min"])))
        for name in package["input_datetime"]:
            app.ha.put(f"input_datetime.{name}", "00:00:00")
        app.ha.put(app.args["helpers"]["seeded"], "off")

    @pytest.mark.asyncio
    async def test_seeding_retried_each_tick_and_no_control_until_it_works(self, app):
        self.minimums(app)
        real_call = app.call_service
        failing = {"on": True}

        async def call_service(service, **data):
            if failing["on"] and service.startswith("input_"):
                raise RuntimeError("HA rejected the call")
            return await real_call(service, **data)

        app.call_service = call_service
        room(app, "bedroom", current=18.0)
        await tick_at(app, 2026, 11, 4, 1, 0)
        await tick_at(app, 2026, 11, 4, 1, 1)
        assert not app.ha.calls("climate/set_temperature")
        assert not app.ha.calls("set_state", "input_select.heishamon_heatmode")
        assert app.ha.states[app.args["status_entity"]]["state"] == "seeding"
        assert app.args["heartbeat_entity"] not in app.ha.states  # the watchdog takes over
        failing["on"] = False
        await tick_at(app, 2026, 11, 4, 1, 2)
        assert app.ha.states[app.args["helpers"]["seeded"]]["state"] == "on"
        assert app.ha.states[app.args["helpers"]["water_dry_run"]]["state"] == "on"
        assert setpoints(app)["hall_kitchen"] == 24.0 and setpoints(app)["bedroom"] == 21.5

    @pytest.mark.asyncio
    async def test_helpers_at_their_ha_minimum_fall_back_to_the_defaults(self, app):
        self.minimums(app)
        app.ha.put(app.args["helpers"]["seeded"], "on")  # seeded, yet the values were lost
        # Day minimum 18 and floor 16 are out of range (defaults 23.5 / 21); margin 0 and lead 0
        # are legitimate values, so the day setpoint comes at the window start itself.
        await tick_at(app, 2026, 11, 4, 1, 0)
        assert setpoints(app) == dict.fromkeys(app.args["rooms"], 21.0)
        await tick_at(app, 2026, 11, 4, 12, 0)
        assert setpoints(app) == {"hall_kitchen": 23.5, "bedroom": 21.0, "office_a": 21.0, "office_t": 21.0}

    @pytest.mark.parametrize("role", sorted(HELPER_DEFAULTS))
    def test_every_default_is_inside_its_sane_range_and_the_package_range(self, role):
        package = yaml.safe_load(HA_PACKAGE.read_text())
        if role in ("weekday_start", "weekend_start", "window_end"):
            return
        low, high = HELPER_RANGES[role]
        spec = package["input_number"][f"comfort_{role}"]
        assert low <= HELPER_DEFAULTS[role] <= high
        assert spec["min"] <= low and high <= spec["max"]


class TestBoundaryRetry:
    """Review fix 6: an unconfirmed boundary write is repeated."""

    @pytest.mark.asyncio
    async def test_lost_write_is_retried_until_confirmed(self, app):
        hall = app.args["rooms"]["hall_kitchen"]
        real_call = app.call_service
        lost = {"count": 2}

        async def call_service(service, **data):
            if service == "climate/set_temperature" and lost["count"]:
                lost["count"] -= 1
                app.ha.services.append((service, data.get("entity_id"), data))
                return None  # the cloud thermostat never applies it
            return await real_call(service, **data)

        app.call_service = call_service
        await tick_at(app, 2026, 11, 4, 0, 44)
        await tick_at(app, 2026, 11, 4, 0, 45)
        assert setpoints(app)["hall_kitchen"] == 21.5
        await tick_at(app, 2026, 11, 4, 0, 49)
        assert len(app.ha.calls("climate/set_temperature", hall)) == 1
        await tick_at(app, 2026, 11, 4, 0, 50)  # retry 1, lost again
        await tick_at(app, 2026, 11, 4, 0, 55)  # retry 2, applied
        assert setpoints(app)["hall_kitchen"] == 24.0
        await tick_at(app, 2026, 11, 4, 0, 56)
        assert app.boundary is None
        assert app.ha.states[app.args["status_entity"]]["attributes"]["phase_key"] == app.last_phase_key
        await tick_at(app, 2026, 11, 4, 1, 30)
        assert len(app.ha.calls("climate/set_temperature", hall)) == 3

    @pytest.mark.asyncio
    async def test_a_manual_change_ends_the_retry(self, app):
        app.ha.put(app.args["rooms"]["hall_kitchen"], "unavailable")
        await tick_at(app, 2026, 11, 4, 0, 45)
        app.ha.put(app.args["rooms"]["hall_kitchen"], "heat", temperature=22.0)
        await app._on_target_change("", "temperature", 21.5, 22.0, {"room": "hall_kitchen"})
        await tick_at(app, 2026, 11, 4, 0, 55)
        assert setpoints(app)["hall_kitchen"] == 22.0
        assert app.boundary is None

    @pytest.mark.asyncio
    async def test_away_cancels_a_pending_retry(self, app):
        app.ha.put(app.args["rooms"]["hall_kitchen"], "unavailable")
        await tick_at(app, 2026, 11, 4, 0, 45)
        app.ha.put(app.args["rooms"]["hall_kitchen"], "heat", temperature=21.5, current_temperature=21.6)
        app.ha.put(app.args["helpers"]["away"], "on")
        await app._on_away()
        for minute in (50, 55, 59):
            await tick_at(app, 2026, 11, 4, 0, minute)
        assert setpoints(app)["hall_kitchen"] == 21.5


class TestDhwPricesAndDst:
    """Review fix 9: today's prices only, and 23 h / 25 h days."""

    @pytest.mark.asyncio
    async def test_yesterdays_prices_are_not_used(self, app):
        set_prices(app, {3: 0.01, 4: 0.01}, day=date(2026, 11, 3))
        await tick_at(app, 2026, 11, 4, 0, 0)
        assert app.dhw_plan is None
        assert not any("no full PVPC" in m for m in app.ha.messages())  # not late yet
        set_prices(app, {20: 0.01, 21: 0.01}, day=date(2026, 11, 4))
        await tick_at(app, 2026, 11, 4, 0, 10)
        assert app.dhw_plan["start"] == local(2026, 11, 4, 20)

    def test_day_hour_slots(self):
        assert len(day_hour_slots(TZ, date(2026, 11, 4))) == 24
        spring = day_hour_slots(TZ, date(2026, 3, 29))
        assert len(spring) == 23 and "price_02h" not in [key for _, _, key in spring]
        fall = day_hour_slots(TZ, date(2026, 10, 25))
        assert len(fall) == 25
        assert [key for _, _, key in fall][2:4] == ["price_02h", "price_02h_d"]
        assert fall[3][0] - fall[2][0] == timedelta(hours=1)

    @pytest.mark.asyncio
    async def test_spring_forward_day_is_planned_with_23_prices(self, app):
        day = date(2026, 3, 29)
        attributes = {f"price_{hour:02d}h": 0.2 for hour in range(24) if hour != 2}
        attributes["price_15h"] = attributes["price_16h"] = 0.05
        app.ha.put(app.args["sensors"]["pvpc_price"], "0.2", **attributes)
        app.ha.states[app.args["sensors"]["pvpc_price"]].update(
            attributes=attributes, last_updated=TZ.localize(datetime(2026, 3, 29, 0, 0, 5)).isoformat()
        )
        await tick_at(app, 2026, 3, 29, 0, 1)
        assert app.dhw_plan["start"] == TZ.localize(datetime(2026, 3, 29, 15))
        assert app.dhw_plan["end"] - app.dhw_plan["start"] == timedelta(hours=2)
        assert app.dhw_plan["day"] == day

    @pytest.mark.asyncio
    async def test_fall_back_day_uses_the_repeated_hour_in_elapsed_time(self, app):
        attributes = {f"price_{hour:02d}h": 0.2 for hour in range(24)}
        attributes["price_02h"] = 0.2
        attributes["price_02h_d"] = 0.01  # 02:00 CET, the second 02:00
        attributes["price_03h"] = 0.01
        app.ha.states[app.args["sensors"]["pvpc_price"]].update(
            attributes=attributes, last_updated=TZ.localize(datetime(2026, 10, 25, 0, 0, 5)).isoformat()
        )
        app.ha.put(app.args["sensors"]["outdoor"], "13")  # mild: Sunday early start 09:00 - 215 min = 05:25
        await tick_at(app, 2026, 10, 25, 0, 1)
        start = app.dhw_plan["start"]
        assert start.utcoffset() == timedelta(hours=1) and start.hour == 2  # 02:00 CET
        assert app.dhw_plan["end"] - start == timedelta(hours=2)

class TestPhysicalAcknowledgements:
    @pytest.mark.asyncio
    async def test_unconfirmed_mode_cannot_keep_the_watchdog_heartbeat_healthy(self, app):
        feedback = app.args["sensors"]["operating_mode"]
        app.ha.put(feedback, "DHW")
        room(app, "bedroom", current=21.0)
        await tick_at(app, 2026, 11, 4, 0, 30)
        assert app.pending_mode["target"] == "Heat+DHW"
        assert not app.ha.calls("set_state", app.args["heartbeat_entity"])
        # The helper was updated optimistically, but actual feedback is still DHW.
        assert app.ha.states["input_select.heishamon_heatmode"]["state"] == "Heat+DHW"
        await tick_at(app, 2026, 11, 4, 0, 31)
        assert not app.ha.calls("automation/trigger", "automation.set_heatmode")
        await tick_at(app, 2026, 11, 4, 0, 32)
        assert app.ha.calls("automation/trigger", "automation.set_heatmode")
        assert not app.ha.calls("set_state", app.args["heartbeat_entity"])
        # The sensor finally confirms the command.
        app.ha.put(feedback, "Heat+DHW")
        await tick_at(app, 2026, 11, 4, 0, 33)
        assert app.pending_mode is None
        assert app.ha.calls("set_state", app.args["heartbeat_entity"])

    @pytest.mark.asyncio
    async def test_dhw_target_is_not_assumed_from_ha_helper_and_is_resent(self, app):
        app.clock[0] = local(2026, 11, 4, 1, 0)
        plan = {"day": WED, "start": local(2026, 11, 4, 1), "end": local(2026, 11, 4, 3),
                "forced": False}
        app.ha.put(app.args["sensors"]["tank_target"], "45")
        app.ha.put(app.args["commands"]["tank_target"], "49")  # already desired, physical target stale
        await app._start_dhw(plan)
        assert not plan.get("target_written") and not plan.get("forced")
        assert app.ha.calls("automation/trigger", "automation.set_tank_target_temperature")
        assert not app.ha.calls("switch/turn_on")
        app.clock[0] = local(2026, 11, 4, 1, 1)
        await app._start_dhw(plan)
        assert len(app.ha.calls("automation/trigger", "automation.set_tank_target_temperature")) == 1
        app.clock[0] = local(2026, 11, 4, 1, 2)
        await app._start_dhw(plan)
        assert len(app.ha.calls("automation/trigger", "automation.set_tank_target_temperature")) == 2
        # No water heating commanded until the unit reports the safe target.
        app.ha.put(app.args["sensors"]["tank_target"], "49")
        await app._start_dhw(plan)
        assert plan["target_written"] is True
        assert plan["force_requested"] is True
        assert plan.get("forced") is False

    @pytest.mark.asyncio
    async def test_force_dhw_waits_for_feedback_retries_and_restores_intent(self, app):
        app.clock[0] = local(2026, 11, 4, 1, 0)
        plan = {"day": WED, "start": local(2026, 11, 4, 1), "end": local(2026, 11, 4, 3),
                "forced": False}
        await app._start_dhw(plan)
        # FakeHA acknowledges the switch immediately, but the flag is an intent until the next tick.
        assert plan["force_requested"] and not plan["forced"]
        assert app._restore_dhw_plan(app._stored_dhw_plan() if app.dhw_plan else
            {**plan, "day": plan["day"].isoformat(),
             "start": plan["start"].isoformat(), "end": plan["end"].isoformat()})["force_requested"]
        await app._start_dhw(plan)
        assert plan["forced"] is True
        assert app.force_owned is True

