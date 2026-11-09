#!/usr/bin/env python3
"""Access-gate runtime checks against the pinned agentgateway v1.7.0 binary.

Ported from the throwaway `/tmp/opencode/access-gate-proof/proof.py` evidence (39/39 passed,
reproduced twice on 2026-11-09) into a committed, reproducible harness: the binary is resolved
from `AGENTGATEWAY_BIN`/PATH instead of a fixed /tmp path, ports are chosen at runtime, and the
work directory is disposable. Every security assertion is carried over unchanged, including the
negative ones and the two documented route-shape LIMITATIONS.

Nothing here touches a cluster or a real credential: the issuer is a runtime-generated ES256
stub, tokens are synthetic, backends are loopback capture servers, and credentials are only ever
reported as sha256[:12] or presence booleans.

The policy under test is the edge contract for the MCP plane:
  mcpAuthentication strict + audiences ["mcp-gateway"] + per-route scopesSupported,
  authorization rules that deny any token carrying email/preferred_username and require the
  route's own scope, requestHeaderModifier strips client credentials, backendAuth passthrough
  forwards the caller's own validated bearer, and the MCP backend runs stateless.

Check list (id - what it proves):
  A    no bearer -> 401                          B   wrong iss -> 401
  A2   the 401 challenge names OUR port (no stale process is answering)   C wrong aud -> 401
  D    token signed by an unknown key -> 401     E   expired -3600s -> 401
  E3   expired -45s -> 200 (60s leeway in the pinned JWT lib, measured behaviour)
  E2   nbf in the future -> 401
  F    scope mcp_grafana -> 200                  G   grafana token on the context7 route -> 403
  H    scope mcp_context7 -> 200                 I   scope as an array -> 403
  J    scope absent -> 403                       K   near-miss scope mcp_grafana_admin -> 403
  L    substring scope xmcp_grafana -> 403       M   multi-token scope string -> 200
  N    email claim present -> 403                O   email claim present but empty -> 403
  P    email claim present but falsy (0) -> 403  Q   preferred_username present -> 403
  R    preferred_username present but empty -> 403
  S    ID-token shape (email + username + scopes array, no scope) -> 403
  T    identity claims AND the right scope -> 403 (presence beats scope)
  U    the grafana backend receives exactly the caller's validated bearer
  Y0a  duplicate Authorization lines: the spoofed value never reaches the backend
  Y0b  spoofed Authorization FIRST -> 401 and the backend is never contacted
  V    inbound Cookie stripped                    W  inbound X-Grafana-URL stripped
  X    inbound X-Grafana-Service-Account-Token stripped
  Y    the forwarded bearer is still the caller's own after the spoof attempt
  Z    the context7 backend sees no Authorization (route removes it, no backendAuth)
  AA   the context7 backend sees no Cookie
  AB   stateless MCP: the gateway issues no Mcp-Session-Id
  AC   a foreign Mcp-Session-Id without a JWT -> 401 (no session resume before authentication)
  AD   a foreign Mcp-Session-Id with the wrong scope -> 403
  AE   a different subject forging a victim session id is forwarded with its OWN credential
  AF   LIMITATION (route shape): the advertised RFC 9728 path is forwarded to the backend,
       not answered by the gateway -> 405
  AG   LIMITATION (route shape): the under-prefix metadata path IS identity-gated -> 401
  AA1  on a route WITHOUT requestHeaderModifier.remove the client Cookie TRAVERSES the edge

    python3 test/mcp-gateway/access-gate-checks.py

Requires: the pinned agentgateway binary (see gatefixture.FETCH_HELP), python3 with PyJWT and
cryptography. Exit 0 all passed, 1 a check failed, 3 skipped (artifact missing, nothing proven),
4 the fixture could not start. KEEP=1 leaves the work directories behind.
"""

import os
import sys
import time
import urllib.parse

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

from gatefixture import (
    ABSENT,
    AUDIENCE,
    CaptureBackend,
    Checks,
    Gateway,
    HarnessError,
    Skip,
    SyntheticIdp,
    banner,
    binary_fingerprint,
    duplicate_auth_request,
    free_port,
    http_get,
    mcp_post,
    render,
    resolve_binary,
    send_raw,
    sha12,
    skipped,
)

SUITE = "access-gate"

CONFIG = """
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
- name: grafana
  matches:
  - path: {pathPrefix: "/grafana/mcp"}
  policies:
    mcpAuthentication:
      mode: strict
      issuer: "@@IDP@@"
      audiences: ["@@AUD@@"]
      jwks: {url: "@@IDP@@/jwks"}
      resourceMetadata:
        resource: "http://127.0.0.1:@@GW@@/grafana/mcp"
        scopesSupported: ["mcp_grafana"]
    authorization:
      rules:
      - require: "!has(jwt.email)"
      - require: "!has(jwt.preferred_username)"
      - require: "has(jwt.scope) && jwt.scope.split(' ').exists(s, s == 'mcp_grafana')"
    requestHeaderModifier:
      remove: ["Cookie", "X-Grafana-URL", "X-Grafana-Service-Account-Token"]
    backendAuth:
      passthrough: {}
  backends:
  - mcp:
      targets:
      - name: grafana
        mcp: {host: "127.0.0.1", port: @@GRAF@@, path: "/mcp"}
      statefulMode: stateless
- name: grafana-meta
  matches:
  - path: {pathPrefix: "/.well-known/oauth-protected-resource/grafana/mcp"}
  backends:
  - mcp:
      targets:
      - name: grafana
        mcp: {host: "127.0.0.1", port: @@GRAF@@, path: "/mcp"}
      statefulMode: stateless
- name: context7-nostrip
  matches:
  - path: {pathPrefix: "/context7-nostrip/mcp"}
  policies:
    mcpAuthentication:
      mode: strict
      issuer: "@@IDP@@"
      audiences: ["@@AUD@@"]
      jwks: {url: "@@IDP@@/jwks"}
      resourceMetadata:
        resource: "http://127.0.0.1:@@GW@@/context7-nostrip/mcp"
        scopesSupported: ["mcp_context7"]
    authorization:
      rules:
      - require: "!has(jwt.email)"
      - require: "has(jwt.scope) && jwt.scope.split(' ').exists(s, s == 'mcp_context7')"
  backends:
  - mcp:
      targets:
      - name: context7
        mcp: {host: "127.0.0.1", port: @@CTX@@, path: "/mcp"}
      statefulMode: stateless
- name: context7
  matches:
  - path: {pathPrefix: "/context7/mcp"}
  policies:
    mcpAuthentication:
      mode: strict
      issuer: "@@IDP@@"
      audiences: ["@@AUD@@"]
      jwks: {url: "@@IDP@@/jwks"}
      resourceMetadata:
        resource: "http://127.0.0.1:@@GW@@/context7/mcp"
        scopesSupported: ["mcp_context7"]
    authorization:
      rules:
      - require: "!has(jwt.email)"
      - require: "!has(jwt.preferred_username)"
      - require: "has(jwt.scope) && jwt.scope.split(' ').exists(s, s == 'mcp_context7')"
    requestHeaderModifier:
      remove: ["Cookie", "Authorization", "X-Session-Id"]
  backends:
  - mcp:
      targets:
      - name: context7
        mcp: {host: "127.0.0.1", port: @@CTX@@, path: "/mcp"}
      statefulMode: stateless
"""

GRAFANA_PREFIX = "/grafana/mcp"
CONTEXT7_PREFIX = "/context7/mcp"
NOSTRIP_PREFIX = "/context7-nostrip/mcp"


def run(binary, version):
    checks = Checks(SUITE)
    idp = SyntheticIdp()
    grafana = CaptureBackend("grafana")
    context7 = CaptureBackend("context7")
    gateway = None
    try:
        def build(port):
            return render(CONFIG, GW=port, IDP=idp.origin, AUD=AUDIENCE,
                          GRAF=grafana.port, CTX=context7.port)

        gateway = Gateway(binary, build, label="access-gate")
        gw = gateway.port
        banner([("binary", f"{binary} (v{version['version']} git {version['git_revision'][:12]})"),
                ("binary sha256", binary_fingerprint(binary)),
                ("gateway port", gw),
                ("stub issuer", idp.origin),
                ("capture backends", f"grafana :{grafana.port} context7 :{context7.port}"),
                ("credentials", "reported as sha256[:12] / presence only")])

        good_graf = idp.mint()
        good_ctx = idp.mint(scope="openid profile mcp_context7", sub="acct-bbbb")
        exp = sha12(good_graf)

        st, hdr, _ = mcp_post(gw, GRAFANA_PREFIX)
        www = hdr.get("www-authenticate") or hdr.get("WWW-Authenticate") or ""
        checks.add("A", "missing JWT -> 401", st, 401, f"www={www[:90]}")
        checks.add("A2", "fingerprint: 401 challenge advertises OUR gateway port "
                         "(a stale process cannot answer for us)",
                   f"127.0.0.1:{gw}" in www, True)
        advertised = (www.split('resource_metadata="')[1].split('"')[0]
                      if 'resource_metadata="' in www else "")

        checks.add("B", "wrong iss -> 401",
                   mcp_post(gw, GRAFANA_PREFIX, idp.mint(iss=f"http://127.0.0.1:{free_port()}"))[0],
                   401)
        checks.add("C", "wrong aud -> 401",
                   mcp_post(gw, GRAFANA_PREFIX, idp.mint(aud="victim-other"))[0], 401)
        checks.add("D", "unknown signing key -> 401",
                   mcp_post(gw, GRAFANA_PREFIX,
                            idp.mint(signing_key=idp.other_key, signing_kid="other-key"))[0], 401)

        grafana.clear()
        now = int(time.time())
        checks.add("E", "expired (-3600s) -> 401",
                   mcp_post(gw, GRAFANA_PREFIX,
                            idp.mint(exp=now - 3600, iat=now - 7200, nbf=now - 7200))[0], 401,
                   f"reached_backend={len(grafana.records) > 0}")
        grafana.clear()
        checks.add("E3", "expired within the jsonwebtoken 60s leeway -> 200 "
                         "(measured behaviour of the pinned binary, not a policy we set)",
                   mcp_post(gw, GRAFANA_PREFIX,
                            idp.mint(exp=now - 45, iat=now - 7200, nbf=now - 7200))[0], 200)
        grafana.clear()
        checks.add("E2", "not-yet-valid (nbf in the future) -> 401",
                   mcp_post(gw, GRAFANA_PREFIX, idp.mint(nbf=now + 3600))[0], 401,
                   f"reached_backend={len(grafana.records) > 0}")

        checks.add("F", "valid scope mcp_grafana -> 200",
                   mcp_post(gw, GRAFANA_PREFIX, good_graf)[0], 200)
        checks.add("G", "grafana token on the context7 route (wrong scope) -> 403",
                   mcp_post(gw, CONTEXT7_PREFIX, good_graf)[0], 403)
        checks.add("H", "valid scope mcp_context7 -> 200",
                   mcp_post(gw, CONTEXT7_PREFIX, good_ctx)[0], 200)

        checks.add("I", "scope as ARRAY -> 403",
                   mcp_post(gw, GRAFANA_PREFIX, idp.mint(scope=["mcp_grafana"]))[0], 403)
        checks.add("J", "scope missing -> 403",
                   mcp_post(gw, GRAFANA_PREFIX, idp.mint(scope=ABSENT))[0], 403)
        checks.add("K", "near-miss scope -> 403",
                   mcp_post(gw, GRAFANA_PREFIX, idp.mint(scope="mcp_grafana_admin"))[0], 403)
        checks.add("L", "substring scope -> 403",
                   mcp_post(gw, GRAFANA_PREFIX, idp.mint(scope="xmcp_grafana"))[0], 403)
        checks.add("M", "scope as multi-token string -> 200",
                   mcp_post(gw, GRAFANA_PREFIX, idp.mint(scope="openid mcp_grafana x"))[0], 200)

        checks.add("N", "email claim present (non-empty) -> 403",
                   mcp_post(gw, GRAFANA_PREFIX, idp.mint(email="person@example.invalid"))[0], 403)
        checks.add("O", "email claim present (EMPTY string) -> 403",
                   mcp_post(gw, GRAFANA_PREFIX, idp.mint(email=""))[0], 403)
        checks.add("P", "email claim present (falsy 0) -> 403",
                   mcp_post(gw, GRAFANA_PREFIX, idp.mint(email=0))[0], 403)
        checks.add("Q", "preferred_username present -> 403",
                   mcp_post(gw, GRAFANA_PREFIX, idp.mint(preferred_username="someuser"))[0], 403)
        checks.add("R", "preferred_username present (EMPTY) -> 403",
                   mcp_post(gw, GRAFANA_PREFIX, idp.mint(preferred_username=""))[0], 403)
        checks.add("S", "ID-token shape (email + username + scopes array, no scope) -> 403",
                   mcp_post(gw, GRAFANA_PREFIX,
                            idp.mint(email="person@example.invalid",
                                     preferred_username="subjectname",
                                     scopes=["editor", "viewer"], scope=ABSENT))[0], 403)
        checks.add("T", "identity claims + correct scope -> 403 (presence beats scope)",
                   mcp_post(gw, GRAFANA_PREFIX,
                            idp.mint(email="person@example.invalid",
                                     preferred_username="subjectname", scopes=["editor"]))[0], 403)

        grafana.clear()
        for method in ("initialize", "tools/list", "tools/call"):
            mcp_post(gw, GRAFANA_PREFIX, good_graf, method=method)
        last = grafana.last()
        checks.add("U", "grafana backend sees EXACT validated bearer (token hash match)",
                   last.get("bearer_sha12"), exp,
                   f"methods={[r['method'] for r in grafana.records]} "
                   f"prefix_ok={last.get('bearer_prefix_ok')}")

        grafana.clear()
        dup_status, dup_headers = send_raw(
            gw, duplicate_auth_request(GRAFANA_PREFIX, good_graf, "Bearer SPOOFED-TOKEN"))
        dup = grafana.last()
        checks.add("Y0a", "duplicate Authorization: spoofed value NEVER reaches backend",
                   dup.get("bearer_sha12"), exp,
                   f"status={dup_status} sent_authz={dup_headers.count('authorization')}")
        grafana.clear()
        st2, _ = send_raw(
            gw, duplicate_auth_request(GRAFANA_PREFIX, good_graf, "Bearer SPOOFED-TOKEN",
                                       order="spoof_first"))
        checks.add("Y0b", "spoofed-first Authorization order -> 401, nothing reaches backend",
                   (st2, len(grafana.records)), (401, 0))

        grafana.clear()
        mcp_post(gw, GRAFANA_PREFIX, good_graf, extra_headers={
            "Cookie": "sid=victim", "X-Grafana-URL": "http://attacker.invalid",
            "X-Grafana-Service-Account-Token": "SA-TOKEN-LEAK", "X-Session-Id": "victim-sess"})
        g2 = grafana.last()
        checks.add("V", "malicious inbound Cookie stripped", g2.get("cookie_present"), False)
        checks.add("W", "malicious X-Grafana-URL stripped", g2.get("x_grafana_url_present"), False)
        checks.add("X", "malicious SA-token header stripped",
                   g2.get("x_grafana_sa_token_present"), False)
        checks.add("Y", "grafana backend bearer still the caller's own after spoof attempt",
                   g2.get("bearer_sha12"), exp, f"forwarded={g2.get('header_names')}")

        context7.clear()
        mcp_post(gw, CONTEXT7_PREFIX, good_ctx, extra_headers={"Cookie": "sid=victim"})
        c = context7.last()
        checks.add("Z", "context7 backend sees NO Authorization (no backendAuth)",
                   c.get("auth_present"), False, f"forwarded={c.get('header_names')}")
        checks.add("AA", "context7 backend sees NO Cookie", c.get("cookie_present"), False)

        st, hdr, body = mcp_post(gw, GRAFANA_PREFIX, good_graf, method="initialize")
        checks.add("AB", "stateless: no Mcp-Session-Id issued",
                   hdr.get("Mcp-Session-Id") or hdr.get("mcp-session-id"), None,
                   f"resp={body[:80]!r}")
        checks.add("AC", "foreign session id + no JWT -> 401 (no resume without authn)",
                   mcp_post(gw, GRAFANA_PREFIX, None,
                            extra_headers={"Mcp-Session-Id": "stolen-session"})[0], 401)
        checks.add("AD", "foreign session id + wrong scope -> 403",
                   mcp_post(gw, CONTEXT7_PREFIX, good_graf,
                            extra_headers={"Mcp-Session-Id": "stolen-session"})[0], 403)
        grafana.clear()
        mcp_post(gw, GRAFANA_PREFIX, idp.mint(sub="acct-cccc"),
                 extra_headers={"Mcp-Session-Id": "victim-session"})
        g3 = grafana.last()
        checks.add("AE", "different sub with forged session id gets own credential "
                         "(no identity carry-over)",
                   g3.get("bearer_sha12") != exp, True,
                   f"caller={g3.get('bearer_sha12')} victim={exp}")

        advertised_path = urllib.parse.urlparse(advertised).path if advertised else ""
        st, _, body = http_get(gw, advertised_path or "/nope")
        checks.add("AF", "LIMITATION(route shape): advertised RFC9728 path is forwarded to the "
                         "backend, not answered by the gateway", st, 405,
                   f"advertised={advertised} body={body[:60]!r}")
        st, _, _ = http_get(gw, "/grafana/mcp/.well-known/oauth-protected-resource")
        checks.add("AG", "LIMITATION(route shape): under-prefix metadata path IS identity-gated "
                         "(401), so it cannot leak the resource", st, 401)

        context7.clear()
        mcp_post(gw, NOSTRIP_PREFIX, good_ctx, extra_headers={"Cookie": "sid=victim"})
        leak = context7.last()
        checks.add("AA1", "WITHOUT requestHeaderModifier.remove, Cookie TRAVERSES the edge "
                          "(real risk: the strip list is mandatory)",
                   leak.get("cookie_present"), True, f"forwarded={leak.get('header_names')}")
        return checks
    finally:
        if gateway:
            if os.environ.get("KEEP"):
                print(f"kept: {gateway.work}")
            else:
                gateway.cleanup()
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
