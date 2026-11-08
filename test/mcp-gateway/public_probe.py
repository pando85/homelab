#!/usr/bin/env python3
"""Public-boundary check for the MCP gateway: does the internet see an auth gate?

Run it from outside the cluster (a GitHub-hosted runner is the intended place).
No credentials, nothing probed beyond these fixed expectations, which must match
apps/mcp-gateway:

    https://mcp.grigri.cloud/firecrawl/mcp  -> 401 + protected-resource metadata
    authorization server                    -> exactly one, the Kanidm client issuer
    admin plane mcp.internal.grigri.cloud   -> silent to the internet

Real tokens and real tools are verified in-cluster, since credentials must not
leave the cluster.    python3 test/mcp-gateway/public_probe.py [--self-test]
"""

import ipaddress
import json
import re
import socket
import sys
import urllib.error
import urllib.parse
import urllib.request

HOST = "mcp.grigri.cloud"
ROUTE = "/firecrawl/mcp"
ISSUER = "https://idm.grigri.cloud/oauth2/openid/mcp-gateway"
SCOPE = "mcp_firecrawl"
ADMIN_HOST = "mcp.internal.grigri.cloud"
PRM_PREFIX = "/.well-known/oauth-protected-resource"
RESOURCE = "https://%s%s" % (HOST, ROUTE)
HEADERS = {"Accept": "application/json, text/event-stream", "Content-Type": "application/json"}
BODY = b'{"jsonrpc":"2.0","id":1,"method":"initialize","params":{}}'


def call(method, url, headers=None, data=None):
    """(status, headers, body). Never follows redirects; failures give status None."""
    if not url.startswith("https://"):
        raise AssertionError("refusing a non-HTTPS target: " + url)

    class NoRedirect(urllib.request.HTTPRedirectHandler):
        def redirect_request(self, *args, **kwargs):
            return None

    opener = urllib.request.build_opener(NoRedirect)
    request = urllib.request.Request(url, data=data, method=method, headers=dict(headers or {}))
    try:
        with opener.open(request, timeout=20) as response:
            return response.status, response.headers, response.read()
    except urllib.error.HTTPError as exc:
        return exc.code, exc.headers, exc.read()
    except (urllib.error.URLError, OSError):
        return None, {}, b""


def metadata_url(challenge):
    """resource_metadata="..." out of a Bearer challenge, else None."""
    if not challenge or not challenge.lower().startswith("bearer"):
        return None
    match = re.search(r'resource_metadata\s*=\s*"([^"]+)"', challenge, re.IGNORECASE)
    return match.group(1) if match else None


def check_metadata_url(url):
    """The advertised document must live on this host and describe this route."""
    parsed = urllib.parse.urlparse(url)
    if parsed.scheme != "https" or parsed.hostname != HOST:
        return "not on https://%s: %s" % (HOST, url)
    if not parsed.path.startswith(PRM_PREFIX) or not parsed.path.endswith(ROUTE):
        return "path %r is not %s%s" % (parsed.path, PRM_PREFIX, ROUTE)
    return None


def check_prm(document):
    """Protected-resource metadata must name the canonical resource and issuer."""
    if not isinstance(document, dict):
        return ["metadata body is not a JSON object"]
    problems = []
    if document.get("resource") != RESOURCE:
        problems.append("resource %r, expected %r" % (document.get("resource"), RESOURCE))
    if document.get("authorization_servers") != [ISSUER]:
        problems.append("authorization_servers %r, expected exactly [%r]"
                        % (document.get("authorization_servers"), ISSUER))
    if SCOPE not in (document.get("scopes_supported") or []):
        problems.append("scopes_supported lacks %r" % SCOPE)
    return problems


def check_route_status(status):
    if status == 401:
        return None
    if status == 404:
        return "404: no MCP route published on this host yet"
    if status is None:
        return "no answer: the edge is unreachable from here"
    if 300 <= status < 400:
        return "%s: redirects instead of refusing" % status
    return "%s: expected 401" % status


def check_admin_status(status):
    """Only silence counts: 404 (ingress default backend) or no answer at all.

    A 401/403 means the gateway itself answers on the internet - exposure even
    though it refuses the request.
    """
    if status is None or status == 404:
        return None
    return "%s: the admin plane answers from the internet" % status


def is_public(address):
    try:
        addr = ipaddress.ip_address(address)
    except ValueError:
        return True
    if addr.version == 4 and addr in ipaddress.ip_network("100.64.0.0/10"):
        return False  # CGNAT / tailnet: not routable on the public internet
    return not (addr.is_private or addr.is_loopback or addr.is_link_local
                or addr.is_multicast or addr.is_reserved or addr.is_unspecified)


def addresses(host):
    try:
        infos = socket.getaddrinfo(host, 443, proto=socket.IPPROTO_TCP)
    except socket.gaierror:
        return None
    return sorted({info[4][0] for info in infos})


def probe_resource():
    """The 401 gate, the discovery URL, the metadata content, a rejected bad token."""
    found = addresses(HOST)
    if not found:
        return ["%s does not resolve from here" % HOST]
    if not [a for a in found if is_public(a)]:
        return ["%s resolves only to %s: not an external test" % (HOST, ", ".join(found))]

    status, headers, _ = call("POST", RESOURCE, HEADERS, BODY)
    problem = check_route_status(status)
    if problem:
        return [problem]
    discovered = metadata_url(headers.get("WWW-Authenticate"))
    if not discovered:
        return ["401 without a resource_metadata challenge (RFC 9728 discovery absent)"]
    problem = check_metadata_url(discovered)
    if problem:
        return ["metadata URL: " + problem]

    meta_status, _, meta_body = call("GET", discovered)
    if meta_status != 200:
        return ["metadata fetch returned %s" % meta_status]
    try:
        problems = check_prm(json.loads(meta_body.decode()))
    except (UnicodeDecodeError, json.JSONDecodeError):
        return ["metadata body is not JSON"]

    bad_status, _, _ = call("POST", RESOURCE, {"Authorization": "Bearer invalid.0.token",
                                               **HEADERS}, BODY)
    if bad_status != 401:
        problems.append("invalid bearer token got %s, expected 401" % bad_status)
    if not problems:
        print("  401 gate; metadata %s; issuer %s; scope %s" % (discovered, ISSUER, SCOPE))
    return problems


def probe_admin(failures):
    found = addresses(ADMIN_HOST)
    public = [a for a in found or [] if is_public(a)]
    if not found:
        print("  %s does not resolve publicly: not reachable" % ADMIN_HOST)
    elif not public:
        print("  %s -> %s: private-only, acceptable" % (ADMIN_HOST, ", ".join(found)))
    else:
        # Only the routable addresses: a public runner has no business dialling a
        # LAN address, and anything served there is not internet exposure.
        for address in public:
            for path in (ROUTE, "/"):
                status, _, _ = call("GET", "https://%s%s" % (address, path),
                                    {"Host": ADMIN_HOST})
                print("  %s%s -> %s" % (address, path, status or "no answer"))
                problem = check_admin_status(status)
                if problem:
                    failures.append(problem)
    return failures


def live():
    failures = probe_admin(probe_resource())
    for failure in failures:
        print("FAIL %s" % failure)
    if failures:
        return 1
    print("PASS: the MCP route is gated and the admin plane is silent")
    return 0


def self_test():
    """Decision logic only: no network, no servers."""
    prm_url = "https://%s%s%s" % (HOST, PRM_PREFIX, ROUTE)
    good = {"resource": RESOURCE, "authorization_servers": [ISSUER], "scopes_supported": [SCOPE]}
    cases = [
        ("challenge parsed", metadata_url('Bearer resource_metadata="%s"' % prm_url)),
        ("no header", metadata_url(None) is None),
        ("not bearer", metadata_url('Basic realm="x"') is None),
        ("no metadata in bearer", metadata_url('Bearer error="invalid_token"') is None),
        ("metadata url accepted", check_metadata_url(prm_url) is None),
        ("other host rejected", check_metadata_url(prm_url.replace(HOST, "evil.example"))),
        ("http rejected", check_metadata_url(prm_url.replace("https", "http"))),
        ("wrong prefix rejected", check_metadata_url("https://%s/x%s" % (HOST, ROUTE))),
        ("other route rejected",
         check_metadata_url("https://%s%s/grafana/mcp" % (HOST, PRM_PREFIX))),
        ("401 accepted", check_route_status(401) is None),
        ("200 rejected", check_route_status(200) is not None),
        ("redirect rejected", check_route_status(302) is not None),
        ("404 says not published", "no MCP route published" in check_route_status(404)),
        ("unreachable reported", "unreachable" in check_route_status(None)),
        ("admin 404 is silent", check_admin_status(404) is None),
        ("admin timeout is silent", check_admin_status(None) is None),
        ("admin 401 is exposure", "answers from the internet" in check_admin_status(401)),
        ("admin 403 is exposure", "answers from the internet" in check_admin_status(403)),
        ("admin 200 is exposure", "answers from the internet" in check_admin_status(200)),
        ("cgnat not public", not is_public("100.64.5.6")),
        ("node/pod ips not public", not is_public("192.168.192.11")
         and not is_public("10.0.4.110")),
        ("public ip public", is_public("1.1.1.1")),
        ("valid metadata passes", check_prm(good) == []),
        ("wrong resource caught", bool(check_prm({**good, "resource": "https://x/y"}))),
        ("wrong issuer path caught",
         bool(check_prm({**good, "authorization_servers": ["https://idm.grigri.cloud/o/c"]}))),
        ("other issuer host caught",
         bool(check_prm({**good, "authorization_servers": ["https://elsewhere/x"]}))),
        ("extra issuer caught",
         bool(check_prm({**good, "authorization_servers": [ISSUER, ISSUER]}))),
        ("missing issuer caught", bool(check_prm({**good, "authorization_servers": []}))),
        ("missing scope caught", bool(check_prm({**good, "scopes_supported": ["openid"]}))),
        ("non-object metadata caught", bool(check_prm([]))),
    ]
    failed = [name for name, outcome in cases if not outcome]
    for name, outcome in cases:
        print("  %s %s" % ("ok  " if outcome else "FAIL", name))
    print("%s self-test: %d/%d" % ("FAIL" if failed else "PASS", len(cases) - len(failed),
                                   len(cases)))
    return 1 if failed else 0


if __name__ == "__main__":
    sys.exit(self_test() if "--self-test" in sys.argv[1:] else live())
