#!/usr/bin/env python3
"""Can ONE Grafana verify several Kanidm clients, and is that *bounded*?

Why this file was rewritten: the previous version printed outcomes and exited 0
unconditionally, and it minted every client's tokens with the **same** `iss`. That was
wrong in a way that mattered. Kanidm v1.11.2 publishes a **per-client issuer**
(`https://<host>/oauth2/openid/<client>`), so two clients of one Kanidm differ in three
claims *and* in signing key, while the `sub` is the **same account UUID**. A stub with one
issuer therefore measured a configuration that cannot exist, and it hid the incompatibility
this probe now measures.

Measured here, on the pinned `grafana/grafana:13.3.0-distroless`:

  CONFIG-A  current working path: `jwk_set_url` of one client + `expect_claims` pinning
            that client's own `iss` and `aud`. This is what production relies on today for
            direct Grafana JWT access, so its bounds are asserted as SECURITY-REQUIRED:
            only that client's key + iss + aud authenticates.
  CONFIG-B  merged `jwk_set_file` holding both clients' keys, `expect_claims` still pinning
            ONE issuer -> does the second client work at all?
  CONFIG-C  merged file, `expect_claims.iss` given as an ARRAY of both issuers -> does
            Grafana treat it as any-of?
  CONFIG-D  merged file, `expect_claims` pinning `aud` only (no iss) -> is the issuer bound
            to the key, or is key-set membership the only real boundary?

Exit status is meaningful: 0 only if every SECURITY-REQUIRED assertion held. Cross-product
outcomes that Grafana *permits* but the architecture must not rely on are reported as
limitations with their measured code, and the summary states plainly whether strict bounds
are maintainable with a shared JWKS file. Nothing here changes any deployed Grafana; no
live IdP, no live cluster. Tokens, keys and cookie values are never printed.

    python3 test/mcp-gateway/grafana-merged-keyset-probe.py

Local containers only. Synthetic identities (`.invalid` addresses, random UUIDs).
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

from cryptography.hazmat.primitives.asymmetric import ec
import jwt as pyjwt

GRAFANA_IMAGE = "docker.io/grafana/grafana:13.3.0-distroless"
GRAFANA_PORT = 14600
ISSUER_PORT = 14601
ADMIN_PW = "probe-admin-password"
WORK = os.environ.get("MCP_GRAFANA_PROBE_WORK") or tempfile.mkdtemp(prefix="grafana-merged-")
# The bridge gateway, because Grafana runs in a container and must reach this stub.
HOST_GW = subprocess.run(["docker", "network", "inspect", "bridge", "-f",
                          "{{range .IPAM.Config}}{{.Gateway}}{{end}}"],
                         capture_output=True, text=True).stdout.strip() or "127.0.0.1"
BASE = f"https://{HOST_GW}:{ISSUER_PORT}"
CA_IN_CONTAINER = "/etc/grafana/issuer-ca.pem"

# Kanidm v1.11.2: one OAuth2 client = one signing key = one issuer path. `sub` is the
# shared account UUID (deliberately one PERSON here, which is the realistic case and also
# the hazard for a merged key set).
CLIENTS = ("grafana", "mcp-gateway", "mcp-gateway-admin")


def iss_for(client):
    return f"{BASE}/oauth2/openid/{client}"


PERSON = {"sub": str(uuid.uuid4()), "login": "probe-person-a",
          "email": "probe-person-a@grigri.invalid"}
KEYS = {}
RESULTS = []       # (kind, name, ok, detail)  kind in {"SECURITY", "CAPABILITY"}
LIMITATIONS = []   # measured outcomes the architecture must not build on


def key_for(name):
    KEYS.setdefault(name, ec.generate_private_key(ec.SECP256R1()))
    return KEYS[name]


def pub_jwk(name):
    pub = key_for(name).public_key().public_numbers()

    def b64u(n, size=32):
        return base64.urlsafe_b64encode(n.to_bytes(size, "big")).rstrip(b"=").decode()
    return {"kty": "EC", "crv": "P-256", "x": b64u(pub.x, 32), "y": b64u(pub.y, 32),
            "alg": "ES256", "use": "sig", "kid": name}


class Issuer(http.server.BaseHTTPRequestHandler):
    """Per-client JWKS endpoints, the way Kanidm publishes them."""

    def log_message(self, *a):
        pass

    def do_GET(self):
        client = self.path.strip("/").split(".")[0]
        if client not in KEYS:
            self.send_response(404)
            self.end_headers()
            return
        body = json.dumps({"keys": [pub_jwk(client)]}).encode()
        self.send_response(200)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)


def mint(client, sub=None, username=None, email=None, iss=None, aud=None,
         key=None, scopes=None, bad_sig=False):
    """A token in Kanidm's shape for `client`, overridable to build forgeries.

    `iss`/`aud`/`key` can be set independently of `client`, which is how the
    cross-product cases are made: a signature that is valid for *some* key in the set
    while the issuer/audience claims belong to a different client.
    """
    now = int(time.time())
    claims = {"iss": iss or iss_for(client), "sub": sub or PERSON["sub"],
              "aud": aud or client, "azp": client, "iat": now, "nbf": now,
              "exp": now + 300, "jti": str(uuid.uuid4()),
              "preferred_username": username or PERSON["login"],
              "email": email or PERSON["email"],
              "scopes": scopes or ["openid", "email", "profile"], "token_use": "id"}
    sign_key = key if key is not None else key_for(client)
    if bad_sig:
        sign_key = ec.generate_private_key(ec.SECP256R1())
    return pyjwt.encode(claims, sign_key, algorithm="ES256",
                        headers={"kid": pub_jwk(client)["kid"], "typ": "JWT"})


def api(method, path, token=None, admin=False, body=None):
    cmd = ["curl", "-sS", "-m", "15", "-o", os.path.join(WORK, "r.txt"), "-w", "%{http_code}",
           f"http://127.0.0.1:{GRAFANA_PORT}{path}", "-X", method]
    if admin:
        cmd += ["-u", f"admin:{ADMIN_PW}"]
    if token:
        cmd += ["-H", f"Authorization: Bearer {token}"]
    if body is not None:
        cmd += ["-H", "Content-Type: application/json", "-d", json.dumps(body)]
    code = subprocess.run(cmd, capture_output=True, text=True).stdout.strip()
    try:
        data = json.loads(open(os.path.join(WORK, "r.txt")).read() or "{}")
    except Exception:
        data = {}
    return code, data


def cert():
    ca = os.path.join(WORK, "issuer.pem")
    if not os.path.exists(ca):
        subprocess.run(["openssl", "req", "-x509", "-newkey", "ec", "-pkeyopt",
                        "ec_paramgen_curve:prime256v1", "-nodes", "-keyout",
                        os.path.join(WORK, "issuer.key"), "-out", ca, "-days", "1",
                        "-subj", "/CN=kanidm-stub", "-addext",
                        f"subjectAltName=IP:{HOST_GW},IP:127.0.0.1"],
                       check=True, capture_output=True)
    return ca


def merged_jwks(names):
    p = os.path.join(WORK, "merged.jwk")
    json.dump({"keys": [pub_jwk(n) for n in names]}, open(p, "w"))
    return p


def start_grafana(cfg, fresh=True):
    """cfg: dict with jwk_set_url | jwk_set_file, and expect_claims (dict or None)."""
    subprocess.run(["docker", "rm", "-f", "gmerged"], capture_output=True)
    if fresh:
        shutil.rmtree(os.path.join(WORK, "data"), ignore_errors=True)
    os.makedirs(os.path.join(WORK, "data"), exist_ok=True)
    # the distroless image runs as UID 472 and must be able to create grafana.db
    os.chmod(os.path.join(WORK, "data"), 0o777)
    env = [
        "-e", f"GF_SERVER_HTTP_PORT={GRAFANA_PORT}",
        "-e", f"GF_SERVER_ROOT_URL=http://127.0.0.1:{GRAFANA_PORT}",
        "-e", "GF_SECURITY_ADMIN_USER=admin",
        "-e", f"GF_SECURITY_ADMIN_PASSWORD={ADMIN_PW}",
        "-e", "GF_ANALYTICS_REPORTING_ENABLED=false",
        "-e", "GF_USERS_AUTO_ASSIGN_ORG=true",
        "-e", "GF_USERS_AUTO_ASSIGN_ORG_ROLE=Editor",
        "-e", "GF_AUTH_JWT_ENABLED=true",
        "-e", "GF_AUTH_JWT_HEADER_NAME=Authorization",
        "-e", "GF_AUTH_JWT_USERNAME_CLAIM=preferred_username",
        "-e", "GF_AUTH_JWT_EMAIL_CLAIM=email",
        "-e", "GF_AUTH_JWT_AUTO_SIGN_UP=true",
        "-e", "GF_AUTH_JWT_SKIP_ORG_ROLE_SYNC=true",
        "-e", "GF_AUTH_JWT_CACHE_TTL=0s",
        # jwk_set_url is served by the stub over TLS with a self-signed certificate, so
        # without this the key set never loads and every token fails as jwt.invalid. The
        # real Kanidm issuer has a valid certificate; this line only makes the fixture
        # comparable to it.
        "-e", f"GF_AUTH_JWT_TLS_CLIENT_CA={CA_IN_CONTAINER}",
    ]
    if "url" in cfg:
        env += ["-e", f"GF_AUTH_JWT_JWK_SET_URL={cfg['url']}"]
    if "file" in cfg:
        env += ["-e", "GF_AUTH_JWT_JWK_SET_FILE=/etc/grafana/merged.jwk"]
    if cfg.get("expect") is not None:
        env += ["-e", f"GF_AUTH_JWT_EXPECT_CLAIMS={json.dumps(cfg['expect'])}"]
    cmd = ["docker", "run", "-d", "--name", "gmerged", "--network", "bridge",
           "-p", f"{GRAFANA_PORT}:{GRAFANA_PORT}",
           "-v", f"{os.path.join(WORK, 'data')}:/var/lib/grafana",
           "-v", f"{cert()}:{CA_IN_CONTAINER}:ro"]
    if "file" in cfg:
        cmd += ["-v", f"{cfg['file']}:/etc/grafana/merged.jwk:ro"]
    cmd += env + [GRAFANA_IMAGE]
    subprocess.run(cmd, check=True, capture_output=True)
    for _ in range(45):
        c, _ = api("GET", "/api/health")
        if c == "200":
            return True
        time.sleep(2)
    log = subprocess.run(["docker", "logs", "--tail", "20", "gmerged"],
                         capture_output=True, text=True)
    why = (log.stdout + log.stderr).strip().splitlines()[-1:] or ["no output"]
    state = subprocess.run(["docker", "inspect", "-f", "{{.State.Status}} "
                            "{{.State.ExitCode}}", "gmerged"],
                           capture_output=True, text=True).stdout.strip()
    print(f"  [CAPAB] measured   the fixture did not start with this configuration "
          f"(state={state}): {why[0][:200]}")
    return False


def check(kind, name, ok, detail):
    RESULTS.append((kind, name, ok, detail))
    tag = "ok " if ok else "VIOLATION" if kind == "SECURITY" else "no"
    print(f"  [{kind[:5]}] {tag:9} {name}\n            {detail}")


def main():
    print(f"Grafana {GRAFANA_IMAGE}, per-client issuers, shared sub={PERSON['sub'][:8]}…")
    for c in CLIENTS:
        print(f"   issuer[{c}] = {iss_for(c)}")
    srv = http.server.ThreadingHTTPServer((HOST_GW, ISSUER_PORT), Issuer)
    ctx = ssl.SSLContext(ssl.PROTOCOL_TLS_SERVER)
    ctx.load_cert_chain(cert(), os.path.join(WORK, "issuer.key"))
    srv.socket = ctx.wrap_socket(srv.socket, server_side=True)
    threading.Thread(target=srv.serve_forever, daemon=True).start()

    # ---------------- CONFIG-A: the current, working, single-client path ----------------
    print("\n=== CONFIG-A  jwk_set_url=grafana-only, expect_claims pins grafana iss+aud ===")
    start_grafana({"url": f"{BASE}/grafana.jwk",
                   "expect": {"iss": iss_for("grafana"), "aud": ["grafana"]}})
    c, d = api("GET", "/api/user", token=mint("grafana"))
    user_a = d.get("id")
    check("SECURITY", "A1 valid grafana key+iss+aud authenticates (the live path stays up)",
          c == "200", f"HTTP {c} userId={user_a}")

    c, d = api("GET", "/api/user", token=mint("mcp-gateway"))
    check("SECURITY", "A2 gateway key+gateway iss+gateway aud refused (separate plane)",
          c in ("401", "403"), f"HTTP {c} messageId={d.get('messageId')}")

    c, d = api("GET", "/api/user", token=mint("mcp-gateway-admin"))
    check("SECURITY", "A3 disallowed admin client refused",
          c in ("401", "403"), f"HTTP {c} messageId={d.get('messageId')}")

    # same key that IS in the set, but the other client's allowed iss+aud values
    tok = mint("grafana", iss=iss_for("mcp-gateway"), aud="mcp-gateway")
    c, d = api("GET", "/api/user", token=tok)
    check("SECURITY", "A4 INCLUDED key + other client's iss+aud is refused",
          c in ("401", "403"),
          f"HTTP {c} messageId={d.get('messageId')} - if this is 200, expect_claims does "
          f"not bind the claims and the pinned issuer is decorative")

    tok = mint("grafana", iss="https://evil.invalid/oauth2/openid/grafana")
    c, d = api("GET", "/api/user", token=tok)
    check("SECURITY", "A5 wrong iss with correct key+aud is refused",
          c in ("401", "403"), f"HTTP {c} messageId={d.get('messageId')}")

    tok = mint("grafana", aud="mcp-gateway-admin")
    c, d = api("GET", "/api/user", token=tok)
    check("SECURITY", "A6 wrong aud with correct key+iss is refused",
          c in ("401", "403"), f"HTTP {c} messageId={d.get('messageId')}")

    tok = mint("grafana", bad_sig=True)
    c, d = api("GET", "/api/user", token=tok)
    check("SECURITY", "A7 forged signature (right kid, wrong key) refused - harness sanity",
          c in ("401", "403"), f"HTTP {c} messageId={d.get('messageId')}")

    # ---------------- CONFIG-B: merged file, expect_claims pins ONE issuer ----------------
    print("\n=== CONFIG-B  merged jwk_set_file[grafana,mcp-gateway], expect_claims "
          "iss=grafana only ===")
    start_grafana({"file": merged_jwks(["grafana", "mcp-gateway"]),
                   "expect": {"iss": iss_for("grafana"), "aud": ["grafana"]}}, fresh=True)
    c, d = api("GET", "/api/user", token=mint("grafana"))
    check("SECURITY", "B1 grafana token still authenticates through the merged file",
          c == "200", f"HTTP {c} userId={d.get('id')}")
    c, d = api("GET", "/api/user", token=mint("mcp-gateway"))
    print(f"  [CAPAB] measured   B2 gateway token (own key+iss+aud) -> HTTP {c} "
          f"messageId={d.get('messageId')}")
    tok = mint("mcp-gateway", iss=iss_for("grafana"), aud="grafana")
    c, d = api("GET", "/api/user", token=tok)
    LIMITATIONS.append(("B3 merged file + pinned grafana issuer: gateway SIGNED token "
                        "carrying grafana's iss+aud values", c, d.get("id")))

    # ---------------- CONFIG-C: merged file, iss as an ARRAY ----------------
    print("\n=== CONFIG-C  merged file, expect_claims.iss = [grafana iss, gateway iss] ===")
    if not start_grafana({"file": merged_jwks(["grafana", "mcp-gateway"]),
                          "expect": {"iss": [iss_for("grafana"), iss_for("mcp-gateway")],
                                     "aud": ["grafana", "mcp-gateway"]}}, fresh=True):
        LIMITATIONS.append(("C0 expect_claims with an ARRAY-valued iss: Grafana 13.3.0 "
                            "refuses the configuration, so two issuers cannot be pinned "
                            "at once", "n/a", "n/a"))
        c_g, c_w = "000", "000"
    else:
        c_g, _ = api("GET", "/api/user", token=mint("grafana"))
        c_w, _ = api("GET", "/api/user", token=mint("mcp-gateway"))
    print(f"  [CAPAB] measured   C1 array-valued expect_claims: grafana->HTTP {c_g}, "
          f"gateway->HTTP {c_w}")
    if c_g != "000":
        cc, dd = api("GET", "/api/user", token=mint(
            "grafana", iss="https://evil.invalid/oauth2/openid/grafana"))
        check("SECURITY", "C2 an issuer outside the array is still refused",
              cc in ("401", "403"), f"HTTP {cc} messageId={dd.get('messageId')}")

    # ---------------- CONFIG-D: merged file, aud-only pin (no iss) ----------------
    print("\n=== CONFIG-D  merged file, expect_claims pins aud only (no iss) ===")
    start_grafana({"file": merged_jwks(["grafana", "mcp-gateway"]),
                   "expect": {"aud": ["grafana", "mcp-gateway"]}}, fresh=True)
    c_g, d_g = api("GET", "/api/user", token=mint("grafana"))
    c_w, d_w = api("GET", "/api/user", token=mint("mcp-gateway"))
    print(f"  [CAPAB] measured   D1 both planes' own tokens: grafana->HTTP {c_g}, "
          f"gateway->HTTP {c_w}")
    c_a, d_a = api("GET", "/api/user", token=mint("mcp-gateway-admin"))
    check("SECURITY", "D2 admin-plane key (not in the file) is refused",
          c_a in ("401", "403"), f"HTTP {c_a} messageId={d_a.get('messageId')}")
    tok = mint("mcp-gateway", iss="https://anything.invalid/oauth2/openid/whatever",
               aud="grafana")
    c_x, d_x = api("GET", "/api/user", token=tok)
    if c_x == "200":
        LIMITATIONS.append(("D3 merged file + aud-only pin: gateway key with an ARBITRARY "
                            "iss and a pinned aud", c_x, d_x.get("id")))
    same_sub = (isinstance(d_g, dict) and isinstance(d_w, dict)
                and d_g.get("id") == d_w.get("id"))
    print(f"  [CAPAB] measured   D4 one shared `sub` through two clients -> grafana "
          f"userId={d_g.get('id')}, gateway userId={d_w.get('id')}, same_user={same_sub} "
          f"({'IDENTITY MERGED into one user' if same_sub else 'separate users'})")
    if same_sub:
        LIMITATIONS.append(("D4 the same Kanidm account becomes ONE Grafana user across "
                            "both clients when the key set is merged", "200", d_g.get("id")))

    print("\n=== Summary ===")
    sec = [r for r in RESULTS if r[0] == "SECURITY"]
    fails = [r for r in sec if not r[2]]
    print(f"SECURITY assertions held: {len(sec) - len(fails)} / violated: {len(fails)}")
    for name, code, uid in LIMITATIONS:
        print(f"  LIMITATION  {name}: HTTP {code} userId={uid}")
    if fails:
        print("VERDICT: a bound that the architecture depends on does NOT hold. "
              "Exit 1.")
    elif LIMITATIONS:
        print("VERDICT: the single-client path (CONFIG-A) keeps its bounds, so the live "
              "direct-Grafana JWT surface stays safe; but Grafana's expect_claims cannot "
              "bind an issuer to a key, so a SHARED jwk_set_file is a cross-product of "
              "(any key in the file) x (any allowed iss) x (any allowed aud). Strict "
              "per-plane bounds are therefore NOT maintainable with one merged key set: "
              "either one key set per plane (per-client jwk_set_url / separate instances), "
              "or the gateway stays the only audience gate. Exit 2 = expected limitation, "
              "not a harness failure.")
    else:
        print("VERDICT: every bound held, including with the merged key set. Exit 0.")
    subprocess.run(["docker", "rm", "-f", "gmerged"], capture_output=True)
    return 1 if fails else (2 if LIMITATIONS else 0)


if __name__ == "__main__":
    sys.exit(main())
