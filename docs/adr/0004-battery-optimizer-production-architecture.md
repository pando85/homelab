# ADR-0004: Production runtime and repository boundaries for the home battery optimizer

- **Status:** Proposed
- **Date:** 2026-10-24
- **Decision owners:** Homelab maintainers
- **Extends:** [ADR-0001 — economic dispatch](0001-home-battery-economic-dispatch.md), [ADR-0002 — appliance-aware forecasting](0002-appliance-aware-load-forecasting.md), [ADR-0003 — measurement and baseline](0003-phase1-measurement-and-baseline.md)
- **Detailed requirements:** [Battery optimizer production design (PDR)](../../planning/battery-optimizer-production-design.md)
- **Scope:** Software ownership, production topology, integrations, deployment and release boundaries. This ADR **does not authorize live actuation**.

## Context

The existing `scripts/energy-baseline/` Python 3.12 package already has reproducible Prometheus/Home Assistant extraction, immutable Parquet/DuckDB datasets, an offline battery model, five benchmark policies, a configurable 15-minute rolling-horizon solver and a typed `DispatchIntent` / `DispatchPlan` contract. They do not form a production service. The dispatch contract has **no I/O, writer or lease implementation**; there is no live battery controller, safety guard or Kubernetes deployment.

Home Assistant (`apps/home-assistant/`) and AppDaemon already run in K3s. Prometheus collects Shelly measurements; HA exposes PVPC, appliance entities and existing HVAC/DHW scheduling. The grid meter's electrical position, polarity and freshness, the purchased battery's actual firmware/Modbus interface, and the backup circuit's interaction with Omnibattery still require field verification.

We want to operate independently versioned, tested and released optimizer code, rather than permanently developing application code inside the homelab GitOps repository. We also want a small operating footprint, one clear actuator owner and reproducible comparisons between offline simulation and live decisions.

## Decision

### 1. One continuously running Python planning service, not a monolithic HA automation

Build a **single-replica Python 3.12+ Deployment** for the economic planner. Organize it as modules within one process:

1. `ha_adapter`: authenticated HA WebSocket subscription for state changes, reconnect/reconciliation and read-only REST calls; use event time, observation time, availability and freshness. Never assume an HA entity has a particular program/phase/power attribute without an inventory.
2. `metrics_adapter`: historical Prometheus HTTP range queries for power and energy, plus source/lineage validation. Do **not** use Prometheus as the second-by-second closed-loop grid controller.
3. `tariff_adapter`: validated, publication-aware PVPC/marginal import prices; optional OMIE estimate with explicit uncertainty and published-at timestamps. Keep tariff applicability and Europe/Madrid DST accounting from ADR-0001.
4. `state_store`: in-memory, timestamped snapshot of current SOC, battery/grid telemetry, appliance observations, known heating schedules and health. Reconcile after reconnection/restart; never treat the last value as current indefinitely.
5. `forecast` and `optimizer`: reuse the same pure physical model, feature contracts, forecasts and SciPy/HiGHS planner as offline backtests. Replan on a configurable 15-minute cadence and sufficiently significant input/publication changes, with debounce; only the current interval can become an intent.
6. `dispatch`: validate and submit typed `DispatchIntent` (mode, power, SOC target, generated/expiry timestamps, sequence) **only through an explicit, tested HA/Omnibattery adapter**, with a shadow-mode default. Observe the actual device response separately from the requested power.

Use `asyncio` for long-running subscriptions/timers; use `httpx` for HTTP, `FastAPI` (or a small equivalent ASGI layer) for internal status, health and metrics, and `prometheus-client` for metrics. Run any blocking optimization in an executor or isolated worker so WebSocket freshness/health checks are not starved. Avoid exposing a public control API.

### 2. Keep fast physical regulation out of the economic planner

Omnibattery inside HA remains the **only battery Modbus writer** and owns the fast Shelly grid-feedback loop. The planner supplies bounded *economic intent*, not raw inverter registers or continuously updated physical setpoints.

**Important unresolved integration contract:** Omnibattery documents automatic PD control, time slots and manual ownership, but a supported external intent API that preserves automatic PD while accepting a dynamic economic charge/discharge ceiling has **not** been demonstrated. Existing manual mode removes the battery from the automatic control pool; it is **not** an acceptable substitute for a zero-export feedback loop.

Prefer an upstream-compatible extension/high-level adapter that lets Omnibattery own all physical writes while accepting bounded economic constraints. Until the real supported API, command acknowledgement, expiry semantics, no-export behaviour and safe mode transitions are verified **there is no live dispatch**. The planner must never compete with Omnibattery via a second Modbus connection.

### 3. Do not confuse economic-command expiry with a fail-safe

A planning lease prevents stale *application intent* when participating components are alive; it cannot stop an inverter if HA, the host or network has failed. Neither a HA timer, Kubernetes probe nor a Modbus write acknowledgement proves actual power is zero.

Require independent hardware/inverter protection and installer verification. Before unattended dispatch, test and document loss of HA, Shelly readings, optimizer pod, network, command acknowledgement and controller ownership. Verify the battery's observed physical response and a **provably enforceable fallback** on the actual unit. If there is no fail-safe independent of the failed HA/control path, classify no-export on HA failure as **unproven**, keep economic actuation disabled, and require supervised or manual mitigation. Backup loads must be tested separately from GRID-side regulation. Existing AppDaemon HVAC/DHW authority is unchanged.

### 4. Separate live service from historical extraction and model training

Run historical `extract → validate → build → benchmark/report` operations as independent **Kubernetes CronJobs/Jobs**, backed by a private persistent data volume when approved. Keep current append-only, content-addressed Parquet storage with manifests; DuckDB is a local analytical reader, **not** a new database server. Do not introduce PostgreSQL/Redis/Kafka/Celery just to schedule one household battery.

Follow the existing [production extraction plan](../../planning/energy-baseline-production-execution.md) rather than silently replacing its present manual 3-day/10-day-overlap policy. Training and model promotion are separate batch operations, optional until out-of-sample **net EUR uplift** justifies advanced forecasting. No experiment may silently change the live model or actuation configuration.

### 5. Separate application source/release lifecycle from homelab GitOps lifecycle

**Target:** a future dedicated application repository (working name `pando85/battery-optimizer`, **not created by this ADR**) owns Python source, package, test fixtures, lockfile, Dockerfile, CI, image publishing, developer docs, typed contracts and versioned releases. Choose its final name during extraction.

`pando85/homelab` continues to own the deployment manifests/Helm values, ArgoCD Applications, ServiceMonitor, NetworkPolicies, Vault/ExternalSecrets references, resource/storage configuration, model-selection configuration and local runbooks. The production Deployment references a pinned OCI image digest (`ghcr.io/...@sha256:...`); application release and GitOps rollout are **separate reviewable changes**. No unpinned `latest` image and no image build as a prerequisite for each homelab configuration edit.

Migration of `scripts/energy-baseline/` occurs in an explicit follow-up: preserve code and tests, existing CLI and replay artifacts, dataset/provenance semantics, and avoid divergent dual-maintained copies. Historic dataset identifiers depending on Git HEAD or config hashes must retain traceability across repository migration; do not claim old/new runs are byte-identical without rerunning and checking.

### 6. Operate with one active authority

Start with one Deployment replica (no HPA). Reconciled, recent data and explicit operator enablement are prerequisites for **every** output command. Keep shadow mode as the default until hardware and failure-injection gates pass. On startup, reconnect, restart or lost state, refuse actuation until ownership, physical idle behaviour and health have been re-established. Single replica is operational simplicity, **not** a distributed lock: HA/Omnibattery itself must enforce exclusive authority and safe handoff.

Make plans, decisions, actual battery telemetry, import/export behaviour, source freshness, forecast quality and estimated/realized costs visible to Prometheus/Grafana. Separate model-scoring observations from actuator assertions; never count projected savings as realized bill reductions. Internal HTTP endpoints are read-only unless a separately reviewed authenticated operator mechanism is introduced.

## Topology (logical)

| Plane | Component | Traffic / authority |
|---|---|---|
| Read/observe | HA WebSocket + REST | Current entities and battery feedback (read-only for planner inputs) |
| Read/observe | Prometheus HTTP API | Recent/historical time series; no fast control |
| Read/observe | Tariff feeds | Publication-aware prices and estimated future prices |
| Decide | `battery-optimizer` Deployment | Snapshot → demand forecast → receding-horizon MPC → typed intent |
| Act | Supported HA/Omnibattery intent adapter **(to be designed)** | Exclusive bounded economic intents; mode transitions and acknowledgements |
| Physical control | Omnibattery + battery/BMS | Shelly-feedback power regulation + native independent protections |
| Batch | Extraction and optional training Jobs | Private immutable datasets, models, reproducible comparison |
| Observe | `/metrics`, ServiceMonitor, Grafana, Alertmanager | Diagnostics/alerts; no authority to actuate |

## Alternatives

| Alternative | Decision / reason |
|---|---|
| Put MPC into AppDaemon/HA automation | Rejected: mixes analytical computation with physical control, complicates tests/releases and HA outage isolation |
| CronJob every 15 min as the entire optimizer | Rejected: poor fit for event subscriptions, state reconstruction and live command reconciliation; still appropriate for batch extraction |
| Go/Rust runtime with Python ML sidecars | Deferred: adds cross-language/protocol complexity to a working Python numerical core without demonstrated need |
| Separate microservice per model, broker and a new operational DB | Rejected for v1: single-home workload does not justify distributed control surface |
| Python writes Modbus directly beside Omnibattery | Rejected: dual physical writers and loss of single fast control authority |
| Reuse Omnibattery manual mode as though it kept auto PD | Rejected unless independently verified; documented handoff removes the battery from its automatic pool |
| Implement all application source permanently in homelab | Rejected as end-state: couples testing/image release to local infrastructure changes |
| Create/deploy new application repo now | Deferred: this ADR documents intended ownership; extraction, CI and first runtime image need their own PR and acceptance |

## Consequences and decision gates

**Benefits:** independently released Python package/container; same model for backtest and runtime; minimal cluster additions; clear read/decide/act boundary; native observability and GitOps. **Costs:** adapter to HA and potentially Omnibattery upstream work, reconnect/replay correctness, operational safeguards and release coordination between repositories.

**Open gates:** qualified electrical installation, signed Shelly at net-grid point, measured battery behaviour and Modbus capabilities, Omnibattery intent semantics, demonstrable safe-stop under HA/network loss, backup-circuit interaction, billable PVPC reconciliation, repeatable live shadow evidence and rollback runbook.

All prior economic and safety constraints from ADR-0001 through ADR-0003 remain binding. This ADR contains **architecture only**: no independent repository, CI, image, Deployment, secret, Omnibattery change, automatic command or battery activation is created here.
