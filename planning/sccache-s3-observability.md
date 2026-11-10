# sccache S3 observability plan

**Status: plan only, nothing implemented.** Prepared 2026-11-10 from read-only checks (kubectl
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
- Caveat: the live quota series reads 50 GiB (53,687,091,200) because commit 91bc10afb (100 GiB)
  is not pushed or synced yet; usage is already 54.3 GB. Alerts below would fire immediately if
  rolled out before that sync.

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
14-day expiry plateaus near 180 GiB, so the pending lifecycle sizing, not the alerts, must keep the
plateau under about 70% of quota. Prometheus retention (240d) covers the 15d offset. Add short
runbook text pointing at the troubleshooting doc in `annotations.description`.

## 3. Client-side stats (per-level hits, misses, L1 writes, errors)

Job step output is not in Loki; runner container stdout is (labels `namespace="ci-runners"`,
`container="runner"`, via Vector). All options need a Calypso change in
`.forgejo/actions/sccache-setup/action.yml` (and a post step) to emit one line at job end:
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

1. After the 100 GiB quota commit is pushed and synced (owner approval), confirm
   `minio_bucket_quota_total_bytes{bucket="sccache"}` reads 107,374,182,400.
2. Commit the Probe, then the rules and dashboard in `platform/minio/` (one commit each, scope
   `minio`). Push needs owner approval; ArgoCD does the apply. Validate: `helm template
   --include-crds platform/minio`, `pre-commit run`, then target `up{job="minio-bucket"}`, series
   count (about 25-125), usage vs `du -sb /export/sccache`, and a `promtool`-style dry check of rules.
3. Test alert expressions against live data in Grafana Explore before enabling `for:` durations.
4. Calypso change for `SCCACHE_STATS` (option A): owner-approved change in the Calypso repo,
   verified on one Validation job (line appears in Loki with `{container="runner"}`), then panel 6.
   If `/proc/1/fd/1` is not writable from jobs, fall back to option B.
5. After the lifecycle value is chosen (separate work), re-check the lifecycle alert threshold.

Owner approval needed: pushes, ArgoCD syncs, any Calypso change. Not needed: read-only checks.

## Unverified

- Writing to the runner container stdout from host-mode job steps (option A).
- JSON fields of `sccache --show-stats --stats-format json` per level on the pinned 0.19.0 build.
- Forgejo job-log REST availability (option C), `minio_bucket_*` scrape load on MinIO (small by
  size, not measured), Probe `?target=` query param being ignored by the bucket endpoint (it is by
  the cluster endpoint today).
