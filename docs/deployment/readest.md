# Readest Deployment

Readest is an open-source ebook reader with a self-hosted stack. This deployment uses:
- **bjw-s `app-template`** with multiple controllers
- **Zalando Postgres operator** for the database
- **Shared MinIO** (`platform/minio/`) for S3 storage
- **kaniop** for OIDC integration with Kanidm
- **nginx ingress** for path-based routing (no Kong)

## Architecture

```
Browser
  └── readest.grigri.cloud
        ├── / ──────────────► client:3000 (Next.js)
        ├── /auth/v1/* ─────► auth:9999 (GoTrue)
        └── /rest/v1/* ─────► rest:3000 (PostgREST)

Internal:
  client ──► minio.minio.svc:9000 (S3, bucket: readest-files)
  auth   ──► readest-postgres (Zalando)
  rest   ──► readest-postgres (Zalando)
  auth   ──► idm.grigri.cloud (Kanidm OIDC via kaniop)
```

Single domain `readest.grigri.cloud` with nginx path-based routing. No separate API gateway (Kong) needed.

## Controllers

| Controller | Image | Port | Purpose |
|---|---|---|---|
| `client` | `ghcr.io/readest/readest:0.12.6` | 3000 | Next.js web app |
| `db-migrate` | init container (`postgres:17-alpine`) | — | Downloads and applies Readest DB schema at startup |
| `patch-oauth` | init container (same as client) | — | Patches JS bundles for Kanidm OAuth |
| `auth` | `supabase/gotrue:v2.196.0` | 9999 | Auth service (OIDC client) |
| `rest` | `postgrest/postgrest:v14.3` | 3000 | REST API (auto-generated from Postgres schema) |

No MinIO controller — uses shared `platform/minio/` instance.

## Database: Zalando Postgres + Automated Migrations

Readest's schema is tightly coupled to Supabase (uses `auth.users` FK, `auth.uid()` in RLS policies, Supabase roles). The solution:

1. **Zalando CR** creates the database, base roles, and `pgcrypto` extension
2. **Manual schema setup** (required because Zalando doesn't auto-create Supabase schemas):
   - Create `auth`, `storage`, `realtime`, `graphql_public` schemas
   - Create enum types: `auth.factor_type`, `auth.code_challenge_method`, `auth.one_time_token_type`
3. **GoTrue** runs migrations on first start to create tables in the `auth` schema
4. **`db-migrate` init container** automatically downloads and applies the Readest app schema:
   - Downloads `schema.sql` and all migrations from GitHub at `DB_SCHEMA_VERSION` (matches container image tag)
   - Creates required roles (`anon`, `authenticated`, `service_role`) and `extensions` schema
   - Grants `service_role` BYPASSRLS attribute (required for storage upload API to bypass RLS policies)
   - Grants permissions to `anon`, `authenticated`, and `service_role` on public tables and `auth` schema
   - Sets default privileges for future tables
   - Applies base schema + incremental migrations with idempotent ledger tracking (`readest_meta.migrations`)
   - Skips downloads if all migrations already applied (optimization)
   - Uses advisory lock for safe concurrent pod starts
   - Sends `NOTIFY pgrst, 'reload schema'` so PostgREST picks up new tables without restart

**Note:** The manual schema setup (step 2) is a one-time operation. If the database is recreated, these steps must be repeated. The `db-migrate` init container handles the Readest app tables automatically on every pod start.

### Schema Version Tracking

The `DB_SCHEMA_VERSION` env var in the `db-migrate` init container must match the Readest container image tag. When Renovate bumps the image tag, also update `DB_SCHEMA_VERSION` to ensure the correct migrations are downloaded.

### Zalando CR

```yaml
apiVersion: acid.zalan.do/v1
kind: postgresql
metadata:
  name: readest-postgres
  labels:
    backup/retain: weekly
spec:
  teamId: readest
  numberOfInstances: 1
  resources:
    requests: { cpu: 10m, memory: 128Mi }
    limits:    { memory: 256Mi }
  volume:
    size: 5Gi
  users:
    readest: [superuser, createdb]
    supabase_auth_admin: []
    authenticator: []
    pgbouncer: []
    supabase_storage_admin: []
  databases:
    readest: readest
  preparedDatabases:
    readest:
      extensions:
        pgcrypto: public
  postgresql:
    version: "17"
    parameters:
      archive_mode: "off"
      max_connections: "25"
      shared_buffers: 32MB
      full_page_writes: "off"
      # ZFS-tuned logging disabled
```

## OIDC Integration (kaniop)

Readest uses GoTrue as the auth service, which supports external OAuth providers. We integrate with Kanidm via OIDC.

### KanidmGroup

```yaml
apiVersion: kaniop.rs/v1beta1
kind: KanidmGroup
metadata:
  name: readest-users
spec:
  kanidmRef: { name: kanidm, namespace: kanidm }
```

### KanidmOAuth2Client

```yaml
apiVersion: kaniop.rs/v1beta1
kind: KanidmOAuth2Client
metadata:
  name: readest
spec:
  kanidmRef: { name: kanidm, namespace: kanidm }
  displayname: readest
  origin: https://readest.grigri.cloud/
  redirectUrl:
    - https://readest.grigri.cloud/auth/v1/callback
  scopeMap:
    - group: readest-users
      scopes: [openid, profile, email]
  preferShortUsername: true
  strictRedirectUrl: true
```

### GoTrue OIDC Env Vars

```yaml
GOTRUE_EXTERNAL_OIDC_ENABLED: "true"
GOTRUE_EXTERNAL_OIDC_CLIENT_ID:
  valueFrom:
    secretKeyRef:
      name: readest-kanidm-oauth2-credentials
      key: CLIENT_ID
GOTRUE_EXTERNAL_OIDC_SECRET:
  valueFrom:
    secretKeyRef:
      name: readest-kanidm-oauth2-credentials
      key: CLIENT_SECRET
GOTRUE_EXTERNAL_OIDC_URL: https://idm.grigri.cloud/oauth2/openid/readest
```

### Kanidm Integration

**Status: Working.**

The Readest frontend hardcodes OAuth provider buttons (Google, Apple, GitHub, Discord). To use Kanidm as the OAuth provider, we implemented a multi-layered solution:

1. **Init Container**: Patches the JavaScript bundles at pod startup to:
   - Change `provider:"discord"` to `provider:"custom:kanidm"`
   - Change button label from "Discord" to "Kanidm"
   - Remove Google, Apple, and GitHub buttons from the UI

2. **Nginx Rewrite**: Added `configuration-snippet` to the API ingress that rewrites the `provider` parameter from any hardcoded value to `custom:kanidm`. This ensures the backend always uses our custom Kanidm provider.

3. **Nginx Redirect**: Added `server-snippet` to the client ingress that redirects `/callback` to `/auth/v1/callback`. This routes Kanidm's OAuth callback to GoTrue's callback endpoint.

4. **Custom OAuth Provider in GoTrue**: Manually inserted the custom OAuth provider into the `auth.custom_oauth_providers` table, bypassing GoTrue's SSRF protection that blocks private IPs. The provider is configured with:
   - `identifier`: `custom:kanidm`
   - `provider_type`: `oauth2`
   - `authorization_url`: `https://idm.grigri.cloud/ui/oauth2`
   - `token_url`: `https://idm.grigri.cloud/oauth2/token`
   - `userinfo_url`: `https://idm.grigri.cloud/oauth2/openid/readest/userinfo`

5. **Kanidm OAuth2 Client**: Updated the redirect URLs to include both internal and external domains, and both `/callback` and `/auth/v1/callback` paths. This ensures GoTrue can redirect back to the correct URL after authentication.

**Known Issues:**
- The custom OAuth provider must be manually inserted into the database (not managed by GitOps)
- GoTrue's SSRF protection prevents registering providers pointing to internal services via the API

**OAuth Flow:**
1. User clicks "Sign in with Kanidm" in Readest
2. Init container has already patched the UI to use `provider:"custom:kanidm"`
3. Nginx rewrites any provider parameter to `custom:kanidm` (belt and suspenders)
4. GoTrue redirects to Kanidm's OAuth2 endpoint
5. User authenticates with Kanidm
6. Kanidm redirects to `/callback`
7. Nginx redirects `/callback` to `/auth/v1/callback` (GoTrue's callback endpoint)
8. GoTrue completes the authentication and redirects back to Readest
9. User is logged in and sees their library

### Native App Support (Android)

The official Android APK works against this deployment, but it cannot be patched like the web
image (its JS is bundled in the APK), so login depends entirely on the server side:

1. **Nginx provider rewrite preserves `redirect_to`.** The APK always sends
   `provider=discord&redirect_to=readest://auth-callback`. The API ingress
   `configuration-snippet` rewrites the provider to `custom:kanidm` while keeping `redirect_to`
   (default web callback URL when absent). Uses flag variables + `return 302` — see the
   troubleshooting doc for the nginx pitfalls ("if is evil", rewrite loops).
2. **`GOTRUE_URI_ALLOW_LIST` includes `readest://auth-callback`**, otherwise GoTrue silently
   ignores the deep-link redirect target and sends the browser back to the web UI.
3. **`GOTRUE_CORS_ALLOWED_HEADERS: apikey`.** The Tauri WebView is cross-origin with the API, so
   supabase-js calls go through CORS preflight; GoTrue's defaults don't allow the `apikey` header
   that supabase-js sends on every request, which silently breaks session establishment after the
   deep link returns (app says "go to login"). Supabase Cloud hides this behind Kong.

Full flow, root causes and diagnosis commands:
`docs/troubleshooting/readest-android-oauth.md`.

### Learnings and Workarounds

This integration required several workarounds due to limitations in GoTrue and Readest:

#### GoTrue SSRF Protection

**Problem:** GoTrue's custom OAuth provider API (`/admin/custom-providers`) validates URLs and blocks private IPs (RFC 1918) to prevent SSRF attacks. Since `idm.grigri.cloud` resolves to `192.168.193.4` from within the cluster, registration fails with "URL cannot resolve to private network addresses".

**Attempted Solutions:**
- `GOTRUE_CUSTOM_OAUTH_PRIVATE_HOSTS` environment variable - doesn't exist in GoTrue v2.196.0
- Using `oidc` provider type instead of `oauth2` - same validation
- Using `oauth2` with explicit endpoints - same validation

**Workaround:** Insert the custom provider directly into the `auth.custom_oauth_providers` table via SQL, bypassing the API validation:

```sql
INSERT INTO auth.custom_oauth_providers (
  provider_type, identifier, name, client_id, client_secret,
  authorization_url, token_url, userinfo_url, scopes, pkce_enabled, enabled
) VALUES (
  'oauth2', 'custom:kanidm', 'Kanidm',
  '<client_id>', '<client_secret>',
  'https://idm.grigri.cloud/ui/oauth2',
  'https://idm.grigri.cloud/oauth2/token',
  'https://idm.grigri.cloud/oauth2/openid/readest/userinfo',
  ARRAY['openid', 'profile', 'email'], true, true
);
```

**Impact:** The custom provider is not managed by GitOps and must be manually inserted if the database is recreated.

#### Readest UI Hardcoded OAuth Providers

**Problem:** Readest's frontend has hardcoded OAuth provider buttons (Google, Apple, GitHub, Discord) in `AuthPanel.tsx`. The `OAuthProvider` type is restricted to these specific values, and the UI doesn't support custom providers.

**Attempted Solutions:**
- Custom Docker image - rejected to avoid maintenance burden
- Runtime configuration (`OAUTH_PROVIDERS` env var) - Readest doesn't support this
- CSS/JavaScript injection via nginx - didn't work reliably with Next.js SPA

**Workaround:** Init container patches the compiled JavaScript bundles at pod startup:
- Searches for `provider:"discord"` and replaces with `provider:"custom:kanidm"`
- Changes button label from "Discord" to "Kanidm"
- Removes the other provider buttons using sed

**Impact:** The patch must be updated if Readest changes its UI structure or JavaScript compilation output. The init container runs on every pod start, so changes are applied automatically.

#### GoTrue Callback URL Construction

**Problem:** GoTrue constructs the OAuth callback URL as `API_EXTERNAL_URL + "/callback"`, which gives `https://readest.grigri.cloud/callback`. However, Readest's frontend expects the callback at `/auth/callback`, and GoTrue's actual callback endpoint is at `/auth/v1/callback`.

**Attempted Solutions:**
- Change `API_EXTERNAL_URL` to include `/auth/v1` - breaks other URLs
- Find GoTrue config to override callback path - doesn't exist
- Update Kanidm client to accept `/callback` - works but doesn't solve routing

**Workaround:** Nginx redirect from `/callback` to `/auth/v1/callback`:

```yaml
nginx.ingress.kubernetes.io/server-snippet: |
  location = /callback {
    return 301 /auth/v1/callback$is_args$args;
  }
```

**Impact:** Adds a redirect hop to the OAuth flow. The Kanidm client must accept both `/callback` and `/auth/v1/callback` as valid redirect URLs.

#### GoTrue Custom Provider Prefix Requirement

**Problem:** GoTrue requires custom OAuth providers to have the `custom:` prefix (e.g., `custom:kanidm`). The Readest UI sends `provider=discord` without this prefix, so GoTrue never finds the custom provider.

**Attempted Solutions:**
- Register provider with identifier `discord` - GoTrue rejects because `discord` is a built-in provider
- Use environment-based OIDC (`GOTRUE_EXTERNAL_OIDC_*`) - same SSRF protection

**Workaround:**
1. Init container patches UI to send `provider:"custom:kanidm"` instead of `provider:"discord"`
2. Nginx rewrite converts any remaining hardcoded provider params to `custom:kanidm`

**Impact:** The init container must patch all occurrences of the provider string in the JavaScript bundles.

#### Zalando Postgres Search Path

**Problem:** GoTrue migrations failed because it couldn't find the `auth` schema. The database had the schema, but GoTrue's connection didn't include it in the search path.

**Workaround:** Set the search path at the database level:

```sql
ALTER DATABASE readest SET search_path TO auth, public;
```

**Impact:** Must be run after database creation. If the database is recreated, this must be re-run.

#### Manual Schema Setup for Supabase Compatibility

**Problem:** Zalando Postgres operator doesn't auto-create Supabase-specific schemas and enum types. GoTrue migrations expect these to exist.

**Workaround:** Manual schema setup (one-time operation):

```sql
-- Create schemas
CREATE SCHEMA IF NOT EXISTS auth;
CREATE SCHEMA IF NOT EXISTS storage;
CREATE SCHEMA IF NOT EXISTS realtime;
CREATE SCHEMA IF NOT EXISTS graphql_public;

-- Create enum types required by GoTrue migrations
CREATE TYPE auth.factor_type AS ENUM ('totp', 'webauthn', 'phone');
CREATE TYPE auth.code_challenge_method AS ENUM ('s256', 'plain');
CREATE TYPE auth.one_time_token_type AS ENUM (
  'confirmation_token', 'reauthentication_token', 'recovery_token',
  'email_change_token_new', 'email_change_token_current', 'email_change_verify'
);
```

**Impact:** Must be run after database creation. If the database is recreated, this must be re-run.

### Premium Features and Quotas

**Status: Working.**

Readest is a freemium SaaS product with tiered quotas. For self-hosted deployments, quotas can be configured via environment variables:

- `STORAGE_FIXED_QUOTA`: Cloud storage limit in bytes (default: 1GB for free tier)
- `TRANSLATION_FIXED_QUOTA`: Daily translation character limit (default: 10K for free tier)

**Current Configuration (Pro plan limits):**
- Storage: 20GB (21474836480 bytes)
- Translations: 500K characters/day (500000)

**Note:** The "Upgrade to Readest Premium" button still appears in the UI because Readest checks subscription status through the REST API. However, the actual quotas are already at Pro plan limits, so all features work without restriction.

### Summary of Hacks

| Hack | Why | Maintenance Burden |
|------|-----|-------------------|
| Init container patches JS bundles | Readest UI hardcoded | Medium - must update if UI changes |
| Nginx rewrite for provider param (preserves `redirect_to`) | Web UI hardcoded + APK can't be patched | Medium - snippet must survive UI/URL changes |
| Nginx redirect `/callback` → `/auth/v1/callback` | GoTrue callback URL construction | Low |
| Manual DB insert for custom provider | GoTrue SSRF protection | High - not GitOps-managed |
| Manual schema setup | Zalando doesn't auto-create Supabase schemas | Low - one-time per DB |
| Database search path | GoTrue can't find auth schema | Low - one-time per DB |
| Environment variable quotas | Self-hosted deployment | Low - GitOps-managed |
| `GOTRUE_URI_ALLOW_LIST` deep-link scheme | Android app returns via `readest://` | Low - GitOps-managed |
| `GOTRUE_CORS_ALLOWED_HEADERS: apikey` | GoTrue CORS omits `apikey`; Tauri WebView is cross-origin | Low - GitOps-managed |

Mobile login deep-dive (flow, root causes, diagnosis): `docs/troubleshooting/readest-android-oauth.md`.

### Failure Modes and Recovery (Runbook)

Most breakage happens after **Renovate bumps `ghcr.io/readest/readest` or `supabase/gotrue`**.
After merging such PRs, always smoke-test: web login (Kanidm button) and mobile login.

**Triage first:** web broken → provider row / init patch / nginx snippets. Mobile-only broken →
CORS, `GOTRUE_URI_ALLOW_LIST`, provider rewrite. Quick check chain:

```bash
curl -sI  "https://readest.grigri.cloud/auth/v1/authorize?provider=discord&redirect_to=readest://auth-callback" | grep -i location   # nginx rewrite
curl -sI  "https://readest.grigri.cloud/callback" | grep -i location                                                                 # callback redirect
curl -s -D - -o /dev/null -X OPTIONS "https://readest.grigri.cloud/auth/v1/user" \
  -H "Origin: https://tauri.localhost" -H "Access-Control-Request-Method: GET" \
  -H "Access-Control-Request-Headers: apikey,authorization,x-client-info" | grep -i access-control-allow-headers                     # CORS
```

#### 1. Init container `patch-oauth` fails (Readest web upgrade)

- **Symptoms:** `readest-client` pod stuck in `Init:Error`/`CrashLoopBackOff` (script exits 1
  when it can't find `provider:"discord"` — loud by design). If pods start but the login screen
  shows Google/Apple/GitHub buttons again, a button-removal `sed` pattern silently stopped
  matching.
- **Diagnose:**
  ```bash
  kubectl --context=grigri logs -n readest deploy/readest-client -c patch-oauth
  ```
- **Fix:** find the new minified patterns and update the init script in `apps/readest/values.yaml`:
  ```bash
  # inspect the new image's bundles
  docker run --rm -it --entrypoint sh ghcr.io/readest/readest:<newtag>
  grep -rl 'provider:"discord"' /  2>/dev/null   # locate the bundle(s)
  grep -o 'provider:"discord"[^,]*' <bundle>
  ```
  Diff old vs new bundle to see what changed (minifier output shifts between releases), adapt the
  `sed` expressions, `helm lint` + `helm template`, commit. Re-verify the login screen after sync.

#### 2. Nginx provider rewrite no longer triggers (login lands on web UI / "unsupported provider")

- **Symptoms:** mobile login completes but stays in browser; GoTrue logs show
  `provider=discord` reaching `/authorize` (rewrite skipped) or provider errors.
- **Diagnose:** first `curl` above — the `location` must contain
  `provider=custom:kanidm&redirect_to=readest://auth-callback`. Check what the app actually sends:
  `adb logcat -s NativeBridgePlugin` → `Launching OAuth URL:` (new APK versions may change the URL
  shape, e.g. new provider ids, extra params, PKCE `code_challenge`).
- **Fix:** update the `configuration-snippet` gate conditions in `apps/readest/values.yaml`
  (both `readest-api` and `external-api` ingresses). Keep the nginx "if is evil" rules in
  `docs/troubleshooting/readest-android-oauth.md` in mind: no nested `if`, no `map` in snippets,
  `return 302` instead of `rewrite ... last`.

#### 3. `/callback` returns 404 after Kanidm login

- **Symptoms:** browser shows a Next.js 404 on `readest.grigri.cloud/callback?code=...`.
- **Diagnose:** second `curl` above must return `301 → /auth/v1/callback`. Verify the
  `server-snippet` exists on both client ingresses (`readest-client`, `external-client`).
- **Fix:** restore the `location = /callback { return 301 /auth/v1/callback$is_args$args; }`
  server-snippet.

#### 4. Login fails for everyone after DB recreation/restore (custom provider row lost)

- **Symptoms:** "unsupported provider" or provider-not-found in GoTrue logs; web and mobile both
  broken. The `custom:kanidm` row is a manual insert, **not** GitOps-managed.
- **Diagnose:**
  ```bash
  kubectl --context=grigri exec -n readest readest-postgres-0 -c postgres -- \
    psql -U supabase_auth_admin -d readest -c \
    "SELECT identifier, enabled FROM auth.custom_oauth_providers;"
  ```
- **Fix:** re-run the `INSERT` from the "GoTrue SSRF Protection" section above, and — if the DB
  was recreated — repeat the one-time steps: Supabase schemas/enums, `ALTER DATABASE ... SET
  search_path` (Deployment Steps 10).

#### 5. Mobile "go to login" returns (CORS or URI allow-list regression)

- **Symptoms:** deep link returns to the app but no session. After gotrue upgrades, CORS defaults
  or env-var names may change.
- **Diagnose:** third `curl` above — `access-control-allow-headers` must include `apikey` (a 204
  with no CORS headers = preflight failure). Then check GoTrue logs for the device IP: only
  `/authorize`+`/callback` and no `/user`/`/token` = client-side call blocked. Also verify
  `GOTRUE_URI_ALLOW_LIST` still contains `readest://auth-callback`.
- **Fix:** re-apply `GOTRUE_CORS_ALLOWED_HEADERS: apikey`; if the upgraded gotrue renamed the
  setting, check `internal/conf/configuration.go` (`CORSConfiguration`) in the matching
  supabase/auth tag. If the new APK changed the deep-link scheme, update the allow-list and the
  rewrite's default `redirect_to` accordingly.

#### 6. Users appear as "free" on mobile (known limitation)

GoTrue puts `plan`/quotas in `user_metadata`; the app reads a top-level `plan` claim. UI-gating
only — server quotas are unaffected. Long-term fix: GoTrue custom access-token hook promoting the
claims. See `docs/troubleshooting/readest-android-oauth.md`.

#### 7. "permission denied for table files" or RLS violation on upload

- **Symptoms:** `{"error":"permission denied for table files"}` or
  `{"error":"new row violates row-level security policy for table \"files\""}` when uploading
  books via `/api/storage/upload`.
- **Root cause:** The storage upload API uses `service_role` (via `SUPABASE_ADMIN_KEY`). If
  `service_role` lacks table grants or the `BYPASSRLS` attribute, uploads fail.
- **Diagnose:**
  ```bash
  kubectl --context=grigri exec -n readest readest-postgres-0 -c postgres -- \
    psql -U readest -d readest -c "
    SELECT rolname, rolbypassrls FROM pg_roles WHERE rolname = 'service_role';
    SELECT grantee, privilege_type FROM information_schema.role_table_grants
    WHERE table_schema = 'public' AND table_name = 'files' AND grantee = 'service_role';
  "
  ```
- **Fix:** The `db-migrate` init container now handles this automatically. If manually fixing:
  ```sql
  ALTER ROLE service_role BYPASSRLS;
  GRANT ALL ON ALL TABLES IN SCHEMA public TO service_role;
  GRANT ALL ON ALL SEQUENCES IN SCHEMA public TO service_role;
  GRANT USAGE ON SCHEMA auth TO service_role;
  GRANT EXECUTE ON ALL FUNCTIONS IN SCHEMA auth TO service_role;
  ```

### Future Improvements

If Readest adds support for custom OAuth providers via runtime configuration, we can:
- Remove the init container
- Remove the nginx rewrite
- Use standard GoTrue custom OAuth provider registration (if SSRF protection is relaxed)

If GoTrue adds support for private hosts in custom OAuth providers, we can:
- Use the GoTrue API to register the provider instead of manual DB insert
- Manage the provider configuration via GitOps

Open mobile gap: GoTrue puts Kanidm claims (`plan`, quotas) in `user_metadata`, but the app reads
a top-level `plan` JWT claim, so mobile users resolve as "free" (cosmetic/UI-gating; server-side
quotas are unaffected). Fix would be a GoTrue custom access-token hook promoting those claims.
See `docs/troubleshooting/readest-android-oauth.md#known-limitations--follow-ups`.

## MinIO (Shared Instance)

Readest uses the shared MinIO at `platform/minio/` for S3 storage.

### Additions to `platform/minio/`

**Bucket:**
```yaml
- name: readest-files
  policy: none
  versioning: false
  objectlocking: false
```

**Policy:**
```yaml
- name: readestPolicy
  statements:
    - resources:
        - 'arn:aws:s3:::readest-files/*'
      actions:
        - "s3:AbortMultipartUpload"
        - "s3:GetObject"
        - "s3:DeleteObject"
        - "s3:PutObject"
        - "s3:ListMultipartUploadParts"
    - resources:
        - 'arn:aws:s3:::readest-files'
      actions:
        - "s3:GetBucketLocation"
        - "s3:ListBucket"
        - "s3:ListMultipartUploads"
```

**User:**
```yaml
- accessKey: readest
  existingSecret: minio-users
  existingSecretKey: readestPassword
  policy: readestPolicy
```

### Readest S3 Config

```yaml
S3_ENDPOINT: http://minio.minio.svc:9000
S3_PUBLIC_ENDPOINT: https://s3.internal.grigri.cloud
S3_REGION: us-east-1
S3_BUCKET_NAME: readest-files
S3_ACCESS_KEY_ID: readest
S3_SECRET_ACCESS_KEY:
  valueFrom:
    secretKeyRef:
      name: minio-users
      key: readestPassword
```

## Ingress (nginx)

Single domain with path-based routing:

```yaml
ingress:
  readest:
    enabled: true
    className: nginx-internal  # + external ingress
    annotations:
      nginx.ingress.kubernetes.io/proxy-body-size: "0"  # large book uploads
    hosts:
      - host: readest.grigri.cloud
        paths:
          - path: /auth/v1
            pathType: Prefix
            service:
              identifier: auth
              port: http
          - path: /rest/v1
            pathType: Prefix
            service:
              identifier: rest
              port: http
          - path: /
            pathType: Prefix
            service:
              identifier: client
              port: http
```

Both internal (`readest.internal.grigri.cloud`) and external (`readest.grigri.cloud`) ingresses.

## Secrets (Vault)

| Vault Path | Keys |
|---|---|
| `/readest/jwt` | `secret`, `anon_key`, `service_role_key` |
| `/minio/users` | `readestPassword` (added to existing path) |

The `anon_key` and `service_role_key` are HS256 JWTs signed with the JWT secret:
- `anon_key`: payload `{"role": "anon"}`
- `service_role_key`: payload `{"role": "service_role"}`

## Deployment Steps

1. User logs in to Vault (`vault login`)
2. Create `/readest/jwt` secret (generate JWT keys)
3. Add `readestPassword` to `/minio/users` in Vault
4. Update `platform/minio/` (values.yaml + external-secrets)
5. Create all files under `apps/readest/`
6. `helm dependency build apps/readest/`
7. `helm template --include-crds --namespace readest readest apps/readest/` to validate
8. `helm lint apps/readest/`
9. Commit and push — ArgoCD auto-syncs
10. **Manual database setup** (required for Supabase/GoTrue compatibility — one-time):
    ```bash
    # Create schemas required by GoTrue
    kubectl --context=grigri exec -n readest readest-postgres-0 -c postgres -- \
      psql -U supabase_auth_admin -d readest -c \
      "CREATE SCHEMA IF NOT EXISTS auth; CREATE SCHEMA IF NOT EXISTS storage; CREATE SCHEMA IF NOT EXISTS realtime; CREATE SCHEMA IF NOT EXISTS graphql_public;"

    # Create enum types required by GoTrue migrations
    kubectl --context=grigri exec -n readest readest-postgres-0 -c postgres -- \
      psql -U supabase_auth_admin -d readest -c \
      "CREATE TYPE auth.factor_type AS ENUM ('totp', 'webauthn', 'phone');
       CREATE TYPE auth.code_challenge_method AS ENUM ('s256', 'plain');
       CREATE TYPE auth.one_time_token_type AS ENUM ('confirmation_token', 'reauthentication_token', 'recovery_token', 'email_change_token_new', 'email_change_token_current', 'email_change_verify');"
    ```
    **Note:** The Readest app tables (`books`, `replicas`, etc.) are created automatically by the `db-migrate` init container on first pod start.
11. Store DB connection strings in Vault:
    ```bash
    export VAULT_ADDR=https://vault.internal.grigri.cloud
    AUTH_PASSWORD=$(kubectl --context=grigri get secret -n readest supabase-auth-admin.readest-postgres.credentials.postgresql.acid.zalan.do -o jsonpath='{.data.password}' | base64 -d)
    REST_PASSWORD=$(kubectl --context=grigri get secret -n readest authenticator.readest-postgres.credentials.postgresql.acid.zalan.do -o jsonpath='{.data.password}' | base64 -d)
    AUTH_URL="postgres://supabase_auth_admin:${AUTH_PASSWORD}@readest-postgres:5432/readest?sslmode=require"
    REST_URL="postgres://authenticator:${REST_PASSWORD}@readest-postgres:5432/readest?sslmode=require"
    vault kv put secret/readest/db-urls auth_url="$AUTH_URL" rest_url="$REST_URL"
    ```
12. Verify pods, access `readest.internal.grigri.cloud`

## Key Configuration

```yaml
SELF_HOSTED: "true"                    # unlocks all premium features
DISABLE_SIGNUP: "false"                # allow user registration
ENABLE_EMAIL_AUTOCONFIRM: "true"       # no SMTP needed
STORAGE_FIXED_QUOTA: "1073741824"      # 1GB storage per user
TRANSLATION_FIXED_QUOTA: "50000"       # 50k translation characters
OBJECT_STORAGE_TYPE: "s3"              # use MinIO for book storage
```

## Deleting Books

Readest supports soft-delete for books. To delete a book:

1. **Web UI**: Right-click on the book cover in the library → "Delete Book"
2. **Desktop App**: Right-click on the book → "Delete Book"

Deleted books are marked with `deleted_at` timestamp in the database and hidden from the library view. The actual files remain in MinIO storage until manually cleaned up.

To permanently remove a book (including files):

```bash
# Get the book ID and file key
kubectl --context=grigri exec -n readest readest-postgres-0 -c postgres -- \
  psql -U readest -d readest -c "
  SELECT id, book_hash, file_key FROM public.files
  WHERE user_id = '<user-id>' AND deleted_at IS NOT NULL;
"

# Delete from MinIO
kubectl --context=grigri exec -n platform-minio-0 -c minio -- \
  mc rm --recursive --force myminio/readest-files/<user-id>/Readest/Books/<book_hash>/

# Hard delete from database
kubectl --context=grigri exec -n readest readest-postgres-0 -c postgres -- \
  psql -U readest -d readest -c "
  DELETE FROM public.files WHERE user_id = '<user-id>' AND book_hash = '<book_hash>';
  DELETE FROM public.books WHERE id = '<book-id>';
"
```

## Files Created

```
apps/readest/
├── Chart.yaml
├── values.yaml
└── templates/
    ├── postgresql.yaml              # Zalando CR
    ├── external-secrets.yaml        # Vault: JWT, MinIO readest user password, OIDC secret
    ├── kanidm-oauth2-client.yaml    # kaniop OIDC client
    ├── kanidm-group.yaml            # kaniop readest-users group
    └── configmap-init-sql.yaml      # Supabase bootstrap SQL (roles, schema, migrations)
```

Updates to `platform/minio/`:
- `values.yaml` — add `readest-files` bucket + `readest` user + scoped policy
- `templates/external-secrets-users.yaml` — add `readestPassword` from Vault
