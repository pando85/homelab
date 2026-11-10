#!/usr/bin/env python3
"""Unit tests for the gateway helper scripts, driven by the *rendered* artifacts.

Scope of this file is the pure-Python logic that does not need the Hermes
package: reading the rendered ``config.json``, deriving the per-route scope,
atomic 0600 credential writes, and the no-mtime-churn skip. What needs Hermes
itself - ``atomic_config_write``, ``mcp-tokens`` naming, the real exchange over
HTTP, the uid behaviour - is covered against the real image in
``test_helper_container_runtime.py``, and what needs the chart is covered in
``test_render_enable.py``. Nothing here mocks Hermes away to make a weak claim.

Fixtures are not hand-written: they come from ``helm template``, so a template
that ships a shape the scripts do not read fails here instead of in the pod.
Run: ``python3 apps/hermes/tests/test_mcp_gateway_multi.py``.
"""

from __future__ import annotations

import importlib.util
import json
import os
import subprocess
import sys
import tempfile
import time

REPO = subprocess.run(["git", "rev-parse", "--show-toplevel"], capture_output=True,
                      text=True, check=True).stdout.strip()
FILES = os.path.join(REPO, "apps", "hermes", "files")


def _load(name: str):
    spec = importlib.util.spec_from_file_location(name, os.path.join(FILES, name + ".py"))
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)  # type: ignore[union-attr]
    return module


merge = _load("mcp_gateway_merge")
token = _load("mcp_gateway_token")


def _rendered_config() -> dict:
    """The config.json exactly as ``helm template`` renders it into the ConfigMap."""
    out = subprocess.run(["helm", "template", "--namespace", "hermes", "hermes",
                          os.path.join(REPO, "apps", "hermes"),
                          "--set", "global.mcpGateway.enabled=true",
                          "--show-only", "templates/configmap-mcp-gateway.yaml"],
                         capture_output=True, text=True, check=True).stdout
    import yaml
    doc = next(d for d in yaml.safe_load_all("---\n" + out) if isinstance(d, dict))
    return json.loads(doc["data"]["config.json"])


def test_rendered_config_loads_for_both_routes():
    cfg = _rendered_config()
    with tempfile.NamedTemporaryFile("w", suffix=".json", delete=False) as fh:
        json.dump(cfg, fh)
        path = fh.name
    try:
        servers, exchange = merge._load_gateway_config(path)
    finally:
        os.unlink(path)
    assert set(servers) == {"homelab-grafana", "homelab-context7", "homelab-firecrawl"}, sorted(servers)
    assert exchange["clientId"] == exchange["audience"] == "mcp-gateway"
    assert exchange["marginSeconds"] == 300 and exchange["maxFailures"] == 3
    print("✓ rendered config.json: 3 routes, exchange settings")


def test_scope_is_read_where_hermes_reads_it():
    """``oauth.scope``, not a top-level ``scope`` (Hermes passes config['oauth'])."""
    cfg = _rendered_config()
    assert token._scope_of("homelab-grafana", cfg["servers"]["homelab-grafana"]) == "mcp_grafana"
    assert token._scope_of("homelab-context7", cfg["servers"]["homelab-context7"]) == "mcp_context7"
    assert token._scope_of("homelab-firecrawl", cfg["servers"]["homelab-firecrawl"]) == "mcp_firecrawl"
    for name, entry in cfg["servers"].items():
        assert "scope" not in entry, f"{name}: top-level scope would be ignored by Hermes"
    for bad in ({}, {"oauth": {}}, {"oauth": None}, {"scope": "mcp_grafana"}):
        try:
            scope = token._scope_of("x", bad)
        except ValueError:
            continue
        assert not scope, f"{bad!r} must not yield a usable scope, got {scope!r}"
    print("✓ per-route scope from oauth.scope; missing or misplaced scope rejected")


def test_credentials_are_written_0600_and_atomically():
    with tempfile.TemporaryDirectory() as tmp:
        path = os.path.join(tmp, "homelab-grafana.json")
        payload = {"access_token": "synthetic", "token_type": "Bearer", "expires_in": 600,
                   "scope": "mcp_grafana", "expires_at": int(time.time()) + 600}
        token._write_atomic(path, json.dumps(payload, indent=2) + "\n")
        assert oct(os.stat(path).st_mode)[-3:] == "600"
        assert json.load(open(path)) == payload
        assert not [f for f in os.listdir(tmp) if f.startswith(".tmp-")], "temp file leaked"
        # A planted symlink must not become a write target.
        link = os.path.join(tmp, "escape.json")
        os.symlink(os.path.join(tmp, "outside"), link)
        try:
            token._write_atomic(link, "{}")
            raise AssertionError("wrote through a symlink")
        except RuntimeError as exc:
            assert "symlink" in str(exc)
        print("✓ 0600 atomic write, no temp leak, symlink target refused")


def test_identical_token_does_not_touch_the_file():
    """Same credential + still valid ⇒ no write, because Hermes reloads by mtime."""
    with tempfile.TemporaryDirectory() as tmp:
        path = os.path.join(tmp, "homelab-grafana.json")
        client = os.path.join(tmp, "homelab-grafana.client.json")
        remaining = 600
        stored = {"access_token": "synthetic", "token_type": "Bearer",
                  "expires_in": remaining, "scope": "mcp_grafana",
                  "expires_at": int(time.time()) + remaining}
        with open(path, "w", encoding="utf-8") as fh:
            json.dump(stored, fh)
        os.chmod(path, 0o600)
        os.utime(path, (time.time() - 50, time.time() - 50))
        before = os.stat(path).st_mtime_ns
        cfg = {"url": "https://idm.grigri.cloud/oauth2/token", "clientId": "mcp-gateway",
               "audience": "mcp-gateway", "httpTimeoutSeconds": 15, "marginSeconds": 300,
               "oauthClientId": "mcp-gateway"}
        real = token._exchange
        token._exchange = lambda *a, **k: ("synthetic", remaining + 300, "mcp_grafana")
        try:
            token._refresh(cfg, "subject", path, client, "mcp_grafana")
        finally:
            token._exchange = real
        assert os.stat(path).st_mtime_ns == before, "identical credential must not churn mtime"
        client_info = json.load(open(client, encoding="utf-8"))
        assert client_info["client_id"] == "mcp-gateway"
        assert "client_secret" not in client_info and "refresh_token" not in json.load(open(path))
        print("✓ unchanged credential: no rewrite, no mtime churn, no refresh_token")


def test_expired_written_token_is_replaced():
    with tempfile.TemporaryDirectory() as tmp:
        path = os.path.join(tmp, "homelab-grafana.json")
        client = os.path.join(tmp, "homelab-grafana.client.json")
        stale = {"access_token": "synthetic-old", "token_type": "Bearer", "expires_in": 5,
                 "scope": "mcp_grafana", "expires_at": int(time.time()) - 1}
        with open(path, "w", encoding="utf-8") as fh:
            json.dump(stale, fh)
        cfg = {"url": "https://idm.grigri.cloud/oauth2/token", "clientId": "mcp-gateway",
               "audience": "mcp-gateway", "httpTimeoutSeconds": 15, "marginSeconds": 300,
               "oauthClientId": ""}
        real = token._exchange
        token._exchange = lambda *a, **k: ("synthetic-new", 900, "mcp_grafana")
        try:
            token._refresh(cfg, "subject", path, client, "mcp_grafana")
        finally:
            token._exchange = real
        fresh = json.load(open(path, encoding="utf-8"))
        assert fresh["access_token"] == "synthetic-new"
        assert fresh["expires_at"] > time.time(), "never leave an expired credential on disk"
        assert not os.path.exists(client), "no client record when oauthClientId is empty"
        print("✓ expired credential is replaced before anything else is touched")


if __name__ == "__main__":
    print("Running MCP gateway script tests (fixtures from helm template)...")
    for fn in [test_rendered_config_loads_for_both_routes,
               test_scope_is_read_where_hermes_reads_it,
               test_credentials_are_written_0600_and_atomically,
               test_identical_token_does_not_touch_the_file,
               test_expired_written_token_is_replaced]:
        fn()
    print("\n✓ All script tests passed")
