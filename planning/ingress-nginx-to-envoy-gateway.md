# ingress-nginx to Envoy Gateway Migration

## Overview

Replace both ingress-nginx controllers (`nginx-internal`, `nginx-external`) with Envoy Gateway
(Gateway API: `GatewayClass`, `Gateway`, `HTTPRoute`/`TLSRoute`/`TCPRoute` and Envoy Gateway
policies), then decommission ingress-nginx and oauth2-proxy.

This also delivers the grigri (staging) platform part of
[multivara/gitops#199](https://git.grigri.cloud/multivara/gitops/issues/199) (Envoy Gateway for
Forkline Remote): experimental-channel Gateway API CRDs, a shared external Gateway with HTTPS
listeners plus a TLS passthrough listener, and the router 443 switch away from `192.168.193.4`.

**Status: Phase 1 in progress (2026-11-09). See Execution Status and Autonomy Scope.**

Strategy in one paragraph: run Envoy Gateway **side by side** on two new Cilium LB VIPs
(`.13` internal, `.14` external). Every app gets its internal and external routes and is verified
against the new VIPs **app by app**, while nginx keeps serving all real traffic. When every app is
done, traffic moves in **two single switches**, each verified and reversible in seconds:

1. **internal switch**: all internal names move to `.13` in one move (same DNS mechanism as
   today, see Phase 4);
2. **external switch**: one pfSense alias edit (`k8s_external_ingress`, `.4 -> .14`) plus the
   three DNS Resolver host overrides.
   nginx stays idle as the rollback path until both switches have soaked, then both controllers are
   removed together.

## Decisions

| #   | Topic                       | Decision                                                                                                                                                           |
| --- | --------------------------- | ------------------------------------------------------------------------------------------------------------------------------------------------------------------ |
| D1  | Strategy                    | Side by side. App by app: internal route, verify, external route, verify. After all apps: one internal switch, verify; then one external switch at pfSense, verify |
| D2  | VIPs and Forgejo SSH        | `.13` internal, `.14` external. Forgejo SSH (29518) as a `TCPRoute` on the external gateway, moving with the same pfSense alias                                    |
| D3  | Auth                        | Native Envoy Gateway OIDC against Kanidm + JWT authorization on `groups`; oauth2-proxy removed in Phase 6                                                          |
| D4  | Calypso runners             | TLS passthrough bridge of `*.app.staging.forkline.dev` to ingress-nginx-external until calypso emits HTTPRoutes                                                    |
| D5  | nginx-only behaviours       | Port and verify **all** of them (none dropped)                                                                                                                     |
| D6  | Internal DNS                | Same mechanism as today: external-dns publishes internal names to Cloudflare; the switch is one change that moves all records to `.13`                             |
| D7  | Cloudflare internal records | Keep them, pointing to `.13` (routes carry the external-dns annotation after the switch)                                                                           |
| D8  | Decommission timing         | Both controllers together at the end, after the external switch soaks                                                                                              |
| D9  | BGP ECMP                    | Not now: placement keeps proxies on `prusik`, one path per VIP as today                                                                                            |

## Execution Status

Work happens in a dedicated worktree (`git worktree add -b envoy-gateway <dir> origin/master`),
committed and pushed **directly to master** (`git push origin envoy-gateway:master`, after
`git pull --rebase origin master`). No PRs in this repo.

| Phase | State       | Notes                                                                                                                             |
| ----- | ----------- | --------------------------------------------------------------------------------------------------------------------------------- |
| 0     | done        | All steps done except step 7 (post the listener contract on multivara#199): owner action, does not block Phase 1. Findings below. |
| 1     | done        | All steps done except step 6 (ZeroSSL, waits for the owner's EAB credentials) and P1.1 (optional pfSense). Gate 1 satisfied.     |
| 2     | in progress | Pilots without auth done; auth pilots (kanidm route, http-echo, esphome, navidrome, radarr external) live and verified without a browser; the interactive login (Gate 2) waits for the owner. Procedure: `docs/conventions/gateway-api-routes.md`. |
| 3-6   | not started |                                                                                                                                   |

### Phase 1 progress (2026-11-09)

| Step | Commit                                                                                                             | Result                                                                                                                                                                                                           |
| ---- | ------------------------------------------------------------------------------------------------------------------ | ---------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------- |
| 1    | `68de320f4` `envoy-gateway: Add Envoy Gateway controller and Gateway API CRDs`                                     | Controller and experimental CRDs live, Synced Healthy.                                                                                                                                                           |
| 2    | `205cb6710` + `94f45bd35` `envoy-gateway: Add internal and external gateways` / `Use one certificate per listener` | Both Gateways Accepted and Programmed on `.13` / `.14`, 7 Certificates Ready (all `letsencrypt-prod-dns`), 2 proxies per Gateway on `prusik`, `.13/32` and `.14/32` learned by pfSense.                          |
| 3    | `eef9c8613` `git: Add forgejo SSH TCPRoute on the external gateway`                                                | TCPRoute `git-forgejo-ssh` Accepted and ResolvedRefs; SSH through `.14` shows the same host keys as through nginx.                                                                                               |
| 4    | `2c0498a11` `envoy-gateway: Add monitoring`; `71d00f34e` `envoy-gateway: Add external tracing to the otel-collector`; `1ba494e72` `envoy-gateway: Add roll-test marker to the external proxy pod template` | ServiceMonitor `envoy-gateway` and PodMonitor `envoy-proxy` (5 targets up, `gateway` label), 3 upstream dashboards (v1.9.3) in folder `envoy-gateway`, PrometheusRule `envoy-gateway` (3 alerts, loaded, not firing), external tracing to Tempo (service `envoy-external`), Loki `traceID` derived field. Roll test: 0 failures (below). |
| 5    | `9bfc7a781` `external-dns: Add gateway-httproute and crd sources and forkline.io`                                  | Sources `service ingress gateway-httproute crd`, domain filters plus `forkline.io`. Pod 0 restarts, no RBAC or watch errors, every cycle "All records are already up to date"; `dig` before and after identical. |
| 6    | pending                                                                                                            | `cert-manager: Add ZeroSSL ClusterIssuer for forkline.io` waits for the owner's EAB credentials in Vault (can trail).                                                                                            |
| 7    | `874727b4a` `scripts: ...` + `9ad8e1404` `http-echo: Add HTTPRoutes on both gateways`                              | Both routes Accepted and ResolvedRefs, 200 over h2 on `.13` and `.14`, real client IP preserved; no DNS record moved. Gate 1 evidence below.                                                                     |
| 8    | `09d6fd1f8` `envoy-gateway: Add the runner passthrough bridge TLSRoute`                                            | TLSRoute `runners-bridge` (`tls-bridge-runners`, `*.app.staging.forkline.dev` -> `ingress-nginx-external-controller:443`) Accepted and ResolvedRefs, 1 attached route; ReferenceGrant in `ingress-nginx-external` (chart template). G11 below.                                                                  |
| 9    | `192d1d3ee` `envoy-gateway: Add internal isidoro listener`                                                         | Listener `https-isidoro-grigri-cloud` on `internal` Programmed, own Certificate Ready; `.13` serves `*.isidoro.grigri.cloud` over h2 for `idm-internal.isidoro.grigri.cloud`; no `OverlappingTLSConfig` on either Gateway. |

Steps 8 and 9 notes, G11 evidence (2026-11-09, from the workstation, runner host
`autonomous-runner-issue-603-09.app.staging.forkline.dev`, whose backend has no endpoints, so nginx
answers 503 on both paths):

- Bridge, `--resolve ...:443:192.168.193.14` vs `.4`: same certificate (`CN=*.app.staging.forkline.dev`,
  Let's Encrypt, sha256 `1F:98:04:6E:...:44:5F`, serial `0660DCF9...`), ALPN h2 and `HTTP/2 503` on both:
  TLS is terminated by nginx, not by Envoy.
- Wildcard depth: SNI `x.y.app.staging.forkline.dev` (two labels) **also passes through**: Envoy `.14`
  and nginx `.4` return the same "Kubernetes Ingress Controller Fake Certificate" (sha256
  `AE:95:5F:B6:...:3E:A3`), so the Gateway API wildcard matches several labels as documented.
- Coexistence with the HTTPS listeners on 443: SNI `foo.staging.forkline.dev` gets the Envoy
  `*.staging.forkline.dev` certificate, ALPN h2 (curl: `HTTP/2 404`, no route); apex
  `staging.forkline.dev` gets its own cert, h2; `http-echo.grigri.cloud` gets `*.grigri.cloud`, h2,
  `HTTP/2 200`. No passthrough leakage into the terminating listeners.
- Not covered: a runner with a live backend (none exists today), websocket upgrades and long
  streams through the bridge. The bridge hides the client address from nginx (no PROXY protocol).
- Isidoro: `openssl s_client -connect 192.168.193.13:443 -servername idm-internal.isidoro.grigri.cloud`
  returns `CN=*.isidoro.grigri.cloud` (SAN only that, Let's Encrypt, verification OK), ALPN h2;
  `curl` through `.13` gets `HTTP/2 404` (no route yet).

Steps 5 and 7 notes (2026-11-09):

- external-dns v0.23.0, chart 1.23.0: adding the two sources made the chart add `gateways`,
  `httproutes` and `namespaces` RBAC by itself. The `DNSEndpoint` CRD already existed. Policy stays
  `upsert-only`, annotation filter unchanged. After the sync the route-bearing cycle (routes created
  20:52:14, cycles 20:52:44 and 20:54:44) planned no change: the `http-echo.grigri.cloud` route
  (annotation `enabled` only, target `grigri.cloud` from the external Gateway) renders the record
  that already exists. `dig @1.1.1.1` on 4 internal (`.1`), 5 external and 2 forkline staging names is identical before and after; `http-echo.internal.grigri.cloud` still answers `.1`.
- app-template 5.3.0 renders `gateway.networking.k8s.io/v1` HTTPRoutes when the helm template gets
  `--api-versions gateway.networking.k8s.io/v1/HTTPRoute`; `scripts/deploy-dir.rh` now passes it.
  Live (Argo's own render) the routes are `v1` too.
- http-echo: `route.internal` (`internal` / `https-internal-grigri-cloud`, no annotation) and
  `route.external` (`external` / `https-grigri-cloud`, annotation `enabled`). Both Ingresses
  unchanged: nginx `.1` still 200, nginx `.4` still 302 to `auth.grigri.cloud` (oauth2-proxy), and
  the Envoy external route has no auth yet (Phase 2 SecurityPolicy).

Step 2 notes:

- **Left out: listener `https-runners-staging-forkline-dev`** (replaces `tls-bridge-runners` only
  once calypso emits HTTPRoutes). All other listeners of the contract are deployed (the internal
  `https-isidoro-grigri-cloud` came in step 9).
- One Certificate per listener (7). A first version shared one Certificate between two
  listeners (apex plus wildcard, `mintpsicologia.es` plus `www`): Envoy Gateway then sets
  `OverlappingTLSConfig` (HTTP/2 coalescing risk), so they were split. `https-grigri-cloud`
  keeps one Certificate with the four wildcard SANs.
- Gateway-wide `BackendTrafficPolicy` carries only the 60s request timeout: no `compressor`
  gateway-wide on purpose (per route, never on S3 hosts). `ClientTrafficPolicy`: ALPN, `http10`,
  `KeepUnchanged`, idle timeout 600s, TCP keepalive, HSTS and security headers, `Server` removed;
  internal adds `xForwardedFor.numTrustedHops: 1`.
- JSON access log to stdout is on; `traceID` is filled since step 4 (external gateway only).
- Internal HPA uses `ContainerResource` metrics on the `envoy` container (the pod also runs the
  shutdown-manager sidecar, which has no requests).

Step 4 notes (monitoring, tracing, roll test; all verified 2026-11-09):

- **Scraping**: controller `Service envoy-gateway` port `metrics` (19001, `/metrics`) via
  ServiceMonitor; proxies have no Service for stats, so a PodMonitor scrapes container port
  `metrics` (19001, `/stats/prometheus`) and copies the owning-gateway pod label to a `gateway`
  label (`internal` / `external`). Both carry `release: monitoring`. 5 targets up.
- **Dashboards**: `envoy-proxy-global`, `envoy-clusters`, `envoy-gateway-global` from
  `charts/gateway-addons-helm/dashboards` at v1.9.3, as a `configMapGenerator` (sidecar label,
  folder `envoy-gateway`, ServerSideApply for size). Local edits: hardcoded datasource uid
  replaced by the `$datasource` variable, controller namespace default `envoy-gateway`. Bump them
  with the chart version.
- **Alerts** (`resources/prometheus-rules.yaml`, same thresholds, `for` and `severity` labels as
  `system/ingress-nginx-external/rules/alerts.yaml`): `IngressNginxExternalHighErrorRate` ->
  `EnvoyGatewayHighErrorRate`, `...CriticalErrorRate` -> `EnvoyGatewayCriticalErrorRate`,
  `...HighLatency` -> `EnvoyGatewayHighLatency` (p95 > 5000 ms, `cross-backups` excluded by
  cluster name `httproute/cross-backups/.*`). Envoy has no per-host metric: grouping is by
  `gateway` and `envoy_cluster_name` (`httproute/<ns>/<route>/rule/<n>`), from
  `envoy_cluster_upstream_rq_xx` and `envoy_cluster_upstream_rq_time_bucket`. Unmatched requests
  (404 no route) are not in these series. Covers both gateways (nginx-internal had no alerts).
- **Tracing**: external gateway only, like nginx (`nginx-internal` does not trace).
  `EnvoyProxy.spec.telemetry.tracing`: OpenTelemetry to
  `otel-collector-opentelemetry-collector.otel-collector:4317`, `serviceName: envoy-external`,
  `samplingRate: 100` (nginx `AlwaysOn`), tags `http.host` and `upstream.cluster`; a
  `ReferenceGrant` in `otel-collector` allows the cross-namespace backendRef. The collector
  treats `envoy-external` like `nginx-external` (tail sampling 100%, SERVER to CLIENT span kind).
  Loki gets a `traceID` derived field (the Envoy JSON key has a capital `ID`). Tempo needed no
  change. Evidence: a request with a `traceparent` through `.14` appears in Tempo as service
  `envoy-external`, root span `ingress`, parented to the incoming span id, with the same trace id
  in the access log line. Envoy exports spans for every request, including 404s.
- **Tracing does not roll pods** (dynamic xDS config). The roll test therefore rides on a no-op
  pod annotation `homelab.grigri.cloud/roll-test` on both EnvoyProxies (internal in `71d00f34e`,
  external in `1ba494e72`). It can go with the next real pod template change.
- **Roll test (Gate 1)**: ~10 req/s per VIP (3 parallel `curl` loops, 404 from Envoy, 2 s connect
  and 5 s total timeout) from the workstation, logged from before the first roll until 2 minutes
  after the last pod was replaced: **3241 requests to `.14`, 3236 to `.13`, 0 failures** (no code
  000, no timeouts, none during either roll). 4 probes took about 1 s (SYN retransmit) at two
  instants outside the roll windows, hitting both VIPs at the same time (client side). Timeline
  (UTC): internal 20:50:04-20:50:38, external 20:51:54-20:52:27; each roll: new pod created, Ready
  about 11 s later (`maxSurge: 1`, `maxUnavailable: 0`), old pod gets SIGTERM only then and exits
  about 11 s later (shutdown-manager `minDrainDuration` 10 s; no open connections to drain), next
  new pod follows. `terminationGracePeriodSeconds` 360, `--drain-time-s 60`. No tuning needed.
  Not covered: long-lived connections (websockets, big transfers) across a roll, which drain up to
  60 s and are then cut.

### Phase 2 progress: auth pilots (2026-11-09)

Pattern (recipe 1, procedure in `docs/conventions/gateway-api-routes.md` "Authentication"): one
`KanidmOAuth2Client` per app in its namespace (secret alias `client-secret`, no `ReferenceGrant`),
one `SecurityPolicy` per protected route, skip paths as separate same-host routes without a policy.
Every Ingress untouched; nginx `.1` / `.4` answers identical before and after each commit; every
route and policy Accepted; no proxy restart.

| App                                | Commit                                    | Result                                                                                                                                                                                                                                                                                                                       |
| ---------------------------------- | ----------------------------------------- | ---------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------- |
| kaniop (operator restart)          | `88ea9d945`                               | Kaniop enables `HTTPRoute` / `BackendTLSPolicy` support **only if the APIs exist at operator start** (`check_api_queryable_optional`); it started before the Gateway API CRDs, so `spec.gateway` would have been ignored. A pod annotation marker in `system/kaniop/values.yaml` restarts it (bump after CRD installs). Kanidm StatefulSet untouched. |
| kanidm (recipe 12)                 | `e02d10b8d`                               | `spec.gateway` on `external` / `https-grigri-cloud` (HTTPRoute `kanidm`, `v1`), `spec.backendTlsPolicy` emits `gateway.networking.k8s.io/v1` (hostname `idm.grigri.cloud`, `wellKnownCACertificates: System`, Service port `https`); raw `BackendTrafficPolicy` SourceIP consistent hash (nginx `upstream-hash-by $remote_addr`), merged with the gateway defaults. `spec.ingress` kept. 7 paths (UI, login, discovery, JWKS, CSS, authorize error) byte-identical on `.14` and `.4`; one client sticks to one replica through both proxies. |
| http-echo (external, headers)      | `467ba620c`                               | 302 to `/ui/oauth2` with `client_id=http-echo`, `redirect_uri=https://http-echo.grigri.cloud/oauth2/callback`, `scope=openid profile email groups`, S256 challenge, state; nonce and verifier cookies `Secure; HttpOnly`, path `/oauth2/callback`, 600 s. Forged id token cookie, forged HMAC, forged bearer, spoofed `X-Auth-Request-*`: all 302. `/oauth2/logout` clears the five cookies, 302 `/`. Wrong state / no cookies on the callback: 401. Groups: the three `k8s-oauth2-proxy` scope map groups (nginx had no `allowed_groups`). Internal route stays without auth (as nginx-internal). |
| esphome (internal)                 | `2f0d84fc0`                               | Route `esphome-internal` (`forceRename`), group `esphome-users` (exists in Kanidm: scope map accepted). Same probes on `.13`: 302 with `client_id=esphome`, forged/spoofed 302, logout clears cookies. No identity headers (nginx passed none). |
| navidrome (skip paths, Lua)        | `885e0d580`, `10bd7446f`                  | Four routes: `navidrome-{internal,external}` (policy, `X-Auth-Request-Preferred-Username` from `preferred_username`) and `navidrome-{internal,external}-subsonic` (`/rest`, `/share`, no policy, `X-Auth-Request-*` removed). Ten paths compared with nginx on both hosts: identical, except `/restx` now needs login (nginx's skip regex had no segment boundary). External `/api` answers 401 without redirect (`denyRedirect` on `:path`, as nginx's `api` Ingress without `auth-signin`). Spoofed `X-Auth-Request-Preferred-Username` on `/rest/ping.view`: Subsonic `missing parameter: 'u'`, same as nginx. Lua body rewrite (`EnvoyExtensionPolicy`, only uncompressed `text/html`, `Accept-Encoding` removed on the UI route) Accepted, not yet exercised on real HTML (needs a login). |
| radarr external (apiKey skip, Lua) | `8271a906f`, `4b56f4801`                  | Raw routes in `templates/httproute-external.yaml` (the internal ones untouched): protected catch-all and movie list (one policy, group `radarr-users`), `radarr-external-apikey` (`/api/v3/.*` + `queryParams apiKey .+`) and `radarr-external-apikey-movie-list` (`(?-i)/api/v3/movie`), no policy. **`queryParams` regex works.** 10 cases identical to nginx (wrong case `apikey`, empty value, `/api/v3?apiKey`, `/API/...` all 302; key present 401 from radarr). Lua: access log path gains `&excludeLocalCovers=true`, explicit `false` left alone. |

Findings:

- **EG regex route order**: regex routes sort by regex length, then query match count (EG
  `sort.go`). The first radarr version let `/API/v3/movie?apiKey=` skip auth through the longer
  `(?i)` skip regex; `(?-i)` fixed it (`4b56f4801`).
- **`claimToHeaders` appends, but `jwt_authn` first removes every configured claim header**
  (Envoy `sanitizeHeaders`), so a protected route overwrites spoofed values. Route-level header
  removal cannot be used there (it runs in the router, after `jwt_authn`).
- **Client secret and HMAC delivered**: a callback with the real state and a bogus code reaches
  Kanidm's `handle_oauth2_token_exchange` and fails on the code (`Failed to deserialise a valid
  JWE`), for all four clients on both gateways. The token exchange and JWKS go through cluster DNS
  to `idm.grigri.cloud` (today pfSense `.4`); JWKS fetch success 3, failure 1 (a startup race,
  `cluster ... is not configured`, retried).
- **#10075**: no Envoy restart on any proxy across 6 OIDC policy creations or updates. Each proxy
  logged `initial fetch timed out for ...tls.v3.Secret` twice at 21:26:51 (navidrome, policy
  became valid when the Kaniop Secret appeared 15 s earlier); the bogus-code check afterwards
  passed, so the secrets arrived. See Open Items.
- **Security headers differ from nginx** (not auth, Phase 1 platform): the nginx ConfigMaps put the
  four headers under `configuration-snippet`, which is not a valid ingress-nginx ConfigMap key, so
  nginx never sent them (`.4` sends only HSTS). Envoy's `lateResponseHeaders.set` now adds them on
  every app and **overrides the app's own values** (Kanidm `Referrer-Policy: origin` and
  `Permissions-Policy` replaced, `X-Frame-Options: DENY` added everywhere, which can break apps
  embedded in iframes). Decide: keep (new hardening, check iframe users), or `addIfAbsent`, or drop.
- **Kanidm replica restarts during a reindex** (pre-existing, not caused by the route):
  `kanidm-default-1` started a replication-triggered `System reindex` at 21:16:29, failed probes and
  was liveness-killed 4 times until 21:21:04; `kanidm-default-0` served throughout, no 5xx on the
  nginx path. Consumer reindexes run 10-28 times per hour per replica since at least 2026-11-08.
  The StatefulSet has no startupProbe and a 1 s probe timeout. Owner follow-up.
- Envoy logs the raw cookie value when it cannot decrypt a token cookie (`failed to decrypt
  token:`): treat proxy logs as sensitive.

Interactive login: **not done**. The agent browser (passless, RP `idm.grigri.cloud`) stopped at
"User Verification Required" (no PIN set, `require_uv`); not retried. Owner test in Gate 2.

### Phase 2 progress: pilots without auth (2026-11-09)

All routes are internal (`internal` / `https-internal-grigri-cloud`, no external-dns annotation),
every Ingress is untouched, every Argo app Synced Healthy, every route and policy Accepted and
ResolvedRefs, no `response_flags` other than `-` in the Envoy access log, client IP preserved
(`client_address` is the LAN address), external-dns planned no record change for these hosts. No
NetworkPolicy or CiliumNetworkPolicy exists in any of the six namespaces, so none was needed.

| App (style)                          | Commit      | Result                                                                                                                                                                                                                                                             |
| ------------------------------------ | ----------- | ------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------- |
| firecrawl (A, app-template `route:`) | `3ff1f435c` | `firecrawl.internal` 200 h2 on `.13`, same JSON body as `.1`; cert `*.internal.grigri.cloud` verifies, ALPN h2, HSTS and security headers, no `Server`.                                                                                                            |
| rsshub (kustomize, raw)              | `1b918d1d6` | Raw `HTTPRoute` in `resources/` (kustomize has no cluster capabilities, so no chart `route:`). `/`, `/healthz`, `/test/1` (1911 B XML) identical on `.13` and `.1`.                                                                                                |
| hubble (cilium, raw)                 | `671ec0200` | Raw `HTTPRoute` in `system/kube-system/resources/cilium/`, `hubble.ui.ingress` untouched. UI HTML (642 B) identical on `.13` and `.1`. gRPC-web streaming not exercised (see below).                                                                              |
| vault (operator CR, standalone)      | `83f440b77` | `HTTPRoute` + `BackendTLSPolicy` (`Secret vault-tls` `ca.crt`, hostname `vault.vault.svc.cluster.local`, port `api-port`); `spec.ingress` kept. `/v1/sys/health` 200 h2 through `.13` with the same body as `.1`, also without `-k`; `/v1/pki/iot/ca/pem` returns the `iot.grigri` CA on both; `/ui/` 200. Proxy config dump shows SNI, SAN match and the SDS CA `vault/vault-ca`. |
| unifi (self-signed backend)          | `1fe3f7726` | EG `Backend` (FQDN `unifi.unifi.svc.cluster.local:8443`, `insecureSkipVerify`) as backendRef. `/` 302 `/manage`, `/status` JSON `rc: ok` on `.13` and `.1`. Same trust as nginx.                                                                                  |
| radarr (Lua request case)            | `8de64d1dc` | Second `HTTPRoute` `radarr-movie-list` (path `RegularExpression (?i)/api/v3/movie`, wins over the catch-all `/`) + `EnvoyExtensionPolicy` with the Lua. Proof with the API key (header, never printed): no param 8129801 B / 0 local images = explicit `excludeLocalCovers=true`; explicit `false` 8367716 B / 4034 local images; identical on `.1`. `/API/V3/Movie` (case) filtered too, `/api/v3/movie/1` and `/api/v3/movies` 404 as on nginx, `?tmdbId=` passes through. |

Skipped on purpose: **`radarr.grigri.cloud` external route** (oauth2-proxy `auth-url`, `auth-snippet`
apiKey bypass, buffer 16k): not created here, done by the auth pilots below (`8271a906f`).

Findings:

- Lua works (`enableLua`), a second same-host HTTPRoute with a regex path overrides the catch-all
  `/`, and `(?i)` regexes work in route paths.
- `BackendTLSPolicy` accepts `kind: Secret` CA references (key `ca.crt`, same namespace); `sectionName`
  is the Service port name.
- Routes get `timeout: 60s` plus `idle_timeout: 3600s` and `upgrade_configs: websocket`. Whether an
  upgraded websocket survives the 60 s timeout is **not yet verified** (no websocket test target:
  `http-echo` has no websocket endpoint). Needs a held-connection test on the first websocket app
  (Home Assistant, UniFi events, jellyfin) in Phase 3.
- Hubble UI uses gRPC-web streaming on `/api`; the probe path 404s on nginx and Envoy alike, so the
  stream itself still needs a browser check before the switch.
- Procedure written: `docs/conventions/gateway-api-routes.md`; `helm.md` and
  `deploying-new-apps.md` now point to it instead of the nginx snippet guidance.

### Phase 0 findings (2026-11-09)

- `.13` and `.14` are free on the cluster side too.
- Internal switch coverage: 42 of 45 internal hosts move (annotation `external-dns.kubernetes.io/enabled=true`).
  Three use the `external-dns.alpha.kubernetes.io/enabled` key, which the external-dns filter
  ignores, so their Cloudflare records (`.1`) are never updated and will not move:
  `idm.staging.internal.forkline.dev` (multivara `keycloak`), `vault.staging.internal.forkline.dev`
  (multivara `calypso`), `idm-internal.isidoro.grigri.cloud` (kiais `isidoro-keycloak`). Owners
  must switch to the non-alpha key (or the records are edited by hand) before Phase 4.
- Records that must not move (Service-sourced): `ldaps.idm.internal` (.8), `mosquitto.internal`
  (.9), `mosquitto-tls.internal` (.10), `transcoder-rabbit.internal` (.3), `turn` (.5, CNAME),
  `*.app.staging.forkline.dev` (CNAME, from the nginx-external Service). No `DNSEndpoint` exists yet.
- Pods resolve via nodelocaldns/CoreDNS -> node resolv.conf -> pfSense: `idm`, `git`, `auth`,
  `grafana` = `.4` (override); `mcp` = public IP (NAT reflection); `*.internal` = `.1` (Cloudflare).
- External Secrets uses `https://vault.vault:8200` (unaffected). **cert-manager `iot` ClusterIssuer
  uses `https://vault.internal.grigri.cloud` on purpose** (Vault custom CA): the vault route must
  be verified before Phase 4. Other ingress-path consumers: kanidm backups and velero cross-backup
  rclone (`s3.internal`), velero `publicUrl`, unpackerr (`sonarr/radarr.internal`), self URLs and
  OAuth redirect origins.
- TLS secret consumers: only `kanidm/idm-external-tls-certificate` (SANs `idm.grigri.cloud`,
  `ldaps.idm.internal.grigri.cloud`), mounted by the kanidm pods, owned by the kaniop-generated
  Ingress: needs an explicit `Certificate` before Phase 6.
- Fourth Ingress source: **kiais/gitops** (`tea.millaguie.net`, third-party owner) owns `isidoro`
  (external) and `isidoro-keycloak` (internal + external, operator-generated). multivara/gitops owns
  calypso (5 + vault), keycloak (2), gitlab. These need routes in their repos before the
  corresponding switch: only the runner wildcard is bridged, so a host without a route 404s on
  `.14` after P5.1. Full analysis: [Tenant repos on grigri](#tenant-repos-on-grigri-multivara-kiais).
- The alpha key is not limited to the three internal hosts: **every tenant Ingress** (external
  ones included) uses `external-dns.alpha.kubernetes.io/*`, so their records are frozen. The
  external ones are CNAMEs to the apex and need no move; `idm.isidoro.grigri.cloud` is the one
  external tenant record that is an **A to `.4`** and must be moved by the route annotation at P5.

#### Envoy Gateway and Gateway API (researched 2026-11-09, read-only)

| Topic                  | Result                                                                                                                                                                                                                                                                                                                                                                                                                                                        |
| ---------------------- | ------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------- |
| Envoy Gateway version  | **v1.9.3** (2026-10-26, supported until 2027-02-14, tested on k8s 1.33-1.36; live is 1.35.8). v1.10.0 (2026-11-06) is too new                                                                                                                                                                                                                                                                                                                                 |
| Envoy image            | v1.9.3 defaults to `distroless-v1.39.5`, which still trusts the compromised eMudhra CAs. Pin `docker.io/envoyproxy/envoy:distroless-v1.39.6` (`global.images.envoyProxy.image`, and `envoyDeployment.container.image` on each EnvoyProxy, which replaces)                                                                                                                                                                                                     |
| Gateway API            | v1.6.1 bundled. TCPRoute, UDPRoute GA in 1.6, TLSRoute standard since 1.5                                                                                                                                                                                                                                                                                                                                                                                     |
| CRD install            | `oci://docker.io/envoyproxy/gateway-crds-helm` (`crds.gatewayAPI.enabled`, `.channel: experimental`, `crds.envoyGateway.enabled`) plus `gateway-helm` with `crds.enabled: false`. CRDs are in `templates/`, so no `includeCRDs`                                                                                                                                                                                                                               |
| CRD channel            | Experimental kept: standard would be enough for `v1` TCPRoute/TLSRoute, but experimental also serves `v1alpha2` (needed by calypso and the Forkline Remote relay)                                                                                                                                                                                                                                                                                             |
| Safe-upgrades policy   | The main chart's `ValidatingAdmissionPolicy` (`failurePolicy: Fail` on all CRD writes) goes away with `crds.enabled: false`                                                                                                                                                                                                                                                                                                                                   |
| ServerSideApply        | Required: `envoyproxies` 649 KB, `securitypolicies` 275 KB, `httproutes` 299 KB (experimental) exceed the 262144-byte last-applied limit. Same as `system/tempo/kustomization.yaml:36`. Upstream's Argo guide uses it too                                                                                                                                                                                                                                     |
| Chart values           | `config.envoyGateway.extensionApis.enableBackend` and `enableLua` (Lua is off by default since v1.9.0). `topologyInjector.enabled` defaults true (turn off). No ServiceMonitor in the chart (metrics on controller Service port 19001)                                                                                                                                                                                                                        |
| Argo CD 3.6.1 health   | Built in for Gateway, GatewayClass, HTTPRoute, GRPCRoute, TLSRoute, BackendTLSPolicy, ListenerSet and the Envoy Backend, BackendTrafficPolicy, ClientTrafficPolicy, EnvoyExtensionPolicy, EnvoyPatchPolicy, SecurityPolicy. **None** for TCPRoute, UDPRoute, ReferenceGrant, EnvoyProxy, HTTPRouteFilter (they show Healthy without a check; fine)                                                                                                            |
| publish-status-address | Confirmed on the forkline chart: `controller.publishService.enabled: false` plus `extraArgs.publish-status-address=192.168.193.13` renders `--publish-status-address`. The two flags are mutually exclusive (controller exits if both are set); `--update-status` must stay true; the leader syncs status every 60s, so expect about 1 minute after a rollout; `--update-status-on-shutdown` only clears status with a single pod                             |
| app-template `route:`  | Supports HTTPRoute, GRPCRoute, TCPRoute, TLSRoute, UDPRoute, several routes per release, `tpl` in hostnames. It picks `v1` only if `gateway.networking.k8s.io/v1/HTTPRoute` is in `Capabilities.APIVersions`: local renders need `--api-versions gateway.networking.k8s.io/v1/HTTPRoute` (`scripts/deploy-dir.rh:26-31` passes only the Ingress one); Argo's discovery is expected to give `v1` (unchecked). A `RequestRedirect` rule cannot have backendRefs |

Native route support per component (B group); everything else is a raw `HTTPRoute` template:

| Component                              | Native key                                                                                       | Gotcha                                                                                                                                                                                                                                                                                                                    |
| -------------------------------------- | ------------------------------------------------------------------------------------------------ | ------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------- |
| argo-cd 10.17.0                        | `server.httproute.*`                                                                             | Shared hostnames: native for `argocd.internal`, raw route for the external `/api/webhook` on `argocd-server:80` (`server.insecure` is set)                                                                                                                                                                                |
| forgejo 17.3.2                         | `httpRoute.*`, `tcpRoute.*` (creates `<fullname>-ssh`, `v1alpha2`)                               | `httpRoute.matches.timeouts` renders invalid: do not use it, set timeouts in Envoy policies. Values sit under `forgejo:`                                                                                                                                                                                                  |
| minio 5.4.0                            | none (raw)                                                                                       | Disable chart `ingress` and `consoleIngress`                                                                                                                                                                                                                                                                              |
| kube-prometheus-stack 92.22.0, grafana | `prometheus.route.main`, `alertmanager.route.main`, `grafana.route.main`                         | Disable the matching `ingress` blocks; grafana is external today                                                                                                                                                                                                                                                          |
| loki 20.4.1, prometheus-long-term      | raw                                                                                              | Replace both raw Ingresses; ports are named `http-metrics` and `http-web`                                                                                                                                                                                                                                                 |
| oauth2-proxy 10.7.2                    | `gatewayApi.*`                                                                                   | Single `gatewayRef`; set `rules[].matches` to `PathPrefix /oauth2` (default is `/`). Removed in Phase 6 anyway                                                                                                                                                                                                            |
| ollama 1.95.0                          | `httpRoute.*`                                                                                    | No `annotations` key; `hostnames` defaults to `ollama.local`, always override                                                                                                                                                                                                                                             |
| open-webui 17.5.0                      | `route.*`                                                                                        | `httpsRedirect` adds a second route defaulting to `sectionName: http`                                                                                                                                                                                                                                                     |
| bitnami wordpress 34.2.6               | `wordpress.httpRoute.*` (internal host)                                                          | `parentRefs` defaults to a Gateway named `gateway`: always set. Pulling from `charts.bitnami.com` fails (pre-existing); OCI `registry-1.docker.io/bitnamicharts/wordpress` works. External and www redirect stay raw                                                                                                      |
| cilium 1.20.4 (hubble UI)              | none (raw)                                                                                       | Set `hubble.ui.ingress.enabled: false`                                                                                                                                                                                                                                                                                    |
| Kaniop Kanidm CR 0.21.1                | `spec.gateway.*` (HTTPRoute `kanidm`, optional `backendTlsPolicy`), coexists with `spec.ingress` | Default backend port 8443 hard-coded; no BackendTrafficPolicy/ClientTrafficPolicy, so the source-IP hash is a separate raw CR. Before removing `spec.ingress`: set `spec.tlsSecretName: idm-external-tls-certificate` and add an explicit `Certificate` (keeps the `ldaps.idm.internal` SAN), else the pods fail to start |
| Vault operator CR 1.24.1               | none (`spec.ingress` only)                                                                       | Remove `spec.ingress`, add raw HTTPRoute and `BackendTLSPolicy`. The operator has no delete path: removing `spec.ingress` **leaves an orphan Ingress** that Argo will not prune                                                                                                                                           |

Backend Service and port per host (confirmed against live Ingresses and Services):

| Host                                                         | Service : port                                                                                   |
| ------------------------------------------------------------ | ------------------------------------------------------------------------------------------------ |
| `argocd.internal`, `argocd.grigri.cloud` `/api/webhook`      | `argocd/argocd-server` : 80                                                                      |
| `git.grigri.cloud`                                           | `git/git-forgejo-http` : 3000 (`http`); SSH `git-forgejo-ssh` : 22                               |
| `s3` / `mc-s3` internal                                      | `minio/minio` : 9000, `minio-console` : 9001                                                     |
| `s3` / `mc-s3` external                                      | `s3-public/s3-public-minio` : 9000, `s3-public-minio-console` : 9001                             |
| `cross-backups` / `mc-cross-backups`                         | `cross-backups/cross-backups-minio` : 9000, `cross-backups-minio-console` : 9001                 |
| `prometheus.internal`, `alertmanager.internal`               | `monitoring/monitoring-kube-prometheus-prometheus` : 9090, `...-alertmanager` : 9093             |
| `prometheus-long-term.internal`                              | `monitoring/prometheus-long-term` : 9090                                                         |
| `grafana.grigri.cloud`                                       | `monitoring/monitoring-grafana` : 80                                                             |
| `loki.internal`                                              | `loki/loki` : 3100 (`http-metrics`)                                                              |
| `auth.grigri.cloud` `/oauth2`                                | `oauth2-proxy/oauth2-proxy` : 80                                                                 |
| `ollama.internal`                                            | `ollama/ollama` : 11434                                                                          |
| `open-webui.grigri.cloud`                                    | `open-webui/open-webui` : 80                                                                     |
| `mintpsicologia.internal`, `mintpsicologia.es`, `www.` (301) | `mintpsicologia/mintpsicologia-wordpress` : 80 (www: `RequestRedirect` route, no backend)        |
| `hubble.internal`                                            | `kube-system/hubble-ui` : 80                                                                     |
| `idm.grigri.cloud`                                           | `kanidm/kanidm` : 8443 (`https`, headless, backend TLS required; ldaps 3636 on the same Service) |
| `vault.internal`                                             | `vault/vault` : 8200 (`api-port`), TLS backend (hostname = in-cluster FQDN)                      |

Auth, Kanidm and external-dns results (applied in recipe 1 and the Target Design below):

| Topic                | Result                                                                                                                                                                                                                                                                                                         |
| -------------------- | -------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------- |
| PKCE                 | No field; Envoy always sends S256 (verifier cookie `CodeVerifier-<suffix>`), Kanidm accepts it: no PKCE opt-out anywhere. Token auth is `client_secret_basic`                                                                                                                                                  |
| OIDC discovery       | Runs on every translation, no cache between rounds. A failure marks the policy not accepted and routes return 500 until a later reconcile; the last-good fallback arrives in v1.10. Set `provider.authorizationEndpoint` and `tokenEndpoint` explicitly: discovery is skipped                                  |
| Kanidm URLs          | Issuer `https://idm.grigri.cloud/oauth2/openid/<client>`, JWKS `.../<client>/public_key.jwk`, authorize `https://idm.grigri.cloud/ui/oauth2`, token `https://idm.grigri.cloud/oauth2/token`, ID tokens ES256, no `end_session_endpoint` (Kanidm 1.11.2, Kaniop 0.21.1)                                         |
| Groups claim         | Scope `groups` emits UUIDs and SPNs (`name@idm.grigri.cloud`) in one flat `groups` array; `groups_spn` emits SPNs only. Authorization rules match the SPN values                                                                                                                                               |
| ID token from cookie | The oauth2 filter runs before jwt_authn and decrypts the cookies. Set `oidc.cookieNames.idToken: <fixed>` and `jwt.providers[].extractFrom.cookies: [<fixed>]` (default name has a random suffix)                                                                                                              |
| Client secret        | EG wants key `client-secret`; Kaniop's secret has `CLIENT_ID`/`CLIENT_SECRET`: use `secretKeyAliases.clientSecret: [client-secret]`. Cross-namespace needs a `ReferenceGrant` from `SecurityPolicy` (group `gateway.envoyproxy.io`)                                                                            |
| `claimToHeaders`     | Array claims are not supported. If a groups header is needed, `claimMap[].joinStrategy: csv` on the Kaniop client makes a string claim                                                                                                                                                                         |
| directResponse       | `directResponse.header.set`/`add` work (`Location` included); `remove` is ignored                                                                                                                                                                                                                              |
| queryParams regex    | `RegularExpression` supported (full match), maps to Envoy `safe_regex`                                                                                                                                                                                                                                         |
| BackendTrafficPolicy | `compression` is deprecated: use `compressor: [{type: Brotli}, {type: Gzip}]` (mutually exclusive with `compression`). `timeout.http.requestTimeout: 0s` works; a route's own `timeouts.request` wins. Local rate limit has no burst field (bucket = `requests`)                                               |
| external-dns v0.23.0 | `--annotation-filter` applies to routes. `target` is read only from Gateways/ListenerSets, never routes. Default target is Gateway `status.addresses` (`.13`/`.14`, from the LB Service). Chart RBAC follows `sources`. Do not add `gateway-tcproute` (reads `v1alpha2`; forgejo SSH shares the `git.` record) |
| cert-manager 1.21.3  | Nothing needed: explicit Certificates in `envoy-gateway` issued by `letsencrypt-prod-dns`, same-namespace `certificateRefs` need no `ReferenceGrant`                                                                                                                                                           |

## Autonomy Scope (agreed with the owner 2026-11-09)

**Autonomous, no check-backs** (no real traffic moves; nginx keeps serving `.1`/`.4`):

- Phase 0 read-only work; docs and plan updates committed and pushed.
- Phase 1 additive platform commits, one at a time, each followed by Argo `Synced Healthy` and the
  step's checks before the next.
- Phases 2-3: internal routes (no DNS annotation) and external routes (same CNAME annotations as
  today) app by app, with policies, Lua, Kaniop OAuth2 client changes in git; verify against
  `.13`/`.14`.
- Rollback of my own push: revert commit and push immediately, then report.
- Stop and alert the owner when: Argo stays Degraded after a revert, an existing app's error rate
  moves on nginx, an alert fires that could be ours, or a CRD conflicts.

**Needs the owner's explicit yes, each time:** comments on multivara#199 or any change in
multivara/kiais repos or calypso; the manual delete of the orphan Ingress the Vault operator
leaves behind after `spec.ingress` is removed (it has no delete path); the Phase 4 internal
switch commit (then executed, verified and rolled back autonomously); Phase 6 decommission;
anything outside this plan.

**Owner only:** pfSense edits (P4.1, P5.x, P6.x). The agent prepares exact steps and verifies
read-only before and after.

**Rules from `AGENTS.md`:** commit first, then push; never `kubectl apply/patch/delete/edit`,
never `helm install/upgrade`; read-only `kubectl --context=grigri`; no attribution lines in
commits; public repo hygiene. Reports: one hand-back per phase, or when stopped.

## Context & Current State

Facts gathered 2026-11-09 from this repo, a read-only look at the live cluster, a read-only look
at pfSense (`pfsense.grigri`, 2.8.1) and multivara/gitops.

### Controllers

|                      | internal                                                                     | external                                                                                                                                                               |
| -------------------- | ---------------------------------------------------------------------------- | ---------------------------------------------------------------------------------------------------------------------------------------------------------------------- |
| Argo app / namespace | `system/ingress-nginx` / `ingress-nginx`                                     | `system/ingress-nginx-external` / `ingress-nginx-external`                                                                                                             |
| Chart                | forkline `ingress-nginx` 2026.11.6 (OCI)                                     | same                                                                                                                                                                   |
| IngressClass         | `nginx-internal` (default)                                                   | `nginx-external`                                                                                                                                                       |
| LB VIP               | `192.168.193.1`, `externalTrafficPolicy: Local`                              | `192.168.193.4`, `Local`                                                                                                                                               |
| Replicas             | HPA 2-11                                                                     | 2                                                                                                                                                                      |
| Placement            | preferred `prusik` (100) and amd64 (50), spread `maxSkew: 4`, no tolerations | same                                                                                                                                                                   |
| Forwarded headers    | `use-forwarded-headers: true`                                                | `false` (real client IP via `Local`)                                                                                                                                   |
| Extras               |                                                                              | OTel tracing (`service.name=nginx-external`), `limit-req-status-code: 429`, trace ids in access log, external-dns wildcard `*.app.staging.forkline.dev` on its Service |

Shared controller config: snippets allowed (`annotations-risk-level: Critical`), brotli, HSTS,
global security headers (Referrer-Policy, `X-Frame-Options: DENY`, nosniff, Permissions-Policy),
`proxy_cache_path` zone `static-cache` (kroki), TCP map `29518 -> git/git-forgejo-ssh:22` on both.

### Cluster network

- 3 nodes: `prusik` (control plane), `grigri`, `k8s-amd64-1` (tainted `dedicated=ci:NoSchedule`,
  CI only).
- Cilium 1.20.4, kube-proxy replacement, BGP v2 (cluster ASN 64513, pfSense `192.168.192.1` ASN
  64512), pool `default` = `192.168.193.0/24`, every LB Service advertised unless labelled
  `cilium.io/bgp-skip`. Cilium's own Gateway API and ingress controller are **off** (no conflict).
- VIPs in use: .1 nginx-int, .2 unifi, .3 transcoder-rabbit, .4 nginx-ext, .5 coturn,
  .7 qbittorrent, .8 kanidm-ldaps, .9 mosquitto, .10 mosquitto-tls. pfSense aliases also name
  .6 (`k8s_deluge`) and .11 (`k8s_pt`), both unadvertised. **.12-.15 appear nowhere** in pfSense
  config or BGP.

### DNS: how names resolve today

| Names                                                                                                             | Resolved by                                                                                                                       | Answer            | Moved by                      |
| ----------------------------------------------------------------------------------------------------------------- | --------------------------------------------------------------------------------------------------------------------------------- | ----------------- | ----------------------------- |
| `*.internal.grigri.cloud`, `*.staging.internal.forkline.dev`                                                      | Cloudflare (public A records written by external-dns from the Ingress status); pfSense unbound just recurses, no rebind filtering | `192.168.193.1`   | the internal switch (Phase 4) |
| external names on the override list (idm, git, argocd, grafana, auth, ... 28 + forkline staging + mintpsicologia) | pfSense unbound host overrides (LAN and cluster pods)                                                                             | `192.168.193.4`   | pfSense P5.2                  |
| external names, from the internet                                                                                 | Cloudflare CNAME to `grigri.cloud` / `ddns.staging.forkline.dev` -> public IP -> pfSense NAT                                      | public IP -> `.4` | pfSense P5.1                  |
| external names not on the override list (mcp, photos, requests, ...) from LAN                                     | Cloudflare -> public IP -> NAT reflection                                                                                         | `.4`              | pfSense P5.1                  |
| `k8s.grigri` (`*.k8s.grigri`)                                                                                     | unbound custom option (redirect zone)                                                                                             | `.1`              | pfSense P4.1                  |

external-dns: Cloudflare, `sources: [service, ingress]`, `policy: upsert-only`, annotation filter
`external-dns.kubernetes.io/enabled=true`. Internal Ingresses carry no `target`, so the record is
the Ingress status address, which ingress-nginx sets from its LB Service (`.1`). The `DNSEndpoint`
CRD is already installed.

### pfSense (read-only inventory, 2026-11-09)

Not in git today; Phase 0 writes it to `docs/network/pfsense.md`.

- **WAN** `pppoe0`, single public IPv4, no WAN VIPs. No HAProxy or other proxy package: public
  ports are plain pf port forwards. NAT reflection on globally (pure NAT + NAT proxy helper).
- **Port forwards touching the cluster:**

| NAT rule                 | Ports (alias)                               | Target (alias)                           | Notes                                                                                                   |
| ------------------------ | ------------------------------------------- | ---------------------------------------- | ------------------------------------------------------------------------------------------------------- |
| grigri port redirect tcp | `external_ingress_tcp` = 80, 443, **29518** | `k8s_external_ingress` = `192.168.193.4` | the only ingress rule; WAN pass rule auto-associated; rdr also on every LAN-side interface (reflection) |
| TURN tcp/udp             | `turn_tcp`/`turn_udp`                       | `k8s_turn` = .5                          | coturn, untouched                                                                                       |
| qbittorrent              | `qbittorrent_tcp` = 50413                   | `k8s_qbittorrent` = .7                   | untouched                                                                                               |

- **Mosquitto (.9/.10) is not public.** No WAN NAT or pass rule; only reachable from LAN-side
  interfaces. It has its own VIPs and is not on the ingress path: this migration does not touch
  it.
- **No bandwidth limiter on `.4`.** `docs/conventions/bandwidth-protection.md` describes one as a
  recommendation; the only limiters configured are the guest Wi-Fi ones.
- **DNS Resolver (unbound) overrides:**

| Override                   | IP  | Aliases                                                                                                                                                                                                                                                              |
| -------------------------- | --- | -------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------- |
| `freshrss.grigri.cloud`    | .4  | 28 names: api.m.rajoy, m.rajoy, gitea, navidrome, wallabag, argocd, bazarr, jackett, sonarr, radarr, accounts, nextcloud, qbittorrent, jellyfin, idm, auth-kanidm, grafana, antdroid, auth, appdaemon, dawarich, atuin, git, gitlab, hass, isidoro, searxng, readest |
| `app.staging.forkline.dev` | .4  | auth, idm, api `.staging.forkline.dev`                                                                                                                                                                                                                               |
| `mintpsicologia.es`        | .4  | www                                                                                                                                                                                                                                                                  |
| `turn.grigri.cloud`        | .5  |                                                                                                                                                                                                                                                                      |
| `unifi-controller.grigri`  | .2  | unifi.grigri                                                                                                                                                                                                                                                         |
| custom options             | .1  | `local-zone: "k8s.grigri" redirect` + `local-data: "k8s.grigri A 192.168.193.1"`                                                                                                                                                                                     |

- **FRR/BGP:** route-map `allow-all` in/out, `next-hop-self`, `allowas-in` on neighbors
  `grigri` (.2) and `prusik` (.3). Neighbor `k8s-amd64-1` (.11) is Established without a
  route-map, so it exchanges no prefixes; harmless here because that node never runs a proxy. No
  `maximum-paths`. Every ingress VIP is learned from `prusik` today.
- Live states: 48 to `.4:443`, 0 to `.4:29518`, 0 to `.1`, 6 to `.9:1883`.

### Gateway API state

No Gateway API CRDs, no Envoy Gateway. cert-manager 1.21.3 without Gateway API support.

### Ingress inventory

98-99 Ingresses live (53-54 external, 45 internal; runner Ingresses come and go).

- **This repo:** 85 (43 ext, 42 int).
- **multivara/gitops** (`grigri.cloud/apps/`), 10 incl. one runtime runner (9 static; per-app
  analysis in the tenant section below):

| Ingress                                     | Class | Host                                                      | Notable                                                                                                                                                                                                       |
| ------------------------------------------- | ----- | --------------------------------------------------------- | ------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------- |
| calypso                                     | ext   | `app.staging.forkline.dev` `/`                            | oauth2-proxy auth (in-cluster), CSP snippet                                                                                                                                                                   |
| calypso-api                                 | ext   | `app.staging.forkline.dev` `/api`, `/webhooks`, `/public` | auth with `auth-snippet` skips, body 6m                                                                                                                                                                       |
| calypso-external-api                        | ext   | `api.staging.forkline.dev` `/v1(/\|$)(.*)`                | regex rewrite to `/public-api/v1/$2`, `limit-rps 20` x5 burst                                                                                                                                                 |
| calypso-landing-page                        | ext   | `staging.forkline.dev`                                    | auth, CSP snippet                                                                                                                                                                                             |
| oauth2-proxy                                | ext   | `app.staging.forkline.dev/oauth2`                         |                                                                                                                                                                                                               |
| keycloak-ingress                            | ext   | `idm.staging.forkline.dev`                                | backend HTTPS                                                                                                                                                                                                 |
| gitlab-webservice-default                   | ext   | `gitlab.grigri.cloud`                                     | 35G, 3600s, buffering off                                                                                                                                                                                     |
| vault                                       | int   | `vault.staging.internal.forkline.dev`                     | backend HTTPS                                                                                                                                                                                                 |
| keycloak-admin-ingress                      | int   | `idm.staging.internal.forkline.dev`                       | backend HTTPS                                                                                                                                                                                                 |
| **runners** (created by calypso at runtime) | ext   | `<runner>.app.staging.forkline.dev`                       | nginx `auth-url` to calypso-auth-proxy `?runner_id=`, Authorization passthrough, 3600s, buffering off; wildcard cert `calypso-runners/runners-subdomain`; DNS from the wildcard on the nginx-external Service |

- **kiais/gitops** (`tea.millaguie.net`, third party), 3: `isidoro` (ext), `isidoro-keycloak`
  `keycloak-ingress` (ext, operator-generated) and `keycloak-admin-ingress` (int). The
  `mcp-gateway` namespaces are this repo (`apps/mcp-gateway`, `apps/mcp-gateway-admin`).

Calypso builds runner Ingresses in
`crates/calypso-control-plane/src/runner_controller/k8s/runner/ingress.rs` with nginx-only auth
annotations (`runner_controller/service.rs:1164-1181`); its RBAC only grants `ingresses`. No
HTTPRoute support exists, and #199 does not cover it (hence the D4 bridge).

Annotation load (live, all repos):

| Feature                                                                           | Count | Envoy Gateway equivalent                       | Difficulty |
| --------------------------------------------------------------------------------- | ----- | ---------------------------------------------- | ---------- |
| `auth-url` / `auth-signin` (oauth2-proxy, per-app `allowed_groups`)               | 24    | `SecurityPolicy` OIDC + JWT authorization      | high       |
| `auth-snippet` bypasses (navidrome `/rest\|/share`, radarr `apiKey`, calypso-api) | 4     | extra route rule without the policy            | medium     |
| `auth-response-headers`                                                           | 9     | `SecurityPolicy` `claimToHeaders`              | medium     |
| `proxy-buffer-size`                                                               | 28    | none needed (Envoy header limits are larger)   | none       |
| `proxy-body-size`                                                                 | 20    | none needed (Envoy has no default body limit)  | none       |
| proxy timeouts                                                                    | 14    | `BackendTrafficPolicy` / route `timeouts`      | low        |
| `proxy-buffering off`                                                             | 14    | none needed (Envoy streams)                    | none       |
| `backend-protocol: HTTPS`                                                         | 8     | `BackendTLSPolicy` or EG `Backend` with TLS    | medium     |
| `rewrite-target` (regex)                                                          | 3     | `URLRewrite` / `HTTPRouteFilter` regex         | medium     |
| `configuration-snippet`                                                           | 16    | case by case (snippet table)                   | high       |
| `server-snippet`                                                                  | 9     | case by case (snippet table)                   | high       |
| `upstream-hash-by $remote_addr` (kanidm)                                          | 1     | `BackendTrafficPolicy` ConsistentHash SourceIP | low        |
| `permanent-redirect` (www.mintpsicologia.es)                                      | 1     | `RequestRedirect` 301                          | low        |
| `limit-rps` (calypso-external-api)                                                | 1     | `BackendTrafficPolicy` local rate limit        | low        |
| `whitelist-source-range`, GRPC, websocket annotations                             | 0     |                                                |            |

How Ingresses are defined in this repo (decides how routes get written):

- **A. app-template 5.3.0 `ingress:` block** (~40 apps): app-template already supports `route:`
  (HTTPRoute). Mechanical switch per app.
- **B. upstream chart values**: argocd, forgejo, minio, s3-public, cross-backups, monitoring
  (prometheus, alertmanager, grafana), oauth2-proxy, ollama, open-webui, mintpsicologia
  (wordpress), hubble UI, kanidm (Kaniop CR), vault (operator CR). Use the chart's own route
  support where it exists, else disable its ingress and add a raw `HTTPRoute` template.
- **C. raw `Ingress` manifests**: mcp-gateway, mintpsicologia external, tanque (2), nextcloud,
  rsshub, searxng, argocd webhook, loki, prometheus-long-term. Rewrite as raw `HTTPRoute`.

### Things that pin nginx today (break silently if missed)

- `apps/hermes/values.yaml:146-158` hostAliases `mcp.grigri.cloud -> 192.168.193.4`, plus tests
  `apps/hermes/tests/test_hostaliases_mcp_dns.py`, `test_render_enable.py`.
- NetworkPolicies admitting the nginx namespaces: `apps/mcp-gateway/templates/ciliumnetworkpolicy.yaml:65-72`,
  `apps/mcp-gateway-admin/templates/ciliumnetworkpolicy.yaml` (moved from `networkpolicy.yaml` by work in progress on 2026-11-09), `test/mcp-gateway/jobs/*.yaml`.
  **Live check 2026-11-09:** both render only with `networkIsolation: true` (off), so no active
  homelab policy admits only nginx today; they must gain the Envoy proxy selector before that
  flag is ever enabled. Cluster-wide, the only live policies naming the nginx namespaces are
  tenant ones: the operator-generated Keycloak NetworkPolicies in `keycloak` and
  `isidoro-keycloak` (8443 only from `ingress-nginx*`: Envoy gets 503 `UF`) and
  `calypso-runners/calypso-control-plane-runner-policy` (egress hairpin to
  `ingress-nginx-external`). Details and fixes in the tenant section.
- Monitoring: dashboards in `system/ingress-nginx/dashboards/`, alerts in
  `system/ingress-nginx-external/rules/alerts.yaml`, OTel tail sampling keyed on
  `service.name == "nginx-external"` (`system/otel-collector/values.yaml:58-111`), Loki trace id
  regex (`system/loki/resources/configmap-grafana-datasource-loki.yaml:23`), Tempo alert
  (`system/tempo/resources/prometheus-rules-telemetry-alerts.yaml:25`).
- In-cluster clients of ingress hostnames: many apps use `https://idm.grigri.cloud` as OIDC
  issuer, plus vault, git, s3 URLs. Pods resolve through pfSense, so `idm`, `git`, `argocd`,
  `grafana`, `auth` reach `.4` via the override and move at P5.2; `*.internal` names move at the
  internal switch.
- Ingress-owned certificates: cert-manager runs with `--enable-certificate-owner-ref=true`, so
  deleting an Ingress deletes its `Certificate` **and the TLS Secret**. Any workload mounting an
  ingress TLS secret breaks (suspect: kanidm `ldaps.idm.internal...` SAN on the ingress cert).
- Tenant coupling (multivara, kiais; section above): a frozen **A `.4`** record for
  `idm.isidoro.grigri.cloud` and frozen A `.1` records for three internal hosts (alpha key), nginx
  metrics in tenant alerts and dashboards, literal `192.168.193.4` and nginx pod labels in the
  unmerged calypso network policies.
- The external-dns wildcard `*.app.staging.forkline.dev` lives as annotations on the
  nginx-external **Service**; runner hosts depend on it.
- Docs that still teach nginx: `AGENTS.md` tech stack, `docs/conventions/helm.md:48-66`,
  `docs/conventions/deploying-new-apps.md:155-173`, README network section,
  `docs/conventions/bandwidth-protection.md` (names `.4`).

### Tenant repos on grigri (multivara, kiais)

Read-only analysis 2026-11-09: both repos at `origin/main`, the live cluster, `dig @1.1.1.1`.
The owners are "the multivara owner" and "the kiais owner (third party)". Every change below is an
**owner action in their repo**: the agent never writes to either repo, its forge or its issues
without that owner's explicit yes (Autonomy Scope). Unmerged work in progress in multivara
(`apps/calypso`, chart 0.52 rc, managed remote MCP and a test fixture) was read but is not
counted as deployed; where it matters it is marked WIP.

**How they are deployed**

|                                                                                                      | multivara/gitops                                                                                                                                                                                                                                                                                                                                                                                                      | kiais/gitops                                                                                                            |
| ---------------------------------------------------------------------------------------------------- | --------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------- | ----------------------------------------------------------------------------------------------------------------------- |
| Forge                                                                                                | Forgejo `git.grigri.cloud` (our own `git` app: Argo and the OCI chart pulls go through the ingress path)                                                                                                                                                                                                                                                                                                              | `tea.millaguie.net` (external, not touched by this migration)                                                           |
| Argo objects (ours, `bootstrap/root/templates/stack-*.yaml`, values in `bootstrap/root/values.yaml`) | ApplicationSets `multivara-apps` (`grigri.cloud/apps/*`: `calypso`, `gitlab`, `keycloak`) and `multivara-platform` (`grigri.cloud/platform/*`: `redis-operator`, `keycloak-operator`, no ingress)                                                                                                                                                                                                                     | ApplicationSet `kiais-apps` (`grigri.cloud/apps/*`: `isidoro`, `isidoro-config`, `isidoro-keycloak`)                    |
| Namespaces                                                                                           | directory name; calypso also owns `calypso-runners`                                                                                                                                                                                                                                                                                                                                                                   | directory name                                                                                                          |
| Sync                                                                                                 | automated `prune` + `selfHeal`, retry 10, `CreateNamespace`, project `default` (no restriction): a push to the tenant `main` is applied within minutes with no review on our side. An agent may only trigger the sync of an already pushed commit                                                                                                                                                                     | same                                                                                                                    |
| Ingress source                                                                                       | calypso chart `oci://git.grigri.cloud/multivara/helm-charts` (0.51 deployed, Ingress only, no route support), oauth2-proxy chart 10.7.2 (native `gatewayApi`), vendored gitlab chart 9.10.3 (native `gatewayRoute`), Keycloak and Vault (bank-vaults) operator CRs                                                                                                                                                    | isidoro chart `oci://tea.millaguie.net/kiais/helm-charts` 0.22.x (Ingress only, no route support), Keycloak operator CR |
| Env layout                                                                                           | `grigri.cloud/` is the **promotion source** for `nbg1-c01` (production, Hetzner, still ingress-nginx, not part of this plan): promrail copies everything outside `custom/` verbatim and `kustomization.yaml` must stay identical in both. Gateway API changes therefore go in `custom/resources` + `custom/patches` (with an empty placeholder file in `nbg1-c01`), never in the shared `values.yaml` or `resources/` | staging overrides live in `custom/patches` (same convention); whether kiais promotes elsewhere is unknown               |

Trust boundary, unchanged by this migration: any tenant commit can create routes on our Gateways
(`allowedRoutes.from: All`) for any hostname its listener matches, exactly as it can create an
Ingress today. The kiais owner is a third party, so a hostname lint (see Target Design) is cheap
insurance.

**Every ingress dependency (live and in git)**

| Repo | App (namespace)                                                                                      | Host -> backend                                                                                        | Today (class nginx-external unless noted)                                                                                                                                                                                               | Target                                                                                                                                                                                                                                                                                                                                     | Owner action                                                                                                                                 | By       |
| ---- | ---------------------------------------------------------------------------------------------------- | ------------------------------------------------------------------------------------------------------ | --------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------- | ------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------ | -------------------------------------------------------------------------------------------------------------------------------------------- | -------- |
| m    | calypso `calypso`                                                                                    | `app.staging.forkline.dev` `/` -> `calypso-web:80`                                                     | `auth-url` `http://oauth2-proxy.calypso.svc/oauth2/auth` + `auth-signin` redirect + `auth-response-headers` (`X-Auth-Request-*`), CSP `add_header`; cert `staging-forkline-dev-tls` (LE); DNS CNAME                                     | external `https-wildcard-staging-forkline-dev`; HTTPRoute, auth policy (decision A below), CSP via `ResponseHeaderModifier`                                                                                                                                                                                                                | routes in `custom/resources`; `ingress.enabled: false` in `custom/patches` (this flag also removes the next two Ingresses: one commit)       | P3       |
| m    | calypso `calypso-api`                                                                                | same host `/api`, `/webhooks`, `/public` -> `calypso-control-plane:8080`                               | as above, plus `auth-snippet` skips (`/api/v1/health`, `/webhook*`, `/public*`, `device-auth/start` and `/poll`), body 6m                                                                                                               | same listener; the skip paths as more specific rules without the auth policy, `/api` under it                                                                                                                                                                                                                                              | as above                                                                                                                                     | P3       |
| m    | calypso `calypso-external-api`                                                                       | `api.staging.forkline.dev` `/v1(/\|$)(.*)` -> `calypso-control-plane:8080`                             | regex rewrite to `/public-api/v1/$2`, `proxy-redirect` `/public-api/` -> `/`, `limit-rps 20` x5 burst, body 6m; cert `api-staging-forkline-dev-tls`                                                                                     | same listener; `HTTPRouteFilter` regex rewrite + local rate limit (recipe 13). `proxy-redirect` has no Envoy equivalent: confirm the API never redirects                                                                                                                                                                                   | as above                                                                                                                                     | P3       |
| m    | calypso `calypso-landing-page`                                                                       | `staging.forkline.dev` `/` -> `calypso-landing-page:80`, `/public` -> control plane                    | auth (staging is not public) + CSP; cert `landing-page-tls`; DNS CNAME to `ddns.staging.forkline.dev`                                                                                                                                   | external `https-staging-forkline-dev` (apex), HTTPRoute + auth + CSP                                                                                                                                                                                                                                                                       | `landingPage.ingress.enabled: false` + route                                                                                                 | P3       |
| m    | oauth2-proxy (`calypso`, **its own instance**, Keycloak realm `forkline`; not `system/oauth2-proxy`) | `app.staging.forkline.dev` `/oauth2` -> `oauth2-proxy:80`                                              | buffer 16k, no TLS block (host cert by SNI)                                                                                                                                                                                             | external wildcard listener; chart `gatewayApi` (`PathPrefix /oauth2`)                                                                                                                                                                                                                                                                      | `ingress.enabled: false` + `gatewayApi`                                                                                                      | P3       |
| m    | keycloak `keycloak-ingress` (CR `spec.ingress`)                                                      | `idm.staging.forkline.dev` -> `keycloak-service:8443` HTTPS                                            | backend TLS without verify, buffer 128k; cert `keycloak-external-ingress-tls`; DNS CNAME                                                                                                                                                | external wildcard listener; HTTPRoute + `BackendTLSPolicy` (hostname `keycloak.keycloak.svc`, the only SAN match; CA secret `keycloak-ca`)                                                                                                                                                                                                 | `spec.ingress.enabled: false` (confirm the operator deletes it: Vault's does not), NetworkPolicy fix (below)                                 | P3       |
| m    | keycloak `keycloak-admin-ingress` (static, class nginx-internal)                                     | `idm.staging.internal.forkline.dev` -> same backend                                                    | same, internal; DNS A `.1`, alpha key: **frozen**                                                                                                                                                                                       | internal `https-staging-internal-forkline-dev`, same `BackendTLSPolicy`                                                                                                                                                                                                                                                                    | route; non-alpha annotation on the route in the Phase 4 window                                                                               | P4       |
| m    | calypso `vault` (bank-vaults CR `spec.ingress`, nginx-internal)                                      | `vault.staging.internal.forkline.dev` -> `vault:8200` HTTPS                                            | backend TLS; cert `vault-tls-certificate`; DNS A `.1`, alpha: **frozen**                                                                                                                                                                | internal `https-staging-internal-forkline-dev`; HTTPRoute + `BackendTLSPolicy` (hostname `vault.calypso.svc.cluster.local`, CA = `vault-tls` key `ca.crt`)                                                                                                                                                                                 | remove `spec.ingress`; the operator leaves an **orphan Ingress** (owner-approved delete, as for our vault); annotation in the Phase 4 window | P4       |
| m    | gitlab `gitlab-webservice-default`                                                                   | `gitlab.grigri.cloud` -> `gitlab-webservice-default:8181`                                              | body 35G, 3600s, buffering off; cert `gitlab-wildcard-tls`; DNS CNAME to `grigri.cloud`; on the pfSense override list; in-cluster runner uses `https://gitlab.grigri.cloud`; SSH is not exposed (ClusterIP only)                        | external `https-grigri-cloud`; `gitlab.webservice.gatewayRoute` (`enabled: true`, `gatewayName: external`, `gatewayNamespace: envoy-gateway`, `sectionName: https-grigri-cloud`, per-rule `timeouts`), `global.ingress.enabled: false`; keep `global.gatewayApi.installEnvoy: false` so the chart creates no Envoy policies on our Gateway | values change, verify with `helm template`                                                                                                   | P3       |
| m    | runners (runtime, `calypso-runners`, 1 live)                                                         | `<runner>.app.staging.forkline.dev` -> `<runner>:opencode`                                             | Ingress built by the control plane: `auth-url` `calypso-auth-proxy:8080/auth?runner_id=`, `Authorization` passthrough, 3600s, no buffering, websockets; wildcard cert `runners-subdomain-tls`; DNS wildcard CNAME (ours, nginx Service) | now: passthrough bridge to nginx-external (**homelab-owned** `TLSRoute`, dies with nginx). Later: `https-runners-staging-forkline-dev` + per-runner HTTPRoute + ext auth (decision B)                                                                                                                                                      | calypso code emits HTTPRoute, RBAC for `httproutes` and policies                                                                             | P6       |
| m    | calypso `mcp-fixture` (**WIP**, unmerged)                                                            | `mcp-fixture.staging.forkline.dev` -> fixture:8080                                                     | new Ingress, non-alpha key, target `staging.forkline.dev`; no DNS record yet                                                                                                                                                            | external wildcard listener; HTTPRoute; DNS by `DNSEndpoint` (a route's `target` is not read)                                                                                                                                                                                                                                               | write the route in the same change as the Ingress                                                                                            | P3       |
| m    | Remote (#199, not deployed)                                                                          | `remote-relay.staging.forkline.dev` (HTTPRoute), `*.remote.staging.forkline.io` (TLSRoute passthrough) | none yet                                                                                                                                                                                                                                | `https-wildcard-staging-forkline-dev`, `tls-remote-staging-forkline-io`, namespace `calypso-remote`                                                                                                                                                                                                                                        | per #199: zone `forkline.io` has no NS yet, no ZeroSSL issuer                                                                                | after P5 |
| k    | isidoro `isidoro`                                                                                    | `isidoro.grigri.cloud` -> `isidoro:8080` (LLM gateway, SSE)                                            | read/send 1800s, connect 10s, buffering off, body 50m, `least_conn`, OTel snippet; cert `isidoro-tls`; DNS CNAME to `grigri.cloud`; on the override list; clients incl. hermes and the voice assistant (`/v1`)                          | external `https-grigri-cloud`; raw HTTPRoute (chart has no route support) + `BackendTrafficPolicy` (idle >= 1800s, `LeastRequest`) + tracing tag `peer.service`                                                                                                                                                                            | route in `custom/resources`, `ingress.enabled: false`; replace the nginx alerts (below)                                                      | P3       |
| k    | isidoro-keycloak `keycloak-ingress` (CR)                                                             | `idm.isidoro.grigri.cloud` -> `keycloak-service:8443` HTTPS                                            | as multivara keycloak, plus OTel annotations; DNS **A `.4`**, alpha: frozen, the only tenant external record that is not a CNAME                                                                                                        | external `https-grigri-cloud` (cert already has SAN `*.isidoro.grigri.cloud`); HTTPRoute + `BackendTLSPolicy`                                                                                                                                                                                                                              | as multivara keycloak; the A record moves with the route annotation at P5                                                                    | P5       |
| k    | isidoro-keycloak `keycloak-admin-ingress` (static, nginx-internal)                                   | `idm-internal.isidoro.grigri.cloud` -> same                                                            | internal; DNS A `.1`, alpha: **frozen**                                                                                                                                                                                                 | internal gateway has **no listener or cert** for this name (gap, see below)                                                                                                                                                                                                                                                                | route; annotation in the Phase 4 window                                                                                                      | P4       |

(m = multivara, k = kiais, By = latest phase that needs it done. P3 means routes verified
against `.13`/`.14` before Gate 3.) Counts match the live inventory: 10 multivara Ingresses (9
static, 1 runner) and 3 kiais, all 13 on nginx.

**Gaps in our side of the contract**

- **`idm-internal.isidoro.grigri.cloud`** is internal but two labels under `grigri.cloud`; no
  internal listener or certificate covers it. **Closed (2026-11-09, Phase 1 step 9):** internal
  listener `https-isidoro-grigri-cloud` (hostname `*.isidoro.grigri.cloud`, its own Certificate
  from `letsencrypt-prod-dns`, one Certificate per listener) is deployed.
- A new Certificate `*.app.staging.forkline.dev` in `envoy-gateway` is needed for
  `https-runners-staging-forkline-dev` (the `*.staging.forkline.dev` wildcard does not cover two
  labels). Not needed while the bridge passes TLS through.
- The bridge `TLSRoute` (`*.app.staging.forkline.dev` -> `ingress-nginx-external:443`) is ours,
  not multivara's: its backend is our nginx and it is deleted with it. **Deployed (2026-11-09,
  Phase 1 step 8):** `TLSRoute runners-bridge` in `envoy-gateway`, its `ReferenceGrant` is a
  chart template of `system/ingress-nginx-external`.

**Non-Ingress coupling found**

| What                                                                                                                                                                                     | Where                                                                                                                                                                                                                                                                                                                                                                                          | Effect when Envoy replaces nginx                                                                           | Fix                                                                                                                                                                                                                    | By                    |
| ---------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------- | ---------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------- | ---------------------------------------------------------------------------------------------------------- | ---------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------- | --------------------- |
| NetworkPolicy `keycloak/keycloak-network-policy` and `isidoro-keycloak/keycloak-network-policy`, generated by the operator from Keycloak CR `spec.networkPolicy.https`                   | port 8443 only from namespaces `ingress-nginx`, `ingress-nginx-external` and the import job (8080, 9000 open)                                                                                                                                                                                                                                                                                  | Envoy proxies are dropped: 503 `UF` on every idm host (OIDC for calypso and isidoro)                       | add `envoy-gateway` to the list in the grigri `custom/patches` (a list patch replaces the whole list: write all entries); drop the nginx namespaces in Phase 6. Alternative: backend the plain `http` port 8080 (open) | P3 (needed to verify) |
| CiliumNetworkPolicy `calypso-runners/calypso-control-plane-runner-policy` (chart `networkPolicy.ingressNamespaceSelector: ingress-nginx-external`, `allowIngressControllerEgress: true`) | ingress to runner :3000 from that namespace (also open to `cluster`/`host`/`world` entities, so unaffected); **egress** from runners to that namespace on 80/443 (hairpin: names that resolve to the LB VIP)                                                                                                                                                                                   | after P5.2 `idm`, `git`, `app`/`api` names resolve to `.14` (Envoy pods): runner egress to them is dropped | add an egress rule for the Envoy proxies (chart `additionalEgress` or selector); keep the nginx-external rule while runners are bridged                                                                                | before P5.2           |
| WIP: `custom/patches/values.yaml` of calypso                                                                                                                                             | `mcpNetworkPolicy.gateway/broker.additionalEgress` to `192.168.193.4/32:443` and the nginx-external pod labels, `fixture.additionalIngress` from nginx-external pods on 8080, `mcp.egress.allowlist` hairpin                                                                                                                                                                                   | same failure; literal `.4`                                                                                 | author the Envoy equivalents (`.14`, namespace `envoy-gateway`, labels `app.kubernetes.io/name=envoy`, `app.kubernetes.io/component=proxy`, `gateway.envoyproxy.io/owning-gateway-name=external`) in the same change   | P3 / P5               |
| chart values `gitProxyNetworkPolicy.ingressNamespaceSelector` (calypso), `networkPolicy.ingressNamespaceSelector` (isidoro), default `ingress-nginx`                                     | both policies `enabled: false` on grigri today                                                                                                                                                                                                                                                                                                                                                 | none now; a trap if enabled later                                                                          | note for the owners                                                                                                                                                                                                    | n/a                   |
| In-cluster callers of tenant names, through the pfSense override (move at P5.2, no repo change)                                                                                          | calypso control plane and its oauth2-proxy -> `idm.staging.forkline.dev`; gitlab-runner -> `https://gitlab.grigri.cloud`; hermes and the voice assistant -> `isidoro.grigri.cloud/v1`; Argo and chart pulls from `git.grigri.cloud/multivara`; isidoro -> its Keycloak (issuer is in a Secret, not read: assumed `idm.isidoro.grigri.cloud`, an A `.4` record that never goes through pfSense) | none if routes are live                                                                                    | verify at P5 (Phase 5 step 6); blackbox probes for `app.staging.forkline.dev` and `api.staging.forkline.dev` already exist in `system/monitoring`                                                                      | P5                    |
| nginx metrics in tenant monitoring                                                                                                                                                       | kiais `isidoro/resources/prometheusrule-ingress-edge.yaml` (`nginx_ingress_controller_requests{exported_service="isidoro"}`: the 5xx and 413 alerts go silent), multivara `calypso/dashboards/landing-analytics.json` (8 nginx queries, also promoted to prod: query both)                                                                                                                     | silent alerts, empty panels                                                                                | rewrite on Envoy metrics, keep nginx series via `or` where the file is promoted                                                                                                                                        | P6 gate               |
| `hostAliases`, `ingress-nginx` Service references, `192.168.193.1`                                                                                                                       | none in either repo (only the WIP `.4` above)                                                                                                                                                                                                                                                                                                                                                  |                                                                                                            |                                                                                                                                                                                                                        |                       |
| cert-manager ownership                                                                                                                                                                   | no pod mounts an ingress TLS secret in any tenant namespace (checked live, with a positive control `vault-tls`)                                                                                                                                                                                                                                                                                | deleting the Ingresses deletes their Certificates and Secrets safely                                       | none                                                                                                                                                                                                                   | n/a                   |

**Certificates and DNS per zone**

| Zone / names                                        | DNS today                                                                                                                                                                                                                                                                                         | Certificate today                                                                                        | On our gateway                                                    | Result                                                            |
| --------------------------------------------------- | ------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------- | -------------------------------------------------------------------------------------------------------- | ----------------------------------------------------------------- | ----------------------------------------------------------------- |
| `staging.forkline.dev` apex, `app.`, `api.`, `idm.` | CNAME chain to `ddns.staging.forkline.dev` -> WAN. Written earlier by our external-dns (TXT owner `default`) under the alpha key, now **frozen** (the filter only matches `external-dns.kubernetes.io/enabled`); `upsert-only` keeps them. In the LAN: override `.4` for `app`/`auth`/`idm`/`api` | LE `letsencrypt-prod-dns` (our ClusterIssuer, Cloudflare DNS-01) by ingress-shim in `calypso`/`keycloak` | `staging-forkline-dev-tls`, `wildcard-staging-forkline-dev-tls`   | terminated by us; tenant TLS secrets become unused                |
| `*.app.staging.forkline.dev`                        | wildcard CNAME to `app.staging...`, from the nginx-external **Service** annotations (ours). Survives their removal (`upsert-only`)                                                                                                                                                                | `runners-subdomain` in `calypso-runners` (LE)                                                            | bridge: passthrough, nginx keeps terminating with that secret     | keep Certificate and nginx-external until runners emit HTTPRoutes |
| `*.staging.internal.forkline.dev`                   | A `.1` from Cloudflare, alpha key: **frozen**                                                                                                                                                                                                                                                     | LE                                                                                                       | `staging-internal-forkline-dev-tls`                               | records flip only when a route carries the non-alpha key          |
| `gitlab.`, `isidoro.grigri.cloud`                   | CNAME to `grigri.cloud`, frozen                                                                                                                                                                                                                                                                   | LE                                                                                                       | `grigri-cloud-tls`                                                | covered                                                           |
| `idm.isidoro.grigri.cloud`                          | **A `.4`**, frozen                                                                                                                                                                                                                                                                                | LE                                                                                                       | `grigri-cloud-tls` (SAN `*.isidoro.grigri.cloud`)                 | must move to `.14`                                                |
| `idm-internal.isidoro.grigri.cloud`                 | A `.1`, frozen                                                                                                                                                                                                                                                                                    | LE                                                                                                       | none on the internal gateway                                      | gap above                                                         |
| `forkline.io` (Remote, #199)                        | zone has no NS yet                                                                                                                                                                                                                                                                                | none; ZeroSSL issuer pending                                                                             | passthrough: the relay terminates TLS with per-user ZeroSSL certs | maintainer items in #199                                          |
| cluster-internal CAs                                | n/a                                                                                                                                                                                                                                                                                               | `keycloak-ca` (both Keycloaks), `vault-tls` (bank-vaults CA), `calypso-root-ca` (git-proxy)              | never exposed                                                     | only used as `BackendTLSPolicy` CA                                |

DNS rules for tenant routes:

1. **External tenant routes carry no external-dns annotation.** The CNAMEs are already right; a
   route-sourced record would be an A to `.14` (a private address), replacing the public CNAME
   path and breaking internet access.
2. **Frozen internal records and `idm.isidoro` (A)** flip when the route carries
   `external-dns.kubernetes.io/enabled: "true"` (target defaults to the Gateway address,
   `.13` / `.14`). Add it in the same window as the switch (Phase 4 for the three internal
   names, Phase 5 for `idm.isidoro`) and keep the alpha key on the Ingress, so the two sources
   never overlap. Rollback: `upsert-only` never reverts a record, so set it back by hand (the
   owner) and keep serving from nginx.
3. **New names** (`mcp-fixture`, `remote-relay`, per-user `*.remote.staging.forkline.io`) use
   `DNSEndpoint` (the `crd` source, Phase 1 step 5).

**Decisions for the multivara owner (spike in Phase 2, before Phase 3 routes are written)**

- **A. Web auth.** Today nginx `auth_request` to the in-cluster oauth2-proxy, which answers 401
  and nginx redirects to `/oauth2/start`. Envoy `extAuth` returns the auth service's 401 to the
  browser: there is no `auth-signin` equivalent. Options: (1) `SecurityPolicy.extAuth.http` to
  `oauth2-proxy.calypso:80` with oauth2-proxy in auth-only mode so its own sign-in redirect is
  what the browser sees (unproven: spike), `headersToBackend` for the four `X-Auth-Request-*`
  headers and the access token header; (2) the native `SecurityPolicy` OIDC against the
  Keycloak realm (same pattern as recipe 1; array claims need a CSV mapper; one re-login;
  oauth2-proxy and its Redis go away). Recommended: spike (1), fall back to (2). The auth must
  also be skipped on the `/api` skip paths and `/public`.
- **B. Runner auth and routes.** Per runner: HTTPRoute (host -> runner Service `opencode` port) and
  ext auth to `calypso-auth-proxy`. One `SecurityPolicy` can cover all runner routes with
  `targetSelectors` on a route label if the auth proxy derives `runner_id` from the `Host`
  header (today it comes from the Ingress annotation query string). Needs the control plane to
  create and delete HTTPRoutes (RBAC), websockets/SSE verified, timeout `0s`.
- **C. Keycloak backend.** BackendTLS (parity, needs the NetworkPolicy fix and
  `BackendTLSPolicy`) or plain `http` 8080 (already open in the NetworkPolicy, no TLS policy,
  but an unencrypted in-cluster hop and `proxy.headers: xforwarded` trusts what reaches 8080).
  Envoy does not add `X-Forwarded-Host`; the CRs pin `hostname` with `strict: true`, so
  verify login and redirects on a test host first.
- **D. If the kiais owner cannot act in time**, fallback (needs the homelab owner's decision):
  homelab-owned HTTPRoutes plus a `ReferenceGrant` in the tenant namespace, removed when the
  tenant takes over. Not planned.

**What each tenant must land, in order**

| Before                | multivara                                                                                                                                                                                                                                        | kiais                                                                                            |
| --------------------- | ------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------ | ------------------------------------------------------------------------------------------------ |
| Gate 3 (Phase 3)      | routes + policies for every row marked P3 in `custom/resources` (with matching placeholders in `nbg1-c01`); Keycloak NetworkPolicy fix; `BackendTLSPolicy`s; decision A and C taken; WIP netpols and fixture route authored for both controllers | route for `isidoro` + policies; Keycloak NetworkPolicy fix; `BackendTLSPolicy` for `idm.isidoro` |
| Phase 4 (internal)    | internal routes for `idm.staging.internal`, `vault.staging.internal` verified on `.13`; non-alpha annotation on them in the window; vault orphan Ingress deleted (owner yes)                                                                     | route for `idm-internal.isidoro` (after our listener exists) + annotation in the window          |
| Phase 5 (external)    | runner egress rule for Envoy; calypso `mcp` hairpin rules (WIP); landing/CSP/auth verified from the LAN on `.14`                                                                                                                                 | `idm.isidoro` route + annotation (A record to `.14`) in the window                               |
| Phase 6 (remove both) | `ingress.enabled: false` everywhere, Keycloak `spec.ingress` and Vault `spec.ingress` off, runners on HTTPRoute (calypso), nginx namespaces out of every netpol, nginx metrics replaced; **runners are the only thing still bridged**            | `ingress.enabled: false`, nginx namespaces out of the Keycloak NetworkPolicy, alerts rewritten   |

What stays bridged: only `*.app.staging.forkline.dev` (TLS passthrough to nginx-external, which
then sees Envoy pod IPs as clients; no PROXY protocol). It keeps `ingress-nginx-external`, the
Certificate `calypso-runners/runners-subdomain`, the runner CiliumNetworkPolicy selector and the
control plane's `ingressClass: nginx-external` until the control plane emits HTTPRoutes. Nothing
else may stay on nginx past P5: unbridged hosts without a route would 404 on `.14`.

**Owner checklist (asks, in this order; none is done by the agent)**

multivara owner:

- [ ] Take decisions A and C; agree the runner design B (calypso work, not covered by #199).
- [ ] Routes and policies in `custom/` for every P3 row; Keycloak NetworkPolicy and WIP netpol
      fixes; placeholders in `nbg1-c01`.
- [ ] Non-alpha annotation on the internal routes in the Phase 4 window; approve the orphan
      vault Ingress delete.
- [ ] Runner egress to the Envoy proxies before P5.2; #199 items (zone, ZeroSSL, relay).
- [ ] Phase 6 cleanup: `ingress.enabled: false`, nginx references, landing analytics queries.

kiais owner (third party; ask through the homelab owner, lead time unknown, so ask before Phase 3):

- [ ] Route (and `idm.isidoro`, `idm-internal.isidoro` routes) in `custom/`, Keycloak
      NetworkPolicy fix, replace the nginx alert rule.
- [ ] Non-alpha annotation on the two Keycloak routes in the Phase 4 and Phase 5 windows.
- [ ] Phase 6: `ingress.enabled: false`, nginx namespace out of the NetworkPolicy.

Homelab (us, autonomous): internal listener for `*.isidoro.grigri.cloud` and the bridge
`TLSRoute` (both deployed, Phase 1),
`*.app.staging.forkline.dev` Certificate when the runner listener is added, the Gate 3 diff
script over tenant hosts, the blackbox probes.

## Target Design

```
                 pfSense (BGP peer, NAT, unbound)
       LAN clients |                      | public 80/443/29518 -> NAT alias k8s_external_ingress
                   v                      v
     VIP .13 Gateway "internal"     VIP .14 Gateway "external"
                   |   EnvoyProxy "internal"   |   EnvoyProxy "external"
                   +----------+   +------------+
                              v   v
       HTTPRoutes / TLSRoutes / TCPRoute in app namespaces (parentRef picks gateway + listener)
```

- **One app** `system/envoy-gateway` (namespace `envoy-gateway`), two OCI charts at **v1.9.3**
  via kustomize `helmCharts`, each with a Renovate hint: `gateway-crds-helm` (experimental channel,
  Gateway API v1.6.1 plus the Envoy Gateway CRDs) and `gateway-helm` with `crds.enabled: false`
  (this also drops the safe-upgrades `ValidatingAdmissionPolicy`). CRDs sync with
  `ServerSideApply=true` (several exceed 262144 bytes). **Gateway API experimental channel**: it
  keeps serving `v1alpha2` for calypso compatibility (the standard channel would be enough for
  `v1` `TCPRoute`/`TLSRoute`). Envoy image pinned to `docker.io/envoyproxy/envoy:distroless-v1.39.6`
  (eMudhra CA fix). Controller config: `extensionApis.enableBackend: true` (unifi), `enableLua: true`
  for `EnvoyExtensionPolicy` (readest, radarr, navidrome), `topologyInjector.enabled: false`. Sync
  waves: CRDs/controller, then GatewayClass, EnvoyProxy, Certificates, Gateways, global policies.
  Argo CD 3.6.1 has no health check for `TCPRoute`, `UDPRoute`, `ReferenceGrant`, `EnvoyProxy`,
  `HTTPRouteFilter` (they show Healthy unchecked; read their status by hand).
- **One `GatewayClass` (`envoy`), two `Gateway`s** (`internal`, `external`) in `envoy-gateway`,
  each with its own `EnvoyProxy` (via `spec.infrastructure.parametersRef`), so each gets its own
  Deployment and LB Service:
  - Service: `loadBalancerClass: io.cilium/bgp-control-plane`, `lbipam.cilium.io/ips: <VIP>`,
    `externalTrafficPolicy: Local` (real client IP through pfSense DNAT; staging matches the
    #199 requirement).
  - Deployment, PDB `minAvailable: 1`, `rollingUpdate: {maxSurge: 1, maxUnavailable: 0}` (avoid
    a roll gap with `Local`).
  - **Placement copied from ingress-nginx** (`system/ingress-nginx/values.yaml:56-90`), set in
    `EnvoyProxy.spec.provider.kubernetes.envoyDeployment.pod` / `.envoyHpa`: preferred node
    affinity `kubernetes.io/hostname In [prusik]` (weight 100) and `kubernetes.io/arch In
[amd64]` (weight 50); topology spread `maxSkew: 4` on `kubernetes.io/hostname`,
    `DoNotSchedule`, `matchLabelKeys: [pod-template-hash]`, selector on the gateway's own proxy
    labels; no tolerations (never on the CI node); internal HPA 2-11, external 2 replicas.
- **Listeners (this is the contract other repos reference with `parentRefs.sectionName`):**

| Gateway  | Listener                              | Port / protocol     | Hostname                                     | Cert / backend                                                                                                              |
| -------- | ------------------------------------- | ------------------- | -------------------------------------------- | --------------------------------------------------------------------------------------------------------------------------- |
| internal | `http`                                | 80 HTTP             |                                              | redirect route                                                                                                              |
| internal | `https-internal-grigri-cloud`         | 443 HTTPS           | `*.internal.grigri.cloud`                    | wildcard                                                                                                                    |
| internal | `https-staging-internal-forkline-dev` | 443 HTTPS           | `*.staging.internal.forkline.dev`            | wildcard (multivara vault, keycloak admin)                                                                                  |
| internal | `https-isidoro-grigri-cloud`          | 443 HTTPS           | `*.isidoro.grigri.cloud`                     | own wildcard Certificate (kiais `idm-internal.isidoro`; deployed 2026-11-09)                                                |
| external | `http`                                | 80 HTTP             |                                              | redirect route                                                                                                              |
| external | `https-grigri-cloud`                  | 443 HTTPS           | `*.grigri.cloud`                             | wildcards `*.grigri.cloud`, `*.rajoy.grigri.cloud`, `*.m.rajoy.grigri.cloud`, `*.isidoro.grigri.cloud` (Envoy picks by SNI) |
| external | `https-staging-forkline-dev`          | 443 HTTPS           | `staging.forkline.dev`                       | apex cert                                                                                                                   |
| external | `https-wildcard-staging-forkline-dev` | 443 HTTPS           | `*.staging.forkline.dev`                     | wildcard (app, api, idm)                                                                                                    |
| external | `tls-bridge-runners`                  | 443 TLS Passthrough | `*.app.staging.forkline.dev`                 | `TLSRoute` to `ingress-nginx-external:443` (D4, temporary)                                                                  |
| external | `https-runners-staging-forkline-dev`  | 443 HTTPS           | `*.app.staging.forkline.dev`                 | replaces the bridge once calypso emits HTTPRoutes                                                                           |
| external | `tls-remote-staging-forkline-io`      | 443 TLS Passthrough | `*.remote.staging.forkline.io`               | `TLSRoute` from the `calypso-remote` relay (#199)                                                                           |
| external | `https-mintpsicologia-es` / `-www`    | 443 HTTPS           | `mintpsicologia.es`, `www.mintpsicologia.es` | per-host certs                                                                                                              |
| external | `tcp-forgejo-ssh`                     | 29518 TCP           |                                              | `TCPRoute` to `git/git-forgejo-ssh:22`                                                                                      |

`allowedRoutes.namespaces.from: All` (single-owner cluster). All certs live in
`envoy-gateway`, issued by `letsencrypt-prod-dns` (DNS-01, Cloudflare), so no `ReferenceGrant`
and no cert-manager Gateway API support are needed; ~100 per-host certs go away. A Gateway API
wildcard matches several labels, so `*.grigri.cloud` would also accept an
`x.internal.grigri.cloud` route, but the external gateway holds no cert for it and the TLS
handshake fails: keep it that way and add a lint (route with `.internal.` host must not
reference `external`).

- **Gateway-wide defaults** (one `ClientTrafficPolicy` + one `BackendTrafficPolicy` per gateway),
  matching nginx so apps don't notice:
  - request timeout 60s (Envoy Gateway default is 15s), route-level overrides for long-running
    apps (immich, forgejo, nextcloud, transcoder, gitlab, minio/s3, runners: 3600s or `0s`);
  - HSTS identical to nginx (`max-age=31536000; includeSubDomains`), remove `Server`, the four
    global security headers;
  - `tls.alpnProtocols: [h2, http/1.1]` pinned (**mandatory here**: hostname-scoped TLS
    listeners silently drop h2 gateway-wide otherwise), `http1.http10` enabled, path
    `escapedSlashesAction: KeepUnchanged`, idle timeouts and TCP keepalive set explicitly;
  - compression (`compressor: [{type: Brotli}, {type: Gzip}]`; the older `compression` field is deprecated) **only per route, never on MinIO/S3 hosts** (compression rewrites
    `Accept-Encoding`, which breaks SigV4-signed requests);
  - every route-level `BackendTrafficPolicy` uses `mergeType: StrategicMerge`, otherwise it
    replaces the gateway one instead of extending it.
- **Auth (D3)**: `SecurityPolicy` OIDC against Kanidm + JWT `authorization` on the `groups`
  claim, one policy per app; recipe 1 below.
- **Telemetry**: Prometheus scraping of controller and proxies (ServiceMonitor/PodMonitor with
  `release: monitoring`), Envoy Gateway's Grafana dashboards, external proxy tracing via
  `EnvoyProxy.telemetry.tracing` to `otel-collector...:4317`, JSON access logs with trace id.
- **DNS**: external-dns gains sources `gateway-httproute` and `crd`, and `forkline.io` in
  `domainFilters` (#199). Not `gateway-tcproute` (v0.23 reads `v1alpha2`; forgejo SSH shares the
  `git.` record). `--annotation-filter` applies to routes, so every route carries
  `external-dns.kubernetes.io/enabled=true`; the `target` annotation is read **only from the
  Gateway** (never routes) and defaults to its `status.addresses` (`.13`/`.14`). The runner wildcard moves from the nginx Service annotations to a
  `DNSEndpoint` owned by multivara.
- **Forgejo SSH (D2)**: `TCPRoute` on the external gateway, sharing `git.grigri.cloud`, the
  pfSense port alias and the unbound override with HTTPS; no pfSense rule of its own.

### Special case recipes (D5: all ported and verified)

Recipes below are the ones proven in a previous production migration of the same kind, adapted
to this repo. "Unproven" marks a mechanism with no prior use: spike it in Phase 2. Lua runs in an
`EnvoyExtensionPolicy` (EEP) targeting the route; attach it only to the narrowest route that
needs it.

**Rules that apply to every case**

- nginx snippets are never converted by tools (`ingress2gateway` and friends silently drop auth,
  backend protocol and snippets, and mistranslate sizes): translate each one by what it does,
  after capturing its live behaviour with `curl`.
- Gateway API path matching is case-sensitive and segment-based (`PathPrefix /api` matches
  `/api` and `/api/x`, not `/apix`); `Exact` beats the longest prefix; a regex path match is a
  full match (append `.*`).
- A route-level `BackendTrafficPolicy` replaces the gateway one unless it sets
  `mergeType: StrategicMerge`.
- A `requestBuffer` anywhere in scope breaks websockets, WebDAV and large uploads: never set one.
- A route that bypasses auth must not carry `claimToHeaders`, and must strip any incoming
  `X-Auth-Request-*` headers (otherwise a client can spoof identity).

**1. Auth (24 Ingresses, D3)**: proven on the Phase 2 pilots (http-echo, esphome, navidrome,
radarr). Full procedure, nginx mapping and checks: `docs/conventions/gateway-api-routes.md`
("Authentication").

```yaml
apiVersion: kaniop.rs/v1beta1
kind: KanidmOAuth2Client # one per app, in the app namespace
metadata: { name: <app> }
spec:
  kanidmRef: { name: kanidm, namespace: kanidm }
  displayname: <app>
  origin: https://<host>/
  redirectUrl: [https://<host>/oauth2/callback] # every host of the app
  scopeMap: [{ group: <app>-users, scopes: [openid, profile, email, groups] }]
  preferShortUsername: true
  strictRedirectUrl: true
  secretKeyAliases: { clientSecret: [client-secret] }
---
apiVersion: gateway.envoyproxy.io/v1alpha1
kind: SecurityPolicy # one per protected route (may target several routes of one host)
metadata: { name: <app>-<gateway> }
spec:
  targetRefs: [{ group: gateway.networking.k8s.io, kind: HTTPRoute, name: <route> }]
  oidc:
    provider:
      issuer: https://idm.grigri.cloud/oauth2/openid/<app>
      authorizationEndpoint: https://idm.grigri.cloud/ui/oauth2 # explicit: no discovery
      tokenEndpoint: https://idm.grigri.cloud/oauth2/token
    clientID: <app>
    clientSecret: { name: <app>-kanidm-oauth2-credentials } # same namespace, key client-secret
    redirectURL: https://<host>/oauth2/callback
    logoutPath: /oauth2/logout
    scopes: [openid, profile, email, groups]
    cookieNames: { accessToken: oidc-access-token, idToken: oidc-id-token }
  jwt:
    providers:
      - name: kanidm
        issuer: https://idm.grigri.cloud/oauth2/openid/<app>
        audiences: [<app>]
        extractFrom: { cookies: [oidc-id-token] }
        remoteJWKS: { uri: https://idm.grigri.cloud/oauth2/openid/<app>/public_key.jwk }
        claimToHeaders: [{ header: X-Auth-Request-Preferred-Username, claim: preferred_username }]
  authorization:
    defaultAction: Deny
    rules:
      - name: allowed-groups
        action: Allow
        principal:
          jwt:
            provider: kanidm
            claims: [{ name: groups, valueType: StringArray, values: [<app>-users@idm.grigri.cloud] }]
```

- **One Kanidm client per app** (changed from "one shared client in `envoy-gateway`"): the secret
  lands next to the policy (no `ReferenceGrant`), Kanidm refuses users outside the scope map group
  at `/ui/oauth2` already (the JWT rule is the second gate), a token of one app is not valid for
  another (per-client issuer and audience), and it is the repo's existing per-app Kaniop pattern.
- The OIDC cookie is **per host**: there is no cross-app cookie. Single sign-on still works
  because Kanidm keeps its own session, so each new app is a silent redirect (after a one-time
  consent per client). Don't rely on `cookieDomain`.
- Identity headers: replicate what nginx `auth-response-headers` passed (navidrome:
  `X-Auth-Request-Preferred-Username`; esphome and radarr: none; http-echo: user, email, preferred
  username). `claimToHeaders` cannot copy arrays: for a groups header use `claimMap[].joinStrategy:
  csv` on the client. searxng is still open.
- **Kanidm facts (verified 2026-11-09):** Envoy always sends PKCE S256 and Kanidm accepts it (no
  opt-out anywhere); per-client issuer and JWKS; ID tokens ES256; no `end_session_endpoint`, so
  `logoutPath` only clears the Envoy cookies; the `groups` scope emits UUIDs and SPNs in one flat
  array and the rules match the SPN; `secretKeyAliases.clientSecret: [client-secret]`.
- Skip paths: a separate same-host route without the policy that removes `X-Auth-Request-*`;
  query skips with `queryParams` regex (works); mind EG's regex order (length, then query count).
- 401 instead of a redirect (nginx `auth-url` without `auth-signin`): `oidc.denyRedirect` on the
  `:path` pseudo-header.
- Verify: `docs/conventions/gateway-api-routes.md` "Verification (no browser needed)", then Gate 2.

**2. Global controller behaviour** (per gateway)

| nginx                                                                                              | Envoy Gateway                                                                                                                                                                                                                                                               |
| -------------------------------------------------------------------------------------------------- | --------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------- |
| `more_set_headers` security headers, HSTS (`max-age=31536000; includeSubDomains`), `server_tokens` | `ClientTrafficPolicy.headers.lateResponseHeaders.set` (Referrer-Policy, X-Frame-Options, X-Content-Type-Options, Permissions-Policy, Strict-Transport-Security) and `remove: [Server]`                                                                                      |
| brotli                                                                                             | `BackendTrafficPolicy.compressor: [{type: Brotli}, {type: Gzip}]` on routes only, never on S3 or navidrome routes                                                                                                                                                           |
| `use-forwarded-headers: true` (internal)                                                           | internal `ClientTrafficPolicy.clientIPDetection.xForwardedFor.numTrustedHops: 1` (or `trustedCIDRs`); external: none                                                                                                                                                        |
| OTel tracing, trace_id in access log                                                               | `EnvoyProxy.telemetry.tracing` (`provider.type: OpenTelemetry`, backendRef to the otel-collector Service :4317, `samplingRate: 100`) + `ReferenceGrant`; JSON access log with `"traceID": "%TRACE_ID%"`; service name via `customTags` or check what the collector receives |
| `limit-req-status-code: 429`                                                                       | default for Envoy local rate limits                                                                                                                                                                                                                                         |
| implicit nginx defaults                                                                            | `tls.alpnProtocols: [h2, http/1.1]`, `path.escapedSlashesAction: KeepUnchanged`, `http1.http10: {}`, `timeout.http.idleTimeout: 600s`, `tcpKeepalive`, request timeout 60s                                                                                                  |

Verify: `curl -skI https://<host>/ | grep -i 'strict-transport\|x-frame\|referrer\|permissions\|server'`,
`curl -sk --http2 -o /dev/null -w '%{http_version}'` = 2.

**3. readest** (`apps/readest/values.yaml` ~L340-385, ~L411-460)

- nginx: on `/auth/v1/authorize`, when the query is empty or names `provider=google|apple|github|discord`,
  302 to the same path with `?provider=custom:kanidm&redirect_to=<redirect_to arg, else
https://readest.grigri.cloud/auth/callback>`; `/callback` 301 to `/auth/v1/callback` keeping
  the query; `/auth/v1(/|$)(.*)` rewritten to `/$2`.
- Envoy: Lua on the authorize rule (no Gateway API filter can compute a query):

```lua
function envoy_on_request(h)
  local path, q = h:headers():get(":path"):match("^([^?]*)%??(.*)$")
  if path ~= "/auth/v1/authorize" then return end
  local ql, hit = q:lower(), (q == "")
  for _, v in ipairs({"google", "apple", "github", "discord"}) do
    if ql:find("provider=" .. v, 1, true) then hit = true end
  end
  if not hit then return end
  local rt = q:match("redirect_to=([^&]*)") or "https://readest.grigri.cloud/auth/callback"
  h:respond({[":status"] = "302", ["location"] = path .. "?provider=custom:kanidm&redirect_to=" .. rt}, "")
end
```

`/callback`: rule `Exact /callback` + `RequestRedirect {path: {type: ReplaceFullPath,
  replaceFullPath: /auth/v1/callback}, statusCode: 301}` (query preserved). Rewrite: rule
`PathPrefix /auth/v1` + `URLRewrite ReplacePrefixMatch: /`.

- Gotcha: check whether nginx evaluated `$uri` before or after the rewrite (curl the live host
  first); the internal host's default `redirect_to` may differ, read both snippets.
- Verify: `curl -sk -o /dev/null -w '%{http_code} %{redirect_url}\n' 'https://<host>/auth/v1/authorize?provider=google&redirect_to=X'`, then a full login on web and app.

**4. radarr** (`apps/radarr/values.yaml` ~L62-69, ~L94-106)

- nginx: append `excludeLocalCovers=true` to `/api/v3/movie` when absent; external skips auth when
  `^/api/v3/...[?&]apiKey=` is present.
- Envoy: a dedicated `Exact /api/v3/movie` rule with Lua
  (`if not q:find("excludeLocalCovers=", 1, true) then h:headers():replace(":path", <path with
the param appended, "?" or "&">) end`), attached to both the protected and the bypass copy.
  Bypass: a second HTTPRoute, `PathPrefix /api/v3` + `queryParams: [{type: RegularExpression,
name: apiKey, value: ".+"}]`, no SecurityPolicy (radarr validates the key, as today).
  `queryParams` regex match is unproven: spike.
- Verify: with `apiKey` 200 without login, without it 302 to Kanidm; radarr log shows
  `excludeLocalCovers=true` on a request that did not send it.

**5. navidrome** (`apps/navidrome/values.yaml` ~L65-106)

- nginx: auth skip `^/(rest|share)`; external also disables compression, clears
  `Accept-Encoding` and injects a `#/login -> /` script before `</html>`.
- Envoy: bypass = second HTTPRoute with `PathPrefix /rest` and `PathPrefix /share`, no policy,
  `RequestHeaderModifier remove: [X-Auth-Request-User, X-Auth-Request-Email, ...]`. UI route:
  `RequestHeaderModifier remove: [Accept-Encoding]`, no compression, Lua response body rewrite
  (unproven, no prior use):

```lua
function envoy_on_response(h)
  local ct = h:headers():get("content-type") or ""
  if not ct:find("text/html", 1, true) then return end   -- never buffer audio
  local body = h:body()
  local s = body:getBytes(0, body:length())
  local out = s:gsub("</html>", '<script>if(location.hash==="#/login"){location.replace("/")}</script></html>')
  body:setBytes(out)
end
```

- Gotcha: `body()` buffers the whole response, hence the content-type guard; check
  `content-length` is fixed up. With native OIDC the `#/login` landing may no longer happen:
  verify before and after, keep the Lua only if still needed.
- Verify: `curl -sk https://navidrome.grigri.cloud/app/ | grep -c location.replace` = 1; a
  Subsonic client works without login.

**6. kroki** (`apps/kroki/values.yaml` ~L77-81, cache zone in the nginx `http-snippet`)

- nginx: `proxy_cache` 7d on a 300 MB `/dev/shm` zone, `expires 7d`, `X-Cache-Status`.
- Envoy has no production-grade response cache (the HTTP cache filter is experimental, in-memory
  per pod, and needs `EnvoyPatchPolicy`). Exact parity: a small caching sidecar (nginx or
  Varnish) in the kroki pod with the same zone, 7d validity and `X-Cache-Status`; the route
  points at the sidecar port. Plus `ResponseHeaderModifier` `Cache-Control: public,
max-age=604800` if the sidecar does not set it.
- Verify: two identical GETs, the second shows `X-Cache-Status: HIT` and is faster.

**7. tanque-stream** (`apps/tanque/templates/stream-ingress.yaml`)

- nginx: `/` 302 to `/stream.html?src=tcp_magic`, before auth.
- Envoy: `HTTPRouteFilter.directResponse` with `statusCode: 302` and `header.set: [{name:
Location, value: /stream.html?src=tcp_magic}]` (`set` works, `remove` is ignored; no Lua needed).
  Rule `Exact /` on a route **without** the SecurityPolicy (keeps "redirect before login").
- Verify: `curl -sk -o /dev/null -w '%{http_code} %{redirect_url}\n' https://tanque-stream.grigri.cloud/`.

**8. nextcloud** (`apps/nextcloud/nextcloud.yaml` server-snippet)

| nginx                                                                   | Envoy Gateway                                                                                                                          |
| ----------------------------------------------------------------------- | -------------------------------------------------------------------------------------------------------------------------------------- |
| `server_tokens off`                                                     | gateway-wide `remove: [Server]`                                                                                                        |
| `proxy_hide_header X-Powered-By`                                        | `ResponseHeaderModifier remove: [X-Powered-By]`                                                                                        |
| `= /` with UA `^DavClnt` -> 302 `/remote.php/webdav/`                   | rule `Exact /` + `headers: [{type: RegularExpression, name: User-Agent, value: "^DavClnt.*"}]` + `RequestRedirect ReplaceFullPath` 302 |
| `/.well-known/carddav`, `/.well-known/caldav` -> 301 `/remote.php/dav/` | two `Exact` rules + `RequestRedirect ReplaceFullPath` 301                                                                              |
| `^~ /.well-known` -> 301 `/index.php$uri`                               | `PathPrefix /.well-known` + `RequestRedirect path ReplacePrefixMatch /index.php/.well-known` 301                                       |
| `^/(build\|tests\|config\|lib\|3rdparty\|templates\|data)(/\|$)` -> 404 | one rule with 7 `PathPrefix` matches + `HTTPRouteFilter.directResponse` 404                                                            |
| `^/(\.\|autotest\|occ\|issue\|indie\|db_\|console)` -> 404              | `RegularExpression` `^/(\.\|autotest\|occ\|issue\|indie\|db_\|console).*` + `directResponse` 404                                       |
| `robots.txt` logging off                                                | nothing                                                                                                                                |
| 3600s, buffering off, 35G                                               | `timeouts.request: 0s`; no buffer, no body limit                                                                                       |

Verify: `curl -A DavClnt` on `/`, `/.well-known/caldav`, `/config/x` (404), desktop and mobile
sync, a multi-GB upload over a slow link (stream idle timeout).

**9. wallabag**: rule with `queryParams: [{type: RegularExpression, name:
PHP_SESSION_UPLOAD_PROGRESS, value: ".*"}]` + `directResponse` 403 ahead of the catch-all
(unproven; for nginx's case-insensitive substring parity use Lua on `:path` + `respond` 403).
Verify `?PHP_SESSION_UPLOAD_PROGRESS=x` = 403, normal URL = 200.

**10. home-assistant**: rule `Exact /api/prometheus` + `directResponse` 403 ahead of `/`
(Prometheus scrapes the Service directly). Verify 403 from outside.

**11. MinIO** (`platform/minio`, `apps/s3-public`, `apps/cross-backups`): `timeouts.request: 0s`;
nothing for body, buffering or chunked; `ClientTrafficPolicy.headers.withUnderscoresAction:
Allow` if clients send underscore headers (`ignore_invalid_headers off`); **no compression**
(it strips the signed `Accept-Encoding`, SigV4 fails with 400/403). Verify with a real SDK
(`aws s3 ls --debug`, `mc`) and an upload longer than 60s.

**12. Backend TLS**

- kanidm (**done 2026-11-09**, `e02d10b8d`, see Phase 2 auth progress): the Kaniop CR's native `spec.gateway` (HTTPRoute `kanidm`, optional `backendTlsPolicy`,
  coexists with `spec.ingress`, backend port 8443) + a separate raw
  `BackendTrafficPolicy.loadBalancer: {type: ConsistentHash, consistentHash: {type: SourceIP}}`
  (needs `Local`, kept). Before removing `spec.ingress`: `spec.tlsSecretName:
idm-external-tls-certificate` and an explicit `Certificate` (keeps the LDAPS SAN), otherwise
  the pods fail to start. Check the `BackendTLSPolicy` apiVersion the operator emits.
- vault: operator CR `ingress` off, standalone HTTPRoute, `BackendTLSPolicy` with the
  operator's TLS secret CA and the in-cluster Service FQDN as `hostname`. The operator never
  deletes the Ingress it created: delete it by hand (needs the owner's yes, Autonomy Scope).
- unifi (self-signed): EG `Backend` (`enableBackend: true`) with `tls.insecureSkipVerify: true`,
  same trust as nginx today (don't tighten unasked).
- Verify: policies `Accepted=True`, 200 not 503 with response flag `UF`.

**13. Small ones**

- forgejo SSH: listener `tcp-forgejo-ssh` (TCP 29518) + `TCPRoute` to `git-forgejo-ssh:22`;
  `kubectl get crd tcproutes.gateway.networking.k8s.io`; `ssh -p 29518 git@...`.
- `www.mintpsicologia.es`: HTTPRoute with no backend, `RequestRedirect {scheme: https,
hostname: mintpsicologia.es, statusCode: 301}`.
- calypso-external-api (multivara): `HTTPRouteFilter.urlRewrite.path.replaceRegexMatch` with the
  nginx regex; `BackendTrafficPolicy.rateLimit` `type: Local`, `mergeType: StrategicMerge`. A
  local limit is per route per proxy pod, not per client IP like nginx: size it
  (20 rps x 5 burst x proxy pods) and add a `clientSelectors` source-IP rule if per-IP limiting
  matters.

## Strategy: why side by side

|                                     | Side by side (chosen)                                                                 | Big bang, Envoy takes `.1`/`.4`                                         |
| ----------------------------------- | ------------------------------------------------------------------------------------- | ----------------------------------------------------------------------- |
| Per-app testing before real traffic | full: `curl --resolve`, `ssh -p 29518`, browser with `/etc/hosts` against `.13`/`.14` | none in place                                                           |
| Switch                              | two single moves (internal, external), each with a seconds-long rollback              | everything at once                                                      |
| Rollback                            | revert one change; nginx is still running                                             | revert git and wait for Argo, while ArgoCD/Vault/idm may be unreachable |

### Expected unavailability

| Service                                        | When                                 | How long                                                                                                  | Why                                                   |
| ---------------------------------------------- | ------------------------------------ | --------------------------------------------------------------------------------------------------------- | ----------------------------------------------------- |
| internal names                                 | internal switch (Phase 4)            | none in practice: clients that cached `.1` (Cloudflare TTL, 300s auto) keep reaching nginx, still running |                                                       |
| all external hosts                             | pfSense P5.1 apply                   | seconds (filter reload); open connections keep their pf state to `.4` and nginx until they close          |                                                       |
| LAN clients on overridden names                | pfSense P5.2 (unbound restart)       | ~1-2 s DNS blip; cached answers keep hitting `.4` (still served)                                          |                                                       |
| forgejo SSH (29518)                            | pfSense P5.1                         | same seconds; open SSH sessions continue on `.4`                                                          | moves with 80/443                                     |
| everyone using auth                            | first visit after their app's switch | one re-login                                                                                              | oauth2-proxy cookie replaced by the Envoy OIDC cookie |
| mosquitto (.9/.10), coturn, qbittorrent, unifi | never                                | 0                                                                                                         | own VIPs and NAT rules, not on the ingress path       |

## pfSense change plan

Manual in the pfSense UI by the owner; each change has its verify and rollback. Read-only checks
can be run by an agent (`ssh pfsense.grigri`, show-only commands).

| #        | When                 | Change                                                                                                                                                                                                                         | Verify                                                                                                                         | Rollback              |
| -------- | -------------------- | ------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------ | ------------------------------------------------------------------------------------------------------------------------------ | --------------------- |
| P0.1     | Phase 0              | Export the cluster-relevant config (tables above) into `docs/network/pfsense.md`                                                                                                                                               | committed                                                                                                                      |                       |
| P0.2     | optional, any time   | FRR neighbor `192.168.192.11` (k8s-amd64-1): route-map `allow-all` in/out, `next-hop-self`, `allowas-in` like grigri/prusik; update `docs/deployment/cilium-bgp-control-plane.md`. Hygiene only, not needed for this migration | `vtysh -c 'show ip bgp summary'` shows prefixes, not `(Policy)`                                                                | remove the route-maps |
| P1.1     | Phase 1, optional    | Aliases `k8s_internal_gateway` = .13, `k8s_external_gateway` = .14 (documentation)                                                                                                                                             | `pfctl -t <alias> -T show`                                                                                                     | delete                |
| P1.2     | Phase 1              | nothing else: BGP `allow-all` accepts the new /32s, LAN-side interfaces already pass to the VIP range                                                                                                                          | `vtysh -c 'show ip bgp 192.168.193.0/24 longer-prefixes'` has .13 and .14                                                      |                       |
| P4.1     | **internal switch**  | Unbound custom options: `local-data: "k8s.grigri A 192.168.193.13"` (or delete both lines if nothing uses `k8s.grigri`; only a doc mention found)                                                                              | `drill k8s.grigri @192.168.192.1`                                                                                              | restore `.1`          |
| **P5.1** | **external switch**  | Firewall > Aliases > `k8s_external_ingress`: `192.168.193.4` -> `192.168.193.14`. Retargets the WAN port forward (80, 443, 29518), its WAN pass rule and reflection on every interface                                         | from mobile data: `curl -I https://git.grigri.cloud`, `ssh -p 29518 git@git.grigri.cloud`; `pfctl -sn \| grep 29518` shows .14 | alias back to .4      |
| **P5.2** | **external switch**  | DNS Resolver host overrides `freshrss.grigri.cloud` (28 aliases), `app.staging.forkline.dev` (3), `mintpsicologia.es` (www): `.4` -> `.14`. IP only, no list cleanup in this window                                            | `drill idm.grigri.cloud @192.168.192.1` = .14, also from a pod                                                                 | IPs back to .4        |
| P5.3     | end of external soak | Diagnostics > States: kill remaining states to `192.168.193.4`                                                                                                                                                                 | `pfctl -ss \| grep -c 192.168.193.4` = 0                                                                                       | n/a                   |
| P6.1     | Phase 6              | Rename `k8s_external_ingress` to `k8s_external_gateway` (or point the rule at the P1.1 alias)                                                                                                                                  | rule shows the new alias                                                                                                       |                       |
| P6.2     | Phase 6              | Override list cleanup: drop dead names (gitea, jackett, accounts, auth-kanidm, and gitlab/isidoro if gone), add missing external names (mcp, photos, requests, ...); then remove the hermes hostAliases                        | `drill mcp.grigri.cloud` = .14 from a pod                                                                                      |                       |
| P6.3     | Phase 6              | Update `docs/network/pfsense.md` and `docs/conventions/bandwidth-protection.md`                                                                                                                                                |                                                                                                                                |                       |

## Implementation Plan

Each phase ends at a gate. Do not start the next phase until the gate passes.

### Phase 0 - Pre-flight (no cluster changes)

1. pfSense P0.1 export.
2. Confirm `.13`/`.14` free on the cluster side (`kubectl get svc -A` LB IPs) and record them.
3. DNS map: `dig` every internal ingress host against `1.1.1.1` (expect Cloudflare `.1`), list
   every Cloudflare record under `internal.grigri.cloud` and `staging.internal.forkline.dev`
   that does **not** point at `.1` (LB services such as `ldaps.idm.internal` = .8; they must not
   move), and resolve from a pod `idm`, `git`, `vault.internal`, `s3.internal`, `auth`, `mcp`.
4. Confirm the internal switch mechanism works on the forkline ingress-nginx chart: the
   controller flag `--publish-status-address` (chart `controller.publishService.enabled: false`
   - `extraArgs`) makes every `nginx-internal` Ingress report that address in its status, which
     is what external-dns publishes. Check with `helm template` and the chart source.
5. Inventory ingress TLS secret consumers: for every ingress `tls.secretName`, check nothing
   else mounts or references it (kanidm LDAPS first). Any hit gets an explicit `Certificate`
   before Phase 6 removes its Ingress.
6. Find the owner repo of the `isidoro`, `isidoro-keycloak`, `mcp-gateway` Ingresses.
7. Post the listener contract on #199 (owner action) so multivara writes routes against it.
8. Verify the remaining assumptions (done 2026-11-09, see Phase 0 findings; the Cilium BGP
   `Local` advertisement is verified by the Gate 1 roll test):
   - Cilium BGP advertises a `Local` VIP only from nodes with a ready local endpoint: Gate 1;
   - external-dns `gateway-httproute`: the annotation filter applies to routes, `target` is read
     only from the Gateway;
   - Gateway API channel bundled with the pinned `gateway-helm`: v1.6.1 experimental, but the
     CRDs come from `gateway-crds-helm`;
   - Argo CD 3.6 health checks for Gateway API kinds: none for TCPRoute, UDPRoute,
     ReferenceGrant, EnvoyProxy, HTTPRouteFilter;
   - which upstream charts in group B support `HTTPRoute` natively: table in the findings;
   - Kanidm with Envoy Gateway OIDC: per-client issuer/JWKS, PKCE S256 accepted (recipe 1);
   - `queryParams` regex matching: supported (full match), confirm in the radarr spike.

**Gate 0:** met except the #199 contract post (owner action, does not block Phase 1): pfSense
export committed, DNS map recorded here, publish-status-address confirmed.

### Phase 1 - Platform (both gateways live, no app traffic)

Commits, in order, each synced and checked before the next:

1. `envoy-gateway: Add Envoy Gateway controller and Gateway API CRDs` (`gateway-crds-helm` and
   `gateway-helm` v1.9.3, experimental channel, ServerSideApply, Renovate hints, Backend and Lua
   enabled, Envoy image pinned). Restart the Argo CD application controller if it keeps reporting
   unknown kinds after the CRDs land.
2. `envoy-gateway: Add internal and external gateways` (GatewayClass, 2 EnvoyProxy with the
   nginx placement, 2 Gateways on `.13`/`.14` with the listener table, Certificates, HTTP->HTTPS
   redirect route, gateway-wide ClientTrafficPolicy and BackendTrafficPolicy).
3. `git: Add forgejo SSH TCPRoute on the external gateway`.
4. `envoy-gateway: Add monitoring` (ServiceMonitor/PodMonitor, dashboards, alerts mirroring the
   three nginx-external ones on Envoy metrics for both gateways; external tracing to the
   otel-collector, new service name added next to `nginx-external` in otel-collector, Loki and
   Tempo configs).
5. `external-dns: Add gateway-httproute and crd sources and forkline.io`.
6. `cert-manager: Add ZeroSSL ClusterIssuer for forkline.io` (#199; needs the owner's EAB
   credentials in Vault, can trail).
7. `http-echo: Add HTTPRoutes on both gateways` (smoke test target).

pfSense: P1.1 (optional).

**Gate 1:**

- both Gateways `Programmed=True` with the right address; pfSense learns .13 and .14 via BGP (done, step 2);
- `curl --resolve http-echo.internal.grigri.cloud:443:192.168.193.13 ...` and the external
  equivalent: 200, valid wildcard cert, h2, HSTS and security headers, no `Server`; `http://`
  redirects;
  **done 2026-11-09 (step 7):** 200, valid Let's Encrypt wildcard (`*.internal.grigri.cloud` /
  `*.grigri.cloud`), ALPN h2, HSTS plus the security headers, no `Server`, `http://` 301 to the
  https URL, on both VIPs;
- `ssh -p 29518 -o HostKeyAlias=git.grigri.cloud git@192.168.193.14` authenticates with the same
  host key as through nginx; **done 2026-11-09 (step 3):** identical host keys and authentication
  on `.14:29518`;
- http-echo sees the real LAN client IP: **done 2026-11-09:** `ip` and `x-forwarded-for` are the
  client's own address (routed LAN, no SNAT) on both VIPs;
- no-SNI behaviour (recorded, not a gate): Envoy `.13` and `.14` reset the TCP connection on a
  ClientHello without SNI (`errno=104`, no certificate); nginx `.4` and `.1` answer with the
  self-signed "Kubernetes Ingress Controller Fake Certificate" and a 404. Nothing in the
  migration depends on the nginx behaviour; revisit only if a health check or client connects by IP;
- roll test: continuous `curl` against both VIPs during `kubectl rollout restart` of each Envoy
  deployment, zero failures (or a measured gap, tuned before going further); **done 2026-11-09**
  (pod-template roll through git instead of `rollout restart`, 0 failures, see step 4 notes);
- metrics in Grafana, a test trace in Tempo; **done 2026-11-09** (step 4 notes);
- #199 gate G11 on this deployment (wildcard `TLSRoute` matches two labels, passthrough coexists
  with the HTTPS listeners, h2 still negotiated on `https-grigri-cloud`); **done 2026-11-09 (step
  8, evidence in the steps 8 and 9 notes):** same certificate and same response through the bridge
  as directly via `.4`, two-label SNI passes through, h2 on `https-grigri-cloud` and
  `https-wildcard-staging-forkline-dev`, terminating hosts never reach passthrough.

**Phase 1 closed 2026-11-09** except step 6 (ZeroSSL ClusterIssuer, owner credentials) and P1.1
(optional pfSense). Neither blocks Phase 2.

Rollback: delete the app dir; nothing depends on it.

### Phase 2 - Pilot and patterns

Goal: settle the route pattern per definition style (A, B, C) and the auth pattern before
scaling. Same per-app procedure as Phase 3.

- A, no annotations: `firecrawl`; C raw: `rsshub`;
- A, auth with group check and identity headers: `esphome` and `http-echo` (Kanidm client,
  `groups` authorization, `claimToHeaders`, spoof test);
- auth skip: `navidrome` (`/rest`, `/share`);
- B chart values / CR: `hubble` (cilium values), `vault` (operator CR, `BackendTLSPolicy`);
- self-signed backend: `unifi` (EG `Backend` with TLS skip-verify);
- the kanidm route itself first (before any OIDC policy), then one Lua request case (`radarr`)
  and the Lua body rewrite (`navidrome`), to prove `EnvoyExtensionPolicy` early.

Write the per-app procedure into `docs/conventions/` (replaces the nginx snippet in `helm.md` /
`deploying-new-apps.md`).

**Gate 2:** every pilot passes the per-route checklist on both VIPs; auth: login, 403 for a user
outside the group, logout, skip paths open.

Gate 2 auth status (2026-11-09):

- [x] unauthenticated redirect with the right authorize parameters (all four apps, both VIPs)
- [x] forged cookies / bearer / spoofed identity headers rejected (302), skip paths open and strip
      spoofed identity headers (navidrome, radarr), skip boundaries identical to nginx
- [x] logout clears the Envoy cookies (no Kanidm end-session: next request re-logs silently)
- [x] client secret and HMAC delivered (bogus-code exchange reaches Kanidm's code check)
- [ ] **owner**: interactive login per app through the VIPs (throwaway Chromium profile with
      `--host-resolver-rules`, command in the procedure doc): 200, `x-auth-request-*` values in
      the http-echo JSON, a client-sent `X-Auth-Request-Preferred-Username` not echoed (copy the
      Cookie header into a `0600` file, `curl -H @file -H 'X-Auth-Request-Preferred-Username:
      spoofed'`), navidrome logs in the same user as today and `/app/#/login` lands on `/`
      (Lua), esphome log websocket held longer than 60 s, radarr UI
- [ ] **owner**: refusal for a person outside the group: a test person in no scope map group is
      refused by Kanidm at `/ui/oauth2` (create and delete it by hand). The Envoy 403 itself needs
      a token without the allowed SPN: after the login test, a temporary http-echo commit that
      allows only a non-existent group SPN must answer 403 `RBAC: access denied`, then revert
- [ ] **owner**: every member of `esphome-users` / `radarr-users` is also in a
      `k8s-oauth2-proxy` scope map group today (nginx needs both, the new clients only the app
      group); otherwise the new path admits someone nginx refused

### Phase 3 - App by app (no traffic moves)

Per app, in one PR (or one per gateway if it is easier to review):

1. internal route (`parentRefs: internal`, `sectionName` from the contract) with the app's
   policies. **No external-dns annotation yet** (internal records must not flip app by app).
2. verify against `.13`: `curl --resolve <host>:443:192.168.193.13`, browser with an
   `/etc/hosts` entry, the per-route checklist, the app's special case recipe.
3. external route (`parentRefs: external`) with the definitive external-dns annotations
   (`enabled` only on the route; the `target: grigri.cloud` annotation lives on the external
   Gateway, see Open Items): safe now, because external records are CNAMEs to the apex and do
   not change.
4. verify against `.14` the same way, from the LAN.
5. NetworkPolicies: if the app, or the namespace it lives in, has a policy that admits the nginx
   namespaces (grep `ingress-nginx` in the app and `kubectl get netpol,cnp -n <ns>`), it must
   also admit the Envoy proxies (namespace `envoy-gateway`, labels
   `app.kubernetes.io/name=envoy`, `app.kubernetes.io/component=proxy`,
   `gateway.envoyproxy.io/owning-gateway-name=<gateway>`) **before** step 2: a missing rule
   shows as 503 `UF` only on the Envoy path. Egress policies of pods that call hairpin names
   (resolving to `.14` after P5.2) need the same rule.
6. tick the app in the tracking list below. The Ingresses stay untouched.

Order: plain apps, then timeouts/body, then auth, then snippets, then the control-plane names
(argocd, vault, s3/minio, kanidm, oauth2-proxy-dependent apps last).

Tenants (their repos, in parallel, against the contract; per-app table, DNS rules and owner
checklist in [Tenant repos on grigri](#tenant-repos-on-grigri-multivara-kiais)): multivara
calypso web/api/external-api/landing, oauth2-proxy, keycloak (+ admin), vault, gitlab; kiais
isidoro and isidoro-keycloak (+ admin). The routes live in their `custom/` directories
(multivara promotes `grigri.cloud/` to production), external tenant routes carry **no**
external-dns annotation, and the Keycloak NetworkPolicies must admit `envoy-gateway` before
those routes can be verified. Ours (both deployed in Phase 1): the bridge `TLSRoute` for runners
and the internal `*.isidoro.grigri.cloud` listener.

Tracking list (fill during Phase 3): one row per app, columns `internal route` / `verified .13` /
`external route` / `verified .14`.

**Gate 3 (before any switch):**

- every Ingress in the cluster, **tenant ones included** (10 multivara, 3 kiais, plus the WIP
  `mcp-fixture`), has a route on the matching gateway, verified (compare
  `kubectl get ingress -A` hosts against `kubectl get httproute -A` hosts with a script; the
  runner hosts are covered by the bridge);
- scripted diff of every host: `curl -sk -o /dev/null -w '%{http_code} %{redirect_url}'` with
  `--resolve` to the nginx IP vs the Envoy IP, identical;
- functional list from the LAN against the Envoy IPs: nextcloud sync (WebDAV), immich mobile
  upload of a large video, `git push` over HTTPS and SSH, `mc ls` against s3-public, Home
  Assistant app (websocket), jellyfin playback, navidrome Subsonic client, calypso login and SSE
  stream, one runner host through the bridge, calypso-external-api returns 429 over 20 rps,
  calypso `/api` skip paths open and the rest behind auth, Keycloak login on `idm.staging` and
  `idm.isidoro` (checks the NetworkPolicy fix), gitlab clone and push over HTTPS (large body,
  3600s), an isidoro streaming completion longer than 60 s, the Vault UI on
  `vault.staging.internal.forkline.dev`;
- tenant prerequisites of the "Gate 3" row in the owner checklist are done (NetworkPolicy fixes,
  `BackendTLSPolicy`s, decisions A and C), and the existing blackbox probes for
  `app.staging.forkline.dev` and `api.staging.forkline.dev` pass against the Envoy path;
- no `UF`/`URX` response flags in Envoy access logs;
- prepared, unmerged: the internal switch commit, the hermes hostAliases/tests commit
  (`.4 -> .14`), and the commit removing the external-dns annotations from the nginx-external
  Service.

### Phase 4 - Internal switch (one move)

Same DNS mechanism as today: external-dns publishes internal names to Cloudflare from the
Ingress status. The switch makes the internal controller **report `.13`** instead of its own LB
IP, so external-dns rewrites every internal record in one sync. nginx-internal keeps running on
`.1` and serving clients with cached answers.

1. Pre-checks: break-glass ready (`kubectl port-forward` to argocd-server and vault), Envoy
   internal pods ready.
2. Merge `ingress-nginx: Publish the internal Envoy gateway address` (`publishService.enabled:
false`, `--publish-status-address=192.168.193.13`). This changes the status address of
   multivara's `*.staging.internal.forkline.dev` and kiais's internal Ingresses too, but their
   Cloudflare records use the alpha key and **do not follow it**: the three frozen internal names
   (`idm.staging.internal`, `vault.staging.internal`, `idm-internal.isidoro`) flip only when
   the tenant adds the non-alpha annotation to their internal route in this window (owner
   action, see the tenant section). Verify them in step 4 against `.13`.
3. pfSense P4.1 (`k8s.grigri`).
4. Verify: `kubectl get ingress -A` internal ones show `.13`; external-dns log shows the
   updates; `dig @1.1.1.1 argocd.internal.grigri.cloud` = .13; the records from the Phase 0
   exception list are unchanged; from a pod, `vault.internal`/`s3.internal` resolve to .13;
   External Secrets syncs (force one refresh), one backup job writes to minio, argocd and vault
   UIs, Grafana datasources (loki, prometheus-long-term) healthy.
5. Right after: `*: Publish internal routes in DNS` (external-dns annotation on all internal
   routes; same target `.13`, so no record changes; D7).
6. Watch 3-7 days: nginx-internal request rate goes to ~0 (residual = a client pinned to `.1`
   or a cached answer, find it).

Rollback: revert step 2 (records go back to `.1` within one external-dns interval + TTL) and
P4.1.

**Gate 4:** nginx-internal near zero requests for 3+ days, no regressions.

### Phase 5 - External switch (one window, at pfSense)

Low-traffic time. The owner does the pfSense steps; an agent watches and verifies.

1. Pre-checks: Gate 3 diff script re-run against `.14`, external Envoy pods ready, nginx
   healthy (rollback target), tenant routes (multivara, kiais) and the bridge `Accepted`,
   runner egress to the Envoy proxies in place in multivara (before P5.2: the hairpin names
   resolve to `.14`), the WIP calypso `mcp` rules updated from `.4` to `.14`.
2. pfSense **P5.1** (alias `k8s_external_ingress` -> .14), apply.
3. pfSense **P5.2** (3 host overrides -> .14), apply.
4. Merge the prepared commits (hermes hostAliases/tests, nginx-external Service annotations
   removed; the `*.app.staging` wildcard record stays, `upsert-only`). In the same window the
   kiais owner adds the non-alpha annotation to the `idm.isidoro.grigri.cloud` route (its A
   record is the one external tenant record that goes to `.14` only this way); the Ingress keeps
   its alpha key until Phase 6.
5. Check that every OIDC `SecurityPolicy` is still `Accepted` (the explicit
   `authorizationEndpoint`/`tokenEndpoint` mean no discovery against the moved `idm.grigri.cloud`,
   so no controller restart is needed).
6. Verify from outside (phone on mobile data) and from the LAN: the Gate 3 functional list,
   OIDC login on argocd/grafana/forgejo/freshrss/dawarich (in-cluster OIDC clients reach idm
   through the override, now `.14`), argocd webhook delivery from GitHub, Argo still syncing
   `multivara/gitops` from `git.grigri.cloud` (kiais is pulled from `tea.millaguie.net`,
   unaffected), external-dns healthy, the Forkline Remote passthrough listener reachable on
   public 443 (#199), from a runner: `git` and `idm.staging.forkline.dev` reachable (egress
   rule), `idm.isidoro.grigri.cloud` resolves to `.14` and isidoro logins work.
7. Watch one week: Envoy 5xx/latency alerts; nginx-external request rate drops to bridge
   traffic only. Then P5.3.

Rollback: P5.1 and P5.2 back to `.4` (seconds), revert the hermes commit, and set the
`idm.isidoro.grigri.cloud` A record back to `.4` by hand (`upsert-only` never reverts it). Routes
stay in place.

**Gate 5:** one week without regressions.

### Phase 6 - Decommission (both controllers together, D8)

Separate commits, in order:

1. Explicit `Certificate`s for any TLS secret found in Phase 0.5, then delete every `ingress:`
   block, raw Ingress template and chart `ingress.enabled` (grep templates for each removed
   values key so nothing falls back to a chart default). multivara and kiais do the same in
   their repos (owner actions; `ingress.enabled: false`, Keycloak and Vault `spec.ingress` off,
   the orphan Vault Ingress deleted with the owner's yes). Gate it with
   `grep -rn 'nginx\|192.168.193.[14]'` over both tenant `grigri.cloud/` trees (no hits left
   except the runner path while bridged).
2. Remove `system/oauth2-proxy`, its Kanidm client and Vault paths (D3). This is ours only: the
   oauth2-proxy in the `calypso` namespace is multivara's and follows decision A in the tenant
   section.
3. Remove `system/ingress-nginx`. Remove `system/ingress-nginx-external` **only after** calypso
   runners use HTTPRoutes and the bridge listener is replaced; until then it serves runners
   only (it also needs the `runners-subdomain` Certificate and the runner network policy
   selector, both multivara-owned, to stay). Then clean what Argo does not prune: IngressClasses, admission webhook configs,
   leader-election Leases, ClusterRoles, leftover Secrets.
4. Drop nginx references from NetworkPolicies (ours and the tenants': Keycloak
   NetworkPolicies, runner policy), otel-collector rules, Loki/Tempo configs, dashboards and
   alerts (including the isidoro edge alerts and the calypso landing analytics queries).
5. Docs: `AGENTS.md` tech stack, `docs/conventions/helm.md`, `deploying-new-apps.md` decision
   tree, README network section, `docs/network/pfsense.md`; an ADR for the Gateway API choice.
6. pfSense P6.1-P6.3.

**Gate 6:** zero Ingress objects (or only runner ones while the bridge exists), no
`nginx_ingress_controller_*` series once both are gone, no `.1`/`.4` in Cilium LB IPAM, all Argo
apps Synced/Healthy.

## Verification Checklist (per route)

- [ ] status and redirects match nginx; `http://` redirects to `https://`
- [ ] valid cert for the host, h2 negotiated (also on a second host after any listener change)
- [ ] HSTS, security headers present; `Server` absent
- [ ] auth: 302 to login unauthenticated, 403 wrong group, 200 right group, skip paths open
- [ ] the app's special case recipe, if any
- [ ] large upload, long request (timeouts), websocket/SSE where the app uses them
- [ ] paths with `%2F`, trailing slashes, prefix boundaries (`/api` vs `/apifoo`)
- [ ] real client IP in the app log
- [ ] Envoy access log `response_flags` empty

## Troubleshooting (expected failure modes)

| Symptom                                 | Likely cause                                                                                             |
| --------------------------------------- | -------------------------------------------------------------------------------------------------------- |
| new VIP unreachable from LAN            | not advertised: `Local` with no ready endpoint (proxies Pending, e.g. placement too strict)              |
| 504 after 15s                           | gateway-wide request timeout missing or overridden by a route policy without `StrategicMerge`            |
| S3/MinIO 403 `SignatureDoesNotMatch`    | compression enabled on an S3 host                                                                        |
| `%2F` paths return 3xx/404              | `escapedSlashesAction` not `KeepUnchanged`                                                               |
| 426 for old clients                     | `http1.http10` not enabled                                                                               |
| h2 lost on all hosts                    | hostname-scoped TLS listener without pinned ALPN                                                         |
| intermittent 503 `UF`                   | backend expects TLS (missing `BackendTLSPolicy`) or a NetworkPolicy still admits only nginx              |
| VIP unreachable during roll             | `maxSurge 0` with `Local`; BGP withdraws before the new pod is ready                                     |
| OIDC 500 on a route                     | policy not `Accepted`: endpoints not set explicitly and discovery failed; set them (no restart needed)   |
| internal record not moved after Phase 4 | Ingress has a `target` annotation or is not `nginx-internal`; or the name is a Service record (expected) |
| app lost TLS secret                     | Ingress deleted with owner-ref'd Certificate still mounted elsewhere                                     |
| LAN works, outside fails (or reverse)   | P5.1 and P5.2 out of step: LAN uses the override, outside uses the alias                                 |

## Files to Modify

New: `system/envoy-gateway/**`, `docs/conventions/` gateway procedure, `docs/network/pfsense.md`,
ADR. Changed: `system/external-dns/values.yaml`, `system/cert-manager` (ZeroSSL issuer),
`system/ingress-nginx/values.yaml` (Phase 4 publish address), `platform/git` (TCPRoute), every
app in the inventory, `apps/hermes/values.yaml` + tests, mcp-gateway(-admin) network policies,
`system/otel-collector/values.yaml`, `system/loki/...`, `system/tempo/...`, monitoring alerts,
docs. Removed (Phase 6): `system/ingress-nginx/`, `system/oauth2-proxy/`, later
`system/ingress-nginx-external/`. Other repos (owner actions): multivara/gitops routes in
`custom/`, Keycloak NetworkPolicy and runner egress fixes, `DNSEndpoint`s for new names; kiais/gitops
routes and Keycloak NetworkPolicy; calypso runner HTTPRoute support. Ours for tenants: bridge
`TLSRoute`, internal `*.isidoro.grigri.cloud` listener and Certificate. Manual: pfSense table above.

## Execution Order

Phase 0 -> 1 -> 2 -> 3 (all apps, both gateways, no traffic) -> 4 (internal switch) -> 5
(external switch at pfSense) -> 6 (decommission both). #199's DNS/ZeroSSL/Keycloak items run in
parallel from Phase 1; Forkline Remote staging works from Phase 5 on.

## Open Items

- Calypso HTTPRoute support for runners (multivara/calypso work, not covered by #199); gates
  the removal of ingress-nginx-external and of the bridge listener.
- Phase 1 leftovers: step 6 (ZeroSSL ClusterIssuer, waits for the owner's EAB credentials in
  Vault) and P1.1 (optional pfSense).
- Tenant open points (details in the tenant section): web auth design for calypso (ext auth vs
  OIDC, spike), runner auth and RBAC for HTTPRoutes, Keycloak backend TLS vs plain port, the
  whether the operators
  delete their Ingress when `spec.ingress` is turned off (Vault's does not), the kiais owner's
  lead time, and the unmerged calypso network policies that hard-code `.4`.
- kroki: caching sidecar image and config (recipe 6).
- ~~Envoy Gateway issue #10075~~ **Resolved (2026-11-09):** the crash (SIGSEGV when an OIDC
  `SecurityPolicy` is deleted and recreated: SDS secret provider destroyed on a worker thread) is
  fixed by Envoy PR 47631, backported to `release/v1.39` as #47784 (`cb66a76524`, between v1.39.3
  and v1.39.4), so `distroless-v1.39.6` has it. Live: no proxy restart across 6 OIDC policy
  creations and updates; secrets delivered (bogus-code check). One transient `initial fetch timed
  out` for two SDS secrets when a policy became valid together with its Secret; it recovered.
  Not exercised: deliberate delete/recreate churn (needs `kubectl delete`).
- Interactive OIDC login, Envoy 403 and the Lua `#/login` rewrite: owner test (Gate 2).
- Websockets through a route with the 60 s request timeout (esphome logs) unverified.
- Gateway-wide security headers override app headers (Phase 2 auth findings): decide.
- Kanidm: startupProbe or longer probe timeouts against reindex restarts (Phase 2 auth findings).
- Helm hooks (the `gateway-helm` certgen Job, RBAC) under kustomize `helmCharts` become Argo
  hooks; they need the namespace first. Verify in the Phase 1 step 1 sync.
- Gateway `status.addresses` confirmed live (`.13` / `.14`, step 2). Still untested: the tracing
  `ReferenceGrant`.
- ~~external-dns `target` is per Gateway~~ **Resolved (2026-11-09, step 2):** the external
  Gateway carries `external-dns.kubernetes.io/target: grigri.cloud` (every homelab-owned external
  Ingress targets it today); the internal Gateway has no target (records default to `.13`).
  `mintpsicologia.es` routes carry no external-dns annotation (records already exist and
  external-dns runs upsert-only); multivara's forkline hosts are their concern. Inert until the
  `gateway-httproute` source is added (step 5).
- bitnami wordpress: `charts.bitnami.com` pull fails (pre-existing); use OCI
  `registry-1.docker.io/bitnamicharts/wordpress`; run `helm dependency build` on the real chart
  before the mintpsicologia step.
- forgejo chart `httpRoute.matches.timeouts` renders invalid: timeouts only via Envoy policies.
