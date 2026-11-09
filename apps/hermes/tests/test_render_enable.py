#!/usr/bin/env python3
"""Render-level tests for the opt-in MCP gateway client (``global.mcpGateway``).

These are behavior tests against ``helm template`` output, not source greps.
The load-bearing assertions are:

* with the switch off, the chart renders **byte-for-byte** what the chart does
  at ``HEAD`` - so "off" really means "nothing of this feature exists";
* with the switch on, every pre-existing object (the Hermes StatefulSet first)
  is still rendered *unchanged*, and the only additions are the helper
  ConfigMap and Deployment;
* the switch is a real boolean that reacts to ``--set`` on the command line,
  and a non-boolean value fails the render instead of being coerced.

Run: ``python3 apps/hermes/tests/test_render_enable.py`` from anywhere.
"""

from __future__ import annotations

import os
import re
import subprocess
import sys
import tempfile

REPO = subprocess.run(["git", "rev-parse", "--show-toplevel"], capture_output=True,
                      text=True, check=True).stdout.strip()
CHART = os.path.join(REPO, "apps", "hermes")
FLAG = "global.mcpGateway.enabled"


def _helm(chart_dir: str, *extra: str) -> str:
    r = subprocess.run(["helm", "template", "--namespace", "hermes", "hermes", chart_dir, *extra],
                       capture_output=True, text=True)
    if r.returncode != 0:
        raise AssertionError(f"helm template failed: {r.stderr.strip()[:400]}")
    return r.stdout


def _docs(manifest: str) -> dict[tuple[str, str], str]:
    """Index rendered documents by (kind, metadata.name) as normalized YAML."""
    import yaml
    out: dict[tuple[str, str], str] = {}
    for doc in yaml.safe_load_all(manifest):
        if not doc or not isinstance(doc, dict) or not doc.get("kind"):
            continue
        key = (doc["kind"], doc["metadata"]["name"])
        assert key not in out, f"duplicate object {key}"
        out[key] = yaml.safe_dump(doc, sort_keys=True)
    return out


def _baseline_chart() -> str:
    """A pristine copy of the chart at the pre-feature baseline commit.

    Uses commit 004a96473 (mcp-gateway: Attach the Context7 API key to the context7 route)
    as the baseline before the DNS fix was added. This ensures tests compare against
    the state before hostAliases was introduced.

    ``git archive`` is read-only - it touches no index, no worktree and no refs.
    ``charts/`` is a build artifact (``helm dependency build``), not tracked, and
    the vendored ``app-template-5.3.0.tgz`` is the same one both charts resolve,
    so copying it keeps the comparison to *our* files only.
    """
    dest = tempfile.mkdtemp(prefix="hermes-baseline-chart-")
    subprocess.run(f"git -C {REPO} archive 004a96473 apps/hermes | tar -x -C {dest}",
                   shell=True, check=True)
    src = os.path.join(CHART, "charts")
    if os.path.isdir(src):
        dst = os.path.join(dest, "apps/hermes", "charts")
        os.makedirs(dst, exist_ok=True)
        for tgz in sorted(f for f in os.listdir(src) if f.endswith(".tgz")):
            with open(os.path.join(src, tgz), "rb") as fh:
                data = fh.read()
            with open(os.path.join(dst, tgz), "wb") as fh:
                fh.write(data)
    return os.path.join(dest, "apps/hermes")


def test_disabled_render_is_head_baseline():
    """``enabled: false`` must produce exactly the manifests HEAD produces.

    Exception: the hostAliases addition for mcp.grigri.cloud DNS override is
    present in the disabled render because values.yaml cannot use Helm template
    conditionals. The entry is harmless when unused (just an /etc/hosts entry
    that never gets queried).
    """
    import yaml
    baseline = _helm(_baseline_chart())
    disabled = _helm(CHART, "--set", f"{FLAG}=false")

    # Parse both renders
    baseline_docs = list(yaml.safe_load_all(baseline))
    disabled_docs = list(yaml.safe_load_all(disabled))

    # Filter out the hostAliases from the StatefulSet in disabled render
    for doc in disabled_docs:
        if doc and isinstance(doc, dict) and doc.get("kind") == "StatefulSet":
            pod_spec = doc.get("spec", {}).get("template", {}).get("spec", {})
            if "hostAliases" in pod_spec:
                # Check it's the expected mcp.grigri.cloud override
                host_aliases = pod_spec["hostAliases"]
                assert len(host_aliases) == 1
                assert host_aliases[0]["ip"] == "192.168.193.4"
                assert host_aliases[0]["hostnames"] == ["mcp.grigri.cloud"]
                # Remove it for comparison
                del pod_spec["hostAliases"]

    # Re-serialize and compare
    baseline_normalized = yaml.safe_dump_all(baseline_docs, sort_keys=True)
    disabled_normalized = yaml.safe_dump_all(disabled_docs, sort_keys=True)

    assert baseline_normalized == disabled_normalized, (
        "disabled render differs from the HEAD chart render "
        "(beyond the expected hostAliases addition for mcp.grigri.cloud)"
    )
    assert "mcp-gateway" not in disabled
    assert "mcpGateway" not in disabled
    print("✓ disabled render == HEAD baseline (normalized, +hostAliases for DNS override)")


def test_enabled_changes_only_the_two_cutover_scripts():
    """Enabled state: the paired cutover touches exactly two script strings.

    The architecture is *additive* for identity/routes (helper pod) and
    *substitutive* for the Grafana JWT client, because the legacy sidecar keeps
    working only while Grafana trusts the ``audience=grafana`` id_token. So the
    only allowed delta in the agent's own pod is the body of
    ``grafana-mcp-token-init`` and ``grafana-mcp-refresher``; containers,
    images, env, volumes, mounts and annotations must be untouched, and the
    legacy script text must survive verbatim in the off render (asserted by the
    previous test against the pre-feature baseline).

    Exception: the hostAliases addition for mcp.grigri.cloud DNS override is
    present in the enabled render. This is filtered out for comparison.
    """
    import yaml
    base = _helm(_baseline_chart())
    on = _helm(CHART, "--set", f"{FLAG}=true")
    base_sts = next(d for d in yaml.safe_load_all(base) if isinstance(d, dict) and d.get("kind") == "StatefulSet")
    on_sts = next(d for d in yaml.safe_load_all(on) if isinstance(d, dict) and d.get("kind") == "StatefulSet")
    pod_b, pod_o = base_sts["spec"]["template"]["spec"], on_sts["spec"]["template"]["spec"]

    # Filter out hostAliases from the enabled render for comparison
    pod_o_filtered = dict(pod_o)
    if "hostAliases" in pod_o_filtered:
        # Verify it's the expected mcp.grigri.cloud override
        host_aliases = pod_o_filtered["hostAliases"]
        assert len(host_aliases) == 1
        assert host_aliases[0]["ip"] == "192.168.193.4"
        assert host_aliases[0]["hostnames"] == ["mcp.grigri.cloud"]
        del pod_o_filtered["hostAliases"]

    CUT_OVER = {"grafana-mcp-token-init", "grafana-mcp-refresher"}
    for field in ("containers", "initContainers"):
        assert [c["name"] for c in pod_o_filtered[field]] == [c["name"] for c in pod_b[field]], f"{field} list changed"
        for cb, co in zip(pod_b[field], pod_o_filtered[field]):
            delta = {k for k in set(cb) | set(co) if cb.get(k) != co.get(k)}
            if cb["name"] in CUT_OVER:
                assert delta == {"args"}, f"{cb['name']} changed beyond its script: {delta}"
            else:
                assert not delta, f"{cb['name']} changed with the switch on: {delta}"
    assert pod_o_filtered["volumes"] == pod_b["volumes"], "no new volume or mount may appear"
    assert base_sts["spec"]["template"]["metadata"] == on_sts["spec"]["template"]["metadata"], "no annotation/label delta"
    for key, body in _docs(base).items():
        if key == ("StatefulSet", "hermes"):
            continue
        assert on_docs_equal(key, base, on), f"pre-existing object {key} changed unexpectedly"
    added = sorted(set(_docs(on)) - set(_docs(base)))
    assert added == [("ConfigMap", "hermes-mcp-gateway"), ("Deployment", "hermes-mcp-gateway")], added
    print("✓ enabled state: only the 2 cutover scripts differ + hostAliases DNS override, nothing else in the agent pod")


def on_docs_equal(key, base_manifest, on_manifest):
    import yaml
    def pick(manifest):
        return next((d for d in yaml.safe_load_all(manifest)
                     if isinstance(d, dict) and d.get("kind") == key[0]
                     and d.get("metadata", {}).get("name") == key[1]), None)
    a, b = pick(base_manifest), pick(on_manifest)
    return yaml.safe_dump(a, sort_keys=True) == yaml.safe_dump(b, sort_keys=True)


def test_cutover_scripts_are_gateway_mode_when_on():
    """On: gateway client + access_token, and no credential in curl's argv."""
    import yaml
    on = _helm(CHART, "--set", f"{FLAG}=true")
    sts = next(d for d in yaml.safe_load_all(on) if isinstance(d, dict) and d.get("kind") == "StatefulSet")
    pod = sts["spec"]["template"]["spec"]
    scripts = {c["name"]: c["args"][0]
               for c in pod["initContainers"] + pod["containers"]
               if c["name"] in {"grafana-mcp-token-init", "grafana-mcp-refresher"}}
    assert set(scripts) == {"grafana-mcp-token-init", "grafana-mcp-refresher"}
    for name, script in scripts.items():
        assert "client_id=mcp-gateway" in script, name
        assert "audience=mcp-gateway" in script, name
        assert "scope=mcp_grafana" in script, name
        assert "--data-binary @-" in script, name
        assert '\"access_token\"' in script or '"access_token":"[^"]*"' in script, name
        assert "-d \"subject_token=" not in script and "id_token" not in script, name
        assert "{{" not in script, "template left unrendered"
    # The off render keeps the legacy script verbatim (no gateway client strings).
    off = _helm(CHART, "--set", f"{FLAG}=false")
    assert "client_id=grafana" in off and "id_token" in off
    assert "client_id=mcp-gateway" not in off.split("kind: StatefulSet")[1], "gateway mode leaked into off render"
    print("✓ cutover scripts: gateway client/scope, stdin body, access_token; off keeps legacy verbatim")


def test_enabled_helper_pod_contract():
    """The helper pod must carry the identity, mounts and shape the design promises."""
    import yaml
    dep = next(d for d in yaml.safe_load_all(_helm(CHART, "--set", f"{FLAG}=true"))
               if isinstance(d, dict) and d.get("kind") == "Deployment")
    spec = dep["spec"]
    assert spec["strategy"]["type"] == "Recreate", "one writer at a time on a shared PVC"
    pod = spec["template"]["spec"]
    assert pod["automountServiceAccountToken"] is False, "no Kubernetes API identity for this pod"
    agent = next(d for d in yaml.safe_load_all(_helm(CHART, "--set", f"{FLAG}=true"))
                 if isinstance(d, dict) and d.get("kind") == "StatefulSet")
    # Co-location is a property of the volume (the RWO ZFS PV is hard-pinned to prusik), but the
    # pod must still not be schedulable somewhere else: assert it equals the agent's selector
    # rather than duplicating a hostname here, so the two can never drift independently.
    agent_sel = agent["spec"]["template"]["spec"].get("nodeSelector") or {}
    assert agent_sel, "expected the agent pod to be node-pinned in the first place"
    assert pod["nodeSelector"] == agent_sel, "helper must share the agent's nodeSelector"
    assert pod["runtimeClassName"] == agent["spec"]["template"]["spec"].get("runtimeClassName"), \
        "helper must share the agent's runtime class"
    assert pod["runtimeClassName"], "runtime class must not silently disappear"
    agent_image = next(c["image"] for c in agent["spec"]["template"]["spec"]["containers"]
                       if c["name"] == "hermes")
    # The helper pod has 4 containers: 1 busybox init (uid 0) + 3 hermes-agent (uid 10000)
    names = [c["name"] for c in pod["initContainers"]] + [c["name"] for c in pod["containers"]]
    assert names == ["create-mcp-tokens-dir", "mcp-gateway-token", "mcp-gateway-config", "mcp-gateway-token-refresher"], names
    for c in pod["initContainers"] + pod["containers"]:
        sc = c["securityContext"]
        # The busybox init container runs as uid 0 to create the directory;
        # the hermes-agent Python scripts run as uid 10000 (Hermes user).
        if c["name"] == "create-mcp-tokens-dir":
            assert sc["runAsUser"] == 0 and sc["runAsGroup"] == 0, "busybox init must run as uid 0"
            assert c["image"].startswith("busybox:"), "init container must use busybox"
        else:
            assert c["image"] == agent_image, "helpers must run the agent's Hermes build"
            assert sc["runAsUser"] == 10000 and sc["runAsGroup"] == 10000, "Python scripts must run as uid 10000"
            assert sc["readOnlyRootFilesystem"] is True and sc["allowPrivilegeEscalation"] is False
            assert sc["capabilities"]["drop"] == ["ALL"]
            # Token containers have 256Mi limit, config container has 128Mi
            if "token" in c["name"]:
                assert c["resources"]["limits"]["memory"] == "256Mi", f"{c['name']} memory limit"
            else:
                assert c["resources"]["limits"]["memory"] == "128Mi", f"{c['name']} memory limit"
        mounts = {m["mountPath"]: m for m in c["volumeMounts"]}
        assert "/opt/data" in mounts or "/mcp-gateway" in mounts, f"{c['name']} missing expected mount"
        # No secret may reach argv: the whole exchange is in-process Python.
        joined = " ".join([*c.get("command", []), *c.get("args", [])])
        assert "curl" not in joined and "subject_token" not in joined, joined
    pvc = [v for v in pod["volumes"] if v.get("persistentVolumeClaim")]
    assert [v["persistentVolumeClaim"]["claimName"] for v in pvc] == ["hermes-data"]
    # The workload credential arrives by Secret reference only, in the two
    # containers that actually exchange it.
    for c in pod["initContainers"] + pod["containers"]:
        if "mcp_gateway_token.py" not in " ".join(c.get("args", [])):
            continue
        for env in c.get("env", []):
            if env["name"] == "MCP_GATEWAY_SUBJECT_TOKEN":
                ref = env["valueFrom"]["secretKeyRef"]
                assert (ref["name"], ref["key"]) == ("grafana-mcp-token", "token"), ref
                break
        else:
            raise AssertionError(f"{c['name']} has no MCP_GATEWAY_SUBJECT_TOKEN")
    print("✓ helper pod: Recreate, no SA token mount, busybox uid 0 + hermes-agent uid 10000, PVC + secret ref, 256Mi/128Mi limits")


def test_enabled_configmap_shape():
    """The rendered ConfigMap must carry Hermes' own entry shape, twice."""
    import json
    import yaml
    cm = next(d for d in yaml.safe_load_all(_helm(CHART, "--set", f"{FLAG}=true"))
              if isinstance(d, dict) and d.get("kind") == "ConfigMap"
              and d["metadata"]["name"] == "hermes-mcp-gateway")
    cfg = json.loads(cm["data"]["config.json"])
    assert set(cfg["servers"]) == {"homelab-grafana", "homelab-context7"}
    for name, entry in cfg["servers"].items():
        assert entry["auth"] == "oauth"
        # Hermes reads the scope from the oauth sub-dict (config.get("oauth"));
        # a top-level "scope" would be ignored by the provider.
        assert entry["oauth"]["scope"].startswith("mcp_"), entry
        assert "scope" not in entry, "scope must live under oauth, where Hermes reads it"
        assert entry["url"].startswith("https://mcp.grigri.cloud/")
        assert entry["enabled"] is True
    ex = cfg["exchange"]
    assert ex["url"] == "https://idm.grigri.cloud/oauth2/token"
    assert ex["clientId"] == ex["audience"] == "mcp-gateway"
    assert ex["marginSeconds"] == 300 and ex["maxFailures"] == 3
    # Scripts ship in the same ConfigMap the pod mounts.
    assert set(cm["data"]) == {"config.json", "mcp_gateway_merge.py", "mcp_gateway_token.py"}
    # No credential material in a ConfigMap, ever.
    assert "subject_token" not in cm["data"]["config.json"]
    print("✓ ConfigMap: 2 routes with oauth.scope, exchange config, scripts, no secrets")


def test_string_flag_fails_closed():
    """A string flag must abort the render, not be quietly truthy-coerced."""
    r = subprocess.run(["helm", "template", "--namespace", "hermes", "hermes", CHART,
                        "--set-string", f"{FLAG}=false"], capture_output=True, text=True)
    assert r.returncode != 0, "a string 'false' must not render"
    assert "must be a boolean" in r.stderr, r.stderr[:300]
    print("✓ non-boolean flag fails the render")


def test_service_account_stays_opt_in():
    """The extra identity is off even when the feature is on."""
    out = _helm(CHART, "--set", f"{FLAG}=true")
    assert "hermes-mcp\n" not in out, "hermes-mcp SA must not render by default"
    on = _helm(CHART, "--set", f"{FLAG}=true", "--set", "global.mcpGateway.serviceAccount.enabled=true")
    assert re.search(r"^  name: hermes-mcp$", on, re.M), "SA renders when both flags are true"
    assert "kind: KanidmServiceAccount" in on
    print("✓ dedicated identity renders only with its own second flag")


def test_git_edit_and_cli_flag_are_the_same_path():
    """Editing values.yaml and ``--set`` must reach the same boolean."""
    with open(os.path.join(CHART, "values.yaml"), encoding="utf-8") as fh:
        original = fh.read()
    assert re.search(r"^  mcpGateway:\n    enabled: true$", original, re.M), "on by default in git"
    flipped = original.replace("    enabled: true\n", "    enabled: false\n", 1)
    with tempfile.NamedTemporaryFile("w", suffix=".yaml", delete=False) as fh:
        fh.write(flipped)
        overlay = fh.name
    try:
        from_git = _helm(CHART, "-f", overlay)
    finally:
        os.unlink(overlay)
    assert _docs(from_git) == _docs(_helm(CHART, "--set", f"{FLAG}=false"))
    print("✓ git edit and --set produce identical output")


if __name__ == "__main__":
    print("Running MCP gateway render tests...")
    for fn in [test_disabled_render_is_head_baseline,
               test_enabled_changes_only_the_two_cutover_scripts,
               test_cutover_scripts_are_gateway_mode_when_on,
               test_enabled_helper_pod_contract,
               test_enabled_configmap_shape,
               test_string_flag_fails_closed,
               test_service_account_stays_opt_in,
               test_git_edit_and_cli_flag_are_the_same_path]:
        fn()
    print("\n✓ All render tests passed")
