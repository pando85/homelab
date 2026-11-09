#!/usr/bin/env python3
"""Probes the rendered Grafana-JWT sidecar scripts against a fixture issuer.

This is the evidence for the paired cutover: the SAME script text that
``helm template`` produces for the existing ``grafana-mcp-token-init`` container
is executed inside ``curlimages/curl:8.23.0`` (the image that container actually
uses) against a loopback fixture that answers a token exchange. Nothing talks to
Kanidm, Grafana or the cluster; every token here is a literal fixture string.

Measured, not asserted from reading:

* switch OFF  -> writes the ``id_token`` (legacy ``audience=grafana`` behaviour
  preserved verbatim) and the subject token DOES appear in a process command
  line, which is the exposure the baseline carries with ``-d subject_token=...``;
* switch ON   -> writes the gateway ``access_token`` for
  ``client_id=audience=mcp-gateway``, ``scope=mcp_grafana``, and the subject
  token appears in the POST body and in NO command line.

Only the init container is executed: the refresher body is the same exchange
wrapped in ``while true; do sleep 600; ... done``, which the render test compares
as text; running it would just add a ten-minute sleep to a test.

Run: ``python3 apps/hermes/tests/test_cutover_sidecar_probe.py``.
"""

from __future__ import annotations

import http.server
import json
import os
import subprocess
import sys
import tempfile
import threading
import time

REPO = subprocess.run(["git", "rev-parse", "--show-toplevel"], capture_output=True,
                      text=True, check=True).stdout.strip()
CHART = os.path.join(REPO, "apps", "hermes")
CURL_IMAGE = "curlimages/curl:8.23.0"
SUBJECT = "SYNTHETIC-SUBJECT-TOKEN-DO-NOT-USE"      # fixture, never a real credential
ID_TOKEN = "synthetic-id-token-aud-grafana"
ACCESS_TOKEN = "synthetic-access-token-aud-mcp-gateway"
FLAG = "global.mcpGateway.enabled"

_capture: list[dict] = []


class _FixtureAS(http.server.BaseHTTPRequestHandler):
    def do_POST(self):  # noqa: N802
        import urllib.parse
        raw = self.rfile.read(int(self.headers.get("Content-Length") or 0)).decode()
        form = dict(urllib.parse.parse_qsl(raw))
        _capture.append(form)
        # Hold the request open so the driver can sample /proc mid-flight.
        time.sleep(1.5)
        # Compact separators: the sidecar scripts extract with
        # grep -o '"id_token":"[^"]*"', which would not match spaced JSON.
        body = json.dumps({
            "access_token": ACCESS_TOKEN,
            "id_token": ID_TOKEN,
            "token_type": "Bearer",
            "expires_in": 900,
            "scope": form.get("scope", ""),
        }, separators=(",", ":")).encode()
        self.send_response(200)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def log_message(self, *_a):
        pass


def _start_as() -> tuple[http.server.ThreadingHTTPServer, int]:
    global _capture
    _capture = []
    srv = http.server.ThreadingHTTPServer(("127.0.0.1", 0), _FixtureAS)
    threading.Thread(target=srv.serve_forever, daemon=True).start()
    return srv, srv.server_address[1]


ISSUER = "https://idm.grigri.cloud/oauth2/token"


def _script(name: str, enabled: bool, port: int) -> str:
    """The rendered ``args`` of one sidecar container, issuer pointed at loopback.

    Both branches address the real production issuer in git (the legacy text by
    literal, the gateway text through ``global.mcpGateway.exchange.url``). The
    probe substitutes that single URL for the fixture, and asserts it was the
    only substitution, so nothing here ever sends a request to Kanidm - a live
    failed exchange would still be a live call to production.
    """
    import yaml
    extra = ["--set", f"{FLAG}=true"] if enabled else ["--set", f"{FLAG}=false"]
    out = subprocess.run(["helm", "template", "--namespace", "hermes", "hermes", CHART, *extra],
                         capture_output=True, text=True, check=True).stdout
    sts = next(d for d in yaml.safe_load_all(out) if isinstance(d, dict) and d.get("kind") == "StatefulSet")
    pod = sts["spec"]["template"]["spec"]
    for cont in pod["initContainers"] + pod["containers"]:
        if cont["name"] == name:
            script = cont["args"][0]
            assert script.count(ISSUER) == 1, "expected exactly one issuer URL to isolate"
            return script.replace(ISSUER, f"http://127.0.0.1:{port}/token")
    raise AssertionError(f"{name} not rendered")


def _run(script: str, workdir: str) -> dict:
    """Execute the script as the sidecar does: uid 10000, /shared writable."""
    os.makedirs(os.path.join(workdir, "shared"), exist_ok=True)
    os.chmod(os.path.join(workdir, "shared"), 0o777)
    os.chmod(workdir, 0o777)  # the container writes its sampling output here
    with open(os.path.join(workdir, "init.sh"), "w", encoding="utf-8") as fh:
        fh.write(script)
    with open(os.path.join(workdir, "env"), "w", encoding="utf-8") as fh:
        fh.write(f"KANIDM_TOKEN={SUBJECT}\n")
    os.chmod(os.path.join(workdir, "env"), 0o600)
    # The scripts reference the absolute /shared path the chart mounts there.
    # Directly capture the argv of the ``curl`` process of interest, polled while
    # the fixture holds the request open (1.5s). Sampling every process in the
    # container once is timing-sensitive; ``pidof curl`` over 4s is not, and it
    # is the process whose command line an attacker would read through
    # /proc/<pid>/cmdline.
    driver = ("nohup sh /fixture/init.sh >/fixture/out.log 2>&1 & "
              "i=0; while [ $i -lt 40 ]; do i=$((i+1)); "
              "for pid in $(pidof curl 2>/dev/null); do "
              "[ -r /proc/$pid/cmdline ] || continue; "
              "tr '\\0' ' ' < /proc/$pid/cmdline >> /fixture/cmdlines.txt; "
              "echo >> /fixture/cmdlines.txt; done; "
              "sleep 0.1; done; "
              "sleep 1; "
              "cat /shared/grafana-jwt; echo; cat /fixture/cmdlines.txt 2>/dev/null; echo RC-DONE")
    r = subprocess.run(["docker", "run", "--rm", "--network", "host",
                        "--user", "10000:10000", "--entrypoint", "/bin/sh",
                        "--env-file", os.path.join(workdir, "env"),
                        "-v", f"{workdir}:/fixture",
                        "-v", f"{workdir}/shared:/shared",
                        "-v", "/etc/resolv.conf:/etc/resolv.conf:ro",
                        CURL_IMAGE, "-c", driver],
                       capture_output=True, text=True, timeout=180)
    out = r.stdout + r.stderr
    assert "RC-DONE" in out, out[-600:]
    body = out.split("RC-DONE")[0]
    token, _, cmdlines = body.partition("\n")
    return {"token": token.strip(), "cmdlines": cmdlines}


def test_legacy_off_writes_id_token_and_leaks_argv():
    """Baseline behaviour is preserved verbatim - including its argv exposure."""
    srv, port = _start_as()
    try:
        res = _run(_script("grafana-mcp-token-init", enabled=False, port=port),
                   tempfile.mkdtemp(prefix="cutover-off-"))
        assert res["token"] == ID_TOKEN, res["token"]
        assert _capture and _capture[0].get("audience") == "grafana", _capture
        assert "curl" in res["cmdlines"], "never captured the curl process"
        assert SUBJECT in res["cmdlines"], "expected the documented baseline argv exposure"
        print("✓ off: id_token written, audience=grafana, credential visible in cmdline (baseline)")
    finally:
        srv.shutdown()


def test_enabled_writes_gateway_access_token_without_argv():
    """Cutover: gateway client + access token, credential never in a cmdline."""
    srv, port = _start_as()
    try:
        res = _run(_script("grafana-mcp-token-init", enabled=True, port=port),
                   tempfile.mkdtemp(prefix="cutover-on-"))
        assert res["token"] == ACCESS_TOKEN, res["token"]
        assert len(_capture) == 1, _capture
        form = _capture[0]
        assert form["client_id"] == form["audience"] == "mcp-gateway", form
        assert form["scope"] == "mcp_grafana", form
        assert form["grant_type"] == "urn:ietf:params:oauth:grant-type:token-exchange", form
        assert form["subject_token"] == SUBJECT, "credential must reach the issuer"
        assert "curl" in res["cmdlines"], "never captured the curl process"
        assert "--data-binary @-" in res["cmdlines"], "curl did not read the body from stdin"
        assert SUBJECT not in res["cmdlines"], "credential leaked into /proc/<pid>/cmdline"
        assert not [ln for ln in res["cmdlines"].splitlines() if "-d " in ln], \
            f"curl still takes -d arguments: {res['cmdlines']}"
        print("✓ on: access_token written for mcp-gateway/mcp_grafana, zero cmdline hits")
    finally:
        srv.shutdown()


def test_refresher_body_tracks_the_init_body():
    """The refresher's exchange must move in lockstep with the init container's.

    Compares the token-exchange lines only (the init container additionally
    ``chmod``s the file, which is baseline behaviour and not part of the
    exchange). If these ever diverge between the two containers, one of them
    would keep the old issuer while the other moved on.
    """
    srv, port = _start_as()
    try:
        for enabled in (False, True):
            init = _script("grafana-mcp-token-init", enabled, port)
            ref = _script("grafana-mcp-refresher", enabled, port)
            body = "\n".join(l for l in ref.splitlines() if "sleep 600" not in l
                             and "while true" not in l and "done" != l.strip())
            core = "\n".join(sorted(l.strip() for l in body.splitlines() if l.strip().startswith(("RESPONSE", "| curl", "TOKEN", "JWT", "echo", "-d "))))
            core_init = "\n".join(sorted(l.strip() for l in init.splitlines() if l.strip().startswith(("RESPONSE", "| curl", "TOKEN", "JWT", "echo", "-d "))))
            assert core == core_init, f"paired cutover drift at enabled={enabled}"
        print("✓ init and refresher bodies move together in both states")
    finally:
        srv.shutdown()


if __name__ == "__main__":
    if subprocess.run(["docker", "image", "inspect", CURL_IMAGE], capture_output=True).returncode != 0:
        print(f"SKIP: {CURL_IMAGE} not available locally")
        sys.exit(0)
    print("Running cutover sidecar probes (fixture issuer on loopback)...")
    for fn in [test_legacy_off_writes_id_token_and_leaks_argv,
               test_enabled_writes_gateway_access_token_without_argv,
               test_refresher_body_tracks_the_init_body]:
        fn()
    print("\n✓ All cutover probes passed")
