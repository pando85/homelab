#!/usr/bin/env python3
"""Foundation-specific checks for the public gateway deployment.

Validates the parts of the foundation that must stay true no matter which routes are
enabled - the pod, the probes, the session key, the ingress, the identity client:
  * nodeSelector is prusik (the only approved node for initial rollout);
  * the gateway's only MCP surface is `routes` (the `mcp:` convenience block is gone,
    so nothing can serve that this chart did not write out in full);
  * session key lifecycle is ESO CreatedOnce + immutable, and the Secret reference is
    unchanged (a rotated key invalidates live sessions, so it is asserted, not assumed);
  * probes are correct (startup/readiness on 15021 /healthz/ready);
  * JWT validation values in the RENDERED config are the exact Kanidm per-client
    issuer/audience/JWKS (the config is the only source - there is no second copy);
  * image pinned to v1.7.0, HTTPRoute (serving path via Envoy Gateway external) +
    Ingress nginx-external with TLS (retained rollback path), service ClusterIP only,
    adminAddr "off" (no admin listener), hardened container;
  * human scope grants carry no identity scopes and no person membership is declared;
  * SESSION_KEY is a required secretKeyRef (fail-closed).

Which routes exist, what they enforce, and the enabled-flag contract are covered by
route-checks.py (structure) and local-boot-checks.py (runtime, pinned binary).

    python3 test/mcp-gateway/foundation-checks.py
"""
import json
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
    values = yaml.safe_load(vals)

    # ---- nodeSelector ----
    dep = find(docs, "Deployment")
    node_sel = (dep or {}).get("spec", {}).get("template", {}).get("spec", {}).get("nodeSelector", {})
    check("nodeSelector is kubernetes.io/hostname: prusik",
          node_sel.get("kubernetes.io/hostname") == "prusik",
          str(node_sel))

    # ---- the only MCP surface is explicit routes ----
    check("no `mcp:` convenience block: every MCP route is written out in values.yaml",
          "mcp" not in cfg or not (cfg.get("mcp") or {}).get("targets"),
          f"mcp={cfg.get('mcp')!r}")
    prefixes = [next(iter(m["path"].items()))[1] for r in cfg.get("routes", [])
                for m in r["matches"] if "pathPrefix" in m["path"]]
    enabled = sorted(n for n, s in values["backends"].items() if s["enabled"])
    check("enabled backends in values match the MCP routes that exist",
          set(prefixes) == {values["backends"][n]["path"] for n in enabled},
          f"routes={prefixes} enabled={enabled}")
    check("grafana backend is enabled after the Grafana JWT cutover",
          values["backends"]["grafana"]["enabled"] is True)
    check("firecrawl backend is enabled (route live)",
          values["backends"]["firecrawl"]["enabled"] is True)

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

    # ---- JWT validation, read from the config that actually runs ----
    authns = [r["policies"]["mcpAuthentication"] for r in cfg.get("routes", [])
              if (r.get("policies") or {}).get("mcpAuthentication")]
    check("every MCP route authenticates against the exact Kanidm per-client issuer",
          authns and all(a["issuer"] == "https://idm.grigri.cloud/oauth2/openid/mcp-gateway"
                         for a in authns), f"{len(authns)} route(s)")
    check("every MCP route pins the audience to the OAuth2 client name",
          all(a["audiences"] == ["mcp-gateway"] for a in authns))
    check("every MCP route uses Kanidm's public_key.jwk (there is no /.well-known/jwks.json)",
          all(a["jwks"] == {"url": "https://idm.grigri.cloud/oauth2/openid/mcp-gateway/public_key.jwk"}
              for a in authns))
    check("every MCP route is strict (no anonymous path through the gate)",
          all(a.get("mode") == "strict" for a in authns))
    check("every MCP route is stateless at the backend (no gateway-issued session id)",
          all(b["mcp"]["statefulMode"] == "stateless" for r in cfg.get("routes", [])
              for b in r.get("backends", []) if "mcp" in b))

    # ---- image pin ----
    check("image tag is empty (use chart appVersion)",
          "tag: \"\"" in vals or "tag: ''" in vals)
    if main_container:
        image = main_container.get("image", "")
        check("Deployment image is cr.agentgateway.dev/agentgateway:v1.7.0",
              "cr.agentgateway.dev/agentgateway:v1.7.0" in image,
              image)

    # ---- serving path (HTTPRoute) and rollback path (Ingress) ----
    # The Envoy Gateway migration has ended. The HTTPRoute is the SERVING path for
    # mcp.grigri.cloud; the Ingress is retained as the documented rollback path
    # (docs/network/ingress.md, ingress-migration Phase 6). Both must exist and agree
    # on host and backend so a rollback is a DNS/annotation flip, not a chart change.
    hr = find(docs, "HTTPRoute")
    check("HTTPRoute exists (serving path via Envoy Gateway)", hr is not None)
    if hr:
        prefs = hr["spec"].get("parentRefs", [])
        check("HTTPRoute parentRef gateway is 'external' in namespace envoy-gateway",
              len(prefs) == 1 and prefs[0].get("name") == "external"
              and prefs[0].get("namespace") == "envoy-gateway",
              str(prefs))
        check("HTTPRoute sectionName is https-grigri-cloud",
              prefs[0].get("sectionName") == "https-grigri-cloud" if prefs else False)
        check("HTTPRoute hostname is mcp.grigri.cloud",
              hr["spec"].get("hostnames") == ["mcp.grigri.cloud"])
        rules = hr["spec"].get("rules", [])
        if rules:
            bref = rules[0].get("backendRefs", [{}])[0]
            check("HTTPRoute backend is Service mcp-gateway-standalone port 3000",
                  bref.get("name") == "mcp-gateway-standalone" and bref.get("port") == 3000,
                  f"name={bref.get('name')} port={bref.get('port')}")

    # The Ingress is the ROLLBACK path. It carries no traffic while the HTTPRoute is
    # attached to the Envoy Gateway, but must stay deployable so a rollback is instant.
    ing = find(docs, "Ingress")
    check("Ingress exists (retained rollback path, docs/network/ingress.md)", ing is not None)
    if ing:
        check("ingress className is nginx-external (rollback, not serving)",
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
    exacts = [next(iter(m["path"].items()))[1] for r in cfg.get("routes", [])
              for m in r["matches"] if "exact" in m["path"]]
    check("RFC 9728 discovery is gateway-served for every backend, enabled or not",
          set(exacts) == {"/.well-known/oauth-protected-resource/grafana/mcp",
                          "/.well-known/oauth-protected-resource/context7/mcp",
                          "/.well-known/oauth-protected-resource/firecrawl/mcp"},
          str(exacts))

    # ---- human scope grants: scopes only, never membership, never identity ----
    smap = {e["group"]: list(e["scopes"]) for e in
            values["kanidm"]["oauth2Client"]["humanScopeMap"]}
    check("human scope map grants MCP scopes and no identity scopes (openid/email/profile)",
          smap and all(not set(s) & {"openid", "email", "profile", "groups"} for s in smap.values()),
          str(smap))
    check("human scope map declares NO membership (people are managed in Kanidm, not in git)",
          not any(k in e for e in values["kanidm"]["oauth2Client"]["humanScopeMap"]
                  for k in ("members", "users", "persons")))

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
        # `members` on the Kanidm CRD is AUTHORITATIVE, so what renders here is the
        # whole membership of the group. It must stay exactly the declared list of
        # service accounts - no person, and nothing invented by this chart.
        declared = values["kanidm"]["workloadGroup"]["serviceMembers"]
        check("group members equal the declared service-account list",
              grp["spec"].get("members") == declared, str(grp["spec"].get("members")))
        check("the only member is the EXISTING grafana-mcp service account (no new SA)",
              declared == ["grafana-mcp"])

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
