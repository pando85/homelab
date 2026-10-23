# Energy Baseline — Production Execution Plan

- **Status:** NOT YET SCHEDULED (automated) — interim manual extraction cadence in force since 2026-10-23
- **Date:** 2026-10-23
- **Extends:** [ADR-0003](../docs/adr/0003-phase1-measurement-and-baseline.md)
- **Related:** [Deploying new apps](../docs/conventions/deploying-new-apps.md)

## Purpose

This document captures the concrete requirements for running the energy-baseline extraction pipeline as a **recurrent production CronJob** in the cluster. It is a planning reference — no manifests are committed, no resources are deployed, and no scheduling decision has been made. Implementation begins only when the CLI package is mature and the requirements below are reviewed and approved.

It also records the **interim manual extraction cadence** adopted on 2026-10-23, which is an operating rule for the private store rather than a deployment: see [Interim manual extraction cadence](#interim-manual-extraction-cadence).

## Current State

- **Development**: supervised offline work on a local PC. Temporary exports go to `/tmp`; the durable store is the private data directory (`$ENERGY_BASELINE_DATA_DIR`, default `~/data/energy-baseline/`), which `paths.py` refuses to place inside a git worktree. No cluster deployment, no cluster storage.
- **Production**: automated scheduling is **still not implemented**. The GitOps CronJob and its `openebs-zfspv` PVC on prusik remain **deferred, not cancelled**, and the requirements below still stand for when they are created. What changed on 2026-10-23 is that extraction is no longer ad-hoc: a short manual cycle from the development PC is now the operating arrangement.
- **Canonical store**: the HA enum parts are preserved but **not yet consumed** — `build.py` has no HA reader. See [Known state of the private store](#known-state-of-the-private-store).

Temporary development exports in `/tmp` are **not durable** and do not meet production preservation requirements. The production path requires persistent storage with backup. The interim store — the private data directory on the development PC — is durable across runs but is outside the cluster's backup path entirely, which is one more reason the CronJob plus PVC remains the end state.

## Interim manual extraction cadence

**Decision, 2026-10-23.** Asked how to stop Home Assistant enum history from silently aging out of the recorder, the owner chose **manual extraction on a short cycle from the development PC**. The alternatives were pulling the cluster CronJob forward (declined — the CLI is not mature and the GitOps deployment is a deliberate later step) and accepting the loss (declined — recorder rows are the one data class that cannot be recovered). The GitOps CronJob and its `openebs-zfspv` PVC on prusik therefore remain **deferred, not cancelled**; the requirements in sections 3–7 below still apply when they are scheduled.

### Operating rule

- **Declared retention is 10 days.** [`apps/home-assistant/config/configuration.yaml`](../apps/home-assistant/config/configuration.yaml) gives the `recorder:` block **only** a `db_url` (lines 13-14): there is no `purge_keep_days`, no `auto_purge`, no `commit_interval` and no `exclude`/`include` in that file or in the packages it includes (`apps/home-assistant/config/integrations/`), so Home Assistant's documented default applies — `purge_keep_days: 10` ([HA recorder integration reference](https://www.home-assistant.io/integrations/recorder/)). That declared figure, not an inference from extracted rows, is what the cadence is sized against.
- **Re-extract every 3 days, pulling a window 10 days deep.** The cadence is 3 days; the window depth is what makes a missed run heal. Binding constraint: **no more than the declared 10 days may elapse between two successful runs**, because a row purged from `states` is gone for good. Two consecutive missed runs still leave a 9-day gap — inside the boundary, and the next pull's 10-day window re-covers everything the recorder still holds, so nothing is lost. Three consecutive missed runs (12 days) is where the declared retention starts eating rows nobody pulled, which is the failure mode automation exists to catch. Daily also satisfies the rule (ADR-0003 calls it comfortable); 3 days is chosen as the loosest cadence that still tolerates a forgotten week plus one, and with a few hundred rows a day the cost of overlapping pulls is not the constraint.
- **Overlap deliberately.** Each pull re-requests windows already pulled. That is safe only because of row-level dedup (next bullet) and only while the chunk width does not change (see [Chunk width must stay constant between runs](#chunk-width-must-stay-constant-between-runs)).
- **Overlapping re-extraction does not duplicate rows.** `extract_ha.py` matches every candidate row against what is **already stored** on the key `ROW_IDENTITY_FIELDS = (entity, last_changed, last_updated, state, attributes_json)`, deliberately excluding the run-specific provenance columns `extraction_id`, `carry_in` and `source_window_start`. Prior coverage is discovered by scanning the **part files already on disk** (`load_stored_row_index`), not by window arithmetic — so a partially failed earlier run cannot open a gap, because its surviving parts still count as covered. Suppression is disclosed per chunk and in `quality_flags` (`duplicate_rows_suppressed`, with counts in `params.row_accounting`) as an **expected, benign accounting note, not an alarm**, kept distinct in prefix and wording from the genuine-problem flags (`empty_chunk:`, `chunk_accounting_mismatch`, `no_rows_for_entity:`). A third run over an already fully stored window writes **zero new parts and zero rows**. Parts stay append-only: dedup means *not writing*, never rewriting or deleting. Full detail in [scripts/energy-baseline/README.md](../scripts/energy-baseline/README.md).

### Chunk width must stay constant between runs

This is a real caveat and it is the kind of thing that silently inflates a store: **carry-in rows are synthesised at chunk *start* anchors, so a run that uses a different chunk width legitimately creates new anchors and writes genuinely new rows.** Those rows are not duplicates under the identity above, so dedup will not suppress them. Overlapping pulls are free **only while the chunk width is unchanged**.

The width is `DEFAULT_CHUNK_SECONDS = 86400` (24 h) and the `extract` CLI exposes no flag to change it, so in practice the rule is: do not change the constant, and do not pass a different `chunk_seconds` to a programmatic call. Two disclosures tell you if the rule has been broken — `carry_in_classification_disagreement:<window>` (a suppressed duplicate would have been classified as carry-in differently from the stored copy, i.e. the chunk boundaries did not line up) and `duplicate_rows_already_stored:<n>` (the prior parts themselves hold a logical key more than once). If the width ever has to change, expect one fresh full-depth pass at the new anchors and treat the rows written at the old ones as history that stays.

### Known state of the private store

- **Twelve duplicate rows already exist**, written by the first overlapping run before cross-run dedup existed: manifest `1ec3085f-61b6-444a-a51e-cf7a3f6b90e1` (started 2026-10-23T15:33:45Z, 12 rows across 3 parts) contributed **0 genuinely new rows** on top of `750bce0e-6956-472b-9f0b-172ce4936a86` (39 rows, 9 parts) — 51 stored rows for 39 distinct logical observations. Both runs used the same 86400 s chunk width, and the second manifest's `quality_flags` was empty, which is exactly the silence the disclosure flags now remove.
- **Decision: leave them in place.** They are inert, parts are immutable, and `validate` checks manifest part hashes against the files on disk — so a half-revert breaks validation instead of cleaning the store. Do not rewrite or delete a part to make the count look tidy.
- **Obligation on whoever writes the HA reader:** deduplicate on the documented row identity — `(entity, last_changed, last_updated, state, attributes_json)` — exactly as the Prometheus path collapses `(series_key, ts, value)`. This is mandatory regardless of whether the store is ever cleaned, and it is the reason the 12 duplicates are safe to leave. Note that **`build.py` currently has no HA reader at all**, so the enum data is preserved but consumed by nothing; the obligation lands with the reader, whenever that is written.
- **Residual disclosure in private storage:** the older manifest `750bce0e-…` still carries `params.base_url` with the literal Home Assistant endpoint. It is deliberately left unmodified because manifests are immutable; it lives only in the private data directory, outside git, at mode 0600. Newer manifests record `params.endpoint` as the role label `home-assistant-history-api` plus a one-way `params.endpoint_instance_sha256_16` digest of the URL instead of the URL itself, as `1ec3085f-…` does.

### Why manual is acceptable interim, and not sufficient long term

Manual extraction is acceptable now because the volume is negligible (a few hundred rows a day for the appliance enums), the pull is one CLI command, and row-level dedup makes an overlapping re-pull free — so the risk of running it is essentially nil.

It is **not sufficient** because it depends on someone remembering. While extraction is manual there is no scheduler heartbeat, no `PrometheusRule`, and no job metric to alert on: a forgotten stretch is invisible until it is unrecoverable. A multi-week gap loses data **permanently**, because recorder history — unlike the Prometheus archive, which can be re-queried for any window inside its retention — cannot be re-extracted later. The interim cadence lowers the risk; only the automated CronJob, with the heartbeat and chunk-accounting alerts of section 6, **bounds** it. That is why this document stays a requirement list rather than being retired.

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
- **Overlap dedup**: extraction windows may overlap between consecutive runs, and for the HA source that overlap is now removed **at extraction**, not deferred to build time: rows already present in the store are not written again, matched on `ROW_IDENTITY_FIELDS` = `(entity, last_changed, last_updated, state, attributes_json)` with run provenance (`extraction_id`, `carry_in`, `source_window_start`) excluded. Raw part files stay append-only and immutable — dedup means *not writing*, never rewriting. Because parts written before 2026-10-23 *do* contain duplicated rows, any reader must still collapse on that identity, exactly as the Prometheus path collapses `(series_key, ts, value)`; for Prometheus rows the natural keys are the full original label set plus the true sample timestamp. Re-pulling an overlapping window is cheap and safe **provided the chunk width is unchanged** — see [Chunk width must stay constant between runs](#chunk-width-must-stay-constant-between-runs).
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
because it cannot be recovered later. That recurring **production** schedule is **still not
implemented**. What changed on 2026-10-23 is that the manual path is no longer ad-hoc: it has a
declared cadence, a declared window depth and row-level dedup, so it is now an operating rule
rather than a best-effort habit — see [Interim manual extraction cadence](#interim-manual-extraction-cadence).

### 2. Deterministic Immutable Manifests

- Each extraction records a reproducible manifest: schema version, code version, config version, extraction timestamp, stable input identity and content hash, units, data-quality flags, UTC start/end with actual duration.
- Manifests are immutable once written. Re-extraction of the same window produces a new manifest with a new `extraction_id` and extraction timestamps. Part **content hashes will differ between runs even for identical source data**, because `extraction_id` is stored on every row; reproducibility is therefore asserted at the *value* level (same samples, same true timestamps) and not by byte-identical files.
- The canonical Parquet store is append-only; old partitions are never overwritten in place.

### 3. CronJob Specification

- **`concurrencyPolicy: Forbid`** — no overlapping jobs. If a run is still active when the next schedule fires, the new run is skipped.
- **Retries**: `backoffLimit` configured to retry transient failures (network timeouts, HA API 5xx). Permanent failures (auth, schema mismatch) must not retry indefinitely — set a reasonable limit (e.g. 3–5) and alert on exhaustion.
- **Timeouts**: `activeDeadlineSeconds` set to bound runaway jobs. The timeout must be generous enough for a full backfill but short enough to detect hangs.
- **Schedule**: to be determined when the CronJob is created, and it must be **no looser than the interim manual cadence** — every 3 days with a window 10 days deep, or daily, which ADR-0003 also calls comfortable. The exact cron expression is a deployment-time decision. Keep `chunk_seconds` at the `DEFAULT_CHUNK_SECONDS = 86400` default across the handover from manual to scheduled, and never change it on a live store: carry-in anchors would mint new rows for windows that are already stored.

### 4. Credentials and Secrets

- **HA API token**: retrieved via ExternalSecret from Vault, mounted as environment variables (`envSecret` references in the pod spec). The token is **never copied to files** on the PVC or in the container filesystem.
- **Credentials in memory only**: the process reads secrets from process environment (populated by Kubernetes from the ExternalSecret). No secret material is written to disk, logged, or included in manifests.
- **Vault path**: to be determined (consistent with existing ExternalSecret patterns in the cluster, e.g. `personal/`, `work/`, `web/` prefixes in `pass`).
- **Interim (manual) period**: the token is supplied from the operator's own secret store into the process environment for the duration of a run, never written to a file on the development PC and never recorded in a manifest. The endpoint itself is likewise not stored verbatim any more — see [Known state of the private store](#known-state-of-the-private-store).

### 5. Storage — PVC on prusik

- **Node affinity**: the PVC is backed by `openebs-zfspv` (or `fast-zfspv`), which is node-local. The CronJob pod **must** have a `nodeAffinity` or `nodeSelector` targeting prusik. No other node can mount a prusik-local PV.
- **PVC reclaim policy**: `Retain` (consistent with existing `openebs-zfspv` StorageClass defaults). The PV is not auto-deleted on PVC removal.
- **ArgoCD prune**: `Prune=false` on the PVC resource annotation to prevent ArgoCD from deleting it on sync. Data loss from a prune would be irreversible.
- **Backup metadata**: the PVC must carry backup labels (`backup/retain`, `backup`) according to existing cluster conventions. A PVC created without these labels is silently never backed up.
- **Size**: to be determined based on measured extraction volume. Development in `/tmp` provides no durable archive; production sizing requires measuring actual Parquet output over a representative window.

### 6. Observability and Alerting

- **Scheduler heartbeat alert**: a PrometheusRule that fires when the CronJob has not completed a successful run within a configurable threshold (e.g. `> 2 days` since last successful completion). The threshold must leave a recovery window before the declared retention expires — sized against the 10 days in config, which is the only figure this plan can cite.
- **Silent-gap alerting**: a job that exits 0 having skipped chunks is a data-loss event for this source, so alert on the manifest's chunk accounting (`params.chunk_accounting.mismatch`, `empty_chunk:*` and `chunk_accounting_mismatch` quality flags), not only on job failure.
- **Interim period (manual)**: there is no scheduler heartbeat, no `PrometheusRule` and no job metric to alert on while extraction is manual, so a missed stretch of cadence is **invisible until the data is already purged**. The chunk-accounting and disclosure flags below still fire *inside a run's manifest* — they just require someone to run and read it. This is the gap automation closes, not something the manual cadence can alert its way out of.
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
- **Does not promote the interim manual cadence to a deployment.** [Interim manual extraction cadence](#interim-manual-extraction-cadence) records an operating rule for a human running the CLI on a development PC; it is not a substitute for the deferred CronJob, and it changes nothing in GitOps.

## Interim Operating Checklist (manual, until the CronJob lands)

The manual cadence above is only safe if these hold for every run:

- [ ] Run `extract --source ha-history` on a **3-day cadence**, requesting a window 10 days deep
- [ ] Never let more than the declared 10 days elapse between two successful runs
- [ ] Keep the chunk width at the `DEFAULT_CHUNK_SECONDS = 86400` default (no flag changes, no programmatic overrides)
- [ ] Read the manifest's `quality_flags` after each run: `duplicate_rows_suppressed` and `carry_in_classification_disagreement` are expected/benign or a chunk-width signal; `empty_chunk:*`, `chunk_accounting_mismatch` and `no_rows_for_entity:*` mean coverage must be re-attempted while the window may still exist
- [ ] Confirm `params.prior_coverage` reports what was already stored, i.e. that the run scanned the parts on disk rather than trusting window arithmetic
- [ ] Treat the store as private: it lives outside git at mode 0600, must never be committed, and no cost or consumption figure derived from it goes into this repository

What this cadence does **not** provide is a scheduler, a heartbeat alert, cluster backup
coverage, or a node-local PVC — those stay the requirements in sections 3–7, and they are
the reason the CronJob is deferred rather than cancelled.

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
- [ ] Keep `chunk_seconds` at 86400 on the first scheduled runs — the schedule inherits the manual store, and a width change would mint new carry-in anchors over already-stored windows
- [ ] Alert on `duplicate_rows_already_stored` and `carry_in_classification_disagreement` as chunk-width/store-hygiene signals, not as coverage failures
- [ ] Implement the HA reader with row-identity dedup (`build.py` has none today): collapse `(entity, last_changed, last_updated, state, attributes_json)` at read time instead of rewriting parts to remove the 12 pre-dedup duplicates
- [ ] If a tighter bound is needed, establish the effective retention boundary from a **read-only** recorder database query — do not infer it from the earliest extracted row
- [ ] Document the Vault path and PVC size in this file (update from "to be determined")
- [ ] Record in this file that the interim manual cadence has been superseded by the schedule (update the Status line and the cadence section)

## References

- [ADR-0003 — Phase 1 measurement and baseline](../docs/adr/0003-phase1-measurement-and-baseline.md)
- [AppDaemon baseline context](./energy-baseline-appdaemon-baseline-context.md)
- [Deploying new apps](../docs/conventions/deploying-new-apps.md)
- [Cluster hygiene — backup labels](../docs/troubleshooting/cluster-hygiene.md)
- [OpenEBS ZFS slow startup with fsGroup](../docs/troubleshooting/openebs-zfspv-slow-startup-fsgroup.md)
- [energy-baseline CLI README](../scripts/energy-baseline/README.md)
- [HA history extractor source](../scripts/energy-baseline/src/energy_baseline/extract_ha.py)
- [Home Assistant Recorder integration](https://www.home-assistant.io/integrations/recorder/)
