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
SEARXNG_URL=http://searxng.searxng.svc.cluster.local:8080
```

### Archivo: `~/.hermes/config.yaml`
```yaml
web:
  backend: firecrawl
  search_backend: firecrawl
  extract_backend: firecrawl
  use_gateway: true
```

`FIRECRAWL_API_URL` es suficiente sin API key cuando la autenticación upstream está deshabilitada
(`USE_DB_AUTHENTICATION=false`). `is_available()` acepta la URL sola.

## Notas de despliegue

- **Imágenes fijadas** a `2.10.19` con hints de Renovate. `firecrawl/playwright-service` solo
  publica `latest`, así que esa imagen no se puede fijar por versión.
- **Schema nuq**: el esquema `nuq` no lo crea la app — upstream lo provisiona con una imagen
  Postgres custom. En Zalando/Spilo nadie lo ejecuta, así que se usa un init container
  `nuq-schema-init` que aplica `files/nuq.sql`. En un bump de versión, Renovate ejecuta
  `apps/firecrawl/hack/update-nuq-sql.sh` automáticamente; guards de pre-commit y deploy-time
  verifican la sincronización.
- **`cron.database_name`**: los 36 jobs de `pg_cron` en `nuq.sql` requieren
  `cron.database_name=firecrawl` en el CR de Zalando. Es un GUC que requiere restart — borrar el
  pod de postgres una vez después de aplicar el cambio.

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

## Aislamiento de egreso (SSRF)

Firecrawl OSS no filtra direcciones privadas: `validateUrl.ts` solo valida la forma de la URL y
`threat-protection`/`blocklist` son listas de reputación por dominio que además requieren
`USE_DB_AUTHENTICATION`. El control está por tanto en `values.yaml` → `app-template.networkpolicies`
(NetworkPolicy nativo, **solo `Egress`**; el ingreso no cambia, el aislamiento de ingreso sigue
apagado en el cluster — ver `docs/conventions/deploying-new-apps.md`).

| Política | Pods | Destinos permitidos |
|----------|------|---------------------|
| `firecrawl-fetchers` | api, worker, nuq-worker | DNS (node-local-dns y kube-dns, 53), redis 6379, `cluster-name=firecrawl-postgres` 5432, playwright 3000, internet TCP 80/443 |
| `firecrawl-search` | api | searxng 8080 (solo el API llama a `executeSearch`) |
| `firecrawl-browser` | playwright | DNS, internet TCP 80/443 |

El `ipBlock` de internet excluye 10.0.0.0/8 (pods **y** servicios 10.43.0.0/16, que caen dentro),
172.16.0.0/12, 192.168.0.0/16 (LAN y VLANs), 169.254.0.0/16 (link-local, incluido
169.254.169.254), 127.0.0.0/8, 100.64.0.0/10 y los rangos reservados restantes. No hay ninguna
regla IPv6: los `ipBlock` son IPv4, así que todo destino IPv6 queda denegado por defecto.

Cilium evalúa el egreso **después** del DNAT de servicio, así que los pares tienen que ser
selectores de pod/namespace del backend y nunca un ClusterIP. Lo mismo aplica a DNS: el
`CiliumLocalRedirectPolicy` de `system/kube-system/resources/nodelocaldns/` redirige la dirección
del servicio `kube-dns` al pod del caché local, por eso la política selecciona ese pod.

Riesgos residuales que esto **no** cierra:

- **Loopback dentro del pod**: NetworkPolicy no ve el tráfico de un contenedor a su propio
  `127.0.0.1`. Un exploit dentro del contenedor `app` sigue alcanzando el listener del propio pod
  (3002/3005/3006/3000). Mitigación: no activar `ADMIN_INTERNAL_PORT` (upstream lo recomienda solo
  detrás de una network policy) — hoy no está puesto. Separar el fetch del resto en contenedores
  distintos no es posible con el runtime upstream.
- **Dependencias permitidas como pivote**: redis no tiene contraseña y el pod SearXNG también hace
  peticiones salientes; su `/image_proxy` reenvía URLs, pero exige un HMAC del `secret_key` de
  SearXNG, así que no es un proxy abierto para quien solo llama a Firecrawl.
- **Túnel desde internet**: un host público puede reenviar a la LAN (VPN, rebotes tipo
  `localhost.run`). Un filtro L3 no lo distingue; haría falta un proxy de egreso con política por
  nombre resuelto.
- **API sin autenticación** (`USE_DB_AUTHENTICATION=false`): cualquiera que llegue al ingress sigue
  pudiendo pedir URLs públicas. La política reduce el alcance (solo 80/443 públicos), no convierte
  el servicio en un endpoint seguro expuesto a internet.

Comprobación estática del render (sin tocar el cluster):

```bash
helm dependency build apps/firecrawl/   # Chart.lock y charts/ están en .gitignore
helm template --namespace firecrawl firecrawl apps/firecrawl \
  | python3 test/mcp-gateway/firecrawl-egress-test.py
```

Verificación tras el despliegue (observacional, no destructiva). El script hace primero una
**línea base pública** —salud, un scrape de `example.com`, un `search` (SearXNG) y un `crawl` de una
sola página inicial (cola de worker)— y solo si todo pasa afirma algo sobre los destinos privados.
Si la línea base falla, el resultado es `INCONCLUSIVE` y sale con código distinto de cero: un
destino privado que no responde tampoco diría nada si el scrape público tampoco funciona.

Cilium descarta los paquetes denegados en vez de rechazarlos (`policyDenyResponse: none`), así que
un destino bloqueado se manifiesta como job que agota su propio `timeout` (`SCRAPE_TIMEOUT_MS`, por
debajo del tope de espera `CURL_MAX_TIME`). Como un scrape self-hosted intenta varios motores en
orden —`playwright` (calidad 20) y luego el `fetch` HTTP en el propio pod, ver
`scraper/scrapeURL/engines/index.ts` de la versión fijada—, cada sondas negativa recorre el pod del
navegador **y** el de los fetchers. Aun así una negativa solo cuenta como prueba si su error tiene
firma de red (`DENIAL_RE`); un 500 o un error de cola se marca `INCONCLUSIVE`, nunca `PASS`.

Los destinos son rutas de salud o listados sin secretos y sin efectos: gateway de la LAN,
`169.254.169.254/latest/meta-data/`, el ClusterIP de `kube-dns`, `idm.grigri.cloud/status`, el
propio `/v0/health/readiness` por hairpin y `vault.internal.grigri.cloud/v1/sys/health` (salud
pública de Vault, no devuelve material cifrado). No se imprimen cuerpos ni tokens; del fallo solo se
muestra una línea saneada con el error del job.

Casos que el script **no** prueba: loopback mismo pod, IPv6 (no hay regla, nada que alcanzar),
profundidad más allá de la página inicial, y si redis/postgres/SearXNG/DNS siguen accesibles —eso lo
afirma el test estático del render, junto a la ausencia de reglas de ingreso.

```bash
bash apps/firecrawl/hack/verify-egress.sh   # BASE_URL por defecto: ingress interno
```

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
