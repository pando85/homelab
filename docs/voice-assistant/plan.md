# Kivi plan

Last updated 2026-11-05.

## 1. Goal

A Spanish voice assistant, "Kivi" (Home Assistant pipeline "Kiwi"), with low latency and high
accuracy on home commands. General questions, live information and follow-ups are routed to Kiais.
The MVP is home control; everything else is a later improvement.

## 2. Current architecture (live 2026-11-05)

```text
satellite (LG webOS TV, ESPHome API)
  -> HA openWakeWord (wake word still `alexa`)
  -> Whisper large-v3-turbo int8 on the GTX 1060
       custom image agil/wyoming-whisper 3.8.1.1, verbs prompt + live HA names
  -> HA built-in intents first (prefer_local_intents)
  -> miss: Kiais fast, through the core llama_cpp integration
  -> Piper TTS
```

| Measure | Value |
| --- | --- |
| Whisper latency, p50 | 0.91 s |
| Local (built-in intent) commands, end to end | about 1 s |
| Kiais commands | about 4.1 s |
| Kiais general questions | about 2.3 s (first words about 0.9 s) |
| Verbs prompt, clean synthetic audio | 82 to 88% correct |
| Verbs prompt, synthetic audio at 0 dB SNR | 46 to 57% correct |

The accuracy rows come from synthetic Spanish audio, not real household use; see
[Small local models](experiments/small-local-models.md) and
[STT hint list](stt-hint-list-and-learning.md). Detail on each part:
[Conversation agent evaluation](experiments/conversation-agent-eval.md),
[Kiais as the conversation agent](kiais-integration.md),
[Conversation agent design](conversation-agent.md),
[Whisper image build](whisper-image-build.md).

## 3. Decisions log (2026-11-05)

| Decision | Why |
| --- | --- |
| Core `llama_cpp` integration to reach Kiais, not `litellm` or an own component | Least to maintain; verified with `prefer_local_intents` ([Kiais integration](kiais-integration.md)). |
| The verbs prompt ships with the custom Whisper image; the verbs-only fallback on 2.4.0 is not needed | The canary passed and 3.8.1.1 is live ([image build](whisper-image-build.md)). |
| No local fallback LLM | Kiais availability is treated as Home Assistant's own ([Decisions 2026-11-05](conversation-agent.md#decisions-2026-11-05)). |
| No small LLM on the GTX 1060 (router, STT corrector or home agent) | Rejected by measurement ([Small local models](experiments/small-local-models.md)). |
| Transcripts re-enabled in Loki for the Whisper stream, 180-day retention | Needed to validate against real usage; see section 5. |
| No audio collection for now | Superseded the same day by the next row. |
| Audio collection on (owner approval 2026-11-05): Home Assistant keeps the audio of every pipeline run for 90 days, including the continuous wake-stage room audio while the satellites listen | Real household audio for wake word, AEC / front-end and speech-to-text work. Cluster storage only, no backup, agents see counts and sizes only; section 5 and the [data register](data-register.md) (datasets 5 and 6). |
| Local models only for any decision model; no cloud API | Privacy; no household text leaves the cluster or the workstation. |

## 4. Workstreams

| | Workstream | Status | Next step | Gate / decision owner |
| --- | --- | --- | --- | --- |
| a | Wake word "Kivi" | Training | Retrain with word-level SNR augmentation, evaluate against `alexa` | Owner approves deploy |
| b | Whisper | Done (3.8.1.1 live) | Confirm the hint-list effect on real commands | Owner provides real commands |
| c | Real-usage validation | Collecting | Offline replay of the collected transcripts | Owner reviews samples |
| d | Decision model ("Kivi decider") | Validating value | P0 zero-shot baselines and headroom analysis | Headroom meaningful, then owner |
| e | Whisper fine-tuning | Collecting audio (from 2026-11-05) | Fine-tune only after the AEC work, on the collected request audio | Owner (approved the collection 2026-11-05) |
| f | Later / backlog | Not started | See below | Owner |

### a. Wake word "Kivi"

- Custom model in openWakeWord format, trained on the workstation. Doc:
  [Kivi wake word](experiments/kivi-wake-word.md) (in progress).
- First runs collapsed: the default augmentation sets the SNR against the whole window, so the
  positives end up too noisy and the training pushes a huge weight on the negatives. Retraining with
  word-level SNR augmentation.
- Baseline to beat, `alexa` on synthetic audio: recall 63% / 38% / 21% at clean / 10 dB / 0 dB;
  false accepts 0.2 to 0.4 per hour of Spanish speech.
- Deploy needs the owner's approval. Rollback: set the satellite's `wake_word_id` back to `alexa`.

### b. Whisper

- Done: 3.8.1.1 live with the verbs prompt and live Home Assistant names.
- Next: confirm the hint-list effect with the owner's real commands; investigate the "Pon" heard as
  "Con" error; Renovate on the private image repository; manual image bumps in this repository
  until then.

### c. Real-usage validation (data)

Transcripts stay in Loki for 180 days (section 5). Replay on the workstation uses derived
datasets that are deleted at the end of the experiment, at most 30 days. Plan:

1. Offline replay of the transcripts against Home Assistant's built-in sentences and the real
   entities.
2. Measure the miss rate and classify each miss: misheard (STT), phrasing gap, other.
3. Turn the classes into fixes: aliases, custom sentences, hint-list changes.

Log policy and the data register: [Log policy](stt-hint-list-and-learning.md#8-log-policy-voice-transcripts-in-loki-temporary-exception),
[data register](data-register.md).

### d. Decision model ("Kivi decider")

Validate the value first; build nothing before there is headroom over the built-in intents.

```text
utterance (STT text)
  -> HA built-in intents
       match: execute (about 1 s)
       miss:  Kivi decider (small local model)
                -> home command: execute the intent through HA
                -> everything else: Kiais (general questions, live info, follow-ups)
```

| Phase | What | Detail |
| --- | --- | --- |
| P0 | Zero-shot baselines: Laya, Quyet-1.0-Tiny, embedding + fuzzy matching | [Decision models](experiments/decision-models.md) (in progress) |
| P0 | Broader synthetic dataset `kivi-fake-v1`: fake homes, phrasing styles, negatives including TV dialogue; headroom analysis | [Decider PoC](experiments/kivi-decider-poc.md) (in progress) |
| P0 | Real-usage replay after a few days of data (workstream c) | |
| P1 | Data, only if headroom is meaningful | |
| P2 | Fine-tune Quyet-1.0-Tiny; optional local teacher: pplx-decider-v1.1-27b (open weights) or Laya | |
| P3 | Gates: wrong actions at most 0.5%; at least 50% of built-in misses rescued; p95 below 150 ms; at most 800 MB on the GTX 1060 (fp32, Pascal) | Any miss of a gate stops the line |
| P4 | Integration: decider HTTP service plus a small Home Assistant custom conversation agent that delegates to Kiais; shadow mode first; structure borrowed from [home-assistant-laya](#related-project-home-assistant-laya), not installed | Owner approves each live step |

#### Related project: home-assistant-laya

[allenporter/home-assistant-laya](https://github.com/allenporter/home-assistant-laya) (Apache-2.0,
read 2026-11-05 at `8803550`, `laya==0.4.24`; source reading only, nothing installed or run) is a
Home Assistant custom component (HACS) with a conversation agent entity around the same Laya model.
It is the closest existing match for P4, but it is a prototype, not a base to run as is.

What it does (documented, from the source):

- **Pipeline.** Selected as the pipeline's conversation agent. It builds a shortlist (at most 5
  intents, 10 areas, 20 entities, lexical matching on names), asks one Laya forward pass five typed
  questions (intent, compound, target entity, target area, entity or area), and on a confident
  answer executes the HA intent (`intent.async_handle`, slots `name`, `area`, `domain`,
  `brightness`, `temperature`). On low confidence or a "compound" request it calls
  `conversation.async_converse` on a configured fallback agent, so a chain Laya, then Kiais is a
  config option. With `prefer_local_intents` the pipeline tries the built-in intents first
  (HA behaviour as in [Kiais integration](kiais-integration.md); not yet tried with this
  non-LLM agent, hypothesis until the shadow run).
- **Entities.** Only those exposed to Assist (`async_should_expose`), but if none are exposed it
  silently uses all states. Controllable domains only.
- **Gate.** Per-question probability of the chosen option against `confidence_threshold` 0.40 (the
  README describes a normalised formula, the code uses the raw probability) and `compound_threshold`
  0.70. Our zero-shot study needed 0.6 to 0.85 and the threshold did not transfer between phrase
  sets ([Decision models](experiments/decision-models.md)).
- **Hardware.** In-process PyTorch in the HA executor via the `laya` package; the README quotes
  about 1.2 GB of RAM, 100 to 250 ms on a CUDA GPU and 1.5 to 3.0 s on CPU for a full home context
  (author estimates, not measured by us). `idle_timeout` defaults to 0, so the weights are unloaded
  after every request and reloaded on the next.

Why it does not fit as is:

- **No Spanish.** The component calls `laya.load()` with no checkpoint, which is the English-only
  root checkpoint (ModernBERT-large); the multilingual one is never selected. Instructions, stopwords,
  the domain words and the number regexes (`%`, "degrees") are English; "grados" or "por ciento" are
  not parsed. Tests and the evaluation home are English only.
- **No evidence for our gates.** No accuracy, wrong-action or rescue numbers; the benchmarks are the
  model card's latency. The slot set excludes volume and media actions, which we planned to cover.
- **Wrong-action risk by design.** An area target with no domain word in the utterance is sent as
  `area` only (hypothesis: what HA does with it is unchecked).
- **Hard to deploy in our cluster.** `requirements` makes HA pip-install `laya` (torch, transformers,
  and a new `vegaml` dependency) into the HA container; issue 6 reports the install failing on
  HA OS. Issue 7 (remote Laya service) is open without a reply.
- **Maturity.** Created 2026-09-20, one release (0.1.0). The author's 45 commits are from 2026-09-20
  and 21; since then only Renovate bumps (53 commits). 9 stars, 1 fork, 3 open issues. CI runs
  pytest, ruff, hassfest and HACS validation; about 900 lines of integration tests, speculative-engine unit tests and slow live tests
  on an English fake home. `hacs.json` says HA 2024.04 but the code uses the newer conversation
  entity API; unproven against our HA 2026.10.4.
- **Privacy and supply chain.** Inference is local and the integration code makes no network
  calls of its own. HA installs `laya` from PyPI at runtime and the package downloads weights from
  Hugging Face on first load (revision pinned inside `laya`, safetensors and JSON only, no
  `trust_remote_code`). The integration passes no hash. Not reviewed: `laya` 0.4.24 beyond a read of
  `load` and `serve`; `vegaml` not reviewed at all.

What is reusable:

- **`laya[serve]`** already ships an HTTP server (FastAPI, `POST /v1/systemone` and `/batch`, bearer
  key, idle unload, `LAYA_THREADS`, `LAYA_MODELS` to pick `multilingual`, `LAYA_CUDA_AMP=fp32`
  to avoid fp16 autocast on Pascal). That covers the "decider HTTP service" half of P4 without code
  of ours, for Laya only (Quyet needs its own service).
- **`DecisionEngine`** (`speculative/scoring/engine.py`) is a small abstract class
  (`async_predict(state, questions)`), so a `RemoteEngine` that calls the server over HTTP is
  roughly 60 lines. This is the change issue 7 asks for.
- **The 5-stage `DecisionFlow`** (request, retrieval, hydration, scoring, resolution) and its
  fixtures are a useful reference for the HA side: exposure handling, intent introspection,
  originating-area prior, fallback via `async_converse`.

How it changes P4 (proposal, owner decides):

1. **Build our own thin HA agent for P4, borrowing the structure**; do not install this component.
   Reasons: Spanish, our gates, shadow mode, our own question design and parsing (already measured
   in `decision_models/`) and the model choice are all ours, so the reusable part is the ~190-line
   `conversation.py` pattern and the `DecisionEngine` seam, both Apache-2.0 (keep the licence
   header and attribution if code is copied).
2. **Serve over HTTP outside HA**: `laya[serve]` for Laya, a minimal service for Quyet; HA never
   imports torch.
3. **Add what is missing**, in this order: Spanish number and domain parsing; the `multilingual`
   or fine-tuned checkpoint; a per-question threshold fitted on `kivi-fake-v1` and real replay and
   checked on held-out phrases; a shadow mode (log the decision, always fall through to Kiais, no
   device action); no "all states" fallback; area-only targets rejected unless the domain is known;
   unload disabled (`idle_timeout` > 0 equivalent) so p95 can be measured.
4. **Contribute only small, upstreamable pieces** if useful (remote engine for issue 7, a
   `model` option), after P3 says the line continues. There is no sign of maintainer activity
   beyond the first two days, so do not depend on upstream merges.
5. **Reference result, not a reason to skip P3.** This project does not change the P0 finding:
   zero-shot Laya multilingual was near chance on action and area in Spanish; the component adds no
   fine-tuning or Spanish data. The P3 gates stay as they are.

### e. Whisper fine-tuning (audio collection started, fine-tuning later)

Audio collection started on 2026-11-05 on the owner's approval: Home Assistant's `assist_pipeline`
debug recording keeps the request audio and the continuous wake-stage room audio of every run for
90 days, on the cluster volume `home-assistant-voice-recordings` ([data register](data-register.md),
datasets 5 and 6). Fine-tuning itself still waits: only if real data shows ASR errors dominate, and
after the AEC / front-end work. Then LoRA on large-v3-turbo, evaluated on a held-out real set against
the base model. The wake-stage audio also serves the wake word (workstream a) and AEC work.

#### Related project: paulglavin/VoicePipeline (reviewed 2026-11-05)

Not adopted. A server-side Wyoming STT wrapper: offline DTLN noise suppression, Silero VAD,
WeSpeaker speaker ID and English STT; no AEC and no wake word. MIT, a single-author hobby project
with no tests. Ideas borrowed into this plan:

1. **Review and labelling loop for the recorded audio**, needed before the recordings can train the
   wake word, AEC or STT: the stored WAV with its transcript and scores, a Pending queue of
   low-confidence items, owner confirm or reject, and an append-only labels table.
2. **Experiment: a blind noise suppressor arm** (DTLN or RNNoise; off, on and a 50 % blend) in the
   synthetic noise benchmark and the live A/B runner. Expected gain is modest; AEC remains the main
   fix.
3. **Parked: speaker ID**, for per-person replies or for rejecting TV voices.

### f. Later / backlog

- Phase-2 learning collector: alias proposals from misses, within the transcript retention window
  ([STT hint list and learning](stt-hint-list-and-learning.md)).
- Kiais stream tool-call chunk fix (optional; [Kiais integration](kiais-integration.md)).
- AEC / front-end and false-wake work, in the
  [satellite repository](https://github.com/pando85/webos-voice-satellite).
- Cleanup of old Home Assistant entries.

## 5. Data handling

Purpose-limited: transcripts are collected only to validate the assistant on real usage, in Loki
(global 180-day retention, no separate stream rule), and analysed locally; derived replay datasets on the workstation are deleted at the end of the experiment, at most 30 days. Agents see aggregates only (counts, miss rates,
categories); no transcript text goes to any AI service or cloud. Samples are shown only to the
owner. Nothing private is committed to git. Transcripts expire with the 180-day retention; stopping means
reverting the exception commit. Audio of every pipeline run (request audio and the continuous
wake-stage room audio) is kept for 90 days on a cluster volume from 2026-11-05, on the owner's
approval; agents never open or listen to it and report counts and sizes only. Policy and checked locations:
[Log policy](stt-hint-list-and-learning.md#8-log-policy-voice-transcripts-in-loki-temporary-exception);
[data register](data-register.md).

## 6. Open decisions for the owner

1. Approve the deploy of the custom "Kivi" wake word once it beats `alexa` (rollback is one setting).
2. Provide real commands (or a review session of samples) to confirm the hint-list effect and the
   "Pon" / "Con" error.
3. Decide, after the P0 headroom analysis, whether the decider goes on to P1 and P2.
4. Choose the optional local teacher for P2 (pplx-decider-v1.1-27b or Laya), if the project reaches it.
   Related: for P4, confirm "own thin agent, borrowing the home-assistant-laya structure" instead of
   installing or forking that component (workstream d).
5. Approve any live Home Assistant change: shadow-mode conversation agent, entity cleanup.
6. Decided 2026-11-05: audio collection is on (90 days, including wake-stage room audio). Household
   consent is the owner's responsibility.
7. Confirm whether the 180-day transcript retention stays or is shortened once the validation is done.
