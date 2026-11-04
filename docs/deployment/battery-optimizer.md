# battery-optimizer

Two workloads of [pando85/home-energy-optimizer](https://github.com/pando85/home-energy-optimizer), deployed
from `apps/battery-optimizer/` with one image, one PVC and one ConfigMap:

- **`pipeline`**: a CronJob that extracts Home Assistant recorder history and Prometheus samples
  into an append-only Parquet store, validates it, and builds the 15-minute analysis dataset
  (Phase B).
- **`service`**: the resident Phase C **shadow service** (`battery-optimizer serve`). It reads Home
  Assistant and Prometheus, solves the MPC plan every 15 minutes and records shadow intents and a
  shadow-vs-native comparison. In `shadow` (deployed) it has no actuation path and no write
  credentials; `dry_run` and `live` add the battery actuator of ADR-0007, see
  [Live battery control](#live-battery-control). The production plan and its history live in
[`planning/energy-baseline-production-execution.md`](https://github.com/pando85/homelab/blob/master/planning/energy-baseline-production-execution.md).

The application code, ADRs and domain docs live in the app repository; this page covers only the
deployment.

## What runs

| Item | Value |
|---|---|
| Namespace / ArgoCD app | `battery-optimizer` (ApplicationSet auto-discovery) |
| CronJob | `battery-optimizer-pipeline`, `30 3 * * *` UTC, `concurrencyPolicy: Forbid` |
| Image | `ghcr.io/pando85/battery-optimizer` (keeps its name after the repo rename until the staged OCI rename), pinned `tag@sha256`, Renovate-managed |
| Steps (one container, `sh -ec`, fails fast) | extract `ha-history` (8 d lookback) -> extract `prometheus` (3 d lookback) -> `validate` -> `build` |
| Failure handling | `backoffLimit: 2`, `activeDeadlineSeconds: 3600`, `startingDeadlineSeconds: 21600`, history 3/3 |
| Node | pinned to `prusik` (node-local `openebs-zfspv`) |
| Secret | ExternalSecret `battery-optimizer-home-assistant` <- Vault KV `secret/battery-optimizer/home-assistant`, property `token` (read-only HA token, env only, never on disk). Only with `global.liveHomeAssistantToken: true`: ExternalSecret `battery-optimizer-home-assistant-live` <- `secret/battery-optimizer/home-assistant-live` (service only, see [Live battery control](#live-battery-control)) |
| Config | ConfigMap `battery-optimizer-config` rendered from `apps/battery-optimizer/files/` (`tariff.yaml`, `service.yaml`, and the climate shadow's `climate.yaml`, `climate-mpc.yaml`) |
| Storage | PVC `battery-optimizer-data`, 5 Gi, `openebs-zfspv`, mounted at `/data` (`BATTERY_OPTIMIZER_DATA_DIR`) |
| Backup | labels `backup: battery-optimizer-zfs`, `backup/retain: quaterly`; SnapshotSchedule `battery-optimizer-zfs-backups` (daily 06:00 UTC, keep 20) |
| Alerts | `PrometheusRule/battery-optimizer` (label `release: monitoring`) for the CronJob, `PrometheusRule/battery-optimizer-service` (long-term) for the service |

## Design notes and gotchas

- **Why daily with an 8 day HA window.** HA's recorder keeps 10 days (`purge_keep_days` default,
  nothing overrides it). Rows older than that are gone for good, so the binding rule is "never more
  than 10 days between two successful runs". Daily with an 8 day lookback tolerates 7 missed runs. The window is deliberately shorter than the
  retention: the recorder purges nightly, so the oldest chunk of a 10 day window is partly or wholly
  gone and would raise a coverage gap every night (see the next note). Re-pulling is
  free because the HA extractor dedups against rows already in the store. Chunk width stays at the
  CLI default (86400 s); changing it on a live store mints new carry-in anchors.
- **Prometheus extraction is not deduplicated across runs**, so the lookback is only 3 days
  (about 40 KB per day-part). The Prometheus archive has no retention limit, so there is no urgency.
- **Bootstrap.** If `/data/manifests` holds no finished Prometheus extraction (empty PVC), the run
  extracts Prometheus from `PROM_BOOTSTRAP_START` (2024-01-01) instead of the 3 day lookback. A
  manifest is written only by a finished run, so an interrupted bootstrap retries from scratch.
- **Build cadence and window.** `build` is content-addressed and write-once, and measured on the
  cluster **every new extraction changes the dataset id** (the part list changes), adding about
  4.2 MB per run (~1.5 GB a year if built daily) while nothing consumes the dataset yet. The job
  therefore builds only on UTC days 01 and 02 of the month (`BUILD_ON_DAYS`), over the complete
  months `[BUILD_START, first of this month)`; other days log `build skipped`. The newest partial
  month is not in the dataset until the month closes. There is no CLI-side garbage collection of
  superseded datasets. Run a manual build job if you need one off-cycle (steps 1-3 still run).
- **Permissions.** No `fsGroup` (slow startup on `openebs-zfspv`). A fresh ZFS dataset is
  `root:root 0755`, so the `fix-permissions` init container chowns the volume **root** to 65532
  (recursively only if the root is not already owned by 65532). See
  [`ci-runner-workspace-permission-denied.md`](../troubleshooting/ci-runner-workspace-permission-denied.md).
- **Alerts use `release: monitoring`, not `operator.prometheus.io/instance: prometheus-long-term`.**
  kube-state-metrics and kubelet series exist only in the main Prometheus; a rule labelled for
  `prometheus-long-term` would load but never fire.
- **No NetworkPolicy.** An egress-only policy (DNS, HA 8123, long-term Prometheus 9090) ran from
  0.2.1 until 0.3.0 and was removed on 2026-10-25: battery-optimizer is first-party software published
  from our own repository, and the repo keeps NetworkPolicies for workloads that are not under our
  control (LLM harnesses). Outside `live` its only credential is the read-only HA token and it has no
  actuation path; in `live` the HA client can send only the allowlisted Omnibattery writes (ADR-0007).
- **The two power signals come from a Gen1 Shelly EM over MQTT at 2 s; the counters still poll.**
  `shellyem-main` (`192.168.86.130`) publishes `shellies/shellyem-244CAB417668/emeter/{0,1}/power` to
  mosquitto every **2 s** (`mqtt_update_period: 2`) and HA consumes them via
  `apps/home-assistant/config/integrations/shelly_em_mqtt.yaml`. Since the 2026-10-27 cutover,
  `grid_power` and `observed` carry those IDs from the **`mqtt`** platform, measured as
  `battery_optimizer_input_age_seconds{role="grid_power"} = 2.3 s` (was 31.9 s). `grid_energy` and
  everything else still arrive over the native integration at `2.2 × 15 s = 33 s`, which is correct for
  a monotonic counter consumed as 900 s deltas — a faster transport would add recorder rows without
  adding information. The device still carries a permanent error-level `push_update_*` repair, because
  CoIoT is multicast-only (`peer: ""`) and nothing forwards it to the HA pod; that is a hygiene item,
  not a data problem.
  A faster transport initially looked unusable — the device reboot-looped — but that was
  **mis-diagnosed**: the loop needs MQTT *and* `coiot.enabled: false` *and* an HA `setup_retry` storm,
  so MQTT is safe as long as CoIoT stays enabled
  ([`planning/shelly-em-mqtt-implementation.md`](../../planning/shelly-em-mqtt-implementation.md)).
  Do **not** rename these entities or the device: `friendly_name` is a Prometheus label and part of
  the archive's `series_key`. Renaming also *moves* recorder history — if an ID must ever change hands
  again, use remove-then-promote
  ([`home-assistant-entity-id-handover.md`](../troubleshooting/home-assistant-entity-id-handover.md)).
- **Never disable CoIoT on that device, and treat HA restarts as a checked operation.** With
  `coiot.enabled: false` the firmware stops serving `/cit/*`, HA's `initialize()` hangs on
  `GET /cit/d`, and the *next* HA restart drops all 14 `shellyem-main` entities into `setup_retry`.
  That happened on 2026-10-26 and cost ~48 min of history (18:02:35Z-18:50:34Z). Since the MQTT cutover
  the blast radius is narrower but not gone: `grid_power` and `observed` would keep working (they come
  from the broker), while **`grid_energy` — what the dataset is actually built from — would be lost**,
  and MQTT being permanently enabled means the reboot-loop co-factor is always present. Before any HA
  ConfigMap change (Reloader restarts the pod), verify `GET /cit/d` still answers `200`. Recovery,
  correct endpoint and pitfalls:
  [`shelly-gen1-coiot-disabled-breaks-ha-setup.md`](../troubleshooting/shelly-gen1-coiot-disabled-breaks-ha-setup.md).
- **`grid_energy` still takes ~20 poll timeouts a day; `grid_power` no longer does.** The native
  `/status` REST poll occasionally exceeds `DEVICE_IO_TIMEOUT = 10 s` (~19.7 `unavailable` rows/day on
  `metadata_id` 182 over a 10-day window, dating back before MQTT existed), which flips the whole block
  to `unavailable` at `T + 33 s + 10 s`. In the Prometheus archive those become missing 60 s samples —
  the 2026-10-27 03:30Z run saw 4420 of ~4526 expected over 3 days and still reported `violations=0`.
  Watch for it if the coverage check below ever starts failing: the fix is to move the counters to MQTT
  too, which has so far not been worth the recorder cost.
- **`tariff.yaml` and `service.yaml` are vendored copies** of `config/` in the app repository (the
  image carries no config). Update both together; the tariff config hash is part of every dataset id.
  The CronJob mounts the same ConfigMap at `/config` and sets `BATTERY_OPTIMIZER_SERVICE_CONFIG=/config/service.yaml`,
  so `build` and `extract` read `battery.commissioned_at` and the battery counters from the same
  `service.yaml` as the service; set them there on installation day and nothing else needs changing.
- **Coverage gaps fail the Job.** From 0.2.1 `extract` exits 3 when the run's manifest records a
  coverage gap (HA: `empty_chunk`, `empty_chunk_after_window_filter`, `chunk_accounting_mismatch`,
  `no_rows_for_entity`, `no_rows_extracted:all`; Prometheus: `no_samples_extracted`,
  `no_series_chunks`, `series_without_samples_chunks`). The data and manifest are kept, but the Job
  fails and `BatteryOptimizerJobFailed` fires, so a silent gap is no longer silent. The pipeline does
  **not** pass `--allow-coverage-gaps` (except for the one-off Prometheus bootstrap, whose 2024
  history has known holes). If a window legitimately has no data, fix the window rather than opting out.

## Shadow service (Phase C)

| Item | Value |
|---|---|
| Workload | Deployment `battery-optimizer-service`, 1 replica, `strategy: Recreate`, same image anchor as the CronJob |
| Command | `battery-optimizer serve`, port 8080: `/health/live`, `/health/ready`, `/status`, `/plan`, `/metrics` |
| Config | `files/tariff.yaml` + `files/service.yaml` (vendored from the app repo `config/`) in ConfigMap `battery-optimizer-config`, mounted at `/config`. `service.yaml` uses `soc_source: simulated` because Home Assistant has no battery entities yet |
| HA token | the same ExternalSecret as the pipeline, **mounted as a file** (`BATTERY_OPTIMIZER_HA_TOKEN_FILE`). Read-only, non-admin. With `global.liveHomeAssistantToken: true` the live user's token is mounted at the same path instead ([Live battery control](#live-battery-control)) |
| Storage | the same PVC `battery-optimizer-data` at `/data` (RWO on prusik, `Recreate` avoids two pods). The service writes only `/data/service/v1/`; the pipeline uses `raw/`, `manifests/` and `analysis/`, so the paths never collide |
| Resources | requests 100m / 256Mi, memory limit 512Mi, no CPU limit (observed ~125-150 MiB RSS) |
| Probes | liveness **and** readiness on `/health/live`; generous startup probe. `/health/ready` returns 503 whenever an input is stale, which would remove the endpoint and stop the scrape exactly when the metrics matter, so alert on `battery_optimizer_ready` instead |
| Service / scrape | ClusterIP `battery-optimizer:8080` (`http`); ServiceMonitor labelled `operator.prometheus.io/instance: prometheus-long-term` (job `battery-optimizer`, 30 s), so `battery_optimizer_*` lives in the long-term Prometheus for the multi-week comparison |
| Alerts | `PrometheusRule/battery-optimizer-service` (long-term Prometheus): `BatteryOptimizerServiceDown`, `ServiceAbsent`, `NotReady` (30 m), `HomeAssistantDisconnected` (10 m), `GridPowerStale` (15 m), `PlanStale` (1 h), `SolveFailures`, `SinkWriteErrors`, `SolvesSkipped`, `HistoryStale`, the [climate shadow](#climate-shadow-track-b2) alerts (`rules-service/climate-shadow.yaml`) and the [live control](#alerts) alerts (`rules-service/actuator.yaml`) |
| Dashboard | ConfigMap `dashboards-battery-optimizer`, Grafana folder `battery-optimizer`, datasource `prometheus-long-term`: `Battery optimizer shadow service` and `Battery optimizer climate shadow` |

**Passive archives (0.3.0).** The `archive:` block of `service.yaml` turns on two append-only
collectors under `/data/service/v1/`: `pvpc_snapshots/` (every state change of the PVPC price sensor,
to measure when next-day prices really appear) and `weather_forecasts/` (`weather.get_forecasts` for
`weather.forecast_home`, hourly at HH:05 UTC). The weather query is the single `call_service` frame
the HA guard lets through; every other service call is still refused in code. Watch
`battery_optimizer_pvpc_snapshots_total`, `battery_optimizer_weather_forecast_fetches_total{result}`
and `battery_optimizer_archive_last_success_timestamp_seconds`. There is no retention yet.

The shadow store (`/data/service/v1/`) is private household data and is covered by the same PVC
snapshots. Read the comparison with `battery-optimizer shadow-report` from a pod or a copy of the
store; `comparison_basis` stays `no_battery_pre_commissioning` until battery telemetry is mapped, and
that basis does not satisfy exit gate C.

Checks without a port-forward:

```bash
kubectl --context=grigri -n battery-optimizer exec deploy/battery-optimizer-service -- \
  python -c "import urllib.request as u; print(u.urlopen('http://127.0.0.1:8080/health/ready').read().decode())"
```

## Climate shadow (track B2)

From 0.4.0 the service also runs the climate shadow (`climate_shadow:` in `files/service.yaml`): the
thermal model and the price-blind comfort MPC of the app repository on live data. Every hour at HH:10
UTC, on its own worker thread, it reads, predicts, plans, records and scores. **It is read-only and
actuates nothing**: the plan is written to the PVC and never sent anywhere, it uses no new credential,
and the HA guard still refuses every service call but `weather.get_forecasts`. App-side reference:
`docs/service.md#climate-shadow-track-b2` and the preregistration
`docs/planning/climate-preregistration-2026-27.md` in the app repository.

| Input | Source |
|---|---|
| Room temperatures and setpoints (four thermostats), Aquarea outdoor sensor, outlet target, compressor frequency, heat / DHW / cool power consumed and produced, force-DHW switch, legacy AppDaemon enable and minimum hours, PVPC | long-term Prometheus (`hass_*` series), 30 days fetched at the first run and then incrementally |
| `input_boolean.comfort_control_enable` (regime), `input_text.comfort_dhw_plan`, `input_boolean.comfort_away` | Home Assistant, through the existing read-only session; each may be absent. Until the comfort controller is deployed they do not exist and every record is regime `legacy` |
| Outdoor forecast | the service's own weather archive (`weather_forecasts/`, fetched at HH:05) |

**Failure isolation.** A missing prior, site file or config, a Prometheus error or a solver failure is
logged (`climate shadow not loaded: ...`, `climate shadow run failed`), counted, written as a
`climate_shadow_error` event and retried the next hour. It never changes readiness and never touches
the battery planner, which keeps solving every 15 minutes. A healthy job logs nothing; check `/status`.

**Files.**

| Path | In git? | Content |
|---|---|---|
| `/config/climate.yaml`, `/config/climate-mpc.yaml` | yes, `files/` (public in the app repo too) | Comfort rooms, filters, MPC horizon and schedule. Keep them **byte-identical** to the app repository: their sha256 is part of every climate record's lineage |
| `/data/analysis/v1/climate-shadow/priors/<prior>/prior.json` | **no**, PVC only | The season prior (model parameters fitted on household data), content-addressed and write-once; `climate_shadow.prior` names it |
| `/data/climate/site.yaml` | **no**, PVC only | Private site file (coordinates, reference station). Never commit it or paste it anywhere |
| `/data/service/v1/climate_{decisions,scores,days,models}/` | no | The shadow's records, written by the service; covered by the PVC snapshots and `just pull-store` |

The prior and the site file stay out of this public repository and out of Vault: they are not
credentials, and the job reads them from the data directory only. They are copied onto the PVC once
from the owner's workstation, which keeps them in `~/data/battery-optimizer-cluster` (the
`just pull-store` copy of the PVC).

**Install or refresh the prior.** In the app repository, on a clean checkout of the deployed commit,
with `BATTERY_OPTIMIZER_DATA_DIR=~/data/battery-optimizer-cluster`:

```bash
uv run python hack/climate_shadow_prior.py --feature-set <climate feature set id> \
  --dataset ~/data/battery-optimizer-cluster/analysis/v1/<dataset id> --season S3
# prints the prior id and writes analysis/v1/climate-shadow/priors/<id>/prior.json
```

Copy it and the site file onto the PVC through the running pod (the image has `tar`; `/data` is the
only writable path):

```bash
PRIOR=<id>; SRC=~/data/battery-optimizer-cluster
tar -C "$SRC" -cf - "analysis/v1/climate-shadow/priors/$PRIOR/prior.json" climate/site.yaml \
  | kubectl --context=grigri -n battery-optimizer exec -i deploy/battery-optimizer-service -c service -- \
      tar -xf - -C /data --no-same-owner
```

then set `climate_shadow.prior: "<id>"` in `files/service.yaml` and merge. A prior directory can stay on
the PVC after it is replaced: only the configured one is loaded.

**Go-live of the comfort controller.** On the day the comfort controller takes over, set
`climate_shadow.go_live: "YYYY-MM-DD"` (local date) in `files/service.yaml` and merge. Without it there is
no new-window verdict (the first `comfort` record is only reported as a provisional date); the new
window starts 7 days after `go_live`. Do not change the prior, the comfort rooms or `climate-mpc.yaml`
around go-live: any of them starts a new lineage, which the gates evaluate apart.

**Read the gate report** (from the store on the PVC, no network; it prints private figures, keep the
output local):

```bash
kubectl --context=grigri -n battery-optimizer exec deploy/battery-optimizer-service -c service -- \
  battery-optimizer climate-shadow-report            # add --format json for the raw report
```

Per window (`legacy`, `new`) it shows the valid heating days and weeks (6 weeks are needed, else every
criterion is UNDETERMINED), each preregistered criterion with its value, threshold and PASS / FAIL /
UNDETERMINED, RMSE, persistence RMSE, skill and bias at 1, 6, 12 and 24 h, the daily heat MdAPE, solver
statuses and times and the top-offset share. `/status` (`climate_shadow`) shows the prior, model hash,
last run, scoring queue, error, go-live and the two verdicts.

**When to refit the prior.** The weekly `ua`/`q0` re-estimation runs inside the service; the prior stays
fixed through both preregistered windows. Fit a new one for the next heating season, or earlier only
when a model input changes: the outdoor sensor or the heat measurement, the comfort rooms or their
thermostats. A change of the schedule, the water curve or the machine limits is an edit of
`climate-mpc.yaml` (vendor it from the app repository), not a refit. Record either in the app
repository's regime log.

**Expected after a deploy.** The first run happens at the next HH:10 (or at once if the pod starts after
HH:10). `battery_optimizer_climate_shadow_up` is 0 until then and 1 afterwards; `runs_total{result="planned"}`
and `solves_total{status="optimal"}` grow every hour; `regime{regime="legacy"}` is 1; the first
`refits_total` is counted at the first run. Accuracy series (`rmse_kelvin`, `skill_ratio`, ...) appear
once the first origins are scored, 24 to 48 h later, and the gates stay UNDETERMINED until 6 valid
heating weeks are in a window.

**Alerts** (`rules-service/climate-shadow.yaml`): `BatteryOptimizerClimateShadowDown` (`up == 0` for 2 h),
`ClimateShadowNoRun` (no run for 2 h), `ClimateShadowRunErrors` (more than 2 errors in 6 h),
`ClimateShadowSolveFailures` (infeasible or error solves) and `ClimateShadowNoState` (the climate series
have gaps). **Dashboard**: `Battery optimizer climate shadow` (runs, solver, accuracy per horizon,
valid weeks, gates, regime).

## Live battery control

ADR-0007 in the app repository (`docs/adr/0007-live-battery-control.md`, operations in
`docs/service.md#live-control-dry_run-and-live`) adds `mode: dry_run | live` to `files/service.yaml`.
The service then sets Omnibattery's direction switches, PD grid target and discharge cap through a
narrow write allowlist; Omnibattery stays the only Modbus writer. The deployed mode is `shadow`
until the owner changes it. The battery and its Omnibattery setup are in
[`home-assistant-omnibattery.md`](../troubleshooting/home-assistant-omnibattery.md).

| Piece | Where | State |
|---|---|---|
| Kill switch `input_boolean.battery_optimizer_live` | `apps/home-assistant/config/integrations/battery_optimizer.yaml` | Read by the service only. Created off; restores its last state across HA restarts (no `initial:`) |
| Heartbeat `input_datetime.battery_optimizer_heartbeat` | same file | Written by every reconciled `live` tick (~30 s) |
| Watchdog automation `battery_optimizer_watchdog` | same file | Every minute: kill switch on and heartbeat older than 5 min -> both direction switches off; also on the kill switch's `off` edge. Persistent notification `battery_optimizer_watchdog` (replaced, not stacked) |
| Live token | `global.liveHomeAssistantToken` in `values.yaml` (default `false`) | Off: nothing rendered. On: ExternalSecret `battery-optimizer-home-assistant-live`, mounted in the service at `/secrets/home-assistant/BATTERY_OPTIMIZER_HA_TOKEN` instead of the read-only token. The pipeline keeps the read-only token |
| Alerts | `rules-service/actuator.yaml` | See [Alerts](#alerts) |
| AC failsafe | `apps/esphome/config/smart-plug-v2-battery.yaml` | See [ESPHome AC failsafe](#esphome-ac-failsafe). Must be flashed by hand |

**Tokens.** `shadow` and `dry_run` use the read-only `battery-optimizer` HA user. `live` uses the
separate non-admin user `battery-optimizer-live` (group `system-users`, local only), whose long-lived
token the owner keeps in `pass`. Home Assistant cannot restrict a token to entities, so the service's
`ha_guard` allowlist is the restriction. Rendering fails when `files/service.yaml` says `mode: live`
while `global.liveHomeAssistantToken` is `false` (ArgoCD then shows a `ComparisonError` and syncs
nothing), so `live` never runs on the read-only token.

**Watchdog in `dry_run`.** `dry_run` keeps the kill switch on (rollout step 2) but sends no heartbeat,
so the watchdog turns both direction switches off every minute and keeps its notification up. That is
expected: nothing else should be driving the battery during the rehearsal. The heartbeat and
kill-switch alerts exclude `dry_run` for that reason.

### Live token in Vault

Once, before the first `live` rollout. Load the token from a `0600` file, never from the command line,
and never print it:

```bash
export VAULT_ADDR=https://vault.internal.grigri.cloud
umask 077
TOKEN_FILE="$(mktemp)"
# Put only the battery-optimizer-live user's long-lived token in "$TOKEN_FILE", taken from
# `pass iot/hass-battery-optimizer-live` (no trailing newline).
vault kv put secret/battery-optimizer/home-assistant-live token=@"$TOKEN_FILE"
shred -u "$TOKEN_FILE"
vault kv metadata get secret/battery-optimizer/home-assistant-live   # check it exists, no value shown
```

The ExternalSecret only exists while the flag is on, so a missing Vault secret shows up at the `live`
rollout: `SecretSyncedError` on `battery-optimizer-home-assistant-live` and the new service pod stuck
in `ContainerCreating`. The kill switch is off at that point, so nothing reaches the battery; roll back.

### ESPHome AC failsafe

The battery holds its last charge or discharge command while Home Assistant is down (commissioning
test: outages of 23 s to 10.5 minutes, no device timeout). The watchdog and the service's safe idle
both need HA, so the battery's AC plug (Athom Smart Plug V2 "Battery",
`switch.smart_plug_v2_battery`) carries a failsafe in its own device file. The shared
`packages/smart-plug-v2.yaml` is unchanged:

- after **5 minutes** without a Home Assistant API client (`api.connected` with
  `state_subscription_only`, so a log viewer does not count; checked every 10 s), the relay turns
  **off**. The battery then only powers its backup port and stops charging or discharging to the house;
- when HA reconnects, the relay turns back **on**, only if the failsafe turned it off. A global with
  `restore_value` remembers that across reboots; any relay turn-on clears it. A relay the owner switched
  off stays off;
- `relay_restore_mode: RESTORE_DEFAULT_ON`: a reboot or OTA restores the last state, and ON when
  nothing was saved. A failsafe OFF is restored as OFF and switched on again when HA returns;
- ESPHome's own `api` `reboot_timeout` (default 15 min) reboots the plug during a long HA outage. The
  relay stays off through it.

It also trips on a Wi-Fi loss of 5 minutes. When the AC comes back the inverter goes back to Bypass,
and Omnibattery may report `backup_mode` for a few minutes; the optimizer holds safe idle meanwhile.

**Flashing.** Pushing changes only the ConfigMap that the ESPHome dashboard reads (Reloader restarts the
`esphome` pod); the plug keeps its old firmware until it is flashed over OTA. Either use the dashboard at
`esphome.internal.grigri.cloud` (`smart-plug-v2-battery` -> Install -> Wirelessly), or flash from
`apps/esphome/config` (see its `README.md`):

```bash
cd apps/esphome/config
make requirements
KUBE_CONTEXT=grigri make secrets
make smart-plug-v2-battery.yaml   # esphome run: compile + OTA
```

The plug reboots once (a few seconds) and restores the relay ON. The failsafe firmware has been on
the plug since 2026-11-03 (ESPHome 2026.10.1) and passed the hardware
[test](#failsafe-test) on 2026-11-04. Flash while the
battery is idle (direction switches off), then check `switch.smart_plug_v2_battery` is on.

**Known limit.** A manual relay ON (HA or the plug's button) during a long HA absence is undone within
10 s: the absence counter is only reset by an HA reconnect or a reboot, not by a manual turn-on. To
force AC while HA stays down, power-cycle the plug, then press the button (5 more minutes of grace).
Fix candidate: let a manual turn-on suspend the failsafe until HA reconnects.

**Servers on the backup port.** Since 2026-11-03 the backup port feeds the servers (~250 W) that host
Home Assistant. A trip makes them run from the battery: at 50 % SOC that is roughly 8 h before the
battery's own cutoff. An HA outage longer than that drains the battery and stops the servers; the
plug then has to be switched on by hand (power-cycle + button).

#### Failsafe test

Passed on the flashed firmware on 2026-11-04 (see the result below). Repeat it after any change to the
failsafe or the plug's firmware. It takes ~15 minutes and Home Assistant is really stopped for ~6
minutes. The servers switch to battery power for the switchover and back, the same switchover the
commissioning test passed with the TV (max ping gap 0.22 s).

Preconditions:

- SOC above 30 %, battery in Bypass, Omnibattery `balanced`, backup-port load below the 500 W threshold.
- Kill switch `input_boolean.battery_optimizer_live` **off** (both direction switches off), so the
  optimizer does not chase the battery while it is islanded. Note the time; turn it back on at the end.
- A second way to reach the plug if HA misbehaves: the relay button, and the ESPHome logs from a
  workstation (`apps/esphome/config`: `KUBE_CONTEXT=grigri make secrets`, then
  `make logs FILE=smart-plug-v2-battery.yaml`; a log subscription is not a state subscriber, so it does
  not keep the relay on). Delete `secrets.yaml` afterwards.
- The Kubernetes API must stay reachable from the workstation: the servers host HA and the cluster.
  Do not run it if a server reboot would be a problem.

Steps (T = the moment HA is gone; the owner runs the scaling, agents must not):

1. Start the ESPHome logs and note `switch.smart_plug_v2_battery` = on, `sensor.venus_inverter_state`
   = Bypass, `sensor.venus_ac_offgrid_power` ≈ servers' load.
2. **Stop HA for real.** The `home-assistant` Application comes from the `apps` ApplicationSet with
   selfHeal, so scale `argocd/argocd-repo-server` to 0 first (otherwise Argo reverts the scale-down),
   then `home-assistant/home-assistant-hass` to 0. **T:** the pod is gone and the plug logs the lost
   API connection (`Reading failed CONNECTION_CLOSED`). Lighter alternative: Settings → Devices &
   services → ESPHome → `smart-plug-v2-battery-9d9516` → ⋮ → **Disable** drops only the plug's API
   connection and leaves HA running.
3. **~T+5:00:** the log shows `Battery failsafe: no Home Assistant for 5 min, relay OFF`. Within
   seconds the battery reports `Backup Mode` (Omnibattery keeps polling it over Modbus) and the servers
   keep running (check the Kubernetes API or a ping).
4. **T+6:** restore HA (scale `home-assistant-hass` to 1), then `argocd-repo-server` to 1. Within ~10 s
   of the HA API reconnect the log shows `Battery failsafe: Home Assistant is back, relay ON`; the
   switch is on in HA and the inverter returns to Bypass within 5–33 s. The `home-assistant` app shows
   sync Unknown until Argo's next refresh (about 3 min); a hard refresh clears it.
5. Manual-off check: with HA connected, turn `switch.smart_plug_v2_battery` off, wait 30 s: it must
   stay off (the failsafe only restores what it turned off). Turn it on again; the battery returns to
   Bypass.
6. Turn the kill switch back on and check the optimizer takes control at the next 15-minute slot.

Pass: relay off at about T+5:00 (up to ~10 s early, see the result), back on within 20 s of the HA
reconnect, manual off respected, servers never lost power. Fail or abort:

- relay still on at T+6:00: the failsafe does not trip (likely `api.connected` still sees a state
  subscriber). Restore HA and treat the battery as unprotected against HA outages until fixed;
- relay not back on 1 minute after HA is back: turn it on from HA; if HA cannot reach the plug, press
  the button once;
- any server reboots during the switchover: put the servers back on mains and stop using the backup
  port until the switchover is understood.

Record the timings in this section and in the regime log of the optimizer repo.

**Result 2026-11-04: PASS** (HA really stopped, repo-server scaled to 0 as above; kill switch off
08:29:35, SOC 45 %, backup-port load ~220 W):

| Event | Time (UTC) | Timing |
| ----- | ---------- | ------ |
| HA stopped / pod gone | 08:32:31 / 08:32:34 | T |
| Plug relay OFF | 08:37:25 | 4:52 after the plug lost the HA API connection, 4:51 after the pod was gone |
| HA back (API connected) | 08:38:48 | HA down ~6:17 |
| Plug relay ON | 08:38:56 | 8 s after reconnect (limit 20 s) |
| Inverter back in Bypass | ~08:39:06 | ~10 s after relay ON |
| Manual OFF | 08:40:09 | respected for the 46 s observed, no failsafe restore |
| Manual ON, then Bypass | 08:41:05 | ~7 s |
| Kill switch ON, optimizer in control | 08:41:44 | `discharging` from 08:42:25 (~40 s) |

- The trip is a few seconds before T+5:00 measured from the pod being gone: the 5-minute counter
  starts when HA drops its state subscriptions, shortly before the TCP close. Not an early trip.
- The servers never rebooted: no pod restarts, the Kubernetes API answered `/readyz` throughout. One
  Modbus sample at the switchover showed the backup output at 0 W for one ~6 s poll interval; that is
  the inverter's reading during the mode change, not a 6 s outage.
- SOC 45 % -> 44 % (live discharge resumed, ~254 W).
- Grid side (Shelly EM per-minute history, averages): ~450 W from the grid with the battery idle (kill
  switch off). During the trip the servers' ~220 W moved to the battery (grid minute 08:38 averaged
  241 W), back to ~450 W after the relay came on. No visible import spike at reconnection (minute
  resolution only).

### Rollout: dry_run, then live

Preconditions (owner):

- The HA helpers and the watchdog are deployed. Pushing them restarts Home Assistant through Reloader,
  so check the Shelly `GET /cit/d` first (see the CoIoT note above). In Developer tools, check that the
  three entity IDs exist without a `_2` suffix and that the kill switch is **off**.
- The ESPHome failsafe is flashed and tested (passed 2026-11-04).
- Omnibattery: competing strategies off; `number.omnibattery_pd_controller_max_power_change` raised to
  2000 W. Note the backup threshold
  [workaround](../troubleshooting/home-assistant-omnibattery.md#workaround-active-since-2026-11-03).
- The system caps stay at today's 1000 W charge / 800 W discharge until go-live (steps 1-2 of the live
  phase). In `dry_run` the charge clamping therefore reflects the 1000 W cap minus the house load,
  not the go-live 2500 W.

**dry_run (1-2 days):**

1. Update the image to the optimizer release with live control (Renovate PR, or by hand) and vendor
   its `config/service.yaml` changes (the `actuation:` section). Set `mode: dry_run` in
   `files/service.yaml`. Merge; ArgoCD rolls the pod.
2. Check `/status` (`mode: dry_run`, an `actuator` block) and `battery_optimizer_build_info{mode="dry_run"}`.
3. Turn the kill switch **on**. Nothing is sent; the watchdog keeps the switches off (see above).
4. Review `/data/service/v1/actuation/` and the `battery_optimizer_actuator_*` metrics: guard reasons,
   the mapping at every slot, setpoint churn, how often a charge is clamped, and the house-load
   estimate against the meter.

**live, supervised:**

Go-live steps first, with the battery idle. Above 800 W of discharge the Venus needs Premium mode, and
with the system power limits on the PD target is bounded by the system caps (ADR-0007 commissioning
findings):

1. Turn both *Allow* switches (`switch.venus_battery_allow_charge`,
   `switch.venus_battery_allow_discharge`) **off**, then switch **Premium mode on** in the Marstek app.
   Change nothing else in the app.
2. Set `number.omnibattery_system_max_charge_power` and `number.omnibattery_system_max_discharge_power`
   to **2500 W** (today 1000 / 800 W). The Venus's own `number.venus_max_charge_power` /
   `_max_discharge_power` are already 2500 W; check them.

Then:

3. Put the live token in Vault ([above](#live-token-in-vault)).
4. Turn the kill switch **off**.
5. In one commit: `mode: live` in `files/service.yaml` (the app also requires
   `planning.soc_source: entity` for `live`) and `global.liveHomeAssistantToken: true` in `values.yaml`.
   Merge.
6. Check `kubectl --context=grigri -n battery-optimizer get externalsecret` (both `SecretSynced`), the
   new pod running, `/status` `mode: live`, and `battery_optimizer_build_info{mode="live"}`.
7. Turn the kill switch **on** while watching the grid meter. Verify: writes confirmed
   (`battery_optimizer_actuator_writes_total{result="ok"}`), the heartbeat entity advancing about every
   30 s, a slot change in each direction, the import staying under 4500 W while charging, and the
   charge and discharge reaching up to 2500 W (minus the house load for a charge).
8. Watchdog test: pause ArgoCD auto-sync for the app, scale the Deployment to 0, and within about 6
   minutes see both direction switches go off, the `battery_optimizer_watchdog` notification and
   `BatteryOptimizerHeartbeatStale`. Scale back to 1 and resume auto-sync. (Agents must not run these
   `kubectl` writes; the owner does.)
9. Graceful stop: delete the pod (ArgoCD recreates it) and check that the actuation records show a
   safe idle with reason `shutdown` before the restart, and that the new pod holds safe idle until its
   first fresh plan.

Only then leave it unattended. HA host power loss is still untested (app repo TODO-5.4); the AC
failsafe is the layer that does not depend on HA.

### Rollback

1. Turn `input_boolean.battery_optimizer_live` **off**. This is immediate: the service sets safe idle
   once and stops writing, and the watchdog repeats the safe idle in HA. In an emergency, also turn the
   *Allow* switches off on the Battery dashboard, or the battery's AC plug.
2. In one commit: `mode: shadow` (or `dry_run`) in `files/service.yaml` and
   `global.liveHomeAssistantToken: false`. Merge. ArgoCD prunes the live ExternalSecret and its Secret
   and the pod restarts on the read-only token. The flag cannot go off while the mode is still `live`
   (rendering fails).
3. Restore Omnibattery's native configuration by hand: safe idle leaves the PD target and the
   discharge cap at their last values, so set them back before switching a direction on. Decide whether
   the 2500 W system caps and Premium mode stay (both *Allow* switches off while changing them).
4. To retire the live user for good: delete its long-lived token in HA (the user's profile, as an
   admin) and `vault kv metadata delete secret/battery-optimizer/home-assistant-live`.

### Alerts

`rules-service/actuator.yaml`, long-term Prometheus. The `actuator_*` series exist only in `dry_run`
and `live`; the HA-side rules read `hass_*` and only fire while the kill switch is on.

| Alert | Fires when |
|---|---|
| `BatteryOptimizerActuatorAlert` (critical) | `actuator_alert == 1` for 2 m: a write was not confirmed after its retries |
| `BatteryOptimizerActuatorSafeIdle` | in control and the guard has held safe idle for 30 m (`actuator_guard_reason` says why) |
| `BatteryOptimizerImportOverCap` (critical) | guard reason `import_over_cap`: import above 4700 W while charging |
| `BatteryOptimizerHeartbeatFailures` | a heartbeat write failed in the last 10 m |
| `BatteryOptimizerHeartbeatStale` (critical) | the heartbeat entity is older than 5 m while the kill switch is on (not in `dry_run`); fires with the service down |
| `BatteryOptimizerKillSwitchOnNotLive` | the kill switch has been on for 15 m while the service is not `live` or `dry_run` (or is down) |
| `BatteryOptimizerChargePowerHigh` | measured battery charge above 2600 W for 2 m (the go-live caps are 2500 W): a cap or `battery_ac_power_sign` is wrong |
| `BatteryOptimizerChargeClamped` | `battery_optimizer_actuator_clamped_watts > 1000` for 30 m (sized for the 2500 W caps). **Unconfirmed metric name** from the unreleased optimizer change; check it against the released `/metrics` |

### Omnibattery backup cooldown workaround

`number.venus_backup_offgrid_threshold` is **500 W** (default 50 W) because of upstream bug
ffunes/Omnibattery#660 (`backup_mode` latched after the grid returns while the backup port has a load).
The fix is ffunes/Omnibattery#723. Once a release with #723 is installed, set the threshold back to
50 W and check that the status returns to `balanced` within 5 minutes with a load on the backup port in
Bypass. Side effects of the high threshold and the full procedure:
[`home-assistant-omnibattery.md`](../troubleshooting/home-assistant-omnibattery.md).

## First run (2026-10-25)

Manual Job from the CronJob on an empty PVC: success in 5m46s including the full Prometheus
bootstrap (1028 parts, 5.84 M rows), peak working set about 1.15 GiB (requests 768 Mi, limit 2 Gi),
about 0.54 core peak, 41.5 MiB on the volume afterwards. Steady-state runs only re-pull 8 days of
HA history and 3 days of Prometheus. The PVC is the store of record. A migration of the manual
`~/data/energy-baseline` store was checked on 2026-10-25 and is **not needed**: every HA row in it
(39 distinct) is already in the PVC (54), Prometheus is re-extractable, and the OMIE backfill is
public. The local store stays frozen as the record behind the offline evaluation runs.

Container logs carry one `httpx` INFO line per request (roughly 1000 for the bootstrap), so
kubelet log rotation can drop the early `step N/4` lines of a long run; Loki keeps the full stream.

## Operate

```bash
# State
kubectl --context=grigri -n battery-optimizer get cronjob,job,pvc,externalsecret
kubectl --context=grigri -n battery-optimizer logs job/<job-name> -c pipeline

# Run one pipeline outside the schedule (e.g. after fixing a failure)
kubectl --context=grigri -n battery-optimizer create job --from=cronjob/battery-optimizer-pipeline battery-optimizer-pipeline-manual-<n>
```

Local analysis copy (read-only on the cluster; streams the PVC out of the service pod, validates it,
and syncs it into a separate directory — never into the frozen `~/data/energy-baseline`):

```bash
cd ~/home-energy-optimizer && KUBE_CONTEXT=grigri just pull-store   # ~/data/battery-optimizer-cluster
```

Bumping the image is a Renovate PR changing `tag` in `apps/battery-optimizer/values.yaml`. A new
code version changes every dataset id, so expect one extra dataset on the next build.

Growing the volume: raise `spec.resources.requests.storage` in `templates/pvc.yaml`
(`allowVolumeExpansion` is on). Never delete the PVC; `Prune=false` and `Retain` protect it.

## App Repository Notes

- Code lives in `pando85/home-energy-optimizer` (moved history-preserving from `scripts/energy-baseline`
  on 2026-10-24/25). Releases are cut with `just release`; the `auto-tag` workflow uses the repo
  secrets `PAT` and `GPG_PRIVATE_KEY` (both set). Renovate (Mend app) automerges.
- Local analysis env: a gitignored `.envrc` (direnv) sets `UV`, `KUBE_CONTEXT=grigri` and
  `BATTERY_OPTIMIZER_DATA_DIR=~/data/battery-optimizer-cluster`.
- The load-forecaster and price-outlook improvement plans (app repo `docs/planning/`) are **parked
  since 2026-10-25**; resume triggers are in the load plan §6 (spring 2027 cooling season, battery
  commissioning, household or rule change). Evaluation results stay in the private data directory.
- Owner decisions, do not re-raise: taxes in `tariff.yaml` (PVPC, not important); license, DCO and
  GHCR bootstrap are done or not needed.
