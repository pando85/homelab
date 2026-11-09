#!/usr/bin/env python3
"""What does Grafana 13.3.0 actually do with each Kanidm token shape?

This is the decision test for caller-preserving Grafana delegation. The gateway can
validate a Kanidm JWT and hand it onward (`backendAuth.passthrough`, measured in
backend-auth-probe.py), and mcp-grafana 2.1.1 forwards the caller's Authorization
verbatim (measured in grafana-forwarding-probe.py). The remaining question is what
Grafana itself makes of the token, because that is where identity is either
preserved or silently invented.

It runs the pinned Grafana image locally (no cluster, no live Grafana) configured
the way the repo configures it today:

    [auth.jwt]
      enabled, header_name = Authorization
      jwk_set_url          = <local https stub standing in for Kanidm>
      username_claim       = preferred_username
      email_claim          = email
      auto_sign_up         = true
      skip_org_role_sync   = true
      role_attribute_path  = contains(scopes[*], 'editor') && 'Editor' || 'Viewer'

and asks:

  1. An ACCESS-shaped token (Kanidm's OAuth2RFC9068Token: sub=uuid, scope as a
     space string, NO username/email claims) - does Grafana attribute it to a
     person, or create a user out of the uuid?
  2. An ID-shaped token (preferred_username + scopes array) - does it land on the
     existing user?
  3. Does a role granted natively in Grafana survive later JWT logins
     (skip_org_role_sync), or does role_attribute_path overwrite it?
  4. Do two identities get two different effective permissions (viewer vs editor)
     through the same MCP-configured Grafana?
  5. Is `audience` enforced at all? The repo sets `audience = grafana`, but
     v13.3.0's readAuthJWTSettings() has no such key (pkg/setting/setting_jwt.go),
     so this checks the real behaviour and whether `expect_claims` does the job.

    python3 test/mcp-gateway/grafana-jwt-mapping-probe.py [--expect-claims JSON]

Requires docker and the local JWKS stub it starts itself (self-signed, 127.0.0.1).
"""
import argparse
import json
import os
import re
import shutil
import socket
import ssl
import subprocess
import sys
import tempfile
import threading
import time
import uuid

import http.server

from cryptography.hazmat.primitives.asymmetric import ec
from cryptography.hazmat.primitives import serialization
import jwt as pyjwt

GRAFANA_IMAGE = "docker.io/grafana/grafana:13.3.0-distroless"
GW_PORT = 14500
ISSUER_PORT = 14501
ADMIN_PW = "probe-admin-password"
WORK = os.environ.get("MCP_GRAFANA_PROBE_WORK") or tempfile.mkdtemp(prefix="grafana-jwt-")

ISSUER = f"https://127.0.0.1:{ISSUER_PORT}"

# Two independent signing keys, mirroring Kanidm v1.11.2 where every OAuth2 client
# has its own key pair (verified: /oauth2/openid/<client>/public_key.jwk returns
# different kids per client).
KEYS = {}


def jwk_of(name):
    from cryptography.hazmat.primitives.asymmetric.utils import encode_dss_signature
    key = KEYS[name]
    pub = key.public_key().public_numbers()

    def b64u(n, size):
        import base64
        return base64.urlsafe_b64encode(n.to_bytes(size, "big")).rstrip(b"=").decode()

    return {"kty": "EC", "crv": "P-256", "x": b64u(pub.x, 32), "y": b64u(pub.y, 32),
            "alg": "ES256", "use": "sig", "kid": name}


class Issuer(http.server.BaseHTTPRequestHandler):
    def log_message(self, *a):
        pass

    def do_GET(self):
        client = self.path.strip("/").split(".")[0]        # /grafana.jwk -> grafana
        if client not in KEYS:
            self.send_response(404)
            self.send_header("Content-Length", "0")
            self.end_headers()
            return
        body = json.dumps({"keys": [jwk_of(client)]}).encode()
        self.send_response(200)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)


def mint(client, shape, sub=None, username=None, email=None, scopes=None,
         scope_string=None, aud=None, typ=None):
    """Build a token in the exact shape Kanidm publishes for that flow."""
    now = int(time.time())
    KEYS.setdefault(client, ec.generate_private_key(ec.SECP256R1()))
    aud = aud or client
    if shape == "access":
        # OAuth2RFC9068Token (server/lib/src/idm/oauth2.rs: access_token_data)
        claims = {"iss": ISSUER, "sub": sub, "aud": [aud], "client_id": client,
                  "iat": now, "nbf": now, "exp": now + 300, "jti": str(uuid.uuid4()),
                  "scope": scope_string or "openid email profile mcp_grafana",
                  "session_id": str(uuid.uuid4())}
        typ = typ or "at+jwt"
    else:
        # OidcToken (same file: let oidc = OidcToken { ... })
        claims = {"iss": ISSUER, "sub": sub, "aud": [aud], "azp": client,
                  "iat": now, "nbf": now, "exp": now + 300, "jti": str(uuid.uuid4()),
                  "preferred_username": username, "email": email,
                  "scopes": scopes if scopes is not None else ["openid", "email", "profile"],
                  "token_use": "id"}
        typ = typ or "JWT"
    return pyjwt.encode(claims, KEYS[client], algorithm="ES256",
                        headers={"kid": jwk_of(client)["kid"], "typ": typ})


def api(port, method, path, token=None, admin=False, body=None):
    cmd = ["curl", "-sS", "-m", "15", "-o", os.path.join(WORK, "resp.txt"),
           "-w", "%{http_code}", f"http://127.0.0.1:{port}{path}", "-X", method]
    if admin:
        cmd += ["-u", f"admin:{ADMIN_PW}"]
    if token:
        cmd += ["-H", f"Authorization: Bearer {token}"]
    if body is not None:
        cmd += ["-H", "Content-Type: application/json", "-d", json.dumps(body)]
    code = subprocess.run(cmd, capture_output=True, text=True).stdout.strip()
    try:
        data = json.loads(open(os.path.join(WORK, "resp.txt")).read() or "{}")
    except Exception:
        data = {}
    return code, data


def users(port):
    """All users, by login. Grafana 12+ paginates this endpoint with {"users": [...]}."""
    # /api/admin/users was removed; /api/org/users is the working list in 13.3.0.
    _, rows = api(port, "GET", "/api/org/users", admin=True)
    return {u["login"]: u for u in rows} if isinstance(rows, list) else {}


def org_role(port, user_id):
    """Org role of a user, read from the org member list.

    v13.3.0 has no GET/PUT /api/org/users/{id} or .../role route any more
    (pkg/api/api.go registers only /org/users, /org/users/new, /org/users/invite;
    role management moved to the unified RBAC API), so the list is the read path.
    """
    code, rows = api(port, "GET", "/api/org/users", admin=True)
    if isinstance(rows, list):
        for r in rows:
            if r.get("userId") == user_id:
                return r.get("role")
    return None


def set_role(port, user_id, role):
    """Best-effort native grant: the legacy admin route, then the org route."""
    for path in (f"/api/org/users/{user_id}/role", f"/api/orgs/1/users/{user_id}/role"):
        code, _ = api(port, "PUT", path, admin=True, body={"role": role})
        if code in ("200", "204"):
            return code, path
    return code, None


def create_local_user(port, login, email, role="Viewer"):
    """A user created the way the live ones are: locally/externally, before any JWT login.

    `role` is what the owner cares about: an Editor granted natively in Grafana must
    stay Editor when the same person arrives through the gateway with a token whose
    role_attribute_path would say Viewer.
    """
    code, data = api(port, "POST", "/api/admin/users", admin=True,
                     body={"login": login, "email": email, "name": login,
                           "password": "probe-only-password", "role": role})
    return code, data


def grant(port, user_id, role):
    """Set an org role the way an admin would in the UI/API (a native grant)."""
    last = None
    for path in (f"/api/org/users/{user_id}/role", f"/api/orgs/1/users/{user_id}/role"):
        last = api(port, "PUT", path, admin=True, body={"role": role})
        if last[0] in ("200", "204"):
            return last, path
    return last, None


def who(token):
    code, data = api(GW_PORT, "GET", "/api/user", token=token)
    return code, data


def dash_write(token):
    """A write that needs Editor: creating a dashboard."""
    # A random uid per attempt: reusing one turns "not allowed" into 409 conflict.
    body = {"dashboard": {"uid": f"probe-{uuid.uuid4().hex[:10]}", "title": "probe",
                          "panels": []}, "folderUid": "", "overwrite": False}
    return api(GW_PORT, "POST", "/api/dashboards/db", token=token, body=body)


def make_cert():
    ca = os.path.join(WORK, "jwt-ca.pem")
    if not os.path.exists(ca):
        subprocess.run(["openssl", "req", "-x509", "-newkey", "ec", "-pkeyopt",
                        "ec_paramgen_curve:prime256v1", "-nodes", "-keyout",
                        os.path.join(WORK, "issuer.key"), "-out", ca, "-days", "1",
                        "-subj", "/CN=kanidm-stub",
                        "-addext", "subjectAltName=IP:127.0.0.1"],
                       check=True, capture_output=True)
    return ca


def start_issuer():
    ca = make_cert()
    ctx = ssl.SSLContext(ssl.PROTOCOL_TLS_SERVER)
    ctx.load_cert_chain(ca, os.path.join(WORK, "issuer.key"))
    srv = http.server.ThreadingHTTPServer(("127.0.0.1", ISSUER_PORT), Issuer)
    srv.socket = ctx.wrap_socket(srv.socket, server_side=True)
    threading.Thread(target=srv.serve_forever, daemon=True).start()


def start_grafana(jwk_client="grafana", expect_claims=None, auto_sign_up="true",
                  port=GW_PORT, auto_assign_role="Viewer"):
    ca = os.path.join(WORK, "jwt-ca.pem")
    subprocess.run(["docker", "rm", "-f", "gjwtprobe"], capture_output=True)
    env = [
        "-e", f"GF_SERVER_HTTP_PORT={port}",
        "-e", "GF_SERVER_ROOT_URL="
        f"http://127.0.0.1:{port}/",
        "-e", "GF_SECURITY_ADMIN_USER=admin",
        "-e", f"GF_SECURITY_ADMIN_PASSWORD={ADMIN_PW}",
        "-e", "GF_ANALYTICS_REPORTING_ENABLED=false",
        "-e", "GF_ANALYTICS_CHECK_FOR_UPDATES=false",
        "-e", "GF_USERS_AUTO_ASSIGN_ORG=true",
        "-e", f"GF_USERS_AUTO_ASSIGN_ORG_ROLE={auto_assign_role}",
        "-e", "GF_AUTH_JWT_ENABLED=true",
        "-e", "GF_AUTH_JWT_HEADER_NAME=Authorization",
        "-e", f"GF_AUTH_JWT_JWK_SET_URL={ISSUER}/{jwk_client}.jwk",
        "-e", f"GF_AUTH_JWT_TLS_CLIENT_CA={ca}",
        "-e", "GF_AUTH_JWT_CACHE_TTL=0s",
        "-e", "GF_AUTH_JWT_USERNAME_CLAIM=preferred_username",
        "-e", "GF_AUTH_JWT_EMAIL_CLAIM=email",
        "-e", f"GF_AUTH_JWT_AUTO_SIGN_UP={auto_sign_up}",
        "-e", "GF_AUTH_JWT_SKIP_ORG_ROLE_SYNC=true",
        "-e", "GF_AUTH_JWT_ROLE_ATTRIBUTE_PATH=contains(scopes[*], 'editor') && 'Editor' || 'Viewer'",
        # Present in the live config; kept here to test whether it has any effect.
        "-e", "GF_AUTH_JWT_AUDIENCE=grafana",
    ]
    if expect_claims:
        env += ["-e", f"GF_AUTH_JWT_EXPECT_CLAIMS={expect_claims}"]
    cmd = ["docker", "run", "--name", "gjwtprobe", "--rm", "--network", "host",
           "--user", "0:0", "-v", f"{ca}:{ca}:ro", *env, GRAFANA_IMAGE]
    log = open(os.path.join(WORK, "grafana.log"), "w")
    subprocess.Popen(cmd, stdout=log, stderr=subprocess.STDOUT, start_new_session=True)
    for _ in range(90):
        time.sleep(1)
        try:
            with socket.create_connection(("127.0.0.1", port), timeout=1):
                code, _ = api(port, "GET", "/api/health")
                if code == "200":
                    return True
        except Exception:
            continue
    print("grafana did not become ready; log tail:")
    print(open(os.path.join(WORK, "grafana.log")).read()[-3000:])
    return False


def banner(t):
    print("\n" + "=" * 74 + f"\n{t}\n" + "=" * 74)


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--expect-claims", default=None,
                    help='JSON, e.g. {"iss":"https://127.0.0.1:14501","aud":["grafana","mcp-gateway"]}')
    ap.add_argument("--auto-sign-up", default="true")
    ap.add_argument("--keep", action="store_true", help="leave the container running")
    args = ap.parse_args()

    if not shutil.which("docker"):
        print("docker is required for this probe")
        return 2
    start_issuer()

    ok = start_grafana(expect_claims=args.expect_claims, auto_sign_up=args.auto_sign_up)
    if not ok:
        return 1
    print(f"grafana {GRAFANA_IMAGE} ready on {GW_PORT}; "
          f"jwk_set_url={ISSUER}/grafana.jwk; "
          f"auto_sign_up={args.auto_sign_up}; expect_claims={args.expect_claims or '(unset)'}")

    SUB_A = "5f0c2b64-1a2b-4c3d-9e8f-0a1b2c3d4e5f"
    SUB_B = "7a1d3c75-2b3c-4d5e-8f90-1a2b3c4d5e6f"

    banner("1. ACCESS-shaped token (Kanidm's real MCP bearer: sub=uuid, no username claim)")
    before = set(users(GW_PORT))
    tok = mint("grafana", "access", sub=SUB_A)
    code, me = who(tok)
    print(f"   GET /api/user -> {code}  login={me.get('login')!r} id={me.get('id')}")
    after = users(GW_PORT)
    created = set(after) - before
    print(f"   users created by that call: {sorted(created) or 'none'}")
    for login in sorted(created):
        u = after[login]
        print(f"     login={login!r} name={u.get('name')!r} "
              f"email={u.get('email')!r} org role={org_role(GW_PORT, u['id'])!r}")

    banner("2. Person A already exists locally (as OAuth users do), then logs in with a JWT")
    c, created = create_local_user(GW_PORT, "person-a", "person-a@grigri.invalid")
    existing_id = created.get("id") if isinstance(created, dict) else None
    print(f"   POST /api/admin/users -> {c} id={existing_id} "
          f"(simulates the account Kanidm's generic_oauth already created)")
    n_before = len(users(GW_PORT))
    tokA = mint("grafana", "id", sub=SUB_A, username="person-a",
                email="person-a@grigri.invalid",
                scopes=["openid", "email", "profile", "editor"])
    code, me = who(tokA)
    n_after = len(users(GW_PORT))
    print(f"   GET /api/user -> {code} login={me.get('login')!r} id={me.get('id')}")
    print(f"   user count {n_before} -> {n_after}; same account as the pre-existing one: "
          f"{me.get('id') == existing_id}")
    print(f"   users now: {sorted(users(GW_PORT))}")

    banner("3. Native permissions are not overwritten (and not escalated) by JWT logins")
    uid = me.get("id")
    if uid:
        print(f"   person-a org role before any JWT-tampering: {org_role(GW_PORT, uid)!r}")
        tokA_nos = mint("grafana", "id", sub=SUB_A, username="person-a",
                        email="person-a@grigri.invalid",
                        scopes=["openid", "email", "profile"])  # role_attribute_path -> Viewer
        who(tokA_nos)
        print(f"   after a login whose role_attribute_path says Viewer: role="
              f"{org_role(GW_PORT, uid)!r}   (expect Editor to survive)")
        tokB_esc = mint("grafana", "id", sub=SUB_B, username="person-b",
                        email="person-b@grigri.invalid",
                        scopes=["openid", "email", "profile", "editor"])
        _, mb = who(tokB_esc)
        print(f"   person-b with an 'editor' scope: role={org_role(GW_PORT, mb.get('id'))!r} "
              f"(skip_org_role_sync=true means the token must NOT set roles)")

    banner("4. Two identities, two effective permissions, same Grafana")
    for label, t in (("person-a (native Editor)", tokA),
                     ("person-b (token says editor)", tokB_esc)):
        code, me = who(t)
        w_code, w_body = dash_write(t)
        print(f"   {label:<26} /api/user={code} login={me.get('login')!r} "
              f"org role={org_role(GW_PORT, me.get('id'))!r}  "
              f"POST /api/dashboards/db -> {w_code} {str(w_body.get('message', ''))[:55]}")

    banner("5. Is `audience` enforced? (live config sets audience = grafana)")
    for aud in ("grafana", "vault", "not-grafana-at-all"):
        t = mint("grafana", "id", sub=SUB_A, username="person-a",
                 email="person-a@grigri.invalid", aud=aud,
                 scopes=["openid", "email", "profile"])
        code, me = who(t)
        print(f"   aud={aud!r:<18} -> /api/user {code} login={me.get('login')!r}")
    print("   If every aud above is accepted, `audience` is inert and only "
          "`expect_claims` can pin it.")

    banner("6. Wrong signer (a different Kanidm client's key) must be rejected")
    t = mint("mcp-gateway", "id", sub=SUB_A, username="person-a",
             email="person-a@grigri.invalid", aud="grafana",
             scopes=["openid", "email", "profile"])
    code, me = who(t)
    print(f"   signed by the mcp-gateway client key -> /api/user {code} "
          f"(401 expected: Grafana only trusts the jwk_set_url it is configured with)")

    banner("7. Roles granted natively (not by the token) - fresh instance, auto role Editor")
    subprocess.run(["docker", "rm", "-f", "gjwtprobe"], capture_output=True)
    time.sleep(2)
    if not start_grafana(auto_assign_role="Editor"):
        return 1
    SUB_C = str(uuid.uuid4())
    tokC = mint("grafana", "id", sub=SUB_C, username="person-c",
                email="person-c@grigri.invalid", scopes=["openid", "email", "profile"])
    code, mc = who(tokC)
    print(f"   person-c first JWT login -> {code} login={mc.get('login')!r} "
          f"id={mc.get('id')} org role={org_role(GW_PORT, mc.get('id'))!r} "
          f"(role came from auto_assign_org_role, i.e. Grafana-side, not the token)")
    code_try, _ = set_role(GW_PORT, mc.get("id"), "Viewer")
    print(f"   (v13.3.0 has no legacy org-role PUT route: attempt -> {code_try}; "
          f"role stays {org_role(GW_PORT, mc.get('id'))!r} regardless)")
    tokC2 = mint("grafana", "id", sub=SUB_C, username="person-c",
                 email="person-c@grigri.invalid",
                 scopes=["openid", "email", "profile", "editor"])
    code, _ = who(tokC2)
    print(f"   after a login whose scopes contain 'editor': role="
          f"{org_role(GW_PORT, mc.get('id'))!r} (skip_org_role_sync=true: the token "
          f"must not re-promote)")
    SUB_D = str(uuid.uuid4())
    tokD = mint("grafana", "id", sub=SUB_D, username="person-d",
                email="person-d@grigri.invalid", scopes=["openid", "email", "profile"])
    code, md = who(tokD)
    print(f"   person-d first login -> {code} id={md.get('id')} "
          f"org role={org_role(GW_PORT, md.get('id'))!r}")
    # Both identities are Editors here (Grafana-side grant at signup), so writes must
    # be accepted for each of them: 200, or 409 only if the uid already exists.
    for label, t in (("person-c (native Editor, Viewer-scoped token)", tokC2),
                     ("person-d (native Editor)", tokD)):
        w_code, w_body = dash_write(t)
        ok = w_code in ("200", "409")
        print(f"   {label:<42} POST /api/dashboards/db -> {w_code} "
              f"{str(w_body.get('message', ''))[:40]}  write{'ALLOWED' if ok else 'DENIED'}")

    if not args.keep:
        subprocess.run(["docker", "rm", "-f", "gjwtprobe"], capture_output=True)
        print(f"\ncontainer removed; work dir {WORK}")
    else:
        print(f"\ncontainer left running (docker rm -f gjwtprobe); work dir {WORK}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
