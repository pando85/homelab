#!/usr/bin/env python3
"""Merge the owned homelab MCP gateway entry into Hermes ``config.yaml``.

Runs as an init container of the ``hermes-mcp-gateway`` helper pod - a second
pod that mounts the same ``hermes-data`` claim the agent lives on - inside the
pinned ``nousresearch/hermes-agent`` image, and uses Hermes' own primitives:

* ``hermes_cli.config.get_config_path``  - resolves ``$HERMES_HOME/config.yaml``
* ``hermes_cli.config.atomic_config_write`` - Hermes' single documented
  chokepoint for config writes: it refuses to clobber an existing file that
  cannot be read (permission error, broken mount, transient I/O - the exact
  root cause of the config-overwrite incidents in
  ``docs/troubleshooting/hermes-config-overwrite-recovery.md``) and delegates
  to ``utils.atomic_yaml_write`` (temp file + fsync + ``os.replace``, preserving
  the previous mode and owner).

Deliberately NOT ``save_config()``: that path normalises the whole document
(``_normalize_root_model_keys`` rewrites the ``model`` section, default-stripping
drops keys, managed-scope stripping removes leaves). A sidecar that owns one key
must not restyle sections it never touched, so this script reads the raw YAML and
writes the raw document back with exactly one key changed.

Ownership rules:

* exactly one key is written: ``mcp_servers.<serverName>``;
* every other top-level section and every sibling ``mcp_servers`` entry read
  back from disk is carried through untouched - never a whole-map replacement;
* nothing is written when the entry already matches (no mtime churn, so Hermes'
  config watcher does not reconnect the gateway on every pod start);
* a file that does not parse, or whose ``mcp_servers`` is not a mapping, is
  left byte-for-byte alone and the script fails closed;
* nothing is printed but key names and counts - ``config.yaml`` holds secrets.

Known cost of the raw round-trip, identical to ``hermes mcp add`` which also
rewrites the file through the YAML round-trip: hand-written comments in
``config.yaml`` are not preserved.

One side effect to expect, and it is Hermes' own, not this script's: importing
``hermes_cli.config`` runs Hermes' config load, which on an unparseable
``config.yaml`` warns on stderr and snapshots the file to
``config.yaml.corrupt.<ts>.bak`` so the content stays recoverable. This script
never writes in that case.

Turned on by ``global.mcpGateway.enabled``; when it is false neither this script
nor the helper pod that runs it is rendered at all, and the chart renders exactly
what it did before the feature existed.

A merged entry only becomes a live connection when Hermes re-reads
``mcp_servers``: at gateway startup, or through the ``/reload-mcp`` chat command.
The automatic config watcher is the interactive CLI's
(``cli.py::_check_config_mcp_changes``, called only from its ``process_loop``),
and ``gateway run`` does not run it, so enabling this feature is followed by the
agent's next restart or a ``/reload-mcp``.
"""

from __future__ import annotations

import json
import os
import sys


def _fail(reason: str) -> int:
    print(f"mcp-gateway-merge: {reason}", file=sys.stderr)
    return 1


def _load_gateway_config(path: str) -> tuple[dict[str, dict], dict]:
    """Load multi-target gateway config.

    Returns:
        Tuple of (servers_map, exchange_config) where servers_map is
        {server_name: entry_config} for each target.
    """
    with open(path, encoding="utf-8") as fh:
        cfg = json.load(fh)
    if not isinstance(cfg, dict):
        raise ValueError("gateway config is not an object")

    # Multi-target: servers is a map of name -> entry
    servers_raw = cfg.get("servers", {})
    if not isinstance(servers_raw, dict):
        raise ValueError("gateway config 'servers' is not an object")

    servers = {}
    for name, entry in servers_raw.items():
        servers[str(name).strip()] = dict(entry)

    exchange = dict(cfg.get("exchange", {}))
    return servers, exchange


def main() -> int:
    config_file = os.environ.get("MCP_GATEWAY_CONFIG", "/mcp-gateway/config.json")
    try:
        servers, exchange = _load_gateway_config(config_file)
    except Exception as exc:  # noqa: BLE001 - never echo the file
        return _fail(f"gateway config unreadable: {type(exc).__name__}")

    if not servers:
        return _fail("gateway config has no servers defined")

    # Validate all servers before touching config
    for name, desired in servers.items():
        url = desired.get("url")
        if not name or not isinstance(url, str) or not url.startswith(("http://", "https://")):
            return _fail(f"server '{name}' has no valid http(s) url")
        if desired.get("auth") != "oauth":
            return _fail(f"server '{name}' must authenticate with the OAuth token store")

    try:
        import yaml
        from hermes_cli.config import atomic_config_write, get_config_path
    except Exception as exc:  # noqa: BLE001
        return _fail(f"hermes_cli.config unavailable: {type(exc).__name__}")

    path = get_config_path()

    # Read the raw document here rather than through load_config(): a file that
    # fails to parse must not be rewritten from a normalized skeleton.
    raw: dict = {}
    if os.path.exists(path):
        try:
            with open(path, encoding="utf-8") as fh:
                raw = yaml.safe_load(fh) or {}
        except Exception as exc:  # noqa: BLE001
            return _fail(f"existing config.yaml will not parse ({type(exc).__name__}); not touching it")
    if not isinstance(raw, dict):
        return _fail("existing config.yaml is not a mapping; not touching it")

    existing_servers = raw.get("mcp_servers")
    if existing_servers is not None and not isinstance(existing_servers, dict):
        return _fail("existing config.yaml 'mcp_servers' is not a mapping; not touching it")
    existing_servers = existing_servers or {}

    # Check if any server needs updating
    needs_update = False
    for name, desired in servers.items():
        if existing_servers.get(name) != desired:
            needs_update = True
            break

    if not needs_update:
        print(f"mcp-gateway-merge: all {len(servers)} server(s) already current ({len(existing_servers)} total in config)")
        return 0

    # Apply all server entries
    for name, desired in servers.items():
        existing_servers[name] = desired

    raw["mcp_servers"] = existing_servers
    try:
        atomic_config_write(path, raw, sort_keys=False)
    except Exception as exc:  # noqa: BLE001
        return _fail(f"atomic_config_write refused: {exc}")

    print(f"mcp-gateway-merge: wrote {len(servers)} server(s) ({len(existing_servers)} total in config)")
    return 0


if __name__ == "__main__":
    sys.exit(main())
