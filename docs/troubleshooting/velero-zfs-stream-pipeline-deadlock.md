# Velero/OpenEBS ZFS Stream Pipeline Deadlock

## Problem

A Velero backup stops advancing on one PV while its `ZFSBackup` stays `Init`. On the volume's
owner node, `zfs send` remains alive, its `nc` child has exited (possibly as a zombie), and the
receiver's TCP port 9011 no longer has an active connection. `zfs destroy` of the backup snapshot
returns `dataset is busy` because the sender is still using it. The backup can block subsequent
scheduled backups.

**An object in S3 is not proof of a complete stream.** The receiver closes its object writer when a
client disconnects, even if the expected ZFS stream has not arrived. A finished object and a
`Client{...} operation completed` log line do not mean that the `ZFSBackup` reached `Done` or that
the volume can be restored.

## Root Cause

The installed OpenEBS ZFS driver **v2.11.1** runs `zfs send ... | nc -w 3 <velero-pod-ip> 9011`.
Its `runPipe` implementation waits for `zfs send` **before** waiting for `nc`. If `nc` exits while
the send is still producing data, the copy into `nc` stops consuming the send's stdout. The pipe
fills, `zfs send` blocks, and the backup controller never updates `ZFSBackup.status` from `Init`.
The implementation also returns only `nc`'s exit error, ignoring the `zfs send` error: killing the
sender alone risks a false `Done` status for a truncated stream.

The installed OpenEBS Velero plugin **v3.6.0** polls `ZFSBackup.status` without a timeout in its
`checkBackupStatus` loop. The backup remains blocked even with a 32-hour Velero
`itemOperationTimeout` configured; earlier backups ran longer than 32 hours. On a node-agent
restart, `snapshot already there` is informational: `CreateSnapshot` returns success and the agent
**tries to send again**. Restarting it alone can reproduce the deadlock.

What caused `nc` to exit early on a given run still requires investigation. Possibilities include
receiver termination, an idle timeout under object-store backpressure, or transport failure. Do
not infer an S3 or network outage solely from a zombie `nc` process. Control-plane outages are a
separate, observed source of `PartiallyFailed` backups.

## How to Diagnose

```bash
kubectl --context=grigri -n velero get backups
kubectl --context=grigri -n zfs-localpv get zb \
  -o custom-columns='NAME:.metadata.name,STATUS:.status,NODE:.spec.ownerNodeID,DEST:.spec.backupDest'

# On the owner node, match the specific snapshot and note the sender, nc and their parent PIDs.
ssh <node> 'ps -eo pid,ppid,stat,etime,args' | grep -E 'zfs send|\[nc\]|zfs-driver'
ssh <node> 'ss -tn state established' | grep 9011

# Read-only estimate; compare with the object size, allowing for stream compression/options.
ssh <node> 'zfs send -nP datasets/openebs/<volume>@<backup-name>'
ssh <node> 'zfs holds datasets/openebs/<volume>@<backup-name>'
ssh <node> 'zfs get -H -o property,value clones,userrefs datasets/openebs/<volume>@<backup-name>'
```

Inspect the Velero backup logs and the **historical** node-agent logs in Loki if pod logs have
rotated. Look for a Velero `Uploading snapshot to` line without a matching `successfully uploaded
object`, a `Client{...} operation completed` line, and an agent `Got add event for Bkp` without a
`backup ... done` line. Inspect `zfs holds` and `zfs get clones,userrefs` before attributing a busy
snapshot to a send process; a held or cloned snapshot requires a different fix.

The 2026-09-26 Gitea incident is an example: the full send for
`restored-3eeb806f-98c4-4ff4-a2b7-ef6f676077a1@retain-quaterly-20260926081711` was estimated
at **31,893,359,112 bytes**, but the object observed in S3 was only about **1.1 GiB**. Velero's
receiver logged a client completion at 08:19:42 UTC without logging overall upload completion.
After an agent restart, a new `zfs send` remained blocked for over seven hours with a defunct
`nc` process. The matching `ZFSBackup` remained `Init` and Velero stayed at 21/234 items. The
partial object must **not** be treated as a usable Gitea backup.

## Fix / Workaround

**Do not kill the sender first.** Because the driver ignores the sender's exit code, that could
mark a truncated backup `Done`. Do not destroy the snapshot while the send is running, and never
use `zfs destroy -R` on an in-use volume.

For an active operation that cannot be recovered, perform these steps manually, one at a time:

1. Record the exact `ZFSBackup` name, owner node, snapshot, sender PID, and parent Velero backup.
   Delete **the stuck `ZFSBackup` CR first** so the Velero plugin cannot mistake a terminated send
   for success. This intentionally fails that volume's snapshot; the backup may finish
   `PartiallyFailed`. Do not delete a working backup's other `Done` CRs.
2. Confirm Velero logged `zfsbackups.zfs.openebs.io "<name>" not found` for that volume and advanced
   past the item. If abandoning the entire Velero backup instead, confirm it is no longer active
   before further cleanup.
3. Re-check the sender's PID **and full command line**; only then stop the specific lingering
   `zfs send` with `TERM`. Confirm it and its `nc` child are gone. Avoid a broad `pkill` that might
   interrupt other volumes. A node-agent restart alone does not resolve the stream failure.
4. Check that no `Done` `ZFSBackup` references the snapshot as `snapName` or `prevSnapName`, and that
   ZFS reports no holds or dependent clones. Then destroy **only** the orphaned snapshot on its
   owner node. Treat its S3 object as incomplete; clean it up only after checking backup metadata
   and incremental dependencies.
5. Run a new backup with a new name and verify **every** expected `ZFSBackup` is `Done`, that Velero
   reports all snapshots completed, and that a representative restore works. A completed Backup
   resource alone is not proof that all volumes are restorable.

The operator can use these targeted commands after confirming each preceding condition. Replace
placeholders with names and a PID verified immediately before use; never apply them to a live
transfer that is still making progress:

```bash
# Step 1: intentionally fail only this volume's backup operation.
kubectl --context=grigri -n zfs-localpv delete zb <volume>.<backup-name>

# Step 2: verify Velero recorded the failed volume before continuing.
kubectl --context=grigri -n velero get backup <backup-name> -o yaml

# Step 3: after verifying the PID and full command line, stop only this sender.
ssh <node> 'ps -p <pid> -o pid,ppid,stat,etime,args'
ssh <node> 'sudo kill -TERM <pid>'

# Step 4: after the process exits and the snapshot is confirmed orphaned.
ssh <node> 'sudo zfs destroy datasets/openebs/<volume>@<backup-name>'
```

For prevention, use a fixed upstream release or patch the driver to cancel/reap **both** children
when either side exits or the receiver disconnects, propagate both exit statuses, and bound the
operation with a timeout. The receiver should reject/clean up truncated streams rather than treat
socket closure as success. Alert on `Init` resources and non-advancing backups; investigate node
reboots, API outages and receiver/object-store stalls independently.

## References

- Incident and incremental-chain audit: `docs/troubleshooting/velero-2026-09-26-stuck-backup-recovery.md`
- General Velero runbook: `docs/troubleshooting/velero-backup-failures.md`
- [ZFS driver v2.11.1 `runPipe` and `CreateBackup`](https://github.com/openebs/zfs-localpv/blob/v2.11.1/pkg/zfs/zfs_util.go)
- [ZFS backup controller v2.11.1](https://github.com/openebs/zfs-localpv/blob/v2.11.1/pkg/mgmt/backup/backup.go)
- [Velero plugin v3.6.0 backup polling](https://github.com/openebs/velero-plugin/blob/v3.6.0/pkg/zfs/plugin/backup.go)
- [Velero plugin v3.6.0 receiver and client handling](https://github.com/openebs/velero-plugin/blob/v3.6.0/pkg/clouduploader/server_utils.go)
