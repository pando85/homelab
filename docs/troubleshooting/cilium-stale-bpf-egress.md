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

## Notes

- This is distinct from the Armbian kernel BPF masquerade regression
  (`docs/troubleshooting/armbian-kernel-bpf-masquerade.md`) which affects only UDP SNAT on ARM64.
  This issue affects all traffic from pod network to host network.
- The NodeLocal DNS + Cilium kube-proxy replacement combination is inherently fragile because
  `169.254.25.10` is not managed by Cilium's service controller. Consider whether NodeLocal DNS is
  needed when Cilium already provides efficient DNS load balancing.
- Root cause of the BPF state staleness is unclear — may be related to long Cilium pod uptime
  combined with endpoint churn (pod creation/deletion) accumulating inconsistencies in BPF maps.
