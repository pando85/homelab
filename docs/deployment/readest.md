# Readest Deployment

Readest is an open-source ebook reader with a self-hosted stack. This deployment uses:
- **bjw-s `app-template`** with multiple controllers
- **Zalando Postgres operator** for the database
- **Public MinIO** (`apps/s3-public/`) for S3 storage (externally reachable for book downloads)
- **kaniop** for OIDC integration with Kanidm
- **nginx ingress** for path-based routing (no Kong)

## Architecture

```
Browser
  └── readest.grigri.cloud
        ├── / ──────────────► client:3000 (Next.js)
        ├── /auth/v1/* ─────► auth:9999 (GoTrue)
        └── /rest/v1/* ─────► rest:3000 (PostgREST)
  └── s3.grigri.cloud (presigned URLs) ──► s3-public MinIO :9000

Internal:
  client ──► s3-public-minio.s3-public.svc:9000 (S3, bucket: readest-files)
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

No MinIO controller — uses the dedicated public MinIO at `apps/s3-public/`.

## Database: Zalando Postgres + Automated Migrations

Readest's schema is tightly coupled to Supabase (uses `auth.users` FK, `auth.uid()` in RLS policies, Supabase roles). The solution:

1. **Zalando CR** creates the database, base roles, and `pgcrypto` extension
2. **`db-migrate` init container** automatically bootstraps the full schema on every pod start:
   - Creates required schemas: `auth`, `extensions`, `graphql_public`
   - Creates required roles (`anon`, `authenticated`, `service_role`) with `service_role BYPASSRLS`
   - Downloads `schema.sql` and all migrations from GitHub at `DB_SCHEMA_VERSION` (matches container image tag)
   - Grants permissions to `anon`, `authenticated`, and `service_role` on public tables and `auth` schema
   - Sets default privileges for future tables
   - Applies base schema + incremental migrations with idempotent ledger tracking (`readest_meta.migrations`)
   - Skips downloads if all migrations already applied (optimization)
   - Uses advisory lock for safe concurrent pod starts
   - Sends `NOTIFY pgrst, 'reload schema'` so PostgREST picks up new tables without restart
3. **GoTrue** runs migrations on first start to create tables in the `auth` schema

**Note:** All schema setup is fully automated. The `db-migrate` init container creates all required schemas (`auth`, `extensions`, `graphql_public`) and roles before applying migrations. No manual steps needed on fresh clusters.

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

**Status: Automated.** The `db-migrate` init container now creates all required schemas (`auth`, `extensions`, `graphql_public`) and enum types automatically. No manual steps needed.

If you need to verify the schemas exist:

```bash
kubectl --context=grigri exec -n readest readest-postgres-0 -c postgres -- \
  psql -U postgres -d readest -c \
  "SELECT nspname FROM pg_namespace WHERE nspname IN ('auth','extensions','graphql_public') ORDER BY nspname;"
```

**Historical note:** Previously this was a manual one-time operation. The automation was added after a postgres recreate incident revealed the need for idempotent schema setup.

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
| `db-migrate` init container | Automates schema setup (auth, extensions, graphql_public, roles, migrations) | Low - fully automated |
| Database search_path | GoTrue can't find auth schema | Low - one-time per DB |
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

## Object Storage (Public MinIO)

Readest stores book files in the dedicated **public** MinIO at `apps/s3-public/` (host
`s3.grigri.cloud`), *not* the internal backup MinIO. Book downloads use **presigned URLs** that the
browser fetches directly, so the S3 host must be reachable from the internet — the internal
`s3.internal.grigri.cloud` is cluster-only and breaks external downloads.

See `docs/deployment/s3-public.md` for the full public MinIO design.

### Bucket / user (in `apps/s3-public/`)

- Bucket: `readest-files`
- User (access key): `readest`, scoped by `readestPolicy` (rw on `readest-files/*`)
- Password: Vault `/s3-public/users:readestPassword`

### Readest S3 Config

```yaml
S3_ENDPOINT: http://s3-public-minio.s3-public.svc:9000   # in-cluster (server-side I/O)
S3_PUBLIC_ENDPOINT: https://s3.grigri.cloud              # external (presigned URLs)
S3_REGION: us-east-1
S3_BUCKET_NAME: readest-files
S3_ACCESS_KEY_ID: readest
S3_SECRET_ACCESS_KEY:
  valueFrom:
    secretKeyRef:
      name: readest-minio-secret   # ExternalSecret → /s3-public/users:readestPassword
      key: readestPassword
```

`S3_ENDPOINT` is the in-cluster service for server-side reads/writes; `S3_PUBLIC_ENDPOINT` is the
external host embedded in presigned URLs the browser fetches.

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
| `/s3-public/users` | `readestPassword` (S3 secret key for the public MinIO) |

The `anon_key` and `service_role_key` are HS256 JWTs signed with the JWT secret:
- `anon_key`: payload `{"role": "anon"}`
- `service_role_key`: payload `{"role": "service_role"}`

## Deployment Steps

1. User logs in to Vault (`vault login`)
2. Create `/readest/jwt` secret (generate JWT keys)
3. Create `/s3-public/users` secret with `readestPassword` (see `docs/deployment/s3-public.md`)
4. Deploy `apps/s3-public/` (public MinIO) — see `docs/deployment/s3-public.md`
5. Create all files under `apps/readest/`
6. `helm dependency build apps/readest/`
7. `helm template --include-crds --namespace readest readest apps/readest/` to validate
8. `helm lint apps/readest/`
9. Commit and push — ArgoCD auto-syncs
10. **Verify schema setup** (should be automatic via `db-migrate` init container):
    ```bash
    # Check that all required schemas exist
    kubectl --context=grigri exec -n readest readest-postgres-0 -c postgres -- \
      psql -U postgres -d readest -c \
      "SELECT nspname FROM pg_namespace WHERE nspname IN ('auth','extensions','graphql_public') ORDER BY nspname;"

    # Check that db-migrate completed successfully
    kubectl --context=grigri logs -n readest deploy/readest-client -c db-migrate | tail -20
    ```
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

## Recreating the Postgres Database

If you need to recreate the postgres database (e.g., PVC corruption, node migration), follow this procedure:

### Before Recreate: Backup Non-GitOps Data

The following are **not** managed by GitOps and must be backed up manually:

```bash
BACKUP_DIR="/home/agil/backups/readest-pre-recreate-$(date +%Y%m%d-%H%M%S)"
mkdir -p "$BACKUP_DIR" && chmod 700 "$BACKUP_DIR"

# 1. Database-level settings (search_path)
kubectl --context=grigri exec -n readest readest-postgres-0 -c postgres -- \
  psql -U postgres -d readest -c \
  "SELECT 'ALTER DATABASE '||d.datname||' SET search_path TO '||array_to_string(setconfig, ', ')||';'
   FROM pg_db_role_setting s JOIN pg_database d ON d.oid=s.setdatabase
   WHERE d.datname='readest';" > "$BACKUP_DIR/db-settings.sql"

# 2. Custom OAuth provider (Kanidm integration)
kubectl --context=grigri exec -n readest readest-postgres-0 -c postgres -- \
  psql -U postgres -d readest -c \
  "COPY (SELECT * FROM auth.custom_oauth_providers WHERE identifier='custom:kanidm') TO STDOUT WITH CSV HEADER;" \
  > "$BACKUP_DIR/kanidm-custom-oauth-provider.csv"

# 3. User data (optional, if you want to preserve accounts)
kubectl --context=grigri exec -n readest readest-postgres-0 -c postgres -- \
  pg_dump -U postgres -d readest --table=auth.users --table=public.books --data-only \
  > "$BACKUP_DIR/user-data.sql"

# 4. Full dump (safety net)
kubectl --context=grigri exec -n readest readest-postgres-0 -c postgres -- \
  pg_dump -U postgres -d readest > "$BACKUP_DIR/readest-full.sql"

echo "Backup saved to $BACKUP_DIR"
```

### Recreate Procedure

1. **Delete the PVC and pod** (this triggers a fresh volume):
   ```bash
   kubectl --context=grigri delete pod readest-postgres-0 -n readest
   kubectl --context=grigri delete pvc pgdata-readest-postgres-0 -n readest
   ```

2. **Delete stale Patroni DCS ConfigMaps** (critical — see troubleshooting doc):
   ```bash
   kubectl --context=grigri delete configmap readest-postgres-config readest-postgres-leader -n readest
   ```

3. **Wait for the pod to recreate** and Patroni to bootstrap:
   ```bash
   kubectl --context=grigri get pods -n readest -w
   # Wait for readest-postgres-0 to show 2/2 Running
   kubectl --context=grigri exec -n readest readest-postgres-0 -c postgres -- patronictl list
   # Should show: Role=Leader, State=running
   ```

4. **Force Zalando operator sync** (if database/roles aren't created automatically):
   ```bash
   kubectl --context=grigri annotate postgresql readest-postgres -n readest \
     zalando.org/sync-at="$(date +%s)" --overwrite
   ```

5. **Restart app pods** to trigger `db-migrate` and GoTrue migrations:
   ```bash
   kubectl --context=grigri rollout restart deployment/readest-client deployment/readest-auth -n readest
   ```

6. **Wait for schema setup** (db-migrate + GoTrue):
   ```bash
   # Check db-migrate logs
   kubectl --context=grigri logs -n readest deploy/readest-client -c db-migrate | tail -30

   # Verify schemas exist
   kubectl --context=grigri exec -n readest readest-postgres-0 -c postgres -- \
     psql -U postgres -d readest -c \
     "SELECT nspname FROM pg_namespace WHERE nspname IN ('auth','extensions','graphql_public');"
   ```

7. **Restore non-GitOps data**:
   ```bash
   # Restore search_path
   kubectl --context=grigri exec -i -n readest readest-postgres-0 -c postgres -- \
     psql -U postgres -d readest < "$BACKUP_DIR/db-settings.sql"

   # Restore custom OAuth provider (convert CSV to INSERT)
   # Note: You'll need to manually convert the CSV to an INSERT statement
   # or re-insert from the deployment doc template

   # Restore user data (if backed up)
   kubectl --context=grigri exec -i -n readest readest-postgres-0 -c postgres -- \
     psql -U postgres -d readest < "$BACKUP_DIR/user-data.sql"
   ```

8. **Restart PostgREST** to reload schema cache:
   ```bash
   kubectl --context=grigri rollout restart deployment/readest-rest -n readest
   ```

9. **Verify**:
   - Login via Kanidm OIDC
   - Upload/download books
   - Check PostgREST API responses

### Common Issues After Recreate

- **Patroni "waiting for leader to bootstrap"**: Stale DCS ConfigMaps not deleted. See `docs/troubleshooting/zalando-patroni-stale-dcs-deadlock.md`.
- **"schema auth does not exist"**: db-migrate didn't run or failed. Check init container logs.
- **"permission denied for table files"**: `service_role` missing BYPASSRLS or grants. db-migrate should handle this automatically.
- **PostgREST 503 "Could not query schema cache"**: Missing `graphql_public` schema or PostgREST needs restart.

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

Object storage lives in the dedicated public MinIO `apps/s3-public/` (see
`docs/deployment/s3-public.md`):
- `values.yaml` — `readest-files` bucket + `readest` user + scoped `readestPolicy`
- `templates/external-secrets-users.yaml` — `readestPassword` from Vault `/s3-public/users`
