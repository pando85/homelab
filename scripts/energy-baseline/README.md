# energy-baseline

Reproducible energy measurement and baseline toolkit for the home battery optimizer project
([ADR-0003](../../docs/adr/0003-phase1-measurement-and-baseline.md)).

## Quick start

```bash
uv sync --extra dev
uv run energy-baseline --help
uv run pytest
uv run ruff check .
```

## Development and production locations

### Data directory

All output goes to a private data directory **outside the git worktree**.
Default: `~/data/energy-baseline`. Override with `--data-dir` or `ENERGY_BASELINE_DATA_DIR`.

**Development (this repo, local runs):** data lives under `/tmp/opencode/energy-baseline`
(chmod 0700, ephemeral, wiped on reboot). Used for first-extraction milestones and
offline tests. All files written with mode 0600, dirs with 0700.

**Production (planned):** data moves to a cluster PVC (e.g. `openebs-zfspv` on prusik)
with the same directory layout and permission model. Migration is a `rsync` + a
`--data-dir` flag flip; the package code is location-agnostic.

### No recurring scheduler yet

There is no recurring scheduler, cron job, or Kubernetes CronJob wired up for this toolkit yet.
Every command below is run manually (or by a human-driven runbook). A scheduled pipeline
(extract → validate → build → benchmark → report) is deferred to a later phase: see
[ADR-0003](../../docs/adr/0003-phase1-measurement-and-baseline.md) for the scope boundary.

## CLI commands

| Command        | Status       | Purpose                                        |
|----------------|--------------|------------------------------------------------|
| `extract`      | **functional** | Ingest from Prometheus / HA history to Parquet |
| `validate`     | **functional** | Validate raw Parquet against schemas + manifest hashes |
| `build`        | **functional** | Build 15-min analysis dataset                  |
| `benchmark`    | **functional** | Run policy benchmarks on a dataset             |
| `report`       | **functional** | Generate monthly reconciliation report         |
| `inspect-sql`  | **functional** | Inspect DuckDB SQL queries                     |
| `mirror`       | deferred     | Deferred — not implemented in Phase 1          |

### `extract`

```bash
# Prometheus (no auth, default 4 entities)
uv run energy-baseline extract \
  --source prometheus \
  --config config/tariff.yaml \
  --lookback-days 9

# HA history (requires ENERGY_BASELINE_HA_TOKEN env var)
uv run energy-baseline extract \
  --source ha-history \
  --config config/tariff.yaml \
  --start 2026-10-14T00:00:00Z \
  --end 2026-10-23T10:59:56Z \
  --entities sensor.lavadora_current_status \
  --entities sensor.lavavajillas_current_status

# Both sources in one run
uv run energy-baseline extract --source all --lookback-days 7
```

Time window: either `--start`/`--end` (ISO-8601 UTC, end defaults to now) or
`--lookback-days N` (start = N days ago at midnight UTC, end = now).

### `validate`

```bash
uv run energy-baseline validate --source ha-history
uv run energy-baseline validate --source prometheus
uv run energy-baseline validate --source all   # default
```

Checks per source:
- Every part file matches `RAW_SAMPLE` / `RAW_ENUM_STATE` schema
- Filename content hash matches actual SHA-256 of file bytes
- Every manifest's recorded part hashes match the actual files on disk
- No zero-row parts, no unreadable parts

Exit code 0 if all checks pass; 1 otherwise.

### `build`

```bash
uv run energy-baseline build \
  --start 2026-10-01T00:00:00+00:00 \
  --end 2026-10-23T00:00:00+00:00 \
  --assume-no-battery  # default; use --no-assume-no-battery to refuse
```

The `--assume-no-battery` flag is a **recorded assumption, not a detection**: missing battery
telemetry cannot prove a battery does not exist. The assumption is pinned in the build manifest.
When `--no-assume-no-battery` is set, the build requires registered battery telemetry to
reconstruct gross load; without it the build refuses.

**Immutable store:** the canonical data directory is write-once, content-addressed. Re-running
`build` for a window that already exists is idempotent when the bytes match. When they do not —
`refusing to overwrite .../build_manifest.json with different content (existing=…, new=…)` —
the build **refuses** with one line on stderr and exit code 1; it does not print a traceback, and
it never overwrites or deletes anything. The dataset id covers source part hashes, config hash,
code version, schema version, window and builder parameters, so an identical id with different
bytes means something outside those inputs changed since the original build — in practice code in
a dirty working tree, whose version string records only a `-dirty` marker, or a provenance field
the id does not cover. Fix it on your side of the boundary: point `--data-dir` at a fresh data
directory, or remove that one dataset directory yourself and re-run.

### `benchmark`

```bash
uv run energy-baseline benchmark \
  --dataset ~/data/energy-baseline/analysis/v1/<dataset_id> \
  --train-end 2026-09-01T00:00:00+00:00 \
  --policies all \
  --terminal-mode free \
  --time-limit-s 120.0
```

`--train-end` is **required with no default**: thresholds are fitted on the earlier split and
frozen before evaluation, so the cut must be declared explicitly and is never inherited. Optional
flags (`--train-days`, `--charge-pct`, `--discharge-pct`, `--initial-soc-frac`, `--import-limit-kw`,
`--min-usable-fraction`, `--time-limit-s`, `--mip-rel-gap`, `--mip-abs-gap`,
`--allow-reconstructed-quality`, `--terminal-mode`) default to the benchmark module's constants.
The CLI calls `core_available()` first and refuses with a clear message if the numeric core
(battery / policies / oracle / cost) is not usable.

**Usability gate:** intervals with `allocation_quality="estimated_boundary"` are benchmarked by
default (it is a bounded, telescoping-preserving approximation, not a degraded fallback).  The
`--allow-reconstructed-quality` flag admits reconstructed *values*: forward-filled prices and
partially gapped energy totals.  `unknown_gap`/`gap_total_unknown` allocation and
`missing`/`ambiguous` prices are never benchmarked, with or without the flag.

**Config drift check:** the benchmark verifies that the tariff config being used matches the
one pinned in the dataset's build manifest.  A mismatch hard-fails with both hashes shown —
the dataset must be rebuilt with the same config, or the same config must be passed.  There is
no bypass: benchmarking a battery against another build's data is exactly the error this check
exists for.

**Import ceiling (`--import-limit-kw`):** the site's contracted power, `import_limit_kw: 5.0` in
`config/tariff.yaml`, **user-confirmed from the contract on 2026-10-23** — never inferred from
sensor data. It constrains the *charging decision* only:
`charge[t] <= max(0, import_limit_kw * h - load[t])`. Because `load[t]` is per-interval energy,
that comparison is an average over the interval — the same 15-minute-average basis on which
contracted power is actually enforced, so the constraint matches the contract rather than
approximating it. Net grid import above the ceiling is therefore an **observation**, not a
violation: it can only happen where the measured load already exceeds the ceiling, which no
dispatch decision caused and none can undo, and treating it as infeasible would invalidate the
historical baseline itself. Schedules report those intervals via
`Schedule.ceiling_exceeding_intervals` / `battery.ceiling_exceedance_indices`. No safety margin
is applied; the configured value is used exactly as given. Instantaneous peaks (2025 archive
maximum 8.68 kW) bear on the battery's power rating, not on contract feasibility — see
`battery.py` invariants 6 and 7 for the measured table.

**Dataset identity:** `dataset_id` is a SHA-256 over the source part hashes, the canonical
config hash, the code version (including the working-tree dirty marker), the schema version,
the requested window and the builder parameters.  A dirty working tree produces a different id
from a clean one, so the reproducibility guarantee the id exists to provide is preserved.

**Policy set:** exactly three policies are benchmarked (no-battery, causal price-threshold,
and perfect-information oracle). A fourth "day-ahead-informed" variant was considered and
deferred — see `planning/energy-baseline-appdaemon-baseline-context.md`. The causal policy is
deliberately **not** a replica of the household's AppDaemon rule; AppDaemon's effect is already
embedded in the measured load, which is why the *no-battery* policy is the counterfactual.

**Oracle certification:** the oracle is **not certified** as optimal. It hits its solver time
limit (default 120 s) and is reported as a feasible incumbent with an upper bound on cost,
never as an optimum. The implied saving is therefore a lower bound on optimal saving.

**Cost basis:** all cost figures are **pre-tax** (untaxed energy cost). Tax rates
(`iee_pct`, `iva_pct`) are null/unconfirmed in `config/tariff.yaml`, so no billed number
exists. Fill taxes from a verified invoice to get a taxed basis.

### `report`

```bash
uv run energy-baseline report --dataset ~/data/energy-baseline/analysis/v1/<dataset_id>
uv run energy-baseline report --benchmark ~/data/energy-baseline/analysis/v1/<dataset_id>/benchmarks/<run_id>
```

`--dataset` must contain exactly one benchmark run; if multiple runs exist, the error lists the
available run ids rather than silently picking the newest. `--benchmark` overrides `--dataset`.

### `inspect-sql`

```bash
uv run energy-baseline inspect-sql --list-queries
uv run energy-baseline inspect-sql --dataset ~/data/energy-baseline/analysis/v1/<dataset_id> \
  --query price_distribution
uv run energy-baseline inspect-sql --dataset ~/data/energy-baseline/analysis/v1/<dataset_id> \
  --sql "SELECT * FROM intervals WHERE price_eur_per_kwh > 0.20" \
  --benchmark ~/data/energy-baseline/analysis/v1/<dataset_id>/benchmarks/<run_id>
```

Exactly one action is required: `--sql`, `--query`, or `--list-queries`. `--sql` and `--query` are
mutually exclusive. Results print as aligned text by default, or JSON with `--json`. The SQL is
validated as a single read-only statement (SELECT / WITH ... SELECT / EXPLAIN SELECT); anything
else is refused at the grammar level.

`benchmark_results` and `benchmark_schedules` exist only when `--benchmark PATH` is given. Curated
queries that read them (`monthly_cost`, `excluded_intervals`, `benchmark_summary`) and any ad-hoc
SQL naming them are therefore refused up front, before DuckDB is opened, with one line naming the
missing view and telling you to pass `--benchmark` — instead of leaking a raw
`duckdb.CatalogException` traceback. Dataset-only queries (`price_distribution`,
`quality_summary`, `dataset_meta`) work without it.

### `mirror`

Deferred — not implemented in Phase 1 (ADR-0003 §optional). The optional TimescaleDB / SQL mirror
is out of scope for this phase.

## HA authentication

HA history extraction requires a bearer token. The token is **never written to
disk** by the package — it is passed via the `ENERGY_BASELINE_HA_TOKEN`
environment variable or the `token=` keyword argument to `extract_ha_history()`.
The HA base URL is also **never recorded**: the manifest keeps only
`endpoint: "home-assistant-history-api"` plus `endpoint_instance_sha256_16`,
a one-way digest that allows cross-run identity correlation (telling two
instances apart) without recording the endpoint itself.

httpx INFO-level request logging (which would leak the full URL including
entity filters) is suppressed at the start of `extract_ha_history()` via
`_suppress_httpx_info_logging()`, and `_sanitize_error()` strips the base URL
and host from transport-error text before it reaches stderr or logs.

For a first-run login flow (username + password from `pass`), see
[`docs/troubleshooting/home-assistant-api-hacs-deploy.md`](../../docs/troubleshooting/home-assistant-api-hacs-deploy.md).
The login flow is ephemeral: the token lives in memory for the duration of the
extract call and is never persisted.

## Known issues

### HA retention: declared 10 days, effective boundary unverified

`apps/home-assistant/config/configuration.yaml` sets only `recorder.db_url` —
there is no `purge_keep_days`, no `commit_interval`, and no `exclude`/`include`
override, so Home Assistant's documented default of 10 days applies. The
*effective* coverage boundary is unverified and may be shorter; this package
does not probe retention or write synthetic rows to test availability (doing
either would change live Home Assistant configuration, which ADR-0003 forbids
in this phase).

A 2026-10-23 run with `--lookback-days 9` declared 10 chunks, wrote 9 parts
(39 rows), and observed one empty chunk (2026-10-14 → 2026-10-15). With 10-day
retention reaching back to roughly 2026-10-13, that empty chunk is **not**
explained by retention — with only two appliances producing 39 rows in nine
days, a day with no state transition is the likelier reading. An earliest
*observed row* is not a retention boundary; the entities may simply not have
changed state.

The operational conclusion stands regardless of the exact number: enum history
must be extracted recurringly well inside the declared retention because **HA
recorder history is not re-extractable**, unlike the Prometheus archive. The
recurring production schedule is **still not implemented**.

### Per-chunk emptiness is now captured in `quality_flags`

`extract_ha.py` materialises every declared chunk into a `ChunkOutcome`
*before* fetching, so a zero-row chunk produces flags rather than vanishing
from the manifest. A declared/written mismatch raises
`chunk_accounting_mismatch` with the empty windows named; per-chunk ledger
entries live in `params.chunk_outcomes[]` and summary counts in
`params.chunk_accounting`. Two flag prefixes distinguish the cause:
`empty_chunk:` (payload carried nothing — causes indistinguishable) and
`empty_chunk_after_window_filter:` (payload carried rows, none inside the
half-open window). `coverage_notes` spells out the disagreement in words.

### `source_window_start` / `carry_in` flags

Both fields are preserved in the raw Parquet for every HA row. `carry_in=True`
marks rows whose `last_updated` was at or before the chunk start (not a real
transition). Consumers must filter on `carry_in=False` before treating
`last_changed` as a real event time.

## Secrets policy

- No secrets are written to files by this package
- `ENERGY_BASELINE_HA_TOKEN` is read from env or passed explicitly; never logged
- HA extraction writes no endpoint identity: the manifest records
  `endpoint: "home-assistant-history-api"` plus a 16-char SHA-256 digest of the
  base URL (`endpoint_instance_sha256_16`) for cross-run correlation only.
  Prometheus extraction still records `params.base_url` (an unauthenticated
  internal archive URL) — the asymmetry is a known gap
- `_redact_error()` strips bearer tokens, credential-bearing parameters
  (`password`, `passwd`, `pwd`, `token`, `access_token`, `refresh_token`,
  `id_token`, `secret`, `api_key`, `apikey`, `authorization`, `client_secret`
  in both `key=value` and `key: value` form), and common API-key prefixes
  (`sk-`, `pat-`, `ghp_`, `gho_`, `xoxb/xoxa/xoxp/xoxr/xoxs-`) from any
  error message before it reaches stderr or logs
- HA extraction additionally applies `_sanitize_error()` to strip the base URL
  and its host from transport-error text, and suppresses httpx INFO-level
  request logging (which would otherwise leak the full URL with entity filters)
- The CLI catches `ValueError`, `RuntimeError` (and their domain subclasses:
  `BenchmarkInputError`, `ReportInputError`, `SQLInputError`, `BenchmarkError`,
  `ReportError`), `NotImplementedError` (for deferred commands like `mirror`),
  and `OSError` (including `FileExistsError` from the immutable store's
  no-clobber writes, `PermissionError`, `IsADirectoryError`, `FileNotFoundError`)
  and prints one redacted line to stderr with exit code 1; no raw tracebacks
  leak. A no-clobber refusal additionally explains that nothing was overwritten
  or deleted and what to do instead — the tool never cleans up canonical data
  on its own
- Curated `inspect-sql` queries that need a benchmark-only view are checked up
  front, so a missing `--benchmark` is reported as a usage error rather than
  surfacing a DuckDB `CatalogException`
- Unexpected exceptions (e.g. `KeyError`, `TypeError`, or any other exception
  type) are allowed to propagate so bugs are visible; there is no blanket
  `except Exception`
