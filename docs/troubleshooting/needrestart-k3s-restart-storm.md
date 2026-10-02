# needrestart: k3s Restart Storm During Unattended Upgrades

## Problem

On 2026-10-02 unattended-upgrades on `grigri` installed ~50 packages (libc6, openssl, kernel, ...)
between 11:38 and 12:05 local time. After every package, needrestart restarted `k3s.service`:
22 restarts in 24 minutes (`Starting kubelet.` node events about once a minute). The first round
also restarted `systemd-networkd` and `systemd-resolved`. For about a minute, pods on `grigri`
were unreachable from other nodes. Redis Sentinel failed over, the redis-operator reverted the
roles, and Sentinel was left pointing at a replica. isidoro lost Redis for about 30 minutes.

This was not new: `grigri` logs show 10–29 k3s restarts on every upgrade day since at least
2026-04. `k8s-amd64-1` (Ubuntu 26.04) showed none.

## Root Cause

- **k3s restarts repeat.** needrestart before 3.9 (Ubuntu 24.04 ships 3.6) blames an outdated
  process on its parent's systemd unit. Pod processes are children of `containerd-shim`, which
  lives in the `k3s.service` cgroup, so needrestart restarts `k3s.service`. k3s uses
  `KillMode=process`, so the shims and pods survive the restart and k3s is flagged again on the
  next dpkg run. Upstream fixed this in needrestart 3.9 by ignoring processes in other PID
  namespaces ([needrestart#317](https://github.com/liske/needrestart/issues/317)).
- **The network cut.** Cilium uses `routingMode: native` with `autoDirectNodeRoutes: true`, so the
  routes to other nodes' pod CIDRs live in the host routing table. By default systemd-networkd
  removes routes and policy rules it did not create whenever it starts, so restarting it cuts
  the node's pod network until Cilium reinstalls them.

## How to Diagnose

```bash
# Which services needrestart restarted, per dpkg run
kubectl --context=grigri get --raw \
  "/api/v1/nodes/<node>/proxy/logs/unattended-upgrades/unattended-upgrades-dpkg.log" \
  | grep -A2 'Restarting services'

# k3s restarts (kubelet "Starting" node events)
kubectl --context=grigri get events -A --field-selector involvedObject.kind=Node,reason=Starting

# On the node: why needrestart flags k3s (read-only)
sudo needrestart -r l -v 2>&1 | grep -E 'child of|k3s'
```

## Fix

The `prepare` role (`unattended-upgrades` and `networkd` tags) installs:

- `/etc/needrestart/conf.d/50-k8s-node.conf`: `override_rc` entries that defer `k3s.service`,
  `k3s-agent.service`, `systemd-networkd.service` and `systemd-resolved.service`. They show up
  under "Service restarts being deferred" and pick up new libraries at the next reboot.
- `/etc/systemd/networkd.conf.d/50-cilium.conf`: `ManageForeignRoutes=no` and
  `ManageForeignRoutingPolicyRules=no`. It takes effect at the next reboot. Do not restart
  networkd to apply it, because that restart is what flushes the routes.
- `apt-daily-upgrade.timer`: runs at 03:30 (+30 min jitter), just before the kured window
  (05:00–08:00 Europe/Madrid), so kured picks up kernel/libc updates and deferred services the
  same morning.

Apply it with a scoped run:

```bash
cd metal && ANSIBLE_EXTRA_ARGS="-t unattended-upgrades,networkd --limit grigri" make prepare
```
