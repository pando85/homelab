#!/usr/bin/env bash
# Local validation for the MCP gateway foundation. Never contacts the live cluster.
#
#   1. `helm lint` + `helm template` both planes
#   2. boot the PINNED agentgateway binary with each rendered config and assert the
#      fail-closed behaviour and the ops endpoints
#   3. structural invariants of the rendered objects (test/mcp-gateway/chart-checks.py)
#
# Requires: helm, curl, python3 (+PyYAML), and a pinned agentgateway binary at
# $AGW_BIN (downloaded to a private temp dir if absent).
#
# Run from repo root: bash test/mcp-gateway/local-render.sh
#
# The chart renders point at cluster DNS names, so the booted gateway cannot reach a
# backend; that is fine here because nothing is enabled by default (`routes: []`).
# Enabled-route behaviour against a real backend is covered by e2e-authz.py, which
# boots the same templates with local overrides.
set -euo pipefail

REPO_ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/../.." && pwd)"
AGENTGATEWAY_VERSION="v1.7.0"
TMP="$(mktemp -d)"
trap 'rm -rf "$TMP"' EXIT

AGW_BIN="${AGW_BIN:-$TMP/agentgateway}"
if [ ! -x "$AGW_BIN" ]; then
  echo "Fetching agentgateway $AGENTGATEWAY_VERSION for config validation..."
  curl -fsSL -o "$AGW_BIN" \
    "https://github.com/agentgateway/agentgateway/releases/download/$AGENTGATEWAY_VERSION/agentgateway-linux-amd64"
  chmod +x "$AGW_BIN"
fi

pass=0
fail=0
record() { # record <status> <label> [detail]
  local status="$1" label="$2" detail="${3:-}"
  if [ "$status" = "PASS" ]; then
    echo "PASS  $label"
    pass=$((pass+1))
  else
    echo "FAIL  $label${detail:+ — $detail}"
    fail=$((fail+1))
  fi
}

cd "$REPO_ROOT"

# agentgateway binds the ops ports 15020/15021 by default, so only one instance can
# run per host: the planes are validated one after the other, never in parallel.
SESSION_KEY="$(openssl rand -hex 32)"
export SESSION_KEY

for chart_dir in apps/mcp-gateway apps/mcp-gateway-admin; do
  ns="$(basename "$chart_dir")"
  out="$TMP/$ns.yaml"

  if helm lint "$chart_dir" >"$TMP/lint-$ns.log" 2>&1; then
    record PASS "$ns: helm lint"
  else
    record FAIL "$ns: helm lint" "$(tail -3 "$TMP/lint-$ns.log" | tr '\n' ' ')"
  fi

  if ! helm template --namespace "$ns" "$ns" "$chart_dir" >"$out" 2>"$TMP/tpl-$ns.log"; then
    record FAIL "$ns: helm template" "$(tail -3 "$TMP/tpl-$ns.log" | tr '\n' ' ')"
    continue
  fi
  record PASS "$ns: helm template renders"

  python3 - "$out" "$TMP/config-$ns.yaml" << 'PYEOF'
import sys, yaml
for doc in yaml.safe_load_all(open(sys.argv[1])):
    if doc and doc.get("kind") == "ConfigMap":
        open(sys.argv[2], "w").write(doc["data"]["config.yaml"])
        break
else:
    sys.exit("no ConfigMap found")
PYEOF
  # Test-only: the rendered port is the cluster's 3000; use a free local one.
  sed -i 's/port: 3000/port: 18099/' "$TMP/config-$ns.yaml"

  "$AGW_BIN" -f "$TMP/config-$ns.yaml" >"$TMP/run-$ns.log" 2>&1 &
  pid=$!
  sleep 2
  if ! kill -0 "$pid" 2>/dev/null; then
    record FAIL "$ns: agentgateway boots with the rendered config" \
      "$(tail -4 "$TMP/run-$ns.log" | tr '\n' ' ')"
    continue
  fi
  record PASS "$ns: agentgateway boots with the rendered config"

  if curl -sf -m 3 -o /dev/null "http://127.0.0.1:15021/healthz/ready"; then
    record PASS "$ns: readiness on 15021 /healthz/ready"
  else
    record FAIL "$ns: readiness endpoint"
  fi
  if curl -sf -m 3 -o /dev/null "http://127.0.0.1:15020/metrics"; then
    record PASS "$ns: metrics on the separate port 15020"
  else
    record FAIL "$ns: metrics port"
  fi
  # adminAddr: "off" means nothing listens on the default admin port 15000. This is
  # checked on the wire, not in the log: the operator UI and the config dump are the
  # two things that must not be reachable from a pod network.
  if ! curl -s -m 2 -o /dev/null "http://127.0.0.1:15000/" \
     && ! grep -q ']:15000' "$TMP/run-$ns.log"; then
    record PASS "$ns: admin address is off (nothing listens on 15000)"
  else
    record FAIL "$ns: admin address is reachable on 15000"
  fi
  code=$(curl -s -m 3 -o /dev/null -w '%{http_code}' http://127.0.0.1:18099/mcp || echo "000")
  if [ "$code" = "404" ]; then
    record PASS "$ns: no MCP route answers while routes are disabled (404)"
  else
    record FAIL "$ns: expected 404 on /mcp while disabled" "got $code"
  fi
  for p in /firecrawl/mcp /grafana/mcp /vault/mcp; do
    c=$(curl -s -m 3 -o /dev/null -w '%{http_code}' "http://127.0.0.1:18099$p" || echo "000")
    [ "$c" = "404" ] || record FAIL "$ns: $p should be 404 while disabled" "got $c"
  done
  record PASS "$ns: every backend path answers 404 while disabled"

  # No session-protection warning: SESSION_KEY is exported above, which is what the
  # chart does through a generated Secret. e2e-authz.py asserts both encoders.
  if grep -q "MCP session IDs are not encrypted" "$TMP/run-$ns.log"; then
    record FAIL "$ns: ran with base64 session IDs despite SESSION_KEY"
  else
    record PASS "$ns: AES session protection active"
  fi

  kill -9 "$pid" 2>/dev/null || true
  wait "$pid" 2>/dev/null || true
  sleep 1
done

echo
echo "=== structural assertions on the rendered objects ==="
if python3 "$REPO_ROOT/test/mcp-gateway/chart-checks.py"; then
  pass=$((pass+1))
else
  fail=$((fail+1))
fi

echo
echo "=== Summary: $pass pass, $fail fail ==="
test "$fail" -eq 0
