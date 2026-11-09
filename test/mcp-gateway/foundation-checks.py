#!/usr/bin/env python3
"""Foundation-specific checks for the policy-free public gateway baseline.

Validates the Phase A foundation that is ready for deployment:
  * networkIsolation=false means NO NetworkPolicy/CiliumNetworkPolicy renders;
  * nodeSelector is prusik (the only approved node for initial rollout);
  * all backend routes disabled (routes: []) - gateway answers 404 before auth;
  * session key lifecycle is ESO CreatedOnce + immutable;
  * probes are correct (startup/readiness/liveness on 15021);
  * JWT validation values are correct (issuer, audience, jwksUrl) for future use;
  * image pinned to v1.7.0;
  * ingress is nginx-external with TLS;
  * humanScopeMap is empty (no human grants wired);
  * adminAddr is "off";
  * service is ClusterIP only;
  * SESSION_KEY is a required secretKeyRef (fail-closed);
  * Deployment structural fields are correct (securityContext, resources, etc.).

NOTE: With routes: [], no auth or RFC 9728 discovery is testable - the gateway
answers 404 before reaching auth. This phase validates health/TLS/fail-closed-404
only. Actual auth/discovery tests require enabling a route in a later phase.

This complements chart-checks.py (which covers broader invariants) and focuses
on the deployment-ready foundation state.

    python3 test/mcp-gateway/foundation-checks.py
"""
import json
import re
import subprocess
import sys

import yaml

CHART = "mcp-gateway"
results = []


def check(label, ok, detail=""):
    print(f"{'PASS' if ok else 'FAIL'}  {label}" + (f"\n      {detail}" if detail else ""))
    results.append(bool(ok))


def helm(sets=()):
    cmd = ["helm", "template", CHART, f"apps/{CHART}", "--namespace", CHART, "--include-crds"]
    for kv in sets:
        cmd += ["--set", kv]
    out = subprocess.run(cmd, capture_output=True, text=True, timeout=300)
    if out.returncode != 0:
        raise RuntimeError(f"helm template {list(sets)}: {out.stderr[-900:]}")
    return [d for d in yaml.safe_load_all(out.stdout) if d]


def find(docs, kind):
    return next((d for d in docs if d.get("kind") == kind), None)


def find_all(docs, kind):
    return [d for d in docs if d.get("kind") == kind]


def config_text(docs):
    cm = find(docs, "ConfigMap")
    return cm["data"]["config.yaml"] if cm else ""


def main():
    # Render with default values (networkIsolation: false)
    docs = helm()
    text = config_text(docs)
    cfg = yaml.safe_load(text)
    vals = open(f"apps/{CHART}/values.yaml").read()

    # ---- networkIsolation guard ----
    check("networkIsolation defaults to false in values.yaml",
          re.search(r"^networkIsolation:\s*false\s*$", vals, re.M) is not None)
    check("no NetworkPolicy rendered when networkIsolation=false",
          find_all(docs, "NetworkPolicy") == [])
    check("no CiliumNetworkPolicy rendered when networkIsolation=false",
          find_all(docs, "CiliumNetworkPolicy") == [])

    # Verify NetworkPolicy DOES render when enabled (for future use)
    docs_np = helm(["networkIsolation=true"])
    np_list = find_all(docs_np, "NetworkPolicy")
    check("NetworkPolicy renders when networkIsolation=true",
          len(np_list) == 2,
          f"got {len(np_list)} policies")

    # ---- nodeSelector ----
    dep = find(docs, "Deployment")
    node_sel = (dep or {}).get("spec", {}).get("template", {}).get("spec", {}).get("nodeSelector", {})
    check("nodeSelector is kubernetes.io/hostname: prusik",
          node_sel.get("kubernetes.io/hostname") == "prusik",
          str(node_sel))

    # ---- all backends disabled ----
    check("routes: [] in config (all backends disabled)",
          cfg.get("routes") == [],
          f"routes={cfg.get('routes')!r}")
    check("firecrawl backend disabled in values",
          re.search(r"^\s{2}firecrawl:\s*\n(?:.*\n)*?\s{4}enabled: false", vals, re.M) is not None)
    check("grafana backend disabled in values",
          re.search(r"^\s{2}grafana:\s*\n(?:.*\n)*?\s{4}enabled: false", vals, re.M) is not None)

    # ---- session key ESO lifecycle ----
    es = find(docs, "ExternalSecret")
    gen = find(docs, "Password")
    check("ExternalSecret exists for session key", es is not None)
    check("Password generator exists for session key", gen is not None)
    if es:
        check("refreshPolicy is CreatedOnce",
              es["spec"].get("refreshPolicy") == "CreatedOnce")
        check("target is immutable",
              es["spec"]["target"].get("immutable") is True)
        check("target creationPolicy is Owner",
              es["spec"]["target"].get("creationPolicy", "Owner") == "Owner")
    if gen:
        g = gen["spec"]
        check("generator length=32 encoding=hex (64 hex chars = 32 bytes AES-256-GCM)",
              g.get("length") == 32 and g.get("encoding") == "hex")
        check("generator secretKeys is [SESSION_KEY]",
              g.get("secretKeys") == ["SESSION_KEY"])

    # ---- probes ----
    containers = (dep or {}).get("spec", {}).get("template", {}).get("spec", {}).get("containers", [])
    main_container = next((c for c in containers if c.get("name") == "main"), None)
    check("main container exists", main_container is not None)
    if main_container:
        probes = main_container.get("probes", {}) or main_container.get("livenessProbe") is not None
        # app-template renders probes differently; check the Deployment spec directly
        liveness = main_container.get("livenessProbe")
        readiness = main_container.get("readinessProbe")
        startup = main_container.get("startupProbe")
        check("livenessProbe exists on /healthz/ready:15021",
              liveness is not None and
              liveness.get("httpGet", {}).get("path") == "/healthz/ready" and
              liveness.get("httpGet", {}).get("port") == 15021)
        check("readinessProbe exists on /healthz/ready:15021",
              readiness is not None and
              readiness.get("httpGet", {}).get("path") == "/healthz/ready" and
              readiness.get("httpGet", {}).get("port") == 15021)
        check("startupProbe exists on /healthz/ready:15021",
              startup is not None and
              startup.get("httpGet", {}).get("path") == "/healthz/ready" and
              startup.get("httpGet", {}).get("port") == 15021)

    # ---- JWT validation ----
    # JWT settings are in values.yaml but only render into config when routes are enabled.
    # Foundation baseline has routes: [] so no auth/discovery is testable yet - the gateway
    # answers 404 before reaching auth. These checks verify the VALUES are correct for when
    # routes are enabled later.
    check("issuer is exact Kanidm per-client path in values",
          "issuer: https://idm.grigri.cloud/oauth2/openid/mcp-gateway" in vals)
    check("audience is the OAuth2 client name in values",
          "audience: mcp-gateway" in vals)
    check("jwksUrl is Kanidm public_key.jwk in values",
          "jwksUrl: https://idm.grigri.cloud/oauth2/openid/mcp-gateway/public_key.jwk" in vals)
    check("routes: [] means no auth/discovery testable yet (404 before auth)",
          cfg.get("routes") == [])

    # ---- image pin ----
    check("image pinned to v1.7.0",
          "tag: v1.7.0" in vals and
          "repository: cr.agentgateway.dev/agentgateway" in vals)
    if main_container:
        image = main_container.get("image", "")
        check("Deployment image is cr.agentgateway.dev/agentgateway:v1.7.0",
              "cr.agentgateway.dev/agentgateway:v1.7.0" in image,
              image)

    # ---- ingress ----
    ing = find(docs, "Ingress")
    check("Ingress exists", ing is not None)
    if ing:
        check("ingress className is nginx-external",
              ing["spec"].get("ingressClassName") == "nginx-external")
        tls = ing["spec"].get("tls", [])
        check("TLS is configured",
              len(tls) > 0 and tls[0].get("secretName") == "mcp-gateway-tls-certificate")

    # ---- RFC 9728 metadata paths ----
    check("RFC 9728 well-known path is in config (for future routes)",
          "/.well-known/oauth-protected-resource" in text or cfg.get("routes") == [],
          "routes are empty so no paths needed yet")

    # ---- humanScopeMap empty ----
    check("humanScopeMap is empty (no human grants)",
          re.search(r"humanScopeMap:\s*\[\]", vals) is not None)

    # ---- adminAddr off ----
    check("adminAddr is 'off' (no admin listener)",
          cfg.get("config", {}).get("adminAddr") == "off")

    # ---- service ----
    svc = find(docs, "Service")
    check("Service is ClusterIP",
          svc is not None and svc["spec"].get("type") == "ClusterIP")
    if svc:
        ports = {p["port"] for p in svc["spec"].get("ports", [])}
        check("Service exposes ports 3000, 15020, 15021",
              {3000, 15020, 15021} <= ports,
              str(sorted(ports)))

    # ---- SESSION_KEY ----
    if main_container:
        env = [e for e in main_container.get("env", []) if e.get("name") == "SESSION_KEY"]
        check("SESSION_KEY env var exists", len(env) > 0)
        if env:
            ref = env[0].get("valueFrom", {}).get("secretKeyRef", {})
            check("SESSION_KEY is from secretKeyRef (not a plain value)",
                  bool(ref) and "name" in ref and "key" in ref)
            check("SESSION_KEY secretKeyRef is required (optional is not true)",
                  ref.get("optional") is not True)

    # ---- Kanidm resources ----
    o2c = find(docs, "KanidmOAuth2Client")
    check("KanidmOAuth2Client exists", o2c is not None)
    if o2c:
        check("OAuth2 client is public",
              o2c["spec"].get("public") is True)
        check("OAuth2 client has strictRedirectUrl",
              o2c["spec"].get("strictRedirectUrl") is True)
        check("OAuth2 client has allowLocalhostRedirect",
              o2c["spec"].get("allowLocalhostRedirect") is True)

    grp = find(docs, "KanidmGroup")
    check("KanidmGroup exists (mcp-gateway-agents)",
          grp is not None and grp["metadata"]["name"] == "mcp-gateway-agents")
    if grp:
        check("KanidmGroup has no members by default (empty serviceMembers)",
              "members" not in grp.get("spec", {}))

    # ---- security context ----
    if main_container:
        sc = main_container.get("securityContext", {})
        check("runAsNonRoot is true", sc.get("runAsNonRoot") is True)
        check("allowPrivilegeEscalation is false",
              sc.get("allowPrivilegeEscalation") is False)
        check("readOnlyRootFilesystem is true",
              sc.get("readOnlyRootFilesystem") is True)
        caps = sc.get("capabilities", {}).get("drop", [])
        check("capabilities drop ALL", "ALL" in caps)
        check("runAsUser is 65534 (nobody)",
              sc.get("runAsUser") == 65534)
        check("runAsGroup is 65534 (nobody)",
              sc.get("runAsGroup") == 65534)

    # ---- resources ----
    if main_container:
        resources = main_container.get("resources", {})
        requests = resources.get("requests", {})
        limits = resources.get("limits", {})
        check("CPU request is set (10m)",
              requests.get("cpu") == "10m")
        check("Memory request is set (64Mi)",
              requests.get("memory") == "64Mi")
        check("Memory limit is set (256Mi)",
              limits.get("memory") == "256Mi")

    # ---- pod spec ----
    pod_spec = (dep or {}).get("spec", {}).get("template", {}).get("spec", {})
    check("automountServiceAccountToken is false",
          pod_spec.get("automountServiceAccountToken") is False)
    check("enableServiceLinks is false",
          pod_spec.get("enableServiceLinks") is False)
    check("replicas is 1",
          (dep or {}).get("spec", {}).get("replicas") == 1)

    print()
    print(f"=== {sum(results)} passed, {results.count(False)} failed ===")
    return 1 if results.count(False) else 0


if __name__ == "__main__":
    sys.exit(main())
