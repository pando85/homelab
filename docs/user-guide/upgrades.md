# Upgrades

## OS upgrades

Managed by `unattended-upgrade` in Debian based distributions and rebooted by `kured` when needed.

Review the update history in `/var/log/unattended-upgrades/unattended-upgrades-dpkg.log`

## k3s upgrades

Managed by [system-upgrade-controller](https://github.com/rancher/system-upgrade-controller).
Increase K3s version in `system/system-upgrade/k3s/kustomization.yaml` file.
