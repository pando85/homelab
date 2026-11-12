# Office A IoT Wi-Fi Flapping (fan-office-a, mini-switch-office-a)

Investigated 2026-11-12. All times UTC. The fan is resolved by an AP setting change (result pending);
the mini switch is **not resolved**.

## Problem

`BlackboxProbeFailed` fired repeatedly for `fan-office-a` (ICMP ping) and `mini-switch-office-a`
(TCP 6053, the ESPHome API port). Config: `system/monitoring/blackbox-exporter-values.yaml`.

- `fan-office-a`: 344 up/down changes in 7 d, outages of 35-243 min. Failures start 2026-11-03
  ~13:07Z (probe-down count per day from ~0 to 100-558).
- `mini-switch-office-a`: ~100% up until Nov 4, down from Nov 5 21:08Z, ~99% down since Nov 8 with
  brief recoveries (e.g. down Nov 12 09:25-20:41Z).
- Only the Wi-Fi IoT devices in Office A flap (fan, mini switch, a couple of Shellys/plug). Wired and
  cluster targets are stable. Shelly/SU100 devices stay associated (1-3 drops in 15 d), so their probe
  noise is not association loss. SU100 devices fail 20-30 times/day since at least Oct 3: unrelated
  baseline, exclude them.

Setup: two APs serve both IoT SSIDs, in or near the same room (UniFi controller 10.0.162).

| AP | Model / fw | 2.4 GHz | Notes |
|---|---|---|---|
| `limonero` | U6-LR, 6.7.54 | ch 6 auto, 20 MHz, power high | ~18-23 clients, 36% utilisation, ~18% TX retries, min-RSSI disabled, 139 d uptime |
| `cerezo` | BZ2, 4.3.28 (2.4 GHz only) | ch 11, power medium | ~1 client, min-RSSI **enabled** at -67 dBm (rssi 28 at noise floor ~-95) |

`fan-office-a` is a Tuya ceiling-fan controller driven by localtuya (only 6668/tcp, no web UI or
logs). `mini-switch-office-a` is an ESP8285 Athom mini switch (ESPHome 2025.10.2, `power_save_mode:
none`, no `debug:` component). Both are permanently mains-powered, with no relay dependency between
them.

## Root Cause

### fan-office-a (confirmed from AP logs)

1. **`cerezo` min-RSSI kick.** `stamgr` logged `kick-sta ... reason:Low RSSI` at 09:25:29Z (rssi 7)
   and 21:01:14Z (rssi 26) on Nov 12, both below the 28 threshold. The 09:25 kick starts the
   09:25-10:40Z outage. The fan's signal on `cerezo` fell from about -60..-62 dBm to -69..-71 dBm
   around Nov 2-3 (cause not found: physical move, TX power or interference are the candidates).
   Nov 3 13:59Z - Nov 4 18:20Z it went through 25 s-5 min drop/reconnect loops on `cerezo`, then
   sat on `limonero` at -80..-88 dBm (~34 drops there, ~59 UniFi drops total).
2. **Hourly group-key rekey on `limonero`.** `wpa_group_rekey=3600` runs at hh:01:2x for every client
   on the VAP. At 20:01:26Z the fan (silent since ~19:45Z, weak -85 dBm uplink) did not answer after 4
   tries and the AP sent deauth reason 16. It had succeeded at 15:01-19:01Z. This explains the fixed
   hh:01:26 UniFi drop timestamps (12 since Nov 5). `cerezo` rekeys around hh:08:33.

### mini-switch-office-a (not resolved)

- UniFi disconnects happened at -46..-59 dBm, well above `cerezo`'s threshold, and `limonero` has no
  min-RSSI, so this is not a signal kick.
- `limonero` log at 20:59Z: 60 s keep-alive silence, then `tx continue fail cnt = 200` (the AP cannot
  reach it). The mini sends its own deauth (reason 3) at 20:40:54Z and 21:01:13Z, coinciding with
  device-side reboots (~20:40:46Z and ~21:01:15Z).
- Radio: ~12.6% TX retries, AP-to-device rate 1 Mbps, device-reported RSSI -75..-79 dBm vs UniFi
  -63..-65.
- Interpretation: the ESP radio hangs and the device reboots itself. `wifi.reboot_timeout` is 24 h
  (`apps/esphome/config/packages/connection.yaml`) but `api.reboot_timeout` is not overridden, so
  ESPHome's default of 15 min is the likely trigger (not verified). The reset reason is not logged.
- Offline gaps per UniFi events: ~35 h (Nov 4 23:10 - Nov 6 10:41), ~3.4 d (Nov 7 08:13 - Nov 10
  17:03), ~1.6 d, ~11 h (Nov 12 09:26-20:41). First UniFi drop Nov 2 19:24Z. The owner chose not to
  flash a `debug:` component yet.
- The probe targets the API port, so it cannot tell a hung API from lost Wi-Fi. There is no ping
  probe for this device.

## Fix / Workaround

### Applied 2026-11-12 (live change outside git, owner approved)

On `cerezo`'s 2.4 GHz radio only, Minimum RSSI was lowered from -67 dBm to -75 dBm (rssi 20). No other
AP or SSID setting changed. The fan now stays on the AP it hears best instead of being kicked onto
`limonero` at -85 dBm.

### Result

To be filled in after 48 h of probe data.

### Follow-ups, in order

1. If the fan still drops: lengthen or disable the group rekey interval on the fan's IoT SSID only.
2. Investigate the unexplained fan signal drop on `cerezo` (physical position, antenna, interference).
3. If the mini switch keeps failing: add a ping probe for it in `blackbox-exporter-values.yaml` and a
   `debug:` component (reset reason) to its ESPHome config.
4. Longer term: replace `cerezo` (old 2.4 GHz-only BZ2) or add a third AP.

## How to Diagnose

Prometheus (read-only):

```promql
sum by (target) (changes(probe_success{job="blackbox-exporter"}[7d]))
min_over_time(probe_success{target=~"fan-office-a|mini-switch-office-a"}[1h])
```

Then correlate the outage start with AP logs. **Collect AP logs within hours of an incident**:
`limonero`'s buffer only covered ~7 h (14:01-21:17Z on Nov 12).

- SSH to the AP, `logread` or `/var/log/messages`. Look for `stamgr kick-sta ... reason:`,
  hostapd deauth/disassoc reason codes, and the group-key rekey lines.
- The UniFi 10.0.162 event log has **no disconnect-reason field** (legacy `stat/event` returns
  empty). Real reason codes are only in the AP logs.
- AP config values (`min_rssi_enabled`, `min_rssi`) come from the controller's `stat/device` JSON.
  UniFi `min_rssi` -67 dBm = rssi 28 at noise -95; -75 dBm = rssi 20.
- Timezones differ: UniFi events are UTC, AP logs CET, Home Assistant logs local.
- Mini switch device logs: `uvx --from 'esphome==2026.10.1' esphome logs mini-switch-office-a.yaml
  --device mini-switch-office-a.iot.grigri`, see the runbook in `apps/esphome/config/README.md`
  (needs `secrets.yaml` from the `esphome-secrets` secret).
- Prometheus has no UniFi/unpoller metrics, so signal and kick data come only from UniFi and the APs.

## Pitfalls / Dead Ends

Do not repeat these:

- **Shared mains glitch or the Nov 3 battery/AC-failsafe work.** No correlation: fan failures began
  13:07Z, before the first Nov 3 evening commit at 20:51Z; failures do not cluster at hh:01; only the
  fan failed at 20:01 and 21:01 (no Shelly/SU100 dropped). There is no voltage or power series in
  Prometheus to check it directly.
- **Fan powered through the mini switch relay.** Ruled out by the owner (both permanently powered).
  The fan's connection-refused (Errno 111, a fresh boot) 4 s after the mini's 20:40:46Z reboot is a
  coincidence or a shared upstream cause.
- **An hh:01 job in the repo.** Nothing runs at minute 01 (HA `time_pattern` runs every minute;
  battery-optimizer slots are :00/:15/:30/:45; only a weak 2x correlation with its actuator writes).
  The hh:01 pattern is the WPA group rekey.
- **The blackbox exporter itself.** Probe results match device-side evidence.
- **AP-wide faults.** No crash, watchdog or radio reset on `limonero` (139 d uptime, no channel
  changes, Radio AI disabled). `cerezo` logs frequent `rx_clear=99/100` interference lines and ~20k
  cumulative beacon-stuck/HAL resets since boot, but the counters did not move over 30 s and are not
  tied to incidents. The Nov 4 16:42-16:43Z event, where four clients left `cerezo` within seconds
  (one at -85 dBm), looks like a `cerezo` radio disturbance.
- The mini switch's probe failing does not prove lost Wi-Fi (API port probe), and the fan's ping
  failing does not prove a power loss: check the AP kick/deauth reason first.
