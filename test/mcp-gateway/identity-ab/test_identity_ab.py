"""Two-identity (A/B) acceptance suite for the homelab MCP gateway.

Cross-component, read-only, and deliberately small: native HTTP clients
(``httpx``) against documented APIs, ``PyJWT`` for claim inspection, no custom
broker or auth framework. It *observes* the gateway, Grafana and Vault; it never
configures them (the gateway and Vault work belongs to other charts/agents).

Why two contexts: every interesting failure mode here is a cross-over - one
caller's credential being accepted as another's, a session id surviving a
credential swap, a backend identity collapsing onto a shared service account.
A single-context test cannot see any of that, so without two distinct
identities the live tier SKIPS with the exact missing prerequisite instead of
reporting a green run that proved nothing.

Two tiers:

* OFFLINE (always runs, no cluster, no secrets): the harness's own invariants -
  credentials only from Secret-volume files, the target -> plane -> issuer/audience
  resolution (each gateway plane is its own Kanidm OAuth2 client, so nothing may
  inherit one ambient pair; a target with no declared plane is an error), the
  audience/issuer predicates in both directions, chart-pair and chart-placement
  checks read from the gateway charts, per-target route shape, and sanitised
  comparisons that cannot echo a token.
* LIVE (needs the prerequisites in ``conftest.py``): permission outcomes
  observed THROUGH the gateway for two *human* callers, MCP session isolation
  when one caller presents the other's session id, rejection of untrusted caller
  tokens (foreign signature, expired, wrong issuer, wrong audience - reported as
  "unverified credential refused at the edge", because this harness cannot sign
  with the Kanidm client key and so cannot isolate which claim check fired),
  precedence of a
  caller's own native backend credential over any unrequested fallback channel,
  and alternate-credential-channel injection.
  Direct backend readouts (Grafana ``/api/user``, Vault ``lookup-self``) are
  named ``*_baseline`` for a reason: they are *prerequisites*, never parity
  evidence. They show two accounts differ at the backend; they say nothing about
  which identity the gateway acts as, and no parity claim is made from them.

Two credential classes, kept apart deliberately:

* EDGE bearer: an OAuth2 ACCESS token, on every route, validated by the gateway.
  This suite never presents an ID token as a bearer, and ``credential: idToken`` on
  a route does not mean that - it names what the *backend* is meant to receive.
* Native per-user backend credential: a caller-supplied ``X-Vault-Token`` is the
  caller's own Vault credential and must be honoured, not stripped categorically
  (that is the legitimate zero-broker path: OIDC login -> that user's own token).

What must fail instead is a *second*, unrequested channel - ``?token=``,
``VAULT_TOKEN=``, an alternate header, a rewritten backend address or a per-request
TLS override - because those let a caller borrow someone else's identity.
"""

from __future__ import annotations

import hashlib
import json
import os
import re
import stat
import time
from pathlib import Path

import httpx
import jwt
import pytest
import yaml

from contexts import REDACTION_MARKERS, load_context

REPO = Path(__file__).resolve().parents[3]
# Read-only comparison against the gateway charts' declared contract. This suite
# never edits another chart; it only checks that its own assumptions still match.
PUBLIC = "public"
ADMIN = "admin"
CHART_VALUES = {PUBLIC: REPO / "apps" / "mcp-gateway" / "values.yaml",
                ADMIN: REPO / "apps" / "mcp-gateway-admin" / "values.yaml"}

# ---------------------------------------------------------------------------
# Small pure helpers (the only logic this suite owns).
# ---------------------------------------------------------------------------

# The credential-bearing parameter names worth probing, and the field names that
# must never appear in reported output. Both extend the single list in
# contexts.py rather than restating it, so the two halves cannot drift apart.
TOKEN_QUERY_KEYS = ("token", "access_token", "vault_token")
SECRET_KEYS = REDACTION_MARKERS + TOKEN_QUERY_KEYS + ("subject_token",)

# Kanidm issues one issuer/audience pair per OAuth2 CLIENT, and each gateway plane
# is its own client: the public plane (``apps/mcp-gateway``) validates
# ``.../oauth2/openid/mcp-gateway`` with ``aud: mcp-gateway``, the admin plane
# (``apps/mcp-gateway-admin``) validates ``.../openid/mcp-gateway-admin`` with
# ``aud: mcp-gateway-admin``. There is no bare ``https://idm.grigri.cloud`` issuer
# for these tokens, so "exact equality" is only right against the per-client URI.
#
# Review correction: the suite used to carry ONE pair and apply it everywhere,
# which made an admin-plane route look verified while it was being measured against
# the public client's expectations. So nothing resolves to "public" implicitly -
# a target maps to a plane, a plane maps to its chart, and the chart is the
# authority for the pair.
TARGET_PLANE = {"firecrawl": PUBLIC, "grafana": PUBLIC, "vault": ADMIN}
# Desired architecture as approved (docs/design/mcp-gateway-architecture.md):
# Grafana and Firecrawl public, Vault admin. Grafana's *chart placement* is still
# pending the gateway's correction; test_backends_are_declared_on_their_approved_
# plane below reports that as a failure instead of quietly adapting to it.
DECLARED_PAIR = {
    PUBLIC: ("https://idm.grigri.cloud/oauth2/openid/mcp-gateway", "mcp-gateway"),
    ADMIN: ("https://idm.grigri.cloud/oauth2/openid/mcp-gateway-admin",
           "mcp-gateway-admin"),
}
# Explicit per-plane spellings. Offline shape fixtures name the plane they are
# modelling (PUBLIC_ISSUER below) instead of inheriting an ambient default.
PUBLIC_ISSUER, PUBLIC_AUDIENCE = DECLARED_PAIR[PUBLIC]
ADMIN_ISSUER, ADMIN_AUDIENCE = DECLARED_PAIR[ADMIN]


def plane_of(target: str) -> str:
    """The plane a target belongs to. An unknown target is an error, not "public"."""
    name = (target or "").strip().lower()
    if name not in TARGET_PLANE:
        raise KeyError(
            f"target {name!r} has no declared plane: add it to TARGET_PLANE with the "
            "plane the approved architecture puts it on, or correct "
            "MCP_IDENTITY_TARGET - resolving it to the public plane implicitly is what "
            "this helper exists to prevent")
    return TARGET_PLANE[name]


def chart_pair(plane: str) -> tuple[str, str] | None:
    """The (issuer, audience) the plane's own chart declares; None if unreadable."""
    path = CHART_VALUES[plane]
    if yaml is None or not path.is_file():
        return None
    auth = (yaml.safe_load(path.read_text(encoding="utf-8")) or {}).get("auth") or {}
    issuer, audience = auth.get("issuer"), auth.get("audience")
    if not issuer or not audience:
        return None
    return str(issuer).strip(), str(audience).strip()


def expected_edge_pair(target: str) -> tuple[str, str]:
    """The (issuer, audience) a live run for this target must present and expect.

    ``MCP_IDENTITY_ISSUER`` / ``MCP_IDENTITY_AUDIENCE`` are an operator override for
    a non-standard deployment, and they are honoured only as a complete pair and only
    after being checked against the plane's chart: a half-set override (a public
    issuer with an admin audience, or the other way round) would test a combination
    Kanidm can never issue, and a stale override would silently retarget the whole
    run. With no override, the pair comes from the plane's own chart, falling back to
    DECLARED_PAIR only where that chart is not readable offline.
    """
    plane = plane_of(target)
    declared = chart_pair(plane) or DECLARED_PAIR[plane]
    env_issuer = (os.environ.get("MCP_IDENTITY_ISSUER") or "").strip()
    env_audience = (os.environ.get("MCP_IDENTITY_AUDIENCE") or "").strip()
    if bool(env_issuer) != bool(env_audience):
        pytest.fail("MCP_IDENTITY_ISSUER and MCP_IDENTITY_AUDIENCE must be set together: "
                    "one of them alone pairs a real value with the other plane's, so no "
                    "result from this run would be attributable")
    if env_issuer:
        if (env_issuer, env_audience) != declared:
            pytest.fail(
                f"the explicit override is not what the {plane!r} plane chart declares "
                f"({declared[0]!r} / {declared[1]!r}): either drop the overrides, or "
                f"point MCP_IDENTITY_TARGET at the plane that actually issues "
                "this pair - running a live gate against a mismatched pair reports a "
                "verdict the gateway never had a chance to give")
        return env_issuer, env_audience
    return declared


def chart_backends(plane: str) -> dict:
    """The ``backends`` map of a plane's chart, read-only, for placement checks."""
    path = CHART_VALUES[plane]
    if yaml is None or not path.is_file():
        pytest.skip(f"{path} is not readable, so backend placement cannot be checked")
    return (yaml.safe_load(path.read_text(encoding="utf-8")) or {}).get("backends") or {}


def route_for(base: str, target: str) -> str:
    """Per-target gateway route. The gateway has no aggregate ``/mcp``.

    An empty target is refused rather than producing ``/mcp``, because a helper
    that silently built an aggregate route would look like a working config until
    every call 404ed.
    """
    target = (target or "").strip().strip("/")
    if not target:
        raise ValueError("a per-target route needs a target (no aggregate /mcp exists)")
    return f"{base.rstrip('/')}/{target}/mcp"


def identity_label(token: str) -> str:
    """A non-secret label for a caller token: issuer fingerprint + subject only.

    Used for comparisons and assertion messages so a failure is explainable
    without ever printing a credential.
    """
    claims = jwt.decode(token, options={"verify_signature": False})
    subject = str(claims.get("sub") or claims.get("cn") or "no-sub")
    issuer = hashlib.sha1(str(claims.get("iss", "")).encode()).hexdigest()[:8]
    return f"{issuer}/{subject[:24]}"


def decoded_claims_must_match(token: str, *, audience: str, issuer: str) -> dict:
    """Claim-shape predicates this harness assumes - NOT authentication.

    Deliberately decodes without verifying the signature, which means the result
    proves nothing about authenticity: anyone can mint these claims. Only the
    gateway - it holds Kanidm's per-client JWKS - can accept or reject a caller
    token, and ``test_gateway_rejects_untrusted_caller_tokens`` is the check that
    exercises that. Nothing here may be read as "the token is valid".

    Two shapes are asserted, both exactly:

    * ``aud`` is Kanidm's OAuth2 *client name* (``mcp-gateway``), independent of
      the gateway's canonical TLS host;
    * ``iss`` is the FULL per-client issuer URI, compared exactly. Both the IdM
      root (``https://idm.grigri.cloud``, which signs nothing for this client) and
      a look-alike host or path (``https://idm.grigri.cloud.attacker.example``,
      ``.../oauth2/openid/mcp-gateway/evil``) are different issuers, and a check
      that only compared hosts would accept the second one.
    """
    claims = jwt.decode(token, options={"verify_signature": False})
    aud = claims.get("aud")
    aud_set = {aud} if isinstance(aud, str) else set(aud or [])
    assert audience in aud_set, f"audience is {sorted(aud_set)}, expected {audience!r}"
    assert claims.get("iss") == issuer, (
        f"issuer is {claims.get('iss')!r}, expected the exact {issuer!r}")
    assert "exp" in claims, "caller token must be time-bounded"
    return claims


def scope_set(claims: dict) -> set[str]:
    """The granted scopes of a Kanidm token, in either of its two shapes.

    Kanidm puts an ACCESS token's scopes in one space-separated ``scope`` string
    and an ID token's in a ``scopes`` ARRAY (apps/mcp-gateway/values.yaml). Reading
    both shapes is what lets ``edge_bearer_must_be_an_access_token`` tell them
    apart instead of guessing, and keeps a scope check from silently seeing
    nothing when the token is the other class.
    """
    scope = claims.get("scope")
    if isinstance(scope, str):
        return {s for s in scope.split() if s}
    scopes = claims.get("scopes")
    if isinstance(scopes, list):
        return {str(s) for s in scopes if s}
    return set()


def edge_bearer_must_be_an_access_token(claims: dict, *, who: str,
                                        strict: bool = False) -> None:
    """Architecture rule, enforced where the evidence exists: the edge gets an ACCESS token.

    The gateway's own bearer validation must run against an OAuth2 *access* token.
    An ID token is never an acceptable edge credential, and a route configured with
    ``credential: idToken`` does not change that - that field describes the
    credential the *backend* is meant to receive (a separate, deliberately minted,
    per-user token for the backend's own audience), not what the caller presents at
    the gateway. Keeping the two semantics apart is what stops "backend wants an
    ID token" from quietly becoming "the edge accepts ID tokens".

    Where a token class is observable from claims, this asserts it instead of
    assuming it. Kanidm's own shapes differ (see apps/mcp-gateway/values.yaml):
    the access token carries ``scope`` as ONE space-separated string, the ID token
    carries ``scopes`` as an ARRAY. So an array-shaped caller token is refused here
    rather than sent, and a caller token whose class cannot be shown is never
    silently trusted.

    ``strict`` is the live-tier form. Offline (fixtures, predicates) a missing
    ``scope`` claim only means "this harness cannot prove the class", so it skips. In
    a live run a caller token that cannot be shown to be an access token is a defect
    in the credential supplied to the gate: skipping there would leave the parity
    tests still able to report a green verdict for a credential whose class nobody
    established, so it fails with an explicit status instead.
    """
    if isinstance(claims.get("scopes"), list):
        pytest.fail(
            f"caller context {who} carries an ID-token shape (`scopes` array): this suite "
            "sends OAuth2 ACCESS tokens to the gateway edge and will not present an ID "
            "token as a bearer, even as a fallback")
    if not isinstance(claims.get("scope"), str) or not scope_set(claims):
        message = (f"caller context {who} carries no access-token `scope` string, so its "
                   "token class cannot be established from claims; this suite will not "
                   "assume it is an access token (the gateway, not this suite, is the "
                   "verifier)")
        if strict:
            pytest.fail(message + " - and a live gate must not run on an unproven "
                                  "credential class")
        pytest.skip(message)


def redact(text: str) -> str:
    """Strip anything credential-shaped from a string before it is reported.

    Four shapes, each matched loosely on purpose (a report is free-form text):
    bearer values, JWT-ish values, Vault-style ``s.``/``hvs.`` tokens, and any
    ``<secret-key>=<value>`` / ``"<secret-key>": "<value>"`` pair.
    """
    out = re.sub(r"(?i)bearer\s+[A-Za-z0-9._~+/=-]{8,}", "Bearer <redacted>", text)
    out = re.sub(r"eyJ[A-Za-z0-9_-]{8,}\.[A-Za-z0-9_-]{8,}\.[A-Za-z0-9_-]{4,}", "<jwt>", out)
    out = re.sub(r"\bhvs\.[A-Za-z0-9_\-]{8,}|\bs\.[A-Za-z0-9_\-]{12,}", "<vault-token>", out)
    keys = "|".join(SECRET_KEYS)
    # The value class excludes '<' so an already-redacted placeholder
    # (<jwt>, <vault-token>) is never swallowed into a generic <redacted>,
    # which would hide which shape actually matched.
    out = re.sub(rf"(?i)(?P<key>\"?(?:{keys})\"?)(?P<sep>\s*[:=]\s*)(?P<q>['\"]?)[^\s'\"&,}}<]{{6,}}",
                 lambda m: f"{m.group('key')}{m.group('sep')}{m.group('q')}<redacted>", out)
    return out


def mcp_rpc(url: str, bearer: str, method: str, *, params: dict | None = None,
            session_id: str | None = None, extra_headers: dict | None = None,
            query: dict | None = None,
            transport: httpx.BaseTransport | None = None) -> httpx.Response:
    """One JSON-RPC call over streamable HTTP, with the caller's bearer.

    A single sender for every request this suite makes against the gateway, so
    a header/query variant under test cannot accidentally be applied to only
    one of the two requests it compares. ``transport`` exists so that property is
    testable offline (``httpx.MockTransport``) instead of being merely claimed.
    """
    headers = {
        "Authorization": f"Bearer {bearer}",
        "Accept": "application/json, text/event-stream",
        "Content-Type": "application/json",
        "MCP-Protocol-Version": "2025-06-18",
    }
    if session_id:
        headers["Mcp-Session-Id"] = session_id
    headers.update(extra_headers or {})
    with httpx.Client(timeout=30.0, transport=transport) as client:
        return client.post(url, params=query, headers=headers,
                           json={"jsonrpc": "2.0", "id": 1, "method": method,
                                 "params": params if params is not None else {}})


def mcp_initialize(url: str, bearer: str, *, extra_headers: dict | None = None,
                   query: dict | None = None,
                   transport: httpx.BaseTransport | None = None) -> tuple[int, str | None]:
    """Open an MCP streamable-HTTP session; return (status, Mcp-Session-Id).

    ``query`` is the URL query string, not JSON-RPC params: the only reason a
    caller of this harness sets it is to smuggle a credential in a parameter
    (``?token=`` and friends) and see whether anything reads it.
    """
    resp = mcp_rpc(url, bearer, "initialize",
                   params={"protocolVersion": "2025-06-18", "capabilities": {},
                           "clientInfo": {"name": "identity-ab", "version": "0"}},
                   extra_headers=extra_headers, query=query, transport=transport)
    return resp.status_code, resp.headers.get("mcp-session-id")


# ---------------------------------------------------------------------------
# OFFLINE tier: harness invariants, no cluster, no real credentials.
# ---------------------------------------------------------------------------

def _write_secret(tmp_path: Path, text: str, mode: int, name: str = "token") -> Path:
    path = tmp_path / name
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(text, encoding="utf-8")
    path.chmod(mode)
    return path


def _fake_jwt(sub: str, aud: str = PUBLIC_AUDIENCE,
              iss: str = PUBLIC_ISSUER, **extra: object) -> str:
    """A structurally real, signature-irrelevant token for OFFLINE shape tests.

    The defaults name the plane being modelled (public) instead of an ambient
    "default": an admin-plane shape must pass ``aud=ADMIN_AUDIENCE,
    iss=ADMIN_ISSUER`` explicitly, the same way a live run resolves the pair per
    target. Nothing in this helper authenticates anything.
    """
    # A >=32 byte HMAC key: PyJWT warns (and future versions refuse) short keys,
    # and a warning per test buries real output. These tokens are structurally
    # real but signature-irrelevant: the gateway verifies against Kanidm's JWKS,
    # this suite only reads claims.
    return jwt.encode({"sub": sub, "aud": [aud], "iss": iss, "exp": 4_000_000_000, **extra},
                      key="test-only-signing-key-that-is-long-enough-32", algorithm="HS256")


def test_credentials_come_from_files_not_env(monkeypatch, tmp_path) -> None:
    """A credential is referenced by path; an env *value* is refused outright."""
    secret = _fake_jwt("hermes-mcp")
    good = _write_secret(tmp_path, secret, 0o600)

    monkeypatch.setenv("MCP_IDENTITY_CALLER_A_FILE", str(good))
    ctx = load_context("MCP_IDENTITY_CALLER_A_FILE", "A")
    assert ctx.token() == secret

    # The same credential appearing as an env value is a policy violation, even
    # when a valid file path is also present.
    monkeypatch.setenv("MCP_IDENTITY_CALLER_A_TOKEN", secret)
    with pytest.raises(pytest.fail.Exception, match="never from the environment"):
        load_context("MCP_IDENTITY_CALLER_A_FILE", "A")

    monkeypatch.delenv("MCP_IDENTITY_CALLER_A_FILE")
    monkeypatch.delenv("MCP_IDENTITY_CALLER_A_TOKEN")
    with pytest.raises(pytest.skip.Exception, match="two distinct identities"):
        load_context("MCP_IDENTITY_CALLER_A_FILE", "A")

    monkeypatch.setenv("MCP_IDENTITY_CALLER_A_FILE", str(tmp_path / "not-mounted"))
    with pytest.raises(pytest.skip.Exception, match="Secret volume not mounted"):
        load_context("MCP_IDENTITY_CALLER_A_FILE", "A")


def test_leaked_context_is_refused(monkeypatch, tmp_path) -> None:
    """A group-readable credential invalidates isolation claims: hard failure."""
    world_readable = _write_secret(tmp_path, _fake_jwt("hermes-mcp"), 0o644, name="world")
    monkeypatch.setenv("MCP_IDENTITY_CALLER_A_FILE", str(world_readable))
    with pytest.raises(pytest.fail.Exception, match="group/other-readable"):
        load_context("MCP_IDENTITY_CALLER_A_FILE", "A").token()


def test_audience_is_the_client_name_not_the_url_host() -> None:
    """The exchanged token's ``aud`` is Kanidm's client name.

    Main's correction: the gateway's canonical discovery URL and the audience are
    independent, so a check that compared ``aud`` to the host would reject a valid
    token (and a check that compared nothing would accept a foreign one).
    """
    token = _fake_jwt("hermes-mcp")
    claims = decoded_claims_must_match(token, audience=PUBLIC_AUDIENCE,
                                       issuer=PUBLIC_ISSUER)
    assert claims["sub"] == "hermes-mcp"
    with pytest.raises(AssertionError, match="audience"):
        decoded_claims_must_match(_fake_jwt("hermes-mcp", aud="grafana"),
                                  audience=PUBLIC_AUDIENCE, issuer=PUBLIC_ISSUER)


def test_issuer_is_the_exact_uri_not_a_host_match() -> None:
    """``iss`` must equal the configured issuer URI, character for character.

    A host-suffix comparison (the shape this suite used before review) accepts
    ``https://idm.grigri.cloud.attacker.example`` and ``http://idm.grigri.cloud``,
    i.e. exactly the issuers a wrong-issuer test is supposed to catch.

    The first rejected value below is the one this suite itself got wrong: Kanidm
    signs per-client issuers (``.../oauth2/openid/<client>``), so the bare IdM
    root is NOT a valid issuer for a gateway caller token, even though it is the
    same host. Pinning it here is what keeps the default from sliding back.
    """
    exact = PUBLIC_ISSUER
    for wrong in ("https://idm.grigri.cloud",  # the IdM root: real, but not this issuer
                  f"{exact}.attacker.example", "http://idm.grigri.cloud",
                  "https://idm.grigri.cloud/oauth2/openid/mcp-gateway/",
                  "https://idm.grigri.cloud/oauth2/openid/grafana",
                  ADMIN_ISSUER,  # the OTHER gateway plane: a real Kanidm issuer, wrong here
                   f"{exact}/evil"):
        with pytest.raises(AssertionError, match="issuer"):
            decoded_claims_must_match(_fake_jwt("hermes-mcp", iss=wrong),
                                      audience=PUBLIC_AUDIENCE, issuer=exact)
    # And symmetrically: the public pair must not satisfy an admin-plane expectation
    # either. A one-directional check is how the old single hard-coded default looked
    # like coverage while it compared everything to the public client.
    for wrong in (PUBLIC_ISSUER, f"{ADMIN_ISSUER}/evil"):
        with pytest.raises(AssertionError, match="issuer"):
            decoded_claims_must_match(_fake_jwt("someone", aud=ADMIN_AUDIENCE, iss=wrong),
                                      audience=ADMIN_AUDIENCE, issuer=ADMIN_ISSUER)
    # The shape this suite does accept: the plane's own pair, both directions.
    assert decoded_claims_must_match(
        _fake_jwt("someone", aud=ADMIN_AUDIENCE, iss=ADMIN_ISSUER),
        audience=ADMIN_AUDIENCE, issuer=ADMIN_ISSUER)["iss"] == ADMIN_ISSUER


def test_unverified_decode_proves_nothing_about_authenticity() -> None:
    """Pin the limit of the offline helpers: a forged token "passes" them.

    This is why the suite never reports a caller as authenticated from a decoded
    claim set, and why ``test_gateway_rejects_untrusted_caller_tokens`` (live,
    against the gateway's own verification) is the only thing that can prove
    issuer/audience/signature enforcement. The assertion below is deliberately
    a proof-of-weakness: if these helpers ever grew signature checking, this test
    fails and the author has to look at that trade-off.
    """
    forged = jwt.encode({"sub": "someone-else", "aud": [PUBLIC_AUDIENCE],
                         "iss": PUBLIC_ISSUER, "exp": 4_000_000_000},
                        key="a-key-the-gateway-has-never-seen-32bytes", algorithm="HS256")
    claims = decoded_claims_must_match(forged, audience=PUBLIC_AUDIENCE,
                                       issuer=PUBLIC_ISSUER)
    assert claims["sub"] == "someone-else", (
        "these helpers must only ever inspect claim shape, never authenticate")
    with pytest.raises(jwt.exceptions.InvalidSignatureError):
        jwt.decode(forged, key="a-different-key-of-the-same-length-32b",
                   algorithms=["HS256"], options={"require": ["exp"]})


def test_edge_bearer_class_is_access_token_not_id_token() -> None:
    """Contract rule, checked where the claims show it: no ID tokens at the edge.

    ``credential: idToken`` on a route says what the *backend* is meant to receive;
    it is not permission for the gateway edge to accept an ID token as the bearer,
    and it must not quietly become an approved fallback. So the harness refuses an
    ID-token-shaped caller credential, and it distinguishes the classes from the
    shapes Kanidm actually emits (access: one ``scope`` string; ID: ``scopes``
    ARRAY) rather than assuming the file it was handed holds the right class.

    Whether a deliberately supplied, separate per-user ID token for a backend is
    supported at all is the gateway agent's design question, not this suite's, and
    nothing here confirms per-user OIDC passthrough.
    """
    access = jwt.decode(_fake_jwt("human-a", scope="openid email profile mcp_firecrawl"),
                        options={"verify_signature": False})
    edge_bearer_must_be_an_access_token(access, who="A")
    assert "mcp_firecrawl" in scope_set(access)

    id_token = jwt.decode(_fake_jwt("human-a", scopes=["mcp_firecrawl"], nonce="n",
                                    auth_time=1), options={"verify_signature": False})
    with pytest.raises(pytest.fail.Exception, match="ID-token"):
        edge_bearer_must_be_an_access_token(id_token, who="A")
    # The other half of the rule: scope checks must see both shapes, so a scope
    # assertion can never pass by silently reading nothing.
    assert scope_set(id_token) == {"mcp_firecrawl"}
    assert scope_set({"sub": "no-scope"}) == set()

    # The live tier's stricter form must FAIL where the offline form skips: an
    # unproven credential class may never end up as a passed parity run, and a skip
    # that reads as "not applicable" is exactly how that would hide.
    no_class = jwt.decode(_fake_jwt("human-a"), options={"verify_signature": False})
    with pytest.raises(pytest.fail.Exception, match="cannot be established"):
        edge_bearer_must_be_an_access_token(no_class, who="A", strict=True)
    with pytest.raises(pytest.skip.Exception):
        edge_bearer_must_be_an_access_token(no_class, who="A")


def test_pair_overrides_are_per_target_consistent_and_chart_validated(monkeypatch) -> None:
    """An operator override is honoured only as a complete, matching pair.

    Three ways this used to go wrong, all covered here offline: one variable set
    alone (public issuer + admin audience, a combination Kanidm cannot issue), a
    stale override applied to a target on the other plane (the run would measure a
    pair the gateway never validates), and no override at all falling back to one
    ambient public pair regardless of target.
    """
    for name in ("MCP_IDENTITY_ISSUER", "MCP_IDENTITY_AUDIENCE"):
        monkeypatch.delenv(name, raising=False)

    # No override: each target gets its own plane's pair, from that plane's chart.
    assert expected_edge_pair("firecrawl") == (PUBLIC_ISSUER, PUBLIC_AUDIENCE)
    assert expected_edge_pair("vault") == (ADMIN_ISSUER, ADMIN_AUDIENCE)
    with pytest.raises(KeyError, match="no declared plane"):
        expected_edge_pair("something-new")

    # Half an override is refused, with the reason.
    monkeypatch.setenv("MCP_IDENTITY_ISSUER", ADMIN_ISSUER)
    with pytest.raises(pytest.fail.Exception, match="set together"):
        expected_edge_pair("vault")

    # A complete override must match the chart of the plane being tested.
    monkeypatch.setenv("MCP_IDENTITY_AUDIENCE", ADMIN_AUDIENCE)
    assert expected_edge_pair("vault") == (ADMIN_ISSUER, ADMIN_AUDIENCE)
    with pytest.raises(pytest.fail.Exception, match="plane chart declares"):
        expected_edge_pair("firecrawl")  # public route, admin override


def test_each_plane_chart_declares_the_pair_this_suite_resolves() -> None:
    """Pin BOTH planes' issuer/audience pairs to their own charts, not to a comment.

    This suite's pair was wrong once (the IdM root instead of Kanidm's per-client
    issuer), and one shared pair afterwards quietly applied the public client's
    expectations to admin-plane work. The gateway validates the per-client pair
    exactly, so each plane's ``auth.issuer`` / ``auth.audience`` is read out of that
    plane's own chart and compared: drift fails loudly, in the plane it happened in.
    Read-only - this test never edits another chart.
    """
    for plane, path in CHART_VALUES.items():
        if not path.is_file():
            pytest.skip(f"{path} is not available to compare against")
        declared = chart_pair(plane)
        assert declared == DECLARED_PAIR[plane], (
            f"{plane} plane chart declares {declared}, this suite resolves "
            f"{DECLARED_PAIR[plane]}: fix the constant or the chart in its own review, "
            "do not loosen the comparison")
    # And the resolution actually used by a live run, per target.
    for target, plane in TARGET_PLANE.items():
        assert expected_edge_pair(target) == DECLARED_PAIR[plane], (
            f"target {target!r} resolves to the {plane!r} pair, expected "
            f"{DECLARED_PAIR[plane]}")


def test_target_resolves_to_a_declared_plane_never_implicitly_public() -> None:
    """The mapping is explicit, so an unlisted target cannot inherit "public".

    Vault is the case that used to be wrong by silence: its route lives on the admin
    plane with its own Kanidm client, and measuring it against the public pair would
    have looked like a passing identity gate while testing an audience the gateway
    never accepts there.
    """
    assert plane_of("firecrawl") == PUBLIC
    assert plane_of("grafana") == PUBLIC, (
        "Grafana belongs on the public plane per the approved architecture; if that "
        "decision changes, change TARGET_PLANE deliberately, not as a side effect")
    assert plane_of("vault") == ADMIN
    assert plane_of(" GRAFANA ") == PUBLIC  # case/whitespace tolerant, not name tolerant
    for bad in ("", "grafana-admin", "admin", "searxng", "mcp-gateway"):
        with pytest.raises(KeyError, match="no declared plane"):
            plane_of(bad)
    assert ADMIN_ISSUER != PUBLIC_ISSUER and ADMIN_AUDIENCE != PUBLIC_AUDIENCE
    assert expected_edge_pair("vault") == (ADMIN_ISSUER, ADMIN_AUDIENCE)
    assert expected_edge_pair("firecrawl") == (PUBLIC_ISSUER, PUBLIC_AUDIENCE)


def test_backends_are_declared_on_their_approved_plane() -> None:
    """Chart DISTRIBUTION check - red until the gateway's chart correction lands.

    Deliberately not adapted to the current tree and deliberately not skipped:
    main's approved diagram puts Firecrawl and Grafana on the public plane and Vault
    on the admin plane, and Grafana was placed on the admin plane without approval.
    A harness that read the pair from wherever a backend happened to be declared
    would certify that move after the fact, so this test states the expectation and
    fails. When the gateway moves Grafana back, it goes green on its own; nothing in
    this file may move a route between planes.
    """
    public_backends = chart_backends(PUBLIC)
    admin_backends = chart_backends(ADMIN)
    problems = []
    for target in sorted(TARGET_PLANE):
        plane = plane_of(target)
        here, there = (public_backends, admin_backends) if plane == PUBLIC \
            else (admin_backends, public_backends)
        other = ADMIN if plane == PUBLIC else PUBLIC
        if target not in here:
            problems.append(f"{target} is not declared in the {plane} chart at all")
        if target in there:
            problems.append(f"{target} is declared on the {other} plane, approved "
                            f"placement is {plane}")
    assert not problems, (
        "backend placement does not match the approved architecture: "
        + "; ".join(problems)
        + " - this is the pending gateway chart correction (Grafana belongs on the "
          "public plane, per-client issuer/audience of that plane), reported as a "
          "failure on purpose rather than adapted to")


def test_route_shape_is_per_target_and_never_aggregate() -> None:
    base = "https://mcp.grigri.cloud"
    assert route_for(base, "firecrawl") == "https://mcp.grigri.cloud/firecrawl/mcp"
    assert route_for(base, "grafana") == "https://mcp.grigri.cloud/grafana/mcp"
    # An empty target must not silently build the aggregate route that does not
    # exist on this gateway.
    with pytest.raises(ValueError, match="no aggregate"):
        route_for(base, "")


def test_every_gateway_request_shares_one_sender() -> None:
    """The probes cannot differ in transport shape, and that is testable offline.

    Both the mediated probe and the stolen-session replay go through ``mcp_rpc``,
    so a header or content-type difference between them would make a crossover
    result an artefact of the harness. Driven against ``httpx.MockTransport`` with
    no network at all.
    """
    seen: list[httpx.Request] = []

    def handler(request: httpx.Request) -> httpx.Response:
        seen.append(request)
        return httpx.Response(200, json={"result": {"content": []}},
                              headers={"mcp-session-id": "session-from-gateway"})

    transport = httpx.MockTransport(handler)
    url = route_for("https://mcp.grigri.cloud", "firecrawl")
    mcp_initialize(url, "caller-A", transport=transport)
    mcp_probe(url, "caller-B", "read_thing", {"path": "a-only"},
              session_id="session-from-gateway", transport=transport)
    assert len(seen) == 2, "expected exactly the two probes"
    for request in seen:
        assert request.url.path == "/firecrawl/mcp"
        assert "text/event-stream" in request.headers["accept"]
        assert request.headers["mcp-protocol-version"] == "2025-06-18"
        assert request.headers["content-type"] == "application/json"
        assert request.headers["authorization"].startswith("Bearer ")
    # Only the second one carries a session id, and the stolen-session case is
    # exactly that: B's bearer plus A's session id.
    assert "mcp-session-id" not in seen[0].headers
    assert seen[1].headers["mcp-session-id"] == "session-from-gateway"
    assert seen[1].headers["authorization"] == "Bearer caller-B"


def test_classify_maps_responses_to_attributable_outcomes() -> None:
    """The crossover assertions depend on this mapping, so it is pinned offline.

    ``granted`` means a backend returned data (the only outcome that can be a
    leak); ``denied`` means it answered but refused this caller; ``unauthenticated``
    means the gateway refused the credential; ``unverifiable`` means a 5xx, which
    is deliberately NOT lumped in with a refusal.
    """
    served = {"result": {"content": [{"type": "text", "text": "data"}]}}
    assert classify(200, served) == PROBE_GRANTED
    assert classify(200, {"result": {"isError": True, "content": []}}) == PROBE_DENIED
    assert classify(200, {"error": {"code": -32001, "message": "forbidden"}}) == PROBE_DENIED
    assert classify(401, None) == PROBE_UNAUTHENTICATED
    assert classify(403, None) == PROBE_UNAUTHENTICATED
    assert classify(404, None) == PROBE_DENIED
    assert classify(500, None) == PROBE_UNVERIFIABLE
    assert classify(503, None) == PROBE_UNVERIFIABLE
    # A 200 whose body is neither JSON nor an SSE frame (an HTML error page, a
    # truncated proxy response) is not evidence about identity, and must not be
    # reported as a served read.
    assert classify(200, None) == PROBE_UNVERIFIABLE


def test_body_parsing_covers_json_and_mcp_sse_frames() -> None:
    """``safe_json`` must see through ``text/event-stream``, or reads look unreadable.

    The gateway answers streamable-HTTP calls with SSE frames, so a parser that
    only understands a bare JSON body would classify a genuine read as
    unverifiable - and an unverifiable read cannot fail a crossover test.
    """

    def response(text: str) -> httpx.Response:
        return httpx.Response(200, text=text, headers={"content-type": "text/event-stream"})

    served = response('event: message\ndata: {"result": {"content": [{"type": "text", '
                      '"text": "value"}]}}\n\n')
    assert classify(200, safe_json(served)) == PROBE_GRANTED
    denied = response('event: message\ndata: {"result": {"isError": true, "content": []}}\n\n')
    assert classify(200, safe_json(denied)) == PROBE_DENIED
    assert classify(200, safe_json(response("<html>502 Bad Gateway</html>"))) == PROBE_UNVERIFIABLE
    assert classify(200, safe_json(response("data: not json\n\n"))) == PROBE_UNVERIFIABLE


def _mock_gateway(*, inherit: bool) -> httpx.MockTransport:
    """A stand-in gateway: caller-bound, or reproducing the hypothesised leak.

    ``a-only`` is readable only when the identity *actually serving* the request is
    caller A. With ``inherit=True`` that identity is whoever opened the session -
    the behaviour a reading of agentgateway v1.7.0's session cache
    (``mcp/session.rs``: backend id + matched conditions, no caller-identity
    binding) suggests is possible. That is a HYPOTHESIS about the deployed gateway
    until the live tier has actually run: this mock proves the harness can DETECT
    the weakness, nothing more, and it is not evidence that the real gateway has it.
    No network, no credentials: the labels are literals.
    """
    owner: dict[str, str] = {}

    def handler(request: httpx.Request) -> httpx.Response:
        bearer = request.headers.get("authorization", "").removeprefix("Bearer ").strip()
        if not bearer:
            return httpx.Response(401, json={"error": {"code": -32001, "message": "no credential"}})
        sid = request.headers.get("mcp-session-id") or "sess-1"
        owner.setdefault(sid, bearer)
        served = owner[sid] if inherit else bearer
        if served == "caller-A":
            return httpx.Response(200, json={"result": {"content": [{"type": "text",
                                                                     "text": "secret"}]}})
        return httpx.Response(403, json={"error": {"code": -32001, "message": "forbidden path"}})

    return httpx.MockTransport(handler)


def test_the_crossover_test_would_actually_detect_a_crossover() -> None:
    """Non-vacuity proof for the live stolen-session test, run offline.

    A replay test can "pass" for the wrong reason: if its probe never reads
    anything, no leak is ever visible and the run looks green forever. So the
    exact same request sequence is driven here against both gateway shapes, and
    the inheriting one MUST surface as ``granted``. If this test ever fails, the
    live crossover assertion is decorative and has to be redesigned.
    """
    url = route_for("https://mcp.grigri.cloud", "firecrawl")

    def run(inherit: bool) -> list[tuple[str, str, int]]:
        transport = _mock_gateway(inherit=inherit)
        # A opens the session and reads its own fixture first, as the live test does.
        baseline = mcp_probe(url, "caller-A", "read_secret", {"path": "a-only"},
                             transport=transport)
        assert baseline[0] == PROBE_GRANTED, f"mock gateway is broken ({baseline})"
        return stolen_session_attempts(url, "caller-A", "caller-B", "read_secret",
                                       {"path": "a-only"}, "sess-1", transport=transport)

    inherited = run(inherit=True)
    assert any(outcome == PROBE_GRANTED for _label, outcome, _status in inherited), (
        f"an inheriting gateway was not detected as granting A's fixture: {inherited}")
    bound = run(inherit=False)
    assert all(outcome != PROBE_GRANTED for _label, outcome, _status in bound), (
        f"a caller-bound gateway was reported as a crossover: {bound}")
    assert all(outcome in (PROBE_UNAUTHENTICATED, PROBE_DENIED)
               for _label, outcome, _status in bound), bound


def test_reports_cannot_echo_credentials() -> None:
    """Sanitised comparison output: labels and shapes yes, credential values never.

    Every field name the gateway/Vault/Grafana responses can carry is planted
    with a unique marker value and run through ``redact``; if any survives, an
    assertion message or report built with this helper could leak it.
    """
    planted = {
        "bearer": "caller-secret-DO-NOT-ECHO",
        "jwt": "eyJhbGciOiJSUzI1NiJ9.eyJzdWIiOiJhZWFkZWFkZWFkZSJ9.c2lnX3ZhbHVlX2RvX25vdF9lY2hv",
        "vault": "s.kwA1backendsecretdonotecho",
        "kv": "kvpair-secret-value-DO-NOT-ECHO",
    }
    raw = (
        f"initialize failed Bearer {planted['bearer']} "
        f"id_token={planted['jwt']} token={planted['vault']} "
        f"access_token='{planted['kv']}' client_secret: {planted['kv']} "
        f"subject_token={planted['kv']} refresh_token={planted['kv']}"
    )
    safe = redact(raw)
    for name, value in planted.items():
        assert value not in safe, f"{name} survived redaction: {safe}"
    assert "Bearer <redacted>" in safe, safe
    assert "<jwt>" in safe, safe
    assert "<vault-token>" in safe, safe
    assert "access_token='<redacted>'" in safe and "client_secret: <redacted>" in safe, safe

    # Bare occurrences (no key=value context) must also be caught where the
    # shape identifies them. An opaque random string cannot be recognised as a
    # credential by any regex - which is exactly why the harness never
    # interpolates token values into messages in the first place and compares
    # identity_label() digests instead.
    bare = redact(f"rejected: {planted['jwt']} / {planted['vault']}")
    assert planted["jwt"] not in bare and planted["vault"] not in bare, bare

    label = identity_label(_fake_jwt("hermes-2-mcp"))
    assert "DO-NOT-ECHO" not in label and label.count("/") == 1


# ---------------------------------------------------------------------------
# LIVE tier: two real HUMAN identities, proven through the gateway.
#
# What the pinned sources say (a READING, to be confirmed by the live run - the
# offline mock below only proves this suite can detect the weakness, it is not
# evidence that the deployed gateway has it):
#
# * agentgateway v1.7.0 appears to resume an MCP session from the encrypted
#   session id plus backend/condition matches, with no caller-identity binding in
#   the session cache (mcp/session.rs). Session-id encryption keeps an eavesdropper
#   from guessing one; it does nothing about a caller who legitimately holds A's id
#   and presents it. So "the id was AES-GCM sealed" proves nothing about
#   authorisation, and no test here may conclude isolation from it. Whether the
#   live gateway actually lets B ride A's session is exactly what the run decides;
#   the owner's separate ``jwt.sub`` conditions investigation is the fix path.
# * vault-mcp 0.2.0 caches the Vault credential per backend session, so a
#   rotation is not visible until that session ends. The upstream main branch has
#   token-hash/session protection that is NOT in the pinned 0.2.0 release.
#
# Either way the requirement is fixed and does not depend on the reading: the only
# acceptable outcomes for "caller B presents caller A's session id" are rejected
# outright, or executed strictly as B. Inheriting any of A's capabilities is a
# FAILURE - never re-labelled as a documented, accepted risk, and never skipped
# once a request was actually served.
# ---------------------------------------------------------------------------

PROBE_DENIED = "denied"
PROBE_GRANTED = "granted"
PROBE_UNAUTHENTICATED = "unauthenticated"
PROBE_UNVERIFIABLE = "unverifiable"


def classify(status: int, body: object) -> str:
    """Which identity did the backend serve? Coarse, non-secret, four-way.

    ``unauthenticated`` covers a rejected credential (401/403 at the gateway) and
    is a legitimate outcome for a stolen-session probe; ``denied`` means the
    request reached a backend that refused the operation for THIS caller;
    ``granted`` means a JSON-RPC result came back; ``unverifiable`` means either a
    5xx or a 2xx whose body carries no JSON-RPC envelope, i.e. the request got as
    far as routing/transport and nobody knows which identity would have been served.

    Only ``granted`` on someone else's fixture is a crossover. Neither kind of
    ``unverifiable`` is ever treated as a safe refusal: "it did not work" and "it
    was denied to this caller" are different claims, and only the second one is
    evidence.
    """
    if status >= 500:
        return PROBE_UNVERIFIABLE
    if status in (401, 403):
        return PROBE_UNAUTHENTICATED
    if status >= 400:
        return PROBE_DENIED
    if not isinstance(body, dict):
        return PROBE_UNVERIFIABLE
    result = body.get("result")
    if isinstance(result, dict) and result.get("isError"):
        return PROBE_DENIED
    if result is None and body.get("error"):
        return PROBE_DENIED
    return PROBE_GRANTED


def probe_prerequisites() -> tuple[str, dict, dict]:
    """The gateway-mediated identity probe, or a skip naming what is missing.

    Parity must be observed THROUGH the gateway (caller JWT -> gateway -> MCP
    backend -> native backend identity). A direct ``/api/user`` or
    ``lookup-self`` call is a baseline on its own: it shows the two identities
    differ at the backend, never what the gateway sends on their behalf. So the
    mediated tests require an operator-declared read-only tool and two fixtures
    that only the matching identity may read; without them these tests report
    "pending authoritative backend-mediated proof" instead of passing on the
    baseline.

    Which target can settle it depends on the route's declared credential model
    (``apps/mcp-gateway*/values.yaml``, ``backends.<name>.credential``) - and note
    first that ``credential`` describes the credential the BACKEND receives, never
    what the caller presents: the gateway edge authenticates an OAuth2 ACCESS token
    on every route (see ``edge_bearer_must_be_an_access_token``).

    * ``grafana``: ``credential: accessToken`` - ``backendAuth.passthrough`` forwards
      the caller's validated Kanidm ACCESS token, and that is the shape the edge and
      this suite accept. The template keeps ``idToken`` only as the measured record of
      the alternative and it is NOT approved: a route's ``credential`` names what the
      *backend* receives, never what the caller presents, and an ID token must never
      become an edge bearer or a silent approved fallback. Whether Grafana can map the
      forwarded ACCESS token to an existing native user at all is the gateway agent's
      open question (the pinned ``[auth.jwt]`` has no login/email claim to read from a
      Kanidm access token), and no per-user OIDC passthrough is claimed here.
    * ``firecrawl``: ``credential: none`` - the caller's bearer stops at the gateway
      and the backend uses its own fixed server-side config. So a difference seen on
      this route measures the gateway's scope decision only: valid for allow/deny and
      forged-credential rejection, never as backend-identity parity. This route is
      also not currently a safe target for a live run at all: the Firecrawl egress
      NetworkPolicy change caused a probe-timeout health regression and was reverted
      (AGENTS.md, Applications), and private-destination reachability has never been
      verified.
    * ``vault``: not routable today (``enabled: false``, ``credential: none``), so
      its cases can only run once that route forwards a caller's own native
      credential.
    """
    tool = (os.environ.get("MCP_IDENTITY_PROBE_TOOL") or "").strip()
    a_only = (os.environ.get("MCP_IDENTITY_PROBE_ARG_A_ONLY") or "").strip()
    b_only = (os.environ.get("MCP_IDENTITY_PROBE_ARG_B_ONLY") or "").strip()
    arg_key = (os.environ.get("MCP_IDENTITY_PROBE_ARG_KEY") or "path").strip()
    missing = [name for name, value in ((
        ("MCP_IDENTITY_PROBE_TOOL", tool),
        ("MCP_IDENTITY_PROBE_ARG_A_ONLY", a_only),
        ("MCP_IDENTITY_PROBE_ARG_B_ONLY", b_only),
    )) if not value]
    if missing:
        pytest.skip(
            "gateway-mediated parity is PENDING, not proven: needs "
            f"{', '.join(missing)} (a read-only backend tool plus one fixture path "
            "readable only by A and one readable only by B). Direct backend readouts "
            "alone are a baseline, not parity.")
    return tool, {arg_key: a_only}, {arg_key: b_only}


def mcp_probe(url: str, bearer: str, tool: str, args: dict, *, session_id: str | None = None,
              extra_headers: dict | None = None, query: dict | None = None,
              transport: httpx.BaseTransport | None = None) -> tuple[str, int]:
    """Run one read-only tool call through the gateway and classify the outcome."""
    resp = mcp_rpc(url, bearer, "tools/call", params={"name": tool, "arguments": args},
                   session_id=session_id, extra_headers=extra_headers, query=query,
                   transport=transport)
    outcome = classify(resp.status_code, safe_json(resp))
    return outcome, resp.status_code


def safe_json(resp: httpx.Response) -> object:
    """Parse a gateway response body: plain JSON, or MCP streamable-HTTP SSE frames.

    A body that yields neither is returned as ``None``, which ``classify`` reports
    as unverifiable - an unreadable 2xx is not evidence of anything, and guessing
    would let a crossed-over read hide behind a parsing failure.
    """
    try:
        return json.loads(resp.text)
    except ValueError:
        pass
    for line in resp.text.splitlines():  # "data: {...}" frames
        if line.startswith("data:"):
            try:
                return json.loads(line[5:].strip())
            except ValueError:
                continue
    return None


def stolen_session_attempts(url: str, a_token: str, b_token: str, tool: str, args: dict,
                            session_id: str, *, transport: httpx.BaseTransport | None = None
                            ) -> list[tuple[str, str, int]]:
    """Replay A's session id as B, and with no bearer: (label, outcome, status).

    Pulled out of the live test so an offline test can run the identical sequence
    against a mock gateway that *does* inherit the caller, and prove that a
    crossover is reported as ``granted`` rather than passing unnoticed.
    """
    out: list[tuple[str, str, int]] = []
    for label, bearer in (("with its own bearer", b_token), ("with no bearer at all", "")):
        outcome, status = mcp_probe(url, bearer, tool, args, session_id=session_id,
                                   transport=transport)
        out.append((label, outcome, status))
    return out


def live_caller_claims(context, who: str, target: str) -> dict:
    """Decode a LIVE caller token against its own plane's pair, or stop.

    Every live site goes through this one helper, for two reasons:

    * pair resolution is per target, so no test can keep an ambient public
      issuer/audience while probing an admin route (the bug this correction removes -
      the scope-gate test and the rejection matrix each used to hard-code it);
    * the credential class must be established as an ACCESS token before any request
      is made. In the live tier an unprovable class FAILS instead of skipping,
      because a skip here would leave the parity tests free to report a green
      verdict on a credential nobody verified the class of.

    Returns the claims so a caller can also assert the route scope from them.
    """
    issuer, audience = expected_edge_pair(target)
    claims = decoded_claims_must_match(context.token(), audience=audience, issuer=issuer)
    edge_bearer_must_be_an_access_token(claims, who=who, strict=True)
    return claims


def test_two_human_contexts_are_distinct_identities(contexts, target) -> None:
    """Prerequisite proof for the HUMAN A/B gate; it FAILS if A and B are one.

    These are two *human* Kanidm accounts holding the permissions they already
    have. The workload service accounts (``hermes-mcp`` and friends) are about
    token renewal for the Hermes sidecars and are deliberately NOT part of this
    gate: two identical workloads could not show a permission difference, and
    nothing here may create a person or a group membership to get one.
    """
    a, b = contexts
    # Resolved per target and validated against that target's plane chart; both
    # bearers must be established ACCESS tokens (live tier: fail, never skip).
    ca = live_caller_claims(a, "A", target)
    cb = live_caller_claims(b, "B", target)
    assert ca.get("sub") != cb.get("sub"), (
        f"both caller tokens carry the same subject ({identity_label(a.token())}): "
        "the A/B gate needs two human accounts that already differ in permissions")
    # What must NOT be the difference: the gateway's own scope grant. Both humans
    # have to hold the route scope (the target's plane chart,
    # ``oauth2Client.humanScopeMap``; both planes ship it EMPTY today, so the owner
    # has to name the group before a live run is possible, and membership is manual,
    # never edited here), because a caller without it is
    # refused at the edge with 403 - that measures the scope gate, not backend
    # permissions, and it is tested separately below. The parity difference has to
    # come from the BACKEND's own RBAC: different native roles over different
    # protected resources (the A-only / B-only fixtures).
    route_scope = (os.environ.get("MCP_IDENTITY_ROUTE_SCOPE") or "").strip()
    if not route_scope:
        pytest.skip("MCP_IDENTITY_ROUTE_SCOPE is not set, so this suite cannot prove both "
                    "callers hold the route scope: with one caller outside the grant, the "
                    "pair would measure the gateway's scope gate instead of backend RBAC")
    for claims, who in ((ca, "A"), (cb, "B")):
        granted = scope_set(claims)
        assert route_scope in granted, (
            f"caller {who} does not hold the route scope {route_scope!r} (granted: "
            f"{sorted(granted)}): the gateway would refuse it at the edge, so this pair "
            "tests the scope gate, not backend parity. Supply two humans who both have "
            "the scope and DIFFERENT backend rights; the negative case belongs in "
            "test_scope_gate_rejects_a_caller_without_the_route_scope")
    # Decoding proved shape only (see test_unverified_decode_proves_nothing_about_
    # authenticity); the expiry claim is what the gateway will actually enforce.
    for claims, who in ((ca, "A"), (cb, "B")):
        assert claims["exp"] > time.time(), f"caller context {who} is already expired"


def test_gateway_rejects_untrusted_caller_tokens(mcp_url, contexts, target) -> None:
    """Every credential the edge must refuse - with honest attribution.

    Positive control first: a *real* caller token must be accepted by the same
    route, or rejecting everything would look like a pass. A control that comes back
    unauthenticated is a missing prerequisite (the route or its scope grant is off); a
    control that comes back 5xx is a broken gateway and is failed, not skipped.

    Each case below mutates exactly ONE intended defect and keeps the rest of the
    claim set at what THIS plane issues, using the pair resolved from the target
    (``expected_edge_pair``), so a public-plane defect test can never be run against
    an admin route by ambient default - and the near-miss issuers used to be
    hard-coded to the public client, which meant an admin run tested the wrong thing.

    Attribution limit, stated instead of hidden: this suite cannot sign with the
    Kanidm client key, so every token here fails signature verification first and the
    gateway refuses it as an unverified credential. A rejection therefore proves the
    edge does not admit untrusted bearers; it does NOT prove which claim check
    (issuer, audience, expiry) fired. Isolating a claim defect needs a validly signed
    token with one wrong claim, which is the gateway's own real-binary local suite
    against a test issuer - not this harness. What stays required of the gateway
    either way: never 200, never 5xx.
    """
    a, _b = contexts
    control, _sid = mcp_initialize(mcp_url, a.token())
    if control >= 500:
        pytest.fail(f"the gateway answered HTTP {control} to a valid caller token: a 5xx "
                    "means the credential reached routing/transport, so nothing about "
                    "authorisation can be read from this run")
    if control >= 400:
        pytest.skip(
            f"the route also rejected a valid caller token (HTTP {control}), so rejecting "
            "untrusted ones proves nothing: enable the route and its scope grant first "
            f"(the {plane_of(target)} plane chart, backends.<target>) and re-run")
    issuer, audience = expected_edge_pair(target)
    other_issuer, other_audience = DECLARED_PAIR[ADMIN if plane_of(target) == PUBLIC
                                                 else PUBLIC]
    key = "an-arbitrary-key-the-issuer-never-published-32b"
    now = int(time.time())

    def mint(name: str, **claim) -> tuple[str, str]:
        claims = {"sub": name, "aud": [audience], "iss": issuer, "exp": now + 600}
        claims.update(claim)
        return name, jwt.encode(claims, key=key, algorithm="HS256")

    cases = dict([
        mint("foreign-signature"),  # claims all correct, only the key is wrong
        mint("expired", exp=now - 60),
        mint("wrong-issuer", iss=f"{issuer}.attacker.example"),
        mint("issuer-trailing-slash", iss=f"{issuer}/"),
        # Kanidm signs a separate issuer per OAuth2 client, so the IdM root and the
        # OTHER plane's genuine client issuer are both wrong here even though both
        # are real Kanidm issuers - the shapes a host-only check would accept.
        mint("idm-root-issuer", iss="https://idm.grigri.cloud"),
        mint("other-plane-issuer", iss=other_issuer),
        mint("wrong-audience", aud=["something-else"]),
        mint("other-plane-audience", aud=[other_audience]),
    ])
    cases["not-a-token"] = "not.a.jwt"
    for name, token in cases.items():
        status, _ = mcp_initialize(mcp_url, token)
        assert status < 500, f"{name}: gateway returned {status} (a 5xx means the token " \
                             "reached the routing/transport layer)"
        assert status in (400, 401, 403), \
            f"{name} was accepted by the gateway with {status}: unverified caller tokens " \
            "must be rejected at the edge, before any backend call"


def test_scope_gate_rejects_a_caller_without_the_route_scope(mcp_url, target) -> None:
    """Separate, explicitly non-parity check: the gateway's own admission decision.

    A caller that does not hold the route scope must be refused at the edge. That
    is a *scope-gate* result and must never be reported as an A/B permission
    difference - if one of the two parity callers lacks the scope, what was
    measured here is all there is, and the parity question is still unanswered.

    Optional: needs ``MCP_IDENTITY_CALLER_UNGRANTED_FILE``, a real human ACCESS
    token known not to hold ``MCP_IDENTITY_ROUTE_SCOPE``. No test creates or edits
    a membership to produce one.
    """
    route_scope = (os.environ.get("MCP_IDENTITY_ROUTE_SCOPE") or "").strip()
    if not route_scope:
        pytest.skip("MCP_IDENTITY_ROUTE_SCOPE is not set: without the scope name the "
                    "negative case cannot be verified as a scope-gate result")
    if not (os.environ.get("MCP_IDENTITY_CALLER_UNGRANTED_FILE") or "").strip():
        pytest.skip("MCP_IDENTITY_CALLER_UNGRANTED_FILE is not set: this needs one real "
                    "human ACCESS token known NOT to hold the route scope (never created, "
                    "granted or edited by this suite)")
    ungranted = load_context("MCP_IDENTITY_CALLER_UNGRANTED_FILE", "ungranted")
    # Same per-target pair resolution and the same ACCESS-token requirement as the
    # parity gate: a negative case measured with the wrong issuer/audience, or with a
    # credential whose class is unknown, says nothing about the scope decision.
    claims = live_caller_claims(ungranted, "ungranted", target)
    assert route_scope not in scope_set(claims), (
        f"the context labelled ungranted actually holds {route_scope!r}: it is not a "
        "negative case, and using it would make this test meaningless")

    status, _sid = mcp_initialize(mcp_url, ungranted.token())
    assert status < 500, f"gateway returned {status} for an ungranted caller: a 5xx means " \
                         "the credential reached routing/transport, not the authoriser"
    assert status in (401, 403), (
        f"an ungranted caller was accepted with HTTP {status}: the gateway is not enforcing "
        "the route scope, so every A/B permission result on this route is untrustworthy")


def test_backend_mediated_permissions_differ_by_human_caller(mcp_url, contexts,
                                                             target) -> None:
    """Core acceptance: THROUGH the gateway, A's fixture is readable by A only.

    The whole point of two *different* humans: if the gateway collapsed them onto
    one shared backend credential, both fixtures would behave identically. Read
    only - nothing is created or modified - and each result is attributable to the
    exact probe because the fixture path itself is the identity-bearing argument.

    Order matters, and it is what makes this a parity test rather than a scope
    test: both callers must first be admitted at the edge (baseline success), then
    the difference must show up on the backend's own protected resources. A caller
    refused at the edge is the scope gate - that is a different question, covered
    by test_scope_gate_rejects_a_caller_without_the_route_scope, and it must never
    be reported as "B cannot read A's data because the backend RBAC said so".
    """
    a, b = contexts
    tool, a_args, b_args = probe_prerequisites()
    # Credential class and plane-correct pair, before anything is sent.
    live_caller_claims(a, "A", target)
    live_caller_claims(b, "B", target)

    _, sess_a = mcp_initialize(mcp_url, a.token())
    _, sess_b = mcp_initialize(mcp_url, b.token())
    outcomes = {
        ("A", "own-fixture"): mcp_probe(mcp_url, a.token(), tool, a_args, session_id=sess_a)[0],
        ("A", "other-fixture"): mcp_probe(mcp_url, a.token(), tool, b_args, session_id=sess_a)[0],
        ("B", "own-fixture"): mcp_probe(mcp_url, b.token(), tool, b_args, session_id=sess_b)[0],
        ("B", "other-fixture"): mcp_probe(mcp_url, b.token(), tool, a_args, session_id=sess_b)[0],
    }
    # Edge admission first, per caller. Both must pass, or there is no parity
    # measurement to make: the pair differs at the gateway, not at the backend.
    refused = [who for who in ("A", "B")
               if outcomes[(who, "own-fixture")] == PROBE_UNAUTHENTICATED]
    if refused:
        pytest.fail(
            f"caller(s) {refused} were refused at the gateway edge even for their own "
            "fixture, so this run measured the scope gate, not backend parity: both "
            "humans must already hold the route scope (MCP_IDENTITY_ROUTE_SCOPE) and "
            "differ only in the backend's own RBAC")
    # Each caller must see its own fixture served (granted or a backend-side
    # denial, which still proves the backend acted AS that caller) and must never
    # be served the other caller's fixture.
    for who in ("A", "B"):
        other = "B" if who == "A" else "A"
        assert outcomes[(who, "other-fixture")] != PROBE_GRANTED, (
            f"caller {who} was GRANTED caller {other}'s exclusive fixture through the "
            "gateway: the backend is not acting as the caller")
    unverifiable = sorted(str(k) for k, v in outcomes.items() if v == PROBE_UNVERIFIABLE)
    assert not unverifiable, (
        f"the gateway failed (5xx) on {unverifiable}: no identity can be attributed to a "
        "request that did not complete, so parity is unproven rather than proven")
    granted = {k for k, v in outcomes.items() if v == PROBE_GRANTED}
    if not granted:
        pytest.skip("neither caller was served any fixture: the declared probe tool/args do "
                    "not match this backend, so the crossover assertion above proved nothing "
                    "positive - this is PENDING authoritative evidence, not a pass")
    assert granted <= {("A", "own-fixture"), ("B", "own-fixture")}, (
        f"successful reads were not exclusively own-fixture reads: {sorted(granted)}")


def test_stolen_session_id_cannot_inherit_the_original_caller(mcp_url, contexts,
                                                               target) -> None:
    """A session id A holds, presented by B, must not act as A.

    Targets the session cache the pinned source describes: resumption keyed on the
    backend and matched conditions, with no caller-identity binding, and the id
    handed to A in a response header. That reading is the hypothesis; this run is
    the evidence. B replaying A's id with B's own bearer is the crossover session-id
    encryption does NOT prevent. Acceptable: the gateway rejects (401/403/404) or
    the request is served strictly as B. Failure: any ``granted`` on A's exclusive
    fixture - and no skip, since the request was served.
    """
    a, b = contexts
    tool, a_args, _b_args = probe_prerequisites()
    # Same rule as the parity gate: the pair comes from this target's plane and both
    # bearers must be established ACCESS tokens, otherwise a crossover verdict could
    # be produced by a credential this suite never characterised correctly.
    live_caller_claims(a, "A", target)
    live_caller_claims(b, "B", target)
    _, sess_a = mcp_initialize(mcp_url, a.token())
    if not sess_a:
        pytest.skip("the gateway issued no Mcp-Session-Id for A, so there is no session "
                    "object to steal; caller-scoped sessions must be asserted inside the "
                    "gateway's own tests (agentgateway exposes no header to check here)")

    baseline = mcp_probe(mcp_url, a.token(), tool, a_args, session_id=sess_a)[0]
    assert baseline == PROBE_GRANTED, (
        f"A cannot read its own fixture ({baseline}): fixture assumptions are wrong, "
        "so the crossover test below would be vacuous")

    attempts = stolen_session_attempts(mcp_url, a.token(), b.token(), tool, a_args, sess_a)
    for label, outcome, status in attempts:
        if outcome == PROBE_GRANTED:
            pytest.fail(
                f"B replayed A's session id ({label}, HTTP {status}) and was GRANTED A's "
                "exclusive fixture: the session inherited caller A's capability. That is a "
                "failure whatever the mechanism - it is not re-labelled as an accepted risk, "
                "and it is not skipped because the request was served. (The pinned "
                "agentgateway v1.7.0 session cache is the hypothesis for why; confirming it, "
                "and the `jwt.sub` conditions fix, is the gateway agent's work, not this "
                "assertion's.)")
        if outcome == PROBE_UNVERIFIABLE:
            pytest.fail(
                f"B replaying A's session id {label} produced HTTP {status}: a gateway failure "
                "is not a refusal, so no identity can be attributed to that request. This is "
                "reported as a failure to investigate, never as a pass.")
        if label == "with no bearer at all":
            # Reaching here means the request was refused (401/403) or denied by the
            # backend (4xx): either way, A's session id alone did not authorise it.
            assert outcome in (PROBE_UNAUTHENTICATED, PROBE_DENIED), outcome


def test_session_identity_is_pinned_for_the_whole_session(mcp_url, contexts) -> None:
    """The pinned vault-mcp caches its credential per session: rotation is inert.

    Documented so a future "token rotation did not take effect" report is not
    mistaken for an identity leak: within one session the backend credential is
    fixed until the session ends. The security-relevant half - a *different*
    caller riding that session - is the test above, and upstream's token-hash
    session binding is not in the pinned 0.2.0 release, so this behaviour is
    expected, not assumed to be fixed.
    """
    a, _b = contexts
    tool, a_args, _ = probe_prerequisites()
    _, sess = mcp_initialize(mcp_url, a.token())
    first = mcp_probe(mcp_url, a.token(), tool, a_args, session_id=sess)[0]
    second = mcp_probe(mcp_url, a.token(), tool, a_args, session_id=sess)[0]
    assert first == second, f"same caller, same session, different outcome: {first}/{second}"


def test_native_vault_token_header_is_not_overridden_by_a_fallback(
        mcp_url, target, contexts) -> None:
    """Caller A's own ``X-Vault-Token`` must win over any unrequested channel.

    A native per-user Vault credential in the header is legitimate and must not
    be stripped (the owner's zero-broker path: OIDC login -> the user's own Vault
    token). What must never happen is a second channel - ``?token=``,
    ``?access_token=``, ``?vault_token=``, ``VAULT_TOKEN`` in a header - changing
    which identity is served. Attribution is by fixture: A's exclusive path is
    readable while B's exclusive token is smuggled in, and B's exclusive path is
    not.
    """
    if target != "vault":
        pytest.skip(f"target is '{target}': needs MCP_IDENTITY_TARGET=vault")
    a, _b = contexts
    tool, a_args, b_args = probe_prerequisites()
    # load_context, not a bare Path.read_text: the caller's own native Vault token
    # is a credential and obeys the same rules as the JWTs (file only, mode 0600,
    # never an env value), and it skips naming the exact variable that is missing.
    own = load_context("MCP_IDENTITY_VAULT_A_FILE", "vault-A").token()
    other = load_context("MCP_IDENTITY_VAULT_B_FILE", "vault-B").token()

    own_headers = {"X-Vault-Token": own}
    _, sess = mcp_initialize(mcp_url, a.token(), extra_headers=own_headers)
    baseline = mcp_probe(mcp_url, a.token(), tool, a_args, session_id=sess,
                         extra_headers=own_headers)
    assert baseline[0] == PROBE_GRANTED, (
        f"A's own X-Vault-Token did not read A's fixture ({baseline[0]}/{baseline[1]}): this "
        "route is not forwarding the caller's native credential, so header-vs-fallback "
        "precedence cannot be observed at all (that is a route configuration question, not a "
        "pass or a leak)")

    # One fresh session per channel: the pinned vault-mcp caches the credential when
    # the backend session opens, so a fallback smuggled into a later request of an
    # existing session could be ignored for reasons that have nothing to do with
    # precedence. Presenting the channel at initialize is the only fair test.
    for channel, header_extra, query_extra in (
        ("query:token", None, {"token": other}),
        ("query:vault_token", None, {"vault_token": other}),
        ("query:access_token", None, {"access_token": other}),
        ("header:VAULT_TOKEN", "VAULT_TOKEN", None),
    ):
        headers = dict(own_headers)
        if header_extra:
            headers[header_extra] = other
        status, sid = mcp_initialize(mcp_url, a.token(), extra_headers=headers, query=query_extra)
        assert status < 500, f"gateway returned {status} for an initialize carrying {channel}"
        own_read = mcp_probe(mcp_url, a.token(), tool, a_args, session_id=sid,
                             extra_headers=headers, query=query_extra)
        cross_read = mcp_probe(mcp_url, a.token(), tool, b_args, session_id=sid,
                               extra_headers=headers, query=query_extra)
        # Attribution as a pair, because a status code alone cannot separate
        # "channel ignored" from "channel used": only which fixture was served says.
        if cross_read[0] == PROBE_GRANTED:
            pytest.fail(f"{channel}: the fallback credential was honoured - A's request read "
                        f"B's exclusive fixture (HTTP {cross_read[1]})")
        if own_read[0] == PROBE_GRANTED:
            continue  # still served as A: the extra channel was ignored, which is correct
        if own_read[1] in (400, 401, 403) and cross_read[1] in (400, 401, 403):
            continue  # the gateway refused the ambiguous request outright: safe, nothing served
        pytest.fail(f"{channel}: A's header credential stopped being honoured without the "
                    f"fallback being honoured either (own {own_read}, other {cross_read}) - "
                    "some third identity is now serving the request")


def test_no_alternate_credential_channel_is_honoured(mcp_url, contexts) -> None:
    """Query-string and duplicate-header caller credentials must not be accepted.

    A 4xx on every variant is the expected outcome and needs no backend readout,
    so that half is asserted here. A 2xx is NOT a pass: the gateway may be
    ignoring the extra channel, or it may be reading it, and a status cannot tell
    them apart - so the suite reports pending and names the readout that would
    settle it instead of guessing. ``X-Vault-Token`` is deliberately absent: it is
    a caller's legitimate native credential (see the precedence test).
    """
    a, b = contexts
    victim, attacker = a.token(), b.token()

    outcomes: dict[str, int] = {}
    for key in TOKEN_QUERY_KEYS:
        status, _ = mcp_initialize(mcp_url, victim, query={key: attacker})
        assert status < 500, f"gateway crashed (HTTP {status}) on a ?{key}= credential"
        outcomes[f"query:{key}"] = status
    for header in ("X-Forwarded-Authorization", "X-Access-Token", "X-Original-Token"):
        status, _ = mcp_initialize(mcp_url, victim, extra_headers={header: f"Bearer {attacker}"})
        assert status < 500, f"gateway crashed (HTTP {status}) on a {header} credential"
        outcomes[f"header:{header}"] = status

    accepted = {k: v for k, v in outcomes.items() if v < 400}
    if not accepted:
        return  # fully verified without a readout: every alternate channel was rejected
    pytest.skip(
        f"gateway answered {sorted(accepted)} with an extra credential channel; proving "
        "WHICH identity was served needs the mediated readout (MCP_IDENTITY_PROBE_TOOL + "
        "MCP_IDENTITY_PROBE_ARG_A_ONLY/_B_ONLY). A status code alone cannot separate "
        "'channel ignored' from 'channel used', so this suite will not claim either.")


def test_baseline_grafana_readouts_are_distinct_principals() -> None:
    """BASELINE ONLY: the two humans really are two principals at Grafana.

    Explicitly not parity evidence - it bypasses the gateway, so it says nothing
    about what the gateway sends on a caller's behalf. It exists because the
    mediated tests are meaningless if the two accounts are the same principal or
    share permissions; that assumption is checked here, cheaply and read-only.
    """
    grafana = (os.environ.get("MCP_IDENTITY_BACKEND_GRAFANA_URL") or "").strip()
    a_file, b_file = (os.environ.get("MCP_IDENTITY_GRAFANA_A_FILE"),
                      os.environ.get("MCP_IDENTITY_GRAFANA_B_FILE"))
    if not (grafana and a_file and b_file):
        pytest.skip("needs MCP_IDENTITY_BACKEND_GRAFANA_URL plus _GRAFANA_A_FILE and _B_FILE")

    def who(token_file: str) -> dict:
        resp = httpx.get(f"{grafana.rstrip('/')}/api/user",
                         headers={"Authorization": f"Bearer {Path(token_file).read_text().strip()}"},
                         timeout=20.0)
        assert resp.status_code == 200, f"Grafana /api/user -> {resp.status_code}"
        return resp.json()

    a, b = who(a_file), who(b_file)
    assert a["id"] != b["id"] and a["login"] != b["login"], (
        "both backend contexts resolve to the same Grafana principal")


def test_baseline_grafana_read_only_rbac_difference() -> None:
    """BASELINE: the two accounts differ by a READ, so no write fixture is needed.

    Preference order for the A/B fixture set: a protected dashboard/folder or
    datasource that one account may read and the other may not. That is enough to
    make a permission difference observable without mutating anything, and it is
    what the mediated test builds on.
    """
    grafana = (os.environ.get("MCP_IDENTITY_BACKEND_GRAFANA_URL") or "").strip()
    a_file = os.environ.get("MCP_IDENTITY_GRAFANA_A_FILE")
    b_file = os.environ.get("MCP_IDENTITY_GRAFANA_B_FILE")
    a_uid = (os.environ.get("MCP_IDENTITY_GRAFANA_A_ONLY_UID") or "").strip()
    b_uid = (os.environ.get("MCP_IDENTITY_GRAFANA_B_ONLY_UID") or "").strip()
    if not (grafana and a_file and b_file and a_uid and b_uid):
        pytest.skip("needs MCP_IDENTITY_BACKEND_GRAFANA_URL, both _GRAFANA_*_FILE values "
                    "and MCP_IDENTITY_GRAFANA_A_ONLY_UID/_B_ONLY_UID (two dashboards that "
                    "only the matching account may read)")

    def read(token_file: str, uid: str) -> int:
        token = Path(token_file).read_text().strip()
        return httpx.get(f"{grafana.rstrip('/')}/api/dashboards/uid/{uid}",
                         headers={"Authorization": f"Bearer {token}"}, timeout=20.0).status_code

    matrix = {("A", "own"): read(a_file, a_uid), ("A", "other"): read(a_file, b_uid),
              ("B", "own"): read(b_file, b_uid), ("B", "other"): read(b_file, a_uid)}
    assert matrix[("A", "own")] == 200, f"A cannot read its own dashboard: {matrix}"
    assert matrix[("B", "own")] == 200, f"B cannot read its own dashboard: {matrix}"
    assert matrix[("A", "other")] in (403, 404), f"A read B's dashboard: {matrix}"
    assert matrix[("B", "other")] in (403, 404), f"B read A's dashboard: {matrix}"


def test_baseline_vault_self_lookup_differs() -> None:
    """BASELINE: two Vault tokens, two policy sets, compared on sanitised metadata.

    Kept because the mediated Vault probe is only meaningful if the identities
    actually differ at Vault. Token values are never compared or reported; only
    policy names, token type, display name and the capabilities on the synthetic
    fixture path are, and the last assertion proves the comparison output cannot
    carry a credential.
    """
    vault = (os.environ.get("MCP_IDENTITY_BACKEND_VAULT_URL") or "").strip()
    fixture = (os.environ.get("MCP_IDENTITY_VAULT_FIXTURE_PATH") or "").strip()
    a_file = os.environ.get("MCP_IDENTITY_VAULT_A_FILE")
    b_file = os.environ.get("MCP_IDENTITY_VAULT_B_FILE")
    if not (vault and fixture and a_file and b_file):
        pytest.skip("needs MCP_IDENTITY_BACKEND_VAULT_URL, MCP_IDENTITY_VAULT_FIXTURE_PATH "
                    "and MCP_IDENTITY_VAULT_A_FILE/_B_FILE")

    def view(token_file: str) -> dict:
        token = Path(token_file).read_text().strip()
        headers = {"X-Vault-Token": token}
        lookup = httpx.get(f"{vault.rstrip('/')}/v1/auth/token/lookup-self",
                           headers=headers, timeout=20.0)
        caps = httpx.get(f"{vault.rstrip('/')}/v1/sys/capabilities-self", headers=headers,
                         json={"path": fixture, "token": token}, timeout=20.0)
        assert lookup.status_code == 200, f"lookup-self -> {lookup.status_code}"
        assert caps.status_code == 200, f"capabilities-self -> {caps.status_code}"
        data = lookup.json()["data"]
        return {"policies": sorted(data.get("policies", [])), "type": data.get("type"),
                "display_name": data.get("display_name"),
                "capabilities": sorted(caps.json()["data"]["capabilities"][fixture])}

    a, b = view(a_file), view(b_file)
    assert a["policies"] != b["policies"], "same policy set for both identities"
    assert a["capabilities"] != b["capabilities"], "same capabilities on the fixture path"
    report = redact(str(a) + str(b))
    for token_file in (a_file, b_file):
        assert Path(token_file).read_text().strip() not in report, (
            "a Vault token leaked into the comparison output")


def test_backend_address_and_tls_override_are_not_caller_controlled(mcp_url,
                                                                    context_a) -> None:
    """A caller cannot point the gateway at another backend or disable its TLS.

    Override shapes tried as query parameters, each observed against a request
    the gateway must answer as itself: a 5xx would mean the parameter reached the
    routing/transport layer. Proving the override never arrived at the backend
    needs the backend's own record of THAT request, so the suite also requires a
    request log it can attribute by correlation id; without it, the negative is
    reported as pending instead of claimed.
    """
    bearer = context_a.token()
    correlation = "identity-ab-" + os.urandom(6).hex()
    probes = ({"backend": "http://127.0.0.1:1"}, {"tls_skip_verify": "true"},
              {"upstream": "https://attacker.invalid/mcp"})
    for query in probes:
        status, _ = mcp_initialize(mcp_url, bearer, query=query,
                                   extra_headers={"X-Request-Id": correlation})
        assert status < 500, (
            f"gateway returned {status} for {query}: an override parameter reached the "
            "routing/transport layer")

    evidence = (os.environ.get("MCP_IDENTITY_EVIDENCE_DIR") or "").strip()
    if not evidence:
        pytest.skip("needs MCP_IDENTITY_EVIDENCE_DIR containing <target>.log with the "
                    f"backend's own record of correlation id {correlation!r}: without a "
                    "per-request backend log this suite cannot claim the override never "
                    "reached the backend")
    log = Path(evidence) / "override.log"
    if not log.is_file():
        pytest.skip(f"{log} is absent: no backend request log to attribute the probes to")
    text = log.read_text(encoding="utf-8")
    if correlation not in text:
        pytest.skip("the backend log does not contain the probe correlation id: the log is "
                    "not from this run, so nothing here can be attributed to these requests")
    for injected in ("127.0.0.1:1", "attacker.invalid", "tls_skip_verify", "upstream="):
        assert injected not in text, f"backend log contains {injected!r}: override reached the backend"


def test_context_files_are_mode_0600_or_less(contexts) -> None:
    """Every live context's credential must be private on disk (0600 or 0400)."""
    for ctx in contexts:
        mode = stat.S_IMODE(ctx.token_file.stat().st_mode)
        assert not mode & 0o077, f"{ctx.name}: mode {oct(mode)}"
        assert mode in (0o400, 0o600), f"{ctx.name}: unexpected mode {oct(mode)}"


# ---------------------------------------------------------------------------
# SUPPLEMENTARY WRITE TIER - NOT part of core parity.
# ---------------------------------------------------------------------------

def test_optional_write_rbac_difference_not_required() -> None:
    """Optional extra: a write that one account may perform and the other may not.

    Labelled not-necessary because read-only allow/deny (above) already proves a
    permission difference, and this test mutates a live backend. It runs only
    against an isolated, disposable fixture the owner created for exactly this
    purpose, and only after an explicit opt-in - no ad-hoc remote mutation, ever.
    """
    if os.environ.get("MCP_IDENTITY_ALLOW_BACKEND_WRITES") != "1":
        pytest.skip("write tier is OFF by default and not required for parity: set "
                    "MCP_IDENTITY_ALLOW_BACKEND_WRITES=1 only together with an isolated "
                    "disposable folder the owner created for this")
    grafana = (os.environ.get("MCP_IDENTITY_BACKEND_GRAFANA_URL") or "").strip()
    folder = (os.environ.get("MCP_IDENTITY_GRAFANA_WRITE_FOLDER") or "").strip()
    a_file = os.environ.get("MCP_IDENTITY_GRAFANA_A_FILE")
    b_file = os.environ.get("MCP_IDENTITY_GRAFANA_B_FILE")
    if not (grafana and folder and a_file and b_file):
        pytest.skip("needs MCP_IDENTITY_GRAFANA_WRITE_FOLDER (throwaway) plus both tokens")

    def write(token_file: str) -> int:
        token = Path(token_file).read_text().strip()
        resp = httpx.post(f"{grafana.rstrip('/')}/api/dashboards/db",
                          headers={"Authorization": f"Bearer {token}"},
                          json={"folderUid": folder, "overwrite": False,
                                "dashboard": {"title": "identity-ab-probe",
                                              "uid": "identity-ab-probe",
                                              "schemaVersion": 39, "panels": []}},
                          timeout=20.0)
        return resp.status_code

    low, high = sorted([write(a_file), write(b_file)])
    assert low != high, "both accounts write identically: no RBAC difference to show"
    assert high in (200, 409), f"expected exactly one allowed write, got {low}/{high}"
