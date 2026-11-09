#!/usr/bin/env python3
"""Official Vault MCP server (hashicorp/vault-mcp-server:0.2.0) behind the pinned
agentgateway v1.7.0 - local product proof, no live cluster involvement.

Question: can the official HashiCorp MCP product be fronted by the gateway's
chart-rendered Vault route while each caller uses their OWN Vault token?

Everything is measured against real containers, not documentation:

  * a disposable Vault dev server (hashicorp/vault:1.15) holding two KV paths, with
    two native Vault ACL tokens of DIFFERENT visibility plus one token whose policy
    matches neither, all created over Vault's own HTTP API;
  * two `hashicorp/vault-mcp-server:0.2.0` instances (digest verified) started with
    NO ambient VAULT_TOKEN - asserted from `docker inspect` - so an env fallback can
    never masquerade as per-request delegation;
  * the gateway config rendered from apps/mcp-gateway-admin by `helm template`
    (test/mcp-gateway/chartconfig.py), never a hand-copied config.

Measured matrix:

  [1] per-request credential, direct to the product     -> does X-Vault-Token work at all?
  [2] no credential anywhere                             -> the product's own error
  [3] stateful session, credential rotation              -> per-request or cached per session?
  [4] session id from instance 1 replayed at instance 2  -> backend-side session binding,
      and the same session id presented with NO credential at all
  [5] ?vault_token= query form                           -> the product's own rejection
  [6] caller-supplied VAULT_ADDR toward a canary         -> target-override vector
  [7] through the gateway, exactly as the chart renders  -> what the shipped route delivers
  [8] stateless + native header                          -> the minimal change that works

Test-only deviations from the shipped chart, each printed with a TEST-ONLY line:
  (a) `statefulMode: stateless` - the chart renders no such field, so this measures the
      mode the route would need; it does not ship it;
  (b) not stripping `x-vault-token` for case [8] - the shipped config DOES strip it.

Nothing here enables a route in the repo's values, and no secret value is printed:
tokens are identified only by which test used them.
"""
import base64
import hashlib
import http.server
import io
import ssl
import json
import os
import re
import socket
import subprocess
import sys
import threading
import time

from cryptography.hazmat.primitives.asymmetric import ec
import jwt

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import chartconfig  # noqa: E402

AGENTGATEWAY_VERSION = "v1.7.0"
WORK = "/tmp/opencode/vmcp-probe"
os.makedirs(WORK, exist_ok=True)

VAULT_IMAGE = "hashicorp/vault:1.15"
MCP_IMAGE = ("hashicorp/vault-mcp-server@sha256:"
             "e4e8df2fb7d27f12c5cb8d1c5b37663a9569ad5ce0047fa0281c56a1769222aa")
PREFIX = "vmcp-probe-"
BACKEND_HOST = "127.0.0.1"
GW_PORT, STATS_PORT, READY_PORT = 14200, 14201, 14202
ISSUER_PORT, CANARY_PORT = 14203, 14204
TLSVAULT_PORT = 14206
TLS_ENDPOINT = {"n": 0, "paths": [], "no_token": 0, "conns": 0}
STRIP_EXPECT = ["vault_addr", "vault-addr", "vault_token", "vault_skip_verify",
                "vault-skip-verify", "vault_namespace", "vault-namespace",
                "x-vault-namespace"]
MARKER_A = "A_ONLY_SECRET_MARKER"
MARKER_B = "B_ONLY_SECRET_MARKER"
CANARY_MARKER = "TARGET_OVERRIDE_LEAK"
TLS_MARKER = "TLS_ENDPOINT_SECRET_MARKER"
EDGE_JWT = []                      # the synthetic edge JWTs this probe mints

results = []          # (kind, name, ok, detail); kind REQUIRED|CONTROL|OBSERVED|NOTTESTED
containers = []
procs = []

# Every value below is a fixture secret (synthetic, never a real credential). They are
# registered here and scanned out of the program's own output at the end: a proof that
# prints the payload it is trying to keep secret is not a proof.
FORBIDDEN = [MARKER_A, MARKER_B, CANARY_MARKER, TLS_MARKER]
SESSION_IDS = []      # every session id we ever saw; audited, never printed
OUT = []              # captured stdout AND stderr


class _Tee(io.TextIOBase):
    """Capture our own stdout so the redaction check can audit what was really printed."""

    def __init__(self, inner):
        self.inner = inner

    def write(self, text):
        OUT.append(text)
        return self.inner.write(text)

    def flush(self):
        return self.inner.flush()


def classify(text):
    """A capability signature, never the payload.

    Earlier versions of this probe printed raw response excerpts, and the fixture
    markers live inside those responses, so the log leaked the very values the report
    claimed were never printed (observed 7 times in the recorded output). Everything now
    goes through this: it names what a response *proves*, not what it contains.
    """
    low = (text or "").lower()
    tags = []
    if MARKER_A in (text or ""):
        tags.append("has:A-marker")
    if MARKER_B in (text or ""):
        tags.append("has:B-marker")
    if CANARY_MARKER in (text or ""):
        tags.append("has:canary-marker")
    if TLS_MARKER in (text or ""):
        tags.append("has:tls-marker")
    for phrase, tag in (("permission denied", "vault:permission-denied"),
                        ("token not provided", "product:no-credential"),
                        # x509 trust failure gets its own tag: [10b] asserts on exactly
                        # this phrase, not on any other TLS-ish wording
                        ("x509", "tls:x509-verify-failed"),
                        ("certificate signed by unknown authority",
                         "tls:x509-verify-failed"),
                        ("error performing https request", "tls:https-request-failed"),
                        ("certificate", "tls:certificate-word"),
                        ("no active session", "gateway:no-active-session"),
                        ("jwt token required", "edge:jwt-required")):
        if phrase in low:
            tags.append(tag)
    return " ".join(tags) or "no-recognised-signal"


def req(name, ok, detail=""):
    """A security-required assertion: a violation fails the run."""
    results.append(("REQUIRED", name, bool(ok), detail))
    print(f"  {'PASS' if ok else 'VIOLATION':22} [required] {name}"
          + (f"\n       {detail}" if detail else ""))


def ctrl(name, reproduced, detail=""):
    """An expected-known-bad control: the vulnerability MUST reproduce, or the harness
    proves nothing. Never reported as a pass of the product."""
    results.append(("CONTROL", name, bool(reproduced), detail))
    print(f"  {'EXPECTED-BAD' if reproduced else 'CONTROL-BROKE':22} "
          f"[control] {name}" + (f"\n       {detail}" if detail else ""))


def obs(name, detail=""):
    results.append(("OBSERVED", name, None, detail))
    print(f"  {"measured":22} [observed]  {name}" + (f"\n       {detail}" if detail else ""))

# --- Kanidm-shaped ACCESS token minting (same contract as e2e-authz.py) --------
KEY = ec.generate_private_key(ec.SECP256R1())
PUB = KEY.public_key().public_numbers()
KID = hashlib.sha256(f"{PUB.x:x}{PUB.y:x}".encode()).hexdigest()[:12]


def b64u(num, size):
    return base64.urlsafe_b64encode(num.to_bytes(size, "big")).rstrip(b"=").decode()


JWKS = {"keys": [{"kty": "EC", "crv": "P-256", "x": b64u(PUB.x, 32), "y": b64u(PUB.y, 32),
                 "alg": "ES256", "use": "sig", "kid": KID}]}


def mint(aud, scope=None):
    now = int(time.time())
    payload = {
        "iss": f"http://{BACKEND_HOST}:{ISSUER_PORT}",
        "sub": "11111111-1111-4111-8111-111111111111",
        "aud": aud, "exp": now + 600, "iat": now, "nbf": now,
        "jti": f"probe-{now}", "client_id": aud,
    }
    if scope is not None:
        payload["scope"] = scope
    return jwt.encode(payload, KEY, algorithm="ES256",
                      headers={"kid": KID, "typ": "at+jwt"})


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


def serve(handler, port, host=BACKEND_HOST):
    # host defaults to 127.0.0.1; the canary must bind 0.0.0.0 so a request dialled
    # from INSIDE a container can reach it
    httpd = http.server.ThreadingHTTPServer((host, port), handler)
    threading.Thread(target=httpd.serve_forever, daemon=True).start()
    return httpd


# ---------------------------------------------------------------- plumbing
def record(name, verdict, detail=""):
    """Compatibility shim for the old (verdict-string) style.

    "PASS"/"FAIL" become a REQUIRED assertion, "LEAK:"/"MEASURED: honoured" become a
    control, anything else is an observation. No verdict string is silently counted as a
    pass: `verdict.startswith("FAIL")` is a violation and `MEASURED` alone is not a pass.
    """
    if verdict.startswith("PASS"):
        req(name, True, detail)
    elif verdict.startswith("FAIL"):
        req(name, False, detail)
    elif verdict.startswith("LEAK"):
        # A leak is a security violation, not an expected-bad control: the harness must
        # never be able to turn it into a held assertion.
        req(name, False, f"{verdict} {detail}".strip())
    else:
        obs(name, f"{verdict} {detail}".strip())


def sh(args, timeout=180):
    return subprocess.run(args, capture_output=True, text=True, timeout=timeout)


def docker(*args, **kw):
    r = sh(["docker", *args], **kw)
    if r.returncode != 0:
        raise RuntimeError(f"docker {' '.join(args)}: {r.stderr.strip()[-400:]}")
    return r.stdout


def cleanup():
    for p in procs:
        try:
            p.terminate()
        except OSError:
            pass
    for name in containers:
        sh(["docker", "rm", "-f", name])


def start(name, image, args=None, env=None, volumes=()):
    """`volumes` is a list of (host_path, container_path), mounted read-only. Used once:
    the disposable HTTPS Vault fixture's CA inside the official MCP container."""
    cmd = ["docker", "run", "-d", "--name", name]
    for src, dst in volumes:
        cmd += ["-v", f"{src}:{dst}:ro"]
    for k, v in (env or {}).items():
        cmd += ["-e", f"{k}={v}"]
    cmd += [image]
    if args:
        cmd += list(args)
    r = sh(cmd)
    if r.returncode != 0:
        raise RuntimeError(f"start {name}: {r.stderr.strip()[-400:]}")
    containers.append(name)
    return ip_of(name)


def ip_of(name):
    return docker("inspect", "-f",
                  "{{range .NetworkSettings.Networks}}{{.IPAddress}}{{end}}", name).strip()


def wait_tcp(host, port, timeout=90, note=""):
    deadline = time.time() + timeout
    while time.time() < deadline:
        try:
            with socket.create_connection((host, port), timeout=2):
                return True
        except OSError:
            time.sleep(1)
    raise RuntimeError(f"{host}:{port} never listened ({note})")


def http_json(method, url, body=None, headers=None, timeout=20):
    import urllib.error
    import urllib.request
    data = json.dumps(body).encode() if body is not None else None
    req = urllib.request.Request(url, data=data, method=method)
    for k, v in (headers or {}).items():
        req.add_header(k, v)
    try:
        with urllib.request.urlopen(req, timeout=timeout) as r:
            return r.status, json.loads(r.read().decode() or "{}")
    except urllib.error.HTTPError as e:
        raw = e.read().decode()
        try:
            return e.code, json.loads(raw or "{}")
        except json.JSONDecodeError:
            return e.code, {"raw": raw[:300]}


# ---------------------------------------------------------------- Vault fixture
def vault_ready(addr):
    return wait_tcp(urllib_parse_host(addr), 8200, note="vault dev")


def urllib_parse_host(addr):
    return addr.split("://", 1)[1].split(":")[0]


def vault_mount_info(addr, token):
    st, body = http_json("GET", f"{addr}/v1/sys/mounts", headers={"X-Vault-Token": token})
    if st != 200:
        raise RuntimeError(f"sys/mounts -> {st}")
    info = body.get("data", {}).get("secret/", {})
    return info.get("type"), (info.get("options") or {}).get("version", "1")


def vault_write(addr, token, path, data, kv):
    api = f"{addr}/v1/secret/data/{path}" if kv == "2" else f"{addr}/v1/secret/{path}"
    return http_json("POST", api, {"data": data} if kv == "2" else data,
                     {"X-Vault-Token": token})


def vault_token_for(addr, root, policy, paths, extra_paths=()):
    """Native Vault ACL: one policy that covers exactly one path family, one token
    bound to it. Nothing here is gateway-specific - this is Vault's own authorisation."""
    rules = "\n".join(f'path "{p}" {{\n  capabilities = ["read", "list"]\n}}\n'
                      for p in tuple(paths) + tuple(extra_paths))
    st, _ = http_json("PUT", f"{addr}/v1/sys/policies/acl/{policy}", {"policy": rules},
                      {"X-Vault-Token": root})
    if st not in (200, 204):          # Vault answers policy writes with 204
        raise RuntimeError(f"policy {policy} -> {st}")
    st, body = http_json("POST", f"{addr}/v1/auth/token/create",
                         {"policies": [policy], "no_parent": True, "ttl": "2h",
                          "display_name": f"probe-{policy}"},
                         {"X-Vault-Token": root})
    if st != 200:
        raise RuntimeError(f"token create {policy} -> {st}: {json.dumps(body)[:180]}")
    return body["auth"]["client_token"]


# ---------------------------------------------------------------- MCP client
def mcp(base, path, method, params=None, token=None, headers=None, session=None,
        req_id=1, expect_headers=True):
    """One streamable-HTTP JSON-RPC call. Returns (status, session_id, response text)."""
    body = {"jsonrpc": "2.0", "method": method}
    if req_id is not None:
        body["id"] = req_id
    if params is not None:
        body["params"] = params
    cmd = ["curl", "-sS", "-m", "30", "-D", "-",
           "-H", "Accept: application/json, text/event-stream",
           "-H", "Content-Type: application/json",
           "-H", "mcp-protocol-version: 2025-06-18"]
    if token:
        cmd += ["-H", f"Authorization: Bearer {token}"]
    if session:
        cmd += ["-H", f"mcp-session-id: {session}"]
    for k, v in (headers or {}).items():
        cmd += ["-H", f"{k}: {v}"]
    cmd += ["-d", json.dumps(body), f"{base}{path}"]
    out = sh(cmd).stdout
    head, sep, payload = out.partition("\r\n\r\n")
    if not sep:
        head, _, payload = out.partition("\n\n")
    lines = head.splitlines()
    status = ""
    if lines:
        m = re.search(r"HTTP/[\d.]+ (\d+)", lines[0])
        status = m.group(1) if m else ""
    sess = ""
    for ln in lines:
        if ln.lower().startswith("mcp-session-id:"):
            sess = ln.split(":", 1)[1].strip()
    if sess:
        SESSION_IDS.append(sess)   # registered for the output audit, never printed
    text = payload
    for ln in payload.splitlines():
        if ln.startswith("data:"):
            text = ln[5:].strip()
            break
    return status, sess, text


INIT = {"protocolVersion": "2025-06-18", "capabilities": {},
        "clientInfo": {"name": "vault-proof", "version": "1"}}


def mcp_init(base, path, token=None, headers=None):
    """initialize + the `notifications/initialized` the MCP SDK expects before any
    other request. Returns (status, session_id, init_text)."""
    st, sess, txt = mcp(base, path, "initialize", INIT, token=token, headers=headers)
    if st == "200" and sess:
        mcp(base, path, "notifications/initialized", {}, token=token, headers=headers,
            session=sess, req_id=None)
    return st, sess, txt


def tool_call(base, path, tool, args, **kw):
    st, _, txt = mcp(base, path, "tools/call", {"name": tool, "arguments": args},
                     req_id=7, **kw)
    return st, txt


# ---------------------------------------------------------------- gateway
def stateless_patch(cfg):
    """TEST-ONLY deviation (a): the chart renders no `statefulMode`, so force the
    per-request mode on the route's LocalMcpBackend (`mcp:` block, sibling of
    `targets:`) and measure what it does. Indentation is taken from the rendered text,
    not assumed."""
    m = re.search(r"( *)- mcp:\n( *)targets:\n", cfg)
    if not m or cfg.count(m.group(0)) != 1:
        raise RuntimeError("statefulMode patch point not found or not unique")
    lead, block = m.group(1), m.group(2)
    repl = f"{lead}- mcp:\n{block}statefulMode: stateless\n{block}targets:\n"
    print("TEST-ONLY deviation (a): `statefulMode: stateless` injected on the Vault "
          "backend - the chart renders no such field")
    return cfg.replace(m.group(0), repl, 1)


def unstrip_patch(cfg):
    """TEST-ONLY deviation (b): the shipped config strips x-vault-token, the only
    credential header the official product reads. Drop that one entry to compare."""
    m = re.search(r"( *)- x-vault-token\n", cfg)
    old = m.group(0) if m else None
    if not old or cfg.count(old) != 1:
        raise RuntimeError("strip-list patch point not found or not unique")
    print("TEST-ONLY deviation (b): `x-vault-token` dropped from the strip list - "
          "the shipped chart DOES strip it")
    return cfg.replace(old, "", 1)


def boot(cfg_text, tag):
    cfg_path = os.path.join(WORK, f"cfg-{tag}.yaml")
    log_path = os.path.join(WORK, f"log-{tag}.txt")
    # 0600: these files can carry the rendered config and the gateway's own log lines.
    fd = os.open(cfg_path, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
    with os.fdopen(fd, "w") as fh_cfg:
        fh_cfg.write(cfg_text)
    fd2 = os.open(log_path, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
    with os.fdopen(fd2, "wb") as fh:
        procs.append(subprocess.Popen([BINARY, "-f", cfg_path], stdout=fh,
                                      stderr=subprocess.STDOUT, cwd=WORK))
    for _ in range(60):
        try:
            with socket.create_connection((BACKEND_HOST, GW_PORT), timeout=1):
                return log_path
        except OSError:
            time.sleep(0.5)
    # never echo the gateway log into the exception - it goes through classify()
    raise RuntimeError(f"gateway {tag} never listened: {classify(open(log_path).read())}")


def render_vault_route(be_url, host_hdr, resource, credential):
    return chartconfig.render(
        "apps/mcp-gateway-admin", "mcp-gateway-admin", "mcp-gateway-admin",
        ["backends.vault.enabled=true",
         f"backends.vault.url={be_url}",
         f"backends.vault.hostHeader={host_hdr}",
         f"backends.vault.resource={resource}",
         f"backends.vault.credential={credential}",
         f"auth.issuer=http://{BACKEND_HOST}:{ISSUER_PORT}",
         f"auth.jwksUrl=http://{BACKEND_HOST}:{ISSUER_PORT}/jwks",
         f"gateway.port={GW_PORT}"],
        ("jwt.scope.split(' ').exists(s, s == 'mcp_vault')", "vault_addr"),
        quiet=True, stats_port=STATS_PORT, readiness_port=READY_PORT)


def get_binary():
    if len(sys.argv) > 1:
        return sys.argv[1]
    path = os.path.join(WORK, "agentgateway")
    if not os.path.exists(path):
        import urllib.request
        url = ("https://github.com/agentgateway/agentgateway/releases/download/"
               f"{AGENTGATEWAY_VERSION}/agentgateway-linux-amd64")
        print(f"Fetching agentgateway {AGENTGATEWAY_VERSION} ...")
        with urllib.request.urlopen(url, timeout=300) as resp, open(path, "wb") as fh:
            fh.write(resp.read())
        os.chmod(path, 0o755)
    return path


def gw_ip_for_cert():
    """The bridge gateway IP, used only for the canary certificate's SAN."""
    return subprocess.run(["docker", "network", "inspect", "bridge", "-f",
                            "{{range .IPAM.Config}}{{.Gateway}}{{end}}"],
                          capture_output=True, text=True).stdout.strip()


TLS_PORT = 0


def main():
    sys.stdout = _Tee(sys.stdout)
    sys.stderr = _Tee(sys.stderr)
    if sh(["docker", "version"]).returncode != 0:
        raise SystemExit("docker is required for this probe")
    for img in (VAULT_IMAGE, MCP_IMAGE):
        if sh(["docker", "image", "inspect", img]).returncode != 0:
            print(f"Pulling {img} ...")
            docker("pull", img, timeout=900)
    global BINARY
    BINARY = get_binary()
    import atexit
    atexit.register(cleanup)

    serve(IssuerHandler, ISSUER_PORT)
    hits = {}

    class CanaryHandler(http.server.BaseHTTPRequestHandler):
        def log_message(self, *a):
            pass

        def do_GET(self):
            hits["n"] = hits.get("n", 0) + 1
            body = json.dumps({"marker": CANARY_MARKER}).encode()
            self.send_response(200)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)

        def do_POST(self):
            self.do_GET()

    serve(CanaryHandler, CANARY_PORT, host="0.0.0.0")

    # The same handler over TLS with a self-signed certificate, for the negative test
    # that a retargeted address cannot simply ignore certificate validation.
    global TLS_PORT
    TLS_PORT = CANARY_PORT + 1
    key_p, crt_p = os.path.join(WORK, "canary.key"), os.path.join(WORK, "canary.pem")
    if not os.path.exists(crt_p):
        sh(["openssl", "req", "-x509", "-newkey", "ec", "-pkeyopt",
            "ec_paramgen_curve:prime256v1", "-nodes", "-keyout", key_p, "-out", crt_p,
            "-days", "1", "-subj", "/CN=canary", "-addext",
            f"subjectAltName=IP:{gw_ip_for_cert() or '127.0.0.1'}"])
    tls_httpd = http.server.ThreadingHTTPServer(("0.0.0.0", TLS_PORT), CanaryHandler)
    tls_ctx = ssl.SSLContext(ssl.PROTOCOL_TLS_SERVER)
    tls_ctx.load_cert_chain(crt_p, key_p)
    tls_httpd.socket = tls_ctx.wrap_socket(tls_httpd.socket, server_side=True)
    threading.Thread(target=tls_httpd.serve_forever, daemon=True).start()

    # ---- Vault fixture -------------------------------------------------------
    root = f"root-{os.urandom(8).hex()}"
    va = start(PREFIX + "vault", VAULT_IMAGE,
               ["server", "-dev", f"-dev-root-token-id={root}",
                "-dev-listen-address=0.0.0.0:8200", "-log-level=ERROR"])
    addr_a = f"http://{va}:8200"
    if not vault_ready(addr_a):
        raise SystemExit("vault dev never became ready")
    time.sleep(2)
    kv_type, kv = vault_mount_info(addr_a, root)
    if kv != "2":
        http_json("DELETE", f"{addr_a}/v1/sys/mounts/secret", headers={"X-Vault-Token": root})
        http_json("POST", f"{addr_a}/v1/sys/mounts/secret",
                  {"type": "kv", "options": {"version": "2"}}, {"X-Vault-Token": root})
        kv_type, kv = vault_mount_info(addr_a, root)
    print(f"Vault fixture: {addr_a} mount secret/ type={kv_type} version={kv}")

    vault_write(addr_a, root, "a/canary", {"value": MARKER_A}, kv)
    vault_write(addr_a, root, "b/canary", {"value": MARKER_B}, kv)
    pre = "secret/data/" if kv == "2" else "secret/"
    # A ladder of native Vault ACL shapes, from least privilege upward. The point is
    # to find out which token the *product* can actually use, because read_secret
    # resolves the mount before reading it (measured: a one-path token fails with
    # "failed to list mounts"), and that determines whether per-caller Vault
    # credentials can be least-privilege at all.
    WIDE = ("sys/mounts", "sys/internal/ui/mounts")
    shapes = [
        # label, token, the path that token's own policy covers
        ("root (dev token)", root, "a/canary"),
        ("one path only",
         vault_token_for(addr_a, root, "proof-min-a", (f"{pre}a/canary",)), "a/canary"),
        ("own path + metadata + sys/mounts",
         vault_token_for(addr_a, root, "proof-wide-a", (f"{pre}a/canary",),
                         ("secret/metadata/a/*",) + WIDE), "a/canary"),
        ("own path (b) + metadata + sys/mounts",
         vault_token_for(addr_a, root, "proof-wide-b", (f"{pre}b/canary",),
                         ("secret/metadata/b/*",) + WIDE), "b/canary"),
        ("matching nothing",
         vault_token_for(addr_a, root, "proof-none", (f"{pre}nope/canary",), WIDE),
         "a/canary"),
    ]

    # ---- official MCP backends, NO ambient VAULT_TOKEN ----------------------
    bes = {}
    for n, name in ((1, PREFIX + "be1"), (2, PREFIX + "be2")):
        # the image has no ENTRYPOINT and its CMD is ["/bin/vault-mcp-server","stdio"],
        # so the run args must repeat the binary path to override the stdio default
        bes[n] = start(name, MCP_IMAGE,
                       ["/bin/vault-mcp-server", "streamable-http",
                        "--transport-host", "0.0.0.0",
                        "--transport-port", "8000", "--mcp-endpoint", "/mcp"],
                       env={"VAULT_ADDR": addr_a})
    env_dump = docker("inspect", "-f", "{{json .Config.Env}}", PREFIX + "be1")
    if "VAULT_TOKEN" in env_dump:
        raise SystemExit("backend has an ambient VAULT_TOKEN: the proof would be meaningless")
    print("both backend instances: NO ambient VAULT_TOKEN (docker inspect), VAULT_ADDR "
          "fixed to the fixture by deployment")
    for n in (1, 2):
        wait_tcp(bes[n], 8000, note=f"backend {n}")

    base1, base2 = f"http://{bes[1]}:8000", f"http://{bes[2]}:8000"
    # the canary runs on the host, so its address has to be reachable from INSIDE the
    # backend container: that is the docker bridge gateway, not 127.0.0.1
    gw_ip = docker("inspect", "-f",
                   "{{range $k, $v := .NetworkSettings.Networks}}{{ $v.Gateway }}{{end}}",
                   PREFIX + "be1").strip()
    canary_addr = f"http://{gw_ip}:{CANARY_PORT}"
    print(f"canary target address (host as seen from the containers): {canary_addr}")

    # discover the read tool + its argument names from the product itself. The root
    # token is used here only for discovery; the ACL ladder below decides which token
    # shapes a caller could actually use.
    disc_tok = shapes[0][1]          # the dev root token, used only for tool discovery
    st, s_probe, _ = mcp_init(base1, "/mcp", headers={"X-Vault-Token": disc_tok})
    req("[0] product reachable, initialize+initialized accepted", st == "200",
           f"HTTP {st}, session id length {len(s_probe)} chars")
    st, _, lst = mcp(base1, "/mcp", "tools/list", {}, session=s_probe,
                     headers={"X-Vault-Token": disc_tok})
    tool_name, arg_keys = None, []
    m = re.search(r"\{.*\}", lst, re.S)
    if m:
        try:
            doc = json.loads(m.group(0))
            tools = doc.get("result", {}).get("tools", [])
            print(f"tools/list HTTP {st}: {[t['name'] for t in tools]}")
            for t in tools:
                props = (t.get("inputSchema") or {}).get("properties", {})
                if "read" in t["name"] and ("kv2" in t["name"] or "secret" in t["name"]):
                    tool_name = t["name"]
                    arg_keys = list(props)
                    break
        except json.JSONDecodeError:
            pass
    if not tool_name:
        raise SystemExit(f"no read tool discovered; tools/list signature: {classify(lst)}")
    path_key = "path" if "path" in arg_keys else arg_keys[0]
    eng_key = next((k for k in arg_keys if "engine" in k or "mount" in k), None)
    print(f"read tool: {tool_name} args {arg_keys}")

    def read_args(path):
        a = {path_key: path}
        if eng_key:
            a[eng_key] = "secret"
        return a

    # ---- [1] which native ACL shape can the product use, per request? -----
    print("\n[1] official product, direct: which native Vault ACL shape works?")
    tok_a = tok_b = tok_none = None
    usable = []
    marker_of = {"a/canary": MARKER_A, "b/canary": MARKER_B}
    other_of = {"a/canary": "b/canary", "b/canary": "a/canary"}
    for label, tok, own_path in shapes:
        st, sx, _ = mcp_init(base1, "/mcp", headers={"X-Vault-Token": tok})
        st, txt_own = tool_call(base1, "/mcp", tool_name, read_args(own_path),
                                session=sx, headers={"X-Vault-Token": tok})
        marker = marker_of[own_path]
        if marker in txt_own:
            own = f"reads {own_path}"
        elif "permission denied" in txt_own.lower() or "403" in txt_own:
            own = f"denied on {own_path}"
        else:
            own = f"FAILED: {classify(txt_own)}"
        st, txt_other = tool_call(base1, "/mcp", tool_name, read_args(other_of[own_path]),
                                  session=sx, headers={"X-Vault-Token": tok})
        other = ("LEAK: also reads " + other_of[own_path]
                 if marker_of[other_of[own_path]] in txt_other
                 else "cannot read " + other_of[own_path])
        detail = f"init HTTP {st}; own path: {own}; other path: {other}"
        works = own.startswith("reads") and other.startswith("cannot")
        is_root = label.startswith("root")
        record(f"[1] ACL shape: {label}",
               "USABLE (but root-equivalent)" if works and is_root
               else "USABLE" if works else "NOT USABLE", detail)
        if works and not is_root:
            usable.append((label, tok))
    # the two identities used by the rest of the matrix: least-privilege where the
    # product allows it, otherwise the root token, with the limitation recorded
    if len(usable) >= 2:
        tok_a = next(t for l, t in usable if "a)" not in l and "path (b)" not in l)
        tok_b = next(t for l, t in usable if "path (b)" in l)
        tok_none = shapes[-1][1]
        record("[1x] least-privilege delegation is possible", "PASS",
               "a per-path token (plus the mount metadata the product itself reads) "
               "works and stays inside its own ACL")
    else:
        tok_a = tok_b = root
        record("[1x] least-privilege delegation is possible",
               "BLOCKER: only a root-equivalent token works",
               "every narrower ACL shape fails inside the product, so a caller could "
               "not hand it a normal user token")

    named = {"a": tok_a, "b": tok_b, "none": tok_none}
    FORBIDDEN.extend(v for v in (root, tok_a, tok_b, tok_none) if v)
    print("NOTE: the edge JWTs in this probe are minted by a local stub (synthetic "
          "ES256 keys, per-client issuer). They are NOT live Kanidm tokens; live "
          "registration and the live two-user test stay open.")

    # ---- [2] no credential anywhere ----------------------------------------
    print("\n[2] no credential anywhere")
    st, s0, _ = mcp_init(base1, "/mcp")
    st, txt = tool_call(base1, "/mcp", tool_name, read_args("a/canary"), session=s0)
    req("[2] no credential fails closed with the product's own error",
        MARKER_A not in txt and "token" in txt.lower(),
           f"HTTP {st} {classify(txt)}")

    # ---- [3] stateful session semantics ------------------------------------
    print("\n[3] stateful: per-request credential or client cached per session?")
    st, sa, _ = mcp_init(base1, "/mcp", headers={"X-Vault-Token": tok_a})
    st, txt_a = tool_call(base1, "/mcp", tool_name, read_args("a/canary"), session=sa,
                          headers={"X-Vault-Token": tok_a})
    st, txt_rot = tool_call(base1, "/mcp", tool_name, read_args("b/canary"), session=sa,
                            headers={"X-Vault-Token": tok_b})
    sticky = MARKER_B not in txt_rot
    # CONTROL: stateful mode caching the Vault client per session is the vulnerability
    # this plan relies on being true. It must reproduce, and it must never be printed as
    # a pass. If it stops reproducing, the harness is not observing what it claims.
    ctrl("[3a] stateful mode ignores a rotated credential (expected-bad control)",
         sticky,
           f"token B presented on A's stateful session: HTTP {st} "
           f"{classify(txt_rot)} - B's own marker absent means the cached client, not "
           f"the presented credential, served the read")
    st, txt = tool_call(base1, "/mcp", tool_name, read_args("a/canary"),
                        headers={"X-Vault-Token": tok_a})
    obs("[3b] tool call with no session id (stateful mode)",
        f"HTTP {st} {classify(txt)} - shows what the mode does with a credential-bearing "
        f"session; not a security expectation either way")

    # ---- [4] session replay at the other instance --------------------------
    print("\n[4] session id issued by instance 1, replayed at instance 2")
    st, txt = tool_call(base2, "/mcp", tool_name, read_args("a/canary"), session=sa,
                        headers={"X-Vault-Token": tok_a})
    # The caller supplies its OWN token on this call, so a value here is not another
    # user's secret: it shows the second instance accepted a session id it never
    # issued instead of refusing it.
    # CONTROL: in stateful mode a session id is not bound to the instance that issued
    # it. The caller presents its OWN token on this call, so a served read is not
    # another user's secret - it proves the second instance accepted a foreign session
    # id instead of refusing it. Expected-bad, and it must reproduce.
    cross_instance = MARKER_A in txt
    ctrl("[4] stateful session id from instance 1 is accepted by instance 2 "
         "(expected-bad control)", cross_instance,
         f"HTTP {st} {classify(txt)}"
         + ("" if cross_instance else " - CONTROL DID NOT REPRODUCE: this harness can "
            "no longer show the property the stateless requirement rests on"))
    st, txt = tool_call(base1, "/mcp", tool_name, read_args("a/canary"), session=sa)
    # The product's stateful design, not a gateway bug: the credential lives on the
    # cached session client, so the session id alone is a bearer token. This is exactly
    # why a per-caller Vault route has to be stateless.
    bearer_like = MARKER_A in txt
    ctrl("[4b] in stateful mode the session id ALONE serves a read, i.e. it is a "
         "bearer credential (expected-bad control)", bearer_like,
         f"no credential presented on this call: HTTP {st} {classify(txt)}"
         + ("" if bearer_like else " - CONTROL DID NOT REPRODUCE"))

    # ---- [5] the product's own query-parameter handling ---------------------
    # The names are the ENV names (client.go:25-29, middleware.go:133): the product
    # reads `VAULT_ADDR` / `VAULT_TOKEN` / `VAULT_SKIP_VERIFY` as header or query keys,
    # and a token in the QUERY is refused by the product itself (middleware.go:149-152).
    print("\n[5] query strings straight to the product (the names it actually reads)")
    for q, note in ((f"vault_token={tok_a}", "wrong case: the product ignores it"),
                    (f"VAULT_TOKEN={tok_a}", "the name it reads -> its own guard"),
                    (f"VAULT_ADDR={canary_addr}", "address override in the query")):
        n0 = hits.get("n", 0)
        st, sq, txt = mcp(base1, f"/mcp?{q}", "initialize", INIT,
                          headers={"X-Vault-Token": tok_a})
        read = ""
        if st == "200":
            mcp(base1, f"/mcp?{q}", "notifications/initialized", {}, session=sq,
                headers={"X-Vault-Token": tok_a}, req_id=None)
            _, read = tool_call(base1, f"/mcp?{q}", tool_name, read_args("a/canary"),
                               session=sq, headers={"X-Vault-Token": tok_a})
        hit = hits.get("n", 0) > n0
        honoured = hit or f"{gw_ip}:{CANARY_PORT}" in read
        verdict = ("PASS (400 by the product's own guard)" if st == "400" else
                   "MEASURED: honoured" if honoured else "MEASURED: ignored")
        record(f"[5] ?{q.split('=')[0]} ({note})", verdict,
               f"init HTTP {st}; canary {'hit' if hit else 'no hit'}; "
               f"read: {classify(read or txt)}")

    # ---- [6] target retargeting, direct ------------------------------------
    print("\n[6] caller-supplied Vault address, direct to the product")
    n0 = hits.get("n", 0)
    st, sc, _ = mcp_init(base1, "/mcp",
                         headers={"X-Vault-Token": tok_a, "VAULT_ADDR": canary_addr})
    st, txt = tool_call(base1, "/mcp", tool_name, read_args("a/canary"), session=sc,
                        headers={"X-Vault-Token": tok_a, "VAULT_ADDR": canary_addr})
    dialled = f"{gw_ip}:{CANARY_PORT}" in txt or hits.get("n", 0) > n0
    ctrl("[6] direct to the product: a caller-supplied VAULT_ADDR is honoured "
         "(expected-bad control - this is why the edge must strip it)",
         dialled,
           f"canary hits {n0} -> {hits.get('n', 0)}; HTTP {st} {classify(txt)}")

    # ---- [7] through the gateway, SHIPPED chart config --------------------
    # apps/mcp-gateway-admin is NOT in HEAD (reverted by 0ef0b4dad); the working tree
    # still holds it untracked, and all 8 of its files are byte-identical to 9049af3bf.
    # So this IS the foundation's real rendered config, from that commit, rendered by
    # helm rather than copied - and it is not something current HEAD deploys. Stated
    # because [7a] and [8-pre] are assertions about that chart.
    print("\n[7] through the gateway, as apps/mcp-gateway-admin renders it (chart = "
          "commit 9049af3bf, untracked in the working tree after revert 0ef0b4dad; "
          "rendered by helm, not copied)")
    resource = "https://mcp.internal.grigri.cloud/vault/mcp"
    cfg = render_vault_route(f"{base1}/mcp", f"{bes[1]}:8000", resource, "accessToken")
    log = boot(cfg, "shipped")
    kjwt = mint("mcp-gateway-admin", scope="mcp_vault")
    EDGE_JWT.append(kjwt)          # synthetic, still registered for the audit
    gw = f"http://{BACKEND_HOST}:{GW_PORT}"
    st, gs, txt = mcp_init(gw, "/vault/mcp", token=kjwt, headers={"X-Vault-Token": tok_a})
    st2, txt2 = tool_call(gw, "/vault/mcp", tool_name, read_args("a/canary"), token=kjwt,
                          session=gs, headers={"X-Vault-Token": tok_a})
    blocked = MARKER_A not in txt2 and CANARY_MARKER not in txt2
    # REQUIRED, typed boolean. This is the CURRENT shipped behaviour and it is expected:
    # the chart strips the only credential header the product reads, so the route is not
    # ready for native delegation. It is asserted as a fact about the chart, never as a
    # pass of a Vault route.
    req("[7a] shipped chart config strips X-Vault-Token, so no caller credential reaches "
        "Vault (current expected state, NOT a ready route)", blocked,
        f"init HTTP {st} / call HTTP {st2} {classify(txt2)} - the product's own "
        f"no-credential error is the expected signal here")

    n0 = hits.get("n", 0)
    st, asess, _ = mcp(gw, "/vault/mcp", "initialize", INIT, token=kjwt,
                       headers={"VAULT_ADDR": canary_addr})
    st2, txt2 = tool_call(gw, "/vault/mcp", tool_name, read_args("a/canary"), token=kjwt,
                          session=asess, headers={"VAULT_ADDR": canary_addr,
                                                  "X-Vault-Token": tok_a})
    dialled = f"{gw_ip}:{CANARY_PORT}" in txt2 or hits.get("n", 0) > n0
    req("[7b] shipped route blocks a caller-supplied VAULT_ADDR toward the canary",
        not dialled,
           f"canary hits {n0} -> {hits.get('n', 0)}; HTTP {st2} {classify(txt2)}")

    for q, note in ((f"VAULT_TOKEN={tok_a}", "the product's own credential guard"),
                    (f"VAULT_ADDR={canary_addr}", "address override in the query")):
        n0 = hits.get("n", 0)
        st, _, txt2 = mcp(gw, f"/vault/mcp?{q}", "initialize", INIT, token=kjwt,
                          headers={"X-Vault-Token": tok_a})
        hit = hits.get("n", 0) > n0
        req(f"[7c] query override ?{q.split('=')[0]} does not reach the backend "
            f"({note})", not hit and CANARY_MARKER not in txt2,
            f"canary {n0} -> {hits.get('n', 0)}; HTTP {st}; {classify(txt2)}")

    # ---- [8] stateless + native header (test-only config) -----------------
    print("\n[8] stateless route + native per-request credential (TEST-ONLY config)")
    cfg2 = unstrip_patch(stateless_patch(
        render_vault_route(f"{base1}/mcp", f"{bes[1]}:8000", resource, "accessToken")))
    procs[-1].terminate()
    time.sleep(1)
    log2 = boot(cfg2, "stateless")

    def stateless_read(who, path):
        tok_v = named[who]
        st, s, _ = mcp_init(gw, "/vault/mcp", token=kjwt, headers={"X-Vault-Token": tok_v})
        st2, txt = tool_call(gw, "/vault/mcp", tool_name, read_args(path), token=kjwt,
                             session=s, headers={"X-Vault-Token": tok_v})
        return st, st2, txt

        # The test-only patch must be exactly one entry wide: compared with the SHIPPED
    # render, the only header-strip entry it may drop is the native `x-vault-token`, and
    # it may not add any other header back. Everything else the chart strips - address,
    # token aliases, skip-verify, namespace - has to stay stripped, or the patched route
    # would prove nothing about the production shape.
    _a = [ln.strip() for ln in cfg.splitlines() if ln.strip()]
    _b = [ln.strip() for ln in cfg2.splitlines() if ln.strip()]
    _strip_a = [ln for ln in _a if ln.startswith("- ")]
    _strip_b = [ln for ln in _b if ln.startswith("- ")]
    _dropped = [ln for ln in _strip_a if ln not in _strip_b]
    _forwarded_back = [ln for ln in _strip_b if ln not in _strip_a]
    req("[8-pre] the TEST-ONLY patch forwards ONLY the native X-Vault-Token and keeps "
        "every other shipped header strip in place",
        _dropped == ["- x-vault-token"] and not _forwarded_back
        and len(_strip_b) == len(_strip_a) - 1
        and "statefulMode: stateless" in cfg2
        and all(ln in _strip_b for ln in
                ("- vault_addr", "- vault-addr", "- vault_skip_verify",
                 "- vault-skip-verify", "- vault_namespace", "- vault-namespace",
                 "- x-vault-namespace", "- vault_token")),
        f"shipped strip entries {len(_strip_a)} -> patched {len(_strip_b)}; "
        f"dropped={_dropped}; newly forwarded={_forwarded_back or 'none'}; "
        f"stateless={'statefulMode: stateless' in cfg2}")

    st, st2, txt = stateless_read("a", "a/canary")
    req("[8a] stateless: token A reads its own path through the gateway",
        MARKER_A in txt,
           f"init {st} call {st2} {classify(txt)}")
    st, st2, txt = stateless_read("b", "b/canary")
    req("[8b] stateless: token B reads its own path", MARKER_B in txt,
           f"init {st} call {st2} {classify(txt)}")
    st, st2, txt = stateless_read("a", "b/canary")
    req("[8c] stateless: token A cannot read B's path",
        MARKER_B not in txt,
           f"{st2} {classify(txt)}")
    st, st2, txt = stateless_read("none", "a/canary")
    req("[8d] stateless: a token whose ACL matches nothing is denied",
        MARKER_A not in txt,
           f"{st2} {classify(txt)}")
    st, st2, txt = stateless_read("b", "b/canary")
    req("[8e] stateless: no 'no active session' class failure",
        "no active session" not in txt.lower() and MARKER_B in txt,
           f"{st2} {classify(txt)}")
    st, _, txt = mcp_init(gw, "/vault/mcp", headers={"X-Vault-Token": tok_a})
    req("[8f] stateless route still requires the gateway bearer",
        st in ("401", "403"),
           f"HTTP {st} {classify(txt)}")
    log2_txt = open(log2).read()
    req("[8g] SESSION_KEY active: no unencrypted-session warning in the gateway log",
        "not encrypted" not in log2_txt)

    # [8h] with the credential forwarded, an attempt to retarget the backend tests the
    # address part of the strip list on its own - otherwise the missing credential
    # masks it (which is exactly what happened in [7b]).
    n0 = hits.get("n", 0)
    st, sx, _ = mcp_init(gw, "/vault/mcp", token=kjwt,
                         headers={"X-Vault-Token": tok_a, "VAULT_ADDR": canary_addr})
    st2, txt2 = tool_call(gw, "/vault/mcp", tool_name, read_args("a/canary"), token=kjwt,
                          session=sx, headers={"X-Vault-Token": tok_a,
                                               "VAULT_ADDR": canary_addr})
    dialled = hits.get("n", 0) > n0 or f"{gw_ip}:{CANARY_PORT}" in txt2
    served_from_fixture = MARKER_A in txt2
    req("[8h] shipped strip list blocks a caller VAULT_ADDR even when the credential "
        "IS forwarded (address override must never retarget the backend)",
        not dialled and served_from_fixture,
           f"canary {n0} -> {hits.get('n', 0)}; HTTP {st2} {classify(txt2)}")

    # ---- [9] REQUIRED: stateless per-request semantics, override bounds ----
    print("\n[9] stateless route: per-caller credential bounds (security-required)")
    tok_a2 = vault_token_for(addr_a, root, "proof-rot-b", (f"{pre}b/canary",),
                             ("secret/metadata/b/*",) + WIDE)
    FORBIDDEN.append(tok_a2)

    # 9a: A's session id, but B's OWN credential on the request -> must act as B, never A.
    st, sa_gw, _ = mcp_init(gw, "/vault/mcp", token=kjwt, headers={"X-Vault-Token": tok_a})
    st2, txt = tool_call(gw, "/vault/mcp", tool_name, read_args("b/canary"), token=kjwt,
                         session=sa_gw, headers={"X-Vault-Token": named["b"]})
    req("[9a] stateless: A's session id + B's own credential acts as B, not A",
        MARKER_B in txt and MARKER_A not in txt,
        f"init {st} call {st2} {classify(txt)}")
    st2, txt = tool_call(gw, "/vault/mcp", tool_name, read_args("a/canary"), token=kjwt,
                         session=sa_gw, headers={"X-Vault-Token": named["b"]})
    req("[9b] and B cannot read A's path through that same session",
        MARKER_A not in txt, f"call {st2} {classify(txt)}")

    # 9c: a request on an established session with NO backend credential must fail closed.
    st2, txt = tool_call(gw, "/vault/mcp", tool_name, read_args("a/canary"),
                         token=kjwt, session=sa_gw)
    req("[9c] stateless: a missing backend credential is rejected, never inherited",
        MARKER_A not in txt and MARKER_B not in txt,
        f"call {st2} {classify(txt)}")

    # 9d: rotation is honoured on the NEXT call, because there is no cached client.
    st2, txt = tool_call(gw, "/vault/mcp", tool_name, read_args("b/canary"), token=kjwt,
                         session=sa_gw, headers={"X-Vault-Token": tok_a2})
    req("[9d] stateless: a rotated native credential is honoured on the next tool call",
        MARKER_B in txt, f"call {st2} {classify(txt)}")

    # 9e: the edge still requires the gateway bearer on this test-only route.
    st_anon, _, _ = mcp(gw, "/vault/mcp", "initialize", INIT,
                        headers={"X-Vault-Token": tok_a})
    req("[9e] stateless route without an edge JWT -> 401 (backend credential is not "
        "an edge credential)", st_anon == "401", f"HTTP {st_anon}")

    # 9f: address / skip-verify / namespace aliases stay stripped, in any spelling.
    # A namespace or address switch is detectable: the read would stop returning A's
    # marker (or would dial the canary). Baseline behaviour must be unchanged.
    n0 = hits.get("n", 0)
    variants = [("VAULT_ADDR", canary_addr), ("vault_addr", canary_addr),
                ("Vault_Addr", canary_addr), ("vAULT-aDDR", canary_addr),
                ("VAULT-ADDR", canary_addr), ("VAULT_SKIP_VERIFY", "true"),
                ("vault_skip_verify", "true"), ("Vault-Skip-Verify", "true"),
                ("X-Vault-Namespace", "sub/attacker"), ("VAULT_NAMESPACE", "sub/attacker"),
                ("vault-namespace", "sub/attacker")]
    bad = []
    for k, v in variants:
        st2, txt = tool_call(gw, "/vault/mcp", tool_name, read_args("a/canary"),
                             token=kjwt, session=sa_gw,
                             headers={"X-Vault-Token": tok_a, k: v})
        if MARKER_A not in txt or CANARY_MARKER in txt:
            bad.append(f"{k}={v} -> {st2} {classify(txt)}")
    req(f"[9f] {len(variants)} header spellings (address / skip-verify / namespace "
        f"aliases, mixed case) change nothing at the backend", not bad,
        "; ".join(bad) or f"every variant: read still served from the fixed target and "
        f"namespace; canary {n0} -> {hits.get('n', 0)}")

    # 9g: query-string overrides, both spellings, on the stateless route.
    n0 = hits.get("n", 0)
    qbad, qnotes = [], []
    for q in (f"VAULT_ADDR={canary_addr}", f"vault_addr={canary_addr}",
              "VAULT_SKIP_VERIFY=true", "vault_namespace=sub/attacker"):
        st2, txt = tool_call(gw, f"/vault/mcp?{q}", tool_name, read_args("a/canary"),
                             token=kjwt, session=sa_gw, headers={"X-Vault-Token": tok_a})
        if CANARY_MARKER in txt:
            qbad.append(f"?{q.split('=')[0]} -> {st2} {classify(txt)}")
        elif MARKER_A not in txt:
            qnotes.append(f"?{q.split('=')[0]} -> HTTP {st2} {classify(txt)} "
                          f"(no override effect, and the query itself was not routed)")
    req("[9g] no query override reaches the backend (address / skip-verify / namespace)",
        not qbad, "; ".join(qbad) or
        f"canary {n0} -> {hits.get('n', 0)} (never dialled); " + "; ".join(qnotes))

    # 9h: TLS bounds. Negative proof: pointed at a self-signed HTTPS Vault, the product
    # must fail the handshake rather than read through it - so a caller who *did* manage
    # to retarget could not silently ignore certificate errors either.
    tls_addr = f"https://{gw_ip}:{TLS_PORT}"
    st, stt, txt = (None, None, "")
    try:
        st, sx, _ = mcp_init(base1, "/mcp",
                             headers={"X-Vault-Token": tok_a, "VAULT_ADDR": tls_addr})
        stt, txt = tool_call(base1, "/mcp", tool_name, read_args("a/canary"), session=sx,
                             headers={"X-Vault-Token": tok_a, "VAULT_ADDR": tls_addr})
    except Exception:                                           # noqa: BLE001
        txt = "tls handshake failed"   # never echo the exception text into the log
    obs("[9h] self-signed canary attempt returned no readable secret "
        f"(HTTP {stt}, {classify(txt) or 'no signal'}) - NOT a TLS proof: absence of a "
        "marker is not certificate verification. See [10a]/[10b].")
    # ---- [10] TLS, measured against a real HTTPS Vault endpoint -------------
    # A disposable local HTTPS Vault-API fixture with its own CA, plus the official
    # product pointed at it twice: once with the CA mounted (VAULT_CACERT) and once
    # without. Same path, same request shape, same auth header - only trust differs.
    # A local CA inside a throwaway container is a fixture, not a production change.
    print("\n[10] TLS: valid-CA positive and untrusted-cert negative on the same endpoint")
    ca_p, srv_p, srv_k = (os.path.join(WORK, "tls-ca.pem"),
                          os.path.join(WORK, "tls-srv.pem"),
                          os.path.join(WORK, "tls-srv.key"))
    ext_p = os.path.join(WORK, "tls-srv.ext")
    io.open(ext_p, "w").write(f"subjectAltName=IP:{gw_ip},IP:127.0.0.1\n")
    for argv in (
        ["openssl", "req", "-x509", "-newkey", "ec", "-pkeyopt",
         "ec_paramgen_curve:prime256v1", "-nodes", "-keyout", ca_p + ".key",
         "-out", ca_p, "-days", "1", "-subj", "/CN=vault-probe-ca"],
        ["openssl", "req", "-new", "-newkey", "ec", "-pkeyopt",
         "ec_paramgen_curve:prime256v1", "-nodes", "-keyout", srv_k,
         "-out", os.path.join(WORK, "tls-srv.csr"), "-subj", "/CN=vault-probe"],
        ["openssl", "x509", "-req", "-in", os.path.join(WORK, "tls-srv.csr"),
         "-CA", ca_p, "-CAkey", ca_p + ".key", "-out", srv_p, "-days", "1",
         "-extfile", ext_p],
    ):
        r = sh(argv)
        if r.returncode != 0:
            break
    tls_ready = os.path.exists(srv_p) and os.path.exists(ca_p)
    if not tls_ready:
        results.append(("NOTTESTED", "[10] TLS positive/negative pair", None,
                        "NOT TESTED: could not create the local CA/leaf pair"))
        print("  NOT TESTED            [10] TLS fixture certificates unavailable")
    else:
        read_path = "a/tls"      # fixture-local path: matched, and shown in diagnostics

        class VaultTlsHandler(http.server.BaseHTTPRequestHandler):
            """Minimal KV-v2-shaped Vault API over TLS, for the trust question only."""

            def log_message(self, *a):
                pass

            def _send(self, obj, code=200):
                body = json.dumps(obj).encode()
                self.send_response(code)
                self.send_header("Content-Type", "application/json")
                self.send_header("Content-Length", str(len(body)))
                self.end_headers()
                self.wfile.write(body)

            def do_GET(self):
                p = self.path.split("?")[0]
                TLS_ENDPOINT["n"] += 1
                TLS_ENDPOINT["paths"].append(p)
                if not self.headers.get("X-Vault-Token"):
                    # count it, and answer the way Vault does for an unauthenticated read
                    TLS_ENDPOINT["no_token"] += 1
                    return self._send({"errors": ["missing client token"]}, 400)
                if p.endswith("/sys/mounts") or "/internal/ui/mounts" in p:
                    # keyed "secret/" exactly as Vault does, so the product's mount
                    # lookup finds the KV-v2 mount and issues the data read
                    return self._send({"data": {
                        "secret/": {"type": "kv", "accessor": "probe-mount",
                                    "options": {"version": "2"},
                                    "config": {"secret": "kv", "version": "2"}}}})
                if "/data/" in p or p.rstrip("/").endswith(read_path):
                    # KV-v2 envelope, the shape the real fixture serves for this path
                    return self._send({"data": {"data": {"value": TLS_MARKER},
                                                "metadata": {"version": 1}}})
                if "/secret/" in p or "/metadata/" in p:
                    # KV-v1 style envelope, for either spelling of the read path
                    return self._send({"data": {"value": TLS_MARKER}})
                return self._send({"data": {}})

            def do_POST(self):
                self.do_GET()

        class CountingServer(http.server.ThreadingHTTPServer):
            """Counts connection attempts at accept time, BEFORE the TLS handshake, so an
            attempt that dies in the handshake (the untrusted-certificate case) is still
            evidence that the endpoint was reached rather than unreachable."""
            def get_request(self):
                TLS_ENDPOINT["conns"] += 1
                return super().get_request()

        tls_ctx = ssl.SSLContext(ssl.PROTOCOL_TLS_SERVER)
        tls_ctx.load_cert_chain(certfile=srv_p, keyfile=srv_k)
        httpsd = CountingServer(("0.0.0.0", TLSVAULT_PORT), VaultTlsHandler)
        httpsd.socket = tls_ctx.wrap_socket(httpsd.socket, server_side=True)
        threading.Thread(target=httpsd.serve_forever, daemon=True).start()
        tls_addr = f"https://{gw_ip}:{TLSVAULT_PORT}"

        ok_ip = start(PREFIX + "be-tls-ok", MCP_IMAGE,
                      ["/bin/vault-mcp-server", "streamable-http", "--transport-host",
                       "0.0.0.0", "--transport-port", "8000", "--mcp-endpoint", "/mcp"],
                      env={"VAULT_ADDR": tls_addr, "VAULT_CACERT": "/etc/probe/ca.pem",
                           "SSL_CERT_FILE": "/etc/probe/ca.pem"},
                      volumes=[(ca_p, "/etc/probe/ca.pem")])
        bad_ip = start(PREFIX + "be-tls-bad", MCP_IMAGE,
                       ["/bin/vault-mcp-server", "streamable-http", "--transport-host",
                        "0.0.0.0", "--transport-port", "8000", "--mcp-endpoint", "/mcp"],
                       env={"VAULT_ADDR": tls_addr})
        wait_tcp(ok_ip, 8000, note="tls-ok backend")
        wait_tcp(bad_ip, 8000, note="tls-bad backend")
        ok_base, bad_base = f"http://{ok_ip}:8000", f"http://{bad_ip}:8000"

        def tls_read(base):
            st, sx, _ = mcp_init(base, "/mcp", headers={"X-Vault-Token": tok_a})
            st2, txt = tool_call(base, "/mcp", tool_name, read_args(read_path),
                                 session=sx, headers={"X-Vault-Token": tok_a})
            return st, st2, txt

        n_before = TLS_ENDPOINT["n"]
        st, st2, txt_ok = tls_read(ok_base)
        req("[10a] TLS POSITIVE: with the fixture CA trusted (VAULT_CACERT), the same "
            "path/request/auth is served over HTTPS",
            TLS_MARKER in txt_ok,
            f"init {st} call {st2}, CA mounted read-only and trusted via VAULT_CACERT "
            f"+ SSL_CERT_FILE; paths seen {sorted(set(TLS_ENDPOINT['paths']))[:4]} "
            f"(fixture paths, not secrets); request count "
            f"{n_before} -> {TLS_ENDPOINT['n']}")

        n_before = TLS_ENDPOINT["n"]
        st, st2, txt_bad = tls_read(bad_base)
        sig = classify(txt_bad)
        req("[10b] TLS NEGATIVE: without that CA the SAME endpoint fails with an x509 "
            "trust error (exact phrase asserted), not a silent success",
            "tls:x509-verify-failed" in sig and TLS_MARKER not in txt_bad,
            f"init {st} call {st2} signature={sig} (asserted on the exact x509/"
            f"certificate phrase, never on absence alone); endpoint requests "
            f"{n_before} -> {TLS_ENDPOINT['n']}")

        st, st2, txt_skip = (None, None, "")
        try:
            st, sx2, _ = mcp_init(bad_base, "/mcp",
                                  headers={"X-Vault-Token": tok_a,
                                           "VAULT_SKIP_VERIFY": "true"})
            st2, txt_skip = tool_call(bad_base, "/mcp", tool_name, read_args(read_path),
                                      session=sx2,
                                      headers={"X-Vault-Token": tok_a,
                                               "VAULT_SKIP_VERIFY": "true"})
        except Exception:                                    # noqa: BLE001
            txt_skip = ""
        caller_can_disable = TLS_MARKER in txt_skip
        # Measured, not claimed either way: whether the product lets a request-level
        # VAULT_SKIP_VERIFY turn verification off. The requirement that a caller cannot
        # send that header through the gateway is asserted at the edge by [9f]/[7c]
        # (REQUIRED); this line only records what the backend itself does.
        obs("[10c] direct to the product, a caller-supplied VAULT_SKIP_VERIFY header",
            f"init {st} call {st2} {classify(txt_skip)} -> "
            + ("header HONOURED: a caller can disable certificate verification, which is "
               "why the edge strip list must keep it"
               if caller_can_disable else
               "header had no effect on this endpoint: verification held, so this is "
               "not evidence that the strip list is load-bearing"))

        req("[10d] endpoint connection evidence: the positive call really served "
            "requests over the TLS fixture, and the negative attempt reached the "
            "listener without being served",
            TLS_ENDPOINT["n"] >= 1 and TLS_ENDPOINT["conns"] >= 2
            and TLS_MARKER in txt_ok and TLS_MARKER not in txt_bad,
            f"endpoint served {TLS_ENDPOINT['n']} HTTP request(s) over TLS on "
            f"{sorted(set(TLS_ENDPOINT['paths']))[:6]} "
            f"({TLS_ENDPOINT['no_token']} without a token header); "
            f"{TLS_ENDPOINT['conns']} connection attempt(s) accepted at the listener, "
            f"so the untrusted-CA backend did reach it and only the certificate "
            f"verification stood between it and the secret")

    # The earlier [9h] claimed "TLS verification on" from an ABSENCE of the marker.
    # That was not a TLS proof; [10a]/[10b] replace it with a real pair. Kept here only
    # as a pointer so the log cannot be read as evidence of certificate verification.
    obs("[9h-superseded] the old absence-only check is not evidence of certificate "
        "verification; [10a]/[10b] replaced it",
        "see [10a] valid-CA positive and [10b] untrusted-cert negative")

    # Two things this file cannot prove locally; recorded as kinds so the summary states
    # them instead of a reviewer having to infer them from the RESULT line.
    results.append(("NOTTESTED", "live two-person A/B at the real cluster route", None,
                    "NOT TESTED: needs the owner to run it against the live gateway and "
                    "real Kanidm tokens. The proofs above are local containers with a "
                    "synthetic stub issuer only. No route is claimed ready."))
    results.append(("NOTTESTED", "production per-route config that forwards "
                    "X-Vault-Token", None,
                    "NOT TESTED: the shipped chart strips that header, so the stateless "
                    "delegation shape here exists only as a TEST-ONLY patch "
                    "([8-pre]); nothing in apps/ implements it and none was committed."))

    # ---- summary -----------------------------------------------------------
    print("\n=== Summary (by kind; a measurement is never counted as a pass) ===")
    # The audit first, so its REQUIRED row is inside the tallies below.
    mine = report_audit()
    for kind in ("REQUIRED", "CONTROL", "OBSERVED", "NOTTESTED"):
        rows = [r for r in results if r[0] == kind]
        if kind == "OBSERVED":
            print(f"  {len(rows)} observed measurements  "
                  f"{[r[1].split(']')[0] + ']' for r in rows]}")
            continue
        if kind == "NOTTESTED":
            for _, name, _, detail in rows:
                print(f"  NOT TESTED: {name}\n            {detail}")
            continue
        ok = [r for r in rows if r[2]]
        for _, name, passed, detail in rows:
            if not passed:
                print(f"  {'VIOLATION' if kind == 'REQUIRED' else 'CONTROL-BROKE'}: {name}"
                      f"\n            {detail}")
        print(f"  {kind}: {len(ok)} / {len(rows)} held")
    fails = [r for r in results if r[0] == "REQUIRED" and not r[2]]
    broke = [r for r in results if r[0] == "CONTROL" and not r[2]]
    if fails or broke or mine:
        print("\nRESULT: NOT acceptable for review - see the lines above.")
        sys.exit(1)
    print("\nRESULT: every REQUIRED assertion held, every expected-bad control "
          "reproduced, output audited clean. The two TEST-ONLY deviations stay "
          "uncommitted; the live two-user test is still open.")


def audit_output():
    """Scan every output surface, in memory, before cleanup. Returns (mine, surfaces).

    `mine` is THIS PROBE'S OWN printed output (stdout and stderr, both tee'd) - the only
    surface the "no values printed" claim is about, because that is what ends up in a
    shared console log. `surfaces` is everything else this probe can read but does not
    control: the gateway's own log file and the logs of the disposable containers it
    created (no other container is ever touched).

    Findings are reported as fixture LABEL plus a count, never a value: the labels are
    this file's own constant names. Runs on the success path, the SystemExit path and the
    crash path, because a leaking failure log is still a leak.
    """
    named = [("marker-A", MARKER_A), ("marker-B", MARKER_B),
             ("canary-marker", CANARY_MARKER), ("tls-marker", TLS_MARKER)]
    named += [("vault-credential", v) for v in FORBIDDEN
              if v not in (MARKER_A, MARKER_B, CANARY_MARKER, TLS_MARKER)]
    named += [("edge-jwt", v) for v in EDGE_JWT if v]
    named += [("session-id", v) for v in SESSION_IDS if v]

    def scan(text):
        out = []
        for label, value in named:
            c = text.count(value) if value else 0
            if c:
                out.append((label, c))
        return out

    mine = scan("".join(OUT))
    surfaces = []
    for fn in sorted(os.listdir(WORK)):
        if not (fn.startswith("log-") or fn.startswith("cfg-")):
            continue
        path = os.path.join(WORK, fn)
        try:
            content = io.open(path, encoding="utf-8", errors="replace").read()
        except OSError:
            continue
        f = scan(content)
        if f:
            surfaces.append((path, f))
    for name in containers:
        r = sh(["docker", "logs", name], timeout=60)
        f = scan((r.stdout or "") + (r.stderr or ""))
        if f:
            surfaces.append((f"container:{name}", f))
    return mine, surfaces


_AUDITED = []


def report_audit():
    """One place that turns the audit into statements, on every exit path (run once)."""
    if _AUDITED:
        return _AUDITED[0]
    _AUDITED.append([])          # claim it first, so a failure inside cannot recurse
    mine, surfaces = audit_output()
    req("no fixture secret value appears in this probe's own stdout+stderr", not mine,
        "; ".join(f"{l} x{c}" for l, c in mine)
        or "every line this program printed (stdout and stderr, tee'd in memory) was "
           "scanned against the registered fixture values, session ids and edge JWTs")
    if surfaces:
        obs("fixture values do appear in surfaces this probe does not print: the "
            "gateway's own log file and the disposable containers' own logs",
            "; ".join(f"{src}: " + ",".join(f"{l}x{c}" for l, c in f)
                      for src, f in surfaces)
            + " - all values are synthetic fixtures, the files are 0600 in WORK and the "
              "containers are removed by cleanup. A backend or gateway that writes "
              "credentials or tool payloads into its OWN log is an operational finding "
              "for the owner, not a pass or a failure of this probe")
    _AUDITED[0] = mine
    return mine


if __name__ == "__main__":
    try:
        main()
    except SystemExit as exc:
        leaked = report_audit()
        if leaked:
            print("[redaction audit] this probe's own output leaked fixture values")
            sys.exit(1)
        raise exc
    except BaseException as exc:                                # noqa: BLE001
        # A crash still produces output. Put the traceback in a 0600 file of our own,
        # scan it, and print only the exception type plus counts - never the raw text.
        import traceback
        tb = "".join(traceback.format_exception(exc))
        path = os.path.join(WORK, "crash.txt")
        fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
        with os.fdopen(fd, "w") as fh:
            fh.write(tb)
        named = ([MARKER_A, MARKER_B, CANARY_MARKER, TLS_MARKER] + list(FORBIDDEN)
                 + list(SESSION_IDS) + list(EDGE_JWT))
        in_tb = sum(tb.count(v) for v in named if v)
        print(f"[crash] {type(exc).__name__}: full traceback written to {path} "
              f"(0600); fixture-value occurrences in it: {in_tb}")
        try:
            report_audit()
        except Exception:                                       # noqa: BLE001
            print("[redaction audit] could not complete on the crash path")
        raise
