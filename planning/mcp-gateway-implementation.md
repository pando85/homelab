# MCP Gateway Implementation Plan

**Status:** Task 1 was committed (`9049af3bf`) and auto-deployed by ArgoCD on 2026-11-08 with
**every backend route disabled**, and is **reverted again**: both planes reached `Degraded` because
the attached NetworkPolicies made the gateway pods unreachable from the node on every port, so the
kubelet startup probes on `:15021/healthz/ready` timed out and each container was killed on the probe
threshold while the process itself logged `marking server ready` within ~6 ms. **Both planes never
reached Ready at `9049af3bf`, so session-key stability across reconciles, RFC 9728 discovery, and
disabled-backend 404 were NEVER observed live; they are local render/boot evidence only. No live
gateway or parity claim exists.** The next gate before re-land is an approved GitOps canary isolating
ingress-only vs egress-only probe failure. No backend was ever reachable (routes rendered empty), no
existing workload was disturbed, and the two charts plus the `test/mcp-gateway` foundation suite are
back to local/uncommitted work. Measurement and reasoning: "Second occurrence" in
`docs/troubleshooting/mcp-firecrawl-egress-regression.md`. The Hermes sidecar
keeps serving Grafana until parity is proven.
**Provisional placement decision (2026-11-09):** both MCP gateway planes (and the diagnostic canary)
are pinned to node `prusik` via `nodeSelector: kubernetes.io/hostname=prusik` for diagnosis and
initial rollout. Accepted trade-off: single-node failure domain means the gateway is unavailable when
prusik is down, which is acceptable for a disabled-only foundation and a diagnostic canary where the
goal is controlled observation, not production availability. Placement will be revisited after the
node→pod probe/datapath gate is proven and the canary demonstrates stable Ready state. The earlier Firecrawl fetcher-egress change
(`0b16b31b9` -> `8fff4f224`) is the same failure class; its exact packet-level cause is still not
captured.
**Date:** 2026-11-08
**Architecture:** [docs/design/mcp-gateway-architecture.md](../docs/design/mcp-gateway-architecture.md)
**Owner approval required before any live deployment**

## Overview

**Review corrections applied (2026-11-08, foundation review):** B1 session key moved off
Helm random/`lookup` onto the ESO native generator with `CreatedOnce` + immutable target (deterministic
renders); B2 both charts now ship `kanidm.humanScopeMap: []` - no human grant is wired, and the group
choice is a separate gate on the grant only; B3 the Grafana delegation candidate moved back to the
**public** plane where the approved architecture puts it (the admin plane is Vault only) and its
backend namespace is its own `grafana-mcp`, not `mcp-gateway-admin`; the NetworkPolicy comments no
longer claim that `enable-host-firewall=false` guarantees kubelet probe traffic is unaffected - that
assumption is now **measured false** on the live cluster: a pod carrying these policies answers no
port from the node (host-sourced dial times out at 6.14 s on every port, including a port with no
listener), while a policy-free pod answers in 0.07 s. No blanket host/remote-node allow is added,
because Kubernetes NetworkPolicy cannot express a node peer - the open question for re-land is a
Cilium-native mechanism (for example a CiliumNetworkPolicy with `fromEntities: host` /
`toEntities: host`, or `enable-host-firewall` plus an explicit node allow) proven by a bounded
host-sourced dial or Hubble verdicts. All routes stay off; the initial rollout scope, if deployment is ever
approved separately, is the public plane first, and the new pods' exact label selectors mean a NotReady
gateway pod cannot disturb an existing workload.


Phased implementation of centralized MCP gateway using upstream agentgateway (Rust standalone).
Two deployments: `apps/mcp-gateway` (public) and `apps/mcp-gateway-admin` (private), each with
its own Kanidm OAuth2 client so a token minted for one plane cannot be replayed on the other.
Native OAuth resource-server auth (signed JWT + JWKS, no oauth2-proxy, no cookie redirect),
NetworkPolicy isolation, one dedicated pod per backend. Routes render from values and the
default render is `routes: []`, so the shipped foundation answers 404 everywhere.

Every "measured" statement below comes from the local suites in `test/mcp-gateway/` running the
pinned v1.7.0 binary (and pinned Grafana/mcp-grafana/firecrawl images) against chart-rendered
configs - not from assumptions or from the live cluster.

## MCP Gateway Follow-up Tasks

- [ ] **Migrate to official agentgateway-standalone chart**: Currently using `app-template` wrapper; migrate to official `oci://cr.agentgateway.dev/charts/agentgateway-standalone`. Verify and pin exact artifact version from official source before migration.

## Task 0: Architecture & Inventory

**Architecture:** done | **agentgateway pin:** done | **Kanidm inventory:** pending (identity agent)

- [x] **agentgateway version selection**: pinned `v1.7.0` (image `cr.agentgateway.dev/agentgateway:v1.7.0`
  plus the same release binary in the local tests). Transport: MCP streamable-HTTP, `mode: strict`
  authentication, JWKS validation. `schema/config.json` at that tag is the source of truth used for
  every policy below - it was read, not guessed.
- [x] **Token-shape inventory (measured, Kanidm v1.11.2 source + local verification)**:
  - ACCESS token (`OAuth2RFC9068Token`): `iss`, `sub` = account UUID, `aud` = OAuth2 **client name**,
    `client_id`, `exp/nbf/iat/jti`, `scope` = one space-separated string, `session_id`. It carries
    **no** `email`, no login, no groups. `claim_map`/custom claims only land in the ID token.
  - ID token (`OidcToken`): `preferred_username`, `email`, `scopes` **array**, `azp`.
  - Each OAuth2 client is signed with its **own** key set (`.../oauth2/openid/<client>/public_key.jwk`,
    different `kid`s measured for `grafana`, `vault`, `hermes`).
  - Token exchange (`grant_type=urn:ietf:params:oauth:grant-type:token-exchange`) is
    **service-account only**: the subject token must be an API token, a user access token is rejected,
    and `audience` must equal the client name. There is no human token-exchange path today.
  - `jwtLegacyCryptoEnable` only switches RS256/ES256; it cannot move claims between token types.
  - **The gateway cannot enforce a token *class* (v1.7.0, source + schema).** `typ` is not validated:
    `at+jwt` appears nowhere in the crate, decoding is `decode::<Map<String, Value>>` (payload only,
    `http_jwt.rs:658`), and `Validation` is configured with algorithm, `nbf`, issuer, audience and
    `required_spec_claims` only. CEL sees just the claims: `Claims { inner, jwt }` materialises `inner`
    (`http_jwt.rs:511-539`), so the JWS header is not in the activation and no rule can require `typ`.
    `jwtValidationOptions.requiredClaims` does not help either - the schema states only
    `exp, nbf, aud, iss, sub` are enforced (others, incl. `iat`/`jti`, ignored; default `["exp"]`), and
    both Kanidm shapes carry all five. Consequence: ACCESS-vs-ID separation rests entirely on claim
    *content* CEL (`jwt.scope` string vs `jwt.scopes` array). Since both shapes are signed by the same
    per-client key set, a wrong-class token still passes signature/issuer/audience and is stopped only
    by that CEL - a heuristic about what the issuer happens to emit, not a cryptographic class proof.
    Measured in `e2e-authz.py` cases L (ID bearer -> 403) and M (ACCESS bearer -> 200).
- [ ] **Kanidm inventory / reuse decisions**: **no human scope grant ships in the foundation** - both
  charts render `kanidm.humanScopeMap: []`. An earlier draft wired `searxng-users` to `mcp_firecrawl`;
  that was withdrawn (review B2): "may search" does not imply "may fetch arbitrary URLs", and the group
  has not been confirmed for this purpose. Withholding a grant needs no approval, and the owner's
  preference is to reuse an existing group rather than invent one - naming that group is a separate
  gate on the *grant*, not on gateway behaviour, which the local suites prove with test-only scope
  fixtures instead. `mcp-gateway-agents` stays the service-accounts-only group (rendered with no
  members). Person membership stays manual (never CRD-managed) per `docs/user-guide/kanidm.md`.
- [ ] **Register the OAuth2 clients**: `mcp-gateway` (public, PKCE, loopback redirect) and
  `mcp-gateway-admin`. Until they exist, enabling any route would fail JWKS lookup with
  `nomatchingentries` - which is exactly why the foundation ships with `routes: []`.

**Dependencies:** None | **Acceptance:** Kanidm reuse decisions documented, agentgateway version pinned

## Task 1: Foundation (Gateway + Auth + NetworkPolicy)

**Implemented locally** (`apps/mcp-gateway/`, `apps/mcp-gateway-admin/`, uncommitted). One chart
per plane; both render fail-closed and both were booted with the pinned binary.

### 1.1 Chart Structure

- [x] **`apps/mcp-gateway/`** and **`apps/mcp-gateway-admin/`**: template-only charts on
  `app-template` 5.3.0 (`Chart.lock` + vendored dependency, `helm dependency build` cannot reach
  the internal chart repo from here). Extra templates only for what app-template does not create:
  ConfigMap (gateway config), Secret (session key), NetworkPolicy, Kaniop OAuth2 client + group.
  **Routes render from `backends.<name>` values**, so `helm template` output is what runs and the
  tests boot real chart output. `enabled: false` for every backend -> `routes: []`.
- [x] **Admin plane**: `nginx-internal` class (LAN + VPN reach), own Kanidm client
  (`mcp-gateway-admin`), own issuer/audience/JWKS, own ESO-generated session-key Secret (see
  Task 0 / B1 above), and **no egress path to
  any backend while its routes are disabled** - opening a route and widening egress is the same
  values edit, asserted by `chart-checks.py`.

### 1.2 Kanidm OAuth2 Client

- [x] **Chart-side shape**: public client, `allowLocalhostRedirect: true`
  (`http://127.0.0.1:15000/callback`), `strictRedirectUrl: true`, `scopeMap` = concatenation of the
  human map and the workload map, because Kanidm requires every requested scope to be inside the
  granted set (`openid email profile` are concatenated with the MCP scope).
- [x] **Audience decision**: `audiences: [<client name>]`, not the resource URL - measured token
  shape above. `resourceMetadata.resource` is still the public URL for RFC 9728 discovery.
- [ ] **Live registration** (pending, owner/identity agent): clients `mcp-gateway` and
  `mcp-gateway-admin` do not exist yet; scope grants `mcp_firecrawl` (and candidate `mcp_grafana`)
  must be issued through scopeMap group membership, never through `client_credentials` (which
  yields an unusable JWE for this purpose).

### 1.3 NetworkPolicy

- [ ] **Both directions**: gateway ingress only from its own ingress controller + Prometheus;
  backend pods get ingress only from the gateway workload (dedicated pod per backend,
  `namespace`/`workloadName`/`port` from values); gateway egress limited to DNS, Kanidm/JWKS and
  enabled backends. Service is ClusterIP: no NodePort, no load balancer, no hostPort. Ops access
  is a separate **port** (metrics 15020, readiness 15021) and `adminAddr: "off"`, verified: nothing
  listens on 15000.
  **Not safe as written - live result:** the policy set is self-consistent for pod-to-pod peers but
  the kubelet probe path is node-sourced and dies with any restrictive policy attached, so the pods
  never became Ready and the release was reverted. Re-land needs the node path expressed in a
  Cilium-native way and proven live (see the incident doc), not inferred from `helm template`.
  **Diagnostic A/B used `kubectl get --raw /api/v1/namespaces/<ns>/pods/<pod>:<port>/proxy/...`
  (apiserver pod-proxy), not a direct node/kubelet path; the source identity through the K3s proxy
  is unproven. Both Ingress and Egress were co-attached, so the neighbour comparison alone does not
  isolate which policy type drops the traffic. Native K8S NetworkPolicy typically leaves node-local
  traffic permitted, but Cilium's actual enforcement may differ (runtime 1.20.2 vs docs 1.20.4 patch
  difference). Prior stale-datapath incidents are hypothesis, not causal proof. Timeout alone is not
  proof of drop. No privileged capture or restart was performed. Do not recommend blanket
  host/remote-node allow; a future port-15021-only candidate, L3/L4 not HTTP-path claim.

### 1.4 Auth Validation + Discovery Tests

- [x] **Local, against chart-rendered configs** (`test/mcp-gateway/e2e-authz.py`, cases A-N):
  401 + `WWW-Authenticate` resource metadata for anonymous; 401 wrong audience; 403 missing or
  near-miss scope; 200 for a scoped caller; unmapped path 404; RFC 9728 metadata returns the exact
  issuer at the URL the gateway advertises; the backend always sees the pinned Host; and the admin
  delegation route forwards the caller's own credential.
- [x] **Header/credential boundary** (`session-isolation-probe.py`): the caller's backend-URL and
  credential-override headers never reach a backend, while an unnamed header does - so the deny
  list, not the gateway default, is the protection.
- [ ] **Live**: OpenCode discovery + PKCE against Kanidm, Hermes noninteractive SA token exchange,
  negative tests in-cluster. Requires the registered clients and owner-approved test method.

### 1.5 Initial Backend Routes DISABLED

- [x] Default render is `routes: []` (no JWKS fetch, no unauthenticated answer). Enabling a route
  additionally requires the Kanidm client and a scopeMap group, and is a deliberate values change.

### 1.6 Secrets + Observability

- [x] **Session key (review B1 corrected)**: the chart renders **no key material at all**.
  `templates/session-key-generator.yaml` (ESO `Password`, `encoding: hex`, `length: 32`,
  `secretKeys: [SESSION_KEY]`) + `templates/session-key-externalsecret.yaml`
  (`refreshPolicy: CreatedOnce`, `target.immutable: true`, `creationPolicy: Owner`) own the lifecycle,
  which is the repo's existing secret convention and is deterministic under repeated `helm template`.
  The previous shape (`templates/secret.yaml` with `lookup` + `randAlphaNum`) was **removed**: under
  ArgoCD `lookup` returns empty, so every render produced a different key, and the keep-annotation
  only orphaned stale Secrets instead of fixing the lifecycle.
  Verified against the installed operator, read-only: ESO **v2.14.0** (`ghcr.io/external-secrets/
  external-secrets:v2.14.0`), `passwords.generators.external-secrets.io` and
  `generatorstates.generators.external-secrets.io` CRDs present, `spec.refreshPolicy` is a string enum
  (`CreatedOnce|Periodic|OnChange`), `spec.target.immutable` exists, `dataFrom.sourceRef.generatorRef`
  exists, `Password.spec.encoding` enum includes `hex`, and the `external-secrets-controller`
  ClusterRole (bound to SA `external-secrets/external-secrets`) grants get/list/watch on `passwords`
  plus full access to `generatorstates`.
  Lifecycle read from source, not inferred: `shouldRefresh()`
  (`pkg/controllers/externalsecret/externalsecret_controller.go`) refreshes a `CreatedOnce`
  ExternalSecret only while `status.syncedResourceVersion` is empty or `status.refreshTime` is zero, so
  periodic ticks, `refreshInterval` changes and spec updates never regenerate the value;
  `target.immutable: true` blocks any later data rewrite.
  Deletion lifecycle, read precisely from the pinned controller: `creationPolicy: Owner` makes the
  operator call `controllerutil.SetControllerReference` (`:837`), so the target Secret carries an
  `ownerReference` to the ExternalSecret and **plain Kubernetes owner GC removes it when the ES is
  deleted** - that is the path that produces a fresh key, not a controller decision. `target.deletionPolicy`
  is a separate, unrelated switch: `cleanupManagedSecrets` returns early unless it is `Delete`, and the
  `Retain` branch (`:448`) only covers "provider returned no data", where the Secret is left as-is. No
  `GeneratorState` object is required by this shape: `CreatedOnce` + the validity check
  (`isSecretValid`, `:1300`) already keep the value stable, and deleting the Secret by hand is the other
  legitimate way to get a new key. Both cases invalidate in-flight MCP sessions - documented behaviour,
  not a rotation mechanism and not a self-heal. No live deletion was tested.
  Length arithmetic read from source: `generatePasswords()` hex-encodes the *generated characters*
  (`encodePassword([]byte(pass), encoding)`), so `length: 32` + `encoding: hex` yields 64 hex
  characters, and `http_sessionpersistence.rs` `hex::decode`s that to exactly the 32 bytes AES-256-GCM
  requires (wrong length = `invalid AES-256-GCM key length`). Entropy is honestly stated: the key block
  is 32 bytes but its entropy is that of 32 generated characters (~180 bits), not 256 bits -
  sufficient for session-ID confidentiality/integrity, which is all this protects; a full-entropy
  Vault-held value is the alternative if the owner ever wants one (no such key exists in Vault today,
  and creating one ad hoc would be the drift this repo avoids).
  Missing key fails closed: `SESSION_KEY` is a required `secretKeyRef`, so the pod cannot start before
  the Secret exists - no plaintext fallback, no silent base64 downgrade. Measured consequence of the
  key either way: read-only config mount + no key -> "MCP session IDs are not encrypted" + 104-char
  base64 IDs; with the key -> 208-char AES IDs
  (test/mcp-gateway/session-isolation-probe.py, both phases; the AES path is also exercised with a
  representative generated value).
- [x] **Kanidm service-account credentials**: Kaniop-managed Secret (no Vault round-trip). No
  secret value or personal identifier in git, rendered manifests, logs or docs.
- [ ] **Audit**: gateway logs are JSON at `logLevel: info`; backend attribution for delegation
  routes comes from the forwarded caller credential, not from a gateway-side identity claim.
  Loki/metrics wiring is unchanged (ServiceMonitor on the ops port only).

### 1.7 Single Replica

- [x] One replica per plane, and the session behaviour of that choice measured (see Task 4 for the
  session-identity result, which is the part that blocks Vault, not the replica count).

**Dependencies:** Task 0 | **Acceptance:** `helm template` deterministic, NetworkPolicy blocks
ordinary pods, auth tests pass, no direct backend exposure, backend routes disabled until
validation. Local part: met. Live part: needs the Kanidm clients plus owner approval.

## Task 2: Grafana MCP (standalone + caller-preserving delegation)

**Status:** candidate implemented in the admin chart and **disabled** (`backends.grafana.enabled:
false`). The existing Hermes sidecar keeps working; nothing about it is changed. Two owner
decisions and one test prerequisite are outstanding - see the gates at the end.

### Measured on the pinned versions (local containers: Grafana `13.3.0-distroless`,
### mcp-grafana `2.1.1`). The scripts named below - `grafana-jwt-mapping-probe.py`,
### `grafana-merged-keyset-probe.py`, `grafana-forwarding-probe.py`,
### `grafana-sub-alias-probe.py` - are local research probes: uncommitted, pending product review, and
### not part of the foundation. The findings stand as local evidence; none of them is live parity, and
### the merged-key-set finding among them is **retracted outright**: its fixture used one issuer for both
### clients, which hid the issuer-trust failure described in item 4 below.

1. **Attribution.** `[auth.jwt]` maps the account from `username_claim` / `email_claim`. A JWT with
   neither is rejected with `missing login and email claim in JWT` and **no user is created**
   (fail-closed). Kanidm's ACCESS token has neither claim, so the standard MCP bearer cannot be
   attributed by Grafana at all; the ID-token shape maps to the pre-existing user by
   `preferred_username` (same user id, no duplicate).
2. **Permissions stay Grafana's own.** With the live `skip_org_role_sync = true`, Grafana skips role
   extraction entirely: `role_attribute_path` is inert, and a token carrying an `editor` scope
   neither escalates nor demotes (measured: writes stay 403 for a Viewer). Delegation therefore
   preserves native Grafana permissions exactly - no shared Viewer/Editor fallback is needed or
   allowed.
3. **Audience.** v13.3.0 has **no `audience` option** in `[auth.jwt]`, so the live
   `audience = grafana` line is inert. Only `expect_claims` enforces `aud` (it accepts a string or
   an array). Measured: with `expect_claims`, a token for `aud: vault` → 401.
4. **Key sets and issuer trust - the merged-key-set idea is RETRACTED, not pending.** Each Kanidm
   OAuth2 client has its own signing key and its own `iss`, so an admin-plane or gateway-plane token does
   not verify against the `grafana` client's JWKS (401). A later probe run against real Grafana `13.3.0`
   established why the workaround cannot work as previously claimed:
   - one `expect_claims.iss` value is a single exact string, so pinning the `grafana` issuer **rejects
     the second client's tokens** (their `iss` differs);
   - supplying `iss` as an array makes Grafana **fail at startup**, so there is no "pin both issuers"
     shape;
   - merging both key sets into one `jwk_set_file` **without** pinning `iss` accepts any token signed by
     any key in the file, from **any** issuer: signature trust without issuer trust. That is unsafe, not
     merely unreviewed.
   The only shape Grafana `13.3.0` natively supports here is **one client - per-client JWKS plus exact
   `iss` and `aud`**. Consequence, stated plainly: forwarding the gateway's own ACCESS token to the
   Grafana instance that already trusts the `grafana` client is **blocked**, because compatible
   dual-client trust does not exist in this version. The design keeps two independent clients, two `iss`,
   two `aud`, two JWKS URLs, and renders **no** merged key set anywhere. Re-land or enablement work must
   not resurrect a merged `jwk_set_file` as an option; cross-client Grafana trust needs an upstream
   capability (per-client trust, or multi-key sets that keep the issuer-to-key association), not a
   config trick.
   **Related hazard, measured (a precondition, not a config to ship).** With `allow_insecure_email_lookup
   = true` (the live committed value, unchanged by this work and not approved to change), Grafana attaches
   a JWT alias to an **existing** account found by email, so a token carrying a matching email claim from
   any issuer/key that Grafana trusts lands on that person's account. The probe that measured this used an
   **issuer-signed** token - the email claim comes from the IdP and is signature-verified, not
   unauthenticated user input - and **no existing compromise is alleged or observed**. Documented
   consequence: **never enable a Grafana delegation or bootstrap route while that setting is `true`**, and
   the issuer-trust question above has to be resolved first.
5. **mcp-grafana trust boundary.** It forwards the caller's `Authorization` to Grafana verbatim when
   `GRAFANA_FORWARD_HEADERS` names it. A caller-supplied `X-Grafana-Service-Account-Token` or
   `X-Grafana-API-Key` **replaces** the Grafana credential, `X-Grafana-Org-Id` switches org,
   `X-Grafana-Id` is ignored, and `X-Grafana-URL` is refused at `initialize` unless
   `GRAFANA_ALLOW_URL_OVERRIDE` (+ `GRAFANA_ALLOWED_URLS`) is set. With no credential at all it still
   calls Grafana **unauthenticated** - the backend is not fail-closed, so the gateway must supply the
   caller's credential and must strip every override header (both are chart invariants).

### Candidate shape (rendered, disabled)

- **Public-plane** route `/grafana/mcp` (`apps/mcp-gateway`) with `credential: accessToken` → authorization on the ACCESS token's
  `scope` string (`jwt.scope.split(' ')`) plus `backendAuth.passthrough`, so Grafana receives the
  caller's own bearer. Verified by `e2e-authz.py` cases K-N (anonymous 401, public-plane token 401,
  ID-token bearer 403 on this route, ACCESS bearer 200 forwarded verbatim, near-miss scope 403,
  every injected override header stopped, Host pinned).
- Backend pod (proposed `apps/grafana-mcp`, **not created unilaterally** - coordinate with the
  Hermes owner): `mcp-grafana:2.1.1` pinned by tag and digest, `-t streamable-http`, fixed
  `GRAFANA_URL`, `GRAFANA_FORWARD_HEADERS=Authorization`, **no** static service-account token,
  `--usage-stats=disabled`, no URL override.
- Live Grafana change: **there is no safe config-only shape today.** Per-client JWKS with exact `iss`
  and `aud` covers a single client, and the merged `jwk_set_file` + `expect_claims` idea is **retracted
  as unsafe** (dropping issuer trust is the failure mode, and pinning both issuers is not expressible -
  see the key-sets item). Direct gateway-ACCESS delegation to the live instance therefore stays
  **blocked** until compatible dual-client trust exists upstream; owner approval cannot conjure it into
  existence. Separately, `auto_sign_up` (live `true`; a valid-but-unmapped identity would create a user)
  and `allow_insecure_email_lookup` (live `true`) are both owner decisions that must be settled before any
  delegation route is ever enabled, and this work changes **neither**.
- Never in scope: new UUID-named users, overwriting existing permissions, user or role mutation,
  shared credentials. (13.3.0 does not even expose `PUT /api/org/users/{id}/role`.)

### Why the bearer shape needs an owner decision

The edge bearer is the Kanidm **ACCESS token only** - an ID token is not an acceptable edge
credential for this project, so the candidate above cannot be built on one. That collides with
measured Grafana behaviour: `[auth.jwt]` attributes a caller from `username_claim` / `email_claim`,
and Kanidm v1.11.2 puts no login claim into an ACCESS token (`claim_map` and `jwtLegacyCryptoEnable`
do not move claims into access tokens; there is no human token-exchange; ID-JAG `CrossAppAccessAuth`
exists in v1.7.0 but needs IdP support Kanidm does not have). Measured, on the live claim name
(`username_claim = preferred_username`, `grafana-jwt-mapping-probe.py`, a local uncommitted probe): an ACCESS-token bearer is
declined for having no login/email claim and **creates no user** - correct fail-closed behaviour.

That is not the whole answer, and the newer probe narrows the gap considerably: with
`username_claim = sub` the ACCESS token *does* identify someone (Kanidm's `sub` is the account UUID),
it just identifies a person Grafana has never bound (see option (d) below, measured
`grafana-sub-alias-probe.py`, a local uncommitted probe).

This is an identity-layer gap, not a gateway gap, and it is **not** to be solved by changing the edge
credential. The owner's options:

- (a) **Change the IdP configuration/product** so the ACCESS token carries a stable login claim
  (per-client claim map, a different issuer, or an upstream feature). Cleanest downstream, touches
  every other ACCESS consumer.
- (b) **Dual-credential**: the MCP session authenticates with the ACCESS token at the edge, and the
  gateway forwards a *separate*, per-caller ID-token-shaped credential that Grafana maps. v1.7.0 has
  no mechanism to mint a second credential per route - it can only passthrough the bearer it validated
  - so this needs an upstream feature, and it widens the accepted bearer shapes.
- (c) **Keep the existing sidecar/service-account flow for humans** and delegate nothing yet. The
  sidecar already works; the gateway route stays disabled. No identity change, no new attack surface.
- (d) **`username_claim = sub` plus a one-time identity-claim bootstrap per person.** The gateway
  keeps forwarding the caller's own ACCESS token; Grafana maps on `sub` (the Kanidm account UUID, the
  same value in every token class and stable across renames); and a person who already exists as an
  OAuth user is linked by presenting ONE token that also carries the identity claims, which attaches
  the `('jwt', sub)` connection to that existing user.

**Option (d) has a LOCAL positive only - native secure integration is still pending**
(`grafana-sub-alias-probe.py`, a local uncommitted probe; 16/16 assertions on a fixture, disposable Grafana `13.3.0-distroless`
with its own SQLite file, synthetic identities only, real generic-OAuth authorization-code flow against
a local Kanidm-stub issuer - the live instance was never touched). **What that fixture does NOT
establish**: it served BOTH planes from a single issuer, so it does not exercise the canonical
per-client `iss`/`aud` separation this release keeps (two independent Kanidm clients, two issuers,
two JWKS URLs). Because the alias bootstrap was tested against a single issuer for both planes, it
resolves neither open question at the Grafana trust boundary: **issuer trust** (answered negatively since -
a merged key set is unsafe and dual-client trust is unsupported) and **alias lifecycle** (how a
`('jwt', sub)` binding is created, re-pointed and revoked over time, which the fixture never exercised).
A positive bootstrap on a fixture is therefore not delegation support, and the Grafana-side audience
behaviour beside it was printed without assertions, so it is an observation, not a verified negative.
**The alias bootstrap is local positive but unapproved while the live settings
`allow_insecure_email_lookup=true` and `auto_sign_up=true` remain in place.** The local fixture
measured that a foreign `sub` carrying an existing email claim attached an existing JWT alias; **no
real compromise is alleged or observed**, and the live settings are untouched by this work.
Reviewer-flagged and recorded here so nobody reads the 16/16 as production integration proof:

| step | measured on 13.3.0 |
| --- | --- |
| pre-existing user, OAuth module only | `user_auth = ('oauth_generic_oauth', <oauth sub>)`, native role Editor |
| sub-only ACCESS JWT before any binding | 401 `user.sync.signup-disabled` (`auto_sign_up=false`); **no user and no `user_auth` row created** |
| one bootstrap JWT (`sub` + `preferred_username` + `email`) | 200, and `('jwt', sub)` is attached to the **same userId** - no second user |
| sub-only ACCESS JWT after the bootstrap | 200, resolving that same existing user |
| native RBAC | still Editor (`skip_org_role_sync=true` keeps it even though `role_attribute_path` evaluates to Viewer), and the JWT-authenticated identity **can exercise** Editor: `POST /api/dashboards/db` → 200 with no session cookie in the request |
| unknown `sub`, `auto_sign_up=false` | refused, no user invented |
| unknown `sub`, `auto_sign_up=true` | **invents a user whose login is the uuid** → `auto_sign_up` must stay `false` |
| token whose `aud` is another client | still authenticates the mapped user → Grafana checks no audience **value** without `expect_claims`; the class gate stays at the gateway |
| `aud` encoding | a flat string and a flat array behave identically (401 `user.sync.signup-disabled` for both) |

Two honest limits on this table. First, the OAuth user is matched by Grafana's own login rule, which
binds `('oauth_generic_oauth', <oauth sub>)` and stores the **e-mail** as the login, so the bootstrap
token must carry the claim values Grafana's JWT client looks up for `username_claim=sub` to land on
that row - measured working here with `sub` + `preferred_username` + `email` in one token. Second,
this proves the *identity* mechanics on a fixture: the live two-person parity gate below (different
native rights, real Kanidm issuer, the real `grafana-mcp` backend behind the rendered route) is still
open and still owner-gated, so the route stays disabled.

No live Grafana change is applied by this work, and none is currently available that would make
delegation safe: per-client exact `iss`/`aud` covers one client only, the merged key set is **retracted
as unsafe**, `allow_insecure_email_lookup` is `true`, and the per-person bootstrap is local proof only.
Grafana delegation therefore stays **blocked**, not merely pending approval.

### Costs of the two-token alternative (b) - measured, not accepted, not enabled

The owner's preferred design is the simple one: the caller's **ACCESS token** is validated at the edge
and forwarded verbatim in `Authorization` (`backendAuth.passthrough`), and Grafana attributes the
request to that user natively. That is what the shipped candidate renders and what `e2e-authz.py` M/N
prove end-to-end. Its single defect is upstream of the gateway: Kanidm puts no login claim in an ACCESS
token, so Grafana has nothing to map **under the live `username_claim = preferred_username`** (measured
401 `missing login and email claim in JWT`). Option (d) above removes that premise: with
`username_claim = sub`, the ACCESS token is enough on its own after a one-time bootstrap, so (b) buys
nothing that (d) does not already provide - and it costs all of the following.

Option (b) - a second, ID-token-shaped credential alongside the ACCESS bearer - is **not** a small
extension of that design, and this work does not treat it as equivalent or seamless:

1. **The gateway cannot mint it.** v1.7.0 offers `backendAuth.passthrough` (forwards the bearer it
   validated) or a static per-route credential (`signJwt`/`apiKey`); a *second* per-caller credential
   obtained from the IdP is not a mechanism that exists, and a static credential is precisely the
   shared-identity fallback that is out of scope here.
2. **It needs an allow-in path that is the opposite of the shipped invariant.** The charts strip
   caller-supplied credential and target-override headers case-insensitively; a second credential
   header would have to be *permitted* through, weakening a rule that cases N and the session probe
   currently demonstrate holds.
3. **It changes Grafana instance-wide, not route-wide.** Grafana reads its JWT from one header
   (`[auth.jwt] header_name`). Pointing that at a non-standard header - since `X-Grafana-Id` is ignored
   by mcp-grafana (measured) - makes the whole instance trust a header that every existing and future
   client can attempt to set, rather than confining the exception to the MCP route.
4. **The token-exchange path that could produce it is service-account-only** (Kanidm v1.11.2), so there
   is no human flow to build on.

Verdict: (b) is recorded as an option with concrete costs, the shipped route stays `credential:
accessToken` and **disabled**, and no plane change, header allowance or Grafana setting has been made
on its account.

### The delegation chain that does work (mcp-grafana 2.1.1, read in source)

The backend itself needs **no static credential** for caller-preserving delegation, which is what
makes option (b) affordable at all. Chain, with the files read at the pinned tag:

1. `caller_auth.go` - `RequireBearerToken` deletes the caller's `Authorization` after the MCP server
   has validated it, so the inbound bearer is not the Grafana credential by default.
2. `main.go:1187` - the server **refuses to start** if `--server-auth-token` is set while
   `GRAFANA_FORWARD_HEADERS` forwards `Authorization`: the two mechanisms are mutually exclusive by
   design, which is the guard that stops a static service account silently shadowing a caller.
3. `mg.go:157` / `mg.go:651` - forwarded headers are attached to the outbound request as extra
   headers.
4. `mg.go:736` - `AuthRoundTripper` then **overwrites** `Authorization: Bearer <apiKey>`, where the
   key comes from `mg.go:215` `apiKeyFromHeaders` = `X-Grafana-Service-Account-Token`, else
   `X-Grafana-Api-Key`.

Consequences built into the chart invariants: delegation needs neither a server token nor a static
env token; the caller's bearer travels in `Authorization` and survives only if no override header is
present; therefore the gateway **must strip** `x-grafana-service-account-token`, `x-grafana-api-key`,
`x-grafana-org-id`, `x-grafana-url` and friends case-insensitively (measured stopped in `e2e-authz.py`
case N), and must supply the caller's own credential via `backendAuth.passthrough` - because with no
credential at all mcp-grafana calls Grafana unauthenticated rather than failing closed.

### Two-user parity test design (prerequisite, not yet runnable)

A gateway 403 proves only the gateway's CEL, nothing about backend RBAC, unless the two callers are
*both allowed by the gateway* and differ *only* in what Grafana itself permits. Design:

1. Two distinct human Kanidm accounts, already existing Grafana users (no new users), both members of
   the group that grants the `mcp_grafana` scope, and both able to produce a bearer the route accepts.
2. Different **native** Grafana rights, e.g. one Viewer and one Editor, set through Grafana's own
   normal process - not by this work, and never by editing the other's permissions.
3. Same tool, same server, same session semantics, called as A then as B:
   - read-only tool (list datasources / search) → both succeed, and the audit shows each as **its own**
     Grafana user id, with A's session id never appearing in B's request path;
   - a write tool (create/update in a scratch folder) → the Writer succeeds, the Viewer is refused
     **by Grafana**, and the refusal is a Grafana 403 (not a gateway 403);
   - cross-replay: A's `Mcp-Session-Id` presented with B's bearer must not resume as A
     (`session-binding-probe.py` shows what a leak looks like: B served with A's cached credential).
4. Pass condition: every result matches what the same user could do directly in the Grafana UI, and
   no third identity appears anywhere. Any escalation, any shared identity, any session reuse between
   the two callers is a fail and keeps the route disabled.

Simulated or single-identity runs do not satisfy this gate.

### Gates before this goes live

- Test prerequisite: **two distinct human Grafana identities with different native rights** (none
  supplied; results must not be simulated).
- Kanidm `mcp-gateway` (public-plane) client registered and `mcp_grafana` granted via a scopeMap group.
- Owner decisions on `auto_sign_up` and `allow_insecure_email_lookup` (both live `true`, both untouched
  by this work), plus an upstream answer on dual-client trust - owner approval alone cannot make a
  merged key set safe.
- Only after a passing parity test: add the standalone backend, migrate one client, then remove the
  Hermes sidecar.

## Task 3: Firecrawl MCP

**Status:** chart and gateway route implemented locally; the route ships **disabled**. An
independent, fetcher-only rollout of `apps/firecrawl/` is owned by another agent (approved by the
owner) - this section is only about the MCP path.

- [x] **`apps/firecrawl-mcp/`**: dedicated pod, upstream `firecrawl-mcp` pinned by tag **and
  digest**, amd64 `nodeSelector`, probes, no API key (the in-cluster Firecrawl API is
  unauthenticated), `FIRECRAWL_MCP_ALLOWED_HOSTS` matching the pinned `hostHeader`.
- [x] **Route shape**: `/firecrawl/mcp` on the public plane with `credential: none` - the fetcher
  needs no user credential, so the caller's bearer is stripped at the edge and no `backendAuth` is
  rendered. Whole-backend scope `mcp_firecrawl`, no per-tool rules.
- [x] **NetworkPolicy**: MCP listener ingress from the gateway workload only, in its own namespace.
- [ ] **Group permissions (owner decision)**: which human group may use web access;
  `mcp-gateway-agents` is the service-account group (created empty; a service account joins in the
  same commit that enables its runtime).
- [ ] **SSRF bounds - OPEN BLOCKER, attempted in the cluster and rolled back.** The fetcher egress
  policies that would carry these bounds were committed and pushed (`0b16b31b9`), auto-deployed by
  ArgoCD, and within ~26 s Firecrawl's `api`, `worker` and both `nuq` pods went `NotReady` on probe
  timeouts (`browser` stayed healthy). A scoped revert (`8fff4f224`) restored a healthy baseline with
  all three policies absent. **Root cause unknown**: the aggregate denied-packet counter read `0`,
  which proves nothing because it is cumulative and cannot be attributed to a namespace, policy or
  port; the live `apps/firecrawl/hack/verify-egress.sh` check was **never run**, so no SSRF destination
  was ever demonstrated blocked; and the stale-Cilium-datapath look-alike in
  `docs/troubleshooting/cilium-stale-bpf-egress.md` produces the same fingerprint (egress outage, no
  deny events) when a policy is attached. Static review of the reverted rules does show at least one
  configured dependency with no rule covering it (`SEARXNG_ENDPOINT`, another namespace, port 8080).
  Full record and the checks required before any re-attempt:
  `docs/troubleshooting/mcp-firecrawl-egress-regression.md`. No further canary or re-land is
  authorised.

Two independent reasons keep this route disabled, and they must not be collapsed into one:

1. the **fetcher egress/SSRF gate is unresolved** (above) - gateway-side NetworkPolicy only limits who
   may reach the MCP pod directly, it cannot replace destination control inside the fetcher;
2. the **group decision** (who may use web access at all) has not been made.

Gateway authentication passing its own tests does **not** make this backend onboardable, and the
platform is not end-to-end ready while either reason stands.

**Dependencies:** Task 1 | **Acceptance:** route enabled only after the Kanidm client exists, the group
decision is made **and** the egress gate is demonstrated live. **Owner approval gate** before public
access.

## Task 4: Vault MCP (private, phased) - BLOCKED, route stays disabled

**Status:** `backends.vault.enabled: false`. No Vault route, scope or group exists. The blocker is
precise and measured on the **official product** (item 7 below), and the zero-custom-code path is
identified: option (a) works locally with two deliberate deviations that are the owner's call - a
per-route `x-vault-token` allow and `statefulMode: stateless`. So this is "blocked on an approved
configuration change", not "unknown".

### Corrected findings (these supersede the earlier spike notes)

1. **Native-token candidate, no new IdP plumbing.** The caller completes their **existing native
   Vault OIDC login** and presents their own `X-Vault-Token` next to the edge Kanidm JWT. Vault's
   own ACLs decide and Vault's own audit log records the real caller. That needs **no** new Vault
   JWT mount, **no** alias mapping, **no** policy change, no shared `VAULT_TOKEN` env, and no
   gateway-side credential injection. `Authorization` passthrough into Vault is **inert** (vault-mcp
   does not authenticate to Vault with the bearer), so this must not be described as seamless
   delegation.
2. **`auth/jwt/login` conversion is a different, optional path.** Vault's JWT login answers with
   `client_token`, while agentgateway's OAuth-shaped response carries `access_token`; converting one
   into the other is not supported by the pinned pieces and is not assumed anywhere.
3. **Human access tokens carry no email and no groups** (verified against the pinned Kanidm).
   Requesting the `email` scope is not evidence that it lands in an access token. The existing native
   OIDC path sidesteps this; a new JWT mount would create `sub`-UUID aliases different from the
   current `user_claim = email` mappings, which is a reason not to build it. Existing mappings and
   policies are not to be mutated.
4. **The hard two-user gate is session identity, and v1.7.0 has no native per-caller binding.**
   vault-mcp v0.2.0 caches the Vault credential by MCP session (the upstream `tokenHash` protections
   are unreleased, not in the tag). agentgateway re-checks a resumed session only against the issuing
   backend and the target conditions (`mcp/session.rs`: `get_session` / `get_or_resume_session`;
   `mcp/upstream/mod.rs:545`: `conditions_match`) - never against the caller's identity, and
   `MCPSessionState` stores no identity at all (`http/sessionpersistence.rs:31-49`). Encrypting or
   shortening the session ID does not change this: it protects the value, not who may use it.
   Measured against the chart-rendered route with a session-caching stub
   (`test/mcp-gateway/session-binding-probe.py`):
   - baseline (stateful, one target): caller B replaying A's `Mcp-Session-Id` **is served with A's
     cached Vault token** → gate broken;
   - `statefulMode: stateless`: no client session is issued and each request opens its own upstream
     session, so the backend acts on the credential presented with that request → **satisfied**;
   - per-caller target `condition: jwt.sub == ...`: cross-caller resume is refused
     (`404 session not found`) and another caller reaching a namespaced tool name errors out (measured
     `500` / JSON-RPC `-32603`, not a clean `403`) → **satisfied**, but tool names become
     `caller-a_<tool>` and it needs one static target per
     identity.
   For the routes this foundation ships (per-request credential via `backendAuth.passthrough`), the
   replay is harmless because the request carries its own bearer - measured in
   `session-isolation-probe.py` in both AES and base64 session modes.
5. **Query and header boundary (measured).** A request to `/firecrawl/mcp?vault_addr=...` reaches the
   backend as plain `/mcp`: the query string is dropped because the upstream host and path are fixed
   in config. That removes vault-mcp's `vault_addr` query SSRF **for gateway traffic**, but it is an
   observed property of the pinned gateway, not a Vault fix - so it is asserted in tests and will
   fail loudly if it ever changes. Every route's deny list strips `vault_addr`, `vault-addr`,
   `vault_token`, `vault_skip_verify`, `vault-skip-verify`, `x-vault-namespace`, `vault_namespace`
   and `vault-namespace` (both spellings; HTTP header names are matched case-insensitively).
   `x-vault-token` is stripped plane-wide today as well: a real Vault route must instead pass the
   caller's **own** token through, which needs an explicit per-route allow - deliberately not built
   while the route is blocked.
   **CRITICAL operational finding:** agentgateway info logs the full request path including the query
   string. The probe observed a synthetic credential in `?VAULT_TOKEN=`. Durable rule: **never place
   credentials in query strings; gateway logs are a credential-bearing surface; caller `X-Vault-Token`
   header only; any future route must redact/avoid secrets in URLs.** No real credential or user data
   was exposed; synthetic fixture only.
6. **Session protection is required but is not the fix.** `SESSION_KEY` must be a real AES key from
   the chart-generated Secret (never `session.key: disabled`, never a committed literal), because
   without it the pods run base64 session IDs and lose even the issuing-backend guard. That still
   does not satisfy the two-user gate - do not mark Vault safe because sessions are encrypted or
   time-limited.
7. **The official product was run behind the pinned gateway - measured support for option (a)**
   (`vault-official-stateless-probe.py`, a local uncommitted probe, re-run after review: **28/28
   REQUIRED held, 4/4 known-bad CONTROLS reproduced, 12 OBSERVED, 2 NOT-TESTED** (live two-human
   A/B; production per-route `X-Vault-Token` allow not wired). These counts replace the earlier
   "24 cases" framing; the security negatives are still **incomplete**, so nothing here is a pass
   claim. `hashicorp/vault-mcp-server:0.2.0`
   digest `sha256:e4e8df2fb7d27f12c5cb8d1c5b37663a9569ad5ce0047fa0281c56a1769222aa`, a `hashicorp/vault:1.15`
   dev fixture, agentgateway `v1.7.0` with the chart-rendered config). Mock backends are not product
   proof, so this used the real server, with two ACL identities and **no ambient `VAULT_TOKEN`** in
   either backend container (`middleware.go:133-152` reads `VAULT_ADDR`/`VAULT_TOKEN`/`X-Vault-Token`
   header -> query -> env; `client.go:110-118` falls back to env and otherwise errors
   `vault token not provided for session`):

   | configuration | result |
   | --- | --- |
   | shipped route as-is (stateful default, `x-vault-token` stripped) | the server reaches Vault with no credential -> `Failed to get Vault client: vault token not provided for session`. Today's chart **cannot** carry a per-caller Vault token: item 5 confirmed on the real product |
   | stateful + a token the backend can actually use | the Vault client is cached **per MCP session** (`GetVaultClient(session.SessionID())`): credential rotation is ignored for the life of the session and the session id alone behaves like a bearer credential; a session id minted by one instance is accepted by a **second** instance -> stateful cannot carry per-caller secrets |
   | `statefulMode: stateless` + `x-vault-token` **not** stripped (both TEST-ONLY deviations) | native ACL isolation is correct end to end: A reads `secret/a` + the canary and is **denied** `secret/b`; B reads `secret/b` + the canary and is denied `secret/a`; a valid token with no ACL is denied through the gateway; no `no active session` failure appears; the edge still answers 401 `{"error":"unauthorized","error_description":"JWT token required"}` with no bearer, and the AES `SESSION_KEY` stays active |
   | caller-supplied backend address | the product **honours** `VAULT_ADDR` (header and `?VAULT_ADDR=`) and **refuses** `?VAULT_TOKEN=` with its own 400; the shipped strip list blocks the address override, so the gateway - not the backend - is what keeps the Vault endpoint fixed |

   Two notes for whoever eventually enables this. The usable ACL shape is "the caller's own path + the
   mount's metadata + `sys/mounts`": `read_secret` lists mounts first, so a policy narrowed to a single
   path is denied for a reason unrelated to delegation, and a root-equivalent token stays
   root-equivalent through the gateway. And the two deviations above are exactly the owner decision in
   option (a) - a per-route `x-vault-token` allow plus `statefulMode: stateless` - so they are **not**
   applied to the charts and the route stays disabled. Local Vault is 1.15 (the only image available
   here), so this proves ACL/session semantics on a local dev fixture - **not** version parity with the
   production Vault, and not a complete isolation proof: cross-caller session-id swap and credential
   rotation under `stateless` were not both exercised as asserted negatives.
   **TLS now has a real positive (valid-CA success) and a real negative (untrusted-CA exact x509
   failure with connection evidence), but this is still LOCAL/test-only and no route is ready.**
   Still open: the route patch is test-only (the per-route `x-vault-token` allow is not wired in
   production shape) and no human A/B run exists. Read the stateless ACL result as a LOCAL positive
   that supports investigating option (a), never as approval to enable the route.

### Options for the owner (cheapest first)

- (a) **stateless + per-request native token**: `statefulMode: stateless` on the route, `x-vault-token`
  allowed for that route only, fixed Vault address/TLS in the backend env and no token env.
  **First product run done (item 7): the ACL isolation works and vault-mcp 0.2.0 tolerates a fresh
  upstream session per request.** What is still needed to ship it: a per-route **allow** mechanism,
  because `backends.<name>.stripRequestHeaders` is additive-only today - the plane default strips
  `x-vault-token` and a route cannot un-strip it; the stripped set must keep `vault_addr` /
  `vault-addr` (measured: the product honours a caller-supplied address, header and query), and an
  MCP-client check that a server issuing no session id is accepted. The live two-user test stays.
- (b) **upstream session-identity binding** (or a backport of the unreleased `tokenHash` work), then a
  stateful route is safe.
- (c) **stay blocked**. No route, no scope, no group until (a) or (b) is approved.
Not on the table: a custom auth broker, monkeypatching the backend, per-tool gateway rules, or a
shared admin credential.

**Dependencies:** Task 1 | **Acceptance:** one of (a)/(b) chosen by the owner, two-user isolation
proven live, no direct ingress to the backend. **Owner approval gate** before deploying.

## Task 5: Migration (OpenCode / Hermes)

NOT STARTED

- [ ] **OpenCode migration**: one instance at a time. Update user config MCP URLs to point to gateway. Test OAuth discovery → PKCE → access token → gateway → backend
- [ ] **Hermes migration**: one instance at a time, start with first (has sidecar). Update config: MCP URLs → gateway. PVC persistence (`docs/troubleshooting/hermes-config-overwrite-recovery.md`), rollback: switch to previous gateway or pause before exclusivity cutover
- [ ] **Future (TODO only)**: SearXNG/Context7 onboarding (after core MVP), HA, Playwright, Postgres

**Dependencies:** Tasks 2-3 (Vault validated in Task 4 parallel; no need to block regular migration on Vault, but overall MVP includes Vault) | **Acceptance:** OpenCode/Hermes migrated one at a time, rollback tested. **Owner approval gate** before each instance migration

## Tests

**Status:** the local suites are written and executed against the pinned binary/images and the actual
chart-rendered configs. Cluster-level verification has **not** run (no gateway/Kanidm resource exists;
live checks need an approved deployment).

Executed locally (all green, 2026-11-08). The files in the paths below are the local working-copy
test suite: after the first-deploy revert they are **not committed**, so treat each path as local
evidence until the foundation re-lands:

| suite | what it proves | result |
|---|---|---|
| `local-render.sh` (local runner, **not committed**: as written it trips the repo's shellcheck pre-commit hook - SC2155 at line 50 plus three SC2015 infos - so it stays out of the foundation until fixed) | renders both charts, boots the pinned v1.7.0 binary per plane: readiness on 15021, metrics on 15020, nothing listening on 15000, `404` on every backend path with `routes: []`, AES session active | 19 pass / 0 fail |
| `test/mcp-gateway/chart-checks.py` | structural invariants on the rendered objects: fail-closed default, per-plane issuer/audience, CEL shape per `credential`, `backendAuth.passthrough` placement, no `session:` block in config, egress rules absent while disabled and added per enabled backend, ClusterIP only with separate metrics/healthz ports, every shipped backend disabled, no backend *setting* `credential: idToken`, scopeMap/groups render, plus review B1/B2/B3: the charts render **no Secret** and no `randAlphaNum`/`lookup` in template logic, the ESO `Password` + `ExternalSecret(CreatedOnce, target.immutable)` pair with a required `secretKeyRef`, byte-identical repeated renders, a 64-hex/32-byte key arithmetic check, `humanScopeMap: []` on both planes, Grafana's candidate on the **public** plane only, and no admin egress path to it | 106 pass / 0 fail |
| `test/mcp-gateway/e2e-authz.py` | cases A-N against the real gateway: anonymous 401 + challenge discovery, wrong-audience 401, missing scope 403, scoped 200 with the bearer stripped (`credential: none`), unmapped path 404, multi-scope string, near-miss 403, resource metadata at the advertised URL, pinned `Host`, AES session, then the admin plane: public-plane token 401, ID-token bearer 403, ACCESS bearer 200 forwarded verbatim, near-miss 403, and all injected credential/target-override headers stopped while a control header still passes | all passed |
| `test/mcp-gateway/session-isolation-probe.py` | SESSION_KEY present -> AES ids (208 chars, no warning); no key + read-only config -> base64 ids (104 chars) **with** the `MCP session IDs are not encrypted` warning; query params dropped; 12 override headers stopped; B replaying A's session id is served with **B's own** bearer in both modes | all passed |
| `test/mcp-gateway/session-binding-probe.py` | the Vault two-user gate, three shapes: baseline stateful/one target = **LEAK** (B served with A's cached credential), `statefulMode: stateless` = **OK**, per-caller `condition: jwt.sub` = **REFUSED** (`404 session not found`) | reproduced, gate verdict unchanged |
| `grafana-merged-keyset-probe.py` (local, uncommitted) | **result RETRACTED as unsafe, not pending review**: pinning `iss` rejects the second client, an `iss` array makes Grafana fail at startup, and a merged key set without `iss` pinning trusts any issuer signed by a listed key. The earlier reading ("each token mapped to its own existing
user; unlisted `aud` and a third key -> 401") came from a fixture whose single issuer masked exactly that
gap | retracted - unsafe, do not ship |
| `grafana-jwt-mapping-probe.py`, `grafana-forwarding-probe.py` (local, uncommitted, pending review), `backend-auth-probe.py` | claim-mapping and header-forwarding/overwrite behaviour of Grafana 13.3.0 / mcp-grafana 2.1.1 (Task 2 findings) | passed |
| `grafana-sub-alias-probe.py` (local, uncommitted, pending review) | **LOCAL fixture evidence for Task 2 option (d)** (single issuer for both planes, so no per-client `iss`/aud coverage): a real generic-OAuth authorization-code login against a local Kanidm-stub issuer (per-client JWKS, CA bind-mounted, synthetic identities only), then the JWT path on the pinned `13.3.0-distroless` image - OAuth-module binding and native Editor read straight out of the fixture's SQLite, sub-only ACCESS before/after the one-time identity-claim bootstrap, the `('jwt', sub)` row landing on the SAME userId, a JWT-authenticated dashboard write with no session cookie involved, `auto_sign_up` false vs true, unknown-sub refusal, and the audience-value gap that keeps the class gate at the gateway | 16 pass / 0 fail |
| `vault-official-stateless-probe.py` (local, uncommitted, pending review) | **LOCAL fixture evidence for Task 4** (21 REQUIRED + 4 CONTROLS + 11 OBSERVED + 1 NOT TESTED; security negatives incomplete, TLS not established), official `hashicorp/vault-mcp-server:0.2.0` (digest-pinned) behind the pinned gateway with a disposable `hashicorp/vault:1.15` dev fixture, two native ACL identities and **no ambient `VAULT_TOKEN`**: shipped route vs the two test-only deviations (stateful + `x-vault-token` unstripped, and `statefulMode: stateless`), cross-instance session-id acceptance, credential-rotation staleness, per-caller ACL isolation, `VAULT_ADDR` header/query override with a Vault-less canary listener, and the edge 401 without a bearer | 24 cases, 0 failures (13 assertions + 15 measured verdicts) |

Still open, and **not** satisfiable locally:

- [ ] Two-human parity run for Grafana delegation (design in Task 2) - needs two distinct identities
      with different native rights; no live AB run has been done.
- [ ] `mcp_firecrawl` / `mcp_grafana` scope denial and grant behaviour **in the cluster**, once the
      Kanidm clients exist.
- [ ] Direct-to-backend access denied from an ordinary pod (NetworkPolicy) - must be verified with a
      committed, approved test method, never ad-hoc `kubectl run/apply`.
- [ ] Public plane cannot reach admin-plane routes (verified here by audience separation locally; the
      real plane separation is an ingress/nginx + policy matter).
- [ ] Gateway outage produces no bypass; restrictive policies are not reopened by additive ones.
- [ ] Firecrawl: all live egress/SSRF assertions - see
      `docs/troubleshooting/mcp-firecrawl-egress-regression.md` gates.

Correction recorded after re-measurement: an earlier note in this plan said Grafana 13.3.0
"declines a token whose `aud` is a JSON array". That was **my own signer bug** (an `alg: RS256`
header on an ES256 key, plus a doubly-wrapped `aud` list), not product behaviour. Measured
head-to-head in `grafana-sub-alias-probe.py` case [2c], a flat string audience and a flat array
audience give the identical answer. The claims that survive are the two about the audience
*value*: 13.3.0 has no `audience` option in `[auth.jwt]`, and without `expect_claims` it checks
no `aud` value at all, so a token minted for another client authenticates the mapped user.

Test method note: everything executed above runs the pinned binary in the local session sandbox against
chart-rendered configs, so the assertions are about shipped output, not hand-written fixtures.

**Dependencies:** Tasks 1-4 | **Acceptance:** local suites stay green at commit time; cluster items
require an approved deployment.

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
