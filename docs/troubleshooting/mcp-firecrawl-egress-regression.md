# Firecrawl egress NetworkPolicies: applied, broke readiness, rolled back

**Status:** rolled back, baseline healthy. The exact packet-level cause of the 2026-11-08 18:53
regression is still **not captured** (no Hubble/packet evidence) and this doc does **not** claim it
was solved. Later the same day the MCP gateway foundation hit the same *symptom* - kubelet probe
timeouts against a healthy process - and a Grafana MCP backend reproduced it on 2026-11-09. Two
empirical matrices run on 2026-11-09 with disposable pods **settled the mechanism for the probe
failures** (see "Measured answer" below, matrix 1 and 2): the break is on the **ingress** side - any
policy set carrying an ingress rule denies the kubelet probe, and on this cluster the probe source is
matchable only as `world`. Both hypotheses this doc published earlier - "a native NetworkPolicy cannot
name the node, so ingress had no permitted source" and the follow-up "the pod→node **response** is
dropped by an enforced egress policy" - are **falsified**; they are kept below as history, labelled as
superseded. The rollback/recovery chronology of the 2026-11-08 events stays **inconclusive**, and the
**18:53 Firecrawl root cause stays UNKNOWN**: it was an egress-only set, and egress-only sets measured
clean, so nothing reproduced explains it. The MCP `firecrawl` backend route remains disabled. The probe
matrix objects in `default` were deleted and the baseline was verified intact.

## Timeline (read-only investigation, 2026-11-08)

| time | observation |
|---|---|
| 18:53:02 | `0b16b31b9` (`firecrawl: Isolate fetcher egress to block SSRF pivots`) deployed by ArgoCD; egress NetworkPolicies created in the Firecrawl namespace |
| 18:53:03 | BPF reload for the selected endpoints |
| 18:53:08–14 | kubelet **probe timeouts** for `api`, `worker` and both `nuq` pods, across two nodes |
| throughout | `browser` (playwright) stayed `Ready` |
| 19:08:11 | scoped revert (`8fff4f224`) synced; the **same** `api` process regained readiness **without a restart**; the new policies absent |
| +30 min | stable baseline, all 7 containers `Ready`; no earlier restarts or node pressure. An exit code `137` with `reason: Error` is **not** OOM evidence |

The change is associated with the probe regression: it starts at policy attach and reverses on policy
delete while the application process keeps running. **What inside the change caused it is not
established.**

## What the change rendered

Three egress-only policies (no `policyTypes: [Ingress]`, so ingress enforcement is not directly
implicated; cluster-wide ingress isolation stays off):

* `fetchers` — selects `api` + `worker` + `nuq-worker`, 5 rules: DNS (node-local + kube-dns),
  in-namespace `redis:6379`, `cluster-name: firecrawl-postgres` on 5432, in-namespace `playwright:3000`,
  public internet 80/443 with 14 `except` CIDRs (RFC1918, `10.0.0.0/8`, CGNAT, link-local, reserved,
  multicast; no IPv6 allowed).
* `search` — api-only, allows the `searxng` namespace (only the API process calls it; `executeSearch`
  is reached from `controllers/v2/search.ts`, not a job worker).
* browser — 2 rules: the `&egressDns` and `&egressInternet` YAML anchors, **byte-identical** to the
  ones the `fetchers` policy uses, including all 14 `except` CIDRs.

So the surviving rendered difference between the workloads that broke and the one that did not is
**extra peers and extra rules on the selected side**, not a missing internet rule on the browser side.
An earlier note here claimed the browser policy lacked the multi-CIDR internet rule and that SearXNG
had no rule at all: both are **wrong** and retracted — the browser shares the same anchors, and SearXNG
is covered by the api-only `search` policy.

## Limits of what was observed

* **Denied counters read `0`.** That is not exoneration: they are cumulative and cannot be attributed
  to a namespace, policy or port range, so a relevant increase could be invisible in an aggregate or in
  events that aged out of the window.
* **"Probe requests timed out" is not a confirmed node↔pod network denial.** No packet-level, eBPF or
  root/exec datapath capture was taken, so the alternative — the request reached the application and
  stalled inside the handler — cannot be separated from a packet-path problem. Logs show the process
  alive with background database work; that is **not** evidence of handler responsiveness or of where a
  packet was lost.
* **No live egress assertion was ever executed.** `apps/firecrawl/hack/verify-egress.sh` was never run,
  so none of the SSRF destinations the deny list targets was demonstrated blocked. The offline render
  test only asserted that `to:` selectors still match the pods the chart renders; it did not model the
  node↔pod kubelet probe path.
* Unranked hypotheses, none of them a conclusion: extra peers/rule count on the selected endpoints, the
  number of probe ports, in-process vs sidecar behaviour, endpoint regeneration on attach, or the
  stale-datapath look-alike in `docs/troubleshooting/cilium-stale-bpf-egress.md` (which does not fit
  cleanly, since readiness returned on policy deletion with no Cilium restart).
* No privileged command, `exec`, canary, new-cluster reproduction or re-land was run.

## Gates before any re-attempt

1. **Capture, don't infer.** During a canary: `cilium-dbg monitor -t deny` on an affected node,
   `cilium-dbg bpf ct list global` for the probe destination, and a capture of the kubelet→pod health
   request. Aggregate counters prove nothing in either direction.
2. **Model the probe path** in tests, not only selector matching, and make any egress test fail loudly
   when the workload is unhealthy for the wrong reason — a dead app otherwise reads as "SSRF blocked".
3. **Canary one workload**, applied out-of-band and watched, rather than selecting every fetcher pod at
   once. The probe path is proven open only by the shape in `docs/conventions/cilium.md` — a dedicated
   health port allowed from `world`, app/metrics ports from `fromEndpoints` — which must be measured on
   this cluster, not assumed from an entity name. A blanket `host`/`remote-node` allow does **not** fix
   probes here (measured), and a look-alike cluster is not evidence.
4. **One concern per commit** — egress isolation shipped together with a public-exposure rework, so a
   regression has more than one candidate.

## Relationship to the MCP gateway

The gateway's `firecrawl` backend route is a separate matter and stays disabled. Gateway-side
NetworkPolicy limits who may reach the MCP pod directly; it does not constrain what the Firecrawl
fetcher reaches, so it cannot substitute for this egress gate. See
`docs/design/mcp-gateway-architecture.md` and `planning/mcp-gateway-implementation.md`.

## History note

`0b16b31b9` also added `.github/workflows/mcp-gateway-e2e.yaml`, `test/mcp-gateway/public_probe.py` and
`test/mcp-gateway/firecrawl-egress-test.py`; the scoped revert removed all six paths, so none of that
tooling is in the tree today. The commits hold the code if it is needed again — reworked against the
gates above, not restored wholesale.

## Second occurrence (same day, MCP gateway foundation): node-sourced dials unanswered

The disabled-only MCP gateway foundation (`9049af3bf`, `apps/mcp-gateway` and
`apps/mcp-gateway-admin`, **every route disabled**) was deployed by ArgoCD at 20:31 and both planes
reached `Degraded` with the identical signature:

* the process was healthy - `agentgateway 1.7.0` logged `marking server ready` about 6 ms after
  start and the readiness listener was established on `[::]:15021`;
* kubelet reported `Startup probe failed: Get "http://<podIP>:15021/healthz/ready": context deadline
  exceeded` on **both** planes, and killed each container at the startup threshold (30 x 10 s =
  ~297 s), so the pods restarted in a loop while never being Ready;
* the probe port was **not** in the chart's ingress allow list at all (ingress permitted only 3000 from
  the ingress controller and 15020 from Prometheus), yet the failures were not limited to it. **The
  rationale written into that chart comment - the port stays unlisted because a policy "cannot name the
  node" - is the claim this doc now retracts**, see below. The chart source itself is owned by the
  gateway owner and its comment still has to be corrected in a future source change.

Measured read-only, same moment, same node, via the API server pod proxy (a host-sourced dial, like
kubelet's):

| dial | pod has NetworkPolicies | pod has none |
|---|---|---|
| listening port | 502 after **6.14 s** (SYN unanswered) | real response in **0.07 s** |
| closed port (`15999` / `19999`) | 502 after **6.14 s** | 502 in **0.07 s** (`connection refused`) |

The numbers stand: for the policy-covered pod **every** port behaved like a silent drop, including a
port with no listener at all, and including ports the policy allowed for specific pod peers; the control
pod was on the **same node and the same pod CIDR** (`10.0.4.222` vs `10.0.4.127`). What the numbers
support is only "**node-sourced traffic was not delivered while this policy set was attached**". The
reading this doc previously published - a "clean A/B" that **demonstrated** a node→pod ingress denial -
is **audit-corrected**: the *reason* given then ("a NetworkPolicy cannot name the node, so the probe path
had no permitted source") is wrong, and the dial alone could not separate directions - Ingress and Egress
were attached together, and it went through the apiserver pod-proxy, so its source identity is unproven.
The 2026-11-09 matrix below reproduces this failure as case B (native NetworkPolicy, **ingress only** →
NotReady), so the **direction** was right after all, for a different reason: on this datapath no ingress
entity except `world` matches the kubelet probe source.

## Third occurrence (2026-11-09, Grafana MCP backend)

`838031375` (`grafana-mcp: Add scoped CiliumNetworkPolicy (post-health)`) attached a **Cilium**-native
policy to a backend that had already been proven healthy without isolation (180 s Ready window
07:04:35Z-07:07:35Z, `restarts=0`). Its ingress rules allowed the node explicitly -
`fromEntities: [host, remote-node]` on the `:8081` healthz listener - and on `:8000` only the gateway
plane pods. Liveness still failed with `context deadline exceeded` (13 times over ~6 min) and the
container restarted 6 times before `1c28157e2` (`grafana-mcp: Roll back backend CNP (kubelet probes
blocked)`) rendered no CNP at all (`networkIsolation: false`); the backend returned to `restarts=0`.

That event killed the "node cannot be named on ingress" story from the second occurrence, because the
allowance had been granted in exactly the form that story said was impossible. The reading published
here on 2026-11-09 - **"the pod→node response is dropped by an enforced egress policy that never names
the node"** - is now **SUPERSEDED / FALSIFIED** by the measurement below. The observations it was built
from stand (egress listed only Grafana pods on 3000 and DNS on 53, no node/host; `enable-host-firewall`
`false`, `enable-policy: default`, `allow-localhost` unset in `cilium-config`;
`calypso-runners/calypso-control-plane-runner-policy` live with `toEntities: [cluster]` egress) — but
the inference drawn from them was wrong: Calypso survives because its **ingress** `fromEntities` list
also contains `world`, verified live, not because of its egress.

## Measured answer (2026-11-09, two matrices on `grigri`)

Cilium `v1.20.4`, `enable-policy: default`, `enable-host-firewall: false`, `allow-localhost` unset.
Disposable pods in `default` with unique labels and one `python http.server` carrying `httpGet`
startup/readiness/liveness on 8080. All objects deleted afterwards; baseline verified intact.

**Matrix 1 - which direction breaks a probe:**

| case | policy attached | result |
|---|---|---|
| A | none | Ready |
| B | native NetworkPolicy, **ingress only** | NotReady, `context deadline exceeded`, +2 restarts; recovered ~8 s after delete |
| C | native NetworkPolicy, **egress only** (DNS) | **Ready**, 83 s clean, 0 restarts |
| D | CNP, egress only, `toEntities: [host, remote-node]` + DNS | **Ready** |
| E1 | CNP ingress `fromEndpoints` + egress `cluster` | NotReady (flapping) |
| E2 | CNP ingress `fromEndpoints` **plus** `fromEntities: [host, remote-node]` on the probe port | NotReady |
| E3 | CNP ingress `fromEntities: [cluster]` | NotReady |
| E4 | CNP ingress `fromCIDR` 192.168.192.0/24 (node subnet) | NotReady |
| E5 | CNP ingress `fromEntities: [host, remote-node, cluster, world]` on the probe port | **Ready**, 115 s clean |
| G1 | CNP ingress `fromEndpoints`, no egress section | NotReady, persistent |

**Matrix 2 - the production split-port shape** (three ports: 3000 app / 15020 metrics / 15021 healthz):

- **S2** — ingress `fromEntities: [world]` restricted to 15021 (+ a second health port 8081) and
  `fromEndpoints` for 3000 / 15020 / 8000; egress `toEntities: [cluster]`: server **Ready, zero probe
  failures**; a non-permitted pod was DENIED on 3000 / 15020 / 8000 while permitted pods were allowed.
- **S3** — the same CNP applied **6 m 32 s before** the endpoint was recreated: Ready in 6 s, 0 probe
  failures. Ordering (policy before workload) is safe.
- **S5** — S2 minus the `world` rule, everything else identical: NotReady, first failure 6 s after
  attach, container killed, +1 restart. Restoring `world` returned it to Ready and all assertions were
  re-established.

**What is now established:**

1. The **egress-return hypothesis is falsified**. Egress-only policy sets never broke a probe (C, D);
   ingress is stateful and probe replies cross it without trouble.
2. The break is **ingress-side only**: any policy set carrying an ingress rule on an `httpGet`/
   `tcpSocket`-probed pod denies the kubelet probe - native NetworkPolicy and CNP alike
   (B, E1, E2, E3, E4, G1).
3. With `enable-host-firewall: false`, `host`, `remote-node`, `cluster` and a node-subnet `fromCIDR`
   **all fail to match the kubelet probe source**; the path is indistinguishable from `world`. The cure
   recommended earlier here - `fromEntities`/`toEntities: [host, remote-node]` - is wrong and
   insufficient (E2).
4. **Safe shape** (S2/S3/S5): a **dedicated** health/probe port allowed by `fromEntities: [world]` and
   nothing else in that rule; app + metrics ports from `fromEndpoints`; egress `toEntities: [cluster]`.
   Keep `cluster` off the health port - in E5 it shared the rule list and leaked the app port. Residual
   exposure is that `world` opens only that dedicated health port to anything routable to the pod IP
   (health body only). Owner alternatives if that is unacceptable: `exec` probes (immune by
   construction), or `enable-host-firewall=true` - cluster-wide, **untested here**, and the only route
   that would make `host`/`remote-node` matching meaningful.
5. **Native NetworkPolicy cannot express this** (no entities, and the node `ipBlock` does not match), so
   any native NP with `policyTypes: [Ingress]` on a probed pod will break it.
6. Operationally: attach the CNP at or before the workload (S3) and keep `failureThreshold >= 6`.

**What remains unknown:** the 18:53 Firecrawl event. It was `policyTypes: [Egress]` only and every
egress-only case above measured clean, so **no reproduced behaviour explains it** - root cause stays
**UNKNOWN**, and it must not be re-attributed to ingress or to egress-return. The 2026-11-08
gateway/grafana-mcp rollback chronology also stays inconclusive: recovery was not isolated to policy
removal alone. `browser` (playwright) staying Ready with the same egress anchors is still unexplained.

**Consequence for any re-land:** use the split-port shape in `docs/conventions/cilium.md` and prove it
with the bounded disposable dial on this cluster before attaching it to anything that probes with
`httpGet`/`tcpSocket`. A pod whose only probes are `exec` (like the Firecrawl redis) is unaffected by
construction, which is worth checking before assuming a policy set is "safe for probes".
