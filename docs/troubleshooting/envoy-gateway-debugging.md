# Envoy Gateway debugging

## Problem

A route on the Envoy Gateways (see [Ingress](../network/ingress.md)) misbehaves: wrong status, no
route, auth denial, upstream failure. The Envoy image is distroless (no `wget`, `curl`, `sh`), so
`kubectl exec` into the proxy is not an option. This page collects the working access paths and the
known quirks.

## Root Cause

Not a single fault. The recurring causes are listed under Fix / Workaround: access-log flags to
read, two log leaks that are still open, and two Argo CD behaviours that look like failures but are
not.

## How to Diagnose

### Admin interface via port-forward

The Envoy admin port is `19000` inside the proxy pod. Forward it and use `curl` locally:

```sh
kubectl --context=grigri -n envoy-gateway get pods
kubectl --context=grigri -n envoy-gateway port-forward pod/<envoy pod> 19111:19000 &
curl -s 'localhost:19111/config_dump?resource=dynamic_listeners'
curl -s 'localhost:19111/config_dump?resource=dynamic_route_configs'
curl -s 'localhost:19111/stats?filter=<substring>'
curl -s 'localhost:19111/clusters'
```

List the HTTP filters of every filter chain (policy-derived filters are named
`envoy.filters.http.<kind>/securitypolicy/<ns>/<name>`):

```sh
curl -s 'localhost:19111/config_dump?resource=dynamic_listeners' \
  | jq -r '.configs[].active_state.listener.filter_chains[].filters[].typed_config.http_filters[].name'
```

`/clusters` shows per-upstream connect failures, for example cluster `idm_grigri_cloud_443` for the
OIDC token and JWKS calls Envoy makes to `idm.grigri.cloud` through its own VIP.

### Access-log response flags

Read the `response_flags` field of the access log (Loki or `kubectl logs`):

| Flag                    | Meaning                                                        |
|-------------------------|----------------------------------------------------------------|
| `NR`                    | No route: 404 for an unknown host or path                      |
| `UAEX`                  | Denied by the external authorization service                   |
| `DC`                    | Downstream closed: normal for streaming clients                |
| `UF` / `UH` / `URX` / `UC` | Upstream failure: investigate the backend                   |

### Envoy behaviour that looks like a bug but is expected

Port 80 redirects with 301 (nginx 308); 404/403/redirects have empty bodies; headers containing
underscores are dropped; `nextcloud` `/` answers 302 on Envoy and 200 on nginx. Envoy merges
duplicate slashes and decodes unreserved percent-encoding before matching, so a deny rule must match
the normalized path.

## Fix / Workaround

### Known log leaks (open issues, no mitigation yet)

- When Envoy cannot decrypt an OIDC cookie it logs the **raw cookie value** (seen in Loki).
- Query strings that carry API keys (for example radarr) appear in access logs.

Treat Loki content from the proxies as sensitive, do not paste it into docs or tickets, and rotate a
key that has been pasted somewhere durable.

### Argo CD shows `Progressing` for a label-selected `BackendTrafficPolicy`

A `BackendTrafficPolicy` that selects routes by label (`targetSelectors`) reports `Progressing` in
Argo CD while no matching route exists, because it has no status yet. It is not an error and clears
once a matching route appears.

### Argo CD never refreshes an app by itself

Argo CD here reconciles on webhooks only (`timeout.reconciliation: 0` in `argocd-cm`). An app whose
source repository sends no webhook never refreshes on its own: trigger a refresh by hand after
pushing.

### Ingress-side checks

- A route that "works" for every path may be an SPA shell answering 200: probe a path only the
  backend serves.
- Moving an app from oauth2-proxy to Envoy native OIDC makes users log in once more (new cookies).
- Envoy proxy pods listen on container ports `10443` and `10080`, so NetworkPolicy egress toward
  them must allow those, not 443 and 80.
- Long connections cut after a policy change: see
  [listener drain](envoy-gateway-listener-drain.md).
