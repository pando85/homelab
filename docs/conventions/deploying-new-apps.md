# Deploying New Applications

This guide captures the decision-making process and patterns for deploying new applications to the homelab. Use this as a checklist when planning a new app deployment.

## Phase 1: Analyze the Application

### 1.1 Identify the Stack

Start by understanding what the app needs. For Docker Compose deployments, map each service:

```bash
# Example: analyze Readest's compose.yaml
curl -s https://raw.githubusercontent.com/readest/readest/main/docker/compose.yaml
```

Create a table:

| Service | Image | Port | Purpose | State |
|---------|-------|------|---------|-------|
| client | ghcr.io/readest/readest | 3000 | Next.js web app | Stateless |
| db | supabase/postgres | 5432 | PostgreSQL | Stateful |
| auth | supabase/gotrue | 9999 | Auth service | Stateless |
| rest | postgrest/postgrest | 3000 | REST API | Stateless |
| minio | minio/minio | 9000 | S3 storage | Stateful |

### 1.2 Identify Dependencies

- **Database**: Does it need PostgreSQL, MySQL, Redis, etc.?
- **Object Storage**: Does it need S3/MinIO?
- **Auth**: Does it support OIDC/OAuth? What provider?
- **API Gateway**: Does it need Kong, Traefik, or can nginx handle it?
- **Secrets**: What credentials does it need? (API keys, JWT secrets, etc.)

### 1.3 Check for Existing Infrastructure

Before deploying new instances, check what already exists:

```bash
# Find existing MinIO deployments
grep -r "minio" apps/ platform/ --include="*.yaml"

# Find existing Postgres deployments
grep -r "postgresql" apps/ --include="*.yaml"

# Find existing OIDC integrations
grep -r "KanidmOAuth2Client" apps/ --include="*.yaml"
```

## Phase 2: Make Architectural Decisions

### 2.1 Database: Zalando vs Container

**Use Zalando Postgres operator when:**
- The app uses standard PostgreSQL features
- You want automated backups, snapshots, and operator-managed lifecycle
- The app doesn't require custom Postgres extensions not in the Spilo image
- The app doesn't need Supabase-specific schemas (auth, storage, realtime)

**Use container Postgres when:**
- The app requires Supabase extensions (auth schema, GoTrue, PostgREST)
- The app needs custom init scripts that conflict with Zalando's role management
- The app uses a specific Postgres distribution (e.g., TimescaleDB, Citus)

**Decision tree:**
```
Does the app need Supabase auth/storage/realtime schemas?
├── YES → Use container Postgres with init scripts
└── NO → Does it need custom extensions not in Spilo?
    ├── YES → Use custom Spilo image (see immich) or container Postgres
    └── NO → Use Zalando operator
```

**Example: Readest**
- Needs `auth.users` FK, `auth.uid()` in RLS, Supabase roles
- Decision: Zalando for the database + init container for Supabase bootstrap SQL

### 2.2 Object Storage: Which MinIO?

**See `docs/deployment/minio-architecture.md`** for the complete architecture, decision tree, and
integration patterns.

There are **three** MinIO instances, pick by who fetches the objects:

| Instance | Path | Host | Reachable from internet? | Use for |
|---|---|---|---|---|
| **Internal shared** | `platform/minio/` | `s3.internal.grigri.cloud` | No | In-cluster/backup storage (Velero, Kaniop). Server-side I/O only. |
| **Public** | `apps/s3-public/` | `s3.grigri.cloud` | **Yes** | Apps that hand the **browser** presigned URLs (e.g. Readest book downloads). |
| **Cross-backups** | `apps/cross-backups/` | `cross-backups.grigri.cloud` | Yes | *Receiving* backups pushed from other machines (restic, borg, mc mirror). |

**Quick decision:**
- Browser/external client fetches objects? → **Public MinIO** (`apps/s3-public/`)
- Server-side I/O only? → **Internal MinIO** (`platform/minio/`)
- Receiving backups from external machines? → **Cross-backups** (`apps/cross-backups/`)

> **Gotcha:** The internal MinIO host `s3.internal.grigri.cloud` is `nginx-internal` only. If an
> app generates presigned URLs against it, external clients fail to download. Use `apps/s3-public/`
> and set the app's public endpoint to `https://s3.grigri.cloud`. See `docs/deployment/s3-public.md`.

**Pattern for adding a bucket/user to the public MinIO (`apps/s3-public/values.yaml`):**
```yaml
buckets:
  - name: <app>-files
    policy: none
    versioning: false
    objectlocking: false

policies:
  - name: <app>Policy
    statements:
      - resources:
          - 'arn:aws:s3:::<app>-files/*'
        actions:
          - "s3:GetObject"
          - "s3:PutObject"
          - "s3:DeleteObject"
          - "s3:AbortMultipartUpload"
          - "s3:ListMultipartUploadParts"
      - resources:
          - 'arn:aws:s3:::<app>-files'
        actions:
          - "s3:GetBucketLocation"
          - "s3:ListBucket"
          - "s3:ListBucketMultipartUploads"

users:
  - accessKey: <app>
    existingSecret: s3-public-users
    existingSecretKey: <app>Password
    policy: <app>Policy
```

Then add `<app>Password` to Vault `/s3-public/users` and to
`apps/s3-public/templates/external-secrets-users.yaml`. The same pattern applies to
`platform/minio/` (secret `minio-users`, Vault `/minio/users`) for internal buckets.

### 2.3 API Gateway: Kong vs nginx Ingress

**Use nginx ingress when:**
- The app just needs path-based routing (`/api/*` → backend)
- You don't need API-specific features (rate limiting, JWT validation, etc.)
- The app's backend services can be reached directly via Kubernetes services

**Use Kong when:**
- The app requires API gateway features (rate limiting, JWT validation, request transformation)
- The app's Docker Compose already includes Kong with custom plugins
- You need to expose multiple APIs with different auth mechanisms

**Decision tree:**
```
Does the app's compose.yaml include Kong/Traefik?
├── YES → Can nginx replace it with path-based routing?
│   ├── YES → Use nginx ingress (simpler, no extra controller)
│   └── NO → Use Kong (app requires API gateway features)
└── NO → Use nginx ingress
```

**Example: Readest**
- Compose includes Kong for routing `/auth/v1/*` and `/rest/v1/*`
- nginx can handle this with path-based routing → no Kong needed

### 2.4 OIDC Integration: kaniop Pattern

**When to use OIDC:**
- The app supports OAuth2/OIDC login
- You want centralized authentication via Kanidm
- You want group-based access control

**Pattern:**
```yaml
# 1. Create KanidmGroup (access control)
apiVersion: kaniop.rs/v1beta1
kind: KanidmGroup
metadata:
  name: <app>-users
spec:
  kanidmRef: { name: kanidm, namespace: kanidm }

# 2. Create KanidmOAuth2Client (OAuth2 registration)
apiVersion: kaniop.rs/v1beta1
kind: KanidmOAuth2Client
metadata:
  name: <app>
spec:
  kanidmRef: { name: kanidm, namespace: kanidm }
  displayname: <app>
  origin: https://<app>.grigri.cloud/
  redirectUrl:
    - https://<app>.grigri.cloud/<callback-path>
  scopeMap:
    - group: <app>-users
      scopes: [openid, profile, email]
  preferShortUsername: true
  strictRedirectUrl: true

# 3. Reference credentials in app env vars
env:
  OAUTH_CLIENT_ID:
    valueFrom:
      secretKeyRef:
        name: <app>-kanidm-oauth2-credentials
        key: CLIENT_ID
  OAUTH_CLIENT_SECRET:
    valueFrom:
      secretKeyRef:
        name: <app>-kanidm-oauth2-credentials
        key: CLIENT_SECRET
  OIDC_URL: https://idm.grigri.cloud/oauth2/openid/<app>
```

**Common callback paths:**
- Generic: `/oauth/callback`, `/auth/callback`
- Immich: `/auth/login`
- FreshRSS: `/i/`
- Open WebUI: `/oauth/oidc/callback`

Check the app's documentation for the exact callback URL format.

### 2.5 Secret Management

**Three-tier pattern:**

1. **Zalando auto-generated secrets** (database credentials)
   - No ExternalSecret needed
   - Reference directly: `<teamId>.<cluster-name>.credentials.postgresql.acid.zalan.do`
   - Keys: `username`, `password`

2. **ExternalSecrets from Vault** (app-specific secrets)
   - API keys, JWT secrets, OAuth credentials, SMTP passwords
   - Pattern:
   ```yaml
   apiVersion: external-secrets.io/v1
   kind: ExternalSecret
   metadata:
     name: <app>-secrets
   spec:
     secretStoreRef:
       kind: ClusterSecretStore
       name: vault
     target:
       name: <app>-secrets
     data:
       - secretKey: JWT_SECRET
         remoteRef:
           key: /<app>/jwt
           property: secret
   ```

3. **kaniop auto-generated secrets** (OIDC client credentials)
   - No ExternalSecret needed
   - Reference: `<app>-kanidm-oauth2-credentials`
   - Keys: `CLIENT_ID`, `CLIENT_SECRET`

**Vault path convention:**
- **CLI access:** `secret/<app>/<secret-type>` (e.g., `secret/readest/jwt`, `secret/minio/users`)
- **ExternalSecret reference:** `/<app>/<secret-type>` (without `secret/` prefix)
- The ClusterSecretStore has `secret/` as the base path, so ExternalSecrets omit it

**Examples:**
- CLI: `vault kv get secret/minio/users` → ExternalSecret: `key: /minio/users`
- CLI: `vault kv put secret/readest/jwt secret=...` → ExternalSecret: `key: /readest/jwt`
- CLI: `vault kv patch secret/minio/users readestPassword=...` (add to existing secret)

**Common paths:**
- `secret/readest/jwt` (secret, anon_key, service_role_key)
- `secret/immich/jwt` (secret)
- `secret/minio/users` (rootUser, rootPassword, veleroPassword, kaniopPassword, readestPassword)
- `secret/nextcloud/admin` (username, password)
- `secret/nextcloud/smtp` (username, password, host)

### 2.6 Image Pinning

**Always pin image tags.** Never use `latest`.

**Pattern:**
```yaml
image:
  # renovate: datasource=docker depName=ghcr.io/readest/readest
  repository: ghcr.io/readest/readest
  tag: 0.9.20
```

**Finding the right tag:**
- Check GitHub releases: `https://github.com/<org>/<repo>/releases`
- Check container registry: `ghcr.io/<org>/<repo>/tags` or Docker Hub
- Use the latest stable release, not pre-release/RC versions

**Renovate hints:**
- Add `# renovate: datasource=docker depName=<image>` above the `repository:` line
- This enables automatic PR updates when new versions are released
- For non-standard registries, add `registryUrl=https://ghcr.io`

## Phase 3: File Structure

### 3.1 Standard Directory Layout

```
apps/<app-name>/
├── Chart.yaml                          # Helm chart metadata + dependencies
├── values.yaml                         # App configuration (controllers, services, ingress, persistence)
└── templates/
    ├── postgresql.yaml                 # Zalando CR (if using Zalando)
    ├── external-secrets.yaml           # Vault secrets (if needed)
    ├── kanidm-oauth2-client.yaml       # OIDC client (if using OIDC)
    ├── kanidm-group.yaml               # OIDC group (if using OIDC)
    ├── pvc.yaml                        # PersistentVolumeClaims (if needed)
    ├── snapshots.yaml                  # ZFS snapshot schedule (if needed)
    └── configmap-*.yaml                # ConfigMaps (if needed)
```

### 3.2 Chart.yaml

```yaml
apiVersion: v2
name: <app-name>
version: 0.0.0
dependencies:
  - name: app-template
    version: 5.1.0
    repository: https://bjw-s-labs.github.io/helm-charts/
```

### 3.3 values.yaml Structure

```yaml
app-template:
  controllers:
    <app-name>:
      labels:
        backup/retain: weekly           # Backup retention policy
      type: statefulset                 # or Deployment
      containers:
        <app-name>:
          image:
            # renovate: datasource=docker depName=<image>
            repository: <image>
            tag: <version>
          env:
            # App-specific environment variables
          probes:
            liveness: &probes
              enabled: true
              custom: true
              spec:
                httpGet:
                  path: /healthz
                  port: <port>
                initialDelaySeconds: 0
                periodSeconds: 10
                timeoutSeconds: 1
                failureThreshold: 3
            readiness: *probes
            startup:
              enabled: true
              custom: true
              spec:
                httpGet:
                  path: /healthz
                  port: <port>
                failureThreshold: 30
                periodSeconds: 10
          resources:
            requests:
              cpu: 10m
              memory: 128Mi
            limits:
              memory: 512Mi

  service:
    <app-name>:
      controller: <app-name>
      ports:
        http:
          port: <port>

  ingress:
    <app-name>:
      enabled: true
      className: nginx-internal        # or nginx-external
      annotations:
        external-dns.alpha.kubernetes.io/enabled: "true"
        cert-manager.io/cluster-issuer: letsencrypt-prod-dns
      hosts:
        - host: &host <app>.grigri.cloud
          paths:
            - path: /
              pathType: Prefix
              service:
                identifier: <app-name>
                port: http
      tls:
        - hosts:
            - *host
          secretName: <app>-tls-certificate

  defaultPodOptions:
    securityContext:
      runAsUser: <uid>
      runAsGroup: <gid>
      fsGroup: <gid>
      fsGroupChangePolicy: "OnRootMismatch"
    enableServiceLinks: false
    # nodeSelector:
    #   kubernetes.io/hostname: <node>

  persistence:
    config:
      enabled: true
      globalMounts:
        - path: /config
      existingClaim: config-<app>
```

### 3.4 PVC Pattern

```yaml
apiVersion: v1
kind: PersistentVolumeClaim
metadata:
  labels:
    app.kubernetes.io/instance: <app>
    app.kubernetes.io/name: <app>
    backup: <app>-zfs                   # For snapshot schedule selector
    backup/retain: weekly
  annotations:
    argocd.argoproj.io/sync-options: Prune=false   # NEVER auto-delete PVCs
  name: config-<app>
spec:
  accessModes:
    - ReadWriteOnce
  resources:
    requests:
      storage: 5Gi
```

### 3.5 Snapshot Schedule Pattern

```yaml
apiVersion: snapscheduler.backube/v1
kind: SnapshotSchedule
metadata:
  name: <app>-backups
spec:
  retention:
    maxCount: 20
  schedule: "0 1 * * *"               # Daily at 01:00 UTC
  claimSelector:
    matchLabels:
      backup: <app>-zfs                # Matches PVC label
```

## Phase 4: Validation and Deployment

### 4.1 Pre-deployment Checklist

- [ ] All image tags are pinned (no `latest`)
- [ ] Renovate hints added above image repositories
- [ ] PVCs have `Prune=false` annotation
- [ ] PVCs have `backup:` and `backup/retain:` labels
- [ ] Ingress annotations include `cert-manager.io/cluster-issuer`
- [ ] ExternalSecrets reference correct Vault paths
- [ ] OIDC redirect URLs match the app's callback path
- [ ] Resource requests and limits are defined
- [ ] Probes are configured (liveness, readiness, startup)

### 4.2 Validation Commands

```bash
# Build chart dependencies
helm dependency build apps/<app>/

# Template the chart (validate YAML syntax)
helm template --include-crds --namespace <app> <release-name> apps/<app>/

# Lint the chart (validate Helm best practices)
helm lint apps/<app>/

# Check for common issues
# - Missing image tags
# - Incorrect service references in ingress
# - Missing probe configurations
```

### 4.3 Deployment Steps

1. **Create Vault secrets** (if needed)
   ```bash
   vault login
   vault kv put secret/<app>/jwt secret=<value> anon_key=<value>
   ```

2. **Update shared infrastructure** (if needed)
   - Add bucket/user to MinIO
   - Add ExternalSecret entry for new credentials

3. **Create app files**
   - `Chart.yaml`
   - `values.yaml`
   - `templates/` (postgresql, external-secrets, kanidm, pvc, snapshots)

4. **Validate**
   ```bash
   helm dependency build apps/<app>/
   helm template --include-crds --namespace <app> <app> apps/<app>/
   helm lint apps/<app>/
   ```

5. **Commit and push**
   ```bash
   git add apps/<app>/
   git commit -m "<app>: Add <app> deployment"
   git push
   ```

6. **Verify ArgoCD sync**
   ```bash
   kubectl --context=grigri get application <app> -n argocd
   kubectl --context=grigri get pods -n <app>
   ```

7. **Test the application**
   - Access the ingress URL
   - Verify OIDC login works
   - Check logs for errors

## Phase 5: Common Pitfalls

### 5.1 Database Issues

**Zalando rejects hyphenated database names**
- Workaround: Create databases manually via `kubectl exec psql`
- See: `docs/troubleshooting/radarr-sqlite-to-postgres.md`

**Supabase apps need auth schema**
- Use Zalando for the database + init container for Supabase bootstrap
- See: `docs/deployment/readest.md`

**Custom Postgres extensions not in Spilo image**
- Use custom Spilo image (see immich) or container Postgres
- See: `apps/immich/templates/postgresql.yaml`

### 5.2 OIDC Issues

**Redirect URL mismatch**
- Check the app's documentation for the exact callback path
- Common patterns: `/oauth/callback`, `/auth/callback`, `/auth/login`
- kaniop `strictRedirectUrl: true` rejects mismatched URLs

**OIDC client credentials not generated**
- kaniop creates `<app>-kanidm-oauth2-credentials` secret automatically
- Wait for the secret to appear: `kubectl get secret -n <app> <app>-kanidm-oauth2-credentials`

### 5.3 Ingress Issues

**Path-based routing conflicts**
- nginx ingress evaluates paths in order (longest match wins)
- Put more specific paths first (`/auth/v1` before `/`)
- See: `docs/deployment/readest.md` for multi-path example

**Large file uploads fail**
- Add `nginx.ingress.kubernetes.io/proxy-body-size: "0"` annotation
- Default is 1MB

### 5.4 MinIO Issues

**Presigned URLs don't work / external downloads fail**
- The browser fetches presigned URLs directly, so `S3_PUBLIC_ENDPOINT` must be an
  **internet-reachable** host. The internal MinIO host (`s3.internal.grigri.cloud`) is
  `nginx-internal` only — external clients can't reach it.
- For browser-facing storage use the public MinIO (`apps/s3-public/`):
  - In-cluster endpoint (`S3_ENDPOINT`): `http://s3-public-minio.s3-public.svc:9000`
  - External endpoint (`S3_PUBLIC_ENDPOINT`): `https://s3.grigri.cloud`
- For server-side-only storage the internal MinIO is fine:
  - In-cluster: `http://minio.minio.svc:9000`, external: `https://s3.internal.grigri.cloud`
- See `docs/deployment/s3-public.md` and section 2.2.

**Bucket creation fails**
- MinIO Helm chart creates buckets on startup
- Check bucket name is lowercase, no special characters
- Verify policy syntax (ARN format: `arn:aws:s3:::<bucket>/*`)

### 5.5 Image Tag Issues

**Using `latest` tag**
- Always pin to a specific version
- Add Renovate hint for automatic updates
- Check GitHub releases for latest stable version

**Renovate doesn't detect updates**
- Add `# renovate: datasource=docker depName=<image>` above `repository:`
- For non-Docker Hub registries, add `registryUrl=https://ghcr.io`

## Phase 6: Documentation

After deployment, document:

1. **Deployment guide** in `docs/deployment/<app>.md`
   - Architecture diagram
   - Controllers and their purposes
   - Database/storage configuration
   - OIDC integration details
   - Ingress routing
   - Secrets required
   - Deployment steps

2. **Troubleshooting** in `docs/troubleshooting/<app>-<issue>.md` (if issues arise)
   - Problem description
   - Root cause
   - Diagnosis commands
   - Fix/workaround

3. **AGENTS.md** — Add one-line pitfall if non-obvious behavior discovered
   - Link to troubleshooting doc
   - Example: "Readest needs Supabase auth schema — use Zalando + init container"

## References

- **Helm conventions**: `docs/conventions/helm.md`
- **Cilium networking**: `docs/conventions/cilium.md`
- **Documenting learnings**: `docs/conventions/documenting-learnings.md`
- **Example deployments**: `docs/deployment/hermes-agent.md`, `docs/deployment/unifi.md`
- **Zalando Postgres patterns**: `apps/immich/templates/postgresql.yaml`, `apps/dawarich/templates/postgresql.yaml`
- **MinIO patterns**: `platform/minio/values.yaml`
- **OIDC patterns**: `apps/immich/templates/kanidm-oauth2-client.yaml`
