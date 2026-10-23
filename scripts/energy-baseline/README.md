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

## Data directory

All output goes to a private data directory **outside the git worktree**.
Default: `~/data/energy-baseline`. Override with `--data-dir` or `ENERGY_BASELINE_DATA_DIR`.

## CLI commands

| Command        | Purpose                                        |
|----------------|------------------------------------------------|
| `extract`      | Ingest from Prometheus / HA history to Parquet |
| `validate`     | Validate raw Parquet against schemas           |
| `build`        | Build 15-min analysis dataset                  |
| `benchmark`    | Run policy benchmarks on a dataset             |
| `report`       | Generate monthly reconciliation report         |
| `inspect-sql`  | Inspect DuckDB SQL queries                     |
| `mirror`       | Deferred — not implemented in Phase 1          |
