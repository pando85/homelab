# Energy Baseline — Progress & Handoff

- **Date:** 2026-10-23
- **HEAD:** f536cbd4
- **Uncommitted work:** 9 files (B2 day-ahead policy implementation + tests)

## Task Status

| Task | Status | Notes |
|------|--------|-------|
| **B1: Train 2024 / Eval full 2025** | **completed** (provisional) | Benchmark ran; results NOT accepted as final — see [Known issues](#known-issues) |
| **B2: Day-ahead policy** | **in_progress** | Implemented, 9 uncommitted files, agent reports 1116 tests; code review pending |
| **B3: Oracle policy** | completed | Certified in previous accepted run |
| **Data extraction (Prometheus)** | completed | validate740: 4198469 rows, all pass |
| **Data extraction (HA)** | not_applicable | HA data NOT consumed by B1/B2 or build pipeline; optional future appliance forecasting — states not exported as numeric values; does not block current work |
| **Dataset build (2-year)** | completed (provisional) | Built dirty; see [Known issues](#known-issues) |
| **ADR-0003 update for B2** | not_done | Review in progress; no edits yet |
| **Script cleanup / commit** | not_done | Pending review acceptance |

## Data Artifacts

### Raw extraction

- **2024 Prometheus manifest:** `eb5c93af-95ec-4b5f-8a0c-8b3e357ec1bd`
  - 2,061,219 rows, 365 parts
  - validate740: 4,198,469 rows, all pass

### Built datasets

| Hash | Status | Description |
|------|--------|-------------|
| `4eb4ee1fd1dcd8ceb76c7ee86f1e4425f5da5ba6f95719e2bffba5a27c4c6f72` | **provisional — NOT final** | 2-year dataset, 70,176 intervals, residual 0. Built dirty (uncommitted code). |
| `f1e6763b8597e687aa2ac3b19bcfeeafb9b568b7a541fa058ad32845b7f9fe51` | **accepted (reference)** | Previous certified dataset: 11-month, 3-policy, benchmark `0a858414fc11834b5f71772b7245f93b` |
| `c14d44908aa0db25065b3dbd19d9324c89d4cf8e247e6d03567f0f359a593944` | **pre-correction — not reference** | Superseded; do not cite |

### Benchmark runs

- **Latest (NOT accepted):** `--train-end 2025-01-01T00Z --train-days 365 --terminal-mode free --time-limit-s 600 --policies all`
  - Results pending objective comparability review
  - See [Known issues](#known-issues) for train-days and year-boundary problems

## Known Issues

### 1. Train-days: 365 vs 366 (leap year)

`--train-days 365` with `--train-end 2025-01-01T00Z` excludes 2024-01-01 (leap year has 366 days).
**Must fix to `--train-days 366`** to cover all of 2024.

### 2. UTC year vs Madrid calendar year

`--train-end 2025-01-01T00Z` is 2025-01-01 00:00 UTC = 2025-01-01 01:00 CET. The actual Madrid
calendar year boundary is 2025-01-01 00:00 CET = 2024-12-31 23:00 UTC. Current choice uses UTC
midnight, which is **not** a full local calendar year. This is an explicit design choice, not a
claim of "full 2024" in local time. Must be documented clearly in any report.

### 3. Day-ahead policy assumptions (B2)

- Assumes archived realized prices equal the midnight-published day-ahead curve. No historical
  snapshot verification has been performed.
- Myopic 28-day load forecast terminal credit: implementation initially unreviewed.

### 4. Dirty build

Provisional dataset `4eb4ee1...` was built with uncommitted code. Must rebuild on clean commit
before accepting as final.

## TODO

### Immediate (before acceptance)

1. **Review B2 code** — 9 uncommitted files, day-ahead policy + tests
2. **Fix train-days** — change 365 → 366 for full 2024 coverage
3. **Clarify year boundary** — document UTC vs Madrid choice explicitly
4. **Review day-ahead assumptions** — archived = published curve; 28-day terminal credit
5. **Final test run** — all tests pass on clean code
6. **Commit clean code** — B2 implementation + any fixes
7. **Rebuild dataset** — on clean commit, with corrected train-days
8. **Run benchmark** — on rebuilt dataset, all policies
9. **Verify report artifacts** — exact commands below

### Commands (placeholders — run after clean commit)

```bash
# Rebuild dataset (adjust hash/path as needed)
cd scripts/energy-baseline
python -m energy_baseline build \
  --manifest <MANIFEST_ID> \
  --output <DATASET_PATH>

# Run benchmark (corrected train-days)
python -m energy_baseline benchmark \
  --dataset <DATASET_PATH> \
  --train-end 2025-01-01T00:00Z \
  --train-days 366 \
  --terminal-mode free \
  --time-limit-s 600 \
  --policies all \
  --output <BENCHMARK_OUTPUT>

# Generate report
python -m energy_baseline report \
  --benchmark <BENCHMARK_OUTPUT> \
  --output <REPORT_PATH>
```

### Deferred

- ADR-0003 update to include B2 as policy 3 (review in progress)
- Appliance-aware load forecasting (ADR-0002) — requires HA state export as numeric values; not blocking
- Historical price snapshot verification for day-ahead assumption

## Constraints

- **No cost/savings/household energy totals in git** — numeric results stay local or in reports
- **No live queries** — all data is archived
- **No commit/stash/restore** during this handoff — review first
- Local data remains; numeric sources are re-extractable from Prometheus manifests

## References

- [ADR-0003 — Phase 1 measurement and baseline](../docs/adr/0003-phase1-measurement-and-baseline.md)
- [AppDaemon baseline context](./energy-baseline-appdaemon-baseline-context.md)
- [Production execution plan](./energy-baseline-production-execution.md)
- [Implementation plan](./home-battery-optimizer-implementation.md)
