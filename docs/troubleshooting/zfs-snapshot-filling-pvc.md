# ZFS Snapshots Filling Up PVC

## Symptoms

`KubePersistentVolumeFillingUp` alert fires for a PVC. `df` inside the pod shows the volume is
nearly full, but the actual data (`du`) is much smaller than the volume capacity.

## Root Cause

ZFS Copy-on-Write snapshots hold references to old blocks that have been overwritten. When an
application frequently rewrites large files (e.g., media cover images), each snapshot accumulates
unique blocks. The cumulative space consumed by all snapshots can far exceed the live data size.

Two independent backup mechanisms create ZFS snapshots on every PVC:

1. **Snapscheduler** (`snapscheduler.backube/v1` SnapshotSchedule) — daily at 01:00 UTC,
   configurable `maxCount` per app (default: 20). Creates `snapshot-<uuid>` ZFS snapshots.

2. **Velero** (Schedule CRD) — weekly on Tuesdays at 02:30 UTC, 90-day TTL. Uses the
   `openebs.io/zfspv-blockstore` plugin which creates `retain-quaterly-<timestamp>` ZFS snapshots
   and uploads volume data to MinIO.

### Known High-Churn Applications

| App | Path | Issue |
|-----|------|-------|
| Radarr | `/config/MediaCover/` (2.7 GB) | Re-downloads poster/fanart images for ~2000 movies daily (~800-1800 file rewrites/day) |
| Sonarr | `/config/MediaCover/` | Same pattern as Radarr |
| Bazarr | `/config/MediaCover/` | Same pattern as Radarr |
| Lidarr | `/config/MediaCover/` | Same pattern as Radarr |
| Prowlarr | `/config/MediaCover/` | Same pattern as Radarr |

Each daily snapshot holds ~50 MB of unique MediaCover blocks. With `maxCount: 20`, that's ~1 GB
minimum in snapshots alone, with burst days reaching 200+ MB per snapshot.

## Diagnosis

```bash
# Check ZFS space breakdown (live data vs snapshots)
zfs list -o name,used,usedbysnapshots,avail,refer <dataset>

# List all snapshots with their unique space usage
zfs list -t snapshot -o name,used,refer,creation <dataset>

# Check what files changed between two snapshots
sudo zfs diff <dataset>@<old-snap> <dataset>@<new-snap> | grep -c MediaCover

# Check inside the pod
kubectl exec -n <namespace> <pod> -- df -h /config
kubectl exec -n <namespace> <pod> -- du -sh /config/* | sort -rh

# Check snapscheduler retention
kubectl get snapshotschedules -A

# Check Velero backup expiration
kubectl get backups -n velero -o custom-columns=NAME:.metadata.name,PHASE:.status.phase,EXPIRATION:.status.expiration
```

## Remediation

### Reduce snapscheduler retention

For high-churn *arr apps, reduce `maxCount` in `apps/<name>/templates/snapshots.yaml`:

```yaml
spec:
  retention:
    maxCount: 5  # was 20, reduced for *arr apps with large MediaCover
```

Snapscheduler prunes excess VolumeSnapshot CRDs (and their ZFS snapshots) at the next scheduled
snapshot time. Changes take effect on the next reconciliation.

### Clean up orphaned ZFS snapshots

When Velero backup objects expire and are garbage-collected, the corresponding ZFS snapshots
should be cleaned up. However, some snapshots may become orphaned (e.g., after a Velero restore
or migration). These appear as ZFS snapshots with no matching Velero backup object or
VolumeSnapshot CRD.

```bash
# Identify orphaned snapshots (ZFS snapshots with no k8s CRD managing them)
zfs list -t snapshot -o name <dataset>

# Destroy orphaned snapshots
sudo zfs destroy <dataset>@<snapshot-name>
```

### Increase ZFS quota (temporary band-aid)

```bash
sudo zfs set quota=20G <dataset>
```

## Prevention

- Use `maxCount: 5` for *arr apps (MediaCover is regenerable — Radarr re-downloads images
  automatically). The real config is only ~50-80 MB (DB + settings).
- Use `maxCount: 1` for database PVCs (Postgres, MariaDB) since they use WAL-based recovery.
- Use `maxCount: 20` only for PVCs with low churn and critical data.
- MediaCover data is not worth snapshotting extensively — Velero weekly backups provide
  sufficient disaster recovery coverage.

## Key Files

| File | Purpose |
|------|---------|
| `apps/<name>/templates/snapshots.yaml` | Snapscheduler SnapshotSchedule CRD per app |
| `platform/velero/templates/schedule-retain-quaterly.yaml` | Velero weekly backup schedule |
| `platform/velero/templates/schedule-retain-weekly.yaml` | Velero weekly full backup schedule |
| `system/zfs-localpv/` | ZFS-LocalPV driver and VolumeSnapshotClass (`zfspv-snapclass`) |
