# Debugging Wi-Fi Clients on the UniFi Network

Generic runbook for a Wi-Fi client that keeps dropping (IoT device, plug, fan controller) on the
UniFi APs. Everything here is read-only until the last section. Worked example with real numbers and
dead ends: [Office A IoT Wi-Fi flapping](office-iot-wifi-flapping.md). Controller basics (adoption,
SSH access): [UniFi](../deployment/unifi.md).

## Problem

A client fires `BlackboxProbeFailed` or shows up/down flaps in Home Assistant, and the question is
whether it is the client, the radio link or the AP. The UniFi controller does not answer that
directly, so work down the evidence ladder below and stop as soon as a source gives a reason.

## Evidence Ladder

### 1. Prometheus blackbox view (when, how long, how often)

```promql
sum by (target) (changes(probe_success{job="blackbox-exporter"}[7d]))
min_over_time(probe_success{job="blackbox-exporter",target="<target>"}[5m])
```

- Count transitions per target first (`changes`), then run the second as a range query with step 5m:
  each `0` is a 5-minute-down window, consecutive zeros give the outage length.
- **Always `sum by (target)`**: a blackbox pod replacement starts new series, so the same target is
  split over several `instance`/`pod` series and a plain `changes` undercounts.
- The probe interval is 30 s: every start/end time is +-30 s. Do not read finer than that.
- **ICMP vs TCP.** Check the probe module in `system/monitoring/blackbox-exporter-values.yaml`.
  The ESPHome probes are TCP 6053, the API port, so a hung API (or a self-reboot) looks like lost
  Wi-Fi. Add a ping probe for the device to tell the two apart.
- Prometheus has **no UniFi/AP metrics** (no per-client RSSI, retries or AP channel history). AP-side
  history exists only in the RAM logs below. Follow-up idea: a UniFi exporter (e.g. unpoller).
- This workstation cannot reach the IoT VLAN (pings fail even for healthy devices): use the blackbox
  view or a host on the VLAN, never "I cannot ping it" as evidence.

### 2. UniFi controller (who is on which AP, radio settings)

Through the firefox-devtools MCP (one WebDriver session at a time) against the controller UI.

- The event log has **no disconnect reason field**. Do not look for one.
- On controller 10.0.162 the API prefix is `/api/s/default/...`; `/proxy/network/...` returns 404.
- Useful read-only GETs:

  | Endpoint | Gives |
  |---|---|
  | `stat/sta` | per client: AP, signal, rate, retries, uptime |
  | `stat/device` | per AP radio table: channel, TX power, `min_rssi_enabled`/`min_rssi` (dBm), interference, retries |
  | `stat/rogueap` | neighbour APs, only on the AP's own channel |
  | `stat/spectrum-scan` | empty unless a scan was run. **A scan disrupts the AP's clients: do not run one casually** |

- Tool hygiene: tool output can expose the session cookie and CSRF token in request headers. Treat
  it as sensitive and log out afterwards.

### 3. AP logs over SSH (source of truth for reasons)

`ssh cerezo` / `ssh limonero` (key auth, read-only commands: `cat /proc/uptime`, `grep`, `wlanconfig`).
Logs are `/var/log/messages` and `messages.old`.

- **Collect them BEFORE rebooting an AP**: they are RAM-only and lost on reboot; `limonero` keeps
  only ~7 h. Copy them off first, and do not reboot an AP before a planned check.
- `cerezo` is an old BZ2 whose dropbear needs legacy algorithms. They come from the host entry in
  `~/.ssh/config` (`PubkeyAcceptedKeyTypes +ssh-rsa`, `HostkeyAlgorithms +ssh-rsa`,
  `KexAlgorithms +diffie-hellman-group1-sha1`). A tool that ignores that file (paramiko, sshpass
  with default options) fails in key exchange: use plain `ssh`.
- Times: AP logs are CET (UTC+1), UniFi and Prometheus are UTC, Home Assistant is local. An AP that
  just booted has a clock that is not NTP-synced yet (about 90 s off for `cerezo`).
- Avoid `iwpriv ra3 show stainfo` on `limonero`: it segfaulted that process.

Lines worth knowing:

| Log line | Meaning |
|---|---|
| `stamgr kick-sta-on ... (reason:Low RSSI rssi:N)` | The min-RSSI setting kicked the client. The UniFi slider is dBm, the log is rssi above the noise floor: -67 dBm = rssi 28, -75 dBm = rssi 20 at a -95 dBm floor |
| `keep-alive ... silent for 60-sec` | The client stopped acknowledging frames for 60 s |
| `MacTableMaintenance ... had left` | The AP removed the station after the silence |
| `tx continue fail cnt` | Consecutive failed transmissions: the AP cannot reach the client |
| `group key handshake failed (RSN) after 4 tries` | The client did not answer the WPA group rekey. `wpa_group_rekey=3600` runs hourly at a fixed second (hh:01:2x on `limonero`, hh:08:33 on `cerezo`): weak clients that drop at the same second every hour |
| `rx_clear=99/100, rx_frame=0` | Non-Wi-Fi energy on the channel (interference), not traffic |

DEAUTH reason codes: **3** station leaving (client side, e.g. it rebooted or gave up), **16** group
key handshake timeout (see above), **2** previous authentication no longer valid.

### 4. Device side

For ESPHome devices: the `debug:` sensors (Reset Reason, Device Info, Heap Free, Loop Time) and
`make device-logs DEVICE=<device>`. See the Agent Runbook in `apps/esphome/config/README.md`
(reboot reasons, flashing). Third-party devices (Tuya, Shelly) usually have nothing: use the Home
Assistant integration logs in Loki.

## What the Signals Mean

| Observation | Likely meaning |
|---|---|
| Kick with `reason:Low RSSI` | The min-RSSI setting. Check `stat/device` `min_rssi` and the client signal on that AP |
| Silent 60 s, then removed, no kick, no stamgr action | The station stopped acknowledging frames: device hang, weak uplink, or power. Not visible from the AP |
| Deauth reason 16 at hh:01 / hh:08 | Group rekey failed on a weak or sleeping client |
| Deauth reason 3 plus a device-side reboot at the same time | The device restarted itself (check Reset Reason) |
| UniFi drops at -46..-59 dBm | Not a signal kick; look at the device (hang, own reboot) |
| High AP TX retries on both APs and channels | Link margin or interference, not one AP |
| Client never tries to associate again after removal | Device radio hung or device off: the AP side is fine |

## Before Changing Wi-Fi Settings

- Read-only first; one change at a time, so the next 48 h of data answers one question.
- No channel or TX-power change without a (planned) 2.4 GHz scan and a pinned neighbour channel
  (auto channel can land on the same one).
- AP TX power only improves the downlink (what the device hears). If the evidence is a weak uplink
  or a silent client, raising it does not help.
- UniFi changes are outside git: record the change, time and values in the incident doc.
- Owner approval for every live change; announce which clients can be affected (provisioning
  takes about 45 s).
- Verify the units of a setting before applying it (dBm in the UI vs rssi in the log).

## Method Lessons

- Correlate timestamps across independent sources (probe, UniFi, AP log, device) before proposing a
  cause; mind the CET/UTC offset and the 30 s sample interval.
- State hypotheses as hypotheses. Three were retracted in the Office A case (relay dependency, an
  hh:01 job in the repo, "`cerezo` is unhealthy because of its short uptime").
- Sample intervals bound timing precision: do not infer ordering inside one interval.
- Avoid power or channel changes without evidence for them; they move the problem and destroy the
  baseline.
- Verify units (dBm vs rssi) before applying a threshold.
- Read-only first, one change at a time.
- Document dead ends in the incident page so the next agent does not repeat them.
