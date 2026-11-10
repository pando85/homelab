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
`minio-sccache-provisioning` hook) and a 14-day age-based lifecycle rule. Ingest outpaced expiry, so
the quota was reached. The action runs `SCCACHE_MULTILEVEL_WRITE_ERROR_POLICY=l0`, which writes the
local level first and silently drops failed secondary writes, so a full bucket is invisible to
sccache and to the job result. Reads keep working, so hits continue until entries expire. The
quota was raised to 100 GiB (a shorter lifecycle expiry is being sized); this is the intended
degradation (jobs fall back to local cache), not a build failure.

## How to Diagnose

```bash
# Bucket usage on disk (read-only); compare with the quota in sccache-config.yaml
kubectl --context=grigri -n minio exec deploy/minio -- du -sb /export/sccache

# PUT 4xx rate (Prometheus job `minio`; cluster-level, not per bucket)
sum(increase(minio_s3_requests_4xx_errors_total{job="minio",api="putobject"}[1d]))
  / sum(increase(minio_s3_requests_total{job="minio",api="putobject"}[1d]))
```

In a CI job log, check the `L1 (s3) writes` line of the sccache stats: persistent 0 across jobs that
compiled new crates means S3 writes are failing. Per-bucket usage and quota metrics are not
scraped yet; see `planning/sccache-s3-observability.md`.

## Fix / Workaround

- Confirm usage is near the quota; do not purge the bucket by hand.
- Raise the quota or shorten the lifecycle expiry in `platform/minio/templates/sccache-config.yaml`
  (quota in bytes, not `100gb`), commit, push, and let ArgoCD re-run the provisioning hook.
- Rollback for the consumer is `cache-mode: disk` in the Calypso action; local caches are unaffected.
