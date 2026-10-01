# prusik Storage and Memory Architecture

Status: **partially implemented.** Analysis date 2026-09-30, updated 2026-10-01 after k8s-amd64-1 joined. Re-verify measurements before acting.

## Problem

prusik's only pool is `datasets`, RAIDZ1 across 4x 12TB SATA HDDs, carrying everything: media, the
OpenEBS volume pool (`datasets/openebs`, 2.09TB, dominated by `minio-backup`), all 13 Zalando
Postgres clusters, Forgejo, Jellyfin metadata, and the CI runner PVCs.

| Metric | HDD pool | Node NVMe |
|---|---|---|
| Read latency avg | **14-17ms** | 0.17-0.29ms |
| Read latency peak (5m windows) | **54ms** | — |
| Device busy avg / 7d peak | 34-36% / **81%** | 2.7% |

The workload is **latency-bound, not throughput-bound**. `gitea-postgres` does only 44 ops/s and
171 KB/s. Forgejo's slow path is `git-upload-pack` at 200-310ms reading git objects.

## The Root Cause Is RAM, Not Disks

This is the most important finding and it changes the order of work.

```
Sum of container limits on prusik:  210 GiB   (measured 7d, 44.7 GiB of requests)
Physical RAM:                        62 GiB
zfs_arc_max:                          5.00 GiB (pinned, metal/inventory/host_vars/prusik.yml:12-13)
MemAvailable minimum (7d):         1.97 GiB
arc_meta_used:                     3.05 GiB   (60% of the ARC is metadata)
16 GiB swap configured but unused
6 OOM kills in 7 days: ci-runner-0 x3, calypso-postgres-0, calypso-control-plane, git-forgejo
Container CPU 7d burst peak:       10.0 cores (ci-runner-0 alone: 7.68)
```

The node is overcommitted, so the ARC is pinned at 5GB. Consequences cascade:

- The hot working set is **12-14GB** — Postgres 6.8GB, Forgejo git ~2GB, Jellyfin metadata 3-5GB.
  A 5GB ARC cannot hold it, producing **154M misses per week**.
- The L2ARC is starved by the same pressure: `l2_abort_lowmem` fires ~1/sec, and its buffer headers
  consume **1.68 GiB of the 5 GiB cap (~34%)**. See `docs/troubleshooting/prusik-l2arc-ineffective.md`.
- **This already caused an outage.** CI builds drove MemAvailable to 3.9GB, triggering NVMe reclaim
  I/O that saturated the disk and crashed etcd —
  `docs/troubleshooting/velero-2026-09-25-oom-etcd-outage.md`.

RAM latency is ~0.0001ms. An SSD is ~0.1ms. An HDD is 14-54ms. **A correctly sized ARC beats any SSD
tier by roughly 1000x on the metric that matters.** Freeing RAM is the highest-value change available
and it costs nothing.

### What frees the RAM

The CI runner is the largest single consumer:

| Component | Declared limit | Actual peak |
|---|---|---|
| `runner` container | 18Gi | **11.2GB** (idle 100-300MB, 5-14GB during jobs, max observed 14.4GB) |
| `daemon` (docker:dind) | 8Gi | ~1GB |
| `/workspace` tmpfs | 8Gi | 1.8GB used (counts against node RAM) |

Moving CI off prusik reclaims **~19GB**, but CI cannot move to a 16 GiB node (see below). The
realistic path is spreading stateless pods first:

```
62 GiB total (measured)
- 210 GiB container limits (44.7 GiB requests) — massive overcommit
- 10.5 GiB p95 stateless pod spread to k8s-amd64-1
- ~19 GiB CI peak remains on prusik until node has 32 GiB RAM
= zfs_arc_max can rise from 5 GiB to ~8-10 GiB, still short of 12-14 GiB hot set
```

At 8-10 GiB the ARC holds more of the hot set but not all of it. `arc_evict_not_enough` is currently
only 0.018/sec, so the ARC is constrained rather than thrashing — even a partial increase helps, but
the full fix requires either moving CI (needs 32 GiB in k8s-amd64-1) or right-sizing the 210 GiB of
limits.

**This likely eliminates the database-latency problem without any new disks.**

## The L2ARC Should Be Fixed, Not Removed

Correcting an earlier conclusion: the L2ARC is load-bearing for Jellyfin UX.

Average L2ARC hit size is **17-28KB** — small random reads, not video blocks. `config-jellyfin` reads
1.42TB per 7d, more than either media dataset, and contains no video at all (media is HostPath
read-only; transcodes are `emptyDir`). It is 1,511 library metadata entries, 521 People dirs, a 218MB
SQLite DB, and 196 subtitle dirs.

The defect is `l2arc_noprefetch=0`, which devotes **462GB (41%) of the device to one-shot sequential
video prefetch**. Setting `l2arc_noprefetch=1` frees that for demand data, growing the useful
MRU+MFU population from 687GB toward ~1.05TB. Playback is unaffected — a 4K remux is ~12MB/s against
~400-500MB/s of RAIDZ1 sequential throughput.

## Where the SSDs Fit

Reassessed after the above. **Do the RAM and L2ARC work first and re-measure** — the SSD tier may
shrink to nothing, or to a narrow role.

### Available hardware

| Device | Slot | Character |
|---|---|---|
| 2x SanDisk Ultra II `SDSSDHII` 256GB | SATA6G_5/6 (both free) | 2014, 19nm planar **TLC**, Marvell 88SS9187, **has DRAM**, nCache 2.0 SLC buffer |
| FIKWOT FN960 2TB NVMe | M.2_1 | **Stay as L2ARC** |
| (spare) | M.2_3, PCIe 4.0 x4 chipset lanes | future expansion |

Do **not** populate PCIEX1 — it disables SATA6G_3/4, taking available SATA ports to 0
(`docs/hardware/prusik.md:75`).

### Ultra II limits that constrain their use

SanDisk never published TBW for this model; community estimate **~60-80 TBW ≈ 0.22 DWPD**. It has a
real DRAM cache (~98K random read IOPS QD32) and an nCache SLC write buffer of ~4-6GB.

**The critical property is a sustained-write cliff**: once nCache fills, sequential write drops from
~500 MB/s to **~150-200 MB/s** with folding stalls. Docker image layer extraction is exactly that
workload. These drives are therefore **unsuitable for CI** and suitable for read-heavy, low-write data.

They are also ~12 years old, and aged TLC tends to fail suddenly rather than degrade gradually.
**Verify wear before committing:**

```bash
smartctl -A /dev/sdX | grep -Ei 'Reallocated|Wear_Leveling|Total_LBAs_Written|Uncorrect|Reserve_Nand'
# Total_LBAs_Written raw × 512 / 1e12 = host TB written
```

No standard percentage-used attribute exists on this controller. Above ~30-40TB written, treat as
high-risk for write duty; any non-zero `Reallocated_Sector_Ct` disqualifies it for durable data.

### Remaining justification for an SSD tier

After a 20GB ARC, what still *cannot* live in RAM:

| Dataset | Size | Why ARC won't hold it |
|---|---|---|
| Forgejo `packages/` (OCI registry) | ~32GB | Exceeds ARC, and CI image pulls read it in bursts — **can evict the hot set** |
| `minio-backup` | 2.09TB | Velero streams it; massive sequential ARC churn |
| Media (`peliculas`, `series`) | TB-scale | Sequential, doesn't need low latency |

Two cheaper mitigations to try before buying/migrating anything:

- **`primarycache=metadata` on the backup dataset** so Velero runs stop evicting the hot set.
- **Split Forgejo package storage** onto its own dataset (Forgejo supports `[storage.packages]` with
  a separate path) so a cold 32GB blob store can't thrash ARC holding hot DB and git data.

Only if those are insufficient does a mirrored `fast` pool for `packages/` become worthwhile. 238GB
against ~32GB of data leaves ample headroom.

### Mirror, not stripe — and why "avoid RAID for writes" doesn't apply here

A **ZFS mirror has no write penalty.** The RAIDZ partial-stripe write amplification that motivates
avoiding RAID does not apply to a 2-way mirror: writes go to both legs in parallel at single-disk
speed. Only capacity halves. Reads scale 2x, which is what a random-read-latency problem needs.

Where genuinely disposable, write-heavy data needs a home, the right answer is a **single-device pool
with `sync=disabled`** on an NVMe — not an unmirrored pair of 2014 SATA TLC drives.

### Dataset properties

| Dataset | Properties | Rationale |
|---|---|---|
| `fast/db` | `recordsize=8K`, `atime=off` | Postgres uses 8K pages; the 128K default causes read-modify-write amplification |
| `fast/ci` (if used) | `sync=disabled`, `atime=off` | Disposable data — the "don't care if it breaks" semantics belong in a dataset property, not in dropping redundancy |

Keep `fstype: zfs` (datasets, not zvols) to match the existing StorageClass and avoid double-CoW.
See `docs/troubleshooting/openebs-zfspv-slow-startup-fsgroup.md` before setting `fsGroup`.

## Workload Sizing (measured)

| Workload | Actual | Character |
|---|---|---|
| `ci-runner-docker` (dind data-root) | 32.4 G | sustained write churn; docker-gc prunes at 80-95% |
| `ci-runner-cache` | 11 G | 7-day retention |
| `/workspace` tmpfs | 1.8 G of 8Gi | small-file churn, in RAM |
| Forgejo `data-gitea-zfs-0` | 33.5 G (37.1G phys) | `packages/` OCI ≈32G dominates |
| All 13 Postgres clusters | **6.8 G combined** | random read, minimal write |

Forgejo breakdown: `packages/` ~32G (257 content-addressable SHA256 blob dirs), `actions_log/` ~1.2G
(13,923 files), `actions_artifacts/` 734MB, `git/` 1.1G, `attachments/` 66MB.
**There is no `lfs/` directory — LFS is not configured.** Growth ≈1-1.5 GB/month; 12-month projection
37-42G. Nothing prunes Actions logs/artifacts, which is a larger growth vector than the databases.

ZFS compression on this data is ~1.06x — OCI blobs and Postgres pages do not compress. Size pools on
physical bytes.

## Adding the PN51-E1 Node (k8s-amd64-1)

ASUS PN51-E1, Ryzen 5 5500U (6C/12T, ~15 W), **16 GiB RAM** (14.52 GiB visible), 119 GiB SSD with
the root LV already fully expanded (102 G free), Ubuntu 26.04.1, kernel 7.0.0-34, IP
192.168.192.11. Joined as an **untainted** amd64 worker. See
`planning/k8s-amd64-1-node-addition.md` for the full plan and
`docs/troubleshooting/ansible-ubuntu-2604-compat.md` for the 26.04 Ansible work.

### Decision: untainted, not taint-restricted

The original plan prescribed `ephemeral=true:NoSchedule`. We deliberately did **not** taint it, so
the 44 stateless workloads can spread freely. Consequence: `allowedTopologies` in
`system/zfs-localpv/templates/storage-class-openebs-zfspv.yaml:13-18` (grigri + prusik only) is now
the *sole* protection against durable PVCs binding there. With `WaitForFirstConsumer` a PVC-backed
pod goes `Pending` rather than landing on the node, which is a safe failure mode — but a PVC with
no `storageClassName` silently inherits the default class and hangs forever instead of failing
loudly.

### The CI runner cannot move to 16 GiB

`ci-runner-0` measures 0.84 GiB avg / 1.95 GiB p95 but **peaks at 17.4 GiB RSS and 7.68 cores**,
and was OOM-killed 3 times in 7 days on prusik. Its two 50 GiB PVCs
(`platform/ci-runners/resources/pvc-runner-cache.yaml:12`, `pvc-runner-docker.yaml:14`) omit
`storageClassName` so they are node-bound to ZFS. Moving CI needs **2x16 GiB SODIMM (32 GiB) in
the node**, plus converting those PVCs to a local class. Measured CI disk usage is ~32.4 G dind +
~11 G cache, which does fit in 102 G.

### No local StorageClass exists

There is no local-path-provisioner, longhorn, or openebs localpv-hostpath anywhere in the repo.
k3s' bundled local-storage is disabled server-side (`metal/roles/k3s/defaults/main.yml:8-12`) so it
cannot be re-enabled per node. A new `system/local-path/` chart is required; the ApplicationSet at
`bootstrap/root/templates/stack.yaml:15-22` auto-discovers any immediate child directory of
`system/` (namespace == dirname), and it must **not** carry `is-default-class` because
`storage-class-openebs-zfspv.yaml:6` owns that.

### `zfsNode` exclusion mechanism

`system/zfs-localpv/values.yaml:10-11` selects the node DaemonSet on `kubernetes.io/arch: amd64`, so
it schedules onto a pool-less host and CrashLoops. The upstream chart renders only `nodeSelector`
and `tolerations` (`zfs-node.yaml:164-167,172-175`) — there is no affinity key — so exclusion
requires a new label such as `storage.zfspv=true` applied to grigri and prusik via `node_labels` in
their host_vars and the `node-labels` play. **Ordering matters:** the labels must exist before the
selector is flipped, or `zfsNode` loses every eligible node and PVC mounts break cluster-wide.

### DNS search-domain blocker

The new node did not receive the `grigri` DHCP search domain that prusik and grigri get.
`roles/k3s/templates/config.yaml.j2` builds `server: https://prusik:6443` from `ansible_hostname`,
so short-name resolution is a join prerequisite. Handled by `prepare_dns_search_domains`.

### Guardrails still in force

- **Do not add the node to `openebs-zfspv` `allowedTopologies`.** This is the strongest control
  available: without it, no durable PVC can be provisioned there.
- Treat node loss as a non-event: anything running there must restart cleanly elsewhere.
- A `reclaimPolicy: Delete` local class is the right fit for this node's ephemeral disk.

## Implementation Gotchas

### A second StorageClass is required for any new pool

`storage-class-openebs-zfspv.yaml:11` hardcodes `poolname: "datasets/openebs"`, and `storageClassName`
on an existing PVC is **immutable**. Migration means create-new, copy, switch — never edit in place.

- **`allowedTopologies` must be prusik only** for a prusik-local pool. With
  `volumeBindingMode: WaitForFirstConsumer`, a wrong topology means the PVC silently never binds.
- **Must not carry `is-default-class: "true"`** (currently on the existing class, line 6).
- **`reclaimPolicy: Retain`** leaks released PVs and ZFS space on every deleted PVC — audit per
  `docs/troubleshooting/cluster-hygiene.md`.

### Backup follows labels, not pools — so labels are the single point of failure

Verified: the backup stack is label-driven and follows a PVC into a new pool or onto a new node.

- **Snapscheduler** selects via `claimSelector.matchLabels.backup: <name>-zfs` using the
  `zfspv-snapclass` VolumeSnapshotClass — pool-agnostic.
- **Velero** schedules filter on PVC label `backup/retain: weekly|quaterly`;
  `openebs/velero-plugin:3.6.0` resolves PV → CSI volumeHandle → ZFSVolume CR and creates `ZFSBackup`
  CRs per volume.

**The #1 silent-failure risk is recreating a PVC without its labels.** Both systems skip unlabelled
PVCs with no error and no warning, and there is no pool-level filter to catch the omission. Preserve
`backup/retain`, `backup`, and `app.kubernetes.io/*` on every migrated PVC, and confirm a snapshot
and a Velero backup actually complete before calling the migration done.

Also note the MinIO backup target (`minio-backup`, 2.09TB) lives on `datasets/openebs`, so a pool
failure takes the backups with it. Pre-existing, unchanged by this work; the weekly rclone cross-backup
to external S3 (`platform/velero/templates/cross-backup/cronjob.yaml:26`) is the real off-box copy.
`metal/inventory/group_vars/all/backup.yml:1` hardcodes `backup_target_dir: /datasets/backups`.

### Snapshot space on a small pool

A 238GB pool has far less slack than the 7.2TB `datasets` pool. See
`docs/troubleshooting/zfs-snapshot-filling-pvc.md`. Tighten retention on `fast/db`; disable snapshots
on CI scratch.

## Realistic Payoff of Stateless-Only Moves

With CI stuck on prusik (needs 32 GiB in the new node), the realistic path is spreading the 44
stateless pods to k8s-amd64-1. Measured: **9.5 GiB avg / 10.5 GiB p95 across 62 pods, 0.24 cores
of CPU combined** — enough to lift `zfs_arc_max` from 5 GiB to roughly 8-10 GiB, still short of the
12-14 GiB hot set.

Ranked by measured RAM at ~0 CPU: gitlab-webservice 3.28→3.60 GiB, gitlab-sidekiq 1.67→1.70 GiB,
keycloak-1 x2 (0.71 + 0.66), keycloak-operator 0.37, rsshub 0.23, argocd repo-server/server/appset/
notifications/redis ~0.20 total, readest-client 0.17, kroki 0.13, searxng 0.11, calypso stateless
pair ~0.6. Also `apps/esphome` uses 0.11 GiB while carrying an **8 GiB memory + 10 CPU limit**, and
the two gitlab pods mount **41 unbounded `emptyDir: Memory` volumes** — a live OOM amplifier.

Rebalancing must be done by adding `topologySpreadConstraints` and letting ArgoCD roll pods out;
`kubectl delete pod` and `rollout restart` are both forbidden and futile under `selfHeal: true`.

### Per-pod disk IO is not measurable

`container_fs_reads_bytes_total` and `container_fs_writes_bytes_total` exist but report 0 for every
pod cluster-wide, so IO affinity has to be inferred from PVC and hostPath topology. Any future
analysis in this doc that claims per-pod IO should be treated as suspect. See
`docs/troubleshooting/cluster-hygiene.md`.

### Disk IO confirmation (7d)

`sda`-`sdd` at 28-30% io_time with ~0 GiB/h throughput, confirming latency-bound small IO.

## Recommended Order of Work

1. **`l2arc_noprefetch=1`** — one line in `metal/roles/setup/templates/zfs.conf.j2`, reversible.
   Re-measure over 7d: `l2_prefetch_asize` should fall toward zero while `l2_hits` holds or rises.
2. **Spread stateless pods to k8s-amd64-1** via `topologySpreadConstraints`. Reclaims ~10 GiB p95,
   enough to raise `zfs_arc_max` to 8-10 GiB.
3. **Raise `zfs_arc_max` to 8-10 GiB** (not 20-24 as originally hoped). Re-measure Forgejo
   `git-upload-pack` latency and Postgres. Partial relief, not a full fix.
4. **`primarycache=metadata` on the backup dataset**, and split Forgejo `packages/` to its own
   dataset, to stop bulk traffic evicting the enlarged hot set.
5. **Re-measure, then decide on SSDs.** Install and SMART-check the Ultra IIs; use them only for what
   still cannot live in ARC.
6. **Right-size container limits** — 210 GiB of limits on 62 GiB is what forced the 5GB ARC cap in
   the first place and will re-constrain it if left alone.
7. **CI move requires 32 GiB in k8s-amd64-1** (2x16 GiB SODIMM) plus converting its PVCs to a local
   StorageClass. Only then does the full ~19 GiB reclaim become available.

## Open Questions

- SMART wear state of both Ultra IIs — gates any use of them for durable data.
- Whether `/workspace` should stay partly in RAM once CI has fast local NVMe.
- Retention policy for Forgejo Actions logs/artifacts (~2GB, unpruned, growing).
- Why `container_fs_reads_bytes_total` / `container_fs_writes_bytes_total` report 0 for every pod.
