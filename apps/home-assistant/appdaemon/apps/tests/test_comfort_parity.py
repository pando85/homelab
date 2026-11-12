"""Operational parity with the legacy apps: the comfort controller against the archived
climate_control and dhw_control, in the same simulator under identical weather, prices and
failures.

The legacy apps are imported from apps/home-assistant/appdaemon/archive/legacy-price-control/ (tests
only) with their archived apps.yaml and watchdog. Legacy assumptions, as it ran: Z1 request fixed at
26 degC (direct mode), thermostats at fixed setpoints (hall/kitchen 24.0, other rooms 21.5),
`min_hours_per_day` 12, DHW forced at the cheapest hour of each 12-hour block.

Run with -s to print the parity table.
"""

import pytest

from comfort_control import MIN_OFF, MIN_ON
from tests import comfort_checks as checks
from tests.comfort_sim import HEAT_MODE, TZ, local, metrics, parity_fault_scenarios, parity_scenarios, scenario

pytestmark = pytest.mark.long

PARITY = [item.name for item in parity_scenarios("comfort")] + [item.name for item in parity_fault_scenarios("comfort")]
COLD = ["cold", "front", "parity_failures"]

# Tolerances (each comparison below names its own).
MODE_SWITCH_TOLERANCE = 1.0  # mode switches per day: new <= legacy + 1
START_RATIO = 1.3  # compressor starts per day: new <= 1.3 x legacy ...
START_SLACK = 2.0  # ... or legacy + 2, whichever is larger (3-6 starts a day: one start is 20-30 %)
TANK_TOLERANCE = 0.5  # K: highest tank temperature, new <= legacy + 0.5
WATCHDOG_MINUTES = 16  # minutes in DHW only after AppDaemon goes silent: 15 min watchdog + 1 tick


def pair(name):
    return scenario(name, "comfort"), scenario(name, "legacy")


def written(sim, names):
    return {(service, target) for _, who, service, target, _ in sim.ha.services if who in names and target}


def parity_table():
    rows = [
        "| scenario | app | mode switches/day | starts/day | longest cold gap (min) | forced DHW/day "
        "| DHW cycles/day | max tank degC | heated h/day | mean water degC | kWh/day | hall window share |",
        "|---|---|---|---|---|---|---|---|---|---|---|---|",
    ]
    for name in PARITY:
        for label, sim in zip(("new", "legacy"), pair(name)):
            m = metrics(sim)
            rows.append(
                f"| {name} | {label} | {m['mode_switches_per_day']:.1f} | {m['starts_per_day']:.1f} "
                f"| {m['longest_cold_gap_min']} | {m['forced_dhw_per_day']:.1f} | {m['dhw_cycles_per_day']:.1f} "
                f"| {m['max_tank']:.1f} | {m['heated_hours_per_day']:.1f} | {m['mean_water_heating']:.1f} "
                f"| {m['kwh_per_day']:.1f} | {m['day_window_share']:.2f} |"
            )
    return "\n".join(rows)


def test_print_the_parity_table():
    table = parity_table()
    print("\n" + table)
    assert table.count("\n") == 1 + 2 * len(PARITY)


@pytest.mark.parametrize("name", PARITY)
def test_mode_command_path_is_the_same_and_extra_entities_are_justified(name):
    new, legacy = pair(name)
    new_writes = written(new, {"comfort_control"})
    legacy_writes = written(legacy, {"climate_control", "dhw_control"})
    # The same mode command: AppDaemon set_state on the selector, the "Set heatmode" automation.
    assert ("set_state", HEAT_MODE) in new_writes and ("set_state", HEAT_MODE) in legacy_writes
    new_modes = {new for _, _, new, source in new.ha.mode_changes if source == "comfort_control"}
    legacy_modes = {new for _, _, new, source in legacy.ha.mode_changes if source == "climate_control"}
    assert new_modes == legacy_modes == {"Heat+DHW", "DHW"}
    # Legacy entities the new app also writes: the mode and force DHW.
    assert legacy_writes <= new_writes
    justified = {
        ("set_state", HEAT_MODE),
        ("switch/turn_on", "switch.aquarea_force_dhw_mode"),  # legacy dhw_control
        ("switch/turn_off", "switch.aquarea_force_dhw_mode"),  # releases what it forced
        ("input_number/set_value", "input_number.heishamon_heatshift_z1"),  # software curve
        ("input_number/set_value", "input_number.heishamon_tank_temp"),  # tank target <= 50
        ("set_state", "sensor.comfort_control_status"),
        ("set_state", "sensor.comfort_control_heartbeat"),
        ("notify/send_message", "notify.agil"),  # as legacy
        ("input_text/set_value", "input_text.comfort_dhw_plan"),  # DHW plan across HA restarts
        *{("climate/set_temperature", f"climate.thermostat_{room}") for room in ("hall_kitchen", "bedroom", "office_a", "office_t")},
    }
    assert new_writes <= justified, new_writes - justified


@pytest.mark.parametrize("name", PARITY)
def test_mode_switches_per_day_and_no_flapping(name):
    new, legacy = pair(name)
    assert metrics(new)["mode_switches_per_day"] <= metrics(legacy)["mode_switches_per_day"] + MODE_SWITCH_TOLERANCE
    # No flapping: every on period of the new app lasts MIN_ON, every off period MIN_OFF.
    assert checks.check_machine_limits(new) == []
    shortest = min(minutes for _, minutes in _periods(new))
    assert shortest >= min(MIN_ON, MIN_OFF).total_seconds() / 60


def _periods(sim):
    changes = [(at, new) for at, old, new, source in sim.ha.mode_changes if old != new and source == "comfort_control"]
    return [(state, (b - a).total_seconds() / 60) for (a, state), (b, _) in zip(changes, changes[1:])]


@pytest.mark.parametrize("name", PARITY)
def test_compressor_starts_per_day(name):
    new, legacy = pair(name)
    new_starts, legacy_starts = metrics(new)["starts_per_day"], metrics(legacy)["starts_per_day"]
    assert new_starts <= max(START_RATIO * legacy_starts, legacy_starts + START_SLACK), (new_starts, legacy_starts)


@pytest.mark.parametrize("name", COLD)
def test_no_longer_gap_without_heating_than_legacy_in_the_cold(name):
    new, legacy = pair(name)
    assert metrics(new)["longest_cold_gap_min"] <= metrics(legacy)["longest_cold_gap_min"]
    assert metrics(new)["longest_cold_gap_min"] <= MIN_OFF.total_seconds() / 60 + 1


@pytest.mark.parametrize("name", PARITY)
def test_dhw_cycles_and_tank_temperature(name):
    new, legacy = pair(name)
    new_m, legacy_m = metrics(new), metrics(legacy)
    # One forced window a day; the archived dhw_control forced one per 12-hour block (two a day).
    assert new_m["forced_dhw_per_day"] == pytest.approx(1.0, abs=0.1)
    assert new_m["forced_dhw_per_day"] <= legacy_m["forced_dhw_per_day"]
    assert new_m["dhw_cycles_per_day"] <= legacy_m["dhw_cycles_per_day"] + 0.1
    assert new_m["max_tank"] <= legacy_m["max_tank"] + TANK_TOLERANCE
    assert new_m["max_tank_target"] <= 50


def test_watchdog_and_restarts_at_least_as_safe_as_legacy():
    new, legacy = pair("parity_failures")
    outages = [
        (local(2027, 1, 13, 9, 30), local(2027, 1, 13, 14, 30)),
        (local(2027, 1, 16, 20, 0), local(2027, 1, 17, 1, 0)),
    ]
    for start, end in outages:
        new_gap = checks.longest_dhw_only(new, start, end)
        legacy_gap = checks.longest_dhw_only(legacy, start, end)
        assert new_gap <= max(legacy_gap, WATCHDOG_MINUTES), (start, new_gap, legacy_gap)
    assert metrics(new)["longest_cold_gap_min"] <= metrics(legacy)["longest_cold_gap_min"]
    # After every restart the new app neither repeats nor loses the day's DHW window.
    days = {record.at.astimezone(TZ).date() for record in new.records if record.at >= local(2027, 1, 12)}
    assert checks.check_dhw(new, sorted(days)[:-1]) == []
    for sim in (new, legacy):
        assert not [line for line in sim.logs if line[2] == "CALLBACK_ERROR"]


@pytest.mark.parametrize("name", PARITY)
def test_heated_hours_reported(name):
    """Informational: heated hours and water temperature for both (no pass/fail threshold)."""
    new, legacy = pair(name)
    for sim in (new, legacy):
        m = metrics(sim)
        assert 0 < m["heated_hours_per_day"] <= 24 and 20 <= m["mean_water_heating"] <= 35
