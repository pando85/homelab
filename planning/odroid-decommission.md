# Odroid HC4 Decommissioning Plan

Retire `k8s-odroid-hc4-3` from the grigri cluster. The node exists only as a third failure
domain for Redis Sentinel; Cilium 1.20 cannot run on its Armbian kernel, and we keep Cilium
at 1.20 rather than maintaining an ARM outlier. Redis moves to master + replica without
Sentinel (2-node degraded mode with manual failover), managed in the external app repos.

## Prerequisites (external repos, MUST complete first)

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
   - Delete the `[odroid_hc4]` section (line 25-26)
   - Delete commented `odroid_c4` / `rock64` lines
2. Delete stale host vars:
   - `metal/inventory/host_vars/k8s-odroid-hc4-1.yml`
   - `metal/inventory/host_vars/k8s-odroid-hc4-2.yml`
   - `metal/inventory/host_vars/k8s-odroid-hc4-3.yml`
3. `system/ingress-nginx/values.yaml` — remove the `k8s-odroid-hc4-3` preferred-affinity
   entry (line 79)
4. `system/ingress-nginx-external/values.yaml` — same (line 86)
5. `README.md` — remove the `k8s-odroid-hc4-3` hardware table row
6. `docs/deployment/cilium-bgp-control-plane.md` — remove the pfSense peer entry for
   `192.168.192.23` (documents the manual pfSense change from Phase 2)

Notes:

- No Cilium BGP resource changes: `system/kube-system/resources/cilium/bgp-peer-config.yaml`
  is generic; the peer disappears with the node
- ARM-specific code in `metal/` roles is kept (harmless, reusable if ARM hardware returns)
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
4. Remove the node object:
   ```bash
   kubectl --context=grigri delete node k8s-odroid-hc4-3
   ```
5. SSH in and power off:
   ```bash
   ssh k8s-odroid-hc4-3 poweroff
   ```
6. Physically unplug when convenient

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
- Update `docs/user-guide/add-or-remove-nodes.md` if the inventory path references are
  still wrong (it mentions `metal/inventories/master/inventory.ini`, actual path is
  `metal/inventory/hosts.ini`)

## Rollback

- Before Phase 2 step 5: `kubectl uncordon k8s-odroid-hc4-3`; revert the inventory commit
- After power-off: re-add via `docs/user-guide/add-or-remove-nodes.md` (requires reimage;
  Petitboot SPI bypass per `docs/deployment/manual-setup.md`)
- Restore full HA later by adding any third failure domain and re-introducing Sentinel in
  the app repos

## Known accepted risks

- Redis failover is manual until a third failure domain exists; acceptable for pre-prod
- `kaniop-webhook` single replica can block Kanidm CRD operations during any node loss
  (pre-existing, not introduced by this change)
