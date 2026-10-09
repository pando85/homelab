# Staging Continuous Profiling: Collector Choice and Rollback

Covers the `system/alloy` (active) and `system/otel-ebpf-profiler` (blocked, kept inert)
profiling collectors, the `system/pyroscope` backend, and the monitoring surface that
observes them.

## Current state

| Piece | State | Why |
|---|---|---|
| `system/alloy` | **ACTIVE** — Alloy 1.20.1 (chart 1.13.1), `pyroscope.ebpf` → `pyroscope.write` over Pyroscope's **native `/ingest`** | Validated end-to-end on grigri, prusik and k8s-amd64-1 |
| `system/otel-ebpf-profiler` | **INERT** — in the ApplicationSet `excludes` list, manifests retained in git | OTLP profiles path is blocked upstream (see below). Not the rollback target |
| `system/pyroscope` | ACTIVE — v2 single-binary, filesystem backend, `retention_period: 1w` (168h) | Shared backend for whichever collector is active |

## Why OTel-first is blocked (do not "roll back" to it)

The OTel eBPF profiler (`otel/opentelemetry-collector-ebpf-profiler:0.162.0`) emits an OTLP
resource attribute with an **empty key**. Pyroscope's OTLP ingest
(`POST /v1development/profiles`) copies resource attribute keys straight into Prometheus-style
label names without sanitising, so every profile is rejected with
`HTTP 400 invalid label name ''`.

- Transport works; the data is invalid. Not fixable from our config: no collector knob can
  target an empty key, no newer profiler or Pyroscope release fixes it, and Pyroscope's
  `ingestion_relabeling_rules` do not apply to the OTLP profiles path.
- Alloy is structurally immune: labels come from Alloy's own `discovery.relabel` output and
  travel over the pprof-based native `/ingest` path, so the OTLP attribute→label copy never runs.
- Full analysis: `research_notes/Calypso OTel profiling feasibility/empty-label-verdict.md`.

**Therefore rollback of the profiling stack means "stop collecting", not "go back to OTel".**
Both collectors are switched off by excluding both directories.

## Rollback: stop all profiling (git only, no kubectl)

Both collectors are children of the `system` ApplicationSet, so removing a directory from
discovery prunes its Application and everything it owns. Edit `bootstrap/root/values.yaml`:

```yaml
  - name: system
    excludes:
      - alloy                # stops eBPF collection on every node
      - otel-ebpf-profiler   # already excluded; keep it excluded
```

```bash
git add bootstrap/root/values.yaml
git commit -m "fix(monitoring): Exclude alloy profiler to stop eBPF collection"
git push origin master
```

ArgoCD removes `alloy` from the ApplicationSet's generated list, deletes the `alloy`
Application, and prunes its DaemonSet, Service, ServiceMonitor, ConfigMap, ServiceAccount and
ClusterRole/ClusterRoleBinding. Nothing to do on the cluster.

Verify it stopped:

```bash
kubectl --context=grigri -n argocd get application alloy            # NotFound
kubectl --context=grigri -n alloy get pods,ds,svc,sm 2>&1           # No resources / not found
# Ingestion must go flat:
#   sum(rate(pyroscope_distributor_received_compressed_bytes_count{type="process_cpu"}[5m])) == 0
```

To stop only one node's collection instead of the whole stack, narrow the DaemonSet with
`controller.nodeSelector` or add a `controller.tolerations`/affinity change in
`system/alloy/values.yaml` and push — again git only.

## What rollback does NOT do: profile data

Deleting the `pyroscope` Application prunes the PVC, but **do not treat `reclaimPolicy: Retain`
as a guarantee that data survives**. Retain protects the *volume* from the provisioner; it does
not protect against a `kubectl delete pv`, a pool/dataset destruction, a restore that overwrites
it, or a human pruning the orphaned PV later. The PV is left in `Released` and is invisible to
the next PVC until someone reclaims it by hand.

Also note the PVC carries `argocd.argoproj.io/sync-options: Prune=false`, so an ArgoCD prune of
the `pyroscope` app leaves the PVC in place deliberately.

Consequences:

- Stopping **collection** (exclude `alloy`) never touches stored profiles.
- Deleting the **pyroscope app** leaves a `Released` PV holding data that nothing reads, and a
  re-created PVC will not rebind to it automatically. Treat that as data loss unless the PV is
  deliberately re-bound.
- Before any destructive step, confirm what is actually on the dataset rather than inferring
  it from the storage class policy:

```bash
kubectl --context=grigri -n pyroscope get pvc data-pyroscope-0 \
  -o jsonpath='{.spec.volumeName} {.spec.storageClassName}{"\n"}'
kubectl --context=grigri get pv <PV> \
  -o jsonpath='{.spec.persistentVolumeReclaimPolicy} {.status.phase}{"\n"}'
```

Retention is `168h`, enforced by the compaction worker, so old data ages out on its own; there
is rarely a reason to delete the volume manually.

## Verifying after any collector change

Read-only checks, all from git-applied state:

```bash
# 1. App health
kubectl --context=grigri -n argocd get application alloy pyroscope

# 2. Per-node component health (expect unhealthy: 0 on every node)
for p in $(kubectl --context=grigri -n alloy get pods -o name | cut -d/ -f2); do
  kubectl --context=grigri -n alloy port-forward pod/$p 13099:12345 >/dev/null 2>&1 &
  sleep 2
  curl -s --max-time 5 "http://localhost:13099/api/v0/web/components" \
    | python3 -c 'import json,sys;cs=json.load(sys.stdin);print([c["localID"] for c in cs if c["health"]["state"]!="healthy"] or "healthy")'
  kill %1 2>/dev/null
done

# 3. Coverage and volume, per node (Prometheus, job=alloy)
#    pyroscope_ebpf_active_targets / pyroscope_ebpf_profiling_sessions_failing_total
#    pyroscope_ebpf_pprofs_total{service_name=...} vs pyroscope_write_sent_profiles_total
```

Confirm the eBPF programs actually loaded, per node:

```bash
kubectl --context=grigri -n alloy logs <pod> -c alloy | grep -E "eBPF tracer loaded|Attached tracer program|failed to load"
```

## Known limitation: newest-kernel interpreter unwinders

On kernel 7.0.0-34-generic (k8s-amd64-1, Ubuntu 26.04) the `perf_unwind_python` program fails
to load, which made the whole `pyroscope.ebpf` component exit and lose all coverage on that
node. Mitigation is the documented per-interpreter flag in `system/alloy/values.yaml`:

```alloy
pyroscope.ebpf "profiles" {
  python_enabled = false   # targets are Rust binaries, served by the native unwinder
  ...
}
```

This is deliberately narrow: it drops Python stack unwinding only, not native/CPU profiling, and
changes nothing host-wide. If a future kernel breaks another interpreter unwinder, disable that
one flag the same way. Do not reach for `no_kernel_version_check` — it skips the safety check
rather than removing the failing program, and can yield incomplete profiles.

Because interpreter flags are read when the component is built, a ConfigMap reload is **not**
enough after changing them: the pods must be recreated (delete the pods and let the DaemonSet
recreate them, or bump the DaemonSet pod template in git).

## Cardinality and privacy guardrails

Deliberately **not** emitted, and pinned so a chart/app bump cannot quietly start emitting them:

- `pid_label = false` — no process PID label
- `comm = "none"` — no process command-name label
- no `pod` label in `discovery.relabel` output (a rollout changes the pod name every deploy,
  which would multiply series without adding signal); attribution uses `namespace` + `node`
- no `labelmap` of arbitrary pod labels

`service_name` is `ebpf/<namespace>/<container_name>`, e.g. `ebpf/calypso/control-plane`,
`ebpf/calypso/auth-proxy`, `ebpf/calypso/git-proxy`.

Targets are restricted to containers named `control-plane|git-proxy|auth-proxy` inside namespace
`calypso` (exact match) on the node the DaemonSet pod runs on. gVisor runner pods live in
`calypso-runners`, which the exact namespace match excludes, and their container names would not
pass the keep rule either — they are not profiled.

## Profiling scope: where git-proxy actually runs

Determined from cluster state (read-only), not from assumptions:

- git-proxy is a **standalone single-container pod** in namespace `calypso`, created by
  `calypso-control-plane` per runner session as a `gp-<name>-<id>` Deployment plus a matching
  `gp-<name>` Service (port 8443/https). `kube_pod_container_info{namespace="calypso",
  pod=~"gp-.*"}` shows `container="git-proxy"` for 12+ distinct pods over 24h, and its own
  scrape job is `calypso/calypso-git-proxy` with `container="git-proxy"`.
- The gVisor runner pod is a **separate** object in namespace `calypso-runners`.
- Therefore git-proxy runs under the default (runc) runtime and **is** host-profileable, so the
  Alloy keep rule (`namespace=calypso` + container `git-proxy`) matches it correctly. **No config
  change is needed or warranted.**
- `ebpf/calypso/git-proxy` was absent from the profile stream purely because `gp-*` pods are
  ephemeral: every inspection window happened to have zero of them running (they are created with
  a runner and deleted with it), and git-proxy is a low-CPU MITM proxy, so even while alive it
  can sit below the sampler's per-interval sample threshold.

Net effect on the claim "profiles the 3 Calypso services": scope is correct as configured, but
**observed** coverage so far is `control-plane` and `auth-proxy`; `git-proxy` is matched-by-config,
not yet matched-by-observation. To close it, catch a window where a runner session is live.

## Scraped self-metrics job names

kube-prometheus-operator derives the `job` label from the matched **Service** name, not the
ServiceMonitor name. So the profiles-adjacent scrape jobs are:

| ServiceMonitor | actual `job` label |
|---|---|
| `alloy/alloy` | `alloy` |
| `pyroscope/pyroscope` | `pyroscope` |
| `tempo/tempo` | `tempo` |
| `otel-collector/otel-collector` | `otel-collector-opentelemetry-collector` |

Querying `up{job="otel-collector"}` returns nothing even when the target is up — use the Service
name. Tempo's and the collector's endpoints were inert until `b2cc15fb` (wrong ServiceMonitor port
name, and `ports.metrics` left disabled); a ServiceMonitor whose endpoint port matches no Service
port yields **zero targets silently** — no error, no `up` series at all.


## Alerts

`system/pyroscope/resources/prometheus-rules.yaml` (labels `release: monitoring`, required by the
Prometheus CR's `ruleSelector`). Every metric name in it was read off the live endpoints; the
earlier speculative `pyroscope_ingester_push_errors_total` (a metric that does not exist) was
replaced with `pyroscope_discarded_samples_total`. `PyroscopeDiscardingProfiles` firing with
`reason="invalid_labels"` is the signature of the OTLP label bug reappearing.
