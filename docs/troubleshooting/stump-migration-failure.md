# Stump SQLite Migration Failure

## Problem

Stump pod crashes on startup with SQLite error: `there is already another table or
index with this name: reading_sessions_legacy` or `no such table: reading_sessions`.

This occurs after upgrading Stump to v0.1.5+ when the `m20260519_192218_reading_sessions_v2`
migration fails to complete.

## Root Cause

Stump v0.1.5 introduced migration `m20260519_192218_reading_sessions_v2` which:

1. Renames `reading_sessions` → `reading_sessions_legacy`
2. Renames `finished_reading_sessions` → `finished_reading_sessions_legacy`
3. Creates new `reading_sessions` table with v2 schema
4. Migrates data from legacy tables
5. Drops legacy tables

If the migration partially fails (e.g., at step 5), legacy tables remain and subsequent
startup attempts fail because the migration tries to rename tables that already exist.

## How to Diagnose

```bash
# Check pod logs
kubectl --context=grigri logs stump-0 -n stump --tail=30

# Check database tables
kubectl --context=grigri run stump-db-check --rm --restart=Never --namespace=stump \
  --image=alpine --overrides='{
    "spec": {
      "nodeSelector": {"kubernetes.io/hostname": "prusik"},
      "volumes": [{"name":"config","persistentVolumeClaim":{"claimName":"config-stump"}}],
      "containers": [{
        "name": "check",
        "image": "alpine",
        "command": ["sh", "-c", "apk add sqlite && sqlite3 /config/stump.db \".tables\""],
        "volumeMounts": [{"name":"config","mountPath":"/config"}]
      }]
    }
  }'

# Check for legacy tables
sqlite3 /config/stump.db "SELECT name, type FROM sqlite_master WHERE name LIKE 'reading_sessions%';"
```

## Fix

### Option 1: Restore from Snapshot (Recommended)

If daily snapshots exist, restore from a snapshot before the migration ran:

```bash
# List available snapshots
kubectl --context=grigri get volumesnapshot -n stump

# Create PVC from snapshot (e.g., from 3 days ago)
kubectl apply -f - <<EOF
apiVersion: v1
kind: PersistentVolumeClaim
metadata:
  name: config-stump-restored
  namespace: stump
spec:
  accessModes: [ReadWriteOnce]
  resources:
    requests:
      storage: 500Mi
  dataSource:
    name: config-stump-stump-backups-YYYYMMDD0100
    kind: VolumeSnapshot
    apiGroup: snapshot.storage.k8s.io
EOF

# Wait for binding, then manually complete the migration
# (see Option 2 steps 2-4)
```

After restoring, manually complete the migration:

```bash
# Drop legacy tables
sqlite3 /config/stump.db "DROP TABLE IF EXISTS reading_sessions_legacy;"
sqlite3 /config/stump.db "DROP TABLE IF EXISTS finished_reading_sessions_legacy;"

# Create missing indexes (use quoted names for hyphens)
sqlite3 /config/stump.db "CREATE INDEX IF NOT EXISTS 'idx-reading_sessions-media' ON reading_sessions(media_id);"
sqlite3 /config/stump.db "CREATE INDEX IF NOT EXISTS 'idx-reading_sessions-user-media-recent' ON reading_sessions(user_id, media_id, updated_at, created_at, id);"

# Add missing columns to user_preferences
sqlite3 /config/stump.db "ALTER TABLE user_preferences ADD COLUMN enable_reading_journal INTEGER NOT NULL DEFAULT 0;"
sqlite3 /config/stump.db "ALTER TABLE user_preferences ADD COLUMN day_reset_hour_offset INTEGER NOT NULL DEFAULT 0;"
sqlite3 /config/stump.db "ALTER TABLE user_preferences ADD COLUMN reading_session_grace_period_secs BIGINT NOT NULL DEFAULT 1800;"
```

### Option 2: FORCE_DB_RESET (Data Loss)

If no snapshots or data is not critical:

```yaml
# Add to values.yaml under env:
FORCE_DB_RESET: "true"
```

Let ArgoCD sync, then remove the variable after successful startup.

## Related

- Stump migration source: `crates/migrations/src/m20260519_192218_reading_sessions_v2.rs`
- AGENTS.md note: "Stump v0.1.5+ migration `m20260519_192218_reading_sessions_v2` can fail
  mid-way leaving legacy tables. Restore from snapshot or complete migration manually."
