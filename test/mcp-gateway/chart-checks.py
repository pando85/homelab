#!/usr/bin/env python3
"""Static assertions on the rendered MCP gateway charts (no cluster, no binary).

Each check is an invariant a live change could break silently, asserted on
`helm template` output rather than on the source YAML:

  * both planes render fail-closed (no routes) and stay that way until a backend is
    approved;
  * the session key is a generated Secret consumed through SESSION_KEY, never a
    committed `session.key`;
  * both planes deny the same request headers - the mirrored charts must not drift,
    because the plane that loses an entry becomes the hole;
  * `credential` really controls what reaches the backend: `none` strips the caller's
    bearer and renders no forwarding policy, `accessToken`/`idToken` emit
    `backendAuth.passthrough`;
  * opening a route and widening egress is one edit: the NetworkPolicy takes its
    backend rule from the same values entry and gets none while the route is off;
  * Kanidm: the audience is the OAuth2 CLIENT NAME, `spec.members` is omitted while
    nobody is declared (it is an AUTHORITATIVE field - templates/kanidm-group.yaml).

    python3 test/mcp-gateway/chart-checks.py
"""
import base64
import json
import os
import re
import subprocess
import sys

import yaml

CHARTS = ("mcp-gateway", "mcp-gateway-admin")

results = []


def check(label, ok, detail=""):
    print(f"{'PASS' if ok else 'FAIL'}  {label}" + (f"\n      {detail}" if detail else ""))
    results.append(bool(ok))


def helm(chart, sets=()):
    cmd = ["helm", "template", chart, f"apps/{chart}", "--namespace", chart]
    for kv in sets:
        cmd += ["--set", kv]
    out = subprocess.run(cmd, capture_output=True, text=True, timeout=300)
    if out.returncode != 0:
        raise RuntimeError(f"helm template {chart} {list(sets)}: {out.stderr[-900:]}")
    return [d for d in yaml.safe_load_all(out.stdout) if d]


def find(docs, kind):
    return next((d for d in docs if d.get("kind") == kind), None)


def config_text(docs):
    cm = find(docs, "ConfigMap")
    return cm["data"]["config.yaml"] if cm else ""


def removed_headers(cfg_text, route):
    """The `remove:` entries of one route's backend target."""
    start = cfg_text.index(f"  - name: {route}\n")
    block = cfg_text[start:]
    nxt = re.search(r"\n  - name: ", block)
    if nxt:
        block = block[:nxt.start()]
    out, inside = [], False
    for line in block.splitlines():
        s = line.strip()
        if s == "remove:":
            inside = True
            continue
        if inside:
            if s.startswith("- "):
                out.append(s[2:].strip())
            else:
                inside = False
    return out


def main():
    for chart in CHARTS:
        docs = helm(chart)
        text = config_text(docs)
        cfg = yaml.safe_load(text)
        vals = open(f"apps/{chart}/values.yaml").read()

        check(f"{chart}: renders", bool(docs))
        check(f"{chart}: renders fail-closed (routes: [])", cfg.get("routes") == [],
              f"routes={cfg.get('routes')!r}")
        check(f"{chart}: admin listener off", cfg["config"].get("adminAddr") == "off")
        check(f"{chart}: no committed session key in the rendered config",
              "session" not in cfg["config"],
              "the key must come from the SESSION_KEY env var")
        check(f"{chart}: image pinned to v1.7.0 with a renovate hint",
              "tag: v1.7.0" in vals and
              "renovate: datasource=docker depName=cr.agentgateway.dev/agentgateway" in vals)
        check(f"{chart}: no oauth2-proxy",
              subprocess.run(f"grep -rEl 'oauth2-?proxy' apps/{chart} || true",
                             shell=True, capture_output=True, text=True).stdout.strip() == "")

        # B1: the chart must render NO key material. The key is produced by the ESO
        # Password generator and pulled in exactly once by an ExternalSecret.
        check(f"{chart}: renders no Secret at all (no Helm-side key material)",
              find(docs, "Secret") is None)
        tmpl_src = "".join(open(f"apps/{chart}/templates/{f}").read()
                           for f in os.listdir(f"apps/{chart}/templates"))
        # strip {{- /* ... */ -}} comment blocks: prose about the rejected approach must
        # not fail this, while any surviving call in real template logic must.
        code = re.sub(r"\{\{-?\s*/\*.*?\*/\s*-?\}\}", "", tmpl_src, flags=re.S)
        # also drop plain YAML comment lines: the rationale for B1 quotes the two
        # functions by name, and prose must not satisfy or defeat the check.
        code = "\n".join(ln for ln in code.splitlines() if not ln.lstrip().startswith("#"))
        check(f"{chart}: no randAlphaNum/lookup in template LOGIC (B1)",
              re.search(r"randAlphaNum|\(lookup ", code) is None)
        es = find(docs, "ExternalSecret")
        gen = find(docs, "Password")
        check(f"{chart}: session key comes from an ExternalSecret", es is not None)
        check(f"{chart}: session key comes from a Password generator", gen is not None)
        if es:
            check(f"{chart}: ExternalSecret targets the SESSION_KEY secret name",
                  es["spec"]["target"]["name"] == f"{chart}-session-key")
            check(f"{chart}: refreshPolicy CreatedOnce (create once, never re-sync)",
                  es["spec"].get("refreshPolicy") == "CreatedOnce",
                  f"got {es['spec'].get('refreshPolicy')!r}")
            check(f"{chart}: target is immutable so no later write rewrites the key",
                  es["spec"]["target"].get("immutable") is True)
            check(f"{chart}: target creationPolicy Owner (deleting the Secret is the "
                  "only legitimate way to get a new key)",
                  es["spec"]["target"].get("creationPolicy", "Owner") == "Owner")
            refs = [d.get("sourceRef", {}).get("generatorRef") for d in es["spec"]["dataFrom"]]
            check(f"{chart}: ExternalSecret reads dataFrom.sourceRef.generatorRef "
                  "(the installed field for this shape)",
                  all(r and r.get("kind") == "Password" for r in refs), str(refs)[:120])
        if gen:
            g = gen["spec"]
            check(f"{chart}: generator emits exactly 64 hex chars (length 32 + encoding hex)",
                  g.get("length") == 32 and g.get("encoding") == "hex", str(g)[:140])
            check(f"{chart}: generator output key is SESSION_KEY (secretKeys)",
                  g.get("secretKeys") == ["SESSION_KEY"], str(g.get("secretKeys")))
            # 32 ASCII chars hex-encoded = 64 chars = 32 bytes for AES-256-GCM
            check(f"{chart}: the generated shape is what agentgateway hex::decode needs",
                  len("".join(f"{c:02x}" for c in range(32))) == 64)
        check(f"{chart}: container gets the key through SESSION_KEY",
              re.search(r"SESSION_KEY:\s*\n\s*valueFrom:\s*\n\s*secretKeyRef:", vals) is not None)
        dep = find(docs, "Deployment")
        env = [e for c in (dep or {}).get("spec", {}).get("template", {}).get(
            "spec", {}).get("containers", []) for e in c.get("env", [])
            if e.get("name") == "SESSION_KEY"]
        ref = (env[0].get("valueFrom", {}).get("secretKeyRef") or {}) if env else {}
        check(f"{chart}: SESSION_KEY secretKeyRef is required (fail closed, no base64 "
              "fallback and no plaintext default)",
              bool(ref) and ref.get("optional") is not True and "name" in ref, str(ref)[:120])
        pod = find(docs, "Pod") or None

    # ---- B1 determinism: the same chart rendered twice must be byte-identical,
    # because the previous randAlphaNum+lookup template produced a different
    # SESSION_KEY on every render under ArgoCD (`lookup` sees an empty cluster).
    for chart in CHARTS:
        a = yaml.dump_all(helm(chart), sort_keys=True)
        b = yaml.dump_all(helm(chart), sort_keys=True)
        check(f"{chart}: repeated renders are byte-identical (no randomness/lookup)", a == b)

    # ---- the two planes must deny the same headers
    pub_on = helm("mcp-gateway", ["backends.firecrawl.enabled=true",
                                  "backends.grafana.enabled=true",
                                  "networkIsolation=true"])
    adm_on = helm("mcp-gateway-admin", ["backends.vault.enabled=true",
                                        "networkIsolation=true"])
    pub_fc = config_text(helm("mcp-gateway", ["backends.firecrawl.enabled=true"]))
    pub_text, adm_text = config_text(pub_on), config_text(adm_on)
    graf_text = config_text(helm("mcp-gateway", ["backends.grafana.enabled=true"]))
    pub_rm, adm_rm = removed_headers(pub_text, "firecrawl"), removed_headers(adm_text, "vault")
    check("both planes deny the same request headers",
          set(pub_rm) == set(adm_rm) and len(pub_rm) >= 15,
          f"public {len(pub_rm)} entries, admin {len(adm_rm)}")
    for must in ("x-firecrawl-api-key", "x-api-key", "x-grafana-service-account-token",
                 "x-grafana-api-key", "x-grafana-org-id", "x-grafana-url", "x-grafana-id",
                 "x-vault-token", "vault_addr", "vault-addr", "vault_token",
                 "vault_skip_verify", "vault-skip-verify", "x-vault-namespace",
                 "vault_namespace", "vault-namespace", "cookie"):
        check(f"deny list stops {must}", must in pub_rm and must in adm_rm)
    check("deny list does not accidentally remove the MCP protocol headers",
          not {"mcp-session-id", "mcp-protocol-version", "content-type"} & set(pub_rm))

    # ---- credential model
    check("credential: none strips the caller bearer (public firecrawl)",
          "- authorization" in pub_fc)
    check("credential: none renders no backendAuth (public firecrawl)",
          "backendAuth" not in pub_fc)
    check("delegating route emits backendAuth.passthrough (public grafana)",
          re.search(r"backendAuth:\s*\n\s*passthrough: \{\}", graf_text) is not None)
    check("delegating route must NOT also strip authorization",
          "- authorization" not in graf_text)
    check("shipped grafana route authorizes on the ACCESS token's scope string",
          "jwt.scope.split(' ').exists(s, s == 'mcp_grafana')" in graf_text and
          "jwt.scopes.exists" not in graf_text)
    # B3: the approved architecture puts Grafana MCP on the PUBLIC plane; only Vault is
    # admin. An earlier draft shipped the candidate on the admin chart - that was an
    # unapproved deviation and must not come back.
    adm_vals = open("apps/mcp-gateway-admin/values.yaml").read()
    adm_default = config_text(helm("mcp-gateway-admin"))
    check("admin values define no grafana backend",
          re.search(r"^\s{2}grafana:\s*$", adm_vals, re.M) is None)
    check("admin chart cannot serve /grafana/mcp", "/grafana/mcp" not in adm_default)
    check("grafana backend lives in its own namespace, not mcp-gateway-admin",
          "namespace: grafana-mcp" in open("apps/mcp-gateway/values.yaml").read()
          and "host: grafana-mcp.grafana-mcp.svc.cluster.local" in graf_text)
    check("grafana resource URL is the public host",
          "resource: https://mcp.grigri.cloud/grafana/mcp"
          in config_text(helm("mcp-gateway", ["backends.grafana.enabled=true",
                                             "backends.grafana.resource="
                                             "https://mcp.grigri.cloud/grafana/mcp"])))
    # B2: no human scope grant ships on either plane.
    for chart in CHARTS:
        vals = open(f"apps/{chart}/values.yaml").read()
        m = re.search(r"humanScopeMap:\s*\n?((?:\s*- .+\n?)+|\[\])", vals)
        check(f"{chart}: humanScopeMap ships empty (no human grant wired)",
              m is not None and (m.group(1).strip() == "[]" or not m.group(1).strip()),
              (m.group(0)[:60] if m else "not found"))
        # comment-aware: prose explaining WHY the group is withheld must not fail this
        grant_lines = [ln for ln in vals.splitlines()
                       if not ln.lstrip().startswith("#") and "searxng-users" in ln]
        check(f"{chart}: no searxng-users grant line in values", not grant_lines,
              "; ".join(x.strip() for x in grant_lines) or None)
    # The owner constraint: the edge bearer is always the Kanidm access token. No
    # shipped backend may *set* the idToken shape; values keeps it only in prose as the
    # measured record of the rejected option, so match the setting form and ignore
    # comments (a comment mentioning the rejected value must not fail the suite, and a
    # real setting must not pass by hiding behind one).
    for chart in CHARTS:
        raw = open(f"apps/{chart}/values.yaml").read().splitlines()
        settings = [ln for ln in raw
                    if not ln.lstrip().startswith("#")
                    and re.match(r"^\s*credential:\s*idToken\s*$", ln)]
        check(f"{chart}: no shipped backend uses credential: idToken",
              not settings, "; ".join(s.strip() for s in settings) or None)
    adm_id = config_text(helm("mcp-gateway", ["backends.grafana.enabled=true",
                                              "backends.grafana.credential=idToken"]))
    check("template still renders the measured idToken variant on request",
          "jwt.scopes.exists(s, s == 'mcp_grafana')" in adm_id)
    check("accessToken-shaped route authorizes on the scope string",
          "jwt.scope.split(' ').exists(s, s == 'mcp_firecrawl')" in pub_text)
    check("whole-value scope equality only (no substring match)",
          "contains(jwt.scope" not in pub_text and "startsWith" not in pub_text)

    # ---- fixed backend address, pinned Host, per-plane Kanidm client
    check("route backend is the fixed service URL from values",
          "host: http://firecrawl-mcp.firecrawl-mcp.svc.cluster.local:3000/mcp" in pub_text)
    check("route pins the Host header the backend accepts",
          "host: firecrawl-mcp.firecrawl-mcp.svc.cluster.local" in pub_text and
          "host: grafana-mcp.grafana-mcp.svc.cluster.local" in graf_text)
    check("public plane validates the public Kanidm client",
          "\n          - mcp-gateway\n" in pub_text and
          "openid/mcp-gateway/public_key.jwk" in pub_text and
          "mcp-gateway-admin" not in pub_text)
    # Issuer must be Kanidm's per-client OIDC path, never the bare host: the bare
    # root has no issuer document and tokens carry `iss` of the full path.
    for chart, client in (("mcp-gateway", "mcp-gateway"),
                          ("mcp-gateway-admin", "mcp-gateway-admin")):
        vals = open(f"apps/{chart}/values.yaml").read()
        check(f"{chart}: issuer is the full per-client path",
              f"issuer: https://idm.grigri.cloud/oauth2/openid/{client}" in vals and
              not re.search(r"issuer: https://idm\.grigri\.cloud/?\s*$", vals, re.M))
        check(f"{chart}: jwksUrl is Kanidm's public_key.jwk for that client",
              f"jwksUrl: https://idm.grigri.cloud/oauth2/openid/{client}/public_key.jwk" in vals)
    check("admin plane validates its own Kanidm client",
          "\n          - mcp-gateway-admin\n" in adm_text and
          "openid/mcp-gateway-admin/public_key.jwk" in adm_text and
          "- mcp-gateway\n" not in adm_text)

    # ---- egress follows the same values entry as the route
    def egress_json(docs):
        np = [d for d in docs if d.get("kind") == "NetworkPolicy"
              and d["metadata"]["name"].endswith("-egress")]
        return json.dumps(np[0]["spec"]["egress"]) if np else ""

    pub_eg_off, adm_eg_off = egress_json(helm("mcp-gateway")), egress_json(helm("mcp-gateway-admin"))
    check("no backend egress path while every route is disabled",
          "firecrawl-mcp" not in pub_eg_off and "grafana-mcp" not in adm_eg_off and
          "vault-mcp" not in adm_eg_off)
    pub_eg_on = egress_json(pub_on)
    check("enabling a route adds exactly that backend's egress rule",
          pub_eg_on.count('"port": 3000') == 1 and "firecrawl-mcp" in pub_eg_on,
          f"{pub_eg_on.count('kubernetes.io/metadata.name')} namespace selectors")
    adm_eg_on = egress_json(adm_on)
    # after B3, Grafana is a public-plane backend: enabling Vault must not create any
    # admin egress path to Grafana, and the public chart must be the one that can.
    check("admin egress adds Vault only when Vault is enabled, and never Grafana",
          "vault-mcp" in adm_eg_on and "grafana-mcp" not in adm_eg_on,
          f"admin egress {adm_eg_on.count('kubernetes.io/metadata.name')} selectors")
    pub_eg_graf = egress_json(helm("mcp-gateway", ["backends.grafana.enabled=true",
                                                    "networkIsolation=true"]))
    check("public egress gains the grafana-mcp path only via the public chart",
          "grafana-mcp" in pub_eg_graf and "vault-mcp" not in pub_eg_graf)
    pub_eg_default = egress_json(helm("mcp-gateway"))
    check("public plane has no Vault path at all (admin-only backend)",
          "vault-mcp" not in pub_eg_default and
          "mcp_vault" not in config_text(helm("mcp-gateway",
                                              ["backends.grafana.enabled=true"])))

    # ---- networkIsolation gating: baseline must be policy-free
    # Default render (networkIsolation not set, defaults to false): no NetworkPolicy/CNP
    adm_default_docs = helm("mcp-gateway-admin")
    adm_np_default = [d for d in adm_default_docs if d.get("kind") == "NetworkPolicy"]
    adm_cnp_default = [d for d in adm_default_docs if d.get("kind") == "CiliumNetworkPolicy"]
    check("mcp-gateway-admin: default render has no NetworkPolicy (networkIsolation defaults false)",
          len(adm_np_default) == 0,
          f"found {len(adm_np_default)} NetworkPolicy resources")
    check("mcp-gateway-admin: default render has no CiliumNetworkPolicy",
          len(adm_cnp_default) == 0,
          f"found {len(adm_cnp_default)} CiliumNetworkPolicy resources")

    # Explicit false: no NetworkPolicy/CNP
    adm_false_docs = helm("mcp-gateway-admin", ["networkIsolation=false"])
    adm_np_false = [d for d in adm_false_docs if d.get("kind") == "NetworkPolicy"]
    adm_cnp_false = [d for d in adm_false_docs if d.get("kind") == "CiliumNetworkPolicy"]
    check("mcp-gateway-admin: networkIsolation=false renders no NetworkPolicy",
          len(adm_np_false) == 0,
          f"found {len(adm_np_false)} NetworkPolicy resources")
    check("mcp-gateway-admin: networkIsolation=false renders no CiliumNetworkPolicy",
          len(adm_cnp_false) == 0,
          f"found {len(adm_cnp_false)} CiliumNetworkPolicy resources")

    # Explicit true: existing native NetworkPolicies rendered unchanged
    adm_true_docs = helm("mcp-gateway-admin", ["networkIsolation=true"])
    adm_np_true = [d for d in adm_true_docs if d.get("kind") == "NetworkPolicy"]
    check("mcp-gateway-admin: networkIsolation=true renders both legacy NetworkPolicies",
          len(adm_np_true) == 2,
          f"expected 2, found {len(adm_np_true)}")
    adm_np_names = {d["metadata"]["name"] for d in adm_np_true}
    check("mcp-gateway-admin: networkIsolation=true renders ingress and egress policies",
          adm_np_names == {"mcp-gateway-admin-ingress", "mcp-gateway-admin-egress"},
          f"names: {adm_np_names}")
    # Verify the ingress policy still allows only 3000 and 15020 (not 15021)
    adm_np_ingress = next((d for d in adm_np_true if d["metadata"]["name"] == "mcp-gateway-admin-ingress"), None)
    if adm_np_ingress:
      ingress_ports = set()
      for rule in adm_np_ingress.get("spec", {}).get("ingress", []):
          for port in rule.get("ports", []):
              ingress_ports.add(port.get("port"))
      check("mcp-gateway-admin: legacy ingress policy allows only 3000 and 15020 (not 15021)",
            ingress_ports == {3000, 15020},
            f"ports: {ingress_ports}")
    # Verify values.yaml documents the gating and the legacy status
    adm_vals_text = open("apps/mcp-gateway-admin/values.yaml").read()
    check("mcp-gateway-admin: values.yaml documents networkIsolation gating",
          "networkIsolation: false" in adm_vals_text and
          "policy-free" in adm_vals_text,
          "missing documentation")
    check("mcp-gateway-admin: values.yaml warns that true is NOT production-ready",
          "NOT production-ready" in adm_vals_text or
          "not production-ready" in adm_vals_text,
          "missing warning")

    # ---- services: ClusterIP only, ops port separate from the MCP port
    svc = find(pub_on, "Service")
    check("service is ClusterIP with no NodePort/hostPort",
          svc is not None and svc["spec"]["type"] == "ClusterIP" and
          "nodePort" not in json.dumps(svc))
    ports = {p["port"] for p in svc["spec"]["ports"]} if svc else set()
    check("metrics/healthz are separate ports, not paths on the MCP port",
          {3000, 15020, 15021} <= ports, str(sorted(ports)))

    # ---- Grafana and Vault remain owner decisions
    for chart, names in (("mcp-gateway", ("firecrawl", "grafana")),
                         ("mcp-gateway-admin", ("vault",))):
        vals_txt = open(f"apps/{chart}/values.yaml").read()
        for name in names:
            check(f"{chart}: {name} route ships disabled",
                  re.search(rf"\n  {name}:\n(?:.*\n)*?\s{{4}}enabled: false",
                            vals_txt) is not None)
    check("admin configmap documents the Vault session-identity blocker",
          "Vault MCP BACKEND BLOCKER" in adm_text)
    check("Vault scopes exist only on the admin plane",
          "mcp_vault" in adm_text and "mcp_vault" not in pub_fc)

    # ---- Kanidm resources
    pub_docs = helm("mcp-gateway")
    grp = find(pub_docs, "KanidmGroup")
    check("workload group exists and asserts no members by default",
          grp is not None and grp["metadata"]["name"] == "mcp-gateway-agents" and
          "members" not in grp.get("spec", {}), json.dumps((grp or {}).get("spec", {}))[:160])
    grp_on = find(helm("mcp-gateway", ["kanidm.workloadGroup.serviceMembers[0]=svc@realm"]),
                  "KanidmGroup")
    check("declared service accounts render as the authoritative member list",
          grp_on["spec"].get("members") == ["svc@realm"], json.dumps(grp_on["spec"])[:160])
    o2c = find(pub_docs, "KanidmOAuth2Client")
    check("public OAuth2 client is public with a loopback redirect and strict urls",
          o2c is not None and o2c["spec"]["public"] is True and
          "127.0.0.1" in json.dumps(o2c["spec"]) and
          o2c["spec"].get("strictRedirectUrl") in (True, None))
    check("scopeMap is rendered (human + workload maps concatenated)",
          "scopeMap" in (o2c or {}).get("spec", {}))
    check("values warn that the workload group is service-accounts only",
          "never point it at an existing human/mixed group" in
          open("apps/mcp-gateway/values.yaml").read())
    adm_o2c = find(helm("mcp-gateway-admin"), "KanidmOAuth2Client")
    check("admin client exists as a separate Kanidm app",
          adm_o2c is not None and adm_o2c["metadata"]["name"] == "mcp-gateway-admin")
    check("admin workload group is not created while no admin scope is granted",
          find(helm("mcp-gateway-admin"), "KanidmGroup") is None)

    print()
    print(f"=== {sum(results)} passed, {results.count(False)} failed ===")
    return 1 if results.count(False) else 0


if __name__ == "__main__":
    sys.exit(main())
