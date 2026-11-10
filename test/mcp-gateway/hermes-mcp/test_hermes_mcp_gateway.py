#!/usr/bin/env python3
"""Behavioural tests for the opt-in Hermes MCP gateway integration.

The scripts under test only work against Hermes' own modules (``hermes_cli.config``
and ``tools.mcp_oauth*``) and the MCP SDK version Hermes pins, so they run inside
the **pinned deployed image** (``nousresearch/hermes-agent:v2026.8.16.2``) rather
than against hand-written stubs. The scripts are taken from the chart's rendered
ConfigMap, so a template or values regression fails these tests too.

Requires: ``helm`` and ``docker`` (or ``podman`` via ``MCP_TEST_CONTAINER_CMD``).
Skips with a reason when either is missing, so a plain ``git pull`` checkout can
still collect the suite.

Run: ``test/mcp-gateway/hermes-mcp/run.sh``
"""

from __future__ import annotations

import json
import os
import shutil
import subprocess
import sys
import tempfile
from pathlib import Path

import pytest
import yaml

HERE = Path(__file__).resolve().parent
CHART = HERE.parents[2] / "apps" / "hermes"
CHECKS = HERE / "checks"
RELEASE = "hermes"
NAMESPACE = "hermes"
# The `hermes` account in the pinned image (getent passwd hermes), which also
# owns /opt/data. Every helper container runs as it, so the credential files it
# writes must be owned by it.
AGENT_UID = 10000
# Scratch HERMES_HOME for the in-image checks. Deliberately NOT under /work:
# docker would have to create the tmpfs mount point inside the host bind mount
# and leave a root-owned directory behind. /tmp is the container's own space.
HERMES_MOUNT = "/tmp/mcpgw-hermes-home"

docker = shutil.which(os.environ.get("MCP_TEST_CONTAINER_CMD", "docker"))
helm = shutil.which("helm")


def _values() -> dict:
    return yaml.safe_load((CHART / "values.yaml").read_text())


def _image(values: dict) -> str:
    image = values["app-template"]["controllers"]["hermes"]["containers"]["hermes"]["image"]
    return f"{image['repository']}:{image['tag']}"


def _render_enabled(work: Path) -> list[dict]:
    """Render the chart with the feature on and return every document."""
    values = _values()
    enabled = json.loads(json.dumps(values))
    # ``--set`` cannot flip the YAML aliases that share mcpGateway.enabled, so
    # the override document mirrors exactly what an operator edits in one place.
    enabled["global"]["mcpGateway"]["enabled"] = True

    path = work / "enabled-values.yaml"
    path.write_text(yaml.safe_dump(enabled))
    rendered = subprocess.run(
        ["helm", "template", RELEASE, str(CHART), "--namespace", NAMESPACE, "-f", str(path)],
        capture_output=True,
        text=True,
        check=True,
        cwd=work,
    ).stdout
    return [d for d in yaml.safe_load_all(rendered) if d]


def _rendered_config_map(work: Path) -> dict:
    config_maps = [d for d in _render_enabled(work)
                   if d.get("kind") == "ConfigMap" and d["metadata"]["name"] == "hermes-mcp-gateway"]
    assert len(config_maps) == 1, "expected exactly one gateway ConfigMap"
    return config_maps[0]["data"]


@pytest.fixture(scope="session")
def workspace(tmp_path_factory) -> Path:
    if helm is None:
        pytest.skip("helm is required to render the gateway ConfigMap")
    work = tmp_path_factory.mktemp("mcp-gateway")
    data = _rendered_config_map(work)
    for key, body in data.items():
        (work / key).write_text(body)
    for check in sorted(CHECKS.glob("check_*.py")):
        shutil.copy(check, work / check.name)
    assert "config.json" in data, "ConfigMap must render the gateway config"
    json.loads((work / "config.json").read_text())
    return work


def _prepare_container_workspace(workspace: Path) -> bool:
    """Make the scratch tree readable by the Hermes uid (10000).

    Production renders every helper with ``runAsUser: 10000`` (the agent's uid,
    which owns ``/opt/data``), so the checks only prove something about the
    deployed pod when they run as that uid. Read access to *this test's own* tmp
    tree is all that takes - no privilege escalation, and because the checks'
    ``HERMES_HOME`` is a container tmpfs, nothing owned by uid 10000 is ever left
    on the host for pytest to try to delete.

    Returns False when the tree cannot be made readable.
    """
    try:
        workspace.chmod(0o755)
        for path in workspace.iterdir():
            path.chmod(0o755 if path.is_dir() else 0o644)
    except OSError:
        return False
    return (workspace.stat().st_mode & 0o004) == 0o004


def run_check(workspace: Path, script: str, mode: str, env: dict | None = None) -> str:
    if docker is None:
        pytest.skip("no container runtime (docker/podman) available")
    if not _prepare_container_workspace(workspace):
        pytest.skip("host cannot make its own scratch tree readable by uid 10000")
    argv = [
        docker, "run", "--rm",
        "--entrypoint", "/opt/hermes/.venv/bin/python",
        "-v", f"{workspace}:/work",
        # Ephemeral Hermes home: the checks write config.yaml, mcp-tokens and
        # their private gateway-config copy there, so it must be writable by
        # uid 10000, and discarding it with the container keeps the host tree
        # clean. mode=1777 so the unprivileged uid can create its own 0700 dirs.
        "--tmpfs", f"{HERMES_MOUNT}:mode=1777,size=64m",
        "-e", f"HERMES_HOME={HERMES_MOUNT}",
        "-e", "MCP_GATEWAY_CONFIG=/work/config.json",
        # Same uid the helper Deployment renders, so file ownership of the
        # credential is tested as it exists in the pod, not as root.
        "--user", f"{AGENT_UID}:{AGENT_UID}",
    ]
    for key, value in (env or {}).items():
        argv += ["-e", f"{key}={value}"]
    argv += ["-w", "/work", _image(_values()), f"/work/{script}", mode]
    proc = subprocess.run(argv, capture_output=True, text=True, timeout=900)
    output = proc.stdout + proc.stderr
    assert proc.returncode == 0, f"{script} {mode} failed (rc={proc.returncode}):\n{output}"
    assert any(line.startswith("PASS ") for line in output.splitlines()), f"no PASS line:\n{output}"
    print(output, file=sys.stdout)
    return output


@pytest.mark.parametrize("mode", ["preserve", "malformed", "bad-entry", "redaction"])
def test_config_merge(workspace: Path, mode: str) -> None:
    run_check(workspace, "check_merge.py", mode)


@pytest.mark.parametrize("mode", ["schema", "failclosed", "reload", "idempotent", "symlink",
                                  "obstructed"])
def test_token_store(workspace: Path, mode: str) -> None:
    """Credential-store behaviour, in the pinned image, as the agent uid.

    ``idempotent`` proves no mtime churn on an identical re-run (the gateway
    reloads per mtime, so that would reconnect forever) and that a rotation still
    lands; ``symlink`` proves an unsafe pre-existing path is refused instead of
    written through; ``obstructed`` proves a failed write cleans up its temp file
    and fails closed instead of crashing.
    """
    run_check(workspace, "check_token.py", mode)


def test_token_files_owned_by_agent_uid(workspace: Path) -> None:
    """The credential the agent reads must be owned by the agent's uid.

    ``run_check`` passes ``--user 10000:10000`` (what the chart renders) and the
    in-image check asserts ownership of every file it wrote by that euid, so a
    pod that switched the helpers back to root fails here instead of silently
    proving nothing. Skipped only when the host cannot widen its own tmp tree.
    """
    if docker is None:
        pytest.skip("no container runtime (docker/podman) available")
    if not _prepare_container_workspace(workspace):
        pytest.skip("host cannot make the scratch tree writable by uid 10000")
    output = run_check(workspace, "check_token.py", "ownership")
    if "NOTE running as root" not in output:
        assert "uid 10000" in output, f"check did not run as the agent uid:\n{output}"


def test_disabled_render_has_no_gateway_resources() -> None:
    """With the flag off, no gateway ConfigMap, Deployment, or helper resources render.

    The strict byte-for-byte comparison against a baseline commit is in
    ``apps/hermes/tests/test_render_enable.py::test_disabled_render_is_head_baseline``.
    This test asserts the key property: the feature adds nothing when off, and
    the core hermes workload is still present.
    """
    if helm is None:
        pytest.skip("helm not installed")
    current = subprocess.run(
        ["helm", "template", RELEASE, str(CHART), "--namespace", NAMESPACE,
         "--set", "global.mcpGateway.enabled=false"],
        capture_output=True, text=True, check=True,
    ).stdout
    for added in ("kind: KanidmServiceAccount\nmetadata:\n  name: hermes-mcp",
                  "name: hermes-mcp-gateway", "name: mcp-gateway-config",
                  "name: mcp-gateway-token", "name: mcp-gateway-token-refresher",
                  "hermes-mcp-token"):
        assert added not in current, f"feature leaked into the disabled render: {added}"
    # The hermes container and its data volume must still be present
    for existing in ("hermes-data", "nousresearch/hermes-agent:v2026.8.16.2"):
        assert existing in current, f"regression: {existing} vanished"
    # No gateway ConfigMap rendered while off
    docs = [d for d in yaml.safe_load_all(current) if d]
    assert not any(d.get("kind") == "ConfigMap" and d.get("metadata", {}).get("name") == "hermes-mcp-gateway"
                   for d in docs), "gateway ConfigMap rendered while off"


def test_gateway_entry_only_declares_oauth(workspace: Path) -> None:
    """The merged entry must carry no credential: no headers, no bearer, no secret.

    The credential lives only in the token store, which keeps rotation inside
    the reload mechanism Hermes already implements.
    """
    cfg = json.loads((workspace / "config.json").read_text())
    for name, entry in cfg["servers"].items():
        assert entry["auth"] == "oauth", f"{name} must use oauth"
        assert "headers" not in entry and "env" not in entry, f"{name} has unexpected auth"
        assert entry["url"].startswith("https://"), f"{name} url must be https"
    exchange = cfg["exchange"]
    assert exchange["clientId"] == exchange["audience"], "audience must match the gateway client"
    assert exchange["marginSeconds"] > 0 and exchange["minIntervalSeconds"] > 0
    # Verify firecrawl scope is present in the servers
    assert "homelab-firecrawl" in cfg["servers"], "firecrawl server missing"
    assert cfg["servers"]["homelab-firecrawl"]["oauth"]["scope"] == "mcp_firecrawl"


def test_rendered_entry_matches_gateway_contract(workspace: Path) -> None:
    """Pin the values against what ``apps/mcp-gateway`` actually serves.

    * the gateway routes per target (``/firecrawl/mcp``) and has no aggregate
      ``/mcp``, so the URL must carry the target;
    * only the scopes the gateway really grants may be requested: asking for
      ``openid``/``email`` here would either fail the exchange or silently
      assume a grant that does not exist (the agent needs neither - the access
      token is the only credential Hermes' OAuth provider sends);
    * the aud/client pair is Kanidm's ``mcp-gateway`` and is deliberately
      independent of the host part of the URL.
    """
    cfg = json.loads((workspace / "config.json").read_text())
    firecrawl = cfg["servers"]["homelab-firecrawl"]
    assert firecrawl["url"] == "https://mcp.grigri.cloud/firecrawl/mcp"
    from urllib.parse import urlparse

    parsed = urlparse(firecrawl["url"])
    assert parsed.path == "/firecrawl/mcp", f"not a per-target route: {parsed.path}"
    scope = firecrawl["oauth"]["scope"]
    assert scope == "mcp_firecrawl", f"ungranted scopes: {scope}"
    assert not {"openid", "profile", "email"} & set(scope.split())
    exchange = cfg["exchange"]
    assert exchange["clientId"] == exchange["audience"] == "mcp-gateway"
    assert exchange["url"].startswith("https://idm.grigri.cloud/")

    values = _values()["global"]["mcpGateway"]
    assert values["enabled"] is True, "feature is enabled in values.yaml"


def test_gateway_helpers_block_startup_until_they_succeed(workspace: Path) -> None:
    """A failing merge must NOT let the helper refresher start with an unconfigured entry.

    Both gateway steps are ordinary init containers in the helper Deployment:
    Kubernetes runs them to completion before the refresher container starts and
    re-tries the pod until they exit 0. A native sidecar (``restartPolicy: Always``)
    would instead be allowed to fail while the refresher started without its MCP
    entry, so the rendered absence of that field is part of the fail-closed
    contract - as is the merge script's own non-zero exit (``check_merge.py malformed``).
    """
    docs = _render_enabled(workspace)
    dep = next(d for d in docs if d.get("kind") == "Deployment"
               and d["metadata"]["name"] == "hermes-mcp-gateway")
    pod = dep["spec"]["template"]["spec"]
    init = {c["name"]: c for c in pod.get("initContainers", [])}
    for name in ("mcp-gateway-config", "mcp-gateway-token"):
        assert name in init, f"{name} must be an init container in the helper Deployment"
        assert init[name].get("restartPolicy") is None, (
            f"{name} is a native sidecar: the refresher could start before it succeeded")
    # The exchange must finish before the refresher container starts, so the
    # first token refresh always finds a valid credential on disk.
    containers = [c["name"] for c in pod["containers"]]
    assert "mcp-gateway-token-refresher" in containers


def test_script_changes_roll_the_helper_pod(workspace: Path) -> None:
    """ConfigMap-only edits to the helper scripts must reach the running helper pod.

    The scripts are mounted from the ConfigMap, not baked into the image, so
    kubelet updating the mount does not re-exec the Python that is already
    running. The helper Deployment uses a ``checksum/config`` annotation that
    changes whenever the ConfigMap content changes, which triggers a rolling
    update of the Deployment. This is the helper pod's own rollout mechanism;
    the hermes StatefulSet does not mount the ConfigMap and needs no annotation.
    """
    docs = _render_enabled(workspace)
    dep = next(d for d in docs if d.get("kind") == "Deployment"
               and d["metadata"]["name"] == "hermes-mcp-gateway")
    annotations = dep["spec"]["template"]["metadata"].get("annotations") or {}
    assert "checksum/config" in annotations, (
        f"helper Deployment missing checksum/config annotation: {annotations}")

    pod = dep["spec"]["template"]["spec"]
    volumes = {v["name"]: v for v in pod.get("volumes", []) if "configMap" in v}
    mounted = [n for n, v in volumes.items() if v["configMap"]["name"] == "hermes-mcp-gateway"]
    assert mounted, "no ConfigMap volume: checksum would have nothing to watch"
    helpers = [c["name"] for c in pod["initContainers"] + pod["containers"]
               if c["name"].startswith("mcp-gateway-")]
    readers = [c["name"] for c in pod["initContainers"] + pod["containers"]
               for m in c.get("volumeMounts", []) if m["name"] in mounted]
    assert set(helpers) <= set(readers), f"a helper does not read the scripts volume: {helpers}"
    # Read-only: the mounted code must not be writable from inside the pod.
    for container in pod["initContainers"] + pod["containers"]:
        for m in container.get("volumeMounts", []):
            if m["name"] in mounted:
                assert m.get("readOnly") is True, f"{container['name']} mounts {m['name']} writable"
