# Recovery plan: unreliable "oye Kivi" wake word

**Status (2026-11-08): P0-P3 executed; the new candidate is on a one-device trial.** [P0](oye-kivi-p0-baseline.md)
measured the deployed baseline, [P1/P2](oye-kivi-vnext-ablations.md) closed the filter replay and the
ablations, and P3 ran the gate: **incomplete** — every number the gate can measure passes (room false
wakes 0.12/h against a 0.25/h budget, 1/54 confuser takes against v4's 10/54, 75 % recall on the held-out
session against v4's 25 %), and what it cannot measure is real data: one speaker and no `test`-split takes. Per this plan,
that means **a trial on one device, not promotion**: `oye_kivi_next` `v5-oye-kivi-2026-11-08` is live on
c3-a since 11:56 UTC, c3-b and the household "Kiwi" pipeline still run v4, and rollback is one command.
What is left is data, not modelling: a later-day `--split test` session, spk02, and ~15 h per device of
fresh room exposure. Source of measured baselines: [v4 experiment](kivi-wake-word.md#v4-oye-kivi-2026-11-07-candidate-not-deployed), [promotion and rollback](kivi-wake-word.md#promotion-to-the-household-wake-word-2026-11-07-1959-utc), and [model registry](../model-registry.md).

## Why this is a recovery, not a threshold tweak

The shipped v4 model uses **raw 0.9437 = Wyoming 0.5**. It has **no measured real "oye Kivi" recall**. On held-out *synthetic* voices, recall was 72.8% clean, 47.2% with TV at 10 dB SNR, 23.9% at 0 dB and 61.8% for quiet speech. In the **real room audio test set**, 8 false wakes in 8.31 h = **0.96/h** (c3-a: 2/4.11 h, c3-b: 6/4.20 h), above the owner target of 0.25/h. The reported ~9 wakes in ~1.1 h of live trial activity were **not all verified false positives**; do not use this as a measured FA/h rate.

At raw 0.98 the same offline room test has 1 false wake (0.12/h), but synthetic clean/10 dB recall drops to 57.5%/30.8%. **This operating point was inspected on the test set, so it is not an unbiased model/threshold selection.** Do not silently change the global Wyoming `--threshold`: it affects `alexa` and all other models. If immediate reliability is unacceptable, the owner can revert the Kiwi pipeline to `alexa` using `wake_release.py demote --version v4-oye-kivi-2026-11-07` (requires live HA access and confirmation).

## Changes prepared in this PR

- `vnext_oye_kivi.yaml` is a **candidate-only** copy of the v4 synthetic-data recipe. Keep `v4_oye_kivi.yaml` unchanged so the current model can be reproduced. Remove the phoneme variant for **"oiga Kivi"** from *positive* labels: the target is specifically "oye Kivi". Rebalance the five existing "oye" variants to 100%.
- Give higher candidate negative sampling weight to **"fue Kivi", "ojo Kivi", "oye que vi" and "oye aquí"**, the measured v4 confusers. Keep "oye kiri" at its previous weight: some TTS renderings collapse onto the target. Do **not** introduce "oiga Kivi", "oye kiwi", "hey Kivi" or other phonetically ambiguous negatives until the acoustic checks confirm they are distinguishable. Changing weights is an **ablation hypothesis**, not proven improvement.
- Strengthen `wake_release.py gate`: require at least two distinct test speakers; at least ten real held-out takes per speaker **in each** TV-on and TV-off condition; score the false-wake budget separately per room device as well as pooled; refuse an incomplete result. The gate still reports both point-estimate and 95% CI status. Owner-approved trial overrides in the release driver remain possible and explicit. No change to the running model, threshold, satellites, Helm values or HA state.

## Execution order and acceptance

### P0 — Capture a clean real baseline (owner + speaker consent)

1. Use **the currently deployed v4** at its existing calibration. Record `oye Kivi` in a `train` session, then a **separate later `test` session** for each consenting speaker. Aim for 30–50 positive takes **per TV-off/TV-on condition** per person, at 1.5 m and 3 m, normal and quiet volume. The code gate minimum of 10 per condition is only a guard against absent data, **not** a statistically adequate sample size.
2. Test c3-a and c3-b as distinct devices; for each capture, retain the exact satellite wake-stage audio, PC/webcam reference where available, conditions (TV source, distance, volume) and model/config checksum. Collect near-phrases, including "Kivi" alone, "oye kiri", "oye kili", "oye que vi", "fue Kivi", "ojo Kivi" and normal Spanish speech.
3. Label accidental activations from the HA event/button workflow as *confirmed*, *possible* or *not false*. Count only confirmed false wakes per exposed hour. Record **silent/no-stream/partial** cases independently so capture failures are not mislabeled as classifier misses.
4. Keep identifiable recordings in the existing restricted local dataset, not GitHub, the PR, CI artifacts, cloud STT or log outputs. Follow the [data register](../data-register.md); no new recording process is enabled by this PR.

Useful existing owner-run commands (replace placeholders with local paths/devices; run the second session with `--split test`, never reassign test recordings to train):

```bash
cd scripts/voice-assistant/wakeword
python3 kivi_session.py --phrase "oye kivi" --model-name oye_kivi \
  --device c3-a --speaker spk01 --split train \
  --desk-mic '<alsa-desk-source>' --webcam-mic '<alsa-webcam-source>'
python3 kivi_extract.py --session-dir ~/kivi-sessions/<session-id> --dry-run
python3 kivi_extract.py --session-dir ~/kivi-sessions/<session-id>
python3 wake_release.py record-check --version v4-oye-kivi-2026-11-07
```

The release driver prints the configured device-specific button-take commands; use it instead of manually guessing HA entity ids. Confirm each speaker's consent first. **Do not score/train the test split during candidate development.**

**Exit:** real baseline shows detection rates by speaker, device, 1.5/3 m and TV condition; at least two speakers represented; annotated false-wake examples; enough held-out time to report credible uncertainty.

### P1 — Diagnose where the signal is lost (before retraining)

- Replay **the same recordings** through (A) clean reference microphone, (B) original satellite wake stream, and (C) the supported HA-side filter mirrors (`ha_filter_mirror.py`: WebRTC NS / RNNoise / Speex). Check alignment, peak/RMS and clipping; do not compare different spoken takes.
- Where available, compare raw TV mic channel(s), AEC reference and the processed uplink. Diagnose AEC, single-channel pickup and fixed +24 dB gain independently; do not assume a second NS pass always helps. The current HA 2026.10.4 integration cannot select another wake audio channel: [audio pipeline plan](../audio-pipeline-plan.md).
- Compare scores over time, not only binary activations. Identify whether the failure is (a) missing/partial signal or too-late wake boundary, (b) low classifier score on clear "oye Kivi", or (c) confuser and room false activations.

**Exit:** a source-attributed failure table and a chosen front-end **profile**; no signal-processing change without a matched-model replay result. Coordinate satellite-side changes with [webos-voice-satellite](https://github.com/pando85/webos-voice-satellite).

### P2 — Prepare better candidates and independent evidence

1. Generate **a new versioned dataset** from `vnext_oye_kivi.yaml` using existing `v4_gen.py gen` and `v4_augment.py`; do not overwrite the historical v4 clips/manifests. Audit phonemes, unintelligible Piper samples, duplicate speaker ids, durations, long/short or multi-burst outputs, level distribution and target/negative acoustic collisions (`v4_gen.py check`). Discard anomalous renders before feature extraction.
2. Train a **strict-target ablation** (remove "oiga" only), then a **confuser-weight ablation**, then combinations with consented real positives plus real room/hard negatives. Compare with v4 under the **same** capture front end and threshold-selection budget. Try a fresh head as a baseline against the warm-started v2 head; do not assume the latter generalizes.
3. Partition real data **by speaker and recording session**. Keep at least one fully unseen session per person and, where feasible, a speaker never used for training. A replay-derived/augmented sample must stay in its original split. Separate chronological room `train`, `selection`, and **new future `test`** sets per device. The v4 room test was already inspected for raw 0.98 and cannot become a fresh unbiased test for the new choice.
4. Select the threshold only on the selection part at an explicit false-wake budget. Freeze model file, SHA256, front-end profile and threshold before one-shot testing. Use `pyopen-wakeword` at 16 kHz / 80 ms, 2 s refractory; never claim a batch-feature score is server-exact. Compare false-wakes/hour with exact Poisson CI and recall with Wilson CI.

**Exit:** a candidate model card with immutable dataset/model hashes, split provenance, per-speaker/device confusion breakdown, threshold and fresh holdout scores. Don't label a synthetic improvement as a real-world improvement.

### P3 — Gate, controlled trial, rollback

The release driver's acceptance thresholds are **real held-out recall >=80% TV off, >=60% TV on** for each speaker and pooled; **confirmed room false wakes <=0.25 per hour** for each TV and pooled. Point-estimate passing is distinct from **95% confidence**: publish both. Require at least 2 test speakers, 10 takes in both TV conditions each and room exposure from two devices. More exposure is needed for narrow Poisson intervals (with zero events, a two-sided 95% upper bound falls below 0.25/h only after roughly 15 h per device). Check near-phrases separately; especially "Kivi" alone must not trigger.

Run the existing `wake_release.py score --version <new-release>` and `wake_release.py gate --version <new-release>` after collecting the evidence. If the gate is incomplete or fails, **do not promote to both TVs**. Trial one device only with explicit owner consent, log real hits/false wakes and rollback if necessary. Only package/tag/deploy the immutable model after independent tests and approval; the rollback keeps `alexa` available. Never use the same test part repeatedly to tune thresholds.

## Follow-ups outside this PR

- **Owner:** consented real sessions for both speakers and both TVs; event labels and a fresh chronological room recording.
- **Training workstation:** regenerate datasets, inspect rendered phonemes, run ablations and a fresh selection/test split; publish **aggregate metrics only** and keep audio/models private.
- **Satellite repo:** raw vs processed synchronized stereo/audio-reference replay, AEC/NS/gain A/B, and per-device CPU/latency budget. Avoid double noise suppression until measured.
- **HA/GitOps:** add unobtrusive false-wake and stage-drop metrics without logging speech or names; device-class model/pipeline selection only after the replay identifies meaningful differences.

## Validation of this PR

```bash
cd scripts/voice-assistant/wakeword
python -m pytest test_oye_kivi_profile.py test_wake_release.py -q
```

Static/unit tests require no microphone, cluster access or real recordings. They do **not** prove model accuracy. No new model has been trained, no real voice has been captured, and no deployment or HA changes have been executed in this PR.
