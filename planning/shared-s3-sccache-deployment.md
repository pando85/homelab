# Shared S3 sccache deployment plan

## Status and approval boundaries

**Implemented for CI since 2026-11-06 ~19:10Z** (private bucket, quota, lifecycle, scoped
identities, Calypso action with `cache-mode: disk-s3` default; `disk` is the rollback). Results and
the 2026-11-10 bucket-full incident are in the
[caching analysis](../docs/conventions/rust-build-caching.md#deployed-state-and-results). The
phases below are the original plan; workstation access (phase 6) is not done. Approval to record,
commit and push this plan is not approval to provision secrets, modify application/workstation
configuration or deploy it.

The [Rust build caching analysis](../docs/conventions/rust-build-caching.md) contains the measured
evidence and rationale. This plan describes implementation order, validation gates and rollback.

Approval stages:

1. **Implementation:** prepare scoped changes and local validation in the owning repositories.
   Stop before pushes or other actions that could trigger a live rollout. Request approval before
   any benchmark that writes to shared storage.
2. **Deployment:** review the actual commits and approve secret provisioning, configuration pushes,
   MinIO reconciliation and an idle-window CI pilot explicitly. Commit configuration before it is
   applied; use ArgoCD, not direct resource mutation or Helm installation.
3. **Expansion:** review pilot evidence before enabling additional workflows, runners or workstation
   publishing. Do not treat a successful bucket deployment as approval for all consumers.

Read-only prerequisite checks need no live-change approval. Recheck each repository's instructions,
status and remotes before implementation. Local privilege escalation is never part of agent work.

## Target architecture

Keep the local cache on each machine and add S3 as a best-effort secondary cache:

```text
Workstation local sccache --------+
                                 |
prusik runner local sccache ------+--> Internal MinIO / private sccache bucket
                                 |
k8s-amd64-1 local sccache --------+

Read: local disk -> S3 -> compile
Remote hit: restore artifacts and backfill local cache
New output: local write, then best-effort secondary publication
```

Reuse `platform/minio/` initially, subject to capacity and performance checks. No new MinIO,
Redis or PVC is proposed initially. Its existing HTTPS endpoint is
`https://s3.internal.grigri.cloud`; confirm private reachability from every consumer. Do not expose
the backup MinIO publicly to make workstation access work.

Keep current local cache limits during evaluation. Per-worktree Cargo target isolation is a
separate change, not bundled into this S3 rollout. Preserve normal local incremental compilation.
Do not assume workstation package installation is complete until its installed version is checked.
Version alignment remains a verification step even with Renovate-managed updates.

## Phase 1: prerequisites and baseline

### Read-only checks

- Confirm workstation and runner sccache versions, S3 support and multi-level support. Verify the
  deployed action's effective version, not just its default in a checkout.
- Identify internal MinIO's actual PVC/pool, available capacity, health and existing consumers.
  A PVC request or placement on prusik is not proof of fast backing storage or available space.
- Record backup schedules and current MinIO request latency, errors, resource use and storage load.
  Internal MinIO also serves Velero and Kaniop; protecting backups is a deployment gate.
- Check DNS, routing, TLS verification and endpoint reachability from the workstation and both
  runners, including the actual job execution modes. If private connectivity is missing, propose
  that separately and stop before changing network exposure.
- Inspect the installed chart version and rendered provisioning hooks. Confirm how bucket policy,
  lifecycle and quota can be expressed declaratively and whether changes restart MinIO or rerun
  provisioning for existing buckets. Do not assume hooks only touch the new bucket.
- Confirm which CI jobs share a daemon and which jobs must not share credentials/cached outputs.
  Resolve the trust-boundary design in phase 3 before enabling S3 in a shared daemon.

### Baseline to retain

Record representative build wall times, effective target directories, toolchain/flags, local cache
sizes and hit/miss statistics. Shared-daemon counters can overlap; use isolated measurement or
exclusive-window deltas rather than summing job snapshots. Do not reset unrelated job counters.

**Exit gate:** viable private endpoint, adequate headroom, known provisioning blast radius and a
reviewed credential/daemon isolation design. Stop or revise the proposal if any is missing.

## Phase 2: private bucket and retention

Expected homelab changes:

- `platform/minio/values.yaml`: private bucket and bucket-scoped policy/provisioning entries.
- ExternalSecret references under `platform/minio/templates/`, following existing Vault patterns.
- A declarative provisioning Job only if the chart cannot express lifecycle/quota requirements.
  Inspect existing hooks and use an idempotent, pinned implementation; do not introduce ad-hoc
  remote `mc` mutations outside committed configuration.

Proposed initial settings, subject to verified capacity and backend support:

| Setting | Initial proposal |
|---|---|
| Bucket | `sccache` |
| Anonymous access | Disabled |
| Versioning / object locking | Disabled |
| Purge existing contents during provisioning | Disabled |
| Storage ceiling | 100 GiB = 107,374,182,400 bytes, hard quota (raised from 50 GiB on 2026-11-10; verified: oversized writes rejected) |
| Object expiration | 14 days; one lifecycle rule `sccache-expiry`; tune from observed reuse |
| Backups | Do not add cache contents to backup jobs |
| Authorization | Dedicated bucket-scoped policies; no administrative or backup-bucket access |

Lifecycle expiration is object-age-based, not last-access LRU. `SCCACHE_CACHE_SIZE` bounds only
local disk, not the bucket. Define capacity/error alerts and preserve operational headroom for
other MinIO consumers.

Quota behavior is confirmed on the installed server version (Silo/mc `RELEASE.2026-10-26T00-00-00Z`):
`mc quota set --size <bytes>b` reports `{"quota":<bytes>,"type":"hard"}` and a write past the
cap fails with "Bucket quota exceeded", so compile jobs degrade to their local cache instead of
filling the pool. Note `100gb` would be decimal 100,000,000,000 - the cap is expressed in bytes.
The desired values live in `platform/minio/templates/sccache-config.yaml` and are applied and
re-asserted by the `minio-sccache-provisioning` post-install/post-upgrade hook Job
(`platform/minio/templates/sccache-provisioning.yaml`); see
`docs/troubleshooting/argocd-helm-hook-provisioning.md` for why the state is in a ConfigMap and not
only in the hook.

Version constraint worth knowing before bumping the backend: the provisioning hook reuses the
**server image's own `mc`** (`pgsty/silo:RELEASE.2026-10-26T00-00-00Z`), because the standalone
`quay.io/minio/mc` images were removed and now return 401. That means a Renovate bump of the MinIO
server release also changes the CLI surface the hook depends on. The script does not assume that
quietly: it fails the Job with the applied state in the log if the cap, the quota type or the
lifecycle rule is not exactly as configured, so a version bump surfaces as a failed sync rather than
silently mis-provisioned bucket settings. The command surface verified for the current release is
recorded in `docs/troubleshooting/argocd-helm-hook-provisioning.md`.

Use only the S3 operations the chosen backend needs, including its capability-check object and
multipart operations if required. Verify denial of access to unrelated buckets. Remote cache
write rejection at capacity must not fail normal compilation with the selected hierarchy/policy;
prove this in the pilot.

Before syncing, review rendered manifests and hook changes and schedule any MinIO restart around
backups. Adding disposable cache traffic must not degrade backup reliability.

## Phase 3: credentials and trust boundaries

Do not put AWS credentials in runner-wide environment variables or plaintext ConfigMaps.

Secret-management routes:

- MinIO provisioning: Vault to ExternalSecret using established homelab conventions.
- CI: repository/workflow-scoped Forgejo secrets for approved consumers.
- Workstation: `desktop-installer`-managed configuration using its `pass`/template conventions and
  restrictive installed permissions. Do not hand-edit generated configuration as the durable source.

Keep actual credentials and details mapping personal accounts/tokens to privileges out of commits,
logs and public documentation. Use distinct consumer credentials where appropriate, scoped to the
cache only. The workstation initially consumes remote data read-only; publishing requires a later
review. CI publishing is limited to trusted build contexts, not merely any job running on a runner.

### Shared daemon is a shared capability

Host-mode jobs currently share an sccache daemon. A daemon configured with S3 access can expose
that capability to later clients even if their jobs were not given S3 secrets. Skipping secret
injection is therefore not sufficient isolation.

Before selecting a credential flow, either:

1. establish that all users of a daemon belong to the same reviewed trust domain; or
2. isolate daemon endpoints/ports and configuration between trust domains, and assess whether
   stronger job/runner isolation is needed when untrusted processes share the host and caches.

Separate ports alone are not a security boundary against a malicious process with access to both.
Audit local-cache sharing too: an untrusted local cache writer can undermine trusted consumers.
Do not promise security through workflow conditionals alone.

Choose a credential-loading mechanism supported by the installed binary that does not leak cache
credentials into unrelated AWS tooling. Do not assume environment variables are per-invocation
when an already-running daemon may retain its startup configuration. Validate read-only operation,
negative authorization checks and secret masking without printing values.

**Exit gate:** reviewed trust model and effective least-privilege credential flow for each pilot
consumer. If existing runner isolation cannot enforce the model, stop and propose that work first.

## Phase 4: CI pilot implementation

Expected application changes in the Calypso repository:

- Extend `.forgejo/actions/sccache-setup/action.yml` with an explicit shared-cache opt-in, disabled
  by default initially, and any daemon isolation required by phase 3.
- Configure the hierarchy explicitly:

  ```bash
  SCCACHE_MULTILEVEL_CHAIN=disk,s3
  SCCACHE_MULTILEVEL_WRITE_ERROR_POLICY=l0
  ```

- Set bucket, HTTPS endpoint, region and path-style behavior after checking the installed backend's
  requirements. Setting disk/S3 variables alone does not establish a multi-level chain.
- Preserve `/cache/sccache` and its existing `10G` local limit for comparison.
- Retain the existing version pin and Renovate management. Capture per-level statistics where
  supported, without credentials or credential-bearing URLs in logs.
- Keep local-only configuration available as an explicit rollback path.

Trial one controlled workflow/runner window first. An isolated daemon can avoid changing an active
shared daemon, provided its storage/trust assumptions are reviewed. Any required daemon restart or
runner rollout must wait for verified idle state: the existing runner shutdown behavior can kill
active jobs. Do not assume a 3h runner shutdown setting makes the whole pod drain gracefully.

The inspected sccache 0.19.0 implementation writes local first with `l0`, then spawns secondary
uploads, but still performs an awaited secondary capability check in write setup. Uploads consume
resources and are not a durable delivery queue. Remote lookups, checks, retries and timeouts can
add latency; test failures rather than promising instantaneous fallback. Avoid `all` write policy
for this disposable secondary tier because it waits for remote completion.

Do not bulk-upload existing caches or assume they publish automatically. Let successful new
compilations populate S3 naturally; local hits alone need not populate the secondary backend.

## Phase 5: validation and acceptance

Approve write-producing benchmarks before running them. Use dedicated disposable prefixes/buckets
and isolated caches/daemons; never wipe normal runner/developer caches. Delete only task-created
benchmark objects, scoped to the approved namespace and cleanup policy, never unrelated contents.

Keep source, toolchain, flags and starting target/local-cache state equivalent when comparing
local-only with local-plus-S3. Isolate the S3 experiment from worktree-target policy changes.

| Test | Required evidence |
|---|---|
| Warm local repeat build | Local-hit performance remains intact |
| Publish, then build on another machine | Confirm remote hits actually avoid compilation |
| Changed workspace code | Representative development performance, incremental preserved |
| Concurrent mixed GET/PUT traffic | Read tails and backup impact under slow writes |
| Endpoint unavailable/slow or credentials invalid | Bounded delays, error reporting, build behavior |
| Quota rejection | Failed publication does not break local compilation |
| Daemon exits with uploads outstanding | Incomplete publication remains harmless |
| Authorization negatives | Private bucket and no access outside approved scope |

Record build wall time, local/remote hit and miss counts, upload/check errors, GET/missing-object/PUT
p50/p95/p99, bytes transferred, memory, disk growth and other-consumer impact. Large-file sequential
throughput alone is not representative of artifact-sized mixed requests.

Local Cargo builds/tests/lints must follow the workstation's build-coordination rules, including
`flock -n -E 75 /tmp/opencode/cargo-build.lock <command>`, one owner and one command at a time.
Exit 75 means busy, not permission to queue retries or bypass the lock. Exercise simultaneous
storage load through approved non-Cargo traffic or separately scheduled CI jobs; do not bypass
local build admission to create a three-agent benchmark.

Proposed acceptance gates, to finalize before the trial:

- Warm-build median regression no greater than **5%** across repeated equivalent runs, with a
  reviewed repetition count/noise baseline and tail-latency results.
- Reproducible cross-machine wall-time benefit, not merely more reported cache hits.
- No backup failures or unacceptable MinIO/resource regression, including at least one backup
  cycle. Agree numerical request-latency/resource budgets from the baseline before testing.
- Remote outage causes neither build failure nor stalls beyond an agreed timeout budget. Select
  and verify that budget before fault testing; best-effort writes alone do not guarantee it.
- Capacity, quota rejection and per-level errors are observable without leaking credentials.

**Exit gate:** record results and request expansion approval. If gates fail, retain local-only
caching. A dedicated fast-storage backend is a new proposal, not an automatic escalation.

## Phase 6: staged adoption

After successful pilot review and explicit approval:

1. Enable approved CI workflows on both runners, using the reviewed daemon/trust layout.
2. Observe representative workloads and at least one backup cycle before adding clients.
3. Add workstation read-only S3 access through `desktop-installer`, verifying its installed
   sccache version and supported configuration. Keep local compilation functional offline.
4. Evaluate workstation publishing separately after confirming useful key compatibility and trust.
5. Tune bucket retention/capacity and local limits from observed working sets, not assumptions.

Do not reduce local limits initially. Local-first sharing does not free the existing roughly
10 GiB cache on k8s-amd64-1, and it does not solve Docker-image disk growth. Those remain separate
capacity decisions. No worktree-target migration is implied by enabling S3.

### Cross-host hits and path normalization

The storage side is complete and verified: private `sccache` bucket, hard quota 100 GiB
(107,374,182,400 bytes, writes past the cap rejected), one 14-day lifecycle rule
(`sccache-expiry`), separate scoped identities (`sccache-ci` read/write, `sccache-desktop`
read-only) and a credential file delivered to the runners.

The quota was raised from 50 GiB because the original bucket filled in about 3.5 days (2026-11-06 to
2026-11-10 ~04:30Z, ~12-14 GiB/day) and S3 writes were silently rejected (write policy `l0`; see
`docs/troubleshooting/sccache-s3-bucket-full-silent.md`). A shorter lifecycle expiry is being sized
next; this plan does not fix its value.

sccache keys include source file paths, so builds under different workspace prefixes do not collide.
Path normalization (`normalize-paths: true`, sccache 0.19 `BASEDIRS` = workspace root) **is now
implemented** in the Calypso action and enabled only for Validation (`rust.yml`); release workflows
still use path-specific keys and a change enabling it is being prepared in Calypso. Workstation
checkouts are not normalized. Cross-host hits are plausible for Validation (identical basedir shape
on both runners) but not proven; a workstation-to-CI benefit is still to be earned. Keep Phase 6
step 4 (workstation publishing) gated on demonstrated key compatibility, and do not respond to low
cross-host hits by changing retention or the cap.

## Rollback

- Disable the shared-cache opt-in and restore explicit local-only configuration through the owning
  repositories, committing before application and obtaining required push/deployment approval.
- Restart affected daemons or roll runners only in a verified idle window. Reverting environment
  configuration alone does not reconfigure a daemon already running.
- Preserve local cache directories; do not purge the bucket or delete any PVC as rollback.
- Leave the private bucket intact while investigating. Retire it only through separately approved,
  scoped lifecycle work after no consumers depend on it.
- Revoke/rotate cache credentials separately if security requires it; do not disturb backup
  credentials or shared service configuration unnecessarily.

## Deliverables and next approval

| Owner repository | Expected deliverables after implementation approval |
|---|---|
| Homelab | Private bucket/policy/retention/quota configuration, ExternalSecret references, rendering/lint evidence and deployment runbook updates |
| Calypso | Opt-in multi-level setup, reviewed daemon/secret isolation, controlled pilot workflow and per-level evidence |
| Desktop installer | Later, read-only S3 configuration through managed templates and `pass`; publishing remains a separate decision |

Link final runbooks and pilot evidence from the
[caching analysis](../docs/conventions/rust-build-caching.md). Recheck existing MinIO provisioning
patterns in `platform/minio/` and the
[MinIO architecture](../docs/deployment/minio-architecture.md) before editing.

**Next requested approval:** implementation-only preparation of the infrastructure and CI pilot
changes, stopping before configuration pushes, secret provisioning, live benchmarks or deployment.
This plan's documentation commit/push does not grant that approval.
