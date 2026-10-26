# Shelly EM MQTT high-frequency telemetry — implementation spec

**Status:** in execution 2026-10-26. TODO-1 done and measured; the measurements below **supersede the
original 1 Hz assumption** — read "Measured MQTT behaviour" before anything else.

**Goal:** get `shellyem-main`'s grid telemetry into Home Assistant at the highest frequency the
device can produce (~1 Hz) so a future battery integration (omnibattery) can run a zero-export /
power-limiting control loop on it — **without changing a single entity ID, friendly name or
Prometheus label set**.

**Supersedes** the CoIoT unicast TODO in
[`energy-baseline-production-execution.md`](energy-baseline-production-execution.md): Gen1 CoIoT is
capped at `coiot_update_period: 15..65535 s`, so HA can never do better than `2.2 × 15 s = 33 s` on
that transport. MQTT is the only sub-15 s path. See
[`home-assistant-shelly-coiot-push.md`](../docs/troubleshooting/home-assistant-shelly-coiot-push.md).

## Decisions (2026-10-26)

| # | Decision | Choice | Rejected alternative |
|---|---|---|---|
| D1 | Transport | **MQTT** — the device republishes the whole emeter block on change (measured **0.37 s** apart under load) plus a periodic floor | CoIoT unicast — hard 15 s floor, needs a new Service + VIP + pfSense UDP rule |
| D9 | `mqtt_update_period` | **5 s** — bounds idle staleness at 5 s (60× margin under `freshness_s.grid_power: 300`) while transients still arrive on change | `1` — 5× the recorder rows for no control benefit, since on-change already covers transients; `0` — removes the staleness guarantee entirely |
| D2 | Broker credential | **Reuse the shared mosquitto user** (Vault `/mosquitto/user`) | Dedicated user + ACL — deferred (TODO-6.1). Accepted risk: mosquitto has no `acl_file`, so the device can read all telemetry and publish to any command topic |
| D3 | Scope | **Partial takeover** — only `channel_1_power` and `channel_2_power` move to MQTT | Full migration (delete the `shelly` config entry) — Gen1 MQTT has no `power_factor`, no overpower boolean and no reboot command, and it would orphan the device entry and the Energy Dashboard cost entity |
| D4 | Naming | **Identical `entity_id` and `friendly_name`** to the native entities, so `hass_sensor_power_w{...}` is byte-identical | New `_mqtt` IDs — would require changes in `extract_prom.py`, `service.yaml`, ADR-0003 and `iot-energy.json` |
| D5 | Recorder | **Record both channels** (upper bound ~+34.5 k rows/day, +18 % on today's 188 733/day; change-driven, so a quiet house writes far fewer) | Grid only; or exclude both from the recorder (no HA history) |
| D6 | Prometheus scrape | **Unchanged at 60 s** | 15 s — the dataset builds 900 s slots from counter deltas and `build.py` never uses instantaneous power, so 60 s is already 15× oversampled |
| D7 | CoIoT / repair | Leave `coiot` disabled on the device; **ignore** `push_update_244CAB417668` in the HA UI (human, one click); close the CoIoT TODO | Implement CoIoT unicast anyway — only buys repair hygiene once MQTT delivers 1 Hz |
| D8 | pfSense | **No change** — IoT-VLAN TCP to the mosquitto VIP already passes | (CoIoT would have needed a new UDP 5683 rule) |

## Context and current state

All read-only, verified 2026-10-26.

### Device

| Item | Value | Source |
|---|---|---|
| Device | `shellyem-main`, Shelly EM (`SHEM`), Gen1, fw `1.14.0-gcb84623`, MAC `244CAB417668` | `/shelly`, `/settings` |
| Address | `192.168.86.130` = `shellyem-main.iot.grigri`, IoT VLAN 501 | pfSense DHCP/ARP |
| `device.report_period` | **1 s** | `/settings` |
| `coiot` | `{"enabled": false, "update_period": 15, "peer": ""}` — **drifted** from the CoIoT doc, which recorded `enabled: true` | `/settings` |
| `mqtt` | `{"enable": false, "server": "192.168.33.3:1883", "user": "", "id": "shellyem-244CAB417668", "update_period": 30, "max_qos": 0, "retain": false, "clean_session": true, "keep_alive": 60}` — stale server from a previous network | `/settings` |
| `cloud` | disabled | `/settings` |
| Credentials | `pass show iot/shellyem-main` (first line = password, `login:` line = username) | — |

### Home Assistant

| Item | Value |
|---|---|
| Version / pod | 2026.10.4, `home-assistant-hass-0` = `10.0.4.218` on `prusik` |
| `shelly` config entry | gen 1, host `shellyem-main.iot.grigri`, user `agil`, enabled |
| `mqtt` config entry | `mosquitto.internal.grigri.cloud:1883`, protocol 5, enabled |
| YAML config | 100 % GitOps: `/config/configuration.yaml` is `!include gitops/configuration.yaml`; packages come from `apps/home-assistant/config/integrations/` via ConfigMap `home-assistant-config-integrations` |
| Reload behaviour | Reloader (`reloader.stakater.com/auto: "true"`) restarts the HA pod on ConfigMap change; `mqtt.reload` exists as an admin service but the restart happens anyway |
| Current cadence | ~33 s (`2.2 × 15`), verified in `/api/history/period`; one `unavailable` blip at 16:58:57Z |

### Broker

| Item | Value |
|---|---|
| Service | `mosquitto` LoadBalancer, VIP `192.168.193.9`, ports 1883/8083/9234, `externalTrafficPolicy: Local` |
| BGP | `192.168.193.9/32` has **one** path, from `prusik` (`192.168.192.3`) → `Local` is safe |
| DNS | pfSense unbound resolves `mosquitto.internal.grigri.cloud` → `192.168.193.9` |
| Config | `allow_anonymous false`, `password_file`, persistence on, **no `acl_file`** |
| Clients | `broker_clients_connected = 6` (HASS, tanque, valetudopng, goHeishamon, exporter +1) |
| IoT reachability | pf state table: `192.168.86.100` and `192.168.86.200` → `192.168.193.9:1883` ESTABLISHED. The IoT catch-all is TCP-only (`flags S/SA` without `proto`) — it blocks CoIoT's UDP but **passes MQTT** |

### Measured MQTT behaviour (TODO-1, 2026-10-26)

Measured live on the device after enabling MQTT. **This overturns the "1 Hz" assumption the plan was
written with** — the transport is change-driven, not clock-driven.

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
| **Reboot required** | A `mqtt_*` settings change is stored but **not applied**: after changing `mqtt_update_period` without a reboot the device reported `mqtt.connected: false` and published nothing. After `/reboot` it published immediately at the new period |
| Settings POST | The device accepts the change but does not answer — `curl` times out with no body. Verify with a follow-up `GET /settings`, do not treat the timeout as a failure |

**Consequences**

- The effective maximum frequency is the device's own change detection (≥2.7 Hz observed), *not*
  `mqtt_update_period`. `update_period` only bounds how stale the value can get when the load is
  static. Hence D9 = 5 s.
- Recorder volume is driven by real load movement, not by a fixed 1 Hz clock — the budget below is an
  upper bound, and a quiet house writes far fewer rows.
- `total` (Wh) is published on the same cycle, but it stays on the native integration: converting it
  to kWh in a template would duplicate `sensor.shellyem_main_channel_1_energy` and orphan the Energy
  Dashboard cost entity (`unique_id` `f33de95b01d7a48b13f263b9c020d09a_grid_cost`).

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
The recorder's `states_meta` is keyed by the `entity_id` string, so new rows append under the same
`metadata_id` → **HA history and dashboards continue too**.

`name: "shellyem-main Channel 1 Power"` yields both the friendly_name and, via slugify,
`sensor.shellyem_main_channel_1_power`.

### Consumers that must not break

- `battery-optimizer/src/.../extract_prom.py` — `ENTITY_SPECS` / `DEFAULT_ENTITY_IDS` hardcode
  `sensor.shellyem_main_channel_1_{power,energy,energy_returned}`
- `apps/battery-optimizer/files/service.yaml` + the app repo's `config/service.yaml` — `grid_power`,
  `grid_energy`, `observed`
- `apps/home-assistant/dashboards/iot-energy.json` — 11 refs to ch1 energy, 2 to ch1 power
- ADR-0003 in the app repo — ch1 = whole-home feed, ch2 = aerotherm branch (owner-confirmed wiring)

With D4, **none of them needs a change**.

### Budget

| Resource | Today | After (upper bound, `update_period=5`) | Limit |
|---|---|---|---|
| Recorder rows/day (all entities) | 188 733 | ≤ ~223 000 (+18 %) | — |
| Recorder DB (logical / physical) | 1687 MB / 684 MB | ≤ ~2.0 GB / ~810 MB | 6 Gi PVC, 5.1 GB free |
| Long-term Prometheus | 1.3 GB, HA job = 2993 series @60 s | unchanged (D6) | 200 GB |
| mosquitto | 6 clients | 7 clients, ≤ ~2.4 msg/s | — |

## Implementation plan

### TODO-1 — enable MQTT on the device and measure ✅ done 2026-10-26

Impact: device reboots (~40 s offline; relay state and the flash-persisted `total` counters survive).
No cluster or firewall change. Rollback: `mqtt_enable=false` + reboot.

- [x] **1.1** Configure and reboot:

```bash
SUSER=$(pass show iot/shellyem-main | sed -n 's/^login: //p')
SPASS=$(pass show iot/shellyem-main | head -1)
MQTT_USER=$(kubectl --context=grigri -n mosquitto get secret mosquitto-user -o jsonpath='{.data.username}' | base64 -d)
MQTT_PASS=$(kubectl --context=grigri -n mosquitto get secret mosquitto-user -o jsonpath='{.data.password}' | base64 -d)
D=http://shellyem-main.iot.grigri

# The POST is applied but NOT answered: curl times out with an empty body. That is normal.
curl -sS --max-time 20 -u "$SUSER:$SPASS" -X POST -G "$D/settings" \
  --data-urlencode mqtt_enable=true \
  --data-urlencode mqtt_server=mosquitto.internal.grigri.cloud:1883 \
  --data-urlencode mqtt_user="$MQTT_USER" \
  --data-urlencode mqtt_pass="$MQTT_PASS" \
  --data-urlencode mqtt_update_period=5 \
  --data-urlencode mqtt_max_qos=0 \
  --data-urlencode mqtt_retain=false
curl -sS --max-time 10 -u "$SUSER:$SPASS" "$D/reboot"   # mandatory: settings are not applied without it
sleep 40
curl -sS --max-time 8 -u "$SUSER:$SPASS" "$D/settings" | jq -c '.mqtt | del(.user)'
curl -sS --max-time 8 -u "$SUSER:$SPASS" "$D/status"   | jq -c '.mqtt'   # -> {"connected":true}
unset SUSER SPASS MQTT_USER MQTT_PASS
```

Never echo or commit the credentials. Gen1 has no MQTT TLS — accepted (IoT VLAN, internal broker).

- [x] **1.2** Measure the real per-topic cadence — results in
      [Measured MQTT behaviour](#measured-mqtt-behaviour-todo-1-2026-10-26). Summary: change-driven
      (0.37 s minimum observed) with a hard `mqtt_update_period` floor; whole emeter block published
      per cycle; `power` payloads carry 2 decimals, `total` is Wh with 1 decimal.

```bash
printf '%s\n%s\n' "$MQTT_USER" "$MQTT_PASS" \
  | timeout 80 kubectl --context=grigri -n mosquitto exec -i sts/mosquitto -c mosquitto -- \
    sh -c 'read U; read P; exec mosquitto_sub -h 127.0.0.1 -u "$U" -P "$P" \
      -t "shellies/shellyem-244CAB417668/emeter/0/power" -F "%U %p" -W 60' \
  | awk 'NR>1{printf "%.2f ", $1-p} {p=$1} END{print ""}'
```

- [x] **1.3** `broker_clients_connected` 6 → **7**; device `mqtt.connected: true`.

### TODO-2 — probe entities (GitOps, no naming risk)

- [ ] **2.1** Add `apps/home-assistant/config/integrations/shelly_em_mqtt.yaml` with **probe** names
      (`ShellyEM Grid Power MQTT` / `ShellyEM Aerotherm Power MQTT`) so they cannot collide with the
      canonical IDs.
- [ ] **2.2** Validate and deploy: `helm dependency build apps/home-assistant/`,
      `helm template --include-crds --namespace home-assistant home-assistant apps/home-assistant/`,
      `pre-commit run --all-files`, commit, push, let ArgoCD sync (HA restarts, ~1 min).
- [ ] **2.3** Verify: probe updates ~1 Hz; **values agree with the native entities** (this also
      confirms `emeter/0` = channel 1, `emeter/1` = channel 2); adjust `value_template` precision to
      match the native 2-decimal state if TODO-1.2 shows more decimals.
- [ ] **2.4** Remove the probe block (keep the file), so the registry holds no stale entry.

### TODO-3 — cutover (the only delicate step)

Order matters. If an MQTT entity is created while the canonical ID is taken it gets a `_2` suffix
and **keeps it** (the registry pins `entity_id` to `unique_id`), even after the ID is freed.

- [ ] **3.1** (human or agent via the websocket API, with confirmation) In **Settings → Devices &
      services → shellyem-main**, rename `sensor.shellyem_main_channel_1_power` →
      `sensor.shellyem_main_channel_1_power_coiot` and **disable** it. Same for channel 2.
      Disabling alone is not enough: conflicts are checked against *registered* entities including
      disabled ones (`helpers/entity_registry.py:1382-1384`).
- [ ] **3.2** Replace the probe block with the final sensors (canonical `name:` + `unique_id`),
      validate, commit, push. Reloader restarts HA; the new unique_ids are unknown to the registry,
      so entity IDs are generated from the names and claim the canonical slots. Gap ≈ the restart.
- [ ] **3.3** (human) Ignore the `push_update_244CAB417668` repair in the HA UI. Ignoring is
      persisted (`helpers/issue_registry.py`, storage key `repairs.issue_registry`), so it survives
      restarts even though the issue itself is `is_persistent: false`.

Final YAML:

```yaml
# Shelly EM (Gen1) grid telemetry at ~1 Hz over MQTT.
#
# These two sensors TAKE OVER the entity IDs previously owned by the native `shelly`
# integration (renamed to *_coiot and disabled there), so entity_id, friendly_name,
# device_class and unit — and therefore the Prometheus series
# hass_sensor_power_w{entity,friendly_name,...} — are byte-identical to before.
# battery-optimizer's series_key strips only {instance,pod_ip}, so the Parquet archive
# continues unbroken; the recorder's states_meta is keyed by entity_id, so history does too.
#
# Why MQTT: Gen1 CoIoT is capped at 15 s (coiot_update_period 15..65535) and HA polls at
# 2.2x that = 33 s. The EM publishes emeter topics on change at its 1 s report_period.
#
# Everything else (energy counters, Energy Dashboard cost entity, voltage, power_factor,
# relay switch, reboot button, device entry) stays on the native integration: Gen1 MQTT
# publishes no power_factor, no overpower boolean and no reboot command.
#
# Do NOT rename these entities or the shellyem-main device: friendly_name is a Prometheus
# label and part of the archive's series_key.
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

### TODO-4 — post-cutover verification

- [ ] **4.1** Entity IDs and friendly names unchanged:
      `GET /api/states/sensor.shellyem_main_channel_1_power` → `attributes.friendly_name` =
      `shellyem-main Channel 1 Power`.
- [ ] **4.2** Cadence ~1 Hz over a 5-minute `/api/history/period` window.
- [ ] **4.3** Prometheus label set identical — diff before/after:
      `hass_sensor_power_w{entity=~"sensor.shellyem_main_channel_[12]_power"}` (expect the same
      `domain/entity/friendly_name/job/namespace/service/endpoint/pod`; `instance` may differ).
- [ ] **4.4** Recorder continuity: rows for the entity exist on both sides of the cutover
      timestamp, under one `metadata_id`.
- [ ] **4.5** Recorder growth in line with the budget table; HA Postgres PVC still has headroom.
- [ ] **4.6** `battery-optimizer` service healthy: `battery_optimizer_ready == 1`, no
      `GridPowerStale`, no new `HomeAssistantDisconnected`. Note the live service now sees ~1
      `state_changed` event/s for `grid_power` over its WebSocket (read-only, allowlisted).
- [ ] **4.7** Next pipeline run succeeds with no new coverage gaps.
- [ ] **4.8** Grafana "IoT - Energy" (`apps/home-assistant/dashboards/iot-energy.json`) still plots
      ch1/ch2 power.

### TODO-5 — docs and cleanup

- [ ] **5.1** Move this spec's durable content to `docs/deployment/shelly-em-mqtt.md` (or fold into
      the existing HA docs) once implemented.
- [ ] **5.2** `docs/troubleshooting/home-assistant-shelly-coiot-push.md` — mark superseded for
      resolution (done in the same commit as this spec); record the `coiot.enabled: false` drift.
- [ ] **5.3** `planning/energy-baseline-production-execution.md` — replace the CoIoT TODO section
      with a pointer here (done in the same commit as this spec).
- [ ] **5.4** `docs/deployment/battery-optimizer.md` — update the "grid signals come from a polled
      Gen1 Shelly" gotcha.
- [ ] **5.5** `apps/mosquitto/rules/custom.yaml` — `MQTTMissingClient` still expects ≥5 clients;
      optionally raise to 6 and add `shellyem-main` to the description.

### TODO-6 — deferred / optional

- [ ] **6.1** Dedicated mosquitto user + ACL scoped to `shellies/shellyem-244CAB417668/#` (rw) and
      `shellies/command` (read), with a full-access block for the existing shared user. Needs an
      `apps/mosquitto/` ExternalSecret + ConfigMap + values change, one Vault write (human) and a
      broker restart. Rejected for now (D2); revisit if the IoT VLAN's blast radius matters more.
- [ ] **6.2** HA ServiceMonitor `interval: 60s → 15s` for finer long-term history. Rejected (D6):
      the dataset builds 900 s slots from counter deltas. Would also need a new `EntitySpec`.
- [ ] **6.3** Re-enable CoIoT unicast purely for repair hygiene. Rejected (D7).

## Troubleshooting

| Symptom | Cause / fix |
|---|---|
| Entity appears as `sensor.shellyem_main_channel_1_power_2` | The canonical ID was still registered when the MQTT entity was created. Rename or delete the MQTT registry entry, free the canonical ID (TODO-3.1), then reload. The `_2` never resolves itself |
| Native entity renamed but MQTT one still `_2` | The registry pins `entity_id` to `unique_id`. Change the MQTT `unique_id` (or delete its registry entry) so the ID is regenerated from `name` |
| No MQTT messages at all | **Every `mqtt_*` settings change needs a `/reboot`** — measured: without it the device stores the value, reports `mqtt.connected: false` and publishes nothing. Also check `/settings` `.mqtt.enable` / `.mqtt.server` and `broker_clients_connected` |
| Device connects then drops repeatedly | Wrong credential, or `mqtt_id` collides with another client. Keep the default `shellyem-244CAB417668` |
| `push_update_244CAB417668` still there | Expected. `check_coiot_config()` returns True when CoIoT is disabled (`components/shelly/utils.py:1070`), and `COAP_REPLY` increments the failure counter unconditionally (`coordinator.py:456-462`). Only a real CoAP push or removing the config entry clears it → ignore it (TODO-3.3) |
| Prometheus series duplicated | The renamed native entity is still enabled. Disable it |
| Values differ slightly from before | `value_template` precision vs. the device's payload — align using TODO-1.2 |
| Recorder growing faster than budgeted | Check whether voltage/PF MQTT sensors were added by mistake; they are deliberately **not** in this plan |

## Reference information

| Item | Value |
|---|---|
| Device | `shellyem-main` = `192.168.86.130` = `shellyem-main.iot.grigri`, IoT VLAN 501 |
| MQTT client id / topic prefix | `shellyem-244CAB417668` → `shellies/shellyem-244CAB417668/…` |
| Topics used | `emeter/0/power`, `emeter/1/power`, `online` |
| Topics available but unused | `emeter/<i>/{reactive_power,voltage,energy,returned_energy,total,total_returned}`, `relay/0` |
| Broker | `mosquitto.internal.grigri.cloud:1883` = VIP `192.168.193.9` (single BGP path via prusik) |
| Broker credential | Vault `/mosquitto/user` → ExternalSecret `mosquitto-user` (ns `mosquitto`), keys `username`/`password` |
| Device credential | `pass show iot/shellyem-main` |
| HA token flow | [`home-assistant-api-hacs-deploy.md`](../docs/troubleshooting/home-assistant-api-hacs-deploy.md) |
| Gen1 `/settings` params | `mqtt_enable`, `mqtt_server`, `mqtt_user`, `mqtt_pass`, `mqtt_id`, `mqtt_update_period` (0 disables), `mqtt_max_qos`, `mqtt_retain`, `mqtt_clean_session`, `mqtt_keep_alive` |

## Files to modify

| File | Change | TODO |
|---|---|---|
| `apps/home-assistant/config/integrations/shelly_em_mqtt.yaml` | **new** — probe block, then the final two MQTT sensors | 2.1, 3.2 |
| HA entity registry (`.storage`, not git) | rename + disable the two native power entities | 3.1 |
| HA repairs registry (`.storage`, not git) | ignore `push_update_244CAB417668` | 3.3 |
| Shelly device config (not git) | `mqtt_enable=true` + broker/credential, reboot | 1.1 |
| `docs/troubleshooting/home-assistant-shelly-coiot-push.md` | superseded banner + `coiot` drift | 5.2 |
| `planning/energy-baseline-production-execution.md` | replace the CoIoT TODO section | 5.3 |
| `docs/deployment/battery-optimizer.md` | update the grid-signal gotcha | 5.4 |
| `apps/mosquitto/rules/custom.yaml` | optional client-count alert bump | 5.5 |
| `docs/deployment/shelly-em-mqtt.md` | **new**, after implementation | 5.1 |

No change needed in `apps/battery-optimizer/`, the app repo, `iot-energy.json` or any dashboard
(consequence of D4).

## Execution order

```
TODO-1 (device + measure)  ──►  TODO-2 (probe, verify cadence/values)
                                       │
                                       ▼
                        TODO-3.1 rename+disable native  ──►  TODO-3.2 final YAML deploy
                                                                       │
                                                                       ▼
                                                          TODO-4 verify  ──►  TODO-5 docs
TODO-3.3 ignore repair (independent, human)
TODO-6 deferred
```

Division of labour: TODO-1/2/3.2/4/5 are agent-executable (TODO-1 and TODO-3.2 touch things outside
git — the device and the cluster — so each needs an explicit go-ahead). TODO-3.1 and TODO-3.3 are
registry/UI actions: human, or agent over the HA websocket API with confirmation.

## Related

- [`docs/troubleshooting/home-assistant-shelly-coiot-push.md`](../docs/troubleshooting/home-assistant-shelly-coiot-push.md) — why CoIoT push never worked, and its 15 s floor
- [`energy-baseline-production-execution.md`](energy-baseline-production-execution.md) — the plan this supersedes the input-signal TODO of
- [`docs/deployment/battery-optimizer.md`](../docs/deployment/battery-optimizer.md) — consumer of these entities
- [`docs/conventions/deploying-new-apps.md`](../docs/conventions/deploying-new-apps.md), [`docs/conventions/helm.md`](../docs/conventions/helm.md)
- [Shelly Gen1 API — MQTT support](https://shelly-api-docs.shelly.cloud/gen1/#mqtt-support), [Shelly EM: MQTT](https://shelly-api-docs.shelly.cloud/gen1/#shelly-em-mqtt)
- [HA MQTT sensor](https://www.home-assistant.io/integrations/sensor.mqtt/), [HA Prometheus integration](https://www.home-assistant.io/integrations/prometheus/)
