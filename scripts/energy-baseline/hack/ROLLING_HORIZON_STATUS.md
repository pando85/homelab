# Rolling Horizon Implementation Status

Status document for the two-event rolling-horizon experiment code in this directory:
`src/energy_baseline/rolling_horizon.py`, `hack/compare_forecasts.py`, and their tests.

**No euro figures are recorded here.** Costs live only in private `--output` directories,
never in the repository. This file describes mechanisms and test status, not measurements.

## Current State

```
$ uv run pytest -q -o addopts=""
1201 passed

before the slot-position alignment fix: 1194 passed (+7 tests here)
  - tests/test_rolling_horizon.py:      63  (was 56)
  - tests/test_compare_forecasts_cli.py: 16  (unchanged)
$ uv run ruff check . && uv run ruff format --check .   # both clean
```

Before the day-level bias work: 1167 passed (+27 there). An earlier revision of this file
recorded "28 tests in test_rolling_horizon.py: ALL PASSING; 1149 total tests in test suite:
ALL PASSING", and listed "No synthetic CLI smoke test" as not done. **Neither description
matches the tree.** `test_rolling_horizon.py` held 36 tests before the bias work started, not
28 (20 bias tests were added since, and 36 + the rest reproduces the 1167 that `pytest`
reported at that point), and a synthetic CLI smoke test does exist — this directory's own
`tests/test_compare_forecasts_cli.py` runs `hack/compare_forecasts.py` as a subprocess against
a temporary built dataset. Corrected here rather than quietly overwritten, because a status
note whose counts are stale suppresses the next verification run. If you take one habit from
this file: paste the actual `pytest` summary line into a status note, do not describe it from
memory — and paste the count for the *current* revision, not one you measured earlier.

## What Is Implemented

### `src/energy_baseline/rolling_horizon.py`

1. **Two-event dispatcher** — planning events at 14:00 and 20:30 local (Europe/Madrid).
   Each plans from its own time to the midnight ending D+1, executes only until the next
   event, and carries the **actual** clipped SOC forward, so the next solve starts from the
   real state rather than the planned one.
2. **Price noise with two independent channels** (`generate_price_noise`) — see *Forecast
   error model* below.
3. **Frozen load forecast** (`build_frozen_load_profile`) — slot-position means learned from
   local days strictly before `training_end_idx`, applied unchanged across every evaluation
   day. Slot position is derived from the **local wall clock** (see *Slot positioning* below),
   not from offsets within the sliced array. Records training bounds, a training-window hash,
   slots learned, the inferred interval length and timezone, and the pooled-mean fallback count
   for DST offsets that were never learned. `interval_starts_utc` is **required**: without it
   there is no correct positioning and the function raises rather than guessing.
4. **Event computation** (`compute_rolling_events`) — warmup events tile whatever the window
   opens with before the first scheduled event, split at every local midnight so no plan is
   ever credited with tomorrow's prices. Guarantees `exec_end_idx <= plan_end_idx` and that
   each interval in the window is executed exactly once; violations raise rather than being
   papered over.
5. **Main dispatcher** (`run_rolling_horizon`) — reuses `solve_oracle`, `simulate` and
   `_clip_plan_to_model_bounds`. Solver/physics failures degrade that one event to recorded
   zero flows. Scoring is always against **true** prices and the actual clipped grid import.
6. **Day-ahead wrapper** (`run_day_ahead_with_frozen_forecast`) — the existing
   `policy_day_ahead` on the same frozen profile, for a like-for-like comparison.

### `hack/compare_forecasts.py`

Standalone experiment runner (matrix: windows × sigmas × bias-sigmas × seeds).

```bash
uv run python hack/compare_forecasts.py \
    --dataset /path/to/analysis/v1/<dataset_id> \
    --config config/tariff.yaml \
    --output /tmp/energy-compare/run-1 \
    --test-start 2025-10-01T00:00:00Z \
    --test-end 2026-01-01T01:00:00Z \
    --windows 28,365,all \
    --sigmas 0,0.1,0.2 \
    --bias-sigmas 0,0.1 \
    --seeds 0,1,2
```

Guards, all still in force:

- `--output` that exists and is non-empty is **refused** (a clobbered directory silently
  mixes two measurements).
- An `--output` inside a git work tree is **refused**: artifacts carry costs derived from
  private interval data and must not be committable.
- `summary.json` and every schedule are written with `allow_nan=False`.
- Only the numeric core's declared errors (`OracleError`, `BatteryPhysicsError`) degrade a
  run to a recorded failure; anything else aborts loudly with a traceback and non-zero exit.
- Dataset columns are checked finite on usable rows before any dot product is formed.
- The frozen profile is built **once per window** and shared by both policies, so a test-load
  drift cannot sneak in through a per-run recompute.

## Forecast Error Model

`generate_price_noise` builds the next-day price estimate as

```
p_hat[t] = p_true[t] * (1 + b[day(t)] + e[t])
```

with two channels, both deterministic per `(seed, local_date)`:

| Channel | Draw | Stresses |
| --- | --- | --- |
| `e[t] ~ N(0, sigma)` | once per 15-minute slot | which hours the optimiser picks — largely averages out within a day and preserves the ordering of tomorrow's hours |
| `b[day] ~ N(0, bias_sigma)` | once per local day, applied to every slot of that day | the cross-day hold-versus-spend decision — does **not** average out, and leaves the within-day ordering untouched |

Both are derived from one key, `sha256(f"{seed}:{local_date}")`, split into disjoint slices:
the per-slot stream is seeded from `digest[:8]`, the day-bias stream from `digest[8:16]`.
Separate generators, so neither channel's draw count can perturb the other.

**`bias_sigma = 0` reproduces the pre-bias model bit-for-bit.** The bias existed to stress-test
a conclusion drawn from an i.i.d.-only noise model, and that conclusion had already been
measured on disk across many configurations; if adding the second channel had perturbed the
first stream by even one consumed variate, old and new rows would describe different processes
while carrying the same sigma. Four tests pin this: two golden SHA-256 digests captured from the
pre-bias implementation (one multi-day rising series, one covering zero and negative prices), an
inline replica of the original algorithm that the shipped function must match exactly, and a
check that the bias comes from `digest[8:16]` of the same key.

Why the day-level variant exists at all: independent per-slot noise is close to harmless for a
same-day optimiser, so the i.i.d. result says nothing about a **persistent** estimation error —
the way a real day-ahead proxy errs when a weather regime or a forward component is misjudged.
`sigma=0, bias_sigma>0` isolates exactly that: a pure per-day scaling of truth with no
within-day distortion. `--bias-sigmas` is applied in the same nested loop as `--sigmas`, and the
same `(seed, date)` bias is **shared across load-window variants**, which is what keeps a window
comparison a controlled comparison. `bias_sigma` is recorded in each run record, in the schedule
filename (so two arms cannot be confused), and in telemetry; the realised per-day bias actually
applied, its mean and its standard deviation, are reported in the noise metadata alongside MAE,
MAPE and the within-10 %/20 % fractions — all still computed over **estimated slots only**, with
zero true prices excluded from the percentage denominators.

**This file records no verdict on whether the bias changes the economic conclusion.** That is a
measurement for the matrix run, not something to assert in advance or tune toward.

## Slot Positioning

`build_frozen_load_profile` learns one mean per **slot position**, and applies it to every
evaluation interval with the same position. The rule is clock-derived, not array-derived:

```
k = (t_utc − utc_instant_of_local_midnight(local_day(t))) / interval_length
```

The midnight instant comes from `datetime.combine(local_day, 00:00, tzinfo=tz)` — the **fold=0**
occurrence, i.e. the first instant whose local date is that day — and the subtraction is between
two aware instants, so it measures real elapsed time. What that yields, all pinned by tests:

| Local day shape | Positions | Notes |
| --- | --- | --- |
| normal (24 h) | 0..95 | identical to offset-within-day, so nothing re-indexed |
| **partial** (a slice edge that is not local midnight) | its true clock offsets | a day entered at 02:00 CEST starts at 8, not 0 — the defect fixed below |
| fall-back (25 h, 100 intervals) | 0..99 | repeated hour unambiguous: first 02:00 → 8, second → 12 |
| spring-forward (23 h, 92 intervals) | 0..91 | contiguous, no hole, no fallback |

Only positions 96..99 exist on no ordinary day, so a 28-day window that happens to contain no
fall-back day leaves exactly those four to the pooled mean. Positions never learned always fall
back to the pooled mean and are counted in `eval_slots_pooled_fallback`.

**Both sides use the same rule.** The training loop iterates its intervals and derives each one's
position with this identical function, so for a day present in full — which is every training day
in a trailing window except a truncated boundary day — it reproduces the offsets the means have
always been keyed by. `test_full_local_day_positions_are_unchanged_from_offset_within_run` asserts
that equality over normal *and* DST-shaped days, which is what keeps already-measured profiles
comparable instead of silently re-indexing every learned mean.

**A spring-forward consequence, stated rather than hidden.** On a 23-hour day, position 8 is
wall-clock 03:00 while position 8 on an ordinary day is 02:00, so afternoon means are applied one
hour early in wall-clock terms there. Any contiguous-position scheme does this, offsets-within-a-full-
local-day produced exactly these numbers before the fix, and so no behaviour changed — only
truncated days moved.

## Defects Found And Fixed

1. **Frozen-profile slot positions came from slice-run offsets, not the clock** — position was
   the offset within each run of *sliced* array, so any local day truncated by a slice edge was
   relabelled from 0. Verified against the real dataset: `training_end_idx = 61344` is
   `2025-10-01T00:00:00Z`, which is **02:00 CEST, not a local midnight**, so local day 2025-10-01
   appears in the evaluation slice with only its final 88 intervals. The full-array bounds of that
   day are `(61336, 61432)`, making the true position of index 61344 equal to **8**, while the run
   rule assigned 0..87. Magnitude: **88 of 8,832 holdout intervals (1.00 %)**, all in one
   contiguous region, shifted by exactly **8 positions = 2.0 hours** — every forecast on the first
   partial day was the mean of a slot two hours earlier.

   - Fixed by deriving position from the local wall clock (`_clock_slot_position`, see *Slot
     positioning*). Positions for indices 61344..61347 are now **8, 9, 10, 11**.
   - `interval_starts_utc` was optional and only fed the training hash; it is now required and a
     missing value raises `ValueError` naming it, because silently falling back to the run rule is
     what produced this. The runner already passed it and now also passes `tz=MADRID`.
   - Pinned by `test_partial_first_local_day_gets_clock_positions` (asserts positions 8..11, that
     the shift is exactly the measured 8, and that a wall-clock-only load is predicted exactly),
     `test_missing_interval_starts_is_refused`, and
     `test_strict_train_cut_holds_on_partial_first_local_day`.
   - **The trailing boundary was never broken, contrary to a previous claim.** Index 70172 is
     `2025-12-31T23:00:00Z` = 2026-01-01 00:00 CET, true position **0**, and the old rule also gave
     0 — a trailing partial day *starts* at the slice edge instead of ending mid-way through it, so
     both rules agree there. The reported "92-position" misalignment at that end is wrong;
     `test_trailing_partial_local_day_is_not_shifted` pins the correct behaviour explicitly so it
     is not "fixed" later.
   - **Not a defect:** the dataset's own leading edge (index 0 is `2024-01-01T00:00:00Z` = 01:00
     CET, so local day 2024-01-01 exists with only 92 intervals). That truncation belongs to the
     build window, not this function, and is deliberately left alone.
   - Side effect worth knowing: correcting positions made one integration fixture pass for the
     wrong reason. `test_not_bit_identical_to_day_ahead` used a load identical at each local-clock
     position of every day, so once the profile was positioned correctly it reproduced measured load
     exactly; given an exact forecast and (at sigma 0) exact prices, both policies plan the same
     dispatch and the asserted cost difference vanished. The old positional error had been *acting
     as* a forecast error. The fixture now varies the load level day to day, so its premise is real.
2. **Frozen-profile test-data leakage** — the forecast was computed as a rolling trailing mean
   over the *whole* dataset, so evaluation days were partly predicting themselves. Fixed by
   learning slot means from local days strictly before `training_end_idx` and applying the
   frozen result everywhere; pinned by `test_uses_only_prior_data`,
   `test_window_days_respected`, and `test_eval_load_change_leaves_forecast_unchanged` (editing
   an evaluation day's measured load must leave the forecast byte-identical). The runner also
   builds one profile per window rather than per run.
3. **Crash when evaluation started after the 20:30 event** — the opening warmup planned to the
   next scheduled event, which is the *following* day's 14:00, so it planned across a local
   midnight while only executing part of it: either `exec_end_idx > plan_end_idx` or the night
   was left with no plan at all. Fixed by splitting warmups at every midnight
   (`_warmup_events`), one per local day, each planning only the day it starts in with exact
   prices and no tomorrow. Pinned by `test_start_after_evening_event_splits_at_midnight`,
   `test_warmup_spanning_several_midnights`, `test_short_window_with_no_events_is_still_executed`,
   and `test_run_rolling_horizon_after_evening_event`.
4. **DST fixture cut the fall-back day in half** — the fixture started at a UTC midnight, which
   split 2025-10-26's 25-hour local day and made its interval count meaningless. Fixed by
   starting the fixture at local midnight (2025-10-24T22:00Z) so days 25/26/27 are whole; the
   long day is now asserted to carry 100 intervals and a complete noise curve, including its
   repeated hour. See `test_dst_fall_back_2025_oct_26` and
   `test_warmup_after_evening_event_on_dst_day` (DST and the after-20:30 split together).
5. **`window_days` ignored** — every window used all training days, so window variants were
   identical. Fixed; different windows now produce different profiles.
6. **Unusable `sigma` accepted** — negative or infinite sigma was silently tolerated and produced
   a meaningless run that still reported success. Now refused, in the library and at the CLI,
   with the same rules applied to `--bias-sigmas`.
7. **Blanket `except Exception` in the runner** — swallowed programming errors as "that policy
   failed". Narrowed to the declared solver/physics errors.

## Test Inventory

`tests/test_rolling_horizon.py` (63):

- Price noise, i.i.d. channel — 8
- Day-level bias: bit-compatibility with the pre-bias stream — 4
  (`test_bias_sigma_zero_reproduces_pre_bias_array`, `..._with_zeroes_and_negatives`,
  `test_pre_bias_stream_is_the_documented_derivation`,
  `test_bias_stream_is_the_other_digest_slice`)
- Day-level bias: behaviour — 13 (pure day scaling with `sigma=0`; determinism on both the
  mixed and bias-only paths; different seeds, different bias; one draw per local day rather
  than one global draw; independence from the per-slot channel; separability of the two
  streams; zeros and negatives without NaN; three invalid-`bias_sigma` cases; empty-mask
  no-NaN reporting; one-schema exact control)
- Day-level bias: shared across load-window variants — 3
  (`test_bias_identical_across_load_window_variants`,
  `test_bias_shared_through_the_dispatcher_for_different_profiles`,
  `test_bias_only_run_changes_the_plan_but_never_the_scoring`)
- Frozen load profile — 11, of which 7 pin slot positioning:

  | Test | Pins |
  | --- | --- |
  | `test_partial_first_local_day_gets_clock_positions` | positions 8..95 on a day entered at 02:00 CEST; shift is exactly 8 = 2 h; exact prediction of a wall-clock-only load elsewhere |
  | `test_trailing_partial_local_day_is_not_shifted` | trailing boundary correct at position 0 — explicitly **not** the claimed 92-position error |
  | `test_strict_train_cut_holds_on_partial_first_local_day` | means still derive only from pre-cutoff indices on a truncated-first-day slice, checked against a hand-derived window mean |
  | `test_dst_fall_back_clock_positions_run_zero_to_ninety_nine` | the 25-hour day runs 0..99; repeated 02:00 → 8 and 12; only offsets 96..99 fall back |
  | `test_dst_spring_forward_clock_positions_run_zero_to_ninety_one` | the 23-hour day runs 0..91 contiguously, no fallback |
  | `test_full_local_day_positions_are_unchanged_from_offset_within_run` | whole local days (normal and DST) keep their historic offsets, so learned means stay comparable |
  | `test_missing_interval_starts_is_refused` | omitting `interval_starts_utc` raises instead of silently using run offsets |

- Event computation — 6
- Warmup splitting — 5
- Rolling-horizon integration, including DST and not-bit-identical-to-day-ahead — 13

`tests/test_compare_forecasts_cli.py` (16) runs the real runner as a subprocess against a
synthetic on-disk dataset (manifest + parquet in `ANALYSIS_INTERVAL` shape). No network, no
credentials, no real data directory. The seven bias cases check that omitting `--bias-sigmas`
reproduces the i.i.d.-only run, that the grid multiplies the run count and is recorded per run,
that a pure day-bias arm's MAPE is reconstructible from its reported per-day biases (proof the
flag reached the model, not just the record), that schedule filenames carry `bias_sigma` and
agree with their own payloads, that invalid bias sigmas are refused at the CLI (three cases),
and that the fresh-output refusal and `allow_nan=False` strictness still hold on a bias run.

Two guards worth knowing about when extending these tests: several assert **non-vacuity** (that
the two arms of a comparison really differ) before asserting the interesting equality, because an
identical-fixture comparison passes trivially; and the day-bias fixtures deliberately use a
cheap-night/expensive-evening price shape rather than a flat level — on a fixture where every day
has the same profile, a uniform multiplicative day bias rescales everything equally and cannot
change any decision, so such a test would prove nothing while passing.

## Known Limitations

1. **Spring-forward is covered for positioning, not end to end.** The 2026-03-29 day's slot
   positions (0..91) are now pinned by `test_dst_spring_forward_clock_positions_run_zero_to_ninety_one`,
   but no dispatcher/noise run exercises a 23-hour day: only the 2025-10-26 fall-back day is rolled.
2. **No performance figures.** No timing per solve or per run has been recorded here.
3. **All tests are synthetic.** The runner has only ever been exercised end to end against a
   synthetic built dataset in these tests; the real-data matrix is run separately and its output
   stays out of the repository.
4. **Noise is synthetic, not measured.** Both channels use future truth solely to construct a
   noisy forecast. They characterise policy *sensitivity* to forecast error of a given magnitude;
   they are not an empirical accuracy figure and prove nothing about a deployable estimator.
5. **Load-forecast accuracy figures recorded for earlier revisions are stale.** The positioning fix
   changes the frozen profile on exactly the first partial holdout day, so any previously written
   per-window MAE / MAPE / within-10 %/20 % and `eval_slots_pooled_fallback` figures describe the
   mis-shifted profile. This file keeps no measurements of its own; refresh them from a re-run rather
   than carrying the old ones forward.
