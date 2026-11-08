#!/usr/bin/env bash
# Read-only observation script for canary baseline phase.
# Captures pod status, events, logs, and Cilium status without any mutations.
#
# Usage: ./tests/observe-baseline.sh [namespace]
# Default namespace: mcp-canary

set -euo pipefail

NAMESPACE="${1:-mcp-canary}"
CONTEXT="grigri"

echo "=== Canary Baseline Observation ==="
echo "Namespace: $NAMESPACE"
echo "Context: $CONTEXT"
echo "Timestamp: $(date -u +%Y-%m-%dT%H:%M:%SZ)"
echo ""

echo "--- Pod Status ---"
kubectl --context="$CONTEXT" get pods -n "$NAMESPACE" -o wide
echo ""

echo "--- Pod Details ---"
kubectl --context="$CONTEXT" describe pod -l app.kubernetes.io/name=mcp-canary -n "$NAMESPACE" | grep -A 20 "Conditions:" || true
echo ""

echo "--- Pod Events (last 20) ---"
kubectl --context="$CONTEXT" get events -n "$NAMESPACE" --sort-by='.lastTimestamp' | tail -20
echo ""

echo "--- Pod Logs (last 50 lines) ---"
kubectl --context="$CONTEXT" logs -l app.kubernetes.io/name=mcp-canary -n "$NAMESPACE" --tail=50 || true
echo ""

echo "--- NetworkPolicy (should be empty for baseline) ---"
kubectl --context="$CONTEXT" get networkpolicy -n "$NAMESPACE" || echo "No NetworkPolicy found (expected for baseline)"
echo ""

echo "--- Cilium Agent Status (read-only) ---"
kubectl --context="$CONTEXT" get pods -n kube-system -l k8s-app=cilium -o wide | head -5
echo ""

echo "--- Cilium Agent Age (one line per node) ---"
kubectl --context="$CONTEXT" get pods -n kube-system -l k8s-app=cilium -o custom-columns='NAME:.metadata.name,AGE:.metadata.creationTimestamp,NODE:.spec.nodeName' || true
echo ""

echo "=== Observation Complete ==="
echo "No mutations performed. All commands are read-only (get/describe/logs)."
