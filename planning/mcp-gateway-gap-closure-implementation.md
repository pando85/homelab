# MCP Gateway Gap Closure Implementation Plan

**Status:** Not started. This is the continuation plan for the MCP gateway work.
**Anchor revision:** measured against `origin/master` at `2121ca476` (2026-11-10). Findings are
timestamped; the cluster moves — re-run §5 before trusting any line here.
**Predecessor:** [`mcp-gateway-implementation.md`](mcp-gateway-implementation.md) — build-out history,
measured probe/policy matrices, and the E2E acceptance record. Do not duplicate that evidence here.
**Architecture:** [`docs/design/mcp-gateway-architecture.md`](../docs/design/mcp-gateway-architecture.md)
(needs the corrections listed in Phase 1 — its status block still describes a pre-isolation tree).

## 1. Overview

The gateway MVP is **live, network-isolated, and serving two real routes** to two real clients
(OpenCode and Hermes). Measured against the design doc's own MVP definition
(`planning/mcp-gateway-implementation.md`, "Success Criteria"), **4 of 7 criteria are met**.

This plan records the remaining gaps as a prioritised register, separates what an agent can do
unaided from what needs an owner decision, and gives re-verification commands so the next session
starts from measurement rather than from this document's claims.

The dominant theme is **consolidation, not construction**: the gateway currently exists *alongside*
the legacy paths it was meant to replace. Three Grafana MCP paths and one Firecrawl path still bypass
it entirely.

### 1.1 Success Criteria scorecard (measured 2026-11-10)

| # | Criterion (verbatim from the predecessor plan) | State | Evidence |
|---|---|---|---|
| 1 | agentgateway deployed (public + admin) | **MET** | both pods `1/1 Running`; admin restarts 17 (pre-existing baseline, no new), public 0 |
| 2 | Grafana MCP standalone (**sidecar removed**) | **PARTIAL** | standalone pod live and serving; sidecar still in `hermes-0` — see G1 |
| 3 | Firecrawl MCP deployed | **PARTIAL** | pod live; **no gateway route exists at all** — see G2 |
| 4 | Vault MCP deployed (admin-only, scope decided) | **NOT MET** | `backends.vault.enabled: false`; admin `routes: []` — see G3 |
| 5 | OpenCode/Hermes migrated (≥1 instance each) | **MET (letter), NOT (spirit)** | 2 of 8 OpenCode servers go through the gateway — see G4 |
| 6 | All tests pass, rollback tested | **PARTIAL** | suites green and **all tracked in git** (the predecessor plan wrongly calls them uncommitted); rollback never exercised — see G5; committed in-cluster Jobs never re-run since isolation landed — see G13 |
| 7 | Owner approval recorded for each gate | **PARTIAL** | gates 1, 2, 7 done; 3 pending; 4, 5 not reached |

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
- Network isolation: three CiliumNetworkPolicies (`mcp-gateway`, `mcp-gateway-admin`, `grafana-mcp`),
  all `Valid=True`, in the measured split-port shape. Both permitted and denied paths were measured
  per plane at enablement. The gateway CNPs already allow **both** ingress data planes
  (ingress-nginx and Envoy proxies), which is why the in-flight Envoy migration did not break them.
- Audit is **better than the design specified**: live gateway log lines carry `jwt.sub`,
  `mcp.target`, `mcp.method.name`, `gen_ai.tool.name`, `http.status`, `trace.id`/`span.id`, with no
  payload. All four MCP namespaces do reach Loki (see the correction in §6.2).
- Metrics scraped on both planes: `up{namespace=~"mcp-gateway|mcp-gateway-admin"} == 1`.

## 2. Gap Register

Severity: **High** = defeats a stated architecture goal or blocks MVP completion; **Medium** =
residual risk or missing control; **Low** = hygiene/documentation.

| ID | Gap | Sev | Blocked on | Phase |
|----|-----|-----|-----------|-------|
| G1 | Hermes legacy `grafana-mcp` sidecar not removed; two Grafana MCP paths live, legacy one has `auth=None` + shared SA token | High | owner approval (gate 3, now satisfiable) | 3 |
| G2 | Firecrawl: pod deployed but **no route in the gateway chart** (not even commented), while `mcp_firecrawl` scope is already granted → inert scope, unreachable pod | High | SSRF/egress gate + group decision | 4 |
| G3 | Vault MCP not deployed; admin plane serves nothing (`routes: []`, `scopeMap: []`, group disabled) | High | owner decision: per-route `x-vault-token` allow + stateless | 4 |
| G4 | Not a single entry point: 6 of 8 OpenCode MCP servers bypass the gateway — a duplicate `context7`, plus `grafana-grigri` hitting the *same* Grafana the gateway fronts with a local credential (`grafana-forkline` is a different, external instance and stays out of scope) | High | per-server owner decision | 3 |
| G5 | Rollback never tested (no dry run, no tested client switch-back) | Medium | owner approval of method | 5 |
| G6 | Placement still provisional: every MCP pod pinned to `prusik`, 1 replica each | Medium | owner decision (plan said revisit once stable — stable ~25 h) | 5 |
| G7 | No Prometheus target for `grafana-mcp` / `firecrawl-mcp`; backend failure only visible via gateway-side errors | Medium | none | 2 |
| G8 | `firecrawl` **backend** namespace has no pod-level policy (only `firecrawl-mcp` is isolated) | Medium | needs its own measurement; prior attempt regressed | 4 |
| G9 | ~~Envoy switchover dependency~~ **CLOSED — verified 2026-11-10.** All three CNPs already carry an `envoy`/`proxy` `fromEndpoints` rule alongside the nginx one, and both data planes are serving live (Hermes via Envoy, OpenCode via nginx) | — | none | — |
| G10 | Documentation drift: design doc + plan + one values comment still describe the pre-isolation, pre-parity state | Low | none | 1 |
| G11 | Canary leftovers: orphan `mcp-canary` namespace, untracked `apps/mcp-canary/` debris, two superseded canary planning docs | Low | none | 1 |
| G12 | `jwt.sub` logged in full into Loki (180-day retention) while the design says "redacted"; no dashboards/alerts on the gateway audit stream | Low | owner decision on retention/pseudonymisation | 2 |
| G13 | Committed verification assets are stale and have not been re-run since isolation landed: `jobs/verify-networkpolicy.yaml` still names the deleted native-NP template, and the predecessor plan calls 13 tracked suites/lines "uncommitted" | Medium | owner approval to apply the Jobs | 1, 2 |

## 3. Context & Current State (measured)

### 3.1 Deployed topology

```
ns mcp-gateway         mcp-gateway-standalone   1/1  prusik  ClusterIP 3000/15020/15021
                       routes LIVE: /context7/mcp, /grafana/mcp (+ 2 RFC 9728 metadata routes)
                       served by BOTH nginx-external (mcp.grigri.cloud, OpenCode) and
                       external Envoy (192.168.193.14, Hermes) — see TODO-2.2
ns mcp-gateway-admin   mcp-gateway-admin        1/1  prusik  ClusterIP 3000/15020/15021
                       routes: []  (fail closed; Vault disabled)
                       nginx-internal + internal Envoy, mcp.internal.grigri.cloud
ns grafana-mcp         grafana-mcp              1/1  prusik  ClusterIP 8000, healthz 8081
ns firecrawl-mcp       firecrawl-mcp            1/1  prusik  ClusterIP 3000 (exec probes)
ns hermes              hermes-0                 containers: grafana-mcp, grafana-mcp-refresher, hermes
                       hermes-mcp-gateway       token-refresher deployment (Kanidm token exchange)
ns mcp-canary          EMPTY — orphan, no ArgoCD Application
```

ArgoCD: `mcp-gateway`, `mcp-gateway-admin`, `grafana-mcp`, `firecrawl-mcp` all Synced/Healthy.

### 3.2 Client inventory (the consolidation gap in one table)

OpenCode (`~/.config/opencode/opencode.json`, `mcp` + `mcp.servers`):

| Server | Transport | Through gateway? |
|---|---|---|
| `homelab-context7` | remote, OAuth PKCE | **yes** |
| `homelab-grafana` | remote, OAuth PKCE | **yes** |
| `context7` | local `npx @upstash/context7-mcp@latest` | no — duplicate of the gateway route |
| `grafana-grigri` | local `mcp-grafana`, `GRAFANA_URL=https://grafana.grigri.cloud` | no — **same instance the gateway fronts**, with local credentials: a true bypass |
| `grafana-forkline` | local `mcp-grafana`, `GRAFANA_URL=https://grafana.nbg1-c01.forkline.dev` | no — but a **different, external** Grafana: out of this gateway's scope |
| `firecrawl` | local `npx firecrawl-mcp` → `firecrawl.internal.grigri.cloud` | no |
| `searxng` | local `npx mcp-searxng` → `searxng.internal.grigri.cloud` | no |
| `playwright` | local `npx @playwright/mcp@latest` | no — out of scope by design |

Hermes (`hermes-0`, via `hermes_cli.mcp_config._get_mcp_servers()`):

| Server | Endpoint | Auth |
|---|---|---|
| `homelab-context7` | `https://mcp.grigri.cloud/context7/mcp` | oauth |
| `homelab-grafana` | `https://mcp.grigri.cloud/grafana/mcp` | oauth |
| `grafana` | `http://localhost:8080/sse` (sidecar) | **none** — shared SA token via `/shared/grafana-jwt` |
| `comunio` | local node command | none (unrelated workload) |

Consequence: an agent that picks `grafana` instead of `homelab-grafana` gets a **shared** backend
identity and produces **no** gateway audit record. That is the exact failure mode the architecture's
"No silent fallback" and "single entry point" goals exist to prevent.

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

### Phase 2 — Observability and handoff safety (no auth change)

- **TODO-2.1** (G7) Decide and implement scraping for `grafana-mcp` and `firecrawl-mcp`.
  `grafana-mcp` sets `serviceMonitor.enabled: false`; either enable it or document why the backend
  is intentionally unscraped. If enabled, the CNP needs a metrics rule from Prometheus pods —
  follow the shape already used on the gateway planes, and re-run the probe-safety checks.
- **TODO-2.2** (G9 — now a maintenance item, not a risk) The Envoy switchover constraint is already
  satisfied. Measured 2026-11-10, both planes serve simultaneously:

  | CNP | app-port `fromEndpoints` | live source seen in gateway logs |
  |---|---|---|
  | `mcp-gateway` (public) | `ingress-nginx` + `component: controller` in `ingress-nginx-external`; **and** `envoy` + `component: proxy` + `gateway.envoyproxy.io/owning-gateway-name: external` in `envoy-gateway` | OpenCode → `10.0.4.148` (`ingress-nginx-external-controller-…h7bhn`); Hermes → `10.0.4.6` / `10.0.4.44` (`envoy-envoy-gateway-external-…`) |
  | `mcp-gateway-admin` | `ingress-nginx` in `ingress-nginx`; **and** `envoy` proxy with `owning-gateway-name: internal` | `10.0.4.73`, `10.0.4.141` (internal Envoy proxies) |
  | `grafana-mcp` | gateway pods only (`agentgateway-standalone` / `instance=mcp-gateway` / `component=standalone`) — unchanged and correct: the backend's peer is the gateway, not the ingress | n/a |

  Remaining work, in order:
  1. Nothing to do before switchover — it is already carrying traffic.
  2. When nginx stops serving these hosts, **remove the `ingress-nginx` rules** from both gateway CNPs
     so the allow-list does not keep a dead data plane in it. One values/template edit per chart.
  3. If Envoy's proxy pod labels ever change (`gateway.envoyproxy.io/owning-gateway-name`,
     `app.kubernetes.io/component: proxy`), the routes fail closed. Add a chart check asserting the
     rendered selector matches the live Envoy pod labels — the same class of bug as §6.4.
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

### Phase 3 — Consolidation (the actual MVP completion)

- **TODO-3.1** (G1) **Remove the Hermes Grafana sidecar.** Owner approval gate 3 is now satisfiable:
  parity is measured. Steps: confirm `global.mcpGateway.enabled: true` is the only path Hermes uses
  for Grafana; drop the `grafana-mcp` and `grafana-mcp-refresher` containers, the
  `grafana-mcp-token-init` init container and the `/shared` volume from `apps/hermes/values.yaml`;
  keep `kanidm-service-account.yaml`'s `grafana-mcp` SA only if the token exchange still consumes it
  (`identity.subjectTokenSecret: grafana-mcp-token` — **verify before deleting anything**).
  Rollback: revert the commit; the sidecar returns with the pod.
- **TODO-3.2** (G4) **Retire bypassing OpenCode servers, one at a time, owner-approved per server.**
  Recommended order and rationale (the two Grafana entries are *not* equivalent — measured from
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

- **TODO-4.1** (G2) Firecrawl. Two independent gates, do not collapse them:
  1. fetcher egress/SSRF control — prior attempt regressed the `firecrawl` backend with an
     **unknown root cause**; the required capture gates are in
     `docs/troubleshooting/mcp-firecrawl-egress-regression.md`. No re-attempt is authorised yet.
  2. the group decision for who may use web access.
  Only then add the `/firecrawl/mcp` route with `credential: none` and whole-backend scope
  `mcp_firecrawl`. Note the scope is *already granted* to `mcp-gateway-agents`, so today it is inert
  identity surface — consider removing it until the route exists.
- **TODO-4.2** (G3) Vault. The zero-custom-code path is identified and locally measured: per-route
  `x-vault-token` allow **plus** `statefulMode: stateless` (stateful mode caches the backend
  credential per MCP session, so a session id behaves like a bearer). Owner must accept both
  deviations. Then: create the admin scope + group, enable the route, and re-verify that the public
  plane still has no Vault path.
- **TODO-4.3** (G8) `firecrawl` backend namespace policy — only attempt after G2's egress gate is
  understood; it is the same subsystem that regressed before.

### Phase 5 — Resilience

- **TODO-5.1** (G6) Placement decision: keep `prusik` pinning (single failure domain, simpler probe
  behaviour) or spread. If spreading, remember the probe-source finding: `world` on the health port
  matches whichever node probes the pod, so the split-port shape survives a move — but re-measure.
- **TODO-5.2** (G5) Rollback dry run, written before executed: `git revert` + ArgoCD reconcile for
  each plane; client-side switch-back for OpenCode and Hermes; explicit statement that removing a
  CNP is **not** the rollback for an auth problem.
- **TODO-5.3** HA / second replica stays out of scope until session behaviour across replicas is
  measured (design lists it as future).

## 5. Verification Checklist

Re-run these before trusting any claim in this document. All read-only.

- [ ] Pods and restarts:
      `for ns in mcp-gateway mcp-gateway-admin grafana-mcp firecrawl-mcp; do kubectl --context=grigri -n $ns get pods; done`
- [ ] CNPs valid: `kubectl --context=grigri get cnp -A | grep -E 'mcp-gateway|grafana-mcp'` → 3 × `Valid=True`
- [ ] ArgoCD: `kubectl --context=grigri -n argocd get applications | grep -E 'mcp|grafana-mcp|firecrawl-mcp'`
- [ ] Live routes: `kubectl --context=grigri -n mcp-gateway get cm mcp-gateway-standalone-config -o jsonpath='{.data.config\.yaml}' | grep -E 'name:|host:|statefulMode'`
- [ ] Hermes acceptance (the authoritative functional test):
      `kubectl --context=grigri exec -i -n hermes hermes-0 -c hermes -- python - < test/mcp-gateway/hermes-live-acceptance.py`
      → must print `"check": "ACCEPTANCE_PASS"` with `"servers": 2`
- [ ] OpenCode side: one real `homelab-context7` `query-docs` call and one `homelab-grafana`
      `list_datasources` call, both returning real upstream content
- [ ] Audit stream: `{namespace="mcp-gateway"} | json | scope="request"` in Loki shows
      `jwt.sub`, `mcp.target`, `gen_ai.tool.name`, `http.status`
- [ ] Metrics: `up{namespace=~"mcp-gateway|mcp-gateway-admin"} == 1`
- [ ] Local suites: `test/mcp-gateway/chart-checks.py` (172),
      `apps/firecrawl-mcp/tests/test_exec_probes.py` (13), `route-checks.py`, `local-boot-checks.py`,
      `foundation-checks.py`, `grafana-backend-checks.py`
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

## 7. Reference Information

| Item | Value |
|---|---|
| Public gateway URL | `https://mcp.grigri.cloud` (`nginx-external`, VIP 192.168.193.4) |
| Admin gateway URL | `https://mcp.internal.grigri.cloud` (`nginx-internal`, VIP 192.168.193.13) |
| OAuth client / redirect | `mcp-gateway`, PKCE S256, `http://127.0.0.1:15000/callback`, `strictRedirectUrl: true` |
| Issuer (public plane) | `https://idm.grigri.cloud/oauth2/openid/mcp-gateway` |
| Scopes | `mcp_context7`, `mcp_grafana`, `mcp_firecrawl` (granted, inert — no route) |
| Human grant | group `grafana-users` → `mcp_grafana` + `mcp_context7`; `supScopeMap`: `grafana-mcp-users`→`editor`, `grafana-admins`→`admin` |
| Gateway ports | app 3000, metrics 15020, healthz 15021 (`adminAddr: "off"`, nothing on 15000) |
| grafana-mcp ports | app 8000, healthz 8081 |
| firecrawl-mcp port | 3000 (exec probes) |
| Hermes DNS fix | main-pod `hostAliases`: `mcp.grigri.cloud` → **`192.168.193.14`** (external Envoy Gateway LB, since `2121ca476`). Previously `192.168.193.4` (nginx-external LB) — the public IP hairpins and times out either way |
| Data planes | **both live**: OpenCode → nginx-external (`192.168.193.4`); Hermes → external Envoy (`192.168.193.14`). Both are in the CNP allow-list |
| Node | every MCP pod on `prusik`; pod CIDR `10.0.4.0/24`, `cilium_host` `10.0.4.252` |
| Cilium | v1.20.4, `enable-policy: default`, `enable-host-firewall: false`, `allow-localhost` unset |

## 8. Files to Modify

| File | Phase | Change |
|---|---|---|
| `docs/design/mcp-gateway-architecture.md` | 1 | status block, inventory table, policy section |
| `planning/mcp-gateway-implementation.md` | 1 | Task 2/5 status, "still open" list, cross-link |
| `apps/mcp-gateway/values.yaml` | 1 | stale `mcp_grafana` "inert / 404" comment |
| `planning/mcp-canary-*.md`, `apps/mcp-canary/` | 1 | delete or mark superseded |
| `apps/grafana-mcp/values.yaml`, `apps/firecrawl-mcp/values.yaml` | 2 | scrape targets (+ CNP metrics rule if enabled) |
| `test/mcp-gateway/chart-checks.py` | 2 | assert the rendered envoy/nginx `fromEndpoints` selectors match live pod labels (TODO-2.2 step 3) |
| `apps/mcp-gateway*/templates/ciliumnetworkpolicy.yaml` | later | drop the `ingress-nginx` rules only after nginx stops serving these hosts |
| `apps/hermes/values.yaml`, `apps/hermes/templates/*` | 3 | remove sidecar, refresher, init container, `/shared` |
| `test/mcp-gateway/jobs/verify-networkpolicy.yaml`, `verify-foundation-incluster.yaml` | 2 | refresh stale policy references and expectations, then re-run |
| `~/.config/opencode/opencode.json` (not in repo) | 3 | retire bypassing servers one at a time |
| `apps/mcp-gateway/values.yaml` | 4 | add `/firecrawl/mcp` route once both gates clear |
| `apps/mcp-gateway-admin/values.yaml` | 4 | Vault scope, group, route |
| `apps/firecrawl/**` (other workstream) | 4 | fetcher egress policy |
| node pinning / replica values in all four charts | 5 | placement decision |

## 9. Execution Order

```
Phase 1 (docs + cleanup)      ── no dependencies, no live change, do first
Phase 2 (observability, jobs) ── G13 refresh + re-run; G7 scrape targets; G12 audit posture
Phase 3 (consolidation)       ── G1 needs owner approval; G4 needs per-server approval
Phase 4 (backends)            ── each blocked on a named owner decision / gate
Phase 5 (resilience)          ── after Phase 3, so rollback is tested against the final shape
```

Hard ordering rules:

- **G9 is closed, not pending.** Both nginx and Envoy data planes are already allowed by the CNPs and
  both are serving. The only remaining Envoy-related work is *removing* the nginx rules once nginx
  stops serving these hosts (TODO-2.2 step 2) — do not do that early, or the OpenCode path fails closed.
- **G1 before G4's Grafana items.** Removing the Hermes sidecar while OpenCode still has two local
  `mcp-grafana` servers does not reduce the Grafana path count.
- **G2's egress gate before any Firecrawl route.** Gateway-side policy cannot substitute for fetcher
  destination control.
- Never weaken a CNP to make a probe pass. The split-port shape is the only measured-safe option.

## Related

- [`mcp-gateway-implementation.md`](mcp-gateway-implementation.md) — build-out, matrices, acceptance
- [`docs/design/mcp-gateway-architecture.md`](../docs/design/mcp-gateway-architecture.md)
- [`docs/conventions/cilium.md`](../docs/conventions/cilium.md) — kubelet probes under enforced policies
- [`docs/troubleshooting/mcp-firecrawl-egress-regression.md`](../docs/troubleshooting/mcp-firecrawl-egress-regression.md)
- [`docs/conventions/gateway-api-routes.md`](../docs/conventions/gateway-api-routes.md)
