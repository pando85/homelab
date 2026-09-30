# Grafana dashboards draw duplicate lines after node-exporter pod restarts

Found 2026-09-30 while fixing the ZFS dashboard (`system/zfs-exporter/dashboards/zfs.json`).
Applies to **any** dashboard querying node-exporter metrics, not just ZFS.

## Problem

Widen the time range on a panel and the same measurement appears as **several overlapping lines in
different colours**, with labels that look identical in the legend. The lines have gaps between them
rather than forming one continuous series.

Worse, panels using `sum(rate(...))` silently **inflate** their values, because the duplicated series
are added together during the windows where they overlap.

## Root Cause

node-exporter runs as a DaemonSet, so Kubernetes service discovery attaches `pod`, `container`,
`namespace`, `endpoint`, `service`, and `job` labels to every series it emits. Each pod restart mints
a **new `pod` label value**, which makes it a brand-new time series as far as Prometheus is concerned:

```
node_zfs_arc_l2_size{instance="prusik",pod="monitoring-prometheus-node-exporter-m7fch",...}
node_zfs_arc_l2_size{instance="prusik",pod="monitoring-prometheus-node-exporter-gxxzw",...}
node_zfs_arc_l2_size{instance="prusik",pod="monitoring-prometheus-node-exporter-t48vr",...}
```

Old series are retained for the full Prometheus retention window. A query with no aggregation returns
one series per pod generation, and Grafana plots each separately. The legend usually shows only the
semantic labels, so they appear identical.

Measured on this cluster: **3 distinct `pod` values over 30 days** for both `prusik` and `grigri`, so
up to 3 phantom lines per panel.

## How to Diagnose

```promql
# How many pod generations exist in the window you are viewing?
count(group by (pod) (last_over_time(node_zfs_arc_l2_size{instance="prusik"}[30d])))
```

Anything above 1 means unaggregated panels on that metric are drawing duplicates.

Compare the raw and collapsed forms over a long range — the raw query returns N partial series, the
collapsed one returns a single continuous series:

```promql
node_zfs_arc_l2_size{instance="prusik"}                  # 3 series, gaps
max by (instance) (node_zfs_arc_l2_size{instance="prusik"})   # 1 series, continuous
```

## Fix

Aggregate away the infrastructure labels, keeping only the semantic ones:

```promql
max by (instance) (node_zfs_arc_l2_size{instance="$instance"})
max by (instance) (rate(node_zfs_arc_mru_hits{instance="$instance"}[$interval]))
max by (instance, zpool, dataset) (rate(node_zfs_zpool_dataset_nread{...}[$interval]))
```

**Use `max`, not `sum` or `avg`.** At any instant only one pod is actually reporting; the others are
stale. `max` selects the live value without double-counting. `sum` inflates during overlap windows
and `avg` dilutes the real value by dividing across dead series.

Apply the aggregation **outside** `rate()`, not inside — `rate()` must see each raw series so it can
handle counter resets correctly, then `max` collapses the result.

Where a panel needs a total across an inner label, nest the two aggregations:

```promql
sum by (zpool) (
  max by (instance, zpool, dataset) (rate(node_zfs_zpool_dataset_nread{...}[$interval]))
)
```

### What does NOT need this

- **Template variables.** `label_values(node_zfs_arc_size, instance)` dedupes on the requested label,
  so `pod` churn does not add entries to the dropdown.
- **Static-target exporters.** The standalone `zfs_exporter` on port 9134 is scraped via a
  `ScrapeConfig` with a fixed `instance` (`prusik.grigri:9134`), so it never churns. Its `zfs_dataset_*`
  and `zfs_pool_*` metrics need no collapsing.

## Related bug found: pool-level ZFS I/O metrics do not exist

The four "Zpool stats" I/O panels queried `node_zfs_zpool_nread`, `node_zfs_zpool_nwritten`,
`node_zfs_zpool_reads`, and `node_zfs_zpool_writes`. **None of these metrics are exported.**
node_exporter's zfs collector emits only `node_zfs_zpool_dataset_*` and `node_zfs_zpool_state`, so
those panels rendered permanently empty — silently, with no error.

Rebuilt as a sum over the per-dataset counters. This is sound because ZFS dataset I/O counters are
non-recursive (a parent's `nread` excludes its children) and the `dataset` label contains no
snapshots — verified 75 datasets on prusik, zero matching `.*@.*`.

Sanity check the result is plausible: prusik showed ~6,131 dataset reads/s against only ~500 disk
IOPS. That gap is ARC absorption, not an error.

**Lesson:** a Grafana panel that renders empty is indistinguishable from one whose metric does not
exist. When touching a dashboard, verify each metric name actually returns data:

```promql
count by (__name__) ({__name__=~"node_zfs_zpool_.*", instance="prusik"})
```
