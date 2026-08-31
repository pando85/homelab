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
- Backup's `kanidmRef.uid` matches target Kanidm's UID

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
7. **Resuming**: Scales all replicas back to original counts
8. **Completed**: Restore finished, Kanidm replication handles replica sync

### Critical Constraints

- **UID binding**: `targetRef.uid` must match Kanidm's `metadata.uid`
- **Image pinning**: `restoreImage` must exactly match `target.spec.image` (no `latest`)
- **PVC required**: Restore requires `volumeClaimTemplate` storage (not `emptyDir`)
- **Safety backup mandatory**: Cannot skip without break-glass annotations
- **Fail-closed after mutation**: After `RestoringPrimary` phase, failures leave target offline
- **One restore at a time**: Only one active restore per Kanidm target

### Common Issues

**Issue: Restore job fails with "Permission denied"**
- **Cause**: Restore job doesn't run as UID 389 (Kanidm user)
- **Status**: Known bug - https://github.com/pando85/kaniop/issues/1005
- **Workaround**: None currently - requires operator fix

**Issue: Backup CR not Ready**
- **Cause**: Backup validation failed or discovery incomplete
- **Fix**: Check backup status conditions, wait for discovery to complete

**Issue: Restore stuck in Quiescing**
- **Cause**: Pods not terminating or PVCs not detaching
- **Fix**: Check pod termination logs, manually delete stuck pods if needed

### Testing Restore (Ephemeral)

For testing restore in a different namespace:

1. Create Kanidm instance in test namespace
2. Create KanidmBackupRepository pointing to same S3 bucket with different prefix
3. Create KanidmBackupSchedule
4. Wait for backup to be created and discovered
5. Create KanidmRestore with `safetyBackup.skip: true` and break-glass annotations
6. Verify restore completes
7. Cleanup all test resources

### References

- Kaniop restore documentation: https://github.com/pando85/kaniop
- Bug #1005: Restore job permission issue
- Backup system architecture: See `docs/troubleshooting/kaniop-backup-system.md`
