# Zalando Patroni Stale DCS Deadlock After PVC Recreate

## Symptoms

After deleting a Zalando Postgres PVC and letting the pod recreate with a fresh volume:

- Pod shows `2/2 Running` (Patroni container is running)
- PostgreSQL is **not serving** — connections fail with "database does not exist" or "connection refused"
- `patronictl list` shows the member as **stopped / Replica** with no Leader
- Logs show: `"waiting for leader to bootstrap"` repeating every 10 seconds
- The `readest-postgres-config` ConfigMap has an `initialize` annotation with an old system identifier
- The postgres data directory is empty (never initdb'd)

## Root Cause

When you delete the PVC, the **Patroni Kubernetes DCS ConfigMaps survive** (they're runtime state, not GitOps-managed, no owner references). These ConfigMaps carry the old cluster's `initialize` marker (system identifier).

Patroni sees the `initialize` marker in the DCS → believes the cluster already exists → refuses to run `initdb` on the fresh empty data directory → waits forever for a Leader that will never appear. **Deadlock.**

The Patroni DCS objects are:
- `readest-postgres-config` (holds `initialize` marker + dynamic config)
- `readest-postgres-leader` (leader lock + replication slot state)

Both are ConfigMaps in the app namespace, created by Patroni, not managed by the Zalando operator or ArgoCD.

## Diagnosis

```bash
# Check pod status
kubectl --context=grigri get pods -n readest -o wide

# Check Patroni cluster state
kubectl --context=grigri exec -n readest readest-postgres-0 -c postgres -- patronictl list

# Check Patroni logs
kubectl --context=grigri logs -n readest readest-postgres-0 -c postgres --tail=40 | grep -i "waiting for leader"

# Check if initialize marker exists
kubectl --context=grigri get configmap readest-postgres-config -n readest -o jsonpath='{.metadata.annotations.initialize}'

# Check postgres data directory (should be empty if never initdb'd)
kubectl --context=grigri exec -n readest readest-postgres-0 -c postgres -- ls -la /home/postgres/pgdata/pgroot/data/
```

If you see:
- `patronictl list` → member "stopped", no Leader
- Logs → "waiting for leader to bootstrap"
- `initialize` annotation present with old system ID
- Empty data directory

→ You have the stale DCS deadlock.

## Fix

**Delete the stale Patroni DCS ConfigMaps and restart the pod.** This is safe: the Zalando operator regenerates the dynamic config from the CR, and Patroni writes a fresh `initialize` marker after `initdb`.

```bash
# 1. Delete stale Patroni DCS ConfigMaps
kubectl --context=grigri delete configmap readest-postgres-config readest-postgres-leader -n readest

# 2. Restart the pod to trigger fresh bootstrap
kubectl --context=grigri delete pod readest-postgres-0 -n readest

# 3. Wait ~30s, then verify Patroni becomes Leader
kubectl --context=grigri exec -n readest readest-postgres-0 -c postgres -- patronictl list
# Should show: Role=Leader, State=running
```

After Patroni bootstraps, the Zalando operator will create the database and roles from the CR. If the operator doesn't sync automatically, force it:

```bash
# Force operator sync by annotating the CR
kubectl --context=grigri annotate postgresql readest-postgres -n readest zalando.org/sync-at="$(date +%s)" --overwrite
```

## Post-Bootstrap Steps

After Patroni bootstraps and the operator creates the database:

1. **Wait for GoTrue + db-migrate** to populate the schema (they run as init containers / startup hooks)
2. **Restore non-GitOps data** (if applicable):
   - `ALTER DATABASE readest SET search_path TO auth, public;`
   - `INSERT INTO auth.custom_oauth_providers ...` (custom OIDC provider)
   - User data, app data, etc.
3. **Restart app pods** to ensure they pick up the fresh schema

## Prevention

To avoid this in the future:

- **Don't delete Zalando Postgres PVCs without also deleting the Patroni DCS ConfigMaps**
- If you must recreate, delete both the PVC and the DCS ConfigMaps in one operation
- Document the recreate procedure in the deployment docs

## Why This Happens

Zalando Postgres uses Patroni for HA. Patroni stores cluster state in Kubernetes DCS (ConfigMaps or Endpoints). The `initialize` marker is written after the first successful `initdb` and contains the cluster's system identifier. When Patroni starts, it checks:

1. Is there an `initialize` marker in the DCS?
2. Does my local data directory match the system identifier?

If (1) yes and (2) no (empty data dir), Patroni assumes it's a replica waiting for the Leader to bootstrap. But if there's no Leader (the old pod is gone), it waits forever.

The fix removes the stale `initialize` marker, so Patroni treats this as a fresh cluster and runs `initdb`.

## References

- Affected deployment: `apps/readest/templates/postgresql.yaml`
- Related: `docs/deployment/readest.md` (Zalando Postgres + automated migrations)
- Patroni DCS docs: https://patroni.readthedocs.io/en/latest/kubernetes.html
