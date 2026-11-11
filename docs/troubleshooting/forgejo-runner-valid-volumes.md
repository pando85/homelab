# Forgejo runner `valid_volumes` silently drops container-job mounts

## Problem

Container jobs (DinD) on the Forgejo runner need bind-mounts from the host into the job container
for sccache to work: `/cache` (sccache binary + slot sockets) and `/sccache-credentials` (S3
credentials, read-only). Adding them to `container.options` is necessary but **not sufficient**:
the runner validates the sources against `container.valid_volumes` too. With the default
`valid_volumes: []`, the mounts are silently dropped — the job container starts without them,
sccache-setup fails to find the binary or credentials, and the job silently falls back to disk
mode (or fails). The runner log shows the tell-tale:

```
[/cache] is not a valid volume, will be ignored
[/sccache-credentials] is not a valid volume, will be ignored
```

## Root Cause

The Forgejo runner applies two independent allow-lists to container-job volumes:

1. `container.options` — the `--volume` flags passed to `docker create`.
2. `container.valid_volumes` — a runner-side allow-list that filters `options` sources before
   they reach docker.

Both must permit the mount. An empty `valid_volumes` (the chart default) rejects everything.
The wildcard `**` is documented but too broad for this cluster — it would let any job mount any
host path, defeating the purpose of the allow-list.

## How to Diagnose

1. Runner pod logs for the job: look for `is not a valid volume, will be ignored` lines.
2. Inside the job container: `ls /cache` and `ls /sccache-credentials` — both empty or missing.
3. The sccache stats line (Loki `{namespace="ci-runners"} |= "SCCACHE_STATS"`) shows
   `compile_requests: null` or `mode: "disk"` with no S3 activity, even though the runner's
   `container.options` string is correct.
4. `kubectl --context=grigri -n ci-runners get configmap <runner-config> -o yaml` — check
   `valid_volumes`.

## Fix / Workaround

Allow-list exactly the two sources the runner needs to pass through:

```yaml
container:
  options: "--volume /cache:/cache --volume /sccache-credentials:/sccache-credentials:ro"
  valid_volumes:
    - /cache
    - /sccache-credentials
```

Do NOT use `valid_volumes: ['**']` — it lets any job mount any host path.

After updating the ConfigMap, restart the runner pods so the new config is loaded. Verify by
dispatching a container job and checking that both mounts are present inside the container.

Applied in homelab commit `ca27baae0` (2026-11-11, both runners restarted and verified live).

## Secondary findings

Three related issues surfaced during the same investigation:

- **`cargo-zigbuild` image lacks `jq`.** `sccache-pin.sh` needs `jq` on every code path (not
  just the S3 branch). The image ships without it; the pin step silently fails and sccache
  falls back to whatever binary is on `$PATH` (often none). Install `jq` in the container
  workflow before sccache-setup.

- **`cargo-zigbuild` image lacks `curl`.** `curl` is the S3 health probe in sccache-setup;
  missing it silently falls back to disk mode. Install `curl` alongside `jq`.

- **Dev-profile `cargo new` builds are never sccache-cacheable.** The throwaway project in
  `sccache-container-test.yml` uses dev profile, which enables incremental compilation by
  default. sccache never caches incremental builds — every compile lands in
  `requests_not_cacheable`, so `compile_requests` (the cacheable count) stays 0 and any
  assertion on it fails even though delivery worked. Fix: set `CARGO_INCREMENTAL=0` on the
  test build step. Calypso commit `781931ed8` (PR pending).

## References

- Homelab runner config: `platform/ci-runners/resources/configmap.yaml` and
  `configmap-amd64.yaml` (`container.options` and `container.valid_volumes`).
- Calypso runbook: `docs/runbooks/sccache-observability.md` in the Calypso repo (Loki delivery,
  schema, verification workflow).
- Planning: `planning/sccache-s3-observability.md` in this repo.
