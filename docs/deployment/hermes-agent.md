# Deploy a New Hermes Agent Instance

## When to Create a New Instance

Each Hermes agent runs in its own namespace with isolated storage, auth, and lifecycle. Create a
new instance when you need:

- A separate agent for a different purpose (e.g., different messaging channels, different skills)
- Isolated data and configuration (each instance has its own `/opt/data` PVC)
- Independent scaling and resource allocation

## Naming Convention

Parameterize by instance name `N` (e.g., `hermes`, `hermes-2`, `hermes-3`):

| Resource | Pattern |
|---|---|
| Directory | `apps/hermes-N/` |
| Namespace | `hermes-N` |
| Controller/Pod | `hermes-N` / `hermes-N-0` |
| PVC | `hermes-N-data` |
| Ingress | `hermes-N.internal.grigri.cloud` |
| Kanidm OAuth2 Client | `hermes-N` |
| Kanidm Group | `hermes-users` (shared across all instances) |
| ServiceAccount | `hermes-agent` (in namespace `hermes-N`) |
| Snapshot Schedule | `hermes-N-zfs-backups` |
| Backup label | `backup: hermes-N-zfs` |

## Step-by-Step Deployment

### 1. Create the Chart Directory

```bash
mkdir -p apps/hermes-N/templates
```

### 2. Create `Chart.yaml`

```yaml
apiVersion: v2
name: hermes-N
version: 0.0.0
dependencies:
  - name: app-template
    version: 5.0.1
    repository: https://bjw-s-labs.github.io/helm-charts/
```

### 3. Create `values.yaml`

Copy from an existing instance and replace:

- Controller name: `hermes` → `hermes-N`
- Container name: `hermes` → `hermes-N`
- `HERMES_DASHBOARD_PUBLIC_URL`: `https://hermes-N.internal.grigri.cloud`
- `HERMES_DASHBOARD_OIDC_ISSUER`: `https://idm.grigri.cloud/oauth2/openid/hermes-N`
- Secret name: `hermes-kanidm-oauth2-credentials` → `hermes-N-kanidm-oauth2-credentials`
- Ingress host: `hermes.internal.grigri.cloud` → `hermes-N.internal.grigri.cloud`
- TLS secret: `hermes-dashboard-tls-certificate` → `hermes-N-dashboard-tls-certificate`
- PVC claim: `hermes-data` → `hermes-N-data`
- `advancedMounts` keys: `hermes` → `hermes-N`

**Important**: Keep `allowed_groups=hermes-users@idm.grigri.cloud` in ingress annotations — all
instances share the same Kanidm group.

### 4. Create `templates/pvc.yaml`

```yaml
apiVersion: v1
kind: PersistentVolumeClaim
metadata:
  labels:
    app.kubernetes.io/instance: hermes-N
    app.kubernetes.io/name: hermes-N
    backup: hermes-N-zfs
    backup/retain: quaterly
  annotations:
    argocd.argoproj.io/sync-options: Prune=false
  name: hermes-N-data
spec:
  accessModes:
    - ReadWriteOnce
  resources:
    requests:
      storage: 100Gi
```

### 5. Create `templates/snapshots.yaml`

```yaml
apiVersion: snapscheduler.backube/v1
kind: SnapshotSchedule
metadata:
  name: hermes-N-zfs-backups
spec:
  retention:
    maxCount: 5
  schedule: "0 1 * * *" # UTC
  claimSelector:
    matchLabels:
      backup: hermes-N-zfs
```

### 6. Create `templates/kanidm-oauth2-client.yaml`

```yaml
---
apiVersion: kaniop.rs/v1beta1
kind: KanidmOAuth2Client
metadata:
  name: hermes-N
spec:
  kanidmRef:
    name: kanidm
    namespace: kanidm

  displayname: hermes-N

  origin: https://hermes-N.internal.grigri.cloud/

  redirectUrl:
    - https://hermes-N.internal.grigri.cloud/auth/callback

  scopeMap:
    - group: hermes-users
      scopes:
        - openid
        - profile
        - email

  preferShortUsername: true
  strictRedirectUrl: true
```

**Note**: No `kanidm-group.yaml` — all instances share the existing `hermes-users` group.

### 7. Create `templates/rbac.yaml` (Optional)

**Only needed if the agent requires internal cluster access** (e.g., exec into other pods).

If the agent only needs external access (Telegram, web dashboard), skip this file entirely.

If needed, copy from an existing instance and update:

- Role names: `hermes-agent-*` → `hermes-N-agent-*` (must be unique per instance to avoid ArgoCD shared resource conflicts)
- RoleBinding names: `hermes-agent-*` → `hermes-N-agent-*`
- RoleBinding subjects namespace: `namespace: hermes` → `namespace: hermes-N`
- RoleBinding roleRef names: update to reference the new Role names

### 8. Validate and Deploy

```bash
# Build dependencies
helm dependency build apps/hermes-N/

# Lint
helm lint apps/hermes-N/

# Template (verify resource names)
helm template --include-crds --namespace hermes-N hermes-N apps/hermes-N/

# Commit and push — ArgoCD auto-syncs
git add apps/hermes-N/
git commit -m "hermes-N: Add Hermes agent instance N"
git push
```

## Post-Deployment Configuration

After ArgoCD syncs and the pod is running, configure the agent by copying files to the PVC.

### Configuration Files (on PVC)

All runtime configuration is stored on the PVC at `/opt/data/`:

| File | Purpose |
|------|---------|
| `config.yaml` | Main config: model, skills, plugins, dashboard settings |
| `auth.json` | API credentials for inference providers |
| `.env` | Environment variables: Telegram bot token, allowed users |

**Important**: These files are NOT managed by GitOps. Copy them from an existing instance or create new ones.

Standalone CLI tools and their runtime credentials must also be placed under `/opt/data`; files
installed into the container root filesystem disappear when the pod is recreated. See
[Persistent CLI Tools in Hermes](../troubleshooting/hermes-persistent-cli-tools.md) for the `fj`
installation and authentication pattern.

### Copy Configuration from Existing Instance

```bash
# Copy config files from hermes to hermes-N
kubectl --context=grigri exec -n hermes hermes-0 -- cat /opt/data/config.yaml > /tmp/config.yaml
kubectl --context=grigri exec -n hermes hermes-0 -- cat /opt/data/auth.json > /tmp/auth.json
kubectl --context=grigri exec -n hermes hermes-0 -- cat /opt/data/.env > /tmp/.env

# Update instance-specific values in config.yaml:
# - dashboard.public_url: https://hermes-N.internal.grigri.cloud
# - dashboard.oauth.self-hosted.issuer: https://idm.grigri.cloud/oauth2/openid/hermes-N

# Copy to new instance
kubectl --context=grigri cp /tmp/config.yaml hermes-N/hermes-N-0:/opt/data/config.yaml
kubectl --context=grigri cp /tmp/auth.json hermes-N/hermes-N-0:/opt/data/auth.json
kubectl --context=grigri cp /tmp/.env hermes-N/hermes-N-0:/opt/data/.env
```

### Configure Telegram

Edit `/opt/data/.env` on the PVC:

```bash
# Telegram Bot Token (from @BotFather)
TELEGRAM_BOT_TOKEN=<bot-token-from-botfather>

# Comma-separated list of allowed user IDs
TELEGRAM_ALLOWED_USERS=<user-id-1>,<user-id-2>

# Default chat for cron delivery
TELEGRAM_HOME_CHANNEL=<user-id-1>
```

**Getting Telegram User IDs**: Users must send a message to the bot first, then check updates:

```bash
curl -s "https://api.telegram.org/bot<TOKEN>/getUpdates" | jq '.result[].message.from'
```

### Restart Gateway

After configuration changes, restart the gateway:

```bash
kubectl --context=grigri exec -n hermes-N hermes-N-0 -- pkill -f "hermes gateway"
# s6-overlay will auto-restart the gateway
```

### Verify Configuration

```bash
# Check gateway status
kubectl --context=grigri exec -n hermes-N hermes-N-0 -- /opt/hermes/.venv/bin/hermes gateway status

# Check config
kubectl --context=grigri exec -n hermes-N hermes-N-0 -- /opt/hermes/.venv/bin/hermes config show

# Check logs
kubectl --context=grigri logs -n hermes-N hermes-N-0 --tail=50
```

## Common Pitfalls

### ArgoCD Shared Resource Conflicts

If RBAC Roles have the same name across instances (e.g., `hermes-agent-radarr` in both `hermes` and `hermes-2`), ArgoCD reports `SharedResourceWarning` and the app stays `OutOfSync`. **Solution**: Use unique Role names per instance (`hermes-N-agent-*`).

### Dashboard Refuses to Bind (No Auth Provider)

Dashboard logs `Refusing to bind dashboard to 0.0.0.0` and ingress returns 502. **Solution**: Ensure `HERMES_DASHBOARD_OIDC_ISSUER` is set and the Kanidm OAuth2 client is registered with the correct origin and redirect URL.

### Kanidm Rejects Redirect URI

Login redirects to Kanidm but shows an error. **Solution**: Set `HERMES_DASHBOARD_PUBLIC_URL` to the exact external URL (with `https://`). Kanidm requires an exact match.

### Telegram Bot Not Responding

Gateway logs show `No messaging platforms enabled`. **Solution**: Ensure `.env` file has `TELEGRAM_BOT_TOKEN` set and the file is readable by the hermes user. Restart the gateway after changes.

### OIDC Issuer Must Be Unique

Each instance needs its own Kanidm OAuth2 client with a unique issuer URL (`openid/hermes-N`). Sharing the same issuer across instances causes redirect conflicts.

## Checklist

- [ ] Kanidm users are members of `hermes-users` group (shared across all instances)
- [ ] DNS propagated for `hermes-N.internal.grigri.cloud`
- [ ] TLS certificate issued (check cert-manager)
- [ ] Dashboard accessible at `https://hermes-N.internal.grigri.cloud`
- [ ] OIDC login works (Kanidm OAuth2 client registered)
- [ ] Inference provider configured
- [ ] Required skills/plugins enabled
- [ ] Messaging channels connected (if needed)
- [ ] Required standalone CLI tools and their configuration are stored under `/opt/data`

## Useful Commands

```bash
# Check pod status
kubectl --context=grigri get pod/hermes-N-0 -n hermes-N -o wide

# View logs
kubectl --context=grigri logs -n hermes-N hermes-N-0 --tail=50 -f

# Open interactive session
kubectl --context=grigri exec -n hermes-N -it hermes-N-0 -- /opt/hermes/.venv/bin/hermes

# Health check
kubectl --context=grigri exec -n hermes-N hermes-N-0 -- /opt/hermes/.venv/bin/hermes doctor
```
