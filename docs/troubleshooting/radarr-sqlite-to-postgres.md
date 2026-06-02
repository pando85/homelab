# Radarr SQLite to PostgreSQL Migration

## Problem

Radarr web UI was slow or failed to load. Logs showed recurring `SQLiteException: database is
locked` errors (15+ occurrences in 6 hours). The SQLite database had grown to 104 MB on ZFS
storage, and frequent RSS syncs (900+ releases every 30 min) caused severe lock contention under
SQLite's single-writer model.

## Root Cause

SQLite uses a single-writer lock. When Radarr processes RSS syncs, download monitoring, import
lists, and web UI requests concurrently, writers block each other. On ZFS (copy-on-write), the
I/O latency compounds the problem, causing longer lock hold times and more failures.

PostgreSQL handles concurrent readers and writers without locking, eliminating the bottleneck.

## Migration Procedure

Based on the [community migration guide](https://gist.github.com/tobz/929fd4ad8da80ac2ce524af73d4ea615),
adapted for our Zalando Postgres operator and GitOps workflow.

### Step 1: Create Postgres cluster

Add a `postgresql.yaml` template to the app's Helm chart (see existing apps like `immich` or
`home-assistant` for patterns). **Important:** the Zalando operator rejects database names
containing hyphens — see pitfall below. Create databases manually instead.

### Step 2: Let Radarr create the schema

Add `RADARR__POSTGRES__HOST`, `PORT`, `USER`, `PASSWORD`, `MAINDB`, `LOGDB` env vars to the
values file. Deploy and let Radarr start — it creates all tables automatically. Verify via:

```bash
kubectl exec -n radarr radarr-postgres-0 -- psql -U radarr -d radarr-main -c "\dt"
```

### Step 3: Dump schema, drop/recreate databases with clean tables

Radarr inserts initial data (config entries, migration history) when creating the schema. These
rows conflict with the SQLite data import. Clean slate is required:

```bash
# Dump schema
kubectl exec -n radarr radarr-postgres-0 -- pg_dump -U radarr -d radarr-main -s > radarr-main-schema.sql
kubectl exec -n radarr radarr-postgres-0 -- pg_dump -U radarr -d radarr-log -s > radarr-log-schema.sql

# Terminate connections before dropping
kubectl exec -n radarr radarr-postgres-0 -- psql -U postgres -c \
  "SELECT pg_terminate_backend(pid) FROM pg_stat_activity WHERE datname IN ('radarr-main','radarr-log') AND pid <> pg_backend_pid();"

# Drop and recreate
kubectl exec -n radarr radarr-postgres-0 -- psql -U postgres -c 'DROP DATABASE "radarr-main";'
kubectl exec -n radarr radarr-postgres-0 -- psql -U postgres -c 'CREATE DATABASE "radarr-main" OWNER radarr;'
# Repeat for radarr-log

# Reimport clean schema
cat radarr-main-schema.sql | kubectl exec -n radarr -i radarr-postgres-0 -- psql -U radarr -d radarr-main
cat radarr-log-schema.sql | kubectl exec -n radarr -i radarr-postgres-0 -- psql -U radarr -d radarr-log
```

### Step 4: Migrate data with pgloader

Run pgloader as a Kubernetes Job inside the cluster (avoids port-forward SSL issues):

```bash
cat <<EOF | kubectl apply -f -
apiVersion: batch/v1
kind: Job
metadata:
  name: pgloader-radarr-main
  namespace: radarr
spec:
  backoffLimit: 0
  template:
    spec:
      restartPolicy: Never
      initContainers:
      - name: copy-db
        image: busybox
        command: ["sh", "-c", "cp /config/radarr.db /work/radarr.db && chmod 644 /work/radarr.db"]
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
          - /work/radarr.db
          - "postgresql://USER:PASS@radarr-postgres.radarr.svc:5432/radarr-main"
        volumeMounts:
        - name: work
          mountPath: /work
      volumes:
      - name: config
        persistentVolumeClaim:
          claimName: config-radarr
          readOnly: true
      - name: work
        emptyDir: {}
EOF
```

Run a separate job for the logs database (`logs.db` → `radarr-log`).

### Step 5: Start Radarr and verify

Remove `replicas: 0` from values, push, wait for ArgoCD sync. Verify:

```bash
# Check no "database is locked" errors
kubectl logs -n radarr radarr-0 --tail=50 | grep -i "locked"
# Confirm movie count
kubectl exec -n radarr radarr-postgres-0 -- psql -U radarr -d radarr-main -c 'SELECT COUNT(*) FROM "Movies";'
```

## Pitfalls Encountered

### Zalando operator rejects hyphenated database names

The `databases` field in the postgresql manifest rejects names with hyphens (`radarr-main`):
`database "radarr-main" has invalid name`. Create databases manually with `kubectl exec psql`
instead.

### pgloader can't connect via SSL to Zalando Postgres

Zalando's Spilo uses self-signed certs. pgloader's `--no-ssl-cert-verification` flag does not
work (at least in `ghcr.io/roxedus/pgloader:latest`). Two workarounds:

1. **Preferred:** Run pgloader as an in-cluster Job — the pod network uses `hostnossl ... reject`
in `pg_hba.conf`, so temporarily add an allow rule:
   ```bash
   kubectl exec -n radarr radarr-postgres-0 -- sed -i \
     '/hostnossl all.*all.*all.*reject/i host    all    all    10.0.0.0/8    md5' \
     /home/postgres/pgdata/pgroot/data/pg_hba.conf
   kubectl exec -n radarr radarr-postgres-0 -- psql -U postgres -c "SELECT pg_reload_conf();"
   # After migration, remove the line (or let the operator reconcile it)
   ```

2. **Alternative:** Use port-forward with `sslmode=require` and accept the cert error won't apply
   (doesn't work reliably — port-forward dies, Docker `--network=host` can't reach it).

### SQLite needs write access to its directory

pgloader cannot read SQLite from a read-only PVC mount — SQLite creates temp files (WAL, journal)
in the same directory. Use an init container to copy the `.db` file to an `emptyDir` volume first.

### ArgoCD reverts manual scale changes

`kubectl scale statefulset radarr --replicas=0` gets reverted by ArgoCD auto-sync. Always set
`replicas: 0` in `values.yaml` and push to git instead.

### Check job status early, don't blind-wait

When running Kubernetes Jobs, check pod status after a few seconds before committing to a long
`kubectl wait --timeout=600s`. Failed jobs fail fast — a 30s delay then `get pods` reveals
errors immediately instead of blocking for 10 minutes.
