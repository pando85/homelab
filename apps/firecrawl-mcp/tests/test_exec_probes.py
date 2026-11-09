#!/usr/bin/env python3
"""
Regression tests for firecrawl-mcp exec probes.

Validates that the rendered Kubernetes manifest uses exec probes (not httpGet)
and that the Node.js health check script is correctly configured.

Run after helm template to verify the probe configuration.
"""

import subprocess
import sys
import yaml


def test_probes_are_exec_type():
    """Verify all probes (liveness, readiness, startup) are exec type, not httpGet."""
    result = subprocess.run(
        ["helm", "template", "--namespace", "firecrawl-mcp", "firecrawl-mcp", "../"],
        capture_output=True,
        text=True,
        check=True,
    )

    manifests = [m for m in yaml.safe_load_all(result.stdout) if m is not None]
    deployment = next(
        m for m in manifests if m.get("kind") == "Deployment"
    )

    container = deployment["spec"]["template"]["spec"]["containers"][0]

    # Check liveness probe
    assert "livenessProbe" in container, "livenessProbe missing"
    assert "exec" in container["livenessProbe"], "livenessProbe is not exec type"
    assert "httpGet" not in container["livenessProbe"], "livenessProbe still has httpGet"

    # Check readiness probe
    assert "readinessProbe" in container, "readinessProbe missing"
    assert "exec" in container["readinessProbe"], "readinessProbe is not exec type"
    assert "httpGet" not in container["readinessProbe"], "readinessProbe still has httpGet"

    # Check startup probe
    assert "startupProbe" in container, "startupProbe missing"
    assert "exec" in container["startupProbe"], "startupProbe is not exec type"
    assert "httpGet" not in container["startupProbe"], "startupProbe still has httpGet"

    print("✓ All probes are exec type")


def test_probes_use_node_http():
    """Verify all probes use Node.js http module with correct configuration."""
    result = subprocess.run(
        ["helm", "template", "--namespace", "firecrawl-mcp", "firecrawl-mcp", "../"],
        capture_output=True,
        text=True,
        check=True,
    )

    manifests = [m for m in yaml.safe_load_all(result.stdout) if m is not None]
    deployment = next(
        m for m in manifests if m.get("kind") == "Deployment"
    )

    container = deployment["spec"]["template"]["spec"]["containers"][0]

    for probe_name in ["livenessProbe", "readinessProbe", "startupProbe"]:
        probe = container[probe_name]
        exec_cmd = probe["exec"]["command"]

        # Verify command structure
        assert exec_cmd[0] == "node", f"{probe_name} doesn't use node"
        assert exec_cmd[1] == "-e", f"{probe_name} doesn't use -e flag"

        # Verify the script content
        script = exec_cmd[2]
        assert "require('http')" in script, f"{probe_name} doesn't require http module"
        assert "127.0.0.1" in script, f"{probe_name} doesn't use 127.0.0.1"
        assert "port:3000" in script, f"{probe_name} doesn't use port 3000"
        assert "/health" in script, f"{probe_name} doesn't check /health"
        assert "timeout:3000" in script, f"{probe_name} doesn't have 3000ms timeout"
        assert "statusCode>=200&&res.statusCode<300" in script, f"{probe_name} doesn't check 2xx range"
        assert "req.on('error'" in script, f"{probe_name} doesn't handle errors"
        assert "req.on('timeout'" in script, f"{probe_name} doesn't handle timeouts"

        print(f"✓ {probe_name} uses Node.js http module correctly")


def test_networkpolicies_preserved():
    """Verify both NetworkPolicies (ingress and egress) are present."""
    result = subprocess.run(
        ["helm", "template", "--namespace", "firecrawl-mcp", "--include-crds", "firecrawl-mcp", "../"],
        capture_output=True,
        text=True,
        check=True,
    )

    manifests = [m for m in yaml.safe_load_all(result.stdout) if m is not None]
    network_policies = [m for m in manifests if m.get("kind") == "NetworkPolicy"]

    assert len(network_policies) == 2, f"Expected 2 NetworkPolicies, found {len(network_policies)}"

    policy_names = {m["metadata"]["name"] for m in network_policies}
    assert "firecrawl-mcp-ingress" in policy_names, "firecrawl-mcp-ingress NetworkPolicy missing"
    assert "firecrawl-mcp-egress" in policy_names, "firecrawl-mcp-egress NetworkPolicy missing"

    print("✓ Both NetworkPolicies preserved")


def test_environment_variables_preserved():
    """Verify critical environment variables are present."""
    result = subprocess.run(
        ["helm", "template", "--namespace", "firecrawl-mcp", "firecrawl-mcp", "../"],
        capture_output=True,
        text=True,
        check=True,
    )

    manifests = [m for m in yaml.safe_load_all(result.stdout) if m is not None]
    deployment = next(
        m for m in manifests if m.get("kind") == "Deployment"
    )

    container = deployment["spec"]["template"]["spec"]["containers"][0]
    env_vars = {e["name"] for e in container.get("env", [])}

    required_vars = {
        "FIRECRAWL_API_URL",
        "FIRECRAWL_MCP_ALLOWED_HOSTS",
        "HTTP_STREAMABLE_SERVER",
        "HOST",
        "PORT",
        "HOME",
    }

    missing = required_vars - env_vars
    assert not missing, f"Missing environment variables: {missing}"

    print("✓ All critical environment variables preserved")


def test_probe_timeouts_aligned():
    """Verify Kubernetes timeoutSeconds provides buffer for Node.js startup + request."""
    result = subprocess.run(
        ["helm", "template", "--namespace", "firecrawl-mcp", "firecrawl-mcp", "../"],
        capture_output=True,
        text=True,
        check=True,
    )

    manifests = [m for m in yaml.safe_load_all(result.stdout) if m is not None]
    deployment = next(
        m for m in manifests if m.get("kind") == "Deployment"
    )

    container = deployment["spec"]["template"]["spec"]["containers"][0]

    for probe_name in ["livenessProbe", "readinessProbe", "startupProbe"]:
        probe = container[probe_name]

        # Kubernetes timeoutSeconds
        k8s_timeout = probe.get("timeoutSeconds", 1)

        # Node.js timeout in script
        script = probe["exec"]["command"][2]
        assert "timeout:3000" in script, f"{probe_name} Node.js timeout not 3000ms"

        # Verify Kubernetes timeout provides buffer for Node startup
        # K8s timeout must be >= 5s to allow for Node.js startup (~500ms) + request (3s)
        assert k8s_timeout >= 5, f"{probe_name} timeoutSeconds {k8s_timeout} < 5s (needs buffer for Node startup)"

        # Verify K8s timeout > Node timeout (prevents race condition)
        k8s_timeout_ms = k8s_timeout * 1000
        assert k8s_timeout_ms > 3000, f"{probe_name} K8s timeout {k8s_timeout}s <= Node timeout 3s (race condition)"

        print(f"✓ {probe_name} timeouts aligned (K8s: {k8s_timeout}s, Node: 3000ms, buffer: {k8s_timeout_ms - 3000}ms)")


def test_negative_cases():
    """Test that probe script exits non-zero on failure conditions."""
    # Extract the script from values.yaml
    with open("../values.yaml") as f:
        values = yaml.safe_load(f)

    probe_spec = values["app-template"]["controllers"]["firecrawl-mcp"]["containers"]["main"]["probes"]["liveness"]["spec"]
    script = probe_spec["exec"]["command"][2]

    # Test 1: Script should exit 1 on non-2xx status
    assert "process.exit(res.statusCode>=200&&res.statusCode<300?0:1)" in script, \
        "Script doesn't exit 1 on non-2xx status"

    # Test 2: Script should exit 1 on error
    assert "req.on('error',()=>process.exit(1))" in script, \
        "Script doesn't exit 1 on error"

    # Test 3: Script should exit 1 on timeout
    assert "req.on('timeout',()=>{req.destroy();process.exit(1)})" in script, \
        "Script doesn't exit 1 on timeout"

    print("✓ Negative cases handled correctly (non-2xx, error, timeout)")


if __name__ == "__main__":
    print("=== Firecrawl MCP Exec Probe Regression Tests ===\n")

    try:
        test_probes_are_exec_type()
        test_probes_use_node_http()
        test_networkpolicies_preserved()
        test_environment_variables_preserved()
        test_probe_timeouts_aligned()
        test_negative_cases()

        print("\n=== All regression tests passed ===")
        sys.exit(0)
    except AssertionError as e:
        print(f"\n✗ Test failed: {e}")
        sys.exit(1)
    except Exception as e:
        print(f"\n✗ Unexpected error: {e}")
        sys.exit(1)
