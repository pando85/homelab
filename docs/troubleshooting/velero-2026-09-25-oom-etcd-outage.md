# Velero Backup Stalled — 2026-09-25 Memory Pressure / etcd Timeout

## Summary

On 2026-09-25, a scheduled Velero backup became stuck `InProgress` and blocked the global backup
queue. Root cause: CI Rust builds on node `prusik` drove memory pressure to 5.9% MemAvailable
(3.9 GB), triggering memory PSI stalls and NVMe reclaim I/O that caused embedded etcd ReadIndex
transactions to time out. k3s lost leader election and exited (code 1) at 02:20:54Z. systemd
restarted k3s, but the interruption left Velero and OpenEBS ZFS backup operations in an
inconsistent state — six ZFSBackup CRs stuck in `Init` and the Velero Backup CR permanently
`InProgress`.

## Timeline (all times UTC, 2026-09-25)

| Time         | Event                                                                                  |
|--------------|----------------------------------------------------------------------------------------|
| ~02:00       | CI Rust builds on `prusik` drive MemAvailable down to 3.9 GB (5.9%)             |
| ~02:00–02:20 | Memory PSI rises; reclaim I/O saturates nvme1n1 (queue depth 218)                |
| ~02:15       | nvme1n1 flush latency rises; etcd ReadIndex and transactions begin timing out    |
| 02:20:54     | k3s loses embedded-etcd leader election and exits with code 1                    |
| 02:20:59+    | systemd restarts k3s; the API server returns                                     |
| 02:21+       | Velero backup remains `InProgress`; six ZFSBackup CRs remain in `Init`            |

## Root Cause Chain

1. **Memory pressure** — CI Rust builds consumed most available RAM on `prusik`.
2. **I/O saturation** — Memory reclaim drove NVMe queue depth to 218 with elevated flush latency.
3. **etcd timeout** — Embedded etcd could not complete ReadIndex/transactions within lease deadlines.
4. **k3s crash** — Leader election lost; k3s process exited (code 1), then restarted via systemd.
5. **Interrupted backup** — Active Velero/OpenEBS ZFS operations were mid-flight when the API server
   went down. The Velero Backup CR was left in `InProgress` and the openebs-zfs-node agent left six
   ZFSBackup CRs in `Init`.

## Ruled Out

- **Dependent-clone failures** — Unrelated; caused by old snapshot GC, not this incident.
- **MinIO** — S3 endpoint was reachable and not a contributing factor.
- **Cilium** — Network datapath was healthy; not a root cause.
- **Sep 24 outage claim** — Retracted; no correlation with this incident.
- **Journal labeling** — Journals were shipped, but their Loki streams lacked `node_name`, which
  prevented node-scoped queries. See `docs/troubleshooting/vector-journal-node-label.md`.

## How to Diagnose

### Memory pressure on the node

```promql
# MemAvailable ratio (should be > 10%; < 5% is critical)
node_memory_MemAvailable_bytes{instance="prusik"} / node_memory_MemTotal_bytes{instance="prusik"}

# Memory PSI — stalled/waiting (seconds of stall per wall-clock second)
rate(node_pressure_memory_stalled_seconds_total{instance="prusik"}[5m])
rate(node_pressure_memory_waiting_seconds_total{instance="prusik"}[5m])
```

### NVMe I/O saturation

```promql
# NVMe queue depth (sustained > 100 indicates saturation)
rate(node_disk_reads_completed_total{instance="prusik",device="nvme1n1"}[5m])
  + rate(node_disk_writes_completed_total{instance="prusik",device="nvme1n1"}[5m])

# Weighted I/O time (sustained > 100% indicates saturation)
rate(node_disk_io_time_weighted_seconds_total{instance="prusik",device="nvme1n1"}[5m])

# Current I/O operations in progress
node_disk_io_now{instance="prusik",device="nvme1n1"}

# Flush latency: average time per flush request
rate(node_disk_flush_requests_time_seconds_total{instance="prusik",device="nvme1n1"}[5m])
  / rate(node_disk_flush_requests_total{instance="prusik",device="nvme1n1"}[5m])
```

### etcd / k3s health

The embedded etcd disk and leader-change metrics were not scraped during the incident. Use the k3s
journal to confirm etcd ReadIndex timeouts and leader-election loss:

```logql
{job="systemd-journal", node_name="prusik", systemd_unit="k3s.service"} |~ "ReadIndex|leader election|leaderelection lost"
{job="systemd-journal", node_name="prusik", systemd_unit="k3s.service"} |~ "Main process exited|Failed with result|Starting k3s"
```

Historical journal entries from before the Vector label fix have no `node_name`; omit that matcher
when investigating older incidents.

### Velero / OpenEBS state

```bash
# Stuck backup
kubectl --context=grigri get backups -n velero
kubectl --context=grigri get backup <name> -n velero -o yaml

# Stuck ZFSBackup CRs
kubectl --context=grigri get zb -n zfs-localpv | grep -v Done

# Velero pod logs around the incident window
kubectl --context=grigri logs -n velero -l app.kubernetes.io/name=velero \
  --since-time=2026-09-25T02:00:00Z --tail=200
```

## Recovery Procedure

Recovery is layered: restore the control plane first, then clear the backup queue, then verify
residual state. All mutating commands are marked with **[MUTATING]**.

### Step 1: Verify control plane health

```bash
# Read-only: confirm k3s is running and etcd is healthy
kubectl --context=grigri get nodes
kubectl --context=grigri get --raw /healthz
```

If k3s is not running, let systemd restart it. Do **not** manually start k3s.

### Step 2: Restart Velero pod **[MUTATING]**

Clears internal sync-controller state and re-establishes watches:

```bash
kubectl --context=grigri delete pod -n velero -l app.kubernetes.io/name=velero
```

### Step 3: Delete the stuck Velero Backup **[MUTATING]**

The `InProgress` backup will never complete. Delete it to unblock the global queue:

```bash
kubectl --context=grigri delete backup <backup-name> -n velero
```

### Step 4: Delete stuck ZFSBackup CRs **[MUTATING]**

```bash
kubectl --context=grigri get zb -n zfs-localpv | grep -v Done
# Delete each stuck CR:
kubectl --context=grigri delete zb -n zfs-localpv <cr-name>
```

### Step 5: Restart openebs-zfs-node agent **[MUTATING]**

Re-establishes the ZFSBackup watch on the affected node:

```bash
kubectl --context=grigri delete pod -n zfs-localpv -l app=openebs-zfs-node \
  --field-selector=spec.nodeName=prusik
```

### Step 6: Verify residual state

After restarts, confirm no orphaned resources remain:

```bash
# No stuck backups
kubectl --context=grigri get backups -n velero | grep -E "InProgress|Failed"

# No stuck ZFSBackup CRs
kubectl --context=grigri get zb -n zfs-localpv | grep -v Done

# No stale zfs send processes
ssh prusik 'ps aux | grep "zfs send" | grep -v grep'

# No orphaned ZFS snapshots from this backup
ssh prusik 'zfs list -H -t snapshot -o name | grep <backup-name>'
```

If orphaned ZFS snapshots exist, clean them up per `velero-backup-failures.md`.

If the backup-sync controller recreates the deleted Backup CR (because data exists in the S3
bucket), retrieve credentials from `pass` or Vault without exposing them on the command line,
then remove the bucket data:

```bash
# [MUTATING] Remove backup data from S3 to break the sync loop
# Retrieve credentials from pass: pass show velero/access-keys
# Or from Vault: vault read secret/data/velero/access-keys
aws --endpoint-url=https://s3.internal.grigri.cloud \
  --profile velero \
  s3 rm --recursive s3://velero/backups/<backup-name>/
```

## Key Takeaway

Restarting the Velero pod is the safest first recovery boundary after a k3s crash interrupts
Velero/OpenEBS operations, but residual CR state, S3 backup data, and ZFS snapshots must all be
verified and cleaned up — otherwise the backup-sync controller recreates stuck CRs in a loop.

## References

- General Velero recovery runbook: `docs/troubleshooting/velero-backup-failures.md`
- Velero schedules: `platform/velero/templates/schedule-retain-weekly.yaml`,
  `schedule-retain-quaterly.yaml`
- Unattended-upgrades maintenance window: `metal/roles/prepare/files/apt-daily-upgrade-override.conf`
