# Plan: add a second Forgejo CI runner on k8s-amd64-1

**Status:** IMPLEMENTED 2026-10-01 — deployed and verified, see §11
**Scope:** add capacity, **not** migrate `ci-runner-0`. **No kata** (see §2).

## Decisions

| # | decision | resolution |
|---|---|---|
| D1 | Labels / capacity | **Same labels** as the existing runner (`ubuntu-latest:host`, `ubuntu-24.04:host`, `docker`), **`capacity: 5`**, OOM kills accepted knowingly (§5.1) |
| D2 | Manifest location | **Same directory** `platform/ci-runners/`, shared `ci-runners` namespace |
| D3 | Credential | **Whole `.runner` file in Vault** at `git/runner-amd64`, emitted verbatim — no id/uuid in git |
| D4 | Runtime | **runc**, matching the existing runner. Kata is out of scope (§2) |
| — | Workspace volume | **hostPath**, not tmpfs: `/srv/ci-runner/workspace` |
| — | Volume layout | Single parent `/srv/ci-runner/` with `docker/`, `cache/`, `workspace/` subdirs, all Ansible-created |

### Why D3 cannot be "reuse the existing credential"

The `.runner` file *is* the runner's identity: `id: 3`, a `uuid`, and a token bound to that
registration. Forgejo tracks heartbeat, dispatched task, and **server-side concurrency accounting per
runner id**. Two daemons presenting id 3 would both poll one task queue under one identity, so the
server dispatches without knowing which daemon claimed a task, and both report status for the same
task id. Sharing would also yield `capacity: 5` total rather than 10. The file says it itself:
*"Removing this file will cause act runner to re-register as a new runner."*

---

## 1. Goal

Run a second Forgejo `act_runner` pinned to `k8s-amd64-1`, so CI jobs are distributed across two
nodes instead of all landing on prusik.

Payoffs: more CI throughput; and prusik's CI peak drops, which matters because prusik is RAM-bound
and CI builds there have crashed k3s via etcd timeout
(`docs/troubleshooting/velero-2026-09-25-oom-etcd-outage.md`). Lower CI peaks are a prerequisite for
TODO-6.6 (raising `zfs_arc_max_gb` from 5).

## 2. Non-goals

- **No kata.** The runner stays on runc like `ci-runner`. Kata was already tried and deliberately
  abandoned here: the kata `ScaledJob` is commented out of `kustomization.yaml:9-10` with *"Ephemeral
  workers need caches for forgejo runner and docker, so I will keep a simpler config"*, and
  `statefulset.yaml:317` carries *`# not working with kata runtime`*. Kata's only users are
  `apps/hermes{,-2,-3,-4}`. `kata-deploy`'s `kubernetes.io/hostname: prusik` selector is left alone.
- **Not** migrating `ci-runner-0`; it keeps its two 50 GiB ZFS PVCs. Migration stays gated on
  TODO-9.2 (2x16 GB DDR4-3200).
- **Not** adding a StorageClass (TODO-6.3 decision).

## 3. Measured starting point

### k8s-amd64-1

| quantity | value |
|---|---|
| CPU / memory allocatable | 11 / **12.53 Gi** |
| Memory requests allocated today | **180Mi (1%)**; limits 1736Mi (13%) |
| Actual usage | 685Mi (5%), 102m CPU |
| `/` filesystem | 113.8 GiB total, **100.7 GiB avail**, 11.5% used, ext4 on LVM |
| kubelet eviction | `memory.available<500Mi,nodefs.available<10%` (`metal/inventory/host_vars/k8s-amd64-1.yml:26`) |
| ZFS pool | none |
| NIC | `enp2s0` @ 1000Mb/s (TODO-9.4 open) |

### Existing runner

`platform/ci-runners/` is a **kustomization** with an explicitly enumerated `resources:` list.
StatefulSet `ci-runner`, 1 replica, runc, 4 containers (`runner` forgejo-runner 13.2.0 at 18Gi/14CPU
limits, `daemon` docker:29.8.1 at 8Gi/4CPU, `docker-gc`, `workspace-gc`), `privileged: true`,
`shareProcessNamespace: true`, **no nodeSelector** — it sits on prusik only because its PVCs are
ZFS-topology-bound. A hostPath runner has no implicit pinning, so pinning must be explicit.

- `capacity: 5` (`resources/configmap.yaml:23`); labels come from the `.runner` file, not the
  configmap (`labels: []` at `configmap.yaml:61`)
- docker-gc: `df -P /var/lib/docker`, `CHECK_INTERVAL=1800`, `LOW=80 MED=85 HIGH=90 CRITICAL=95`
  (`statefulset.yaml:181-185`)
- workspace-gc: `df -P /workspace`, `CHECK_INTERVAL=15`, `LOW=70 MED=80 HIGH=90 CRITICAL=95`,
  `PRESERVE_DIRS=".pnpm-store tool_cache"` (`resources/workspace-gc-script.yaml`)
- KEDA `ScaledJob` is commented out (`kustomization.yaml:9-10`); only a `TriggerAuthentication` is
  live → no autoscaling interference
- CronJob `ci-runner-cache-cleanup` daily, mounts the cache PVC at `/cache`

---

## 4. Storage: one hostPath parent, three subdirs

All under `/srv/ci-runner/`, all `type: Directory` — **not** `DirectoryOrCreate`, which silently
creates a root-owned dir while the runner is uid 1001, giving a permission error that reads like an
app bug.

| subdir | mount | owner | mode | containers |
|---|---|---|---|---|
| `docker/` | `/var/lib/docker` | `root:root` | `0711` | daemon (rw), docker-gc (ro) |
| `cache/` | `/cache` | **`1001:1001`** | `0775` | runner, daemon |
| `workspace/` | `/workspace` | **`1001:1001`** | `0775` | runner, daemon |

**Ownership must come from Ansible**: kubelet does *not* apply `fsGroup`/`fsGroupChangePolicy` to
`hostPath`, so the existing `supplementalGroups: [1001]` will not fix `cache/` or `workspace/`. This
is the concrete reason the TODO-6.3 decision required Ansible-created paths.

Consequences of moving workspace off tmpfs:

- The existing runner's workspace is `emptyDir: {medium: Memory, sizeLimit: 8Gi}`
  (`statefulset.yaml:294-297`). On a 12.53 Gi node an 8 GiB tmpfs would reserve **64% of allocatable
  RAM** — unacceptable. hostPath puts it on the 100.7 GiB SSD instead.
- **`sizeLimit` no longer bounds it.** Two guards remain: `workspace-gc` (15s interval, retention
  pruning, preserves `.pnpm-store tool_cache`) and the root-fs disk thresholds. Both need retuning
  (§5.2), because `workspace-gc`'s `HIGH=90` currently coincides with kubelet's eviction trigger.
- hostPath **persists across pod restarts**, unlike emptyDir. Benefit: warm checkouts and a surviving
  `.pnpm-store`. Cost: stale workspace data, which is exactly what `workspace-gc` exists to handle.
- **Nothing to orphan on deletion.** Contrast the existing runner: deleting its PVCs would strand
  ~100 GiB of ZFS on prusik, because `openebs-zfspv` is `reclaimPolicy: Retain` — the mechanism
  behind the 6 leaked `Released` PVs in TODO-8.
- **Not backed up, correctly**: docker layers, action caches and workspaces are reproducible, and
  Velero's zfs plugin does not cover hostPath anyway (node-agent is not deployed).

## 5. Sizing

### 5.1 Memory — capacity 5, limits at the safe ceiling

`capacity: 5` is what D1 asked for, with OOMs accepted. The arithmetic, stated plainly:
`ci-runner-0` **peaks at 17.4 GiB RSS at capacity 5** (~3.5 GiB per concurrent job), and this node
has **12.53 Gi total**. Demand exceeds the node's entire memory, so limits cannot prevent OOM — they
only choose *what* dies.

Ceiling: 12.53 − 1.7 (existing limits) − 0.5 (eviction reserve) ≈ **10.3 Gi** for this pod.

| container | requests | limits |
|---|---|---|
| `runner` | 1Gi / 200m | **5Gi** / 6 CPU |
| `daemon` | 512Mi / 250m | **5Gi** / 3 CPU |
| `docker-gc` | 32Mi / 10m | 128Mi / 100m |
| `workspace-gc` | 16Mi / 10m | 64Mi / 50m |
| **pod total** | **~1.55Gi / 470m** | **~10.2Gi / 9.15 CPU** |

This keeps the **pod cgroup** as the OOM boundary: a runaway job kills `ci-runner-amd64-0`, never the
node. **At capacity 5 the failure is coarse**: the kernel picks the largest process in the cgroup,
and dockerd lives there, so one OOM can kill the daemon and **all five in-flight jobs at once**,
followed by a daemon restart. That is the accepted trade-off.

The `runner`/`daemon` split matters: `:host`-label jobs execute *inside the runner container* (so its
5Gi bounds them), while `docker`-label jobs are siblings created by dockerd and land in the
**daemon's** cgroup. Under-sizing the daemon would silently cap docker jobs below the runner's.

No pod-level `resources` block — container-level limits only, matching the existing StatefulSet
(whose pod-level block is commented out at `statefulset.yaml:317-324`).

### 5.2 Disk — both GC loops need retuning

Both scripts read `df` of their mount. Today those are dedicated volumes; with hostPath they report
the **113.8 GiB root filesystem**, shared with the OS. Current thresholds then sit past kubelet's
`nodefs.available<10%` trigger:

| tier | docker-gc | workspace-gc | free at that % | verdict |
|---|---|---|---|---|
| LOW | 80% | 70% | 22.8 / 34.1 GiB | late |
| MED | 85% | 80% | 17.1 / 22.8 GiB | late |
| HIGH | 90% | 90% | **11.4 GiB** | **exactly kubelet's eviction trigger** |
| CRITICAL | 95% | 95% | 5.7 GiB | fires after the node is already evicting |

Proposed for the new runner only — **docker-gc 60/70/78/85**, **workspace-gc 55/65/75/82** — so the
most aggressive tier still leaves ~17 GiB, comfortably above the 11.4 GiB eviction line.

Budget: docker ~50 GiB + cache ~20 GiB + workspace ~15 GiB = 85 GiB of the 100.7 GiB available.

Implementation: parameterise the four assignments in both scripts as
`THRESHOLD_LOW="${THRESHOLD_LOW:-80}"` sourced from container `env`, **defaults unchanged** so the
prusik runner behaves identically. Also `CHECK_INTERVAL` 1800→600 for docker-gc on the new runner: a
shared 113.8 GiB disk fills faster than a dedicated volume, and 30 minutes of a heavy build is a lot
of unobserved growth.

`NodeFilesystemSpaceFillingUp` already exists in the `node-exporter` rule group (verified loaded,
`inactive`), so node disk pressure is alerted — no new PrometheusRule needed.

#### Parameterising the shared scripts will restart `ci-runner-0`

The docker-gc script is **inline in the StatefulSet's container args**, so changing it changes the pod
template and ArgoCD rolls `ci-runner-0`. The workspace-gc script is a shared ConfigMap, and this repo
runs reloader. `terminationGracePeriodSeconds: 30` cuts the pod off after 30s even though the runner
is configured with `shutdown_timeout: 3h`, so **in-flight CI jobs on prusik would be killed**.

Two ways to avoid that:

| option | cost |
|---|---|
| **A. Parameterise anyway, sync in a quiet CI window** (recommended) | one deliberate restart; keeps a single copy of each script so they cannot drift |
| B. Leave the shared files untouched; inline the retuned thresholds in `statefulset-amd64.yaml` and add a `workspace-gc-script-amd64` ConfigMap | zero impact on the running runner, but two copies of each shell script that will drift |

Recommendation: **A**, with the Phase 3 sync timed against `git.grigri.cloud` activity, and
`ci-runner-0` confirmed idle first.

### 5.3 Placement

`nodeSelector: {kubernetes.io/hostname: k8s-amd64-1}` on the new StatefulSet **and** on the new
cache-cleanup CronJob (the CronJob must land on the same node to see the hostPath).

Hostname pinning rather than a new label: hostPath is inherently node-bound, so the selector should
say so plainly.

---

## 6. File-by-file changes

### New, under `platform/ci-runners/resources/`

| file | contents |
|---|---|
| `configmap-amd64.yaml` | copy of `configmap.yaml`, name `runner-config-amd64`, `capacity: 5` |
| `external-secret-runner-amd64.yaml` | ExternalSecret `runner-config-amd64` → Vault `git/runner-amd64`, emits the stored `.runner` verbatim (no template, no id/uuid in git) |
| `statefulset-amd64.yaml` | StatefulSet `ci-runner-amd64`: §5.1 resources, three hostPath subdirs, nodeSelector, `THRESHOLD_*`/`CHECK_INTERVAL` env |
| `cronjob-amd64.yaml` | cache cleanup via hostPath + nodeSelector |

### Modified

| file | change |
|---|---|
| `platform/ci-runners/kustomization.yaml` | add the 4 new resources — the list is enumerated, not globbed |
| `platform/ci-runners/resources/statefulset.yaml` | docker-gc thresholds → env with **unchanged defaults** |
| `platform/ci-runners/resources/workspace-gc-script.yaml` | workspace-gc thresholds → env with **unchanged defaults** |
| `metal/inventory/host_vars/k8s-amd64-1.yml` | add `ci_runner_dirs` (§4 paths/owners/modes) |
| `metal/playbooks/install/k8s-ci-runner-dirs.yml` (new) | create the hostPath dirs; tag `ci-runner-dirs`; `hosts: k3s_cluster`; driven by `ci_runner_dirs` |
| `metal/playbooks/install/cluster.yml` | import that play, mirroring `k8s-node-labels.yml` at line 18 |

New image refs carry the same Renovate hint comments as the existing StatefulSet so
`forgejo-runner`/`docker`/`busybox` keep auto-updating in both places.

## 7. Execution

**Phase 0 — approval.** Sign-off on §5.1 sizing and §5.2 thresholds.

**Phase 1 — Ansible dirs** (I commit, then run; permitted by AGENTS.md as committed config with
explicit `--tags`/`--limit`):
```bash
cd metal && ANSIBLE_EXTRA_ARGS="-t ci-runner-dirs --limit k8s-amd64-1" make cluster
```
Verify all three dirs exist with the §4 owners/modes. I report the command and output.

**Phase 2 — Forgejo registration + Vault (you — needs admin access I do not have).**
1. Forgejo → Site Administration → Actions → Runners → *New Runner* → copy the token
2. ```bash
   docker run -v /var/run/docker.sock:/var/run/docker.sock -v "$PWD:/data" --rm \
     code.forgejo.org/forgejo/runner:13.2.0 forgejo-runner register --no-interactive \
     --token "$REG_TOKEN" --name runner-amd64 --instance https://git.grigri.cloud/ \
     --labels ubuntu-latest:host,ubuntu-24.04:host,docker
   vault kv put git/runner-amd64 runner="$(cat .runner)"
   ```
3. Tell me it is done — with D3 I need neither the id nor the uuid.

**Phase 3 — manifests.** I commit §6, push, let ArgoCD sync. No `kubectl apply`, no `helm install`.

**Phase 4 — verification** (read-only): `ci-runner-amd64-0` `4/4 Running` on k8s-amd64-1; Forgejo
shows `runner-amd64` **Online**; `kubectl top pod`; node stays `DiskPressure=False` /
`MemoryPressure=False`; GC logs show the retuned thresholds against the root fs; a trivial workflow
gets picked up; then watch `kube_pod_container_status_last_terminated_reason{reason="OOMKilled"}` for
`ci-runner-amd64-0`.

**Phase 5 — docs.** New `docs/deployment/ci-runners.md` (none exists; the runner is described only in
passing across five files) covering both instances, the sizing rationale, and the registration
runbook. Update `docs/hardware/k8s-amd64-1.md`, `planning/k8s-amd64-1-node-addition.md` (TODO-6.4),
and `docs/conventions/prusik-fast-storage-tier.md`.

## 8. Risks

| risk | severity | mitigation |
|---|---|---|
| **capacity 5 exceeds node RAM** (17.4 GiB demand vs 12.53 Gi) → OOM kills, possibly taking dockerd and all 5 jobs | **high, accepted (D1)** | limits at the 10.2 Gi ceiling so the pod, not the node, is the boundary; watch OOMKilled; lowering capacity later is a one-line change |
| Docker or workspace fills the root fs → `DiskPressure`, evictions | medium | §5.2 retuned thresholds, all firing above the 11.4 GiB eviction line; 600s interval; existing `NodeFilesystemSpaceFillingUp` alert |
| Workspace hostPath loses `sizeLimit` | medium | `workspace-gc` (15s) + retuned thresholds; `.pnpm-store`/`tool_cache` preserved |
| `cache/`/`workspace/` root-owned → runner cannot write, looks like an app bug | medium | `type: Directory` + Ansible sets `1001:1001`; kubelet applies no fsGroup to hostPath |
| `privileged: true` + hostPath = full node access for CI jobs | accepted | unchanged from today's runner; no PSA enforcement anywhere in the cluster. Blast radius is a node holding no critical storage |
| 1 GbE link (TODO-9.4) → slow image pulls, slower jobs than prusik | low-medium | measure job durations in Phase 4 |
| Registration token committed by accident | high | Phase 2 is yours; token goes to Vault only; D3 keeps even the uuid out of git |
| Stale workspace data persists across restarts | low | `workspace-gc` retention pruning |

## 9. Rollback

Remove the four new resources from `kustomization.yaml` and let ArgoCD prune. The existing runner is
untouched (distinct names; the only shared-file edits are env-with-defaults). Residue: the
`/srv/ci-runner/` dirs (harmless; remove by dropping `ci_runner_dirs` and re-running the tagged play)
and the `runner-amd64` registration, which should be deleted in the Forgejo UI so it does not linger
offline.

## 10. What this does not fix

- prusik still runs `ci-runner-0` and can still peak at 17.4 GiB; migration stays gated on TODO-9.2.
- TODO-9.4 (1 GbE negotiation) is open and now affects CI throughput directly.
- The 6 `Released` PVs / ~13.5 GiB of leaked ZFS (TODO-8) are untouched.

---

## 11. Result — deployed 2026-10-01

Commits `1bc41b09` (Ansible dirs), `52d7084f` (runner), `0dc6d7c2` + `1121d0a2` (limit trim).

Verified after rollout:

- `ci-runner-amd64-0` **4/4 Running** on `k8s-amd64-1`, 0 restarts
- Forgejo admin API: `id=4 name=k8s-amd64-1 status=idle version=v13.2.0` with labels
  `[ubuntu-latest, ubuntu-24.04, docker]`; `id=3 name=prusik status=idle` — both runners share one
  job pool, as D1 intended
- runner log: `declared successfully` then `[poller] launched`
- ExternalSecret `runner-config-amd64` → `SecretSynced` / `True`
- ArgoCD `ci-runners` → `Synced` / `Healthy`
- node `DiskPressure=False MemoryPressure=False Ready=True`
- docker-gc: `docker disk usage: 14%` … `sleeping for 600s` — retuned interval live, and it is
  reading the root fs rather than a dedicated volume
- kubelet node accounting: memory limits **12168Mi (94%)**, cpu limits 10650m (96%)
- Ansible: three dirs created, `ok=1 changed=1 failed=0`
- `ci-runner-0` restarted once as predicted by §5.2 (71s age at first observation), back to 4/4

### Deviation 1 — registration used the Forgejo v15 connection model, not a `.runner` file

D3 assumed "store the whole `.runner` in Vault". The instance runs Forgejo **15.0.9**, whose UI issues
a `uuid` + `token` pair for `server.connections.<name>`, and `forgejo-runner 13.2.0` marks both
`register` and `create-runner-file` as **deprecated** while supporting `--url/--uuid/--token-url`.
What shipped instead:

- `configmap-amd64.yaml` carries `server.connections.grigri.{url, uuid, token_url}` with
  `token_url: file:/config/token`; the inline `token` and `token_url` forms are mutually exclusive
- the ExternalSecret emits a single `token` key from Vault **`secret/git/runner-k8s-amd64-1`** — the
  KV mount is `secret`, so ESO's `key: /git/...` resolves underneath it
- `runner.file` is omitted and the `cp /config/.runner .` step is gone
- labels are explicit in `runner.labels`; there is no `.runner` file to fall back to

Net effect is better than planned: no id-, uuid- or token-bearing credential in git at all.
**`ci-runner` on prusik is still on the deprecated `.runner` path** and should be migrated.

### Deviation 2 — limits are 4608Mi, not 5Gi

§5.1 budgeted against 1736Mi of pre-existing node limits; the node actually carries 2760Mi. At
5Gi+5Gi kubelet reported memory limits of **13192Mi against 12831Mi allocatable (102%)**, which
defeats the property the sizing existed for — if every container hit its limit at once the *node*
would go into pressure, not just the pod. 4608Mi each gives pod 9408Mi, node 12168Mi = 94.8%, with
663Mi of slack above the 500Mi eviction reserve. Capacity is unchanged at 5.

Process note: `0dc6d7c2` only changed the daemon, because two edits to the same file were issued
concurrently and the second overwrote the first. `1121d0a2` fixed it. **Verify rendered output, not
edit-tool success messages.**

### Still open

- `docs/deployment/ci-runners.md` does not exist — the runner is documented only in passing across
  five files (Phase 5 of §7 not done)
- Watch `kube_pod_container_status_last_terminated_reason{reason="OOMKilled"}` for
  `ci-runner-amd64-0`: capacity 5 on 12.53Gi will OOM, by design and by decision
- Migrate the prusik runner off the deprecated `.runner` registration
- CPU limits on the node are at 96% — harmless, since CPU is compressible, but there is no burst
  headroom left
- Confirm a real workflow actually lands on the new runner; both were `idle` at verification time
