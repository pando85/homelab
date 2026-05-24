# Helm Chart Conventions

## Chart.yaml Pattern

```yaml
apiVersion: v2
name: <app-name>
version: 0.0.0
dependencies:
  - name: app-template
    version: 4.6.2
    repository: https://bjw-s-labs.github.io/helm-charts/
```

## values.yaml Patterns

- Use `app-template` from bjw-s as the standard wrapper
- Always define `resources.requests` and `resources.limits`
- Use YAML anchors for repeated probe definitions (`&probes`, `*probes`)
- Set `enableServiceLinks: false` in `defaultPodOptions`
- Use `fsGroupChangePolicy: "OnRootMismatch"` for performance

## Standard Labels

```yaml
labels:
  app.kubernetes.io/instance: <name>
  app.kubernetes.io/name: <name>
  backup: <snapshot-name>
  backup/retain: quarterly|monthly|weekly
```

## PVC Sync Protection

```yaml
annotations:
  argocd.argoproj.io/sync-options: Prune=false
```

## Ingress Annotations

Internal:

```yaml
annotations:
  external-dns.alpha.kubernetes.io/enabled: "true"
  cert-manager.io/cluster-issuer: letsencrypt-prod-dns
```

External:

```yaml
annotations:
  external-dns.alpha.kubernetes.io/enabled: "true"
  external-dns.alpha.kubernetes.io/target: grigri.cloud
  cert-manager.io/cluster-issuer: letsencrypt-prod-dns
```

## External Secrets

```yaml
apiVersion: external-secrets.io/v1
kind: ExternalSecret
metadata:
  name: <app-name>
spec:
  secretStoreRef:
    kind: ClusterSecretStore
    name: vault
  target:
    name: <app-name>
  data:
    - secretKey: ENV_VAR_NAME
      remoteRef:
        key: /<app-name>/<path>
        property: <field>
```

## Renovate Integration

Always add Renovate hints above image references:

```yaml
# renovate: datasource=docker depName=ghcr.io/OWNER/IMAGE
image:
  repository: ghcr.io/owner/image
  tag: 1.2.3
```

For custom image fields:

```yaml
# renovate: datasource=docker depName=postgres
dockerImage: postgres:16
```

## Adding a New App

1. Create `apps/<name>/` with `Chart.yaml`, `values.yaml`, `templates/`
2. For upstream charts: add dependency block, run `helm dependency build`, commit `Chart.lock`
3. Namespace equals folder name (auto-created by deploy script)
4. Pin all images with Renovate hints
5. Add Ingress with cert-manager + external-dns annotations
6. Define resource requests and limits
7. Create ExternalSecret objects for secrets (source: Vault path)
8. Create PVC with `Prune=false` annotation for persistent data
