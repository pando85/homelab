# Firecrawl Self-Hosted

Despliegue de Firecrawl en Kubernetes para usar como backend de `web_extract` y `web_search` en Hermes Agent.

## Arquitectura

```
┌─────────────────────────────────────────────────────────┐
│                    Firecrawl Stack                       │
├─────────────────────────────────────────────────────────┤
│  API (puerto 3002)                                      │
│    ↓                                                     │
│  Workers (queue-worker + nuq-worker x2)                │
│    ↓                                                     │
│  Playwright Service (scraping con browser)              │
│    ↓                                                     │
│  Redis (rate limiting + cache)                          │
│  Zalando PostgreSQL (queue backend)                    │
│  SearXNG (búsqueda web - existente en el cluster)      │
└─────────────────────────────────────────────────────────┘
```

## Componentes

- **API**: Servicio principal en puerto 3002
- **Workers**: Procesan trabajos de scraping en cola
- **Playwright**: Browser headless para scraping dinámico
- **Redis**: Rate limiting y cache (container)
- **PostgreSQL**: Backend de colas NuQ (Zalando operator)
- **SearXNG**: Búsqueda web (integrado con el existente)

## Recursos

| Componente | RAM Request | RAM Limit | CPU Request | CPU Limit |
|------------|-------------|-----------|-------------|-----------|
| API | 1Gi | 2Gi | 500m | 1000m |
| Worker | 1Gi | 2Gi | 500m | 1000m |
| NuQ Worker x2 | 2Gi | 4Gi | 1000m | 2000m |
| Playwright | 512Mi | 1Gi | 250m | 500m |
| Redis | 128Mi | 256Mi | 100m | 200m |
| PostgreSQL | 256Mi | 512Mi | 200m | 400m |
| **TOTAL** | **~5Gi** | **~10Gi** | **~2.5 cores** | **~5 cores** |

## Despliegue

```bash
# Build dependencies
helm dependency build apps/firecrawl/

# Template (validate)
helm template --include-crds --namespace firecrawl firecrawl apps/firecrawl/

# Lint
helm lint apps/firecrawl/

# Commit y push (ArgoCD sincroniza automáticamente)
git add apps/firecrawl/
git commit -m "firecrawl: Add self-hosted Firecrawl deployment"
git push
```

## URLs

- **Interna**: `https://firecrawl.internal.grigri.cloud`
- **Cluster**: `http://api.firecrawl.svc.cluster.local:3002`

## Configuración de Hermes

### Archivo: `~/.hermes/.env`
```bash
FIRECRAWL_API_URL=https://firecrawl.internal.grigri.cloud
```

### Archivo: `~/.hermes/config.yaml`
```yaml
web:
  extract_backend: firecrawl
  search_backend: firecrawl
```

## Verificación

```bash
# Health check
kubectl port-forward svc/api 3002:3002 -n firecrawl
curl https://firecrawl.internal.grigri.cloud/v0/health/readiness

# Test scraping
curl -X POST https://firecrawl.internal.grigri.cloud/v2/scrape \
  -H 'Content-Type: application/json' \
  -d '{"url":"https://example.com","formats":["markdown"]}'
```

## Operadores Utilizados

- **Zalando Postgres Operator**: Para la base de datos de colas
- **app-template (bjw-s)**: Helm chart template
- **Snapscheduler**: Backups ZFS automáticos

## Integración con SearXNG

Firecrawl usa el SearXNG existente en el cluster:
- Endpoint: `http://searxng.searxng.svc.cluster.local:8080`
- Engines: google, bing, duckduckgo

## Renovate

Las imágenes tienen hints de Renovate para actualizaciones automáticas:
- `ghcr.io/firecrawl/firecrawl`
- `ghcr.io/firecrawl/playwright-service`
- `docker.io/library/redis`

## Troubleshooting

```bash
# Ver logs del API
kubectl logs -n firecrawl deployment/firecrawl-api -f

# Ver estado de PostgreSQL
kubectl get postgresql -n firecrawl

# Ver pods
kubectl get pods -n firecrawl

# Ver servicios
kubectl get svc -n firecrawl
```
