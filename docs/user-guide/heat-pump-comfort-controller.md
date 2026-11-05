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

Every room has its own two helpers, `input_number.comfort_floor_<room>` (its always-minimum) and
`input_number.comfort_day_minimum_<room>` (its minimum in the shared day window), for the rooms
`hall_kitchen`, `bedroom`, `office_a` and `office_t` (the thermostats
`climate.thermostat_<room>`). The window times and the margin are shared. The table shows the
defaults.

| Room | Day window (from the early start) | Otherwise |
|---|---|---|
| Hall/kitchen | max(day minimum 23.5, floor 21.0) + margin = **24.0** | floor + margin = **21.5** |
| Bedroom, Office A, Office T | max(day minimum 21.0, floor 21.0) + margin = 21.5 | floor + margin = 21.5 |

- A room's target is its floor + margin outside the window and **max(day minimum, floor) + margin**
  inside it, so a day minimum below the floor never lowers the floor.
- Comfort criteria per room: see the optimizer preregistration amendment (pending).

- The day window starts at 12:00 on weekdays and 09:00 at weekends and ends at 22:00 (helpers).
  Public holidays are weekdays.
- Setpoints are written **only at boundaries**: the early start (the day setpoint) and the window
  end (the night setpoint), plus away on and off and enabling the controller. The window start
  itself writes nothing: optimum start is that boundary moved earlier.
- **A manual change holds until the next boundary.** A change made before the early start is
  overwritten by it; a change below the room's floor is not fought.
- A boundary counts as applied only once every written thermostat reports its new target; an
  unconfirmed write (thermostat offline, lost cloud call) is repeated every 5 minutes until it is
  confirmed or a manual change of that room is seen.
- The last applied boundary is stored in `sensor.comfort_control_status`, so an AppDaemon restart
  does not overwrite a manual change. A Home Assistant restart loses that sensor, and the next tick
  writes the current boundary again.

### Optimum start

The day setpoints are written at **window start minus the lead time**, one early start for every
room. The thermostat opens its valve, the run logic starts the heat pump, and the hall/kitchen
reaches its minimum at the window start.

**The lead times are calibrated for the hall/kitchen only.** Rooms other than the hall/kitchen with a
day minimum above their floor use the same lead time (and the same early start), so they may arrive
early or late until their warm-up is measured. Rooms whose day minimum is at or below their floor
write the same setpoint as at night and are not affected.

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
  rooms keep their day setpoints overnight.
- Lead times count elapsed minutes, so they are right on DST days.

### Run logic

The pump's zone sensor is the water temperature, so the thermostats cannot call for heat; the app
switches the operating mode through `input_select.heishamon_heatmode` (the C0 command path):

- **On** (`Heat+DHW`): any thermostat room is below its current target minus the hysteresis helper.
- **Off** (`DHW`): every room is at or above its target, and every thermostat reports a
  temperature and a setpoint.
- A thermostat that reports nothing (`unavailable`, `unknown`, garbage) counts as demand: the pump
  is never switched off while one is missing, and is switched on once the minimum off time has
  passed (Tuya cloud thermostats do drop out; their valves keep working locally). A thermostat
  switched `off` by hand is ignored (its valve stays closed).
- Compressor **minimum on 30 min, minimum off 20 min** (code). Any other operating mode (for
  example `Cool+DHW` set by hand) is left alone.
- **Starts-per-day alarm:** a notification when `sensor.aquarea_compressor_start_stop_counter`
  rises by more than 12 in a local day. It is not a control action.

### Away

`input_boolean.comfort_away` on sets every room to its own floor + margin (21.5 with the defaults). While away, boundaries
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
- **Water dry run:** `input_boolean.comfort_water_dry_run` is seeded **on**. While it is on (or
  unknown) the app only logs the water requests it would write. Turn it off after the first-write
  test of the go-live checklist. Re-seeding the helpers turns it on again.
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
confirmation of the same value does not change the helper, so each change is written once.

**HA guards** (`heishamon.yaml`), whoever writes the helper: "Set Z1 Heat Temperature" publishes
only a number that fits the reported heating mode (20 to 35 degC in direct mode `1`, a -5 to 5 K
shift in curve mode `0`, nothing for an unknown mode); "Set tank target temperature" publishes only
40 to 50 degC (above about 51 degC the unit needs the 3 kW heater). Empty or garbage payloads, which
the fork turns into 0 degC, never reach the bus.

### Hot water (DHW)

- One window per day: the cheapest **2 h** by mean `price / expected COP`, from the PVPC prices of
  `sensor.esios_pvpc` (the same sensor the legacy apps used). The window ends by **23:00**, so it
  never runs into the next day. It is planned only once the sensor carries **today's** prices (the
  local date of its `last_updated`; the integration rolls over on the hour) and retried every
  10 min until the price curve is complete (23 hours on the spring-forward day, 25 with the
  `price_02h_d` hour on the fall-back day); a notification follows if it is still missing after
  01:00. The plan (and the last finished day) is stored in `input_text.comfort_dhw_plan`, which HA
  restores after a restart, and in the status sensor: an AppDaemon or HA restart inside the window
  keeps it, and a restart after it does not plan a second window. Without a stored plan (helper
  lost) a restart plans from the remaining hours, which can mean a second window that day; that is
  harmless (the tank stays at most 50 degC and the heater never runs).
- **Expected COP** is a linear proxy: `2.2 + 0.06 x outdoor`, bounded to 1.5 to 4.5 (about 1.9 at
  -5 degC, 2.6 at 7 degC, 3.4 at 20 degC). The hourly outdoor temperature is yesterday's hourly mean
  of the outdoor sensor plus the offset (persistence forecast). It only ranks hours, so warmer hours
  win at equal price.
- Windows that overlap today's optimum-start warm-up (early start to window start) are excluded.
- **Force DHW is always released** (only with `actuate.dhw` true), checked on every tick, enabled
  or disabled, outside an active forced window:
  - a force the app set itself is turned off at its window end, at the day rollover or on disable
    (and again on the next tick if the turn-off was lost);
  - a force switched on by hand is held like a manual setpoint, for **at most 2 h** from when the
    app first sees it, then turned off and logged (`Force DHW off (manual force DHW held since ...)`);
  - at startup the owner is unknown: a force DHW that is on without an active planned window is
    turned off and logged.
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
| `input_boolean.comfort_water_dry_run` | **on** | Water requests only logged (go-live checklist) |
| `input_text.comfort_dhw_plan` | written by the app | Today's DHW plan, kept across HA restarts; do not edit |
| `input_number.comfort_floor_<room>` | 21.0 degC (all four rooms) | Always-minimum of the room, range 18 to 23 |
| `input_number.comfort_day_minimum_<room>` | `hall_kitchen` 23.5; `bedroom`, `office_a`, `office_t` 21.0 degC | Minimum of the room in the day window, range 18 to 26; below the floor it has no effect |
| `input_number.comfort_margin` | 0.5 K | Setpoint = minimum + margin (every room) |
| `input_datetime.comfort_weekday_start` / `_weekend_start` / `_window_end` | 12:00 / 09:00 / 22:00 | Day window |
| `input_number.comfort_hysteresis` | 0.3 K | Heating on below target minus this |
| `input_number.comfort_lead_{weekday,weekend}_{cold,mid,mild}` | see the lead-time table | Optimum start |
| `input_number.comfort_curve_{cold,mild}_{outdoor,water}` | -5 / 32, 15 / 25 degC | Curve points |
| `input_number.comfort_early_start_raise` | 0 K | Water raise during optimum start |
| `input_number.comfort_outdoor_offset` | 0.0 K | Added to the unit outdoor sensor |
| `input_number.comfort_dhw_target` | 49 degC | Tank target (capped at 50 in the code) |

Rooms are `hall_kitchen`, `bedroom`, `office_a` and `office_t`. **The range of every numeric helper in
the code (`HELPER_RANGES`) is identical to the `min` and `max` of its `input_number`** (a test pins this
for each helper), so HA accepts exactly what the app accepts. A value outside the range (a hand-edited
state, `unknown`, text) is garbage: the app logs a warning and uses that helper's own default.

Machine limits stay in the code: minimum on/off, water clamp 20 to 35 degC, 1 K / 15 min water write
rule, tank cap 50 degC, starts alarm 12, lead-time band edges. `actuate` in `apps.yaml` turns any area
(mode, setpoints, water temperature, DHW) into a dry run that only logs.

## Comfort dashboard

`apps/home-assistant/lovelace/dashboard-comfort.json` is the definition of a Lovelace dashboard for
the controller. **It is a definition only: nothing creates it in Home Assistant** (dashboards live in
HA storage, not in git; see [Home Assistant dashboards](../troubleshooting/home-assistant-dashboards.md)).
It sits in `lovelace/` and not in `dashboards/` because the chart turns every file of `dashboards/`
into a Grafana dashboard.

The file has two keys: `registration` (the `lovelace/dashboards/create` fields: `url_path`
`dashboard-comfort`, title "Comfort", icon `mdi:home-thermometer`, shown in the sidebar, not admin
only) and `config` (the `lovelace/config` payload, a `sections` view that works on a phone: one
column per section, tiles with sliders, standard cards only plus the installed `custom:expander-card`).

- **Top:** `input_boolean.comfort_control_enable`, `input_boolean.comfort_away`, the status
  (`sensor.comfort_control_status` state, phase, hot water window) and the next early starts.
- **One section per room:** the thermostat card of `climate.thermostat_<room>` and two sliders, the
  floor and the day minimum of that room.
- **Shared:** the day window times (`input_datetime.comfort_weekday_start`, `_weekend_start`,
  `_window_end`) and `input_number.comfort_margin`.
- **Climate optimizer** (second section): `input_select.climate_optimizer_mode`, the plan status and a
  markdown card with requested and effective mode, the lock, plan age and expiry, plan against rules
  for now and the agreement of today (see [Climate optimizer plan](#climate-optimizer-plan-adr-0008-shadow)).
  The select is defined by the battery-optimizer/climate package, not by `appdaemon_comfort.yaml`.
- **Advanced** (collapsed): the water dry run, the hysteresis, the hot water target and
  `input_boolean.comfort_helpers_seeded`, labelled "turn OFF to reset EVERY helper to its default"
  and asking for confirmation (the reset also turns the water dry run back on).

Slider edits take effect at the next boundary (early start or window end), like a manual thermostat
change, not at once.

To create it (go-live step 8): follow the procedure of the dashboards guide. Back up
`/config/.storage/lovelace*` first, get an admin token with the login flow of
[API access and HACS deploy](../troubleshooting/home-assistant-api-hacs-deploy.md), check every
entity of the file with `get_states` and the markdown templates with `render_template`, create the
dashboard with `lovelace/dashboards/create` (`registration`) and save the config with
`lovelace/config/save` (`url_path: dashboard-comfort`, `config`), read it back, and **revoke the
token afterwards**. Rollback is `lovelace/dashboards/delete`.

## Climate optimizer plan (ADR-0008, shadow)

The battery-optimizer service plans space heating hourly (the climate MPC) and publishes it as an
advisory **ClimatePlan v1**: per 15-minute slot, heating on or off and the water target. ADR-0008 of
`pando85/home-energy-optimizer` (status **Proposed**) lets this controller read it. **Today the plan
only feeds telemetry: nothing is written from it, and it cannot be made to write by any setting in
Home Assistant.** The rules above keep driving the heat pump.

**Where the plan comes from.** The app does an HTTP `GET` of the optimizer's own in-cluster service
(`climate_plan.url` in `apps.yaml`: `/climate/plan`; the service is ClusterIP, read only). Neither the
`home-assistant` nor the `battery-optimizer` namespace has a NetworkPolicy and no Cilium policy
applies, so the AppDaemon pod reaches it directly, like Home Assistant already does for the battery plan.
This avoids a REST sensor in between and the 16 KB attribute limit of a state object. The timeout is
5 s, the body is capped at 256 KB, nothing is ever sent to the optimizer, and the optimizer has no
token that could write climate entities.

### Modes

The owner's control is `input_select.climate_optimizer_mode` (options `off`, `shadow`, `live`). The
app only **reads** it; a missing, `unavailable`, `unknown` or unrecognised state is `off`.

| Select | Controller | Plan |
|---|---|---|
| `off` (**Rules**) | its own rules, as always | not fetched |
| `shadow` (**Optimizer shadow**) | its own rules | fetched and compared, never applied |
| `live` (**Optimizer live**) | **shadow while the lock is closed** | as shadow; applied only with the lock open |

### The hard lock

`optimizer_live_allowed: false` in `apps.yaml` (and false when the key is missing: only a real
boolean `true` opens it) is the lock. It is a value in git, not a helper, a dashboard switch or a
service call, so a mis-tap cannot open it. While it is false a `live` request behaves exactly like
`shadow` and the status says **"Live requested, locked in git: behaving as shadow"**. The simulation
tests run whole days with the lock closed and prove that the controller's calls to Home Assistant are
identical, call for call, to a run with no plan at all, in `shadow`, in `live`, while the owner flips
the select through every mode, and with the optimizer down or answering garbage.

### What shadow records

`sensor.comfort_climate_plan_status` (written by the app like the other status sensors; state `off`,
`shadow`, `live_locked` or `live`, plus `status_text`). Attributes: `requested_mode`,
`effective_mode`, `live_locked`, `live_allowed_in_git`, `plan_controls`, `plan_id`,
`plan_generated_at`, `plan_valid_until`, `plan_age_min`, `plan_expires_in_min`, `plan_usable`,
`fallback_reason`, `control_blocked`, `slot_start`, what the plan says now (`plan_heating_now`,
`plan_water_c_now`), what the rules say now (`rules_heating_now`, `rules_water_c_now`),
`plan_effect_heating_now` (what the live path would end up commanding), `floor_forced`,
`agree_heating`, `agree_water`, `agree`, and per local day `today_samples` (one per minute),
`today_agree`, `today_agree_pct`, `today_heating_disagree`, `today_water_disagree`,
`today_would_differ` (minutes in which a live plan would have acted differently from the rules),
`today_unusable`, `today_skipped`, `today_plans`. The counters are restored after an AppDaemon restart
and reset after a Home Assistant restart (the sensor is not restored). The log shows
`Climate plan <id> received`, and one `Climate plan [mode] ...` line whenever the plan or the rules
change what they say. "Rules heating" is the run decision of the rules this tick (on, off, or
hold); "plan heating" is the slot's `heating`, the permitted-heating lever.

### Validity and fallback

The plan is polled every 2 minutes (1 minute after a failure) and judged again **at every tick**.
It is absent, and the rules decide, when: the request fails or times out, is a `404` (the status shows
the optimizer's own state, for example `not_served:mode_off`) or any other status, the body is not
JSON or is too large, a field the controller needs is missing or malformed (schema), `version` is not
1, `advisory` is not `true`, `status` is not `planned`, `now >= valid_until`, no slot covers now
(found by time), or the plan id was already applied (below). Absent is never "off" and never "keep the
last plan": a failed fetch drops the plan at once. `fallback_reason` and the log say which.

### The live path (built, unreachable while locked)

Only with the select on `live` **and** the lock open, and only for a valid plan:

- the slot's `heating` **gates the run decision**: `0` keeps heating off (and stops it after the
  compressor's minimum on time) while no comfort floor is at stake; `1` changes nothing (the plan
  permits, the thermostats and the rules decide);
- the slot's `water_c` **replaces the curve target** in a heating slot, through the same write
  rules; a slot without `water_c` uses the curve;
- **comfort floors win:** a room below its own comfort minimum minus the hysteresis (its floor, and
  inside the day window the higher of floor and day minimum; the floor only while away) forces
  heating whatever the plan says, as does a room that reports nothing;
- all fixed limits stay: water 20 to 35 degC, at least 1 K and at most once per 15 min (and the water
  dry-run helper), compressor minimum on 30 / off 20 min, tank at most 50 degC, direct mode, **no
  heater, no boost, no sterilisation**; no other entity is written;
- **each plan id is applied once:** a replan supersedes the previous plan entirely; a superseded or
  revoked id is not applied again (`control_blocked: plan_id_already_applied`, the rules decide);
- changing the select away from `live` revokes plan control at once (the state listener), not at the
  next tick, and the plan in force at that moment is not re-applied if `live` is chosen again.

### Going live later

Not now. ADR-0008 has to be **accepted** first (a new preregistered gate passed, the owner's explicit
go, a supervised first day with the rules one switch away). Only then, a **reviewed commit** changes
`optimizer_live_allowed: false` to `true` in `apps.yaml`, and the owner selects `live`. Rollback is
the select set to `shadow` or `off` (immediate), or a revert of that commit. The optimizer mirrors the
lock on its side (it never reports an effective `live` in its current code), so a go-live is two
reviewed changes, one in each repository.

## Logs, status and suggestions

- AppDaemon log lines (Loki): `Setpoint <room>: old -> new (<boundary>)`, `Manual override <room> at
  <time>: old -> new`, `Water request write at <time>: old -> new degC (outdoor ..., curve ...)`,
  `Heat mode -> <mode> (<reason>)`, `Lead-time band input for <day>`, `Force DHW until ...`.
- `sensor.comfort_climate_plan_status`: the optimizer plan against the rules, see the climate
  optimizer section above.
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

- **Heartbeat:** the app writes `sensor.comfort_control_heartbeat` (a timestamp) at the end of
  every control tick whose run logic worked. It stops when AppDaemon is down, the app failed in
  `initialize`, a tick hangs or the run step keeps failing, and while the helpers are not seeded.
- **Controller silent:** the unit keeps the last water temperature request (it persists it). The
  watchdog automation `AppDaemon comfort watchdog` selects `Heat+DHW` when the heartbeat is missing
  or older than 15 minutes while the controller is enabled and the pump reports `DHW` (at most every
  30 minutes). The thermostats then protect comfort through the
  floor valves; the worst case is extra kWh, never no heating. Force DHW is not touched.
- AppDaemon shutdown or reload changes nothing. Disabling the controller restores `Heat+DHW` once
  and releases force DHW.
- **Helpers lost** (re-created at their HA minimum, `comfort_helpers_seeded` off): every tick retries
  the seeding and skips all control until it works (no heartbeat meanwhile, so the watchdog keeps
  heating on). This is the protection against a helper at its minimum, and it is the only one:
  the minimum of the floor (18 degC) and of the day minimum (18 degC) are valid values, so the range
  check no longer catches them. When HA loses its stored helper states (restore state lost, entities
  renamed or re-created), every helper of the package comes back together: the numbers at their
  minimum and `comfort_helpers_seeded` off, which is what the app acts on. (The simulation resets all
  helpers this way and checks that every room is re-seeded at once and never held at 18.5 degC.)
  - **Residual risk:** a single numeric helper re-created alone while `comfort_helpers_seeded` stays on
    (for example a helper deleted and re-added in the UI, or a helper added by a later change of
    `appdaemon_comfort.yaml` on an already-seeded system) is used at its minimum: a floor of 18 gives a
    setpoint of 18.5 degC. After any change that adds or re-creates helpers, turn
    `input_boolean.comfort_helpers_seeded` off once (it re-seeds every helper, see Helpers). The
    same applies to a system that ever ran the earlier helper set (`comfort_night_floor`,
    `comfort_day_minimum`): turn it off after the sync.
- Expected unit state: `Heating_Mode` 1 (direct), operating mode `Heat+DHW` or `DHW`.
- **Verify after stopping AppDaemon:** wait 15 min; `sensor.aquarea_mode` should read `Heat+DHW`,
  `sensor.aquarea_z1_heat_request_temperature` should keep the last value, and the notification
  "Comfort control has not run for 15 minutes" should arrive if the pump was in `DHW`.

## Rollback

`git revert` the comfort-controller commits (or the merge commit) in this repository and let ArgoCD
sync. That restores `climate_control`, `dhw_control`, their helpers and the legacy watchdog.
There is no runtime strategy switch.

## Go-live checklist

**Before merging** (between the merge and the enable nothing switches heating on):

1. Turn off `input_boolean.appdaemon_climate_enable`: the legacy disable handler restores
   `Heat+DHW` and hands the mode over to manual. Confirm `sensor.aquarea_mode` reads `Heat+DHW`.
2. Check the HA-storage automations, schedules or scheduler cards that call `script.set_heat_mode`,
   and any dashboard that uses the removed helpers (`appdaemon_climate_*`, `appdaemon_dhw_enable`).
   On 2026-11-04 the scheduler card held nine schedules that write the heat mode, the Z1 water request
   or the thermostat setpoints (`switch.schedule_set_heat_mode_dhw`, `switch.schedule_644a1f`,
   `switch.schedule_1e73e5`, `switch.schedule_a6da7c`, `switch.schedule_eb37d6`,
   `switch.schedule_55636c`, `switch.schedule_9249a5` and the two that toggle the legacy enable
   helper); all were off. They must stay off: a second writer would fight the controller.

**Known behaviour:**

- Re-seeding the helpers (turning `input_boolean.comfort_helpers_seeded` off, or HA re-creating
  them) turns `input_boolean.comfort_water_dry_run` back on. This is intended: the safe default.
  The unit keeps its last water request until the owner turns the dry run off again.
- A force DHW switched on by hand is held for at most 2 h (see Hot water).

**Go-live:**

1. The house battery is working.
2. The battery grid-target cap below the 5.0 kW import limit is set (optimizer work-list item 8).
3. Lead times: the B0 values are the defaults; check them in the helpers after seeding. Check the
   per-room floors and day minimums too (defaults: floor 21.0 everywhere, day minimum 23.5 in the
   hall/kitchen and 21.0 elsewhere) and set the owner's values per room. The lead times are the
   hall/kitchen's: a room other than the hall/kitchen with a day minimum above its floor follows
   the same early start.
4. Outdoor source chosen: the unit sensor with `input_number.comfort_outdoor_offset` set by the
   owner (default 0 K).
5. The thermostats' device programs are off (hvac mode `heat`, not `auto`); the app notifies at
   start for any thermostat in `auto`.
6. Legionella decision recorded: sterilization stays off, or a controlled cycle outside the cheap
   hours is scheduled by hand (the app never starts it).
7. `sensor.aquarea_heating_mode` reads `1`.
8. After ArgoCD sync: check the seeded helpers (if the earlier helper set `comfort_night_floor` /
   `comfort_day_minimum` ever ran, turn `input_boolean.comfort_helpers_seeded` off once, see Failure
   mode), then **create the Comfort dashboard in HA from
   `apps/home-assistant/lovelace/dashboard-comfort.json`**: back up Lovelace first, create it via the
   admin login-flow token, revoke that token afterwards (see "Comfort dashboard" below). Not done by
   this PR.
9. Turn on `input_boolean.comfort_control_enable`.
   Until then the pump keeps whatever mode the legacy app left (the app notifies if that is `DHW`).
10. **First water write, by hand, with the dry run still on** (see
   [heat pump water temperature](heat-pump-water-temperature.md)): set
   `input_number.heishamon_heatshift_z1` to the value the unit reports, then to that value + 1 K, and
   watch `Z1_Heat_Request_Temp` and `Z1_Heat_Curve_Target_High_Temp` follow. Only then turn
   `input_boolean.comfort_water_dry_run` off.
11. In `apps/battery-optimizer/files/service.yaml` set
    `climate_shadow.go_live: "YYYY-MM-DD"` (local date of the first controlled day) and merge, so the
    new-regime window of the preregistration starts 7 days later. The Grafana dashboard
    `climate-heat-pump-controller` already reads the comfort helpers and the comfort watchdog.

## Test battery

`apps/home-assistant/appdaemon/apps/tests/` holds, besides the unit tests, a closed-loop simulation
of the app (`comfort_sim.py`): a two-state room/slab model per thermostat room calibrated to the B0
rates (night cooling 0.04 to 0.12 K/h, warming 0.08 to 0.19 K/h at 26 degC water, 0.5 to 0.75 h lag,
150 h time constant), valves that close at each setpoint, an Aquarea-like unit (operating mode,
Heating_Mode, Z1 request, tank with its own reheat, force DHW, start counter), and Home Assistant with
the real heishamon guard templates, the selector echoes, the real watchdog conditions and the PVPC
sensor with its DST keys. The real app runs through its AppDaemon interface on a fake clock with a
one-minute plant step. Weather and prices are synthetic.

```bash
cd apps/home-assistant/appdaemon/apps
pytest tests/                     # everything, as CI runs it (about 3 minutes)
pytest tests/ -m "not long"       # unit tests only (seconds)
pytest tests/test_comfort_parity.py -s   # prints the parity table against the legacy apps
```

The `long` scenarios (14 to 21 simulated days each: typical, cold, mild, a cold front, overrides,
away, both DST changes, a capped lead time, a unit that never clears force DHW, four fault runs and
a parity failure run) check:

- **Comfort:** hall/kitchen at least 23.5 degC in at least 95 % of the day-window minutes after the
  first 2 days, every room at least 21 degC at least 97 % of the time (each scenario states whether
  the plant can hold the setpoint at all). A per-room scenario uses a different floor and day minimum in
  every room (including a day minimum below the floor) and checks each room against its own values,
  plus away, a garbage room helper and a floor change;
- **per-room parity:** with the default helper values every room's setpoint equals what the two shared
  helpers gave before (unit test of the formulas and a simulation check of all four rooms);
- **boundaries:** setpoints written only at the early start, the window end and away; the early
  start matches the band table (independent oracle) and is capped at the previous 22:00;
- manual overrides hold until the next boundary, are logged, and three similar ones raise one
  suggestion; away sets every room to its own floor + margin (21.5 with the defaults) and recovers;
- minimum on 30 / off 20 min, the starts alarm only on a short-cycling day;
- water writes: integers in 20 to 35, direct mode only, at least 1 K, at most one per 15 min, only
  numeric payloads; DHW: one window a day near the cheapest by price/COP, tank target at most 50,
  force DHW always released (including restarts in the window), never with the force heater on;
- faults: thermostats offline (one, all, lost writes), sensors unavailable, Heating_Mode 0, HA and
  AppDaemon restarts, AppDaemon down, a hung tick, garbage helpers (per room), helpers reset: never more than
  the minimum off time plus a tick in `DHW` while a room is below 21 degC, no dangerous write;
- **parity** with the archived `climate_control` / `dhw_control` under identical inputs: the same
  mode command path, mode switches per day at most legacy + 1, compressor starts at most
  1.3 x legacy or legacy + 2, no longer gap without heating, one forced DHW window a day (legacy
  two), a tank no warmer, outages handled at least as well.

**Parity finding (cold weather):** the new app heats **fewer hours at warmer water** than the legacy
rule (cold run: about 8 h a day at 29.5 degC mean water against 11 h at 26 degC). The software curve
gives 29 to 32 degC at 0 to -5 degC outdoor, while the legacy request stayed at 26 degC and its
cheap hours ran with the valves open. Strict 1.3 x legacy compressor starts is not met in three of
five runs (about 5 starts a day against 3.7); the test allows legacy + 2. Whether the warmer water
costs efficiency is for the preregistered efficiency gate (heat-pump kWh per heating degree-day,
base 15 degC, at most +20 % against the S3 band-matched baseline; climate preregistration 2026-27
in `pando85/home-energy-optimizer`) to judge on the real house. If the gate fails, flatten the curve with its
helpers (`input_number.comfort_curve_cold_water` and `_mild_water`, for example 30 / 25 degC); the
lead times are measured at 26 degC water, so they stay conservative.

The model is a test bench, not a forecast: it checks decisions and safety properties, not the real
house's kWh.


### Command acknowledgement and retries

Mode changes are sent through the existing HeishaMon selector automation but are **only
acknowledged when `sensor.aquarea_mode` reports the requested physical mode**. An unchanged
selector helper is not evidence of delivery: after two minutes without feedback, the
controller calls `automation.set_heatmode` to re-publish the command. While confirmation
is pending the controller withholds its heartbeat, enabling the Home Assistant watchdog
to intervene. A manual mode outside Heat+DHW/DHW remains untouched.

DHW first confirms `sensor.aquarea_tank_set_temperature` equals the requested
40–50 °C target; it cannot force DHW based on a helper update alone. Unacknowledged
target writes are retried every two minutes, re-triggering
`automation.set_tank_target_temperature` when the helper is already at the desired
value. The persisted plan distinguishes a `force_requested` intent from confirmed
`forced`, and the controller releases an outstanding force request when its window
expires. No new MQTT writer or physical control path was introduced.

The watchdog also re-triggers `automation.set_heatmode` when its selector is already
`Heat+DHW` but the unit remains in `DHW`; selecting the same value again would
otherwise have no effect.

The hardware commissioning checklist must include **lost-command simulations**:
verify actual status sensors change after each mode and tank request, and that a
disconnected HeishaMon leaves commands pending rather than falsely confirmed.
