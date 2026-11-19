# MCP gateway OAuth re-authentication (refresh token expiry)

## Problem

opencode reports its MCP servers behind `https://mcp.grigri.cloud` (grafana-grigri, context7,
firecrawl) as `needs_auth` after the machine has been idle for a day. The OAuth refresh request to
Kanidm fails with `invalid_grant`, and the user has to repeat the browser login. Observed after
about 22.7 h idle.

## Root Cause

Kanidm's default OAuth2 refresh-token expiry is 16 h. The expiry is sliding (each refresh issues a
new token with a fresh `iat` + expiry), so only an idle period longer than the expiry breaks the
chain.

A refresh is additionally bound to the parent Kanidm session: if that session no longer exists or
was revoked, the refresh fails regardless of the token lifetime.

## How to Diagnose

- opencode shows the MCP server as `needs_auth`; its log contains the refresh failure
  `invalid_grant`.
- Check the live value on the Kaniop resource:
  `kubectl --context=grigri -n mcp-gateway get kanidmoauth2client mcp-gateway -o jsonpath='{.spec.refreshTokenExpiry}'`
  (empty means Kanidm's 16 h default).
- If the value is already large and refresh still fails, suspect the parent Kanidm session
  (expired or revoked), not the refresh token.

## Fix / Workaround

Set `kanidm.oauth2Client.refreshTokenExpiry` in `apps/mcp-gateway/values.yaml`; the template renders
it as `spec.refreshTokenExpiry` on the `KanidmOAuth2Client`. The attribute
(`oauth2_refresh_token_expiry`) is a Uint32; its maximum was accepted by Kanidm 1.11.2 but is
unnecessarily long for a stolen token. The chosen value is `31536000` seconds (1 year).
The template pipes the value through `int64`; without it Helm renders large numbers in scientific
notation.

Trade-off: a stolen refresh token stays valid as long as it keeps being used, or until the parent
Kanidm session is revoked or expires. Revoke the Kanidm session to cut off a compromised client.

### The consent "Proceed" page stays

The consent page cannot be removed for this client. `mcp-gateway` is a public client with a
localhost redirect, and Kanidm (`oauth2.rs`) computes:

```
consent_required = (!previously_granted || (!is_basic && loopback_uri_matched)) && enable_consent_prompt
```

`enable_consent_prompt` is always true for a Public client, and a loopback redirect always forces
the prompt (RFC 6819 guidance for native clients). A full re-auth therefore always shows
"Proceed"; the longer refresh expiry only makes full re-auths rare.

## Verification

- **Config applied:** the `KanidmOAuth2Client` (namespace `mcp-gateway`) has the Kaniop condition
  `RefreshTokenExpiryUpdated: AttributeMatch`.
- **Stored value:** `kanidm system oauth2 get mcp-gateway -D idm_admin | grep refresh` shows
  `oauth2_refresh_token_expiry` (login: [Agent CLI login](../user-guide/kanidm.md#agent-cli-login)).
  Kanidm 1.11.2 accepted the Uint32 maximum `4294967295` and stored it as-is, before the value was
  lowered to `31536000`.
- **Only new tokens benefit:** the lifetime is fixed when a token is issued, so each client must
  re-authenticate once after the change.
- **End-to-end proof:** opencode's log (`~/.local/share/opencode/log/opencode.log`) logs per server
  `mcp oauth request succeeded`, `invalid_grant` or `credential invalidated`. A successful refresh
  after more than 16 h idle shows the fix works.
- **Not checkable by an agent:** the owner's OAuth2 sessions are invisible to the admin accounts
  (see [Agent CLI login](../user-guide/kanidm.md#agent-cli-login)).
- **Renaming a server orphans its credential:** opencode keys stored credentials per MCP server
  name, so renaming a server in `opencode.json` leaves the old credential behind (the old
  `homelab-*` labels remained) and forces a re-auth.
