# prusik: CI memory bursts swap-thrash the root NVMe and freeze the API

Two control-plane outages on 2026-10-12 and 2026-10-13, plus the 2026-09-25 incident, share one
mechanism. Node `prusik` (single-node k3s control plane, embedded etcd, k3s v1.35.8+k3s1 /
etcd v3.6.14-k3s1). The first two incidents were attributed to etcd snapshots; they are not.

**TL;DR:** CI memory bursts collapse `MemAvailable` to ~3 GiB, the kernel's reclaim path (swap-out
+ dirty-page writeback + ZFS ARC shrink) saturates the root NVMe, and that same device holds etcd's
WAL, its bolt DB, and `/swapfile`. etcd's durable writes take seconds instead of milliseconds, the
Kubernetes controller Lease cannot renew, k3s calls `logrus.Fatalf` and exits, and every restart
runs an unconditional startup defrag before serving — ~30–75s of API offline per cycle.

## Problem

What is visible from outside:

- The Kubernetes API becomes unresponsive for **~30–75 seconds at a time**, periodically, in bursts
  that recur every few minutes until the memory pressure passes.
- `kubectl --context=grigri` calls hang or return `DeadlineExceeded`; `kube-system` components log
  `leaderelection lost`; controllers stall.
- Components that were mid-flight when k3s died are left in inconsistent state — e.g. a Velero
  `Backup` stuck `InProgress` with orphaned `ZFSBackup` CRs
  (`docs/troubleshooting/velero-2026-09-25-oom-etcd-outage.md`).
- `k3s.service` restarts itself (`Restart=always`), so `NRestarts` climbs without anyone acting.

**The misdirection:** the freezes appear to start "at a snapshot". k3s cron backups run on the
default `0 */12 * * *` (00:00/12:00 local), and the journal prints `Saving etcd snapshot` right
before a stall, so snapshots look guilty. They were not observed triggering the stalls in these
incidents, but they are not exonerated for all future scenarios — see
[Why snapshots are not the observed trigger](#why-snapshots-are-not-the-observed-trigger).

## Root Cause

```
CI memory burst (ci-runner-0 up to 10.7-15 GiB; calypso gVisor runners 16Gi limits each)
  -> MemAvailable collapses to ~3 GiB (62 GiB host, non-CI baseline is flat at 30-35 GiB)
    -> kernel reclaim: swap-out to /swapfile + dirty-page writeback + ZFS ARC shrink 4.9 -> 2.85 GiB
       (against its 5 GiB cap, metal/inventory/host_vars/prusik.yml)
      -> ALL of that I/O is on the root NVMe (SanDisk Extreme 500GB), which also holds:
         - etcd's WAL and bolt DB (/var/lib/rancher/k3s/server/db/etcd)
         - /swapfile (16 GiB, metal swap_file_size_mb: 16384)
        -> etcd durable-write latency spikes: WAL fsync p99 ~1.92s, backend commit p99 ~3.71s
           at 2026-10-13 03:07:30Z (baseline ~1-3ms)
          -> kube-apiserver -> etcd DeadlineExceeded on writes and ReadIndex
            -> the KUBERNETES CONTROLLER Lease cannot renew
               (kube-system Lease, wrangler's leader.RunOrDie — not etcd raft)
              -> logrus.Fatalf -> k3s exits with code 1
                -> systemd Restart=always starts k3s again
                  -> k3s runs an UNCONDITIONAL startup defrag (pkg/etcd Test()) before serving,
                     0.5-4.06s of extra offline time, on the same saturated device
                    -> API offline ~30-75s. Repeat until the memory burst ends.
```

Three independent amplifiers make a bad latency blip into a full outage:

1. **Shared device.** etcd's durable write path, the swap device, and the root filesystem are the
   same NVMe. Reclaim I/O is not "background" — it is queued ahead of the WAL fsync.
2. **Overcommitted CI.** `prusik` carried **204.6 GiB of pod limits against 50.7 GiB allocatable
   (~403%)**. The scheduler is request-driven, so pods without explicit limits can still be scheduled
   if their requests fit, but the aggregate limit overcommit means any burst is limited only by the
   kernel reaching `MemAvailable` first — and the kernel loses that race against etcd's lease.
3. **Restart cost.** k3s defrags at startup before it serves, so each cycle's downtime is
   *latency stall + defrag*, not just the process restart.

## Evidence

### Incident windows

| Time (UTC)     | Memory pressure source                                                                         | Host state                                                                        | etcd / API outcome                                                            |
|----------------|------------------------------------------------------------------------------------------------|-----------------------------------------------------------------------------------|-------------------------------------------------------------------------------|
| 2026-09-25 02:00–02:21 | CI Rust builds during the weekly Velero backup window                              | `MemAvailable` 3.9 GiB (~5.9%), root-NVMe queue depth 218                          | k3s lost controller lease, exit(1) at 02:20:54; Backup left `InProgress`       |
| 2026-10-12 07:20       | calypso CI activity + an ArgoCD update + a 16,608-event count query                       | memory-PSI "full" elevated, swap active, ARC shrinking toward 2.85 GiB              | API stalls, k3s restart cycle                                                 |
| 2026-10-13 02:06       | CI pressure starts **before** the 02:30Z backup window                                             | swap used 5.5 -> 8.3 GiB, `ci-runner-0` at 10.7 GiB (high-water 15 GiB)             | first stall cycle of the incident                                             |
| 2026-10-13 03:07:30    | after `ci-runner-0` had dropped, calypso gVisor runners carried the pressure | `MemAvailable` ~3 GiB, memory-PSI "full" 0.24–0.63, root-NVMe queue in the hundreds | WAL fsync p99 1.92s, backend commit p99 3.71s, controller lease lost, exit(1) |

CI is the proven memory culprit in all three incidents. Velero serializes weekly and quarterly
backups (one queued behind the other), so they do not run concurrently on prusik's pool. The backups
are a possible contributor to I/O load during the 02:30–03:30Z window, but the memory pressure
started at 02:06Z, before the backup window. RPC names like "calypso lease" describe what was
running, not proof of causal memory pressure — attribution requires per-pod working-set queries
(see the runbook).

### The latency numbers that matter

```
baseline (healthy):        WAL fsync p99 ~1-3ms      backend commit p99 ~1-3ms
2026-10-13 03:07:30Z:      WAL fsync p99 1.92s       backend commit p99 3.71s
restart defrag:            0.5-4.06s per k3s start   (unconditional, before serving)
```

Measured at 30–60s resolution (etcd scrape interval 30s, `system/monitoring/resources/etcd-scrape-config.yaml`).

**Histogram interpolation caveat:** the `1.34s`/`1.88s` p99 figures visible at 03:09Z are from a
`rate(...[5m])` window that straddles k3s restarting and exporting fresh zero-based counters.
`rate(...[5m])` handles counter resets, but the window mixes pre-restart and post-restart buckets
from different populations, which can produce interpolated values that do not correspond to either.
The clean pre-restart peaks with `rate(...[1m])` are **1.92s / 3.71s**. Avoid windows that straddle
restarts; use clean pre-restart windows and verify actual bucket counts if precision matters.

### Memory attribution at the incident peaks

Non-CI baseline on prusik is flat at **30–35 GiB** — every GiB of the collapse is CI.

| Culprit | Shape | Behaviour |
|---|---|---|
| `ci-runner-0` / `runner` (ns `ci-runners`, runc) — PRIMARY | request 4Gi, limit was 18Gi | grew idle → **10.7 GiB** (high-water **15 GiB**), anonymous/unreclaimable. Present identically in both October incidents. Lands on prusik *implicitly* through the `fast-ci-zfspv` PVC's `allowedTopologies` — it has no `nodeSelector`. |
| `calypso-runners` gVisor pods (`autonomous-runner-*`) — SECONDARY/additive | request **0.29Gi** vs limit **16Gi** each, 2–5 concurrent | drove the *second* 2026-10-13 failure at 03:07Z, after `ci-runner-0` had already dropped. These manifests are **not in this repo** (owned by `multivara/gitops` at `/home/agil/multivara/gitops/grigri.cloud/apps/calypso/values.yaml`; app runner source at `/home/agil/multivara/calypso/crates/calypso-control-plane/src/runner_controller/k8s/runner/deployment.rs`). Do not assert specific runtime or gVisor configuration changes without checking the other repo — historical values may not match current state. |

### The observability blind spot (why CI looked half its real size)

The `container!=""` filter used nearly everywhere — and in kube-prometheus-stack's recording rule
(`image!=""`) — **drops all gVisor sandbox series**, because those carry no `container` label.

```
calypso working set, container!="POD":  3.96 GB (decimal, ~3.69 GiB)   (actual)
calypso working set, container!="":     1.97 GB (decimal, ~1.83 GiB)   (what dashboards showed)
```

CI memory was under-reported by ~50%. The values above are decimal GB (10^9 bytes) as reported by
Prometheus; convert to GiB (2^30 bytes) by dividing by 1.074 if you need binary units. The fix is a gVisor-aware recording rule; **it is not yet
implemented** (see Deferred). Until then, use the verified pod-aggregate query from the runbook for
any attribution on prusik.

## Why snapshots are not the observed trigger

The original hypothesis, and the reason this took three incidents to root-cause.

- **k3s cron snapshots are fast.** They stream **130–276 MB in 0.3–0.7s**
  (`etcd_disk_backend_snapshot_duration_seconds` p99 **< 0.64s**). A 0.5s copy cannot produce a
  multi-second API freeze, and it certainly cannot produce one at 03:07 when the schedule fires
  00:00/12:00.
- **Internal raft snapshots are a different thing entirely.**
  `etcd_debugging_snap_save_total_duration_seconds` is the raft-snapshot save (~1.5ms, on a
  ~10k-entry trigger cadence). It has nothing to do with k3s backups. Confusing the two metric names
  sends you down the wrong path.
- **The timing is wrong.** Slow operations begin **minutes after** a snapshot, and continue when no
  snapshot is running. Snapshots are correlated with the *outbreak* only because 12-hour cron and a
  Tuesday backup window happen to sit near the CI bursts that actually cause it.
- **Real but irrelevant inefficiency:** snapshots do copy dead pages — the DB file is **182 MB
  allocated vs 66 MB in use** (`etcd_mvcc_db_total_size_in_bytes` vs
  `..._in_use_in_bytes`). That is a 2.7x file-size inefficiency, fixed by compaction/defrag, not a
  freeze. (It is also what makes startup defrag slow when the device is already saturated.)

Snapshots were not observed triggering the stalls in these incidents, but they are not exonerated
for all future scenarios — if the DB grows large enough or the device degrades, snapshot I/O could
become a factor. Monitor `etcd_mvcc_db_total_size_in_bytes` and the snapshot duration metric.

## Why it is NOT etcd raft leader loss

`etcd_server_has_leader` stayed **1** and `etcd_server_leader_changes_seen_total` stayed **1**
throughout — as expected for a single-node member. The `leaderelection lost` line in the journal is
the **Kubernetes controller Lease** (`kube-system` Lease, k3s's wrangler `leader.RunOrDie`), not a
raft election. Grepping for "leader" without knowing which elector logged it makes this outage look
like an etcd consensus problem, which it never was. The lease is a *victim* of etcd latency.

## Why the ZFS scrub is not the independent cause

A monthly scrub of the 25.9 TB `datasets` pool ran for **~65h** and pushed host I/O-PSI to
**0.5–0.78**. Tempting, but:

- Healthy windows showed **equally high I/O-PSI** with no API stalls.
- The scrub runs on the RAIDZ1 HDDs; etcd's WAL and `/swapfile` are on the root NVMe. Different
  physical devices, different queues.
- The differentiator is **memory**, not disk: memory-PSI "full" 0.24–0.63, active swap, and
  `MemAvailable` < 3 GiB during stalls — and **zero** when healthy.

The scrub is a contributor to I/O load, but its role in the outages is unresolved — controls did
not independently isolate it. Use memory-PSI plus swap as the discriminator for this mechanism.
I/O-PSI alone on this host tells you nothing.

## Why it is NOT an OOM kill

No `node_vmstat_oom_kill` and no `container_oom_events_total` for k3s in any of these windows. The
sequence was **direct-reclaim stalling + swap thrash** (swap used 5.5 → 8.3 GiB at 02:40Z), and then
k3s **exited by itself** (code 1) on lost lease. Nothing was killed. An "OOM?" reflex wastes time
here: look for `oom_kill`, then look at PSI and swap, then look at `NRestarts` and the journal.

**Major page faults are not proof of direct reclaim.** `node_vmstat_pgmajfault` can rise from
file-backed mmap refaults (e.g. etcd's bolt DB pages evicted from page cache and re-read from disk),
not just anonymous page swap-in. The `pgscan_direct` and `pgscan_kswapd` metrics that would prove
direct reclaim are absent from prusik's node-exporter. Use PSI (`node_pressure_memory_stalled_seconds_total`)
and swap rate (`node_vmstat_pswpin`/`pswpout`) as the reclaim indicators.

**k3s swap usage:** k3s's anonymous Go heap swap usage was measured at ~98MB during the incident,
which proves the control plane *currently* has some swapped pages, but it is not the whole historical
picture or the exact mechanism. The file-backed bolt DB mmap pages can be reclaimed and refaulted
without being swapped — that is page-cache eviction, not anonymous swap. The swap thrash (5.5 → 8.3 GiB)
is the primary mechanism, but the in-memory index latency (550ms) is not uniquely proof of swap —
other CPU/GC/query pressure is possible.

## Device names on prusik are NOT stable — read this before writing any query

**NVMe kernel names on prusik change across reboots.** The two devices are:

| Model | Role | Kernel name 2026-09-25 | Kernel name after the 2026-10-02 reboot |
|---|---|---|---|
| **SanDisk Extreme 500GB** | root `/` (ext4) — holds the etcd WAL + bolt DB **and** `/swapfile` | `nvme1n1` | **`nvme0n1`** |
| **FIKWOT FN960 2TB** | L2ARC / special vdev for the HDD RAIDZ1 `datasets` pool | `nvme0n1` | **`nvme1n1`** |

They will flip again at the next reboot. Therefore:

- **Always** identify these disks by model, serial, or `/dev/disk/by-id` — never by `nvmeXnY` — in
  queries, alerts, docs, runbooks, and ad-hoc commands.
- Verify the current mapping before trusting any historical figure:
  `node_disk_info{instance="prusik"}` (maps device → model → PCI path), or on the host
  `lsblk -o NAME,MODEL,SERIAL,MOUNTPOINT`.

## What we changed

**Status: PARTIALLY DEPLOYED, HOST PENDING.** GitOps changes committed and synced (PriorityClass
e61bce19, CI dbb28f62, Velero 78dc33a1, monitoring 48eba216). Metal changes committed (85c1c518)
but **NOT YET APPLIED** to prusik — requires user-initiated maintenance window. See validation
status checklist below.

### Tier 0 — bound CI so it dies before the control plane does

| File | Change |
|---|---|
| `platform/ci-runners/resources/statefulset.yaml` | `runner` limit **18Gi → 12Gi**; added `CARGO_BUILD_JOBS=4` and `MAKEFLAGS=-j4` (cargo defaults to one rustc/linker job per core, so peak RSS multiplies with the 14-CPU limit); `priorityClassName: low-priority` |
| `platform/ci-runners/resources/configmap.yaml` | runner `capacity: 3 → 2` (aggregate peak RSS ≈ capacity × per-job peak — the coarsest lever, since a per-container limit cannot bound the total) |
| `platform/ci-runners/resources/statefulset-amd64.yaml` | `high-priority` → `low-priority`; **removed the backwards `node.kubernetes.io/memory-pressure` toleration** (it kept scheduling a memory-hungry runner onto a node that was already starving); added the parallelism envs |
| `platform/ci-runners/resources/configmap-amd64.yaml` | `capacity: 5 → 2` (k8s-amd64-1 has ~12.53 GiB allocatable) |
| `system/kube-system/resources/priority-class-low.yaml` | new PriorityClass `low-priority`: value **-1000**, `preemptionPolicy: Never` — expendable, but not disruptive; cannot outrank `system-critical`/`node-critical` |
| `platform/ci-runners/resources/limit-range.yaml` | new LimitRange: container max **12Gi**, default 2Gi, defaultRequest 256Mi — turns "someone asks for more" into an admission error |
| `platform/ci-runners/resources/resource-quota.yaml` | new ResourceQuota: `limits.memory: 48Gi`, `requests.memory: 16Gi` — a backstop against a third unbounded runner, not the primary control |

### Tier 0 — de-collide the backups

`platform/velero/templates/schedule-retain-quaterly.yaml`: **`30 2 * * 2` → `30 3 * * 3`**. Weekly and
quarterly were on the same cron, on the same node, so two `zfs send` streams hit prusik's pool at
once (2026-10-13 02:40Z). Wed 03:30 also avoids the etcd snapshots (00:00/12:00), the etcd snapshot
rsync (01:00/13:00), and the daily ZFS snapshot schedule (01:00). Preserves the Thu 02:30 offsite
copy schedule.

### Tier 1 — protect the node and the control plane

| File | Change |
|---|---|
| `metal/inventory/host_vars/prusik.yml` | `eviction-hard=memory.available<300Mi` → **`<1Gi`**, plus `eviction-soft=memory.available<4Gi` and `eviction-soft-grace-period=1m`. 300Mi fired far too late on a 62 GiB host: the node stalls near 3 GiB, so by the time kubelet acted the etcd freeze had already happened. |
| `metal/roles/k3s/templates/k3s.service.j2` | added **`MemoryLow=6G`** to the k3s unit, gated on the `kube_control_plane` group so agent nodes are unaffected. **Caveat:** newly verified that `system.slice` has `memory.low=0` and `k3s.service` has `memory.low=0` (before this change), and there is no `memory_recursiveprot` set. The 6G service-level proposal effective protection is **uncertain** without ancestor budget configured and validated. `MemoryLow` without ancestor protection does not guarantee "take pods first" — it is a preference that needs the ancestor cgroup hierarchy to be configured. **Follow-up:** validate ancestor budget (system.slice, init.scope) and enable recursive protection if needed. Current change is staged but not yet effective. |

### Tier 1 — detect it next time

`system/monitoring/resources/etcd-host-memory-rules.yaml` (new, wired in
`system/monitoring/kustomization.yaml`): 11 alerts — `EtcdTargetDown`, `K3sEtcdRestarted`,
`EtcdWALFsyncSlow`/`Critical`, `EtcdBackendCommitSlow`/`Critical`, `PrusikMemoryAvailableLow`/
`Critical`, `PrusikMemoryPSIHigh`, `PrusikSwapThrashing`, `PrusikRootDiskQueueHigh`. They come in
pairs on purpose: the etcd metrics are the symptom that pages, the node metrics are the cause that
explains it.

**Alert `for:` durations:** warning alerts use `for: 5m`, critical use `for: 2m`. These can miss
brief outages if k3s restarts faster than the window. `K3sEtcdRestarted` uses `for: 0m` as a fast
signal — if the restart alert is the first thing you see, the host-side thresholds are too slow:
tune them down, do not add more etcd rules.

**Why we needed our own etcd rules at all:** kube-prometheus-stack's built-in etcd rules are **inert**
here. They are gated on `kubeEtcd.enabled`, which is `false` in `system/monitoring/values.yaml`
(etcd runs as a systemd process, not a mirrored pod), so `defaultRules.rules.etcd: true` renders
nothing. Do not re-enable it expecting coverage.

### Validation status checklist

**GitOps (committed and synced via ArgoCD):**

- ✅ PriorityClass `low-priority` (e61bce19) — Synced/Healthy
- ✅ CI runners (dbb28f62) — Synced/Healthy, 4/4 pods Running, `low-priority`, 12Gi runner limit,
  `CARGO_BUILD_JOBS=4`, `MAKEFLAGS=-j4`, both StatefulSets capacity 2, LimitRange 12Gi max,
  ResourceQuota 48Gi/16Gi live (used 43904Mi/9568Mi), no FailedCreate events
- ✅ Velero quarterly schedule (78dc33a1) — Synced/Healthy, Wed 03:30 UTC cron `30 3 * * 3` enabled
- ✅ Monitoring rules (48eba216) — Synced/Healthy, 11 rules loaded in Prometheus, state inactive,
  etcd target up 1, WAL fsync p99 3.7ms (baseline), MemAvailable 26.5%
- ⚠️ Monitoring ArgoCD shows pre-existing OutOfSync on KanidmGroup `grafana-mcp-users` — unrelated
  to these rules, does not affect alert functionality

**Metal (committed but NOT applied to prusik):**

- ❌ `metal/inventory/host_vars/prusik.yml` — eviction-soft thresholds configured in git but **not
  yet applied** to host
- ❌ `metal/roles/k3s/templates/k3s.service.j2` — `MemoryLow=6G` configured in git but **not yet
  applied** to host (current `systemctl show k3s -p MemoryLow` returns 0)
- ❌ Ancestor cgroup budget — `system.slice` and `init.scope` still have `memory.low=0`, no
  `memory_recursiveprot` set. The 6G service-level protection is **not verified effective** without
  parent budget. See Deferred §3.

**Before claiming the incident is cured:**

- [ ] Apply metal changes during scheduled maintenance: `cd /home/agil/homelab/metal && ANSIBLE_EXTRA_ARGS="-t k3s --limit prusik" make cluster` (USER ONLY, ~30-75s possible API outage)
- [ ] Validate ancestor cgroup budget and enable recursive protection if needed
- [ ] Watch CI + backup windows for sustained peak behavior (no lease-loss exits, etcd latency <100ms p99, memory-PSI ≈0, swap rate ≈0)
- [ ] Verify calypso gVisor runner bounds in `multivara/gitops` repo

**Do not claim "fixed" or "resolved"** until sustained peak test passes. The calypso second-failure
mode remains unresolved until the other repo is updated.

## Deferred — next steps

Ordered by how much of the failure mode each one removes.

1. **Move CI off prusik** — `planning/ci-runner-amd64-1.md`. Blocked on k8s-amd64-1 going 16 → 32 GiB
   RAM (`planning/k8s-amd64-1-node-addition.md` TODO-9.2): `ci-runner-0` peaks at 17.4 GiB / 7.68
   cores and the node has only 16 GiB, so it cannot host the runner today.
2. **Bound the calypso gVisor runners** — different repo (`multivara/gitops`). Their
   0.29Gi-request / 16Gi-limit shape is the actual overcommit: the scheduler places them believing
   0.29 GiB and they take gigabytes. They drove the second failure of 2026-10-13. The existing
   ResourceQuota is CI-only; calypso is outside this repo's scope, so the second failure mode
   remains unresolved until the other repo is updated.
3. **Validate MemoryLow ancestor protection** — the 6G `MemoryLow` on `k3s.service` is staged but
   not yet effective without ancestor cgroup budget. Verify `system.slice` and `init.scope` have
   appropriate `memory.low` values, and enable `memory_recursiveprot` if needed so the protection
   propagates correctly. Without this, the service-level preference may not provide the intended
   "pods before control plane" behaviour.
4. **gVisor-aware memory recording rule + dashboard**, so CI stops being under-reported by ~50% —
   until then attribution on prusik is wrong in a way that looks like "nothing was using that memory".
5. **Raise `zfs_arc_max` above 5 GiB** (`planning/k8s-amd64-1-node-addition.md` TODO-6.6) — **only
   after CI leaves.** Raising the cap while `MemAvailable` bottoms out at ~3 GiB trades one stall for
   a worse one. See `docs/conventions/prusik-fast-storage-tier.md`.
6. **Dedicated etcd device**, separate from the OS filesystem and swap, so reclaim I/O cannot queue
   ahead of a WAL fsync.
7. **3-member etcd HA** — an *availability* project, explicitly not a fix for this. A second member
   gives no quorum tolerance, and overcommit breaks any member whose host is starved.

## Rejected approaches

Do not re-propose these; each was considered and rejected on the evidence.

- **Disabling or reducing etcd snapshots** — they are not the observed trigger (0.3–0.7s), and they are the
  recovery path.
- **Raising etcd timeouts / heartbeat intervals** — masks the stall, makes the API slower for
  everyone, and leaves the memory collapse in place.
- **Removing swap before freeing RAM** — with 204.6 GiB of limits on 50.7 GiB allocatable, removing
  the overflow valve converts thrash into OOM kills of whatever the kernel picks, including the
  control plane.
- **Tuning `vm.swappiness` alone** — changes the balance between reclaim paths, not the total
  pressure on the shared device.
- **`ionice`/`nice` on k3s** — one process contains the API server, scheduler, controllers, and
  etcd; lowering its I/O priority starves exactly what you are protecting.

## If this recurs — how to keep investigating

Everything below is **read-only**. SSH commands on prusik are for inspection only
(`AGENTS.md`: no ad-hoc mutation on a remote host); if a command needs root, it is marked `[root]`
and it only reads. All `kubectl` commands use `--context=grigri` (the kustomize local context can
include other contexts). Do not escalate to local privileged commands; do not execute mutating
`kubectl` commands. If a `-t k3s` maintenance command is present, it is clearly **NOT agent-allowed** —
the user must run it manually.

Resolve the root device first — see the device-instability section. Use `DEVICE` below for
"whatever `node_disk_info`/`lsblk` says the SanDisk Extreme 500GB is today".

### 1. Confirm it is the same mechanism (60 seconds)

Five discriminating signals. An outage is this mechanism when all of them move **together**; a
healthy baseline is flat on all five.

| Signal | Query | Healthy | This incident | Alert |
|---|---|---|---|---|
| `MemAvailable` | `node_memory_MemAvailable_bytes{instance="prusik"}` | ≥ 8 GiB (flat) | **< ~4 GiB** (~3 GiB) | `PrusikMemoryAvailableLow` / `Critical` |
| Memory PSI "full" | `rate(node_pressure_memory_stalled_seconds_total{instance="prusik"}[1m])` | **0** | **0.24–0.63** | `PrusikMemoryPSIHigh` (>0.1) |
| Swap rate | `rate(node_vmstat_pswpout{instance="prusik"}[1m])` pages/s | ~0.5 pages/s | **> 1000 pages/s** (storm ~25k) | `PrusikSwapThrashing` |
| Root-NVMe queue | `rate(node_disk_io_time_weighted_seconds_total{instance="prusik",device="DEVICE"}[1m])` | single digits | **> 32** (218 measured, 400+ in a later storm) | `PrusikRootDiskQueueHigh` |
| etcd WAL fsync p99 | see [Prometheus](#3-prometheus-audited-query-set) | ~1–3ms | **> 0.25s**, peaked **1.92s** | `EtcdWALFsyncSlow` / `Critical` |
| etcd backend commit p99 | see [Prometheus](#3-prometheus-audited-query-set) | ~1–3ms | **> 0.25s**, peaked **3.71s** | `EtcdBackendCommitSlow` / `Critical` |

**Memory-thrash vs a pure disk problem — the decision rule:**

- Memory signals (PSI full > 0.1, swap, `MemAvailable` < 4 GiB) **all zero** but root-disk queue and
  etcd latency are high → it is the *device*: check SMART, write-cache flush behaviour, thermal
  throttling, and who else is writing to that NVMe. Do not reach for the CI playbook.
- Memory signals **non-zero** → this mechanism. Go straight to attribution (§4).
- High **I/O-PSI with no memory PSI** is normal on this host during a scrub — 0.5–0.78 was measured
  with healthy control-plane latency. Never treat `node_pressure_io_*` as the trigger.
- If etcd latency is high and `etcd_server_has_leader` is still 1 with
  `etcd_server_leader_changes_seen_total` unchanged, that is consistent with this mechanism: the
  killer is the Kubernetes Lease, not raft.

### 2. On the node (SSH, read-only)

```bash
# The outage signature. Also confirms snapshot duration and the startup defrag. [root]
# Use --utc because the host timezone is CEST; explicit --since/--until in UTC avoids ambiguity.
sudo journalctl -u k3s --utc --since "2026-10-13 02:00:00 UTC" --until "2026-10-13 04:00:00 UTC" \
  | grep -iE "apply request took too long|ReadIndex|leaderelection lost|Saving etcd snapshot|fetched snapshot|Defragmenting|DeadlineExceeded"

# Did k3s actually die, and how many times? NRestarts is the outage counter. [root]
systemctl show k3s -p MemoryLow,MemorySwapMax,MemoryCurrent,MemoryPeak,NRestarts
sudo systemctl status k3s --no-pager

# etcd's own view: latency, DB size, leadership (metrics are served in-process on :2381, so
# target-down here means k3s is down or restarting)
curl -s http://127.0.0.1:2381/metrics \
  | grep -E "db_total_size|has_leader|leader_changes|wal_fsync|backend_commit"
curl -s http://127.0.0.1:2381/metrics | grep -E "^process_start_time_seconds"

# Snapshot timing — confirms snapshots are 0.3-0.7s, not the freeze
sudo k3s etcd-snapshot ls

# Is the swapfile really on the root device, and is it fragmented across it? [root]
swapon --show --bytes; findmnt -T /swapfile; sudo filefrag -v /swapfile | head

# Resolve current NVMe naming BEFORE using any device name in a query. [root]
lsblk -o NAME,MODEL,SERIAL,MOUNTPOINT
sudo ls -l /dev/disk/by-id/

# Pressure per resource, live. memory:full > 0 is the tell; io:full alone is not. [root]
cat /proc/pressure/{io,memory,cpu}

# Device-level view: %util, aqu-sz (queue depth), await on the root NVMe
iostat -xz 5 2

# How much of k3s is actually swapped out right now? Large VmSwap = the control plane is paying
# reclaim latency directly. [root]
pgrep -af "k3s server"; sudo grep VmSwap /proc/<k3spid>/status
```

### 3. Prometheus (audited query set)

```promql
# --- Host: is the node out of RAM? ---
node_memory_MemAvailable_bytes{instance="prusik"}
node_memory_MemAvailable_bytes{instance="prusik"}
  / node_memory_MemTotal_bytes{instance="prusik"}          # < 0.08 warning, < 0.05 critical

# --- Host: is anything blocked in reclaim? (the "full" line = *_stalled series) ---
rate(node_pressure_memory_stalled_seconds_total{instance="prusik"}[1m])
rate(node_pressure_memory_waiting_seconds_total{instance="prusik"}[1m])

# --- Host: swap traffic, converted to MiB/s (counters are PAGES, not bytes) ---
rate(node_vmstat_pswpin{instance="prusik"}[1m]) * 4096 / 1048576
rate(node_vmstat_pswpout{instance="prusik"}[1m]) * 4096 / 1048576

# --- Device: resolve DEVICE by model BEFORE using it in queries ---
# The SanDisk Extreme 500GB root disk kernel name flips across reboots. Resolve it now:
node_disk_info{instance="prusik"}
# Look for model="Extreme_500GB" (or similar) and note the current device name.

# --- Device: queue depth (weighted time = avg in-flight), mean write await, utilisation ---
# DEVICE = resolved from node_disk_info above, NOT a remembered nvmeXnY
rate(node_disk_io_time_weighted_seconds_total{instance="prusik",device="DEVICE"}[1m])

# Mean write await in milliseconds (not tail, not weighted time / total IO).
# weighted time is useful for queue depth, NOT the standard await numerator; use write_time.
# clamp_min prevents division by zero when writes are idle.
1000 * rate(node_disk_write_time_seconds_total{instance="prusik",device="DEVICE"}[1m])
  / clamp_min(rate(node_disk_writes_completed_total{instance="prusik",device="DEVICE"}[1m]), 0.001)

# Read await analogous (optional, one query enough):
1000 * rate(node_disk_read_time_seconds_total{instance="prusik",device="DEVICE"}[1m])
  / clamp_min(rate(node_disk_reads_completed_total{instance="prusik",device="DEVICE"}[1m]), 0.001)

rate(node_disk_io_time_seconds_total{instance="prusik",device="DEVICE"}[1m])   # %util

# --- etcd: the symptom that pages. Use [1m] around restarts to avoid mixed populations. ---
histogram_quantile(0.99, sum by (job, instance, le) (
  rate(etcd_disk_wal_fsync_duration_seconds_bucket{job="etcd"}[1m])
))
histogram_quantile(0.99, sum by (job, instance, le) (
  rate(etcd_disk_backend_commit_duration_seconds_bucket{job="etcd"}[1m])
))

# --- etcd: snapshots and DB bloat ---
histogram_quantile(0.99, sum by (job, instance, le) (
  rate(etcd_disk_backend_snapshot_duration_seconds_bucket{job="etcd"}[1m])
))
rate(etcd_debugging_snap_save_total_duration_seconds_sum{job="etcd"}[1m])
  / rate(etcd_debugging_snap_save_total_duration_seconds_count{job="etcd"}[1m])
etcd_mvcc_db_total_size_in_bytes{job="etcd"}
  / etcd_mvcc_db_total_size_in_use_in_bytes{job="etcd"}    # ~2.7x = dead pages, not a freeze

# --- etcd: leadership (expect has_leader=1, leader_changes flat at 1) ---
etcd_server_has_leader{job="etcd"}
etcd_server_leader_changes_seen_total{job="etcd"}

# --- ZFS ARC: is reclaim eating the cache? (cap is pinned at 5 GiB) ---
node_zfs_arc_c_max{instance="prusik"}                     # 5 GiB, metal/inventory/host_vars/prusik.yml
```

**Metrics that do not exist on this node** — do not write queries or alerts against them, they will
silently return nothing (empty panels look identical to a healthy system):

- `node_vmstat_pgscan_direct`, `node_vmstat_pgscan_kswapd` — absent from prusik's node-exporter. Use
  PSI (`node_pressure_memory_stalled_seconds_total`) and `node_vmstat_pswpin`/`pswpout` instead.
- `node_memory_page_size_bytes` — absent, which is why `PrusikSwapThrashing` thresholds in **pages/s**
  (4 KiB pages) rather than bytes.

### 4. Attribute the memory to a workload

Query at a **point in time** at the peak, not `max_over_time` across a window. A windowed max
summarises every short-lived pod at its own moment and reports a total that never existed on the node
simultaneously — which is exactly how a gVisor burst gets blamed on a runner that already exited, or
missed entirely.

```bash
# Prometheus HTTP API: instant query evaluated AT the peak timestamp
curl -sG http://prometheus.internal.grigri.cloud/api/v1/query \
  --data-urlencode 'query=max by(namespace,pod)(container_memory_working_set_bytes{node="prusik",pod!="",container="",id!~".*cri-containerd.*"})' \
  --data-urlencode "time=2026-10-13T03:07:30Z"
```

```promql
# Verified pod-aggregate form — USE THIS ON PRUSIK.
# Excludes the root empty Pod series (container="") and excludes named container paths by matching
# id!~".*cri-containerd.*". max by (namespace,pod) picks the aggregate sandbox series if present,
# but this is not a generic guarantee — verify the metadata id filter against your node-exporter
# version. Use point-in-time instant queries, not summing max_over_time.
max by(namespace,pod)(container_memory_working_set_bytes{node="prusik",pod!="",container="",id!~".*cri-containerd.*"})

# Node-total sanity check against the same instant
sum(container_memory_working_set_bytes{node="prusik",pod!="",container="",id!~".*cri-containerd.*"})

# Was anything actually OOM-killed? (In these incidents: no. Answer the question, then move on.)
increase(container_oom_events_total{node="prusik"}[15m])
node_vmstat_oom_kill{instance="prusik"}
```

Expect: non-CI baseline flat at **30–35 GiB**; anything below that is CI. Check `ci-runner-0` first,
then `calypso-runners`, then any pod that started between the two.

### 5. The restart + startup-defrag amplifier

The downtime per cycle is *stall + unconditional defrag*, so quantify both.

```bash
# Restart count as an outage counter (also exposed as process_start_time_seconds on job="etcd")
systemctl show k3s -p NRestarts

# Each restart: Defragmenting ... before the API comes up (0.5-4.06s measured)
sudo journalctl -u k3s --utc --since "2 hours ago UTC" | grep -iE "Defragmenting|Starting k3s|Main process exited|Failed with result"
```

```promql
# Every change in process start time = one k3s restart. > 0 is the outage, not a rate.
changes(process_start_time_seconds{job="etcd"}[1h])
up{job="etcd"} == 0
```

If the defrag grows steadily across cycles, the DB is accumulating dead pages
(`etcd_mvcc_db_total_size_in_bytes` vs `_in_use_...`): compact and defrag **during a quiet window**,
not during a stall — defrag on a saturated device is the worst possible time.

### 6. Then check the collateral

Every k3s exit mid-backup leaves state behind. Verify it, do not assume a clean recovery:

```bash
kubectl --context=grigri get backups -n velero | grep -E "InProgress|Queued|PartiallyFailed|Failed"
kubectl --context=grigri get zb -n zfs-localpv | grep -v Done
```

Recovery procedure: `docs/troubleshooting/velero-2026-09-25-oom-etcd-outage.md` and
`docs/troubleshooting/velero-backup-failures.md`.

## Prevention and success criteria

Fixes are working when, across a CI burst **and** a backup window:

1. **No k3s lease-loss exits** — `changes(process_start_time_seconds{job="etcd"}[1h]) == 0` and
   `NRestarts` flat; no `leaderelection lost` in the journal.
2. **etcd latency stays sub-100ms at p99** for WAL fsync and backend commit during CI/backup windows
   (incident peak: 1.92s / 3.71s).
3. **CI dies at its own boundary** — the runner container is OOM-killed or evicted (with
   `low-priority` it is first in line) instead of the node thrashing. `container_oom_events_total`
   rising on `ci-runners` while the control plane stays healthy is **success**, not an incident.
4. **memory-PSI "full" ≈ 0 and swap rate ≈ 0 at the peaks**; `MemAvailable` never under the
   `eviction-soft` 4 GiB for more than the 1m grace period.
5. **Alerts fire early** — `PrusikMemoryAvailableLow`, `PrusikMemoryPSIHigh` and `PrusikSwapThrashing`
   precede `EtcdWALFsyncSlow`, which precedes `K3sEtcdRestarted`. If the restart alert is the first
   thing you see, the host-side thresholds are too slow: tune them down, do not add more etcd rules.

**Baseline caveat — read before comparing "before/after".** The pre-incident baseline is
**metrics-only**, and the metrics were added *while* a scrub was running: the scrub started
**2026-10-11 00:24** and the node-exporter/etcd series went live **2026-10-11 ~19:11Z**. There is
therefore **no clean pre-scrub window** in Prometheus. Treat "healthy" as *healthy under I/O-PSI
0.5–0.78 with memory-PSI at zero* — which is precisely the comparison that ruled out the scrub. Do
not conclude "I/O-PSI is high, so we are still broken" from the scrub overlap, and re-measure the
memory/swap floors after the Tier 0/1 changes land rather than reusing these numbers as a target.

## References

- Prior incident, same mechanism: `docs/troubleshooting/velero-2026-09-25-oom-etcd-outage.md`
- Backup collateral and recovery: `docs/troubleshooting/velero-backup-failures.md`,
  `docs/troubleshooting/velero-2026-09-26-stuck-backup-recovery.md`
- Why the ARC is pinned at 5 GiB and what to do about RAM:
  `docs/conventions/prusik-fast-storage-tier.md`,
  `docs/troubleshooting/prusik-l2arc-ineffective.md`
- Moving CI off prusik (TODO-9.2 gate): `planning/ci-runner-amd64-1.md`,
  `planning/k8s-amd64-1-node-addition.md`
- gVisor runner ownership: `planning/odroid-decommission.md`
- Alerts: `system/monitoring/resources/etcd-host-memory-rules.yaml` (each alert's annotation links
  here), scrape config `system/monitoring/resources/etcd-scrape-config.yaml`
- Hardware and device mapping: `docs/hardware/prusik.md`
- Writing docs like this one: `docs/conventions/documenting-learnings.md`
