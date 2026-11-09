# Firecrawl egress NetworkPolicies: applied, broke readiness, rolled back

**Status:** rolled back, baseline healthy. The exact packet-level cause of the 2026-11-08 18:53
regression is still **not captured** (no Hubble/packet evidence) and this doc does **not** claim it
was solved. Later the same day the MCP gateway foundation hit the same *symptom* - kubelet probe
timeouts against a healthy process - and on 2026-11-09 a Grafana MCP backend reproduced it again with a
*different* policy shape, which invalidated the explanation this doc previously gave for it. The
mechanism is now treated as a **probable hypothesis, not a demonstrated cause**: the missing node/host
allowance was on the **egress** side, so the probe reply pod→node is the candidate drop point. An
empirical matrix (disposable probe pods plus per-policy-mode CNPs) is running as this is written; until
it reports, nothing here is settled and the rollback/recovery chronology stays **inconclusive**. See
"Second occurrence" and "Third occurrence" below; the Firecrawl cause stays unknown. The MCP
`firecrawl` backend route remains disabled and no further canary, re-land or live egress test is
authorised beyond the probe matrix already in flight.

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
   once. Express the node path in **both** directions (probe ingress *and* pod→node egress) and measure
   it, rather than guessing at one leg; keep any such allowance scoped to the probe port instead of
   adding a blanket host / remote-node allow, and do not trust a look-alike cluster that does not match
   the real routing, kernel and datapath behaviour.
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
is **audit-corrected to inconclusive**: Ingress and Egress policies were attached together, so the pair
cannot separate the two directions; the dial went through the apiserver pod-proxy, so the source
identity through the K3s proxy is unproven; and a timeout is not by itself a dropped request.

## Third occurrence (2026-11-09, Grafana MCP backend): the ingress explanation fails

`838031375` (`grafana-mcp: Add scoped CiliumNetworkPolicy (post-health)`) attached a **Cilium**-native
policy to a backend that had already been proven healthy without isolation (180 s Ready window
07:04:35Z-07:07:35Z, `restarts=0`). Its ingress rules allowed the node explicitly -
`fromEntities: [host, remote-node]` on the `:8081` healthz listener - and on `:8000` only the gateway
plane pods. Liveness still failed with `context deadline exceeded` (13 times over ~6 min) and the
container restarted 6 times before `1c28157e2` (`grafana-mcp: Roll back backend CNP (kubelet probes
blocked)`) rendered no CNP at all (`networkIsolation: false`); the backend returned to `restarts=0`.

That is the evidence that breaks the earlier story, and it points the other way:

* the **ingress** side of the node path was explicitly allowed, in the exact form the previous text
  said was "impossible to express", and the probes still timed out;
* its **egress** rules listed only the Grafana pods on 3000 and the DNS backends on 53 - **no node or
  host allowance at all**;
* per Cilium docs, local host→pod traffic is allowed **implicitly** unless the agent runs
  `--allow-localhost=policy`. Measured live in `cilium-config`: `allow-localhost` unset,
  `enable-host-firewall: false`, `enable-policy: default`. So an ingress omission on the node path is a
  weak explanation here, and upstream cilium/cilium#21965 (host egress policy denying kubelet probes)
  needs `enable-host-firewall: true`, which is **not** this cluster's configuration.
* the long-running, working reference in this cluster is
  `calypso-runners/calypso-control-plane-runner-policy` (live, `VALID=True`): its ingress allows
  `fromEntities: [host, remote-node, cluster, world]` on the served ports **and** its egress allows
  `toEntities: [cluster]` (port-scoped), where `cluster` is documented to include `host` and
  `remote-node`. The two failing policies each lacked one of those legs.

**Probable mechanism (hypothesis, not proven)**: with an enforced egress policy that never names the
node/host, the **pod→node response** of a kubelet probe is dropped, so the probe times out even though
the request reached the pod. The durable rule this implies is recorded in
`docs/conventions/cilium.md` ("Kubelet probes under enforced policies").

**Still open**: which leg actually drops the packet, and whether a port-scoped egress allowance to the
node is sufficient. An empirical matrix of disposable probe pods with per-policy-mode CNPs is running;
until it reports, the wording above stays probable rather than proven, and the rollback/recovery
chronology of the earlier events stays inconclusive.

**What the second and third occurrences do not prove**: nothing about the 18:53 Firecrawl event. The
Firecrawl policies were `policyTypes: [Egress]` only - consistent with the egress/return hypothesis, but
the browser (playwright) pod stayed Ready with the same egress anchors and the node subnet excluded, so
a generic "egress policy drops the probe reply" story does not fit that event either. The 18:53 root
cause stays **unknown**; only the symptom class is shared - kubelet probe timeouts against a healthy
process, recovered by deleting the policies without a restart.

**Consequence for any re-land**: a pod that runs kubelet `httpGet`/`tcpSocket` probes must have the node
path proven open in **both directions** - probe ingress *and* pod→node egress - **before** the policy set
is attached, and the proof must be measured, not inferred from a render. A native NetworkPolicy cannot
name a node peer, but that is an API limitation only: Cilium expresses it (`fromEntities` /
`toEntities` with `host`, `remote-node`, or a scoped `cluster`), so the node path is never a valid
excuse for omitting one direction. Keep such allowances scoped to the probe port, and prefer a validated
shape copied from a workload that demonstrably runs policy-ed in this cluster. A pod whose only probes
are `exec` (like the Firecrawl redis) is unaffected by construction, which is worth checking before
assuming a policy set is "safe for probes".
