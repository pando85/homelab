# Custom wake word "kivi": training an openWakeWord model and measuring recall and false accepts offline

**Status:** DONE offline (model trained and measured on synthetic voices and public recordings). **Deployed on 2026-11-05** as the wake word of the "Kiwi" pipeline (see [Deployment](#deployment)). Real-voice recall is unknown until the owner test below is run. A **v2 candidate** (server-exact features, front-end emulation; [v2](#v2-2026-11-07-candidate-not-deployed)) exists, not deployed. A **v3 candidate** (v2 plus real takes of one speaker and real room negatives; [v3](#v3-2026-11-07-candidate-not-deployed)) passes the owner gate on point estimates for that speaker on c3-a, not deployed. A **v4 candidate** for the new wake phrase **"oye Kivi"** ([v4](#v4-oye-kivi-2026-11-07-candidate-not-deployed)) failed the room false-wake gate and its real recall is untested; the owner approved it as a **trial on c3-a only** on 2026-11-07 and the same evening made it the **household wake word** (pipeline "Kiwi", both TVs; [v4 trial and promotion](#v4-trial-deployment-2026-11-07-c3-a-only)). Rollback: `alexa`.
**Date:** 2026-11-05
**Device alias / model / firmware / webOS / architecture:** none. PC only (x86_64, RTX 4060 Ti 16 GB, 32 threads). No TV, no microphone, no speaker, no change in Home Assistant, the cluster or the GitOps tree.
**Power and playback state:** not applicable
**Related research / requirements / issue:** satellite-side [ADR 0003, server-side wake word for the MVP](https://github.com/pando85/webos-voice-satellite/blob/main/docs/decisions/0003-server-side-wake-word-for-mvp.md); the Phase 2 corpus and TV content set of that repository ([P2 corpus](https://github.com/pando85/webos-voice-satellite/blob/main/docs/experiments/P2-corpus.md)); [conversation agent evaluation](conversation-agent-eval.md) for the same pipeline
**Risk:** the offline run had none. The deployment changes which word wakes both TV satellites; `alexa` stays loaded as the rollback.
**Approval needed / obtained:** none for the offline run. The owner approved the deployment on 2026-11-05 and accepted the weaker noisy-TV recall, with AEC and noise suppression coming. The real-voice test still needs the owner.

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


## Deployment notes (written before the deployment)

The deployment that was made is in [Deployment](#deployment); it uses neither option below but a private image (see [lessons learned](../lessons-learned.md#wake-word-fetching-a-licence-restricted-model-with-an-ad-hoc-token-and-bot-user)).

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

## Deployment

Deployed on 2026-11-05: model `v1-2026-11-05`, `kivi.tflite`, sha256
`5e20c8ee46428a4b6692efe19309d8ca4d02c2cf73679704d891c316297778cb`, served at the server default
threshold 0.5 (= raw 0.98).

**Delivery (no binary in git, no ConfigMap, no extra credential):**

- The private repository `agil/kivi-models` builds an image on a version tag
  (`.forgejo/workflows/image.yml`): busybox pinned by digest with the served file at
  `/models/kivi.tflite`, checked against the version's `SHA256SUMS` inside the build, and the label
  `cloud.grigri.kivi.model.sha256`. It is pushed as a package of the user `agil`, linked to the
  repository:
  `git.grigri.cloud/agil/kivi-models:v1-2026-11-05@sha256:9e51e0c9dba009480f0706da8f90f178380fd72fceeb05e9e148555bb178b40c`.
  An anonymous pull is refused (checked). Packages take the visibility of their owner, not of the
  repository, so this holds only while `agil` stays "limited" (set on 2026-11-05); the image was
  first published under the private organisation `grigri` for that reason, and moved once `agil`
  became limited.
- The init container `01-copy-kivi` of the `openwakeword` controller runs that image and copies the
  file into an `emptyDir` (16Mi), mounted read-only at `/custom` in the server, which runs with
  `--custom-model-dir /custom` (model id = file stem, `kivi`). Its log is the `sha256sum` of the
  copy. The nodes pull the image with their existing containerd credential for `git.grigri.cloud`,
  like the Whisper image, so there is no `imagePullSecret`, token or Vault entry.
- **Version swap:** push the version tag in `agil/kivi-models`, then change the image tag and
  digest in `apps/home-assistant/values.yaml` and update the registry row. The deployment uses the
  `Recreate` strategy, so the wake word is down for a few seconds while the pod restarts; open
  satellite runs reconnect by themselves.
- History: the first rollout (commit `63045e4aa`) downloaded the file with `curl`, a bot token from
  Vault and a PVC cache. It worked but was replaced the same evening; see
  [lessons learned](../lessons-learned.md#wake-word-fetching-a-licence-restricted-model-with-an-ad-hoc-token-and-bot-user).

**Home Assistant change (outside git):** pipeline "Kiwi" (the preferred one), `wake_word_id` from
`alexa` to `kivi` through the websocket command `assist_pipeline/pipeline/update`, every other field
unchanged (checked by a diff of `assist_pipeline/pipeline/list` before and after). The two TV
satellites follow it without a per-satellite change: their pipeline selects are `preferred`, and
their wake word selects are unavailable (`no_wake_word`) because the wake word runs on the server.
Updating the pipeline aborts the open wake word runs (`wake_word_detection_aborted`) and the
satellites start new ones with the new wake word within about 15 s (observed-on-cluster). No Wyoming
config entry reload was needed: `wake_word/info` listed `kivi` right after the pod restart.

**Verification (observed-on-cluster, 2026-11-05, no sound on the TVs):**

- First rollout (`63045e4aa`): Argo CD Synced and Healthy; init log `downloaded, sha256 OK`; the
  server `Ready`; Wyoming `describe` lists `kivi` next to the stock models. The same file (same
  sha256) is served by the image-based rollout. Before that rollout, a temporary pod on the
  openwakeword node pulled the image without any pull secret and copied the file as uid 568 with
  the expected sha256.
- Synthetic check from a temporary in-cluster pod with a Wyoming client (no TV involved), on
  held-out Piper clips (never used in training): "kivi" by three native Spanish held-out voices,
  3 of 3 detected as `kivi`; a held-out "alexa" clip sent to `kivi` not detected, and sent to `alexa`
  detected; a "kivi" clip sent to `alexa` not detected.
- Both satellites `idle`, each with an open Wyoming connection to the server; the server's CPU use
  shows the streams being processed. The Home Assistant log has only `ConnectionRefusedError` from
  the 8 s pod restart window and nothing after it.

**Rollback:**

1. Pipeline: `assist_pipeline/pipeline/update` on "Kiwi" with `wake_word_id: alexa` and the other
   fields unchanged (or Settings > Voice assistants > Kiwi > wake word "alexa"). The open satellite
   runs restart on the update. `alexa` stays loaded, so this alone is a full functional rollback.
2. Optionally remove the init container, the `kivi-model` volume and `--custom-model-dir` in
   GitOps. The image can stay in the registry (private) or be deleted from the `agil` packages.

## Real-voice test for the owner (written, not run)

Synthetic recall says nothing reliable about real voices. Before switching the "Kiwi" pipeline:

1. Deploy as above, but set `kivi` only on a **test** pipeline (for example "Kiwi Test", today `alexa_v0.1`), assigned to one TV.
2. Recall: each adult says "Kivi" 20 times at 1 to 3 m, in three blocks: TV off, TV at normal volume with speech, TV with music. Count detections in the Assist debug view (Settings > Voice assistants > ... > Debug). No audio is stored. Optional: 10 times "kiwi" said as /kiui/, to see whether the household's natural pronunciation of the pipeline's name wakes it.
3. False wakes: leave the test pipeline active for 3 to 7 evenings of normal TV, and count pipeline runs that nobody started (Home Assistant logbook or the satellite's wake events). Only counts, no recordings.
4. Suggested acceptance: recall of at least 80 % with the TV off and at least 60 % with the TV on, and at most 1 false wake per evening. If it fails, go to the alternatives below rather than lower the threshold.

## Real-voice measurement and dataset tools (built 2026-11-07, not yet run on a person)

Tools in [`scripts/voice-assistant/wakeword/`](../../../scripts/voice-assistant/wakeword/README.md) measure `kivi` on real voices and keep every recording as a permanent, private dataset at `/work/workspaces/kivi-wakeword/datasets/real/` (outside git, mode 700, never deleted by the tools; see [data register](../data-register.md) rows 8-11). They were tested with synthetic signals only: no microphone was opened while writing them.

| Tool | Who runs it | What it does |
| --- | --- | --- |
| `kivi_session.py` | the speaker | text-only cues ("say KIVI now", about every 3.5 s, 3.0 to 4.0 s apart) while recording the desk mic and the webcam mic; writes `session.json` |
| `kivi_extract.py` | the owner | copies the satellite's wake files for the session out of the HA pod (`kubectl exec ... cat`), aligns them to the desk mic by GCC-PHAT, cuts clips of the three sources, appends to `manifest.jsonl`. `--button-takes` cuts takes marked by `input_button.kivi_take` presses instead |
| `kivi_score.py` | the owner | kivi (uncalibrated head, raw thresholds) and alexa through the server-exact path over the aligned satellite audio: recall per block and threshold, near-word false accepts, false accepts per hour on the non-cue time; aggregates only |
| `kivi_import_phone.py` | the owner | imports a phone voice memo (takes found by energy, source `phone`) |
| `kivi_negatives.py` | the owner, on request | copies about 30 h of closed wake files of both TVs as real negatives, minus the cues, presses, detection boundaries, every session window and `--exclude-after` / `--exclude-between` windows |
| `kivi_dataset_marks.py` | the owner | records a later split for a session (`splits.jsonl`) or marks an extraction revision superseded (`superseded.jsonl`); append-only, manifest and audio untouched |

Satellites are a parameter (`--device c3-a`, `--device c3-b`, mapped to the HA device-id prefix in `kivi_devices.json`; the source label is `tv-c3a`, `tv-c3b`, ...). Speakers are pseudonymous ids only (`spk01`, `spk02`): the mapping to people is kept by the owner, outside the tools and the repository.

### Owner procedure: one session (about 12 min, c3-a, first speaker)

```sh
cd ~/homelab/scripts/voice-assistant/wakeword
PY=/work/workspaces/kivi-wakeword/.venv/bin/python
# 1. Unmute c3-a in Home Assistant (its mute switch), so the wake stage audio flows.
python3 kivi_session.py --list-devices            # pick the hardware inputs (alsa_input.*), not monitors or Easy Effects
# 2. The session. Cues are text only. Sit at 1.5 m, then 3 m; the TV is off, then on (normal volume, your content).
python3 kivi_session.py --device c3-a --speaker spk01 --desk-mic Samson --webcam-mic C920 --tag volume=normal
# 3. Soon after (Home Assistant deletes files after 90 days). Check first with --dry-run.
$PY kivi_extract.py --session-dir ~/kivi-sessions/<session id> --dry-run
$PY kivi_extract.py --session-dir ~/kivi-sessions/<session id>
# 4. Scores (aggregates only; also saved in the dataset's scores/ directory).
$PY kivi_score.py --session /work/workspaces/kivi-wakeword/datasets/real/<session id> --also-pc
# 5. Mute c3-a again.
```

The protocol: positions 1.5 m and 3 m, each with the TV off and with the TV on, 10 "kivi" per block; then the near-words kiwi, tivi, kili, kiri, Pipi and "aquí vi", 2 each; then 60 s of silence with the speaker present and quiet. `Enter` separates the blocks. For the 4-6 training sessions vary the conditions: `--repeat 2`, `--only kivi-3m-tvon`, `--tag volume=loud`, `--tag speed=fast`, or `--blocks my_blocks.json` (print the default with `--print-protocol`). Check the `align` lines the extractor prints: a source marked LOW CONFIDENCE was not aligned reliably (its clips use the file-time estimate and may be shifted by up to a second). `alexa` is not said in the protocol: its recall on "kivi" is a cross-talk check, and its false accepts per hour are the baseline to compare with.

### Second speaker (consent first)

Nobody's voice is imported before that person has agreed, knowing what is stored (local, never in git, kept until the owner deletes it, used to measure and train Kivi) and that they can ask for it to be deleted. The importers refuse without `--consent-confirmed`.

1. **Phone memo (3 minutes, any quiet room).** Hold the phone at about 1 m. Say "kivi" 30 times, about 3 s apart: ten at normal volume, ten louder and slower, ten quicker and softer. Then say each of kiwi, tivi, kili, kiri, Pipi and "aquí vi" twice, in that order. Say nothing else (no counting, no comments) and leave a clear pause between takes. Send the memo to the owner. Import with `$PY kivi_import_phone.py MEMO --speaker spk02 --split train --consent-confirmed --expect 42 --near-labels kiwi,kiwi,tivi,tivi,kili,kili,kiri,kiri,Pipi,Pipi,"aquí vi","aquí vi"` (`--split` has no default and must be given; `--dry-run` first: it only counts the takes; a mismatch with `--expect` writes nothing).
2. **Button takes on c3-b (about 5 min).** Unmute c3-b. Say "kivi" once, **then** press the **Kivi take** button in Home Assistant (`input_button.kivi_take`), then wait about 5 s; repeat 30 times (10 with the TV off, 20 with it on, from where she normally sits). Then mute c3-b. The owner runs `$PY kivi_extract.py --button-takes --device c3-b --speaker spk02 --since 2026-11-.. --until 2026-11-.. --split test` (times are local unless they end in Z). The extraction looks for the word in the window from max(press - 4 s, the previous take's press + 0.3 s) to press + 1 s (`--button-order say-then-press`, the default; the old `press-then-say` looks 0 to 6 s after the press), and treats presses less than 1.5 s apart as one take (a double press, counted). The report counts presses, takes, double presses, clips cut and takes without a clear speech onset. A take whose speech does not start before the press, or whose speech span exceeds 2.5 s is flagged `needs_review` and, to keep it out of `test`, its split is set to `unassigned` until the owner has listened to it (`--keep-review-in-split` overrides). The press times are the distinct state values of the button, so a state restored after an HA restart is not a press.
3. The button exists only after the Home Assistant configuration change (`apps/home-assistant/config/integrations/kivi_take.yaml`) is deployed and the helper is loaded (restart, or reload input buttons). One button serves every satellite: the extraction names the device, so run one speaker and one satellite per time window.

### False-wake labels (MT-2)

Owner approval 2026-11-11. Two buttons on the voice dashboard, **False wake c3-a** and **False wake c3-b** (`input_button.kivi_false_wake_c3_a`, `input_button.kivi_false_wake_c3_b`, `apps/home-assistant/config/integrations/kivi_false_wake.yaml`). Press the one of the TV that woke by mistake (nobody said "oye Kivi"), preferably within 60 s of the wake. A miss (said it, no wake) is not a press: only false wakes are labelled.

- **What is stored:** the press time and the device only. No audio, no text. Twice: the button state in the recorder (the recorder has no `purge_keep_days` override, so the default **10 days** applies, and its volume is 6 Gi, too small for 90 days) and one line `<UTC time> <device>` in `/config/false_wake_labels.log` on the backed-up config volume, which has no expiry and is the copy to score against the 90-day wake-stage recordings. A state restored after a restart is not a press (the automation skips presses older than 10 s).
- **Scoring rule (proposed, not yet implemented):** a press on device D at time T marks as false the **latest wake of D that started in [T - 60 s, T]** (a wake is a run with both `00_wake-*` and `01_stt-*` in the run directory names, see [the audio plan](../audio-pipeline-plan.md) MT-2). A press with no wake in that window is `unmatched` and reported, never attached to the nearest wake. Two presses for one wake count once (presses less than 1.5 s apart, like `kivi_take`). Wakes with no press are *not* proven true: the false-wake rate is a lower bound, and a true wake is only known through a transcript the owner confirms. Acceptance: at least 95 % of test presses join the right run within 30 s.
- **Not for take marking:** do not press it during `kivi_take` sessions; those presses would be counted as false wakes.

### Split policy (decided 2026-11-07)

- Every record has a `split`: `train`, `dev`, `test` or `train-only` (`unassigned` until the owner decides). The split belongs to the **session**: all takes of a session and all channels of one utterance (satellite, desk mic, webcam) are in the same split. `kivi_import_phone.py` requires `--split` (there is no default).
- **Production model:** the phone memo of spk02 is `train`. Her c3-b button takes are `test` and are never trained on. Sessions of the first speaker on c3-a are train sessions (and the c3-a evaluation sessions are `dev` or `test`, decided by the owner per session).
- **Unseen-speaker check** (an ablation, not the production model): train again with ALL spk02 data excluded (`training_rows(..., exclude_speakers=["spk02"])` in `kivi_real_common.py` does it, and also drops `needs_review` takes) and evaluate on her `test` takes: that run says how the model treats a voice it has never heard. It is the only run in which she is held out; a held-out device (c3-b) contributes no training audio there either.
- **Rule change (owner decision 2026-11-11): the model must work on any device**, so the desk mic, the webcam and later phone and other devices are **evaluation channels on a test-split session** (odd Europe/Madrid date = `test`, even = `train`, for every device). They are reported per device class (`tv`, `pc-desk`, `pc-webcam`, `phone`) and **never pooled with the TV gate**: the satellite mic stays the gate for the deployed TV path, and "other devices" are informational until a gate is set. In a train or unassigned session these channels are still tagged `train-only` (training material, not evaluation); in a `dev` session they carry `dev` and are not evaluation; in a `test` session they carry `test` (so they cannot leak into training) and are evaluation (`eval_channel` true). New manifest rows carry a `device_class` field; old rows are not rewritten, and the scorer and the gate derive `eval_channel` and the class from (source, split) (`is_eval_channel`, `device_class` in `kivi_real_common.py`), so an old row with split `test` and `eval_channel` false still counts. A session without satellite audio is supported (`extract -- --no-tv`): it is recorded with `tv_channel: missing`, has no TV rows and feeds no gate. (Before this date: "PC and webcam audio is train-only material, `eval_channel` false".)
- Negatives (`negatives/`) are split by device and by time; none from a session window or a held-out device may train in the ablation.

### If the model under test is the live wake word

Pre-flight: `kivi_session.py` and `kivi_extract.py` read `.storage/assist_pipeline.pipelines` from the HA pod (read only) and print the preferred pipeline's wake word id; they warn loudly when it is `kivi`. As of 2026-11-07 the "Kiwi" pipeline uses `alexa`, so saying "kivi" does not close the wake file. If it did, the tools cope: a closed file that ends inside a cue window is a server detection (`run_end_in_window`, the clip ends at the file end), the audio after it is missing until the next run starts, and cues that no aligned audio covers are `lost`, shown as their own count and counted as misses in the denominators. Copies are cut by byte range to the session window plus or minus 60 s, through `.part` files with a checked byte count, and listed `01_stt-*.wav` files of those runs are fetched too. Files whose alignment is not trusted are left out of the clips and the scores (flagged in the report; `--include-unaligned` overrides). The scorer streams each raw file from its sample 0 through pyopen-wakeword, the library inside the server (which includes its 8 s startup autofill), and maps the times through the alignment; the 2.1.0 handler (source in the work directory, `src/wyoming-openwakeword`) does not re-chunk: it resets the features and models on `AudioStart` and passes each converted `AudioChunk` straight to `process_streaming`, which is what the scorer does. Only the chunk size Home Assistant sends is unconfirmed (the scorer uses 1280 samples).

### Trade-offs and doubts

The alignment confidence is a peak-to-sidelobe ratio of GCC-PHAT in 15 s windows; unrelated audio can look sharp in one window, so a source is trusted only when several windows agree on one offset line (small residual, plausible drift). The Postgres read (`--button-takes` without `--presses-file`) is a single `SELECT DISTINCT` of the button's state values through `kubectl exec ... psql` with `default_transaction_read_only`. Checked on 2026-11-07 against the cluster with read-only SELECTs: the defaults (`home-assistant-postgres-0`, container `postgres`, user `postgres`, database `home_assistant`) work, `input_button.kivi_take` exists in `states_meta` with state `unknown` (never pressed), and the query returns 0 presses. A real press has not been read yet.

## First real sessions and room audio (2026-11-07, observed on c3-a and c3-b)

Numbers only. Two sessions of one speaker (spk01) on c3-a, scored with the server's own runtime (`kivi_score.py`) on the uncalibrated v1 (`final/`, sha256 `81e0827f...0511`) and v2 (`v2/final/`, sha256 `aeab0360...a627`). Each model is read at its server-0.5 point (v1 raw 0.98, v2 raw 0.95, given as `--calibration`, not assumed) and at raw 0.9. Session 1 is split `train`, session 2 `test` (marks in `splits.jsonl`). One take is excluded per session because nothing was said: session 1 block 4 cue 5 (B4c5), session 2 block 4 cue 7 (B4c7; the desk mic and the webcam show no speech burst there). Scores used extraction revision `(default)` for session 1 (fixed time offset) and `r2` for session 2 (desk-mic sample count, see below). "TV off" in the block names means the TV on and silent.

**Session 1: what was lost and recovered.** The live wake word (alexa) fired at 11:36:53Z and Home Assistant then ran 8 request-only runs (`01_stt-*.wav`, no wake file) until 11:38:57Z. `kivi_extract.py` now also takes request-only runs inside the session window, aligns each file on its own (PSR at least 15, and at least two files agreeing on the listing latency within 0.5 s), and marks the stage (`stt`). Of the 8 runs, 6 aligned (PSR 50 to 83; offsets within 0.64 s of the listed start); 2 did not (PSR 5.4 and 8.7) and stay out. Cue coverage of tv-c3a for 52 cues, counting the cue window of 4 s: before 40 full and 12 none; now **43 full, 8 partial (0.1 to 0.7 of the window), 1 none**. Partial cues have audio but not the whole window (a run ends at the end of speech, gaps of 2 to 5 s between runs); clips are cut only for full windows (3 new cue clips, 2 new segments; 5 new manifest rows).

**Session 2.** 1 wake file spans the whole window, no request-only run and no live detection inside it (the live detection and the follow-up runs are at 12:26Z, after the session). Cue coverage: tv-c3a, pc-mic and webcam-mic 52 of 52 full. Takes present per source: 40 of 40 "kivi" and 12 of 12 near-words on each. Flagged takes (energy heuristics on the clips): 1 empty on pc-mic and webcam-mic (B4c7); 7 "multi-burst" on pc-mic, 12 on tv-c3a and 6 on webcam-mic (with the TV on, its content makes bursts); 12 clipped on webcam-mic (3 or more full-scale samples), 1 on tv-c3a, none on pc-mic; 1 digital-silence clip on pc-mic (the same B4c7 window, the desk mic gates to zero when quiet). **The desk mic fell about 0.5 s behind the clock between about 600 s and 720 s, 0.7 s over the whole session** (its recorded sample count; probably a dropout while the PC was busy, not verified), so a single offset line no longer fits tv-c3a or webcam-mic (residual 108 and 96 ms, all windows confident, PSR 30 and 12). `kivi_extract.py` now accepts a smooth offset curve when at least 80 % of the windows have a sharp peak, the offsets are continuous and the line fails (flagged `piecewise`; the unrelated-audio test still fails), and it maps session times to the desk-mic file by its sample count when the desk mic lost more than 0.3 s. The first extraction (revision `""`) used the old mapping: its 80 desk-mic rows are **superseded** (`superseded.jsonl`; audio kept) and skipped by `read_manifest`, `training_rows` and the scorers; the data is in revision `r2`.

**tv-c3a recall on "kivi" (k/n), counting every cue as a trial (a cue without a full window of audio is a miss), and over full-audio cues only.** Session 1 has 39 cues (B4c5 excluded), session 2 has 39 (B4c7 excluded).

| block | S1 all / full-only | S2 all / full-only | pooled all / full-only |
| --- | --- | --- | --- |
| *v1, server 0.5 (raw 0.98)* | | | |
| 1.5 m, TV silent | 2/10 / 2/3 | 3/10 / 3/10 | 5/20 / 5/13 |
| 1.5 m, TV on | 1/10 / 1/8 | 2/10 / 2/10 | 3/20 / 3/18 |
| 3 m, TV silent | 2/10 / 2/10 | 2/10 / 2/10 | 4/20 / 4/20 |
| 3 m, TV on | 2/9 / 2/9 | 3/9 / 3/9 | 5/18 / 5/18 |
| **all** | **7/39 / 7/30** | **10/39 / 10/39** | **17/78 / 17/69** |
| *v2, server 0.5 (raw 0.95)* | | | |
| 1.5 m, TV silent | 2/10 / 2/3 | 8/10 / 8/10 | 10/20 / 10/13 |
| 1.5 m, TV on | 5/10 / 5/8 | 6/10 / 6/10 | 11/20 / 11/18 |
| 3 m, TV silent | 3/10 / 3/10 | 2/10 / 2/10 | 5/20 / 5/20 |
| 3 m, TV on | 5/9 / 5/9 | 5/9 / 5/9 | 10/18 / 10/18 |
| **all** | **15/39 / 15/30** | **21/39 / 21/39** | **36/78 / 36/69** |
| *v1 at raw 0.9* | | | |
| all | 20/39 / 20/30 | 28/39 / 28/39 | 48/78 / 48/69 |
| *v2 at raw 0.9* | | | |
| all | 24/39 / 24/30 | 29/39 / 29/39 | 53/78 / 53/69 |

Per block at raw 0.9 (pooled, all cues): v1 12/20, 11/20, 11/20, 14/18; v2 11/20, 14/20, 14/20, 14/18. When the spoken word itself (the desk-mic burst, +-0.25 s) has to lie inside the audio instead of the whole window, session 1 is 17/34 (v2, 0.95) and 8/34 (v1, 0.98).

**Near-words on tv-c3a, hits out of 12 per session** (kiwi, tivi, kili, kiri, Pipi, "aquí vi", 2 each; a hit is a score above the threshold in the cue window). At the server-0.5 point: v1 5 (S1), 4 (S2); v2 9 (S1), 6 (S2). At raw 0.9: v1 10 and 8; v2 10 and 8. Desk mic, v1 0.98: 2 and 4; v2 0.95: 1 and 5. Webcam, v1 0.98: 3 and 6; v2 0.95: 6 and 9. For comparison the desk-mic and webcam recall on "kivi" (pooled, 78 cues) at the server-0.5 point is 6 and 13 (v1) and 12 and 34 (v2).

**Room-audio negatives** (wake-stage audio of both TVs, closed files only, minus the first 0.3 s and last 7 s of every file, the 7 s after a follow-up run, 10 s around cues and presses, the whole window of every recorded session of the device +-120 s, and c3-a from 11:50Z on): 33.24 h in 36 pieces, 16.44 h on c3-a (12 pieces) and 16.81 h on c3-b (24 pieces), all recorded before 2026-11-07 (the PVC held more: 21.5 h usable per TV at most). False accepts at each model's server-0.5 point, exact Poisson 95 % intervals, 2 s refractory:

| scope | hours | v1 (raw 0.98) | v2 (raw 0.95) |
| --- | --- | --- | --- |
| c3-a | 16.44 | 1, 0.06/h [0.002, 0.34] | 7, 0.43/h [0.17, 0.88] |
| c3-b | 16.81 | 4, 0.24/h [0.065, 0.61] | 16, 0.95/h [0.54, 1.55] |
| pooled | 33.24 | 5, 0.15/h [0.049, 0.35] | 23, 0.69/h [0.44, 1.04] |

At raw 0.9 pooled: v1 242 (7.3/h), v2 106 (3.2/h). The v2 offline estimate at its server-0.5 point (0.8/h [0.45, 1.4]) is matched by the room audio; v1 is 4.6 times lower at its own point, v2 recalls about twice as much on the TV.

**Not established.** Two speakers or sessions on one TV are far from a test of unseen voices; per-block counts of 8 to 20 trials have wide intervals; the window rule counts a score anywhere in the 4 s window; the "multi-burst" and "clipped" flags are heuristics.

## Decided training recipe (2026-11-07)

Evidence review: kivi-models repo, docs/research/kivi-training-evidence.md. Retraining follows this recipe:

1. **Server-exact features** for training and evaluation (`stream_mel.py`, `fast_eval.py embed --stream-onnx`); no batch features.
2. **Real positives** from household speakers on the TVs, post-front-end (the wake files are exactly what the wake word sees), plus the PC, webcam and phone takes as train-only extras.
3. **Interference augmentation**: TV speech, music and household voices mixed in at 0-30 dB measured on the word, then passed through the front-end emulation. No stationary noise.
4. **Front-end emulation** (120 Hz high-pass, AEC3 and noise suppression, +24 dB) applied to the training audio, so it matches what the satellites send.
5. **Real TV-room negatives** (`kivi_negatives.py`) plus hard-negative mining on them.
6. **Keep** room impulse response and level augmentation.
7. **Evaluation** as the false-reject rate at 0.5 and at 0.1 false accepts per hour, with a DET curve, reported per speaker and per device. The test set is spk02's c3-b button takes (never trained on); the unseen-speaker ablation excludes all spk02 data from training.

## v2 (2026-11-07, candidate, not deployed)

**Status:** candidate model files and an offline comparison with v1. **Not deployed**, no change in
Home Assistant, the cluster or kivi-models. PC only; no TV, no microphone. All numbers are
observed-offline on synthetic voices (Piper); real voices are still unmeasured. Recipe items 1, 3,
4 and 6 were applied; items 2 and 5 (real positives and real TV-room negatives) wait for the
owner's sessions.

### Method

1. **Server-exact features.** Positives, adversarial negatives, mined hard negatives and the MLS
   Spanish speech negatives were recomputed with the server's chunking and per-chunk clamp
   (`v2_augment.py`, built on `fast_eval.StreamFeatures`). Two traps were found on the way, both in
   [lessons learned](../lessons-learned.md): the old stream training window cut up to 100 ms of the
   word in about half of the positives, and v1's evaluation clips are padded with digital silence,
   which decides the comparison by itself.
2. **ACAV100M kept as batch features.** Its audio is not available, so the gap was measured on proxy
   audio computed both ways (400 AudioSet and 400 MLS clips of 10 s at -30 to -1 dBFS peak,
   `v2_acav_check.py`). The median |embedding difference| was 0.27 (values reach ±43; the 99th
   percentile was 10.5, in quiet windows). v1's score changed by 0.005 on average, and its rates
   above 0.5 / 0.9 / 0.98 were 0.46 / 0.03 / 0 % in batch and 0.43 / 0.03 / 0 % in stream. A linear
   probe separated batch from stream windows of the same audio only at AUC 0.61. Removing ACAV made
   validation recall worse (10.8 % against 12.0 to 15.6 % at 4 dev false accepts), and so did
   128 ACAV + 512 MLS per step (5.3 %).
3. **Interference, TV-like only:** 964 AudioSet balanced-train clips (shards 00-03) with a music,
   singing, speech, conversation, TV, radio, laughter, applause or crowd label and none of 40
   stationary-noise labels (`v2_prep_bg.py`), plus 1 500 x 10 s of MLS Spanish speech, about half
   and half. The SNR is measured on the word, and the interference runs through a 2 s lead before
   the window so the noise suppression has adapted. No white, pink or fan noise.
4. **Front-end emulation after mixing:** the satellite's own chain, the `aec_offline` example of
   webos-voice-satellite (daemon file mode; `v2_frontend.py`). It applies the 120 Hz high-pass,
   sonora AEC3 with noise suppression (the shipped `[aec] enabled = true, ns = true`), +24 dB and
   saturating S16, at 166x real time per core, and the 224-sample latency is removed. The echo
   reference is silent, because synthetic mixes have none. AEC3 therefore passes the mix through,
   and the interference stands for sound that the canceller leaves, such as household voices or
   residual echo; NS still acts on it.
5. **Level and floor, calibrated on real satellite audio.** Aggregates only, from the last 2.5 s of
   24 closed wake-stage files that ended in a detection, plus 4 x 20 s from other files, both TVs
   ([data register](../data-register.md), row 12):
   - loudest 0.5 s before the detection: median -21.5 dBFS RMS (IQR -26.5 to -20.6), peak median
     -7.4 dBFS, no clipping;
   - quietest 80 ms frames: -74.6 to -49.9 dBFS (5th to 95th percentile).

   Clips get a word level of N(-23, 5) dBFS (loudest 0.5 s) and a white mic floor of -62 to
   -48 dBFS before the front-end, which NS lowers by 13 dB into the measured range. Long
   evaluation audio is scaled to a 90th-percentile 80 ms RMS of -24 dBFS.
6. **Kept from v1:** RIR at p 0.5; the same 60 000 positives, the 57 103 adversarial clips without
   "aquí vi", and the same held-out voices; `train_simple.py` settings (layer 64, one-cycle, per
   step 256 positives, 512 ACAV, 128 MLS, 64 adversarial and 128 mined, weight 3). One mining round
   ran on 31 h of MLS train shard 1 through the front-end (27 618 windows).
7. **Evaluation:** `fast_report.py` on server-exact features of the held-out voices (clean, P2 TV
   content at 10 and 0 dB), the near-words and v1's false-accept test sets (16.9 h), all through
   the same level, floor and front-end. The threshold was picked on MLS dev through the front-end.
   v1's original zero-padded set is also reported, labelled as such.

### Candidates and selection

18 heads on these features (plus 6 diagnostic heads on the earlier stream features), 1-6 min each
on the GPU. They were ranked only on the training-voice validation positives
(0-30 dB, p 0.8, through the front-end) at 4 / 8 / 16 false accepts on MLS dev through the
front-end (`v2_rank.py`). The held-out voices were not used for the choice.

| Variant | Validation recall at 4 / 8 / 16 dev false accepts |
| --- | --- |
| v1 (reference) | 17.5 / 34.6 / 46.2 % |
| From scratch, 0-30 dB, p 0.8 (3 seeds; layer 128 similar) | 12.0-15.6 / 16.9-24.2 / 26.5-30.6 % |
| From scratch, 5-30 dB, p 0.7 | 17.2 / 26.3 / 35.2 % |
| From scratch, 10-30 dB, p 0.5 (3 seeds) | 18.0-27.4 / 31.3-34.3 / 37.6-40.6 % |
| v1 + v2 training data together | 27.7 / 36.5 / 44.1 % |
| v1 warm start, 0-30 dB data, 2 000 steps at 1e-4 | 21.8 / 33.2 / 46.4 % |
| **v1 warm start, 10-30 dB p 0.5 data, 2 000 steps at 1e-4** (3 seeds; 4 000 steps worse) | **34.2-39.8 / 42.3-46.3 / 47.9-50.4 %** |

**Chosen:** the v1 warm start on 10-30 dB data, seed 1. Its threshold is **0.95**, the highest
threshold with at most 4 false accepts on MLS dev through the front-end (v1 had 4 at its 0.98 on the
raw dev). It differs from the recipe in two ways:

- **SNR 10-30 dB at p 0.5** instead of 0-30 dB at p 0.8. With 0-30 dB, every from-scratch run was
  worse than v1 on validation. Harder interference made the model fire on plain speech.
- **Fine-tuned from v1** (2 000 steps, lr 1e-4) instead of trained from scratch. The best from-scratch
  run reached 27.4 % at 4 dev false accepts, and the warm start 39.8 %.

### Results (observed-offline, server-exact, through the front-end)

Held-out voices, n = 1 800 per condition, 95 % Wilson intervals; false accepts on v1's test sets
(16.9 h), exact Poisson 95 %:

| Model @ threshold | Clean | TV 10 dB | TV 0 dB | Near-words | False accepts (test) | MLS dev |
| --- | --- | --- | --- | --- | --- | --- |
| v1 @ 0.98 (deployed) | 45.7 % [43.4, 48.0] | 24.9 % [22.9, 26.9] | 11.0 % [9.6, 12.5] | 3.2 % [2.5, 4.2] | 8 = 0.47/h [0.20, 0.93] | 5 |
| **v2 @ 0.95** | **77.9 % [75.9, 79.8]** | **32.4 % [30.3, 34.6]** | **14.5 % [13.0, 16.2]** | 9.3 % [8.1, 10.8] | 14 = 0.83/h [0.45, 1.39] | 4 |
| v2 seeds 0 / 2 @ 0.95 | 79.8 / 77.0 % | 32.7 / 31.1 % | 15.1 / 14.1 % | 10.0 / 9.4 % | 1.06 / 0.77/h | 5 / 6 |

The dev-picked thresholds do not give equal test false accepts (v2 0.83/h, v1 0.47/h; the
intervals overlap), so the same models are compared again at matched test false accepts. This is a
DET comparison only; no threshold is chosen on the test sets:

| Test false accepts | v1 (threshold): clean / 10 dB / 0 dB / near-words | v2 (threshold): clean / 10 dB / 0 dB / near-words |
| --- | --- | --- |
| about 0.45/h | 0.98: 45.7 / 24.9 / 11.0 / 3.2 % (0.47/h) | 0.97: 59.4 / 23.0 / 10.0 / 5.7 % (0.41/h) |
| about 0.9-1.1/h | 0.97: 58.1 / 32.3 / 14.9 / 6.1 % (1.06/h) | 0.94: 84.3 / 35.6 / 16.1 / 11.4 % (0.89/h) |
| about 1.4-1.8/h | 0.96: 69.6 / 38.3 / 17.9 / 7.7 % (1.77/h) | 0.93: 87.6 / 38.8 / 18.0 / 13.6 % (1.42/h) |
| 0 | 0.99: 25.1 / 13.9 / 5.7 / 1.2 % | 0.99: 19.6 / 6.8 / 2.9 / 0.9 % |

**Threshold trade-off of v2** (same features; test total 16.9 h):

| Threshold | Clean | TV 10 dB | TV 0 dB | Near-words | False accepts (test) | MLS dev |
| --- | --- | --- | --- | --- | --- | --- |
| 0.9 | 93.6 % | 46.0 % | 21.4 % | 19.8 % | 39 = 2.30/h | 20 |
| 0.93 | 87.6 % | 38.8 % | 18.0 % | 13.6 % | 24 = 1.42/h | 7 |
| 0.94 | 84.3 % | 35.6 % | 16.1 % | 11.4 % | 15 = 0.89/h | 5 |
| **0.95** | **77.9 %** | **32.4 %** | **14.5 %** | **9.3 %** | **14 = 0.83/h** | **4** |
| 0.96 | 71.0 % | 28.1 % | 12.4 % | 7.2 % | 10 = 0.59/h | 3 |
| 0.97 | 59.4 % | 23.0 % | 10.0 % | 5.7 % | 7 = 0.41/h | 1 |
| 0.98 | 43.1 % | 16.2 % | 6.4 % | 3.6 % | 3 = 0.18/h | 0 |
| 0.99 | 19.6 % | 6.8 % | 2.9 % | 0.9 % | 0 (< 0.18/h) | 0 |

**On v1's zero-padded set** (no floor, no front-end; as in v1's results): v1 @ 0.98 scores
60.2 / 25.1 / 9.7 % with 0.24/h, and v2 @ 0.95 scores 46.2 / 28.8 / 12.2 % with 0.53/h. v2 never
saw digital silence. The real satellite never sends it either (its quietest frames are -75 to
-50 dBFS), so this set does not decide between the two.

**Server runtime check:** 12 held-out clips through the front-end (6 clean, 6 with TV at 10 dB) were
streamed through pyopen-wakeword 1.1.0 with the TFLite files (`eval_stream.py probs`). The maximum
scores matched the ONNX path to 0.0005. The calibrated file at 0.5 decided like the raw file at
0.95 on all 12, and the calibrated ONNX on all 2 999 validation windows.

### Interpretation

- At equal false accepts per hour, v2 has a much higher **clean** recall than v1 through the
  emulated front-end: +14 points at about 0.45/h and +26 points at about 1/h. **With TV interference
  it is level with v1**, within about ±3 points and inside the intervals. The interference
  augmentation did not buy noisy-TV recall here.
- v2 accepts **near-words more often** at the same false-accept rate (5.7 % against 3.2 % at about
  0.45/h, 11.4 % against 6.1 % at about 1/h).
- v1 loses 15 points of clean recall when its input goes through the front-end and floor (60.2 % to
  45.7 % at 0.98). The deployed model's real recall is probably closer to the front-end number
  (inferred, not measured on real voices).
- The candidate is worth a real-voice A/B against v1 (the owner procedure above, `kivi_score.py`
  with `v2/final/kivi_uncalibrated.onnx`) before any deployment decision. If deployed at 0.95
  (the calibrated file at 0.5), the offline false-accept estimate is 0.8/h [0.45, 1.4]. That is
  above v1's 0.47/h and far above the 0.1/h goal; 0.97 gives about v1's false-accept rate.
- The cheapest next levers remain the recipe's items 2 and 5 (real positives, real TV-room
  negatives). A two-word phrase is the fallback if near-words and false accepts stay high.

### Artifacts (private, not committed)

In `/work/workspaces/kivi-wakeword/v2/`: `final/` (model files, `SHA256SUMS`, training history),
`results/` (`fast_report.py` JSON for v1, v2 and the other seeds on both evaluation sets, rankings,
tables), `feat/` (server-exact front-end features), `runs/` (the 18 candidates) and the run scripts
(`feats.sh`, `train*.sh`, `report.sh`, `export.sh`).

| File | SHA-256 | Use |
| --- | --- | --- |
| `kivi.tflite` | `131e185b075a7bf0dbcb3ca8396d3f4c80b2304440260fe38b9434ccf7e29414` | calibrated: server 0.5 = raw 0.95 |
| `kivi.onnx` | `0cd09be2622c5853f515f9300279e4e631de605a78e962d0fcbdf6bf6af2951d` | same, ONNX |
| `kivi_uncalibrated.tflite` | `aeab0360089b4805a4833009f2aeee108aa8807bd7803fae573f6ad861a4a627` | raw output, threshold sweeps |
| `kivi_uncalibrated.onnx` | `531cbe78f41fb895de992e6d00ca3590f16bbb634815b2a7087e6c6fc30e5584` | raw output (`kivi_score.py`, `fast_report.py`) |

## v3 (2026-11-07, candidate, not deployed)

**Status:** candidate model files and a one-shot comparison with v1 and v2 on real audio. **Not
deployed**, no change in Home Assistant, the cluster, the TVs or kivi-models. PC only. v3 adds the
real data of the recipe (items 2 and 5): spk01's session-1 takes and the room negatives. Real
numbers below are **observed on c3-a**, synthetic ones **observed-offline**. One speaker only.

### What changed against v2

1. **Real positives** (`v3_real.py takes`): every session-1 cue take of all three sources
   (`training_rows(read_manifest(...))`, split `train` / `train-only`; superseded rows, `needs_review`
   and B4c5 left out).
   - Each take is re-cut from its raw file with 5 s before and 1 s after the word end. The cut uses
     the manifest row's window (`start_s`) and offset (`align_offset_s`), and is checked sample for
     sample against the stored clip (144 of 144 identical).
   - The word end comes from the desk-mic energy burst (`kivi_score.word_span_from_ref`; median
     word 0.54 s). v2's score peaks a median 0.11 s after it (p10 -0.05, p90 0.43 s) on the 27
     tv-c3a takes, inside the training jitter.
   - 141 take-sources kept. 3 tv-c3a takes were dropped: their STT-stage files hold less than 5 s
     before the word. Only full-coverage cues have clips, so no partial cue is used.
   - **Validation hold-out:** 10 of 51 takes (20 %, 2 per block, all sources of a take together,
     seed 2026). They stay out of the final model too; it was **not** retrained on them.
   - Augmentation, 200 copies per take-source, word end -50 to +250 ms before the end of the
     window:
     - tv-c3a (already through the real front end): gain -6 to +6 dB, and TV-like interference at
       10-30 dB on the word with p 0.4. The interference alone goes through the front-end emulation
       and is then added; the take gets no second front-end pass.
     - pc-mic and webcam-mic: RIR with p 0.5; level N(-23, 5) dBFS on the word; interference at
       10-30 dB with p 0.5; the calibrated mic floor (-62 to -48 dBFS; the desk mic gates to digital
       zero); then the front-end emulation.
   - Windows: "kivi" 4 200 tv (21 take-sources) and 12 400 desk (62); near-words 2 000 tv (10) and
     4 000 desk (20).
   - **Ratio:** per step 256 synthetic positives plus 48 tv and 48 desk real windows, so 27 % of the
     positives are real. Per take-source, a real take is drawn about 535 times (tv) and 180 times
     (desk) as often as one synthetic clip.
2. **Near-words** (`kivi_v3_near.yaml`, `gen_clips.py`, Piper training voices):
   - 40 000 training and 3 000 validation clips of 31 words and 10 short sentences, weighted toward
     the measured confusers (kili, kiri, Pipi, tivi and kiwi at 3; "aquí vi" at 2; quizá, Kiki,
     tipi, chivi, Vivi, Fifi, tevé, la tele, and other C-iβi / kˈi-C-i words at 0.5-2).
   - 1 220 clips by the held-out voices for evaluation.
   - The same interference (10-30 dB, p 0.5) and front end as the positives, so the noise level
     cannot predict the label.
   - Real near-words: the session-1 near-word takes of all sources, augmented as above.
3. **Room negatives** (`v3_room.py split`): per TV, ordered by start time and cut by duration
   into three parts:

   | Part | c3-a | c3-b | Use |
   | --- | --- | --- | --- |
   | train (first 50 %) | 8.22 h | 8.40 h | training and mining |
   | sel (next 25 %) | 4.11 h | 4.20 h | threshold selection |
   | test (latest 25 %) | 4.11 h, from 2026-11-06 09:52Z | 4.20 h, from 11:42Z | final evaluation only |

   Server-exact features of the audio as recorded: no front end, level or floor. The train part
   gives 92 723 windows (every 8th frame, 128 per step) and 8 816 hard negatives (v2 score above
   0.02, at most one per 0.4 s; 128 per step). No session window is in the negatives
   (`kivi_negatives.py` already removed them).
4. **Warm start from v2**: 2 000 steps, lr 1e-4, layer 64, the v2 batch (256 synthetic positives,
   512 ACAV, 128 MLS, 64 v1 adversarial, 128 MLS hard; weight 3) plus the above. From scratch
   (6 000 steps) did worse on synthetic voices at the same room false accepts (24.9 % against 35.1 %
   clean) and the same on real takes (21/22 against 20/22).
5. **Selection:** the threshold is the lowest raw threshold with at most 2 false accepts on the
   selection room part (8.38 h scored, 0.24/h). It was never chosen on session 2 or on the test
   part. At that threshold, candidates were compared on:
   - the held-out synthetic voices (clean, 10 dB, 0 dB; both near-word sets);
   - the 10 held-out real takes.

   **Rule, fixed before round 3:** keep candidates with at least 20/22 real held-out "kivi" and
   held-out-voice near-word acceptance no higher than v2's at the same false-accept rate (13.7 %);
   then take the highest mean synthetic recall.

### Candidates (selection room part and validation only)

At each model's own threshold for ≤ 2 false accepts in 8.38 h of the selection part. "Real
held-out" counts the 10 held-out takes over all sources (8 "kivi" takes = 22 take-sources, 2
near-word takes = 6).

| Variant | Raw threshold | Synthetic clean / 10 dB / 0 dB | Near-words, v1 set / v3 set | Real held-out "kivi" | Real held-out near-words |
| --- | --- | --- | --- | --- | --- |
| v1 | 0.979 | 46.6 / 25.3 / 11.1 % | 3.5 / 10.4 % | 5/22 | 3/6 |
| v2 | 0.974 | 54.2 / 20.3 / 8.7 % | 5.0 / 13.7 % | 3/22 | 3/6 |
| real positives, no room, synthetic near-words weight 6 | 0.911 | 24.7 / 12.8 / 5.7 % | 1.8 / 3.7 % | 20/22 | 3/6 |
| + room train windows + room hard negatives (`ws1`) | 0.863 | 35.1 / 18.3 / 7.9 % | 2.8 / 6.5 % | 20/22 | 3/6 |
| same, from scratch | 0.967 | 24.9 / 14.2 / 7.1 % | 1.6 / 2.9 % | 21/22 | 2/6 |
| room, near weight 6, no real positives | 0.865 | 32.0 / 14.7 / 5.8 % | 2.3 / 5.2 % | 2/22 | 1/6 |
| room, real, no synthetic or real near-words | 0.962 | 75.3 / 31.8 / 14.3 % | 10.1 / 29.2 % | 22/22 | 5/6 |
| room, real, synthetic near weight 10 | 0.854 | 26.8 / 13.6 / 5.3 % | 1.6 / 3.0 % | 20/22 | 2/6 |
| room, real, synthetic near weight 3 (3 seeds) | 0.894-0.920 | 36.1-43.6 / 17.0-21.5 / 7.5-9.3 % | 3.4-4.3 / 7.5-10.1 % | 20/22 | 3-4/6 |
| room, real, synthetic near weight 1, real near weight 15 | 0.918 | 55.6 / 23.2 / 9.7 % | 5.1 / 14.4 % | 21/22 | 3/6 |
| **room, real, synthetic near weight 3, real near weight 15 (`r3_nw3_rn15`, chosen)** | **0.8653** | **44.0 / 21.1 / 8.7 %** | **3.3 / 8.8 %** | **22/22** | **3/6** |
| same, layer 128 from scratch | 0.986 | 32.3 / 15.2 / 7.2 % | 2.1 / 5.2 % | 22/22 | 3/6 |

Findings (observed-offline, selection data only):

- Real positives are what moves the real takes: 2-5/22 without them, 20-22/22 with them.
- The room negatives do not cost synthetic recall. With the rest fixed, clean recall was 24.7 %
  without room data, 24.9 % with the room windows, 29.7 % with the room hard negatives and 35.1 %
  with both.
- The synthetic near-words cost the synthetic voices' recall: 75 % clean without them, 44 % at
  weight 3, 35 % at weight 6, 27 % at weight 10. Real near-word discrimination barely changed
  (3/6 held out at every weight).
- Seeds of one configuration differ by up to 7.5 points of clean recall, mostly through the
  threshold, which rests on the 3rd-highest room peak.
- **Choice:** by the rule, weight 1 / real 15 fails the near-word bound (14.4 %). Synthetic near
  weight 2 (44.1 / 21.6 / 9.1 %), weight 3 (43.6 / 21.5 / 9.3 %) and weight 3 with real
  near-words at 15 (44.0 / 21.1 / 8.7 %) are equal within the seed spread. The tie went to the
  last: 22/22 real held-out and the lowest near-word acceptance.

### Deviations from the brief, with the evidence

- **"kibi" is not a near-word.** Spanish espeak gives `kˈiβi` for "kivi", "quivi" and "kibi" (checked
  2026-11-07; also noted in `kivi_data.yaml`), and the positives use `kˈibi` on purpose. "TV" is read
  as letters by espeak (`tˌeˈuβe`), so "tevé" and "la tele" stand for it.
- **The synthetic near-word weight is 3, not "high".** At weight 6 or 10 the held-out synthetic
  voices lost 9 to 17 more points of clean recall for no gain on the real near-words. The real
  near-words carry the high weight instead (15, 64 + 64 per step).
- **tv-c3a interference is added after the front end** (the interference alone goes through the
  emulation) rather than passing the take through a second front end.
- **The final model is the validated one.** It was not retrained with the 20 % hold-out.
- **Synthetic held-out voices were used for selection,** as instructed, so the synthetic numbers
  below are not independent of the choice.

### Final evaluation (one shot)

Run once, after the choice (`v3/final.sh`):

- Session 2 through `kivi_score.py`: pyopen-wakeword with the uncalibrated TFLite files, B4c7
  excluded.
- The test room part through the same runtime (`v3_room.py score`, pieces of at most 15 min, each
  streamed from its first sample).
- The synthetic sets through the server-exact ONNX path.

Each model is at its own server-0.5 point: v1 raw 0.98, v2 raw 0.95, v3 raw 0.8653. Intervals:
Wilson 95 % for recall, exact Poisson 95 % for false accepts.

| | v1 (0.98) | v2 (0.95) | **v3 (0.8653)** |
| --- | --- | --- | --- |
| Session 2, tv-c3a, 1.5 m TV silent | 3/10 | 8/10 | **10/10** |
| 1.5 m TV on | 2/10 | 6/10 | **9/10** |
| 3 m TV silent | 2/10 | 2/10 | **7/10** |
| 3 m TV on | 3/9 | 5/9 | **7/9** |
| TV silent (pooled) | 5/20, 25 % [11, 47] | 10/20, 50 % [30, 70] | **17/20, 85 % [64, 95]** |
| TV on (pooled) | 5/19, 26 % [12, 49] | 11/19, 58 % [36, 77] | **16/19, 84 % [62, 94]** |
| All | 10/39, 26 % [15, 41] | 21/39, 54 % [39, 68] | **33/39, 85 % [70, 93]** |
| Session 2 near-word hits (of 12): tv-c3a / pc-mic / webcam-mic | 4 / 4 / 6 | 6 / 5 / 9 | **7 / 2 / 4** |
| tv-c3a near-word hits by word (2 each): kili, kiri, kiwi, Pipi, tivi, "aquí vi" | 0, 2, 1, 1, 0, 0 | 2, 2, 1, 1, 0, 0 | 1, 2, 1, 1, 2, 0 |
| Session 2 "kivi" recall, pc-mic / webcam-mic (of 39) | 2 / 3 | 5 / 12 | 28 / 34 |
| Room test part, c3-a (4.11 h) | 2 = 0.49/h [0.06, 1.76] | 10 = 2.43/h [1.17, 4.48] | **1 = 0.24/h [0.006, 1.36]** |
| Room test part, c3-b (4.20 h) | 0 (< 0.88/h) | 8 = 1.90/h [0.82, 3.75] | **1 = 0.24/h [0.006, 1.33]** |
| Room test part, pooled (8.31 h) | 2 = 0.24/h [0.03, 0.87] | 18 = 2.17/h [1.28, 3.42] | **2 = 0.24/h [0.03, 0.87]** |
| Synthetic held-out voices, clean (n = 1 800) | 45.7 % [43.4, 48.0] | 77.9 % [75.9, 79.8] | 44.0 % [41.7, 46.3] |
| TV content at 10 dB | 24.9 % [22.9, 26.9] | 32.4 % [30.3, 34.6] | 21.1 % [19.3, 23.1] |
| TV content at 0 dB | 11.0 % [9.6, 12.5] | 14.5 % [13.0, 16.2] | 8.7 % [7.5, 10.1] |
| Near-words, held-out voices, v1 set (1 799) / v3 set (1 220) | 3.2 / 9.9 % | 9.3 / 27.1 % | 3.3 / 8.8 % |

The v1 and v2 session-2 counts equal the earlier `kivi_score.py` runs (10/39 and 21/39 with the same
blocks), so the path is unchanged. v2 has far more false accepts on the test part (2.17/h) than over
all 33 h (0.69/h): the test part is daytime on 2026-11-06.

**Correction (2026-11-07, found during v5):** the v1 and v2 room-test figures in this table are doubled.
`v3/final.sh` passed 0.95 and 0.98 twice in `--thresholds`, and `v3_room.py score` counted each repetition
(fixed: the thresholds are now made unique). The rerun in the v5 section gives v1 at 0.98: c3-a 1, c3-b 0,
pooled **1 = 0.12/h [0.003, 0.67]**; v2 at 0.95: c3-a 5, c3-b 4, pooled **9 = 1.08/h [0.49, 2.06]**. The v3
figures (0.8653, not repeated) and every other threshold are unchanged (re-checked: identical counts).

**v3 trade-off on real audio** (tv-c3a session 2 against the room test part, pooled 8.31 h; c3-a /
c3-b counts in brackets). A DET-style table, not a threshold choice:

| Raw threshold | Recall (of 39) | TV silent (of 20) | TV on (of 19) | Near-word hits (of 12) | Room false accepts | Per hour [95 %] |
| --- | --- | --- | --- | --- | --- | --- |
| 0.5 | 36 | 19 | 17 | 8 | 87 (46 / 41) | 10.5 [8.4, 12.9] |
| 0.7 | 35 | 19 | 16 | 8 | 20 (9 / 11) | 2.41 [1.47, 3.72] |
| 0.8 | 34 | 18 | 16 | 7 | 11 (6 / 5) | 1.32 [0.66, 2.37] |
| 0.85 | 34 | 18 | 16 | 7 | 3 (2 / 1) | 0.36 [0.07, 1.06] |
| **0.8653** | **33** | **17** | **16** | **7** | **2 (1 / 1)** | **0.24 [0.03, 0.87]** |
| 0.9 | 32 | 17 | 15 | 6 | 1 (0 / 1) | 0.12 [0.003, 0.67] |
| 0.93 | 28 | 16 | 12 | 5 | 0 | 0 (< 0.44) |
| 0.95 | 27 | 16 | 11 | 4 | 0 | 0 (< 0.44) |
| 0.97 | 21 | 14 | 7 | 2 | 0 | 0 (< 0.44) |
| 0.98 | 15 | 11 | 4 | 1 | 0 | 0 (< 0.44) |

For comparison at matched room false accepts: v1 needs 0.98 for 2 (10/39), and v2 0.97 for 2
(14/39).

**Server runtime check:** the 12 v2 check clips (held-out voices through the front end, 6 clean,
6 with TV at 10 dB) went through pyopen-wakeword 1.1.0 with the TFLite files. The maximum scores
matched the ONNX path to 0.001. The calibrated file at 0.5 decided like the raw file at 0.8653 on
all 12.

### Gate verdict

| Gate | v3 | Verdict |
| --- | --- | --- |
| Recall with the TV silent, at least 80 % | 17/20 = 85 % [64, 95] | met on the point estimate |
| Recall with the TV on, at least 60 % | 16/19 = 84 % [62, 94] | met on the point estimate |
| At most 1 false wake per evening (about 0.25/h) | 2 in 8.31 h = 0.24/h [0.03, 0.87] | met on the point estimate |

**v3 passes the gate on point estimates. The intervals do not prove it:** the TV-silent lower bound
is 64 %, and the false-accept upper bound is 0.87/h, about 3.5 per 4 h evening. The weakest block
is still 3 m with the TV silent (7/10). Near-words on the TV channel did not improve (7/12; "tivi"
2/2).

**What this test is and is not:**

- It covers **one speaker**, spk01, who is also the training speaker.
- Session 2 is **one other recording** of the same person, the same TV (c3-a) and the same day.
- On held-out synthetic voices, v3 is level with v1 and well below v2 (44 % against 78 % clean). It
  has become a better detector of spk01 on this TV, not of voices in general. Other household
  voices are likely to do worse than spk01 (inferred).
- spk02's c3-b button takes (test split, never trained on) are the next decisive measurement, with
  the unseen-speaker ablation of the split policy.

**Data caveat:** the room negatives (2026-11-05 17:18Z to 2026-11-06 20:37Z) were mostly recorded
while `kivi` v1 was the live wake word (deployed about 2026-11-05 20:16Z).

- A real "kivi" that v1 missed would sit unlabelled in the negatives. Without listening this cannot
  be checked.
- A test-part "false accept" could then be a correct detection, so the reported rate is
  conservative, not optimistic.
- The ablation above showed no recall cost from training on the room data.

### Artifacts (private, not committed)

In `/work/workspaces/kivi-wakeword/v3/`: `final/` (model files, `SHA256SUMS`,
`kivi_uncalibrated.calibration.json`, training history), `results/` (aggregates: selection JSON,
session-2 and room-test reports, synthetic reports), `runs/` (the 19 candidates), `near/` (synthetic
near-word clips), `feat/` and `real/` (mode 700, see [data register](../data-register.md) row 13),
and the run scripts (`gen_near.sh`, `feats.sh`, `runs1-3.sh`, `select.sh`, `final.sh`,
`export.sh`). Wall clock about 40 min, including generation (8 min), features (7 min), 19 training
runs (about 30 s each warm, 1 min from scratch) and the final evaluation (6 min).

| File | SHA-256 | Use |
| --- | --- | --- |
| `kivi.tflite` | `9c3ffb58bf641b03132fe67d1f3253529397843c379b30b3ed930b24c9de9a31` | calibrated: server 0.5 = raw 0.8653 |
| `kivi.onnx` | `650be56a7465b29ba3a50b87b5941644e08035281f581b9d739b5f08b7e556ab` | same, ONNX |
| `kivi_uncalibrated.tflite` | `beda360612183fe58f22cf062cdaed6c805105bbf523d17d3b5c32cc71c1ab10` | raw output (`kivi_score.py`, threshold sweeps) |
| `kivi_uncalibrated.onnx` | `0444db525743dee9a8bf07a1058bcabe17d6241b8981955a4c49537a2366be51` | raw output (`fast_report.py`, `v3_select.py`) |

## spk02 c3-b button takes (2026-11-07, observed-offline, test split only)

Consent: relayed by the owner on 2026-11-07 (recorded in dataset 9 of the data register). These takes are `test` and are never trained on. Nothing was listened to or transcribed; the numbers below are counts, levels and scores.

**Presses.** `input_button.kivi_take` has 23 presses since it was created (the helper was loaded at 10:39Z; the recorder holds no earlier press): all between 14:11:54Z and 14:13:54Z on 2026-11-07, none before, none during the c3-a sessions (the two `unknown` states at 10:39Z and 12:27Z are the entity being loaded and restored, not presses). Two pairs are 0.8 s apart (double presses of one take), so there are **21 takes**. Gaps between consecutive takes: 2.2 to 18 s (median 3.5 s). Three clusters, split at gaps over 12 s: A 3 takes, B 3 takes, C 15 takes.

**Coverage.** All 23 presses lie inside one closed c3-b wake file (14:11:21Z to 14:15:11Z, 7.36 MB, 230 s, first press 33 s in): 21 of 21 takes have full audio, none partial, none missing. Speech energy in the 5 s before the press (window start at the previous take's press when that is closer): 20 of 21 takes have RMS 28 to 43 dB and peak 40 to 55 dB above the floor (the floor is -71.6 dBFS, the 20th percentile of 20 ms frames over the press span; peaks -31 to -16 dBFS). One take (the second of cluster A) has RMS 3.5 dB and peak 15 dB above the floor: no speech. The wake file closed at 14:15:11.7Z, 78 s after the last press, with a 1.1 s request-audio file (a server-side wake detection of the live word `alexa`, not related to a take). Offline `alexa` on the same file: peak score 0.43, no detection above 0.5 in the take span. A second wake file started at 14:15:25Z and holds no press.

**The extraction tool and this protocol do not match.** `kivi_extract.py --button-takes` looks for the word 0 to 6 s *after* a press. These takes were spoken *before* the press (the model scores peak a median 1.4 s before it). With presses 3.5 s apart the tool therefore cut the next take's word: 20 clips from 23 presses, 3 presses with no clear onset, 16 clips flagged `needs_review` (set to `unassigned`), 4 clips in `test`, and 6 pairs of clips that cut the same burst twice (median onset 2.1 s after the press). **Do not use those clips**; they are in `datasets/real/button-20261107T141100Z-spk02-tv-c3b/` and have since been marked superseded (revision `""`; audio kept). The extractor was fixed the same day (say-then-press window, default), see the re-extraction below. The scores below do not use them: the stored raw wake file was streamed once through pyopen-wakeword (the server's runtime) and each take's score is the maximum of the model's output between max(press - 4 s, previous press) and press + 1 s (the 1 s is the file-time uncertainty, +-1 s).

**Re-extraction (say-then-press, revision `stp3`, 2026-11-07, observed-offline).** `kivi_extract.py --button-takes` now defaults to the owner's order (say, then press): 23 presses are 21 takes (2 double presses merged). The word is the energy-burst span before the press in max(press - 4 s, previous take's last press + 0.3 s) to press + 1 s. Result: 19 clips cut (median speech onset 2.7 s before the press), 2 takes without a usable burst (no burst shorter than 2.5 s above the file floor inside the window), 11 clean clips and 8 flagged `needs_review` (a span over 2.5 s, or speech that only starts after the press). Earlier tries with a context floor cut 17 and 10 clips (revisions `r2`, `stp1`, `stp2`); all of them and the press-then-say rows are marked superseded, audio kept. Owner decision the same day: these takes are `train` (session mark in `splits.jsonl`), not `test`; `needs_review` takes stay out of training. The one take known to hold no speech (the second of cluster A) still produced a 0.2 s noise burst, so its clip is marked excluded (`excluded.jsonl`, `kivi_dataset_marks.py exclude`); `training_rows` skips excluded rows, leaving 10 clean clips usable for training. Energy only: nothing was listened to.

**Check of the scores with the new extraction record** (`kivi_score.py --button-takes`, 21 takes, the extractor's window, the no-speech take counted as a miss): server-0.5 point v1 5/21 (earlier 6/21), v2 10/21 (earlier 9/21), v3 5/21 (earlier 4/21); raw 0.9 v1 14/21, v2 11/21, v3 2/21 (identical to the earlier figures). Each model differs by one take at its server-0.5 point only. Hypothesis, not tested: the windows differ slightly (the new one starts 0.3 s after the previous take's last press, the earlier one at the previous press), and these takes sit close to the threshold. Without the excluded take (n 20): 5, 10 and 5 hits.

**Recall, uncalibrated models, window before the press** (Wilson 95 % interval; raw threshold in brackets):

| Model | Server-0.5 point | Pooled (n 21) | Cluster A (3) | Cluster B (3) | Cluster C (15) | Raw 0.9, pooled | Median of max scores |
| --- | --- | --- | --- | --- | --- | --- | --- |
| v1 | raw 0.98 | 6/21 = 0.29 [0.14, 0.50] | 1/3 [0.06, 0.79] | 1/3 [0.06, 0.79] | 4/15 [0.11, 0.52] | 14/21 = 0.67 [0.45, 0.83] | 0.94 |
| v2 | raw 0.95 | 9/21 = 0.43 [0.25, 0.64] | 1/3 [0.06, 0.79] | 1/3 [0.06, 0.79] | 7/15 [0.25, 0.70] | 11/21 = 0.52 [0.32, 0.72] | 0.94 |
| v3 | raw 0.8653 | 4/21 = 0.19 [0.08, 0.40] | 1/3 [0.06, 0.79] | 1/3 [0.06, 0.79] | 2/15 [0.04, 0.38] | 2/21 = 0.10 [0.03, 0.29] | 0.71 |

Per cluster at raw 0.9: v1 1/3, 3/3, 10/15; v2 1/3, 2/3, 8/15; v3 0/3, 1/3, 1/15. Sensitivity to the window (pooled, server-0.5 point): from press - 4 s to press + 1 s without the previous-press limit: v1 7/21, v2 10/21, v3 6/21; the tool's after-press window (press - 1 s to press + 7 s, which reaches the next take): v1 8/21, v2 11/21, v3 7/21. The no-speech take counts as a miss in every figure (without it, n is 20). The clusters are not independent samples of the voice (one speaker, one session, 2 minutes), and the intervals are wide. These are the same models that scored higher on the spk01 and synthetic sets; on this speaker the v3 server-0.5 point (chosen for a false-accept budget) recovers fewer takes than v1 and v2 at theirs.

## v5 (2026-11-07, candidate, not deployed)

**Status:** candidate model files, a one-shot comparison with v1, v2 and v3, and a leave-spk02-out
ablation. **Not deployed**; no change in Home Assistant, the cluster, the TVs or kivi-models. PC only.
Real numbers are **observed on c3-a and c3-b**, synthetic ones **observed-offline**. Nothing was listened to,
transcribed or printed. **Verdict: v5 does not meet its goal.** At its false-accept point it is level with v3
on synthetic voices (+5 points on quiet speech) and below v3 on spk01. On spk02 it is 2/4, which is too few
takes to rank anything.

**Why.** v3 fitted spk01: 33/39 on spk01's session 2, but 44 % on held-out synthetic voices (v2: 78 %) and
5/21 on spk02's c3-b button takes. v5 adds spk02's real takes and aims at household voices in general:
synthetic held-out recall close to v2's while gaining on both real speakers.

### What changed against v3

1. **spk02's real takes** (`v5_buttons.py`, revision `stp3`, tv-c3b only):
   - 10 clean takes: `training_rows`, take 2 excluded (no speech). The word end is the row's clip minus the
     1 s pad; a fresh energy-burst span agrees on all 10.
   - 3 extra takes cut from the raw wake file, labelled separately (`s2x`). They were picked from the 8
     `needs_review` takes and the 2 without a clip. The window is max(press - 4 s, previous take's last press +
     0.3 s) to the last press + 1 s; bursts are measured against the file's 10th-percentile 20 ms floor. A burst
     had to start before the press. Rule 1 (first to last such burst, at most 2.5 s) kept none. Rule 2 (the last
     burst before the press alone, at most 1.2 s) kept takes 5, 11 and 14. Dropped: takes 1, 4 and 16 (speech
     only after the press), 6 and 21 (span too long), 9 and 19 (no burst).
   - Hold-out: 4 of the 10 clean takes (seed 2026, by take: 3, 7, 13, 15). That leaves 6 clean and 3 extra
     takes for training. spk01's split is as in v3 (10 of 51 takes held out).
   - Augmentation as v3's tv takes, 600 copies per take.
2. **Speakers balanced:** per step, spk01 gets 32 real positives (16 tv-c3a, 16 desk sources) and spk02 gets 32
   (22 clean, 10 extra).
3. **Real share lowered** from 27 % (v3) to **17 %**: 64 real positives against 256 synthetic and 64 quiet
   synthetic. Tuned on validation in round 1 (below).
4. **Level widened down to quiet speech.**
   - Measured on the TV channel (`v5/levels.py`, numbers only), as the loudest 0.5 s RMS, which is the unit of
     `v2_augment.py --level-db` (post-gain, the level after the front end): spk02's 20 speech takes are
     -35.1 to -17.1 dBFS (median -30.3; sample peaks -20.8 to -3.1). spk01's tv-c3a takes are -28.5 to
     -19.3 (median -23.7).
   - The old level, N(-23, 5) dBFS, reaches spk02's 25th percentile in about 3 % of draws.
   - Added: 20 000 quiet synthetic positives and 12 000 quiet synthetic near-words, at uniform -45 to -28 dBFS
     (`--level-db u:-45,-28`, new). -45 dBFS RMS is about -31 dBFS sample peak, close to the brief's "about
     -35 dBFS peak".
   - Real tv takes of both speakers get -12 to +6 dB of gain (v3: ±6). spk01's desk takes get N(-27, 7).
5. **Warm start from v2** (2 000 steps, lr 1e-4, layer 64, the v3 batch otherwise). Validation said v2: the v3
   warm start lost 8 points of synthetic clean recall at equal room false accepts (35.9 against 43.6 %).
6. **Validation (selection only):**
   - Each model's threshold for at most 2 false accepts on the room selection part (8.38 h, 0.24/h).
   - Held-out synthetic voices: clean, TV at 10 and 0 dB, and a new quiet set (the same 1 800 clips at
     -35 dBFS, spk02's quietest take).
   - Synthetic near-words: the v1 set, the v3 set and a quiet copy of the v3 set.
   - Real held-out takes: spk01's 10 (6 "kivi" on tv-c3a) and spk02's 4.
   - The room test part and spk01's session 2 were not used for any choice.

### Candidates (validation only)

At each model's own threshold for at most 2 false accepts in 8.38 h of the room selection part. "spk01 tv" is
spk01's held-out "kivi" takes on tv-c3a (6); "spk02" is the 4 held-out takes (features path, `v3_select.py`).

| Variant | Raw threshold | Synthetic clean / 10 dB / 0 dB / quiet | Near-words, v3 set | spk01 tv | spk02 |
| --- | --- | --- | --- | --- | --- |
| v2 | 0.974 | 54.2 / 20.3 / 8.7 / 48.3 % | 13.7 % | 1/6 | 0/4 |
| v3 | 0.865 | 44.0 / 21.1 / 8.7 / 36.2 % | 8.8 % | 6/6 | 2/4 |
| no real positives (quiet synthetic only) | 0.909 | 29.8 / 14.8 / 5.8 / 27.6 % | 4.7 % | 0/6 | 0/4 |
| real 9 % (32 per step) | 0.887 | 43.5 / 20.3 / 8.6 / 40.8 % | 10.9 % | 4/6 | 1/4 |
| **real 17 % (64)** | 0.895 | 43.6 / 21.2 / 8.4 / 40.2 % | 12.0 % | 5/6 | 2/4 |
| real 23 % (96) | 0.918 | 39.0 / 18.2 / 7.4 / 35.2 % | 10.0 % | 5/6 | 2/4 |
| 17 %, no quiet synthetic | 0.903 | 46.5 / 20.4 / 8.5 / 40.8 % | 12.6 % | 5/6 | 2/4 |
| 17 %, v3 warm start | 0.919 | 35.9 / 15.6 / 7.2 / 30.8 % | 6.2 % | 6/6 | 2/4 |
| 17 %, real near-words 64 + 64 at weight 15 (v3) | 0.886 | 42.1 / 19.2 / 7.7 / 39.4 % | 8.0 % | 5/6 | 1/4 |
| 17 % + spk02 extras, synthetic near weight 3, seeds 1 / 2 / 3 | 0.881 / 0.918 / **0.893** | 48.2 / 39.4 / **45.6** clean, **41.6** quiet (seed 3) | 14.4 / 11.2 / **13.7** % | 5/6 each | 2/4 each |
| 17 % + extras, synthetic near weight 1, seeds 1-3 | 0.906-0.938 | 46.2-58.9 clean, 38.3-50.5 quiet | 13.2-20.5 % | 6/6 | 2/4 |
| 17 % without extras, near weight 1, seeds 1-3 | 0.887-0.932 | 46.7-65.8 clean, 39.8-57.1 quiet | 12.2-23.1 % | 6/6 | 2/4 |
| 17 %, no synthetic near-words | 0.952 | 60.7 / 25.7 / 11.4 / 46.2 % | 20.2 % | 6/6 | 2/4 |
| 17 %, 512 synthetic positives per step | 0.943 | 59.1 / 27.3 / 13.3 / 46.9 % | 19.7 % | 6/6 | 2/4 |
| weight average of the 3 near-weight-1 seeds without extras (`soup_nox_sn1`) | 0.894 | 63.9 / 29.4 / 13.4 / 54.8 % | 21.3 % | 6/6 | 2/4 |
| weight average of the 3 near-weight-3 seeds with extras | 0.896 | 45.1 / 20.9 / 8.8 / 40.5 % | 13.4 % | 5/6 | 2/4 |

Findings (validation only):

- **The real share** moves the real takes: with none, 0/6 and 0/4; at 9 %, 4/6 and 1/4; at 17 %, 5/6 and 2/4.
  At 23 %, synthetic recall drops. The evidence review's 5-10 % was too low for spk02 here.
- **Seeds dominate.** One configuration ranges over 20 points of synthetic clean recall across 3 seeds. The
  threshold rests on the third-highest room peak in 8.4 h. Synthetic recall and near-word acceptance rise
  together, so seeds mostly change the operating point, not how well the model separates.
- **Synthetic near-words are the main cost** to synthetic recall, as in v3. At near-word weight 1, or with
  none, synthetic recall reaches or passes v2 at the same room budget (59-66 % clean against 54 %), but
  near-word acceptance rises to 19-23 % (v2 13.7 %).
- **spk02's extra takes** made no difference on held-out takes: 2/4 with and without them at every seed.
- **Rule, fixed before round 2:** keep candidates with spk01 tv at least 5/6, spk02 at least 2/4 and v3-set
  near-words no higher than v2's 13.7 %; then take the highest mean synthetic recall (clean, 10 dB, 0 dB,
  quiet). Passing: extras + near weight 3, seed 3 (mean 29.5); without extras, near weight 1, seed 1 (29.3);
  the averaged model with extras (28.8). These are equal within the seed spread. **Chosen by the rule:
  `x_sn3_s3`, raw 0.89283.** It does not reach v2's synthetic recall at equal false accepts (45.6 against
  54.2 % clean).
- **Not tested on the test data:** `soup_nox_sn1` (weight average, near weight 1). On validation it meets the
  brief's criterion on synthetic and real voices (63.9 % clean, 54.8 % quiet, 6/6 and 2/4), but it accepts more
  near-words (21.3 % against v2's 13.7 % at the same budget). It is kept in `v5/alt/` to score on spk02's
  fresh test takes. That set is new data, so it can decide between the two without reusing a test.

### Final evaluation (one shot)

`v5/final.sh`, run once after the choice:

- spk01's session 2 through `kivi_score.py` (B4c7 excluded).
- spk02's 4 held-out takes through `kivi_score.py --button-takes`, with every other take excluded.
- The room test part through `v3_room.py score`.
- The synthetic sets through the server-exact ONNX path.

Each model is at its own server-0.5 point. Intervals: Wilson 95 % for recall, exact Poisson 95 % for false
accepts.

| | v1 (0.98) | v2 (0.95) | v3 (0.8653) | **v5 (0.89283)** |
| --- | --- | --- | --- | --- |
| spk01 S2 tv-c3a, 1.5 m TV silent | 3/10 | 8/10 | 10/10 | 10/10 |
| 1.5 m TV on | 2/10 | 6/10 | 9/10 | 9/10 |
| 3 m TV silent | 2/10 | 2/10 | 7/10 | 6/10 |
| 3 m TV on | 3/9 | 5/9 | 7/9 | 5/9 |
| TV silent (pooled) | 5/20, 25 % [11, 47] | 10/20, 50 % [30, 70] | 17/20, 85 % [64, 95] | 16/20, 80 % [58, 92] |
| TV on (pooled) | 5/19, 26 % [12, 49] | 11/19, 58 % [36, 77] | 16/19, 84 % [62, 94] | 14/19, 74 % [51, 88] |
| All | 10/39, 26 % [15, 41] | 21/39, 54 % [39, 68] | 33/39, 85 % [70, 93] | 30/39, 77 % [62, 87] |
| S2 near-word hits (of 12): tv-c3a / pc-mic / webcam-mic | 4 / 4 / 6 | 6 / 5 / 9 | 7 / 2 / 4 | 6 / 2 / 4 |
| tv-c3a near-word hits (2 each): "aquí vi", kili, kiri, kiwi, Pipi, tivi | 0, 0, 2, 1, 1, 0 | 0, 2, 2, 1, 1, 0 | 0, 1, 2, 1, 1, 2 | 1, 0, 2, 1, 1, 1 |
| S2 "kivi" recall, pc-mic / webcam-mic (of 39) | 2 / 3 | 5 / 12 | 28 / 34 | 27 / 30 |
| **spk02 c3-b, 4 held-out takes (too few to rank)** | 3/4 | 3/4 | 1/4 | 2/4 |
| Room test part, c3-a (4.11 h) | 1 = 0.24/h [0.01, 1.36] | 5 = 1.22/h [0.40, 2.84] | 1 = 0.24/h [0.01, 1.36] | 0 (< 0.90/h) |
| Room test part, c3-b (4.20 h) | 0 (< 0.88/h) | 4 = 0.95/h [0.26, 2.44] | 1 = 0.24/h [0.01, 1.33] | 3 = 0.71/h [0.15, 2.09] |
| Room test part, pooled (8.31 h) | 1 = 0.12/h [0.003, 0.67] | 9 = 1.08/h [0.49, 2.06] | 2 = 0.24/h [0.03, 0.87] | 3 = 0.36/h [0.07, 1.05] |
| Synthetic held-out voices, clean (n = 1 800) | 45.7 % [43.4, 48.0] | 77.9 % [75.9, 79.8] | 44.0 % [41.7, 46.3] | 45.6 % [43.3, 47.9] |
| TV content at 10 dB | 24.9 % [22.9, 26.9] | 32.4 % [30.3, 34.6] | 21.1 % [19.3, 23.1] | 21.3 % [19.4, 23.2] |
| TV content at 0 dB | 11.0 % [9.6, 12.5] | 14.5 % [13.0, 16.2] | 8.7 % [7.5, 10.1] | 9.3 % [8.1, 10.8] |
| Quiet, -35 dBFS (spk02's quietest take) | 37.9 % [35.7, 40.2] | 69.4 % [67.3, 71.5] | 36.2 % [34.0, 38.5] | 41.6 % [39.4, 43.9] |
| Near-words, held-out voices: v1 set (1 799) / v3 set (1 220) / v3 set quiet | 3.2 / 9.9 / 7.9 % | 9.3 / 27.1 / 24.8 % | 3.3 / 8.8 / 7.3 % | 5.5 / 13.7 / 11.3 % |

The v1, v2 and v3 session-2 counts equal the v3 run, so the path is unchanged. **spk02's real check is too
small:** 4 takes from one 2-minute session; a single take moves the rate by 25 points. The synthetic held-out
voices were used for the choice, so their numbers are not independent of it.

**v5 trade-off** (DET-style, not a threshold choice). spk01 session 2 on tv-c3a; spk02's 4 held-out takes; the
room test part pooled over 8.31 h (c3-a / c3-b in brackets); synthetic held-out clean / quiet.

| Raw threshold | S2 recall (of 39) | TV silent (of 20) | TV on (of 19) | Near-word hits (of 12) | spk02 (of 4) | Room false accepts | Per hour [95 %] | Synthetic clean / quiet |
| --- | --- | --- | --- | --- | --- | --- | --- | --- |
| 0.5 | 35 | 19 | 16 | 10 | 4 | 131 (50 / 81) | 15.8 [13.2, 18.7] | 94.8 / 92.5 % |
| 0.7 | 34 | 18 | 16 | 10 | 4 | 35 (14 / 21) | 4.21 [2.93, 5.86] | 82.5 / 77.6 % |
| 0.8 | 34 | 18 | 16 | 8 | 4 | 14 (5 / 9) | 1.68 [0.92, 2.83] | 67.1 / 62.6 % |
| 0.85 | 32 | 17 | 15 | 7 | 4 | 5 (1 / 4) | 0.60 [0.20, 1.40] | 56.2 / 52.6 % |
| 0.8653 | 31 | 16 | 15 | 6 | 3 | 4 (0 / 4) | 0.48 [0.13, 1.23] | 52.1 / 48.4 % |
| **0.89283** | **30** | **16** | **14** | **6** | **2** | **3 (0 / 3)** | **0.36 [0.07, 1.05]** | **45.6 / 41.6 %** |
| 0.9 | 30 | 16 | 14 | 5 | 2 | 2 (0 / 2) | 0.24 [0.03, 0.87] | 43.8 / 39.7 % |
| 0.93 | 27 | 14 | 13 | 4 | 2 | 0 | 0 (< 0.44) | 34.4 / 30.1 % |
| 0.95 | 25 | 13 | 12 | 1 | 1 | 0 | 0 (< 0.44) | 27.1 / 22.7 % |
| 0.97 | 14 | 8 | 6 | 1 | 1 | 0 | 0 (< 0.44) | 17.2 / 14.3 % |
| 0.98 | 5 | 4 | 1 | 0 | 1 | 0 | 0 (< 0.44) | 9.6 / 8.6 % |

**Server runtime check:** the 12 v2 check clips went through pyopen-wakeword 1.1.0 with the TFLite files. The
maximum scores matched the ONNX path to 0.0006, and the calibrated file at 0.5 decided like the raw file at
0.89283 on all 12.

### Leave-spk02-out ablation

The chosen recipe without any spk02 draws, seeds 1-3, each at its own room-selection threshold (0.872, 0.866,
0.866). Scored with the server runtime (`kivi_score.py --button-takes`). These models never saw spk02, so all 20
of her speech takes are unseen (take 2 has no speech).

| Model | spk02, 4 held-out takes | spk02, all 20 speech takes | Median max score (21 takes) | Synthetic clean (validation) |
| --- | --- | --- | --- | --- |
| without spk02, seeds 1 / 2 / 3 | 0/4, 0/4, 0/4 | 2/20, 2/20, 2/20 | 0.66 / 0.64 / 0.66 | 46.9 / 49.3 / 51.0 % |
| v5 (with spk02) | 2/4 | not a test (trained on 9 of them) | 0.96 | 45.6 % |
| v1 / v2 / v3 (never saw spk02) | 3/4, 3/4, 1/4 | 5/20, 10/20, 5/20 | 0.95 / 0.94 / 0.71 | 46.6 / 54.2 / 44.0 % |

- spk02's takes are what move spk02, at a small cost in synthetic recall.
- **The recipe does not generalise to an unseen speaker at this false-accept budget.** Without her takes it
  gets 2/20, below v1 and v2. Training on spk01, the room negatives and the near-words makes the model stricter
  on unknown voices than v1 or v2 were. Inferred: a third household voice will do about as badly unless its
  takes are added.
- spk02's 3 extra takes: no effect on her held-out takes (2/4 with and without at 3 seeds, validation path).

### Deviations from the brief, with the evidence

- **Real share 17 %, not lower.** 9 % lost spk01 (4/6) and spk02 (1/4) on validation, with the same synthetic
  recall.
- **The success criterion is not met** under the near-word rule fixed before round 2. Dropping that rule
  meets it on validation (`soup_nox_sn1`), at about twice v2's near-word rate at equal false accepts. This is
  the owner's call.
- **"-35 dBFS peak"** was read in the augmentation's own unit (loudest 0.5 s RMS): -45 to -28 dBFS RMS covers
  spk02's quietest take (-35.1) with 10 dB of margin.
- **Selection was by validation, not retrained:** the final model is the validated one; the hold-out takes
  were not added back.

### Scoring the fresh spk02 test takes

After `kivi_extract.py --button-takes --device c3-b --speaker spk02 --split test --since ... --until ...`
(say, then press), one command from `/work/workspaces/kivi-wakeword` gives recall per model at its server-0.5
point and at the default thresholds. Add `--exclude-take N` for a take with no speech; the reports go into the
session's `scores/` directory:

```sh
systemd-run --user --scope -p MemoryMax=12G -p CPUQuota=800% nice -n 10 .venv/bin/python -I \
  ~/homelab/scripts/voice-assistant/wakeword/kivi_score.py --button-takes datasets/real/<new button session> \
  --model v1=final/kivi_uncalibrated.tflite --model v2=v2/final/kivi_uncalibrated.tflite \
  --model v3=v3/final/kivi_uncalibrated.tflite --model v5=v5/final/kivi_uncalibrated.tflite \
  --model v5alt=v5/alt/soup_nox_sn1/kivi_uncalibrated.tflite \
  --calibration v1=0.98 --calibration v2=0.95 --calibration v3=0.8653 --calibration v5=0.89283 --calibration v5alt=0.89379
```

### Artifacts (private, not committed)

In `/work/workspaces/kivi-wakeword/v5/`:

- `final/`: model files, `SHA256SUMS`, `kivi_uncalibrated.calibration.json`, training history.
- `alt/soup_nox_sn1/` and `abl/nos2_s{1,2,3}/`: uncalibrated files with calibration sidecars; for scoring only,
  not a release.
- `results/`: selection JSON, session-2, spk02, room-test, synthetic and ablation reports, `summary.md`,
  `parity.txt`.
- `runs/`: 23 trained candidates and 3 weight averages.
- `feat/`: quiet synthetic sets.
- `real/` (mode 700, see the [data register](../data-register.md), row 14).
- Scripts: `levels.py`, `feats1.sh`, `feats2.sh`, `runs.sh`, `runs1.sh`, `runs2.sh`, `soup.py`, `abl.sh`,
  `abl_score.sh`, `select.sh`, `final.sh`, `export.sh`, `summ.py`.

Wall clock about 40 min (features 6 min, 23 training runs of about 25 s, selection, final 9 min). Other agents
ran on the same PC at the same time.

| File | SHA-256 | Use |
| --- | --- | --- |
| `kivi.tflite` | `94ad1fba38ee55b526d36a44738aa4fdaade30742b168a07a20f5bd3a598ae03` | calibrated: server 0.5 = raw 0.89283 |
| `kivi.onnx` | `16bb0f19aefab5201bcd606407a1c17f229cca4b88a89ef6c7f2f311ee06dd78` | same, ONNX |
| `kivi_uncalibrated.tflite` | `50ffba74b2e602002b0fa4824cb1d401365281084d0daf337571e69b33065e63` | raw output (`kivi_score.py`, threshold sweeps) |
| `kivi_uncalibrated.onnx` | `177ae3e76a2cd1ec09d5a8a9b5a8547825d114670cc7e2cd1b23d0538ffc50e6` | raw output (`fast_report.py`, `v3_select.py`) |

## v4: "oye Kivi" (2026-11-07, candidate, not deployed)

**Update (2026-11-07, evening):** released and enabled as a trial on c3-a only, with the owner's approval; see
[v4 trial deployment](#v4-trial-deployment-2026-11-07-c3-a-only). The text below is the state before that.

**Status:** candidate model files for the new wake phrase "oye Kivi" and a one-shot evaluation. **Not deployed**;
no change in Home Assistant, the cluster, the TVs or the kivi-models tags. PC only. Nothing was listened to,
transcribed or printed. **Real "oye Kivi" recall is untested:** no real "oye Kivi" takes exist yet. The real
audio below only tests what must *not* wake the model ("Kivi" alone, near-words, room audio).

**Why.** On 2026-11-07 the owner switched the household wake phrase from "Kivi" to "oye Kivi"
(kivi-models `docs/plan/2026-11-fallback-wake-phrase.md`, in progress in parallel with Kivi v5):
- v2 got 21/39 on spk01 and 9/21 on spk02.
- v3 got 33/39 on spk01, but only 4/21 on spk02 and 44 % on held-out synthetic voices.
- "kili", "kiri", "pipi" and "tivi" score as high as "kivi".

### Method

1. **Synthetic positives** (`v4_gen.py`, `v4_oye_kivi.yaml`): 59 969 training and 3 000 validation clips from
   32 voice groups (31 Piper models).
   - Voices: the four native Spanish voices first (weight 2 each), plus LibriTTS-R, VCTK, L2-ARCTIC (non-native
     English accents), ARCTIC, ARU, SEMAINE, Basque, Catalan, Italian, Portuguese (PT, BR), Romanian, German
     (Thorsten emotional) and single English and French voices, all fed Spanish phonemes.
   - Each clip is "oye" + joiner + "kivi" + ending:
     - "oye" variants: ˈoʝe 55 %, ˈoje 18 %, ˈoʒe 8 %, ˈoʃe 4 %, ˈoʎe 5 %, and "oiga" (ˈoɪɣa) 10 %;
     - "kivi" variants: as v1-v3;
     - joiner: space 50 %, comma 20 %, run together 10 %, or two renderings with a gap of 80-450 ms 20 %.
   - Speed (length scale 0.75-1.35), Piper noise scales, and with p 0.5 a resampling warp of ±10 % (pitch,
     formants and tempo together).
   - Durations: median 0.84 s, p95 1.8 s.
2. **Held-out voices** (evaluation only): the v1-v3 set (es_MX claude, es_AR daniela, es_ES carlfm,
   LibriTTS-R 800-903, VCTK 90-108) plus eu_ES maider and L2-ARCTIC speakers 18-23. 2 216 "oye Kivi" clips
   and 2 862 near-phrase clips (46 phrases and 16 sentences, about 46 clips each).
3. **Near-phrases** (59 982 training and 2 999 validation clips by the training voices):
   - "Kivi" alone (with the positives' "kivi" variants, weight 8), "oye kili / kiri / tivi / pipi / chivi /
     Fifi / Vivi / Kiki / ...", "oye", "oye tú", "oye mira", "hoy vi", "o sea", "y Kivi", "ve Kivi",
     "que / mi / el / ojo / fue Kivi", "oiga", and 16 short sentences such as "Kivi, enciende la luz" and
     "oye, ¿qué hora es?" (full list and weights in the YAML).
   - Same interference, level and front-end as the positives, so the level cannot predict the label.
4. **Augmentation**, as v2/v3 (`v4_augment.py` = `v2_augment.py` plus a quiet branch):
   - TV-like interference (AudioSet TV/music/speech and MLS speech, no stationary noise) at 10-30 dB on the
     word with p 0.5; RIR p 0.5; calibrated mic floor; the satellite front end (HPF, AEC3 + NS, +24 dB, S16);
     server-exact features.
   - Level: the word's loudest 0.5 s RMS is N(-23, 5) dBFS, or with p 0.3 uniform in -46 to -30 dBFS.
     Post-front-end word peaks of the validation positives: p1 -34.5, p5 -31.1, median -13.4, p95
     -2.5 dBFS. So the training covers spk02's measured -31 to -16 dBFS and reaches about -35 dBFS.
5. **Real negatives** (`v4_realneg.py`): `negative_rows_for_phrase(read_manifest(...), "oye kivi")` at
   1cf33e371. Superseded rows are skipped and the test session never enters.
   - spk01 session 1: all three sources; 111 "kivi", 36 near-word and 87 background-segment clips.
   - spk02's c3-b button takes, revision `stp3`: 10 clean takes plus the 8 `needs_review` takes. For another
     phrase a loose cut is still a valid negative.
   - 30 augmented copies per take (6 per segment). tv sources get a gain of -12 to +6 dB and interference
     through the front-end emulation; desk sources get RIR, level, floor and the front end.
   - Every window ending inside the clip, stride 2: 88 800 "kivi", 24 840 near-word and 3 576 background
     windows.
   - **spk01's session 2 is not in training:** it is the false-trigger test below. spk02's takes are now
     training data (owner, 2026-11-07); fresh spk02 takes come later. So the spk02 figure below is a fit
     check, not a held-out result.
6. **Room audio**, v3's time split per TV:
   - train part: v3's windows and v3's v2-mined hard negatives;
   - plus hard negatives mined with an early v4 head (`ws_v2_6k`, score above 0.02, at most one per 0.4 s):
     1 254 room windows and 5 033 MLS shard-1 windows through the front end;
   - sel part: the threshold;
   - test part: the final evaluation only.
7. **General negatives** as v2/v3: ACAV100M 512 per step, MLS train 0 windows 128, v2's MLS hard negatives
   128.

### Candidates and selection

20 heads in 3 rounds (`train_simple.py`, layer 64 unless noted, about 1 min each on the GPU). The rule was
written down before round 2 (`v4/results/selection_rule.txt`):
1. **Threshold:** the lowest raw threshold with at most 2 false accepts in the 8.38 h room selection part
   (0.24/h).
2. **Eligible:** a validation "Kivi"-alone acceptance of at most 2 % and a validation near-phrase acceptance
   of at most 1 %.
3. **Choice:** the highest validation recall.

Validation means the training voices' held-back windows, so the held-out voices did not enter the choice
(unlike v3).

| Variant (selection room part, validation) | Raw thr. | Val. recall | Val. near / "Kivi" alone | Held-out clean / 10 dB / quiet | Held-out near / "Kivi" alone |
| --- | --- | --- | --- | --- | --- |
| From scratch, layer 64 / 128 / weight 6 | 0.986-0.997 | 21.8-23.2 % | 0.03-0.17 % / 0 | 51-54 / 29 / 36-40 % | 2.0-2.6 % / 0/46 |
| From scratch, no real negatives | 0.987 | 26.7 % | 0.23 % / 0.95 % | 60.3 / 34.9 / 45.0 % | 5.2 % / 1/46 |
| v2 warm start, 2 000 steps at 1e-4 | 0.805 | 47.3 % | 3.4 % / 3.8 % (not eligible) | 81.6 / 53.5 / 69.3 % | 14.1 % / 4/46 |
| v2 warm start, 6 000 steps at 3e-4 (`ws_v2_6k`) | 0.984 | 31.9 % | 0.43 % / 0.95 % | 66.2 / 40.3 / 54.5 % | 5.5 % / 0/46 |
| + v4-mined negatives, 6 000 steps (3 seeds; "Kivi" weight 12) | 0.986-0.993 | 23.2-29.1 % (28.1 %) | 0.17-0.30 % / ≤ 0.5 % | 58-64 / 31-39 / 44-52 % | 4.1-6.0 % / 1-2/46 |
| + mined, from scratch | 0.985 | 29.1 % | 0.33 % / 0.48 % | 59.0 / 35.5 / 45.0 % | 3.9 % / 1/46 |
| + mined, 3 000 steps at 2e-4, "Kivi" weight 12 (2 seeds) | 0.930-0.932 | 36.3-36.5 % | 0.80-0.87 % / 0.5-1.0 % | 70-71 / 43-44 / 58 % | 7.6 % / 2-3/46 |
| + mined, 4 000 steps at 2e-4, "Kivi" weight 12 (2 seeds, seed 2 not eligible at 1.07 % near; synthetic near 192/step 38.7 %) | 0.927-0.954 | 33.2-41.4 % | 0.47-1.07 % / 0-1.9 % | 68-76 / 42-49 / 57-65 % | 6.6-9.8 % / 3/46 |
| **+ mined, 4 000 steps at 2e-4 (3 seeds; seed 2 `m_ws4k_s2` chosen)** | **0.944-0.947** | **37.4-39.0 %** | **0.73 % / 1.4-1.9 %** | **73-74 / 46-47 / 62-63 %** | **8.9-9.1 % / 3/46** |

Findings (selection data only):
- **The warm start from the "kivi" v2 head beats training from scratch.** At the same room budget, validation
  recall was 37-39 % against 22-29 %.
- **Short warm starts stay a "kivi" detector.** At 2 000 steps "Kivi" alone passed 3.8 % of validation; at
  3 000-4 000 steps with v4's real and synthetic "kivi" negatives it fell to 0.5-1.9 %.
- **Without the real "kivi" takes,** "Kivi" alone stays higher (0.95 % of validation, 1/46 held out) at
  equal room false accepts.
- **The threshold is the noisy part, as in v3:** it rests on the third-highest room peak, and the seeds of
  one recipe span up to 8 points of validation recall.

**Chosen:** `m_ws4k_s2`, raw threshold **0.9437**. It is the v2 warm start, 4 000 steps, one-cycle lr 2e-4,
seed 2. Per step: 256 synthetic positives, 128 synthetic near-phrases, 512 ACAV, 128 MLS, 128 v2 MLS hard,
128 room train, 64 v3 room hard, 128 v4 room hard, 128 v4 MLS hard, and real negatives (64 "kivi" at weight 6,
32 near-word at 6, 32 background); the other negatives at weight 3. `m_ws4k_k12_a192` (38.7 %) was within a
point but tied on validation near-phrases (0.73 %), so the higher recall stayed.

### Final evaluation (one shot)

Run once (`v4/final.sh`):
- spk01 session 2 and spk02's button takes through `kivi_score.py` (pyopen-wakeword with the uncalibrated
  TFLite file);
- the room test part through `v3_room.py score` (the same runtime);
- the synthetic and long-audio sets through the server-exact ONNX path.

All at raw 0.9437 (the calibrated file's 0.5) unless a column says otherwise. Intervals: Wilson 95 % for
rates, exact Poisson 95 % per hour.

| Test | v4 at raw 0.9437 |
| --- | --- |
| **Real "oye Kivi" recall** | **untested (no real takes yet)** |
| Held-out synthetic voices, clean (n 2 216) | 72.8 % [70.9, 74.6] |
| TV content at 10 dB | 47.2 % [45.1, 49.2] |
| TV content at 0 dB | 23.9 % [22.1, 25.7] |
| Quiet speech (word peaks after the front end p5 -33.3, median -29.2, p95 -24.8 dBFS) | 61.8 % [59.8, 63.8] |
| Per held-out voice, clean / quiet | claude 97 / 95 %, maider 86 / 81 %, carlfm 73 / 51 %, libritts 70 / 56 %, vctk 77 / 67 %, l2arctic 61 / 53 %, daniela 42 / 28 % |
| Near-phrases, held-out voices (n 2 862) / with TV at 10 dB | 8.9 % [7.9, 10.0] / 5.8 % [5.0, 6.7] |
| **"Kivi" alone, real, spk01 session 2 (held out), tv-c3a / pc-mic / webcam-mic** | **0/39 / 0/39 / 0/39** [0, 9.0 %] each; 0/39 on every source even at raw 0.5 |
| Real near-words, session 2 (kili, kiri, kiwi, Pipi, tivi, "aquí vi"; 12 per source) | tv-c3a 0/12, pc-mic 1/12, webcam-mic 0/12 |
| "Kivi" alone, real, spk02 c3-b button takes (**training negatives, a fit check**) | 0/21; median of the per-take maximum score 0.04 |
| "Kivi" alone, synthetic, held-out voices | 3/46 = 6.5 % [2.2, 17.5] |
| **Room test part, c3-a (4.11 h)** | **2 = 0.49/h [0.06, 1.76]** |
| **Room test part, c3-b (4.20 h)** | **6 = 1.43/h [0.52, 3.11]** |
| **Room test part, pooled (8.31 h)** | **8 = 0.96/h [0.42, 1.90]** |
| MLS Spanish test (10.3 h) / VoxPopuli test (5.2 h), through the front end | 5 = 0.49/h [0.16, 1.14] / 0 (< 0.71/h) |
| P2 TV content (1.15 h) / P2 commands after "alexa" (0.23 h) | 4 = 3.5/h [0.95, 8.9] / 0 |

Near-phrases on held-out voices that pass most often (out of about 46):
- "oye kiri" 25 (the phrase the check found partly collapsed onto the target);
- "fue Kivi" 21, "ojo Kivi" 19;
- "oye chivi", "oye aquí" and "oye que vi" 15 each;
- "oye kili" 13, "el Kivi" 12;
- "ve Kivi", "que Kivi", "mi Kivi", "oye Kiki" 10 each;
- "oye tivi" 8;
- "y Kivi" and "Kivi" alone 3 each;
- "oye", "oye tú", "oye mira", "o sea" and most sentences 0.

**DET table** (one model, thresholds read off the same runs; not a threshold choice):

| Raw threshold | Clean | TV 10 dB | TV 0 dB | Quiet | Near-phrases | "Kivi" alone (synthetic) | Room test FA (c3-a / c3-b) | Per hour, pooled [95 %] |
| --- | --- | --- | --- | --- | --- | --- | --- | --- |
| 0.8 | 88.7 % | 67.4 % | 37.0 % | 81.3 % | 18.7 % | 5/46 | 42 (20 / 22) | 5.05 [3.64, 6.83] |
| 0.9 | 81.4 % | 56.2 % | 29.8 % | 71.5 % | 12.6 % | 3/46 | 13 (5 / 8) | 1.56 [0.83, 2.67] |
| 0.93 | 75.9 % | 49.8 % | 25.8 % | 65.8 % | 10.1 % | 3/46 | 9 (3 / 6) | 1.08 [0.49, 2.06] |
| **0.9437** | **72.8 %** | **47.2 %** | **23.9 %** | **61.8 %** | **8.9 %** | **3/46** | **8 (2 / 6)** | **0.96 [0.42, 1.90]** |
| 0.95 | 71.1 % | 45.1 % | 22.5 % | 59.5 % | 8.2 % | 3/46 | 7 (1 / 6) | 0.84 [0.34, 1.74] |
| 0.97 | 63.8 % | 37.2 % | 18.7 % | 50.8 % | 5.6 % | 1/46 | 4 (0 / 4) | 0.48 [0.13, 1.23] |
| 0.98 | 57.5 % | 30.8 % | 15.1 % | 42.3 % | 4.1 % | 1/46 | 1 (0 / 1) | 0.12 [0.00, 0.67] |
| 0.99 | 42.5 % | 20.0 % | 9.8 % | 27.7 % | 1.9 % | 0/46 | 1 (0 / 1) | 0.12 [0.00, 0.67] |

**Server runtime check:**
- 12 held-out "oye Kivi" clips through the front end (6 clean, 6 with TV at 10 dB) were streamed through
  pyopen-wakeword 1.1.0 with the TFLite files.
- The maximum scores matched the ONNX path to 0.001.
- The calibrated file at 0.5 decided like the raw file at 0.9437 on 12 of 12 (server and ONNX).

**File name and wake word id** (wyoming-openwakeword v2.1.0 source, `wyoming_openwakeword/__main__.py`,
checked 2026-11-07):
- The id of a custom model is the file stem.
- A version suffix is removed only when the whole stem matches `^([^_]+)_v[0-9.]+$`.
- So `oye_kivi.tflite` is served as `oye_kivi` (described to Home Assistant as "Oye Kivi" by `_get_phrase`).
- A file named `oye_kivi_v4.tflite` would keep the whole stem as its id, because the name part may not
  contain an underscore.
- The deployable file must therefore be named exactly `oye_kivi.tflite`.

### Verdict and interpretation

- **"Kivi" alone does not wake v4 in real audio:** 0/39 on every source of spk01's held-out session 2, at
  every threshold from raw 0.5 up, and 0/21 on spk02's (trained-on) takes. The real near-words of session 2
  pass 0-1 of 12. On synthetic held-out voices "Kivi" alone still passes 3/46, and "fue / ojo / el / que /
  mi Kivi" pass 22-45 %. A word ending in a vowel before "Kivi" is the main synthetic confuser.
- **Room false accepts miss the ≤ 0.25/h target on the test part:**
  - 0.96/h [0.42, 1.90] pooled, against 0.24/h on the selection part at the same threshold.
  - c3-b carries 6 of the 8.
  - v2 showed the same shift from selection to test (the test part is daytime 2026-11-06).
  - The DET table puts the target near raw 0.98 (1 false accept, 0.12/h [0.00, 0.67]). There the
    synthetic recall is 57.5 % clean, 30.8 % at 10 dB and 42.3 % quiet.
  - The threshold was not moved after seeing the test part. Choosing 0.98 would need a new selection rule
    (for example, more room selection hours) or the owner's decision.
- **Synthetic recall is higher than v3's "kivi"** at the same selection budget (at most 2 false accepts on
  the room selection part): 72.8 % against 44.0 % clean, 47.2 % against 21.1 % at 10 dB. The phrases and
  held-out sets differ, so this is indicative only, and v4's test-part false accepts are 4 times v3's. Quiet speech holds 61.8 %. The weakest held-out voice is es_AR daniela
  (42 % clean, 28 % quiet). Synthetic numbers say little about real voices (microWakeWord's warning, in the
  evidence review).
- **Next:** record real "oye Kivi" sessions (one train and one test session for spk01, and fresh c3-b
  button takes for spk02, `kivi_session.py --phrase "oye kivi"`). Then re-select on more room hours, then
  apply the gate. Do not deploy before that.

### Deviations from the brief, with the evidence

- **Phrases left out of the near-phrases:**
  - "oye kibi" and "oye quivi" phonemize to the target `ˈoʝe kˈiβi`.
  - `v4_gen.py check` measures DTW on log-mel of deterministic renderings (62 per phrase), as a ratio to two
    renderings of the target. On that scale the target's own variants score 6.0-7.8. "oye quiví" (5.7,
    stress shift only) and "hoy Kivi" (6.8) fall inside that range.
  - "oye kiri" scored 5.4 with 14 % of renderings below 1.5. It is in the brief, so it stays, at weight 1.5
    instead of 3. It is also the top synthetic confuser (25/46).
  - "oye kiwi" (the pipeline is called "Kiwi") and "ey / hey Kivi" (the alternative phrase) are neither
    positive nor negative.
- **Voices left out:**
  - fr_FR-mls, de_DE-mls, nl_NL-mls and en_US-norman babble on this phrase: 38 / 5 / 0 / 27 % of clips
    dropped by the length check, and 20 / 24 / 10 / 3 % of the kept clips with 4 or more energy bursts,
    against 0 % for the kept voices.
  - Their already-generated held-out rows (fr_mls 100-124) were removed from the manifests, so the held-out
    set has 7 voice groups, not 8.
  - en_US-lessac, fr_FR-tom, en_US-amy and en_GB-jenny_dioco were not used because of unclear licence terms.
- **No held-out spk02 subset:** her takes became training data for Kivi v5 (owner, 2026-11-07), and fresh
  takes come later. spk02's `needs_review` rows of `stp3` were added to the negatives (see Method, item 5).
- **Warm start from v2,** because it validated better (37-39 % against 22-29 % from scratch). Not from v3.
- **The quiet held-out set** uses a loudest-0.5 s RMS of N(-42, 2) dBFS. That gives post-front-end peaks of
  -33 to -25 dBFS, the lower half of spk02's measured range.
- **The first feature job failed** on `--level-db -42,2`: argparse read the value as an option. It was rerun
  with `--level-db=-42,2` (`feats2.sh`). The three sets already written were kept.

### Artifacts (private, not committed)

In `/work/workspaces/kivi-wakeword/v4/`:
- `final/`: model files, `SHA256SUMS`, `oye_kivi_uncalibrated.calibration.json`, training history.
- `results/`: selection JSON, rule, session-2, spk02, room-test and synthetic reports, `near_check.json`,
  `parity.txt`.
- `runs/`: 20 candidates.
- `clips/`: synthetic clips and manifests.
- `feat/`: server-exact features.
- `real/`: mode 700, see the [data register](../data-register.md), row 15.
- `voices/`: 31 downloaded Piper voices, md5-checked, with their model cards.
- Scripts: `dl_voices.sh`, `gen.sh`, `feats.sh`, `feats2.sh`, `runs1-3.sh`, `select.sh`, `final.sh`,
  `export.sh`, `srvcheck/`.

Wall clock 15:23 to 18:40, about 3 h 15 min:
- clip generation 39 min;
- features 15 min;
- 20 training runs of about 1 min, with mining and three selections, about 18 min;
- the final evaluation 7 min;
- about 1.5 h idle after the failed feature job.

| File | SHA-256 | Use |
| --- | --- | --- |
| `oye_kivi.tflite` | `4ec448f65f615fb2a2c3ea8dbf9759e288d2a4098aacfddacb0400c2cc2333c8` | calibrated: server 0.5 = raw 0.9437; wake word id `oye_kivi` |
| `oye_kivi.onnx` | `5d49da88f54dcf12febc11737c2f65aaaf83816c481b8bf30594f08c2d87b803` | same, ONNX |
| `oye_kivi_uncalibrated.tflite` | `4304cb85601ce54e5e8f3cf105a8905f3584b713f11ac8f4ede80b5ece5480a6` | raw output (`kivi_score.py`, threshold sweeps) |
| `oye_kivi_uncalibrated.onnx` | `b3576b0a7461cf3858906f9db7ea7b70fdc67f9382caa67fe63b73073183e859` | raw output (`fast_report.py`, `v4_select.py`) |

## v4 trial deployment (2026-11-07, c3-a only)

**Approval:** the owner, 2026-11-07: "Go ahead, deploy it and document with scripts all the workflow to almost
automate this phase". It covers one kivi-models release tag for this model and a pipeline switch for c3-a. v4
fails the room false-wake gate (0.96/h [0.42, 1.90] against ≤ 0.25/h) and its real recall is untested; the
trial exists to record real "oye Kivi" takes and count false wakes. Nothing on the TVs was touched (no SSH, no
install); c3-b stays on `alexa`.

**Release (private repository `agil/kivi-models`):**
- The image now carries **every model of `models.lock`** at the tagged commit, one `/models/<id>.tflite` each
  plus `/models/SHA256SUMS`: `kivi` `v1-2026-11-05` (`5e20c8ee4642`) and `oye_kivi` `v4-oye-kivi-2026-11-07`
  (`4ec448f65f61`). Each pin is checked against the version's `SHA256SUMS` by the workflow and against the
  file inside the build (a wrong pin fails the build, checked locally).
- Release `git.grigri.cloud/agil/kivi-models:v4-oye-kivi-2026-11-07@sha256:a73af8e220f03afecca80dbbdd7ae62f9d2ab8023b12ff0fd12839a2ac9d215b`
  (CI run 5 on tag `v4-oye-kivi-2026-11-07`, commit `b908806`). Anonymous pull refused (registry HTTP 401;
  `docker pull` without credentials: `unauthorized`).
- Old releases still build: a dispatch of `v1-2026-11-05` (no push) rebuilt the tag's commit the old
  single-model way, and a dispatch of the then untagged v4 version built `main` as a pre-release check
  (runs 3 and 4, both green). The digest read through the registry API for `v1-2026-11-05` matches the old
  pin (`9e51e0c9dba0`), so the digest method is the one Kubernetes resolves.

**Cluster (GitOps, Argo CD):** the init container, renamed `01-copy-wake-words`, runs
`cd /models && sha256sum -c SHA256SUMS && cp /models/*.tflite /custom/` (commit `3b7250cb7`).
Observed-on-cluster, 2026-11-07 18:28-18:33 UTC:
- Argo CD Synced and Healthy; the new openwakeword pod Ready. Init log: `kivi.tflite: OK`, `oye_kivi.tflite: OK`.
- In the pod: `/custom/kivi.tflite` `5e20c8ee…778cb` and `/custom/oye_kivi.tflite` `4ec448f6…2333c8`.
- Wyoming `describe` (from inside the openwakeword pod) lists `alexa`, `hey_jarvis`, `hey_mycroft`,
  `hey_rhasspy`, `kivi`, `okay_nabu` and `oye_kivi` ("Oye Kivi"); Home Assistant's `wake_word/info` lists
  `oye_kivi` without a Wyoming reload.
- Synthetic check through the server (held-out Piper clips, never trained on, streamed from inside the
  openwakeword pod, nothing written): "oye Kivi" sent to `oye_kivi` 4/4 detected; "alexa" sent to `alexa` 2/3;
  "alexa" sent to `oye_kivi` 0/2; "Kivi" alone sent to `oye_kivi` 0/3; "oye Kivi" sent to `alexa` 0/2.
- `alexa`: the stock model and the server image are unchanged; c3-a's Kiwi run restarted with `wake_word-start`
  right after the pod restart (18:29 UTC), before the trial switch.

**Home Assistant (websocket API, short-lived login token, refresh token revoked afterwards; nothing written on
the HA pod):**
- Can `alexa` stay next to `oye_kivi` on one satellite? **No, not with server-side wake words in Home Assistant
  2026.10.4** (source read in the pod): an ESPHome satellite has two pipeline selects, but the second one is
  chosen only when the device reports which on-device wake word fired (`handle_pipeline_start` matches
  `wake_word_phrase` against the satellite's wake word selects); with streaming wake words the satellite sends
  none, so pipeline 1 always runs. A pipeline has one `wake_word_id`, and the Wyoming client sends
  `Detect(names=[wake_word_id])` (or the server default `okay_nabu` without one). So c3-a hears only "oye Kivi"
  during the trial; on-device wake words in the satellite would lift this.
- New pipeline **"oye Kivi"**: a copy of "Kiwi" with only `wake_word_id: oye_kivi`
  (same STT, conversation agent `conversation.fast`, TTS and `prefer_local_intents`). "Kiwi" stays the preferred
  pipeline and keeps `alexa`; "Kiwi Test" unchanged.
- `select.lg_c3_c3_a_voice_satellite_assistant`: `preferred` → **`oye Kivi`**. c3-b's select stays `preferred`.
- A select change applies at the satellite's next run, and the open wake-word run lasts until a detection or a
  reconnect. An `assist_pipeline/pipeline/update` of "Kiwi" with every field unchanged (read back identical)
  invalidated the open run (the runtime aborts the runs of an updated pipeline); c3-a started an "oye Kivi" run
  (`wake_word-start`) within 15 s. c3-b's microphone was muted at the time.
- **A Home Assistant restart reset the select to `preferred`** (observed-on-cluster): the dashboard commit
  below changed a file under `apps/home-assistant/config/`, Reloader restarted the `hass` pod at 18:35 UTC, and
  after the restart c3-a's pipeline select was `preferred` again (history: `oye Kivi` set 18:31:20 by the API
  call, `preferred` at 18:35:46 when the satellite reconnected). The trial stopped silently for 7 minutes, until
  `wake_release.py enable` set it again at 18:42:48 and saw a new "oye Kivi" run. Why the restored state was
  `preferred` is not known. Check `wake_release.py status` after any HA restart.
- Snapshots before and after: `/work/workspaces/kivi-wakeword/releases/v4-oye-kivi-2026-11-07/` (pipeline
  settings and select states only), and the driver's `state.json`.
- Dashboard `/voice-assistant`: an "oye Kivi trial (c3-a pipeline)" select with its on/off state (commit
  `8252f8aa9`; it restarted Home Assistant through Reloader, about 10 s).

**Rollback (exact):**
1. `select.select_option` on `select.lg_c3_c3_a_voice_satellite_assistant` with option `preferred`.
2. Restart the open run: `assist_pipeline/pipeline/update` of "oye Kivi" with its own fields unchanged; c3-a
   starts a "Kiwi" (`alexa`) run within about 15 s (check with `assist_pipeline/pipeline_debug/list`).
   `wake_release.py rollback --tv c3-a` does both and checks the new run (no approval flag: a rollback is always allowed).
3. Optional: delete the pipeline "oye Kivi" (`assist_pipeline/pipeline/delete`). The models stay loaded; to
   drop `oye_kivi` from the cluster, pin `v1-2026-11-05@sha256:9e51e0c9…` again with the old init command.

**Data:** no new dataset. The trial's wake-word audio is HA's existing debug recordings of c3-a (data register,
dataset 1 and the `/voice-recordings` rows), read only by the owner-run extraction tools. Counts only in reports.

**Release driver:** the whole phase is scripted in
[`wake_release.py`](../../../scripts/voice-assistant/wakeword/wake_release.py) (record-check, extract, score,
pick-threshold, calibrate, gate, package, release, deploy, enable, rollback, status); the runbook is
`docs/runbook/wake-word-release.md` in the private `agil/kivi-models`. The v4 release state was recorded with it:
`release`, `deploy` and `enable` ran idempotently against what was already done (no second tag, no new commit),
and `enable` restored the trial after the restart above.

**Next (owner):** record real "oye Kivi" takes (`wake_release.py record-check --version v4-oye-kivi-2026-11-07`
prints the exact commands), then `score` and `gate` on them; then re-select on more room hours.

### Promotion to the household wake word (2026-11-07, 19:59 UTC)

**Owner decision (2026-11-07, evening):** "We have to deploy the oye Kivi as the general wake word." The gate is
**not passed** (room false wakes 0.96/h [0.42, 1.90] on the test part; real recall untested). The owner also
reported false wakes on the c3-a trial: about 9 wakes in about 1.1 h, of which at least 2 were likely room noise
(owner observation, not measured by a tool; nothing was listened to here).

**Change (websocket API, short-lived login revoked afterwards; `wake_release.py promote`):**
- Pipeline "Kiwi" (preferred): `wake_word_id` `alexa` → `oye_kivi`; every other field unchanged (diff of
  `pipeline/list` before and after: only `wake_word_id`). Updating it ended the open "Kiwi" runs.
- c3-a's pipeline select: `oye Kivi` → `preferred`, then a no-op update of "oye Kivi" ended its open run.
  A new "Kiwi" run started at 19:59:21 UTC. Since both selects are `preferred`, an HA restart that resets the
  selects no longer changes the wake word.
- c3-b: select `preferred` (so `oye_kivi`); its microphone is muted by Home Assistant (left as it is), so it
  starts no run until it is unmuted.
- **The select restore is stale in both directions** (observed-on-cluster): after the dashboard's HA restart at
  20:02 UTC c3-a's select came back as `oye Kivi` (its value before the promotion), as at 18:35 it had come back as
  `preferred` (its value before the trial). Home Assistant restores this select from an older saved state, not
  the last one. `promote` was run again (c3-a → `preferred`, new "Kiwi" run seen) and the now redundant pipeline
  "oye Kivi" was deleted (`rollback --tv c3-a --delete-pipeline`), so a stale restore can only fall back to
  `preferred`. The wake word was `oye_kivi` throughout.
- The dashboard's trial select was replaced by the household wake word and both selects' state (another HA
  restart through Reloader, harmless now).
- Snapshot before: `releases/v4-oye-kivi-2026-11-07/ha-before-promote.json`.

**Rollback (exact):** `wake_release.py demote --version v4-oye-kivi-2026-11-07`, or by hand
`assist_pipeline/pipeline/update` of "Kiwi" with every field unchanged except `wake_word_id: alexa`. The update
itself ends the open runs, so both TVs switch within about 15 s. `alexa` and `kivi` stay loaded.

**Stricter threshold without a new image? (investigated, nothing changed):**
- wyoming-openwakeword 2.1.0 (source in the pod) has **no per-model threshold**: `--threshold` (default 0.5),
  `--trigger-level` (default 1) and `--refractory-seconds` (default 2) are global. Home Assistant 2026.10.4 has
  no wake-word sensitivity in the pipeline or the Wyoming integration: a pipeline sends only
  `Detect(names=[wake_word_id])`.
- **Global `--threshold 0.745`** gives `oye_kivi` exactly raw 0.98 (calibrated file: server t = sigmoid(logit(raw)
  − logit(0.9437)); raw 0.97 = 0.659, raw 0.99 = 0.855). A values change (openwakeword args), no new image,
  openwakeword restart only. It also makes `alexa` stricter (0.745 instead of 0.5) and `kivi` (raw 0.993); neither
  is used by a pipeline now, but the `alexa` rollback would then wake less (its recall at 0.745 was not
  measured; it should go back to 0.5 with the rollback).
- **`--trigger-level N`:** in 2.1.0 the counter is not reset when the score drops, only at the start of a run, so
  N counts above-threshold 80 ms steps anywhere in a run, not consecutive ones. Not measured offline; not
  recommended as a precise lever.
- **Recalibrated file** (`oye_kivi.tflite` with raw 0.98 at 0.5): `alexa` untouched, but a new version directory,
  a new tag (needs the owner's approval) and a new image; `wake_release.py init/pick-threshold --threshold 0.98
  --reason .../calibrate/package/release/deploy` does it.
- **What raw 0.98 means** (v4 DET table, same runs; 0.98 was read off the test part, so it is an owner choice, not
  a clean selection): room test part 1 false wake in 8.31 h = 0.12/h [0.00, 0.67] against 8 = 0.96/h [0.42,
  1.90]; held-out synthetic recall 57.5 % clean (72.8 %), 30.8 % with TV at 10 dB (47.2 %), 15.1 % at 0 dB
  (23.9 %), 42.3 % quiet (61.8 %); near-phrases 4.1 % (8.9 %). Real recall at either threshold is unknown; the
  owner's trial count (at least 2 false in about 1.1 h) is above both offline rates, so the room may differ
  from the test part (one evening, one TV).

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
