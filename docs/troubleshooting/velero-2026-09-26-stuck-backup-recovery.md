# Velero Stuck Backup Recovery — 2026-09-26

## Summary

Velero had one backup stuck `InProgress` and another stuck `Queued`. The active backup had counted
its resources but had not backed up any items. Several OpenEBS `ZFSBackup` resources from current
and older failed backups were also stuck in `Init` with ephemeral port 9011 destinations belonging
to Velero pods that no longer existed.

Restarting Velero allowed the queued backup to start, but its first ZFS snapshot remained in `Init`.
Restarting the OpenEBS ZFS node agent on `prusik` did not recover that operation. Deleting the stale
`Init` resources and restarting the agent again allowed the backup to continue. It finished
`PartiallyFailed` with 248 of 248 items processed and 12 of 13 volume snapshots completed. The one
failed snapshot was caused by deleting its stuck `ZFSBackup` resource while Velero was still waiting
for it; it was not a new OpenEBS failure.

The incremental `retain-quaterly` chain was then audited. Four `Done` `ZFSBackup` resources from the
failed 2026-09-08 backup were orphaned from usable Velero backup metadata. They and the failed or
partially failed quarterly backups were removed. No recovery backup named
`retain-quaterly-recovery-20260926` was created.

## Timeline

All times are UTC on 2026-09-26.

| Time | Event |
|------|-------|
| ~07:30 | `retain-quaterly-20260922023004` was stuck `InProgress`; `retain-weekly-20260924142606` was `Queued`. |
| ~07:33 | The stuck quarterly backup and Velero pod were deleted. |
| 07:34 | The queued weekly backup entered `InProgress`; its first `ZFSBackup` remained `Init`. |
| ~07:43 | The OpenEBS ZFS node pod on `prusik` was restarted, but the current resource remained `Init`. |
| ~07:50 | Eight stale `Init` `ZFSBackup` resources were deleted and the ZFS node pod was restarted again. |
| 07:54–07:57 | The weekly backup resumed and progressed from 9 to 248 processed items. |
| 07:57 | The weekly backup finished `PartiallyFailed`: 248/248 items and 12/13 snapshots. |
| Before 08:17 | Four orphaned `Done` resources from failed quarterly backup `retain-quaterly-20260908023057` were deleted. Quarterly backups from 2026-09-01, 2026-09-08, and 2026-09-21 were submitted for deletion. |
| 08:17–08:19 | Catch-up backup `retain-quaterly-20260926081711` reached the Gitea data volume. The receiver closed its object at 08:19:42 without completing the backup operation. |
| 09:06 | Restarting the ZFS node agent retried the existing snapshot; the new send also became stuck. |
| 22:39 | ArgoCD rolled ZFS LocalPV back from 2.11.1 to 2.10.1. The fresh node agent automatically retried the Hermes full send and showed sustained transfer progress. |

## Root Cause

Two separate failures compounded each other:

1. **Queue and controller-state blockage:** the 2026-09-25 control-plane outage left Velero with an
   `InProgress` backup that blocked its global queue, while the OpenEBS ZFS node agent retained
   `Init` resources whose `backupDest` fields pointed at ephemeral Velero pod IPs on port 9011.
   Once a receiver disappears, those operations cannot resume. Old `Init` resources also cause
   repeated reconciliation and obscure current work; restarting only one controller was
   insufficient.
2. **Stream-process deadlock:** after the queue was cleared, large full sends exposed the v2.11.x
   `runPipe` behavior. When `nc` exited early, `zfs send` remained blocked and the current
   `ZFSBackup` stayed `Init`. The exact reason for the original receiver or `nc` exit remains
   unconfirmed and could be outside ZFS LocalPV.

These observations do not mean every failed September backup was caused by v2.11.x. The last
successful quarterly backup ran under v2.11.0 on August 25, but it was incremental. The confirmed
pipeline deadlock occurred under v2.11.1 on September 26 and involved full sends for volumes without
usable predecessor metadata. The cluster was subsequently pinned to v2.10.1 as containment because
its older shell pipeline does not have the same indefinite wait behavior.

## Recovery

The recovery sequence was:

1. Delete the stuck Velero backup and restart the Velero pod.
2. Confirm the queued backup transitions to `InProgress`.
3. Restart the OpenEBS ZFS node agent on the volume owner node.
4. If the current resource remains `Init`, delete all abandoned `Init` `ZFSBackup` resources whose
   parent backups are terminal or being abandoned.
5. Restart the ZFS node agent again and monitor both Velero item progress and `ZFSBackup` status.
6. Remove unusable failed backup records after auditing incremental dependencies.

Do not delete a current `ZFSBackup` while its parent Velero backup is still active unless the parent
is intentionally being abandoned. Velero will report that volume snapshot as failed if the resource
disappears while the plugin is polling it. That caused the 12/13 result in this incident.

This sequence applies to the stale-CR/queue blockage. It is **not** safe for a live send that has
deadlocked (see the next section): restarting the node agent makes it retry the same snapshot and
can hang again, and deleting the current CR must be sequenced against the sender process.

## Stream Pipeline Deadlock on the Gitea Volume

After cleanup, the `retain-quaterly` schedule fired a catch-up run,
`retain-quaterly-20260926081711`. Seven of its eight `ZFSBackup` resources reached `Done`, but
`restored-3eeb806f-98c4-4ff4-a2b7-ef6f676077a1.retain-quaterly-20260926081711` (the Gitea data
volume, `git/data-gitea-zfs-0`) stayed `Init` for over an hour even though its data object had
appeared in the bucket.

Restarting the `prusik` agent produced this log:

```log
zfs_util.go:776] snapshot already there datasets/openebs/restored-3eeb806f-...@retain-quaterly-20260926081711
```

This message is **not** a failure: OpenEBS reuses the snapshot and starts `zfs send` again. On the
owner node, the new send remained alive for over seven hours with a defunct `nc` child, and
`zfs destroy` returned `dataset is busy`. ZFS reported no user holds or dependent clones. Velero
logged `Client{15} operation completed` at 08:19:42 UTC, but never logged overall upload
completion. The S3 object was about **1.1 GiB**, while `zfs send -nP` estimated a full stream of
**31,893,359,112 bytes**. The object is incomplete, not a usable backup of Gitea data.

The installed ZFS driver waits for the sender before the `nc` process that consumes its output. If
`nc` exits early, the send's pipe can fill and block forever, leaving the `ZFSBackup` in `Init`.
The driver ignores sender errors when returning pipeline status; terminating the sender by itself
could falsely mark the truncated stream `Done`. The plugin polls the CR without its own timeout,
so the configured 32-hour Velero item-operation timeout is not a reliable cleanup boundary.
The exact reason `nc` exited early remains unconfirmed. The rollback result shows that 2.10.1
avoids the observed indefinite-wait failure mode, but does not prove whether the initiating event
was in the receiver, S3 path, network, control plane, or driver.

Historical Prometheus queries returned no samples for the relevant August–September range. The
version and failure timeline was reconstructed from retained Velero resources, ArgoCD and Git
history, ZFS process state, logs, and S3 object inspection.

**Correction to the earlier recovery hypothesis:** do not destroy the snapshot first or kill its
`zfs send` first. To abandon an active volume snapshot, remove its `ZFSBackup` CR so Velero records
the failure, confirm Velero advances, then stop the specific lingering send and remove only the
orphaned snapshot after checking references. See
`docs/troubleshooting/velero-zfs-stream-pipeline-deadlock.md` for the diagnostic and recovery
procedure.

Note that the agent log buffer rotates quickly because unrelated reconciliation errors are
retried continuously (see below), so `backup.go` lines for an in-flight operation may already be
gone. Query the fresh pod's log from the start rather than using `--since`.

## Backups Stuck in `Deleting`

Fifteen expired quarterly backups had been sitting in `Deleting` for months. Their
`DeleteBackupRequest` resources were `Processed` **with errors**:

```log
error deleting snapshot <vol>.<backup>: rpc error: code = Unknown desc =
  zfsbackups.zfs.openebs.io "<vol>.<backup>" not found
```

The OpenEBS plugin deletes snapshot data by deleting the `ZFSBackup` resource. Those resources had
long since been garbage collected, so every volume returned `not found`. Velero treats that as a
failure, records the errors, and aborts **before** removing bucket data or the Backup object. The
request is already `Processed`, so it is never retried and the Backup stays `Deleting` forever —
with no `deletionTimestamp` and no finalizer.

Two consequences:

1. The Backup object can only be removed by deleting it directly.
2. Bucket data for those backups was never removed, so it leaks. This audit found 172 bucket
   prefixes against only 13 Backup objects; most prefixes were empty directory markers left behind
   by `aws s3 rm`, plus a few orphaned `.zfsvol` stubs.

Cleanup order matters. Remove the bucket prefix first, then the Backup object; otherwise
`backup-sync` can recreate the object from bucket metadata (expired backups are ignored by sync, so
old prefixes stay dormant rather than looping).

```bash
aws --endpoint-url=https://s3.internal.grigri.cloud --profile velero \
  s3 rm --recursive s3://velero/backups/<backup-name>/
kubectl --context=grigri -n velero delete backup <backup-name>
kubectl --context=grigri -n velero delete deletebackuprequest <request-name>
```

Credentials come from the `cloud` key of the `velero-bucket-credentials` secret, which is an AWS
credentials file; point `AWS_SHARED_CREDENTIALS_FILE` at it rather than printing it.

Volume data lives inside `backups/<backup-name>/zfs-*` keys, not under a separate `zfs/` prefix.
The `prefix: zfs` VSL setting does not create a top-level prefix. Bucket versioning is suspended, so
`aws s3 rm` really removes data — verify a retained full baseline exists before pruning old
prefixes. Here the oldest retained quarterly resource, `20260630023034`, had an empty `prevSnapName`
(a full send at 786 GiB), so nothing retained depended on the pruned prefixes.

## Unrelated Agent Noise

The `prusik` agent also retries forever on:

- `config-qbittorrent@snapshot-3d947adf-...` and `@snapshot-01bea748-...`: `cannot destroy ...
  snapshot has dependent clones`
- datasets `pvc-a414ee13-...` and `pvc-a29270d8-...`: `use '-R' to destroy the following datasets`

These retries do not explain the Gitea stream deadlock, but they generate ~20 error lines per
minute, rotate the agent log, and consume reconciler capacity. Investigate whether the dependent
clones are in use before cleaning up these datasets and `ZFSSnapshot` resources. Do not use
`zfs destroy -R` without confirming the dependent datasets can be removed.

## Quarterly Incremental Chain Verification

`retain-quaterly` runs every Tuesday and uses `zfspv-incr`. Its `incrBackupCount: "11"` setting
creates one full backup followed by up to eleven incrementals. The schedule name describes the
approximately quarterly full cycle, not its execution frequency.

The audit found:

- Backup storage location `default` was `Available`.
- The last successful quarterly Velero backup was `retain-quaterly-20260825023011`.
- Every retained quarterly `ZFSBackup` resource was `Done` after cleanup.
- Every retained non-empty `prevSnapName` referenced an existing retained `ZFSBackup` resource.
- Four `Done` resources from failed backup `retain-quaterly-20260908023057` were removed because
  they were not part of a usable Velero backup.
- `retain-quaterly-20260901023008`, `retain-quaterly-20260908023057`, and
  `retain-quaterly-20260921065003` were submitted for deletion.

The next quarterly schedule will select the latest `Done` resource for each volume as its
incremental base. Volumes without a retained prior resource will be sent as full backups. Deleting
failed resources therefore restores consistent predecessor selection without requiring an immediate
manual full backup.

A clean CR graph does not prove end-to-end restorability. Restore-critical data should still be
periodically tested with an actual Velero restore.

## Verification

```bash
# No queued or active backups
kubectl --context=grigri get backups -n velero \
  | grep -E "InProgress|Queued|New"

# No incomplete OpenEBS backup operations
kubectl --context=grigri get zb -n zfs-localpv | grep -v Done

# Inspect quarterly predecessor references
kubectl --context=grigri get zb -n zfs-localpv \
  -l velero.io/schedule-name=retain-quaterly \
  -o custom-columns='NAME:.metadata.name,PREV:.spec.prevSnapName,STATUS:.status'

# Check the next quarterly run
kubectl --context=grigri get backups -n velero \
  -l velero.io/schedule-name=retain-quaterly \
  --sort-by=.metadata.creationTimestamp
```

## References

- General recovery runbook: `docs/troubleshooting/velero-backup-failures.md`
- Preceding outage: `docs/troubleshooting/velero-2026-09-25-oom-etcd-outage.md`
- Quarterly schedule: `platform/velero/templates/schedule-retain-quaterly.yaml`
- Snapshot locations: `platform/velero/values.yaml`
- ZFS stream deadlock diagnosis and safe recovery: `docs/troubleshooting/velero-zfs-stream-pipeline-deadlock.md`
