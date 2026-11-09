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


def test_ingress_selector_matches_real_gateway_labels():
    """Verify the ingress NetworkPolicy selects the actual gateway pod labels.

    The live public gateway pods are labelled:
      app.kubernetes.io/name=agentgateway-standalone
      app.kubernetes.io/instance=mcp-gateway
      app.kubernetes.io/component=standalone
    The old selector (app.kubernetes.io/name: mcp-gateway) matched nothing and would
    fail CLOSED if the disabled Firecrawl route were ever enabled.
    """
    result = subprocess.run(
        ["helm", "template", "--namespace", "firecrawl-mcp", "--include-crds", "firecrawl-mcp", "../"],
        capture_output=True,
        text=True,
        check=True,
    )

    manifests = [m for m in yaml.safe_load_all(result.stdout) if m is not None]
    ingress_policy = next(
        m for m in manifests
        if m.get("kind") == "NetworkPolicy" and m["metadata"]["name"] == "firecrawl-mcp-ingress"
    )

    ingress_rules = ingress_policy["spec"]["ingress"]
    assert len(ingress_rules) == 1, f"Expected 1 ingress rule, found {len(ingress_rules)}"

    from_selectors = ingress_rules[0]["from"]
    assert len(from_selectors) == 1, f"Expected 1 from selector, found {len(from_selectors)}"

    selector = from_selectors[0]

    # Verify namespace selector
    ns_selector = selector["namespaceSelector"]
    assert ns_selector["matchLabels"]["kubernetes.io/metadata.name"] == "mcp-gateway", \
        "Ingress namespace selector doesn't target mcp-gateway namespace"

    # Verify pod selector matches the real gateway labels
    pod_selector = selector["podSelector"]
    match_labels = pod_selector["matchLabels"]

    assert match_labels.get("app.kubernetes.io/name") == "agentgateway-standalone", \
        f"Ingress podSelector app.kubernetes.io/name is '{match_labels.get('app.kubernetes.io/name')}', expected 'agentgateway-standalone'"
    assert match_labels.get("app.kubernetes.io/instance") == "mcp-gateway", \
        f"Ingress podSelector app.kubernetes.io/instance is '{match_labels.get('app.kubernetes.io/instance')}', expected 'mcp-gateway'"
    assert match_labels.get("app.kubernetes.io/component") == "standalone", \
        f"Ingress podSelector app.kubernetes.io/component is '{match_labels.get('app.kubernetes.io/component')}', expected 'standalone'"

    # Verify port 3000
    ports = ingress_rules[0]["ports"]
    assert len(ports) == 1, f"Expected 1 port, found {len(ports)}"
    assert ports[0]["port"] == 3000, f"Ingress port is {ports[0]['port']}, expected 3000"
    assert ports[0]["protocol"] == "TCP", f"Ingress protocol is {ports[0]['protocol']}, expected TCP"

    print("✓ Ingress selector matches real gateway labels (agentgateway-standalone, mcp-gateway, standalone)")
    print("✓ Ingress port is 3000/TCP")


def test_egress_rules_unchanged():
    """Verify the egress NetworkPolicy rules are unchanged: DNS (node-local-dns + kube-dns) and firecrawl-api:3002."""
    result = subprocess.run(
        ["helm", "template", "--namespace", "firecrawl-mcp", "--include-crds", "firecrawl-mcp", "../"],
        capture_output=True,
        text=True,
        check=True,
    )

    manifests = [m for m in yaml.safe_load_all(result.stdout) if m is not None]
    egress_policy = next(
        m for m in manifests
        if m.get("kind") == "NetworkPolicy" and m["metadata"]["name"] == "firecrawl-mcp-egress"
    )

    egress_rules = egress_policy["spec"]["egress"]
    assert len(egress_rules) == 3, f"Expected 3 egress rules, found {len(egress_rules)}"

    # Rule 1: node-local-dns DNS
    rule1 = egress_rules[0]
    assert rule1["to"][0]["namespaceSelector"]["matchLabels"]["kubernetes.io/metadata.name"] == "kube-system"
    assert rule1["to"][0]["podSelector"]["matchLabels"]["k8s-app"] == "node-local-dns"
    rule1_ports = {(p["port"], p["protocol"]) for p in rule1["ports"]}
    assert (53, "UDP") in rule1_ports and (53, "TCP") in rule1_ports, \
        f"node-local-dns rule missing DNS ports, got {rule1_ports}"

    # Rule 2: kube-dns fallback
    rule2 = egress_rules[1]
    assert rule2["to"][0]["namespaceSelector"]["matchLabels"]["kubernetes.io/metadata.name"] == "kube-system"
    assert rule2["to"][0]["podSelector"]["matchLabels"]["k8s-app"] == "kube-dns"
    rule2_ports = {(p["port"], p["protocol"]) for p in rule2["ports"]}
    assert (53, "UDP") in rule2_ports and (53, "TCP") in rule2_ports, \
        f"kube-dns rule missing DNS ports, got {rule2_ports}"

    # Rule 3: firecrawl-api:3002
    rule3 = egress_rules[2]
    assert rule3["to"][0]["namespaceSelector"]["matchLabels"]["kubernetes.io/metadata.name"] == "firecrawl"
    assert rule3["to"][0]["podSelector"]["matchLabels"]["app.kubernetes.io/name"] == "firecrawl"
    assert rule3["to"][0]["podSelector"]["matchLabels"]["app.kubernetes.io/instance"] == "firecrawl"
    assert rule3["to"][0]["podSelector"]["matchLabels"]["app.kubernetes.io/controller"] == "api"
    rule3_ports = {(p["port"], p["protocol"]) for p in rule3["ports"]}
    assert (3002, "TCP") in rule3_ports, \
        f"firecrawl-api rule missing port 3002/TCP, got {rule3_ports}"

    print("✓ Egress rules unchanged: node-local-dns DNS, kube-dns fallback, firecrawl-api:3002")


if __name__ == "__main__":
    print("=== Firecrawl MCP Exec Probe Regression Tests ===\n")

    try:
        test_probes_are_exec_type()
        test_probes_use_node_http()
        test_networkpolicies_preserved()
        test_environment_variables_preserved()
        test_probe_timeouts_aligned()
        test_negative_cases()
        test_ingress_selector_matches_real_gateway_labels()
        test_egress_rules_unchanged()

        print("\n=== All regression tests passed ===")
        sys.exit(0)
    except AssertionError as e:
        print(f"\n✗ Test failed: {e}")
        sys.exit(1)
    except Exception as e:
        print(f"\n✗ Unexpected error: {e}")
        sys.exit(1)
