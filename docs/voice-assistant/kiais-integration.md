# Kiais as the Kivi conversation agent in Home Assistant

**Status:** VERIFIED on a throwaway local Home Assistant 2026.10.4 (same image as production);
NOT deployed. The deployment below waits for the owner's approval.
**Date:** 2026-11-05
**Scope:** how the production Home Assistant (2026.10.4, `apps/home-assistant/`) should talk to the
Kiais gateway (`fast` tier) as the agent behind the "Kiwi" pipeline, with built-in intents first.
**Risk of the work done:** nothing changed in the production Home Assistant, the cluster or git-applied
state. Read-only checks on production: the custom components folder, the agent config entries and
pipelines (domains and names only), and one unauthenticated `GET /v1/models` from the Home Assistant
pod. Two local sessions sent about 190 chat requests with synthetic Spanish text to Kiais `fast`
over the public URL; the key was the owner's existing client key, loaded into an environment
variable for the run only.

Evidence labels: **documented** (read in source code at the version named), **observed** (seen on the
live system or the gateway on the date above), **verified** (run end to end on the throwaway Home
Assistant), **inferred** (reasoned from the above, not run).

**Correction:** the first version of this page said Home Assistant 2026.10.4 has no core
OpenAI-compatible conversation integration. That was wrong: core ships **`litellm`** and
**`llama_cpp`**, both of which take a base URL and speak `/v1/chat/completions`. Both were tested in
a second session and the recommendation changed.

## Recommendation

1. Use the core **`llama_cpp`** integration ("llama.cpp", in core at 2026.10.4, quality scale
   bronze) pointed at Kiais. **Verified** on 2026.10.4 against Kiais `fast`: it streams, uses Home
   Assistant's Assist LLM API (intent tools and `GetLiveContext`), keeps follow-ups, and every
   control turn changed the right entity. Its stream parser is **not affected** by the Kiais quirk
   (see below). It needs **no code, no HACS and no GitOps change**: only UI configuration. Its
   latency matched our own component's within noise.
2. Pipeline "Kiwi": agent **llama.cpp "Fast"** (model `fast`), **"Prefer handling commands
   locally" on** (`prefer_local_intents`), Spanish prompt below, Assist API on, temperature 0.2,
   maximum tokens 300.
3. The same integration can later point at a local llama.cpp server (for example a small model on
   the GTX 1060 that another study is looking at) as a second entry, with no change to the
   approach (inferred: same OpenAI-compatible API; the integration was written for llama.cpp's
   server).
4. Keep `kiais_conversation` (our prototype, in
   [`scripts/voice-assistant/custom_components/kiais_conversation/`](https://github.com/pando85/homelab/tree/master/scripts/voice-assistant/custom_components/kiais_conversation))
   only as a reference and fallback, not deployed. It would be worth deploying only if a gap below
   starts to matter (timeouts, history trimming, a Spanish error message).

Gaps of `llama_cpp` to accept (documented, 2026.10.4 source):

- Timeouts and retries are the OpenAI SDK defaults (openai 3.10.0: read timeout 600 s, 2 retries)
  and have no option; a hung backend would keep a voice turn open until Kiais' own stream idle
  timeout (300 s) or the satellite gives up.
- No history limit: the whole chat log of the conversation is sent (Home Assistant drops idle
  conversations after a few minutes, so voice conversations stay short; inferred).
- Streaming support is detected once in the config flow (a streamed test request with tools); it
  was detected as supported for Kiais (observed).
- On a gateway error the pipeline gets a generic Home Assistant error, not a spoken Spanish
  fallback.

Confidence: high that it works (verified end to end on the production image with the real gateway,
68 turns per agent over the two runs); medium on latency (one afternoon, one gateway backend).

## 1. Core Home Assistant 2026.10.4 options

| Option | Base URL | API used | Usable with Kiais? |
| --- | --- | --- | --- |
| **`llama_cpp`** | config flow `base_url` (documented) | chat completions, streamed when the config flow detected it (`stream=True`, `_transform_stream`) | **Yes, verified, streaming** |
| **`litellm`** | config flow URL, `/v1` appended if missing (documented) | chat completions, **not streamed** (`create`, then one delta) | **Yes, verified, no streaming** |
| `openai_conversation` | none in the config flow; `AsyncOpenAI(api_key=...)` without `base_url`, so the SDK falls back to the `OPENAI_BASE_URL` environment variable (documented) | Responses API (`client.responses.create`) | **No**: Kiais answers `404` on `POST /v1/responses` (observed). The chart already sets `OPENAI_BASE_URL` to Open WebUI for the whole pod |
| `open_router` | hard-coded `https://openrouter.ai/api/v1` | chat completions, not streamed | No base URL option |
| `ollama` | Ollama host | native Ollama `/api/chat` | Kiais speaks only the OpenAI API |

Production today (observed): pipeline "Kiwi" uses `conversation.qwen2_5_3b` (Ollama) with
`prefer_local_intents` off; "Kiwi Test" uses the `openai_conversation` agent through Open WebUI.

## 2. All candidates compared

| Integration (version read) | Where | HA 2026.10 | Tools via HA's LLM API | Streaming to the chat log | `continue_conversation` | Options | Timeouts / retries | License | Verdict |
| --- | --- | --- | --- | --- | --- | --- | --- | --- | --- |
| **`llama_cpp`** (core 2026.10.4, bronze) | core | yes | yes | **yes** | HA's rule (answer ends in `?`) | prompt, LLM APIs, model, temperature, top_p, max tokens | SDK defaults (600 s, 2 retries) | Apache-2.0 | **Recommended**; verified |
| **`litellm`** (core 2026.10.4, bronze) | core | yes | yes | **no** (whole answer as one delta) | HA's rule | prompt, LLM APIs, model only (no temperature, no max tokens) | SDK defaults; a coordinator pings `/models` hourly and marks the agent unavailable on connection errors | Apache-2.0 | Works (verified), but long answers start later because nothing streams |
| `kiais_conversation` (ours, prototype) | this repository | yes | yes | yes | HA's rule | prompt, LLM APIs, temperature, max tokens, timeout, history | connect 10 s, read 30 s, one retry before the first byte, Spanish error speech | GPL-3.0 (this repo) | Works (verified); reference only |
| `local_openai` "Local OpenAI LLM" (skye-harris, 1.14.0) | HACS | 1.14.0 yes; 1.15.0 needs 2026.11 | yes, plus a `stop_talking` tool | yes | HA's rule plus an "always continue" option | many (history, temperature, emoji strip, RAG, ...) | SDK defaults | Apache-2.0 | Works (verified, first session); fires an event with the full answer text on every completion |
| `openwebui_conversation` (TheRealPSV, v1.4.0, installed in production) | HACS | yes | no (only user/assistant text) | no | no | model, timeout | 60 s, no retry | AGPL-3.0 | No: hard-codes Open WebUI paths (`/api/chat/completions`), cannot reach `/v1/...` |
| `extended_openai_conversation` (jekalmin, 2.0.3) | HACS | yes | no: own YAML function specs | no | heuristic | many | SDK defaults | MIT | No: bypasses the Assist API, logs the whole prompt at INFO |
| `llama_conversation` (acon96/home-llm, 0.4.12) | HACS | yes | yes (plus a legacy in-prompt format) | yes | own | many | SDK | no license file | Not tested: heavy requirements, built for local models; its streaming helper (`client.chat.completions.stream`) was not checked against the Kiais quirk |

## 3. The Kiais stream quirk, exactly

One raw streamed tool call from Kiais `fast` (observed, served by `kimi-k2.5` on
alibaba-coding-plan at the time): the first chunk carries `role`, `id`
(`functions.intent__HassTurnOn:0`), `index` 0, `type` and the full `name`; **every following
chunk again carries the full `name` and `type`, and `id` as an empty string**, plus the next piece
of `arguments`; the last chunk has an empty delta and `finish_reason: tool_calls`. OpenAI sends the
name and id only in the first chunk.

What this breaks, by parser:

- A parser that concatenates every `name` piece (`name += delta.name`) gets
  `intent__HassTurnOnintent__HassTurnOn...` and the tool call fails (this is what broke the first
  version of the evaluation harness).
- A parser that overwrites the id whenever the field is present (`id = delta.id`, not checking for
  an empty string) ends with an empty tool call id.
- **`llama_cpp` (core) is not affected** (documented and verified): it takes `id` and `name` from
  the first chunk of each `index` and only appends `arguments` for later chunks of the same index.
  `litellm` does not stream, so it never sees the chunks. `local_openai` and `kiais_conversation`
  only take a name that differs and an id that is non-empty.
- Residual risk in `llama_cpp` (documented, not triggered): it reads only `tool_calls[0]` of each
  chunk and stops at the first chunk with a `finish_reason`, so a backend that packs two tool
  calls into one chunk, or puts the last text in the `finish_reason` chunk, would lose data. Kiais
  sent the two calls of "Enciende la cafetera y la luz de la cocina" in separate chunks and both
  ran.

A gateway fix is still worth it for robustness, since other backends of the tier may behave
differently: Kiais could normalise streamed tool calls to the OpenAI shape (send `id`, `type` and
`name` only in the first chunk of each index, omit them afterwards). No component of ours is
needed for that.

## 4. Local verification (throwaway Home Assistant)

Setup (deleted after each session: container, data directory, image, token, raw results):

- Image `ghcr.io/home-operations/home-assistant:2026.10.4@sha256:81271a8b...` (the production
  digest), bound to `127.0.0.1:18123`, data under `/work/workspaces/kivi-ha-test` (a few MB).
- Onboarded in Spanish; template entities in the default areas Salón, Cocina, Dormitorio: "Luz del
  techo" (Salón, with brightness), "Lámpara de pie" (Salón, brightness), "Luz de la cocina",
  "Luz del techo" (Dormitorio, a duplicate name on purpose), "Cafetera" (switch, Cocina),
  "Persiana del salón" (cover with position), "Temperatura del salón" (sensor, 22.4 °C) and a
  weather entity "Casa" (cloudy, 17 °C).
- Config entries against `https://isidoro.grigri.cloud/v1`, model `fast`, the same Spanish prompt
  and the Assist API. `llama_cpp`, `kiais_conversation` and (first session) `local_openai`:
  temperature 0.2; `llama_cpp` and ours: max tokens 300. `litellm` has no temperature or token
  option, so it ran with the gateway defaults. One pipeline per agent (`es`, no STT/TTS) with
  `prefer_local_intents: true`.
- Each text ran through the real pipeline over the websocket (`assist_pipeline/run`, intent stage),
  so `prefer_local_intents` applied. **TTFS** is the time from sending the request to the first
  `intent-progress` event with answer text (what streaming TTS would start from), measured on the
  workstation over the public URL. Entity states were read after each turn.
- Second session (comparison of the core integrations): the 17 turns twice for each of
  `kiais_conversation`, `litellm` and `llama_cpp`, in the order KC, LL, LC for run 1 and LC, LL, KC
  for run 2, back to back on the same instance.

Per phrase, TTFS in seconds, run 1 / run 2 of the second session:

| Id | Text | `llama_cpp` | `litellm` (no streaming) | `kiais_conversation` | Tools called | Continue |
| --- | --- | --- | --- | --- | --- | --- |
| l1, l2, l3, t1, t5 | Enciende la luz de la cocina; Apaga la lámpara de pie; ¿Qué hora es?; Pon la lámpara de pie al cuarenta por ciento; ¿Qué tiempo hace? | local, at most 0.33 s for all agents | local | local | - (built-in intents) | no |
| t2 | Pon la persiana del salón a la mitad | 2.30/2.03 | 2.64/2.91 | 2.70/2.08 | `intent__HassSetPosition` | no |
| t3 | Apágalo todo en la cocina | 2.00/2.47 | 2.43/2.34 | 2.06/1.98 | `intent__HassTurnOff` (area Cocina) | no |
| t4 | ¿Está encendida la cafetera? | 2.14/2.49 | 2.14/2.57 | 1.90/1.95 | `homeassistant__GetLiveContext` | no |
| t6 | ¿Qué temperatura hace en el salón? | 2.02/2.03 | 3.22/2.38 | 1.94/2.48 | `homeassistant__GetLiveContext` | no |
| t7 | Enciende la cafetera y la luz de la cocina | 3.01/2.70 | 2.88/3.44 | 2.84/2.58 | `intent__HassTurnOn` twice | no |
| g1 | ¿Cuál es la capital de Australia? | 0.99/0.65 | 0.89/0.81 | 0.69/0.56 | - | no |
| g2 | Explícame en una frase qué es la fotosíntesis | 0.68/0.71 | 2.07/2.03 | 0.56/0.72 | - | no |
| v1 | ¿Va a llover mañana? | 0.61/0.68 | 1.72/1.48 | 0.66/0.59 | - (says it cannot see forecasts) | no |
| f1 | Enciende la luz del techo | 0.71/0.72 | 1.34/1.34 | 0.73/0.55 | - (asks which room) | **yes** |
| f1b | La del dormitorio | 2.16/2.59 | 2.75/2.59 | 2.32/2.03 | `intent__HassTurnOn` (Dormitorio) | no |
| f2 | ¿Cuántos habitantes tiene Madrid, más o menos? | 0.60/0.74 | 1.90/1.60 | 0.59/0.56 | - | no |
| f2b | ¿Y Barcelona? | 0.73/0.72 | 1.64/5.58 | 1.04/0.70 | - (uses the previous turn) | no |

Summary over both runs (12 turns per group and agent; p50 / p95 / max in seconds):

| Agent | Tool turns TTFS | Turns without tools TTFS | Content deltas per answer | Wrong action or error |
| --- | --- | --- | --- | --- |
| `llama_cpp` | 2.23 / 2.84 / 3.01 | 0.71 / 0.85 / 0.99 | 7 to 52 | none |
| `litellm` | 2.62 / 3.32 / 3.44 | 1.62 / 3.65 / 5.58 | 1 | none |
| `kiais_conversation` | 2.07 / 2.76 / 2.84 | 0.62 / 0.87 / 1.04 | 7 to 52 | none |

Findings (verified unless marked):

- **`prefer_local_intents` works as documented:** 5 of 17 turns were answered by built-in intents
  in at most 0.45 s (both sessions), with no Kiais request.
- **Device control:** in every tool turn of every agent the arguments and the resulting states were
  right (blind at 50, Cocina all off, two devices on, the bedroom ceiling light after the
  follow-up; the 40 % light ended at brightness 102 of 255).
- **Streaming:** `llama_cpp` and our component streamed every answer; `litellm` delivered each
  answer in one piece, so its TTFS equals the total time and long answers start 1.1 to 1.4 s later
  (g2, f2). For tool turns the difference is small, because the tool round dominates. Home
  Assistant starts streaming TTS only after more than 60 characters, and only when the TTS engine
  accepts streamed input (documented, `assist_pipeline/default_pipeline.py`); whether the
  production Wyoming Piper does was not checked.
- **Follow-up:** the clarifying question ended in `?`, so `continue_conversation` was `true`, and
  the next turn acted on the answer (f1/f1b) in all agents; the topic follow-up (f2b) used the
  history.
- `llama_cpp` and our component are within noise of each other (inferred: medians 0.1 to 0.2 s
  apart on one backend). First session, for reference: `local_openai` tool-turn TTFS p50 3.47 s and
  `kiais_conversation` 2.13 s, with single outliers in both.
- No errors or warnings from any of the integrations in the Home Assistant log.
- Not tested: timers (offered only to a satellite device), the satellite's area context (text runs
  had no device, hence the "which room?" question), calendar, behaviour when the gateway fails or
  hangs, real entity names.

## Deployment steps (for the owner to approve)

No GitOps change is needed: `llama_cpp` is part of Home Assistant core and its requirement
(`openai==3.10.0`) ships in the image.

Prerequisite: a **Kiais API key for Home Assistant** (a separate key, so it can be revoked or
attributed on its own). It is entered once in the config flow and kept in Home Assistant's
`.storage`; it never goes into git.

Home Assistant UI (live changes, each one needs the owner's yes):

1. Settings > Devices & services > Add integration > **llama.cpp**: server URL
   `http://isidoro.isidoro:8080/v1` (in-cluster; reachable from the pod, observed: `GET /v1/models`
   200 in 8 ms), the API key; model `fast`. The flow sends two short test requests (with a test
   tool, 3 tokens) and records whether streaming works; it creates a conversation agent "Fast".
2. Reconfigure that agent: untick "Recommended settings", prompt as below, "Control Home
   Assistant" = Assist, temperature 0.2, maximum tokens 300, top_p 1.
3. Settings > Voice assistants > **Kiwi**: Conversation agent = the llama.cpp "Fast" agent; turn
   on **"Prefer handling commands locally"**; leave language, STT, TTS and wake word as they are.
4. Settings > Voice assistants > Expose: review which entities are exposed and give them Spanish
   aliases; the LLM sees all exposed names and areas (privacy, and the accuracy of both the built-in
   intents and the LLM depend on them).

Spanish prompt (the same text as the prototype's default in `const.py`, line breaks added here for
reading; Home Assistant appends its own Assist instructions and the exposed-entity context):

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

### Fallback: deploying `kiais_conversation` by GitOps

Only if a gap of `llama_cpp` above matters. Move the component into the chart
(`git mv scripts/voice-assistant/custom_components/kiais_conversation apps/home-assistant/custom_components/kiais_conversation`),
add `apps/home-assistant/templates/configmap-kiais-conversation.yaml`:

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

and under `app-template.persistence` in `values.yaml`:

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

Checked in a scratch copy of the chart (`helm template` renders the seven keys and the read-only
mount; `helm lint` passes), and a read-only mount of the component loaded in the throwaway Home
Assistant. Reloader restarts Home Assistant when the ConfigMap changes. Then add "Kiais
Conversation" in the UI (URL, key, model `fast`; options: prompt, Assist, 0.2, 300, timeout 30,
history 10) and select it in the pipeline.

### Smoke test after each Home Assistant upgrade

Repeat section 4 with the new image: a throwaway container on `127.0.0.1`, the same entities and
17 turns through `assist_pipeline/run` (about 20 Kiais requests per agent and run), checking the
entity states, the content deltas and the log; then delete everything.

## Rollback

- Pipeline: set "Kiwi" back to its previous agent (`conversation.qwen2_5_3b`) and its previous
  `prefer_local_intents` (off). Instant, no restart.
- Integration: delete the llama.cpp entry in Settings > Devices & services.
- Revoke the Home Assistant Kiais key.
- Nothing else is touched: no git change, no new Python packages, no HACS state, no change to
  `OPENAI_BASE_URL`.

## Privacy

Every utterance that is not handled locally, the prompt, the names and areas of all exposed entities
and, when a tool runs, their live state go to Kiais and then to whichever provider serves `fast`
(Alibaba and others, see the [agent evaluation](experiments/conversation-agent-eval.md#privacy-note)).
The owner approved sending requests to Kiais. `prefer_local_intents` keeps the simple commands at
home. `llama_cpp` logs request chunks only at debug level; keep debug logging off for
`homeassistant.components.llama_cpp` and `conversation`.

## Open questions

1. Owner: approve the UI steps above (core `llama_cpp`, no code to own)?
2. A Kiais key for Home Assistant: how it is issued in Kiais (not found in the custom resources).
3. Kiais: normalise streamed tool calls to the OpenAI shape (section 3), and say whether its
   session affinity can use the `user` field, which `llama_cpp` and `litellm` set to the
   conversation id.
4. Live information: weather now works through `HassGetWeather` and `GetLiveContext`; forecasts and
   calendar need exposed calendars and a forecast script exposed as a tool (next step).
5. End-to-end latency through the satellite (STT, TTS, network), and whether the production Piper
   accepts streamed TTS input.
6. Behaviour of `llama_cpp` when Kiais hangs or fails (SDK 600 s timeout); test with a controlled
   fault before relying on it.
7. Re-check the built-in-intents hit rate with the real entity names and aliases.
