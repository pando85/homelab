# Rust build caching for concurrent agents and CI

## Status and recommendation

**Proposal, not deployed.** Evidence collected on 2026-11-06. Recheck versions, storage and
performance before implementation. This document changes no runner, workstation or bucket config.

Use three complementary layers:

1. **A separate Cargo target directory per active development worktree.** Keep incremental
   compilation enabled for the normal edit/check/test loop.
2. **One bounded local sccache per machine**, shared by that machine's worktrees and jobs. Preserve
   fast local hits rather than replacing them with network reads.
3. **An optional shared S3-compatible secondary cache**, evaluating a dedicated private bucket in
   an existing MinIO on prusik first. Adopt it only if representative concurrent builds improve
   without harming other workloads.

Do not deploy the previously suggested 512 MiB Redis as a replacement for the existing caches.
Do not assume MinIO is faster than local storage. The benefit is avoiding compilation on a local
miss, not accelerating a local hit.

## Evidence from the 2026-11-06 investigation

### Runner caches

Read-only inventories compared cache filenames (keys) and file sizes while builds could still run.
The counts are point-in-time snapshots, not a transactionally consistent trace of requests.
Sizes below are stored artifact bytes, not filesystem allocation or Redis memory requirements.

| Measurement | Result |
|---|---:|
| prusik runner | 8,004 entries / 10.00 GiB |
| k8s-amd64-1 runner | 7,121 entries / 10.00 GiB |
| Keys present in both | 2,492 entries / 1.12 GiB |
| Union of retained keys | 12,633 entries / 18.87 GiB |
| k8s-amd64-1 median entry | 280 KiB |
| k8s-amd64-1 p90 / p99 entry | 2.87 MiB / 14.8 MiB |
| Largest k8s-amd64-1 entry | 125 MiB |

Both runners are amd64 and both caches are at their configured size limit. Different retained
content suggests an opportunity for sharing and longer retention. **It does not establish how
many misses on one machine could be satisfied by the other.** That needs a request-level benchmark.

prusik's runner cache is on a `fast-ci-zfspv` PVC backed by the fast pool's SATA SSD mirror, not
NVMe. k8s-amd64-1 uses `/srv/ci-runner/cache` as a hostPath. Both survive ordinary pod restarts;
manual cleanup, volume loss and eviction of individual cache entries are different events.

### CI performance

Inspected job logs contained builds spanning 2026-11-04 through 2026-11-06. Fifty-two logs reported
sccache statistics, but some jobs shared a daemon and counters overlapped. Do not add their
counters together and present the result as unique compilations or aggregate time saved.

Examples of validation-job snapshots that started with zero reported counters:

| Snapshot | Hits | Misses | Cacheable hit rate | Average cache-hit read |
|---|---:|---:|---:|---:|
| Warm example | 1,075 | 469 | 69.6% | 12 ms |
| Almost cold example | 2 | 1,703 | 0.1% | Rounded to 0 ms |
| Cold example | 0 | 738 | 0% | Not applicable |
| Later warm example | 562 | 176 | 76.2% | 15 ms |

Hit rate here is `hits / (hits + misses)`, excluding non-cacheable calls. Even zero initial
counters do not prove exclusive daemon use throughout a job. Reported average compiler times
varied substantially, often around 0.3–2 seconds and higher in some release jobs. Compiler
invocation time is not end-to-end build wall time: compilation overlaps and tests/linking remain.

There is **no measured shared-cache speedup** yet. Neither a 2–3x improvement nor a specific
number of minutes saved is established by these observations.

### Local development

The inspected Calypso checkout had a 17 GiB target directory: 6.3 GiB in `debug/deps`, 7.5 GiB in
`debug/incremental`, and 463 MiB in `debug/build`. It had 89 registered worktrees, not necessarily
active ones. Per-worktree isolation therefore needs an explicit disk-retention policy.

Its `.envrc` directs local worktrees to the main checkout's target directory using Git's common
directory. Its `justfile` derives contract-binary paths from `CARGO_TARGET_DIR`. The inspected
non-interactive shell had not loaded direnv; the presence of `.envrc` alone does not establish
that an agent command uses its environment.

Local sccache was 0.18.0, while inspected CI logs used 0.19.0. The Rust cache-key version changes
from 6 to 7 across these releases, preventing reuse of those Rust entries. A small earlier local
daemon sample reported 22 requests, all non-cacheable; it is not a long-term usage baseline.

## What Cargo and sccache reuse

Cargo and rustc make separate decisions:

- **Cargo freshness:** unchanged inputs/settings can make a crate fresh. Cargo skips rustc
  entirely, so neither incremental compilation nor sccache needs to do work.
- **rustc incremental compilation:** for a dirty crate, Cargo can pass `-C incremental=<path>`.
  rustc reuses intermediate compiler state in that worktree's build directory. The first build
  can have incremental enabled even when there is no previous state to reuse.
- **sccache:** for an eligible non-incremental compiler invocation, reuse its complete outputs
  by a key derived from source, dependencies, compiler identity, arguments and relevant environment.

By default, dev/test builds enable incremental compilation for workspace members and path
dependencies; registry dependencies are non-incremental. Release builds default to
non-incremental. Profiles can override this, as can `CARGO_INCREMENTAL=0` or `1` for eligible crates.
Use `cargo build -vv` on a build that needs compilation to inspect the actual compiler arguments.

An actual runner entry contained an `.rlib`, an `.rmeta` and `stderr`: compiled Rust library code,
metadata and diagnostics packaged as an archive. Other entries reflect their compiler outputs.
They are not entire Cargo target directories.

Important limits, based on sccache 0.19.0:

- Incremental Rust invocations are not cacheable. Retain incremental for interactive workspace
  development; eligible non-incremental dependencies can still use sccache.
- Normal metadata-only `cargo check` invocations do not meet the documented requirement that
  `--emit` include `link`. Do not promise cross-worktree check reuse through sccache.
- Crates invoking the system linker, including binaries and proc-macros, are not cached by this
  Rust mechanism, although their eligible dependencies can be cached.
- Build-script execution, Cargo downloads, test execution and Docker images are not cached by
  Rust compiler-output caching. Misses still compile on the requesting machine.

## Local development across concurrent worktrees

### Isolate mutable build state

A single shared target saves space and can suit sequential branch switching. For simultaneous
agents, Cargo's build-directory locks can serialize builds, switching sources/settings can cause
recompilation, and final executable paths are shared. A recipe that builds then separately runs a
binary can encounter another worktree's output. `cargo clean` also affects every user of that target.

Prefer one target per active worktree. An illustrative replacement for the shared-target portion
of Calypso's `.envrc`, preserving its separate local-environment loading, is:

```bash
if [[ -z "${CI:-}" ]]; then
  export CARGO_TARGET_DIR="$(git rev-parse --show-toplevel)/target"
fi
```

This explicitly replaces a target inherited from another checkout. Apply it in the application
repository only after auditing scripts that assume a hardcoded `target/debug` path. Keep build
and run commands bound to the same worktree and environment. Ensure agent shells explicitly load
an approved environment or otherwise set the target; do not assume interactive direnv hooks run.

### Keep incremental and share the local compiler cache

Retain the existing workstation `rustc-wrapper = "sccache"` and `jobs = 2` as initial settings.
Align sccache versions with CI, then evaluate a 20 GiB local cache budget. This is a proposed
starting size, not a measured optimum. Do not disable incremental globally just to raise the
sccache hit percentage.

Use the same Cargo target within a worktree's edit/check/test cycle. Keep feature sets, package
scope and dev/test settings consistent where semantically appropriate; unnecessary build-graph
changes reduce reuse. Never remove test coverage simply to improve caching.

`jobs = 2` applies per Cargo invocation, not across all agents; parallel builds and linkers can
still exhaust memory. Retain targets for active worktrees and remove inactive build output only
after checking it is unused. At the observed size, three similar targets plus a 20 GiB sccache
could approach 71 GiB before filesystem compression and workload differences. Do not create warm
targets for every registered worktree. Defer a leased pool of build directories unless measurements
justify its additional coordination complexity.

## Shared MinIO as a secondary cache

```text
Worktree A -> isolated target --+
Worktree B -> isolated target --+-> workstation local sccache --+
                                                              |
CI runner -> isolated job target -> runner local sccache -------+-> private MinIO bucket
                                                              |
Other CI runner -> job target -> runner local sccache ----------+
```

The read path is local first, then MinIO, then compilation. A remote hit backfills local storage.
A local hit avoids a remote artifact lookup. A remote miss adds a lookup before compiling.

Evaluate reuse of an existing MinIO rather than deploying a new service immediately, but first
identify its actual backing storage, free space, request latency and other consumers. Existing
MinIO placement on prusik does not imply it uses fast CSI. If measurements justify a dedicated
fast-CSI-backed backend later, treat that as a separate storage decision.

| Option | Warm local data | Cross-machine reuse | Main drawback |
|---|---|---|---|
| Independent local caches | Fast | None | Duplicate compilation and separate retention |
| Remote-only MinIO | Network read | Yes | All hits depend on remote performance/availability |
| Local then MinIO | Local read | Yes, on local misses | Added lookup/upload load and retained local disk use |

For the proposed version, the illustrative configuration shape is:

```bash
SCCACHE_MULTILEVEL_CHAIN=disk,s3
SCCACHE_MULTILEVEL_WRITE_ERROR_POLICY=l0
# Configure SCCACHE_DIR and SCCACHE_CACHE_SIZE for the local level.
# Configure SCCACHE_BUCKET, SCCACHE_ENDPOINT and authentication separately.
```

Setting disk and S3 variables alone does not imply a hierarchy. Verify the chosen binary supports
multi-level caching and restart its daemon in a controlled idle period when changing backend
configuration. Never interrupt a shared daemon under active builds.

### Slow writes and concurrent load

In the inspected 0.19.0 implementation, `l0` writes the local level first and spawns secondary
uploads as background tasks. There is still an awaited remote capability check in secondary-write
setup. Do not describe this as zero-overhead, guaranteed non-blocking publication. Remote reads,
misses, checks, retries and timeouts can add latency. Test outages rather than promising automatic,
instant local fallback. The `all` write policy waits for remote completion and is not recommended
for this disposable secondary cache.

Background uploads consume bandwidth and memory and are not a durable delivery queue. Failed
uploads or daemon termination can leave entries unpublished. Another machine may compile the same
entry while its first upload is still in progress. Concurrent reads/writes can contend with MinIO's
other consumers and degrade its otherwise acceptable read performance.

For scale only: three machines generating 2 GiB each would need about five minutes to upload their
combined 6 GiB at 20 MiB/s aggregate throughput, excluding request overhead. Those are illustrative
numbers, not measured MinIO performance. Benchmark artifact-sized objects and concurrent traffic,
not only a single large sequential transfer.

A 125 MiB artifact takes at least roughly one second over a hypothetical 1 Gbit/s link. A remote
hit helps only when retrieval/restoration costs less than the avoided compilation. For the
requests missing locally, estimate:

```text
benefit = remote_hit_fraction * (compile_time - remote_hit_time)
          - remote_miss_fraction * remote_miss_lookup_time
```

These are per-request estimates; account separately for publication overhead, contention and
parallelism. End-to-end wall time remains the acceptance metric.

### Capacity and retention

Keep current local limits initially to avoid confounding the experiment. A 40–60 GiB remote budget
is a sizing hypothesis based on the 18.87 GiB retained union, not a working-set measurement.
Local-first storage does not free the existing roughly 10 GiB on k8s-amd64-1. Reducing its local
limit later trades disk relief for more remote accesses; it does not fix Docker-image growth.

`SCCACHE_CACHE_SIZE` bounds only the local backend, not the bucket. Define lifecycle rules,
capacity monitoring and any quota behavior explicitly. Age-based object expiration is not
access-based LRU. An existing shared MinIO must retain operational headroom for other workloads.

## Compatibility and security

- Align sccache versions/cache format and the pinned Rust toolchain. Different profiles, features,
  target triples, compiler/sysroot paths, relevant environment and flags can prevent hits.
- Validate path normalization/remapping with representative builds. Sharing a backend does not
  automatically make local and CI keys compatible; do not change embedded path semantics blindly.
- Use a dedicated private bucket or namespace, scoped credentials and TLS on non-local links.
  Artifacts may expose proprietary code, diagnostics and debug paths. Do not use public artifacts
  storage access rules as the model for a compiler cache.
- Treat writers as trusted: shared compiled outputs create a cache-poisoning boundary. Define
  read/write policy by trust level and separate untrusted builds. Do not commit credentials or
  details of which personal account/token has which privileges.
- Keep secrets in the established secret-management flow; use separate cache authentication from
  unrelated application data. Read-only remote access is an option for an initial consumer trial.

## Validation and rollout

1. **Baseline local behavior.** Confirm the effective environment/target directory, versions,
   incremental settings and build recipes. Record wall time, peak memory, target/cache growth and
   build-directory lock waits. Do not reset statistics on a daemon used by unrelated jobs.
2. **Trial worktree isolation separately.** Compare sequential and simultaneous representative
   builds with the current shared target versus per-worktree targets. Include an edit/check/test
   loop, not only a clean release build. Keep code, toolchain, flags and build scope equivalent.
3. **Measure the candidate MinIO.** Use a dedicated disposable benchmark namespace with explicit
   approval. Measure GET hits, absent-object lookups, PUT p50/p95/p99, and aggregate throughput
   under mixed concurrent load. Include its other consumers in impact monitoring.
4. **Trial local plus remote on a small scope.** Use an isolated daemon/cache or otherwise exclusive
   measurement window. Use equivalent starting Cargo targets and local caches for local-only and
   local-plus-remote comparisons. Never wipe the normal developer/runner cache for a benchmark.
5. **Exercise the actual cases:** a warm repeat build; a cross-machine build after publication;
   changed workspace code; and three simultaneous builds. Also test unreachable/slow storage,
   authentication failure and daemon exit with uploads outstanding. Use a local-only rollback
   configuration and confirm behavior before depending on it.
6. **Record per-level evidence.** Capture local hits, remote hits, remote misses, upload/check
   errors and latency, bytes transferred, build wall time, memory and storage effects. Isolate
   counters or use exclusive-window deltas; shared-daemon snapshots are not per-job measurements.
7. **Adopt only on measured benefit.** Require reproducible cross-machine improvements, no material
   warm-build regression, tolerable outage behavior and no unacceptable impact on other services.
   Agree numerical regression/resource budgets before the trial. If remote hits rarely avoid
   meaningful compilation, keep independent local caches.
8. **Tune only after adoption.** Adjust retention and local budgets from observed working sets.
   Keep workstation and CI config changes separate, version-controlled where applicable, and use
   the repository's approval/commit-first deployment workflow. This proposal authorizes no rollout.

## Corrections to the earlier Redis proposal

The original future-improvement section in the
[runner disk-pressure incident](../troubleshooting/ci-runner-amd64-disk-full.md) is superseded:

- HostPath/PVC caches already survive normal runner pod restarts. A remote cache is not required
  for that property, and a volatile Redis loses data on its own restart.
- 512 MiB is only about 5% of one current runner's artifact cache, before Redis overhead.
- Ordinary Redis serves its dataset from RAM. A fast CSI PVC holds RDB/AOF persistence, not a
  larger SSD-resident cache behind a tiny RAM allowance. With persistence disabled it adds no
  cache durability. Retaining the observed union would need roughly 19 GiB of dataset RAM plus
  overhead, an unattractive use of prusik memory.
- Selecting a single remote backend does not imply an automatic local hierarchy or reliable
  fallback. Configure and test multi-level behavior explicitly.
- `SCCACHE_REDIS` is deprecated in the inspected release; any future Redis experiment should use
  `SCCACHE_REDIS_ENDPOINT` and separate authentication settings.
- No 2–3x speedup is established. Local-first MinIO is a hypothesis to benchmark, not a promised
  improvement or a solution to the primary Docker disk-pressure cause.

## References

- [Cargo build cache and target directories](https://doc.rust-lang.org/cargo/reference/build-cache.html)
- [Cargo incremental profile settings](https://doc.rust-lang.org/cargo/reference/profiles.html#incremental)
- [sccache 0.19.0 Rust limitations](https://github.com/mozilla/sccache/blob/v0.19.0/docs/Rust.md)
- [sccache 0.19.0 multi-level configuration](https://github.com/mozilla/sccache/blob/v0.19.0/docs/MultiLevel.md)
- [sccache 0.19.0 multi-level implementation](https://github.com/mozilla/sccache/blob/v0.19.0/src/cache/multilevel.rs)
- [sccache 0.19.0 Redis backend](https://github.com/mozilla/sccache/blob/v0.19.0/docs/Redis.md)
- [Fast storage tier on prusik](prusik-fast-storage-tier.md)
- [Runner disk-pressure incident](../troubleshooting/ci-runner-amd64-disk-full.md)
- Configuration inspected in the application checkout: `.envrc`, `justfile`, `rust-toolchain.toml`,
  `.forgejo/actions/sccache-setup/action.yml`; workstation: `~/.cargo/config.toml`.
