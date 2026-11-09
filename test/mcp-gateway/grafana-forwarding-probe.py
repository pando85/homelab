#!/usr/bin/env python3
"""Measure what mcp-grafana v2.1.1 actually sends to Grafana for each caller header.

The delegation question for the Grafana MCP route is not whether upstream *documents*
header forwarding, but whether a caller's credential arrives at Grafana intact, and
whether a caller can inject credentials or targets of their own. Run against the
pinned image, so the answer is versioned:

  docker.io/grafana/mcp-grafana:2.1.1

A fake Grafana records every header of the API call a tool produces; the harness then
drives the MCP endpoint over Streamable HTTP with different caller headers. Each
scenario reports exactly which credential and which headers Grafana saw.

    python3 test/mcp-gateway/grafana-forwarding-probe.py [--image IMG] [--override]

Requires podman. Nothing here touches the cluster or live Grafana.
"""
import argparse
import http.server
import json
import re
import subprocess
import sys
import threading
import time

CAPTURE = []          # [(path, {header: value})] recorded by the fake Grafana
SSRF_HITS = []        # requests that reached the second, "internal" listener
LOCK = threading.Lock()

GRAFANA_PORT = 9161
SSRF_PORT = 9162
MCP_PORT = 9160


class Handler(http.server.BaseHTTPRequestHandler):
    """Stub Grafana. Requests are bucketed by the port they arrived on."""

    def log_message(self, *a):
        pass

    def _respond(self):
        n = int(self.headers.get("Content-Length") or 0)
        if n:
            self.rfile.read(n)
        port = self.server.server_address[1]
        bucket = SSRF_HITS if port == SSRF_PORT else CAPTURE
        with LOCK:
            bucket.append((self.path, {k.lower(): v for k, v in self.headers.items()}))
        body = json.dumps({"id": 1, "name": "Main Org.", "role": "Admin", "data": []}).encode()
        self.send_response(200)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    do_GET = do_POST = do_PUT = _respond


def mcp_rpc(payload, sid=None):
    base = f"http://127.0.0.1:{MCP_PORT}/mcp"
    cmd = ["curl", "-sS", "-m", "20", "-D", "/tmp/opencode/mg-h.txt",
           "-o", "/tmp/opencode/mg-b.txt", "-w", "%{http_code}", base,
           "-H", "Content-Type: application/json",
           "-H", "Accept: application/json, text/event-stream"]
    for k, v in (CALLER_HEADERS or {}).items():
        cmd += ["-H", f"{k}: {v}"]
    if sid:
        cmd += ["-H", f"Mcp-Session-Id: {sid}"]
    cmd += ["-d", json.dumps(payload)]
    code = subprocess.run(cmd, capture_output=True, text=True).stdout.strip()
    new_sid = sid
    for line in open("/tmp/opencode/mg-h.txt"):
        if line.lower().startswith("mcp-session-id:"):
            new_sid = line.split(":", 1)[1].strip()
    return code, new_sid, open("/tmp/opencode/mg-b.txt").read()


CALLER_HEADERS = {}
TOOL = "get_current_user"


def call_tool(tool=None, headers=None):
    tool = tool or TOOL
    """Full MCP handshake + tools/call with the given caller headers. Returns (code, detail, hdrs)."""
    global CALLER_HEADERS
    CALLER_HEADERS = headers or {}
    with LOCK:
        del CAPTURE[:]
        del SSRF_HITS[:]
    code, sid, body = mcp_rpc({"jsonrpc": "2.0", "id": 1, "method": "initialize", "params": {
        "protocolVersion": "2025-06-18", "capabilities": {},
        "clientInfo": {"name": "probe", "version": "0"}}})
    if code != "200":
        return code, "initialize failed: " + body[:200], [], body[:400]
    mcp_rpc({"jsonrpc": "2.0", "method": "notifications/initialized"}, sid=sid)
    code, _, body = mcp_rpc({"jsonrpc": "2.0", "id": 2, "method": "tools/call",
                             "params": {"name": tool, "arguments": {}}}, sid=sid)
    with LOCK:
        got = list(CAPTURE)
    first = got[0][1] if got else {}
    err = re.search(r'"(isError":true[^}]{0,120})', body)
    return code, (err.group(1) if err else "ok"), first, body[:400]


INTERESTING = ("authorization", "x-grafana-service-account-token", "x-grafana-api-key",
               "x-grafana-org-id", "x-grafana-id", "host", "user-agent")


def run_scenario(name, headers, marker=None):
    code, detail, hdrs, body = call_tool(headers=headers)
    with LOCK:
        ssrf = len(SSRF_HITS)
    print(f"\n--- {name}")
    print(f"    caller sends   : {json.dumps(headers) if headers else '(nothing)'}")
    print(f"    MCP http       : {code}   tool: {detail}")
    if hdrs:
        print("    Grafana saw    :")
        for k in INTERESTING:
            if k in hdrs:
                v = hdrs[k]
                if marker and marker in v:
                    v = v.replace(marker, f"[{marker}]")
                print(f"       {k}: {v}")
    else:
        print(f"    Grafana saw    : (no API call)   body: {body[:160]}")
    if ssrf:
        print(f"    !! {ssrf} request(s) reached the SSRF listener on {SSRF_PORT}")
    return hdrs


PREFERRED = ("get_current_user", "whoami", "list_teams", "get_instance_org",
             "search_dashboards", "list_datasources", "get_annotations")


def discover_tool():
    """tools/list, then choose a read-only tool that needs no arguments."""
    code, sid, body = mcp_rpc({"jsonrpc": "2.0", "id": 1, "method": "initialize", "params": {
        "protocolVersion": "2025-06-18", "capabilities": {},
        "clientInfo": {"name": "probe", "version": "0"}}})
    if code != "200":
        return None, "initialize failed"
    mcp_rpc({"jsonrpc": "2.0", "method": "notifications/initialized"}, sid=sid)
    code, sid, body = mcp_rpc({"jsonrpc": "2.0", "id": 2, "method": "tools/list"}, sid=sid)
    m = re.search(r'"result":\s*(\{.*\})\s*,?\s*"id":2', body)
    names = []
    try:
        data = json.loads(body)
        names = [t["name"] for t in data["result"]["tools"]]
    except Exception:
        pass
    for p in PREFERRED:
        if p in names:
            return p, f"{len(names)} tools"
    return (names[0] if names else None), f"{len(names)} tools"


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--image", default="docker.io/grafana/mcp-grafana:2.1.1")
    ap.add_argument("--runner", default="docker", help="docker or podman")
    ap.add_argument("--override", action="store_true",
                    help="start the server with --allow-grafana-url-override (no allowlist)")
    args = ap.parse_args()

    legit = http.server.ThreadingHTTPServer(("127.0.0.1", GRAFANA_PORT), Handler)
    legit.RequestHandlerClass.target = "legit"
    ssrf = http.server.ThreadingHTTPServer(("127.0.0.1", SSRF_PORT), Handler)
    # a distinct subclass so the handler knows which bucket to fill
    class SSRFHandler(Handler):
        target = "ssrf"
    ssrf.RequestHandlerClass = SSRFHandler
    for s in (legit, ssrf):
        threading.Thread(target=s.serve_forever, daemon=True).start()
    # patch: the legit server must use the legit handler
    legit.RequestHandlerClass = Handler

    env = ["-e", f"GRAFANA_URL=http://127.0.0.1:{GRAFANA_PORT}",
           "-e", "GRAFANA_FORWARD_HEADERS=Authorization",
           "-e", "GRAFANA_ALLOW_CROSS_ORIGIN_REDIRECTS=false"]
    extra = ["--allow-grafana-url-override"] if args.override else []
    runner = args.runner
    subprocess.run([runner, "rm", "-f", "mgprobe"], capture_output=True)
    cmd = [runner, "run", "--name", "mgprobe", "--rm", "--network", "host", *env, args.image,
           "-t", "streamable-http", "-address", f"127.0.0.1:{MCP_PORT}",
           "--allowed-hosts", "*", *extra]
    log = open("/tmp/opencode/mg.log", "w")
    subprocess.Popen(cmd, stdout=log, stderr=subprocess.STDOUT, start_new_session=True)
    up = False
    for _ in range(45):
        time.sleep(1)
        if re.search(rb"using StreamableHTTP transport", open("/tmp/opencode/mg.log", "rb").read()):
            up = True
            break
    if not up:
        print("mcp-grafana did not start; log tail:")
        print(open("/tmp/opencode/mg.log").read()[-2500:])
        return 1
    global TOOL
    TOOL, info = discover_tool()
    print(f"chosen tool: {TOOL!r} ({info})")

    print(f"mcp-grafana {args.image}: streamable-http on {MCP_PORT}, "
          f"GRAFANA_FORWARD_HEADERS=Authorization, url-override="
          f"{'ON' if args.override else 'OFF'}, Grafana stub on {GRAFANA_PORT}, "
          f"decoy on {SSRF_PORT}")

    JWT = "eyJhbGciOiJSUzI1NiJ9.CALLERJWT.sig"
    SA = "SA-SPOOF"
    ID = "FORGED-XGRAFANAID"

    run_scenario("1. delegation path: caller's own bearer token",
                 {"Authorization": f"Bearer {JWT}"}, marker=JWT)
    run_scenario("2. caller injects a service-account token header",
                 {"Authorization": f"Bearer {JWT}", "X-Grafana-Service-Account-Token": SA},
                 marker=SA)
    run_scenario("3. caller injects the deprecated API-key header",
                 {"Authorization": f"Bearer {JWT}", "X-Grafana-API-Key": SA}, marker=SA)
    run_scenario("4. caller injects an org-switch header",
                 {"Authorization": f"Bearer {JWT}", "X-Grafana-Org-Id": "7"})
    run_scenario("5. caller injects X-Grafana-Id (Grafana plugin id-token header)",
                 {"Authorization": f"Bearer {JWT}", "X-Grafana-Id": ID}, marker=ID)
    run_scenario("6. caller tries to retarget Grafana (X-Grafana-URL)",
                 {"Authorization": f"Bearer {JWT}",
                  "X-Grafana-URL": f"http://127.0.0.1:{SSRF_PORT}"})
    run_scenario("7. caller sends no credential at all", {})

    print("\n=== conclusion hints ===")
    print("Scenario 1 must show the caller's Authorization at Grafana for delegation to work.")
    print("Scenarios 2-6 showing anything at Grafana means the gateway MUST strip those headers.")
    print("Scenario 7 shows whether the backend fails closed without a caller credential.")

    subprocess.run([runner, "rm", "-f", "mgprobe"], capture_output=True)
    return 0


if __name__ == "__main__":
    sys.exit(main())
