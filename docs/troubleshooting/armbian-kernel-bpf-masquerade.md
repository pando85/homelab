# Armbian Kernel 6.12 BPF Masquerade Regression (Odroid HC4)

## Problem

Pods on the Odroid HC4 node (`k8s-odroid-hc4-3`) could not resolve external DNS names. The
`external-dns` pod was in `CrashLoopBackOff` with:

```
dial tcp: lookup api.cloudflare.com: i/o timeout
```

The `KaniopReconcileFailures` and `KaniopReconcileDurationHigh` alerts were also firing for pods on
this node. All symptoms pointed to broken external connectivity for UDP traffic from pods.

## Root Cause

The Odroid HC4 runs Armbian with the `current-meson64` kernel. Armbian bumped the `current` branch
from **6.6 LTS** to **6.12** in package versions `25.x`. Kernel 6.12 (`6.12.58-current-meson64` on
aarch64) has a regression in Cilium's BPF masquerading that causes **UDP SNAT to silently fail**.

Evidence:

- `cilium-dbg bpf nat list` on the odroid node had **zero UDP NAT entries**, while TCP NAT worked
  fine
- On the x86_64 nodes (kernel 6.8.0), both TCP and UDP NAT entries were present and working
- nodelocaldns logs showed timeouts when forwarding DNS to the gateway:
  `read udp 10.0.3.152:34796->192.168.192.1:53: i/o timeout`
- Without UDP SNAT, DNS response packets from the gateway could not be routed back to pod IPs

The broken chain was:

1. Pod sends DNS query to nodelocaldns (169.254.25.10:53)
2. nodelocaldns forwards to gateway DNS (192.168.192.1:53) using its pod IP (10.0.3.152)
3. BPF masquerading should SNAT the pod IP to the node IP (192.168.192.23) for egress
4. On kernel 6.12, UDP SNAT did not execute — the packet left with an unroutable source IP
5. Gateway could not send a reply back → DNS timeout

## How to Diagnose

```bash
# Check kernel versions across nodes
kubectl --context=grigri get nodes -o wide

# Check for zero UDP NAT entries (broken) vs populated entries (working)
kubectl --context=grigri exec -n kube-system cilium-<pod-on-node> -- \
  cilium-dbg bpf nat list | grep UDP

# Check nodelocaldns logs for DNS forwarding timeouts
kubectl --context=grigri logs -n kube-system <nodelocaldns-pod> --tail=30

# Compare TCP NAT (should work) vs UDP NAT (broken on affected kernel)
kubectl --context=grigri exec -n kube-system cilium-<pod-on-node> -- \
  cilium-dbg bpf nat list | grep -c TCP
kubectl --context=grigri exec -n kube-system cilium-<pod-on-node> -- \
  cilium-dbg bpf nat list | grep -c UDP
```

## Fix / Workaround

Downgrade the Armbian kernel to 6.6 LTS and hold the package:

```bash
# On the Odroid node via SSH:
sudo apt-get install -y --allow-downgrades \
  linux-image-current-meson64=24.11.1 \
  linux-dtb-current-meson64=24.11.1

sudo apt-mark hold linux-image-current-meson64 linux-dtb-current-meson64
sudo reboot
```

After reboot, verify with:

```bash
ssh k8s-odroid-hc4-3 uname -r
# Should show: 6.6.63-current-meson64

kubectl --context=grigri exec -n kube-system cilium-<new-pod> -- \
  cilium-dbg bpf nat list | grep UDP
# Should show XLATE_SRC/XLATE_DST entries
```

## Notes

- The kernel hold (`apt-mark hold`) prevents `unattended-upgrade` from bumping back to the broken
  6.12 kernel. Remove the hold only after confirming a future kernel version fixes the BPF
  masquerading regression.
- Armbian `current-meson64` package versions `24.x` ship kernel 6.6 LTS; versions `25.x` ship
  6.12; version `26.x` ships 6.18 (which has separate BPF verifier regression reports).
- The Cilium configuration (`enable-bpf-masquerade: true`, `enable-tcx: true`) is unchanged — the
  issue is entirely a kernel BPF subsystem regression on ARM64.
