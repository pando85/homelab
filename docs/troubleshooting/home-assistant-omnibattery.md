# Home Assistant: Omnibattery and the house battery

The house battery (Marstek Venus E V3, `venus` in Home Assistant) is driven by the Omnibattery
integration (HACS, v1.6.0 since 2026-11-10, Modbus TCP). battery-optimizer controls it through
Omnibattery's entities in `live` mode; deployment, rollout and rollback are in
[`docs/deployment/battery-optimizer.md`](../deployment/battery-optimizer.md#live-battery-control).

## Version and upgrade

Installed: **v1.6.0** (stable) since 2026-11-10, upgraded by the owner through HACS from the v1.5.1b1 beta
(installed 2026-11-04), from <https://github.com/ffunes/omnibattery>. v1.6.0 contains:

- The #723 fix for the backup latch. It did not remove the need for the 500 W backup threshold, see
  [Backup-port threshold (500 W)](#backup-port-threshold-500-w).
- The grid-target bound by the contracted power (max 5000 W) of #597/#598 (identical code to the beta
  installed for upstream issue #598, where v1.5.0 capped the PD grid target at 2500 W, so charging was at
  most 2500 W minus the house load).

Stay on stable releases. The upstream 1.6.1 betas are not installed. The optimizer's contracts with
Omnibattery (entity ids, bounds, the weekly full-charge switch and day select, `integration_status`
values) were re-verified against the v1.6.0 source on 2026-11-11 with no change needed in the optimizer;
re-verify them on every upgrade, stable or beta (the checklist is in the optimizer repo,
`docs/planning/periodic-full-charge.md`, section 2). Also check that the target bound by the contracted
power is still in the new release, and that the backup threshold is still 500 W after the upgrade.

## Modbus access

The Venus accepts one Modbus TCP client, Omnibattery. Other clients are refused, so do not run
read-only Modbus probes while Omnibattery is connected.

When Modbus fails, Omnibattery keeps the last SOC and AC values; the only visible change is
`sensor.omnibattery_non_responsive_batteries` going from `None` to `venus`.

## Backup-port threshold (500 W)

`number.venus_backup_offgrid_threshold` is **500 W**, permanently. It is a configuration knob, not a
workaround for a bug. Never set it back to 50 W (the default). The servers on the backup port draw about
220 to 280 W, so 500 W leaves margin. If the backup-port load grows, keep the threshold above it with
margin (the maximum, 2500 W, is fine: the battery is then never excluded).

### Why

`backup_mode` (exclusion from Omnibattery's PD control) is designed for whole-house backup, where all
battery output goes through the uncontrollable backup port. For a permanent small load on that port the
maintainer's answer is to set the threshold above the load. This is by design, with no brand-specific fix
([ffunes/Omnibattery#660](https://github.com/ffunes/Omnibattery/issues/660), maintainer clarification
2026-11-12; our fix PR #861 was closed). v1.6.0 contains #723, but it did not change this.

### Incident of 2026-11-11

The threshold was set from 500 to 50 W at 22:17 UTC. At 22:45 UTC a planned grid charge took the inverter
out of Bypass; the servers' ~240 W exceeded 50 W, so Omnibattery went to `backup_mode` and its 5-minute
cooldown was refreshed continuously. While excluded, Omnibattery skips all power writes, so the last
command kept running even with the allow switches off: about 8 h of charging at a fixed 533 W (SOC 20 to
100 %), then, after one healthy tick at 06:50 UTC, a fixed ~110 W discharge until the threshold went back
to 500 W at 12:04 UTC. The status cleared about 12:10 UTC and the optimizer resumed.
`BatteryOptimizerActuatorSafeIdle` fired.

### How to diagnose

`sensor.omnibattery_integration_status` is `backup_mode` with the grid present (inverter in Bypass or
the AC plug on) for more than 5 minutes:

- compare `sensor.venus_ac_offgrid_power` with `number.venus_backup_offgrid_threshold`; the load is above
  the threshold, so raise the threshold above it with margin;
- while excluded, Omnibattery leaves the last power command running even with the allow switches off, so
  check the battery's real power, not the switches;
- battery-optimizer: `battery_optimizer_actuator_guard_reason{reason="integration_unhealthy"} == 1`,
  and `BatteryOptimizerActuatorSafeIdle` after 30 minutes.

### Outage behaviour (not verified)

With the threshold above the backup load, a grid outage with only the servers on the backup port does not
show as `backup_mode`. The optimizer's behaviour during an outage under this setting is not verified: the
supervised grid-loss test (briefly cut AC with the servers on the backup port, owner's choice of date) is
still open. Record its result here and in the optimizer repo's regime log.
