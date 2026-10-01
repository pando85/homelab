# Cilium LRP Breaks NodeLocal DNS After Node Reboot

## Problem

After a node reboot, DNS resolution breaks for all pods on that node. Symptoms:

- `nslookup kubernetes.default.svc.cluster.local` times out
- External DNS (google.com) may work while internal cluster DNS fails
- ArgoCD shows `Unknown` sync status across many applications
- `nodelocaldns` pod logs show upstream DNS timeouts
- CoreDNS pods on other nodes are healthy and running

## Root Cause

Multiple interacting bugs in Cilium v1.18+/v1.19's CiliumLocalRedirectPolicy (LRP):

1. **addressMatcher frontend guard bypass** (PR #45522): When LRP backing pods (nodelocaldns)
   are not yet Ready after a node reboot, the `len(pods)==0` code path runs an unconditional
   `DeleteFrontend` that wipes the LRP frontend before the override guard can inspect it. By the
   time pods come up, the BPF state is inconsistent — `cilium service list` shows "active" but
   the datapath doesn't redirect traffic.

2. **skipRedirectFromBackend broken** (v1.19.4): When using `serviceMatcher` for kube-dns,
   nodelocaldns forwards cluster.local queries to the kube-dns ClusterIP (10.43.0.10) via TCP.
   `skipRedirectFromBackend: true` should prevent the LRP from redirecting nodelocaldns's own
   traffic back to itself, but it doesn't work — creating a redirect loop that times out.

3. **TCX attachment mode** (v1.19 default on kernel 6.6+): Cilium 1.19 switched from tc to tcx
   BPF attachment. PR #45740 fixed silent packet drops in tcx hooks. Compounds LRP issues.

The direct-CoreDNS workaround is intentionally retained until the native upstream-service path is
revalidated against the current Cilium version. Do not replace it just because the upstream Cilium
example uses `kube-dns-upstream` as a normal ClusterIP Service; that path previously looped in this
cluster.

## Architecture

The current solution uses `serviceMatcher` LRP and forwards cache misses directly to the Ready
CoreDNS pod IPs, bypassing the LRP-matched `kube-dns` ClusterIP:

```
Pod → kube-dns (10.43.0.10) → Cilium LRP serviceMatcher → nodelocaldns
                                                            ↓ cache miss
                                                    CoreDNS pod IPs
                                                    (from EndpointSlices)
```

Corefile generation has two phases:

```
corefile-init
  └─ list Ready kube-dns-upstream EndpointSlice addresses via the in-cluster API
     └─ atomically write /etc/coredns/Corefile.base
        └─ node-cache starts and generates /etc/coredns/Corefile

corefile-watcher
  └─ poll EndpointSlices every 5s via the in-cluster API
     └─ atomically replace Corefile.base only with a valid non-empty endpoint set
        └─ node-cache config sync regenerates Corefile and CoreDNS reloads it
```

`Corefile.base` is deliberate. `node-cache` treats `<-conf>.base` as its source template and owns
the generated `Corefile`; the watcher must not write `Corefile` directly.

The init container and watcher use a pinned official Python Alpine image and only Python's standard
library. They authenticate directly to the Kubernetes API with the mounted ServiceAccount token and
CA certificate. There is no runtime package installation, external binary download, `kubectl`, or
`jq` dependency. EndpointSlice RBAC is reduced to `list` only.

If the API cannot be queried or there are no usable CoreDNS endpoints, the init container waits and
`node-cache` does not start with an empty/stale configuration. After startup, polling failures leave
the last valid `Corefile.base` untouched. Endpoint addresses that are explicitly unready or
terminating are ignored.

Key files:
- `system/kube-system/resources/nodelocaldns/` — all nodelocaldns resources
- `system/kube-system/cilium-values.yaml` — `localRedirectPolicy: true`
- `metal/roles/k3s/templates/config.yaml.j2` — `cluster-dns=10.43.0.10`

## How to Diagnose

```bash
# 1. Check nodelocaldns and cilium pods on affected node
kubectl --context=grigri get pods -n kube-system -l k8s-app=node-local-dns -o wide

# If the pod is stuck in init, inspect initial endpoint discovery
kubectl --context=grigri logs -n kube-system <nodelocaldns-pod> -c corefile-init --tail=20

# 2. Check if LRP is redirecting correctly
kubectl --context=grigri exec -n kube-system cilium-<node-pod> -- \
  cilium-dbg service list | grep -E 'kube-dns|LocalRedirect'

# Should show LocalRedirect for kube-dns (10.43.0.10:53) pointing to nodelocaldns pod IP

# 3. Check watcher endpoint discovery
kubectl --context=grigri logs -n kube-system <nodelocaldns-pod> -c corefile-watcher --tail=20

# 4. Check the base and generated Corefiles contain CoreDNS pod IPs, not 10.43.0.10
kubectl --context=grigri exec -n kube-system <nodelocaldns-pod> -c node-cache -- \
  cat /etc/coredns/Corefile.base
kubectl --context=grigri exec -n kube-system <nodelocaldns-pod> -c node-cache -- \
  cat /etc/coredns/Corefile

# 5. Compare with current Ready CoreDNS EndpointSlices
kubectl --context=grigri get endpointslices -n kube-system \
  -l kubernetes.io/service-name=kube-dns-upstream -o wide

# 6. Test DNS from a pod
kubectl --context=grigri exec -n <ns> <pod> -- nslookup kubernetes.default.svc.cluster.local
```

## Fix / Workaround

If DNS breaks after a node reboot and the generated upstream addresses are correct:

```bash
# Restart cilium pod on the affected node
kubectl --context=grigri delete pod cilium-<node-pod> -n kube-system
```

If endpoint discovery is failing, inspect the init/watcher logs and the headless Service endpoints.
A recreated nodelocaldns pod will not start `node-cache` until it can generate a valid initial
`Corefile.base`.

```bash
kubectl --context=grigri get endpointslices -n kube-system \
  -l kubernetes.io/service-name=kube-dns-upstream -o wide
```

## Observed Incident: grigri Node Reboot (2026-06-05)

**Node:** grigri (x86_64, kernel 6.8.0-124-generic, Cilium v1.19.4)

**Trigger:** Node reboot caused all pods to restart. Cilium and nodelocaldns both have
`system-node-critical` priority. After restart, the old addressMatcher LRP (169.254.25.10)
had stale BPF state — `cilium service list` showed "active" but DNS queries to 169.254.25.10
went unanswered.

**Resolution:** Migrated from `addressMatcher` (169.254.25.10) to `serviceMatcher` (kube-dns)
with a corefile-watcher sidecar that dynamically discovers CoreDNS pod IPs from the
`kube-dns-upstream` headless service endpoints, avoiding the redirect loop caused by broken
`skipRedirectFromBackend`.

## Migration from addressMatcher to serviceMatcher

The old setup used `addressMatcher` with 169.254.25.10 (link-local IP):
- Pods queried 169.254.25.10 → LRP redirected to nodelocaldns
- Fragile: 169.254.25.10 is not a Kubernetes service, not managed by Cilium
- After node reboot, LRP BPF state became stale
- Complete DNS outage with no fallback (169.254.25.10 goes nowhere without LRP)

The new setup uses `serviceMatcher` with kube-dns service:
- Pods query kube-dns (10.43.0.10) → LRP redirects to nodelocaldns
- Better failure mode: if LRP breaks, pods fall through to CoreDNS directly
- Sidecar dynamically discovers CoreDNS pod IPs to avoid redirect loop
- kubelet cluster-dns changed from 169.254.25.10 to 10.43.0.10 (requires Ansible)

## Corefile-Watcher Sidecar Restarts (2026-08-18)

**Symptom:** nodelocaldns pods show 2400+ restarts, but DNS is healthy (SERVFAIL rate = 0).

**Root Cause:** The old `corefile-watcher` sidecar ran `kubectl --watch` to monitor endpoint
changes. The Kubernetes API server closes watch connections periodically. Before the reconnect loop
was added, each normal watch closure caused the script to exit and kubelet to restart the container.

**Current design:** issue #4553 removes the Kubernetes watch entirely. The watcher performs a small
EndpointSlice list request every 5 seconds through the in-cluster API. This is simpler, avoids watch
lifecycle/reconnect behavior, and is sufficient because CoreDNS endpoint changes are infrequent.

## DNS Bootstrap Failure After Reboot (2026-10-01)

**Symptom:** After `prusik` rebooted, cluster DNS failed for roughly 15 minutes. Blackbox probes
reported lookup timeouts for internal and external targets; Vault and Redis workloads were also
affected.

**Contributing failures:**

1. The watcher ran from `alpine` and installed `curl`, then downloaded the latest `kubectl` from
   `dl.k8s.io` every time the container started. A failure anywhere in that external bootstrap path
   left the watcher without kubectl.
2. `corefile-watcher` and `node-cache` were ordinary sibling containers, so Kubernetes could start
   `node-cache` before the watcher had generated a Corefile.
3. The watcher wrote `/etc/coredns/Corefile` directly even though `node-cache` expects
   `/etc/coredns/Corefile.base` as its source configuration. This produced the misleading
   `Failed to read ... Corefile.base` error and bypassed node-cache's normal config-sync ownership.
4. The writable configuration lived in an `emptyDir`, so a newly created pod had no safe initial
   configuration until dynamic discovery succeeded.

**Fix (issue #4553):**

- Replace the runtime package/binary bootstrap with a small stdlib-only Python helper using the
  in-cluster Kubernetes API directly.
- Add `corefile-init`, which waits for at least one usable CoreDNS EndpointSlice address and writes
  a validated `Corefile.base` before any regular container starts.
- Make the watcher update `Corefile.base`, leaving `node-cache` responsible for the effective
  `Corefile` and reload lifecycle.
- Filter out explicitly unready and terminating EndpointSlice entries, de-duplicate and validate IP
  addresses, reject empty or incompletely rendered configurations, and write via a hidden temporary
  file plus atomic rename.
- On watcher/API failure after startup, preserve the last valid base configuration rather than
  replacing it with an empty or stale partial render.
- Reduce the ServiceAccount EndpointSlice permission to `list` only.

This hardens the existing direct-pod-IP workaround; it does **not** claim that the historical Cilium
LRP redirect-loop bug still exists in the current release. Re-evaluate the native
`kube-dns-upstream` ClusterIP path separately with reboot/restart testing before removing the
workaround.
