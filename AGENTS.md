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

- **Format:** `<scope>: <imperative-description>`
- **Scope:** Directory or service name being modified (e.g., `kube-system:`, `vault:`, `monitoring:`)
- **Description:** Imperative mood, no period, concise (e.g., "Add", "Update", "Fix", "Remove", "Change")
- **Special scopes:** `metal` (Ansible/infrastructure), `docs` (documentation), `renovate` (renovate config)

Examples:

```
kube-system: Add kata-nvidia coordinator operator
vault: Change to svg logo
monitoring: Update Helm release kube-prometheus-stack to v82.10.1
metal: Add k3s node provisioning role
docs: Improve AGENTS.md for coding
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
- **Metal node provisioning:** `cd metal && ANSIBLE_EXTRA_ARGS="-t k3s" make cluster`
  (user must run manually; tags can be adjusted e.g. `-t k3s,gvisor`)

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
- Kata-deploy 3.31.0+ requires containerd drop-in directory (`config-v3.toml.d`) — see
  `docs/troubleshooting/kata-containerd-dropin.md`
- `openebs-zfspv` storage class uses `reclaimPolicy: Retain` — deleted PVCs leave released PVs
  that leak ZFS space. Audit periodically: see `docs/troubleshooting/cluster-hygiene.md`
- High pod restart counts don't always mean problems — check `Last State.Reason` (exit 255 =
  node reboot, not app crash). See `docs/troubleshooting/cluster-hygiene.md`

## Cilium Networking

- Cilium 1.19+ uses the **v2 BGP API** (`CiliumBGPClusterConfig` + `CiliumBGPPeerConfig` +
  `CiliumBGPAdvertisement`). The old `CiliumBGPPeeringPolicy` (v2alpha1) was **removed** — see
  `docs/troubleshooting/cilium-1.19-bgp-migration.md`
- BGP v2 requires `CAP_NET_BIND_SERVICE` in Cilium agent capabilities to bind port 179
- `CiliumBGPAdvertisement` without a `selector` selects **nothing** (not all services) — use
  `matchExpressions` with `DoesNotExist` on a non-existent key to match all services
- Cilium v1.18+ uses TCX BPF which **bypasses all host-side `tc` qdiscs** — TBF, clsact on `lxc*`
  veth devices and `cilium_host` all show 0 bytes
- `kubernetes.io/ingress-bandwidth` pod annotation does **NOT work for same-node traffic** (Cilium
  BPF has `!from_host` check that skips local traffic)
- To rate-limit pod ingress: use tc clsact policer inside pod CNI netns (see
  `system/tc-limiter/` and `docs/troubleshooting/bandwidth-limiting.md`)
- pfSense CPU bottleneck is interrupt processing on PPPoE — limit high-throughput services at the
  pod level before traffic reaches the router
- Check troubleshooting docs (`docs/troubleshooting/`) for service-specific tuning guidance before
  adjusting settings — they may contain recommended values and root cause analyses

## Documenting Troubleshooting Learnings

When debugging uncovers non-obvious behavior, root causes, or operational patterns that future
agents would benefit from, document them so the knowledge is discoverable:

### Where to Document

1. **`docs/troubleshooting/<topic>.md`** — Detailed runbooks with problem description, diagnosis
   commands, root cause analysis, and fix steps. This is the primary location.
2. **`AGENTS.md` Common Pitfalls** — Add a one-line bullet when the learning is a common mistake
   or non-obvious behavior an agent should watch for. Link to the troubleshooting doc.
3. **`AGENTS.md` Cilium / other sections** — If the learning is specific to a subsystem already
   documented in AGENTS.md, append it to that section.

### What to Document

- Non-obvious root causes (e.g., exit code 255 = node reboot, not app crash)
- Operational patterns (e.g., released PVs accumulate with Retain policy)
- Workarounds for known limitations (e.g., Cilium TCX bypasses tc qdiscs)
- Recommended values or configurations discovered through investigation

### Format

Use the existing troubleshooting doc pattern:

```markdown
# Topic

## Problem
What happened, symptoms observed.

## Root Cause
Why it happened.

## How to Diagnose
Commands to identify the issue.

## Fix / Workaround
How to resolve it, or why it's expected behavior.
```

### When to Document

After resolving a non-trivial investigation that required cross-referencing multiple sources
(metrics, logs, kubectl, git history). If an agent in the future would benefit from knowing
this without re-discovering it, write it down.

## Licensing

GPLv3 (see LICENSE.md). Generated code must be compatible.
