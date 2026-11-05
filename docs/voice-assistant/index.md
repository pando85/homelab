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

Results of evaluation runs (per-phrase JSON) are private and never committed; the harness writes
them outside this repository.
