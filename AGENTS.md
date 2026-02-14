# AGENTS.md

Guidelines for agentic coding agents working in this homelab repository.

## Repository Overview

Self-hosted Kubernetes (K3s) homelab managed via GitOps. Core principles: declarative
infrastructure, repeatable automation, small blast radius, explicit version pinning, minimal drift.

**Tech Stack:** K3s, ArgoCD, Helm (template-only), Ansible (bootstrap), External Secrets + Vault,
cert-manager, ingress-nginx, Renovate, ZFS, Cilium (BGP).

**Cluster Access:** This is a GitOps repository. Use `kubectl --context=grigri` to see what is
running in the cluster. Metrics are available at https://prometheus.internal.grigri.cloud

## Directory Structure

```
apps/          # Application Helm charts (one subfolder per app)
system/        # Cluster-wide components (ingress, cert-manager, monitoring, identity)
platform/      # Supporting operators/platform services (Vault, external-secrets, git, reloader)
bootstrap/     # ArgoCD bootstrap layer
metal/         # Ansible playbooks, inventory, roles for K3s node provisioning
scripts/       # Utility scripts (deploy-dir.rh for Helm rendering)
test/          # Local k3d test cluster helpers
docs/          # MkDocs documentation site
```

## Build/Lint/Test Commands

### Pre-commit (Required Before Committing)

```bash
# Install hooks
make git-hooks

# Run all checks manually
pre-commit run --all-files

# Run specific hooks
pre-commit run yamllint --all-files
pre-commit run helmlint --all-files
pre-commit run shellcheck --all-files
```

### Helm Commands

```bash
# Build chart dependencies
helm dependency build apps/<name>/

# Template chart (render without applying)
helm template --include-crds --namespace <namespace> <release-name> apps/<name>/

# Validate chart syntax
helm lint apps/<name>/
```

## Code Style Guidelines

### YAML

- **Linting:** yamllint via pre-commit (ignores `templates/`)
- **Document-start:** Disabled
- **Line-length:** Disabled
- Always use 2-space indentation
- Use `---` document separator for multi-doc files
- Quote strings containing special characters

### Commit Messages

- Format: Conventional Commits with sentence-case or lower-case subject
- No enforced type-enum (types are suggestions)
- Blank line before body required

Example:

```
add new monitoring dashboard

feat(monitoring): add grafana dashboard for stump
```

### Helm Charts

**Chart.yaml Pattern:**

```yaml
apiVersion: v2
name: <app-name>
version: 0.0.0
dependencies:
  - name: app-template
    version: 4.6.2
    repository: https://bjw-s-labs.github.io/helm-charts/
```

**values.yaml Patterns:**

- Use `app-template` from bjw-s as the standard wrapper
- Always define `resources.requests` and `resources.limits`
- Use YAML anchors for repeated probe definitions (`&probes`, `*probes`)
- Set `enableServiceLinks: false` in `defaultPodOptions`
- Use `fsGroupChangePolicy: "OnRootMismatch"` for performance

### Labels and Annotations

**Standard Labels:**

```yaml
labels:
  app.kubernetes.io/instance: <name>
  app.kubernetes.io/name: <name>
  backup: <snapshot-name>
  backup/retain: quarterly|monthly|weekly
```

**PVC Sync Protection:**

```yaml
annotations:
  argocd.argoproj.io/sync-options: Prune=false
```

**Ingress Annotations (Internal):**

```yaml
annotations:
  external-dns.alpha.kubernetes.io/enabled: "true"
  cert-manager.io/cluster-issuer: letsencrypt-prod-dns
```

**Ingress Annotations (External):**

```yaml
annotations:
  external-dns.alpha.kubernetes.io/enabled: "true"
  external-dns.alpha.kubernetes.io/target: grigri.cloud
  cert-manager.io/cluster-issuer: letsencrypt-prod-dns
```

### External Secrets

```yaml
apiVersion: external-secrets.io/v1
kind: ExternalSecret
metadata:
  name: <app-name>
spec:
  secretStoreRef:
    kind: ClusterSecretStore
    name: vault
  target:
    name: <app-name>
  data:
    - secretKey: ENV_VAR_NAME
      remoteRef:
        key: /<app-name>/<path>
        property: <field>
```

### Ansible (metal/)

- Profile: `safety` (ansible-lint)
- Task name prefix: `{stem} | `
- Variable naming: `^[a-z_][a-z0-9_]*$`
- Use `become: true` for privilege escalation
- Keep roles in `roles/` with standard structure

### Renovate Integration

Always add Renovate hints above image references:

```yaml
# renovate: datasource=docker depName=ghcr.io/OWNER/IMAGE
image:
  repository: ghcr.io/owner/image
  tag: 1.2.3
```

For custom image fields:

```yaml
# renovate: datasource=docker depName=postgres
dockerImage: postgres:16
```

## Adding a New App

1. Create `apps/<name>/` with `Chart.yaml`, `values.yaml`, `templates/`
2. For upstream charts: add dependency block, run `helm dependency build`, commit `Chart.lock`
3. Namespace equals folder name (auto-created by deploy script)
4. Pin all images with Renovate hints
5. Add Ingress with cert-manager + external-dns annotations
6. Define resource requests and limits
7. Create ExternalSecret objects for secrets (source: Vault path)
8. Create PVC with `Prune=false` annotation for persistent data

## Deployment Restrictions

**CRITICAL: NEVER automatically execute deployment commands.**

- **NEVER run:** `make bootstrap`, `make metal`, `make dev`, `kubectl apply`,
  `helm install/upgrade`, `ansible-playbook`
- **ALWAYS:** Only suggest commands for user to run manually
- **REASON:** This is a production GitOps repository. Deployments must be user-initiated.

## Security

- Never hardcode secrets, tokens, or private keys
- Use ExternalSecret pointing to Vault for all sensitive data
- Assume public repository hygiene at all times
- Private keys detected by pre-commit hook will fail

## Common Pitfalls

- Forgetting `helm dependency build` after updating `Chart.yaml`
- Missing Renovate hints causes images to not auto-update
- Introducing CRDs without `--include-crds` in helm template
- Not waiting for webhooks (cert-manager, external-secrets) before applying dependent resources
- Forgetting `Prune=false` on PVCs causes data loss on sync

## Licensing

GPLv3 (see LICENSE.md). Generated code must be compatible.
