# Gen1 Shelly: disabling CoIoT breaks Home Assistant setup on the next restart

**Status:** diagnosed and fixed 2026-10-26 on `shellyem-main`. Device restored, HA entry `loaded`.
Extended the same day: the same root cause also explains a **device reboot loop** that had been
mis-attributed to MQTT — see
[Second consequence](#second-consequence-the-retry-storm-can-fault-the-device-itself).

> ⚠️ **The stakes went up on 2026-10-27.** MQTT is now *permanently enabled* on `shellyem-main` —
> it feeds both power sensors at 2 s. That means the reboot loop's co-factor is always present, so
> disabling CoIoT and restarting HA now produces **both** failures reliably rather than by chance.
> The blast radius also shifted: the two power entities come from MQTT and would survive a
> `setup_retry` storm, but the **energy counters, voltage, power factor and the relay switch would
> not** — and `grid_energy` is what the battery-optimizer dataset is built from. CoIoT is not optional
> on this device.

Disabling CoIoT on a Gen1 Shelly looks harmless — HA keeps polling and data keeps flowing. It is
**not** harmless: it removes the one endpoint HA needs to *initialize* the device, so the next HA
restart silently drops every entity of that config entry. The failure can surface hours or days
later, triggered by something unrelated (a ConfigMap change and the Reloader restart it causes, an
image bump, a node drain), which makes it very hard to attribute.

It has a second face: while HA retries, the requests it makes to the dead endpoint **hang inside the
device**, consuming its heap and HTTP connection slots. That alone is survivable, but combined with an
MQTT client it faults the firmware into a reboot loop.

## Symptom

- Every entity of one Gen1 Shelly config entry is `unavailable`, all with the *same*
  `last_reported` timestamp (the moment HA gave up).
- **Settings → Devices & services** shows the entry in `setup_retry` with reason
  `Device communication error occurred for <name>`.
- The device itself is perfectly healthy: `curl http://<device>/shelly`, `/settings` and `/status`
  all return `200` from the HA pod *and* from a workstation, with correct live values.
- Other Shelly devices in the same HA are fine — but note they are usually Gen2+ (RPC/HTTP), which
  does not use `/cit/*` at all. Only Gen1 is affected.
- Nothing in the HA log: the failure is raised as `ConfigEntryNotReady` and only surfaces as the
  entry's `reason` string.
- *If MQTT is also enabled:* the device **spontaneously reboots** every ~1–5 min (`uptime` resets with
  no request from you), and `ram_free` sits ~8 KB lower than normal. This reads like an MQTT fault and
  is not one.


## Environment

| Item | Value |
|---|---|
| Device | `shellyem-main`, Shelly EM (`SHEM`), Gen1, fw `20230913-114150/v1.14.0-gcb84623`, MAC `244CAB417668` |
| Address | `192.168.86.130` = `shellyem-main.iot.grigri`, IoT VLAN 501 |
| HA | 2026.10.4, `home-assistant-hass-0`, `aioshelly 13.34.1` |
| Trigger in this incident | Reloader restarted HA at 18:01:16Z after an unrelated ConfigMap change; all 14 entities went `unavailable` at 18:02:35Z |

## Root cause

With `coiot.enabled: false`, the Gen1 firmware does not serve the `/cit/*` family at all:

- HTTP `GET /cit/d` **hangs** — no response, no 404, the client just times out.
- CoAP on UDP 5683 is not answered either (`/cit/d`, `/cit/s` → `NetworkError`).

HA needs `/cit/d` on *every* setup:

```
BlockDevice.initialize()                      # aioshelly/block_device.py
  ├─ get_info()          -> GET /shelly       # works
  ├─ update_settings()   -> GET /settings     # works
  ├─ update_status()     -> GET /status       # works
  └─ if firmware_supported:
       _update_cit_d()                        # <- fails here
```

`_update_cit_d()` uses **HTTP `GET /cit/d`** for firmware ≥ 1.10 (`GEN1_HTTP_CIT_D_MIN_FIRMWARE_DATE`),
falling back to a CoAP `/cit/d` request on older builds — so on this device the failure is an HTTP
hang, and both transports are dead anyway.

The hang becomes `TimeoutError` → `DeviceConnectionTimeoutError` (a subclass of
`DeviceConnectionError`), which HA catches in `components/shelly/__init__.py:238-244`:

```python
except (DeviceConnectionError, MacAddressMismatchError) as err:
    await device.shutdown()
    raise ConfigEntryNotReady(
        translation_domain=DOMAIN,
        translation_key="device_communication_error",   # "Device communication error occurred for {device}"
        translation_placeholders={"device": entry.title},
    ) from err
```

`ConfigEntryNotReady` → the entry retries with a growing backoff and **all its entities stay
`unavailable`** until a setup attempt succeeds.

An already-initialized HA is unaffected: `ShellyBlockCoordinator` polls `/status` over REST every
`2.2 × coiot_update_period` (33 s here), which does not touch `/cit/*`. That is exactly why the
breakage stays invisible until the next restart.

## Second consequence: the retry storm can fault the device itself

The hanging `/cit/d` is not only a HA-side problem. Each stuck request **pins a mongoose HTTP
connection slot and its buffers on the device for as long as HA keeps retrying**, and HA retries with a
backoff that grows to `SETUP_RETRY_MAX_WAIT = 600 s` — so the consumption is standing, not transient.

Measured on `shellyem-main` (fw 1.14.0, `ram_total` 51 064 B):

| | CoIoT off + HA retrying | CoIoT on |
|---|---|---|
| `ram_free` | **27 224 B** | **35 368 B** |
| `GET /status` while 6 `/cit/d` are hanging | **no response** (2 of 3 rounds) | answers normally |
| device log heap floor | `New heap free LWM: 25960` → `25888` | `31408` |

On its own the device survives this — ~20 min stable with MQTT off while HA hammered it. But with an
**MQTT client also running**, the remaining heap margin disappears and the firmware faults: spontaneous
reboots every 71–280 s, which look exactly like "MQTT reboot-loops this device" and are very easy to
mis-diagnose that way. The per-request leak is tiny (18 hanging requests cost ~280 B of unrecovered
heap), which is why replaying hanging `/cit/d` calls by hand does **not** reproduce the fault — it takes
HA's full `initialize()` sequence under retry backoff.

Isolation matrix, ruled-out hypotheses and the inferred mechanism (measurements are direct; the step
from "heap and slots consumed" to "firmware resets" was never proven — no exception dump, and synthetic
replay does not crash it):
[`planning/shelly-em-mqtt-implementation.md`](../../planning/shelly-em-mqtt-implementation.md#root-cause-a-three-way-interaction-not-mqtt-alone).

**Practical consequence:** re-enabling CoIoT fixes both failures at once — HA's setup *and* the reboot
loop. If a Gen1 Shelly is spontaneously rebooting, check `coiot.enabled` **before** suspecting MQTT.

## Diagnosis (read-only)

```bash
SUSER=$(pass show iot/shellyem-main | sed -n 's/^login: //p'); SPASS=$(pass show iot/shellyem-main | head -1)
D=http://shellyem-main.iot.grigri

# 1. The device is healthy and authenticated access works:
curl -sS -m 8 -u "$SUSER:$SPASS" "$D/shelly"   | jq -c '{type,auth,fw}'

# 2. The tell-tale: /cit/d hangs while /settings answers instantly
curl -sS -m 12 -u "$SUSER:$SPASS" -o /dev/null -w 'cit/d http=%{http_code}\n' "$D/cit/d"   # -> http=000 (timeout)
curl -sS -m 8  -u "$SUSER:$SPASS" "$D/settings" | jq -c '.coiot'                            # -> {"enabled":false,...}

# 3. Confirm HA's view of the entry (state + reason)
python3 ha_ws.py '{"type":"config_entries/get","domain":"shelly"}' \
  | jq -c '.result[] | {title,state,reason:(.reason//null)}'
# -> {"title":"shellyem-main","state":"setup_retry","reason":"Device communication error occurred for shellyem-main"}
unset SUSER SPASS
```

CoAP can be probed from inside the HA pod, which has `aiocoap` installed:

```bash
kubectl --context=grigri -n home-assistant exec home-assistant-hass-0 -c hass -- python3 -c "
import asyncio, aiocoap
async def main():
    ctx = await aiocoap.Context.create_client_context()
    req = aiocoap.Message(code=aiocoap.GET, uri='coap://192.168.86.130/cit/d')
    try:
        res = await asyncio.wait_for(ctx.request(req).response, 6)
        print('/cit/d ->', res.code, len(res.payload), 'bytes')
    except Exception as e:
        print('/cit/d -> ERR', type(e).__name__, e)
asyncio.run(main())"
```

To reproduce HA's exact setup path (including the real exception) inside the pod, pipe the
credential in over **stdin** — never as an argv element — and use `ConnectionOptions`:

```python
from aioshelly.block_device import COAP, BlockDevice
from aioshelly.common import ConnectionOptions

coap = COAP()                                  # HA does the same: components/shelly/utils.py:236-238
await coap.initialize()                        # coroutine, (socket_port=5683, socket_ips=None)
opts = ConnectionOptions(ip_address="192.168.86.130", username=USER, password=PW)
dev = await BlockDevice.create(session, coap, opts)   # create(aiohttp_session, coap_context, ip_or_options)
await dev.initialize()                         # raises DeviceConnectionTimeoutError when /cit/d hangs
```

In aioshelly 13.x, `COAP` lives in `aioshelly.block_device` (there is no `aioshelly.coap` module) and
has no `async_create` classmethod; `BlockDevice.create()` does **not** call `initialize()`. Introspect
the installed signatures before writing such a probe.

## Fix

Re-enable CoIoT and reboot. **The endpoint matters:**

```bash
SUSER=$(pass show iot/shellyem-main | sed -n 's/^login: //p'); SPASS=$(pass show iot/shellyem-main | head -1)
D=http://shellyem-main.iot.grigri
curl -sS -m 25 -u "$SUSER:$SPASS" -X POST -G "$D/settings/advanced" \
  --data-urlencode coiot_enable=true --data-urlencode coiot_update_period=15 --data-urlencode coiot_peer= \
  -o /dev/null          # the device applies it but does NOT answer: curl times out, that is normal
curl -sS -m 10 -u "$SUSER:$SPASS" "$D/reboot"     # required, /cit/d keeps hanging until then
sleep 50
curl -sS -m 12 -u "$SUSER:$SPASS" -o /dev/null -w 'cit/d http=%{http_code} size=%{size_download}\n' "$D/cit/d"
unset SUSER SPASS
# -> cit/d http=200 size=922
```

Then make HA retry immediately instead of waiting out the backoff:

```bash
curl -sS -X POST -H "Authorization: Bearer $TOKEN" -H 'Content-Type: application/json' \
  https://hass.grigri.cloud/api/services/homeassistant/reload_config_entry \
  -d '{"entry_id":"<shelly entry id>"}'
```

Verified result: entry `state: loaded`, all 14 entities back within ~35 s, CoAP answering too
(`/cit/d` → `2.05 Content` 922 bytes, `/cit/s` → `2.05 Content` 198 bytes).

`coiot_peer=""` (multicast) is enough — HA only needs `/cit/d` to initialize, it does not need to
receive push. Push still never arrives and the 33 s REST polling plus the standing `push_update_*`
repair remain; that part is
[`home-assistant-shelly-coiot-push.md`](home-assistant-shelly-coiot-push.md).

## Pitfalls

- **`POST /settings/coiot` with `enabled=true` is silently ignored** on fw 1.14.0. It answers `200`
  with the full settings document and `coiot.enabled` still `false`; `enabled=1` behaves the same.
  The working call is `POST /settings/advanced` with `coiot_enable` / `coiot_update_period` /
  `coiot_peer` — exactly what aioshelly's own `BlockDevice.configure_coiot_protocol()` uses. When a
  Gen1 setting will not stick, read the aioshelly source for the real endpoint instead of trusting
  the API docs' URL shape.
- **A reboot is required** for CoIoT (and MQTT) changes to activate. Without it the value is stored
  and `GET /settings` may even show it, but the service does not start.
- **Settings POSTs do not answer.** `curl` reports `(28) Operation timed out ... with 0 bytes
  received` and `http=000` even when the change was applied. Always verify with a follow-up `GET`.
- **Do not conclude "HA cannot reach the device".** TCP to the device works fine; only `/cit/*` is
  gone. Symptom-level tests (`/shelly`, `/settings`) will mislead you — test `/cit/d` specifically.
- **`setup_retry` + `device_communication_error` has many causes** (unreachable host, wrong
  credentials, MAC mismatch, unsupported firmware). `/cit/d` hanging while `/settings` works points
  at CoIoT being disabled.
- **The websocket command `config_entries/reload` does not exist** in HA 2026.10.4 — it returns
  `unknown_command`. Use the `homeassistant.reload_config_entry` service over REST (or
  `/api/services/...` with `entry_id`).
- **Expired HA tokens are noisy and risky.** A monitoring loop with a stale token produced ten
  `homeassistant.components.http.ban` warnings in the HA log ("Login attempt or request with invalid
  authentication"). Refresh the token before a long loop; if `ip_bans_enabled` were on, that host
  would have been banned.
- **Never disable CoIoT on a Gen1 device that HA owns** — not even "to clean up", and not even
  temporarily. It arms a time bomb whose trigger is the next unrelated HA restart.
- **A spontaneous reboot loop is also a symptom of this**, not only of MQTT. The stuck `/cit/d`
  handlers eat ~8 KB of heap and every HTTP slot; add an MQTT client and the device faults. Check
  `coiot.enabled` before blaming MQTT — and note the loop *stops* as soon as CoIoT is re-enabled.
- **`/debug/log` cannot catch the crash on fw 1.14.0.** `debug_enable=true` is accepted by
  `POST /settings` and reads back `false`, so the endpoint keeps serving a frozen tail from an older
  boot. Don't build a diagnosis on it looking "live".
- **`broker_clients_connected` will not show you these reboots.** The device reconnects with the same
  client id, mosquitto replaces the stale session, and the count stays flat (it held at 7 straight
  through two confirmed reboots). It also flaps from your own `mosquitto_sub` sessions. Subscribe to
  `shellies/<id>/online` instead — it fires on every MQTT connect.

## Related

- [`home-assistant-shelly-coiot-push.md`](home-assistant-shelly-coiot-push.md) — why CoIoT push never
  reaches HA, the 33 s polling fallback and the standing `push_update_*` repair
- [`../../planning/shelly-em-mqtt-implementation.md`](../../planning/shelly-em-mqtt-implementation.md) —
  the MQTT work that surfaced this, and the reboot-loop root cause (MQTT is a co-factor, not the trigger)
- [`home-assistant-api-hacs-deploy.md`](home-assistant-api-hacs-deploy.md) — HA login-flow token recipe
- [`../deployment/battery-optimizer.md`](../deployment/battery-optimizer.md) — downstream consumer of these entities
