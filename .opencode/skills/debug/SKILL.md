---
name: debug
description: Debug live issues in the homelab Kubernetes cluster using kubectl and Grafana observability tools.
---

## Purpose

Diagnose and troubleshoot issues in the homelab K3s cluster by combining Kubernetes
direct inspection with Grafana observability (metrics, logs, dashboards, alerts).

## When to use

Use this skill when asked to debug, troubleshoot, investigate, or diagnose issues
with applications or infrastructure running in the homelab cluster.

## Cluster Access

All `kubectl` commands **must** use the grigri context:

```bash
kubectl --context=grigri <command>
```

### Common kubectl Debugging Commands

```bash
# Overview of cluster health
kubectl --context=grigri get nodes
kubectl --context=grigri get pods -A | grep -v Running

# Inspect a specific pod
kubectl --context=grigri describe pod <pod-name> -n <namespace>
kubectl --context=grigri logs <pod-name> -n <namespace> --tail=100
kubectl --context=grigri logs <pod-name> -n <namespace> --previous

# Resource usage
kubectl --context=grigri top pods -n <namespace>
kubectl --context=grigri top nodes

# Events
kubectl --context=grigri get events -n <namespace> --sort-by='.lastTimestamp'

# ArgoCD application status
kubectl --context=grigri get applications -n argocd
kubectl --context=grigri describe application <app-name> -n argocd
```

## Grafana Observability (MCP Tools)

Use the `grafana-grigri` MCP tools for metrics, logs, and alerting. The Grafana
instance is at https://prometheus.internal.grigri.cloud.

### Workflow: Metrics (Prometheus)

1. **Discover metrics** — `list_prometheus_metric_names` with a regex filter
2. **Find label values** — `list_prometheus_label_values` for filtering
3. **Query** — `query_prometheus` with PromQL for instant or range queries
4. **Histograms** — `query_prometheus_histogram` for latency percentiles

Example flow:
```
list_prometheus_metric_names(datasourceUid=..., regex="http_requests")
  → list_prometheus_label_values(datasourceUid=..., labelName="job")
    → query_prometheus(datasourceUid=..., expr='rate(http_requests_total[5m])', ...)
```

### Workflow: Logs (Loki)

1. **Check stream size** — `query_loki_stats` with a label selector
2. **Discover labels** — `list_loki_label_names` / `list_loki_label_values`
3. **Search logs** — `query_loki_logs` with full LogQL
4. **Detect patterns** — `query_loki_patterns` for anomaly detection

Example flow:
```
query_loki_stats(datasourceUid=..., logql='{app="nginx"}')
  → list_loki_label_values(datasourceUid=..., labelName="app")
    → query_loki_logs(datasourceUid=..., logql='{app="nginx"} |= "error"', limit=50)
```

### Workflow: Alerts & Dashboards

- **Alerts** — `list_alert_rules` to find firing/pending alerts; `get_alert_rule_by_uid` for detail
- **Dashboards** — `get_dashboard_summary` for overview; `get_dashboard_panel_queries` to see queries
- **Datasources** — `list_datasources` to find Prometheus/Loki UIDs

### Finding Datasource UIDs

If the datasource UID is unknown:
```
list_datasources() → filter by type (prometheus, loki)
```

## Debugging Strategy

### 1. Identify the Scope

- What app/namespace is affected?
- When did the issue start?
- Is it a deployment, networking, resource, or application issue?

### 2. Check Kubernetes State

Use `kubectl --context=grigri` to check:
- Pod status (CrashLoopBackOff, ImagePullBackOff, OOMKilled)
- Events (scheduling failures, config errors)
- Resource pressure (CPU/memory limits)

### 3. Check Observability

Use `grafana-grigri` MCP tools to:
- Check for firing alerts related to the app
- Query metrics for anomalies (error rate spike, latency increase)
- Search logs for error messages or stack traces
- Review relevant dashboard panels

### 4. Cross-Reference

- Correlate kubectl events with metric spikes or log errors
- Check if a recent ArgoCD sync or Helm upgrade preceded the issue
- Verify ExternalSecrets are syncing from Vault

## Common Issue Patterns

| Symptom | Check |
|---------|-------|
| Pod not starting | `kubectl describe pod`, check events |
| OOMKilled | `kubectl top pods`, query `container_memory_*` metrics |
| High latency | `query_prometheus_histogram`, check `http_request_duration_*` |
| Error spike | `query_loki_logs` with `\|=` "error", query error rate metrics |
| Certificate issues | `kubectl get certificates`, cert-manager logs |
| DNS failures | Check CoreDNS logs, `kubectl get endpoints` |
| PVC issues | `kubectl get pvc`, check storage class and capacity |
| ArgoCD sync failure | `kubectl describe application`, check sync status and errors |
| Vault/ESO issues | Check `externalsecrets` and `secretstore` status |

## Important Notes

- This is a **production** cluster. Only run read-only kubectl commands.
- **NEVER** run `kubectl apply`, `kubectl delete`, `kubectl edit`, or any mutating command.
- The `grafana-grigri` tools are read-only by design.
- If a fix is identified, suggest the command for the user to run manually.
- Time ranges for Grafana queries default to last hour. Adjust when investigating older issues.
