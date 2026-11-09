"""Fixtures for the two-identity (A/B) acceptance suite.

No framework: this file only locates the credentials the operator already has
and refuses to run anything when they are missing. Credential rules live in
``contexts.py``. Nothing here creates, edits or deploys a user, group, role,
policy or gateway route - the suite observes an existing setup or skips.

OFFLINE tier: always runs, needs no cluster and no secrets.

LIVE tier prerequisites (each live test names the exact missing item when it
skips, so a green-looking run can never be a silent no-op):

``MCP_IDENTITY_GATEWAY_URL``
    Base URL of the gateway plane the route under test is actually served on. Must
    be TLS: caller tokens are bearers. Which plane a backend sits on is the gateway
    agent's decision and is still moving; as configured today, ``apps/mcp-gateway``
    serves ``https://mcp.grigri.cloud`` (firecrawl) and ``apps/mcp-gateway-admin``
    declares ``https://mcp.internal.grigri.cloud`` resources for its grafana/vault
    backends. This suite reports the plane it is pointed at - it never moves a
    route between planes and never assumes one, so an operator reads the value from
    the deployed gateway config instead of from this file. The pair a run is measured
    against is resolved from the target's plane (see ``MCP_IDENTITY_TARGET``), and
    ``test_backends_are_declared_on_their_approved_plane`` currently fails on
    Grafana's admin-plane declaration: that failure is the pending chart correction,
    not a harness defect, and it must not be silenced.
``MCP_IDENTITY_TARGET``
    Route under test; shape is ``<base>/<target>/mcp``, per target, and the
    gateway has no aggregate ``/mcp`` route. The target ALSO selects the identity
    pair the run is measured against, because each plane is its own Kanidm OAuth2
    client: ``firecrawl`` and ``grafana`` are public-plane, ``vault`` is admin-plane
    (``TARGET_PLANE`` in the test module - an unlisted target is an error, never an
    implicit "public"). Enabling state and placement are the gateway charts' -
    ``firecrawl`` is currently off and its private-destination reachability was
    never verified (the egress NetworkPolicy change caused a probe-timeout health
    regression and was reverted), so a live run there is not available regardless of
    this suite. ``test_backends_are_declared_on_their_approved_plane`` checks each
    backend is declared in the chart of the plane the approved architecture puts it
    on; it is RED while Grafana is declared on the admin plane, and this suite never
    adapts to that placement, because doing so would certify an unapproved move.
``MCP_IDENTITY_CALLER_A_FILE`` / ``MCP_IDENTITY_CALLER_B_FILE``
    Paths (Secret-volume mounted) to two caller ACCESS tokens belonging to two
    *human* Kanidm accounts. Not the ``hermes-mcp``-style workload service accounts:
    those exist for Hermes' own token renewal, two identical workloads could not
    show a permission difference, and no test here may create a person or change a
    membership to obtain one. Both tokens must be minted for the OAuth2 client of the
    plane the target is on (``MCP_IDENTITY_TARGET``), because Kanidm puts that
    *client name* in ``aud`` (see ``MCP_IDENTITY_AUDIENCE``). They must be ACCESS
    tokens, never ID tokens: the
    gateway edge authenticates access tokens on every route, and a route whose
    *backend* expects an ID token is a separate credential at a separate hop, not
    licence to present one here (``edge_bearer_must_be_an_access_token`` refuses).
    The two accounts must BOTH already hold the route scope (``MCP_IDENTITY_ROUTE_SCOPE``)
    and differ in the BACKEND's own native RBAC. A pair that differs only by a
    Kanidm group is a scope-gate case, not parity: the ungranted one is refused at
    the edge and nothing about backend permissions is ever measured - that negative
    belongs to ``MCP_IDENTITY_CALLER_UNGRANTED_FILE`` below. Person membership is
    manual and never edited in git (``docs/user-guide/kanidm.md``).
``MCP_IDENTITY_ROUTE_SCOPE``
    The scope the route under test requires, per target: ``mcp_firecrawl`` /
    ``mcp_grafana`` on the public plane, ``mcp_vault`` on the admin plane (each
    plane chart's ``backends.<target>.scope``). Required by the human A/B gate so it
    can prove both callers hold it, and by the scope-gate negative so that test knows
    what to exclude. Note that both plane charts ship ``scopeMap: []`` and
    ``humanScopeMap: []`` today, so NO account holds any route scope yet: the live
    gate stays pending until the owner names the groups.
``MCP_IDENTITY_CALLER_UNGRANTED_FILE``
    Optional: one real human ACCESS token that does NOT hold
    ``MCP_IDENTITY_ROUTE_SCOPE``. Used only by the separate scope-gate negative
    test, whose result must never be reported as an A/B permission difference.
``MCP_IDENTITY_AUDIENCE`` / ``MCP_IDENTITY_ISSUER`` (normally NOT set)
    The expected pair is resolved per target from the plane's own chart
    (``apps/mcp-gateway/values.yaml`` ``auth.issuer`` / ``auth.audience`` for
    firecrawl and grafana, ``apps/mcp-gateway-admin/values.yaml`` for vault),
    because Kanidm issues one issuer/audience pair per OAuth2 *client* and each plane
    is its own client. Set them only as a complete pair, and only to match that
    plane's chart: one of them alone (a public issuer with an admin audience, or the
    reverse) describes a combination Kanidm cannot issue, and a pair belonging to the
    other plane makes the run measure something the gateway never validates - both
    are refused with an explicit failure rather than silently tested.
    Kanidm's public-plane pair is ``mcp-gateway`` /
    ``https://idm.grigri.cloud/oauth2/openid/mcp-gateway``; the admin plane's is
    ``mcp-gateway-admin`` / ``…/oauth2/openid/mcp-gateway-admin``. ``aud`` is the
    client NAME, deliberately independent of the gateway's TLS host, and ``iss`` is
    compared with ``==``, never as a host suffix or prefix: the bare IdM root
    ``https://idm.grigri.cloud`` is a real Kanidm issuer but signs nothing for either
    gateway client, and a sibling client's issuer (``…/openid/grafana``, or the other
    plane's client) is equally wrong for a given route. The jwksUrl is the same
    client's ``public_key.jwk``.
``MCP_IDENTITY_PROBE_TOOL`` + ``MCP_IDENTITY_PROBE_ARG_KEY`` (default ``path``)
    + ``MCP_IDENTITY_PROBE_ARG_A_ONLY`` + ``MCP_IDENTITY_PROBE_ARG_B_ONLY``
    The gateway-mediated parity gate: one read-only backend tool, plus one
    fixture only A may read and one only B may read. Without these, permission
    parity is PENDING: the ``test_baseline_*`` readouts call the backend
    directly and therefore prove only that the two accounts differ, never which
    identity the gateway acts as.
``MCP_IDENTITY_BACKEND_GRAFANA_URL`` + ``MCP_IDENTITY_GRAFANA_A_FILE`` /
``MCP_IDENTITY_GRAFANA_B_FILE`` + ``MCP_IDENTITY_GRAFANA_A_ONLY_UID`` /
``MCP_IDENTITY_GRAFANA_B_ONLY_UID``
    Grafana's own API: two native tokens for the baseline identity check, and
    two dashboards only the matching account may read for the baseline RBAC
    difference (a read, so no write fixture is needed).
``MCP_IDENTITY_BACKEND_VAULT_URL`` + ``MCP_IDENTITY_VAULT_A_FILE`` /
``MCP_IDENTITY_VAULT_B_FILE`` + ``MCP_IDENTITY_VAULT_FIXTURE_PATH``
    Vault's own API: the same two native credentials also serve as the caller's
    own ``X-Vault-Token`` for the header-vs-fallback precedence test (the
    legitimate zero-broker path: OIDC login -> that user's own Vault token).
``MCP_IDENTITY_EVIDENCE_DIR``
    Directory holding the backend's own request log (``override.log``) so the
    address/TLS-override probes can be attributed per request by correlation id.
    Without it, that test reports pending instead of claiming a negative.
``MCP_IDENTITY_ALLOW_BACKEND_WRITES``
    Not required for parity, and off by default. Set to ``1`` only together with
    ``MCP_IDENTITY_GRAFANA_WRITE_FOLDER`` pointing at a disposable folder the
    owner created for this purpose - no ad-hoc remote mutation.
"""

from __future__ import annotations

import os

import pytest

from contexts import Context, load_context

__all__ = ["Context", "load_context"]


@pytest.fixture(scope="session")
def gateway_url() -> str:
    url = (os.environ.get("MCP_IDENTITY_GATEWAY_URL") or "").strip().rstrip("/")
    if not url:
        pytest.skip("MCP_IDENTITY_GATEWAY_URL is not set: no deployed gateway to test")
    if not url.startswith("https://"):
        pytest.fail(f"MCP_IDENTITY_GATEWAY_URL must be TLS (caller tokens are bearers): {url}")
    return url


@pytest.fixture(scope="session")
def target() -> str:
    """Which per-target route to exercise, and therefore which plane to use."""
    name = (os.environ.get("MCP_IDENTITY_TARGET") or "").strip().lower()
    if not name:
        pytest.skip("MCP_IDENTITY_TARGET is not set (firecrawl | grafana | vault)")
    if name not in ("firecrawl", "grafana", "vault"):
        pytest.fail(f"unknown target {name!r}: the gateway exposes one route per backend")
    return name


@pytest.fixture(scope="session")
def mcp_url(gateway_url: str, target: str) -> str:
    return f"{gateway_url}/{target}/mcp"


@pytest.fixture(scope="session")
def context_a() -> Context:
    return load_context("MCP_IDENTITY_CALLER_A_FILE", "A")


@pytest.fixture(scope="session")
def context_b() -> Context:
    return load_context("MCP_IDENTITY_CALLER_B_FILE", "B")


@pytest.fixture(scope="session")
def contexts(context_a: Context, context_b: Context) -> tuple[Context, Context]:
    """Two caller contexts, plus proof they are not the same credential."""
    if context_a.token_file == context_b.token_file:
        pytest.fail("contexts A and B point at the same token file: that is one identity, not two")
    return context_a, context_b
