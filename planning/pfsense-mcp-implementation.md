# pfSense MCP Backend Implementation Plan

**Status:** BLOCKED at phase 0. The code and chart can be written, but nothing can be
deployed or validated until two owner-side items exist: a pfSense-side decision on how to
expose the REST API to the cluster (`§3`), and a read-only API token that this pfSense
actually accepts (`§4`). Nothing here is applied or enabled.
**Drafted:** 2026-11-10 against `master` at `c372b1d80` (Playwright Stage 2 live).
**Directive:** install `tomvaisbort/pfsense-mcp:1.1.0-59dc257` as the first backend on the
**admin** plane, with the environment block supplied by the owner, read-only.
**Gap being closed:** the admin plane (`apps/mcp-gateway-admin`, namespace
`mcp-gateway-admin`) has served `routes: []` since it was built. This is its first route, so
it is also the first time the admin plane's discovery shape, backend-credential injection and
Kanidm client are exercised for real.
**Architecture:** [`docs/design/mcp-gateway-architecture.md`](../docs/design/mcp-gateway-architecture.md)
(admin plane section, "Admin plane (deployed, inactive)" and the deferred Vault row).
**Does not duplicate:** [`vault-mcp-implementation.md`](vault-mcp-implementation.md) (Vault MCP,
deferred 2026-11-10) and [`mcp-gateway-implementation.md`](mcp-gateway-implementation.md)
(public-plane acceptance record). This plan reuses their credential and CEL patterns.

All cluster statements below are read-only measurements on 2026-11-10 with
`kubectl --context=grigri` or SSH to the named host. All upstream statements are cited to a
fetched file, a line in it, or an image layer. Anything not measured is marked UNVERIFIED.

## 1. Summary and recommendations

| Question | Recommendation | Strongest reason |
|---|---|---|
| Which image | `tomvaisbort/pfsense-mcp:1.1.0-59dc257`, pinned by **digest** `sha256:8fe68845c07739cd0c60588019b1efbdd093d495554591af1c6e6b770babe817` | It is the only tag that exists, and the digest is the only stable identifier |
| Supply chain | Pin the digest, record the upstream source in the chart, and do not treat the Docker Hub account as the project | The image is published by a personal account; its own OCI label points at `github.com/gensecaihq/pfsense-mcp-server` (MIT) |
| Which plane | **Admin** (`apps/mcp-gateway-admin`), route `/pfsense/mcp` | Owner directive; pfSense is the network root and must not be reachable from the public plane |
| How the gateway authenticates to the backend | A generated shared bearer (`MCP_API_KEY`) that the gateway injects via `backendAuth.key` | The server refuses to start on `streamable-http` without it (`src/main.py:131`) |
| Gateway session mode | **stateless**, proven with the existing probe harness | Stateful mode caches a backend credential per session, which is the recorded reason the Vault route is blocked |
| How the API-only exposure question is resolved | Open decision, `§3`. Recommended: dedicated egress source IP, then a single-IP pfSense rule | pfSense cannot tell pods apart behind masquerade, and 80 of 86 namespaces have default-allow egress |
| Read-only | `MCP_READ_ONLY=true` (correct variable name, verified in source) | Exposes only the 131 read-only tools (`src/main.py:15`) |

## 2. Verified facts

### 2.1 Image and supply chain

| Fact | Evidence |
|---|---|
| Tag exists, and it is the only one | Docker Hub tags API: the full set is `1.1.0-59dc257` |
| Digest | `sha256:8fe68845c07739cd0c60588019b1efbdd093d495554591af1c6e6b770babe817` |
| Architecture | `amd64/linux` only. Acceptable: all three nodes are amd64 (`grigri`, `k8s-amd64-1`, `prusik`), but a future arm64 node cannot run it |
| Size, publish date | 81 MB, pushed 2026-10-19 |
| True source | OCI label `org.opencontainers.image.source = https://github.com/gensecaihq/pfsense-mcp-server`, licence MIT, version 1.1.0 |
| Runtime shape | `Entrypoint: python3 -m src.main`, `Cmd: -t stdio`, `EXPOSE 3000/tcp`, `User: mcp` (uid 1000), `WorkingDir /app`, `Volumes /logs` |
| Hardening the image already does | `chmod -R a-w /app/src`, non-root user, Python 3.11.17 on Debian trixie |

The Docker Hub account (`tomvaisbort`) is not the upstream project. Treat the digest as the
identity of the artefact and `gensecaihq/pfsense-mcp-server` as the source of truth for
behaviour. If the mirror ever disappears or diverges, building from the upstream commit is the
fallback, and that is a separate supply-chain decision.

### 2.2 Runtime contract (from upstream source, not from the owner's env block)

Fetched `https://raw.githubusercontent.com/gensecaihq/pfsense-mcp-server/main/README.md`
(27010 chars) and the files below from the same repo.

| Variable | Required | Default | Meaning |
|---|---|---|---|
| `PFSENSE_URL` | **yes** | - | pfSense base URL. Must be `https://`; `http://` redirects and the client warns about it (`src/client.py:484`) |
| `AUTH_METHOD` | | `api_key` | `api_key`, `basic`, `jwt` |
| `PFSENSE_API_KEY` | for `api_key` | - | Sent as **`X-API-Key`** (`src/client.py:265`). `basic`/`jwt` send `Authorization` instead |
| `PFSENSE_VERSION` | | `CE_2_8_1` | The cluster runs 2.8.1, so the default is correct |
| `VERIFY_SSL` | | `true` | With `true` and no bundle the handshake fails (`src/client.py:86`) |
| `PFSENSE_CA_FILE` | for a private CA | - | PEM path. Setting it while `VERIFY_SSL=false` is rejected as a misconfiguration (`src/client.py:95-97`) |
| `MCP_TRANSPORT` | | `stdio` | `stdio` or `streamable-http` |
| `MCP_HOST` | | `127.0.0.1` | Bind address. Must be `0.0.0.0` in a pod |
| `MCP_PORT` | | `3000` | HTTP port |
| `MCP_API_KEY` | **yes** for HTTP | - | Bearer the *gateway* must present. The server refuses to start on empty, placeholder or short values (`src/main.py:131-143`) |
| `MCP_READ_ONLY` | | `false` | `true` exposes only the 131 read-only tools (`src/main.py:15`) |
| `MCP_ALLOWED_ORIGINS` | | - | README: gateways should add their origin here. Behaviour UNVERIFIED (`§9` U4) |

Two consequences the owner's environment block does not cover:

1. **`MCP_API_KEY` is missing.** Without it the container starts and exits. It is not an
   optional hardening knob.
2. **`VERIFY_SSL=true` needs `PFSENSE_CA_FILE`.** The pfSense certificate is self-signed
   (`§2.3`), so verification cannot succeed on the public trust store.

`/health` is unauthenticated by design ("because `/mcp` requires the bearer token"), which is
what the probes should use.

### 2.3 pfSense facts

| Fact | Value | Evidence |
|---|---|---|
| Version | 2.8.1 | `docs/network/pfsense.md:5`, re-confirmed read-only |
| REST API package | `pfSense-pkg-RESTAPI-2.11` installed | `pkg info` |
| API state | **enabled and answering** | `/api/v2/system/version/` returns a JSON error envelope (`code`, `status`, `response_id`, `message`, `data`) with HTTP 401 |
| API base path | `/api/v2/...`, trailing slash required | `/api/v2/system/version/` 401, `/api/v2/documentation/` 200; the earlier `/api/v1/...` and no-slash forms return the webGUI's HTML 404 |
| Listener | `nginx` on `*:443` (webGUI and API share it) | `sockstat -4 -l` |
| Other listeners | `node_exporter` 192.168.192.1:9100, `darkstat` 192.168.192.1:666 and 192.168.76.254:666 | same |
| TLS subject | `CN=pfsense.grigri, O=grigri` | `openssl s_client` |
| TLS issuer | internal CA, `CN=VPN`, self-signed | same |
| TLS SAN | **`DNS:pfsense.grigri` only, no IP SAN** | same |
| TLS validity | 2019-04-06 to 2029-04-03 | same |
| DMZ address | `igb1.101` = 192.168.192.1/24 (nodes and BGP peers live here) | `docs/network/pfsense.md` §1 |

The missing IP SAN is why `PFSENSE_URL` must be `https://pfsense.grigri` and not the DMZ
address: an IP-based URL cannot validate against this certificate even with the CA present.

### 2.4 Connectivity measurements (why this is blocked)

Measured 2026-11-10. Probes were `curl` only; no cluster or pfSense mutation was performed.

| Source | Destination | Result | Meaning |
|---|---|---|---|
| LAN client | `https://pfsense.grigri/api/v2/system/version/` | HTTP 401, JSON | reachable, API live, no credential |
| LAN client | same, with the stored key | HTTP 401 | **the stored key is rejected** (`§4`) |
| node `prusik`, `grigri`, `k8s-amd64-1` (host network) | `192.168.192.1:443` | `curl` exit 7 from all three | TCP refused by the firewall |
| node `prusik`, `grigri`, `k8s-amd64-1` (host network) | `192.168.192.1:9100` | `curl` exit 35 | TCP **connected**, TLS handshake failed because node_exporter speaks plain HTTP. This is the control proving the path works and the failure is port-specific |
| pod on `prusik` | `192.168.192.1:443` | exit 7 | same as the node, as expected behind masquerade |
| node `prusik`, `k8s-amd64-1` | `https://192.168.76.254/` (LAN address) | exit 7 | DMZ to LAN is blocked as well |

Cause, read-only from `pfctl -sr` on the DMZ interface:

```
pass in quick on igb1.101 inet proto tcp from <OPT1__NETWORK> to (self) port = bgp
pass in quick on igb1.101 inet proto tcp from <OPT1__NETWORK> to (self) port = 6443
pass in quick on igb1.101 inet proto tcp from <OPT1__NETWORK> to (self) port = jetdirect
pass in quick on igb1.101 inet proto tcp from <OPT1__NETWORK> to (self) port = mdqs
pass in quick on igb1.101 inet proto tcp from <OPT1__NETWORK> to (self) port = domain
block return in log quick on igb1.101 inet proto tcp from <OPT1__NETWORK> to (self)   "DMZ to pfsense DENY TCP"
block return in log quick on igb1.101 inet proto udp from <OPT1__NETWORK> to (self)   "DMZ to pfsense DENY UDP"
block return in log quick on igb1.101 inet from any to <OPT4__NETWORK>                "DMZ to LANBRIDGE"
pass in quick on igb1.101 inet all
```

The five passes come from a single rule that uses the **port alias**
`pfsense_internal_services_tcp`, whose contents are exactly `179 6443 9100 666 53`. There is
no 443, and the `quick` deny immediately after it drops everything else addressed to the
firewall itself. `<OPT1__NETWORK>` is `192.168.192.0/24`, which is where all three nodes sit.

**Operational pitfall, observed while measuring:** pfSense's GUI Lockout rule
(`block drop in quick ... from <sshguard> to (self:15) port = https`) banned a LAN client
address for 120 seconds after three rejected API credentials inside five minutes
(`/var/log/system.log`: `3 attacks in 298 secs`, then `unblocking after 143 secs`). Because pod
traffic is masqueraded to the node IP, the
same mechanism means a pod looping on a bad token would ban *the node's* address from 443 and
break the MCP for every pod on that node. Any implementation must bound retries and must be
monitored (`§3.7`, `§9` U5).

## 3. The open decision: exposing only the API

This is the owner's open question and the reason the plan stops here. The constraint that
makes it hard is not on the pfSense side, it is on ours.

### 3.1 Why a firewall rule cannot separate the API from the webGUI

The REST API v2 package serves its routes from `/usr/local/www/api/` **through the same
nginx listener and the same port as the webGUI** (`sockstat` shows one `nginx` on `*:443`;
there is no second listener for the API). Opening 443 from the cluster therefore opens the
**pfSense webGUI login** to the cluster, not just the API. No firewall rule at layer 4 can
distinguish the two, because they are the same socket.

### 3.2 Why pfSense-side path filtering is not a durable answer

- No layer-7 proxy is installed. `pkg info` lists only Avahi, Cron, RESTAPI, darkstat, frr,
  node_exporter/openvpn-client-export, and pfBlockerNG. `docs/network/pfsense.md` states
  plainly: "There is no HAProxy or other reverse proxy".
- The webGUI server config is **generated** (`/var/etc/nginx-webConfigurator.conf`). A hand
  edit that returned 403 for non-`/api/` paths on the DMZ source would be destroyed by the next
  GUI save, package change or upgrade, silently reopening the webGUI.
- Rejected alternative: moving the webGUI to an alternate port. The API lives under the
  webGUI's document root, so it moves with it. There is nothing left on 443 to allow.

Adding a proxy package to the router to get path filtering is possible but puts a new
internet-adjacent service on the one device whose compromise unmounts the whole network. That
is not a proportionate trade for one MCP backend.

### 3.3 The real constraint: pfSense sees the node, not the pod

Measured from the live Cilium configuration:

| Fact | Value |
|---|---|
| `routing-mode` | `native` |
| `ipv4-native-routing-cidr` | `10.0.0.0/8` |
| `enable-bpf-masquerade` | `true` |
| Egress gateway feature | **not enabled**: no `enable-ipv4-egress-gateway` key, and the `CiliumEgressGatewayPolicy` and `CiliumEgressIPPolicy` CRDs do not exist |
| Namespaces with any CiliumNetworkPolicy | 6 of the 86 namespaces that run pods |

Destination `192.168.192.1` is outside the native-routing CIDR, so pod traffic is masqueraded
to the node address. Two consequences follow, and they are the whole argument:

1. A pfSense rule whose source is `<OPT1__NETWORK>` or a single node address cannot
   distinguish the pfSense MCP pod from any other pod on that node.
2. Because 80 of the 86 namespaces that run pods carry no egress policy at all, a CNP on our
   own pod does **not** stop another workload from using that same rule. It only constrains
   our pod.

So "expose the API but not the webGUI" and "expose 443 to one node" are, today, the same
thing. The options below differ in how much of that they accept and what they cost.

### 3.4 Option A: allow 443 from the DMZ, accept node-IP granularity

One rule, placed above the `quick` deny, sourced from a single node address that we pin the
pod to (rather than from all of `<OPT1__NETWORK>`).

- Cost: one firewall rule and one Apply. No new components.
- Exposure: any pod on that node can reach the pfSense **webGUI** on 443. Mitigations that
  reduce but do not remove this: a read-only API token with a narrow ACL, REST API rate
  limiting and ACL if the package offers them (`§9` U1, U2), and the fact that a webGUI login
  still needs credentials.
- Failure mode to accept explicitly: a compromised pod on that node has a login form on the
  network root, and can also trigger the sshguard lockout described in `§2.4`.

### 3.5 Option B: a dedicated egress source IP (recommended for the real answer)

Give the pfSense MCP pod its own source address, then allow only that address on pfSense.

- Requires enabling Cilium egress gateway (`enable-ipv4-egress-gateway`) plus its CRDs, and
  choosing one address in the DMZ subnet that pfSense can route back. That is a cluster-wide
  Cilium change, so it is its own commit with its own blast-radius review, not part of this
  feature.
- Benefit: pfSense's rule becomes a single workload IP rather than a node, so
  "only the API caller is allowed" becomes literally true at layer 4. The webGUI stays
  unreachable from every other pod, including host-network pods on that node.
- Secondary benefit: a bad-token loop bans only that address, so the sshguard blast radius
  shrinks from a node to the pod.

### 3.6 Option C: an in-cluster API-only proxy (hardening, not separation)

A small nginx or Envoy in the admin namespace that accepts plaintext from the MCP pod and
forwards only `PFSENSE_URL/api/v2/*` to pfSense over TLS with the CA pinned.

- It cannot reduce pfSense-side exposure by itself, because the firewall rule still has to
  admit the node (or the egress IP from Option B). Do not present it as a substitute.
- It does buy three things: an enforced path allowlist at layer 7, one place to rate-limit so
  the sshguard self-DoS cannot happen, and one place to hold the CA bundle.
- Cost: a component to own, plus it terminates the TLS hop that would otherwise be end-to-end.

### 3.7 Recommendation

Recommended sequence, chosen to unblock without pretending the exposure is something it is
not:

1. **Phase 1, unblock:** Option A, scoped to **one node address** (the node the pod is pinned
   to), not to `<OPT1__NETWORK>`. Record the residual webGUI exposure in this document and in
   the chart README so it is a known, accepted risk rather than an unnoticed one.
2. **Phase 2, the actual answer:** Option B, as a separate change with its own review, then
   narrow the pfSense rule to the egress IP and delete the node-wide rule.
3. **Option C** only if the sshguard self-DoS or the path allowlist is judged worth a
   component. If it is adopted, it should land with Phase 2, not instead of it.

The owner should treat the DMZ-wide variant as the one to avoid: it turns every pod in the
cluster into a potential webGUI client for the sake of one read-only integration.

## 4. Credential model

Two independent credentials, and conflating them is the easy mistake.

| Credential | Direction | Where it lives | Rotation |
|---|---|---|---|
| pfSense REST API token (read-only) | MCP pod to pfSense | Vault, via ExternalSecret into the pod env | Owner-issued in pfSense, then written to Vault |
| `MCP_API_KEY` shared bearer | gateway to MCP pod | Generated by the External Secrets Operator `Password` generator (`CreatedOnce`), consumed by both the pod env and the gateway's `backendAuth.key` | Regenerated by deleting the Secret, which rotates both sides at once |

Status of the pfSense token: the value stored in `pass` under `grigri/pfsense-mcp-readonly`
(128 characters, `api-key:` line) is **rejected with HTTP 401** by an API that is otherwise
answering. The likely cause is that the token was rotated when the REST API settings were
changed on 2026-11-10, but that is inference, not measurement. A working token must exist
before phase 1, and it must be read-only: the plan assumes the token's own pfSense ACL limits
it, because `MCP_READ_ONLY=true` is a server-side filter, not an authorization boundary. If
the token itself can write, a bug in the filter becomes a write against the network root.

`MCP_API_KEY` is generated rather than stored because nothing else needs to know it. The
generator pattern already exists in this chart (`templates/session-key-generator.yaml`,
`refreshPolicy: CreatedOnce`) and has the useful property that `helm template` stays
deterministic: the chart renders no key material.

## 5. Where it runs

| Decision | Value | Reason |
|---|---|---|
| Namespace | `mcp-gateway-admin` | Same trust domain as the gateway; no cross-namespace Service reference |
| Chart | new `apps/pfsense-mcp/`, `app-template` 5.3.0 | Matches `apps/playwright-mcp` and the admin gateway; no maintained upstream chart exists |
| Node | pinned to one node, `prusik` by default | The admin gateway is already pinned there, and the pfSense firewall rule must name a node (Option A) |
| Service account token | `automountServiceAccountToken: false` | The pod needs the pfSense API, not the Kubernetes API |
| Filesystem | `readOnlyRootFilesystem: true`, `/tmp` emptyDir, `/logs` emptyDir | The image already treats `/app` as read-only; `/logs` is the only writable path it declares |
| User | `runAsUser: 1000` (the image's `mcp` user) | Avoids a chown failure against the read-only root |
| Probes | `GET /health` on 3000, exec-free | Unauthenticated by design; no policy exemption needed |
| Session mode in the gateway | `stateless` | Stateful caches a backend credential per session (`§6`) |

## 6. Gateway changes this route requires

The admin plane has never rendered a route, so three template gaps must be closed first. All
three are in `apps/mcp-gateway-admin/templates/configmap.yaml` and are reusable for every
later admin backend.

1. **Static backend credential.** Today `credential` supports `none` (strips
   `Authorization`) and `accessToken` (`backendAuth.passthrough`). pfSense MCP needs its own
   bearer, so add a mode that emits `backendAuth.key` with the value read from the generated
   Secret. The field shape is already known from the pinned binary: `schema/config.json` at
   tag v1.7.0 defines `BackendAuth` as `oneOf key {value, expression, location}` and
   `FileOrInline = {file} | string`; the public plane already uses the `key` form for
   context7 and firecrawl. Because the gateway replaces the credential, the route must also
   keep stripping the caller's `Authorization`.
2. **RFC 9728 discovery.** The template currently adds
   `/.well-known/oauth-protected-resource<path>` as a **second `exact` match on the
   authenticated route**, so a client that fetches metadata before it has a token gets 401
   instead of JSON. This is the U8 suspicion recorded in
   `vault-mcp-implementation.md`: real, and it must be fixed before the first admin route
   ships. The public plane's measured-good shape is a separate route with `directResponse`
   (`playwright-metadata` in `apps/mcp-gateway/values.yaml`): no authentication, literal
   JSON body, `content-type: application/json`.
3. **Kanidm client loopback redirect.** `apps/mcp-gateway-admin/values.yaml` registers only
   `https://mcp.internal.grigri.cloud/callback`, with a comment stating loopback redirects
   are "not required here". They are: OpenCode is a native client and Kanidm has no dynamic
   client registration and no client_id metadata documents
   (`docs/troubleshooting/kanidm-no-dcr-mcp-client-id.md`). Add
   `http://127.0.0.1:15000/callback`.

Plus the identity surface for this route:

| Object | Value | Note |
|---|---|---|
| Scope | `mcp_pfsense` | Exact-match CEL gate, same shape as the live public-plane routes |
| Human group | `mcp-pfsense-users` | Must render with **no `members` field**; person membership is CLI-only and a rendered `members: []` would clobber it |
| Scope map | `mcp-pfsense-users -> [mcp_pfsense]` | Deliberately **not** added to `mcp-gateway-admin-agents`; the workload group is still disabled |
| CEL rules | `!has(jwt.email)`, `!has(jwt.preferred_username)`, whole-value scope match | Copied from the live routes, not improvised |
| Header strip | `authorization`, `cookie`, plus `x-api-key` and `x-pfsense-*` for defence in depth | The backend trusts `MCP_API_KEY`; a caller must not be able to influence it |

Statefulness: the route must be `stateless` unless a probe proves otherwise. The recorded
objection to stateful mode is that it caches a backend credential per session, and this route
*does* have a backend credential, unlike Playwright. The Playwright probe
(`test/playwright-mcp/session-propagation-probe.py`) is the pattern to copy: measure that a
session id presented without a valid bearer, with a garbage token, and with another route's
scope is refused, and that refused requests never reach the backend.

## 7. Network policies

| Direction | Allow | Deny |
|---|---|---|
| Ingress to pfsense-mcp:3000 | admin gateway pods only (`app.kubernetes.io/name: agentgateway-standalone`, `instance: mcp-gateway-admin`, `component: standalone`, `io.kubernetes.pod.namespace: mcp-gateway-admin`) | everything else, including `world`, `cluster`, and the public gateway namespace |
| Egress from pfsense-mcp | pfSense on `192.168.192.1:443` only, plus DNS to the node-local-dns and kube-dns pods | all other cluster, LAN, RFC1918, and internet destinations |
| Probes | no exemption needed: `/health` is reached over the pod network by kubelet | - |

Precedent for both halves: `apps/playwright-mcp/templates/ciliumnetworkpolicy.yaml` (internet-only
egress with cluster and RFC1918 excepted, gateway-pod ingress) and
`apps/mcp-gateway-admin/templates/ciliumnetworkpolicy.yaml` (split health/app/metrics ports).
Note that the CNP constrains only this pod; it cannot stop other pods from using the pfSense
rule (`§3.3`).

## 8. Phases and acceptance

**Phase 0, resolve the blockers (owner-side, no code).**

1. Decide `§3`: Option A scoped to one node, or Option B with its Cilium change first.
2. Apply the pfSense rule above the `quick` deny, then re-run the connectivity check from that
   node only, with **one** request and a valid token.
3. Issue a read-only API token and place it in Vault.

Acceptance: from the chosen node, `curl -sk -o /dev/null -w '%{http_code}'` against
`https://pfsense.grigri/api/v2/system/version/` with `X-API-Key` returns 200, and the same
call from a *different* node still fails (proving the rule is scoped, not DMZ-wide).

**Phase 1, the workload.** Chart, CNP, ExternalSecret, generated `MCP_API_KEY`. No gateway
route yet, so the MCP is unreachable and inert.

Acceptance: pod Ready, `/health` 200 from inside the namespace, an authenticated
`/api/v2/system/version/` call made *by the pod*, egress to a cluster Service and to the
internet both timing out, and `tools/list` reachable only from inside the namespace.

**Phase 2, the route.** The three template fixes from `§6`, the Kanidm scope/group/scopeMap,
the loopback redirect, and `statefulMode: stateless`.

Acceptance: discovery URL returns 200 JSON **without** a token; `/pfsense/mcp` without a
bearer returns 401 (not 404); a token with the wrong scope is refused; `tools/list` returns
the read-only set; and no write tool is present.

**Phase 3, read-only proof.** Enumerate the tool inventory and compare it with the expected
read-only set. Attempt a write tool by name and confirm it is absent rather than merely
refused.

**Phase 4, documentation.** Chart README (access path, threat model including the residual
webGUI exposure from `§3.4`, the sshguard pitfall), a design-doc row, and a troubleshooting
entry for the 401/lockout loop.

## 9. Open questions

| # | Question | How to settle it | Why it matters |
|---|---|---|---|
| U1 | Does REST API v2 have an application-layer ACL that restricts callers by source and by endpoint? The pages exist (`system_restapi_acl.php`, `system_restapi_acl_entry_edit.php`) but are 358-382 byte shims into `/usr/local/pkg/RESTAPI/Forms/`, and the config is not in `config.xml` | Read the forms in the pfSense UI | Would add layer-7 defence in depth on top of the firewall rule |
| U2 | Can the API be bound to a port other than the webGUI's? | Same UI, settings page | If yes, `§3` collapses to a single-port rule that excludes the webGUI |
| U3 | Does the gateway-to-MCP bearer work with `backendAuth.key`, including `Authorization: Bearer` vs a custom header? | Local probe against the pinned binary plus the container | Determines the `§6` item 1 implementation |
| U4 | Does the MCP require `MCP_ALLOWED_ORIGINS` to include the gateway origin, and does the gateway forward an `Origin` header the backend rejects? | Probe with and without the variable | Would be a silent 403 on every call |
| U5 | How does the MCP retry on a 401 from pfSense? | Read `src/client.py` retry logic, then test with a deliberately bad token against a stub | Governs the sshguard risk in `§2.4` |
| U6 | Is `MCP_READ_ONLY` enforced per transport for `streamable-http`, not just stdio? | Source read plus tool listing over HTTP | The whole read-only claim rests on it |

## 10. Risks

| Risk | Severity | Mitigation |
|---|---|---|
| WebGUI exposed to the cluster (Option A) | High | Scope the rule to one node, pursue Option B, record the acceptance in the README |
| sshguard self-DoS: a bad or rotated token bans the source and breaks the route | Medium | Bound retries, monitor 401s and `sshguard` events, prefer Option B so the banned address is the pod's own |
| First admin route: discovery, credential injection and isolation are all untested on this plane | Medium | Probe locally before deploying; the public plane's measured patterns are the reference |
| Supply chain: personal Docker Hub mirror for a router-management tool | Medium | Pin the digest, cite the upstream source in the chart, re-check the digest on any bump |
| Read-only filter is server-side, not an authorization boundary | Medium | The pfSense token's own ACL must be read-only as well; verify by attempting a write with the token directly |
| amd64-only image | Low | All current nodes are amd64; a future arm64 node cannot schedule it |

## 11. Explicitly not in scope

- Any pfSense configuration change by an agent. The firewall rule, the API token and any ACL
  are owner actions. This repository does not manage pfSense, and
  `docs/network/pfsense.md` is documentation, not source of truth.
- Write tools, even if `MCP_READ_ONLY` were later relaxed. A write path to the network root
  needs its own design, its own phases and an explicit owner decision.
- The Vault MCP (`vault-mcp-implementation.md`, deferred) and the public plane. This plan adds
  nothing to `mcp.grigri.cloud`.
- Rotating or reissuing the pfSense certificate. If the CA were replaced by a publicly trusted
  certificate, `PFSENSE_CA_FILE` would become unnecessary, but that is a separate change with
  its own risk.
