#!/usr/bin/env python3
"""Runs inside the pinned Hermes image: proves the exchanged token file is what
Hermes' own OAuth provider reads, and that the provider picks up a rewrite
without a restart (the mechanism the background refresher depends on).

Modes (argv[1]):

* ``schema``     a stub token endpoint serves a JWT; the script must write
                 ``$HERMES_HOME/mcp-tokens/<server>.json`` matching the pinned
                 ``OAuthToken`` model, with an absolute ``expires_at`` reduced by
                 the refresh margin, mode 0600, parent 0700, and NO refresh_token
                 (so Hermes cannot start an unauthorized builtin refresh).
* ``reload``     the pinned ``MCPOAuthManager`` must attach the file's bearer
                 token, then attach a NEW token after an external rewrite, with
                 no refresh/registration request in between.
* ``failclosed`` exchange failures must leave the previous file untouched, exit
                 non-zero, and never leak the subject token, the access token or
                 a response body.
"""

from __future__ import annotations

import json
import os
import shutil
import stat
import subprocess
import sys
import tempfile
import threading
import time
from http.server import BaseHTTPRequestHandler, HTTPServer
from pathlib import Path

HOME = Path(os.environ["HERMES_HOME"])
SCRIPT = os.environ.get("MCP_GATEWAY_TOKEN_SCRIPT", "/work/mcp_gateway_token.py")


def _local_gateway_config() -> str:
    """Private copy of the rendered ``config.json`` for this check only.

    ``point_at()`` rewrites the token endpoint to a loopback stub, and the
    mounted original is shared with every other check in the suite, so the
    mutation has to happen on a scratch copy (otherwise a later check would
    exchange against a dead port and blame the script).
    """
    import atexit

    source = os.environ["MCP_GATEWAY_CONFIG"]
    directory = tempfile.mkdtemp(prefix="check-config-")
    atexit.register(shutil.rmtree, directory, ignore_errors=True)
    path = os.path.join(directory, "config.json")
    shutil.copyfile(source, path)
    return path


GATEWAY_CONFIG = _local_gateway_config()
# Multi-server config: pick the first server for detailed assertions, but the
# token script exchanges for ALL servers.
_ALL_SERVERS = json.loads(Path(GATEWAY_CONFIG).read_text())["servers"]
SERVER_NAME = next(iter(_ALL_SERVERS))
SAFE = SERVER_NAME.replace(".", "_")
TOKENS = HOME / "mcp-tokens" / f"{SAFE}.json"
CLIENT = HOME / "mcp-tokens" / f"{SAFE}.client.json"

SUBJECT = "kanidm-secret-api-token-DO-NOT-ECHO"
SECRET = "exchanged-access-token-DO-NOT-ECHO"


class Stub(BaseHTTPRequestHandler):
    handler = None  # set by the harness: callable(payload_builder) -> (code, payload)
    requests: list = []
    bodies: list = []
    errors: list = []

    def handle_one_request(self) -> None:  # noqa: D102 - record, never die silently
        try:
            super().handle_one_request()
        except Exception as exc:  # noqa: BLE001
            type(self).errors.append(f"{type(exc).__name__}: {exc}")
            raise

    def _record(self, body: str = "") -> None:
        type(self).requests.append(
            {"method": self.command, "path": self.path,
             "agent": self.headers.get("User-Agent", ""), "body": body[:200]}
        )

    def do_GET(self):  # noqa: N802 - OAuth discovery / resource probes
        self._record()
        self._respond(404, {"error": "not_found"})

    def do_POST(self):  # noqa: N802
        body = self.rfile.read(int(self.headers.get("Content-Length", 0))).decode()
        self._record(body)
        type(self).bodies.append(body)
        code, payload = type(self).handler(self.path, body)
        self._respond(code, payload)

    def _respond(self, code: int, payload: dict) -> None:
        raw = json.dumps(payload).encode()
        self.send_response(code)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(raw)))
        self.end_headers()
        self.wfile.write(raw)

    def log_message(self, *_args) -> None:
        pass


def stub_server(handler) -> tuple[HTTPServer, int]:
    Stub.handler = handler
    Stub.requests = []
    Stub.bodies = []
    srv = HTTPServer(("127.0.0.1", 0), Stub)
    threading.Thread(target=srv.serve_forever, daemon=True).start()
    return srv, srv.server_address[1]


def point_at(port: int, *, mcp_path: str = "/grafana/mcp") -> str:
    """Repoint this check's private config at the loopback stub.

    Returns the stub's MCP URL for the primary server. All servers are repointed
    to the stub so the token script can exchange for all of them against the
    loopback fixture.
    """
    cfg = json.loads(Path(GATEWAY_CONFIG).read_text())
    base = f"http://127.0.0.1:{port}"
    cfg["exchange"]["url"] = f"{base}/token"
    # Repoint all servers to the stub, each with its own path
    for i, name in enumerate(cfg["servers"]):
        cfg["servers"][name]["url"] = f"{base}/server{i}/mcp"
    Path(GATEWAY_CONFIG).write_text(json.dumps(cfg))
    return base + mcp_path


def jwt(exp: int) -> str:
    import base64

    def b64(obj: dict) -> str:
        raw = base64.urlsafe_b64encode(json.dumps(obj).encode())
        return raw.decode().rstrip("=")

    return ".".join([b64({"alg": "ES256"}), b64({"exp": exp, "aud": "mcp-gateway"}), "sig"])


def run_token(mode: str = "once") -> subprocess.CompletedProcess:
    env = dict(
        os.environ,
        PYTHONPATH="/opt/hermes",
        PYTHONDONTWRITEBYTECODE="1",
        MCP_GATEWAY_SUBJECT_TOKEN=SUBJECT,
        MCP_GATEWAY_TOKEN_MODE=mode,
        # The script must read this check's private copy (point_at() repointed
        # its token endpoint to the loopback stub); inheriting the mounted
        # original would send the exchange to the real Kanidm URL.
        MCP_GATEWAY_CONFIG=GATEWAY_CONFIG,
    )
    return subprocess.run([sys.executable, SCRIPT], env=env, capture_output=True, text=True, timeout=60)


def good_handler(path: str, body: str):
    # The same stub serves the container script (urllib, POST /token) and the
    # SDK flow (httpx, POST <mcp path>): answer by shape instead of asserting,
    # so a request we did not expect surfaces as a failed exchange with an
    # inspectable log rather than as a dropped connection both callers
    # misreport.
    import urllib.parse

    if not path.endswith("/token"):
        return 200, {"jsonrpc": "2.0", "result": {}, "id": 1}
    params = dict(urllib.parse.parse_qsl(body))
    if params.get("grant_type") != "urn:ietf:params:oauth:grant-type:token-exchange":
        return 400, {"error": "unsupported_grant_type"}
    if not params.get("subject_token"):
        return 400, {"error": "invalid_request", "hint": "subject_token missing"}
    # Echo back the requested scope so each server gets its own grant
    req_scope = params.get("scope", "mcp_grafana")
    return 200, {
        "access_token": SECRET,
        "token_type": "Bearer",
        "expires_in": 3600,
        "refresh_token": "shall-not-be-stored",
        "scope": req_scope,
    }


def main() -> int:
    mode = sys.argv[1]
    HOME.mkdir(parents=True, exist_ok=True)

    if mode == "failclosed":
        srv, port = stub_server(lambda p, b: (400, {"error": "invalid_request", "echo": SUBJECT}))
        point_at(port)
        TOKENS.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
        TOKENS.write_text(json.dumps({"access_token": "previous", "token_type": "Bearer",
                                      "expires_in": 60, "expires_at": int(time.time()) + 60}))
        before = TOKENS.read_bytes()
        proc = run_token()
        combined = proc.stdout + proc.stderr
        # A crash must not masquerade as fail-closed: require the deliberate
        # error path AND the absence of a Python traceback.
        assert proc.returncode != 0, "a failed exchange must not exit zero"
        assert "mcp-gateway-token: ERROR" in combined, f"no fail-closed marker: {combined}"
        assert "Traceback" not in combined, f"crashed instead of failing closed: {combined}"
        assert TOKENS.read_bytes() == before, "previous token overwritten on failure"
        leaked = [v for v in (SUBJECT, SECRET, "invalid_request") if v in combined]
        assert not leaked, f"output leaked {leaked}"
        assert not list(TOKENS.parent.glob(".tmp-*")), "temp file left behind"
        srv.shutdown()
        print("PASS exchange failure left the old token and leaked nothing")
        return 0

    srv, port = stub_server(good_handler)
    point_at(port)

    proc = run_token()
    assert proc.returncode == 0, f"token run failed: {proc.stdout}{proc.stderr}"

    if mode in ("schema", "ownership"):
        text = TOKENS.read_text()
        payload = json.loads(text)
        assert payload["access_token"] == SECRET
        assert "refresh_token" not in payload, "refresh_token must never be stored"
        assert payload["token_type"] == "Bearer"
        margin = json.loads(Path(GATEWAY_CONFIG).read_text())["exchange"]["marginSeconds"]
        assert payload["expires_in"] == 3600 - margin, payload["expires_in"]
        assert abs(payload["expires_at"] - (time.time() + payload["expires_in"])) < 10

        from mcp.shared.auth import OAuthClientInformationFull, OAuthToken

        stored = OAuthToken.model_validate({k: v for k, v in payload.items() if k != "expires_at"})
        assert stored.access_token == SECRET
        client = json.loads(CLIENT.read_text())
        OAuthClientInformationFull.model_validate(client)
        assert client["client_id"], "client record needs the pre-registered client_id"

        st = TOKENS.stat()
        cst = CLIENT.stat()
        dst = TOKENS.parent.stat()
        assert stat.S_IMODE(st.st_mode) == 0o600, oct(st.st_mode)
        assert stat.S_IMODE(cst.st_mode) == 0o600, oct(cst.st_mode)
        assert stat.S_IMODE(dst.st_mode) == 0o700, "token dir must be 0700"
        # Ownership: the agent reads these files as its own uid (production
        # renders the helpers with runAsUser 10000, same as the agent).
        assert st.st_uid == os.geteuid(), f"{TOKENS.name} uid {st.st_uid} != {os.geteuid()}"
        assert cst.st_uid == os.geteuid(), f"{CLIENT.name} uid {cst.st_uid} != {os.geteuid()}"
        assert dst.st_uid == os.geteuid(), f"mcp-tokens uid {dst.st_uid} != {os.geteuid()}"
        if mode == "ownership":
            # This is the check that only means something when the container ran
            # unprivileged: the harness passes --user 10000:10000 in that case.
            # A host that cannot widen the scratch tree runs as root instead,
            # where the uid pin below does not apply (ownership by euid is still
            # asserted above).
            if os.geteuid() != 0:
                assert os.geteuid() == 10000, f"expected the agent uid, got {os.geteuid()}"
            else:
                print("NOTE running as root: agent-uid ownership not exercised")

        # Hermes' own storage must see a live token with the right remaining TTL.
        import asyncio

        from tools.mcp_oauth import HermesTokenStorage

        tokens = asyncio.run(HermesTokenStorage(SERVER_NAME).get_tokens())
        assert tokens is not None and tokens.access_token == SECRET
        assert 0 < tokens.expires_in <= 3600 - margin + 5, tokens.expires_in
        srv.shutdown()
        print(f"PASS token file matches the pinned OAuthToken schema (uid {os.geteuid()}) "
              "and Hermes storage reads it")
        return 0

    if mode == "idempotent":
        # Two consecutive runs whose exchange returns the same credential must not
        # touch either file: the running gateway reloads on mtime, so churn here
        # would mean a reconnect on every refresher loop (and every pod start). The
        # script tolerates the clock moving (a re-issued identical token keeps its
        # stored expiry), which is what makes this deterministic rather than a race
        # on whether the two runs landed in the same second.
        before = (TOKENS.read_bytes(), CLIENT.read_bytes())
        mtimes = (TOKENS.stat().st_mtime_ns, CLIENT.stat().st_mtime_ns)
        second = run_token()
        assert second.returncode == 0, second.stdout + second.stderr
        assert (TOKENS.read_bytes(), CLIENT.read_bytes()) == before, "content changed on re-run"
        assert (TOKENS.stat().st_mtime_ns, CLIENT.stat().st_mtime_ns) == mtimes, (
            "unchanged write still rewrote the file: mtime churn -> reload storm")

        # The other half: the short circuit must never hide a ROTATION. With a
        # different credential on offer, the file has to change and gain a new mtime.
        srv.shutdown()
        rotated = "rotated-" + SECRET
        srv2, port2 = stub_server(lambda p, b: (200, {
            "access_token": rotated, "token_type": "Bearer", "expires_in": 3600,
            "scope": json.loads(Path(GATEWAY_CONFIG).read_text())["servers"][SERVER_NAME]["oauth"]["scope"]}))
        point_at(port2)
        third = run_token()
        assert third.returncode == 0, third.stdout + third.stderr
        payload = json.loads(TOKENS.read_text())
        assert payload["access_token"] == rotated, "a rotated token was not stored"
        assert TOKENS.stat().st_mtime_ns != mtimes[0], "rotation stored but mtime unchanged"
        srv2.shutdown()
        print("PASS identical re-runs are inert while a rotated credential does land")
        return 0

    if mode == "symlink":
        # A credential path replaced by a symlink (a planted or hand-edited
        # HERMES_HOME) must be refused, never written through: os.replace() onto
        # a link would silently redirect the agent's credential somewhere else.
        outside = HOME / "elsewhere.json"
        outside.write_text("{\"access_token\": \"sentinel\"}\n")
        TOKENS.unlink()
        TOKENS.symlink_to(outside)
        proc = run_token()
        combined = proc.stdout + proc.stderr
        assert proc.returncode != 0, "writing through a symlinked credential path must fail"
        assert "mcp-gateway-token: ERROR" in combined, combined
        assert "Traceback" not in combined, f"crashed instead of failing closed: {combined}"
        assert TOKENS.is_symlink(), "the symlink was replaced instead of refused"
        assert outside.read_text() == "{\"access_token\": \"sentinel\"}\n", (
            "the credential was written through the symlink")
        assert not list(TOKENS.parent.glob(".tmp-*")), "temp file left behind"
        srv.shutdown()
        print("PASS symlinked credential path refused without touching the target")
        return 0

    if mode == "obstructed":
        # The credential path is a directory instead of a file, so the atomic
        # replace fails. This covers the write-failure cleanup branch - the one
        # that once died on a missing `import contextlib`, turning a clean
        # fail-closed into a traceback - so it is exercised, not trusted.
        TOKENS.unlink()
        TOKENS.mkdir()
        proc = run_token()
        combined = proc.stdout + proc.stderr
        assert proc.returncode != 0, "an unwritable credential path must not exit zero"
        assert "mcp-gateway-token: ERROR" in combined, f"no fail-closed marker: {combined}"
        assert "Traceback" not in combined, f"crashed instead of failing closed: {combined}"
        assert not list(TOKENS.parent.glob(".tmp-*")), "temp file left behind after a failed write"
        srv.shutdown()
        print("PASS failed atomic write cleaned up its temp file and failed closed")
        return 0

    if mode == "reload":
        # The mechanism the background refresher exists for: an external writer
        # touching $HERMES_HOME/mcp-tokens/<server>.json must be visible to the
        # ALREADY-BUILT provider on its NEXT request - no restart, no re-auth,
        # no dynamic registration. Driven through a real client so the SDK's
        # auth flow runs exactly as it does in the gateway (a hand-rolled
        # generator.aclose() dead-locks the provider's anyio context lock).
        import asyncio

        from mcp.client.auth.oauth2 import OAuthClientProvider
        from tools.mcp_oauth import HermesTokenStorage
        from tools.mcp_oauth_manager import get_manager
        from tools.mcp_tool import sdk_httpx

        httpx = sdk_httpx()
        url = json.loads(Path(GATEWAY_CONFIG).read_text())["servers"][SERVER_NAME]["url"]
        assert url.startswith("http://127.0.0.1:"), "reload must run against the stub"

        provider = get_manager().get_or_build_provider(SERVER_NAME, url, {})
        assert isinstance(provider, OAuthClientProvider), "pinned provider unavailable"

        mcp_responses = []

        async def two_requests() -> list[str]:
            seen: list[str] = []
            # auth=provider is exactly how tools/mcp_tool wires the pinned
            # OAuthClientProvider into the streamable HTTP session: one client,
            # one event loop, so the SDK's per-request auth flow runs as it does
            # in the gateway (and its anyio context lock stays in one task).
            client = httpx.AsyncClient(auth=provider, timeout=15.0)
            try:
                for i in range(2):
                    if i == 1:
                        # Stand in for this container's rotation: an external
                        # process rewrites the file between two live requests.
                        time.sleep(0.05)  # distinct mtime_ns on coarse clocks
                        rotated = json.loads(TOKENS.read_text())
                        rotated["access_token"] = "rotated-access-token"
                        rotated["expires_in"] = 3000
                        rotated["expires_at"] = int(time.time()) + 3000
                        TOKENS.write_text(json.dumps(rotated))
                    resp = await client.post(url, json={"jsonrpc": "2.0", "method": "ping", "id": 1})
                    # Header names only: never echo credential values into test
                    # output, even the planted dummies.
                    mcp_responses.append((resp.status_code, sorted(resp.request.headers.keys())))
                    seen.append(resp.request.headers.get("Authorization", ""))
                    assert resp.status_code == 200, resp.status_code
            finally:
                await client.aclose()
            return seen

        # Only the flow's own traffic counts: the initial exchange by the
        # subprocess happened before it.
        baseline = len(Stub.requests)
        try:
            sent = asyncio.run(two_requests())
        except Exception as exc:  # noqa: BLE001 - surface the stub's own failures
            raise RuntimeError(f"stub saw {Stub.errors}; requests={Stub.requests[baseline:]}") from exc
        def auth(state: int) -> str:
            """'sent' / 'missing' for request `state`, never the value itself."""
            code, names = mcp_responses[state]
            assert code == 200, code
            return "sent" if "authorization" in names else "missing"

        assert auth(0) == "sent", "first request carried no Authorization header"
        assert auth(1) == "sent", "second request carried no Authorization header"
        assert sent[0] == f"Bearer {SECRET}", f"unexpected first bearer: {sent[0][:12]}..."
        assert sent[1] != sent[0], "external rewrite was not picked up"
        assert sent[1] == "Bearer rotated-access-token", "reloaded token was not used"

        ctx = provider.context
        assert ctx.is_token_valid(), "reloaded token must be treated as valid"
        assert not ctx.can_refresh_token(), "no refresh_token may be stored"

        flow_requests = Stub.requests[baseline:]
        token_posts = [r for r in flow_requests if r["path"].endswith("/token")]
        assert not token_posts, (
            f"the flow must not refresh or re-exchange on its own: {token_posts}")
        mcp_posts = [r for r in flow_requests if not r["path"].endswith("/token")]
        assert len(mcp_posts) >= 2, f"expected the two driven requests: {flow_requests}"
        assert all("grant_type=" not in r["body"] for r in mcp_posts), mcp_posts
        registered = [r for r in flow_requests if r["path"].startswith("/register")]
        assert not registered, f"dynamic registration was attempted: {registered}"
        assert HermesTokenStorage(SERVER_NAME) is not None, "storage must read the same file"
        srv.shutdown()
        print("PASS pinned provider reloads an externally written token per request "
              "with no refresh or registration")
        return 0

    return 1


if __name__ == "__main__":
    sys.exit(main())
