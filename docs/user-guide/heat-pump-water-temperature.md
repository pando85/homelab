# Heat pump water temperature

How the Panasonic Aquarea outlet (heating water) temperature is controlled from Home Assistant and
HeishaMon, which modes exist, what is exposed today and what is only reachable over MQTT.

This page was written from a read-only audit on 2026-10-30. Nothing was published to MQTT, no Home
Assistant service was called and no setting was changed. Statements are either **verified** (read in
code or observed live on that date) or **believed** (taken from the upstream HeishaMon docs or
inferred, not tested on the unit). The ones that matter for a write experiment are marked
*believed*.

Menu paths, ranges and the heater facts were later confirmed from the Panasonic service manual and
are cited as "service manual p.N".

## Data path

```text
Aquarea unit <-serial-> CZ-TAW1 running GoHeishaMon <-MQTT-> mosquitto <-MQTT-> Home Assistant
                                                                   (apps/mosquitto)
```

- The CZ-TAW1 runs the owner's fork
  [`pando85/GoHeishaMon`](https://github.com/pando85/GoHeishaMon) (audited at commit `561bcc8`, main
  code `src/taw2mqtt.go`), not upstream HeishaMon. It speaks to the unit over serial and to the
  broker over MQTT. The broker is authenticated (`mosquitto-user` Secret from Vault
  `/mosquitto/user`, `apps/mosquitto/templates/external-secret.yaml`).
- Base topics: state `panasonic_heat_pump/main/<Topic>`, commands
  `panasonic_heat_pump/commands/<SetTopic>`.
- Home Assistant side: `apps/home-assistant/config/integrations/heishamon.yaml` (MQTT sensors,
  switches, helpers and the automations that publish the commands).

## Control modes

The unit decides how it derives the heating water target for a zone from the setting
"Heating_Mode" (state topic `main/Heating_Mode`, also `Cooling_Mode` for cooling).

| `Heating_Mode` | Mode | `Z1_Heat_Request_Temp` means | Water target |
|---|---|---|---|
| `0` | Compensation curve | A **shift** in K, -5 to +5 | Read from the curve at the outside temperature, plus the shift |
| `1` | Direct | The **absolute** water temperature, 20 to 55 degC (60 degC above -10 degC outdoor; service manual p.26, 131) | The request itself |

The curve is four points per zone and mode, published as state topics:

| Topic (Z1 heating) | Meaning |
|---|---|
| `Z1_Heat_Curve_Outside_Low_Temp` | Coldest outside temperature of the curve |
| `Z1_Heat_Curve_Target_Low_Temp` | Water target at the coldest point, so the *highest* water temperature ("low" follows the outside temperature, not the water) |
| `Z1_Heat_Curve_Outside_High_Temp` | Warmest outside temperature of the curve |
| `Z1_Heat_Curve_Target_High_Temp` | Water target at the warmest point (the *lowest* water temperature) |

The unit keeps reporting the curve points while in direct mode; they are simply not used.
`Z1_Water_Target_Temp` and `Main_Target_Temp` show the water target that is actually in force, in
both modes.

Caveat (*believed*, from the upstream docs): when the zone sensor is a room thermostat or a
thermistor instead of the water temperature, the "direct" temperature is stored in the high curve
target and `SetZ1HeatRequestTemperature` does not set it. The fork does not publish
`Z1_Sensor_Settings`, so this cannot be checked over MQTT. On 2026-10-30 `Z1_Temp` equals
`Main_Outlet_Temp` (28), which suggests the zone sensor is the water temperature.

## Entities and topics

### Exposed in Home Assistant

| Purpose | HA entity | MQTT topic | Notes |
|---|---|---|---|
| Set Z1 heating water request | `input_number.heishamon_heatshift_z1` | `commands/SetZ1HeatRequestTemperature` | Integer, helper range -5 to 40, step 1. Absolute in direct mode, shift in curve mode. `heishamon.yaml:233-253`, helper at `:404-408` |
| Read Z1 request | `sensor.aquarea_z1_heat_request_temperature` | `main/Z1_Heat_Request_Temp` | Also mirrored into the helper by the "Set Z1 Heat selector" automation (`:221-230`) |
| Read Z1 water target | `sensor.aquarea_z1_water_target_temperature` | `main/Z1_Water_Target_Temp` | The effective target (`:750`) |
| Read outlet target / actual | `sensor.aquarea_outlet_target_temperature`, `sensor.aquarea_outlet_temperature` | `main/Main_Target_Temp`, `main/Main_Outlet_Temp` | Main target also follows the tank target in DHW |
| Read Z1 curve points | `sensor.aquarea_z1_heat_curve_{target_high,target_low,outside_high,outside_low}_temperature` | `main/Z1_Heat_Curve_*` | Read only (`:679-700`) |
| Z2 equivalents | `input_number.heishamon_heatshift_z2`, `sensor.aquarea_z2_*` | `commands/SetZ2HeatRequestTemperature` | Zone 2 is not active (`Zones_State` = 0) |
| Cooling request | `input_number.heishamon_coolshift_z{1,2}` | `commands/SetZ{1,2}CoolRequestTemperature` | Same range |
| Operating mode | `input_select.heishamon_heatmode`, `sensor.aquarea_mode`, `sensor.aquarea_mode_code` | `commands/SetOperationMode`, `main/Operating_Mode_State` | Driven by AppDaemon climate control (`Heat+DHW` / `DHW`) |
| Quiet mode | `input_select.heishamon_quietmode`, `sensor.aquarea_quiet_mode` | `commands/SetQuietMode`, `main/Quiet_Mode_Level` | `Off`, 1, 2, 3 |
| Powerful mode | `input_select.heishamon_powermode`, `sensor.aquarea_powerful_mode` | `commands/SetPowerfulMode`, `main/Powerful_Mode_Time` | `Off`, 30, 60, 90 min |
| Holiday mode | `switch.aquarea_holiday_mode` | `commands/SetHolidayMode`, `main/Holiday_Mode_State` | |
| Heat pump power | `switch.aquarea_main_power` | `commands/SetHeatpump`, `main/Heatpump_State` | |
| Force DHW | `switch.aquarea_force_dhw_mode` | `commands/SetForceDHW`, `main/Force_DHW_State` | Used by AppDaemon `dhw_control` |
| Tank target | `input_number.heishamon_tank_temp` | `commands/SetDHWTemp` | 40 to 75 |
| Pump speed (read) | `sensor.aquarea_pump_speed`, `sensor.aquarea_pump_flow` | `main/Pump_Speed`, `main/Pump_Flow` | |

The helpers are not on the dashboards in `apps/home-assistant/dashboards/`; they are only reachable
from the HA UI (entities, scripts, dev tools).

### Only at the MQTT level

| Topic | Kind | What it is |
|---|---|---|
| `main/Heating_Mode`, `main/Cooling_Mode` | state | `0` curve, `1` direct. **Not in HA**, so HA cannot tell which mode the request is written into |
| `main/Zones_State` | state | `0` zone 1, `1` zone 2, `2` both |
| `main/Heat_Delta`, `main/Cool_Delta` | state | Floor heating / cooling delta (K) |
| `main/Heating_Off_Outdoor_Temp`, `main/Heater_On_Outdoor_Temp` | state | Heating cut-off and backup heater outdoor limits |
| `main/Pump_Duty` | state | Pump duty (the fork has no max pump duty setting) |
| `main/Z1_Temp`, `main/Z2_Temp`, `main/Room_Thermostat_Temp` | state | Zone actual temperatures. `Z1_Water_Temp` and `Z2_Water_Temp` read `-78` (not fitted), `Z2_Temp` reads `-128` |
| `main/Z1_Cool_Curve_*`, `main/Z2_*_Curve_*` | state | Cooling curve and zone 2 curves |
| `commands/SetForceDefrost`, `commands/SetForceSterilization` | command | `0`/`1` |
| `commands/SendRawValue` | command | A raw hex frame, see below |
| `commands/OSCommand` | command | Runs a shell command on the CZ-TAW1 (`EnableCommand` is true in the sample config). Treat as root access to the gateway |

## Commands supported by the fork

All handlers are in `src/taw2mqtt.go` (subscriptions at `:503-556`). Every command is a 110-byte
frame that starts `f1 6c 01 10` with all other bytes zero (zero means "no change"), then the
checksum is added on send. Byte offsets are 0-based.

| Command | Payload | Byte | Value written | Line |
|---|---|---|---|---|
| `SetHeatpump` | `0` off, `1` on | 4 | `1` (off) or `2` (on); any non-`1` payload turns it off | `:839` |
| `SetForceDHW` | `0`, `1` | 4 | `64` / `128`; any non-`1` payload clears it | `:736` |
| `SetHolidayMode` | `0`, `1` | 5 | `16` / `32` | `:676` |
| `SetOperationMode` | `0`-`10` | 6 | `18`, `19`, `24`, `33`, `34`, `35`, `40`, `24`, `40`, `24`, `40` for payload 0 to 10 (7 and 8, Auto(Cool), are written as Auto); other payloads write `0` (no change) | `:602` |
| `SetPowerfulMode` | `0`-`3` | 7 | `(n+1) & 7` | `:658` |
| `SetQuietMode` | `0`-`3` | 7 | `(n+1) * 8` | `:821` |
| `SetForceDefrost` | `0`, `1` | 8 | `0` / `2` | `:716` |
| `SetForceSterilization` | `0`, `1` | 8 | `0` / `4` | `:696` |
| `SetZ1HeatRequestTemperature` | integer | 38 | `n + 128` | `:756` |
| `SetZ1CoolRequestTemperature` | integer | 39 | `n + 128` | `:773` |
| `SetZ2HeatRequestTemperature` | integer | 40 | `n + 128` | `:789` |
| `SetZ2CoolRequestTemperature` | integer | 41 | `n + 128` | `:805` |
| `SetDHWTemp` | integer | 42 | `n + 128` | `:640` |
| `SendRawValue` | hex of a full frame | any | as given | `:591` |

Behaviour that matters for safety (verified in the code):

- There is **no range check** in any handler. The value is parsed with `Atoi`, falling back to
  `ParseFloat`, and a payload that does not parse (including an empty one) becomes `0`, which is
  written as byte `128`, that is **0 degC**, not "no change". The byte is truncated to 8 bits, so
  out of range values wrap.
- Commands are queued FIFO (max 256) and sent one per loop with `SleepAfterCommand` (1 s) between
  them. A queued command expires after 2 minutes (`commandTTL`, `:38-48`), so a stale command is
  never replayed hours later.
- There is no acknowledgement frame. The fork does not report whether the unit accepted a value.

Not in the fork (upstream has them): `SetCurves`, `SetZones`, `SetMaxPumpDuty`, `SetPump`,
`SetFloorHeatDelta`/`SetFloorCoolDelta`, `SetHeatingOffOutdoorTemp`, zone sensor settings,
heater and bivalent settings, `SetReset` and the rest of upstream SET15 to SET50. There is also no
command (upstream or fork) that switches between curve and direct mode.

State topics published by the fork: `TOP0` to `TOP94` (`ParseTopicList3`, from `:1178`). Upstream
publishes up to `TOP143` plus the `extra/` and `optional/` groups; the fork has none of those.
`Heating_Mode` is `TOP76` (`:1771`, byte 28, low two bits minus one), `Cooling_Mode` is `TOP81`.

## Switching between curve and direct mode

There is no MQTT or HA command for this. It is an installer setting of the heat pump, made on the
unit's own controller (CZ-TAW1 remote or the indoor unit panel). Per the service manual (p.99, 105):
Menu > Installer setup > Operation setup > Heat > "Water temp. for heating ON", with the choices
"Compensation curve" (default) or "Direct". After the change, `main/Heating_Mode` flips to the new
value.

Manual-confirmed ranges (service manual p.26, 105, 131, 149):

- Curve: four points, with a user shift of +/-5 degC.
- Direct: 20 to 55 degC (60 degC above -10 degC outdoor).
- Powerful mode does not apply in direct mode.

The manual covers the indoor unit WH-ADC0309J3E5 with the outdoor unit WH-UD09JE5. The owner's units
are WH-ADC0309JE5C and WH-UD09JE5-1, assumed equivalent.

## Changing the curve

- **From the controller:** the same installer menu, per zone: the water target at two outside
  temperatures (the four points above; service manual p.105, 131). This is the supported way with this fork.
- **From HA/MQTT with the fork:** not supported. `SetCurves` does not exist in the fork.
- **Upstream HeishaMon:** `commands/SetCurves` takes a JSON document with `zone1`/`zone2`,
  `heat`/`cool`, `target`/`outside`, `high`/`low`. For Z1 heating it writes bytes 75 to 78 as
  `value + 128` (upstream `commands.cpp`, `set_curves`).
- **Raw workaround (*believed*, untested):** `SendRawValue` with a hand-built frame that sets bytes
  75 to 78 to `value + 128` would write the same bytes. It bypasses every check and a wrong frame
  goes straight to the unit; do not use it on the live system without a test plan.

## Current configuration (2026-10-30)

Read-only snapshot from the broker (full refresh at the fork's 300 s `ForceRefreshTime`) and the
Home Assistant Prometheus metrics.

| Item | Value |
|---|---|
| Heating mode (`Heating_Mode`) | `1`, **direct**; `Cooling_Mode` also `1` (direct) |
| Operating mode (`Operating_Mode_State`) | `3`, DHW only (AppDaemon climate control currently has space heating off) |
| Z1 heat request (`Z1_Heat_Request_Temp`) | 26 degC (absolute, direct mode) |
| Z1 water target (`Z1_Water_Target_Temp`) / `Main_Target_Temp` | 26 / 26 degC |
| Z1 heat curve (stored, not in use) | -5 degC outside -> 35 degC, 15 degC outside -> 26 degC |
| Z1 cool request / cool curve | 15 degC; 20 degC outside -> 15, 30 degC outside -> 10 |
| Z2 heat request / water target | 28 / 28 degC (zone 2 inactive: `Zones_State` 0) |
| Quiet / powerful / holiday / force DHW | off / off / off / off |
| Heat pump power | on, no error |
| Pump | `Pump_Duty` 0, `Pump_Speed` 0, `Pump_Flow` 0.13 L/min (not heating) |
| Tank | target 49 degC, actual 49 degC (`DHW_Heat_Delta` -4, sterilization 65 degC / 10 min) |
| Outlet / inlet | 28 / 38 degC; outside 18 degC |
| Deltas and limits | `Heat_Delta` 5 K, `Heating_Off_Outdoor_Temp` 35 degC, `Heater_On_Outdoor_Temp` 0 degC |
| `Heat_Pump_Model` | `-1` (the fork does not decode it) |

History from the Prometheus metrics (90 days): the Z1 request stayed at 26 and the four Z1 curve
points never changed. The Z1 and Z2 requests equal their curve "high" target (26 and 28); whether
the two are linked in the unit is not known (see the experiment note below).

## Electric heater and power limits

From the Panasonic service manual for this unit family (cited as "service manual p.N"). The manual
covers WH-ADC0309J3E5 with WH-UD09JE5; the owner's WH-ADC0309JE5C with WH-UD09JE5-1 is assumed
equivalent. The manual states no other heater steps for this model.

### The heater

- One 3.00 kW electric heater (13.0 A) on a separate supply (Power Supply 2, its own 16 A breaker);
  p.25-26. It is both the space-heating backup and, when the tank heater is set to Internal, the
  hot-water booster; p.138, 146.
- The heat pump alone draws at most 3.47 kW / 15.9 A (about 2.0 kW rated at A7W35); p.25. Heat pump
  plus heater is about 6.5 kW, which **exceeds the 5.0 kW contracted limit on its own**.
- The 5.0 kW limit is enforced by a smart meter that cuts the supply. The owner confirmed that cuts
  have happened. A trip is a safety and availability failure, so the heater must be treated as a
  load that can trip the house, not as a cost.

### What starts the heater automatically

| Trigger | Condition | Page |
|---|---|---|
| Space heating | Outdoor temperature below "O/D temp for heater ON" (default 0 degC; the live `Heater_On_Outdoor_Temp` reads 0 degC), plus outlet and delay conditions | 144 |
| Defrost | Only if the backup heater is enabled: outlet below 10 degC, outdoor below -10 degC or inlet below 27 degC | 145 |
| Hot water | Tank target above what the heat pump reaches alone | 138, 146 |
| Sterilization | Default 65 degC for 10 min, weekly; uses the heater | 150 |
| Anti-freeze | Water below 6 degC | 152 |
| Heat pump error | Only if "Force Heater" = Auto (default Manual) | 144, 147-148 |

Consequences for control:

- **Hot water above about 51 degC starts the 3 kW heater.** The heat pump alone practically reaches
  51 to 55 degC (control reference 51 degC; "above 55 degC only possible with backup heater"; p.26,
  107, 137). The tank range is 40 to 65 degC with the internal heater. The tank target of 49 degC in
  the current configuration is within the heat pump range.
- **A sterilization cycle starts the heater** (65 degC target), a scheduled 3 kW load every week.
- With the heater on, the heat pump plus the heater can exceed 5.0 kW before any other house load
  is counted.
- SMART DHW (default 20:00 to 05:00) can suppress hot-water reheat; p.152.

### Inputs that limit power

All of these need the optional PCB CZ-NS4P, except CN-THERMO; p.92-93, 98, 103, 159-163.

| Input | Effect | Note |
|---|---|---|
| Demand control, 0-10 V | Outdoor-unit current limited to 10-100% | The manual does not say it limits the heater |
| External compressor switch | A DIP pin selects "heaters only off" or everything off | |
| SG-ready | Grid-signal operating states | |
| CN-THERMO external switch | Whole system off | No PCB needed |

Whether any of them is fitted is not known. They are the candidates for an import-limit interlock;
the demand control input alone cannot be relied on to keep the heater off.

## Acknowledgement and latency

- Command path: HA `mqtt.publish` (QoS 0, retain false) -> fork queue -> serial frame at the next
  loop (about 1 to 3 s).
- Confirmation: the unit answers every poll with a full data frame. The fork publishes a state topic
  only **when its value changed** (`:1148`), so the state topic itself is the acknowledgement:
  `main/Z1_Heat_Request_Temp` for the request and `main/Z1_Water_Target_Temp` /
  `main/Main_Target_Temp` for the effect. Expect the request to change within a few seconds
  (*believed*; not measured, nothing was written). The outlet itself follows the new target over
  minutes (compressor ramp), not seconds.
- No change after about 10 s means the command was rejected or lost: there is no error topic.
- A fresh subscriber only sees changed topics until the next full refresh (up to 300 s), so wait
  for the refresh to read a static value such as `Heating_Mode`.
- HA mirrors the confirmed value back into the helper and mutes the command automation for 5 s
  (`delay` in `heishamon.yaml:246-253`).

## What happens if the request is written in the other mode

Not tested (writing is out of scope for the audit). Believed from the upstream ranges
("-5 to 5 or 20 to max"):

- **Direct mode, shift-sized value (for example 3):** below the 20 degC minimum; the unit is expected
  to ignore or clamp it, but a clamp to 20 degC would still heat the floor with a target far below
  the desired one.
- **Curve mode, absolute value (for example 26):** far above the -5 to 5 shift range; the unit is
  expected to ignore or clamp it. If it were accepted as a +26 K shift the water target would hit
  the unit's maximum.

Neither the fork nor the HA helper (range -5 to 40) prevents either case. Before any write, read
`main/Heating_Mode` first.

## Safety notes

- Strictly shadow phase for the battery optimizer: nothing here authorises writing. Any experiment
  is a separate, owner-approved step.
- The HA helper accepts -5 to 40 for every zone. It cannot reach direct temperatures above 40 degC
  and it accepts shift-sized values in direct mode. A dedicated helper or script with a narrower
  range (for example 20 to a chosen maximum in direct mode) and a check on `Heating_Mode` should
  sit in front of the command.
- The firmware clamp is unknown. The fork clamps nothing; upstream `set_z1_heat_request_temperature`
  clamps nothing either. The unit's own limits on the direct value are not documented (the upstream
  docs say "20 to max"); the highest target seen on `Main_Target_Temp` is 59 degC, during DHW.
- Garbage or empty payloads write 0 degC (see above). Always publish a validated integer with
  `retain: false`; a retained command would be replayed by the broker to every reconnecting
  subscriber.
- Echo (*believed*): when the unit reports a different request (for example after a change on the
  controller), the "Set Z1 Heat selector" automation updates the helper and the "Set Z1 Heat
  Temperature" automation then publishes the same value back. It is harmless for an identical value
  but it means every device-side change is re-written once.
- Writes are persistent in the unit; there is no automatic fallback if Home Assistant or the
  optimizer stops. A control loop needs a watchdog that restores a safe request (the existing
  AppDaemon watchdog does this for the operating mode only, see `appdaemon_climate.yaml`).
- `OSCommand` and `SendRawValue` are unrestricted over MQTT. Anyone with broker credentials can
  change unit settings; keep the broker credentials and `mosquitto` network exposure as they are.
- First write experiment, when approved: write a value equal to the current one, check that
  `Z1_Heat_Request_Temp` and `Z1_Heat_Curve_Target_High_Temp` behave as expected, then step by
  1 K, one change at a time, watching `Heating_Mode`, `Z1_Water_Target_Temp` and `Error`.

## Read-only checks

```bash
# state, from the broker pod; the secret is read from the mosquitto-user Secret, never printed
kubectl -n mosquitto exec mosquitto-0 -c mosquitto -- \
  mosquitto_sub -h 127.0.0.1 -u <user> -P <password> -W 330 -C 1 \
  -t 'panasonic_heat_pump/main/Heating_Mode'
```

Prometheus has the HA entities (`hass_sensor_unit_celsius{entity="sensor.aquarea_z1_heat_request_temperature"}`),
but not `Heating_Mode`, because HA does not expose it.
