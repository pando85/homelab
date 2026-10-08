# Topology Spread Constraints

Guidelines for configuring `topologySpreadConstraints` to spread pods across nodes.

## When to use `matchLabelKeys`

For **Deployments with replicas > 1** (static or HPA), always include `matchLabelKeys: [pod-template-hash]`. This scopes the skew calculation to the current ReplicaSet, preventing rolling updates from stalling.

### Why

Without `matchLabelKeys`, the scheduler counts both old and new ReplicaSet pods together. During a rolling update with `maxSkew: 1` and `whenUnsatisfiable: DoNotSchedule`:

```
Node A: old-pod, old-pod  →  count = 2
Node B: old-pod            →  count = 1
Node C: (empty)            →  count = 0, skew = 2 > maxSkew → BLOCKED
```

With `matchLabelKeys: [pod-template-hash]`, new pods only see other new pods for skew calculation, so they can land on nodes that still have old pods.

### Pattern

```yaml
topologySpreadConstraints:
  - maxSkew: 1
    topologyKey: kubernetes.io/hostname
    whenUnsatisfiable: DoNotSchedule
    matchLabelKeys:
      - pod-template-hash    # scope to current ReplicaSet
    labelSelector:
      matchLabels:
        app.kubernetes.io/instance: my-app
        app.kubernetes.io/name: my-app
```

## When to skip `matchLabelKeys`

- **Single-replica Deployments** (`replicas: 1`, no HPA): No rolling update overlap, no benefit.
- **StatefulSets**: No `pod-template-hash` label exists (StatefulSets use controller-revision-hash).
- **CRD-managed workloads**: Only if the CRD controller supports `matchLabelKeys`.

## Current configuration

| File | Replicas | matchLabelKeys | Notes |
|------|----------|----------------|-------|
| `apps/m-rajoy-front/values.yaml` | 2 | ✅ | Static multi-replica |
| `apps/telegram-bot/values.yaml` | 2 | ✅ | Static multi-replica |
| `system/ingress-nginx/values.yaml` | 2, HPA 2-11 | ✅ | Critical path |
| `system/ingress-nginx-external/values.yaml` | 2, HPA 2-11 | ✅ | Critical path |
| `apps/kroki/values.yaml` | 1 | ❌ | Single replica |
| `apps/http-echo/values.yaml` | 1 | ❌ | Single replica |
| `apps/special-web/values.yaml` | 1 | ❌ | Single replica |
| `apps/wallabag/values.yaml` | 1 | ❌ | HA not supported |
| `apps/antdroid/values.yaml` | 1 | ❌ | Single replica |
| `apps/m-rajoy-api/values.yaml` | 1 | ❌ | Single replica |
| `system/monitoring` (Alertmanager) | 2 | ❌ | StatefulSet |
| `system/kanidm` | CRD | ❌ | Kaniop CRD |

## Checklist for new deployments

1. **Determine replica count**: Will this have `replicas > 1` or HPA?
2. **If yes**: Add `matchLabelKeys: [pod-template-hash]` to avoid rollout stalls.
3. **If StatefulSet**: Skip `matchLabelKeys` (no `pod-template-hash`).
4. **Choose `maxSkew`**: Use `1` for strict spreading, `4+` for HPA-scaled workloads.
5. **Choose `topologyKey`**: Usually `kubernetes.io/hostname` for node spreading.
