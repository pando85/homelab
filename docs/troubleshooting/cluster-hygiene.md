# Cluster Hygiene

## Released PersistentVolumes Leaking ZFS Space

### Problem

The `openebs-zfspv` and `fast-zfspv` storage classes use `reclaimPolicy: Retain`. When a PVC is
deleted (app removed, migration, test cleanup), the PV transitions to `Released` but the underlying
ZFS dataset is never destroyed. Over time this silently consumes pool space.

A 2026-10 audit found **89 orphaned volumes totalling 139.6 GiB**, the oldest created 2024-04-29 —
roughly nine months of accumulation before anyone looked.

### Root Cause

The leak is two-stage, and the second stage is invisible to a Released-PV audit:

1. PVC deleted → PV goes `Released`. Still findable by phase.
2. PV deleted → **nothing is cleaned up.** `Retain` means Kubernetes never issues the CSI
   `DeleteVolume` call, so the ZFS driver is never asked to `zfs destroy` anything. Both the ZFS
   dataset *and* its `ZFSVolume` CR survive. The CR carries no `ownerReference` (0 of 178 in the
   audit), so no garbage collection cascades from the PV either.

Stage 2 is why this went unnoticed: once the PV object is gone, `select(.status.phase == "Released")`
matches nothing, and the space is charged to a dataset named only `pvc-<uuid>`. **Auditing Released
PVs alone will report a clean cluster while 139 GiB leaks.**

### How to Audit

The reliable signal is a `ZFSVolume` CR with no matching PV — detectable entirely in-cluster, because
the CR is named identically to its PV (`pvc-<uuid>` for dynamic provisioning, the PV name for static).

```bash
# Full audit: plans only, changes nothing. Cross-references CRs against PVs, PVCs,
# ZFSBackup/Snapshot/Restore CRs, and on-disk state (mounted, snapshots, bytes).
scripts/zfs-orphan-audit.sh

# Include volumes whose only blocker is stale on-disk snapshots
scripts/zfs-orphan-audit.sh --include-snapshotted
```

The same condition is alerted on continuously by `ZFSVolumeOrphaned` in
`system/monitoring/resources/storage-prometheus-rules.yaml`, backed by the
`zfs_localpv_volume_info` metric exposed from `system/monitoring/values.yaml`.

Quick one-liner if you only need a count:

```bash
comm -23 \
  <(kubectl --context=grigri -n zfs-localpv get zfsvolume -o name | sed 's|.*/||' | sort) \
  <(kubectl --context=grigri get pv -o name | sed 's|.*/||' | sort) | wc -l
```

### How to Clean Up

```bash
scripts/zfs-orphan-audit.sh --apply                     # execute the plan it just showed
scripts/zfs-orphan-audit.sh --apply --include-snapshotted
```

Dry run is the default. Every candidate is re-verified live and downgraded to `SKIP` if it has
acquired a PV, a PVC referencing it, a ZFSBackup/Snapshot/Restore CR, an active mount, or on-disk
snapshots. Stale snapshots are a *soft* blocker requiring `--include-snapshotted`; everything else
is *hard* and is never overridden even under `--apply`. Mount status is re-checked immediately
before each `zfs destroy`.

Note that a `Released` PV whose dataset you want gone needs the PV deleted first — while the PV
exists the CR matches it and the script correctly reports no orphan.

### Prevention

**Do not flip `reclaimPolicy` to `Delete`.** Retain is currently the only backstop against an ArgoCD
prune destroying a database: only ~40% of PVCs carry `Prune=false`, and the app runs with
`prune: true` + `selfHeal: true`. Delete would convert every accidental PVC deletion into immediate,
irreversible loss — and this repo has already had one (see
`zalando-patroni-stale-dcs-deadlock.md`, where a deleted PVC's data survived only because of
Retain). The leak is a *detection* gap, not a policy error; `fast-ci-zfspv` already uses `Delete`
for genuinely ephemeral CI scratch, which is the right granularity.

Prefer, in order: the `ZFSVolumeOrphaned` alert (catches leaks at volume #1 instead of #75),
`Prune=false` on every ArgoCD-tracked PVC, and periodic `scripts/zfs-orphan-audit.sh` runs.

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
