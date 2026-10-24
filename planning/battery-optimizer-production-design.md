# PDR: Battery optimizer production design and requirements

- **Status:** Proposed — documentation only, **not approved for actuation**
- **Date:** 2026-10-24
- **PDR means:** Production Design & Requirements
- **Decision:** [ADR-0004: production runtime and repository boundaries](../docs/adr/0004-battery-optimizer-production-architecture.md)
- **Safety basis:** [ADR-0001](../docs/adr/0001-home-battery-economic-dispatch.md)
- **Forecast features:** [ADR-0002](../docs/adr/0002-appliance-aware-load-forecasting.md)
- **Source data and acceptance:** [ADR-0003](../docs/adr/0003-phase1-measurement-and-baseline.md)
- **Existing extraction requirements:** [Energy Baseline — Production Execution Plan](energy-baseline-production-execution.md)
- **Existing maturity/status:** [Component roadmap](energy-battery-roadmap.md)

## 1. Outcome and non-goals

Produce a reproducible, observable, GitOps-delivered service that recommends economically optimal home-battery charging/discharging and eventually applies **guarded intents** through a verified single-writer HA/Omnibattery interface. Preserve the same offline physics, objective and prediction logic for replay and production; isolate real-time grid regulation from economic optimization.

**Not in this change:** creating the future source repository, moving code, building/publishing an image, adding Kubernetes resources, installing Omnibattery, writing to the Marstek, modifying AppDaemon schedules, granting new control credentials, or authorizing live dispatch. This PDR is an implementation proposal for separate PRs.

### Requirements summary

| ID | Requirement | Priority | How verified |
|---|---|---|---|
| RT-01 | One long-running Python planner; single active producer; default shadow-only | Must | Deployment review and restart tests |
| RT-02 | Share pure forecast/battery/MPC/intent modules with offline benchmarking | Must | Unit tests plus same-fixture replay |
| IO-01 | HA WS subscriptions, initial snapshot, reconnect/full reconcile, entity freshness | Must | Fake HA integration tests |
| IO-02 | Prometheus range queries for history; validate signs/units/gaps | Must | Recorded fixture tests |
| IO-03 | Publication-aware PVPC, DST-aware intervals, unknown-price uncertainty | Must | Price/time-travel fixtures |
| SAFE-01 | No second Modbus writer; explicit device ownership, only supported Omnibattery interface | Must | Hardware integration proof |
| SAFE-02 | No-export control uses **fresh net-grid metering**, not MPC intervals | Must | Meter/inverter fault tests |
| SAFE-03 | No commanded output after stale inputs, lease expiry or restart without revalidation | Must | State-machine fault tests |
| SAFE-04 | Verified safe handling of **HA/network loss** independent of HA control path | Must before unattended live | Physical supervised outage test |
| DATA-01 | Immutable, private as-of historical store and reproducible build/benchmark | Must | Existing regression harness |
| REL-01 | Separate app repository owns code/tests/container; homelab owns GitOps rollout | Must for first production release | CI and pinned image deployment PRs |
| OBS-01 | Metrics, structured logs, health, operator enable/disable and decision trace | Must | Alert/dashboards + fault drills |
| ML-01 | Appliance/NILM forecasting optional, gated on held-out net EUR uplift | Should | Walk-forward economic comparisons |

## 2. Proposed component inventory and ownership

| Runtime | Ownership | Implementation |
|---|---|---|
| `battery-optimizer` (Deployment, one replica) | Read HA and Prometheus; snapshot, forecast, solve, produce plans, command reconciliation in gated live mode | Python 3.12+, `asyncio`, `httpx`, SciPy/HiGHS, FastAPI status + prometheus-client |
| `energy-extract` (CronJob) | Read historical HA recorder and Prometheus; validate/deduplicate immutable manifests | Reuse `energy-baseline` extraction CLI, PyArrow/DuckDB, PVC |
| `energy-benchmark` (on-demand Job) | Evaluate baselines, economic margins, drift and proposed model changes; **no actuation** | Same Python package/model with read-only dataset |
| `energy-train` (later CronJob/on-demand Job) | Fit and evaluate optional advanced predictors; promote only after review | Optional CatBoost/LightGBM, versioned model artifact |
| Home Assistant + AppDaemon (existing) | Appliance state, HVAC/DHW control, PVPC sensor, battery integration and user controls | Existing `apps/home-assistant/`; do not change HVAC ownership |
| Omnibattery in HA (**installation and interface not validated**) | Shelly-feedback zero-grid-exchange loop and **only** physical battery writer | Existing upstream integration; propose upstream high-level intent extension if needed |
| Marstek Venus E V3 (**purchased, installation not validated**) | Local battery, inverter, BMS and native device protection | Verify firmware, Modbus TCP transport, AC wiring and backup output |
| Prometheus/Grafana/Alertmanager (existing) | Observability and historical numeric metrics, not real-time safety | ServiceMonitor/PrometheusRule/dashboard via homelab |
| Vault/ExternalSecrets (existing) | HA API token and other credentials, scoped | Existing homelab credential workflow |

### Read / decide / act paths

1. **HA event stream:** WebSocket `state_changed` and a startup/reconnect state-list reconcile → normalization by verified entity mapping → timestamped current snapshot. Includes battery SOC/AC power/health when installed; appliance state/program/phase/time remaining only where actually exposed.
2. **History:** Prometheus API for power/energy/lagged trends; extraction Job also reads HA recorder enum history within its retention. Keep distinct observed-at and received-at timestamps and preserve source lineage. Use the gross demand reconstruction of ADR-0001 once a battery is connected; never substitute grid import alone.
3. **Tariffs and schedules:** Published PVPC as HA entity or audited feed, applicable marginal tax/fees, optional forecast of unpublished intervals; observed AppDaemon planned HVAC/DHW schedule only. Nobody else modifies AppDaemon actions in this design.
4. **Decision:** Normalize inputs → forecast 15-minute demand (baseline first, quantiles later) → LP/MPC over 24–48 h via HiGHS → validate constraints and terminal SOC → emit current-slot `DispatchIntent`.
5. **Dispatch:** A read-only shadow sink is the initial output. In live mode a single adapter submits an *economic upper bound/target intent* to a validated HA/Omnibattery control surface. Omnibattery then clamps actual power against fresh net-grid feedback, import ceiling, SOC/BMS and hardware limits. **The API, cap semantics and expiry are to be established experimentally; no upstream API is asserted here.**
6. **Feedback:** Observe current actual AC power, grid exchange, SOC, command/ownership state and timeout behaviour. Compare intent vs actual; issue alerts; cancel economic mode on unproven responses. Do not mark physical idle as verified from a successful HA action call alone.

## 3. Application modules and suggested interfaces

All module names are placeholders; preserve pure model/forecast functions and separate adapters from domain code.

```text
battery_optimizer/
  domain/
    battery.py            # pure physical model; sourced from existing package
    forecast.py           # baseline and optional models
    optimizer.py          # solver and receding-horizon policy
    dispatch.py           # existing validated DispatchIntent/Plan contract
    pricing.py            # marginal tariff and publication validity
  adapters/
    home_assistant.py     # WS+REST, entity catalog, reconnect
    prometheus.py         # historical range/query
    tariffs.py            # external/HA tariff normalization
    omnibattery.py        # not writable until contract proven
    artifact_store.py     # immutable datasets and model loading
  runtime/
    snapshots.py          # as-of observations, freshness
    scheduler.py          # interval deadlines, debounced replan
    reconciler.py         # requested vs acknowledged vs measured
    safety.py             # app-level refusal; NOT hardware fail-safe
    settings.py
    app.py                # ASGI / health / status / metrics
  offline/
    extract.py
    benchmark.py
    train.py              # optional
tests/
  unit/
  integration/
  replay/
  fault_injection/
  hardware/               # opt-in, never default CI
```

### Runtime lifecycle

- **Bootstrap:** start without command authority; load validated configuration/model; start HTTP health/read-only status; connect to HA; subscribe before reconciling current entities so no gap is silently ignored; initialize latest Prometheus history and current tariff validity; require recent meter, SOC and physical state observations before considering actuation.
- **Main loop:** independent HA subscription consumer, freshness monitor, price/publication watcher, and scheduler. Trigger replan at 15-minute UTC interval boundaries and material changes (e.g. tariff publication, SOC deviation, major known appliance transition), with debounce/coalescing and bounded solver budget. Use aware timestamps and actual duration through Europe/Madrid 23/25-hour days.
- **Planning:** take one immutable `as_of` input snapshot, snapshot model/config version, forecast and optimize with a hard timeout off the event loop (bounded executor/process); coalesce or discard obsolete solves. At most one plan may be published as current per increasing sequence; reject stale generations and expired plans.
- **Dispatch:** independently validate lease, SOC, telemetry, limits, price validity, operator enablement, control ownership and physical safe state. Permit one command in flight; rate-limit/hysteresis; capture acknowledgements and physical feedback distinctly. A failed acknowledgement is **unknown physical state**, not proof of stop.
- **Shutdown/restart:** revoke local eligibility; invoke and independently verify idle where the path remains healthy; persist trace; do not transfer ownership without zero-power confirmation. If the path is unavailable, alert/require operator intervention and rely **only on demonstrated independent protection**, not on shutdown hooks. On restart default to no actuation pending full reconciliation.

### Minimum API (read-only, private network)

| Route | Description |
|---|---|
| `GET /health/live` | Process/event-loop liveness; must not depend on HA availability to prevent restart storms |
| `GET /health/ready` | API/runtime initialized; separately disclose whether economic dispatch is eligible |
| `GET /status` | Current mode, health/quality, latest plan ID, source ages, command/physical reconciliation |
| `GET /plan` | Current validated plan without secrets or household raw history |
| `GET /metrics` | prometheus-client output; no high-cardinality labels (entity timestamps, plan IDs or tokens) |

Use `ClusterIP` only, no public Ingress. Prefer UI/operator enablement in HA behind a **separate, explicit control contract**; do not add unauthenticated `POST /dispatch` or `POST /enable`. Read-only HTTP should still be restricted by namespace NetworkPolicies to monitoring/operator clients.

## 4. Data durability and forecasting contracts

- Initial runtime can be stateless apart from a read-only model artifact; reconstruct live state on restart. Keep a **durable private command/decision journal** if commands become enabled, with sequence, model/config hash, input provenance, requested/accepted/observed power, actor and incident outcome. Format and concurrency guarantees (e.g. single-writer SQLite WAL on dedicated RWO volume versus append-only files) must be selected and reviewed before live phase. Prometheus time series alone are insufficient to audit commands.
- Continue private append-only Parquet + manifests from `energy-baseline`, with DuckDB for local analysis. No automatic copy of sensitive household raw events to GitHub or image layers. Back up PVC data through an explicit retained policy; encryption/access must respect private household activity data.
- Avoid training leakage: snapshots at decision time have only actual available sources; observation, event, publication, ingestion and as-of times remain distinct. Preserve 15-minute interval duration and currency/tax provenance.
- For appliance signals, first use HA observed states as features in an **aggregate** forecast. NILM is optional, probabilistic and never a safety input. Do not add inferred appliance kWh to a forecast that already includes the same load.
- Advanced models are immutable, versioned artifacts; promote explicitly after head-to-head chronological economic replay, and always preserve the baseline fallback.
- Respect [manual extraction cadence](energy-baseline-production-execution.md): a three-day manual extraction with ten-day overlap currently protects HA's declared ten-day recorder retention. The production extraction CronJob is **not scheduled yet**. If introduced, verify effective coverage, dedup, overlapping run policy (`concurrencyPolicy: Forbid`), heartbeat/alerts and no silent loss on missed runs. Do not change the existing data identity rules casually.

## 5. Production topology and security baseline

Recommended first shipping manifests live **only** in `pando85/homelab`, after the application repository publishes a tested image:

| Kubernetes object | Proposed specification / boundary |
|---|---|
| `Deployment battery-optimizer` | 1 replica, RollingUpdate only if exclusivity is enforced end-to-end; prefer `Recreate`/explicit stop-and-verify on initial live phase so old/new pods never overlap |
| `Service + ServiceMonitor` | ClusterIP, read-only health/status/metrics endpoints; scrape at modest interval; do not use HA state IDs as Prometheus labels |
| `CronJob energy-extract` | Separate service account, read-only tokens and RWO PVC; `concurrencyPolicy: Forbid`, bounded deadline, scheduled re-extract and heartbeat; never import control credentials |
| `Job energy-benchmark` | Offline, on demand, read-only immutable historical dataset; no HA actuation privileges |
| `PVC` | ZFS/OpenEBS-backed data store when deployed; decide backup/retention explicitly, isolate from command journal if needed |
| `ConfigMap` | Schema-validated feature/entity maps, tariff mapping, 15-min cadence, limits from measured commissioning and selected policy; no credentials |
| `ExternalSecret` | Scoped HA token, optional protected tariff secret, least privilege; separate runtime read vs actuator authorization as far as HA supports |
| `NetworkPolicy` | Runtime → HA/Prometheus/tariff feeds only; runtime status → monitoring only; limit Modbus access to HA/Omnibattery, **never to optimizer** |
| `PrometheusRule` | Source freshness, reconnect storms, solver failures, expired intent, unacknowledged commands, unexpected observed power/export and missing extract heartbeat |
| `ArgoCD` | Sync desired resources via GitOps; pin OCI image digest from independently released application; **no automatic live-mode switch on deploy** |

### Operational controls

- Use a non-root image/user where possible, read-only root filesystem, explicit resource requests/limits, graceful termination deadline and no privileged host access. NumPy/SciPy dependencies and container architecture must be tested on the real K3s nodes.
- Config includes `control_mode=shadow` by default. Production `live` requires an additional explicit, persistent operator enablement and a separate physical commissioning sign-off. The kill switch should stop new intents and request/verify idle, **but cannot replace independent hardware safety**.
- At the controller boundary, reject stale source data, expired/inconsistent intent, observed export beyond accepted transient bounds, inconsistent meter sign, unknown ownership or a sequence regression. Log cause and refuse further live commands until the documented recovery condition is satisfied.
- Backup output (servers, router, ONT) must be electrically evaluated and separately tested: it may affect the grid meter balance and Omnibattery load accounting. Do not infer UPS/backup guarantees from economic dispatch.

## 6. Failure modes / expected behavior

| Fault | Runtime action | Required system-level guarantee |
|---|---|---|
| Optimizer crash/restart | Stop planning/output, enter shadow/ineligible until reconcile; report outage | Last economic intent must not produce uncontrolled indefinite output; prove via device/controller test |
| HA restart/unreachable | No new commands; mark HA-derived observations stale; alert | HA-local watchdog cannot work while HA is dead; prove **independent** expiry/idle or disable unattended live |
| Shelly stale/missing/misplaced | Reject all new economic actuation | Fast regulator must handle sensor loss safely and prevent intentional export; hardware supervised test |
| Battery offline or SOC/temperature missing | Command unknown; stop economic permission; alert | No false physical-idle acknowledgement |
| Tariff feed incomplete/late | No fabricated tomorrow price; use explicit forecast or price-only safe fallback when reviewed | No hidden future-data leakage |
| Solver timeout / invalid plan | Retain only valid unexpired plan for observation; reject new live output | No uncontrolled setpoint when economic plan unavailable |
| Restart/rolling upgrade with two pods | Only one active authority; no concurrent uncoordinated writes | Verified ownership and no split-brain; prefer Recreate until proven |
| Backup/off-grid load not represented in Shelly | Disable risky dispatch until topology correction | Avoid unsupported no-export claim and unmeasured consumption |

**Gate:** Passing Python fault-injection tests is insufficient to claim HA-outage safety. If the physical unit cannot enforce the safe behaviour while HA is down, the deployment can operate in **shadow** but must not be described as unattended zero-export-safe.

## 7. Application repository and release lifecycle

**Intended source split — not performed by this PR:**

| Repository | Owns | Does not own |
|---|---|---|
| Dedicated app repo (working name `pando85/battery-optimizer`) | `pyproject.toml`, lockfile, source, offline CLI, unit/integration/replay tests, simulator fixtures, Dockerfile, GH Actions, image/wheel releases, SBOM/provenance, developer docs and versioned APIs | Homelab-specific kube values, HA credentials, ArgoCD controller, private telemetry |
| `pando85/homelab` | `apps/battery-optimizer/` GitOps manifests, pinned image digest, secret refs, entity/tariff mappings, local PVC/backup, alerts, operator/rollback runbooks | Application business logic and build pipeline after migration |

### Proposed CI for the independent repo

1. Lint/format/type checks (Ruff + typing discipline) and package import/wheel checks; pin Python/build dependencies using a committed lockfile.
2. `pytest` unit/property tests for signed energy accounting, SOC/limits, negative prices, DST, solver validity, expiry and deterministic serialization.
3. HA/Prometheus/Omnibattery fake-adapter integration tests: delayed and out-of-order events, WS reconnect/reconcile, duplicate meter samples, command rejection, lease expiry and restart.
4. Deterministic replay and golden fixture tests against existing baseline and benchmark policy ladder; verify shared core and dataset provenance. Hardware-in-the-loop tests opt-in and supervised, never part of public CI.
5. Build immutable OCI image (multi-arch only as needed) and Python wheel; scan dependencies/images, produce SBOM/provenance and attach digest to a versioned release, for example in GHCR. Avoid leaking secrets/private datasets to artifacts.
6. A **separate homelab PR** updates the pinned digest and environment config; ArgoCD rolls it out in `shadow` first. Rollback uses the previous digest and manual safety/ownership verification, not an automatic change from live back to native while output is unknown.

### Migration checkpoints

- **M0:** document and agree contracts/ownership here; leave `scripts/energy-baseline/` untouched.
- **M1:** create independent repo and migrate intact Python package, tests and offline fixtures; both CLI and test suite pass in the new location. Update command docs without breaking source/provenance traceability.
- **M2:** add container runtime read-only/shadow with HA/Prometheus adapters, package/release CI and GHCR image. No live code or secrets needed.
- **M3:** create homelab Deployment/Service/ServiceMonitor and extraction CronJob **by separate PRs** after runbook and data-retention review; observe for at least two representative weeks.
- **M4:** validate the Omnibattery economic-intent extension and hardware shutdown/failures under supervision; only then write/enable a tested single-writer interface under explicit approval.
- **M5:** progressively roll out limited discharge → limited charge → full *measured* allowable power, with independent evidence, alerting and rollback gates from ADR-0001.

## 8. Acceptance and non-acceptance

### Gate A — application/service readiness (shadow)

- Dedicated-repo CI builds the same importable domain engine used by CLI and service; backtests remain reproducible and show no invalid simultaneous charge/discharge or fictitious export.
- One K3s service reconnects to HA and restores as-of entity state without reusing stale readings; Prometheus history and PVPC data quality are visible.
- Every emitted intent is schema-valid, has time/sequence provenance and can be observed in `shadow`; no output transport to battery exists.
- Structured status, Prometheus alerts, HA outage behavior and extraction heartbeat/coverage are demonstrated. Operator has a comprehensible deployment/rollback runbook.
- Historical prices/efficiency caveats are honest: the current 0.80 round-trip efficiency is an assumption, not device calibration; demand forecasts/rolling-horizon rankings require like-for-like evaluation on current data.

### Gate B — live actuation (separate explicit approval)

- Meter placement/direction, import limit, electrical protection, backup wiring, actual Marstek firmware/control path and Omnibattery behavior are independently verified.
- Owner exclusivity is enforced at Omnibattery, not merely by Kubernetes replica count; manual mode is not mistaken for auto-PD regulation.
- Fresh-meter cap, permitted power/SOC, command acknowledgement **and measured output**, expiry behaviour and stop transition all pass real-device tests.
- HA/optimizer/Modbus/network failure tests establish an **independent fail-safe** (or the feature remains disabled). No claimed no-export protection exists based solely on HA timers.
- At least two weeks in shadow and progressive controlled rollout under ADR-0001/roadmap gates, with zero unexplained output and observable import/export transients.
- Rollback can be performed locally without K3s/Internet, including manual intervention when physical idle cannot be confirmed.

## 9. Open decisions for later PRs

1. Final new repository name, licensing and image registry coordinates; do not treat the working name as created.
2. Whether Omnibattery already exposes a compatible high-level external constraint API, or needs an upstream contribution; define supported direction/power bounds, expiry and measured acknowledgment.
3. How the actual Marstek behaves on HA outage or transport loss, and whether a physically independent no-export/idle fallback exists.
4. Exact Shelly net-grid entity and signs, independent charging import ceiling and how BACKUP loads affect metering/Omnibattery regulation.
5. Persistence of the command journal, HA least-privilege limitations, realistic input freshness thresholds, worker CPU/memory and backup/restore budgets from commissioning measurements.
6. Whether fine-cadence rolling horizon and richer appliance-conditioned forecasts yield **greater net savings** than the simpler policies under aligned causal training/replay; no advanced model is mandatory.

**No automatic actuation or new deployment is authorized by approving these design documents alone.**
