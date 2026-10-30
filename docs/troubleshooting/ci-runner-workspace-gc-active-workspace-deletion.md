# CI Runner: workspace-gc deleted active job workspace

## Problem

A Forgejo Actions Rust build job on the prusik runner (`runner13.2`) was killed mid-execution
when the `workspace-gc` sidecar deleted its `/workspace/ec4edb78aea87248` directory. The job
had been running for 61 minutes. At the time of deletion, `/workspace` was at 78% usage, which
put it in the `THRESHOLD_MED` (70-80%) tier with a 60-minute retention. The script's age check
used `stat -c %Y` (mtime), and the job's workspace directory was 61 minutes old — exceeding
the 60-minute retention by 1 minute, so it was deleted.

The runner pod was `Running`, healthy, polling for tasks, and logging no errors — the failure
was reported only to the Forgejo job, never to the container log.

## Root Cause

The `workspace-gc` script's online loop had no guard against deleting workspaces belonging to
active jobs. The `pgrep forgejo-runner` check only determined whether the runner process
existed (to decide whether to exit the loop), not whether it was currently executing jobs.

The script's age-based retention (`stat -c %Y`) cannot distinguish between a stale orphan
directory and an active job's workspace. Directory mtime updates when direct children are
created or removed, not on recursive writes — so a long-running build that doesn't create
new files in the workspace root will have an mtime close to directory creation time.

The script also swept `/workspace/*` indiscriminately, including 2-character hex prefixes
used by `actions/cache` bucket sharding, not just 16-character hex job workspace names.

## Fix

The script now operates in **monitor-only mode**: it reports disk usage and emits rate-limited
pressure alerts, but **never deletes any workspace directory**. All destructive cleanup is
disabled.

Orphan workspace reclamation is deferred to a future maintenance procedure that requires:
- The runner pod to be stopped (verified via pod status, not in-pod pgrep)
- Exclusive PVC access (no other pod mounting the volume)
- A committed, GitOps-managed maintenance workflow (no ad-hoc kubectl commands)

This maintenance procedure is not yet implemented. The normal runner removes completed workspaces
at the end of each job, so orphans are rare. Disk space is monitored via alerts; if usage exceeds
thresholds, operators will be notified. A committed, GitOps-managed orphan reclamation workflow
may be added in the future if needed.

## Rollout plan

### Pre-merge verification

Before merging, verify the fleet is idle to minimize disruption:

1. **Check Forgejo job queue.** Site Administration → Actions → Runners. Verify no in-progress
   tasks on either runner (prusik `ci-runner`, amd64 node `ci-runner-amd64`).
2. **Check pod status.** Verify both runner pods are Running with no recent restarts.
3. **Check docker events.** Verify no active container activity on either runner's dockerd.

If any jobs are active, wait for them to complete before merging.

### Merge and sync

1. **Merge the PR.** ArgoCD will sync the changes automatically (selfHeal + prune enabled).
2. **ConfigMap update.** The `workspace-gc-script` ConfigMap changes. The reloader annotation
   on the StatefulSet will trigger a rolling restart of the pods.
3. **Rolling restart.** The StatefulSet controller will terminate the old pod and create a
   new pod with the updated script. The `terminationGracePeriodSeconds` and daemon container
   lifecycle remain unchanged (grace 30s, preStop that kills dockerd immediately).

### Verification

After the new pod is Running:
- Check logs for the workspace-gc sidecar: it should log "workspace disk usage: X%" and
  alerts (if above threshold), but no "removing" messages.
- Verify no workspace directories are deleted over time.

### Rollback

If the monitor-only script causes issues (e.g., disk pressure alerts are too noisy), the forward
fix is to tune alert thresholds or intervals, not to revert to destructive deletion. The incident
demonstrated that age-based deletion is unsafe; restoring it without a replacement orphan
reclamation procedure would recreate the original problem.

## Future work

Graceful drain improvements (extended `terminationGracePeriodSeconds`, daemon container TERM
trap that waits for runner shutdown) are deferred to a future PR after end-to-end testing.
The runner's source code shows graceful shutdown architecture exists (`shutdown_timeout: 3h`
is already configured), but the pod lifecycle changes have not been verified in production.

## Related

- `platform/ci-runners/resources/workspace-gc-script.yaml` — the ConfigMap with the new script.
- `test/ci-runner-workspace-gc.sh` — local tests for the script.
