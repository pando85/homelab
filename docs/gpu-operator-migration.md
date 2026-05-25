# GPU Operator Migration

Migrating from the standalone `nvidia-device-plugin` helm chart to the full NVIDIA GPU Operator, while keeping the Ansible-managed host driver and `nvidia-patch` intact.

## What Changes

| Component | Before | After |
|---|---|---|
| Driver | Ansible (`nvidia-headless-550-server` + nvidia-patch) | **Unchanged** — `driver.enabled: false` |
| Container toolkit | Ansible (`nvidia-container-runtime` apt) | **Unchanged** — `toolkit.enabled: false` |
| RuntimeClass `nvidia` | Manual (`kube-system/resources/runtime-class-nvidia.yaml`) | **Unchanged** — kept manual |
| Device plugin | Standalone chart in `kube-system` (v0.19.1, 8 replicas) | GPU operator DaemonSet in `gpu-operator` |
| GFD | Enabled in standalone chart | GPU operator managed |
| NFD | Via node label `feature.node.kubernetes.io/pci-10de.present` | GPU operator managed (full NFD) |
| DCGM exporter | Not deployed | **New** — GPU metrics via Prometheus |
| kata-nvidia coordinator | Watches `kube-system-nvidia-device-plugin` in `kube-system` | Watches `nvidia-device-plugin-daemonset` in `gpu-operator` |

## Why driver and toolkit stay on the host

The Ansible role (`metal/roles/nvidia-container-runtime`) runs [keylase/nvidia-patch](https://github.com/keylase/nvidia-patch) after driver install. This patch removes the NVENC concurrent encoding session limit, which is needed by Jellyfin and any other transcoding workloads. If the GPU operator managed the driver (containerized), the patch would need to be re-applied on every driver restart — too fragile. Keeping the host driver is the right call.

## Branch

`system/gpu-operator` — single commit, all hooks pass.

## Apply

```bash
cd ~/homelab
git push origin system/gpu-operator
# open PR and merge
```

ArgoCD will automatically detect `system/gpu-operator/` as a new Application (the `system` ApplicationSet watches `system/*`).

**Order of events after merge:**

1. ArgoCD syncs `kube-system` — removes `nvidia-device-plugin` DaemonSet
2. ArgoCD syncs `gpu-operator` — creates namespace, deploys GPU operator
3. GPU operator deploys NFD worker DaemonSet → labels GPU nodes
4. GPU operator deploys device-plugin DaemonSet (in `gpu-operator` ns) → 8 GPU slots
5. GPU operator deploys DCGM exporter → metrics available

## Verify after apply

```bash
# GPU operator pods healthy
kubectl get pods -n gpu-operator

# Node advertising 8 GPU slots (same as before)
kubectl get node <gpu-node> -o jsonpath='{.status.allocatable.nvidia\.com/gpu}'
# expected: 8

# DCGM metrics (new)
kubectl port-forward -n gpu-operator svc/nvidia-dcgm-exporter 9400:9400
curl localhost:9400/metrics | grep DCGM_FI_DEV_GPU_UTIL
```

## Existing apps

No changes needed. Apps (`ollama`, `jellyfin`) use:
- `resources.limits.nvidia.com/gpu: 1` ✅ same resource name
- `runtimeClassName: nvidia` ✅ same RuntimeClass, same handler

## Next step: local model inference

With the GPU operator deployed and DCGM metrics available, the next step is running a local LLM via Ollama. The current Ollama deployment already requests `nvidia.com/gpu: 1` and `runtimeClassName: nvidia` — it will work immediately after the migration.

To run a specific model:
```yaml
# apps/ollama/values.yaml
ollama:
  models:
    run:
      - llama3.2:3b     # ~2 GB VRAM, fits easily in one time-slice
      - qwen2.5:7b      # ~4.5 GB VRAM
```

With 8 time-sliced slots from one GPU, multiple Ollama instances or other inference workloads can coexist.
