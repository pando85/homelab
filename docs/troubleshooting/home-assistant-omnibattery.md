# Home Assistant: Omnibattery and the house battery

The house battery (Marstek Venus E V3, `venus` in Home Assistant) is driven by the Omnibattery
integration (HACS, v1.6.0 since 2026-11-10, Modbus TCP). battery-optimizer controls it through
Omnibattery's entities in `live` mode; deployment, rollout and rollback are in
[`docs/deployment/battery-optimizer.md`](../deployment/battery-optimizer.md#live-battery-control).

## Version and upgrade

Installed: **v1.6.0** (stable) since 2026-11-10, upgraded by the owner through HACS from the v1.5.1b1 beta
(installed 2026-11-04), from <https://github.com/ffunes/omnibattery>. v1.6.0 contains:

- The fix for the backup latch, #723 (for #660), so the 500 W workaround was removed on 2026-11-11, see
  [Workaround (removed 2026-11-11)](#workaround-removed-2026-11-11).
- The grid-target bound by the contracted power (max 5000 W) of #597/#598 (identical code to the beta
  installed for upstream issue #598, where v1.5.0 capped the PD grid target at 2500 W, so charging was at
  most 2500 W minus the house load).

Stay on stable releases. The upstream 1.6.1 betas are not installed. The optimizer's contracts with
Omnibattery (entity ids, bounds, the weekly full-charge switch and day select, `integration_status`
values) were re-verified against the v1.6.0 source on 2026-11-11 with no change needed in the optimizer;
re-verify them on every upgrade, stable or beta (the checklist is in the optimizer repo,
`docs/planning/periodic-full-charge.md`, section 2). Also check that the target bound by the contracted
power and the #723 fix are still in the new release.

## Modbus access

The Venus accepts one Modbus TCP client, Omnibattery. Other clients are refused, so do not run
read-only Modbus probes while Omnibattery is connected.

When Modbus fails, Omnibattery keeps the last SOC and AC values; the only visible change is
`sensor.omnibattery_non_responsive_batteries` going from `None` to `venus`.

## `integration_status` stuck in `backup_mode` after the grid returns

Status: **fixed in Omnibattery v1.6.0** (installed 2026-11-10); workaround removed 2026-11-11; the supervised
grid-loss test is pending. The sections below describe the problem as it was seen on v1.5.0 and v1.5.1b1.

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

### How to diagnose (if it comes back)

- `sensor.omnibattery_integration_status` is `backup_mode` while the inverter reports Bypass (AC
  present) for more than 5 minutes.
- `sensor.venus_ac_offgrid_power` is above `number.venus_backup_offgrid_threshold`.
- battery-optimizer: `battery_optimizer_actuator_guard_reason{reason="integration_unhealthy"} == 1`,
  and `BatteryOptimizerActuatorSafeIdle` after 30 minutes.

### Workaround (removed 2026-11-11)

From 2026-11-03 to 2026-11-11 `number.venus_backup_offgrid_threshold` was **500 W** (set by the owner in
Home Assistant; 200 W during the test, default 50 W): a backup-port load below the threshold never
re-armed the cooldown, so the status returned to `balanced` within 5 minutes of AC coming back. The cost
was that a real outage with a backup load of 500 W or less was not detected as backup, there was no
post-backup cooldown, and the shutdown standby was not skipped while the battery fed a small backup load.

Omnibattery v1.6.0 (installed 2026-11-10) contains #723, and the owner set the threshold back to **50 W**
(the default) on 2026-11-11 at 23:17 local. With 50 W those side effects are gone: small-load outages are
detected as backup again, the 5-minute post-backup cooldown is back, and the shutdown standby is skipped
while the battery feeds the servers.

Passive check, passed: in Bypass with 237 to 251 W on the backup port (the servers),
`sensor.omnibattery_integration_status` stayed `balanced` and the optimizer's guard passed for 20
minutes (23:17 to 23:37); before the fix the status latched within 5 minutes.

**Pending: the supervised grid-loss test** (deferred by the owner to a later date). Briefly cut AC (the
battery's AC plug off, then on; or the supervised HA-stop test in the optimizer deployment doc) with the
servers on the backup port, and confirm Backup Mode on loss and `balanced` again after AC is back. Until it
is done, 50 W is verified only by the passive check above. Record the result here and in the optimizer
repo's regime log.

**Rollback:** if `sensor.omnibattery_integration_status` ever stays at `backup_mode` with AC present for
more than 5 minutes, set `number.venus_backup_offgrid_threshold` to **500 W** again (it must stay above
the servers' backup-port load of about 225 to 255 W; the side effects above then apply again) and
reopen the upstream issue.
