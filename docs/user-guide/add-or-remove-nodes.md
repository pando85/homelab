# Add or remove nodes

Or how to scale vertically. To replace the same node with a clean OS, remove it and add it again.

## Add new nodes

!!! tip

    You can add multiple nodes at the same time

### Inventory groups

Add the node to `metal/inventory/hosts.ini`. A worker joins `[amd64_node]` (which flows
`kube_node` → `k3s_cluster`) and `[amd64]`. It must **not** join `kube_control_plane`, `nvidia`
or `ipmi`:

```diff
diff --git a/metal/inventory/hosts.ini b/metal/inventory/hosts.ini
--- a/metal/inventory/hosts.ini
+++ b/metal/inventory/hosts.ini
@@ -7,6 +7,7 @@
 [amd64_node]
 grigri
+k8s-amd64-1

 [amd64]
 prusik
 grigri
+k8s-amd64-1
```

The hardcoded play host lists in `prepare.yml` and `cluster.yml` must **not** be edited:

| play | hosts | purpose |
|---|---|---|
| `prepare.yml:11` Setup amd64 | grigri, prusik | media user, postfix/telegram notifications, smartd |
| `prepare.yml:18` Setup swap | prusik | swap file |
| `prepare.yml:23` Setup NVIDIA drivers | nvidia group | GPU runtime |
| `prepare.yml:30` Setup pikvm | prusik-ipmi | IPMI console |
| `cluster.yml:28` Install zfs-exporter | grigri, prusik | ZFS metrics |

A new node gets no `setup` role, so no media user, no postfix/telegram notifications and no
smartd config. These are exactly the ZFS/GPU/backup paths a new node must stay out of.

### Host variables

Create `metal/inventory/host_vars/<node>.yml`. Contents depend on whether the node has a ZFS pool.

**No-ZFS node** (e.g. k8s-amd64-1):

```yaml
# Ubuntu 26.04: sudo-rs rejects -H; classic sudo is at /usr/bin/sudo.ws
ansible_become_exe: /usr/bin/sudo.ws

# chrony is the default time daemon on 26.04; the `ntp` package does not exist
ntp_daemon_select: chrony
ntp_package: chrony
ntp_config_file: /etc/chrony/chrony.conf

# Required when DHCP does not push the search domain. roles/k3s/templates/config.yaml.j2
# builds `server: https://prusik:6443` from ansible_hostname, so a node that cannot resolve
# short names cannot join. grigri and prusik get `search grigri` from DHCP.
prepare_dns_search_domains:
  - grigri

# All three of kube-reserved / system-reserved / eviction-hard must be set because
# host_vars overrides group_vars/kube_node.yml wholesale.
k3s_kubelet_extra_args:
  - kube-reserved=cpu=0.5,memory=512Mi,ephemeral-storage=1Gi
  - system-reserved=cpu=0.5,memory=1Gi,ephemeral-storage=1Gi
  - eviction-hard=memory.available<500Mi,nodefs.available<10%
```

Omit every `zfs_*`/`l2arc_*`/`swap_file_size_mb` key — `roles/setup/tasks/zfs.yml:2-8` asserts
on them and hard-fails. Also never set `prepare_additional_disks` (the disk-format loop at
`roles/prepare/tasks/main.yml:64-70` no-ops only while that list is empty, and
`prepare_additional_disks_force_format` is data-destroying).

**ZFS node** (e.g. prusik, grigri): same `k3s_kubelet_extra_args` (all three keys, because
`group_vars/kube_node.yml:6` is overridden wholesale), plus `zfs_arc_min_gb`, `zfs_arc_max_gb`,
`l2arc_write_max_mb`, `l2arc_write_boost_mb`, `l2arc_noprefetch`, and on prusik only
`swap_file_size_mb`.

### Ubuntu 26.04 specifics

| key | value | why |
|---|---|---|
| `ansible_become_exe` | `/usr/bin/sudo.ws` | sudo-rs removed `-H`; do NOT `apt remove sudo-rs`, it takes `ubuntu-minimal` with it |
| `ntp_daemon_select` | `chrony` | the `ntp` package no longer exists in resolute; must go through `ntp_daemon_select` / the `metal/roles/ntp` wrapper — setting `ntp_daemon` in host_vars does nothing (`include_vars` outranks inventory host_vars) |
| `ntp_package` | `chrony` | |
| `ntp_config_file` | `/etc/chrony/chrony.conf` | |
| `prepare_dns_search_domains` | `[grigri]` | when DHCP does not push the search domain |

24.04 uses ntpsec, 22.04 uses the `ntp` default. See
[`docs/troubleshooting/ansible-ubuntu-2604-compat.md`](../troubleshooting/ansible-ubuntu-2604-compat.md)
for full details.

### Prerequisites: pfSense BGP neighbour

`system/kube-system/resources/cilium/bgp-cluster-config.yaml` selects `kubernetes.io/os: linux`
with `localASN: 64513` peering to `peerAddress: 192.168.192.1` at `peerASN: 64512`. The cluster
side is automatic for any new node, but **FRR on pfSense must have a neighbour entry for the
node's IP with remote-as 64513**, matching grigri and prusik. Without it, BGP never establishes
and no pod CIDR or LoadBalancer IP is announced from the node.

### Pre-flight: bootstrap the new node

`make first-boot` is **not usable** for a modern Ubuntu node: it forces
`-e ansible_user=root --ask-pass` and Ubuntu images are `PermitRootLogin prohibit-password`. It
is also not scopeable because it appends its own `--limit` after `ANSIBLE_EXTRA_ARGS` (the last
`--limit` wins in `metal/Makefile:10,43-82`).

Before the first `make prepare`, create the user and `~/.ssh/authorized_keys` on the new node.
Passwordless sudo is needed because `roles/prepare/tasks/user.yml:26-30` is what writes the
`%sudo ... NOPASSWD:ALL` rule (chicken-and-egg). Either:

- add `/etc/sudoers.d/90-ansible` (mode 440) by hand once, or
- run the first prepare with `-K` (ask-become-pass), which works because `ansible_become_exe`
  points at classic sudo

### Prepare and join

All commands must be scoped with `--limit`. `ANSIBLE_EXTRA_ARGS` is interpolated verbatim BEFORE
the playbook path (`metal/Makefile:10,43-82`), so the last `--limit` wins.

```bash
# 1. OS preparation (scoped to the new node)
cd metal && ANSIBLE_EXTRA_ARGS="--limit <node>" make prepare

# 2. Join the cluster (HUMAN ONLY — make cluster is on the AGENTS.md never-run list)
cd metal && ANSIBLE_EXTRA_ARGS="--limit <node>" make cluster
```

With `--limit`, the k3s role's `run_once` + `delegate_to: prusik` tasks are read-only (`slurp`)
and the kubeconfig write is `delegate_to: localhost`.

### Node labels

The `node-labels` play needs `metal/kubeconfig.yaml`, which only exists after the k3s role writes
it (`roles/k3s/tasks/main.yml:167`) during `make cluster`. So labels come **after** the join.

```bash
# Label ZFS nodes only (HUMAN ONLY)
cd metal && ANSIBLE_EXTRA_ARGS="--limit grigri,prusik -t node-labels" make cluster
```

### Post-join verification

```bash
# Node is Ready
kubectl --context=grigri get nodes -o wide

# Allocatable memory and kubelet args
kubectl describe node <node>

# DaemonSets scheduled: cilium, cilium-envoy, node-exporter, smartctl-exporter,
# vector-agent, nodelocaldns
kubectl get ds -A

# Cilium BGP peer to 192.168.192.1 established (bgp-cluster-config.yaml peers on
# kubernetes.io/os: linux, so it is automatic — but the router must accept the new peer)
kubectl --context=grigri get pods -n kube-system -l k8s-app=cilium -o wide
kubectl --context=grigri exec -n kube-system <cilium-pod-on-that-node> -c cilium-agent -- cilium bgp peers

# chrony synced to pfsense.grigri (192.168.192.1)
chronyc -n sources
# On 22.04/24.04 (ntpsec): ntpq -pn

# Nothing new stuck Pending
kubectl get pods -A --field-selector=status.phase=Pending
```

#### BGP peer must show `established`

`cilium bgp peers` prints Session `active` when the TCP session is still trying — this is **not**
established. A healthy peer shows `established`, a non-zero uptime, and at least one advertised
route. Meanwhile Cilium itself looks completely healthy (`Cilium: Ok`, `NetworkUnavailable=False`,
`KubeProxyReplacement: True`), so the missing BGP session is easy to miss.

If the session stays `active`, the pfSense FRR neighbour entry for the node's IP is missing or
wrong (see [Prerequisites](#prerequisites-pfsense-bgp-neighbour)). Until fixed, the node's podCIDR
is not advertised — traffic from outside the cluster to pods on this node will not route, and no
LoadBalancer IP is announced from it. Intra-cluster pod-to-pod traffic across nodes is unaffected
(same L2 segment). `cilium bgp routes advertised ipv4 unicast` is empty until the session is
`established`. Note: `cilium bgp peers` prints a deprecation notice suggesting `cilium shell
bgp/peers`; both work.

#### The node is briefly `Ready,SchedulingDisabled` — this is normal

The `k3s-agent` system-upgrade `Plan` (namespace `system-upgrade`) has `cordon: true` and selects
every worker (`node-role.kubernetes.io/control-plane DoesNotExist`). Immediately after joining, the
node shows `Ready,SchedulingDisabled` with taint `node.kubernetes.io/unschedulable:NoSchedule`. An
`apply-k3s-agent-on-<node>-...` job runs; because the node already has the target k3s version, the
job compares sha256 of `/opt/k3s` against `/host/usr/local/bin/k3s`, logs
`[INFO] Binary already been replaced`, exits 0, and the controller uncordons. This clears in under
a minute. The node ends up labelled `plan.upgrade.cattle.io/k3s-agent`, so it is not re-upgraded.

**Do NOT uncordon by hand.** The Ansible-managed `/etc/rancher/k3s/config.yaml` and
`/etc/systemd/system/k3s.service` are untouched during this process.

#### Time sync must point at pfsense.grigri

Verify with `chronyc sources` (26.04/chrony) or `ntpq -pn` (22.04/24.04 ntpsec) — the output
should show `192.168.192.1`, matching prusik and grigri.

If `make cluster` fails partway through the play, handlers run at the end are discarded, so the
chrony/ntp daemon keeps its distro config **in memory** even though the config file on disk is
correct. This is sticky: a re-run sees the file unchanged, notifies no handler, and never reloads.
Recovery: force a handler run or restart the daemon manually. See
[`docs/troubleshooting/ansible-limit-cross-host-facts.md`](../troubleshooting/ansible-limit-cross-host-facts.md)
for the full explanation of the handler-discard mechanism.

!!! warning

    On a no-ZFS node, `zfs-localpv-node` is `2/2 Running` with zero restarts even though the node
    has no pool and the `zpool` binary is not installed — it only fails when actually asked to
    provision or mount a volume. A green DaemonSet is **not** evidence the node can serve PVCs.
    The `allowedTopologies` exclusion in
    `system/zfs-localpv/templates/storage-class-openebs-zfspv.yaml` is the only thing preventing
    durable PVCs from binding to a no-ZFS node.

!!! warning

    A PVC with no `storageClassName` silently inherits the cluster default `openebs-zfspv`, whose
    `allowedTopologies` list only grigri and prusik — on a no-ZFS node such a PVC stays `Pending`
    forever instead of failing loudly. There is currently no local-path StorageClass and k3s'
    bundled local-storage is disabled (`metal/roles/k3s/defaults/main.yml:8-12`).

#### Scoped Ansible runs and cross-host facts

All `make prepare` / `make cluster` runs must be scoped with `--limit`. Scoped runs break on
templates that read another host's *gathered* facts (`hostvars[x].ansible_hostname`), because
`--limit` removes the control plane from every play and `delegate_to` does not gather delegate
facts. This was fixed in commit `1c3f5a48`. See
[`docs/troubleshooting/ansible-limit-cross-host-facts.md`](../troubleshooting/ansible-limit-cross-host-facts.md)
for the full explanation and the second symptom (handler discard on a discarded play).

## Remove a node

!!! danger

    It is recommended to remove nodes one at a time

!!! warning

    Removing a host from `metal/inventory/hosts.ini` only stops Ansible from targeting it — it
    does not stop k3s on the box itself. If the machine is still running with
    `/etc/rancher/node/password` on disk, it will silently rejoin the cluster on the next boot.
    Always drain, uninstall k3s on the node, and delete the Node object **before** touching the
    inventory.

1. Drain the node:

   ```sh
   kubectl drain ${NODE_NAME} --delete-emptydir-data --ignore-daemonsets --force
   ```

2. Uninstall k3s **on the node itself** (still in the inventory at this point):

   ```sh
   cd metal
   ANSIBLE_EXTRA_ARGS="--limit ${NODE_NAME}" make uninstall-k3s
   ```

   `metal/playbooks/uninstall/k3s.yml` stops/disables the `k3s` service, kills leftover
   container/kubelet processes, unmounts `/run/k3s` and `/var/lib/rancher/k3s`, and removes
   `/usr/local/bin/k3s`, `/etc/rancher/k3s`, `/etc/rancher/node` (the cluster join token) and
   the systemd unit. This repo installs k3s from a downloaded binary, so upstream's
   `k3s-agent-uninstall.sh`/`k3s-killall.sh` do not exist on the node — this playbook is the
   equivalent.

3. Remove the node from the cluster (this also garbage-collects its
   `<node>.node-password.k3s` secret in `kube-system`):

   ```sh
   kubectl delete node ${NODE_NAME}
   ```

4. Now remove it from the inventory, and delete its `host_vars/` file if it has one:

   ```diff
   diff --git a/metal/inventory/hosts.ini b/metal/inventory/hosts.ini
   --- a/metal/inventory/hosts.ini
   +++ b/metal/inventory/hosts.ini
   @@ -7,7 +7,6 @@
    [amd64_node]
    grigri
   -k8s-amd64-1
   ```

   Commit and let ArgoCD/Ansible converge.

5. Shutdown the node:

   ```
   ssh ${NODE_NAME} poweroff
   ```
