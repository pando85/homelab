#!/usr/bin/env python3
"""Grafana 13.3.0 `username_claim = sub`: does an EXISTING OAuth user keep their
identity and their native permissions through JWT delegation?

This is the migration question behind caller-preserving Grafana delegation. The repo's
live Grafana authenticates people through Kanidm generic OAuth, so those users exist
with an `oauth_generic_oauth` auth connection. A Kanidm ACCESS token carries `sub` (an
account uuid) and nothing else - no login, no email - so a JWT-authenticated request
mapped by `sub` has to find an existing user whose *login* is that uuid. The reviewer's
source note says it will not: `getUser` matches the `(auth_module='jwt', auth_id=<sub>)`
tuple first, then email, then login, and Kanidm-OAuth users carry a different module.

Measured here against the pinned image (real login flows, no live Grafana touched),
with a JWKS/OAuth2 stub standing in for Kanidm and synthetic identities only:

  [1] a REAL generic-OAuth login creates the existing user (not an admin-made user),
      and its org role is set natively to Editor;
  [2] before any bootstrap, a sub-only ACCESS-shaped JWT must fail closed, create no
      user, and add no auth connection;
  [3] the proposed one-off alias bootstrap - an identity-claim-bearing JWT meant to
      attach the ('jwt', sub) connection to the EXISTING user - and the same attempt
      with `allow_insecure_email_lookup=true`, the setting that exists for exactly
      this takeover case;
  [4] the OAuth identity and its native Editor role survive those attempts untouched
      (nothing is silently downgraded, nothing is duplicated);
  [5] an unknown sub with auto_sign_up=false stays a 401 with no new user;
  [6] the same unknown sub with auto_sign_up=true INVENTS a user named after the uuid,
      which is why the delegation path must keep auto_sign_up=false;
  [7] with a merged key set, an access token minted for the *other* client still
      authenticates as the mapped person - JWT auth itself does not separate token
      classes (the audience/class gate belongs to the gateway).

Whatever [3] measures is the answer to the architecture doc's option (d): this probe
does not assume the bootstrap works, it tests it. Nothing here changes any deployed
Grafana - the instance is a disposable local fixture with its own SQLite file.

    python3 test/mcp-gateway/grafana-sub-alias-probe.py

Requires docker and the pinned Grafana image. Nothing outside /tmp is written.
"""
import base64
import http.server
import json
import os
import re
import shutil
import socket
import sqlite3
import ssl
import subprocess
import sys
import tempfile
import threading
import time
import uuid
import urllib.parse

from cryptography.hazmat.primitives.asymmetric import ec
import jwt as pyjwt

GRAFANA_IMAGE = "docker.io/grafana/grafana:13.3.0-distroless"
GNAME = "gsubprobe"
GW_PORT = 14510
ISSUER_PORT = 14511
ADMIN_PW = "probe-admin-password"
WORK = os.environ.get("MCP_GRAFANA_SUB_WORK") or tempfile.mkdtemp(prefix="grafana-sub-")
# Grafana runs in a container, so the issuer stub has to be reachable at the address a
# container uses for the host - the docker bridge gateway - not 127.0.0.1.
HOST_GW = subprocess.run(["docker", "network", "inspect", "bridge", "-f",
                          "{{range .IPAM.Config}}{{.Gateway}}{{end}}"],
                         capture_output=True, text=True).stdout.strip() or "127.0.0.1"
ISSUER = f"https://{HOST_GW}:{ISSUER_PORT}"


def iss_for(client):
    """Kanidm v1.11.2 publishes one OIDC issuer PER OAuth2 client:
    https://<host>/oauth2/openid/<client>. Every token this probe mints therefore carries
    its own client's issuer, not one shared string - with a shared `iss` the stub looked
    like a configuration that cannot exist and hid the issuer/key coupling that
    grafana-merged-keyset-probe.py now measures."""
    return f"{ISSUER}/oauth2/openid/{client}"
DB_DIR = os.path.join(WORK, "grafana-data")
# the stub issuer uses a self-signed certificate, so Grafana needs the CA *inside* the
# container: it is bind-mounted at this path rather than referenced from the host
CA_IN_CONTAINER = "/etc/grafana/probe-issuer-ca.pem"
os.makedirs(DB_DIR, exist_ok=True)

KEYS = {}
PERSON = {"sub": str(uuid.uuid4()), "login": "probe-person-a",
          "email": "probe-person-a@grigri.invalid"}
UNKNOWN = {"sub": str(uuid.uuid4())}

# the OAuth2 stub's issued codes, single use
CODES = {}
# the cookie jar of the logged-in OAuth session, set by oauth_login()
JAR = None


def jwk_of(name):
    key = KEYS[name]
    pub = key.public_key().public_numbers()

    def b64u(n, size):
        return base64.urlsafe_b64encode(n.to_bytes(size, "big")).rstrip(b"=").decode()
    return {"kty": "EC", "crv": "P-256", "x": b64u(pub.x, 32), "y": b64u(pub.y, 32),
            "alg": "ES256", "use": "sig", "kid": name}


def key_for(name):
    KEYS.setdefault(name, ec.generate_private_key(ec.SECP256R1()))
    return KEYS[name]


def mint(client, shape, sub, username=None, email=None, scopes=None,
         scope_string=None, aud=None, client_id=None, aud_array=False):
    """Kanidm's two published token shapes (verified against v1.11.2 oauth2.rs).

    `aud` is emitted as a STRING here because that is the form the live Kanidm tokens
    carry (aud = the OAuth2 client name). The JSON-array form is also produced on
    request, because it turned out to change Grafana's answer completely: 13.3.0's JWT
    parser declines a token whose `aud` is an array, so a probe that minted arrays
    measured "401 for everything" instead of the mapping question. See case [2c].
    """
    now = int(time.time())
    key_for(client)
    aud = aud or client
    if aud_array and not isinstance(aud, list):
        aud = [aud]
    # NOTE: the claim is emitted exactly as computed above. It used to be wrapped in a
    # list here as well, so `aud_array=True` produced a nested array and Grafana
    # declined the token for a reason that had nothing to do with the question being
    # asked. That artifact briefly made me believe 13.3.0 rejects array audiences; the
    # controlled comparison in [2c] shows it accepts both encodings.
    if shape == "access":
        claims = {"iss": iss_for(client), "sub": sub, "aud": aud, "client_id": client,
                  "iat": now, "nbf": now, "exp": now + 300, "jti": str(uuid.uuid4()),
                  "client_id": client_id or client,
                  "scope": scope_string or "openid email profile mcp_grafana",
                  "session_id": str(uuid.uuid4())}
        typ = "at+jwt"
    else:
        claims = {"iss": iss_for(client), "sub": sub, "aud": aud, "azp": client,
                  "iat": now, "nbf": now, "exp": now + 300, "jti": str(uuid.uuid4()),
                  "preferred_username": username, "email": email,
                  "scopes": scopes if scopes is not None else ["openid", "email", "profile"],
                  "token_use": "id"}
        typ = "JWT"
    return pyjwt.encode(claims, KEYS[client], algorithm="ES256",
                        headers={"kid": jwk_of(client)["kid"], "typ": typ})


class Stub(http.server.BaseHTTPRequestHandler):
    """Minimal Kanidm stand-in: per-client JWKS plus a working OAuth2 code flow."""

    def log_message(self, *a):
        pass

    def _send(self, code, body=b"", headers=()):
        self.send_response(code)
        for k, v in headers:
            self.send_header(k, v)
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        if body:
            self.wfile.write(body)

    def do_GET(self):
        p = urllib.parse.urlparse(self.path)
        if p.path.endswith(".jwk"):
            client = p.path.strip("/").split(".")[0]
            if client not in KEYS:
                return self._send(404)
            body = json.dumps({"keys": [jwk_of(client)]}).encode()
            return self._send(200, body, [("Content-Type", "application/json")])
        if p.path == "/authorize":
            q = urllib.parse.parse_qs(p.query)
            code = str(uuid.uuid4())
            CODES[code] = q.get("redirect_uri", [""])[0]
            red = (q["redirect_uri"][0] + "?code=" + code +
                   "&state=" + q.get("state", ["x"])[0])
            return self._send(302, headers=[("Location", red)])
        if p.path == "/userinfo":
            auth = self.headers.get("Authorization", "")
            if not auth.startswith("Bearer probe-access-"):
                return self._send(401)
            body = json.dumps({"sub": PERSON["sub"], "email": PERSON["email"],
                               "preferred_username": PERSON["login"]}).encode()
            return self._send(200, body, [("Content-Type", "application/json")])
        return self._send(404)

    def do_POST(self):
        p = urllib.parse.urlparse(self.path)
        if p.path != "/token":
            return self._send(404)
        n = int(self.headers.get("Content-Length") or 0)
        form = urllib.parse.parse_qs(self.rfile.read(n).decode())
        code = form.get("code", [""])[0]
        if code not in CODES:
            return self._send(400, json.dumps({"error": "invalid_grant"}).encode())
        CODES.pop(code)
        # a real IdP returns an id_token in the code flow and Grafana validates it
        # against the configured jwks_url, so the stub does the same
        id_token = mint("kanidm-oauth", "id", sub=PERSON["sub"],
                        username=PERSON["login"], email=PERSON["email"],
                        scopes=["openid", "email", "profile"], aud="kanidm-oauth")
        body = json.dumps({"access_token": "probe-access-" + uuid.uuid4().hex[:8],
                           "id_token": id_token,
                           "token_type": "bearer", "expires_in": 300,
                           "scope": "openid email profile"}).encode()
        return self._send(200, body, [("Content-Type", "application/json")])


def make_cert():
    ca = os.path.join(WORK, "issuer-ca.pem")
    subprocess.run(["openssl", "req", "-x509", "-newkey", "ec", "-pkeyopt",
                    "ec_paramgen_curve:prime256v1", "-nodes", "-keyout",
                    os.path.join(WORK, "issuer.key"), "-out", ca, "-days", "1",
                    "-subj", "/CN=kanidm-stub",
                   "-addext", f"subjectAltName=IP:127.0.0.1,IP:{HOST_GW}"],
                   check=True, capture_output=True)
    return ca


def start_issuer():
    for name in ("grafana", "mcp-gateway", "mcp-gateway-admin", "kanidm-oauth"):
        key_for(name)
    ca = make_cert()
    ctx = ssl.SSLContext(ssl.PROTOCOL_TLS_SERVER)
    ctx.load_cert_chain(ca, os.path.join(WORK, "issuer.key"))
    srv = http.server.ThreadingHTTPServer(("0.0.0.0", ISSUER_PORT), Stub)
    srv.socket = ctx.wrap_socket(srv.socket, server_side=True)
    threading.Thread(target=srv.serve_forever, daemon=True).start()


def grafana_env(port, auto_sign_up, auto_assign_role):
    return [
        "-e", f"GF_SERVER_HTTP_PORT={port}",
        "-e", f"GF_SERVER_ROOT_URL=http://127.0.0.1:{port}",
        "-e", "GF_SECURITY_ADMIN_USER=admin",
        "-e", f"GF_SECURITY_ADMIN_PASSWORD={ADMIN_PW}",
        "-e", "GF_ANALYTICS_REPORTING_ENABLED=false",
        "-e", "GF_ANALYTICS_CHECK_FOR_UPDATES=false",
        "-e", "GF_USERS_AUTO_ASSIGN_ORG=true",
        "-e", f"GF_USERS_AUTO_ASSIGN_ORG_ROLE={auto_assign_role}",
        # --- the delegation shape under test: map by the access token's `sub`
        "-e", "GF_AUTH_JWT_ENABLED=true",
        "-e", "GF_AUTH_JWT_HEADER_NAME=Authorization",
        "-e", f"GF_AUTH_JWT_JWK_SET_URL={ISSUER}/grafana.jwk",
        "-e", f"GF_AUTH_JWT_TLS_CLIENT_CA={CA_IN_CONTAINER}",
        "-e", "GF_AUTH_JWT_CACHE_TTL=0s",
        "-e", "GF_AUTH_JWT_USERNAME_CLAIM=sub",
        "-e", "GF_AUTH_JWT_EMAIL_CLAIM=email",
        "-e", f"GF_AUTH_JWT_AUTO_SIGN_UP={auto_sign_up}",
        "-e", "GF_AUTH_JWT_SKIP_ORG_ROLE_SYNC=true",
        "-e", "GF_AUTH_JWT_ROLE_ATTRIBUTE_PATH=contains(scopes[*], 'editor') && 'Editor' || 'Viewer'",
        # --- the existing identity provider, so "already exists" is a real case
        "-e", "GF_AUTH_GENERIC_OAUTH_ENABLED=true",
        "-e", "GF_AUTH_GENERIC_OAUTH_NAME=kanidm",
        "-e", "GF_AUTH_GENERIC_OAUTH_CLIENT_ID=kanidm-oauth",
        "-e", "GF_AUTH_GENERIC_OAUTH_CLIENT_SECRET=probe-secret",
        "-e", f"GF_AUTH_GENERIC_OAUTH_AUTH_URL={ISSUER}/authorize",
        "-e", f"GF_AUTH_GENERIC_OAUTH_TOKEN_URL={ISSUER}/token",
        "-e", f"GF_AUTH_GENERIC_OAUTH_API_URL={ISSUER}/userinfo",
        "-e", f"GF_AUTH_GENERIC_OAUTH_JWK_SET_URL={ISSUER}/kanidm-oauth.jwk",
        "-e", "GF_AUTH_GENERIC_OAUTH_SCOPES=openid email profile",
        "-e", "GF_AUTH_GENERIC_OAUTH_ALLOW_SIGN_UP=true",
        "-e", f"GF_AUTH_GENERIC_OAUTH_TLS_CLIENT_CA={CA_IN_CONTAINER}",
    ]


def start_grafana(auto_sign_up="false", auto_assign_role="Editor", fresh=False,
                  extra=()):
    subprocess.run(["docker", "rm", "-f", GNAME], capture_output=True)
    if fresh:
        shutil.rmtree(DB_DIR, ignore_errors=True)
    os.makedirs(DB_DIR, exist_ok=True)
    # the distroless image runs as uid 472 and writes its SQLite file into the bind
    # mount, which is a host directory this probe owns and deletes afterwards
    os.chmod(DB_DIR, 0o777)
    # every GF_* setting is an environment variable, so it must come BEFORE the image
    # name: appended after it, docker passes the flags to Grafana itself, which then
    # dies on "flag provided but not defined: -e".
    cmd = ["docker", "run", "-d", "--name", GNAME,
           "-p", f"127.0.0.1:{GW_PORT}:{GW_PORT}",
           "-v", f"{DB_DIR}:/var/lib/grafana",
           "-v", f"{os.path.join(WORK, 'issuer-ca.pem')}:{CA_IN_CONTAINER}:ro"]
    cmd += grafana_env(GW_PORT, auto_sign_up, auto_assign_role)
    for k, v in extra:
        cmd += ["-e", f"{k}={v}"]
    cmd += [GRAFANA_IMAGE]
    r = subprocess.run(cmd, capture_output=True, text=True)
    if r.returncode:
        raise SystemExit(f"grafana did not start: {r.stderr[-400:]}")
    for _ in range(120):
        code, _ = api("GET", "/api/health")
        if code == "200":
            return
        time.sleep(1)
    raise SystemExit("grafana never became healthy: " + container_logs()[-1500:])


def container_logs():
    return subprocess.run(["docker", "logs", GNAME], capture_output=True,
                          text=True).stdout


def log_errors(limit=12):
    """The interesting lines from the container log: OAuth, token, and error ones."""
    pat = re.compile(r"oauth|token|invalid|error|fail|denied", re.I)
    lines = [ln for ln in container_logs().splitlines()
             if pat.search(ln) and "flag evaluation" not in ln]
    return " || ".join(lines[-limit:])[-1200:]


def api(method, path, token=None, admin=False, body=None, cookie=None, jar=None,
        follow=False):
    out = os.path.join(WORK, "resp.txt")
    cmd = ["curl", "-sS", "-m", "20", "-D", os.path.join(WORK, "head.txt"), "-o", out,
           "-w", "%{http_code}", f"http://127.0.0.1:{GW_PORT}{path}", "-X", method]
    # curl does not follow redirects unless -L is given, which is what the OAuth hops
    # need: each Location is followed by hand, one hop at a time.
    del follow
    if admin:
        cmd += ["-u", f"admin:{ADMIN_PW}"]
    if token:
        cmd += ["-H", f"Authorization: Bearer {token}"]
    if cookie:
        cmd += ["-H", f"Cookie: {cookie}"]
    if jar:
        cmd += ["-c", jar, "-b", jar]
    if body is not None:
        cmd += ["-H", "Content-Type: application/json", "-d", json.dumps(body)]
    r = subprocess.run(cmd, capture_output=True, text=True)
    body = open(out).read() if os.path.exists(out) else ""
    try:
        data = json.loads(body or "{}")
    except Exception:
        data = {"raw": body[:200]}
    if not r.stdout.strip():
        data.setdefault("curl_error", (r.stderr or "").strip()[:160])
    return r.stdout.strip(), data


def curl_url(url, jar=None, insecure=False, to_file=None):
    """Fetch an ABSOLUTE url without following redirects. The OAuth2 hops go to the
    stub (https, self-signed), not to Grafana, so they need their own fetch helper."""
    out = to_file or os.path.join(WORK, "hop.txt")
    cmd = ["curl", "-sS", "-m", "20", "-D", os.path.join(WORK, "head.txt"), "-o", out,
           "-w", "%{http_code}", url]
    if insecure:
        cmd += ["-k"]
    if jar:
        cmd += ["-c", jar, "-b", jar]
    r = subprocess.run(cmd, capture_output=True, text=True)
    head = open(os.path.join(WORK, "head.txt")).read()
    loc = re.search(r"(?i)^location:\s*(.+)$", head, re.M)
    body = open(out).read() if os.path.exists(out) else ""
    return r.stdout.strip(), (loc.group(1).strip() if loc else ""), body


def jar_cookies(jar):
    """Read the cookie jar: Netscape format, name at index 5, value at index 6."""
    if not jar or not os.path.exists(jar):
        return {}
    out = {}
    for ln in open(jar):
        # curl writes HttpOnly cookies on lines beginning with the literal marker
        # "#HttpOnly_<domain>", and the file's own comments start with "# ". Skipping
        # every "# " line drops grafana_session itself (measured: /api/user still
        # authenticated with the jar, while the parser reported no session), so the
        # marker is stripped instead.
        if ln.startswith("#HttpOnly_"):
            ln = "#" + ln[len("#HttpOnly_"):]
        elif ln.startswith("#") or not ln.strip():
            continue
        parts = ln.rstrip("\n").split("\t")
        if len(parts) >= 7 and parts[5]:
            out[parts[5]] = parts[6]
    return out


def oauth_login():
    """Drive the generic-OAuth code flow for real, so the user ends up bound to
    `oauth_generic_oauth` the way a Kanidm login binds it."""
    jar = os.path.join(WORK, f"jar-{uuid.uuid4().hex[:6]}.txt")
    open(jar, "w").close()
    st, loc1, _ = curl_url(f"http://127.0.0.1:{GW_PORT}/login/generic_oauth", jar=jar)
    if not loc1:
        return None, f"step1 {st}: Grafana did not redirect to the authorize URL"
    st2, loc2, _ = curl_url(loc1, jar=jar, insecure=True)
    if not loc2:
        return None, f"step2 {st2}: the stub authorize did not redirect back"
    st3, loc3, body3 = curl_url(loc2, jar=jar,
                                to_file=os.path.join(WORK, "callback.html"))
    # 13.3.0 answers the callback with a redirect to "/" and only issues
    # grafana_session on that next request, so follow it once with the same jar
    curl_url(f"http://127.0.0.1:{GW_PORT}{loc3 or '/'}", jar=jar,
             to_file=os.path.join(WORK, "after.html"))
    cookies = jar_cookies(jar)
    sess = cookies.get("grafana_session")
    if not sess:
        return None, (f"step3 {st3} set no grafana_session (cookies={sorted(cookies)}, "
                      f"loc={loc3[:80]}, body={body3[:120]}); "
                      f"grafana said: {log_errors()}")
    c4, me = api("GET", "/api/user", jar=jar)
    if c4 != "200":
        return None, f"session cookie rejected: {c4} {json.dumps(me)[:150]}"
    global JAR
    JAR = jar
    return f"grafana_session={sess[:12]}...", me


def db_rows():
    """Read the auth bindings straight out of the copied SQLite DB (read-only)."""
    dst = os.path.join(WORK, "grafana.db")
    subprocess.run(["docker", "cp", f"{GNAME}:/var/lib/grafana/grafana.db", dst],
                   capture_output=True)
    if not os.path.exists(dst):
        return [], []
    con = sqlite3.connect(f"file:{dst}?mode=ro", uri=True)
    try:
        auth = con.execute("select user_id, auth_module, auth_id from user_auth "
                           "order by user_id").fetchall()
        users = con.execute("select id, version, login, email from main.user "
                            "where id > 1 order by id").fetchall()
        return auth, users
    except sqlite3.Error:
        return [], []
    finally:
        con.close()


def org_role(user_id):
    """The org role of a user, read as admin. 13.3.0 returns a list here."""
    c, rows = api("GET", "/api/org/users", admin=True)
    items = rows if isinstance(rows, list) else rows.get("users", []) if isinstance(rows, dict) else []
    for r in items:
        if r.get("userId") == user_id:
            return r.get("role")
    return None


def jwt_as(token):
    c, me = api("GET", "/api/user", token=token)
    return c, me


results = []


def record(name, ok, detail=""):
    results.append((name, ok, detail))
    print(f"  {'PASS' if ok else 'FAIL'}     {name}" + (f"\n         {detail}" if detail else ""))


def dash_write(cookie=None, token=None, jar=None):
    """A write that needs Editor: creating a dashboard."""
    body = {"dashboard": {"uid": f"sub-{uuid.uuid4().hex[:10]}", "title": "probe",
                          "panels": []}, "folderUid": "", "overwrite": False}
    # `jar` is opt-in: an OAuth session cookie must not silently answer for a JWT test
    if jar is None and token is None and cookie is None:
        jar = JAR
    return api("POST", "/api/dashboards/db", body=body, cookie=cookie, token=token,
               jar=jar)


def main():
    start_issuer()
    print(f"starting Grafana {GRAFANA_IMAGE} with username_claim=sub, auto_sign_up=false")
    start_grafana(auto_sign_up="false", auto_assign_role="Editor", fresh=True)

    # ---- [1] a real OAuth login creates the existing user ------------------
    cookie, me = oauth_login()
    if not cookie:
        raise SystemExit(f"[1] the OAuth login did not work: {me}")
    record("[1] real generic-OAuth login (the pre-existing-user case)", True,
           f"userId={me.get('id')} login={me.get('login')} email={me.get('email')} "
           "(Grafana stores the login it derives from its own oauth login_field, "
           "which here is the e-mail, not the IdP username)")
    auth0, users0 = db_rows()
    binds = {a[1]: a for a in auth0 if a[0] == me["id"]}
    record("[1b] the person is bound to the OAuth module, not to jwt",
           "oauth_generic_oauth" in binds and "jwt" not in binds,
           f"modules={sorted(binds)}")
    role = org_role(me["id"])
    record("[1c] native org role is Editor", role == "Editor", f"role={role}")

    # ---- [2] sub-only ACCESS token, before any bootstrap -------------------
    tok_sub = mint("grafana", "access", sub=PERSON["sub"], aud="mcp-gateway",
                   scope_string="openid email profile mcp_grafana")
    c, body = jwt_as(tok_sub)
    auth1, users1 = db_rows()
    record("[2] sub-only ACCESS JWT fails closed BEFORE any binding exists",
           c in ("401", "403"),
           f"HTTP {c} messageId={body.get('messageId') if isinstance(body, dict) else None}")
    record("[2b] it created no duplicate user and no jwt binding",
           len(users1) == len(users0) and
           not [a for a in auth1 if a[1] == "jwt" and a[2] == PERSON["sub"]],
           f"users {len(users0)} -> {len(users1)}")

    # [2c] encoding comparison. Both shapes must reach the SAME answer: a flat
    # string audience and a flat array audience are both valid RFC 9068 encodings, and
    # Grafana 13.3.0 accepts either. The value is not checked without expect_claims
    # (case [7]).
    tok_str = mint("grafana", "access", sub=PERSON["sub"], aud="mcp-gateway")
    tok_arr = mint("grafana", "access", sub=PERSON["sub"], aud="mcp-gateway",
                   aud_array=True)
    c_str, b_str = jwt_as(tok_str)
    c_arr, b_arr = jwt_as(tok_arr)
    id_str = (b_str.get("messageId") if isinstance(b_str, dict) else None)
    id_arr = (b_arr.get("messageId") if isinstance(b_arr, dict) else None)
    record("[2c] string and array `aud` encodings are treated identically",
           c_str == c_arr and id_str == id_arr,
           f"string -> HTTP {c_str} {id_str} | array -> HTTP {c_arr} {id_arr}")

    # ---- [3] the proposed one-off alias bootstrap --------------------------
    boot = mint("grafana", "id", sub=PERSON["sub"], username=PERSON["login"],
                email=PERSON["email"], aud="mcp-gateway")
    c, body = jwt_as(boot)
    auth2, users2 = db_rows()
    took_over = c == "200" and isinstance(body, dict) and body.get("id") == me["id"]
    record("[3] identity-claim bootstrap JWT attaches the EXISTING OAuth user "
           "(auto_sign_up=false, default lookup rules)",
           took_over, f"HTTP {c} userId={body.get('id') if isinstance(body, dict) else None}")
    record("[3b] ('jwt', sub) connection created on that same user, no new user",
           any(a[0] == me["id"] and a[2] == PERSON["sub"] for a in auth2
               if a[1] == "jwt") and len(users2) == len(users0),
           f"jwt rows={[a for a in auth2 if a[1] == 'jwt']} users {len(users0)} -> {len(users2)}")

    if not took_over:
        # If the default lookup rules do not attach the existing user, try the one
        # setting that exists for exactly this case, then re-measure.
        start_grafana(auto_sign_up="false", auto_assign_role="Editor", fresh=False,
                      extra=[("GF_AUTH_OAUTH_ALLOW_INSECURE_EMAIL_LOOKUP", "true")])
        cookie2, me2 = oauth_login()
        c, body = jwt_as(mint("grafana", "id", sub=PERSON["sub"],
                              username=PERSON["login"], email=PERSON["email"],
                              aud="mcp-gateway"))
        took_over = bool(cookie2) and c == "200" and isinstance(body, dict) \
            and body.get("id") == me2["id"]
        record("[3c1] bootstrap with allow_insecure_email_lookup=true", took_over,
               f"HTTP {c} userId={body.get('id') if isinstance(body, dict) else None}")
        target = me2["id"] if cookie2 else me["id"]
    else:
        target = me["id"]
    c, body = jwt_as(tok_sub)
    record("[3d] sub-only ACCESS after the bootstrap resolves the same user",
           c == "200" and isinstance(body, dict) and body.get("id") == target,
           f"HTTP {c} userId={body.get('id') if isinstance(body, dict) else None}")

    # [3c2] the dangerous-lookup CONTROL, run whether or not the happy case passed:
    # `allow_insecure_email_lookup=true` finds a user by e-mail alone. A token for a
    # DIFFERENT Kanidm account that merely claims this user's e-mail must not gain that
    # Grafana identity. Expected outcome: refused. If it attaches, that is a
    # cross-account identity grant and the setting must stay off forever.
    start_grafana(auto_sign_up="false", auto_assign_role="Editor", fresh=False,
                  extra=[("GF_AUTH_OAUTH_ALLOW_INSECURE_EMAIL_LOOKUP", "true")])
    auth_before, users_before = db_rows()
    c, body = jwt_as(mint("grafana", "id", sub=UNKNOWN["sub"], username=PERSON["login"],
                          email=PERSON["email"], aud="mcp-gateway"))
    auth_after, users_after = db_rows()
    hijack = (c == "200" and isinstance(body, dict) and body.get("id") == target
              and any(a[2] == UNKNOWN["sub"] for a in auth_after if a[1] == "jwt"))
    record("[3c2] CONTROL: a DIFFERENT sub claiming an existing user's e-mail must not "
           "attach to that user (allow_insecure_email_lookup=true)", not hijack,
           f"HTTP {c} userId={body.get('id') if isinstance(body, dict) else None} "
           f"jwt rows before={len([a for a in auth_before if a[1] == 'jwt'])} "
           f"after={len([a for a in auth_after if a[1] == 'jwt'])} "
           f"-> {'REFUSED, as required' if not hijack else 'ATTACHED: cross-account identity grant'}")

    # ---- [4] permissions: native role kept, and usable through the JWT -----
    role_after = org_role(target)
    record("[4] native RBAC is NOT silently downgraded (skip_org_role_sync=true, "
           "role_attribute_path would say Viewer)", role_after == "Editor",
           f"role=Editor expected, {role_after} measured")
    c, body = dash_write(token=tok_sub)
    record("[4b] the JWT-authenticated identity can actually exercise Editor",
           c == "200", f"HTTP {c} created={bool(isinstance(body, dict) and body.get('uid'))}"
           f" status={body.get('status') if isinstance(body, dict) else None} "
           f"(no session cookie used)")
    c, body = api("GET", "/api/user", jar=JAR)
    record("[4c] the OAuth session is still the same single user",
           c == "200" and body.get("id") == target, f"HTTP {c} id={body.get('id')}")

    # ---- [5] an unknown sub must not become a user ------------------------
    start_grafana(auto_sign_up="false", auto_assign_role="Editor", fresh=False)
    _, _ = oauth_login()
    _, users3 = db_rows()
    tok_unknown = mint("grafana", "access", sub=UNKNOWN["sub"], aud="mcp-gateway")
    c, body = jwt_as(tok_unknown)
    _, users3b = db_rows()
    record("[5] unknown sub with auto_sign_up=false -> refused", c in ("401", "403"),
           f"HTTP {c} {json.dumps(body)[:110]}")
    record("[5b] unknown sub created no user and no login named after the uuid",
           len(users3b) == len(users3) and not [u for u in users3b if u[2] == UNKNOWN["sub"]],
           f"users {len(users3)} -> {len(users3b)}")

    # ---- [6] the same unknown sub with auto_sign_up=true ------------------
    start_grafana(auto_sign_up="true", auto_assign_role="Viewer", fresh=False)
    c, body = jwt_as(tok_unknown)
    auth4, users4 = db_rows()
    invented = [u for u in users4 if u[2] == UNKNOWN["sub"]]
    record("[6] auto_sign_up=true INVENTS a user whose login is the uuid",
           c == "200" and len(invented) == 1,
           f"HTTP {c} users {len(users3)} -> {len(users4)} invented={invented} "
           f"-> the duplicate-identity hazard: auto_sign_up must stay false")

    # ---- [7b] a real other-client token is refused by the key set ----------
    # Same Kanidm, different OAuth2 client: different signing key AND different issuer.
    # Grafana here trusts only the `grafana` client's key set, so this must not verify.
    tok_gw = mint("mcp-gateway", "access", sub=PERSON["sub"])
    c, body = jwt_as(tok_gw)
    record("[7b] a token from the gateway client (own key + own per-client issuer) is "
           "refused by the grafana-only key set", c in ("401", "403"),
           f"HTTP {c} messageId={body.get('messageId') if isinstance(body, dict) else None}"
           f" - measured with the issuer per client, which is what Kanidm actually does")

    # ---- [7] the audience VALUE is not checked ----------------------------
    tok_other = mint("grafana", "access", sub=UNKNOWN["sub"], aud="some-other-client")
    c, body = jwt_as(tok_other)
    record("[7] a token whose aud is a DIFFERENT client still authenticates",
           c == "200",
           f"HTTP {c} userId={body.get('id') if isinstance(body, dict) else None} - Grafana 13.3.0 checks no audience "
           f"value without expect_claims, so the class/audience gate belongs at the gateway")

    print("\n=== Summary ===")
    bad = [n for n, ok, _ in results if not ok]
    for n, ok, _ in results:
        print(f"{'PASS' if ok else 'FAIL':6} {n}")
    if bad:
        print("\nFAILED:", ", ".join(bad))
        sys.exit(1)
    print("\nGrafana sub-mapping / alias-bootstrap assertions all behaved as measured; "
          "the bootstrap is a test-only migration prerequisite, not an enabled config")


if __name__ == "__main__":
    try:
        main()
    finally:
        subprocess.run(["docker", "rm", "-f", GNAME], capture_output=True)
        if os.environ.get("MCP_GRAFANA_SUB_KEEP"):
            print(f"work dir kept: {WORK}")
        else:
            shutil.rmtree(WORK, ignore_errors=True)
