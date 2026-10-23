# Energy Baseline — AppDaemon Price Rule as Baseline Context

- **Status:** RECORD ONLY — no scope change to ADR-0003 *(as of writing; the ADR was amended on
  2026-10-23 — see the supersession note below)*
- **Date:** 2026-10-23
- **Supersession note (2026-10-23):** The [Scope decision](#scope-decision-and-rationale) below
  states "no fourth policy is added in this phase" and the [day-ahead
  variant](#considered-and-deferred-a-day-ahead-informed-variant) was "considered and deferred".
  **This is superseded as of 2026-10-23:** the user approved implementation of a day-ahead policy
  (B2) as a new rung between causal and oracle. The policy is implemented in
  `scripts/energy-baseline/src/energy_baseline/policies.py` (`policy_day_ahead`) and is **committed
  at `c1569314`**. [ADR-0003](../docs/adr/0003-phase1-measurement-and-baseline.md) was amended the
  same day: its set is now four policies and its status is **Accepted**, so every three-policy
  statement below is historical. The analysis itself remains valid as baseline context — the AppDaemon
  rule is still thermal, not electrical, and still not a battery policy — but the day-ahead price
  variant it motivated is no longer deferred. The two **UNRESOLVED** items below
  (`min_hours_per_day` and the ESIOS day-boundary semantics) stay unresolved and now constrain how the
  new rung may be read. See [energy-baseline-progress.md](./energy-baseline-progress.md) for current
  status.
- **Extends:** [ADR-0003](../docs/adr/0003-phase1-measurement-and-baseline.md)
- **Related:** [Production execution plan](./energy-baseline-production-execution.md), [Implementation plan](./home-battery-optimizer-implementation.md), [Progress & handoff](./energy-baseline-progress.md)

## Purpose

[ADR-0003](../docs/adr/0003-phase1-measurement-and-baseline.md) fixes exactly three benchmark
policies: no-battery, causal simple price-based, and perfect-information oracle. **(Superseded
2026-10-23: ADR-0003 now has four rungs; the day-ahead variant was adopted as rung 3. The analysis
below remains valid as baseline context.)** It had been *assumed* that policy 2 — the causal
price-threshold policy — was required to mirror the household's existing AppDaemon price rule.
**That assumption was wrong.** This document records what the AppDaemon code actually does, verified
from source, and records the decision to keep the three policies unchanged and treat the finding
as **baseline context** rather than as a policy specification.

It also closes the open statement in ADR-0003 that "the magnitude of this prior optimization is not
quantified here": the *mechanism* is now known from source, so the remaining unknown is purely its
measured magnitude in the ch1 series, which the pipeline computes.

No code changes resulted from this investigation. See [Scope decision](#scope-decision-and-rationale).

## Verified findings

Two apps under `apps/home-assistant/appdaemon/apps/` make price-based decisions. A third,
[`last_connection_sensor.py`](../apps/home-assistant/appdaemon/apps/last_connection_sensor.py), is a
one-minute heartbeat that writes `sensor.last_appdaemon_connection` and contains no price logic.

Both decision apps read the **same entity**, `sensor.esios_pvpc`
([`apps.yaml:10`](../apps/home-assistant/appdaemon/apps/apps.yaml),
[`apps.yaml:28`](../apps/home-assistant/appdaemon/apps/apps.yaml)) — the `pvpc_updated` HACS
integration — and both read its **attribute dict `price_00h`…`price_23h`**, i.e. the full day-ahead
curve for the current day, *not* the live state value.
[`climate_control.py:73-91`](../apps/home-assistant/appdaemon/apps/climate_control.py) and
[`dhw_control.py:76-94`](../apps/home-assistant/appdaemon/apps/dhw_control.py) each iterate
`price_{i:02d}h` for `i in range(24)`.

A grep for `battery|charge|discharge|soc|thermal|tank|water` across the appdaemon tree returns
**zero matches**. There is no electrical-storage semantics anywhere in this code.

### Summary table

| App | Actuator entity | Rule | Constants | Cadence | Causal? |
|-----|-----------------|------|-----------|---------|---------|
| `ClimateControl` ([`climate_control.py:31`](../apps/home-assistant/appdaemon/apps/climate_control.py)) | `input_select.heishamon_heatmode` — aerotherm HVAC mode ([`apps.yaml:14-18`](../apps/home-assistant/appdaemon/apps/apps.yaml)) | 7-day historical mean × 2/3 threshold; if fewer than `min_hours` clear it, fall back to pure rank `sorted(...)[:min_hours]` | threshold factor `2/3` ([`climate_control.py:197`](../apps/home-assistant/appdaemon/apps/climate_control.py)); `days=7` ([`climate_control.py:175`](../apps/home-assistant/appdaemon/apps/climate_control.py)); fallback price `0.09` €/kWh ([`apps.yaml:19`](../apps/home-assistant/appdaemon/apps/apps.yaml), [`climate_control.py:199`](../apps/home-assistant/appdaemon/apps/climate_control.py)) | daily `00:00:40` ([`climate_control.py:45`](../apps/home-assistant/appdaemon/apps/climate_control.py)) + on enable toggle | **Look-ahead** — full 24 h curve read at decision time |
| `DHWControl` ([`dhw_control.py:34`](../apps/home-assistant/appdaemon/apps/dhw_control.py)) | `switch.aquarea_force_dhw_mode` — DHW tank boost ([`apps.yaml:32-34`](../apps/home-assistant/appdaemon/apps/apps.yaml)) | pure per-window argmin: split the day into fixed blocks, boost at the cheapest hour of each | `interval_hours` default 24, **deployed value 12** ([`apps.yaml:37`](../apps/home-assistant/appdaemon/apps/apps.yaml)); valid set `{1,2,3,4,6,8,12,24}` ([`dhw_control.py:154`](../apps/home-assistant/appdaemon/apps/dhw_control.py)) | daily `00:00:30` ([`dhw_control.py:48`](../apps/home-assistant/appdaemon/apps/dhw_control.py)) + on enable toggle | **Look-ahead** — full 24 h curve read at decision time |

### `climate_control.py` — hybrid threshold + rank

- The historical pull is `get_history(entity_id=…, days=7, minimal_response=True, no_attributes=True)`
  ([`climate_control.py:172-184`](../apps/home-assistant/appdaemon/apps/climate_control.py)), averaged
  at line 194 and thresholded at line 197.
- `price_10dma = mean([...])`
  ([`climate_control.py:194`](../apps/home-assistant/appdaemon/apps/climate_control.py)) — **the
  variable is misnamed: it is a 7-day mean, not a 10-day moving average.** Only `days=7` is passed
  and no windowing is applied to the list it averages.
- `cheap_price_limit = price_10dma * 2 / 3`
  ([`climate_control.py:197`](../apps/home-assistant/appdaemon/apps/climate_control.py)).
- `cheapest_prices = list(filter(lambda x: x.value < cheap_price_limit, prices))` — strict `<`
  ([`climate_control.py:206`](../apps/home-assistant/appdaemon/apps/climate_control.py)).
- Branch: if `len(cheapest_prices) >= min_hours` → schedule exactly those hours; otherwise →
  `_cheaper_hours(prices, min_hours)`, which is `sorted(prices, key=lambda x: x.value)[:min_hours]`
  — pure rank, no threshold
  ([`climate_control.py:110-111`](../apps/home-assistant/appdaemon/apps/climate_control.py),
  [`climate_control.py:206-226`](../apps/home-assistant/appdaemon/apps/climate_control.py)).
- Past hours of the current day are skipped at execution, not re-run
  ([`climate_control.py:272-276`](../apps/home-assistant/appdaemon/apps/climate_control.py)).

### `dhw_control.py` — pure argmin per fixed window

- `interval_hours` is read with default 24
  ([`dhw_control.py:153`](../apps/home-assistant/appdaemon/apps/dhw_control.py)); the **deployed
  value is 12** ([`apps.yaml:37`](../apps/home-assistant/appdaemon/apps/apps.yaml)); anything outside
  `{1,2,3,4,6,8,12,24}` is coerced back to 24
  ([`dhw_control.py:154-156`](../apps/home-assistant/appdaemon/apps/dhw_control.py)).
- `chunk_list(prices, interval_hours)` then `min(chunk, key=lambda x: x.value)` per block
  ([`dhw_control.py:152-160`](../apps/home-assistant/appdaemon/apps/dhw_control.py)). No threshold,
  no history, no mean — with `interval_hours: 12` the tank is boosted once in 00-11 h and once in
  12-23 h, at each block's cheapest hour.

### Cadence and causality

Both apps schedule **once per day** — `00:00:40` for climate
([`climate_control.py:45`](../apps/home-assistant/appdaemon/apps/climate_control.py)) and `00:00:30`
for DHW ([`dhw_control.py:48`](../apps/home-assistant/appdaemon/apps/dhw_control.py)) — plus
re-registration when the enable toggle flips. At that moment each fetches the full 24-hour curve and
registers `run_at` timers for every future hour of the day.

**This is look-ahead within the day:** at decision time the app already knows all 24 prices for that
day. Neither app is strictly causal in the sense ADR-0003 requires of policy 2 ("uses only
information causally available at decision time: current price and past state",
[line 144](../docs/adr/0003-phase1-measurement-and-baseline.md); input rule at
[line 100](../docs/adr/0003-phase1-measurement-and-baseline.md)).

### Fallbacks

- Climate
  [`_register_fallback_schedule()`](../apps/home-assistant/appdaemon/apps/climate_control.py)
  (call-site and retry at lines 153-164, definition at lines 250-257) runs the HVAC for `min_hours`
  from the *current* hour, then retries after 600 s.
- On historical-data failure `cheap_price_limit` falls back to
  `fallback_cheap_electricity_price = 0.09` €/kWh
  ([`apps.yaml:19`](../apps/home-assistant/appdaemon/apps/apps.yaml),
  [`climate_control.py:199`](../apps/home-assistant/appdaemon/apps/climate_control.py)) with a
  Telegram notice.
- DHW fallback
  ([`dhw_control.py:175-186`](../apps/home-assistant/appdaemon/apps/dhw_control.py)) forces every
  window start, or fires immediately if all window starts have already passed
  ([`dhw_control.py:183-184`](../apps/home-assistant/appdaemon/apps/dhw_control.py)).

### Negative prices are accepted, not rejected

`negative_price_notification(prices)`
([`utils.py:36-58`](../apps/home-assistant/appdaemon/apps/utils.py)) is purely informational and is
called at [`climate_control.py:168-170`](../apps/home-assistant/appdaemon/apps/climate_control.py);
DHW's `min(...)` naturally selects the most negative hour. Nothing filters on `>= 0`. This is the
opposite of the dashboard `>= 0` guard ADR-0003 identifies as defective (line 31). Pinned by
[`test_climate_control.py:168-202`](../apps/home-assistant/appdaemon/apps/tests/test_climate_control.py),
[`test_dhw_control.py:96-102`](../apps/home-assistant/appdaemon/apps/tests/test_dhw_control.py),
[`test_utils.py:75-108`](../apps/home-assistant/appdaemon/apps/tests/test_utils.py).

### Not covered by any test

Live in code but unasserted: the `price_10dma * 2/3` threshold, the 7-day window, the
`>= min_hours` branch, and the `0.09` €/kWh constant. Behaviour here is established by reading the
source, not by a test contract, so it can drift without a test failing.

## Things NOT determinable from the repo

### `min_hours_per_day` runtime value — UNRESOLVED

`min_hours` comes from `input_number.appdaemon_climate_min_hours_per_day`
([`climate_control.py:209`](../apps/home-assistant/appdaemon/apps/climate_control.py)). Its helper
definition
([`config/integrations/appdaemon_climate.yaml:7-11`](../apps/home-assistant/config/integrations/appdaemon_climate.yaml))
sets `min: 0`, `max: 24`, `step: 1` with **no `initial`** key and no `restore` key either. The repo
therefore declares a *range*, not a value. The live value is whatever the running Home Assistant
instance holds — set at runtime through the UI/API, persisted in Home Assistant state that is not
committed to this git repository.

Evidence pointing to **15**:
[`docs/troubleshooting/pvpc-updated-esios-api-zip-response.md:11`](../docs/troubleshooting/pvpc-updated-esios-api-zip-response.md)
logs `Less than 15 hours of data available`, which is the exact message template emitted at
[`climate_control.py:211-212`](../apps/home-assistant/appdaemon/apps/climate_control.py) with
`min_hours` interpolated.

Weaknesses of that evidence, stated plainly:

- It is a *single log line from a troubleshooting document*, not a configuration snapshot, and its
  date is not pinned to the current deployed state.
- The message fires only on the `len(prices) < min_hours` path, i.e. during the ESIOS ZIP outage, so
  it reflects the value as at that incident — a value the UI can change at any time.
- `min_hours` is re-read from HA state on every run
  ([`climate_control.py:209`](../apps/home-assistant/appdaemon/apps/climate_control.py),
  [`climate_control.py:251`](../apps/home-assistant/appdaemon/apps/climate_control.py)); nothing
  freezes it.
- The test suite mocks it as `"4"`
  ([`test_climate_control.py:560`](../apps/home-assistant/appdaemon/apps/tests/test_climate_control.py)),
  which is a fixture value and carries no information about production.

**This is recorded as unresolved.** 15 is the best available inference, not an established fact, and
must not be written into any policy parameter or test fixture as if it were.

### ESIOS archive 70 day semantics at 00:00 — UNRESOLVED

Whether the sensor's `price_00h…price_23h` attributes at ~00:00 describe the day that has just
started or the next day is not determinable from this repo. The AppDaemon code assumes whichever set
the sensor delivers and re-labels it onto `datetime(now.year, now.month, now.day)` plus `i` hours
([`climate_control.py:85`](../apps/home-assistant/appdaemon/apps/climate_control.py),
[`dhw_control.py:88`](../apps/home-assistant/appdaemon/apps/dhw_control.py)), so the code cannot
answer the question. It is already tracked as **TODO-0.4** in
[`planning/home-battery-optimizer-implementation.md:45`](./home-battery-optimizer-implementation.md)
("whether today's/tomorrow's arrays match their validity interval"). It stays unresolved here, and it
matters: the day-boundary convention decides which calendar day a reconstructed day-ahead curve
belongs to.

## Scope decision and rationale

**Decision: the three benchmark policies in ADR-0003 are unchanged. No fourth policy is added in
this phase. The AppDaemon rule is recorded above as baseline context only.**

**(Superseded 2026-10-23: the day-ahead variant was adopted as rung 3; ADR-0003 now has four
policies. The analysis above remains valid as baseline context for the `no_battery` and
`causal_price` rungs, and the unresolved items below constrain how the new `day_ahead` rung may be
read. See [ADR-0003](../docs/adr/0003-phase1-measurement-and-baseline.md) and
[energy-baseline-progress.md](./energy-baseline-progress.md) for current status.)**

### The premise was incorrect

It was assumed that policy 2 must mirror the AppDaemon rule. ADR-0003 assigns "existing AppDaemon
scheduling" to policy **1**, not policy 2. Lines 143-144 verbatim:

> 1. **No battery** — existing AppDaemon scheduling, baseline cost. This is the counterfactual: what
> we pay today without any battery dispatch.
> 2. **Causal simple price-based policy** — a fixed-threshold rule (charge below a percentile,
> discharge above another) that uses only information causally available at decision time: current
> price and past state. Parameters are trained on an earlier split and frozen for evaluation.
> Represents a simple, interpretable dispatch strategy implementable without future knowledge.

Policy 1 *is* the AppDaemon counterfactual. Policy 2 is deliberately "a simple, interpretable
dispatch strategy implementable without future knowledge". Folding the AppDaemon rule into policy 2
would have been a scope error, not a fidelity improvement.

### A literal replica cannot be a battery policy

AppDaemon schedules two **thermal** actuators: the aerotherm HVAC mode
(`input_select.heishamon_heatmode`) and the DHW tank boost (`switch.aquarea_force_dhw_mode`). It has
no SOC, no charge or discharge path, no efficiency, and no import ceiling. A "battery policy that
replicates AppDaemon" is not expressible without inventing a thermal-to-electrical mapping that does
not exist in the source and has no measured basis.

### Re-simulating it would double-count

The load that AppDaemon shifts is **already inside the measured ch1 series**, which ADR-0003 defines
as the gross household load (line 94). Line 114 states it directly: AppDaemon "has already shifted
some aerotherm consumption to cheaper hours, which reduces the incremental headroom available to a
battery". Simulating that same shifting as a battery dispatch strategy would count the benefit twice
— once in the measured load shape, once in the simulated policy — and would make the causal policy's
savings number meaningless.

### Replacing the causal rung would destroy the information ladder

The three policies form an evidence ladder on information availability: **(now four; the day-ahead
rung was adopted 2026-10-23 as rung 3 between causal and oracle)**

| Rung | Information used | Interpretation |
|------|------------------|----------------|
| 1 — no battery | none (measured) | what we pay today |
| 2 — causal | current price + past state | achievable with no forecast |
| 3 — **day_ahead** *(added 2026-10-23)* | today's 24 h at midnight | reachable from published prices alone |
| 4 — oracle | perfect future prices and load | physically feasible ceiling |

AppDaemon is not rung 2's peer; it is a look-ahead rule, as shown above. Substituting it for the
causal policy would leave the ladder with no current-price-only rung, and the **causal → oracle gap
is precisely the measure of the value of forecast/perfect information** that a later controller
architecture decision needs. Collapsing that rung removes the number the next phase is meant to
read.

### The shifting that exists today is thermal, not electrical

The only storage in the current system is building thermal mass and the DHW tank. There is no
electrical storage in this codebase (grep for `battery|charge|discharge|soc` → zero matches). Any
statement about "AppDaemon already doing battery arbitrage" would be a category error.

## Considered and deferred: a day-ahead-informed variant

**Status:** *Adopted 2026-10-23* — the day-ahead policy is now rung 3 in ADR-0003's four-rung ladder.
The analysis below is retained as historical context; see [ADR-0003](../docs/adr/0003-phase1-measurement-and-baseline.md)
and [energy-baseline-progress.md](./energy-baseline-progress.md) for the current implementation.

The household **demonstrably already has day-ahead prices available**. Both apps consume
`price_00h…price_23h` on `sensor.esios_pvpc` at midnight, which is a full 24-point day-ahead curve
already reaching Home Assistant. So a day-ahead-informed variant is *feasible*, and this was
**considered and deferred — not rejected outright.**

ADR-0003 line 100 already anticipates exactly this:

> If a later policy variant uses a day-ahead price array, that assumption is explicitly labelled: the
> policy requires that prices are published and known ahead of time, and the pipeline verifies that
> the sensor actually archives them (e.g. `price_00h..price_23h`) rather than assuming realised
> prices are available ahead of time.

PVPC is day-ahead **by construction**: the prices for day D are published the evening of D−1. A
day-ahead-informed policy is therefore reconstructible from the price series this pipeline already
extracts, **with no new extraction** — the data path exists. The condition that must be checked
before such a policy is trusted is the archival one line 100 names (whether the attribute dict is
captured per hour, rather than only the scalar state), plus the unresolved day-boundary semantics
above.

It is deferred because adding a fourth rung is a scope change to a decision that is currently
**Proposed**, and because the causal → oracle gap the three-policy set exists to measure would still
be needed to interpret it.

**(Superseded 2026-10-23: the day-ahead policy was adopted as rung 3; ADR-0003 is now Accepted with
four policies. The unresolved ESIOS day-boundary semantics above still constrain interpretation of
the new rung.)**

## What This Document Does NOT Do

- **Does not change ADR-0003's three-policy set**, its Status, or its Date. The only ADR edits are a
  note that the mechanism behind line 114 is now source-verified, and a row in the rejected/deferred
  table.

  **(Superseded 2026-10-23: ADR-0003 now has four policies and is Accepted; this document's
  three-policy statements are historical. The unresolved items above remain valid constraints.)**

- **Does not add, implement, or parameterise a fourth policy.**

  **(Superseded 2026-10-23: the fourth policy was implemented and committed at `c1569314`.)**
- **Does not assert `min_hours_per_day = 15` as fact.**
- **Changes no code.** No file under `scripts/energy-baseline/`, no AppDaemon app, and no Home
  Assistant configuration was modified by this investigation. The findings are read-only: the
  AppDaemon apps keep sole ownership of HVAC and DHW scheduling, and this phase remains offline with
  no live actuation.

## References

- [ADR-0003 — Phase 1 measurement and baseline](../docs/adr/0003-phase1-measurement-and-baseline.md)
- [ADR-0001 — Forecast-informed economic dispatch](../docs/adr/0001-home-battery-economic-dispatch.md)
- [ADR-0002 — Appliance-aware load forecasting](../docs/adr/0002-appliance-aware-load-forecasting.md)
- [Home battery optimizer implementation plan](./home-battery-optimizer-implementation.md)
- [Production execution plan](./energy-baseline-production-execution.md)
- [AppDaemon climate control source](../apps/home-assistant/appdaemon/apps/climate_control.py)
- [AppDaemon DHW control source](../apps/home-assistant/appdaemon/apps/dhw_control.py)
- [AppDaemon shared utilities](../apps/home-assistant/appdaemon/apps/utils.py)
- [AppDaemon app configuration](../apps/home-assistant/appdaemon/apps/apps.yaml)
- [AppDaemon helpers input_number definition](../apps/home-assistant/config/integrations/appdaemon_climate.yaml)
- [AppDaemon negative prices](../docs/troubleshooting/appdaemon-pvpc-negative-prices.md)
- [ESIOS ZIP response incident — source of the `min_hours` inference](../docs/troubleshooting/pvpc-updated-esios-api-zip-response.md)
