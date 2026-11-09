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

- **Any enforced Cilium policy set on a workload must permit BOTH legs of the kubelet probe path**:
  ingress to the probe port from the node (`fromEntities: [host, remote-node]`) **and** egress back to
  the node (`toEntities: [host, remote-node]`, or a port-scoped `toEntities: [cluster]` as
  `calypso-runners/calypso-control-plane-runner-policy` does). `cluster` is documented to include the
  `host` and `remote-node` entities, so a `cluster` egress rule covers the node.
- **A probe `context deadline exceeded` does not mean the request was never delivered.** The request can
  have reached the pod and its reply been dropped by the egress side. Attach the symptom to a direction
  only from a per-policy/per-port verdict, never from the timeout alone.
- **Aggregate Hubble/deny counters prove nothing** — they are cumulative and unattributable to a
  namespace, policy or port. Capture `cilium-dbg monitor -t deny`, the CT entry and a targeted dial.
- Measured on this cluster (`cilium-config`, 2026-11-09): `enable-host-firewall: false`,
  `enable-policy: default`, `allow-localhost` unset — so upstream cilium/cilium#21965 (host-egress
  policy denying kubelet, needs `enable-host-firewall: true`) is **not** our configuration, and per
  Cilium docs local host→pod ingress is allowed implicitly unless `--allow-localhost=policy`. Both
  facts mean an *ingress* omission on the node path is a weak explanation; the egress/return leg is the
  live candidate and is being measured, not assumed (see
  `docs/troubleshooting/mcp-firecrawl-egress-regression.md`).
- **Native Kubernetes NetworkPolicy cannot name a node peer** — that is an API limitation only, not a
  Cilium one (Cilium has the entities above). It must never be used as an explanation for a failure that
  happened with a Cilium-native policy attached, and it is not a reason to skip the egress allowance.
- **Never document a datapath behaviour as "proven" without a disposable in-cluster test on this
  cluster** (real routing, kernel, datapath). A look-alike cluster or a `helm template` render is not
  evidence.
- Chart/source hygiene: comments in charts that justify *omitting* the probe port from a policy by
  "the node cannot be named anyway" are misleading for the reason above and must be corrected before any
  re-land.
