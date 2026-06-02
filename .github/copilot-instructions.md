# Homelab Copilot Instructions

Use `AGENTS.md` as the source of truth for repository context, deployment restrictions, security,
commit style, and documentation conventions. This file only adds Copilot-specific behavior and avoids
duplicating always-loaded agent guidance.

## Required Behavior

- Make surgical edits and avoid full-file rewrites unless the user asks.
- Reuse existing Helm, YAML, Renovate, and ExternalSecret patterns.
- Keep generated guidance concise; link to docs instead of restating runbooks.
- Suggest validation commands such as `helm template`, `helm lint`, `make docs`, or
  `pre-commit run --all-files` when relevant.
- Never automatically deploy or mutate the cluster. Do not run `make bootstrap`, `make metal`,
  `make dev`, `kubectl apply`, `helm install/upgrade`, `ansible-playbook`, or equivalent commands.

## When Unsure

Default to the smallest reversible change, state assumptions, and point to the relevant file under
`docs/` or `AGENTS.md`.
