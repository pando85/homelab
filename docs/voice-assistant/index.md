# Kivi voice assistant

"Kivi" is the household voice assistant. In Home Assistant the Assist pipeline is named "Kiwi"
and the name is pronounced "kivi"; the assistant spells it "Kivi". The satellite hardware and
software are developed in
[pando85/webos-voice-satellite](https://github.com/pando85/webos-voice-satellite); this section
keeps the homelab side: which conversation agent ("brain") answers, measured against the cluster
services.

- [Conversation agent evaluation](experiments/conversation-agent-eval.md): Kiais gateway metrics,
  the dry-run harness in `scripts/voice-assistant/agent_eval/`, Spanish test phrases, and which
  requests Home Assistant's built-in intents already handle.
- [Kiais as the conversation agent](kiais-integration.md): core (`llama_cpp`, `litellm`), HACS and
  own-component options verified on a throwaway Home Assistant 2026.10.4 with
  `prefer_local_intents`, the Kiais streamed tool-call quirk, and the deployment and rollback steps
  awaiting approval (core `llama_cpp`, UI only).
- [STT hint list and learning from usage](stt-hint-list-and-learning.md): biasing the Spanish
  Whisper STT towards the home's names (upstream `--hass-token`), a privacy-bounded loop that
  turns misrecognitions into reviewed hint, alias and correction proposals, and a phased plan.

Results of evaluation runs (per-phrase JSON) are private and never committed; the harness writes
them outside this repository.
