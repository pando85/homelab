# Envoy Gateway listener changes close long connections

## Problem

Websockets, SSE streams and long downloads served through an Envoy Gateway listener are cut, all at
once, shortly after an unrelated policy or certificate change on the same listener. The cut is
roughly 60 seconds after the change, and it affects every host on that listener, not only the one
that changed.

## Root Cause

All HTTPS hosts of a Gateway share one Envoy listener on 443. Each Gateway listener (hostname) is one
filter chain. Each `SecurityPolicy` (extAuth, OIDC, JWT) and each `EnvoyExtensionPolicy` (Lua)
becomes its own named HTTP filter in that chain's filter list, for example
`envoy.filters.http.ext_authz/securitypolicy/<ns>/<name>`: disabled at chain level and enabled per
route.

Adding or removing such a policy, changing a certificate on a listener, or any listener change
replaces the filter chain. Envoy drains the old chain and every connection still open on it is closed
when the drain ends. The proxies run `--drain-strategy immediate --drain-time-s 60` (Envoy Gateway
default; the `EnvoyProxy` sets no `shutdown`).

- `HTTPRoute`-only changes go through RDS and drain nothing.
- Other filter chains (other hostnames) are untouched.
- A per-route policy that changes often (one per tenant runner) therefore cuts every other long
  connection on that listener each time it is added or removed.

Comparison with ingress-nginx: every Ingress add or remove reloads the controller, and old workers
close their remaining connections after `worker-shutdown-timeout` (default 240 s), on every host of
that controller. Envoy is narrower (one listener chain, 60 s), not absent. Tenant runner churn
produced about 60 nginx reloads per day (423 runner Ingresses in 14 days).

## How to Diagnose

The Envoy image is distroless (no `wget`, `curl`, `sh`), so use a port-forward (details in
[Envoy Gateway debugging](envoy-gateway-debugging.md)):

```sh
kubectl --context=grigri -n envoy-gateway port-forward pod/<envoy pod> 19111:19000 &
curl -s 'localhost:19111/config_dump?resource=dynamic_listeners' \
  | jq -r '.configs[].active_state.listener.filter_chains[].filters[].typed_config.http_filters[].name'
```

Each chain lists its HTTP filters; the policy-derived ones carry the `securitypolicy/<ns>/<name>`
suffix. If the policy you changed is in a chain that also serves long-lived clients, expect a drain.
Compare `/stats?filter=listener_manager` counters before and after to see listener modifications.

## Fix / Workaround

This is expected behaviour, not a fault. Operating rule: **make no listener, certificate or
Security/ExtensionPolicy changes during long transfers, and batch them** so one drain covers several
changes. Route-only edits are safe. Avoid designs that create or delete a policy per short-lived
object on a listener shared with long-lived connections.
