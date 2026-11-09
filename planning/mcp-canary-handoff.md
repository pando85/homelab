# MCP Canary Diagnostic: Implementation-Ready Handoff

## Purpose
Isolate which NetworkPolicy type (ingress vs egress) caused kubelet startup-probe timeouts on prusik for the MCP gateway foundation pods. The canary is a faithful agentgateway v1.7.0 pod with no identity, no backends, no routes, no Ingress - only the listening shape and the policy.

## Status: IMPLEMENTATION-READY

All local tests pass. No commits, no live changes, no foundation file modifications beyond nodeSelector pinning.

## Files Created

### Chart Structure
```
apps/mcp-canary/
├── Chart.yaml                    # v0.1.0, appVersion 1.7.0, app-template 5.3.0
├── values.yaml                   # policyMode, rolloutNonce, nodeSelector, probes
├── charts/
│   └── app-template-5.3.0.tgz   # Vendored, gitignored
├── Chart.lock                    # Pinned dependency digest
└── templates/
    ├── configmap.yaml            # agentgateway config (adminAddr off, routes: [])
    └── networkpolicy.yaml        # Conditional: none/ingress/egress
```

### Test Script
```
apps/mcp-canary/tests/render-tests.sh   # Static render tests (all pass)
```

## Key Design Decisions

### 1. No Session Key Required
- `adminAddr: "off"` in config → agentgateway runs without SESSION_KEY
- No ExternalSecret, no Password generator, no ESO dependency
- Simpler than foundation (which requires ESO CreatedOnce for session key)

### 2. No Identity/Backends/Routes
- No KanidmOAuth2Client, no KanidmGroup
- No Ingress object
- No backend peers in NetworkPolicy
- `routes: []` in config → every request answers 404
- Proves only: pod can start, probes can pass, policy behavior

### 3. NodeSelector Pinned to prusik
- All three charts (canary + both foundation) pinned to `kubernetes.io/hostname: prusik`
- Trade-off accepted: single-node failure domain for diagnosis/initial rollout
- Provisional; revisit after datapath gate

### 4. Rollout Nonce Annotation
- `mcp-canary.grigri.cloud/rollout-nonce` pod annotation
- Bumping it recreates the pod, so NetworkPolicy is present before first probe
- Critical: applying NP to a running pod does not reproduce startup timeout

### 5. NetworkPolicy Modes
- `policyMode: none` → zero NetworkPolicy objects (baseline)
- `policyMode: ingress` → one ingress-only NP (TCP3000 from ingress-nginx-external, TCP15020 from monitoring/prometheus; NO 15021)
- `policyMode: egress` → one egress-only NP (DNS UDP+TCP53, TCP443 to ingress-nginx-external, TCP8443 to kanidm; no backend peers)
- One policy type per mode, never both

### 6. Probes Mirror Foundation
- startup: `/healthz/ready` port 15021, failureThreshold 30, periodSeconds 10
- readiness: same, failureThreshold 3
- liveness: same, failureThreshold 3
- Total startup window: 300s (30 × 10s)

## Local Test Results

### Render Tests (all pass)
```
=== Test: policyMode=none renders no NetworkPolicy ===
PASS

=== Test: policyMode=ingress renders one ingress NetworkPolicy ===
PASS

=== Test: policyMode=egress renders one egress NetworkPolicy ===
PASS

=== Test: rolloutNonce annotation is present ===
PASS

=== Test: no identity resources in any mode ===
PASS

=== Test: no backend routes in any mode ===
PASS

=== All tests passed ===
```

### Helm Lint
```
==> Linting apps/mcp-canary
[INFO] Chart.yaml: icon is recommended
1 chart(s) linted, 0 chart(s) failed
```

### NodeSelector Confirmation
```
apps/mcp-canary/values.yaml:
  nodeSelector:
    kubernetes.io/hostname: prusik

apps/mcp-gateway/values.yaml (untracked, frozen):
  nodeSelector:
    kubernetes.io/hostname: prusik

apps/mcp-gateway-admin/values.yaml (untracked, frozen):
  nodeSelector:
    kubernetes.io/hostname: prusik
```

## Execution Plan (NOT RUN YET)

### Phase 0: Baseline (policyMode=none)
```bash
# Commit canary chart with policyMode=none, rolloutNonce=0
git add apps/mcp-canary/
git commit -m "mcp-canary: Add diagnostic canary chart (policyMode=none)"
git push

# ArgoCD syncs, creates namespace mcp-canary, deploys pod
kubectl --context=grigri get pods -n mcp-canary -w

# Wait for Ready <= 180s
kubectl --context=grigri wait --for=condition=Ready pod -l app.kubernetes.io/name=mcp-canary -n mcp-canary --timeout=180s

# If Ready: baseline passes, proceed to Phase 1
# If NotReady: record events, logs, STOP (baseline failure is a confound)
kubectl --context=grigri describe pod -l app.kubernetes.io/name=mcp-canary -n mcp-canary
kubectl --context=grigri logs -l app.kubernetes.io/name=mcp-canary -n mcp-canary --tail=50
```

**Expected**: pod Ready within 180s (no policy, no confound)

### Phase 1: Ingress-Only (policyMode=ingress, rolloutNonce=1)
```bash
# Bump nonce + flip mode
# Edit apps/mcp-canary/values.yaml:
#   policyMode: ingress
#   rolloutNonce: "1"

git add apps/mcp-canary/values.yaml
git commit -m "mcp-canary: Phase 1 ingress-only diagnostic"
git push

# ArgoCD syncs, pod recreated with ingress NP present
kubectl --context=grigri wait --for=condition=Ready pod -l app.kubernetes.io/name=mcp-canary -n mcp-canary --timeout=360s

# If Ready <= 360s: ingress passes, proceed to Phase 2
# If NotReady: record exact failure, then ROLLBACK
```

**If FAIL**:
```bash
# Record observations
kubectl --context=grigri get pod -l app.kubernetes.io/name=mcp-canary -n mcp-canary -o wide
kubectl --context=grigri describe pod -l app.kubernetes.io/name=mcp-canary -n mcp-canary
kubectl --context=grigri logs -l app.kubernetes.io/name=mcp-canary -n mcp-canary --tail=100

# Immediate rollback: policyMode=none, rolloutNonce=2
# Edit apps/mcp-canary/values.yaml:
#   policyMode: none
#   rolloutNonce: "2"

git add apps/mcp-canary/values.yaml
git commit -m "mcp-canary: ROLLBACK ingress failed, baseline restored"
git push

# Confirm Ready
kubectl --context=grigri wait --for=condition=Ready pod -l app.kubernetes.io/name=mcp-canary -n mcp-canary --timeout=180s

# STOP and report: ingress-only causes startup timeout
```

**Expected**: ingress-only may cause timeout (reproduces foundation issue)

### Phase 2: Egress-Only (policyMode=egress, rolloutNonce=3)
```bash
# Only reached if Phase 1 passes
# Edit apps/mcp-canary/values.yaml:
#   policyMode: egress
#   rolloutNonce: "3"

git add apps/mcp-canary/values.yaml
git commit -m "mcp-canary: Phase 2 egress-only diagnostic"
git push

# ArgoCD syncs, pod recreated with egress NP present
kubectl --context=grigri wait --for=condition=Ready pod -l app.kubernetes.io/name=mcp-canary -n mcp-canary --timeout=360s

# If Ready <= 360s: egress passes
# If NotReady: record exact failure, then ROLLBACK
```

**If FAIL**:
```bash
# Record observations (same as Phase 1)
kubectl --context=grigri get pod -l app.kubernetes.io/name=mcp-canary -n mcp-canary -o wide
kubectl --context=grigri describe pod -l app.kubernetes.io/name=mcp-canary -n mcp-canary
kubectl --context=grigri logs -l app.kubernetes.io/name=mcp-canary -n mcp-canary --tail=100

# Immediate rollback: policyMode=none, rolloutNonce=4
# Edit apps/mcp-canary/values.yaml:
#   policyMode: none
#   rolloutNonce: "4"

git add apps/mcp-canary/values.yaml
git commit -m "mcp-canary: ROLLBACK egress failed, baseline restored"
git push

# Confirm Ready
kubectl --context=grigri wait --for=condition=Ready pod -l app.kubernetes.io/name=mcp-canary -n mcp-canary --timeout=180s

# STOP and report: egress-only causes startup timeout
```

**Expected**: egress-only may cause timeout (reproduces foundation issue)

### Phase 3: Cleanup
```bash
# If both pass: no reproduction found
# If either fails: reproduction found, mechanism still unknown

# Commit deletion of canary chart
git rm -r apps/mcp-canary/
git commit -m "mcp-canary: Remove diagnostic canary (Argo prune)"
git push

# ArgoCD prunes namespace mcp-canary (may remain empty, harmless)
# Verify resources gone
kubectl --context=grigri get all -n mcp-canary
# Expected: No resources found (or namespace empty)

# Verify other apps still Healthy
kubectl --context=grigri get applications -n argocd
```

## Observations to Record

For each phase, if pod does not become Ready within timeout:

1. **Pod status**: `kubectl get pod -o wide` (node, IP, restarts, age)
2. **Events**: `kubectl describe pod` (reason, message, deadline vs refused)
3. **Restart counts**: `kubectl get pod -o jsonpath='{.status.containerStatuses[0].restartCount}'`
4. **Logs**: `kubectl logs --tail=100` (bounded, no exec/escalation)
5. **NetworkPolicy**: `kubectl get networkpolicy -n mcp-canary -o yaml` (verify correct mode)

## Confounds / Blockers

### None Identified
- No identity/backends/routes → no auth surface, no backend connectivity needed
- No session key → no ESO dependency, no secret material
- Pinned to prusik → same node as foundation, same datapath
- Probes mirror foundation → same failure mode if policy blocks kubelet

### Known Unknowns
- Mechanism: if ingress/egress causes timeout, is it Cilium BPF, kubelet probe path, or reply traffic?
- Stale BPF: if canary reproduces, does Cilium agent restart fix it? (NOT authorized in this task)
- Foundation re-land: if canary passes both, does foundation still fail? (separate task)

## What This Does NOT Do

- ❌ No Cilium restart
- ❌ No exec/escalation
- ❌ No privileged capture (tcpdump, cilium monitor)
- ❌ No direct kubectl apply/delete/patch (only read-only get/describe/logs/wait/watch)
- ❌ No identity/backend route implementation
- ❌ No foundation chart restoration (they stay untracked/frozen)
- ❌ No claim of "staleBPF" or mechanism (only reproduction)

## Foundation Charts: Untracked, Frozen

Both `apps/mcp-gateway/values.yaml` and `apps/mcp-gateway-admin/values.yaml` are:
- Untracked (not in git HEAD after revert 0ef0b4dad)
- Byte-identical to 9049af3bf except for the two-line nodeSelector addition
- Frozen for later re-land review
- No commits, no push, no restore of tracked files

**Exact diff** (both charts identical):
```diff
       pod:
         enableServiceLinks: false
         automountServiceAccountToken: false
+        nodeSelector:
+          kubernetes.io/hostname: prusik
       containers:
```

## Product Probes: Frozen

All product probes remain frozen:
- `test/mcp-gateway/vault-official-stateless-probe.py` (28/28 REQUIRED, 4/4 CONTROL, exit 0)
- `test/mcp-gateway/grafana-merged-keyset-probe.py` (9/9 SECURITY, exit 2 = limitation)
- `test/mcp-gateway/grafana-sub-alias-probe.py` (17 PASS, 1 hazard FAIL, exit 1)
- No changes, no new runs, no new investigations

## Deferred Items

- **Vault**: no candidate implementation/conversion route work
- **Hermes**: keep working, existing Grafana sidecar and gateway OFF
- **Grafana**: explore agent assigned READONLY reference compare + single JWT client migration plan; DO NOT duplicate/investigate reference or implement Grafana changes yet

## Handoff Checklist

- [x] Chart created with all required files
- [x] Local render tests pass (all 6 test groups)
- [x] Helm lint passes
- [x] NodeSelector pinned to prusik in all three charts
- [x] No identity/backends/routes in canary
- [x] Execution plan documented with exact commands
- [x] Rollback plan documented for each phase
- [x] Observations to record documented
- [x] Confounds/blockers documented
- [x] Foundation charts frozen with exact diff documented
- [x] Product probes frozen
- [x] Deferred items documented
- [ ] **NOT YET**: Commit canary chart
- [ ] **NOT YET**: Push to remote
- [ ] **NOT YET**: Execute Phase 0 (baseline)
- [ ] **NOT YET**: Execute Phase 1 (ingress) if baseline passes
- [ ] **NOT YET**: Execute Phase 2 (egress) if ingress passes
- [ ] **NOT YET**: Execute Phase 3 (cleanup)

## Next Action

**Awaiting independent review before any commit/push/live execution.**

Reviewer should verify:
1. Chart structure matches requirements (no identity/backends/routes)
2. Render tests cover all assertions (NP count/type/rules/ports/peers, no identity, no backend, image/tag/nodeSelector/ports, deterministic, nonce annotation)
3. Execution plan is safe (read-only kubectl, scoped rollback, no escalation)
4. Foundation charts remain frozen (untracked, only nodeSelector added)
5. Product probes remain frozen (no changes, no new runs)

Once approved, execute Phase 0 → Phase 1 → Phase 2 → Phase 3 in sequence, with immediate rollback on first failure.
