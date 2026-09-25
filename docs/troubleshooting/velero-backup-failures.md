# Velero Backup Failures

## Problem

Velero backups failing with `PartiallyFailed` status, showing errors like:
- `apiserver not ready`
- `dial tcp 10.43.0.1:443: connect: connection refused`
- `runtime core not ready`

ZFSBackup CRs stuck in `Init` state with stale `backupDest` addresses. Orphaned ZFS snapshots consuming space.

## Root Cause

**Multiple failure modes:**

1. **Unattended-upgrades restarting k3s**: systemd package upgrades trigger daemon-reexec, causing k3s to restart during backup windows. This breaks the API server connection mid-backup.

2. **OpenEBS ZFS backup controller wedged**: The ZFS node agent's watch can become stale, leaving ZFSBackup CRs stuck in `Init` state. The ephemeral backup receiver (port 9011) on the Velero pod is temporary and disappears when backups fail, making stuck CRs unrecoverable.

3. **Stale zfs send processes**: Failed backups can leave `zfs send` processes running, holding snapshots open and preventing cleanup. These processes appear as:
   ```
   /sbin/zfs send datasets/openebs/<pvc>@<backup-name>
   ```

4. **Backup-sync controller loop**: When backup data exists in the bucket but the Backup CR is deleted, the sync controller recreates it every 2 minutes, creating a loop of stuck ZFSBackup CRs.

5. **Memory pressure crashes k3s during backup**: Heavy workloads (e.g., CI builds) can drive MemAvailable low enough to saturate NVMe with reclaim I/O, causing embedded etcd to time out on ReadIndex/transactions. k3s loses leader election and exits (code 1). systemd restarts it, but active Velero/OpenEBS operations are left mid-flight — Backup CR stuck `InProgress`, ZFSBackup CRs stuck `Init`. Pod restart is the safest first recovery boundary, but residual CR/S3/ZFS state must be verified. See `docs/troubleshooting/velero-2026-09-25-oom-etcd-outage.md` for the full incident timeline and recovery procedure.

## How to Diagnose

**Check for failed backups:**
```bash
kubectl --context=grigri get backups -n velero | grep -E "InProgress|PartiallyFailed|Failed"
```

**Check for stuck ZFSBackup CRs:**
```bash
kubectl --context=grigri get zb -n zfs-localpv | grep -v Done
```

**Check for stale zfs send processes:**
```bash
ssh <node> 'ps aux | grep "zfs send" | grep -v grep'
```

**Check for orphaned ZFS snapshots:**
```bash
ssh <node> 'zfs list -H -t snapshot -o name,used | grep -E "retain-weekly|retain-quaterly" | grep -v "$(kubectl get zb -n zfs-localpv -o jsonpath="{.items[*].spec.snapName}" | tr " " "\n" | sort -u)"'
```

**Check if unattended-upgrades caused k3s restart:**
```bash
journalctl -u k3s --since "1 hour ago" | grep -i "stopped\|started"
journalctl -u unattended-upgrades --since "1 hour ago" | grep -i "systemd"
```

**Check if k3s crashed from memory pressure / etcd timeout:**
```bash
journalctl -u k3s --since "1 hour ago" | grep -iE "leader election|lost leader|exit code"
```

**Memory pressure metrics (read-only):**
```promql
# Node memory available ratio (should be > 10%; < 5% is critical)
node_memory_MemAvailable_bytes{instance="prusik"} / node_memory_MemTotal_bytes{instance="prusik"}

# Memory PSI — stalled/waiting (seconds of stall per wall-clock second)
rate(node_pressure_memory_stalled_seconds_total{instance="prusik"}[5m])
rate(node_pressure_memory_waiting_seconds_total{instance="prusik"}[5m])
```

The embedded etcd disk and leader-change metrics are not currently scraped. Confirm ReadIndex
timeouts and leader-election loss in the k3s journal instead.

## Fix / Workaround

### Prevention: Maintenance Window

Configure unattended-upgrades to run in a broad window that doesn't collide with backups:

```ini
# /etc/systemd/system/apt-daily-upgrade.timer.d/override.conf
[Timer]
OnCalendar=
OnCalendar=Mon,Wed..Sun *-*-* 08:00:00
RandomizedDelaySec=10h
```

This runs upgrades Monday + Wednesday–Sunday, 08:00–18:00, avoiding:
- Velero weekly/quarterly backups (Tuesday 02:30)
- Velero remote syncs (Thursday/Friday 02:30)
- Kanidm backups (daily 03:00)

**Do not blacklist systemd packages** — this prevents security updates and is excessive for a first-time issue.

### Cleanup: Stuck Backups

1. **Restart Velero pod to clear sync controller state:**
   ```bash
   kubectl --context=grigri delete pod -n velero -l app.kubernetes.io/name=velero
   ```

2. **Delete failed Velero backup:**
   ```bash
   kubectl --context=grigri delete backup <backup-name> -n velero
   ```

3. **Delete stuck ZFSBackup CRs:**
   ```bash
   kubectl --context=grigri delete zb -n zfs-localpv \
     <pvc-uuid>.<backup-name> \
     <pvc-uuid>.<backup-name>
   ```

4. **Remove backup data from bucket (if sync controller loops):**
   Retrieve credentials from `pass` or Vault without exposing them on the command line:

   ```bash
   aws --endpoint-url=https://s3.internal.grigri.cloud \
     --profile velero \
     s3 rm --recursive s3://velero/backups/<backup-name>/
   ```

### Cleanup: Stuck ZFS Snapshots

1. **Kill stale zfs send processes:**
   ```bash
   ssh <node> 'sudo pkill -9 -f "zfs send.*<backup-name>"'
   ```

2. **Destroy orphaned snapshots:**
   ```bash
   ssh <node> 'sudo zfs destroy datasets/openebs/<pvc>@<backup-name>'
   ```

### Recovery: Wedged ZFS Node Agent

If ZFSBackup CRs are stuck in `Init` and the node agent isn't processing them:

```bash
kubectl --context=grigri delete pod -n zfs-localpv -l app=openebs-zfs-node
```

The DaemonSet will recreate the pod with a fresh watch.

## References

- Velero schedules: `platform/velero/templates/schedule-retain-weekly.yaml`, `schedule-retain-quaterly.yaml`
- Unattended-upgrades config: `metal/roles/prepare/files/50unattended-upgrades`
- Timer override: `metal/roles/prepare/files/apt-daily-upgrade-override.conf`
- Ansible role: `metal/roles/prepare/tasks/unattended-upgrades.yml`
- 2026-09-25 memory-pressure incident: `docs/troubleshooting/velero-2026-09-25-oom-etcd-outage.md`
