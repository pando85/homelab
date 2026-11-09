#!/usr/bin/env python3
"""Grafana backend checks: JWT identity propagation config + mcp-grafana delegation.

Reproducible, disposable, local-only. Runs real Grafana and real mcp-grafana containers on a
private docker network against a local throwaway issuer. Nothing touches a cluster. Every
subject, login and email is generated and uses the reserved ".example" TLD; no real identity
appears anywhere in this file or its output.

Plane A - Grafana auth.jwt with the target configuration
  A1 boots and serves with the target config
  A2 unknown subject refused            (auto_sign_up = false)
  A3 wrong issuer refused              (expect_claims.iss)
  A4 wrong audience refused            (expect_claims.aud)
  A5 native org roles enforced         (Editor writes, Viewer 403)
  A6 a ("jwt", sub) alias survives switching the trusted client, with roles intact

Plane B - mcp-grafana as the gateway backend
  B1 starts with no ambient Grafana credential
  B2 /healthz answers "ok" on the side listener, which is the only probe path that works
  B3 the canonical backend Host is accepted and any other Host is refused (exact allowlist)
  B4 --disable-write really is read-only: writers disappear, and the generic request tool
     loses its body argument and collapses its method enum to GET only
  B5 each caller's own bearer reaches Grafana, so one server process yields Editor success
     and Viewer 403 for the identical tool call
  B6 a caller with no bearer gets no Grafana data (no ambient-token leak)
  B7 caller authentication plus a forwarded Authorization header is refused at startup

    python3 test/mcp-gateway/grafana-backend-checks.py

Requires: docker, python3 with PyJWT and cryptography.
Environment: GRAFANA_IMAGE, MCP_GRAFANA_IMAGE, KEEP=1 to leave containers and workdir behind.
"""

import base64
import http.server
import json
import os
import shutil
import socket
import ssl
import subprocess
import sys
import tempfile
import threading
import time
import uuid

import jwt as pyjwt
from cryptography.hazmat.primitives.asymmetric import ec

GRAFANA_IMAGE = os.environ.get("GRAFANA_IMAGE", "docker.io/grafana/grafana:13.3.0-distroless")
MCP_IMAGE = os.environ.get("MCP_GRAFANA_IMAGE", "docker.io/grafana/mcp-grafana:2.1.1")

NET = "gbc-net"
GRAFANA_C, MCP_C = "gbc-grafana", "gbc-mcp"
GRAFANA_PORT, ISSUER_PORT = 14701, 14700
MCP_PUBLIC_PORT, MCP_HEALTHZ_PORT = 14702, 14703
ADMIN_USER, ADMIN_PW = "gbc-admin", "gbc-admin-pw"

# Canonical in-cluster Host for the backend: <service>.<namespace>.svc.cluster.local:<port>
CANONICAL_HOST = "grafana-mcp.grafana-mcp.svc.cluster.local:8000"

# The client Grafana trusts today, and the one it will trust after the cutover.
LEGACY_CLIENT = "grafana-legacy"
TARGET_CLIENT = "mcp-gateway"

WORK = os.environ.get("GBC_WORK") or tempfile.mkdtemp(prefix="grafana-backend-checks-")
DATA = os.path.join(WORK, "grafana-data")
CA_HOST = os.path.join(WORK, "issuer-ca.pem")
CA_CONTAINER = "/etc/grafana/issuer-ca.pem"

os.makedirs(DATA, exist_ok=True)
KEY = ec.generate_private_key(ec.SECP256R1())

EDITOR = {"sub": str(uuid.uuid4()), "login": "gbc-subject-a", "email": "gbc-a@example"}
VIEWER = {"sub": str(uuid.uuid4()), "login": "gbc-subject-b", "email": "gbc-b@example"}

ISSUER_BASE = None


def docker(*args):
    return subprocess.run(["docker", *args], capture_output=True, text=True)


def curl(*args):
    return subprocess.run(["curl", "-sS", "-m", "25", *args],
                          capture_output=True, text=True).stdout


def jwks():
    numbers = KEY.public_key().public_numbers()

    def coord(value):
        return base64.urlsafe_b64encode(value.to_bytes(32, "big")).rstrip(b"=").decode()

    return json.dumps({"keys": [{"kty": "EC", "crv": "P-256", "kid": "gbc",
                                 "x": coord(numbers.x), "y": coord(numbers.y)}]})


def mint(sub, aud=TARGET_CLIENT, iss=TARGET_CLIENT, username=None, email=None):
    """Sign a token with the throwaway local key. Never pointed at a real IdP."""
    now = int(time.time())
    claims = {
        "iss": f"{ISSUER_BASE}/oauth2/openid/{iss}",
        "sub": sub,
        "aud": aud,
        "iat": now,
        "nbf": now,
        "exp": now + 600,
        "jti": str(uuid.uuid4()),
    }
    if username:
        claims["preferred_username"] = username
    if email:
        claims["email"] = email
    return pyjwt.encode(claims, KEY, algorithm="ES256", headers={"kid": "gbc"})


class Issuer(http.server.BaseHTTPRequestHandler):
    """Serves one key under every client path, so a per-client JWKS switch is testable."""

    def do_GET(self):
        if self.path.endswith("/public_key.jwk"):
            body = jwks().encode()
            self.send_response(200)
            self.send_header("Content-Type", "application/json")
            self.end_headers()
            self.wfile.write(body)
            return
        self.send_response(404)
        self.end_headers()

    def log_message(self, *_args):
        pass


def start_issuer():
    global ISSUER_BASE
    gateway = docker("network", "inspect", "bridge", "-f",
                     "{{range .IPAM.Config}}{{.Gateway}}{{end}}").stdout.strip() or "127.0.0.1"
    ISSUER_BASE = f"https://{gateway}:{ISSUER_PORT}"
    subprocess.run(
        ["openssl", "req", "-x509", "-newkey", "ec", "-pkeyopt", "ec_paramgen_curve:prime256v1",
         "-nodes", "-keyout", os.path.join(WORK, "issuer.key"), "-out", CA_HOST,
         "-days", "1", "-subj", "/CN=gbc-issuer",
         "-addext", f"subjectAltName=IP:127.0.0.1,IP:{gateway}"],
        check=True, capture_output=True)
    ctx = ssl.SSLContext(ssl.PROTOCOL_TLS_SERVER)
    ctx.load_cert_chain(CA_HOST, os.path.join(WORK, "issuer.key"))
    server = http.server.ThreadingHTTPServer(("0.0.0.0", ISSUER_PORT), Issuer)
    server.socket = ctx.wrap_socket(server.socket, server_side=True)
    threading.Thread(target=server.serve_forever, daemon=True).start()


def grafana_env(trusted_client):
    """Target [auth.jwt] config, exactly as it renders into grafana.ini.

    There is deliberately no `audience` key: Grafana ignores it for auth.jwt. Audience is
    enforced only through expect_claims, which is a single JSON string holding issuer AND
    audience.
    """
    expect = json.dumps({"iss": f"{ISSUER_BASE}/oauth2/openid/{trusted_client}",
                         "aud": trusted_client}, separators=(",", ":"))
    return [
        f"GF_SERVER_HTTP_PORT={GRAFANA_PORT}",
        f"GF_SERVER_ROOT_URL=http://127.0.0.1:{GRAFANA_PORT}",
        f"GF_SECURITY_ADMIN_USER={ADMIN_USER}",
        f"GF_SECURITY_ADMIN_PASSWORD={ADMIN_PW}",
        "GF_ANALYTICS_REPORTING_ENABLED=false",
        "GF_USERS_AUTO_ASSIGN_ORG=true",
        "GF_USERS_AUTO_ASSIGN_ORG_ROLE=Viewer",
        "GF_AUTH_OAUTH_ALLOW_INSECURE_EMAIL_LOOKUP=false",
        "GF_AUTH_JWT_ENABLED=true",
        "GF_AUTH_JWT_HEADER_NAME=Authorization",
        f"GF_AUTH_JWT_JWK_SET_URL={ISSUER_BASE}/oauth2/openid/{trusted_client}/public_key.jwk",
        f"GF_AUTH_JWT_TLS_CLIENT_CA={CA_CONTAINER}",
        "GF_AUTH_JWT_CACHE_TTL=60s",
        "GF_AUTH_JWT_USERNAME_CLAIM=sub",
        "GF_AUTH_JWT_EMAIL_CLAIM=email",
        "GF_AUTH_JWT_AUTO_SIGN_UP=false",
        "GF_AUTH_JWT_SKIP_ORG_ROLE_SYNC=true",
        f"GF_AUTH_JWT_EXPECT_CLAIMS={expect}",
    ]


def start_grafana(trusted_client):
    docker("rm", "-f", GRAFANA_C)
    os.chmod(DATA, 0o777)
    args = ["run", "-d", "--name", GRAFANA_C, "--network", NET,
            "-p", f"127.0.0.1:{GRAFANA_PORT}:{GRAFANA_PORT}",
            "-v", f"{DATA}:/var/lib/grafana",
            "-v", f"{CA_HOST}:{CA_CONTAINER}:ro"]
    for item in grafana_env(trusted_client):
        args += ["-e", item]
    args.append(GRAFANA_IMAGE)
    if docker(*args).returncode:
        raise SystemExit(f"grafana failed to start trusting {trusted_client}")
    if not wait_for(f"http://127.0.0.1:{GRAFANA_PORT}/api/health"):
        raise SystemExit("grafana never became healthy")


def start_mcp(allowed_hosts=CANONICAL_HOST, forward="Authorization", server_token=None,
              disable_write=True):
    docker("rm", "-f", MCP_C)
    env = {
        "GRAFANA_URL": f"http://{GRAFANA_C}:{GRAFANA_PORT}",
        "GRAFANA_USAGE_STATS": "disabled",
    }
    if forward:
        env["GRAFANA_FORWARD_HEADERS"] = forward
    if server_token:
        env["MCP_GRAFANA_SERVER_TOKEN"] = server_token
    args = ["run", "-d", "--name", MCP_C, "--network", NET,
            "--entrypoint", "/app/mcp-grafana",
            "-p", f"127.0.0.1:{MCP_PUBLIC_PORT}:8000",
            "-p", f"127.0.0.1:{MCP_HEALTHZ_PORT}:8081"]
    for key, value in env.items():
        args += ["-e", f"{key}={value}"]
    args.append(MCP_IMAGE)
    args += ["--transport", "streamable-http", "--address", "0.0.0.0:8000",
             "--healthz-address", ":8081", "--allowed-hosts", allowed_hosts]
    if disable_write:
        args.append("--disable-write")
    if docker(*args).returncode:
        raise SystemExit("mcp-grafana failed to start")
    return env


def wait_for(url, expect="200", tries=90, host_header=None, match_body=False):
    """Poll url. By default compares the HTTP code; with match_body compares the body text."""
    for _ in range(tries):
        args = ["-H", f"Host: {host_header}"] if host_header else []
        if not match_body:
            args += ["-o", "/dev/null", "-w", "%{http_code}"]
        args.append(url)
        out = curl(*args).strip()
        if match_body:
            if out == expect:
                return out
        elif out == expect:
            return True
        time.sleep(1)
    return None if match_body else False


def admin(method, path, body=None):
    args = ["-X", method, "-u", f"{ADMIN_USER}:{ADMIN_PW}", "-w", "\n@@%{http_code}",
            f"http://127.0.0.1:{GRAFANA_PORT}{path}"]
    if body is not None:
        args += ["-H", "Content-Type: application/json", "-d", json.dumps(body)]
    payload, _, status = curl(*args).rpartition("\n@@")
    try:
        return status.strip(), json.loads(payload or "{}")
    except json.JSONDecodeError:
        return status.strip(), {}


def whoami(token):
    args = ["-w", "\n@@%{http_code}", f"http://127.0.0.1:{GRAFANA_PORT}/api/user",
            "-H", f"Authorization: Bearer {token}"]
    payload, _, status = curl(*args).rpartition("\n@@")
    try:
        return status.strip(), json.loads(payload or "{}")
    except json.JSONDecodeError:
        return status.strip(), {}


def write_dashboard(token, title):
    uid = "gbc" + uuid.uuid4().hex[:8]
    args = ["-X", "POST", "-w", "\n@@%{http_code}",
            f"http://127.0.0.1:{GRAFANA_PORT}/api/dashboards/db",
            "-H", "Content-Type: application/json",
            "-H", f"Authorization: Bearer {token}",
            "-d", json.dumps({"dashboard": {"title": title, "uid": uid, "panels": []},
                              "overwrite": False})]
    _, _, status = curl(*args).rpartition("\n@@")
    return status.strip()


def mcp(token, method, params, host=CANONICAL_HOST):
    body = {"jsonrpc": "2.0", "id": str(uuid.uuid4()), "method": method, "params": params}
    args = ["-X", "POST", "-w", "\n@@%{http_code}",
            f"http://127.0.0.1:{MCP_PUBLIC_PORT}/mcp",
            "-H", "Content-Type: application/json",
            "-H", "Accept: application/json, text/event-stream",
            "-H", f"Host: {host}", "-d", json.dumps(body)]
    if token:
        args += ["-H", f"Authorization: Bearer {token}"]
    payload, _, status = curl(*args).rpartition("\n@@")
    text = payload.strip()
    if text.startswith("data:"):
        text = text.split("data:", 1)[1].strip()
    try:
        return status.strip(), json.loads(text)
    except json.JSONDecodeError:
        return status.strip(), {"raw": text[:300]}


def tool_names(payload):
    return [t.get("name", "") for t in (payload.get("result") or {}).get("tools", [])]


def tool_props(payload, name):
    for tool in (payload.get("result") or {}).get("tools", []):
        if tool.get("name") == name:
            return tool.get("inputSchema", {}).get("properties", {})
    return {}


def result_text(payload):
    try:
        return " ".join(c.get("text", "") for c in payload["result"]["content"])
    except (KeyError, TypeError):
        return json.dumps(payload)[:200]


CHECKS = []


def check(label, passed, detail=""):
    CHECKS.append((label, bool(passed), detail))
    print(f"{'PASS' if passed else 'FAIL':4} {label}")
    if detail:
        print(f"     {detail}")


def plane_a():
    print("\n--- Plane A: Grafana auth.jwt target config ---")
    check("A1 boots and serves with target config", True)

    for principal, role in ((EDITOR, "Editor"), (VIEWER, "Viewer")):
        _, created = admin("POST", "/api/admin/users", {
            "name": principal["login"], "login": principal["login"],
            "email": principal["email"], "password": "gbc-subject-pw"})
        principal["id"] = created.get("id")
        admin("PATCH", f"/api/org/users/{principal['id']}", {"role": role})
        # Bootstrap the ("jwt", sub) alias while the legacy client is still trusted.
        code, _ = whoami(mint(principal["sub"], aud=LEGACY_CLIENT, iss=LEGACY_CLIENT,
                              username=principal["login"], email=principal["email"]))
        if code != "200":
            raise SystemExit(f"bootstrap failed ({principal['login']}): HTTP {code}")

    editor = mint(EDITOR["sub"], aud=LEGACY_CLIENT, iss=LEGACY_CLIENT)
    viewer = mint(VIEWER["sub"], aud=LEGACY_CLIENT, iss=LEGACY_CLIENT)

    code, _ = whoami(mint(str(uuid.uuid4()), aud=LEGACY_CLIENT, iss=LEGACY_CLIENT))
    check("A2 unknown subject refused (auto_sign_up=false)", code in ("401", "403"),
          f"HTTP {code}")

    code, _ = whoami(mint(EDITOR["sub"], aud=LEGACY_CLIENT, iss="wrong-issuer"))
    check("A3 wrong issuer refused (expect_claims.iss)", code in ("401", "403"), f"HTTP {code}")

    code, _ = whoami(mint(EDITOR["sub"], aud="wrong-audience", iss=LEGACY_CLIENT))
    check("A4 wrong audience refused (expect_claims.aud, no audience key set)",
          code in ("401", "403"), f"HTTP {code}")

    code = write_dashboard(editor, "gbc-editor-write")
    check("A5a Editor native role writes", code == "200", f"HTTP {code}")
    code = write_dashboard(viewer, "gbc-viewer-write")
    check("A5b Viewer native role refused", code in ("401", "403"), f"HTTP {code}")

    # The cutover: trust a different client, different issuer, different audience, different
    # JWKS path, same data directory.
    start_grafana(TARGET_CLIENT)
    code, body = whoami(mint(EDITOR["sub"]))
    check("A6a after client switch same subject resolves same user",
          code == "200" and body.get("id") == EDITOR["id"],
          f"HTTP {code} id={body.get('id')} expected={EDITOR['id']}")
    code = write_dashboard(mint(EDITOR["sub"]), "gbc-post-switch")
    check("A6b Editor role preserved across switch", code == "200", f"HTTP {code}")
    code = write_dashboard(mint(VIEWER["sub"]), "gbc-post-switch")
    check("A6c Viewer still refused across switch", code in ("401", "403"), f"HTTP {code}")
    code, _ = whoami(mint(EDITOR["sub"], iss=LEGACY_CLIENT, aud=LEGACY_CLIENT))
    check("A6d legacy-issued token no longer trusted after switch",
          code in ("401", "403"), f"HTTP {code}")


def plane_b():
    print("\n--- Plane B: mcp-grafana backend ---")
    start_mcp()
    answered = wait_for(f"http://127.0.0.1:{MCP_HEALTHZ_PORT}/healthz",
                        expect="ok", match_body=True)
    container = docker("inspect", "-f", "{{range .Config.Env}}{{println .}}{{end}}", MCP_C).stdout
    check("B1 starts with no ambient Grafana credential",
          "GRAFANA_SERVICE_ACCOUNT_TOKEN" not in container,
          "GRAFANA_SERVICE_ACCOUNT_TOKEN absent")

    check("B2 /healthz answers 'ok' on side listener (probe path)", answered,
          f"body={answered!r}")
    code = curl("-o", "/dev/null", "-w", "%{http_code}", "-H", f"Host: {CANONICAL_HOST}",
                f"http://127.0.0.1:{MCP_PUBLIC_PORT}/health").strip()
    print(f"     note: /health on the main listener -> HTTP {code}; not a valid probe")

    _, listed = mcp(None, "tools/list", {})
    canonical_ok = "result" in listed
    code_foreign, foreign = mcp(None, "tools/list", {}, host="127.0.0.1:9999")
    refused = "host not allowed" in json.dumps(foreign).lower() or code_foreign == "403"
    check("B3 canonical Host accepted, other Host refused (exact allowlist, no wildcard)",
          canonical_ok and refused,
          f"canonical ok={canonical_ok} foreign -> HTTP {code_foreign} "
          f"{result_text(foreign)[:60]!r}")

    names = tool_names(listed)
    props = tool_props(listed, "grafana_api_request")
    writers_gone = "update_dashboard" not in names
    get_only = props.get("method", {}).get("enum") == ["GET"]
    no_body = "body" not in props
    read_present = "search_dashboards" in names
    check("B4 --disable-write is a complete read-only guard",
          writers_gone and get_only and no_body and read_present,
          f"tools={len(names)} writers_gone={writers_gone} method={props.get('method', {}).get('enum')} "
          f"body_arg_present={not no_body} read_tool={read_present}")

    _, blocked = mcp(mint(EDITOR["sub"]), "tools/call",
                     {"name": "update_dashboard",
                      "arguments": {"dashboard": {"title": "gbc-must-not-be-created"}}})
    check("B4b privileged writer tool not invocable on the read-only backend",
          "unknown tool" in json.dumps(blocked).lower(), result_text(blocked)[:90])

    # B5/B6 need a writable backend so Grafana's own RBAC is what decides. Synthetic subjects.
    start_mcp(disable_write=False)
    wait_for(f"http://127.0.0.1:{MCP_HEALTHZ_PORT}/healthz", expect="ok", match_body=True)

    _, ed = mcp(mint(EDITOR["sub"], username=EDITOR["login"], email=EDITOR["email"]),
                "tools/call", {"name": "update_dashboard", "arguments": {
                    "dashboard": {"title": "gbc-edge-" + uuid.uuid4().hex[:6],
                                  "uid": "gbcv" + uuid.uuid4().hex[:8], "panels": []},
                    "overwrite": False}})
    _, vw = mcp(mint(VIEWER["sub"], username=VIEWER["login"], email=VIEWER["email"]),
                "tools/call", {"name": "update_dashboard", "arguments": {
                    "dashboard": {"title": "gbc-edge-" + uuid.uuid4().hex[:6],
                                  "uid": "gbcv" + uuid.uuid4().hex[:8], "panels": []},
                    "overwrite": False}})
    ed_text, vw_text = result_text(ed), result_text(vw)
    check("B5 caller's own bearer reaches Grafana (Editor ok, Viewer 403, one process)",
          "success" in ed_text.lower() and "403" in vw_text,
          f"editor={ed_text[:60]!r} viewer={vw_text[:60]!r}")

    _, anon = mcp(None, "tools/call", {"name": "search_dashboards", "arguments": {}})
    anon_text = result_text(anon)
    # Positive control first: the same read tool DOES work for a real caller, so a failure
    # here is specifically about the missing bearer and not a broken tool or a coincidental
    # substring inside a Go struct dump.
    _, authed = mcp(mint(EDITOR["sub"]), "tools/call",
                    {"name": "search_dashboards", "arguments": {}})
    authed_text = result_text(authed)
    control_ok = '"isError": true' not in json.dumps(authed)
    denied = "Unauthorized" in anon_text or "auth.unauthorized" in anon_text
    check("B6 caller with no bearer gets no Grafana data (no ambient token)",
          control_ok and denied,
          f"bearer_call_ok={control_ok} no-bearer-says={anon_text[:70]!r}")

    start_mcp(server_token="some-caller-token")
    up = wait_for(f"http://127.0.0.1:{MCP_HEALTHZ_PORT}/healthz", expect="ok",
                  match_body=True, tries=15)
    logs = docker("logs", MCP_C).stdout + docker("logs", MCP_C).stderr
    reason = next((ln for ln in logs.splitlines() if "refusing to start" in ln), "")
    check("B7 caller auth + forwarded Authorization refused at startup",
          up is None and "refusing to start" in logs, reason[:150] or f"healthz={up!r}")


def main():
    for port in (GRAFANA_PORT, ISSUER_PORT, MCP_PUBLIC_PORT, MCP_HEALTHZ_PORT):
        with socket.socket() as probe:
            if probe.connect_ex(("127.0.0.1", port)) == 0:
                raise SystemExit(f"port {port} already in use; aborting instead of colliding")

    print(f"grafana image : {GRAFANA_IMAGE}")
    print(f"mcp image     : {MCP_IMAGE}")
    print(f"workdir       : {WORK}")
    print(f"python        : {sys.version.split()[0]}")
    print(f"docker        : {docker('--version').stdout.strip()}")

    start_issuer()
    docker("network", "create", NET)
    start_grafana(LEGACY_CLIENT)
    plane_a()
    plane_b()

    passed = sum(1 for _, ok, _ in CHECKS if ok)
    print(f"\n{passed}/{len(CHECKS)} checks passed")
    failed = [label for label, ok, _ in CHECKS if not ok]
    if failed:
        print("failed: " + "; ".join(failed))

    # Reported, not asserted away. On this config a foreign subject carrying a known login and
    # email still resolves to the existing user, because Grafana falls back to email/login
    # matching once a token has validated. No Grafana config closes that; the gate has to be
    # the gateway's check on the edge token.
    probe = mint(str(uuid.uuid4()), username=EDITOR["login"], email=EDITOR["email"])
    code, body = whoami(probe)
    print(f"\nnote  foreign-subject-with-known-identity: HTTP {code} id={body.get('id')} "
          f"(that is user {EDITOR['id']})")
    print("      Kanidm 1.11.2 access tokens (OAuth2RFC9068Token + ...Extensions) carry only "
          "iss/sub/aud/exp/nbf/iat/jti/client_id plus scope as one space-separated string, "
          "and set typ=at+jwt; email, preferred_username and groups exist only in the id_token. "
          "So a gateway that requires typ=at+jwt, the exact scope and no email/preferred_username "
          "claims forwards subject-only tokens and this fallback becomes unreachable.")
    return 1 if failed else 0


if __name__ == "__main__":
    try:
        sys.exit(main())
    finally:
        if os.environ.get("KEEP"):
            print(f"kept: {WORK} {GRAFANA_C} {MCP_C}")
        else:
            docker("rm", "-f", GRAFANA_C, MCP_C)
            docker("network", "rm", NET)
            shutil.rmtree(WORK, ignore_errors=True)
