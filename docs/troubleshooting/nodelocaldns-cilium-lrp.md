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

## Architecture

The current solution uses `serviceMatcher` LRP with a sidecar for dynamic upstream discovery:

```
Pod → kube-dns (10.43.0.10) → Cilium LRP serviceMatcher → nodelocaldns
                                                            ↓ cache miss
                                                    forward to CoreDNS pod IPs
                                                    (discovered dynamically by
                                                    corefile-watcher sidecar via
                                                    kube-dns-upstream headless service)
```

Key files:
- `system/kube-system/resources/nodelocaldns/` — all nodelocaldns resources
- `system/kube-system/cilium-values.yaml` — `localRedirectPolicy: true`
- `metal/roles/k3s/templates/config.yaml.j2` — `cluster-dns=10.43.0.10`

## How to Diagnose

```bash
# 1. Check nodelocaldns and cilium pods on affected node
kubectl --context=grigri get pods -n kube-system -l k8s-app=node-local-dns -o wide

# 2. Check if LRP is redirecting correctly
kubectl --context=grigri exec -n kube-system cilium-<node-pod> -- \
  cilium-dbg service list | grep -E 'kube-dns|LocalRedirect'

# Should show LocalRedirect for kube-dns (10.43.0.10:53) pointing to nodelocaldns pod IP

# 3. Check sidecar discovered CoreDNS IPs
kubectl --context=grigri logs -n kube-system <nodelocaldns-pod> -c corefile-watcher --tail=5

# 4. Check generated Corefile has pod IPs (not 10.43.0.10)
kubectl --context=grigri exec -n kube-system <nodelocaldns-pod> -c node-cache -- \
  cat /etc/coredns/Corefile

# 5. Test DNS from a pod
kubectl --context=grigri exec -n <ns> <pod> -- nslookup kubernetes.default.svc.cluster.local
```

## Fix / Workaround

If DNS breaks after a node reboot:

```bash
# Restart cilium pod on the affected node
kubectl --context=grigri delete pod cilium-<node-pod> -n kube-system
```

If sidecar hasn't discovered endpoints:

```bash
# Check kube-dns-upstream endpoints exist
kubectl --context=grigri get endpoints kube-dns-upstream -n kube-system

# Restart nodelocaldns to force sidecar rediscovery
kubectl --context=grigri rollout restart daemonset/nodelocaldns -n kube-system
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
