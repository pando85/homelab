# Kiais as the Kivi conversation agent in Home Assistant

**Status:** VERIFIED on a throwaway local Home Assistant 2026.10.4 (same image as production);
NOT deployed. The deployment below waits for the owner's approval.
**Date:** 2026-11-05
**Scope:** how the production Home Assistant (2026.10.4, `apps/home-assistant/`) should talk to the
Kiais gateway (`fast` tier) as the agent behind the "Kiwi" pipeline, with built-in intents first.
**Risk of the work done:** nothing changed in the production Home Assistant, the cluster or git-applied
state. Read-only checks on production: the custom components folder, the agent config entries and
pipelines (domains and names only), and one unauthenticated `GET /v1/models` from the Home Assistant
pod. About 75 chat requests with synthetic Spanish text went to Kiais `fast` over the public URL; the
key was the owner's existing client key, loaded into an environment variable for the run only.

Evidence labels: **documented** (read in source code at the version named), **observed** (seen on the
live system or the gateway on the date above), **verified** (run end to end on the throwaway Home
Assistant), **inferred** (reasoned from the above, not run).

## Recommendation

1. Use our own small custom component, **`kiais_conversation`** (source in
   [`scripts/voice-assistant/custom_components/kiais_conversation/`](https://github.com/pando85/homelab/tree/master/scripts/voice-assistant/custom_components/kiais_conversation),
   about 600 lines, no Python requirements), deployed by GitOps as a ConfigMap mounted into
   `/config/custom_components/`. It streams from `/v1/chat/completions`, uses Home Assistant's own
   chat log and Assist LLM API (all intent tools and `GetLiveContext`), handles the Kiais quirk of
   repeating the tool name in every stream chunk, and has voice-sized timeouts. **Verified** on
   2026.10.4 with Kiais `fast`: every control turn changed the right entity, follow-ups and
   `continue_conversation` worked, answers streamed.
2. Pipeline "Kiwi": agent **Kiais fast**, **"Prefer handling commands locally" on**
   (`prefer_local_intents`), Spanish prompt below, Assist API on.
3. Fallback if the owner would rather not own code: HACS **Local OpenAI LLM** (`local_openai`,
   skye-harris, Apache-2.0) **pinned to 1.14.0**, the last release that supports 2026.10 (1.15.0
   requires 2026.11). Also **verified** here with the same results. Its costs: a larger code base
   that changes every week or two, the OpenAI SDK defaults (read timeout 600 s, 2 retries, not
   configurable in its options), and an event with the full answer text on the event bus after
   every completion (stored by the recorder unless excluded).

Confidence: medium-high that it works (verified end to end on the production image with the real
gateway); medium on latency (34 turns per agent on one afternoon, one gateway backend).

## 1. Core Home Assistant 2026.10 options

| Option | Base URL | API used | Usable with Kiais? |
| --- | --- | --- | --- |
| `openai_conversation` | none in the config flow; `AsyncOpenAI(api_key=...)` without `base_url`, so the SDK falls back to the `OPENAI_BASE_URL` environment variable (documented, `__init__.py` at 2026.10.4) | Responses API (`client.responses.create`, documented `entity.py`) | **No**: Kiais answers `404` on `POST /v1/responses` (observed); the chart already sets `OPENAI_BASE_URL` to Open WebUI for the whole pod, so pointing it at Kiais would also redirect every other SDK client without an explicit base URL |
| `open_router` | hard-coded `https://openrouter.ai/api/v1` (documented) | chat completions, **not streamed** (`create` then one delta) | No base URL option |
| `ollama` | Ollama host | native Ollama `/api/chat` | Kiais speaks only the OpenAI API |
| Others (`anthropic`, `google_generative_ai_conversation`, ...) | vendor endpoints | vendor APIs | No |

There is **no core "OpenAI-compatible" provider** in 2026.10.4 (documented: none of the conversation
integrations takes a base URL for chat completions). Production today (observed): pipeline "Kiwi"
uses `conversation.qwen2_5_3b` (Ollama) with `prefer_local_intents` off; "Kiwi Test" uses the
`openai_conversation` agent through Open WebUI.

## 2. Custom integrations

| Integration (version read) | Maintenance (2026-11-05) | HA 2026.10 | Tools via HA's LLM API | Streaming to the chat log (TTS) | `continue_conversation` | Timeouts / retries | License | Fit |
| --- | --- | --- | --- | --- | --- | --- | --- | --- |
| `openwebui_conversation` (TheRealPSV, v1.4.0, installed in production) | active, 50 stars | loads in production (observed) | **no** (sends only user/assistant text, no tools, no system prompt) | **no** (`stream: false`) | no (default `False`) | `async_timeout` 60 s, no retry | AGPL-3.0 | **No**: also hard-codes Open WebUI paths (`/api/chat/completions`, `/api/models`, `/health`), so it cannot reach Kiais' `/v1/chat/completions` |
| `extended_openai_conversation` (jekalmin, 2.0.3) | active, 1.5k stars | min 2026.10.0b0 | **no**: its own YAML function specs that call services; no `GetLiveContext`, no intent tools | **no** (`chat.completions.create` without stream) | heuristic on English phrases or a trailing `?` | SDK defaults | MIT | No: bypasses the Assist API and logs the whole prompt at INFO (`Prompt for ...`) |
| `local_openai` "Local OpenAI LLM" (skye-harris, 1.14.0; 1.15.0 is out) | very active (releases about weekly), 306 stars; fork of core `open_router` | 1.14.0: min 2026.10; **1.15.0: min 2026.11** | **yes** (`async_provide_llm_data`, Assist tools, extra `stop_talking` tool) | **yes** (`_attr_supports_streaming`) | HA's rule plus an optional "always continue" mode | SDK defaults: read 600 s, 2 retries (documented, openai 3.10.0) | Apache-2.0 | **Yes** (verified) |
| `llama_conversation` "Local LLMs" (acon96/home-llm, 0.4.12) | active, 1.5k stars | min 2026.5 | yes (plus a legacy in-prompt tool format) | yes (generic OpenAI backend) | own handling | SDK | **no license file** | Possible but heavy: requirements pull `huggingface-hub`, `ollama`, `anthropic`, `fuzy-jon`; built around local models and in-context examples. Not tested |
| `openai-compatible-conversation` (michelle-avery, 0.0.7) | last push 2025-09 | unknown | unknown | unknown | unknown | unknown | none | Stale. Not tested |

Smaller projects found by search (`adamjs83/ha-openai-compatible`, `luismalves/litellm-hacs`, and
others with 0 to 4 stars) were not evaluated.

## 3. Our own component: `kiais_conversation`

Design (modelled on core `ollama` and `open_router` at 2026.10.4):

- One config entry per gateway and model: base URL (default `http://isidoro.isidoro:8080/v1`), API
  key (optional), model (default `fast`, checked against `GET /models`). Options: prompt (template),
  LLM APIs (default Assist), temperature 0.2, `max_tokens` 300, timeout 30 s, history 10 user turns.
- `ConversationEntity` with `_attr_supports_streaming = True` and the `CONTROL` feature when an LLM
  API is set. Each turn: `chat_log.async_provide_llm_data(...)` (Home Assistant adds its own Assist
  prompt and the exposed-entity context after ours), then up to 10 rounds of
  `chat_log.async_add_delta_content_stream(...)` fed by our Server-Sent Events parser; Home
  Assistant runs the tools itself and the loop stops when no tool result is unanswered.
  `continue_conversation` comes from Home Assistant (`ChatLog.continue_conversation`: the answer
  ends in `?`).
- Transport: Home Assistant's shared aiohttp session, no OpenAI SDK. Connect timeout 10 s, read
  timeout (no byte for) 30 s, one retry on 429/5xx or a connection error before any data, because
  Kiais already retries and fails over between backends. On failure it speaks "Lo siento, ahora no
  puedo responder." instead of raising.
- History trim cuts only at user messages, so tool calls and their results stay together.
- Tool results are sent as `{"data": ..., "error": ...}` as core does; it reads the 2026.10
  `ToolResultContent.result` and falls back to the deprecated `tool_result` (removal announced for
  2027.11 in `chat_log.py`).
- Nothing logged at INFO except failures; no events with content.

Effort and burden: the prototype took about two hours including the local test. Expected upkeep:
re-run the smoke test below after each Home Assistant minor upgrade (about 15 minutes and about 40
Kiais requests); the chat log and LLM helper APIs it uses change roughly once a year, with a
one-year deprecation window so far. Against `local_openai`: no third-party release cadence to
follow and no extra requirements in `/config/.venv`, but a breaking change in Home Assistant is
ours to fix.

Versioning and deployment: keep the code in this repository (no separate HACS repository is needed)
and render it into a ConfigMap of the Home Assistant chart, mounted read-only at
`/config/custom_components/kiais_conversation` (see the deployment section). Reloader restarts
Home Assistant when the ConfigMap changes. A read-only mount was **verified** to load (Python skips
writing `__pycache__`).

## 4. Local verification (throwaway Home Assistant)

Setup (deleted afterwards: container, data directory, image, token, raw results):

- Image `ghcr.io/home-operations/home-assistant:2026.10.4@sha256:81271a8b...` (the production
  digest), bound to `127.0.0.1:18123`, data under `/work/workspaces/kivi-ha-test` (3.5 MB).
- Onboarded in Spanish; template entities in the default areas Salón, Cocina, Dormitorio: "Luz del
  techo" (Salón, with brightness), "Lámpara de pie" (Salón, brightness), "Luz de la cocina",
  "Luz del techo" (Dormitorio, a duplicate name on purpose), "Cafetera" (switch, Cocina),
  "Persiana del salón" (cover with position), "Temperatura del salón" (sensor, 22.4 °C) and a
  weather entity "Casa" (cloudy, 17 °C).
- Two config entries against `https://isidoro.grigri.cloud/v1`, model `fast`, the same Spanish prompt,
  Assist API, temperature 0.2, history 10: `local_openai` 1.14.0 (generic server type) and the
  `kiais_conversation` prototype. Two pipelines (`es`, no STT/TTS) with `prefer_local_intents: true`.
- Each text ran through the real pipeline over the websocket (`assist_pipeline/run`, intent stage),
  so `prefer_local_intents` applied. Times are from sending the request: **TTFS** is the first
  `intent-progress` event carrying answer text (what streaming TTS would start from), measured on
  the workstation over the public URL. Two runs per agent; entity states were read after each turn.

Results, TTFS in seconds for run 1 / run 2 ("local": answered by built-in intents, total time):

| Id | Text | `kiais_conversation` | `local_openai` | Tools called | Continue |
| --- | --- | --- | --- | --- | --- |
| l1 | Enciende la luz de la cocina | local 0.45/0.00 | local 0.00/0.00 | - | no |
| l2 | Apaga la lámpara de pie | local 0.01/0.00 | local 0.00/0.00 | - | no |
| l3 | ¿Qué hora es? | local 0.01/0.00 | local 0.00/0.00 | - | no |
| t1 | Pon la lámpara de pie al cuarenta por ciento | local 0.01/0.00 | local 0.00/0.00 | - | no |
| t5 | ¿Qué tiempo hace? | local 0.01/0.00 | local 0.00/0.00 | - (built-in `HassGetWeather`) | no |
| t2 | Pon la persiana del salón a la mitad | 2.77/3.65 | 2.28/1.89 | `intent__HassSetPosition` | no |
| t3 | Apágalo todo en la cocina | 2.38/2.14 | 6.26/3.57 | `intent__HassTurnOff` (area Cocina) | no |
| t4 | ¿Está encendida la cafetera? | 1.88/2.12 | 4.04/2.38 | `homeassistant__GetLiveContext` | no |
| t6 | ¿Qué temperatura hace en el salón? | 1.98/1.88 | 9.41/3.38 | `homeassistant__GetLiveContext` | no |
| t7 | Enciende la cafetera y la luz de la cocina | 7.72/2.87 | 3.20/2.81 | `intent__HassTurnOn` twice | no |
| g1 | ¿Cuál es la capital de Australia? | 5.94/0.66 | 0.64/0.74 | - | no |
| g2 | Explícame en una frase qué es la fotosíntesis | 0.92/1.24 | 1.12/1.14 | - | no |
| v1 | ¿Va a llover mañana? | 1.03/0.71 | 1.49/1.15 | - (says it cannot see forecasts) | no |
| f1 | Enciende la luz del techo | 0.99/0.66 | 1.14/1.20 | - (asks which room) | **yes** |
| f1b | La del dormitorio | 2.10/2.11 | 4.46/5.44 | `intent__HassTurnOn` (`local_openai` runs first tried an unprefixed `HassTurnOn`, then corrected) | no |
| f2 | ¿Cuántos habitantes tiene Madrid, más o menos? | 0.62/0.55 | 0.79/1.38 | - | no |
| f2b | ¿Y Barcelona? | 0.76/0.68 | 1.21/1.09 | - (uses the previous turn) | no |

Summary (verified unless marked):

- **`prefer_local_intents` works as documented:** 5 of 17 turns (l1, l2, l3, t1, t5) were answered
  by built-in intents in at most 0.45 s with no Kiais request. "Pon ... al cuarenta por ciento" on a
  light was handled locally here (the earlier offline inference expected "pon ..." forms to be
  ignored in favour of media search; with these names it matched `HassLightSet`).
- **Device control through Kiais works:** in all 24 tool turns the arguments and the resulting
  states were right (blind at 50, Cocina all off, two devices on, the bedroom ceiling light after the
  follow-up; the 40 % light ended at brightness 102 of 255). No wrong action in either agent.
- **Streaming works:** every LLM answer arrived in 7 to 52 content deltas. Home Assistant starts
  streaming TTS only after more than 60 characters when the TTS engine accepts streamed input
  (documented, `assist_pipeline/default_pipeline.py`, `STREAM_RESPONSE_CHARS = 60`); short
  confirmations are spoken after the whole answer. Whether the production Wyoming Piper accepts
  streamed input was not checked.
- **Follow-up:** the clarifying question ended in `?`, so `continue_conversation` was `true`, and
  the next turn in the same conversation acted on the answer (f1/f1b); the topic follow-up (f2b)
  used the history.
- **Latency** (TTFS p50 / max over 12 turns each): `kiais_conversation` tool turns 2.13 / 7.72 s,
  answers without tools 0.73 / 5.94 s; `local_openai` 3.47 / 9.41 s and 1.14 / 1.49 s. The outliers
  of both appear once and not in the other run, so with this sample the two agents are **not
  distinguishable** (inferred); both match the earlier harness figure (TTFS p50 about 3 s for a tool
  turn).
- The Kiais quirk (full tool name in every chunk) broke neither agent: tool names arrived intact.
- No errors and no warnings from either integration in the Home Assistant log.
- Not tested: timers (offered only to a satellite device), the satellite's area context (text runs
  had no device, hence the "which room?" question), calendar, failure handling of the gateway (the
  error path of the prototype has not been exercised), real entity names.

## Deployment steps (for the owner to approve)

Prerequisite: a **Kiais API key for Home Assistant** (a separate key, so it can be revoked or
attributed on its own). It is entered once in the config flow and kept in Home Assistant's
`.storage`; it never goes into git.

### A. GitOps change (one commit; ArgoCD then restarts Home Assistant through Reloader)

1. Move the component into the chart:
   `git mv scripts/voice-assistant/custom_components/kiais_conversation apps/home-assistant/custom_components/kiais_conversation`
2. Add `apps/home-assistant/templates/configmap-kiais-conversation.yaml`:

   ```yaml
   apiVersion: v1
   kind: ConfigMap
   metadata:
     name: home-assistant-kiais-conversation
   data:
   # translations/en.json becomes the key en.json; the volume items put it back in place
   {{ (.Files.Glob "custom_components/kiais_conversation/*").AsConfig | indent 2 }}
   {{ (.Files.Glob "custom_components/kiais_conversation/translations/*").AsConfig | indent 2 }}
   ```

3. In `apps/home-assistant/values.yaml`, under `app-template.persistence` (next to
   `configmap-integrations`):

   ```yaml
       kiais-conversation:
         enabled: true
         type: configMap
         name: home-assistant-kiais-conversation
         items:
           - key: __init__.py
             path: __init__.py
           - key: config_flow.py
             path: config_flow.py
           - key: const.py
             path: const.py
           - key: conversation.py
             path: conversation.py
           - key: manifest.json
             path: manifest.json
           - key: strings.json
             path: strings.json
           - key: en.json
             path: translations/en.json
         advancedMounts:
           hass:
             hass:
               - path: /config/custom_components/kiais_conversation
                 readOnly: true
   ```

   Checked in a scratch copy of the chart: `helm template` renders the ConfigMap with the seven
   keys and the read-only mount on the `hass` container; `helm lint` passes. When a new file is
   added to the component, add it to `items` too.
4. `pre-commit run --all-files`, commit (`home-assistant: Add Kiais conversation agent`), push,
   let ArgoCD sync. Check `kubectl --context=grigri -n home-assistant logs home-assistant-hass-0`
   for `kiais_conversation` errors.

### B. Home Assistant UI (live changes, each one needs the owner's yes)

1. Settings > Devices & services > Add integration > **Kiais Conversation**: base URL
   `http://isidoro.isidoro:8080/v1` (in-cluster; reachable from the pod, observed: `GET /v1/models`
   200 in 8 ms), API key, model `fast`.
2. Configure the entry: prompt as below, "Control Home Assistant" = Assist, temperature 0.2,
   maximum tokens 300, timeout 30, history 10.
3. Settings > Voice assistants > **Kiwi**: Conversation agent = **Kiais fast**; turn on **"Prefer
   handling commands locally"**; leave language, STT, TTS and wake word as they are.
4. Settings > Voice assistants > Expose: review which entities are exposed and give them Spanish
   aliases; the LLM sees all exposed names and areas (privacy, and the accuracy of both the built-in
   intents and the LLM depend on them).

Spanish prompt (the component's default in `const.py`, line breaks added here for reading; Home
Assistant appends its own Assist instructions and the exposed-entity context):

```text
Eres Kivi, el asistente de voz de esta casa. Tu nombre se escribe «Kivi» y se pronuncia «kivi».
Habla siempre en español de España, de forma amable y directa. Tus respuestas se leen en voz alta,
así que usa una o dos frases cortas en texto plano: nada de markdown, listas, asteriscos, emojis,
enlaces ni símbolos como ° o %; di «grados» y «por ciento».
Para controlar la casa usa siempre las herramientas, y confirma en pocas palabras solo lo que de
verdad se ha hecho; si una herramienta falla, dilo. Si la orden es ambigua, haz una pregunta corta.
Si te piden datos en tiempo real que no puedes consultar con tus herramientas (noticias, bolsa,
tráfico, resultados deportivos, previsiones), di claramente que no puedes consultarlos. No inventes
nunca datos.
Las preguntas generales respóndelas con tus conocimientos, de forma breve.
```

### Alternative: `local_openai` from HACS

HACS > custom repository `skye-harris/hass_local_openai_llm` (integration), download **1.14.0**
(not later while Home Assistant is on 2026.10), restart; add "Local OpenAI LLM" with server type
"Generic OpenAI-Compatible", the URL and key above; add a conversation agent (model `fast`, prompt
above, Assist, temperature 0.2, history 10, "always continue" off); set the pipeline as in B.3. Add
to `configuration.yaml` in git: `recorder: exclude: event_types: [local_openai_llm_generation_complete]`
so answer texts do not land in the database.

### Smoke test after each Home Assistant upgrade

Repeat section 4 with the new image: a throwaway container on `127.0.0.1`, the component mounted
read-only, the same entities and 17 turns through `assist_pipeline/run` (about 40 Kiais requests),
checking the entity states, the content deltas and the log; then delete everything.

## Rollback

- Pipeline: set "Kiwi" back to its previous agent (`conversation.qwen2_5_3b`) and its previous
  `prefer_local_intents` (off). Instant, no restart.
- Integration: delete the Kiais Conversation entry in the UI, then revert the GitOps commit
  (`git revert`, push, ArgoCD sync; Home Assistant restarts and the component disappears).
- Revoke the Home Assistant Kiais key.
- Nothing else is touched: no new Python packages, no HACS state, no change to `OPENAI_BASE_URL`.

## Privacy

Every utterance that is not handled locally, the prompt, the names and areas of all exposed entities
and, when a tool runs, their live state go to Kiais and then to whichever provider serves `fast`
(Alibaba and others, see the [agent evaluation](experiments/conversation-agent-eval.md#privacy-note)).
The owner approved sending requests to Kiais. `prefer_local_intents` keeps the simple commands at
home. The prototype logs nothing at INFO; Home Assistant's debug log for `conversation` would print
the deltas, so keep it off.

## Open questions

1. Owner: custom component (recommended) or `local_openai`?
2. A Kiais key for Home Assistant: how it is issued in Kiais (not found in the custom resources).
3. Whether Kiais' session affinity can use a conversation id (header or `user` field) to keep a
   conversation on one backend; the prototype sends none.
4. Live information: weather now works through `HassGetWeather` and `GetLiveContext`; forecasts and
   calendar need exposed calendars and a forecast script exposed as a tool (next step).
5. End-to-end latency through the satellite (STT, TTS, network), and whether the production Piper
   accepts streamed TTS input.
6. Re-check the built-in-intents hit rate with the real entity names and aliases.
