#!/usr/bin/env python3
"""Shared fixture for the MCP gateway access-gate runtime checks.

Everything here is local and synthetic:
  * a throwaway ES256 issuer with keys generated at runtime (never committed, never printed);
  * loopback streamable-HTTP MCP capture backends that record exactly what the gateway
    forwarded, reporting credentials as sha256[:12] or presence booleans only;
  * the pinned agentgateway binary, started on a private loopback port with an isolated HOME.

Ports are never fixed: the fixture servers bind port 0 (kernel-assigned) and the gateway takes a
free port that is verified to be unoccupied, so several harnesses can share one machine and a
stale process cannot fake a result.

If the pinned artifact is not available the suites SKIP (exit 3) - they never report a pass
against a gateway that was not actually run.
"""

import hashlib
import json
import os
import re
import shutil
import socket
import subprocess
import sys
import tempfile
import threading
import time
import urllib.error
import urllib.request
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

import jwt as pyjwt
from cryptography.hazmat.primitives.asymmetric import ec

# The gateway version every assertion in these suites was measured against.
PINNED_VERSION = "1.7.0"
AUDIENCE = "mcp-gateway"

# Official release artifact, for the fetch instructions printed when it is missing.
ARTIFACT_URL = ("https://github.com/agentgateway/agentgateway/releases/download/"
                "v1.7.0/agentgateway-linux-amd64")
ARTIFACT_SHA256 = "1b2fc01a2de29ca113bbb6258b5cae7b1eb3c724071c09d687a449132f2f42f8"

# Extra places to look before giving up; AGENTGATEWAY_BIN (or PATH) takes precedence.
CACHE_PATHS = (
    os.path.expanduser("~/.cache/agentgateway/agentgateway"),
    os.path.expanduser("~/.local/bin/agentgateway"),
    "./agentgateway",
)

FETCH_HELP = (
    f"agentgateway v{PINNED_VERSION} binary not found.\n"
    f"  Fetch the official artifact and point the suite at it:\n"
    f"    curl -fsSL -o agentgateway {ARTIFACT_URL}\n"
    f"    echo '{ARTIFACT_SHA256}  agentgateway' | sha256sum -c -   # linux-amd64\n"
    f"    chmod +x agentgateway\n"
    f"    AGENTGATEWAY_BIN=$PWD/agentgateway python3 test/mcp-gateway/<suite>.py\n"
    f"  Or, if the pinned image is what you have:\n"
    f"    docker run --rm cr.agentgateway.dev/agentgateway:v{PINNED_VERSION} --version"
)


class Skip(Exception):
    """A prerequisite is missing: report SKIPPED, never a pass."""


class HarnessError(Exception):
    """The fixture itself could not be brought up; the run produced no result."""


def sha12(value):
    return hashlib.sha256(value.encode()).hexdigest()[:12] if value else None


def binary_fingerprint(path):
    """sha256[:12] of the binary actually executed, so results name their subject."""
    digest = hashlib.sha256()
    with open(path, "rb") as handle:
        for block in iter(lambda: handle.read(1 << 20), b""):
            digest.update(block)
    return digest.hexdigest()[:12]


def resolve_binary():
    """Return (path, version dict) for the pinned binary, or raise Skip."""
    candidates = [os.environ.get("AGENTGATEWAY_BIN"), shutil.which("agentgateway")]
    candidates += [p for p in CACHE_PATHS if os.path.isfile(p)]
    for path in [c for c in candidates if c]:
        if not os.access(path, os.X_OK):
            continue
        try:
            out = subprocess.run([path, "--version"], capture_output=True, text=True,
                                 timeout=30, check=False)
            version = json.loads(out.stdout)
        except (OSError, ValueError, subprocess.SubprocessError):
            raise Skip(f"{path} is not a runnable agentgateway binary\n{FETCH_HELP}")
        if version.get("version") != PINNED_VERSION:
            raise Skip(f"{path} reports version {version.get('version')!r}, suite is measured "
                       f"against {PINNED_VERSION!r}\n{FETCH_HELP}")
        return path, version
    raise Skip(FETCH_HELP)


def free_port(attempts=50):
    """A loopback port nothing is listening on (another harness may share this box)."""
    for _ in range(attempts):
        with socket.socket() as reserve:
            reserve.bind(("127.0.0.1", 0))
            port = reserve.getsockname()[1]
        with socket.socket() as probe:
            probe.settimeout(0.4)
            if probe.connect_ex(("127.0.0.1", port)) != 0:
                return port
    raise HarnessError("no free loopback port")


def render(template, **subs):
    """Fill @@NAME@@ markers, so YAML flow braces need no escaping."""
    text = template
    for key, value in subs.items():
        text = text.replace(f"@@{key}@@", str(value))
    if "@@" in text:
        raise HarnessError(f"unfilled marker in config: {text[text.index('@@'):][:60]}")
    return text


class _Absent:
    pass


ABSENT = _Absent()


class SyntheticIdp:
    """Self-signed ES256 stub issuer: JWKS plus OIDC/AS metadata on loopback."""

    def __init__(self):
        self.key = ec.generate_private_key(ec.SECP256R1())
        self.other_key = ec.generate_private_key(ec.SECP256R1())
        self.kid = "gate-key"
        self.server = ThreadingHTTPServer(("127.0.0.1", 0), self._handler())
        self.port = self.server.server_address[1]
        self.origin = f"http://127.0.0.1:{self.port}"
        threading.Thread(target=self.server.serve_forever, daemon=True).start()

    def _jwk(self, key, kid):
        import base64

        numbers = key.public_key().public_numbers()

        def b64(value, length=32):
            return base64.urlsafe_b64encode(value.to_bytes(length, "big")).rstrip(b"=").decode()

        return {"kty": "EC", "crv": "P-256", "x": b64(numbers.x), "y": b64(numbers.y),
                "alg": "ES256", "use": "sig", "kid": kid}

    def _handler(self):
        idp = self

        class Handler(BaseHTTPRequestHandler):
            def log_message(self, *args):
                pass

            def do_GET(self):
                if self.path == "/jwks":
                    body = json.dumps({"keys": [idp._jwk(idp.key, idp.kid)]}).encode()
                elif self.path.startswith("/.well-known/openid-configuration"):
                    body = json.dumps({"issuer": idp.origin,
                                       "jwks_uri": f"{idp.origin}/jwks"}).encode()
                elif self.path.startswith("/.well-known/oauth-authorization-server"):
                    body = json.dumps({"issuer": idp.origin, "jwks_uri": f"{idp.origin}/jwks",
                                       "token_endpoint": f"{idp.origin}/token"}).encode()
                else:
                    self.send_response(404)
                    self.end_headers()
                    return
                self.send_response(200)
                self.send_header("Content-Type", "application/json")
                self.send_header("Content-Length", str(len(body)))
                self.end_headers()
                self.wfile.write(body)

        return Handler

    def claims(self, **overrides):
        now = int(time.time())
        base = {"iss": self.origin, "sub": "acct-aaaa", "aud": AUDIENCE, "client_id": AUDIENCE,
                "scope": "openid profile mcp_grafana", "session_id": "sess-aaaa",
                "iat": now, "nbf": now, "exp": now + 3600}
        base.update(overrides)
        return {k: v for k, v in base.items() if v is not ABSENT}

    def mint(self, **overrides):
        """Sign a synthetic token. `key`/`kid` override which signer is used."""
        key = overrides.pop("signing_key", self.key)
        kid = overrides.pop("signing_kid", self.kid)
        return pyjwt.encode(self.claims(**overrides), key, algorithm="ES256",
                            headers={"kid": kid})

    def stop(self):
        self.server.shutdown()
        self.server.server_close()


class CaptureBackend:
    """Minimal stateless streamable-HTTP MCP server that records what it received."""

    def __init__(self, name):
        self.name = name
        self.records = []
        self.server = ThreadingHTTPServer(("127.0.0.1", 0), self._handler())
        self.port = self.server.server_address[1]
        threading.Thread(target=self.server.serve_forever, daemon=True).start()

    def _handler(self):
        capture = self

        class Handler(BaseHTTPRequestHandler):
            protocol_version = "HTTP/1.1"

            def log_message(self, *args):
                pass

            def reply(self, code, obj):
                body = json.dumps(obj).encode()
                self.send_response(code)
                self.send_header("Content-Type", "application/json")
                self.send_header("Content-Length", str(len(body)))
                self.end_headers()
                self.wfile.write(body)

            def do_POST(self):
                length = int(self.headers.get("Content-Length", 0))
                raw = self.rfile.read(length)
                headers = {k.lower(): v for k, v in self.headers.items()}
                auth = headers.get("authorization")
                record = {
                    "path": self.path,
                    "method": None,
                    "tool": None,
                    "auth_present": auth is not None,
                    "auth_sha12": sha12(auth),
                    # the gateway forwards "Bearer <token>"; compare against the token itself
                    "bearer_sha12": sha12(auth.split(" ", 1)[1]) if auth and " " in auth else None,
                    "bearer_prefix_ok": bool(auth) and auth.startswith("Bearer "),
                    "cookie_present": "cookie" in headers,
                    "x_grafana_url_present": "x-grafana-url" in headers,
                    "x_grafana_sa_token_present": "x-grafana-service-account-token" in headers,
                    "x_session_present": ("mcp-session-id" in headers or "x-session-id" in headers),
                    "header_names": sorted(headers),
                }
                try:
                    msg = json.loads(raw or b"{}")
                    record["method"] = msg.get("method")
                    if record["method"] == "tools/call":
                        record["tool"] = msg.get("params", {}).get("name")
                        record["tool_arg"] = (msg.get("params", {}).get("arguments", {})
                                              .get("marker"))
                except (ValueError, AttributeError):
                    pass
                capture.records.append(record)
                if len(capture.records) > 200:
                    del capture.records[:-50]
                self.respond(msg if isinstance(msg, dict) else {}, record)

            def respond(self, msg, record):
                idn = msg.get("id", 1)
                method = record["method"]
                if method == "initialize":
                    self.reply(200, {"jsonrpc": "2.0", "id": idn, "result": {
                        "protocolVersion": "2025-06-18", "capabilities": {"tools": {}},
                        "serverInfo": {"name": f"capture-{capture.name}", "version": "0"}}})
                elif method == "tools/list":
                    self.reply(200, {"jsonrpc": "2.0", "id": idn, "result": {"tools": [
                        {"name": "echo", "inputSchema": {"type": "object"}}]}})
                elif method == "tools/call":
                    self.reply(200, {"jsonrpc": "2.0", "id": idn, "result": {
                        "content": [{"type": "text", "text": "ok"}]}})
                elif method == "notifications/initialized":
                    self.send_response(202)
                    self.send_header("Content-Length", "0")
                    self.end_headers()
                else:
                    self.reply(200, {"jsonrpc": "2.0", "id": idn, "result": {}})

            def do_GET(self):
                self.reply(405, {"error": "no sse"})

        return Handler

    def last(self):
        return self.records[-1] if self.records else {}

    def clear(self):
        self.records.clear()

    def stop(self):
        self.server.shutdown()
        self.server.server_close()


class Gateway:
    """agentgateway on a private loopback port, with its own HOME and config directory.

    Note: the gateway rewrites the local config file it is started with (it appends its
    auto-generated session key), so the path handed to it must always be disposable.
    """

    def __init__(self, binary, build_config, label="gateway"):
        self.work = tempfile.mkdtemp(prefix=f"access-gate-{label}-")
        self.config_path = os.path.join(self.work, "config.yaml")
        self.log_path = os.path.join(self.work, "gateway.log")
        self.binary = binary
        self.process = None
        self._log = None
        self.port = free_port()
        with open(self.config_path, "w") as handle:
            handle.write(build_config(self.port))
        for attempt in range(3):
            self._validate()
            self._start(binary)
            if self._wait_port(25):
                return
            self.stop()
            self.port = free_port()
            with open(self.config_path, "w") as handle:
                handle.write(build_config(self.port))
            time.sleep(0.3 * (attempt + 1))
        tail = self.tail()
        self.cleanup()
        raise HarnessError(f"gateway failed to start on a free port; log tail:\n{tail}")

    def _validate(self):
        """Fail with the config error rather than a start timeout if the policy is malformed."""
        out = subprocess.run([self.binary, "-f", self.config_path, "--validate-only"],
                             capture_output=True, text=True, timeout=60, check=False)
        if out.returncode != 0:
            message = (out.stdout + out.stderr).strip()
            self.cleanup()
            raise HarnessError(f"config rejected by the pinned binary: {message[:800]}")

    def _start(self, binary):
        env = dict(os.environ, HOME=os.path.join(self.work, "home"),
                   XDG_CONFIG_HOME=os.path.join(self.work, "home", ".config"))
        os.makedirs(env["HOME"], exist_ok=True)
        # Not a `with`: the child inherits this fd and keeps writing after _start returns;
        # stop() closes it.
        self._log = open(self.log_path, "w")  # noqa: SIM115
        self.process = subprocess.Popen([binary, "-f", self.config_path], stdout=self._log,
                                        stderr=self._log, env=env)

    def _wait_port(self, timeout):
        deadline = time.time() + timeout
        while time.time() < deadline:
            if self.process.poll() is not None:
                return False
            try:
                with socket.create_connection(("127.0.0.1", self.port), 0.5):
                    return True
            except OSError:
                time.sleep(0.2)
        return False

    def tail(self, limit=2500):
        try:
            with open(self.log_path, errors="replace") as handle:
                return handle.read()[-limit:]
        except OSError:
            return "(no log)"

    def stop(self):
        if self.process and self.process.poll() is None:
            self.process.terminate()
            for _ in range(20):
                if self.process.poll() is not None:
                    break
                time.sleep(0.1)
            self.process.kill()
            self.process.wait(timeout=10)
        if self._log:
            self._log.close()
            self._log = None

    def cleanup(self):
        self.stop()
        shutil.rmtree(self.work, ignore_errors=True)

    def __enter__(self):
        return self

    def __exit__(self, *exc_info):
        if os.environ.get("KEEP"):
            print(f"kept: {self.work}")
        else:
            self.cleanup()
        return False


def mcp_post(port, path, token=None, method="initialize", extra_headers=None, params=None,
             host=None):
    """One MCP JSON-RPC POST. Returns (status, headers, body-prefix)."""
    body = {"jsonrpc": "2.0", "id": 1, "method": method}
    if method == "initialize":
        body["params"] = params if params is not None else {
            "protocolVersion": "2025-06-18", "clientInfo": {"name": "gate-checks", "version": "0"},
            "capabilities": {}}
    elif params is not None:
        body["params"] = params
    if method == "tools/call":
        body["params"] = params or {"name": "echo", "arguments": {"marker": "m"}}
    headers = {"Content-Type": "application/json",
               "Accept": "application/json, text/event-stream",
               "MCP-Protocol-Version": "2025-06-18"}
    if host:
        headers["Host"] = host
    if token:
        headers["Authorization"] = f"Bearer {token}"
    for key, value in (extra_headers or {}).items():
        headers[key] = value
    request = urllib.request.Request(f"http://127.0.0.1:{port}{path}",
                                     data=json.dumps(body).encode(), method="POST", headers=headers)
    try:
        with urllib.request.urlopen(request, timeout=15) as response:
            return response.status, dict(response.headers), response.read()[:400]
    except urllib.error.HTTPError as error:
        return error.code, dict(error.headers), error.read()[:400]
    except (OSError, urllib.error.URLError) as error:
        return f"ERR:{type(error).__name__}", {}, str(error).encode()


def http_get(port, path, token=None, host=None, accept="application/json"):
    headers = {"Accept": accept}
    if host:
        headers["Host"] = host
    if token:
        headers["Authorization"] = f"Bearer {token}"
    request = urllib.request.Request(f"http://127.0.0.1:{port}{path}", headers=headers)
    try:
        with urllib.request.urlopen(request, timeout=15) as response:
            return response.status, dict(response.headers), response.read()[:400]
    except urllib.error.HTTPError as error:
        return error.code, dict(error.headers), error.read()[:400]
    except (OSError, urllib.error.URLError) as error:
        return f"ERR:{type(error).__name__}", {}, str(error).encode()


def duplicate_auth_request(path, token, spoof, order="valid_first"):
    """Raw HTTP with two Authorization lines - urllib silently collapses duplicates."""
    payload = json.dumps({"jsonrpc": "2.0", "id": 1, "method": "initialize", "params": {
        "protocolVersion": "2025-06-18", "clientInfo": {"name": "gate-checks", "version": "0"},
        "capabilities": {}}}).encode()
    valid = f"Authorization: Bearer {token}\r\n"
    forged = f"Authorization: {spoof}\r\n"
    pair = valid + forged if order == "valid_first" else forged + valid
    head = (f"POST {path} HTTP/1.1\r\nHost: 127.0.0.1\r\nConnection: close\r\n"
            f"Content-Type: application/json\r\nAccept: application/json, text/event-stream\r\n"
            f"MCP-Protocol-Version: 2025-06-18\r\n{pair}"
            f"Content-Length: {len(payload)}\r\n\r\n")
    return head.encode() + payload


def send_raw(port, request_bytes):
    """Send pre-built bytes, return (status, lower-cased header block).

    A rejected request can be closed abruptly (the gateway sends 401 and resets), which discards
    bytes still in flight, so the status is parsed from whatever arrived rather than requiring a
    complete header block. What a check needs is the status code plus the capture side effect.
    """
    with socket.create_connection(("127.0.0.1", port), 10) as conn:
        conn.settimeout(10)
        conn.sendall(request_bytes)
        data = b""
        while len(data) < (1 << 20):
            try:
                chunk = conn.recv(4096)
            except TimeoutError:
                break
            if not chunk:
                break
            data += chunk
            if b"\r\n\r\n" in data:
                conn.settimeout(0.6)
    match = re.match(rb"HTTP/\d(?:\.\d)?[ \t]+(\d{3})", data)
    status = int(match.group(1)) if match else f"ERR:unparsed:{data[:24]!r}"
    head = data.split(b"\r\n\r\n", 1)[0]
    return status, head.decode(errors="replace").lower()


class Checks:
    """Pass/fail bookkeeping with the reason a failed check matters."""

    def __init__(self, suite):
        self.suite = suite
        self.rows = []

    def add(self, cid, desc, got, want, detail=""):
        ok = str(got) == str(want)
        self.rows.append({"id": cid, "desc": desc, "got": str(got), "want": str(want),
                          "pass": ok, "detail": detail})
        if not ok:
            print(f"FAIL  {cid:<5} got={str(got)[:40]!r} want={str(want)[:40]!r}  {desc}"
                  + (f"\n        detail: {detail[:300]}" if detail else ""))
        return ok

    def report(self):
        passed = sum(1 for row in self.rows if row["pass"])
        failed = len(self.rows) - passed
        print(f"\n{'ID':<5} {'RESULT':<7} {'GOT':>10} {'WANT':>10}  assertion")
        for row in self.rows:
            print(f"{row['id']:<5} {'PASS' if row['pass'] else 'FAIL':<7} "
                  f"{row['got'][:10]:>10} {row['want'][:10]:>10}  {row['desc']}")
        print(f"\nSUMMARY suite={self.suite} pass={passed} fail={failed} total={len(self.rows)}")
        print(f"TOTAL {passed}/{len(self.rows)} passed")
        if failed:
            print("failed: " + "; ".join(r["id"] for r in self.rows if not r["pass"]))
        return failed


def skipped(checks_suite, reason):
    """Report a skip loudly: no gateway ran, therefore nothing was proven."""
    print(f"SKIPPED suite={checks_suite}: no result was produced, nothing is asserted here.")
    for line in str(reason).splitlines():
        print(f"  {line}")
    print(f"SUMMARY suite={checks_suite} pass=0 fail=0 total=0 skipped=1")
    return 3


def banner(args):
    print(f"python        : {sys.version.split()[0]}")
    for key, value in args:
        print(f"{key:<14}: {value}")
