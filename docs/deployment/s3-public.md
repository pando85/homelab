# s3-public (Public MinIO)

A dedicated, **externally-exposed** MinIO instance for apps that need to serve object storage
content to clients outside the cluster (e.g. Readest book downloads via presigned URLs).

**See `docs/deployment/minio-architecture.md`** for the complete three-tier MinIO architecture
(internal, public, cross-backups) and decision tree for choosing the right instance.

This instance is separate from:
- **`platform/minio/`** — internal-only MinIO for cluster backups (Velero, Kaniop). Host
  `s3.internal.grigri.cloud`, `nginx-internal` ingress. **Not** reachable from the internet.
- **`apps/cross-backups/`** — external MinIO that *receives* backups from other machines. Host
  `cross-backups.grigri.cloud`.

## Why a separate instance?

Apps like Readest generate **presigned S3 URLs** that the *browser* fetches directly. Those URLs
must resolve to a publicly-reachable host. The internal MinIO (`s3.internal.grigri.cloud`) is only
reachable inside the cluster, so external clients get connection errors when downloading books.

Rather than exposing the backup MinIO to the internet (large blast radius), we run a dedicated
public instance that only holds user-facing app content.

## Architecture

```
Browser (external)
  └── https://s3.grigri.cloud/...        (presigned URLs)  ──► s3-public MinIO :9000
  └── https://mc-s3.grigri.cloud/        (console, OIDC)   ──► s3-public MinIO :9001

In-cluster app (e.g. readest-client)
  └── http://s3-public-minio.s3-public.svc:9000            ──► s3-public MinIO :9000
```

- **Namespace:** `s3-public`
- **Node:** pinned to `prusik` (via `nodeSelector`), alongside `ingress-nginx-external` and
  `cross-backups` MinIO.
- **Storage:** ZFS PVC `s3-public` (500Gi, `openebs-zfspv`, expandable).
- **Ingress:** `nginx-external` for both S3 API and console.

## Endpoints

| Purpose | URL | Ingress class |
|---|---|---|
| S3 API | `https://s3.grigri.cloud` | nginx-external |
| MinIO Console | `https://mc-s3.grigri.cloud` | nginx-external |
| In-cluster S3 | `http://s3-public-minio.s3-public.svc:9000` | — |

## Console OIDC

The console uses Kanidm OIDC (`s3-public` client, `s3-public-users` group), same pattern as the
internal MinIO. Members of `s3-public-users` get `consoleAdmin` via
`MINIO_IDENTITY_OPENID_ROLE_POLICY`.

## Secrets (Vault)

| Vault path | Keys |
|---|---|
| `/s3-public/users` | `rootUser`, `rootPassword`, `readestPassword` |

`readestPassword` is the S3 secret key for the `readest` access key (scoped to the `readest-files`
bucket via `readestPolicy`).

### One-time Vault setup

```bash
export VAULT_ADDR=https://vault.internal.grigri.cloud
vault login

# Generate strong credentials
ROOT_USER="s3publicadmin"
ROOT_PASS=$(openssl rand -base64 32 | tr -d '/+=' | cut -c1-40)
READEST_PASS=$(openssl rand -base64 32 | tr -d '/+=' | cut -c1-40)

vault kv put secret/s3-public/users \
  rootUser="$ROOT_USER" \
  rootPassword="$ROOT_PASS" \
  readestPassword="$READEST_PASS"
```

## Buckets & users

| Bucket | User (access key) | Policy | Used by |
|---|---|---|---|
| `readest-files` | `readest` | `readestPolicy` (rw on `readest-files/*`) | Readest |

To add a new app: add a bucket, a policy, and a user to `apps/s3-public/values.yaml`, add the
password to Vault `/s3-public/users`, and add the key to
`apps/s3-public/templates/external-secrets-users.yaml`.

## App integration (Readest example)

```yaml
# apps/readest/values.yaml — client container env
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

`S3_ENDPOINT` is the in-cluster service (fast, no TLS, server-side uploads). `S3_PUBLIC_ENDPOINT`
is the external host baked into presigned URLs the browser fetches.

## Deployment steps

1. Create the Vault secret (see above).
2. Commit `apps/s3-public/` — ArgoCD creates the namespace, PVC, MinIO, ingresses, OIDC client.
3. Wait for the pod + TLS certs:
   ```bash
   kubectl --context=grigri get pods,ingress -n s3-public
   ```
4. Add your user to the `s3-public-users` Kanidm group for console access.
5. Point the consuming app (Readest) at the new endpoints and commit.
6. Smoke-test: upload from the app, then confirm the presigned URL host is `s3.grigri.cloud` and
   loads from an external network.

## Files

```
apps/s3-public/
├── Chart.yaml                          # minio chart dependency (5.4.0)
├── values.yaml                         # MinIO config, external ingress, OIDC, readest bucket/user
└── templates/
    ├── pvc.yaml                        # ZFS PVC (500Gi)
    ├── external-secrets-users.yaml     # Vault /s3-public/users
    ├── kanidm-groups.yaml              # s3-public-users group
    └── kanidm-oauth2-client.yaml       # console OIDC client
```
