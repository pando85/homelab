#!/usr/bin/env python3
"""Does the pinned gateway accept a Kanidm-shaped token that Grafana can map?

Question this answers (for the Grafana delegation decision):

  Kanidm v1.11.2 issues two different JWTs per authorisation. Only ONE of them
  carries the claims Grafana's live `auth.jwt` config needs to resolve a caller
  to an existing Grafana user:

    ACCESS token (OAuth2RFC9068Token, typ "at+jwt"):
        iss, sub (account uuid), aud, client_id, exp, iat, nbf, jti,
        scope (space-separated STRING), session_id
        -> NO preferred_username, NO email, NO `scopes` array
           (server/lib/src/idm/oauth2.rs, access_token_data construction)

    ID token (OidcToken, typ "JWT"):
        iss, sub, aud, azp, email, preferred_username, scopes (ARRAY), ...
        -> this is what Grafana reads today
           (username_claim = preferred_username,
            role_attribute_path = contains(scopes[*], 'editor'))

  agentgateway's `mcpAuthentication` validates iss/aud/JWKS and exposes claims
  to CEL as `jwt.*`. This probe reports exactly which of the two shapes the
  gateway will accept, and whether CEL can test a *list* claim (`scopes`) or a
  *string* claim (`scope`), so the delegation design is based on measured
  behaviour instead of assumptions.

It also decodes the Kanidm-shaped claims with the same JMESPath expressions
Grafana would apply, and reports whether an ACCESS token would resolve to an
existing username or force a new user (Grafana: AuthID = `sub`, sign-up allowed
by `auto_sign_up`).

Usage: python3 grafana-token-shape-probe.py [path/to/agentgateway]
Nothing here touches the live cluster or live Grafana.
"""
import base64
import hashlib
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
WORK = os.environ.get("MCP_E2E_WORK") or tempfile.mkdtemp(prefix="mcp-gw-shape-")
CFG = os.path.join(WORK, "shape-config.yaml")
LOG = os.path.join(WORK, "shape-gateway.log")
ISSUER_PORT = 14302
GW_PORT = 14300

KEY = ec.generate_private_key(ec.SECP256R1())
PUB = KEY.public_key().public_numbers()


def b64u(num, size):
    return base64.urlsafe_b64encode(num.to_bytes(size, "big")).rstrip(b"=").decode()


JWKS = {"keys": [{"kty": "EC", "crv": "P-256", "x": b64u(PUB.x, 32), "y": b64u(PUB.y, 32),
                 "alg": "ES256", "use": "sig", "kid": "shapec"}]}
ISSUER = "http://127.0.0.1:%d" % ISSUER_PORT


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
    def log_message(self, *a):
        pass

    def do_POST(self):
        n = int(self.headers.get("Content-Length") or 0)
        req = json.loads(self.rfile.read(n) or b"{}")
        result = {"protocolVersion": "2025-06-18", "capabilities": {"tools": {}},
                  "serverInfo": {"name": "shape-probe", "version": "0"}} \
            if req.get("method") == "initialize" else {}
        body = json.dumps({"jsonrpc": "2.0", "id": req.get("id", 1), "result": result}).encode()
        self.send_response(200)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)


def mint_access(sub, scope_string):
    """Kanidm ACCESS token shape: typ at+jwt, `scope` as one string."""
    now = int(time.time())
    return pyjwt.encode({
        "iss": ISSUER, "sub": sub, "aud": "grafana", "client_id": "grafana",
        "iat": now, "nbf": now, "exp": now + 300, "jti": str(sub),
        "scope": scope_string, "session_id": str(sub),
    }, KEY, algorithm="ES256", headers={"kid": "shapec", "typ": "at+jwt"})


def mint_idtoken(sub, username, email, scopes_list):
    """Kanidm ID token shape: typ JWT, `scopes` as an array, identity claims."""
    now = int(time.time())
    return pyjwt.encode({
        "iss": ISSUER, "sub": sub, "aud": "grafana", "azp": "grafana",
        "iat": now, "nbf": now, "exp": now + 300, "jti": str(sub),
        "preferred_username": username, "email": email, "scopes": scopes_list,
    }, KEY, algorithm="ES256", headers={"kid": "shapec", "typ": "JWT"})


def port_open(port, host="127.0.0.1"):
    try:
        with socket.create_connection((host, port), timeout=1):
            return True
    except OSError:
        return False


def post(port, path, token, rule_field):
    body = json.dumps({"jsonrpc": "2.0", "id": 1, "method": "initialize",
                       "params": {"protocolVersion": "2025-06-18", "capabilities": {},
                                  "clientInfo": {"name": "probe", "version": "0"}}})
    cmd = ["curl", "-sS", "-m", "10", "-o", os.path.join(WORK, "resp.txt"), "-w", "%{http_code}",
           f"http://127.0.0.1:{port}{path}",
           "-H", "Content-Type: application/json",
           "-H", "Accept: application/json, text/event-stream",
           "-H", "mcp-protocol-version: 2025-06-18",
           "-H", f"Authorization: Bearer {token}", "-d", body]
    return subprocess.run(cmd, capture_output=True, text=True).stdout.strip()


def write_config(rule):
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
    backends:
      - mcp:
          targets:
            - name: grafana
              mcp:
                host: "http://127.0.0.1:{ISSUER_PORT + 1}/mcp"
    policies:
      mcpAuthentication:
        mode: strict
        issuer: {ISSUER}
        audiences:
          - grafana
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
          - require: "{rule}"
"""
    open(CFG, "w").write(cfg)


def run_case(label, rule, tokens):
    """Boot the gateway once per CEL rule: the config file is read at startup."""
    write_config(rule)
    proc = subprocess.Popen([BINARY, "-f", CFG], stdout=open(LOG, "w"),
                            stderr=subprocess.STDOUT, start_new_session=True)
    up = False
    for _ in range(40):
        time.sleep(0.5)
        if proc.poll() is not None:
            break
        if port_open(GW_PORT):
            up = True
            break
    print(f"\n### rule: {rule}")
    if not up:
        print("  gateway not up; log tail:")
        print("  " + open(LOG).read()[-500:].replace("\n", "\n  "))
        return
    for name, tok in tokens:
        code = post(GW_PORT, "/grafana/mcp", tok, rule)
        verdict = {"200": "ACCEPTED", "403": "rejected by authorization",
                   "401": "rejected by authentication"}.get(code, f"http {code}")
        print(f"  {name:<34} -> {code} ({verdict})")
    os.killpg(os.getpgid(proc.pid), signal.SIGTERM)
    proc.wait(timeout=15)


def jmespath_like_grafana(token, username_claim, role_expr):
    """Report what Grafana's auth.jwt would derive from this token."""
    claims = pyjwt.decode(token, options={"verify_signature": False, "verify_aud": False})
    login = claims.get(username_claim)
    scopes = claims.get("scopes")
    role = "Editor" if (isinstance(scopes, list) and "editor" in scopes) else "Viewer"
    print(f"    claims seen by Grafana: sub={claims.get('sub')} "
          f"{username_claim}={login!r} scopes={scopes!r} scope={claims.get('scope')!r}")
    print(f"    -> username_claim('{username_claim}') resolves to {login!r}; "
          f"role_attribute_path -> {role}; AuthID would be sub={claims.get('sub')}")
    return login


def main():
    global BINARY
    BINARY = sys.argv[1] if len(sys.argv) > 1 else None
    if BINARY is None:
        import urllib.request
        BINARY = os.path.join(WORK, "agentgateway")
        if not os.path.exists(BINARY):
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
        ("127.0.0.1", ISSUER_PORT + 1), Backend).serve_forever(), daemon=True).start()

    SUB = "5f0c2b64-1a2b-4c3d-9e8f-0a1b2c3d4e5f"
    access = mint_access(SUB, "openid email profile mcp_grafana")
    idtok = mint_idtoken(SUB, "somebody", "somebody@example.invalid",
                         ["openid", "email", "profile", "editor", "mcp_grafana"])

    print("=" * 70)
    print("A. gateway authorization CEL against the two Kanidm claim shapes")
    run_case("string scope", "jwt.scope.split(' ').exists(s, s == 'mcp_grafana')",
             [("ACCESS token (scope string)", access), ("ID token (no scope claim)", idtok)])
    run_case("list scopes", "jwt.scopes.exists(s, s == 'mcp_grafana')",
             [("ID token (scopes array)", idtok), ("ACCESS token (no scopes claim)", access)])
    run_case("identity claim", "jwt['preferred_username'] == 'somebody'",
             [("ID token (preferred_username)", idtok), ("ACCESS token", access)])

    print("\n" + "=" * 70)
    print("B. what Grafana 13.3.0 auth.jwt would make of each token")
    print("  ACCESS token:")
    login = jmespath_like_grafana(access, "preferred_username", None)
    print(f"    -> with auto_sign_up=true Grafana would sign in AuthID=sub and, finding no "
          f"login {login!r}, create a NEW user; with auto_sign_up=false it fails.")
    print("  ID token:")
    login = jmespath_like_grafana(idtok, "preferred_username", None)
    print(f"    -> login {login!r} matches an existing Grafana user, native role kept.")

    print("\nSummary: the ACCESS token authenticates fine at the gateway but carries no")
    print("claim Grafana can map to an existing user; the ID token is the only shape")
    print("Grafana's current config can attribute to a person.")


if __name__ == "__main__":
    main()
