# Kivi voice assistant

"Kivi" is the household voice assistant. In Home Assistant the Assist pipeline is named "Kiwi"
and the name is pronounced "kivi"; the assistant spells it "Kivi". The satellite hardware and
software are developed in
[pando85/webos-voice-satellite](https://github.com/pando85/webos-voice-satellite); this section
keeps the homelab side: which conversation agent ("brain") answers, measured against the cluster
services.

- [Kivi plan](plan.md): goal, live architecture, decisions, workstreams (wake word, Whisper, real-usage
  validation, decision model, fine-tuning) and the open decisions for the owner.
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

Results of evaluation runs (per-phrase JSON) are private and never committed; the harness writes
them outside this repository.
