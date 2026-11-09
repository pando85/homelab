#!/usr/bin/env python3
"""Stub authorization server for the helper's container tests.

Answers the token-exchange POSTs that ``files/mcp_gateway_token.py`` makes, and
records what it received so the test can prove *where* the workload credential
went: in the request body, never in a process command line.

Every token here is a literal fixture string. Nothing in this file is, or could
be, a real credential, and it is only ever addressed over a container-local
loopback port.

Usage (inside the test container)::

    python3 /fixture/fake_token_as.py --port 19443 --delay 3 --record /work/as.jsonl

``--delay`` holds each response open so the driver can sample ``/proc`` while the
exchange is genuinely in flight; without it the request would finish before the
sampler runs and a "no leak" result would prove nothing.
"""

from __future__ import annotations

import argparse
import http.server
import json
import threading
import time
import urllib.parse

_lock = threading.Lock()


class Handler(http.server.BaseHTTPRequestHandler):
    delay = 0.0
    record = ""

    def do_POST(self):  # noqa: N802
        length = int(self.headers.get("Content-Length") or 0)
        raw = self.rfile.read(length).decode("utf-8", "replace")
        form = dict(urllib.parse.parse_qsl(raw, keep_blank_values=True))
        scope = form.get("scope", "")
        entry = {
            "ts": time.time(),
            "path": self.path,
            "grant_type": form.get("grant_type"),
            "client_id": form.get("client_id"),
            "audience": form.get("audience"),
            "subject_token_type": form.get("subject_token_type"),
            "scope": scope,
            # Deliberately a predicate plus a prefix: the record file must not
            # hold a full copy of the credential it just received.
            "has_subject_token": bool(form.get("subject_token")),
            "subject_token_prefix": (form.get("subject_token") or "")[:8],
            "body_bytes": length,
        }
        with _lock:
            with open(self.record, "a", encoding="utf-8") as fh:
                fh.write(json.dumps(entry, separators=(",", ":")) + "\n")
                fh.flush()

        if self.delay:
            time.sleep(self.delay)

        # Compact separators on purpose: the sidecar scripts extract tokens with
        # grep, which would not match whitespace-padded JSON.
        body = json.dumps({
            "access_token": f"synthetic-access-token-for-{scope or 'none'}",
            "token_type": "Bearer",
            "expires_in": 900,
            "scope": scope,
        }, separators=(",", ":")).encode()
        self.send_response(200)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def log_message(self, *args):  # silent
        pass


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--port", type=int, required=True)
    ap.add_argument("--delay", type=float, default=0.0)
    ap.add_argument("--record", required=True)
    ns = ap.parse_args()
    Handler.delay = ns.delay
    Handler.record = ns.record
    srv = http.server.ThreadingHTTPServer(("127.0.0.1", ns.port), Handler)
    srv.serve_forever()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
