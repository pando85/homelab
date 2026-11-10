#!/usr/bin/env python3
"""Asserts the legacy Grafana MCP sidecar has been removed from the chart.

The sidecar (``grafana-mcp``, ``grafana-mcp-refresher``,
``grafana-mcp-token-init``) and its shared volume (``grafana-mcp-shared``)
were removed on 2026-11-10. Hermes now reaches Grafana through the central
MCP gateway at ``https://mcp.grigri.cloud/grafana/mcp`` using OAuth (server
name ``homelab-grafana``, scope ``mcp_grafana``), with caller-preserving
delegation. The gateway path is validated by
``test/mcp-gateway/hermes-live-acceptance.py``.

This test ensures the sidecar does not silently return: no container,
initContainer, volume, or ``/shared`` mount that belonged to the legacy
path may appear in the rendered StatefulSet.

Run: ``python3 apps/hermes/tests/test_cutover_sidecar_probe.py``.
"""

from __future__ import annotations

import os
import subprocess

REPO = subprocess.run(["git", "rev-parse", "--show-toplevel"], capture_output=True,
                      text=True, check=True).stdout.strip()
CHART = os.path.join(REPO, "apps", "hermes")


def _helm(*extra: str) -> str:
    r = subprocess.run(["helm", "template", "--namespace", "hermes", "hermes", CHART, *extra],
                       capture_output=True, text=True)
    if r.returncode != 0:
        raise AssertionError(f"helm template failed: {r.stderr.strip()[:400]}")
    return r.stdout


def _sts_pod(manifest: str) -> dict:
    import yaml
    sts = next(d for d in yaml.safe_load_all(manifest)
               if isinstance(d, dict) and d.get("kind") == "StatefulSet")
    return sts["spec"]["template"]["spec"]


def test_sidecar_containers_are_absent():
    """The legacy grafana-mcp sidecar containers must not be rendered."""
    pod = _sts_pod(_helm())
    container_names = {c["name"] for c in pod.get("containers", [])}
    init_names = {c["name"] for c in pod.get("initContainers", [])}
    for name in ("grafana-mcp", "grafana-mcp-refresher"):
        assert name not in container_names, f"{name} still present in containers"
    assert "grafana-mcp-token-init" not in init_names, \
        "grafana-mcp-token-init still present in initContainers"
    print("✓ sidecar containers absent: grafana-mcp, grafana-mcp-refresher, grafana-mcp-token-init")


def test_shared_volume_is_absent():
    """The grafana-mcp-shared emptyDir and /shared mounts must not be rendered."""
    import yaml
    manifest = _helm()
    # Check the rendered YAML text for the volume name and mount path
    sts = next(d for d in yaml.safe_load_all(manifest)
               if isinstance(d, dict) and d.get("kind") == "StatefulSet")
    volumes = sts["spec"]["template"]["spec"].get("volumes", [])
    volume_names = {v.get("name") for v in volumes}
    assert "grafana-mcp-shared" not in volume_names, \
        "grafana-mcp-shared volume still present"
    # Also check no container mounts /shared (the legacy sidecar path)
    for c in pod_containers(sts):
        for mount in c.get("volumeMounts", []):
            assert mount["mountPath"] != "/shared", \
                f"{c['name']} still mounts /shared"
    print("✓ grafana-mcp-shared volume and /shared mounts absent")


def test_gateway_entries_are_present():
    """The gateway MCP server entries must be present in the ConfigMap."""
    import json
    import yaml
    manifest = _helm()
    cm = next(d for d in yaml.safe_load_all(manifest)
              if isinstance(d, dict) and d.get("kind") == "ConfigMap"
              and d["metadata"]["name"] == "hermes-mcp-gateway")
    cfg = json.loads(cm["data"]["config.json"])
    assert "homelab-grafana" in cfg["servers"], "homelab-grafana route missing"
    assert "homelab-context7" in cfg["servers"], "homelab-context7 route missing"
    assert cfg["servers"]["homelab-grafana"]["url"] == "https://mcp.grigri.cloud/grafana/mcp"
    assert cfg["servers"]["homelab-grafana"]["oauth"]["scope"] == "mcp_grafana"
    print("✓ gateway entries present: homelab-grafana, homelab-context7")


def pod_containers(sts: dict) -> list:
    """All containers (init + regular) in the StatefulSet pod."""
    pod = sts["spec"]["template"]["spec"]
    return pod.get("initContainers", []) + pod.get("containers", [])


if __name__ == "__main__":
    print("Running sidecar removal assertions...")
    for fn in [test_sidecar_containers_are_absent,
               test_shared_volume_is_absent,
               test_gateway_entries_are_present]:
        fn()
    print("\n✓ All sidecar removal assertions passed")
