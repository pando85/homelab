# Home Assistant Restarts and the Scheduler Integration

## Problem

On 2026-11-18 two merges that only changed Home Assistant config (15:43 and 15:51 local) re-opened
the bedroom cover (`cover.shellyplus2pm_bedroom`, to 70 % and then 86 %) without anyone asking.

## Root Cause

Any change under `apps/home-assistant/config/**`, or to the HA pod spec, makes ArgoCD restart Home
Assistant. On startup the custom `scheduler` integration (v3.3.9) re-executes the time slot in
progress unless it knows when HA was shut down:

- `custom_components/scheduler/__init__.py`: `async_handle_shutdown` saves the shutdown time
  (`store.async_set_time_shutdown`) in the stop handler.
- `switch.py`: `_init` sets `skip_initial_execution` when the slot started before the recorded
  shutdown time. Without a recorded time, the slot in progress runs again.
- `timer.py`: `current_timeslot` treats a point entry (start, no stop) as current only for 1 minute
  after its start; a range entry is current for its whole span.

The pod had `terminationGracePeriodSeconds: 1` (set in 2023 "to avoid duplicate metrics"), so HA was
SIGKILLed before any stop handler ran and the shutdown time was never saved.

## Fix

- PR #4837 raised the grace period to 60 s. HA restarts since 16:12 local shut down cleanly.
- Bedroom schedule `7f9391` was converted from time ranges to point entries (00:00 to position 13,
  10:30 to open, 20:00 to position 13), so even a crash or OOM kill cannot re-apply an action.
- Still range-based: extractor schedule `5e2839` (`switch.shellyem_main`: off 00:00-08:00, on
  08:00-23:00). A crash restart re-applies its current slot, which is harmless while it matches the
  device state.

## Rules of Thumb

- HA config PRs restart HA. Before merging, read the enabled schedules in
  `/config/.storage/scheduler.storage` and look for range entries whose current slot differs from the
  device state.
- Prefer point entries for devices with physical side effects (covers, valves, anything that moves).
- Edit a schedule with `POST /api/services/scheduler/edit` carrying `entity_id`, `weekdays`,
  `repeat_type` and `timeslots`. Omit `name` to keep the entity id. Auth as in
  [home-assistant-api-hacs-deploy.md](home-assistant-api-hacs-deploy.md).
