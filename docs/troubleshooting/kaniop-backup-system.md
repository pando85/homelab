# Kaniop Backup System Architecture and Operations

## Problem
The kaniop backup system has multiple components that interact in non-obvious ways. Understanding the architecture is critical for troubleshooting backup issues, performing cleanup operations, and configuring retention policies.

## Architecture Overview

### Backup Flow
1. **Kanidm creates local backups** on schedule (configured via `KanidmBackupSchedule.spec.schedule`)
2. **data-mover-transport sidecar** uploads backups to S3 and maintains state of known backups
3. **Discovery controller** (runs every 5 minutes) scans S3 and creates `KanidmBackup` CRs for manifests without CRs
4. **Backup controller** validates backups and updates CR status (Ready/Invalid/Discovering)
5. **Retention controller** applies retention policy and deletes CRs that don't match
6. **Deletion Jobs** handle S3 data cleanup when CRs are deleted

### Key Components
- **KanidmBackupSchedule**: Defines backup schedule and retention policy
- **KanidmBackupRepository**: S3 storage configuration
- **KanidmBackup**: Individual backup CRs with validation status
- **Discovery Job**: Periodic S3 scanning (every 5 min, controlled by kaniop operator)
- **Deletion Job**: S3 cleanup when CRs are deleted

## Critical Behaviors

### 1. Discovery Controller Behavior
- Runs every 5 minutes (hardcoded in operator)
- Creates CRs for ALL S3 manifests that don't have CRs
- Has a 1000 manifest limit per discovery run (truncation)
- If more than 1000 backups exist in S3, some won't get CRs

### 2. Retention Controller Behavior
- Applies retention policy to existing CRs only
- Does NOT delete S3 data directly - creates deletion Jobs
- Deletion Jobs run and remove S3 data, then remove finalizers from CRs
- Orphaned S3 backups (without CRs) are NOT cleaned up by retention

### 3. data-mover-transport State Tracking
- Maintains in-memory state of known backups
- Avoids re-uploading backups that were already uploaded
- State is lost on pod restart, causing re-scan of local backups
- Logs show "backup already known, skipping" for cached backups

### 4. Immutable Schedule Field
- `KanidmBackupSchedule.spec.schedule` is immutable after creation
- Changing schedule requires delete/recreate of the CR
- ArgoCD will show OutOfSync if schedule differs from git

### 5. Namespace Requirement
- `KanidmBackupSchedule` must have explicit `namespace: kanidm` in metadata
- Without namespace, CR is created in `default` namespace
- Webhook validation prevents multiple schedules targeting same Kanidm

## How to Diagnose

### Check backup uploads
```bash
# Check if backups are being uploaded to S3
kubectl --context=grigri logs -n kanidm kanidm-default-0 -c data-mover-transport --since=1h | grep -E "(upload|starting|success)"

# List backups in S3
kubectl --context=grigri exec -n minio mc-cleanup -- mc ls minio/kaniop/kanidm/v1/tenants/kanidm/clusters/be36942f-0685-4d42-a137-8faa891d3830/backups/
```

### Check discovery status
```bash
# Check KanidmBackupSchedule status
kubectl --context=grigri get kanidmbackupschedule kanidm -n kanidm -o yaml

# Check discovery controller logs
kubectl --context=grigri logs -n kaniop deployment/kaniop --since=10m | grep -i discovery

# Check KanidmBackup CRs
kubectl --context=grigri get kanidmbackups -n kanidm
```

### Check retention activity
```bash
# Check retention controller logs
kubectl --context=grigri logs -n kaniop deployment/kaniop --since=1h | grep -iE "(retention|delete)"

# Check deletion jobs
kubectl --context=grigri get jobs -n kanidm | grep -E "kb-|delet"
```

### Check local backups
```bash
# List local backup files (from node)
ls /var/lib/kubelet/pods/*/volumes/kubernetes.io~csi/pvc-*/mount/backups/
```

## Fix / Workaround

### Cleanup Orphaned S3 Backups
When S3 has orphaned backups (without CRs) that need to be removed:

1. **Delete KanidmBackupSchedule** (stops discovery controller):
   ```bash
   kubectl --context=grigri delete kanidmbackupschedule kanidm -n kanidm
   ```

2. **Delete all KanidmBackup CRs**:
   ```bash
   kubectl --context=grigri delete kanidmbackups --all -n kanidm
   ```

3. **Clean S3 bucket** (requires mc client):
   ```bash
   # Create mc pod
   kubectl --context=grigri run mc-cleanup --image=minio/mc:latest --restart=Never --command -n minio -- sleep 3600

   # Configure mc
   kubectl --context=grigri exec -n minio mc-cleanup -- mc alias set minio https://s3.internal.grigri.cloud kaniop '<password>'

   # Delete all backups (including versioned delete markers)
   kubectl --context=grigri exec -n minio mc-cleanup -- mc rm --recursive --force --versions minio/kaniop/kanidm/v1/tenants/kanidm/clusters/be36942f-0685-4d42-a137-8faa891d3830/backups/

   # Cleanup mc pod
   kubectl --context=grigri delete pod mc-cleanup -n minio
   ```

4. **Recreate KanidmBackupSchedule** with correct schedule:
   ```bash
   kubectl --context=grigri create -f system/kanidm/templates/backup-schedule.yaml
   ```

### Change Backup Schedule
Since schedule is immutable:
```bash
kubectl --context=grigri delete kanidmbackupschedule kanidm -n kanidm
kubectl --context=grigri create -f system/kanidm/templates/backup-schedule.yaml
```

### Verify Backup System Health
```bash
# Check schedule is configured
kubectl --context=grigri get kanidmbackupschedule kanidm -n kanidm

# Check backups are being created
kubectl --context=grigri get kanidmbackups -n kanidm

# Check S3 has backups
kubectl --context=grigri exec -n minio mc-cleanup -- mc ls minio/kaniop/kanidm/v1/tenants/kanidm/clusters/be36942f-0685-4d42-a137-8faa891d3830/backups/ | wc -l

# Check Kanidm backup config
kubectl --context=grigri logs -n kanidm kanidm-default-0 -c kanidm --since=10m | grep "online_backup"
```

## Retention Policy Configuration

Current retention policy:
```yaml
retention:
  keepLast: 8      # Always keep last 8 backups
  daily: 7         # Keep one backup per day for 7 days
  weekly: 4        # Keep one backup per week for 4 weeks
  monthly: 12      # Keep one backup per month for 12 months
```

Retention is applied based on `createdAt` timestamp in KanidmBackup CR status, not CR creation time.

## Common Issues

### Issue: Many backups in S3 but few CRs
**Cause**: Discovery truncation (1000 manifest limit)
**Fix**: Clean up orphaned S3 backups (see Cleanup procedure above)

### Issue: Retention not deleting old backups
**Cause**: Retention only works on CRs, not orphaned S3 data
**Fix**: Ensure discovery is working and creating CRs for all S3 backups

### Issue: Backups not being uploaded to S3
**Cause**: data-mover-transport sidecar not running or S3 credentials invalid
**Fix**: Check sidecar logs and KanidmBackupRepository configuration

### Issue: KanidmBackupSchedule OutOfSync in ArgoCD
**Cause**: Schedule field is immutable, or namespace missing
**Fix**: Delete and recreate the CR with correct values

## References
- Kaniop GitHub: https://github.com/pando85/kaniop
- PR #1004: Hardened transport retention and restore validation
- PR #998: Fixed discovery result truncation
