# MCP Gateway Integration - READY FOR REVIEW

## Files Changed (apps/hermes/ only)

1. **values.yaml** - Multi-target config: `servers.homelab-grafana` + `servers.homelab-context7` with per-server scopes (`mcp_grafana`, `mcp_context7`). Gateway opt-in flag `enabled: false` (not flipped).

2. **templates/configmap-mcp-gateway.yaml** - Renders multi-server JSON config. Verified: `helm template --set mcpGateway.enabled=true` produces correct config with both servers.

3. **files/mcp_gateway_merge.py** - Refactored to iterate `servers` map, write all entries to `mcp_servers` in Hermes config.yaml. Syntax OK.

4. **files/mcp_gateway_token.py** - Refactored to exchange tokens per-server with per-server scope, write separate token files. Loop mode refreshes all servers. Syntax OK.

5. **templates/kanidm-service-account-mcp-gateway.yaml** - New SA `hermes-mcp` in `mcp-gateway-agents` group (unchanged from prior work).

## Local Tests Passed

✓ Python syntax: both scripts parse cleanly
✓ Helm template: renders valid JSON with 2 servers when enabled
✓ OpenCode MCP tools: `context7.resolve-library-id` + `grafana-grigri.list_datasources` both return data
✓ Existing Grafana sidecar: `grafana-mcp` container running, token refresher active

## Blockers Identified

### 1. Grafana JWT Issuer Mismatch (HARD BLOCKER)

**Current Grafana config** (`system/monitoring/values.yaml`):
- `[auth.jwt] audience = grafana` (NOT `mcp-gateway`)
- `jwk_set_url` points to `grafana` client JWKS (NOT `mcp-gateway`)
- `username_claim = preferred_username` (service accounts have no `preferred_username`)
- `oauth_allow_insecure_email_lookup = true` (known bad control, owner flagged)

**Gateway would forward**: token with `aud: mcp-gateway`, no `preferred_username` claim.

**Result**: Gateway Grafana route CANNOT authenticate. Grafana rejects the token.

**Resolution options** (require coordinated live change):
- (a) Change Grafana to trust `mcp-gateway` JWKS + use `sub` claim (breaks existing OAuth users)
- (b) Dual JWKS trust (owner's probe retracted as unsafe)
- (c) Keep existing sidecar until separate migration project

**Recommendation**: Keep existing `grafana-mcp` sidecar. Gateway Grafana route stays disabled.

### 2. OpenCode Human OAuth Scope (BLOCKER)

**Current state**: `apps/mcp-gateway/values.yaml` has `kanidm.oauth2Client.humanScopeMap: []` (empty).

**Impact**: OpenCode OAuth sign-in will fail at Kanidm scope validation. No human can use the gateway.

**Resolution**: Owner must:
1. Create dedicated group (e.g., `mcp-users`) or reuse existing (e.g., `grafana-users`)
2. Add to `humanScopeMap` with scopes: `openid email profile mcp_grafana mcp_context7`
3. Add owner to group manually (person membership never CRD-managed)

**OpenCode patch plan** (apply after humanScopeMap populated):
```jsonc
// ~/.config/opencode/opencode.json
{
  "mcp": {
    "servers": {
      "homelab-grafana": {
        "type": "remote",
        "url": "https://mcp.grigri.cloud/grafana/mcp",
        "oauth": {
          "client_id": "mcp-gateway",
          "scope": "openid email profile mcp_grafana",
          "callback_port": 15000
        }
      },
      "homelab-context7": {
        "type": "remote",
        "url": "https://mcp.grigri.cloud/context7/mcp",
        "oauth": {
          "client_id": "mcp-gateway",
          "scope": "openid email profile mcp_context7",
          "callback_port": 15000
        }
      }
    }
  }
}
```

Then run: `opencode mcp auth homelab-grafana` + `opencode mcp auth homelab-context7` (manual browser OAuth).

## MVP Acceptance Criteria

### Hermes (candidate, not enabled)

**Config test**:
```bash
cd /home/agil/homelab
helm template --namespace hermes hermes apps/hermes/ --set mcpGateway.enabled=true | \
  grep -A 5 '"servers"' | jq .
```
Expected: JSON with `homelab-grafana` + `homelab-context7` entries.

**Actual tool calls** (after enabling + backend routes enabled):
```bash
kubectl --context=grigri exec -n hermes hermes-0 -c hermes -- \
  hermes mcp call homelab-context7 resolve-library-id '{"libraryName":"React"}'
kubectl --context=grigri exec -n hermes hermes-0 -c hermes -- \
  hermes mcp call homelab-grafana list_datasources '{}'
```
Expected: Both return data (context7: library list, grafana: datasource list).

### OpenCode (working now)

**Actual tool calls** (verified):
```bash
# Context7
opencode execute 'tools.context7["resolve-library-id"]({"libraryName":"Kubernetes","query":"pod spec"})'

# Grafana
opencode execute 'tools["grafana-grigri"].list_datasources({"limit":5})'
```
Both return data ✓

## What Was NOT Done

- Did NOT flip `mcpGateway.enabled` to `true` (owner-gated)
- Did NOT enable gateway backends (`apps/mcp-gateway/values.yaml` backends.*.enabled stays `false`)
- Did NOT modify `system/monitoring/values.yaml` Grafana JWT config
- Did NOT create new Kanidm groups or person memberships
- Did NOT commit/push (awaiting main review)

## Identity Preservation

- Existing `grafana-mcp` SA: `entryManagedBy: grafana-mcp-users`, has `mail: grafana-mcp@grigri.cloud`, token secret `grafana-mcp-token`
- New `hermes-mcp` SA: `entryManagedBy: mcp-gateway-agents`, no mail, token secret `hermes-mcp-token`
- Both SAs coexist. Gateway route uses `hermes-mcp` (when enabled). Sidecar uses `grafana-mcp` (current).

## Next Steps (Owner Actions)

1. Review this report
2. Decide: keep Grafana sidecar OR migrate to gateway (requires Grafana JWT config change)
3. If gateway: enable `backends.grafana.enabled` + `backends.context7.enabled` in `apps/mcp-gateway/values.yaml`
4. Populate `humanScopeMap` with approved human group
5. Flip `mcpGateway.enabled: true` in `apps/hermes/values.yaml`
6. Apply OpenCode patch + manual OAuth sign-in
7. Commit + push + ArgoCD sync
