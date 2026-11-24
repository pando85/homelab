# CI Runners: Memory Overcommit at Capacity 2 (OOM Kills and Evictions)

## Problem

2026-11-24 (v0.67.0 release day): 7 failed `multivara/calypso` jobs across the three runners in
~15 hours, while all runner pods/processes looked healthy:

- **z800** (desktop compose runner): 4 failures. One clippy compile killed by signal 9; three
  jobs whose logs stop mid-compile with no error, marked failed by Forgejo at *identical
  timestamps* (12:05:40, then 15:10:40 for two tasks at once).
- **ci-runner-amd64-0** (k8s-amd64-1): 2 failures, both marked failed at exactly 01:35:40 after
  the pod was recreated at 01:22.
- **ci-runner-0** (prusik): 1 genuine test failure (`reset_worker` tests, Redis connection
  timeout) — not infrastructure; read the job log before blaming the runner.

The z800 pattern looks like a network outage but is not one.

## Root Cause

**Capacity 2 with a 12Gi runner memory limit is an overcommit.** One heavy Rust job peaks at
~10.7GiB (fits — prusik runs capacity 1 and did not OOM). Two concurrent heavy host-mode jobs
want ~17–20GiB and cannot fit, on either machine. Kernel OOM process tables from z800 show what
the 12GiB cgroup holds at capacity 2:

- 2× `cargo` (one per job) + up to 4 compiler workers each (`CARGO_BUILD_JOBS=4`):
  `clippy-driver` 5.4GiB, `rustc` 2.7–5.2GiB each — ~11GiB with only 3 workers alive at snapshot
- 2× leased sccache slot daemons: 0.6–0.9GiB
- cargos, nextest, runner, page tables: ~0.5GiB

It usually survives because the concurrent pair is mixed: one heavy + one light job, or a
docker-mode job whose memory is accounted to the dind/host daemon cgroup instead of the runner's.
Release days cluster heavy host-mode jobs (validation + release builds + renovate PRs) and both
cap-2 runners lost simultaneously. The `statefulset-amd64.yaml` limit comment accepts this
trade-off explicitly ("jobs can still demand ~17.4GiB individually… OOM kills remain possible").

Two distinct failure mechanics:

- **Cluster (k8s-amd64-1, ~12.53GiB allocatable):** two jobs push the *node* into
  `MemoryPressure`; kubelet evicts the `low-priority` runner pod. Runner log: `received shutdown
  signal` / `shutdown initiated … N tasks currently running`. The pod's `terminationGracePeriodSeconds`
  (30s) cuts the 3h drain short; jobs die. The amd64 runner registers ephemerally, so the pod
  recreation also changes its runner UUID.
- **z800 (62GiB host, 12g cgroup):** the cgroup hits `memory.max` and everything stalls in direct
  reclaim — no logs, no heartbeats, `UpdateTask` client timeouts. Forgejo declares the tasks
  failed after ~12min of silence and **archives their logs**. Either the container OOM-dies and
  docker restarts it (`OOMKilled=true`, `RestartCount` increments; systemd reports e.g. "12G
  memory peak, 32.2T read from disk" — a page-cache refault storm), or the OOM killer shoots one
  compiler, memory frees, and the runner wakes up to `RESULT_FAILURE` + `log file has been
  archived` (final log upload rejected, 10 retries). sccache stats pushed to Loki *after* the
  failure timestamp prove the job kept running locally throughout.

## How to Diagnose

**Fetch the failed job logs (Forgejo API).** Three different id spaces — don't mix them:

```bash
# task list (id = task id, run_number = UI run number)
curl -H "Authorization: token $T" \
  'https://git.grigri.cloud/api/v1/repos/<owner>/<repo>/actions/tasks?limit=50&page=1'
# run list: id = internal run id (the 81xxx "run_id" in SCCACHE_STATS),
# index_in_repo = the 54xxx run number used in web URLs
curl ... '/actions/runs?limit=50'
# jobs of a run (bare JSON array; job id ≠ task id):
curl ... '/actions/runs/<run_id>/jobs'
# job log (plain text, timestamped):
curl ... '/actions/jobs/<job_id>/logs'
```

`fj actions tasks` lists tasks but has no log command. `/admin/actions/runners` needs
`read:admin` scope; repo/org runner lists are empty for org-level registrations.

**Triage signals:**

- Several tasks failed at the *same second* → runner-level event (eviction, OOM, restart), not
  job bugs. Contract gates failing in 0–1s are `needs` cascades of a failed validation.
- Job log ends mid-compile with no error → runner died or stalled; check the runner, not the code.
- Loki `{namespace="ci-runners", container="runner"}`: eviction shows the shutdown-signal lines;
  z800 pushes only `SCCACHE_STATS` (`source="sccache-stats-push"`), no runner log. Stats arriving
  after the failure time = the job was alive while Forgejo thought it dead.

**On z800 (post-mortem counters survive the stall):**

```bash
docker inspect forgejo-ci-runner-1 --format \
  'StartedAt={{.State.StartedAt}} OOMKilled={{.State.OOMKilled}} RestartCount={{.RestartCount}}'
journalctl -k --since '2026-11-24 00:00' | grep -iE 'oom|killed process'
# OOM table: RSS is in 4KiB pages. Two cargo rows = two concurrent jobs.
S=/sys/fs/cgroup/system.slice/docker-<id>.scope
cat $S/memory.events    # max = reclaim hits (21.7M in 5h), oom_kill counter
cat $S/memory.pressure  # PSI total = cumulative full-stall seconds
```

Gotcha: `docker logs --since 14:50` parses naive timestamps as **local time** (CET here) — append
`Z` for UTC, or the window is off by an hour.

**Cluster side:** `kube_node_status_condition{condition="MemoryPressure"}` and
`kube_pod_created` (uid change = pod recreation) around the failure timestamp;
`kubectl --context=grigri get events -n ci-runners` (events expire fast — Loki/journal are the
durable record).

## Fix / Workaround

- This is the **accepted trade-off** of cap 2 @ 12Gi, not a misconfiguration. Options:
  capacity 1 on both cap-2 runners (prusik model; halves peak throughput), more memory
  (16Gi still doesn't hold two ~10Gi jobs — only a large raise helps), or accept occasional
  failed jobs on heavy-overlap days and re-run them (the v0.67.0 release recovered this way).
- Don't raise the limit blindly: per-job peaks, not the limit, set the ceiling; the z800 compose
  deliberately mirrors the cluster pod (`mem_limit: 12g`, `cpus: 8`, `-j4`, capacity 2).
- **z800 gc visibility gap:** `ci`'s `verify_running` only checks `svc_list="runner"`, so a
  missing `gc` container passes `ci check`/`ci up`. Symptom: leaked `redis-<run>-1` /
  `postgres-<run>-1` service containers of failed runs stay up. Fix: `./ci up` (starts gc);
  durable fix: add `gc` to `svc_list`. See `~/multivara/ci-runners/docs/troubleshooting.md`.
- After infrastructure failures, re-run the jobs; identical-timestamp failures are safe to retry
  once the runner is healthy (idle cgroup, no pressure).

## Related

- `docs/troubleshooting/ci-runner-amd64-disk-full.md` — the other k8s-amd64-1 resource squeeze
- `docs/troubleshooting/sccache-cargo-env-cache-keys.md`, `planning/sccache-s3-observability.md`
- z800 runner repo: `multivara/ci-runners` (`compose.yaml`, `docs/troubleshooting.md`)
- z800 storage incident: `desktop-installer/docs/incidents/2026-11-19-docker-zvol-readonly-pool-full.md`
