# Heat pump comfort controller

The AppDaemon app `comfort_control` (`apps/home-assistant/appdaemon/apps/comfort_control.py`) runs
the Aquarea heat pump against a comfort schedule with the fewest kWh. It ignores price for space
heating, and it keeps hot water price-optimised. It replaces the legacy cheapest-hours
`climate_control` and the DHW price forcing `dhw_control`, which are archived in
`apps/home-assistant/appdaemon/archive/legacy-price-control/` and not deployed.

The design is `docs/planning/comfort-controller-design.md` and ADR-0006 (with its 2026-10-31
amendment) in `pando85/home-energy-optimizer`. Unit facts, commands and safety notes are in
[heat pump water temperature](heat-pump-water-temperature.md).

**Rollout gate:** merge only once the house battery is working and the go-live checklist below is
done.

## What it does

Everything runs on a one-minute tick.

### Setpoints and schedule

| Room | Day window | Otherwise |
|---|---|---|
| Hall/kitchen (`climate.thermostat_hall_kitchen`) | day minimum + margin = **24.0** | floor + margin = **21.5** |
| Bedroom, Office A, Office T | 21.5 | 21.5 |

- The day window starts at 12:00 on weekdays and 09:00 at weekends and ends at 22:00 (helpers).
  Public holidays are weekdays.
- Setpoints are written **only at boundaries**: the early start (the day setpoint) and the window
  end (the night setpoint), plus away on and off and enabling the controller. The window start
  itself writes nothing: optimum start is that boundary moved earlier.
- **A manual change holds until the next boundary.** A change made before the early start is
  overwritten by it; a change below the 21 degC floor is not fought.
- The last applied boundary is stored in `sensor.comfort_control_status`, so an AppDaemon restart
  does not overwrite a manual change. A Home Assistant restart loses that sensor, and the next tick
  writes the current boundary again.

### Optimum start

The hall/kitchen day setpoint is written at **window start minus the lead time**. The thermostat
opens its valve, the run logic starts the heat pump, and the room reaches the minimum at the window
start.

Lead times (minutes, editable helpers). Source: B0 warm-up analysis, 2026-10-31, p75. They are the
p75 time to bring the hall/kitchen from 23.5 degC at 22:00 back to 23.5 degC at 26 degC water, so
they are conservative once the curve gives warmer water.

| Outdoor band | Weekday (window 12:00) | Weekend (window 09:00) |
|---|---|---|
| below 8 degC | 675 (early start 00:45) | 530 (00:10) |
| 8 to 12 degC | 545 | 430 |
| 12 degC and above | 275 | 215 |

- The band edges (8 and 12 degC) are code constants (`LEAD_BAND_EDGES`).
- **Band input:** the offset-corrected unit outdoor sensor, **averaged over the hour before the
  previous window end** (21:00 to 22:00) and frozen for the next window, so the early start does not
  move during the night. After an AppDaemon restart past 22:00 the live reading is used and frozen.
  Without any outdoor reading the longest lead time of the day type is used.
- The early start may fall before midnight (for example after a helper edit). The lead time is
  **capped at the time since the previous window end**: if it is longer than the night, the
  hall/kitchen keeps its day setpoint overnight.
- Lead times count elapsed minutes, so they are right on DST days.

### Run logic

The pump's zone sensor is the water temperature, so the thermostats cannot call for heat; the app
switches the operating mode through `input_select.heishamon_heatmode` (the C0 command path):

- **On** (`Heat+DHW`): any thermostat room is below its current target minus the hysteresis helper.
- **Off** (`DHW`): every room is at or above its target.
- Compressor **minimum on 30 min, minimum off 20 min** (code). Thermostats that are `off` or report
  no temperature are ignored. Any other operating mode (for example `Cool+DHW` set by hand) is left
  alone.
- **Starts-per-day alarm:** a notification when `sensor.aquarea_compressor_start_stop_counter`
  rises by more than 12 in a local day. It is not a control action.

### Away

`input_boolean.comfort_away` on sets every room to floor + margin (21.5). While away, boundaries
pass without writing. Off writes the schedule that applies at that moment, including the day
setpoint if the early start has passed.

### Water temperature

- Software curve: outdoor -5 degC -> 32 degC water, 15 degC -> 25 degC, linear between the points,
  clamped outside. The unit's own curve points are untouched.
- Optional **raise during optimum start** (helper, default 0 K): from the early-start write until
  the hall/kitchen reaches its setpoint.
- **Integer, clamped to 20 to 35 degC.** Written only when `sensor.aquarea_heating_mode` is `1`
  (direct); in curve mode the same command is a shift in K. Nothing is written when the heating
  mode, the outdoor temperature, the curve points or the current request are unknown or garbage.
- Written only on a change of **at least 1 K** of the unrounded target against the request the unit
  reports, and **at most once per 15 min** (the unit probably persists settings). Every write is
  logged with time, old and new request, outdoor input and reason.
- **Outdoor source:** `sensor.aquarea_outside_ambient_temperature` plus the
  `input_number.comfort_outdoor_offset` helper (default 0 K). The unit sensor reads about 1.6 to
  2.5 K low while the compressor runs; a positive offset corrects that. A different entity is a
  change of `sensors.outdoor` in `apps.yaml`.

**Command path:** the app sets `input_number.heishamon_heatshift_z1`; the existing "Set Z1 Heat
Temperature" automation publishes it to `SetZ1HeatRequestTemperature` with `retain: false`. A direct
`mqtt.publish` from AppDaemon would cause the echo described in the water temperature guide: the
unit reports the new request, "Set Z1 Heat selector" copies it into the helper, and the command
automation publishes the same value a second time (an extra write to the unit's memory). Through
the helper the command automation mutes the selector while the unit confirms, and a later
confirmation of the same value does not change the helper, so each change is written once. The
helper range and an `is_number` condition on the automation (added with this app, also on "Set tank
target temperature") keep empty or garbage payloads, which the fork turns into 0 degC, off the bus.

### Hot water (DHW)

- One window per day: the cheapest **2 h** by mean `price / expected COP`, from the PVPC prices of
  `sensor.esios_pvpc` (the same sensor the legacy apps used). It is planned just after midnight and
  retried every 10 min until the price curve is complete; a restart plans from the remaining hours.
- **Expected COP** is a linear proxy: `2.2 + 0.06 x outdoor`, bounded to 1.5 to 4.5 (about 1.9 at
  -5 degC, 2.6 at 7 degC, 3.4 at 20 degC). The hourly outdoor temperature is yesterday's hourly mean
  of the outdoor sensor plus the offset (persistence forecast). It only ranks hours, so warmer hours
  win at equal price.
- Windows that overlap today's optimum-start warm-up (early start to window start) are excluded.
- In the window the app sets the tank target to `input_number.comfort_dhw_target`, **capped at
  50 degC in the code**, through `input_number.heishamon_tank_temp`, waits for the unit to report
  it, and turns on `switch.aquarea_force_dhw_mode` (the mechanism of the legacy `dhw_control`). At
  the window end it turns force DHW off if it is still on.
- It does not force DHW while `binary_sensor.aquarea_force_heater_state` is on or the reported tank
  target is above 50 degC, and it never sends sterilization or heater commands.
- **Single DHW writer:** `dhw_control` is archived, so this app is the only writer of force DHW and
  of the tank target. The unit still reheats the tank on its own below target minus its delta.

### Boost

Not implemented (phase 2, design section 8). See `BOOST_ENABLED` and the TODO in the code.

## Helpers

Defined in `apps/home-assistant/config/integrations/appdaemon_comfort.yaml`. HA creates new
helpers at their minimum and `initial:` would reset edits at every restart, so the app writes the
defaults once while `input_boolean.comfort_helpers_seeded` is off and then turns it on. **Turning it
off resets every helper to the defaults below.**

| Helper | Default | Meaning |
|---|---|---|
| `input_boolean.comfort_control_enable` | off at creation | Controller on. Off restores `Heat+DHW` once and leaves the mode manual |
| `input_boolean.comfort_away` | off | Away mode |
| `input_number.comfort_day_minimum` | 23.5 degC | Hall/kitchen minimum in the day window |
| `input_number.comfort_night_floor` | 21.0 degC | Every room, always |
| `input_number.comfort_margin` | 0.5 K | Setpoint = minimum + margin |
| `input_datetime.comfort_weekday_start` / `_weekend_start` / `_window_end` | 12:00 / 09:00 / 22:00 | Day window |
| `input_number.comfort_hysteresis` | 0.3 K | Heating on below target minus this |
| `input_number.comfort_lead_{weekday,weekend}_{cold,mid,mild}` | see the lead-time table | Optimum start |
| `input_number.comfort_curve_{cold,mild}_{outdoor,water}` | -5 / 32, 15 / 25 degC | Curve points |
| `input_number.comfort_early_start_raise` | 0 K | Water raise during optimum start |
| `input_number.comfort_outdoor_offset` | 0.0 K | Added to the unit outdoor sensor |
| `input_number.comfort_dhw_target` | 49 degC | Tank target (capped at 50 in the code) |

Machine limits stay in the code: minimum on/off, water clamp 20 to 35 degC, 1 K / 15 min water write
rule, tank cap 50 degC, starts alarm 12, lead-time band edges. `actuate` in `apps.yaml` turns any area
(mode, setpoints, water temperature, DHW) into a dry run that only logs.

## Logs, status and suggestions

- AppDaemon log lines (Loki): `Setpoint <room>: old -> new (<boundary>)`, `Manual override <room> at
  <time>: old -> new`, `Water request write at <time>: old -> new degC (outdoor ..., curve ...)`,
  `Heat mode -> <mode> (<reason>)`, `Lead-time band input for <day>`, `Force DHW until ...`.
- `sensor.comfort_control_status`: state `heating`, `idle`, `away` or `disabled`; attributes with
  the phase, the next early starts, the frozen band inputs, the DHW window and the overrides of the
  last 14 days.
- **Suggestions:** three manual overrides of the same room, in the same direction, within an hour
  of the same time of day in 14 days raise a persistent notification ("Comfort control suggestion")
  with the overrides and their mean. The schedule is never changed by the app. The override history
  lives in the status sensor, so it survives AppDaemon restarts but not Home Assistant restarts; the
  log keeps every override.
- Telegram notifications: the DHW window, helper seeding, starts alarm, blocked DHW, thermostats
  running their device program (`auto`), sterilization enabled, disable and errors.

## Failure mode

- **AppDaemon down:** the unit keeps the last water temperature request (it persists it). The
  watchdog automation `AppDaemon comfort watchdog` selects `Heat+DHW` when
  `sensor.last_appdaemon_connection` is older than 15 minutes while the controller is enabled and
  the pump reports `DHW` (at most every 30 minutes). The thermostats then protect comfort through the
  floor valves; the worst case is extra kWh, never no heating. Force DHW is not touched.
- AppDaemon shutdown or reload changes nothing. Disabling the controller restores `Heat+DHW` once.
- Expected unit state: `Heating_Mode` 1 (direct), operating mode `Heat+DHW` or `DHW`.
- **Verify after stopping AppDaemon:** wait 15 min; `sensor.aquarea_mode` should read `Heat+DHW`,
  `sensor.aquarea_z1_heat_request_temperature` should keep the last value, and the notification
  "AppDaemon has not reported for 15 minutes" should arrive if the pump was in `DHW`.

## Rollback

`git revert` the comfort-controller commits (or the merge commit) in this repository and let ArgoCD
sync. That restores `climate_control`, `dhw_control`, their helpers and the legacy watchdog.
There is no runtime strategy switch.

## Go-live checklist

1. The house battery is working.
2. The battery grid-target cap below the 5.0 kW import limit is set (optimizer work-list item 8).
3. Lead times: the B0 values are the defaults; check them in the helpers after seeding.
4. Outdoor source chosen: the unit sensor with `input_number.comfort_outdoor_offset` set by the
   owner (default 0 K).
5. The thermostats' device programs are off (hvac mode `heat`, not `auto`); the app notifies at
   start for any thermostat in `auto`.
6. Legionella decision recorded: sterilization stays off, or a controlled cycle outside the cheap
   hours is scheduled by hand (the app never starts it).
7. `sensor.aquarea_heating_mode` reads `1`.
8. After ArgoCD sync: check the seeded helpers, then turn on `input_boolean.comfort_control_enable`.
   Until then the pump keeps whatever mode the legacy app left (the app notifies if that is `DHW`).
