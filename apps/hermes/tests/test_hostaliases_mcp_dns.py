#!/usr/bin/env python3
"""Regression test for mcp.grigri.cloud DNS override via hostAliases.

The MCP gateway (mcp.grigri.cloud) resolves to the public IP 79.117.1.43 via
external DNS, but the external Envoy Gateway LoadBalancer listens on the
internal Cilium BGP IP 192.168.193.14. Without a DNS override, hermes pods
cannot reach the gateway (HTTPS connection times out).

This test verifies that hostAliases is correctly configured in defaultPodOptions
to override mcp.grigri.cloud -> 192.168.193.14 for both the hermes StatefulSet
and the hermes-mcp-gateway Deployment.

Run: ``python3 apps/hermes/tests/test_hostaliases_mcp_dns.py`` from anywhere.
"""

from __future__ import annotations

import os
import subprocess
import sys

REPO = subprocess.run(["git", "rev-parse", "--show-toplevel"], capture_output=True,
                      text=True, check=True).stdout.strip()
CHART = os.path.join(REPO, "apps", "hermes")

EXPECTED_IP = "192.168.193.14"
EXPECTED_HOSTNAME = "mcp.grigri.cloud"


def _helm(*extra: str) -> str:
    r = subprocess.run(["helm", "template", "--namespace", "hermes", "hermes", CHART, *extra],
                       capture_output=True, text=True)
    if r.returncode != 0:
        raise AssertionError(f"helm template failed: {r.stderr.strip()[:400]}")
    return r.stdout


def _get_pod_specs(manifest: str) -> dict[str, dict]:
    """Extract pod specs from StatefulSet and Deployment manifests."""
    import yaml
    pod_specs = {}
    for doc in yaml.safe_load_all(manifest):
        if not doc or not isinstance(doc, dict):
            continue
        kind = doc.get("kind")
        name = doc.get("metadata", {}).get("name", "")
        if kind in ("StatefulSet", "Deployment"):
            spec = doc.get("spec", {}).get("template", {}).get("spec", {})
            pod_specs[f"{kind}/{name}"] = spec
    return pod_specs


def test_hostaliases_present_in_main_pod():
    """hostAliases must be present in the hermes StatefulSet (main pod).

    The helper Deployment does not need hostAliases because it only reaches
    idm.grigri.cloud (already resolved via split-horizon DNS), not mcp.grigri.cloud.
    """
    manifest = _helm()
    pod_specs = _get_pod_specs(manifest)

    # Find the hermes StatefulSet
    sts_name = None
    for name in pod_specs:
        if "StatefulSet" in name and "hermes" in name:
            sts_name = name
            break

    assert sts_name is not None, "hermes StatefulSet not found"
    pod_spec = pod_specs[sts_name]
    host_aliases = pod_spec.get("hostAliases", [])
    assert host_aliases, f"{sts_name} missing hostAliases"

    # Find the entry for mcp.grigri.cloud
    mcp_entry = None
    for entry in host_aliases:
        if EXPECTED_HOSTNAME in entry.get("hostnames", []):
            mcp_entry = entry
            break

    assert mcp_entry is not None, (
        f"{sts_name} hostAliases missing entry for {EXPECTED_HOSTNAME}"
    )
    assert mcp_entry.get("ip") == EXPECTED_IP, (
        f"{sts_name} hostAliases IP mismatch: "
        f"expected {EXPECTED_IP}, got {mcp_entry.get('ip')}"
    )


def test_hostaliases_ip_matches_external_ingress_lb():
    """The hostAliases IP must match the external Envoy Gateway LoadBalancer IP."""
    import yaml

    # The external Gateway's data plane Service IP is set on its EnvoyProxy resource
    envoyproxy = os.path.join(REPO, "system", "envoy-gateway", "resources", "envoyproxy-external.yaml")
    with open(envoyproxy) as f:
        docs = [d for d in yaml.safe_load_all(f) if d]
    proxy = next(d for d in docs if d.get("kind") == "EnvoyProxy" and d["metadata"]["name"] == "external")

    lb_ip = (proxy.get("spec", {})
             .get("provider", {})
             .get("kubernetes", {})
             .get("envoyService", {})
             .get("annotations", {})
             .get("lbipam.cilium.io/ips"))

    assert lb_ip == EXPECTED_IP, (
        f"external Envoy Gateway LB IP changed: expected {EXPECTED_IP}, got {lb_ip}. "
        f"Update hostAliases in apps/hermes/values.yaml to match."
    )


def test_hostaliases_scoped_to_main_pod():
    """hostAliases must be in the hermes StatefulSet, not in the helper Deployment."""
    manifest = _helm()
    pod_specs = _get_pod_specs(manifest)

    # Find the hermes StatefulSet and helper Deployment
    sts_name = None
    dep_name = None
    for name in pod_specs:
        if "StatefulSet" in name and "hermes" in name:
            sts_name = name
        elif "Deployment" in name and "hermes" in name:
            dep_name = name

    assert sts_name is not None, "hermes StatefulSet not found"
    assert dep_name is not None, "hermes-mcp-gateway Deployment not found"

    # Main pod should have hostAliases
    sts_spec = pod_specs[sts_name]
    host_aliases = sts_spec.get("hostAliases", [])
    assert host_aliases, f"{sts_name} should have hostAliases"

    # Helper Deployment should NOT have hostAliases (it doesn't reach mcp.grigri.cloud)
    dep_spec = pod_specs[dep_name]
    dep_host_aliases = dep_spec.get("hostAliases", [])
    assert not dep_host_aliases, (
        f"{dep_name} should not have hostAliases (helper only reaches idm.grigri.cloud)"
    )


def test_hostaliases_does_not_affect_other_charts():
    """Verify hostAliases is not added to unrelated charts (spot check)."""
    import yaml

    # Check a few other charts to ensure they don't have this hostAliases
    test_charts = ["apps/mcp-gateway", "apps/readest", "system/monitoring"]

    for chart_path in test_charts:
        full_path = os.path.join(REPO, chart_path)
        if not os.path.isdir(full_path):
            continue

        r = subprocess.run(
            ["helm", "template", "--namespace", "test", "test", full_path],
            capture_output=True, text=True
        )
        if r.returncode != 0:
            continue  # Chart may have dependencies or other issues

        for doc in yaml.safe_load_all(r.stdout):
            if not doc or not isinstance(doc, dict):
                continue
            kind = doc.get("kind")
            if kind in ("StatefulSet", "Deployment", "DaemonSet"):
                pod_spec = doc.get("spec", {}).get("template", {}).get("spec", {})
                host_aliases = pod_spec.get("hostAliases", [])
                for entry in host_aliases:
                    assert EXPECTED_HOSTNAME not in entry.get("hostnames", []), (
                        f"{chart_path} has unexpected hostAliases for {EXPECTED_HOSTNAME}"
                    )


if __name__ == "__main__":
    import traceback

    tests = [
        test_hostaliases_present_in_main_pod,
        test_hostaliases_ip_matches_external_ingress_lb,
        test_hostaliases_scoped_to_main_pod,
        test_hostaliases_does_not_affect_other_charts,
    ]

    failed = 0
    for test in tests:
        try:
            test()
            print(f"✓ {test.__name__}")
        except AssertionError as e:
            print(f"✗ {test.__name__}: {e}")
            failed += 1
        except Exception as e:
            print(f"✗ {test.__name__}: unexpected error")
            traceback.print_exc()
            failed += 1

    sys.exit(1 if failed else 0)
