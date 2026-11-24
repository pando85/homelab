# DCGM Exporter Metric Renames (gpu-operator bumps)

## Problem

`GpuMetricsMissing` fired for prusik ~15 minutes after Renovate PR #4846 rolled out
gpu-operator v26.7.2 → v26.7.3 (2026-11-23 03:49 UTC), and kept firing even though the
GPU node was healthy: dcgm-exporter pod Running, scrape `up=1`, GTX 1060 at 47 °C,
8/8 `nvidia.com/gpu` slots advertised. The DCGM Grafana dashboard also lost all prusik
panels, and the `instance` variable no longer listed the node.

## Root Cause

The dcgm-exporter bundled with the chart (image `4.7.0-4.9.0-distroless`, exporter
4.9.0) **renamed its metrics with unit suffixes** and changed its default counters file.
The alert rules and dashboard still queried the old names, so the watched series simply
ceased to exist:

| Old name (≤ v26.7.2) | New name (4.9.0) | Unit change |
|---|---|---|
| `DCGM_FI_DEV_GPU_TEMP` | `DCGM_FI_DEV_GPU_TEMP_CELSIUS` | — (°C) |
| `DCGM_FI_DEV_POWER_USAGE` | `DCGM_FI_DEV_BOARD_POWER_WATTS` | — (W) |
| `DCGM_FI_DEV_SM_CLOCK` | `DCGM_FI_DEV_SM_CLOCK_HERTZ` | MHz → Hz |
| `DCGM_FI_DEV_MEM_CLOCK` | `DCGM_FI_DEV_MEMORY_CLOCK_HERTZ` | MHz → Hz |
| `DCGM_FI_DEV_GPU_UTIL` | `DCGM_FI_DEV_GPU_UTIL_RATIO` | % (0–100) → ratio (0–1) |
| `DCGM_FI_DEV_MEM_COPY_UTIL` | `DCGM_FI_DEV_MEMORY_UTIL_RATIO` | % → ratio |
| `DCGM_FI_DEV_ENC_UTIL` / `DEC_UTIL` | `DCGM_FI_DEV_ENC_UTIL_RATIO` / `DEC_UTIL_RATIO` | % → ratio |
| `DCGM_FI_DEV_FB_USED` / `FB_FREE` | `DCGM_FI_DEV_FB_USED_BYTES` / `FB_FREE_BYTES` | MiB → bytes |
| `DCGM_FI_DEV_TOTAL_ENERGY_CONSUMPTION` | `DCGM_FI_DEV_GPU_ENERGY_JOULES_TOTAL` | mJ → J |
| `DCGM_FI_DEV_PCIE_REPLAY_COUNTER` | `DCGM_FI_DEV_PCIE_REPLAY_TOTAL` | — |
| `DCGM_FI_DEV_XID_ERRORS` | `DCGM_FI_DEV_XID_ERROR` (singular) | — |
| `DCGM_FI_DEV_CLOCK_THROTTLE_REASONS` | **dropped from default counters** | see below |

Additional traps found during the investigation:

- **Two scrape paths, two label styles.** The in-cluster ServiceMonitor scrape emits
  lowercase `hostname="prusik"` (the k8s node name). The `z800-nvidia-dcgm-exporter`
  ScrapeConfig (`system/monitoring/resources/z800-gpu-scrape-config.yaml`) scrapes the
  owner's desktop, a *different machine* with an older exporter emitting capitalized
  `Hostname="z800"` and old metric names. After the rename, the only remaining
  `DCGM_FI_DEV_GPU_TEMP` series came from z800 — which can never match `node="prusik"`,
  so `GpuMetricsMissing` fired permanently.
- **Throttle bitmask dropped, field still alive.** `DCGM_FI_DEV_CLOCK_THROTTLE_REASONS`
  is a deprecated alias of DCGM field 112 (`DCGM_FI_DEV_CLOCKS_EVENT_REASONS`, same
  `DCGM_CLOCKS_EVENT_REASON_*` bitmask). The exporter's default counters file no longer
  enables it; upstream prefers new per-reason `*_THROTTLE_SECONDS_TOTAL` counters
  (disabled by default). `GpuThrottling` was silently dead — it had never fired because
  the field was not in the old chart's counters file either.
- **XID metric is sticky and lazy.** `DCGM_FI_DEV_XID_ERROR` is only exported once an
  XID has occurred, and then keeps the last XID code as its value. `> 0` alerts fire
  permanently after a single event until the driver is reloaded; silence manually after
  investigating.

## How to Diagnose

```bash
# When did the alert go pending? (epoch value of ALERTS_FOR_STATE)
# Prometheus: ALERTS_FOR_STATE{alertname="GpuMetricsMissing"}

# Which DCGM metric names does the node actually export right now?
# Prometheus: count by (__name__) ({instance="<dcgm-pod-ip>:9400"})

# Exporter startup log shows the counters file and skipped fields
kubectl --context=grigri logs -n gpu-operator -l app=nvidia-dcgm-exporter --tail=50

# Correlate with the chart bump
git log --oneline -5 -- system/gpu-operator/kustomization.yaml
```

`up=1` with missing metric names means "scrape works, exporter renamed/dropped fields",
not "exporter down". Empty dashboard panels look identical to nonexistent metrics.

## Fix / Workaround

Applied in `system/gpu-operator/`:

- `resources/prometheus-gpu-rules.yaml`: temperature alerts and `GpuMetricsMissing` use
  `DCGM_FI_DEV_GPU_TEMP_CELSIUS`; `GpuXidError` uses `DCGM_FI_DEV_XID_ERROR`.
- `values.yaml`: `dcgmExporter.config` pins a custom counters file (ConfigMap
  `dcgm-exporter-metrics`, key `dcgm-metrics.csv`) with the unit-suffixed names **plus
  re-enabled `DCGM_FI_DEV_CLOCK_THROTTLE_REASONS`**, keeping `GpuThrottling`'s bitmask
  expression valid. If a future DCGM removes the deprecated name, switch the csv line
  and the alert to `DCGM_FI_DEV_CLOCKS_EVENT_REASONS`.
- `dashboards/dcgm-exporter.json`: queries accept old **or** new names (`or` /
  `__name__=~` regex) so both prusik and the z800 desktop render; `label_replace(...,
  "Hostname", "$1", "hostname", "(.+)")` normalizes the node label; unit conversions
  (MHz→Hz ×1e6, ratio→% ×100, MiB→bytes ×1048576) keep panels consistent.

### After every gpu-operator bump

1. Check exporter logs for `Skipping line … metric not enabled` warnings.
2. Verify the alert metric names exist: `count by (__name__) ({job="nvidia-dcgm-exporter"})`
   and compare against `prometheus-gpu-rules.yaml`.
3. Confirm no `Gpu*` alert is firing and the dashboard's `instance` variable lists prusik.

The custom counters file means upstream default-file changes no longer propagate
automatically — a deliberate trade of freshness for alert stability. Renovate chart
bumps are one-line PRs that hide the exporter version change; `GpuMetricsMissing` is the
watchdog for the temperature metrics, but alerts whose metric silently vanishes (XID,
throttling) give no signal. If this recurs, consider requiring manual approval for
gpu-operator bumps (Renovate `dependencyDashboardApproval`) so release notes get read.
