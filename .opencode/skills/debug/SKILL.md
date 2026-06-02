---
name: debug
description: Use ONLY when debugging live homelab Kubernetes/K3s cluster issues with read-only kubectl or Grafana observability.
---

## Scope

Diagnose live issues in the `grigri` K3s cluster by combining read-only Kubernetes inspection with
Grafana observability. Do not use this skill for static chart edits, docs-only work, or general code
review.

## Safety Rules

- Every Kubernetes command must include `--context=grigri`.
- Use read-only commands only: `get`, `describe`, `logs`, `top`.
- Never run `kubectl apply`, `delete`, `edit`, `patch`, `rollout`, `scale`, Helm deploy commands, or
  Ansible deployment commands.
- If a fix is identified, report it and suggest commands for the user to run manually.

## Fast Workflow

1. Identify scope: app, namespace, symptom, and when it started.
2. Check Kubernetes state with `kubectl --context=grigri`.
3. Check Grafana alerts, metrics, logs, or dashboard queries as needed.
4. Correlate events with logs, metrics, ArgoCD syncs, and recent repository changes.
5. Report root cause, evidence, and safe next steps.

## Kubernetes Checks

```bash
kubectl --context=grigri get nodes
kubectl --context=grigri get pods -A
kubectl --context=grigri describe pod <pod> -n <namespace>
kubectl --context=grigri logs <pod> -n <namespace> --tail=100
kubectl --context=grigri logs <pod> -n <namespace> --previous
kubectl --context=grigri top pods -n <namespace>
kubectl --context=grigri get events -n <namespace> --sort-by='.lastTimestamp'
kubectl --context=grigri get applications -n argocd
```

Look for pod states, events, image pulls, scheduling failures, OOMKills, resource pressure,
ExternalSecret status, PVC status, and ArgoCD sync errors.

## Grafana Checks

Use `grafana-grigri` tools for the live cluster.

Metrics:

1. Discover metrics with `list_prometheus_metric_names`.
2. Find filters with `list_prometheus_label_values`.
3. Query with `query_prometheus` or `query_prometheus_histogram`.

Logs:

1. Check stream size with `query_loki_stats` using a selector only.
2. Discover labels with `list_loki_label_names` and `list_loki_label_values`.
3. Search with `query_loki_logs` using LogQL filters.

Dashboards and alerts:

- Use `list_alert_rules` and `get_alert_rule_by_uid` for firing or pending alerts.
- Use `get_dashboard_summary` before loading large dashboards.
- Use `get_dashboard_panel_queries` to inspect dashboard PromQL or LogQL.
- Use `list_datasources` if a Prometheus or Loki datasource UID is unknown.

## Common Patterns

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
