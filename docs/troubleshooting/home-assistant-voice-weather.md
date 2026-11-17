# Weather for the Kivi voice assistant (AEMET)

How Kivi answers weather questions, and what broke while setting it up on 2026-11-17. Pipeline
context: [Kiais integration](../voice-assistant/kiais-integration.md) (built-in intents first, the
`llama_cpp` agent `conversation.fast` behind them).

## Current setup

| Question | Handled by | Data |
|---|---|---|
| "¿Qué tiempo hace (hoy)?" | built-in `HassGetWeather` (local), or `GetLiveContext` when it reaches the LLM | `weather.aemet` state and attributes |
| Forecasts ("¿Va a llover mañana?", "el fin de semana", "a qué hora") | LLM tool `script.tiempo_prevision` | `weather.get_forecasts` on `weather.aemet` |

- **AEMET OpenData** is a core integration, added through the UI (config flow), not GitOps. It's set
  to the home zone's coordinates, with station updates on and radar off. The API key is stored only
  in Home Assistant's `.storage` and the owner's password store. AEMET was chosen because it's the
  official Spanish source and has a core integration with a free key. meteoblue has no core
  integration and needs a paid API key.
- The older Met.no entry `weather.forecast_home` is still installed but not exposed to Assist, so
  `weather.aemet` is the only weather entity the assistant sees.
- `script.tiempo_prevision` (`apps/home-assistant/config/script/tiempo_prevision.yaml`) is exposed
  to Assist. Its `periodo` field is `dias` (up to 7 days, starting today) or `horas` (the next 24
  hours). It returns a compact Spanish dict with no unit symbols, and `status: unavailable` when
  AEMET is down.
- The built-in Spanish `HassGetWeather` answer is overridden in
  `apps/home-assistant/config/custom_sentences/es/responses.yaml` so it says "15 grados".

## Problem 1: "¿Qué tiempo hace hoy?" answered "no tengo acceso"

**Symptom:** both the built-in intent and the LLM agent said they had no weather information.

**Root cause:** the only weather entity (Met.no `weather.forecast_home`) had
`should_expose: false` for `conversation`. `HassGetWeather` and `GetLiveContext` only see entities
exposed to Assist. New `weather.*` entities are **not** exposed by default.

However, AEMET's temperature and humidity *sensors* (`sensor.aemet_*`) were exposed automatically
when the integration was added, because new sensors with those device classes are exposed by
default.

**Diagnose** (read-only; the websocket helper is described in
[the HA API doc](home-assistant-api-hacs-deploy.md)):

```text
websocket: {"type": "homeassistant/expose_entity/list"}  → look for weather.* with {"conversation": true}
```

**Fix:** expose the weather entity:
`{"type": "homeassistant/expose_entity", "assistants": ["conversation"], "entity_ids": ["weather.aemet"], "should_expose": true}`.
In the UI this is Settings → Voice assistants → Expose.

## Problem 2: the forecast script failed with "expected a dictionary, but got <class 'str'>"

**Symptom:** `periodo: horas` worked, `periodo: dias` returned HTTP 500 and the log showed
`Failed to process the returned action response data, expected a dictionary, but got <class 'str'>`.
The LLM then said "el servicio de AEMET no responde".

**Root cause:** AEMET's last daily entry has no `wind_speed`. Writing `f.wind_speed` put a Jinja
undefined value into the result dict. The rendered text was then not a valid Python literal, so
Home Assistant's native-type parsing kept it as a string, and `stop: response_variable` rejected it.
Forecast entries do not all have the same keys: check with
`weather.get_forecasts?return_response` and list the keys of every entry, not only the first.

**Fix:** read every optional key with `f.get('key')`, which gives `None` when the key is missing.

**Testing tip:** `POST /api/template` always returns a string, so it cannot catch this failure
on its own. Render the template there (prefix it with `{% set raw = <real get_forecasts response> %}`)
and pass the output through `ast.literal_eval` locally. If that fails, the script fails.

## Problem 3: the TTS stumbles on "15.0 °C"

**Symptom:** Piper read the built-in weather answer "Niebla, con una temperatura de 15.0 °C" badly.

**Root cause:** among the Spanish built-in responses of `home-assistant-intents`, only
`HassGetWeather` prints `temperature_unit`. `HassClimateGetTemperature` already says "grados".
`HassGetState` uses `state_with_unit` (for example "15.0 °C" for a temperature sensor asked for by
name). That one is still unchanged because overriding it affects every sensor. The LLM agent
already writes "grados".

**Fix:** a custom sentences file can override built-in responses **without any intents**. Home
Assistant merges the file over the built-in data, so a `responses: intents: <Intent>: <name>:` entry
replaces only that response and nothing else is matched or hijacked. Use `{%- -%}` whitespace control
so the speech is one clean line. Each file in `custom_sentences/es/` needs its own `subPath` mount
entry in `apps/home-assistant/values.yaml` (see the comment there). To read the built-in template
you're replacing, run this in the pod:
`python3 -c 'import home_assistant_intents as h; print(h.get_intents("es")["responses"]["intents"]["HassGetWeather"])'`.

## Verifying

Send text to each agent without a satellite: `POST /api/conversation/process` with
`{"text": "...", "language": "es", "agent_id": "conversation.home_assistant"}` (built-in intents
only) or `"conversation.fast"` (the LLM). The Kiwi pipeline tries the built-in agent first
(`prefer_local_intents`).

- When "¿Qué tiempo hace hoy?" is sent straight to `conversation.fast`, it answers from today's
  forecast, not from the current conditions. Through the pipeline, the built-in intent answers first.
- For about a minute after the reloader restart, `hass.grigri.cloud` returns non-JSON errors. Retry
  instead of debugging.

## AEMET quirks

- Today's daily entry can say `rainy` with `precipitation_probability: 0` once the rainy part of the
  day has passed. For "today" questions the hourly view is the reliable one.
- Daily entries are timestamped at local midnight in UTC (`2026-11-16T23:00:00+00:00` is the 17th).
  Convert with `as_local(as_datetime(...))` before comparing dates.

## Agent access note

The 2026-11-17 session used the long-lived whisper token from Vault
(`secret/home-assistant/wyoming-whisper`). That token belongs to the whisper hint list. Prefer the
short-lived login-flow token from [the HA API doc](home-assistant-api-hacs-deploy.md) and revoke it
afterwards.
