# Vault Operator Probe Timeouts

## Problem

The vault-operator pod restarts frequently (24+ restarts over 73 days) with exit code 1.
Logs show "leader election lost" when the K3s API server is briefly unreachable during
node reboots or upgrades.

## Root Cause

The vault-operator has a tight liveness probe configuration:
- `timeoutSeconds: 1` — same pattern as the documented ArgoCD repo-server probe death spiral
- `failureThreshold: 3` — only 30s grace before kill

When the API server is unreachable, the operator's health endpoint may also be slow,
causing the liveness probe to kill the container before it can recover. The operator
fatally exits on leader election loss instead of retrying gracefully.

## How to Diagnose

```bash
# Check restart count
kubectl --context=grigri get pod -n vault -l app.kubernetes.io/name=vault-operator

# Check previous container logs
kubectl --context=grigri logs -n vault vault-vault-operator-<hash> --previous | tail -20

# Look for "leader election lost" or "apiserver not ready"
kubectl --context=grigri logs -n vault vault-vault-operator-<hash> --previous | grep -E "leader election|apiserver"
```

## Fix

The vault-operator chart template only supports these probe fields:
- `initialDelaySeconds`
- `periodSeconds`
- `successThreshold`
- `timeoutSeconds`

**Note:** `failureThreshold` is NOT rendered by the template.

Increase `timeoutSeconds` in `platform/vault/values.yaml`:

```yaml
vault-operator:
  livenessProbe:
    initialDelaySeconds: 60
    periodSeconds: 10
    successThreshold: 1
    timeoutSeconds: 5  # was 1
  readinessProbe:
    periodSeconds: 10
    successThreshold: 1
    timeoutSeconds: 5  # was 1
```

## Status

Vault remains operational despite restarts — all ExternalSecrets sync correctly,
vault-0 is healthy. The restarts are cosmetic but indicate the operator cannot
survive brief API server disruptions.

## Related

- Similar pattern: `docs/troubleshooting/argocd-repo-server-probe-death-spiral.md`
- Upstream issue: vault-operator should use exponential backoff on API server errors
  instead of fatally exiting on leader election loss.
