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

- **Export freshness**: each run must cover data no older than the recorder's
  declared retention, with margin. Enum history not extracted within that window is
  permanently lost — unlike the Prometheus archive, this source cannot be re-queried
  later.
- **Overlap dedup**: extraction windows may overlap between consecutive runs. Raw part files are append-only and *do* contain duplicated rows across overlapping pulls (each run stamps its own `extraction_id`); deduplication happens deterministically at **build** time, not in the raw store. The natural keys are the full original label set plus the true sample timestamp for Prometheus rows, and `entity_id` plus `last_changed` for HA history rows. Because the volume is negligible, re-pulling an overlapping window is cheap and safe.
- **Unfinished runs produce no coverage**: a run that fails or is interrupted before writing its manifest does not count toward coverage. The next run must re-cover the same window. Orphaned raw parts from a failed run are therefore possible and must be ignored by consumers that resolve coverage from manifests rather than from part-file listing.
- **Empty chunks are recorded, not absorbed**: each successful run's manifest declares how
  many chunks it asked for, how many parts it wrote, and names every zero-row chunk in
  `quality_flags` plus `params.chunk_outcomes`. Treat a `chunk_accounting_mismatch` or
  `empty_chunk` flag as coverage that must be re-attempted while the window may still exist.

#### Recorder retention: declared, not measured

The retention that freshness is sized against comes from configuration, not from inference
off extracted rows:

- **Declared:** [`apps/home-assistant/config/configuration.yaml`](../apps/home-assistant/config/configuration.yaml)
  has a `recorder:` block that sets **only** `db_url`. There is no `purge_keep_days`, no
  `commit_interval`, and no `exclude`/`include` anywhere in that file or in the packages it
  includes (`apps/home-assistant/config/integrations/`), so Home Assistant's documented
  defaults apply — `purge_keep_days: 10`, `auto_purge: true` (nightly at 04:12 local),
  `commit_interval: 5`, no entity filter
  ([HA recorder integration reference](https://www.home-assistant.io/integrations/recorder/)).
  No filter gates the appliance enum entities, so they are recorded. That declared 10 days is
  the only retention figure this plan treats as fact.
  [ADR-0003 §"Appliance enum telemetry — resolved"](../docs/adr/0003-phase1-measurement-and-baseline.md)
  separately records an audit in which the recorder's `states` window measured 10.19 days; that
  is a table-wide observation from a different audit, not a measured coverage boundary for these
  entities, and this plan does not rely on it.
- **Observed on the 2026-10-23 run** (manifest `750bce0e-6956-472b-9f0b-172ce4936a86`,
  source `ha_history`, window 2026-10-14 → 2026-10-23T10:59:56Z): 10 chunks declared,
  9 parts written, 39 rows; the 2026-10-14 chunk returned nothing and `quality_flags` was
  `[]`. The earliest observed `last_changed` across those rows is 2026-10-15T11:39:47Z,
  with per-entity earliest on 10-15 and 10-17, and 14 of the 39 rows carry `carry_in=True`.
  These are the facts; the declared figure above is not derived from them.
- **What the declared retention already rules out:** the nightly purge keeps 10 days relative to
  the purge, so at the 2026-10-23 run the declared window reached back to roughly 2026-10-13. The
  empty 2026-10-14 chunk therefore sits **inside** the declared retention and is not explained by
  it. That is arithmetic from configuration, not an inference from extracted rows — and it is a
  further reason not to read a purge boundary out of a run's data.
- **Unverified:** the **effective coverage boundary**. A "~8 days, measured" figure is **not
  established** and should not be repeated. An earliest *observed* row is not a global
  retention boundary — the appliances may simply not have changed state before it — and one
  empty chunk has at least three causes (no transitions in the window, already purged, or a
  degenerate empty response) that the HA history API does not distinguish. The extractor
  records all three possibilities without selecting one, and does not probe retention or write
  a synthetic state to find out, because that would change recorder configuration, which
  ADR-0003 forbids in this phase. The effective boundary **could be shorter than the declared
  10 days** (a failed or lagging purge, or missing rows, is not ruled out); nothing here proves
  it is, and nothing here proves it is not. Establishing it requires reading the recorder
  database directly (read-only), which is out of scope for this phase.

The operational conclusion does not depend on which number is right, and is unchanged: enum
history must be extracted on a recurring schedule **well inside the declared retention**,
because it cannot be recovered later, and that recurring production schedule is **still not
implemented** — this document remains a plan, and the extraction has only ever been run
manually.

### 2. Deterministic Immutable Manifests

- Each extraction records a reproducible manifest: schema version, code version, config version, extraction timestamp, stable input identity and content hash, units, data-quality flags, UTC start/end with actual duration.
- Manifests are immutable once written. Re-extraction of the same window produces a new manifest with a new `extraction_id` and extraction timestamps. Part **content hashes will differ between runs even for identical source data**, because `extraction_id` is stored on every row; reproducibility is therefore asserted at the *value* level (same samples, same true timestamps) and not by byte-identical files.
- The canonical Parquet store is append-only; old partitions are never overwritten in place.

### 3. CronJob Specification

- **`concurrencyPolicy: Forbid`** — no overlapping jobs. If a run is still active when the next schedule fires, the new run is skipped.
- **Retries**: `backoffLimit` configured to retry transient failures (network timeouts, HA API 5xx). Permanent failures (auth, schema mismatch) must not retry indefinitely — set a reasonable limit (e.g. 3–5) and alert on exhaustion.
- **Timeouts**: `activeDeadlineSeconds` set to bound runaway jobs. The timeout must be generous enough for a full backfill but short enough to detect hangs.
- **Schedule**: to be determined when the CronJob is created. Daily is comfortable against the declared 10-day retention and stays comfortable even if the effective boundary turns out to be shorter; the exact cron expression is a deployment-time decision.

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

- **Scheduler heartbeat alert**: a PrometheusRule that fires when the CronJob has not completed a successful run within a configurable threshold (e.g. `> 2 days` since last successful completion). The threshold must leave a recovery window before the declared retention expires — sized against the 10 days in config, which is the only figure this plan can cite.
- **Silent-gap alerting**: a job that exits 0 having skipped chunks is a data-loss event for this source, so alert on the manifest's chunk accounting (`params.chunk_accounting.mismatch`, `empty_chunk:*` and `chunk_accounting_mismatch` quality flags), not only on job failure.
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
- [ ] Alert on per-chunk accounting flags (`chunk_accounting_mismatch`, `empty_chunk:*`), not only on job failure
- [ ] Verify extraction covers a window inside the declared retention (10 days per `recorder` config, no override) with margin on first run
- [ ] If a tighter bound is needed, establish the effective retention boundary from a **read-only** recorder database query — do not infer it from the earliest extracted row
- [ ] Document the Vault path and PVC size in this file (update from "to be determined")

## References

- [ADR-0003 — Phase 1 measurement and baseline](../docs/adr/0003-phase1-measurement-and-baseline.md)
- [Deploying new apps](../docs/conventions/deploying-new-apps.md)
- [Cluster hygiene — backup labels](../docs/troubleshooting/cluster-hygiene.md)
- [OpenEBS ZFS slow startup with fsGroup](../docs/troubleshooting/openebs-zfspv-slow-startup-fsgroup.md)
