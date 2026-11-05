# Home Assistant: Omnibattery and the house battery

The house battery (Marstek Venus E V3, `venus` in Home Assistant) is driven by the Omnibattery
integration (HACS, v1.5.0, Modbus TCP). battery-optimizer controls it through Omnibattery's entities
in `live` mode; deployment, rollout and rollback are in
[`docs/deployment/battery-optimizer.md`](../deployment/battery-optimizer.md#live-battery-control).

## `integration_status` stuck in `backup_mode` after the grid returns

### Problem

With the backup function on and a load on the battery's backup (EPS) port, Omnibattery can keep
`sensor.omnibattery_integration_status` at `backup_mode` after AC is back and the inverter is in
**Bypass**. While it does, Omnibattery excludes the battery from PD control, and battery-optimizer's
guard holds safe idle (`integration_unhealthy`), so the battery does nothing.

Seen in the commissioning test on 2026-11-03: a 57-85 W load on the backup port kept the status at
`backup_mode` until the threshold below was raised.

### Root cause

Upstream bug [ffunes/Omnibattery#660](https://github.com/ffunes/Omnibattery/issues/660), fixed by
[ffunes/Omnibattery#723](https://github.com/ffunes/Omnibattery/pull/723). In Bypass the grid feeds the
backup port, so Omnibattery should treat the port as idle, but only when the inverter state and
`ac_offgrid_power` come from the same poll. The coordinator stamps the reading times in the middle of
a poll and publishes the data later, so the same-poll check fails and the Bypass zeroing is skipped.
The backup-port load then counts as off-grid load; above `number.venus_backup_offgrid_threshold`
(default 50 W) it re-arms the 5-minute post-backup cooldown on every poll, and the cooldown never runs
out.

### How to diagnose

- `sensor.omnibattery_integration_status` is `backup_mode` while the inverter reports Bypass (AC
  present) for more than 5 minutes.
- `sensor.venus_ac_offgrid_power` is above `number.venus_backup_offgrid_threshold`.
- battery-optimizer: `battery_optimizer_actuator_guard_reason{reason="integration_unhealthy"} == 1`,
  and `BatteryOptimizerActuatorSafeIdle` after 30 minutes.

### Workaround (active since 2026-11-03)

`number.venus_backup_offgrid_threshold` = **500 W** (set by the owner in Home Assistant; 200 W during
the test). A backup-port load below the threshold never re-arms the cooldown, so the status returns to
`balanced` within 5 minutes of AC coming back.

Side effects of the high threshold, until it is set back to 50 W:

- **A real outage with a small backup load is not detected as backup.** With a backup-port load of
  500 W or less, Omnibattery does not exclude the battery from PD control during a grid outage and
  the status does not change to `backup_mode`, so neither Omnibattery's exclusion nor the optimizer's
  `integration_unhealthy` guard reacts. (The optimizer's other guards still apply; in a real outage
  the grid meter and usually Home Assistant are down too.)
- **No post-backup cooldown below 500 W.** After an outage, or after the AC failsafe plug turns AC
  back on, Omnibattery resumes PD writes at once instead of waiting 5 minutes.
- **Shutdown standby is not skipped.** On an Omnibattery unload or Home Assistant stop, the battery
  is sent to standby and RS485 control is released unless the backup-port load is above the
  threshold; with 500 W that happens even while the battery is feeding a small backup load.
- **The workaround only covers loads up to 500 W.** A larger backup-port load in Bypass still hits
  the latch. Keep the backup-port load (servers, once moved there) under 500 W or remove the
  workaround first.

### Removing the workaround

Once an Omnibattery release that contains #723 is installed (check the release notes, then update
through HACS and restart Home Assistant):

1. Set `number.venus_backup_offgrid_threshold` back to **50 W**.
2. Verify with a load on the backup port while the inverter is in **Bypass**: either the existing
   backup load, or briefly cut AC (the battery's AC plug off, then on) with a test load connected.
3. `sensor.omnibattery_integration_status` must return to `balanced` within **5 minutes** of AC being
   back and stay there. If it stays at `backup_mode`, set the threshold to 500 W again and reopen
   the upstream issue.
4. Update this page and the battery-optimizer deployment doc.
