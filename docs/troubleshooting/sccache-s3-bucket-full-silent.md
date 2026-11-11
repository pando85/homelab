# sccache S3 tier silently stops writing when the bucket is full

## Problem

CI jobs show healthy compilation and no sccache errors, but the S3 tier stops growing:

- job-log `sccache --show-stats` reports `L1 (s3) writes` of 0 (or far below `L0`) and 0 write errors;
- MinIO `minio_s3_requests_4xx_errors_total` for `putobject` rises (14% of PUTs over the
  last day on 2026-11-10; the `api` label value is lowercase `putobject`);
- per-job hit rate drifts down as S3 stops backfilling, and no alert fires.

Seen on 2026-11-10: the 50 GiB `sccache` bucket filled in ~3.5 days (~12-14 GiB/day); from ~04:20Z
MinIO rejected PUTs with "Bucket quota exceeded".

## Root Cause

The bucket has a hard quota (`platform/minio/templates/sccache-config.yaml`, applied by the
`minio-sccache-provisioning` hook) and an age-based lifecycle rule (14 days at the time). Ingest outpaced
expiry, so the quota was reached. The action runs `SCCACHE_MULTILEVEL_WRITE_ERROR_POLICY=l0`, which writes the
local level first and silently drops failed secondary writes, so a full bucket is invisible to
sccache and to the job result. Reads keep working, so hits continue until entries expire. The
quota is now 200 GiB with a 7-day expiry (current values: `sccache-config.yaml`); this is the intended
degradation (jobs fall back to local cache), not a build failure.

## How to Diagnose

```bash
# Bucket usage on disk (read-only); compare with the quota in sccache-config.yaml
kubectl --context=grigri -n minio exec deploy/minio -- du -sb /export/sccache

# PUT 4xx rate (Prometheus job `minio`; cluster-level, not per bucket)
sum(increase(minio_s3_requests_4xx_errors_total{job="minio",api="putobject"}[1d]))
  / sum(increase(minio_s3_requests_total{job="minio",api="putobject"}[1d]))
```

Per-bucket series (Prometheus job `minio-bucket`, scraped from `/minio/v2/metrics/bucket`):

```promql
# Usage as a fraction of the quota (alerts at 0.70 and 0.90)
minio_bucket_usage_total_bytes{job="minio-bucket",bucket="sccache"}
  / on (bucket) minio_bucket_quota_total_bytes{job="minio-bucket",bucket="sccache"}

# Rejected PUTs for the bucket (should be 0)
sum(rate(minio_bucket_requests_4xx_errors_total{job="minio-bucket",bucket="sccache",api="putobject"}[10m]))
```

Also `minio_bucket_usage_object_total`, `minio_bucket_requests_total`, `minio_bucket_traffic_{received,sent}_bytes`.
Alerts (`platform/minio/rules/sccache.yaml`): `SccacheBucketUsageHigh`, `SccacheBucketUsageCritical`,
`SccacheBucketFullIn24h`, `SccachePutRejections`, `SccacheLifecycleNotExpiring`. Dashboard: Grafana
"sccache S3". Design and open work: `planning/sccache-s3-observability.md`.

In Loki, check per-job SCCACHE_STATS lines from the runner container. The verified pipeline is:

```logql
{namespace="ci-runners",container="runner"} |= "SCCACHE_STATS"
| regexp "SCCACHE_STATS (?P<payload>.+)"
| regexp "\"job\":\"(?P<ci_job>[^\"]+)\""
| line_format "{{.payload}}"
| json
```

Healthy: read-write jobs have `l1_writes > 0`, `cache_write_errors = 0`. If `l1_writes` is persistently
0 across jobs that compiled new crates, S3 writes are failing — compare with the PUT 4xx metric above
to confirm. See [planning/sccache-s3-observability.md](../../planning/sccache-s3-observability.md)
for the full pipeline and dashboard queries. Container jobs (e.g. `forkline-cli-release.yml`
`build-unix`) are not in Loki yet (see the planning doc, "Remaining gaps").

## Fix / Workaround

- Confirm usage is near the quota; do not purge the bucket by hand.
- Raise the quota or shorten the lifecycle expiry in `platform/minio/templates/sccache-config.yaml`
  (quota in bytes, not `100gb`), commit, push, and let ArgoCD re-run the provisioning hook.
- Rollback for the consumer is `cache-mode: disk` in the Calypso action; local caches are unaffected.
