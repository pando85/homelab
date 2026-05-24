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
docs/          # Documentation site
```

## Build/Lint/Test Commands

```bash
# Install hooks
make git-hooks

# Run all checks
pre-commit run --all-files

# Build chart dependencies
helm dependency build apps/<name>/

# Template chart
helm template --include-crds --namespace <namespace> <release-name> apps/<name>/

# Validate chart
helm lint apps/<name>/
```

## Code Style

- **YAML:** 2-space indent, `---` for multi-doc, yamllint via pre-commit (ignores `templates/`)
- **Helm:** Use `app-template` from bjw-s. See `docs/conventions/helm.md` for full patterns
- **Ansible:** `safety` profile. See `docs/conventions/ansible.md`
- **Renovate:** Always add hints above image refs — see `docs/conventions/helm.md#renovate-integration`

## Commit Messages

**Format:** `<scope>: <imperative-description>`

- Scope: directory or service name (`kube-system:`, `vault:`, `monitoring:`)
- Special scopes: `metal` (Ansible), `docs` (documentation), `renovate` (renovate config)

```
kube-system: Add kata-nvidia coordinator operator
vault: Change to svg logo
monitoring: Update Helm release kube-prometheus-stack to v82.10.1
```

## Deployment Restrictions

**CRITICAL: NEVER automatically execute deployment commands.**

- **NEVER run:** `make bootstrap`, `make metal`, `make dev`, `kubectl apply`,
  `helm install/upgrade`, `ansible-playbook`
- **ALWAYS:** Only suggest commands for user to run manually
- **Metal node provisioning:** `cd metal && ANSIBLE_EXTRA_ARGS="-t k3s" make cluster`
  (user must run manually)

## Security

- Never hardcode secrets, tokens, or private keys
- Use ExternalSecret pointing to Vault for all sensitive data
- Assume public repository hygiene at all times

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

## Subsystem Details

- **Cilium networking:** See `docs/conventions/cilium.md` for BGP, TCX, bandwidth limiting details
- **Documenting learnings:** See `docs/conventions/documenting-learnings.md` for when/how to write
  troubleshooting docs

## Licensing

GPLv3 (see LICENSE.md). Generated code must be compatible.
