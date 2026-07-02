# Bandwidth protection (pfSense CPU saturation)

The pfSense router (NetGate GX-412TC, 4×1 GHz, software PPPoE on `pppoe0`) saturates
its CPU in interrupt context around **~300 Mbit/s** of sustained WAN throughput
(30%+ interrupt). This has caused router instability and reboots — see
`docs/troubleshooting/bandwidth-limiting.md` for root-cause analysis. This document is
the complete fix plan across three coordinated layers.

## Core principle: direction determines the enforcement point

| Direction | WAN load source | Shapable in-cluster? | Authoritative tool |
|---|---|---|---|
| **Outbound** (cluster→internet) | a pod | yes — at the source | Cilium `kubernetes.io/egress-bandwidth` (EDT, paces) |
| **Inbound** (internet→cluster) | external | **no** — pfSense is upstream of the cluster | pfSense traffic shaper (dummynet) |

pfSense forwards internet→cluster packets *before* they reach any node or pod, so no
in-cluster mechanism can authoritatively reduce inbound load on pfSense. A pod-level
policer only creates delayed, lossy TCP backpressure. **Inbound must be capped at (or
upstream of) pfSense.**

Cilium's Bandwidth Manager only documents `egress-bandwidth`; there is **no native
ingress limiting**, and `kubernetes.io/ingress-bandwidth` is a no-op for same-node
traffic (BPF `!from_host` check in `l3.h`).

## Layer 1 — Outbound: Cilium `egress-bandwidth` (native)

Annotate every WAN-uploading pod (the rclone-sync CronJobs already do):

```yaml
metadata:
  annotations:
    kubernetes.io/egress-bandwidth: "100M"
```

Cilium EDT paces (not drops) at the source — clean and proven. **Action:** audit all
upload workloads and add the annotation. This fully replaces the original
tc-limiter's outbound role.

## Layer 2 — Inbound: pfSense dummynet limiter (the guarantee)

The only mechanism that can protect pfSense from downloads. One-time pfSense config:

- Traffic Shaper → Limiter `WAN-in-backups`, dummynet pipe, **~250 Mbit** (below the
  ~300 Mbit saturation knee, leaves headroom for other traffic).
- Firewall rule on WAN-in, dst = `192.168.193.4` (the `ingress-nginx-external`
  LoadBalancer IP), action = pipe to `WAN-in-backups`.

This prevents the reboot scenario regardless of cluster state. It lives outside GitOps
(pfSense XML); record it here and optionally automate via the pfSense API / Ansible
later. **Recommended regardless of Layer 3** — it is the authoritative inbound cap.

## Layer 3 — In-cluster controller (`system/tc-limiter/`)

Per-pod granularity, same-node backpressure, and observability. **Implemented as a
minimal Go controller** (stdlib-only, compiled at container start from a
ConfigMap-injected source on the multi-arch `golang` image — no build pipeline, runs
on amd64 + arm64 nodes).

Responsibilities:

- Find the target pod's CNI netns (via the Cilium agent unix socket → pod IP → netns).
- Apply a `clsact` ingress policer on the pod `eth0` at the configured rate.
- **Verify and keep applied**: read back the filter, compare the rate, re-apply on
  drift or external deletion; periodic forced refresh as a safety net.
- Structured logging on every path (never silent — the key gap of the old shell
  version).

Deliberate v1 limitations:

- `clsact` ingress can only police (drop), not shape. For inbound it provides
  backpressure, not authoritative pacing — that is Layer 2's job.
- Targets a single pod (env-configured `TARGET_POD_SELECTOR` / `RATE_LIMIT`), pinned
  to one node (where the workload's ZFS PVC lives).

Future enhancements (not in v1): annotation-driven multi-pod targeting, CRI/PID
netns discovery (drops the Cilium socket dependency entirely), Prometheus metrics,
node-agnostic scheduling, mirred→veth egress qdisc for true inbound shaping.

## Migration

1. Implement Layer 3 minimal controller (`system/tc-limiter/`).
2. Add Layer 2 pfSense limiter and record the config above.
3. Add alerting: pfSense interrupt% `>25` for 3m (warn) / `>35` for 2m (critical),
   WAN rx|tx `>280M` for 3m, controller not-applied.
4. Audit upload workloads for `egress-bandwidth` (Layer 1).
