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

### Cleanup Orphaned S3 Backups
When S3 has orphaned backups that need removal:
1. Delete `KanidmBackupSchedule` (stops discovery)
2. Delete all `KanidmBackup` CRs
3. Clean S3 with `mc rm --recursive --force --versions` (requires mc client)
4. Recreate `KanidmBackupSchedule`
