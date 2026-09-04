# AppDaemon Climate/DHW Control Fails on Negative PVPC Prices

## Problem

AppDaemon apps `climate_control` and `dhw_control` get stuck in a retry loop:

- `ERROR climate_control: Error getting prices:` — note the exception message is **empty**
- Telegram notification: `Error getting prices: retrying in 10 minutes`
- The aerotherm HVAC is never scheduled (stays in off/DHW-only mode), so the house is not cooled
  or heated on price-optimized hours

## Root Cause

Both apps rejected negative prices: `Price.__post_init__` raised a bare `ValueError` for
`value < 0`. With high solar generation, the Spanish PVPC market now regularly has **negative
prices** during midday hours (e.g. 2026-09-05: -0.0107 €/kWh at 15-16h). As soon as today's price
curve contains a negative hour, `_get_prices` raises and every 10-minute retry fails again — all
day long. The bare `ValueError` renders as an empty string, which is why the log line ends with
`Error getting prices:` and no message. An empty exception message in these logs is the fingerprint
of this bug.

## How to Diagnose

```bash
# Check today's PVPC curve for negative hours
curl -s "https://api.esios.ree.es/archives/70/download_json?locale=es&date=$(date +%F)" \
  | python3 -c "
import json,sys
for r in json.load(sys.stdin)['PVPC']:
    if float(r['PCB'].replace(',', '.')) < 0:
        print(r['Hora'], r['PCB'])
"

# Empty exception message in AppDaemon logs
kubectl --context=grigri -n home-assistant logs -l app=appdaemon --tail=50 | grep "Error getting prices"
```

## Fix / Workaround

Fixed in `apps/home-assistant/appdaemon/apps/`:

- `Price` accepts negative prices (they are legitimate market values, not sensor errors)
- When today's curve contains negative hours, the schedule notification (climate and DHW)
  includes a line listing them as merged ranges, e.g. `Negative PVPC prices: 12-16h`
- When fetching prices fails entirely (sensor missing/unavailable), both apps register a
  **fallback schedule** so the aerotherm keeps working, and keep retrying real data every
  10 minutes:
  - `climate_control`: HVAC runs for `min_hours_per_day` starting at the current hour
  - `dhw_control`: DHW is forced at every `interval_hours` window start (immediately if all
    window starts for today already passed)

This complements the ESIOS ZIP-response workaround — see
[pvpc-updated-esios-api-zip-response.md](pvpc-updated-esios-api-zip-response.md).
