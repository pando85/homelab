# Kivi conversation capabilities roadmap

**Status:** proposal (documentation only; no deployment or model change approved)  
**Date:** 2026-11-07  
**Scope:** Home Assistant's conversational agent, external information access, natural spoken replies,
latency feedback, and future memory. Audio front-end/wake-word work is tracked separately in
[audio-pipeline-plan.md](audio-pipeline-plan.md) and the
[webOS satellite repository](https://github.com/pando85/webos-voice-satellite).

## 1. Goal and constraints

Make Kivi feel like a responsive voice assistant for everyday home control **and** useful
questions requiring current or personal data. It should answer simple commands immediately,
fetch external data only when relevant, speak naturally, and accurately acknowledge work that
takes longer than a normal voice turn.

The documented live architecture is:

```text
Satellite -> Home Assistant Assist -> Whisper STT
   -> prefer_local_intents
      |-- built-in HA intent -> direct action -> Piper TTS
      '-- no match -> HA core llama_cpp conversation integration
                    -> Kiais gateway, tier "fast"
                    -> model/tool call(s) -> Piper TTS
```

**Important:** `llama_cpp` here is the **Home Assistant integration**, used as an
OpenAI-compatible client of Kiais. This does **not** mean the deployed conversation model runs
locally in llama.cpp. Keep the current model gateway and `prefer_local_intents` behavior;
do not add a second local-LLM router or change the Whisper/GPU budget as part of this plan.

Reference baselines (2026-11-05 documentation; verify against production before using as gates):

| Path | Documented latency |
| --- | --- |
| Built-in intent, end-to-end | ~1 s |
| Kiais home-control command | ~4.1 s |
| Kiais general question | ~2.3 s |
| Whisper STT p50 | ~0.9 s |

See [Kivi plan](plan.md), [conversation-agent.md](conversation-agent.md),
[Kiais integration](kiais-integration.md), and
[conversation agent evaluation](experiments/conversation-agent-eval.md).

## 2. Design principles

1. **Preserve the fast path.** Deterministic HA intents remain first; never require
   web retrieval or an LLM tool round for ordinary supported control commands.
2. **Tools, not huge prompts.** Expose small, typed, bounded queries; retrieve relevant
   information on demand. Do not inject all entities, history, or documents into each turn.
3. **Truthful progress.** Spoken acknowledgements reflect actual execution state; do not
   announce "searching" before a search/tool job has started or for a trivial action.
4. **One conversation per user request.** Preserve conversation ID, satellite/area,
   tool-result correlation, cancellation, and reply ordering across tool calls and speech.
5. **Least privilege and privacy.** Explicit allowlist, read-only by default, secrets in
   existing secret management, minimal tool responses, no unreviewed cloud disclosure.
6. **Measure before replacing.** Prefer existing HA integrations and streaming facilities;
   build a custom orchestrator only for a confirmed capability gap.

## 3. Target architecture

```text
Multiple satellites
  -> existing Assist wake word / STT path
  -> HA built-in intents -------------------------------> Piper -> originating satellite
  -> miss
       -> HA conversation agent -> Kiais fast
             |-- existing HA Assist/LLM tools (device control; exposed entities only)
             |-- selected MCP tools (read-only lookup, weather, energy, calendar, web)
             '-- later: bounded memory / document retrieval
       -> streamed text (if end-to-end delivery is supported)
       -> progress/long-task coordinator (only where needed)
             |-- accurate short acknowledgement after a threshold
             |-- cancellation + busy/interrupt handling
             '-- optional asynchronous job with later reply to the originating satellite
```

Home Assistant's **MCP client** can make a remote MCP server's *tools* available
to compatible conversation agents. It currently does not expose MCP resources, prompts,
sampling, or notifications to the agent. Verify that the **installed HA version and the
specific `llama_cpp` agent configuration** accept the required extra tools and stream
tool calls correctly before choosing MCP over native HA tools or scripts. Do not confuse
HA's MCP client (outbound lookup for Kivi) with HA's MCP server (inbound access to HA).

References:
- [Home Assistant MCP client](https://www.home-assistant.io/integrations/mcp/)
- [Assist Satellite integration](https://www.home-assistant.io/integrations/assist_satellite/)
- [Assist Satellite entity / announcement feature support](https://developers.home-assistant.io/docs/core/entity/assist-satellite/) (check version-specific implementation)

## 4. Phased implementation

### Phase 0 — Baseline and integration capability spike

**Before any production change.**

- [ ] Record real satellite latency distributions: wake-to-STT, STT, intent match,
  Kiais first content/token, tool round-trip, first *audible* TTS, full speech completion;
  capture p50/p95 and timeouts by route and satellite.
- [ ] Re-run the existing fake-home dry-run and real-satellite smoke tests with
  `prefer_local_intents` enabled. Include follow-ups, Spanish colloquialisms, and
  multiple exposed entities with the same friendly name.
- [ ] Test HA 2026.10.4 `llama_cpp` + Kiais `fast` with one benign mock read-only
  MCP tool: tool-schema discovery, tool-call arguments, result delivery, streaming,
  and multi-turn conversation ID retention.
- [ ] Confirm whether the *deployed* Piper/TTS, Assist event stream, and both webOS
  satellite implementations can play first-sentence audio before the entire LLM
  response is complete. Test without assuming incremental LLM text equals audible
  streaming.
- [ ] Check `assist_satellite.announce` and `start_conversation` support on each
  target device. A listed HA service alone does not imply satellite feature support.

**Gate:** baseline and an end-to-end capability matrix exist; either confirm that the
core agent supports the chosen tools or document the smallest alternative integration.
No new orchestrator yet.

### Phase 1 — Useful, safe external tools

Start with a **small curated tool catalog**, not an open-ended executor. Candidate tools:

| Priority | Capability | Example contract | Risk/control |
| --- | --- | --- | --- |
| 1 | Energy/home measurements | `energy_get_summary(period, scope)` returning kWh, cost basis, timestamps | read-only; bounded intervals; known units |
| 1 | Weather/forecast | `weather_get_forecast(area, horizon)` returning source and forecast time | short cache TTL; freshness shown |
| 2 | Web lookup | `web_search(query, limit)` returning titles, links, publication dates, excerpts | explicit egress, timeouts, hostile-content handling |
| 2 | Calendar | `calendar_list_events(range)` with limited calendar selection | account consent, scoped credentials, minimal disclosure |
| 3 | Observability | `system_health_summary(service, window)` instead of unrestricted PromQL | read-only; scoped services; bounded result size |

Implementation tasks:
- [ ] Identify existing HA integrations, scripts, and purpose-built APIs first.
  Use the HA MCP client for remote tool services when its compatibility spike passes.
- [ ] Add explicit allowlisting, auth scopes, per-tool deadline, concurrency limits,
  output size limits, argument validation, and safe error/result schemas.
- [ ] Provide source, retrieval timestamp, units, and freshness/TTL where appropriate.
  Return structured results; never make the LLM parse bulk logs, full web pages,
  or unbounded historical sensor series.
- [ ] Define which tools are **cloud-eligible**: Kiais `fast` may route requests and
  returned content to third-party providers. Review any personal-data exposure before
  enabling tools; mask secrets, private identifiers, and unnecessary entity details.
- [ ] Test prompt-injection resistance for untrusted web/doc content; external text
  must be treated as data, never as authorization to call another tool.
- [ ] Add a small Spanish evaluation set (fresh facts, ambiguous targets, unavailable
  source, tool timeout, stale cache, prompt injection).

**Gate:** data questions return sourced or appropriately qualified answers, and
simple commands keep the original fast path with no meaningful latency regression.

### Phase 2 — Spoken style, streaming, and conversational continuity

- [ ] Refine Kivi's Spanish voice prompt: one or two short sentences by default,
  natural phrasing, no Markdown or long lists, no repetitive acknowledgements,
  a single clarifying question when needed, and explicit uncertainty for missing data.
- [ ] Preserve `conversation_id` and the source satellite/area for follow-up turns;
  use room context to resolve ambiguous targets, without silently changing rooms.
- [ ] Verify tool-call streaming through Kiais and the HA core integration (including
  fragmented tool-call deltas) and tune final-answer segmentation for Piper.
- [ ] Compare whole-response TTS vs sentence-chunked TTS on actual satellites:
  first audible speech, clipping, prosody, buffering, speaker overlap, and barge-in.
- [ ] Add golden voice dialogues covering brief controls, direct questions, tool
  lookups, multi-turn follow-ups, ambiguity, and interruptions.

**Gate:** first speech is measurably quicker for eligible streamed answers without
truncated sentences, reordered speech, or additional wrong device actions.

### Phase 3 — Honest progress and long-running tasks

**Proposed initial policy**, to be tuned using production measurements:

| Elapsed time without useful speech | Behavior |
| --- | --- |
| 0–2 s | No filler; allow ordinary work to finish |
| ~2–3 s | If a real long-running tool/job has started, optionally say a short, specific acknowledgement |
| ~3–10 s | Continue work silently; avoid chatter |
| ~10–15 s | Optional useful status update only if execution is still active |
| >20–30 s | If supported, offer asynchronous completion rather than holding the voice session indefinitely |

- [ ] Define a per-request state machine:
  `accepted -> tool_started -> progressing -> completed/failed/cancelled`.
  Emit acknowledgements only from confirmed transitions, never from LLM speculation.
- [ ] Decide which component owns progress: start by testing HA native streaming/events.
  Add a thin coordinator only if HA cannot interleave progress audio and final speech.
- [ ] Ensure progress and final TTS play **in order** and on the originating satellite;
  handle satellite busy states, double announcements, loss of connection,
  retries, duplicate events, and user cancellation/barge-in.
- [ ] Use the satellite's supported `ANNOUNCE` / `START_CONVERSATION` capabilities;
  fall back to normal response playback when absent. Do not assume all ESPHome
  or webOS devices behave the same.
- [ ] For genuinely lengthy work, design a durable bounded job lifecycle:
  request/job IDs, idempotency, max runtime, retry policy, expiry, restart recovery,
  and explicit opt-in to a later announcement. Never claim a job started before
  its scheduler confirms acceptance.
- [ ] Evaluate multi-user safety: a deferred result must not interrupt an unrelated
  user or announce private information in an occupied room without policy/consent.

**Gate:** acknowledgements are factually correct, never delay normal commands,
and do not cause duplicate audio or spurious conversations.

### Phase 4 — Optional contextual memory and retrieval (RAG)

Defer until real use demonstrates the need for persistent recall.

- [ ] Define explicit user-approved memory classes (preferences, durable facts,
  and reminders) and a user-visible inspect/correct/delete path; avoid silently
  saving raw household dialogue.
- [ ] Use short recent-turn context + bounded summary + selective retrieved
  documents, not entire historical transcripts in every prompt.
- [ ] Add retrieval over a narrowly approved document corpus, with metadata,
  provenance, access control, TTL/reindexing, and a clear "not found" response.
- [ ] Budget retrieval latency and tokens; cache only public/safe facts at their
  natural freshness horizon, never stale states or sensitive tool responses.
- [ ] Extend the [data register](data-register.md) *before* collecting or persisting
  any new conversation or embedding data; specify retention, consent, deletion,
  backups, and provider exposure.

**Gate:** measurable improvement in multi-turn factual accuracy without a
material latency or privacy regression.

## 5. Evaluation and rollout

**Metrics by route, tool, and satellite:** end-of-speech to first *meaningful audible*
word, first audible *progress* message, final answer p50/p95, tool start and duration,
TTS buffer time, request completion, timeouts, fallback rate, duplicate announcements,
wrong-action rate, tool hallucinations, and user cancellation.

Suggested acceptance criteria (targets, **not** current measurements):
- Fast-path control accuracy and latency must not regress; compare to the same
  real-device baseline and run the existing action safety harness.
- Acknowledgements occur only when work is actually pending, not for commands
  already completed; no fabricated sources or progress state.
- Retrieval has hard latency and output bounds, clear attribution where useful,
  safe failures, and no surprise disclosure to cloud providers.
- A timeout, cancellation, HA restart, satellite disconnect, and tool failure
  produce a controlled and comprehensible outcome.
- Rollout one satellite/tool at a time, with a feature toggle, reversible config,
  and a shadow/dry-run mode for any newly proposed device-control tool.

## 6. Decisions still requiring approval

1. Which external information sources may leave the home via Kiais `fast`?
   The current gateway sends unmatched requests to upstream third-party models.
2. Which first two read-only tools merit a production trial (energy, weather,
   web, calendar, or monitoring)?
3. Do we want acknowledgements only within one active voice turn, or also
   deferred completion announcements? If deferred, what are the privacy rules?
4. Do the target webOS and future ESPHome satellites support the required
   announcement, interruption, and conversation-continuation semantics?
5. Should persistent memory ever store preferences or conversation summaries?
   If yes, agree on retention and deletion before implementing it.

**Out of scope for this PR:** enabling an MCP server, deploying new credentials,
modifying the Kiais gateway, changing a model, activating long-lived recording or
memory, modifying satellite firmware, or changing the live Assist pipeline.
