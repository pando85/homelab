# Custom wake word "kivi": training an openWakeWord model and measuring recall and false accepts offline

**Status:** DONE offline (model trained and measured on synthetic voices and public recordings); **not deployed**. Real-voice recall is unknown until the owner test below is run.
**Date:** 2026-11-05
**Device alias / model / firmware / webOS / architecture:** none. PC only (x86_64, RTX 4060 Ti 16 GB, 32 threads). No TV, no microphone, no speaker, no change in Home Assistant, the cluster or the GitOps tree.
**Power and playback state:** not applicable
**Related research / requirements / issue:** satellite-side [ADR 0003, server-side wake word for the MVP](https://github.com/pando85/webos-voice-satellite/blob/main/docs/decisions/0003-server-side-wake-word-for-mvp.md); the Phase 2 corpus and TV content set of that repository ([P2 corpus](https://github.com/pando85/webos-voice-satellite/blob/main/docs/experiments/P2-corpus.md)); [conversation agent evaluation](conversation-agent-eval.md) for the same pipeline
**Risk:** none for the household (offline). The model is not deployed; deployment needs the owner's approval of the numbers below and a GitOps change.
**Approval needed / obtained:** none for this offline run. Deployment, the threshold change and the owner's real-voice test need the owner.

Evidence labels: **documented** (read in a source or a model card), **observed-offline** (measured on the PC with synthetic or public recordings, not in the room), **inferred**, **hypothesis**. Nothing here is a measurement with a real voice or the TV microphone.

## Question and falsifiable hypotheses

Can a custom openWakeWord model for the lone Spanish word "kivi" (/ˈki.βi/; the owner chose the single word despite the higher false-wake risk of a two-syllable name) replace `alexa` in the "Kiwi" pipeline (Home Assistant `wake_word.openwakeword`, the cluster's `wyoming-openwakeword` 2.1.0), with recall and false accepts the owner can accept?

- H1: on held-out synthetic Spanish voices (never used in training), recall at the chosen threshold is at least as high as the stock `alexa` model's recall on "alexa" said by the same voices.
- H2: on long Spanish speech and TV-like audio, false accepts per hour at that threshold are at most about 1 per 10 hours (0.1/h), and not worse than `alexa` on the same audio by more than the confidence intervals allow.
- H3: the Spanish near-words ("kiwi", "Vivi", "tivi", "aquí vi", ...) wake the model rarely; the risky ones are named.

## Method

### Training data (synthetic, Spanish phonetics)

- **Positives:** "kivi" rendered by Piper ONNX voices from Spanish phonemes, 60 000 training and 3 000 validation clips. Variants: kˈiβi 45 %, kˈibi 20 %, kˈivi 20 %, kˈiːβi 10 %, kˈiːbi 5 % ("kivi", "quivi" and "kibi" all phonemize to kˈiβi in Spanish espeak, so the phonemes are varied directly), with trailing punctuation for prosody ("", ".", "!", ",", "?"), length scale 0.75 to 1.35, noise scales 0.4 to 1.0, peak -12 to -1 dBFS.
- **Speakers:** training voices are 4 native Spanish speakers (es_ES sharvard M and F, davefx, es_MX ald) plus English multi-speaker models fed with the Spanish phonemes (LibriTTS-R speakers 0-799 of 904, VCTK 0-89 of 109) and Italian, Portuguese and Catalan voices, for speaker variety (about 45 % native Spanish by weight). **Held out** for evaluation only: es_MX claude, es_AR daniela, es_ES carlfm (native Spanish), LibriTTS-R speakers 800-903 and VCTK 90-108. The MLS "low" Spanish voices and ca_ES pau were dropped: they babble or repeat on a single word (median 1.5 to 2.6 s and 2 to 8 energy bursts for "kˈiβi"; observed-offline).
- **Adversarial negatives:** 60 000 + 3 000 clips of 121 Spanish words and phrases close to "kivi" (the /w/ form "kiwi"/"kiui"; one consonant away: "Vivi", "Bibi", "tivi", "Fivi", "chivi", "Kiki", "Kiko", "bici", "civil"; "qui-" words: "quizá", "quieres", "quién", "esquivo", "equívoco"; across word boundaries: "aquí vi", "y vi", "qué vi", "ki") and 30 sentences with them in context. The full list is in `scripts/voice-assistant/wakeword/kivi_data.yaml`.
- **General negatives:** ACAV100M openWakeWord features (the first 8 GiB of the 17.3 GB file, about 990 h of multilingual real-world audio) and 20 h of Spanish read speech from MLS (train shard 0), as feature windows; the 11.3 h openWakeWord false-positive validation set during training.
- **Background and reverberation:** MIT room impulse responses (270), AudioSet balanced-train shard 00 (500 clips of noise and music, weighted 3x) and Spanish speech from MLS train shard 2 (1 500 x 10 s). The official run used openWakeWord's `augment_clips` with these (-10 to +15 dB SNR, EQ, distortion, pitch shift, band-stop, coloured noise, gain); the final model uses the word-level augmentation below.

### Training

1. **Official recipe first (failed).** openWakeWord `train.py` (commit 368c037, 2025-12-30) with `--augment_clips --train_model`: model `dnn`, layer 32, 50 000 steps, batch per step ACAV 1024 + MLS 256 + adversarial 100 + positive 100, `max_negative_weight` 1500, target 0.2 false positives per hour. The model collapsed: 3.5 % recall on its own validation positives and 8 % on held-out voices. Removing the "aquí vi" phrases (they contain the target sound a-**kˈi-βi**) and lowering the weight to 300 did not help (1-6 % validation recall). See [Pitfalls](#pitfalls), item 2, for the cause.
2. **What worked** (`augment_set.py --mode simple`, `train_simple.py`, `fast_eval.py mine`):
   - Own augmentation with the SNR measured on the word itself: clip end-aligned in a 2 s window (0-200 ms jitter), room impulse response with p 0.5, background with p 0.7 at **5 to 30 dB** against the clip's RMS, peak -20 to -1 dBFS. The "aquí vi" family (5 % of the adversarial clips) is excluded from training and kept in the evaluation.
   - The same openWakeWord DNN head (Flatten, Linear, LayerNorm, ReLU, one FCN block, Linear, Sigmoid; exported by openWakeWord's `export_model`), **layer 64**, trained with a plain balanced loop: 6 000 steps, Adam with one-cycle LR 3e-4, batch per step 256 positives, 512 ACAV, 128 MLS Spanish, 64 adversarial and 128 mined hard negatives, a fixed negative weight of 3, and no `auto_train` weight ramp.
   - **Hard-negative mining:** a first model scored 31 h of other Spanish speech (MLS train shard 1), and its 40 718 highest-scoring windows (above 0.05, at most one per 0.4 s) became an extra negative class.
   - Variants trained (11, 2-6 min each on the GPU): with and without adversarials, two augmentation strengths, layer 32/64/128/256, one or two mining rounds. They were ranked by held-out recall at a fixed false-accept count on **MLS dev only** (not the test sets), with server-exact features. The chosen model is variant `s5`.
3. **Export:** `export_tflite.py` (the `onnx_tf` path of `train.py` does not install with current TensorFlow) rebuilds the head as a TensorFlow function and converts it; max |ONNX - TFLite| 1.5e-6. `--calibrate-threshold 0.98` shifts the last bias by -logit(0.98), so the file deployed with the server's default global threshold 0.5 decides exactly like the raw model at 0.98 (checked in the server runtime).

Time on the PC (RTX 4060 Ti, 32 threads, shared with other heavy jobs all evening): clip generation 37 min (131 000 clips, CPU), official augmentation and features 23 min, official training 26 min, own augmentation 3 min, each variant 2-6 min, mining 3 min of GPU embedding. Wall clock including the failed recipe and the evaluation fix: about 6 h.

### Evaluation (offline, server-exact features)

The cluster runs **pyopen-wakeword 1.1.0** inside `wyoming-openwakeword` 2.1.0 (TFLite C API, 80 ms chunks, detection when the probability is above the threshold, trigger level 1, 2 s refractory). Every reported number uses that server's exact features: either pyopen-wakeword itself (`eval_stream.py`, used for `alexa` and to verify) or `fast_eval.py embed --stream-onnx`, which reproduces the server's chunking with a melspectrogram model whose 80 dB clamp is per chunk (see [Pitfalls](#pitfalls), item 1). It matches pyopen-wakeword streaming to 0.002 in the final score (12 clips checked one by one) and to 0.032 in the embeddings (unit test). `fast_report.py` refuses features that are not tagged server-exact. Cross-check of the `alexa` emulation: 55/97 on the Phase 2 positive session at 0.5, against 58/97 in Home Assistant's own digital run of P2 session 2.

- **Recall:** each held-out clip as its own stream (fresh state after the 8 s autofill, 1 s before and after), clean and mixed with the Phase 2 TV content clips (not used in training) at 10 dB and 0 dB SNR. `alexa` is measured on "alexa" said by the same held-out voices with Spanish phonetics (alˈeksa / alˈeɣsa).
- **False accepts per hour:** every detection on audio without the wake word, exact Poisson 95 % intervals. MLS Spanish **dev** (10.3 h) was used only to pick the model and threshold. The test sets are MLS Spanish test (10.3 h, LibriVox readers disjoint from training), VoxPopuli Spanish test (5.2 h, parliament speeches), the Phase 2 TV content set (1.15 h of Spanish and English speech, film, music with and without vocals, and an action scene), the Phase 2 negative and near-miss sessions (0.09 h), and the Phase 2 positive session (0.23 h of Spanish commands after "alexa"; no "kivi" in it). None of the MLS or VoxPopuli transcripts contains "kivi", "kiwi", "quivi" or "kibi".
- **Near-words:** 1 799 held-out-voice clips of the 121 adversarial words and 30 sentences.

## Results (observed-offline, server-exact)

Chosen model: variant `s5` (layer 64, 416 KB TFLite), **recommended threshold 0.98** (picked on MLS dev). In the deployable, calibrated file, 0.98 sits at 0.5. "alexa" is the stock model at the server's default 0.5, as the household uses it today.

**Recall on held-out synthetic voices** (n = 1 800 clips each; 95 % Wilson interval):

| Condition | kivi @ 0.98 | alexa @ 0.5 (same voices) |
| --- | --- | --- |
| Clean | **60.2 %** [57.9, 62.4] | 63.2 % [60.9, 65.4] |
| TV content at 10 dB SNR | 25.1 % [23.1, 27.1] | 38.2 % [36.0, 40.4] |
| TV content at 0 dB SNR | 9.7 % [8.4, 11.1] | 21.3 % [19.5, 23.3] |

Per held-out voice, clean (kivi @ 0.98 / alexa @ 0.5): es_MX claude 58 % / 86 %, es_AR daniela 78 % / 64 %, es_ES carlfm 46 % / 23 %, LibriTTS-R 800-903 68 % / 75 %, VCTK 90-108 43 % / 73 %. Both models depend strongly on the voice; with TV in the background "kivi" loses recall faster than "alexa" (daniela drops to 3.5 % at 10 dB).

**False accepts per hour** (exact Poisson 95 % interval):

| Audio (hours) | kivi @ 0.98 | alexa @ 0.5 |
| --- | --- | --- |
| MLS Spanish test (10.3) | 3 = 0.29/h [0.06, 0.85] | 2 = 0.20/h [0.02, 0.71] |
| VoxPopuli Spanish test (5.2) | 1 = 0.19/h [0.005, 1.08] | 2 = 0.39/h [0.05, 1.41] |
| Phase 2 TV content (1.15) | 0 (< 3.2/h) | 0 (< 3.2/h) |
| Phase 2 negatives + near-misses (0.09) | 0 | 2 ("Alexandria"-type near-misses) |
| Phase 2 commands after "alexa" (0.23) | 0 | not applicable (contains "alexa") |
| **Test total, 16.9 h (16.5 h for alexa)** | **4 = 0.24/h [0.06, 0.61]** | 6 = 0.36/h [0.13, 0.79] |
| MLS dev, 10.3 h (selection only) | 4 = 0.39/h | 3 = 0.29/h |

**Trade-off of the threshold** (same model; test total 16.9 h; clean / 10 dB recall):

| Threshold | Clean recall | 10 dB recall | False accepts on the test sets | Near-word acceptance |
| --- | --- | --- | --- | --- |
| 0.97 | 71.7 % | 31.7 % | 12 = 0.71/h | 5.3 % |
| **0.98** | **60.2 %** | **25.1 %** | **4 = 0.24/h** | **3.7 %** |
| 0.985 | 48.1 % | 19.2 % | 3 = 0.18/h | 2.7 % |
| 0.99 | 28.7 % | 12.7 % | 0 (< 0.18/h) | 1.4 % |

**Near-words** at 0.98 (held-out voices, about 12 clips per word; 66/1 799 = 3.7 % [2.9, 4.6] overall): "Kili" 5/12, "kiri" 5/12, "Pipi" 4/11, "tivi" 4/12, "kiwi" 3/12, "quiwi" 3/12, "chivi" 3/12, "pipí" 3/12, "Kiki" 3/12, "aquí viví" 3/12, "ti vi" 3/12, "qué vi" 3/12, "que vi" 3/12; 1/12 for "la tivi", "Fivi", "Titi", "Gigi", "Didi" and "Fifi"; 0 for most others ("Vivi", "bici", "civil", "quizá", "quieres" ...). "kiwi" said as /kiui/ wakes it about a quarter of the time: a member of the household who says the pipeline's name "Kiwi" will sometimes wake it and sometimes not.

### Hypotheses

- H1 (recall at least alexa's): **not met with noise, about equal when clean** (60 % vs 63 %, intervals overlap; 25 % vs 38 % at 10 dB SNR).
- H2 (about 0.1/h or less, not worse than alexa): **0.24/h at 0.98, the same as alexa within the intervals; 0.1/h is not shown** (zero false accepts at 0.99, but with 29 % recall).
- H3 (near-words): **partly.** The model rejects most of them, but "Kili", "kiri", "Pipi", "tivi" and "kiwi" pass 25-42 % of the time at 0.98.

### Interpretation

- The lone word "kivi" is usable only at about the same recall as "alexa" in quiet. Both are measured on synthetic voices, so the absolute numbers say little about real people. With TV sound in the room it is clearly worse than "alexa", which matters on a TV satellite (the echo canceller is parked).
- The best lever left is to **retrain on server-exact features** (inferred; the model is now trained on batch features that differ from what the server computes, see Pitfalls), followed by real-voice positives from the owner's test. A two-word phrase such as "oye Kivi" should separate much better from running Spanish speech (hypothesis, not measured).

## Pitfalls

Both are recorded with symptom, cause, detection, fix and guard in [lessons learned](../lessons-learned.md).

1. **Melspectrogram clamp: the server's features are not the training features.** openWakeWord's `melspectrogram` model (ONNX and TFLite) ends with `ReduceMax` over its **whole input** and a clamp at max - 80 dB. The server (pyopen-wakeword) calls it on one 1760-sample chunk at a time, so the floor is per 80 ms chunk. Batch extraction (`AudioFeatures.embed_clips`, used for training and by most offline evaluation code) puts the floor at the loudest point of the whole batch. For the same clip the scores differed by up to 0.99 (for example 0.96 in the server and 0.004 in batch). This made the first offline estimates wrong in both directions. **Fix:** `stream_mel.py` rewrites the model so that the max is per batch row, and `fast_eval.py embed --stream-onnx` feeds it the server's 1760-sample chunks with pyopen-wakeword's frame and window bookkeeping. **Verified tolerance:** embeddings within 0.032 of pyopen-wakeword streaming (values reach ±63) and final scores within 0.002 on 12 clips. **Guard:** `test_server_exact_features.py` (streaming vs batched within 0.05; batch features must differ; `fast_report.py` refuses features not tagged `server-exact`).
2. **Augmentation SNR over the whole window buries a short word.** openWakeWord's `augment_clips` mixes background at -10 to +15 dB SNR computed over the whole 2 s padded window. For a 0.45 s word, many positives end up at or below 0 dB on the word itself (the measured word-to-background amplitude ratio was 1.0 to 2.4). With `auto_train`'s negative weight rising to 1500x (doubled when the false-positive target is missed), every run collapsed to 2-9 % recall. **Fix:** augment with the SNR measured against the word (5 to 30 dB), and train with a fixed small negative weight plus mined hard negatives. **Guard:** `augment_set.py --mode simple` documents the choice, and the training history shows validation recall at fixed false accepts (`train_simple.py`).


## Deployment notes (not done; for when the owner approves)

**How the cluster's server loads models (documented, read-only check of `apps/home-assistant/values.yaml` and the `wyoming-openwakeword` 2.1.0 source):** the `openwakeword` controller runs `rhasspy/wyoming-openwakeword:2.1.0` with `--preload-model alexa`, an option that 2.x still accepts but ignores (deprecated). Custom models are read from `--custom-model-dir <DIR>` (repeatable): every `*.tflite` in it becomes a model whose id is the file stem, with a `_vX.Y` suffix removed (`kivi.tflite` and `kivi_v0.1.tflite` both give `kivi`). The model must be TFLite (`pyopen-wakeword` runs it through the TFLite C API); the ONNX file is not used. `--threshold` (default 0.5), `--trigger-level` (1) and `--refractory-seconds` (2) are **global**, for all models of the server.

**Threshold without touching `alexa`:** the recommended operating point is far above 0.5, so the deployable file is exported with `export_tflite.py --calibrate-threshold T`, which shifts the last layer's bias by -logit(T). The output then crosses 0.5 exactly where the raw model crosses T, so the server keeps its default `--threshold 0.5` and `alexa` (and any other pipeline) is unaffected.

**GitOps change that would be needed** (in `apps/home-assistant/`, not made):

1. Get the file into the pod. The calibrated model is 416 KB (555 KB in base64), which fits a ConfigMap (1 MiB limit). Options, for the owner to choose:
   - (a) an init container that downloads `kivi.tflite` from an owner-chosen URL (for example a bucket on the in-cluster MinIO) into an `emptyDir` and checks its SHA-256 before the server starts. No binary in git.
   - (b) commit the binary (for example `apps/home-assistant/files/wakeword/kivi.tflite`) and mount it through a ConfigMap rendered with `binaryData: {kivi.tflite: {{ .Files.Get "files/wakeword/kivi.tflite" | b64enc }}}`. This is the simplest GitOps path, but it needs the owner's licensing decision first: the model inherits CC BY-NC-SA 4.0 terms from the ACAV100M features (the stock openWakeWord models carry the same terms), and some training voices are CC BY-SA.
2. Mount it read-only at `/custom` in the `openwakeword` container (an `advancedMounts` entry under `persistence`, as the `whisper` and `piper` volumes do), and add the argument:

   ```yaml
   args:
     - --preload-model   # deprecated in 2.x, harmless; can be dropped in the same change
     - alexa
     - --custom-model-dir
     - /custom
   ```

3. After Argo CD rolls the pod, reload the Wyoming openWakeWord integration in Home Assistant so that it reads the new model list (`kivi` next to `alexa`, `hey_jarvis`, ...).
4. Pipeline: in the "Kiwi" Assist pipeline set the wake word to `kivi` (stored as `wake_word_id: kivi` in `.storage/assist_pipeline.pipelines`; today `alexa`). This is a Home Assistant change made outside git, so it needs the owner's explicit yes per AGENTS.md. **Rollback:** set `wake_word_id` back to `alexa` (the stock model stays loaded either way), then revert the GitOps commit if wanted. The satellite needs no change: the wake word runs on the server (satellite ADR 0003).

## Real-voice test for the owner (written, not run)

Synthetic recall says nothing reliable about real voices. Before switching the "Kiwi" pipeline:

1. Deploy as above, but set `kivi` only on a **test** pipeline (for example "Kiwi Test", today `alexa_v0.1`), assigned to one TV.
2. Recall: each adult says "Kivi" 20 times at 1 to 3 m, in three blocks: TV off, TV at normal volume with speech, TV with music. Count detections in the Assist debug view (Settings > Voice assistants > ... > Debug). No audio is stored. Optional: 10 times "kiwi" said as /kiui/, to see whether the household's natural pronunciation of the pipeline's name wakes it.
3. False wakes: leave the test pipeline active for 3 to 7 evenings of normal TV, and count pipeline runs that nobody started (Home Assistant logbook or the satellite's wake events). Only counts, no recordings.
4. Suggested acceptance: recall of at least 80 % with the TV off and at least 60 % with the TV on, and at most 1 false wake per evening. If it fails, go to the alternatives below rather than lower the threshold.

## Alternatives if the owner test fails

- Retrain on server-exact features (see Pitfalls, item 1). This is the cheapest next step: the scripts exist, about 1 h on the PC.
- Add real positives: 50 to 100 "Kivi" takes per household member, recorded by the owner. They would stay local and never be committed; recording needs the owner's opt-in and consent.
- A two-word phrase ("oye Kivi", "hola Kivi"): longer, so likely far fewer false wakes from running Spanish (hypothesis).
- Keep `alexa`, which is the rollback anyway.

## Artifacts (private, not committed)

Work directory `/work/workspaces/kivi-wakeword/` (NVMe, outside git). Models are in `final/`:

| File | SHA-256 | Use |
| --- | --- | --- |
| `kivi.tflite` (416 KB) | `5e20c8ee46428a4b6692efe19309d8ca4d02c2cf73679704d891c316297778cb` | deployable, calibrated: 0.5 = raw 0.98 |
| `kivi.onnx` | `b034e656ca6ccbafc802d81dcebfa3c1b2a731318f2e2a9ef3f0d35d97e49e66` | same, ONNX |
| `kivi_uncalibrated.tflite` / `.onnx` | `81e0827f...0511` / `f8d376f8...b11f` | raw outputs, for threshold sweeps |

Results (JSON per threshold, voice and word) are in `results/` and server-exact embeddings in `emb_s/`. About 12 GB of downloads (8 GiB of ACAV100M features, 1.45 GB MLS train, 315 MB MLS dev and test, 994 MB VoxPopuli test, 688 MB AudioSet, 185 MB validation features, about 1 GB of Piper voices).
