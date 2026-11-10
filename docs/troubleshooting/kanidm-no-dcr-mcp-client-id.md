# Kanidm has no dynamic client registration for MCP clients

## Problem

A native MCP client's OAuth flow fails against Kanidm expecting a `client_id`. The client tried
client-id metadata documents (CIMD) and dynamic client registration (DCR) in sequence, both
fell through, and the authorization request was never issued.

## Root Cause

Kanidm supports **neither** dynamic client registration nor client-id metadata documents. The
authorization-server metadata document at
`https://idm.grigri.cloud/oauth2/openid/mcp-gateway/.well-known/oauth-authorization-server`
omits both fields:

- `registration_endpoint`: absent
- `client_id_metadata_document_supported`: absent

The only supported grant types are `authorization_code` and
`urn:ietf:params:oauth:grant-type:token-exchange`. The only supported code challenge method is
`S256`. The only supported token endpoint auth methods are `client_secret_basic` and
`client_secret_post`, but the MCP gateway client is `public: true` (PKCE only, no secret).

General rule: with Kanidm, every OAuth client must be pre-registered in git via
`KanidmOAuth2Client` and its `client_id` supplied to the client out of band. There is no dynamic
path.

## How to Diagnose

```bash
curl -s https://idm.grigri.cloud/oauth2/openid/mcp-gateway/.well-known/oauth-authorization-server \
  | jq '{registration_endpoint, client_id_metadata_document_supported, grant_types_supported}'
```

Both `registration_endpoint` and `client_id_metadata_document_supported` must be `null` for this
to be the cause. If either is present, the client should have used it and the failure is elsewhere.

## Fix / Workaround

### For OpenCode (human operator, browser-based)

The pre-registered client is `mcp-gateway`. Because the `KanidmOAuth2Client` sets
`preferShortUsername: true`, the short name **is** the `client_id`. Each OpenCode server's
`oauth` block must therefore include:

```json
{
  "client_id": "mcp-gateway",
  "redirect_uri": "http://127.0.0.1:15000/callback",
  "scope": "<the single scope the route's RFC 9728 document advertises>",
  "callback_port": 15000
}
```

Omit `client_secret` (public client). Keep one scope per server; a client that requests a union
of scopes is refused wholesale unless the caller is a member of every corresponding group. After
editing the config, clear any cached credentials and re-authorise:

```bash
opencode mcp logout <server>
```

OpenCode's client-identification precedence (see "Client registration" and "Pre-registered
clients" at <https://opencode.ai/docs/mcp-servers/>): pre-registered client when
`oauth.client_id` is configured, else CIMD when the AS supports it and `oauth.redirect_uri` is
unset, else DCR. Setting `client_id` and `redirect_uri` short-circuits the fallback chain.

### For in-cluster agents (Hermes, the reference implementation)

Hermes avoids the problem entirely because the client is pre-registered declaratively in git and
tokens are obtained with the grant Kanidm **does** support:

- Token exchange: `apps/hermes/values.yaml:83-91` (`exchange.url`, `clientId: mcp-gateway`,
  `audience: mcp-gateway`) and `apps/hermes/files/mcp_gateway_token.py:123-131` (grant_type
  `urn:ietf:params:oauth:grant-type:token-exchange`, client_id, audience, scope, subject_token,
  subject_token_type `access_token`). The subject token is the Kanidm service-account API token
  read from the Secret named by `identity.subjectTokenSecret`, passed in process memory only,
  never in argv. No refresh token is ever written.
- Pre-seeded RFC 7591 registration record: `apps/hermes/files/mcp_gateway_token.py:196-212`
  writes `<server>.client.json` (`client_id: mcp-gateway`, `client_name`, `grant_types:
  [authorization_code, refresh_token]`, `response_types: [code]`,
  `token_endpoint_auth_method: none`) driven by `oauthClientId` in `values.yaml:101`. That is a
  pre-seeded registration record, so the MCP client never attempts DCR. It refuses to write
  through a symlink.

## Prevention

Any new MCP client that talks to this Kanidm instance must be given the `client_id` out of band.
Do not assume DCR or CIMD will work; the authorization-server metadata document is the source of
truth and both fields are absent.
