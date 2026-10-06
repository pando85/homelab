# Vector Log Loss and New-Pod Starvation

## Problem

Two distinct but related symptoms in the Vector → Loki pipeline:

1. **Short-lived pod log loss**: Logs from pods that run briefly (Argo workflows, migrations,
   CI jobs, CronJobs) silently never reach Loki. The pod runs, produces output, and completes —
   but no logs appear.

2. **New-pod starvation on busy nodes**: After a Vector restart or a period of high log volume,
   freshly deployed pods stop appearing in Loki. Existing pods continue to log normally. The new
   pods are healthy and producing output, but Vector never reads their files.

## Root Cause

Vector's `kubernetes_logs` source uses a file-discovery and file-reading loop with several
tuning parameters that default to values optimized for low-overhead, not for completeness or
fairness.

### Short-lived pod log loss (glob_minimum_cooldown_ms)

Vector scans `/var/log/pods/` for new log files on a timer controlled by
`glob_minimum_cooldown_ms`. The default is **60000ms (60 seconds)**. When a pod is created and
deleted within that window — common for Argo workflow steps, database migrations, or batch jobs —
the log file appears and disappears between scans. Vector never discovers it. The logs are gone.

### New-pod starvation (oldest_first + rotate_wait_secs + max_read_bytes)

Three defaults combine to starve new pods under backlog:

- **`oldest_first: true`** (default): Vector reads the oldest file in the queue first. After a
  restart, all existing log files queue up. New files from freshly deployed pods are appended to
  the tail and wait behind the entire backlog. On busy nodes with lots of historical data, new
  pods can wait minutes before their first log line is read.

- **`rotate_wait_secs: i64::MAX`** (default): When a log file is rotated or its owning pod is
  deleted, Vector keeps the file handle open **indefinitely**. Over time this accumulates thousands
  of stale file descriptors. Each one consumes a slot in the read loop's internal budget, leaving
  fewer slots available for new files. The read loop becomes saturated with dead files.

- **`max_read_bytes: 2048`** (default): Each read cycle drains only 2KB from each file. When
  there's a backlog (e.g., after a Vector restart or Loki outage), draining 2KB/cycle is far too
  slow to catch up. The backlog grows, and new files get pushed further down the priority queue.

Together, these defaults mean: after any disruption, Vector spends all its time reading old data
from deleted pods and never gets to the new ones.

## How to Diagnose

**Short-lived pod log loss:**

```logql
# Check if a known short-lived pod's logs exist
{pod=~"some-argo-workflow-step.*"}
```

If the pod ran and completed but returns nothing, check Vector's file discovery cooldown:

```bash
kubectl --context=grigri -n loki get configmap vector-agent -o jsonpath='{.data.vector\.yaml}' \
  | grep glob_minimum_cooldown_ms
```

If absent or set to 60000, short-lived pods are being missed.

**New-pod starvation:**

```bash
# Check if Vector has the starvation-prone defaults
kubectl --context=grigri -n loki get configmap vector-agent -o jsonpath='{.data.vector\.yaml}' \
  | grep -E 'oldest_first|rotate_wait_secs|max_read_bytes'
```

If `oldest_first` is missing (defaults to true), `rotate_wait_secs` is missing (defaults to
i64::MAX), or `max_read_bytes` is missing (defaults to 2048), the pipeline is vulnerable.

```promql
# Check for accumulated open files (high = stale handles building up)
vector_component_errors_total{component_id="kubernetes_logs"}

# Check buffer fill — starvation shows high buffer on loki_pods with low send rate
rate(vector_buffer_received_events_total{component_id="loki_pods"}[5m])
rate(vector_buffer_sent_events_total{component_id="loki_pods"}[5m])
```

## Fix

Apply these parameters to the `kubernetes_logs` source:

```yaml
sources:
  kubernetes_logs:
    type: kubernetes_logs
    read_from: "beginning"          # don't skip logs from pod startup on first run
    glob_minimum_cooldown_ms: 5000  # scan every 5s — catches pods that live <60s
    oldest_first: false             # fair scheduling — new files aren't queued behind backlog
    rotate_wait_secs: 60            # release stale file handles after 60s, not forever
    max_read_bytes: 16384           # drain 16KB/cycle to clear backlog faster
```

Additionally, configure explicit memory buffers on Loki sinks to prevent silent log drops under
backpressure:

```yaml
sinks:
  loki_pods:
    buffer:
      type: memory
      max_events: 1000
      when_full: block   # apply backpressure, never drop events
  loki_journal:
    buffer:
      type: memory
      max_events: 200
      when_full: block   # apply backpressure, never drop events
```

Without `when_full: block`, Vector's default behavior when the sink can't keep up (Loki slow or
down) is to drop new events. `block` causes Vector to stop reading from sources instead, which
preserves all collected logs and surfaces the problem via buffer-fill alerts.

## Parameter Reference

| Parameter | Default | Tuned | Rationale |
|-----------|---------|-------|-----------|
| `read_from` | `"end"` | `"beginning"` | Read entire log file on first encounter, not just tail. Prevents missing startup logs. |
| `glob_minimum_cooldown_ms` | 60000 | 5000 | File discovery interval. 5s catches pods that live and die within a minute. |
| `oldest_first` | `true` | `false` | Fair file scheduling. New files get read immediately instead of waiting behind backlog. |
| `rotate_wait_secs` | `i64::MAX` | 60 | Release rotated/deleted file handles after 60s. Prevents FD exhaustion from stale pods. |
| `max_read_bytes` | 2048 | 16384 | Bytes drained per cycle per file. 16KB clears backlog ~8× faster. |
| `buffer.when_full` | `drop_newest` | `block` | Never drop events under backpressure. Block sources and surface the problem via alerts. |

See the [Vector kubernetes_logs source
documentation](https://vector.dev/docs/reference/configuration/sources/kubernetes_logs/) and
[Vector buffer documentation](https://vector.dev/docs/guides/configuring/buffers/) for upstream
details on each parameter.
