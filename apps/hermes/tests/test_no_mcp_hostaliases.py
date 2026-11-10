#!/usr/bin/env python3
"""Guard: no pod pins mcp.grigri.cloud through hostAliases.

mcp.grigri.cloud used to be pinned to the external Envoy Gateway LoadBalancer IP
with ``hostAliases`` because the name was missing from the internal DNS. pfSense
now has a DNS host override for it, so pods resolve it through
CoreDNS/nodelocaldns like every other internal name. A hostAliases pin would
silently go stale if the LoadBalancer IP ever changes.

The workaround was removed in commit 78ae995ee ("hermes: Remove the mcp.grigri.cloud
hostAliases workaround"). This test ensures it is not re-introduced.

Run: ``python3 apps/hermes/tests/test_no_mcp_hostaliases.py`` from anywhere.
"""

from __future__ import annotations

import os
import subprocess
import sys

REPO = subprocess.run(["git", "rev-parse", "--show-toplevel"], capture_output=True,
                      text=True, check=True).stdout.strip()
CHART = os.path.join(REPO, "apps", "hermes")

HOSTNAME = "mcp.grigri.cloud"


def _render(*extra: str) -> str:
    r = subprocess.run(["helm", "template", "--namespace", "hermes", "hermes", CHART, *extra],
                       capture_output=True, text=True)
    if r.returncode != 0:
        raise AssertionError(f"helm template failed: {r.stderr.strip()[:400]}")
    return r.stdout


def _pin_offenders(manifest: str) -> list[str]:
    import yaml
    offenders = []
    for doc in yaml.safe_load_all(manifest):
        if not isinstance(doc, dict) or doc.get("kind") not in ("StatefulSet", "Deployment"):
            continue
        spec = doc.get("spec", {}).get("template", {}).get("spec", {})
        for entry in spec.get("hostAliases", []) or []:
            if HOSTNAME in entry.get("hostnames", []):
                offenders.append(f"{doc['kind']}/{doc['metadata']['name']} -> {entry.get('ip')}")
    return offenders


def test_no_hostaliases_pin_mcp_hostname():
    for flag in ("false", "true"):
        offenders = _pin_offenders(_render("--set", f"global.mcpGateway.enabled={flag}"))
        assert not offenders, (
            f"{HOSTNAME} must resolve via DNS (pfSense host override), not hostAliases: {offenders}"
        )


if __name__ == "__main__":
    import traceback

    failed = 0
    for test in (test_no_hostaliases_pin_mcp_hostname,):
        try:
            test()
            print(f"✓ {test.__name__}")
        except Exception:
            print(f"✗ {test.__name__}")
            traceback.print_exc()
            failed += 1
    sys.exit(1 if failed else 0)
