# Energy Baseline — Progress & Handoff

- **Date:** 2026-10-23
- **HEAD:** c1569314 — B2 (day-ahead policy) and the comparability fix are committed, with no
  uncommitted source files in that commit
- **Uncommitted at the time of writing (documentation pass):** `docs/adr/0003-…`,
  `planning/energy-baseline-progress.md`, `scripts/energy-baseline/README.md`, plus one additive change
  in `report.py` and two tests in `tests/test_report.py` — see *Documentation-pass changes to
  `report.py`* under *Commands*. Nothing else. `git commit` was off-limits for this pass, so these
  edits are left for review
- **Test suite:** 1120 tests pass at `c1569314` (the 1116 reported before the comparability fix is
  superseded); 1122 with the two horizon-scope tests

## Task Status

| Task | Status | Notes |
|------|--------|-------|
| **B1: Train 2024 / Eval full 2025** | **conditionally accepted** | Independent read-only acceptance review on 2026-10-23 found **no mathematical blocker**. The verdict, its eight conditions, the verified structural findings and the three known gaps are in [Acceptance (conditional accept)](#acceptance-conditional-accept) — read the result *with* those conditions, not as an unqualified accept |
| **B2: Day-ahead policy** | **committed** | Implemented at `c1569314` (was reported as 9 uncommitted files / 1116 tests — both superseded: the work is committed and the suite is 1120 tests after the comparability fix). Authoritative run complete and conditionally accepted, results not in git, see [Benchmark runs](#benchmark-runs) |
| **B3: Oracle policy** | completed | Certified in the earlier 11-month reference run **and** in the authoritative run (gap inside the 1e-06 tolerance at a 900 s limit) — see [Acceptance](#acceptance-conditional-accept) |
| **Data extraction (Prometheus)** | completed | validate740: 4198469 rows, all pass |
| **Data extraction (HA)** | not_applicable | HA data NOT consumed by B1/B2 or build pipeline; optional future appliance forecasting — states not exported as numeric values; does not block current work |
| **Dataset build (2-year)** | completed | Clean-tree `da2ac07a7a39…` supersedes the dirty `4eb4ee1…`; see [Built datasets](#built-datasets) |
| **ADR-0003 update for B2** | **done** | Amended 2026-10-23: policy set is now a four-rung information ladder with `day_ahead` at rung 3, plus a *Comparability rule — one objective, different information* section and an open *day-ahead publication versus realised archive* validation item; scope table, Consequences and the ESIOS deferred row updated; status **Proposed → Accepted** |
| **Script cleanup / commit** | done | B2 + fixes committed at `c1569314` (1120 tests) |

## Data Artifacts

### Raw extraction

- **2024 Prometheus manifest:** `eb5c93af-95ec-4b5f-8a0c-8b3e357ec1bd`
  - 2,061,219 rows, 365 parts
  - validate740: 4,198,469 rows, all pass

### Built datasets

| Hash | Status | Description |
|------|--------|-------------|
| `da2ac07a7a39affc43ef9e94523fb6a154ee6213d590086d8c261b265d957140` | **clean tree — the authoritative dataset** | Built from committed code (`c1569314`), no `-dirty` marker. Every claim now being tested runs on this and nothing else — see [Benchmark runs](#benchmark-runs) |
| `4eb4ee1fd1dcd8ceb76c7ee86f1e4425f5da5ba6f95719e2bffba5a27c4c6f72` | **SUPERSEDED — do not cite** | 2-year dataset, 70,176 intervals, residual 0. Built dirty (uncommitted code). Its run `5dc71800ae28…` used pre-fix code in which the day-ahead policy scored terminal SOC at the training-window mean while the oracle scored it at zero: the two costs sat on different objectives, so that run's gap-closed percentage is invalid and must not be quoted at all. Carries a `SUPERSEDED.md` marker on disk |
| `f1e6763b8597e687aa2ac3b19bcfeeafb9b568b7a541fa058ad32845b7f9fe51` | **accepted (reference)** | Previous certified dataset: 11-month, 3-policy, benchmark `0a858414fc11834b5f71772b7245f93b` |
| `c14d44908aa0db25065b3dbd19d9324c89d4cf8e247e6d03567f0f359a593944` | **SUPERSEDED — do not cite** | Pre-correction build: carries the withdrawn `quantisation_kwh: 0.0016` (measured GCD is 0.0001), has no `energy_reconciliation` block, and its two benchmark runs (`6c4f456a`, `e80155f7`) terminated as uncertified 120 s incumbents. Carries a `SUPERSEDED.md` marker on disk |

### Benchmark runs

- **Superseded — do not cite:** `5dc71800ae28…` on dataset `4eb4ee1fd1dc…` ran pre-fix code in which
  the day-ahead policy valued terminal SOC at the training-window mean while the oracle valued it at
  zero. The two costs sat on different objectives, so that run's gap-closed percentage is invalid.
- **Older provisional attempt — NOT accepted:** `--train-days 365 --time-limit-s 600` excluded
  2024-01-01 and was never accepted; see [Known issues](#known-issues).
- **Authoritative run — completed 2026-10-23, conditionally accepted:** four policies on the
  clean-tree dataset `da2ac07a7a39…`, run as `--train-end 2025-01-01T00:00:00Z`,
  `--train-days 366`, `--terminal-mode free`, `--time-limit-s 900`, `--policies all`; run id
  `b0a9b59aa846487a82d08fc4ba718708`, whose `benchmark.json`, `results.parquet`,
  `schedules.parquet`, `report.md` and `report.json` are all present. **No cost, saving, monetary or
  household-energy figure from it appears in this file and none will be added** — those live only in
  the private data directory. What [Acceptance](#acceptance-conditional-accept) does record is
  structural telemetry: interval and day counts, solver gaps against their tolerances, quality
  fractions, and clipped kWh refused by the measured constraints. The fields below are listed so a
  reader knows where to look, not as values:
  - oracle certification: `policies.oracle.solver_status` / `bound_kind` / `is_certified`
  - `comparison.day_ahead_closes_gap_fraction`
  - `comparison.oracle_dominates_day_ahead_guaranteed` — `False` by construction unless the oracle is
    certified **and** `terminal_mode_used` is `free`
  - `comparison.day_ahead_minus_causal_eur` — sign asserted in neither direction

## Acceptance (conditional accept)

The authoritative run `b0a9b59aa846487a82d08fc4ba718708` on dataset `da2ac07a7a39…` is **conditionally
accepted** by an independent read-only acceptance review. There is **no mathematical blocker**: the
accounting is internally consistent, comparisons are asserted only where they are provable, and the
structural claims below were re-derived from the artifacts rather than from this file. The conditions
are part of the accepted result — a restatement that drops one of them is not the accepted claim.

### Conditions attached to the accept

1. **Untaxed only.** The run carries `has_unconfirmed_rate: true`. No billed figure exists anywhere in
   it and none is claimed — see ADR-0003 §*Cost basis — pre-tax, by decision*.
2. **Battery absent by recorded assumption, not by detection.** `--assume-no-battery` is pinned in the
   build manifest; missing battery telemetry cannot prove a battery does not exist.
3. **Provisional physics.** Symmetric one-way efficiency (charge = discharge = √RTE) and zero idle
   loss are provisional and unmeasured; the run's own assumptions block says so rather than this file
   vouching for it.
4. **Day-ahead publication assumption.** Archived price ≡ published-at-midnight is **unverified**, and
   the **direction of its error is not established** — because each day's plan is re-scored against the
   same archived realised prices every other policy is scored on, divergence perturbs the *decisions*,
   not the accounting basis of those decisions. An adverse expectation is reasonable; it is not a
   bound. See *Known issues §3* below and ADR-0003 §*Open validation item: day-ahead publication
   versus realised archive*. The ESIOS day-boundary question (TODO-0.4) stays open with it.
5. **Averaging basis.** No-export and the import ceiling are enforced on **15-minute interval
   averages**, not on instantaneous telemetry. That is the basis on which contracted power is actually
   enforced, so it is correct for the ceiling and **average-only for export**: a sub-interval export
   spike is invisible to this model.
6. **DST slot-index limitation — structural.** The load forecast is position-aligned, so on a 100-slot
   fall-back day the 4 tail slots have no prior day at those positions and take the pooled-window
   fallback. In this run that is exactly one day, 2025-10-26 with `slots_pooled_fallback: 4`, and it is
   empirically immaterial — that day's per-interval forecast error is ordinary, below the median of the
   solved days. It remains a structural property of position-aligned slot forecasting, not a one-off.
7. **Horizon is a UTC-scoped 2025**, not a Madrid-local calendar year — see *Known issues §2*.
8. **Terminal SOC free and zero-valued** on the shared scoring basis, so the day-ahead policy's
   end-state energy is **uncredited**: energy still stored when the horizon closes is something it paid
   for and never gets value back. That is conservative against it, and the oracle is scored the same
   way, which is what keeps the two comparable.

### Verified structural findings

These are the substance of the accept. None is monetary.

- **Training window recomputed independently from the raw Parquet:** 35,136 intervals = 366 × 96, i.e.
  the full UTC 2024 leap year; 34,108 usable.
- **Evaluation:** 35,040 intervals = 365 × 96; 33,953 usable; 1,087 excluded — 296 gap, 711
  price-null, 48 gap-partial, 32 forward-filled. `allow_reconstructed_quality=false`, so no
  reconstructed price enters any cost.
- **Oracle certified** on an actual relative gap of 6.348e-07 against the 1e-06 tolerance, at a 900 s
  limit — a proven optimum, not a time-limited incumbent.
- **One objective, checked rather than asserted:** in all four policies `cost_eur == meter_cost_eur`,
  the inventory adjustment is 0, and the scoring terminal value is the shared 0; costs re-summed from
  `schedules.parquet` agree to better than 3e-13; `min grid = 0.000`, so no plan exports; SOC stayed
  within [1.024, 4.864] kWh — the configured window of the 5.12 kWh nominal; and no interval carries
  simultaneous charge and discharge.
- **Day-ahead solve telemetry:** 366 local days, 360 solved, 6 skipped (2025-04-01…06, every interval
  of those days gap-excluded), 0 infeasible, 0 no-incumbent, 0 at the per-day time limit, 0 degraded to
  hold-SOC; **359 certified and 1 correctly demoted** — 2025-08-19 at gap 1.044e-06, reported
  `feasible` / `upper` / uncertified instead of optimal. That demotion is the certification gate
  working, not a defect.
- **Clipping magnitude — record it, it is the honest size of forecast error's effect on execution:**
  measured load forced clipping on **358 of the 360 solved days**, removing **15.8 % of planned charge
  (344.66 kWh)** and **24.3 % of planned discharge (521.21 kWh)**. The forecast never enters cost; it
  changed what the plan wanted, and the kernel refused it. This belongs next to the result, not in a
  footnote.
- **Input integrity:** `config_hash`, `build_manifest_sha256` and the part hashes all verify, and
  **740/740** raw-lineage parts are located and hash-verified.
- **Full suite: 1120 passed** at `c1569314`; 1122 with the two horizon-scope tests added by the
  documentation pass recorded under *Commands* below.

### Known gaps (non-blocking, recorded so nothing is silently dropped)

1. **Output files carry no self-manifest.** `run_id` hashes *inputs* only, and `benchmark.json`,
   `report.json`, `report.md`, `results.parquet` and `schedules.parquet` have no recorded sha256 of
   their own. Input lineage is fully hash-verified; the bytes this run wrote are not, so a truncated or
   altered artifact would surface only by re-deriving it, not by checking it.
2. **The monthly quality gate is inert.** `min_usable_fraction = 0.0`, so ADR-0003's "monthly quality
   thresholds are configured and declared" is satisfied nominally — nothing can fail it. April 2025
   sits at **71.7 % usable** with 6 fully-skipped days and a thinned forecast window: the minimum
   `window_days_with_usable_data` across the run's solved days is **22** against the configured 28,
   with per-slot trailing means resting on as few as 20 samples. Disclosed, not gated. *(An earlier
   draft of the review note said 15 trailing days; the artifact does not support that, and 22 is what
   `day_ahead.per_day[].forecast` records.)*
3. **Presentation inconsistency, cosmetic only.** The `in_evaluation` rows of `quality.per_month` sum
   to 35,044 intervals / 33,957 usable against the evaluation block's 35,040 / 33,953, because a
   month-granular flag straddles the local-month boundary — 4 intervals land in `2026-01`. The report's
   own `monthly` table, bucketed directly on `local_day`, reconciles exactly.

## Known Issues

### 1. Train-days: 365 vs 366 (leap year) — resolved, 366 is authoritative

`benchmark.py` computes the training window as `train_end - train_days` up to `train_end`
(half-open). With `--train-end 2025-01-01T00:00:00Z`, `--train-days 365` therefore covers
[2024-01-02, 2025-01-01) and **excludes 2024-01-01**; 2024 is a leap year, so a full 2024 needs
**366**. The authoritative run passes `--train-days 366`, which covers
[2024-01-01, 2025-01-01) — 366 UTC days.

### 2. UTC year vs Madrid calendar year — state it as UTC-scoped, not "a full local year"

`--train-end 2025-01-01T00:00:00Z` is 2025-01-01 00:00 UTC = 2025-01-01 01:00 CET. The Madrid
calendar year boundary is 2025-01-01 00:00 CET = 2024-12-31 23:00 UTC. So `--train-days 366` yields a
**UTC-scoped** year, not a local one: the split instant sits one hour *after* local midnight on both
edges, the training window opens at 01:00 CET on 2024-01-01, and the evaluation window starts at
01:00 CET on 2025-01-01 — so neither boundary is a Madrid midnight and the local day on each edge is
partial. The day-ahead partition respects local calendar days either way (it solves per
`Europe/Madrid` day, 92- or 100-interval DST days included), but the **training/evaluation boundary
does not coincide with a local new year**. Any report must describe the scope as UTC-scoped and must
not claim a full Madrid-local calendar year.

### 3. Day-ahead policy assumptions (B2)

- Assumes archived realised prices equal the midnight-published day-ahead curve. No historical
  snapshot verification has been performed, and the assumption stays **unverified**. Its **direction of
  error is not established**: each day's plan is re-scored by the battery kernel against the same
  archived realised prices every other policy is scored on, so a divergent published curve changes
  *which decisions the policy makes*, not the accounting basis of those decisions — the cost of
  whatever it decides is still computed on realised prices. A wrong signal can make the policy choose
  badly, but "badly" is not guaranteed to mean "worse than reported": the pointwise sign can flip and
  the magnitude is unmeasured. The *expected* direction is adverse; that is an expectation, not a
  bound. The run artifact discloses the reliance the same way —
  `day_ahead.information_set.assumptions_disclosed` records the assumption, that it is *not measured
  here*, and the unresolved day-boundary convention; it claims no direction either. Cheap fix is still
  open: snapshot `sensor.esios_pvpc`'s attribute dict at `00:00:40` daily. See ADR-0003 §*Open
  validation item: day-ahead publication versus realised archive*.
- Myopic 28-day load forecast and terminal credit: **reviewed** — the earlier note that the
  implementation was "initially unreviewed" is no longer true. The terminal credit is now *two* values:
  the **planning** value (`terminal_value_eur_per_kwh`, defaulted to the frozen training-window mean)
  lives inside each daily objective, while the **scoring** value (`scoring_terminal_value_eur_per_kwh`)
  is threaded from the oracle's own keyword arguments, so day-ahead and oracle cannot drift onto
  different objectives again. Pinned by `tests/test_benchmark.py::TestDayAheadScoringValue`. The
  forecast is strictly causal and position-aligned; its one structural gap is the DST tail-slot case
  under *Acceptance* condition 6.

### 4. Dirty build — resolved by the clean rebuild

`4eb4ee1...` was built from a dirty tree, so its `code_version` carries only a `-dirty` marker and its
identity is not reproducible. It has been rebuilt clean-tree as `da2ac07a7a39…` (built from
`c1569314`), and that is the dataset the authoritative run uses. Both the dirty build and the earlier
pre-correction build (`c14d44908aa0…`) now carry a `SUPERSEDED.md` marker on disk and are cited by
nothing.

## TODO

### Immediate (before acceptance) — all nine closed on 2026-10-23

1. **Review B2 code** — done: the review caught the incomparable terminal-value defect (day-ahead
   scored at the training-window mean while the oracle scored at zero) and the fix is in `c1569314`
2. **Fix train-days** — done: the authoritative run passes `--train-days 366`
3. **Clarify year boundary** — done: see *Known issues §2* above — UTC-scoped, not a local year
4. **Review day-ahead assumptions** — **reviewed**: the terminal credit is now a separate planning
   value and the scoring value is threaded from the oracle, and the acceptance review walked the
   assumptions. What stays open is the assumption itself — published ≡ realised is unverified, its
   direction of error is not established, and ADR-0003 keeps it as an open validation item
5. **Final test run** — 1120 tests pass on the committed sources; 1122 with the two horizon-scope
   tests added by the documentation pass below
6. **Commit clean code** — done at `c1569314`
7. **Rebuild dataset** — done: `da2ac07a7a39…`
8. **Run benchmark** — done: run `b0a9b59aa846487a82d08fc4ba718708` on `da2ac07a7a39…` (four
   policies, 366 train days, 900 s). Its numbers stay in the private artifact
9. **Verify report artifacts** — **done**: `report` ran over that run and wrote `report.md` and
   `report.json` into `…/benchmarks/b0a9b59aa846487a82d08fc4ba718708/`. Nothing monetary from them is
   transcribed into git either way

### Commands — the authoritative run as it was executed

```bash
cd scripts/energy-baseline
export ENERGY_BASELINE_DATA_DIR="$HOME/data/energy-baseline"
DS="$ENERGY_BASELINE_DATA_DIR/analysis/v1/da2ac07a7a39affc43ef9e94523fb6a154ee6213d590086d8c261b265d957140"

uv run --locked energy-baseline benchmark \
  --config "$PWD/config/tariff.yaml" \
  --dataset "$DS" \
  --train-end 2025-01-01T00:00:00Z \
  --train-days 366 \
  --terminal-mode free \
  --time-limit-s 900 \
  --policies all

uv run --locked energy-baseline report \
  --config "$PWD/config/tariff.yaml" \
  --dataset "$DS" \
  --benchmark "$DS/benchmarks/b0a9b59aa846487a82d08fc4ba718708"
```

Neither command takes `--output`, and `build` takes `--start`/`--end`, not `--manifest`: artifacts are
written under the data directory at content-addressed paths and only those paths are printed. The
dataset and run directories are read back from there. `report` resolves its run from `--benchmark`
when that is given; `--config` was also passed, so the tariff hash pinned in the run is re-checked
against the config the report is written from. (`report --dataset "$DS"` alone would have worked too —
this dataset holds exactly one run — but that is not what was executed, and it skips the config check.)

**Do not re-run `report` over `b0a9b59a…`.** Its `report.md` and `report.json` were written before the
`report.py` change that adds an explicit horizon-scope line (below), and run artifacts are no-clobber:
`write_part_atomic` refuses different bytes at the same path, and the CLI reports that as one line with
exit code 1. Even if it were forced into a fresh location, the new bytes would hash differently and
what is on disk would no longer match what the review read. **The existing artifacts are immutable and
are the ones the acceptance verdict was read against.** The code change affects *future* runs only.

### Documentation-pass changes to `report.py` (uncommitted at the time of writing)

One additive change; nothing else in the module was touched:

- `report.py` now emits an explicit **horizon-scope line** in `report.md` (under *Evaluation window*)
  and as `horizon_scope_note` in `report.json`, stating that the horizon is a **UTC-scoped window and
  not a `Europe/Madrid` calendar year**. The package README records the rule — "if a 'full calendar
  year' is being claimed, the claim is UTC-scoped and the report must say so" — but the report
  satisfied it only implicitly, through `Z`-suffixed labels. The line is derived from the run's own
  fields (its two bound labels plus the build manifest's pinned `params.timezone`), so it names no
  year of its own, and it states "a calendar year too" when the bounds genuinely do fall on local
  midnights rather than disclaiming unconditionally. Two tests cover it:
  `TestGenerateReport::test_report_states_the_utc_scoped_horizon` and
  `TestGenerateReport::test_horizon_scope_note_is_derived_from_the_run` in `tests/test_report.py`.
- For run `b0a9b59a…` the line would read that `2025-01-01T00:00:00Z` is `2025-01-01 01:00` and
  `2026-01-01T00:00:00Z` is `2026-01-01 01:00` local — the same fact *Known issues §2* records by hand.
- The change adds no monetary value to any artifact and stays inside the reporting layer: `report.py`
  is not an input to `build` or `benchmark`, so no dataset id or run id changes. A `build` run *from
  this working tree* would, as always, carry a dirty `code_version` marker.

### Deferred

- ADR-0003 update to include B2 as policy 3 — **done 2026-10-23**: the ADR now describes four rungs
  with `day_ahead` at rung 3, records the one-objective comparability rule and the open
  publication-versus-archive validation item, and is status **Accepted**
- Appliance-aware load forecasting (ADR-0002) — the HA Prometheus exporter emits state-value series
  only for **numerically convertible** states, so appliance enum strings are genuinely absent from the
  durable archive: what survives is `hass_entity_available`, `hass_last_updated_time_seconds`,
  `hass_state_change_created` and `hass_state_change_total` — *that* a transition happened and *when*,
  never *to what*. The recorder `extract` is therefore the only source of appliance phase, and it is
  not re-extractable. Still deferred, and consumed by nothing today; see ADR-0003 §*Appliance enum
  telemetry — resolved*
- Historical price snapshot verification for day-ahead assumption

## Constraints

- **No cost/savings/household energy totals in git** — numeric results stay local or in reports. The
  line this draws is currency and household throughput, not structure: interval and day counts, quality
  fractions, solver gaps against their tolerances, SOC bounds in kWh and **clipped kWh** are recorded in
  [Acceptance](#acceptance-conditional-accept), because they describe what the run *did* rather than
  what the household *pays*. A euro amount, a savings figure, a percentage of savings, a payback period
  or a household energy total belongs in no committed file, and none is added here.
- **No live queries** — all data is archived
- **No commit/stash/restore** during this handoff — review first
- Local data remains; numeric sources are re-extractable from Prometheus manifests

## References

- [ADR-0003 — Phase 1 measurement and baseline](../docs/adr/0003-phase1-measurement-and-baseline.md)
- [AppDaemon baseline context](./energy-baseline-appdaemon-baseline-context.md)
- [Production execution plan](./energy-baseline-production-execution.md)
- [Implementation plan](./home-battery-optimizer-implementation.md)
