# Shelly EM MQTT high-frequency telemetry — implemented at 2 s

**Status:** **in production since 2026-10-27 07:34Z.** Both `sensor.shellyem_main_channel_{1,2}_power`
are now fed by MQTT at a measured **2.01 s median** (was 33 s REST polling — **16.5× faster**), with
entity IDs, friendly names, Prometheus label sets, recorder `metadata_id`s (182/186) and long-term
statistic ids (30/34) all unchanged. Verified end state in
[Device state now](#device-state-now-verified) and [Cutover result](#cutover-result-2026-10-27).

The reboot loop that first made this look impossible is *not* caused by MQTT on its own: it needs
**MQTT enabled** + **`coiot.enabled: false`** + **HA in a `setup_retry` storm** against the `/cit/d`
endpoint that hangs while CoIoT is off. Since CoIoT must stay enabled anyway, the production
configuration is the one combination that does not fault. An 11 h 14 m soak (MQTT on + CoIoT on)
followed by a 2 s soak confirmed it. See [Root cause](#root-cause-a-three-way-interaction-not-mqtt-alone).

**Correction to the earlier conclusion:** the first pass blamed MQTT because every spontaneous reboot
landed in an MQTT-on window. That was a confound — CoIoT had been disabled during the same window, and
HA had just restarted into a retry storm. Isolating the variables (matrix below) shows MQTT is a
*co-factor that removes heap margin*, not the trigger.

**Second outcome, and the reason for a new doc:** the obvious way to hand a canonical entity ID to
another platform — rename the old entity away, rename the new one in — silently **moves the recorder
history** with the old name. The cutover therefore uses remove-then-promote. Full write-up:
[`home-assistant-entity-id-handover.md`](../docs/troubleshooting/home-assistant-entity-id-handover.md).

**Third outcome:** `coiot.enabled: false` on a Gen1 Shelly breaks HA's *setup* path, so the next HA
restart drops every entity of that config entry. It cost ~48 min of grid telemetry. Full write-up:
[`shelly-gen1-coiot-disabled-breaks-ha-setup.md`](../docs/troubleshooting/shelly-gen1-coiot-disabled-breaks-ha-setup.md).

**Goal (met):** get `shellyem-main`'s grid telemetry into Home Assistant at the highest
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

### Mechanism (measurements are direct; the causal chain is inferred)

| Measurement | CoIoT **off** + HA retrying | CoIoT **on** |
|---|---|---|
| `ram_free` (of `ram_total` 51 064) | **27 224 B** | **35 368 B** |
| `GET /cit/d` | hangs, no response, no 404 | `200`, 922 B, 36 ms |
| `GET /status` during 6 concurrent hanging `/cit/d` | **no response** (2 of 3 rounds) | n/a |
| Heap low-water marks in the device log | `New heap free LWM: 25960`, `25888` | `31408` |

Every number above was observed. What is *inferred* is the step from "heap and slots are consumed" to
"the firmware resets": each stuck `/cit/d` handler appears to pin a mongoose HTTP connection slot *and*
its buffers — roughly **8 KB of heap and all the connection slots** for the duration of HA's retry
storm — leaving no margin for a large allocation (WiFi TX / MQTT publish buffer) → fault → reset. The
per-request leak is small (18 hanging requests cost ~280 B of *unrecovered* heap: 27 224 → 26 944),
which is why the device survives synthetic replay. **This mechanism was never directly proven**: no
exception dump was captured, and replaying the hanging calls by hand does not crash the device (see
[What I could not capture](#what-i-could-not-capture)). What *is* established by the isolation matrix is
the trigger condition — CoIoT off + HA retrying + MQTT on — and that re-enabling CoIoT stops the loop.
That is enough to operate safely without knowing the exact fault path.

The same inference explains the irregular reboot spacing: HA's backoff grows toward
`SETUP_RETRY_MAX_WAIT = 600 s`, and each reboot resets the heap, so the interval depends on when the
next retry burst lands.

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

Checked at 07:41Z, 2026-10-27, after the cutover:

| Setting | Value | Note |
|---|---|---|
| `mqtt.enable` | **`true`** | `server: mosquitto.internal.grigri.cloud:1883`, **`update_period: 2`**, `keep_alive: 60`, `max_qos: 0`, `clean_session: true`, `retain: false` |
| `mqtt.connected` | `true` | client id `shellyem-244CAB417668` |
| `coiot` | `{"enabled": true, "update_period": 15, "peer": ""}` | **must stay enabled** — it is both the HA setup dependency and what keeps MQTT safe |
| `cloud` | `false` | untouched |
| `fw` | `1.14.0-gcb84623` | unchanged — the beta was never flashed |
| `GET /cit/d` | `200`, 922 bytes in 33 ms | HA setup dependency satisfied; re-checked immediately before every HA restart |
| `ram_free` | 35 848 B of 51 064 B | healthy at `update_period: 2`; ~8 KB lower only while stuck `/cit/d` handlers are in flight |
| `uptime` | 1 347 s (boot 07:18:22Z, the commanded reboot that activated `update_period: 2`) | no spontaneous reboot |
| HA `shelly` entry | `loaded`, 14 entities live, 0 unavailable | survived the ConfigMap-driven restart at 07:32:56Z |
| Power entities | platform **`mqtt`**, canonical IDs | native ones recreated as `..._power_2` and disabled |
| Recorder | `metadata_id` 182/186, median **2.01 s** | 26 470 pre-cutover rows still under the same id |
| Long-term statistics | statistic ids **30**/34, unit W | 2 918 short-term buckets before, and the 07:35 bucket compiled from MQTT |
| Prometheus | 6 series, label sets **byte-identical** to the pre-cutover baseline | `instance` excluded per `VOLATILE_IDENTITY_LABELS` |

**Rollback** has two independent layers. To stop MQTT publishing only (device side):

```bash
SUSER=$(pass show iot/shellyem-main | sed -n 's/^login: //p'); SPASS=$(pass show iot/shellyem-main | head -1)
curl -s -u "$SUSER:$SPASS" -X POST -G http://shellyem-main.iot.grigri/settings \
  --data-urlencode mqtt_enable=false -o /dev/null          # never answers: expect curl (28)
curl -s -u "$SUSER:$SPASS" http://shellyem-main.iot.grigri/reboot >/dev/null
```

To restore the native entities as the canonical power sensors, revert
`apps/home-assistant/config/integrations/shelly_em_mqtt.yaml`, let HA restart, then run
`ansible-playbook playbooks/shelly-em.yaml -i localhost, --limit localhost --tags registry \
-e shelly_em_registry_action=rollback`. Do **not** rename the MQTT entities off the canonical IDs —
that would drag the history with them
([why](../docs/troubleshooting/home-assistant-entity-id-handover.md#rollback)).

⚠️ **Do not disable CoIoT on this device again** — it breaks HA's setup path *and* it is the co-factor
in the reboot loop. Do not restart HA without first checking `GET /cit/d` answers.


## Decisions (2026-10-26)

D1/D9/D2/D3/D5 were the design that the (mis-diagnosed) blocker appeared to invalidate. Root-causing
it **reinstates** them: they are gated on the soak, not on a hardware problem.
D4, D6, D7, D8 remain valid regardless of transport.

| # | Decision | Choice | Status |
|---|---|---|---|
| D1 | Transport | **MQTT** — the device republishes the whole emeter block on change (measured **0.37 s** apart under load) plus a periodic floor | **Implemented** — the reboot loop was the CoIoT-off/retry-storm interaction, not MQTT |
| D9 | `mqtt_update_period` | ~~5 s~~ → **2 s** — the owner asked for the 2 s goal; measured **median 2.009 s, p95 2.012 s, max 2.128 s over 180 s** with zero gaps >3 s, and heap unchanged (35 848 B) | **Implemented** |
| D2 | Broker credential | Reuse the shared mosquitto user (Vault `/mosquitto/user`) | **Implemented** — confirmed working end to end |
| D3 | Scope | Partial takeover — only `channel_{1,2}_power` move to MQTT; energy counters, cost entity, voltage, power factor, relay and reboot button stay native | **Implemented** |
| D4 | Naming | Identical `entity_id` and `friendly_name`, so `hass_sensor_power_w{...}` stays byte-identical | **Implemented and diffed** — see [Series identity contract](#series-identity-contract-why-d4-works) |
| D5 | Recorder | Record both channels | **Implemented, cost re-measured**: +81 435 rows/day (**+40 %** on 202 240/day), ≈ +520 MB at the observed 628 B/row over 10.17-day retention → DB 1.69 → ~2.2 GB of a 6 Gi PVC. Long-term statistics are unaffected (still 5-min buckets). Lever if it ever hurts: drop channel 2 from the recorder, or shorten `purge_keep_days` |
| D6 | Prometheus scrape | Unchanged at 60 s — the dataset builds 900 s slots from counter deltas and `build.py` never uses instantaneous power | **Valid** |
| D7 | CoIoT / repair | ~~Leave `coiot` disabled~~ → **CoIoT must stay enabled** — doubly mandatory now: it is HA's setup dependency *and* the reboot loop's co-factor. The `push_update_244CAB417668` repair was **not** ignored — it is left visible as a genuine signal | **Revised** |
| D8 | pfSense | No change — IoT-VLAN TCP to the mosquitto VIP already passes | **Valid** (and confirmed: the device connected through the VIP) |
| D10 | Entity handover | ~~Rename native away, rename MQTT in~~ → **remove-then-promote**, so the recorder's rename migration never runs and the collision guard preserves canonical history | **Revised during implementation** — see [`home-assistant-entity-id-handover.md`](../docs/troubleshooting/home-assistant-entity-id-handover.md) |


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
| `mqtt_update_period=2` (**production**) | 96 messages/channel in 180 s: median **2.009 s**, p95 2.012 s, max 2.128 s, min 0.005 s (on change), **zero gaps >3 s**. In HA's recorder: median 2.01 s, max 2.83 s (ch1) / 4.01 s (ch2) |
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
| `series_key` | sha256 of the label set minus `VOLATILE_IDENTITY_LABELS = {instance, pod_ip}` | `home-energy-optimizer/src/battery_optimizer/extract_prom.py:147` |

`pod` is stable (StatefulSet), `instance` is stripped → **the Parquet archive continues unbroken**.
The recorder's `states_meta` is keyed by the `entity_id` string (ch1 power = `metadata_id` 182,
ch2 = 186), so new rows append under the same id → **HA history and dashboards continue too**.

`name: "shellyem-main Channel 1 Power"` yields both the friendly_name and, via slugify,
`sensor.shellyem_main_channel_1_power`.

This contract is transport-agnostic: it is what any future takeover (MQTT, ESPHome, a replacement
device) must satisfy to avoid touching the consumers.

### Consumers that must not break

- `home-energy-optimizer/src/.../extract_prom.py` — `ENTITY_SPECS` / `DEFAULT_ENTITY_IDS` hardcode
  `sensor.shellyem_main_channel_1_{power,energy,energy_returned}`
- `apps/battery-optimizer/files/service.yaml` + the app repo's `config/service.yaml` — `grid_power`,
  `grid_energy`, `observed`
- `apps/home-assistant/dashboards/iot-energy.json` — 11 refs to ch1 energy, 2 to ch1 power
- ADR-0003 in the app repo — ch1 = whole-home feed, ch2 = aerotherm branch (owner-confirmed wiring)

With D4, **none of them needed a change** — and none was touched. The cutover preserved every ID,
label and statistic, so all four consumers kept working through it.

## What was executed

| TODO | Outcome |
|---|---|
| **1** Enable MQTT + measure | ✅ Done 2026-10-26, rolled back once over the (mis-diagnosed) reboot loop, then re-enabled and soaked 11 h 14 m clean |
| **2** Probe entities | ✅ Done 2026-10-27 07:33Z. `apps/home-assistant/config/integrations/shelly_em_mqtt.yaml` deployed under temporary IDs `sensor.shellyem_main_channel_{1,2}_power_mqtt_probe`; recorder confirmed median **2.01 s** on both before anything production was touched |
| **3** Cutover | ✅ Done 07:34:30Z via `scripts/shelly-em-registry.py promote` — **remove-then-promote**, not rename (D10). Native entries removed, MQTT promoted onto the canonical IDs, shelly entry reloaded, recreated `..._power_2` natives disabled |
| **4** Post-cutover verification | ✅ See [Cutover result](#cutover-result-2026-10-27) |
| **5** Docs | ✅ This file, [`home-assistant-entity-id-handover.md`](../docs/troubleshooting/home-assistant-entity-id-handover.md) (new), the CoIoT-disabled doc, the CoIoT push doc, `INDEX.md`, `AGENTS.md`, `energy-baseline-production-execution.md`, `battery-optimizer.md`. 5.5 (mosquitto alert bump) still moot |
| **6** Deferred | Unchanged: dedicated mosquitto user + ACL (and rotating the shared user, whose username leaked into two transcripts), 15 s Prometheus scrape, CoIoT unicast for `push_update_*` repair hygiene |

### Deployed HA-side YAML

`apps/home-assistant/config/integrations/shelly_em_mqtt.yaml` (GitOps: any change restarts HA, so
check `GET /cit/d` first). Differences from the original sketch:

- `default_entity_id` instead of relying on `name` slugification — the temporary probe ID applies only
  at first creation, so the promoted canonical ID survives restarts.
- `expire_after: 15` instead of `availability_topic` — with a 2 s stream, no payload for 15 s means
  seven missed publishes, so the sensor goes `unavailable` rather than serving a stale grid value to a
  control loop. The device's `online` LWT is still the right thing to watch for reboots.
- No `force_update`: at 2 s the values genuinely change, so the recorder writes anyway.

```yaml
mqtt:
  sensor:
    - name: "shellyem-main Channel 1 Power"   # friendly_name must match the native one exactly
      default_entity_id: sensor.shellyem_main_channel_1_power_mqtt_probe
      unique_id: shellyem_main_channel_1_power_mqtt
      state_topic: shellies/shellyem-244CAB417668/emeter/0/power
      unit_of_measurement: W
      device_class: power
      state_class: measurement
      value_template: "{{ value | round(2) }}"
      expire_after: 15
    - name: "shellyem-main Channel 2 Power"
      default_entity_id: sensor.shellyem_main_channel_2_power_mqtt_probe
      unique_id: shellyem_main_channel_2_power_mqtt
      state_topic: shellies/shellyem-244CAB417668/emeter/1/power
      unit_of_measurement: W
      device_class: power
      state_class: measurement
      value_template: "{{ value | round(2) }}"
      expire_after: 15
```

## Options — resolved

Option **A′ (finish the MQTT cutover) was implemented on 2026-10-27**, after an 11 h 14 m clean soak
at `update_period: 30` and a 2 s cadence measurement. The others are no longer needed for frequency:

| Option | Frequency | Status |
|---|---|---|
| **A′. MQTT cutover** | **2.01 s** periodic, 0.37 s on transients | ✅ **Implemented** — the production path |
| **B. CoIoT unicast push** | 15 s (hard floor) | Not needed for frequency. Still the only way to clear the `push_update_244CAB417668` repair, and it needs a pfSense UDP rule (**human step**). Procedure: [`home-assistant-shelly-coiot-push.md`](../docs/troubleshooting/home-assistant-shelly-coiot-push.md) |
| **A. Accept 33 s** | 33 s | Superseded |
| **D. Replace the meter** | device-dependent | Not needed. Note the unit reports `hw_revision: "dev-prototype"`, so a retail Gen2/RPC replacement remains the escape hatch if this one misbehaves |
| **C. Diagnose the reboot loop** | — | ✅ Done first, and it is what made A′ safe — see [Root cause](#root-cause-a-three-way-interaction-not-mqtt-alone). Firmware was **never** upgraded, per the owner's constraint |

### Cutover result (2026-10-27)

| Time (UTC) | Step | Evidence |
|---|---|---|
| 07:18:22 | `mqtt_update_period=2` applied by the scoped Ansible playbook + one commanded reboot | `/settings` reads `update_period: 2`; playbook refuses to run unless CoIoT and MQTT are already enabled, and waits for `/cit/d` 200 after the reboot |
| 07:19–07:22 | Broker-side cadence capture, 180 s | 96 messages/channel: median **2.009 s**, p95 2.012 s, max 2.128 s, **zero gaps >3 s** |
| 07:32:56 | HA restarted by Reloader after the ConfigMap sync (probe YAML) | Pre-flight `GET /cit/d` = **200**, `coiot.enabled` = true; entry came back `loaded`, no shelly/MQTT errors in the log |
| 07:33:29 | Probes live under temporary IDs | `friendly_name` already exactly `shellyem-main Channel N Power`, unit W, `state_class: measurement`; recorder median **2.01 s** (min 1.27, max 2.75) |
| 07:34:30 | `promote` — remove native, promote MQTT, reload shelly, disable recreated `..._power_2` | Preflight asserted both probes numeric, in W, at the exact friendly_name, and **0.6 s** stale |
| 07:35 → 07:41 | Post-cutover verification | Table below |

| Check | Result |
|---|---|
| Recorder `metadata_id` | Still **182** / **186**; 26 470 pre-cutover rows intact, post-cutover rows appending to the same id |
| Recorder cadence | median **2.01 s**, max 2.83 s (ch1) / 4.01 s (ch2) over 6 min; the only >30 s interval is the handover instant itself |
| Long-term statistics | statistic ids **30** / **34** unchanged, unit W; the **07:35** bucket (mean 476.9 W) compiled from MQTT, continuous with 07:20/07:25/07:30 |
| Prometheus | 6 series before and after; label sets **byte-identical** (diffed with `instance` excluded per `VOLATILE_IDENTITY_LABELS`) |
| Entity set | 14 live `shellyem_main` entities, **0 unavailable** — same set as before; disabled `..._power_2` natives are absent from the state machine, so they are neither recorded nor exported |
| HA repairs | Only the pre-existing `push_update_244CAB417668`; no unit-change or statistics repairs |
| battery-optimizer | `battery_optimizer_ready=1`; **`input_age_seconds{role="grid_power"} = 2.3 s`**, down from 31.9 s — a 14× fresher control input, against a `freshness_s` budget of 300 |
| All 12 `not_ready_reason` gauges | 0 |
| Device | uptime monotonic from the 07:18:22Z boot (no spontaneous reboot), `ram_free` 35 848 B at `update_period: 2` |
| Expected log noise | Two `Cannot rename statistic_id ... already exists` ERRORs — the collision guard preserving history. Their *absence* would mean history moved |

**Reboot detector going forward:** device `uptime` versus elapsed wall time. `broker_clients_connected`
cannot see a same-client-id reconnect, and nothing records the `online` topic.

⚠️ Any future HA restart on this device re-runs Gen1 `initialize()`. Verify `GET /cit/d` returns `200`
**immediately before** the sync — that is the failure that cost 48 min of telemetry on 2026-10-26.

### The old 43–66 s recorder gaps: attributed, and now gone

An unresolved question from the soak was four ch1 gaps >40 s (43, 43, 66, 43 s) that `uptime` proved
were **not** reboots. They are now explained, and they were the strongest argument for moving off
polling:

- **The 43 s gaps are REST poll timeouts.** At each one, *all* five ch1 native entities
  (`power`, `energy`, `energy_returned`, `voltage`, `power_factor`) flip to `unavailable` at the **same
  timestamp**, i.e. a coordinator-wide `/status` failure — not an entity glitch. 43 s is exactly
  `33 s` poll interval + `DEVICE_IO_TIMEOUT = 10 s`: the poll at `T+33` starts, times out after 10 s, and
  the whole block is marked unavailable at `T+43`.
- **It is chronic and pre-existing, not caused by MQTT.** `metadata_id` 182 accumulated **198
  `unavailable` rows over 10.1 days ≈ 19.7/day** (daily range 13–40), with counts from 2026-10-19
  onward — before MQTT existed on this device. ~1 % of the day, ~14 min/day, on the signal a control
  loop would read.
- **The 66 s gap at 04:31:26** is two poll intervals with a changed value: one poll produced no state
  change (identical float), or was deduped by the recorder. Benign.

**After the cutover this failure mode no longer touches the grid signal.** In the first 27 min the
MQTT-fed `metadata_id` 182/186 recorded **843/834 rows with zero `unavailable` and zero gaps >10 s**
(max 2.83 s / 4.33 s). MQTT is a persistent broker connection rather than a per-request HTTP poll to
the device, so a slow `/status` no longer costs a sample — this is a **reliability** gain independent of
the 16.5× frequency gain.

**Residual weakness:** the energy counters are still native, so they still take ~20 poll timeouts/day.
Those appear in the Prometheus archive as missing 60 s samples (the 2026-10-27 03:30Z run over a 3-day
window: 4420 of ~4526 expected samples, i.e. 106 min including the 48 min incident) and the run still
reported `violations=0` / `Completed`. If that ever starts failing the coverage check, the fix is to
push the counters over MQTT too — but they are consumed as 900 s deltas, so it has not been worth the
recorder cost yet.


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
| A few `unavailable` rows per hour on the *native* Shelly entities, ~20/day | `/status` REST poll timeout — the whole block flips together at `T + 33 s + DEVICE_IO_TIMEOUT (10 s)`. Chronic device/WiFi behaviour (rssi −61 dBm) that predates MQTT. The two power sensors are now immune because they come over MQTT. See [the old 43–66 s recorder gaps](#the-old-4366-s-recorder-gaps-attributed-and-now-gone) |
| `Cannot rename statistic_id ... because the new statistic_id already exists` ERROR during the handover | **The collision guard working** — it is what keeps long-term statistics under the canonical ID. Its absence would mean history moved. [`home-assistant-entity-id-handover.md`](../docs/troubleshooting/home-assistant-entity-id-handover.md) |
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

- [`docs/troubleshooting/home-assistant-entity-id-handover.md`](../docs/troubleshooting/home-assistant-entity-id-handover.md) — **the reusable technique this cutover needed**: renaming an entity moves its recorder history, so hand IDs over with remove-then-promote
- [`docs/troubleshooting/shelly-gen1-coiot-disabled-breaks-ha-setup.md`](../docs/troubleshooting/shelly-gen1-coiot-disabled-breaks-ha-setup.md) — the landmine that caused the 48 min outage and the reboot loop
- [`docs/troubleshooting/home-assistant-shelly-coiot-push.md`](../docs/troubleshooting/home-assistant-shelly-coiot-push.md) — why CoIoT push never worked, its 15 s floor, and why it is now moot for frequency
- [`energy-baseline-production-execution.md`](energy-baseline-production-execution.md) — the plan whose input-signal TODO this was
- [`docs/deployment/battery-optimizer.md`](../docs/deployment/battery-optimizer.md) — consumer of these entities
- Automation: `scripts/shelly-em-registry.py` (inspect / promote / rollback) driven by
  `metal/playbooks/shelly-em.yaml` (`--tags device` for `mqtt_update_period`, `--tags registry` for the handover)
- [Shelly Gen1 API — MQTT support](https://shelly-api-docs.shelly.cloud/gen1/#mqtt-support), [Shelly EM: MQTT](https://shelly-api-docs.shelly.cloud/gen1/#shelly-em-mqtt)
- [HA MQTT sensor](https://www.home-assistant.io/integrations/sensor.mqtt/), [HA Prometheus integration](https://www.home-assistant.io/integrations/prometheus/)
