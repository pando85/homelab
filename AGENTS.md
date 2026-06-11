# AGENTS.md

Concise repository guidance for agentic coding agents. Keep this file short: add detailed runbooks
under `docs/`, and only keep high-signal reminders here.

## Repository Overview

Self-hosted Kubernetes (K3s) homelab managed via GitOps. Core principles: declarative
infrastructure, repeatable automation, small blast radius, explicit version pinning, minimal drift.

**Tech Stack:** K3s, ArgoCD, Helm (template-only), Ansible (bootstrap), External Secrets + Vault,
cert-manager, ingress-nginx, Renovate, ZFS, Cilium (BGP).

**Cluster Access:** This is a GitOps repository. Use `kubectl --context=grigri` to see what is
running in the cluster. Metrics are available at https://prometheus.internal.grigri.cloud

## Repository Map

```
apps/      # Application Helm charts, one release per folder
system/    # Cluster-wide components: ingress, cert-manager, monitoring, identity
platform/  # Supporting operators/services: Vault, external-secrets, git, reloader
bootstrap/ # ArgoCD bootstrap layer
metal/     # Ansible inventory, playbooks, roles for K3s node provisioning
scripts/   # Utility scripts, including deploy-dir.rh for Helm rendering
test/      # Local k3d test helpers
docs/      # MkDocs documentation and runbooks
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
- **Docs:** Put durable troubleshooting details in `docs/troubleshooting/`; keep `AGENTS.md` to links
  and one-line reminders. See `docs/conventions/documenting-learnings.md`

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

## Skill Usage

- Use the `debug` skill only for live cluster investigation with read-only `kubectl` and Grafana
  observability.
- Do not copy runbook steps into agent instructions. Link to the relevant docs page instead.
- Prefer focused, path-specific docs over adding broad always-loaded guidance.

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
- `openebs-zfspv` with `fstype: zfs` + `fsGroup` causes slow pod startup (recursive chown on
  every mount). `fsGroupChangePolicy: OnRootMismatch` doesn't help — kubelet resets setgid bit.
  If the app manages its own file ownership (runs as volume owner or has init chown), remove
  `fsGroup` entirely. See `docs/troubleshooting/openebs-zfspv-slow-startup-fsgroup.md`
- High pod restart counts don't always mean problems — check `Last State.Reason` (exit 255 =
  node reboot, not app crash). See `docs/troubleshooting/cluster-hygiene.md`
- Armbian kernel 6.12 on Odroid HC4 breaks Cilium UDP BPF masquerading — hold kernel at 6.6 LTS
  (`24.11.1`). See `docs/troubleshooting/armbian-kernel-bpf-masquerade.md`
- Cilium BPF datapath can go stale on a node — pod egress breaks while host network works. Can
  manifest as partial breakage (some connections work, others don't) — e.g. Vector buffer filling
  on one sink while another on the same pod is fine. Fix: delete the Cilium pod.
  See `docs/troubleshooting/cilium-stale-bpf-egress.md`
- Cilium LRP `skipRedirectFromBackend` is broken in v1.19.4 — nodelocaldns with `serviceMatcher`
  needs a corefile-watcher sidecar to forward directly to CoreDNS pod IPs, avoiding redirect loop.
  `addressMatcher` has post-reboot bugs (PR #45522). See
  `docs/troubleshooting/nodelocaldns-cilium-lrp.md`
- ArgoCD has `selfHeal: true` with real-time cluster watches — `kubectl apply` will be detected
  and reverted almost instantly. Always commit/push first, let ArgoCD sync, then verify.
  See `docs/troubleshooting/argocd-gitops-workflow.md`
- Unattended-upgrades must blacklist NVIDIA packages — host-level driver upgrades conflict with
  the GPU Operator's containerized driver management, causing `Driver/library version mismatch`.
  Fix: `cd metal && ANSIBLE_EXTRA_ARGS="-t unattended-upgrades" make prepare`
  See `docs/troubleshooting/nvidia-driver-version-mismatch.md`
- Zalando Postgres operator rejects hyphenated database names in the `databases` field — create
  them manually with `psql`. See `docs/troubleshooting/radarr-sqlite-to-postgres.md`
- tc-limiter hostPath mounts need `mountPropagation: HostToContainer` — otherwise Cilium socket
  goes stale after restart and rate limiting silently stops working.
  See `docs/troubleshooting/bandwidth-limiting.md`

## Subsystem Docs

- **Cilium networking:** See `docs/conventions/cilium.md` for BGP, TCX, bandwidth limiting details
- **Documenting learnings:** See `docs/conventions/documenting-learnings.md` for when/how to write
  troubleshooting docs

## Licensing

GPLv3 (see LICENSE.md). Generated code must be compatible.
