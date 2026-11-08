# MCP Gateway Architecture

**Status:** Agreed baseline (implementation validation pending). First live attempt of the
disabled-only foundation was deployed and reverted on 2026-11-08: the NetworkPolicy set denies the
node path, so kubelet probes never passed - see
`docs/troubleshooting/mcp-firecrawl-egress-regression.md` ("Second occurrence").
**Date:** 2026-11-08
**Implementation plan:** [`planning/mcp-gateway-implementation.md`](https://github.com/pando85/homelab/blob/master/planning/mcp-gateway-implementation.md)

## Overview

Centralized MCP gateway giving AI agents and human operators a single entry point to homelab MCP
servers (Grafana, Firecrawl, SearXNG, Vault, etc.). Two separate deployments: a public-facing
gateway and a VPN-only admin interface. The gateway deploys the upstream
[agentgateway](https://agentgateway.dev/) standalone binary; chart packaging is a spike item.

MVP consumers: OpenCode human operators and Hermes agent workloads. Home Assistant integration is
future work.

## Goals & Scope

### In Scope (MVP)

- **Upstream agentgateway**: Deploy existing Rust standalone binary; chart packaging is a spike
- **Two deployments**: Gateway (`apps/mcp-gateway` or TBD); admin interface in separate namespace.
  Admin does NOT expose Vault admin backend to public plane
- **Native MCP OAuth**: OpenCode → MCP OAuth discovery → Kanidm auth-code + PKCE → access token.
  Gateway validates (strict issuer + audience). No oauth2-proxy. Exact flow is a spike item
- **Existing Kanidm groups**: Reuse verified groups only after confirming memberships/suitability.
  Do NOT assume `hermes-users` implies workload permission or `vault-admins` exists.
  Create a new group only if no existing group fits, with approval.
  **Group membership**: Person accounts must be added manually (see `docs/user-guide/kanidm.md`); never encode person membership in CRDs or reconcile through Git. Service-account-only groups may use CRD membership
- **Per-MCP access**: Any configured existing group grants that MCP, not every MCP.
  Whole MCP = all tools allowed
- **Network isolation**: Default-deny NetworkPolicy; gateway-only ingress. No NodePort/LB/hostPort
- **Auth delegation** where supported; otherwise per-MCP fixed service credential. No silent
  fallback; if blocked, decide per-MCP, do not impose broker
- **Credentials**: Kaniop-managed K8s Secrets for Kanidm tokens; Vault/ExternalSecrets for others.
  Do not force all Kanidm API tokens into Vault
- **Grafana MCP standalone**: Move from Hermes sidecar to dedicated pod in gateway ns.
  Gateway connects via ClusterIP/pod network. Remove sidecar after parity.
  Only first Hermes has the sidecar now
- **Baseline audit**: Redacted — caller, backend principal, target, tool, outcome. No raw payload
  logging. Metrics inventory confirmed during spike

### Out of Scope

- **Home Assistant integration**: Future; HA MCP client compatibility not yet spiked
- **Per-tool authorization**: Whole MCP access or nothing
- **Generic credential broker**: No dynamic per-user credential generation in MVP
- **Platform hardening**: Privileged node/RBAC limitation brief only, not a comprehensive project
- **Session engineering**: Single replica allowed initially; session behaviour tested during spike.
  No "stateless / no initialize / pooling" claims without verification
- **Playwright remote browser**: Distinct workload, later phase

## Architecture Topology

### Two Deployments

```
Public plane                              Admin plane (VPN-only)
─────────────────                       ────────────────────────
Internet/VPN
  │
  ├─ OpenCode (human)
  └─ Hermes (agent)
          │
          ▼
  nginx-external
  mcp.grigri.cloud
          │
          ▼
  ┌─────────────────────────┐            ┌─────────────────────────┐
  │ ns: mcp-gateway         │            │ ns: mcp-gateway-admin   │
  │                         │            │                         │
  │ agentgateway (upstream) │            │ agentgateway (upstream) │
  │ - access token validate │            │ - access token validate │
  │ - group/scope check     │            │ - admin group check     │
  │ - MCP routing + audit   │            │ - MCP routing + audit   │
  └───────────┬─────────────┘            └───────────┬─────────────┘
              │ ClusterIP (NetworkPolicy)             │ ClusterIP (NetworkPolicy)
    ┌─────────┼──────────┐                            │
    ▼         ▼          ▼                            ▼
┌────────┐ ┌─────────┐ ┌──────────┐           ┌──────────────┐
│Grafana │ │Firecrawl│ │SearXNG/  │           │ Vault MCP    │
│MCP pod │ │MCP pod  │ │Context7  │           │ pod          │
│        │ │         │ │(next)    │           │              │
└────────┘ └─────────┘ └──────────┘           └──────────────┘

No public-plane route to Vault MCP.
Admin gateway does not grant Hermes automatic admin.
```

### Traffic Flow

**Human operator (OpenCode desktop):**
1. OpenCode MCP client → MCP OAuth discovery → Kanidm auth-code + PKCE → access token
2. `Authorization: Bearer <access_token>` to `https://mcp.grigri.cloud/mcp`
3. Gateway validates access token (strict issuer, strict audience, expiry)
4. Gateway checks group membership / per-target assignment → routes to backend MCP
5. Backend MCP executes tool call, returns result
6. Gateway logs redacted audit event (caller, backend, tool, outcome)

**Hermes agent (cluster-side):**
1. Hermes pod → cluster-internal Service DNS → gateway
2. Gateway validates Kanidm service account access token
3. Gateway checks group / target → routes to backend MCP
4. Backend authenticates via delegated credentials or fixed service credential
5. Audit event logged

## Authentication & Authorization

### Identity Model

- **Human operators**: Kanidm OAuth2 auth-code + PKCE → access token. Issuer derived from
  discovered/configured Kanidm OAuth metadata (exact match, no wildcard). Audience = configured
  MCP resource audience (verified during spike; must not accept arbitrary client audience).
  No ID token at the edge
- **Hermes agents**: Kanidm service account → access token. Same strict validation.
  Grant issuance pending verification
- **Group membership**: Via Kanidm OAuth scopes or supported lookup. Do NOT assume access token
  carries a `groups` claim; verify during spike
- **Caller identification**: Stable `sub` claim plus client identity. Username optional;
  do not assume `preferred_username` in access token

### Token Validation

- Valid issuer: exact match to discovered/configured issuer, no wildcard
- Valid audience: exact match to configured MCP resource audience (verified during spike;
  must not accept arbitrary client audience)
- Not expired: `exp` claim
- Group/scope extraction: Kanidm OAuth scopes or supported lookup (spike to confirm mechanism)

### Backend Credential Patterns

**Grafana MCP** (historical proven flow, `apps/hermes/values.yaml:23-91`). Delegation status after
live review: Grafana `13.3.0` trusts **one** JWT client - per-client JWKS with exact `iss` and `aud`. A
merged `jwk_set_file` covering both Kanidm clients is **retracted as unsafe** (pinning `iss` rejects the
second client, an `iss` array fails startup, and merging without `iss` pinning trusts any issuer holding
a listed key), so caller-preserving access-token delegation to the live instance is **blocked** until
compatible dual-client trust exists upstream, and `allow_insecure_email_lookup = true` (live value,
unchanged) must be settled before any delegation route is enabled:
- Current: SA API token → exchange → Grafana `id_token`. This is the proven service-account flow,
  not a target for human delegation or proof of `client_credentials` exchange
- Gateway presents backend bearer/header credential; isolation gate between caller and backend
- Caller-preserving delegation on this plane: the edge bearer stays the Kanidm **access** token and
  is forwarded verbatim. Grafana identifies the person from a stable claim (`sub`), not from a
  username it has never seen, and a person who already exists as an OAuth user is linked once by a
  bootstrap token that carries both `sub` and the identity claims. Live Grafana settings, per-person
  bootstraps and the two-person parity run remain owner gates; the local route stays disabled.
  Details, measurements and the alternatives that were costed and rejected:
  `planning/mcp-gateway-implementation.md` (Task 2)

**Firecrawl MCP** (hosted existing MCP package):
- Backend currently lacks caller auth — gateway audit is the identity boundary
- No wrapper invented; deploy the existing `firecrawl-mcp` package
- Backend may observe MCP network source, not authenticated caller identity

**SearXNG / Context7**: Candidate centralized MCP packages. Context7 is NOT excluded because
  upstream is external — evaluate during spike

**Vault MCP** (planned, private plane): Official MCP exists. Measured with the official product
behind the gateway: stateful mode caches the backend credential per MCP session, so the session id
alone behaves like a bearer and rotation is ignored — stateful cannot carry per-caller secrets.
Per-request (stateless) forwarding of the caller's own native token does preserve Vault's own ACLs
and audit identity, at the cost of a deliberate per-route header allow plus a stateless route, which
is an owner decision. That support rests on **local fixture evidence only** (21 REQUIRED + 4 CONTROLS +
11 OBSERVED + 1 NOT TESTED, security negatives incomplete); **TLS is not established** - the valid-CA
positive and the invalid-CA controlled negative are both pending, and the only measured TLS fact is that
native Vault denies an invalid certificate. `planning/mcp-gateway-implementation.md` (Task 4)

**Unsupported delegation**: If a backend does not support delegation or the exchange chain is
  blocked, do NOT silently weaken identity or impose a new broker. Decide per-MCP using the shared
  accepted pattern and record the classification

### Known Gates (Spike Required)

1. **OpenCode MCP OAuth discovery + PKCE flow**: Exact Kanidm client config, token refresh for
   long-lived desktop clients
2. **Group membership mechanism**: OAuth scopes vs supported lookup — verify which Kanidm supports
3. **Existing group suitability**: Confirm `grafana-mcp-users`, `hermes-users` memberships and
   whether they fit the per-target model. No assumptions
4. **Kanidm user-token exchange**: Not verified for human delegation. If blocked, record per-MCP
   decision: supported delegated alternative, explicitly approved shared service credential,
   or defer target

## MCP Server Inventory

| Server | Phase | Current State | Target |
|--------|-------|---------------|--------|
| Grafana MCP | MVP | Sidecar in first Hermes pod | Dedicated pod in gateway ns |
| Firecrawl | MVP | REST API, network-isolated | Deploy `firecrawl-mcp` package |
| Vault | MVP (private, phased) | No MCP yet | Planned, needs spike |
| SearXNG | Next | REST API, internal | Deploy MCP package |
| Context7 | Next | External (upstream) | Evaluate centralized deployment |
| HA, Postgres, Playwright | Future | N/A | Separate phases |

## Network & Trust Boundaries

### NetworkPolicy

- **All MCP listener ingress**: Allowed only from gateway namespace AND gateway pods (label-based)
- **No direct backend exposure**: No NodePort, LoadBalancer, or hostPort for MCP backends
- **Ops exceptions on a separate PORT**: Metrics/probe on a different port, NOT a `/metrics` path
  distinction (L3/4 enforcement). Narrow probe/node limitations via labels/RBAC. Additive
  policies; verify denied for ordinary pods
- **ClusterIP is not isolation**: NetworkPolicy is the isolation mechanism

### SSRF

- **Firecrawl**: Validate destination restrictions at actual fetchers, including redirects and
  DNS resolution, and bound duration/concurrency. Gateway-only validation is insufficient.
  Before public onboarding either verify controls or record explicit owner acceptance of
  remaining homelab risk

- **Status (as of this writing): attempted, rolled back, blocker open.** Fetcher egress isolation was
  committed and pushed (`0b16b31b9`), auto-deployed by ArgoCD, and within ~12 s of attach the Firecrawl
  `api`, `worker` and both `nuq` pods began timing out kubelet health probes across two nodes (selected
  endpoints' BPF reloaded 1 s after the apply) while `browser` stayed `Ready`. A scoped revert
  (`8fff4f224`) synced at 19:08:11 and the **same** `api` process regained readiness **without a
  restart**; the baseline stayed stable for 30 min with the new policies absent. **Root cause is not
  established**: denied-packet counters read `0` but are cumulative and unattributable to a
  namespace/policy/port (not exoneration); no packet/eBPF/exec capture was taken, so "probe requests
  timed out" is not a confirmed node↔pod network denial; the live SSRF verification never ran, so no
  destination was ever demonstrated blocked; and the browser policy shares the byte-identical DNS and
  internet anchors with the fetchers, so rule shape is not a discriminator — only extra peers and rule
  count on the selected side. Full record, including retracted intermediate hypotheses and the capture
  gates required before any re-attempt:
  `docs/troubleshooting/mcp-firecrawl-egress-regression.md`.

  Consequence for this design: the gateway's `firecrawl` backend route stays **disabled**, and
  gateway-side NetworkPolicy (which only restricts who may reach the MCP pod directly) is not a
  substitute for fetcher egress control. Firecrawl exposure therefore remains blocked on two
  independent gates: this SSRF gate, and the group-membership decision that grants the
  service-only `mcp-gateway-agents` workload access.

### Ingress

- Gateway: `nginx-external` for public plane, `nginx-internal` for admin plane
- MCP backends: No ingress resources. All traffic through gateway

## Baseline Security Controls

### Credential Management

- **Kanidm identity tokens**: Existing Kaniop-managed Kubernetes Secrets
- **Other backend secrets**: Vault + ExternalSecrets
- **No custom credential broker**: Delegated caller credentials where supported; otherwise fixed
  per-MCP credentials. Dynamic credential issuance is future
- **No fail-open**: Expiry checked, no bypass on outage. No automatic static credential fallback

### Audit & Observability

- **Redacted audit**: Caller, backend principal, target, tool, outcome. No raw payload logging
- **Metrics**: Use supported metrics from pinned version; select during spike
- **Log destination**: Existing Loki pipeline; structured JSON logs

### Accepted Limitations

- **Whole MCP access includes write tools**: Group access = all tools, including writes
- **Shared backend permissions for non-delegated backends**: Callers in the same group share
  backend credentials where delegation is not supported. Delegated backends preserve caller
  permissions
- **Bounded revocation TTL**: Actual TTL measured and defined during spike. Removing a user
  takes effect at next token expiry check — no immediate revocation in MVP
- **No bypass on outage**: Gateway or Kanidm unavailable → access fails. No fallback
- **Session compatibility**: Verified during spike, not assumed
- **No platform hardening**: Focused on MCP gateway, not platform-wide security

## Future Improvements (Nonblocking)

- Per-tool authorization (if real need emerges)
- Enhanced revocation (short-lived JWTs, frequent refresh)
- Quotas & rate limiting
- Session management for long-running operations
- High availability (multiple replicas after session behaviour verified)
- Approval workflows for sensitive write operations
- Caller-bound dynamic credentials for backends that support it
- PostgreSQL MCP (see below)
- Playwright remote browser

## PostgreSQL MCP (Future)

Same access model as other MCPs. Choose during onboarding: rotated fixed DB login or Vault
dynamic credentials. Dynamic credentials do not automatically provide per-user attribution.
Scoped non-superuser roles, limits, expiry. Existing session caveat applies. Fine-grained
authorization is optional, not a requirement.

**References**:
- [Vault PostgreSQL database secrets engine](https://developer.hashicorp.com/vault/docs/secrets/databases/postgresql)
- [PostgreSQL RLS](https://www.postgresql.org/docs/current/ddl-rowsecurity.html)

## Related Documentation

- [Deploying new applications](../conventions/deploying-new-apps.md)
- [Cilium networking](../conventions/cilium.md)
- [Grafana MCP deployment with Kanidm authentication](../troubleshooting/grafana-mcp-hermes.md)
- [Hermes config overwrite recovery](../troubleshooting/hermes-config-overwrite-recovery.md)
- [Hermes agent deployment](../deployment/hermes-agent.md)
- [Conversation capabilities roadmap](../voice-assistant/conversation-capabilities-roadmap.md)
- [MCP gateway implementation plan](https://github.com/pando85/homelab/blob/master/planning/mcp-gateway-implementation.md)
