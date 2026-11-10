#!/usr/bin/env python3
"""Runs inside the pinned Hermes image: proves the opt-in config merge keeps
unmanaged instance config intact.

Modes (argv[1]) - all assert loudly and exit non-zero on regression:

* ``preserve``   unknown keys and sibling MCP servers survive; our entry is
                 replaced wholesale (a stale nested key from an older entry goes
                 away); file mode/owner unchanged; second run writes nothing.
* ``malformed``  an unparseable config.yaml is left byte-for-byte alone.
* ``bad-entry``  a broken gateway entry refuses to write anything.
* ``redaction``  no value from the existing config reaches stdout/stderr.

``HERMES_HOME`` is a scratch directory, so the real ``hermes_cli.config`` writer
and ``utils.atomic_yaml_write`` are the code under test.
"""

from __future__ import annotations

import json
import os
import shutil
import stat
import subprocess
import sys
import tempfile
from pathlib import Path

import yaml

HOME = Path(os.environ["HERMES_HOME"])
CONFIG = HOME / "config.yaml"
SCRIPT = os.environ.get("MCP_GATEWAY_MERGE_SCRIPT", "/work/mcp_gateway_merge.py")


def _local_gateway_config() -> str:
    """Private copy of the rendered ``config.json`` for this check only.

    The mounted original is shared by every check in the suite (and across
    runs), so any deliberate corruption - a broken entry, a stub token URL -
    must happen in a scratch copy. Without this, one check's negative case
    silently poisons the next check's config.
    """
    import atexit

    source = os.environ["MCP_GATEWAY_CONFIG"]
    directory = tempfile.mkdtemp(prefix="check-config-")
    atexit.register(shutil.rmtree, directory, ignore_errors=True)
    path = os.path.join(directory, "config.json")
    shutil.copyfile(source, path)
    return path


GATEWAY_CONFIG = _local_gateway_config()

PRIVATE = {
    "model": {"default": {"provider": "custom", "model": "isidoro"}},
    "agent": {"max_turns": 77, "personality": "mine"},
    "telegram": {"token": "PLANTED-SECRET-DO-NOT-ECHO"},
    "skills": ["a", "b"],
}
SIBLING = {
    "command": "npx",
    "args": ["-y", "private-mcp"],
    "env": {"TOKEN": "PLANTED-SECRET-DO-NOT-ECHO"},
    "enabled": True,
}


def desired_entries() -> dict[str, dict]:
    """All server entries from the rendered gateway config (multi-target)."""
    return json.loads(Path(GATEWAY_CONFIG).read_text())["servers"]


def server_names() -> list[str]:
    """All owned ``mcp_servers`` keys, taken from the rendered chart config."""
    return list(json.loads(Path(GATEWAY_CONFIG).read_text())["servers"].keys())


def private_sibling_key() -> str:
    """A name that can never collide with any owned key."""
    return "unmanaged-" + "-".join(server_names())


def write_config(servers: dict, extra: dict | None = None) -> None:
    doc = dict(extra if extra is not None else PRIVATE)
    doc["mcp_servers"] = servers
    CONFIG.write_text(yaml.safe_dump(doc, sort_keys=False))
    os.chmod(CONFIG, 0o640)


def run_merge() -> subprocess.CompletedProcess:
    env = dict(
        os.environ,
        PYTHONPATH="/opt/hermes",
        PYTHONDONTWRITEBYTECODE="1",
        # Always the private copy: `bad-entry` corrupts it, and the mounted
        # original is shared with every other check in the suite.
        MCP_GATEWAY_CONFIG=GATEWAY_CONFIG,
    )
    return subprocess.run(
        [sys.executable, SCRIPT], env=env, capture_output=True, text=True, timeout=120
    )


def load() -> dict:
    return yaml.safe_load(CONFIG.read_text())


def snapshot() -> tuple[bytes, int, int, int]:
    st = CONFIG.stat()
    return CONFIG.read_bytes(), stat.S_IMODE(st.st_mode), st.st_uid, st.st_gid


def main() -> int:
    mode = sys.argv[1]
    HOME.mkdir(parents=True, exist_ok=True)

    if mode == "malformed":
        CONFIG.write_text("mcp_servers: [unclosed\n  bad: : :\n")
        before = CONFIG.read_bytes()
        listing = set(os.listdir(HOME))
        proc = run_merge()
        assert proc.returncode != 0, f"expected refusal, got: {proc.stdout}{proc.stderr}"
        assert CONFIG.read_bytes() == before, "corrupt config was modified"
        # Importing hermes_cli.config makes Hermes itself warn about the broken
        # file and snapshot it to ``config.yaml.corrupt.<ts>.bak`` (and create
        # its usual scaffolding). That is Hermes protecting the user's data, not
        # our writer: assert nothing else appeared and the snapshot is byte-equal,
        # so the corrupt content is provably still recoverable.
        added = set(os.listdir(HOME)) - listing
        allowed = {n for n in added
                   if (HOME / n).is_dir() or n == "SOUL.md"
                   or n.startswith("config.yaml.corrupt.") and n.endswith(".bak")}
        assert added == allowed, f"unexpected files in HERMES_HOME: {added - allowed}"
        for name in added:
            if name.startswith("config.yaml.corrupt."):
                assert (HOME / name).read_bytes() == before, "corrupt snapshot differs"
        print("PASS malformed config left untouched and failed closed")
        return 0

    if mode == "bad-entry":
        write_config({private_sibling_key(): SIBLING})
        before, *_ = snapshot()
        # Build a config with one broken server (no url) among otherwise valid ones
        bad_servers = dict(desired_entries())
        first_name = next(iter(bad_servers))
        bad_servers[first_name] = {"command": "whoami"}  # no url, no auth
        Path(GATEWAY_CONFIG).write_text(json.dumps({"servers": bad_servers,
                                                    "exchange": json.loads(Path(GATEWAY_CONFIG).read_text())["exchange"]}))
        proc = run_merge()
        assert proc.returncode != 0, "entry without a url must fail closed"
        assert CONFIG.read_bytes() == before, "config rewritten after a bad entry"
        print("PASS invalid gateway entry refused")
        return 0

    if mode == "redaction":
        write_config({private_sibling_key(): SIBLING})
        proc = run_merge()
        assert proc.returncode == 0, proc.stderr
        combined = proc.stdout + proc.stderr
        assert "PLANTED-SECRET-DO-NOT-ECHO" not in combined, "secret echoed"
        assert "isidoro" not in combined, "config values echoed"
        print("PASS merge output names keys only")
        return 0

    # default: preserve. The owned keys and their stale predecessors come from the
    # rendered chart config, so this proves the real deployment names.
    owned_names = server_names()
    owned_entries = desired_entries()
    # Seed one owned key with a stale entry to prove replacement (not just addition)
    first_owned = owned_names[0]
    stale_entry = dict(owned_entries[first_owned])
    stale_entry["url"] = "https://old.example/firecrawl/mcp"
    stale_entry["stale_key"] = "remove-me"
    write_config({private_sibling_key(): SIBLING,
                  first_owned: stale_entry})
    before_mode_owner = snapshot()[1:]
    proc = run_merge()
    assert proc.returncode == 0, f"merge failed: {proc.stdout}{proc.stderr}"
    doc = load()

    assert doc["model"] == PRIVATE["model"], "unmanaged model config changed"
    assert doc["agent"] == PRIVATE["agent"], "unmanaged agent config changed"
    assert doc["telegram"] == PRIVATE["telegram"], "unmanaged credentials changed"
    assert doc["skills"] == PRIVATE["skills"], "unmanaged skills changed"

    servers = doc["mcp_servers"]
    assert set(servers) == {private_sibling_key(), *owned_names}, f"servers lost/added: {set(servers)}"
    assert servers[private_sibling_key()] == SIBLING, "sibling MCP server entry modified"
    for name in owned_names:
        assert servers[name] == owned_entries[name], f"{name} gateway entry not exact"
    assert "stale_key" not in servers[first_owned], "owned entry merged instead of replaced"
    # The gateway serves per-target routes only (``/<target>/mcp``): an entry
    # pointing at a bare ``/mcp`` would 404, so the chart must keep the target
    # in the path and the server name must name the target.
    from urllib.parse import urlparse

    for name in owned_names:
        entry = owned_entries[name]
        parsed = urlparse(entry["url"])
        assert parsed.path.endswith("/mcp"), f"{name}: not an MCP route: {parsed.path}"
        target = parsed.path.removesuffix("/mcp").strip("/")
        assert target and target.replace("-", "") in name.replace("-", ""), (
            f"server '{name}' does not name the url target '{target}'")
    assert snapshot()[1:] == before_mode_owner, "config.yaml mode/owner changed"

    before = CONFIG.read_bytes()
    proc = run_merge()
    assert proc.returncode == 0, proc.stderr
    assert "already current" in proc.stdout, f"second run rewrote: {proc.stdout}"
    assert CONFIG.read_bytes() == before, "second run changed the file (mtime churn)"
    print("PASS merge preserves siblings and unmanaged keys, idempotent")
    return 0


if __name__ == "__main__":
    sys.exit(main())
