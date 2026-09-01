# Kaniop Backup System Gotchas

## Problem
Kaniop backup system has non-obvious behaviors that cause confusion during troubleshooting and cleanup.

## Root Cause
Multiple components interact in ways that aren't immediately clear from the CRD definitions.

## How to Diagnose

Check if discovery is working:
```bash
kubectl --context=grigri logs -n kaniop deployment/kaniop --since=10m | grep -i discovery
```

Check if retention is running:
```bash
kubectl --context=grigri logs -n kaniop deployment/kaniop --since=1h | grep -iE "(retention|delete)"
```

## Fix / Workaround

### Immutable Schedule Field
`KanidmBackupSchedule.spec.schedule` is immutable. To change schedule:
```bash
kubectl --context=grigri delete kanidmbackupschedule kanidm -n kanidm
kubectl --context=grigri create -f system/kanidm/templates/backup-schedule.yaml
```

### Namespace Requirement
`KanidmBackupSchedule` must have explicit `namespace: kanidm` in metadata. Without it, CR is created in `default` namespace and webhook validation fails.

### Discovery Truncation
Discovery controller scans S3 every 5 minutes and creates CRs for all manifests, but has a 1000 manifest limit. If S3 has more than 1000 backups, some won't get CRs.

### Retention Only Works on CRs
Retention controller applies policy to existing `KanidmBackup` CRs only. It does NOT delete orphaned S3 data (backups without CRs).

### Deleting a Backup CR Deletes S3 Data

`KanidmBackup` uses the `kanidmbackups.kaniop.rs/finalizer` finalizer. Normal CR deletion runs a data-mover deletion Job that removes the manifest and payload from S3.

Never delete a copied or discovered backup CR merely to clean Kubernetes state. To preserve S3 data, remove the finalizer before deleting the CR:

```bash
kubectl --context=grigri patch kanidmbackup <name> -n <namespace> \
  --type=merge -p '{"metadata":{"finalizers":null}}'
kubectl --context=grigri delete kanidmbackup <name> -n <namespace>
```

Verify the S3 manifest and payload before and after cleanup.

### Cleanup Orphaned S3 Backups
When S3 has orphaned backups that need removal:
1. Delete `KanidmBackupSchedule` (stops discovery)
2. Clean S3 with an S3 client after confirming the exact backup prefix
3. Remove matching stale CRs only after deciding whether their finalizers should run
4. Recreate `KanidmBackupSchedule`
