# MinIO Architecture

The homelab runs **three separate MinIO instances**, each with a distinct purpose and network exposure. This separation minimizes blast radius and keeps backup infrastructure isolated from user-facing services.

## Overview

| Instance | Path | Host | Network Exposure | Purpose |
|----------|------|------|------------------|---------|
| **Internal** | `platform/minio/` | `s3.internal.grigri.cloud` | Cluster-only (`nginx-internal`) | In-cluster object storage, Velero backups, Kaniop backups |
| **Public** | `apps/s3-public/` | `s3.grigri.cloud` | Internet-facing (`nginx-external`) | Apps that serve presigned URLs to browsers (e.g., Readest book downloads) |
| **Cross-backups** | `apps/cross-backups/` | `cross-backups.grigri.cloud` | Internet-facing (`nginx-external`) | Receiving backups pushed from external machines |

## Internal MinIO (`platform/minio/`)

**Purpose:** Cluster-internal object storage and backup targets.

**Characteristics:**
- Namespace: `minio`
- Host: `s3.internal.grigri.cloud`
- Ingress class: `nginx-internal` (cluster-only, not reachable from internet)
- Storage: ZFS-backed PVC
- Authentication: Kanidm OIDC for console access

**Used by:**
- **Velero** — cluster backup storage
- **Kaniop** — Kanidm backup storage
- Apps that need server-side-only object storage (no browser access)

**When to use:**
- Your app needs S3-compatible storage but only for server-side I/O
- The storage is for backups, logs, or internal data processing
- No external clients need to fetch objects directly

**Example apps using internal MinIO:**
- Velero (cluster backups)
- Kaniop (Kanidm backups)

**Adding a bucket/user:**
```yaml
# platform/minio/values.yaml
buckets:
  - name: <app>-data
    policy: none

policies:
  - name: <app>Policy
    statements:
      - resources: ['arn:aws:s3:::<app>-data/*']
        actions: ['s3:GetObject', 's3:PutObject', 's3:DeleteObject']

users:
  - accessKey: <app>
    existingSecret: minio-users
    existingSecretKey: <app>Password
    policy: <app>Policy
```

Add `<app>Password` to Vault `/minio/users` and update `platform/minio/templates/external-secrets-users.yaml`.

## Public MinIO (`apps/s3-public/`)

**Purpose:** Object storage for apps that serve presigned URLs to external clients (browsers).

**Characteristics:**
- Namespace: `s3-public`
- Host: `s3.grigri.cloud` (S3 API), `mc-s3.grigri.cloud` (console)
- Ingress class: `nginx-external` (internet-facing)
- Storage: ZFS-backed PVC (500Gi)
- Authentication: Kanidm OIDC for console access (`s3-public-users` group)
- Node: Pinned to `prusik`

**Used by:**
- **Readest** — book file storage with presigned download URLs

**When to use:**
- Your app generates presigned S3 URLs that browsers fetch directly
- External clients need to download/upload objects via S3
- You need internet-accessible object storage

**Why separate from internal MinIO?**
- Presigned URLs must resolve to an internet-reachable host
- Internal MinIO (`s3.internal.grigri.cloud`) is cluster-only — external clients get connection errors
- Exposing the backup MinIO to the internet increases blast radius
- Public MinIO only holds user-facing app content, not critical backups

**App integration pattern:**
```yaml
# apps/<app>/values.yaml
env:
  S3_ENDPOINT: http://s3-public-minio.s3-public.svc:9000   # in-cluster (server-side I/O)
  S3_PUBLIC_ENDPOINT: https://s3.grigri.cloud              # external (presigned URLs)
  S3_BUCKET_NAME: <app>-files
  S3_ACCESS_KEY_ID: <app>
  S3_SECRET_ACCESS_KEY:
    valueFrom:
      secretKeyRef:
        name: <app>-minio-secret
        key: <app>Password
```

- `S3_ENDPOINT` — in-cluster service for server-side uploads/downloads (fast, no TLS overhead)
- `S3_PUBLIC_ENDPOINT` — external host baked into presigned URLs (browser fetches this)

**Adding a bucket/user:**
See `docs/deployment/s3-public.md` for the full pattern.

## Cross-backups MinIO (`apps/cross-backups/`)

**Purpose:** Receiving backups pushed from external machines (other homelabs, remote servers).

**Characteristics:**
- Namespace: `cross-backups`
- Host: `cross-backups.grigri.cloud`
- Ingress class: `nginx-external` (internet-facing)
- Storage: ZFS-backed PVC
- Authentication: Kanidm OIDC for console access

**Used by:**
- External machines pushing backups via `restic`, `borg`, or `mc mirror`
- Off-site backup storage

**When to use:**
- You need to receive backups from machines outside the cluster
- You want a dedicated backup target separate from internal storage
- You need internet-accessible storage for backup tools

**Why separate from public MinIO?**
- Cross-backups is purely for backup ingestion, not app data
- Separates backup traffic from app traffic
- Different retention policies and access patterns
- Easier to audit and monitor backup storage independently

## Decision Tree

```
Does your app need S3-compatible object storage?
├── YES → Who fetches the objects?
│   ├── Browser/external client (presigned URLs)
│   │   └── Use apps/s3-public/ (public MinIO)
│   │       - S3_ENDPOINT: http://s3-public-minio.s3-public.svc:9000
│   │       - S3_PUBLIC_ENDPOINT: https://s3.grigri.cloud
│   │
│   ├── Server-side only (app reads/writes, no browser access)
│   │   └── Use platform/minio/ (internal MinIO)
│   │       - Endpoint: http://minio.minio.svc:9000
│   │
│   └── Receiving backups from external machines?
│       └── Use apps/cross-backups/ (cross-backups MinIO)
│           - Endpoint: https://cross-backups.grigri.cloud
│
└── NO → Use a different storage solution (PVC, database, etc.)
```

## Common Pitfalls

### Presigned URLs don't work from external clients

**Symptom:** App uploads work, but browser can't download via presigned URL. Error: "connection refused" or timeout.

**Root cause:** `S3_PUBLIC_ENDPOINT` is set to the internal MinIO host (`s3.internal.grigri.cloud`), which is cluster-only.

**Fix:** Use the public MinIO and set `S3_PUBLIC_ENDPOINT: https://s3.grigri.cloud`. See `docs/deployment/s3-public.md`.

### Exposing internal MinIO to the internet

**Symptom:** Security concern — backup infrastructure is internet-accessible.

**Root cause:** Internal MinIO ingress was changed to `nginx-external` or a new external ingress was added.

**Fix:** Internal MinIO must use `nginx-internal` only. If you need internet-accessible storage, use `apps/s3-public/`.

### Confusing cross-backups with public MinIO

**Symptom:** App tries to use cross-backups for presigned URLs, or cross-backups is used for app data.

**Root cause:** Misunderstanding the purpose of each MinIO instance.

**Fix:**
- **Public MinIO** = app data served to browsers (presigned URLs)
- **Cross-backups** = backup ingestion from external machines
- **Internal MinIO** = cluster-internal storage and backups

## Network Architecture

```
Internet
  │
  ├── nginx-external (192.168.193.4)
  │   ├── s3.grigri.cloud → s3-public MinIO (presigned URLs)
  │   ├── mc-s3.grigri.cloud → s3-public MinIO (console)
  │   └── cross-backups.grigri.cloud → cross-backups MinIO (backup ingestion)
  │
  └── nginx-internal (192.168.193.1)
      ├── s3.internal.grigri.cloud → internal MinIO (cluster-only)
      └── <app>.internal.grigri.cloud → <app> (cluster-only)

Cluster
  ├── s3-public namespace
  │   └── s3-public-minio:9000 (in-cluster endpoint)
  │
  ├── minio namespace
  │   └── minio:9000 (in-cluster endpoint)
  │
  └── cross-backups namespace
      └── cross-backups-minio:9000 (in-cluster endpoint)
```

## Secrets (Vault)

| Vault Path | Instance | Keys |
|------------|----------|------|
| `/minio/users` | Internal | `rootUser`, `rootPassword`, `veleroPassword`, `kaniopPassword`, `<app>Password` |
| `/s3-public/users` | Public | `rootUser`, `rootPassword`, `readestPassword`, `<app>Password` |
| `/cross-backups/users` | Cross-backups | `rootUser`, `rootPassword`, `<app>Password` |

## Files

```
platform/minio/
├── Chart.yaml
├── values.yaml                         # Internal MinIO config, buckets, users
└── templates/
    ├── pvc.yaml
    ├── external-secrets-users.yaml     # Vault /minio/users
    └── kanidm-oauth2-client.yaml       # Console OIDC

apps/s3-public/
├── Chart.yaml
├── values.yaml                         # Public MinIO config, buckets, users
└── templates/
    ├── pvc.yaml
    ├── external-secrets-users.yaml     # Vault /s3-public/users
    ├── kanidm-oauth2-client.yaml       # Console OIDC
    └── kanidm-groups.yaml              # s3-public-users group

apps/cross-backups/
├── Chart.yaml
├── values.yaml                         # Cross-backups MinIO config
└── templates/
    ├── pvc.yaml
    ├── external-secrets-users.yaml     # Vault /cross-backups/users
    └── kanidm-oauth2-client.yaml       # Console OIDC
```

## References

- **Public MinIO deployment**: `docs/deployment/s3-public.md`
- **Readest integration example**: `docs/deployment/readest.md`
- **Decision tree for new apps**: `docs/conventions/deploying-new-apps.md` §2.2
