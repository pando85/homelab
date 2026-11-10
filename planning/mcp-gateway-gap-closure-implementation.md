# MCP Gateway Gap Closure Implementation Plan

**Status:** Phases 1-4 executed (2026-11-10). G1, G2, G4, G6, G10-G14 done; G3 deferred; G5 closed by
decision; G7, G15 partial. G4 is done for the three gateway routes: the owner converted the bypassing
OpenCode servers to remote gateway entries. Per-route human authorisation landed in `fa96d899a`. See
the Execution log and its addendum in section 4.
**Anchor revision:** revision 2, measured against `origin/master` at `2a3b8dbb0` (2026-11-10).
Findings are timestamped; the cluster moves — re-run §5 before trusting any line here.
**What changed in revision 2:** the Envoy Gateway migration **ended**. Envoy is now the sole serving
data plane for `mcp.grigri.cloud` and `mcp.internal.grigri.cloud`; ingress-nginx still runs but
carries no MCP traffic and is the documented rollback path until ingress-migration Phase 6. That
inverts two things revision 1 said: the nginx CNP rules are now **load-bearing for rollback** rather
than debris (G15), and a set of committed tests and Jobs now assert a **dead** Ingress (G14).
**Predecessor:** [`mcp-gateway-implementation.md`](mcp-gateway-implementation.md) — build-out history,
measured probe/policy matrices, and the E2E acceptance record. Do not duplicate that evidence here.
**Architecture:** [`docs/design/mcp-gateway-architecture.md`](../docs/design/mcp-gateway-architecture.md)
(needs the corrections listed in Phase 1 — its status block still describes a pre-isolation tree).

## 1. Overview

The gateway MVP is **live, network-isolated, and serving three real routes** to two real clients
(OpenCode and Hermes). Measured against the design doc's own MVP definition
(`planning/mcp-gateway-implementation.md`, "Success Criteria"), **6 of 7 criteria are met** (see the
scorecard in §1.1; only the deferred Vault admin plane, criterion 4, is open).

This plan records the remaining gaps as a prioritised register, separates what an agent can do
unaided from what needs an owner decision, and gives re-verification commands so the next session
starts from measurement rather than from this document's claims.

The dominant theme is **consolidation, not construction**: the gateway currently exists *alongside*
the legacy paths it was meant to replace. As of `fa96d899a` the OpenCode bypasses are closed:
`context7`, `grafana-grigri` and `firecrawl` are remote gateway entries, not local servers. What is
left local is deliberate: `grafana-forkline` targets a different, external Grafana and is out of this
gateway's scope, `searxng` has no gateway route yet, `playwright` is out of scope by design.

A second theme, new in revision 2, is **handoff debt**: the ingress layer under this gateway changed
underneath it. The gateway itself needed no change — its CNPs already allowed the Envoy proxies — but
the tests, Jobs and comments that describe "how traffic arrives" were written for nginx and did not
move with it. That class of debt is silent: nothing fails, the assertions just stop meaning anything.

**Update 2026-11-10:** Phases 1-4 are substantially complete. Hermes is fully migrated (no sidecar,
three gateway servers). Firecrawl route is live. Placement is pinned to `prusik`. Verification assets
were repaired and re-run. OpenCode bypass retirement is done for the three routes (owner action, see
G4 and section 10), and each of those routes is now gated by its own human Kanidm group (`fa96d899a`).
Remaining work: deferred Vault admin plane decision, and ingress-migration Phase 6 coupling (G15).

### 1.1 Success Criteria scorecard (measured 2026-11-10)

| # | Criterion (verbatim from the predecessor plan) | State | Evidence |
|---|---|---|---|
| 1 | agentgateway deployed (public + admin) | **MET** | both pods `1/1 Running`; admin restarts 17 (pre-existing baseline, no new), public 0 |
| 2 | Grafana MCP standalone (**sidecar removed**) | **MET** | sidecar removed from `hermes-0` (commit 8b1b7007a); pod now `1/1` |
| 3 | Firecrawl MCP deployed | **MET** | route `/firecrawl/mcp` live, scope-gated, stateless; acceptance passed — see G2 |
| 4 | Vault MCP deployed (admin-only, scope decided) | **NOT MET / DEFERRED** | admin plane stays fail-closed (`routes: []`); owner decision 2026-11-10 — see G3 |
| 5 | OpenCode/Hermes migrated (≥1 instance each) | **MET (both)** | Hermes: 3 gateway servers, no bypass. OpenCode: all three routes converted to remote gateway entries, see G4 |
| 6 | All tests pass, rollback tested | **MET (tests), CLOSED (rollback)** | suites green, Jobs re-run and repaired (G13); rollback closed by decision (G5) — `git revert` + ArgoCD is the path |
| 7 | Owner approval recorded for each gate | **MET** | all gates decided: 1-3, 6, 7 approved; 4 deferred; 5 closed; G4 executed by the owner |

### 1.2 What is already correct (do not "fix" these)

Verified live, not inferred from the charts:

- Two planes on the official `agentgateway-standalone` v1.7.0 chart, each with its own Kanidm client
  (`mcp-gateway`, `mcp-gateway-admin` — both present as `kanidmoauth2clients.kaniop.rs`).
- Edge auth is strict and *narrower than the design required*: routes additionally enforce
  `require: !has(jwt.email)` and `require: !has(jwt.preferred_username)`, so an ID token is rejected
  on claim content even though v1.7.0 cannot validate `typ`.
- Per-MCP scope gates via CEL on the space-delimited `scope` string; `statefulMode: stateless` on
  both live routes (this is what makes the session-replay concern moot for them).
- Caller-preserving delegation for Grafana (`backendAuth.passthrough`, no ambient credential in the
  backend pod). Two distinct callers were observed reaching Grafana with **different native org
  roles**, neither server-admin. Which identity holds which role is deliberately not recorded here
  (repo rule: no account→privilege mapping in git).
- No public-plane route to Vault: `grep -c vault apps/mcp-gateway/values.yaml` → `0`.
- No direct backend exposure: every service in the MCP namespaces is `ClusterIP`; no Ingress or
  HTTPRoute exists for any backend.
- `firecrawl-mcp` is **already** correctly isolated before any route exists: native NetworkPolicies,
  `exec` probes (policy-immune), ingress limited to the gateway pods, egress limited to DNS plus
  `firecrawl-api:3002`, and no API key by design. TODO-4.1 adds a route to it; it must not touch the
  chart.
- Network isolation: three CiliumNetworkPolicies (`mcp-gateway`, `mcp-gateway-admin`, `grafana-mcp`),
  all `Valid=True`, in the measured split-port shape. Both permitted and denied paths were measured
  per plane at enablement. The gateway CNPs allow **both** ingress data planes (ingress-nginx and
  Envoy proxies), which is why the Envoy migration completed without touching the gateway at all:
  measured `src.addr` in the gateway access log is `10.0.4.6`/`10.0.4.44` (external Envoy proxies),
  and no MCP route request arrives from an nginx pod.
- Audit is **better than the design specified**: live gateway log lines carry `jwt.sub`,
  `mcp.target`, `mcp.method.name`, `gen_ai.tool.name`, `http.status`, `trace.id`/`span.id`, with no
  payload. All four MCP namespaces do reach Loki (see the correction in §6.2).
- Metrics scraped on both planes: `up{namespace=~"mcp-gateway|mcp-gateway-admin"} == 1`.

## 2. Gap Register

Severity: **High** = defeats a stated architecture goal or blocks MVP completion; **Medium** =
residual risk or missing control; **Low** = hygiene/documentation.

| ID | Gap | Sev | Blocked on | Phase |
|----|-----|-----|-----------|-------|
| G1 | **DONE** — Hermes legacy `grafana-mcp` sidecar removed; `hermes-0` now `1/1` with initContainer `fix-sa-token` (kept for SA token prep) and container `hermes` only. `grafana-mcp`, `grafana-mcp-refresher`, `grafana-mcp-token-init` resources, `grafana-mcp-shared` volume, and `localhost:8080/sse` server entry removed. Commit 8b1b7007a. | High | — | 3 |
| G2 | **DONE** — Firecrawl route `/firecrawl/mcp` plus RFC 9728 discovery live. Scope-gated on `mcp_firecrawl` with exact-match CEL, `statefulMode: stateless`, no backend credential, Authorization and Cookie stripped. Authorized for `mcp-gateway-agents` group (no Kanidm change). Commits 8fa3885ff (route) and b351fb11a (Hermes client). Validated: acceptance printed ACCEPTANCE_PASS with servers: 3, real `firecrawl_scrape` returned 610 bytes, 403 for all wrong-scope combinations. Follow-up: humans are gated by `mcp-firecrawl-users` (`fa96d899a`), see the Execution log addendum. | High | — | 4 |
| G3 | **DEFERRED by owner decision 2026-11-10** — Vault MCP not deployed; admin plane stays fail-closed (`routes: []`). Open question: should the admin plane exist at all while serving nothing? It consumes a pod, CNP, Ingress, HTTPRoute and scrape target. | High | owner decision deferred | 4 |
| G4 | **DONE (owner action, measured 2026-11-10)** — OpenCode bypass servers retired for the three gateway routes. `~/.config/opencode/opencode.json` now holds remote entries: `context7` `https://mcp.grigri.cloud/context7/mcp` with `oauth.scope: mcp_context7`, `grafana-grigri` `https://mcp.grigri.cloud/grafana/mcp` with `mcp_grafana`, `firecrawl` `https://mcp.grigri.cloud/firecrawl/mcp` with `mcp_firecrawl`; all three `type: remote`, `callback_port: 15000`. The local `context7` and `firecrawl` servers no longer exist in the config. Still local by decision: `grafana-forkline` (external Grafana), `searxng` (no route), `playwright` (out of scope). See section 10. | High | — | 3 |
| G5 | **CLOSED BY OWNER DECISION 2026-11-10** — No separate rollback exercise. Rationale: for validated routes, `git revert` + ArgoCD reconcile restores previous state; Hermes sidecar was the rollback path until G1 removed it; only artefact retained is that retired OpenCode server blocks must be saved before deletion. Ingress-layer rollback belongs to ingress migration (tracked as G15). | Medium | — | 5 |
| G6 | **DONE** — Owner decision: pin everything MCP to `prusik`. `grafana-mcp` gained `kubernetes.io/hostname: prusik`; `firecrawl-mcp` gained same pin while keeping `kubernetes.io/arch: amd64`; gateway planes and Hermes already pinned. Verified `prusik` is amd64 with capacity. Commit 8fa3885ff. | Medium | — | 5 |
| G7 | **PARTIAL / RE-SCOPED** — ServiceMonitor for grafana-mcp enabled but produced `up == 0` (image has no HTTP client), so reverted along with CNP rule widening app port to Prometheus. firecrawl-mcp confirmed no `/metrics` endpoint (404). Net position: neither backend scraped, deliberately; gateway-side `agentgateway_requests_total` carries route and status labels for visibility. | Medium | — | 2 |
| G8 | `firecrawl` **backend** namespace has no pod-level policy (only `firecrawl-mcp` is isolated) | Medium | needs its own measurement; prior attempt regressed | 4 |
| G9 | ~~Envoy switchover dependency~~ **CLOSED, and now final** — the migration ended. Envoy external (`192.168.193.14`) serves the public plane, Envoy internal (`192.168.193.13`) the admin plane; `src.addr` confirms no MCP route request comes from nginx. Nothing to do | — | none | — |
| G10 | **DONE** — Documentation corrected; canary planning docs carry superseded banners. | Low | — | 1 |
| G11 | **DONE** — Untracked `apps/mcp-canary/` debris deleted, orphan `mcp-canary` namespace deleted with owner approval. | Low | — | 1 |
| G12 | **DONE, OWNER DECISION** — Full `jwt.sub` kept in Loki (audit trails wanted); design doc wording corrected. Alerting added: PrometheusRule with MCPGatewayHighErrorRate, MCPGatewayCriticalErrorRate, MCPGatewayElevated403Rate, MCPGatewayTargetDown. Metric names verified against live series: `agentgateway_requests_total` with labels namespace, route, status, method, protocol, reason, pod, job. Commit fe5837fe0. | Low | — | 2 |
| G13 | **DONE** — Committed Jobs repaired and run in-cluster for first time since isolation. Results: NETPOL_VERIFY_PASS 3/3 (cross-namespace access times out), FOUNDATION_VERIFY_PASS 4/4 for prometheus-labelled pod, 3/3 for unlabelled. Jobs deleted after. Three defects fixed: curl digest did not resolve (replaced with verified 64-char manifest), blocked-detection parsed `000000` (curl emits `000`, `|| echo 000` appended another), assertion expected any monitoring pod to scrape 15020 while policy admits only prometheus-labelled pods. Commits 30abdcd74, 18154236f. | Medium | — | 1, 2 |
| G14 | **DONE** — `foundation-checks.py` and `route-checks.py` now assert HTTPRoute serving path alongside retained nginx Ingress rollback path; `chart-checks.py` gained drift guard asserting rendered Envoy proxy selector matches live labels and that nginx rules are present. Stale nginx-era comments corrected in gateway CNP templates and `apps/grafana-mcp/values.yaml`. Suite counts: route-checks 108/0, chart-checks 187/0, foundation-checks 60/0, local-boot-checks 79/0. Commit d760a5f81. | Medium | — | 1, 2 |
| G15 | **PARTIAL** — CNP templates now carry comments stating nginx rules are retained rollback path, removable only in ingress-migration Phase 6 (commit 3c99e3f5c). Still outstanding: reciprocal note in `docs/network/ingress.md`, extending `scripts/gateway-check.sh` to MCP hosts (skipped as another session was editing that script). | Medium | ingress-migration Phase 6 | 2 |

## 3. Context & Current State (measured)

### 3.1 Deployed topology

```
ns mcp-gateway         mcp-gateway-standalone   1/1  prusik  ClusterIP 3000/15020/15021
                       routes LIVE: /context7/mcp, /grafana/mcp, /firecrawl/mcp
                       (+ 3 RFC 9728 metadata routes), all statefulMode: stateless
                       mcp.grigri.cloud -> HTTPRoute -> Gateway external (192.168.193.14)
                       the nginx-external Ingress object still exists, idle (rollback path)
ns mcp-gateway-admin   mcp-gateway-admin        1/1  prusik  ClusterIP 3000/15020/15021
                       routes: []  (fail closed; Vault disabled)
                       mcp.internal.grigri.cloud -> Gateway internal (192.168.193.13)
ns grafana-mcp         grafana-mcp              1/1  prusik  ClusterIP 8000, healthz 8081
                       ingress: gateway pods only (correct: its peer is the gateway, not the ingress)
ns firecrawl-mcp       firecrawl-mcp            1/1  prusik  ClusterIP 3000 (exec probes)
                       ingress: gateway pods only, by native NetworkPolicy, egress: DNS + firecrawl-api:3002
ns hermes              hermes-0                 container: hermes (sidecar removed, G1/8b1b7007a)
                       hermes-mcp-gateway       token-refresher deployment (Kanidm token exchange)
ns mcp-canary          DELETED (G11), verified absent 2026-11-10
```

Ingress layer, measured 2026-11-10 after the migration ended: Envoy `external` = `192.168.193.14`,
`internal` = `192.168.193.13`; ingress-nginx still runs (`192.168.193.1` / `.4`) and still reconciles
the old Ingress objects, but serves no MCP traffic. Both data planes are in the CNP allow-lists —
deliberately, because nginx is the rollback path (G15).

ArgoCD: `mcp-gateway`, `mcp-gateway-admin`, `grafana-mcp`, `firecrawl-mcp` all Synced/Healthy.

### 3.2 Client inventory (the consolidation gap in one table)

OpenCode (`~/.config/opencode/opencode.json`, `mcp` + `mcp.servers`):

| Server | Transport | Through gateway? |
|---|---|---|
| `homelab-context7` | remote, OAuth PKCE | **yes** |
| `homelab-grafana` | remote, OAuth PKCE | **yes** |
| `context7` | remote `https://mcp.grigri.cloud/context7/mcp`, OAuth scope `mcp_context7` | **yes** (was a local duplicate, converted 2026-11-10) |
| `grafana-grigri` | remote `https://mcp.grigri.cloud/grafana/mcp`, OAuth scope `mcp_grafana` | **yes** (was a local `mcp-grafana` with local credentials against the same instance, converted 2026-11-10) |
| `grafana-forkline` | local `mcp-grafana`, `GRAFANA_URL=https://grafana.nbg1-c01.forkline.dev` | no, deliberately: a **different, external** Grafana, out of this gateway's scope |
| `firecrawl` | remote `https://mcp.grigri.cloud/firecrawl/mcp`, OAuth scope `mcp_firecrawl` | **yes** (was a local `firecrawl-mcp`, converted 2026-11-10) |
| `searxng` | local `npx mcp-searxng` → `searxng.internal.grigri.cloud` | no gateway route yet |
| `playwright` | local `npx @playwright/mcp@latest` | no, out of scope by design |

Hermes (`hermes-0`, via `hermes_cli.mcp_config._get_mcp_servers()`):

| Server | Endpoint | Auth |
|---|---|---|
| `homelab-context7` | `https://mcp.grigri.cloud/context7/mcp` | oauth |
| `homelab-grafana` | `https://mcp.grigri.cloud/grafana/mcp` | oauth |
| `homelab-firecrawl` | `https://mcp.grigri.cloud/firecrawl/mcp` | oauth |
| `comunio` | local node command | none (unrelated workload) |

Measured 2026-11-10 after G1: the sidecar entry `grafana` → `http://localhost:8080/sse` (no auth, shared
SA token via `/shared/grafana-jwt`) is gone, so `homelab-grafana` is the only Grafana path from Hermes.

Consequence (then, and the reason for G1): an agent that picked `grafana` instead of `homelab-grafana`
got a **shared** backend identity and produced **no** gateway audit record. That is the exact failure
mode the architecture's "No silent fallback" and "single entry point" goals exist to prevent.

### 3.3 Why the admin plane is idle

`apps/mcp-gateway-admin/values.yaml`: `backends.vault.enabled: false`, `oauth2.scopeMap: []`,
`oauth2Group.enabled: false`. Its only traffic is health-check 404s on `/`
(389 log lines in 24 h, `error: route not found`). This is *correct fail-closed behaviour*, not a
fault — but the plane consumes a pod, a CNP, an Ingress, an HTTPRoute and a scrape target while
delivering nothing, so G3 and G6 should be decided together.

## 4. Implementation Plan

### Phase 1 — No live behaviour change (agent may do unaided)

- **TODO-1.1** Correct `docs/design/mcp-gateway-architecture.md` (grep anchors given; line numbers
  move):
  - `grep -n 'parity\* through the gateway is still not demonstrated'` (Status block) → parity **is**
    demonstrated; link the acceptance record in the predecessor plan. The same sentence claims
    "Policy re-land is unblocked in principle … tracked in `planning/…`" — re-land happened.
  - `grep -n 'The current tree renders no policy'` (NetworkPolicy section) → three CNPs are live and
    `Valid=True`; the adjacent "Pending source fix (not a doc change)" bullet is done — the legacy
    comment is gone and the CNPs carry the measured rationale.
  - Inventory table: Context7 is no longer "Next / evaluate centralized deployment" — it is a
    deployed route with a Vault-delivered upstream key. Firecrawl is still genuinely pending (G2).
- **TODO-1.2** Correct `planning/mcp-gateway-implementation.md` (locate by section/grep, not line
  number — they shift):
  - Overview (`grep -n 'stays commented out'`): "the **grafana** route stays commented out until
    Grafana's `auth.jwt` trusts the `mcp-gateway` client" → the route is rendered, live, and serving
    caller-preserving delegation; the Grafana-side trust question was settled by the subject-only
    identity mapping in `system/monitoring/values.yaml`.
  - Task 2 status (`grep -n 'candidate implemented in the admin chart'`): the candidate is live on
    the **public** plane and serving, not disabled in the admin chart.
  - Task 5 header (`grep -n '^NOT STARTED'`, first hit) → both clients are migrated; reference G4 for
    what remains. The second `NOT STARTED` is Rollback and is accurate — leave it, cross-link G5.
  - "Still open" list: the two-human parity item is satisfied in substance by the measured differing
    native roles, and in tooling by `identity-ab/` (TODO-2.5).
- **TODO-1.3** Correct `apps/mcp-gateway/values.yaml` (`grep -n 'inert now'`): the comment claims
  `mcp_grafana` is "inert now: the grafana MCP route is not rendered … gets 404". False — the route is
  rendered and serving. Keep the explanation of why the scope must be reconciled in Kanidm before use.
- **TODO-1.4** Canary cleanup (G11): delete the orphan `mcp-canary` namespace, remove untracked
  `apps/mcp-canary/` debris, and either delete or mark superseded:
  `planning/mcp-canary-handoff.md`, `planning/mcp-canary-fixed-summary.md`
  (their diagnostic question was settled by matrix rows S1–S6).
  Namespace deletion is a cluster mutation → **needs owner approval**; the rest does not.
- **TODO-1.5** Add a `planning/README.md`-conformant cross-link from
  `mcp-gateway-implementation.md` to this file so the two do not diverge again. *(done — see the
  "Continuation" block at the top of that file.)*
- **TODO-1.6** (G13) Correct the "uncommitted" claims in `planning/mcp-gateway-implementation.md`.
  Measured: `git ls-files test/mcp-gateway/` tracks **every** suite the plan calls local-only —
  `e2e-authz.py`, `local-render.sh`, `session-isolation-probe.py`, `session-binding-probe.py`,
  `grafana-jwt-mapping-probe.py`, `grafana-forwarding-probe.py`, `grafana-merged-keyset-probe.py`,
  `grafana-sub-alias-probe.py`, `vault-official-stateless-probe.py` — plus two assets the plan does
  not mention at all: `identity-ab/` (the two-identity A/B harness) and `jobs/` (committed in-cluster
  verification Jobs). Find the stale lines with
  `grep -n 'uncommitted\|not committed\|not in the committed tree' planning/mcp-gateway-implementation.md`
  (13 hits as of `449a35331`, in Task 1/2/4 and the Tests table — cite the grep, not line numbers,
  because they move).
  Consequence worth stating in that file: its "Still open" item *"Direct-to-backend access denied
  from an ordinary pod … must be verified with a committed, approved test method, never ad-hoc
  `kubectl run/apply`"* already has such a method (`jobs/verify-networkpolicy.yaml`), and its
  *"Two-human parity run … no live AB run has been done"* already has a harness
  (`identity-ab/`, whose LIVE tier skips loudly without prerequisites rather than passing vacuously).
- **TODO-1.7** (G14) **Repair the ingress-era assertions and comments.** Both objects exist, so these
  assertions pass while saying nothing about the serving path — the most expensive kind of green.
  Rule for every one of them: assert **both** objects and label them (`HTTPRoute` = serving path,
  `Ingress` = rollback path), so neither can silently become the only one.
  - `test/mcp-gateway/foundation-checks.py` (`grep -n 'nginx-external'`, ~156) and
    `test/mcp-gateway/route-checks.py` (`grep -n 'ingress unchanged'`, ~316) — extend to the HTTPRoute
    (parentRef gateway `external`, hostname, backend port) rather than replacing the Ingress check.
  - `apps/grafana-mcp/values.yaml` (`grep -n 'nginx-external Ingress carries no auth'`),
    `apps/mcp-gateway/templates/ciliumnetworkpolicy.yaml` and
    `apps/mcp-gateway-admin/templates/ciliumnetworkpolicy.yaml` (`grep -n 'ingress-nginx-external'`) —
    comments only. Say "envoy proxies serve; nginx rules retained as the rollback path (G15)".
  - Do **not** touch `scripts/gateway-check.sh`'s `--compare` help text or `scripts/eg-host-diff.sh`:
    both deliberately reference nginx as a comparison target and are still correct.

### Phase 2 — Observability and handoff safety (no auth change)

- **TODO-2.1** (G7) Decide and implement scraping for `grafana-mcp` and `firecrawl-mcp`.
  `grafana-mcp` sets `serviceMonitor.enabled: false`; either enable it or document why the backend
  is intentionally unscraped. If enabled, the CNP needs a metrics rule from Prometheus pods —
  follow the shape already used on the gateway planes, and re-run the probe-safety checks.
- **TODO-2.2** (G9 — closed; what remains is a drift guard) The migration ended and Envoy serves
  both planes. Measured 2026-11-10:

  | CNP | app-port `fromEndpoints` | what actually forwards |
  |---|---|---|
  | `mcp-gateway` (public) | `ingress-nginx`/`controller` in `ingress-nginx-external` **and** `envoy`/`proxy` + `owning-gateway-name: external` in `envoy-gateway` | Envoy external only (`src.addr` `10.0.4.6`, `10.0.4.44`) |
  | `mcp-gateway-admin` | `ingress-nginx` in `ingress-nginx` **and** `envoy`/`proxy` + `owning-gateway-name: internal` | Envoy internal only |
  | `grafana-mcp` | gateway pods (`agentgateway-standalone` / `instance=mcp-gateway` / `component=standalone`) on 8000, plus `world` on 8081 for kubelet probes | n/a — the backend's peer is the gateway, not the ingress. The 8081 `world` rule is the measured probe path (§6.1), **not** a mistake to "fix" |

  Remaining work, deliberately small:
  1. **Do not remove the `ingress-nginx` rules.** They are the allow-list for the documented rollback
     path (G15). They become removable only in ingress-migration Phase 6, in the same change that
     deletes ingress-nginx — never earlier, and never as gateway-side "tidying".
  2. Add the drift guard: a chart check asserting the rendered Envoy selector matches the live proxy
     pod labels (`app.kubernetes.io/component: proxy`, `app.kubernetes.io/name: envoy`,
     `gateway.envoyproxy.io/owning-gateway-name`). If those labels change, the routes fail closed with
     no error anywhere — the same class of bug as §6.4.
  3. Explain residual nginx traffic: 404 `GET /` from `10.0.4.148`/`10.0.4.234` (nginx-external pods)
     still appears in the gateway log. Before Phase 6, establish whether that is a leftover public-DNS
     record pointing at `192.168.193.4` or nginx's own probe behaviour. See TODO-2.7.
- **TODO-2.3** (G12) Decide the audit retention posture: full `jwt.sub` in Loki for 180 days vs
  hashed/truncated. The acceptance tooling already hashes subjects, so a consistent choice is cheap.
  Add a dashboard or at least one alert on gateway `http.status >= 500` and on `403` rate.
- **TODO-2.4** (G13) Update and re-run the committed verification Jobs against the **live CNP** shape:
  - `jobs/verify-networkpolicy.yaml` header still says the policy under test is
    `apps/mcp-gateway/templates/networkpolicy.yaml` with "ingress 3000 ← ingress-nginx-external only,
    ingress 15020 ← monitoring only". That template was replaced by `ciliumnetworkpolicy.yaml`, which
    additionally allows `world` on 15021. Fix the header, and re-derive the expectations from the
    rendered CNP rather than from the comment.
  - Its three assertions (cross-namespace → 3000 / 15020 / 15021 must all time out as `000`) are
    **expected to still hold**, because S6 Step 4 measured that `world` does not admit
    cluster-identity pods. That is an expectation from a related measurement, **not** a re-run —
    the Job has not been applied since the CNP landed. Apply it (owner approval: it is a cluster
    mutation, and `test/` is outside ArgoCD's watch), record the output, then delete it.
  - `jobs/verify-foundation-incluster.yaml`: same treatment — check its assumptions against the
    current routes (two live MCP routes + two RFC 9728 metadata routes, not `routes: []`).
- **TODO-2.5** Run the `identity-ab/` LIVE tier once its prerequisites are supplied (two caller
  credentials, `MCP_IDENTITY_TARGET`, and issuer/audience if not inferred). This is the committed
  path to closing the predecessor plan's two-identity parity item, and it is read-only by design.
  Owner approval needed for the credential provisioning only.
- **TODO-2.6** (G15) **Write the CNP↔rollback coupling down in both places it can be acted on.**
  1. `docs/network/ingress.md`, rollback section: state that a pfSense/DNS rollback to nginx is only
     effective for hosts whose CiliumNetworkPolicy still lists the nginx controllers, and name
     `apps/mcp-gateway/templates/ciliumnetworkpolicy.yaml` +
     `apps/mcp-gateway-admin/templates/ciliumnetworkpolicy.yaml` as examples that do. Add this to the
     Phase 6 removal checklist as a gate: "remove the nginx `fromEndpoints` rules only in the change
     that deletes ingress-nginx".
  2. Both CNP templates: a comment on the nginx rule reading *retained as the documented ingress
     rollback path; removable only in ingress-migration Phase 6* — so a reader who sees dead weight
     cannot conclude it is safe to delete.
  3. `docs/conventions/gateway-api-routes.md` already documents the Hermes `hostAliases` pin to
     `192.168.193.14`; confirm it still matches `apps/hermes/values.yaml` and that it survives the
     Phase 6 DNS cleanup (an upsert-only DNS record pitfall is recorded at `2a3b8dbb0` — check the
     same trap does not apply to this override).
  4. This is docs and comments only: **no CNP selector change**, so no risk to the live allow-list.
- **TODO-2.7** (G15, residual traffic) Determine whether anything still reaches these hosts through
  nginx. Read-only, in this order:
  1. `bash scripts/gateway-check.sh` — it already checks Envoy proxies, gateways, routes, pfSense and
     DNS for 12 items, but **not** the MCP hosts; note which of its per-host checks would cover them.
  2. Check public DNS for `mcp.grigri.cloud` and `grafana.grigri.cloud` against `192.168.193.4`, and
     the pfSense alias contents, for a stale record still resolving to nginx.
  3. Only if a stale record exists, propose a DNS fix through the ingress migration's own docs — not
     as part of this plan. Record the answer either way; "no residual traffic" is itself a finding that
     unblocks Phase 6's CNP rule removal.
- **TODO-2.8** (G14) Extend `scripts/gateway-check.sh` to cover `mcp.grigri.cloud` and
  `mcp.internal.grigri.cloud` if its per-host probe list is data-driven; otherwise add the two hosts
  where the list lives. The gateway's healthz on 15021 is `world`-reachable, so a cheap HTTP probe is
  enough to prove "route accepted and forwarding" without touching auth.

### Phase 3 — Consolidation (the actual MVP completion)

- **TODO-3.1** (G1) **Remove the Hermes Grafana sidecar.** Owner approval gate 3 is now satisfiable:
  parity is measured and Hermes already lists `homelab-grafana` through the gateway. Measured
  inventory of what the sidecar consists of (`apps/hermes/values.yaml`, grep each name):

  | Resource | Role | Disposition |
  |---|---|---|
  | container `grafana-mcp` | in-pod `mcp-grafana:2.2.0`, reads JWT from `/shared/grafana-jwt` | remove |
  | container `grafana-mcp-refresher` | re-exchanges the JWT every 600 s | remove |
  | initContainer `grafana-mcp-token-init` | first exchange at pod start | remove |
  | volume `grafana-mcp-shared` (emptyDir, 1 Mi) | `/shared` shared by all three | remove **only after** the step below |
  | initContainer `fix-sa-token` | copies the SA token into `/shared` | **verify first**: keep if the main `hermes` container mounts `/shared`; remove only if the mount list proves it is sidecar-only |
  | MCP server entry `grafana` → `http://localhost:8080/sse`, `auth=None` | the bypassing server itself | remove from the Hermes MCP config |
  | deployment `hermes-mcp-gateway` | Kanidm token exchange **for the gateway** OAuth | **keep** — unrelated to the sidecar |

  Steps: confirm `global.mcpGateway.enabled: true` and that the gateway server entry is the only
  Grafana path; check whether `identity.subjectTokenSecret: grafana-mcp-token` is consumed by
  anything other than the sidecar before deleting the SA; render and diff
  (`helm template --include-crds --namespace hermes hermes apps/hermes/`) so the removal is reviewed
  as a diff, not as prose.
  Acceptance: `test/mcp-gateway/hermes-live-acceptance.py` still prints `ACCEPTANCE_PASS` with
  `servers: 2`, `hermes-0` goes `3/3 → 1/1`, and Loki shows a new Grafana call arriving at the gateway
  from Hermes with a `jwt.sub`.
  Rollback: `git revert`; the sidecar returns with the pod. No CNP change is involved — the gateway
  CNPs are unaffected by this removal.
- **TODO-3.2** (G4) **Retire bypassing OpenCode servers, one at a time, owner-approved per server.**
  **DONE (owner action, measured 2026-11-10)** for items 1, 2 and the `firecrawl` part of item 4: each
  local server was **replaced in place** by a remote gateway entry of the same name rather than deleted,
  so the agent-facing server names did not change. The local `context7` and `firecrawl` servers no longer
  exist in the config. Items 3 and 5 stay local by decision; `searxng` still has no route. The order and
  rationale below are kept as history because they are the reason the three routes were safe to convert.
  Recommended order and rationale (the two Grafana entries are *not* equivalent, measured from
  `GRAFANA_URL` in `~/.config/opencode/opencode.json`):
  1. `context7` (local) — pure duplicate of a working gateway route; zero capability loss.
  2. `grafana-grigri` — targets `https://grafana.grigri.cloud`, **the same instance** the gateway's
     `grafana-mcp` backend fronts, but authenticates with a local credential instead of the caller's
     own bearer. This is the highest-value retirement: it removes an unaudited path to the same data.
     Verify first that `homelab-grafana` exposes the tool set this server was started with
     (`-enabled-tools=datasource,prometheus,loki,alerting,…`) — the gateway backend currently offers
     63 tools, so this is likely a superset, but confirm rather than assume.
  3. `grafana-forkline` — targets `https://grafana.nbg1-c01.forkline.dev`, an **external** Grafana.
     Do **not** repoint it at the homelab backend; that silently changes which instance it queries.
     Either leave it local (documented exception) or give the gateway its own route + backend pod for
     that instance, which is new scope, not consolidation.
  4. `firecrawl`, `searxng` — blocked on G2 and on SearXNG onboarding (design lists it "Next").
  5. `playwright` — out of scope, leave local.
- **TODO-3.3** After each retirement, re-run the acceptance script and confirm the retired server's
  capability is still reachable (see §5).

### Phase 4 — Backend onboarding (each needs an owner decision first)

- **TODO-4.1** (G2) Firecrawl **route** — revision 1 wrongly coupled this to the fetcher egress
  regression. They are separable, and the measurements say so:

  | Measured | Consequence |
  |---|---|
  | `apps/firecrawl-mcp/templates/networkpolicy.yaml` already limits ingress to the gateway pods and egress to DNS + `firecrawl-api:3002` | adding a route changes no policy surface on the backend |
  | `apps/firecrawl-mcp/values.yaml`: **no `FIRECRAWL_API_KEY` by design**, upstream fixed to `http://firecrawl-api.firecrawl.svc.cluster.local:3002` | the route needs no Vault-delivered upstream secret; `credential: none` is honest |
  | exec probes + 13 committed assertions in `apps/firecrawl-mcp/tests/test_exec_probes.py` | the pod is probe-immune under the existing native NPs |
  | `mcp_firecrawl` is already in the `mcp-gateway-agents` scopeMap | the authorisation surface exists today and is inert |

  So the only gate is the **group decision**: who may use a shared, anonymous web fetcher. Note the
  architectural deviation this creates and record it in the design doc: unlike Grafana, Firecrawl is
  **not** caller-preserving — every caller's fetches run under the same upstream identity. The
  gateway audit still names the caller, but the destination control is per-group, not per-person.
  That is the trade the owner is being asked to accept.
  Steps once approved: add `/firecrawl/mcp` (+ its RFC 9728 metadata route) to
  `apps/mcp-gateway/values.yaml` with `statefulMode: stateless`, scope `mcp_firecrawl`,
  `credential: none`; `helm template` diff; ArgoCD sync; acceptance per §5.
  Interim option if the group decision will take longer: **remove the inert `mcp_firecrawl` grant**
  until the route exists, so no identity surface is granted for a path that cannot be taken.
- **TODO-4.2** (G3) Vault. The zero-custom-code path is identified and locally measured: per-route
  `x-vault-token` allow **plus** `statefulMode: stateless` (stateful mode caches the backend
  credential per MCP session, so a session id behaves like a bearer). Owner must accept both
  deviations. Then: create the admin scope + group, enable the route, and re-verify that the public
  plane still has no Vault path (`grep -c vault apps/mcp-gateway/values.yaml` must stay `0`).
  Note the admin plane currently consumes a pod, a CNP, an Ingress, an HTTPRoute and a scrape target
  to serve nothing — G3 and G6 are worth deciding together.
- **TODO-4.3** (G8) **Fetcher destination control** — the `firecrawl` namespace has no pod-level
  policy of its own, so the fetcher's outbound destinations are not constrained in-cluster. Prior
  attempt regressed the backend with an **unknown root cause**; the capture gates are in
  `docs/troubleshooting/mcp-firecrawl-egress-regression.md`. **No re-attempt is authorised.** This
  stays independent of TODO-4.1: the route can ship without it, and shipping it must not be a
  precondition for the route.

### Phase 5 — Resilience

- **TODO-5.1** (G6) Placement decision. Measured — the plan's earlier "every MCP pod pinned to
  `prusik`" was wrong in detail, which matters because the pins are not all the same kind:

  | Workload | Constraint | Replicas |
  |---|---|---|
  | `mcp-gateway`, `mcp-gateway-admin` | `kubernetes.io/hostname: prusik` | 1 |
  | `hermes-0`, `hermes-mcp-gateway` | inherited `defaultPodOptions.nodeSelector` → `prusik` | 1 |
  | `grafana-mcp` | none (currently lands on `prusik`) | 1 |
  | `firecrawl-mcp` | `kubernetes.io/arch: amd64` only | 1 |

  So the backends can already move; only the gateways and Hermes are hard-pinned. Decide per
  workload whether the pin is deliberate or a leftover. If spreading: the probe-source finding is
  node-independent (`world` on the health port matches whichever node's kubelet probes the pod, so
  the split-port shape survives a move), but re-measure rather than trust it — §6.1 is the trap that
  cost the most time in this project.
- **TODO-5.2** (G5) **Rollback dry run, written before executed.** Three layers, each with its own
  method — the layers are not interchangeable:
  1. **Config**: `git revert` + ArgoCD reconcile, per plane. Note the CNP rollback is template-only
     (`networkIsolation: false`), no pod churn.
  2. **Client**: OpenCode `opencode.json` entry restored from the retired server's known-good block
     (so TODO-3.2 must **save each block before deleting it**); Hermes sidecar restored by reverting
     TODO-3.1.
  3. **Ingress**: the documented nginx rollback in `docs/network/ingress.md` (pfSense alias, unbound
     override, pf states, Cloudflare DNS). This is the layer whose validity depends on G15 — the CNP
     nginx rules must still be present. Exercising it is also the only real proof that the coupling
     holds, so the dry run must include a check that a request through nginx reaches the gateway.
  Write the whole thing down first; then exercise layer 2 (cheapest, reversible) and, if the owner
  agrees, layer 3. Explicit statement to include: **removing a CNP is not the rollback for an auth
  problem** — it widens exposure without fixing the failure.
- **TODO-5.3** HA / second replica stays out of scope until session behaviour across replicas is
  measured (design lists it as future). Both live routes are `statefulMode: stateless`, which
  suggests a second replica is cheap — but measure, do not infer.

### Execution log (2026-11-10)

Commits landed today:

| Commit | Subject |
|--------|---------|
| bf6c92f36 | docs corrections |
| d760a5f81 | test assertions for both data planes |
| 3c99e3f5c | CNP retention comments |
| 8b1b7007a | hermes sidecar removal |
| 8fa3885ff | firecrawl route plus backend placement pins and grafana-mcp scrape attempt (later reverted) |
| fe5837fe0 | gateway alerts |
| b351fb11a | hermes firecrawl client entry |
| e8718d549 | hermes-mcp suite repair |
| 30abdcd74 | job image digest fix |
| 18154236f | in-cluster re-verification |
| fa96d899a | per-route Kanidm human groups for context7 and firecrawl |

**Lesson:** commit 8fa3885ff has a subject mentioning only the firecrawl route but also contains
grafana-mcp and firecrawl-mcp changes. An earlier aborted pre-commit run left unrelated paths staged;
`git commit` commits the whole index. Rule: stage and commit in one step, or use `git commit -- <paths>`,
and always check `git show --stat` after committing.

**Note:** concurrent workstream removed Hermes `hostAliases` pin (commit 78ae995ee) because public DNS
now resolves `mcp.grigri.cloud` to the Envoy address directly. Any line in this plan describing that
pin as current is historical.

### Execution log addendum: per-route human authorisation (fa96d899a)

`grafana-users` used to grant both `mcp_grafana` and `mcp_context7`, so a human who could reach Grafana
could reach context7, and no group granted `mcp_firecrawl` to humans at all. The chart now renders one
group per route that needs its own human gate.

What landed:

| Item | Shape |
|---|---|
| New values key | `kanidm.humanGroups: [mcp-context7-users, mcp-firecrawl-users]` |
| New template | `apps/mcp-gateway/templates/kanidm-group.yaml` renders each name as a `KanidmGroup` with **no `members` field** (person membership is never encoded in CRDs, `docs/user-guide/kanidm.md`) |
| Human scope map | `kanidm.oauth2Client.humanScopeMap` is now three entries: `mcp-context7-users` → [`mcp_context7`], `mcp-firecrawl-users` → [`mcp_firecrawl`], `grafana-users` → [`mcp_grafana`] |
| Workload scope map | `mcp-gateway-agents` → [`openid`, `email`, `profile`, `mcp_firecrawl`, `mcp_grafana`, `mcp_context7`], deliberately **unchanged** |
| Removed | `mcp_context7` from `grafana-users` |

Why the workload group keeps all three scopes while humans are split per route: agents are workloads.
Splitting them would mean encoding service-account membership in the per-route human groups, and CRD
`members` is authoritative when present, so a reconcile could clobber people the owner added by hand.
The asymmetry is intentional; do not "finish the job" by moving agents into the human groups.

Live verification (measured 2026-11-10, all read-only):

- Both `KanidmGroup` CRs report `ready: true` with condition `Exists=True` ("Group exists.").
- The `KanidmOAuth2Client` `mcp-gateway` reports `ScopeMapUpdated=True` and every other condition
  `True`; its live `spec.scopeMap` matches the four-group shape above.
- A Kaniop reconcile ran **after** the manual membership change and left the membership intact. That is
  the evidence that a `KanidmGroup` CR without a `members` field does not clobber manually added people.
- The owner added themselves to both new groups with the CLI and confirmed with
  `kanidm group list-members`:

  ```bash
  kanidm group add-members <group> <account> -D idm_admin -H https://idm.grigri.cloud
  ```

  (Person names and account names are not recorded here, per the repo rule.)
- Suites after the change: `chart-checks` 200/0, `foundation-checks` 61/0, `route-checks` 110/0,
  `local-boot-checks` 79/0, `helm lint` clean.

Caveat this introduces (stated as a rule, not about anyone): `mcp_context7` left `grafana-users`, so any
other member of `grafana-users` loses context7 on a **new** OAuth consent. Kanidm evaluates scope maps at
consent time, so an already-issued token keeps working until it expires and the loss only materialises on
re-consent. The remedy is manual:

```bash
kanidm group add-members mcp-context7-users <account> -D idm_admin -H https://idm.grigri.cloud
```

The population affected is the household/other members of `grafana-users`, which is exactly why this was
announced rather than shipped silently.

CLI reality on the operator's workstation (measured, durable): every `kanidm` command must pass
`-H https://idm.grigri.cloud` (there is no `~/.config/kanidm/config`; the packaged `/etc/kanidm/config`
`uri` is still the `https://idm.example.com` placeholder), the client 1.12.0 / server 1.11.2 mismatch
warning is benign for group read/write, `KANIDM_PASSWORD` replaces the interactive `kanidm login` prompt
in a non-TTY shell, and `kanidm logout -D idm_admin -H <url>` should follow a session so no long-lived
ReadWrite token stays in `~/.cache/kanidm_cli`. Full notes: `docs/user-guide/kanidm.md`.

## 5. Verification Checklist

Re-run these before trusting any claim in this document. All read-only.

- [ ] Pods and restarts:
      `for ns in mcp-gateway mcp-gateway-admin grafana-mcp firecrawl-mcp; do kubectl --context=grigri -n $ns get pods; done`
- [ ] CNPs valid: `kubectl --context=grigri get cnp -A | grep -E 'mcp-gateway|grafana-mcp'` → 3 × `Valid=True`
- [ ] ArgoCD: `kubectl --context=grigri -n argocd get applications | grep -E 'mcp|grafana-mcp|firecrawl-mcp'`
- [ ] Live routes: `kubectl --context=grigri -n mcp-gateway get cm mcp-gateway-standalone-config -o jsonpath='{.data.config\.yaml}' | grep -E 'name:|host:|statefulMode'`
- [ ] Hermes acceptance (the authoritative functional test):
      `kubectl --context=grigri exec -i -n hermes hermes-0 -c hermes -- python - < test/mcp-gateway/hermes-live-acceptance.py`
      → must print `"check": "ACCEPTANCE_PASS"` with `"servers": 3` (context7, grafana, firecrawl)
- [ ] OpenCode side: one real `homelab-context7` `query-docs` call, one `homelab-grafana`
      `list_datasources` call, and one `firecrawl` scrape, all returning real upstream content
- [ ] Per-route human groups (after fa96d899a):
      `kubectl --context=grigri -n mcp-gateway get kanidmgroups.kaniop.rs` lists `mcp-context7-users`,
      `mcp-firecrawl-users`, `mcp-gateway-agents`, all `ready: true`; and
      `kubectl --context=grigri -n mcp-gateway get kanidmoauth2clients.kaniop.rs mcp-gateway -o jsonpath='{.status.conditions}'`
      shows `ScopeMapUpdated=True`
- [ ] Audit stream: `{namespace="mcp-gateway"} | json | scope="request"` in Loki shows
      `jwt.sub`, `mcp.target`, `gen_ai.tool.name`, `http.status`
- [ ] Metrics: `up{namespace=~"mcp-gateway|mcp-gateway-admin"} == 1`
- [ ] Serving data plane (post-migration): `kubectl --context=grigri -n envoy-gateway get gateway` →
      both `Programmed=True`; and `src.addr` in the gateway access log resolves to `envoy-…-proxy`
      pods, not `ingress-nginx-*`. `src.addr` is stronger evidence than reading either chart.
- [ ] Rollback path still valid: the nginx `fromEndpoints` rules are present in both gateway CNPs and
      still match live nginx pods (`kubectl --context=grigri get pods -n ingress-nginx-external -o wide`).
      If ingress-migration Phase 6 has deleted nginx, this check inverts — the rules must then be
      *gone* (G15).
- [ ] Ingress health: `bash scripts/gateway-check.sh` (read-only, 12 checks) — extend it per TODO-2.8
      so the MCP hosts are covered.
- [ ] Local suites: `test/mcp-gateway/chart-checks.py` (200 as of fa96d899a),
      `apps/firecrawl-mcp/tests/test_exec_probes.py` (13), `route-checks.py` (110),
      `local-boot-checks.py` (79), `foundation-checks.py` (61), `grafana-backend-checks.py`
- [ ] Denied-path check after any CNP edit: TCP from an unlisted pod to the app port must time out.
      Prefer the committed Job over ad-hoc pods — `test/mcp-gateway/jobs/verify-networkpolicy.yaml`
      (needs TODO-2.4's header/expectation refresh first); owner applies and deletes it.
- [ ] Cross-identity properties (session isolation, no credential cross-over) via the committed
      harness: `test/mcp-gateway/identity-ab/` — OFFLINE tier always runs; LIVE tier skips loudly
      when its prerequisites are missing, so a green run without them proves nothing.

## 6. Troubleshooting / Known Pitfalls

### 6.1 Probe + policy (the one that costs the most time)

Any ingress rule on an `httpGet`/`tcpSocket`-probed pod **kills the pod** unless the probe source is
matched, and the only entity that matches it here is `world`. `host`, `remote-node`, `cluster` and
`fromCIDR` (even the exact source `/32`) are all falsified — the CNP is accepted as `Valid=True` and
still denies the probe. Use the split-port shape; keep `cluster` off the health port.
Full matrix: `docs/troubleshooting/mcp-firecrawl-egress-regression.md`; durable rule:
`docs/conventions/cilium.md`. `exec` probes are policy-immune — that is why `firecrawl-mcp` keeps
native NetworkPolicies.

### 6.2 Loki time windows lie

A 6 h `count_over_time` showed **zero** streams for `grafana-mcp`, `mcp-gateway-admin` and
`firecrawl-mcp`, which looked like a missing log pipeline. It was not: those namespaces are simply
quiet, and the window cut off their last entries. At 24 h all four appear
(`mcp-gateway` 3262, `mcp-gateway-admin` 389, `grafana-mcp` 52, `firecrawl-mcp` 27 lines).
**Always use ≥24 h before concluding a namespace is not collected.**

### 6.3 Grafana MCP errors arrive inside HTTP 200

A tool-level failure can be returned as a successful JSON-RPC response. Check the tool result and any
nested `status`, never just the transport status.

### 6.4 Stale selectors and stale comments fail silently

The Firecrawl ingress rule matched `app.kubernetes.io/name: mcp-gateway` for months while the real
gateway pods are labelled `agentgateway-standalone` / `instance=mcp-gateway` / `component=standalone`
(fixed at `0c1aee0b9`). Any new `fromEndpoints`/`podSelector` must be checked against **live pod
labels**, not against the chart name.

The same failure mode shows up in prose: `apps/mcp-gateway/values.yaml` still calls the live Grafana
route "not rendered … gets 404", and `jobs/verify-networkpolicy.yaml` still names a template that was
deleted. Three separate instances of a comment outliving the code it described. **Treat every
explanatory comment as a claim to verify against the rendered output or the live object, never as
ground truth** — and fix it in the same commit that changes the behaviour.

### 6.5 Do not hand-patch the cluster

ArgoCD `selfHeal: true` reverts `kubectl apply/patch/edit` almost instantly. Commit, push, let it
reconcile.

### 6.6 Other workstreams move the ground under you

This document's first draft recorded "the CNPs only allow ingress-nginx pods, so the Envoy
switchover will fail closed" as a High-severity gap. It was **already false when written**: a
concurrent workstream had added `envoy`/`proxy` rules to both gateway CNPs, pinned Hermes to the
external Envoy LB (`192.168.193.14`), and Envoy was serving real 200s. A single `git fetch` plus a
look at the live CNP and the gateway's `src.addr` log field disproved it in two minutes.

Rule: **before recording a cross-workstream dependency as a gap, fetch and measure it.** Prefer
evidence that cannot go stale silently — `src.addr` in the gateway access log names the actual
forwarding pod, which is stronger than reading either chart's selectors.

Revision 2 repeated the lesson from the other side. TODO-2.2 told a future reader to delete the nginx
`fromEndpoints` rules "once nginx stops serving these hosts". It has stopped serving them — and the
rules are still required, because `docs/network/ingress.md` documents nginx as the **rollback path**
until Phase 6. A rule that is load-bearing for recovery is not dead weight, and nothing in the
gateway chart says so (G15).

Second rule: **a transient dual-serving state is not a cleanup opportunity.** Before proposing to
delete a selector, a rule or an object, read the neighbouring workstream's rollback doc and ask what
depends on it *for recovery*, not just for the happy path.

## 7. Reference Information

| Item | Value |
|---|---|
| Public gateway URL | `https://mcp.grigri.cloud` — HTTPRoute → Envoy gateway `external`, VIP `192.168.193.14` (the nginx-external Ingress object remains, idle) |
| Admin gateway URL | `https://mcp.internal.grigri.cloud` — HTTPRoute → Envoy gateway `internal`, VIP `192.168.193.13` (nginx-internal Ingress remains, idle) |
| OAuth client / redirect | `mcp-gateway`, PKCE S256, `http://127.0.0.1:15000/callback`, `strictRedirectUrl: true` |
| Issuer (public plane) | `https://idm.grigri.cloud/oauth2/openid/mcp-gateway` |
| Scopes | `mcp_context7`, `mcp_grafana`, `mcp_firecrawl` (all three live: one route each, all `stateless`) |
| Human grant | per-route, via `kanidm.oauth2Client.humanScopeMap`: `mcp-context7-users`→`mcp_context7`, `mcp-firecrawl-users`→`mcp_firecrawl`, `grafana-users`→`mcp_grafana` (person membership manual, never in CRDs); workload grant: `mcp-gateway-agents`→all three scopes; `supScopeMap`: `grafana-mcp-users`→`editor`, `grafana-admins`→`admin` |
| Per-route discovery | Each route's RFC 9728 document advertises exactly ONE scope: `.../context7/mcp`→`["mcp_context7"]`, `.../grafana/mcp`→`["mcp_grafana"]`, `.../firecrawl/mcp`→`["mcp_firecrawl"]`, all with `authorization_servers: ["https://idm.grigri.cloud/oauth2/openid/mcp-gateway"]` |
| Gateway ports | app 3000, metrics 15020, healthz 15021 (`adminAddr: "off"`, nothing on 15000) |
| grafana-mcp ports | app 8000, healthz 8081 |
| firecrawl-mcp port | 3000 (exec probes) |
| Hermes DNS fix | main-pod `hostAliases`: `mcp.grigri.cloud` → **`192.168.193.14`** (external Envoy Gateway LB, since `2121ca476`). Previously `192.168.193.4` (nginx-external LB) — the public IP hairpins and times out either way |
| Data planes | **Envoy only** (measured `src.addr` = the external Envoy proxy pods). ingress-nginx still runs at `192.168.193.1`/`.4`, serves no MCP traffic, and is the documented rollback path until ingress-migration Phase 6. Both planes are in the CNP allow-lists **on purpose** — see G15 |
| Node | gateways + Hermes pinned to `prusik`; `grafana-mcp` unpinned, `firecrawl-mcp` amd64-only. Pod CIDR `10.0.4.0/24`, `cilium_host` `10.0.4.252` |
| Cilium | v1.20.4, `enable-policy: default`, `enable-host-firewall: false`, `allow-localhost` unset |

## 8. Files to Modify

| File | Phase | Change |
|---|---|---|
| `docs/design/mcp-gateway-architecture.md` | 1 | status block, inventory table, policy section |
| `planning/mcp-gateway-implementation.md` | 1 | Task 2/5 status, "still open" list, 13 stale "uncommitted" lines |
| `apps/mcp-gateway/values.yaml` | 1 | stale `mcp_grafana` "inert / 404" comment |
| `planning/mcp-canary-*.md` (tracked), `apps/mcp-canary/` (untracked debris) | 1 | delete or mark superseded |
| `test/mcp-gateway/foundation-checks.py`, `route-checks.py` | 1 | assert the HTTPRoute **and** the Ingress, labelled serving vs rollback (G14) |
| `apps/grafana-mcp/values.yaml`, `apps/mcp-gateway*/templates/ciliumnetworkpolicy.yaml` | 1 | nginx-era comments only — no selector change |
| `docs/network/ingress.md` | 2 | rollback section: name the CNP allow-list coupling and the Phase 6 gate (G15) |
| `docs/conventions/gateway-api-routes.md` | 2 | confirm the Hermes `hostAliases` pin survives Phase 6 DNS cleanup |
| `test/mcp-gateway/chart-checks.py` | 2 | drift guard: rendered Envoy selector == live proxy pod labels (TODO-2.2 step 2) |
| `scripts/gateway-check.sh` | 2 | cover `mcp.grigri.cloud` / `mcp.internal.grigri.cloud` (TODO-2.8) |
| `apps/grafana-mcp/values.yaml`, `apps/firecrawl-mcp/values.yaml` | 2 | scrape targets (+ CNP metrics rule if enabled) |
| `test/mcp-gateway/jobs/verify-networkpolicy.yaml`, `verify-foundation-incluster.yaml` | 2 | refresh stale policy/ingress references, then re-run |
| `apps/hermes/values.yaml`, `apps/hermes/templates/*` | 3 | remove sidecar, refresher, token-init, `/shared`, and the `grafana` MCP server entry |
| `~/.config/opencode/opencode.json` (not in repo) | 3 | **DONE** for the three routes (owner replaced each bypassing local server with a remote gateway entry; see G4 and section 10) |
| `apps/mcp-gateway/values.yaml` | 4 | **DONE** (G2): `/firecrawl/mcp` + metadata route added; **DONE** (fa96d899a): per-route `kanidm.humanGroups` and `humanScopeMap` rendered |
| `apps/mcp-gateway-admin/values.yaml` | 4 | Vault scope, group, route |
| `apps/firecrawl/**` (separate workstream, unauthorised) | 4 | fetcher egress/destination control (G8) |
| node pinning / replica values | 5 | per-workload placement decision |
| `apps/mcp-gateway*/templates/ciliumnetworkpolicy.yaml` | **6 of the ingress migration** | drop the `ingress-nginx` rules only in the change that deletes ingress-nginx |

## 9. Execution Order

```
Phase 1 (docs + cleanup + G14) ── no dependencies, no live change, do first
Phase 2 (handoff safety, obs)  ── G15 rollback invariant; G13 Jobs; G7 scraping; G12 audit posture
Phase 3 (consolidation)        ── G1 done; G4 done for the three routes (owner action)
Phase 4 (backends)             ── G2 route = group decision only; G3 Vault = owner decision; G8 unauthorised
Phase 5 (resilience)           ── after Phase 3, so rollback is tested against the final shape
```

Hard ordering rules:

- **The nginx CNP rules are not debris.** G9 is closed, but removing those rules belongs to ingress-
  migration Phase 6, in the same change that deletes ingress-nginx — because they are the allow-list
  for the documented rollback path (G15). Gateway-side "tidying" here would make a future rollback
  fail closed at the network layer.
- **G1 before G4's Grafana items.** (Satisfied 2026-11-10: G1 landed in `8b1b7007a`, G4's Grafana
  item retired when the owner converted `grafana-grigri` to a remote gateway entry.) Removing the
  Hermes sidecar while OpenCode still had two local `mcp-grafana` servers would not have reduced the
  Grafana path count; the ordering rule held.
- **G2's route and G8's fetcher control are independent.** The route may ship on the group decision
  alone; the fetcher egress work has an unresolved regression and must not become a precondition —
  or be bundled — with it.
- **Save before deleting.** Every `opencode.json` block retired in TODO-3.2 must be recorded first;
  it is the rollback artefact for layer 2 of TODO-5.2.
- Never weaken a CNP to make a probe pass. The split-port shape is the only measured-safe option.

## 10. Handover: connecting OpenCode to the gateway

Gateway entry points (measured 2026-11-10):

- `https://mcp.grigri.cloud/context7/mcp`
- `https://mcp.grigri.cloud/grafana/mcp`
- `https://mcp.grigri.cloud/firecrawl/mcp`

All use OAuth PKCE against issuer `https://idm.grigri.cloud/oauth2/openid/mcp-gateway`,
redirect `http://127.0.0.1:15000/callback`. Each publishes RFC 9728 discovery at
`/.well-known/oauth-protected-resource/<target>/mcp`, so a discovery-capable client needs only the URL
**plus** the out-of-band `client_id` and `redirect_uri`. Kanidm supports neither dynamic client
registration (DCR) nor client-id metadata documents (CIMD): the authorization-server metadata
document at `.../.well-known/oauth-authorization-server` omits both `registration_endpoint` and
`client_id_metadata_document_supported`. A client that falls through CIMD and DCR to nothing fails
expecting a `client_id`. The pre-registered client is `mcp-gateway` (the short name, because the
`KanidmOAuth2Client` sets `preferShortUsername: true`), and it is `public: true` (PKCE only, no
secret). Each OpenCode server's `oauth` block must therefore include:

```json
{
  "client_id": "mcp-gateway",
  "redirect_uri": "http://127.0.0.1:15000/callback",
  "scope": "<the single scope the route's RFC 9728 document advertises>",
  "callback_port": 15000
}
```

After editing the config, clear any cached credentials and re-authorise: `opencode mcp logout <server>`.
Full diagnosis and the reference implementation for in-cluster agents:
[`docs/troubleshooting/kanidm-no-dcr-mcp-client-id.md`](../docs/troubleshooting/kanidm-no-dcr-mcp-client-id.md).

**Per-route discovery advertises exactly ONE scope.** Measured live:

| Route | `scopes_supported` | `authorization_servers` |
|---|---|---|
| `.../context7/mcp` | `["mcp_context7"]` | `["https://idm.grigri.cloud/oauth2/openid/mcp-gateway"]` |
| `.../grafana/mcp` | `["mcp_grafana"]` | `["https://idm.grigri.cloud/oauth2/openid/mcp-gateway"]` |
| `.../firecrawl/mcp` | `["mcp_firecrawl"]` | `["https://idm.grigri.cloud/oauth2/openid/mcp-gateway"]` |

Corollary (and the reason a client must be configured with one server per route, not one server that
asks for all three scopes): Kanidm refuses the **whole** authorization when any requested scope is not
in the granted set. A caller that asks for `mcp_context7` and `mcp_firecrawl` in the same request is
refused wholesale unless they are a member of **both** `mcp-context7-users` and `mcp-firecrawl-users`.
Per-server single-scope configuration is therefore required.

**Human authorisation is per-route** (since `fa96d899a`):

| Kanidm group | Scope granted | Route gated |
|---|---|---|
| `mcp-context7-users` | `mcp_context7` | `/context7/mcp` |
| `mcp-firecrawl-users` | `mcp_firecrawl` | `/firecrawl/mcp` |
| `grafana-users` | `mcp_grafana` | `/grafana/mcp` |

Workloads use `mcp-gateway-agents`, which keeps all three scopes. Person membership in the human
groups is managed manually with `kanidm group add-members <group> <account> -D idm_admin -H https://idm.grigri.cloud`
and is never encoded in CRDs (`docs/user-guide/kanidm.md`).

**Caveat from the split** (stated as a rule, not about anyone): `mcp_context7` moved out of
`grafana-users`, so any other member of `grafana-users` loses context7 on a **new** OAuth consent.
Kanidm evaluates scope maps at consent time, so an already-issued token keeps working until it
expires and the loss only materialises on re-consent. The remedy is manual:

```bash
kanidm group add-members mcp-context7-users <account> -D idm_admin -H https://idm.grigri.cloud
```

The population affected is the household/other members of `grafana-users`.

**OpenCode config state (measured 2026-11-10):** `~/.config/opencode/opencode.json` now holds three
remote gateway entries, one per route, each with `type: remote` and `oauth.scope` set to exactly the
scope that route's discovery advertises:

| Server name | URL | OAuth scope |
|---|---|---|
| `context7` | `https://mcp.grigri.cloud/context7/mcp` | `mcp_context7` |
| `grafana-grigri` | `https://mcp.grigri.cloud/grafana/mcp` | `mcp_grafana` |
| `firecrawl` | `https://mcp.grigri.cloud/firecrawl/mcp` | `mcp_firecrawl` |

All three use `oauth.callback_port: 15000`. The local `context7` and `firecrawl` servers no longer
exist in the config. The retirement-order rationale (the local `context7` duplicate, the local
`grafana-grigri` that bypassed the gateway against the same instance, the local `firecrawl` that
required the human grant to land first) is kept as history: the blocks were replaced in place rather
than deleted, so agent-facing server names did not change.

**Still local by decision:** `grafana-forkline` (different, external Grafana, out of this gateway's
scope), `searxng` (no gateway route yet), `playwright` (out of scope by design).

**Post-retirement check:** one real tool call through each `homelab-*` server, plus check the
gateway audit stream in Loki (`{namespace="mcp-gateway"} | json | scope="request"`) to confirm the
call is recorded with the caller's `jwt.sub`.

## Related

- [`mcp-gateway-implementation.md`](mcp-gateway-implementation.md) — build-out, matrices, acceptance
- [`docs/design/mcp-gateway-architecture.md`](../docs/design/mcp-gateway-architecture.md)
- [`docs/conventions/cilium.md`](../docs/conventions/cilium.md) — kubelet probes under enforced policies
- [`docs/troubleshooting/mcp-firecrawl-egress-regression.md`](../docs/troubleshooting/mcp-firecrawl-egress-regression.md)
- [`docs/conventions/gateway-api-routes.md`](../docs/conventions/gateway-api-routes.md)
- [`docs/network/ingress.md`](../docs/network/ingress.md) — data planes, rollback, Phase 6 removals
