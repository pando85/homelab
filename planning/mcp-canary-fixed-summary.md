# MCP Canary Diagnostic: Fixed Implementation Summary

## Review Fixes Applied

### 1. Two-Commit Per Policy Phase (CRITICAL)
**Problem**: Single commit changing mode+nonce together conceals transition failures.

**Solution**: Separate commits for mode change and nonce bump:
- **Commit 1**: Change `policyMode` only, keep `rolloutNonce` unchanged
  - Observe: Argo applies NetworkPolicy to RUNNING pod
  - Check: Does old pod fail probes? → IMMEDIATE ROLLBACK if yes
  - Only proceed if old pod stays healthy with new NP present
- **Commit 2**: Bump `rolloutNonce` to recreate pod
  - Observe: Fresh pod starts with policy already present
  - Check: Does new pod pass startup probes? → ROLLBACK if no

**Between phases**: Must return to `policyMode: none` + new nonce, confirm old NP absent, baseline pod Ready before next phase.

**First failure at any phase**: Rollback to `none` + new nonce, confirm all NP absent, baseline pod Ready, then STOP. No next phase.

### 2. Writable /tmp emptyDir (VERIFIED)
**Problem**: Historical foundation had writable /tmp, canary must match.

**Solution**: Added `emptyDir` volume mounted at `/tmp` with 64Mi sizeLimit.

**Runtime proof**: Container boots successfully with:
- `readOnlyRootFilesystem: true`
- `runAsUser: 65534` (nonroot)
- Config mounted read-only at `/config/config.yaml`
- Writable `/tmp` via emptyDir

**Session key behavior**: agentgateway v1.7.0 generates a session key when none is configured and attempts to write it back to the config file. This fails with "Read-only file system" (expected), then falls back to unencrypted MCP session IDs with a warning. This is acceptable for the canary because:
- No routes/sessions are tested (`routes: []`)
- No identity or backends are configured
- Readiness endpoint still responds
- Documented in configmap.yaml as canary-specific fixture security decision

### 3. policyMode Enum Validation
**Problem**: Invalid mode values accepted silently.

**Solution**: Added validation in `networkpolicy.yaml`:
```yaml
{{- if not (has .Values.policyMode (list "none" "ingress" "egress")) }}
{{- fail (printf "policyMode must be one of: none, ingress, egress (got: %s)" .Values.policyMode) }}
{{- end }}
```

**Test coverage**: Negative tests for "both", "invalid", "INGRESS", "ingress+egress", "" all correctly rejected.

### 4. Structural Render Tests (Not Just grep)
**Problem**: Tests used simple grep patterns, not structural YAML assertions.

**Solution**: Added `assert_yaml_structure()` function that:
- Extracts YAML blocks by kind+name
- Verifies field values within specific resources
- Checks podSelector matches canary labels only (not existing workloads)

**Coverage**:
- NetworkPolicy count/type/rules/ports/peers
- No identity resources (KanidmOAuth2Client, KanidmGroup, ExternalSecret, Ingress, ClusterRole, etc.)
- No backend routes (`routes: []`)
- No NodePort/LoadBalancer/hostNetwork/hostPort
- Image v1.7.0, nodeSelector prusik, ports 3000/15020/15021
- Deterministic render
- Nonce annotation present
- Writable /tmp emptyDir mounted

### 5. Pre-commit Checks
**Shellcheck**: All test scripts pass shellcheck with no warnings.

**Helm lint**: Passes with only INFO about missing icon (acceptable).

**Render with --include-crds**: All tests use `--include-crds` flag.

**Architecture**: Image is amd64, matches prusik ARCH (verified via `docker inspect`).

### 6. Scoped Read-Only kubectl Scripts
**observe-baseline.sh**: Captures pod status, events, logs, Cilium agent status (read-only).

**observe-policy-transition.sh**: Captures pod restarts, NetworkPolicy details, probe failures, node/IP (read-only).

**No full specs/secrets**: Scripts use `get/describe/logs` only, no `-o yaml` for secrets, `--tail=50/100` for logs.

**Context**: All scripts use `--context=grigri` explicitly.

### 7. Execution Handoff Phase Criteria
**Initial readiness**: Bounded wait ≤180s, then +3min stable window (no new Unhealthy events, no restarts) before mode change.

**After NP transition**: Fresh startup, bounded observation, record node/IP.

**Failure trigger**: Any new probe event → immediate rollback, don't wait for 30s kill.

**Cilium capture**: Baseline Cilium age/status only (read-only), no exec/escalation/priv capture/restart.

**Final cleanup**: `git rm -r apps/mcp-canary/` (only owned tracked paths), Argo app prune (no `kubectl delete namespace`), docs retain results.

## Independent Audit Corrections

### Valid Render Gap
Initial commit `5c0a882d5` contained invalid string nodeSelector at controller level, causing ArgoCD ComparisonError. Fixed in subsequent commits `4c68ca7ac` (removed template expression) and `c6c0a64f0` (removed top-level nodeSelector). Baseline window 01:07-01:10 reported in execution cannot substantiate 180s stability with valid manifest; actual valid baseline began after `c6c0a64f0` synced at 01:09:09.

### Actual Commit Times vs Reported
Ingress commit `54cc30531` actual time 01:13:37 UTC, not 01:10 as reported. First readiness probe failure at 01:16:22 is ~2min 45s after policy apply, not ~6min as timeline suggested. No authoritative apply/watch timestamps captured during execution; relative event ages converted to absolute times without precision.

### Unverified Watch Windows
Phase 0 and rollback 180s stability watches executed but timestamps not captured with precision. Watch output shows pod status at intervals but no continuous verification of probe success/failure counts. Restart timing not captured; rollback pod recreation (nonce bump 0→1) means recovery not isolated to policy removal alone.

### Recovery Mechanism Not Isolated
Rollback changed both policyMode (ingress→none) AND rolloutNonce (0→1), causing pod recreation with new UID. Cannot attribute recovery solely to NetworkPolicy removal; pod recreation itself may have resolved probe failures. Same-pod-UID recovery (policy removal without recreation) not tested.

### Source Conclusion
Ingress-associated kubelet probe failure reproduced on prusik. Missing explicit node allow is CANDIDATE mechanism, not proven root cause. No BPF verdict captured, no source host identity captured. Chronology, stability windows, and recovery mechanism INCONCLUSIVE. No egress phase executed; cannot clear egress/Firecrawl.

**Update 2026-11-09 (both readings below this line are now resolved by measurement):** the "missing
explicit node allow" candidate was an **ingress-side** allow, and it was granted explicitly on
`838031375` (`fromEntities: host, remote-node` on the probe port) — the probes failed anyway. The
follow-up candidate recorded here, "**pod→node response under an enforced egress policy**", was then
**falsified** by the two matrices run on `grigri`: egress-only policy sets kept probe pods Ready with
zero probe failures, while *any* policy carrying an ingress rule broke them (native NetworkPolicy and
CNP alike), and with `enable-host-firewall: false` the kubelet probe source matches **only** `world` —
`host`, `remote-node`, `cluster` and a node-subnet `fromCIDR` all fail. So this canary's own conclusion
("ingress-associated failure, missing node allow as candidate") was right about the direction and wrong
about the cure. The egress phase listed as "not yet executed" was executed separately and measured clean.
Durable rule and the proven split-port shape: `docs/conventions/cilium.md`; matrix tables and the full
trail, including this file's superseded readings: "Measured answer" in
`docs/troubleshooting/mcp-firecrawl-egress-regression.md`.

## Fixed File List

### Chart Files
- `apps/mcp-canary/Chart.yaml` (unchanged)
- `apps/mcp-canary/values.yaml` (added emptyDir for /tmp)
- `apps/mcp-canary/templates/configmap.yaml` (documented session key behavior)
- `apps/mcp-canary/templates/networkpolicy.yaml` (added policyMode enum validation)

### Test Files
- `apps/mcp-canary/tests/render-tests.sh` (rewritten with structural assertions + negative tests)
- `apps/mcp-canary/tests/observe-baseline.sh` (new: read-only baseline observation)
- `apps/mcp-canary/tests/observe-policy-transition.sh` (new: read-only transition observation)

### Documentation
- `planning/mcp-canary-handoff.md` (moved from root, will be rewritten with two-commit phases)

## Actual Test Results

### Render Tests (all pass)
```
=== Test: policyMode=none renders no NetworkPolicy ===
PASS

=== Test: policyMode=ingress renders one ingress NetworkPolicy ===
PASS

=== Test: policyMode=egress renders one egress NetworkPolicy ===
PASS

=== Test: policyMode validation rejects invalid values ===
  ✓ policyMode='both' correctly rejected
  ✓ policyMode='invalid' correctly rejected
  ✓ policyMode='INGRESS' correctly rejected
  ✓ policyMode='ingress+egress' correctly rejected
  ✓ policyMode='' correctly rejected
PASS

=== Test: rolloutNonce annotation is present ===
PASS

=== Test: no identity resources in any mode ===
PASS

=== Test: no backend routes in any mode ===
PASS

=== Test: no NodePort/LoadBalancer/hostNetwork/hostPort ===
PASS

=== Test: writable /tmp emptyDir is mounted ===
PASS

=== All tests passed ===
```

### Container Boot Test (verified)
```
Testing container boot with readOnlyRootFilesystem + nonroot + emptyDir /tmp...
Expected: warning about failing to write session key (acceptable for canary)

✓ Container boot successful: readiness endpoint responded
✓ Expected warning present: 'failed to write generated session key'
  (acceptable for canary - no routes/sessions tested)
✓ No unexpected permission errors
✓ Container logs show:
  - version: 1.7.0
  - listener established on [::]:15021 (readiness)
  - listener established on [::]:15020 (stats)
  - started bind on bind/3000 (gateway)
  - admin listener disabled (adminAddr: off)
```

### Shellcheck (all pass)
```
$ shellcheck apps/mcp-canary/tests/*.sh
(no output = no warnings)
```

### Helm Lint (passes)
```
$ helm lint apps/mcp-canary
==> Linting apps/mcp-canary
[INFO] Chart.yaml: icon is recommended
1 chart(s) linted, 0 chart(s) failed
```

### Architecture (verified)
```
$ docker inspect cr.agentgateway.dev/agentgateway:v1.7.0 --format='{{.Architecture}}'
amd64
```

## Two-Commit Phase Execution (NOT RUN YET)

### Phase 0: Baseline (policyMode=none, rolloutNonce=0)
```bash
# Commit canary chart with policyMode=none, rolloutNonce=0
git add apps/mcp-canary/
git commit -m "mcp-canary: Add diagnostic canary chart (policyMode=none)"
git push

# ArgoCD syncs, creates namespace mcp-canary, deploys pod
# Wait for Ready ≤180s
kubectl --context=grigri wait --for=condition=Ready pod -l app.kubernetes.io/name=mcp-canary -n mcp-canary --timeout=180s

# +3min stable window: no new Unhealthy events, no restarts
./apps/mcp-canary/tests/observe-baseline.sh mcp-canary

# If Ready + stable: baseline passes, proceed to Phase 1a
# If NotReady or restarts: record events, logs, STOP (baseline failure is a confound)
```

### Phase 1a: Ingress Mode Only (policyMode=ingress, rolloutNonce=0 UNCHANGED)
```bash
# Change mode ONLY, keep nonce unchanged
# Edit apps/mcp-canary/values.yaml:
#   policyMode: ingress
#   rolloutNonce: "0"  # UNCHANGED

git add apps/mcp-canary/values.yaml
git commit -m "mcp-canary: Phase 1a ingress mode (old pod still running)"
git push

# ArgoCD syncs, applies ingress NetworkPolicy to RUNNING pod
# Observe: does old pod fail probes?
./apps/mcp-canary/tests/observe-policy-transition.sh mcp-canary ingress

# If old pod fails probes (Unhealthy events, restarts): IMMEDIATE ROLLBACK
# If old pod stays healthy (Ready, 0 restarts, no probe failures): proceed to Phase 1b
```

### Phase 1b: Ingress Fresh Pod (policyMode=ingress, rolloutNonce=1)
```bash
# Bump nonce to recreate pod
# Edit apps/mcp-canary/values.yaml:
#   policyMode: ingress  # UNCHANGED
#   rolloutNonce: "1"    # BUMPED

git add apps/mcp-canary/values.yaml
git commit -m "mcp-canary: Phase 1b ingress fresh pod"
git push

# ArgoCD syncs, recreates pod with ingress NP present
# Wait for Ready ≤360s
kubectl --context=grigri wait --for=condition=Ready pod -l app.kubernetes.io/name=mcp-canary -n mcp-canary --timeout=360s

# Observe fresh startup
./apps/mcp-canary/tests/observe-policy-transition.sh mcp-canary ingress

# If Ready: ingress passes, proceed to Phase 1c (return to baseline)
# If NotReady: record events, logs, ROLLBACK to none+nonce=2, STOP
```

### Phase 1c: Return to Baseline (policyMode=none, rolloutNonce=2)
```bash
# Return to baseline BEFORE egress phase
# Edit apps/mcp-canary/values.yaml:
#   policyMode: none
#   rolloutNonce: "2"

git add apps/mcp-canary/values.yaml
git commit -m "mcp-canary: Phase 1c return to baseline before egress"
git push

# ArgoCD syncs, removes ingress NetworkPolicy, recreates pod
# Wait for Ready ≤180s
kubectl --context=grigri wait --for=condition=Ready pod -l app.kubernetes.io/name=mcp-canary -n mcp-canary --timeout=180s

# Confirm old NP absent, baseline pod Ready
./apps/mcp-canary/tests/observe-baseline.sh mcp-canary

# If Ready + no NP: proceed to Phase 2a
# If NotReady or NP still present: record, STOP
```

### Phase 2a: Egress Mode Only (policyMode=egress, rolloutNonce=2 UNCHANGED)
```bash
# Change mode ONLY, keep nonce unchanged
# Edit apps/mcp-canary/values.yaml:
#   policyMode: egress
#   rolloutNonce: "2"  # UNCHANGED

git add apps/mcp-canary/values.yaml
git commit -m "mcp-canary: Phase 2a egress mode (old pod still running)"
git push

# ArgoCD syncs, applies egress NetworkPolicy to RUNNING pod
# Observe: does old pod fail probes?
./apps/mcp-canary/tests/observe-policy-transition.sh mcp-canary egress

# If old pod fails probes: IMMEDIATE ROLLBACK
# If old pod stays healthy: proceed to Phase 2b
```

### Phase 2b: Egress Fresh Pod (policyMode=egress, rolloutNonce=3)
```bash
# Bump nonce to recreate pod
# Edit apps/mcp-canary/values.yaml:
#   policyMode: egress  # UNCHANGED
#   rolloutNonce: "3"   # BUMPED

git add apps/mcp-canary/values.yaml
git commit -m "mcp-canary: Phase 2b egress fresh pod"
git push

# ArgoCD syncs, recreates pod with egress NP present
# Wait for Ready ≤360s
kubectl --context=grigri wait --for=condition=Ready pod -l app.kubernetes.io/name=mcp-canary -n mcp-canary --timeout=360s

# Observe fresh startup
./apps/mcp-canary/tests/observe-policy-transition.sh mcp-canary egress

# If Ready: egress passes, proceed to Phase 3 (cleanup)
# If NotReady: record events, logs, ROLLBACK to none+nonce=4, STOP
```

### Phase 3: Cleanup
```bash
# Remove canary chart (only owned tracked paths)
git rm -r apps/mcp-canary/
git commit -m "mcp-canary: Remove diagnostic canary (Argo prune)"
git push

# ArgoCD prunes namespace mcp-canary (may remain empty, harmless)
# DO NOT kubectl delete namespace
# Verify resources gone
kubectl --context=grigri get all -n mcp-canary

# Verify other apps still Healthy
kubectl --context=grigri get applications -n argocd
```

## Rollback Procedure (Any Phase)
```bash
# Immediate rollback to baseline
# Edit apps/mcp-canary/values.yaml:
#   policyMode: none
#   rolloutNonce: "<next-nonce>"

git add apps/mcp-canary/values.yaml
git commit -m "mcp-canary: ROLLBACK <phase> failed, baseline restored"
git push

# Wait for Ready ≤180s
kubectl --context=grigri wait --for=condition=Ready pod -l app.kubernetes.io/name=mcp-canary -n mcp-canary --timeout=180s

# Confirm all NP absent, baseline pod Ready
./apps/mcp-canary/tests/observe-baseline.sh mcp-canary

# STOP: do not proceed to next phase
```

## Confounds / Blockers

### None Identified
- No identity/backends/routes → no auth surface, no backend connectivity needed
- No session key → no ESO dependency, no secret material (warning acceptable)
- Pinned to prusik → same node as foundation, same datapath
- Probes mirror foundation → same failure mode if policy blocks kubelet
- Writable /tmp → matches historical foundation, verified runtime

### Known Unknowns
- Mechanism: if ingress/egress causes timeout, is it Cilium BPF, kubelet probe path, or reply traffic?
- Two-commit approach: does applying NP to running pod reproduce the issue, or only fresh startup?
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
- ❌ No single-commit mode+nonce changes (conceals transition failures)

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
- [x] Local render tests pass (all 10 test groups)
- [x] Helm lint passes
- [x] Shellcheck passes (no warnings)
- [x] NodeSelector pinned to prusik in all three charts
- [x] No identity/backends/routes in canary
- [x] Writable /tmp emptyDir verified (runtime proof)
- [x] policyMode enum validation (negative tests pass)
- [x] Structural render tests (not just grep)
- [x] Scoped read-only kubectl scripts (observe-baseline, observe-policy-transition)
- [x] Two-commit phase execution plan documented
- [x] Rollback plan documented for each phase
- [x] Observations to record documented
- [x] Confounds/blockers documented
- [x] Foundation charts frozen with exact diff documented
- [x] Product probes frozen
- [x] Deferred items documented
- [x] Architecture verified (amd64)
- [ ] **NOT YET**: Commit canary chart
- [ ] **NOT YET**: Push to remote
- [ ] **NOT YET**: Execute Phase 0 (baseline)
- [ ] **NOT YET**: Execute Phase 1a/1b/1c (ingress two-commit)
- [ ] **NOT YET**: Execute Phase 2a/2b (egress two-commit)
- [ ] **NOT YET**: Execute Phase 3 (cleanup)

## Next Action

**Awaiting independent review before any commit/push/live execution.**

Reviewer should verify:
1. Two-commit phase approach correctly separates mode change from nonce bump
2. Rollback procedure is immediate and complete (none + new nonce, confirm NP absent)
3. Render tests cover all assertions (structural, not just grep)
4. Runtime proof shows container boots with readOnlyRootFilesystem + writable /tmp
5. policyMode validation rejects invalid values
6. Observation scripts are read-only (get/describe/logs only)
7. Execution plan is safe (no escalation, no Cilium restart, no direct apply/delete/patch)
8. Foundation charts remain frozen (untracked, only nodeSelector added)
9. Product probes remain frozen (no changes, no new runs)

Once approved, execute Phase 0 → Phase 1a → Phase 1b → Phase 1c → Phase 2a → Phase 2b → Phase 3 in sequence, with immediate rollback on first failure.
