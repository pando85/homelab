# Exposing an App with Gateway API (Envoy Gateway)

How an app gets an HTTP(S) route on the Envoy Gateways. This replaces the ingress-nginx
annotation and snippet guidance. Background, decisions and the migration state:
`planning/ingress-nginx-to-envoy-gateway.md` (the recipes there cover every nginx-only behaviour
that was ported).

## Migration state: all traffic on Envoy, nginx idle until Phase 6

All traffic is served by the Envoy Gateways: internal names resolve to `.13` since Phase 4
(2026-11-09) and public traffic reaches `.14` since Phase 5 (2026-11-10). ingress-nginx
(`.1` internal, `.4` external) still runs, idle, as a rollback path until Phase 6 removes it. Until
then an existing app may still carry its Ingress next to its route; never remove an Ingress as
part of adding a route (Phase 6 does that). A **new** app gets **only** a route: no Ingress, no
nginx annotations. After Phase 6 only routes remain. Health of the whole path, re-runnable and
read-only: `scripts/gateway-check.sh`.

## Gateways and listeners

Both Gateways live in namespace `envoy-gateway`. A route picks one with `parentRefs`
(`namespace: envoy-gateway`, `sectionName` from the table). VIPs: `.13` internal, `.14` external.

| Host pattern                | Gateway    | `sectionName`                 | Notes                                          |
| --------------------------- | ---------- | ----------------------------- | ---------------------------------------------- |
| `<app>.internal.grigri.cloud` | `internal` | `https-internal-grigri-cloud` | wildcard cert, LAN only                        |
| `<app>.grigri.cloud`        | `external` | `https-grigri-cloud`          | wildcard cert, public (behind pfSense NAT)     |

Other listeners (forkline.dev, mintpsicologia, passthrough, TCP) are in the listener contract of
the plan. All certificates live in `envoy-gateway`; an app never creates a Certificate or a TLS
secret, and routes carry no `cert-manager.io/*` annotation. A route whose host contains `.internal.`
must never reference the `external` Gateway (the host has no certificate there).

A host with more than one label below `internal.grigri.cloud` (today only `m.rajoy.internal` and
`api.m.rajoy.internal`) is not covered by the `*.internal.grigri.cloud` wildcard certificate: those
routes use the listener `https-rajoy-internal-grigri-cloud` (own certificate). Ask before adding more.

## Writing the route

Mirror the live Ingress hosts and paths exactly (`kubectl get ingress -n <ns> -o yaml`), including
backend Service and port.

### app-template apps (`route:` block)

```yaml
app-template:
  route:
    <name>:
      kind: HTTPRoute
      # annotations: only on external routes, see "DNS" below
      hostnames:
        - <app>.internal.grigri.cloud
      parentRefs:
        - name: internal
          namespace: envoy-gateway
          sectionName: https-internal-grigri-cloud
      rules:
        - backendRefs:
            - identifier: <service-identifier>   # key under `service:`
              port: <port number>                # a number, not the port name
```

Reference examples: `apps/http-echo/values.yaml` (internal and external), `apps/firecrawl/values.yaml`.
app-template renders `gateway.networking.k8s.io/v1` only when the API is in the Helm capabilities:
`scripts/deploy-dir.rh` passes it and Argo's own render gets it from the cluster. For a local
check add `--api-versions gateway.networking.k8s.io/v1/HTTPRoute` to `helm template`.

### Apps rendered by kustomize `helmCharts` (rsshub, kube-system)

`kustomize build` has no cluster capabilities, so do not rely on the chart's `route:`. Add a raw
`HTTPRoute` file to `resources:` (`apps/rsshub/resources/httproute.yaml`). The kustomization sets
the namespace; leave `metadata.namespace` out. Check with `kustomize build --enable-helm .`.

### Raw `HTTPRoute` in a wrapper chart (`templates/`)

Used by apps that need more than one object (`apps/unifi`, `apps/radarr`, `platform/vault`).
Plain YAML under `templates/`, `apiVersion: gateway.networking.k8s.io/v1`, no namespace.

### Upstream chart with native route support

Use the chart's own key where it exists (argo-cd, forgejo, kube-prometheus-stack, grafana, ollama,
open-webui, wordpress, oauth2-proxy; table with gotchas in the plan) and disable the chart's
`ingress` block **only when the Ingress is retired**. Until then both exist. Charts without route
support (cilium hubble UI, minio, loki, vault) get a raw `HTTPRoute`:
`system/kube-system/resources/cilium/hubble-ui-httproute.yaml` keeps `hubble.ui.ingress` untouched.

### Operator CRs that create the Ingress (vault)

The route is a standalone `HTTPRoute` next to the CR. Do **not** remove `spec.ingress` from the Vault
CR as part of adding it: the operator never deletes an Ingress it created, so removing the field
orphans the Ingress (Argo does not prune it). That deletion is an owner decision.

### gRPC and plain HTTP on one hostname

Gateway API accepts only one of a `GRPCRoute` and an `HTTPRoute` that share a hostname on the same
listener (the other conflicts and is not accepted). An app that serves both gRPC and plain HTTP
paths on one hostname uses a single `HTTPRoute`, with an h2c backend for the gRPC paths: set
`appProtocol: kubernetes.io/h2c` on the Service port.

## DNS (external-dns)

How names resolve (pfSense host overrides, NAT reflection, hairpin, the two VIPs) is in
[`docs/network/ingress.md`](../network/ingress.md). This section is only the per-route how-to.

external-dns publishes a route only when it carries the opt-in annotation
`external-dns.kubernetes.io/enabled: "true"`. The record target is not set on routes: the
**external Gateway** carries `external-dns.kubernetes.io/target: grigri.cloud` and the internal
Gateway has none.

- **External route:** annotation `external-dns.kubernetes.io/enabled: "true"` and nothing else (no
  `target`). The record is a **CNAME to `grigri.cloud`**. A route can never produce an A record on
  the external Gateway.
- **Internal route:** the same annotation; the record is an **A record to `.13`** (the Gateway
  address). Phase 4 annotated every internal route.
- **An A record on an external name** (for example a name that must resolve to a private IP, so it
  works from the LAN and the cluster only): a `DNSEndpoint` (the `crd` source), because an annotated
  external route would only produce the CNAME. Example: a tenant Keycloak host that publishes
  `192.168.193.14`.
- **A wildcard owned by someone else** (the tenant runner wildcard) is held by a `DNSEndpoint` of
  its owner: do not annotate routes for names under it.
- **upsert-only:** external-dns never deletes or reverts a record (records are created with
  `ttl=1`, auto). Removing the annotation or rolling a change back leaves the record. Moving a
  record back needs a manual Cloudflare edit **and** reverting the source, otherwise external-dns
  re-asserts it within its 2 minute interval.
- Verify no record changed: `kubectl logs -n external-dns deploy/external-dns --since=15m | grep -v 'already up to date'`.

## nginx annotation to Gateway API

The gateway-wide defaults match nginx (60 s idle timeouts and no total request limit, HSTS only,
`Server` removed, h2 and HTTP/1.0, no body limit, no response buffering, websocket upgrade enabled on
every route). An app only needs something extra when it relied on a non-default behaviour.

| nginx                                                              | Gateway API / Envoy Gateway                                                                                                         |
| ------------------------------------------------------------------ | ----------------------------------------------------------------------------------------------------------------------------------- |
| `proxy-body-size`, `proxy-buffer-size`, `proxy-buffering off`       | nothing (no body limit, streaming, larger header limits)                                                                             |
| `proxy-read/send-timeout` (idle between reads or writes)            | only when longer than 60 s: route `BackendTrafficPolicy` `timeout.http.streamIdleTimeout` with `mergeType: StrategicMerge`; see "Timeouts" |
| `proxy-connect-timeout`                                             | route `BackendTrafficPolicy` `timeout.tcp.connectTimeout`, only when it is not the 5 s default; see "Timeouts"                       |
| long uploads, downloads, streams, SSE, websockets                   | nothing while data keeps flowing (no total limit); **never** `timeouts.request` / `requestTimeout` (a total limit)                   |
| `backend-protocol: HTTPS`, backend CA available                     | `BackendTLSPolicy` (below)                                                                                                           |
| `backend-protocol: HTTPS`, self-signed backend, nginx never verified | Envoy Gateway `Backend` with `tls.insecureSkipVerify: true` (below)                                                                  |
| `rewrite-target`, redirects                                         | `URLRewrite` / `RequestRedirect` filters (a redirect rule cannot have `backendRefs`)                                                 |
| `configuration-snippet` / `server-snippet`                          | translate by behaviour (recipes in the plan); request-side query or header logic is an `EnvoyExtensionPolicy` with Lua (below)       |
| compression                                                         | `BackendTrafficPolicy` `compressor` per route, never on S3/MinIO hosts                                                               |
| `auth-url` / `auth-signin` / `auth-snippet` skips                   | see "Authentication"                                                                                                                 |

Response headers: nginx sends only `Strict-Transport-Security: max-age=31536000; includeSubDomains`
(https only, replacing an app's own value) and removes `Server`; the gateways do exactly that and
nothing else, so an app's own `Referrer-Policy`, `Permissions-Policy` etc. pass through. Per-app
hardening headers are added later on that app's route (filter), never gateway-wide.

Never convert nginx snippets with a tool, and never guess: capture the live behaviour with `curl`
on the nginx VIP (`.1` internal, `.4` external) first, then reproduce it on `.13` / `.14`.

### Backend TLS with a CA: `BackendTLSPolicy` (vault)

`platform/vault/templates/httproute.yaml`. Reference the CA straight from the operator's Secret
(`kind: Secret`, key `ca.crt`, same namespace as the policy), so rotation follows the operator:

```yaml
apiVersion: gateway.networking.k8s.io/v1
kind: BackendTLSPolicy
spec:
  targetRefs:
    - {group: "", kind: Service, name: vault, sectionName: api-port}   # port NAME of the Service
  validation:
    hostname: vault.vault.svc.cluster.local     # must match a SAN of the backend certificate
    caCertificateRefs:
      - {group: "", kind: Secret, name: vault-tls}
```

Check the SANs first (`kubectl get secret ... -o jsonpath='{.data.server\.crt}' | base64 -d | openssl x509 -noout -ext subjectAltName`)
and never print the whole Secret (it can hold the CA key). A wrong hostname or CA shows as 503
`UF` with the policy still `Accepted`.

### Backend TLS without a CA: `Backend` (unifi)

`apps/unifi/templates/httproute.yaml`. nginx never verified the self-signed certificate, so the same
trust is an Envoy Gateway `Backend` (controller option `extensionApis.enableBackend`, on) referenced
from the route:

```yaml
apiVersion: gateway.envoyproxy.io/v1alpha1
kind: Backend
spec:
  endpoints: [{fqdn: {hostname: <svc>.<ns>.svc.cluster.local, port: 8443}}]
  tls: {insecureSkipVerify: true}
---
# in the HTTPRoute rule
backendRefs:
  - {group: gateway.envoyproxy.io, kind: Backend, name: unifi}
```

A `Backend` has static endpoints only: the Service FQDN, not pod IPs. Do not tighten verification
unasked.

### Lua on a request (radarr)

`apps/radarr/templates/httproute.yaml` (Radarr#11557: append `excludeLocalCovers=true` to the movie
list). The controller runs with `extensionApis.enableLua: true`. Pattern:

1. a **dedicated HTTPRoute** for the narrowest path (it wins over the catch-all route of the same
   host; Gateway API merges same-host routes and `Exact` / more specific paths take precedence;
   `RegularExpression` paths such as `(?i)/api/v3/movie` also won over `PathPrefix /` live);
2. an `EnvoyExtensionPolicy` targeting **that** HTTPRoute with `lua: [{type: Inline, inline: ...}]`
   defining `envoy_on_request(handle)`; rewrite the path with
   `handle:headers():replace(":path", ...)`.

Reproduce the nginx regex semantics exactly (case-insensitive, query handling) and test the Lua
with `lua5.4` offline against the edge cases before pushing. nginx path matches are
case-insensitive when the regex says `~*`; Gateway API paths are case-sensitive unless a regex
with `(?i)` is used. A regex path is a **full** match.

## Timeouts

**nginx timeouts are idle timeouts, Envoy's route timeout is a total limit.** nginx
`proxy-read-timeout` / `proxy-send-timeout` (default 60 s) only fire when nothing is read or
written for that long. Envoy's `requestTimeout` (what `BackendTrafficPolicy.timeout.http.requestTimeout`
and HTTPRoute `timeouts.request` / `backendRequest` set) runs from the end of the request until the
response is complete, so 60 s total cut a 50 KB/s download of the Radarr movie list at 60.3 s where
nginx delivered all of it in 158 s. Gateway-wide defaults (`system/envoy-gateway/resources/policies.yaml`):

| Envoy setting                                              | Value | nginx equivalent                                       |
| ---------------------------------------------------------- | ----- | ------------------------------------------------------ |
| BTP `timeout.http.requestTimeout`                          | `0s`  | none (nginx has no total limit)                        |
| BTP `timeout.http.streamIdleTimeout` (route idle timeout)  | `60s` | `proxy_read_timeout` / `proxy_send_timeout` 60 s       |
| CTP `timeout.http.streamIdleTimeout` (listener)            | `60s` | same, for requests no route matched                    |
| BTP `timeout.tcp.connectTimeout`                           | `5s`  | `proxy_connect_timeout` 5 s (Envoy default is 10 s)    |
| BTP `timeout.http.connectionIdleTimeout` (upstream)        | `60s` | upstream `keepalive_timeout` 60 s                      |
| CTP `timeout.http.idleTimeout` (client connection)         | `75s` | `keepalive_timeout` 75 s                               |
| CTP `timeout.http.requestHeadersReceivedTimeout`           | `60s` | `client_header_timeout` 60 s                           |

`streamIdleTimeout` counts a stream as idle only when no bytes move in **either direction**, and it
also bounds upgraded streams: a websocket or SSE stream with traffic (pings, events) is never cut
by it, a silent one is cut after 60 s, exactly like nginx. A websocket is **not** subject to the
route request timeout. Do not set `requestReceivedTimeout`: it is a total upload limit
(nginx `client_body_timeout` is idle).

### Translating a nginx timeout annotation

| Annotation                                    | Gateway API                                                                                                                       |
| --------------------------------------------- | --------------------------------------------------------------------------------------------------------------------------------- |
| `proxy-read-timeout`, `proxy-send-timeout` <= 60 | nothing (gateway default)                                                                                                       |
| `proxy-read-timeout`, `proxy-send-timeout` > 60  | `streamIdleTimeout` = the larger of the two (Envoy has one idle timeout), on the route BTP below                                |
| `proxy-connect-timeout` <= 60 and != 5        | `tcp.connectTimeout` = the value                                                                                                  |
| `proxy-connect-timeout` > 60                  | `tcp.connectTimeout: 60s`: 300/600/3600 are copy-paste values (a cluster Service connects in milliseconds or fails), say so in the PR |
| `proxy-body-size`, `proxy-buffer-size`, `proxy-buffering`, `proxy-request-buffering` | nothing (see above)                                                          |

Route override (mergeType is mandatory: without it this policy REPLACES the gateway one and
`requestTimeout` goes back to Envoy's 15 s). Proven live on the rsshub pilot: the route kept
`timeout: 0s`, got `idle_timeout: 3600s` and the cluster `connect_timeout: 60s`, the rest was inherited.

```yaml
apiVersion: gateway.envoyproxy.io/v1alpha1
kind: BackendTrafficPolicy
metadata:
  name: <app>-timeouts
spec:
  targetRefs:
    - {group: gateway.networking.k8s.io, kind: HTTPRoute, name: <route>}
  mergeType: StrategicMerge
  timeout:
    tcp:
      connectTimeout: 60s
    http:
      streamIdleTimeout: 3600s
```

Never use `timeouts.request` on the
route to emulate a long nginx timeout, it would reintroduce the total limit.

Per-route values the Ingresses need today (all other hosts keep the defaults):

| App (Ingress host)                                          | `streamIdleTimeout` | `connectTimeout` |
| ----------------------------------------------------------- | ------------------- | ---------------- |
| forgejo (`git.grigri.cloud`)                                | 3600s               | 60s              |
| gitlab (`gitlab.grigri.cloud`)                   | 3600s               | 60s              |
| immich (`photos.internal`, `photos`)                        | 3600s               | 60s              |
| nextcloud (`nextcloud.grigri.cloud`)                        | 3600s               | 60s              |
| transcoder (`transcoder.internal`)                          | 3600s               | 60s              |
| calypso runner Ingress (`*.app.staging.forkline.dev`)       | 3600s               | 60s              |
| minio (`s3.internal`), s3-public (`s3`), cross-backups      | 3601s               | default          |
| isidoro (`isidoro.grigri.cloud`, tenant)                    | 1800s               | 10s              |
| jellyseerr (`requests.grigri.cloud`)                        | 1800s (send was 600) | 60s             |
| prowlarr (`prowlarr.grigri.cloud`)                          | 300s                | 60s (was 300)    |

Re-read the live annotations before each app (`kubectl get ingress -n <ns> <name> -o yaml`); the
table is the 2026-11-09 inventory.

**Config changes that modify a listener drain connections.** Adding or changing an HCM filter
(OIDC, JWT, Lua, a new TLS hostname) modifies the listener and Envoy closes the old listener's
connections after the drain time (60 s). A websocket or download open during such a sync is cut
(seen once, a 121 s websocket on the pilots), nginx keeps old workers until their connections finish.
Do not sync listener-affecting changes during a long transfer; the client reconnects. A new or
removed `SecurityPolicy` or `EnvoyExtensionPolicy` counts as such a change (each becomes its own
HTTP filter in the listener's filter chain), and so does a certificate change: mechanism, how to
see it in the Envoy config and what to batch in
[`docs/troubleshooting/envoy-gateway-listener-drain.md`](../troubleshooting/envoy-gateway-listener-drain.md).

Test a route like this (the Phase 2 reproduction): a rate-limited response longer than 60 s
(`curl --limit-rate 50k ... -w '%{size_download} %{time_total}'` of a few MB), a websocket held
more than 60 s with traffic, and a client that stops reading for 100 s (cut at about 60 s on both
nginx and Envoy, resumed after 40 s on both).

## NetworkPolicies

Before verifying, check the app's namespace: `kubectl get netpol,cnp -n <ns>` and
`grep -rn ingress-nginx apps/<app>`. If any policy admits only the nginx namespaces, add the Envoy
proxies **in the same commit** (an omission shows only on the Envoy path as 503 `UF`):

```yaml
- namespaceSelector:
    matchLabels:
      kubernetes.io/metadata.name: envoy-gateway
  podSelector:
    matchLabels:
      app.kubernetes.io/name: envoy
      app.kubernetes.io/component: proxy
      gateway.envoyproxy.io/owning-gateway-name: internal   # or external
```

Egress policies of pods that call hairpin names (which resolve to a gateway VIP) need the same
rule, with one difference: the Envoy proxy pods listen on the **container ports 10443 (https) and
10080 (http)**, the Service ports 443 and 80 map to them. Cilium evaluates the destination after
the Service is translated to the pod, so an egress rule for `443`/`80` toward the Envoy pods never
matches and the call times out:

```yaml
egress:
  - toNamespaces: [...]    # or toEndpoints with the Envoy proxy labels shown above
    toPorts:
      - ports:
          - {port: "10443", protocol: TCP}
          - {port: "10080", protocol: TCP}
```

The hairpin appears as soon as DNS points the public name at the VIP (pfSense overrides, see
[`docs/network/ingress.md`](../network/ingress.md)): tenant runners and any pod that
calls `https://<public name>` hits it. None of the first pilots had a policy in its namespace.

## Authentication

Native Envoy Gateway OIDC against Kanidm plus a JWT group check (plan decision D3). It replaces
the nginx `auth-url` / `auth-signin` annotations (oauth2-proxy at `auth.grigri.cloud`) per app.
Worked examples: `apps/http-echo` (identity headers), `apps/esphome` (internal only),
`apps/navidrome` (skip paths, Lua body rewrite, 401 without redirect), `apps/radarr` (apiKey skip,
raw routes).

### Per app: three objects

1. **`KanidmOAuth2Client`** in the app namespace (`templates/kanidm-oauth2-client.yaml`), one per
   app, never the shared `k8s-oauth2-proxy` client:

   ```yaml
   apiVersion: kaniop.rs/v1beta1
   kind: KanidmOAuth2Client
   metadata:
     name: <app>
   spec:
     kanidmRef: { name: kanidm, namespace: kanidm }
     displayname: <app>
     origin: https://<host>/
     redirectUrl:
       - https://<host>/oauth2/callback # one per host (internal and external)
     # the group(s) of the nginx `allowed_groups=`; Kanidm issues no token to anyone else
     scopeMap:
       - group: <app>-users
         scopes: [openid, profile, email, groups]
     preferShortUsername: true # short `preferred_username`, as oauth2-proxy sent it
     strictRedirectUrl: true
     secretKeyAliases:
       clientSecret: [client-secret] # Envoy Gateway reads this key
   ```

   The operator writes `<app>-kanidm-oauth2-credentials` (`CLIENT_ID`, `CLIENT_SECRET`,
   `client-secret`) next to the policy: no `ReferenceGrant`. PKCE stays on (Envoy always sends
   S256). The scope map group must exist in Kanidm (Kaniop reports `ScopeMapUpdated=False`
   otherwise); groups created by hand are fine.

2. **`SecurityPolicy`** per protected route (`templates/security-policy.yaml`); one policy may
   target several routes of the same host (they then share the cookies):

   ```yaml
   apiVersion: gateway.envoyproxy.io/v1alpha1
   kind: SecurityPolicy
   metadata:
     name: <app>-<gateway>
   spec:
     targetRefs:
       - { group: gateway.networking.k8s.io, kind: HTTPRoute, name: <route> }
     oidc:
       provider:
         issuer: https://idm.grigri.cloud/oauth2/openid/<app>
         # explicit endpoints: no OIDC discovery at translation time
         authorizationEndpoint: https://idm.grigri.cloud/ui/oauth2
         tokenEndpoint: https://idm.grigri.cloud/oauth2/token
       clientID: <app>
       clientSecret: { name: <app>-kanidm-oauth2-credentials }
       redirectURL: https://<host>/oauth2/callback
       logoutPath: /oauth2/logout # the default `/logout` shadows the app's own path
       scopes: [openid, profile, email, groups]
       cookieNames: { accessToken: oidc-access-token, idToken: oidc-id-token }
     jwt:
       providers:
         - name: kanidm
           issuer: https://idm.grigri.cloud/oauth2/openid/<app>
           audiences: [<app>]
           extractFrom: { cookies: [oidc-id-token] }
           remoteJWKS: { uri: https://idm.grigri.cloud/oauth2/openid/<app>/public_key.jwk }
           claimToHeaders: # only the headers nginx `auth-response-headers` passed
             - { header: X-Auth-Request-Preferred-Username, claim: preferred_username }
     authorization:
       defaultAction: Deny
       rules:
         - name: allowed-groups
           action: Allow
           principal:
             jwt:
               provider: kanidm
               claims:
                 - name: groups
                   valueType: StringArray
                   values: [<app>-users@idm.grigri.cloud] # SPN form, same as `allowed_groups=`
   ```

3. **The route** with a stable name (`forceRename` in app-template when the release has a single
   route: its default name changes when a second route is added, and the policy would then
   target nothing).

### Mapping from nginx

| nginx / oauth2-proxy                                        | Envoy Gateway                                                                                                                                       |
| ----------------------------------------------------------- | --------------------------------------------------------------------------------------------------------------------------------------------------- |
| `auth-url ...?allowed_groups=<g>@idm.grigri.cloud`          | client `scopeMap` group `<g>` + `authorization` allow on `groups` = `<g>@idm.grigri.cloud`                                                         |
| `auth-url` without `allowed_groups`                         | every group of the `k8s-oauth2-proxy` client scope map (`k8s-oauth2-proxy-users`, `navidrome-users`, `hermes-users`), as http-echo                  |
| `auth-response-headers: X-Auth-Request-Preferred-Username`  | `claimToHeaders` `preferred_username` (`X-Auth-Request-User` / `-Email`: `email`, oauth2-proxy's default user claim)                                |
| `X-Auth-Request-Groups`                                     | `claimToHeaders` cannot copy arrays: add a `claimMap` with `joinStrategy: csv` on the client and map that string claim                             |
| `auth-url` without `auth-signin` (401, no redirect)         | `oidc.denyRedirect.headers: [{name: ":path", type: RegularExpression, value: "/api([/?].*)?"}]` (navidrome external `/api`)                        |
| `auth-snippet` `return 200` skips                           | a second same-host route with the skip matches and **no** policy, plus a `RequestHeaderModifier` that removes `X-Auth-Request-*` (below)             |
| `auth.grigri.cloud` cookie on `.grigri.cloud`, shared       | cookies per host (`oidc-id-token`, `oidc-access-token`, `OauthHMAC-*`, `OauthExpires-*`, `RefreshToken-*`); a new app is a silent Kanidm redirect |
| `/oauth2/sign_out`                                          | `/oauth2/logout` clears the Envoy cookies only (Kanidm has no end-session endpoint): the next request logs in again silently while Kanidm's session lives |

### Skip paths (no auth)

Same-host routes merge; give the skip its own route without a `SecurityPolicy`:

```yaml
- matches:
    - path: { type: PathPrefix, value: /rest }
    - path: { type: PathPrefix, value: /share }
  filters:
    - type: RequestHeaderModifier
      requestHeaderModifier:
        remove: [X-Auth-Request-User, X-Auth-Request-Email, X-Auth-Request-Preferred-Username,
                 X-Auth-Request-Groups, X-Auth-Request-Access-Token]
```

- Gateway API prefixes are segment-based: `^/(rest|share)` in nginx also skipped `/restx`; Envoy
  does not (stricter, accepted).
- A query-based skip (radarr `apiKey`) is `path: RegularExpression` + `queryParams:
  [{type: RegularExpression, name: apiKey, value: ".+"}]`. Envoy Gateway orders regex routes by
  **regex length, then query match count**: a skip must not lose to a longer protected regex on
  the same host (radarr uses `(?-i)/api/v3/movie` against the protected `(?i)/api/v3/movie`).
  Compare every case (case, empty value, sub-paths) with nginx.
- Header removal on the **protected** route is not possible with a route filter: route header
  changes run in the router, after `jwt_authn`, and would also remove the claim headers. There
  `jwt_authn` itself removes every `claimToHeaders` header before it adds the claim (Envoy
  `sanitizeHeaders`), so a client-sent copy never reaches the app.

### Ext auth: `pathOverride` and client identity headers

For a backend that authenticates through its own service (an auth proxy, not Kanidm OIDC) the
`SecurityPolicy` uses `extAuth.http`. Two settings decide who controls what the auth service sees:

- `pathOverride` **replaces the original path and query** in the auth request with a fixed value:
  the client has no influence on the path the auth service is asked about. Use it whenever the
  auth service only needs the request's identity (cookies, `Authorization`), or when the path
  carries a per-route identifier (`pathOverride: /auth/runner/<id>`).
- plain `path` **appends the client's path** to the auth service's path: the client chooses part of
  the auth request, so the auth service must not trust it.

Whatever the auth service answers in `headersToBackend` is copied to the app. Client-sent copies of
those identity headers must be removed first, on the listener, before any filter or route sees
them:

```yaml
apiVersion: gateway.envoyproxy.io/v1alpha1
kind: ClientTrafficPolicy
spec:
  targetRefs:
    - {group: gateway.networking.k8s.io, kind: Gateway, name: external, sectionName: <listener>}
  headers:
    earlyRequestHeaders:
      removeOnMatch:                       # names match case-insensitively
        - {type: Prefix, value: x-auth-request-}
```

Caveat: a `ClientTrafficPolicy` scoped to a **listener** (`sectionName`) **replaces** the
gateway-wide `ClientTrafficPolicy` for that listener entirely: the timeouts and limits of
`system/envoy-gateway/resources/policies.yaml` do not merge in. Copy every setting the listener
still needs into the listener-scoped policy (compare with `kubectl get clienttrafficpolicy -A`).
The header strip is a listener change: it drains that listener's long connections once (see
"Timeouts").

### Verification (no browser needed)

```bash
H=<host>; VIP=192.168.193.14   # .13 for internal hosts
# 1. unauthenticated: 302 to /ui/oauth2 with client_id, redirect_uri, scope, S256 challenge, state
curl -sk --resolve $H:443:$VIP -o /dev/null -w '%{http_code} %{redirect_url}\n' https://$H/
# 2. forged cookies or bearer: still 302 (the OAuth2 filter checks its HMAC before jwt_authn)
curl -sk --resolve $H:443:$VIP -b 'oidc-id-token=x.y.z; OauthHMAC=x' -o /dev/null -w '%{http_code}\n' https://$H/
# 3. logout clears the five cookies and redirects to /
curl -sk --resolve $H:443:$VIP -D- -o /dev/null https://$H/oauth2/logout | grep -i set-cookie
# 4. skip paths answer like nginx, also with a spoofed X-Auth-Request-Preferred-Username
```

Prove the client secret and HMAC reached the proxy (SDS) without a login: start a flow (keep the
cookies), call `/oauth2/callback?code=bogus&state=<state from step 1>`: Envoy answers 401 and the
Kanidm log shows `POST /oauth2/token` with `handle_oauth2_token_exchange` failing on `Failed to
deserialise a valid JWE` (the code), which Kanidm only reaches after the client authenticated.

Interactive check (owner, once per pattern change), with a throwaway browser profile that maps the
hosts to the gateway VIPs (Kanidm itself resolves normally):

```bash
chromium --user-data-dir="$(mktemp -d)" \
  --host-resolver-rules="MAP http-echo.grigri.cloud 192.168.193.14, MAP esphome.internal.grigri.cloud 192.168.193.13" \
  https://http-echo.grigri.cloud/
```

Expect: Kanidm login and consent, then the http-echo JSON with `x-auth-request-preferred-username`
(short name) and `x-auth-request-email`; `https://http-echo.grigri.cloud/oauth2/logout` clears the
cookies; a person outside the scope map group is refused by Kanidm at `/ui/oauth2`; an Envoy 403
(`RBAC: access denied`) needs a token whose `groups` lack the allowed SPN (see the plan, Gate 2).

### Gotchas

- Policy `Accepted=False` (`secret ... does not exist`) for a few seconds after the first sync: the
  operator creates the Secret after the policy. Envoy Gateway answers 500 on the route meanwhile
  (fail closed) and recovers by itself; the proxies may log `initial fetch timed out for
  ...tls.v3.Secret` once, harmless when the bogus-code check above passes afterwards.
- The proxies reach `idm.grigri.cloud` through cluster DNS, which resolves it to the external
  gateway's own VIP (`.14`, pfSense override): token exchange and JWKS depend on that name
  resolving in pods. Verified working since Phase 5 (the Envoy cluster `idm_grigri_cloud_443` has 0
  connect failures; a Navidrome login proved the exchange).
- Envoy logs the raw cookie value when it cannot decrypt a token cookie (`failed to decrypt token:`
  in the proxy log): only forged or stale cookies hit it, but treat the proxy log as sensitive.

## Denying a path at the edge

nginx `location = /metrics { deny all; }` or `if ($uri ~ "^/metrics(/|$)") { return 404; }` in a
configuration snippet becomes a first route rule with an `HTTPRouteFilter` direct response (the
repo example: `apps/home-assistant/templates/httproutefilter-prometheus-deny.yaml`, which answers
403 for an Exact path; the 404 variant only changes `statusCode`):

```yaml
apiVersion: gateway.envoyproxy.io/v1alpha1
kind: HTTPRouteFilter
metadata:
  name: <app>-deny
spec:
  directResponse:
    statusCode: 404
    contentType: text/plain
    body: {type: Inline, inline: not found}
---
# in the HTTPRoute, FIRST rule, before the catch-all
rules:
  - matches:
      - path: {type: PathPrefix, value: /metrics}      # matches /metrics and /metrics/...
    filters:
      - type: ExtensionRef
        extensionRef: {group: gateway.envoyproxy.io, kind: HTTPRouteFilter, name: <app>-deny}
  - backendRefs: [...]
```

Why the rule must match the **normalized** path: Envoy merges duplicate slashes (`//metrics`) and
decodes percent-encoded unreserved characters (`/%6detrics`) **before** route matching, like nginx
`$uri`. So the rule only needs the clean path, and a request in disguise still hits it; but a
deny written for the raw spelling would never match. Prove it with the three spellings
(`/metrics`, `//metrics`, `/%6detrics`) plus `/metricsx` (a different path, must pass) on the
Envoy VIP. `PathPrefix` is segment-based (`/metrics` and `/metrics/x`, not `/metricsx`); use
`Exact` when only the one path is denied.

The deny applies to the edge only: Prometheus scrapes the Service inside the cluster and is not
affected. When the nginx equivalent is still needed (only while nginx runs), it is the snippet
`if ($uri ~ "^/metrics(/|$)") { return 404; }` on the Ingress; nginx matches the normalized `$uri`
as well.

## Pods that must reach a public name at the gateway VIP

A pod that calls a public hostname normally leaves the cluster, hits the WAN address and relies on
NAT reflection. When it must reach the **in-cluster gateway VIP** directly (a name with no
pfSense override, or a client that must not depend on the router), pin the name in the pod spec
with `hostAliases`. Hermes does this for `mcp.grigri.cloud` -> `192.168.193.14`
(`apps/hermes/values.yaml`, `defaultPodOptions.hostAliases`; the regression test
`apps/hermes/tests/test_hostaliases_mcp_dns.py` checks the IP against the external Gateway):

```yaml
hostAliases:
  - ip: "192.168.193.14"          # the external Gateway VIP
    hostnames:
      - mcp.grigri.cloud
```

An alias is an `/etc/hosts` line: it survives DNS changes, so when the VIP moves it must be edited
in git too (the test fails when it drifts). The pod's egress NetworkPolicy must allow the Envoy
container ports (see "NetworkPolicies"). Remove the alias once the name has a pfSense override.

## Verification (per route, before the next commit)

1. Argo app `Synced Healthy`; `kubectl get httproute -n <ns> <name> -o jsonpath='{.status.parents}'`
   shows `Accepted=True` and `ResolvedRefs=True` (also for `BackendTLSPolicy`, `Backend`,
   `EnvoyExtensionPolicy`). A pre-existing unrelated OutOfSync resource in `kube-system` does not
   count.
2. Compare the new VIP with nginx, same request, `--resolve` so DNS is not involved:

   ```bash
   H=<host>
   for ip in 192.168.193.13 192.168.193.1; do   # internal; use .14 and .4 for external
     curl -sk --resolve $H:443:$ip -o /dev/null -D- -w 'code=%{http_code} h%{http_version}\n' https://$H/<path>
   done
   ```

   Expect the same status and body size and the same response headers: on the Envoy side h2, HSTS
   (once) and no `Server`, nothing else beyond what the app itself sends. Differences that are
   **expected** and not a defect (seen in every host diff):

   | Observed                                                      | Why                                                                            |
   | ------------------------------------------------------------- | ------------------------------------------------------------------------------ |
   | port 80 redirect `301` on Envoy, `308` on nginx               | the gateway `http` listener redirects with 301; both are permanent             |
   | empty body on Envoy 404, 403 and redirects                    | the status is what matters; Envoy adds no body                                 |
   | request headers whose name contains an underscore are dropped | Envoy drops them; nothing to fix unless an app needs one (then rename it)      |
   | paths are case-sensitive on Envoy                             | a path nginx matched case-insensitively (`~*`) needs a `(?i)` regex path       |
   | an app root answering `200` on nginx answers `302`            | seen on nextcloud `/`; compare the redirect target, not the status             |
   | OIDC apps ask for one more login when moving from oauth2-proxy to Envoy native OIDC | new per-host cookies, the old ones are not understood            |

   Anything else is a defect until explained. The comparison with nginx only exists while nginx
   runs: after Phase 6 compare against the previous release or the app's own expectations instead.
3. Certificate: `openssl s_client -connect 192.168.193.13:443 -servername $H -alpn h2` shows the
   wildcard certificate (`Verification: OK`, `ALPN protocol: h2`). A request without `-k` must verify.
4. A functional request per app (an API call, a real page, for a body-size or long request the
   actual long request), the special-case behaviour (a proof that distinguishes "filter applied"
   from "not applied", e.g. a response that differs with and without the rewritten parameter),
   `http://` redirects to `https://`, `/prefix` vs `/prefixfoo` boundaries.
5. Envoy access log: no `response_flags` other than `-` and the real client address in
   `client_address`:

   ```bash
   kubectl logs -n envoy-gateway -l gateway.envoyproxy.io/owning-gateway-name=internal -c envoy --since=10m | grep <host>
   ```

6. For backend TLS: dump the proxy cluster to prove what is validated (read-only):
   `kubectl port-forward -n envoy-gateway <proxy pod> 19000:19000`, then
   `curl -s 'localhost:19000/config_dump?resource=dynamic_active_clusters'` and look at
   `transport_socket_matches` of `httproute/<ns>/<route>/rule/<n>` (`sni`, `match_typed_subject_alt_names`,
   the SDS secret `<ns>/<name>-ca`). With `insecureSkipVerify` there is no validation context.
7. external-dns did not change a record for the app (see "DNS").

Tips: in zsh `path` is tied to `PATH`, so never use it as a loop variable; a body larger than a few
MB works through Envoy unchanged (Radarr movie list, 8 MB, identical size on both paths).

## Known unknowns

- Websockets: verified over 60 s with traffic on the Radarr SignalR endpoint (160 s and 200 s held,
  also under the old 60 s total limit). A websocket silent for more than 60 s is expected to be cut
  (`streamIdleTimeout`, like nginx's `proxy_read_timeout`) but no pilot has a silent websocket to
  prove it; check it on the first app that has one (Home Assistant, esphome logs, jellyfin).
- Hubble UI streams over gRPC-web (`/api`); the stream was not exercised (the probe paths answer 404
  on both nginx and Envoy). Open the UI in a browser against `.13` before the switch.
- A backend that accepts a request and never answers is bounded by `streamIdleTimeout` (60 s), not
  proven end to end: no pilot backend can be made silent without a mutation.
- Envoy streams request bodies to the backend (no request buffering, nginx buffers by default): a
  client that uploads slowly reaches the app as a slow upload (Radarr's Kestrel aborts a stalled body
  after 5 s). Apps that cannot take that need `proxy-request-buffering`-like behaviour, which Envoy
  Gateway does not offer; test large slow uploads on immich/nextcloud/forgejo.
