# Home Assistant Dashboards: Live Editing and Pitfalls

## Overview

Every Lovelace dashboard is UI-managed (`mode: storage`): the config lives in
`/config/.storage/lovelace.<id>` on the `home-assistant-config` PVC, **not in git**. The dashboard
list is `/config/.storage/lovelace_dashboards`. They were redesigned on 2026-10-30 into `sections`
views; the build scripts used then were throwaway, so the live storage files are the source of
truth.

| Dashboard (`url_path`) | Sidebar | Content |
|---|---|---|
| Overview (`lovelace`, default) | yes | Badges, a "Now" section of conditional cards (errors, washer/dishwasher running, vacuum out, irrigation running, rack > 40 °C), energy, one section per area. Room headings link to the detail pages below |
| Climate (`dashboard-climate`) | yes | Rooms (thermostats + 24h history), heat pump, controls (advanced ones in an expander), AppDaemon smart scheduling with a PVPC "cheapest N hours" markdown card |
| Schedules (`dashboard-schedules`) | yes | Every `custom:scheduler-card` (heat pump, thermostats, shutters, extractor) |
| Battery (`dashboard-battery`) | yes | Operating the house battery (Omnibattery `venus`): a templated health card (optimizer live switch and heartbeat once deployed, Omnibattery mode, grid meter freshness, import headroom under the 5 kW trip, SOC, permissions, competing Omnibattery features, RS485 ownership), the two *Allow* switches as the stop control, live tiles with the PVPC price, 24 h price/power/SOC history and today's energy; the optimizer's plan (`config/integrations/battery_optimizer_plan.yaml` polls the service's `/plan`): planned vs actual battery power and SOC over 24 h and a next-hours table; manual override, limits and firmware in expanders. Created 2026-11-03 |
| Office (`dashboard-rack`), Terrace, Tanque, Plants, Mobiles | yes | Detail pages; diagnostics and rarely used entities live in `custom:expander-card` sections |
| Kitchen, Bedroom, Office T (`dashboard-kitchen`, `dashboard-bedroom`, `office-t`) | **hidden** | Detail pages reached from Overview room headings |
| Areas, Map | yes | Auto-generated strategies |

Installed custom cards (`lovelace_resources`): `scheduler-card`, `expander-card`,
`valetudo-map-card`, `flower-card`. Anything else (`apexcharts-card`, `card-mod`) is not installed.

## Editing a Dashboard Live

Use the websocket API from inside the pod (`kubectl exec -i home-assistant-hass-0 -- python3`, HA's
Python ships `aiohttp`), with an admin token passed on stdin, never printed. Get the token with the
login flow in [home-assistant-api-hacs-deploy.md](home-assistant-api-hacs-deploy.md); saving
dashboards needs an admin user, so service tokens such as `battery-optimizer`'s cannot do it.

| Command | Use |
|---|---|
| `lovelace/config` (`url_path`, `force: true`) | Read a config. The default dashboard is `url_path: null`, not `"lovelace"` |
| `lovelace/config/save` (`url_path`, `config`) | Replace a config; takes effect on page reload, no restart |
| `lovelace/dashboards/list` / `create` / `update` / `delete` | Dashboard list; `update` takes `dashboard_id` (e.g. `show_in_sidebar: false`); `url_path` must contain a hyphen |
| `get_states`, `render_template` | Check entities exist and test markdown templates before saving |
| `config/area_registry/update`, `config/entity_registry/update` | Area temperature/humidity sensors, entity `area_id` |

Procedure that kept every change reversible:

1. **Back up** before the first change: `cp -p /config/.storage/lovelace* /config/.storage/core.area_registry /config/lovelace-backup-<date>/`
   in the pod, plus a local copy. (`sha256sum *` order differs between the pod and a desktop
   locale; compare per file.)
2. **Validate** every entity the new config references with `get_states`; render markdown templates
   with `render_template`.
3. **Compare** the live config with the backup (or with your last save) and refuse to save if it
   changed — someone may have edited it in the UI.
4. **Save**, then read it back and compare with what you built.
5. **Rollback** = `lovelace/config/save` with the backup's `data.config`. Restore
   `show_in_sidebar`, delete created dashboards, and reset area sensors the same way.

Dashboard edits are not GitOps; agree each change with the user before applying it.

**Do not run heavy commands inside the HA container.** It has a 1 GiB memory limit: a
`grep -o '.{0,1500}pattern.{0,1500}'` over the minified frontend bundles in `hass_frontend/` got the
container OOM-killed (HA restarted, 2026-10-30). Copy the files out with `kubectl exec ... cat` and
search locally; `grep -l -F` to find the file is fine.

## Pitfalls

### Entity references silently break

A tile or entities row whose entity does not exist renders as "Entity not available" with no log
line. The old Climate dashboard pointed at `sensor.heishamon_power_consumed`/`_produced`/`_cop` for
months after the template sensors became `sensor.aquarea_*`. After renaming or removing an entity,
grep the dashboards (`/config/.storage/lovelace.*`) as well as the repo.

### Card behaviour

- Markdown cards need a **blank line** between lines; a single newline is joined.
- `statistic` cards (e.g. "today" with `period: {calendar: {period: day}}`, `stat_type: change`)
  need long-term statistics: the sensor must have a `state_class`. Template sensors without one (e.g.
  `sensor.aquarea_power_consumed`) only work in `history-graph`/tiles.
- `history-graph` on `climate.*` entities plots current vs target temperature and shades periods by
  `hvac_action`, so it is only meaningful when `hvac_action` is real (see localtuya below).
- Per-card `visibility` (`state_not: [...]`, `numeric_state`) is how the "only when relevant" cards
  and hidden-when-`unavailable` tiles work; it also applies to badges.
- A heading card's `tap_action: navigate` makes the section title a link to a detail page.

### Data behind the cards

- **Shelly EM main channels:** channel 1 (`sensor.shellyem_main_channel_1_*`) is the whole house;
  channel 2 is **circuit 2** (aerotherm, dishwasher, washer, extractor), not just the heat pump.
  Circuit 1 (cooker, sockets, humid areas, thermostats, lighting) is `sensor.circuit_1_power` =
  ch1 − ch2 + battery AC power (`config/integrations/energy_circuits.yaml`): the house battery plugs
  into circuit 1, so ch1 − ch2 alone is net of the battery (see [Energy dashboard](#energy-dashboard)). The source of this mapping is the row titles
  in `apps/home-assistant/dashboards/iot-energy.json`. The heat pump's own reading is
  `sensor.aquarea_power_consumed`.
- **Room temperatures** come from the thermostats' `current_temperature` attribute via template
  sensors (`config/integrations/room_temperatures.yaml`), set as each area's temperature sensor so the
  Areas dashboard shows them. The terrace uses the Higrow 1 DHT sensor.
- **Aquarea Z1/Z2 water temperature** is not fitted (HeishaMon reported `-78` for a year); the
  sensors were removed from `heishamon.yaml`.
- **HeishaMon firmware:** the heat pump's CZ-TAW1 runs the owner's
  [`pando85/GoHeishaMon`](https://github.com/pando85/GoHeishaMon) fork. `SetOperationMode` accepts
  0–10 and sends mode byte 0 for any other payload (`HandleSetOperationMode` in
  [`src/taw2mqtt.go`](https://github.com/pando85/GoHeishaMon/blob/561bcc81f33e/src/taw2mqtt.go)), so
  the "Set heatmode" automation in `heishamon.yaml` maps every `input_select.heishamon_heatmode`
  option to 0–8, mirroring "Set heatmode selector".
- **Disabled `sensor.shellyem_main_channel_*_power_2`** entities look stale but are the rollback
  anchors of the MQTT handover — do not delete them. Orphan `*_mqtt_probe` rows in `states_meta` are
  harmless and cannot be removed. See
  [home-assistant-entity-id-handover.md](home-assistant-entity-id-handover.md).

### Energy dashboard

The built-in Energy dashboard is configured in `/config/.storage/energy` (API: `energy/get_prefs`,
`energy/save_prefs`, `energy/validate`). It is a hierarchy with `included_in_stat`, `name` and live
power (`stat_rate`):

```
Grid: Shelly EM channel 1 (PVPC price)
Battery: Venus E V3 (from = sensor.venus_total_discharging_energy, to = ..._charging_energy,
         power sensor.venus_ac_power, SOC sensor.venus_battery_soc, 5.12 kWh)
├─ Circuit 1 (sensor.circuit_1_energy, gross of the battery): cooker, sockets, humid areas,
│   thermostats, lighting
│   ├─ Z800 (Shelly PM mini)
│   ├─ Snapmaker (smart plug)
│   └─ Servers (sensor.servers_energy = ∫ sensor.venus_ac_offgrid_power, battery backup port)
└─ Circuit 2 (Shelly EM channel 2): aerotherm, dishwasher, washer, extractor
    ├─ Heat pump (sensor.aquarea_energy_consumed)
    │   └─ Hot water / Heating / Cooling (sensor.aquarea_{dhw,heat,cool}_energy_consumed)
    └─ Washing machine (sensor.lavadora_energy_this_month)
```

HA shows each parent's remainder as `<name> (untracked)` in the device charts and as untracked
consumption in the flows (dishwasher, extractor and standby on circuit 2).

- Only devices drawing at least 0.1% of the total power are drawn in the "Now" power flow; an idle
  circuit 2 (~35 W) is a thin strip.
- `sensor.lavadora_energy_yesterday` has negative statistics; use `..._this_month` (monthly reset is
  handled as a meter reset). The Shelly 2PM shutter energy sensors are left out (~0.1 kWh/month).

#### Battery and circuit 1

The battery's AC plug is on circuit 1 and the servers hang off its backup (EPS) port. HA counts home
consumption as grid import + battery discharge − battery charge, so circuit 1 has to be **gross** of
the battery for the devices to add up: `Circuit 1 Power` = ch1 − ch2 + `sensor.venus_ac_power`
(+ discharging, − charging). `venus_ac_power` and the two counters are the inverter's whole AC side,
backup-port feed included: on 2026-11-03 a ~300 W discharge with ~250 W of servers on the backup port
lowered ch1 − ch2 by ~260 W (not ~550 W) and the discharge counter rose ~0.31 kWh/h. The battery's
grid-port draw is therefore servers − `venus_ac_power`, and the cases are:

| Battery state | ch1 − ch2 (net) | Gross circuit 1 |
|---|---|---|
| Bypass / idle (AC power 0) | other loads + servers (passed through) | same; no battery flow |
| Discharging P W | other loads + servers − P (can be negative) | other loads + servers |
| Charging C W (AC power −C) | other loads + servers + C | other loads + servers |
| Grid outage | house meters dark | servers fed by the battery; circuit 1 unavailable, so Servers can exceed Circuit 1 for that window |

- The net difference must **not** be clamped before adding the battery (it goes negative while the
  battery feeds circuit 2 too); only the gross is clamped at 0 for sampling skew.
- An unavailable `venus_ac_power` counts as 0 W (idle), so an Omnibattery outage while the battery
  keeps discharging under-counts circuit 1 for that time.
- Servers (`sensor.servers_energy`) is the backup-port output whether it comes from the grid (Bypass)
  or the battery, and is always inside gross circuit 1, so there is no double count.
- The battery's own standby draw shows as Circuit 1 (untracked); its conversion losses are the
  charge − discharge difference on the battery flow, not consumption. The gross is integrated from
  power while HA's total uses the 0.01 kWh counters, so a small residual lands in untracked.
- Before the gross formula was deployed, `sensor.circuit_1_energy` integrated the net difference: from
  battery commissioning (2026-11-03) to the HA restart it is low by discharge − charge (a few tenths
  of a kWh); not backfilled.

#### Statistics backfill (2026-10-30)

Two energy series were repaired with the websocket API rather than re-deployed sensors:

- `sensor.aquarea_energy_consumed` stopped counting on 2025-12-10 18:00 because its source
  `sensor.aquarea_power_consumed` had a wrong availability check (fixed in `bdfdc5825`); 1560.6 kWh
  were missing. The gap hours were rebuilt from the sum of the three per-mode sensors with
  `recorder/import_statistics`, continuing from the last good sum, and every later row was shifted with
  `recorder/adjust_sum_statistics` (it shifts hourly and 5-minute rows, so live counting continues).
  Monthly totals now equal the per-mode sum since 2024-04.
- `sensor.circuit_1_energy` (created 2026-10-30) got history back to 2024-04 from channel 1 − channel 2
  hourly deltas (negatives clamped), imported with sums anchored so the first live hour counts in full.
  Its old sums are negative by design; only differences are ever displayed.

Rules that made this safe: export the rows first (`\copy ... TO STDOUT WITH CSV` on `statistics`,
`statistics_short_term` and `statistics_meta`); only import hours older than the sensor's first live
row, or shift live rows with `adjust_sum_statistics` — never import over live rows, because new
statistics are compiled from the last 5-minute row; wait for the hour's compile (`HH:00:10`) when the
anchor hour is the current one; check monthly totals against the source series afterwards. The import
is queued in the recorder, so re-check after a few seconds. Login-flow access tokens expire after 30
minutes.

### Grafana energy dashboard: counter functions (`iot-energy.json`)

`apps/home-assistant/dashboards/iot-energy.json` is a Grafana dashboard (provisioned from git, unlike the Lovelace
dashboards above) that sums energy counters from `hass_sensor_energy_kwh` in `prometheus-long-term`.

**Restart merge.** Each Home Assistant pod restart changes the `instance` and `pod` labels and starts a new series,
so summing `increase()` per series loses the energy accrued in the gap between the old and the new series (commits
`c0295b0ce` and `64472d1a0`). Every energy panel therefore first merges the series with
`max without(instance, pod)` and takes the function over a 1 m subquery (the scrape interval), for example
`increase((max without(instance, pod) (hass_sensor_energy_kwh{entity="..."}))[$__interval:1m])`.

**Which function per counter.** Checked against the history since 2023-11-07:

| Counter | Function | Why |
|---|---|---|
| Shelly EM channels 1 and 2, Z800 (`sensor.shellypmmini_z800_energy`), Venus charging and discharging totals | `increase` | Never dipped or reset |
| Snapmaker plug | `increase` | The device really resets to 0 (44 times) |
| Aquarea heat, hot water and cool, and `sensor.servers_energy` | `delta` | They dip after a restart restores a stale value (81 dips on Aquarea; on 2024-04-29 two dips of 5.5 and 11.7 kWh never recovered) |

**Rule.** Never use `increase` on a counter that dips: `increase` takes a drop for a counter reset and adds the whole
counter value (for example +2198 kWh instead of 8.2 for one Aquarea dip of 0.13 kWh). Never use `delta` on a counter
that resets, because it subtracts the reset. When adding a panel for a new counter, look up its history for dips and
resets first and pick the function from the table.

**Rejected alternative.** Summing clamped per-minute increases is safe for both cases, but it loses the energy that
accrues in data gaps longer than 5 minutes (about 1.5 to 2%).

**Rename.** `sensor.shellypmmini_servers_energy` was renamed `sensor.shellypmmini_z800_energy` on 2026-11-03, when
the servers moved to the battery's backup port and that Shelly PM mini began metering the Z800 workstation. The
servers are now `sensor.servers_energy`, the integral of `sensor.venus_ac_offgrid_power`
(see [Battery and circuit 1](#battery-and-circuit-1)). The old id no longer exists, so a panel or
query that still uses it must be pointed at the new one.

### Where history lives

- The recorder keeps **10 days**.
- `hass_*` metrics (the `/api/prometheus` ServiceMonitor) are scraped by **`prometheus-long-term`**
  (`https://prometheus-long-term.internal.grigri.cloud`) only; `prometheus.internal.grigri.cloud`
  has none. Use it for anything older than 10 days, e.g.
  `hass_climate_action{entity=~"climate.thermostat_.*"} == 1` together with `hass_climate_mode`,
  `hass_climate_current_temperature_celsius` and `hass_climate_target_temperature_celsius`.

### Thermostat heating state

The thermostats' own `hvac_action` source (DP 36) follows the mode, not the demand; `heuristic_action`
derives it from temperatures instead. See
[home-assistant-localtuya.md](home-assistant-localtuya.md#hvac_action-dp-36-follows-the-mode-not-the-demand).
