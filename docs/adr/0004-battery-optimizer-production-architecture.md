# ADR-0004: Production runtime and independent source repository for the battery optimizer

- **Status:** Proposed
- **Date:** 2026-10-24
- **Decision owners:** Homelab maintainers
- **Extends:** [ADR-0001 — economic dispatch](0001-home-battery-economic-dispatch.md)
- **Implementation and acceptance:** [Home battery optimizer implementation plan](../../planning/home-battery-optimizer-implementation.md) (`16, plus Phases 5–6)
- **Current component state:** [Battery roadmap](../../planning/energy-battery-roadmap.md)

## Context

[ADR-0001](0001-home-battery-economic-dispatch.md) already decides the economic MPC, single-writer Omnibattery ownership and safe-dispatch constraints. [ADR-0002](0002-appliance-aware-load-forecasting.md) and [ADR-0003](0003-phase1-measurement-and-baseline.md) cover forecasting inputs and the offline data pipeline. **This ADR only decides how the optimizer becomes a separately released production application.** It does not redefine physical dispatch or replace those decisions.

The current `scripts/energy-baseline/` Python package can extract Prometheus/HA data, create reproducible offline datasets, compare policies and serialize typed dispatch intents. It has no live writer, runtime service or deployed safety guard. HA, AppDaemon, Prometheus, Grafana, Vault and ArgoCD already exist in the homelab.

Application code, test suite and container artifacts need an independent release lifecycle; homelab-specific cluster configuration must remain GitOps-managed. We want minimal new operational dependencies.

## Decision

1. **Separate repositories, without moving code in this PR.** A future dedicated optimizer repository (working name `pando85/battery-optimizer`, not yet created) will own Python code, offline CLI, tests/fixtures, dependency lockfile, Dockerfile, CI and immutable OCI image releases. `pando85/homelab` will own the pinned image digest, ArgoCD/Helm manifests, HA entity mappings, secrets references, PVC, policies, dashboards and operator runbooks. Keep the current `scripts/energy-baseline/` implementation unchanged until a dedicated migration PR preserves tests, CLI and historical dataset provenance; its dataset IDs currently depend on the source repository commit.

2. **One small, continuously running Python service.** Deploy a single-replica `battery-optimizer` process in K3s, starting in **shadow mode**. Its modules consume current HA entity states/events through authenticated WebSocket/REST, use the Prometheus HTTP API for historical measurements, validate PVPC tariff publication times, compute forecast + receding-horizon MPC using the **same pure code as the offline benchmarks**, and emit validated `DispatchIntent` messages. Use Python 3.12+, `asyncio`/`httpx`, the existing SciPy/HiGHS stack, a small internal HTTP status/metrics API (FastAPI is a reasonable choice) and `prometheus-client`. Do not add a broker, cache cluster or dedicated DB solely for this workload. Keep blocking solves off the event loop.

3. **Historical data remains batch.** Run `extract/validate/build` and optional benchmark/training as separate, read-only Jobs/CronJobs using private Parquet manifests and DuckDB, according to the [existing extraction plan](../../planning/energy-baseline-production-execution.md). An extract schedule must retain HA recorder events before its declared retention expires. Model training is optional and only promoted after chronological, out-of-sample **net savings** validation.

4. **Planner intents are not physical setpoints.** Omnibattery in HA remains the sole Modbus writer and fast regulator using *fresh net-grid Shelly measurements*. The planner must not write Modbus concurrently. A supported high-level economic constraint/intent interface is **not yet verified**. Omnibattery manual mode can exclude the battery from automatic PD regulation, so manual mode cannot be assumed to retain no-export feedback. An upstream-compatible extension may be required; before that integration is proven, the service can plan only in shadow mode.

5. **Physical failure behaviour gates live operation.** A lease, HA automation, pod termination handler or API acknowledgement does **not** guarantee 0 W when HA/network/optimizer fails. The actual Marstek, meter topology, backup circuit and controller must be commissioned and tested under supervised failures; independent device/installer protections remain authoritative. No unattended live dispatch until the measurable power response, ownership transition, safe stop and no-export behaviour under failure are established. A new pod must never resume actuation just because a prior plan was persisted.

6. **Independent release, reviewed GitOps promotion.** The future app repo builds/tests/releases a pinned wheel and container image (e.g. GHCR digest, SBOM and provenance where feasible). A separate homelab PR updates the image **by digest**, runs shadow validation and gates live mode explicitly; no automatic change of control authority on image upgrade. Initial Deployment has one replica and no HPA. A replica count of one is **not** an exclusivity guarantee: enforce ownership inside the HA/Omnibattery boundary.

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
