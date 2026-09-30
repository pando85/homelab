# prusik Storage and Memory Architecture

Status: **planned, not implemented.** Analysis date 2026-09-30. Re-verify measurements before acting.

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
Sum of container limits on prusik:  ~85 GB
Physical RAM:                        64 GB
zfs_arc_max:                          5 GB   (metal/inventory/host_vars/prusik.yml:12-13)
MemAvailable minimum (24h):       2.15-3.5 GB
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

Moving CI off prusik reclaims **~19GB**, supporting:

```
64GB total
- 22GB  non-CI container actual peak
-  8GB  remaining tmpfs/shared
-  3GB  OS / kubelet / containerd
-  4GB  safety margin (keep MemAvailable > 4GB)
= 27GB → set zfs_arc_max to 20-24GB
```

At 20GB the **entire 12-14GB hot set becomes RAM-resident**. `arc_evict_not_enough` is currently only
0.018/sec, so the ARC is constrained rather than thrashing — a 4x increase should convert nearly all
154M weekly misses into hits.

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

## Adding the PN51-E1 Node

ASUS PN51-E1, Ryzen 5 5500U (6C/12T Zen2, 15-25W, Vega 7 iGPU), amd64 — **matches the existing
cluster arch**, so no cross-arch image problems. 2x SODIMM, up to 64GB DDR4-3200. Storage:
**1x M.2 NVMe (PCIe 3.0 x4) plus one shared M.2-SATA *or* 2.5" SATA bay** — confirm the installed
configuration, but assume **a single usable data disk and no ZFS redundancy**.

### The right role: an explicitly ephemeral compute node

The single-disk limitation is not a drawback if the node is reserved for workloads whose state is
reproducible from git or re-runnable. CI is the ideal tenant.

**Guardrails — these are what make a no-backup node safe:**

- **Do not add the node to `openebs-zfspv` `allowedTopologies`.** This is the strongest control
  available: without it, no durable PVC can be provisioned there, so stateful pods simply cannot
  schedule onto it. `system/zfs-localpv/templates/storage-class-openebs-zfspv.yaml` currently lists
  `grigri` and `prusik`; leave it that way.
- **Taint it** (e.g. `ephemeral=true:NoSchedule`) so workloads opt in deliberately rather than
  drifting there because the scheduler sees free CPU.
- Give CI a **local NVMe** path via hostPath or a node-local StorageClass, never the shared class.
- Treat node loss as a non-event: anything running there must restart cleanly elsewhere.

### Good candidates

1. **CI runners** — `forgejo-runner` + `docker:dind`. Memory-hungry, I/O-hungry, disposable. Solves
   both the prusik RAM problem and the 8Gi `/workspace` ceiling at once.
2. **Pull-through registry cache** — absorbs CI image pulls locally so they stop reading Forgejo's
   32GB `packages/` over the network and evicting prusik's ARC. Directly protects the hot set.
3. **Renovate and scheduled CronJobs** — disposable, already containerised.
4. **Restore/backup verification** — spin up copies of production data to test restores without
   touching prod. Genuinely valuable and safe, given how many restore procedures this repo documents.
5. **Staging / `test/` k3d workloads.**

### Poor candidates

- **All durable state**: the 13 Postgres clusters, Vault, Kanidm, MinIO, Forgejo, Immich, Home
  Assistant, Grafana, Prometheus/Loki/Tempo.
- **qBittorrent** — its 186 IOPS come from HostPath mounts into `datasets/{series,peliculas}` on
  prusik. Moving it would require network storage and would relocate the contention, not remove it.
- **Small stateless infrastructure** (external-dns, cert-manager, ingress) — negligible footprint, so
  little RAM is recovered, while blast radius grows if the node is less reliable than prusik.

### Versus just using grigri

grigri (8C, 32GB, ~11.8GB used, ~30 pods) has roughly 20GB free, so it looks tempting and requires no
new hardware. **It is tighter than it appears**: a CI peak of 11.2GB plus the 8GB workspace tmpfs is
~19GB, leaving ~1GB before grigri's own ZFS ARC. It also needs the `ci-runner-docker` and
`ci-runner-cache` PVCs migrated to grigri's local pool, since openebs-zfspv is node-local.

The PN51-E1 *adds* 6 cores and up to 64GB of new capacity instead of reshuffling existing pressure,
and it draws 15-25W. Prefer it if the hardware is to hand; grigri is the lower-effort fallback but
leave real headroom.

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

## Recommended Order of Work

1. **`l2arc_noprefetch=1`** — one line in `metal/roles/setup/templates/zfs.conf.j2`, reversible.
   Re-measure over 7d: `l2_prefetch_asize` should fall toward zero while `l2_hits` holds or rises.
2. **Move CI to the PN51-E1** (or grigri). This reclaims ~19GB on prusik and removes the memory
   spikes that caused the September etcd outage.
3. **Raise `zfs_arc_max` to 20-24GB.** Re-measure Forgejo `git-upload-pack` latency and Postgres.
   **Expect this alone to resolve most of the reported latency.**
4. **`primarycache=metadata` on the backup dataset**, and split Forgejo `packages/` to its own
   dataset, to stop bulk traffic evicting the enlarged hot set.
5. **Re-measure, then decide on SSDs.** Install and SMART-check the Ultra IIs; use them only for what
   still cannot live in ARC.
6. **Right-size container limits** — 85GB of limits on 64GB is what forced the 5GB ARC cap in the
   first place and will re-constrain it if left alone.

## Open Questions

- SMART wear state of both Ultra IIs — gates any use of them for durable data.
- PN51-E1 actual installed RAM and disk configuration.
- Whether `/workspace` should stay partly in RAM once CI has fast local NVMe.
- Retention policy for Forgejo Actions logs/artifacts (~2GB, unpruned, growing).
- Adding the node to `metal/`: `hosts.ini` `[amd64_node]`, a new `host_vars/<name>.yml` with
  `kube-reserved` / `system-reserved` / `eviction-hard` and its own `zfs_arc_max_gb`. It inherits
  `group_vars/amd64.yml`.
