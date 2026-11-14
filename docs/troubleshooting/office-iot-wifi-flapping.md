# Office A IoT Wi-Fi Flapping (fan-office-a, mini-switch-office-a)

Investigated 2026-11-12. All times UTC (AP logs are CET, UTC+1). **Status after the 48 h follow-up
(2026-11-14): the fan is partially fixed (improving), the mini switch is not fixed.** Both devices were
power-cycled on Nov 12 (~22:55Z); see [Follow-up on 2026-11-14](#follow-up-on-2026-11-14). Next step: new
mini firmware on Nov 15, see [Next step](#next-step-planned-2026-11-15).

## Problem

`BlackboxProbeFailed` fired repeatedly for `fan-office-a` (ICMP ping) and `mini-switch-office-a`
(TCP 6053, the ESPHome API port). Config: `system/monitoring/blackbox-exporter-values.yaml`.

- `fan-office-a`: 344 up/down changes in 7 d, outages of 35-243 min. Failures start 2026-11-03
  ~13:07Z (probe-down count per day from ~0 to 100-558).
- `mini-switch-office-a`: ~100% up until Nov 4, down from Nov 5 21:08Z, ~99% down since Nov 8 with
  brief recoveries (e.g. down Nov 12 09:25-20:41Z).
- Only the Wi-Fi IoT devices in Office A flap (fan, mini switch, a couple of Shellys/plug). Wired and
  cluster targets are stable. Shelly/SU100 devices stay associated (1-3 drops in 15 d). SU100 devices
  fail 20-30 times/day since at least Oct 3: unrelated baseline, exclude them.

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
  -63..-65. Offline gaps per UniFi events: ~35 h, ~3.4 d, ~1.6 d, ~11 h (Nov 12 09:26-20:41); first
  drop Nov 2 19:24Z.
- After the owner's flash the mini came back, then was down again from 22:26Z (the fan recovered at
  ~22:38Z): an AP restart did not bring it back.
- Interpretation: the ESP radio hangs and the device reboots itself. `wifi.reboot_timeout` is 24 h
  (`apps/esphome/config/packages/connection.yaml`) but `api.reboot_timeout` is not overridden, so
  ESPHome's default of 15 min is the likely trigger (not verified). The reset reason was not logged
  before the flash.
- The probe targets the API port, so it cannot tell a hung API from lost Wi-Fi. There is no ping
  probe for this device.

### Common pattern

For the mini (21:24-21:26Z on `limonero`) and the fan (21:51-21:52Z on `cerezo`), the AP only sees
the station go silent and get removed. The cause is not visible from the APs: radio hang, firmware or
power.

## Fix / Workaround

### Applied 2026-11-12 (live change outside git, owner approved)

On `cerezo`'s 2.4 GHz radio only, Minimum RSSI was lowered from -67 dBm to -75 dBm (rssi 28 -> 20 at
~-95 dBm noise) at 21:32:53Z. Provisioning took ~45 s (21:33:04-21:33:47Z), no reboot, all clients
stayed. The UniFi UI slider is in dBm (-67..-90). The save also added `assisted_roaming_enabled:
false` as a default. No other AP or SSID setting changed.

### Result

- **Interim: it did not stop the outages.** The fan stayed on `cerezo` ~47 min through the change,
  then dropped at 21:51-21:52Z with no kick (see cause 3).
- **48 h verdict (power-cycle Nov 12 22:55Z to Nov 14 ~22:05Z, ~47 h): fan partially fixed, mini not
  fixed.** Details in [Outcome](#outcome-run-2026-11-14-2205z).
  - Fan: 49 probe state changes in 47 h (~180 per 7 d vs 348 baseline, ~48 % fewer, borderline on the
    >50 % criterion). Up continuously since Nov 13 ~18:25Z (~27.5 h clean). Whether the min-RSSI change,
    the power-cycle or the reboot of `cerezo` helped is not separable.
  - Mini: TCP 6053 up only ~6 min after boot and ~10 min on Nov 13 16:41Z, down ~47 h of 47 h 20 min.
    It never self-recovered, so the API-timeout reboot is not the mechanism (see the Outcome).

### ESPHome upgrade (mini switch), flashed 2026-11-12

Flashed 2026.10.2 with `debug:` sensors (reset reason, device info, heap free, heap max block, loop
time; ~516 B RAM, ~5.3 KB flash) to record why it reboots. The sensors are readable only while the
device is reachable.

The upgrade alone is **not expected to fix it** (~20-30%, low confidence):

- Same Arduino ESP8266 core 3.1.2 and Wi-Fi blob in 2025.10.2 and 2026.10.x; no release note from
  2025.10.2 to 2026.10.1 describes an ESP8266 Wi-Fi hang fix (PR 18333 only fixes an mDNS crash).
- Unresolved upstream reports of the same symptom: esphome issues 16724 (API dropped while ping still
  works) and 14999 (API handshake timeout, weak Wi-Fi), ratgdo issue 541 (off Wi-Fi for hours, helped
  by `wifi: phy_mode: 11G`, available since 2026.5.0).
- Fleet comparison: the two ESP8285 smart plugs run 2026.10.x on the identical Wi-Fi config and one
  is stable (0 flaps in 7 d), so the same firmware can hold Wi-Fi.

Not done, optional: `phy_mode: 11G`, an `api: reboot_timeout` tweak.

### RF environment and the channel/power decision

Read-only from the controller. No scan was stored: a spectrum scan disrupts the AP's clients, so it
was **not run**.

- `stat/spectrum-scan` is empty for both APs. Neighbour data exists only on each AP's own channel:
  `limonero` ch6 sees ~309 neighbour BSSIDs (298 weaker than 10 dB SNR, only 4 at 20 dB or more);
  `cerezo` ch11 sees 21 (all weak, strongest 25 dB SNR) plus 7 on ch10. Noise floor -96 dBm on both.
- Channel busy: `limonero` ~29-30% (17 clients, 29% TX retries); `cerezo` 5-21% depending on the
  panel, TX retries ~26%, 1 client. About 14 points of the 21% are neither its own rx nor tx:
  non-Wi-Fi energy, matching the `rx_clear=99/100, rx_frame=0` log lines.

**Decision (owner agreed): do not change `cerezo`'s channel or TX power.**

- AP TX power only improves what the devices hear (downlink); the evidence is stations going silent
  and a weak uplink (fan ~-70 dBm at `cerezo`). `limonero` is already at high power and the mini
  still dropped there.
- ch1 would be the only channel clear of `limonero`'s ch6, but nothing measured supports it and
  retries are equally bad on both channels.
- If revisited: do a planned 2.4 GHz scan on `cerezo` with the owner's go-ahead, and pin `limonero`
  to ch6 first (auto channel could land on 1 or 11).

### Related changes

- ESPHome flash tooling (`make flash-device`, `flash-device-migrate`, `device-logs`, one
  `ESPHOME_VERSION` pin synced with the dashboard image by Renovate): `apps/esphome/config/README.md`.
- Fleet hardening (OTA encryption reusing the API key, digest auth on the web servers), tracked in
  `planning/esphome-security-hardening-todo.md`; flashed on 2026-11-12 except offline devices. Anything
  holding the API key can flash devices.

## Follow-up on 2026-11-14

Run on Nov 14 ~22:05Z (~47 h after the power-cycle). Everything was read-only. Do **not** reboot an AP
or device before the AP logs are copied. The checks, baseline and decision tables below are kept for
re-runs; the outcome comes first.

### Outcome (run 2026-11-14 ~22:05Z)

Window: Nov 12 22:55Z power-cycle to Nov 14 ~22:05Z.

**Fan: partially fixed, improving.**

- Blackbox: 49 state changes in 47 h (~180 per 7 d vs 348), ~48 % fewer.
- 5-min down windows: Nov 12 23:05 - Nov 13 00:10 (~65-70 min, right after the power-cycle), Nov 13
  07:30-07:35, a 13:15-14:15 cluster (longest ~25 min, 13:45-14:05), 14:55, 16:45-16:50, 18:20.
  Continuously up since Nov 13 ~18:25Z.
- Loki (check d): 23 `Disconnected`, 20 `Failed to connect`, 4 `Initial state update failed`. Last fan
  line Nov 13 18:16:29Z, none in the following ~28 h.
- Nov 13 16:40Z: the fan and several other localtuya devices disconnected within seconds and the mini
  briefly came back at 16:41Z, which points to a shared Wi-Fi/AP event, not a fan fault.
- Now on `cerezo` (IoT VLAN 501, `ath2`; on `limonero` `ra1`/`rai1`) at rssi 28 (~-67 dBm), above the
  -75 dBm threshold, associated since ~Nov 13 18:17Z, with only the hourly group-key handshakes (all
  completed; `cerezo` now rekeys at hh:35:02). Station identity inferred from VLAN and association time,
  not confirmed with the controller.
- Cause of the Nov 13 drops unknown: the AP logs did not reach back (`cerezo` covered Nov 14 17:07-22:05Z,
  `limonero` only 20:34-22:05Z).

**Mini switch: not fixed.**

- Blackbox TCP 6053: up only Nov 12 22:56-23:00 (~6 min after the 22:54:44 boot) and Nov 13
  16:41-16:51, down the rest (~47 h of 47 h 20 min). Only 2 probe changes.
- HA `esphome` warnings `Can't connect to ESPHome API ... Timeout while connecting` at Nov 12 23:02:37
  and Nov 13 16:53:28.
- It never self-recovered. If Wi-Fi stayed up, the default 15 min `api.reboot_timeout` should have
  rebooted it and briefly brought the probe back. It did not, so this is a hard radio/Wi-Fi hang (or the
  device not rebooting): the "Mini hangs again" row of the decision table.
- Not readable: Reset Reason and the other debug sensors (device offline, `make device-logs` timed out,
  no HA API token to read entity history). Its MAC was not identified, so the AP logs cannot say whether
  it is still associated; no leave/kick/deauth/tx-fail lines for any client on IoT VLAN 502 in the covered
  window (`cerezo` `ath4`, `limonero` `ra3`).

**APs.** `cerezo` uptime 47.5 h (the manual reboot), `limonero` 142 d. No AP-wide events (no radio
resets, crashes or mass drops). Low-RSSI kicks and one group-key failure on `cerezo` hit non-IoT LAN
clients, not these devices.

**Lessons.** AP log retention is too short to explain drops after the fact (`limonero` ~1.5 h, `cerezo`
~5 h this time): copy logs within ~1 h of a drop, or add a UniFi exporter (unpoller) for history.

### Baseline at the start of the window

- **Power-cycle:** the owner power-cycled both devices at ~22:52-22:55Z on Nov 12. The mini booted at
  ~22:54:44Z with Reset Reason `External System` (the power-cycle itself, so it says nothing about
  earlier hangs), ESPHome 2026.10.2 with debug sensors. It associated on `limonero` at ~-63 dBm
  AP-side / -69 dBm device-side, ~2 % retries, no disconnects in a 90 s log capture.
- **Fan:** back at ~22:55Z, probably on `cerezo`; `limonero` still held a silent stale entry for it at
  -84..-88 dBm.
- **Settings in effect:** `cerezo` 2.4 GHz min RSSI -75 dBm since Nov 12 21:32:53Z (no channel/power
  change; `cerezo` rebooted manually at ~22:34:10Z). ESPHome fleet on 2026.10.2, OTA encryption required.
- **Probe state changes in the 7 d before** (compare a 48 h count x 3.5):

| Target | Changes / 7 d | Note |
|---|---|---|
| `fan-office-a` | 348 | outages of 35-243 min |
| `mini-switch-office-a` | 21 | ~99 % down since Nov 8 |
| `shellyplus2pm-office-a` / `-office-t` | 78 / 82 | same-room comparison |
| `shellypmmini-z800` / `smart-plug-v2-snapmaker` | 176 / 94 | comparison |

Exclude the SU100 devices (~150 each, baseline since Oct 3).

### Checks

a. **Prometheus** (datasource `prometheus`). The blackbox pod may have been replaced, so aggregate by
   `target`:

   ```promql
   sum by (target) (changes(probe_success{job="blackbox-exporter",target=~"fan-office-a|mini-switch-office-a|shellyplus2pm-office-a|shellyplus2pm-office-t"}[48h]))
   max by (target) (min_over_time(probe_success{job="blackbox-exporter",target=~"fan-office-a|mini-switch-office-a"}[5m]))
   ```

   Run the second as a 48 h range query, step 5m: each `0` is a 5-minute-down window, 2 or more
   consecutive zeros is an outage longer than 5 min. Count the windows and the longest run.
b. **Mini in Home Assistant:** the Reset Reason, Device Info, Heap Free, Heap Max Block and Loop Time
   sensors and their last-changed times. A changed Reset Reason means it rebooted by itself:
   `Software/System restart` fits the 15 min API timeout, a watchdog/exception reason fits a firmware
   hang, `External System` or `Power-on` fits a power event. Uptime: `cd apps/esphome/config && timeout 90
   make device-logs DEVICE=mini-switch-office-a`.
c. **APs over SSH, before anything is rebooted** (RAM-only logs, `limonero` keeps ~7 h): `ssh cerezo` /
   `ssh limonero` (key auth, read-only commands such as `cat /proc/uptime`, `grep`, `wlanconfig`). In
   `/var/log/messages` and `messages.old` look for `Low RSSI` kicks, `group key handshake failed`,
   `tx continue fail` and DEAUTH reason codes per client (3 / 16 / 2). Note which AP each client is on.
d. **Fan in Loki** (namespace `home-assistant`, 48 h): localtuya lines for the fan. Real message
   texts (HA logs only WARNING and above, so there are no reconnect lines):
   `Disconnected - waiting for discovery broadcast`, `Failed to connect to fan-office-a.iot.grigri:
   [Errno 110] Connect call failed`, and ERROR `Initial state update failed, giving up: TimeoutError()`:

   ```logql
   {namespace="home-assistant"} |= "fan-office-a" |~ "Disconnected - waiting|Failed to connect|Initial state update failed"
   ```
e. **UniFi client view** for the two devices (AP, signal, retries), through the firefox-devtools MCP,
   API prefix `/api/s/default` (see How to Diagnose for hygiene).

### Decision

Success criteria against the baselines above:

| Outcome | Criteria over 48 h |
|---|---|
| Fixed | both devices up, at most a handful of transitions each, no outage longer than 5 min |
| Partially | fan or mini improved by more than 50 % (changes, or down-time share for the mini) but still dropping |
| Not fixed | anything else |

Actions by evidence (one change at a time):

| Evidence | Action |
|---|---|
| Mini hangs again, no self-reboot, Reset Reason unchanged | Hardware/firmware: replace the mini switch (an ESP32-based one is the obvious swap), or try `wifi: phy_mode: 11G` first. Add a ping probe in `system/monitoring/blackbox-exporter-values.yaml` to separate a hung API from lost Wi-Fi |
| Mini self-reboots with `Software/System restart` | The API-timeout reboot is firing: find out why HA/the probe lost the API (`api: reboot_timeout` default is 15 min; do not disable it) |
| Fan still dropping with `Low RSSI` kicks | Lower or disable `cerezo` min RSSI further, or put an AP closer |
| Fan dropping with group-key failures on `limonero` | Lengthen/disable the group rekey on the fan's IoT SSID only (owner approval, one SSID) |
| Fan silent then gone, no AP reason | Link margin is the problem: move/add an AP closer to the room's ceiling fixture, check the Tuya module position |
| Nothing improves | Consider a UniFi exporter (e.g. unpoller) for per-client RSSI/retries over time, and replacing `cerezo` (old 2.4 GHz-only BZ2; evidence that it is faulty is weak) |

Rules: no channel or TX-power change without a planned 2.4 GHz scan (see the decision above) and a
pinned `limonero` channel; owner approval for any live UniFi change.

### Next step (planned 2026-11-15)

The owner flashes a new firmware version to the mini switch. Per the decision table the candidates are
`wifi: phy_mode: 11G` (available since ESPHome 2026.5.0) and/or a newer ESPHome. One change at a time
where possible.

1. Before power-cycling, check whether the button and LED still respond (a hung radio with a live main
   loop is different from a dead device).
2. Add a blackbox ICMP ping probe for the mini in `system/monitoring/blackbox-exporter-values.yaml`, so a
   hung API can be told from lost Wi-Fi.
3. After flashing, read the Reset Reason sensor and re-run checks a-d.
4. If the mini still hangs, replace it with an ESP32-based switch.

## How to Diagnose

Prometheus (read-only):

```promql
sum by (target) (changes(probe_success{job="blackbox-exporter"}[7d]))
min_over_time(probe_success{target=~"fan-office-a|mini-switch-office-a"}[1h])
```

Then correlate the outage start with AP logs.

- **Collect AP logs first, and before rebooting an AP.** AP logs are RAM-only: the manual `cerezo`
  reboot (~22:34:10Z; boot-log clock ~90 s early before NTP sync) destroyed the pre-reboot log.
  `limonero` keeps only ~7 h. SSH to the AP and copy `/var/log/messages` and `messages.old` (or
  `logread`).
- AP log retention is short (`limonero` ~1.5 h, `cerezo` ~5 h in the 48 h follow-up): copy logs within
  ~1 h of a drop, or add a UniFi exporter (unpoller).
- VAP mapping for the IoT SSIDs: VLAN 501 (fan) is `ath2` on `cerezo` and `ra1`/`rai1` on `limonero`;
  VLAN 502 (mini) is `ath4` on `cerezo` and `ra3` on `limonero`.
- `wlanconfig` is not available on `limonero` (no station list there).
- Look in the AP logs for `stamgr kick-sta ... reason:`, `wpa_group_rekey`, `MacTableMaintenance`
  and DEAUTH reason codes 3/16/2. The controller event log has **no disconnect reason** (legacy
  `stat/event` returns empty); the AP logs do.
- **UniFi API.** The prefix on this controller is `/api/s/default/...` (`/proxy/network/...` returns
  404). `stat/device` has `min_rssi_enabled`/`min_rssi` (-67 dBm = rssi 28 at noise -95; -75 dBm =
  rssi 20). `stat/rogueap` lists neighbours only on the AP's own channel; `stat/spectrum-scan` is
  empty unless a scan was run.
- Avoid `iwpriv ra3 show stainfo` on `limonero`: it segfaulted that process.
- `cerezo` (BZ2, old dropbear) needs legacy SSH algorithms (ssh-rsa host/pubkey, group1 kex), set in
  the owner's `~/.ssh/config` host entry: a plain modern client or a tool that ignores that config
  (paramiko, sshpass with default options) fails during key exchange.
- Tool hygiene: controller tool output can expose the session cookie/CSRF token in request headers:
  treat it as sensitive and log out afterwards. The firefox-devtools MCP allows one WebDriver session.
- **Probing from the workstation does not work**: it cannot reach the IoT VLAN (pings fail even for
  healthy Shellys). Use the blackbox exporter's view or a host on the VLAN.
- Prometheus has no UniFi/AP metrics, so AP-side history exists only in RAM logs (hence the exporter idea in the Decision table).
- Timezones: UniFi events UTC, AP logs CET, Home Assistant logs local.
- Mini switch device logs: `make device-logs DEVICE=mini-switch-office-a`, see the runbook in
  `apps/esphome/config/README.md` (needs `secrets.yaml` from the `esphome-secrets` secret).

## Pitfalls / Dead Ends

Do not repeat these:

- **Shared mains glitch or the Nov 3 battery/AC-failsafe work.** No correlation: fan failures began
  13:07Z, before the first Nov 3 evening commit at 20:51Z; failures do not cluster at hh:01; only the
  fan failed at 20:01 and 21:01 (no Shelly/SU100 dropped). There is no voltage or power series in
  Prometheus to check it directly.
- **Fan powered through the mini switch relay.** Ruled out by the owner. The fan's Errno 111 (fresh
  boot) 4 s after the mini's 20:40:46Z reboot is a coincidence or a shared upstream cause.
- **An hh:01 job in the repo.** Nothing runs at minute 01 (HA `time_pattern` every minute;
  battery-optimizer slots :00/:15/:30/:45). The hh:01 pattern is the WPA group rekey.
- **The blackbox exporter itself.** Probe results match device-side evidence.
- **"`cerezo` is unhealthy because of its short uptime."** Wrong: that was the manual reboot. The
  ~20k cumulative beacon-stuck/HAL resets since boot are unexplained but unproven harmful; pstore was
  empty after the reboot.
- **The `cerezo` reboot as the cause of the 22:26-22:35Z outage.** Both devices were down from 22:26Z,
  before the ~22:34Z reboot (cause unknown).
- **AP-wide faults.** No crash, watchdog or radio reset on `limonero` (139 d uptime, no channel
  changes, Radio AI disabled). `cerezo` logs frequent `rx_clear=99/100` interference lines, but the
  reset counters did not move over 30 s and are not tied to incidents. The Nov 4 16:42-16:43Z event,
  where four clients left `cerezo` within seconds (one at -85 dBm), looks like a `cerezo` radio
  disturbance.
- A failing probe does not prove the cause (the mini's hits the API port): check the AP kick/deauth
  reason first.
