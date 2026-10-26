# AGENTS.md

Concise repository guidance for agentic coding agents. Keep this file short: add detailed runbooks
under `docs/`, and only keep high-signal reminders here.

## Repository Overview

Self-hosted K3s homelab via GitOps. Principles: declarative infra, repeatable automation, small blast radius, version pinning, minimal drift.

**Tech Stack:** K3s, ArgoCD, Helm (template-only), Ansible (bootstrap), External Secrets + Vault, cert-manager, ingress-nginx, Renovate, ZFS, Cilium (BGP).

**Cluster Access:** This is a GitOps repository. `kubectl --context=grigri`. Metrics: https://prometheus.internal.grigri.cloud

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
make git-hooks
pre-commit run --all-files
helm dependency build apps/<name>/
helm template --include-crds --namespace <namespace> <release-name> apps/<name>/
helm lint apps/<name>/
```

## Workflows

**Deploying a new application:**
1. Read `docs/conventions/deploying-new-apps.md` — decision trees, patterns, checklist, reference paths (OIDC, Zalando, MinIO)
2. Check existing deployments: `apps/immich/`, `apps/readest/`
3. Run validation commands before committing

## Code Style

- **YAML:** 2-space indent, `---` for multi-doc, yamllint via pre-commit (ignores `templates/`)
- **Helm:** `app-template` from bjw-s. See `docs/conventions/helm.md`
- **Ansible:** `safety` profile. See `docs/conventions/ansible.md`
- **Renovate:** Add hints above image refs — `docs/conventions/helm.md#renovate-integration`
- **Docs:** Troubleshooting in `docs/troubleshooting/`; `AGENTS.md` for links/reminders. See `docs/conventions/documenting-learnings.md`
- **Security:** No hardcoded secrets. ExternalSecret → Vault. Public repo hygiene

## Commit Messages

**Format:** `<scope>: <imperative-description>` — scope is directory or service name. Special scopes: `metal` (Ansible), `docs`, `renovate`.

```
kube-system: Add kata-nvidia coordinator operator
vault: Change to svg logo
monitoring: Update Helm release kube-prometheus-stack to v82.10.1
```

## Deployment Rules

**Commit first, then apply.** Git must contain the change before it is applied anywhere. Never apply
an uncommitted or partially edited tree.

### Agents may run

- Read-only inspection: `kubectl get/describe/logs/top`, Prometheus/Grafana/Loki queries,
  `helm template`, `helm lint`, `helm dependency build`, `pre-commit run`
- **Narrowly scoped `metal/` Ansible** for config that is already committed, using explicit
  `--tags` and `--limit`. Example: `cd metal && ANSIBLE_EXTRA_ARGS="-t zfs-config --limit prusik" make prepare`
- **Scoped `k3s` role config apply** for an already-committed, **config-only** change:
  `cd metal && ANSIBLE_EXTRA_ARGS="-t k3s --limit <node>" make cluster`. Re-templates
  `/etc/rancher/k3s/config.yaml` and `k3s.service`, restarts k3s if config changed (idempotent —
  binary install gated on `/usr/local/bin/k3s` absent). Causes ~30–75s API downtime on `prusik`.
  Requires: committed, config-only (no `k3s_version` bump), explicit `--limit`
- ArgoCD sync of an already-committed and pushed change

Report the command and outcome. Prefer narrowest tag/limit. Target by playbook: `prepare.yml`
roles (`prepare`, `setup` incl. `zfs-config`, `swap`, `nvidia-container-runtime`) → `make prepare`;
`cluster.yml` roles (`ntp`, `gvisor`, `k3s`) → `make cluster`. Always `-t <tag> --limit <node>`.

### Never run

- **`kubectl apply/delete/edit/patch`** on cluster resources — ArgoCD `selfHeal: true` with real-time
  watches reverts these almost instantly
- **Node lifecycle / high blast radius:** `make bootstrap`, `make dev`, `make uninstall-k3s`,
  unscoped/all-node `make cluster`, `-t k3s` without `--limit`, k3s version bumps via Ansible
  (changing `k3s_version` or re-downloading binary). These can take down the sole control-plane
  node. Exception: scoped, committed, config-only `-t k3s --limit <node>` — see "Agents may run"
- **Anything data-destroying:** PVC/PV deletion, `zpool destroy`, `zfs destroy`, pool re-creation,
  disk formatting (`prepare_additional_disks_force_format`)
- Untagged or unlimited `make prepare` / `ansible-playbook` runs — always scope them
- `helm install/upgrade` — let ArgoCD do it

When in doubt, or when a change needs a hard prohibition above, stop and hand the user the exact
command.

## Skill Usage

- `debug` skill: live cluster investigation with read-only `kubectl` and Grafana observability only
- Do not copy runbook steps into agent instructions — link to docs instead
- Prefer focused, path-specific docs over broad always-loaded guidance

## Common Pitfalls

Full pitfall index (65 entries grouped by subsystem): `docs/troubleshooting/INDEX.md`

**Pre-deployment rules** (no separate doc — must be known before starting work):

- Forgetting `helm dependency build` after updating `Chart.yaml`
- Missing Renovate hints causes images to not auto-update
- Introducing CRDs without `--include-crds` in helm template
- Not waiting for webhooks (cert-manager, external-secrets) before applying dependent resources
- Forgetting `Prune=false` on PVCs causes data loss on sync
- Freshly joined node shows `Ready,SchedulingDisabled` for ~1 min: `k3s-agent` system-upgrade Plan
  has `cordon: true`. Don't uncordon by hand — resolves automatically
- Apps with init-container workarounds: log version info, fail loudly (`exit 1`) on mismatch so
  Renovate bumps surface breakage. Check init container logs after version bumps
- ArgoCD `selfHeal: true` reverts `kubectl apply` almost instantly — commit/push first, let ArgoCD
  sync. See `docs/troubleshooting/argocd-gitops-workflow.md`

**Subsystem index** (top pitfalls per group — see `docs/troubleshooting/INDEX.md` for full entries):

- **Kubernetes & GitOps** (14): node addition, app-template `tpl` evaluation, topology spread constraints
- **ArgoCD** (4): repo-server probe death-spiral, kustomize OCI auth override
- **Monitoring** (8): Grafana sidecar collisions, Vector log loss, Tempo WAL path collision
- **Networking** (6): Cilium stale BPF datapath, Armbian kernel BPF breakage, nodelocaldns LRP
- **Storage & Backup** (10): prusik RAM-bound latency, ZFS orphan leak, Velero backup chains
- **Ansible & Infra** (2): cross-host facts in scoped runs, Ubuntu 26.04 compat
- **LLM Infrastructure** (3): Hermes config overwrite, auth failures, persistent CLI tools
- **Desktop & Home** (8): Home Assistant venv/aiohttp/localtuya, PVPC negative prices, Shelly CoIoT push, never disable CoIoT on a Gen1 Shelly
- **Applications** (10): Supabase/Zalando bootstrap, GoTrue CORS, Firecrawl nuq schema

## Subsystem Docs

- **Cilium:** `docs/conventions/cilium.md` — BGP, TCX, bandwidth limiting
- **Deploying new apps:** `docs/conventions/deploying-new-apps.md` — decision-making, patterns, checklist
- **Documenting learnings:** `docs/conventions/documenting-learnings.md` — when/how to write troubleshooting docs
- **prusik storage:** `docs/conventions/prusik-fast-storage-tier.md` — ZFS pool sizing, SSD selection, PVC migration. Hardware: `docs/hardware/prusik.md`, `docs/hardware/k8s-amd64-1.md`
- **Adding a node:** `docs/user-guide/add-or-remove-nodes.md` (runbook), `planning/k8s-amd64-1-node-addition.md` (worked example)
- **Topology spread:** `docs/conventions/topology-spread-constraints.md` — `matchLabelKeys`
- **Battery optimizer:** app code in `pando85/battery-optimizer`; deployment + extraction plan `planning/energy-baseline-production-execution.md`

## Licensing

GPLv3 (see LICENSE.md). Generated code must be compatible.
