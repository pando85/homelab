# Kivi voice assistant

"Kivi" is the household voice assistant. In Home Assistant the Assist pipeline is named "Kiwi"
and the name is pronounced "kivi"; the assistant spells it "Kivi". The satellite hardware and
software are developed in
[pando85/webos-voice-satellite](https://github.com/pando85/webos-voice-satellite); this section
keeps the homelab side: which conversation agent ("brain") answers, measured against the cluster
services.

- [Kivi plan](plan.md): goal, live architecture, decisions, workstreams (wake word, Whisper, real-usage
  validation, decision model, fine-tuning) and the open decisions for the owner.
- [Conversation capabilities roadmap](conversation-capabilities-roadmap.md) (proposal, 2026-11-07):
  safe MCP/data tools, natural Spanish voice replies, end-to-end streaming, truthful progress
  announcements, optional asynchronous jobs and memory/RAG, and evaluation/rollout gates.
- [Audio pipeline plan](audio-pipeline-plan.md) (proposal, 2026-11-07): the server half of the mid-term plan
  shared with the satellite repository: what HA 2026.10.4 does with satellite audio, Whisper and openWakeWord
  settings, false-wake labels and wake counts, diagnostics in Grafana, capability profiles and device-class
  pipelines, XVF3800 and ESP32 onboarding, a design-only `kivi-audio` Wyoming proxy, and new data register rows.
- [Conversation agent evaluation](experiments/conversation-agent-eval.md): Kiais gateway metrics,
  the dry-run harness in `scripts/voice-assistant/agent_eval/`, Spanish test phrases, and which
  requests Home Assistant's built-in intents already handle.
- [Kiais as the conversation agent](kiais-integration.md): core (`llama_cpp`, `litellm`), HACS and
  own-component options verified on a throwaway Home Assistant 2026.10.4 with
  `prefer_local_intents`, the Kiais streamed tool-call quirk, and the deployment (core `llama_cpp`,
  UI only, deployed 2026-11-05 as `conversation.fast`) and rollback steps.
- [Small local models](experiments/small-local-models.md): the GTX 1060 budget, small models as
  router, STT corrector and home agent, Whisper prompt, model size and CPU options, measured on
  synthetic Spanish audio; ranked additions in [Conversation agent design](conversation-agent.md).
- [Kivi decider PoC](experiments/kivi-decider-poc.md): headroom of a small decision model behind the
  built-in intents, measured before building one; the synthetic dataset `kivi-fake-v1` (four fake
  homes, phrasing styles, TV-dialogue negatives, local-LLM paraphrases, audio subset) and what
  custom sentences already cover.
- [Decision models](experiments/decision-models.md): whether a small non-generative decision model
  (Laya, Quyet) or an embedding and fuzzy-matching baseline can execute, offline and without wrong
  actions, the home commands the built-in intents miss, before they go to Kiais; zero-shot on the
  synthetic corpus, the `kivi-fake-v1` fake homes and the real names (counts only).
- [Whisper GPU benchmark](experiments/whisper-gpu-benchmark.md): compute type and beam size of the live
  Whisper on the GTX 1060 with synthetic Spanish commands (partial: no setting beats the current one on a
  quiet GPU, beam 1 saves 26 ms; the benchmark pod disturbed the live service, see the incident section;
  `--vad-clip` not measured).
- [STT hint list and learning from usage](stt-hint-list-and-learning.md): biasing the Spanish
  Whisper STT towards the home's names (upstream `--hass-token`), a privacy-bounded loop that
  turns misrecognitions into reviewed hint, alias and correction proposals, and a phased plan.
- [Data register](data-register.md): what the assistant collects (Whisper transcripts in Loki for 180
  days under a temporary exception, derived replay datasets, names list, Kiais requests, no audio),
  where it lives, who reads it, how to stop or delete it, and the usage rules.
- [Building the Whisper STT image on Forgejo](whisper-image-build.md): summary and the cluster side
  (canary, ExternalSecret, prompt ConfigMap, values.yaml cutover, rollback to 2.4.0). The build
  itself (Dockerfile, Forgejo workflow, Pascal sm_61 checks, runner disk, risks) lives in
  `agil/wyoming-whisper` on `git.grigri.cloud` (private).

- [Oye Kivi recovery plan](experiments/oye-kivi-recovery.md) (2026-11-08): baseline failures,
  a strict-target experimental data profile, real-speaker/room evaluation, DSP replay, release gates
  and next steps. No model or Home Assistant change is deployed by this plan.
- [P0 real-voice baseline](experiments/oye-kivi-p0-baseline.md) (2026-11-08): deployed v4 recognises
  22.5 % of 80 consented real takes (30 % TV off, 15 % TV on) and false-accepts 18.5 % of 54 real
  near-word takes; the desk mic scores the same, so the gap is the model, not the satellite.
- [vnext ablations](experiments/oye-kivi-vnext-ablations.md) (2026-11-08): real positive takes lift
  unseen-take recall from 25 % to 70 % on the satellite at the same false-wake budget; confuser
  negatives without them collapse it to 1 %. TV on meets the gate, TV off at 3 m does not.
- [Custom wake word "kivi"](experiments/kivi-wake-word.md): openWakeWord model for the lone word
  "kivi", trained on synthetic Spanish voices, measured against `alexa` with the server's exact
  features (recall, false accepts per hour, near-words), deployment notes (`--custom-model-dir`,
  calibrated threshold, rollback to `alexa`) and the owner's real-voice test. Not deployed.
  Scripts in `scripts/voice-assistant/wakeword/`.
- [Model registry](model-registry.md): one row per model (STT, wake words `alexa` and `kivi`, conversation
  agent, decision models) with version, short sha256, where it runs, whether it is deployed, threshold,
  measured scores, licence and storage (the licence-restricted `kivi` model is in the private
  Forgejo repository `agil/kivi-models`), and how to register a model.
- [Lessons learned](lessons-learned.md): pitfalls met so far, each with symptom, root cause,
  detection, fix and guard.

Results of evaluation runs (per-phrase JSON) are private and never committed; the harness writes
them outside this repository.
