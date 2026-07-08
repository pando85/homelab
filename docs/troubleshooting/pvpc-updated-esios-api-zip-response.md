# PVPC Updated: ESIOS API Returns ZIP Instead of JSON

## Problem

The `pvpc_updated` custom integration (HACS, from
[oscarrgarciia/HA-PVPC-Updated](https://github.com/oscarrgarciia/HA-PVPC-Updated)) silently fails
to load PVPC electricity prices. Symptoms:

- HASS logs: `WARNING [pvpc_updated] Client error in '...' -> 200, message='Attempt to decode JSON
  with unexpected mimetype: json'`
- AppDaemon `climate_control` / `dhw_control` stuck in retry loop: `Less than 15 hours of data
  available: []. Retrying in 10 minutes`
- `sensor.esios_pvpc` shows `unavailable` or stale data

## Root Cause

The ESIOS API (`api.esios.ree.es`) changed its response behavior for the public PVPC endpoint
(`/archives/70/download_json`):

1. **Content-Type** changed from `application/json` to `text/html`
2. **Response body** changed from plain JSON to a ZIP archive containing JSON files

aiohttp's `resp.json()` validates Content-Type by default, raising `ContentTypeError` on the
mismatch. Even with `content_type=None`, the ZIP body cannot be parsed as JSON.

Per the [ESIOS API docs](https://api.esios.ree.es/doc/archive_json/get_a_specific_archive_value_of_type_json.html),
the endpoint should return `application/json; charset=utf-8`. This is an API bug on ESIOS's side
(reported to consultasios@ree.es on 2026-07-08).

## How to Diagnose

```bash
# Check response headers
curl -sI "https://api.esios.ree.es/archives/70/download_json?locale=es&date=$(date +%F)"

# Check if response is ZIP (magic bytes PK\x03\x04)
curl -s "https://api.esios.ree.es/archives/70/download_json?locale=es&date=$(date +%F)" | file -

# Check HASS logs for the warning
kubectl --context=grigri -n home-assistant logs home-assistant-hass-0 --tail=100 | grep pvpc

# Check AppDaemon for the retry loop
kubectl --context=grigri -n home-assistant logs -l app=appdaemon --tail=50 | grep "Less than"
```

## Fix / Workaround

A fix has been submitted upstream: [PR #5](https://github.com/oscarrgarciia/HA-PVPC-Updated/pull/5).
It reads raw bytes, detects ZIP by magic bytes, extracts JSON from the archive, and falls back to
plain JSON parsing.

If the upstream fix is merged and installed via HACS, no manual action is needed.

To apply the fix manually on the running pod (temporary, lost on restart):

```bash
# Copy the patched file from the local fork
kubectl --context=grigri -n home-assistant cp \
  HA-PVPC-Updated/custom_components/pvpc_updated/aiopvpc/pvpc_data.py \
  home-assistant-hass-0:/config/custom_components/pvpc_updated/aiopvpc/pvpc_data.py

# Restart the pod to pick up changes
kubectl --context=grigri -n home-assistant delete pod home-assistant-hass-0
```
