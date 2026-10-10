# localtuya Light Color Temperature Ignored on Home Assistant 2026.x

## Problem

The fan-with-light "Fan and light office A" (`light.ceiling_light_office_a`, localtuya, Tuya
protocol 3.2) could not change color temperature. Investigation showed the light could not be
turned on from HA at all: its recorder history contained only `off`/`unavailable` for weeks, and
every state write logged:

```
HomeAssistantError: light.ceiling_light_office_a (...LocaltuyaLight) set to unsupported color mode
brightness, expected one of {<ColorMode.HS: 'hs'>, <ColorMode.COLOR_TEMP: 'color_temp'>}
Entity light.ceiling_light_office_a is requesting unknown DPS index -1
```

## Root Cause

Three independent bugs in `pando85/localtuya` (fixed in `d72f3f7` and `68ae9e6`,
[PR #8](https://github.com/pando85/localtuya/pull/8)):

1. **Mired API removed.** HA 2026.x `LightEntity` only passes `color_temp_kelvin` to `turn_on` and
   only reads `color_temp_kelvin` / `min_color_temp_kelvin` / `max_color_temp_kelvin`. localtuya
   still used `kwargs["color_temp"]`, `color_temp`, `min_mireds`, `max_mireds`, so temperature
   requests were silently dropped and the entity advertised HA's default 2000–6535 K instead of the
   configured range. There is no deprecation warning anymore — the old API is simply gone.
2. **HA 2026.x raises on an unsupported `color_mode`** (it used to warn). The light's
   `color_mode` DP was configured as DP 2, which is the *fan's* mode DP. When on, DP 2 held a fan
   value, so neither white nor colour mode matched and the entity fell back to `BRIGHTNESS`.
3. **`has_config()` only treated the string `"-1"` as unset.** The config entry stored `color` as
   the int `-1` and `music_mode` as `False`, so the light advertised `hs` and the effect feature
   and polled DP `-1`.

An earlier Kelvin attempt (`0f48723`) was reverted because it assigned to properties, skipped
`turn_on`, and ignored the min-Kelvin offset and the reverse option — check it before re-doing
similar work.

## How to Diagnose

- Entity attributes (see [API access](home-assistant-api-hacs-deploy.md)): a broken light shows
  `min/max_color_temp_kelvin` 2000/6535 regardless of config, or an `hs` mode the device lacks.
- Logs: `kubectl --context=grigri -n home-assistant logs home-assistant-hass-0 -c hass | grep -i
  -E 'localtuya|unsupported color mode|unknown DPS'`.
- Device config (DP map, kelvin range, reverse): localtuya entries in
  `/config/.storage/core.config_entries` (read-only; contains `local_key` secrets — don't print).
- localtuya `enable_debug: true` on the device produced no DEBUG lines in the HA log, so DP values
  could not be read from logs.

## Device Reference

| Entity | DPs |
|---|---|
| Fan `fan.ceiling_fan_office_a` | on/off 1, speed 3 (1–6), direction 8 |
| Light `light.ceiling_light_office_a` | on/off 15, brightness 16 (0–100), color_temp 17 (0–100) |

Kelvin range 3000–6000, `color_temp_reverse: false`. The light's `color_mode` still points at
DP 2 in the config entry; the fixed code ignores the mode DP for lights without HS support, but it
is cleaner to clear it in the localtuya options flow. With 100 DP steps over 3000 K the readback is
quantised (~30 K): 4000 K requested reads back as 3960 K.

## Fix / Workaround

Code fix is merged and deployed via HACS (see
[HACS deploys](home-assistant-api-hacs-deploy.md)). Verified on HA 2026.9.4: `supported_color_modes`
is `["color_temp"]`, range 3000–6000 K, 4000 K and 5500 K requests apply, fan state unaffected, no
localtuya errors after restart.

Other HACS custom integrations exposing lights may have the same mired-API or color-mode issue on
HA 2026.x — check them first if a light silently ignores color temperature.
