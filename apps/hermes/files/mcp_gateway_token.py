#!/usr/bin/env python3
"""Exchange this namespace's Kanidm service-account API token for homelab MCP
gateway access tokens and store them in Hermes' OAuth token files.

Multi-target: exchanges one token per configured gateway route (each for its own
``oauth.scope``), writing separate token files. Hermes' OAuth provider reloads each
file by mtime on the next request, so rotation needs no restart.

Why the OAuth token store and not ``headers: Authorization: Bearer ${T}``: at
the deployed tag (v2026.8.16.2, commit 7339f5f1) ``${VAR}`` expansion of
``url``/``headers`` happens at config-load / connect time only, so a rotating
bearer in ``.env`` cannot reach a session that is already connected. Hermes does
have a fresh-token hook for exactly this case - ``tools/mcp_oauth_manager``
watches ``$HERMES_HOME/mcp-tokens/<server>.json`` by mtime inside
``async_auth_flow``, i.e. per request, and reloads without a restart. This
container's whole job is to keep those files fresher than their expiry.

Storage layout and file naming come from Hermes itself (``_get_token_dir`` /
``_safe_filename``) so they cannot drift from what the provider reads.

Schema written (``mcp.shared.auth.OAuthToken`` fields plus Hermes' absolute
``expires_at``, which ``HermesTokenStorage.get_tokens`` turns back into the
remaining TTL after a restart)::

    {"access_token": ..., "token_type": "Bearer", "expires_in": <seconds>,
     "scope": "<space separated>", "expires_at": <unix>}

``refresh_token`` is deliberately never written: the SDK's
``can_refresh_token()`` also requires it, so Hermes will not attempt an
unauthorized refresh or a dynamic client registration behind this container's
back. Rotation here refreshes ``marginSeconds`` before expiry.

Identity: a Kanidm *service account* API token (workload identity), never a
person's session - today the ``grafana-mcp`` account the working sidecar already
uses, so no new privileged identity is introduced. The token reaches this process
from a Secret reference through the environment; the exchange request is built in
memory by ``urllib``, never through a command line, because argv is readable by
any process in the container through ``/proc/<pid>/cmdline`` (a
``curl --data-urlencode "subject_token=$TOKEN"`` leaks it exactly that way). The
container-runtime test samples every ``/proc/*/cmdline`` mid-request and asserts
the credential appears only in the POST body.
When Kaniop rotates that token the exchanges start failing; this container exits
after ``maxFailures`` so the pod restarts and re-reads the Secret instead of
running on a stale credential.

Fail closed: any error leaves the previous token files untouched and exits
non-zero. Run twice by the chart, in the ``hermes-mcp-gateway`` helper pod that
mounts the agent's ``hermes-data`` claim: once as an init container (blocks the
helpers from coming up until a credential exists) and once as the long-running
refresher container.
"""

from __future__ import annotations

import base64
import contextlib
import json
import os
import signal
import ssl
import sys
import tempfile
import time
import urllib.parse
import urllib.request

_STOP = False

# How much earlier than a re-issued identical token the stored file may claim to
# expire and still be considered current. Only ever skips a write when the
# credential is byte-identical, so the true expiry can be at most this late.
_STALE_TOLERANCE = 60


def _stop(_signum, _frame) -> None:  # pragma: no cover - signal path
    global _STOP
    _STOP = True


def _fail(reason: str) -> int:
    """Report a cause without echoing a token, a response body, or a secret."""
    print(f"mcp-gateway-token: ERROR {reason}", file=sys.stderr)
    return 1


def _subject_token() -> str:
    """This namespace's Kanidm service-account API token.

    Delivered by the kubelet from the Kaniop-managed Secret via ``secretKeyRef``
    (chart: ``MCP_GATEWAY_SUBJECT_TOKEN``): never a command line, never a
    mounted file the agent uid could not read, never logged. Kaniop rotation
    changes the Secret, not the running container's environment, so a rotated
    token makes exchanges fail until the container restarts - which is exactly
    what ``maxFailures`` triggers.
    """
    return (os.environ.get("MCP_GATEWAY_SUBJECT_TOKEN") or "").strip()


def _token_paths(server_name: str) -> tuple[str, str]:
    from tools.mcp_oauth import _get_token_dir, _safe_filename

    directory = _get_token_dir()
    safe = _safe_filename(server_name)
    return str(directory / f"{safe}.json"), str(directory / f"{safe}.client.json")


def _jwt_expiry(token: str) -> int | None:
    """Unverified ``exp`` of a JWT, used only when the AS omits ``expires_in``."""
    parts = token.split(".")
    if len(parts) != 3:
        return None
    try:
        blob = parts[1] + "=" * (-len(parts[1]) % 4)
        claims = json.loads(base64.urlsafe_b64decode(blob))
    except Exception:  # noqa: BLE001
        return None
    exp = claims.get("exp")
    return int(exp) if isinstance(exp, (int, float)) else None


def _exchange(cfg: dict, subject_token: str, scope: str) -> tuple[str, int, str]:
    body = urllib.parse.urlencode(
        {
            "grant_type": "urn:ietf:params:oauth:grant-type:token-exchange",
            "client_id": cfg["clientId"],
            "audience": cfg["audience"],
            "scope": scope,
            "subject_token": subject_token,
            "subject_token_type": "urn:ietf:params:oauth:token-type:access_token",
        }
    ).encode()
    req = urllib.request.Request(
        cfg["url"],
        data=body,
        headers={"Content-Type": "application/x-www-form-urlencoded", "Accept": "application/json"},
        method="POST",
    )
    ctx = ssl.create_default_context(cafile=os.environ.get("SSL_CERT_FILE") or None)
    with urllib.request.urlopen(req, timeout=int(cfg["httpTimeoutSeconds"]), context=ctx) as resp:
        payload = json.loads(resp.read().decode("utf-8"))

    token = payload.get("access_token")
    if not isinstance(token, str) or not token:
        # Kanidm error payloads can echo request parameters: status only.
        raise RuntimeError("response carried no access_token")

    expires_in = payload.get("expires_in")
    expires_in = int(expires_in) if isinstance(expires_in, (int, float)) else _jwt_expiry(token)
    if expires_in is None:
        raise RuntimeError("token lifetime unknown (no expires_in and no JWT exp)")

    resp_scope = payload.get("scope")
    return token, int(expires_in), resp_scope if isinstance(resp_scope, str) else scope


def _guard_parent(directory: str) -> None:
    if os.path.islink(directory):
        # Never follow a planted symlink out of the credential directory.
        raise RuntimeError(f"{os.path.basename(directory)} is a symlink, not a directory")
    os.makedirs(directory, mode=0o700, exist_ok=True)
    if os.path.islink(directory):
        raise RuntimeError(f"{os.path.basename(directory)} became a symlink")
    st = os.stat(directory)
    if st.st_uid not in (os.geteuid(), 0) and os.geteuid() != 0:
        raise RuntimeError(f"token directory owned by uid {st.st_uid}, not {os.geteuid()}")
    if st.st_mode & 0o077:
        # Tighten only our own credential directory; never chmod user data.
        os.chmod(directory, 0o700)


def _write_atomic(path: str, text: str) -> None:
    if os.path.islink(path):
        # An existing symlink would let os.replace() aim the credential file at
        # whatever its target is (or leave the link pointing somewhere private):
        # refuse instead of writing through it.
        raise RuntimeError(f"{os.path.basename(path)} is a symlink; refusing to write through it")
    if os.path.exists(path):
        st = os.stat(path)
        if st.st_uid not in (os.geteuid(), 0) and os.geteuid() != 0:
            raise RuntimeError(f"{os.path.basename(path)} owned by uid {st.st_uid}, not {os.geteuid()}")
    fd, tmp = tempfile.mkstemp(dir=os.path.dirname(path), prefix=".tmp-", suffix=".token")
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as fh:
            fh.write(text)
            fh.flush()
            os.fsync(fh.fileno())
        os.chmod(tmp, 0o600)
        os.replace(tmp, path)
    except Exception:
        with contextlib.suppress(OSError):
            os.unlink(tmp)
        raise


def _write_client_info(client_path: str, client_id: str) -> None:
    """Optional pre-registered client record (``<server>.client.json``).

    Matches the RFC 7591 shape Hermes would have stored after dynamic client
    registration, so a later move to real authorization-code + refresh tokens
    needs no other change. Public client: no secret, PKCE only.
    """
    if not client_id:
        return
    record = {
        "client_id": client_id,
        "client_name": "Hermes Agent",
        "grant_types": ["authorization_code", "refresh_token"],
        "response_types": ["code"],
        "token_endpoint_auth_method": "none",
    }
    if os.path.islink(client_path):
        raise RuntimeError(f"{os.path.basename(client_path)} is a symlink; refusing to write through it")
    if os.path.exists(client_path):
        with open(client_path, encoding="utf-8") as fh:
            try:
                if json.load(fh) == record:
                    return
            except ValueError:
                pass
    _write_atomic(client_path, json.dumps(record, indent=2) + "\n")


def _refresh(cfg: dict, subject_token: str, tokens_path: str, client_path: str, scope: str) -> int:
    token, expires_in, resp_scope = _exchange(cfg, subject_token, scope)
    remaining = max(expires_in - int(cfg["marginSeconds"]), 1)
    payload = {
        "access_token": token,
        "token_type": "Bearer",
        "expires_in": remaining,
        "scope": resp_scope,
        "expires_at": int(time.time()) + remaining,
    }
    text = json.dumps(payload, indent=2) + "\n"
    # Client record first, credential last: a reader that sees a fresh token
    # must never be able to miss the client file that token belongs to (both
    # files are replaced atomically, so each is individually always whole).
    # _write_client_info is a no-op when the record already matches, so this
    # costs nothing per loop and cannot churn the mtime Hermes' reload watches.
    _write_client_info(client_path, str(cfg.get("oauthClientId") or ""))
    if os.path.isfile(tokens_path) and not os.path.islink(tokens_path):
        try:
            with open(tokens_path, encoding="utf-8") as fh:
                stored = json.load(fh)
        except ValueError:
            stored = None
        # Same credential, expiry within STALE_TOLERANCE seconds: leave the file
        # alone. Hermes reloads on mtime, so an unchanged-token rewrite per loop
        # would reconnect the MCP session forever. The stored expiry is kept as
        # written (never extended), so the file stays conservative about lifetime.
        same = stored and all(stored.get(k) == payload[k] for k in
                              ("access_token", "token_type", "expires_in", "scope"))
        if same and int(stored.get("expires_at") or 0) + _STALE_TOLERANCE >= payload["expires_at"]:
            return remaining  # unchanged: no mtime churn, no reload storm
    _write_atomic(tokens_path, text)
    print(f"mcp-gateway-token: stored token, valid {remaining}s")
    return remaining


def _scope_of(server_name: str, server_cfg: dict) -> str:
    """The OAuth scope this route is exchanged for.

    Read from ``oauth.scope``, not from a top-level ``scope``: Hermes passes
    ``config.get("oauth")`` into the provider (``tools/mcp_tool.py:3395``) and
    the provider's own docs name ``oauth.scope`` the authoritative key
    (``tools/mcp_oauth.py:1315``). A top-level ``scope`` in ``mcp_servers.<name>``
    is ignored by Hermes, so exchanging for it would silently mint a credential
    for the wrong grant.
    """
    oauth = server_cfg.get("oauth")
    if not isinstance(oauth, dict):
        raise ValueError(f"server '{server_name}' has no 'oauth' block")
    return str(oauth.get("scope") or "").strip()


def main() -> int:
    signal.signal(signal.SIGTERM, _stop)
    config_path = os.environ.get("MCP_GATEWAY_CONFIG", "/mcp-gateway/config.json")
    try:
        with open(config_path, encoding="utf-8") as fh:
            root = json.load(fh)
        exchange_cfg = dict(root["exchange"])
        servers = root.get("servers", {})
        if not isinstance(servers, dict):
            return _fail("gateway config 'servers' is not an object")
        if not servers:
            return _fail("gateway config has no servers defined")
    except Exception as exc:  # noqa: BLE001
        return _fail(f"gateway config unreadable: {type(exc).__name__}")

    subject_token = _subject_token()
    if not subject_token:
        return _fail("no service-account API token in the environment")
    if not str(exchange_cfg.get("url", "")).startswith("https://") and "127.0.0.1" not in str(exchange_cfg.get("url", "")):
        return _fail("refusing to send a workload credential to a non-TLS token endpoint")
    if not all((exchange_cfg.get("clientId"), exchange_cfg.get("audience"))):
        return _fail("clientId and audience are required")

    # Validate all servers and prepare token paths
    server_tasks = []
    for server_name, server_cfg in servers.items():
        if (server_cfg.get("auth") or "").lower().strip() != "oauth":
            return _fail(f"server '{server_name}' must authenticate with the OAuth token store")
        try:
            scope = _scope_of(server_name, server_cfg)
            if not scope:
                raise ValueError(f"server '{server_name}' has no 'oauth.scope'")
            tokens_path, client_path = _token_paths(server_name)
            _guard_parent(os.path.dirname(tokens_path))
        except Exception as exc:  # noqa: BLE001
            return _fail(f"token store unavailable for '{server_name}': {exc}")
        server_tasks.append({
            "name": server_name,
            "scope": scope,
            "tokens_path": tokens_path,
            "client_path": client_path,
        })

    if (os.environ.get("MCP_GATEWAY_TOKEN_MODE") or "once").strip() == "once":
        # Initial exchange for all servers
        for task in server_tasks:
            try:
                _refresh(exchange_cfg, subject_token, task["tokens_path"], task["client_path"], task["scope"])
            except Exception as exc:  # noqa: BLE001
                return _fail(f"initial exchange failed for '{task['name']}': {type(exc).__name__}")
        return 0

    # Loop mode: refresh all servers on each iteration
    failures = 0
    while not _STOP:
        try:
            min_remaining = None
            for task in server_tasks:
                remaining = _refresh(exchange_cfg, subject_token, task["tokens_path"], task["client_path"], task["scope"])
                if min_remaining is None or remaining < min_remaining:
                    min_remaining = remaining
            failures = 0
        except Exception as exc:  # noqa: BLE001
            failures += 1
            if failures >= int(exchange_cfg["maxFailures"]):
                return _fail(f"{failures} consecutive exchanges failed ({type(exc).__name__}); "
                              "restarting the pod instead of serving a stale credential")
            print(f"mcp-gateway-token: exchange failed ({type(exc).__name__}) "
                  f"{failures}/{exchange_cfg['maxFailures']}; previous tokens kept", file=sys.stderr)
            min_remaining = int(exchange_cfg["minIntervalSeconds"])
        deadline = time.time() + max(min(min_remaining, int(exchange_cfg["maxIntervalSeconds"])),
                                     int(exchange_cfg["minIntervalSeconds"]))
        while not _STOP and time.time() < deadline:
            time.sleep(1)
    return 0


if __name__ == "__main__":
    sys.exit(main())
