# Kivi speech-to-text: a home vocabulary hint list and learning from usage

**Status:** design (no live change). Nothing in Home Assistant, the cluster or GitOps state was changed.
**Date:** 2026-11-05
**Scope:** the Spanish STT step of the Assist pipeline "Kiwi" (assistant "Kivi"): Wyoming
faster-whisper in namespace `home-assistant`, Home Assistant 2026.10.4, pipeline language `es`.
**Related:** [conversation agent evaluation](experiments/conversation-agent-eval.md) (built-in
intents coverage, `prefer_local_intents`); [small local models study, section 5](experiments/small-local-models.md#5-whisper-prompt-hotwords-beam-and-model-size)
(prompt and hotwords measurements, summarised in [section 1.6](#16-measured-effect-on-this-setup));
[Kiais integration](kiais-integration.md) (current conversation agent).

Evidence labels: **documented** (read in source code, a changelog or a manifest, with the version
named), **observed** (read from the cluster, Loki or the repository on the date above),
**inferred** (derived from documented material, not run), **hypothesis** (to be tested).

## Summary

1. **Upstream already does phase 1.** `wyoming-faster-whisper` 3.6.0 (2026-08) added
   `--hass-token`: the server reads the conversation-exposed entity names, their aliases, and the
   area and floor names from Home Assistant over the websocket API, ranks them in four tiers and
   passes them to Whisper as `initial_prompt` within a 200-token budget, refreshed in the
   background at the start of each utterance. 3.7.0 fixed alias ranking and added environment and
   `_FILE` configuration. The deployed image is 2.4.0 (2024-12), which has only a static
   `--initial-prompt`. The cheapest phase 1 is to rebuild the owner's image on 3.8.1 and set
   `--hass-token` plus a fixed, generic Spanish command-verb `--initial-prompt` prefix. No home
   names need to be committed, because they are fetched in the cluster.
2. **Measured: names plus command verbs help, names alone barely do.** On 145 clips per condition
   with TV sound mixed in, built-in intent success went from 82/71/65/46 % (clean, +10, +5, 0 dB)
   without a prompt to 85/79/75/57 % with area and device names plus a generic verb list
   (48 clips fixed against 7 broken over the noisy conditions, p ≤ 0.007 each). Names alone gave
   81/74/70/52 %. `hotwords` gave byte-identical output to `initial_prompt`. "Kivi" in the prompt
   leaked into 3 of 1400 transcripts, so the wake name stays out ([section 1.6](#16-measured-effect-on-this-setup)).
3. **`hotwords` is not a separate mechanism.** In faster-whisper it is the same "previous text"
   slot as `initial_prompt` (no logit bias). For commands of a single 30 s window the two behave
   alike (confirmed by measurement), so `initial_prompt` is enough.
4. **Home Assistant sends nothing usable per request.** The Wyoming integration in 2026.10.4 sends
   `Transcribe(language=...)` only. The protocol has `transcript_names` and `transcript_terms`
   since Wyoming 1.10.0, but neither Home Assistant 2026.10.4 nor the server uses them.
5. **Transcripts are already stored.** The deployed server logs every transcript at INFO level and
   Vector ships it to Loki, which keeps 180 days. Any learning loop should start by deciding this.
6. **Learning loop:** capture each run from Home Assistant's pipeline debug API (STT text, intent
   result, `processed_locally`, timings) into a small in-cluster store with 30-day retention for
   text, derive misrecognition signals, and turn them each week into ranked proposals (hint words,
   entity aliases, a text correction map) that the owner approves. Measure with intent success
   rate and repeat rate before and after.

## 1. Mechanics

### 1.1 What is deployed (observed)

- `apps/home-assistant/values.yaml`, controller `whisper`: image `pando85/wyoming-whisper:2.4.0`
  (built from `pando85/wyoming-addons`, `whisper/Dockerfile`: CTranslate2 4.5.0 on Ubuntu 20.04
  with CUDA 12.2, `wyoming-faster-whisper` v2.4.0), args `--model large-v3-turbo --language es
  --device cuda --compute-type int8`, runtime class `nvidia`, on `prusik`. No `--initial-prompt`,
  no `--beam-size` (2.4.0 default `0` = auto). Service `home-assistant-whisper:10300`.
- `wyoming-faster-whisper` 2.4.0 pins `faster-whisper==1.1.0` and `wyoming==1.5.3`
  (documented, `requirements.txt` at tag `v2.4.0`).
- The handler logs each transcript with `_LOGGER.info(text)` (documented, `handler.py` at
  `v2.4.0`; also `dispatch_handler.py` in 3.8.1). Observed: 157 `INFO:wyoming_faster_whisper.handler`
  lines in the last 48 h of pod logs, and 707 Loki entries for `{namespace="home-assistant",
  container="whisper"}` in the last 7 days (counts only, no text read). Loki
  `retention_period: 180d` (`system/loki/values.yaml`).
- Measured by the owner: STT p50 0.86 s for about 3 s of audio (given, not re-measured here).

### 1.2 What the server supports

| Feature | 2.4.0 (deployed) | 3.8.1 (latest tag, 2026-09-14) | Label |
| --- | --- | --- | --- |
| `--initial-prompt` (static, startup only) | yes | yes, also `WYO_WHISPER_INITIAL_PROMPT[_FILE]` | documented |
| Home Assistant names as prompt (`--hass-token`, `--hass-api`, `--hass-refresh-seconds`, `--hass-prompt-max-tokens` default 200, `--hass-prompt-timeout` default 1.0 s) | no | yes (3.6.0, alias tiering 3.7.0) | documented |
| `hotwords` | no | no (not passed to faster-whisper) | documented |
| Per-request prompt from the `transcribe` event | no | no (only `language` is read) | documented |
| Segment confidence (`avg_logprob`, `no_speech_prob`) returned or logged | no | no (segments joined to text, `info` discarded) | documented |
| Server-side VAD (`--vad-filter`, `--vad-clip`, `--vad-endpointing`) | no | yes | documented |
| Text post-processing / correction map | no | no | documented |

Source: `rhasspy/wyoming-faster-whisper` (now `OHF-Voice`), `CHANGELOG.md`, `__main__.py`,
`dispatch_handler.py`, `faster_whisper_handler.py`, `hass_api.py`, `name_cache.py`,
`vocabulary.py` at `v3.8.1`/`HEAD`. `CHANGELOG.md` at `HEAD` lists an unreleased 3.9.0 (language
reporting only).

How the 3.6+ name biasing works (documented, `hass_api.py`, `vocabulary.py`, `name_cache.py`):

- Websocket calls: `homeassistant/expose_entity/list` (keeps entities exposed to `conversation`),
  `get_states` (friendly names), `config/entity_registry/get_entries` (aliases, area, device),
  `config/device_registry/list` (only when an entity's area comes from its device),
  `config/area_registry/list`, `config/floor_registry/list`. Nothing is written.
- Tiers, filled in order until the budget is used: (1) areas and floors that contain an exposed
  entity, (2) entity names in `light`, `switch`, `fan`, `media_player`, `climate`, `scene`, `todo`,
  (3) other areas and floors, (4) other entity names. Each alias sits behind its entity. Names are
  de-duplicated case-insensitively and joined with `, `, ending in `.`. The budget is counted with
  the model's own tokenizer. `--initial-prompt` is always kept at the front.
- Refresh starts at `AudioStart` in the background; at `AudioStop` the server waits at most
  `--hass-prompt-timeout` for an unfinished refresh, then uses the previous names. A failed fetch
  never fails a transcript. Default `--hass-refresh-seconds 0` refreshes on every utterance.
- `--debug` logs the exact prompt and how many names were dropped. Note that `--debug` also logs
  each transcript (`models.py`), so it should only be enabled briefly.
- **The token must belong to an administrator**: `homeassistant/expose_entity/list` is decorated
  `@websocket_api.require_admin` in Home Assistant 2026.10.4 (documented,
  `homeassistant/components/homeassistant/exposed_entities.py`). Home Assistant tokens have no
  scopes, so this token can do anything an admin can.

### 1.3 What Home Assistant sends per request (documented, 2026.10.4)

- `homeassistant/components/wyoming/stt.py` writes `Transcribe(language=metadata.language)` and
  then the audio. Manifest pins `wyoming==1.10.2`.
- Wyoming 1.10.0 added `vad_sensitivity`, `transcript_names` and `transcript_terms` to `transcribe`
  (documented, `rhasspy/wyoming` `CHANGELOG.md`, `wyoming/asr.py`). Home Assistant 2026.10.4 does
  not fill them, and the 3.8.1 server does not read them. So a per-request hint list from Home
  Assistant is not available today; it must be configured on the server.
- The `Transcript` event can carry a `context` dict, but the Home Assistant integration reads only
  `text` (documented). Confidence cannot travel back to Home Assistant this way.

### 1.4 `initial_prompt` versus `hotwords` in faster-whisper (documented, faster-whisper 1.1.0)

- `initial_prompt` is tokenised and placed in the decoder's previous-text slot
  (`<|startofprev|>`) for the first window; with `condition_on_previous_text` later windows see
  the previous output instead. It is reset when the decoding temperature rises above
  `prompt_reset_on_temperature` (0.5).
- `hotwords` (added in faster-whisper 1.0.2) goes into the **same** previous-text slot, before any
  previous tokens, for every window, and is ignored when `prefix` is set. It is not a keyword
  boost or logit bias. For voice commands, which fit in one 30 s window, the two are equivalent;
  using both would only duplicate tokens.
- Length: the previous-text part is cut to `max_length // 2 - 1` = 223 tokens (Whisper's decoder
  context is 448). Anything longer is silently truncated from the front.

Known pitfalls:

- **Echo and hallucination** (documented in the upstream `vocabulary.py` comment and changelog;
  widely reported for Whisper prompting): long prompts make the model output prompt words that
  were not spoken, especially on silence or non-speech. In this home the risky audio is not
  silence (Home Assistant's VAD ends the stream) but TV sound and room noise captured after the
  wake word. Hypothesis: a prompt that is a list of nouns is low risk, because an echoed noun list
  does not match an intent; a prompt with full imperative sentences ("enciende la luz del
  salón") could produce an actionable hallucination. Keep the prefix free of complete commands.
- **Style leakage:** the model imitates the prompt's casing, punctuation and spelling. This helps
  (names come back spelled, accented and capitalised as Home Assistant stores them) and can hurt
  (a comma list prompt tends to add commas; an English prompt can pull Spanish output towards
  English). The prefix should be Spanish and use the same punctuation as normal speech.
- **Model dependence:** Distil-Whisper models degrade with prompts (documented, upstream 3.8.0
  changelog: `distil-small.en` breaks from about 52 prompt tokens). `large-v3-turbo` is a pruned,
  fine-tuned model, not a distilled one; in Spanish it benefits from a prompt of about 120
  tokens (observed, 1.6). Whisper `small` and `medium` were significantly worse than turbo
  without a prompt (clean 70 % and 76 % against 82 %), so the model stays.
- **Latency:** prompt tokens add decoder prefill work per beam. Hypothesis: tens of milliseconds
  on the GTX 1060 for about 200 tokens; being measured in section 9 of the
  [small local models study](experiments/small-local-models.md), against the 0.86 s p50.
- **Wake word in the prompt:** observed: with "Kivi" at the start of the prompt, "Kivi" appeared
  in 3 of 1400 transcripts whose audio did not contain it. A leading "Kivi" can break strict
  intent matching, so the wake name stays out of the prompt.

### 1.5 Home Assistant matching that the hint list feeds (documented, 2026.10.4)

- The default agent matches in stages: exposed entities only, then unexposed entities, then
  unknown names (`IntentMatchingStage` in `conversation/default_agent.py`), with `hassil==3.12.1`
  and `home-assistant-intents==2026.10.6`. Names and aliases must match the text.
- 2026.10.4 adds a gazetteer fallback (`conversation/gazetteer.py`, `gazetteer-matcher==1.1.0`)
  that reaches intents through near-miss names and unseen wordings, but `LANGUAGE = "en"`: it is
  **English only** and does not run on the "prefer local intents" fast path. For Spanish there is
  no fuzzy matching, so a single misheard letter or accent in a name means no match.
- Hypothesis (not checked in `hassil` source here): accent and case differences between the
  transcript and the stored name can break a match. The hint list helps here because Whisper
  copies the stored spelling. Test with `scripts/voice-assistant/agent_eval/builtin_intents.py`.

### 1.6 Measured effect on this setup

Source: [small local models study, section 5](experiments/small-local-models.md#5-whisper-prompt-hotwords-beam-and-model-size) (observed by that study, 2026-11-05). Setup:
`faster-whisper` `large-v3-turbo` int8, beam 5, as deployed, run on `z800`; 5 Piper voices × 29
control phrases that built-in intents can handle = 145 clips per condition; TV content mixed in
at +10, +5 and 0 dB SNR. Metric: Home Assistant built-in intent success (`hassil` 3.12.1,
`home-assistant-intents` 2026.10.6). The names are those of the study's test home, not the
owner's.

| Prompt (`initial_prompt`) | Clean | +10 dB | +5 dB | 0 dB |
| --- | --- | --- | --- | --- |
| None | 82 % | 71 % | 65 % | 46 % |
| Area and device names (about 80 tokens) | 81 % | 74 % | 70 % | 52 % |
| Names plus generic Spanish command verbs (about 40 tokens more) | **85 %** | **79 %** | **75 %** | **57 %** |
| Same text as `hotwords` | byte-identical output to `initial_prompt` | | | |

- Names only against no prompt: 31 clips fixed and 9 broken over the noisy conditions; no gain on
  clean audio.
- Names plus verbs against no prompt: 48 fixed and 7 broken over the noisy conditions
  (p ≤ 0.007 in each noisy condition); against names only: 29 fixed and 4 broken.
- The verb list used: `Enciende, apaga, sube, baja, abre, cierra, para, pausa, reanuda, silencia,
  quita el silencio, pon, temporizador, minutos, volumen, brillo, por ciento, grados.` Without it
  the remaining errors were mostly the first verb of short commands (for example "Apaga la tele"
  heard as "Hasta acá la tele", "Reanuda" as "Renuda").
- "Kivi" at the start of the prompt: 3 of 1400 transcripts contained "Kivi" not present in the
  audio.
- STT latency with the prompt: being measured (section 9 of that study); 0.86 s p50 without a
  prompt in real use.

Conclusion (inferred from the above): the command verbs carry most of the gain, and they are
generic, so they belong in the static `--initial-prompt` prefix; the names add a smaller gain on
noisy audio and come from Home Assistant through `--hass-token`.

How 3.8.1 composes the two (documented, `vocabulary.py` `_build_prompt` at `v3.8.1`, unchanged at
`HEAD`): the `--initial-prompt` text comes first (a `.` is added if it does not end in
punctuation), then the Home Assistant names joined by `, ` and ending in `.`. The
`--hass-prompt-max-tokens` budget (default 200) counts the **whole** prompt, prefix included,
using the model's tokenizer, and the prefix is always kept. With the ~40-token verb prefix about
160 tokens are left for names, in tier order. Two differences from the study's prompt are
untested: the order (the study put names before verbs; upstream puts the prefix first) and the
real home's name list, which may be longer than the study's ~80 tokens. Hypothesis: neither
changes the result much; check with the study's harness by swapping the order once.

### 1.7 Making the hint list reloadable without a restart

Three ways, smallest first:

1. **Upstream 3.8.1 as is (recommended).** Names from Home Assistant reload by themselves
   (per utterance or every `--hass-refresh-seconds`). The static part (`--initial-prompt`) is
   startup-only; put it in a ConfigMap read through `WYO_WHISPER_INITIAL_PROMPT_FILE` and let
   Reloader restart the pod when it changes (the chart already uses
   `reloader.stakater.com/auto`). A restart reloads the model; with a weekly cadence at most this
   is acceptable. Inferred: the prefix is generic Spanish text, so it can live in git.
2. **Small patch, file watched.** Extend the image with a file `/config/stt-hints.txt` (extra
   names) and `/config/stt-corrections.yaml` (see 3.4), checked by modification time at each
   `AudioStart` like the name refresh, and appended as tier 0 after the prefix. About 60 lines in
   `name_cache.py`/`dispatch_handler.py` plus a ConfigMap or Secret volume (a mounted ConfigMap
   updates in place within about a minute; no restart). Needed for home-specific extra words and
   for the correction map.
3. **Small patch, fetched.** Same, but the server fetches hints and corrections from the phase 2
   service over HTTP. Avoids keeping home-specific words in a Kubernetes object that GitOps does
   not manage. More moving parts; only worth it once phase 2 exists.

Image work for option 1 (inferred, to verify in a build): in `pando85/wyoming-addons`
`whisper/Dockerfile`, set `WYOMING_WHISPER_VERSION` to `3.8.1` and install
`wyoming-faster-whisper[hass]` (extra `hass` = `aiohttp`). 3.x needs `faster-whisper>=1.2.1`,
`av<19` and `pysilero-vad`; it does not need torch, so the CTranslate2 CUDA base still fits. The
Ubuntu 20.04 base ships Python 3.8, the minimum 3.x supports; a newer CTranslate2 base is worth
taking at the same time. `run.sh` in the image passes `--uri`, `--data-dir`, `--download-dir`;
3.x reads environment variables too. Upstream's own `Dockerfile.gpu` is not published and is about
10.7 GB because of CUDA torch. 3.0.0 changed `--model` and `--language` defaults to `auto`; the
deployment passes both explicitly, so that change is harmless.

## 2. Building the hint list automatically

### 2.1 Sources

| Source | API (read-only) | Admin needed | Notes |
| --- | --- | --- | --- |
| Exposed entities | ws `homeassistant/expose_entity/list` | yes | only `conversation` exposure |
| Friendly names | ws `get_states` | no | what the user sees and says |
| Aliases, entity area | ws `config/entity_registry/get_entries` | no | |
| Device area | ws `config/device_registry/list` | no | |
| Areas and floors, with their aliases | ws `config/area_registry/list`, `config/floor_registry/list` | no | upstream 3.8.1 includes area and floor aliases next to their names (`_place_names`) |
| Assistant name, Spanish command words | static prefix in git | n/a | generic, not home-specific |
| Media sources (playlists, radio names, artists) | Music Assistant or `media_player/browse_media` | no | only if music commands are common; long names cost many tokens |

Admin requirement documented for 2026.10.4 as above. All APIs are documented websocket commands of
the frontend.

### 2.2 Ranking and trimming

Upstream's deterministic tiers (1.2) are a good default. Changes worth making only after phase 2
data exists:

- **Usage-weighted ranking:** within a tier, order names by how often they appear in successful
  commands over the last 30 days, and promote names that appear in misrecognitions (3.3). This
  needs the patch in 1.7 option 2 or 3.
- **Budget:** keep `--hass-prompt-max-tokens` at the default 200. The measured prompt was about
  120 tokens; a real home that fills the budget gives a longer prompt than was tested, so lower
  the budget to about 130 if the phase 1 check shows echoed names. Spanish names with accents cost
  more tokens per character than English ones (inferred from BPE behaviour; check with `--debug`).
- **Prefix (generic, in git):** the measured verb list from 1.6, unchanged:
  `Enciende, apaga, sube, baja, abre, cierra, para, pausa, reanuda, silencia, quita el silencio,
  pon, temporizador, minutos, volumen, brillo, por ciento, grados.` (about 40 tokens, counted
  inside the 200-token budget). Single words and short phrases only, no full commands (1.4), and
  never "Kivi" (observed leak, 1.6). Changing the list means re-running the study's comparison.

### 2.3 Refresh

Set `--hass-refresh-seconds 300`. Every utterance otherwise opens a websocket and runs `get_states`
for the whole house; with 300 s a rename takes at most five minutes to show and the at-most-1 s
wait at `AudioStop` happens at most once per five minutes (inferred from `name_cache.py`).

### 2.4 Where it runs and the GitOps shape

| Option | Effort | Notes |
| --- | --- | --- |
| A. Inside the STT service (upstream `--hass-token`) | lowest | no generator to maintain; names never leave the cluster or reach git |
| B. CronJob generator writing a ConfigMap or Secret | medium | needs write RBAC in the namespace; fights ArgoCD if the object is in git; home names in a Kubernetes object |
| C. Sidecar generator writing a shared file | medium | no RBAC; still needs the 1.7 option 2 patch |

Recommended A. GitOps changes (proposal, not applied): in `apps/home-assistant/values.yaml` under
`controllers.whisper`: new image tag, args `--hass-api http://home-assistant-hass:8123/api
--hass-refresh-seconds 300`, environment `WYO_WHISPER_HASS_TOKEN_FILE` pointing at a mounted
Secret, and an `ExternalSecret` reading `secret/home-assistant/<path>` from Vault. Keep the
Renovate hint above the image. The token is created by the owner in Home Assistant (a dedicated
user is cleaner to revoke) and written to Vault by the owner.

## 3. Learning from usage

### 3.1 Capture points (documented, 2026.10.4, unless stated)

| Point | What it has | Limits |
| --- | --- | --- |
| Pipeline debug runs: ws `assist_pipeline/pipeline_debug/list` and `/get` | every event of a run: `run-start`, `stt-end` with `stt_output.text`, `intent-end` with `processed_locally` and `intent_output` (response type, error code, targets, `conversation_id`), TTS, timestamps | admin only; in memory, `STORED_PIPELINE_RUNS = 10` per pipeline; lost on restart (the hass pod restarted 45 min before this check, observed) |
| Whisper logs (Loki) | transcript text and time | no intent result, no agent, no confidence; currently 180 days in Loki |
| Satellite logs (webos-voice-satellite) | `STT_END` text, `INTENT_END` speech and `conversation_id`, its own stage latencies | only that satellite; no intent result code |
| `assist_pipeline` `debug_recording_dir` option | audio files per run | audio: off by default, opt-in only |
| A custom Home Assistant integration hooking the pipeline | everything, no loss | couples to internal APIs that change between releases |

Most reliable without touching Home Assistant internals (inferred): **poll the pipeline debug
API every 10 to 15 s** and store new runs. Ten runs per pipeline is far more than a household
produces in 15 s. Cross-check completeness weekly against the whisper transcription count; a
gap only happens across Home Assistant restarts.

### 3.2 What to record and what not

Per run (in the store, not in logs):

- time, pipeline, satellite device, language, conversation id;
- STT text (normalised: lower case, whitespace collapsed), STT duration, audio duration if known;
- confidence from a whisper patch that logs `avg_logprob`, `no_speech_prob` and prompt hash per
  utterance **without text**, joined on time (the Transcript event cannot carry it, 1.3);
- intent stage result: `processed_locally`, response type (`action_done`, `query_answer`,
  `error`), error code (`no_intent_match`, `no_valid_targets`, `failed_to_handle`), intent name
  and target entity ids;
- which agent answered (local built-in or the LLM agent), stage latencies (STT, intent, TTS).

Not recorded: audio (off; opt-in short sessions only, with consent of everyone recorded), TTS
text of LLM answers (not needed for STT learning), anything from runs of other pipelines.

Retention and access:

- raw run rows with text: **30 days**, then deleted; derived counts and n-gram statistics without
  free text kept 1 year; approved corrections (pairs of short command texts) kept until removed.
- store: a new database and user in the existing Zalando cluster `home-assistant-postgres`,
  declared in `apps/home-assistant/templates/postgresql.yaml`; credentials from the operator
  Secret. Velero backups then hold up to 30 days of text too, which should be accepted
  explicitly. A small SQLite file on its own PVC is the lighter alternative.
- access: only the collector and the owner; no ingress; the review page (3.5) behind the existing
  `oauth2-proxy`. Prometheus metrics expose counts only.
- the whisper INFO transcript lines: either drop them in Vector for `container="whisper"` or
  patch the server to log them at DEBUG; otherwise they stay in Loki for 180 days alongside the
  store. Owner decision.

### 3.3 Signals of a misrecognition

| Signal | Definition (initial; calibrate on data) | Strength |
| --- | --- | --- |
| No local match | `intent-end` error `no_intent_match` or `no_valid_targets` from the built-in agent | strong for home commands, but general questions also fail locally by design when they go to the LLM |
| Quick repeat | a second run from the same satellite within 20 s whose text is similar (normalised edit ratio at least 0.6) | strong; the second text is often the correction |
| Repeat that succeeds | quick repeat where the first failed and the second gave `action_done` | strongest; gives a (wrong, right) pair |
| LLM did a home action | `prefer_local_intents` is on and the agent is Kiais through core `llama_cpp` (`conversation.fast`) since 2026-11-05 ([Kiais integration](kiais-integration.md)); so `processed_locally` false and the LLM run changed a device state (state change with the run's context id, from the recorder) | medium; needs the context join, hypothesis that the context id carries through |
| Low confidence | `avg_logprob` below about -0.8 or `no_speech_prob` above 0.5 | weak alone; thresholds are hypotheses |
| User correction | next run within 10 s is "no", "para", "cancela", "eso no" | medium |
| Empty or one-word transcript | empty text or a lone area or entity name | medium; also detects prompt echo |

### 3.4 From signals to proposals

A weekly job (CronJob in the same app) groups the week's flagged runs and produces ranked
proposals, each with its count, examples (at most three, shown only in the review page, never in
notifications) and the expected effect:

1. **Hint words:** words in the right side of (wrong, right) pairs that are names or domain words
   and are not in the current prompt, or were dropped by the budget. Action: raise them (needs
   the 1.7 option 2 or 3 patch) or rename or alias the entity so upstream picks it up.
2. **Entity and area aliases:** a wrong form that recurs (at least 3 times, from at least 2 days)
   for the same target. Action: add it as an alias in Home Assistant. Guard: reject an alias that
   equals or is close to another entity's name or alias in the same area. Aliases are registry
   changes **outside git**, so each one needs the owner's explicit yes (`~/homelab/AGENTS.md`,
   "Owner approval for live changes").
3. **Correction map:** word or phrase rewrites applied to the transcript before intent matching
   (for example a systematic mis-spelling of an area). Home Assistant has no hook between STT and
   the intent stage, so the place is the whisper server (1.7 option 2 or 3, about 30 more lines:
   apply whole-word, case-insensitive rules after transcription). Each rule records its count and
   the date approved, and is dropped if its hit count is zero for 90 days.
4. **Evaluation and fine-tuning dataset:** approved pairs become the text regression set; audio
   only from opt-in sessions where the owner reads the approved phrases (or Piper-synthesised
   clips as in the small local models study).

Review and approval:

- weekly: a Home Assistant persistent notification with counts only ("12 proposals, local success
  rate 81 % (+3)"), linking to the review page;
- review page: a small internal page (the collector's own, behind `oauth2-proxy`) listing
  proposals with approve or reject; approving an alias writes nothing to Home Assistant by itself,
  it produces the exact change for the owner to make or confirm;
- generic changes (code, manifests, generic prefix words, metric and dashboard definitions) go
  through a PR to `~/homelab`; home-specific words, aliases and correction rules never enter the
  public repository (`~/homelab/AGENTS.md`, personal information).

### 3.5 Measuring improvement

- **Primary:** weekly local success rate = runs with `processed_locally` and `action_done` or
  `query_answer`, divided by runs classified as home commands (local success, plus local failures
  whose quick repeat succeeded or whose LLM run acted on a device).
- **Secondary:** no-match rate, quick-repeat rate, LLM fallbacks for home actions, STT p50 and
  p95, prompt echo rate (empty or name-only transcripts).
- **Regression set:** approved (wrong, right) pairs. Text level, offline: the right side must
  match locally and the wrong side, after the correction map, must too (run with
  `builtin_intents.py` against an exported inventory, not against live Home Assistant, because
  `conversation/process` executes actions). Audio level: WER and intent success on the opt-in or
  synthetic clips, with and without the prompt and corrections.
- **Before and after:** compare 2 weeks before a change with 2 weeks after; at household volumes
  (about 80 runs a day observed from whisper logs) a few percentage points is noise, so report
  counts with a confidence interval and treat small changes as unproven.

## 4. Options compared

| Option | Effort | Risk | Latency cost | Privacy |
| --- | --- | --- | --- | --- |
| Static `--initial-prompt` with home names on 2.4.0 | very low | names in git (public repo), stale | prompt prefill (hypothesis: tens of ms) | names exposed publicly: not acceptable |
| Static verb-only `--initial-prompt` on 2.4.0 (interim) | very low | verbs alone not measured | about 40 prompt tokens | generic text only |
| Upstream 3.8.1 `--hass-token` | low (image rebuild, Secret) | admin token in the STT pod; image jump 2.4 → 3.8 | same prefill; refresh hidden in the utterance; at most 1 s once per refresh interval if Home Assistant is slow | names stay in the cluster |
| Generator CronJob plus ConfigMap | medium | write RBAC, drift with ArgoCD | same | names in a Kubernetes object |
| Usage logging and weekly proposals | medium (collector, store, review page) | another admin token; text at rest 30 days | none (off the request path) | transcripts at rest; bounded retention |
| Correction map in the STT server | low on top of phase 2 | a bad rule rewrites good text; mitigated by approval and counts | negligible | rules hold home words, kept out of git |
| Small local model as STT corrector or router | medium to high | added latency and new failure modes | hundreds of ms (hypothesis; see the small local models study) | stays local |
| Fine-tuning Whisper on home data | high | overfitting, maintenance per model update | none at inference | needs consented audio |

## 5. Recommended phased plan

**Phase 1: fixed verb prefix plus live Home Assistant names.**
Justified by 1.6: names plus verbs raised built-in intent success by 3 to 11 points (more in
noise), with few regressions; the wake name stays out. Open gate: the prompt's latency cost
(study section 9) must stay small against the 0.86 s p50.
1. Rebuild `pando85/wyoming-whisper` on `wyoming-faster-whisper[hass]==3.8.1` (owner's repo).
2. In GitOps: new tag, `--hass-api`, `--hass-refresh-seconds 300`, token via ExternalSecret and
   `WYO_WHISPER_HASS_TOKEN_FILE`, and the measured verb list (2.2) as `--initial-prompt`, in a
   ConfigMap read through `WYO_WHISPER_INITIAL_PROMPT_FILE`. No "Kivi".
3. Verify: `--debug` for a few minutes to read the composed prompt (verbs first, then names) and
   the dropped-name count (then off); STT p50 within 0.1 s of 0.86 s; owner's spot check on 20
   home commands, including short ones that start with a verb ("Apaga la tele", "Reanuda").
   Optional interim step before the image rebuild: the verb list alone as `--initial-prompt` on
   the deployed 2.4.0 (one arg, generic text). Verbs alone were not measured (hypothesis: most of
   the gain, because the remaining no-verb errors were leading verbs); check with the study's
   harness first.
4. Owner-side cleanups that help immediately: add Spanish aliases for entities whose names are
   English or technical, and keep exposure to what is actually said.

**Phase 2: usage logging and weekly proposals.**
Collector (polls pipeline debug), store (30-day text), signals (3.3), weekly proposals and review
page (3.4), metrics and a Grafana dashboard with counts only, decision on whisper transcript lines
in Loki. Patch the whisper server to log confidence without text and to read an extra-hints file
and a correction map (1.7 option 2). Start the regression set.

**Phase 3: correction model or fine-tune, only if justified.**
Trigger: after 8 weeks of phase 2, local success on home commands still below the target (owner to
set, for example 95 %) and the residual errors are not fixable by aliases or rules. Candidates: a
small local model as corrector or router (see the small local models study), or a LoRA or full
fine-tune of the Whisper model on consented audio.

## 6. Needs from the owner

- Approval to rebuild and push the whisper image in `pando85/wyoming-addons`.
- Done (2026-11-05): a Home Assistant long-lived token from an administrator account (required by
  `expose_entity/list`), now in Vault (see "The Home Assistant token" below).
- Decision on the transcripts already in Loki (180 days): drop or keep.
- Phase 2: approval to keep transcripts for 30 days in the cluster, the store choice, and the
  review flow; each proposed alias is approved one by one.

### The Home Assistant token

Created on 2026-11-05 for `--hass-token` (not yet wired into the whisper deployment).

- Belongs to the owner's admin user. Home Assistant tokens have no scopes, so it can do anything
  that admin can; treat it as a full-access secret.
- Name: `wyoming-whisper Kivi STT hint list`; lifespan 3650 days (to about 2036-11).
- Vault: `secret/home-assistant/wyoming-whisper`, key `hass_token` (KV v2 version 1). ExternalSecret
  form: `remoteRef.key: /home-assistant/wyoming-whisper`, `property: hass_token`.
- Verified: the Vault value authenticates against `GET /api/` (HTTP 200) and matches the value
  created, by hash.
- Revoke: Home Assistant profile, Security, Long-lived access tokens, delete the entry by name.
  Anything using it then gets 401 and the hint list falls back to the static prefix.
- Rotate: create a new token with the same name pattern over the websocket
  (`auth/long_lived_access_token`) or in the profile, `vault kv put` it from a `0600` file to the
  same path (new KV version), let the ExternalSecret and Reloader roll the pod, then delete the old
  token. See [Home Assistant API access for agents](../troubleshooting/home-assistant-api-hacs-deploy.md)
  for the headless login.

## 7. Open questions

- Latency cost of the ~120 to 200-token prompt on the GTX 1060 (small local models study,
  section 9).
- Verbs-only prompt and prefix-first order, not measured (1.6).
- Does part of the wake word reach the STT audio, and does Home Assistant strip "Kivi" from the
  transcript before matching? (Keeping "Kivi" out of the prompt is settled, 1.6.)
- Answered: `prefer_local_intents` is on and the agent is Kiais via core `llama_cpp`
  (`conversation.fast`) since 2026-11-05 ([Kiais integration](kiais-integration.md)).
- Does `hassil` 3.12.1 fold accents when matching Spanish names?
- Does the context id of an LLM tool call reach the recorder's state changes, so they can be joined
  to the run?

## 8. Log policy: voice transcripts in Loki (temporary exception)

Two owner decisions on 2026-11-05, in this order:

1. Morning: what people say to the assistant must not be kept in logs or Loki. A Vector filter and a
   Whisper image that logs transcripts only at DEBUG implemented it.
2. Later the same day: a time-limited **exception**. To collect real usage for offline replay
   against Home Assistant's built-in sentences (the decision-model idea in
   [the plan](plan.md)), transcripts are kept in Loki for **180 days**, on the owner's explicit
   request ("we can get all the data that we need"). What is collected, why, and how to stop is in
   the [data register](data-register.md).

**Current state (from 2026-11-05, effective once the change is synced).**

- **Source.** The Whisper container (`git.grigri.cloud/agil/wyoming-whisper:3.8.1.1`, namespace
  `home-assistant`, container `whisper`) logs each transcript as
  `DEBUG:wyoming_faster_whisper.dispatch_handler:<text>` (the 2.x image used
  `INFO:wyoming_faster_whisper.handler:<text>`). The image patch moved that line from INFO to DEBUG.
- **How DEBUG is enabled for that one logger.** Not with `--debug`: at DEBUG the server prints its
  whole argument namespace, which includes the Home Assistant token read from
  `WYO_WHISPER_HASS_TOKEN_FILE`, and it enables DEBUG for every library. Instead
  `apps/home-assistant/config/whisper-logging/sitecustomize.py` (a ConfigMap mounted at
  `/opt/whisper-logging`, loaded through `PYTHONPATH`) sets the root logger to INFO and only
  `wyoming_faster_whisper.dispatch_handler` to DEBUG. Besides the transcript, that logger's other
  DEBUG lines are now logged too: audio stopped, VAD end of voice command, completed request time,
  and the **initial prompt with the Home Assistant names**. The names are accepted in logs; the
  token is not logged.
- **Vector.** The `drop_voice_transcripts` filter in `system/loki/vector-agent.yaml` is kept but not
  wired in: the `truncate` transform reads `kubernetes_logs` directly. `vector validate` warns
  "has no consumers", which is expected. Its regex now also matches `dispatch_handler` (the old one
  matched only `handler`, which no longer matches the 3.8.1 logger name), and the tests cover both.
- **Loki.** No per-stream rule. The global `retention_period: 180d` applies (the compactor has
  `retention_enabled: true` and `delete_request_store: filesystem`), so the whisper stream is kept
  180 days like everything else. If the global value is ever lowered, the transcript retention
  changes with it. The stream labels are `namespace="home-assistant"`, `container="whisper"`.
- **False wakes are captured too.** Wake word detection runs on the server (openWakeWord), so a false
  wake streams whatever was being said to Whisper and its transcript lands in Loki like any
  command. People in the room are recorded in text without having said the wake word.
- **How to turn it off.** Revert the commit that made this exception (restores the Vector wiring and
  removes the sitecustomize ConfigMap, `PYTHONPATH` and the mount). Whisper then logs no transcript
  (DEBUG is off for that logger again). Transcripts already in Loki stay until they age out at 180
  days; to remove them earlier, use Loki's delete API (see the data register).

**Original policy, still valid outside the exception.** The other places checked on 2026-11-05
(counts and shapes only, no text read):

- Home Assistant (`hass`): default log level (no `logger:` config), so only warnings and errors
  reach Loki. None of the 2,212 voice-related lines in 7 days carries input text (the common one is
  an `ollama` "model not found" traceback). Do not enable debug for `conversation`,
  `assist_pipeline` or `homeassistant.components.llama_cpp`: that would log request text.
- Home Assistant recorder (PostgreSQL): the `assist_satellite`, `conversation`, `stt`, `tts` and
  `wake_word` entities only keep a state (idle, listening, processing, responding) and
  `friendly_name`; no events carry speech text.
- Piper (`rhasspy/wyoming-piper:2.5.2`): only startup lines at INFO. It logs the synthesized text
  only with `--debug`; never add that flag.
- openWakeWord: startup line only.
- Kiais gateway (namespace `isidoro`, `RUST_LOG=kiais=debug`): logs events and timings, no
  request or response bodies seen. The `chat_messages` table in its database is empty. Re-check
  after Kiais upgrades because it runs at debug level.
- **Old transcripts already in Loki.** Not purged: the morning decision was to stop collecting them,
  not to delete the history, and the exception now keeps new ones as well. They age out with the
  180-day retention. Loki's deletion mode is the default (`filter-and-delete`), so a purge by log
  selector and line filter through the delete API remains possible.
