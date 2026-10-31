"""Closed-loop simulation of the comfort controller: simulated decisions over 14-21 days.

The real ComfortControl app runs against tests/comfort_sim.py (house, heat pump, Home Assistant).
Each scenario is simulated once per session and shared by the tests that read it. Synthetic
weather and prices only.
"""

from collections import Counter
from datetime import date, timedelta

import pytest

from comfort_control import MAX_STARTS_PER_DAY
from tests import comfort_checks as checks
from tests import comfort_sim as sim_module
from tests.comfort_sim import (
    DT,
    TZ,
    WATER_TAU,
    default_rooms,
    local,
    metrics,
    plant_capable,
    scenario,
)

pytestmark = pytest.mark.long

COMFORT_RUNS = ["typical", "cold", "mild", "front", "overrides", "away", "dst_spring", "dst_fall", "capped_lead", "no_auto_clear"]
DAY_WINDOW_SHARE = 0.95  # hall/kitchen >= 23.5 degC in the day window, after the first 2 days
FLOOR_SHARE = 0.97  # every room >= 21 degC


def run_days(sim, first_offset=2):
    """The full simulated days after the warm-up."""
    start = sim.scenario.start + timedelta(days=first_offset)
    return [start + timedelta(days=offset) for offset in range(sim.scenario.days - first_offset)]


# --- The plant model ---------------------------------------------------------------------------


def _coast_and_warm(room, outdoor):
    room.tr = 23.5 if room.name == "hall_kitchen" else 21.5
    room.ts = room.tr + (room.k_out * (room.tr - outdoor) - room.q_internal) / room.k_slab_room

    def step(water=None):
        heat = room.k_water * max(0.0, water - room.ts) if water is not None else 0.0
        flow = room.k_slab_room * (room.ts - room.tr)
        room.ts += DT * (heat - flow) / room.c_slab
        room.tr += DT * (flow - room.k_out * (room.tr - outdoor) + room.q_internal) / room.c_room

    start = room.tr
    for _ in range(600):
        step()
    cooling = (start - room.tr) / 10
    water, trace = room.ts, []
    for _ in range(480):
        water += (26 - water) * min(1.0, DT / WATER_TAU)
        step(water)
        trace.append(room.tr)
    trough = trace.index(min(trace))
    warming = (trace[-1] - trace[trough]) / ((len(trace) - trough) / 60)
    return cooling, warming, trough / 60


@pytest.mark.parametrize("outdoor", [0, 5, 10])
def test_plant_matches_the_measured_rates(outdoor):
    """B0 rates (design section 6): hall/kitchen night cooling 0.04-0.12 K/h, warming 0.08-0.19 K/h
    at 26 degC water, lag after the start 0.5-0.75 h (thermostat resolution about 0.5 h, so
    0.4-1.0 h here); slab time constant about 150 h. The other rooms are similar."""
    for room in default_rooms():
        assert (room.c_room + room.c_slab) / room.k_out == pytest.approx(150, rel=0.01)
        cooling, warming, lag = _coast_and_warm(room, outdoor)
        assert 0.0 <= cooling <= 0.13, (room.name, cooling)
        assert 0.07 <= warming <= 0.21, (room.name, warming)
        if room.name == "hall_kitchen":
            assert 0.03 <= cooling, cooling
            assert 0.4 <= lag <= 1.0, lag


def test_servers_warm_office_a_and_water_raises_the_heat():
    rooms = {room.name: room for room in default_rooms()}
    assert rooms["office_a"].q_internal / rooms["office_a"].k_out > rooms["office_t"].q_internal / rooms["office_t"].k_out
    hall = rooms["hall_kitchen"]
    assert sim_module.steady_hall(0, 30) > sim_module.steady_hall(0, 26) + 3


# --- Comfort -----------------------------------------------------------------------------------


@pytest.mark.parametrize("name", COMFORT_RUNS)
def test_comfort_when_the_plant_is_capable(name):
    sim = scenario(name)
    capable, coldest = plant_capable(sim)
    result = metrics(sim)
    if not capable:
        pytest.skip(f"{name}: the plant cannot hold 24 degC at the curve water on a {coldest:.1f} degC day")
    assert result["day_window_share"] >= DAY_WINDOW_SHARE, result
    for room, share in result["floor_share"].items():
        assert share >= FLOOR_SHARE, (room, share)


def test_every_scenario_states_whether_the_plant_is_capable():
    flags = {name: plant_capable(scenario(name)) for name in COMFORT_RUNS}
    assert all(isinstance(capable, bool) for capable, _ in flags.values())
    # A plant that cannot hold the day setpoint is flagged, not silently counted as a failure.
    assert not sim_module.steady_hall(-5, 22) >= 24.5


# --- Boundaries --------------------------------------------------------------------------------


@pytest.mark.parametrize("name", ["typical", "cold", "mild", "front", "dst_spring", "dst_fall", "capped_lead", "no_auto_clear"])
def test_early_start_lead_times_match_the_band_table(name):
    sim = scenario(name)
    assert checks.check_boundaries(sim, run_days(sim)) == []


@pytest.mark.parametrize("name", ["typical", "cold", "mild", "front", "dst_spring", "dst_fall", "capped_lead", "no_auto_clear"])
def test_setpoints_written_only_at_boundaries(name):
    sim = scenario(name)
    assert checks.check_writes_only_at_boundaries(sim, run_days(sim, 1)) == []


def test_band_table_is_the_design_table():
    sim = scenario("typical")
    bands = Counter()
    for day in run_days(sim):
        lead, band = checks.lead_minutes(sim, day)
        assert lead == checks.DESIGN_LEADS[("weekend" if day.weekday() >= 5 else "weekday", band)]
        bands[band] += 1
    assert bands  # the typical scenario exercises at least one band


def test_bands_cover_cold_mid_and_mild():
    seen = {checks.lead_minutes(scenario(name), day)[1] for name in ("cold", "typical", "mild") for day in run_days(scenario(name))}
    assert seen == {"cold", "mid", "mild"}


def test_early_start_is_capped_at_the_previous_window_end():
    sim = scenario("capped_lead")
    capped = [day for day in run_days(sim) if day.weekday() >= 5]
    assert capped
    for day in capped:
        early, (lead, band) = checks.early_start(sim, day)
        assert (lead, band) == (840, "cold")
        assert early == local(*(day - timedelta(days=1)).timetuple()[:3], 22, 0)
        # The hall keeps its day setpoint overnight: no night write at 22:00 on Friday/Saturday.
        assert checks.hall_setpoint(sim, early + timedelta(minutes=2)) == 24.0
        assert checks.hall_setpoint(sim, early + timedelta(hours=5)) == 24.0


# --- Manual overrides ------------------------------------------------------------------------


def test_manual_override_holds_until_the_next_boundary():
    sim = scenario("overrides")
    for day, minute in ((16, 30), (17, 40), (18, 20)):
        start = local(2026, 11, day, 10, minute)
        for offset in (2, 60, 300, 680):
            at = start + timedelta(minutes=offset)
            if at < local(2026, 11, day, 22, 0):
                assert checks.hall_setpoint(sim, at) == 25.0, at
        assert checks.hall_setpoint(sim, local(2026, 11, day, 22, 2)) == 21.5
    # The app never wrote the hall between the override and the window end.
    writes = [at for at, data in checks.calls(sim, "climate/set_temperature", checks.HALL_ENTITY)]
    assert not [at for at in writes if local(2026, 11, 16, 10, 30) < at < local(2026, 11, 16, 21, 59)]


def test_overrides_are_logged_and_a_suggestion_follows_the_third():
    sim = scenario("overrides")
    logged = [line for line in sim.logs if line[3].startswith("Manual override hall_kitchen")]
    assert len(logged) == 3
    status = sim.ha.states["sensor.comfort_control_status"]["attributes"]["overrides"]
    assert [(item["room"], item["new"], item["direction"]) for item in status] == [("hall_kitchen", 25.0, "raise")] * 3
    suggestions = [n for n in sim.ha.notifications if "Nothing was changed" in n[2]]
    assert len(suggestions) == 1
    assert suggestions[0][0] == local(2026, 11, 18, 10, 20)
    # Own writes are never logged as overrides.
    assert not [line for line in sim.logs if line[3].startswith("Manual override") and "hall_kitchen" not in line[3]]


def test_a_lowered_night_setpoint_holds_until_the_early_start():
    sim = scenario("away")
    bedroom = 1
    lowered = local(2026, 11, 11, 23, 15)
    early, _ = checks.early_start(sim, date(2026, 11, 12))
    assert early > lowered
    assert checks.record_at(sim, early - timedelta(minutes=1)).setpoints[bedroom] == 20.5
    assert checks.record_at(sim, early + timedelta(minutes=2)).setpoints[bedroom] == 21.5


# --- Away --------------------------------------------------------------------------------------


def test_away_sets_every_room_to_21_5_and_recovers():
    sim = scenario("away")
    on, off = local(2026, 11, 6, 10, 0), local(2026, 11, 9, 15, 0)
    assert checks.record_at(sim, on + timedelta(minutes=2)).setpoints == (21.5, 21.5, 21.5, 21.5)
    away_writes = [at for at, _ in checks.calls(sim, "climate/set_temperature") if on + timedelta(minutes=1) < at < off]
    assert away_writes == []  # boundaries pass without writing while away
    assert checks.record_at(sim, local(2026, 11, 8, 12, 0)).setpoints == (21.5, 21.5, 21.5, 21.5)
    # Away off on a Monday at 15:00 is inside the day window: the day setpoint is written at once.
    assert checks.record_at(sim, off + timedelta(minutes=2)).setpoints[0] == 24.0
    # Recovery is physics: about 2 K at the measured 0.08-0.19 K/h warming rate takes 10-25 h.
    recovered = next(r.at for r in sim.records if r.at > off and r.rooms[0] >= 23.5)
    assert recovered - off <= timedelta(hours=24), recovered
    window = [r for r in sim.records if local(2026, 11, 11, 12, 0) <= r.at < local(2026, 11, 11, 22, 0)]
    assert sum(r.rooms[0] >= 23.5 for r in window) / len(window) >= 0.95
    assert min(r.rooms[i] for r in sim.records if on <= r.at for i in range(4)) >= 21.0


# --- Machine limits ----------------------------------------------------------------------------


@pytest.mark.parametrize("name", COMFORT_RUNS)
def test_minimum_on_and_off_times_hold(name):
    assert checks.check_machine_limits(scenario(name)) == []


@pytest.mark.parametrize("name", COMFORT_RUNS)
def test_starts_alarm_silent_on_normal_days(name):
    sim = scenario(name)
    assert metrics(sim)["starts_per_day"] < MAX_STARTS_PER_DAY
    assert not [n for n in sim.ha.notifications if "compressor starts" in n[2]]


# --- Water temperature -------------------------------------------------------------------------


@pytest.mark.parametrize("name", COMFORT_RUNS)
def test_water_writes_are_safe(name):
    sim = scenario(name)
    assert checks.calls(sim, "input_number/set_value", sim_module.Z1_HELPER)  # the curve is used
    assert checks.check_water_writes(sim) == []


def test_water_follows_the_curve_colder_is_warmer():
    cold, mild = metrics(scenario("cold")), metrics(scenario("mild"))
    assert cold["mean_water_heating"] > mild["mean_water_heating"] + 2
    assert 20 <= mild["mean_water_heating"] and cold["mean_water_heating"] <= 35


# --- DHW ---------------------------------------------------------------------------------------


@pytest.mark.parametrize("name", COMFORT_RUNS)
def test_one_dhw_window_per_day_released_and_capped(name):
    sim = scenario(name)
    assert checks.check_dhw(sim, run_days(sim, 1)) == []
    assert metrics(sim)["max_tank_target"] <= 50


def test_force_dhw_released_by_the_app_when_the_unit_keeps_it():
    sim = scenario("no_auto_clear")
    offs = checks.calls(sim, "switch/turn_off", sim_module.FORCE_DHW)
    ons = checks.calls(sim, "switch/turn_on", sim_module.FORCE_DHW)
    assert len(offs) >= len(ons) - 1
    for (on, _), (off, _) in zip(ons, offs):
        assert timedelta(hours=1, minutes=59) <= off - on <= timedelta(hours=2, minutes=1)


@pytest.mark.parametrize("name", ["typical", "cold", "mild", "front", "no_auto_clear"])
def test_dhw_window_is_the_cheapest_by_price_over_cop(name):
    sim = scenario(name)
    for day in run_days(sim):
        (start, _), = [c for c in checks.calls(sim, "switch/turn_on", sim_module.FORCE_DHW) if c[0].astimezone(TZ).date() == day]
        # Exactly the best window on what the app can know (its sensor history, an independent
        # recomputation), and within 10 % of the physical optimum on the day's true temperatures
        # (persistence forecast, unit sensor reading about 2 K low while the compressor runs).
        assert checks.dhw_window_regret(sim, day, start, "sensor") == pytest.approx(0, abs=1e-9), (day, start)
        regret = checks.dhw_window_regret(sim, day, start, "true")
        assert regret is not None and regret <= 0.006, (day, start, regret)


def test_negative_midday_prices_attract_the_weekend_window():
    sim = scenario("mild")
    weekend = [day for day in run_days(sim) if day.weekday() >= 5]
    for day in weekend:
        (start, _), = [c for c in checks.calls(sim, "switch/turn_on", sim_module.FORCE_DHW) if c[0].astimezone(TZ).date() == day]
        assert 11 <= start.astimezone(TZ).hour <= 14, (day, start)


def test_no_dhw_forcing_while_the_force_heater_is_on():
    sim = scenario("faults_sensors")
    heater_day = date(2026, 11, 13)
    assert not [c for c in checks.calls(sim, "switch/turn_on", sim_module.FORCE_DHW) if c[0].astimezone(TZ).date() == heater_day]
    assert any("DHW not forced: force heater" in n[2] for n in sim.ha.notifications)


# --- DST ---------------------------------------------------------------------------------------


@pytest.mark.parametrize("name,dst_day", [("dst_spring", date(2027, 3, 28)), ("dst_fall", date(2026, 10, 25))])
def test_dst_days(name, dst_day):
    sim = scenario(name)
    assert dst_day.weekday() == 6  # Sunday: weekend window, Monday after it is a weekday
    assert checks.check_boundaries(sim, [dst_day, dst_day + timedelta(days=1)]) == []
    early_sunday, _ = checks.early_start(sim, dst_day)
    early_monday, _ = checks.early_start(sim, dst_day + timedelta(days=1))
    assert early_sunday.astimezone(TZ).date() in (dst_day, dst_day - timedelta(days=1))
    sunday_start, _ = sim_module.day_window(dst_day)
    monday_start, _ = sim_module.day_window(dst_day + timedelta(days=1))
    assert (sunday_start.hour, monday_start.hour) == (9, 12)
    assert checks.check_dhw(sim, [dst_day - timedelta(days=1), dst_day, dst_day + timedelta(days=1)]) == []
    assert not [line for line in sim.logs if line[2] in ("ERROR", "CALLBACK_ERROR")]


def test_simulations_log_no_errors():
    for name in COMFORT_RUNS:
        sim = scenario(name)
        assert not [line for line in sim.logs if line[2] in ("ERROR", "CALLBACK_ERROR")], name
