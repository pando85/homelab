#!/usr/bin/env bash
# Read-only observation script for canary policy transition phases.
# Captures pod status, NetworkPolicy details, events, and logs after a policy change.
#
# Usage: ./tests/observe-policy-transition.sh [namespace] [expected-mode]
# Default namespace: mcp-canary
# Expected mode: none|ingress|egress (for validation)

set -euo pipefail

NAMESPACE="${1:-mcp-canary}"
EXPECTED_MODE="${2:-none}"
CONTEXT="grigri"

echo "=== Canary Policy Transition Observation ==="
echo "Namespace: $NAMESPACE"
echo "Expected mode: $EXPECTED_MODE"
echo "Context: $CONTEXT"
echo "Timestamp: $(date -u +%Y-%m-%dT%H:%M:%SZ)"
echo ""

echo "--- Pod Status (check for restarts/age) ---"
kubectl --context="$CONTEXT" get pods -n "$NAMESPACE" -o wide
echo ""

echo "--- Pod Restart Count ---"
kubectl --context="$CONTEXT" get pod -l app.kubernetes.io/name=mcp-canary -n "$NAMESPACE" \
  -o jsonpath='{.items[0].status.containerStatuses[0].restartCount}' || echo "0"
echo ""
echo ""

echo "--- NetworkPolicy (should match expected mode) ---"
np_count=$(kubectl --context="$CONTEXT" get networkpolicy -n "$NAMESPACE" --no-headers | wc -l)
echo "NetworkPolicy count: $np_count"

if [ "$EXPECTED_MODE" = "none" ]; then
    if [ "$np_count" -ne 0 ]; then
        echo "⚠ WARNING: expected 0 NetworkPolicy for mode=none, found $np_count"
    fi
elif [ "$EXPECTED_MODE" = "ingress" ] || [ "$EXPECTED_MODE" = "egress" ]; then
    if [ "$np_count" -ne 1 ]; then
        echo "⚠ WARNING: expected 1 NetworkPolicy for mode=$EXPECTED_MODE, found $np_count"
    else
        echo ""
        echo "--- NetworkPolicy Details ---"
        kubectl --context="$CONTEXT" get networkpolicy -n "$NAMESPACE" -o yaml | grep -A 30 "spec:"
    fi
fi
echo ""

echo "--- Pod Events (last 30, check for probe failures) ---"
kubectl --context="$CONTEXT" get events -n "$NAMESPACE" --sort-by='.lastTimestamp' | tail -30 | grep -E "Unhealthy|Probe|Failed" || echo "No probe failures found"
echo ""

echo "--- Pod Logs (last 100 lines, check for errors) ---"
kubectl --context="$CONTEXT" logs -l app.kubernetes.io/name=mcp-canary -n "$NAMESPACE" --tail=100 | grep -iE "error|fail|panic" || echo "No errors found in logs"
echo ""

echo "--- Pod Node and IP ---"
kubectl --context="$CONTEXT" get pod -l app.kubernetes.io/name=mcp-canary -n "$NAMESPACE" \
  -o custom-columns='NAME:.metadata.name,NODE:.spec.nodeName,IP:.status.podIP,HOSTIP:.status.hostIP' || true
echo ""

echo "--- Cilium Agent Status (read-only, no restart) ---"
kubectl --context="$CONTEXT" get pods -n kube-system -l k8s-app=cilium -o wide | head -5
echo ""

echo "=== Observation Complete ==="
echo "No mutations performed. All commands are read-only (get/describe/logs)."
echo ""
echo "Decision criteria:"
echo "  - Pod Ready with 0 restarts: policy transition succeeded"
echo "  - Pod not Ready or restarts > 0: policy transition failed, ROLLBACK immediately"
echo "  - Probe failures in events: policy blocked kubelet probes, ROLLBACK immediately"
