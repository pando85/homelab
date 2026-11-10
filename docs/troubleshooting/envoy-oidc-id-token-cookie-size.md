# Envoy OIDC session breaks: ID token cookie over 4096 B

## Problem

After a successful Kanidm login through an Envoy Gateway OIDC `SecurityPolicy`, the first page
loads, but its assets and API calls fail: the browser console shows CORS errors
(Chromium `MissingAllowOriginHeader`, Firefox `NS_ERROR_*` network errors) on requests that were
redirected to `https://idm.grigri.cloud/ui/oauth2`. Reloads keep failing, sometimes the whole
session ends and Kanidm asks for a login again. It happens in every browser.

## Root Cause

Envoy's OAuth2 filter keeps the session in cookies on the app host: `oidc-access-token`,
`oidc-id-token`, `RefreshToken-*`, `OauthHMAC-*` and `OauthExpires-*`. The token cookies hold the
whole token, encrypted (base64 of an AES ciphertext, larger than the JWT itself).

Browsers drop any cookie whose name plus value exceeds 4096 B, silently (Chromium blocked reason
`NameValuePairExceedsMaxSize`). With the `groups` scope, Kanidm puts every group of the person in
the ID token; for the owner's account that made `oidc-id-token` 5680 B. The browser then has the
HMAC, expiry, access and refresh cookies but no ID token, so on every request:

1. Envoy's `jwt_authn` finds no ID token, the OAuth2 filter refreshes with the refresh token and
   sends all five cookies again (`Set-Cookie` on every response, the ID token dropped again).
2. Parallel asset requests all refresh with the same refresh token. Kanidm's refresh tokens are
   single use: a reused one is refused and can end the whole session.
3. The requests without a valid session get a 302 to Kanidm, which a `fetch`/XHR/module load
   cannot follow cross-origin: CORS error.

The `groups` scope only existed for the old oauth2-proxy per-Ingress group filtering.

## How to Diagnose

The cookie values are secrets: measure sizes only.

- Browser devtools, Network tab, the `/oauth2/callback` response: the `Set-Cookie` list shows
  `oidc-id-token` with a warning (Chromium: "exceeds the maximum size"), and later responses carry
  `Set-Cookie` again although nothing changed. Application tab: no `oidc-id-token` cookie.
- From a script over CDP (`Network.responseReceivedExtraInfo`): print each `Set-Cookie` name and
  `len(name=value)`, and the `blockedCookies` reasons; never the values.
- After the fix, the five cookies are set once, at the callback, and normal responses carry no
  `Set-Cookie` from Envoy.

The cookie is encrypted, so the JWT claims cannot be read from it. Check what the client asks for
instead: `oidc.scopes` of the policy, the client's `scopeMap`, and
`kanidm system oauth2 get <client> -D idm_admin` (`oauth2_rs_scope_map`, `oauth2_rs_claim_map`).

Measured on 2026-11-10 (Chromium, owner account), `oidc-id-token`:

| setup                                    | size             |
| ---------------------------------------- | ---------------- |
| `groups` scope, JWT rule on `groups`     | 5680 B (dropped) |
| no `groups`, claim map `access: [<app>]` | 993 to 1014 B    |

## Fix / Workaround

Never request the `groups` scope on an Envoy OIDC client. Each `KanidmOAuth2Client` has a small
claim map that gives members of `<app>-users` the claim `access: [<client name>]`
(`joinStrategy: array`), the scope maps and `oidc.scopes` are `[openid, profile, email]`, and the
`SecurityPolicy` authorizes on `access` (`valueType: StringArray`, value = the client name, so a
token of another client never matches). Kanidm emits claim maps in the ID token without any extra
scope. Recipe: `docs/conventions/gateway-api-routes.md` ("Authentication"). Commits `87d36c4cf`
(http-echo pilot) and `3056771f7` (the other OIDC policies).

Changing the requested scopes makes Kanidm ask for consent again once per app.

A web app manifest (`<link rel="manifest">`) is fetched without cookies, so it keeps getting the
302 to Kanidm and one CORS error per page load (bazarr, navidrome). That is unrelated to the cookie
size and harmless for the page.

When adding claims, keep `oidc-id-token` well below 4096 B: measure the callback's `Set-Cookie`
sizes after the change.
