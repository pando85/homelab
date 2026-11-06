# Stopping Home Assistant under ArgoCD

## Problem

You need Home Assistant really stopped, for example for an HA-outage test such as the battery
[failsafe test](../deployment/battery-optimizer.md#failsafe-test), and a plain
`kubectl scale ... --replicas=0` is reverted within seconds.

## Root Cause

The `home-assistant` Application comes from the `apps` ApplicationSet with `selfHeal: true`, so Argo
CD restores the replica count from git. Patching the Application's `syncPolicy` does not help either:
the ApplicationSet undoes the patch.

## Procedure

Run these `kubectl` writes only with the owner's explicit go.

1. Scale the Argo CD repo-server (deployment `argocd-repo-server`, namespace `argocd`) to 0.
2. Scale the StatefulSet `home-assistant/home-assistant-hass` to 0.

```bash
kubectl --context=grigri -n argocd scale deploy/argocd-repo-server --replicas=0
kubectl --context=grigri -n home-assistant scale sts/home-assistant-hass --replicas=0
```

To restore, bring Home Assistant back first, then the repo-server:

```bash
kubectl --context=grigri -n home-assistant scale sts/home-assistant-hass --replicas=1
kubectl --context=grigri -n argocd scale deploy/argocd-repo-server --replicas=1
```

The `home-assistant` app shows sync `Unknown` until Argo CD's next refresh (about 3 minutes); a hard
refresh clears it.

## When to use it

Use it for real HA-outage tests instead of proxies such as disabling an integration. A similar tip for
restoring a volume from a Velero backup is in
[`restore-backup.md`](../user-guide/restore-backup.md#velero).
