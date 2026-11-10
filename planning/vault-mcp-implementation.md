# Vault MCP Backend Implementation Plan

**Status:** PROPOSAL. Nothing here is applied, committed or enabled. Drafted 2026-11-10 against
`master` at `fa96d899a` (per-route Kanidm human groups). All cluster statements below are read-only
measurements or file citations; all upstream statements are cited to a fetched URL or marked
UNVERIFIED.
**Gap being closed:** G3 in
[`mcp-gateway-gap-closure-implementation.md`](mcp-gateway-gap-closure-implementation.md) (deferred by
owner decision 2026-11-10), and Task 4 of
[`mcp-gateway-implementation.md`](mcp-gateway-implementation.md) (BLOCKED, route stays disabled).
**Architecture:** [`docs/design/mcp-gateway-architecture.md`](../docs/design/mcp-gateway-architecture.md)
(Vault backend section, the two recorded deviations, and the "Deferred" inventory row).
**Does not duplicate:** the Task 4 measurement matrix (item 7 of that plan) and the probe/policy
matrix in `docs/conventions/cilium.md`. This plan assumes both and cites them by section.

## 1. Summary and recommendations

| Question | Recommendation | Strongest reason |
|---|---|---|
| Which server | Official `hashicorp/vault-mcp-server`, image `hashicorp/vault-mcp-server:0.2.0` | It is the only first-party option, it is the one this repo already measured behind the pinned gateway, and it supports per-request `X-Vault-Token` (§3.3) |
| Identity model | **(a) caller-preserving**, per-request `X-Vault-Token` | Model (b) is already ruled out for Vault by the predecessor plan ("Not on the table: ... a shared admin credential", `planning/mcp-gateway-implementation.md:928-929`), and §5.4 shows why a shared Vault credential is categorically worse than the Firecrawl precedent |
| Phase 1 privilege | **Read-only**, on an owner-named allow-list of KV prefixes | 62 distinct ExternalSecret paths live under `secret/`, including third-party API credentials; write turns a tool call into a cluster-wide compromise path (§5.4) |
| Where it runs | Admin plane `mcp-gateway-admin`, internal hostname only | The design puts Vault on the admin plane and the public plane must stay Vault-free (`grep -c vault apps/mcp-gateway/values.yaml` must stay `0`) |
| Do agents get `mcp_vault`? | **No**, not in phase 1 | Hermes service accounts have no Vault login path today (§4.2) and a service-account Vault token is model (b) by another name |
| Admin plane | **Keep it dormant (option i)** and schedule, do not start, §11 phase 3 | Phase 0 has 8 open unknowns, 3 of which are outside this repo's control (§10) |

Two deviations must be accepted before any of this is shippable, both already recorded in the design
doc: the per-route `x-vault-token` allow and `statefulMode: stateless`. §8 restates them with the
evidence, what breaks without each, and how to verify.

## 2. Phase 0: resolve the unknowns before writing any YAML

No chart file should be authored until each line below has a measured answer recorded in this file.

| ID | Unknown | How to answer it | Why it gates everything |
|---|---|---|---|
| U1 | Contents of the published image: entrypoint, binary path, `Config.User`, whether any shell or HTTP client exists | `docker manifest inspect` plus `docker inspect` on the pinned amd64 digest | Decides whether `exec` probes are possible at all (§7) and whether `runAsNonRoot` works |
| U2 | Whether the security fixes on `main` will be released as a container | Watch `hashicorp/vault-mcp-server` releases and the Docker Hub tags page | §3.6: the published image is 18 months older than the fixed source; a self-built image changes the supply-chain story and the pinning convention |
| U3 | Per-request header semantics on **this** image: is `X-Vault-Token` honoured per request with no ambient `VAULT_TOKEN`, and is the env fallback used when the header is absent | Re-run `test/mcp-gateway/vault-official-stateless-probe.py` against the pinned digest, plus a real `hashicorp/vault:2.1.2` (the earlier run used a 1.15 dev fixture) | The prior run proves the semantics on a Vault **1.15 local fixture**, explicitly "not version parity with the production Vault" (`planning/mcp-gateway-implementation.md:905-908`) |
| U4 | Whether the MCP clients here (OpenCode, Hermes) can attach a custom `X-Vault-Token` header to every request, and tolerate a server that issues no `Mcp-Session-Id` on the admin plane | Local client config trial against the disabled-then-enabled route | If the client cannot send a second credential, model (a) has no caller and the whole feature is unusable |
| U5 | Does Vault have an audit device? | Measured today: **no**. `platform/vault/templates/vault.yaml` `externalConfig` declares only `auth`, `secrets`, `policies` (lines 199-409); repo-wide grep for `audit_devices` / `sys/audit` returns zero | Model (a)'s headline benefit ("Vault's audit log names the real caller") is **not available until an audit device is enabled**. §4 must not oversell it |
| U6 | Live pod labels of the Vault instance, for the backend's egress rule | `kubectl --context=grigri -n vault get pods -l app.kubernetes.io/name=vault --show-labels` (read-only) | Pitfall §6.4 of the gap-closure plan: a selector copied from a chart rather than from live pods silently denies real traffic |
| U7 | Who can already log in to Vault via OIDC, and what they get | Read-only check of the `vault-admins` group membership; **never record names or the account-to-privilege mapping in git** (repo rule) | §4.2: the only existing OIDC role grants `policies = admin`, so today's per-caller token is root-equivalent |
| U8 | Does the admin plane's discovery route actually answer? | Render with `backends.vault.enabled=true` and compare against the public plane's `directResponse` approach | The public plane documents that the advertised discovery path is **forwarded to the backend instead of answered by the gateway** (`apps/mcp-gateway/values.yaml:185-207`), while the admin template only adds a second `exact` match (`apps/mcp-gateway-admin/templates/configmap.yaml:51-56`) and has no `directResponse`. One of the two is stale |

## 3. What the upstream project actually is

Sources fetched 2026-11-10:
`https://github.com/hashicorp/vault-mcp-server/blob/main/README.md` (via raw),
`https://developer.hashicorp.com/vault/ai/mcp-server`,
`https://github.com/hashicorp/vault-mcp-server/releases`,
`https://hub.docker.com/r/hashicorp/vault-mcp-server/tags`,
`https://github.com/hashicorp/vault-mcp-server/blob/main/CHANGELOG.md`.

### 3.1 Image and pin

* Published as Docker Hub `hashicorp/vault-mcp-server` (HashiCorp, "Verified Publisher").
* Tags present: `0.1.0`, `0.2.0`, `0.2`, `latest`. `latest` and `0.2` resolve to the same manifest as
  `0.2.0`. All were last pushed "about 1 year ago" as of this writing.
* `0.2.0` is multi-arch: `linux/386`, `linux/amd64`, `linux/arm/v6`, `linux/arm64`. The amd64 image
  manifest digest listed on the tags page is `sha256:26cbff73ae6b...` and the index digest starts
  `sha256:796962664075...`.
* **Pin discrepancy:** the committed local probe pinned digest
  `sha256:e4e8df2fb7d27f12c5cb8d1c5b37663a9569ad5ce0047fa0281c56a1769222aa`
  (`planning/mcp-gateway-implementation.md:885-886`). That does not match either digest listed on the
  tags page today. UNVERIFIED which is correct for `0.2.0` on `linux/amd64`. Resolve in U1 and pin the
  digest actually pulled; do not carry the old value forward by assumption.
* No Helm chart is published upstream. Only a `Dockerfile` and a Go binary
  (`https://github.com/hashicorp/vault-mcp-server/blob/main/Dockerfile`). Per
  `docs/conventions/helm.md` ("Use `app-template` from bjw-s only when no maintained official chart
  exists"), `app-template` is the correct base here.

### 3.2 Transports, endpoint, port

| Setting | Default | Source |
|---|---|---|
| Transports | `stdio` (default) and Streamable HTTP | README intro and "Setup" section |
| Enable HTTP | `./vault-mcp-server http --transport-port 8080`, or `TRANSPORT_MODE=http` | README "Command Line Usage" / "Environment Variables" |
| Bind host | `TRANSPORT_HOST` default `127.0.0.1`, so `0.0.0.0` must be set explicitly for in-cluster use | README "Environment Variables" |
| Port | `TRANSPORT_PORT` / `--transport-port`, default `8080` | same |
| MCP path | `MCP_ENDPOINT`, default `/mcp` | same |
| CORS | `MCP_ALLOWED_ORIGINS` (default empty), `MCP_CORS_MODE` default `strict`; README states it is "a CORS protection and *not* a network connection allowlist" | README "Environment Variables" and Security Note |
| TLS | `MCP_TLS_CERT_FILE` / `MCP_TLS_KEY_FILE` (plain HTTP by default) | same |
| Rate limit | `MCP_RATE_LIMIT_GLOBAL` `10:20`, `MCP_RATE_LIMIT_SESSION` `5:10` | same |

HashiCorp's own docs mark the Vault MCP server **Beta** and say "We strongly discourage using beta
features in production deployments of Vault"
(`https://developer.hashicorp.com/vault/ai/mcp-server`). The README adds "At this stage, the MCP
server is intended for local use only ... deployed in trusted networks with trusted clients." This
deployment is exactly the case those sentences warn about, so the admin plane's internal-only
positioning and the CNP are the compensating controls, not optional polish.

### 3.3 How it authenticates to Vault (the crux)

Static configuration is `VAULT_ADDR` (default `http://127.0.0.1:8200`), `VAULT_TOKEN` (documented as
"required") and `VAULT_NAMESPACE` (optional).

Per-request passthrough **is supported**. Quoting the README section "HTTP Mode Configuration":

> In HTTP mode, Vault configuration can be provided through multiple methods (in order of
> precedence): **HTTP Headers**: `VAULT_ADDR`, `X-Vault-Token`, and `X-Vault-Namespace`
> **Environment Variables**: Standard `VAULT_ADDR`, `VAULT_TOKEN`, and `VAULT_NAMESPACE` env vars

and the adjacent note:

> Do not supply `VAULT_ADDR` as a URL query parameter. Use the `VAULT_ADDR` HTTP header instead.

Consequences for this design:

1. Model (a) is possible without custom code: the backend holds **no** `VAULT_TOKEN`, each request
   carries the caller's own `X-Vault-Token`, and Vault's ACLs decide. This is the same conclusion the
   repo reached locally (`planning/mcp-gateway-implementation.md:917-924`, option (a)).
2. The header fallback means an ambient `VAULT_TOKEN` env would silently upgrade any request that
   omits the header. §7 therefore makes "no `VAULT_TOKEN` env, no ExternalSecret" a hard requirement,
   and §9.3 asserts it.
3. `VAULT_ADDR` is also caller-settable per request. The measured product behaviour is that it
   **honours** a caller-supplied address in both header and query form
   (`planning/mcp-gateway-implementation.md:898`). The gateway's strip list is the only thing keeping
   the Vault endpoint fixed; that is a gateway property, not a Vault fix, and it must stay asserted.
4. No AppRole, no Kubernetes auth, no OIDC: the server does not log in to Vault itself. It only
   presents a token it was given. That single fact is why `automountServiceAccountToken` can be
   `false` (§7).

### 3.4 Tool set and its privilege

From the README "Available Tools" section, the server exposes three groups:

| Group | Tools | Vault API surface it needs |
|---|---|---|
| Mounts | `create_mount`, `list_mounts`, `delete_mount` | `sys/mounts` (write, delete). `delete_mount` destroys an engine and everything under it |
| KV | `read_secret`, `write_secret`, `list_secrets`, `delete_secret` | `secret/data/*`, `secret/metadata/*` |
| PKI | `enable_pki`, `create_pki_issuer`, `list_pki_issuers`, `read_pki_issuer`, `create_pki_role`, `read_pki_role`, `list_pki_roles`, `delete_pki_role`, `issue_pki_certificate` | `pki/*` including issuer and role writes |

The docs page lists the same capability set
(`https://developer.hashicorp.com/vault/ai/mcp-server`, "How it works").

Two properties that shape the whole design:

* **Privilege is whatever the presented token can do.** There is no independent authorisation inside
  the server. A root-equivalent token stays root-equivalent through the gateway
  (`planning/mcp-gateway-implementation.md:900-903`).
* **The tool surface cannot be reduced.** The README documents no allowlist, no `--disable-write`, no
  read-only flag and no per-group toggle. UNVERIFIED at source level (the Go files under `pkg/tools/`
  were not read for this plan); treat "no flag exists" as a documentation-level finding to re-check in
  U1/U3. Practical effect: an agent will *see* `delete_mount` and `write_secret` and be refused by
  Vault, so least privilege has to be expressed in a Vault policy, never in the chart (§5).

### 3.5 Alternatives considered

Credible alternatives found while researching: third-party community servers (for example an
`mschuchard/vault-mcp-server` listing on `mcpservers.org`), and HashiCorp's separate
`vault-radar-mcp-server` (HCP/scan data, not a KV server), plus the "MCP Remote Vault Demo" in
`hashicorp/vault-examples`, which is a FastMCP sample app showing Vault-issued dynamic credentials for
*other* MCP servers rather than a Vault server itself. None was evaluated in depth: UNVERIFIED for
all of them. The predecessor plan already fixed the frame as "official MCP exists"
(`docs/design/mcp-gateway-architecture.md`:234) and §4's caller-preserving model depends on the
per-request header documented in 3.3, which no alternative was shown to provide. **Do not adopt a
community server here**: it would replace a first-party credential-handling path with an unaudited one
for no gained capability.

### 3.6 Release state versus source state (a real risk)

The releases page lists only `v0.1.0` and `v0.2.0` (`v0.2.0` released 24 Sep, commit `c67575d`). The
CHANGELOG has a `0.2.1` entry (Gemini extension, PR #56) with **no release and no container tag**.
Meanwhile the repo's recent history includes `test: add test case for VAULT_ADDR query parameter
rejection` (11 Aug 2026) and a merge of `VAULT-51441-sec-vuln-fixes` (PR #135, 15 Oct 2026).

Therefore: the only publishable image (`0.2.0`) predates the `VAULT_ADDR` query rejection and the
October security fixes. Pinning it means shipping code with known SSRF-shaped behaviour and relying on
the gateway's strip list and query-string drop as the mitigation, exactly as recorded at
`planning/mcp-gateway-implementation.md:860-866`. Options, in order of preference:

1. Wait for a released image that contains the fixes (cheapest, outside our control: U2).
2. Build the image from a pinned upstream commit in this repo's CI and mirror it, with a provenance
   note in the chart. Costs: build pipeline, an internal registry path, and the pinning convention
   now covers a commit rather than a tag.
3. Pin `0.2.0` and treat the gateway strip list as the control. This is what the local evidence
   supports today, and it is defensible **only** while the Vault MCP pod's egress is limited to the
   Vault service (§7), because a stripped-header bypass plus a wide egress policy is the actual SSRF.

## 4. Identity model, with the trade-off explicit

### 4.1 What exists in this cluster today

| Item | State today | Evidence |
|---|---|---|
| Vault version | `hashicorp/vault:2.1.2` | `platform/vault/templates/vault.yaml:9-10` |
| Operator / packaging | bank-vaults `vault-operator` `1.24.1`, `kind: Vault`, `size: 1` | `platform/vault/Chart.yaml:5-7`, `vault.yaml:1-8` |
| Storage | Raft, `/vault/file`, 1 Gi RWO PVC | `vault.yaml:151-154`, `124-134` |
| Reachability | ClusterIP `vault:8200` with TLS listener, plus ingress `vault.internal.grigri.cloud` on `nginx-internal` | `vault.yaml:88`, `110-118`, `155-160` |
| KV v2 mount | `secret`, `version: 2` | `vault.yaml:243-248` |
| Other mounts | PKI at `pki/grigri` and `pki/iot` | `vault.yaml:250-331` |
| Auth method: Kubernetes | Enabled. Roles `allow-secrets` (SA `external-secrets` in ns `external-secrets`, policy `allow_secrets`, ttl 1h) and `allow-iot-pki` (SA `cert-manager`) | `vault.yaml:221-241` |
| Auth method: OIDC | Enabled at path `oidc`, discovery `https://idm.grigri.cloud/oauth2/openid/vault`, `default_role: default`, role `default` → `policies: admin`, `user_claim: email`, ttl 1h, redirect URIs include the UI and `http://localhost:8250/oidc/callback` | `vault.yaml:200-220` |
| Kanidm side of that OIDC | OAuth2 client `vault` exists (public + PKCE, `allowLocalhostRedirect`, `scopeMap: vault-admins → openid/profile/email`) and group `vault-admins` exists as a `KanidmGroup` with no `members` field | `platform/vault/templates/kanidm-oauth2-client.yaml`, `platform/vault/templates/kanidm-groups.yaml` |
| Vault audit device | **None** | U5 above |
| ExternalSecrets path | ClusterSecretStore `vault`: `server: https://vault.vault:8200`, `path: secret`, CA from Secret `vault-tls` in ns `vault`, Kubernetes auth `mountPath: kubernetes`, `role: allow-secrets` | `platform/external-secrets/resources/clustersecretstore.yaml:1-21` |

Precise gap statement: **a per-caller Vault token is obtainable today by a human**, with
`vault login -method=oidc` (the CLI redirect `http://localhost:8250/oidc/callback` is already in both
the Kanidm client and the Vault role). The gap is not "no OIDC login path exists"; it is that the only
role that path can take grants the `admin` policy
(`vault.yaml:343-409`: `sys/policies/acl/*`, `auth/*`, `sys/auth/*`, `secret/*`, `pki/*`,
`sys/mounts/*`, all with `sudo`). Handing that to an MCP client would be a deliberate privilege
escalation, not a delegation.

### 4.2 Model (a): caller-preserving

Each caller obtains their own Vault token and presents it as `X-Vault-Token` on every request through
the gateway. Vault's ACLs authorise; the gateway's Kanidm bearer authorises *route use*, Vault
authorises *data access*. Two independent checks, neither of which the gateway has to bridge.

What must be built for (a) to be safe rather than merely possible:

1. A **narrow Vault policy** plus a **narrow OIDC role** (for example `mcp_read`), committed to
   `platform/vault/templates/vault.yaml` under `externalConfig`, so it is reconciled by the
   vault-configurer instead of hand-applied. Draft in §5.2.
2. Acceptance that role selection at login is **voluntary**. Vault's OIDC login takes `-role=` from
   the user; `groups_claim` is commented out (`vault.yaml:218`), so nothing binds a person to the
   narrow role. A caller who can already mint an `admin` token can also mint one and use it through the
   MCP route. The honest security property of (a) is therefore: *the MCP route adds no privilege beyond
   what the caller already holds in their own Vault token*, which is exactly the property that makes it
   acceptable, and it is **not** the property "MCP callers are limited". Say this in the design doc
   rather than implying the stronger claim.
3. A way for the client to keep the token fresh: the role TTL is 1h (`vault.yaml:220`), so an
   long-running agent session fails within the hour with a Vault "missing client token" or permission
   error surfaced **inside an HTTP 200 JSON-RPC error** (pitfall §6.3 of the gap-closure plan). Cheap
   mitigation: raise the role TTL for the narrow role, or re-mint by hand; do not solve it with an
   ambient token.
4. **Agents are excluded in phase 1.** A Hermes workload has no Vault identity: Kubernetes auth is
   bound to exactly two SAs outside its own namespace (`vault.yaml:226-241`), so `mcp-gateway-agents`
   cannot produce a caller token. Giving it one would mean a new Vault Kubernetes role bound to the
   agent's SA, i.e. a **shared** credential used by every agent instance, which is model (b) with
   extra steps (§6.3).

### 4.3 Model (b): shared service token

The backend pod holds one token from an ExternalSecret and every caller acts with it.

Why this is different in kind from the Firecrawl precedent the owner already accepted. Firecrawl's
credential is *no credential*: the in-cluster API is unauthenticated and the destination is fixed
(`apps/firecrawl-mcp/values.yaml:68-70`), so a shared identity there means "everyone can fetch via one
crawler". For Vault:

* The token would have to read the same KV mount that stores the credentials of everything else. The
  ClusterSecretStore's own policy `allow_secrets` is `secret/*` with create/read/update/delete/list
  (`vault.yaml:333-337`): a token at that level **is** every app's credential, including the Cloudflare
  API tokens at `/cert-manager/cloudflare` and `/external-dns/cloudflare`, the Git/CI credentials under
  `/argocd/*` and `/git/*`, and 62 distinct paths overall (measured by grepping `remoteRef` keys in
  `apps/`, `system/`, `platform/`, `bootstrap/`).
* The credential itself would have to live in Vault to be delivered by an ExternalSecret whose store is
  Vault: a recursive master key, plus a rotation story that the design doc already forbids ("No
  automatic static credential fallback", `docs/design/mcp-gateway-architecture.md`:352-353).
* Vault-side attribution disappears completely: every read looks like one token to Vault, and the only
  witness is the gateway log, which has no audit device to corroborate it (U5).
* A single leaked pod equals total secret compromise, with no per-caller containment.

### 4.4 Recommendation

**Model (a), humans only, read-only, phase 1.** Justified by (i) the predecessor plan already putting
"a shared admin credential" out of bounds for Vault, (ii) §4.3's blast radius, and (iii) §3.3: the
capability exists upstream without custom code, and the repo has local evidence that it works end to
end behind the pinned gateway.

## 5. Least privilege

### 5.1 Intended use and the threat model

Use case: an agent or operator reads, and possibly later writes, KV v2 secrets under specific prefixes
so it can operate the homelab (inspect an app's config, rotate a value it owns). Explicitly **not** in
scope: reading policies, creating tokens, mounting or removing engines, PKI writes, anything under
`auth/`.

### 5.2 Draft policy HCL (NOT applied anywhere)

Committed as `externalConfig.policies` in `platform/vault/templates/vault.yaml` (name
`mcp_kv_read`), plus a matching `auth` role `mcp_read` bound to that policy:

```hcl
# mcp_kv_read: least privilege for tokens presented through the Vault MCP route.
# Phase 1 is read-only. Deny-by-omission: Vault refuses anything not listed.

# Required by the MCP server's own mount discovery (list_mounts / read_secret
# lists mounts first; see planning/mcp-gateway-implementation.md:900-903).
# Read only: it reveals which engines exist, which is acceptable information
# disclosure, and it grants no mutation.
path "sys/mounts" {
  capabilities = ["read", "list"]
}

# Permitted KV v2 prefixes, owner-named. `data/` is the value path, `metadata/`
# is what KV v2 list and destroy-version operations check; both are needed for a
# working read_secret/list_secrets pair on KV v2.
path "secret/data/<app>/*" {
  capabilities = ["read"]
}
path "secret/metadata/<app>/*" {
  capabilities = ["read", "list"]
}
```

Deliberately absent, each with the risk it would open:

| Omitted | Because |
|---|---|
| `secret/*` wildcard | One grant covers all 62 paths, including third-party API tokens and CI credentials |
| `create`, `update`, `delete` | §5.4 |
| `sys/mounts/*`, `sys/policies/*`, `sys/auth` | `create_mount`/`delete_mount` and policy self-editing; a token that can write a policy can write itself a bigger one |
| `auth/token/*` | Token creation is the classic lateral path: mint a long-lived child and use it outside the gateway and outside the audit shape |
| `pki/*` | The PKI tools stay visible to the caller and simply get denied; `issue_pki_certificate` against `pki/grigri` would mint a cert for any `*.grigri` host |
| `sudo` | Any `sudo` on `sys/` widens past the intent of the whole feature |

Also required in the same commit: a `default_role` question. `vault.yaml:208` sets
`default_role: default`, which is the `admin` role. Changing the default to the narrow role would
silently change the owner's own UI login (a behaviour change to an existing auth path), so **add the
role and leave `default_role` alone**, then use `vault login -method=oidc -role=mcp_read` explicitly.
Flagged as open question 4 in §12.

### 5.3 What the agent will still be able to learn

`sys/mounts` read plus `secret/metadata/<app>/*` list reveals path names and key names under the
allowed prefixes, and `read` returns the values. The allow-list is therefore the entire privacy
boundary: naming a prefix in the policy is equivalent to publishing that app's secrets to every holder
of the scope. Review the prefix list as a security decision, per prefix, in its own commit.

### 5.4 Write: recommendation is no, in phase 1

A write-capable Vault MCP is a privilege-escalation path into every application whose credential lives
in that KV mount, because the write does not need to touch Vault's admin API to be devastating: change
`/external-dns/cloudflare`, and the ExternalSecret refresh pushes the attacker's token into a live
workload that then performs attacker-chosen DNS operations. Same shape for `/argocd/*` (Git
credentials feeding the deploy pipeline) and `/git/admin`. The gateway cannot see or gate this: from
its side it is one legitimate `write_secret` by an authorized caller, and the audit line will name a
real caller doing a real escalation.

Recommendation: **read-only in phase 1** (`write_secret` and `delete_secret` remain in the tool list but
are denied by Vault). Revisit write in a later phase only as prefix-scoped grants for paths **created
for that purpose** (for example `secret/mcp-agent/*`, consumed by nothing), never for a path an existing
ExternalSecret reads. If write is later enabled on a real path, it needs its own owner gate, its own
negative test, and an explicit note in `docs/design/mcp-gateway-architecture.md`.

## 6. Where it runs: the admin plane

### 6.1 Justification

`docs/design/mcp-gateway-architecture.md` (Topology) draws the admin plane as the VPN-only home for
"Vault MCP pod" and states "No public-plane route to Vault MCP". The admin plane has its own Kanidm
client (`mcp-gateway-admin`) so a public-plane bearer cannot authenticate there or vice versa (audience
is the client name, `apps/mcp-gateway-admin/values.yaml:199-207`), it is internal-only by DNS
(`mcp.internal.grigri.cloud`, Envoy gateway `internal`, VIP `192.168.193.13`), and its CNP grants **no
`world` egress** (`apps/mcp-gateway-admin/templates/ciliumnetworkpolicy.yaml:129-136`). That is the
right home for a target whose blast radius is the whole secret store. Keep it: no public-plane Vault
route, ever, and keep the guard `grep -c vault apps/mcp-gateway/values.yaml` at `0`.

### 6.2 What enabling a route there requires

Today: `backends.vault.enabled: false` and `routes: []`
(`apps/mcp-gateway-admin/values.yaml:231-246`, `templates/configmap.yaml:41-42`). The values block
already names `path: /vault/mcp`, `resource: https://mcp.internal.grigri.cloud/vault/mcp`,
`scope: mcp_vault`, `hostHeader`, `credential: none`, `stripRequestHeaders: []`, and a chart-check
asserts the route ships disabled (`test/mcp-gateway/chart-checks.py:774-775`).

Required changes, each in the same commit set:

| Item | Change | Notes |
|---|---|---|
| Scope | `mcp_vault` on the `mcp-gateway-admin` client | Already the value's intent; must exist in Kanidm before use, since a route whose JWKS client has no scope map is inert |
| Human group | `mcp-vault-users`, rendered as a `KanidmGroup` with **no `members` field** | Follows `fa96d899a`. `docs/user-guide/kanidm.md`: person membership is manual, never in CRDs. Note the admin chart has **no `humanGroups` key or template today**: `templates/kanidm-group.yaml` is gated on `kanidm.workloadGroup.enabled` and renders only the service group. The public-plane pattern must be ported first |
| Scope map | `kanidm.oauth2Client.humanScopeMap: [{group: mcp-vault-users, scopes: [mcp_vault]}]` | `templates/kanidm-oauth2-client.yaml:24-31` already concats `scopeMap` + `humanScopeMap`, and both are `[]` today |
| `mcp-gateway-agents` | **Do not add `mcp_vault`** | §4.2 item 4: no agent Vault identity exists, so the grant would hand out a route with no way for the grantee to be authorised by Vault. And a service-account Vault token is model (b), which §4.3 rejects. Also the admin workload group is `enabled: false` (`values.yaml:252-257`); leave it that way |
| CEL gate | Copy the public plane's three rules | `apps/mcp-gateway/values.yaml:326-328` shape: `!has(jwt.email)`, `!has(jwt.preferred_username)`, and `has(jwt.scope) && jwt.scope.split(' ').exists(s, s == 'mcp_vault')`. **The admin template has none of the identity rejects and no `has()` guard** (`templates/configmap.yaml:80-94`), so enabling it as-is would be strictly weaker at the edge than the public routes. Aligning this is required, not optional |
| Discovery route | Already emitted by the template as a second `exact` match (`configmap.yaml:51-56`) | But U8 must confirm it is *answered*, because the public plane's own comment says the advertised path is forwarded to the backend unless served by `directResponse`. If the admin match does not answer 200 JSON, port the `directResponse` shape from `apps/mcp-gateway/values.yaml:209-236` (with `scopes_supported: ["mcp_vault"]` and `authorization_servers` at `.../openid/mcp-gateway-admin`) |
| `statefulMode` | Add to the template, set `stateless` for the Vault route | The template renders no `statefulMode` anywhere, so the route would default to stateful, which is the failure mode §8.2 describes. `test/mcp-gateway/foundation-checks.py:143` already asserts every route backend is `stateless`, so the omission fails loudly rather than silently |
| Strip list | Per-route handling of `x-vault-token` | §8.1 |

### 6.3 CNP changes on both sides

1. **Admin gateway CNP**: unchanged for MCP ingress (the internal Envoy proxies and the retained
   nginx rollback rules are already there, `ciliumnetworkpolicy.yaml:74-94`). Its egress is bare
   `toEntities: [cluster]` (`:110-112`), which already reaches `vault-mcp` in its own namespace, so no
   gateway-side egress edit is needed. Do **not** add `world` egress: the backend, not the gateway,
   talks to Vault.
2. **New backend policy** (§7): ingress from the admin gateway pods only on 8080, egress to DNS and
   the Vault service pods on 8200 only.
3. **Existing assertions that encode the current fail-closed decision and must be changed deliberately,
   not "fixed around"**: `chart-checks.py:218-245` (asserts `x-vault-token` and every `vault_*` variant
   is stripped even with `backends.vault.enabled=true`), `:458-466` (asserts no `vault-mcp` in admin
   egress and no `mcp_vault` on the public plane), `:762-780` (asserts the Vault route ships disabled).
   Each must be rewritten to the new intent in the same commit, and the public-plane guards must stay.

### 6.4 Namespace and URL conflict to resolve first

`values.yaml:238` points at `vault-mcp.mcp-gateway-admin.svc.cluster.local:8080`, i.e. the backend
inside the admin namespace. Repo convention is one app per `apps/<name>/` directory whose namespace
equals the folder name (`scripts/deploy-dir.rh`, `docs/conventions/helm.md` "Namespace equals folder
name"). A new `apps/vault-mcp/` therefore lands in namespace `vault-mcp` and the URL must become
`vault-mcp.vault-mcp.svc.cluster.local:8080`, with `hostHeader`, `namespace`, `workloadName` and `port`
in the same block updated. Do not silently keep the old FQDN: it would resolve to nothing, and the
failure would be a gateway-side 5xx with a correct-looking pod.

## 7. The backend chart `apps/vault-mcp/`

Name: `apps/vault-mcp/` (matches `backends.vault.workloadName` and the design doc's "Vault MCP pod").

| Decision | Value | Justification |
|---|---|---|
| Base | `app-template` `5.3.0` (`https://bjw-s-labs.github.io/helm-charts/`) | No official upstream chart exists (§3.1); `docs/conventions/helm.md` allows app-template then. Matches `apps/firecrawl-mcp/Chart.yaml` and the admin plane |
| Controller | Deployment, `replicas: 1`, `strategy: RollingUpdate` | Single replica is the accepted posture for all MCP backends here; HA is future work in the design doc |
| Image | `docker.io/hashicorp/vault-mcp-server`, `tag: 0.2.0`, `digest: <resolved in U1>` | Pin the digest, not just the tag: `latest` and `0.2` alias the same old build, and §3.6 shows tag semantics are unreliable here. `apps/firecrawl-mcp/values.yaml:48-53` is the precedent (tag + digest for a single-arch pin) |
| Renovate hint | `# renovate: datasource=docker depName=hashicorp/vault-mcp-server` above `repository:` | `docs/conventions/helm.md` "Renovate Integration"; a missing hint means the pin never gets a bump PR |
| Command/args | `["http"]`, `["--transport-port","8080","--transport-host","0.0.0.0"]` | HTTP mode is not the default (§3.2) and the default bind is `127.0.0.1`, which is unreachable in-cluster |
| Env | `VAULT_ADDR=https://vault.vault:8200`, `TRANSPORT_MODE=http`, `TRANSPORT_HOST=0.0.0.0`, `TRANSPORT_PORT=8080`, `MCP_ALLOWED_ORIGINS=""`, `MCP_CORS_MODE=strict`, `HOME=/tmp` | `VAULT_ADDR` fixed in the pod, matching the ClusterSecretStore's server (`clustersecretstore.yaml:8`) and the TLS listener. **No `VAULT_TOKEN`.** No `VAULT_NAMESPACE` (OSS Vault, no namespaces; `X-Vault-Namespace` stays stripped) |
| Probes | Prefer `exec` if U1 finds a runnable binary path (e.g. `--version`); otherwise **no probes**, documented | `exec` probes are policy-immune, which is why `firecrawl-mcp` keeps native NPs (`docs/design/mcp-gateway-architecture.md`:296-298, gap-closure §6.1). A `httpGet` probe would need `world` on the **app** port, because this server has no separate health listener (README documents none), and that would expose the MCP listener to every routable pod IP. `apps/grafana-mcp/values.yaml:187-197` is the precedent for "the image has no HTTP client, so we do not fake a probe" |
| `automountServiceAccountToken` | **`false`** | Resolves the tension directly: Kubernetes auth to Vault would need the SA token, but under model (a) the backend never authenticates to Vault itself (§3.3 item 4). The pod therefore needs no cluster identity at all, and a browser-facing-ish MCP pod keeps none. If a later design moves credential acquisition into the pod (AppRole or K8s auth), this flips to `true` **and** becomes a new attack-surface review, because at that point the pod can mint Vault tokens for itself |
| Security context | `runAsNonRoot: true`, `allowPrivilegeEscalation: false`, `readOnlyRootFilesystem: true`, drop `ALL`, `seccompProfile: RuntimeDefault` | Mirrors `apps/firecrawl-mcp/values.yaml:39-44` and `107-112`. U1 must confirm the image's `Config.User`; if it runs as root, set an explicit `runAsUser`/`runAsGroup` (and if it is a `scratch` image with no shell, probes and `HOME` need revisiting) |
| Resources | requests `10m`/`64Mi`, limits `256Mi` | Consistent with both existing backends; a Go static binary of ~5 MB |
| Placement | `nodeSelector: kubernetes.io/hostname: prusik` | Owner decision 2026-11-10 pins all MCP workloads to `prusik` (gap-closure G6/TODO-5.1, `8fa3885ff`). The image is multi-arch (§3.1), so unlike `firecrawl-mcp` no `arch: amd64` pin is required; add it only if the digest pin turns out to be amd64-only |
| Persistence | None | Stateless; `/tmp` emptyDir if `HOME` writes are needed |
| ExternalSecret | **None in model (a)** | Any ExternalSecret here would deliver an ambient `VAULT_TOKEN`, which §4.3 and §3.3 item 2 both forbid. If model (b) were ever approved, the pattern is the standard ClusterSecretStore/`remoteRef.key: /<app>/<path>` shape and the path would be `secret/mcp-gateway/vault`; that path must not exist until the decision is made |
| Service | ClusterIP `vault-mcp:8080` | No ingress resource of any kind: the only peer is the gateway (design doc "No direct backend exposure") |
| Network policy | **Native `NetworkPolicy` pair** (ingress + egress), like `firecrawl-mcp` | Ingress: admin gateway pods only, port 8080 (labels `app.kubernetes.io/name=mcp-gateway-admin`, `instance=mcp-gateway-admin`, `controller=agentgateway`; verified as the pattern in `apps/firecrawl-mcp/templates/networkpolicy.yaml:21-32`). Egress: `node-local-dns` + `kube-dns` on 53 and the Vault pods on 8200 (U6 for live labels). Cilium `toEntities` is unnecessary if probes are `exec`/absent, which is precisely the exception the design doc carves out for native NPs |
| PodMonitor / ServiceMonitor | None | The README documents no metrics endpoint. `apps/firecrawl-mcp/values.yaml:132-137` records the same "404, so do not scrape" reasoning; gateway-side `agentgateway_requests_total` stays the observability path |

## 8. The two deviations, restated with evidence

### 8.1 Per-route `x-vault-token` allow

**Does agentgateway forward it by default?** Yes. The measured property is recorded in the admin
template's own comment: "Everything the gateway does NOT remove reaches the backend verbatim (measured:
`IncomingRequestContext::apply` copies all client headers except `content-*` and the MCP session id, and
with no removals a caller's cookie/x-vault-token/x-api-key all arrived)"
(`apps/mcp-gateway-admin/templates/configmap.yaml:113-117`). Forwarding is the default; the deviation is
not "add an allow", it is **remove `x-vault-token` and `vault-token` from the route's strip list**
(`configmap.yaml:144-145`), which today strips them unconditionally for every admin route. The
predecessor plan states the same blocker in chart terms: "`backends.<name>.stripRequestHeaders` is
additive-only today - the plane default strips `x-vault-token` and a route cannot un-strip it"
(`planning/mcp-gateway-implementation.md:920-922`). So the change is a template refactor from a hardcoded
deny list to a per-route list, keeping `vault_addr`, `vault-addr`, `vault_token`,
`vault_skip_verify`, `x-vault-namespace` and their spellings stripped on **all** routes
(`configmap.yaml:146-153`): the product honours a caller-supplied `VAULT_ADDR` (§3.3 item 3), and
`vault_token`/`X-Vault-Token` are different header names with the same effect, so `vault_token` must
**stay** stripped even when `X-Vault-Token` is allowed. Verify that asymmetry in U3: it is documented
from the README's header list, not read from source.

**What breaks if not accepted:** the backend receives no credential. Measured: the shipped shape ends in
`Failed to get Vault client: vault token not provided for session`
(`planning/mcp-gateway-implementation.md:895`), i.e. every tool call fails. Model (a) is then impossible
and the only remaining shape is (b).

**Residual risk:** the header is a bearer with a 1h life crossing the gateway in cleartext over
cluster-internal HTTP (`vault-mcp:8080` is plain HTTP; TLS ends at the Envoy/ingress layer). Anyone who
can read that hop sees a live Vault token. Mitigations that must exist with the allow: backend egress
restricted to Vault (§7), no `world` ingress to the backend, no credential in a query string ever
(the CRITICAL finding: agentgateway logs the full request path, and a synthetic credential was observed
in `?VAULT_TOKEN=`; `docs/design/mcp-gateway-architecture.md`:242-246), and gateway log redaction
reviewed before enablement.

**Side finding worth fixing regardless:** the public-plane routes strip only `Authorization`, `Cookie`
and their backend-specific headers (`apps/mcp-gateway/values.yaml:338-344`, `457-462`, `515-518`); none
strips `x-vault-token`, so a caller's Vault token would be forwarded to `mcp.context7.com` today. Add the
Vault header names to the public strip lists as defence-in-depth.

**How to verify:** a chart check that the rendered Vault route's `remove` list excludes
`x-vault-token`/`vault-token` but still includes every `vault_addr`/`vault_token`/`x-vault-namespace`
spelling; a public-plane chart check that the reverse holds (Vault headers stripped there, since no
public route may ever pass one); and an in-cluster test (§9.4) that a `vault_addr` header override from a
legitimate, scope-authorized caller does not change which Vault the tools hit.

### 8.2 `statefulMode: stateless`

**What breaks if not accepted:** stateful mode caches the Vault client per MCP session
(`GetVaultClient(session.SessionID())`), so a session id becomes a bearer credential: caller B replaying
A's `Mcp-Session-Id` is served **with A's cached Vault token**, rotation is ignored for the life of the
session, and a session minted by one instance is accepted by a second
(`planning/mcp-gateway-implementation.md:846-859` and `:896`; also
`test/mcp-gateway/session-binding-probe.py`). That is a broken two-user isolation gate, and the pinned
gateway cannot fix it, because `MCPSessionState` stores no caller identity at all.

**Residual risk under stateless:** no `Mcp-Session-Id` is issued, which is the point, but it puts MCP
session semantics on the client. U4 must confirm OpenCode and Hermes accept a server that never returns a
session id (the two live public routes already do this, so the risk is low, and
`planning/mcp-gateway-implementation.md:923-924` notes the client check is still outstanding). The other
residual: stateless means **every** request must carry the Vault credential, so a client that caches
headers per session and drops them later produces confusing "missing client token" errors rather than 401s.

**How to verify:** render-level assertion (`statefulMode: stateless` on the Vault backend, which
`test/mcp-gateway/foundation-checks.py:143` already requires of every route), plus a live two-caller test
in §9.4 that caller A's tool result never appears for caller B.

## 9. Verification plan

Ordered. Steps 1-2 are agent-runnable now; 3-6 are owner-approved live actions, and nothing in this plan
is applied before its gate passes ("Commit first, then apply").

### 9.1 Render and lint (no cluster contact)

```bash
helm dependency build apps/vault-mcp/
helm template --include-crds --namespace vault-mcp vault-mcp apps/vault-mcp/ | less
helm lint apps/vault-mcp/
pre-commit run --all-files
```

Render the gateway with the route on, without changing the shipped default:

```bash
helm template --namespace mcp-gateway-admin mcp-gateway-admin apps/mcp-gateway-admin/ \
  --set backends.vault.enabled=true | sed -n '/config.yaml:/,$p'
grep -c vault apps/mcp-gateway/values.yaml   # must stay 0
```

### 9.2 Repo assertion suites

```bash
python3 test/mcp-gateway/chart-checks.py      # 200 as of fa96d899a
python3 test/mcp-gateway/route-checks.py      # 110
python3 test/mcp-gateway/foundation-checks.py # 61
python3 test/mcp-gateway/local-boot-checks.py # 79
python3 test/mcp-gateway/discovery-checks.py
```

New assertions to add (the Vault-relevant shape, mirroring how the live routes are asserted):
Vault route rendered with `stateless`; `remove` excludes `x-vault-token` but includes every other
`vault_*` spelling (`chart-checks.py:243-245` is the list to invert per-route); CEL contains all three
rules including the two identity rejects; scope `mcp_vault` appears only in the admin render;
`KanidmGroup mcp-vault-users` renders with no `members` key; no ExternalSecret, no `VAULT_TOKEN` env and
no `automountServiceAccountToken: true` in the `apps/vault-mcp` render; backend egress names exactly DNS
pods and Vault pods, nothing else.

### 9.3 In-cluster verification Job

Add `test/mcp-gateway/jobs/verify-vault-mcp.yaml`, owned by the same pattern as
`jobs/verify-networkpolicy.yaml`. Assert, from a pod that is **not** the admin gateway: TCP to
`vault-mcp:8080` times out (gateway-only ingress); TCP to `vault:8200` from the backend pod succeeds and
from an arbitrary pod in another namespace is denied or allowed per the actual Vault CNP (read the live
Vault-side policy first; do not assume); and the backend's DNS path works. Two repo-specific traps to
respect: the Job image must be pinned by a **verified digest** (G13: the committed digest did not resolve
and had to be repaired in `30abdcd74`), and the embedded shell is **not** linted by pre-commit, so the
script must fail loudly (`exit 1`) rather than depend on linting. Applying and deleting the Job is a
cluster mutation outside ArgoCD's watch: owner approves and applies it.

### 9.4 Live acceptance (owner-approved, after sync)

1. Positive, real KV read through the gateway: create a purpose-made dummy path first
   (`vault kv put secret/<allowed-prefix>/mcp-smoke value=<dummy>`), then call `read_secret` through
   `https://mcp.internal.grigri.cloud/vault/mcp` with a **real caller Vault token** from the narrow role
   and a valid `mcp-gateway-admin` bearer. Assert the read succeeds and that no secret value is ever
   echoed into logs, this document or the test output (assert on shape and length, not content). Delete
   the dummy path afterwards.
2. Positive, `list_secrets` on the same prefix, proving `sys/mounts` + `metadata` grants are sufficient
   (this is the exact combination that confused the local fixture, per
   `planning/mcp-gateway-implementation.md:900-903`).
3. Negative, no scope: a caller whose Kanidm token lacks `mcp_vault` gets **403 at the edge** and the
   backend logs nothing. Also repeat the three wrong-scope combinations used for Firecrawl, plus an ID
   token carrying `email` (must be refused by the new identity-claim rules).
4. Negative, no Vault credential: a valid `mcp_vault` bearer with **no** `X-Vault-Token` header reaches
   the backend and fails with the "token not provided" shape inside a JSON-RPC error, and there is no
   ambient fallback (proves §7's no-`VAULT_TOKEN` rule).
5. Negative, per-caller ACL: a token from a role with no KV grant is denied; a disallowed prefix under a
   narrow token is denied; `create_mount`/`delete_mount`/PKI tools are denied by Vault.
6. Negative, no Vault-address override: send `vault_addr` and `VAULT_ADDR` headers with a valid caller
   bearer and confirm the tools still hit the configured Vault (and that `?vault_addr=` does not
   redirect either).
7. Negative, bypass: from an arbitrary in-cluster pod, direct MCP POST to `vault-mcp:8080` is denied
   (§9.3), and no caller can reach Vault's API without presenting a real Vault token: the gateway adds no
   credential of its own.
8. Parity with the two live routes: `stateless` behaviour confirmed with a real MCP client (U4), and the
   RFC 9728 discovery URL answered with `mcp_vault` in `scopes_supported` (U8).

### 9.5 Audit trail

```logql
{namespace="mcp-gateway-admin"} | json | scope="request"
```

Expect, per call: `jwt.sub` (full, per the owner's 2026-11-10 decision to keep it), the target, the
tool name, `http.status`, `trace.id`. Two cautions: the tool-name field measured on the live public
plane is `gen_ai.tool.name` (gap-closure §1.2, `planning/mcp-gateway-gap-closure-implementation.md:87-89`),
not `mcp.tool.name`; confirm the actual field on the admin plane before writing any alert or dashboard.
And the log is silent on *Vault-side* authorisation: a Vault denial appears as a successful gateway call
carrying an error payload, so §9.4's ACL negatives must read the tool result, not just the status
(pitfall §6.3). With no Vault audit device (U5), nothing on the Vault side names the caller; enabling
one is phase 4 work and needs its own storage/retention decision.

## 10. Should the admin plane exist at all?

The owner's open question (G3): the plane consumes a pod, a CNP, an Ingress, an HTTPRoute and a scrape
target while serving nothing.

| Option | Cost | Risk | What it commits us to |
|---|---|---|---|
| (i) Keep dormant (today) | ~64-256 Mi of RAM, one 1-replica Deployment, 1 CNP, 1 Ingress, 1 HTTPRoute + its DNS name, 1 ServiceMonitor target, 1 Kanidm OAuth2 client, ~389 log lines/24 h of health-check 404s (measured, gap-closure §3.3) | **Rot**: the plane's template already diverges from the public plane (no identity-claim CEL, no `statefulMode`, an unverified discovery path, a stale backend FQDN). Unexercised code drifts silently, which is this repo's own §6.4/§6.6 pitfall class | Nothing beyond re-verifying it whenever the public plane changes |
| (ii) Delete until Vault is wanted | Objects that disappear: namespace `mcp-gateway-admin`, its Deployment/Service/ServiceAccount/ConfigMap/Secret (`mcp-gateway-admin-session-key`) plus the ESO generator, the CNP, the nginx-internal Ingress and the `internal` Gateway HTTPRoute and the `mcp.internal.grigri.cloud` DNS record, the ServiceMonitor, the ArgoCD Application, the `mcp-gateway-admin` Kanidm OAuth2 client, and the whole `configmap.yaml` template. Re-adding costs: re-authoring that 176-line template, i.e. re-learning the measured header boundary from scratch; a fresh session key (harmless, `CreatedOnce`); a new Kanidm client, so every admin caller re-consents; the DNS name again. Estimate 1-2 focused sessions | Deleting the Kanidm client and its consent state is not a tidy-up; it is an irreversible-ish identity change | Deferring MVP criterion 4 indefinitely |
| (iii) Land Vault now | §11 phases 0-3, both §8 deviations accepted by the owner, plus the narrow-role and policy work in §5.2 and the client-header capability question (U4) | Ships a beta upstream product, in production, against the credential store of every other app, on an image older than its own security fixes (§3.6) | A privileged path we must then operate: token expiry UX, no revocation, audit device, per-prefix reviews forever |

**Recommendation: (i) keep it dormant**, with two conditions. First, treat §6.2's divergences as
documentation debt to fix in a *route-disabled* commit (align CEL, add `statefulMode`, resolve U8,
correct the backend FQDN), so the template is honest about what it will do when a route is enabled.
Second, set a re-decision trigger rather than a date: start §11 phase 1 only when (a) a released image
containing the security fixes exists (U2), **or** (b) a named agent workflow needs KV access and cannot
run with `kubectl` plus ExternalSecrets. Do **not** choose (ii): the plane's cost is small and fixed
while its deletion destroys the one artifact that encodes the measured header boundary. Do **not**
choose (iii) yet: three of the eight phase-0 unknowns are outside this repo's control.

## 11. Phasing, execution order, and rollback

Smallest blast radius first. Each phase ends at a gate; no phase starts before its predecessor's gate
passes. Live application is always: commit, push, ArgoCD reconcile.

| Phase | Contents | Gate (proof before moving on) |
|---|---|---|
| 0. Resolve unknowns | U1-U8 only. Research, renders, read-only `kubectl`, re-run the committed probes | Written answer for all 8 in this file. **No YAML authored before this gate** |
| 1. Decisions | Owner answers §12 (identity model, read-only, agents, plane option). Record them in `docs/design/mcp-gateway-architecture.md` plus the two §8 deviations as *accepted* or *rejected* | Design doc updated and committed; deviation text names the residual risk, not just the acceptance |
| 2. Policy and identity (no route) | Add `mcp_kv_read` policy + `mcp_read` role to `platform/vault/templates/vault.yaml` (additive; `default_role` untouched). Port the `humanGroups` key and template to the admin chart; create `mcp-vault-users` (no `members` field) and the `humanScopeMap` entry. Align the admin route template (CEL identity rejects, `statefulMode`, U8's discovery fix) | `helm template` diffs reviewed; a manual `vault login -method=oidc -role=mcp_read` yields a token that can read an allowed prefix, cannot list policies, and cannot write. Owner adds themselves to `mcp-vault-users` by hand |
| 3. Backend, unrouted | Create `apps/vault-mcp/` per §7 with the route still `enabled: false`; add its native NPs | Pod `1/1` and `Ready` with no policy-caused probe failure (watch restarts for ≥180 s, the repo's convention); an unlisted pod cannot reach 8080 (§9.3 Job); `chart-checks` and `foundation-checks` pass |
| 4. Route on, internal only | `backends.vault.enabled: true`; update the four assertion groups in §6.3 item 3; ArgoCD sync | Full §9.4 acceptance, both §8 verifications, Loki shows `jwt.sub` + tool + status for a real call, and `grep -c vault apps/mcp-gateway/values.yaml` still `0` |
| 5. Hardening (optional, separate) | Vault audit device; alerts on the admin route's 4xx/5xx (the existing PrometheusRule groups are namespace-scoped, check whether they already cover a new route); the public-plane Vault header strip (§8.1 side finding); a live two-caller A/B via `test/mcp-gateway/identity-ab/` | Each its own commit and gate; none of it is a precondition for keeping phase 4 up |

**Rollback** is per-change `git revert` plus an ArgoCD reconcile (the owner closed the separate
rollback-exercise question on 2026-11-10, gap-closure G5). Two properties make it cheap here: routes
reload in place, so flipping `backends.vault.enabled` back to `false` does **not** restart the gateway
(measured, `apps/mcp-gateway/values.yaml:169-173`), and deleting the backend chart removes a pod and two
NPs with no data. The one artefact that does not roll back cleanly is a **Vault-side write** made
through the route (phase 5+ only): that is a data change, not a config change, which is §5.4's other
argument for read-only in phase 1. Note also the standing rule: removing a CNP is not the rollback for
an auth problem.

## 12. Open questions for the owner

1. **Identity model (a) caller-preserving or (b) shared service token?** Recommendation: **(a)**. The
   strongest single reason: under (b) one credential in one pod reads all 62 KV paths, including the
   third-party API tokens the cluster's own ExternalSecrets consume, so a pod compromise is a full-stack
   compromise; (a) adds no privilege beyond the caller's own token. (b) is also already excluded for
   Vault in `planning/mcp-gateway-implementation.md:928-929`.
2. **Phase 1 read-only, or read-write?** Recommendation: **read-only**. Write on any prefix an
   ExternalSecret already reads converts a tool call into credential replacement in a live workload, and
   the gateway cannot distinguish it from legitimate use.
3. **Do agents (`mcp-gateway-agents`, Hermes) get `mcp_vault` at all?** Recommendation: **no**. No Vault
   identity exists for them (§4.1's Kubernetes auth bindings), and minting one shared agent token is
   model (b) reintroduced.
4. **Add the narrow OIDC role and leave `default_role: default` (the `admin` role) alone, or change the
   default too?** Recommendation: **leave the default alone** and require `-role=mcp_read` explicitly.
   Changing `default_role` silently alters the owner's own Vault login and the UI path
   (`vault.yaml:208`, `213-214`).
5. **Accept the two §8 deviations?** Recommendation: accept both, conditioned on U3 re-running the probe
   against a real Vault 2.1.2 and the pinned digest, because the existing evidence is a local 1.15
   fixture. Without them there is no model (a) at all.
6. **Admin plane: (i) dormant, (ii) delete, (iii) land Vault now?** Recommendation: **(i)**, with the
   phase-2 template alignment done while route-disabled, and a trigger-based re-decision (§10).
7. **Is a self-built image from a pinned upstream commit acceptable** until an upstream release contains
   the security fixes (§3.6 option 2)? Recommendation: **yes, if** phase 4 is otherwise blocked by U2,
   with provenance recorded in the chart comments. Pinning `0.2.0` and relying on the gateway strip list
   is acceptable only while the backend's egress is Vault-only.
8. **Vault audit device enabled?** Recommendation: **yes, before** any agent (not just humans) is
   allowed through, since without it there is no independent record of what the privileged path actually
   did, and phase 4's acceptance relies on gateway logs alone. Cost and log destination are their own
   decision.
9. **Which KV prefixes, exactly?** Recommendation: phase 4 = **one** prefix, purpose-made and consumed
   by nothing (for example `secret/mcp-smoke/*`), then add real prefixes one reviewed commit at a time.
   §5.3: naming a prefix in the policy is the same act as publishing that app's secrets to every holder
   of `mcp_vault`.
