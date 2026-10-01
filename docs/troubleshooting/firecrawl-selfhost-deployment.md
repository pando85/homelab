# Firecrawl Self-Hosted Deployment Issues

## Problem

Firecrawl self-hosted deployment (image `ghcr.io/firecrawl/firecrawl:2.10.19`, Zalando Postgres,
inline Redis) failed at multiple layers after initial deploy. Symptoms ranged from misleading
Postgres auth errors to queue worker crashes and missing database tables.

## Root Causes

### 1. Masked placeholder in DATABASE_URL (`28P01` password auth failure)

`values.yaml` contained a literal `***` where `$(DATABASE_PASSWORD)` belonged — a masked
placeholder got committed. Kubelet expanded `$(DATABASE_USERNAME)` via env var reference, but the
password stayed as literal `***`. Workers failed with `28P01 password authentication failed for
user "firecrawl"`, which looks like a wrong credential but is actually a broken expansion
placeholder.

### 2. Redis `/data` owned by wrong UID → `MISCONF` blocks all writes

The Redis container runs as uid 1000, but the image's `/data` directory is owned by uid 999.
`BGSAVE` failed (cannot write RDB), and Redis's `stop-writes-on-bgsave-error yes` (default) then
rejected ALL write commands with `MISCONF Redis is configured to save RDB snapshots ...`. The queue
worker crashed because it could not enqueue.

### 3. `nuq` queue schema not auto-created (42P01 `relation "nuq.queue_scrape" does not exist`)

Upstream Firecrawl provisions the `nuq` queue schema via a **custom Postgres image** that runs
`apps/nuq-postgres/nuq.sql` from `docker-entrypoint-initdb.d`. On Zalando/Spilo, nobody runs that
SQL — the Spilo image has its own entrypoint. The app connects and finds no `nuq` schema at all.

### 4. `pg_cron` requires `cron.database_name` GUC (restart-required)

`nuq.sql` registers 36 housekeeping jobs via `pg_cron`. The extension needs
`cron.database_name=firecrawl` to know which database to schedule jobs in. This is a
restart-required GUC set via the Zalando Postgres CR `parameters`. Patroni/Spilo caches it — the
postgres pod must be deleted once for `pg_cron`'s background worker to pick up the new value.

### 5. Zalando `pg_hba` rejects non-SSL; Spilo uses self-signed cert

Zalando's default `pg_hba` includes `hostnossl all all all reject`. Spilo generates a self-signed
TLS cert, so `sslmode=require` fails with `DEPTH_ZERO_SELF_SIGNED_CERT`, and `sslmode=disable`
fails with `28000` (rejected by `pg_hba`). The correct value is `sslmode=no-verify` (supported by
`pg-connection-string` used by node-postgres).

### 6. SnapshotSchedule `claimSelector` label mismatch

The `claimSelector` used `backup: firecrawl-postgres-zfs`, but Zalando PVCs never carry that label.
Zalando PVCs get `cluster-name=<cr-name>`, `team=`, `application=spilo`, plus labels from the CR
metadata. Repo pattern (freshrss, sonarr): select on `cluster-name`.

## How to Diagnose

```bash
# 1. Check if DATABASE_URL was expanded correctly (do NOT print the password)
kubectl --context=grigri exec -n firecrawl deploy/firecrawl-api -- \
  printenv NUQ_DATABASE_URL | md5sum
# Compare against expected hash; or check for literal '***':
kubectl --context=grigri exec -n firecrawl deploy/firecrawl-api -- \
  printenv NUQ_DATABASE_URL | grep -c '\*\*\*'

# 2. Redis MISCONF — check bgsave status
kubectl --context=grigri exec -n firecrawl deploy/firecrawl-api -- \
  redis-cli -h <redis-host> INFO persistence | grep rdb_last_bgsave_status

# 3. Missing nuq schema — check if tables exist
kubectl --context=grigri exec -n firecrawl deploy/firecrawl-api -- \
  psql "$NUQ_DATABASE_URL" -c "SELECT tablename FROM pg_tables WHERE schemaname='nuq';"

# 4. pg_cron database setting
kubectl --context=grigri exec -n firecrawl postgresql-pod -- \
  psql -c "SHOW cron.database_name;"

# 5. SSL mode — check connection string
kubectl --context=grigri exec -n firecrawl deploy/firecrawl-api -- \
  printenv NUQ_DATABASE_URL | grep -o 'sslmode=[^&]*'
```

## Fix / Workaround

### 1. DATABASE_URL placeholder

Ensure `values.yaml` uses the kubelet variable expansion syntax `$(DATABASE_PASSWORD)` — not a
literal or masked value. Verify with hash comparison inside the pod, never by printing the secret.

### 2. Redis writable `/data`

Mount an `emptyDir` at `/data` and set `securityContext.fsGroup: 1000` so the group-writable
directory is available to the Redis process.

### 3. nuq schema init container

`apps/firecrawl/files/nuq.sql` is auto-generated from the upstream firecrawl tag by
`apps/firecrawl/hack/update-nuq-sql.sh` (removes `ALTER SYSTEM` statements, wraps in advisory
locks). The SQL is rendered into ConfigMap `firecrawl-nuq-schema` and applied by an idempotent
`nuq-schema-init` psql init container on api/worker/nuq-worker deployments.

**Version sync automation:**
- **Generator script**: `apps/firecrawl/hack/update-nuq-sql.sh [VERSION]` downloads upstream SQL
  and applies transforms. Defaults to the firecrawl image tag from `values.yaml`.
- **Pre-commit guard**: `apps/firecrawl/hack/check-nuq-version.sh` verifies the Git ref in
  `files/nuq.sql` matches the image tag in `values.yaml`. Runs automatically via pre-commit hook.
- **Deploy-time guard**: Init containers compare the SQL's Git ref against the ConfigMap's
  `expected-version` (rendered from `values.yaml`) and fail fast on mismatch.
- **Renovate post-upgrade task**: `.github/renovate-config.json` has a `packageRule` for
  `ghcr.io/firecrawl/firecrawl` that runs `update-nuq-sql.sh` after version bumps, so the SQL is
  regenerated automatically.

**Manual regeneration** (if automation fails):
```bash
apps/firecrawl/hack/update-nuq-sql.sh <NEW_VERSION>
git add apps/firecrawl/files/nuq.sql
git commit -m "firecrawl: Regenerate nuq.sql for v<NEW_VERSION>"
```

### 4. `cron.database_name`

Set in the Zalando Postgres CR:

```yaml
spec:
  parameters:
    cron.database_name: "firecrawl"
```

After applying, **delete the postgres pod** once so Patroni restarts with the new GUC. Verify:

```bash
kubectl --context=grigri exec -n firecrawl <postgres-pod> -- psql -c "SHOW cron.database_name;"
# Should return: firecrawl
```

### 5. SSL mode

Use `sslmode=no-verify` in the connection string. Both `sslmode=require` (cert verification fails)
and `sslmode=disable` (pg_hba rejects) do not work with Zalando/Spilo's self-signed cert.

### 6. SnapshotSchedule claimSelector

Select on `cluster-name`:

```yaml
spec:
  claimSelector:
    matchLabels:
      cluster-name: firecrawl-postgres
```

## End-to-End Verification

```bash
# Scrape (any Bearer token works when USE_DB_AUTHENTICATION=false)
curl -X POST https://firecrawl.internal.grigri.cloud/v1/scrape \
  -H 'Content-Type: application/json' \
  -H 'Authorization: Bearer any-token' \
  -d '{"url":"https://example.com","formats":["markdown"]}'

# Search (proxies SearXNG)
curl -X POST https://firecrawl.internal.grigri.cloud/v1/search \
  -H 'Content-Type: application/json' \
  -H 'Authorization: Bearer any-token' \
  -d '{"query":"test"}'
```

## Related

- Deployment: `apps/firecrawl/`
- Upstream nuq schema: `apps/nuq-postgres/nuq.sql` in firecrawl repo at tag `v2.10.19`
- [Zalando Patroni Stale DCS Deadlock](zalando-patroni-stale-dcs-deadlock.md)
