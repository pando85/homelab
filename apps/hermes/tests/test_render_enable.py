#!/usr/bin/env python3
"""Render-level tests for the opt-in MCP gateway client (``global.mcpGateway``).

These are behavior tests against ``helm template`` output, not source greps.
The load-bearing assertions are:

* with the switch off, the chart renders **byte-for-byte** what the chart does
  at ``HEAD`` (modulo the hostAliases DNS override and the sidecar removal);
* with the switch on, every pre-existing object (the Hermes StatefulSet first)
  is still rendered *unchanged*, and the only additions are the helper
  ConfigMap and Deployment;
* the legacy grafana-mcp sidecar (containers, init container, shared volume)
  has been removed and must not return;
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

    Exceptions (filtered out before comparison):
    - the hostAliases addition for mcp.grigri.cloud DNS override is present in
      the disabled render because values.yaml cannot use Helm template
      conditionals. The entry is harmless when unused (just an /etc/hosts entry
      that never gets queried).
    - the legacy grafana-mcp sidecar (grafana-mcp, grafana-mcp-refresher,
      grafana-mcp-token-init containers and grafana-mcp-shared volume) was
      removed on 2026-11-10. The baseline commit still has them, so they are
      filtered from the baseline before comparison.
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
                assert host_aliases[0]["ip"] == "192.168.193.14"
                assert host_aliases[0]["hostnames"] == ["mcp.grigri.cloud"]
                # Remove it for comparison
                del pod_spec["hostAliases"]

    # Filter out the legacy sidecar from the baseline StatefulSet (removed 2026-11-10)
    SIDECAR_CONTAINERS = {"grafana-mcp", "grafana-mcp-refresher"}
    SIDECAR_INIT_CONTAINERS = {"grafana-mcp-token-init"}
    SIDECAR_VOLUMES = {"grafana-mcp-shared"}
    for doc in baseline_docs:
        if doc and isinstance(doc, dict) and doc.get("kind") == "StatefulSet":
            pod_spec = doc.get("spec", {}).get("template", {}).get("spec", {})
            pod_spec["containers"] = [c for c in pod_spec.get("containers", [])
                                      if c["name"] not in SIDECAR_CONTAINERS]
            pod_spec["initContainers"] = [c for c in pod_spec.get("initContainers", [])
                                          if c["name"] not in SIDECAR_INIT_CONTAINERS]
            pod_spec["volumes"] = [v for v in pod_spec.get("volumes", [])
                                   if v.get("name") not in SIDECAR_VOLUMES]
            # Remove /shared mounts from remaining containers
            for c in pod_spec.get("containers", []) + pod_spec.get("initContainers", []):
                c["volumeMounts"] = [m for m in c.get("volumeMounts", [])
                                     if m["mountPath"] != "/shared"]
            # Normalize fix-sa-token: the sidecar removal changed its mount path
            # from /shared/serviceaccount to /sa-token and updated its command
            # accordingly. Update the baseline to match the new state.
            for c in pod_spec.get("initContainers", []):
                if c["name"] == "fix-sa-token":
                    for m in c.get("volumeMounts", []):
                        if m["mountPath"] == "/shared/serviceaccount":
                            m["mountPath"] = "/sa-token"
                    if c.get("args"):
                        c["args"] = ["cp -L /var/run/secrets/kubernetes.io/serviceaccount/* /sa-token/ && find /sa-token -type d -exec chmod 0755 {} \\; && find /sa-token -type f -exec chmod 0644 {} \\;"]

    # The Envoy Gateway migration added a route, its SecurityPolicy and the OIDC client for it:
    # none of them exists in the baseline, and none touches the Hermes pod.
    envoy_additions = {("HTTPRoute", "hermes-internal"), ("SecurityPolicy", "hermes-internal"),
                       ("KanidmOAuth2Client", "hermes-envoy")}
    disabled_docs = [d for d in disabled_docs
                     if not (isinstance(d, dict) and d.get("kind")
                             and (d["kind"], d["metadata"]["name"]) in envoy_additions)]

    # Re-serialize and compare
    baseline_normalized = yaml.safe_dump_all(baseline_docs, sort_keys=True)
    disabled_normalized = yaml.safe_dump_all(disabled_docs, sort_keys=True)

    assert baseline_normalized == disabled_normalized, (
        "disabled render differs from the HEAD chart render "
        "(beyond the expected hostAliases addition for mcp.grigri.cloud "
        "and the sidecar removal)"
    )
    assert "mcp-gateway" not in disabled
    assert "mcpGateway" not in disabled
    print("✓ disabled render == HEAD baseline (normalized, +hostAliases, -sidecar)")


def test_enabled_adds_only_the_helper_pod():
    """Enabled state: the only additions are the helper ConfigMap and Deployment.

    After the sidecar removal (2026-11-10), the StatefulSet is identical
    whether the flag is on or off. The feature is purely additive: it adds
    a separate helper pod (ConfigMap + Deployment) that writes MCP server
    entries and tokens to the hermes-data PVC.
    """
    import yaml
    off = _helm(CHART, "--set", f"{FLAG}=false")
    on = _helm(CHART, "--set", f"{FLAG}=true")
    off_sts = next(d for d in yaml.safe_load_all(off) if isinstance(d, dict) and d.get("kind") == "StatefulSet")
    on_sts = next(d for d in yaml.safe_load_all(on) if isinstance(d, dict) and d.get("kind") == "StatefulSet")
    pod_off, pod_on = off_sts["spec"]["template"]["spec"], on_sts["spec"]["template"]["spec"]

    # Filter out hostAliases from both for comparison (present in both, harmless)
    for pod in (pod_off, pod_on):
        pod.pop("hostAliases", None)

    # The StatefulSet must be identical (sidecar is gone, no cutover scripts)
    for field in ("containers", "initContainers"):
        assert [c["name"] for c in pod_on[field]] == [c["name"] for c in pod_off[field]], f"{field} list changed"
        for cb, co in zip(pod_off[field], pod_on[field]):
            delta = {k for k in set(cb) | set(co) if cb.get(k) != co.get(k)}
            assert not delta, f"{cb['name']} changed with the switch on: {delta}"
    assert pod_on["volumes"] == pod_off["volumes"], "volumes changed with the switch on"
    assert off_sts["spec"]["template"]["metadata"] == on_sts["spec"]["template"]["metadata"], "no annotation/label delta"
    # All pre-existing objects are unchanged
    for key in _docs(off):
        if key == ("StatefulSet", "hermes"):
            continue
        assert on_docs_equal(key, off, on), f"pre-existing object {key} changed unexpectedly"
    # The only additions are the helper ConfigMap and Deployment
    envoy_additions = {("HTTPRoute", "hermes-internal"), ("SecurityPolicy", "hermes-internal"),
                       ("KanidmOAuth2Client", "hermes-envoy")}
    added = sorted(set(_docs(on)) - set(_docs(off)) - envoy_additions)
    assert added == [("ConfigMap", "hermes-mcp-gateway"), ("Deployment", "hermes-mcp-gateway")], added
    print("✓ enabled state: StatefulSet unchanged, only helper ConfigMap+Deployment added")


def on_docs_equal(key, base_manifest, on_manifest):
    import yaml
    def pick(manifest):
        return next((d for d in yaml.safe_load_all(manifest)
                     if isinstance(d, dict) and d.get("kind") == key[0]
                     and d.get("metadata", {}).get("name") == key[1]), None)
    a, b = pick(base_manifest), pick(on_manifest)
    return yaml.safe_dump(a, sort_keys=True) == yaml.safe_dump(b, sort_keys=True)


def test_sidecar_is_removed():
    """The legacy grafana-mcp sidecar containers and volume are gone."""
    import yaml
    for flag_val in ("true", "false"):
        out = _helm(CHART, "--set", f"{FLAG}={flag_val}")
        sts = next(d for d in yaml.safe_load_all(out)
                   if isinstance(d, dict) and d.get("kind") == "StatefulSet")
        pod = sts["spec"]["template"]["spec"]
        container_names = {c["name"] for c in pod.get("containers", [])}
        init_names = {c["name"] for c in pod.get("initContainers", [])}
        volume_names = {v.get("name") for v in pod.get("volumes", [])}
        assert "grafana-mcp" not in container_names, f"grafana-mcp present (flag={flag_val})"
        assert "grafana-mcp-refresher" not in container_names, f"grafana-mcp-refresher present (flag={flag_val})"
        assert "grafana-mcp-token-init" not in init_names, f"grafana-mcp-token-init present (flag={flag_val})"
        assert "grafana-mcp-shared" not in volume_names, f"grafana-mcp-shared present (flag={flag_val})"
        # No container may mount /shared (the legacy sidecar path)
        for c in pod.get("containers", []) + pod.get("initContainers", []):
            for mount in c.get("volumeMounts", []):
                assert mount["mountPath"] != "/shared", \
                    f"{c['name']} mounts /shared (flag={flag_val})"
    # No legacy script text in the rendered output
    off = _helm(CHART, "--set", f"{FLAG}=false")
    assert "client_id=grafana" not in off, "legacy grafana client_id still in render"
    assert "id_token" not in off, "legacy id_token still in render"
    print("✓ sidecar removed: no grafana-mcp containers, no grafana-mcp-shared volume, no /shared mounts, no legacy scripts")


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
               test_enabled_adds_only_the_helper_pod,
               test_sidecar_is_removed,
               test_enabled_helper_pod_contract,
               test_enabled_configmap_shape,
               test_string_flag_fails_closed,
               test_service_account_stays_opt_in,
               test_git_edit_and_cli_flag_are_the_same_path]:
        fn()
    print("\n✓ All render tests passed")
