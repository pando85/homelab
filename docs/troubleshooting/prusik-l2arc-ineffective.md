# prusik L2ARC is misconfigured, not useless

Measured 2026-09-30 over 7d and 30d windows. Prometheus: `instance="prusik"`.

**Correction:** an earlier version of this doc concluded the L2ARC was net-negative and should be
removed. That was wrong. It reasoned from the low hit ratio (34-39%) and the small share of total
reads served (1.4%) without checking *what* the L2ARC was caching. The average hit size proves it
is serving the most latency-sensitive traffic on the node. **Do not remove the L2ARC.**

## Problem

The 2TB FIKWOT FN960 NVMe (`nvme0n1`, M.2_1) is the L2ARC cache vdev for the `datasets` RAIDZ1 pool.
Symptoms that looked like L2ARC failure:

- Hit ratio only **34-39%**
- `l2_abort_lowmem` firing **~0.6-1.0/sec** (365K-589K events per 7d)
- `l2_hdr_size` **1.68 GiB** against a 5 GiB ARC cap — **~34%** of the ARC spent on L2ARC metadata
- HDD read latency still 14-17ms average, 54ms peak, with Forgejo `git-upload-pack` at 200-310ms

## Root Cause

The L2ARC is genuinely valuable and simultaneously badly starved. Three distinct issues.

### 1. Why the low hit ratio is misleading

The ratio is computed over the population that *already missed the ARC* — 3.7% of all reads. It is
not a measure of usefulness.

```promql
sum(increase(node_zfs_arc_hits{instance="prusik"}[7d]))       # 4.94B  (96.3% of all reads)
sum(increase(node_zfs_arc_l2_hits{instance="prusik"}[7d]))    #  71.3M
sum(increase(node_zfs_arc_l2_misses{instance="prusik"}[7d]))  # 118.8M
```

71.3M reads per week rescued from 14-54ms HDD latency down to 0.29ms. That is 1.4% of total volume
and close to 100% of the *painful* reads.

### 2. What it is actually caching — the load-bearing number

```promql
increase(node_zfs_arc_l2_read_bytes{instance="prusik"}[7d])
  / increase(node_zfs_arc_l2_hits{instance="prusik"}[7d])
```

**≈17-28KB per hit.** Video streaming reads 128KB-1MB blocks. Small hits mean metadata, thumbnails,
SQLite pages, git objects, and Postgres blocks — exactly the latency-critical population.

Corroborated by `config-jellyfin` reading **1.42TB in 7d, more than either media dataset**. That PVC
holds no video: media is mounted HostPath read-only from `datasets/peliculas` and `datasets/series`,
and `transcodes/` is an `emptyDir` that never touches ZFS. The 1.42TB is entirely UI traffic:

| Path | Contents |
|---|---|
| `metadata/library/` | 1,511 entries — posters, backdrops, logos |
| `metadata/People/` | 521 dirs — actor images and metadata |
| `data/jellyfin.db` | 218MB SQLite + WAL/SHM |
| `data/subtitles/` | 196 cached subtitle dirs |
| `cache/images/` | 73MB |

Demand-read hit ratios are healthy — **demand data 93.7%** (2.31B hits / 154M misses),
**demand metadata 98.1%** (18.44B / 356M). The L2ARC feeds application reads, not speculation.

### 3. The real defect: `l2arc_noprefetch=0`

`metal/roles/setup/templates/zfs.conf.j2` sets `l2arc_noprefetch=0`, caching one-shot sequential
prefetch. Jellyfin video playback reads each block exactly once, so this occupies the device with
data that will never be hit again, evicting the small random blocks that matter.

| Bucket | Size | Share |
|---|---|---|
| Prefetch | **462GB** | 41% — one-shot video, near-zero reuse |
| MRU | 444GB | 39% — demand |
| MFU | 243GB | 21% — demand |
| **Useful (MRU+MFU)** | **687GB of 1.13TB** | 59% |

Prefetch hit ratio is **20.7%** (11.3M hits / 54.4M), consistent with read-once streaming.

### 4. The upstream cause: the ARC is too small

Every block cached on the L2ARC device needs an index header (`l2arc_buf_hdr_t`, ~175 bytes) that
lives in **ARC RAM**, not on the NVMe. That is what `node_zfs_arc_l2_hdr_size` measures — the RAM
cost of *describing* the cache, as opposed to `node_zfs_arc_l2_size`, which is the data on the device.
On prusik the ratio is ~0.14%: 1.68 GiB of headers indexing 1227 GiB of cached data.

That sounds negligible until you compare it against the right denominator. The headers are charged
to a **5 GiB ARC cap**, so they consume **~34%** of it. `l2_abort_lowmem` at ~1/sec is the same
pressure showing up differently: ZFS drops L2ARC writes when the ARC cannot afford them, so the
device cannot retain what it caches. Both are symptoms of an ARC capped at 5 GiB on a node whose
container limits sum to ~85GB against 64GB of RAM.

The header tax is *proportional to the ARC cap*, not to the L2ARC — at a 20 GiB ARC the same 1.68 GiB
is 8% instead of 34%. This is the main reason growing the ARC also fixes the L2ARC.

**Grafana caveat:** the `L2 ARC size` panel (id 365) in `system/zfs-exporter/dashboards/zfs.json`
plots `actual` (`l2_size`) and `hdr` (`l2_hdr_size`) as two series on the **same bytes axis**. At
1227 GiB versus 1.68 GiB that is a 730:1 ratio, so `hdr` renders as a flat line at zero and the
problem is invisible on the dashboard. The two series also measure different resources with very
different scarcity — device space (60% of 2TB, harmless) versus ARC RAM (34% of 5 GiB, severe).
Always evaluate headers as a fraction of `arc_c_max`, never against `l2_size`:

```promql
node_zfs_arc_l2_hdr_size / node_zfs_arc_c_max * 100   # prusik ~34%, grigri ~4%
```

**Fixing the ARC is what makes the L2ARC work.** See `docs/conventions/prusik-fast-storage-tier.md`.

## How to Diagnose

```promql
# Average cached-read size — small (<64KB) means metadata/random, large means streaming
increase(node_zfs_arc_l2_read_bytes{instance="prusik"}[7d])
  / increase(node_zfs_arc_l2_hits{instance="prusik"}[7d])

# How much of the device is wasted on one-shot prefetch?
node_zfs_arc_l2_prefetch_asize{instance="prusik"} / node_zfs_arc_l2_asize{instance="prusik"}

# Is the ARC too small to sustain the L2ARC?
rate(node_zfs_arc_l2_abort_lowmem{instance="prusik"}[1h])
node_zfs_arc_l2_hdr_size{instance="prusik"} / node_zfs_arc_c_max{instance="prusik"}

# Is the app's own read path being served? (the ratio that matters)
node_zfs_arc_demand_data_hits
  / (node_zfs_arc_demand_data_hits + node_zfs_arc_demand_data_misses)
```

Device health was clean: `l2_cksum_bad` 0, `l2_io_error` 0, `l2_evict_reading` 0,
`l2_evict_lock_retry` 0. The NVMe is not the problem.

## Fix

**Step 1 — `l2arc_noprefetch=1`** (the ZFS default) in `metal/roles/setup/templates/zfs.conf.j2`.
One-line, reversible.

Expected: 462GB released from one-shot video, letting demand data grow from 687GB toward the full
~1.05TB (+53% useful cache). Jellyfin library browsing, search, and poster loading should get
snappier; Forgejo git reads and Postgres also benefit. **Video playback is unaffected** — a 4K remux
is ~12MB/s against ~400-500MB/s of RAIDZ1 sequential throughput, and transcodes already bypass ZFS.

Verify by watching `l2_prefetch_asize` fall toward zero while `l2_hits` holds steady or rises.

**Baseline captured 2026-09-30 (prusik, 7d), for comparison:**

| Metric | Before | Target after |
|---|---|---|
| `l2_prefetch_asize` / `l2_asize` | 462GB / 1.13TB = **41%** | **< 5%** |
| `l2_mru_asize` + `l2_mfu_asize` | 444 + 243 = **687GB** | **≥ 900GB** |
| `l2_hits` per day | ~10.2M | **≥ baseline** (must not fall) |
| Avg L2ARC hit size | 17-28KB | ~unchanged |
| `l2_abort_lowmem` | ~0.6-1.0/sec | unchanged — needs ARC growth, not this |

L2ARC is a circular log: the 462GB already resident is **overwritten gradually, not evicted
immediately**. At `l2arc_write_max=300MB/s` a full 1.05TB cycle is ~1h in theory, but real write rate
tracks actual miss volume. **Allow 24-48h before judging, and compare 7d windows.**

The change also affects **grigri**, which has its own L2ARC (`l2arc_write_max_mb: 120`) and an 8GB ARC
cap on 32GB RAM. Roll out to prusik first, measure, then extend.

Revert per host with `l2arc_noprefetch: 0` in `metal/inventory/host_vars/<host>.yml` and re-running
the `zfs-config` tag — no template edit needed.

**Step 2 — grow the ARC** (the real fix). Requires freeing RAM on prusik first; moving the CI runner
off the node reclaims ~19GB and supports `zfs_arc_max` of 20-24GB. This largely stops
`l2_abort_lowmem`, shrinks the header tax to ~8%, and puts the whole 12-14GB hot set in RAM.

**Do not remove the cache vdev.** Without it every poster, thumbnail, and SQLite page read falls to
14-54ms HDD latency.

**Do not raise `zfs_arc_max` before freeing RAM.** Available memory hit a 2.15-3.5GB minimum during
CI builds, and that pressure already caused a control-plane outage — see
`docs/troubleshooting/velero-2026-09-25-oom-etcd-outage.md`. Raising the cap without relieving
container pressure risks repeating it on the sole control-plane node.
