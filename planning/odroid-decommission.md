# Odroid HC4 Decommissioning Plan

Retire `k8s-odroid-hc4-3` from the grigri cluster. The node existed only as a third failure
domain for Redis Sentinel; Cilium 1.20 cannot run on its kernel, and we keep Cilium at 1.20
rather than maintaining an ARM outlier. Redis moved to master + replica without Sentinel
(2-node degraded mode with manual failover), managed in the external app repos.

**Status: completed.** The node is drained, removed from the cluster, and physically
disconnected. See the "Deviations from the original plan" section at the bottom for what
differed from the steps below and one outstanding follow-up.

## Prerequisites (external repos)

Redis migration is owned by the app gitops repos, not this one:

- `kiais/gitops` — isidoro
- `multivara/gitops` — calypso, oauth2-proxy

For each of the three Redis groups (`isidoro-redis`, `calypso-redis`, `oauth2-proxy-redis`):

1. Shrink `RedisReplication` CR from 3 to 2 replicas (keep per-hostname anti-affinity)
2. Delete the `RedisSentinel` CR
3. Switch clients from sentinel mode to direct connection to the operator's master service
4. Drop oauth2-proxy's `min-replicas-to-write 1` (without Sentinel it turns replica loss
   into a write outage)

**Order is critical:** Redis pods and Sentinels use required hostname anti-affinity — three
replicas cannot schedule on two nodes. If this repo's inventory change lands first, drain
will leave pods Pending until the resize is done.

Exit criteria before touching this repo:

- All three groups running master + replica on `grigri` and `prusik`, Ready
- Zero `redis-sentinel` pods cluster-wide
- Apps connected and healthy for at least 24h

## Phase 1 — Repository changes (GitOps commit)

Single commit, scope `metal` + `system` + `docs`:

1. `metal/inventory/hosts.ini`
   - Remove `odroid_hc4` from `[kube_node:children]` and `[arm:children]`
   - Delete the `[odroid_hc4]` section
   - Delete commented `odroid_c4` / `rock64` lines
2. Delete stale host vars:
   - `metal/inventory/host_vars/k8s-odroid-hc4-1.yml`
   - `metal/inventory/host_vars/k8s-odroid-hc4-2.yml`
   - `metal/inventory/host_vars/k8s-odroid-hc4-3.yml`
3. `system/ingress-nginx/values.yaml` — remove the `k8s-odroid-hc4-3` preferred-affinity entry
4. `system/ingress-nginx-external/values.yaml` — same
5. `README.md` — remove the `k8s-odroid-hc4-3` hardware table row
6. `docs/deployment/cilium-bgp-control-plane.md` — remove the pfSense peer entry for
   `192.168.192.23` (documents the manual pfSense change from Phase 2)

Notes:

- No Cilium BGP resource changes: `system/kube-system/resources/cilium/bgp-peer-config.yaml`
  is generic; the peer disappears with the node
- ARM-specific code in `metal/` roles and `metal/inventory/group_vars/arm.yml` is kept
  (harmless, reusable if ARM hardware returns)
- Armbian/Cilium troubleshooting docs stay as historical incident records

## Phase 2 — Manual operations (user-executed)

1. pfSense: delete BGP neighbor `192.168.192.23`
2. Drain the node:
   ```bash
   kubectl --context=grigri drain k8s-odroid-hc4-3 --ignore-daemonsets --delete-emptydir-data --force
   ```
3. Verify rescheduling. Single-replica workloads move to grigri/prusik; expect brief gaps:
   - `oauth2-proxy` (calypso) — OIDC-gated services briefly unavailable
   - `metrics-server`, `redis-operator`, `vault-operator`, `vault-configurer`
   - `kaniop-webhook` — Kanidm CRD mutations blocked until it is Ready elsewhere (~1-2 min)
   - CoreDNS drops to 1 replica temporarily
4. Uninstall k3s **on the node itself**, while it is still reachable and still in
   `hosts.ini` (this repo installs k3s from a downloaded binary, so upstream's
   `k3s-agent-uninstall.sh` does not exist — use the repo's equivalent):
   ```bash
   cd metal
   ANSIBLE_EXTRA_ARGS="--limit k8s-odroid-hc4-3" make uninstall-k3s
   ```
   This stops/disables `k3s.service`, kills leftover processes, unmounts k3s volumes, and
   removes `/usr/local/bin/k3s`, `/etc/rancher/k3s`, `/etc/rancher/node` (the cluster join
   token) and the systemd unit. Skipping this step means the box silently rejoins the
   cluster if it is ever powered back on.
5. Remove the node object (this also garbage-collects its `node-password.k3s` secret):
   ```bash
   kubectl --context=grigri delete node k8s-odroid-hc4-3
   ```
6. Physically unplug / power off

## Phase 3 — Post-decommission verification

- No Pending pods: `kubectl --context=grigri get pods -A | grep -v Running`
- No events referencing the node:
  `kubectl --context=grigri get events -A --sort-by=.lastTimestamp`
- Redis master + replica healthy for both apps; `redis-operator` reconciling
- ArgoCD: all applications Synced/Healthy
- 24-48h soak before considering it closed

## Phase 4 — Documentation follow-up

- Add a Redis manual-failover runbook (promote with `REPLICAOF NO ONE`, reattach old
  master as replica) — lives with the Redis deployment docs in the app repos
- `docs/user-guide/add-or-remove-nodes.md` — fixed: corrected the stale inventory path
  (`metal/inventories/master/inventory.ini` → `metal/inventory/hosts.ini`) and added the
  missing k3s uninstall step to the removal procedure

## Rollback

- Before Phase 2 step 6: `kubectl uncordon k8s-odroid-hc4-3`; revert the inventory commit
- After power-off: re-add via `docs/user-guide/add-or-remove-nodes.md` (requires reimage;
  Petitboot SPI bypass per `docs/deployment/manual-setup.md`)
- Restore full HA later by adding any third failure domain and re-introducing Sentinel in
  the app repos

## Known accepted risks

- Redis failover is manual until a third failure domain exists; acceptable for pre-prod
- `kaniop-webhook` single replica can block Kanidm CRD operations during any node loss
  (pre-existing, not introduced by this change)

## Deviations from the original plan

Executed out of order and with one gap:

- The node was already cordoned before Phase 1/2 started, so drain was a no-op for
  scheduling (nothing new could land there).
- `kubectl delete node` and physical power-off happened **before** the k3s uninstall step
  (Phase 2 step 4). The machine is now unreachable, so the uninstall could not be run.
- **Outstanding follow-up:** `/etc/rancher/k3s`, `/etc/rancher/node/password` (cluster join
  token) and `k3s.service` (enabled) are still on the Odroid's disk. If the box is ever
  powered back on and reconnected to the network without a reimage or manual cleanup, it
  will silently rejoin the cluster. Before reusing/repurposing the hardware, either reimage
  it or run the equivalent cleanup over SSH first (see Phase 2 step 4).
- `metal/playbooks/uninstall/k3s.yml` was hardened as part of this work: it now also removes
  `/etc/rancher/node` (previously leaked the join token) and refuses to run without an
  explicit `--limit` (previously targeted `hosts: all`, risking a cluster-wide wipe if run
  by mistake).
