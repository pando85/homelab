# Energy Baseline — Progress & Handoff

- **Updated:** 2026-10-24
- **Accepted baseline code:** `c1569314`; acceptance documentation and the two horizon-scope tests
  were subsequently committed and pushed in `a4837f6d`. The documentation pass described below is
  historical, not outstanding work.
- **Baseline test suite:** 1122 passed after the reporting change (1120 at `c1569314`). The package
  has grown since: the suite is **1444 passing** today, `ruff check` clean and `ruff format --check`
  clean on 39 files. Every historical count in this file stays as written — a stale count pasted from
  memory is worse than no count, so quote the one that matches the revision you are describing.
- **Current extension:** the rolling-horizon policy has been **promoted from an offline experiment to
  the fifth canonical rung**, with a configurable replan cadence implementing ADR-0001 §3; the shipped
  round-trip efficiency has been **changed from 0.88 to 0.80**; a read-side Home Assistant enum state
  reader and a dispatch intent contract exist. See *2026-10-24 pass* below. None of this deploys or
  actuates anything.
- **Outstanding, blocking every corrected figure:** the dataset rebuild at 0.80 **has not happened**.
  It must follow the commit (see *Constraints* and the sequencing rule in the
  [component roadmap](energy-battery-roadmap.md)), because `code_version()` is the git HEAD of a clean
  tree and feeds `dataset_id`. Until it lands, the new `dataset_id` and every corrected benchmark
  figure are `PENDING REBUILD`, and no 0.88-era number in this file may be quoted as current.

## Task Status

| Task | Status | Notes |
|------|--------|-------|
| **B1: Train 2024 / Eval full 2025** | **superseded (0.88 era)** | The conditionally accepted run `b0a9b59aa846…` is the result the acceptance review read, and the review itself stands as a record. Its battery figures were computed at the round-trip efficiency the config has since replaced (0.88 → 0.80), so they are optimistic and must be re-derived before use — see *Round-trip efficiency applied (2026-10-24)*. Independent read-only acceptance review on 2026-10-23 found **no mathematical blocker**; the verdict and its eight conditions are in [Acceptance (conditional accept)](#acceptance-conditional-accept) |
| **B2: Day-ahead policy** | **committed** | Implemented at `c1569314` (was reported as 9 uncommitted files / 1116 tests — both superseded: the work is committed and the suite is 1120 tests after the comparability fix). Authoritative run complete and conditionally accepted, results not in git, see [Benchmark runs](#benchmark-runs) |
| **Rolling-horizon policy** | **committed, run PENDING** | `rolling_horizon` is now the **fifth canonical policy** (`POLICY_NAMES` = `no_battery, causal_price, day_ahead, rolling_horizon, oracle`; `all` includes it), promoted from the [forecast experiments](energy-baseline-forecast-experiments.md) to ADR-0003's *Strategy comparison* rung 4. Canonical defaults: exact prices (`sigma = 0.0`, `bias_sigma = 0.0`, `seed = 0`) on the legacy two-publication cadence, horizon 96 slots. No five-policy run exists yet |
| **Configurable replan cadence** | **committed, not validated on real data** | `replan_interval_slots` / `horizon_slots` implement ADR-0001 §3's "re-solve every 15 minutes over a rolling 24–48 h horizon, execute only the current interval". `replan_interval_slots=None` is the legacy schedule and is proven **bit-identical** to it by golden digests extracted from `HEAD` with `git show`; cadence 1 with `horizon_slots=96` is roughly **48× the solve count** (about **37× the wall time** — per-solve time is *lower* at a fine cadence, ~20 ms against ~25 ms, so wall time grows more slowly than solve count) |
| **Oracle reference** | completed (0.88 era) | Certified in the earlier 11-month reference run **and** in the authoritative run (gap inside the 1e-06 tolerance at a 900 s limit) — see [Acceptance](#acceptance-conditional-accept). Certification is a property of the time limit, not of the efficiency, so the gate still works; the certified *numbers* are superseded |
| **B3: HA enum state reader** | **done (no longer deferred)** | `src/energy_baseline/ha_states.py` plus the `ha-states` CLI subcommand. Reads the preserved appliance enum history and deduplicates **at read time** on `(entity, last_changed, last_updated, state, attributes_json)`; as-of queries are inclusive at the transition boundary and cannot leak a later transition into an earlier snapshot (ADR-0002's requirement). Verified live, read-only, against the real store: `parts=12 rows_before_dedup=51 rows_after_dedup=39 duplicates=12 entities=2`. Read-side only — it does not feed `build`, does not change `dataset_id`, and no appliance modelling, NILM or forecasting was built on it |
| **Dispatch intent contract (TODO-4.6)** | **done, contract only** | `src/energy_baseline/dispatch.py`: typed, validated, canonically serialized `DispatchIntent` / `DispatchPlan` (interval, mode, target power, SOC target, expected price, confidence, generation instant, expiry, sequence). The clock is a **required caller argument with no default** — there is no fallback path to a wall clock, and a test walks the module's AST to prove no clock call and no `time`/`timeit` import, which holds against future edits rather than only against its author. Physics delegated to `battery.py`; the import ceiling stays an **observation**, never a rejection; `confidence` and `expected_buy_price_eur_per_kwh` are optional with absence distinguishable from a value. **No writer, no I/O, no CLI subcommand** |
| **Battery-assumption guard** | **done** | `tests/test_benchmark.py::TestResolveBatteryAssumptions::test_shipped_tariff_battery_assumptions_are_pinned` pins the shipped battery block; see *Round-trip efficiency applied (2026-10-24)* |
| **Round-trip efficiency 0.80** | **applied to config; rebuild pending** | `config/tariff.yaml` now ships `round_trip_efficiency: 0.80`; `config_hash` `41fdc30b…` → `7a901801…`; dataset superseded — see *Round-trip efficiency applied (2026-10-24)* |
| **Data extraction (Prometheus)** | completed | validate740: 4198469 rows, all pass |
| **Data extraction (HA)** | **extracted and readable** | Extraction was always in place; what B3 added is the read side. HA states are still NOT consumed by the benchmark pipeline or `build`, so they remain optional future appliance forecasting rather than a blocker for current work |
| **Dataset build (2-year)** | **SUPERSEDED — rebuild pending** | Clean-tree `da2ac07a7a39…` was authoritative and is now superseded by the 0.80 config change; the replacement id is `PENDING REBUILD — fill after the clean-tree build`. See [Built datasets](#built-datasets) |
| **ADR-0003 update for B2** | **done** | Amended 2026-10-23: policy set became a four-rung information ladder with `day_ahead` at rung 3, plus a *Comparability rule — one objective, different information* section and an open *day-ahead publication versus realised archive* validation item; scope table, Consequences and the ESIOS deferred row updated; status **Proposed → Accepted** |
| **ADR-0003 update for the fifth rung** | **done 2026-10-24** | Real amendment, not a mention: ladder, ladder table, scope table, *Comparability rule* (extended to the fifth rung on the same shared-scoring basis), *Consequences* and *Definition of done* all now say five; the four-rung wording is kept visible as the prior state. Status line and Definition-of-done semantics unchanged |
| **Script cleanup / commit** | done | B2 + fixes committed at `c1569314` (1120 tests) |

## Data Artifacts

### Raw extraction

- **2024 Prometheus manifest:** `eb5c93af-95ec-4b5f-8a0c-8b3e357ec1bd`
  - 2,061,219 rows, 365 parts
  - validate740: 4,198,469 rows, all pass

### Built datasets

| Hash | Status | Description |
|------|--------|-------------|
| `PENDING REBUILD — fill after the clean-tree build` | **the dataset that must be built next** | Rebuild of the 2-year window from a clean tree at `round_trip_efficiency: 0.80` (`config_hash` `7a901801b4148233e232fb86d534c4b7f626ec32af8e7279ad2f9e77831687d1`). It does not exist yet, so its id is unknown and cannot be guessed: `dataset_id` covers the config hash **and** `code_version()`, which is the git HEAD of a clean tree, so the build must follow the commit that lands this change |
| `da2ac07a7a39affc43ef9e94523fb6a154ee6213d590086d8c261b265d957140` | **SUPERSEDED — clean tree, wrong efficiency** | Was built from committed code (`c1569314`) with no `-dirty` marker and was the dataset every claim being tested ran on. It is superseded because `compute_dataset_id` hashes `config_hash`: the battery block changed (`0.88` → `0.80`), so this build no longer describes the shipped config. Its run `b0a9b59aa846…` is the result the acceptance review read and stays on record, but every battery figure derived from it is optimistic and must be re-derived. Do not cite it as current |
| `4eb4ee1fd1dcd8ceb76c7ee86f1e4425f5da5ba6f95719e2bffba5a27c4c6f72` | **SUPERSEDED — do not cite** | 2-year dataset, 70,176 intervals, residual 0. Built dirty (uncommitted code). Its run `5dc71800ae28…` used pre-fix code in which the day-ahead policy scored terminal SOC at the training-window mean while the oracle scored it at zero: the two costs sat on different objectives, so that run's gap-closed percentage is invalid and must not be quoted at all. Carries a `SUPERSEDED.md` marker on disk |
| `f1e6763b8597e687aa2ac3b19bcfeeafb9b568b7a541fa058ad32845b7f9fe51` | **accepted (reference)** | Previous certified dataset: 11-month, 3-policy, benchmark `0a858414fc11834b5f71772b7245f93b` |
| `c14d44908aa0db25065b3dbd19d9324c89d4cf8e247e6d03567f0f359a593944` | **SUPERSEDED — do not cite** | Pre-correction build: carries the withdrawn `quantisation_kwh: 0.0016` (measured GCD is 0.0001), has no `energy_reconciliation` block, and its two benchmark runs (`6c4f456a`, `e80155f7`) terminated as uncertified 120 s incumbents. Carries a `SUPERSEDED.md` marker on disk |

### Benchmark runs

- **No five-policy run exists yet.** `rolling_horizon` is now selected by `all`, so the next
  authoritative run is a five-rung run on a rebuilt clean-tree dataset at 0.80. Its run id, artifact
  paths and every figure in it are `PENDING REBUILD`; none can be stated now, because the run id is
  derived from the dataset id that the rebuild has not produced.
- **Superseded — do not cite:** `5dc71800ae28…` on dataset `4eb4ee1fd1dc…` ran pre-fix code in which
  the day-ahead policy valued terminal SOC at the training-window mean while the oracle valued it at
  zero. The two costs sat on different objectives, so that run's gap-closed percentage is invalid.
- **Older provisional attempt — NOT accepted:** `--train-days 365 --time-limit-s 600` excluded
  2024-01-01 and was never accepted; see [Known issues](#known-issues).
- **Authoritative run — completed 2026-10-23, conditionally accepted, now SUPERSEDED ON EFFICIENCY:**
  four policies on the clean-tree dataset `da2ac07a7a39…`, run as `--train-end 2025-01-01T00:00:00Z`,
  `--train-days 366`, `--terminal-mode free`, `--time-limit-s 900`, `--policies all`; run id
  `b0a9b59aa846487a82d08fc4ba718708`, whose `benchmark.json`, `results.parquet`,
  `schedules.parquet`, `report.md` and `report.json` are all present. This is the artifact set the
  acceptance review read, and [Acceptance](#acceptance-conditional-accept) is recorded against it. The
  config changed underneath it: its battery figures were computed at `round_trip_efficiency: 0.88`,
  which 0.80 has superseded, so they are optimistic upper-side results and are **not** current. It was
  a four-policy run; the ladder has five rungs. **No cost, saving, monetary or household-energy figure
  from it appears in this file and none will be added** — those live only in the private data
  directory. What [Acceptance](#acceptance-conditional-accept) does record is structural telemetry:
  interval and day counts, solver gaps against their tolerances, quality fractions, and clipped kWh
  refused by the measured constraints. The fields below are listed so a reader knows where to look,
  not as values:
  - oracle certification: `policies.oracle.solver_status` / `bound_kind` / `is_certified`
  - `comparison.day_ahead_closes_gap_fraction`
  - `comparison.oracle_dominates_day_ahead_guaranteed` — `False` by construction unless the oracle is
    certified **and** `terminal_mode_used` is `free`
  - `comparison.day_ahead_minus_causal_eur` — sign asserted in neither direction
  - the fifth rung adds `comparison.rolling_horizon_closes_gap_fraction`,
    `comparison.rolling_horizon_minus_day_ahead_eur`,
    `comparison.oracle_dominates_rolling_horizon_guaranteed` and
    `comparison.rolling_horizon_may_lose_day_ahead` (always `true`: it is an assertion of honesty, not
    an observed loss)

## Acceptance (conditional accept)

The authoritative run `b0a9b59aa846487a82d08fc4ba718708` on dataset `da2ac07a7a39…` is **conditionally
accepted** by an independent read-only acceptance review. There is **no mathematical blocker**: the
accounting is internally consistent, comparisons are asserted only where they are provable, and the
structural claims below were re-derived from the artifacts rather than from this file. The conditions
are part of the accepted result — a restatement that drops one of them is not the accepted claim.

**This verdict is recorded against the run as it was, and the run sits on a config that has since
changed.** `round_trip_efficiency` was 0.88 when it was reviewed and is 0.80 now, so everything about
how much the battery moved, and therefore every figure derived from it, is superseded and awaiting a
clean-tree rebuild (see *Round-trip efficiency applied (2026-10-24)*). What the review established
about the **accounting** does not depend on the efficiency and is not re-opened by the change: one
shared scoring objective, exclusions identical across policies, certification gates that fail loudly,
and dominance asserted only where provable. What the review measured about **behaviour** is a 0.88
behaviour and must be re-measured. Read the two separately rather than reading this section as a
current result.

### Conditions attached to the accept

1. **Untaxed only.** The run carries `has_unconfirmed_rate: true`. No billed figure exists anywhere in
   it and none is claimed — see ADR-0003 §*Cost basis — pre-tax, by decision*.
2. **Battery absent by recorded assumption, not by detection.** `--assume-no-battery` is pinned in the
   build manifest; missing battery telemetry cannot prove a battery does not exist.
3. **Provisional physics.** Symmetric one-way efficiency (charge = discharge = √RTE) and zero idle
   loss are provisional and unmeasured; the run's own assumptions block says so rather than this file
   vouching for it. The run's RTE was **0.88**; the shipped config is now **0.80**, so this condition
   is more true than it was, not less: the mapping is still symmetric, idle loss is still assumed zero
   and still unmeasured, and the adopted 0.80 remains an owner decision rather than a measurement of
   this unit. Since 2026-10-24 the block is pinned by
   `tests/test_benchmark.py::TestResolveBatteryAssumptions::test_shipped_tariff_battery_assumptions_are_pinned`,
   which also pins that the provisional framing itself survives a future value change.
4. **Day-ahead publication assumption — RESOLVED by owner assertion 2026-10-24.** The owner stated
   on 2026-10-24 that prices published between 20:15 and 20:20 on day D−1 for delivery day D are the
   prices realised (`user_assertion`, `asserted_on: "2026-10-24"`, `verified_from_data: false`).
   Published ≡ realised is now an owner-stated fact, though not verified from data. The original
   concern — that divergence perturbs decisions rather than the accounting basis — remains a
   structural property of the scoring, not a defect. The ESIOS day-boundary question (TODO-0.4)
   stays open. See *Known issues §3* below and ADR-0003 §*Open validation item: day-ahead
   publication versus realised archive*.
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
- **One objective, checked rather than asserted:** in each policy that run executed — all four of
  them — `cost_eur == meter_cost_eur`, the inventory adjustment is 0, and the scoring terminal value is
  the shared 0; costs re-summed from `schedules.parquet` agree to better than 3e-13; `min grid = 0.000`,
  so no plan exports; SOC stayed within [1.024, 4.864] kWh — the configured window of the 5.12 kWh
  nominal; and no interval carries simultaneous charge and discharge. The check itself is a loop over
  whichever policies ran, so the fifth rung is **covered by extension, not duplication**: a five-policy
  run scores `rolling_horizon` on the same shared 0.0 terminal basis through the same assertion.
- **Day-ahead solve telemetry:** 366 local days, 360 solved, 6 skipped (2025-04-01…06, every interval
  of those days gap-excluded), 0 infeasible, 0 no-incumbent, 0 at the per-day time limit, 0 degraded to
  hold-SOC; **359 certified and 1 correctly demoted** — 2025-08-19 at gap 1.044e-06, reported
  `feasible` / `upper` / uncertified instead of optimal. That demotion is the certification gate
  working, not a defect.
- **Clipping magnitude — record it, it is the honest size of forecast error's effect on execution:**
  measured load forced clipping on **358 of the 360 solved days**, removing **15.8 % of planned charge
  (344.66 kWh)** and **24.3 % of planned discharge (521.21 kWh)**. The forecast never enters cost; it
  changed what the plan wanted, and the kernel refused it. This belongs next to the result, not in a
  footnote. These are 0.88-era behaviour figures: a hairier battery plans different flows, so the
  re-measured numbers are `PENDING REBUILD`. They also need the cadence caveat that now exists — clipped
  totals count each time an interval was **planned**, so they are not comparable across replan cadences
  and are not curtailment (see *Configurable replan cadence*).
- **Input integrity:** `config_hash`, `build_manifest_sha256` and the part hashes all verify, and
  **740/740** raw-lineage parts are located and hash-verified.
- **Full suite: 1120 passed** at `c1569314`; 1122 with the two horizon-scope tests added by the
  documentation pass recorded under *Commands* below; **1444** today, after the fifth policy, the
  cadence work and the two new modules.

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

- Assumes archived realised prices equal the midnight-published day-ahead curve. **RESOLVED by
  owner assertion 2026-10-24** (`user_assertion`, `asserted_on: "2026-10-24"`,
  `verified_from_data: false`): the owner stated that prices published between 20:15 and 20:20 on
  day D−1 for delivery day D are the prices realised. The assumption is now an owner-stated fact,
  though not verified from data. Its **direction of error is not established** as a data question —
  because each day's plan is re-scored by the battery kernel against the same archived realised
  prices every other policy is scored on, a divergent published curve changes *which decisions the
  policy makes*, not the accounting basis of those decisions — the cost of whatever it decides is
  still computed on realised prices. A wrong signal can make the policy choose badly, but "badly"
  is not guaranteed to mean "worse than reported": the pointwise sign can flip and the magnitude is
  unmeasured. The *expected* direction is adverse; that is an expectation, not a bound. The run
  artifact discloses the reliance the same way —
  `day_ahead.information_set.assumptions_disclosed` records the assumption, that it is *not measured
  here*, and the unresolved day-boundary convention; it claims no direction either. The daily
  attribute snapshot (snapshot `sensor.esios_pvpc`'s attribute dict at `00:00:40` daily) would
  convert assertion into measurement, and is now optional rather than blocking. See ADR-0003 §*Open
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
`c1569314`), and that was the dataset the authoritative run used. **That clean-tree build has since been
superseded on a different axis:** the shipped `config_hash` changed when `round_trip_efficiency` went to
0.80, so `da2ac07a…` no longer matches the config and needs its own clean-tree rebuild (see
*Built datasets* and *Round-trip efficiency applied (2026-10-24)*). The lesson of this section — build
from a clean tree — still applies to that rebuild. Both the dirty build and the earlier pre-correction
build (`c14d44908aa0…`) now carry a `SUPERSEDED.md` marker on disk and are cited by nothing.

## TODO

### Immediate (before acceptance) — all nine closed on 2026-10-23, two of them re-opened on 2026-10-24

The nine items were closed against the config as it then stood. Adopting `round_trip_efficiency: 0.80`
re-opens items 7 and 8: the dataset and the run that depended on 0.88 are superseded, and the rebuild
has not been executed. Items 1–6 and 9 are unaffected — the code, the review, the split arithmetic and
the reporting change do not depend on the efficiency value.

1. **Review B2 code** — done: the review caught the incomparable terminal-value defect (day-ahead
   scored at the training-window mean while the oracle scored at zero) and the fix is in `c1569314`
2. **Fix train-days** — done: the authoritative run passes `--train-days 366`
3. **Clarify year boundary** — done: see *Known issues §2* above — UTC-scoped, not a local year
4. **Review day-ahead assumptions** — **reviewed**: the terminal credit is now a separate planning
   value and the scoring value is threaded from the oracle, and the acceptance review walked the
   assumptions. The publication assumption (published ≡ realised) was **unverified** but is now
   **resolved by owner assertion 2026-10-24** (`user_assertion`, `verified_from_data: false`): the
   owner stated that the D−1 evening publication for day D is the realised price curve. ADR-0003
   keeps the item recorded; the daily attribute snapshot is now optional rather than blocking
5. **Final test run** — 1120 tests pass on the committed sources; 1122 with the two horizon-scope
   tests added by the documentation pass below
6. **Commit clean code** — done at `c1569314`
7. **Rebuild dataset** — done: `da2ac07a7a39…`
8. **Run benchmark** — done: run `b0a9b59aa846487a82d08fc4ba718708` on `da2ac07a7a39…` (four
   policies, 366 train days, 900 s). Its numbers stay in the private artifact
9. **Verify report artifacts** — **done**: `report` ran over that run and wrote `report.md` and
   `report.json` into `…/benchmarks/b0a9b59aa846487a82d08fc4ba718708/`. Nothing monetary from them is
   transcribed into git either way

### Commands — the authoritative run as it was executed (0.88 era, superseded)

**These are the commands as historically run, kept because the review was read against their output.**
They no longer reproduce a citable result: `config/tariff.yaml` now hashes differently, so the same
command against the same dataset path fails the config-drift check by design. The rebuild and the
five-policy run come after the commit that lands this change; their dataset and run ids are
`PENDING REBUILD`, and `--policies all` now selects five rungs rather than four.

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

- **The ADR amendments below were listed here as deferred; both are done and are kept in place rather
  than deleted.** ADR-0003 update to include B2 as policy 3 — **done 2026-10-23**: the ADR described
  four rungs with `day_ahead` at rung 3, recorded the one-objective comparability rule and the open
  publication-versus-archive validation item, and became status **Accepted**. ADR-0003 update for the
  fifth rung — **done 2026-10-24**: the ladder, its table, the scope table, the *Comparability rule*
  (extended to the new rung on the same shared-scoring basis), *Consequences* and *Definition of done*
  now say five, with the four-rung wording left visible as the prior state. Neither amendment changed
  the ADR's Status line or its Definition-of-done semantics.
- **B3 (HA appliance-state reader) is no longer deferred — it was built on 2026-10-24.** What exists is
  `src/energy_baseline/ha_states.py` and the `ha-states` CLI subcommand: read-time dedup on the same
  `(entity, last_changed, last_updated, state, attributes_json)` identity the extractor uses, and
  as-of queries inclusive at the transition boundary so a snapshot structurally cannot see a later
  transition (ADR-0002's requirement). Verified live against the real store, read-only: `parts=12
  rows_before_dedup=51 rows_after_dedup=39 duplicates=12 entities=2`. **Still deferred, and unchanged
  by this:** feeding appliance states into `build`, appliance-conditioned forecasting, NILM, and any
  feature engineering on top of the reader. The reader is read-side only — it does not enter
  `dataset_id` and no policy consumes it — so the deferral that actually matters was about *use*, not
  about *access*, and that stays deferred to ADR-0002 Phase 3.
- Appliance-aware load forecasting (ADR-0002) — the HA Prometheus exporter emits state-value series
  only for **numerically convertible** states, so appliance enum strings are genuinely absent from the
  durable archive: what survives is `hass_entity_available`, `hass_last_updated_time_seconds`,
  `hass_state_change_created` and `hass_state_change_total` — *that* a transition happened and *when*,
  never *to what*. The recorder `extract` is therefore the only source of appliance phase, and it is
  not re-extractable. Now **readable** (see the B3 item above), and still consumed by nothing today;
  see ADR-0003 §*Appliance enum telemetry — resolved*
- **A dispatch intent writer, device I/O, and a CLI subcommand for the contract.** The contract itself
  (TODO-4.6) was built on 2026-10-24 as `src/energy_baseline/dispatch.py`; deliberately nothing writes,
  nothing does I/O, and no subcommand was added, because the contract only has to be *capable* of
  supporting them. That remains the boundary ADR-0001 draws: the planner emits **requested** setpoints,
  one component may write setpoints at a time, and no hardware is authorized.
- **No battery telemetry, no device writer, no safety guard, no live actuation.** Nothing described in
  this file is deployed; the ACT column of the
  [component roadmap](energy-battery-roadmap.md) is unbuilt apart from the contract's data shape.
- Historical price snapshot verification for day-ahead assumption — **resolved by owner assertion
  2026-10-24** (published ≡ realised, `verified_from_data: false`); the daily attribute snapshot is
  now optional rather than blocking
- OMIE-based 14:00 price estimate for the two-publication experiment — the owner stated on
  2026-10-24 that a 14:00 estimate of tomorrow's PVPC is constructible from OMIE day-ahead prices
  plus adjustments to derive PVPC, with stated accuracy around 80%. No archived 14:00 estimate
  exists yet; the synthetic-noise grid in [forecast experiments
  §4](energy-baseline-forecast-experiments.md) remains the only quantitative evidence about how
  estimate accuracy affects dispatch. It is an **experiment**, not the canonical rung: the promoted
  policy's default is exact prices, so building the OMIE estimate is what would let the noise arms be
  replaced by a real forecast, not what is needed for the rung to exist

### Pending from owner assertions (2026-10-24)

All three were **owner assertions** (`user_assertion`, `asserted_on: "2026-10-24"`,
`verified_from_data: false`), not measurements made by this pipeline. Item 1 has since been
**applied**, so it is no longer pending in the sense this section was written for; it is kept here
with its wording corrected, because the plan it describes was abandoned and a later reader will want
to know why.

1. **Round-trip efficiency — was the top pending item, now APPLIED at 0.80.** Community-measured RTE
   for similar batteries is 0.80–0.84. The paragraph this bullet used to carry said that range was
   "contradicting the shipped 0.88 in `config/tariff.yaml` by 4–8 percentage points" and that **"a
   sensitivity run therefore precedes any config change"**. **Both are superseded.** On 2026-10-24 the
   owner adopted **0.80** — the **bottom** of the community range — directly, without waiting for the
   sensitivity run, on the reasoning that at 2.5 kW the unit operates mostly near its power rating,
   where efficiency is typically worst. `config/tariff.yaml` now ships `round_trip_efficiency: 0.80`,
   one value line, with every other battery parameter untouched (`capacity_kwh` 5.12, `power_kw` 2.5,
   `soc_window_min` 0.20, `soc_window_max` 0.95, `initial_soc_frac` 0.20, `import_limit_kw` 5.0);
   one-way efficiency is `sqrt(0.80) = 0.89443` per direction instead of `sqrt(0.88) = 0.93808`, and
   `config_hash` moved from `41fdc30b926c099be2cbef201db4141244d82faaa80b9907c7530354f64bf182` to
   `7a901801b4148233e232fb86d534c4b7f626ec32af8e7279ad2f9e77831687d1`.
   **0.80 is still an assumption, not a measurement of this unit** — the efficiency-versus-power curve
   is unmeasured and idle draw is still assumed zero and unmeasured.
   Consequence for the information ladder: the 0.88-era ladder spanned roughly **141 → 450 → 526
   equivalent full cycles per year** (causal → `day_ahead` → `oracle`), and a lower round-trip
   efficiency does not scale rungs uniformly — it penalises cycling, compresses the ladder, and can
   reorder rungs. So those three EFC values, and every other figure computed at 0.88 including the
   authoritative run and the holdout grid, are **superseded optimistic results**, not current ones.
   Holdout EFC values quoted here for the same reason — `day_ahead` 114.70 / 111.16 / 110.87 and
   rolling 116.73 / 115.77 / 114.32 for windows 28 / 365 / all over three months — are 0.88-era
   observations from a run that predates the promotion of `rolling_horizon` to a canonical rung.
   Re-measured at 0.80 they are `PENDING REBUILD`; the corrected ladder ordering is not known yet.
   The `config_hash` → `dataset_id` coupling this bullet described as a reason to hold the value
   **did** fire: dataset `da2ac07a…` is superseded and needs a clean-tree rebuild.
2. **Published ≡ realised.** Resolved by owner assertion; recorded in *Known issues §3* and
   *Acceptance condition 4* above. The daily attribute snapshot would convert assertion into
   measurement and is now optional.
3. **14:00 OMIE-based price estimate source.** Real source now exists (OMIE day-ahead + PVPC
   adjustments, ~80% accuracy); no archived estimate yet. The synthetic-noise grid already brackets
   this accuracy; see [forecast experiments §12.1](energy-baseline-forecast-experiments.md#121-the-1400-next-day-price-estimate-has-a-real-source).

## 2026-10-24 pass — five rungs, 0.80, cadence, reader, contract

Code changes that are complete and verified, recorded here so the handoff describes the tree as it
now is. None of it is deployed, none of it actuates a battery, and the dataset rebuild that would make
benchmark figures current has **not** run.

### Round-trip efficiency applied (2026-10-24)

`config/tariff.yaml` ships `round_trip_efficiency: 0.80`, adopting the **bottom** of the
community-measured 0.80–0.84 range. One value line changed; `capacity_kwh` 5.12, `power_kw` 2.5,
`soc_window_min` 0.20, `soc_window_max` 0.95, `initial_soc_frac` 0.20 and `import_limit_kw` 5.0 are
untouched. One-way efficiency is therefore `sqrt(0.80) = 0.89443` per direction, replacing
`sqrt(0.88) = 0.93808`. `config_hash` moved from
`41fdc30b926c099be2cbef201db4141244d82faaa80b9907c7530354f64bf182` to
`7a901801b4148233e232fb86d534c4b7f626ec32af8e7279ad2f9e77831687d1` (verified by re-hashing the
previous and current file through the package's own `manifest.config_hash`).

- **It supersedes 0.88 and is still an assumption.** The owner's reasoning for the conservative end is
  recorded in the config comment: at 2.5 kW the unit operates mostly near its power rating, where
  efficiency is typically worst. It is not a measurement of this household's unit; efficiency versus
  output power is a curve nobody has measured, and idle draw is still assumed zero and unmeasured.
- **Dataset `da2ac07a…` is superseded.** `compute_dataset_id` hashes `config_hash`, so the accepted
  dataset no longer describes the shipped config. Corrected dataset id:
  `PENDING REBUILD — fill after the clean-tree build`.
- **Every 0.88 benchmark figure is now an optimistic superseded result**, including the accepted
  four-policy run `b0a9b59aa846…` and the 81-configuration forecast grid. They stay on record as what
  was reviewed, and are not restated as current. Corrected figures for all five rungs:
  `PENDING REBUILD — fill after the clean-tree build`.
- **The rebuild must follow the commit, not precede it.** `code_version()` is the git HEAD of a clean
  tree and also feeds `dataset_id`, so every commit re-mints the identity. Building now would produce an
  id the commit immediately invalidates.
- **New guard, and why it exists.** Nothing previously guarded the shipped battery block:
  `test_shipped_tariff_yaml_stays_unconfirmed` pins only the tax rates. A silent edit to efficiency,
  capacity, the SOC window or the import ceiling would have moved every reported figure while the suite
  stayed green, and — because `config_hash` feeds `compute_dataset_id` — would have re-minted the
  dataset identity in the same breath. **The 0.88 → 0.80 change passed silently, and that is how the gap
  was found.** `tests/test_benchmark.py::TestResolveBatteryAssumptions::test_shipped_tariff_battery_assumptions_are_pinned`
  now pins the block. It is verified **non-vacuous**: it fails at the old value, because `sqrt(0.80)`
  differs from `sqrt(0.88)`. It also pins the *provisional framing* — the symmetric `sqrt` mapping stays
  a labelled assumption and `idle_losses_measured` stays `false` — so a value edit can never quietly
  promote an assumption to a fact.

### Rolling-horizon promoted to the fifth canonical rung

`POLICY_NAMES` is now `('no_battery', 'causal_price', 'day_ahead', 'rolling_horizon', 'oracle')`, and
`--policies all` includes it. The rung sits **between `day_ahead` and `oracle`** deliberately, because
it is strictly more informed than the rung below it: at 20:30 on D−1 it already sees day D's exact
prices, which `day_ahead` does not see until D's own midnight, and its horizon spans to the end of D
rather than stopping at it.

Canonical defaults, all verified: `sigma = 0.0`, `bias_sigma = 0.0`, `seed = 0`, cadence `None` (the
legacy two-publication 14:00/20:30 schedule), horizon `96`.

- **Exact prices by default**, because the ladder is about *information*, not synthetic forecast error:
  every other rung sees exact prices, so a rung that started from a noisy estimate would not be
  comparable to them. The noise grid stays an experiment, reachable through flags.
- **Legacy cadence by default**, because it is the configuration the golden-value tests pin bit-for-bit
  and the one validated on real data. ADR-0001's 15-minute cadence is reachable but costs roughly **48×
  the solve count** (about **37× the wall time** — the ratios differ because a fine-cadence solve is
  cheaper) and has not been validated on real data.

Dominance is asserted only where provable, which is the ADR-0003 comparability rule applied to a new
rung rather than a new rule: `oracle <= rolling_horizon` is claimed only when the oracle is **certified**
and scored in `free` terminal mode, and the artifact records
`comparison.rolling_horizon_may_lose_day_ahead` as **`true`** (as it does for causal and baseline). That
the relation held in **81 of 81** offline configurations is strong empirical evidence **within that configuration**, not a proof — so
it is recorded as an observation, and one produced at 0.88 on a now-superseded dataset. All 81 configurations were a Q4 holdout immediately following the training cut; that scope does not transfer to evaluation windows that extrapolate far beyond the cut, and a measured counterexample now exists: on the 2025 full-year ladder the same rung cost MORE than `day_ahead`. The identified asymmetry sufficient to explain the reversal is the load-forecast basis: this rung's profile is frozen at the training cut while `day_ahead` re-learns before each dispatch day. Re-running the identical Q4-2025 window on the same dataset reproduced the win, which rules out the efficiency correction as the cause. Cost consistency
(`cost_eur == meter_cost_eur`, inventory adjustment 0) was **extended** to the fifth policy by the
existing per-policy loop rather than duplicated for it.

**Open design decision (2026-10-24): whether to align `rolling_horizon`'s load-forecast basis with
`day_ahead`'s causal rolling one.** The original claim was that the two rungs "differ only in price
information and cadence, not in load knowledge"; that claim is now known to be false, and the
identified asymmetry (this rung's profile frozen at the training cut, `day_ahead` re-learning before
each dispatch day) is sufficient to explain the full-year reversal. Aligning the bases would make
the claim true and the comparison like-for-like. The tension is honest: the frozen profile was
**correct and deliberate in the experiment** — it keeps every noise arm on one identical load basis
so arms are comparable to each other, and it cannot leak evaluation data; carrying it into the
canonical ladder unchanged placed it beside a rung that does not freeze, and the two bases are now
incommensurable. Changing the default would invalidate the golden-value tests that pin legacy
behaviour bit-for-bit, so it needs new goldens and an explicit decision, not a drive-by edit. The
decisive test — re-running the full year with the two rungs on an aligned load-forecast basis — has
**not** been run.

New CLI flags, all on the `benchmark` subcommand: `--rolling-horizon-replan-interval-slots`,
`--rolling-horizon-horizon-slots`, `--rolling-horizon-sigma`, `--rolling-horizon-bias-sigma`,
`--rolling-horizon-seed`, `--rolling-horizon-time-limit-s`. All five invalid-input paths were verified
to exit 1 with exactly one clean line and no traceback: cadence < 1; horizon < 1;
`--rolling-horizon-horizon-slots` given without a cadence (refused, because the legacy schedule derives
its horizon from the local-day partition — accepting it would emit an artifact claiming a horizon the run
never used); sigma < 0 or non-finite; bias sigma < 0 or non-finite.

### Configurable replan cadence

`rolling_horizon.py` gained `replan_interval_slots` and `horizon_slots`, implementing ADR-0001 §3.
`replan_interval_slots=None` (default) is the legacy two-publication schedule; `1` with
`horizon_slots=96` is ADR-0001's "re-solve every 15 minutes, with a configurable rolling 24–48 hour
horizon; execute only the current interval". The publication rule was collapsed into a **single**
function so the information set is derived from the decision instant and cannot drift between cadences:
prices for local day D+1 become exact at 20:30 on day D, so a 09:00 solve estimates D+1 and a 21:00
solve does not, at any cadence.

- **Legacy behaviour is bit-identical, not approximately so.** Golden digests were extracted from
  `HEAD` via `git show` and pinned as literals — SHA-256 of the charge/discharge/SOC/grid arrays, exact
  cost, EFC, terminal SOC, event and clipping counts, noise statistics, plus digests of the whole event
  table and the frozen profile — across two fixtures (one with a partial opening local day, one covering
  the 2025-10-26 fall-back weekend) at four noise arms. Non-vacuity is asserted: the control and two
  seeds give three different charge digests. A separate cross-implementation differential against the
  pre-change module ran **108** event-table comparisons and **108** full dispatch runs, all identical —
  recorded honestly as a development-time check: it is **not** a committed test, so it cannot be re-run
  from the repo, unlike the golden digests above.
- **Two honesty properties are reported in the artifact and must not be dropped.**
  `executed_multiplicity_mean` is exactly **1.0** while `planning_multiplicity_mean` is much higher —
  about **94.4** at cadence 1 over a 96-slot horizon, about **2.5** at the legacy cadence — so
  **clipping totals are not comparable across cadences and are not curtailment**: they count each time
  an interval was planned, not extra energy refused. `clipping_totals_comparable_across_cadences` is
  therefore `false`, with an explanatory note. `solve_count` is recorded per run so a run's compute
  cost is visible in its own artifact rather than discovered afterwards.

### Read-side HA enum reader, and the dispatch intent contract

`src/energy_baseline/ha_states.py` (CLI subcommand `ha-states`) closes the long-deferred B3 item. It
reads the preserved Home Assistant appliance enum history and **deduplicates at read time** on
`(entity, last_changed, last_updated, state, attributes_json)` — part files are immutable, so the 12
known pre-dedup duplicate rows were deliberately left in place rather than rewriting history. As-of
queries are inclusive at the transition boundary and structurally cannot leak a later transition into an
earlier snapshot, which is ADR-0002's as-of requirement. Verified live against the real store,
read-only: `parts=12 rows_before_dedup=51 rows_after_dedup=39 duplicates=12 entities=2` for
`sensor.lavadora_current_status` and `sensor.lavavajillas_current_status`. It is a **read-side** module:
it does not feed `build`, does not change `dataset_id`, and no appliance modelling, NILM or forecasting
was built on it.

`src/energy_baseline/dispatch.py` closes TODO-4.6 as a **contract only**: a typed, validated, canonically
serialized `DispatchIntent` / `DispatchPlan` carrying interval, mode (`idle`/`charge`/`discharge`),
target power, SOC target, expected price, confidence, generation instant, expiry and sequence. Its clock
is a **required caller argument with no default**, so there is no fallback path to a wall clock — and a
test walks the module's AST to prove no clock call and no `time`/`timeit` import, which holds against
future edits rather than only against its own author. Physics validation is delegated to `battery.py`
(the tests require the kernel's own wording to surface, so no numeric rule can drift there); the import
ceiling stays an **observation**, never a rejection, and a plan-level ceiling check is deliberately
absent because headroom needs a load the plan does not carry. `confidence` and
`expected_buy_price_eur_per_kwh` are optional and their **absence is distinguishable from a value**,
because nothing in the existing policies produces either. **No writer, no I/O and no CLI subcommand were
built** — the contract only has to be capable of supporting them.

### Two test gaps closed, and one wrong assumption corrected

- The cadence flags had no committed subprocess coverage, only a by-hand check. Six tests were added to
  `tests/test_compare_forecasts_cli.py`, covering the summary and schedule records, the multiplicity
  pair, that cost still equals the dot product of true prices and actual grid with noise in play, and all
  three validation refusals.
- Spring-forward had no end-to-end coverage at all — only fall-back, and only clock positions. A
  `_dst_spring_forward_window()` helper and two end-to-end tests were added over the 92-interval
  2026-03-29 day.
- **The instructive part, recorded because the failure was more informative than the passing test would
  have been.** The first spring-forward assertion expected a plan **longer** than 92 intervals and
  **failed** — because with the evaluation window ending at the close of the short day, every horizon is
  legitimately clipped there. **The code was right and the test was wrong**, and the failure hid the
  property actually worth testing. Widening the window to include the following day made it so: the test
  now proves an evening solve on a 23-hour day still plans **across** midnight into a 24-hour one —
  exactly what a fixed 96-slot horizon would get wrong.
- **Suite:** **1444 passing**; `ruff check` clean; `ruff format --check` clean on 39 files.

## Forecast experiments

### 2026-10-24: completed rolling-history sensitivity

The existing `day_ahead` policy was run on the same UTC 2025 evaluation population using trailing
load-history windows of 7, 28, 56, 182 and 365 local days. All use dataset `da2ac07a7a39…`, the same
366-day training split, battery assumptions, shared zero terminal scoring value and strict quality
mask. Only the forecast-window length changes; these are rolling forecasts, not frozen holdouts.

| Window (local days) | Private benchmark run id |
|---|---|
| 7 | `2403a37cd34b9360f4a53c511cf7e6e9` |
| 28 | `a572dbc7413d42082f0a71263d8c66cf` |
| 56 | `ad517b8e9884fa29289582bf9da0d451` |
| 182 | `3f6a0775d9a26a317dc6f688841415ed` |
| 365 | `531a8ab9b27bfaeedbea7eb5f31ed911` |

The longer history did not improve dispatch cost in this sweep. The shortest window had the lowest
forecast mean absolute error, but the 28-day window had the lowest dispatch cost: forecast accuracy
and dispatch value are different metrics. This is a retrospective sensitivity, not an independently
selected winner or evidence of statistical significance. Monetary results remain private.

### Completed 2026-10-24: frozen three-month holdout and two-publication policy

Protocol and limitations: [forecast experiments](energy-baseline-forecast-experiments.md).

- **Holdout:** `[2025-10-01T00:00:00Z, 2026-01-01T00:00:00Z)`. Fit only on data strictly before
  the holdout, comparing the previous 28 days, previous 365 days and all available preceding data.
  This avoids training on dates after a test observation. The UTC split contains 61,344 training
  intervals and 8,832 test intervals; the coverage audit finds 8,820 test intervals usable (12
  excluded), subject to the runner's canonical quality-mask check. Available extracted history begins
  in January 2024; "all" is not a claim that several complete training years exist.
- **Frozen versus rolling:** freeze each consumption profile before the holdout. Do not call this
  identical to the rolling full-year sweep above: the experiment changes both history length and
  whether recent test-period observations can update the forecast.
- **New planning schedule:** at local 14:00, use today's exact prices and tomorrow's estimated
  prices, planning through the end of tomorrow; at 20:30, the first quarter-hour boundary safely
  after the user-stated 20:15–20:20 publication window, revise using exact prices. Execute only up
  to the next event and carry the actually reached SOC forward. At 14:00 this is approximately
  34 hours, conventionally called the "36-hour" variant, with DST-dependent duration.
- **Correction to the initial brief:** a 14:00 forecast is not a no-op merely because exact prices
  arrive before tomorrow starts. Actions executed before publication change tomorrow's opening
  SOC. Even zero price-estimation error need not reproduce the single-day policy, because the
  horizons and earlier actions differ.
- **Price uncertainty:** without archived 14:00 forecasts, test seeded synthetic relative-error
  scenarios and report measured errors. "80–90% precision" has no unique mathematical definition;
  do not present a chosen noise parameter as empirical forecasting skill. Exact evening publication
  is a user-specified scenario assumption, not a new measurement of the archive. **Update
  2026-10-24:** the owner stated that a 14:00 estimate of tomorrow's PVPC is constructible from
  OMIE day-ahead prices plus adjustments to derive PVPC, with stated accuracy around 80%. A real
  source now exists; no archived estimate yet. The synthetic-noise grid already brackets this
  accuracy ([forecast experiments §12.1](energy-baseline-forecast-experiments.md#121-the-1400-next-day-price-estimate-has-a-real-source)).
- **Scope at the time:** isolated offline experiment; the canonical benchmark registry stayed at four
  policies until a separate adoption decision. **That adoption decision has since been taken: the
  policy is now the fifth canonical rung** (see *Rolling-horizon promoted to the fifth canonical rung*).
  What the promotion took was the *mechanism*, at exact prices on the legacy two-publication cadence —
  not this experiment's numbers, which were produced before the promotion and at the 0.88 efficiency
  that has since been superseded. No new telemetry extraction, controller deployment or actuation.
- **Executed 2026-10-24, then re-run the same day:** a slot-positioning defect in the frozen load
  profile (positions derived from offsets within the sliced array instead of the local wall clock)
  was found during verification and fixed, and the whole matrix was re-run. **Cite only**
  `/tmp/opencode/energy-baseline/holdout-final-20261024T035727/`; the earlier
  `holdout-bias-20261024T023833/` and `holdout-full-*` / `holdout-sigma0-*` directories predate the
  fix and are superseded (they sit outside git and outside the canonical store, so they are not
  immutable artifacts — re-derive rather than cite). Corrected run: 81 configurations (3 windows ×
  3 per-slot σ × 3 per-day bias σ × 3 seeds); each rolling run solved 185 events (92 days × 2 + 1
  warmup), each `day_ahead` run 93 days; zero infeasible / no-incumbent / at-limit in every run. The
  two-publication rolling horizon beat `day_ahead` in 81 of 81 configurations; the margin was narrow
  and never changed sign — same verdict as before the fix. That 81-of-81 record is strong evidence
  **within that configuration** (all 81 were a Q4 holdout immediately following the training cut)
  and not a proof; it does not transfer to evaluation windows that extrapolate far beyond the cut,
  and a measured counterexample now exists: on the 2025 full-year ladder the same rung cost MORE
  than `day_ahead`. The identified asymmetry sufficient to explain the reversal is the load-forecast
  basis: this rung's profile is frozen at the training cut while `day_ahead` re-learns before each
  dispatch day. Re-running the identical Q4-2025 window on the same dataset reproduced the win,
  which rules out the efficiency correction as the cause. Load-forecast MAE ranked windows
  best→worst 28 / 365 / all; dispatch cost ranked 365 first under both policies in every one of the
  27 noise cells (27 of 27). Price-estimate accuracy was close to irrelevant: the worst mean noise
  penalty is about one tenth of the mean policy margin, and a persistent day-level bias is roughly
  five times as damaging as per-slot noise at the same σ. **Update, later the same day:** the policy
  this grid measured is now the fifth canonical rung, and the grid itself was computed at
  `round_trip_efficiency: 0.88` on dataset `da2ac07a…`, both since superseded — the ordering findings
  are information orderings and are not expected to flip, but every figure in them is
  `PENDING REBUILD`. Full results in [forecast experiments
  §11](energy-baseline-forecast-experiments.md#11-results--executed-holdout-matrix-2026-10-24-corrected-run).
- **Not affected by that defect — record it, because the opposite is the natural assumption:** the
  canonical `day_ahead` policy computes its forecast over the **full** array and selects evaluation
  indices afterwards (`benchmark.py:1053` → `:1058`), so the accepted authoritative run
  `b0a9b59aa846487a82d08fc4ba718708` stands unchanged. The generalisable rule — derive positions,
  *then* slice; slicing first is unsafe whenever a window edge is not a local midnight — and the
  evidence are in [forecast experiments §5](energy-baseline-forecast-experiments.md#slot-positioning-defect-in-the-frozen-profile--found-fixed-matrix-re-run).
  It is also why `--train-end` values that are not local midnights, per *Known issues §2*, are safe
  for the canonical forecast but were not for the experiment module.
- **Outstanding:** independent leakage review of the **corrected** executed artifacts (the defect
  itself is fixed and pinned by tests, including one that pins the trailing boundary as *not* broken;
  what is missing is the independent review); re-derivation of the grid at 0.80 on a rebuilt dataset.
  The adoption question that used to sit in this list has been decided — `rolling_horizon` is a
  canonical rung now — and it does not retire the review, because the rung's *numbers* are still the
  superseded 0.88 ones. Do not cite unreviewed results as accepted.

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
- [ADR-0001 — Forecast-informed economic dispatch](../docs/adr/0001-home-battery-economic-dispatch.md)
- [Battery controller component roadmap](energy-battery-roadmap.md) — the component view of the same
  work: what each piece is, its state, and what is hardware-gated
- [Forecast experiments protocol and results](energy-baseline-forecast-experiments.md)
- [AppDaemon baseline context](./energy-baseline-appdaemon-baseline-context.md)
- [Production execution plan](./energy-baseline-production-execution.md)
- [Implementation plan](./home-battery-optimizer-implementation.md)
