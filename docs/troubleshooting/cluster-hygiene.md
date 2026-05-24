# Cluster Hygiene

## Released PersistentVolumes Leaking ZFS Space

### Problem

The `openebs-zfspv` storage class uses `reclaimPolicy: Retain`. When PVCs are deleted (app removed,
migration, test cleanup), the PV transitions to `Released` phase but the underlying ZFS dataset is
never destroyed. Over time this silently consumes pool space.

### How to Audit

```bash
# List all released PVs with app info
kubectl --context=grigri get pv -o json | \
  jq -r '.items[] | select(.status.phase == "Released") |
    "\(.metadata.name) \(.spec.capacity.storage) \(.spec.claimRef.namespace)/\(.spec.claimRef.name)"'

# Count total leaked space
kubectl --context=grigri get pv -o json | \
  jq '[.items[] | select(.status.phase == "Released") |
    .spec.capacity.storage | rtrimstr("Gi") | tonumber] | add'
```

### How to Clean Up

```bash
# Delete all released PVs
kubectl --context=grigri get pv -o json | \
  jq -r '.items[] | select(.status.phase == "Released") | .metadata.name' | \
  xargs -I{} kubectl --context=grigri delete pv {}
```

Verify underlying ZFS datasets were destroyed by the ZFS-localPV controller after PV deletion.

### Prevention

Consider changing the storage class `reclaimPolicy` to `Delete` for non-critical workloads, or
creating a periodic CronJob that cleans up released PVs older than N days.

---

## Interpreting High Restart Counts

### Problem

A pod showing hundreds of restarts looks alarming but may be completely normal.

### How to Diagnose

```bash
# Check the last termination reason
kubectl --context=grigri describe pod <pod> -n <namespace> | grep -A5 "Last State:"
```

### Common Patterns

| Last State Reason | Exit Code | Meaning |
|---|---|---|
| `Unknown` | 255 | Node reboot — kubelet SIGKILL'd the pod. Normal for DaemonSets and StatefulSets on node restarts. |
| `Error` | 1 | Application crash — investigate logs with `--previous`. |
| `OOMKilled` | 137 | Out of memory — increase memory limits. |
| `Completed` | 0 | Init container finished normally. |

### Key Points

- **Exit code 255** from `Reason: Unknown` is a node-level kill (reboot, eviction, etc.), not an
  application bug. Accumulated restarts over months from node reboots are expected.
- Always check **when** the last restart happened. A restart 30 days ago is stale information.
- **DaemonSets** (kured, zfs-localpv-node, nfd-worker, smartctl-exporter) restart on every node
  reboot by design.
- Restart counts reset when pods are recreated (deployment rollout, manual delete).
