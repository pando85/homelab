# qBittorrent HDD I/O Saturation

## Problem

Forgejo (git.grigri.cloud) shows intermittent latency spikes — git operations take 1-2 seconds,
runner `UpdateTask` calls spike to 100-300ms, and `pre-receive` hooks exceed 1.5s.

HDD disks on `prusik` show queue depth 8-11 (saturated at ~240 IOPS each), with only moderate
throughput (~8 MB/s per disk).

## Root Cause

qBittorrent uses **HostPath volumes** mounted directly to HDD datasets
(`/datasets/{series,peliculas,musica}/download`). Seeding torrents read small pieces (256KB-4MB)
from random locations across the disk. This random I/O pattern saturates HDDs at their ~240 IOPS
ceiling.

**L2ARC does not help with writes** — it's a read cache only. For reads, L2ARC has a 40% hit ratio
which helps but doesn't fully solve the problem when many torrents are actively seeding with random
access patterns.

The combined random I/O from qBittorrent + other workloads (ci-runner, cross-backups minio,
jellyfin) exceeds the HDD IOPS ceiling, causing queue depth to spike and affecting all workloads
on prusik.

## How to Diagnose

1. Check HDD queue depth (values > 1 indicate I/O queuing):

```bash
# Via Grafana/Prometheus
kubectl --context=grigri query_prometheus --expr 'rate(node_disk_io_time_weighted_seconds_total{instance="prusik", device=~"sd[abcd]"}[5m])'
```

2. Check per-container I/O on HDDs:

```bash
# Top consumers of HDD reads
kubectl --context=grigri query_prometheus --expr 'topk(10, rate(container_fs_reads_bytes_total{node="prusik", device=~"/dev/sd.*"}[5m]))'
```

3. Check qBittorrent I/O priority:

```bash
kubectl --context=grigri exec qbittorrent-0 -n qbittorrent -c qbittorrent -- ionice -p 1
```

4. Check qBittorrent config values:

```bash
kubectl --context=grigri exec qbittorrent-0 -n qbittorrent -c qbittorrent -- cat /config/qBittorrent/qBittorrent.conf | grep MaxActive
```

## Fix

Two changes applied together:

### 1. ionice idle priority (the key fix)

Run qBittorrent with I/O class 3 (idle) — only does I/O when no other process needs disk:

```yaml
# values.yaml — qbittorrent container
command:
  - /bin/sh
  - -c
  - |
    exec ionice -c 3 /entrypoint.sh

securityContext:
  capabilities:
    add:
      - SYS_NICE  # Required for ionice
```

### 2. Increase startup probe timeout

With idle I/O priority, qBittorrent startup is slow (loading 4400+ torrents from BT_backup).
Increase the startup probe to 10 minutes:

```yaml
startup:
  failureThreshold: 60  # was 30
  periodSeconds: 10
```

### Results

| Metric | Before | After |
|--------|--------|-------|
| HDD queue depth | 8-11 | 1.5-5.5 |
| HDD IOPS | ~240 (ceiling) | ~145-190 |
| Forgejo git ops | 1511ms | 5-7ms |
| Forgejo runner updates | 112-295ms | 5-78ms |

## Notes

- `ionice -c 3` (idle) is the lowest possible I/O priority. There is no class 10.
- `MaxActiveUploads` was kept at 2800 to avoid hit-and-run penalties on private trackers.
  The ionice setting handles I/O prioritization, not the upload limit.
- L2ARC (1.28 TB on prusik) only caches reads, not writes. It has a ~40% hit ratio which
  helps but doesn't eliminate the problem for random seeding reads.
- If further I/O reduction is needed, consider moving qBittorrent downloads to NVMe storage.
