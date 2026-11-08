# Firecrawl egress NetworkPolicies: applied, broke readiness, rolled back

**Status:** rolled back, baseline healthy, **root cause unknown**. The MCP `firecrawl` backend route
stays disabled. No further canary, re-land or live egress test is authorised.

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
   once. Do not "fix" a probe path by adding a blanket host / remote-node allow, and do not trust a
   look-alike cluster that does not match the real routing, kernel and datapath behaviour.
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
