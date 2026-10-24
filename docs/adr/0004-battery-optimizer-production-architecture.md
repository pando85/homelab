# ADR-0004: Production runtime and independent source repository for the battery optimizer

- **Status:** Proposed
- **Date:** 2026-10-24
- **Decision owners:** Homelab maintainers
- **Extends:** [ADR-0001 — economic dispatch](0001-home-battery-economic-dispatch.md)
- **Implementation and acceptance:** [Home battery optimizer implementation plan, §16](../../planning/home-battery-optimizer-implementation.md#16-production-packaging-and-runtime-rollout) (plus Phases 5–6)
- **Current component state:** [Battery roadmap](../../planning/energy-battery-roadmap.md)

## Context

[ADR-0001](0001-home-battery-economic-dispatch.md) already decides the economic MPC, single-writer Omnibattery ownership and safe-dispatch constraints. [ADR-0002](0002-appliance-aware-load-forecasting.md) and [ADR-0003](0003-phase1-measurement-and-baseline.md) cover forecasting inputs and the offline data pipeline. **This ADR only decides how the optimizer becomes a separately released production application.** It does not redefine physical dispatch or replace those decisions.

The current `scripts/energy-baseline/` Python package can extract Prometheus/HA data, create reproducible offline datasets, compare policies and serialize typed dispatch intents. It has no live writer, runtime service or deployed safety guard. HA, AppDaemon, Prometheus, Grafana, Vault and ArgoCD already exist in the homelab.

Application code, test suite and container artifacts need an independent release lifecycle; homelab-specific cluster configuration must remain GitOps-managed. We want minimal new operational dependencies.

## Decision

1. **Separate repositories, without moving code in this PR.** A future dedicated optimizer repository (working name `pando85/battery-optimizer`, not yet created) will own Python code, offline CLI, tests/fixtures, dependency lockfile, Dockerfile, CI and immutable OCI image releases. `pando85/homelab` will own the pinned image digest, ArgoCD/Helm manifests, HA entity mappings, secrets references, PVC, policies, dashboards and operator runbooks. Keep the current `scripts/energy-baseline/` implementation unchanged until a dedicated migration PR preserves tests, CLI and historical dataset provenance; its dataset IDs currently depend on the source repository commit.

2. **One small, continuously running Python service.** Deploy a single-replica `battery-optimizer` process in K3s, starting in **shadow mode**. Its modules consume current HA entity states/events through authenticated WebSocket/REST, use the Prometheus HTTP API for historical measurements, validate PVPC tariff publication times, compute forecast + receding-horizon MPC using the **same pure code as the offline benchmarks**, and emit validated `DispatchIntent` messages. Use Python 3.12+, `asyncio`/`httpx`, the existing SciPy/HiGHS stack, a small internal HTTP status/metrics API (FastAPI is a reasonable choice) and `prometheus-client`. Do not add a broker, cache cluster or dedicated DB solely for this workload. Keep blocking solves off the event loop.

3. **Historical data remains batch.** Run `extract/validate/build` and optional benchmark/training as separate, read-only Jobs/CronJobs using private Parquet manifests and DuckDB, according to the [existing extraction plan](../../planning/energy-baseline-production-execution.md). An extract schedule must retain HA recorder events before its declared retention expires. Model training is optional and only promoted after chronological, out-of-sample **net savings** validation.

4. **Planner intents are not physical setpoints.** Omnibattery in HA remains the sole Modbus writer and fast regulator using *fresh net-grid Shelly measurements*. The planner must not write Modbus concurrently. **Source review (2026-10-24) confirms a usable, preliminary public HA entity control surface** for runtime grid targets, power ceilings and per-battery directional permissions while retaining automatic PD ownership (details below). This is the preferred **first adapter candidate**, not a proven atomic/leased economic-intent interface. A supported high-level `DispatchIntent` API with atomic transitions and expiry is **not** currently established; an upstream-compatible extension may eventually be warranted. Omnibattery's battery manual mode removes it from the automatic pool and is not the intended first control path. **Shadow-first and the separate hardware safety gates remain unchanged.**

5. **Physical failure behaviour gates live operation.** A lease, HA automation, pod termination handler or API acknowledgement does **not** guarantee 0 W when HA/network/optimizer fails. The actual Marstek, meter topology, backup circuit and controller must be commissioned and tested under supervised failures; independent device/installer protections remain authoritative. No unattended live dispatch until the measurable power response, ownership transition, safe stop and no-export behaviour under failure are established. A new pod must never resume actuation just because a prior plan was persisted.

6. **Independent release, reviewed GitOps promotion.** The future app repo builds/tests/releases a pinned wheel and container image (e.g. GHCR digest, SBOM and provenance where feasible). A separate homelab PR updates the image **by digest**, runs shadow validation and gates live mode explicitly; no automatic change of control authority on image upgrade. Initial Deployment has one replica and no HPA. A replica count of one is **not** an exclusivity guarantee: enforce ownership inside the HA/Omnibattery boundary.

## Omnibattery integration contract — source review (2026-10-24)

**Evidence status:** Based on [ffunes/Omnibattery](https://github.com/ffunes/Omnibattery) source and reference documentation as inspected on 2026-10-24; **no local HA / Venus E V3 integration or real-device tests have been conducted**. Entity IDs are examples/patterns and may differ on an existing HA registry. Treat this as an *adapter design hypothesis* until commissioning, not an assertion that live dispatch is safe.

### Confirmed existing Home Assistant controls

| Surface | Behavior observed upstream | Intended use / limitation |
|---|---|---|
| `number.*_pd_target_grid_power` | Hot-reloaded control target, not a direct device setpoint. **Positive = grid import; negative = grid export**. Entity bounds follow configured battery capacity and optional system caps. | Request a grid-import target for economic charging or a small **positive** import margin during discharge; cannot assume exact battery watts. |
| `switch.*_battery_allow_charge` and `switch.*_battery_allow_discharge` | Per-battery automatic-PD participation flags. Disabling a direction adds a controller blocker and attempts `0 W` when active in that direction. | Gate charge/discharge; remain in automatic mode. A successful switch action alone does not prove physical idle. |
| `switch.*_system_power_limits` and `number.*_system_max_{charge,discharge}_power` | Optional total automatic-control caps, enforced through the PD power allocation; changed via `config_entry.data` and controller hot-reload. | Bound permitted power; **a numeric cap of `0` means unlimited/disabled in that direction, NOT zero output**. Use direction blockers to prohibit operation. |
| `switch.*_battery_manual_mode` / manual setpoint numbers | Manual mode first requests and verifies `0 W` for ownership handoff, then removes the battery from automatic PD dispatch. | **Do not use** for ordinary MPC orchestration: automatic net-grid feedback no longer owns that battery. |
| `sensor.*_integration_status`, battery SOC/power, Shelly grid meter | Feedback and blocker diagnostics are available where supported/configured. | Observe *requested, accepted, measured* values separately; never equate successful HA service response with actual power. |

The standard HA `number.set_value` and `switch.turn_on/off` services are candidate commands. They are **separate persistent configuration writes**, not one atomic transaction: `number.py` updates `config_entry.data` then hot-reloads controller parameters; switch changes are likewise persisted. No native **economic-command TTL, sequence fencing or atomic multiparameter apply/rollback** has been verified. The optimizer must not assume that a previous economic setting expires when its pod crashes.

The PD implementation is **incremental**: it adjusts the prior output with proportional and filtered-derivative corrections; explicit `Ki` defaults to zero. It also enforces deadband/hysteresis, rate limiting, availability and SOC restrictions, and actual net-grid feedback. The MPC therefore supplies *authorization/preferences*, not second-by-second corrections or guaranteed delivered power. Do not try to implement an external 15-minute no-export controller. Additional Omnibattery features (predictive grid charging, time slots, dynamic pricing, setpoint offsets/overrides, hourly balance, peak shaving) can independently change target or direction; inventory and disable **competing economic strategies** before enabling MPC, retaining safety protections.

### Provisional mapping from MPC intent to public HA entities

| `DispatchIntent.mode` | Direction permissions | PD grid target | Bound |
|---|---|---|---|
| `idle` | Charge **off**, discharge **off** | Normal conservative import target | No physical output guarantee until readback |
| `discharge` | Charge **off**, discharge **on** | Small **positive** import margin (e.g. +50 W, commissioning value) | Positive system discharge cap (if needed) and actual metered demand, SOC and firmware limits |
| `charge` | Charge **on**, discharge **off** | **Positive import** target chosen from desired battery charge plus observed/forecast load | Contracted power, positive charge cap and physical meter feedback |

This mapping is a **hypothesis for integration tests, not a ready-to-execute procedure**. A target for *grid import* does not equal battery charging power; `P_grid ≈ P_house + P_battery_charge - P_battery_discharge` (no PV, consistent meter boundary). Source logic computes the allowed `pd_target_grid_power` slider maximum from aggregate charge caps, so naïvely setting an import target and a matching battery charge cap to the same number can unintentionally reduce actual charging. Hardware caps, step size, load changes and active overrides further affect realizable outputs. Resolve desired-power translation using readback, actual power and meter feedback, not by assuming the setpoint was achieved.

**Ordering/failure contract to design before writes:** source freshness and control-eligibility check; safely constrain permissions *before* changing a target or widening a cap; update ordered entities with one in-flight command and readback; inspect controller blockers and measured grid/battery power; publish a reconciled state only when verified. Preserve user/installer configured limits: do not overwrite arbitrary HA control settings on shutdown, assume the optimizer is their sole owner, or restore an obsolete configuration. Recovery from partial writes, remote user edits and restart needs its own explicit ownership/version policy. The current `DispatchIntent.expires_at` is application metadata and is **not enforced by Omnibattery**.

### Safety caveats / commissioning gates

- **HA/optimizer outage:** Omnibattery stores persistent controls and runs inside HA. HA-local timeouts, scripts or lease checks cannot execute after HA dies; whether the Venus continues an old setpoint or autonomously idles under that failure is **unverified**. Until a physically independent and tested fallback is available, no unattended economic dispatch or no-export guarantee.
- **Meter:** Omnibattery expects **signed net-grid power** (positive import) and recommends 1–2 s updates; it performs a safety cycle approximately every 2 s and documents a **65 s stale-reading tolerance**. Those values are not an independent hardware watchdog or a guarantee against transient export. Confirm the Shelly CT location/polarity, fresh timestamp, signed export and real device latency.
- **Backup output:** upstream `_is_backup_function_active()` excludes a battery from PD when the backup port is serving genuine off-grid demand above a default **50 W threshold** (or the reading is unavailable), with a 5-minute cooldown. It explicitly treats confirmed *AC Bypass* differently. A permanently connected ~250 W server load **might** coexist with PD in bypass, but do not assume it will: check actual firmware states, meter boundaries and BACKUP transfer behavior with the real unit.
- **External overrides:** block unrelated automation/native economic plans from racing the MPC's three or more HA entity updates. Verify all high-level modes, command refusal and idle before any transfer back to a default native policy.

**Decision gate:** Implement and test the **existing HA-entity adapter** first in read-only/shadow and subsequently supervised laboratory operation. Propose a small upstream Omnibattery `economic_intent` API (one atomic mode/caps/target apply with source ownership, monotonic sequence, expiry and observed status) **only if** public entities prove insufficient or atomic leasing is required for a viable safety case. Even an HA-local TTL cannot solve HA-loss fail-safe; validate that independently on the hardware.

**Upstream source references (read 2026-10-24):**
- [Entity catalogue](https://github.com/ffunes/Omnibattery/blob/main/site-docs/reference/entities.md), [PD tuning/targets/caps](https://github.com/ffunes/Omnibattery/blob/main/site-docs/features/pd-controller.md) and [grid-meter cadence](https://github.com/ffunes/Omnibattery/blob/main/site-docs/configuration/main-sensor.md).
- [Config entities, hot reload and bound calculation](https://github.com/ffunes/Omnibattery/blob/main/custom_components/omnibattery/number.py), [direction switches and manual ownership](https://github.com/ffunes/Omnibattery/blob/main/custom_components/omnibattery/switch.py), [PD allocation, caps, setpoint overrides and backup exclusion](https://github.com/ffunes/Omnibattery/blob/main/custom_components/omnibattery/__init__.py).
- [Regression tests for grid-target bounds](https://github.com/ffunes/Omnibattery/blob/main/tests/test_target_grid_power_bounds.py) and [official automation changing the target via HA](https://github.com/ffunes/Omnibattery/blob/main/blueprints/different_grid_target_blueprint.yaml).

## Responsibility boundary

| Owner | Responsibility |
|---|---|
| Future app repository | Domain model, predictor, MPC, intent contract, HA/Prometheus adapters, replay tests, source CI, container build and release |
| Homelab repository | Desired runtime version, GitOps deployment, health/metrics scrape, secret references, private storage, local entity/tariff configuration, safety runbooks |
| Home Assistant / Omnibattery | Live battery feedback, exclusive hardware commands, fast grid-power regulation (subject to verification) |
| Hardware / electrical installation | BMS, inverter and independent electrical protections; must not rely on planner availability |

## Alternatives considered

| Alternative | Outcome |
|---|---|
| Keep all application source permanently in homelab | Rejected: ties application tests and image releases to home configuration changes |
| New PDR alongside the existing phased implementation plan | Rejected: duplicates phase gates and creates a second operational source of truth |
| Run optimizer as a 15-minute CronJob or AppDaemon script | Rejected: subscription/reconnect, state reconciliation and command verification favor a resident service |
| Microservices, Kafka/Redis/Celery or new PostgreSQL | Deferred: unnecessary for one battery and a small number of event streams |
| Direct battery Modbus writes from Python | Rejected: conflicts with Omnibattery's sole writer and closed-loop regulation |
| Full new repository/deployment in this PR | Deferred: separate migrations and activation gates need review |

## Consequences

Independent application versioning is cleaner, but deploying an app release now requires an explicit homelab digest bump. Tests and reproducible backtests move with the package; cluster and HA installation details remain local. The physical control interface and fail-safe behaviour are open **implementation blockers**, not solved by these technology choices.

The steps, proposed Kubernetes resources, CI expectations, API/metrics and acceptance checks live **only** in the [existing implementation plan](../../planning/home-battery-optimizer-implementation.md#16-production-packaging-and-runtime-rollout). No PDR is maintained.

**No separate repository, CI pipeline, image, Kubernetes Deployment, HA change, writer or live actuation is created or authorized by this ADR.**
