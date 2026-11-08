# MCP Gateway Implementation Plan

**Status:** Planning (NOT STARTED)
**Date:** 2026-11-08
**Architecture:** [docs/design/mcp-gateway-architecture.md](../docs/design/mcp-gateway-architecture.md)
**Owner approval required before any live deployment**

## Overview

Phased implementation of centralized MCP gateway using upstream agentgateway (Rust standalone). Two deployments: `apps/mcp-gateway` (public) and `apps/mcp-gateway-admin` (VPN-only). Native OAuth resource-server auth, NetworkPolicy isolation. All checkboxes NOT STARTED except architecture.

## Task 0: Architecture & Inventory

**Architecture:** ✓ agreed | **Inventory:** NOT STARTED

- [ ] **Kanidm inventory**: List existing group memberships (`grafana-mcp-users`, `hermes-users`, etc.), verify per-target suitability. Check OAuth2 clients: dashboard UI vs workload creds. **Critical**: existing Hermes dashboard OAuth clients ≠ workload credentials automatically. Output: reuse decisions, no live DB inspection. **Manual membership gate**: Person accounts must be added manually to groups (see `docs/user-guide/kanidm.md`); never encode person membership in CRDs or reconcile through Git
- [ ] **agentgateway version selection**: Pin tested version (standalone binary or chart), validate transport support (SSE, HTTP). If chart: `helm template` deterministic render. Document: version, transport matrix, deployment mode

**Dependencies:** None | **Acceptance:** Kanidm reuse decisions documented, agentgateway version pinned

## Task 1: Foundation (Gateway + Auth + NetworkPolicy)

NOT STARTED

### 1.1 Chart Structure

- [ ] **`apps/mcp-gateway/`**: Chart.yaml with app-template dependency (per `docs/conventions/helm.md`) or pinned upstream chart (decided in Task 0; upstream standalone preferred). values.yaml for configuration. Templates for extra resources only: Ingress, NetworkPolicy, OAuth client config, ExternalSecret, ServiceMonitor. app-template or upstream chart generates Deployment/Service. Run `helm dependency build`, `helm lint`, `helm template` (existing convention, not apply)
- [ ] **`apps/mcp-gateway-admin/`**: Same pattern, internal ingress only. **Guard**: committing/pushing triggers Argo deployment; stage safely before live approval (worktree or draft PR)

### 1.2 Kanidm OAuth2 Client

- [ ] **Register OAuth2 client**: auth-code + PKCE for human operators. Issuer derived from discovered/configured Kanidm OAuth metadata. Audience = configured gateway resource audience (verified during spike; use selected registered OAuth client identifiers, not assumed universal binding). No ID token at edge. **Spike gates**: token refresh for long-lived clients, group claims mechanism (OAuth scopes vs lookup)

### 1.3 NetworkPolicy

- [ ] **Backend ingress**: allow only from gateway namespace AND pods (label-based). No NodePort/LB/hostPort. Ops exceptions on separate PORT (L3/4), not `/metrics` path. Additive policies; verify denied for ordinary pods

### 1.4 Auth Validation + Discovery Tests

- [ ] **Access token validation**: configured issuer/audience/expiry, group/scope extraction. No cookie redirect
- [ ] **OpenCode auth tests**: MCP OAuth discovery returns valid metadata, 401 on missing/invalid token, pre-registered client supports callback + refresh
- [ ] **Hermes noninteractive identity test**: service account token issuance verified (current SA exchange precedent is not proof of general flow)
- [ ] **Negative tests** (committed GitOps test job or handoff): wrong audience → 401, expired → 401, no group → discovery filtered or denied, wrong target → tool call denied. Invalid JWT → 401

### 1.5 Initial Backend Routes DISABLED

- [ ] **Deploy gateway with backend routes disabled**: independent review of auth + NetworkPolicy first. No accidental enabled tools before validation passes

### 1.6 Secrets + Observability

- [ ] **Secrets**: Kaniop-managed K8s Secrets for Kanidm tokens; Vault/ExternalSecrets for others. Don't force Kanidm tokens into Vault
- [ ] **Audit**: redacted (caller, backend principal, target, tool, outcome), no raw payload logging, existing Loki pipeline. Use supported metrics from pinned version; select during spike

### 1.7 Single Replica

- [ ] **Deploy single replica**, test session behaviour

**Dependencies:** Task 0 | **Acceptance:** `helm template` deterministic, NetworkPolicy blocks ordinary pods, auth tests pass (OpenCode discovery, Hermes noninteractive, negative tests), no direct backend exposure, audit redacted, backend routes disabled until validation. **Owner approval gate** before push to main

## Task 2: Grafana MCP (Standalone + Migration)

NOT STARTED

- [ ] **Dedicated pod in gateway namespace**: separate pod, gateway connects via ClusterIP/pod network; no localhost between pods. Remove old co-located Hermes sidecar after parity
- [ ] **Backend credential handling**: current SA API token → exchange → `id_token` is historical proven flow (service accounts), not proof human delegation. **Decision gate**: if delegation unsupported, owner chooses documented service-credential alternative vs delay. No new broker automatic
- [ ] **Sidecar migration**: keep first Hermes sidecar until parity confirmed (hermes-2/3/4 have no sidecars). After parity: remove sidecar (`apps/hermes/values.yaml` edit). **Rollback**: restore sidecar if standalone fails (temporary, explicit controlled)
- [ ] **Pipeline checks**: human (OAuth → PKCE → token → gateway → Grafana) and agent (SA token → gateway → Grafana). Protected Grafana APIs still respect permissions (coarse full enabled interface)

**Dependencies:** Task 1 | **Acceptance:** Grafana MCP accessible via gateway from both clients, sidecar removed after parity, backend credential pattern documented. **Owner approval gate**: deploy candidate first, then test live parity, then remove sidecar

## Task 3: Firecrawl MCP

NOT STARTED

- [ ] **Deploy `firecrawl-mcp` package**: pin version, validate transport (stdio/remote/hosted). No custom wrapper
- [ ] **Group permissions**: all enabled tools allowed (coarse), shared backend credentials (Firecrawl currently unauthenticated). Gateway audit = identity boundary
- [ ] **Internal API dependency**: Firecrawl REST API (`https://firecrawl.internal.grigri.cloud`, existing nginx-internal) ≠ Firecrawl MCP (separate deployment). NetworkPolicy: MCP listener ingress from gateway only
- [ ] **SSRF bounds**: validate destination restrictions at actual fetchers, including redirects and DNS resolution, and bound duration/concurrency. Gateway-only validation is insufficient. Before public onboarding either verify controls or record explicit owner acceptance of remaining homelab risk
- [ ] **NetworkPolicy**: MCP API only accessible via gateway, no affect native Firecrawl REST API users

**Dependencies:** Task 1 | **Acceptance:** Firecrawl MCP accessible via gateway, group permissions enforced, SSRF bounds documented, NetworkPolicy blocks direct access. **Owner approval gate** before public access

## Task 4: Vault MCP (Private, Phased)

NOT STARTED

- [ ] **Spike Vault official MCP**: runtime creds least permission, no secret disclosure by injection. **Spike gates**: static/session credential behavior, JWT login → native token (if supported, no false direct JWT API)
- [ ] **Owner scope decision**: official Vault MCP exposes secret read/write tools. Coarse full enabled interface. **Decision**: metadata/config-only desired? Can upstream disable secret tools? Don't introduce per-tool gateway model silently
- [ ] **Admin-only access**: group gate (reuse existing or create with approval), backend Vault ACL restricts secret data if desired. Trusted admin scope explicitly accepted. Separate creds, private reach (no public → admin)
- [ ] **Token lifecycle**: expiry/renew/revoke configured model (no permanent root token), concurrency isolation appropriate
- [ ] **Deploy in admin namespace**: VPN-only, internal ingress to admin gateway ONLY. No direct ingress to Vault MCP pod. NetworkPolicy: admin gateway → Vault MCP only

**Dependencies:** Task 1, Task 0 (admin group) | **Acceptance:** Vault MCP accessible via admin gateway only (no direct ingress), owner scope decision documented, token lifecycle configured, admin group enforced. **Owner approval gate** before deploying

## Task 5: Migration (OpenCode / Hermes)

NOT STARTED

- [ ] **OpenCode migration**: one instance at a time. Update user config MCP URLs to point to gateway. Test OAuth discovery → PKCE → access token → gateway → backend
- [ ] **Hermes migration**: one instance at a time, start with first (has sidecar). Update config: MCP URLs → gateway. PVC persistence (`docs/troubleshooting/hermes-config-overwrite-recovery.md`), rollback: switch to previous gateway or pause before exclusivity cutover
- [ ] **Future (TODO only)**: SearXNG/Context7 onboarding (after core MVP), HA, Playwright, Postgres

**Dependencies:** Tasks 2-3 (Vault validated in Task 4 parallel; no need to block regular migration on Vault, but overall MVP includes Vault) | **Acceptance:** OpenCode/Hermes migrated one at a time, rollback tested. **Owner approval gate** before each instance migration

## Tests

NOT STARTED

**Test matrix:**
- [ ] Wrong/no group → discovery filtered or denied (empty list acceptable if no access)
- [ ] Full MCP allowed (appropriate group)
- [ ] Direct tool call denied (ordinary pods, NetworkPolicy)
- [ ] Direct access from ordinary pods denied (Services + pod IP)
- [ ] Public gateway denied Vault MCP (admin only)
- [ ] Internal plane no auth bypass
- [ ] Restrictive policies not reopened by additive
- [ ] No unprivileged caller header/backend/credential injection
- [ ] Expiry/stream sessions no bypass
- [ ] Gateway outage no bypass
- [ ] Invalid JWT → 401

**Test method:** No ad-hoc `kubectl run/apply`. Committed GitOps test job if approved, or read-only diagnostic existing pods commands handoff. Document test method (needs approved deployment, not ad-hoc)

**Dependencies:** Tasks 1-4 | **Acceptance:** All tests pass, method documented and approved

## Rollback

NOT STARTED

- [ ] **Git revert**: normal approval GitOps, `git revert` + push + ArgoCD sync. Don't remove NetworkPolicy/reopen bypass
- [ ] **Sidecar restoration**: can restore pre-migration Grafana sidecar only if explicitly controlled temporary migration (not pretend satisfies final invariant). Preferable: switch clients to previous working gateway OR pause migration BEFORE exclusivity cutover
- [ ] **Manual recovery**: native direct APIs separate authorized recovery path, not MCP bypass

**Rollback triggers:** auth validation fails, NetworkPolicy blocks legitimate traffic, backend credential delegation breaks, session incompatible, owner decision

**Dependencies:** None (always available) | **Acceptance:** Rollback path tested (dry run), owner knows triggers, no data loss

## Owner Approval Gates

**All live deployments require explicit owner approval:**

1. Before pushing Task 1 (foundation) to main: review chart structure, NetworkPolicy, auth
2. Before deploying Grafana MCP candidate: review before live parity test
3. Before removing Hermes sidecar: confirm standalone works in parity test
4. Before enabling Firecrawl MCP public access: SSRF bounds, owner decision
5. Before deploying Vault MCP: scope decision (metadata-only vs full), admin group
6. Before migrating each OpenCode/Hermes instance: test rollback
7. Before running live tests: approve method, no ad-hoc

**Deployment safety**: Prepare changes on non-watched branch; owner approves each push and live rollout. Merge/push to Argo-watched revision can trigger automated deployment; do not rely on manual sync as gate.

## File Locations

**Proposed layout** (not a decision; actual Helm template depends on Task 0 chart choice):

```
apps/mcp-gateway/
├── Chart.yaml, values.yaml
└── templates/
    ├── ingress-external.yaml
    ├── networkpolicy.yaml, networkpolicy-backend-allow.yaml
    ├── servicemonitor.yaml
    ├── grafana-mcp-deployment.yaml, grafana-mcp-service.yaml
    ├── firecrawl-mcp-deployment.yaml, firecrawl-mcp-configmap.yaml
    └── externalsecret-*.yaml

apps/mcp-gateway-admin/
├── Chart.yaml, values.yaml
└── templates/
    ├── ingress-internal.yaml
    ├── networkpolicy.yaml
    └── vault-mcp-deployment.yaml, vault-mcp-configmap.yaml
```

Note: Deployment/Service resources generated by app-template or upstream chart; templates/ contains only extra resources (OAuth, secrets, policy, service monitors, backend pods).

**Existing references:**
- Conventions: `docs/conventions/deploying-new-apps.md`, `docs/conventions/helm.md`
- Hermes sidecar: `apps/hermes/values.yaml`
- Kaniop pattern: `apps/hermes/templates/kanidm-service-account.yaml`
- ServiceMonitor: `system/tempo/resources/servicemonitor.yaml`
- Config overwrite: `docs/troubleshooting/hermes-config-overwrite-recovery.md`

## Success Criteria

**MVP complete when:**
- [ ] agentgateway deployed (public + admin)
- [ ] Grafana MCP standalone (sidecar removed)
- [ ] Firecrawl MCP deployed
- [ ] Vault MCP deployed (admin-only, scope decided)
- [ ] OpenCode/Hermes migrated (≥1 instance each)
- [ ] All tests pass, rollback tested
- [ ] Owner approval recorded for each gate

**Out of scope (future):** HA, Playwright, Postgres, per-tool auth, enhanced revocation, quotas, session management, multiple replicas
