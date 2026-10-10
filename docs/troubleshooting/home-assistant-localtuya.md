# Home Assistant localtuya (pando85 fork)

## Overview

Tuya devices are controlled locally by the `localtuya` custom integration, installed through HACS
from [`pando85/localtuya`](https://github.com/pando85/localtuya) `master` (no releases, HACS tracks
commits). Local checkout: `~/localtuya`. Deploy procedure and API access:
[home-assistant-api-hacs-deploy.md](home-assistant-api-hacs-deploy.md).

The fork is deliberately narrowed to what is used. On 2026-10-10 the unused `cover`, `vacuum`,
`select` and `binary_sensor` platforms were removed
([PR #9](https://github.com/pando85/localtuya/pull/9)). This matters beyond tidiness:
`config_flow.py` builds its YAML schema from **every** platform in `PLATFORMS`, so every platform
module is imported at load time — an HA API removal in an unused platform would take down the whole
integration. Adding a new device type means restoring its platform from git history.

## Devices in Use

One config entry, 5 devices, 22 entities (HA 2026.9.4):

| Device | Protocol | Entities and DPs |
|---|---|---|
| Thermostat bedroom / hall-kitchen / office A / office T | 3.3 | climate DP 1 (hvac mode DP 2, target temp 16, current temp 24, valve 36 `open/close`, heat/cool flag 104 bool `True`=heat, `False`=cool), switch Frost DP 10, switch Child lock DP 40, number Backlight DP 105 (0–100), sensor Control mode DP 101 |
| Fan and light office A | 3.2 | fan DP 1 (speed DP 3 int 1–6, direction DP 8); light DP 15 (brightness 16 0–100, color temp 17 0–100 → 3000–6000 K) |

Device config lives in `/config/.storage/core.config_entries` (contains `local_key` secrets — never
print or copy it out of the scratchpad).

## Light Color Temperature Ignored / Light Never Turns On (fixed)

Symptom: color temperature changes did nothing; history showed the light never reached `on`; log:

```
HomeAssistantError: light.ceiling_light_office_a (...LocaltuyaLight) set to unsupported color mode
brightness, expected one of {<ColorMode.HS: 'hs'>, <ColorMode.COLOR_TEMP: 'color_temp'>}
Entity light.ceiling_light_office_a is requesting unknown DPS index -1
```

Root causes, fixed in `d72f3f7` and `68ae9e6` ([PR #8](https://github.com/pando85/localtuya/pull/8)):

1. **Mired API removed in HA 2026.x.** `LightEntity` only passes `color_temp_kelvin` and only reads
   `color_temp_kelvin` / `min_color_temp_kelvin` / `max_color_temp_kelvin`. The old `color_temp` /
   `min_mireds` code was silently ignored (no deprecation warning remains) and the entity showed
   HA's default 2000–6535 K. An earlier attempt (`0f48723`, reverted) skipped `turn_on` and ignored
   the min-Kelvin offset and reverse option.
2. **HA 2026.x raises on an unsupported `color_mode`** (it used to warn). The light's `color_mode`
   was configured as DP 2, which is the fan's mode DP — not a DP this device reports.
3. **`has_config()` only treated the string `"-1"` as unset**, but the config stores the int `-1`
   and `False`, so the light advertised `hs` and the effect feature.

Verified live: `supported_color_modes: ["color_temp"]`, 3000–6000 K. Readback is quantised (~30 K
per DP step): 4000 K reads back as 3960 K.

## Audit Findings (2026-10-10)

A read-only audit of the deployed entities found five more code bugs, all reproduced in a venv
with the live config. Fixes are in [PR #10](https://github.com/pando85/localtuya/pull/10) (one
commit per bug), **not merged or deployed yet**, pending a live
thermostat test by the user (do not test thermostat writes without them):

1. **Thermostat heat/cool switch fails while in `auto`** (`climate.py` `set_hvac_mode`): the write
   of `manual` to DP 2 lacks `await` (log: `RuntimeWarning: coroutine 'TuyaDevice.set_dp' was never
   awaited`), and `auto` has no branch when a cooling DP is configured. It only worked on
   2026-10-08 because the thermostats were already `manual`.
2. **Cool mode always reports `hvac_action: heating`**: DP 104 (bool) is compared to the string
   `"cool"`.
3. **Fan speed one step off for integer percentages**: `math.ceil` on 16.67% steps maps 17 → speed
   2, 34 → 3, 51 → 4, 67 → 5, 84 → 6. **The HA UI is not affected** (verified with Playwright): the
   more-info slider (6 speeds → `ha-control-slider` with `step = percentage_step`) sends floats like
   `16.666…`, and HA core's `vol.Coerce(int)` truncates them to 16/33/50/66/83/100, which `ceil`
   maps correctly. Only integer callers hit it: automations/scripts (`percentage: 17`), Assist
   ("set the fan to 67%"), or API clients that round instead of truncate. The dialog label shows
   "17%"/"67%" — display rounding only.
4. **Number entities write floats** (`57.0`) to integer DPs.
5. **All entities registered on the last device's interface** (`common.py`
   `tuyainterface.add_entities` outside the per-device loop) — latent, only matters with
   restore-on-reconnect.

Plus unconfigured DPs (`-1`, the white-only light's mode DP 2) are still included in status
requests; PR #10 drops them (fan+light now requests `[1, 15, 16, 17, 3, 8]`).

Behaviour changes PR #10 brings to the thermostats (DP 104 `True` = heat, `False` = cool, confirmed
from history; the PR keeps that mapping):

- `heat`/`cool` from `auto` now really writes `manual` to DP 2 (then waits `MODE_WAIT`, then DP 104).
- Selecting `auto` now writes `auto` to DP 2 (previously a no-op).
- A missing DP 104 displays `heat` instead of `cool`; cool mode with an open valve shows `cooling`.
- Configs without a cooling DP behave exactly as before.

### Live test plan (with the user)

On one thermostat, record state, then: `auto` → `heat`, `heat` → `cool` (only when cooling is
expected), back to the original mode. After each step check DP 2 is `manual`, DP 104 matches, the
entity's `hvac_mode` / `hvac_action` are right, and no `never awaited` warning is logged. Also try
`auto` from `manual`. Fan: call `fan.set_percentage` with 17 and 67 via the API (not the UI, which
never sends integers) and confirm speeds 1 and 4. Backlight: set an integer value and confirm DP 105
follows.

## Pending Options-UI Cleanup

- Light: clear `color_mode` (DP 2) and `color` (`-1`).
- Fan and light device: remove `scan_interval: 10` (sends empty `UPDATEDPS` every 10 s, none of
  its DPs are in the update allow-list), `enable_debug`, and manual DP `300`.
- "Thermostat office A" device name has a stray trailing `"`.

## Operational Notes

- The HA pod is not on the host network, so Tuya UDP discovery broadcasts never arrive; devices
  reconnect via the 60 s reconnect timer. Short (15–95 s) unavailable blips are device/Wi-Fi side —
  the bedroom thermostat drops most (10× in 7 days).
- `enable_debug: true` on a device produced no DEBUG lines in the HA log; DP values cannot be read
  from logs without also raising the logger level.
- A whole-HA outage (all entities unavailable, including `sun.sun`) is not a localtuya problem —
  e.g. 2026-10-03 08:27 → 10-05 03:15 UTC.
