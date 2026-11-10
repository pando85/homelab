# CI runners and sccache S3: follow-up checks

## Overview

Follow-up checklist for the CI runner and shared sccache S3 work done on 2026-11-10. The changes
are deployed. This file records what still has to be verified, when, and how. Each item is a
read-only check unless it says otherwise. Fixes go through git and Argo CD, never by hand on a
host or with `kubectl apply`.

Related documents:

- [Shared S3 sccache deployment plan](shared-s3-sccache-deployment.md)
- [sccache S3 observability plan](sccache-s3-observability.md)
- [Rust build caching](../docs/conventions/rust-build-caching.md)
- [sccache bucket full, silent write loss](../docs/troubleshooting/sccache-s3-bucket-full-silent.md)
- [ci-runner amd64 disk full](../docs/troubleshooting/ci-runner-amd64-disk-full.md)

## Context: deployed on 2026-11-10

| Change | Where |
|---|---|
| `sccache` bucket: 200 GiB hard quota, 7-day expiry | `platform/minio/templates/sccache-config.yaml` |
| Per-bucket MinIO scrape (`job="minio-bucket"`), `Sccache*` alerts, "sccache S3" dashboard | `platform/minio/` |
| Renovate updates the runner images of both statefulsets in one group | `.github/renovate.json5` |
| Legacy `/cache/sccache` removed on both runners | runner start script |
| docker-gc: no image/build-cache pruning while a job runs; budget on Docker's own usage; BuildKit cache cap 8 GB | `platform/ci-runners/resources/` |
| amd64 runner CPU limit 5 → 8, DinD 3 CPU/4.5Gi → 2 CPU/2Gi | `statefulset-amd64.yaml` |
| Calypso: path normalisation everywhere, PR jobs read-only on S3, image cleanup after push | Calypso #2872, #2874 |

Baseline before the changes (2026-11-03 to 2026-11-10):

- sccache bucket writes: 11.8 GiB/day average over full days, 22 GiB on a weekday, at least
  25.9 GiB in the peak 24 h.
- amd64 runner container: CPU throttled 15.4% of periods, CPU peak at the 5-core limit.
- prusik runner PVC: 79–90% used. Docker used about 37 GB of it, including about 36 GB of
  anonymous volumes.
- Three `Build Unix targets` jobs failed with `No such image` after a pull that overlapped a
  docker-gc prune. A re-run on 2026-11-10 12:58Z, after pruning stopped during jobs, passed.

## Check on 2026-11-11 (24 h after rollout)

- [ ] **amd64 throttling dropped.** Compare with the 15.4% baseline:
  `sum(rate(container_cpu_cfs_throttled_periods_total{namespace="ci-runners",pod=~"ci-runner-amd64-.*",container="runner"}[1d])) / sum(rate(container_cpu_cfs_periods_total{namespace="ci-runners",pod=~"ci-runner-amd64-.*",container="runner"}[1d]))`.
  Also confirm the runner never OOMKilled and node `MemAvailable` on k8s-amd64-1 stayed above
  1 GB.
- [ ] **prusik PVC back under 75%.** Check `df -h /workspace` and `docker system df` in the
  `ci-runner-0` daemon container. The anonymous volumes should be gone and the build cache at or
  under 8 GB. If not, see the docker-gc busy-starvation fix (prune volumes and containers while
  jobs run) and confirm it is deployed.
- [ ] **No `No such image` failures.** In Loki, `{namespace="ci-runners"} |= "No such image"`.
  Also re-check the DinD daemon log for `lease does not exist` errors.
- [ ] **No rejected S3 uploads.** `sum(increase(minio_bucket_requests_4xx_errors_total{bucket="sccache",api="putobject"}[1d]))`
  should be 0, and no `Sccache*` alert fired.
- [ ] **S3 write volume falling.** `sum(increase(minio_bucket_traffic_received_bytes{bucket="sccache"}[1d]))`
  should be below the 11.8 GiB/day baseline on a comparable weekday. The first day after #2872
  includes one cold rebuild of the release jobs.
- [ ] **Client-side stats arriving** (once Calypso #2876 is merged).
  `{namespace="ci-runners", container="runner"} |= "SCCACHE_STATS"` should return one line per
  sccache job. PR jobs should report `l1_writes = 0`.

## Investigate by 2026-11-14 (read-only)

- [ ] **amd64 loses its Docker images on restart.** After the 2026-11-10 restarts,
  `docker system df` in `ci-runner-amd64-0` showed 0 images and 0 volumes, with only the build
  cache left, although `/var/lib/docker` is the hostPath `/srv/ci-runner/docker`. Find out
  whether the containerd image store metadata is lost, a startup step wipes images, or the
  docker-gc emergency tier ran. Every rollout currently forces all base images to be pulled again.
- [ ] **Forgejo connection errors from the runners.** 541 `failed to fetch task` errors in the
  7 days to 2026-11-10, plus `connection refused` to Forgejo (192.168.193.4:443) at
  2026-11-10 06:09Z. Correlate with Forgejo pod restarts, Envoy Gateway or ingress events, and
  node events. Effect: late job pickup and lost `ReportState`/`ReportLog` updates.
- [ ] **Duplicate node-exporter scrapes.** Each node is scraped by two node-exporter pods with
  the same `instance` label, so node metrics are doubled. Find the second source (DaemonSet,
  Probe or static config) and remove it through git.
- [ ] **Unexplained runner restarts.** 26 prusik runner restarts on 2026-10-30, and
  `kube_pod_container_status_last_terminated_reason` returns no data. Check whether the reason
  metric is missing from kube-state-metrics, and whether restarts continue.

## Review on 2026-11-17 (one week of bucket data)

- [ ] **Expiry and margin.** Rule of thumb: quota ≥ 2 × busy-day GiB × (expiry days + 1). With
  200 GiB and 7 days, the busiest 8-day window of writes should stay at or under 100 GiB:
  `max_over_time(sum(increase(minio_bucket_traffic_received_bytes{bucket="sccache"}[8d]))[2d:1h])`.
  If it stays under, consider a longer expiry only when hit rates show benefit. If it goes over,
  shorten the expiry or raise the quota (MinIO had 2.6 TB free on 2026-11-10).
- [ ] **`SccacheLifecycleNotExpiring`** can evaluate from about 2026-11-19 (it needs 9 days of
  history). Confirm the bucket usage curve flattens once the first 7-day expiries run, from
  about 2026-11-14.
- [ ] **Local cache slot size.** The legacy cache removal freed 10 GiB per runner. If the
  `SCCACHE_STATS` lines show S3 hits concentrated on jobs with an empty or evicted local slot,
  raise the Calypso action's slots from 2×5 GiB toward 2×7–8 GiB. Watch prusik's 64 GiB PVC.
- [ ] **Short jobs waiting behind Rust builds.** prusik runs one job at a time and the median
  job lasts about 6 s. Decide whether a small runner label for change detection and lint is
  worth it. A queue-time measurement from the Forgejo API would back the decision.

## Evaluated and rejected: cargo-nextest (2026-11-10)

Measured locally on a Calypso worktree (cargo-nextest 0.9.151, same build artifacts, 4065 tests
in 11 binaries, identical counts and skips under both runners, 3 alternating rounds each):

| Scenario | `cargo test` median | nextest median | Change |
|---|---|---|---|
| Postgres, pinned to 8 cores (amd64-like) | 616.8 s | 610.4 s | -1.0% |
| Postgres, pinned to 14 cores (prusik-like) | 382.7 s | 374.4 s | -2.2% |
| Postgres, 32 cores, no cap | 406.9 s | 306.9 s | -24.6%, but 2 of 3 runs failed |

- No gain at CI core counts. `calypso-control-plane` holds ~98% of test time and already
  saturates the cores, so scheduling across binaries adds nothing.
- Process-per-test bypasses the 12-database limit in
  `crates/calypso-control-plane/src/db/test_support.rs` (`MAX_CONCURRENT`). Postgres hit
  "too many clients already" at 32 threads. The fix (a 12-thread cap) also removes the gain.

Follow-ups for Calypso, if test time is to be cut:

- [ ] `api::public_api::tests::billing_routes_need_billing_read_scope` takes 126-137 s by itself:
  it builds a full `AppState` 12 times without a database. It is the floor of the test phase.
- [ ] The ~30-40 s Postgres-backed tests (for example runner-display `stage2_*`).
- [ ] `scripts/ci/run-with-transient-retry.sh` would treat a failing test whose output contains
  `error: failed to open` as a transient build error and retry it three times. Only relevant if a
  runner other than `cargo test` is adopted.

## Later

- [ ] **Separate disk or volume for `/srv/ci-runner` on k8s-amd64-1**, so CI cannot push the
  node into kubelet DiskPressure. This is a hardware or layout decision.
- [ ] **Close out the plans.** Mark the remaining phases of `shared-s3-sccache-deployment.md`
  and `sccache-s3-observability.md` as done, and move durable content to `docs/`.
