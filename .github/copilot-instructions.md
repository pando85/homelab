# Homelab Repository – Copilot Instructions

These instructions give Copilot enough context to propose accurate changes without re-discovering
project structure each time. Keep answers concise, infra-safe, and follow established patterns.

---

## Purpose & Overview

Self‑hosted Kubernetes (K3s) homelab managed via GitOps. Principles: declarative infra, repeatable
automation, small blast radius, explicit version pinning, minimal drift. Key tech: K3s, ArgoCD, Helm
(template-only), Ansible (bootstrap), External Secrets + Vault, cert-manager, ingress-nginx,
Renovate, ZFS.

## Layout

- `apps/` – Application Helm charts (each subfolder = one app release). Some vendor charts vendored
  under `charts/` with a `Chart.lock` if dependencies are pinned. Values & templates live here.
- `system/` – Cluster-wide foundational components (ingress, cert-manager, monitoring, identity,
  etc.).
- `platform/` – Supporting operators/platform services (Vault, external-secrets, git, reloader,
  backup operators, etc.).
- `bootstrap/` – ArgoCD bootstrap layer (installs ArgoCD pointing back to this repo).
- `metal/` – Ansible playbooks, inventory, and roles to provision the K3s nodes.
- `scripts/` – Utility scripts. Notably `deploy-dir.rh` (Rash script) renders & applies a Helm chart
  directory.
- `test/` – Local ephemeral test cluster helpers (k3d config + stepwise provisioning script).
- `docs/` – MkDocs site (Material theme) for extended documentation.
- Root files: `Makefile`, `README.md`, `commitlint.config.js`, `.pre-commit-config.yaml`,
  `mkdocs.yml`.

## Workflows

Always run pre-commit before committing.

Makefile targets:

- `make metal` – Run Ansible (from `metal/`) to (re)configure K3s hosts.
- `make bootstrap` – Deploy ArgoCD layer (bootstraps the GitOps controller).
- `make dev` – Execute `test/step_by_step_tests.sh` → spins up local k3d cluster, applies core
  components in controlled order.
- `make docs` – Live preview docs via Docker + mkdocs-material.
- `make git-hooks` – Install pre-commit + commit-msg hook.

## Conventions

- YAML must be clean & lintable: `yamllint` runs via pre-commit.
- Commit messages validated by commitlint (Conventional style; subject sentence/lower case; blank
  line before body). Avoid scope noise unless meaningful.
- Prefer explicit resource requests/limits.
- Keep images pinned (exact tags). Add/maintain Renovate hints above image lines when updating
  patterns.
- Avoid introducing imperative kubectl commands into charts—keep them declarative.
- Follow existing label/annotation patterns if adding new apps.

## Renovate & Pinning

- Renovate manages image/chart bumps—respect existing `# renovate:` comment patterns.
- When adding a new container image line, reuse an existing regex form: e.g.
  `# renovate: datasource=docker depName=ghcr.io/OWNER/IMAGE` Next line:
  `image: ghcr.io/OWNER/IMAGE:TAG` or custom field (e.g. `dockerImage:` for Postgres operator).
- Do not invent new variant markers unless necessary.

## Adding a New App

Checklist (keep order):

- Create `apps/<name>/` with `Chart.yaml`, `values.yaml`, `templates/`.
- If using upstream dependency charts: add dependency block, run `helm dependency build`, commit
  `Chart.lock`.
- Namespace = folder name (auto-created by deploy script).
- Pin all images + add Renovate hints above them.
- Add Ingress (with cert-manager + external-dns annotations) if public.
- Define resource requests & limits.
- Add External Secret objects if secrets required (source: Vault path).

## Ansible (metal)

- Provisioning logic & inventory define nodes.
- Avoid mixing host-specific secrets into repo—fetch or inject via Vault/External Secrets.

## Documentation

- Add/extend service docs in `docs/` + link from index where relevant.
- Run `make docs` to preview.

## Common Pitfalls

- Forgetting to regenerate chart dependencies (`helm dependency build`) when adding/updating
  dependencies.
- Missing Renovate hints → images won't auto-bump.
- Introducing CRDs without `--include-crds` pattern (maintain consistency if adding new deploy
  logic).
- Not waiting for cert-manager/external-secrets webhooks—causes transient failures.

## Copilot Guidance

When generating changes:

- Modify only necessary blocks.
- Maintain alphabetical order when clearly intended (labels, annotations); avoid churn.
- Reuse existing patterns; avoid new abstractions unless clearly additive.
- Suggest validation (k3d spin-up, helm template) for changes impacting core/system charts.

## Security & Secrets

- Never hardcode secrets. Use External Secrets pointing to Vault.
- Assume public repo hygiene: no private keys, no tokens.

## Licensing

- Repository is GPLv3 (see `LICENSE.md`). Any generated code must be compatible.

## When Unsure

Default to existing conventions. If something is absent: propose minimal, reversible change; state
assumptions explicitly.

## LLM Optimization Notes

- Keep responses concise; prefer bullet lists for procedures.
- Do not re-enumerate repo layout each time—reference section headings instead.
- Assume Helm template workflow (no helm install/upgrade in-cluster).
- Never introduce secrets/plaintext credentials.
- Prefer incremental diffs (surgical edits) over full-file rewrites.

---

These instructions are repository-wide. Supplemental path-specific rules can be added under
`.github/instructions/` if specialization becomes necessary.
