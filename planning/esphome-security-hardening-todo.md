# ESPHome security hardening (TODO)

## Overview

Status: **TODO**, not started. Two deprecation warnings printed by ESPHome 2026.10.1 when the
battery and Snapmaker plugs were flashed on 2026-11-03. Both affect every device that includes
`packages/connection.yaml` (and the web server of the smart plugs and mini switches). Do it before
the deadlines, in one change, and flash every device once.

## Context & Current State

- **OTA:** `ota: - platform: esphome` with `password: !secret ota_password`. ESPHome warns that the
  password wastes flash/RAM and recommends `encryption`; the unencrypted OTA fallback is removed in
  **ESPHome 2027.3**. After that, OTA without the new scheme stops working, so every device would
  need a serial flash.
- **Web server auth:** `web_server` `auth` defaults to `basic` (password sent reversibly); the default
  becomes `digest` in **ESPHome 2027.1.0**. Set `type: digest` explicitly now (or `type: basic` to
  keep it, not recommended).

## Implementation Plan

- [ ] TODO-1.1: Read the ESPHome 2026.x/2027.x changelogs for the exact OTA `encryption` syntax
      and the migration path from a password (does the first flash need the old password plus the
      new key?).
- [ ] TODO-1.2: Add the OTA encryption key to Vault (`/esphome/ota`, new key), the ExternalSecret
      template and `secrets.yaml` (`apps/esphome/templates/external-secret.yaml`).
- [ ] TODO-1.3: Change `packages/connection.yaml` (OTA) and the packages with `web_server`
      (`smart-plug-v2.yaml`, others: `grep -rn web_server packages/`) to `auth: type: digest`.
- [ ] TODO-1.4: Flash every device once over OTA (runbook: `apps/esphome/config/README.md`,
      "Agent Runbook"), battery plug last and with the owner's OK, while the battery is idle.
- [ ] TODO-1.5: Check each device reconnects to HA and the dashboard can still OTA it.

## Verification Checklist

- [ ] No `ota` / `web_server` deprecation warnings in `esphome config` for any device.
- [ ] Every device flashed with the new firmware (HA `sw_version`).
- [ ] A second OTA with the new scheme works for at least one device of each type.

## Files to Modify

| File | Change |
|---|---|
| `apps/esphome/config/packages/connection.yaml` | OTA encryption |
| `apps/esphome/config/packages/*.yaml` with `web_server` | `auth: type: digest` |
| `apps/esphome/templates/external-secret.yaml` | new secret key |
| `apps/esphome/config/README.md` | Vault paths table |
