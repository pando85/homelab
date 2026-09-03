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
| `client` | `ghcr.io/readest/readest:0.9.20` | 3000 | Next.js web app |
| `db-init` | init container (same as client) | — | Runs Supabase bootstrap SQL on first start |
| `auth` | `supabase/gotrue:v2.185.0` | 9999 | Auth service (OIDC client) |
| `rest` | `postgrest/postgrest:v14.3` | 3000 | REST API (auto-generated from Postgres schema) |

No MinIO controller — uses shared `platform/minio/` instance.

## Database: Zalando Postgres + Supabase Bootstrap

Readest's schema is tightly coupled to Supabase (uses `auth.users` FK, `auth.uid()` in RLS policies, Supabase roles). The solution:

1. **Zalando CR** creates the database, base roles, and `pgcrypto` extension
2. **Manual schema setup** (required because Zalando doesn't auto-create Supabase schemas):
   - Create `auth`, `storage`, `realtime`, `graphql_public` schemas
   - Create enum types: `auth.factor_type`, `auth.code_challenge_method`, `auth.one_time_token_type`
3. **GoTrue** runs migrations on first start to create tables in the `auth` schema
4. SQL files mounted from ConfigMap (vendored from upstream Readest repo)

**Note:** The manual schema setup is a one-time operation. If the database is recreated, these steps must be repeated.

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
| Nginx rewrite for provider param | Belt and suspenders | Low |
| Nginx redirect `/callback` → `/auth/v1/callback` | GoTrue callback URL construction | Low |
| Manual DB insert for custom provider | GoTrue SSRF protection | High - not GitOps-managed |
| Manual schema setup | Zalando doesn't auto-create Supabase schemas | Low - one-time per DB |
| Database search path | GoTrue can't find auth schema | Low - one-time per DB |
| Environment variable quotas | Self-hosted deployment | Low - GitOps-managed |

### Future Improvements

If Readest adds support for custom OAuth providers via runtime configuration, we can:
- Remove the init container
- Remove the nginx rewrite
- Use standard GoTrue custom OAuth provider registration (if SSRF protection is relaxed)

If GoTrue adds support for private hosts in custom OAuth providers, we can:
- Use the GoTrue API to register the provider instead of manual DB insert
- Manage the provider configuration via GitOps

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
10. **Manual database setup** (required for Supabase compatibility):
    ```bash
    # Create schemas
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
