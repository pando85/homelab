# ADR-0001: Forecast-informed economic dispatch for a home AC battery

- **Status:** Proposed
- **Date:** 2026-10-23
- **Decision owners:** Homelab maintainers
- **Implementation plan:** [Home battery optimizer](../../planning/home-battery-optimizer-implementation.md)

## Context

The homelab already operates Home Assistant under `apps/home-assistant/`, with:

- AppDaemon's `climate_control` and `dhw_control` choosing low-price PVPC hours for aerothermal heating and hot water (`apps/home-assistant/appdaemon/apps/`).
- An ESIOS PVPC sensor (`sensor.esios_pvpc`) and historical household electricity measurements.
- Shelly devices including `shellyem-main` and `shellypmmini-servers`, visible in the monitoring configuration. **Their electrical placement, sign convention, accuracy, and measurement interval must be verified before control**; a channel named "main" is not sufficient evidence that it measures net grid exchange.
- An approximately 250 W continuously running server load. The remaining demand is less predictable; the aerothermal load is already deliberately shifted to cheap hours.

A grid-charged AC battery of roughly 5 kWh is being evaluated, with Marstek Venus E 3.0 as the current leading candidate, not a committed dependency. It offers approximately 5.12 kWh nominal energy and up to 2.5 kW nominal AC power, subject to its exact hardware, firmware and installation configuration. The objective is **bill reduction through time-of-use arbitrage**, not solar self-consumption. Backup power, when available, is a separate electrical feature, not part of the dispatch guarantee.

PVPC prices can be negative and have publication delays; daily schedules must account for Europe/Madrid daylight-saving transitions (23-/25-hour days). A correct optimization needs both the *marginal billable import price* and a demand forecast. Merely running the battery whenever the home consumes power can exhaust it before the expensive evening hours.

Prior economic simulations were exploratory: their efficiencies, useful SOC window, equipment prices, forecast accuracy, idle draw, battery life and bill/tax assumptions are **not measurements** and must not become production constants without validation.

## Decision

**Build a forecast-informed, receding-horizon economic planner, while delegating device-level regulation and safety to one existing Home Assistant battery integration.** Implement it progressively, with an offline backtest and shadow mode before enabling any battery writes.

### Logical architecture

```mermaid
flowchart TD
    P[PVPC / tariff-aware price feed] --> D[Validated time-series and forecasts]
    S[Shelly grid and load sensors] --> D
    H[Existing AppDaemon HVAC and DHW schedules] --> D
    B[Battery SOC and telemetry] --> D
    D --> M[Economic planner: MPC / HiGHS]
    M --> I[Dispatch intent: mode, power, expiry]
    I --> O[Home Assistant / Omnibattery: sole device writer]
    S --> O
    O --> E[Marstek E 3.0 or compatible AC battery]
    E --> B
    D --> Q[Backtesting and metrics]
    M --> Q
```

1. **Metering and data:** Preserve raw UTC/offset-aware intervals, source provenance, units, interval duration and missing-data flags. The input demand is gross household consumption, not observed grid import while the battery is active. For a home with no PV, using the chosen sign convention: `P_home = P_grid - P_battery_charge + P_battery_discharge`, where `P_grid` is signed positive for import. Validate each sign from real observations; account for inverter standby and any other production.
2. **Forecasting:** Start with a transparent calendar/seasonal/rolling-median predictor, with the ~250 W server baseline and the **already committed** AppDaemon climate/DHW schedule as explicit inputs. Report prediction intervals (e.g. P10/P50/P90), not only point forecasts. Consider gradient-boosted models only if out-of-sample economic savings improve.
3. **Economic optimization:** Re-solve every 15 minutes, with a configurable rolling 24–48 hour horizon; execute only the current interval and replan on new readings. Optimize net **marginal import cost** including applicable variable taxes/charges, converter losses, standby consumption and an explicit throughput/degradation sensitivity. Represent an unknown future price as a forecast with uncertainty; never treat tomorrow's unpublished tariff as known. Preserve a terminal SOC value or equivalent terminal penalty instead of artificially emptying the battery at midnight.
4. **Physical constraints:** Respect verified power, SOC and energy limits (provisional 20–95% SOC, ≤2.5 kW for the E 3.0), import power ceiling, realistic charge/discharge efficiencies, and no simultaneous charge/discharge. Enforce no intentional export via independent **real-time grid-feedback control**; the economic optimizer alone is not a sufficiently fast export guard. The charge target must not violate the household's contracted/import limit.
5. **Control ownership:** Start with [Omnibattery](https://ffunes.github.io/Omnibattery/) for local Marstek E v3 Modbus TCP communication and grid-feedback control. Confirm actual device firmware support and the integration's external-command or manual-control contract before writing an adapter. **Only one component may write battery setpoints at a time.** The planner must not write raw Modbus registers concurrently with Omnibattery automatic control. The documented manual-mode handoff isolates a battery from Omnibattery automatic dispatch, but using it requires a dedicated single-writer adapter, explicit lease/heartbeat and a tested safe-stop sequence; prefer extending Omnibattery with a higher-level dispatch-intent interface if a reliable public interface is absent.
6. **Safety and operations:** A missing/stale meter, stale SOC, lost battery connectivity, control lease expiry, invalid price schedule, solver failure, import limit breach or uncertain inverter response cancels economic dispatch and requests safe idle (0 W) through the established controller. Independently configured battery/BMS protection remains authoritative. Alert and require explicit acknowledgement for recurrent command failures. After safe idle is acknowledged, manual control can be returned to Omnibattery; do not assume an API timeout guarantees the physical output is zero. Never depend on the planner to provide anti-islanding or backup safety.
7. **Integration boundary:** The existing AppDaemon heating and DHW algorithms retain authority over comfort/heat scheduling. The battery planner observes their schedules, not their control entities. Joint heat-pump/battery scheduling is an optional later ADR; it must preserve comfort and prevent two controllers independently changing the same load.
8. **Deployment:** Keep application/config changes GitOps-managed. Begin with an offline Python model; introduce a small dedicated `apps/battery-optimizer/` deployment only after shadow testing, rather than placing optimization and device communication inside one monolithic HA automation. Publish metrics to the existing Prometheus/Grafana stack. Store credentials through the established Vault/ExternalSecret workflow.

### Optimization formulation (first implementation)

For each interval `t`, decide nonnegative grid charge `c_t`, AC discharge `d_t`, and battery energy `E_t` (kWh). With duration `Δt` in hours:

- Battery state: `E_(t+1) = E_t + η_c c_t Δt - (d_t / η_d) Δt - standby_loss_t`.
- Grid import: `g_t = max(0, forecast_home_load_t + c_t - d_t)`. A model of export must constrain it to zero, not discard/export energy for free.
- Minimize `Σ_t [buy_price_t * g_t * Δt + throughput_cost(d_t, c_t)] + terminal_SOC_penalty`, subject to energy, power, grid and mode constraints.
- For unknown future prices or load, use scenarios/robust margins rather than a fictitious perfect forecast.
- A linear model with HiGHS is the first choice where its assumptions suffice; use MILP for binary charge/discharge state or piecewise efficiency only when it materially changes the optimum. Verify that the objective does not allow pathological simultaneous flow or artificial negative-price cycling.

The economic planner emits **requested** setpoints. Actual output is capped by live load, grid meter feedback, hardware protections and command acknowledgement. A 15-minute plan is not an authorization to inject into the grid for 15 minutes.

### Comparison and validation

Compare the same date ranges and the same *available-at-the-time* information for all policies:

1. **No battery** — existing HVAC/DHW scheduling remains in place.
2. **Omnibattery/native rule** — its supported price-based schedule and regulation, as actually configured.
3. **Price-only planner** — tariff and SOC, simple baseline demand.
4. **MPC with statistical demand forecast** — expected first custom implementation.
5. **MPC with advanced/quantile forecast** — optional, only if its net economic value warrants it.
6. **Perfect-foresight oracle** — diagnostic upper bound only, not an achievable saving estimate.

Use walk-forward training/evaluation without future consumption leakage, measure demand prediction accuracy **and** incremental EUR savings (including converter losses and idle energy), equivalent full cycles, curtailed commands, export/import violations, controller availability and forecast regret. Retain uncertainty bands via day/week block bootstrap and tariff/efficiency sensitivity. Reconcile to monthly invoices and report fixed charges separately.

## Rejected or deferred alternatives

| Alternative | Rationale |
|---|---|
| Hardcoded cheap/expensive windows only | Valuable baseline and safe fallback, but ignores SOC opportunity cost and shifting peaks. |
| Forecast model choosing setpoints directly | Entangles demand inference, economics and hardware safety; difficult to test and audit. |
| End-to-end LLM/agent control | Unnecessary, non-deterministic and difficult to enforce hard constraints. |
| Direct Modbus writes from a second controller | Conflicting device ownership with Omnibattery and weak failure isolation. |
| Rewrite Omnibattery/HA control | Reimplements known hardware/protocol and feedback problems before proving any economic upside. |
| Optimize heat-pump hours jointly in v1 | AppDaemon already owns HVAC/DHW schedules and comfort; adds a conflicting control surface. |
| Machine-learning forecast from day one | More complexity without evidence that forecast MAE improvement creates financial value. |

## Consequences

- Positive: explainable dispatch, local operation, an auditable savings model, reusable battery adapter and a gradual path to more advanced forecasts.
- Costs: historical data normalization, tariff accounting, command ownership integration and persistent observation/metrics require engineering and calibration.
- Unresolved: exact main Shelly grid boundary, PVPC billed tariff reconciliation, the real E 3.0/alternative model and firmware, achievable Modbus command contract, and local electrical protection/certification. **Round-trip efficiency:** the owner-supplied community-measured range of 0.80–0.84 contradicted the 0.88 the offline model used, and on 2026-10-24 the owner **adopted 0.80** — the bottom of that range — as a simplifying assumption, reasoning that at 2.5 kW the unit operates mostly near its power rating where efficiency is typically worst. `config/tariff.yaml` now ships `round_trip_efficiency: 0.80`, one value line; the other battery parameters are unchanged. 0.80 **supersedes 0.88** and is still an assumption, not a measurement of this household's unit: efficiency versus output power remains a curve nobody has measured, and idle draw remains assumed zero and wholly unmeasured. These are Phase 0 gates, not assumed away (cross-reference [ADR-0003](0003-phase1-measurement-and-baseline.md) *Consequences → Unresolved* and *Round-trip efficiency: 0.88 → 0.80, applied* for the consequence on every figure produced so far — every 0.88-era result, including the accepted baseline run, is now a superseded optimistic figure awaiting a clean-tree rebuild).
- **Offline implementation note (2026-10-24), changing no part of this decision.** Two pieces of the architecture above now exist in the offline model, and only in the offline model. **(a)** Item 3's receding-horizon controller is reachable: `rolling_horizon` is a canonical benchmark policy, and `rolling_horizon.py` takes `replan_interval_slots` / `horizon_slots`, so `replan_interval_slots=1` with `horizon_slots=96` is exactly "re-solve every 15 minutes over a rolling 24-hour horizon, execute only the current interval". The publication rule is stated once and derived from the decision instant, so a 09:00 solve estimates the next day and a 21:00 solve does not, at any cadence. The default remains the legacy two-event schedule, which is the configuration validated bit-for-bit; the 15-minute cadence costs roughly **48× the solve count** (8,832 against 185 over a 92-day holdout) and roughly **37× the wall time** (~175 s against ~4.7 s per arm on synthetic fixtures) — the two ratios differ because a fine-cadence solve is cheaper, since each legacy event plans the rest of its local day *and the next one* while cadence 1 plans a fixed 96 slots — and it has **not** been validated on real data. **(b)** The `Dispatch intent: mode, power, expiry` box is now a typed, validated, canonically serialized contract (`src/energy_baseline/dispatch.py`): interval, mode, target power, SOC target, expected price, confidence, generation instant, expiry and sequence, with physics delegated to the battery kernel and a caller-supplied clock that has no wall-clock fallback. It has **no writer, no I/O and no CLI subcommand** — the diagram's `Home Assistant / Omnibattery: sole device writer` node and every safety path in item 6 are still unbuilt, and nothing here actuates a battery.
- No hardware purchase or automatic actuation is authorized by accepting this ADR.

  - **Owner note (2026-10-24): the battery hardware has already been bought.** The sentence above is
    unchanged and still true: it describes what *this ADR authorizes*, and it authorizes neither a
    purchase nor actuation. The purchase is a separate owner decision, taken outside this decision
    record, and automatic actuation remains unauthorized. What the purchase does change is which
    Phase 0 gates stay live:
    - **TODO-0.5 — device *selection* is moot, device *verification* is not.** The model is no longer
      a candidate to choose but a delivered unit to measure. Everything in TODO-0.5 that was
      verification remains fully live, and is now verifiable on the installed unit instead of on a
      datasheet: firmware, the supported local read/write method, power and SOC limits, response
      latency, partial-load efficiency and idle draw, and how the unit is safely stopped. The E 3.0
      named in Context is therefore to be *confirmed against the delivered unit*, including whether
      native Ethernet/Modbus TCP is actually present on it.
    - **TODO-0.6 is unaffected and remains a gate** — a qualified installer, electrical protection,
      anti-islanding and BMS independence do not follow from owning the hardware.
    - **TODO-0.1 is unaffected and remains a gate** — tracing the main Shelly channels to the net grid
      point of common coupling and verifying clamp directions still precedes any no-export control.

    This is the work the hardware-gated rows of
    [the battery roadmap](../../planning/energy-battery-roadmap.md) describe (§2, *How each component
    iterates*): the battery model from v2 onward, the device writer and the safety guard. Nothing here
    makes the offline Phase 1 baseline results current — the efficiency correction and the clean-tree
    rebuild still stand.

## References

- [Implementation plan](../../planning/home-battery-optimizer-implementation.md)
- [ADR-0003 — Phase 1 measurement and baseline](0003-phase1-measurement-and-baseline.md) — **Accepted** (2026-10-23): the offline backtest and strategy baseline this decision prescribes before any battery writes, now an information ladder of five policies (four at acceptance; `rolling_horizon` promoted from an offline experiment on 2026-10-24)
- [Current AppDaemon PVPC climate/DHW configuration](../../apps/home-assistant/appdaemon/apps/apps.yaml)
- [Home Assistant deployment](../../apps/home-assistant/values.yaml)
- [Existing negative-price fallback runbook](../troubleshooting/appdaemon-pvpc-negative-prices.md)
- [Omnibattery overview](https://ffunes.github.io/Omnibattery/)
- [Omnibattery model compatibility](https://ffunes.github.io/Omnibattery/compatibility/)
- [Omnibattery manual-control ownership](https://ffunes.github.io/Omnibattery/features/multi-battery/)
