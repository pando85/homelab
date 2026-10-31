"""Independent checks (oracles) over a finished simulation of the comfort controller.

Each check returns a list of problems (empty when the run behaves as specified), so a failing
test shows every violation at once. The oracles recompute the expected behaviour from the design
(boundary times, lead-time bands, DHW ranking, machine limits), not from the app's internals.
"""

from collections import defaultdict
from datetime import datetime, time, timedelta

from tests.comfort_sim import FORCE_DHW, SENSOR, TZ, Z1_HELPER, day_window, local

APP = "comfort_control"
HALL_ENTITY = "climate.thermostat_hall_kitchen"
LEAD_TABLE_HELPERS = "input_number.comfort_lead_{day}_{band}"
DESIGN_LEADS = {  # design section 6, p75 (minutes)
    ("weekday", "cold"): 675,
    ("weekday", "mid"): 545,
    ("weekday", "mild"): 275,
    ("weekend", "cold"): 530,
    ("weekend", "mid"): 430,
    ("weekend", "mild"): 215,
}
TICK = timedelta(seconds=70)  # a write follows its boundary within one tick


def calls(sim, service, entity=None, caller=APP):
    return [
        (at, data)
        for at, who, name, target, data in sim.ha.services
        if name == service and who == caller and (entity is None or target == entity)
    ]


def state_at(sim, entity, at):
    """The entity state at `at` (the last change at or before it), from the HA history."""
    value = None
    for changed, state in sim.ha.history.get(entity, []):
        if changed > at:
            break
        value = state
    return value


def number_at(sim, entity, at):
    try:
        return float(state_at(sim, entity, at))
    except (TypeError, ValueError):
        return None


def record_at(sim, at):
    minute = at.replace(second=0, microsecond=0)
    index = int((minute - sim.records[0].at).total_seconds() // 60)
    if 0 <= index < len(sim.records):
        return sim.records[index]
    return None


def band(outdoor):
    return "cold" if outdoor < 8 else "mid" if outdoor < 12 else "mild"


def lead_minutes(sim, day):
    """Lead time of the window of `day`: the band of the mean outdoor reading over the hour before
    the previous window end (the app's one-minute samples), the minutes from the helpers."""
    _, previous_end = day_window(day - timedelta(days=1))
    samples = []
    at = previous_end - timedelta(hours=1)
    while at < previous_end:
        value = number_at(sim, SENSOR["outdoor"], at + timedelta(seconds=10))
        if value is not None:
            samples.append(value)
        at += timedelta(minutes=1)
    day_type = "weekend" if day.weekday() >= 5 else "weekday"
    if not samples:
        return None
    chosen = band(sum(samples) / len(samples))
    helper = LEAD_TABLE_HELPERS.format(day=day_type, band=chosen)
    return number_at(sim, helper, previous_end) or DESIGN_LEADS[(day_type, chosen)], chosen


def early_start(sim, day):
    start, _ = day_window(day)
    _, previous_end = day_window(day - timedelta(days=1))
    lead = lead_minutes(sim, day)
    if lead is None:
        return None, None
    early = start.astimezone(TZ) - timedelta(minutes=lead[0])
    early = max(early, previous_end)
    return TZ.normalize(early), lead


def hall_setpoint(sim, at):
    record = record_at(sim, at)
    return None if record is None else record.setpoints[0]


def check_boundaries(sim, days, skip=()):
    """The hall/kitchen day setpoint arrives at the early start (lead time of its band, capped at
    the previous window end) and not before; the night setpoint arrives at the window end."""
    problems = []
    for day in days:
        if day in skip:
            continue
        early, lead = early_start(sim, day)
        if early is None:
            problems.append(f"{day}: no outdoor samples for the band")
            continue
        _, previous_end = day_window(day - timedelta(days=1))
        _, end = day_window(day)
        if early > previous_end and hall_setpoint(sim, early - timedelta(minutes=1)) != 21.5:
            problems.append(f"{day}: hall not at 21.5 just before the early start {early:%m-%d %H:%M} {lead}")
        if hall_setpoint(sim, early + timedelta(minutes=2)) != 24.0:
            problems.append(f"{day}: hall not at 24.0 after the early start {early:%m-%d %H:%M} {lead}")
        if hall_setpoint(sim, end - timedelta(minutes=2)) != 24.0:
            problems.append(f"{day}: hall not at 24.0 before the window end")
        next_early, _ = early_start(sim, day + timedelta(days=1))
        if next_early is not None and next_early > end and hall_setpoint(sim, end + timedelta(minutes=2)) != 21.5:
            problems.append(f"{day}: hall not back to 21.5 after the window end")
    return problems


def boundary_instants(sim, days):
    instants = []
    for day in days:
        early, _ = early_start(sim, day)
        if early is not None:
            instants.append(early)
        instants.append(day_window(day)[1])
    return instants


def check_writes_only_at_boundaries(sim, days, extra=()):
    """Every setpoint write of the app follows a boundary (or an allowed extra instant) by at most
    one tick."""
    allowed = boundary_instants(sim, days) + list(extra)
    first = local(days[0].year, days[0].month, days[0].day)
    last = local(days[-1].year, days[-1].month, days[-1].day) + timedelta(days=1)
    problems = []
    for at, data in calls(sim, "climate/set_temperature"):
        if not first <= at < last:
            continue
        if not any(timedelta(0) <= at - instant < TICK for instant in allowed):
            problems.append(f"setpoint write at {at:%m-%d %H:%M:%S} {data['entity_id']} {data['temperature']}")
    return problems


def check_water_writes(sim):
    """Integers in 20-35, only in direct mode, a change of at least 1 K, at most one per 15 min,
    and only numeric payloads on the bus."""
    problems = []
    writes = calls(sim, "input_number/set_value", Z1_HELPER)
    previous = None
    for at, data in writes:
        value = data["value"]
        if not isinstance(value, int) or isinstance(value, bool) or not 20 <= value <= 35:
            problems.append(f"{at}: water request {value!r} is not an integer in 20-35")
        record = record_at(sim, at)
        if record is None or record.heating_mode != 1 or state_at(sim, SENSOR["heating_mode"], at) != "1":
            problems.append(f"{at}: water request written outside direct mode")
        current = number_at(sim, SENSOR["z1_request"], at)
        if current is None or abs(value - current) < 1:
            problems.append(f"{at}: water request {value} is not 1 K away from {current}")
        if previous is not None and at - previous < timedelta(minutes=15):
            problems.append(f"{at}: water request {at - previous} after the previous one")
        previous = at
    for at, topic, payload in sim.ha.mqtt:
        if topic in ("SetZ1HeatRequestTemperature", "SetDHWTemp") and not payload.lstrip("-").isdigit():
            problems.append(f"{at}: non-numeric {topic} payload {payload!r}")
    return problems


def forced_windows(sim):
    """(on, off) of each force DHW period seen by the plant (minute resolution)."""
    periods, start = [], None
    for record in sim.records:
        if record.force and start is None:
            start = record.at
        elif not record.force and start is not None:
            periods.append((start, record.at))
            start = None
    if start is not None:
        periods.append((start, None))
    return periods


def check_dhw(sim, days, no_window_days=(), down=()):
    """One forced window per day; the force is released by the window end (two hours, plus one
    tick) and never runs past 23:00; the tank target stays at most 50 degC."""
    problems = []
    turned_on = defaultdict(list)
    for at, _ in calls(sim, "switch/turn_on", FORCE_DHW):
        turned_on[at.astimezone(TZ).date()].append(at)
    for day in days:
        expected = 0 if day in no_window_days else 1
        if len(turned_on[day]) != expected:
            problems.append(f"{day}: {len(turned_on[day])} forced DHW windows, expected {expected}")
    for on, off in forced_windows(sim):
        if off is None:
            problems.append(f"force DHW on at the end of the run (since {on})")
            continue
        if any(start <= off and on <= end for start, end in down):
            continue
        if off - on > timedelta(hours=2, minutes=2):
            problems.append(f"force DHW on {on:%m-%d %H:%M} to {off:%m-%d %H:%M}")
        if off.astimezone(TZ).time() > time(23, 2) or off.astimezone(TZ).date() != on.astimezone(TZ).date():
            problems.append(f"force DHW past 23:00: {on:%m-%d %H:%M} to {off:%m-%d %H:%M}")
    if any(record.tank_target > 50 for record in sim.records):
        problems.append("tank target above 50 degC")
    for at, data in calls(sim, "input_number/set_value", "input_number.heishamon_tank_temp"):
        if data["value"] > 50:
            problems.append(f"{at}: tank target write {data['value']}")
    return problems


def dhw_window_regret(sim, day, start, forecast="sensor"):
    """Score of the forced window that starts at `start` minus the best allowed window of the day
    (EUR per kWh of heat), by mean price / expected COP. None when `start` is not allowed.

    forecast "sensor": what the app can know at planning time, the hourly means of the unit sensor
    readings of the last 24 h (persistence; the live reading fills hours without one).
    forecast "true": the true outdoor temperature of the day itself (the physical optimum).
    """
    prices = sim.prices_for(day)
    planned = min(at for at, _, message in sim.ha.notifications if "DHW window" in message and at.astimezone(TZ).date() == day)
    if forecast == "sensor":
        samples = defaultdict(list)
        for at, state in sim.ha.history[SENSOR["outdoor"]]:
            if planned - timedelta(days=1) <= at <= planned and state not in ("unavailable", "unknown"):
                samples[at.astimezone(TZ).hour].append(float(state))
        live = number_at(sim, SENSOR["outdoor"], planned)
        outdoor = {hour: (sum(samples[hour]) / len(samples[hour]) if samples[hour] else live) for hour in range(24)}
    else:
        today = defaultdict(list)
        for record in sim.records:
            if record.at.astimezone(TZ).date() == day:
                today[record.at.astimezone(TZ).hour].append(record.outdoor)
        outdoor = {hour: sum(v) / len(v) for hour, v in today.items()}
    early, _ = early_start(sim, day)
    window_start, _ = day_window(day)
    scores = {}
    for hour in range(0, 22):
        begin = TZ.localize(datetime.combine(day, time(hour)))
        end = begin + timedelta(hours=2)
        if begin < window_start and early is not None and end > early:
            continue
        if planned - begin >= timedelta(minutes=5):
            continue  # already started when the window was planned
        cop = [min(max(2.2 + 0.06 * outdoor[h], 1.5), 4.5) for h in (hour, hour + 1)]
        scores[hour] = sum(prices[h] / c for h, c in zip((hour, hour + 1), cop)) / 2
    hour = start.astimezone(TZ).hour
    return scores[hour] - min(scores.values()) if hour in scores else None


def check_machine_limits(sim):
    """Between two consecutive mode changes of the app: on at least 30 min, off at least 20 min."""
    problems = []
    changes = [(at, new, source) for at, old, new, source in sim.ha.mode_changes if old != new]
    for (at, new, source), (next_at, _, next_source) in zip(changes, changes[1:]):
        if source != APP or next_source != APP:
            continue
        minutes = (next_at - at).total_seconds() / 60
        limit = 30 if new == "Heat+DHW" else 20
        if minutes < limit:
            problems.append(f"{new} for {minutes:.0f} min from {at:%m-%d %H:%M}")
    return problems


def longest_dhw_only(sim, start, end):
    """Longest stretch (minutes) of the pump in DHW only (no space heating allowed) in [start, end)."""
    longest = current = 0
    for record in sim.records:
        if not start <= record.at < end:
            continue
        current = current + 1 if record.mode != "Heat+DHW" else 0
        longest = max(longest, current)
    return longest
