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
  -d "audience=grafana"
```

Response:

```json
{
  "access_token": "eyJ...",
  "issued_token_type": "urn:ietf:params:oauth:token-type:access_token",
  "token_type": "Bearer",
  "expires_in": 3600
}
```

### 3. Grafana JWT Validation

Grafana's `auth.jwt` module validates the JWT signature using the Kanidm JWKS endpoint and extracts user claims:

```yaml
# system/monitoring/values.yaml
grafana.ini:
  auth.jwt:
    enabled: true
    header_name: Authorization
    header_format: "Bearer %(token)s"
    key_file: /etc/grafana/jwt-key.pem  # Kanidm public key for signature validation
    username_claim: sub  # or "email", "preferred_username"
    auto_sign_up: true
```

The JWT must be signed by Kanidm's private key, which Grafana validates using the public key from `key_file`.

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
  -d "audience=grafana")

# Extract JWT (using grep/cut since jq may not be available)
JWT=$(echo "$JWT_RESPONSE" | grep -o '"access_token":"[^"]*"' | cut -d'"' -f4)
echo "JWT: $JWT"
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

## Related

- [Kanidm Restore Procedure](kanidm-restore-procedure.md)
- [Hermes Agent Deployment](../deployment/hermes-agent.md)
- Kaniop documentation: https://github.com/kaniop/kaniop
- Grafana JWT auth: https://grafana.com/docs/grafana/latest/setup-grafana/configure-security/configure-authentication/jwt/
- RFC 8693 (Token Exchange): https://datatracker.ietf.org/doc/html/rfc8693
