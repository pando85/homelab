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
