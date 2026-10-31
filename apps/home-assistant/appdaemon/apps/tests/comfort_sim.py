"""Closed-loop simulator for the heat-pump controllers (test harness, not deployed).

A deterministic discrete-event simulation on a one-minute plant step that drives a real AppDaemon
app (the comfort controller, or the archived legacy apps) through the AppDaemon API it uses:
mocked Home Assistant state with listeners, a fake clock, run_every / run_daily / run_at timers.

Plant: a two-state room/slab RC model per thermostat room, floor valves closed at each thermostat
setpoint, and an Aquarea-like unit (Heat+DHW / DHW operating mode, Heating_Mode direct or curve,
Z1 water request, a hot-water tank with its own reheat, force DHW, compressor start counter).
Home Assistant side: the heishamon command automations with their real guard templates, the
selector echoes, the watchdog automation with its real conditions, the PVPC sensor (hourly
update, DST keys), helpers with their HA ranges.

Everything is synthetic: weather and price profiles are generated, no real data is used.

Units: temperatures degC, capacities kWh/K, conductances kW/K, powers kW, time steps in hours.
"""

import asyncio
import functools
import heapq
import importlib.util
import math
import random
import sys
from collections import defaultdict, namedtuple
from dataclasses import dataclass, field
from datetime import date, datetime, time, timedelta, timezone, tzinfo
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

import pytz
import yaml

import comfort_control
from tests.ha_template import condition_true, render

TZ = pytz.timezone("Europe/Madrid")


class MemoTz(tzinfo):
    """pytz Europe/Madrid (the AppDaemon time zone type) with a memoised localize(), which the
    comfort controller calls a few dozen times per tick. Same results, a faster simulation."""

    def __init__(self, tz):
        self.tz = tz
        self.cache = {}
        self.zone = tz.zone

    def localize(self, naive, is_dst=False):
        key = (naive, is_dst)
        if key not in self.cache:
            self.cache[key] = self.tz.localize(naive, is_dst=is_dst)
        return self.cache[key]

    def fromutc(self, dt):
        return self.tz.fromutc(dt.replace(tzinfo=self.tz))

    def utcoffset(self, dt):
        return self.tz.localize(dt.replace(tzinfo=None)).utcoffset() if dt is not None else None

    def dst(self, dt):
        return self.tz.localize(dt.replace(tzinfo=None)).dst() if dt is not None else None

    def tzname(self, dt):
        return self.tz.zone


APP_TZ = MemoTz(TZ)
APPS_DIR = Path(__file__).resolve().parents[1]
APPDAEMON_DIR = APPS_DIR.parent
CONFIG_DIR = APPDAEMON_DIR.parent / "config" / "integrations"
LEGACY_DIR = APPDAEMON_DIR / "archive" / "legacy-price-control"

HEAT_MODE = "input_select.heishamon_heatmode"
Z1_HELPER = "input_number.heishamon_heatshift_z1"
TANK_HELPER = "input_number.heishamon_tank_temp"
FORCE_DHW = "switch.aquarea_force_dhw_mode"
SENSOR = {
    "outdoor": "sensor.aquarea_outside_ambient_temperature",
    "heating_mode": "sensor.aquarea_heating_mode",
    "z1_request": "sensor.aquarea_z1_heat_request_temperature",
    "tank_target": "sensor.aquarea_tank_set_temperature",
    "starts": "sensor.aquarea_compressor_start_stop_counter",
    "sterilization": "binary_sensor.aquarea_sterilization_state",
    "force_heater": "binary_sensor.aquarea_force_heater_state",
    "price": "sensor.esios_pvpc",
    "mode": "sensor.aquarea_mode",
}
LAST_CONNECTION = "sensor.last_appdaemon_connection"
ROOMS = ("hall_kitchen", "bedroom", "office_a", "office_t")
Record = namedtuple(
    "Record",
    "at mode space dhw compressor water target outdoor rooms setpoints tank tank_target force elec starts heating_mode",
)
MINUTE = timedelta(minutes=1)
DT = 1 / 60  # plant step, hours


def local(*args):
    return TZ.localize(datetime(*args))


# --- Plant -----------------------------------------------------------------------------------


@dataclass
class Room:
    """Two-state RC: slab (floor screed, heated by the water) and room (air and furniture).

    C_s dTs/dt = Kw (Tw - Ts) [valve open, space heating] - Ksr (Ts - Tr)
    C_r dTr/dt = Ksr (Ts - Tr) - Kro (Tr - To) + q_int
    Time constant of the whole room (C_r + C_s) / Kro = 150 h (measured slab time constant).
    """

    name: str
    c_room: float
    c_slab: float
    k_slab_room: float
    k_out: float
    k_water: float
    q_internal: float
    tr: float = 22.0
    ts: float = 22.5
    valve: bool = False


def default_rooms():
    # Calibrated (see test_plant_matches_the_measured_rates) to the B0 rates: night cooling
    # 0.04-0.12 K/h, warming 0.08-0.19 K/h at 26 degC water, 0.5-0.75 h lag after the start.
    return [
        Room("hall_kitchen", 1.5, 4.5, 1.2, 0.04, 0.55, 0.12),
        Room("bedroom", 0.75, 2.25, 0.6, 0.02, 0.22, 0.04),
        Room("office_a", 0.6, 1.8, 0.48, 0.016, 0.18, 0.10),  # servers: extra internal heat
        Room("office_t", 0.6, 1.8, 0.48, 0.016, 0.18, 0.03),
    ]


UNIT_CURVE = (-5.0, 35.0, 15.0, 26.0)  # the unit's own Z1 curve (used in Heating_Mode 0 only)
TANK_HEAT_RATE = 18.0  # K/h with the compressor on the tank
TANK_LOSS = 0.25  # K/h standby
TANK_REHEAT_DELTA = -4.0  # DHW_Heat_Delta: the unit reheats below target + delta
HP_MAX_HEAT = 7.0  # kW thermal to the floor
WATER_TAU = 0.4  # h, supply temperature approach to the target after a start
VALVE_HYSTERESIS = 0.2  # K: a valve opens below setpoint - this and closes at the setpoint
REPORT_EVERY = 10  # minutes between thermostat temperature reports


def cop_space(water, outdoor):
    return max(1.8, min(6.0, 5.2 - 0.09 * (water - 25) + 0.07 * outdoor))


def cop_dhw(outdoor):
    return max(1.5, min(4.0, 2.2 + 0.06 * outdoor))


# --- Profiles (synthetic) ---------------------------------------------------------------------


def weather_profile(kind, start_day, days, seed=1):
    """Hourly outdoor temperature (UTC hour start -> degC), deterministic."""
    rng = random.Random(seed)
    offsets = [rng.uniform(-1.5, 1.5) for _ in range(days + 3)]
    base = {"cold": 0.0, "typical": 7.0, "mild": 14.0, "front": 8.0, "dst": 9.0}[kind]
    amplitude = {"cold": 3.0, "typical": 4.0, "mild": 4.0, "front": 4.0, "dst": 4.0}[kind]
    values = {}
    t = local(start_day.year, start_day.month, start_day.day).astimezone(timezone.utc) - timedelta(days=1)
    end = t + timedelta(days=days + 3)
    while t < end:
        lt = t.astimezone(TZ)
        index = (lt.date() - start_day).days + 1
        mean = base + offsets[max(0, min(index, len(offsets) - 1))]
        if kind == "front":
            # A cold front on day 5: from 8 down to about 1 degC over 12 h, back up to 12 from day 9.
            hours = (t - local(start_day.year, start_day.month, start_day.day).astimezone(timezone.utc)).total_seconds() / 3600
            if 96 <= hours < 108:
                mean = 8 - 7 * (hours - 96) / 12
            elif 108 <= hours < 192:
                mean = 1.0
            elif 192 <= hours < 216:
                mean = 1 + 11 * (hours - 192) / 24
            elif hours >= 216:
                mean = 12.0
        diurnal = amplitude * math.sin(2 * math.pi * (lt.hour + lt.minute / 60 - 9) / 24)
        values[t] = mean + diurnal + rng.uniform(-0.3, 0.3)
        t += timedelta(hours=1)
    return values


def price_profile(kind, day, seed=1):
    """Synthetic PVPC prices (EUR/kWh) for each local hour of `day` (0-23)."""
    rng = random.Random(seed * 1000 + day.toordinal())
    weekend = day.weekday() >= 5
    prices = {}
    for hour in range(24):
        if kind == "flat":
            price = 0.14 + rng.uniform(-0.01, 0.01)
        elif weekend:
            price = 0.09 if hour < 8 else 0.11
            if kind == "weekend_negative" and 11 <= hour < 16:
                price = -0.01 if 12 <= hour < 15 else 0.002
            price += rng.uniform(-0.005, 0.005)
        else:  # weekday double peak (2.0TD): valle 0-8, peaks 10-14 and 18-22
            if hour < 8:
                price = 0.08
            elif 10 <= hour < 14 or 18 <= hour < 22:
                price = 0.24
            else:
                price = 0.15
            if hour in (3, 4):
                price -= 0.01
            price += rng.uniform(-0.01, 0.01)
        prices[hour] = round(price, 5)
    return prices


# --- Home Assistant stand-in -------------------------------------------------------------------


class SimHA:
    """Entity store with listeners, the heishamon automations and the watchdog."""

    def __init__(self, sim):
        self.sim = sim
        self.states = {}
        self.listeners = []  # dicts: host, callback, entity, attribute, new, old, kwargs
        self.queue = []
        self.history = defaultdict(list)  # entity -> [(time, state)]
        self.services = []  # (time, caller, service, entity, data)
        self.mqtt = []  # (time, topic, payload)
        self.notifications = []  # (time, caller, message)
        self.mode_changes = []  # (time, old, new, source)
        self.up = True
        self.started_at = sim.now
        self.ranges = {}
        for package in ("heishamon.yaml", "appdaemon_comfort.yaml"):
            data = yaml.safe_load((CONFIG_DIR / package).read_text())
            for name, spec in (data.get("input_number") or {}).items():
                self.ranges[f"input_number.{name}"] = (spec["min"], spec["max"])
        legacy = yaml.safe_load((LEGACY_DIR / "ha-packages" / "appdaemon_climate.yaml").read_text())
        for name, spec in legacy["input_number"].items():
            self.ranges[f"input_number.{name}"] = (spec["min"], spec["max"])
        heishamon = {a["alias"]: a for a in yaml.safe_load((CONFIG_DIR / "heishamon.yaml").read_text())["automation"]}
        self.z1_guard = heishamon["Set Z1 Heat Temperature"]["condition"][0]["value_template"]
        self.tank_guard = heishamon["Set tank target temperature"]["condition"][0]["value_template"]
        self.watchdog = None
        self.watchdog_last = None
        self.automations_on = False  # off while the simulator sets up the initial state

    # --- state ---

    def set(self, entity, state=None, attributes=None, replace=False, source="ha", fire=True):
        now = self.sim.now
        old = self.states.get(entity)
        old_attributes = dict(old["attributes"]) if old else {}
        new_attributes = dict(attributes or {}) if replace else {**old_attributes, **(attributes or {})}
        new_state = old["state"] if (state is None and old) else state
        changed_state = old is None or old["state"] != new_state
        item = {
            "state": new_state,
            "attributes": new_attributes,
            "last_changed": now if changed_state or old is None else old["last_changed"],
            "last_updated": now,
        }
        self.states[entity] = item
        if changed_state:
            self.history[entity].append((now, new_state))
        if not fire or (not changed_state and old_attributes == new_attributes):
            return
        for listener in self.listeners:
            if listener["entity"] != entity:
                continue
            attribute = listener["attribute"]
            if attribute is None:
                if not changed_state:
                    continue
                before, after = (old["state"] if old else None), new_state
            else:
                before, after = old_attributes.get(attribute), new_attributes.get(attribute)
                if before == after:
                    continue
            if listener["new"] is not None and after != listener["new"]:
                continue
            if listener["old"] is not None and before != listener["old"]:
                continue
            self.queue.append((listener, entity, attribute, before, after))
        self._automations(entity, old, item, source)

    def remove(self, entity):
        self.states.pop(entity, None)

    def get(self, entity, attribute=None):
        item = self.states.get(entity)
        if item is None:
            return None
        if attribute in (None, "state"):
            return item["state"]
        if attribute == "all":
            return {
                "entity_id": entity,
                "state": item["state"],
                "attributes": dict(item["attributes"]),
                "last_changed": item["last_changed"].isoformat(),
                "last_updated": item["last_updated"].isoformat(),
            }
        return item["attributes"].get(attribute)

    # --- services ---

    async def call_service(self, caller, service, **data):
        entity = data.get("entity_id")
        self.services.append((self.sim.now, caller, service, entity, data))
        if not self.up:
            raise ConnectionError("Home Assistant is down")
        if service == "climate/set_temperature":
            self.sim.thermostat_write(entity, data["temperature"])
        elif service == "input_number/set_value":
            low, high = self.ranges.get(entity, (-math.inf, math.inf))
            value = float(data["value"])
            if not low <= value <= high:
                raise ValueError(f"{entity}: {value} outside {low}-{high}")
            self.set(entity, str(value))
        elif service == "input_text/set_value":
            if len(data["value"]) > 255:
                raise ValueError(f"{entity}: longer than 255")
            self.set(entity, data["value"])
        elif service == "input_datetime/set_datetime":
            self.set(entity, data["time"])
        elif service in ("input_boolean/turn_on", "input_boolean/turn_off"):
            self.set(entity, "on" if service.endswith("on") else "off")
        elif service in ("switch/turn_on", "switch/turn_off"):
            if entity == FORCE_DHW:
                payload = "1" if service.endswith("on") else "0"
                self.mqtt.append((self.sim.now, "SetForceDHW", payload))
                self.sim.unit.force_dhw = payload == "1"
                self.set(entity, "on" if payload == "1" else "off")
            else:
                raise ValueError(f"unexpected switch {entity}")
        elif service == "input_select/select_option":
            self.set(entity, data["option"], source=caller)
        elif service in ("notify/send_message", "persistent_notification/create"):
            self.notifications.append((self.sim.now, caller, data.get("message", "")))
        else:
            raise ValueError(f"unexpected service {service}")

    # --- automations (heishamon.yaml) ---

    def _automations(self, entity, old, item, source):
        if not self.automations_on:
            return
        unit = self.sim.unit
        if entity == HEAT_MODE:
            before = old["state"] if old else None
            if before != item["state"]:
                self.mode_changes.append((self.sim.now, before, item["state"], source))
            # "Set heatmode": the selector state is published as SetOperationMode.
            if item["state"] in ("Heat+DHW", "DHW", "Heat", "Cool+DHW"):
                self.mqtt.append((self.sim.now, "SetOperationMode", item["state"]))
                unit.mode = item["state"]
        elif entity == Z1_HELPER and (old is None or old["state"] != item["state"]):
            trigger = SimpleNamespace(to_state=SimpleNamespace(state=item["state"]))
            store = self.template_store()
            if condition_true(self.z1_guard, store, self.sim.now, variables={"trigger": trigger}):
                payload = render('{{ "%.0f" % (states("' + Z1_HELPER + '") | int) }}', store, self.sim.now)
                self.mqtt.append((self.sim.now, "SetZ1HeatRequestTemperature", payload))
                unit.command_z1(payload)
            else:
                self.mqtt.append((self.sim.now, "blocked:SetZ1HeatRequestTemperature", item["state"]))
        elif entity == TANK_HELPER and (old is None or old["state"] != item["state"]):
            trigger = SimpleNamespace(to_state=SimpleNamespace(state=item["state"]))
            store = self.template_store()
            if condition_true(self.tank_guard, store, self.sim.now, variables={"trigger": trigger}):
                payload = "%.0f" % int(float(item["state"]))
                self.mqtt.append((self.sim.now, "SetDHWTemp", payload))
                unit.tank_target = float(payload)
            else:
                self.mqtt.append((self.sim.now, "blocked:SetDHWTemp", item["state"]))

    def template_store(self):
        return self.states

    def run_watchdog(self):
        """The watchdog automation, with the real conditions of its package."""
        if not self.up or self.watchdog is None:
            return
        automation = self.watchdog
        now = self.sim.now
        this = {
            "last_changed": self.started_at,
            "attributes": SimpleNamespace(last_triggered=self.watchdog_last),
        }
        for condition in automation["condition"]:
            if condition["condition"] == "state":
                if self.get(condition["entity_id"]) != condition["state"]:
                    return
            elif not condition_true(condition["value_template"], self.states, now, this=this):
                return
        self.watchdog_last = now
        action = automation["action"]
        variables = {}
        for step in action:
            if "variables" in step:
                for name, template in step["variables"].items():
                    variables[name] = render(template, self.states, now, this=this)
            elif step.get("action") == "input_select.select_option":
                option = render(str(step["data"]["option"]), self.states, now, this=this, variables=variables)
                self.set(step["target"]["entity_id"], option, source="watchdog")
            elif step.get("action") == "notify.send_message":
                self.notifications.append((now, "watchdog", "watchdog restored heating"))


# --- The unit ------------------------------------------------------------------------------------


class Unit:
    def __init__(self, sim, heating_mode=1, z1=26, tank_target=49.0, force_auto_clear=True):
        self.sim = sim
        self.mode = "DHW"
        self.heating_mode = heating_mode
        self.z1 = z1  # direct: absolute degC; curve: shift K
        self.tank_target = tank_target
        self.tank = 47.0
        self.force_dhw = False
        self.force_auto_clear = force_auto_clear
        self.dhw_active = False
        self.compressor = False
        self.starts = 5000
        self.water = 24.0  # supply temperature, degC
        self.z1_reports = []

    def command_z1(self, payload):
        value = int(payload)
        if self.heating_mode == 1:
            self.z1 = max(20, min(55, value))
        else:
            self.z1 = max(-5, min(5, value))

    def water_target(self, outdoor):
        if self.heating_mode == 1:
            return float(self.z1)
        return comfort_control.curve_water(outdoor, *UNIT_CURVE) + self.z1


# --- AppDaemon stand-in -------------------------------------------------------------------------


class SimDefer(BaseException):
    """Raised by the legacy apps' asyncio.sleep(600): the host re-runs the callback later."""


class FakeDatetime(datetime):
    clock = None

    @classmethod
    def now(cls, tz=None):
        now = cls.clock()
        return now.astimezone(tz) if tz else now.replace(tzinfo=None)


def load_legacy(name):
    """Import an archived legacy app (tests only)."""
    module_name = f"legacy_{name}"
    if module_name in sys.modules:
        return sys.modules[module_name]
    spec = importlib.util.spec_from_file_location(module_name, LEGACY_DIR / f"{name}.py")
    module = importlib.util.module_from_spec(spec)
    sys.modules[module_name] = module
    spec.loader.exec_module(module)
    return module


class AppHost:
    """One app instance with the AppDaemon API bound to the simulator."""

    def __init__(self, sim, cls, args, name):
        self.sim = sim
        self.name = name
        self.timers = {}
        self.alive = True
        with patch.object(cls, "__init__", lambda self: None):
            app = cls()
        app.args = args
        app._config_model = SimpleNamespace(name=name)  # ADAPI.name
        app.log = self.log
        app.get_timezone = lambda: APP_TZ
        app._now = lambda: sim.now  # the comfort controller's clock seam
        app.AD = SimpleNamespace(sched=SimpleNamespace(schedule=defaultdict(dict)), logging=None)
        self.schedule = app.AD.sched.schedule
        for method in (
            "get_state",
            "set_state",
            "call_service",
            "get_history",
            "sleep",
            "listen_state",
            "run_every",
            "run_daily",
            "run_at",
            "cancel_timer",
            "create_task",
            "get_entity",
        ):
            setattr(app, method, getattr(self, method))
        self.app = app

    def log(self, msg, level="INFO", **kwargs):
        self.sim.logs.append((self.sim.now, self.name, level or "INFO", msg))

    async def get_state(self, entity, attribute=None, **kwargs):
        return self.sim.ha.get(entity, attribute)

    async def set_state(self, entity, state=None, attributes=None, **kwargs):
        self.sim.ha.services.append((self.sim.now, self.name, "set_state", entity, {"state": state}))
        if not self.sim.ha.up:
            raise ConnectionError("Home Assistant is down")
        self.sim.ha.set(entity, state, attributes, replace=attributes is not None, source=self.name)

    async def call_service(self, service, **data):
        return await self.sim.ha.call_service(self.name, service, **data)

    async def get_history(self, entity_id, days=1, **kwargs):
        since = self.sim.now - timedelta(days=days)
        items = [
            {"state": state, "last_changed": at.isoformat()}
            for at, state in self.sim.ha.history.get(entity_id, [])
            if at >= since
        ]
        return [items]

    async def sleep(self, seconds):
        return None

    def get_entity(self, entity):
        host = self

        class Entity:
            async def turn_on(self):
                await host.call_service("switch/turn_on", entity_id=entity)

        return Entity()

    async def listen_state(self, callback, entity, attribute=None, new=None, old=None, **kwargs):
        listener = {
            "host": self,
            "callback": callback,
            "entity": entity,
            "attribute": attribute,
            "new": new,
            "old": old,
            "kwargs": kwargs,
        }
        self.sim.ha.listeners.append(listener)
        return listener

    def _add_timer(self, callback, when, interval=None, daily=None):
        handle = self.sim.add_timer(self, callback, when, interval=interval, daily=daily)
        self.schedule[self.name][handle] = {"callback": callback}
        return handle

    async def run_every(self, callback, start, interval, **kwargs):
        when = self.sim.now if start == "now" else start
        return self._add_timer(callback, when, interval=timedelta(seconds=interval))

    def _next_local(self, clock):
        hh, mm, ss = (int(x) for x in clock.split(":"))
        today = self.sim.now.astimezone(TZ).date()
        for offset in (0, 1, 2):
            when = TZ.localize(datetime.combine(today + timedelta(days=offset), time(hh, mm, ss)))
            if when > self.sim.now:
                return when
        raise AssertionError("unreachable")

    async def run_daily(self, callback, start, **kwargs):
        return self._add_timer(callback, self._next_local(start), daily=start)

    async def run_at(self, callback, start, **kwargs):
        return self._add_timer(callback, self._next_local(start))

    async def cancel_timer(self, handle):
        self.sim.cancelled.add(handle)
        self.schedule[self.name].pop(handle, None)

    async def create_task(self, coroutine):
        await self.sim.guard(self, coroutine)

    def shutdown(self):
        self.alive = False
        self.sim.ha.listeners = [item for item in self.sim.ha.listeners if item["host"] is not self]
        for handle in list(self.schedule[self.name]):
            self.sim.cancelled.add(handle)
        self.schedule[self.name].clear()


# --- Simulation ----------------------------------------------------------------------------------


@dataclass
class Scenario:
    name: str
    start: date
    days: int
    weather: str = "typical"
    prices: str = "double_peak"
    seed: int = 1
    controller: str = "comfort"  # or "legacy"
    events: list = field(default_factory=list)  # (local datetime tuple, action name, kwargs)
    rooms: list = None
    force_auto_clear: bool = True
    report_every: int = REPORT_EVERY
    legacy_min_hours: int = 12
    warmup_days: int = 2
    exclude: list = field(default_factory=list)  # (start tuple, end tuple): not measured (away)


class Simulation:
    def __init__(self, scenario):
        self.scenario = scenario
        start = local(scenario.start.year, scenario.start.month, scenario.start.day) - timedelta(days=1)
        self.now = start
        self.end = start + timedelta(days=scenario.days + 1)
        self.measure_from = start + timedelta(days=1)
        self.logs = []
        self.cancelled = set()
        self.heap = []
        self.seq = 0
        self.unit = Unit(self, force_auto_clear=scenario.force_auto_clear)
        self.ha = SimHA(self)
        self.rooms = scenario.rooms or default_rooms()
        self.weather = weather_profile(scenario.weather, scenario.start, scenario.days + 1, scenario.seed)
        self.hosts = []
        self.records = []
        self.reported = {}
        self.faults = defaultdict(lambda: None)
        self.ad_up = True
        self.hung = False
        self.drop_writes = defaultdict(int)  # thermostat entity -> writes to drop
        self.thermostat_entities = {}
        self.device_setpoint = {}
        self.excluded = [(local(*a), local(*b)) for a, b in scenario.exclude]
        self._setup()

    # --- clock and events ---

    def add_timer(self, host, callback, when, interval=None, daily=None):
        self.seq += 1
        handle = f"t{self.seq}"
        heapq.heappush(self.heap, (when, self.seq, ("timer", handle, host, callback, interval, daily)))
        return handle

    def at(self, when, action, priority=0):
        self.seq += 1
        heapq.heappush(self.heap, (when, self.seq, ("call", action)))

    async def guard(self, host, coroutine):
        if not host.alive:
            coroutine.close()
            return
        try:
            await coroutine
        except SimDefer:
            raise
        except Exception as error:  # AppDaemon logs callback exceptions and carries on
            self.logs.append((self.now, host.name, "CALLBACK_ERROR", repr(error)))
        await self.drain()

    async def drain(self):
        while self.ha.queue:
            listener, entity, attribute, old, new = self.ha.queue.pop(0)
            host = listener["host"]
            if not host.alive or not self.ad_up:
                continue
            try:
                await listener["callback"](entity, attribute, old, new, dict(listener["kwargs"]))
            except Exception as error:
                self.logs.append((self.now, host.name, "CALLBACK_ERROR", repr(error)))

    def outdoor(self):
        hour = self.now.astimezone(timezone.utc).replace(minute=0, second=0, microsecond=0)
        a = self.weather[hour]
        b = self.weather.get(hour + timedelta(hours=1), a)
        fraction = self.now.minute / 60
        return a + (b - a) * fraction

    # --- setup ---

    def _setup(self):
        ha, scenario = self.ha, self.scenario
        args = yaml.safe_load((APPS_DIR / "apps.yaml").read_text())["comfort_control"]
        self.comfort_args = args
        for room, entity in args["rooms"].items():
            self.thermostat_entities[entity] = room
        for model in self.rooms:
            model.tr, model.ts = 22.5, 23.2
        legacy_setpoints = {"hall_kitchen": 24.0, "bedroom": 21.5, "office_a": 21.5, "office_t": 21.5}
        for entity, room in self.thermostat_entities.items():
            target = legacy_setpoints[room] if scenario.controller == "legacy" else 21.5
            ha.set(entity, "heat", {"current_temperature": 22.5, "temperature": target})
            self.device_setpoint[entity] = target
        ha.set(HEAT_MODE, "Heat+DHW", {"options": ["Heat", "Cool", "Auto", "DHW", "Heat+DHW", "Cool+DHW"]})
        ha.set(Z1_HELPER, "26.0")
        ha.set(TANK_HELPER, "49.0")
        ha.set(FORCE_DHW, "off")
        ha.set(SENSOR["sterilization"], "off")
        ha.set(SENSOR["force_heater"], "off")
        if scenario.controller == "comfort":
            helpers = args["helpers"]
            for role, value in comfort_control.HELPER_DEFAULTS.items():
                ha.set(helpers[role], str(float(value)) if not isinstance(value, str) else value)
            ha.set(helpers["enable"], "on")
            ha.set(helpers["away"], "off")
            ha.set(helpers["seeded"], "on")
            ha.set(helpers["water_dry_run"], "off")  # the owner did the first-write test
            ha.watchdog = yaml.safe_load((CONFIG_DIR / "appdaemon_comfort.yaml").read_text())["automation"][0]
        else:
            ha.set("input_boolean.appdaemon_climate_enable", "on")
            ha.set("input_boolean.appdaemon_dhw_enable", "on")
            ha.set("input_number.appdaemon_climate_min_hours_per_day", str(float(scenario.legacy_min_hours)))
            ha.set("input_select.appdaemon_climate_heat_mode", "Heat+DHW")
            package = yaml.safe_load((LEGACY_DIR / "ha-packages" / "appdaemon_climate.yaml").read_text())
            ha.watchdog = package["automation"][0]
        self.unit.mode = "Heat+DHW"
        self._report_unit()
        ha.automations_on = True
        self._update_price(force=True)
        # Seven days of price history for the legacy 7-day mean.
        self._price_history()
        self.at(self.now + timedelta(seconds=10), self.start_appdaemon)
        t = self.now
        while t < self.end:
            self.at(t, self.plant_step)
            if t.minute == 0:
                self.at(t + timedelta(seconds=20), self.update_price)
            t += MINUTE
        for when, action, kwargs in scenario.events:
            self.at(local(*when), lambda action=action, kwargs=kwargs: getattr(self, f"ev_{action}")(**kwargs))

    def _price_history(self):
        day = self.now.date() - timedelta(days=7)
        series = []
        while day < self.now.date():
            for start, hour, key in comfort_control.day_hour_slots(TZ, day):
                series.append((start, str(price_profile(self.scenario.prices, day, self.scenario.seed)[hour])))
            day += timedelta(days=1)
        self.ha.history[SENSOR["price"]] = series

    def prices_for(self, day):
        return price_profile(self.scenario.prices, day, self.scenario.seed)

    async def update_price(self):
        self._update_price()

    def _update_price(self, force=False):
        if self.faults["price"] == "unavailable":
            self.ha.set(SENSOR["price"], "unavailable", {}, replace=True)
            return
        today = self.now.astimezone(TZ).date()
        prices = self.prices_for(today)
        attributes = {}
        for _, hour, key in comfort_control.day_hour_slots(TZ, today):
            attributes[key] = prices[hour]
        current = prices[self.now.astimezone(TZ).hour]
        self.ha.set(SENSOR["price"], str(current), attributes, replace=True)

    # --- AppDaemon ---

    async def start_appdaemon(self):
        self.ad_up = True
        scenario = self.scenario
        if scenario.controller == "comfort":
            host = AppHost(self, comfort_control.ComfortControl, dict(self.comfort_args), "comfort_control")
            self.hosts = [host]
        else:
            legacy_apps = yaml.safe_load((LEGACY_DIR / "apps.yaml").read_text())
            self.hosts = []
            for name, cls_name in (("climate_control", "ClimateControl"), ("dhw_control", "DHWControl")):
                module = load_legacy(name)
                FakeDatetime.clock = lambda: self.now
                module.datetime = FakeDatetime
                module.asyncio = SimpleNamespace(sleep=self._legacy_sleep)
                host = AppHost(self, getattr(module, cls_name), dict(legacy_apps[name]), name)
                self.hosts.append(host)
        import utils

        utils.asyncio = SimpleNamespace(sleep=self._no_sleep)
        for host in self.hosts:
            await self.run_callback(host, host.app.initialize)

    async def _legacy_sleep(self, seconds):
        raise SimDefer(seconds)

    async def _no_sleep(self, seconds):
        return None

    async def stop_appdaemon(self):
        for host in self.hosts:
            if host.alive:
                try:
                    await host.app.terminate()
                except Exception as error:
                    self.logs.append((self.now, host.name, "CALLBACK_ERROR", repr(error)))
                host.shutdown()
        self.ad_up = False

    async def run_callback(self, host, callback, *args):
        try:
            await self.guard(host, callback(*args))
        except SimDefer as defer:
            # asyncio.sleep(600) inside a legacy callback: the retry runs after the delay.
            root = getattr(host.app, "_daily_register_schedulers", None)
            if root is not None:
                self.add_timer(host, root, self.now + timedelta(seconds=defer.args[0]))

    # --- the plant ---

    def thermostat_write(self, entity, value):
        if self.faults[("thermostat", entity)] == "unavailable" or self.ha.get(entity) == "unavailable":
            return  # a cloud thermostat that is offline drops the write
        if self.drop_writes[entity] > 0:
            self.drop_writes[entity] -= 1
            return
        self.device_setpoint[entity] = float(value)
        self.ha.set(entity, None, {"temperature": value})

    def _report_unit(self):
        ha, unit = self.ha, self.unit
        if not ha.up:
            return
        outdoor = self.outdoor()
        if self.faults["outdoor"] == "unavailable":
            ha.set(SENSOR["outdoor"], "unavailable")
        else:
            bias = -2.0 if unit.compressor else 0.0  # the unit sensor reads low while running
            ha.set(SENSOR["outdoor"], str(int(round(outdoor + bias))))
        heating_mode = self.faults["heating_mode"]
        if heating_mode == "unavailable":
            ha.set(SENSOR["heating_mode"], "unavailable")
        else:
            ha.set(SENSOR["heating_mode"], str(unit.heating_mode))
        ha.set(SENSOR["z1_request"], str(unit.z1))
        # "Set Z1 heat selector": the reported request copied into the helper as "%.1f".
        ha.set(Z1_HELPER, "%.1f" % unit.z1)
        ha.set(SENSOR["tank_target"], str(int(unit.tank_target)))
        ha.set(TANK_HELPER, "%.1f" % unit.tank_target)
        ha.set(SENSOR["starts"], str(unit.starts))
        ha.set(SENSOR["mode"], unit.mode)
        ha.set(FORCE_DHW, "on" if unit.force_dhw else "off")
        ha.set(HEAT_MODE, unit.mode, source="unit")  # "Set heatmode selector"
        ha.set(SENSOR["force_heater"], "on" if self.faults["force_heater"] else "off")

    async def plant_step(self):
        unit, rooms = self.unit, self.rooms
        outdoor = self.outdoor()
        # Hot water: forced or below target + delta -> the tank has priority.
        if unit.force_dhw or unit.tank < unit.tank_target + TANK_REHEAT_DELTA:
            unit.dhw_active = True
        if unit.dhw_active and unit.tank >= unit.tank_target:
            unit.dhw_active = False
            if unit.force_dhw and unit.force_auto_clear:
                unit.force_dhw = False
        space = unit.mode == "Heat+DHW" and not unit.dhw_active and any(room.valve for room in rooms)
        compressor = unit.dhw_active or space
        if compressor and not unit.compressor:
            unit.starts += 1
        if self.faults["short_cycle"]:
            if self.now.minute % 30 == 0:
                unit.starts += 1
        unit.compressor = compressor
        target = unit.water_target(outdoor)
        slab_mean = sum(room.ts for room in rooms) / len(rooms)
        if space:
            unit.water += (target - unit.water) * min(1.0, DT / WATER_TAU)
        else:
            unit.water += (slab_mean - unit.water) * min(1.0, DT / 0.5)
        heats = [room.k_water * max(0.0, unit.water - room.ts) if (space and room.valve) else 0.0 for room in rooms]
        total = sum(heats)
        if total > HP_MAX_HEAT:
            heats = [h * HP_MAX_HEAT / total for h in heats]
            total = HP_MAX_HEAT
        elec = total / cop_space(unit.water, outdoor) if space else 0.0
        for room, heat in zip(rooms, heats):
            flow = room.k_slab_room * (room.ts - room.tr)
            room.ts += DT * (heat - flow) / room.c_slab
            room.tr += DT * (flow - room.k_out * (room.tr - outdoor) + room.q_internal) / room.c_room
        if unit.dhw_active:
            unit.tank += TANK_HEAT_RATE * DT
            elec += 6.0 / cop_dhw(outdoor)
        unit.tank -= TANK_LOSS * DT
        lt = self.now.astimezone(TZ)
        if (lt.hour, lt.minute) in ((7, 30), (21, 30)):
            unit.tank -= 8.0  # showers
        # Thermostats: the device closes its valve at its setpoint (also while offline from the
        # cloud) and reports the room temperature every few minutes.
        for model in rooms:
            entity = self.comfort_args["rooms"][model.name]
            setpoint = self.device_setpoint[entity]
            if model.tr < setpoint - VALVE_HYSTERESIS:
                model.valve = True
            elif model.tr >= setpoint:
                model.valve = False
            if not self.ha.up:
                continue
            item = self.ha.states.get(entity)
            if self.faults[("thermostat", entity)] == "unavailable":
                if item["state"] != "unavailable":
                    self.ha.set(entity, "unavailable", {}, replace=True)  # HA drops the attributes
                continue
            if item["state"] == "unavailable":
                self.ha.set(entity, "heat", {"current_temperature": round(model.tr, 1), "temperature": setpoint})
            elif lt.minute % self.scenario.report_every == 0:
                self.ha.set(entity, None, {"current_temperature": round(model.tr, 1)})
        self._report_unit()
        if self.ad_up and self.ha.up:
            self.ha.set(LAST_CONNECTION, self.now.isoformat())
        self.ha.run_watchdog()
        self.records.append(
            Record(
                self.now,
                unit.mode,
                space,
                unit.dhw_active,
                compressor,
                unit.water,
                target,
                outdoor,
                tuple(room.tr for room in rooms),
                tuple(
                    comfort_control.parse_number(self.ha.states.get(e, {}).get("attributes", {}).get("temperature"))
                    for e in self.comfort_args["rooms"].values()
                ),
                unit.tank,
                unit.tank_target,
                unit.force_dhw,
                elec,
                unit.starts,
                unit.heating_mode,
            )
        )
        await self.drain()

    # --- scenario events ---

    async def ev_manual_setpoint(self, room, value):
        entity = self.comfort_args["rooms"][room]
        self.device_setpoint[entity] = float(value)
        self.ha.set(entity, None, {"temperature": value}, source="owner")

    async def ev_helper(self, entity, value):
        self.ha.set(entity, value, source="owner")

    async def ev_fault(self, key, value):
        if isinstance(key, list):
            key = tuple(key)
        self.faults[key] = value

    async def ev_heating_mode(self, value):
        """The owner switches the unit between curve (0) and direct (1) mode: the Z1 request is
        then a shift (0 K) or the last absolute request."""
        unit = self.unit
        if value == 0 and unit.heating_mode == 1:
            unit.direct_z1, unit.z1 = unit.z1, 0
        elif value == 1 and unit.heating_mode == 0:
            unit.z1 = getattr(unit, "direct_z1", 26)
        unit.heating_mode = value

    async def ev_appdaemon_restart(self, down_minutes=0):
        await self.stop_appdaemon()
        self.at(self.now + timedelta(minutes=down_minutes, seconds=7), self.start_appdaemon)

    async def ev_appdaemon_down(self):
        await self.stop_appdaemon()

    async def ev_ha_restart(self, down_minutes=3, reset_helpers=False):
        """HA restart: AppDaemon stops its apps (plugin failed), HA restores the helpers (or, with
        reset_helpers, re-creates them at their minimum with seeded off), entities that are not
        restored disappear, and AppDaemon re-initialises the apps when HA is back."""
        await self.stop_appdaemon()
        self.ha.up = False

        async def back():
            ha = self.ha
            ha.up = True
            ha.started_at = self.now
            for entity in (self.comfort_args["status_entity"], self.comfort_args["heartbeat_entity"], LAST_CONNECTION):
                ha.remove(entity)
            if reset_helpers:
                package = yaml.safe_load((CONFIG_DIR / "appdaemon_comfort.yaml").read_text())
                for name, spec in package["input_number"].items():
                    ha.set(f"input_number.{name}", str(float(spec["min"])))
                for name in package["input_datetime"]:
                    ha.set(f"input_datetime.{name}", "00:00:00")
                for name in package["input_boolean"]:
                    ha.set(f"input_boolean.{name}", "off")
                for name in package["input_text"]:
                    ha.set(f"input_text.{name}", "")
                ha.set(self.comfort_args["helpers"]["enable"], "on")  # the owner turns it back on
            self._report_unit()
            self.at(self.now + timedelta(seconds=30), self.start_appdaemon)

        self.at(self.now + timedelta(minutes=down_minutes), back)

    async def ev_hang_tick(self, minutes, mode=None):
        """A tick that never returns: the app's busy flag stays set. `mode` is the operating mode
        the pump is left in (the last thing the tick did)."""
        host = self.hosts[0]
        host.app.busy = True
        if mode is not None:
            self.ha.set(HEAT_MODE, mode, source="comfort_control")

        async def release():
            host.app.busy = False

        self.at(self.now + timedelta(minutes=minutes), release)

    async def ev_drop_thermostat_writes(self, room, count):
        self.drop_writes[self.comfort_args["rooms"][room]] += count

    async def ev_force_dhw(self):
        """The owner switches force DHW on by hand."""
        await self.ha.call_service("owner", "switch/turn_on", entity_id=FORCE_DHW)

    async def ev_disable(self):
        self.ha.set(self.comfort_args["helpers"]["enable"], "off", source="owner")

    async def ev_enable(self):
        self.ha.set(self.comfort_args["helpers"]["enable"], "on", source="owner")

    async def ev_away(self, on):
        self.ha.set(self.comfort_args["helpers"]["away"], "on" if on else "off", source="owner")

    # --- run ---

    async def run(self):
        while self.heap:
            when, _, item = heapq.heappop(self.heap)
            if when >= self.end:
                break
            self.now = when
            if item[0] == "call":
                await item[1]()
                await self.drain()
                continue
            _, handle, host, callback, interval, daily = item
            if handle in self.cancelled or not host.alive:
                continue
            if interval is not None:
                heapq.heappush(self.heap, (when + interval, self.seq, item))
                self.seq += 1
            elif daily is not None:
                heapq.heappush(self.heap, (host._next_local(daily), self.seq, item))
                self.seq += 1
            else:
                host.schedule[host.name].pop(handle, None)
            if not self.ha.up:
                continue
            await self.run_callback(host, callback, {})
        return self


def run_scenario(scenario):
    return asyncio.run(Simulation(scenario).run())


# --- Metrics ---------------------------------------------------------------------------------------

DAY_MINIMUM = 23.5
FLOOR = 21.0


@functools.lru_cache(maxsize=None)
def day_window(day):
    start = time(9) if day.weekday() >= 5 else time(12)
    return TZ.localize(datetime.combine(day, start)), TZ.localize(datetime.combine(day, time(22)))


def measured(sim, record):
    """Records after the warm-up days, outside any period the scenario excludes (away)."""
    first = sim.scenario.start + timedelta(days=sim.scenario.warmup_days)
    if record.at.astimezone(TZ).date() < first:
        return False
    return not any(start <= record.at < end for start, end in sim.excluded)


def app_mode_changes(sim):
    names = {host.name for host in sim.hosts} | {"comfort_control", "climate_control"}
    return [(at, old, new) for at, old, new, source in sim.ha.mode_changes if source in names]


def metrics(sim):
    if getattr(sim, "_metrics", None) is None:
        sim._metrics = _metrics(sim)
    return dict(sim._metrics)


def _metrics(sim):
    records = [r for r in sim.records if measured(sim, r)]
    days = max(1, len({r.at.astimezone(TZ).date() for r in records}))
    window_minutes = window_ok = 0
    floor_ok = [0] * len(ROOMS)
    cold_gap = longest_cold_gap = 0
    heated = 0
    water_sum = 0.0
    dhw_cycles = 0
    previous = None
    for r in records:
        local_at = r.at.astimezone(TZ)
        start, end = day_window(local_at.date())
        if start <= r.at < end:
            window_minutes += 1
            window_ok += r.rooms[0] >= DAY_MINIMUM
        for index, value in enumerate(r.rooms):
            floor_ok[index] += value >= FLOOR
        if r.mode != "Heat+DHW" and min(r.rooms) < FLOOR:
            cold_gap += 1
            longest_cold_gap = max(longest_cold_gap, cold_gap)
        else:
            cold_gap = 0
        if r.space:
            heated += 1
            water_sum += r.water
        if r.dhw and (previous is None or not previous.dhw):
            dhw_cycles += 1
        previous = r
    first, last = records[0], records[-1]
    in_range = [(at, old, new) for at, old, new in app_mode_changes(sim) if first.at <= at <= last.at]
    forced = [m for m in sim.ha.mqtt if m[1] == "SetForceDHW" and m[2] == "1" and first.at <= m[0] <= last.at]
    return {
        "days": days,
        "day_window_share": window_ok / window_minutes if window_minutes else 1.0,
        "floor_share": dict(zip(ROOMS, (ok / len(records) for ok in floor_ok))),
        "min_room": min(min(r.rooms) for r in records),
        "mode_switches_per_day": len(in_range) / days,
        "starts_per_day": (last.starts - first.starts) / days,
        "heated_hours_per_day": heated / 60 / days,
        "mean_water_heating": water_sum / heated if heated else None,
        "longest_cold_gap_min": longest_cold_gap,
        "forced_dhw_per_day": len(forced) / days,
        "dhw_cycles_per_day": dhw_cycles / days,
        "max_tank": max(r.tank for r in records),
        "max_tank_target": max(r.tank_target for r in records),
        "kwh_per_day": sum(r.elec for r in records) / 60 / days,
    }


def mode_durations(sim, source_names=("comfort_control",)):
    """(state, minutes) of each completed on/off period between controller mode changes."""
    changes = [(at, new, source) for at, old, new, source in sim.ha.mode_changes if old != new]
    periods = []
    for (at, new, source), (next_at, _, next_source) in zip(changes, changes[1:]):
        if source in source_names and next_source in source_names:
            periods.append((new, (next_at - at).total_seconds() / 60))
    return periods


def steady_hall(outdoor, water, rooms=None):
    """Steady-state hall/kitchen temperature with the heat on all the time at `water`."""
    hall = (rooms or default_rooms())[0]
    resistance = 1 / hall.k_water + 1 / hall.k_slab_room
    return (water + resistance * (hall.k_out * outdoor + hall.q_internal)) / (1 + hall.k_out * resistance)


def plant_capable(sim):
    """Can the plant hold the hall/kitchen day setpoint on the coldest day, at the curve water?"""
    by_day = defaultdict(list)
    for r in sim.records:
        by_day[r.at.astimezone(TZ).date()].append(r.outdoor)
    coldest = min(sum(v) / len(v) for v in by_day.values())
    water = comfort_control.curve_water(coldest, -5, 32, 15, 25)
    return steady_hall(coldest, water) >= DAY_MINIMUM + 1.0, coldest


# --- Scenario catalogue and cache ---------------------------------------------------------------

_CACHE = {}


def cached(scenario):
    key = (scenario.name, scenario.controller)
    if key not in _CACHE:
        _CACHE[key] = run_scenario(scenario)
    return _CACHE[key]


def parity_scenarios(controller):
    """Identical inputs for the comfort controller and the legacy apps."""
    return [
        Scenario("typical", date(2026, 11, 2), 14, "typical", "double_peak", seed=11, controller=controller),
        Scenario("cold", date(2027, 1, 11), 21, "cold", "flat", seed=12, controller=controller),
        Scenario("mild", date(2027, 3, 1), 14, "mild", "weekend_negative", seed=13, controller=controller),
        Scenario("front", date(2026, 12, 7), 14, "front", "double_peak", seed=14, controller=controller),
    ]


HALL, BEDROOM = "hall_kitchen", "bedroom"


def comfort_scenarios():
    """Comfort-controller-only scenarios (behaviour checks)."""
    return [
        Scenario(
            "overrides",
            date(2026, 11, 2),
            21,
            "typical",
            "double_peak",
            seed=21,
            events=[
                # The owner raises the hall/kitchen at 10:30 three weekdays in a row.
                ((2026, 11, 16, 10, 30), "manual_setpoint", {"room": HALL, "value": 25.0}),
                ((2026, 11, 17, 10, 40), "manual_setpoint", {"room": HALL, "value": 25.0}),
                ((2026, 11, 18, 10, 20), "manual_setpoint", {"room": HALL, "value": 25.0}),
            ],
        ),
        Scenario(
            "away",
            date(2026, 11, 2),
            14,
            "typical",
            "double_peak",
            seed=22,
            events=[
                ((2026, 11, 6, 10, 0), "away", {"on": True}),
                ((2026, 11, 9, 15, 0), "away", {"on": False}),
                # The owner lowers the bedroom at night: it holds until the next boundary.
                ((2026, 11, 11, 23, 15), "manual_setpoint", {"room": BEDROOM, "value": 20.5}),
            ],
            # Away (and its recovery) and the night the owner chose 20.5 are not comfort periods.
            exclude=[((2026, 11, 6, 10, 0), (2026, 11, 10, 22, 0)), ((2026, 11, 11, 23, 15), (2026, 11, 12, 10, 0))],
        ),
        Scenario("dst_spring", date(2027, 3, 20), 14, "dst", "weekend_negative", seed=23),
        Scenario("dst_fall", date(2026, 10, 17), 14, "dst", "double_peak", seed=24),
        # Weekend lead time 840 min (the helper maximum): 09:00 - 14 h is capped at 22:00.
        Scenario(
            "capped_lead",
            date(2027, 1, 11),
            14,
            "cold",
            "flat",
            seed=26,
            events=[((2027, 1, 10, 0, 5), "helper", {"entity": "input_number.comfort_lead_weekend_cold", "value": "840.0"})],
        ),
        # The worst case for force DHW: the unit never clears it, only the app does.
        # The owner forces DHW by hand on Thursday 07:45: held for at most 2 h.
        Scenario(
            "no_auto_clear",
            date(2026, 11, 23),
            14,
            "typical",
            "weekend_negative",
            seed=25,
            force_auto_clear=False,
            events=[((2026, 11, 26, 7, 45), "force_dhw", {})],
        ),
    ]


def thermostat_fault(room, start, end):
    entity = f"climate.thermostat_{room}"
    return [
        (start, "fault", {"key": ["thermostat", entity], "value": "unavailable"}),
        (end, "fault", {"key": ["thermostat", entity], "value": None}),
    ]


def fault_scenarios():
    return [
        Scenario(
            "faults_thermostats",
            date(2026, 11, 2),
            14,
            "typical",
            "double_peak",
            seed=31,
            force_auto_clear=False,
            events=[
                *thermostat_fault("bedroom", (2026, 11, 5, 2, 0), (2026, 11, 5, 8, 0)),
                *thermostat_fault("office_a", (2026, 11, 6, 21, 30), (2026, 11, 6, 23, 30)),
                *thermostat_fault("office_t", (2026, 11, 7, 0, 0), (2026, 11, 7, 6, 0)),
                *thermostat_fault("hall_kitchen", (2026, 11, 8, 0, 0), (2026, 11, 8, 2, 0)),
                *[
                    event
                    for room in ROOMS
                    for event in thermostat_fault(room, (2026, 11, 10, 3, 0), (2026, 11, 10, 9, 0))
                ],
                ((2026, 11, 11, 21, 59), "drop_thermostat_writes", {"room": "hall_kitchen", "count": 2}),
            ],
        ),
        Scenario(
            "faults_sensors",
            date(2026, 11, 2),
            14,
            "typical",
            "double_peak",
            seed=32,
            force_auto_clear=False,
            events=[
                ((2026, 11, 5, 6, 0), "fault", {"key": "outdoor", "value": "unavailable"}),
                ((2026, 11, 5, 18, 0), "fault", {"key": "outdoor", "value": None}),
                ((2026, 11, 6, 20, 30), "fault", {"key": "outdoor", "value": "unavailable"}),
                ((2026, 11, 7, 3, 0), "fault", {"key": "outdoor", "value": None}),
                ((2026, 11, 8, 23, 30), "fault", {"key": "price", "value": "unavailable"}),
                ((2026, 11, 10, 0, 0), "fault", {"key": "price", "value": None}),
                ((2026, 11, 10, 10, 0), "fault", {"key": "heating_mode", "value": "unavailable"}),
                ((2026, 11, 10, 16, 0), "fault", {"key": "heating_mode", "value": None}),
                ((2026, 11, 11, 8, 0), "heating_mode", {"value": 0}),
                ((2026, 11, 12, 8, 0), "heating_mode", {"value": 1}),
                ((2026, 11, 13, 0, 0), "fault", {"key": "force_heater", "value": True}),
                ((2026, 11, 14, 0, 0), "fault", {"key": "force_heater", "value": None}),
                ((2026, 11, 14, 0, 0), "fault", {"key": "short_cycle", "value": True}),
                ((2026, 11, 15, 0, 0), "fault", {"key": "short_cycle", "value": None}),
            ],
        ),
        Scenario(
            "faults_restarts",
            date(2027, 1, 11),
            14,
            "cold",
            "double_peak",
            seed=33,
            force_auto_clear=False,
            events=[
                ((2027, 1, 13, 0, 44), "appdaemon_restart", {"down_minutes": 2}),
                ((2027, 1, 14, 15, 0), "appdaemon_restart", {"down_minutes": 1}),
                ((2027, 1, 16, 1, 0), "appdaemon_down", {}),
                ((2027, 1, 16, 5, 0), "appdaemon_restart", {"down_minutes": 0}),
                ((2027, 1, 17, 10, 0), "ha_restart", {"down_minutes": 3}),
                ((2027, 1, 19, 14, 0), "ha_restart", {"down_minutes": 5, "reset_helpers": True}),
                ((2027, 1, 20, 21, 59), "appdaemon_restart", {"down_minutes": 0}),
                ((2027, 1, 21, 16, 30), "appdaemon_restart", {"down_minutes": 30}),
                ((2027, 1, 22, 15, 30), "ha_restart", {"down_minutes": 3}),
            ],
        ),
        Scenario(
            "faults_operations",
            date(2027, 1, 11),
            14,
            "cold",
            "flat",
            seed=34,
            force_auto_clear=False,
            events=[
                ((2027, 1, 13, 3, 0), "hang_tick", {"minutes": 90, "mode": "DHW"}),
                ((2027, 1, 14, 8, 0), "helper", {"entity": "input_number.comfort_day_minimum", "value": "abc"}),
                ((2027, 1, 14, 8, 0), "helper", {"entity": "input_number.comfort_hysteresis", "value": "-1"}),
                ((2027, 1, 14, 8, 0), "helper", {"entity": "input_number.comfort_curve_cold_water", "value": "99"}),
                ((2027, 1, 14, 8, 0), "helper", {"entity": "input_datetime.comfort_window_end", "value": "25:99"}),
                ((2027, 1, 14, 8, 0), "helper", {"entity": "input_number.comfort_lead_weekday_cold", "value": "unknown"}),
                ((2027, 1, 14, 8, 0), "helper", {"entity": "input_number.comfort_night_floor", "value": "16.0"}),
                ((2027, 1, 15, 8, 0), "helper", {"entity": "input_number.comfort_day_minimum", "value": "23.5"}),
                ((2027, 1, 15, 8, 0), "helper", {"entity": "input_number.comfort_hysteresis", "value": "0.3"}),
                ((2027, 1, 15, 8, 0), "helper", {"entity": "input_number.comfort_curve_cold_water", "value": "32.0"}),
                ((2027, 1, 15, 8, 0), "helper", {"entity": "input_datetime.comfort_window_end", "value": "22:00:00"}),
                ((2027, 1, 15, 8, 0), "helper", {"entity": "input_number.comfort_lead_weekday_cold", "value": "675.0"}),
                ((2027, 1, 15, 8, 0), "helper", {"entity": "input_number.comfort_night_floor", "value": "21.0"}),
                ((2027, 1, 17, 12, 0), "disable", {}),
                ((2027, 1, 17, 18, 0), "enable", {}),
                ((2027, 1, 19, 0, 0), "fault", {"key": "short_cycle", "value": True}),
                ((2027, 1, 20, 0, 0), "fault", {"key": "short_cycle", "value": None}),
            ],
        ),
    ]


def parity_fault_scenarios(controller):
    """Identical failures for both controllers: AppDaemon down on a cold night and restarts."""
    return [
        Scenario(
            "parity_failures",
            date(2027, 1, 11),
            14,
            "cold",
            "double_peak",
            seed=41,
            controller=controller,
            events=[
                ((2027, 1, 13, 9, 30), "appdaemon_down", {}),
                ((2027, 1, 13, 14, 30), "appdaemon_restart", {"down_minutes": 0}),
                ((2027, 1, 15, 2, 0), "appdaemon_restart", {"down_minutes": 1}),
                ((2027, 1, 16, 20, 0), "appdaemon_down", {}),
                ((2027, 1, 17, 1, 0), "appdaemon_restart", {"down_minutes": 0}),
                ((2027, 1, 18, 10, 0), "ha_restart", {"down_minutes": 3}),
            ],
        )
    ]


def scenario(name, controller="comfort"):
    for item in (
        parity_scenarios(controller)
        + comfort_scenarios()
        + fault_scenarios()
        + parity_fault_scenarios(controller)
    ):
        if item.name == name and item.controller == controller:
            return cached(item)
    raise KeyError(name)
