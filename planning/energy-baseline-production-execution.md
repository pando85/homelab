# Energy Baseline — Production Execution Plan

- **Status:** NOT YET SCHEDULED
- **Date:** 2026-10-23
- **Extends:** [ADR-0003](../docs/adr/0003-phase1-measurement-and-baseline.md)
- **Related:** [Deploying new apps](../docs/conventions/deploying-new-apps.md)

## Purpose

This document captures the concrete requirements for running the energy-baseline extraction pipeline as a **recurrent production CronJob** in the cluster. It is a planning reference — no manifests are committed, no resources are deployed, and no scheduling decision has been made. Implementation begins only when the CLI package is mature and the requirements below are reviewed and approved.

## Current State

- **Development**: supervised offline work on a local PC, using `/tmp` for temporary exports. No cluster deployment, no persistent storage.
- **Production**: not yet scheduled. This document defines the requirements that must be met before any GitOps commit deploys the CronJob.

Temporary development exports in `/tmp` are **not durable** and do not meet production preservation requirements. The production path requires persistent storage with backup.

## Scope Boundary

| In scope | Out of scope |
|----------|-------------|
| Scheduled batch extraction CronJob | Deployed long-running service |
| Persistent PVC on prusik | Live battery/controller/HA config changes |
| GitOps-committed manifests | Ad-hoc `kubectl apply` |
| Enum state extraction from HA recorder | Optimiser deployment or live actuation |

**No live battery control, controller decisions, or Home Assistant configuration changes are introduced by this plan.** The CronJob extracts data; it does not control anything.

## Concrete Requirements

### 1. Freshness and Coverage

- **Export freshness**: each run must cover data no older than 10 days from the current date. The 10-day recorder window is a hard deadline — enum history not extracted within it is permanently lost.
- **Overlap dedup**: extraction windows may overlap between consecutive runs. Raw part files are append-only and *do* contain duplicated rows across overlapping pulls (each run stamps its own `extraction_id`); deduplication happens deterministically at **build** time, not in the raw store. The natural keys are the full original label set plus the true sample timestamp for Prometheus rows, and `entity_id` plus `last_changed` for HA history rows. Because the volume is negligible, re-pulling an overlapping window is cheap and safe.
- **Unfinished runs produce no coverage**: a run that fails or is interrupted before writing its manifest does not count toward coverage. The next run must re-cover the same window. Orphaned raw parts from a failed run are therefore possible and must be ignored by consumers that resolve coverage from manifests rather than from part-file listing.

### 2. Deterministic Immutable Manifests

- Each extraction records a reproducible manifest: schema version, code version, config version, extraction timestamp, stable input identity and content hash, units, data-quality flags, UTC start/end with actual duration.
- Manifests are immutable once written. Re-extraction of the same window produces a new manifest with a new `extraction_id` and extraction timestamps. Part **content hashes will differ between runs even for identical source data**, because `extraction_id` is stored on every row; reproducibility is therefore asserted at the *value* level (same samples, same true timestamps) and not by byte-identical files.
- The canonical Parquet store is append-only; old partitions are never overwritten in place.

### 3. CronJob Specification

- **`concurrencyPolicy: Forbid`** — no overlapping jobs. If a run is still active when the next schedule fires, the new run is skipped.
- **Retries**: `backoffLimit` configured to retry transient failures (network timeouts, HA API 5xx). Permanent failures (auth, schema mismatch) must not retry indefinitely — set a reasonable limit (e.g. 3–5) and alert on exhaustion.
- **Timeouts**: `activeDeadlineSeconds` set to bound runaway jobs. The timeout must be generous enough for a full backfill but short enough to detect hangs.
- **Schedule**: to be determined when the CronJob is created. Daily is comfortable against a 10-day window; the exact cron expression is a deployment-time decision.

### 4. Credentials and Secrets

- **HA API token**: retrieved via ExternalSecret from Vault, mounted as environment variables (`envSecret` references in the pod spec). The token is **never copied to files** on the PVC or in the container filesystem.
- **Credentials in memory only**: the process reads secrets from process environment (populated by Kubernetes from the ExternalSecret). No secret material is written to disk, logged, or included in manifests.
- **Vault path**: to be determined (consistent with existing ExternalSecret patterns in the cluster, e.g. `personal/`, `work/`, `web/` prefixes in `pass`).

### 5. Storage — PVC on prusik

- **Node affinity**: the PVC is backed by `openebs-zfspv` (or `fast-zfspv`), which is node-local. The CronJob pod **must** have a `nodeAffinity` or `nodeSelector` targeting prusik. No other node can mount a prusik-local PV.
- **PVC reclaim policy**: `Retain` (consistent with existing `openebs-zfspv` StorageClass defaults). The PV is not auto-deleted on PVC removal.
- **ArgoCD prune**: `Prune=false` on the PVC resource annotation to prevent ArgoCD from deleting it on sync. Data loss from a prune would be irreversible.
- **Backup metadata**: the PVC must carry backup labels (`backup/retain`, `backup`) according to existing cluster conventions. A PVC created without these labels is silently never backed up.
- **Size**: to be determined based on measured extraction volume. Development in `/tmp` provides no durable archive; production sizing requires measuring actual Parquet output over a representative window.

### 6. Observability and Alerting

- **Scheduler heartbeat alert**: a PrometheusRule that fires when the CronJob has not completed a successful run within a configurable threshold (e.g. `> 2 days` since last successful completion). The threshold must leave a recovery window before the 10-day hard deadline.
- **Job metrics**: `kube_job_status_failed`, `kube_job_status_succeeded`, and `kube_job_complete` are available from kube-state-metrics. The alert queries these to detect missed runs.
- **Pod logs**: captured by Vector and shipped to Loki (standard cluster path). No special logging configuration required beyond structured output from the CLI.

### 7. GitOps and Deployment

- All manifests committed to git and deployed via ArgoCD. No `kubectl apply` for production resources.
- Follow conventions from [deploying-new-apps.md](../docs/conventions/deploying-new-apps.md) when choosing chart patterns and structure.
- The CronJob is a batch extractor, not a service. It does not expose ports, does not have a Service/Ingress, and does not participate in mesh routing.

## What This Document Does NOT Do

- **Does not schedule the CronJob.** The schedule, image tag, and resource requests are deployment-time decisions.
- **Does not deploy anything.** No manifests are committed. No resources exist in the cluster.
- **Does not change Home Assistant configuration.** Recorder retention, include/exclude filters, and all HA settings remain unchanged.
- **Does not introduce live actuation.** No battery control, no controller decisions, no dispatch commands.

## Checklist for Implementation

When the CLI is mature and this plan is approved, the implementation commit must:

- [ ] Decide the container image build, registry and tagging strategy (new work — the repo publishes no Python images today)
- [ ] Create the CronJob manifest under `apps/` (Kubernetes workloads live there; `scripts/` is host-local utilities only)
- [ ] Create the PVC with `openebs-zfspv` StorageClass, `Retain` reclaim policy, prusik node affinity
- [ ] Annotate PVC with `argocd.argoproj.io/sync-options: Prune=false`
- [ ] Add backup labels (`backup/retain`, `backup`) per cluster conventions
- [ ] Create ExternalSecret for HA API token from Vault
- [ ] Reference the ExternalSecret via `envSecret` in the CronJob pod spec (never copy to files)
- [ ] Set `concurrencyPolicy: Forbid`, `backoffLimit`, `activeDeadlineSeconds`
- [ ] Create PrometheusRule for scheduler heartbeat alert
- [ ] Verify extraction covers a window < 10 days old on first run
- [ ] Document the Vault path and PVC size in this file (update from "to be determined")

## References

- [ADR-0003 — Phase 1 measurement and baseline](../docs/adr/0003-phase1-measurement-and-baseline.md)
- [Deploying new apps](../docs/conventions/deploying-new-apps.md)
- [Cluster hygiene — backup labels](../docs/troubleshooting/cluster-hygiene.md)
- [OpenEBS ZFS slow startup with fsGroup](../docs/troubleshooting/openebs-zfspv-slow-startup-fsgroup.md)
