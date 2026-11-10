# Helm Chart Conventions

## Chart Selection

Prefer maintained official/first-party Helm charts when available. Use `app-template` from bjw-s only when no maintained official chart exists for the application. If an official chart cannot support the needed deployment mode, document the limitation and obtain owner approval rather than silently falling back to a generic wrapper. Always pin chart versions and use wrapper charts for homelab-specific additions (routes, ESO, Kanidm, etc.).

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

- Use `app-template` from bjw-s only when no maintained official chart exists
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

## Routes (Gateway API) and DNS Annotations

Apps are exposed with Gateway API `HTTPRoute`s on the Envoy Gateways. There are no `Ingress`
objects and no `ingress:` values block (`ingress.enabled: false` on charts that default to it).
Full procedure, backend TLS, Lua, NetworkPolicy and verification:
[`gateway-api-routes.md`](gateway-api-routes.md).

External route (the target comes from the external Gateway, never from the route):

```yaml
annotations:
  external-dns.kubernetes.io/enabled: "true"
```

Internal route: the same annotation (the record is an A record to the internal Gateway address).
Routes carry no `cert-manager.io/*` annotation: certificates live on the Gateways.

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
5. Add an HTTPRoute on the right Envoy Gateway ([`gateway-api-routes.md`](gateway-api-routes.md)) with the external-dns annotation; protected apps also get a `SecurityPolicy`
6. Define resource requests and limits
7. Create ExternalSecret objects for secrets (source: Vault path)
8. Create PVC with `Prune=false` annotation for persistent data
