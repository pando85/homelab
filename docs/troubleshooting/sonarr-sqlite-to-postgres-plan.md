# Sonarr SQLite to PostgreSQL Migration

Completed 2026-06-02. Preventative migration following the same procedure used for Radarr.
See `docs/troubleshooting/radarr-sqlite-to-postgres.md` for pitfall details.

## Current State

| Item | Value |
|------|-------|
| SQLite `sonarr.db` | 160 MB |
| SQLite `logs.db` | 8 MB |
| Memory usage | 254 Mi (limit 700 Mi) |
| PVC | `config-sonarr`, 5 Gi, prusik |
| Locked errors | 0 (but same single-writer risk as Radarr) |
| HostPath mount | `/datasets/series` |

## Phase 1: Create Postgres Cluster (git commit)

### 1a. Create `apps/sonarr/templates/postgresql.yaml`

```yaml
apiVersion: "acid.zalan.do/v1"
kind: postgresql
metadata:
  name: sonarr-postgres
  labels:
    backup/retain: quaterly
spec:
  teamId: sonarr

  numberOfInstances: 1

  resources:
    requests:
      cpu: 10m
      memory: 128Mi
    limits:
      memory: 512Mi

  volume:
    size: 2Gi

  users:
    sonarr:
      - superuser
      - createdb
  # databases with hyphens are rejected by the Zalando operator
  # ("invalid name"). Created manually instead:
  #   CREATE DATABASE "sonarr-main" OWNER sonarr;
  #   CREATE DATABASE "sonarr-log" OWNER sonarr;

  postgresql:
    version: "17"
    parameters:
      archive_mode: "off"
      max_connections: "25"
      shared_buffers: 128MB
      log_checkpoints: "off"
      log_connections: "off"
      log_disconnections: "off"
      log_lock_waits: "off"
      log_min_duration_statement: "-1"
      log_statement: none
      full_page_writes: "off"
```

### 1b. Create `apps/sonarr/templates/snapshots-postgres.yaml`

```yaml
apiVersion: snapscheduler.backube/v1
kind: SnapshotSchedule
metadata:
  name: sonarr-postgres-backups
spec:
  retention:
    maxCount: 1
  schedule: "0 1 * * *" # UTC
  claimSelector:
    matchLabels:
      cluster-name: sonarr-postgres
```

### 1c. Commit and push

```bash
git add apps/sonarr/templates/postgresql.yaml apps/sonarr/templates/snapshots-postgres.yaml
git commit -m "sonarr: Add Postgres cluster and snapshot schedule"
git push
```

### 1d. Wait for ArgoCD sync

```bash
kubectl --context=grigri get pods -n sonarr -w
# Wait for sonarr-postgres-0 to be Running
```

### 1e. Create databases manually (Zalando rejects hyphenated names)

```bash
kubectl --context=grigri exec -n sonarr sonarr-postgres-0 -- psql -U postgres -c \
  'CREATE DATABASE "sonarr-main" OWNER sonarr;'
kubectl --context=grigri exec -n sonarr sonarr-postgres-0 -- psql -U postgres -c \
  'CREATE DATABASE "sonarr-log" OWNER sonarr;'
```

## Phase 2: Add Postgres Env Vars (git commit)

### 2a. Edit `apps/sonarr/values.yaml`

Add Postgres env vars and increase memory limit:

```yaml
          env:
            TZ: Europe/Madrid
            SONARR__POSTGRES__HOST: sonarr-postgres
            SONARR__POSTGRES__PORT: "5432"
            SONARR__POSTGRES__MAINDB: sonarr-main
            SONARR__POSTGRES__LOGDB: sonarr-log
            SONARR__POSTGRES__USER:
              valueFrom:
                secretKeyRef:
                  name: sonarr.sonarr-postgres.credentials.postgresql.acid.zalan.do
                  key: username
            SONARR__POSTGRES__PASSWORD:
              valueFrom:
                secretKeyRef:
                  name: sonarr.sonarr-postgres.credentials.postgresql.acid.zalan.do
                  key: password

          resources:
            requests:
              cpu: 150m
              memory: 170Mi
            limits:
              memory: 1Gi    # increased from 700Mi for Postgres overhead
```

### 2b. Commit and push

```bash
git add apps/sonarr/values.yaml
git commit -m "sonarr: Add Postgres env vars and increase memory limit"
git push
```

### 2c. Wait for ArgoCD sync — Sonarr starts on Postgres

```bash
kubectl --context=grigri logs -n sonarr sonarr-0 -f --tail=50
# Look for: "Sonarr is running with Postgres" or similar startup messages
```

### 2d. Verify schema was created

```bash
kubectl --context=grigri exec -n sonarr sonarr-postgres-0 -- psql -U sonarr -d sonarr-main -c "\dt"
kubectl --context=grigri exec -n sonarr sonarr-postgres-0 -- psql -U sonarr -d sonarr-log -c "\dt"
```

## Phase 3: Dump Schema, Clean Recreate (manual)

Sonarr inserts initial data (config, migration history) when creating the schema. These rows
conflict with the SQLite data import.

### 3a. Backup current Sonarr config

```bash
mkdir -p /tmp/sonarr-migration
kubectl --context=grigri cp sonarr/sonarr-0:/config/config.xml /tmp/sonarr-migration/config.xml.bak
kubectl --context=grigri cp sonarr/sonarr-0:/config/sonarr.db /tmp/sonarr-migration/sonarr.db.bak
kubectl --context=grigri cp sonarr/sonarr-0:/config/logs.db /tmp/sonarr-migration/logs.db.bak
```

### 3b. Dump schema

```bash
kubectl --context=grigri exec -n sonarr sonarr-postgres-0 -- pg_dump -U sonarr -d sonarr-main -s > /tmp/sonarr-migration/sonarr-main-schema.sql
kubectl --context=grigri exec -n sonarr sonarr-postgres-0 -- pg_dump -U sonarr -d sonarr-log -s > /tmp/sonarr-migration/sonarr-log-schema.sql
```

### 3c. Terminate connections, drop and recreate databases

```bash
kubectl --context=grigri exec -n sonarr sonarr-postgres-0 -- psql -U postgres -c \
  "SELECT pg_terminate_backend(pid) FROM pg_stat_activity WHERE datname IN ('sonarr-main','sonarr-log') AND pid <> pg_backend_pid();"

kubectl --context=grigri exec -n sonarr sonarr-postgres-0 -- psql -U postgres -c 'DROP DATABASE "sonarr-main";'
kubectl --context=grigri exec -n sonarr sonarr-postgres-0 -- psql -U postgres -c 'CREATE DATABASE "sonarr-main" OWNER sonarr;'

kubectl --context=grigri exec -n sonarr sonarr-postgres-0 -- psql -U postgres -c 'DROP DATABASE "sonarr-log";'
kubectl --context=grigri exec -n sonarr sonarr-postgres-0 -- psql -U postgres -c 'CREATE DATABASE "sonarr-log" OWNER sonarr;'
```

### 3d. Reimport clean schema

```bash
cat /tmp/sonarr-migration/sonarr-main-schema.sql | kubectl --context=grigri exec -n sonarr -i sonarr-postgres-0 -- psql -U sonarr -d sonarr-main
cat /tmp/sonarr-migration/sonarr-log-schema.sql | kubectl --context=grigri exec -n sonarr -i sonarr-postgres-0 -- psql -U sonarr -d sonarr-log
```

## Phase 4: Migrate Data with pgloader (manual)

### 4a. Scale down Sonarr (git commit)

```yaml
# In apps/sonarr/values.yaml, add under controllers.sonarr:
replicas: 0
```

```bash
git add apps/sonarr/values.yaml
git commit -m "sonarr: Scale down for Postgres data migration"
git push
# Wait for pod to terminate
kubectl --context=grigri get pods -n sonarr -w
```

### 4b. Temporarily allow non-SSL connections in pg_hba.conf

```bash
kubectl --context=grigri exec -n sonarr sonarr-postgres-0 -- sed -i \
  '/hostnossl all.*all.*all.*reject/i host    all    all    10.0.0.0/8    md5' \
  /home/postgres/pgdata/pgroot/data/pg_hba.conf
kubectl --context=grigri exec -n sonarr sonarr-postgres-0 -- psql -U postgres -c "SELECT pg_reload_conf();"
```

### 4c. Get Postgres credentials

```bash
SONARR_PG_USER=$(kubectl --context=grigri get secret -n sonarr sonarr.sonarr-postgres.credentials.postgresql.acid.zalan.do -o jsonpath='{.data.username}' | base64 -d)
SONARR_PG_PASS=$(kubectl --context=grigri get secret -n sonarr sonarr.sonarr-postgres.credentials.postgresql.acid.zalan.do -o jsonpath='{.data.password}' | base64 -d)
echo "User: $SONARR_PG_USER"
```

### 4d. Run pgloader for main database (160 MB — expect 2-3 min)

```bash
cat <<EOF | kubectl apply -f -
apiVersion: batch/v1
kind: Job
metadata:
  name: pgloader-sonarr-main
  namespace: sonarr
spec:
  backoffLimit: 0
  template:
    spec:
      restartPolicy: Never
      initContainers:
      - name: copy-db
        image: busybox
        command: ["sh", "-c", "cp /config/sonarr.db /work/sonarr.db && chmod 644 /work/sonarr.db"]
        securityContext:
          runAsUser: 995
          runAsGroup: 501
        volumeMounts:
        - name: config
          mountPath: /config
          readOnly: true
        - name: work
          mountPath: /work
      containers:
      - name: pgloader
        image: ghcr.io/roxedus/pgloader:latest
        command:
          - pgloader
          - --with
          - "quote identifiers"
          - --with
          - "data only"
          - /work/sonarr.db
          - "postgresql://${SONARR_PG_USER}:${SONARR_PG_PASS}@sonarr-postgres.sonarr.svc:5432/sonarr-main"
        volumeMounts:
        - name: work
          mountPath: /work
      volumes:
      - name: config
        persistentVolumeClaim:
          claimName: config-sonarr
          readOnly: true
      - name: work
        emptyDir: {}
EOF
```

### 4e. Check pgloader status

```bash
sleep 10
kubectl --context=grigri get pods -n sonarr -l job-name=pgloader-sonarr-main
# If Completed, check logs:
kubectl --context=grigri logs -n sonarr job/pgloader-sonarr-main
# If failed, check logs for errors
```

### 4f. Run pgloader for logs database

```bash
cat <<EOF | kubectl apply -f -
apiVersion: batch/v1
kind: Job
metadata:
  name: pgloader-sonarr-log
  namespace: sonarr
spec:
  backoffLimit: 0
  template:
    spec:
      restartPolicy: Never
      initContainers:
      - name: copy-db
        image: busybox
        command: ["sh", "-c", "cp /config/logs.db /work/logs.db && chmod 644 /work/logs.db"]
        securityContext:
          runAsUser: 995
          runAsGroup: 501
        volumeMounts:
        - name: config
          mountPath: /config
          readOnly: true
        - name: work
          mountPath: /work
      containers:
      - name: pgloader
        image: ghcr.io/roxedus/pgloader:latest
        command:
          - pgloader
          - --with
          - "quote identifiers"
          - --with
          - "data only"
          - /work/logs.db
          - "postgresql://${SONARR_PG_USER}:${SONARR_PG_PASS}@sonarr-postgres.sonarr.svc:5432/sonarr-log"
        volumeMounts:
        - name: work
          mountPath: /work
      volumes:
      - name: config
        persistentVolumeClaim:
          claimName: config-sonarr
          readOnly: true
      - name: work
        emptyDir: {}
EOF
```

### 4g. Restore pg_hba.conf

```bash
kubectl --context=grigri exec -n sonarr sonarr-postgres-0 -- sed -i \
  '/host    all    all    10\.0\.0\.0\/8    md5/d' \
  /home/postgres/pgdata/pgroot/data/pg_hba.conf
kubectl --context=grigri exec -n sonarr sonarr-postgres-0 -- psql -U postgres -c "SELECT pg_reload_conf();"
```

## Phase 5: Verify and Start Sonarr (git commit)

### 5a. Verify data before starting

```bash
kubectl --context=grigri exec -n sonarr sonarr-postgres-0 -- psql -U sonarr -d sonarr-main -c 'SELECT COUNT(*) FROM "Series";'
# Should match the series count from Sonarr UI before migration
```

### 5b. Scale up Sonarr (git commit)

Remove `replicas: 0` from `apps/sonarr/values.yaml`:

```bash
git add apps/sonarr/values.yaml
git commit -m "sonarr: Scale up after Postgres migration"
git push
```

### 5c. Verify

```bash
# Check no "database is locked" errors
kubectl --context=grigri logs -n sonarr sonarr-0 --tail=50 | grep -i "locked"
# Confirm series count
kubectl --context=grigri exec -n sonarr sonarr-postgres-0 -- psql -U sonarr -d sonarr-main -c 'SELECT COUNT(*) FROM "Series";'
# Check memory
kubectl --context=grigri top pod -n sonarr
```

## Rollback

If something goes wrong:

1. Set `replicas: 0` in values.yaml, push
2. Remove all `SONARR__POSTGRES__*` env vars from values.yaml, push
3. Restore config.xml from backup:
   ```bash
   kubectl --context=grigri cp /tmp/sonarr-migration/config.xml.bak sonarr/sonarr-0:/config/config.xml
   ```
4. SQLite files on the PVC are untouched — Sonarr will use them when Postgres env vars are removed

## Cleanup (after confirming everything works)

- Delete pgloader jobs: `kubectl delete job -n sonarr pgloader-sonarr-main pgloader-sonarr-log`
- Remove local backups: `rm -rf /tmp/sonarr-migration`
- Optionally shrink Sonarr PVC (still has old SQLite files taking space)

## Results

| Metric | Before (SQLite) | After (Postgres) |
|--------|----------------|-----------------|
| DB size | 160 MB (sonarr.db) + 8 MB (logs.db) | 73 MB (main) + ~6 MB (log) |
| Memory | 254 Mi | 176 Mi (Sonarr) + 205 Mi (Postgres) |
| "database is locked" errors | 0 (preventative) | 0 |
| Series | 198 | 198 |
| Episodes | 9,182 | 9,182 |
| Episode files | 4,810 | 4,810 |
| pgloader time | - | 7.7s (main) + 1.2s (log) |

### Commits (4 total)

1. `sonarr: Add Postgres cluster and snapshot schedule` — postgresql.yaml + snapshots-postgres.yaml
2. `sonarr: Add Postgres env vars and increase memory limit` — values.yaml (env vars + 700Mi→1Gi)
3. `sonarr: Scale down for Postgres data migration` — values.yaml (replicas: 0)
4. `sonarr: Scale up after Postgres migration` — values.yaml (removed replicas: 0)

### Notes

- pgloader migrated 75,571 rows (main) and 33,498 rows (log) with 0 errors
- Postgres memory limit: 512 Mi (actual usage ~205 Mi)
- Sonarr memory limit: 1 Gi (actual usage ~176 Mi)
- Local backups at `/tmp/sonarr-migration/` for rollback safety
- SQLite files still on PVC at `/config/sonarr.db` and `/config/logs.db` (untouched)
