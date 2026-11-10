#!/usr/bin/env python3
"""Route checks for the public MCP gateway: what the chart renders must equal what was proven.

Structural, offline, deterministic. It renders the chart and asserts the gateway
config against the exact strings validated by the local runtime proofs (39/39 access-gate
and 21/21 discovery), so a reviewer can compare the committed config with the measured one
character by character. No secret value appears in this file or in its output.

  python3 test/mcp-gateway/route-checks.py

Covers:
  1. the enabled/disabled contract: `backends.<name>.enabled` and the rendered MCP routes
     agree in BOTH directions (so a flag can never claim a route is off while it serves,
     and each backend is an independent switch - context7 goes live alone);
  2. context7 route: strict Kanidm issuer/audience/JWKS, per-route resource URL and scope,
     fail-closed authorization rules, the full credential/URL-override strip list, no
     backendAuth, stateless;
  3. the grafana route (live since the cutover): read from the rendered config, with
     backendAuth passthrough, its own per-route scope, and pointing at the canonical
     in-cluster host;
  4. RFC 9728 discovery: both exact paths, gateway-served, literal JSON body (NOT base64),
     CEL-quoted headers, resource = the absolute route URL, authorization_servers = the
     Kanidm per-client issuer, per-route scopes_supported, and paths disjoint from the MCP
     pathPrefix (a prefix collision would silently swallow the MCP route or the metadata);
  5. isolation: networkIsolation is true and exactly one CiliumNetworkPolicy (no native
     NetworkPolicy) is rendered, admitting ingress-nginx and the external Envoy proxies;
  6. what must NOT have moved: prusik pin, session-key Secret/ExternalSecret/Password,
     probes, HTTPRoute serving path + Ingress rollback path, ClusterIP-only service,
     no admin listener.
"""
import json
import subprocess
import sys

import yaml

CHART = "mcp-gateway"
RESULTS = []

ISSUER = "https://idm.grigri.cloud/oauth2/openid/mcp-gateway"
JWKS = ISSUER + "/public_key.jwk"
HOST = "mcp.grigri.cloud"
PUBLIC = "https://mcp.grigri.cloud"
WELL_KNOWN = "/.well-known/oauth-protected-resource"


def check(label, ok, detail=""):
    print(f"{'PASS' if ok else 'FAIL'}  {label}" + (f"\n      {detail}" if detail else ""))
    RESULTS.append(bool(ok))


def helm():
    out = subprocess.run(
        ["helm", "template", CHART, f"apps/{CHART}", "--namespace", CHART, "--include-crds"],
        capture_output=True, text=True, timeout=300, check=False,
    )
    if out.returncode != 0:
        raise RuntimeError(f"helm template: {out.stderr[-900:]}")
    return [d for d in yaml.safe_load_all(out.stdout) if d]


def find(docs, kind):
    return next((d for d in docs if d.get("kind") == kind), None)


def by_name(routes, name):
    return next((r for r in routes if r.get("name") == name), None)


def authn_resource(route):
    """The resource URL mcpAuthentication puts in its 401 challenge, if the route has one."""
    if not route:
        return None
    return ((route.get("policies") or {}).get("mcpAuthentication") or {}) \
        .get("resourceMetadata", {}).get("resource")


def path_of(route):
    """The single match's path condition as ('exact'|'pathPrefix'|..., value)."""
    return tuple(next(iter(m["path"].items())) for m in route["matches"])


def main():
    docs = helm()
    cm = find(docs, "ConfigMap")
    cfg = yaml.safe_load(cm["data"]["config.yaml"])
    routes = cfg["routes"]
    values_text = open(f"apps/{CHART}/values.yaml").read()
    values = yaml.safe_load(values_text)
    sub = values["agentgateway-standalone"]
    backends = values["backends"]

    check("config is nested under the official chart values", bool(sub.get("config")))
    check("startup settings live under config.config (live lesson: no adminAddr at LocalConfig root)",
          cfg["config"].get("adminAddr") == "off" and cfg["config"].get("storage") == {"mode": "file"},
          str(cfg["config"]))
    check("chart-owned sections are not re-declared (storage injected by the chart)",
          "storage" in cfg["config"] and "database" not in cfg["config"])
    check("gateway listener is port 3000 (matches the Service targetPort)",
          sub["config"]["gateways"] == {"default": {"port": 3000}})

    # ---------------- 1. enabled/disabled contract, both directions ----------------
    by_path = {}
    for r in routes:
        kind, value = path_of(r)[0]
        if kind == "pathPrefix":
            by_path[value] = r
    for name, spec in backends.items():
        route = by_name(routes, name)
        wants = spec["path"]
        if spec["enabled"]:
            check(f"{name}: enabled=true renders exactly one MCP route at {wants}",
                  route is not None and path_of(route) == (("pathPrefix", wants),),
                  f"route={route and path_of(route)}")
        else:
            check(f"{name}: enabled=false renders NO MCP route (flag off -> nothing serves)",
                  route is None and wants not in by_path,
                  f"rendered prefixes={sorted(by_path)}")
    for prefix, route in by_path.items():
        name = route["name"]
        check(f"{name}: a rendered MCP route is not enabled behind its flag",
              name in backends and backends[name]["enabled"] is True,
              f"pathPrefix={prefix}")

    # ---------------- 2. context7 (LIVE) ----------------
    ctx = by_name(routes, "context7")
    pol = ctx["policies"]
    authn = pol["mcpAuthentication"]
    check("context7 attaches to the named gateway (top-level routes, not nested under gateways)",
          ctx.get("gateways") == ["default"], str(ctx.get("gateways")))
    check("context7 issuer is the exact Kanidm per-client path", authn["issuer"] == ISSUER)
    check("context7 audience is pinned to the OAuth2 client name",
          authn["audiences"] == ["mcp-gateway"])
    check("context7 JWKS URL is Kanidm's public_key.jwk (no /.well-known/jwks.json: 404 there)",
          authn["jwks"] == {"url": JWKS})
    check("context7 mode is strict (a missing bearer is refused, not anonymous)",
          authn.get("mode") == "strict")
    check("context7 resourceMetadata is required and holds the absolute route URL",
          authn["resourceMetadata"] == {"resource": f"{PUBLIC}/context7/mcp",
                                        "scopesSupported": ["mcp_context7"]},
          str(authn["resourceMetadata"]))
    rules = [r["require"] for r in pol["authorization"]["rules"]]
    check("authorization is fail-closed: no identity claims, then the exact singular scope",
          rules == [
              "!has(jwt.email)",
              "!has(jwt.preferred_username)",
              "has(jwt.scope) && jwt.scope.split(' ').exists(s, s == 'mcp_context7')",
          ], json.dumps(rules))
    check("scope match splits the space-separated string (Kanidm emits one string, not an array)",
          any("jwt.scope.split(' ').exists(s, s == 'mcp_context7')" in r for r in rules))
    removed = pol["requestHeaderModifier"]["remove"]
    for h in ("Authorization", "Cookie", "X-Grafana-URL", "X-Grafana-Service-Account-Token"):
        check(f"context7 strips {h} at the edge", h in removed, str(removed))
    want_ba = {"key": {"value": {"file": "/etc/agentgateway/secrets/context7/api-key"},
                       "location": {"header": {"name": "Authorization", "prefix": "Bearer "}}}}
    check("context7 sends the upstream API key read from the mounted Secret file (never inline)",
          pol.get("backendAuth") == want_ba, json.dumps(pol.get("backendAuth")))
    check("context7 has NO passthrough: the caller's bearer can never reach the upstream",
          "passthrough" not in (pol.get("backendAuth") or {}),
          json.dumps(sorted((pol.get("backendAuth") or {}).keys())))
    check("the key is injected into a header the edge already strips, so exactly one reaches it",
          want_ba["key"]["location"]["header"]["name"] in removed, str(removed))
    be = ctx["backends"][0]["mcp"]
    check("context7 backend is stateless (gateway issues no Mcp-Session-Id)",
          be["statefulMode"] == "stateless")
    tgt = be["targets"][0]
    check("context7 target is the hosted server, split host/port/path (no URI guessing)",
          tgt["mcp"] == {"host": "mcp.context7.com", "port": 443, "path": "/mcp"}, str(tgt["mcp"]))
    check("context7 target declares backendTLS explicitly",
          tgt.get("policies", {}).get("backendTLS") == {}, str(tgt.get("policies")))
    check("context7 route is not host-pinned (a client may SNI any name the ingress forwards)",
          "hostnames" not in ctx)

    # ---------------- 2b. firecrawl (LIVE) ----------------
    fc = by_name(routes, "firecrawl")
    check("firecrawl route exists (enabled=true)",
          fc is not None, "firecrawl route should be rendered when backend is enabled")
    if fc:
        fpol = fc["policies"]
        fauthn = fpol["mcpAuthentication"]
        check("firecrawl route matches its flag values (path, scope, resource)",
              path_of(fc) == (("pathPrefix", backends["firecrawl"]["path"]),)
              and fauthn["resourceMetadata"]["resource"] == backends["firecrawl"]["resource"],
              str(path_of(fc)))
        check("firecrawl issuer is the exact Kanidm per-client path",
              fauthn["issuer"] == ISSUER)
        check("firecrawl audience is pinned to the OAuth2 client name",
              fauthn["audiences"] == ["mcp-gateway"])
        check("firecrawl JWKS URL is Kanidm's public_key.jwk",
              fauthn["jwks"] == {"url": JWKS})
        check("firecrawl mode is strict",
              fauthn.get("mode") == "strict")
        check("firecrawl resourceMetadata holds the absolute route URL and its scope",
              fauthn["resourceMetadata"] == {"resource": f"{PUBLIC}/firecrawl/mcp",
                                            "scopesSupported": ["mcp_firecrawl"]},
              str(fauthn["resourceMetadata"]))
        frules = [r["require"] for r in fpol["authorization"]["rules"]]
        check("firecrawl authorization is fail-closed: no identity claims, then mcp_firecrawl",
              frules == [
                  "!has(jwt.email)",
                  "!has(jwt.preferred_username)",
                  "has(jwt.scope) && jwt.scope.split(' ').exists(s, s == 'mcp_firecrawl')",
              ], json.dumps(frules))
        fremoved = fpol["requestHeaderModifier"]["remove"]
        for h in ("Authorization", "Cookie"):
            check(f"firecrawl strips {h} at the edge", h in fremoved, str(fremoved))
        check("firecrawl has NO backendAuth (no upstream credential injected)",
              fpol.get("backendAuth") is None,
              json.dumps(fpol.get("backendAuth")))
        fbe = fc["backends"][0]["mcp"]
        check("firecrawl backend is stateless",
              fbe["statefulMode"] == "stateless")
        ftgt = fbe["targets"][0]
        check("firecrawl target is the in-cluster service, split host/port/path",
              ftgt["mcp"] == {"host": "firecrawl-mcp.firecrawl-mcp.svc.cluster.local",
                              "port": 3000, "path": "/mcp"}, str(ftgt["mcp"]))
        check("firecrawl target has no backendTLS (plain HTTP inside the cluster)",
              "backendTLS" not in (ftgt.get("policies") or {}))

    # ---------------- 3. grafana (PRESENT, LIVE) ----------------
    # The grafana route is live (enabled=true), so we check it directly from the rendered config
    graf = by_name(routes, "grafana")
    check("grafana route is live (enabled=true)",
          graf is not None, "grafana route should be rendered when backend is enabled")
    if graf:
        gpol = graf["policies"]
        check("grafana route matches its flag values (path, scope, resource)",
              path_of(graf) == (("pathPrefix", backends["grafana"]["path"]),)
              and graf["policies"]["mcpAuthentication"]["resourceMetadata"]["resource"]
              == backends["grafana"]["resource"],
              str(path_of(graf)))
        check("grafana route validates its own scope, not context7's",
              "mcp_grafana" in json.dumps(gpol["authorization"]))
        check("grafana forwards the caller's validated bearer (per-caller backend auth)",
              gpol.get("backendAuth") == {"passthrough": {}})
        check("grafana disables automatic port-dropping authority rewrite",
              gpol.get("urlRewrite") == {"authority": {
                  "full": "grafana-mcp.grafana-mcp.svc.cluster.local:8000"}})
        gremoved = gpol["requestHeaderModifier"]["remove"]
        check("grafana strips the URL/credential overrides mcp-grafana honours",
              all(h in gremoved for h in ("Cookie", "X-Grafana-URL", "X-Grafana-Service-Account-Token")),
              str(gremoved))
        check("grafana strips its own inbound credential before backendAuth replaces it",
              "Authorization" in gremoved, str(gremoved))
        gtgt = graf["backends"][0]["mcp"]["targets"][0]
        check("grafana target is the canonical in-cluster host the backend allowlists",
              gtgt["mcp"] == {"host": "grafana-mcp.grafana-mcp.svc.cluster.local",
                              "port": 8000, "path": "/mcp"}, str(gtgt["mcp"]))
        check("grafana target has no backendTLS (plain HTTP inside the cluster)",
              "backendTLS" not in (gtgt.get("policies") or {}))
        check("grafana target pins Host without broadening the backend allowlist",
              (gtgt.get("policies") or {}).get("requestHeaderModifier")
              == {"set": {"Host": "grafana-mcp.grafana-mcp.svc.cluster.local:8000"}})

    # ---------------- 4. RFC 9728 discovery ----------------
    meta = [r for r in routes if "metadata" in r["name"]]
    check("all three discovery routes are rendered (context7, grafana, firecrawl)",
          {r["name"] for r in meta} == {"context7-metadata", "grafana-metadata", "firecrawl-metadata"})
    for r in meta:
        name = r["name"].replace("-metadata", "")
        spec = backends[name]
        kind, value = path_of(r)[0]
        check(f"{name}: discovery path is EXACT {WELL_KNOWN}/{name}/mcp",
              kind == "exact" and value == f"{WELL_KNOWN}/{name}/mcp", f"{kind}={value}")
        check(f"{name}: discovery path is disjoint from the MCP pathPrefix",
              not str(value).startswith(spec["path"]) and not spec["path"].startswith(value))
        check(f"{name}: discovery is pinned to the public vhost",
              r.get("hostnames") == [HOST], str(r.get("hostnames")))
        check(f"{name}: discovery needs no authorization (public advertisement)",
              set(r["policies"]) == {"directResponse"}, str(sorted(r["policies"])))
        dr = r["policies"]["directResponse"]
        check(f"{name}: directResponse answers 200 with a body and no bodyExpression",
              dr.get("status") == 200 and "body" in dr and "bodyExpression" not in dr)
        body = dr["body"]
        check(f"{name}: body is LITERAL JSON, not the base64 an export would show",
              body.lstrip().startswith("{"), body[:24].replace("\n", " "))
        doc = json.loads(body)
        check(f"{name}: resource is the absolute route URL",
              doc["resource"] == spec["resource"], doc["resource"])
        check(f"{name}: authorization_servers is the Kanidm per-client issuer",
              doc["authorization_servers"] == [ISSUER], str(doc["authorization_servers"]))
        check(f"{name}: scopes_supported is exactly this route's scope",
              doc["scopes_supported"] == [spec["scope"]], str(doc.get("scopes_supported")))
        check(f"{name}: bearer_methods_supported is header",
              doc["bearer_methods_supported"] == ["header"])
        check(f"{name}: the served document and the 401 challenge name the same resource",
              doc["resource"] == (authn_resource(by_name(routes, name))
                                  or (name == "grafana" and authn_resource(graf))),
              doc["resource"])
        check(f"{name}: headers are CEL expressions, so literals are inner-quoted",
              dr["headers"] == {"content-type": "'application/json'",
                                "cache-control": "'public, max-age=300'"}, str(dr["headers"]))

    # ---------------- 5. isolation: the measured CNP is enabled ----------------
    check("networkIsolation is true (measured CiliumNetworkPolicy is live)",
          values["networkIsolation"] is True)
    kinds = {d["kind"] for d in docs}
    cnps = [d for d in docs if d["kind"] == "CiliumNetworkPolicy"]
    check("exactly one CiliumNetworkPolicy rendered", len(cnps) == 1, str(sorted(kinds)))
    check("no native NetworkPolicy rendered", "NetworkPolicy" not in kinds)
    if cnps:
        app_sel = [ep.get("matchLabels", {})
                   for r in cnps[0]["spec"].get("ingress", [])
                   if any(str(p.get("port")) == "3000"
                          for tp in r.get("toPorts", []) for p in tp.get("ports", []))
                   for ep in r.get("fromEndpoints", [])]
        check("CNP admits ingress-nginx and the external Envoy Gateway proxies on 3000",
              any(m.get("app.kubernetes.io/name") == "ingress-nginx" for m in app_sel) and
              any(m.get("app.kubernetes.io/name") == "envoy" and
                  m.get("gateway.envoyproxy.io/owning-gateway-name") == "external" and
                  m.get("io.kubernetes.pod.namespace") == "envoy-gateway" for m in app_sel),
              str(app_sel))

    # ---------------- 6. what did not move ----------------
    dep = find(docs, "Deployment")
    spec = dep["spec"]["template"]["spec"]
    check("still pinned to prusik",
          spec["nodeSelector"] == {"kubernetes.io/hostname": "prusik"}, str(spec["nodeSelector"]))
    container = next(c for c in spec["containers"] if c["name"] == "agentgateway")
    check("SESSION_KEY still comes from the same required secretKeyRef (key unchanged)",
          [e for e in container["env"] if e.get("name") == "SESSION_KEY"][0]["valueFrom"]["secretKeyRef"]
          == {"name": "mcp-gateway-session-key", "key": "SESSION_KEY"})
    check("readiness/startup probes are unchanged (/healthz/ready on 15021)",
          container["readinessProbe"]["httpGet"] == {"path": "/healthz/ready", "port": 15021}
          and container["startupProbe"]["httpGet"] == {"path": "/healthz/ready", "port": 15021})
    es, gen = find(docs, "ExternalSecret"), find(docs, "Password")
    check("session key lifecycle unchanged (CreatedOnce + immutable + owner)",
          es["spec"]["refreshPolicy"] == "CreatedOnce" and es["spec"]["target"]["immutable"] is True)
    check("session key generator unchanged (32 hex chars -> 64 hex -> AES-256-GCM)",
          gen["spec"]["length"] == 32 and gen["spec"]["encoding"] == "hex"
          and gen["spec"]["secretKeys"] == ["SESSION_KEY"])
    svc = find(docs, "Service")
    check("service stays ClusterIP with 3000/15020/15021",
          svc["spec"]["type"] == "ClusterIP"
          and {p["port"] for p in svc["spec"]["ports"]} == {3000, 15020, 15021})
    # Serving path: HTTPRoute via Envoy Gateway (external). The Ingress is retained as
    # the rollback path (docs/network/ingress.md, ingress-migration Phase 6).
    hr = find(docs, "HTTPRoute")
    check("HTTPRoute serving path: parentRef external/envoy-gateway, sectionName https-grigri-cloud",
          hr is not None
          and hr["spec"]["parentRefs"][0]["name"] == "external"
          and hr["spec"]["parentRefs"][0].get("namespace") == "envoy-gateway"
          and hr["spec"]["parentRefs"][0].get("sectionName") == "https-grigri-cloud",
          str(hr["spec"].get("parentRefs")) if hr else "no HTTPRoute")
    check("HTTPRoute serving path: hostname mcp.grigri.cloud, backend mcp-gateway-standalone:3000",
          hr is not None
          and hr["spec"]["hostnames"] == ["mcp.grigri.cloud"]
          and hr["spec"]["rules"][0]["backendRefs"][0]["name"] == "mcp-gateway-standalone"
          and hr["spec"]["rules"][0]["backendRefs"][0]["port"] == 3000,
          str(hr["spec"]) if hr else "no HTTPRoute")
    ing = find(docs, "Ingress")
    check("ingress retained as rollback path: nginx-external + TLS for mcp.grigri.cloud -> the gateway service",
          ing["spec"]["ingressClassName"] == "nginx-external"
          and ing["spec"]["tls"][0]["secretName"] == "mcp-gateway-tls-certificate"
          and ing["spec"]["rules"][0]["http"]["paths"][0]["backend"]["service"]["name"]
          == "mcp-gateway-standalone")
    o2c = find(docs, "KanidmOAuth2Client")["spec"]
    check("OAuth2 client stays public with the exact single redirect (not widened)",
          o2c["public"] is True and o2c["strictRedirectUrl"] is True
          and o2c["redirectUrl"] == ["http://127.0.0.1:15000/callback"])
    check("no PKCE relaxation: allowInsecureClientDisablePkce is absent",
          "allowInsecureClientDisablePkce" not in o2c)
    smap = {e["group"]: e["scopes"] for e in o2c["scopeMap"]}
    check("service-only group gains both route scopes and keeps the old ones",
          smap.get("mcp-gateway-agents", [])[:4] == ["openid", "email", "profile", "mcp_firecrawl"]
          and {"mcp_grafana", "mcp_context7"} <= set(smap.get("mcp-gateway-agents", [])),
          str(smap.get("mcp-gateway-agents")))
    check("human scopes come from per-route groups plus grafana-users",
          set(smap) == {"mcp-gateway-agents", "mcp-context7-users",
                        "mcp-firecrawl-users", "grafana-users"}
          and smap["mcp-context7-users"] == ["mcp_context7"]
          and smap["mcp-firecrawl-users"] == ["mcp_firecrawl"]
          and smap["grafana-users"] == ["mcp_grafana"], str(smap))
    groups = [d for d in docs if d.get("kind") == "KanidmGroup"]
    workload_grp = next((g for g in groups if g["metadata"]["name"] == "mcp-gateway-agents"), None)
    check("workload group members are service accounts only (grafana-mcp, no new SA, no person)",
          workload_grp is not None
          and workload_grp["spec"]["members"] == ["grafana-mcp"],
          str(workload_grp["spec"]["members"] if workload_grp else None))
    for g in groups:
        if g["metadata"]["name"] in ("mcp-context7-users", "mcp-firecrawl-users"):
            check(f"group {g['metadata']['name']} has no members field (person membership is manual)",
                  "members" not in g.get("spec", {}),
                  str(list(g.get("spec", {}).keys())))

    print()
    print(f"=== {sum(RESULTS)} passed, {RESULTS.count(False)} failed ===")
    return 1 if RESULTS.count(False) else 0


if __name__ == "__main__":
    sys.exit(main())
