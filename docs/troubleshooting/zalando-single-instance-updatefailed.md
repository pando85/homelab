# Zalando single-instance cluster stuck `UpdateFailed` after a resource change

## Problem

After changing `spec.resources` (e.g. lowering a memory request) on a **single-replica**
(`numberOfInstances: 1`) Zalando `postgresql` CR, the operator sets
`status.PostgresClusterStatus: UpdateFailed` and ArgoCD reports the application `Degraded`.

The database is actually fine: the postgres pod is `Running`, Patroni reports it `Leader`/`running`
with `0` restarts, the new resource values are applied to the StatefulSet and pod, and clients
connect normally. Only the operator's status is stuck. A periodic resync does not reliably clear it.

This is distinct from the [stale-DCS deadlock](zalando-patroni-stale-dcs-deadlock.md), which happens
after a PVC recreate; here the data dir and DCS are untouched.

## Root Cause

To apply a pod-spec change such as resources, the operator must recreate the pod. With a single
instance there is no replica to fail over to:

```
cannot perform switch over before re-creating the pod: no replicas
recreating old master pod "<ns>/<cluster>-0"
```

The operator deletes the sole master and waits for the old pod object to disappear. The StatefulSet
controller immediately recreates a pod **with the same name**, so the operator's deletion-wait cannot
confirm the old pod is gone and eventually times out (~10 minutes):

```
could not sync statefulsets: could not recreate pods: could not recreate old master pod
"<ns>/<cluster>-0": pod deletion wait timeout
```

The recreate itself succeeded — the new pod carries the updated resources — but the operator's
bookkeeping ended in error, leaving `UpdateFailed` latched on the CR.

## How to Diagnose

```bash
# Operator logs show the no-replica recreate + deletion timeout
kubectl --context=grigri -n postgres-operator logs deploy/postgres-operator --tail=2000 \
  | grep -i '<cluster>' | grep -iE 'no replicas|recreat|deletion wait|updated'

# CR status is latched
kubectl --context=grigri -n <ns> get postgresql <cluster> \
  -o jsonpath='{.status.PostgresClusterStatus}{"\n"}'   # -> UpdateFailed

# But the pod is healthy AND the change is applied
kubectl --context=grigri -n <ns> exec <cluster>-0 -c postgres -- patronictl list
kubectl --context=grigri -n <ns> get pod <cluster>-0 \
  -o jsonpath='{.spec.containers[0].resources}{"\n"}'
```

If the pod is `Leader`/`running`, restarts are not climbing, and the StatefulSet resources already
match the CR, the failure is cosmetic bookkeeping — not a real outage.

## Fix / Workaround

Restart the postgres-operator to clear the latched state. It resyncs, sees the StatefulSet already
matches the desired spec, does nothing destructive, and sets the status back to `Running`:

```bash
kubectl --context=grigri -n postgres-operator rollout restart deploy/postgres-operator
kubectl --context=grigri -n <ns> get postgresql <cluster> \
  -o jsonpath='{.status.PostgresClusterStatus}{"\n"}'   # -> Running
```

This restarts the control-plane operator only; it does **not** touch the database pods or data. It
does briefly pause reconciliation for all Zalando clusters, so prefer it over deleting/recreating
the postgres pod.

## Prevention

Resource/spec changes on a single-instance cluster inherently require recreating the sole master
(a brief blip) and can trip this deletion-wait timeout. Where practical, batch resource changes, or
expect a transient `UpdateFailed` that needs an operator restart to clear. Multi-instance clusters
can switch over to a replica and avoid the same-name deletion-wait confusion.
