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

import difflib
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
    options = values["app-template"]["controllers"]["hermes"]["defaultContainerOptions"]["image"]
    return f"{options['repository']}:{options['tag']}"


def _render_enabled(work: Path) -> list[dict]:
    """Render the chart with the feature on and return every document."""
    values = _values()
    controller = values["app-template"]["controllers"]["hermes"]
    enabled = json.loads(json.dumps(values))
    # ``--set`` cannot flip the YAML aliases that share mcpGateway.enabled, so
    # the override document mirrors exactly what an operator edits in one place.
    enabled["mcpGateway"]["enabled"] = True
    for name in controller["initContainers"]:
        if name.startswith("mcp-gateway-"):
            enabled["app-template"]["controllers"]["hermes"]["initContainers"][name]["enabled"] = True
    for name in controller["containers"]:
        if name.startswith("mcp-gateway-"):
            enabled["app-template"]["controllers"]["hermes"]["containers"][name]["enabled"] = True
    for name in values["app-template"]["persistence"]:
        if name.startswith("mcp-gateway-"):
            enabled["app-template"]["persistence"][name]["enabled"] = True

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
        # Same uid the StatefulSet renders, so file ownership of the credential
        # is tested as it exists in the pod, not as root.
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


def test_disabled_render_identical_to_head() -> None:
    """The default-off pod must be what it was, except one proven-inert line.

    Renders the committed chart and the working tree and diffs them. Exactly one
    delta is tolerated, and the test proves it cannot do anything: the
    ConfigMap-scoped Reloader annotation, which only fires for ConfigMaps the pod
    references - and while the feature is off this pod references none, so no
    annotation-triggered rollout is possible. It exists because the helpers run
    scripts mounted from ``hermes-mcp-gateway``: without a rollout trigger, a
    script fix would never reach the running containers.

    Anything else - a new container, volume, object or annotation - fails here.
    """
    if helm is None:
        pytest.skip("helm not installed")
    head = subprocess.run(
        ["git", "archive", "HEAD", "apps/hermes"], capture_output=True, text=True, check=True
    ).stdout
    with tempfile.TemporaryDirectory() as tmp:
        root = Path(tmp)
        subprocess.run(["tar", "-x", "-C", root], input=head.encode(), check=True)
        committed = root / "apps" / "hermes"
        # Sub charts are gitignored build output: copy them so both renders use
        # the same app-template version.
        shutil.copytree(CHART / "charts", committed / "charts")

        def render(path: Path) -> str:
            return subprocess.run(
                ["helm", "template", RELEASE, str(path), "--namespace", NAMESPACE],
                capture_output=True, text=True, check=True, cwd=tmp,
            ).stdout

        before, after = render(committed), render(CHART)
        if before == after:
            return
        added = [ln for ln in difflib.unified_diff(before.splitlines(), after.splitlines(),
                                                   lineterm="")
                  if ln.startswith("+") and not ln.startswith("+++")]
        removed = [ln for ln in difflib.unified_diff(before.splitlines(), after.splitlines(),
                                                     lineterm="")
                   if ln.startswith("-") and not ln.startswith("---")]
        allowed = {"+      annotations: ", '+        configmap.reloader.stakater.com/auto: "true"'}
        assert set(added) <= allowed, f"unexpected default-off delta: {added}"
        assert not removed, f"lines vanished from the disabled render: {removed}"

        # ...and the tolerated line is inert while disabled: no ConfigMap volume
        # or reference exists for Reloader to watch.
        docs = [d for d in yaml.safe_load_all(after) if d]
        pod = next(d for d in docs if d["kind"] == "StatefulSet")["spec"]["template"]
        refs = [v["configMap"]["name"] for v in pod["spec"].get("volumes", [])
                if "configMap" in v]
        refs += [e["configMapRef"]["name"] for c in pod["spec"]["containers"]
                 for e in c.get("envFrom", []) or [] if "configMapRef" in e]
        assert not refs, f"disabled pod references ConfigMaps {refs}: the annotation is not inert"
        assert not any(d["kind"] == "ConfigMap" for d in docs), "gateway ConfigMap rendered while off"


def test_disabled_render_is_untouched() -> None:
    """With the flag off the chart must render exactly what it rendered before.

    The strict proof is the byte-for-byte comparison against the committed
    chart (``run.sh --compare-head``); this assertion only names the resources
    the feature would add, since "mcp-gateway" alone also matches the untouched
    Grafana sidecar.
    """
    if helm is None:
        pytest.skip("helm not installed")
    assert _values()["mcpGateway"]["enabled"] is False, "feature must stay off by default"
    current = subprocess.run(
        ["helm", "template", RELEASE, str(CHART), "--namespace", NAMESPACE],
        capture_output=True, text=True, check=True,
    ).stdout
    for added in ("kind: KanidmServiceAccount\nmetadata:\n  name: hermes-mcp",
                  "name: hermes-mcp-gateway", "name: mcp-gateway-config",
                  "name: mcp-gateway-token", "name: mcp-gateway-token-refresher",
                  "hermes-mcp-token"):
        assert added not in current, f"feature leaked into the disabled render: {added}"
    for existing in ("grafana-mcp-refresher", "hermes-data", "nousresearch/hermes-agent:v2026.8.16.2"):
        assert existing in current, f"regression: {existing} vanished"


def test_gateway_entry_only_declares_oauth(workspace: Path) -> None:
    """The merged entry must carry no credential: no headers, no bearer, no secret.

    The credential lives only in the token store, which keeps rotation inside
    the reload mechanism Hermes already implements.
    """
    cfg = json.loads((workspace / "config.json").read_text())
    entry = cfg["entry"]
    assert entry["auth"] == "oauth"
    assert "headers" not in entry and "env" not in entry
    assert entry["url"].startswith("https://")
    exchange = cfg["exchange"]
    assert exchange["clientId"] == exchange["audience"], "audience must match the gateway client"
    assert "mcp_firecrawl" in exchange["scope"], "Firecrawl grant comes from the mapped scope"
    assert exchange["marginSeconds"] > 0 and exchange["minIntervalSeconds"] > 0


def test_rendered_entry_matches_gateway_contract(workspace: Path) -> None:
    """Pin the values against what ``apps/mcp-gateway`` actually serves.

    * the gateway routes per target (``/firecrawl/mcp``) and has no aggregate
      ``/mcp``, so the URL must carry the target and the serverName must say
      which target this entry talks to;
    * only the scopes the gateway really grants may be requested: asking for
      ``openid``/``email`` here would either fail the exchange or silently
      assume a grant that does not exist (the agent needs neither - the access
      token is the only credential Hermes' OAuth provider sends);
    * the aud/client pair is Kanidm's ``mcp-gateway`` and is deliberately
      independent of the host part of the URL.
    """
    cfg = json.loads((workspace / "config.json").read_text())
    assert cfg["serverName"] == "homelab-firecrawl"
    assert cfg["entry"]["url"] == "https://mcp.grigri.cloud/firecrawl/mcp"
    from urllib.parse import urlparse

    parsed = urlparse(cfg["entry"]["url"])
    assert parsed.path == "/firecrawl/mcp", f"not a per-target route: {parsed.path}"
    exchange = cfg["exchange"]
    assert exchange["scope"] == "mcp_firecrawl", f"ungranted scopes: {exchange['scope']}"
    assert not {"openid", "profile", "email"} & set(exchange["scope"].split())
    assert exchange["clientId"] == exchange["audience"] == "mcp-gateway"
    assert cfg["exchange"]["url"].startswith("https://idm.grigri.cloud/")

    values = _values()["mcpGateway"]
    assert values["serviceAccount"]["entryManagedBy"] == "mcp-gateway-agents"
    assert values["serviceAccount"]["name"] == "hermes-mcp", "one unique id per instance"
    assert values["enabled"] is False, "first instance ships disabled"


def test_gateway_helpers_block_startup_until_they_succeed(workspace: Path) -> None:
    """A failing merge must NOT let Hermes start with an unconfigured entry.

    Both gateway steps are ordinary init containers: Kubernetes runs them to
    completion before the app container starts and re-tries the pod until they
    exit 0. A native sidecar (``restartPolicy: Always``) would instead be
    allowed to fail while the agent started without its MCP entry, so the
    rendered absence of that field is part of the fail-closed contract - as is
    the merge script's own non-zero exit (``check_merge.py malformed``).
    """
    pod = next(d for d in _render_enabled(workspace)
               if d.get("kind") == "StatefulSet")["spec"]["template"]["spec"]
    init = {c["name"]: c for c in pod.get("initContainers", [])}
    for name in ("mcp-gateway-config", "mcp-gateway-token"):
        assert name in init, f"{name} must be an init container, not a sidecar"
        assert init[name].get("restartPolicy") is None, (
            f"{name} is a native sidecar: the agent could start before it succeeded")
    # The exchange must finish before the agent's own container starts, so the
    # first MCP request in the pod always finds a token file.
    assert "hermes" in [c["name"] for c in pod["containers"]]


def test_script_changes_roll_the_pod(workspace: Path) -> None:
    """ConfigMap-only edits to the helper scripts must reach the running pod.

    The scripts are mounted, not baked into the image, so kubelet updating the
    mount does not re-exec the Python that is already running. Reloader is the
    repo's answer (platform/reloader, used by home-assistant/mosquitto), and the
    ConfigMap-scoped annotation is deliberate: it can never be woken by a Secret
    rotation, which the existing Grafana refresher handles by design.
    """
    docs = _render_enabled(workspace)
    sts = next(d for d in docs if d.get("kind") == "StatefulSet")
    annotations = sts["spec"]["template"]["metadata"].get("annotations") or {}
    assert annotations.get("configmap.reloader.stakater.com/auto") == "true", annotations

    pod = sts["spec"]["template"]["spec"]
    volumes = {v["name"]: v for v in pod.get("volumes", []) if "configMap" in v}
    mounted = [n for n, v in volumes.items() if v["configMap"]["name"] == "hermes-mcp-gateway"]
    assert mounted, "no ConfigMap volume: Reloader would have nothing to watch"
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
