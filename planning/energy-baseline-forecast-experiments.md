# Energy Baseline — Forecast Experiments Protocol

- **Updated:** 2026-10-24
- **Status:** experiment executed on a frozen holdout; independent leakage review outstanding. No
  monetary figure appears in this file.
- **The policy this document specifies is no longer only an experiment.** On 2026-10-24 `rolling_horizon`
  was **promoted to the fifth canonical benchmark policy** — ADR-0003 *Strategy comparison* rung 4, between
  `day_ahead` and `oracle`, included in `--policies all`. That retires this document's earlier framing of a
  standalone experiment beside a four-policy registry; the framing is kept visible below (§6, §7, §9, §10)
  with its wording corrected, because *why* it was once "not a fifth canonical policy" is part of the
  record. What the promotion adopted is the **mechanism**, at exact prices on the legacy two-publication
  cadence — **not** the numbers in §11.
- **Every figure in this file is a 0.88-era result.** The round-trip efficiency shipped afterwards
  (`0.88` → `0.80`), so §11's numbers are optimistic superseded results: the dataset they were computed on
  is superseded and corrected figures are `PENDING REBUILD — fill after the clean-tree build`. Findings
  that are *information orderings* rather than magnitudes are not expected to flip, and are labelled where
  they are quoted.
- **Re-run after a defect fix:** a slot-positioning defect in the frozen load profile was found
  during verification, fixed, and **the whole 81-configuration matrix was re-run**. §11 records the
  corrected run; every number written from an earlier run is superseded. The defect, its correction
  and one claim about it that turned out to be false are in §5.
- **Related:** [Progress & Handoff](./energy-baseline-progress.md) §*Forecast experiments* and
  §*2026-10-24 pass*

This document specifies the offline experiment protocol that compared a two-publication ~36-hour
rolling-horizon policy against the existing single-publication `day_ahead` baseline. Nothing here
is deployed or actuated. No monetary figure appears in this file. The policy that came out of it is
now canonical; the experiment's own numbers remain screening results, produced before the promotion
and before the efficiency correction.

## 1. Objective

Measure whether a two-publication schedule — one at local 14:00 using today's exact prices and
tomorrow's estimated prices, a second at local 20:30 revising with exact prices — improves executed
dispatch cost over the offline single-publication `day_ahead` policy (one MILP per day, solved at
midnight). Holding a load profile fixed does not make its forecast more accurate merely because the
controller replans; price-estimation error and executed dispatch performance are separate metrics. The comparison is on a **frozen three-month holdout**, not a rolling
window.

## 2. Holdout and training windows

| Parameter | Value |
|-----------|-------|
| Training data available | 2024-01-01T00:00:00Z → 2025-10-01T00:00:00Z (~21 months) |
| Holdout (test) | 2025-10-01T00:00:00Z → 2026-01-01T00:00:00Z (Oct–Dec 2025) |
| Training cutoff | Strictly before 2025-10-01T00:00:00Z — no future data in any fit |
| History description | 21 months of extracted history, not "several years" |

The holdout is frozen: load profiles are fit once before the holdout starts and never updated with
test-period observations. This is distinct from the previous full-2025 rolling forecast-window sweep
documented in [Progress §Forecast experiments](./energy-baseline-progress.md#forecast-experiments),
which allowed recent test-period observations to update the forecast. Both experiments change history
length; only the holdout experiment freezes the profile.

### Training window variants (frozen profiles)

| Variant | Fit window | Description |
|---------|-----------|-------------|
| `prior_28` | 28 local days before holdout | Short trailing mean |
| `prior_365` | 365 local days before holdout | One-year trailing mean |
| `prior_all` | All available data before holdout (2024-01 → 2025-09) | Full extracted history |

All three variants use the same position-aligned slot-pooling mechanism with DST handling as the
existing `day_ahead` policy. Neither model uses weather, season, or weekday features — forecasts are
trailing means at each slot position.

**Verified counts (re-derived from the dataset and the runner's `summary.json`):**

- Training usable: **59,241** intervals across **640** local days; `training_end_idx = 61344`.
- Evaluation: **8,832** intervals, **8,820** usable (12 excluded). The split, re-derived from the
  dataset's own quality columns, is **4 × `allocation_quality=unknown_gap`, 4 ×
  `energy_quality=gap_partial`, 4 × `price=null`** — two on 2025-10-02, one on 2025-10-04, one on
  2025-11-03, two on 2025-12-04, one on 2025-12-10, one on 2025-12-11, four on 2025-12-31. No
  exclusion is labelled `ambiguous`; that quality exists in the mask's vocabulary but contributes
  nothing here.
- Holdout spans **93** distinct local days. Both UTC boundaries fall mid-local-day: local
  2025-10-01 contributes 88 usable intervals (the first 8 of its 96 fall below the UTC cut and
  count as training), local 2026-01-01 contributes 4 usable intervals (local 00:00–01:00).
- Fit-window date ranges, as computed by `train_days[-window_days:]`: `prior_28` spans
  **2025-09-04 … 2025-10-01** (the last entry is the partial boundary day, contributing only
  its 8 pre-cut intervals); `prior_365` spans **2024-10-02 … 2025-10-01** and includes the
  2024-10-27 fall-back day (100 intervals); `prior_all` covers all 640 training local days.
- **DST pooled-fallback mechanism (verified, not a hypothesis):** positions 96–99 exist only on
  a 100-slot fall-back day. `prior_28`'s fit window contains no 100-slot day, so those four
  positions are never learned and the 2025-10-26 fall-back day's tail slots fall back to the
  pooled mean. `prior_365` and `prior_all` include 2024-10-27 and learn all four positions.
  The runner reports `eval_slots_pooled_fallback = 4` for `prior_28` and `0` for the other two
  windows.

## 3. Policies compared

### 3.1 Baseline: `day_ahead` (single publication)

One MILP per local day, solved at midnight using archived realised prices for the full day. This is
the policy from the authoritative run `b0a9b59aa846…` (see [Progress](./energy-baseline-progress.md)).

### 3.2 New: `two_pub` (two publications, ~36h horizon)

`two_pub` is this experiment's name for what is now the canonical `rolling_horizon` policy. The
canonical rung's **defaults** are this schedule with the noise turned off (`sigma = 0.0`,
`bias_sigma = 0.0`) — the arms below are reachable through flags, and the canonical rung also accepts a
configurable replan cadence this experiment never used, up to ADR-0001's 15-minute mode.

Two publication events per local day:

| Event | Local time | Price information | Horizon |
|-------|-----------|-------------------|---------|
| Afternoon | 14:00 | Today exact + tomorrow estimated | Through end of tomorrow (~34h, DST-dependent) |
| Evening | 20:30 | Today exact + tomorrow exact | Through end of tomorrow (~27.5h remaining) |

**Event timing rationale:**
- 14:00 is chosen as a point where today's prices are known exactly and tomorrow's must be estimated.
- 20:30 is the first quarter-hour boundary safely after the user-stated 20:15–20:20 publication
  window for next-day prices. This is a conservative assumption, not a measurement of the archive.
- The 14:00 publication is **not** a no-op even when exact prices arrive before tomorrow starts.
  Actions executed between 14:00 and the evening revision change the opening SOC for tomorrow. Even
  with zero price-estimation error, the two-publication policy need not reproduce the single-day
  policy because the horizons and earlier actions differ. No equivalence is claimed.

**Execution rule:** at each event, solve the MILP for the remaining horizon, execute only up to the
next event, and carry the actually reached SOC forward. The evening revision re-solves from the
post-afternoon SOC with exact prices for the full remaining window.

**Verified event counts (from the executed matrix):** each rolling run solved **185 events**
(92 local days × 2 scheduled events + 1 warmup on 2025-10-01). Event kinds: 92 afternoon,
92 evening, 1 warmup. The baseline `day_ahead` solved **93 days** per run (one per local day
intersecting the holdout). Per-event verified durations from a control schedule: afternoon plans
34.0 h and executes 6.5 h; evening plans 27.5 h and executes 17.5 h; boundary days are shorter
(warmup 12.0 h; final day 2025-12-31 afternoon 11.0 h, evening 4.5 h). All 185 events in every
run reported `solver_status = optimal`; zero infeasible, zero no-incumbent, zero at the time limit.

### 3.3 Common constraints

- Shared zero terminal scoring value — identical physical masking
- Planning terminal value = mean of the **usable** training prices only (frozen, independent of
  profile variant; no evaluation price enters it). Its numeric value is recorded in the private
  artifact and is deliberately not repeated here.
- Same battery model, efficiency assumptions, import/export ceilings as the authoritative run
- Clipping on measured load deviation; no instantaneous power claims; no export on the 15-minute
  average basis only; degradation not costed
- End-horizon partial local days handled by UTC clipping

## 4. Synthetic price uncertainty

Without archived 14:00 price snapshots, the afternoon event's tomorrow estimate is modelled as
synthetic multiplicative Gaussian noise applied to the realised next-day prices.

| Parameter | Values |
|-----------|--------|
| Noise model | Multiplicative: `price_estimated = price_realised × (1 + ε)`, `ε ~ N(0, σ²)` |
| Relative sigma (σ) | 0 (perfect control), 0.1, 0.2 |
| Seeds | 0, 1, 2 |
| Granularity | Daily, date-deterministic (same seed + date → same noise vector) |
| Shared across variants | Yes — same noise realization for all profile variants at a given (seed, date) |

**Bias channel (added to stress the independence assumption):** a second independent channel
draws a per-local-day multiplicative bias `b[day] ~ N(0, bias_sigma)`, applied to every slot of
that day. Unlike per-slot noise it does not average out within a day and leaves the within-day
ordering untouched, so it stresses the cross-day hold-versus-spend decision. `bias_sigma = 0`
reproduces the i.i.d.-only model bit-for-bit. Both channels are derived from disjoint slices of
`sha256(f"{seed}:{local_date}")`, so neither channel's draw count can perturb the other.

**Non-identifiability limit (honest):** `p_hat/p_true − 1 = b + e` is additively
non-identifiable from a single realisation, so statistical independence of the two channels is
verified by stream construction and by zero within-day spread at `σ = 0`, not by a numerical
independence test.

**What this is not:**
- σ = 0 is a perfect-control baseline, not a claim of 80–90% forecasting accuracy
- No noise parameter is presented as empirical forecasting skill
- No confidence interval is computed — this is a per-seed screen, not a statistical test
- No annual extrapolation from 3 months to a full year

**What is reported (per seed, per profile variant):**
- MAE (mean absolute error) of price estimates vs realised
- MAPE (mean absolute percentage error), with zero-price intervals excluded from the denominator
- Fraction of intervals within 10% relative error
- Fraction of intervals within 20% relative error
- Handling of negative prices: negative realised prices are kept as-is (they are real market events);
  multiplicative noise can produce negative estimates from positive realised prices and vice versa,
  both are reported without clipping

**Deferred (not required for this experiment):**
- Correlated noise across hours/days
- Stress scenarios beyond σ ∈ {0, 0.1, 0.2}
- Autocorrelated or regime-switching noise models

## 5. Leakage guards

| Guard | Description |
|-------|-------------|
| Execute each interval once | Plans overlap, but only actions up to the next event execute. Every test interval executes exactly once; its load forecast remains frozen |
| Fit before test | All profile fits use only data strictly before 2025-10-01T00:00:00Z |
| Shared noise | The same synthetic noise realization is applied across all profile variants for a given (seed, date) pair, so differences isolate the profile, not the noise |
| No lookahead | The 14:00 event uses only prices available at 14:00 (today exact, tomorrow estimated); the 20:30 event uses only prices available at 20:30 (today exact, tomorrow exact) |

**Verified invariants (from the executed matrix):**

- **Execution multiplicity exactly 1:** over the 8,820 usable holdout intervals, each interval
  is executed exactly once (verified from a control schedule's `per_event` records).
- **Planning multiplicity 1–4, mean 2.55:** each usable interval is planned on average 2.55
  times (and up to four times) by overlapping horizons. Histogram: 74 intervals planned once,
  5,112 twice, 2,364 thrice, 1,270 four times. Re-verified unchanged in the corrected run's control
  schedule. This is why rolling's clipped-kWh totals are roughly three times `day_ahead`'s
  (finding 6 below) — they count repeated planning of the same intervals, not three times more
  refused energy.
- **Frozen profile cut rule (generalisable lesson):** a forecast is frozen only if its fit
  window is bounded by an explicit cut (`training_end_idx`), not by construction order. A
  per-day causal trailing mean over the whole dataset is *not* frozen: each day's forecast uses
  the prior day, which for a 14:00 solve means the same afternoon and evening it was standing
  in. Precomputation is not freezing. The regression test `test_eval_load_change_leaves_forecast_unchanged`
  perturbs every post-cutoff load value with absurd magnitudes and asserts the forecast is
  bit-identical.

### Slot-positioning defect in the frozen profile — found, fixed, matrix re-run

`build_frozen_load_profile` derived a slot's position as **the offset within a run of the sliced
array**, not within its true local day. Any local day truncated by a slice edge was therefore
relabelled from position 0. Verified against the real dataset: `training_end_idx = 61344` is
`2025-10-01T00:00:00Z`, which is **02:00 CEST**, not a local midnight, so local day 2025-10-01's
true first position in the evaluation slice is **k = 8** while the slice-first rule assigned
**k = 0**. The first partial local day received forecasts shifted by **8 positions = 2.0 hours**,
affecting **88 of 8,832 holdout intervals (1.00 %)** — all on that one day, all with the same
shift. Aggregate load-forecast MAE moved only in the fourth significant digit (§11), so this was
never a numbers problem; it was a correctness problem, and the matrix has been re-run rather than
patched arithmetically.

**The fix** derives position from the local wall clock:
`k = (t_utc − local_midnight_of_its_local_day) / interval_length`, with the midnight instant built
by `datetime.combine(local_day, time.min, tzinfo=tz)` (**fold=0**) and the subtraction taken between
two *aware* instants so `k` counts real elapsed time. `interval_starts_utc` is now **required** and
its absence raises `ValueError` naming it — silently falling back to the offset rule is what
produced the defect. The same clock rule is applied on the learned-mean side, so indexing stays
consistent; for a whole local day clock position ≡ offset-within-run, pinned by
`test_full_local_day_positions_are_unchanged_from_offset_within_run`, which is what keeps already
measured profiles comparable instead of re-indexing every learned mean.

**DST convention:** each UTC instant maps to exactly one position, so the repeated fall-back hour is
unambiguous (first 02:00 CEST → 8, second 02:00 CET → 12). Fall-back days run positions 0..99,
spring-forward days 0..91.

**Scope of the change, measured rather than assumed.** Re-deriving positions from the clock changed
the position assigned to **88 evaluation intervals** (all on 2025-10-01, all by exactly +8) and, on
the learned-mean side, **92 training intervals** — those belonging to local day 2024-01-01, which is
itself truncated by the dataset's own leading edge (index 0 is `2024-01-01T00:00:00Z` = 01:00 CET).
That truncation belongs to the build window, not to this function, and was deliberately left alone;
what changed is that it is now *labelled* correctly (positions 4..95 instead of 0..91). Consequence:
the `prior_28` and `prior_365` fit windows contain no truncated day, so their learned means are
bit-identical before and after, while `prior_all`'s slot means genuinely moved. That is why all three
`day_ahead` baselines shifted a little in §11 rather than just one.

Two honest details about the fix itself:

- **`datetime.replace(hour=0)` would have been wrong**, and is the obvious way to write this. It
  keeps the *current* utcoffset, so after a fall-back it re-anchors to 01:00 CET and collapses the
  25-hour day onto 0..95 — duplicating positions and serving evening means an hour off. A DST test
  caught this; `_clock_slot_position`'s docstring records why the aware-instant subtraction is used
  instead.
- **A spring-forward limitation survives, deliberately.** On a 23-hour day position 8 *is*
  wall-clock 03:00, so afternoon means land one hour early in wall-clock terms there. That is
  inherent to any contiguous-position scheme, identical to what the old rule produced for full
  local days, and unchanged by this fix — recorded as a limitation, not a regression.

### A test that had been passing for the wrong reason

`test_not_bit_identical_to_day_ahead` failed **legitimately** after the fix, and investigating it is
the most instructive item in this experiment. Its synthetic load was identical at each clock position
of every day, so a *correctly* positioned per-slot mean reproduces measured load exactly; given an
exact forecast and (at σ = 0) exact prices, the two policies then plan identically and the asserted
cost difference vanished. **The positioning defect had been supplying the very forecast error the
test depended on** — its mis-assignment by up to eight slots *was* the imperfection being measured.
The fixture now carries a per-day load level, and it is verified non-vacuous against the pre-fix
fixture: without the per-day level `forecast_exact = True` and `cost_diff = 0`; with it
`forecast_exact = False` and `cost_diff` is non-zero.

**Generalisable lesson:** a test that asserts "these two things differ" can be satisfied by an
unrelated bug, so it needs a **non-vacuity check** — a stated reason, verifiable in the fixture, for
why they must differ. Several guards in this suite now assert non-vacuity before the interesting
equality.

### Withdrawn claim: the trailing boundary was never misaligned

An earlier pass of this document recorded that the **trailing** boundary (2026-01-01) was also
misaligned, with its 4 intervals receiving offsets 0..3 "instead of 92..95". **That is false.**
Verified against the dataset: index 70172 is `2025-12-31T23:00:00Z`, `local_day = 2026-01-01`, its
true clock position is **k = 0** and the slice-first rule also assigned **0**. A *trailing* partial
day starts at the slice edge rather than being cut mid-way through it, so both rules agree there.
Only the **leading** partial local day was affected. The claim is kept visible here rather than
deleted, and `test_trailing_partial_local_day_is_not_shifted` pins the correct behaviour explicitly
so nobody "fixes" that boundary later.

### Negative result: the canonical `day_ahead` path and the accepted run are NOT affected

The **canonical** `day_ahead` policy does not have this defect, so the accepted authoritative run
`b0a9b59aa846487a82d08fc4ba718708` stands unchanged. **Read "stands unchanged" narrowly: it is about
this defect.** The run was not contaminated by the slot-positioning bug; it *was* computed at the 0.88
round-trip efficiency that has since been superseded, so it is unchanged-but-superseded and its figures
are `PENDING REBUILD` (see §11's banner). Recorded as a negative result with its
evidence, because a later reader who has just read the section above would otherwise assume the
accepted run is contaminated too.

Verified from source: `benchmark.py:1053` calls `contiguous_day_bounds` over the **full** dataset
and computes the forecast over the full array, and only then selects the evaluation indices out of
the resulting forecast (`forecast_eval = forecast_values[np.asarray(eval_index, …)]`, line 1058).
The new module did the opposite — it sliced first and derived positions from the slice, which is
what introduced the shift. Concretely: the authoritative run's evaluation starts at index **35136**
= `2025-01-01T00:00:00Z`, whose full-array local-day bounds for 2025-01-01 are **(35132, 35228)**, so
the canonical path assigns **k = 4** — the correct clock offset for 01:00 CET — where a slice-first
implementation would have assigned k = 0. That local day contributes **92 of its 96** intervals to
the evaluation slice.

**Generalisable rule: slice-then-derive-position is unsafe whenever a window edge is not a local
midnight; derive-then-slice is safe.** This is also why `--train-end` values that are not local
midnights — already documented as *Known issue §2* in the progress file, where the split is
UTC-scoped rather than Madrid-local — are safe for the canonical forecast but were *not* safe for
the new module. Any future windowed experiment must derive positions on the full array or require a
local-midnight window edge.

**What the defect never threatened:** leakage. No post-cut data entered any slot mean before or
after the fix (the cut rule is separate and independently tested), and both policies read the same
frozen profile, so the head-to-head comparison was never asymmetric — it was just measured on a
mis-shifted forecast for 88 intervals.

## 6. Implementation

Implementation is complete, and the policy is **no longer experiment-only**: `rolling_horizon` is a
canonical `benchmark` policy, the fifth rung of ADR-0003's ladder, selected by `--policies all`. The
standalone experiment runner still exists and still does what it did — it is how the noise arms are
reachable, since the canonical rung's defaults are exact prices. What changed is the sentence this
section used to carry: "**no new canonical `energy-baseline` subcommand or registered benchmark policy
is implied. The canonical registry stays at four policies.**" That was accurate when the matrix was run
and is withdrawn — the registry is five policies. No subcommand was added for the experiment, and none
was needed: the canonical flags live on `benchmark` (`--rolling-horizon-replan-interval-slots`,
`--rolling-horizon-horizon-slots`, `--rolling-horizon-sigma`, `--rolling-horizon-bias-sigma`,
`--rolling-horizon-seed`, `--rolling-horizon-time-limit-s`).

| File | Role |
|------|------|
| `scripts/energy-baseline/src/energy_baseline/rolling_horizon.py` | The policy module, now canonical: two-publication logic **plus** a configurable replan cadence (`replan_interval_slots` / `horizon_slots`, implementing ADR-0001 §3), clock-derived slot positioning (`_clock_slot_position`), synthetic noise with two channels, event-driven execution, and a single publication rule so the information set is derived from the decision instant and cannot drift between cadences |
| `scripts/energy-baseline/hack/compare_forecasts.py` | Comparison script: runs baseline and two-publication on the frozen holdout, computes metrics; carries the cadence flags |
| `scripts/energy-baseline/tests/test_rolling_horizon.py` | **112** tests (63 when this section was first written): price noise, day-level bias, frozen profile and slot positioning, event computation, warmup splitting, DST, integration, plus the golden-digest tests that prove the legacy cadence is bit-identical and the cadence-mode tests |
| `scripts/energy-baseline/tests/test_compare_forecasts_cli.py` | **22** tests running the CLI as a subprocess against a synthetic dataset (16 before the cadence work: +6 subprocess tests for the cadence flags, the multiplicity pair, true-price scoring, and the three validation refusals) |
| `scripts/energy-baseline/src/energy_baseline/benchmark.py` | Where the rung is registered: `POLICY_NAMES`, canonical defaults, flag validation, artifact blocks, comparison flags |
| `scripts/energy-baseline/hack/ROLLING_HORIZON_STATUS.md` | Status document for the rolling-horizon code — defect list, slot-positioning rule and test inventory live there |

**Test suite:** **1201 passed** when this section was written (1194 before the slot-positioning fix, +7
positioning and boundary tests), `ruff check` and `ruff format --check` clean on 35 files. **Today:
1444 passing**, ruff clean on **39** files. The growth is the cadence work, the fifth-rung registration,
the HA enum reader and the dispatch contract — see [Progress §*2026-10-24 pass*](./energy-baseline-progress.md).

## 7. Status

| Task | Status | Notes |
|------|--------|-------|
| Protocol specification | **done** | This document |
| `rolling_horizon.py` implementation | **done, and canonical** | Three defects found in review/verification and fixed: the frozen forecast was not truly frozen (fit window was per-day causal, not cut-bounded); a warmup crash when the evaluation window opened after 20:30 local (fixed by splitting warmups at every local midnight); and slot positions taken as slice-run offsets instead of local clock offsets (§5). All three have regression tests. The module has since gained the configurable replan cadence (§6); suite now **1444 passed**, ruff clean |
| `hack/compare_forecasts.py` implementation | **done** | End-to-end synthetic invocation verified; exact data-loader API checks passed |
| Synthetic noise generation | **done** | Deterministic date/seed generation with two independent channels (per-slot noise and per-day bias); diagnostics cover estimated delivery slots only |
| Frozen profile fits (prior_28, prior_365, prior_all) | **done** | Fit window bounded by explicit `training_end_idx`; regression test perturbs every post-cutoff load value and asserts the forecast is bit-identical. Positions are now clock-derived (§5), so all three profiles were re-fit — which is why the matrix was re-run rather than corrected arithmetically |
| Baseline runs (day_ahead on holdout) | **done** | 93 days solved per run; invariant to the noise channels (the baseline never sees the estimate), so 81 records collapse to 3 distinct runs — one per load window. Re-run after the fix: all three moved slightly, because every window applies a different forecast on 2025-10-01, and `prior_all`'s learned means moved too (§5) |
| Two-publication runs (σ ∈ {0, 0.1, 0.2}, bias σ ∈ {0, 0.1, 0.2}, seeds {0, 1, 2}) | **done** | 81 configurations; each rolling run solved 185 events (92 days × 2 + 1 warmup); zero infeasible / no-incumbent / at-limit in every one of the 162 policy runs |
| Metric computation (MAE, MAPE, frac within 10/20%) | **done** | Load-forecast accuracy by window; price-estimate accuracy by (σ, bias σ) cell; EFC; terminal SOC; clipped kWh — all re-derived from the corrected run in §11 |
| Leakage review | **not done** | An independent check of the fit-before-test and shared-noise invariants has **not** been performed on the *executed corrected* artifacts. The §5 defect is fixed and pinned by tests, but that is self-verification, not the outstanding independent review — so this row stays open and no result below may be cited as accepted. It bears less on the canonical rung than it did on the grid: the rung's defaults are the noise-free arm, and its promotion did not wait on this review |
| Artifact identities | **superseded** | Dataset `da2ac07a…` is **no longer current**: the 0.80 config change supersedes it, so the replacement is `PENDING REBUILD — fill after the clean-tree build`. Run directory `/tmp/opencode/energy-baseline/holdout-final-20261024T035727/` (`summary.json` + 81 rolling schedules + 3 `window_*_day_ahead.json` = 84 files). Outside git and outside the canonical store, so **not** immutable: re-derive to cite later. The earlier `holdout-bias-20261024T023833/`, `holdout-full-*` and `holdout-sigma0-*` directories predate the fix and are superseded (§11) |
| Adoption decision | **taken — the policy is canonical** | This row read "**not done** — no adoption is recommended. One held-out quarter plus a few synthetic seeds is a screen, and §7's own review row is still open". That judgement is kept on record because it was the honest reading of a *screen*, and it still is one. What changed is that the adoption taken was adoption of the **mechanism** — a replanning rung with an earlier and longer information set — registered at exact prices on the legacy cadence, which is the arm that does not depend on synthetic noise at all. It is not an endorsement of §11's magnitudes, and it retires neither the leakage review nor the re-derivation the efficiency correction forces |
| Final comparison table | **done, 0.88-era** | See §11 below. Every figure there was computed at `round_trip_efficiency: 0.88` on a dataset that config change has superseded, and before the rung was promoted: corrected numbers are `PENDING REBUILD` |

## 8. Validation commands

**Flags (read from `hack/compare_forecasts.py` argparse):** `--dataset`, `--config`, `--output`
(must be fresh), `--test-start`, `--test-end`, `--windows` (comma-separated days or 'all'),
`--sigmas`, `--bias-sigmas`, `--seeds`, `--time-limit`. The executed matrix's `summary.json`
records no argv; the configuration is recoverable from the run records' fields
(`window_days_requested`, `sigma`, `bias_sigma`, `seed`).

Planned gates are the focused synthetic tests, full package pytest suite, Ruff lint/format checks,
and an independent review of accounting, chronological execution and information boundaries.
Private comparison output must use a fresh destination outside git.

## 9. Constraints

- **No cost, savings, euro amounts, or household energy totals in git.** Only technical metrics:
  MAE, MAPE, fraction within error bands, solver gaps, quality fractions, SOC bounds, clipped kWh.
- **No statistical significance claims.** Per-seed screens, not confidence intervals.
- **No annual extrapolation.** Results cover Oct–Dec 2025 only.
- **No deployed changes.** This is an offline experiment. The canonical registry no longer "stays at
  four policies until a separate adoption decision", which is what this bullet used to say: the
  adoption decision was taken and the registry is **five**. Nothing about that deploys or actuates
  anything — the promotion moved a policy from `hack/` experiment code into the canonical ladder, it
  did not move it onto a cluster.
- **No commit, stash, restore, or pre-commit operations** during this documentation pass.
- **No live queries.** All data is archived.
- **No private data written to git.** Numeric results stay local.

## 10. Decision boundary

No adoption recommendation is made before the results. Compare executed cost, cycling, clipping,
terminal SOC and price-noise sensitivity, rather than choosing on forecast MAE alone. One held-out
quarter and a few synthetic seeds are a screening experiment, not a purchase or deployment case.

**Update 2026-10-24 — the rung was adopted, and that does not retire this paragraph.** What was adopted
is the *mechanism*: a replanning policy that holds the same published information earlier and over a
longer span, registered at exact prices where no synthetic-noise assumption enters at all. What was
**not** adopted is a claim about magnitudes — §11 is still a screen over one quarter, still carries the
open leakage review of §7, and is now additionally marked 0.88-era. Nothing here is a purchase or
deployment case, and nothing was deployed.

**MAE-ranked windows ≠ cost-ranked windows (verified):** load-forecast MAE ranked the three
windows best→worst as 28, 365, all; dispatch cost put **365 first in all 27 noise cells under both
policies** (27 of 27 for each). A decision rule based on MAE alone would have picked 28, while cost
says 365. The 28-day window's MAE advantage is real but does not translate to dispatch value;
forecast accuracy and dispatch value are different metrics. This holds in the corrected run, and it
held in the pre-fix run too — the ordering did not depend on the defect.

There is **no deployed battery optimiser**: both policies are offline simulations. Preserve that
boundary. If a later validation needs real publication errors, propose snapshots at 14:00 and after
evening publication separately; no telemetry or Home Assistant configuration change is made here.

## 11. Results — executed holdout matrix (2026-10-24, corrected run)

> **Superseded on efficiency — read the numbers below as a record, not as results.** This matrix was
> computed at `round_trip_efficiency: 0.88` on dataset `da2ac07a…`. Both have been superseded: the
> shipped config is now 0.80, which makes every figure here an optimistic 0.88-era result, and the
> dataset that produced them no longer describes that config. The corrected matrix is
> `PENDING REBUILD`. They are kept rather than deleted because §5 and §7 were reviewed against these
> bytes, and because the two findings that carry the section are *orderings* — the rolling rung beat
> `day_ahead` in every configuration of that Q4 holdout grid (all 81 configurations were a holdout
> immediately following the training cut), and forecast accuracy ranked the load windows the opposite
> way to dispatch value — which a change in battery friction does not obviously reverse. That scope
> does not transfer to evaluation windows that extrapolate far beyond the cut, and a measured
> counterexample now exists: on the 2025 full-year ladder the same rung cost MORE than `day_ahead`.
> The identified asymmetry sufficient to explain the reversal is the load-forecast basis: this rung's
> profile is frozen at the training cut while `day_ahead` re-learns before each dispatch day.
> Re-running the identical Q4-2025 window on the same dataset reproduced the win, which rules out
> the efficiency correction as the cause. What *is*
> affected is every magnitude, including the noise-penalty-versus-margin ratios, since the margin itself
> is a 0.88 quantity. The run predates the rung's promotion as well, so it describes an experiment
> beside a four-policy registry, not the canonical fifth rung.

### Artifact identities and storage caveat

- Dataset `da2ac07a7a39affc43ef9e94523fb6a154ee6213d590086d8c261b265d957140` — **SUPERSEDED** by the
  0.80 config change; build manifest sha256
  `15ce9b8f596911cae1bbdb68c03d2efbd322936abb334b7fcafe4f315a445cef`. The rebuild's id is
  `PENDING REBUILD — fill after the clean-tree build`.
- **Cite only this run directory:** `/tmp/opencode/energy-baseline/holdout-final-20261024T035727/`,
  executed **after** the slot-positioning fix in §5. Contents: `summary.json` + `schedules/` with 81
  rolling files (one per configuration) and 3 `window_*_day_ahead.json` files (the day-ahead baseline
  is deterministic in the noise channels, so one per window suffices) = 84 files total.
- **Superseded — do not cite:** `holdout-bias-20261024T023833/`, `holdout-full-20261024T021630/` and
  `holdout-sigma0-20261024T021545/`. All were computed **before** the alignment fix, so their frozen
  profiles carry the shifted first partial day. They live outside git and outside the canonical store,
  which means they are **not immutable artifacts** and there is nothing to preserve: re-derive them
  rather than cite them. Numbers taken from them that appeared in earlier passes of this document are
  listed before/after in the last subsection below, so a reader who saw an old figure can tell which
  is which.
- Nothing here has been independently reviewed (§7): these are **screening results, not accepted ones**.

### Configuration (unchanged by the fix)

- **81 configurations** = 3 load windows (28 / 365 / all-available, where `all` resolved to **640**
  local days in every run) × 3 per-slot σ (0, 0.1, 0.2) × 3 per-day bias σ (0, 0.1, 0.2) × 3 seeds.
- Holdout `[2025-10-01T00:00:00Z, 2026-01-01T00:00:00Z)`: **8,832 intervals, 8,820 usable**
  (12 excluded — 4 `allocation_quality=unknown_gap`, 4 `energy_quality=gap_partial`, 4 `price=null`).
  Training strictly before the cut: **59,241 usable intervals across 640 local days**, with
  `training_end_idx = 61344`.
- Every rolling run solved **185 events** (92 days × 2 + 1 warmup); every `day_ahead` run solved
  **93 days**; **zero infeasible, zero no-incumbent, zero at the time limit** in all 81 — and all 185
  events of every rolling run reported `solver_status = optimal`.
- In all 81 runs, for both policies, `cost_eur == meter_cost_eur` with inventory adjustment 0 — the
  shared zero terminal-scoring basis held wherever it could be checked.
- Terminal SOC across all 162 policy runs spanned **2.1381 … 2.9692 kWh** against a 1.024 kWh opening.

### Load-forecast accuracy by window (frozen, on the 8,820 usable holdout intervals)

| Window | MAE kWh/interval | MAPE | within 10 % | DST pooled-fallback slots |
|---|---|---|---|---|
| 28 days | 0.075666 | 0.46467 | 0.19229 | 4 |
| 365 days | 0.089818 | 0.74463 | 0.08220 | 0 |
| all (640) | 0.091565 | 0.77414 | 0.06882 | 0 |

The 28-day window has clearly the best MAE and MAPE; the 365-day and all-available windows are
close to each other. The 4 fallback slots for the 28-day window are exactly the extra positions
(96–99) of the 2025-10-26 fall-back day, whose fit window contains no 100-slot day.

**Before → after the alignment fix**, so the size of the defect is visible rather than asserted: MAE
0.075824 → 0.075666 (28 days), 0.089903 → 0.089818 (365), 0.091631 → 0.091565 (all), and MAPE fell in
all three windows too. Within-10 % is not monotone: it rose for the 28-day window (0.19116 → 0.19229)
and for all-available (0.06871 → 0.06882) but **fell** slightly for 365 days (0.08254 → 0.08220),
because re-anchoring one day's 88 intervals moves a band count, not a level — which is exactly what a
genuinely tiny correction looks like on a threshold metric. No window ranking changed.

### Price-estimate accuracy actually realised (estimated slots only, 8,744 per run)

Values shown are the seed-0 arm; per-seed spread within a cell is small (e.g. worst-cell MAPE
ranges 0.2243–0.2333 across seeds). **The fix did not change this table**: noise depends on
`(seed, local_date)` and the price grid, never on slot positioning, and every cell was re-checked
against the corrected `summary.json` — all nine reproduce unchanged, sign flips included. All runs
sharing a `(σ, bias σ, seed)` cell also carry byte-identical noise metadata across the three load
windows, which is the shared-noise invariant of §5 holding in the executed artifact.

| per-slot σ | per-day bias σ | MAPE | within 10 % | within 20 % | sign flips |
|---|---|---|---|---|---|
| 0 | 0 | 0.0000 | 1.000 | 1.000 | 0 |
| 0.1 | 0 | 0.0813 | 0.672 | 0.952 | 0 |
| 0.2 | 0 | 0.1626 | 0.368 | 0.672 | 0 |
| 0 | 0.1 | 0.0815 | 0.681 | 0.967 | 0 |
| 0 | 0.2 | 0.1631 | 0.373 | 0.681 | 0 |
| 0.1 | 0.1 | 0.1153 | 0.507 | 0.837 | 0 |
| 0.1 | 0.2 | 0.1826 | 0.336 | 0.624 | 0 |
| 0.2 | 0.1 | 0.1820 | 0.332 | 0.616 | 0 |
| 0.2 | 0.2 | 0.2307 | 0.266 | 0.507 | 11 |

Note explicitly that σ = 0.1 and σ = 0.2 land at MAPE 0.081 and 0.163 — under a
one-minus-MAPE reading, accuracy fractions of 0.92 and 0.84, which straddle the stated 0.8–0.9
range — but this is a mapping onto a stated range, not a measurement of any real forecast,
because no archived 14:00 estimate exists. Noise is identical across load-window variants for a
given `(seed, date)` by construction, which is what isolates the profile effect.

### Findings (restated against the corrected run)

1. **The two-publication rolling horizon beat single-publication `day_ahead` in 81 of 81
   configurations** — every window, every noise level, every seed. The margin never changed sign and
   its spread is narrow: the widest margin is less than one and a quarter times the narrowest. This is
   the robust result **within that configuration** (all 81 were a Q4 holdout immediately following
   the training cut), and it is the same verdict the pre-fix run gave — the defect moved margins
   slightly and never reversed one. That scope does not transfer to evaluation windows that
   extrapolate far beyond the cut, and a measured counterexample now exists: on the 2025 full-year
   ladder the same rung cost MORE than `day_ahead`. The identified asymmetry sufficient to explain
   the reversal is the load-forecast basis: this rung's profile is frozen at the training cut while
   `day_ahead` re-learns before each dispatch day. Re-running the identical Q4-2025 window on the
   same dataset reproduced the win, which rules out the efficiency correction as the cause.

2. **Price-estimate accuracy remained close to irrelevant.** The worst mean penalty of any noise arm,
   measured against its own exact-price control at the same window (the 28-day window under a day-bias
   σ = 0.2, whose estimates carry MAPE 0.1631), is about **one tenth** of the mean
   rolling-versus-`day_ahead` margin; taken arm by arm rather than averaged over seeds, the largest
   penalty sits a little above that. Both are ratios between two *effects*, not claims about cost. The
   structural reason: at 20:30 the exact curve for the whole target day arrives **before that day
   starts**, so a 14:00 error is fully corrected before the affected day executes; the only exposure is
   the 6.5 h between events, when the *current* day's prices are exact anyway.

3. **A persistent day-level bias was roughly five times more damaging than independent per-slot noise
   at the same σ.** Pooled across windows and seeds, the mean penalty of a σ = 0.1 day-bias is ~5.7×
   the mean penalty of σ = 0.1 per-slot noise, and at σ = 0.2 about ~8.4× (again: a ratio between two
   cost penalties). The bias variant was added precisely to stress the independence assumption an
   i.i.d.-only model makes, and it changed the magnitude but not the conclusion. Two honest limits: the
   per-configuration ratio is far wider than the pooled one (median ~6.6, spanning 1.6–37, with two of
   the eighteen pure per-slot arms actually *cheaper* than their own control), so the effect is stated
   as pooled; and `p_hat/p_true − 1 = b + e` is additively **non-identifiable** from a single
   realisation, so independence is verified by stream construction — two separate `default_rng`
   instances seeded from different 8-byte slices of one `sha256(f"{seed}:{date}")` digest — and by zero
   within-day spread at σ = 0, **not** by a numerical independence test.

4. **Load-history length barely mattered on the frozen holdout, and forecast accuracy ranked the
   windows opposite to cost.** The 28-day window clearly had the best MAE and MAPE, yet the **365-day
   window was cheapest in all 27 noise cells under both policies**. Per policy, precisely: under
   `day_ahead` the ranking is fixed — 365 first, 28 second, all-available third (it is deterministic in
   the noise channels, so there is one ranking, not 27) — while for rolling 365 also wins all 27 cells
   but **second place goes to the 28-day window in 18 cells and to all-available in 9**, and of the nine
   (σ, bias σ) combinations only two have all three seeds agree on second place. The gap between those
   two windows never approaches either the policy margin or the noise penalty. Record the contrast with
   the earlier rolling full-2025 sweep ([Progress §Forecast
   experiments](./energy-baseline-progress.md#forecast-experiments)), where the 28-day window had the
   lowest dispatch cost and longer history did not improve it. The two experiments differ in whether
   recent observations update the forecast, so they are not comparable and neither establishes a general
   ranking. **Forecast accuracy and dispatch value are different metrics** — any adoption rule keyed on
   MAE alone would have picked the 28-day window here, and that is the durable lesson.

5. **EFC (equivalent full cycles):** `day_ahead` **114.70 / 111.16 / 110.87** and rolling control rows
   **116.73 / 115.77 / 114.32** for windows 28 / 365 / all. Across every noise arm rolling spans
   113.17–118.02, so it cycles somewhat more than `day_ahead` at each window but stays within a few EFC
   of it — the rolling policy's advantage is not bought with materially more cycling. Degradation
   remains costed nowhere in any of these numbers.

6. **Clipping totals are not comparable between the two policies and must not be read as
   curtailment.** Rolling reports roughly three times the clipped charge kWh of `day_ahead` (window 365
   control: rolling 346.39 vs `day_ahead` 114.84; ratio ~3.0) and roughly two and a half times the
   clipped discharge kWh (482.35 vs 189.00; ratio ~2.5). The reason is arithmetic, not physical: each
   usable interval is **planned** a mean of 2.55 times (up to four) across overlapping ~34 h and ~27.5 h
   horizons while being **executed** exactly once — measured multiplicity histogram over the 8,820
   usable intervals: 74 planned once, 5,112 twice, 2,364 thrice, 1,270 four times; execution multiplicity
   exactly 1 for every one of them. The totals therefore count repeated planning of the same intervals.
   Record this explicitly so nobody later reads it as three times more refused energy. **It is now a
   reported property rather than a reader's duty:** the canonical rung emits
   `executed_multiplicity_mean` (exactly 1.0), `planning_multiplicity_mean` (about 94.4 at a one-interval
   cadence over a 96-slot horizon, about 2.5 at this legacy cadence — the same quantity the 2.55 measured
   here), `clipping_totals_comparable_across_cadences: false` with an explanatory note, and `solve_count`
   per run, so a run's compute cost is visible in its own artifact.

7. **Terminal SOC** ended between **2.1381 and 2.9692 kWh** across all 162 policy runs against a 1.024
   kWh opening, uncredited under the shared zero scoring value. `day_ahead`'s terminal SOC is invariant
   to the noise channels (2.97 / 2.70 / 2.56 for windows 28 / 365 / all); rolling's ranges are
   2.14–2.50, 2.30–2.44 and 2.24–2.36. Because rolling ends with somewhat less stored energy, part of
   its margin is a mechanical consequence of the uncredited-terminal convention. Quantified only
   ordinally: it is small relative to the policy margin, and the same convention applies to both
   policies in the same direction, so it does not bear on the ordering in finding 1.

### What the re-run changed, and what it did not

| Quantity | Pre-fix run (superseded) | This run |
|---|---|---|
| Rolling beats `day_ahead` | 81 / 81 | **81 / 81** — unchanged |
| Load-forecast MAE, 28 / 365 / all | 0.075824 / 0.089903 / 0.091631 | 0.075666 / 0.089818 / 0.091565 |
| EFC `day_ahead`, 28 / 365 / all | 114.60 / 111.08 / 110.77 | 114.70 / 111.16 / 110.87 |
| EFC rolling control rows | 116.62 / 115.63 / 114.22 | 116.73 / 115.77 / 114.32 |
| Clipped kWh, window 365 control (charge / discharge) | 347.25 vs 114.64; 483.00 vs 189.10 | 346.39 vs 114.84; 482.35 vs 189.00 (ratios ~3.0 / ~2.5 unchanged) |
| Terminal-SOC span over all runs | 2.1381–2.9692 kWh | same span (one `day_ahead` value moved in the third decimal) |
| Price-estimate accuracy, all nine cells | — | byte-identical, verified |
| Bias-versus-noise damage ratio, σ = 0.1 / 0.2 | ~5.7× / ~8.3× | ~5.7× / ~8.4× |
| Rolling second place between the two remaining windows | 28 in 13 cells, all-available in 14 | 28 in **18** cells, all-available in **9** — unstable in both runs and across seeds, so no claim rests on it |

The pattern matches the mechanism: 88 intervals out of 8,820 shifted by two hours cannot move an
aggregate metric much. What it *did* do — break a test that had been relying on the shift (§5) — is the
louder signal, and it is why the matrix was re-run end to end instead of being corrected on paper.

## 12. Owner assertions of 2026-10-24

Three owner-supplied facts recorded on 2026-10-24. All are **owner assertions, not measurements
made by this pipeline** (`user_assertion`, `asserted_on: "2026-10-24"`, `verified_from_data: false`).

### 12.1 The 14:00 next-day price estimate has a real source

The 14:00 estimate of tomorrow's PVPC prices is constructible from **OMIE day-ahead prices plus
adjustments to derive PVPC**, with owner-stated accuracy around **80%**. Previously no source
existed and the synthetic-noise grid in §4 was a substitute for a source that did not exist. A real
one now does, which makes the two-publication schedule **implementable rather than hypothetical**.

**Honesty preserved:** there is still **no archived record of any 14:00 estimate**, so nothing in
the store measures the owner's 80%. The synthetic grid remains the only quantitative evidence about
how estimate accuracy affects dispatch.

**The tested grid already brackets 80% accuracy.** The two cells nearest a natural "MAPE 0.20"
reading of "80% accuracy" are:

| per-slot σ | per-day bias σ | MAPE | within 10 % | sign flips |
|---|---|---|---|---|
| 0.2 | 0.1 | 0.1820 | 0.332 | 0 |
| 0.2 | 0.2 | 0.2307 | 0.266 | 11 |

"80% accuracy" has no unique mathematical definition; MAPE 0.20 is the interpretation used here,
and the bracket means the conclusion does not depend on picking one.

**The sensitivity conclusion survives at the owner's stated accuracy.** Across all 81 configurations
the rolling horizon was cheaper than `day_ahead`, margin range **−1.6509 to −2.0559** with median
**−1.9270** (differences between two policies on the same basis, not costs). At the two cells
bracketing MAPE 0.20 the mean penalty against the perfect-price control was **+0.0296** and
**+0.0981**, worst single configuration **+0.2096**. The worst observed penalty at the owner's
stated accuracy is roughly **one eighth of the median policy margin** (0.2096 / 1.9270 ≈ 0.11), and
the mean penalties are one to two orders of magnitude smaller.

**Both magnitudes above are 0.88-era and are quoted as the record of what was reviewed, not as current
figures.** The margin between two policies is a difference of two costs computed on the superseded
battery, so it moves when the efficiency moves; the *ratio* of a noise penalty to that margin is the
thing the conclusion rests on, and whether the ratio survives is `PENDING REBUILD`. No corrected figure
is stated here.

**Decision this licenses:** no investment in price-forecast skill is warranted for this controller.
The value sits in horizon length and the evening revision, not in estimate accuracy. This is what
the grid supports; it is an offline conclusion about a simulated controller — nothing is deployed.

### 12.2 Published equals realised, always

The prices published between 20:15 and 20:20 on day D−1 for delivery day D are the prices realised.
This is the assumption `day_ahead` was recorded as making without evidence
(§*Known issues §3* in [Progress](./energy-baseline-progress.md); ADR-0003 *Open validation item:
day-ahead publication versus realised archive*). The owner assertion resolves that open validation
item: published ≡ realised is now an owner-stated fact, `verified_from_data: false`.

This removes the experiment's **largest unverified input assumption**. The daily attribute snapshot
would still convert assertion into measurement, and is now optional rather than blocking.

### 12.3 Round-trip efficiency 0.80–0.84

**Applied: the shipped value is now 0.80, and this section's premise has changed under it.** What this
section used to say — "Recorded in `config/tariff.yaml` above the `round_trip_efficiency` value. **The
shipped 0.88 is** optimistic by 4–8 percentage points against the only efficiency evidence that now
exists" — was accurate when written and is withdrawn: on 2026-10-24 the owner **adopted 0.80**, the
bottom of the community-measured 0.80–0.84 range, reasoning that at 2.5 kW the unit operates mostly near
its power rating where efficiency is typically worst. `config/tariff.yaml` now ships
`round_trip_efficiency: 0.80`; one value line, with every other battery parameter untouched
(`capacity_kwh` 5.12, `power_kw` 2.5, `soc_window_min` 0.20, `soc_window_max` 0.95,
`initial_soc_frac` 0.20, `import_limit_kw` 5.0). One-way efficiency is `sqrt(0.80) = 0.89443` per
direction, replacing `sqrt(0.88) = 0.93808`, and `config_hash` moved from
`41fdc30b926c099be2cbef201db4141244d82faaa80b9907c7530354f64bf182` to
`7a901801b4148233e232fb86d534c4b7f626ec32af8e7279ad2f9e77831687d1`.

**It is still an assumption.** 0.80 is community measurement from other installations, not a measurement
of this household's unit; the efficiency-versus-power curve is still unmeasured and idle draw is still
assumed zero and unmeasured.

**Consequence for this document.** The `config_hash` → `dataset_id` coupling the earlier version of this
section warned about — "a sensitivity run therefore precedes any config change, so the correction cannot
be applied by editing a value in place without superseding the accepted dataset" — **fired**: the
sensitivity run was not waited for, dataset `da2ac07a…` is superseded, and §11's EFC, clipping and
margin figures are 0.88-era. A lower round-trip efficiency penalises cycling, which compresses the
information ladder and can reorder rungs; **whether it reorders this experiment's rolling-versus-`day_ahead`
verdict is not known**, because the re-derivation has not run. Corrected dataset id and corrected figures:
`PENDING REBUILD — fill after the clean-tree build`. Full record in
[Progress §*Round-trip efficiency applied (2026-10-24)*](./energy-baseline-progress.md) and ADR-0003
§*Round-trip efficiency: 0.88 → 0.80, applied*.

## 13. References

- [Progress & Handoff](./energy-baseline-progress.md) — authoritative run, acceptance conditions,
  rolling-history sensitivity
- [ADR-0003 — Phase 1 measurement and baseline](../docs/adr/0003-phase1-measurement-and-baseline.md)
- [Production execution plan](./energy-baseline-production-execution.md)
- [Implementation plan](./home-battery-optimizer-implementation.md)
