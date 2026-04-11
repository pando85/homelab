# IPv6 Implementation Spec

> **Status**: Draft
> **Created**: 2026-04-11
> **Author**: Context from opencode session

## Overview

Enable IPv6 connectivity for Kubernetes cluster nodes and pods. This document contains all context,
diagnostic findings, and implementation steps for an LLM to execute IPv6 enablement in the cluster.

## Context & Current State

### Network Infrastructure

**pfSense Router** (192.168.192.1 / pfsense.grigri):
- **WAN**: PPPoE with DHCPv6-PD from ISP (Orange Spain)
- **IPv6 Prefix**: `2a0c:5a82:927f::/56` delegated from ISP
- **LANBridge (opt4)**: Tracks WAN prefix → `2a0c:5a82:9200:b500::/64`
- **RA Configuration** (radvd enabled):
  ```
  interface bridge0 {
    AdvSendAdvert on;
    AdvManagedFlag on;      # DHCPv6 stateful for addresses
    AdvOtherConfigFlag on;  # DHCPv6 for DNS
    AdvAutonomous on;       # SLAAC also allowed
    prefix 2a0c:5a82:9200:b500::/64 {...}
    RDNSS 2a0c:5a82:9200:b500:5a9c:fcff:fe10:ffc2 {...}
    DNSSL grigri {...}
  }
  ```
- Router working correctly - PC on same network has IPv6 connectivity

### K8s Nodes Problem

**All nodes lack IPv6** despite router providing it:

| Node | Interface | IPv6 State | `accept_ra` |
|------|-----------|------------|-------------|
| grigri | `enp0s20f0` | Only link-local `fe80::` | **0** (blocked) |
| prusik | `eno1` | Only link-local `fe80::` | **0** (blocked) |
| k8s-odroid-hc4-3 | `eth0` | Only link-local `fe80::` | **0** (blocked) |

**Root Cause**: Netplan only configured `dhcp4: true` without IPv6. systemd-networkd sets
`accept_ra=0` when IPv6 is not explicitly configured.

**Sysctl verification**:
```bash
ssh grigri 'sysctl net.ipv6.conf.enp0s20f0.accept_ra'
# Output: net.ipv6.conf.enp0s20f0.accept_ra = 0  ← PROBLEM

ssh prusik 'sysctl net.ipv6.conf.eno1.accept_ra'
# Output: net.ipv6.conf.eno1.accept_ra = 0  ← PROBLEM
```

**Netplan config** (grigri `/etc/netplan/*.yaml`):
```yaml
network:
  ethernets:
    enp0s20f0:
      dhcp4: true
      # MISSING: dhcp6 or accept-ra
  version: 2
```

### Cilium Configuration

**Current** (`system/kube-system/cilium-values.yaml`):
- IPv4 only: `enable-ipv4: true` (implicit)
- IPv6 disabled: `enable-ipv6: false` (in configmap)
- Pod CIDR: `10.0.1.0/24` per node (cluster-pool IPAM)
- Native routing mode with BGP
- No IPv6 masquerading: `IPv6: Disabled` in status

**ConfigMap** (`cilium-config`):
```
enable-ipv6: "false"
enable-ipv6-masquerade: "true"
ipv4-native-routing-cidr: 10.0.0.0/8
ipam: cluster-pool
cluster-pool-ipv4-cidr: 10.0.0.0/8
cluster-pool-ipv4-mask-size: "24"
```

### BGP Setup

pfSense FRR BGP neighbors:
- grigri (192.168.192.2) - AS 64513
- prusik (192.168.192.3) - AS 64513
- k8s-odroid-hc4-3 (192.168.192.23) - AS 64513

Currently only IPv4 routes advertised. IPv6 pod CIDR will need to be added.

---

## Implementation Plan

### Phase 1: Node IPv6 Enablement

#### TODO-1.1: Add IPv6 sysctl settings to Ansible

**File**: `metal/roles/prepare/defaults/main.yml`

**Action**: Add IPv6 kernel parameters to `prepare_sysctl` list:

```yaml
prepare_sysctl:
  # ... existing entries (net.core.somaxconn, fs.inotify.max_user_instances) ...

  # IPv6 Router Advertisement acceptance
  - key: net.ipv6.conf.all.accept_ra
    value: 1
  - key: net.ipv6.conf.default.accept_ra
    value: 1

  # IPv6 forwarding (required for pod networking)
  - key: net.ipv6.conf.all.forwarding
    value: 1
  - key: net.ipv6.conf.default.forwarding
    value: 1
```

**Note**: Setting `all.accept_ra=1` propagates to new interfaces. Existing interfaces need manual
reset or reboot.

#### TODO-1.2: Configure per-node network interface in Ansible inventory

**Files**: `metal/inventory/host_vars/grigri.yml`, `prusik.yml`, `k8s-odroid-hc4-3.yml`

**Action**: Add `prepare_primary_interface` variable:

```yaml
# grigri.yml
prepare_primary_interface: enp0s20f0

# prusik.yml
prepare_primary_interface: eno1

# k8s-odroid-hc4-3.yml
prepare_primary_interface: eth0
```

#### TODO-1.3: Create Ansible task to enable IPv6 RA on existing interfaces

**File**: `metal/roles/prepare/tasks/main.yml`

**Action**: Add task after sysctl configuration:

```yaml
- name: Enable IPv6 RA on primary interface
  sysctl:
    name: "net.ipv6.conf.{{ prepare_primary_interface }}.accept_ra"
    value: 1
    sysctl_set: true
    state: present
  when: prepare_primary_interface is defined
  tags:
    - ipv6
```

#### TODO-1.4: Apply Ansible changes to all nodes

**Command** (user must run manually):
```bash
cd metal
ansible-playbook playbooks/install/prepare.yml -t ipv6
```

**Alternative**: One-time manual fix on each node:
```bash
ssh grigri 'sysctl -w net.ipv6.conf.enp0s20f0.accept_ra=1'
ssh prusik 'sysctl -w net.ipv6.conf.eno1.accept_ra=1'
ssh k8s-odroid-hc4-3 'sysctl -w net.ipv6.conf.eth0.accept_ra=1'
```

#### TODO-1.5: Verify nodes receive IPv6 addresses

**Commands**:
```bash
ssh grigri 'ip -6 addr show enp0s20f0'
ssh prusik 'ip -6 addr show eno1'
ssh k8s-odroid-hc4-3 'ip -6 addr show eth0'
```

**Expected**: Global IPv6 address from `2a0c:5a82:9200:b500::/64` prefix.

**Verification**: Ping IPv6 from node:
```bash
ssh grigri 'ping -6 -c1 ipv6.google.com'
# or
ssh grigri 'curl -6 https://ipv6.google.com'
```

---

### Phase 2: Cilium IPv6 Configuration

#### TODO-2.1: Enable IPv6 in Cilium values

**File**: `system/kube-system/cilium-values.yaml`

**Action**: Add IPv6 configuration at end of file (after line 185):

```yaml
ipv6:
  enabled: true

# IPv6 native routing CIDR (LAN prefix from pfSense)
ipv6NativeRoutingCIDR: "2a0c:5a82:9200:b500::/64"

# IPv6 Pod CIDR allocation
ipam:
  operator:
    # IPv4 unchanged
    clusterPoolIPv4CIDR: 10.0.0.0/8
    clusterPoolIPv4MaskSize: 24
    # IPv6: carve /60 from ISP delegated prefix for pods
    # Note: This requires sub-subnetting the /64 LAN prefix
    # Alternative: Use ULA (fd00::/48) for private pod network
    clusterPoolIPv6CIDR: 2a0c:5a82:9200:b5::/60
    clusterPoolIPv6MaskSize: 64

# Enable IPv6 masquerading for external traffic
ipv6:
  enabled: true
  masquerade: true  # BPF masquerade for IPv6
```

**Decision Required**: IPv6 Pod CIDR strategy:
- **Option A** (recommended): Use ISP prefix sub-subnet `2a0c:5a82:9200:b5::/60`
  - Pods have globally routable IPv6 addresses
  - No NAT66 required
  - Need to configure pfSense to route this prefix

- **Option B**: Use Unique Local Address `fd00::/48`
  - Private IPv6 for pods (like IPv4 private ranges)
  - Requires NAT66/masquerading for external traffic
  - More isolated, safer approach

#### TODO-2.2: Update BGP peering policy for IPv6

**File**: `system/kube-system/resources/cilium/bgp-peering-policy.yaml`

**Action**: Add IPv6 pod CIDR advertisement (if using Option A):

```yaml
spec:
  nodeSelector:
    matchLabels:
      kubernetes.io/os: linux
  virtualRouters:
    - localASN: 64513
      exportPodCIDR: true
      # Add IPv6 CIDR to advertised prefixes
      serviceSelector:
        matchExpressions:
          - key: somekey
            operator: NotIn
            values:
              - never-match
      # Note: Cilium automatically advertises pod CIDRs when exportPodCIDR: true
```

**Verification**: Check BGP advertisements after Cilium restart:
```bash
ssh pfsense.grigri 'vtysh -c "show bgp ipv6 summary"'
ssh pfsense.grigri 'vtysh -c "show bgp ipv6"'
```

#### TODO-2.3: Deploy updated Cilium configuration

**Note**: This is a GitOps repository. Changes must be committed and pushed.

**Commands** (user must run):
```bash
git add system/kube-system/cilium-values.yaml
git commit -m "cilium: Enable IPv6 dual-stack networking"
git push
# ArgoCD will sync automatically, or force sync:
argocd app sync cilium
```

#### TODO-2.4: Verify Cilium IPv6 status

**Commands**:
```bash
kubectl --context=grigri exec -n kube-system daemonset/cilium -- cilium status
# Check for: IPv6: Enabled, IPv6 BIG TCP, Masquerading IPv6: Enabled

kubectl --context=grigri get ciliumnode -o yaml | grep -A5 ipv6
```

---

### Phase 3: Pod IPv6 Verification

#### TODO-3.1: Create test pod with IPv6

**Command**:
```bash
kubectl --context=grigri run ipv6-test --rm -it --restart=Never \
  --image=curlimages/curl:8.12.1 \
  --overrides='{"spec":{"nodeSelector":{"kubernetes.io/hostname":"grigri"}}}' \
  -- curl -6 https://ipv6.google.com
```

#### TODO-3.2: Verify pod has IPv6 address

**Commands**:
```bash
kubectl --context=grigri run ipv6-test --rm -it --restart=Never \
  --image=curlimages/curl:8.12.1 \
  -- ip -6 addr

# Or check Cilium endpoint
kubectl --context=grigri exec -n kube-system daemonset/cilium -- \
  cilium endpoint list | grep ipv6
```

#### TODO-3.3: Test IPv6 connectivity from pod

**Test cases**:
1. External IPv6 (internet):
   ```bash
   kubectl run test --rm -it --image=curlimages/curl -- curl -6 https://cloudflare.com/cdn-cgi/trace | grep ip
   # Expected: ip=2a0c:5a82:9200:b5xx:xxxx
   ```

2. Node-to-pod IPv6:
   ```bash
   ssh grigri 'ping -6 <pod-ipv6-address>'
   ```

3. Pod-to-pod IPv6 (same node):
   ```bash
   # Create two pods and ping between them
   ```

---

### Phase 4: pfSense IPv6 Routing (if using public pod CIDR)

**Note**: Only needed if using Option A (public IPv6 pod CIDR).

#### TODO-4.1: Add static route for IPv6 pod CIDR

**pfSense UI**: `System > Routing > Static Routes`

Add route:
- Network: `2a0c:5a82:9200:b5::/60`
- Gateway: Select appropriate gateway (LANBridge or BGP learned)

**Alternative**: Let BGP handle it - Cilium will advertise pod CIDR via BGP.

#### TODO-4.2: Verify BGP IPv6 route advertisement

**Commands**:
```bash
ssh pfsense.grigri 'vtysh -c "show bgp ipv6 unicast summary"'
ssh pfsense.grigri 'vtysh -c "show route ipv6"'
```

---

## Verification Checklist

After implementation, verify:

- [ ] Nodes have global IPv6 addresses from `2a0c:5a82:9200:b500::/64`
- [ ] Nodes can ping IPv6 internet (`ping -6 ipv6.google.com`)
- [ ] Cilium status shows `IPv6: Enabled`
- [ ] Pods receive IPv6 addresses
- [ ] Pods can reach IPv6 internet
- [ ] BGP advertises IPv6 pod CIDR (check pfSense routing table)
- [ ] Dual-stack services work (IPv4 + IPv6)

---

## Troubleshooting

### Nodes still don't have IPv6 after sysctl change

1. Check router is sending RA:
   ```bash
   ssh grigri 'rdisc6 enp0s20f0'  # or tcpdump for ICMPv6 RA
   ```

2. Force RA request:
   ```bash
   ssh grigri 'rdisc6 -r enp0s20f0'
   ```

3. Check DHCPv6:
   ```bash
   ssh grigri 'dhclient -6 -v enp0s20f0'
   ```

4. Reboot node to reset interface state.

### Pods don't get IPv6

1. Check Cilium IPAM:
   ```bash
   kubectl exec -n kube-system cilium-xxx -- cilium ipam list | grep ipv6
   ```

2. Check CiliumNode has IPv6 CIDR:
   ```bash
   kubectl get ciliumnode grigri -o yaml | grep -A10 ipv6
   ```

3. Restart Cilium pods:
   ```bash
   kubectl rollout restart daemonset/cilium -n kube-system
   ```

### IPv6 connectivity blocked

1. Check pfSense firewall rules for IPv6:
   - LAN → WAN IPv6 pass rules
   - DMZ (VLAN 101) IPv6 rules if nodes on DMZ

2. Check Cilium host firewall (currently disabled):
   ```bash
   kubectl exec -n kube-system cilium-xxx -- cilium status | grep firewall
   ```

---

## Files to Modify

| File | Change |
|------|--------|
| `metal/roles/prepare/defaults/main.yml` | Add IPv6 sysctls |
| `metal/inventory/host_vars/grigri.yml` | Add `prepare_primary_interface` |
| `metal/inventory/host_vars/prusik.yml` | Add `prepare_primary_interface` |
| `metal/inventory/host_vars/k8s-odroid-hc4-3.yml` | Add `prepare_primary_interface` |
| `metal/roles/prepare/tasks/main.yml` | Add IPv6 RA enablement task |
| `system/kube-system/cilium-values.yaml` | Enable IPv6, add IPv6 CIDR config |

---

## Reference Information

### IPv6 Prefixes

- **ISP Delegated**: `2a0c:5a82:927f::/56` (WAN)
- **LAN Prefix**: `2a0c:5a82:9200:b500::/64` (bridge0 - tracked from WAN)
- **Pod CIDR (Option A)**: `2a0c:5a82:9200:b5::/60` (carved from LAN)
- **Pod CIDR (Option B)**: `fd00::/48` (ULA - private)

### Node Interface Names

- `grigri`: `enp0s20f0`
- `prusik`: `eno1`
- `k8s-odroid-hc4-3`: `eth0`

### Router RA Settings

From `/var/etc/radvd.conf` on pfsense.grigri:
- Managed flag: on (DHCPv6 addresses)
- Other config flag: on (DHCPv6 DNS)
- Autonomous: on (SLAAC allowed)
- Prefix: `2a0c:5a82:9200:b500::/64`
- DNS: `2a0c:5a82:9200:b500:5a9c:fcff:fe10:ffc2`
- Domain: `grigri`

### BGP Configuration

- pfSense AS: 64512
- K8s nodes AS: 64513
- Neighbors: grigri, prusik, k8s-odroid-hc4-3 (IPv4)

---

## Execution Order

1. **Phase 1** (Nodes): Ansible changes → Apply → Verify nodes have IPv6
2. **Phase 2** (Cilium): Update values → Commit → Verify Cilium IPv6 enabled
3. **Phase 3** (Pods): Create test pod → Verify connectivity
4. **Phase 4** (pfSense): Verify routing (if public CIDR used)

**Important**: Phase 1 must complete before Phase 2. Cilium IPv6 requires nodes to have IPv6
addresses for native routing.
