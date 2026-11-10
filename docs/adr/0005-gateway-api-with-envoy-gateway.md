# ADR-0005: Gateway API with Envoy Gateway instead of ingress-nginx

- **Status:** Accepted
- **Date:** 2026-11-10
- **Decision owners:** Homelab maintainers
- **Plan and execution record:** [ingress-nginx to Envoy Gateway migration](../../planning/ingress-nginx-to-envoy-gateway.md)
- **Operating docs:** [Ingress](../network/ingress.md), [Exposing an app with Gateway API](../conventions/gateway-api-routes.md)

## Context

Two ingress-nginx controllers (`nginx-internal` on `192.168.193.1`, `nginx-external` on
`192.168.193.4`) served every app. Three things pinned the cluster to them:

- **nginx-only behaviour.** Apps relied on `configuration-snippet` / `server-snippet` rewrites,
  redirects, per-app timeout annotations and `auth-snippet` skip paths. These live in
  annotations that only ingress-nginx understands.
- **Authentication through a shared oauth2-proxy.** One oauth2-proxy (`auth.grigri.cloud`) with one
  Kanidm client and a shared cookie on `.grigri.cloud` fronted every protected Ingress through
  `auth-url` / `auth-signin`.
- **The tenant platform.** The hosted tenant (Forkline Remote) needs Gateway API: experimental
  channel CRDs, a shared external Gateway with HTTPS listeners plus a TLS passthrough listener, and
  routes (`HTTPRoute`, `TLSRoute`) in its own namespaces.

Forgejo SSH (`29518`) and the public ports 80 and 443 reached the cluster through one pfSense alias
pointing at the nginx VIP.

## Decision

Replace both controllers and the shared oauth2-proxy with Envoy Gateway (Gateway API plus Envoy Gateway
policies). The plan records the decisions D1 to D9; in short:

1. **Side by side, two switches (D1).** Envoy Gateway ran next to nginx on new VIPs. Each app got
   its routes and was verified against the new VIPs while nginx served real traffic; then one
   internal switch (DNS) and one external switch (the pfSense alias plus three DNS Resolver host
   overrides). nginx stayed idle as the rollback path until both switches had soaked, then both
   controllers were removed together (D8).
2. **Two Gateways, two VIPs (D2).** `internal` on `192.168.193.13` (LAN) and `external` on
   `192.168.193.14` (public, behind the same pfSense alias). Forgejo SSH is a `TCPRoute` on the
   external Gateway and moves with the same alias. Each Gateway has its own `EnvoyProxy`, Deployment
   and Service.
3. **Native OIDC against Kanidm (D3).** Per protected route an Envoy Gateway `SecurityPolicy` does
   OIDC and a JWT check; oauth2-proxy is removed. Each app has its own Kanidm OAuth2 client, its own
   `<app>-users` group and its own claim (`access` = `[<app>]`); there is no shared client and no
   global group. Recipe: [Authentication](../conventions/gateway-api-routes.md#authentication).
4. **Port everything (D5).** Every nginx-only behaviour was ported and verified (rewrites,
   redirects, Lua `EnvoyExtensionPolicy`, backend TLS, skip paths); none was dropped. Snippets are
   translated by behaviour, never by tool.
5. **Same DNS mechanism (D6, D7).** external-dns publishes names to Cloudflare from annotated
   routes; the internal records point at `.13`.
6. **Cilium BGP, no ECMP (D9).** Both Services are `LoadBalancer` with
   `loadBalancerClass: io.cilium/bgp-control-plane` and `externalTrafficPolicy: Local`. Proxies are
   placed on `prusik`, so each VIP keeps one path as before; ECMP is not used.
7. **Tenant runners (D4).** The tenant runner wildcard was bridged to nginx by a TLS passthrough
   listener until the tenant emitted `HTTPRoute`s; the bridge was then replaced by a terminating
   runner listener.

## Consequences

Operating rules that follow from the choice (details in the linked docs):

- **Listener drains.** All HTTPS hosts of a Gateway share one Envoy listener. A new or removed
  `SecurityPolicy` or `EnvoyExtensionPolicy`, or a certificate change, replaces a filter chain and
  closes connections still open after the 60 s drain. `HTTPRoute`-only changes drain nothing. Batch
  listener-affecting changes and avoid them during long transfers:
  [listener drain](../troubleshooting/envoy-gateway-listener-drain.md).
- **Timeouts are idle, not total.** nginx timeouts were idle timeouts; Envoy's route request timeout
  is a total limit. Gateway defaults are `requestTimeout: 0s` and `streamIdleTimeout: 60s`; longer
  idle values go in a route `BackendTrafficPolicy` with `mergeType: StrategicMerge`, never in
  `timeouts.request`.
- **Request bodies are streamed.** Envoy does not buffer requests as nginx did, so a slow upload
  reaches the app as a slow upload. There is no body-size limit.
- **Underscore headers are dropped.** Request headers whose name contains an underscore do not reach
  the backend; an app that needs one has to rename it.
- **Cookie size limit for OIDC.** The Envoy OAuth2 session lives in cookies per host and browsers
  drop cookies above 4096 B. The `groups` scope puts every group in the ID token and overflows
  the limit, so scopes are `openid profile email` and authorization uses the per-app claim:
  [ID token cookie size](../troubleshooting/envoy-oidc-id-token-cookie-size.md). Moving from
  oauth2-proxy cost one extra login per app (new per-host cookies). Kanidm has no end-session
  endpoint, so logout clears only the Envoy cookies.
- **Gateway-wide settings must be explicit.** ALPN `[h2, http/1.1]` is pinned (hostname-scoped TLS
  listeners otherwise drop h2), HSTS is the only added header, route-level policies use
  `StrategicMerge`, and compression is per route and never on MinIO/S3 hosts (it breaks SigV4).
- **Certificates live on the Gateways** (`envoy-gateway` namespace, DNS-01). Routes carry no
  `cert-manager.io/*` annotation and apps create no TLS secrets. A route with an `.internal.` host
  must not reference the external Gateway.
- **CRDs.** Gateway API experimental channel, synced server-side. Argo CD has no health check for
  several of the kinds (`TCPRoute`, `ReferenceGrant`, `EnvoyProxy`, `HTTPRouteFilter`): read their
  status by hand.
- **NetworkPolicies must admit the Envoy proxy pods**, and egress toward them uses the container
  ports `10443` and `10080`, not `443` and `80`.
- **external-dns is upsert-only and opt-in** (`external-dns.kubernetes.io/enabled`); the external
  Gateway supplies the CNAME target. Changing a name's record type needs a manual Cloudflare edit.
- **No nginx rollback path.** The side-by-side rollback (pfSense alias and overrides back to the
  nginx VIPs) ended with the removal; restoring nginx means reverting the removal commits in git.

## Alternatives considered

- **Big bang, Envoy takes `.1` and `.4`.** No per-app testing before real traffic, one switch for
  everything, and rollback means reverting git and waiting for Argo CD while Argo CD, Vault and the
  identity provider may be unreachable. Rejected for the side-by-side strategy.
- **Keep oauth2-proxy behind Envoy `extAuth`** (`SecurityPolicy.extAuth.http`). It works and the
  tenant's own web auth uses it with its own oauth2-proxy (auth-only mode, so the browser sees
  oauth2-proxy's sign-in redirect, as Envoy `extAuth` has no `auth-signin` equivalent). Not chosen
  for the homelab apps: native OIDC removes oauth2-proxy and its shared Kanidm client.
- **A global Kanidm group on top of the per-app groups** (a `homelab-users` tier required by every
  policy). Not taken: per-app groups only, so Kanidm decides who may log in to each app and Envoy
  re-checks the same group.
- **Converting Ingresses with a tool (`ingress2gateway` and similar).** They silently drop auth and
  snippets; conversion is by behaviour, verified with `curl --resolve` against the new VIP.
- **BGP ECMP across nodes.** Not now (D9): placement keeps proxies on `prusik`, one path per VIP.
- **Standard-channel Gateway API CRDs.** Enough for `v1` `TCPRoute` and `TLSRoute`, but the
  experimental channel keeps serving `v1alpha2`, which the tenant still uses.
