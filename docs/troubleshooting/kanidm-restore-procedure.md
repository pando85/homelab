# Kanidm Backup Restore Procedure

## Problem
Need to restore Kanidm from a backup for disaster recovery or testing.

## Root Cause
N/A - this is a runbook for the restore process.

## How to Diagnose

Check if backups are available:
```bash
kubectl --context=grigri get kanidmbackups -n kanidm
```

Check backup status (must be `Ready`):
```bash
kubectl --context=grigri get kanidmbackup <backup-name> -n kanidm -o yaml
```

## Fix / Workaround

### Restore Process

**Prerequisites:**
- Kanidm instance exists and is running
- At least one `KanidmBackup` CR with status `Ready`
- Target `spec.domain` matches the domain stored in the backup
- Target `spec.image` matches the backup's Kanidm version
- Target has exactly one replica group with `primaryNode: true`
- Target uses PVC storage and security context UID/GID/fsGroup 389

There are two restore modes:

- `backupRef` only supports restoring to the original Kanidm CR because Kaniop 0.16.2 requires the backup's `kanidmRef.uid` to match the target UID.
- `local` supports clean-cluster disaster recovery after the verified S3 payload is staged on the target primary PVC.

**1. Get target Kanidm UID:**
```bash
KANIDM_UID=$(kubectl --context=grigri get kanidm kanidm -n kanidm -o jsonpath='{.metadata.uid}')
```

**2. Get available backups:**
```bash
kubectl --context=grigri get kanidmbackups -n kanidm
```

**3. Create KanidmRestore CR:**
```yaml
apiVersion: kaniop.rs/v1beta1
kind: KanidmRestore
metadata:
  name: kanidm-restore
  namespace: kanidm
  annotations:
    backup.kaniop.rs/break-glass-reason: "Disaster recovery restore"
    backup.kaniop.rs/break-glass-approved-by: "<your-name>"
spec:
  targetRef:
    name: kanidm
    uid: <KANIDM_UID>  # Must match metadata.uid of target Kanidm
  source:
    backupRef:
      name: <backup-name>  # e.g., kb-0ee13386
  restoreImage: kanidm/server:1.11.1  # Must match target's spec.image exactly
  safetyBackup:
    repositoryRef:
      name: kanidm  # Repository for safety backup
    skip: false  # Set to true only for testing with break-glass annotations
```

**4. Monitor restore progress:**
```bash
kubectl --context=grigri get kanidmrestore kanidm-restore -n kanidm -w
```

Restore phases:
- `Pending` → `Validating` → `Quiescing` → `SafetyBackup` → `PreparingSource` → `RestoringPrimary` → `Verifying` → `RebuildingReplicas` → `Resuming` → `Completed`

**5. Check restore status:**
```bash
kubectl --context=grigri get kanidmrestore kanidm-restore -n kanidm -o yaml
```

**6. Cleanup after successful restore:**
```bash
kubectl --context=grigri delete kanidmrestore kanidm-restore -n kanidm
```

### Restore Flow Details

1. **Quiescing**: Scales all StatefulSets to 0, waits for pods to stop
2. **SafetyBackup**: Creates offline backup of current DB (can be skipped with break-glass)
3. **PreparingSource**: Downloads backup from S3 to PVC
4. **RestoringPrimary**: Runs `kanidmd database restore` (mutation boundary - failures after this leave target offline)
5. **Verifying**: Validates restored database
6. **RebuildingReplicas**: Deletes secondary PVCs, scales primary to 1, regenerates certs
7. **Resuming**: Clears restore maintenance state; normal Kanidm reconciliation restores desired replica counts
8. **Completed**: Primary restore finished; wait separately for all desired replicas and replication health

### Critical Constraints

- **UID binding**: `targetRef.uid` must match Kanidm's `metadata.uid`
- **Remote restore binding**: `backupRef` also requires the backup's `kanidmRef.uid` to match the target UID; Kaniop 0.16.2 cannot use it for a fresh Kanidm CR
- **Domain binding**: Target `spec.domain` must match the database domain stored in the backup, or Kanidm will refuse to start after restore
- **Image pinning**: `restoreImage` must exactly match `target.spec.image` and the backup's Kanidm version (no `latest`)
- **Primary designation**: Exactly one replica group must set `primaryNode: true`
- **PVC required**: Restore requires `volumeClaimTemplate` storage (not `emptyDir`)
- **Security context**: Set `runAsUser`, `runAsGroup`, and `fsGroup` to `389`; otherwise source and database jobs can fail with permission errors
- **Safety backup mandatory**: Cannot skip without break-glass annotations
- **Fail-closed after mutation**: After `RestoringPrimary` phase, failures leave target offline
- **One restore at a time**: Only one active restore per Kanidm target
- **Completed is early**: Kaniop can report `Completed` before all desired replicas are Ready; verify the Kanidm CR reports the expected replica count
- **Retained storage**: `openebs-zfspv` uses `reclaimPolicy: Retain`; secondary PVC deletion during restore leaves released PVs and ZFS datasets that require a separate storage audit

### Clean-Cluster Disaster Recovery

Kaniop 0.16.2 does not support cross-UID disaster recovery through `source.backupRef`. Use `source.local`:

1. Verify the S3 manifest and payload exist before creating or changing any CR.
2. Confirm the payload SHA-256 matches `manifest.json`.
3. Create the target with the backup's domain and Kanidm image version.
4. Stage the payload as a safe basename such as `/data/dr-backup.json.gz` on the primary PVC.
5. Set ownership to `389:389`; the `/data` directory and existing database files must also be writable by UID 389.
6. Delete the staging Job immediately after it completes. Completed pods still reference PVCs and can prevent Kaniop from deleting a secondary PVC during `RebuildingReplicas`.
7. Create the restore with `source.local.fileName: dr-backup.json.gz`.

Do not create an Ingress for an ephemeral same-domain target. The domain config does not make outbound requests to production; use the namespace-local Service for validation.

```yaml
apiVersion: kaniop.rs/v1beta1
kind: KanidmRestore
metadata:
  name: kanidm-dr-test
  namespace: default
  annotations:
    backup.kaniop.rs/break-glass-reason: "Clean-cluster DR test"
    backup.kaniop.rs/break-glass-approved-by: "<your-name>"
spec:
  targetRef:
    name: kanidm
    uid: <NEW_TARGET_UID>
  source:
    local:
      fileName: dr-backup.json.gz
  restoreImage: kanidm/server:1.11.1
  safetyBackup:
    skip: true
```

After completion, verify both content and replication:

```bash
kubectl --context=grigri get kanidm kanidm -n default
kubectl --context=grigri exec -n default kanidm-default-0 -- kanidmd db-scan list-id2entry
kubectl --context=grigri logs -n default kanidm-default-0 --since=5m | grep "Incremental Replication Success"
kubectl --context=grigri logs -n default kanidm-default-1 --since=5m | grep "Incremental Replication Success"
```

### Common Issues

**Issue: Restore job fails with "Permission denied"**
- **Cause**: Target lacks security context UID/GID/fsGroup 389, or files on the primary PVC are owned by another UID
- **Status**: Kaniop issue #1005 was fixed in 0.16.2 by inheriting the target security context
- **Fix**: Configure security context 389 before restore and ensure `/data` is writable by UID 389

**Issue: Restore is stuck in `RebuildingReplicas` with a secondary PVC terminating**
- **Cause**: A completed staging Job pod still references the secondary PVC, so `kubernetes.io/pvc-protection` prevents deletion
- **Fix**: Delete the completed Job that mounts the terminating PVC; do not remove the PVC protection finalizer

**Issue: Restored primary crashes with a domain mismatch**
- **Cause**: The target `spec.domain` differs from the domain stored in the backup
- **Fix**: For a clean retry, create the target with the backup's domain. Otherwise perform an offline Kanidm domain rename before starting the server

**Issue: Backup CR not Ready**
- **Cause**: Backup validation failed or discovery incomplete
- **Fix**: Check backup status conditions, wait for discovery to complete

**Issue: Restore stuck in Quiescing**
- **Cause**: Pods not terminating or PVCs not detaching
- **Fix**: Check pod termination logs, manually delete stuck pods if needed

### Testing Restore (Ephemeral)

For testing a production backup in a different namespace:

1. Verify the manifest and payload in S3, including the payload SHA-256.
2. Create a PVC-backed Kanidm target with the same domain and image version as the backup, exactly one primary replica group, and security context 389.
3. Use a self-signed test TLS certificate for that domain and do not create an Ingress.
4. Stage only the verified payload on the primary PVC and delete the staging Job immediately.
5. Restore through `source.local`; `backupRef` cannot cross target UIDs in Kaniop 0.16.2.
6. Wait for both the restore to report `Completed` and the Kanidm CR to report all desired replicas Ready.
7. Validate representative people, groups, and OAuth2 clients with `kanidmd db-scan` and confirm incremental replication succeeds on both pods.
8. Before deleting a copied `KanidmBackup` CR, remove `kanidmbackups.kaniop.rs/finalizer`; normal CR deletion removes the referenced S3 data.
9. Audit released PVs created by secondary PVC replacement and follow the cluster hygiene procedure to reclaim their ZFS datasets.

### References

- Kaniop restore documentation: https://github.com/pando85/kaniop
- Bug #1005: Restore job permission issue
- Backup system architecture: See `docs/troubleshooting/kaniop-backup-system.md`
