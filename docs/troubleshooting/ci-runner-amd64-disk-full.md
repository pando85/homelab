# ci-runner-amd64 fills the k8s-amd64-1 root disk

## Problem

On 2026-11-01 at 00:28 CET, kubelet on `k8s-amd64-1` reported DiskPressure at 90% of the 113.8 GiB
root filesystem.

- It evicted `ci-runner-amd64-0`, then redis, vector-agent, node-exporter and smartctl-exporter.
- Loki has a log gap because vector-agent was evicted.
- The runner pod could not be admitted again until the CI hostPaths were wiped by hand.
- The same thing happened on 2026-10-31 between 07:16 and 10:16, while the old `workspace-gc`
  sidecar was still running. Removing that sidecar was not the cause.

## Root Cause

All of the runner's hostPaths (`/srv/ci-runner/{docker,cache,workspace}`) sit on the node's root
filesystem, which k3s also uses. Here is what took the space:

- **DinD `/var/lib/docker` was the main consumer.** The calypso `build-or-retag` workflow builds
  `calypso-runner` (about 7.6 GB) and `calypso-mcp-verify` (about 7.0 GB) for every commit. After
  the push, no container uses those images, but nothing removes them either.
- **`docker-gc` pruned by age, and the age is the image *creation* time** (`docker image prune
  --filter until=`), not when the image was last used. Freshly built images are always "too young".
  The 6h critical tier reclaimed 0 B at 89%, and eviction followed one minute later.
- **The check interval was 600s.** A CI burst grows usage by about 4.5 GiB every 10 minutes.
- **The workspace was small and is not part of the problem.** It holds about 2.7 GiB:
  `.pnpm-store`, `tool_cache`, and action clones. sccache is capped at 10G by calypso's
  `sccache-setup`.
- **The runner's native workspace GC (`workdir_cleanup_age`) cannot help with this.** It only
  deletes leftover per-job folders under `/workspace`, only when the runner is fully idle, and
  only once they are older than the configured age. It never touches Docker, `/cache` or the
  toolcache. With 24h it never fired, because the pod restarts more often than that.

## How to Diagnose

```bash
# Node usage by path
ssh k8s-amd64-1 'sudo du -xsh /srv/ci-runner/{workspace,cache,docker} /var/lib/rancher'

# DinD contents. The pod path is too long for a unix socket, so cd into its directory first
ssh k8s-amd64-1 'sudo sh -c "cd /var/lib/kubelet/pods/*/volumes/kubernetes.io~empty-dir/docker-run \
  && curl -s --unix-socket docker.sock http://d/system/df"' | jq '.Images[] | {Size, Containers, RepoTags}'

# What the GC loops did, and kubelet evictions (Loki)
{namespace="ci-runners",pod="ci-runner-amd64-0",container="docker-gc"} |~ "usage|reclaimed"
{node_name="k8s-amd64-1"} |~ "eviction_manager|image_gc_manager"
```

The native runner GC logs only when it removes something: `[poller] removed N stale host
workspaces`. It is silent when it skips.

## Fix / Workaround

- `platform/ci-runners/resources/statefulset-amd64.yaml` changes to `docker-gc`:
  - It checks every 120s.
  - At 78% it removes every unused image, all build cache and all unused volumes, with no age
    filter. Docker itself protects anything in use. The cost is pulling base images again.
- `metal/inventory/host_vars/k8s-amd64-1.yml` sets `nodefs.available<5%` (95% used). The node only
  runs CI, so this gives the prune tier more room before kubelet starts evicting.
- `workdir_cleanup_age: 4h` is just above the 3h job timeout, so the native GC runs again after a
  restart.
- Emergency cleanup: with the runner stopped, wiping `/srv/ci-runner/docker/*` and
  `/srv/ci-runner/cache/*` is safe. Everything in them can be rebuilt.
