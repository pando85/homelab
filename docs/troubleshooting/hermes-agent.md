# Hermes Agent

## Overview

[Hermes Agent](https://hermes-agent.org/) by Nous Research — self-hosted AI agent with persistent
memory, multi-platform messaging gateway, and automated skill creation. Runs inside a Kata VM for
isolation.

## Deployment

| Component | Value |
|-----------|-------|
| Namespace | `hermes` |
| Image | `nousresearch/hermes-agent:v2026.5.29.2` (Docker Hub) |
| Runtime | `kata` (VM isolation) |
| Node | `prusik` |
| User | root (required by s6-overlay stage2 hook for volume chown) |
| Data volume | `/opt/data` on PVC `hermes-data` (5Gi, openebs-zfspv) |
| Backups | Daily ZFS snapshots (maxCount=5, 01:00 UTC), quarterly Velero |
| Resources | 500m-2 CPU, 1-4Gi RAM |

## Next Steps

The pod is running but **not yet configured**. Steps to complete setup:

1. **Initial config** — run setup wizard:
   ```bash
   kubectl exec -n hermes -it hermes-0 -- hermes setup
   ```
   Or configure manually:
   ```bash
   kubectl exec -n hermes -it hermes-0 -- hermes model    # pick provider/model
   kubectl exec -n hermes -it hermes-0 -- hermes tools    # enable/disable tools
   ```

2. **API keys** — stored in `/opt/data/.env`. Can be set via:
   ```bash
   kubectl exec -n hermes -it hermes-0 -- hermes config set OPENROUTER_API_KEY sk-...
   ```
   Or use Nous Portal OAuth: `hermes setup --portal`

3. **Messaging gateway** — connect Telegram/Discord/Slack/etc:
   ```bash
   kubectl exec -n hermes -it hermes-0 -- hermes gateway setup
   ```

4. **Dashboard** (optional) — enable web UI by adding env var `HERMES_DASHBOARD=1` to values.yaml.
   Binds port 9119 inside container. Would need a Service + Ingress added.

5. **Ingress** (optional) — if you want the gateway API server or dashboard accessible externally,
   add a Service (port 8642 for API, 9119 for dashboard) and Ingress to values.yaml.

## Key Files

| File | Purpose |
|------|---------|
| `apps/hermes/values.yaml` | Helm values (image, runtime, resources, PVC mount) |
| `apps/hermes/templates/pvc.yaml` | PVC with Prune=false protection |
| `apps/hermes/templates/snapshots.yaml` | Daily ZFS snapshot schedule |

## Architecture Notes

- s6-overlay is PID 1 (`/init`). It supervises the gateway, dashboard, and per-profile services.
- The ENTRYPOINT is `["/init", "/opt/hermes/docker/main-wrapper.sh"]`. Container args are routed
  through the wrapper — **do not set `command`** (which overrides ENTRYPOINT), use `args` instead.
  Setting `command: ["gateway", "run"]` breaks s6-overlay init and crashes with exit code 2.
- The stage2 hook runs as root to chown `/opt/data`, then drops to the `hermes` user (UID 10000)
  via `s6-setuidgid` for supervised services.
- Multi-profile support: `kubectl exec -n hermes hermes-0 -- hermes profile create <name>`.
  Each profile gets its own supervised gateway with independent state.

## Useful Commands

```bash
# Check pod status
kubectl get pod/hermes-0 -n hermes -o wide

# View gateway logs
kubectl logs -n hermes hermes-0 --tail=50 -f

# Open interactive session
kubectl exec -n hermes -it hermes-0 -- hermes

# Check config
kubectl exec -n hermes hermes-0 -- hermes config

# Health check
kubectl exec -n hermes hermes-0 -- hermes doctor

# Update image tag in values.yaml, then push — ArgoCD auto-syncs
```
