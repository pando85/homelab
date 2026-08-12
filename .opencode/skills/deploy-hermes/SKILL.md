---
name: deploy-hermes
description: Use when deploying a new Hermes agent instance. Creates Helm chart, configures OIDC, and sets up Telegram gateway.
---

## Scope

Deploy a new Hermes agent instance with isolated namespace, storage, and authentication. Each
instance runs independently with its own configuration on PVC.

## Prerequisites

- Read the full deployment guide: `docs/deployment/hermes-agent.md`
- Determine the instance name (e.g., `hermes-2`, `hermes-3`)
- Decide if internal cluster access is needed (RBAC) or external-only

## Workflow

### 1. Determine Requirements

Ask the user:
- Instance name/number (N)
- Does it need internal cluster access? (exec into other pods)
- Which Telegram bot token to use (from @BotFather)
- Which users to allow (need their Telegram IDs)

### 2. Create Helm Chart

```bash
mkdir -p apps/hermes-N/templates
```

Create files:
- `Chart.yaml` — app-template v5.0.1 dependency
- `values.yaml` — controller, ingress, OIDC config (replace all `hermes` with `hermes-N`)
- `templates/pvc.yaml` — 100Gi PVC with Prune=false
- `templates/snapshots.yaml` — daily ZFS snapshots
- `templates/kanidm-oauth2-client.yaml` — OAuth2 client for dashboard (reuse `hermes-users` group)
- `templates/rbac.yaml` — **only if internal access needed** (unique Role names: `hermes-N-agent-*`)

### 3. Key Substitutions in values.yaml

| Original | Replace With |
|----------|--------------|
| `hermes` (controller/container name) | `hermes-N` |
| `hermes.internal.grigri.cloud` | `hermes-N.internal.grigri.cloud` |
| `openid/hermes` | `openid/hermes-N` |
| `hermes-kanidm-oauth2-credentials` | `hermes-N-kanidm-oauth2-credentials` |
| `hermes-data` (PVC) | `hermes-N-data` |
| `hermes-dashboard-tls-certificate` | `hermes-N-dashboard-tls-certificate` |

**Keep unchanged**: `allowed_groups=hermes-users@idm.grigri.cloud` (shared Kanidm group)

### 4. Validate and Deploy

```bash
helm dependency build apps/hermes-N/
helm lint apps/hermes-N/
helm template --include-crds --namespace hermes-N hermes-N apps/hermes-N/

git add apps/hermes-N/
git commit -m "hermes-N: Add Hermes agent instance N"
git push
```

Wait for ArgoCD sync (check with `kubectl --context=grigri get application hermes-N -n argocd`).

### 5. Post-Deployment Configuration

**IMPORTANT: Do NOT copy `config.yaml` from another instance** — this overwrites the new instance's
unique identity. Only copy `auth.json` if you need to share API credentials.

```bash
# Only copy auth.json if needed (API credentials)
kubectl --context=grigri exec -n hermes hermes-0 -- cat /opt/data/auth.json > /tmp/auth.json
kubectl --context=grigri cp /tmp/auth.json hermes-N/hermes-N-0:/opt/data/auth.json
```

The `config.yaml` will be auto-generated with defaults when the gateway starts. Edit it to
configure the model, dashboard URL, and OAuth issuer:

```bash
# Edit config.yaml to set instance-specific values
kubectl --context=grigri exec -n hermes-N hermes-N-0 -- vi /opt/data/config.yaml
```

Required settings in `config.yaml`:
- `dashboard.public_url`: `https://hermes-N.internal.grigri.cloud`
- `dashboard.oauth.self-hosted.issuer`: `https://idm.grigri.cloud/oauth2/openid/hermes-N`

### 6. Configure Telegram

Edit `/opt/data/.env` on the PVC:

```bash
TELEGRAM_BOT_TOKEN=<token>
TELEGRAM_ALLOWED_USERS=<user-id-1>,<user-id-2>
TELEGRAM_HOME_CHANNEL=<user-id-1>
```

**Getting Telegram User IDs**: Users must message the bot first, then:

```bash
curl -s "https://api.telegram.org/bot<TOKEN>/getUpdates" | jq '.result[].message.from'
```

### 7. Restart Gateway

```bash
kubectl --context=grigri exec -n hermes-N hermes-N-0 -- pkill -f "hermes gateway"
# s6-overlay auto-restarts
```

### 8. Verify

```bash
kubectl --context=grigri exec -n hermes-N hermes-N-0 -- /opt/hermes/.venv/bin/hermes gateway status
kubectl --context=grigri exec -n hermes-N hermes-N-0 -- /opt/hermes/.venv/bin/hermes config show
kubectl --context=grigri logs -n hermes-N hermes-N-0 --tail=50
```

## Critical Pitfalls

### ArgoCD Shared Resource Conflicts

If RBAC Roles have the same name across instances, ArgoCD reports `SharedResourceWarning`.
**Fix**: Use unique Role names per instance (`hermes-N-agent-*`).

### Dashboard Refuses to Bind

Logs show `Refusing to bind dashboard to 0.0.0.0`. **Fix**: Ensure `HERMES_DASHBOARD_OIDC_ISSUER`
is set and Kanidm OAuth2 client is registered with correct origin.

### Telegram Bot Not Responding

Logs show `No messaging platforms enabled`. **Fix**: Ensure `.env` has `TELEGRAM_BOT_TOKEN` and
file is readable. Restart gateway after changes.

### OIDC Issuer Must Be Unique

Each instance needs its own Kanidm OAuth2 client with unique issuer (`openid/hermes-N`). Sharing
causes redirect conflicts.

### Config Overwrite Warning

**NEVER copy `config.yaml`, `memories/`, or `skills/` from another instance** — this overwrites
the target's unique identity (personalities, MCPs, memories). Only copy `auth.json` if needed.
If you accidentally overwrite, recovery requires ZFS snapshot rollback.
See `docs/troubleshooting/hermes-config-overwrite-recovery.md`

## Configuration Files (on PVC)

| File | Purpose | Copy from other instance? |
|------|---------|---------------------------|
| `/opt/data/config.yaml` | Model, skills, plugins, dashboard settings | **NO** — has unique identity |
| `/opt/data/auth.json` | API credentials for inference providers | YES — if needed |
| `/opt/data/.env` | Environment variables (Telegram token, allowed users) | **NO** — create fresh |
| `/opt/data/memories/` | Instance memories and knowledge | **NO** — unique to instance |
| `/opt/data/skills/` | Instance-specific skills and MCPs | **NO** — unique to instance |

These are NOT managed by GitOps. Create new config for each instance.

## Verification Checklist

- [ ] ArgoCD application synced and healthy
- [ ] Pod running on target node
- [ ] PVC bound
- [ ] Ingress created with correct host
- [ ] TLS certificate issued
- [ ] Kanidm OAuth2 client registered
- [ ] Dashboard accessible at `https://hermes-N.internal.grigri.cloud`
- [ ] OIDC login works
- [ ] Telegram bot responds to allowed users
- [ ] Gateway status shows Telegram connected
