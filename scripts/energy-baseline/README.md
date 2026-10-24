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
| `ha-states`    | **functional** | Read Home Assistant enum state history (read-side, deduplicated, as-of) |
| `mirror`       | deferred     | Deferred — not implemented in Phase 1          |

**A module with no CLI surface, deliberately:** `dispatch.py` is the dispatch **intent contract**
(ADR-0001's `planner → intent → writer` seam, TODO-4.6) — typed, validated, canonically serialized
`DispatchIntent` / `DispatchPlan` values carrying interval, mode (`idle`/`charge`/`discharge`), target
power, SOC target, expected price, confidence, generation instant, expiry and sequence. Its clock is a
**required caller argument with no default**, so there is no fallback path to a wall clock (an AST test
proves the module contains no clock call and no `time`/`timeit` import — a guard against future edits,
not only against its author), physics is delegated to `battery.py`, the import ceiling stays an
**observation** rather than a rejection, and `confidence` / `expected_buy_price_eur_per_kwh` are optional
with **absence distinguishable from a value**. No writer, no I/O and no subcommand were built: the contract
only has to be capable of supporting them, and this package still actuates nothing.

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

Re-running an overlapping or identical window is safe for the HA source: rows
already in the store are not written again, and the manifest discloses how many
were suppressed. See [Overlapping re-extraction is deduplicated at row
level](#overlapping-re-extraction-is-deduplicated-at-row-level).

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
  --time-limit-s 600.0
```

**`--time-limit-s 600.0` in the example is deliberate, not decoration.** ADR-0003 §*Benchmark
methodology clarifications* records what a full-year horizon actually costs: at the default 120 s the
oracle terminated as a time-limited incumbent — `solver_status="time_limit"`, `bound_kind="upper"`,
`is_certified=False` — so its saving is only a lower bound, while 600 s on the same horizon certified
within tolerance and could be reported as a proven optimum. `benchmark.py` still pins
`DEFAULT_TIME_LIMIT_S = 120.0`, so any run that wants a certified ceiling must pass this flag
explicitly. The day-ahead rung is a separate knob: `--day-ahead-time-limit-s` is per **local day**
(default 5.0 s), not per horizon, and the run records which one a number came from.

`--train-end` is **required with no default**: thresholds are fitted on the earlier split and
frozen before evaluation, so the cut must be declared explicitly and is never inherited. Optional
flags (`--train-days`, `--charge-pct`, `--discharge-pct`, `--initial-soc-frac`, `--import-limit-kw`,
`--min-usable-fraction`, `--time-limit-s`, `--mip-rel-gap`, `--mip-abs-gap`,
`--allow-reconstructed-quality`, `--terminal-mode`, `--day-ahead-forecast-window-days`,
`--day-ahead-terminal-value`, `--day-ahead-time-limit-s`,
`--rolling-horizon-replan-interval-slots`, `--rolling-horizon-horizon-slots`, `--rolling-horizon-sigma`,
`--rolling-horizon-bias-sigma`, `--rolling-horizon-seed`, `--rolling-horizon-time-limit-s`) default to the
benchmark module's constants. The CLI calls `core_available()` first and refuses with a clear message if
the numeric core (battery / policies / oracle / cost) is not usable.

**A full-year training window is 366 days, not 365.** When the split is meant to cover all of a
leap year — e.g. `--train-end 2025-01-01T00:00:00+00:00 --train-days N` for calendar 2024 —
`N` must be **366**, not 365: a leap year has 366 days and 365 excludes 2024-01-01. This applies
only to the day's *date* boundary; the day-ahead partition respects local Madrid calendar days
regardless. A UTC-midnight `--train-end` is also not a Madrid-local-year boundary (Madrid is
UTC+1 or UTC+2, so the local year turns at 23:00Z the day before); if a "full calendar year" is
being claimed, the claim is UTC-scoped and the report must say so.

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

**Battery assumptions — what is confirmed, what is assumed, and what is pinned.** Only two figures in
the `battery:` block are confirmed hardware facts: `capacity_kwh: 5.12` (nameplate) and `power_kw: 2.5`
(max AC power). `import_limit_kw: 5.0` is user-confirmed from the contract, never inferred from sensor
data. Everything else in the block is a labelled assumption — most importantly `round_trip_efficiency`,
which since 2026-10-24 ships **0.80**, the bottom of the community-measured 0.80–0.84 range, adopted on
the reasoning that at 2.5 kW the unit operates mostly near its power rating where efficiency is
typically worst. It supersedes the previous provisional 0.88 and is **still not a measurement of this
household's unit**: the efficiency-versus-power curve is unmeasured, and idle draw is assumed zero and
unmeasured. Because the block is mapped to one-way efficiencies by a symmetric square root, the kernel
now charges and discharges at `sqrt(0.80) = 0.89443` per direction, replacing `sqrt(0.88) = 0.93808`.
Two consequences follow, and both are why the whole block is pinned by
`tests/test_benchmark.py::TestResolveBatteryAssumptions::test_shipped_tariff_battery_assumptions_are_pinned`:

- **A silent edit to efficiency, capacity, the SOC window or the import ceiling moves every reported
  figure while the test suite stays green** — and re-mints `dataset_id`, because `config_hash` feeds
  `compute_dataset_id`. The 0.88 → 0.80 change did exactly that, and passing silently is how the gap was
  found. The test is verified non-vacuous: it fails at the old value. It also pins the *framing*, so
  changing a number cannot promote an assumption to a measurement — `idle_losses_measured` stays `false`.
- **Every dataset built at 0.88 now fails the config drift check above, by design**, and every benchmark
  figure computed at 0.88 is an optimistic superseded result — including the accepted authoritative run
  and the forecast-experiment grid. `config_hash` moved from
  `41fdc30b926c099be2cbef201db4141244d82faaa80b9907c7530354f64bf182` to
  `7a901801b4148233e232fb86d534c4b7f626ec32af8e7279ad2f9e77831687d1`. The rebuild at 0.80 **has run**:
  dataset `2e01620ba72777ee8c25e5d8967151c095d2d5371450b85aecdbf7a8da69afd9` (70,176 rows, analysis
  content byte-identical to its 0.88 predecessor), and the five-rung ladder has been re-run on it
  (run `f816306cb8ad46afbb49a77e73e5ff1a`, oracle certified). Rebuild *after* committing:
  `code_version()` is the git HEAD of a clean tree and also feeds `dataset_id`, so every commit
  re-mints the identity and a pre-commit build is invalidated by the very commit it waits for.

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

**Policy set — five rungs, one per information level.** The evaluation window is scored by
`no_battery`, `causal_price`, `day_ahead`, `rolling_horizon` and `oracle` (ADR-0003
§"Strategy comparison"; the day-ahead rung was previously "considered and deferred" in
`planning/energy-baseline-appdaemon-baseline-context.md` and is now built, and `rolling_horizon` was
promoted from an offline experiment to a canonical rung on 2026-10-24 — the ladder was four rungs until
then, and `POLICY_NAMES` is now `('no_battery', 'causal_price', 'day_ahead', 'rolling_horizon', 'oracle')`,
which is also what `--policies all` runs):

| Policy | Prices known | Load known |
|---|---|---|
| `no_battery` | none — measured history is the counterfactual | none |
| `causal_price` | none (percentile thresholds frozen on the train split) | none |
| `day_ahead` | **the day's 24 h, at local midnight** | **a strictly causal forecast** |
| `rolling_horizon` | today's 24 h from 20:30 **the evening before**, and tomorrow's from 20:30 today — the same publication the day-ahead rung waits for at midnight, in hand earlier, over a horizon that runs past the local midnight `day_ahead` stops at | **a strictly causal forecast** (the same profile mechanism) |
| `oracle` | whole horizon, perfect | whole horizon, perfect |

`rolling_horizon` sits **between** `day_ahead` and `oracle` because it is strictly more informed than
`day_ahead` and still has to forecast its load. The publication rule is stated once, in one function, and
derived from the decision instant — prices for local day D+1 become exact at 20:30 on day D — so a 09:00
solve estimates D+1 and a 21:00 solve does not, at **any** cadence. That single statement is what keeps
the information set from drifting between the two cadences. The rung re-solves on a schedule, executes only
up to the next event, and carries the **actually reached** SOC forward, so the next solve starts from the
real state rather than the planned one. Its flags, canonical defaults, cadence costs and dominance rules
are under *The rolling-horizon rung* below.

The causal rule captured a known share of the oracle's optimum, but the oracle also knows future
*load*, so the unreachable remainder conflated two different things. `day_ahead` separates them:
whatever it recovers is value reachable from information that is genuinely published for free, and
what remains is value that needs load foresight nobody has. That split —
`comparison.day_ahead_closes_gap_fraction` — is the number a build/no-build decision should read,
not the causal→oracle gap on its own. The fifth rung adds the matching measurement,
`comparison.rolling_horizon_closes_gap_fraction`, alongside `rolling_horizon_minus_day_ahead_eur` and
`rolling_horizon_minus_causal_eur`. Those differences are reported **without an expected sign**:
`rolling_horizon_minus_day_ahead_eur` is `rolling_horizon − day_ahead` on the shared basis, so a negative
value means the better-informed rung came out cheaper and a positive one means it did not — either is the
result, not a defect. The run also carries
`comparison.rolling_horizon_vs_day_ahead_observation`, which records the 81-of-81 empirical dominance as
"strong empirical evidence, not a mathematical proof" and explains why no guarantee is available.

The day-ahead price assumption is **not** self-certifying; it rests on this household's own
deployed automation, verified from source: `apps/home-assistant/appdaemon/apps/climate_control.py:45`
schedules `run_daily(..., "00:00:40")` and lines 73-91 of the same file read
`pvpc["attributes"][f"price_{i:02d}h"]` for `i in range(24)` off `sensor.esios_pvpc`
(`apps/home-assistant/appdaemon/apps/apps.yaml:10`), re-labelling them onto the current local day
(line 85); `apps/home-assistant/appdaemon/apps/dhw_control.py:48` does the same at `00:00:30`. A full
day's curve is therefore in hand at local midnight in production today. What the pipeline still
cannot verify is the *archival* half of ADR-0003 §*Policy design*: the store keeps the price realised in each
hour, not a midnight snapshot of the attribute dict, so `day_ahead` assumes published-in-advance
equals realised-and-archived. The artifact's own disclosure calls that assumption "true by construction
for day-ahead PVPC, but not measured here" — the first half describes how the tariff is designed to
publish, not what this archive holds. The pipeline's inability to verify it is **unchanged**; what changed
on 2026-10-24 is the *status of the gap*. ADR-0003 §*Open validation item: day-ahead publication versus
realised archive* records the **owner assertion** (`user_assertion`, `asserted_on: "2026-10-24"`,
`verified_from_data: false`) that the 20:15–20:20 publication on D−1 describes day D and is the realised
curve, which resolves the assumption as an owner-stated fact and closes the day-boundary convention
(previously TODO-0.4), and makes the midnight attribute-dict snapshot an optional confirmation rather than
a blocking item. An owner assertion is not a measurement, so nothing here should be read as the archive
having been checked.

The reliance is still recorded in every run artifact under
`day_ahead.information_set.assumptions_disclosed` instead of being quietly relied on, and it claims no
**direction of error** — nor does this README. A wrong midnight curve would change which decisions the
policy makes, not the prices those decisions are costed on, because the plan is re-scored against the
same archived realised prices every other policy is scored on; so divergence can hurt or help, its
expected direction is adverse, and that is an expectation, not a bound. Establishing a direction needs a
midnight snapshot to compare against, which is the cheap fix ADR-0003 keeps open (snapshot
`sensor.esios_pvpc`'s attribute dict at `00:00:40` daily).

Mechanically the policy is a **rolling daily MILP**: the evaluation window is partitioned on
`Europe/Madrid` calendar days (never UTC days — the tariff is hourly in local time, so a
spring-forward day is 92 intervals and a fall-back day 100, each in exactly one solve), one
`scipy.optimize.milp`/HiGHS solve per day through the *same* `oracle.solve_oracle` assembly the
ceiling uses, and the actual end-of-day SOC rolled forward as the next day's initial condition. The
load forecast is a trailing mean of the same slot position over the previous
`--day-ahead-forecast-window-days` days (`default 28`), reading only intervals strictly before the
target day; a slot with no prior sample falls back to the pooled window mean, and the first local day
of a dataset forecasts zero — the ladder is recorded in `day_ahead.forecast.fallback_rule`, never
applied silently. The forecast constrains decisions and **never** enters cost: every euro is
re-computed against the measured load, and a plan the measured load cannot absorb is clipped to it
(discharge beyond measured load would export; charge beyond measured-load headroom would breach the
ceiling) with the clipped energy reported.

`day_ahead` is **myopic by construction** and that is disclosed rather than tuned away: a daily solve
cannot see tomorrow, so end-of-day SOC is credited at `--day-ahead-terminal-value` EUR/kWh — the
policy's only cross-day coupling, per ADR-0001's "preserve a terminal SOC value … instead of
artificially emptying the battery at midnight". Omitted, the value is the mean price of the frozen
training window and the artifact records provenance `default:training_window_mean_price`; passing a
number records it as `cli`. `0.0` is a legal, documented choice (pure cash flow, battery emptied by
design). Note what a low terminal value does *not* mean: because charging only pays off once the
credit covers the charge, valuing storage far below the cheap-hours price is not conservatism, it is
a disabled battery.

**Two terminal values, not one (a withdrawn claim recorded because it was the first draft).**
An earlier version of `day_ahead` used one value — the internal training-window mean V — for
*both* the daily planning and the reported `cost_eur`, while the oracle used 0.0 for its
reported cost. The two policies were therefore being scored on different terminal bases, which
made the headline comparison and `oracle_dominates_day_ahead` meaningless: the day-ahead policy
was receiving a credit for its ending SOC that the oracle wasn't, and the credit could be worth
more than the entire causal→day-ahead delta it was meant to measure. Fixing this required
separating the two numbers:

- **`terminal_value_eur_per_kwh`** (V, set by `--day-ahead-terminal-value` or defaulted to the
  training-window mean): used *inside* each daily objective so the myopic daily solve has a
  reason to keep useful energy overnight. Recorded in the artifact as
  `day_ahead.terminal_value.eur_per_kwh` and as `internal_planning_value_eur_per_kwh` in
  `assumptions.terminal_accounting.day_ahead`. The sum of the daily objectives under V is
  recorded as `day_ahead.internal_daily_objective_sum_eur` with an explicit note that it is
  **not** the common score.
- **`scoring_terminal_value_eur_per_kwh`** (default `0.0`, threaded from
  `_oracle_kwargs["terminal_value_eur_per_kwh"]`): what the returned Schedule's `cost_eur` and
  `inventory_adjustment_eur` use, and the same number the oracle is scored on. Recorded as
  `policies.day_ahead.terminal_value_eur_per_kwh` (which is what `BENCHMARK_RESULT` and the
  per-policy JSON record expose) and as `day_ahead.scoring_terminal_value_eur_per_kwh`.

With V ≠ 0 and a positive final SOC, the internal sum and the reported cost differ by exactly
`V × (terminal - initial)` — the credit the daily objectives gave stored energy. The reported
number is the metered cost on the shared scoring basis. This is what makes
`oracle ≤ day_ahead` a real dominance rather than a bookkeeping artefact, and it is pinned
by `tests/test_benchmark.py::TestDayAheadScoringValue` and the V=0.4 case in
`TestDayAheadPolicy.test_cost_is_meter_plus_telescoped_inventory`.

**Seed protection is horizon-level, not day-level.** The charge the evaluation window was *handed*
is not any policy's to spend; everything bought after the window opened is, including across a day
boundary. A first draft of `day_ahead` floored each daily solve at that day's own opening SOC, which
silently re-classified yesterday's purchased charge as unspendable seed: on a synthetic fixture the
battery filled on day one, froze at the ceiling for the rest of the window, and lost to the causal
rule while holding 4.6 kWh it was forbidden to use. The floor is now fixed at
`max(soc_min, initial SOC)` for the whole roll — the same floor `causal_price` and the free `oracle`
use, which is what keeps all five policies on one common feasible set and what makes
`oracle ≤ day_ahead` a provable relation rather than a hope. Supporting it needed one defaulted
parameter on `oracle.build_model`/`solve_oracle` (`floor_kwh`); with the default the certified model
is byte-identical, pinned by
`tests/test_oracle.py::TestDayAheadReusesOracleAssembly::test_default_floor_reproduces_the_certified_model_exactly`.
A spec whose opening seed already fills the SOC window has no headroom in either direction: those days
are reported as `frozen_by_seed_at_ceiling` with zero flows instead of being sent to the solver as an
invalid spec.

One day's MILP failing does **not** abort the run: that day holds SOC, charges nothing, is flagged in
`day_ahead.per_day[].degradation`, and the counts surface in `day_ahead.days_infeasible` /
`days_no_incumbent` / `days_skipped_no_usable_intervals` / `days_without_headroom`. The oracle's
answer is never substituted.

A forecast-derived plan can ask for more than the measured load allows — discharge beyond it would
export, charge beyond the measured-load headroom would breach the ceiling — so the plan is clipped to
the measured constraints in chronological order and the refused energy is reported as
`charge_clipped_kwh` / `discharge_clipped_kwh` with `days_with_clipping`. Clipping only ever reduces
a flow, never re-plans one: a wrong forecast costs dispatch quality and is disclosed, it does not
buy feasibility.

Ordering is asserted only where it is provable. Oracle ≤ baseline and oracle ≤ causal hold as before.
Oracle ≤ day-ahead is guaranteed when the oracle is a *certified* optimum **and**
`terminal_mode_used == "free"`: the free oracle minimises over exactly the feasible set that
contains the day-ahead plan (same SOC floor `max(soc_min, initial)`, same physics, same endpoint
freedom, and the same scoring terminal value, now 0.0 by default), so `oracle.cost_eur ≤
day_ahead.cost_eur` is arithmetic. On a time-limited incumbent the oracle reports its own best
feasible cost and the comparison is empirical — hence the separate
`comparison.oracle_dominates_day_ahead_guaranteed` flag. **When `terminal_mode_used` is `"fixed"`
the guarantee is `False` by construction:** the fixed oracle pins final SOC to the initial value,
which may exclude the day-ahead plan's actual final SOC, so the day-ahead plan is not in the
oracle's feasible set and no dominance is provable even if the empirical comparison happens to
hold. Do not assert fixed dominance; a `terminal_mode=fixed` run with a day-ahead plan ending
above its seed is the canonical counterexample the test suite pins
(`TestDayAheadScoringValue::test_fixed_terminal_mode_does_not_guarantee_day_ahead_dominance`).
**Day-ahead versus causal is guaranteed in NEITHER direction** and is never asserted: a myopic
optimiser with a poor forecast and a badly chosen terminal value can lose to a dumb threshold
rule. The difference is reported as `comparison.day_ahead_minus_causal_eur`, and if it is
positive, that is the result, not a defect.

**The rolling-horizon rung (`rolling_horizon`) — a fifth policy with a configurable replan cadence.**
The rung re-solves on a schedule, executes only up to the next event, and carries the **actually reached**
SOC forward, so the next solve starts from the real state rather than the planned one. Physics are reached
only through the same kernel the other rungs use, and its scoring terminal value is the same shared 0.0
threaded from the oracle's own keyword arguments — the fifth rung is scored on the common objective, not on
one of its own.

Canonical defaults, all verified:

| Knob | Default | Why that default |
|---|---|---|
| `--rolling-horizon-sigma` | `0.0` | The ladder compares *information*, not synthetic forecast error; every other rung sees exact prices, so a noisy fifth rung would not be comparable |
| `--rolling-horizon-bias-sigma` | `0.0` | Same reason |
| `--rolling-horizon-seed` | `0` | Only matters with noise; noise stays an experiment reachable through flags |
| `--rolling-horizon-replan-interval-slots` | unset (legacy two-publication 14:00 / 20:30 schedule) | The configuration the golden-value tests pin bit-for-bit and the one validated on real data |
| `--rolling-horizon-horizon-slots` | unset with the legacy cadence; `96` when a cadence is given | The legacy horizon is DST-dependent and derived from the local-day partition, so a single recorded number would be fabricated |
| `--rolling-horizon-time-limit-s` | the day-ahead per-solve limit | Per **solve**, like the day-ahead's per-day limit, and unlike the whole-horizon `--time-limit-s` |

**Cadence, and what it costs.** `--rolling-horizon-replan-interval-slots 1` with
`--rolling-horizon-horizon-slots 96` is ADR-0001 §3's controller: re-solve every 15 minutes over a rolling
24-hour horizon (96–192 slots covers its 24–48 h band), execute only the current interval. **It costs
roughly 48× the solve count** — 8,832 solves against the 185 the legacy two-publication schedule needs
over a 92-day holdout — **and roughly 37× the wall time** (~175 s against ~4.7 s per arm, both measured on
synthetic fixtures). The two multipliers differ because a fine-cadence solve is *cheaper* than a legacy one
(~20 ms/solve against ~24 ms/solve): each legacy event plans the rest of its local day *and the next one*
(~34 h at 14:00), while cadence 1 plans a fixed 96 slots, and per-solve time tracks horizon length. So wall
time grows more slowly than solve count, and neither ratio is the horizon length. It has **not** been
validated on real data; the legacy cadence has. Legacy
behaviour is not merely unchanged but **bit-identical**, pinned by golden digests extracted from `HEAD` with
`git show` and pinned as literals — digests of the charge/discharge/SOC/grid arrays, exact cost, EFC, terminal
SOC, event and clipping counts, noise statistics, plus digests of the whole event table and the frozen
profile — over two fixtures (one with a partial opening local day, one covering the 2025-10-26 fall-back
weekend) at four noise arms, with non-vacuity asserted (the control and two seeds give three different
charge digests).

**Two honesty properties the artifact reports; do not drop or reinterpret them.**

- `executed_multiplicity_mean` is exactly **1.0** while `planning_multiplicity_mean` is much higher —
  about **94.4** at cadence 1 over a 96-slot horizon, about **2.5** at the legacy cadence. An interval is
  *planned* by every overlapping horizon and *executed* once. **Clipping totals are therefore not
  comparable across cadences and are not curtailment**: they count repeated planning of the same
  intervals, not extra refused energy. The artifact says so itself: `clipping_totals_comparable_across_cadences`
  is `false`, with a note explaining why.
- `solve_count` is recorded per run, so a run's compute cost is visible in its own artifact instead of
  being discovered afterwards.

**Dominance is asserted only where provable.** `comparison.oracle_dominates_rolling_horizon_guaranteed`
follows the existing rule exactly: `oracle ≤ rolling_horizon` is guaranteed only when the oracle is
**certified** *and* scored in `free` terminal mode, because only then does the oracle minimise over the
feasible set that contains this plan; under `fixed` it is `False` by construction. The relationship the
fifth rung is supposed to demonstrate is **not** asserted: `comparison.rolling_horizon_may_lose_day_ahead`
is recorded as **`true`**, together with `rolling_horizon_may_lose_causal` and `rolling_horizon_may_lose_baseline`.
That `rolling_horizon ≤ day_ahead` held in **81 of 81** offline configurations is strong empirical evidence
**within that configuration**, not a proof — it is recorded as an observation about that experiment,
whose figures are themselves superseded (see the efficiency note below). All 81 configurations were a
Q4 holdout immediately following the training cut; that scope does not transfer to evaluation windows
that extrapolate far beyond the cut, and a measured counterexample now exists: on the 2025 full-year
ladder the same rung cost MORE than `day_ahead`. The identified asymmetry sufficient to explain the
reversal is the load-forecast basis: this rung's profile is frozen at the training cut while
`day_ahead` re-learns before each dispatch day. Re-running the identical Q4-2025 window on the same
dataset reproduced the win, which rules out the efficiency correction as the cause. Cost consistency (`cost_eur == meter_cost_eur`, inventory adjustment 0) is a
loop over whichever policies ran, so the fifth rung is covered by extension rather than duplication.

**Invalid input is refused, in one line, with exit 1 and no traceback.** All five paths were verified:
cadence < 1; horizon < 1; `--rolling-horizon-horizon-slots` given **without** a cadence — refused on purpose,
because the legacy schedule derives its horizon from the local-day partition, so accepting the flag would
emit an artifact claiming a horizon the run never used; sigma < 0 or non-finite; bias sigma < 0 or non-finite.

Per-day solve **wall-clock** times are deliberately absent from the artifacts: runs are
content-addressed and written no-clobber (`write_part_atomic` refuses the same run id with different
bytes), so a timing field would make an identical re-run fail and would destroy byte-level
reproducibility. Deterministic per-day telemetry (`solver_status`, `is_certified`, `mip_gap`, the
count of days that consumed their limit) is recorded instead; the rolling rung adds the same telemetry
**per event**, plus `prices_exact_end_idx` and `estimated_price_intervals`, so each solve's information
set is readable from the artifact rather than having to be re-derived.

Artifacts follow the existing precedent: `BENCHMARK_RESULT` and `Schedule.as_dict()` are unchanged,
and the day-ahead detail lives in `benchmark.json` (`day_ahead.*`) plus the wide
`schedules.parquet` (`policy="day_ahead"` rows).

The causal policy is deliberately **not** a replica of the household's AppDaemon rule; AppDaemon's
effect is already embedded in the measured load, which is why the *no-battery* policy is the
counterfactual. `day_ahead` shares the *information* AppDaemon uses (tomorrow's prices), not its
thermal actuators or its rule.

**Oracle certification:** certification is a property of the time limit, not of the oracle. At the
default 120 s on a full-year horizon the oracle returns a **time-limited incumbent**
(`is_certified=False`, its cost an upper bound), so the implied saving is only a lower bound on
optimal saving; at 600 s or more on that same horizon it **certified within tolerance**
(`solver_status="optimal"`, `bound_kind="certified"`) and is reported as a proven optimum. ADR-0003
§*Benchmark methodology clarifications* carries the measured telemetry for both. The knob that decides
this is the whole-horizon `--time-limit-s` above — not `--day-ahead-time-limit-s`, which is a separate
per **local day** limit (default 5.0 s) on the day-ahead solves and certifies nothing about the oracle.

**Solver noise at the no-export bound:** HiGHS can return a discharge one ulp above the load that
its own no-export row is bounded by, so a plan `battery.check_feasible` accepts (its slack is
`_TOL_KWH = 1e-6` kWh) can carry a net grid import of `-1.4e-16` kWh. `cost.compute_interval_cost`
refuses *any* negative energy by design — export credits are not modelled — so the orchestrator
costs a negative inside the kernel's own slack as zero and reports how many intervals needed that per
policy in `cost_consistency.<policy>.grid_noise_snapped_intervals` with an explanatory
`grid_noise_note`. Beyond that slack a negative is never clamped and the cost model's error is the
correct, loud outcome. This was found by a run over the then-four-policy ladder on synthetic data (the final
interval of a 100-slot fall-back day), not by the day-ahead policy itself — its clipped plans come out with
exact zeros, which `TestGridNoiseSnapping` pins. The check is a loop over the policies that ran, so the
fifth rung is covered by the same code path.

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
`--config` is optional but recommended: when passed, its canonical hash is checked against the one
pinned in the run and a mismatch refuses (`ReportError`) instead of reporting against another tariff.

**The report states its own horizon scope.** `report.md` carries a *Horizon scope* line under
*Evaluation window*, and `report.json` carries the same sentence as `horizon_scope_note`: the horizon
is a **UTC-scoped window, not a calendar year in the dataset's local timezone**, unless both bounds
genuinely fall on local midnights, in which case the line says it is a local calendar year too. The
sentence is derived from the run's own bound labels and the build manifest's pinned `params.timezone`,
so it hardcodes no year — the rule under *`benchmark`* above ("the claim is UTC-scoped and the report
must say so") is now enforced by the report instead of left to `Z`-suffixed labels to imply it.

Reports are written into the run directory with the same no-clobber rule as every other artifact, so a
changed report does not silently rewrite a run that has already been reviewed: re-generating a report
whose bytes differ raises `FileExistsError`, which the CLI surfaces as one line and exit code 1 — it is
intended. An existing run's `report.md` and `report.json` are evidence, not a rendering to refresh; the
tool never deletes canonical data on your behalf, so if a report genuinely must be rebuilt, remove that
one file by hand and know that its bytes will differ from what was reviewed.

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

### `ha-states`

```bash
# Summary of the store: parts, rows before and after dedup, entities present
uv run energy-baseline ha-states

# Every deduplicated state for one entity, in order
uv run energy-baseline ha-states --entity sensor.lavadora_current_status --list-states

# What that entity's state was at an instant (inclusive at the transition boundary)
uv run energy-baseline ha-states --entity sensor.lavadora_current_status \
  --as-of 2026-10-20T12:00:00Z
```

Reads the Home Assistant appliance enum history that `extract --source ha-history` preserves, through
`ha_states.py`. It exists because the durable Prometheus archive cannot carry these values at all —
the exporter emits state-value series only for numerically convertible states — and because the
recorder keeps them for about ten days, so the Parquet store is the only surviving copy.

- **Deduplication happens at read time**, on the same identity the extractor uses:
  `(entity, last_changed, last_updated, state, attributes_json)`. Part files are immutable and 12 known
  pre-dedup duplicate rows were **deliberately left in place** rather than rewriting history, so collapsing
  them is the reader's job; see
  [Overlapping re-extraction is deduplicated at row level](#overlapping-re-extraction-is-deduplicated-at-row-level).
- **As-of queries are inclusive at the transition boundary**: at exactly a transition instant the new state
  is visible, because `last_changed` is when the state became effective. Only rows with
  `last_changed <= T` are returned, which makes it *structurally* impossible to leak a later transition into
  an earlier snapshot — ADR-0002's as-of requirement, not a convention to remember.
- `carry_in` and both timestamps (`last_changed` = event time, `last_updated` = observation time) are
  preserved verbatim; consumers must filter `carry_in=False` before treating `last_changed` as a real
  event. Unavailable/unknown states are surfaced, not silently dropped.
- `--as-of` and `--list-states` both require `--entity`; a refusal is a `ValueError`, which the CLI prints
  as one line with exit code 1.
- **Verified live against the real store, read-only:** `parts=12 rows_before_dedup=51 rows_after_dedup=39
  duplicates=12 entities=2` for `sensor.lavadora_current_status` and `sensor.lavavajillas_current_status`.

**This is a read-side module.** It does not feed `build`, does not enter `dataset_id`, and no policy or
benchmark reads it. No appliance modelling, NILM or forecasting was built on it: it closes the long-deferred
"there is no reader" gap and nothing more, and ADR-0002 Phase 3 remains the place where consumption would
start.

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
recurring production schedule is **still not implemented**; today the cadence is
manual, on windows that deliberately overlap. That is only safe because
re-extraction is deduplicated at row level — see
[Overlapping re-extraction is deduplicated at row
level](#overlapping-re-extraction-is-deduplicated-at-row-level).

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

The mismatch is reconciled against `parts_written + fully_suppressed_chunks`,
so a chunk that found nothing new because everything was already stored is not
counted as a gap — only a chunk that yielded no new observation at all is.

### Overlapping re-extraction is deduplicated at row level

Manual extraction on a short recurring cycle overlaps windows by design (10-day
retention leaves no room for gap-free scheduling), and ADR-0003 asserts that
overlapping pulls are "cheap and idempotent to re-pull". That claim was false
until 2026-10-23: two real runs (9 parts / 39 rows, then 3 parts / 12 rows)
stored 51 rows for 39 distinct observations — 12 duplicates, 0 genuinely new
rows — while the second manifest recorded `quality_flags: []`, so it looked like
a clean, fully productive run even though the store had grown by ~31 %.

`extract_ha.py` now compares every candidate row against what is **already
stored** before writing:

- **Identity** is `(entity, last_changed, last_updated, state, attributes_json)`
  — the five `RAW_ENUM_STATE` columns that describe what Home Assistant recorded.
  `extraction_id`, `source_window_start` and `carry_in` are excluded: they describe
  the pull, not the observation. `extraction_id` is a fresh UUID per run, which is
  precisely why the content-addressed no-clobber store could never recognise two
  overlapping parts as duplicates — some of those part files were byte-identical
  except for that one column. Timestamps are compared as whole microseconds since
  the epoch, so a one-microsecond difference is never collapsed.
- **Prior coverage comes from the part files on disk**, not from manifests and not
  from window arithmetic: a row physically present is a row that must not be
  written twice, and a run that died after writing some parts left those parts
  behind. Manifests are scanned only to report provenance — which parts a successful
  run claimed, which are orphans.
- **The requested window is never narrowed.** Chunk boundaries stay a deterministic
  function of `[start, end)` so carry-in anchoring, part naming and part content all
  remain reproducible; dedup happens per row on top of that.
- **Nothing already stored is touched.** Dedup means not writing a duplicate —
  never rewriting, appending to, or deleting an existing part.

Accounting is recorded per chunk in `params.chunk_outcomes[]` and in totals in
`params.row_accounting`: `raw_rows` (what the response carried), `rows_seen`
(candidates surviving carry-in detection, the half-open filter and intra-chunk
dedup), `duplicate_rows_suppressed` (already present, not re-written), and
`new_rows` / `rows_written` (identical, because a new row is always written — a
failing write aborts the run before any manifest exists). `params.prior_coverage`
records what was scanned: parts, rows, distinct keys, pre-existing duplicates,
orphan parts and unreadable parts. `params.row_identity` records the key itself.

When any row is suppressed the manifest raises
`duplicate_rows_suppressed:<window>: seen=… already_present=… new=… rows_written=…`
for that chunk plus a `duplicate_rows_suppressed:total:` entry. **This is an
accounting disclosure, not an alarm:** on an intentional overlap suppression is the
expected outcome, each observation stays stored exactly once, and nothing is lost.
It is deliberately distinct in prefix and wording from the genuine-problem flags
(`empty_chunk:`, `empty_chunk_after_window_filter:`, `chunk_accounting_mismatch`,
`no_rows_for_entity:`, `no_rows_extracted:all`). A chunk whose rows were all already
present is marked `fully_suppressed`: no part file, but not a coverage gap, so it
does not raise `chunk_accounting_mismatch` either — and `no_rows_extracted:all` is
keyed on rows *seen*, so a productive overlap never claims that coverage is missing.

Three further disclosures describe real problems rather than intent:

- `carry_in_classification_disagreement:<window>` — a suppressed duplicate would be
  classified as carry-in differently from the copy already stored, which means the
  two runs' chunk boundaries did not line up. The stored row is kept as-is; check
  the chunk width and window start.
- `duplicate_rows_already_stored:<n>` — the prior parts themselves hold a logical key
  more than once, the residue of pre-dedup overlaps. This run reports the count and
  repairs nothing.
- `prior_part_unreadable:<part name>: <error type>` — a damaged part contributed no
  keys, so rows it holds may be written again. Duplication is the acceptable failure
  mode; losing not-re-extractable history is not. Only the part *name* is recorded,
  never its path.

**Idempotence holds:** a re-run of a window that is already fully stored writes zero
new parts and zero rows, discloses the suppression counts, and raises neither
`no_rows_extracted:all` nor `chunk_accounting_mismatch`. Covered by
`tests/test_extract_ha.py::TestCrossRunDedup`, including the mis-aligned-chunk case
where the tail chunk's window differs from the run that stored its rows and still
duplicates nothing. Idempotence is exact when the chunk width stays the same across
runs: carry-in rows are anchored to the chunk start (unchanged, deliberate behaviour),
so a run with a different `chunk_seconds` also synthesises carry-in rows at new anchors
— genuinely different rows, which are written, not deduplicated. Keep the chunk width
constant and overlapping pulls cost nothing.

**The 12 rows already duplicated in the private data dir** (manifest `1ec3085f…`,
overlapping `750bce0e…`) were not — and will not be — removed by this package. They
were inert because `build.py` has no HA reader at all, and they stay inert for a
consumer that does exist: the read-side `ha-states` reader (see
[§`ha-states`](#ha-states)) **collapses them at read time** on exactly the identity
described here, rather than rewriting immutable history. Two things follow. First,
the read-side rule was mandatory for any HA reader regardless of when the store
is cleaned, and it is now implemented: dedup on the same row identity, exactly as the Prometheus path collapses
`(series_key, ts, value)`. Second, if a physically clean store is ever wanted, either
revert the whole contributing run (its manifest together with the parts that *only*
that manifest claims, after verifying every one of their keys also exists in another
part, and after a snapshot) or rebuild into a fresh data directory. Never rewrite a
part in place, and never delete a part while its manifest still claims it — `validate`
checks manifest part hashes against the files on disk, so a half-revert breaks
validation instead of fixing it.

### `source_window_start` / `carry_in` flags

Both fields are preserved in the raw Parquet for every HA row. `carry_in=True`
marks rows whose `last_updated` was at or before the chunk start (not a real
transition). Consumers must filter on `carry_in=False` before treating
`last_changed` as a real event time.

Both are provenance about the *query*, not about the observation, which is why
neither is part of the dedup identity above: `source_window_start` is anchored to
the chunk start, and `carry_in` is derived from it. That anchoring is what makes
carry-in rows collapse correctly on re-extraction — the same chunk start produces
the same synthetic row, so an overlapping re-pull suppresses it like any other
duplicate rather than inventing an appliance event per run.

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
