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

1. **Delete failed Velero backup:**
   ```bash
   kubectl --context=grigri delete backup <backup-name> -n velero
   ```

2. **Delete stuck ZFSBackup CRs:**
   ```bash
   kubectl --context=grigri delete zb -n zfs-localpv \
     <pvc-uuid>.<backup-name> \
     <pvc-uuid>.<backup-name>
   ```

3. **Remove backup data from bucket (if sync controller loops):**
   ```bash
   AWS_ACCESS_KEY_ID=velero AWS_SECRET_ACCESS_KEY=<secret> \
     aws --endpoint-url=https://s3.internal.grigri.cloud \
     s3 rm --recursive s3://velero/backups/<backup-name>/
   ```

4. **Restart velero pod to clear sync controller state:**
   ```bash
   kubectl --context=grigri delete pod -n velero -l app.kubernetes.io/name=velero
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
