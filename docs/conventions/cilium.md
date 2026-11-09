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
- **The kubelet probe source cannot be matched by `host`, `remote-node`, `cluster` or any `fromCIDR`**
  while `enable-host-firewall: false`. The proven probe source address is **10.0.4.252**, the node's
  `cilium_host` link address (`ip route get <podIP>` => `dev cilium_host src 10.0.4.252`); node pod
  CIDRs are prusik 10.0.4.0/24, grigri 10.0.1.0/24, k8s-amd64-1 10.0.0.0/24. S6a `fromCIDR:
  ["10.0.4.252/32"]` and S6b `fromCIDR: ["10.0.4.0/24"]` were both accepted by the CNP validator
  (`Valid=True`, no warning about overlapping the cluster range) yet the probe was still **DENIED**
  in both cases. Only `world` matched. Any advice of the form "allow the node with
  `fromEntities`/`toEntities: [host, remote-node]`" is **falsified** — that cure does not work here,
  and it is not what the one long-lived policy-covered workload relies on:
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
  the pod IP, and only the health body. S6c proved that `world` does **not** admit cluster-identity
  pods: an in-cluster client pod whose labels were not in the `fromEndpoints` rule was denied on the
  health port (timeout), while a client pod with the permitted labels was allowed on the app port 8080
  and denied on 8081. So the residual exposure is narrower than it first appears: a compromised pod
  inside the cluster cannot reach the health port; only world-identity traffic (the node's kubelet,
  and anything unclassified) can. If the owner rejects even that, the alternatives are `exec` probes
  or `enable-host-firewall=true` — cluster-wide, **untested here**, and **not a proven cure for
  probes** because the probe source is a pod-network link address (10.0.4.252 via `cilium_host`)
  rather than a node address, so enabling the host firewall may reclassify nothing; still untested.
- **Timing is safe:** applying the CNP *before* the endpoint exists works (attached 6 m 32 s earlier →
  Ready in 6 s, zero probe failures). Apply at or before the workload and keep
  `failureThreshold >= 6` anyway.
- Aggregate Hubble/deny counters still prove nothing — cumulative and unattributable. Never document a
  datapath behaviour as "proven" without a disposable in-cluster test on this cluster; a
  `helm template` render or a look-alike cluster is not evidence.
