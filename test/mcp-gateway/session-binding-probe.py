#!/usr/bin/env python3
"""Can agentgateway v1.7.0 bind an MCP session to ONE caller, natively?

Why this exists. vault-mcp v0.2.0 caches the Vault credential per MCP *session*: the
token presented at `initialize` is what every later call on that session executes as.
The owner's two-user gate is therefore: caller B, with their own valid credential, must
never be executed as caller A through a shared session. Encrypting the session id is
irrelevant to that - it protects the value, not who may use it.

Upstream source says a resumed session is re-checked only against the issuing backend
and the target *conditions*, never against the caller's identity:
  crates/agentgateway/src/mcp/session.rs  get_session / get_or_resume_session:
      if entry.backend_id != builder.backend_id
          || !entry.session.relay.upstreams.conditions_match(&builder.backend, ctx)
      { return None }
  crates/agentgateway/src/mcp/upstream/mod.rs:545 conditions_match compares, per
      conditioned target, whether session membership still equals the CEL result.

So the candidate native mechanisms are MEASURED here, not assumed:

  1. baseline                        - stateful backend, one target
  2. statefulMode: stateless         - "create one session per request"
  3. per-caller target `condition`   - conditions on the validated `jwt.sub`

The backend stub emulates vault-mcp v0.2.0 exactly: it captures the credential at
`initialize` and thereafter acts as that credential for every request carrying the same
*backend* session id, and reports the identity it acted as in the tool result. No
cluster, no real Vault, no real credentials.

    python3 test/mcp-gateway/session-binding-probe.py [path/to/agentgateway]

The config starts from the real chart render (test/mcp-gateway/chartconfig.py, route
`firecrawl`, `credential: none` so the caller's gateway bearer is stripped) plus one
asserted test-only patch: `x-vault-token` is NOT removed, because in the Vault model
that header is the caller's own credential. Everything else is production shape.
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
import uuid

from cryptography.hazmat.primitives.asymmetric import ec
import jwt as pyjwt

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import chartconfig  # noqa: E402

AGENTGATEWAY_VERSION = "v1.7.0"
WORK = os.environ.get("MCP_E2E_WORK") or tempfile.mkdtemp(prefix="mcp-gw-bind-")
BACKEND_PORT = 14801
ISSUER_PORT = 14802
GW_PORT = 14800
ROUTE = "firecrawl"             # the chart's public route name; reused as a shell
BACKEND_URL = f"http://127.0.0.1:{BACKEND_PORT}/mcp"

KEY = ec.generate_private_key(ec.SECP256R1())
PUB = KEY.public_key().public_numbers()
ISSUER = f"http://127.0.0.1:{ISSUER_PORT}"
AUD = "mcp-gateway"
SCOPE = "mcp_firecrawl"

SUB_A = "aaaaaaaa-1111-4111-8111-111111111111"
SUB_B = "bbbbbbbb-2222-4222-8222-222222222222"
TOK_A = "hvs.caller-A-own-vault-token"
TOK_B = "hvs.caller-B-own-vault-token"

VAULT_SESSIONS = {}     # backend session id -> credential captured at initialize
SEEN = []
LOCK = threading.Lock()


def b64u(num, size):
    return base64.urlsafe_b64encode(num.to_bytes(size, "big")).rstrip(b"=").decode()


JWKS = {"keys": [{"kty": "EC", "crv": "P-256", "x": b64u(PUB.x, 32), "y": b64u(PUB.y, 32),
                 "alg": "ES256", "use": "sig", "kid": "bind"}]}


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


class VaultLikeBackend(http.server.BaseHTTPRequestHandler):
    """MCP server that caches the credential per backend session, like vault-mcp 0.2.0."""

    def log_message(self, *a):
        pass

    def do_POST(self):
        n = int(self.headers.get("Content-Length") or 0)
        try:
            req = json.loads(self.rfile.read(n) or b"{}")
        except Exception:
            req = {}
        method = req.get("method", "")
        client_sid = self.headers.get("Mcp-Session-Id", "")
        vault_tok = self.headers.get("X-Vault-Token", "")
        bearer = self.headers.get("Authorization", "")
        send_sid = None
        with LOCK:
            SEEN.append({"path": self.path, "method": method, "backend_sid": client_sid,
                         "x-vault-token": vault_tok, "authorization_present": bool(bearer)})
            if method == "initialize":
                send_sid = f"backend-{uuid.uuid4().hex[:8]}"
                VAULT_SESSIONS[send_sid] = vault_tok or "(none)"
                acting = vault_tok or "(none)"
            else:
                acting = VAULT_SESSIONS.get(client_sid, "(unknown session)")
        if method == "initialize":
            result = {"protocolVersion": "2025-06-18", "capabilities": {"tools": {}},
                      "serverInfo": {"name": "vault-like", "version": "0.2.0"}}
        elif method in ("tools/list", "server/discover"):
            result = {"tools": [{"name": "read_secret", "description": "reads",
                                 "inputSchema": {"type": "object"}}]}
        elif method == "tools/call":
            result = {"content": [{"type": "text",
                                   "text": json.dumps({"acting_as": acting})}],
                      "isError": False}
        else:
            result = {}
        body = json.dumps({"jsonrpc": "2.0", "id": req.get("id", 1), "result": result}).encode()
        self.send_response(200)
        self.send_header("Content-Type", "application/json")
        if send_sid:
            self.send_header("Mcp-Session-Id", send_sid)
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)


def mint(sub):
    now = int(time.time())
    return pyjwt.encode({
        "iss": ISSUER, "sub": sub, "aud": AUD, "client_id": AUD,
        "iat": now, "nbf": now, "exp": now + 600, "jti": f"{sub}-{now}",
        "scope": f"openid email profile {SCOPE}", "session_id": sub},
        KEY, algorithm="ES256", headers={"kid": "bind", "typ": "at+jwt"})


def rpc(method, token=None, sid=None, vault_token=None, params=None):
    cmd = ["curl", "-sS", "-m", "10", "-D", os.path.join(WORK, "h.txt"),
           "-o", os.path.join(WORK, "b.txt"), "-w", "%{http_code}",
           f"http://127.0.0.1:{GW_PORT}/{ROUTE}/mcp",
           "-H", "Content-Type: application/json",
           "-H", "Accept: application/json, text/event-stream",
           "-H", "mcp-protocol-version: 2025-06-18"]
    if token:
        cmd += ["-H", f"Authorization: Bearer {token}"]
    if sid:
        cmd += ["-H", f"Mcp-Session-Id: {sid}"]
    if vault_token:
        cmd += ["-H", f"X-Vault-Token: {vault_token}"]
    payload = {"jsonrpc": "2.0", "id": 1, "method": method}
    if method == "initialize" or params is not None:
        payload["params"] = params if params is not None else {
            "protocolVersion": "2025-06-18", "capabilities": {},
            "clientInfo": {"name": "bind-probe", "version": "0"}}
    cmd += ["-d", json.dumps(payload)]
    code = subprocess.run(cmd, capture_output=True, text=True).stdout.strip()
    new_sid = sid
    for line in open(os.path.join(WORK, "h.txt")):
        if line.lower().startswith("mcp-session-id:"):
            new_sid = line.split(":", 1)[1].strip()
    body = open(os.path.join(WORK, "b.txt")).read()
    m = re.search(r'acting_as\\?"\s*:\s*\\?"([^"\\]+)', body)
    return code, new_sid, body, (m.group(1) if m else None)


def base_config():
    """The real chart route, adapted to the Vault credential model (one asserted patch)."""
    cfg = chartconfig.render(
        "apps/mcp-gateway", "mcp-gateway", "mcp-gateway",
        [f"backends.{ROUTE}.enabled=true",
         f"backends.{ROUTE}.url={BACKEND_URL}",
         f"backends.{ROUTE}.hostHeader=127.0.0.1:{BACKEND_PORT}",
         f"backends.{ROUTE}.resource=http://127.0.0.1:{GW_PORT}/{ROUTE}/mcp",
         f"backends.{ROUTE}.credential=none",
         f"auth.issuer={ISSUER}",
         f"auth.jwksUrl={ISSUER}/jwks",
         f"gateway.port={GW_PORT}"],
        ("mcpAuthentication", "- authorization", "- x-vault-token"))
    if "passthrough" in cfg:
        raise RuntimeError("credential=none must not render backendAuth.passthrough")
    # Test-only, asserted: X-Vault-Token is the caller's own credential for a Vault
    # backend, so it has to reach the server. Nothing else is changed.
    patched = cfg.replace("                    - x-vault-token\n", "", 1)
    if patched == cfg:
        raise RuntimeError("x-vault-token removal not found - template changed, probe stale")
    return patched


def patch(cfg, old, new, label):
    if old not in cfg:
        raise RuntimeError(f"{label}: expected rendered text not found: {old!r}")
    return cfg.replace(old, new, 1)


def boot(cfg, name):
    path = os.path.join(WORK, f"{name}.yaml")
    open(path, "w").write(cfg)
    log = open(os.path.join(WORK, f"{name}.log"), "w")
    env = dict(os.environ)
    env["SESSION_KEY"] = "0f" * 32       # production uses the chart's generated Secret
    proc = subprocess.Popen([BINARY, "-f", path], stdout=log, stderr=subprocess.STDOUT,
                            start_new_session=True, env=env)
    for _ in range(40):
        time.sleep(0.5)
        if proc.poll() is not None:
            print(f"   [{name}] gateway exited:\n" + open(path.replace(".yaml", ".log")).read()[-1200:])
            return None
        try:
            with socket.create_connection(("127.0.0.1", GW_PORT), timeout=1):
                return proc
        except OSError:
            continue
    print(f"   [{name}] gateway never came up")
    return None


def experiment(label, cfg, notes, attack_names=()):
    print("=" * 78)
    print(f"EXPERIMENT: {label}")
    for n in notes:
        print(f"   {n}")
    proc = boot(cfg, re.sub(r"\W+", "_", label)[:28].strip("_") or "gw")
    if proc is None:
        return "BOOT-FAILED"
    try:
        with LOCK:
            VAULT_SESSIONS.clear()
            del SEEN[:]
        tokA, tokB = mint(SUB_A), mint(SUB_B)

        code, sidA, _, _ = rpc("initialize", token=tokA, vault_token=TOK_A)
        print(f"\n   1. A initialize                      -> http {code}, "
              f"session issued {bool(sidA)}")
        if code != "200":
            print(f"      {open(os.path.join(WORK, 'b.txt')).read()[:200]}")
            return "ERROR"

        # Discover first: with more than one target the gateway namespaces tool names,
        # so a fixed name would fail for reasons that have nothing to do with isolation.
        code_l, _, body_l, _ = rpc("tools/list", token=tokA, sid=sidA, vault_token=TOK_A,
                                   params={})
        names = re.findall(r'"name"\s*:\s*"([^"]+)"', body_l)
        tool_a = names[0] if names else "read_secret"
        print(f"   2. A tools/list                      -> http {code_l}, visible tool "
              f"names {names[:6]}")

        code, _, _, acting = rpc("tools/call", token=tokA, sid=sidA, vault_token=TOK_A,
                                 params={"name": tool_a, "arguments": {}})
        print(f"   3. A tools/call on own session       -> http {code}, acted as "
              f"{acting!r} (want {TOK_A!r})")

        code, _, body, acting = rpc("tools/call", token=tokB, sid=sidA, vault_token=TOK_B,
                                    params={"name": tool_a, "arguments": {}})
        print(f"   4. B replays A's session id, own token-> http {code}, acted as {acting!r}")
        if acting == TOK_A:
            verdict = "LEAK"
            print("      => GATE BROKEN: B was served with A's cached credential")
        elif acting == TOK_B:
            verdict = "OK"
            print("      => safe: the backend used B's own credential")
        else:
            verdict = "REFUSED"
            print(f"      => B's replay did not ride A's session "
                  f"(acting_as={acting!r}, body={body[:90].strip()!r})")

        code_i, sidB, _, _ = rpc("initialize", token=tokB, vault_token=TOK_B)
        _, _, body_lb, _ = rpc("tools/list", token=tokB, sid=sidB, vault_token=TOK_B, params={})
        names_b = re.findall(r'"name"\s*:\s*"([^"]+)"', body_lb)
        tool_b = names_b[0] if names_b else tool_a
        code_c, _, _, acting_b = rpc("tools/call", token=tokB, sid=sidB, vault_token=TOK_B,
                                     params={"name": tool_b, "arguments": {}})
        print(f"   5. B own session                     -> init http {code_i}, names "
              f"{names_b[:6]}, call http {code_c}, acted as {acting_b!r} "
              f"(want {TOK_B!r})")
        print(f"      client session ids distinct: {sidA != sidB}; "
              f"backend sessions cached: {len(VAULT_SESSIONS)}")
        with LOCK:
            calls = [s for s in SEEN if s["method"] == "tools/call"]
            who = {"": "(absent)", TOK_A: "A", TOK_B: "B"}
            print("      X-Vault-Token the backend saw on each tools/call, in order: "
                  + ", ".join(who.get(c["x-vault-token"], "?") for c in calls)
                  + "; caller Authorization forwarded: "
                    f"{any(c['authorization_present'] for c in calls)}")

        # 6. guessing another caller's namespaced tool name must not work either
        candidates = list(dict.fromkeys(list(attack_names) + [n for n in names if n != tool_a]))
        for on in candidates[:3]:
            code_x, _, body_x, acting_x = rpc("tools/call", token=tokB, sid=sidB,
                                              vault_token=TOK_B,
                                              params={"name": on, "arguments": {}})
            print(f"   6. B (own session) calls A's tool name {on!r} -> http {code_x}, "
                  f"acted as {acting_x!r}   body={body_x[:70].strip()!r}")
            if acting_x == TOK_A:
                verdict = "LEAK"
                print("      => GATE BROKEN through a namespaced tool name")
        return verdict
    finally:
        os.killpg(os.getpgid(proc.pid), signal.SIGTERM)
        proc.wait(timeout=15)


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
    threading.Thread(target=lambda: http.server.ThreadingHTTPServer(
        ("127.0.0.1", ISSUER_PORT), Issuer).serve_forever(), daemon=True).start()
    threading.Thread(target=lambda: http.server.ThreadingHTTPServer(
        ("127.0.0.1", BACKEND_PORT), VaultLikeBackend).serve_forever(), daemon=True).start()

    base = base_config()
    single_target = ("            - name: firecrawl\n"
                     "              mcp:\n"
                     "                # Fixed cluster service address from values, never a\n"
                     "                # caller-derived URL.\n"
                     f"                host: {BACKEND_URL}\n")
    results = {}

    results["1 baseline"] = experiment(
        "1 baseline: chart route, stateful, one target", base,
        ["this is a Vault-shaped route with no extra protection"])

    stateless = patch(base, "      - mcp:\n          targets:\n",
                      "      - mcp:\n          statefulMode: stateless\n          targets:\n",
                      "statefulMode")
    results["2 stateless"] = experiment(
        "2 statefulMode: stateless", stateless,
        ["McpStatefulMode: 'create one per request (Stateless)'"])

    two_targets = (f"            - name: caller-a\n"
                   f"              condition: \"jwt.sub == '{SUB_A}'\"\n"
                   f"              mcp:\n"
                   f"                host: {BACKEND_URL}\n"
                   f"            - name: caller-b\n"
                   f"              condition: \"jwt.sub == '{SUB_B}'\"\n"
                   f"              mcp:\n"
                   f"                host: {BACKEND_URL}\n")
    conds = patch(base, single_target, two_targets, "per-caller conditions")
    results["3 jwt.sub conditions"] = experiment(
        "3 per-caller target conditions on validated jwt.sub", conds,
        ["two targets on the SAME backend host, each gated by jwt.sub",
         "multiplexing now applies, so tool names may be prefixed by target name"],
        attack_names=["caller-a_read_secret"])

    print("=" * 78)
    print("SUMMARY - does a session-caching backend serve B as A?")
    for k, v in results.items():
        meaning = {"LEAK": "does NOT satisfy the two-user gate",
                   "OK": "satisfies it (per-request credential)",
                   "REFUSED": "satisfies it (session resume refused)",
                   "BOOT-FAILED": "config rejected by the pinned binary",
                   "ERROR": "probe error"}[v]
        print(f"   {k:<22} {v:<10} {meaning}")
    print(f"\n(work dir {WORK})")
    return 0


if __name__ == "__main__":
    sys.exit(main())
