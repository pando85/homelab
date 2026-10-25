# battery-optimizer

Two workloads of [pando85/battery-optimizer](https://github.com/pando85/battery-optimizer), deployed
from `apps/battery-optimizer/` with one image, one PVC and one ConfigMap:

- **`pipeline`**: a CronJob that extracts Home Assistant recorder history and Prometheus samples
  into an append-only Parquet store, validates it, and builds the 15-minute analysis dataset
  (Phase B).
- **`service`**: the resident Phase C **shadow service** (`battery-optimizer serve`). It reads Home
  Assistant and Prometheus, solves the MPC plan every 15 minutes and records shadow intents and a
  shadow-vs-native comparison. It has no actuation path and no write credentials. The production plan and its history live in
[`planning/energy-baseline-production-execution.md`](https://github.com/pando85/homelab/blob/master/planning/energy-baseline-production-execution.md).

The application code, ADRs and domain docs live in the app repository; this page covers only the
deployment.

## What runs

| Item | Value |
|---|---|
| Namespace / ArgoCD app | `battery-optimizer` (ApplicationSet auto-discovery) |
| CronJob | `battery-optimizer-pipeline`, `30 3 * * *` UTC, `concurrencyPolicy: Forbid` |
| Image | `ghcr.io/pando85/battery-optimizer`, pinned `tag@sha256`, Renovate-managed |
| Steps (one container, `sh -ec`, fails fast) | extract `ha-history` (8 d lookback) -> extract `prometheus` (3 d lookback) -> `validate` -> `build` |
| Failure handling | `backoffLimit: 2`, `activeDeadlineSeconds: 3600`, `startingDeadlineSeconds: 21600`, history 3/3 |
| Node | pinned to `prusik` (node-local `openebs-zfspv`) |
| Secret | ExternalSecret `battery-optimizer-home-assistant` <- Vault KV `secret/battery-optimizer/home-assistant`, property `token` (read-only HA token, env only, never on disk) |
| Config | ConfigMap `battery-optimizer-config` rendered from `apps/battery-optimizer/files/` (`tariff.yaml`, `service.yaml`) |
| Storage | PVC `battery-optimizer-data`, 5 Gi, `openebs-zfspv`, mounted at `/data` (`BATTERY_OPTIMIZER_DATA_DIR`) |
| Backup | labels `backup: battery-optimizer-zfs`, `backup/retain: quaterly`; SnapshotSchedule `battery-optimizer-zfs-backups` (daily 06:00 UTC, keep 20) |
| Alerts | `PrometheusRule/battery-optimizer` (label `release: monitoring`) for the CronJob, `PrometheusRule/battery-optimizer-service` (long-term) for the service |

## Design notes and gotchas

- **Why daily with an 8 day HA window.** HA's recorder keeps 10 days (`purge_keep_days` default,
  nothing overrides it). Rows older than that are gone for good, so the binding rule is "never more
  than 10 days between two successful runs". Daily with an 8 day lookback tolerates 7 missed runs. The window is deliberately shorter than the
  retention: the recorder purges nightly, so the oldest chunk of a 10 day window is partly or wholly
  gone and would raise a coverage gap every night (see the next note). Re-pulling is
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
- **NetworkPolicy is egress-only.** The repo keeps ingress isolation off because of the Cilium
  stale-datapath bug (see
  [`cilium-stale-bpf-egress.md`](../troubleshooting/cilium-stale-bpf-egress.md)); an egress-only
  policy leaves kubelet probes and scrapes alone. The first run under the policy (manual Job,
  73 s) reached DNS, Home Assistant and Prometheus.
- **`tariff.yaml` and `service.yaml` are vendored copies** of `config/` in the app repository (the
  image carries no config). Update both together; the tariff config hash is part of every dataset id.
- **Coverage gaps fail the Job.** From 0.2.1 `extract` exits 3 when the run's manifest records a
  coverage gap (HA: `empty_chunk`, `empty_chunk_after_window_filter`, `chunk_accounting_mismatch`,
  `no_rows_for_entity`, `no_rows_extracted:all`; Prometheus: `no_samples_extracted`,
  `no_series_chunks`, `series_without_samples_chunks`). The data and manifest are kept, but the Job
  fails and `BatteryOptimizerJobFailed` fires, so a silent gap is no longer silent. The pipeline does
  **not** pass `--allow-coverage-gaps` (except for the one-off Prometheus bootstrap, whose 2024
  history has known holes). If a window legitimately has no data, fix the window rather than opting out.

## Shadow service (Phase C)

| Item | Value |
|---|---|
| Workload | Deployment `battery-optimizer-service`, 1 replica, `strategy: Recreate`, same image anchor as the CronJob |
| Command | `battery-optimizer serve`, port 8080: `/health/live`, `/health/ready`, `/status`, `/plan`, `/metrics` |
| Config | `files/tariff.yaml` + `files/service.yaml` (vendored from the app repo `config/`) in ConfigMap `battery-optimizer-config`, mounted at `/config`. `service.yaml` uses `soc_source: simulated` because Home Assistant has no battery entities yet |
| HA token | the same ExternalSecret as the pipeline, **mounted as a file** (`BATTERY_OPTIMIZER_HA_TOKEN_FILE`). Read-only, non-admin; no other credential exists |
| Storage | the same PVC `battery-optimizer-data` at `/data` (RWO on prusik, `Recreate` avoids two pods). The service writes only `/data/service/v1/`; the pipeline uses `raw/`, `manifests/` and `analysis/`, so the paths never collide |
| Resources | requests 100m / 256Mi, memory limit 512Mi, no CPU limit (observed ~125-150 MiB RSS) |
| Probes | liveness **and** readiness on `/health/live`; generous startup probe. `/health/ready` returns 503 whenever an input is stale, which would remove the endpoint and stop the scrape exactly when the metrics matter, so alert on `battery_optimizer_ready` instead |
| Service / scrape | ClusterIP `battery-optimizer:8080` (`http`); ServiceMonitor labelled `operator.prometheus.io/instance: prometheus-long-term` (job `battery-optimizer`, 30 s), so `battery_optimizer_*` lives in the long-term Prometheus for the multi-week comparison |
| Alerts | `PrometheusRule/battery-optimizer-service` (long-term Prometheus): `BatteryOptimizerServiceDown`, `ServiceAbsent`, `NotReady` (30 m), `HomeAssistantDisconnected` (10 m), `GridPowerStale` (15 m), `PlanStale` (1 h), `SolveFailures`, `SinkWriteErrors`, `SolvesSkipped`, `HistoryStale` |
| Dashboard | ConfigMap `dashboards-battery-optimizer`, Grafana folder `battery-optimizer`, datasource `prometheus-long-term` |

**NetworkPolicy** `battery-optimizer-egress` selects every pod in the namespace (service and CronJob)
and is **egress-only**: DNS (kube-dns and nodelocaldns pods plus `169.254.25.10/32`), Home Assistant
pods on 8123 and the long-term Prometheus pods on 9090. Nothing else, in particular no Modbus. There
is intentionally no Ingress section, so kubelet probes and Prometheus scrapes are not under policy
(the documented Cilium stale-datapath breakage is dropped ingress probes). Peers are matched with
pod/namespace selectors because Cilium identifies in-cluster endpoints by identity.

The shadow store (`/data/service/v1/`) is private household data and is covered by the same PVC
snapshots. Read the comparison with `battery-optimizer shadow-report` from a pod or a copy of the
store; `comparison_basis` stays `no_battery_pre_commissioning` until battery telemetry is mapped, and
that basis does not satisfy exit gate C.

Checks without a port-forward:

```bash
kubectl --context=grigri -n battery-optimizer exec deploy/battery-optimizer-service -- \
  python -c "import urllib.request as u; print(u.urlopen('http://127.0.0.1:8080/health/ready').read().decode())"
```

## First run (2026-10-25)

Manual Job from the CronJob on an empty PVC: success in 5m46s including the full Prometheus
bootstrap (1028 parts, 5.84 M rows), peak working set about 1.15 GiB (requests 768 Mi, limit 2 Gi),
about 0.54 core peak, 41.5 MiB on the volume afterwards. Steady-state runs only re-pull 8 days of
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
