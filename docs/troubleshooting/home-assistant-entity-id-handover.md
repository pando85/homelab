# Home Assistant: moving an entity ID to another platform without losing its history

**Status:** used in production 2026-10-27 to move `sensor.shellyem_main_channel_{1,2}_power` from the
native `shelly` integration to MQTT at a 2 s cadence. History, long-term statistics, Prometheus labels
and every consumer survived unbroken. Automation: `scripts/shelly-em-registry.py`
(via `metal/playbooks/shelly-em.yaml --tags registry`).

## Problem

You want an existing canonical `entity_id` — one that already has recorder history, long-term
statistics, dashboards, Prometheus series and external consumers pinned to it — to be fed by a
*different* integration instead (here: Shelly REST/CoIoT polling → MQTT push), with the ID, the
`friendly_name` and the metric labels unchanged.

The obvious approach is to rename the old entity out of the way and rename the new one onto the
freed ID. **That silently moves the history to the old entity's new name**, so the canonical ID
starts from zero rows: dashboards and the energy panel go blank before the cutover, the Parquet
archive starts a new series, and nothing errors out loudly enough to notice.

## Root cause

HA's recorder migrates metadata **in place** whenever an entity is *renamed*:

- `components/recorder/entity_registry.py` filters registry events with
  `event_data["action"] == "update" and "old_entity_id" in event_data`, then calls both
  `async_update_statistics_metadata(old, new_statistic_id=new)` and
  `async_update_states_metadata(old, new_entity_id=new)`.
- `table_managers/states_meta.py::update_metadata` runs
  `UPDATE states_meta SET entity_id = <new> WHERE entity_id = <old>` — **but returns `False` without
  updating when `<new>` already exists** (collision guard).
- `table_managers/statistics_meta.py::update_statistic_id` has the same guard and logs
  `Cannot rename statistic_id <old> to <new> because the new statistic_id already exists` at
  **ERROR** level.

Two consequences that the rename approach gets exactly backwards:

1. Renaming the *old* entity away succeeds (the target ID is free), so the production history
   **follows it** to the new ID.
2. A registry **removal** fires `action="remove"`, which the recorder's rename filter ignores — so
   nothing migrates and the canonical `states_meta` / `statistics_meta` rows stay put.

## Fix / correct procedure: remove-then-promote

1. **Deploy the new platform's entity under a temporary ID and verify it** — state, unit,
   `friendly_name` and the actual update cadence, all before touching anything production.
2. **Remove** the old entity with `config/entity_registry/remove`. History stays under the canonical
   ID; the entity platform also drops it from the state machine immediately (it listens for
   `action="remove"`), so there is no window where two entities write the same ID.
3. **Promote** the new entity with `config/entity_registry/update` and
   `new_entity_id: <canonical>`. The recorder attempts a migration and the **collision guard refuses
   it** — which is the desired outcome: the canonical rows survive and new states append to the same
   `metadata_id`.
4. **Reload the old integration's config entry** (`homeassistant.reload_config_entry` service — the
   websocket command `config_entries/reload` does not exist). It recreates its entity under a
   suffixed ID, then **disable that entry** (`disabled_by: user`) so it can never reclaim the
   canonical ID or pollute the state machine, recorder and Prometheus export.
   Find the recreated entry by `unique_id`, never by an assumed `_2` suffix.

Order matters: promote **before** reloading, or the old integration reclaims the canonical ID.

### Expected log noise

The two `Cannot rename statistic_id ... because the new statistic_id already exists` ERROR lines are
the collision guard doing its job. **Do not "fix" them** — their absence would mean the guard did not
fire and history was moved.

## How to verify (all read-only)

```bash
# 1. Canonical IDs must keep their original metadata_id / statistic id
kubectl --context=grigri -n home-assistant exec home-assistant-postgres-0 -c postgres -- \
  psql -U postgres -d home_assistant -At -c \
  "select metadata_id, entity_id from states_meta where entity_id like '<prefix>%' order by 1"

# 2. Rows must exist on BOTH sides of the cutover under the same metadata_id.
#    NB: statistics tables have a legacy NULL `start` column — use `start_ts` (epoch).
kubectl --context=grigri -n home-assistant exec home-assistant-postgres-0 -c postgres -- \
  psql -U postgres -d home_assistant -At -c \
  "select count(*) filter (where start_ts < <cutover_epoch>) before,
          count(*) filter (where start_ts >= <cutover_epoch>) after
     from statistics_short_term where metadata_id=<statistic_id>"

# 3. Prometheus label sets must be byte-identical (instance/pod_ip are volatile by design)
curl -sS -G <prometheus>/api/v1/query --data-urlencode 'query=<metric>{entity="<canonical>"}'
```

Also confirm the 5-minute statistics bucket *after* the cutover appears — it is compiled at
`HH:M5:10`, so check a few minutes later, not immediately.

## Rollback

Revert the new platform's YAML and let HA restart so its entities are **removed**. Never rename them
off the canonical ID — that drags the accumulated history with them. Then rename the disabled native
entry back onto the canonical ID (the guard skips the migration again) and re-enable it.
`scripts/shelly-em-registry.py rollback` enforces this ordering and refuses to run while an MQTT
entity still owns a canonical ID.

## Gotchas

- **A disabled entity still reserves its `entity_id`.** You cannot promote onto an ID held by a
  disabled entry — remove that entry instead.
- **`friendly_name` is part of the Prometheus label set** (`hass_sensor_power_w{friendly_name=...}`).
  For a YAML MQTT sensor it comes from `name:`; for a native Shelly entity it is assembled from the
  device and channel names. They must match exactly — assert it in the promotion preflight, do not
  eyeball it.
- **`default_entity_id` only applies at first creation** when `unique_id` is set, so a temporary ID
  in YAML does not fight a later registry rename across restarts.
- **Orphaned metadata is harmless**: the temporary ID keeps its own `states_meta` row and its states
  age out with normal retention. There is no supported API to delete it, so leave it alone.
- Registry `update` returns as soon as the registry is written, **not** when the recorder has
  processed the queued migration tasks. Verify from the database afterwards rather than trusting the
  API response.

## Related

- [`shelly-gen1-coiot-disabled-breaks-ha-setup.md`](shelly-gen1-coiot-disabled-breaks-ha-setup.md) —
  the CoIoT dependency that makes any HA restart on a Gen1 device risky
- [`../../planning/shelly-em-mqtt-implementation.md`](../../planning/shelly-em-mqtt-implementation.md) —
  the 2 s MQTT cutover this technique was built for, with its measurements and rollback
