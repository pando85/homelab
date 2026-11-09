# Cilium Networking

- Cilium 1.19+ uses the **v2 BGP API** (`CiliumBGPClusterConfig` + `CiliumBGPPeerConfig` +
  `CiliumBGPAdvertisement`). The old `CiliumBGPPeeringPolicy` (v2alpha1) was **removed** — see
  `docs/troubleshooting/cilium-1.19-bgp-migration.md`
- BGP v2 requires `CAP_NET_BIND_SERVICE` in Cilium agent capabilities to bind port 179
- `CiliumBGPAdvertisement` without a `selector` selects **nothing** (not all services) — use
  `matchExpressions` with `DoesNotExist` on a non-existent key to match all services
- Cilium v1.18+ uses TCX BPF which **bypasses all host-side `tc` qdiscs** — TBF, clsact on `lxc*`
  veth devices and `cilium_host` all show 0 bytes
- `kubernetes.io/ingress-bandwidth` pod annotation does **NOT work for same-node traffic** (Cilium
  BPF has `!from_host` check that skips local traffic)
- To rate-limit pod ingress: use tc clsact policer inside pod CNI netns (see
  `system/tc-limiter/` and `docs/troubleshooting/bandwidth-limiting.md`)
- pfSense CPU bottleneck is interrupt processing on PPPoE — limit high-throughput services at the
  pod level before traffic reaches the router
- Check troubleshooting docs (`docs/troubleshooting/`) for service-specific tuning guidance before
  adjusting settings — they may contain recommended values and root cause analyses

## Kubelet probes under enforced policies

Measured 2026-11-09 on `grigri` (Cilium `v1.20.4`, `enable-policy: default`,
`enable-host-firewall: false`, `allow-localhost` unset) with disposable probe pods and CNPs in
`default`, all deleted afterwards. Matrix numbers:
`docs/troubleshooting/mcp-firecrawl-egress-regression.md`.

- **An INGRESS section is what breaks kubelet `httpGet`/`tcpSocket` probes.** Any policy set that puts an
  ingress rule on a probed pod makes probes time out with `context deadline exceeded` — native
  NetworkPolicy and CiliumNetworkPolicy alike. Egress-only policy sets did not break a single probe in
  any run. A `context deadline exceeded` therefore means the **request was denied on ingress**, not that
  a reply was lost on egress; ingress is stateful and probe replies traverse it unharmed.
- **The kubelet probe source cannot be matched by `host`, `remote-node`, `cluster` or a node-subnet
  `fromCIDR`** while `enable-host-firewall: false`. Only `world` matched. Any advice of the form "allow
  the node with `fromEntities`/`toEntities: [host, remote-node]`" is **falsified** — that cure does not
  work here, and it is not what the one long-lived policy-covered workload relies on:
  `calypso-runners/calypso-control-plane-runner-policy` (273 d, `VALID=True`) survives because its
  ingress `fromEntities` also lists `world` (verified live, not inferred).
- **Production-safe shape (proven):** keep the probe on a **dedicated health port**; allow that port from
  `fromEntities: [world]` and *nothing else* in that rule; serve the real app port and the metrics port
  from `fromEndpoints`; egress `toEntities: [cluster]`. Do **not** put `cluster` on the health port — in
  one run it shared the rule list and leaked the app port.
- **Native Kubernetes NetworkPolicy cannot express this at all** — no entities, and a node `ipBlock`
  does not match the probe source. Any native NP with `policyTypes: [Ingress]` on a probed pod breaks it.
  Use a CiliumNetworkPolicy in the shape above, or make the probes `exec`, which is immune by
  construction.
- **Accepted residual exposure:** `world` opens only that dedicated health port to anything routable to
  the pod IP, and only the health body. If the owner rejects that, the alternatives are `exec` probes or
  `enable-host-firewall=true` — cluster-wide and **untested here**, and the only thing that would make
  `host`/`remote-node` matching meaningful.
- **Timing is safe:** applying the CNP *before* the endpoint exists works (attached 6 m 32 s earlier →
  Ready in 6 s, zero probe failures). Apply at or before the workload and keep
  `failureThreshold >= 6` anyway.
- Aggregate Hubble/deny counters still prove nothing — cumulative and unattributable. Never document a
  datapath behaviour as "proven" without a disposable in-cluster test on this cluster; a
  `helm template` render or a look-alike cluster is not evidence.
