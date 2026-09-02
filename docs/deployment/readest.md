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
