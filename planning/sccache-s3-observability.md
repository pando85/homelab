# sccache S3 observability plan

**Status (2026-11-11): fully deployed.** Homelab parts pushed and synced 2026-11-10 (ArgoCD minio
Synced/Healthy). Client side shipped in Calypso commit c99a1901 (2026-11-10, PR #2877), verified
live 2026-11-11 (10 job lines/24h, 0 write errors). Implemented in `platform/minio/`:
`templates/probe-bucket.yaml` (job `minio-bucket`, 60s, 36 series after relabelling, `inflight` and
TTFB dropped), alerts in `templates/prometheus-rules.yaml` + `rules/sccache.yaml`
(`SccacheBucketUsageHigh`, `SccacheBucketUsageCritical`, `SccacheBucketFullIn24h`,
`SccachePutRejections`, `SccacheLifecycleNotExpiring`; quota read from
`minio_bucket_quota_total_bytes`, nothing hardcoded) and `dashboards/sccache.json` (dashboard
"sccache S3", uid `sccache-s3`, panels 20–26 wired to live Loki SCCACHE_STATS). Differences from
the sketches below: alert names/severities as listed (PUT rejections is a warning on rate > 0 for
30m, lifecycle is `info`: lowest usage of the last 7d above the highest usage 8d ago, inert until 9
days of series history exist); TTFB panels use the cluster-wide
`minio_s3_requests_ttfb_seconds_distribution` (per api), not a per-bucket one.

Original status: plan only. Prepared 2026-11-10 from read-only checks (kubectl
get/exec-curl on the MinIO pod, Prometheus and Loki via Grafana). Background:
[caching analysis](../docs/conventions/rust-build-caching.md#deployed-state-and-results),
[bucket-full incident](../docs/troubleshooting/sccache-s3-bucket-full-silent.md).

## Gap

The 2026-11-10 incident (50 GiB bucket full, PUT 4xx, `l0` hiding it from jobs) was found by hand.
Today Prometheus has only cluster-level MinIO series (`minio_s3_requests_*`, `minio_cluster_usage_*`);
no per-bucket usage, quota or request rate, no audit log, no client-side per-level stats.

## 1. Per-bucket metrics scrape

How MinIO is scraped today: the chart renders a `Probe` (`minio-cluster`, `jobName: minio`,
`prober.url: minio.minio:9000`, path `/minio/v2/metrics/cluster`, label `release: monitoring`),
not a ServiceMonitor (`includeNode` is off). `MINIO_PROMETHEUS_AUTH_TYPE=public`, so **no bearer
token** is needed.

Verified on this build (`pgsty/silo:RELEASE.2026-10-26`), from inside the pod:

| Endpoint | Result |
|---|---|
| `/minio/v2/metrics/bucket` | 200, no auth; 634 series for 4 buckets. Has `minio_bucket_usage_total_bytes`, `_usage_object_total`, `minio_bucket_quota_total_bytes`, `minio_bucket_requests_total`/`_4xx_errors_total`/`_inflight_total` (label `api`, lowercase, e.g. `putobject`), `minio_bucket_traffic_{received,sent}_bytes`, TTFB distribution |
| `/minio/metrics/v3/bucket/api/<bucket>` | 200, but one path per bucket and only api/traffic/TTFB |
| `/minio/metrics/v3/bucket/usage/...`, `.../quota/...` | 404 on this build; no list endpoint |

**Decision: v2 `/minio/v2/metrics/bucket`**, the only endpoint with usage and quota. Add a second
`Probe` next to the chart's, same pattern:

- `platform/minio/templates/probe-bucket.yaml`: `jobName: minio-bucket`, `prober.path:
  /minio/v2/metrics/bucket`, `interval: 60s` (usage comes from the scanner's usage cache, so
  faster scraping adds nothing), labels `release: monitoring`.
- Cardinality: 396 of the 634 series are `ttfb_seconds_distribution` and 72 are size/version
  distributions. Use `metricRelabelings` to `keep` only
  `minio_bucket_(usage_total_bytes|usage_object_total|quota_total_bytes|requests_total|requests_4xx_errors_total|requests_inflight_total|traffic_.*)`
  (about 25 series, all buckets) and, optionally, TTFB for `bucket="sccache"` only (~100 series).
- Quota is now 200 GiB, pushed and synced (commit 12ccf6cf9); live usage 62.3 GiB / 31% as of
  2026-11-11.

## 2. Alerts

File: `platform/minio/templates/prometheus-rules.yaml`, `PrometheusRule` with `release: monitoring`
(pattern: `apps/qbittorrent/templates/prometheus-rules.yaml`, `system/monitoring/resources/*-rules.yaml`),
group `minio-sccache.rules`, `severity: warning|critical` as elsewhere.

| Alert | Expression (sketch) | For |
|---|---|---|
| `SccacheBucketUsageHigh` warning | `usage / quota > 0.70` | 30m |
| `SccacheBucketUsageCritical` critical | `usage / quota > 0.90` | 15m |
| `SccacheBucketFillingSoon` warning | `predict_linear(usage[6h], 86400) > quota` | 1h |
| `SccachePutRejected` critical | `rate(minio_bucket_requests_4xx_errors_total{bucket="sccache",api="putobject"}[15m]) / rate(minio_bucket_requests_total{...putobject}[15m]) > 0.02` | 15m |
| `SccacheLifecycleNotExpiring` warning | `delta(usage[3d]) > 0 and usage offset 15d > 0` (bucket older than expiry + 1 day and still growing) | 6h |

Notes: use PUT only (GET 4xx is mostly normal misses: 45% of bucket GETs). Quota is enforced from
scanner usage, so usage can overshoot it slightly. The first three need a margin: at ~13 GiB/day a
14-day expiry would plateau near 180 GiB; the 7-day expiry with a 200 GiB quota plateaus near
95-145 GiB (about 48-72% of quota), which is why the expiry and quota, not the alerts, keep the
plateau under about 70% of quota on average weeks. Prometheus retention (240d) covers the 15d offset. Add short
runbook text pointing at the troubleshooting doc in `annotations.description`.

## 3. Client-side stats (per-level hits, misses, L1 writes, errors)

Job step output is not in Loki; runner container stdout is (labels `namespace="ci-runners"`,
`container="runner"`, via Vector). Option A is implemented and live: Calypso commit c99a1901
(PR #2877) emits one `SCCACHE_STATS {json}` line per job to the runner container stdout at job end:
`SCCACHE_STATS {"job":...,"runner":...,"workflow":...,"l0_hits":...,"l1_hits":...,"misses":...,"l1_writes":...,"errors":...}`
built from `sccache --show-stats --stats-format json`.

| Option | Cost | Pros | Cons |
|---|---|---|---|
| A. Also write the line to the runner container stdout, read from Loki | Action change only | No new component, secret or network path; per-job history; Vector already ships it | LogQL-derived (Grafana panels, no Loki ruler so no repo-managed alerts); depends on the job being able to write `/proc/1/fd/1` |
| B. Push to the existing Pushgateway (`monitoring`, `pushgateway-prometheus-pushgateway:9091`, no auth, scraped with `honorLabels`) | Action change plus egress to monitoring ns | Real Prometheus metrics, repo-managed alerts | Per-job grouping keys grow without bound; per-runner keys keep only the last job, so no sums; stale values |
| C. Exporter that scrapes Forgejo job logs by API | New Deployment, token, parser, state | Works retroactively | Heaviest; Forgejo job-log API on this version unverified |
| D. Runner textfile collector | Hostpath plumbing | Prometheus-native | prusik cache is a PVC, no textfile collector on the runners |

**Recommendation: A.** Cheapest, no secrets, uses the pipeline that exists. Server-side alerts in
section 2 cover the incident class; client-side stats answer "is S3 helping" and "are L1 writes
zero". If repo-managed client alerts are wanted later, add B as a gauge-only follow-up
(`last job L1 writes == 0` per runner), not as the primary path.

## 4. Dashboard

`platform/minio/dashboards/sccache.json` (picked up by the glob in
`platform/minio/templates/dasbhoards.yaml`; `overview.json` is 98 KB, so check the ConfigMap size
or use a second ConfigMap). Panels:

1. Bucket usage vs quota (bytes, quota as threshold line) and percent used.
2. Usage growth per day and predicted time to full.
3. Object count and ingest bytes/s (`traffic_received`).
4. sccache PUT/GET request rate by `api`; PUT 4xx ratio.
5. GET/PUT TTFB p50/p99 (sccache bucket).
6. Loki: hits by level (L0 vs S3), per-job hit rate, `L1 writes == 0` job count, errors. Needs A.

## 5. Rollout, validation, approvals

1. Quota is 200 GiB, pushed and synced. `minio_bucket_quota_total_bytes{bucket="sccache"}` reads
   214,748,364,800.
2. Probe, rules and dashboard in `platform/minio/` committed, pushed, ArgoCD-synced.
3. Alert expressions tested against live data in Grafana Explore before enabling `for:` durations.
4. Calypso change for `SCCACHE_STATS` (option A) shipped in commit c99a1901 (PR #2877, 2026-11-10),
   verified live 2026-11-11 (10 jobs/24h, 0 write errors). Dashboard panels 20–26 wired to Loki.
5. After the lifecycle value is chosen (separate work), re-check the lifecycle alert threshold.

Owner approval needed: pushes, ArgoCD syncs, any Calypso change. Not needed: read-only checks.

## Unverified

- JSON fields of `sccache --show-stats --stats-format json` per level on the pinned 0.19.0 build.
- Forgejo job-log REST availability (option C), `minio_bucket_*` scrape load on MinIO (small by
  size, not measured), Probe `?target=` query param being ignored by the bucket endpoint (it is by
  the cluster endpoint today).

## Remaining gaps (2026-11-11)

1. **No client-side alerting yet.** No Loki ruler rules; deferred until the container-job coverage
   mechanism is chosen. The container-job path is now complete (see Closed gaps below), so
   client-side alerting can be evaluated as Phase 3 work.
2. **`SccacheLifecycleNotExpiring` inert** until ~2026-11-19. The alert needs 8-day-offset history;
   the series started 2026-11-10T13:07Z.

## Closed gaps (2026-11-11)

1. **Container jobs now in Loki.** Calypso PR #2903 (merged) implemented direct HTTP push from
   container jobs to Loki (`.ci/sccache-stats-line.sh`, `/.dockerenv` branch). The push URL is
   `http://loki.loki.svc:3100/loki/api/v1/push` (auth_enabled false). The homelab runner config
   was updated in two commits: `e43f34228` added `container.options` mounts for `/cache` and
   `/sccache-credentials`; `ca27baae0` added them to `container.valid_volumes` (the runner
   validates both allow-lists; with `valid_volumes: []` the mounts were silently dropped — see
   `docs/troubleshooting/forgejo-runner-valid-volumes.md`). End-to-end verification: test run
   75307 on ci-runner-0 (prusik) DinD — mounts accepted, pin current via jq, slot0 leased with
   disk+s3 levels, and `loki: pushed (container job)`. The line is queryable in Loki: stream
   `{namespace="ci-runners",source="sccache-stats-push"}`, payload
   `container-sccache run=75307 runner=ad70c092f872 cr=6 hits=0 w=0 err=0`. This is the first
   proven container-job stats delivery.
2. **z800 workstation coverage complete.** z800 reports to Loki in both host-mode and docker-mode
   jobs. Commit `7e5ff8f` set `SCCACHE_STATS_LOKI_URL=https://loki.internal.grigri.cloud` in both
   the compose environment (host-mode jobs) and runner.envs (docker-mode jobs) — cluster DNS
   `loki.loki.svc` does not resolve on z800. Commit `4cadba9` added docker-mode `/cache` +
   credentials mounts via a `@@SECRETS_DIR@@` placeholder rendered by `scripts/render-secrets.sh`
   to the repo's secrets dir (host paths). Both applied and verified. Caveat: z800's docker storage is a fixed 50G ZFS zvol (manjaro/docker) with no docker-gc (unlike the cluster runners), so it can fill up and block ALL docker-mode jobs — observed 2026-11-11 when 100% full made apt fail to install node, failing the strict test until pruned; a durable fix (GC timer or zvol resize) is deferred.
3. **Strict container test + CARGO_INCREMENTAL fix.** Calypso PR #2909 (merged, `68cc7d28`)
   installed `jq` and `curl` in the container workflows (the `cargo-zigbuild` image lacks them;
   `sccache-pin.sh` needs `jq` on every path; `curl` is the S3 health probe — missing it silently
   falls back to disk mode). The test workflow `sccache-container-test.yml` is now STRICT: it
   asserts the binary exists, a slot is leased, and `compile_requests > 0`, failing loudly on
   mismatch. Calypso PR #2911 (merged, `6e0ba2beb`) sets
   `CARGO_INCREMENTAL="0"` on the test build step — the throwaway `cargo new` dev-profile build
   enables incremental compilation, which sccache never caches (run 75307 had all 6 requests land
   in `requests_not_cacheable`, failing the `compile_requests > 0` assertion even though delivery
   worked).

## Verified LogQL pipeline

The base pipeline for extracting JSON fields from `SCCACHE_STATS` lines in Loki:

```logql
{namespace="ci-runners",container="runner"} |= "SCCACHE_STATS"
| regexp "SCCACHE_STATS (?P<payload>.+)"
| regexp "\"job\":\"(?P<ci_job>[^\"]+)\""
| line_format "{{.payload}}"
| json
```

Two pitfalls that broke naive approaches:

1. **Plain `| json` extracts nothing.** The line has the prefix `SCCACHE_STATS ` before the JSON
   object, so the json parser sees invalid input. The first `regexp` strips the prefix into a
   `payload` capture, then `line_format` replaces the line with just the JSON before `| json`.
2. **The JSON field `job` collides with the Loki stream label `job`.** The json parser drops it
   silently. Extract it via a second `regexp` as `ci_job` before the json stage.

After the pipeline, available JSON fields: `v`, `repo`, `workflow`, `ci_job` (from regexp),
`run_id`, `event`, `ref`, `runner`, `slot`, `mode`, `s3_write_mode`, `normalize_paths`,
`compile_requests`, `l0_hits`, `l1_hits`, `hits_total`, `misses`, `l1_writes`,
`cache_write_errors`, `cache_read_errors`. All numeric fields are integers; `null` means absent/N/A.
