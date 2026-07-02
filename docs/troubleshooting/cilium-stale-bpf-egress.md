# Cilium Stale BPF Datapath Breaks Pod Egress

## Problem

Pods on a node lose external connectivity and DNS resolution. Symptoms include:

- `lookup kubernetes.default.svc.cluster.local on 169.254.25.10:53: i/o timeout`
- ExternalSecret validation failures (`ClusterSecretStore "vault" is not ready`)
- `ExternalDNS` unable to reach upstream APIs
- ArgoCD sync errors due to DNS resolution failures

Host network works fine (SSH, ping to gateway, external IPs). Pod-to-pod cross-node traffic works.
Only pod egress to host network and beyond is broken.

## Root Cause

Cilium's BPF programs attached to the host interface (`enp0s20f0`) and `cilium_host` can become
stale after extended pod uptime. When this happens, the BPF datapath silently drops or misroutes
traffic from pod network namespaces to the host network stack.

The NodeLocal DNS address `169.254.25.10` is not a Kubernetes Service — Cilium has no BPF service
entry for it. With `kubeProxyReplacement: True`, iptables DNAT rules installed by NodeLocal DNS are
bypassed entirely. Pods send DNS to `169.254.25.10`, Cilium BPF treats it as non-service traffic,
and the stale BPF state prevents it from reaching the host network where NodeLocal DNS listens.

Evidence pattern:

- `cilium-dbg bpf ct list global` shows thousands of DNS entries to `169.254.25.10:53` with
  `Packets=0 Bytes=0 RxFlagsSeen=0x00` (all unanswered)
- Zero connections to external IPs (185.12.x, 1.1.1.1, 8.8.8.8) in the connection tracking table
- `cilium-dbg service list` shows only 1 backend for `kube-dns` (should have 2)
- Host network works perfectly (`ssh <node> ping 1.1.1.1` succeeds)
- Cross-node pod-to-pod traffic works (Cilium native routing unaffected)

## How to Diagnose

```bash
# 1. Check host network — if this works, the problem is pod-to-host only
ssh <node> "ping -c 2 -W 3 1.1.1.1"

# 2. Test pod egress — this will fail
kubectl --context=grigri exec -n <ns> <pod> -- \
  wget -q -O /dev/null --timeout=5 https://google.com

# 3. Check DNS CT entries — thousands with Packets=0 means unanswered
kubectl --context=grigri exec -n kube-system cilium-<pod> -- \
  cilium-dbg bpf ct list global | grep "169.254.25.10:53" | wc -l
kubectl --context=grigri exec -n kube-system cilium-<pod> -- \
  cilium-dbg bpf ct list global | grep "169.254.25.10:53" | grep -v "Packets=0" | wc -l

# 4. Check for external connections — zero entries confirms broken egress
kubectl --context=grigri exec -n kube-system cilium-<pod> -- \
  cilium-dbg bpf ct list global | grep -E "(1\.1\.1\.1|8\.8\.8\.8|185\.12\.)" | wc -l

# 5. Check kube-dns backends — should have 2, stale state may show 1
kubectl --context=grigri exec -n kube-system cilium-<pod> -- \
  cilium-dbg service list | grep 10.43.0.10
```

## Fix / Workaround

Restart the Cilium pod on the affected node to force BPF program recompilation and reattachment:

```bash
kubectl --context=grigri delete pod cilium-<pod> -n kube-system
```

Verify after the new pod starts:

```bash
# External connectivity restored
kubectl --context=grigri exec -n monitoring <grafana-pod> -- \
  wget -q -O /dev/null --timeout=5 https://google.com

# DNS working
kubectl --context=grigri exec -n <ns> <pod> -- nslookup google.com
```

## Observed Incident: Vector Buffer Fill on Odroid HC4 (2026-06-03)

**Node:** `k8s-odroid-hc4-3` (ARM64, kernel 6.6.63)

**Symptom:** `VectorHighBufferFill` alert fired — `loki_journal` buffer on `vector-agent-p7vxd`
at 97% and climbing. The buffer had been growing steadily for ~6 hours.

**Key finding:** Stale BPF can affect specific connections selectively. On the same pod:
- `loki_journal` sink (endpoint `http://loki:3100`): DNS resolution completely broken,
  buffer filling, retries exhausting, events dropped
- `loki_pods` sink (same `http://loki:3100` endpoint): working normally, 0% buffer fill

Vector logs showed repeated DNS failures on a single stuck request (`request_id=1718`):
```
HTTP error. error=error trying to connect: dns error: failed to lookup address information: Temporary failure in name resolution
```

**Diagnosis commands used:**
```bash
# Check buffer fill percentage per agent
# PromQL: (vector_buffer_events{component_id=~"loki_pods|loki_journal"} / vector_buffer_max_size_events{component_id=~"loki_pods|loki_journal"}) * 100

# Check vector logs for DNS errors
kubectl --context=grigri logs vector-agent-p7vxd -n loki --tail=100 | grep -iE "error|dns|buffer"

# Check sent bytes — loki_journal was 0 on affected pod, loki_pods was normal
# PromQL: rate(vector_component_sent_bytes_total{component_id=~"loki_pods|loki_journal"}[5m])
```

**Fix:** Restarting the Cilium pod on `k8s-odroid-hc4-3` restored DNS. Buffer dropped to 0%
immediately (Vector had already exhausted retries and dropped the queued events).

**Impact:** Journal logs from this node were lost for the period between DNS failure and retry
exhaustion (~2 hours of buffered events dropped).

## Observed Incident: Router Outage — prusik BPF Stale (2026-06-06)

**Node:** prusik (x86_64, Cilium v1.19.4)

**Trigger:** Router outage (~7h earlier). Network recovery caused widespread pod restarts across
the cluster. After recovery, Cilium BPF on prusik became stale.

**Symptoms:**

- `vault-0` (vault ns, on prusik): readiness probe timeouts (90+ failures in 23h). Vault
  responded when queried from inside the pod, but kubelet probes timed out crossing the BPF
  datapath. Service endpoint kept flipping in/out of ready.
- `nodelocaldns` corefile-watcher sidecar: restarting every ~20min on all 3 nodes (76-82
  restarts each). CoreDNS pod IPs kept changing after restarts, triggering sidecar restarts.
- `hermes-0`: restarted during the incident.
- `bazarr`: OOMKilled (1Gi limit) + subcleaner sidecar 53 restarts.
- `jellyfin`: 17 restarts, 137 liveness probe timeouts over 23h.

**Fix:** Deleting the Cilium pod on prusik (`cilium-swmbt`) resolved the vault-0 readiness
failures. Only one transitional failure occurred after the restart, then stable for 3+ minutes
(vs. multiple failures per minute before).

```bash
kubectl --context=grigri delete pod cilium-swmbt -n kube-system
```

**Diagnosis commands used:**

```bash
# High restart counts (indicating post-outage instability)
kubectl --context=grigri get pods -A -o json | \
  jq -r '.items[] | select(.status.containerStatuses != null) |
  .metadata.namespace + "/" + .metadata.name + ": restarts=" +
  (.status.containerStatuses | map(.restartCount) | add | tostring)' | sort -t= -k2 -rn

# Vault readiness events (ongoing probe failures)
kubectl --context=grigri get events -n vault --field-selector involvedObject.name=vault-0 \
  --sort-by='.lastTimestamp'
```

## Observed Incident: ArgoCD NetworkPolicy + Post-Reboot Stale Datapath — prusik (2026-07-02)

**Node:** prusik (amd64, kernel 6.8.0-134, Cilium v1.19.5)

**Trigger:** prusik rebooted ~14h earlier (kernel patched 6.8.0-124 → 6.8.0-134). The Cilium pod
(`cilium-n68ct`) was recreated ~4h after the reboot to clear an earlier stale-datapath episode
(~20 pods showed the "14h ago" node-reboot restart stamp; the Cilium pod was only 10h old).

**Symptom:** Enabling ArgoCD's per-component NetworkPolicy (`global.networkPolicy.create: true`)
caused kubelet startup/liveness probes to `argocd-repo-server` (and other prusik pods) to time
out (`context deadline exceeded`). Meanwhile Prometheus (in-cluster path) could still scrape the
same pod's metrics port (8084) at HTTP 200 in <30ms.

This is the classic stale-datapath signature:

- **Host → pod traffic (kubelet probes)** — dropped (governed by NetworkPolicy ingress via BPF,
  which the stale datapath fails to evaluate correctly)
- **Pod → pod / in-cluster traffic** — works (not gated by the stale policy path)

**Diagnosis — confirming it's Cilium, not the app or kernel:**

- Probes fail only when NetworkPolicy isolation is active; removing the NP → probes pass
- Non-ArgoCD pods on prusik (`kanidm`, CI runners) show the same probe-failure pattern
- Prometheus scraping (in-cluster path) succeeds while kubelet probing (host path) fails
- **Deleting the Cilium pod fixes it** — this rules out a kernel BPF regression (a kernel bug
  would persist across userspace restart)

**Fix:**

```bash
kubectl --context=grigri delete pod cilium-n68ct -n kube-system
```

Keep `global.networkPolicy.create: false` in ArgoCD values until Cilium ships a version with the
fixes for #46065 / #45233 (see upstream table below). Restarting the Cilium pod after every node
reboot remains the only reliable workaround.

## Upstream Issue Tracking

| Issue | Description | Status | Affects v1.19.5? |
|-------|-------------|--------|-------------------|
| [#46065](https://github.com/cilium/cilium/issues/46065) | `CgroupInetSockRelease` BPF link orphaned on agent restart — deleting the pod restores it (v1.19.x regression) | Open, no fix | **Yes (v1.19.x-specific)** |
| [#45233](https://github.com/cilium/cilium/issues/45233) | NetworkPolicy silently missing from endpoint BPF policy on fresh node bootstrap | Open, no fix | **Yes** |
| [PR #46741](https://github.com/cilium/cilium/pull/46741) | Gate agent readiness on eBPF datapath (proper fix — prevents false-Ready on stale datapath) | Open, unmerged | Would prevent the symptom |
| [#45077](https://github.com/cilium/cilium/issues/45077) | Host loses egress on Cilium agent restart (orphaned cgroup BPF programs) | Open, no assignee | Unconfirmed (v1.18 confirmed) |
| [#43944](https://github.com/cilium/cilium/issues/43944) | LRP shows invalid IP for nodelocaldns | Open, assigned to ysksuzuki/ajmmm | Yes |
| [#44138](https://github.com/cilium/cilium/issues/44138) | Rethink LRP API (skipRedirectFromBackend, addressMatcher scope) | Open, design phase | Yes |
| [PR #45522](https://github.com/cilium/cilium/pull/45522) | addressMatcher refuse-override guard | Merged, backported to v1.19 | Fix included in v1.19.4 |

No ETA on any open issue. v1.19.5 (latest stable as of 2026-07) includes none of the above fixes.
The only mitigation for BPF staleness remains deleting the Cilium pod on the affected node. For
nodelocaldns, the `serviceMatcher` + corefile-watcher sidecar remains the recommended workaround
until #43944 is resolved. NetworkPolicy isolation should be kept disabled until #46065 / #45233
ship in a release — it turns the latent stale-datapath bug into an active probe-failure outage on
any node that recently rebooted.

## Notes

- This is distinct from the Armbian kernel BPF masquerade regression
  (`docs/troubleshooting/armbian-kernel-bpf-masquerade.md`) which affects only UDP SNAT on ARM64.
  This issue affects all traffic from pod network to host network.
- The NodeLocal DNS + Cilium LRP combination has known bugs in v1.18+/v1.19. See
  `docs/troubleshooting/nodelocaldns-cilium-lrp.md` for the full migration from addressMatcher
  to serviceMatcher with dynamic upstream discovery.
- Root cause of the BPF state staleness is now linked to two upstream issues: BPF link orphaning
  on agent restart ([#46065](https://github.com/cilium/cilium/issues/46065), a v1.19.x regression)
  and NetworkPolicy missing from endpoint BPF on fresh node bootstrap
  ([#45233](https://github.com/cilium/cilium/issues/45233)). Both are triggered by node reboots
  or Cilium pod restarts. The proper fix (readiness-gating on datapath state, PR #46741) is not
  yet merged.
- Diagnostic shortcut: **if deleting the Cilium pod fixes it, it's not a kernel bug.** Kernel
  BPF/JIT regressions persist across userspace restarts; this bug clears on agent restart because
  the stale BPF programs are recompiled and reattached from a clean state.
- NetworkPolicy enforcement turns the latent staleness into an active outage: it makes kubelet
  health probes (host → pod) dependent on correct BPF policy evaluation, which the stale datapath
  cannot provide. Keep `networkPolicy.create: false` until #46065 / #45233 are fixed.
