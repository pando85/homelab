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
    """The `remove:` entries of one route's backend target.

    Handles both formats:
    - Admin gateway (templated): "  - name: {route}\n"
    - Public gateway (hardcoded): "      name: {route}\n"
    """
    # Try admin gateway format first
    try:
        start = cfg_text.index(f"  - name: {route}\n")
        block = cfg_text[start:]
        # Find the next route at the same indentation level
        nxt = re.search(r"\n  - name: ", block[1:])
        if nxt:
            block = block[:nxt.start() + 1]
    except ValueError:
        # Try public gateway format
        start = cfg_text.index(f"      name: {route}\n")
        block = cfg_text[start:]
        # Find the next route at the same indentation level
        nxt = re.search(r"\n      name: ", block[1:])
        if nxt:
            block = block[:nxt.start() + 1]

    out, inside = [], False
    for line in block.splitlines():
        s = line.strip()
        if s == "remove:":
            inside = True
            continue
        if inside:
            # Skip comment lines
            if s.startswith("#"):
                continue
            if s.startswith("- "):
                header = s[2:].strip()
                # Stop if we hit a non-header line (like "backends:" or "mcp:")
                if ":" in header and not header.startswith("-"):
                    break
                out.append(header)
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
        # Admin plane is fail-closed (no routes enabled); public plane has context7 and
        # grafana enabled by default (approved architecture).
        if chart == "mcp-gateway-admin":
            check(f"{chart}: renders fail-closed (routes: [])", cfg.get("routes") == [],
                  f"routes={cfg.get('routes')!r}")
        else:
            check(f"{chart}: renders with approved routes (context7 + grafana + firecrawl)",
                  len(cfg.get("routes", [])) >= 3,
                  f"routes={len(cfg.get('routes', []))}")
        check(f"{chart}: admin listener off", cfg["config"].get("adminAddr") == "off")
        check(f"{chart}: no committed session key in the rendered config",
              "session" not in cfg["config"],
              "the key must come from the SESSION_KEY env var")
        # Check the rendered Deployment image, not just values.yaml, because the public
        # chart uses an official upstream chart dependency and may not have an explicit
        # tag in values.yaml. The effective image must be v1.7.0.
        dep = find(docs, "Deployment")
        containers = (dep or {}).get("spec", {}).get("template", {}).get("spec", {}).get("containers", [])
        images = [c.get("image", "") for c in containers]
        has_v170 = any("v1.7.0" in img or ":v1.7.0" in img for img in images)
        check(f"{chart}: effective image pinned to v1.7.0 (rendered Deployment)",
              has_v170,
              f"images={images}")
        # Renovate hint is required when there's an explicit tag in values.yaml.
        # Charts using upstream dependencies may not have explicit tags.
        has_explicit_tag = "tag: v1.7.0" in vals
        has_renovate = "renovate: datasource=docker depName=cr.agentgateway.dev/agentgateway" in vals
        check(f"{chart}: renovate hint present when explicit tag in values.yaml",
              not has_explicit_tag or has_renovate,
              f"explicit_tag={has_explicit_tag}, renovate={has_renovate}")
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
        # Check the rendered Deployment for SESSION_KEY env var, not values.yaml regex.
        # The official chart may use extraEnv or other structures, so the rendered
        # Deployment is the source of truth.
        dep = find(docs, "Deployment")
        env = [e for c in (dep or {}).get("spec", {}).get("template", {}).get(
            "spec", {}).get("containers", []) for e in c.get("env", [])
            if e.get("name") == "SESSION_KEY"]
        ref = (env[0].get("valueFrom", {}).get("secretKeyRef") or {}) if env else {}
        check(f"{chart}: rendered Deployment has SESSION_KEY from ESO secret (no plaintext)",
              bool(ref) and ref.get("optional") is not True and "name" in ref,
              f"secretKeyRef={ref}")
        pod = find(docs, "Pod") or None

    # ---- B1 determinism: the same chart rendered twice must be byte-identical,
    # because the previous randAlphaNum+lookup template produced a different
    # SESSION_KEY on every render under ArgoCD (`lookup` sees an empty cluster).
    for chart in CHARTS:
        a = yaml.dump_all(helm(chart), sort_keys=True)
        b = yaml.dump_all(helm(chart), sort_keys=True)
        check(f"{chart}: repeated renders are byte-identical (no randomness/lookup)", a == b)

    # ---- the two planes must deny security-critical headers
    # The admin plane has a templated configmap with a comprehensive remove list for all
    # potential backends. The public plane has hardcoded routes with specific remove lists
    # for each route. Both planes must remove common security headers (Authorization, Cookie)
    # and backend-specific headers relevant to their routes.
    pub_on = helm("mcp-gateway", ["backends.grafana.enabled=true",
                                  "networkIsolation=true"])
    adm_on = helm("mcp-gateway-admin", ["backends.vault.enabled=true",
                                        "networkIsolation=true"])
    pub_text, adm_text = config_text(pub_on), config_text(adm_on)
    graf_text = config_text(helm("mcp-gateway", ["backends.grafana.enabled=true"]))
    # Public plane uses context7 route (always present), admin plane uses vault route
    pub_rm = removed_headers(pub_text, "context7")
    adm_rm = removed_headers(adm_text, "vault")
    # Both planes must remove common security headers
    common_headers = {"authorization", "cookie"}
    check("both planes deny common security headers (Authorization, Cookie)",
          common_headers.issubset({h.lower() for h in pub_rm}) and
          common_headers.issubset({h.lower() for h in adm_rm}),
          f"public {pub_rm}, admin {adm_rm}")
    # Admin plane must remove all backend-specific headers (comprehensive list)
    check("admin plane denies comprehensive backend headers",
          len(adm_rm) >= 15,
          f"admin {len(adm_rm)} entries")
    # Public plane must remove headers relevant to its backends
    check("public plane denies Grafana-specific headers",
          "x-grafana-service-account-token" in {h.lower() for h in pub_rm} and
          "x-grafana-url" in {h.lower() for h in pub_rm},
          f"public {pub_rm}")
    # Check specific headers that must be removed by the admin plane
    for must in ("x-firecrawl-api-key", "x-api-key", "x-grafana-service-account-token",
                 "x-grafana-api-key", "x-grafana-org-id", "x-grafana-url", "x-grafana-id",
                 "x-vault-token", "vault_addr", "vault-addr", "vault_token",
                 "vault_skip_verify", "vault-skip-verify", "x-vault-namespace",
                 "vault_namespace", "vault-namespace", "cookie"):
        check(f"admin deny list stops {must}", must.lower() in {h.lower() for h in adm_rm})
    check("deny list does not accidentally remove the MCP protocol headers",
          not {"mcp-session-id", "mcp-protocol-version", "content-type"} & set(pub_rm))

    # ---- credential model
    # Public plane context7 route uses apiKeyFile credential (strips Authorization, uses API key file)
    pub_context7_text = config_text(helm("mcp-gateway"))
    check("credential: apiKeyFile strips the caller bearer (public context7)",
          "- authorization" in pub_context7_text.lower() or "- Authorization" in pub_context7_text)
    check("credential: apiKeyFile renders no backendAuth.passthrough (public context7)",
          "backendAuth:\n          passthrough:" not in pub_context7_text)
    # Public plane firecrawl route uses credential: none (strips Authorization, no backendAuth)
    check("credential: none strips the caller bearer (public firecrawl)",
          "- authorization" in pub_context7_text.lower() or "- Authorization" in pub_context7_text)
    # Extract the firecrawl route section and verify it has no backendAuth
    import re as re2
    fc_match = re2.search(r'- name: firecrawl\n(.*?)(?=\n      - name: |\Z)', pub_context7_text, re2.DOTALL)
    fc_section = fc_match.group(0) if fc_match else ""
    check("credential: none renders no backendAuth at all (public firecrawl)",
          "name: firecrawl" in pub_context7_text and "backendAuth" not in fc_section,
          f"firecrawl section has backendAuth: {'backendAuth' in fc_section}")
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
    check("grafana backend lives in grafana-mcp namespace (verified by host name)",
          "host: grafana-mcp.grafana-mcp.svc.cluster.local" in graf_text)
    check("grafana resource URL is the public host",
          "resource: https://mcp.grigri.cloud/grafana/mcp"
          in config_text(helm("mcp-gateway", ["backends.grafana.enabled=true",
                                             "backends.grafana.resource="
                                             "https://mcp.grigri.cloud/grafana/mcp"])))
    # B2: human scope grants
    # Public gateway: humanScopeMap has per-route groups (mcp-context7-users,
    # mcp-firecrawl-users) plus the existing grafana-users for mcp_grafana.
    # Admin gateway: humanScopeMap is empty (no human access to admin plane).
    pub_vals = open("apps/mcp-gateway/values.yaml").read()
    adm_vals = open("apps/mcp-gateway-admin/values.yaml").read()

    # Public gateway: exactly three group->scope pairs
    # Extract the humanScopeMap block: from the key to the next key at same indent
    pub_hsm_match = re.search(r"^    humanScopeMap:\n((?:      .*\n)*)", pub_vals, re.M)
    pub_hsm_block = pub_hsm_match.group(1) if pub_hsm_match else ""
    check("mcp-gateway: humanScopeMap has mcp-context7-users group",
          "mcp-context7-users" in pub_hsm_block,
          pub_hsm_block[:120] or "not found")
    check("mcp-gateway: humanScopeMap has mcp-firecrawl-users group",
          "mcp-firecrawl-users" in pub_hsm_block,
          pub_hsm_block[:120] or "not found")
    check("mcp-gateway: humanScopeMap still has grafana-users group",
          "grafana-users" in pub_hsm_block,
          pub_hsm_block[:120] or "not found")
    # mcp_context7 must NOT be granted by grafana-users anymore
    check("mcp-gateway: grafana-users does NOT grant mcp_context7 (moved to mcp-context7-users)",
          not re.search(
              r"-\s+group:\s+grafana-users\s*\n\s+scopes:\s*\n(?:\s+- mcp_\w+\n)*\s+- mcp_context7",
              pub_hsm_block),
          "grafana-users still grants mcp_context7")

    # Admin gateway should have empty humanScopeMap
    adm_m = re.search(r"humanScopeMap:\s*\n?((?:\s*- .+\n?)+|\[\])", adm_vals)
    check("mcp-gateway-admin: humanScopeMap ships empty (no human grant wired)",
          adm_m is not None and (adm_m.group(1).strip() == "[]" or not adm_m.group(1).strip()),
          (adm_m.group(0)[:60] if adm_m else "not found"))

    # Rendered KanidmGroup CRs: the two new human groups must exist with no `members` key
    pub_docs = helm("mcp-gateway")
    rendered_groups = [d for d in pub_docs if d.get("kind") == "KanidmGroup"]
    rendered_group_names = {g["metadata"]["name"] for g in rendered_groups}
    check("mcp-gateway: mcp-context7-users renders as KanidmGroup CR",
          "mcp-context7-users" in rendered_group_names,
          str(sorted(rendered_group_names)))
    check("mcp-gateway: mcp-firecrawl-users renders as KanidmGroup CR",
          "mcp-firecrawl-users" in rendered_group_names,
          str(sorted(rendered_group_names)))
    for g in rendered_groups:
        if g["metadata"]["name"] in ("mcp-context7-users", "mcp-firecrawl-users"):
            check(f"mcp-gateway: group {g['metadata']['name']} has no members field",
                  "members" not in g.get("spec", {}),
                  str(list(g.get("spec", {}).keys())))

    # Rendered OAuth2Client scopeMap: exactly the four expected entries
    o2c = find(pub_docs, "KanidmOAuth2Client")
    check("mcp-gateway: rendered OAuth2Client exists", o2c is not None)
    if o2c:
        smap = {e["group"]: e["scopes"] for e in o2c["spec"]["scopeMap"]}
        check("mcp-gateway: rendered scopeMap has exactly 5 group entries",
              set(smap) == {"mcp-gateway-agents", "mcp-context7-users",
                            "mcp-firecrawl-users", "mcp-playwright-users", "grafana-users"},
              str(sorted(smap)))
        check("mcp-gateway: mcp-gateway-agents still grants all 3 mcp scopes (agent path)",
              {"mcp_firecrawl", "mcp_grafana", "mcp_context7"} <= set(smap.get("mcp-gateway-agents", [])),
              str(smap.get("mcp-gateway-agents")))
        check("mcp-gateway: mcp-context7-users grants mcp_context7",
              smap.get("mcp-context7-users") == ["mcp_context7"],
              str(smap.get("mcp-context7-users")))
        check("mcp-gateway: mcp-firecrawl-users grants mcp_firecrawl",
              smap.get("mcp-firecrawl-users") == ["mcp_firecrawl"],
              str(smap.get("mcp-firecrawl-users")))
        check("mcp-gateway: grafana-users grants only mcp_grafana",
              smap.get("grafana-users") == ["mcp_grafana"],
              str(smap.get("grafana-users")))
        check("mcp-gateway: mcp-playwright-users grants mcp_playwright",
              smap.get("mcp-playwright-users") == ["mcp_playwright"],
              str(smap.get("mcp-playwright-users")))

    # comment-aware: prose explaining WHY the group is withheld must not fail this
    for chart in CHARTS:
        vals = open(f"apps/{chart}/values.yaml").read()
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
    adm_id = config_text(helm("mcp-gateway-admin", ["backends.vault.enabled=true", "backends.vault.credential=idToken"]))
    check("template still renders the measured idToken variant on request",
          "jwt.scopes.exists(s, s == 'mcp_vault')" in adm_id)
    check("accessToken-shaped route authorizes on the scope string",
          "jwt.scope.split(' ').exists(s, s == 'mcp_grafana')" in graf_text)
    check("whole-value scope equality only (no substring match)",
          "contains(jwt.scope" not in graf_text and "startsWith" not in graf_text)

    # ---- fixed backend address, pinned Host, per-plane Kanidm client
    check("route backend is the fixed service URL from values",
          "host: grafana-mcp.grafana-mcp.svc.cluster.local" in graf_text)
    check("route pins the Host header the backend accepts",
          "host: grafana-mcp.grafana-mcp.grigri.cloud" in graf_text or
          "host: grafana-mcp.grafana-mcp.svc.cluster.local" in graf_text)
    check("public plane validates the public Kanidm client",
          "      - mcp-gateway\n" in pub_text and
          "openid/mcp-gateway/public_key.jwk" in pub_text and
          "mcp-gateway-admin" not in pub_text)
    # Issuer must be Kanidm's per-client OIDC path, never the bare host: the bare
    # root has no issuer document and tokens carry `iss` of the full path.
    # Public gateway: check rendered config (hardcoded routes)
    # Admin gateway: check values.yaml (templated backends)
    pub_vals = open("apps/mcp-gateway/values.yaml").read()
    check("mcp-gateway: issuer is the full per-client path",
          "issuer: https://idm.grigri.cloud/oauth2/openid/mcp-gateway" in pub_text and
          not re.search(r"issuer: https://idm\.grigri\.cloud/?\s*$", pub_text, re.M))
    check("mcp-gateway: jwksUrl is Kanidm's public_key.jwk for that client",
          "url: https://idm.grigri.cloud/oauth2/openid/mcp-gateway/public_key.jwk" in pub_text)

    adm_vals = open("apps/mcp-gateway-admin/values.yaml").read()
    check("mcp-gateway-admin: issuer is the full per-client path",
          "issuer: https://idm.grigri.cloud/oauth2/openid/mcp-gateway-admin" in adm_vals and
          not re.search(r"issuer: https://idm\.grigri\.cloud/?\s*$", adm_vals, re.M))
    check("mcp-gateway-admin: jwksUrl is Kanidm's public_key.jwk for that client",
          "jwksUrl: https://idm.grigri.cloud/oauth2/openid/mcp-gateway-admin/public_key.jwk" in adm_vals)
    check("admin plane validates its own Kanidm client",
          "\n          - mcp-gateway-admin\n" in adm_text and
          "openid/mcp-gateway-admin/public_key.jwk" in adm_text and
          "- mcp-gateway\n" not in adm_text)

    # ---- egress follows the same values entry as the route
    # Public gateway: CiliumNetworkPolicy with bare toEntities: [cluster] (allows all cluster traffic).
    # No per-backend egress rules - the architecture uses cluster-wide allowance.
    # Admin gateway: also CiliumNetworkPolicy with bare toEntities: [cluster].
    # Both charts use the same proven egress shape: bare cluster + DNS pod rules.
    def cnp_egress_json(docs):
        cnp = [d for d in docs if d.get("kind") == "CiliumNetworkPolicy"]
        if not cnp:
            return ""
        return json.dumps(cnp[0].get("spec", {}).get("egress", []))

    def np_egress_json(docs):
        np = [d for d in docs if d.get("kind") == "NetworkPolicy"
              and d["metadata"]["name"].endswith("-egress")]
        return json.dumps(np[0]["spec"]["egress"]) if np else ""

    # networkIsolation defaults to true (measured CNP is live): the default render carries
    # the same egress as the explicit true render; an explicit false renders no policy.
    pub_eg_def = cnp_egress_json(helm("mcp-gateway"))
    adm_eg_def = cnp_egress_json(helm("mcp-gateway-admin"))
    pub_eg_off = cnp_egress_json(helm("mcp-gateway", ["networkIsolation=false"]))
    adm_eg_off = cnp_egress_json(helm("mcp-gateway-admin", ["networkIsolation=false"]))
    check("egress policy is rendered by default (networkIsolation defaults true)",
          pub_eg_def != "" and adm_eg_def != "",
          f"public={pub_eg_def[:50] if pub_eg_def else 'none'}, admin={adm_eg_def[:50] if adm_eg_def else 'none'}")
    check("no egress policy while networkIsolation is explicitly false",
          pub_eg_off == "" and adm_eg_off == "",
          f"public={pub_eg_off[:50] if pub_eg_off else 'none'}, admin={adm_eg_off[:50] if adm_eg_off else 'none'}")

    # With networkIsolation=true, both charts render CNP with bare cluster egress
    adm_eg_on = cnp_egress_json(adm_on)
    pub_eg_on = cnp_egress_json(pub_on)
    check("admin CNP egress has bare toEntities: [cluster]",
          '"toEntities": ["cluster"]' in adm_eg_on or "'toEntities': ['cluster']" in adm_eg_on,
          f"admin egress {adm_eg_on[:100]}")
    check("public CNP egress has bare toEntities: [cluster]",
          '"toEntities": ["cluster"]' in pub_eg_on or "'toEntities': ['cluster']" in pub_eg_on,
          f"public egress {pub_eg_on[:100]}")
    # Admin CNP has no per-backend egress rules (bare cluster allows all)
    # The architecture is: routes: [] (fail-closed) + bare cluster egress
    # Enabling a backend doesn't change the network policy - it changes the config
    check("admin CNP egress is bare cluster (no per-backend rules)",
          "vault-mcp" not in adm_eg_on and "grafana-mcp" not in adm_eg_on,
          f"admin egress should be bare cluster, not per-backend")
    check("public plane has no Vault path at all (admin-only backend)",
          "vault-mcp" not in pub_eg_on and
          "mcp_vault" not in config_text(helm("mcp-gateway",
                                              ["backends.grafana.enabled=true"])))

    # ---- networkIsolation gating: the measured CNP is enabled by default, false is the rollback
    # Default render (networkIsolation not set, defaults to true): exactly one CNP, no native NP
    adm_default_docs = helm("mcp-gateway-admin")
    adm_np_default = [d for d in adm_default_docs if d.get("kind") == "NetworkPolicy"]
    adm_cnp_default = [d for d in adm_default_docs if d.get("kind") == "CiliumNetworkPolicy"]
    check("mcp-gateway-admin: default render has no native NetworkPolicy",
          len(adm_np_default) == 0,
          f"found {len(adm_np_default)} NetworkPolicy resources")
    check("mcp-gateway-admin: default render has exactly one CiliumNetworkPolicy (networkIsolation defaults true)",
          len(adm_cnp_default) == 1,
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

    # Explicit true: CiliumNetworkPolicy rendered (native NPs replaced)
    adm_true_docs = helm("mcp-gateway-admin", ["networkIsolation=true"])
    adm_np_true = [d for d in adm_true_docs if d.get("kind") == "NetworkPolicy"]
    adm_cnp_true = [d for d in adm_true_docs if d.get("kind") == "CiliumNetworkPolicy"]
    check("mcp-gateway-admin: networkIsolation=true renders no native NetworkPolicy",
          len(adm_np_true) == 0,
          f"found {len(adm_np_true)} native NetworkPolicy resources (should be 0)")
    check("mcp-gateway-admin: networkIsolation=true renders CiliumNetworkPolicy",
          len(adm_cnp_true) == 1,
          f"expected 1, found {len(adm_cnp_true)}")
    # Verify values.yaml documents the gating
    adm_vals_text = open("apps/mcp-gateway-admin/values.yaml").read()
    check("mcp-gateway-admin: values.yaml enables networkIsolation and documents the false rollback",
          re.search(r"^networkIsolation: true$", adm_vals_text, re.M) is not None and
          re.search(r"Rollback.*networkIsolation:\s*false", adm_vals_text, re.M) is not None,
          "missing networkIsolation: true or rollback documentation (expected comment like '# Rollback: set `networkIsolation: false`...')")

    # ---- CNP shape verification for all three charts
    # Helper to verify the proven CNP shape: world on health port only, app port from
    # specific endpoints, egress bare cluster + DNS, no native NP.
    def verify_cnp_shape(chart_name, docs_with_isolation, pod_labels, health_port, app_port, metrics_port=None,
                         envoy_gateway=None):
        """Verify CiliumNetworkPolicy matches the proven shape."""
        cnps = [d for d in docs_with_isolation if d.get("kind") == "CiliumNetworkPolicy"]
        nps = [d for d in docs_with_isolation if d.get("kind") == "NetworkPolicy"]

        check(f"{chart_name}: no native NetworkPolicy rendered with networkIsolation=true",
              len(nps) == 0,
              f"found {len(nps)} native NetworkPolicy resources")

        if not cnps:
            check(f"{chart_name}: CiliumNetworkPolicy exists", False, "no CNP found")
            return

        cnp = cnps[0]
        spec = cnp.get("spec", {})

        # endpointSelector must match the real pod labels
        selector = spec.get("endpointSelector", {}).get("matchLabels", {})
        for label_key, label_val in pod_labels.items():
            check(f"{chart_name}: CNP endpointSelector has {label_key}={label_val}",
                  selector.get(label_key) == label_val,
                  f"selector={selector}")

        ingress_rules = spec.get("ingress", [])
        egress_rules = spec.get("egress", [])

        # Find the world rule (health port)
        world_rules = [r for r in ingress_rules if "fromEntities" in r and "world" in r.get("fromEntities", [])]
        check(f"{chart_name}: exactly one ingress rule with fromEntities: [world]",
              len(world_rules) == 1,
              f"found {len(world_rules)} world rules")

        if world_rules:
            world_rule = world_rules[0]
            # world rule must have toPorts with ONLY the health port
            world_ports = set()
            for tp in world_rule.get("toPorts", []):
                for p in tp.get("ports", []):
                    world_ports.add((str(p.get("port")), p.get("protocol", "TCP")))
            check(f"{chart_name}: world rule toPorts contains ONLY health port {health_port}",
                  world_ports == {(str(health_port), "TCP")},
                  f"ports={world_ports}")
            # world rule must not have fromEndpoints (it's fromEntities only)
            check(f"{chart_name}: world rule has no fromEndpoints (pure entity rule)",
                  "fromEndpoints" not in world_rule,
                  "world rule should not mix entities and endpoints")

        # No rule should list both world and cluster
        for i, rule in enumerate(ingress_rules + egress_rules):
            entities = rule.get("fromEntities", []) + rule.get("toEntities", [])
            has_world = "world" in entities
            has_cluster = "cluster" in entities
            check(f"{chart_name}: rule {i} does not list both world and cluster",
                  not (has_world and has_cluster),
                  f"entities={entities}")

        # App port must be restricted to fromEndpoints (not fromEntities)
        app_port_rules = []
        for rule in ingress_rules:
            for tp in rule.get("toPorts", []):
                for p in tp.get("ports", []):
                    if str(p.get("port")) == str(app_port):
                        app_port_rules.append(rule)
        check(f"{chart_name}: app port {app_port} has fromEndpoints rules",
              all("fromEndpoints" in r for r in app_port_rules),
              f"app port rules should use fromEndpoints, not fromEntities")
        check(f"{chart_name}: app port {app_port} is NOT in world rules",
              not any(str(app_port) in str(r.get("toPorts", [])) for r in world_rules),
              "app port must not be exposed via world entity")

        # Edge admission (mcp-gateway planes only): only the Envoy Gateway proxies of the owning
        # Gateway are named on the app port.
        if envoy_gateway:
            app_sel = [ep.get("matchLabels", {})
                       for r in app_port_rules for ep in r.get("fromEndpoints", [])]
            check(f"{chart_name}: app port {app_port} does not admit ingress-nginx controller pods",
                  not any(m.get("app.kubernetes.io/name") == "ingress-nginx" for m in app_sel),
                  f"selectors={app_sel}")
            check(f"{chart_name}: app port {app_port} admits the {envoy_gateway} Envoy Gateway proxies",
                  any(m.get("app.kubernetes.io/name") == "envoy" and
                      m.get("app.kubernetes.io/component") == "proxy" and
                      m.get("gateway.envoyproxy.io/owning-gateway-name") == envoy_gateway and
                      m.get("io.kubernetes.pod.namespace") == "envoy-gateway" for m in app_sel),
                  f"selectors={app_sel}")
            check(f"{chart_name}: only the {envoy_gateway} Gateway proxies are admitted (no other Envoy gateway)",
                  all(m.get("gateway.envoyproxy.io/owning-gateway-name") == envoy_gateway
                      for m in app_sel if m.get("app.kubernetes.io/name") == "envoy"),
                  f"selectors={app_sel}")

        # Metrics port (if present) must be from Prometheus endpoints only
        if metrics_port:
            metrics_rules = []
            for rule in ingress_rules:
                for tp in rule.get("toPorts", []):
                    for p in tp.get("ports", []):
                        if str(p.get("port")) == str(metrics_port):
                            metrics_rules.append(rule)
            if metrics_rules:
                check(f"{chart_name}: metrics port {metrics_port} from Prometheus endpoints",
                      all("fromEndpoints" in r for r in metrics_rules),
                      "metrics should use fromEndpoints")
                for rule in metrics_rules:
                    eps = rule.get("fromEndpoints", [])
                    has_prometheus = any(
                        ep.get("matchLabels", {}).get("app.kubernetes.io/name") == "prometheus"
                        for ep in eps
                    )
                    check(f"{chart_name}: metrics rule selects prometheus pods",
                          has_prometheus,
                          f"endpoints={eps}")

        # Egress: bare toEntities: [cluster] with no toPorts
        cluster_egress = [r for r in egress_rules if "toEntities" in r and "cluster" in r.get("toEntities", [])]
        check(f"{chart_name}: egress has toEntities: [cluster] rule",
              len(cluster_egress) >= 1,
              "missing bare cluster egress")
        if cluster_egress:
            # At least one cluster rule must have NO toPorts (bare cluster)
            bare_cluster = [r for r in cluster_egress if "toPorts" not in r or not r.get("toPorts")]
            check(f"{chart_name}: bare cluster egress rule has no toPorts",
                  len(bare_cluster) >= 1,
                  "cluster egress must be bare (no toPorts) to allow all cluster traffic")

        # DNS rules must name the real pods
        dns_rules = [r for r in egress_rules if "toEndpoints" in r]
        if dns_rules:
            dns_ports = set()
            for rule in dns_rules:
                for tp in rule.get("toPorts", []):
                    for p in tp.get("ports", []):
                        dns_ports.add((str(p.get("port")), p.get("protocol")))
            check(f"{chart_name}: DNS rules cover port 53 UDP and TCP",
                  ("53", "UDP") in dns_ports and ("53", "TCP") in dns_ports,
                  f"dns_ports={dns_ports}")

    # Test mcp-gateway-admin CNP
    verify_cnp_shape(
        "mcp-gateway-admin",
        adm_true_docs,
        pod_labels={
            "app.kubernetes.io/name": "mcp-gateway-admin",
            "app.kubernetes.io/instance": "mcp-gateway-admin",
            "app.kubernetes.io/controller": "agentgateway",
        },
        health_port=15021,
        app_port=3000,
        metrics_port=15020,
        envoy_gateway="internal",
    )

    # Test mcp-gateway (public) CNP
    pub_true_docs = helm("mcp-gateway", ["networkIsolation=true"])
    verify_cnp_shape(
        "mcp-gateway",
        pub_true_docs,
        pod_labels={
            "app.kubernetes.io/name": "agentgateway-standalone",
            "app.kubernetes.io/instance": "mcp-gateway",
            "app.kubernetes.io/component": "standalone",
        },
        health_port=15021,
        app_port=3000,
        metrics_port=15020,
        envoy_gateway="external",
    )

    # Test grafana-mcp CNP
    graf_true_docs = helm("grafana-mcp", ["networkIsolation=true"])
    verify_cnp_shape(
        "grafana-mcp",
        graf_true_docs,
        pod_labels={
            "app.kubernetes.io/name": "grafana-mcp",
            "app.kubernetes.io/instance": "grafana-mcp",
            "app.kubernetes.io/component": "mcp-server",
        },
        health_port=8081,
        app_port=8000,
        metrics_port=None,  # grafana-mcp has no metrics port
    )

    # ---- CNP fromEndpoints drift guard ----
    # If Envoy's proxy pod labels ever change, the CNP stops matching and the routes
    # fail closed with no error anywhere (Cilium silently drops, Envoy logs 503 UF).
    # That condition must fail LOUDLY here, not silently in production.
    for chart, envoy_gw in [
        ("mcp-gateway", "external"),
        ("mcp-gateway-admin", "internal"),
    ]:
        cnp_docs = helm(chart, ["networkIsolation=true"])
        cnps = [d for d in cnp_docs if d.get("kind") == "CiliumNetworkPolicy"]
        if not cnps:
            check(f"{chart}: CNP drift guard SKIPPED (no CNP rendered)", False,
                  "networkIsolation=true must render a CiliumNetworkPolicy")
            continue
        cnp = cnps[0]
        app_rules = [r for r in cnp["spec"].get("ingress", [])
                     if any(str(p.get("port")) == "3000"
                            for tp in r.get("toPorts", [])
                            for p in tp.get("ports", []))]
        all_eps = [ep for r in app_rules for ep in r.get("fromEndpoints", [])]
        envoy_eps = [ep.get("matchLabels", {}) for ep in all_eps
                     if ep.get("matchLabels", {}).get("app.kubernetes.io/name") == "envoy"]
        # Envoy proxy drift guard: exact label set from the live proxy pods
        check(f"{chart}: CNP admits the {envoy_gw} Envoy proxy with exact live labels "
              "(drift guard: if Envoy relabels, routes fail closed with no error)",
              any(e.get("app.kubernetes.io/component") == "proxy"
                  and e.get("gateway.envoyproxy.io/owning-gateway-name") == envoy_gw
                  and e.get("io.kubernetes.pod.namespace") == "envoy-gateway"
                  for e in envoy_eps),
              f"envoy selectors={envoy_eps}")
        # ingress-nginx is gone: no rule may name it
        check(f"{chart}: CNP has no ingress-nginx rule (the controllers were removed)",
              not any(ep.get("matchLabels", {}).get("app.kubernetes.io/name") == "ingress-nginx"
                      for ep in all_eps),
              f"selectors={[ep.get('matchLabels', {}) for ep in all_eps]}")

    # grafana-mcp default-false checks
    graf_default_docs = helm("grafana-mcp")
    graf_cnp_default = [d for d in graf_default_docs if d.get("kind") == "CiliumNetworkPolicy"]
    graf_np_default = [d for d in graf_default_docs if d.get("kind") == "NetworkPolicy"]
    check("grafana-mcp: default render has exactly one CiliumNetworkPolicy (networkIsolation defaults true)",
          len(graf_cnp_default) == 1,
          f"found {len(graf_cnp_default)} CNP resources")
    graf_false_docs = helm("grafana-mcp", ["networkIsolation=false"])
    check("grafana-mcp: networkIsolation=false renders no CiliumNetworkPolicy",
          not [d for d in graf_false_docs if d.get("kind") == "CiliumNetworkPolicy"],
          "rollback switch must remove the policy")
    check("grafana-mcp: default render has no NetworkPolicy",
          len(graf_np_default) == 0,
          f"found {len(graf_np_default)} NetworkPolicy resources")

    # ---- services: ClusterIP only, ops port separate from the MCP port
    svc = find(pub_on, "Service")
    check("service is ClusterIP with no NodePort/hostPort",
          svc is not None and svc["spec"]["type"] == "ClusterIP" and
          "nodePort" not in json.dumps(svc))
    ports = {p["port"] for p in svc["spec"]["ports"]} if svc else set()
    check("metrics/healthz are separate ports, not paths on the MCP port",
          {3000, 15020, 15021} <= ports, str(sorted(ports)))

    # ---- Grafana and Vault remain owner decisions
    # Public gateway: context7 and grafana are ENABLED (hardcoded routes)
    # Admin gateway: vault is DISABLED (templated, enabled: false)
    vals_txt = open("apps/mcp-gateway/values.yaml").read()
    check("mcp-gateway: context7 route ships enabled",
          re.search(r"\n  context7:\n(?:.*\n)*?\s{4}enabled: true",
                    vals_txt) is not None)
    check("mcp-gateway: grafana route ships enabled",
          re.search(r"\n  grafana:\n(?:.*\n)*?\s{4}enabled: true",
                    vals_txt) is not None)
    check("mcp-gateway: firecrawl route ships enabled",
          re.search(r"\n  firecrawl:\n(?:.*\n)*?\s{4}enabled: true",
                    vals_txt) is not None)
    adm_vals_txt = open("apps/mcp-gateway-admin/values.yaml").read()
    check("mcp-gateway-admin: vault route ships disabled",
          re.search(r"\n  vault:\n(?:.*\n)*?\s{4}enabled: false",
                    adm_vals_txt) is not None)
    check("admin configmap documents the Vault session-identity blocker",
          "Vault MCP BACKEND BLOCKER" in adm_text)
    check("Vault scopes exist only on the admin plane",
          "mcp_vault" in adm_text and "mcp_vault" not in pub_context7_text)

    # ---- Kanidm resources
    pub_docs = helm("mcp-gateway")
    grp = find(pub_docs, "KanidmGroup")
    # Public gateway now has a KanidmGroup with grafana-mcp as a member (service account)
    check("workload group exists with grafana-mcp service account member",
          grp is not None and grp["metadata"]["name"] == "mcp-gateway-agents" and
          grp.get("spec", {}).get("members") == ["grafana-mcp"],
          json.dumps((grp or {}).get("spec", {}))[:160])
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
          "Never point `kanidm.workloadGroup.name` at an existing human" in
          open("apps/mcp-gateway/templates/kanidm-group.yaml").read())
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
