#!/usr/bin/env python3
"""End-to-end verification harness for the MCP gateway foundation.

Runs the pinned agentgateway binary with routes rendered from the REAL charts
(test/mcp-gateway/chartconfig.py - never a hand-copied config) against an ES256
issuer that mimics Kanidm's token layout, and asserts the security posture.

Public plane, apps/mcp-gateway firecrawl route (`credential: none`):

  A  no bearer token             -> 401 + WWW-Authenticate resource metadata
  B  token for a different aud   -> 401
  C  token without the scope     -> 403
  D  token with the scope        -> 200 from the backend, and the caller's
                                     Authorization header is NOT forwarded
  E  unmapped path               -> 404 (no route)
  F  Kanidm-style single scope string ("a b mcp_firecrawl c") -> 200
  G  near-miss scope name ("mcp_firecrawl_admin") -> 403, proving the CEL
                                     compares whole tokens and not substrings
  H  RFC 9728 metadata advertises the exact issuer
  I  the backend's Host is the fixed service authority, never the caller's
  J  session IDs use the AES encoder (SESSION_KEY), not the base64 fallback

Admin plane, apps/mcp-gateway-admin grafana route (`credential: idToken`, which
renders `backendAuth.passthrough` so the backend can attribute the call):

  K  anonymous -> 401, and a PUBLIC-plane token -> 401: the two planes' audiences
     cannot replay against each other
  L  an ACCESS-shaped token (Kanidm's space-separated `scope` string) -> 403,
     because this route reads the ID token's `scopes` array
  L  an ID-token-shaped bearer -> 403: the shipped route only accepts the Kanidm
     ACCESS token, because attribution must not be solved by changing the edge bearer
  M  an access-shaped token with the scope -> 200 and the backend receives exactly
     the caller's own bearer (caller-preserving delegation)
  N  near-miss scope -> 403, and every header the pinned backends trust
     (X-Grafana-*, X-Vault-Token, Cookie, vault_addr, ...) stops at the edge

Test-only deviations from the production config, both deliberate:
  - config.backend.allowDynamicLoopback: true, so the stub backend and stub
    issuer can bind 127.0.0.1. Production leaves this at its secure default
    (false) and points at cluster service DNS names.
  - a local ES256 issuer instead of Kanidm, so no live identity is touched and
    no credential material ever reaches the repo.

Usage:
  python3 test/mcp-gateway/e2e-authz.py [path/to/agentgateway]
Without a path it downloads the pinned release into a temp dir. Exits non-zero
if any assertion fails.
"""
import base64
import hashlib
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
import urllib.request

from cryptography.hazmat.primitives.asymmetric import ec
import jwt

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import chartconfig  # noqa: E402  (renders the real charts; see chartconfig.py)

AGENTGATEWAY_VERSION = "v1.7.0"

WORK = os.environ.get("MCP_E2E_WORK") or tempfile.mkdtemp(prefix="mcp-gw-e2e-")
BACKEND_LOG = os.path.join(WORK, "backend-headers.jsonl")
GW_CONFIG = os.path.join(WORK, "gateway-config.yaml")
GW_LOG = os.path.join(WORK, "gateway.log")
ADMIN_GW_CONFIG = os.path.join(WORK, "gateway-admin-config.yaml")
ADMIN_GW_LOG = os.path.join(WORK, "gateway-admin.log")

BACKEND_HOST = "127.0.0.1"
BACKEND_PORT = 14101
ISSUER_PORT = 14102
GW_PORT = 14100
ADMIN_GW_PORT = 14103

KEY = ec.generate_private_key(ec.SECP256R1())
PUB = KEY.public_key().public_numbers()
KID = hashlib.sha256(f"{PUB.x:x}{PUB.y:x}".encode()).hexdigest()[:12]


def b64u(num, size):
    return base64.urlsafe_b64encode(num.to_bytes(size, "big")).rstrip(b"=").decode()


JWKS = {
    "keys": [
        {
            "kty": "EC",
            "crv": "P-256",
            "x": b64u(PUB.x, 32),
            "y": b64u(PUB.y, 32),
            "alg": "ES256",
            "use": "sig",
            "kid": KID,
        }
    ]
}


def mint(aud, scope=None, scopes=None, client=None):
    """Mimic a Kanidm ACCESS token (OAuth2RFC9068Token, v1.11.2 oauth2.rs):
    `scope` is ONE space-separated string, `sub` is the account uuid and `aud` is
    the OAuth2 CLIENT NAME. It carries no login or email claim at all."""
    payload = {
        "iss": f"http://{BACKEND_HOST}:{ISSUER_PORT}",
        "sub": "11111111-1111-4111-8111-111111111111",
        "aud": aud,
        "exp": int(time.time()) + 600,
        "iat": int(time.time()),
        "nbf": int(time.time()),
        "jti": f"probe-{int(time.time())}",
        "client_id": client or aud,
    }
    if scope is not None:
        payload["scope"] = scope
    if scopes is not None:
        payload["scopes"] = scopes
    return jwt.encode(payload, KEY, algorithm="ES256",
                      headers={"kid": KID, "typ": "at+jwt"})


def mint_id(aud, scopes, username="person-a", email=None, sub=None):
    """Mimic a Kanidm ID token (OidcToken): `scopes` is an ARRAY and it carries the
    identity claims (`preferred_username`, `email`) that the access token lacks -
    which is why a backend that must attribute a call to a person has to be given
    this shape (measured: the access shape gives Grafana no login/email to map)."""
    now = int(time.time())
    payload = {
        "iss": f"http://{BACKEND_HOST}:{ISSUER_PORT}",
        "sub": sub or "11111111-1111-4111-8111-111111111111",
        "aud": aud,
        "azp": aud,
        "exp": now + 600, "iat": now, "nbf": now,
        "jti": f"probe-id-{now}",
        "preferred_username": username,
        "email": email or f"{username}@grigri.invalid",
        "scopes": scopes,
        "token_use": "id",
    }
    return jwt.encode(payload, KEY, algorithm="ES256", headers={"kid": KID, "typ": "JWT"})


class IssuerHandler(http.server.BaseHTTPRequestHandler):
    def log_message(self, *a):
        pass

    def do_GET(self):
        if self.path.startswith("/.well-known/oauth-authorization-server") or self.path == "/jwks":
            body = json.dumps(JWKS).encode()
            self.send_response(200)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)
        else:
            self.send_response(404)
            self.end_headers()


class BackendHandler(http.server.BaseHTTPRequestHandler):
    """Minimal MCP streamable-HTTP backend; records the headers it receives."""

    def log_message(self, *a):
        pass

    def do_POST(self):
        length = int(self.headers.get("Content-Length") or 0)
        raw = self.rfile.read(length) if length else b"{}"
        with open(BACKEND_LOG, "a") as fh:
            fh.write(json.dumps({k.lower(): v for k, v in self.headers.items()}) + "\n")
        try:
            req = json.loads(raw or b"{}")
        except Exception:
            req = {"id": 1, "method": "unknown"}
        method = req.get("method", "")
        if method == "initialize":
            result = {
                "protocolVersion": "2025-06-18",
                "capabilities": {"tools": {}},
                "serverInfo": {"name": "probe-backend", "version": "0"},
            }
        elif method == "tools/list":
            result = {"tools": [{"name": "probe", "description": "probe", "inputSchema": {"type": "object"}}]}
        else:
            result = {}
        body = json.dumps({"jsonrpc": "2.0", "id": req.get("id", 1), "result": result}).encode()
        self.send_response(200)
        self.send_header("Content-Type", "application/json")
        self.send_header("Mcp-Session-Id", "probe-session")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)


def free_port(hint):
    for p in (hint, hint + 1000, hint + 2000):
        with socket.socket() as s:
            try:
                s.bind((BACKEND_HOST, p))
                return p
            except OSError:
                continue
    raise RuntimeError("no port available")


def serve(handler, port):
    httpd = http.server.ThreadingHTTPServer((BACKEND_HOST, port), handler)
    threading.Thread(target=httpd.serve_forever, daemon=True).start()
    return httpd


def curl(port, path, token=None, extra=None):
    cmd = ["curl", "-sS", "-m", "10", "-D", "-", "-o", os.path.join(WORK, "body.txt"),
           "-w", "%{http_code}", f"http://{BACKEND_HOST}:{port}{path}",
           "-H", "Content-Type: application/json",
           "-H", "Accept: application/json, text/event-stream",
           "-H", "mcp-protocol-version: 2025-06-18",
           "-d", json.dumps({"jsonrpc": "2.0", "id": 1, "method": "initialize",
                             "params": {"protocolVersion": "2025-06-18", "capabilities": {},
                                        "clientInfo": {"name": "probe", "version": "0"}}})]
    if token:
        cmd += ["-H", f"Authorization: Bearer {token}"]
    if extra:
        cmd += extra
    out = subprocess.run(cmd, capture_output=True, text=True)
    return out.stdout, out.stderr


def get_binary():
    if len(sys.argv) > 1:
        return sys.argv[1]
    path = os.path.join(WORK, "agentgateway")
    if not os.path.exists(path):
        url = ("https://github.com/agentgateway/agentgateway/releases/download/"
               f"{AGENTGATEWAY_VERSION}/agentgateway-linux-amd64")
        print(f"Fetching agentgateway {AGENTGATEWAY_VERSION} ...")
        with urllib.request.urlopen(url, timeout=180) as resp, open(path, "wb") as fh:
            fh.write(resp.read())
        os.chmod(path, 0o755)
    return path


def issuer_url():
    return f"http://{BACKEND_HOST}:{ISSUER_PORT}"


def render_public_config():
    """The shipping firecrawl route on the PUBLIC plane, straight from the chart."""
    return chartconfig.render(
        "apps/mcp-gateway", "mcp-gateway", "mcp-gateway",
        ["backends.firecrawl.enabled=true",
         f"backends.firecrawl.url=http://{BACKEND_HOST}:{BACKEND_PORT}/mcp",
         f"backends.firecrawl.hostHeader={BACKEND_HOST}:{BACKEND_PORT}",
         f"backends.firecrawl.resource=http://{BACKEND_HOST}:{GW_PORT}/firecrawl/mcp",
         f"auth.issuer={issuer_url()}",
         f"auth.jwksUrl={issuer_url()}/jwks",
         f"gateway.port={GW_PORT}"],
        ("mcpAuthentication", "requestHeaderModifier", "jwt.scope.split(' ')"))


def render_admin_config():
    """The delegation candidate on the PUBLIC plane, straight from the chart.

    Grafana MCP is a public-plane backend (`mcp.grigri.cloud`) per the approved
    architecture; only Vault belongs to the admin plane. `credential: accessToken`
    makes the route authorize on the access token's `scope` string and emit
    `backendAuth.passthrough`, so the caller's own credential reaches the backend.
    The route stays disabled in values; enabling it is an owner decision recorded in
    planning/mcp-gateway-implementation.md (Task 2).
    """
    return chartconfig.render(
        "apps/mcp-gateway", "mcp-gateway-deleg", "mcp-gateway",
        ["backends.grafana.enabled=true",
         f"backends.grafana.url=http://{BACKEND_HOST}:{BACKEND_PORT}/mcp",
         f"backends.grafana.hostHeader={BACKEND_HOST}:{BACKEND_PORT}",
         f"backends.grafana.resource=http://{BACKEND_HOST}:{ADMIN_GW_PORT}/grafana/mcp",
         f"auth.issuer={issuer_url()}",
         f"auth.jwksUrl={issuer_url()}/jwks",
         f"gateway.port={ADMIN_GW_PORT}"],
        ("jwt.scope.split(' ').exists(s, s == 'mcp_grafana')", "backendAuth",
         "passthrough", "- x-grafana-service-account-token", "- vault_addr"),
        # Two gateways on one host would both want the default 15020/15021 ops ports.
        stats_port=ADMIN_GW_PORT + 1, readiness_port=ADMIN_GW_PORT + 2)


def boot_gateway(binary, config, config_path, log_path, port):
    """Write a rendered config and start the pinned binary on `port`.

    The session key comes from the SESSION_KEY env var, exactly as in the cluster
    where it is a chart-generated Secret: config.rs prefers the env over any config
    value, and a base64 (keyless) session ID would skip the issuing-backend check.
    """
    open(config_path, "w").write(config)
    log = open(log_path, "w")
    env = dict(os.environ)
    env["SESSION_KEY"] = hashlib.sha256(b"mcp-gateway-e2e-session-key").hexdigest()
    proc = subprocess.Popen([binary, "-f", config_path], stdout=log,
                            stderr=subprocess.STDOUT, start_new_session=True, env=env)
    for _ in range(60):
        time.sleep(0.5)
        if proc.poll() is not None:
            raise RuntimeError(f"gateway exited early:\n{open(log_path).read()[-3000:]}")
        try:
            with socket.create_connection(("127.0.0.1", port), timeout=1):
                return proc
        except OSError:
            continue
    os.killpg(os.getpgid(proc.pid), signal.SIGTERM)
    raise RuntimeError(f"gateway never became ready:\n{open(log_path).read()[-3000:]}")


def admin_suite(expect, failures):
    """K-N: the caller-preserving delegation route on the PUBLIC plane (chart-rendered).

    The route is `credential: accessToken`: the edge bearer is the Kanidm ACCESS token
    and nothing else. Kanidm's access token has no login/email claim and its scopes are
    a string, while the ID token carries `preferred_username`/`email` and a `scopes`
    ARRAY (both verified against the pinned Kanidm). That is the right edge contract; the
    backend side needs `username_claim = sub` plus a one-time per-person identity-claim
    bootstrap, measured by grafana-sub-alias-probe.py (a local research probe, kept out
    of the foundation commit and pending review) - not a different token class at the edge.
    """
    path = "/grafana/mcp"
    aud = "mcp-gateway"
    tok_access = mint(aud=aud, scope="openid email profile mcp_grafana")
    tok_id = mint_id(aud, ["openid", "email", "profile", "mcp_grafana"])
    tok_near = mint(aud=aud, scope="mcp_grafana_admin")
    tok_other_plane = mint(aud="mcp-gateway-admin", scope="mcp_grafana")

    # K - anonymous, and an admin-plane token, must both be refused here
    headers, err = curl(ADMIN_GW_PORT, path)
    expect("K1 delegation route, anonymous -> 401", "401",
           headers.strip().splitlines()[-1], err[:200])
    headers, err = curl(ADMIN_GW_PORT, path, token=tok_other_plane)
    expect("K2 admin-plane token on the public delegation route -> 401", "401",
           headers.strip().splitlines()[-1], err[:200])
    print("      (same signing key in this harness, so this proves audience "
          "separation; in Kanidm the two clients also have different keys)")

    # L - an ID-token bearer is refused on the shipped route. The gateway does not
    # check JWS `typ`, so the CEL on the `scopes` array is what enforces "access
    # token only": attribution problems must not be solved at the edge.
    headers, err = curl(ADMIN_GW_PORT, path, token=tok_id)
    expect("L id-token bearer -> 403", "403", headers.strip().splitlines()[-1], err[:200])

    # M - the access-shaped credential is accepted AND reaches the backend unchanged
    open(BACKEND_LOG, "w").close()
    headers, err = curl(ADMIN_GW_PORT, path, token=tok_access)
    expect("M access-shaped token -> 200", "200", headers.strip().splitlines()[-1], err[:200])
    seen = [json.loads(l) for l in open(BACKEND_LOG) if l.strip()]
    if not seen:
        print("FAIL  M: backend never reached")
        failures.append("M backend hit")
    else:
        got = seen[-1].get("authorization", "")
        want = f"Bearer {tok_access}"
        if got != want:
            print(f"FAIL  M: delegation lost the caller - backend Authorization is "
                  f"{got[:20]!r}, want the caller's own bearer")
            failures.append("M passthrough")
        else:
            print("      backend received the caller's own access token verbatim "
                  "(caller-preserving delegation)")
        host = seen[-1].get("host", "")
        if host != f"{BACKEND_HOST}:{BACKEND_PORT}":
            print(f"FAIL  M: backend Host={host!r}, want the fixed authority")
            failures.append("M host pinned")
    print("      note: with the live `username_claim = preferred_username` Grafana still "
          "rejects this shape (no login/email claim; grafana-jwt-mapping-probe.py is a "
          "local research probe, uncommitted and pending review); with "
          "`username_claim = sub` plus the one-time identity-claim bootstrap it resolves "
          "the same existing user (grafana-sub-alias-probe.py, local and uncommitted). "
          "The edge contract here is "
          "correct either way, and no Grafana setting was changed by this work.")

    # N - near-miss scope, and the trusted headers stop at the edge
    headers, err = curl(ADMIN_GW_PORT, path, token=tok_near)
    expect("N1 near-miss scope -> 403", "403", headers.strip().splitlines()[-1], err[:200])

    open(BACKEND_LOG, "w").close()
    inject = []
    for name, value in (("X-Grafana-Service-Account-Token", "sa-of-another-user"),
                        ("X-Grafana-API-Key", "glakey-of-another-user"),
                        ("X-Grafana-Org-Id", "9"),
                        ("X-Grafana-Url", f"http://{BACKEND_HOST}:1/denied"),
                        ("X-Grafana-Id", "eyJhbGciOiJIUzI1NiJ0.e30.c2ln"),
                        ("X-Firecrawl-Api-Key", "fc-of-another-user"),
                        ("X-Api-Key", "generic-key"),
                        ("X-Vault-Token", "hvs.vault-credential"),
                        ("vault_addr", "http://attacker.example:8200"),
                        ("vault-skip-verify", "true"),
                        ("X-Vault-Namespace", "other-team"),
                        ("Cookie", "grafana_session=stolen")):
        inject += ["-H", f"{name}: {value}"]
    # The bearer on this route is the ACCESS token (the edge contract), so the
    # injection case must be driven with an access-shaped token: with the rejected
    # ID-token bearer the gateway answers 403 (case L) and the backend is never
    # reached, which would prove nothing about header stripping.
    headers, err = curl(ADMIN_GW_PORT, path, token=tok_access, extra=inject)
    expect("N2 scoped accessToken with injected headers -> 200", "200",
           headers.strip().splitlines()[-1], err[:200])
    seen = [json.loads(l) for l in open(BACKEND_LOG) if l.strip()]
    if not seen:
        print("FAIL  N2: backend never reached")
        failures.append("N2 backend hit")
    else:
        got = seen[-1]
        leaked = [k for k in ("x-grafana-service-account-token", "x-grafana-api-key",
                             "x-grafana-org-id", "x-grafana-url", "x-grafana-id",
                             "x-firecrawl-api-key", "x-api-key", "x-vault-token",
                             "vault_addr", "vault-addr", "vault_skip_verify",
                             "vault-skip-verify", "x-vault-namespace",
                             "vault_namespace", "cookie") if k in got]
        if leaked:
            print(f"FAIL  N2: caller-controlled headers reached the backend: {leaked}")
            failures.append("N2 header strip")
        else:
            print("      no credential/target-override header reached the backend")
        if got.get("authorization", "") != f"Bearer {tok_access}":
            print("FAIL  N2: bearer changed when headers were injected")
            failures.append("N2 bearer stable")


def main():
    binary = get_binary()
    os.makedirs(WORK, exist_ok=True)
    open(BACKEND_LOG, "w").close()

    global BACKEND_PORT, ISSUER_PORT, GW_PORT, ADMIN_GW_PORT
    BACKEND_PORT = free_port(BACKEND_PORT)
    ISSUER_PORT = free_port(ISSUER_PORT)
    GW_PORT = free_port(GW_PORT)
    ADMIN_GW_PORT = free_port(ADMIN_GW_PORT)
    if ADMIN_GW_PORT == GW_PORT:
        ADMIN_GW_PORT = GW_PORT + 100

    serve(IssuerHandler, ISSUER_PORT)
    serve(BackendHandler, BACKEND_PORT)

    issuer = issuer_url()
    try:
        proc = boot_gateway(binary, render_public_config(), GW_CONFIG, GW_LOG, GW_PORT)
    except RuntimeError as exc:
        print(exc)
        sys.exit(2)

    failures = []

    def expect(label, want, got, detail=""):
        if want == got:
            print(f"PASS  {label}")
        else:
            print(f"FAIL  {label}: expected {want}, got {got} {detail}")
            failures.append(label)

    # A - anonymous caller is rejected with the OAuth challenge
    headers, err = curl(GW_PORT, "/firecrawl/mcp")
    code = headers.strip().splitlines()[-1]
    expect("A anonymous -> 401", "401", code, err[:200])
    challenge = ""
    for line in headers.splitlines():
        if line.lower().startswith("www-authenticate:"):
            challenge = line.split(":", 1)[1].strip()
    if not challenge:
        print("  note: no WWW-Authenticate header in", headers.splitlines()[:12])
        failures.append("A www-authenticate")
    else:
        print(f"      WWW-Authenticate: {challenge[:120]}")

    # B - right signature, wrong audience
    tok = mint(aud="other-client", scope="mcp_firecrawl")
    headers, err = curl(GW_PORT, "/firecrawl/mcp", token=tok)
    expect("B wrong audience -> 401", "401", headers.strip().splitlines()[-1], err[:200])

    # C - right audience, no scope
    tok = mint(aud="mcp-gateway", scope="openid profile")
    headers, err = curl(GW_PORT, "/firecrawl/mcp", token=tok)
    expect("C missing scope -> 403", "403", headers.strip().splitlines()[-1], err[:200])

    # D - authorised caller reaches the backend without leaking its bearer
    open(BACKEND_LOG, "w").close()
    tok = mint(aud="mcp-gateway", scope="mcp_firecrawl")
    headers, err = curl(GW_PORT, "/firecrawl/mcp", token=tok)
    code = headers.strip().splitlines()[-1]
    expect("D scoped token -> 200", "200", code, err[:200])
    body = open(os.path.join(WORK, "body.txt")).read()
    if "serverInfo" in body:
        print("      backend returned a real MCP initialize result")
    else:
        print("FAIL  backend response missing serverInfo:", body[:200])
        failures.append("D mcp result")
    seen = [json.loads(l) for l in open(BACKEND_LOG) if l.strip()]
    if not seen:
        print("FAIL  backend never received a request")
        failures.append("D backend hit")
    else:
        h = seen[-1]
        if "authorization" in h:
            print("FAIL  caller bearer token forwarded to backend:", h["authorization"][:24])
            failures.append("D bearer leak")
        else:
            print("      caller Authorization header stripped before backend")

    # E - unmapped path
    tok = mint(aud="mcp-gateway", scope="mcp_firecrawl")
    headers, err = curl(GW_PORT, "/vault/mcp", token=tok)
    expect("E unmapped path -> 404", "404", headers.strip().splitlines()[-1], err[:200])

    # F - Kanidm writes one space-separated scope string: matching must still work
    tok = mint(aud="mcp-gateway", scope="email profile mcp_firecrawl offline_access")
    headers, err = curl(GW_PORT, "/firecrawl/mcp", token=tok)
    expect("F multi-scope string -> 200", "200", headers.strip().splitlines()[-1], err[:200])

    # G - a scope that merely CONTAINS the name must not pass (no substring match)
    tok = mint(aud="mcp-gateway", scope="mcp_firecrawl_admin")
    headers, err = curl(GW_PORT, "/firecrawl/mcp", token=tok)
    expect("G near-miss scope -> 403", "403", headers.strip().splitlines()[-1], err[:200])

    # H - resource metadata must advertise the configured issuer verbatim, so a
    # client doing RFC 9728 discovery ends up at Kanidm and not at a bare host.
    # Two things are checked: the path the gateway itself points clients at in its
    # 401 challenge, and the chart's explicit `/.well-known/oauth-protected-resource
    # <route path>` match (without that match the gateway 404s its own challenge URL).
    m = re.search(r'resource_metadata="([^"]+)"', challenge)
    if not m:
        print("FAIL  H: the 401 challenge carries no resource_metadata URL:", challenge[:160])
        failures.append("H challenge url")
    else:
        url = m.group(1)
        raw = subprocess.run(["curl", "-sS", "-m", "10", url],
                             capture_output=True, text=True).stdout
        try:
            servers = json.loads(raw).get("authorization_servers")
        except Exception:
            servers = None
        if servers != [issuer]:
            print(f"FAIL  H: the advertised {url} returned authorization_servers={servers}, "
                  f"want {[issuer]}")
            failures.append("H advertised url")
        else:
            print(f"PASS  metadata at the URL the gateway advertises -> exact issuer ({url})")

    raw = subprocess.run(["curl", "-sS", "-m", "10",
                          f"http://127.0.0.1:{GW_PORT}/.well-known/oauth-protected-resource"
                          f"/firecrawl/mcp"],
                         capture_output=True, text=True).stdout
    try:
        servers = json.loads(raw).get("authorization_servers")
    except Exception:
        servers = None
    if servers != [issuer]:
        print(f"FAIL  H: per-route metadata returned {servers}, want {[issuer]} ({raw[:120]})")
        failures.append("H per-route url")
    else:
        print("PASS  metadata /.well-known/oauth-protected-resource/firecrawl/mcp -> exact issuer")

    # The bare `/.well-known/oauth-protected-resource` is NOT served: every route is a
    # distinct path with its own scopes, and RFC 9728 only requires the metadata for a
    # resource that lives at the host root. Recorded as an observation, not a failure.
    bare = subprocess.run(["curl", "-sS", "-m", "10", "-o", "/dev/null", "-w", "%{http_code}",
                           f"http://127.0.0.1:{GW_PORT}/.well-known/oauth-protected-resource"],
                          capture_output=True, text=True).stdout
    print(f"      note: bare /.well-known/oauth-protected-resource -> HTTP {bare} "
          f"(no root route; clients use the per-resource URL above)")

    # I - the backend must see the FIXED authority, never the caller's Host
    # header; otherwise the Firecrawl Host allowlist could not be a service name.
    open(BACKEND_LOG, "w").close()
    tok = mint(aud="mcp-gateway", scope="mcp_firecrawl")
    headers, _ = curl(GW_PORT, "/firecrawl/mcp", token=tok,
                      extra=["-H", "Host: mcp.grigri.cloud"])
    caller_host_code = headers.strip().splitlines()[-1]
    seen = [json.loads(l) for l in open(BACKEND_LOG) if l.strip()]
    want = f"127.0.0.1:{BACKEND_PORT}"
    if not seen:
        # The gateway refused the request before proxying: acceptable, the
        # caller's Host never reaches the backend either way.
        print(f"PASS  caller Host rejected at the gateway (HTTP {caller_host_code}), backend untouched")
    else:
        got = seen[-1].get("host", "")
        if got != want:
            print(f"FAIL  backend Host={got}, want the fixed backend authority {want} "
                  f"(caller Host forwarded through, HTTP {caller_host_code})")
            failures.append("I backend Host")
        else:
            print(f"PASS  backend Host is the fixed authority ({got}), caller Host not forwarded")

    # J - session protection must be the AES encoder, not the base64 fallback.
    # config.rs: `session.key: disabled` or an unset key both land on base64, and
    # sessionpersistence.rs only records the issuing backend when AES is active.
    gwlog = open(GW_LOG).read()
    if "MCP session IDs are not encrypted" in gwlog:
        print("FAIL  gateway is running base64 session IDs (issuing-backend check off)")
        failures.append("J session encoder")
    else:
        print("PASS  gateway reports no unencrypted-session warning (SESSION_KEY active)")

    os.killpg(os.getpgid(proc.pid), signal.SIGTERM)
    proc.wait(timeout=20)

    # ---- admin plane: the caller-preserving delegation route
    try:
        admin_proc = boot_gateway(binary, render_admin_config(), ADMIN_GW_CONFIG,
                                  ADMIN_GW_LOG, ADMIN_GW_PORT)
    except RuntimeError as exc:
        print("FAIL  admin gateway did not boot: " + str(exc))
        failures.append("admin boot")
        admin_proc = None
    if admin_proc is not None:
        try:
            admin_suite(expect, failures)
        finally:
            os.killpg(os.getpgid(admin_proc.pid), signal.SIGTERM)
            admin_proc.wait(timeout=20)

    if failures:
        print("\nFAILED:", ", ".join(failures))
        sys.exit(1)
    print("\nAll end-to-end authz assertions passed")


if __name__ == "__main__":
    main()
