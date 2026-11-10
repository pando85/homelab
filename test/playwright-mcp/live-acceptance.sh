#!/usr/bin/env bash
# Live acceptance test for the Playwright MCP server.
#
# Drives a real MCP session over HTTP from inside the pod, navigates to
# https://example.com, and asserts the snapshot contains "Example Domain".
# Also verifies the egress policy: internet HTTPS succeeds, cluster Service
# addresses time out, and LAN addresses time out.
#
# Run after ArgoCD syncs:
#   bash test/playwright-mcp/live-acceptance.sh
#
# Bounded timeouts: total runtime ~60-90s. Idempotent and safe to re-run.
set -euo pipefail

KUBECTL_CONTEXT=grigri
NAMESPACE=playwright-mcp
RELEASE=playwright-mcp
TOTAL=0
PASS=0

log() { printf '%s\n' "$*"; }
pass() { TOTAL=$((TOTAL + 1)); PASS=$((PASS + 1)); log "PASS  $*"; }
fail() { TOTAL=$((TOTAL + 1)); log "FAIL  $*"; }

kubectl_grigri() { kubectl --context="$KUBECTL_CONTEXT" "$@"; }

# Find the pod
POD=$(kubectl_grigri -n "$NAMESPACE" get pod \
  -l "app.kubernetes.io/instance=$RELEASE" \
  -o jsonpath='{.items[0].metadata.name}' 2>/dev/null) || {
  log "ERROR: no pod found for release=$RELEASE in namespace=$NAMESPACE"
  exit 1
}
log "pod=$POD"

# Wait for the pod to be Ready (bounded)
if ! kubectl_grigri -n "$NAMESPACE" wait pod/"$POD" --for=condition=Ready --timeout=120s; then
  fail "pod not Ready within 120s"
  log "pod status:"
  kubectl_grigri -n "$NAMESPACE" get pod "$POD" -o wide
  log "pod events:"
  kubectl_grigri -n "$NAMESPACE" describe pod "$POD" | tail -30
  exit 1
fi
pass "pod is Ready"

# MCP session script (runs inside the pod via kubectl exec)
MCP_JS=$(cat <<'JS'
const http = require('http');

function rpc(method, params, id, sessionId, protocolVersion) {
  return new Promise((resolve, reject) => {
    const isNotification = id === null || id === undefined;
    const body = JSON.stringify(isNotification
      ? { jsonrpc: '2.0', method, params }
      : { jsonrpc: '2.0', id, method, params });
    const headers = {
      'Content-Type': 'application/json',
      'Accept': 'application/json, text/event-stream',
    };
    if (sessionId) headers['Mcp-Session-Id'] = sessionId;
    if (protocolVersion) headers['MCP-Protocol-Version'] = protocolVersion;
    const req = http.request({
      hostname: '127.0.0.1',
      port: 8931,
      path: '/mcp',
      method: 'POST',
      timeout: 20000,
      headers,
    }, (res) => {
      let data = '';
      res.setEncoding('utf8');
      res.on('data', (chunk) => { data += chunk; });
      res.on('end', () => resolve({ status: res.statusCode, headers: res.headers, body: data }));
    });
    req.on('error', reject);
    req.on('timeout', () => { req.destroy(); reject(new Error('timeout ' + method)); });
    req.end(body);
  });
}

function parseBody(body) {
  const t = body.trim();
  if (t.startsWith('{')) return JSON.parse(t);
  // SSE framing: lines "event: ...\ndata: {...}\n\n"
  const lines = t.split(/\r?\n/).filter((l) => l.startsWith('data:'));
  if (!lines.length) throw new Error('unparseable body: ' + body.slice(0, 400));
  // Multiple data lines: concatenate (some servers split large responses)
  return JSON.parse(lines.map((l) => l.slice(5).trim()).join(''));
}

async function main() {
  // 1. initialize
  const initResp = await rpc('initialize', {
    protocolVersion: '2025-06-18',
    capabilities: {},
    clientInfo: { name: 'pw-acceptance', version: '1' },
  }, 1, null, null);
  if (initResp.status >= 500) {
    console.error('INITIALIZE_FAILED', initResp.status, initResp.body.slice(0, 500));
    process.exit(2);
  }
  const initResult = parseBody(initResp.body);
  const sessionId = initResp.headers['mcp-session-id'];
  const protocolVersion = initResp.headers['mcp-protocol-version'] || '2025-06-18';
  if (!sessionId) {
    console.error('NO_SESSION_ID', initResp.headers);
    process.exit(2);
  }
  console.log('INITIALIZE_OK', 'sessionId=' + sessionId, 'protocolVersion=' + protocolVersion);

  // 2. notifications/initialized
  await rpc('notifications/initialized', {}, null, sessionId, protocolVersion);
  console.log('NOTIFIED');

  // 3. tools/list
  const toolsResp = await rpc('tools/list', {}, 2, sessionId, protocolVersion);
  const toolsResult = parseBody(toolsResp.body);
  const toolCount = (toolsResult.result && toolsResult.result.tools)
    ? toolsResult.result.tools.length
    : 0;
  console.log('TOOLS_LIST', 'count=' + toolCount);
  if (toolCount === 0) {
    console.error('NO_TOOLS', toolsResp.body.slice(0, 500));
    process.exit(2);
  }

  // 4. browser_navigate to https://example.com
  const navResp = await rpc('tools/call', {
    name: 'browser_navigate',
    arguments: { url: 'https://example.com' },
  }, 3, sessionId, protocolVersion);
  const navResult = parseBody(navResp.body);
  if (navResult.result && navResult.result.isError) {
    console.error('NAVIGATE_ERROR', JSON.stringify(navResult.result).slice(0, 500));
    process.exit(2);
  }
  console.log('NAVIGATE_OK');

  // 5. browser_snapshot
  const snapResp = await rpc('tools/call', {
    name: 'browser_snapshot',
    arguments: {},
  }, 4, sessionId, protocolVersion);
  const snapResult = parseBody(snapResp.body);
  const snapText = (snapResult.result && snapResult.result.content)
    ? snapResult.result.content.map((b) => b.text || '').join('\n')
    : '';
  if (!snapText.includes('Example Domain')) {
    console.error('SNAPSHOT_MISSING_EXAMPLE_DOMAIN');
    console.error('SNAPSHOT_BODY', snapText.slice(0, 1000));
    process.exit(2);
  }
  console.log('SNAPSHOT_OK', 'bytes=' + snapText.length);

  // 6. browser_close (best-effort, idempotent)
  try {
    await rpc('tools/call', { name: 'browser_close', arguments: {} }, 5, sessionId, protocolVersion);
    console.log('CLOSED');
  } catch (e) {
    console.log('CLOSE_SKIPPED', e.message);
  }

  console.log('MCP_SESSION_PASS');
}

main().catch((e) => {
  console.error('MCP_SESSION_FAIL', e.message);
  process.exit(2);
});
JS
)

# Run the MCP session
log "=== MCP session ==="
if kubectl_grigri -n "$NAMESPACE" exec -i "$POD" -- node -e "$MCP_JS"; then
  pass "MCP session (initialize, tools/list, browser_navigate, browser_snapshot)"
else
  fail "MCP session"
  log "raw output above"
fi

# Egress tests (run inside the pod via kubectl exec)
EGRESS_JS=$(cat <<'JS'
const http = require('http');
const https = require('https');

function tcpConnect(host, port, timeoutMs) {
  return new Promise((resolve) => {
    const sock = require('net').createConnection({ host, port });
    let done = false;
    const finish = (ok, err) => {
      if (done) return;
      done = true;
      sock.destroy();
      resolve({ ok, err: err ? err.code || err.message : null });
    };
    sock.setTimeout(timeoutMs, () => finish(false, new Error('timeout')));
    sock.on('connect', () => finish(true));
    sock.on('error', (e) => finish(false, e));
  });
}

function httpsGet(url, timeoutMs) {
  return new Promise((resolve) => {
    const req = https.get(url, { timeout: timeoutMs }, (res) => {
      let data = '';
      res.setEncoding('utf8');
      res.on('data', (c) => { data += c; });
      res.on('end', () => resolve({ ok: true, status: res.statusCode, bytes: data.length }));
    });
    req.on('error', (e) => resolve({ ok: false, err: e.code || e.message }));
    req.on('timeout', () => { req.destroy(); resolve({ ok: false, err: 'timeout' }); });
  });
}

async function main() {
  // 1. internet HTTPS
  const inet = await httpsGet('https://example.com', 15000);
  console.log('INTERNET_HTTPS', JSON.stringify(inet));
  if (!inet.ok || inet.status !== 200) {
    console.error('INTERNET_FAIL');
    process.exit(3);
  }

  // 2. cluster Service (mcp-gateway-standalone.mcp-gateway.svc.cluster.local:3000)
  //    DNS resolution is allowed, but TCP to 10.43.x.x is denied -> timeout.
  const cluster = await tcpConnect('mcp-gateway-standalone.mcp-gateway.svc.cluster.local', 3000, 8000);
  console.log('CLUSTER_SVC', JSON.stringify(cluster));
  if (cluster.ok) {
    console.error('CLUSTER_SVC_CONNECTED_UNEXPECTEDLY');
    process.exit(3);
  }

  // 3. LAN (192.168.193.13:443, the internal Envoy VIP)
  const lan = await tcpConnect('192.168.193.13', 443, 8000);
  console.log('LAN_VIP', JSON.stringify(lan));
  if (lan.ok) {
    console.error('LAN_VIP_CONNECTED_UNEXPECTEDLY');
    process.exit(3);
  }

  console.log('EGRESS_PASS');
}

main().catch((e) => {
  console.error('EGRESS_FAIL', e.message);
  process.exit(3);
});
JS
)

log "=== egress policy ==="
if kubectl_grigri -n "$NAMESPACE" exec -i "$POD" -- node -e "$EGRESS_JS"; then
  pass "egress policy (internet OK, cluster+LAN denied)"
else
  fail "egress policy"
  log "raw output above"
fi

log ""
log "PLAYWRIGHT_VERIFY_PASS $PASS/$TOTAL"
if [ "$PASS" -ne "$TOTAL" ]; then
  exit 1
fi
