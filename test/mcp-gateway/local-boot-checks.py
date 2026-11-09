#!/usr/bin/env python3
"""Boot the CHART-RENDERED gateway config on the pinned agentgateway binary and behave-test it.

route-checks.py proves the chart contains the strings that were validated; this proves the
pinned binary accepts those exact strings and enforces them at runtime - local config parsing
is strict (an unsupported key aborts startup), so this is the gate that catches "compiles in
YAML, dies at boot".

Nothing here touches a cluster, a production credential, or a non-loopback listener:
  * the gateway binds 127.0.0.1 on an ephemeral port (the chart's 3000 is rewritten locally);
  * the session key is freshly generated hex, never stored or printed;
  * every bearer is minted by a self-signed ES256 stub issuer on loopback;
  * credentials are reported as sha256[:12] fingerprints only.

Three runs:
  A  the production config, verbatim except port/bindAddress, against the REAL Kanidm JWKS -
     proves the exact committed YAML boots and answers the public surface (discovery 200,
     MCP 401 with the advertised resource_metadata URL, disabled backend 404).
  B  the same routes with the issuer pointed at the stub and the targets at loopback captures,
     plus the disabled grafana route recovered from values.yaml and un-commented - proves the
     access gate (scope, identity-claim refusal, strip lists, backendAuth passthrough) and that
     the commented block is real config, not prose.
  C  the same process as B with the config file rewritten (grafana route removed) - proves
     route changes are picked up WITHOUT restarting the pod, which is what makes this chart's
     "edit values.yaml, ArgoCD syncs" workflow actually take effect.

    python3 test/mcp-gateway/local-boot-checks.py

Requires: python3 with PyJWT + cryptography; agentgateway v1.7.0.
Set AGENTGATEWAY_BIN=/path/to/agentgateway to use a local copy, otherwise the pinned release
asset is downloaded once to AGENTGATEWAY_CACHE (default ~/.cache/mcp-gateway-tests) and its
sha256 is verified. KEEP=1 leaves the work directory behind.
"""
import base64
import hashlib
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
import json
import os
import re
import shutil
import socket
import subprocess
import sys
import tempfile
import textwrap
import threading
import time
import urllib.error
import urllib.request
import uuid

import jwt as pyjwt
import yaml
from cryptography.hazmat.primitives.asymmetric import ec

BIN_VERSION = "1.7.0"
BIN_URL = ("https://github.com/agentgateway/agentgateway/releases/download/"
           f"v{BIN_VERSION}/agentgateway-linux-amd64")
BIN_SHA256 = "1b2fc01a2de29ca113bbb6258b5cae7b1eb3c724071c09d687a449132f2f42f8"
CACHE = os.environ.get("AGENTGATEWAY_CACHE", os.path.expanduser("~/.cache/mcp-gateway-tests"))
BIN = os.environ.get("AGENTGATEWAY_BIN") or os.path.join(CACHE, "agentgateway")

CHART = "mcp-gateway"
ISSUER = "https://idm.grigri.cloud/oauth2/openid/mcp-gateway"  # real IdP: public, no secrets
HOST = "mcp.grigri.cloud"
WELL_KNOWN = "/.well-known/oauth-protected-resource"
AUD = "mcp-gateway"
HEALTH_PORT = 15021  # the port the chart's probes use; fixed by the binary
KID = "boot-key"

WORK = os.environ.get("BOOT_WORK") or tempfile.mkdtemp(prefix="mcp-gateway-boot-")
KEY = ec.generate_private_key(ec.SECP256R1())
OTHER = ec.generate_private_key(ec.SECP256R1())
CAPTURES = {"grafana": [], "context7": []}
RESULTS = []
GW = {}       # port of the running gateway
ISSUER_URL = ""


def check(label, ok, detail=""):
    print(f"{'PASS' if ok else 'FAIL'}  {label}" + (f"\n      {detail}" if detail else ""))
    RESULTS.append(bool(ok))


def fp(token):
    return hashlib.sha256(token.encode()).hexdigest()[:12] if token else None


# --------------------------------------------------------------------------- fixture plumbing
def b64i(i, ln):
    return base64.urlsafe_b64encode(i.to_bytes(ln, "big")).rstrip(b"=").decode()


def jwk(key, kid):
    pub = key.public_key().public_numbers()
    return {"kty": "EC", "crv": "P-256", "x": b64i(pub.x, 32), "y": b64i(pub.y, 32),
            "alg": "ES256", "use": "sig", "kid": kid}


def token(key, kid, **over):
    now = int(time.time())
    claims = {"iss": ISSUER_URL, "sub": f"acct-{uuid.uuid4().hex[:6]}", "aud": AUD,
              "client_id": AUD, "scope": "openid email profile mcp_context7",
              "iat": now, "nbf": now, "exp": now + 3600}
    claims.update(over)
    claims = {k: v for k, v in claims.items() if v is not None}
    return pyjwt.encode(claims, key, algorithm="ES256", headers={"kid": kid})


class Issuer(BaseHTTPRequestHandler):
    def log_message(self, *a):
        pass

    def do_GET(self):
        if self.path == "/jwks":
            body = json.dumps({"keys": [jwk(KEY, KID)]}).encode()
        elif self.path.startswith("/.well-known/"):
            body = json.dumps({"issuer": ISSUER_URL, "jwks_uri": f"{ISSUER_URL}/jwks",
                               "token_endpoint": f"{ISSUER_URL}/token"}).encode()
        else:
            self.send_response(404)
            self.end_headers()
            return
        self.send_response(200)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)


class Capture(BaseHTTPRequestHandler):
    """Minimal stateless streamable-HTTP MCP server: answers, and records what it received."""

    name = "context7"

    def log_message(self, *a):
        pass

    def _rec(self, raw):
        hdrs = {k.lower(): v for k, v in self.headers.items()}
        auth = hdrs.get("authorization")
        rec = {"path": self.path, "auth": auth, "bearer": fp(auth.split(" ", 1)[1])
               if auth and auth.lower().startswith("bearer ") else None,
               "auth_raw": fp(auth) if auth else None,
               "cookie": hdrs.get("cookie"), "x_grafana_url": hdrs.get("x-grafana-url"),
               "x_sa_token": hdrs.get("x-grafana-service-account-token"),
               "session_id_hdr": hdrs.get("mcp-session-id") or hdrs.get("x-session-id"),
               "method": None, "hdrs": sorted(hdrs)}
        try:
            msg = json.loads(raw or b"{}")
            rec["method"] = msg.get("method")
            rec["id"] = msg.get("id", 1)
        except Exception:
            rec["id"] = 1
        CAPTURES[self.name].append(rec)
        return rec

    def _reply(self, rec):
        m = rec["method"]
        if m == "initialize":
            res = {"protocolVersion": "2025-06-18", "capabilities": {"tools": {}},
                   "serverInfo": {"name": f"capture-{self.name}", "version": "0"}}
        elif m == "tools/list":
            res = {"tools": [{"name": "echo", "inputSchema": {"type": "object"}}]}
        elif m == "tools/call":
            res = {"content": [{"type": "text", "text": f"ok-{self.name}"}]}
        elif m and m.startswith("notifications/"):
            self.send_response(202)
            self.send_header("Content-Length", "0")
            self.end_headers()
            return
        else:
            res = {}
        body = json.dumps({"jsonrpc": "2.0", "id": rec["id"], "result": res}).encode()
        self.send_response(200)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def do_POST(self):
        raw = self.rfile.read(int(self.headers.get("Content-Length", 0)))
        self._reply(self._rec(raw))

    def do_GET(self):
        self.send_response(405)
        self.end_headers()


class GrafanaCapture(Capture):
    name = "grafana"


class CtxCapture(Capture):
    name = "context7"


def free_port():
    s = socket.socket()
    s.bind(("127.0.0.1", 0))
    p = s.getsockname()[1]
    s.close()
    return p


def serve(handler, port):
    srv = ThreadingHTTPServer(("127.0.0.1", port), handler)
    srv.daemon_threads = True
    threading.Thread(target=srv.serve_forever, daemon=True).start()
    return srv


def wait_port(port, timeout=25):
    deadline = time.time() + timeout
    while time.time() < deadline:
        try:
            with socket.create_connection(("127.0.0.1", port), 1):
                return True
        except OSError:
            time.sleep(0.1)
    return False


def http(method, port, path, body=None, token_value=None, host=HOST, extra=None):
    req = urllib.request.Request(f"http://127.0.0.1:{port}{path}", data=body, method=method)
    if body is not None:
        req.add_header("Content-Type", "application/json")
        req.add_header("Accept", "application/json, text/event-stream")
        req.add_header("MCP-Protocol-Version", "2025-06-18")
    req.add_header("Host", host)
    if token_value:
        req.add_header("Authorization", f"Bearer {token_value}")
    for k, v in (extra or {}).items():
        req.add_header(k, v)
    try:
        with urllib.request.urlopen(req, timeout=15) as r:
            return r.status, {k.lower(): v for k, v in r.headers.items()}, r.read()[:2000]
    except urllib.error.HTTPError as e:
        return e.code, {k.lower(): v for k, v in e.headers.items()}, e.read()[:2000]
    except Exception as e:  # noqa: BLE001
        return f"ERR:{type(e).__name__}", {}, str(e).encode()[:200]


def rpc(path, token_value=None, method="initialize", port=None, extra=None):
    body = {"jsonrpc": "2.0", "id": 1, "method": method}
    if method == "initialize":
        body["params"] = {"protocolVersion": "2025-06-18",
                          "clientInfo": {"name": "boot-checks", "version": "0"},
                          "capabilities": {}}
    elif method == "tools/call":
        body["params"] = {"name": "echo", "arguments": {}}
    else:
        body["params"] = {}
    return http("POST", port or GW["port"], path, json.dumps(body).encode(), token_value,
                extra=extra)


# --------------------------------------------------------------------------- config building
def rendered_config():
    out = subprocess.run(
        ["helm", "template", CHART, f"apps/{CHART}", "--namespace", CHART, "--include-crds"],
        capture_output=True, text=True, timeout=300, check=False)
    if out.returncode != 0:
        raise RuntimeError(f"helm template: {out.stderr[-800:]}")
    docs = [d for d in yaml.safe_load_all(out.stdout) if d]
    cm = next(d for d in docs if d["kind"] == "ConfigMap")
    return cm["data"]["config.yaml"]


def commented_route(values_text, header):
    lines = values_text.splitlines()
    start = next(i for i, l in enumerate(lines) if l.strip() == f"# {header}")
    out = []
    for line in lines[start:]:
        stripped = line.lstrip(" ")
        if not stripped.startswith("#") or re.fullmatch(r"#\s*-{3,}", stripped):
            break
        out.append(" " * (len(line) - len(stripped)) + stripped[2:])
    return yaml.safe_load(textwrap.dedent("\n".join(out)))[0]


def base_cfg(text, port):
    cfg = yaml.safe_load(text)
    # local-only deviations, the only two that a loopback run can make:
    cfg["gateways"]["default"] = {"port": port, "bindAddress": "127.0.0.1"}
    cfg["config"]["backend"] = {"allowDynamicLoopback": True}
    return cfg


def use_stub_authn(cfg):
    for r in cfg["routes"]:
        a = (r.get("policies") or {}).get("mcpAuthentication")
        if a:
            a["issuer"] = ISSUER_URL
            a["jwks"] = {"url": f"{ISSUER_URL}/jwks"}
    return cfg


def retarget(cfg, name, host, port):
    for r in cfg["routes"]:
        if r["name"] == name:
            for be in r["backends"]:
                for tgt in be["mcp"]["targets"]:
                    tgt["mcp"] = {"host": host, "port": port, "path": "/mcp"}
                    # a loopback capture speaks plain HTTP; backendTLS is what the real
                    # https upstream needs and is exercised by run A and by the live plane.
                    tgt.pop("policies", None)
    return cfg


def write(cfg, path):
    with open(path, "w") as fh:
        yaml.safe_dump(cfg, fh, sort_keys=False, width=4096)


def port_open(port):
    try:
        with socket.create_connection(("127.0.0.1", port), 0.5):
            return True
    except OSError:
        return False


def boot(cfg_path, log):
    # The readiness proof is only ours if nothing else already owns the health port.
    if port_open(HEALTH_PORT):
        raise RuntimeError(f"port {HEALTH_PORT} is already in use - stop the other gateway first")
    env = dict(os.environ, HOME=f"{WORK}/home", XDG_CONFIG_HOME=f"{WORK}/home/.config")
    os.makedirs(env["HOME"], exist_ok=True)
    # generated, single-use, never printed: 32 bytes hex is what the binary demands
    env["SESSION_KEY"] = os.urandom(32).hex()
    fh = open(log, "w")
    proc = subprocess.Popen([BIN, "-f", cfg_path], stdout=fh, stderr=fh, env=env)
    return proc, fh


def stop(proc, fh):
    proc.terminate()
    try:
        proc.wait(timeout=10)
    except subprocess.TimeoutExpired:
        proc.kill()
    fh.close()


# --------------------------------------------------------------------------- runs
def run_a(text):
    print("\n=== A: production config, verbatim (loopback port only) ===")
    port = free_port()
    GW["port"] = port
    cfg = base_cfg(text, port)
    path = f"{WORK}/configA.yaml"
    write(cfg, path)
    proc, fh = boot(path, f"{WORK}/A.log")
    try:
        up = wait_port(port)
        check("A boots with the chart-rendered config (strict local schema, no unknown keys)",
              up and proc.poll() is None, f"pid={proc.pid}")
        code, _, _ = http("GET", HEALTH_PORT, "/healthz/ready")
        check("A readiness endpoint answers 200 on port 15021 (the chart's probe)",
              code == 200, str(code))
        for name in ("context7", "grafana"):
            code, hdrs, body = http("GET", port, f"{WELL_KNOWN}/{name}/mcp")
            doc = None
            try:
                doc = json.loads(body)
            except Exception:  # noqa: BLE001
                pass
            check(f"A /{name} discovery is 200 unauthenticated and valid JSON",
                  code == 200 and isinstance(doc, dict), str(code))
            check(f"A /{name} discovery resource is the absolute route URL",
                  doc and doc.get("resource") == f"https://{HOST}/{name}/mcp",
                  str(doc and doc.get("resource")))
            check(f"A /{name} discovery advertises the Kanidm per-client issuer and its scope",
                  doc and doc.get("authorization_servers") == [ISSUER]
                  and doc.get("scopes_supported") == [f"mcp_{name}"], str(doc))
            check(f"A /{name} discovery sends content-type (CEL literal header, not raw text)",
                  hdrs.get("content-type", "").startswith("application/json")
                  and hdrs.get("cache-control") == "public, max-age=300", str(hdrs))
        code, _, _ = http("GET", port, f"{WELL_KNOWN}/context7/mcp", host="someone-else.example")
        check("A discovery is host-pinned: another Host is 404, not served", code == 404, str(code))
        code, hdrs, body = rpc("/context7/mcp")
        wa = hdrs.get("www-authenticate", "")
        check("A live MCP route without a bearer is 401", code == 401, str(code))
        check("A 401 challenge advertises THIS gateway's discovery URL exactly",
              f'resource_metadata="https://{HOST}{WELL_KNOWN}/context7/mcp"' in wa, wa)
        check("A 401 issues no Mcp-Session-Id (stateless route)",
              not hdrs.get("mcp-session-id"), str(sorted(hdrs)))
        forged = pyjwt.encode({"iss": ISSUER, "aud": AUD, "sub": "x", "scope": "mcp_context7",
                               "exp": int(time.time()) + 60}, OTHER, algorithm="ES256",
                              headers={"kid": "unknown"})
        code, _, _ = rpc("/context7/mcp", forged)
        check("A token signed by an unknown key is 401 against the REAL Kanidm JWKS",
              code == 401, str(code))
        code, _, body = rpc("/grafana/mcp")
        check("A disabled grafana route is 404 (no auth, no backend, nothing to reach)",
              code == 404, f"{code} {body[:60]!r}")
    finally:
        stop(proc, fh)


def run_b(text, grafana_route):
    print("\n=== B: same routes, stub issuer + loopback captures (+ grafana block enabled) ===")
    global GW
    port, ip, gp, cp = free_port(), free_port(), free_port(), free_port()
    GW = {"port": port}
    handler = type("BoundIssuer", (Issuer,), {})
    srv = serve(handler, ip)
    globals()["ISSUER_URL"] = f"http://127.0.0.1:{ip}"
    serve(GrafanaCapture, gp)
    serve(CtxCapture, cp)
    cfg = retarget(use_stub_authn(base_cfg(text, port)), "context7", "127.0.0.1", cp)
    graf = json.loads(json.dumps(grafana_route))
    graf["policies"]["mcpAuthentication"]["issuer"] = ISSUER_URL
    graf["policies"]["mcpAuthentication"]["jwks"] = {"url": f"{ISSUER_URL}/jwks"}
    graf["backends"][0]["mcp"]["targets"][0]["mcp"] = {
        "host": "127.0.0.1", "port": gp, "path": "/mcp"}
    cfg["routes"].append(graf)
    path = f"{WORK}/configB.yaml"
    write(cfg, path)
    proc, fh = boot(path, f"{WORK}/B.log")
    try:
        check("B boots with the un-commented grafana route added (comment block is real config)",
              wait_port(port) and proc.poll() is None)
        check("B stub issuer is reachable (fixture, not production)",
              http("GET", ip, "/jwks")[0] == 200)
        cap_c, cap_g = CAPTURES["context7"], CAPTURES["grafana"]
        tk_ctx = token(KEY, KID, scope="openid email profile mcp_context7")
        code, hdrs, body = rpc("/context7/mcp", tk_ctx)
        check("B valid mcp_context7 bearer reaches the backend (200 + JSON-RPC envelope)",
              code == 200 and b"jsonrpc" in body, f"{code} {body[:70]!r}")
        rec = cap_c[-1] if cap_c else {}
        check("B context7 backend sees NO Authorization (no backendAuth, bearer stripped)",
              rec.get("auth") is None, str(rec.get("hdrs")))
        code, _, _ = rpc("/context7/mcp", tk_ctx, method="tools/list")
        code, _, _ = rpc("/context7/mcp", tk_ctx, method="tools/call")
        check("B tools/call through the stateless route succeeds", code == 200, str(code))
        code, _, _ = rpc("/grafana/mcp", token(KEY, KID, scope="mcp_grafana openid"))
        check("B grafana route (from the comment block) serves mcp_grafana bearers",
              code == 200, str(code))
        rec = cap_g[-1] if cap_g else {}
        check("B grafana backend sees a bearer at all (passthrough, not a stripped edge)",
              rec.get("bearer") is not None, f"backend bearer={rec.get('bearer')}")
        tk_graf = token(KEY, KID, scope="mcp_grafana")
        rpc("/grafana/mcp", tk_graf)
        rec = cap_g[-1]
        check("B passthrough forwards exactly the validated token, byte-identical",
              rec.get("bearer") == fp(tk_graf), f"{rec.get('bearer')} vs {fp(tk_graf)}")
        code, _, _ = rpc("/context7/mcp", tk_graf)
        check("B grafana scope on the context7 route is 403 (scope is per-route, not global)",
              code == 403, str(code))
        code, _, _ = rpc("/context7/mcp", token(KEY, KID, scope="mcp_context7x"))
        check("B near-miss scope string is 403", code == 403, str(code))
        code, _, _ = rpc("/context7/mcp", token(KEY, KID, scope=["mcp_context7"]))
        check("B scope as ARRAY (ID-token shape) is 403", code == 403, str(code))
        code, _, _ = rpc("/context7/mcp", token(KEY, KID, scope=None))
        check("B missing scope claim is 403", code == 403, str(code))
        code, _, _ = rpc("/context7/mcp", token(KEY, KID, email="someone@example.com",
                                                scope="mcp_context7"))
        check("B correct scope PLUS an email claim is 403 (identity presence beats scope)",
              code == 403, str(code))
        code, _, _ = rpc("/context7/mcp", token(KEY, KID, preferred_username="someone",
                                                scope="mcp_context7"))
        check("B correct scope PLUS preferred_username is 403", code == 403, str(code))
        code, _, _ = rpc("/context7/mcp", token(KEY, KID, iss="http://127.0.0.1:%d" % free_port(),
                                                scope="mcp_context7"))
        check("B wrong issuer is 401", code == 401, str(code))
        now = int(time.time())
        code, _, _ = rpc("/context7/mcp", token(KEY, KID, iat=now - 7200, nbf=now - 7200,
                                                exp=now - 3600, scope="mcp_context7"))
        check("B expired token is 401", code == 401, str(code))
        code, _, _ = rpc("/context7/mcp", token(OTHER, KID, scope="mcp_context7"))
        check("B unknown signing key is 401", code == 401, str(code))
        code, _, _ = rpc("/context7/mcp", token(KEY, "other-kid", scope="mcp_context7"))
        check("B unknown kid is 401", code == 401, str(code))
        n_before = len(cap_c)
        rpc("/context7/mcp", tk_ctx, extra={"Cookie": "session=evil", "X-Grafana-URL": "http://x",
                                            "X-Grafana-Service-Account-Token": "leak",
                                            "X-Session-Id": "forged"})
        rec = cap_c[-1]
        check("B stripped headers never traverse the edge on context7",
              len(cap_c) == n_before + 1 and rec["cookie"] is None
              and rec["x_grafana_url"] is None and rec["x_sa_token"] is None
              and rec["session_id_hdr"] is None,
              str({k: rec[k] for k in ("cookie", "x_grafana_url", "x_sa_token", "session_id_hdr")}))
        rpc("/grafana/mcp", tk_graf, extra={"Cookie": "session=evil",
                                            "X-Grafana-URL": "http://evil",
                                            "X-Grafana-Service-Account-Token": "leak"})
        rec = cap_g[-1]
        check("B grafana backend sees no caller Cookie/URL override but DOES see its bearer",
              rec["cookie"] is None and rec["x_grafana_url"] is None
              and rec["x_sa_token"] is None and rec["bearer"] == fp(tk_graf), str(rec["bearer"]))
        check("B stateless: the gateway issues no Mcp-Session-Id anywhere",
              all(r["session_id_hdr"] is None for r in cap_c + cap_g))
        return cfg, path, proc, fh, port
    except Exception:
        stop(proc, fh)
        raise


def run_c(cfg, path, proc, fh, port, grafana_route):
    print("\n=== C: rewrite the watched config file, no restart ===")
    cfg = json.loads(json.dumps(cfg))  # detach
    cfg["routes"] = [r for r in cfg["routes"] if r["name"] != "grafana"]
    pid_before = proc.pid
    write(cfg, path)
    deadline, code = time.time() + 60, None
    while time.time() < deadline:
        code, _, _ = rpc("/grafana/mcp", token(KEY, KID, scope="mcp_grafana"), port=port)
        if code == 404:
            break
        time.sleep(1)
    check("C grafana route disappears WITHOUT a restart (ConfigMap edit applies in place)",
          code == 404 and proc.poll() is None and proc.pid == pid_before,
          f"code={code} pid={pid_before}")
    code, _, _ = rpc("/context7/mcp", token(KEY, KID, scope="mcp_context7"), port=port)
    check("C the live context7 route is unaffected by the reload", code == 200, str(code))
    stop(proc, fh)


def run_d(text, grafana_route):
    """Why the passthrough route can ALSO strip Authorization: header modification runs
    before backend auth, so the inbound credential is deleted and then replaced by the
    validated one. Measured here so the disabled route does not have to guess."""
    print("\n=== D: backendAuth.passthrough with Authorization in the strip list ===")
    port, ip, gp = free_port(), free_port(), free_port()
    GW.update({"port": port})
    globals()["ISSUER_URL"] = f"http://127.0.0.1:{ip}"
    serve(type("I", (Issuer,), {}), ip)
    serve(GrafanaCapture, gp)
    cfg = base_cfg(text, port)
    graf = json.loads(json.dumps(grafana_route))
    graf["policies"]["mcpAuthentication"]["issuer"] = ISSUER_URL
    graf["policies"]["mcpAuthentication"]["jwks"] = {"url": f"{ISSUER_URL}/jwks"}
    graf["policies"]["requestHeaderModifier"]["remove"].insert(0, "Authorization")
    graf["backends"][0]["mcp"]["targets"][0]["mcp"] = {"host": "127.0.0.1", "port": gp,
                                                       "path": "/mcp"}
    cfg["routes"] = [graf]
    path = f"{WORK}/configD.yaml"
    write(cfg, path)
    proc, fh = boot(path, f"{WORK}/D.log")
    try:
        if not wait_port(port) or proc.poll() is not None:
            check("D boots with Authorization stripped AND backendAuth.passthrough", False,
                  "config rejected or process died")
            return
        tk = token(KEY, KID, scope="mcp_grafana")
        code, _, _ = rpc("/grafana/mcp", tk, port=port)
        rec = CAPTURES["grafana"][-1] if CAPTURES["grafana"] else {}
        check("D passthrough still delivers the validated bearer when Authorization is stripped",
              code == 200 and rec.get("bearer") == fp(tk),
              f"code={code} backend={rec.get('bearer')} want={fp(tk)} "
              "(order-dependent: if the backend sees none, the strip list must stay as committed)")
    finally:
        stop(proc, fh)


def ensure_binary():
    if os.path.exists(BIN):
        return True
    os.makedirs(CACHE, exist_ok=True)
    print(f"fetching pinned agentgateway {BIN_VERSION} -> {BIN}")
    try:
        subprocess.run(["curl", "-fsSL", "-o", BIN, BIN_URL], check=True, timeout=600)
    except Exception as exc:  # noqa: BLE001
        print(f"cannot fetch the binary: {exc}\nSet AGENTGATEWAY_BIN=/path/to/agentgateway")
        return False
    digest = hashlib.sha256(open(BIN, "rb").read()).hexdigest()
    if digest != BIN_SHA256:
        print(f"pinned sha256 mismatch: {digest}")
        os.remove(BIN)
        return False
    os.chmod(BIN, 0o755)
    return True


def main():
    if not ensure_binary():
        return 1
    text = rendered_config()
    values_text = open(f"apps/{CHART}/values.yaml").read()
    grafana_route = commented_route(values_text, "- name: grafana")
    cfg = yaml.safe_load(text)
    check("rendered config has NO grafana MCP route (run A and B depend on that)",
          all(r["name"] != "grafana" for r in cfg["routes"]))
    run_a(text)
    got = run_b(text, grafana_route)
    run_c(*got, grafana_route)
    run_d(text, grafana_route)
    print(f"\nlog files: {WORK}")
    if not os.environ.get("KEEP"):
        shutil.rmtree(WORK, ignore_errors=True)
    print(f"\n=== {sum(RESULTS)} passed, {RESULTS.count(False)} failed ===")
    return 1 if RESULTS.count(False) else 0


if __name__ == "__main__":
    sys.exit(main())
