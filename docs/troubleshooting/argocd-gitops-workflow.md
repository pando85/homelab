# ArgoCD GitOps Workflow

## Problem

Applying changes directly with `kubectl apply` during development or debugging. Changes appear to
work briefly, then get reverted by ArgoCD — or ArgoCD detects drift instantly and the manual change
never takes effect at all.

## Root Cause

ArgoCD is configured with `selfHeal: true` and real-time Kubernetes watches. When a resource on the
cluster diverges from the git state, ArgoCD detects it within seconds and reconciles back to git.
This is not a polling cycle — it uses Kubernetes informers for live cluster state and git webhooks
for repository changes.

## How to Diagnose

```bash
# Check if an application has selfHeal enabled
kubectl --context=grigri get application <app> -n argocd -o yaml | grep -A5 syncPolicy

# Check recent sync history (look for self-heal triggered syncs)
kubectl --context=grigri get application <app> -n argocd -o jsonpath='{.status.history}' \
  | python3 -c "import json,sys; h=json.load(sys.stdin); [print(f'{x[\"id\"]:5} {x[\"revision\"][:12]} {x[\"deployedAt\"]} auto={x[\"initiatedBy\"][\"automated\"]}') for x in h[-5:]]"

# Check if a resource is OutOfSync
kubectl --context=grigri get application <app> -n argocd -o jsonpath='{.status.sync.status}'
```

## Correct Workflow

**For all changes to ArgoCD-managed resources:**

1. Edit the file in git (locally)
2. Commit and push
3. Wait for ArgoCD to detect the commit and sync (watch via `kubectl get application <app> -n argocd`)
4. Verify the change took effect with read-only `kubectl get` / `kubectl logs`

**Never use `kubectl apply`, `kubectl edit`, or `kubectl patch`** on ArgoCD-managed resources. The
change will be reverted, and the revert may mask debugging by changing state under you.

**Exception:** During active incident response, the user may explicitly authorize `kubectl apply`
or `kubectl rollout restart`. Even then, be aware that ArgoCD will revert unless you also push the
matching change to git before the next self-heal cycle.

## Common Mistakes

- Using `kubectl apply -f` to "test quickly" — ArgoCD reverts it, and you may misinterpret results
  from the reverted state rather than your intended change
- Editing the local file AND applying with kubectl, then observing results — the observation may be
  from ArgoCD's reverted state, not your change
- Assuming ArgoCD polls on a slow cycle — it uses real-time watches and reacts within seconds

## Relevant Configuration

- ApplicationSet `system` generates Applications for all `system/*` directories
- All Applications have `prune: true` and `selfHeal: true`
- Sync option `ApplyOutOfSyncOnly=true` is enabled
- Retry: up to 10 attempts with exponential backoff (1m → 16m max)
