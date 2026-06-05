# Migrating Zalando Postgres PVC Across Nodes

## Problem

A Zalando Postgres pod and its PVC (openebs-zfspv, local ZFS) are on the wrong node and need to
move to another node. This is common after migrating an app (e.g. radarr/sonarr) to a different
node — the app pods moved but the Postgres cluster's PVC stayed behind.

### Constraints

- `openebs-zfspv` is local storage (RWO). The PV has node affinity pinning it to the source node.
- StatefulSets always remove the highest ordinal on scale-down (`-1` before `-0`).
- The postgres-operator has `enable_pod_antiaffinity: true`, preventing two pods of the same
  cluster from running on the same node.
- ArgoCD has `selfHeal: true` — all changes must go through Git. Manual `kubectl apply/patch`
  will be reverted instantly.
- Deleting the PVC **before** the pod is required. Without it, the pod recreates on the same node
  because the PV's node affinity forces it there.

## Prerequisites

- Source node: the node where the Postgres pod currently runs (e.g. `grigri`)
- Target node: where the Postgres pod should end up (e.g. `prusik`)
- The postgres-operator must be healthy and the cluster in `Running` state

## Procedure

All changes go through Git commits. Manual steps are limited to `cordon`, `delete pvc`, and
`delete pod` — node-level or force-recreate operations that ArgoCD does not track.

### Commit 1: Disable anti-affinity + scale to 2

Files to change:

- `platform/postgres-operator/values.yaml` — set `enable_pod_antiaffinity: false`
- `apps/<app>/templates/postgresql.yaml` — set `numberOfInstances: 2`

Commit and push. Wait for ArgoCD to sync both changes. Verify:

```bash
# Operator picked up new config
kubectl --context=grigri get deploy -n postgres-operator

# Replica -1 appeared on target node
kubectl --context=grigri get pods -n <namespace> -o wide

# Replication is healthy (lag = 0)
kubectl --context=grigri exec -n <namespace> <cluster>-0 -c postgres -- patronictl list
```

### Manual steps: Move pod -0 to target node

```bash
CONTEXT=grigri
NAMESPACE=<namespace>
CLUSTER=<cluster-name>
SOURCE_NODE=<source-node>

# 1. Cordon source node (pods can't schedule there)
kubectl --context=$CONTEXT cordon $SOURCE_NODE

# 2. Delete PVC (removes PV node affinity binding)
kubectl --context=$CONTEXT delete pvc pgdata-$CLUSTER-0 -n $NAMESPACE

# 3. Delete pod (triggers recreation on target node)
kubectl --context=$CONTEXT delete pod $CLUSTER-0 -n $NAMESPACE

# 4. Wait for -0 to come up on target node and rejoin as replica
kubectl --context=$CONTEXT get pods -n $NAMESPACE -o wide -w

# 5. Verify replication healthy
kubectl --context=$CONTEXT exec -n $NAMESPACE $CLUSTER-1 -c postgres -- patronictl list

# 6. Uncordon source node
kubectl --context=$CONTEXT uncordon $SOURCE_NODE
```

After step 3, Patroni bootstraps `-0` from the leader (`-1`). This may take a few minutes
depending on data size.

### Commit 2: Scale back to 1

- `apps/<app>/templates/postgresql.yaml` — set `numberOfInstances: 1`

Commit and push. The operator removes `-1` (highest ordinal). `-0` remains on the target node
and takes over as leader.

Verify:

```bash
kubectl --context=grigri get pods -n <namespace> -o wide
kubectl --context=grigri exec -n <namespace> <cluster>-0 -c postgres -- patronictl list
```

### Commit 3: Re-enable anti-affinity + add nodeAffinity

- `platform/postgres-operator/values.yaml` — set `enable_pod_antiaffinity: true`
- `apps/<app>/templates/postgresql.yaml` — add `nodeAffinity` to keep the postgres pod on the
  target node (see [Post-migration: nodeAffinity](#post-migration-nodeaffinity))

Commit and push.

### Cleanup: Remove leaked ZFS volumes and orphaned PVCs

After scaling back to 1, the `-1` replica PVCs remain as orphans (Bound but unused). The original
`-0` PVCs on the source node are Released. All need manual cleanup.

```bash
# 1. Delete orphaned -1 PVCs (Bound but no pod using them)
kubectl --context=grigri delete pvc pgdata-$CLUSTER-1 -n $NAMESPACE

# 2. Delete released PVs
kubectl --context=grigri get pv | grep Released
kubectl --context=grigri delete pv <released-pv-name>

# 3. Orphaned -1 PVs become Released after PVC deletion — delete those too
kubectl --context=grigri delete pv <orphaned-pv-name>

# 4. Destroy ZFS datasets (may need -r for snapshots)
ssh <source-node> "zfs destroy -r <pool>/<dataset>"
```

PV deletion does **not** automatically destroy the underlying ZFS dataset. The `-r` flag is needed
when ZFS snapshots exist as children.

## Batching Multiple Clusters

If migrating multiple Postgres clusters in the same direction (e.g. both radarr and sonarr),
you can batch them:

- **Commit 1:** disable anti-affinity + scale both to 2
- **Manual:** migrate pod -0 for each cluster (one at a time)
- **Commit 2:** scale both back to 1
- **Commit 3:** re-enable anti-affinity

Do NOT run manual steps for multiple clusters in parallel — complete one cluster fully before
starting the next to avoid confusion.

## Post-migration: nodeAffinity

After migration, add a `preferred` nodeAffinity to the postgresql CRD so the postgres pod prefers
the target node. This prevents accidental scheduling on the wrong node without blocking startup if
the target is temporarily unavailable.

```yaml
spec:
  nodeAffinity:
    preferredDuringSchedulingIgnoredDuringExecution:
      - weight: 100
        preference:
          matchExpressions:
            - key: kubernetes.io/hostname
              operator: In
              values:
                - <target-node>
```

Use `preferred` rather than `required` — a hard requirement would prevent the pod from starting at
all if the target node is down, which is worse than running on the "wrong" node temporarily.

## Pitfalls

- **PVC deletion blocks while pod is running.** The CSI driver won't release the volume while
  mounted. If `kubectl delete pvc` hangs, delete the pod first (or in a separate terminal) to
  unblock it.
- **zfs-localpv CSI may stall after operator changes.** If PVC provisioning fails with
  `DeadlineExceeded`, restart the `zfs-localpv-controller` deployment in `zfs-localpv` namespace.
- **Do NOT use `kubectl patch` on postgresql CRDs.** ArgoCD will revert changes instantly. All
  spec changes must go through Git commits.
- **App pods may lose database connectivity after postgres pod restart.** Radarr (and likely
  Sonarr) do not automatically reconnect when the postgres connection drops. Restart the app pod
  after the migration completes:
  ```bash
  kubectl --context=grigri delete pod <app-pod> -n <namespace>
  ```
