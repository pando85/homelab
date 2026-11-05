# Whisper decoding settings on the GTX 1060: compute type, beam size, VAD

**Status:** PARTIAL, aborted by the experimenter at 2026-11-05 about 19:17 UTC after the benchmark pod
caused CUDA out-of-memory errors in the live Whisper (see [Incident](#incident-the-live-whisper-was-affected)).
Compute type and beam size were measured; `--vad-clip` and `--vad-endpointing` were **not**.
**Date:** 2026-11-05
**Systems:** node `prusik` (GTX 1060 3 GB, `sm_61`, driver 580.159.03), image
`git.grigri.cloud/agil/wyoming-whisper:3.8.1.1` (wyoming-faster-whisper 3.8.1, faster-whisper 1.2.1,
CTranslate2 4.8.5), model `large-v3-turbo` revision `0a363e91`. Nothing ran on a TV or in Home Assistant.
**Related:** [Whisper image build](../whisper-image-build.md), [small local models](small-local-models.md)
(prompt, GPU budget), [Kivi decider PoC](kivi-decider-poc.md) (the audio set)
**Scripts:** `scripts/voice-assistant/whisper_bench/`

Evidence labels: **documented** (help text, source of the image), **measured** (this run),
**inferred**, **hypothesis**.

## Question

For Spanish home commands on this GPU, which `wyoming_faster_whisper` settings give the best latency
(audio end to transcript) without losing accuracy? Matrix: `--compute-type` int8 vs int8_float32,
`--beam-size` 0 (auto, current) / 1 / 5, then `--vad-clip` off vs on at the best combination.

## Summary

1. **On an undisturbed GPU every configuration is within about 30 ms of the others (measured).**
   p50 audio-end to transcript is 0.85 to 0.88 s and p90 0.88 to 0.93 s for all five completed
   configs. That matches the production p50 of about 0.91 s.
2. **`--beam-size 0` is exactly `5` (documented and measured):** `__main__.py` maps `<= 0` to 5 on x86.
   The two runs gave identical text on 480 of 480 clips.
3. **`int8` and `int8_float32` gave identical text on 480 of 480 clips and the same latency (+3 ms,
   measured).** Hypothesis: on `sm_61`, which has no fp16, CTranslate2 resolves `int8` to int8 weights
   with float32 activations, i.e. the same compute as `int8_float32`. Not verified in the CT2 source.
4. **`--beam-size 1` is the only setting that differs: 26 ms faster** at p50 (paired, 95 % CI 21 to 28 ms,
   98 uncontended clips), 3 % of the request time. Accuracy is not worse within the noise of this set:
   command exact match 222/384 vs 219/384 (McNemar p = 0.58), WER 13.6 % vs 14.1 %. 57 of 480 transcripts
   differ from beam 5 (3 clean, 19 at +5 dB, 35 at 0 dB), in both directions.
5. **The big latency effect was not a setting but GPU sharing.** Another client was sending the live
   Whisper a request about every 15 s (about 7 s each) during this run. Clips that overlapped one of those
   requests had p50 about 1.17 s instead of 0.87 s (+0.3 s) and a maximum of 3.9 s. A first, naive reading
   of the whole data set would have credited beam 1 or `int8_float32` with a 0.25 s gain; that is the
   contention (the first config ran while 71 % of its clips overlapped live requests, the later ones 41 %).
6. **Not measured:** `--vad-clip`, `--vad-endpointing`, the float32 out-of-memory confirmation, the
   reversed second round. See [What was not done](#what-was-not-done).

**Recommendation: no change to `values.yaml` for latency.** Current settings are on par with every
alternative on a quiet GPU. `--beam-size 1` is a 26 ms (3 %) optional saving with no measurable accuracy
cost; if wanted, see [Recommendation](#recommendation). The larger lever is whatever else uses the live
Whisper and the shared GPU.

## Incident: the live Whisper was affected

The owner approved a temporary benchmark pod. Its premise was that the live instance is mostly idle and
that a second instance fits next to it (1133 MiB in use of 3072). Both turned out wrong:

- A second large-v3-turbo instance used **1.77 to 1.83 GiB at peak** (1.3 GiB after warm-up, growing
  with decoding). The card peaked at **2967 of 3072 MiB**, leaving about 100 MiB. This is the reason
  [the image build note](../whisper-image-build.md#phase-b-gpu-canary-on-prusik-before-touching-the-real-stt)
  says not to run two large-v3-turbo instances; it was not followed here.
- The live Whisper was **not idle**: from 17:50 UTC (before the pod existed) it handled 30 to 45 requests
  per 10 minutes with a median of about 7 s each, and it did so again at 19:20 UTC after the pod was
  deleted. The origin of that load is not known from the logs (and transcripts were deliberately not read).
- **Impact on the live service (from its log, timestamps and error classes only):** four requests failed:
  two with `RuntimeError: CUDA failed with error out of memory` (18:45:34 and 18:53:51 UTC) and two with
  `cudaErrorInvalidDevice: invalid device ordinal` (18:45:34 and 18:54:02 UTC, an after-effect of the
  first error in the same process). While the pod was running, 67 of 190 live requests took more than 5 s
  (maximum 47 s). Requests succeeded again in about 0.9 s right after the pod was deleted; the pod has
  0 restarts and the GPU is back at 1133 MiB. The live deployment was never scaled, restarted or edited.
- The benchmark's own server also ran out of memory once (`int8f32-b1`, at clip 86 of 480). That config
  and the aborted reversed round are not used.

The run was stopped and the pod deleted as soon as this was seen. **A rerun should not share the card
with the live Whisper while anything else uses it** (see the options at the end).

## Setup

- **Pod:** `pod.yaml` (temporary, label `app=whisper-bench`, `activeDeadlineSeconds` 3 h), on `prusik`,
  `runtimeClassName: nvidia`, `nvidia.com/gpu: 1` (the card is time-sliced 8 ways, so it scheduled).
  The same image by digest, runs as uid 568. It needs no pull secret (the node already caches the image).
- **Model:** the live model volume could not be shared: it is an OpenEBS ZFS dataset and the node refused
  a second mount ("already mounted"). The pod downloaded the same revision (`0a363e91`, same `model.bin`
  blob) from Hugging Face into an emptyDir. The live pod was only read for `ls` of the cache.
- **Server:** the real `python3 -m wyoming_faster_whisper` entrypoint per config, with the live
  `--model large-v3-turbo --language es --device cuda` and the live verbs `--initial-prompt`, plus the
  config's `--compute-type` and `--beam-size`. **`--hass-api` was omitted** (no token in the benchmark),
  so the prompt is the static verbs prefix only; the live server appends Home Assistant names after it,
  which makes the live prompt longer (and possibly slower) than the benchmark's. `--local-files-only`
  on; no `--debug`.
- **Client:** `wyoming_client.py`: `Transcribe(es)`, `AudioStart`, 1024-sample `AudioChunk`s, `AudioStop`,
  wait for `Transcript`. Latency = `AudioStop` written to `Transcript` received (the server only
  transcribes on `AudioStop`, so this is the time HA waits after its own end of speech detection).
  Chunks were sent without real-time pacing. Sequential, one request at a time, 10 unmeasured warm-up
  requests per config (the model loads lazily on the first one), fixed shuffled clip order, identical
  for every config.
- **Audio, synthetic only:** `select_subset.py`, seed 20261105, from the `kivi-fake-v1` audio set of the
  [decider PoC](kivi-decider-poc.md) (Piper voices, TV-content mixes). 160 items stratified by phrasing
  style (16 per style, 8 styles = 128 commands) and kind (10 questions, 6 out of domain, 6 unclear,
  5 unsupported, 5 two-action = 32 non-commands), each in clean, +5 dB and 0 dB = **480 clips**, mean
  3.2 s. No household recording and no Loki transcript was used.
- **Metrics (`metrics.py`, `analyze.py`):** word and character error rate after normalisation (`numbers_es`:
  "veinte" = "20", "20 %" = "veinte por ciento", "21,5" = "veintiuno y medio"; accents and punctuation
  removed), **command exact match** = normalised transcript equals the normalised reference (strict:
  extra words count), over the 128 commands per condition. Latency p50, p90, max. Paired comparison per
  (item, condition) clip: exact McNemar on exact match and a 95 % percentile bootstrap (3000 to 10000
  resamples over clips) on latency differences. GPU memory is whole-card `nvidia-smi`, sampled every
  0.25 s, minus the 1133 MiB of the live pod.
- **Contention control:** the live Whisper logs `Completed request in S second(s)` per request.
  `live_overlap.py` marks a benchmark clip as overlapped when its window (plus 1 s each side) touches a
  live request's window (end minus S, end). Only timestamps and durations are used. The logs were not
  read for text. Because S includes the live server's own waiting, the windows are generous: "quiet"
  clips are really quiet, and many quiet clips were discarded.

## Matrix and what ran

| Config | compute | beam | Result |
| --- | --- | --- | --- |
| `int8-b0` (current) | int8 | 0 (= 5) | 480 clips, 71 % overlapped a live request |
| `int8-b5` | int8 | 5 | 480 clips, 63 % overlapped (A/A control for `b0`) |
| `int8-b1` | int8 | 1 | 480 clips, 42 % overlapped |
| `int8f32-b0` | int8_float32 | 0 | 480 clips, 42 % overlapped |
| `int8f32-b5` | int8_float32 | 5 | 480 clips, 41 % overlapped |
| `int8f32-b1` | int8_float32 | 1 | server out of memory at clip 86; unusable |
| `*-r2` reversed round | | | aborted after 48 clips; discarded |
| vad-clip on, vad-endpointing | | | not run |
| float32 | | | not run (see below) |

## Results

### Accuracy (all 480 clips; text does not depend on contention)

Identical for `int8-b0`, `int8-b5`, `int8f32-b0` and `int8f32-b5` (text identical on 480/480 clips).

| Config | Cond | WER % | CER % | Command exact |
| --- | --- | ---: | ---: | ---: |
| int8 / int8_float32, beam 0 or 5 | clean | 5.21 | 1.88 | 92/128 (71.9 %) |
| | +5 dB | 11.77 | 5.84 | 75/128 (58.6 %) |
| | 0 dB | 25.23 | 15.40 | 52/128 (40.6 %) |
| | all | 14.07 | 7.71 | 219/384 (57.0 %) |
| int8, beam 1 | clean | 5.13 | 1.93 | 92/128 (71.9 %) |
| | +5 dB | 10.01 | 4.92 | 79/128 (61.7 %) |
| | 0 dB | 25.74 | 15.96 | 51/128 (39.8 %) |
| | all | 13.62 | 7.61 | 222/384 (57.8 %) |

Beam 1 against beam 5, paired exact match on commands: 8 fixed, 5 broken (all), p = 0.58; per condition
0/0 (clean), 5/1 (+5 dB, p = 0.22), 3/4 (0 dB, p = 1.0). With about 380 command pairs from 128
items (each in three conditions, so not independent) the set can only exclude a loss of several points.

### Latency on uncontended clips only (no live request within 1 s)

Subsets differ per config (138 to 282 clips), so the p50 and p90 are close but not paired; the paired
test below is the comparison. p90 is tight (0.88 to 0.93 s) because the decoder is short for commands.

| Config | n | p50 s | p90 s | max s |
| --- | ---: | ---: | ---: | ---: |
| int8-b0 (current) | 138 | 0.875 | 0.905 | 2.07 |
| int8-b5 | 179 | 0.872 | 0.918 | 3.87 |
| int8-b1 | 276 | 0.849 | 0.876 | 3.51 |
| int8f32-b0 | 277 | 0.877 | 0.907 | 0.98 |
| int8f32-b5 | 282 | 0.875 | 0.912 | 2.09 |

Paired against `int8-b0`, uncontended in both (difference = other minus base, p50 with 95 % CI;
`n` clips): `int8-b5` +0.003 s (n = 49, identical text), `int8f32-b0` +0.003 s (n = 78),
`int8f32-b5` +0.003 s (n = 84), **`int8-b1` -0.026 s [-0.028, -0.021] (n = 98)**. Against `int8-b1`
as the base the others are +0.022 to +0.027 s slower (n = 96 to 170).

### Latency including contention (all 480 clips; not a property of the settings)

| Config | p50 s | p90 s | max s | share of clips overlapping a live request |
| --- | ---: | ---: | ---: | ---: |
| int8-b0 | 1.129 | 1.455 | 2.14 | 71 % |
| int8-b5 | 0.930 | 1.391 | 3.87 | 63 % |
| int8-b1 | 0.859 | 1.160 | 3.51 | 42 % |
| int8f32-b0 | 0.889 | 1.276 | 2.12 | 42 % |
| int8f32-b5 | 0.885 | 1.300 | 2.09 | 41 % |

The A/A control shows it: `int8-b0` and `int8-b5` have identical decoding, yet differ by 0.20 s at p50
(paired CI -0.24 to -0.04) because of when they ran. Overlapped clips had p50 1.09 to 1.18 s in every
config, uncontended ones 0.85 to 0.88 s.

### GPU memory (whole card, MiB)

| Config | before start | after warm-up | peak | own use at peak |
| --- | ---: | ---: | ---: | ---: |
| int8-b0 | 1133 | 2487 | 2967 | 1834 |
| int8-b5 | 1133 | 2455 | 2967 | 1834 |
| int8-b1 | 1133 | 2487 | 2935 | 1802 |
| int8f32-b0 | 1133 | 2487 | 2903 | 1770 |
| int8f32-b5 | 1133 | 2327 | 2903 | 1770 |

The peaks are not a clean per-config figure: the card is shared with whatever else ran at the time.
The live instance alone is 1133 MiB when idle.

## What was not done

- **`--vad-clip`:** not measured. Documented in the image source (`models.py`, `vad_clip_enabled`): clipping
  is "a wash for faster-whisper, which pads audio to 30s internally regardless". The data above agree in
  spirit: on uncontended clips latency barely depends on clip length (1.3 to 6.3 s, correlation 0.01;
  medians 0.85 s under 2.5 s, 0.87 s for 2.5 to 3.5 s, 0.89 s above 3.5 s), so shortening the audio
  should not help. This is a documented expectation, not a measurement here.
- **`--vad-endpointing`:** not measured. Its help text ("End a command and send its transcript after this
  many seconds of VAD-detected silence (default: wait for audio-stop)") does bear on request latency, but
  only for a client that keeps streaming after the speaker stops; Home Assistant's own end-of-speech
  detection ends the stream. A test needs real-time paced streaming (`wyoming_client.transcribe(realtime=True)`
  and the `limit` field of a plan are ready for it) and a risk review: it can cut commands that contain a
  pause.
- **float32:** not tried; 809 M parameters at 4 bytes is more than the card's 3 GiB, and trying it would
  claim the remaining memory next to the live instance. `int8_float32` and `int8` are the only usable
  types (`ctranslate2.get_supported_compute_types("cuda")` in the pod: float32, int8, int8_float32).
- **Reversed round:** planned to cancel order and drift effects; aborted by the incident.

## Recommendation

1. **Keep the current arguments** (`--compute-type int8`, `--beam-size` unset = 5). On a quiet GPU no
   alternative is meaningfully faster, and none is more accurate. `int8_float32` has no effect.
2. **Optional, low value:** `--beam-size 1` saves about 26 ms per request (3 %) with no measurable accuracy
   loss on this set. Because 57 transcripts changed (more in noise), it should be judged on real usage
   (the usage study) before anyone relies on it. If the owner wants it, in `apps/home-assistant/values.yaml`
   under `whisper.containers.whisper.args`, after `"int8"`:

   ```yaml
            - --compute-type
            - int8
            - --beam-size
            - "1"
   ```

   Not applied. Roll back by removing the two lines (the default is 5).
3. **Do not expect a latency win from other settings.** The measurable latency problem in this run was
   other work on the GPU, about +0.3 s at p50 and seconds at the tail. Find out which client sends the live
   Whisper a request every 15 s, and move it or schedule it.

## Options for a rerun

Needed: `--vad-clip` on/off and optionally `--vad-endpointing`, with a reversed round and no contention.

1. **Quiet window, owner-coordinated:** pause the other client of the live Whisper, then run the pod. The
   pod's peak (about 1.8 GiB) still leaves only about 100 MiB next to the live instance, so a live
   request could still hit an out-of-memory error; run it at a time when nobody uses voice.
2. **Run it on the workstation** (RTX 4060 Ti) with the same scripts and image. Latency would not transfer
   to the GTX 1060 (it would still answer the vad-clip question for accuracy, not for time).
3. **Scale the live Whisper to 0 for the run** with the owner's explicit approval (a short voice outage),
   which is the only way to measure with the card to itself.

## Reproduce

```sh
# on the workstation: stage the subset (synthetic audio of the decider PoC)
python3 scripts/voice-assistant/whisper_bench/select_subset.py --data $POC/data/kivi-fake-v1 --out $STAGE
# pod: kubectl apply -f pod.yaml, download the model (faster_whisper.utils.download_model), kubectl cp the
# scripts, plan_stage1.json and $STAGE, then run inside the pod:
python3 run_bench.py --plan plan_stage1.json --manifest /work/data/manifest.csv --root /work/data --out /work/out1 \
    --live tcp://home-assistant-whisper.home-assistant.svc.cluster.local:10300
# analysis, with the live request intervals for the uncontended view:
kubectl logs deploy/home-assistant-whisper --timestamps | python3 live_overlap.py --extract > live.json
python3 analyze.py --results out1 --items $POC/data/kivi-fake-v1/items.jsonl --base int8-b0 --quiet live.json
python3 -m pytest scripts/voice-assistant/whisper_bench
```

Raw results (synthetic text and timings) stay on the workstation under
`/work/workspaces/whisper-bench-stage/` and are not committed.

## Cleanup (done 2026-11-05)

The pod `whisper-bench` and its emptyDir volumes are deleted; no other objects were created (two earlier
CPU-only help pods were deleted too). The live `home-assistant-whisper` pod is Running with 0 restarts and
the GPU is back at 1133 MiB.
