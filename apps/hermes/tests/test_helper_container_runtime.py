#!/usr/bin/env python3
"""Runs the rendered gateway helper scripts inside the real Hermes image.

Everything here is offline: a throwaway docker volume stands in for the
``hermes-data`` PVC, a loopback fixture answers the token exchange, and the
tokens are literal fixture strings. No cluster access, no Kanidm, no real
credential, no live Secret is read.

What it actually verifies, because these were the open review findings:

``uid`` behavior on the agent's home directory
    The volume root is ``root:root 0755``, which is how ``/opt/data`` looks on
    the cluster because the agent container itself runs as uid 0. A uid-10000
    helper therefore fails with ``PermissionError`` and writes nothing, while a
    uid-0 helper (with all capabilities dropped, exactly as the chart sets it)
    succeeds. That is the evidence for the chart's documented uid choice.

Credential handling
    The subject token must appear in the authorization server's POST body and in
    no ``/proc/<pid>/cmdline`` inside the container, which is what rules out the
    legacy ``curl --data-urlencode`` shape for the enabled path.

Entry shape and write discipline
    ``mcp_gateway_merge.py`` writes Hermes' ``mcp_servers`` entries, preserves
    everything else, does not touch the file's mtime when nothing changed, and
    refuses outright to rewrite a ``config.yaml`` it cannot parse.

Artifacts are the ones ``helm template`` produces (the ConfigMap payload), so a
template that ships a different ``config.json`` shape than the scripts read
fails here rather than in the pod.

The fixture directory is normalised to ``root:root 0700`` before each run,
because that is what ``/opt/data`` is on the cluster: ``zfs-localpv`` mounts a
fresh dataset and the agent container (``runAsUser: 0``) creates everything under
it. That is deliberately *not* what the image ships - the image has
``drwx------ hermes(10000) hermes(10000) /opt/data``, which a plain
``docker volume`` inherits, and testing against that would have shown a uid-10000
helper writing agent data that in the real pod it cannot touch.

Requires the local image; skips (loudly) if docker or the image is missing.
Run: ``python3 apps/hermes/tests/test_helper_container_runtime.py``.
"""

from __future__ import annotations

import json
import os
import random
import shutil
import string
import subprocess
import sys
import tempfile
import time

REPO = subprocess.run(["git", "rev-parse", "--show-toplevel"], capture_output=True,
                      text=True, check=True).stdout.strip()
CHART = os.path.join(REPO, "apps", "hermes")
IMAGE = "nousresearch/hermes-agent:v2026.8.16.2"
SUBJECT = "SYNTHETIC-SUBJECT-TOKEN-DO-NOT-USE"  # fixture only, never a real credential
AS_PORT = 8089

_FIXTURE_DIR = os.path.join(CHART, "tests", "fixtures")
_state: dict = {}


def _available() -> bool:
    if subprocess.run(["docker", "info"], capture_output=True).returncode != 0:
        return False
    return subprocess.run(["docker", "image", "inspect", IMAGE], capture_output=True).returncode == 0


def _setup() -> dict:
    """Render the ConfigMap and stage its payload plus the fixture AS."""
    if _state:
        return _state
    work = tempfile.mkdtemp(prefix="hermes-mcpgw-")
    out = subprocess.run(["helm", "template", "--namespace", "hermes", "hermes", CHART,
                          "--set", "global.mcpGateway.enabled=true",
                          "--show-only", "templates/configmap-mcp-gateway.yaml"],
                         capture_output=True, text=True, check=True).stdout
    import yaml
    doc = next(d for d in yaml.safe_load_all("---\n" + out) if isinstance(d, dict))
    data = doc["data"]
    cfg = json.loads(data["config.json"])
    # Point the exchange at the loopback fixture. Route URLs stay as configured:
    # only the token endpoint is simulated here.
    cfg["exchange"]["url"] = f"http://127.0.0.1:{AS_PORT}/token"
    scripts = os.path.join(work, "mcp-gateway")
    os.makedirs(scripts)
    with open(os.path.join(scripts, "config.json"), "w", encoding="utf-8") as fh:
        json.dump(cfg, fh, indent=2)
    for name in ("mcp_gateway_merge.py", "mcp_gateway_token.py"):
        with open(os.path.join(scripts, name), "w", encoding="utf-8") as fh:
            fh.write(data[name])
    obs = os.path.join(work, "observed")
    os.makedirs(obs, exist_ok=True)
    os.chmod(obs, 0o777)
    vol = "hermes-mcpgw-test-" + "".join(random.choices(string.ascii_lowercase, k=6))
    subprocess.run(["docker", "volume", "create", vol], check=True, capture_output=True)
    # Normalise the mount to the cluster's ownership, then verify it took.
    subprocess.run(["docker", "run", "--rm", "--user", "0:0", "--entrypoint", "/bin/sh",
                    "-v", f"{vol}:/opt/data", IMAGE, "-c",
                    "rm -rf /opt/data/* /opt/data/[!.]* 2>/dev/null; chown 0:0 /opt/data; chmod 0700 /opt/data; ls -ld /opt/data"],
                   check=True, capture_output=True, text=True)
    _state.update(work=work, scripts=scripts, observed=obs, volume=vol, cfg=cfg)
    return _state


def _teardown():
    st = _setup()
    # The volume root is uid 0, but nothing we delete lives in it: the named
    # volume is removed by docker, and the scratch dir is ours.
    subprocess.run(["docker", "volume", "rm", "-f", st["volume"]], capture_output=True)
    shutil.rmtree(st["work"], ignore_errors=True)


def _docker_volume_run(script: str, *, user: str, env: dict[str, str] | None = None,
                       seed: dict[str, str] | None = None) -> subprocess.CompletedProcess:
    """Run a driver as ``user`` with all capabilities dropped.

    Each run gets its own scratch mounted at ``/work`` (world-writable, so any
    uid can create it): a shared directory would let one run's files block the
    next, and a uid-0-with-caps-dropped process cannot overwrite another uid's
    files - which is itself the point of not reusing ``/work`` between cases.
    """
    st = _setup()
    obs = tempfile.mkdtemp(prefix="obs-", dir=st["observed"])
    os.chmod(obs, 0o777)
    for name, text in (seed or {}).items():
        with open(os.path.join(obs, name), "w", encoding="utf-8") as fh:
            fh.write(text)
    st["last_obs"] = obs
    cmd = ["docker", "run", "--rm", "--user", user, "--cap-drop", "ALL",
           "--entrypoint", "/bin/sh",
           "-v", f"{st['volume']}:/opt/data",
           "-v", f"{st['scripts']}:/mcp-gateway:ro",
           "-v", f"{obs}:/work",
           "-v", f"{_FIXTURE_DIR}:/fixture:ro",
           "-e", "PYTHONPATH=/opt/hermes",
           "-e", "PYTHONDONTWRITEBYTECODE=1",
           "-e", "MCP_GATEWAY_CONFIG=/mcp-gateway/config.json",
           "-e", f"MCP_GATEWAY_SUBJECT_TOKEN={SUBJECT}"]
    for k, v in (env or {}).items():
        cmd += ["-e", f"{k}={v}"]
    cmd += [IMAGE, "-c", script]
    return subprocess.run(cmd, capture_output=True, text=True, timeout=300)


# --- the in-container drivers ------------------------------------------------

_DRIVER_TOKEN = f"""
set -u
python3 /fixture/fake_token_as.py --port {AS_PORT} --delay 3 --record /work/as.jsonl &
ASPID=$!
sleep 1
python3 /mcp-gateway/mcp_gateway_token.py > /work/token.out 2> /work/token.err &
TOKENPID=$!
sleep 1.2
# Sample every process's argv while the exchange is in flight.
: > /work/cmdlines.txt
for p in /proc/[0-9]*; do
  [ -r "$p/cmdline" ] || continue
  tr '\\0' ' ' < "$p/cmdline" >> /work/cmdlines.txt
  echo >> /work/cmdlines.txt
done
wait $TOKENPID
RC=$?
kill $ASPID 2>/dev/null
echo "exit=$RC"
cat /work/token.out /work/token.err
ls -l /opt/data/mcp-tokens 2>&1 || true
"""

_DRIVER_TOKEN_REPORT = """
for f in /opt/data/mcp-tokens/*.json; do
  [ -e "$f" ] || continue
  echo "FILE $f $(stat -c '%a %u' "$f")"
  echo "BODY $(tr -d '\n' < "$f")"
done
"""


def test_uid_10000_cannot_write_the_agents_home():
    """Non-root fails on the agent-owned home and writes nothing (fail closed)."""
    st = _setup()
    r = _docker_volume_run(_DRIVER_TOKEN, user="10000:10000")
    body = r.stdout + r.stderr
    assert "exit=1" in body, body[-800:]
    assert "token store unavailable" in body and "Permission denied" in body, body[-800:]
    listing = _docker_volume_run("ls -l /opt/data 2>&1; find /opt/data -name '*.json' | wc -l",
                                 user="0:0")
    assert listing.stdout.strip().endswith("0"), listing.stdout
    print("✓ uid 10000 on the agent-owned home: PermissionError, exit 1, nothing written")


def test_uid_0_writes_credentials_0600():
    """The chart's actual configuration: uid 0 with all capabilities dropped."""
    st = _setup()
    r = _docker_volume_run(_DRIVER_TOKEN, user="0:0")
    body = r.stdout + r.stderr
    assert "exit=0" in body, body[-800:]
    assert "Permission denied" not in body, body[-800:]
    report = _docker_volume_run(_DRIVER_TOKEN_REPORT, user="0:0").stdout
    files: dict[str, dict | None] = {}
    modes: list[str] = []
    pending = None
    for line in report.splitlines():
        if line.startswith("FILE "):
            _, path, mode, uid = line.split()
            pending = os.path.basename(path)
            files[pending] = None
            modes.append(f"{mode}:{uid}")
        elif line.startswith("BODY ") and pending:
            files[pending] = json.loads(line[5:])
            pending = None
    expected = {"homelab-grafana.json", "homelab-grafana.client.json",
                "homelab-context7.json", "homelab-context7.client.json",
                "homelab-firecrawl.json", "homelab-firecrawl.client.json"}
    assert expected <= set(files), sorted(files)
    assert set(modes) == {"600:0"}, f"credential mode/owner: {sorted(set(modes))}"
    tok = files["homelab-grafana.json"]
    assert tok["access_token"] == "synthetic-access-token-for-mcp_grafana", tok
    assert tok["scope"] == "mcp_grafana" and tok["token_type"] == "Bearer"
    assert "refresh_token" not in tok, "never hand Hermes a refresh token"
    assert tok["expires_at"] > time.time() + 300, "must not be written near expiry"
    print("✓ uid 0 (caps dropped): 6 files, mode 0600, per-route scopes, no refresh_token")
    return tok


def test_subject_token_never_reaches_argv():
    """In-process HTTP: the credential is in the POST body, not in any cmdline."""
    _docker_volume_run(_DRIVER_TOKEN, user="0:0")
    obs = _setup()["last_obs"]
    with open(os.path.join(obs, "cmdlines.txt"), encoding="utf-8") as fh:
        cmdlines = fh.read()
    assert SUBJECT not in cmdlines, "credential leaked into /proc/*/cmdline"
    assert "mcp_gateway_token.py" in cmdlines, "driver did not sample the exchange"
    with open(os.path.join(obs, "as.jsonl"), encoding="utf-8") as fh:
        seen = [json.loads(line) for line in fh if line.strip()]
    assert len(seen) >= 2, seen
    scopes = sorted({s["scope"] for s in seen})
    assert scopes == ["mcp_context7", "mcp_firecrawl", "mcp_grafana"], scopes
    for s in seen:
        assert s["grant_type"] == "urn:ietf:params:oauth:grant-type:token-exchange"
        assert s["audience"] == s["client_id"] == "mcp-gateway"
        assert s["has_subject_token"] and s["subject_token_prefix"] == SUBJECT[:8]
    assert not any("curl" in line for line in cmdlines.splitlines()), cmdlines
    print("✓ credential in body only: 3 exchanges, per-route scopes, zero hits in /proc cmdlines")


_MCP_FIXTURE = """
model:
  default: claude-opus-4-6
mcp_servers:
  local-something:
    command: npx
    args: ["-y", "some-server"]
gateway:
  loop_watchdog: false
"""

_DRIVER_MERGE = """
set -u
python3 -c 'import pathlib;pathlib.Path("/opt/data/config.yaml").write_text(pathlib.Path("/work/config.yaml").read_text())'
chmod 644 /opt/data/config.yaml
python3 /mcp-gateway/mcp_gateway_merge.py; echo "first=$?"
stat -c 'mtime=%Y' /opt/data/config.yaml
python3 /mcp-gateway/mcp_gateway_merge.py; echo "second=$?"
stat -c 'mtime=%Y' /opt/data/config.yaml
echo "-----"; cat /opt/data/config.yaml
"""

_DRIVER_MERGE_BROKEN = """
set -u
printf 'model: [unclosed\\n  bad: : :\n' > /opt/data/config.yaml
before=$(md5sum /opt/data/config.yaml)
python3 /mcp-gateway/mcp_gateway_merge.py; echo "exit=$?"
after=$(md5sum /opt/data/config.yaml)
[ "$before" = "$after" ] && echo "UNTOUCHED" || echo "MODIFIED"
"""


def test_merge_adds_entries_and_is_idempotent():
    """One write per change, sibling servers and other sections preserved."""
    st = _setup()
    r = _docker_volume_run(_DRIVER_MERGE, user="0:0", seed={"config.yaml": _MCP_FIXTURE})
    body = r.stdout + r.stderr
    assert "first=0" in body and "second=0" in body, body[-900:]
    mtimes = [int(l.split("=")[1]) for l in body.splitlines() if l.startswith("mtime=")]
    assert len(mtimes) == 2 and mtime_equal(mtimes), f"mtime churn between runs: {mtimes}"
    import yaml
    doc = yaml.safe_load(body.split("-----", 1)[1])
    assert set(doc["mcp_servers"]) == {"local-something", "homelab-grafana", "homelab-context7", "homelab-firecrawl"}
    assert doc["mcp_servers"]["local-something"]["command"] == "npx"
    assert doc["model"]["default"] == "claude-opus-4-6"
    assert doc["gateway"]["loop_watchdog"] is False
    entry = doc["mcp_servers"]["homelab-grafana"]
    assert entry["auth"] == "oauth" and entry["oauth"]["scope"] == "mcp_grafana"
    assert entry["url"] == st["cfg"]["servers"]["homelab-grafana"]["url"]
    print("✓ merge: 3 routes added, sibling + unrelated sections intact, no mtime churn")


def test_merge_refuses_unparseable_config():
    """Fail closed: a config that does not parse is left byte-for-byte alone."""
    st = _setup()
    r = _docker_volume_run(_DRIVER_MERGE_BROKEN, user="0:0")
    body = r.stdout + r.stderr
    assert "exit=1" in body, body[-500:]
    assert "UNTOUCHED" in body, body[-500:]
    assert "will not parse" in body, body[-500:]
    print("✓ merge on corrupt config.yaml: exit 1, file untouched")


def mtime_equal(values: list[int]) -> bool:
    return len(set(values)) == 1


if __name__ == "__main__":
    if not _available():
        print(f"SKIP: docker and/or image {IMAGE} not available locally")
        sys.exit(0)
    print("Running MCP gateway container-runtime tests (offline, fixture tokens only)...")
    try:
        for fn in [test_uid_10000_cannot_write_the_agents_home,
                   test_uid_0_writes_credentials_0600,
                   test_subject_token_never_reaches_argv,
                   test_merge_adds_entries_and_is_idempotent,
                   test_merge_refuses_unparseable_config]:
            fn()
        print("\n✓ All container-runtime tests passed")
    finally:
        _teardown()
