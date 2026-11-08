#!/usr/bin/env bash
# Static render tests for the mcp-canary chart. Renders each policyMode and asserts
# on the output: NetworkPolicy count/type/rules/ports/peers, no identity resources,
# no backend route, image/v1.7.0, nodeSelector prusik, ports, deterministic render,
# nonce rollout annotation.
#
# Usage: ./tests/render-tests.sh
# Exit 0 if all assertions pass, 1 otherwise.

set -euo pipefail

CHART_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
RELEASE="mcp-canary"
NAMESPACE="mcp-canary"

render() {
    local mode="$1"
    local nonce="${2:-0}"
    helm template "$RELEASE" "$CHART_DIR" \
        --namespace "$NAMESPACE" \
        --set "policyMode=$mode" \
        --set "rolloutNonce=$nonce" \
        --include-crds
}

assert_contains() {
    local output="$1"
    local pattern="$2"
    local msg="$3"
    if ! echo "$output" | grep -q "$pattern"; then
        echo "FAIL: $msg"
        echo "  expected pattern: $pattern"
        return 1
    fi
}

assert_not_contains() {
    local output="$1"
    local pattern="$2"
    local msg="$3"
    if echo "$output" | grep -q "$pattern"; then
        echo "FAIL: $msg"
        echo "  unexpected pattern: $pattern"
        return 1
    fi
}

assert_count() {
    local output="$1"
    local pattern="$2"
    local expected="$3"
    local msg="$4"
    local actual
    actual=$(echo "$output" | grep -c "$pattern" || true)
    if [ "$actual" -ne "$expected" ]; then
        echo "FAIL: $msg"
        echo "  expected $expected occurrences of '$pattern', got $actual"
        return 1
    fi
}

# Structural assertion: extract a YAML block and verify its structure
assert_yaml_structure() {
    local output="$1"
    local kind="$2"
    local name_pattern="$3"
    local field="$4"
    local expected_value="$5"
    local msg="$6"

    # Extract the YAML block for this kind+name
    local block
    block=$(echo "$output" | awk "/kind: $kind/,/^---$|^$/" | grep -A 100 "name: $name_pattern" | head -50)

    if ! echo "$block" | grep -q "$field: $expected_value"; then
        echo "FAIL: $msg"
        echo "  expected '$field: $expected_value' in $kind/$name_pattern"
        echo "  block:"
        echo "$block" | head -20
        return 1
    fi
}

echo "=== Test: policyMode=none renders no NetworkPolicy ==="
out=$(render none)
assert_count "$out" "kind: NetworkPolicy" 0 "none mode should render zero NetworkPolicy objects"
assert_contains "$out" "kind: Deployment" "none mode should render a Deployment"
assert_contains "$out" "kind: Service" "none mode should render a Service"
assert_contains "$out" "kind: ConfigMap" "none mode should render a ConfigMap"
assert_contains "$out" "cr.agentgateway.dev/agentgateway" "image should be agentgateway"
assert_contains "$out" "agentgateway:v1.7.0" "image tag should be v1.7.0"
assert_contains "$out" "kubernetes.io/hostname: prusik" "nodeSelector should pin to prusik"
assert_contains "$out" "port: 3000" "gateway port should be 3000"
assert_contains "$out" "port: 15021" "healthz port should be 15021"
assert_contains "$out" "path: /healthz/ready" "healthz path should be /healthz/ready"
assert_contains "$out" "mcp-canary.grigri.cloud/rollout-nonce" "rollout nonce annotation should be present"
assert_not_contains "$out" "KanidmOAuth2Client" "no KanidmOAuth2Client should be rendered"
assert_not_contains "$out" "KanidmGroup" "no KanidmGroup should be rendered"
assert_not_contains "$out" "kind: ExternalSecret" "no ExternalSecret should be rendered"
assert_not_contains "$out" "kind: Ingress" "no Ingress should be rendered"
# ServiceAccount is allowed (standard K8s resource, not identity)
echo "PASS"

echo ""
echo "=== Test: policyMode=ingress renders one ingress NetworkPolicy ==="
out=$(render ingress)
assert_count "$out" "kind: NetworkPolicy" 1 "ingress mode should render exactly one NetworkPolicy"
assert_contains "$out" "name: mcp-canary-ingress" "ingress NP should be named mcp-canary-ingress"
assert_contains "$out" "policyTypes:" "ingress NP should have policyTypes"
assert_contains "$out" "Ingress" "ingress NP should have Ingress policyType"
assert_not_contains "$out" "Egress" "ingress NP should not have Egress policyType"
assert_contains "$out" "kubernetes.io/metadata.name: ingress-nginx-external" "ingress NP should allow from ingress-nginx-external"
assert_contains "$out" "app.kubernetes.io/name: ingress-nginx" "ingress NP should select ingress-nginx pods"
assert_contains "$out" "port: 3000" "ingress NP should allow TCP3000"
assert_contains "$out" "kubernetes.io/metadata.name: monitoring" "ingress NP should allow from monitoring"
assert_contains "$out" "app.kubernetes.io/name: prometheus" "ingress NP should select prometheus pods"
assert_contains "$out" "port: 15020" "ingress NP should allow TCP15020"
# Extract just the NetworkPolicy section and verify 15021 is not in the ingress rules
np_section=$(echo "$out" | awk '/kind: NetworkPolicy/,/^---$|^$/' | grep -v "^---" | grep -v "^$")
if echo "$np_section" | grep -q "port: 15021"; then
    echo "FAIL: ingress NP should NOT allow TCP15021 (probe port)"
    echo "  found 'port: 15021' in NetworkPolicy section"
    exit 1
fi
# Verify podSelector matches canary labels only
assert_contains "$np_section" "app.kubernetes.io/instance: mcp-canary" "ingress NP podSelector should match canary instance"
assert_contains "$np_section" "app.kubernetes.io/name: mcp-canary" "ingress NP podSelector should match canary name"
echo "PASS"

echo ""
echo "=== Test: policyMode=egress renders one egress NetworkPolicy ==="
out=$(render egress)
assert_count "$out" "kind: NetworkPolicy" 1 "egress mode should render exactly one NetworkPolicy"
assert_contains "$out" "name: mcp-canary-egress" "egress NP should be named mcp-canary-egress"
assert_contains "$out" "policyTypes:" "egress NP should have policyTypes"
assert_contains "$out" "Egress" "egress NP should have Egress policyType"
assert_not_contains "$out" "Ingress" "egress NP should not have Ingress policyType"
assert_contains "$out" "k8s-app: node-local-dns" "egress NP should allow to node-local-dns"
assert_contains "$out" "k8s-app: kube-dns" "egress NP should allow to kube-dns"
assert_contains "$out" "protocol: UDP" "egress NP should allow UDP"
assert_contains "$out" "protocol: TCP" "egress NP should allow TCP"
assert_contains "$out" "port: 53" "egress NP should allow DNS port 53"
assert_contains "$out" "port: 443" "egress NP should allow TCP443"
assert_contains "$out" "port: 8443" "egress NP should allow TCP8443"
assert_contains "$out" "kubernetes.io/metadata.name: kanidm" "egress NP should allow to kanidm namespace"
# Verify podSelector matches canary labels only
np_section=$(echo "$out" | awk '/kind: NetworkPolicy/,/^---$|^$/' | grep -v "^---" | grep -v "^$")
assert_contains "$np_section" "app.kubernetes.io/instance: mcp-canary" "egress NP podSelector should match canary instance"
assert_contains "$np_section" "app.kubernetes.io/name: mcp-canary" "egress NP podSelector should match canary name"
echo "PASS"

echo ""
echo "=== Test: policyMode validation rejects invalid values ==="
for invalid in "both" "invalid" "INGRESS" "ingress+egress" ""; do
    output=$(helm template "$RELEASE" "$CHART_DIR" --namespace "$NAMESPACE" \
        --set "policyMode=$invalid" --set "rolloutNonce=0" 2>&1 || true)
    if echo "$output" | grep -q "policyMode must be one of"; then
        echo "  ✓ policyMode='$invalid' correctly rejected"
    else
        echo "FAIL: policyMode='$invalid' should be rejected but was accepted"
        echo "Output: $output" | head -5
        exit 1
    fi
done
echo "PASS"

echo ""
echo "=== Test: rolloutNonce annotation is present ==="
out=$(render none)
assert_contains "$out" "mcp-canary.grigri.cloud/rollout-nonce" "rollout nonce annotation should be present"
# Deterministic: same inputs -> same output
out1=$(render none)
out2=$(render none)
if [ "$out1" != "$out2" ]; then
    echo "FAIL: render is not deterministic"
    exit 1
fi
echo "PASS"

echo ""
echo "=== Test: no identity resources in any mode ==="
for mode in none ingress egress; do
    out=$(render "$mode")
    assert_not_contains "$out" "KanidmOAuth2Client" "$mode mode should not render KanidmOAuth2Client"
    assert_not_contains "$out" "KanidmGroup" "$mode mode should not render KanidmGroup"
    assert_not_contains "$out" "kind: ExternalSecret" "$mode mode should not render ExternalSecret"
    assert_not_contains "$out" "kind: Ingress" "$mode mode should not render Ingress"
    assert_not_contains "$out" "ClusterRole" "$mode mode should not render ClusterRole"
    assert_not_contains "$out" "ClusterRoleBinding" "$mode mode should not render ClusterRoleBinding"
    assert_not_contains "$out" "Role" "$mode mode should not render Role"
    assert_not_contains "$out" "RoleBinding" "$mode mode should not render RoleBinding"
done
echo "PASS"

echo ""
echo "=== Test: no backend routes in any mode ==="
for mode in none ingress egress; do
    out=$(render "$mode")
    assert_contains "$out" "routes: \[\]" "$mode mode should render empty routes"
done
echo "PASS"

echo ""
echo "=== Test: no NodePort/LoadBalancer/hostNetwork/hostPort ==="
for mode in none ingress egress; do
    out=$(render "$mode")
    assert_not_contains "$out" "type: NodePort" "$mode mode should not render NodePort service"
    assert_not_contains "$out" "type: LoadBalancer" "$mode mode should not render LoadBalancer service"
    assert_not_contains "$out" "hostNetwork: true" "$mode mode should not enable hostNetwork"
    assert_not_contains "$out" "hostPort:" "$mode mode should not use hostPort"
done
echo "PASS"

echo ""
echo "=== Test: writable /tmp emptyDir is mounted ==="
out=$(render none)
assert_contains "$out" "emptyDir" "emptyDir volume should be present"
assert_contains "$out" "sizeLimit: 64Mi" "emptyDir should have 64Mi size limit"
assert_contains "$out" "mountPath: /tmp" "emptyDir should be mounted at /tmp"
echo "PASS"

echo ""
echo "=== All tests passed ==="
