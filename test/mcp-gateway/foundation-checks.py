#!/usr/bin/env python3
"""Foundation-specific checks for the policy-free public gateway baseline.

Validates the Phase A foundation that is ready for deployment:
  * nodeSelector is prusik (the only approved node for initial rollout);
  * all backend routes disabled (mcp.targets: []) - gateway answers 404 before auth;
  * session key lifecycle is ESO CreatedOnce + immutable;
  * probes are correct (startup/readiness on 15021);
  * JWT validation values are correct (issuer, audience, jwksUrl) for future use;
  * image pinned to v1.7.0;
  * ingress is nginx-external with TLS;
  * humanScopeMap is empty (no human grants wired);
  * adminAddr is "off";
  * service is ClusterIP only;
  * SESSION_KEY is a required secretKeyRef (fail-closed);
  * Deployment structural fields are correct (securityContext, resources, etc.).

NOTE: With mcp.targets: [], no auth or RFC 9728 discovery is testable - the gateway
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
    # Render with default values
    docs = helm()
    text = config_text(docs)
    cfg = yaml.safe_load(text)
    vals = open(f"apps/{CHART}/values.yaml").read()

    # ---- nodeSelector ----
    dep = find(docs, "Deployment")
    node_sel = (dep or {}).get("spec", {}).get("template", {}).get("spec", {}).get("nodeSelector", {})
    check("nodeSelector is kubernetes.io/hostname: prusik",
          node_sel.get("kubernetes.io/hostname") == "prusik",
          str(node_sel))

    # ---- all backends disabled ----
    check("mcp.targets: [] in config (all backends disabled)",
          cfg.get("mcp", {}).get("targets") == [],
          f"mcp.targets={cfg.get('mcp', {}).get('targets')!r}")
    check("grafana backend disabled in values",
          re.search(r"^\s{2}grafana:\s*\n(?:.*\n)*?\s{4}enabled: false", vals, re.M) is not None)
    check("context7 backend disabled in values",
          re.search(r"^\s{2}context7:\s*\n(?:.*\n)*?\s{4}enabled: false", vals, re.M) is not None)

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
    main_container = next((c for c in containers if c.get("name") == "agentgateway"), None)
    check("agentgateway container exists", main_container is not None)
    if main_container:
        # Official chart uses startupProbe and readinessProbe (no livenessProbe)
        readiness = main_container.get("readinessProbe")
        startup = main_container.get("startupProbe")
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
    # Foundation baseline has mcp.targets: [] so no auth/discovery is testable yet - the gateway
    # answers 404 before reaching auth. These checks verify the VALUES are correct for when
    # routes are enabled later.
    check("issuer is exact Kanidm per-client path in values",
          "issuer: https://idm.grigri.cloud/oauth2/openid/mcp-gateway" in vals)
    check("audience is the OAuth2 client name in values",
          "audience: mcp-gateway" in vals)
    check("jwksUrl is Kanidm public_key.jwk in values",
          "jwksUrl: https://idm.grigri.cloud/oauth2/openid/mcp-gateway/public_key.jwk" in vals)
    check("mcp.targets: [] means no auth/discovery testable yet (404 before auth)",
          cfg.get("mcp", {}).get("targets") == [])

    # ---- image pin ----
    check("image tag is empty (use chart appVersion)",
          "tag: \"\"" in vals or "tag: ''" in vals)
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
        # Check backend service name
        rules = ing["spec"].get("rules", [])
        if rules:
            backend = rules[0].get("http", {}).get("paths", [{}])[0].get("backend", {})
            svc_name = backend.get("service", {}).get("name", "")
            check("Ingress backend points to mcp-gateway-standalone service",
                  svc_name == "mcp-gateway-standalone",
                  f"got {svc_name}")

    # ---- RFC 9728 metadata paths ----
    check("RFC 9728 well-known path is in config (for future routes)",
          "/.well-known/oauth-protected-resource" in text or cfg.get("mcp", {}).get("targets") == [],
          "targets are empty so no paths needed yet")

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
        check("Service name is mcp-gateway-standalone",
              svc["metadata"]["name"] == "mcp-gateway-standalone")

    # ---- SESSION_KEY ----
    if main_container:
        env = [e for e in main_container.get("env", []) if e.get("name") == "SESSION_KEY"]
        check("SESSION_KEY env var exists", len(env) > 0)
        if env:
            ref = env[0].get("valueFrom", {}).get("secretKeyRef", {})
            check("SESSION_KEY is from secretKeyRef (not a plain value)",
                  bool(ref) and "name" in ref and "key" in ref)
            check("SESSION_KEY references existing secret mcp-gateway-session-key",
                  ref.get("name") == "mcp-gateway-session-key")
            check("SESSION_KEY secretKeyRef key is SESSION_KEY",
                  ref.get("key") == "SESSION_KEY")
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
        # When serviceMembers is empty, the template omits the members field entirely
        # (owner-managed membership, no authoritative assertion)
        check("KanidmGroup has no members field when serviceMembers is empty",
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
    check("replicas is 1",
          (dep or {}).get("spec", {}).get("replicas") == 1)

    # ---- Deployment naming ----
    check("Deployment name is mcp-gateway-standalone (avoids immutable selector conflict)",
          dep["metadata"]["name"] == "mcp-gateway-standalone")
    dep_selector = (dep or {}).get("spec", {}).get("selector", {}).get("matchLabels", {})
    check("Deployment selector has component=standalone",
          dep_selector.get("app.kubernetes.io/component") == "standalone")

    print()
    print(f"=== {sum(results)} passed, {results.count(False)} failed ===")
    return 1 if results.count(False) else 0


if __name__ == "__main__":
    sys.exit(main())
