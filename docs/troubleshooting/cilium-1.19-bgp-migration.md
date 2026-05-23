# Cilium 1.19 Upgrade: BGP v1 API Removal

## Problem

After upgrading Cilium from 1.18.x to 1.19.x, all BGP peering stops. Symptoms:

- No ingress services reachable (LoadBalancer IPs not routed)
- `cilium-dbg bgp peers` returns empty table
- `CiliumBGPNodeConfig` resources not created
- ArgoCD shows Synced/Healthy but BGP is completely down

## Root Cause

Cilium 1.19 **removed** the `CiliumBGPPeeringPolicy` CRD (v2alpha1 "BGPv1" API) and its control
plane. The old resources are silently ignored — no error is logged, no warning is raised, BGP
simply does nothing.

### Affected Resources

The following CRD was removed:

```yaml
apiVersion: cilium.io/v2alpha1
kind: CiliumBGPPeeringPolicy
```

### Two Additional Issues Discovered

1. **Port 179 permission denied** — the v2 BGP API runs GoBGP as a TCP server on port 179
   (privileged). The Cilium agent needs `CAP_NET_BIND_SERVICE` to bind it.

   ```
   failed to start BGP instance: failed starting BGP server:
   listen tcp4 0.0.0.0:179: bind: permission denied
   ```

2. **Empty advertisement selector selects nothing** — `CiliumBGPAdvertisement` without a
   `selector` matches zero services (not all services). This is different from the v1 API where
   `serviceSelector` with `NotIn` was used to match everything.

## Migration to BGP v2 API

The v2 API splits the old single `CiliumBGPPeeringPolicy` into three resources:

| v2alpha1 (removed) | v2 (replacement) | Purpose |
|---------------------|-------------------|---------|
| `CiliumBGPPeeringPolicy` | `CiliumBGPClusterConfig` | BGP instances, node selector, peer definitions |
| _(inline timers)_ | `CiliumBGPPeerConfig` | Peer settings (timers, auth, address families) |
| _(inline serviceSelector)_ | `CiliumBGPAdvertisement` | What to advertise (Service VIPs, PodCIDR) |

### Before (v2alpha1)

```yaml
apiVersion: cilium.io/v2alpha1
kind: CiliumBGPPeeringPolicy
metadata:
  name: default
spec:
  nodeSelector:
    matchLabels:
      kubernetes.io/os: linux
  virtualRouters:
    - localASN: 64513
      serviceSelector:
        matchExpressions:
          - key: advertise-bgp
            operator: NotIn
            values:
              - "never-used-value"
      neighbors:
        - peerAddress: "192.168.192.1/32"
          peerASN: 64512
```

### After (v2 — three resources)

**CiliumBGPClusterConfig** — BGP instance + peer definitions:

```yaml
apiVersion: cilium.io/v2
kind: CiliumBGPClusterConfig
metadata:
  name: default
spec:
  nodeSelector:
    matchLabels:
      kubernetes.io/os: linux
  bgpInstances:
    - name: "instance-64513"
      localASN: 64513
      localPort: 179
      peers:
        - name: "peer-64512"
          peerASN: 64512
          peerAddress: "192.168.192.1"
          peerConfigRef:
            name: default-peer
```

**CiliumBGPPeerConfig** — timer and family settings:

```yaml
apiVersion: cilium.io/v2
kind: CiliumBGPPeerConfig
metadata:
  name: default-peer
spec:
  timers:
    connectRetryTimeSeconds: 120
    holdTimeSeconds: 90
    keepAliveTimeSeconds: 30
  families:
    - afi: ipv4
      safi: unicast
      advertisements:
        matchLabels:
          advertise: bgp
```

**CiliumBGPAdvertisement** — what routes to advertise:

```yaml
apiVersion: cilium.io/v2
kind: CiliumBGPAdvertisement
metadata:
  name: default-advertisement
  labels:
    advertise: bgp
spec:
  advertisements:
    - advertisementType: "Service"
      selector:
        matchExpressions:
          - key: cilium.io/bgp-skip
            operator: DoesNotExist
      service:
        addresses:
          - LoadBalancerIP
```

### Key Differences

| Aspect | v1 (v2alpha1) | v2 (cilium.io/v2) |
|--------|---------------|--------------------|
| `peerAddress` | CIDR notation (`192.168.192.1/32`) | Plain IP (`192.168.192.1`) |
| Timers | Inline in `neighbors` | Separate `CiliumBGPPeerConfig` resource |
| Service matching | `serviceSelector` on virtualRouter | `selector` on `CiliumBGPAdvertisement` |
| Match all services | `NotIn` with dummy value | `DoesNotExist` on non-existent key (opt-out) |
| Port binding | Handled internally | Requires `CAP_NET_BIND_SERVICE` |

### Required Helm Values Change

Add `NET_BIND_SERVICE` to the Cilium agent capabilities:

```yaml
securityContext:
  capabilities:
    ciliumAgent:
      # ... existing capabilities ...
      - NET_BIND_SERVICE  # Required for BGP v2 to bind privileged port 179
```

## Verification

```bash
# Check BGP peers are established
kubectl exec -n kube-system <cilium-pod> -- cilium-dbg bgp peers

# Check routes are advertised
kubectl exec -n kube-system <cilium-pod> -- cilium-dbg bgp routes advertised

# Check node configs were auto-generated
kubectl get ciliumbgpnodeconfigs.cilium.io

# Check for permission errors in logs
kubectl logs -n kube-system <cilium-pod> | grep "permission denied"
```

### Expected Output

```
# bgp peers
Local AS   Peer AS   Peer Address        Session       Uptime   Family         Received   Advertised
64513      64512     192.168.192.1:179   established   3m       ipv4/unicast   9          8

# bgp routes advertised
VRouter   Peer            Prefix              NextHop         Age   Attrs
64513     192.168.192.1   192.168.193.1/32    192.168.192.3   30s   [{Origin: i} {AsPath: 64513}]
...
```

## Files Changed

- `system/kube-system/resources/cilium/bgp-cluster-config.yaml` — new CiliumBGPClusterConfig
- `system/kube-system/resources/cilium/bgp-peer-config.yaml` — new CiliumBGPPeerConfig
- `system/kube-system/resources/cilium/bgp-advertisement.yaml` — new CiliumBGPAdvertisement
- `system/kube-system/resources/cilium/bgp-peering-policy.yaml` — removed (v2alpha1)
- `system/kube-system/kustomization.yaml` — updated resource references
- `system/kube-system/cilium-values.yaml` — added `NET_BIND_SERVICE` capability

## Lessons Learned

1. **Pre-create v2 resources before upgrading** — the migration guide recommends creating the new
   v2 resources before removing v1, so BGP transitions without downtime.

2. **ArgoCD sync does not mean healthy** — the old `CiliumBGPPeeringPolicy` was synced
   successfully but silently ignored by Cilium 1.19. Always verify with `cilium-dbg bgp peers`.

3. **Check Cilium upgrade notes** — the removal was documented at
   https://docs.cilium.io/en/v1.19/operations/upgrade/#upgrade-notes but easy to miss.

4. **Kernel matters** — Cilium 1.19.0 had a BPF verifier kernel bug affecting kernels 6.17-6.19
   (issues [#44216](https://github.com/cilium/cilium/issues/44216),
   [#44430](https://github.com/cilium/cilium/issues/44430)). Kernels below 6.17 (like our
   6.8.x and 6.12.x) are unaffected.
