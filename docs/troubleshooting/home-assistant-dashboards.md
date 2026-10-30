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
  ch1 − ch2 (`config/integrations/energy_circuits.yaml`). The source of this mapping is the row titles
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
`energy/save_prefs`, `energy/validate`). Since 2026-10-30 it is a hierarchy with `included_in_stat`,
`name` and live power (`stat_rate`):

```
Grid: Shelly EM channel 1 (PVPC price)
├─ Circuit 1: cooker, sockets & lighting (sensor.circuit_1_energy)
│   ├─ Servers (Shelly PM mini)
│   └─ Snapmaker (plug)
└─ Circuit 2: heat pump & appliances (Shelly EM channel 2)
    ├─ Heat pump · hot water / heating / cooling (sensor.aquarea_{dhw,heat,cool}_energy_consumed)
    └─ Washing machine (sensor.lavadora_energy_this_month)
```

HA shows the remainder of each parent as untracked (dishwasher, extractor and standby on circuit 2).

- **Do not use `sensor.aquarea_energy_consumed`** for history: it integrates
  `sensor.aquarea_power_consumed`, whose availability check was wrong until `bdfdc5825`, so it
  recorded 0 kWh until 2026-10-30. The three per-mode sensors are correct back to 2024-04.
- `sensor.circuit_1_energy` starts on 2026-10-30, so earlier periods show Servers larger than
  circuit 1.
- `sensor.lavadora_energy_yesterday` has negative statistics; use `..._this_month` (monthly reset is
  handled as a meter reset).
- The Shelly 2PM shutter energy sensors are left out (~0.1 kWh/month together).

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
