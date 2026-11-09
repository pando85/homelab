# pfSense: cluster-relevant configuration

pfSense configuration is not managed in git. This page records the parts the Kubernetes cluster
depends on, so that changes can be planned against them. Snapshot taken 2026-11-09 on pfSense
2.8.1 by read-only inspection. Re-check before acting on any value here.

For the FRR (BGP) setup on the cluster side, see
[Cilium BGP control plane](../deployment/cilium-bgp-control-plane.md). For the planned changes,
see `planning/ingress-nginx-to-envoy-gateway.md` in the repository root.

## 1. Interfaces

| Interface | Name | Address | Purpose |
|-----------|------|---------|---------|
| `pppoe0` | WAN | Single public IPv4 (PPPoE) | Internet uplink. No WAN virtual IPs |
| `igb1.101` | DMZ | 192.168.192.1/24 | Kubernetes nodes and BGP peers |
| `bridge0` | LANBridge | 192.168.76.254/24 | Main LAN |
| `igb1.501` | IOT | 192.168.86.254/24 | IoT devices |
| `igb1.502` | IOT_OFFLINE | 192.168.96.254/24 | IoT devices without internet access |
| `ovpns1` | VPN | n/a | OpenVPN server |
| (guest Wi-Fi) | Guest | n/a | Guest Wi-Fi, see [Limiters](#7-limiters) |

Packages relevant to the cluster: FRR (BGP), pfBlockerNG, node_exporter.

There is no HAProxy or other reverse proxy. Public ports are plain pf port forwards
(see [Port forwards](#3-port-forwards)).

## 2. Kubernetes LoadBalancer VIPs

The VIPs live in 192.168.193.0/24 and are learned by pfSense over BGP
(see [BGP](#6-bgp-frr)).

| Address | Service |
|---------|---------|
| 192.168.193.1 | ingress-nginx (internal) |
| 192.168.193.2 | UniFi |
| 192.168.193.3 | transcoder-rabbit |
| 192.168.193.4 | ingress-nginx (external) |
| 192.168.193.5 | coturn (TURN) |
| 192.168.193.7 | qBittorrent |
| 192.168.193.8 | Kanidm LDAPS |
| 192.168.193.9 | Mosquitto |
| 192.168.193.10 | Mosquitto TLS |

Aliases that reference addresses not advertised over BGP:

| Address | Alias | Note |
|---------|-------|------|
| 192.168.193.6 | `k8s_deluge` | Not advertised |
| 192.168.193.11 | `k8s_pt` | Not advertised |

Planned (see the planning document above):

| Address | Service |
|---------|---------|
| 192.168.193.13 | Envoy Gateway (internal) |
| 192.168.193.14 | Envoy Gateway (external) |

## 3. Port forwards

All rules are on WAN, with the destination set to the WAN address. The filter rule is
auto-associated with each redirect.

| Rule | Ports (port alias) | Target (host alias) |
|------|--------------------|---------------------|
| grigri port redirect tcp | `external_ingress_tcp` = 80, 443, 29518 | `k8s_external_ingress` (192.168.193.4) |
| TURN tcp | `turn_tcp` = 3478, 3479, 5349 | `k8s_turn` (192.168.193.5) |
| TURN udp | `turn_udp` = 3478, 3479 | `k8s_turn` (192.168.193.5) |
| qbittorrent | `qbittorrent_tcp` = 50413 | `k8s_qbittorrent` (192.168.193.7) |

- Port 29518 is Forgejo SSH.
- Mosquitto (.9, .10) has no port forward. It is reachable from the LAN only: explicit passes for
  1883 and 8883 from IOT_OFFLINE, and any-to-any on the other internal interfaces.
- UniFi (.2) is not forwarded.
- NAT reflection is enabled globally (pure NAT with the NAT proxy helper). Internal clients that
  use the public address of a forwarded port reach the same target.

## 4. Host and port aliases

Host aliases:

| Alias | Address | Note |
|-------|---------|------|
| `k8s_external_ingress` | 192.168.193.4 | |
| `k8s_turn` | 192.168.193.5 | |
| `k8s_plex` | 192.168.193.5 | Stale description: same address as `k8s_turn` |
| `k8s_qbittorrent` | 192.168.193.7 | |
| `k8s_deluge` | 192.168.193.6 | Not advertised |
| `k8s_mosquitto` | 192.168.193.9 | |
| `k8s_mosquitto_tls` | 192.168.193.10 | |
| `k8s_pt` | 192.168.193.11 | Not advertised |

Port aliases:

| Alias | Ports |
|-------|-------|
| `external_ingress_tcp` | 80, 443, 29518 |
| `turn_tcp` | 3478, 3479, 5349 |
| `turn_udp` | 3478, 3479 |
| `qbittorrent_tcp` | 50413 |
| `mosquitto_tcp` | 1883 |
| `mosquitto_tls_tcp` | 8883 |
| `pfsense_internal_services_tcp` | 179, 6443, 9100, 666, 53 |

## 5. DNS Resolver (unbound)

- Resolves recursively. It does not forward upstream.
- DNSSEC is off.
- No private-address rebinding filter is configured. Public records that point at private
  addresses resolve normally. For example, `*.internal.grigri.cloud` records published to
  Cloudflare by external-dns resolve to the internal VIPs.

Host overrides (split horizon):

| Override | Address | Aliases |
|----------|---------|---------|
| `freshrss.grigri.cloud` | 192.168.193.4 | See the list below |
| `app.staging.forkline.dev` | 192.168.193.4 | `auth`, `idm`, `api` (all `.staging.forkline.dev`) |
| `mintpsicologia.es` | 192.168.193.4 | `www.mintpsicologia.es` |
| `turn.grigri.cloud` | 192.168.193.5 | |
| `unifi-controller.grigri` | 192.168.193.2 | `unifi.grigri` |

Aliases of `freshrss.grigri.cloud` (all `.grigri.cloud`):

- `gitea`, `navidrome`, `wallabag`, `argocd`, `bazarr`, `jackett`, `sonarr`, `radarr`
- `accounts`, `nextcloud`, `qbittorrent`, `jellyfin`, `idm`, `auth-kanidm`, `grafana`
- `antdroid`, `auth`, `appdaemon`, `dawarich`, `atuin`, `git`, `gitlab`, `hass`, `searxng`
- `readest`

Two personal-name hostnames under the `grigri.cloud` override were left out on purpose. Read them
from the resolver's host override list if needed.

Custom options:

- `local-zone: "k8s.grigri" redirect`
- `local-data: "k8s.grigri A 192.168.193.1"`

These are stored base64-encoded in `config.xml`, so a plain `grep` of the config does not find
them.

Names not listed above (for example `mcp`, `photos`, `requests`) resolve publicly and reach the
cluster through NAT reflection.

## 6. BGP (FRR)

Global settings:

| Setting | Value |
|---------|-------|
| Local ASN | 64512 |
| Router ID | 192.168.192.1 |
| Route map | `allow-all` |

Neighbors:

| Neighbor | ASN | Route map | Other settings |
|----------|-----|-----------|----------------|
| 192.168.192.2 (grigri) | 64513 | `allow-all` in/out | next-hop-self, allowas-in |
| 192.168.192.3 (prusik) | 64513 | `allow-all` in/out | next-hop-self, allowas-in |
| 192.168.192.11 (k8s-amd64-1) | 64513 | none | Established, but exchanges no prefixes |

- 192.168.192.11 is configured and Established but has no route map, so no prefixes are exchanged.
  This is harmless while that node only runs CI workloads. To change this, give it the same
  settings as the other two neighbors.
- No `maximum-paths` is set, so one path is installed per VIP.

## 7. Limiters

- Only the guest Wi-Fi upload and download limiters exist.
- No limiter applies to the cluster VIPs. The inbound limiter described in
  [bandwidth protection](../conventions/bandwidth-protection.md) is a recommendation, not
  configured.

## 8. How to re-inspect (read-only)

Connect with `ssh pfsense.grigri`, then run show-only commands:

```sh
vtysh -c 'show ip bgp summary'
vtysh -c 'show ip bgp 192.168.193.0/24 longer-prefixes'
pfctl -sn                          # rdr (port forward) rules
pfctl -t <alias> -T show           # contents of a table alias
pfctl -ss | grep -c <ip>           # state count for an address
```

Warning: never dump `/cf/conf/config.xml` whole. It contains secrets. Extract only the specific
fields you need.
