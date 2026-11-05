# Lessons learned

One entry per pitfall met while building the voice assistant: what it looked like, why it
happened, how it was found, what fixed it, and what now guards against a repeat.

## Wake word: the server's features are not the training features

- **Symptom:** offline scores of a custom openWakeWord model disagreed with the server for the
  same clip, from 0.004 to 0.96. Model selection and threshold estimates based on them were wrong
  in both directions.
- **Root cause:** openWakeWord's `melspectrogram` model ends with a `ReduceMax` over its whole
  input tensor and clamps the log-mel at max - 80 dB. The server (`wyoming-openwakeword` 2.x,
  pyopen-wakeword) calls it on one 1760-sample chunk at a time, so the floor is set per 80 ms.
  Batch extraction (`AudioFeatures.embed_clips`, used for training and typical offline evaluation)
  sets the floor from the loudest sample of the whole batch.
- **How detected:** clip-by-clip comparison of the batched path against pyopen-wakeword streaming
  showed agreement for some voices and large gaps for others. A per-chunk TFLite run matched the
  server exactly, and the ONNX graph showed the `ReduceMax`.
- **Fix:** `scripts/voice-assistant/wakeword/stream_mel.py` makes the max per batch row.
  `fast_eval.py embed --stream-onnx` then feeds the server's 1760-sample chunks with its frame and
  window bookkeeping. This matches streaming to 0.032 in the embeddings and 0.002 in the score.
- **Guard:** `scripts/voice-assistant/wakeword/test_server_exact_features.py` checks streaming
  against batched (tolerance 0.05) and that plain batch features differ. `fast_report.py` refuses
  embeddings not tagged `server-exact`. Details:
  [kivi wake word, Pitfalls](experiments/kivi-wake-word.md#pitfalls).

## Wake word: default augmentation buries a short word

- **Symptom:** with the official openWakeWord recipe (`train.py`, `auto_train`), every "kivi" model
  collapsed to 2-9 % recall, even on its own training positives.
- **Root cause:** `augment_clips` mixes background at -10 to +15 dB SNR computed over the whole 2 s
  padded window. For a 0.45 s word, many positives end up at or below 0 dB on the word. `auto_train`
  then raises the negative weight to 1500x or more, which pushes every output down.
- **How detected:** ablations on cached features. A plain classifier on clean positives reached
  98 % held-out recall at about 1 false window per hour, on noisy positives 43 %, and with the
  near-word negatives 15 %. The word-to-background amplitude ratio of augmented positives was 1.0 to 2.4.
- **Fix:** augment with the SNR measured against the word (5 to 30 dB). Train the same DNN head
  with a fixed small negative weight and mined hard negatives (`augment_set.py --mode simple`,
  `train_simple.py`, `fast_eval.py mine`).
- **Guard:** the training history logs validation recall at fixed false accepts.
  [kivi wake word, Training](experiments/kivi-wake-word.md#training) records the choice.
