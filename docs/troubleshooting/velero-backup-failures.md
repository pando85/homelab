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

2. **OpenEBS ZFS stream pipeline deadlock**: The node agent runs `zfs send | nc` to an ephemeral
   Velero receiver on port 9011. If `nc` exits early, `zfs send` can block on its pipe indefinitely,
   leaving `ZFSBackup` in `Init` and blocking Velero. A restarted agent can retry an already-created
   snapshot and hang again. See `docs/troubleshooting/velero-zfs-stream-pipeline-deadlock.md`.

3. **Stale zfs send processes**: Failed backups can leave `zfs send` processes running, holding snapshots open and preventing cleanup. These processes appear as:
   ```
   /sbin/zfs send datasets/openebs/<pvc>@<backup-name>
   ```

4. **Backup-sync controller loop**: When backup data exists in the bucket but the Backup CR is deleted, the sync controller recreates it every 2 minutes, creating a loop of stuck ZFSBackup CRs.

5. **Memory pressure crashes k3s during backup**: Heavy workloads (e.g., CI builds) can drive MemAvailable low enough to saturate NVMe with reclaim I/O, causing embedded etcd to time out on ReadIndex/transactions. k3s loses leader election and exits (code 1). systemd restarts it, but active Velero/OpenEBS operations are left mid-flight — Backup CR stuck `InProgress`, ZFSBackup CRs stuck `Init`. Pod restart is the safest first recovery boundary, but residual CR/S3/ZFS state must be verified. See `docs/troubleshooting/velero-2026-09-25-oom-etcd-outage.md` for the full incident timeline and recovery procedure.

## How to Diagnose

**Check for failed or queue-blocking backups:**
```bash
kubectl --context=grigri get backups -n velero | grep -E "InProgress|Queued|New|PartiallyFailed|Failed"
```

**Check for stuck ZFSBackup CRs:**
```bash
kubectl --context=grigri get zb -n zfs-localpv | grep -v Done
```

**Check for stale zfs send processes:**
```bash
ssh <node> 'ps -eo pid,ppid,stat,etime,args' | grep -E 'zfs send|\[nc\]|zfs-driver'
```

If `nc` is defunct while the matching `zfs send` is still alive, inspect the `ZFSBackup`,
receiver connection (`ss -tn state established` on the owner node) and Velero logs. An object
already present in S3 may be **truncated**; compare its size with `zfs send -nP` and verify the
resource reached `Done`. Do not treat an S3 object or a receiver `Client ... operation completed`
line as proof of a complete stream.

**Check for orphaned ZFS snapshots:**
```bash
ssh <node> 'zfs list -H -t snapshot -o name,used' | grep -E 'retain-weekly|retain-quaterly'
kubectl --context=grigri -n zfs-localpv get zb \
  -o custom-columns='NAME:.metadata.name,VOLUME:.spec.volumeName,SNAPSHOT:.spec.snapName,PREV:.spec.prevSnapName,STATUS:.status'
```

Compare volume **and** snapshot names before declaring a snapshot orphaned; an incremental may
still reference an older snapshot via `prevSnapName`.

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

1. **Classify the blockage first.** If a `ZFSBackup` is `Init`, check the owner node for a live
   `zfs send` and whether its `nc` peer is still connected. For a deadlocked active send, follow
   `docs/troubleshooting/velero-zfs-stream-pipeline-deadlock.md`: delete the **specific** stuck CR,
   verify Velero records the volume failure, then stop its sender. Do not restart the node agent
   or Velero as a first step while a stream is active. Old `Init` CRs may point at a gone pod IP.
2. **For backups intentionally abandoned**, remove failed backup resources only after checking
   retained full/incremental dependencies. Velero may leave deletion requests `Processed` with
   errors if the OpenEBS plugin tries to delete an already-gone `ZFSBackup`; see
   `docs/troubleshooting/velero-2026-09-26-stuck-backup-recovery.md` for the S3-prefix cleanup order.
   Retrieve credentials from `pass` or Vault without exposing them on the command line. A
   remaining bucket prefix can cause backup-sync to recreate a deleted Backup CR.
3. **If the queue is still blocked after cleaning up the failed operation**, restart the Velero
   pod only after checking for other active sends. Verify that the next backup advances and its
   volume snapshots all reach `Done`.

### Cleanup: Stuck ZFS Snapshots

First ensure Velero has stopped waiting for the affected `ZFSBackup` and cannot report the
incomplete send as successful. Then confirm the exact sender PID and command line and stop that
process with `TERM`; avoid a broad `pkill` or `-9`. Check `zfs holds` and `zfs get clones,userrefs`
before destroying **only** the orphaned snapshot on the owner node. Follow
`docs/troubleshooting/velero-zfs-stream-pipeline-deadlock.md` for the full sequence.

### Recovery: Wedged ZFS Node Agent

Only if the node agent is **not processing** backup CRs and no active send is in progress, restart
the specific node pod after identifying its owner node:

```bash
kubectl --context=grigri -n zfs-localpv get pods -o wide
# Operator action, using the pod name on the affected node:
kubectl --context=grigri -n zfs-localpv delete pod <node-agent-pod-name>
```

The DaemonSet will recreate the pod. Restarting it while a stream is hung can immediately retry
the same snapshot and reproduce the deadlock.

## References

- Velero schedules: `platform/velero/templates/schedule-retain-weekly.yaml`, `schedule-retain-quaterly.yaml`
- Unattended-upgrades config: `metal/roles/prepare/files/50unattended-upgrades`
- Timer override: `metal/roles/prepare/files/apt-daily-upgrade-override.conf`
- Ansible role: `metal/roles/prepare/tasks/unattended-upgrades.yml`
- 2026-09-25 memory-pressure incident: `docs/troubleshooting/velero-2026-09-25-oom-etcd-outage.md`
- 2026-09-26 stuck-queue recovery and incremental-chain audit: `docs/troubleshooting/velero-2026-09-26-stuck-backup-recovery.md`
- OpenEBS ZFS sender/receiver deadlock and incomplete-stream risk: `docs/troubleshooting/velero-zfs-stream-pipeline-deadlock.md`
