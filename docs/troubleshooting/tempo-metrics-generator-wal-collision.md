# Tempo: Metrics-Generator WAL Collides With the Trace Backend's Block Listing

## Problem

`TempoBlockListRisingQuickly` (`system/tempo/resources/prometheus-rules.yaml`) fired repeatedly in
October 2026, and `tempo-0` logged the same error on almost every blocklist poll (~5 min cadence):

```
level=error caller=poller.go:215 msg="failed to poll or create index for tenant"
  tenant=single-tenant
  err="failed to poll tenant blocks: failed listing tenant blocks: invalid UUID length: 60"
```

Polls were not fully stalled — roughly 1 in 5 succeeded (e.g. `writing tenant index ... metas=459`
at 07:44:59, `metas=391` at 08:09:59 on 2026-10-17) — so `tempodb_blocklist_length` swung between
stale and fresh values (383 → 459 → 391 within an hour) instead of tracking real block count
smoothly. The compactor itself was healthy (`tempodb_compaction_outstanding_blocks=0`, no
`tempodb_compaction_errors_total`, successful compactions logged), and the PVC was only ~20% full,
so this was not a capacity or CPU problem — the usual "scale the compactors" advice in the alert's
own message does not apply.

Separately, stored trace data genuinely grew from ~0.3 GiB to ~6.1 GiB between Oct 9 and Oct 17
(see "Related, but distinct" below) — that is a real ingestion-side change, not caused by this bug.

## Root Cause

This deployment runs Tempo **2.9.0 in single-binary mode** (`tempo-1.24.4` chart, deprecated,
`-target=all`, one pod) with `metrics_generator` enabled and:

```yaml
storage:
  trace:
    local:
      path: /var/tempo/traces
metrics_generator:
  traces_storage:
    path: /var/tempo/traces   # ← same root as the backend
  processor:
    local_blocks:
      flush_to_storage: true
      max_block_duration: 1m
```

With `multitenancy_enabled: false`, both resolve to the same tenant directory,
`/var/tempo/traces/single-tenant/`. Verified against Tempo v2.9.0 source:

- `modules/generator/generator.go:createInstance()` appends the tenant ID to
  `metrics_generator.traces_storage.path` before constructing the generator's own WAL
  (`tempodb/wal.New()`), so the generator's WAL root is `/var/tempo/traces/single-tenant/` —
  **the same directory** the main backend uses for `<tenant>/<blockID>/meta.json`.
- `tempodb/encoding/vparquet4/wal_block.go:walPath()` names active WAL block directories
  `<BlockID>+<TenantID>+<VersionString>`, e.g. `9915fb18-…+single-tenant+vParquet4` — exactly
  **60 characters** (36-char UUID + `+single-tenant+` + `vParquet4`).
- `tempodb/backend/local/local.go:ListBlocks()` walks the tenant directory and calls
  `uuid.Parse()` on **every** child directory name that has a `meta.json`/`meta.compacted.json`
  two levels down, with no guard for the `+tenant+version` WAL naming scheme. A 60-char
  `<uuid>+single-tenant+vParquet4` directory name is not a valid bare UUID, so `uuid.Parse`
  returns `invalid UUID length: 60`, and the whole tenant poll fails.

One such directory was confirmed present live at investigation time
(`/var/tempo/traces/single-tenant/9915fb18-…+single-tenant+vParquet4`, ~149 KB, transient
builder state) alongside ~551 legitimate bare-UUID backend block directories.

The generator's *completed* local blocks are staged separately under
`/var/tempo/traces/single-tenant/blocks/<uuid>/` (created by `wal.New()`'s own
`local.NewBackend`), which does not collide by name with the flat `<uuid>/` backend layout —
but `flush_to_storage: true` additionally copies those completed blocks into the main backend
via the real store writer, which is expected and unaffected by this bug.

## How to Diagnose

```bash
# Confirm the poll error (safe, read-only)
kubectl --context=grigri logs tempo-0 -n tempo | grep -i "invalid UUID length"

# Confirm intermittent success/failure pattern rather than a hard stall
kubectl --context=grigri logs tempo-0 -n tempo | grep -i "blocklist poll complete\|writing tenant index"

# Current block count vs. what the alert compares against
# (Prometheus: prometheus.internal.grigri.cloud)
#   tempodb_blocklist_length{namespace="tempo"}
#   avg(tempodb_blocklist_length{namespace=~".*"}) / avg(tempodb_blocklist_length{namespace=~".*"} offset 7d)
```

Confirming the actual offending directory requires shell access to the PVC (not permitted for
automated agents under the `debug` skill's read-only `kubectl` policy — do this manually):

```bash
kubectl --context=grigri exec tempo-0 -n tempo -- \
  sh -c 'ls -la /var/tempo/traces/single-tenant | grep "+single-tenant+"'
```

A directory named `<36-char-uuid>+single-tenant+vParquet4` sitting alongside bare-UUID block
directories is the signature of this collision.

## Fix

Two parts, both in `system/tempo/`:

1. **Separate the paths** (`values.yaml`): `tempo.metricsGenerator.traces_storage.path` is now
   `/var/tempo/generator-traces`, a sibling of — never a parent/child of — the backend's
   `storage.trace.local.path` (`/var/tempo/traces`). The generator's WAL and the backend's block
   listing no longer share a tenant directory.

2. **Migrate the already-written WAL** (`statefulset-wal-migration-patch.yaml`, applied via a
   Kustomize `patches:` entry in `kustomization.yaml`, because the deprecated `tempo-1.24.4` chart
   has no `initContainers` values hook). A `busybox` init container runs before `tempo` starts and
   moves any `<uuid>+single-tenant+vParquet*` directories and the generator's `blocks/` staging
   directory out of the old shared root into the new one. It is idempotent (a no-op on a fresh
   install or on any restart after the first migration), refuses to overwrite on a name collision,
   and fails loudly (`exit 1`) if anything generator-shaped remains at the old path afterwards —
   it never deletes or touches the bare-UUID backend block directories.

No root/`runAsUser: 0` override is needed: the pod's existing `fsGroup: 10001` /
`runAsUser: 10001` already owns both the old and new directories, unlike the fresh-ZFS-dataset
case in [`ci-runner-workspace-permission-denied.md`](ci-runner-workspace-permission-denied.md).

## General Rule

In **single-binary/monolithic** Tempo deployments with `metrics_generator.processor.local_blocks`
enabled, `metrics_generator.traces_storage.path` must never equal, contain, or be contained by
`storage.trace.local.path` (or the equivalent object-store tenant prefix). This was verified
against Tempo v2.9.0 source; the `tempo-1.24.4` Helm chart is deprecated upstream, so re-check
this behavior after any Tempo version bump rather than assuming it still holds.

## Related, but distinct

Around the same window, `metrics_ingestion_time_range_slack` was raised from the default 30s to
60s (commits `cb41d99a`/`4584f1dd`, Oct 9–10), reducing the rate at which the metrics generator
discarded late-arriving spans. Because `local_blocks.flush_to_storage: true` persists
generator-processed spans back to the main backend, this plausibly contributed to the real growth
in stored bytes/blocks over the same week (0.3 GiB → 6.1 GiB) that the ratio-based
`TempoBlockListRisingQuickly` alert also picked up on. That is an ingestion-volume question,
separate from the polling bug fixed here — the alert's ratio can be pushed over its threshold by
either mechanism, and only the polling defect produced the `invalid UUID length: 60` errors.

## Follow-ups noted, not fixed here

- `storage-tempo-0` has **no `backup`/`backup/retain` labels**, so per
  [`AGENTS.md`](../../AGENTS.md) it is silently excluded from Velero/ZFS backups. Adding labels via
  `volumeClaimTemplates` is **not safe to bundle with this change**: `volumeClaimTemplates` is
  immutable on a live StatefulSet, and `persistence.enableStatefulSetAutoDeletePVC: true` sets
  `persistentVolumeClaimRetentionPolicy: {whenDeleted: Delete, whenScaled: Delete}` — forcing a
  StatefulSet recreate to change the template could delete the PVC and its ~6 GiB of real trace
  data. This needs its own change with `Prune=false`/auto-delete disabled first.
- `tempodb_compaction_blocks_total` is a **cumulative counter**, not current inventory — do not
  read a rising value as a growing backlog (this was a real misread during investigation).
