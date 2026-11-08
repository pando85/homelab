#!/usr/bin/env python3
"""Does the pinned gateway hand the caller's bearer token to the backend, and how?

This decides the shape of the Grafana delegation route. mcp-grafana 2.1.1 only sees
the caller's identity if the Authorization header survives the hop
(GRAFANA_FORWARD_HEADERS=Authorization was measured to copy exactly what arrives at
the MCP endpoint - see grafana-forwarding-probe.py, a local research probe that is
not committed with the foundation). So the gateway has to deliver
the caller's validated token upstream, while still being the thing that authenticates
and authorises it.

Three configurations are booted, one at a time, against a recording backend:

  A  route with no auth policy      -> what does the backend see by default?
  B  mcpAuthentication only         -> is the validated bearer still forwarded?
  C  mcpAuthentication + backendAuth.passthrough
     If `passthrough` is not a valid field in this release the gateway refuses to
     load the config, and that refusal is itself the answer we need.

The backend echoes every header it received, so each case prints exactly which
credential and which caller-injected headers reached it.

    python3 test/mcp-gateway/backend-auth-probe.py [path/to/agentgateway]

Local only: loopback listeners, pinned binary, no cluster, no live Grafana.
"""
import base64
import http.server
import json
import os
import signal
import socket
import subprocess
import sys
import tempfile
import threading
import time

from cryptography.hazmat.primitives.asymmetric import ec
import jwt as pyjwt

AGENTGATEWAY_VERSION = "v1.7.0"
WORK = os.environ.get("MCP_E2E_WORK") or tempfile.mkdtemp(prefix="mcp-gw-ba-")
CFG = os.path.join(WORK, "ba-config.yaml")
LOG = os.path.join(WORK, "ba-gateway.log")
GW_PORT = 14400
ISSUER_PORT = 14401
BACKEND_PORT = 14402

KEY = ec.generate_private_key(ec.SECP256R1())
PUB = KEY.public_key().public_numbers()
ISSUER = f"http://127.0.0.1:{ISSUER_PORT}"

SEEN = []


def b64u(num, size):
    return base64.urlsafe_b64encode(num.to_bytes(size, "big")).rstrip(b"=").decode()


JWKS = {"keys": [{"kty": "EC", "crv": "P-256", "x": b64u(PUB.x, 32), "y": b64u(PUB.y, 32),
                 "alg": "ES256", "use": "sig", "kid": "bact"}]}


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
    """Minimal MCP backend that records the headers of every request."""

    def log_message(self, *a):
        pass

    def do_POST(self):
        n = int(self.headers.get("Content-Length") or 0)
        req = json.loads(self.rfile.read(n) or b"{}")
        SEEN.append({k.lower(): v for k, v in self.headers.items()})
        result = {"protocolVersion": "2025-06-18", "capabilities": {"tools": {}},
                  "serverInfo": {"name": "ba-probe", "version": "0"}} \
            if req.get("method") == "initialize" else {}
        body = json.dumps({"jsonrpc": "2.0", "id": req.get("id", 1), "result": result}).encode()
        self.send_response(200)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)


def token(typ_override=None, **extra):
    now = int(time.time())
    claims = {"iss": ISSUER, "sub": "caller-1", "aud": "mcp-gateway-admin",
              "client_id": "mcp-gateway-admin", "iat": now, "nbf": now,
              "exp": now + 300, "jti": str(now), "scope": "openid mcp_grafana",
              "session_id": str(now)}
    claims.update(extra)
    return pyjwt.encode(claims, KEY, algorithm="ES256",
                        headers={"kid": "bact", "typ": typ_override or "at+jwt"})


def write_config(auth_block, backend_auth, extra_remove, expected_typ=None):
    """One route at /grafana/mcp; `auth_block` empty string means no auth policy."""
    pol = ""
    if auth_block:
        pol = f"""    policies:
{auth_block}"""
    # `backendAuth` is a sibling of `policies` inside a target list item.
    bauth = ""
    if backend_auth:
        bauth = "\n" + backend_auth
    remove_block = ""
    if extra_remove:
        remove_block = "\n                  remove:\n" + "\n".join(
            f"                    - {h}" for h in extra_remove)
    cfg = f"""config:
  adminAddr: "off"
  logging:
    format: json
    level: info

gateways:
  default:
    port: {GW_PORT}

routes:
  - name: grafana
    matches:
      - path:
          exact: /grafana/mcp
{pol}
    backends:
      - mcp:
          targets:
            - name: grafana
              mcp:
                host: "http://127.0.0.1:{BACKEND_PORT}/mcp"
              policies:
                requestHeaderModifier:
                  set:
                    host: grafana-mcp.mcp-gateway-admin.svc.cluster.local{remove_block}{bauth}
"""
    open(CFG, "w").write(cfg)


def boot():
    proc = subprocess.Popen([BINARY, "-f", CFG], stdout=open(LOG, "w"),
                            stderr=subprocess.STDOUT, start_new_session=True)
    for _ in range(40):
        time.sleep(0.5)
        if proc.poll() is not None:
            return None, open(LOG).read()
        try:
            with socket.create_connection(("127.0.0.1", GW_PORT), timeout=1):
                return proc, None
        except OSError:
            continue
    return None, open(LOG).read()


def call(auth=None, inject=None):
    with threading.Lock():
        del SEEN[:]
    headers = ["-H", "Content-Type: application/json",
               "-H", "Accept: application/json, text/event-stream",
               "-H", "mcp-protocol-version: 2025-06-18"]
    if auth:
        headers += ["-H", f"Authorization: Bearer {auth}"]
    for k, v in (inject or {}).items():
        headers += ["-H", f"{k}: {v}"]
    body = json.dumps({"jsonrpc": "2.0", "id": 1, "method": "initialize", "params": {
        "protocolVersion": "2025-06-18", "capabilities": {},
        "clientInfo": {"name": "ba-probe", "version": "0"}}})
    code = subprocess.run(["curl", "-sS", "-m", "10", "-o", "/dev/null", "-w", "%{http_code}",
                           f"http://127.0.0.1:{GW_PORT}/grafana/mcp", *headers, "-d", body],
                          capture_output=True, text=True).stdout.strip()
    return code, (SEEN[0] if SEEN else None)


AUTH = f"""      mcpAuthentication:
        mode: strict
        issuer: {ISSUER}
        audiences:
          - mcp-gateway-admin
        jwks:
          url: {ISSUER}/jwks
        resourceMetadata:
          resource: http://127.0.0.1:{GW_PORT}/grafana/mcp
          scopesSupported:
            - mcp_grafana
          bearerMethodsSupported:
            - header
      authorization:
        rules:
          - require: "jwt.scope.split(' ').exists(s, s == 'mcp_grafana')"
"""


def show(label, code, hdrs):
    print(f"\n--- {label}")
    print(f"    gateway http : {code}")
    if hdrs is None:
        print("    backend saw    : (no request reached the backend)")
        return
    for k in ("authorization", "host", "x-grafana-service-account-token", "x-grafana-org-id",
              "x-grafana-url", "x-grafana-id", "mcp-protocol-version"):
        if k in hdrs:
            print(f"    backend saw    : {k}: {hdrs[k][:70]}")


def case(label, auth_block, backend_auth, remove, auth_token=None, inject=None,
         expected_typ=None):
    write_config(auth_block, backend_auth, remove, expected_typ=expected_typ)
    proc, err = boot()
    if proc is None:
        first = [l for l in (err or "").splitlines() if l.strip()]
        print(f"\n--- {label}\n    GATEWAY REJECTED THE CONFIG")
        for l in first[-3:]:
            print("      " + l[:220])
        return
    try:
        code, hdrs = call(auth=auth_token, inject=inject)
        show(label, code, hdrs)
    finally:
        os.killpg(os.getpgid(proc.pid), signal.SIGTERM)
        proc.wait(timeout=15)


def main():
    global BINARY
    BINARY = sys.argv[1] if len(sys.argv) > 1 else os.path.join(WORK, "agentgateway")
    if not os.path.exists(BINARY):
        import urllib.request
        url = ("https://github.com/agentgateway/agentgateway/releases/download/"
               f"{AGENTGATEWAY_VERSION}/agentgateway-linux-amd64")
        print("fetching agentgateway", AGENTGATEWAY_VERSION)
        with urllib.request.urlopen(url, timeout=180) as r, open(BINARY, "wb") as fh:
            fh.write(r.read())
        os.chmod(BINARY, 0o755)

    os.makedirs(WORK, exist_ok=True)
    threading.Thread(target=lambda: http.server.ThreadingHTTPServer(
        ("127.0.0.1", ISSUER_PORT), Issuer).serve_forever(), daemon=True).start()
    threading.Thread(target=lambda: http.server.ThreadingHTTPServer(
        ("127.0.0.1", BACKEND_PORT), Backend).serve_forever(), daemon=True).start()

    CALLER = token()
    INJECT = {"X-Grafana-Service-Account-Token": "SA-SPOOF", "X-Grafana-Org-Id": "7",
              "X-Grafana-URL": "http://127.0.0.1:14499", "X-Grafana-Id": "FORGED"}
    STRIP = ["x-grafana-service-account-token", "x-grafana-api-key", "x-grafana-org-id",
             "x-grafana-id", "x-grafana-url"]
    # backendAuth is a *policy* on the target (SimpleLocalBackendPolicies), sibling of
    # requestHeaderModifier. Verified against schema/config.json at tag v1.7.0.
    PASSTHROUGH = "                backendAuth:\n                  passthrough: {}"
    # An ID-token-shaped credential (typ JWT) is the only Kanidm claim set Grafana's
    # auth.jwt can map to an existing user, so test whether the gateway will accept it.
    IDLIKE = pyjwt.encode({"iss": ISSUER, "sub": "caller-1", "aud": "mcp-gateway-admin",
                           "azp": "mcp-gateway-admin", "iat": int(time.time()),
                           "exp": int(time.time()) + 300, "jti": "x",
                           "preferred_username": "somebody", "email": "somebody@example.invalid",
                           "scopes": ["openid", "email", "profile", "mcp_grafana", "editor"]},
                          KEY, algorithm="ES256", headers={"kid": "bact", "typ": "JWT"})

    print("=" * 72)
    print("A. baseline: no auth policy, no header removals")
    case("A1 caller bearer + injection headers, nothing configured", "", "", [],
         auth_token="opaque-caller-token", inject=INJECT)

    print("\n" + "=" * 72)
    print("B. with mcpAuthentication + authorization (what the chart does today)")
    case("B1 authenticated, nothing removed", AUTH, "", [], auth_token=CALLER)
    case("B2 authenticated, X-Grafana-* stripped", AUTH, "", STRIP,
         auth_token=CALLER, inject=INJECT)
    case("B3 unauthenticated", AUTH, "", STRIP, auth_token=None)
    case("B4 wrong scope", AUTH, "", STRIP, auth_token=token(scope="openid"))

    print("\n" + "=" * 72)
    print("C. delegation: backendAuth.passthrough")
    case("C1 passthrough, X-Grafana-* stripped", AUTH, PASSTHROUGH, STRIP,
         auth_token=CALLER, inject=INJECT)
    case("C2 passthrough, unauthenticated", AUTH, PASSTHROUGH, STRIP, auth_token=None)

    print("\n" + "=" * 72)
    print("D. delegation with expectedTyp JWT (caller presents an ID-token-shaped claim set)")
    AUTH_ID = AUTH.replace("      authorization:\n        rules:\n"
                           "          - require: \"jwt.scope.split(' ').exists(s, s == 'mcp_grafana')\"\n",
                           "      authorization:\n        rules:\n"
                           "          - require: \"jwt.scopes.exists(s, s == 'mcp_grafana')\"\n")
    # v1.7.0 does not check the JWS `typ` at all (jwtValidationOptions only enforces
    # presence of exp/nbf/aud/iss/sub), so an ID-shaped token is accepted if the
    # signature, iss, aud and the CEL rule match.
    case("D1 ID-shaped token + scopes-array CEL + passthrough", AUTH_ID, PASSTHROUGH, STRIP,
         auth_token=IDLIKE)
    case("D2 ID-shaped token against the access-token CEL rule (must fail)", AUTH, PASSTHROUGH,
         STRIP, auth_token=IDLIKE)
    case("D3 passthrough with query-parameter credential", AUTH, PASSTHROUGH, STRIP,
         auth_token=CALLER)

    print("""
Reading of the results
  * If B1/B2 already forward the validated caller token, delegation needs no
    backendAuth block at all - only the header removals.
  * If B1/B2 drop it and C1 boots and forwards, `backendAuth.passthrough` is the
    mechanism to use.
  * Whatever the case, the X-Grafana-* injection headers must be gone at the backend.
""")


if __name__ == "__main__":
    main()
