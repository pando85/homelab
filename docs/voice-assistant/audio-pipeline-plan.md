# Home Assistant audio pipeline plan

Last updated 2026-11-07. **Status: proposal for the owner**; nothing here is deployed or decided until
section 11 is answered. Desk work only: no change to Home Assistant (HA), the cluster or the TVs, no audio read.

This is the server-side half of one plan. The satellite half (front-end, two microphones, diagnostics, raw
replay data, releases) is
[webos-voice-satellite `docs/project/mid-term-plan.md`](https://github.com/pando85/webos-voice-satellite/blob/main/docs/project/mid-term-plan.md).
Both use the same item IDs (`MT-1` to `MT-12`) and one critical path (section 10). Facts about HA's audio chain
come from
[HA-side audio processing](https://github.com/pando85/webos-voice-satellite/blob/main/docs/research/ha-side-audio-processing.md)
(HA 2026.10.4, read from the installed code). Wake word history: [Kivi wake word](experiments/kivi-wake-word.md);
data rules: [data register](data-register.md).

Labels: `documented` (upstream source or docs, with version), `verified` (read from this deployment's code or
config), `observed-on-cluster`, `hypothesis`.

## 1. What HA 2026.10.4 does with satellite audio

| Fact | Consequence | Label |
| --- | --- | --- |
| The ESPHome integration ignores the satellite's `audio_settings` (NS level, auto gain, volume multiplier) since 2024.10; only microVAD runs | No HA-side NS or AGC for any ESPHome satellite, and no setting to turn it on | documented, verified |
| One stream per run: the wake word consumes it, STT continues on the same generator after detection | No per-stage processing in HA; the debug recordings (`00_wake-*`, `01_stt-*`) are exactly what each stage received | documented, verified |
| No pre-roll to STT for satellites (`audio_seconds_to_buffer = 0`; wyoming-openwakeword sends no queued audio) | Anything after the wake word sees only post-detection audio | documented, verified |
| `data2` (`MULTI_CHANNEL_AUDIO`) is used for STT only, and only if the STT service reports `prefers_auto_gain_enabled` and `prefers_noise_reduction_enabled` false; wyoming-faster-whisper 3.8.1 reports true | A "lighter stream for STT" needs a patch in the Whisper image; the wake word always gets channel 0 | documented, verified |
| One pipeline select per satellite chooses its pipeline; a second select is used only when the device reports an on-device wake word | Per-device pipelines work; two wake words on one satellite need on-device detection | observed-on-cluster ([trial deployment](experiments/kivi-wake-word.md#v4-trial-deployment-2026-11-07-c3-a-only)) |
| After an HA restart the pipeline select was restored from an **older** saved state, twice, in both directions | A non-`preferred` select can silently revert; check after every HA restart | observed-on-cluster ([promotion](experiments/kivi-wake-word.md#promotion-to-the-household-wake-word-2026-11-07-1959-utc)) |
| Updating a pipeline aborts its open runs; satellites restart a run within about 15 s | The way to apply a wake word change without touching the TVs | observed-on-cluster |
| `assist_pipeline/pipeline_debug/list` keeps only the last 10 runs per pipeline | Stage timings must be polled often or read from elsewhere | observed-on-cluster (EXP-021 in the satellite repository) |

## 2. Current server settings

| Service | Setting | Value | Label |
| --- | --- | --- | --- |
| Whisper (`agil/wyoming-whisper` 3.8.1.1) | model, compute | `large-v3-turbo`, CUDA `int8` | verified (values.yaml) |
| | language | `es`, fixed | verified |
| | beam size | not set: auto, 5 on x86 | verified; beam 1 saves about 26 ms ([GPU benchmark](experiments/whisper-gpu-benchmark.md)) |
| | initial prompt | static verbs prefix plus live HA names (`--hass-api`, refreshed every 300 s) | verified |
| | VAD filter | `--vad-filter` (Silero) **on since `5690fe717`** (2026-11-07); `--vad-clip` and `--vad-endpointing` off | verified |
| | `no_speech_threshold`, `log_prob_threshold` | not exposed by the server; faster-whisper defaults 0.6 and -1.0 | documented |
| openWakeWord (wyoming-openwakeword 2.1.0) | threshold, trigger level, refractory | 0.5, 1, 2 s, **global for every model** | documented, verified |
| | models | stock `alexa`, plus `kivi` and `oye_kivi` from the private `agil/kivi-models` image; `oye_kivi` is the household wake word since 2026-11-07 | verified ([model registry](model-registry.md)) |
| | per-model operating point | only by calibrating the model file so its chosen raw score maps to 0.5 | documented |

## 3. Verdicts (server side)

Verdicts: **do now**, **after evidence X**, **design only**, **reject**. Brief A is the architecture brief the
owner pasted; numbers 1 to 7 are the coordinator's list. The satellite plan holds the device-side verdicts.

| Item | Verdict | Reason (label) |
| --- | --- | --- |
| A: profiles as YAML in homelab | **Design only** (section 5); implement with the second device class | HA cannot read them (section 1). Consumers are the release, training and evaluation scripts and the docs |
| A: per-stage DSP ownership, no double NS or AGC | **Do now** (section 5 table) | Today HA adds nothing; any NS in front of the wake word is a model change |
| A: verify HA and ESPHome support | **Done** | Section 1 |
| A: dual processed streams | **After evidence** (MT-7 WER); then the satellite's single-stream switch first | `data2` reaches STT only and needs an image patch (section 1) |
| A: continuous wake word stream, no hard VAD gate | **Do now** (keep as is) | wyoming-openwakeword has no gate; a gate before the model changes its input |
| A: Wyoming `kivi-audio` service | **Design only** (section 8) | Build on measured benefit only (MT-8) |
| A: observability per satellite and profile | Health, counts, timings: **do now** (section 7). Recall, WER: **offline** | No ground truth in live runs |
| A: XVF3800 and ESP32 satellites | **Design only** (section 6) | Hardware not here |
| 1: operating point from real takes | **Do now** (MT-1) | Global threshold: ships as a recalibrated file through `wake_release.py` |
| 2: satellite diagnostics to Grafana | **Do now** (MT-3; dashboard here) | HA's Prometheus export already scrapes entity states (`hass` namespace) |
| 3: TV-dialogue false-wake flag | Device side (satellite plan MT-3, MT-9); here only the labels (MT-2) | The tag needs labelled false wakes to be judged |
| 4: NS switch mid-run | Device side, after WER evidence | — |
| 5: AGC for quiet speakers | Offline first (satellite plan MT-4, MT-7) | Changes the model's input |
| 6: one pipeline per device class with its own model variant | **Agree; design only** (section 5) | Supported per satellite; mind the stale select restore |
| 7: Whisper settings | **Measure, do not change** (MT-6) | No WER on real commands yet |

## 4. Work items (HA side)

Order and IDs as in the satellite plan; effort in agent-days (d).

### MT-1. Recalibrated household wake word (in progress)

- **Change:** threshold from the owner's real takes; `wake_release.py init/pick-threshold/calibrate/score/gate/package/release/deploy/promote`, one new image tag. No satellite change.
- **Measure:** the owner's gate on held-out takes per speaker; live false wakes per device per day from MT-2.
- **Rollback:** `wake_release.py demote`, or the previous image pin. **Owner:** takes, operating point, release approval.

### MT-2. False-wake labels and wake counts per device (1 d)

- **Problem:** real false wakes per hour are known only from memory.
- **Change:**
  - An `input_button.kivi_false_wake` helper in `apps/home-assistant/config/integrations/` (as `kivi_take.yaml`) with a button on the voice dashboard; a press means "the last wake on this TV was false".
  - A committed script `scripts/voice-assistant/wake_counts.py` (to write; the count was done once by an ad hoc script): lists run directory names on the `home-assistant-voice-recordings` PVC through `kubectl exec ... find` (names only, no file opened), counts runs that have both `00_wake-*` and `01_stt-*` (a detection; continued-conversation and push-to-talk runs have no wake file, `hypothesis` to check against HA's history on one day) per device per hour, and joins button presses to the nearest earlier detection within 60 s.
- **Measure:** ≥ 95 % of test presses joined to the right run; one day of counts equal to HA's state history.
- **Rollback:** remove the helper; the script only reads names.

### MT-3 (HA part). Diagnostics dashboard (1 d after satellite 0.5.2)

- The satellite's new diagnostic sensors appear as `hass_sensor_*` metrics through HA's Prometheus endpoint (already scraped by `prometheus-long-term`). Add panels to a voice dashboard under `apps/home-assistant/dashboards/`: per device, wakes per hour (MT-2), echo-dominance at wake, input level and clipping at wake, reference open, xruns and drops, daemon CPU.
- **Measure:** panels populated for both TVs for one week. **Rollback:** remove the dashboard JSON.

### MT-3b. Stage timings (1 d)

- A small poller of `assist_pipeline/pipeline_debug/list` and `get` (every 30 s, short-lived token, only event types and timestamps kept) that writes per-run stage durations (wake-word-end to stt-end, stt, intent, tts start) per device to a local file, summarised daily as p50 and p95. Pushing to Prometheus later if useful.
- **Risk:** the debug list holds 10 runs per pipeline, so a busy pipeline can overflow between polls; report coverage.

### MT-6. Whisper on real commands (2 d, after the household sessions)

- **Change:** none at first. Measure on the satellite plan's MT-5 items (prompted text, so no transcription): WER per condition with the current settings, then one change at a time offline (prompt shorter, beam 1, VAD threshold). Measure the VAD filter's effect on noise-only runs: the daily count of `stt-no-text-recognized` errors and of non-empty transcripts after labelled false wakes (counts only, from HA and Loki), two weeks before against two weeks after `5690fe717`.
- **Accept a change:** pooled WER better by ≥ 2 points absolute, no condition worse by > 2 points, latency not worse by > 50 ms p50.
- **Rollback:** revert the values commit.

### MT-8. Decision on the running filter replay (1 d)

- Another session replays real takes and about 33 h of room audio through a second WebRTC NS, RNNoise and speex (`ha_filter_mirror.py`, being written) and scores several wake word models. Its result decides between: nothing; an STT-only denoise stage (section 8); or a wake-side filter, which would need a retrain with the filter in `v2_frontend.py`'s chain.

### MT-11. Profiles and device classes (2 d design, implement on hardware arrival)

- Sections 5 and 6.

## 5. Capability and processing profiles (design)

**Where:** `scripts/voice-assistant/profiles.yaml` in this repository (public; no secrets, no audio facts beyond
what the docs already state). Model files stay in `agil/kivi-models`.

**Who reads it:**

| Consumer | Reads | For |
| --- | --- | --- |
| `wake_release.py` | class → pipeline name, wake word id and version, TVs in the class | creating or updating one pipeline per class through the websocket API, setting each satellite's select, and checking selects after an HA restart (`status`) |
| Training (`v2_frontend.py` and successors) | the class's on-device chain | which front-end emulation the model variant is trained through |
| Evaluation (`kivi_score.py`, `ha_filter_mirror.py`) | class → datasets, emulation, model variant | scoring each class on its own data |
| Docs | everything | the ownership table below, generated or checked |

**What HA cannot consume:** any audio setting (section 1). HA only sees the pipelines and selects that the release
script writes.

**Sketch (not a contract):**

```yaml
classes:
  lg-c3-tv:
    satellites: [c3-a, c3-b]
    capture: {mics: 2, used: ch0, rate: 16000}
    on_device: [hpf-120, aec3-b, webrtc-ns, gain+24]   # owned by webos-voice-satellite
    server: {pipeline: Kiwi, wake_word: oye_kivi, model_version: v4-oye-kivi-2026-11-07}
    emulation: v2_frontend            # chain used to train the variant
```

**Per-stage ownership (who does each step, so nothing runs twice):**

| Stage | LG C3 TV satellite (today) | XVF3800 (planned) | Plain ESP32 + I2S mic (planned) | HA / server |
| --- | --- | --- | --- | --- |
| Echo cancellation | device (AEC3, TV reference) | device (vendor DSP) | none possible without a reference | never (no reference) |
| Beamforming | none (ch0; two mics under evaluation) | device | none (one mic) | never |
| Noise suppression | device (WebRTC NS) | device | none on the device | only in a proxy, only for a class without device NS, only with a model trained through it |
| Gain / AGC | device (fixed +24 dB) | device | none | as NS |
| VAD (end of command) | none | none | none | HA microVAD; Whisper's Silero filter |
| Wake word | server | server, or on-device later | server, or ESPHome `micro_wake_word` | wyoming-openwakeword |

The XVF3800 and ESP32 columns are the plan, not measurements; the XVF3800 processing stages are as described by
its vendor and not verified here (`hypothesis`).

## 6. Onboarding other satellite classes

- **ReSpeaker XVF3800:** the device's DSP (echo cancellation with its own playback as reference, beamforming, NS,
  AGC: vendor-described, not verified) produces a stream whose distribution differs from the TVs'. Steps: record
  real takes and room audio on it (its own data register rows), score the current model, then train a variant
  through an emulation of its chain if recall or false wakes fail the gate. A pipeline of its own (class profile).
  Its echo cancellation only covers what it plays itself, not a TV in the same room (`hypothesis`). Firmware route
  not chosen.
- **Plain ESP32 with an I2S microphone (ESPHome `voice_assistant`):** no NS, AGC or AEC on the device, and HA ignores
  the `noise_suppression_level` and `auto_gain` it sends, so HA receives the raw microphone. This is the one class
  where a server-side NS or AGC stage (section 8) could make sense, with a model variant trained through it. With
  ESPHome `micro_wake_word` on the device, HA's second pipeline select becomes usable for a second wake word.
- **Both:** a profile row, a pipeline, a wake word evaluation on their own data before the household uses them,
  and a check that the select survives an HA restart.

## 7. Observability

| Signal | Source | Where | Label |
| --- | --- | --- | --- |
| Wakes per device per hour, false-wake presses | MT-2 script (names only) and the helper | local summary; later a Prometheus gauge | planned |
| Satellite health at each wake (levels, clipping, echo dominance, reference, drops, CPU) | satellite 0.5.2 diagnostic sensors | HA Prometheus export → Grafana (MT-3) | planned |
| Stage timings p50/p95 per device | MT-3b poller | local summary | planned |
| Whisper latency, VAD lines | Whisper DEBUG lines in Loki (data register row 1) | Grafana Explore | verified (existing) |
| Recall, WER | offline on labelled sets only | experiment notes | — |

No audio, transcript text or names in any metric or dashboard.

## 8. `kivi-audio` Wyoming proxy (design only)

Build only if MT-8 or the satellite plan's MT-7 measures a benefit.

- **Protocol:** a Wyoming server that HA uses as the wake word service, the STT service, or both, and that forwards
  to the real one. Wake: `Describe`/`Info` mirrored from downstream; `Detect(names)`, `AudioStart`, `AudioChunk`,
  `AudioStop` in; `Detection` or `NotDetected` relayed back. STT: `Transcribe`, audio events in, `Transcript` back.
  Each HA run opens its own connection; a wake run can last hours. The events carry no satellite id
  (`hypothesis`, check in the source), so per-device processing can key only on the connection, or the proxy runs
  one instance per class.
- **Pre-roll:** none reaches STT (section 1). An STT-side denoiser starts with no noise estimate on the first
  frames; a combined wake-and-STT proxy cannot share state across the two connections without a device key.
- **Latency budget:** wake side, real-time with at most one 10 ms frame of lookahead and ≤ 30 ms added per chunk;
  STT side, ≤ 100 ms added to Whisper's 0.91 s p50 (the command is buffered anyway). DeepFilterNet's lookahead and
  48 kHz resampling fit the STT side only (`hypothesis`).
- **Placement rule:** STT side first (no wake word impact). Wake side only with a model variant trained through the
  same filter, and only for a class whose device does not already run NS.
- **Rollback:** point the pipeline back at the real service.

## 9. Data handling

All new data follows the [data register](data-register.md) rules (local, owner only, agents see aggregates,
never committed, never sent to a cloud service). Rows to add **when** each collection is approved, not before:

| Planned dataset | Source | Retention (proposal) |
| --- | --- | --- |
| Raw two-channel mic and TV reference of household sessions, with prompted text (satellite plan MT-5) | `tv_dualcap.py` with the satellite stopped | as datasets 8 and 9: until the owner deletes it; deleted on a speaker's request |
| Empty-room TV soak, raw (MT-5) | the same tool, nobody in the room | 90 days |
| False-wake button presses (MT-2) | HA recorder | the recorder's retention |
| Satellite diagnostic sensors (MT-3) | HA Prometheus export | Prometheus retention |
| Stage timings (MT-3b) | pipeline debug API, event types and times only | 90 days |

## 10. Shared critical path

Satellite release 0.5.1 (security fix only) → MT-1, MT-2 and satellite 0.5.2 (MT-3) in weeks 1 to 3 → the raw
replay harness (MT-4) → household sessions (MT-5, owner consent and TV time) → MT-6 and the front-end decision
(MT-7) → only then a front-end change with a retrained model variant and a pipeline per profile (MT-10, MT-11).
MT-8 runs alongside. Diagram and dates: satellite plan, section 4.

## 11. Owner decisions (server side)

1. The operating point of the recalibrated wake word (MT-1).
2. The false-wake button and its use by the household (MT-2).
3. The new data register rows in section 9, each when its collection starts.
4. Whether two model variants and two pipelines may run while the TVs differ (MT-10).
5. Whether a Whisper setting changes on MT-6's evidence, and the Whisper image patch for `data2` (not proposed now).
6. Whether to build the proxy of section 8, after MT-8.

## 12. Not doing, and why

| Not doing | Why |
| --- | --- |
| Enabling HA's speex enhancer (custom integration or core patch) | Unreachable for ESPHome satellites, before both stages, double NS |
| `--vad-endpointing` | Replaces HA's end of command and has no maximum duration |
| `--trigger-level` above 1 | Counts cumulatively per run in 2.1.0; not a consecutive-frames rule |
| A global `--threshold` change for one model | Changes every model; per-model points go in the files |
| A `data2` stream and Whisper image patch now | No WER evidence that lighter processing helps STT |
| Live recall or WER dashboards | No ground truth in live runs |

## Pending (2026-11-07)

Written at the close-out of the 2026-11-07 session; nothing here has been run. Stage numbers are those of the satellite repository's [HA-side audio processing](https://github.com/pando85/webos-voice-satellite/blob/main/docs/research/ha-side-audio-processing.md#6-recommendation-a-staged-plan); the offline replay result is in its section 10.

- **Stage 3, STT-only denoise measurement.** Denoise the audio in front of speech-to-text only, with RNNoise first and WebRTC NS for comparison, and compare it with Whisper's `--vad-filter` (on since `5690fe717`). First measure the VAD filter's own effect on noise-only runs (MT-6: counts of `stt-no-text-recognized` and of non-empty transcripts after labelled false wakes, two weeks before and after). Counts only; no audio or text kept. Needs: the false-wake labels (MT-2) and the owner's go for any HA or Whisper change.
- **Stage 4, VAD gate or wake word proxy, simulated first.** Simulate a voice-activity gate, or a second check, in front of the wake word decision in the offline scorer, on the existing real takes and room audio. It leaves the model's input unchanged. Build a proxy only if the simulation shows fewer false accepts per hour at the same recall (MT-8 decision).
- **Stage 5 is rejected.** Denoising in front of the wake word did not lower the deployed model's false accepts and cost recall in models trained for this front-end ([results](https://github.com/pando85/webos-voice-satellite/blob/main/docs/research/ha-side-audio-processing.md#10-results-of-the-offline-replay-2026-11-07)). Revisit only if the wake word is replaced by a stock model.
- **HA pipeline select caveat.** A Home Assistant restart can restore a TV's pipeline select from an older saved state, in both directions, so check the selects after any restart. Any change under `apps/home-assistant/config/` (a dashboard edit too) restarts Home Assistant through Reloader, about 10 s. Batch such edits and check the selects afterwards.
