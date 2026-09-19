# Grafana MCP Server Deployment with Kanidm Authentication

## Overview

The Grafana MCP (Model Context Protocol) server provides AI agents with access to Grafana dashboards, metrics, and logs. Authentication uses Kanidm service accounts with JWT token exchange to integrate with Grafana's `auth.jwt` module.

### Architecture

```
┌─────────────────┐
│  Kanidm IdP     │
│  Service Account│
└────────┬────────┘
         │ API Token (long-lived)
         ↓
┌─────────────────┐
│  Token Exchange │
│  (RFC 8693)     │
└────────┬────────┘
         │ JWT (short-lived)
         ↓
┌─────────────────┐
│  Grafana        │
│  auth.jwt       │
└─────────────────┘
```

1. **Kanidm Service Account**: Created via `KanidmServiceAccount` CRD, provides a long-lived API token
2. **Token Exchange**: RFC 8693 OAuth 2.0 Token Exchange converts the API token to a short-lived JWT
3. **Grafana Validation**: Grafana's `auth.jwt` module validates the JWT signature and extracts user claims

### File Locations

- MCP server deployment: `apps/hermes/values.yaml` (sidecar container in Hermes pods)
- Kanidm service account: `apps/hermes/templates/kanidm-service-account.yaml`
- Grafana JWT config: `system/monitoring/values.yaml` (Grafana `auth.jwt` section)

## Gotchas and Key Learnings

### 1. Kaniop KanidmServiceAccount requires idm_admin in entryManagedBy group

**Problem**: `KanidmServiceAccount` CRD fails to create API tokens, no service account credentials are provisioned.

**Root Cause**: The Kaniop operator needs to create Kanidm API tokens via the Kanidm API, which requires `idm_admin` privileges. The `entryManagedBy` group specified in the `KanidmServiceAccount` CR must have `idm_admin` as a member.

**Fix**: Ensure the group managing the service account has `idm_admin`:

```bash
# Check group membership
kanidm group get <group-name>

# Add idm_admin if missing
kanidm group add-member <group-name> idm_admin
```

### 2. Kaniop requires serviceAccountNamespaceSelector on Kanidm CR

**Problem**: `KanidmServiceAccount` resources are created but ignored by the Kaniop operator. No `Secret` resources are generated.

**Root Cause**: The `Kanidm` CR (cluster-wide configuration) must specify which namespaces to watch for `KanidmServiceAccount` resources via `serviceAccountNamespaceSelector`. Without this, the operator doesn't process service account requests.

**Fix**: Update the `Kanidm` CR to include namespace selection:

```yaml
apiVersion: kaniop.io/v1alpha1
kind: Kanidm
metadata:
  name: kanidm
spec:
  # ... other config ...
  serviceAccountNamespaceSelector:
    matchExpressions:
      - key: kubernetes.io/metadata.name
        operator: In
        values:
          - hermes-1
          - hermes-2
          - hermes-3
```

Or use a label selector if namespaces are labeled:

```yaml
  serviceAccountNamespaceSelector:
    matchLabels:
      kaniop.io/watch: "true"
```

### 3. curlimages/curl doesn't include jq

**Problem**: JSON parsing fails in init containers or sidecars using `curlimages/curl` with errors like `jq: command not found`.

**Root Cause**: The `curlimages/curl` image is minimal and doesn't include `jq` by default.

**Fix**: Use `grep` and `cut` for simple JSON parsing, or install `jq`:

```bash
# Instead of: echo "$JSON" | jq -r '.access_token'
# Use grep/cut:
echo "$JSON" | grep -o '"access_token":"[^"]*"' | cut -d'"' -f4

# Or install jq in the container:
apk add --no-cache jq  # Alpine-based images
apt-get update && apt-get install -y jq  # Debian-based images
```

### 4. mcp-grafana image tags don't have 'v' prefix

**Problem**: Image pull fails with `manifest unknown` or `not found` errors.

**Root Cause**: The `docker.io/grafana/mcp-grafana` image uses tags without the `v` prefix (e.g., `1.5.1`, not `v1.5.1`).

**Fix**: Use the correct tag format:

```yaml
image:
  repository: docker.io/grafana/mcp-grafana
  tag: "1.5.1"  # NOT "v1.5.1"
```

### 5. mcp-grafana binary location

**Problem**: Container fails to start with `exec: "mcp-grafana": executable file not found in $PATH`.

**Root Cause**: The `mcp-grafana` binary is at `/app/mcp-grafana`, not in the system PATH.

**Fix**: Use the full path in the command:

```yaml
command:
  - /app/mcp-grafana
args:
  - -address
  - "0.0.0.0:8080"
  - -transport
  - sse
```

### 6. mcp-grafana uses -address flag, not -port

**Problem**: SSE transport fails to bind or listens on wrong interface.

**Root Cause**: The mcp-grafana server uses `-address` (host:port) for SSE transport, not separate `-host` and `-port` flags.

**Fix**: Use the correct flag:

```yaml
args:
  - -address
  - "0.0.0.0:8080"  # Full address, not just port
  - -transport
  - sse
```

### 7. StatefulSets don't auto-restart pods on template changes

**Problem**: After updating the StatefulSet spec (e.g., changing container args, env vars, or image), the running pod continues with the old configuration.

**Root Cause**: StatefulSets don't automatically restart pods when the template changes (unlike Deployments with `RollingUpdate` strategy). The pod must be deleted manually to pick up changes.

**Fix**: Delete the pod to trigger recreation with the new spec:

```bash
kubectl --context=grigri delete pod -n <namespace> <statefulset-name>-0
```

The StatefulSet controller will recreate the pod with the updated template.

### 8. Kanidm JWKS endpoint URL is not .well-known/jwks.json

**Problem**: Grafana JWT authentication fails with `invalid character 'R' looking for beginning of value` error.

**Root Cause**: The JWKS endpoint URL `https://idm.grigri.cloud/oauth2/openid/grafana/.well-known/jwks.json` returns "Route not found". Kanidm uses a different endpoint.

**Fix**: Use the correct JWKS endpoint:

```yaml
# system/monitoring/values.yaml
grafana.ini:
  auth.jwt:
    jwk_set_url: https://idm.grigri.cloud/oauth2/openid/grafana/public_key.jwk
```

Verify the endpoint returns valid JWKS:

```bash
curl -s https://idm.grigri.cloud/oauth2/openid/grafana/public_key.jwk | head -c 200
```

### 9. Kanidm access_token doesn't include user claims

**Problem**: Grafana JWT authentication fails with `Missing mandatory claim in JWT` error even though the token appears valid.

**Root Cause**: Kanidm's `access_token` from token exchange doesn't include user claims like `email` or `preferred_username`. These claims are only in the `id_token`.

**Fix**: Use `id_token` instead of `access_token` for Grafana authentication:

```bash
# In init container or refresher script
JWT=$(echo "$RESPONSE" | grep -o '"id_token":"[^"]*"' | cut -d'"' -f4)
```

The `id_token` contains all required claims:

```json
{
  "email": "grafana-mcp@grigri.cloud",
  "email_verified": true,
  "preferred_username": "grafana-mcp",
  "scopes": ["editor", "email", "openid", "profile"]
}
```

### 10. Kanidm service account needs email for Grafana email_claim

**Problem**: Grafana JWT authentication fails with `Missing mandatory claim in JWT` for the `email` claim.

**Root Cause**: The `KanidmServiceAccount` CR doesn't include a `mail` attribute, so the `id_token` doesn't contain the `email` claim required by Grafana's `auth.jwt.email_claim` configuration.

**Fix**: Add the `mail` attribute to the service account:

```yaml
# apps/hermes/templates/kanidm-service-account.yaml
apiVersion: kaniop.rs/v1beta1
kind: KanidmServiceAccount
metadata:
  name: grafana-mcp
spec:
  serviceAccountAttributes:
    displayname: Hermes Grafana MCP
    entryManagedBy: grafana-mcp-users
    mail:
      - grafana-mcp@grigri.cloud
```

### 11. Shared volume permissions between init container and sidecar

**Problem**: The refresher sidecar fails to update the JWT file with `Permission denied` error.

**Root Cause**: The init container (running as uid 100) creates the file, but the refresher (running as uid 10000) cannot overwrite it due to default file permissions.

**Fix**: Set world-writable permissions in the init container:

```yaml
# In init container script
echo -n "$JWT" > /shared/grafana-jwt
chmod 666 /shared/grafana-jwt
```

### 12. Grafana service name in cluster

**Problem**: mcp-grafana fails to connect to Grafana with DNS resolution error: `lookup grafana.monitoring.svc.cluster.local: no such host`.

**Root Cause**: The Grafana service is named `monitoring-grafana`, not `grafana`.

**Fix**: Use the correct service name:

```yaml
env:
  GRAFANA_URL: "http://monitoring-grafana.monitoring.svc.cluster.local"
```

Verify the service name:

```bash
kubectl --context=grigri get svc -n monitoring | grep grafana
```

## Token Exchange Flow

The token exchange converts a long-lived Kanidm API token into a short-lived JWT that Grafana can validate.

### 1. Obtain Kanidm API Token

The `KanidmServiceAccount` CRD creates a Kubernetes `Secret` containing the API token:

```bash
kubectl --context=grigri get secret -n <namespace> <service-account-name> -o jsonpath='{.data.token}' | base64 -d
```

### 2. Exchange for JWT (RFC 8693)

Use the Kanidm OAuth 2.0 token endpoint to exchange the API token for a JWT:

```bash
curl -X POST https://kanidm.example.com/oauth2/token \
  -H "Content-Type: application/x-www-form-urlencoded" \
  -d "grant_type=urn:ietf:params:oauth:grant-type:token-exchange" \
  -d "subject_token=<api-token>" \
  -d "subject_token_type=urn:ietf:params:oauth:token-type:access_token" \
  -d "audience=grafana" \
  -d "scope=openid email profile"
```

Response:

```json
{
  "access_token": "eyJ...",
  "id_token": "eyJ...",
  "issued_token_type": "urn:ietf:params:oauth:token-type:access_token",
  "token_type": "Bearer",
  "expires_in": 900
}
```

**Important**: Use the `id_token` for Grafana authentication, not the `access_token`. The `id_token` contains user claims (email, preferred_username) required by Grafana's JWT auth module. The `access_token` is an opaque token without these claims.

### 3. Grafana JWT Validation

Grafana's `auth.jwt` module validates the JWT signature using the Kanidm JWKS endpoint and extracts user claims:

```yaml
# system/monitoring/values.yaml
grafana.ini:
  auth.jwt:
    enabled: true
    header_name: Authorization
    email_claim: email
    username_claim: preferred_username
    cache_ttl: 60m
    auto_sign_up: true
    audience: grafana
    skip_org_role_sync: true
    jwk_set_url: https://idm.grigri.cloud/oauth2/openid/grafana/public_key.jwk
    role_attribute_path: "contains(scopes[*], 'editor') && 'Editor' || 'Viewer'"
```

The JWT must:
1. Be signed by Kanidm's private key (validated via JWKS endpoint)
2. Contain the `email` claim (from `id_token`)
3. Contain the `preferred_username` claim (from `id_token`)
4. Have the correct `audience` (must match the OAuth2 client name)

## Diagnosis Commands

### Check Kanidm service account status

```bash
# Verify KanidmServiceAccount CR exists
kubectl --context=grigri get kanidmserviceaccount -n <namespace>

# Check if Secret was created
kubectl --context=grigri get secret -n <namespace> | grep <service-account-name>

# Extract API token
kubectl --context=grigri get secret -n <namespace> <service-account-name> -o jsonpath='{.data.token}' | base64 -d
```

### Test token exchange

```bash
# From inside a pod with curl
API_TOKEN=$(cat /var/run/secrets/kanidm/token)
JWT_RESPONSE=$(curl -s -X POST https://kanidm.example.com/oauth2/token \
  -H "Content-Type: application/x-www-form-urlencoded" \
  -d "grant_type=urn:ietf:params:oauth:grant-type:token-exchange" \
  -d "subject_token=$API_TOKEN" \
  -d "subject_token_type=urn:ietf:params:oauth:token-type:access_token" \
  -d "audience=grafana" \
  -d "scope=openid email profile")

# Extract id_token (contains user claims) using grep/cut since jq may not be available
JWT=$(echo "$JWT_RESPONSE" | grep -o '"id_token":"[^"]*"' | cut -d'"' -f4)
echo "JWT: $JWT"

# Verify the token has required claims
PAYLOAD=$(echo "$JWT" | cut -d. -f2)
echo "$PAYLOAD" | base64 -d
```

### Test Grafana authentication

```bash
# From inside a pod with curl
curl -H "Authorization: Bearer $JWT" https://grafana.example.com/api/health
```

### Check mcp-grafana logs

```bash
kubectl --context=grigri logs -n <namespace> <pod-name> -c mcp-grafana --tail=100
```

## Prevention

When deploying or updating the Grafana MCP server:

1. **Verify Kaniop configuration**: Ensure `serviceAccountNamespaceSelector` includes the target namespace
2. **Check group membership**: Verify `idm_admin` is a member of the `entryManagedBy` group
3. **Use correct image tags**: Remember no `v` prefix for `mcp-grafana` tags
4. **Use full binary path**: `/app/mcp-grafana`, not just `mcp-grafana`
5. **Use -address flag**: Not `-port` for SSE transport
6. **Delete pods after StatefulSet updates**: Manual pod deletion required to pick up template changes
7. **Test token exchange**: Verify the full flow (API token → JWT → Grafana auth) after deployment
8. **Use correct JWKS URL**: Kanidm uses `public_key.jwk`, not `.well-known/jwks.json`
9. **Use id_token**: The `id_token` contains user claims; `access_token` is opaque
10. **Add email to service account**: Kanidm service accounts need `mail` attribute for Grafana's `email_claim`
11. **Set file permissions**: Init container must `chmod 666` the JWT file for the refresher sidecar
12. **Verify Grafana service name**: Use `monitoring-grafana`, not `grafana`

## Related

- [Kanidm Restore Procedure](kanidm-restore-procedure.md)
- [Hermes Agent Deployment](../deployment/hermes-agent.md)
- Kaniop documentation: https://github.com/kaniop/kaniop
- Grafana JWT auth: https://grafana.com/docs/grafana/latest/setup-grafana/configure-security/configure-authentication/jwt/
- RFC 8693 (Token Exchange): https://datatracker.ietf.org/doc/html/rfc8693

## Alternative: Envoy Sidecar for Token Management

For more complex authentication scenarios or when managing multiple MCP servers, an Envoy sidecar can handle credential injection transparently.

### Architecture

```
┌─────────────────────────────────────────┐
│  Pod                                    │
│  ┌─────────────┐  ┌─────────────────┐  │
│  │  MCP Server │──│  Envoy Sidecar  │  │
│  │  (localhost)│  │  (token inject) │  │
│  └─────────────┘  └────────┬────────┘  │
└────────────────────────────┼────────────┘
                             │
                             ↓
                    ┌─────────────────┐
                    │  OAuth2 IdP     │
                    │  (Kanidm/etc)   │
                    └─────────────────┘
```

### Benefits

1. **Transparent token injection**: Envoy's `credential_injector` filter adds tokens to outbound requests automatically
2. **Automatic refresh**: Envoy refreshes tokens at `expires_in/2` intervals
3. **Multi-target support**: Single sidecar can manage tokens for multiple services
4. **No application changes**: MCP server doesn't need to know about token management

### Configuration Example

```yaml
# Envoy sidecar with OAuth2 credential injection
- name: envoy-proxy
  image: envoyproxy/envoy:distroless-v1.38.3
  ports:
    - containerPort: 8002
  volumeMounts:
    - name: envoy-config
      mountPath: /etc/envoy
    - name: oauth-secret
      mountPath: /etc/oauth

# Envoy config snippet
http_filters:
  - name: envoy.filters.http.credential_injector
    typed_config:
      "@type": type.googleapis.com/envoy.extensions.filters.http.credential_injector.v3.CredentialInjector
      credentials:
        - name: grafana-token
          oauth2:
            token_endpoint: https://idm.example.com/oauth2/token
            client_id: <from-secret>
            client_secret: <from-secret>
            scopes: ["openid", "email", "profile"]
```

### Gotchas

1. **`credential_injector` is work-in-progress**: Pin Envoy version and watch for breaking changes
2. **Host header rewriting**: Envoy forwards `Host: 127.0.0.1` by default; use `host_rewrite_literal`
3. **Client secret encoding**: Envoy doesn't percent-encode `+` in client secrets (bug #47009); use hex-only secrets

### When to Use

- Multiple MCP servers or services needing OAuth2 tokens
- Complex token refresh logic beyond simple file-based rotation
- Centralized credential management for many sidecars
- When application code shouldn't handle authentication
