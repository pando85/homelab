"""Fault injection in the closed-loop simulation (design section 10: never leave the house
without heating, no dangerous writes).

Faults: thermostats offline (one, all, lost writes), outdoor / price / heating-mode sensors
unavailable, Heating_Mode flipped to curve, the force heater on, compressor short-cycling, HA
restarts (helpers restored, helpers re-created at their minimum), AppDaemon restarts and outages,
a hung tick, garbage helper values, disable / enable.
"""

from datetime import date, timedelta

import pytest

from comfort_control import MIN_OFF
from tests import comfort_checks as checks
from tests.comfort_sim import FORCE_DHW, HEAT_MODE, SENSOR, TZ, Z1_HELPER, local, metrics, scenario

pytestmark = pytest.mark.long

FAULT_RUNS = ["faults_thermostats", "faults_sensors", "faults_restarts", "faults_operations"]
# Longest stretch with the pump in DHW only while a room is below 21 degC: the minimum off time
# plus one tick; with AppDaemon silent the watchdog acts after 15 minutes.
MAX_COLD_GAP = MIN_OFF.total_seconds() / 60 + 1


def days_of(sim):
    return [sim.scenario.start + timedelta(days=offset) for offset in range(1, sim.scenario.days)]


def mode_at(sim, at):
    return checks.record_at(sim, at).mode


# --- Never without heating, never a dangerous write ---------------------------------------------


@pytest.mark.parametrize("name", FAULT_RUNS)
def test_never_without_heating(name):
    sim = scenario(name)
    result = metrics(sim)
    assert result["longest_cold_gap_min"] <= MAX_COLD_GAP, result
    assert result["min_room"] >= 20.8, result
    for share in result["floor_share"].values():
        assert share >= 0.97, result


@pytest.mark.parametrize("name", FAULT_RUNS)
def test_no_dangerous_writes(name):
    sim = scenario(name)
    assert checks.check_water_writes(sim) == []
    touched = {target for _, who, _, target, _ in sim.ha.services if who == checks.APP and target}
    assert not [entity for entity in touched if "steril" in entity or "heater" in entity]
    assert all(record.tank_target <= 50 for record in sim.records)
    blocked = [m for m in sim.ha.mqtt if m[1].startswith("blocked:")]
    app_values = {str(float(data["value"])) for _, data in checks.calls(sim, "input_number/set_value", Z1_HELPER)}
    assert not [m for m in blocked if m[2] in app_values and "Z1" in m[1]], blocked  # the guard never had to stop the app
    assert not [line for line in sim.logs if line[2] == "CALLBACK_ERROR"]


@pytest.mark.parametrize("name", FAULT_RUNS)
def test_force_dhw_always_released(name):
    sim = scenario(name)
    down = [(local(2027, 1, 21, 16, 30), local(2027, 1, 21, 17, 1))] if name == "faults_restarts" else []
    problems = [p for p in checks.check_dhw(sim, []) if "forced DHW windows" not in p]
    for on, off in checks.forced_windows(sim):
        if off is not None and not any(start <= off and on <= end for start, end in down):
            assert off - on <= timedelta(hours=2, minutes=2), (on, off)
    assert problems == []


# --- Thermostats -------------------------------------------------------------------------------


def test_one_thermostat_offline_keeps_heating_on():
    sim = scenario("faults_thermostats")
    for start, end in (
        (local(2026, 11, 5, 2, 0), local(2026, 11, 5, 8, 0)),
        (local(2026, 11, 7, 0, 0), local(2026, 11, 7, 6, 0)),
    ):
        # Review fix 1: a silent room may be cold, so the pump is on once the minimum off time
        # has passed and never switched off while the room is missing.
        settled = start + MIN_OFF + timedelta(minutes=2)
        assert all(r.mode == "Heat+DHW" for r in sim.records if settled <= r.at < end), (start, end)


def test_all_thermostats_offline_switch_heating_on_and_keep_it():
    sim = scenario("faults_thermostats")
    start, end = local(2026, 11, 10, 3, 0), local(2026, 11, 10, 9, 0)
    assert checks.longest_dhw_only(sim, start, end) <= MAX_COLD_GAP
    assert all(r.mode == "Heat+DHW" for r in sim.records if start + MIN_OFF + timedelta(minutes=2) <= r.at < end)
    assert any("no reading from" in line[3] or "heating kept on" in line[3] for line in sim.logs)


def test_boundary_during_an_outage_is_written_when_the_thermostat_returns():
    sim = scenario("faults_thermostats")
    # Sunday 11-08: the hall's early start (00:10 cold band) falls in its 00:00-02:00 outage.
    early, _ = checks.early_start(sim, date(2026, 11, 8))
    assert local(2026, 11, 8, 0, 0) < early < local(2026, 11, 8, 2, 0)
    assert checks.hall_setpoint(sim, local(2026, 11, 8, 2, 7)) == 24.0
    # The night boundary of office_a (22:00) falls in its 21:30-23:30 outage.
    office_a = 2
    assert checks.record_at(sim, local(2026, 11, 6, 23, 37)).setpoints[office_a] == 21.5


def test_lost_boundary_writes_are_retried():
    # Review fix 6: two writes lost by the cloud at the 22:00 boundary: retried every 5 minutes.
    sim = scenario("faults_thermostats")
    writes = [at for at, data in checks.calls(sim, "climate/set_temperature", checks.HALL_ENTITY) if at.date() == date(2026, 11, 11)]
    late = [at for at in writes if at >= local(2026, 11, 11, 22, 0)]
    assert len(late) == 3, late
    assert checks.hall_setpoint(sim, local(2026, 11, 11, 22, 12)) == 21.5
    assert checks.hall_setpoint(sim, local(2026, 11, 11, 22, 8)) == 24.0


# --- Sensors -----------------------------------------------------------------------------------


def test_outdoor_unavailable_stops_water_writes_and_uses_the_longest_lead():
    sim = scenario("faults_sensors")
    for start, end in ((local(2026, 11, 5, 6, 0), local(2026, 11, 5, 18, 0)), (local(2026, 11, 6, 20, 30), local(2026, 11, 7, 3, 0))):
        assert not [at for at, _ in checks.calls(sim, "input_number/set_value", Z1_HELPER) if start <= at < end + timedelta(minutes=1)]
    # Saturday 11-07: no band input at 21:00-22:00, so the longest weekend lead (530 min, 00:10).
    assert checks.hall_setpoint(sim, local(2026, 11, 7, 0, 9)) == 21.5
    assert checks.hall_setpoint(sim, local(2026, 11, 7, 0, 12)) == 24.0


def test_price_unavailable_skips_the_dhw_window_with_a_notification():
    sim = scenario("faults_sensors")
    forced = {at.astimezone(TZ).date() for at, _ in checks.calls(sim, "switch/turn_on", FORCE_DHW)}
    assert date(2026, 11, 9) not in forced
    assert {date(2026, 11, 8), date(2026, 11, 10)} <= forced
    assert any("no full PVPC price curve" in n[2] and n[0].date() == date(2026, 11, 9) for n in sim.ha.notifications)


def test_heating_mode_unavailable_or_curve_blocks_water_writes():
    sim = scenario("faults_sensors")
    for start, end in ((local(2026, 11, 10, 10, 0), local(2026, 11, 10, 16, 0)), (local(2026, 11, 11, 8, 0), local(2026, 11, 12, 8, 0))):
        assert not [at for at, _ in checks.calls(sim, "input_number/set_value", Z1_HELPER) if start <= at < end + timedelta(minutes=1)]
    # In curve mode the unit runs on its own curve plus the shift: the pump still heats.
    curve_day = [r for r in sim.records if local(2026, 11, 11, 9, 0) <= r.at < local(2026, 11, 12, 8, 0)]
    assert any(r.space for r in curve_day)
    assert all(r.heating_mode == 0 for r in curve_day)


def test_starts_alarm_fires_on_the_short_cycling_day_only():
    for name, day in (("faults_sensors", date(2026, 11, 14)), ("faults_operations", date(2027, 1, 19))):
        sim = scenario(name)
        alarms = [n for n in sim.ha.notifications if "compressor starts" in n[2]]
        assert [n[0].astimezone(TZ).date() for n in alarms] == [day], (name, alarms)


# --- Restarts ----------------------------------------------------------------------------------


def test_appdaemon_down_watchdog_restores_heating_at_the_last_water_temperature():
    sim = scenario("faults_restarts")
    down, back = local(2027, 1, 16, 1, 0), local(2027, 1, 16, 5, 0)
    assert mode_at(sim, down) == "DHW"  # the controller had switched heating off
    assert checks.longest_dhw_only(sim, down, back) <= 16
    assert [n[1] for n in sim.ha.notifications if down <= n[0] < back] == ["watchdog"]
    assert mode_at(sim, down + timedelta(minutes=20)) == "Heat+DHW"
    water = {r.target for r in sim.records if down + timedelta(minutes=1) <= r.at < back}
    assert len(water) == 1  # the unit keeps the last request
    assert not [s for s in sim.ha.services if down < s[0] < back and s[1] == checks.APP]


def test_appdaemon_restart_mid_dhw_window_keeps_one_window_and_releases_it():
    sim = scenario("faults_restarts")
    for day in (date(2027, 1, 14), date(2027, 1, 21)):
        ons = [at for at, _ in checks.calls(sim, "switch/turn_on", FORCE_DHW) if at.astimezone(TZ).date() == day]
        assert len(ons) == 1, (day, ons)
        windows = [(on, off) for on, off in checks.forced_windows(sim) if on.astimezone(TZ).date() == day]
        assert windows and all(off is not None and off - on <= timedelta(hours=2, minutes=2) for on, off in windows), windows


def test_restart_at_a_boundary_does_not_skip_it():
    sim = scenario("faults_restarts")
    # AppDaemon restarts at 00:44 on 01-13 for two minutes, over the 00:45 early start.
    early, _ = checks.early_start(sim, date(2027, 1, 13))
    assert checks.hall_setpoint(sim, early + timedelta(minutes=5)) == 24.0


def test_ha_restart_reapplies_the_current_boundary_only():
    sim = scenario("faults_restarts")
    restart = local(2027, 1, 17, 10, 0)  # a Sunday, inside the day window
    writes = [at for at, _ in checks.calls(sim, "climate/set_temperature") if restart <= at < restart + timedelta(hours=1)]
    assert checks.hall_setpoint(sim, restart + timedelta(minutes=10)) == 24.0
    assert len(writes) <= 4


def test_ha_restart_with_helpers_reset_reseeds_and_never_uses_the_minimums():
    # Review fix 3: helpers re-created at their HA minimum with seeded off are re-seeded at once.
    sim = scenario("faults_restarts")
    restart = local(2027, 1, 19, 14, 0)
    after = [r for r in sim.records if restart <= r.at < restart + timedelta(days=2)]
    assert all(value is None or value >= 21.5 for r in after for value in r.setpoints)
    assert sim.ha.get("input_number.comfort_day_minimum") == "23.5"
    assert sim.ha.get("input_boolean.comfort_helpers_seeded") == "on"
    # Seeding turns the water dry run back on: the unit keeps the last request until the owner
    # turns it off again.
    assert sim.ha.get("input_boolean.comfort_water_dry_run") == "on"
    assert not [at for at, _ in checks.calls(sim, "input_number/set_value", Z1_HELPER) if at > restart]
    assert any("(dry run)" in line[3] and line[0] > restart for line in sim.logs if line[3].startswith("Water request"))


# --- Operations ----------------------------------------------------------------------------------


def test_hung_tick_heartbeat_goes_stale_and_the_watchdog_restores_heating():
    # Review fix 5: the per-tick heartbeat, not AppDaemon's connection, feeds the watchdog.
    sim = scenario("faults_operations")
    start, end = local(2027, 1, 13, 3, 0), local(2027, 1, 13, 4, 30)
    assert checks.record_at(sim, start + timedelta(minutes=1)).mode == "DHW"  # the tick hung in DHW
    assert checks.longest_dhw_only(sim, start, end) <= 16
    restored = [n[0] for n in sim.ha.notifications if n[1] == "watchdog" and start <= n[0] < end]
    assert len(restored) == 1 and restored[0] - start <= timedelta(minutes=16), restored
    assert all(r.mode == "Heat+DHW" for r in sim.records if restored[0] < r.at < end)


def test_garbage_helpers_fall_back_to_the_defaults():
    sim = scenario("faults_operations")
    start, end = local(2027, 1, 14, 8, 0), local(2027, 1, 15, 8, 0)
    during = [r for r in sim.records if start < r.at < end]
    assert {r.setpoints[0] for r in during} <= {21.5, 24.0}
    assert all(value == 21.5 for r in during for value in r.setpoints[1:])
    # Window end "25:99" -> 22:00; night floor 16 (out of range) -> 21.
    assert checks.hall_setpoint(sim, local(2027, 1, 14, 22, 2)) == 21.5
    assert any("Helper day_minimum is 'abc'" in line[3] for line in sim.logs)


def test_disable_restores_heating_and_releases_force_dhw():
    sim = scenario("faults_operations")
    off, on = local(2027, 1, 17, 12, 0), local(2027, 1, 17, 18, 0)
    assert mode_at(sim, off + timedelta(minutes=1)) == "Heat+DHW"
    assert not [r for r in sim.records if off < r.at < on and r.force]
    app_writes = [s for s in sim.ha.services if off < s[0] < on and s[1] == checks.APP and s[2] not in ("set_state",)]
    assert [s[2] for s in app_writes] in ([], ["switch/turn_off"])
    assert not [s for s in sim.ha.services if off < s[0] < on and s[1] == checks.APP and s[2] == "set_state" and s[3] == HEAT_MODE]
    # Enabling writes the current schedule (a Sunday at 18:00: the day setpoint).
    assert checks.hall_setpoint(sim, on + timedelta(minutes=2)) == 24.0
