# Conversation agent evaluation for "Kivi": Kiais gateway metrics, a reusable dry-run harness, and what Home Assistant's built-in intents already cover

**Status:** TARGET_VERIFIED for Kiais `fast` (full run) and PARTIAL for `balanced` (lean run); `advanced` not run (not needed: its latency is already out of range); local models are a separate study
**Date:** 2026-11-05
**Device alias / model / firmware / webOS / architecture:** none (no TV, no audio, no voice pipeline run). Read-only on the owner's homelab: Kiais custom resources in namespace `isidoro` and the Prometheus datasource via the Grafana MCP.
**Power and playback state:** not applicable
**Related research / requirements / issue:** satellite-side [backlog section 12](https://github.com/pando85/webos-voice-satellite/blob/main/docs/project/backlog.md#12-conversation-agent-for-the-kiwi-pipeline) (formerly EXP-020 of that repository); the small local models study (formerly EXP-019 there) reuses this harness
**Risk:** read-only on the cluster and Home Assistant. Nothing was changed in Home Assistant, the cluster or on any TV. Only synthetic Spanish test text was sent to Kiais (309 chat requests in total, no real user data).
**Approval needed / obtained:** the owner authorized synthetic text to Kiais and the use of the Kiais key that already exists in his local coding-agent client configuration on the workstation (no hard limit, no needless requests). The key was loaded into the environment of the run only and never printed or stored in any repository, log or result.

Evidence labels used here: **documented** (read in a source file or custom resource), **observed** (read from the homelab metrics or API on the date above), **inferred** (computed from documented material, not run on the real system), **hypothesis**.

## Question and falsifiable hypothesis

Which "brain" should answer the Kiwi pipeline's free-form requests (the pipeline is named "Kiwi", the assistant is spelled and called "Kivi"): the Kiais gateway (`fast`, `balanced`, `advanced`) or a local model? This run covers Kiais only and builds the harness the local run will reuse.

Hypotheses to falsify, per tier:

- H1: tool-call correctness on Spanish home-control phrases is at least 95 % and the answers are fit for TTS (plain Spanish, one or two sentences).
- H2: the time to the first spoken word (TTFS: tool round plus the first token of the final answer) is under about 3 s at the median for a control phrase.
- H3: for live-information questions the model uses a tool or says it cannot know, and never invents.
- H4: failover inside the gateway (including a try of the unreliable `z800` backend) does not add visible latency.

Outcome (details in the chat measurements section): H1 borderline (93 % overall, 94 % on action phrases, against 95 %), H2 borderline (TTFS p50 3.06 s against 3 s), H3 holds, H4 not testable (no failover or `z800` try happened in the run).

## Prerequisites and safe baseline

- Read-only cluster access (`kubectl --context=grigri -n isidoro get ...`) and the Grafana MCP `grafana-grigri` (datasource `prometheus`, 45 days of Kiais series).
- Public listing works without a key: `GET https://isidoro.grigri.cloud/v1/models` returns `balanced`, `fast`, `advanced` (observed).
- `POST /v1/chat/completions` without a key returns `401 invalid_api_key` (observed, one request).
- The key used for the runs is the one already present in the owner's local client configuration for the gateway on the workstation; it was read by a wrapper into one environment variable for the duration of one command. No key was taken from cluster secrets or volumes.

## Commands / steps (copyable and bounded)

### A. Offline checks (done)

```sh
python3 -m unittest discover -s scripts/voice-assistant/agent_eval -p 'test_agent_eval.py'   # 33 tests, local scripted server, no network
python3 scripts/voice-assistant/agent_eval/run.py plan                             # 120 jobs, about 237 requests per full run
```

### B. Kiais chat runs (done 2026-11-05 from the workstation over the public URL)

```sh
export AGENT_EVAL_API_KEY=...        # loaded for the command only; never commit, print or paste it
python3 scripts/voice-assistant/agent_eval/run.py run --base-url https://isidoro.grigri.cloud/v1 --model fast --label kiais-fast
python3 scripts/voice-assistant/agent_eval/run.py run --base-url https://isidoro.grigri.cloud/v1 --model balanced --label kiais-balanced \
    --categories general,live,followup
python3 scripts/voice-assistant/agent_eval/run.py run --base-url https://isidoro.grigri.cloud/v1 --model balanced --label kiais-balanced \
    --categories control --ids c01,c05,c10,c15,c18,c19,c27,c35,c38,c39,c41,c44 --repeat-control 1
python3 scripts/voice-assistant/agent_eval/run.py summarize ~/webos-voice-satellite/devices/private/kivi/kiais-fast/*
```

Requests sent: 11 in two smoke tests (the first one exposed the tool-name streaming issue described below), 221 for `fast` and 77 for `balanced`: 309 in total, plus one unauthenticated request. The harness counts requests in `request_ledger.jsonl` next to the results and refuses to exceed `--total-budget` (default 600) or `--max-requests` per run. After a grading rule or the fake resolver changes, `run.py regrade DIR` re-executes the stored calls and re-grades offline, with no requests (used here after a resolver fix, see below).

### C. Built-in intents inference (done, offline)

```sh
python3 -m venv /tmp/v && /tmp/v/bin/pip install hassil==3.12.1 home-assistant-intents==2026.10.6 pyyaml
/tmp/v/bin/python scripts/voice-assistant/agent_eval/builtin_intents.py
```

## Method

**Harness** (`scripts/voice-assistant/agent_eval/`, Python, standard library plus PyYAML):

- Any OpenAI-compatible `/v1/chat/completions` endpoint, streaming. For a local model use Ollama's OpenAI-compatible mode (`--base-url http://HOST:11434/v1`, `--warmup` to load the model first, `--extra-body '{"options": {...}}'` for Ollama options). No Kiais-specific code. The API key comes only from an environment variable and is never written to results.
- Per request: TTFT (first content or tool-call token), time to any first token (reasoning counts), total, finish reason, `model` field and response headers that look like a backend or request id (kept to find out which backend served, when the gateway exposes it).
- Per phrase: the user text, the model's tool calls, a fake result fed back (dry run: nothing is executed), up to four rounds, then the final spoken text. **TTFS** (time to first speech) is the time of all tool rounds plus the TTFT of the final answer; that is what a user waits through.
- Grading (`scripts/voice-assistant/agent_eval/grade.py`) is automatic and deterministic: the fake Home Assistant resolves each call's name, area, floor and domain slots to entity ids, and the expectation names the tool and the entity ids (exact, include, within), value slots (`brightness`, `position`, `temperature`, `volume_level`, derived `volume_dir` and `total_seconds`) and answer regexes. Alternatives (`any_of`) cover legitimate variants (`HassTurnOff` or `HassSetPosition 0` to lower a blind; a clarifying question for an ambiguous request). Extra state-changing calls fail a phrase. "First-call ok" counts only the first round; "pass" allows a retry after a tool error (as Home Assistant's model loop does). Spoken-format checks (no markdown, no emoji, Spanish, "Kivi" not "Kiwi", length) are reported separately.
- Phrases (`scripts/voice-assistant/agent_eval/phrases_es.yaml`): 44 home control (action 31, state questions 4, ambiguous 3, colloquial 5, unknown target 1, plus lights, blinds, awning, air conditioning, TV volume/mute/pause on "la tele", timers, fan, area words salón/cocina/dormitorio/baño/terraza), 15 general, 11 live-information (weather now, rain tomorrow, time, date, calendar today and week, bitcoin, football result, traffic, news, weekend forecast) and 6 two-turn follow-ups. 76 phrases, 120 jobs with control repeated twice.
- Fake home (`scripts/voice-assistant/agent_eval/inventory_es.yaml`): written for this test, with duplicate names across areas (a "Luz del techo" in three rooms) so ambiguity is real; the satellite sits in "Salón". No real names or identifiers.
- Tool schemas and system prompt (`scripts/voice-assistant/agent_eval/ha_fake.py`): 26 tools modelled on Home Assistant 2026.10.4. **Documented** (read from the `2026.10.4` tag): in 2026.10.4 the Assist LLM API is assembled from per-integration `llm.py` platforms, and tools are namespaced `<domain>__<Name>`: `intent__HassTurnOn`, `intent__HassTurnOff`, `intent__HassSetPosition`, `intent__HassStopMoving`, `intent__HassCancelAllTimers`, `intent__HassStartTimer` and the other timer tools (only offered to a device that supports timers), `light__HassLightSet` (`brightness` 0 to 100, `color`, `temperature`), `climate__HassClimateSetTemperature`, `media_player__HassMediaPause`/`Unpause`/`Next`/`Previous`/`PlayerMute`/`PlayerUnmute`/`SetVolume` (`volume_level` 0 to 100)/`SetVolumeRelative` (`volume_step` `up`, `down` or -100 to 100), `fan__HassFanSetSpeed`, `calendar__get_events` (`calendar`, `range` `today` or `week`), `homeassistant__GetLiveContext` (`name`, `domain`, `area`) and `llm__GetDateTime`. There is **no weather tool and no tool for the current climate temperature**: those go through `GetLiveContext`. The Home Assistant prompt fragments (device-control instruction, area sentence, dynamic-context text, static context list) are copied from the source; the Spanish persona is ours. **Unconfirmed:** the exact device-class enums for the on/off tools (written from memory), the argument-name mapping in `IntentTool` beyond what the source shows, and the shape of real tool results (modelled on the source: `action_done` with `success` targets, errors as `{"error": type, "error_text": ...}` as in `chat_log.py`).
- Resolver approximation: `strip().casefold()` name matching without accent folding, area/floor/domain/device-class filtering, the satellite area preferred for duplicate names, required states for pause/next. It is not Home Assistant's matcher.

**Metrics:** Prometheus on the homelab, metric family `kiais_*`: `kiais_requests_total{model,backend,status,stream}`, `kiais_backend_attempts_total{...,outcome}`, `kiais_failover_events_total{...,reason}`, `kiais_requests_with_failover_total`, `kiais_candidate_skips_total{...,reason}`, `kiais_ttft_seconds_bucket`, `kiais_request_duration_seconds_bucket`, `kiais_dispatch_attempts`, `kiais_circuit_breaker_state`. Windows of 30 days (ending 2026-11-05) and 7 days; percentiles are `histogram_quantile` over bucket increases, so they are interpolated and coarse. In these series `model` is the tier (`fast`, `balanced`, `advanced`) and `backend` is the upstream; the model behind a backend comes from the Model custom resources.

## Expected evidence and failure interpretation

- If the key is issued, H1 fails for a tier when the tool-call pass rate on action phrases is below 95 %; the per-phrase JSON shows whether the cause is wrong arguments (a name instead of area plus domain), extra calls, or no call.
- H2 fails when the TTFS p50 for control phrases is above about 3 s; the split between the tool round and the final answer shows where the time goes.
- H3 fails on any invented number or fact in the live-information phrases (`must_not_regex` and the limitation regex).
- Do not treat the fake-home pass rate as the owner's real home pass rate: names, areas and entity counts differ.

## Observations and redacted evidence links

### Tier definitions (documented, Kiais custom resources, read 2026-11-05)

| Tier | Backends in order (model on upstream) |
| --- | --- |
| `fast` | `kimi-k2.5` on alibaba-coding-plan; `mimo-v2.6-flash-free` on opencode; `qwen3.6-35b-a3b` on `z800` (the owner's PC, excluded from conclusions: the owner says it is not reliable); `qwen3.8-flash-next` on millaguie (llm.millaguie.net); `qwen3.8-flash` on alibaba-token-plan |
| `balanced` | `qwen3.6-35b-a3b` on `z800`; `qwen3.7-plus` on alibaba-coding-plan; `deepseek-v4.1-flash-free` on opencode; `qwen3.8-flash-next` on millaguie; `qwen3.8-flash` on alibaba-token-plan |
| `advanced` | `deepseek-v4.1-flash` on millaguie-advanced; `qwen3.8-max` on alibaba-token-plan; `gpt-6.1-sol` on chatgpt-oauth |

Routing policy `default` (documented): 3 retries with a 0.5 s base and 10 s cap, circuit breaker opening after 5 failures for 30 s, quota demotion at 80 %, cache-aware routing and session affinity. Gateway environment (documented): upstream connect timeout 5 s, stream idle timeout 300 s, slow-first-content degradation threshold 30 s. The Upstream list currently shows `z800` as `Unreachable` with the circuit `Open` and not eligible (observed; `kiais_circuit_breaker_state` is 2 on both gateway pods).

### Traffic and which backend served (observed, 30 days to 2026-11-05)

| Tier | Served (HTTP 200) per 30 d | Per day | Served last 7 d | Backends that served (30 d) |
| --- | ---: | ---: | ---: | --- |
| `fast` | 2 347 | about 78 | 97 | alibaba-token-plan 1 708 (73 %), alibaba-coding-plan 521 (22 %), millaguie 105, opencode 13 |
| `balanced` | 133 303 | about 4 440 | 31 659 | alibaba-coding-plan 69 125 (52 %), alibaba-token-plan 58 801 (44 %), millaguie 3 088, z800 1 605, opencode 630 |
| `advanced` | 13 384 | about 450 | 5 286 | alibaba-token-plan 6 887, chatgpt-oauth 5 447, millaguie-advanced 1 050 |

In the last 7 days **all 97 `fast` requests were served by alibaba-coding-plan (`kimi-k2.5`)**: the serving backend shifts over time (cache-aware routing and quota demotion), so a single tier name does not mean a single model. The `fast` tier carries very little traffic, in bursts (for example 279 requests on one day, 0 to 10 on most), so its caches and connections are often cold. The traffic on `balanced` is dominated by large coding-agent requests: the latency figures below describe that workload, not short voice turns.

### Errors and failover (observed, 30 days)

| Tier | 5xx without a backend | Requests with at least one failover | Failover share of served | Backend attempt outcomes other than success |
| --- | --- | ---: | ---: | --- |
| `fast` | 502: 39, 503: 744 (about 24 % of all `fast` requests, **783 of 783 within 2026-10-14 and 2026-10-15; none from 2026-10-16 to 2026-11-05**) | 206 | 8.8 % | alibaba-coding-plan client_error 76; opencode client_error 34; z800 network_error 14 |
| `balanced` | 502: 576, 503: 4 765 (3.4 %), 504: 1 | 18 164 | 13.6 % | z800 network_error 2 241 and timeout 76; opencode client_error 782; rate_limited several hundred |
| `advanced` | 503: 237 (1.7 %) | 1 956 | 14.6 % | chatgpt-oauth rate_limited 801 |

Correction found during the chat run: `kiais_requests_with_failover_total` also counts requests where a candidate was only **skipped** (for example `z800` through its open circuit). During the `fast` run of 2026-11-05 about 50 of 235 requests were counted "with failover" while the only attempt outcome was a first-try success on one backend and the only event was `z800` `circuit_open` skipping. So the failover shares in the table above overstate real retries; the real failover events (below) are about 49 in 30 days for `fast`.

Failover reasons for `fast`: alibaba-coding-plan `client_error` 31 (rejected requests), opencode `client_error` 4, `z800` `network` 14; skip reasons: `cooldown` 845 (coding-plan), 777 (token-plan), 862 (opencode), `circuit_open` 986 (`z800`) and 55 (opencode), `slow_first_content_degraded` 7 (token-plan). So `z800` is, for `fast`, almost always skipped through its open circuit rather than tried first: 14 network failures in 30 days against 986 skips. **The cost of one try of `z800`: not measured.** The gateway configuration gives a ceiling of the 5 s connect timeout plus the retry delay of at least 0.5 s for a refused or unreachable host; the metrics have no per-attempt duration for failed attempts. Hypothesis: a try while the circuit is closed adds up to about 5.5 s once, then the circuit opens after 5 failures and later requests skip it.

### Latency of the gateway (observed, 30 days; 7 days in brackets)

| Tier | TTFT p50 / p95 (s) | Total p50 / p95 (s) |
| --- | --- | --- |
| `fast` | 1.89 / 4.52 (2.93 / 5.06) | 6.25 / 28.9 (4.17 / 11.5) |
| `balanced` | 2.54 / 8.06 (2.38 / 5.91) | 7.60 / 35.5 (7.35 / 31.4) |
| `advanced` | 4.57 / 16.2 (4.55 / 15.98) | 12.9 / 89.7 (11.3 / 76.5) |

Per backend in `fast`, 30 days:

| Backend (model) | Served | TTFT p50 / p95 (s) | Total p50 / p95 (s) |
| --- | ---: | --- | --- |
| alibaba-coding-plan (`kimi-k2.5`) | 521 | 2.15 / 4.70 | 3.44 / 13.5 |
| alibaba-token-plan (`qwen3.8-flash`) | 1 708 | 1.87 / 4.46 | 7.40 / 31.5 |
| millaguie (`qwen3.8-flash-next`) | 105 | 0.99 / 1.45 | 6.17 / 29.6 |
| opencode (`mimo-v2.6-flash-free`) | 13 | 2.92 / 9.31 | 9.38 / 39.4 |

The backlog's earlier figure ("TTFT p50 about 1.5 s on only 2 requests") is superseded by this: on about 2 350 requests the `fast` TTFT p50 is 1.9 s over 30 days and 2.9 s over the last 7. Caveats: the series mix client workloads and prompt sizes; it is not known whether the gateway's TTFT counts reasoning tokens (a thinking model can show a long TTFT before any spoken text); totals depend on output length, which is short for voice.

**Implication for H2 (inferred, before the chat run):** a control phrase through an LLM costs two round trips (tool call, then the answer), so with a gateway TTFT of about 2 to 3 s the time to first speech would be roughly 4 to 6 s, before TTS and the satellite. Home Assistant can start TTS while the answer streams, but the tool round cannot be streamed away. The chat run below measured it: TTFS p50 is 3.06 s on `fast`, in line with this estimate at the low end. This is a latency argument for keeping simple control off the LLM (below).

### Which phrases Home Assistant's built-in intents would handle (inferred, `prefer_local_intents`)

Documented (source `2026.10.4`, `assist_pipeline` and `conversation/default_agent.py`): with `prefer_local_intents` on, the pipeline first tries a **strict** match of the text against the Spanish sentences using exposed entity names, areas and floors, with the satellite's area as context. When the pipeline's agent is an LLM with control, matches of `HassGetState` and `HassMediaSearchAndPlay` are **ignored** so they do not interfere with the LLM; a "no match" kind of handler error (unknown name or area, nothing in the required state) is ignored too and the text goes to the agent; only successes (and failed-to-handle errors) answer locally. Home Assistant 2026.10.4 pins `hassil==3.12.1` and `home-assistant-intents==2026.10.6`; the analysis used exactly those versions with the same `recognize_best` call and slot lists (names with domain and device-class context, areas, floors, satellite area), and mapped each recognised intent to the fake tool resolver (`scripts/voice-assistant/agent_eval/builtin_intents.py`).

| Result for the 44 control phrases | Count | Phrases |
| --- | ---: | --- |
| handled locally with the expected targets | 29 | c01-c04, c06-c12, c17-c25, c27-c34, c42; for example "Enciende la luz del salón", "Sube el brillo de la lámpara de pie al 80", "Baja la persiana del salón", "Pon el volumen de la tele al 30", "Silencia la tele", "Pausa la tele", "Pon un temporizador de una hora y media", "Enciende la cafetera", "Baja el volumen" |
| handled locally, no LLM-tool twin | 1 | c35 "¿Qué temperatura hace en el salón?" (`HassClimateGetTemperature`, answers from the climate entity) |
| recognised but ignored (goes to the LLM) | 5 | c05, c13, c15, c16, c26: "Pon la luz del salón al cuarenta por ciento", "Pon la persiana del salón a la mitad", "Pon el aire del salón a 22 grados" and similar "pon ..." forms match the media-search sentence first |
| no match (goes to the LLM) | 9 | c14 "Para el toldo", c36 and c37 (state questions), c38-c41 (colloquial: "Está muy oscuro aquí", "Quita la tele", "Ponme la tele más bajita", "Apágalo todo en la cocina"), c43, c44 (a room that does not exist) |

Also locally: "¿Qué hora es?" and "¿Qué día es hoy?" (`HassGetCurrentTime`, `HassGetCurrentDate`); nothing for the weather, the calendar, general questions or the open-world live questions. About two thirds of the control phrases (30 of 44) would never reach the agent; the ones left are the colloquial, ambiguous, numeric "pon ..." and state phrases, which is where the agent matters.

**Limits of this inference:** the names in the fake home were chosen to be natural Spanish names; the owner's real entity names, aliases and exposure decide the real hit rate. Strict matching needs exact wording; no fuzzy matching. The "ignored" group depends on the LLM agent having the control feature (true for an LLM with the Assist API). The result must be rechecked with the owner's real names before it becomes a decision.

### Chat measurements (observed, 2026-11-05, workstation to `https://isidoro.grigri.cloud/v1`, streaming, temperature 0.2)

`fast` (model name `fast`; control phrases twice, every other category once; 126 graded turns, 221 requests). Grading is automatic; "review" phrases (a joke, a free explanation, "hace mucho calor") are excluded from the percentages.

| Group | Turns | Pass | Pass % (without review) | First-call ok % | TTFS p50 / p95 (s) | Total p50 / p95 (s) |
| --- | ---: | ---: | ---: | ---: | --- | --- |
| control, all | 88 | 80 | 93 | 92 | 3.06 / 3.69 | 3.47 / 4.36 |
| control, action | 62 | 58 | 94 | 94 | 3.06 / 3.69 | 3.47 / 4.23 |
| control, ambiguous (clarify or sensible default) | 6 | 6 | 100 | 100 | 3.06 / 3.72 | 3.42 / 4.21 |
| control, colloquial | 10 | 6 | 75 (2 review) | 75 | 2.65 / 3.51 | 3.34 / 4.77 |
| control, state questions | 8 | 8 | 100 | 100 | 3.18 / 3.58 | 3.51 / 4.36 |
| control, missing target ("luz del garaje") | 2 | 2 | 100 | n/a | 1.39 / 1.47 | 2.65 / 2.67 |
| follow-ups (two turns each) | 12 | 12 | 100 | 100 | 2.59 / 4.24 | 2.99 / 4.60 |
| general questions | 15 | 13 | 100 (2 review) | n/a | 1.20 / 1.65 | 1.71 / 4.22 |
| live information | 11 | 11 | 100 | n/a | 0.87 / 2.95 | 1.78 / 4.57 |
| all | 126 | 116 | 95 | 93 | 2.78 / 3.67 | 3.23 / 4.36 |

- Request TTFT (all requests): p50 1.30 s, p95 1.77 s. In a control phrase the tool-call round has a median TTFT of 1.5 s and the final-answer round 1.25 s (82 two-round phrases), so the time to first speech is about 3 s: **H2 is borderline (3.06 s against 3 s), not met with margin.** This includes the network from the workstation to the public URL.
- Failures of `fast` were the same in both repetitions (no phrase differed between the two runs): "Para el toldo" and "Cancela el temporizador" (it asks what to do or which timer instead of acting, 4 of the 4 failed action turns) and "Quita la tele" (it pauses instead of turning off; colloquial). Everything else passed, including the arguments (brightness, position, temperature, volume, timer seconds), the area-plus-domain use for areas and the satellite-area preference for the three "Luz del techo".
- Live information: it called `GetLiveContext` for the weather now, `calendar__get_events` with `today` or `week`, answered the date and time from the prompt, and for the bitcoin price, a football result, traffic, news, tomorrow's rain and the weekend forecast it said it cannot check them ("Solo tengo acceso al tiempo actual"). No invented figures: **H3 holds** on these 11 phrases.
- Spoken format: 100 % of the answers were plain Spanish without markdown or emoji, and the name was never written "Kiwi". Answers were one or two short sentences.
- **H1 (at least 95 %): 93 % overall and 94 % on action phrases, just under the threshold**, and the misses are underspecified requests where asking back is defensible. On the clearly specified phrases it is 100 %.
- Which backend served: the response gives only `model: fast` and no backend header. The gateway metrics for the run window show **all 235 `fast` requests served by alibaba-coding-plan (`kimi-k2.5`), all first-try 200, zero real failovers**; `z800` appeared only as 50 skips through its open circuit. So H4 could not be tested (no retry or `z800` try happened) and the run carries no cost of a `z800` failover.

`balanced` (lean run: 32 general, live and follow-up turns plus 12 control phrases once, chosen to include the hard ones; 77 requests; the control phrases overlap with `fast`'s):

| Group | Turns | Pass % (without review) | TTFS p50 / p95 (s) | Total p50 / p95 (s) |
| --- | ---: | ---: | --- | --- |
| control (12 chosen phrases) | 12 | 92 | 6.64 / 35.85 | 6.93 / 36.39 |
| follow-ups | 12 | 83 | 5.92 / 12.79 | 6.24 / 12.91 |
| general | 15 | 100 (2 review) | 3.37 / 9.13 | 4.02 / 9.27 |
| live | 11 | 100 | 8.36 / 39.01 | 9.34 / 39.30 |
| all | 50 | 94 | 6.37 / 17.63 | 6.59 / 17.85 |

`balanced` is about twice as slow as `fast` (TTFT p50 3.0 s, p95 15.7 s; one control answer took 36 s and one live answer 39 s) and not more accurate on the same phrases: on the 12 shared control phrases `fast` passed 11 and `balanced` 11. Its follow-up misses were clarifying questions where `fast` just acted ("which light?" for the two lights of the bedroom). The tier is shared with the coding workload, so its serving backend cannot be attributed to this run. `advanced` was not run: the 30-day gateway TTFT p50 of 4.6 s and p95 of 16 s already rule it out for voice.

Method notes on the grading, for honesty: after the first pass the resolver was corrected so that a whole-area "turn off" no longer includes sensors, case differences in answer checks were ignored ("Veintinueve" against "veintinueve"), and acting device by device inside an allowed set is no longer an "extra action"; the stored calls were re-graded offline (`regrade`). Two of those corrections turned `fast` failures (state question with "22 grados y 4 décimas", "apágalo todo en la cocina") into passes; the model's outputs were not touched. The first attempt on the gateway also exposed that it repeats the whole tool name in every stream chunk (some backends do); the client was fixed to accept that and covered by a test.

## Privacy note

- **This run:** about 300 requests with synthetic Spanish phrases, the fake home and the system prompt were sent to the gateway; for `fast` the metrics show they were all served by alibaba-coding-plan (Alibaba, `kimi-k2.5`); the `balanced` requests could have gone to any of its backends. No real user data was involved. The harness sends only the synthetic phrases in `phrases_es.yaml`, a fake home, and the system prompt; results (which contain the model's answers and fake tool results) go to a git-ignored directory outside this repository (`~/webos-voice-satellite/devices/private/kivi/`). Never commit them.
- **Which backends would see the text** when a tier is used (documented from the custom resources; the data-handling terms of each provider were **not** verified): `fast` can go to Alibaba (two plans: `kimi-k2.5` and `qwen3.8-flash`), opencode (a free tier, where free usually means the text may be retained or used for training; unverified), millaguie (a third-party host), and `z800` (the owner's PC); `balanced` adds the same; `advanced` can go to millaguie-advanced, Alibaba and OpenAI via ChatGPT OAuth. The routing is dynamic, so the owner cannot choose which one answers a given request.
- **For real use** every utterance, and the static home context in the prompt (room, device and alias names, plus live state when a tool is called), would leave the house to those providers. That is the main cost of option C in the backlog and the reason a local agent or `prefer_local_intents` for most control phrases is attractive.

## Conclusion and confidence

- **`fast` is good enough in accuracy and the right tier, but its latency is at the limit.** On 126 graded turns: 93 % pass on control (94 % on action phrases, 100 % on state questions, follow-ups and live information), 100 % spoken-format compliance, no invented data. Misses are underspecified or colloquial phrases ("Para el toldo", "Cancela el temporizador", "Quita la tele"), and they repeat deterministically. Confidence: medium-high for this fake home and phrase set (88 control turns, 2 repetitions identical); lower for the owner's real names and speech, which the synthetic text does not cover.
- **Latency:** time to first speech p50 3.06 s and p95 3.7 s for a control phrase through an LLM (two round trips, about 1.5 s TTFT each), measured over the public URL from the workstation, with all requests served by one backend (`kimi-k2.5` on alibaba-coding-plan) and no failover. Gateway metrics over 30 days (other workloads) agree: TTFT p50 1.9 s, p95 4.5 s. This is acceptable for conversation, slow for "turn on the light".
- **`balanced`: not worth it.** About twice as slow (TTFS p50 6.4 s, p95 17.6 s, outliers of 36 and 39 s) with no accuracy gain on the shared phrases. **`advanced`** (not run) is out on the gateway's own latency (TTFT p50 4.6 s, p95 16 s).
- **Failover:** none occurred; `z800` is skipped through its open circuit at no cost. Its cost when tried stays unmeasured (hypothesis: up to about 5.5 s once per circuit cycle).
- **Recommendation (confidence medium):** use Kiais **`fast`** as the Kivi fallback agent, together with `prefer_local_intents = true`, so that about two thirds of the control phrases (30 of 44 on the fake home, inferred) are answered locally in well under a second and only the colloquial, state, numeric "pon ..." and general requests pay the roughly 3 s of the LLM. Keep `balanced` and `advanced` out of the voice path. The privacy position still has to be accepted by the owner: every utterance and the home's device names go to Alibaba and possibly other providers behind the tier, whose terms were not checked. The local models study should be compared against these numbers (same harness, same prompt and tools): pass 93 %, TTFS p50 3.06 s.

## Rollback and cleanup

Nothing outside this repository was changed: no Home Assistant, cluster, TV or audio action. To remove the work: delete `scripts/voice-assistant/agent_eval/` and this section of the docs. Delete the private results directory (`~/webos-voice-satellite/devices/private/kivi/`) when no longer needed. The Kiais key was used from the owner's existing client configuration; nothing to revoke.

## Follow-ups / unresolved questions

1. Owner: accept or reject sending utterances and the home's device names to the providers behind `fast` (Alibaba and others); read their data terms if option C is chosen.
2. Whether Home Assistant can reach Kiais at all: the core `openai_conversation` integration has no base-URL option (backlog #12 of the satellite repository), so a custom or HACS integration may be needed; verify tool calling and streaming through it, since this harness talks to the gateway directly.
3. Re-run with the owner's real entity names, aliases and areas (read-only export, not committed) for both the LLM pass rate and the built-in-intents hit rate (section C).
4. Cost of one `z800` try (measure with a controlled request while `z800` is up and while it is down; needs the owner's agreement because it targets his PC).
5. Compare with the local models by running the same harness against Ollama's `/v1`.
6. Whether the gateway's TTFT includes reasoning tokens, and whether a thinking model needs `enable_thinking: false` for voice (the `fast` Model resource has a compatibility variant for Alibaba that does this for `none`).
7. Confirm the tool names, argument shapes and result shapes against a live Home Assistant (the harness is modelled on the 2026.10.4 source only), and the end-to-end latency through the satellite, which adds STT, TTS and the network on the HA side.
8. Re-measure `fast` at a different time of day; this run is one afternoon with all requests on one backend.
