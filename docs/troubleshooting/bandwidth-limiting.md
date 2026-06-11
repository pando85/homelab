# Pod Bandwidth Limiting

## Problem

High-throughput services (e.g., cross-backups MinIO receiving 300+ Mbps uploads from external
sources) saturate pfSense CPU via interrupt processing on the PPPoE WAN interface. pfSense runs on
an AMD GX-412TC SOC (4 cores, 1 GHz) which handles packet processing in software — high throughput
directly translates to high CPU in interrupt context.

## What Does NOT Work

### Cilium `kubernetes.io/ingress-bandwidth` Annotation

The `kubernetes.io/ingress-bandwidth` pod annotation is a **no-op for same-node traffic**. Cilium
BPF code (in `l3.h`) has a `!from_host` check that explicitly skips traffic originating from the
same node. When client and server pods are co-located (e.g., ingress-nginx-external and MinIO both
on node `prusik`), the annotation is never enforced.

### Host-side `tc` Qdiscs (TBF, clsact)

Cilium v1.18+ uses TCX (BPF token-based attach) which **bypasses all `tc` qdiscs** on host-side
devices:

- **`lxc*` veth devices** (host side of pod veth pair): TBF root qdisc, clsact filters — all show
  0 bytes processed
- **`cilium_host` device**: clsact filters — also 0 bytes
- **TCX intercepts packets before the tc layer** on the host side

This means you cannot shape or police traffic using `tc` on any host-side network interface when
Cilium TCX is active.

### Nginx `limit_rate`

The nginx ingress annotation `nginx.ingress.kubernetes.io/limit-rate` only throttles HTTP response
bodies (downloads). It does **not** limit upload request bodies.

## What Works: tc Policer Inside Pod Network Namespace

TCX is not attached inside the pod's network namespace. Inside the pod, `eth0` is a regular
interface where `tc` works normally.

### Solution: clsact Ingress Policer

Apply a clsact ingress policer inside the pod's CNI network namespace:

```bash
# Find the pod's CNI netns on the node
POD_IP="10.0.4.101"
NODE="prusik"

# Get the CNI netns name (matches pod IP)
NS_NAME=$(ssh $NODE "for ns in /var/run/netns/*; do
  ip netns exec \$(basename \$ns) ip -4 addr show eth0 2>/dev/null | grep -q '$POD_IP' && basename \$ns && break
done")

# Apply clsact ingress policer
ssh $NODE "sudo ip netns exec $NS_NAME tc qdisc del dev eth0 clsact 2>/dev/null; \
  sudo ip netns exec $NS_NAME tc qdisc add dev eth0 clsact && \
  sudo ip netns exec $NS_NAME tc filter add dev eth0 ingress matchall \
    action police rate 100Mbit burst 1540 conform-exceed drop"
```

### Why It Works

1. Packets enter the pod via `eth0` after Cilium routing
2. The clsact policer drops excess packets
3. TCP congestion control detects drops and reduces send rate
4. Overall throughput drops to the configured limit within 1-2 minutes
5. Fewer packets reach pfSense → lower interrupt load

### Verifying It's Working

```bash
# Check tc statistics inside pod netns
ssh $NODE "sudo ip netns exec $NS_NAME tc -s filter show dev eth0 ingress"
# Look for: "action drop" with non-zero "overlimits" counter

# Check pfSense CPU
# Via Grafana: 100 - avg(rate(node_cpu_seconds_total{instance="pfsense.grigri",mode="idle"}[5m])) * 100
# Or via Prometheus datasource UID: prometheus
```

## DaemonSet: system/tc-limiter/

The persistent GitOps solution is `system/tc-limiter/` — a DaemonSet that:

1. Connects to Cilium unix socket (`/var/run/cilium/cilium.sock`) to get endpoint IP mappings
2. Iterates CNI network namespaces (`/var/run/netns/*`) to find the matching pod
3. Applies the clsact ingress policer inside the matched pod's netns
4. Runs on node `prusik` only (via `nodeSelector`)

### Configuration

The rate limit is controlled via the `RATE_LIMIT` environment variable in
`system/tc-limiter/values.yaml`:

```yaml
env:
  RATE_LIMIT: "100mbit"
```

### Requirements

- Runs as `runAsUser: 0` with `NET_ADMIN` and `SYS_ADMIN` capabilities
- `hostNetwork: true` for network namespace access
- Mounts `/var/run/netns` (CNI network namespaces) and `/var/run/cilium/cilium.sock`
- Uses `ip netns exec` instead of `nsenter` (not available in Alpine/BusyBox)

## tc-limiter Socket Staleness (June 2026)

### Problem

tc-limiter mounts `/var/run/cilium` as a hostPath. When Cilium restarts, it recreates
`cilium.sock` with a new inode. Without `mountPropagation: HostToContainer`, the
tc-limiter container continues seeing the old (orphaned) socket and gets `Connection refused`
on every API call — silently failing to apply rate limits.

### Symptoms

- tc-limiter logs show startup message but no "Applying rate limit" entries
- `curl --unix-socket /var/run/cilium/cilium.sock` returns exit code 7 (Connection refused)
- Socket timestamp in container is older than Cilium pod start time
- No tc filters on pod's eth0: `ip netns exec <cni-ns> tc filter show dev eth0 ingress` is empty

### Fix

Ensure `mountPropagation: HostToContainer` on both hostPath mounts in
`system/tc-limiter/values.yaml`:

```yaml
persistence:
  cilium-socket:
    type: hostPath
    hostPath: /var/run/cilium
    globalMounts:
      - path: /var/run/cilium
        mountPropagation: HostToContainer
  cni-netns:
    type: hostPath
    hostPath: /var/run/netns
    globalMounts:
      - path: /var/run/netns
        mountPropagation: HostToContainer
```

### Verification

```bash
# Check tc-limiter can reach Cilium API
kubectl --context=grigri exec -n tc-limiter -l app.kubernetes.io/name=tc-limiter -- \
  curl -sf --unix-socket /var/run/cilium/cilium.sock 'http://localhost/v1/endpoint' | head -5

# Check tc rules are applied
kubectl --context=grigri logs -n tc-limiter -l app.kubernetes.io/name=tc-limiter --tail=10
# Should show: "Applying 100mbit rate limit in netns cni-..."

# Verify on the node
ssh prusik "sudo ip netns exec <cni-ns> tc -s filter show dev eth0 ingress"
# Should show: "action order 1: police ... rate 100Mbit ... action drop" with non-zero overlimits
```

## Top Traffic Suspects

When pfSense shows high CPU / interrupt load, these are the most likely causes ordered by
historical impact:

### 1. rclone backup syncs → cross-backups MinIO

- **Source:** External rclone clients (e.g. `79.116.82.10`, `79.117.29.85`)
- **Path:** WAN → pfSense → ingress-nginx-external → cross-backups-minio
- **Pattern:** Sustained 35-50 MB/s upload, heavy HEAD request storms for file existence
  checks + large PUT multipart uploads (50-80 MB chunks)
- **Buckets:** `milla` (personal backups), `dabol` (quarterly backups)
- **CronJobs:** `rclone-sync` (Thu 02:30 UTC), `rclone-sync-bis` (Fri 02:30 UTC) in velero ns
- **Mitigation:** tc-limiter at 100mbit on Minio pod; add `--bwlimit` to rclone as defense-in-depth
- **Check:**
  ```bash
  kubectl --context=grigri logs -n ingress-nginx-external -l app.kubernetes.io/name=ingress-nginx-external --tail=500 | grep cross-backup
  kubectl --context=grigri get jobs -n velero
  ```

### 2. qBittorrent seeding

- **Source:** BitTorrent peers over WAN
- **Path:** WAN → pfSense → ingress-nginx-external / Cilium host-lb → qbittorrent
- **Pattern:** Up to 9 MB/s upload (limited by app), ~2000 seeding torrents, 500-3000 peer
  connections creating high state table entries and packet rate
- **Mitigation:** App-level bandwidth limit (5 MB/s upload), connection limits in qBittorrent config
- **See:** `docs/troubleshooting/qbittorrent-performance.md`

### 3. Ingress-nginx-external (pass-through)

- Not a consumer itself, but aggregates all external traffic — shows high cumulative bandwidth
- Use ingress logs to identify the actual backend:
  ```bash
  kubectl --context=grigri logs -n ingress-nginx-external -l app.kubernetes.io/name=ingress-nginx-external --tail=1000 \
    | awk '{print $1}' | sort | uniq -c | sort -rn | head -20
  ```

### 4. Velero/rclone egress (outbound backups)

- **Source:** rclone jobs in velero namespace pushing backups to remote storage
- **Pattern:** Sustained egress, limited by `kubernetes.io/egress-bandwidth: 100M` annotation
- **Check:** `kubectl --context=grigri get jobs -n velero`

## Quick Diagnosis Workflow

When pfSense load is high:

```bash
# 1. Check pfSense CPU and interrupt %
ssh pfsense.grigri "top -S -n | head -15"

# 2. Check WAN traffic rate
ssh pfsense.grigri "netstat -I igb0 -w 1 -c 2"

# 3. Check top network consumers (Prometheus, instant query)
#   topk(10, sum by (namespace, pod) (rate(container_network_receive_bytes_total[5m])))

# 4. Check ingress access logs for the source
kubectl --context=grigri logs -n ingress-nginx-external -l app.kubernetes.io/name=ingress-nginx-external --tail=500 \
  | awk '{print $1}' | sort | uniq -c | sort -rn | head -10

# 5. Verify tc-limiter is working
kubectl --context=grigri logs -n tc-limiter -l app.kubernetes.io/name=tc-limiter --tail=10

# 6. Check active rclone jobs
kubectl --context=grigri get jobs -n velero
```

## Traffic Path (External → Pod)

```
External Source → pfSense WAN (pppoe0) → LAN (igb1.101)
  → prusik host → cilium_host (TCX egress BPF)
  → Cilium internal routing → lxc veth (host side, TCX active)
  → pod eth0 (no TCX, tc works) → application
```

## pfSense CPU Context

| Metric | Unthrottled | With 50Mbit limit | With 100Mbit limit |
|--------|-------------|-------------------|---------------------|
| CPU usage | 63%+ | ~7% | ~8% |
| Interrupt % | 30%+ | ~1.2% | ~1.5% |
| WAN throughput | 300+ Mbps | ~3 Mbps | ~100 Mbps |

The pfSense state table limit (402,000) was never the issue — only ~1,300 entries were in use.
The bottleneck is purely CPU interrupt processing on the PPPoE interface.

## Quick Diagnostics

### Check if tc policer is active

```bash
kubectl --context=grigri logs -n tc-limiter -l app.kubernetes.io/name=tc-limiter --tail=20
```

### Check pfSense CPU via Grafana

PromQL (datasource UID: `prometheus`):

```promql
100 - (avg by (instance) (rate(node_cpu_seconds_total{instance="pfsense.grigri",mode="idle"}[5m])) * 100)
avg by (instance) (rate(node_cpu_seconds_total{instance="pfsense.grigri",mode="interrupt"}[5m])) * 100
```

### Check WAN throughput

```promql
rate(node_network_receive_bytes_total{device="pppoe0",instance="pfsense.grigri"}[2m]) * 8
```

### Check cross-backups traffic

```promql
rate(container_network_receive_bytes_total{namespace="cross-backups"}[2m]) * 8
```
