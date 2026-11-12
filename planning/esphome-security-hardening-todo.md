# ESPHome security hardening

## Overview

Status: **repo change done, devices not flashed yet.** Two deprecations printed by ESPHome 2026.10.x
(first seen when the battery and Snapmaker plugs were flashed on 2026-11-03):

| Deprecation | Deadline | Fix (in the repo) |
|---|---|---|
| Plaintext / password OTA fallback removed | **ESPHome 2027.3.0** | `ota: - platform: esphome` + bare `encryption:` in `packages/connection.yaml` |
| `web_server` `auth` default changes from `basic` to `digest` | **ESPHome 2027.1.0** | `auth: type: digest` in the three packages with a `web_server` |

Both affect the whole fleet (every device includes `packages/connection.yaml`). The devices only pick
the change up when they are flashed, so the remaining work is one flash per device, in the order below.
Pushing the YAML changes only the ConfigMap the dashboard reads.

## Context & Findings

Researched against `esphome==2026.10.2` (source: `esphome/components/esphome/ota/__init__.py`,
`ota_esphome.cpp`, `espota2.py`, `__main__.py::_upload_via_native_api`, `components/web_server/__init__.py`) and
<https://esphome.io/components/ota/esphome/>.

- **The OTA key is the api key.** A bare `encryption:` under `ota: platform: esphome` reuses
  `api.encryption.key` (`_resolve_encryption_key`). An explicit OTA key must equal the api key or
  validation fails. **No new Vault secret, no ExternalSecret change.**
- **`password:` cannot be combined with `encryption:`** (validation error) except together with the
  uploader-side `allow_plaintext_upload: true`, meant for the single install that migrates a device.
  With encryption the password is not compiled in (saves flash/RAM) and the Noise handshake skips the
  password phase on the device (`ota_esphome.cpp`, `NOISE_HANDSHAKE` -> `DATA`).
- **Minimum firmware that can encrypt OTA: 2026.9.0** (needs an api key; every device has one). A device
  that runs such firmware accepts an encrypted upload even if it still has the old password built in.
- **Migration, per running firmware** (the CLI/dashboard of 2026.10.x handles both):
  - Running >= 2026.9.0: a plain `make flash-device` with the new config. One flash.
  - Running < 2026.9.0 (cannot encrypt): `make flash-device-migrate`. It wraps the device YAML in a
    throwaway overlay (`password` + `encryption: allow_plaintext_upload: true`), so the **one** flash
    goes in plaintext with the old password and the resulting firmware requires encryption. Any
    older firmware (2025.10.2, 2024.3.1) goes straight to the new one in one OTA; no intermediate
    version. The overlay is generated at flash time and deleted afterwards (nothing to revert in git);
    `allow_plaintext_upload` must never be committed (it lets an attacker downgrade an upload).
  - Without the overlay the 2026.10.2 client refuses to send a plaintext image to an old device
    ("device did not offer encryption; refusing to send the image in plaintext"). Fails closed.
    Until 2027.3.0 the plain flash would still silently fall back to plaintext (the warning that
    started this), after that it is refused.
- **Dashboard:** the dashboard image (`values.yaml`, `ghcr.io/esphome/esphome:2026.10.2`) runs the same
  code as the CLI pin in `config/Makefile`, derives the key from `secrets.yaml` and needs nothing else.
  It can OTA a device that already runs the new firmware; it cannot do the first hop of an old device
  (no overlay), use the Makefile for that.
- **`web_server` `ota: false` added.** Once OTA is encrypted, ESPHome warns that the `web_server`
  OTA platform (`/update` on port 80, auto-added by the captive portal) accepts the same image in
  plaintext. Nobody uses it (flashing is `esphome run`), so it is disabled in the three packages;
  the captive-portal recovery on the fallback AP stays. Consequence: the routes to change
  the api/OTA key later are a serial flash or the captive-portal update page on the fallback AP (the
  web_server OTA route ESPHome mentions needs `ota: false` removed and flashed beforehand).
- **web_server digest:** same `username`/`password` secrets, only `type: digest` added. Browsers
  prompt as before; scripts using `curl -u` need `--digest`. No repo component calls the device web UIs.
- **Cost (ESP8285, `mini-switch-office-a`, 2026.10.2, compile only):** RAM 41.6% (34092 B) -> 41.5%
  (34032 B), flash 46.8% (478757 B) -> 46.5% (475937 B). Smaller, not larger (password code gone,
  web OTA gone). `smart-plug-v2-battery`: RAM 42.3% (34680 -> 34612 B), flash 48.1% -> 47.8%.
  `venus-e-cooling` (ESP-12F): flash 41.6% -> 41.2%. ESP32 `irrigation-controller-corner`: RAM 26.8%
  -> 26.7%, flash 49.3% -> 49.2%.
- **Firmware currently running** (read from the device over the api on 2026-11-12):

  | Device | Running | Route |
  |---|---|---|
  | `mini-switch-office-a` | 2026.10.2 | `flash-device` |
  | `smart-plug-v2-snapmaker` | 2026.10.1 | `flash-device` |
  | `smart-plug-v2-battery` | 2026.10.1 | `flash-device`, last |
  | `irrigation-controller-mid` | 2026.4.5 | `flash-device-migrate` |
  | `irrigation-controller-corner` | 2025.10.2 | `flash-device-migrate` |
  | `rack-controller` | 2025.10.2 | `flash-device-migrate` |
  | `mini-switch-1..4`, `lilygo-higrow-1/2` | unknown, not reachable (no ping, not in the blackbox probes) | `flash-device-migrate` when they are back |
  | `garage-door-opener` | unknown, not reachable | blocked, see below; `flash-device-migrate` once fixed |
  | `venus-e-cooling` | not on the network (no DNS record) | its first flash is serial anyway; later ones `flash-device` |

- **Unrelated findings:** `garage-door-opener.yaml` fails `esphome config` (`packages/cc1101.yaml`
  uses the removed `esphome: platform: ESP32`; needs `esp32: board: esp32dev`); `lilygo-higrow-*`
  warn `attenuation: 11db` is deprecated (use `12db`).

## Implementation Plan

- [x] TODO-1.1: Read the exact OTA `encryption` syntax and the migration path (see Findings).
- [x] TODO-1.2: ~~Add an OTA key to Vault~~ Not needed: the api key is reused. `ota_password` stays in
      Vault/`external-secret.yaml` only until the last device is migrated (the overlay needs it),
      then see TODO-1.7.
- [x] TODO-1.3: `packages/connection.yaml` (`encryption:`, no `password:`); `auth: type: digest` and
      `ota: false` in `mini-switch.yaml`, `smart-plug-v2.yaml`, `irrigation-controller.yaml`.
      `make flash-device-migrate` added to `config/Makefile`.
- [x] TODO-1.3b: `esphome config` with 2026.10.2 for every device: no `ota`/`web_server` warnings left.
- [ ] TODO-1.4: Commit and push, then flash every device once, in this order (all from
      `apps/esphome/config`; each flash reboots the device for a few seconds):
  1. `make flash-device DEVICE=mini-switch-office-a` (already on 2026.10.2, lowest risk: proves the
     encrypted path and the digest web UI).
  2. `make flash-device DEVICE=smart-plug-v2-snapmaker`
  3. `make flash-device-migrate DEVICE=irrigation-controller-mid`, then `...-corner` (check no
     watering schedule is running)
  4. `make flash-device-migrate DEVICE=rack-controller` (rack fan controller; the PID restarts)
  5. Offline devices (`mini-switch-1..4`, `lilygo-higrow-1/2`, `garage-door-opener` after fixing its
     config) with `flash-device-migrate` when they are powered again, **before 2027.3.0** (after it
     the migrate route still works, but only because of `allow_plaintext_upload`; plan for serial
     otherwise).
  6. **Last, with the owner's OK, while the battery is idle:**
     `make flash-device DEVICE=smart-plug-v2-battery`. The battery plug feeds the house battery's AC
     input; the reboot drops its AC for a few seconds and the failsafe/`RESTORE_DEFAULT_ON` logic
     brings the relay back (see `docs/deployment/battery-optimizer.md#esphome-ac-failsafe`).
- [ ] TODO-1.5: After each flash: `make device-logs DEVICE=<d>` shows `Encryption: required` in the OTA
      config dump and no `Password configured`; the device reconnects to HA (`sw_version`); the
      dashboard shows it online.
- [ ] TODO-1.6: Second OTA with the new scheme (a plain `make flash-device`, no overlay) on one device of
      each type: ESP8285 (`mini-switch-office-a`) and ESP32 (`irrigation-controller-mid`).
- [ ] TODO-1.7: When every device runs the new firmware: remove `ota_password` from
      `templates/external-secret.yaml` and the Vault path `/esphome/ota`, drop the `/esphome/ota` row
      from `config/README.md`, and remove `flash-device-migrate` from the Makefile.

## Risks

- A device that cannot encrypt and is flashed without the overlay is refused (fails closed), it is
  not bricked. A failed OTA leaves the old firmware running (safe mode on repeated boot failures).
- The api key is now also the OTA key: changing it needs a serial flash or the captive-portal update
  page, never plain OTA. Treat it like the old OTA password for rotation planning.
- Offline devices that stay on pre-2026.9 firmware past 2027.3.0 can only be reached with the
  `allow_plaintext_upload` overlay, a serial flash, or the captive portal.

## Verification Checklist

- [x] No `ota` / `web_server` deprecation warnings in `esphome config` (2026.10.2) for any device
      except `garage-door-opener` (fails to validate for an unrelated reason).
- [ ] Every device flashed with the new firmware (HA `sw_version`, `Encryption: required` in logs).
- [ ] A second OTA with the new scheme works for at least one device of each type.
- [ ] Web UI of one smart plug, one mini switch and one irrigation controller still prompts for the
      same credentials (digest) and loads.

## Files Modified

| File | Change |
|---|---|
| `apps/esphome/config/packages/connection.yaml` | OTA `encryption:` (api key reused), `password:` removed |
| `apps/esphome/config/packages/{mini-switch,smart-plug-v2,irrigation-controller}.yaml` | `auth: type: digest`, `ota: false` |
| `apps/esphome/config/Makefile`, `.gitignore` | `flash-device-migrate` target and its throwaway overlay |
| `apps/esphome/config/README.md` | Runbook: encryption, migrate target, digest |
| `apps/esphome/templates/external-secret.yaml` | unchanged now; drop `ota_password` in TODO-1.7 |
