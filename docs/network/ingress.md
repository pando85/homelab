# Ingress

How HTTP(S) and TCP traffic reaches the cluster: Envoy Gateway, the pfSense forwards in front of it,
and the DNS that points clients at it. Why Envoy Gateway:
[ADR-0005](../adr/0005-gateway-api-with-envoy-gateway.md). For writing a route see
[Exposing an app with Gateway API](../conventions/gateway-api-routes.md). For the pfSense inventory
(interfaces, BGP, all aliases) see [pfSense](pfsense.md).

## Gateways

Envoy Gateway runs in namespace `envoy-gateway`. Both Gateways are `LoadBalancer` Services announced
over BGP by Cilium (see [Cilium BGP control plane](../deployment/cilium-bgp-control-plane.md)).

| Gateway    | VIP              | Reachable from | Hosts                                      |
|------------|------------------|----------------|--------------------------------------------|
| `internal` | `192.168.193.13` | LAN only       | `*.internal.grigri.cloud` and similar      |
| `external` | `192.168.193.14` | Public         | `*.grigri.cloud`, tenant and other domains |

The listener definitions are in `system/envoy-gateway/resources/gateway-external.yaml` and
`gateway-internal.yaml`. All HTTPS listeners of a Gateway share one Envoy listener on 443; each
Gateway listener (hostname) becomes one filter chain. This matters for
[listener drain](../troubleshooting/envoy-gateway-listener-drain.md).

### External listeners

| Listener                                | Port  | Serves                                                                 |
|-----------------------------------------|-------|------------------------------------------------------------------------|
| `http`                                  | 80    | Redirect to HTTPS, 301                                                |
| `https-grigri-cloud`                    | 443   | `*.grigri.cloud` wildcard, the bulk of the routes                      |
| `https-staging-forkline-dev`            | 443   | `staging.forkline.dev` (tenant)                                        |
| `https-wildcard-staging-forkline-dev`   | 443   | `*.staging.forkline.dev` (tenant)                                      |
| `https-runners-staging-forkline-dev`    | 443   | `*.app.staging.forkline.dev`, terminated, HTTPRoute only, routes accepted only from namespace `calypso-runners` |
| `tls-remote-staging-forkline-io`        | 443   | `*.remote.staging.forkline.io`, TLS passthrough (no routes today)      |
| `https-mintpsicologia-es`, `-www`       | 443   | `mintpsicologia.es` and `www.`                                         |
| `tcp-forgejo-ssh`                       | 29518 | TCPRoute to Forgejo SSH                                                |

The internal Gateway carries its own listeners: wildcards for `internal.grigri.cloud`, Isidoro,
Rajoy and the tenant staging domain (see `gateway-internal.yaml`).

### Authentication

Protected apps use an Envoy Gateway `SecurityPolicy` (native OIDC against Kanidm plus a JWT check on
a per-app claim), with one Kanidm client and one `<app>-users` group per app. There is no
shared oauth2-proxy and no `auth.grigri.cloud` host (a tenant runs its own for its own apps).
Recipe and operating details: [Authentication](../conventions/gateway-api-routes.md#authentication) and
[ID token cookie size](../troubleshooting/envoy-oidc-id-token-cookie-size.md).

### Removed: ingress-nginx

ingress-nginx (`192.168.193.1` internal, `192.168.193.4` external), its Ingresses and oauth2-proxy
were removed after both switches soaked; the two VIPs are unused. The Gateways are the only
ingress path, so a route is the only way to expose an app and there is no nginx to roll back to
(history: `planning/ingress-nginx-to-envoy-gateway.md`).

## pfSense in front of the external Gateway

pfSense configuration is not in git and **changes are owner-only**; agents inspect read-only over
`ssh pfsense.grigri`. The full inventory is in [pfSense](pfsense.md); what matters for ingress:

- **Alias `k8s_external_gateway`** (Firewall > Aliases > IP) is the single target of the WAN port
  forwards for 80, 443 and 29518. It expands to 21 `rdr` rules (including one per LAN
  interface for NAT reflection) plus the WAN pass rule. Editing the alias moves public traffic,
  NAT reflection and Forgejo SSH in one change. It points at `192.168.193.14`.
- **Unbound host overrides** (Services > DNS Resolver > Host Overrides) make LAN and in-cluster
  clients resolve public names straight to the gateway VIP instead of the public IP. Without them,
  traffic from inside would leave through the WAN address and come back through NAT reflection
  (hairpin). Three hosts with aliases cover 36 names, all on `192.168.193.14`:
  `freshrss.grigri.cloud` (+28 aliases), `app.staging.forkline.dev` (+4: `auth`, `idm`, `api`,
  `mcp-fixture`), and `mintpsicologia.es` (+ `www`). Editing the host IP moves its aliases. Do not
  touch `turn` (`.5`) or `unifi*` (`.2`). The custom option `k8s.grigri` points at `.13`.
- **Names without an override** (for example `photos`, `lidarr`, `prowlarr`, `mcp`, `s3`,
  `requests`) resolve to the public IP from the LAN and reach the gateway through NAT reflection,
  i.e. through the same alias.
- pf keeps the redirect target of existing states, so an alias change does not move open connections:
  [pfSense alias change keeps old states](../troubleshooting/pfsense-alias-change-keeps-old-states.md).

Read-only checks:

```sh
ssh pfsense.grigri 'pfctl -t k8s_external_gateway -T show; pfctl -sn | grep -c "rdr.*k8s_external_gateway"; pfctl -ss | grep -cE "192\.168\.193\.[14][: ]"'
ssh pfsense.grigri 'grep -E "^local-data: \"[^ ]+ A 192.168.193\." /var/unbound/host_entries.conf'
```

Never dump `config.xml` whole (secrets).

## DNS (external-dns)

external-dns publishes to Cloudflare with policy `upsert-only` and sources `service`, `ingress`,
`gateway-httproute` and `crd`.

- **Opt-in:** a route is published only with `external-dns.kubernetes.io/enabled: "true"`.
- **External Gateway:** carries `external-dns.kubernetes.io/target: grigri.cloud`, so an
  annotated route on it produces a CNAME to `grigri.cloud`. A route cannot produce an A record
  there. For an A record (for example a name that must resolve to a private IP, as
  `idm.isidoro.grigri.cloud` does) use a `DNSEndpoint` (the `crd` source). The tenant owns the
  `DNSEndpoint` holding `*.app.staging.forkline.dev`.
- **Internal Gateway:** has no target annotation, so annotated routes get A records to `.13`.
- **Annotation prefix:** since external-dns v0.22.0 only `external-dns.kubernetes.io/*` is read
  (this cluster runs v0.23.0). `external-dns.alpha.kubernetes.io/*` annotations (`target`,
  `hostname`, `ttl`, ...) are silently ignored; there is no fallback. v0.23.0 added the opt-in flag
  `--enable-legacy-annotation-prefix` (off by default, the configured prefix wins when both are
  present, planned for removal). Trap: `--annotation-filter` matches the raw annotation key, so a
  filter written with the legacy key would still select objects while their legacy `target` is
  ignored; an object meant to get a CNAME then gets an A record from its status IP, and where a
  CNAME already exists Cloudflare rejects it with error 81054 and the whole batch fails. The filter
  here uses the non-alpha `enabled` key, so an object that only carries
  `external-dns.alpha.kubernetes.io/enabled` is ignored entirely.
- **Upsert-only:** external-dns never deletes or reverts a record. Removing an annotation, or rolling
  a route back, leaves the record in Cloudflare. To roll back a moved record, edit it in Cloudflare
  by hand **and** revert the source in git; otherwise external-dns re-asserts the record within its
  2 minute interval. Records are created with `ttl=1` (auto).
- **No record type changes:** upsert-only can never change a name's record TYPE (A to CNAME or back).
  external-dns tries to CREATE the new type while the old record exists, and Cloudflare rejects it
  (error 81054, "A CNAME record with that host already exists"). The whole batch then fails every
  interval; external-dns falls back to individual changes, so only the conflicting names stay stuck.
  When moving a name between gateways or sources, keep its record type (A stays A, CNAME stays
  CNAME) or delete the old record by hand first. An annotated route on the external Gateway yields
  a CNAME and a `DNSEndpoint` yields an A, so moving a name from one to the other is a type change.
- **Orphaned records:** upsert-only also means that removing or renaming a route or Ingress leaves
  its A or CNAME record (and the `a-<name>` TXT owner record) in Cloudflare, still resolving to the
  old target. Removal is a manual Cloudflare delete of the record and its TXT owner records. To find
  them, collect hostnames from git history with
  `git log -p --all | grep -o -E '[a-z0-9.-]+\.grigri\.cloud' | sort -u`, `dig +short <name> @1.1.1.1`
  each and compare against the live routes (`kubectl get httproute,ingress -A`), and check the
  Cloudflare dashboard or API for names never in git. Point-in-time list and cleanup step:
  `planning/ingress-nginx-to-envoy-gateway.md` (Phase 6).

## Rolling back

Change nothing by hand: revert the commit and let Argo CD sync (see
[ArgoCD GitOps workflow](../troubleshooting/argocd-gitops-workflow.md)). A revert that changes a
`SecurityPolicy`, `EnvoyExtensionPolicy` or certificate on a listener drains that listener like the
original change did, so do not revert during a long transfer.

The two VIPs are the only targets: alias `k8s_external_gateway` stays at `192.168.193.14` and the
unbound overrides at `.14`. If the external Gateway itself is down, public traffic has no fallback;
bringing ingress-nginx back means reverting its removal commits in git (owner decision).

## Verifying a change

- `scripts/gateway-check.sh`: read-only. Gateway and listener health, route and policy acceptance,
  pfSense alias and override counts, and host probes. Expected differences when reading old
  captures: port 80 redirect 301 vs 308, empty bodies on 404/403/redirects, headers containing
  underscores dropped by Envoy.
- A single-page app that answers 200 to any path proves nothing about auth. Probe a path only the
  protected backend serves (for example an `/api/` path that returns 401 on bad credentials).
- Debugging Envoy itself: [Envoy Gateway debugging](../troubleshooting/envoy-gateway-debugging.md).

## Pods that call a public name

A pod that calls a public hostname but must reach the in-cluster VIP either relies on the unbound
override (in-cluster clients resolve it too) or pins it with `hostAliases` to the gateway IP. Egress
NetworkPolicy toward Envoy must allow the container ports `10443` and `10080`, not 443 and 80.
Envoy itself reaches `idm.grigri.cloud` through its own external VIP for OIDC token exchange and JWKS.
