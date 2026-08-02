# Grafana datasource sidecar ConfigMap collision

## Symptoms

- Grafana datasource provisioning continuously reloads (sidecar logs show repeated `Writing` and `reload` requests)
- Datasource updates don't take effect
- Grafana logs show `Failed to provision data sources` errors
- Multiple ConfigMaps with the same data key cause last-writer-wins behavior

## Root cause

The Grafana datasource sidecar (`grafana-sc-datasources`) watches ConfigMaps labeled `grafana_datasource: "1"` and writes their data keys as files in `/etc/grafana/provisioning/datasources/`.

If two ConfigMaps use the same data key (e.g., both use `datasource.yaml`), the sidecar writes both to the same file path, causing a collision. The last ConfigMap processed overwrites the first, and the sidecar enters a continuous reload loop trying to reconcile the conflict.

Example collision:
```yaml
# ConfigMap 1: monitoring-kube-prometheus-grafana-datasource
data:
  datasource.yaml: |
    datasources:
      - name: Prometheus
        ...

# ConfigMap 2: long-term-prometheus-grafana-datasource
data:
  datasource.yaml: |  # ← SAME KEY
    datasources:
      - name: Prometheus long term
        ...
```

Both ConfigMaps write to `/etc/grafana/provisioning/datasources/datasource.yaml`, causing the collision.

## Solution

Use unique data keys per ConfigMap, following the gitops-services pattern:

```yaml
# ConfigMap 1: monitoring-kube-prometheus-grafana-datasource
data:
  datasource.yaml: |  # Keep as-is (chart-managed)
    datasources:
      - name: Prometheus
        ...

# ConfigMap 2: long-term-prometheus-grafana-datasource
data:
  long-term-prometheus-datasource.yaml: |  # ← UNIQUE KEY
    datasources:
      - name: Prometheus long term
        ...

# ConfigMap 3: loki-datasource-config
data:
  loki-datasource.yaml: |  # ← UNIQUE KEY
    datasources:
      - name: Loki
        ...

# ConfigMap 4: tempo-datasource-config
data:
  tempo-datasource.yaml: |  # ← UNIQUE KEY
    datasources:
      - name: Tempo
        ...
```

Each ConfigMap now writes to a separate file:
- `/etc/grafana/provisioning/datasources/datasource.yaml`
- `/etc/grafana/provisioning/datasources/long-term-prometheus-datasource.yaml`
- `/etc/grafana/provisioning/datasources/loki-datasource.yaml`
- `/etc/grafana/provisioning/datasources/tempo-datasource.yaml`

## Verification

After applying unique data keys:
1. Check the sidecar logs — should stop showing continuous reloads
2. Verify all datasources are present in Grafana UI
3. Confirm datasource updates take effect (e.g., Loki derivedFields, Tempo tracesToLogs)

## Reference

This pattern is documented in the gitops-services repository:
`/work/gitops/gitops-services/nbg1-c02-prod/monitoring/`
