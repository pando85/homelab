#!/usr/bin/env python3
"""Phase A: does a stateful backend's Mcp-Session-Id survive a stateless gateway route?

The three live gateway routes (context7, grafana, firecrawl) all use
`statefulMode: stateless`. Playwright MCP is stateful: it issues an
`mcp-session-id` header on `initialize` and requires it on subsequent
requests. If the gateway drops or fails to propagate that session id,
then `browser_navigate` followed by `browser_snapshot` would hit a fresh
browser and Stage 2 via the gateway is not viable in stateless mode.

Two questions:
  1. Does the CLIENT receive the backend-issued Mcp-Session-Id?
  2. When the client echoes it, does the BACKEND receive it?

The existing assertion at local-boot-checks.py:635 ("B stateless: the
gateway issues no Mcp-Session-Id anywhere") only proves the gateway does
not INJECT a session id, because the fixture backend never ISSUES one.
This probe uses a fixture that mirrors real Playwright behaviour.

Nothing here touches a cluster or a production credential. The gateway
binds 127.0.0.1 on an ephemeral port. The stub issuer is loopback-only.

    python3 test/playwright-mcp/session-propagation-probe.py

Requires: python3 with PyJWT + cryptography; agentgateway v1.7.0.
"""
import base64
import hashlib
import json
import os
import socket
import subprocess
import sys
import tempfile
import threading
import time
import urllib.error
import urllib.request
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

import jwt as pyjwt
from cryptography.hazmat.primitives.asymmetric import ec

BIN = os.environ.get(
    "AGENTGATEWAY_BIN",
    os.path.expanduser("~/.cache/mcp-gateway-tests/agentgateway"))
PINNED_VERSION = "1.7.0"
AUDIENCE = "mcp-gateway"

results = []


def check(label, ok, detail=""):
    print(f"{'PASS' if ok else 'FAIL'}  {label}" + (f"\n      {detail}" if detail else ""))
    results.append(bool(ok))


def b64i(i, ln):
    return base64.urlsafe_b64encode(i.to_bytes(ln, "big")).rstrip(b"=").decode()


def free_port():
    for _ in range(50):
        with socket.socket() as s:
            s.bind(("127.0.0.1", 0))
            port = s.getsockname()[1]
        with socket.socket() as probe:
            probe.settimeout(0.4)
            if probe.connect_ex(("127.0.0.1", port)) != 0:
                return port
    raise RuntimeError("no free loopback port")


# --------------------------------------------------------------------------- stub IdP
KEY = ec.generate_private_key(ec.SECP256R1())
KID = "probe-key"


def jwk():
    pub = KEY.public_key().public_numbers()
    return {"kty": "EC", "crv": "P-256", "x": b64i(pub.x, 32),
            "y": b64i(pub.y, 32), "alg": "ES256", "use": "sig", "kid": KID}


def mint_token(issuer_origin, scope="mcp_playwright"):
    now = int(time.time())
    claims = {"iss": issuer_origin, "sub": "probe-user", "aud": AUDIENCE,
              "client_id": AUDIENCE, "scope": scope,
              "iat": now, "nbf": now, "exp": now + 3600}
    return pyjwt.encode(claims, KEY, algorithm="ES256", headers={"kid": KID})


class StubIdp(BaseHTTPRequestHandler):
    origin = ""

    def log_message(self, *a):
        pass

    def do_GET(self):
        if self.path == "/jwks":
            body = json.dumps({"keys": [jwk()]}).encode()
        elif self.path.startswith("/.well-known/"):
            body = json.dumps({"issuer": self.origin,
                               "jwks_uri": f"{self.origin}/jwks"}).encode()
        else:
            self.send_response(404)
            self.end_headers()
            return
        self.send_response(200)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)


# --------------------------------------------------------------------------- stateful backend
class StatefulBackend(BaseHTTPRequestHandler):
    """Mirrors Playwright MCP session behaviour:
    - On `initialize`, issues an `mcp-session-id` response header
    - On subsequent requests, requires that header and records whether it matches
    """
    session_id = None
    records = []
    lock = threading.Lock()

    def log_message(self, *a):
        pass

    def do_POST(self):
        length = int(self.headers.get("Content-Length", 0))
        raw = self.rfile.read(length)
        hdrs = {k.lower(): v for k, v in self.headers.items()}
        received_sid = hdrs.get("mcp-session-id")

        try:
            msg = json.loads(raw or b"{}")
        except ValueError:
            msg = {}
        method = msg.get("method")
        msg_id = msg.get("id", 1)

        record = {
            "method": method,
            "received_sid": received_sid,
            "expected_sid": None,
            "sid_match": None,
        }

        if method == "initialize":
            # Issue a session id (like real Playwright does)
            new_sid = f"playwright-sess-{os.urandom(8).hex()}"
            with self.lock:
                StatefulBackend.session_id = new_sid
            record["expected_sid"] = new_sid

            body = json.dumps({
                "jsonrpc": "2.0", "id": msg_id,
                "result": {
                    "protocolVersion": "2025-06-18",
                    "capabilities": {"tools": {}},
                    "serverInfo": {"name": "Playwright", "version": "1.66.0-test"}
                }
            }).encode()
            self.send_response(200)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(body)))
            self.send_header("mcp-session-id", new_sid)
            self.end_headers()
            self.wfile.write(body)

        elif method == "tools/call":
            with self.lock:
                expected = StatefulBackend.session_id
            record["expected_sid"] = expected
            record["sid_match"] = (received_sid == expected) if expected else None

            if received_sid and received_sid == expected:
                body = json.dumps({
                    "jsonrpc": "2.0", "id": msg_id,
                    "result": {"content": [{"type": "text", "text": "ok"}]}
                }).encode()
                self.send_response(200)
                self.send_header("Content-Type", "application/json")
                self.send_header("Content-Length", str(len(body)))
                self.send_header("mcp-session-id", expected)
                self.end_headers()
                self.wfile.write(body)
            else:
                body = json.dumps({
                    "jsonrpc": "2.0", "id": msg_id,
                    "error": {"code": -32000, "message": "invalid session"}
                }).encode()
                self.send_response(200)
                self.send_header("Content-Type", "application/json")
                self.send_header("Content-Length", str(len(body)))
                self.send_header("mcp-session-id", expected or "")
                self.end_headers()
                self.wfile.write(body)

        elif method and method.startswith("notifications/"):
            self.send_response(202)
            self.send_header("Content-Length", "0")
            if received_sid:
                self.send_header("mcp-session-id", received_sid)
            self.end_headers()

        else:
            body = json.dumps({"jsonrpc": "2.0", "id": msg_id, "result": {}}).encode()
            self.send_response(200)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(body)))
            if received_sid:
                self.send_header("mcp-session-id", received_sid)
            self.end_headers()
            self.wfile.write(body)

        with self.lock:
            StatefulBackend.records.append(record)

    def do_GET(self):
        self.send_response(405)
        self.end_headers()


# --------------------------------------------------------------------------- gateway boot
def boot_gateway(config_text, work_dir):
    config_path = os.path.join(work_dir, "config.yaml")
    log_path = os.path.join(work_dir, "gateway.log")
    with open(config_path, "w") as f:
        f.write(config_text)

    # Validate first
    result = subprocess.run(
        [BIN, "-f", config_path, "--validate-only"],
        capture_output=True, text=True, timeout=30)
    if result.returncode != 0:
        raise RuntimeError(f"config rejected: {(result.stdout + result.stderr)[:500]}")

    env = dict(os.environ,
               HOME=os.path.join(work_dir, "home"),
               XDG_CONFIG_HOME=os.path.join(work_dir, "home", ".config"),
               SESSION_KEY=os.urandom(32).hex())
    os.makedirs(env["HOME"], exist_ok=True)

    log_fh = open(log_path, "w")
    proc = subprocess.Popen([BIN, "-f", config_path],
                            stdout=log_fh, stderr=log_fh, env=env)
    return proc, log_fh, log_path


def wait_port(port, timeout=20):
    deadline = time.time() + timeout
    while time.time() < deadline:
        try:
            with socket.create_connection(("127.0.0.1", port), 0.5):
                return True
        except OSError:
            time.sleep(0.2)
    return False


def mcp_request(port, path, token, method="initialize", session_id=None, extra_headers=None):
    """Send an MCP JSON-RPC request. Returns (status, response_headers, body)."""
    body = {"jsonrpc": "2.0", "id": 1, "method": method}
    if method == "initialize":
        body["params"] = {
            "protocolVersion": "2025-06-18",
            "clientInfo": {"name": "session-probe", "version": "0"},
            "capabilities": {}
        }
    elif method == "tools/call":
        body["params"] = {"name": "browser_navigate", "arguments": {"url": "https://example.com"}}
    else:
        body["params"] = {}

    headers = {
        "Content-Type": "application/json",
        "Accept": "application/json, text/event-stream",
        "MCP-Protocol-Version": "2025-06-18",
    }
    # token=None omits the header entirely, which is what the negative tests
    # need: an empty `Bearer ` value is a different request than no credential.
    if token is not None:
        headers["Authorization"] = f"Bearer {token}"
    if session_id:
        headers["Mcp-Session-Id"] = session_id
    if extra_headers:
        headers.update(extra_headers)

    req = urllib.request.Request(
        f"http://127.0.0.1:{port}{path}",
        data=json.dumps(body).encode(),
        method="POST",
        headers=headers)
    try:
        with urllib.request.urlopen(req, timeout=15) as resp:
            return resp.status, dict(resp.headers), resp.read()[:2000]
    except urllib.error.HTTPError as e:
        return e.code, dict(e.headers), e.read()[:2000]
    except (OSError, urllib.error.URLError) as e:
        return f"ERR:{type(e).__name__}", {}, str(e).encode()[:200]


def build_config(gw_port, backend_port, idp_origin):
    return f"""
config:
  adminAddr: "off"
  storage:
    mode: file
gateways:
  default:
    port: {gw_port}
    bindAddress: "127.0.0.1"
routes:
  - name: playwright
    gateways:
      - default
    matches:
      - path:
          pathPrefix: /playwright/mcp
    policies:
      mcpAuthentication:
        mode: strict
        issuer: {idp_origin}
        audiences:
          - mcp-gateway
        jwks:
          url: {idp_origin}/jwks
        resourceMetadata:
          resource: https://mcp.grigri.cloud/playwright/mcp
          scopesSupported:
            - mcp_playwright
      authorization:
        rules:
          - require: "!has(jwt.email)"
          - require: "!has(jwt.preferred_username)"
          - require: "has(jwt.scope) && jwt.scope.split(' ').exists(s, s == 'mcp_playwright')"
      requestHeaderModifier:
        remove:
          - Authorization
          - Cookie
    backends:
      - mcp:
          targets:
            - name: playwright
              mcp:
                host: 127.0.0.1
                port: {backend_port}
                path: /mcp
          statefulMode: @@MODE@@
"""


def run_probe(mode_label, stateful_mode):
    """Boot a stateful backend + gateway in the given mode, probe session propagation."""
    print(f"\n{'='*60}")
    print(f"  Testing statefulMode: {mode_label}")
    print(f"{'='*60}")

    gw_port = free_port()
    backend_port = free_port()
    idp_port = free_port()

    # Reset backend state
    StatefulBackend.session_id = None
    StatefulBackend.records = []

    # Start stub IdP
    idp_handler = type("BoundIdp", (StubIdp,), {})
    idp_handler.origin = f"http://127.0.0.1:{idp_port}"
    idp_srv = ThreadingHTTPServer(("127.0.0.1", idp_port), idp_handler)
    idp_srv.daemon_threads = True
    threading.Thread(target=idp_srv.serve_forever, daemon=True).start()

    # Start stateful backend
    backend_srv = ThreadingHTTPServer(("127.0.0.1", backend_port), StatefulBackend)
    backend_srv.daemon_threads = True
    threading.Thread(target=backend_srv.serve_forever, daemon=True).start()

    # Start gateway
    work = tempfile.mkdtemp(prefix=f"session-probe-{mode_label}-")
    config_text = build_config(gw_port, backend_port,
                               f"http://127.0.0.1:{idp_port}").replace("@@MODE@@", stateful_mode)
    proc, log_fh, log_path = boot_gateway(config_text, work)

    try:
        if not wait_port(gw_port):
            print(f"Gateway failed to start. Log tail:")
            try:
                print(open(log_path).read()[-1000:])
            except OSError:
                pass
            check(f"{mode_label}: gateway boots", False, "port never opened")
            return

        check(f"{mode_label}: gateway boots", proc.poll() is None)

        token = mint_token(f"http://127.0.0.1:{idp_port}")

        # Question 1: Does the CLIENT receive the backend-issued Mcp-Session-Id?
        status, resp_hdrs, body = mcp_request(
            gw_port, "/playwright/mcp", token, method="initialize")
        check(f"{mode_label}: initialize returns 200",
              status == 200, f"status={status} body={body[:100]!r}")

        # The response may be SSE-framed or JSON. Check for session id in headers.
        client_sid = None
        for key, value in resp_hdrs.items():
            if key.lower() == "mcp-session-id":
                client_sid = value
                break

        # Also check if it's in the SSE body
        if not client_sid and body:
            body_str = body.decode(errors="replace")
            # SSE format: look for mcp-session-id in headers
            # The gateway may or may not forward this header
            if "mcp-session-id" in body_str.lower():
                # Try to extract from body (some gateways embed it)
                pass

        # In stateless mode the DROP is the expected result: it is the finding
        # that rules stateless mode out for a stateful backend. Assert it as a
        # pass so this probe stays green and stays re-runnable as a regression
        # guard, rather than recording a permanent, misleading failure.
        if stateful_mode == "stateless":
            time.sleep(0.2)
            backend_issued = StatefulBackend.session_id
            check("stateless: control - the backend DID issue a session id",
                  backend_issued is not None,
                  f"backend session_id={backend_issued}")
            check("stateless: Q1 - the gateway DROPS it, so browser continuity "
                  "is impossible (this is why Playwright needs stateful)",
                  client_sid is None,
                  f"client_sid={client_sid!r} backend_issued={backend_issued!r}")
            print(f"      -> gateway dropped backend session id {backend_issued}")
            return

        check("stateful: Q1 - the client receives a session id",
              client_sid is not None,
              f"client_sid={str(client_sid)[:40]!r}...")

        if not client_sid:
            return

        # Question 2: when the client echoes the gateway's session id, does the
        # BACKEND land on the same browser session?
        status2, resp_hdrs2, body2 = mcp_request(
            gw_port, "/playwright/mcp", token,
            method="tools/call", session_id=client_sid)

        time.sleep(0.2)
        tools_records = [r for r in StatefulBackend.records if r["method"] == "tools/call"]
        if not tools_records:
            check("stateful: Q2 - backend received tools/call",
                  False, f"no tools/call records, status={status2}")
            return
        last = tools_records[-1]
        backend_received_sid = last["received_sid"]
        # The gateway issues its OWN opaque session id and translates it back to
        # the backend's, so equality with client_sid is the WRONG assertion.
        # What matters is that the backend recognised its own session. A side
        # benefit worth recording: the client never learns the backend's real
        # session id, so the backend's id space is not directly addressable.
        check("stateful: Q2 - the gateway translates its session id to the backend's",
              backend_received_sid is not None
              and backend_received_sid != client_sid
              and backend_received_sid == StatefulBackend.session_id,
              f"backend_received={str(backend_received_sid)[:40]!r} "
              f"client_sent={str(client_sid)[:24]!r}...")
        check("stateful: Q2 - backend session matched (browser continuity works)",
              last.get("sid_match") is True,
              f"sid_match={last.get('sid_match')}")

        # SECURITY: stateful mode turns a session id into a handle on live
        # browser state, so prove it is not also a credential. The other routes
        # chose stateless precisely to avoid "a session id becoming a second,
        # unauthenticated way to address a backend session"
        # (apps/mcp-gateway/values.yaml:405-407). This route has no backend
        # credential to cache, but that argument only holds if every request is
        # still authenticated AND still scope-gated, not just `initialize`.
        n_before = len(StatefulBackend.records)

        status_no, _, body_no = mcp_request(
            gw_port, "/playwright/mcp", None,
            method="tools/call", session_id=client_sid)
        check("stateful: a valid session id with NO Authorization is refused",
              status_no in (401, 403), f"status={status_no} body={body_no[:120]!r}")

        status_bad, _, body_bad = mcp_request(
            gw_port, "/playwright/mcp", "not-a-real-token",
            method="tools/call", session_id=client_sid)
        check("stateful: a valid session id with a garbage token is refused",
              status_bad in (401, 403), f"status={status_bad} body={body_bad[:120]!r}")

        # A well-formed token carrying a DIFFERENT route's scope must also be
        # refused: this proves the CEL scope gate re-applies on session
        # continuation requests and is not evaluated only at initialize.
        token_other = mint_token(f"http://127.0.0.1:{idp_port}", scope="mcp_context7")
        status_sc, _, body_sc = mcp_request(
            gw_port, "/playwright/mcp", token_other,
            method="tools/call", session_id=client_sid)
        check("stateful: a session id does not bypass the mcp_playwright scope gate",
              status_sc in (401, 403), f"status={status_sc} body={body_sc[:120]!r}")

        status_forge, _, body_forge = mcp_request(
            gw_port, "/playwright/mcp", token,
            method="tools/call", session_id="forged-session-id")
        check("stateful: a forged session id is not silently accepted",
              status_forge in (400, 401, 403, 404),
              f"status={status_forge} body={body_forge[:120]!r}")

        time.sleep(0.2)
        check("stateful: none of the refused requests reached the backend",
              len(StatefulBackend.records) == n_before,
              f"records before={n_before} after={len(StatefulBackend.records)}")

    finally:
        proc.terminate()
        try:
            proc.wait(timeout=5)
        except subprocess.TimeoutExpired:
            proc.kill()
        log_fh.close()
        idp_srv.shutdown()
        backend_srv.shutdown()
        import shutil
        shutil.rmtree(work, ignore_errors=True)


def main():
    print(f"agentgateway binary: {BIN}")
    print(f"python: {sys.version.split()[0]}")

    if not os.path.isfile(BIN):
        print(f"ERROR: agentgateway binary not found at {BIN}")
        return 1

    ver = subprocess.run([BIN, "--version"], capture_output=True, text=True, timeout=10)
    ver_info = json.loads(ver.stdout)
    print(f"agentgateway version: {ver_info.get('version')}")
    check("pinned version matches", ver_info.get("version") == PINNED_VERSION,
          f"got {ver_info.get('version')}")

    # Test stateless mode first (what the live routes use)
    run_probe("stateless", "stateless")

    # Test stateful mode (what Playwright may need)
    run_probe("stateful", "stateful")

    print(f"\n{'='*60}")
    passed = sum(results)
    failed = len(results) - passed
    print(f"TOTAL {passed}/{len(results)} passed, {failed} failed")
    return 1 if failed else 0


if __name__ == "__main__":
    sys.exit(main())
