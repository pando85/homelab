# k3s system-upgrade Plans stuck: build-date check and node taints

## Problem

After Renovate bumped k3s from `v1.35.8+k3s1` to `v1.36.2+k3s1` (a new minor), both
system-upgrade-controller Plans (`system/system-upgrade/k3s/server.yaml` and `agent.yaml`) stayed
`Complete=False`. Jobs failed with `DeadlineExceeded: Job was active longer than specified deadline`.
The control-plane node (`prusik`) stayed `SchedulingDisabled` meanwhile, because the server Plan has
`cordon: true`.

## Root Cause

Two independent causes, both visible only in pod logs and events, not in the Job error.

### 1. `k3s-upgrade` refuses binaries with an older build month

The server upgrade pods errored with:

```
Error: Current build date 2026-08-27T14:52:34Z is more recent than 2026-06-24T22:52:31Z
```

`scripts/upgrade.sh` in `rancher/k3s-upgrade` (`compare_build_dates`) refuses any target binary whose
build year-month is older than the installed binary's, to prevent downgrades. There is no skip or
force flag.

k3s ships a patch for every supported minor line in the same monthly release batch, so a newer
minor can be an older build. The installed patch is always recent. The first patches of the next
minor (`.0`, `.1`, ...) were built when that minor was released, months earlier, so any upgrade
proposed from them fails. Renovate previously gated k3s minors by `minimumReleaseAge: 120 days`,
which guaranteed the proposed version was at least 120 days old, so every minor upgrade failed.

### 2. The agent Plan could not schedule on a tainted node

`k8s-amd64-1` carries the taint `dedicated=ci:NoSchedule`. The agent Plan had no toleration, so its
pod stayed `Pending` (`FailedScheduling`: `1 node(s) had untolerated taint(s)`) until the Job deadline.
Any new node taint needs a matching toleration in the agent Plan.

## How to Diagnose

```bash
kubectl --context=grigri -n system-upgrade get plans,jobs,pods -o wide
kubectl --context=grigri -n system-upgrade logs <apply-k3s-...-pod> | tail -n 30
kubectl --context=grigri -n system-upgrade get events --sort-by=.lastTimestamp | grep -i <plan>
kubectl --context=grigri get node <node> -o jsonpath='{.spec.taints}'
```

- `Current build date ... is more recent than ...` in the server pod log: cause 1.
- `FailedScheduling ... untolerated taint(s)` in events for an agent pod: cause 2.

## Fix / Workaround

- Cause 1: target the **newest patch** of the next minor, which comes from the same release batch as
  the installed binary and passes the check. Update `system/system-upgrade/k3s/kustomization.yaml`
  and `k3s_version` in `metal/roles/k3s/defaults/main.yml` to that version.
- Cause 2: add the toleration to `system/system-upgrade/k3s/agent.yaml`.
- Do not uncordon by hand: the server Plan uncordons the node on success.
- Expected afterwards: each node shows one upgrade pod in `Unknown` (killed by the k3s restart) plus a
  retried `Completed` pod. Harmless. All three nodes finished within about 5 minutes of the fix.

## Prevention

- `.github/renovate.json5`: the k3s group rule uses
  `allowedVersions: '!/^v\\d+\\.\\d+\\.[0-3]\\+k3s\\d+$/'`, so a new minor is proposed only from `.4`
  (about 4 months after `.0`), and Renovate picks its newest patch. Not yet observed in a real
  Renovate run: if a k3s `vX.Y.0-3` is ever proposed, the filter is not applied.
- `allowedVersions` cannot be combined with `matchUpdateTypes` in the same rule, so it applies to all
  update types of the matched package, not only minors.
- Add a matching toleration to the agent Plan whenever a node gets a new taint.
