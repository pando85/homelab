# Rolling Horizon Implementation Status

Status document for the rolling-horizon experiment code in this directory:
`src/energy_baseline/rolling_horizon.py`, `hack/compare_forecasts.py`, and their tests. The
dispatcher defaults to the two-event (14:00 / 20:30) schedule and can be run at any replan
cadence — see *What Is Implemented*.

**No euro figures are recorded here.** Costs live only in private `--output` directories,
never in the repository. This file describes mechanisms and test status, not measurements.

## Current State

```
$ uv run pytest -q -o addopts=""
1444 passed in 109.68s (0:01:49)

  - tests/test_rolling_horizon.py:      112  (was 110: +2, the spring-forward
                                               end-to-end cases — see *Known
                                               Limitations* item 1)
  - tests/test_compare_forecasts_cli.py: 22  (was 16: +6, `TestCliReplanCadence`
                                               subprocess cases for the cadence
                                               flags — see *Known Limitations*
                                               item 6)
$ uv run pytest -q -o addopts="" tests/test_rolling_horizon.py --collect-only
112 tests collected in 0.43s
$ uv run pytest -q -o addopts="" tests/test_compare_forecasts_cli.py --collect-only
22 tests collected in 0.03s
$ uv run ruff check . && uv run ruff format --check .   # both clean
```

The total moves under you while other work lands in the same tree: the previous revision of
this file recorded 1201 passed, the configurable-replan-cadence work then added 47 tests to
`test_rolling_horizon.py` (63 → 110), and the revision after that recorded 1436 passed with 110/16 —
all accurate for their own trees. Paste the summary line `pytest` actually printed, for
the revision you are describing — see the note below about stale counts.

**Cadence cost: two different multipliers, and both are right.** Measured on synthetic fixtures
(15-minute cadence, 24 h horizon, exact prices, HiGHS, this machine). A 92-day holdout at
`--replan-interval-slots 1 --horizon-slots 96` needs 8,832 solves against the 185 the legacy
two-publication schedule needs (92 days × 2 + 1 warmup), so the **solve-count** ratio is
8,832 / 185 ≈ **48x**. The **wall-time** ratio is smaller: ~175 s — **≈ 3 minutes per sigma/bias/seed
arm** — at cadence 1 against ~4.7 s for the legacy arm over the same holdout, i.e. ≈ **37x**. They
differ because a solve at the fine cadence is *cheaper*: ~20 ms/solve at a 96-interval horizon
(8,832 × ~20 ms ≈ 175 s) against ~24 ms/solve for the legacy schedule, which is also what the 30-day
legacy window gives (59 solves, 1.4 s). The reason is what each solve plans: a legacy event covers the
rest of its local day **and the next one** (~34 h at 14:00, up to 35 h before a fall-back), while
cadence 1 plans a fixed 96 slots, and per-solve time tracks horizon length. Wall time therefore grows
more slowly than solve count, and neither multiplier is the horizon length. Use
`--replan-interval-slots 4` (~2,208 solves) or a shorter `--time-limit` if the full matrix at 15
minutes is too expensive to run.

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

1. **Rolling dispatcher with a configurable replan cadence** — two modes over one code
   path and one publication rule.
   * *Legacy two-event schedule* (default, `replan_interval_slots=None`) — planning events
     at 14:00 and 20:30 local (Europe/Madrid). Each plans from its own time to the midnight
     ending D+1, executes only until the next event, and carries the **actual** clipped SOC
     forward, so the next solve starts from the real state rather than the planned one.
   * *Configurable cadence* (`replan_interval_slots=N`, `horizon_slots=H`) — one solve every
     `N` intervals over an `H`-interval horizon (default `H=96`, i.e. 24 h), executing only
     the next `N` intervals. `--replan-interval-slots 1 --horizon-slots 96` is the 15-minute
     controller ADR-0001 describes; `H` in 96–192 covers its 24–48 h band.
2. **Price information set is a function of the decision instant, not of the cadence** —
   `_last_published_day()` is the single statement of the rule (day D+1 exact from 20:30 on
   day D, the first quarter-hour boundary after the 20:15–20:20 publication window).
   `_prices_exact_end_for_decision()` maps that day to an interval index and BOTH modes call
   it, so the rule cannot be duplicated and drift. A 09:00 solve estimates D+1; a 21:00 solve
   does not; a 48 h horizon estimates D+2 as well.
3. **Price noise with two independent channels** (`generate_price_noise`) — see *Forecast
   error model* below.
4. **Frozen load forecast** (`build_frozen_load_profile`) — slot-position means learned from
   local days strictly before `training_end_idx`, applied unchanged across every evaluation
   day. Slot position is derived from the **local wall clock** (see *Slot positioning* below),
   not from offsets within the sliced array. Records training bounds, a training-window hash,
   slots learned, the inferred interval length and timezone, and the pooled-mean fallback count
   for DST offsets that were never learned. `interval_starts_utc` is **required**: without it
   there is no correct positioning and the function raises rather than guessing.
5. **Event computation** (`compute_rolling_events`) — in legacy mode, warmup events tile
   whatever the window opens with before the first scheduled event, split at every local
   midnight so no plan is ever credited with tomorrow's prices. In cadence mode the events
   tile the window at the requested spacing (there are no warmups: the cadence starts at
   `eval_start_idx`). Both guarantee `exec_end_idx <= plan_end_idx` and that each interval in
   the window is executed exactly once; violations raise rather than being papered over.
6. **Main dispatcher** (`run_rolling_horizon`) — reuses `solve_oracle`, `simulate` and
   `_clip_plan_to_model_bounds`. Solver/physics failures degrade that one event to recorded
   zero flows. Scoring is always against **true** prices and the actual clipped grid import.
   Reports the run's cost and its clipping interpretation (see *Cost and multiplicity
   reporting*).
7. **Day-ahead wrapper** (`run_day_ahead_with_frozen_forecast`) — the existing
   `policy_day_ahead` on the same frozen profile, for a like-for-like comparison.

### Cost and multiplicity reporting

A 15-minute cadence over a 92-day holdout is ~8,832 solves instead of ~185 — about 48x the solves and
about 37x the wall time, which are different numbers for the reason given under *Current State* — so
the cost of a run is recorded rather than discovered afterwards. Telemetry (surfaced per run in the
runner's `rolling_horizon` block, per schedule file, and in `summary.json`'s `replan_cadence`)
carries:

| Key | Meaning |
| --- | --- |
| `solve_count` | events that reached the solver — infeasible / no-incumbent / time-limit outcomes included, because they consumed the budget |
| `planning_events_not_solved` | events skipped before solving (`skipped_no_usable`, `frozen_by_seed`) |
| `replan_interval_slots`, `replan_cadence`, `horizon_slots`, `horizon_hours_nominal` | which controller the artifact describes. Legacy `horizon_slots`/`horizon_hours_nominal` are `None`: the legacy horizon is DST-dependent (34 h at 14:00, 35 h before a fall-back, 27.5 h at 20:30) and a single number would be fabricated |
| `executed_multiplicity_mean` | always `1.0` — each interval executes once, enforced by the coverage check |
| `planning_multiplicity_mean`, `planning_multiplicity_max` | how many overlapping plan horizons covered an interval. Measured on synthetic windows: 2.52 mean / 4 max at the legacy cadence (the same quantity as the ~2.55 measured on the real holdout and recorded in `planning/energy-baseline-forecast-experiments.md`), 94.4 mean / 96 max at `slots=1, horizon=96`, 10.6 mean / 12 max at `slots=8, horizon=96` in the runner's own smoke dataset. Steady state is `horizon_slots / replan_interval_slots`, pulled down only by the clipped horizons at the window edges |
| `planned_interval_slots_total`, `executed_interval_slots_total` | the same two as sums |
| `clipping_totals_comparable_across_cadences` | always `False`, with `clipping_note` saying why |

`charge_clipped_kwh` and `discharge_clipped_kwh` are **per-plan** quantities: an interval is
counted in every horizon that planned it, so at a fine cadence the totals grow with the
overlap, not with any extra curtailed energy. They are not curtailment and not comparable
across cadences; the executed flows and `cost_eur` are. Per-event records now also carry
`prices_exact_end_idx` and `estimated_price_intervals`, so the information set of each solve
is readable from the artifact instead of having to be re-derived.

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

# ADR-0001's 15-minute controller, over the same holdout: ~48x the legacy arm's solves
# (96 solves per local day against its ~2), and ~37x its wall time
uv run python hack/compare_forecasts.py \
    ... --replan-interval-slots 1 --horizon-slots 96 --time-limit 2
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
- `--replan-interval-slots` and `--horizon-slots` must be `>= 1`; `--horizon-slots` **without**
  `--replan-interval-slots` is refused, because the legacy schedule's horizon comes from the
  local-day partition and a slot count handed to it would be silently ignored.
- The chosen cadence is printed **before** the matrix starts (with the solve count it implies),
  and the per-run solve count plus planning multiplicity is a column in the console table: an arm
  that costs ~48x the solves (~37x the wall time) must not be discoverable only after it has
  run.
- Omitting both flags keeps the exact legacy behaviour, including the exact price-noise stream
  (see *Bit-identical legacy schedule*).

## Bit-identical legacy schedule

Adding the cadence parameters must not have moved the ~185-solve results already measured. Two
things pin that, in `tests/test_rolling_horizon.py`:

1. **`TestLegacyCadenceGolden`** — literal golden values captured by running the implementation
   at `git HEAD` (before `replan_interval_slots`/`horizon_slots` existed) on two fixtures: the
   cheap-night/expensive-evening 5-day shape on a partial opening local day, and the whole
   2025-10-25/26/27 fall-back weekend. Pinned per fixture: the SHA-256 of the executed
   charge/discharge/SOC/grid arrays (big-endian float64, as `_golden_digest` encodes it), the
   cost, EFC, terminal SOC, the event counters, the clipping totals, the noise metadata's
   `stats_slots_considered`/MAPE/sign-flips/applied-bias, the SHA-256 of the whole event table
   (index, local day, kind, start, plan end, exec end, prices-exact end) and the frozen-profile
   digest. Four noise arms per fixture (`sigma=0` control, `sigma=0.2` at two seeds, a
   `bias_sigma=0.2` arm), and a non-vacuity test that those arms really have *different* digests
   — a pin that holds under "nothing moved" would not catch a re-seeded noise stream.
2. **Omitted vs explicit `None`** — `test_default_run_matches_explicit_none_run` and
   `test_event_schedule_matches_golden` also assert that passing `replan_interval_slots=None,
   horizon_slots=None` is the same run as passing neither, so the default is the legacy schedule
   itself and not a near-equivalent new path.

Where a refactor risks touching the shared publication rule, the rule is checked rather than
assumed: `TestPublicationRuleAtEveryCadence` derives "which days did this solve estimate" from
the ADR sentence (D+1 published in the 20:15–20:20 window on D, treated as available from 20:30)
and compares it against the implementation's `prices_exact_end_idx` for all 96 quarter-hours of a
local day, for `cadence in (1, 4, 24, 96)` at a fixed decision instant, and for the two
quarter-hours that straddle the boundary (20:15 and 20:30).

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

`tests/test_rolling_horizon.py` (112):

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
- **Legacy-cadence golden values — 14** (`TestLegacyCadenceGolden`): executed
  charge/discharge/SOC/grid digests + cost + EFC + terminal SOC + event counters + clipping
  totals + noise metadata for 2 fixtures × 4 noise arms (8 cases), the event-table digest and
  the omitted-vs-explicit-`None` equivalence for each fixture (2), the frozen-profile digest
  for each fixture (2), one non-vacuity test that the pinned arms really differ (1), and the
  default-vs-`None` dispatcher equivalence (1)
- **Publication rule at every cadence — 7** (`TestPublicationRuleAtEveryCadence`): all 96
  quarter-hours of a local day swept at a 48 h horizon against an independently stated
  expectation of which days are estimated (1); the 20:15/20:30 straddle of the publication
  window (1); same-decision-instant equivalence between the legacy 14:00 solve and cadences
  1/4/24/96 (4); and "no interval is priced exact unless its day was published, and none is
  estimated that was" for every event of a cadence run (1)
- **Cadence dispatch — 20** (`TestReplanCadenceDispatch`): 15-minute cadence end to end
  (coverage, SOC/power/export bounds, one solve per interval); the same over the 100-interval
  2025-10-26 fall-back day; **the 92-interval 2026-03-29 spring-forward day end to end at both
  cadences** — `test_legacy_cadence_over_a_dst_spring_forward_day` (the two-publication schedule
  still tiles a short day, with no solve planned across its midnight) and
  `test_15_minute_cadence_over_a_dst_spring_forward_day` (a slot-counted cadence counts intervals, so
  the 23-hour day gets 92 solves, not the 96 a wall-clock reading would imply) — closing limitation 1
  below; coarser cadences tiling 2/4/12/96; `exec_end` clamped to
  `plan_end` when the cadence is coarser than the horizon (planning multiplicity exactly 1
  there); horizon length configurable at 24/48/96/144/192 slots with the planned length
  asserted per solve (5); the 24 h default inside ADR-0001's band; solve count reported and
  scaling with cadence; planned-versus-executed multiplicity reported and scaling; the legacy
  configuration reporting the documented ~2.5 multiplicity and max 4; invalid cadence and
  invalid horizon rejected at both the event builder and the dispatcher (4); and the frozen
  forecast's price/plan inputs unchanged under a post-cutoff measured-load rewrite
- **Cadence invariants — 8** (`TestReplanCadenceInvariants`): carried SOC is the measured
  post-clipping value at each event boundary, not the plan's; the seed floor is one constant
  across the whole roll; a failed solve holds SOC, records its note, contributes zero flows
  and still tiles coverage; planning terminal value stays out of the scored cost; no
  wall-clock or timing key enters the artifact; `--time-limit` reaches every solve; same seed
  same run; and non-vacuity that a fine cadence is a *different* dispatch than the legacy
  schedule

`tests/test_compare_forecasts_cli.py` (22) runs the real runner as a subprocess against a
synthetic on-disk dataset (manifest + parquet in `ANALYSIS_INTERVAL` shape). No network, no
credentials, no real data directory. The seven bias cases check that omitting `--bias-sigmas`
reproduces the i.i.d.-only run, that the grid multiplies the run count and is recorded per run,
that a pure day-bias arm's MAPE is reconstructible from its reported per-day biases (proof the
flag reached the model, not just the record), that schedule filenames carry `bias_sigma` and
agree with their own payloads, that invalid bias sigmas are refused at the CLI (three cases),
and that the fresh-output refusal and `allow_nan=False` strictness still hold on a bias run.

**Six more cases were added on 2026-10-24** (`TestCliReplanCadence`), closing limitation 6 below: the
cadence flags reach `summary.json`'s `replan_cadence` *and* the per-schedule artifacts (slot count,
horizon, the nominal cadence description, the price-information rule, and the observed solve counts);
omitting both flags still reports the legacy schedule as its own cadence rather than as an absent
one; a cadence run is still scored on **true** prices, so a finer cadence changes when plans are made
and never what they are scored on; and the three refusals are exercised through the real CLI —
cadence below one interval, horizon below one interval, and a horizon with no cadence.

Two guards worth knowing about when extending these tests: several assert **non-vacuity** (that
the two arms of a comparison really differ) before asserting the interesting equality, because an
identical-fixture comparison passes trivially; and the day-bias fixtures deliberately use a
cheap-night/expensive-evening price shape rather than a flat level — on a fixture where every day
has the same profile, a uniform multiplicative day bias rescales everything equally and cannot
change any decision, so such a test would prove nothing while passing.

## Known Limitations

1. **Spring-forward is covered for positioning, not end to end.** — **closed 2026-10-24; the claim is
   kept visible rather than deleted, because a limitation note that quietly disappears hides the fact
   that it was ever a gap.** The 2026-03-29 day's slot
   positions (0..91) are now pinned by `test_dst_spring_forward_clock_positions_run_zero_to_ninety_one`,
   but no dispatcher/noise run exercises a 23-hour day: only the 2025-10-26 fall-back day is rolled,
   at both the legacy cadence (`test_dst_fall_back_2025_oct_26`) and a 15-minute one
   (`test_15_minute_cadence_over_a_dst_fall_back_day`). A 23-hour day under a slot-counted cadence is
   therefore untested — the horizon arithmetic is interval-counted so it should be exact, but "should"
   is what the fall-back case was before it was written.
   **What closed it:** two end-to-end dispatcher runs over the whole spring-forward weekend, at
   **both** cadences — `test_legacy_cadence_over_a_dst_spring_forward_day` and
   `test_15_minute_cadence_over_a_dst_spring_forward_day`, listed under *Test Inventory*. The short
   day is now rolled through the dispatcher, with `event_count` and `solve_count` both exactly 92: the
   cadence counts intervals, so a 23-hour day gets 92 solves and not the 96 a wall-clock reading would
   imply. The two directions fail differently, which is why both are pinned: a 25-hour day tests
   whether the repeated hour is planned twice, a 23-hour day tests whether a horizon counted in
   intervals still reaches the next local midnight when the day is short.
2. **No performance figures for the real dataset.** Per-solve timings for the synthetic fixtures are
   in *Current State* (they are needed to price a cadence change before running it); nothing here
   measures the real holdout, and no timing ever enters an artifact.
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
6. **The cadence flags are not covered by the CLI smoke test.** — **closed 2026-10-24; the claim is
   kept visible rather than deleted.** `--replan-interval-slots` and
   `--horizon-slots` were verified by running the runner as a subprocess against a synthetic
   built dataset by hand (legacy arm: 9 solves, multiplicity 2.21; cadence arm at slots=8,
   horizon=96: 48 solves, multiplicity 10.63, both recorded in `summary.json`), but the cases
   themselves live in `tests/test_compare_forecasts_cli.py`, which is not this work's file to
   edit. Adding them there is the follow-up.
   **The follow-up landed:** `TestCliReplanCadence` adds six subprocess cases to that file, listed
   under *Test Inventory*, so the flags are now covered by the suite rather than by a hand run — and
   that file's count went from 16 to 22 accordingly.
7. **Only the executed dispatch is pinned bit-identically, not the telemetry key set.** The
   cadence work deliberately *adds* artifact fields (`solve_count`, the cadence parameters, the
   multiplicity block, per-event `prices_exact_end_idx` / `estimated_price_intervals`), so a
   golden row's JSON is not byte-comparable with an artifact written before it. The goldens pin
   the quantities that must not move — event geometry, executed flows, cost, clipping totals,
   noise statistics — which is what "same behaviour" means here.
8. **Performance figures are synthetic, measured on this machine.** The ~20 ms/solve and ~3
   minutes-per-arm numbers in *Current State* come from small synthetic fixtures, not from the
   real holdout; treat them as an extrapolation base, and re-measure the first real
   fine-cadence run before committing to a full matrix at that cadence.
