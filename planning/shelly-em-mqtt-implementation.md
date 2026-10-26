# Shelly EM MQTT high-frequency telemetry — reboot loop root-caused

**Status:** **root-caused 2026-10-26, MQTT is viable.** The reboot loop that made this look
"abandoned" is *not* caused by MQTT on its own. It needs three things at once: **MQTT enabled** +
**`coiot.enabled: false`** + **HA in a `setup_retry` storm** against the `/cit/d` endpoint that hangs
while CoIoT is off. Since CoIoT must stay enabled anyway (see below), the production configuration is
the one combination that does *not* fault: **MQTT on + CoIoT on ran 20 min clean** with continuous
publishing and zero unscheduled reconnects.

**Device state right now:** `mqtt.enable: true` (`update_period: 30`), CoIoT enabled, HA entry
`loaded`, 14 entities live, recorder continuous. MQTT is left **enabled as an overnight soak** on the
exact target configuration; nothing subscribes to it yet, so it cannot affect grid telemetry.
Rollback is one line — see [Device state now](#device-state-now-verified).

**Correction to the earlier conclusion:** the first pass blamed MQTT because every spontaneous reboot
landed in an MQTT-on window. That was a confound — CoIoT had been disabled during the same window, and
HA had just restarted into a retry storm. Isolating the variables (matrix below) shows MQTT is a
*co-factor that removes heap margin*, not the trigger.

**Second, more important outcome:** the attempt exposed a latent landmine — `coiot.enabled: false`
on a Gen1 Shelly breaks HA's *setup* path, so the next HA restart drops every entity of that config
entry. It cost ~48 min of grid telemetry. Full write-up:
[`shelly-gen1-coiot-disabled-breaks-ha-setup.md`](../docs/troubleshooting/shelly-gen1-coiot-disabled-breaks-ha-setup.md).

**Goal (unchanged):** get `shellyem-main`'s grid telemetry into Home Assistant at the highest
frequency the device can produce so a future battery integration (omnibattery) can run a zero-export
/ power-limiting control loop on it — **without changing a single entity ID, friendly name or
Prometheus label set**.

## Root cause: a three-way interaction, not MQTT alone

### Isolation matrix

Each row is a real observation window on 2026-10-26 (UTC). "Retry storm" = the `shelly` config entry
in `setup_retry`, so HA repeatedly calls `BlockDevice.initialize()` → `GET /cit/d`, which **hangs**
while CoIoT is disabled.

| MQTT | CoIoT | HA | Window | Outcome |
|---|---|---|---|---|
| **on** | **off** | **retry storm** | 18:03–18:29 | **4+ spontaneous reboots**, 71–280 s apart |
| off | off | retry storm | 18:29–18:49 (~20 min) | stable — *this is the control that exonerates CoIoT-off alone* |
| on | on | `loaded` | 19:34–19:54 (20 min) | **stable** — 37 publishes, no interval >32 s, 1 reconnect (my own reboot) |
| off | on | `loaded` | 18:49–19:34 (45 min) | stable (device uptime 2 729 s) |
| on | off | no storm, synthetic hanging `/cit/d` | ~19:52, 3 rounds × 6 | **stable** (uptime 98 → 195), but `/status` stopped answering in 2 of 3 rounds |

MQTT and CoIoT-disabled are each individually survivable. Only the pair, with HA hammering the hanging
endpoint, faults the device.

### Mechanism (measured, not inferred)

| Measurement | CoIoT **off** + HA retrying | CoIoT **on** |
|---|---|---|
| `ram_free` (of `ram_total` 51 064) | **27 224 B** | **35 368 B** |
| `GET /cit/d` | hangs, no response, no 404 | `200`, 922 B, 36 ms |
| `GET /status` during 6 concurrent hanging `/cit/d` | **no response** (2 of 3 rounds) | n/a |
| Heap low-water marks in the device log | `New heap free LWM: 25960`, `25888` | `31408` |

So each stuck `/cit/d` handler pins a mongoose HTTP connection slot *and* its buffers — roughly **8 KB
of heap and all the connection slots** for the duration of HA's retry storm. The per-request leak is
small (18 hanging requests cost ~280 B of *unrecovered* heap: 27 224 → 26 944), which is why the
device survives my synthetic replay; what kills it is the **standing** consumption plus MQTT's own
footprint leaving no margin for a large allocation (WiFi TX / MQTT publish buffer) → fault → reset.
This also explains the irregular reboot spacing: HA's backoff grows toward `SETUP_RETRY_MAX_WAIT = 600 s`,
and each reboot resets the heap, so the interval depends on when the next retry burst lands.

### What I could not capture

- **No exception dump.** `debug_enable=true` does not persist on fw 1.14.0 — `POST /settings?debug_enable=true`
  is accepted and reads back `false`. `/debug/log` *is* readable with debug disabled, but it is frozen at an
  older boot's tail, so it cannot record the crash. Do not rely on it as a live instrument on this firmware.
- **Synthetic replay is not sufficient.** Firing hanging `/cit/d` requests by hand does not reproduce the
  fault; it needs HA's full `initialize()` sequence under retry backoff (`DEVICE_IO_TIMEOUT = 10 s`,
  `CIT_RETRIES = 3`, `SETUP_RETRY_MAX_WAIT = 600 s`).

### Original experiment log

All times UTC 2026-10-26. "Commanded" = an explicit `GET /reboot`; "spontaneous" =
`uptime` observed lower than the elapsed time since the last boot, with no request from me.
**Every spontaneous reboot below happened while CoIoT was disabled** — that is the confound that made
MQTT look guilty.

| Time | Configuration | Event | Evidence |
|---|---|---|---|
| 17:36:52 | MQTT on, broker hostname, `update_period=30` | commanded boot | `/settings` `.mqtt.enable: true` |
| ~17:41 | MQTT on, `update_period=3` | commanded boot | published at exactly 3.00-3.01 s |
| ~17:47 | MQTT on, `update_period=5` | commanded boot | 55 s capture, clean |
| 18:03:54 | MQTT on, `update_period=5` | **spontaneous reboot** | `uptime: 12` at 18:04:06 |
| ~18:08:38 | MQTT on, `update_period=5` | **spontaneous reboot** | `uptime: 161` at 18:08:29 → `uptime: 12` at 18:08:50 |
| 18:12:11 | MQTT on, `update_period=30` | commanded boot | — |
| 18:14:08 | MQTT on, `update_period=30` | **spontaneous reboot**, 117 s later | `uptime: 113` at 18:16:01; **no HTTP traffic at all** during the 230 s window |
| 18:17:17 | **MQTT off** | commanded boot | — |
| 18:21:07 | MQTT off | **no reboot** | `uptime: 230` = exactly the quiet window |
| 18:23:09 | MQTT on, **raw VIP** `192.168.193.9:1883`, `update_period=30` | commanded boot | — |
| 18:24:20 | MQTT on, raw VIP | **spontaneous reboot**, 71 s later | `uptime: 160` at 18:27:00 |
| 18:29:17 | **MQTT off** | commanded boot | stable since |

Ruled out:

| Hypothesis | Test | Result |
|---|---|---|
| Publish rate too aggressive | `update_period` 5 vs 30 | Both loop; 30 is the firmware default |
| Broker hostname / DNS on the device | `mqtt_server` as FQDN vs raw VIP `192.168.193.9` | Both loop (the VIP looped *faster*) |
| My HTTP polling load | 230 s and 150 s windows with **zero** requests to the device | Looped anyway |
| MQTT connect failure / bad credential | `mqtt.connected: true` in every window, `broker_clients_connected` 6 → 7, messages received at the broker | Connected and publishing fine right up to each reboot |
| Unbounded memory leak | `ram_free` sampled across windows | 35 468–36 124 B when idle, steady — **but** 27 224 B while HA's retry storm was in flight. The consumption is *standing*, not cumulative (see mechanism) |
| Relay side effects | `relays[0].ison` / `default_state` | `default_state: "on"`, `ison: true` throughout — the extractor never toggled |
| Data loss | ch1/ch2 `total` counters vs. recorder history | Monotonic, **zero negative deltas** across all reboots (flash-persisted) |
| A **retained** command redelivered on every connect | `mosquitto_sub -t 'shellies/#' -F '%r\|%t\|%p'` | **Zero** retained messages under `shellies/#` (the only retained traffic on the broker is valetudo under `homeassistant/#` and `valetudo/#`) |
| A rogue publisher hitting `…/command` (`reset_data` / `update_fw` would reboot it) | watched all shelly topics; grepped every `mqtt.publish` in `apps/home-assistant/config/` | Nothing publishes to shelly topics — `heishamon.yaml` only touches `panasonic_heat_pump/*` |
| The broker dropping the client | read `apps/mosquitto/config/mosquitto.conf` | No `max_keepalive`, no `max_queued_messages` / `max_inflight_messages` / `message_size_limit` overrides — and a broker-side disconnect could not *reboot* a device regardless |
| HA's MQTT probe sensors (added 18:01, reboots started 18:03) | pub/sub decoupling | A subscription lives entirely on the broker and generates **no** traffic to the publisher — the device cannot observe its subscribers. The correlation with the probes was really a correlation with the HA restart that put the entry into `setup_retry` |

**Measurement pitfall found on the way:** `broker_clients_connected` cannot see these reboots. The
device reconnects with the same client id (`shellyem-244CAB417668`), so mosquitto replaces the stale
session and the count stays flat — it held at 7 straight through two confirmed reboots. The count also
flaps 6↔7↔8 from my own `mosquitto_sub` sessions. Use the device's `online` / `announce` topics as the
reboot marker instead: they are emitted on every MQTT connect and are unambiguous.

Not tested, and no longer needed: firmware `1.14.1-rc1-g0617c15` (the device offers it as
`beta_version`; `has_update: false`) — **explicitly declined by the owner** for the main grid meter —
plus `keep_alive` / `clean_session` / QoS variants and a second EM unit for A/B. The fault is explained
without them; revisiting any of these only makes sense if the loop reappears with CoIoT **enabled**.


Note `"hw_revision": "dev-prototype"` in `/settings` — this unit may not be typical retail hardware.

## Incident: 48 min of grid telemetry lost

The MQTT work restarted HA (Reloader, on the ConfigMap change for the probe sensors) at 18:01:16Z.
That re-ran `BlockDevice.initialize()`, which needs `GET /cit/d` — and `/cit/d` **hangs** while
`coiot.enabled: false`. The entry went to `setup_retry` with
`Device communication error occurred for shellyem-main` and **all 14 entities went `unavailable` at
18:02:35Z**, while the device itself answered `/shelly`, `/settings` and `/status` perfectly.

Recovered at 18:50:34Z by re-enabling CoIoT through the correct endpoint
(`POST /settings/advanced?coiot_enable=true&coiot_update_period=15&coiot_peer=` + reboot, then
`homeassistant.reload_config_entry`). Diagnosis, evidence and pitfalls:
[`shelly-gen1-coiot-disabled-breaks-ha-setup.md`](../docs/troubleshooting/shelly-gen1-coiot-disabled-breaks-ha-setup.md).

The `coiot.enabled: false` drift pre-dates this work — it was already observed during the read-only
investigation, and it is what makes the failure *latent*: an initialized HA polls `/status` over REST
and never touches `/cit/*`.

## Device state now (verified)

Checked at 20:04:08Z, 2026-10-26:

| Setting | Value | Note |
|---|---|---|
| `mqtt.enable` | **`true`** | soaking on the target configuration; `server: mosquitto.internal.grigri.cloud:1883`, `update_period: 30`, `keep_alive: 60`, `max_qos: 0`, `clean_session: true`, `retain: false` |
| `mqtt.connected` | `true` | client id `shellyem-244CAB417668`; `broker_clients_connected` 6 → 7 |
| `coiot` | `{"enabled": true, "update_period": 15, "peer": ""}` | **must stay enabled** — it is both the HA setup dependency and what keeps MQTT safe |
| `cloud` | `false` | untouched |
| `GET /cit/d` | `200`, 922 bytes in 36 ms | HA setup dependency satisfied |
| `ram_free` | 35 288 B of 51 064 B | healthy; ~8 KB lower while stuck `/cit/d` handlers are in flight |
| `uptime` | 570 s (boot 19:54:38Z, commanded) | no spontaneous reboot since |
| HA `shelly` entry | `loaded`, 14 entities live | recovered on its own once CoIoT was back; no reload needed |
| Recorder | ch1 power ~2 rows/min, continuous 19:26 → 20:01Z | no gap >40 s |
| Entity registry | 19 `*shellyem*` entities, all platform `shelly` (+1 derived `energy`) | **no MQTT-discovered duplicates** — Gen1 publishes no `homeassistant/#` discovery configs |

**Rollback** (if the soak shows any spontaneous reboot — check the `online` topic, not
`broker_clients_connected`):

```bash
SUSER=$(pass show iot/shellyem-main | sed -n 's/^login: //p'); SPASS=$(pass show iot/shellyem-main | head -1)
curl -s -u "$SUSER:$SPASS" -X POST -G http://shellyem-main.iot.grigri/settings \
  --data-urlencode mqtt_enable=false -o /dev/null          # never answers: expect curl (28)
curl -s -u "$SUSER:$SPASS" http://shellyem-main.iot.grigri/reboot >/dev/null
```

⚠️ **Do not disable CoIoT on this device again** — it breaks HA's setup path *and* it is the co-factor
in the reboot loop. Do not restart HA without first checking `GET /cit/d` answers.


## Decisions (2026-10-26)

D1/D9/D2/D3/D5 were the design that the (mis-diagnosed) blocker appeared to invalidate. Root-causing
it **reinstates** them: they are gated on the soak, not on a hardware problem.
D4, D6, D7, D8 remain valid regardless of transport.

| # | Decision | Choice | Status |
|---|---|---|---|
| D1 | Transport | **MQTT** — the device republishes the whole emeter block on change (measured **0.37 s** apart under load) plus a periodic floor | **Reinstated** — the reboot loop was the CoIoT-off/retry-storm interaction, not MQTT. Gated on a clean soak |
| D9 | `mqtt_update_period` | **5 s** — bounds idle staleness at 5 s (60× margin under `freshness_s.grid_power: 300`) while transients still arrive on change | **Valid** — the soak runs at the 30 s default; set 5 at cutover |
| D2 | Broker credential | Reuse the shared mosquitto user (Vault `/mosquitto/user`) | **Valid** — confirmed working end to end |
| D3 | Scope | Partial takeover — only `channel_{1,2}_power` move to MQTT; energy counters, cost entity, voltage, power factor, relay and reboot button stay native | **Valid** |
| D4 | Naming | Identical `entity_id` and `friendly_name`, so `hass_sensor_power_w{...}` stays byte-identical | **Valid** — see [Series identity contract](#series-identity-contract-why-d4-works) |
| D5 | Recorder | Record both channels (upper bound ~+34.5 k rows/day, +18 % on 188 733/day) | **Valid** |
| D6 | Prometheus scrape | Unchanged at 60 s — the dataset builds 900 s slots from counter deltas and `build.py` never uses instantaneous power | **Valid** |
| D7 | CoIoT / repair | ~~Leave `coiot` disabled~~ → **CoIoT must stay enabled** — doubly mandatory now: it is HA's setup dependency *and* the reboot loop's co-factor. The `push_update_244CAB417668` repair was **not** ignored — it is left visible as a genuine signal | **Revised** |
| D8 | pfSense | No change — IoT-VLAN TCP to the mosquitto VIP already passes | **Valid** (and confirmed: the device connected through the VIP) |


## Measured MQTT behaviour (still valid)

Measured live while MQTT was enabled. **The transport is change-driven, not clock-driven** — this
overturned the "~1 Hz" assumption the plan started from.

| Observation | Measurement |
|---|---|
| Publish granularity | The device republishes the **whole emeter block** at once: `power`, `pf`, `reactive_power`, `voltage`, `total`, `total_returned` per channel all share identical timestamps |
| Trigger | **On change**, plus a periodic floor of `mqtt_update_period` |
| `mqtt_update_period=30` (default) | 7 publishes/topic in 60 s; intervals 0.40 … 16.41 s, mean ≈ 5.6 s |
| `mqtt_update_period=5` | 14 intervals in 55 s: **10 at 5.01 s** (the floor) and **4 below it — 0.37, 0.41, 4.40, 4.60, 4.64 s** (on change) |
| `mqtt_update_period=3` | 12 intervals, all 3.00-3.01 s (periodic dominates) |
| Topics published | `emeter/{0,1}/{power,pf,reactive_power,voltage,total,total_returned}`, `relay/0`, and `energy`/`returned_energy` once per connect |
| Payload format | 2 decimals (`589.80`, `-0.70`, `243.93`); `total` in **Wh** with 1 decimal (`17260032.2`) |
| Channel mapping | `emeter/0` = channel 1, `emeter/1` = channel 2 — confirmed twice: native unique_ids are `244CAB417668-emeter_{0,1}-power`, and HTTP `/status` totals match the MQTT `total` payloads |
| **Reboot required** | An `mqtt_*` settings change is stored but **not applied**: without a reboot the device reported `mqtt.connected: false` and published nothing |
| Settings POST | Applied but **not answered** — `curl` times out with no body and `http=000`. Verify with a follow-up `GET /settings` |
| Broker side | `broker_clients_connected` 6 → 7; HA-side MQTT sensors tracked the device at 33 rows / 3 min (≈5.5 s mean) |

The effective maximum frequency is the device's own change detection (≥2.7 Hz observed), *not*
`mqtt_update_period`; the period only bounds staleness when the load is static.

## Context and current state

All read-only, verified 2026-10-26 (before the attempt).

### Device

| Item | Value | Source |
|---|---|---|
| Device | `shellyem-main`, Shelly EM (`SHEM`), Gen1, fw `1.14.0-gcb84623`, MAC `244CAB417668` | `/shelly`, `/settings` |
| Address | `192.168.86.130` = `shellyem-main.iot.grigri`, IoT VLAN 501 | pfSense DHCP/ARP |
| `device.report_period` | **1 s** | `/settings` |
| `coiot` | was `{"enabled": false, ...}` — **drifted** from the CoIoT doc; restored to `enabled: true` | `/settings` |
| `cloud` | disabled | `/settings` |
| Credentials | `pass show iot/shellyem-main` (first line = password, `login:` line = username) | — |

### Home Assistant

| Item | Value |
|---|---|
| Version / pod | 2026.10.4, `home-assistant-hass-0` = `10.0.4.218` on `prusik` |
| `shelly` config entry | id `18e809d8adef1670840ff6fbe3544c0e`, gen 1, host `shellyem-main.iot.grigri`, user `agil` |
| `mqtt` config entry | `mosquitto.internal.grigri.cloud:1883`, protocol 5 |
| YAML config | 100 % GitOps: `/config/configuration.yaml` is `!include gitops/configuration.yaml`; packages come from `apps/home-assistant/config/integrations/` via ConfigMap `home-assistant-config-integrations` |
| Reload behaviour | **Any ConfigMap change restarts the HA pod** — which re-runs `initialize()` for every Gen1 device. Treat that as a risky operation until `/cit/d` is verified |
| Current cadence | ~33 s (`2.2 × 15`), verified in `/api/history/period` |

### Broker

| Item | Value |
|---|---|
| Service | `mosquitto` LoadBalancer, VIP `192.168.193.9`, ports 1883/8083/9234, `externalTrafficPolicy: Local` |
| BGP | `192.168.193.9/32` has **one** path, from `prusik` (`192.168.192.3`) → `Local` is safe |
| DNS | pfSense unbound resolves `mosquitto.internal.grigri.cloud` → `192.168.193.9` |
| Config | `allow_anonymous false`, `password_file`, persistence on, **no `acl_file`** |
| Clients | `broker_clients_connected = 6` (7 while the EM was connected) |
| IoT reachability | pf state table: `192.168.86.100` and `192.168.86.200` → `192.168.193.9:1883` ESTABLISHED. The IoT catch-all is TCP-only (`flags S/SA` without `proto`) — it blocks CoIoT's UDP but **passes MQTT** (confirmed: the EM connected and published) |

### Series identity contract (why D4 works)

Live series:

```
hass_sensor_power_w{domain="sensor", entity="sensor.shellyem_main_channel_1_power",
                    friendly_name="shellyem-main Channel 1 Power",
                    endpoint="http", instance="10.0.4.218:8123", job="home-assistant",
                    namespace="home-assistant", pod="home-assistant-hass-0",
                    service="home-assistant-hass"}
```

| Piece | Determined by | Source |
|---|---|---|
| metric name | `{namespace}_sensor_{device_class}_{unit}` | `components/prometheus/__init__.py:1109-1113`, `_sanitize_metric_name:568` |
| `entity`, `domain`, `friendly_name` | the only labels HA emits | `components/prometheus/__init__.py:597-601` |
| `job/namespace/service/endpoint/instance/pod` | ServiceMonitor relabeling, same target | unchanged |
| `series_key` | sha256 of the label set minus `VOLATILE_IDENTITY_LABELS = {instance, pod_ip}` | `battery-optimizer/src/battery_optimizer/extract_prom.py:147` |

`pod` is stable (StatefulSet), `instance` is stripped → **the Parquet archive continues unbroken**.
The recorder's `states_meta` is keyed by the `entity_id` string (ch1 power = `metadata_id` 182,
ch2 = 186), so new rows append under the same id → **HA history and dashboards continue too**.

`name: "shellyem-main Channel 1 Power"` yields both the friendly_name and, via slugify,
`sensor.shellyem_main_channel_1_power`.

This contract is transport-agnostic: it is what any future takeover (MQTT, ESPHome, a replacement
device) must satisfy to avoid touching the consumers.

### Consumers that must not break

- `battery-optimizer/src/.../extract_prom.py` — `ENTITY_SPECS` / `DEFAULT_ENTITY_IDS` hardcode
  `sensor.shellyem_main_channel_1_{power,energy,energy_returned}`
- `apps/battery-optimizer/files/service.yaml` + the app repo's `config/service.yaml` — `grid_power`,
  `grid_energy`, `observed`
- `apps/home-assistant/dashboards/iot-energy.json` — 11 refs to ch1 energy, 2 to ch1 power
- ADR-0003 in the app repo — ch1 = whole-home feed, ch2 = aerotherm branch (owner-confirmed wiring)

With D4, **none of them needs a change** — and since the cutover never happened, none was touched.

## What was executed

| TODO | Outcome |
|---|---|
| **1** Enable MQTT + measure | ✅ Done. Measurements above. Then **rolled back** (`mqtt_enable=false`) because of the reboot loop |
| **2** Probe entities | ✅ Executed, then **reverted**. `apps/home-assistant/config/integrations/shelly_em_mqtt.yaml` deployed at 18:01, sensors `sensor.shellyem_grid_power_mqtt` / `sensor.shellyem_aerotherm_power_mqtt` came up and tracked the device (33 rows / 3 min). File **deleted** — MQTT is off, so they would sit `unavailable` forever |
| **3** Cutover (rename + disable native, deploy final YAML) | ⛔ **Never executed** — blocked by the reboot loop. The native entities were never renamed, so the canonical IDs are untouched |
| **4** Post-cutover verification | ⛔ N/A. Replaced by incident verification: entry `loaded`, 14 entities live, energy counters monotonic, Prometheus label sets unchanged |
| **5** Docs | ✅ This file, the new CoIoT-disabled troubleshooting doc, and corrections to the CoIoT push doc, `energy-baseline-production-execution.md` and `battery-optimizer.md`. 5.1 (`docs/deployment/shelly-em-mqtt.md`) and 5.5 (mosquitto alert bump) are moot |
| **6** Deferred | Unchanged: dedicated mosquitto user + ACL, 15 s scrape, CoIoT unicast for repair hygiene |

### Reference: the HA-side YAML that was never deployed

Kept in case MQTT becomes viable. It depends on TODO-3.1 (renaming the native entities out of the
way **first** — a disabled entity still blocks its ID, see `helpers/entity_registry.py:1382-1384`,
and an MQTT entity created while the ID is taken keeps its `_2` suffix forever).

```yaml
mqtt:
  sensor:
    - name: "shellyem-main Channel 1 Power"   # -> sensor.shellyem_main_channel_1_power
      unique_id: shellyem_main_channel_1_power_mqtt
      state_topic: shellies/shellyem-244CAB417668/emeter/0/power
      unit_of_measurement: W
      device_class: power
      state_class: measurement
      value_template: "{{ value | round(2) }}"
      availability_topic: shellies/shellyem-244CAB417668/online
      payload_available: "true"
      payload_not_available: "false"
    - name: "shellyem-main Channel 2 Power"   # -> sensor.shellyem_main_channel_2_power
      unique_id: shellyem_main_channel_2_power_mqtt
      state_topic: shellies/shellyem-244CAB417668/emeter/1/power
      unit_of_measurement: W
      device_class: power
      state_class: measurement
      value_template: "{{ value | round(2) }}"
      availability_topic: shellies/shellyem-244CAB417668/online
      payload_available: "true"
      payload_not_available: "false"
```

## Options going forward

Option **C (diagnose the reboot loop) is done** — see [Root cause](#root-cause-a-three-way-interaction-not-mqtt-alone).
That reorders the remaining choices:

| Option | Frequency | Risk / cost |
|---|---|---|
| **A′. Finish the MQTT cutover** *(now the front-runner)* | **0.37 s** on transients, 5 s idle floor | Requires only that CoIoT stays enabled — already mandatory. Residual risk: the clean-soak evidence is 20 min, not hours, so soak overnight first. Each device fault costs ~40 s of telemetry and HA recovers by itself while CoIoT is on |
| **B. CoIoT unicast push** | 15 s (hard floor: `coiot_update_period` is `15..65535`) | A UDP 5683 LoadBalancer Service (`192.168.193.12`), a pfSense IoT-VLAN UDP rule (**human step**), and `coiot_peer` on the device. Clears the `push_update_*` repair as a side effect. Procedure: [`home-assistant-shelly-coiot-push.md`](../docs/troubleshooting/home-assistant-shelly-coiot-push.md). Strictly worse than A′ on frequency, and it is the natural *fallback* if the soak fails |
| **A. Accept 33 s** (status quo) | 33 s | Zero. Already inside `freshness_s.grid_power: 300` with ~9× margin; the dataset uses 900 s slots from counter deltas, so 33 s is not a modelling constraint. **It only blocks a future real-time zero-export loop** |
| **D. Replace the meter** | device-dependent | A Gen2/RPC Shelly EM (or a `shellyplus2pm` on the CT circuits) speaks RPC over HTTP+WebSocket with no CoAP/CoIoT dependency at all, and its MQTT is Gen2 (different topic tree, would need a new `EntitySpec`) |

### Cutover sequence for A′ (not yet executed)

1. **Soak** the current state (MQTT on, `update_period: 30`, CoIoT on) overnight. Success criterion: no
   `shellies/shellyem-244CAB417668/online` event that I did not command, and no recorder gap >40 s.
2. Set `mqtt_update_period=5` + `/reboot`, re-verify stability for a further window.
3. Deploy the parallel MQTT sensors under **temporary** entity ids (the YAML in
   [Reference](#reference-the-ha-side-yaml-that-was-never-deployed)) and confirm the cadence in HA —
   this restarts HA, so check `GET /cit/d` answers `200` **immediately before** the sync.
4. Swap the native `shelly` sensors out and the MQTT sensors onto the production entity ids in a
   **single** commit, so `entity_id` / `friendly_name` / Prometheus labels never change (D4).
5. Re-verify the series identity contract byte-for-byte, then remove the temporary sensors from the
   entity registry (`config/entity_registry/remove`).

⚠️ Step 3 is the one that bit us last time: any HA restart while `coiot.enabled: false` drops all 14
entities for as long as it takes to notice. Verify `/cit/d` first, every time.


## Troubleshooting

| Symptom | Cause / fix |
|---|---|
| Gen1 Shelly entities all `unavailable` after an HA restart, entry in `setup_retry` | `coiot.enabled: false` → `GET /cit/d` hangs. [`shelly-gen1-coiot-disabled-breaks-ha-setup.md`](../docs/troubleshooting/shelly-gen1-coiot-disabled-breaks-ha-setup.md) |
| Device reboots every 1-5 min | MQTT on **and** CoIoT off **and** HA in `setup_retry`. Fix the CoIoT side first (`POST /settings/advanced?coiot_enable=true&coiot_update_period=15&coiot_peer=` + `/reboot`) — that alone stops the loop. Only disable MQTT if it continues with CoIoT **enabled** |
| `/debug/log` never updates / shows an old boot | `debug_enable=true` does not persist on fw 1.14.0 (accepted, reads back `false`). The endpoint is readable but frozen — useless for catching a live crash |
| `broker_clients_connected` flat across a device reboot | Same client id → mosquitto replaces the stale session, count unchanged. Use `shellies/<id>/online` as the reboot marker |
| Settings change has no effect | Every `mqtt_*` / `coiot_*` change needs `/reboot`; the POST itself never answers (`http=000`) |
| `coiot_enable=true` won't stick | Use `POST /settings/advanced`, **not** `/settings/coiot` (silently ignored on fw 1.14.0) |
| `push_update_244CAB417668` repair present | Expected with `peer: ""` — HA never receives the multicast push. `check_coiot_config()` (`components/shelly/utils.py:1070`) and the unconditional `COAP_REPLY` counter (`coordinator.py:456-462`) keep it alive. Cleared only by real unicast push (option B) or by ignoring it in the UI |
| HA token loop spamming `http.ban` warnings | Expired token. Refresh before long monitoring loops |

## Reference information

| Item | Value |
|---|---|
| Device | `shellyem-main` = `192.168.86.130` = `shellyem-main.iot.grigri`, IoT VLAN 501 |
| MQTT client id / topic prefix | `shellyem-244CAB417668` → `shellies/shellyem-244CAB417668/…` |
| Topics | `emeter/{0,1}/{power,pf,reactive_power,voltage,total,total_returned,energy,returned_energy}`, `relay/0`, `online`, `announce`, `info` |
| Broker | `mosquitto.internal.grigri.cloud:1883` = VIP `192.168.193.9` (single BGP path via prusik) |
| Broker credential | Vault `/mosquitto/user` → ExternalSecret `mosquitto-user` (ns `mosquitto`), keys `username`/`password` |
| Device credential | `pass show iot/shellyem-main` |
| HA shelly entry id | `18e809d8adef1670840ff6fbe3544c0e` |
| HA token flow | [`home-assistant-api-hacs-deploy.md`](../docs/troubleshooting/home-assistant-api-hacs-deploy.md) |
| Gen1 `/settings` params | `mqtt_enable`, `mqtt_server`, `mqtt_user`, `mqtt_pass`, `mqtt_id`, `mqtt_update_period` (0 disables), `mqtt_max_qos`, `mqtt_retain`, `mqtt_clean_session`, `mqtt_keep_alive`; `/settings/advanced`: `coiot_enable`, `coiot_update_period`, `coiot_peer` |

## Related

- [`docs/troubleshooting/shelly-gen1-coiot-disabled-breaks-ha-setup.md`](../docs/troubleshooting/shelly-gen1-coiot-disabled-breaks-ha-setup.md) — **the important finding of this attempt**
- [`docs/troubleshooting/home-assistant-shelly-coiot-push.md`](../docs/troubleshooting/home-assistant-shelly-coiot-push.md) — why CoIoT push never worked, and its 15 s floor (option B)
- [`energy-baseline-production-execution.md`](energy-baseline-production-execution.md) — the plan whose input-signal TODO this was
- [`docs/deployment/battery-optimizer.md`](../docs/deployment/battery-optimizer.md) — consumer of these entities
- [Shelly Gen1 API — MQTT support](https://shelly-api-docs.shelly.cloud/gen1/#mqtt-support), [Shelly EM: MQTT](https://shelly-api-docs.shelly.cloud/gen1/#shelly-em-mqtt)
- [HA MQTT sensor](https://www.home-assistant.io/integrations/sensor.mqtt/), [HA Prometheus integration](https://www.home-assistant.io/integrations/prometheus/)
