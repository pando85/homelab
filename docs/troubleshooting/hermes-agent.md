# Hermes Agent

## Overview

[Hermes Agent](https://hermes-agent.org/) by Nous Research — self-hosted AI agent with persistent
memory, multi-platform messaging gateway, and automated skill creation. Runs inside a Kata VM for
isolation.

## Deployment

| Component | Value |
|-----------|-------|
| Namespace | `hermes` |
| Image | `nousresearch/hermes-agent` (Docker Hub) |
| Runtime | `kata` (VM isolation) |
| Node | `prusik` |
| User | root (required by s6-overlay stage2 hook for volume chown) |
| Data volume | `/opt/data` on PVC `hermes-data` (100Gi, openebs-zfspv) |
| Backups | Daily ZFS snapshots (maxCount=5, 01:00 UTC), quarterly Velero |
| Resources | 500m-8 CPU, 1-4Gi RAM |
| Dashboard | `hermes.internal.grigri.cloud` (port 9119, OIDC via Kanidm) |
| Inference | Custom provider `isidoro` (Kiais) at `isidoro.grigri.cloud/v1` |

## Key Files

| File | Purpose |
|------|---------|
| `apps/hermes/values.yaml` | Helm values (image, runtime, resources, PVC mount, OIDC env) |
| `apps/hermes/templates/pvc.yaml` | PVC with Prune=false protection |
| `apps/hermes/templates/snapshots.yaml` | Daily ZFS snapshot schedule |
| `apps/hermes/templates/kanidm-oauth2-client.yaml` | Kaniop OAuth2 client for dashboard |
| `apps/hermes/templates/kanidm-group.yaml` | KanidmGroup `hermes-users` |
| `/opt/data/config.yaml` | Runtime config (model, skills, plugins) — on PVC, not GitOps |

## Architecture Notes

- s6-overlay is PID 1 (`/init`). It supervises the gateway, dashboard, and per-profile services.
- The ENTRYPOINT is `["/init", "/opt/hermes/docker/main-wrapper.sh"]`. Container args are routed
  through the wrapper — **do not set `command`** (which overrides ENTRYPOINT), use `args` instead.
  Setting `command: ["gateway", "run"]` breaks s6-overlay init and crashes with exit code 2.
- The stage2 hook runs as root to chown `/opt/data`, then drops to the `hermes` user (UID 10000)
  via `s6-setuidgid` for supervised services.
- Multi-profile support: `kubectl exec -n hermes hermes-0 -- hermes profile create <name>`.
  Each profile gets its own supervised gateway with independent state.

## Dashboard OIDC Auth

The dashboard uses the self-hosted OIDC provider (Kanidm) for authentication. Two layers protect
it: oauth2-proxy at the ingress (group-gated) and the dashboard's own OIDC session.

### Dashboard refuses to bind (no auth provider)

**Problem**: Dashboard logs `Refusing to bind dashboard to 0.0.0.0` and never listens on 9119.
Ingress returns 502 Bad Gateway.

**Root Cause**: `HERMES_DASHBOARD_INSECURE` no longer disables the auth gate. The dashboard
requires a registered auth provider (basic auth or OIDC) before binding to a non-loopback address.

**Fix**: Configure OIDC via env vars (done in `values.yaml`):

```yaml
- name: HERMES_DASHBOARD_OIDC_ISSUER
  value: https://idm.grigri.cloud/oauth2/openid/hermes
- name: HERMES_DASHBOARD_OIDC_CLIENT_ID
  valueFrom:
    secretKeyRef:
      name: hermes-kanidm-oauth2-credentials
      key: CLIENT_ID
- name: HERMES_DASHBOARD_OIDC_CLIENT_SECRET
  valueFrom:
    secretKeyRef:
      name: hermes-kanidm-oauth2-credentials
      key: CLIENT_SECRET
```

### Kanidm rejects redirect_uri (invalid_origin)

**Problem**: Login redirects to Kanidm but shows an error. Kanidm logs:
`Invalid OAuth2 redirect_uri (must be an exact match) - got http://hermes.internal.grigri.cloud/auth/callback`.

**Root Cause**: Behind a reverse proxy, the dashboard sees `http://` (the proxy terminates TLS)
and constructs the redirect_uri with the wrong scheme. Kanidm requires an exact match against the
registered `https://` URL.

**Fix**: Set the public URL so the dashboard knows its external origin:

```yaml
- name: HERMES_DASHBOARD_PUBLIC_URL
  value: https://hermes.internal.grigri.cloud
```

Or in `/opt/data/config.yaml`:

```yaml
dashboard:
  public_url: https://hermes.internal.grigri.cloud
```

## Custom Inference Provider (Isidoro/Kiais)

Configured in `/opt/data/config.yaml` (PVC, not GitOps):

```yaml
model:
  default: balanced
  provider: custom
  base_url: https://isidoro.grigri.cloud/v1

custom_providers:
  - name: isidoro
    base_url: https://isidoro.grigri.cloud/v1
```

API key stored in `/opt/data/auth.json` under pool key `custom:isidoro`.

## Plugins and Skills

- **Plugins** are opt-in via `plugins.enabled` list in config.yaml. Empty = nothing bundled loads.
  Do not bulk-enable all plugins — only enable what's needed.
- **Skills** are controlled by `skills.disabled` list. Removed from disabled = enabled.
- Changes to `/opt/data/config.yaml` survive pod restarts (PVC) but are not GitOps-managed.

## Persistent CLI Tools

The container root filesystem is ephemeral. Install standalone tools and their configuration on
the `/opt/data` PVC instead of `/usr/local/bin` or `/root`. See
[Persistent CLI Tools in Hermes](hermes-persistent-cli-tools.md) for the persistent `fj` layout,
token scopes, and verification steps.

## Useful Commands

```bash
# Check pod status
kubectl --context=grigri get pod/hermes-0 -n hermes -o wide

# View gateway logs
kubectl --context=grigri logs -n hermes hermes-0 --tail=50 -f

# Open interactive session
kubectl --context=grigri exec -n hermes -it hermes-0 -- /opt/hermes/.venv/bin/hermes

# Check config
kubectl --context=grigri exec -n hermes hermes-0 -- /opt/hermes/.venv/bin/hermes config show

# Health check
kubectl --context=grigri exec -n hermes hermes-0 -- /opt/hermes/.venv/bin/hermes doctor

# Restart gateway (inside pod)
kubectl --context=grigri exec -n hermes hermes-0 -- /opt/hermes/.venv/bin/hermes gateway run --replace

# Update image tag in values.yaml, then push — ArgoCD auto-syncs
```
