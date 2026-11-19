# sccache keys include every `CARGO_*` env var: cold PR jobs and no desktop hits

## Problem

Rust cache hits stay low although the S3 tier is healthy (0 read/write errors, 0 timeouts, 0 PUT 4xx):

- 2026-11-10..19, ~300 `SCCACHE_STATS` events from Calypso: push jobs (read-write) hit 94.5%, PR jobs
  (read-only) 64.7%. `validation` PR jobs are bimodal: of 116, 59 had >= 90% hits and 45 had < 10%.
- The z800 desktop, configured with read-only S3 access, got 0 S3 hits out of 34.6k lookups. S3 was
  reachable and authenticated; the lookups were real 404 misses.

## Root Cause

The pinned sccache (mozilla/sccache `7e07a6b`) hashes the **name and value of every `CARGO_*`
environment variable** that rustc sees into every Rust cache key. Set versus unset counts, even when
the value is empty. Any difference in those variables between two builds gives disjoint keys.

- Calypso CI exported a custom `CARGO_SCOPE` through `$GITHUB_ENV`: empty on push and full runs, a
  per-PR `-p ...` list in affected mode. Every affected-mode PR therefore missed main's entries.
- The desktop's `CARGO_HOME` value, `CARGO_INCREMENTAL`, `CARGO_BUILD_JOBS`, `CARGO_TARGET_DIR` and
  `CARGO_PROFILE_*_DEBUG` differ from CI's.

Ablation on the desktop with a CI-emulated environment (2026-11-19): 234 Rust S3 hits with the
environment identical. Removing any one of `CARGO_SCOPE`, `CARGO_INCREMENTAL`,
`CARGO_PROFILE_{DEV,TEST}_DEBUG`, or switching `CARGO_HOME` (`/cache/cargo` to `~/.cargo`) gave 0.
Removing `CARGO_TARGET_DIR` gave 15. `CARGO` (no underscore) had no effect.

## How to Diagnose

- Compare per-job hit rates in the `sccache-s3` Grafana dashboard or the Loki pipeline in
  [sccache S3 bucket full](sccache-s3-bucket-full-silent.md): PR jobs of the same commit family should
  reach >= 90% once main is warm. A bimodal split (>= 90% or < 10%) points to a key mismatch, not
  capacity or latency.
- Diff `env | grep '^CARGO'` (names and values) between the job that wrote and the job that missed.
- Check S3 first: 0 errors and 404s on GET mean the keys differ, not that the backend is broken.

## Fix / Workaround

- **Rule: never name custom CI env vars with a `CARGO_` prefix.** Use a project prefix.
- Calypso fixes (multivara/calypso, unmerged on 2026-11-19, merge together so main re-warms once):
  PR #3061 renames `CARGO_SCOPE` to `CALYPSO_PKG_SCOPE`; PR #3062 sets `[profile.dev]` and
  `[profile.test]` `debug = "line-tables-only"` in `Cargo.toml` and drops CI's `CARGO_PROFILE_*_DEBUG`.
- Verification pending: after the first main push post-merge, affected-mode PRs should show >= 90%
  hits. If they stay cold, the next suspect is feature unification under `-p` scoping: add a
  cargo-hakari workspace-hack or a `--workspace --no-run` build before the scoped tests.
- The desktop cannot realistically share CI keys, see
  [Rust build caching](../conventions/rust-build-caching.md#workstation-and-the-shared-s3-cache-decision-2026-11-19).
  It uses a disk-only sccache.
