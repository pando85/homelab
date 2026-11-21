# Upgrades

## OS upgrades

Managed by `unattended-upgrade` in Debian based distributions and rebooted by `kured` when needed.

Review the update history in `/var/log/unattended-upgrades/unattended-upgrades-dpkg.log`

## k3s upgrades

Managed by [system-upgrade-controller](https://github.com/rancher/system-upgrade-controller).
Increase K3s version in `system/system-upgrade/k3s/kustomization.yaml` file.

Upgrade to the newest patch of the next minor only: the upgrade image refuses binaries built in an
earlier month than the installed one, and new tainted nodes need a toleration in the agent Plan. See
[k3s system-upgrade troubleshooting](../troubleshooting/k3s-system-upgrade-build-date-check.md).
