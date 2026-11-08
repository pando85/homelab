#!/usr/bin/env python3
"""Session resumption, query and header pass-through at the MCP edge (v1.7.0).

Three owner gates are measured here, against the config the chart actually renders
(`credential: accessToken`, so the caller's validated bearer is forwarded):

  1. TWO-USER ISOLATION. Caller B replays caller A's `Mcp-Session-Id` while presenting
     B's OWN valid token. Upstream, a resumed session is re-checked only against the
     issuing backend and the target conditions, never against the caller's identity
     (crates/agentgateway/src/mcp/session.rs: get_session / get_or_resume_session).
     Whether that is a leak depends on how the backend gets its credential:
       - per-request credential (this probe, the Grafana/firecrawl shape): B is
         executed as B, so the route is safe;
       - credential cached per backend SESSION (vault-mcp 0.2.0): B is executed as A.
         That case is measured separately in session-binding-probe.py, not here.
  2. SESSION PROTECTION. The key comes from the SESSION_KEY env var (a chart-generated
     Secret). With no key - or `session.key: disabled` - config.rs falls back to base64
     session IDs, and MCPSessionState then carries no issuing backend at all.
  3. PASS-THROUGH DENY LIST. What the gateway does not remove reaches the backend
     verbatim, so the route's removal list is the boundary: the caller's target and
     credential override headers must stop, and an unnamed header must still arrive
     (proving it is the list that blocks, not the gateway's defaults).

    python3 test/mcp-gateway/session-isolation-probe.py [path/to/agentgateway]

Local only: two synthetic ES256 subjects with different `sub`, a recording backend, the
pinned binary. No cluster, no live gateway, no real credentials.
"""
import base64
import http.server
import json
import os
import re
import signal
import socket
import subprocess
import sys
import tempfile
import threading
import time

from cryptography.hazmat.primitives.asymmetric import ec
import jwt as pyjwt

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import chartconfig  # noqa: E402

AGENTGATEWAY_VERSION = "v1.7.0"
WORK = os.environ.get("MCP_E2E_WORK") or tempfile.mkdtemp(prefix="mcp-gw-sess-")
BACKEND_PORT = 14701
ISSUER_PORT = 14702
GW_PORT = 14700
STATS_PORT = 14710
READY_PORT = 14711

KEY = ec.generate_private_key(ec.SECP256R1())
PUB = KEY.public_key().public_numbers()
ISSUER = f"http://127.0.0.1:{ISSUER_PORT}"
AUD = "mcp-gateway"
SCOPE = "mcp_firecrawl"
SESSION_KEY = "0f" * 32  # a real deployment uses the chart's generated Secret

SUB_A = "11111111-1111-4111-8111-111111111111"
SUB_B = "22222222-2222-4222-8222-222222222222"

# Headers the route's removal list must stop, and one it must not (the control that
# proves pass-through is deny-list driven).
MUST_STOP = ("x-firecrawl-api-key", "x-api-key", "x-grafana-service-account-token",
             "x-grafana-api-key", "x-grafana-org-id", "x-grafana-url", "x-grafana-id",
             "x-vault-token", "vault_addr", "vault-skip-verify", "x-vault-namespace",
             "cookie")
MUST_PASS = "x-custom-attested"

SEEN = []
LOCK = threading.Lock()

B64 = lambda n, s: base64.urlsafe_b64encode(n.to_bytes(s, "big")).rstrip(b"=").decode()  # noqa: E731
JWKS = {"keys": [{"kty": "EC", "crv": "P-256", "x": B64(PUB.x, 32), "y": B64(PUB.y, 32),
                 "alg": "ES256", "use": "sig", "kid": "sess"}]}


class Issuer(http.server.BaseHTTPRequestHandler):
    def log_message(self, *a):
        pass

    def do_GET(self):
        body = json.dumps(JWKS).encode()
        self.send_response(200)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)


class Backend(http.server.BaseHTTPRequestHandler):
    """Recording MCP backend: keeps the full request line, headers and method."""

    def log_message(self, *a):
        pass

    def do_POST(self):
        n = int(self.headers.get("Content-Length") or 0)
        raw = self.rfile.read(n)
        try:
            req = json.loads(raw or b"{}")
        except Exception:
            req = {}
        with LOCK:
            SEEN.append({"path": self.path,
                         "headers": {k.lower(): v for k, v in self.headers.items()},
                         "method": req.get("method")})
        result = ({"protocolVersion": "2025-06-18", "capabilities": {"tools": {}},
                   "serverInfo": {"name": "sess-probe", "version": "0"}}
                  if req.get("method") == "initialize" else {})
        body = json.dumps({"jsonrpc": "2.0", "id": req.get("id", 1), "result": result}).encode()
        self.send_response(200)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)


def mint(sub):
    now = int(time.time())
    return pyjwt.encode({
        "iss": ISSUER, "sub": sub, "aud": AUD, "client_id": AUD,
        "iat": now, "nbf": now, "exp": now + 600, "jti": f"{sub}-{now}",
        "scope": f"openid email profile {SCOPE}", "session_id": sub},
        KEY, algorithm="ES256", headers={"kid": "sess", "typ": "at+jwt"})


def render_config():
    """The public chart's firecrawl route, in the delegation shape (`accessToken`).

    `credential: none` is what firecrawl ships with; `accessToken` is the shape the
    Grafana candidate uses, and it is the only shape where a session could matter for
    attribution, so this probe runs the delegation variant of the same template.
    """
    return chartconfig.render(
        "apps/mcp-gateway", "mcp-gateway", "mcp-gateway",
        ["backends.firecrawl.enabled=true",
         f"backends.firecrawl.url=http://127.0.0.1:{BACKEND_PORT}/mcp",
         f"backends.firecrawl.hostHeader=127.0.0.1:{BACKEND_PORT}",
         f"backends.firecrawl.resource=http://127.0.0.1:{GW_PORT}/firecrawl/mcp",
         "backends.firecrawl.credential=accessToken",
         f"auth.issuer={ISSUER}",
         f"auth.jwksUrl={ISSUER}/jwks",
         f"gateway.port={GW_PORT}"],
        # The delegation shape must render both the forwarding policy and the CEL
        # for an access token; if either disappears, the measurements below would
        # describe a config nobody ships.
        ("backendAuth", "passthrough", "jwt.scope.split(' ')"),
        stats_port=STATS_PORT, readiness_port=READY_PORT)


def rpc(method, token=None, sid=None, path="/firecrawl/mcp", extra=None, params=None):
    cmd = ["curl", "-sS", "-m", "10", "-D", os.path.join(WORK, "h.txt"),
           "-o", os.path.join(WORK, "b.txt"), "-w", "%{http_code}",
           f"http://127.0.0.1:{GW_PORT}{path}",
           "-H", "Content-Type: application/json",
           "-H", "Accept: application/json, text/event-stream",
           "-H", "mcp-protocol-version: 2025-06-18"]
    if token:
        cmd += ["-H", f"Authorization: Bearer {token}"]
    if sid:
        cmd += ["-H", f"Mcp-Session-Id: {sid}"]
    for k, v in (extra or {}).items():
        cmd += ["-H", f"{k}: {v}"]
    payload = {"jsonrpc": "2.0", "id": 1, "method": method}
    if params is not None or method == "initialize":
        payload["params"] = params if params is not None else {
            "protocolVersion": "2025-06-18", "capabilities": {},
            "clientInfo": {"name": "sess-probe", "version": "0"}}
    cmd += ["-d", json.dumps(payload)]
    code = subprocess.run(cmd, capture_output=True, text=True).stdout.strip()
    new_sid = sid
    for line in open(os.path.join(WORK, "h.txt")):
        if line.lower().startswith("mcp-session-id:"):
            new_sid = line.split(":", 1)[1].strip()
    return code, new_sid, open(os.path.join(WORK, "b.txt")).read()


def last():
    with LOCK:
        return SEEN[-1] if SEEN else None


def clear():
    with LOCK:
        del SEEN[:]


def boot(cfg, name, session_key=SESSION_KEY, read_only=False):
    """Start the pinned binary on a rendered config.

    `read_only` reproduces the cluster: the chart mounts /config from a ConfigMap
    with readOnly: true. That matters for session crypto - ensure_session_key
    (config.rs) will otherwise GENERATE a key and write it back into a writable
    config file, so a plain local run can end up in AES mode by accident and hide
    the fallback this probe is meant to measure.
    """
    path = os.path.join(WORK, f"{name}.yaml")
    log_path = os.path.join(WORK, f"{name}.log")
    open(path, "w").write(cfg)
    if read_only:
        os.chmod(path, 0o444)
    env = dict(os.environ)
    if session_key:
        env["SESSION_KEY"] = session_key
    else:
        env.pop("SESSION_KEY", None)
    proc = subprocess.Popen([BINARY, "-f", path], stdout=open(log_path, "w"),
                            stderr=subprocess.STDOUT, start_new_session=True, env=env)
    for _ in range(40):
        time.sleep(0.5)
        if proc.poll() is not None:
            return None, open(log_path).read()
        try:
            with socket.create_connection(("127.0.0.1", GW_PORT), timeout=1):
                return proc, log_path
        except OSError:
            continue
    return None, open(log_path).read()


def measurements(tokA, tokB):
    """Run the replay/query/header measurements. Returns a list of failure strings."""
    fails = []
    clear()
    code, sidA, _ = rpc("initialize", token=tokA)
    h = last()
    print(f"\n   1. A initialize                 -> http {code}, session issued {bool(sidA)} "
          f"(len {len(sidA or '')})")
    if h:
        print(f"      backend path {h['path']}, bearer is A's: "
              f"{h['headers'].get('authorization','').endswith(tokA[-12:])}")
    if code != "200":
        fails.append("A initialize not 200")

    clear()
    code, _, body = rpc("tools/list", token=tokB, sid=sidA)
    h = last()
    who = "?"
    if h:
        auth = h["headers"].get("authorization", "")
        who = "A" if auth.endswith(tokA[-12:]) else ("B" if auth.endswith(tokB[-12:]) else
                                                     "(none)")
    print(f"   2. B replays A's session id     -> http {code}, backend reached: "
          f"{bool(h)}, forwarded bearer belongs to caller {who}")
    if h and who == "A":
        fails.append("LEAK: B rode A's session with A's credential")
    elif not h:
        print("      (B's replay never reached the backend)")
    else:
        print("      => per-request credential: B is executed as B, not as A")
    if "-32603" in body:
        print("      note: the resumed stream errored upstream (-32603), which is the "
              "upstream's own state, not an authorization decision")

    code, sidB, _ = rpc("initialize", token=tokB)
    print(f"   3. B own session                -> http {code}, distinct id: {sidB != sidA}")

    clear()
    code, _, _ = rpc("initialize", token=tokA,
                     path="/firecrawl/mcp?vault_addr=http://127.0.0.1:9999&x=1")
    h = last()
    q = (h or {}).get("path", "")
    print(f"   4. request with ?vault_addr=... -> http {code}, backend path {q!r} => "
          f"query {'FORWARDED' if '?' in q else 'dropped at the edge'}")
    if "?" in q:
        fails.append("query string forwarded to backend")

    clear()
    inj = {k: "caller-controlled" for k in MUST_STOP}
    inj["X-Vault-Token"] = "hvs.caller-owned"
    inj["Cookie"] = "session=abc"
    inj[MUST_PASS] = "whatever"
    code, _, _ = rpc("initialize", token=tokA, extra=inj)
    h = last()
    print(f"   5. header pass-through          -> http {code}")
    if not h:
        fails.append("header test: backend not reached")
    else:
        hh = h["headers"]
        passed = [k for k in MUST_STOP if k in hh]
        if passed:
            fails.append(f"headers reached backend: {passed}")
            print(f"      FAIL leaked: {passed}")
        else:
            print(f"      all {len(MUST_STOP)} override headers stopped")
        if MUST_PASS not in hh:
            fails.append(f"control header {MUST_PASS} was unexpectedly removed")
            print(f"      FAIL: control header {MUST_PASS} missing - the gateway is not "
                  f"passing unnamed headers, so the deny list is not what protects")
        else:
            print(f"      control header {MUST_PASS} passed through "
                  f"-> the removal list is what blocks, nothing else")
    return fails


def main():
    global BINARY
    BINARY = sys.argv[1] if len(sys.argv) > 1 else None
    if BINARY is None:
        import urllib.request
        BINARY = os.path.join(WORK, "agentgateway")
        if not os.path.exists(BINARY):
            with urllib.request.urlopen(
                "https://github.com/agentgateway/agentgateway/releases/download/"
                f"{AGENTGATEWAY_VERSION}/agentgateway-linux-amd64", timeout=180) as r, \
                    open(BINARY, "wb") as fh:
                fh.write(r.read())
            os.chmod(BINARY, 0o755)

    os.makedirs(WORK, exist_ok=True)
    cfg = render_config()
    open(os.path.join(WORK, "rendered-config.yaml"), "w").write(cfg)
    print(f"chart-rendered config: {os.path.join(WORK, 'rendered-config.yaml')}")
    threading.Thread(target=lambda: http.server.ThreadingHTTPServer(
        ("127.0.0.1", ISSUER_PORT), Issuer).serve_forever(), daemon=True).start()
    threading.Thread(target=lambda: http.server.ThreadingHTTPServer(
        ("127.0.0.1", BACKEND_PORT), Backend).serve_forever(), daemon=True).start()
    tokA, tokB = mint(SUB_A), mint(SUB_B)
    allfails = []

    print("=" * 74)
    print("PHASE 1: production shape (SESSION_KEY set -> AES-256-GCM session IDs)")
    proc, log = boot(cfg, "aes")
    if proc is None:
        print("gateway did not start:\n" + (log or "")[-1500:])
        return 1
    try:
        allfails += measurements(tokA, tokB)
        gwlog = open(log).read()
        warn = "MCP session IDs are not encrypted" in gwlog
        print(f"   6. session protection         -> unencrypted-session warning present: "
              f"{warn} (must be False with an AES key)")
        if warn:
            allfails.append("AES mode still warns about unencrypted sessions")
        # The chart mounts the ConfigMap read-only, so the binary must never be able
        # to rewrite the shipped config (ensure_session_key writes a generated key
        # into a writable config file - see config.rs).
        if "session:" in open(os.path.join(WORK, "aes.yaml")).read():
            allfails.append("gateway mutated its own config file")
        else:
            print("      shipped config still has no `session:` block (config untouched)")
    finally:
        os.killpg(os.getpgid(proc.pid), signal.SIGTERM)
        proc.wait(timeout=15)

    print("=" * 74)
    print("PHASE 2: no SESSION_KEY, read-only config (the cluster's ConfigMap mount)")
    print("   expect the base64 fallback and its warning: this is what shipping")
    print("   without the Secret would do, and it is why the chart generates one.")
    proc, log = boot(cfg, "b64", session_key=None, read_only=True)
    if proc is None:
        print("gateway did not start:\n" + (log or "")[-1500:])
        return 1
    try:
        time.sleep(1.5)
        allfails += measurements(tokA, tokB)
        gwlog = open(log).read()
        warn = "MCP session IDs are not encrypted" in gwlog
        print(f"   6. session protection         -> unencrypted-session warning present: "
              f"{warn} (True is expected: it is why the chart ships a key)")
        if not warn:
            allfails.append("base64 fallback did not warn - key handling changed")
        os.chmod(os.path.join(WORK, "b64.yaml"), 0o644)
    finally:
        os.killpg(os.getpgid(proc.pid), signal.SIGTERM)
        proc.wait(timeout=15)

    print("=" * 74)
    if allfails:
        print("FAILURES: " + "; ".join(allfails))
        print(f"(work dir {WORK})")
        return 2
    print("All session/isolation and pass-through assertions passed")
    print(f"(work dir {WORK})")
    return 0


if __name__ == "__main__":
    sys.exit(main())
