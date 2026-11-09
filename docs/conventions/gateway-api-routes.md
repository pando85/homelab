# Exposing an App with Gateway API (Envoy Gateway)

How an app gets an HTTP(S) route on the Envoy Gateways. This replaces the ingress-nginx
annotation and snippet guidance. Background, decisions and the migration state:
`planning/ingress-nginx-to-envoy-gateway.md` (the recipes there cover every nginx-only behaviour
that was ported).

## Migration state: routes next to Ingresses

Until ingress-nginx is decommissioned, every app carries **both** its Ingress (nginx keeps serving
real traffic) and its route (Envoy Gateway, verified on its own VIP). Never remove an Ingress as
part of adding a route. A **new** app gets the route with its first deploy and an Ingress only if
it must be reachable before the switches. After the decommission only routes remain.

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

## DNS (external-dns)

external-dns watches HTTPRoutes carrying `external-dns.kubernetes.io/enabled: "true"`. The record
target is not set on routes: the **external Gateway** carries `external-dns.kubernetes.io/target:
grigri.cloud` and the internal Gateway has none (its records default to `.13`).

- **External route:** annotation `external-dns.kubernetes.io/enabled: "true"` and nothing else (no
  `target`). The record already exists (CNAME to the apex), so nothing changes.
- **Internal route, before the internal switch:** **no external-dns annotation.** Adding it would
  move that one internal record from `.1` to `.13` app by app. The internal switch commit adds the
  annotation to every internal route at once.
- Verify no record changed: `kubectl logs -n external-dns deploy/external-dns --since=15m | grep -v 'already up to date'`.

## nginx annotation to Gateway API

The gateway-wide defaults match nginx (60 s request timeout, HSTS and security headers, `Server`
removed, h2 and HTTP/1.0, no body limit, no response buffering, websocket upgrade enabled on
every route). An app only needs something extra when it relied on a non-default behaviour.

| nginx                                                              | Gateway API / Envoy Gateway                                                                                                         |
| ------------------------------------------------------------------ | ----------------------------------------------------------------------------------------------------------------------------------- |
| `proxy-body-size`, `proxy-buffer-size`, `proxy-buffering off`       | nothing (no body limit, streaming, larger header limits)                                                                             |
| `proxy-read/send-timeout`, long uploads and streams                 | route `timeouts.request: 0s`, or a route `BackendTrafficPolicy` with `mergeType: StrategicMerge` (never one without it)              |
| websockets                                                          | nothing to configure; **verify an idle connection older than 60 s** (see "Known unknowns")                                          |
| `backend-protocol: HTTPS`, backend CA available                     | `BackendTLSPolicy` (below)                                                                                                           |
| `backend-protocol: HTTPS`, self-signed backend, nginx never verified | Envoy Gateway `Backend` with `tls.insecureSkipVerify: true` (below)                                                                  |
| `rewrite-target`, redirects                                         | `URLRewrite` / `RequestRedirect` filters (a redirect rule cannot have `backendRefs`)                                                 |
| `configuration-snippet` / `server-snippet`                          | translate by behaviour (recipes in the plan); request-side query or header logic is an `EnvoyExtensionPolicy` with Lua (below)       |
| compression                                                         | `BackendTrafficPolicy` `compressor` per route, never on S3/MinIO hosts                                                               |
| `auth-url` / `auth-signin` / `auth-snippet` skips                   | see "Authentication"                                                                                                                 |

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
rule. None of the first pilots had a policy in its namespace.

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
- The proxies reach `idm.grigri.cloud` through cluster DNS (today the pfSense override to nginx
  `.4`, after Phase 5 `.14`): token exchange and JWKS depend on that name resolving in pods.
- Envoy logs the raw cookie value when it cannot decrypt a token cookie (`failed to decrypt token:`
  in the proxy log): only forged or stale cookies hit it, but treat the proxy log as sensitive.

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

   Expect the same status and body size; on the Envoy side also h2, HSTS, `x-frame-options`,
   `referrer-policy`, `permissions-policy`, no `Server`.
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

- Websocket connections older than the gateway-wide 60 s request timeout are not verified through
  the Envoy path yet. The route has `upgrade_configs: websocket` and a 3600 s `idle_timeout`, but
  the 60 s `timeout` is also on the route. Apps with long websockets (Home Assistant, UniFi events,
  jellyfin, Radarr SignalR) need a held-connection test before their route is considered done; the
  fix, if needed, is `timeouts.request: 0s` on that app's route.
- Hubble UI streams over gRPC-web (`/api`); a streaming call was not exercised (the probe path
  answered 404 on both nginx and Envoy). Open the UI in a browser against `.13` before the switch.
