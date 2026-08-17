---
name: merge-prs
description: Use when merging Renovate or other PRs in this GitOps repo. Covers the verify-merge-verify workflow for Helm charts, Ansible roles, and K3s upgrades.
---

## Scope

Merge PRs in this GitOps repository following a safe verify-merge-verify workflow.
Handles three types of changes:
- **Helm/Kustomize apps** (ArgoCD-managed)
- **Ansible roles** (host-level components like zfs_exporter)
- **K3s upgrades** (system-upgrade-controller)

## Safety Rules

- Process PRs **one at a time** - verify each before moving to the next.
- Always check current state before merging.
- Always verify after merging (ArgoCD sync, pod health, endpoint response).
- If something goes wrong, stop and investigate before continuing.

## Merge Workflow

### 1. Pre-merge verification

```bash
# Check current state
gh pr diff <number>
kubectl --context=grigri get applications -n argocd <app-name> -o jsonpath='{.status.sync.status} {.status.health.status}{"\n"}'
kubectl --context=grigri get pods -n <namespace> -o wide
```

### 2. Merge

```bash
# This repo uses squash merges (merge commits are not allowed)
gh pr merge <number> --squash --delete-branch
git pull origin master
```

### 3. Post-merge verification

**For ArgoCD-managed apps:**
```bash
# Wait for sync (15-30s typically)
sleep 15
kubectl --context=grigri get applications -n argocd <app-name> -o jsonpath='{.status.sync.status} {.status.health.status}{"\n"}'
kubectl --context=grigri get pods -n <namespace> -o wide

# Verify endpoint if applicable
curl -sIL https://<domain> | head -10
```

**For Ansible-managed components:**
```bash
# Run the specific Ansible tag
cd metal && ANSIBLE_EXTRA_ARGS="-t <tag>" make cluster

# Verify on nodes
ssh <node> "<command-to-check-version>"
```

**For K3s upgrades:**
```bash
# Monitor node upgrades (happens one node at a time)
kubectl --context=grigri get nodes -o wide
kubectl --context=grigri get plans -n system-upgrade -o wide
```

## Type-Specific Notes

### Helm/Kustomize Apps (ArgoCD)

- ArgoCD auto-syncs within ~15 seconds of merge.
- Wait for `Synced Healthy` status.
- Check pod rollout completes before verifying endpoint.
- Major version bumps may require checking for breaking changes.

### Ansible Roles

- Changes to `metal/roles/*/defaults/main.yml` require running Ansible.
- Use the tag specified in the role's comments (e.g., `-t zfs-exporter`).
- Verify version on each applicable node after apply.
- Some roles only run on specific nodes (e.g., zfs_exporter only on nodes with ZFS).

### K3s Upgrades

- Uses `system-upgrade-controller` with Plans.
- Control plane (prusik) upgrades first, then agents.
- Nodes are cordoned during upgrade (`SchedulingDisabled`).
- Wait for all nodes to reach target version.
- After upgrade, ArgoCD repo-server may cache old k8s version - trigger hard refresh if apps show stale sync status:
  ```bash
  kubectl --context=grigri annotate applications -n argocd <app> argocd.argoproj.io/refresh=hard --overwrite
  ```

## Known Issues

### Monitoring App (pre-existing)

The `monitoring` application has a Helm template rendering error in Grafana's `deployment.yaml`:
```
wrong type for value; expected string; got []interface {}
```
This is a pre-existing issue unrelated to K3s upgrades. The monitoring stack itself runs fine.
ArgoCD shows `Unknown` sync status but `Healthy` health status.

### Stale Upgrade Pods

After K3s upgrades, some `apply-k3s-*` pods may show `Unknown` status. These are stale and can be ignored - they'll be cleaned up automatically.

## Checklist

Before declaring a PR complete:
- [ ] ArgoCD shows `Synced Healthy` (or `Unknown Healthy` for known issues)
- [ ] All pods in namespace are `Running` and `Ready`
- [ ] No error events in `kubectl get events -n <namespace>`
- [ ] Endpoint responds (if applicable)
- [ ] For Ansible: version verified on target nodes
- [ ] For K3s: all nodes at target version, plans show `Complete: True`
