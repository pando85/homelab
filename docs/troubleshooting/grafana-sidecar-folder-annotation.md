# Grafana Dashboard Sidecar Folder Annotation

## Problem

A dashboard provisioned via a ConfigMap with `grafana_dashboard: "1"` label lands in the Grafana
root dashboards folder instead of the intended subfolder, even when a target-directory annotation
is set on the ConfigMap.

This was hit when adding the DCGM GPU dashboard: the kustomization set
`k8s-sidecar-target-directory: /tmp/dashboards/gpu`, but the dashboard appeared in `/tmp/dashboards/`
alongside the kube-prometheus dashboards instead of a `gpu/` subfolder.

## Root Cause

The Grafana dashboard sidecar (`grafana-sc-dashboard`) in this cluster is configured with a custom
folder annotation name:

```bash
kubectl --context=grigri get pod -n monitoring <grafana-pod> -o json \
  | jq -r '.spec.containers[] | select(.name | contains("sc-dashboard")) | .env[] | select(.name=="FOLDER_ANNOTATION") | .value'
# => grafana.grafana.com/dashboards.target-directory
```

The sidecar honors **only** the annotation named in `FOLDER_ANNOTATION`
(`grafana.grafana.com/dashboards.target-directory`). The k8s-sidecar default annotation name
(`k8s-sidecar-target-directory`) is silently ignored — the file is still written, but at the root
path, and with `foldersFromFilesStructure: true` no folder is created.

The kube-prometheus-stack dashboards use the correct annotation, which is why they land in
`/tmp/dashboards/kubernetes/`; dashboards using `k8s-sidecar-target-directory` (e.g. smartctl)
silently land at the root.

## How to Diagnose

```bash
# Where the sidecar writes the dashboard file (look for the file at root vs. in a subfolder)
kubectl --context=grigri exec -n monitoring <grafana-pod> -c grafana -- ls /tmp/dashboards/

# Confirm the annotation name the sidecar is configured to read
kubectl --context=grigri get pod -n monitoring <grafana-pod> -o json \
  | jq -r '.spec.containers[] | select(.name | contains("sc-dashboard")) | .env[] | select(.name=="FOLDER_ANNOTATION") | .value'
```

## Fix / Workaround

Use the annotation key that matches the sidecar's `FOLDER_ANNOTATION` env var. In kustomize
`generatorOptions.annotations`:

```yaml
configMapGenerator:
  - name: dcgm-exporter-dashboard
    files:
      - dashboards/dcgm-exporter.json
generatorOptions:
  annotations:
    grafana.grafana.com/dashboards.target-directory: /tmp/dashboards/gpu
  labels:
    grafana_dashboard: "1"
```

If multiple dashboards share a folder, the annotation goes on `generatorOptions` (applies to all
generators in the kustomization). Verify the annotation survives kustomize rendering:

```bash
kustomize build --enable-helm system/gpu-operator/ | grep -A6 "kind: ConfigMap"
```

The dashboard is still usable when placed at the root; this only affects folder organization in
Grafana's dashboard list.

## Related

All dashboard provisioning in this repo uses `grafana.grafana.com/dashboards.target-directory`
in `generatorOptions.annotations`: `system/gpu-operator`, `system/monitoring` (smartctl),
`system/tempo`, `platform/postgres-operator`, and `bootstrap/argocd`.
