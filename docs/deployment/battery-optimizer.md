# battery-optimizer

Batch data pipeline for [pando85/battery-optimizer](https://github.com/pando85/battery-optimizer):
extract Home Assistant recorder history and Prometheus samples into an append-only Parquet store,
validate it, and build the 15-minute analysis dataset. Deployed from `apps/battery-optimizer/` as a
single CronJob. The production plan and its history live in
[`planning/energy-baseline-production-execution.md`](https://github.com/pando85/homelab/blob/master/planning/energy-baseline-production-execution.md).

The application code, ADRs and domain docs live in the app repository; this page covers only the
deployment.

## What runs

| Item | Value |
|---|---|
| Namespace / ArgoCD app | `battery-optimizer` (ApplicationSet auto-discovery) |
| CronJob | `battery-optimizer-pipeline`, `30 3 * * *` UTC, `concurrencyPolicy: Forbid` |
| Image | `ghcr.io/pando85/battery-optimizer`, pinned `tag@sha256`, Renovate-managed |
| Steps (one container, `sh -ec`, fails fast) | extract `ha-history` (10 d lookback) -> extract `prometheus` (3 d lookback) -> `validate` -> `build` |
| Failure handling | `backoffLimit: 2`, `activeDeadlineSeconds: 3600`, `startingDeadlineSeconds: 21600`, history 3/3 |
| Node | pinned to `prusik` (node-local `openebs-zfspv`) |
| Secret | ExternalSecret `battery-optimizer-home-assistant` <- Vault KV `secret/battery-optimizer/home-assistant`, property `token` (read-only HA token, env only, never on disk) |
| Config | ConfigMap `battery-optimizer-config` rendered from `apps/battery-optimizer/files/tariff.yaml` |
| Storage | PVC `battery-optimizer-data`, 5 Gi, `openebs-zfspv`, mounted at `/data` (`BATTERY_OPTIMIZER_DATA_DIR`) |
| Backup | labels `backup: battery-optimizer-zfs`, `backup/retain: quaterly`; SnapshotSchedule `battery-optimizer-zfs-backups` (daily 06:00 UTC, keep 20) |
| Alerts | `PrometheusRule/battery-optimizer` (label `release: monitoring`) |

## Design notes and gotchas

- **Why daily with a 10 day HA window.** HA's recorder keeps 10 days (`purge_keep_days` default,
  nothing overrides it). Rows older than that are gone for good, so the binding rule is "never more
  than 10 days between two successful runs". Daily tolerates a week of missed runs. Re-pulling is
  free because the HA extractor dedups against rows already in the store. Chunk width stays at the
  CLI default (86400 s); changing it on a live store mints new carry-in anchors.
- **Prometheus extraction is not deduplicated across runs**, so the lookback is only 3 days
  (about 40 KB per day-part). The Prometheus archive has no retention limit, so there is no urgency.
- **Bootstrap.** If `/data/manifests` holds no finished Prometheus extraction (empty PVC), the run
  extracts Prometheus from `PROM_BOOTSTRAP_START` (2024-01-01) instead of the 3 day lookback. A
  manifest is written only by a finished run, so an interrupted bootstrap retries from scratch.
- **Build window.** `build` is content-addressed and write-once, and its dataset id covers every
  source part, so a rolling window would add one full dataset (~20 MB) per run. The job builds the
  complete calendar months `[BUILD_START, first of this month)` so the id only changes when parts
  inside that window change. The newest partial month is therefore not in the dataset until the
  month closes. There is no CLI-side garbage collection of superseded datasets.
- **Permissions.** No `fsGroup` (slow startup on `openebs-zfspv`). A fresh ZFS dataset is
  `root:root 0755`, so the `fix-permissions` init container chowns the volume **root** to 65532
  (recursively only if the root is not already owned by 65532). See
  [`ci-runner-workspace-permission-denied.md`](../troubleshooting/ci-runner-workspace-permission-denied.md).
- **Alerts use `release: monitoring`, not `operator.prometheus.io/instance: prometheus-long-term`.**
  kube-state-metrics and kubelet series exist only in the main Prometheus; a rule labelled for
  `prometheus-long-term` would load but never fire.
- **No NetworkPolicy.** The repo keeps NetworkPolicy isolation off until the Cilium stale-datapath
  bugs are fixed (see
  [`cilium-stale-bpf-egress.md`](../troubleshooting/cilium-stale-bpf-egress.md)). Cilium also
  matches in-cluster destinations by identity, so the `ipBlock` egress pattern used for coturn does
  not work for HA/Prometheus Service IPs.
- **`tariff.yaml` is a vendored copy** of `config/tariff.yaml` in the app repository (the image
  carries no config). Update both together; the config hash is part of every dataset id.
- **Exit codes do not reflect coverage.** `extract` exits 0 even when the manifest carries
  `empty_chunk:*` or `chunk_accounting_mismatch` quality flags, so the job (and its alerts) only
  detect hard failures and missed runs, not silent gaps. Read the manifest `quality_flags` in the
  job logs/store after a bootstrap or an HA outage.

## First run (2026-10-25)

Manual Job from the CronJob on an empty PVC: success in 5m46s including the full Prometheus
bootstrap (1028 parts, 5.84 M rows), peak working set about 1.15 GiB (requests 768 Mi, limit 2 Gi),
about 0.54 core peak, 41.5 MiB on the volume afterwards. Steady-state runs only re-pull 10 days of
HA history and 3 days of Prometheus. The cluster store is separate from the manual
`~/data/energy-baseline` store, which has not been migrated into the PVC.

Container logs carry one `httpx` INFO line per request (roughly 1000 for the bootstrap), so
kubelet log rotation can drop the early `step N/4` lines of a long run; Loki keeps the full stream.

## Operate

```bash
# State
kubectl --context=grigri -n battery-optimizer get cronjob,job,pvc,externalsecret
kubectl --context=grigri -n battery-optimizer logs job/<job-name> -c pipeline

# Run one pipeline outside the schedule (e.g. after fixing a failure)
kubectl --context=grigri -n battery-optimizer create job --from=cronjob/battery-optimizer-pipeline battery-optimizer-pipeline-manual-<n>
```

Bumping the image is a Renovate PR changing `tag` in `apps/battery-optimizer/values.yaml`. A new
code version changes every dataset id, so expect one extra dataset on the next build.

Growing the volume: raise `spec.resources.requests.storage` in `templates/pvc.yaml`
(`allowVolumeExpansion` is on). Never delete the PVC; `Prune=false` and `Retain` protect it.
