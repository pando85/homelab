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
| No audio collection for now | Revisit only with workstream e. |
| Local models only for any decision model; no cloud API | Privacy; no household text leaves the cluster or the workstation. |

## 4. Workstreams

| | Workstream | Status | Next step | Gate / decision owner |
| --- | --- | --- | --- | --- |
| a | Wake word "Kivi" | Training | Retrain with word-level SNR augmentation, evaluate against `alexa` | Owner approves deploy |
| b | Whisper | Done (3.8.1.1 live) | Confirm the hint-list effect on real commands | Owner provides real commands |
| c | Real-usage validation | Collecting | Offline replay of the collected transcripts | Owner reviews samples |
| d | Decision model ("Kivi decider") | Validating value | P0 zero-shot baselines and headroom analysis | Headroom meaningful, then owner |
| e | Whisper fine-tuning | Future, not now | Wait for real data and AEC work | Owner consent, household consent |
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

Log policy and the data register: [Log policy](stt-hint-list-and-learning.md#8-log-policy-no-voice-transcripts-in-logs-or-loki)
(the data register is in progress and will be linked from there).

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
| P4 | Integration: decider HTTP service plus a small Home Assistant custom conversation agent that delegates to Kiais; shadow mode first | Owner approves each live step |

### e. Whisper fine-tuning (future, not now)

Only if real data shows ASR errors dominate, and after the AEC / front-end work. Opt-in audio with
household consent, kept local for 90 days, LoRA on large-v3-turbo, evaluated on a held-out real set
against the base model.

### f. Later / backlog

- Phase-2 learning collector: alias proposals from misses, within the transcript retention window
  ([STT hint list and learning](stt-hint-list-and-learning.md)).
- Kiais stream tool-call chunk fix (optional; [Kiais integration](kiais-integration.md)).
- AEC / front-end and false-wake work, in the
  [satellite repository](https://github.com/pando85/webos-voice-satellite).
- Cleanup of old Home Assistant entries.

## 5. Data handling

Purpose-limited: transcripts are collected only to validate the assistant on real usage, in Loki
with 180-day stream retention, and analysed locally; derived replay datasets on the workstation are deleted at the end of the experiment, at most 30 days. Agents see aggregates only (counts, miss rates,
categories); no transcript text goes to any AI service or cloud. Samples are shown only to the
owner. Nothing private is committed to git. Transcripts expire with the 180-day retention; stopping means
reverting the Loki rule and retention change. No audio is collected. Policy and checked locations:
[Log policy](stt-hint-list-and-learning.md#8-log-policy-no-voice-transcripts-in-logs-or-loki);
the data register is in progress.

## 6. Open decisions for the owner

1. Approve the deploy of the custom "Kivi" wake word once it beats `alexa` (rollback is one setting).
2. Provide real commands (or a review session of samples) to confirm the hint-list effect and the
   "Pon" / "Con" error.
3. Decide, after the P0 headroom analysis, whether the decider goes on to P1 and P2.
4. Choose the optional local teacher for P2 (pplx-decider-v1.1-27b or Laya), if the project reaches it.
5. Approve any live Home Assistant change: shadow-mode conversation agent, entity cleanup.
6. Decide whether, and when, to open the audio collection for Whisper fine-tuning, with household
   consent.
7. Confirm whether the 180-day transcript retention stays or is shortened once the validation is done.
