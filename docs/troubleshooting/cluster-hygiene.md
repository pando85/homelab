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

---

## Released Orphan PVs (Current Audit)

6 `Released` orphan PVs (~13.5 GiB of leaked ZFS space) from calypso/readest/isidoro-redis, aged
26-86 days. A consequence of `reclaimPolicy: Retain` on `openebs-zfspv`. See the cleanup procedure
above.

---

## CrashLoopBackOff Sentinels

`calypso-redis-sentinel-0` and `oauth2-proxy-redis-sentinel-2` in CrashLoopBackOff, both on prusik.
Check logs with `kubectl --context=grigri logs -n <namespace> <pod> --previous`.

---

## Canonical-Milli CPU Oddity

`apps/jellyfin` requests CPU as `2469606195200m` — a canonical-milli oddity (~2.3 GiB memory).
Harmless but it makes resource accounting hard to read.

---

## Per-Pod Disk IO Is a Blind Spot

`container_fs_reads_bytes_total` and `container_fs_writes_bytes_total` report 0 for every pod
cluster-wide, so per-pod IO is unmeasurable. IO affinity has to be inferred from PVC and hostPath
topology. Worth its own investigation; it silently weakens any IO-based analysis.

---

## Dead Makefile Targets

`metal/Makefile:76` `uninstall-longhorn` invokes `playbooks/uninstall/longhorn.yml`, which does not
exist.

`metal/Makefile:44` still passes `--limit 'arm'` although the `[arm]` inventory group was deleted in
commit `bdd75674`.

---

## Ansible Deprecation Warnings Hidden

`metal/ansible.cfg` sets `deprecation_warnings = False`, which hides ansible-core upgrade signals.
Consider enabling it during toolchain bumps.

---

## ansible-lint Was Unrunnable

`ansible-lint` was unrunnable on the Python 3.14 control node until ansible-core was bumped to 2.20
(`RuntimeError: Python 3.14 requires ansible-core version >= 2.20.0`), so lint debt was invisible.
37 findings are now surfaced. It is not wired into pre-commit and there is no Makefile lint target.

---

## Sudoers Edit Without Validation

`roles/prepare/tasks/user.yml:26-30` edits `/etc/sudoers` with `lineinfile` and no `validate=`. A
malformed line could break sudo for both sudo providers.

---

## Dead ntp_driftfile Key

`host_vars/prusik.yml` still carries a dead `ntp_driftfile` key: the galaxy role sets it via
`include_vars`, which outranks inventory host_vars, so prusik's rendered `/etc/ntp.conf` still says
`driftfile /var/lib/ntp/drift`. See
`docs/troubleshooting/ansible-ubuntu-2604-compat.md`.
