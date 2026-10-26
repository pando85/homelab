# Home Assistant Shelly CoIoT push updates (Gen1 devices)

**Status:** diagnosed 2026-10-26. **Superseded as a resolution fix — will not be implemented.**
Gen1 `coiot_update_period` is documented as `15..65535 s`, so CoIoT can never beat 15 s and HA can
never beat `2.2 × 15 s = 33 s`. MQTT reaches the device's own change-detection rate (sub-second on
load transients) with a 5 s guaranteed refresh, needs **no** pfSense change (IoT-VLAN TCP to the
mosquitto VIP already passes) and **no** new Service or VIP. See
[`planning/shelly-em-mqtt-implementation.md`](../../planning/shelly-em-mqtt-implementation.md).

What remains valid here: the diagnosis, the evidence, and the pitfall list. The unicast CoIoT
procedure below is kept as a reference for any *other* Gen1 device where 15 s is enough — and as the
only way to clear the standing `push_update_*` repair, which **MQTT does not fix** (see
[Pitfalls](#pitfalls)).

**Device drift, 2026-10-26 (later the same day):** `shellyem-main` now reports
`coiot: {"enabled": false, ...}`, and MQTT is enabled. HA still polls over CoAP at 33 s, so the
table below describes the state *before* that change.

Generation-1 Shelly devices (`shellyem-main`, a Shelly EM) talk to Home Assistant over **CoIoT**
(CoAP on UDP 5683). CoIoT is enabled on the device, but its **peer is empty**, so the device only
sends *multicast* status announcements — and multicast does not reach the HA pod. HA therefore
falls back to **polling**, which works but at 2.2× the device's own update period and leaves a
standing error-level repair issue.

This page records the evidence, the root cause, the diagnosis commands, and the step-by-step
configuration for unicast CoIoT push in this cluster.

## Problem

Symptoms, all observed on 2026-10-26:

- Home Assistant shows an active, non-ignored, **error**-severity repair for the device:
  `push_update_244CAB417668` ("Shelly device shellyem-main push update failure"), created
  2026-10-25T01:03:58Z. It is re-created about 3 minutes after **every** HA restart
  (`is_persistent: false`, and it takes `MAX_PUSH_UPDATE_FAILURES = 5` poll replies to trip).
- `sensor.shellyem_main_channel_1_power` changes state every **33 s** — not every 15 s, the
  device's configured `coiot.update_period`.
- The device's own CoIoT multicast announcements are visible on the wire but never arrive at HA.

Data is **not** missing: both emeter channels report sane values. This is a resolution/latency and
hygiene problem, not an outage.

## What is configured today

| Item | Value | Source |
|---|---|---|
| Device | `shellyem-main`, Shelly EM (`SHEM`), Gen1, fw `1.14.0-gcb84623` | HA device registry, `/shelly` |
| MAC / IP | `24:4c:ab:41:76:68` / `192.168.86.130` (DHCP with a pfSense static mapping) | HA registry, pfSense ARP/DHCP |
| Network | IoT VLAN 501 (`igb1.501`, pfSense `opt6`, 192.168.86.254/24) | pfSense config |
| `coiot` | `{"enabled": true, "update_period": 15, "peer": ""}` — **empty peer = multicast** | HA diagnostics + device `/settings` |
| Cloud / MQTT | both **disabled** — CoIoT + HTTP polling is the only data path | device `/settings` |
| Device clock | skew 0 s vs. UTC, uptime ~3.8 d, RSSI −56 dBm, both emeters `is_valid: true` | device `/status` |
| HA | 2026.10.4, `aioshelly==13.34.1`, pod `home-assistant-hass-0` = `10.0.4.218` on `prusik` | `/api/config`, manifest, `kubectl` |
| HA `internal_url` / `external_url` | **both null** | `/api/config` |
| HA CoAP socket | UDP `0.0.0.0:5683` bound in the pod, joined IGMP group `224.0.1.187` on `eth0` | `/proc/net/udp`, `/proc/net/igmp` |
| HA service | `home-assistant-hass` exposes **TCP 8123 only** — nothing exposes UDP 5683 | `kubectl get svc` |
| Other Shelly devices | `shellypmmini-servers`, `shellyplus2pm-{office-a,office-t,bedroom}` are **Gen2+** (RPC/WebSocket) and are unaffected | HA device registry |

## Root cause

Three independent gaps, all of which must be closed for *push* to work:

1. **CoIoT is in multicast mode.** With `peer: ""` the device publishes status to
   `224.0.1.187:5683` instead of unicasting to HA. Captured on `igb1.501`:
   ```text
   16:07:04.922328 IP (ttl 128) 192.168.86.130.5683 > 224.0.1.187.5683: UDP, length 239
   16:07:19.924779 IP (ttl 128) 192.168.86.130.5683 > 224.0.1.187.5683: UDP, length 239
   ```
   ~15 s apart, matching `update_period`. TTL 128 means the packets are *routable in principle* —
   the problem is forwarding, not TTL.
2. **Nothing forwards that multicast.** pfSense has no IGMP proxy and no multicast routing
   (`igmp` absent from `/conf/config.xml`, no IGMP joins observed on `igb1.501`/`igb1.101`), and
   HA's pod CIDR (`10.0.0.0/8`, Cilium native routing) is not routable from pfSense at all — only
   the node addresses (`192.168.192.0/24`) and the Cilium LoadBalancer VIPs (`192.168.193.0/24`,
   BGP-learned /32s) are. HA *has* joined the group on `eth0`; the packets simply never get there.
3. **No unicast path exists either.** The HA Service exposes only TCP 8123, so there is no stable
   address the device could push to. In addition, the IoT VLAN's catch-all pass rule carries
   `flags S/SA` **without** an explicit `proto`, which pf treats as TCP-only
   (`pf.conf(5)`: "when flags is specified without proto, the rule is treated as if proto tcp was
   specified"). **Device-initiated UDP from the IoT VLAN to anywhere outside it is blocked today** —
   confirmed by the state table: 4 TCP states from IoT to `192.168.193.9` (mosquitto), 0 UDP states
   from IoT to any cluster VIP. Any unicast design needs an explicit UDP pass rule.

## Why it still works: the polling fallback

What actually reaches HA is HA's own CoAP request, answered by the device:

```text
16:07:24.022560 IP (ttl 62)  192.168.192.3.45104 > 192.168.86.130.5683: UDP, length 12   # request
16:07:24.061033 IP (ttl 128) 192.168.86.130.5683 > 192.168.192.3.45104: UDP, length 239  # reply
```

`192.168.192.3` is `prusik` (the pod's traffic is SNAT'd by the node), `45104` is an ephemeral
source port — **not** a configuration value. The cadence comes from HA:

- `ShellyBlockCoordinator` sets `update_interval = UPDATE_PERIOD_MULTIPLIER × coiot.update_period`
  = `2.2 × 15 s` = **33 s** (`homeassistant/components/shelly/coordinator.py`,
  `const.py: UPDATE_PERIOD_MULTIPLIER = 2.2`).
- Each interval calls `device.update()` → `_coap_request("s")` (`aioshelly/block_device/device.py`),
  i.e. a CoAP GET of `/cit/s`. The reply is classified `CoapType.REPLY` (CoAP code 69 = 2.05) and
  increments `_push_update_failures`; a genuine push would be `CoapType.PERIODIC` (code 30 = 0.30)
  and would reset the counter and delete the repair.

**Rule of thumb:** if a Gen1 Shelly's entities update at exactly `2.2 ×` the device's
`update_period`, you are on the polling fallback, not on push.

Energy cross-check (device `/status` vs. HA): device `total` is in **Wh** — `17257856.8 Wh` =
`17257.8568 kWh`, matching `sensor.shellyem_main_channel_1_energy = 17257.5508 kWh`.

## How to diagnose (read-only)

```bash
# 1. HA side: is the repair there, and what does the device report?
#    (token flow: docs/troubleshooting/home-assistant-api-hacs-deploy.md)
curl -sS -H "Authorization: Bearer $TOKEN" \
  https://hass.grigri.cloud/api/diagnostics/config_entry/<shelly-entry-id> \
  | jq '.data.device_settings, .data.device_info, .data.last_error'
#    -> {"coiot":{"enabled":true,"update_period":15,"peer":""}, ...}

# 2. HA side: is the CoAP socket up, and did it join the multicast group?
kubectl --context=grigri exec -n home-assistant home-assistant-hass-0 -c hass -- cat /proc/net/udp
#    00000000:1633  == 0.0.0.0:5683 (0x1633 = 5683)
kubectl --context=grigri exec -n home-assistant home-assistant-hass-0 -c hass -- cat /proc/net/igmp
#    BB0100E0 on eth0 == 224.0.1.187

# 3. HA side: measure the real update cadence
curl -sS -H "Authorization: Bearer $TOKEN" \
  "https://hass.grigri.cloud/api/history/period/<iso-start>?filter_entity_id=sensor.shellyem_main_channel_1_power&minimal_response"

# 4. Device side (read-only GETs; credentials in `pass iot/shellyem-main`,
#    username is the `login:` line, password the first line — basic auth works):
curl -sS -u "$USER:$PASS" http://shellyem-main.iot.grigri/shelly   # no auth needed: type/mac/auth/fw
curl -sS -u "$USER:$PASS" http://shellyem-main.iot.grigri/settings | jq .coiot,.cloud,.mqtt
curl -sS -u "$USER:$PASS" http://shellyem-main.iot.grigri/status   | jq .time,.unixtime,.uptime,.emeters

# 5. Wire (pfSense, root read-only): distinguish push from poll-reply.
#    A push has NO preceding request from HA; a poll reply does.
tcpdump -p -n -i igb1.501 'udp port 5683 and host 192.168.86.130'
pfctl -ss | grep 5683          # active CoAP states
pfctl -sr | grep -n igb1.501   # IoT rule order (watch for `flags S/SA` on the catch-all)
```

## Fix: unicast CoIoT to a LoadBalancer VIP

Target path — the device pushes unsolicited CoAP to a stable VIP, Cilium delivers it to the pod:

```text
ShellyEM 192.168.86.130 --UDP/5683--> 192.168.193.12 (Cilium LB VIP, BGP-learned by pfSense)
                                              --> home-assistant-hass-0 10.0.4.218:5683
```

HA needs no configuration change: the CoAP socket is already bound to `0.0.0.0:5683`, and
`aioshelly` dispatches incoming datagrams by the **CoAP device-ID option 3332** (last 6 of the MAC),
not by source IP (`block_device/coap.py:208-225`, `device.py:98-103`). So SNAT through the
LoadBalancer is harmless.

### Step 1 — GitOps: expose UDP 5683 (agent may prepare, ArgoCD applies)

Add a second Service to `apps/home-assistant/values.yaml` (`service:` block, next to `hass:`):

```yaml
  service:
    hass:
      controller: hass
      ports:
        http:
          port: 8123
    # CoIoT (Gen1 Shelly push). UDP; consumed by the Shelly devices, not by browsers.
    hass-coiot:
      type: LoadBalancer
      controller: hass
      annotations:
        lbipam.cilium.io/ips: "192.168.193.12"
      ports:
        coap:
          port: 5683
          protocol: UDP
```

Notes:

- **VIP choice:** the pool is `CiliumLoadBalancerIPPool/default` = `192.168.193.0/24`. In use by
  Services: `.1 .2 .3 .4 .5 .7 .8 .9 .10`; reserved by pfSense aliases but not currently advertised:
  `.6` (`k8s_deluge`), `.11` (`k8s_pt`). **Use `.12`** and re-verify both sides at implementation
  time (`kubectl get svc -A -o json | jq '[.items[].status.loadBalancer.ingress[]?.ip]'` plus the
  pfSense `k8s_*` aliases).
- **No `externalTrafficPolicy: Local` here** (unlike mosquitto/unifi). The HA pod is pinned to
  `prusik` by its node-local PVC, the BGP peering config selects *all* Linux nodes, and some VIPs
  are learned ECMP via both nodes — with `Local`, pushes landing on the node without the pod are
  dropped. The default `Cluster` policy SNATs, which is fine because dispatch is by device ID.
  If you do choose `Local`, first confirm with `vtysh -c 'show ip bgp 192.168.193.12/32'` on pfSense
  that the VIP is advertised only by `prusik`.
- Validate before committing, per repo convention:
  ```bash
  helm dependency build apps/home-assistant/
  helm template --include-crds --namespace home-assistant home-assistant apps/home-assistant/ \
    | grep -B4 -A14 'name: home-assistant-hass-coiot'
  pre-commit run --all-files
  ```
  Then commit and let ArgoCD sync (`selfHeal` will revert any `kubectl apply`).

### Step 2 — pfSense: alias + UDP pass rule (**human action**)

Routing needs nothing: pfSense's FRR (AS 64512) learns the new /32 automatically from Cilium
(AS 64513) once the Service exists. The firewall does need a change, because IoT-initiated UDP is
blocked (root cause #3). Agents must not mutate pfSense — do this in the UI:

1. **Firewall → Aliases → Add**: name `k8s_ha_coiot`, type `Host(s)`, address `192.168.193.12`,
   description "K8s Home Assistant CoIoT (UDP 5683) LB VIP".
2. **Firewall → Rules → IoT (opt6) → Add**, placed **above** the two `reject` rules (i.e. next to
   the existing mDNS pass, position 3-4):
   - Action `Pass`, Protocol `UDP`
   - Source: `IoT net` (or narrower: single host `192.168.86.130`)
   - Destination: `k8s_ha_coiot`, port `5683`
   - Description: `ShellyEM CoIoT push to Home Assistant (UDP 5683)`
3. Apply, then confirm the rule is loaded and *not* TCP-only:
   ```bash
   pfctl -sr -v | grep -i 5683
   ```

Prefer the narrow single-host source if only `shellyem-main` needs it; widen it only when another
Gen1 device is onboarded.

### Step 3 — Shelly device: set the CoIoT peer (**human action**)

WebUI (`http://shellyem-main.iot.grigri`, log in with the `pass iot/shellyem-main` credentials):
**Internet & Security → ADVANCED - DEVELOPER SETTINGS** → keep **Enable CoIoT** checked → set
**CoIoT peer** to `192.168.193.12:5683` → **SAVE** → then **reboot the device** (the peer change
only takes effect after a restart; HA's own docs require it).

Equivalent REST call (Gen1 `/settings/advanced`, the same endpoint `aioshelly`'s
`configure_coiot_protocol()` uses):

```bash
PASS=$(pass show iot/shellyem-main | head -1)
USER=$(pass show iot/shellyem-main | sed -n 's/^login: //p')
curl -sS -u "$USER:$PASS" -X POST \
  "http://shellyem-main.iot.grigri/settings/advanced?coiot_enable=true&coiot_peer=192.168.193.12:5683"
curl -sS -u "$USER:$PASS" http://shellyem-main.iot.grigri/settings | jq .coiot
unset PASS USER   # never echo or commit these
```

Expect `{"enabled": true, "update_period": 15, "peer": "192.168.193.12:5683"}`. The reboot can be
triggered from HA (`button.shellyem_main_reboot`) or the device UI. Keep `update_period` at 15.

### Step 4 — Verify

```bash
# a) Push packets with NO preceding HA request (compare against the poll pattern above):
ssh root@pfsense.grigri "tcpdump -p -n -i igb1.501 'udp port 5683 and dst 192.168.193.12'"
#    expect 192.168.86.130.5683 > 192.168.193.12.5683, length 239, every ~15 s
ssh root@pfsense.grigri "pfctl -ss | grep 193.12"

# b) HA update cadence drops from ~33 s to ~15 s:
#    websocket subscribe_events/state_changed on sensor.shellyem_main_channel_1_power,
#    or the /api/history/period call from the diagnosis section.

# c) Device settings persisted:
curl -sS -H "Authorization: Bearer $TOKEN" \
  https://hass.grigri.cloud/api/diagnostics/config_entry/<shelly-entry-id> \
  | jq '.data.device_settings.coiot'

# d) Repair gone (auto-deleted on the first CoAP PERIODIC message):
curl -sS -H "Authorization: Bearer $TOKEN" https://hass.grigri.cloud/api/repairs/list_issues \
  | jq '[.issues[] | select(.domain=="shelly")]'
```

### Rollback

- **Device only:** set the peer back to multicast — `coiot_peer=mcast` (or clear the field in the
  UI) and reboot. HA resumes 33 s polling; the repair returns after ~3 min.
- **Cluster only:** delete the `hass-coiot` Service block from `values.yaml`; Cilium releases the
  VIP and pfSense drops the BGP route. Leave the pfSense alias/rule in place (harmless) or remove
  them in the same change window.

## Alternatives considered

| Option | Verdict |
|---|---|
| **IGMP proxy + multicast routing on pfSense** | Rejected. Needs `igmpproxy` (IoT upstream → DMZ downstream), IGMP firewall rules, *and* a multicast route into `10.0.0.0/8`, which Cilium does not route to pods. Much larger blast radius on the router for one device, and still not guaranteed to reach the pod netns. |
| **`hostNetwork: true` on the HA pod** | Rejected. Binds 5683 on the node, conflicts with anything else using it, bypasses the Service/Ingress model, and complicates node moves. |
| **NodePort + node IP (`192.168.192.3:<nodePort>`) as the peer** | Works, and needs the same pfSense UDP rule, but ties the device config to a node address and a random port. The LB VIP matches cluster convention (`mosquitto`, `unifi`, `transcoder` …) and survives node changes. |
| **HA's own "Configure CoIoT" repair button** | **Do not use** — see pitfalls. |
| **Do nothing** | Legitimate. Polling delivers correct data at 33 s; the cost is a permanent error-level repair and coarser grid-power resolution. If this is the decision, *ignore* the repair deliberately rather than "fixing" it from HA. |

## Pitfalls

- **The `push_update_*` repair survives both "disable CoIoT" and "switch to MQTT".**
  `check_coiot_config()` returns `True` when the device reports `coiot.enabled: false`
  (`components/shelly/utils.py:1070`), which makes `async_manage_coiot_issues_task()` take the
  *push-update-failure* branch and raise the **error**-severity issue rather than the
  `coiot_unconfigured` warning. And `ShellyBlockCoordinator._handle_update()` increments
  `_push_update_failures` on every `COAP_REPLY` **unconditionally** (`coordinator.py:456-462`),
  regardless of the device's CoIoT setting. HA keeps polling over CoAP whether or not the device has
  CoIoT or MQTT enabled, so the only ways to clear it are a genuine CoAP `PERIODIC` push (unicast
  CoIoT, Step 1-3 below), removing the `shelly` config entry, or **ignoring the issue in the UI**.
  Ignoring is persisted (`helpers/issue_registry.py`, storage key `repairs.issue_registry`) even
  though the issue itself is `is_persistent: false`, so it survives restarts.
- **Never click "Configure CoIoT" in HA repairs.** `CoiotConfigureFlow`
  (`homeassistant/components/shelly/repairs.py:262-288`) writes
  `get_coiot_address(hass):get_coiot_port(hass)` straight onto the device via
  `configure_coiot_protocol()`. `get_coiot_address()` derives the address from HA's own URL
  (`get_url(prefer_external=False)` → source IP toward that host). Today `internal_url` and
  `external_url` are both null, so it returns `None` and the flow aborts with `cannot_configure`
  (harmless). If an internal URL is ever configured, HA would overwrite the peer with an address it
  can see — the **pod IP `10.0.4.218`**, which is unreachable from the IoT VLAN — silently breaking
  push. Fixing it then requires re-setting the peer on the device by hand.
- **Leave `internal_url` unset**, or accept a `coiot_unconfigured` *warning* repair: once a peer is
  configured, `check_coiot_config()` compares it against `get_coiot_address():port` and flags a
  mismatch. An **empty** peer is treated as correctly configured (the check is falsy-guarded), which
  is why today's issue is `push_update_failure` (not fixable) rather than `coiot_unconfigured`.
- **`45104` (or any similar port) in a capture is HA's ephemeral polling source port**, not
  something to configure. The CoIoT peer port is always `5683` unless you override it with
  `shelly: coap_port:` in `configuration.yaml` (not used here).
- **Don't point the peer at `hass.grigri.cloud`.** The external ingress LB exposes TCP 80/443 only;
  there is no UDP 5683 path behind it.
- **IoT VLAN blocks device-initiated UDP.** Adding the Service and setting the peer without the
  pfSense rule produces *no visible error* — HA just keeps polling and the repair stays. Verify with
  `tcpdump`, not with "the sensors still update".
- **`externalTrafficPolicy: Local` + ECMP advertisement = ~50 % push loss.** See Step 1.
- **The peer change needs a device reboot**; until then the device keeps multicasting and nothing
  appears to have changed.
- **Gen2+ devices don't use this path.** `shellypmmini-servers` and the `shellyplus2pm-*` devices use
  RPC/outbound WebSocket (and are on `192.168.86.0/24` too); they need no CoIoT peer and are not
  affected by the IoT UDP rule.
- **Shelly relies on SNTP for metering.** This device reports 0 s clock skew, so nothing to do — but
  if power/energy ever looks wrong, check `/status` `unixtime` before blaming CoIoT.

## Relevance to battery-optimizer

`apps/battery-optimizer/files/service.yaml` maps the shadow service's grid signals to this device:
`grid_power: sensor.shellyem_main_channel_1_power`,
`grid_energy: sensor.shellyem_main_channel_1_energy`,
`observed: [sensor.shellyem_main_channel_2_power]`.

**This is not a blocker for Phase C.** With `freshness_s.grid_power: 300` and
`home_assistant.poll_interval_s: 60`, a 33 s source is comfortably fresh, and the slot-energy
accounting uses the `grid_energy` counter deltas, which polling captures fine.

What a faster transport buys:

- **Sub-second response to load transients** in the recorder instead of 33 s samples — better
  training data for the load forecaster, and a fast enough grid signal for a future battery
  zero-export / power-limiting control loop.
- **One less standing error-level repair** in HA *only if* the transport is CoIoT push; MQTT does
  not clear it (see Pitfalls).
- Fewer CoAP requests from HA to the device.

CoIoT caps the first of these at 15 s, so the work is tracked as MQTT instead:
[`planning/shelly-em-mqtt-implementation.md`](../../planning/shelly-em-mqtt-implementation.md).
The original CoIoT TODO in
[`planning/energy-baseline-production-execution.md`](../../planning/energy-baseline-production-execution.md)
is closed as superseded.

## References

- [HA Shelly integration — Gen1 CoIoT configuration](https://www.home-assistant.io/integrations/shelly/#shelly-device-configuration-generation-1)
- [Shelly Gen1 API — CoIoT protocol](https://shelly-api-docs.shelly.cloud/gen1/) (`coiot_peer`: `mcast` or `ip[:port]`, default port 5683)
- HA core `2026.10.4`: `components/shelly/coordinator.py` (poll multiplier, push-failure counting),
  `components/shelly/utils.py` (`check_coiot_config`, `get_coiot_address`),
  `components/shelly/repairs.py` (`CoiotConfigureFlow`)
- `aioshelly` `13.34.1`: `block_device/coap.py` (device-ID dispatch, `CoapType`),
  `block_device/device.py` (`update()` = CoAP GET `/cit/s`, `configure_coiot_protocol()`)
- [`docs/deployment/cilium-bgp-control-plane.md`](../deployment/cilium-bgp-control-plane.md) — VIP advertisement via FRR
- [`docs/conventions/cilium.md`](../conventions/cilium.md) — BGP v2 API, LB pool
- [`docs/troubleshooting/home-assistant-api-hacs-deploy.md`](home-assistant-api-hacs-deploy.md) — headless HA API token for the checks above
