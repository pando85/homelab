# Home Assistant speaker verification and voice access — implementation plan

- **Status:** Proposed; research and rollout plan only. **Not deployed; no policy change approved.**
- **Date:** 2026-11-03
- **Target:** `pando85/homelab` Home Assistant Assist / Wyoming STT pipelines
- **Related:** [Voice and multiroom deployment PR #4704](https://github.com/pando85/homelab/pull/4704), [webOS voice satellite](https://github.com/pando85/webos-voice-satellite), [satellite networking](../docs/network/webos-voice-satellite.md)

## Goal and non-goals

Allow a configurable set of **enrolled household speakers** to use hands-free Home Assistant
Assist, while ignoring unregistered voices and speech coming from TV/radio playback.
Centralize this policy so kitchen ReSpeaker and webOS TV satellites can share it.

This is **speaker verification for convenience/access gating, not strong authentication**.
It must never be presented as proof of identity suitable for unlocking doors, disabling
alarms, payments, or other high-impact actions.

Initially out of scope:

- Per-person Home Assistant accounts, groups, or arbitrary role-based service permissions.
- Retrofitting identity into every Home Assistant conversation agent or LLM tool call.
- Satellite-side recognition and changes to the webOS / ESPHome microphone firmware.
- Cloud voice services or permanent raw-audio retention.
- Production deployment, live HA UI changes, enrollment, or policy changes in this PR.

## Existing architecture and integration boundary

From `apps/home-assistant/values.yaml` at the time of writing:

- Home Assistant: `ghcr.io/home-operations/home-assistant:2026.10.4`.
- Wyoming Whisper: `pando85/wyoming-whisper:2.4.0`, `large-v3-turbo`,
  Spanish, CUDA / int8, `home-assistant-whisper:10300`.
- Piper `es_ES-sharvard-medium` and `wyoming-openwakeword:2.1.0` (model `alexa`).
- Existing voice/multiroom rollout is tracked in PR #4704; do **not** assume that
  its ReSpeaker pairing, TV satellite, and HA UI commissioning are complete.
- `pando85/webos-voice-satellite` uses the ESPHome native API concept for the
  TV. A central STT proxy avoids firmware-specific recognition logic.

Home Assistant's **Acts as user** selects a configured user for a *pipeline*;
it does not detect the speaker. Exposing entities to Assist is an additional,
independent safety control. An STT proxy can reject speech but cannot reliably
assign a different authenticated HA user or enforce fine-grained ACLs merely by
prefixing a transcript with a speaker name.

### Preferred candidate: Wyoming Voice Match

[Wyoming Voice Match](https://github.com/jxlarrea/wyoming-voice-match)
is an STT-side Wyoming proxy: it buffers an utterance, compares speaker embeddings,
optionally extracts speech matching the enrolled voiceprint, and forwards accepted
audio to an existing Wyoming-compatible STT server. It supports multiple enrolled
speakers and rejects unmatched voices with an empty transcript when
`REQUIRE_SPEAKER_MATCH=true`.

Proposed data path (not yet validated end-to-end):

```text
ReSpeaker / webOS satellites (wake-word audio)
          |
          v
Home Assistant Assist pipeline
          |  Wyoming STT request
          v
Wyoming Voice Match (new internal Service, candidate TCP 10350)
          |  speaker match -> allow / no match -> empty transcript
          v
Existing Wyoming Whisper (TCP 10300, unchanged CUDA and Spanish model)
          |
          v
HA conversation agent -> existing HA action / TTS path
```

The proxy must be selected as STT in an **explicit opt-in HA pipeline**.
Do not switch existing/default Assist pipelines until tested. Keep Whisper as
the original upstream, with no LLM in the allow/deny decision.

**Key caveat:** gating applies only to requests routed through this pipeline.
Other Assist pipelines, text entry, automations, HA REST/WebSocket API, or direct
calls to the underlying STT are not protected by the speaker check. Inventory
those entry points before describing the system as access-controlled.

## Candidate configuration (to validate; not deploy-ready)

| Concern | Proposed behavior |
| --- | --- |
| STT upstream | `UPSTREAM_URI=tcp://home-assistant-whisper:10300`; confirm service DNS first |
| Listen | `LISTEN_URI=tcp://0.0.0.0:10350`, internal-only Kubernetes Service |
| Allow policy | `REQUIRE_SPEAKER_MATCH=true`, no fallthrough for unknown/error |
| Identity tagging | `TAG_SPEAKER=false` in MVP; textual tags are **not** authorization credentials |
| Rejected recordings | `SAVE_REJECTED=false` by default; avoid audio in logs |
| Compute | Start `DEVICE=cpu`; benchmark against GPU sharing before allocating GPU |
| Matching thresholds | Begin with documented defaults **only as test baselines**; calibrate by microphone, distance, noise and voice |
| Enrollment | Explicit opt-in, local voiceprint gallery; samples collected through each class of satellite when possible |
| Persistence | Private Kubernetes PVC for model cache/embeddings; no enrolled data in Git, ConfigMaps or container images |
| Packaging | Review code/license/dependencies; pin immutable version/digest, add Renovate hint and resources/probes after PoC |
| Observability | Counts/latency/error codes, not transcripts, speaker names or audio; redact app logs |
| Network | HA-to-proxy and proxy-to-Whisper within the cluster, no Ingress or public ports |
| Data lifecycle | Document opt-in, revocation, deletion, volume/backups retention and access permissions |

**Validate contract and operational behavior before implementation:** Wyoming discovery
metadata and STT language compatibility; successful proxy-to-upstream streaming;
timeouts and request cancellation; stop/restart/re-enrollment; multiple speaker
matching; Python/model dependencies and image supply chain; resource consumption;
and buffering behavior (including short commands and satellite VAD end-of-stream).
The upstream README describes a verification window of up to 5 s, so a claimed
200–500 ms CPU *inference time* must not be confused with total perceived latency.

On service failure, the restrictive test pipeline should **fail closed**, never
silently switch to the unfiltered STT. A manually selected unrestricted fallback
is an explicit policy change, not an automatic high-availability mechanism.

## Alternatives considered

| Option | Strengths | Why not the MVP | Revisit if |
| --- | --- | --- | --- |
| **Wyoming Voice Match (preferred for a PoC)** | Direct Wyoming STT proxy; retains existing CUDA Whisper; multiple registered voices; optional background-speech extraction | Community project, unknown accuracy in this home, added latency, voice replay/spoofing; not per-person ACL | Validate before accepting it for actual gating |
| [VoiceBM](https://github.com/cybericebyte/VoiceBM) | Biometrics with MQTT identity/decision signals, enrollment, optional passive monitoring and rich HA integrations | More moving parts, Wyoming ONNX ASR/MQTT assumptions and systemd-oriented setup; extra identity path when only allow/deny is required | Trusted identity events or richer per-user workflows become necessary |
| [Wyoming Transcribe](https://github.com/rvsh2/wyoming-transcribe) | Speaker recognition and enrollment in the Wyoming transcription stack | Integrates with whisper.cpp / other backends rather than transparently preserving our existing Wyoming Whisper; transcript speaker labels do not enforce HA roles | STT replacement itself is desired, after a comparative accuracy/latency test |
| Home Assistant `Acts as user` / exposed entities only | Native, lowest complexity, useful least-privilege baseline | Fixed pipeline user, not real speaker recognition; cannot stop unauthorized voices from talking | Retain as **defense in depth**, not as a replacement for verification |
| Speaker recognition in each satellite | Could reject locally before sending audio | Duplicated models, enrollment and resource burden across ReSpeaker/webOS; variable hardware support; larger maintenance surface | Central model fails latency or privacy objectives |
| New custom speaker biometric service | Full control over model, identity/event contract and lifecycle | Reimplements existing open-source features and raises security/maintenance cost | Existing proxy cannot meet the measured requirements |
| PIN/passphrase or confirmation | Better secondary check for some sensitive actions | More friction for ordinary commands; reusable spoken secrets can be overheard | Use as an extra factor for nontrivial actions, never solely voiceprint |

**Decision status:** only the *PoC candidate* is preferred, not an irrevocable
product choice. No alternative is ruled out by verified local benchmarks yet.

## Threat model and policy boundaries

- **False accept:** an unregistered speaker matches an enrolled voice. Test with
  other adults, TV/radio playback and recordings of enrolled voices. A voiceprint
  is vulnerable to replay, synthesis and changes in acoustic conditions.
- **False reject:** enrolled speakers can be refused due to short phrases,
  microphone distance, background sound, illness or multiple simultaneous voices.
- **Bypass:** a different HA Assist pipeline or pre-existing script/action may
  bypass the proxy. Review all relevant conversation paths and entity exposures.
- **Transcript prompt injection:** `TAG_SPEAKER=true` is only text for an agent;
  never make authorization decisions from LLM-interpreted speaker labels.
- **Privacy:** embeddings and enrollment WAVs are biometric material. No public
  repository samples, nonconsensual enrollment, full-audio telemetry or default
  rejected-audio recording. Specify deletion and backup retention before rollout.
- **Availability:** failure, timeout, uninitialized voice gallery, absent model,
  or low-confidence identity rejects the voice request. Report this meaningfully
  to the user where possible, without leaking recognized identity.
- **Sensitive actions:** keep locks, alarm disarming, security controls and
  comparable high-impact scripts unexposed to voice even for accepted speakers.
  Enforce access at the backend/action boundary, not through LLM instructions.

## Execution plan and gates

### Phase 0 — validate dependencies and interfaces (research only)

- [ ] Review upstream code, license, active maintenance, image provenance and
      transitive dependencies; record commit/version to benchmark.
- [ ] Verify compatibility with `pando85/wyoming-whisper:2.4.0` and HA Wyoming
      integration; test language discovery and fail-closed behavior using synthetic
      recordings (no household biometric data in CI).
- [ ] Enumerate current and planned voice pipelines (PR #4704, webOS, ReSpeaker),
      agents, exposed entities, scripts and any ways to bypass the proxy.
- [ ] Define a consent, retention, backup and deletion policy for voiceprints.

### Phase 1 — isolated PoC

- [ ] Run the candidate against a **non-default**, opt-in Assist pipeline; keep
      the existing STT path intact and available for comparison.
- [ ] Enroll consenting adult/test speakers, preferably using samples captured
      via target microphones; store embeddings privately and clean temporary WAVs.
- [ ] Compare with existing Whisper baseline on Spanish commands using the same
      utterances; measure speech-to-text quality, false-accept / false-reject
      counts, CPU/memory and end-to-end/p50/p95 added latency.
- [ ] Test unregistered speech, TV/radio audio, overlapping voices, distance,
      noise, very short commands, recorded/replayed voices, disconnections and
      restarts. Inspect behavior with webOS/ESPHome VAD and the ReSpeaker.
- [ ] Confirm the rejected request triggers **no Home Assistant action** and
      errors never fall back to ungated transcription.

### Phase 2 — production-ready GitOps implementation (separate PR)

- [ ] Add a pinned app-template workload and internal Service under
      `apps/home-assistant/`, sizing it from the PoC measurements, with health
      checks, restart policy and persistent model/enrollment storage.
- [ ] Enforce least privilege (non-root if possible, restricted writable paths),
      no external Ingress, no public exposure, protected biometric storage.
- [ ] Expose anonymous metrics (accepted/rejected/unknown/error, latency and
      health), logs without audio, transcripts or household identities.
- [ ] Add Helm render/lint checks for service routing and no public Ingress, plus
      integration/regression tests for accepted/rejected/failed requests.
- [ ] Document the **manual** HA Wyoming integration and pipeline selection
      steps (HA `.storage` is not managed by Helm), and rollback procedure.
- [ ] Obtain owner approval separately for GitOps rollout and any live HA UI
      changes. Do not auto-enroll from rejected recordings.

### Phase 3 — limited rollout and decision

- [ ] Enable only for low-risk, opt-in voice interactions with a small household
      cohort; keep sensitive action exposures disabled.
- [ ] Observe false accepts/rejects, unsupported speakers, ambient playback
      and latency across each available satellite for a representative period.
- [ ] Document known limitations, manual bypass risks, backup/delete procedure
      and operator runbook. Choose keep/rework/reject based on results.

### Optional Phase 4 — individual roles (not part of this implementation)

Only if needed, add **trusted structured speaker identity** with request/session
correlation and a separate authorization layer that checks permissions before
any service action or tool call. The HA fixed `Acts as user` semantics, identity
propagation and bypass paths must be solved explicitly. MQTT/transcript labels or
LLM instructions alone are not adequate authorization controls. Reassess VoiceBM
or a narrow custom policy gateway at that point.

## Test plan and go / no-go evidence

Use a locally collected, consented and noncommitted dataset; keep samples
grouped by microphone and acoustic scenario, with no personal names in reports.
Use at least 100 legitimate utterances **per speaker per satellite class** and
at least 100 negative/unregistered attempts plus separate TV/replay scenarios
for an initial screen. Larger adversarial validation would still be needed
before any security claim.

| Test | Expected result |
| --- | --- |
| Enrolled speaker, quiet/normal Spanish command | Accepted; comparable STT accuracy to direct Whisper |
| Unregistered speaker | No transcript accepted and **zero downstream HA actions** |
| Television/radio, with/without overlapping authorized voice | TV-only rejected; mixed speech evaluated and no unrelated commands executed |
| Replayed enrolled voice / synthesized voice | Characterize vulnerability; **not claimed secure**, never authorize sensitive actions |
| Short/far-field/noisy phrase | Capture false-reject rate; tune enrollment/thresholds, do not weaken policy silently |
| Missing enrollment, model load failure, proxy timeout, upstream unavailable | Fail closed; no action and actionable operator diagnostics |
| Multiple satellites, wake word and VAD timing | Same policy and predictable behavior on each tested microphone class |
| Alternative STT pipeline or API invocation | Explicitly enumerated as **out of scope/bypass**, mitigated at action policy layer |

Proposed **PoC decision targets**, subject to review (not guarantees): no false
accepts in the initial unregistered/TV test sets; at most 5% false rejects for
typical enrolled-speaker commands; no unintentional HA actions on rejection;
p95 added *post-utterance processing* time within 1 s of the baseline or a
documented usability exception. Report measured sample sizes and confidence;
zero failures in a small trial is not evidence of biometric-grade security.

**Stop / rollback:** if the system cannot fail closed, admits unknown voices,
causes unacceptable false rejection, or changes Whisper behavior, stop rollout.
Return the HA pipeline to the original Whisper provider only with an explicit
understanding that this removes the voice allowlist; keep sensitive actions
disabled. Disable/remove the proxy GitOps workload in a separate change,
and verify private voiceprints/recordings are deleted or retained only under
the agreed policy.

## References (reviewed 2026-11-03)

- [Home Assistant: local voice assistant / Acts as user](https://www.home-assistant.io/voice_control/voice_remote_local_assistant/)
- [Home Assistant: Wyoming integration](https://www.home-assistant.io/integrations/wyoming/)
- [Home Assistant: expose entities to Assist](https://www.home-assistant.io/voice_control/voice_remote_expose_devices)
- [Home Assistant: permission enforcement internals](https://developers.home-assistant.io/docs/auth/permissions/) (check current docs path before using as implementation contract)
- [Wyoming Voice Match](https://github.com/jxlarrea/wyoming-voice-match)
- [VoiceBM](https://github.com/cybericebyte/VoiceBM)
- [Wyoming Transcribe](https://github.com/rvsh2/wyoming-transcribe)
