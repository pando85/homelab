# Office A IoT Wi-Fi Flapping (fan-office-a, mini-switch-office-a)

Investigated 2026-11-12. All times UTC (AP logs are CET, UTC+1). **Neither device is resolved.** The
`cerezo` min-RSSI change did not stop the fan outages; the mini switch is awaiting a power-cycle and
the new reset-reason sensors.

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
| `limonero` | U6-LR, 6.7.54 | ch 6 auto, 20 MHz, power high | ~17-23 clients, 29-36% busy, 18-29% TX retries, min-RSSI disabled, 139 d uptime |
| `cerezo` | BZ2, 4.3.28 (2.4 GHz only) | ch 11, power medium | ~1 client, min-RSSI -75 dBm since 21:32Z (was -67), ~26% TX retries |

`fan-office-a` is a Tuya ceiling-fan controller driven by localtuya (only 6668/tcp, no web UI or
logs). `mini-switch-office-a` is an ESP8285 Athom mini switch (`power_save_mode: none`). It ran
ESPHome 2025.10.2 without `debug:`; it now runs 2026.10.2 with `debug:` sensors (see below). Both
devices are permanently mains-powered, with no relay dependency between them.

## Root Cause

### fan-office-a (causes found, but not all of them)

1. **`cerezo` min-RSSI kick.** `stamgr` logged `kick-sta ... reason:Low RSSI` at 09:25:29Z (rssi 7)
   and 21:01:14Z (rssi 26) on Nov 12, both below the old threshold of 28 (-67 dBm). The 09:25 kick
   starts the 09:25-10:40Z outage. The fan's signal on `cerezo` fell from about -60..-62 dBm to
   -69..-71 dBm around Nov 2-3 (cause not found: physical move, TX power or interference are the
   candidates). Nov 3 13:59Z - Nov 4 18:20Z it went through 25 s-5 min drop/reconnect loops on
   `cerezo`, then sat on `limonero` at -80..-88 dBm (~34 drops there, ~59 UniFi drops total).
2. **Hourly group-key rekey on `limonero`.** `wpa_group_rekey=3600` runs at hh:01:2x for every client
   on the VAP. At 20:01:26Z the fan (silent since ~19:45Z, weak -85 dBm uplink) did not answer after 4
   tries and the AP sent deauth reason 16. It had succeeded at 15:01-19:01Z. This explains the fixed
   hh:01:26 UniFi drop timestamps (12 since Nov 5). `cerezo` rekeys around hh:08:33.
3. **Silent-then-gone, no AP action (cause not visible).** On `cerezo` the fan stayed associated
   ~47 min after its 21:05Z rejoin, the probe failed at 21:51Z, and at 21:52:43Z `cerezo` logged it
   leaving with no kick, no Low RSSI, no reason code and no provisioning event. So the Low-RSSI kicks
   are real but only one of several causes.

### mini-switch-office-a (not resolved)

- UniFi disconnects happened at -46..-59 dBm, well above `cerezo`'s threshold, and `limonero` has no
  min-RSSI, so this is not a signal kick.
- `limonero` log at 20:59Z: 60 s keep-alive silence, then `tx continue fail cnt = 200` (the AP cannot
  reach it). The mini sends its own deauth (reason 3) at 20:40:54Z and 21:01:13Z, coinciding with
  device-side reboots (~20:40:46Z and ~21:01:15Z).
- **21:24-21:26Z outage, AP view (`limonero`).** 21:24:07Z last frame, 21:25:07Z `keep-alive silent
  for 60-sec`, 21:25:51Z the AP removed it (`MacTableMaintenance had left`, `per_err_times=61, tx
  continue fail cnt=69`, DEAUTH reason 3 sent by the AP). No kick, no stamgr action, no
  provisioning. It never tried to associate on either AP afterwards.
- Radio: ~12.6% TX retries, AP-to-device rate 1 Mbps, device-reported RSSI -75..-79 dBm vs UniFi
  -63..-65.
- Offline gaps per UniFi events: ~35 h (Nov 4 23:10 - Nov 6 10:41), ~3.4 d (Nov 7 08:13 - Nov 10
  17:03), ~1.6 d, ~11 h (Nov 12 09:26-20:41). First UniFi drop Nov 2 19:24Z.
- After the owner's flash the mini came back, then was unreachable again: down from 22:26Z, while the
  fan recovered at ~22:38Z. An AP restart therefore did not bring it back.
- Interpretation: the ESP radio hangs and the device reboots itself. `wifi.reboot_timeout` is 24 h
  (`apps/esphome/config/packages/connection.yaml`) but `api.reboot_timeout` is not overridden, so
  ESPHome's default of 15 min is the likely trigger (not verified). The reset reason was not logged
  before the flash.
- The probe targets the API port, so it cannot tell a hung API from lost Wi-Fi. There is no ping
  probe for this device.

### Common pattern

For the mini (21:24-21:26Z on `limonero`) and the fan (21:51-21:52Z on `cerezo`), the AP shows the
station going silent and then being removed. The APs only see that the stations stopped
acknowledging frames. The cause is not visible from the APs: device radio hang, firmware or power.

## Fix / Workaround

### Applied 2026-11-12 (live change outside git, owner approved)

On `cerezo`'s 2.4 GHz radio only, Minimum RSSI was lowered from -67 dBm to -75 dBm (rssi 28 -> 20 at
~-95 dBm noise) at 21:32:53Z. Provisioning took ~45 s (21:33:04-21:33:47Z), no reboot, all clients
stayed. The UniFi UI slider is in dBm (-67..-90). The save also added `assisted_roaming_enabled:
false` as a default. No other AP or SSID setting changed.

### Result

- **Interim: it did not stop the outages.** The fan stayed on `cerezo` ~47 min through the change,
  then dropped at 21:51-21:52Z with no kick (see cause 3).
- Final result: to be filled in after 48 h of probe data.

### ESPHome upgrade (mini switch), flashed 2026-11-12

Flashed 2026.10.2 with `debug:` sensors (reset reason, device info, heap free, heap max block, loop
time; ~516 B RAM, ~5.3 KB flash) to record why it reboots. The sensors are readable only while the
device is reachable.

The upgrade alone is **not expected to fix it** (~20-30%, low confidence):

- Same Arduino ESP8266 core 3.1.2 and Wi-Fi blob in 2025.10.2 and 2026.10.x. No release note from
  2025.10.2 to 2026.10.1 describes an ESP8266 Wi-Fi hang fix; PR 18333 (2026.8.0) fixes an mDNS crash
  on ESP8266 only.
- Upstream reports of the same symptom, unresolved: esphome issue 16724 (ESP8266 drops the API while
  still answering ping; closed "not planned"), issue 14999 (API noise handshake timeout, 2026.3.0,
  weak-Wi-Fi ESP8266), ratgdo issue 541 (ESP8266 off Wi-Fi for hours, helped by `wifi: phy_mode: 11G`,
  available since 2026.5.0).
- Fleet comparison: the two ESP8285 smart plugs run 2026.10.x on the identical Wi-Fi config and one
  is stable (0 flaps in 7 d), so the same firmware can hold Wi-Fi.

Not done, optional experiments: `phy_mode: 11G`, and an `api: reboot_timeout` tweak (the default 15
min reboot with no API client is the recovery path: do not disable it).

### RF environment and the channel/power decision

Read-only from the controller. No scan was stored: a spectrum scan disrupts the AP's clients, so it
was **not run**.

- `stat/spectrum-scan` is empty for both APs. Neighbour data exists only on each AP's own channel:
  `limonero` ch6 sees ~309 neighbour BSSIDs (298 weaker than 10 dB SNR, only 4 at 20 dB or more);
  `cerezo` ch11 sees 21 (all weak, strongest 25 dB SNR) plus 7 on ch10. Noise floor -96 dBm on both.
- Channel busy: `limonero` ~29-30% (17 clients, 29% TX retries); `cerezo` 5-21% depending on the
  panel, TX retries ~26%, 1 client. About 14 points of the 21% are neither its own rx nor tx:
  non-Wi-Fi energy, matching the `rx_clear=99/100, rx_frame=0` log lines.
- `limonero` 5 GHz (ch40/80 MHz, 4% busy) is irrelevant: the IoT devices are 2.4 GHz only.

**Decision (owner agreed): do not change `cerezo`'s channel or TX power.**

- AP TX power only improves what the devices hear (downlink); the evidence is stations going silent
  and a weak uplink (fan ~-70 dBm at `cerezo`).
- `limonero` is already at high power and the mini still dropped there.
- ch1 would be the only channel clear of `limonero`'s ch6, but nothing measured supports it, and
  retries are equally bad on both channels, so a move alone may not help.
- If revisited: do a planned 2.4 GHz scan on `cerezo` with the owner's go-ahead, and pin `limonero`
  to ch6 first (auto channel could land on 1 or 11).

### Related changes

- ESPHome flash tooling (`make flash-device`, `flash-device-migrate`, `device-logs`, a single
  `ESPHOME_VERSION` pin kept in sync with the dashboard image by Renovate in one grouped PR): see
  `apps/esphome/config/README.md`.
- Fleet hardening (OTA encryption reusing the API key, digest auth on the web servers), tracked in
  `planning/esphome-security-hardening-todo.md`. The fleet was flashed on 2026-11-12 except offline
  devices. Trade-off: OTA encryption reuses the API key, so anything that holds that key can flash
  devices.

## Recommended Follow-ups, in Order

1. Power-cycle the mini at its breaker, then read its Reset Reason / Device Info in Home Assistant.
   It distinguishes power-on, watchdog/exception and software restart (the 15 min API timeout).
2. If the fan keeps dropping after 48 h: lengthen or disable the group rekey on the fan's IoT SSID
   only.
3. Optional mini experiment: `wifi: phy_mode: 11G`.
4. Add a ping probe for the mini in `system/monitoring/blackbox-exporter-values.yaml` to separate a
   hung API from lost Wi-Fi.
5. Consider a UniFi exporter (e.g. unpoller) for per-client RSSI, retries and channel utilisation
   over time.
6. A planned 2.4 GHz scan, if channel moves are reconsidered (see the decision above).
7. Long term: replace/relocate `cerezo` or add a third AP. Caveat: the evidence that `cerezo` is
   faulty is weak.

## How to Diagnose

Prometheus (read-only):

```promql
sum by (target) (changes(probe_success{job="blackbox-exporter"}[7d]))
min_over_time(probe_success{target=~"fan-office-a|mini-switch-office-a"}[1h])
```

Then correlate the outage start with AP logs.

- **Collect AP logs first, and before rebooting an AP.** AP logs are RAM-only: the `cerezo` reboot
  (manual, ~22:34:10Z; uptime 591 s at 22:44:01Z, boot-log clock ~90 s early before NTP sync)
  destroyed the pre-reboot log. `limonero`'s buffer holds only ~7 h. SSH to the AP and copy
  `/var/log/messages` and `messages.old` (or `logread`).
- Look in the AP logs for `stamgr kick-sta ... reason:`, `wpa_group_rekey`, `MacTableMaintenance`
  and DEAUTH reason codes 3/16/2. The controller event log has **no disconnect reason** (legacy
  `stat/event` returns empty); the AP logs do.
- **UniFi API.** The prefix on this controller is `/api/s/default/...` (`/proxy/network/...` returns
  404). `stat/device` has `min_rssi_enabled`/`min_rssi` (-67 dBm = rssi 28 at noise -95; -75 dBm =
  rssi 20). `stat/rogueap` lists neighbours only on the AP's own channel; `stat/spectrum-scan` is
  empty unless a scan was run.
- Avoid `iwpriv ra3 show stainfo` on `limonero`: it segfaulted that process.
- Tool hygiene: a controller tool response can expose the session cookie/CSRF token in request
  headers. Treat tool output as sensitive and log out afterwards. The firefox-devtools MCP can be
  locked by another WebDriver session (one at a time).
- **Probing from the workstation does not work**: it cannot reach the IoT VLAN (pings fail even for
  healthy Shellys). Use the blackbox exporter's view or a host on the VLAN.
- Prometheus has no UniFi/AP metrics, so AP-side history exists only in RAM logs (hence follow-up 5).
- Timezones differ: UniFi events are UTC, AP logs CET, Home Assistant logs local.
- Mini switch device logs: `make device-logs DEVICE=mini-switch-office-a`, see the runbook in
  `apps/esphome/config/README.md` (needs `secrets.yaml` from the `esphome-secrets` secret).

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
- **Transmit power and channel moves.** Not supported by the data (see the decision above).
- **"`cerezo` is unhealthy because of its short uptime."** Wrong: that was the manual reboot. The
  ~20k cumulative beacon-stuck/HAL resets since boot remain unexplained but unproven harmful. After
  the reboot, pstore was empty with no panic/watchdog/reset-cause lines.
- **The `cerezo` reboot as the cause of the 22:26-22:35Z outage.** Both devices were down from 22:26Z,
  before the ~22:34Z reboot, so it did not cause that outage (cause unknown).
- **AP-wide faults.** No crash, watchdog or radio reset on `limonero` (139 d uptime, no channel
  changes, Radio AI disabled). `cerezo` logs frequent `rx_clear=99/100` interference lines, but the
  reset counters did not move over 30 s and are not tied to incidents. The Nov 4 16:42-16:43Z event,
  where four clients left `cerezo` within seconds (one at -85 dBm), looks like a `cerezo` radio
  disturbance.
- A probe failing does not prove the cause: the mini's probe hits the API port (not lost Wi-Fi
  proof), and the fan's ping failing does not prove a power loss. Check the AP kick/deauth reason
  first.
