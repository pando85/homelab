# Argo CD Helm hooks: provisioning Jobs that never run, and hooks that silently stop running

Applies to any chart-managed app that provisions external state through a hook Job (here: the
shared-cache bucket for `platform/minio`). Four separate traps, all hit while bringing up the
`sccache` bucket in November 2026.

## 1. Defining an Argo CD hook disables every Helm hook in the app

Argo CD maps `helm.sh/hook` onto its own hook phases, but
[the docs](https://github.com/argoproj/argo-cd/blob/master/docs/user-guide/helm.md) warn:

> If you define any Argo CD hooks, *all* Helm hooks will be ignored.

The MinIO chart's post-job (policies, users, buckets) is a Helm hook. Adding
`argocd.argoproj.io/hook: PostSync` to one more Job would have silently stopped the chart's
users/policies from ever reconciling again - while the app still reported `Synced`/`Healthy`.

**Rule:** inside a chart that uses Helm hooks, use Helm hook annotations only
(`helm.sh/hook`, `helm.sh/hook-weight`, `helm.sh/hook-delete-policy`). Argo reads the weight
(`gitops-engine/pkg/sync/hook/helm/weight.go`, default 0), so `hook-weight: "1"` orders a Job after
the chart's unweighted post-job.

## 2. Hook-only changes never trigger a sync, so they never converge

Hook resources are excluded from sync comparison. Editing only a hook Job's spec leaves the
Application `Synced` at the new revision with **no operation at all** - the fixed hook is never
created and nothing re-runs. Symptom: commit pushed, `status.sync.revision` advanced, but
`status.operationState.finishedAt` still points at the old operation and no Job exists.

**Rule:** put the *desired state* a hook enforces in a normal (non-hook) resource. Here that is
`platform/minio/templates/sccache-config.yaml` (ConfigMap with endpoint, bucket, quota bytes,
expiry days, rule ID), read by the hook through `envFrom`. Editing it is a real diff → automated
sync → the hook re-applies and re-asserts. It also keeps the documented cap and the enforced cap
from drifting apart.

If only hook code changes, do not expect convergence: either pair it with a non-hook change or run
an authenticated sync.

## 3. Job deadline must be shorter than Argo's sync timeout

`argocd-cmd-params-cm` sets `controller.sync.timeout.seconds: 600` in this cluster. A hook Job with
a longer `activeDeadlineSeconds` gets reported as
`Operation terminated, triggered by controller sync timeout` instead of a clean Job failure with
readable logs. Keep the Job ceiling below it (420s here) and bound in-script waits so the budget is
explicit (`60 × 3s` API wait).

`hook-delete-policy: before-hook-creation` (without `hook-succeeded`) plus
`ttlSecondsAfterFinished` keeps a successful Job around long enough to read its assertion output.

## 4. Nonroot hook pods cannot read a 0400 Secret

A Secret volume mounted `defaultMode: 0400` lands root-owned; a container running as uid 1000 gets
`cat: /config/rootUser: Permission denied`. Fix is `fsGroup: 1000` + `defaultMode: 0440`
(group-readable, world-unreadable) - no privilege escalation needed.

The failure was hard to see because each attempt exited in under a second and the *Job* failure
reason surfaced as the deadline ("Job was active longer than specified deadline"): ten attempts plus
exponential backoff simply consumed the window. The real cause came from Loki, which still had the
archived pod logs after the Job and its pods were gone
(`namespace=minio`, `pod=~"minio-sccache-provisioning.*"`).

## The mc command surface differs from the docs

Verified against a server built from the pinned image (`pgsty/silo:RELEASE.2026-10-26T00-00-00Z`,
which ships `mc` symlinked to `mcli`) rather than trusting documentation examples:

| Intent | Does **not** work in this build | Works |
|---|---|---|
| Hard bucket quota | `mc admin bucket quota` → error "Deprecated command. Please use 'mcli quota' instead" | `mc quota set TARGET --size 53687091200b` |
| Idempotent lifecycle with a stable ID | `mc ilm rule add --id X --expiry-days 14` → "flag provided but not defined: -id" (the flag is `--expire-days`, and there is no `--id`) | `mc ilm rule import TARGET < rules.json` (replaces the whole configuration, so re-runs converge instead of accumulating rules) |
| Quota semantics | `50gb` parses as decimal 50,000,000,000 | `50GiB` / explicit bytes = 53,687,091,200; `mc quota info --json` reports `{"quota":53687091200,"type":"hard"}` and oversized writes fail with "Bucket quota exceeded" |

Also: this image has no `grep`, `sed`, `awk`, `jq` or `python` - only coreutils and bash (as
`/bin/sh`). Assertions in hook scripts must be POSIX-shell (`case` with literal needles built as
variables). Note that `*"$var")` means *ends with*, not *contains*: the trailing `*` is required,
and omitting it makes a valid state fail.

Avoid passing credentials as `mc alias set` argv; `MC_HOST_<alias>` (env) is supported and keeps
`/`, `@`, `%`, `#`, `?`, `+`, `=`, `&` literal in the userinfo (it does not percent-decode). mc
error strings report the alias/host, not the credential.

## Script-side self-verification

Do not end a provisioning script with `command || true` read-backs: a green sync then means only
that the script did not crash. Read the applied state back and exit non-zero when it differs
(quota bytes, quota type hard, exactly one rule, expected ID, expected days). Build the needles from
variables, and route any command output printed in a failure message through a guard that withholds
it if it contains either credential.

Fail fast on permanent errors: a credential rejection (`SignatureDoesNotMatch`) retried for the full
API-wait window looks exactly like a hang.
