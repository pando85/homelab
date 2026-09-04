# Readest Android OAuth (Deep-Link Login)

Getting the official Readest Android APK to log in against the self-hosted stack. The web flow
already worked (see `docs/deployment/readest.md`); the mobile app adds two hard constraints:

1. **The APK bundles its own minified JS** — the init-container patching only affects the web
   image. The APK always sends `provider=discord`.
2. **The app's WebView is cross-origin** with the API (Tauri apps load from `tauri.localhost`,
   not from `readest.grigri.cloud`), so every supabase-js call is subject to CORS preflight.

## Symptom Progression

| Phase | Symptom | Cause |
|---|---|---|
| 1 | App opens browser, login completes, app stays in browser | `redirect_to=readest://auth-callback` dropped by the nginx provider rewrite |
| 2 | Same as 1 | GoTrue rejected `readest://auth-callback` (not in `GOTRUE_URI_ALLOW_LIST`) |
| 3 | App returns from browser but shows "go to login" | CORS preflight blocked `setSession` → `/auth/v1/user` call (missing `apikey` header) |

## The Full OAuth Flow (Android)

```
1. App (WebView):   GET /auth/v1/authorize?provider=discord&redirect_to=readest://auth-callback
2. nginx:           302 → /auth/v1/authorize?provider=custom:kanidm&redirect_to=readest://auth-callback
3. GoTrue:          302 → Kanidm (redirect_uri=https://readest.grigri.cloud/callback,
                            passes redirect_to through)
4. Kanidm:          user authenticates → 302 → /callback?code=...
5. nginx:           301 /callback → /auth/v1/callback
6. GoTrue:          exchanges code, 302 → readest://auth-callback#access_token=...&refresh_token=...
7. Android:         intent → NativeBridgePlugin resolves pending authWithCustomTab invoke
8. App (WebView):   supabase.auth.setSession(...) → GET /auth/v1/user  ← CORS preflight here
9. App:             session persisted, navigate to library
```

Steps 1–7 worked after the nginx/GoTrue config fixes. Step 8 was the silent killer: the preflight
failed, so no request ever reached GoTrue, and the app concluded there was no session.

## Root Causes

### 1. Nginx provider rewrite dropped `redirect_to`

The original `configuration-snippet` replaced the whole query string with
`provider=custom:kanidm`, discarding the app's `redirect_to=readest://auth-callback`. GoTrue then
fell back to `GOTRUE_SITE_URL` and redirected the browser to the web UI after login.

Pitfalls hit while fixing this (nginx "if is evil"):

- `map` is only valid in `http` context — cannot be used in ingress snippets.
- Nested `if` blocks are not allowed; combine conditions via flag variables and a "gate" string.
- `rewrite ... last` on the authorize URI re-enters the same location → rewrite loop (500).
  Use `return 302` instead.
- Variables set inside an `if` that doesn't match are empty — default them first
  (`set $readest_redirect_to "..."` before the `if`).

Working snippet (on **both** internal and external API ingresses):

```nginx
set $readest_redirect_to "https://readest.grigri.cloud/auth/callback";
if ($arg_redirect_to != "") {
  set $readest_redirect_to $arg_redirect_to;
}
set $readest_authorize "no";
if ($uri ~* "^/auth/v1/authorize$") {
  set $readest_authorize "yes";
}
set $readest_rewrite "no";
if ($args ~* "provider=(google|apple|github|discord)") {
  set $readest_rewrite "yes";
}
if ($args = "") {
  set $readest_rewrite "yes";
}
set $readest_gate "$readest_authorize:$readest_rewrite";
if ($readest_gate = "yes:yes") {
  return 302 $uri?provider=custom:kanidm&redirect_to=$readest_redirect_to;
}
```

### 2. `GOTRUE_URI_ALLOW_LIST` must include the custom scheme

GoTrue silently ignores `redirect_to` values that don't match `GOTRUE_URI_ALLOW_LIST` and falls
back to `GOTRUE_SITE_URL`. Add the deep-link target:

```yaml
GOTRUE_URI_ALLOW_LIST: https://readest.grigri.cloud/**,https://readest.internal.grigri.cloud/**,readest://auth-callback
```

### 3. GoTrue CORS does not allow the `apikey` header (the hard one)

supabase-js sends `apikey: <anon-key>` on **every** request. GoTrue's built-in CORS middleware
(`rs/cors`, `internal/api/api.go`) only allows:

```
Accept, Authorization, Content-Type, X-Client-IP, X-Client-Info, X-JWT-AUD, x-use-cookie, X-Supabase-Api-Version
```

A preflight that includes `apikey` gets a 204 with **no** `Access-Control-Allow-*` headers → the
browser blocks the actual request. On Supabase Cloud Kong sits in front and handles this; when you
expose GoTrue directly through ingress-nginx, cross-origin clients break.

Why the web app worked: it is served from the same origin as the API → no preflight. The Tauri
WebView (origin `tauri.localhost` / `https://tauri.localhost`) is cross-origin, so after the deep
link delivered the tokens, `supabase.auth.setSession()` → `GET /auth/v1/user` died at preflight.
`setSession` **always** makes a network call (`_getUser` when the token is valid, refresh grant
when expired) — the absence of that request in GoTrue logs is the diagnostic tell.

Fix (merged with the defaults via `AllAllowedHeaders` in gotrue conf):

```yaml
GOTRUE_CORS_ALLOWED_HEADERS: apikey
```

## How to Diagnose

Verify the redirect chain end-to-end with curl (no real login needed until the last hop):

```bash
# Step 2: nginx rewrite preserves redirect_to
curl -sI "https://readest.grigri.cloud/auth/v1/authorize?provider=discord&redirect_to=readest://auth-callback" | grep -i location

# Step 3: GoTrue → Kanidm passes redirect_to
curl -s -D - -o /dev/null "https://readest.grigri.cloud/auth/v1/authorize?provider=custom:kanidm&redirect_to=readest://auth-callback" | grep -i location

# Step 5: /callback routes to GoTrue
curl -sI "https://readest.grigri.cloud/callback?code=x&state=y" | grep -i location

# Step 8: CORS preflight as the WebView would send it
curl -s -D - -o /dev/null -X OPTIONS "https://readest.grigri.cloud/auth/v1/user" \
  -H "Origin: https://tauri.localhost" \
  -H "Access-Control-Request-Method: GET" \
  -H "Access-Control-Request-Headers: apikey,authorization,x-client-info" | grep -i access-control
# FAIL: 204 with no access-control-* headers  /  OK: allow-headers includes apikey
```

Android side (`adb logcat`, tag `NativeBridgePlugin`):

- `Launching OAuth URL: ...` — what the app sends (shows hardcoded `provider=discord`).
- `Received intent: ... data=readest://auth-callback#access_token=...` — deep link arrived with
  tokens in the fragment. If this line exists but the session still fails, the problem is in the
  WebView JS (CORS, storage) — not in intent delivery.

Server side:

- GoTrue logs every request with `remote_addr`. List the paths a device IP called:
  `kubectl logs deploy/readest-auth | grep <ip> | grep -oE '"path":"[^"]*"' | sort | uniq -c`.
  The device calling only `/authorize` + `/callback` and never `/token` or `/user` after login
  means the client-side session call is being blocked (CORS) or never issued.
- A token from an intercepted deep link can be replayed to prove the server side accepts it:
  `curl -H "Authorization: Bearer <access_token>" https://readest.grigri.cloud/auth/v1/user`.

## Known Limitations / Follow-ups

- **JWT claims nesting:** our GoTrue puts `plan`, `storage_quota`, `translation_quota` inside
  `user_metadata` (inherited from Kanidm claims), not as top-level JWT claims. The app's
  `getUserProfilePlan()` reads the top-level `plan` claim, so mobile users resolve as "free"
  (redirected to `/user` after login; premium-gated UI may be limited). Server-side quotas are
  still enforced via `STORAGE_FIXED_QUOTA`/`TRANSLATION_FIXED_QUOTA`. A GoTrue custom access-token
  hook (or upstream change) would fix it.
- **`provider_refresh_token` leakage:** GoTrue passes Kanidm's refresh token through the URL
  fragment to the app. Harmless here (self-hosted, HTTPS, single user) but worth knowing.
- **Manual DB row:** the `custom:kanidm` provider lives in `auth.custom_oauth_providers`
  (inserted manually due to SSRF validation) — re-insert if the database is recreated.

## References

- `apps/readest/values.yaml` — nginx snippets, GoTrue env (`GOTRUE_URI_ALLOW_LIST`,
  `GOTRUE_CORS_ALLOWED_HEADERS`)
- `docs/deployment/readest.md` — full deployment incl. web OAuth integration
- `docs/troubleshooting/gotrue-callback-url-routing.md` — `/callback` → `/auth/v1/callback` redirect
- `docs/troubleshooting/gotrue-ssrf-protection.md` — custom provider manual DB insert
- gotrue source: `internal/api/api.go` (CORS options), `internal/conf/configuration.go`
  (`CORS.AllowedHeaders` → `GOTRUE_CORS_ALLOWED_HEADERS`)
- readest app source: `src/app/auth/page.tsx` (`authWithCustomTab`, `handleOAuthUrl`),
  `src/helpers/auth.ts` (`parseOAuthCallbackUrl`, `setSession` flow)
