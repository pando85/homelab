#!/usr/bin/env python3
"""RFC 9728 discovery on agentgateway v1.7.0, solved natively with `directResponse`.

Ported from the throwaway `/tmp/opencode/access-gate-proof/discovery_proof.py` evidence
(21/21 passed, reproduced twice on 2026-11-09). Same assertions, portable harness: the binary is
resolved through gatefixture, ports are runtime-assigned, the work directory is disposable, and
the authority names are reserved-test stand-ins (`mcp.test` / `idp.test`) instead of the live
hostname, so the suite is not coupled to a deployment and needs no DNS - the gateway only ever
sees a Host header on 127.0.0.1. Override with AG_AUTHORITY / AG_ISSUER to re-run it against the
real strings before a cutover.

Why it exists: a strict `mcpAuthentication` route answers 401 with a `resource_metadata`
challenge, but the pinned binary does not serve that document, and a `pathPrefix` route on the
metadata path is forwarded to the MCP backend instead of being answered (measured: 405). The
gateway CAN answer it itself, with a plain static route.

Phase 1 - ask the gateway what it advertises, for both routes, so the paths are known exactly.
Phase 2 - serve exactly those paths with `policies.directResponse` and prove:
  * 200 with NO bearer, valid JSON object, `content-type: application/json`,
    `cache-control: public, max-age=300`;
  * `resource`, `authorization_servers` and `scopes_supported` carry the configured values,
    with per-route scopes - the two documents never share a scope list;
  * the metadata requests reach NEITHER MCP backend (no credential path exists on it);
  * the gate is untouched: no bearer 401, correct scope 200, `email` claim 403,
    cross-target scope 403;
  * a metadata path UNDER the MCP prefix stays identity-gated (401), so the static route cannot
    be used to override or bypass the gate;
  * the static route is host-scoped: a different Host header is not served (404).

Schema facts this suite encodes, all measured against the pinned binary rather than assumed:
`directResponse` is `{status, body | bodyExpression, headers}`; `body` is the LITERAL payload in
local config (base64 appears only in exported/serialized config, and feeding base64 in serves
base64 back); `body` and `bodyExpression` are mutually exclusive; `headers` values are CEL
expressions, so a literal needs inner quotes (`content-type: "'application/json'"`).

    python3 test/mcp-gateway/discovery-checks.py

Requires: the pinned agentgateway binary (see gatefixture.FETCH_HELP), python3 with PyJWT and
cryptography. Exit 0 all passed, 1 a check failed, 3 skipped (artifact missing, nothing proven),
4 the fixture could not start. KEEP=1 leaves the work directories behind.
"""

import json
import os
import sys
import textwrap
import urllib.parse

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

from gatefixture import (
    AUDIENCE,
    CaptureBackend,
    Checks,
    Gateway,
    HarnessError,
    Skip,
    SyntheticIdp,
    banner,
    binary_fingerprint,
    http_get,
    mcp_post,
    render,
    resolve_binary,
    skipped,
)

SUITE = "discovery"
AUTHORITY = os.environ.get("AG_AUTHORITY", "mcp.test")
PROD_ISSUER = os.environ.get("AG_ISSUER", f"https://idp.test/oauth2/openid/{AUDIENCE}")
TARGETS = ("grafana", "context7")
MCP_PATH = {name: f"/{name}/mcp" for name in TARGETS}
SCOPE = {name: f"mcp_{name}" for name in TARGETS}
EXPECTED_ORIGIN = f"https://{AUTHORITY}"
RESOURCE = {name: f"https://{AUTHORITY}{MCP_PATH[name]}" for name in TARGETS}
METADATA_PATH = {name: f"/.well-known/oauth-protected-resource{MCP_PATH[name]}" for name in TARGETS}

BASE = """
config:
  adminAddr: "off"
  readinessAddr: "off"
  backend:
    allowDynamicLoopback: true
  logging: {level: warn, format: json}
gateways:
  default:
    port: @@GW@@
    bindAddress: "127.0.0.1"
routes:
"""

MCP_ROUTE = """- name: @@NAME@@
  hostnames: ["@@AUTHORITY@@"]
  matches:
  - path: {pathPrefix: "@@PREFIX@@"}
  policies:
    mcpAuthentication:
      mode: strict
      issuer: "@@IDP@@"
      audiences: ["@@AUD@@"]
      jwks: {url: "@@IDP@@/jwks"}
      resourceMetadata:
        resource: "@@RESOURCE@@"
        scopesSupported: ["@@SCOPE@@"]
    authorization:
      rules:
      - require: "!has(jwt.email)"
      - require: "!has(jwt.preferred_username)"
      - require: "has(jwt.scope) && jwt.scope.split(' ').exists(s, s == '@@SCOPE@@')"
    requestHeaderModifier:
      remove: ["Cookie", "Authorization"]
  backends:
  - mcp:
      targets:
      - name: @@NAME@@
        mcp: {host: "127.0.0.1", port: @@PORT@@, path: "/mcp"}
      statefulMode: stateless
"""

METADATA_ROUTE = """- name: @@NAME@@-metadata
  hostnames: ["@@AUTHORITY@@"]
  matches:
  - path: {exact: "@@PREFIX@@"}
  policies:
    directResponse:
      status: 200
      body: |-
@@BODY@@
      headers:
        content-type: "'application/json'"
        cache-control: "'public, max-age=300'"
"""


def metadata_document(name):
    """The RFC 9728 document, exactly as the discovery config must carry it."""
    return json.dumps({
        "resource": RESOURCE[name],
        "authorization_servers": [PROD_ISSUER],
        "scopes_supported": [SCOPE[name]],
        "bearer_methods_supported": ["header"],
        "resource_name": f"mcp plane {name} MCP",
    }, indent=2)


def mcp_routes(ports, idp_origin):
    return "".join(render(
        MCP_ROUTE, NAME=name, AUTHORITY=AUTHORITY, PREFIX=MCP_PATH[name], IDP=idp_origin,
        AUD=AUDIENCE, RESOURCE=RESOURCE[name], SCOPE=SCOPE[name], PORT=ports[name])
        for name in TARGETS)


def metadata_routes():
    out = []
    for name in TARGETS:
        body = textwrap.indent(metadata_document(name), " " * 10)
        out.append(render(METADATA_ROUTE, NAME=name, AUTHORITY=AUTHORITY,
                          PREFIX=METADATA_PATH[name], BODY=body))
    return "".join(out)


def advertised_metadata(port, name):
    """What the gateway's own 401 challenge points clients at, for one MCP route."""
    st, hdr, _ = mcp_post(port, MCP_PATH[name], host=AUTHORITY)
    www = hdr.get("www-authenticate") or hdr.get("WWW-Authenticate") or ""
    url = www.split('resource_metadata="')[1].split('"')[0] if 'resource_metadata="' in www else ""
    return st, url


def phase1(checks, binary, idp, ports):
    """Ask the pinned binary which paths it advertises - no guessing, no ingress snippet."""
    def build(port):
        return render(BASE, GW=port) + mcp_routes(ports, idp.origin)

    with Gateway(binary, build, label="discovery-phase1") as gateway:
        for name in TARGETS:
            st, url = advertised_metadata(gateway.port, name)
            parsed = urllib.parse.urlparse(url)
            checks.add(f"P1-{name}", f"{name}: the strict route advertises the RFC9728 document "
                                     f"at its own resource path", parsed.path, METADATA_PATH[name],
                       f"status={st} challenge={url}")
            checks.add(f"P1-{name}-origin",
                       f"{name}: the challenge names the configured authority, not the local "
                       f"socket the client actually used",
                       f"{parsed.scheme}://{parsed.netloc}", EXPECTED_ORIGIN,
                       f"status={st} challenge={url}")


def phase2(checks, binary, idp, ports, grafana, context7):
    """Serve exactly those paths with directResponse and prove the gate is untouched."""
    def build(port):
        return render(BASE, GW=port) + mcp_routes(ports, idp.origin) + metadata_routes()

    with Gateway(binary, build, label="discovery-phase2") as gateway:
        gw = gateway.port
        good_graf = idp.mint()
        for name in TARGETS:
            st, hdr, body = http_get(gw, METADATA_PATH[name], host=AUTHORITY)
            try:
                doc = json.loads(body)
            except ValueError:
                doc = None
            checks.add(f"M-{name}", f"{name} metadata answers 200 with NO bearer", st, 200)
            checks.add(f"M-{name}-json", f"{name} metadata is a valid JSON object",
                       isinstance(doc, dict), True, f"body={body[:60]!r}")
            checks.add(f"M-{name}-ct", f"{name} metadata content-type",
                       hdr.get("content-type"), "application/json")
            checks.add(f"M-{name}-cache", f"{name} metadata cache-control",
                       hdr.get("cache-control"), "public, max-age=300")
            checks.add(f"M-{name}-res", f"{name} metadata resource == {RESOURCE[name]}",
                       (doc or {}).get("resource"), RESOURCE[name])
            checks.add(f"M-{name}-as", f"{name} metadata authorization_servers == the IdP issuer",
                       (doc or {}).get("authorization_servers"), [PROD_ISSUER])
            checks.add(f"M-{name}-scope", f"{name} metadata scopes_supported == [{SCOPE[name]}]",
                       (doc or {}).get("scopes_supported"), [SCOPE[name]])

        grafana.clear()
        context7.clear()
        for name in TARGETS:
            http_get(gw, METADATA_PATH[name], host=AUTHORITY)
        checks.add("M-no-backend", "metadata requests hit NEITHER MCP backend (no credential path)",
                   len(grafana.records) + len(context7.records), 0,
                   f"grafana={len(grafana.records)} context7={len(context7.records)}")

        checks.add("G-no-bearer", "MCP grafana still 401 without a bearer",
                   mcp_post(gw, MCP_PATH["grafana"], None, host=AUTHORITY)[0], 401)
        checks.add("G-valid", "MCP grafana still 200 with the correct scope",
                   mcp_post(gw, MCP_PATH["grafana"], good_graf, host=AUTHORITY)[0], 200)
        checks.add("G-email", "MCP grafana still 403 carrying an email claim",
                   mcp_post(gw, MCP_PATH["grafana"], idp.mint(email="person@example.invalid"),
                            host=AUTHORITY)[0], 403)
        checks.add("C-cross", "MCP context7 still 403 for the grafana scope",
                   mcp_post(gw, MCP_PATH["context7"], good_graf, host=AUTHORITY)[0], 403)

        st, _, _ = http_get(gw, f"{MCP_PATH['grafana']}/.well-known/oauth-protected-resource",
                            host=AUTHORITY)
        checks.add("G-underprefix", "a metadata path UNDER the MCP prefix stays identity-gated "
                                    "(the static route cannot override or bypass the gate)",
                   st, 401)

        st, _, body = http_get(gw, METADATA_PATH["grafana"], host="other.test")
        checks.add("M-host-scoped", "the metadata route is host-scoped (a different Host is not "
                                    "served)", st, 404, f"body={body[:60]!r}")


def run(binary, version):
    checks = Checks(SUITE)
    idp = SyntheticIdp()
    grafana = CaptureBackend("grafana")
    context7 = CaptureBackend("context7")
    ports = {"grafana": grafana.port, "context7": context7.port}
    try:
        banner([("binary", f"{binary} (v{version['version']} git {version['git_revision'][:12]})"),
                ("binary sha256", binary_fingerprint(binary)),
                ("authority", f"{AUTHORITY} (Host header only, no DNS, loopback transport)"),
                ("issuer in doc", PROD_ISSUER),
                ("stub issuer", idp.origin),
                ("capture backends", f"grafana :{grafana.port} context7 :{context7.port}")])
        phase1(checks, binary, idp, ports)
        phase2(checks, binary, idp, ports, grafana, context7)
        return checks
    finally:
        idp.stop()
        grafana.stop()
        context7.stop()


def main():
    try:
        binary, version = resolve_binary()
    except HarnessError as error:
        print(f"ERROR suite={SUITE}: {error}")
        return 4
    except Skip as error:  # artifact missing or wrong version: nothing was proven
        return skipped(SUITE, error)
    try:
        checks = run(binary, version)
    except HarnessError as error:
        print(f"ERROR suite={SUITE}: fixture failed to start, no result was produced\n{error}")
        return 4
    return 1 if checks.report() else 0


if __name__ == "__main__":
    sys.exit(main())
