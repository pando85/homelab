#!/usr/bin/env python3
"""Read-only production acceptance using Hermes' installed MCP runtime.

Run from the repository (requires the deployed Hermes image and existing OAuth files):
  kubectl --context=grigri exec -i -n hermes hermes-0 -c hermes -- python - \
    < test/mcp-gateway/hermes-live-acceptance.py

No DNS overrides, credential exports, configuration writes or fallback identities.
Credentials stay inside the Hermes container. Requests include an opaque run ID
for correlation with gateway logs. JWT decoding below only compares the subject;
cryptographic verification remains the gateway's and Grafana's responsibility.
"""
import asyncio
import base64
import hashlib
import json
import time
import urllib.error
import urllib.request
import uuid
from pathlib import Path

from hermes_cli.mcp_config import _get_mcp_servers
from tools.mcp_tool import _connect_server, _ensure_mcp_loop, _run_on_mcp_loop

RUN = "acceptance-" + uuid.uuid4().hex[:12]
HOME = Path("/opt/data")
SERVERS = _get_mcp_servers()


def report(check, **facts):
    print(json.dumps({"run": RUN, "check": check, **facts}), flush=True)


def credential(route):
    data = json.loads((HOME / "mcp-tokens" / f"homelab-{route}.json").read_text())
    assert data["expires_at"] > time.time(), "OAuth credential has expired"
    return data["access_token"]


def subject_hash(subject):
    return hashlib.sha256(subject.encode()).hexdigest()[:12]


async def call(server, tool, args):
    result = await server.session.call_tool(tool, arguments=args)
    assert not getattr(result, "is_error", getattr(result, "isError", False)), tool
    text = "\n".join(getattr(block, "text", "") for block in result.content)
    assert text, f"Empty result for {tool}"
    report(tool, success=True, bytes=len(text))
    return text


async def positive():
    for route in ("context7", "grafana", "firecrawl"):
        name = "homelab-" + route
        config = dict(SERVERS[name])
        assert config["auth"] == "oauth"
        assert config["url"] == f"https://mcp.grigri.cloud/{route}/mcp"
        config["headers"] = {**config.get("headers", {}), "X-Request-ID": RUN + "-" + route}
        server = await asyncio.wait_for(_connect_server(name, config), timeout=65)
        try:
            report("connected", server=name, tools=len(server._tools))
            if route == "context7":
                text = await call(server, "resolve-library-id", {
                    "libraryName": "Next.js", "query": "GET route handler JSON response"})
                assert "/vercel/next.js" in text
                text = await call(server, "query-docs", {
                    "libraryId": "/vercel/next.js", "query": "GET route handler JSON response"})
                assert "Response.json" in text and "Source:" in text
            elif route == "grafana":
                identity = json.loads(await call(server, "user_info", {}))
                payload = credential(route).split(".")[1]
                claims = json.loads(base64.urlsafe_b64decode(payload + "=" * (-len(payload) % 4)))
                assert identity["login"] == claims["sub"], "Subject did not propagate"
                assert not identity["isGrafanaAdmin"], "Unexpected server-admin privileges"
                report("identity_matches_validated_subject",
                       subject_sha256=subject_hash(identity["login"]),
                       roles=[org["role"] for org in identity["orgs"]])
                sources = json.loads(await call(server, "list_datasources", {"limit": 3}))
                assert sources["datasources"] and sources["total"] > 0
                denied = json.loads(await call(server, "grafana_api_request", {
                    "endpoint": "/api/admin/stats", "method": "GET"}))
                assert denied["status"] == 403
                report("server_admin_api_denied", status=denied["status"])
                tools = {tool.name: tool for tool in server._tools}
                assert "update_dashboard" not in tools
                schema = getattr(tools["grafana_api_request"], "inputSchema", None)
                if schema is None:
                    schema = tools["grafana_api_request"].input_schema
                assert schema["properties"]["method"]["enum"] == ["GET"]
                report("write_tools_unavailable", generic_api_methods=["GET"])
            elif route == "firecrawl":
                # Firecrawl has no per-caller identity upstream, but the gateway
                # audit records the caller. Scrape example.com as a stable,
                # harmless read-only probe that proves an actual fetch happened
                # end to end through the gateway.
                assert len(server._tools) > 0, "firecrawl returned no tools"
                tools = {tool.name: tool for tool in server._tools}
                # Try firecrawl_scrape first (least invasive), fall back to firecrawl_search
                if "firecrawl_scrape" in tools:
                    result = await call(server, "firecrawl_scrape", {
                        "url": "https://example.com"})
                    # Result may be JSON or text; just verify we got something back
                    assert result and len(result) > 50, f"scrape returned too little: {len(result)} bytes"
                    report("firecrawl_scrape_success", bytes=len(result), url="https://example.com")
                elif "firecrawl_search" in tools:
                    result = await call(server, "firecrawl_search", {
                        "query": "example domain", "limit": 1})
                    assert result and len(result) > 50, f"search returned too little: {len(result)} bytes"
                    report("firecrawl_search_success", bytes=len(result))
                else:
                    raise AssertionError(f"firecrawl has no usable tools: {sorted(tools.keys())}")
        finally:
            await server.shutdown()


def negative():
    body = json.dumps({"jsonrpc": "2.0", "id": 1, "method": "initialize", "params": {
        "protocolVersion": "2025-06-18", "capabilities": {},
        "clientInfo": {"name": "hermes-acceptance-negative", "version": "1"}}}).encode()
    for route, source in (("context7", None), ("grafana", None), ("firecrawl", None),
                          ("context7", "grafana"), ("grafana", "context7"),
                          ("firecrawl", "grafana"), ("grafana", "firecrawl")):
        headers = {"Content-Type": "application/json", "Accept": "application/json, text/event-stream",
                   "X-Request-ID": RUN + "-negative"}
        if source:
            headers["Authorization"] = "Bearer " + credential(source)
        request = urllib.request.Request(SERVERS["homelab-" + route]["url"], data=body, headers=headers)
        try:
            with urllib.request.urlopen(request, timeout=15) as response:
                status = response.status
        except urllib.error.HTTPError as error:
            status = error.code
        expected = 403 if source else 401
        report("route_authorization", route=route, credential_scope=source or "none",
               status=status, expected=expected)
        assert status == expected


if __name__ == "__main__":
    _ensure_mcp_loop()
    _run_on_mcp_loop(positive(), timeout=180)
    negative()
    report("ACCEPTANCE_PASS", client="Hermes", servers=3)
