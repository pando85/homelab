#!/usr/bin/env python3
"""Render the real gateway charts for the local test harnesses.

`e2e-authz.py` and `session-isolation-probe.py` both boot the pinned agentgateway
binary from what `helm template` prints, never from a hand-copied config, so a
template regression fails the tests instead of being papered over locally.

Every render goes through two steps:

  1. chart defaults, asserted fail-closed (`routes: []`), because shipping a
     gateway that answers anything before a route is approved is the one failure
     mode this foundation must not have;
  2. the same chart with test overrides (local ports, local issuer URL) plus the
     test-only `config.backend.allowDynamicLoopback: true`, which is required
     because the stub issuer and stub backend bind 127.0.0.1. Production keeps the
     default (false) and points at cluster service DNS names; the flag only gates
     *dynamic* backends, so it does not weaken the fixed route either way.
"""
import os
import subprocess

try:
    import yaml
except ModuleNotFoundError:  # pragma: no cover
    raise SystemExit("PyYAML is required: pip install pyyaml")

LOOPBACK_PATCH = ('  adminAddr: "off"\n',
                  '  adminAddr: "off"\n  backend:\n    allowDynamicLoopback: true\n')


def repo_root():
    return os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))


def extract_gateway_config(rendered):
    """Pull config.yaml out of the rendered ConfigMap."""
    for doc in yaml.safe_load_all(rendered):
        if doc and doc.get("kind") == "ConfigMap":
            return doc["data"]["config.yaml"]
    raise RuntimeError("no ConfigMap in rendered output")


def _helm(chart, release, namespace, sets):
    cmd = ["helm", "template", release, chart, "--namespace", namespace]
    for kv in sets:
        cmd += ["--set", kv]
    out = subprocess.run(cmd, cwd=repo_root(), capture_output=True, text=True, timeout=180)
    if out.returncode != 0:
        raise RuntimeError(f"helm template {' '.join(cmd[2:])} failed: {out.stderr[-900:]}")
    return out.stdout


def render(chart, release, namespace, sets, needles, quiet=False,
           stats_port=None, readiness_port=None):
    """Return the gateway config for `chart` with `sets` applied.

    `needles` are strings the rendered config must still contain, so a template
    edit that silently drops a policy fails here rather than as a confusing HTTP
    code later.

    `stats_port` / `readiness_port` are test-only: agentgateway binds 15020/15021
    by default, which means two gateways cannot run on one host - and the suites
    need the public and the admin plane up in the same process. Production leaves
    both at the defaults the Service and NetworkPolicy already name.
    """
    default_cfg = extract_gateway_config(_helm(chart, release, namespace, []))
    if "routes: []" not in default_cfg:
        raise RuntimeError(f"{chart}: default render is not fail-closed (no `routes: []`)")
    if not quiet:
        print(f"PASS  {chart} renders fail-closed by default (routes: [])")

    cfg = extract_gateway_config(_helm(chart, release, namespace, sets))
    for needle in needles:
        if needle not in cfg:
            raise RuntimeError(f"rendered config lost {needle!r}")

    before = cfg
    cfg = cfg.replace(*LOOPBACK_PATCH)
    if cfg == before:
        raise RuntimeError("config indentation changed; loopback patch did not apply")
    if stats_port or readiness_port:
        cfg = cfg.replace(
            '  adminAddr: "off"\n',
            '  adminAddr: "off"\n'
            + (f'  statsAddr: "127.0.0.1:{stats_port}"\n' if stats_port else "")
            + (f'  readinessAddr: "127.0.0.1:{readiness_port}"\n' if readiness_port else ""),
            1)
    if "session:" in cfg:
        # The session key is a Secret consumed through the SESSION_KEY env var
        # (config.rs prefers the env over any config value); a committed
        # `session.key` would be a static key in git.
        raise RuntimeError("chart must not set config.session; the key comes from SESSION_KEY")
    return cfg
