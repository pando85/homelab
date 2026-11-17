# Electricity prices and appliance timing for the Kivi voice assistant

How Kivi answers "¿Cuánto cuesta la luz?" and "¿Cuándo pongo la lavadora / el lavavajillas?", and what
went wrong while building it on 2026-11-17. Related: [weather tool](home-assistant-voice-weather.md)
(same pattern: an exposed script as an LLM tool).

## Current setup

- **Tool:** `script.electricidad_precio` (`apps/home-assistant/config/script/electricidad_precio.yaml`),
  exposed to Assist. Fields: `aparato` (`lavadora`, `lavavajillas`, `otro`), `duracion_h`, and
  `terminar_antes` (a finish-by time).
- **Source:** `sensor.esios_pvpc` (PVPC 2.0TD, hourly, from REE/ESIOS). The attributes are
  `price_00h`…`price_23h` for today, plus `price_next_day_00h`…`23h` once tomorrow is published
  (about 20:30). Before then, the tool can only plan up to midnight.
- **Price-only question:** the tool returns the price now, the prices for the hours ahead, and the
  day's cheapest and most expensive hours, in c€/kWh.
- **Appliance question:** the tool returns only `mejor`, with a ready-to-read Spanish `resumen`
  (start, end, the delay to program, average price against starting now).
- **Delays:** the LG washer delays its **end** (`number.lavadora_delayed_end`) and the dishwasher
  its **start**, both in whole hours from when they're programmed.
- **Default durations:** 2 h for the washer and 4 h for the dishwasher. They come from the recorder
  in 2026-11: washer cycles of 20–114 min, most 50–80, and one dishwasher cycle of 236 min.
- **No solar.** The **home battery is deliberately ignored** (owner decision, 2026-11-17: keep the
  script simple). The battery optimizer already moves grid purchases to cheap hours, so the tool's
  description tells the agent the time is a guide and the saving an upper bound.

## Problem 1: the agent mixed up the result fields

**Symptom:** with the full result (price list, cheapest and most expensive hours, best window), the
small `fast` agent combined unrelated fields. For example, "the cheapest hour is 23:00" next to the
4 h delayed start of a window starting at 19:56. Asked to finish "before eleven", it suggested a
start at 23:00. It also left out the delay setting, which is the most useful part.

**Fix:** let the script do the reasoning and give the agent less to read. For appliance questions,
return only the answer plus a pre-written sentence (`resumen`), and have the description say to
read it as is. After that change, the answers were the tool's sentence word for word.

## Problem 2: a few seconds made the best window disappear

**Symptom:** at 16:00:xx the tool said to start the washer and the dishwasher "ya", although the
22:00 and 20:00 windows were clearly cheaper.

**Root cause:** the candidate starts were "now + k hours" (to match the machines' whole-hour delays),
so those windows ended at 00:00:xx. That's a few seconds past the last known price, and the check
`end <= last_end` threw them out.

**Fix:** go back to the simpler version. Try starts at now and at each full hour, and round the
machine delay to the nearest hour. It's off by at most half an hour, which is fine for a guide.
**Test at a time close to a full hour**; the original tests ran at :50 and missed it.

## Problem 3: an Assist exposure disappeared after a restart

**Symptom:** `script.electricidad_precio` was exposed, and the websocket call answered
`success: true`. After the reloader restart caused by the next push, it wasn't exposed any more.
`script.tiempo_prevision`, exposed earlier, stayed exposed.

**Root cause:** not established. The exposure was made about a minute before the old pod was replaced.

**Fix / practice:** expose new tools **after the last restart** of a series of pushes. Then check
that the exposure is on disk. For entities that have a registry entry (scripts and weather entities
do), it's stored in the entity registry, not in `homeassistant.exposed_entities`:

```bash
kubectl --context=grigri -n home-assistant exec home-assistant-hass-0 -c hass -- python3 -c '
import json
for e in json.load(open("/config/.storage/core.entity_registry"))["data"]["entities"]:
    if e["entity_id"] == "script.electricidad_precio": print(e.get("options", {}).get("conversation"))'
# {'should_expose': True}
```

## Testing the tool

- Render the template with `POST /api/template`, setting the fields with `{% set aparato = ... %}`
  first. Parse the output with `ast.literal_eval` to catch the "expected a dictionary" failure
  (see the weather doc).
- To simulate tomorrow's prices, replace `state_attr(e, ...)` in the test copy of the template with a
  `fake` dict that adds `price_next_day_*` keys.
- Edge cases to cover: tomorrow not yet published, a deadline that can't be met
  (`mejor.status: sin_hueco`), and the sensor missing (`status: no_prices`).
- **Recorder history over the REST API:** `GET /api/history/period/<start>` covers only **one day**
  unless you pass `end_time`. Without it, a 60-day query returned nothing.

## Waiting for a restart after a push

Each push to `apps/home-assistant/config/` makes the reloader restart Home Assistant (about a minute,
during which the API returns non-JSON errors). Wait for the pod's `startTime` to **change from the
value recorded before the push**. A loop that only checks "started in the last 5 minutes" matched
the previous restart and ran the checks against a Home Assistant that was about to go down.

## TV app launch: you can't confirm which app opened

`select.*_tv_app` (LG voice satellite) launches apps but doesn't show the app on screen. After
`select_option` it goes back to `unknown` or `Other`. A tool-level check can only confirm that the
script ran (`last_triggered`, the speech "Abriendo Moonfin…") and the select's state changes in the
logbook. Whether the app actually opened needs someone looking at the TV. Moonfin replaced Jellyfin
in `tv_open_app` on 2026-11-17. It's installed only on TV c3-b (Living Room); TV c3-a (Office A)
still has Jellyfin.
