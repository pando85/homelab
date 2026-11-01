# ci-runner-amd64 fills the k8s-amd64-1 root disk

## Problem

On 2026-11-01 at 00:28 CET, kubelet on `k8s-amd64-1` reported DiskPressure at 90% of the 113.8 GiB
root filesystem and evicted these pods, in this order:

1. `ci-runner-amd64-0` (it is `low-priority`, so it goes first)
2. `isidoro-redis`
3. `vector-agent`
4. `smartctl-exporter`
5. `node-exporter`

While the node stayed under DiskPressure, kubelet refused to re-admit the pods ("Pod admission
denied … DiskPressure"). Everything stayed down for about 8 hours, until the CI hostPaths were
wiped by hand.

- The same thing had already happened on 2026-10-31 between 07:16 and 10:16 CET, while the old
  `workspace-gc` sidecar was still running.
- Free space had been trending down for days: 44 GiB free on 10-29, down to 15.8 GiB on 10-31.
- Replacing the sidecar with the runner's native workspace GC (#4702) was **not** the cause, and
  **not** the fix either.

### Timeline (CET)

| time | event |
|---|---|
| 10-31 16:53 | Runner rolled out with the GC-enabled image. The running job was killed (see Open issues) |
| 18:35–20:05 | Usage climbed to 76%, then stayed flat for about 3h at low load. That space was not coming back |
| 23:36–00:27 | 24 jobs, mostly calypso: 79 → 83 → 86 → 89 → 90%. Growth was about 4.5 GiB per 10 min |
| 23:56 | `docker-gc` critical tier (`until=6h`) reclaimed 2.2 GB |
| 00:27 | `docker-gc` critical tier reclaimed **0 B** at 89% |
| 00:28 | kubelet image cleanup freed 1.8 GiB, not enough. Eviction started |
| 00:28–09:08 | Pods were refused while under DiskPressure. Loki went blind after 03:44 (vector-agent evicted) |
| 09:01 | Manual `rm -rf /srv/ci-runner/docker/* /srv/ci-runner/cache/*` freed about 56 GiB |

## Root Cause

All of the runner's hostPaths (`/srv/ci-runner/{docker,cache,workspace}`) sit on the node's only
filesystem, which k3s also uses. The ubuntu-vg volume group has no free space (`VFree 0`), so no
separate volume can be carved out. When CI fills the disk, kubelet evicts **every** pod on the node.

The CI hostPath sizes were:

| path | size | what it is | bounded by |
|---|---|---|---|
| `docker/` (DinD data root) | **most of it**: about 11 GiB just 2h after the wipe | calypso images, about 7 GiB each per commit | `docker-gc` only |
| `cache/sccache` | ≤ 10 GiB | calypso Rust compiler cache | `SCCACHE_CACHE_SIZE=10G` (calypso `sccache-setup`) |
| `cache/cache` | small today | the runner's built-in actions cache server | keeps entries for 30 days if used, 7 days if unused; **no size limit** (`act/artifactcache/caches.go`) |
| `cache/bin`, `cache/sqlx-cli` | small | tool binaries that jobs install into `/cache` | nothing |
| `workspace/` | about 2.7 GiB | `.pnpm-store`, `tool_cache`, action git clones, per-job scratch dirs | native GC (scratch dirs only) |

### Why `docker-gc` reclaimed 0 B

- **The calypso `build-or-retag` workflow** builds `calypso-runner` (about 7.6 GB) and
  `calypso-mcp-verify` (about 7.0 GB), tagged with the commit hash. After the push, no container
  uses them, but nothing removes them either.
- **`docker image prune --filter until=<d>` compares against the image's *creation* time**, not
  when it was pulled or last used.
  - Freshly built images are always "too young". So when fresh builds are what fills the disk,
    an age-based tier finds nothing to delete.
  - The reverse also holds: a base image that was just pulled usually has an old creation date,
    so a short `until` filter deletes it immediately.
- **The check interval was 600s.** A CI burst can cross a whole tier between two checks.
- **`df -P /var/lib/docker` inside `docker-gc` measures the whole node root filesystem**, not just
  Docker's usage. Its `docker disk usage: N%` log lines in Loki are therefore a usable history of
  how full the node was.

### Why kubelet could not help

kubelet's image cleanup only sees the images in k3s's own containerd, under `/var/lib/rancher`
(about 11 GiB). Images inside DinD are invisible to it. CI hostPath data does not count as
container ephemeral storage either, so `container_fs_usage_bytes` and kubelet volume metrics show
nothing for these paths. **Prometheus cannot tell you which path is growing; only `du` on the node
can.**

### Why the native runner GC did not help (and cannot)

These are the `pando85/runner` settings `workdir_cleanup_age` and `idle_cleanup_interval`, added in
commit `755c4cae`:

- **It only deletes per-job scratch dirs** under `host.workdir_parent`. A dir qualifies only if all
  of these hold:
  - its name is exactly 16 hex characters;
  - it contains the `.forgejo-runner-scratch` marker (or has the legacy `act/`, `hostexecutor/`
    and `tmp/` layout);
  - nothing is mounted under it;
  - its mtime (in practice, its creation time) is older than `workdir_cleanup_age`.
- **It never touches** `tool_cache`, action clones, `.pnpm-store`, `/cache`, or Docker.
- **It runs only when the runner is completely idle**, at zero running tasks across all
  connections. A tick that lands while a job is running is skipped, not postponed.
- **It does not run at startup.** The first run is one `idle_cleanup_interval` after boot.
- **It has no size or disk-pressure trigger.**
- **It logs only when it removes something**, `[poller] removed N stale host workspaces`, or
  `failed to clean some stale host workspaces` on error. Skips are silent.
- The runner already deletes each job's dir when the job ends, even if it was cancelled or timed
  out. Leftovers appear only when the process is killed (OOM, pod restart or eviction), when
  `RemoveAll` fails, or when cleanup exceeds its 30-minute timeout.
- **With `workdir_cleanup_age: 24h` it never deleted anything.** The pod restarts (rollouts,
  evictions) more often than every 24h.

### The DiskPressure line was already 90%

`metal/inventory/host_vars/k8s-amd64-1.yml` had `eviction-hard=…,nodefs.available<10%`.

- The flag replaces kubelet's whole hard-eviction config. The k3s defaults
  (`/var/lib/rancher/k3s/agent/etc/kubelet.conf.d/00-k3s-defaults.conf`:
  `nodefs.available: 5%`, `imagefs.available: 5%`) no longer applied.
- The default `NodeFilesystemSpaceFillingUp` warning fires below 15% free and the critical one
  below 10%. On this node that left about 5 GiB between the warning and the eviction, roughly
  10 minutes of a CI burst.

## How to Diagnose

```bash
# Node usage by path (read-only)
ssh k8s-amd64-1 'df -h /; sudo du -xsh /srv/ci-runner/{workspace,cache,docker} /var/lib/rancher'
ssh k8s-amd64-1 'sudo du -xsh /srv/ci-runner/workspace/* /srv/ci-runner/workspace/.[!.]* | sort -h | tail'

# DinD contents. The pod's socket path is longer than the unix-socket limit, so curl fails with
# "Unix socket path too long". cd into its directory and use a relative path instead
ssh k8s-amd64-1 'sudo sh -c "cd /var/lib/kubelet/pods/*/volumes/kubernetes.io~empty-dir/docker-run \
  && curl -s --unix-socket docker.sock http://d/system/df"' \
  | jq -r '.Images | sort_by(-.Size)[] | "\(.Size/1e9) GB ctrs=\(.Containers) \(.RepoTags[0])"'

# Effective kubelet eviction thresholds (read-only)
kubectl --context=grigri get --raw /api/v1/nodes/k8s-amd64-1/proxy/configz | jq .kubeletconfig.evictionHard
```

Loki (`https://loki.internal.grigri.cloud`). Node journald is shipped with a `node_name` label:

```logql
{namespace="ci-runners",pod="ci-runner-amd64-0",container="docker-gc"} |~ "usage|reclaimed"
{namespace="ci-runners",container="runner"} |~ "stale host workspaces"
{node_name="k8s-amd64-1"} |~ "eviction_manager|image_gc_manager|admission denied"
```

When the node itself is under DiskPressure, vector-agent gets evicted too, and Loki shows a gap.
For that window, read the node's journal directly:
`ssh k8s-amd64-1 'sudo journalctl --since … --until …'`. Manual cleanups also show up there as
`sudo` `COMMAND=` lines.

## Fix / Workaround

Applied on 2026-11-01. The filesystem layout was deliberately left as it is: this node only runs
CI, so splitting the disk is not worth it.

- **`docker-gc`** (`platform/ci-runners/resources/statefulset-amd64.yaml`):
  - It checks every 120s.
  - 60%: removes dangling images and stopped containers.
  - 70%: removes unused images and build cache older than 24h.
  - **78%: removes every unused image, all build cache and all unused volumes, with no age
    filter.** Docker itself protects anything in use.
  - Cost: base images get pulled again more often. Rarely, a job may lose an image it just pulled.
- **kubelet** (`metal/inventory/host_vars/k8s-amd64-1.yml`): set
  `nodefs.available<5%,imagefs.available<5%` (95% used, about 5.7 GiB free). That gives about
  19 GiB between the 78% prune and eviction. It was applied with
  `ANSIBLE_EXTRA_ARGS="-t k3s --limit k8s-amd64-1" make cluster`; only the agent restarts, and the
  control plane is not affected.
- **Native runner GC** (`configmap-amd64.yaml`): `workdir_cleanup_age: 4h`, just above
  `runner.timeout: 3h`, so any dir that old cannot belong to a running job.
- **Emergency cleanup:** with the runner stopped, wiping `/srv/ci-runner/docker/*` and
  `/srv/ci-runner/cache/*` is safe. Everything in them can be rebuilt (images get pulled again,
  sccache starts cold).

## Open issues

- **Rollouts and evictions kill running jobs.** On SIGTERM the `daemon` container stops dockerd
  immediately, while the runner waits up to `shutdown_timeout: 3h` and the pod grace period is
  only 30s. You can see it in the runner log: `[poller] shutdown begin, 1 tasks currently
  running`, followed by `Daemon shutdown complete` in the same second. Each kill can also leave
  scratch dirs behind for the native GC.
- **The actions cache server has no size limit** (`/cache/cache`). It is only safe while repos make
  little use of `actions/cache`.
- **Removing images in the producing workflow is the real source fix.** Calypso's `build-or-retag`
  could run `docker image rm` after pushing, so CI wouldn't depend on `docker-gc` racing a burst.
