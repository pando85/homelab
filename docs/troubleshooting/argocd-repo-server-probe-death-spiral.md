# ArgoCD Repo-Server Probe Death-Spiral

## Problem

After a Helm chart upgrade, node restart, or cache loss, `argocd-repo-server` enters
`CrashLoopBackOff`. All ArgoCD-managed applications go `Unknown`, and the `argocd` Application
itself cannot self-heal.

Symptoms:

- `argocd-repo-server-*` restart count climbing every ~30-40s
- `kubectl get app -A` shows all applications `Unknown`
- repo-server gRPC port (8081) binds successfully, but the metrics/health port (8084) is never
  reachable before the pod is killed
- `Last State` shows the liveness probe as the termination reason

## Root Cause

The default liveness/readiness probes in the `argo-cd` Helm chart use a **1-second timeout** with
`failureThreshold: 3` and `periodSeconds: 10`. This gives the pod only ~30 seconds before kubelet
kills it.

repo-server has a **slow cold start**: on first launch (or after cache loss) it must initialize
the Git repo cache, run GC, and bind the gRPC server (8081) before the metrics/health HTTP server
(8084). On a busy node or with a large repo cache, binding 8084 can take 60-120+ seconds.

The probes target 8084 (`/healthz`). When the 1s-timeout probe fires before 8084 is bound, it
fails 3 times within 30s → kubelet kills the container → restart → 8084 never gets a chance to
bind → **restart loop that never converges**. The probe kills the pod faster than the app can
reach steady state.

Evidence that confirms a death-spiral (vs an actual app failure):

- gRPC port 8081 is healthy (logs show "ArgoCD repo server starting")
- Prometheus can scrape 8084 `/metrics` once the pod survives long enough to bind it
- A test pod can `curl http://<repo-server-svc>:8084/healthz` → HTTP 200 in <30ms
- Disabling probes makes the pod go `1/1` within ~2 minutes

## How to Diagnose

```bash
# Restart count climbing every ~30-40s = death spiral
kubectl --context=grigri get pods -n argocd -l app.kubernetes.io/name=argocd-repo-server -w

# Check last termination — look for liveness probe as the killer
kubectl --context=grigri get pod -n argocd <repo-server-pod> -o jsonpath='{.status.containerStatuses[0].lastState}'

# Confirm gRPC (8081) started but the pod was killed before 8084 bound
kubectl --context=grigri logs -n argocd <repo-server-pod> | grep -iE "grpc|server.*start"

# Check the probe config — timeoutSeconds of 1 is the problem
kubectl --context=grigri get deploy -n argocd argocd-repo-server \
  -o jsonpath='{.spec.template.spec.containers[0].livenessProbe}'
```

## Fix

Use a `startupProbe` (supported in the `argo-cd` chart >= 10.0) with a generous window. The
startup probe lets the pod take up to `failureThreshold × periodSeconds` to start before
liveness/readiness probes take over:

```yaml
repoServer:
  startupProbe:
    enabled: true
    timeoutSeconds: 5
  livenessProbe:
    timeoutSeconds: 5
    failureThreshold: 5
  readinessProbe:
    timeoutSeconds: 5
    failureThreshold: 5
```

With `startupProbe` default `failureThreshold: 20` and `periodSeconds: 10`, the pod gets a
**200-second window** to bind 8084 before liveness can kill it. The `timeoutSeconds: 5` (vs
default 1) also prevents spurious probe failures under CPU contention.

## Self-Heal Deadlock (Recovery)

When a chart upgrade *caused* the death-spiral, ArgoCD enters a deadlock: repo-server is down, so
it cannot sync the `argocd` Application to apply the fix. Recovery requires applying the fixed
chart out-of-band:

```bash
cd bootstrap && make argocd
```

This runs `kubectl kustomize --enable-helm` against the remote Helm chart and applies it directly.
After repo-server recovers, ArgoCD picks up the git revision and resumes self-management:

```bash
kubectl --context=grigri get application argocd -n argocd \
  -o custom-columns=SYNC:.status.sync.status,HEALTH:.status.health.status,REV:.status.sync.revision
```

## NetworkPolicy Caveat

The `argo-cd` chart >= 10.0 defaults `global.networkPolicy.create: true`. Enabling per-component
NetworkPolicy interacts badly with a **stale Cilium BPF datapath** on a recently-rebooted node:
kubelet health probes (host→pod) get dropped while in-cluster traffic works. Keep
`networkPolicy.create: false` until the Cilium issue is resolved. See
`docs/troubleshooting/cilium-stale-bpf-egress.md`.

## Makefile / Kustomize Note

The `bootstrap/argocd` directory uses a **remote Helm chart via kustomize** (no local
`Chart.yaml`). The Makefile target runs `kubectl kustomize --enable-helm` with the chart fetched
remotely. Do not add a `helm dependency build` step — there is no local chart to build dependencies
for, and a Makefile rule referencing `argocd/Chart.yaml` is always wrong.
