# Upgrades

## OS upgrades

Managed by `unattended-upgrade` in Debian based distributions and rebooted by `kured` when needed.

Review the update history in `/var/log/unattended-upgrades/unattended-upgrades-dpkg.log`

*Note*: grigri is manually upgraded.

## k3s upgrades

Managed by `ansible` manually but it can be replaced with `system-upgrade` controller.
