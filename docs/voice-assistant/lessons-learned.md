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

## Whisper: `--debug` logs the whole argument namespace, including the Home Assistant token

- **Symptom:** none yet. Found while designing how to log transcripts for the usage study.
- **Root cause:** `wyoming-faster-whisper` prints its parsed argument namespace at DEBUG, and
  `--debug` turns DEBUG on for every library. With `--hass-token` read from
  `WYO_WHISPER_HASS_TOKEN_FILE`, the namespace carries the Home Assistant token (an administrator
  token: Home Assistant tokens have no scopes), and it would reach the pod log and Loki.
- **How detected:** reading the server source and the first canary design, which suggested `--debug`
  to see the composed prompt and transcripts.
- **Fix:** never run the production pod with `--debug`. The transcript logger is lowered to DEBUG on
  its own, from `apps/home-assistant/config/whisper-logging/sitecustomize.py` (loaded through
  `PYTHONPATH`): root logger at INFO, only `wyoming_faster_whisper.dispatch_handler` at DEBUG.
- **Guard:** the per-logger `sitecustomize.py` is the only way DEBUG is enabled; its header says why.
  [whisper-image-build.md](whisper-image-build.md) and the
  [log policy](stt-hint-list-and-learning.md#8-log-policy-voice-transcripts-in-loki-temporary-exception)
  forbid pasting `--debug` output.

## Whisper: wyoming-faster-whisper 3.8.1 renamed the transcript logger, which silently broke the Vector drop regex

- **Symptom:** none visible. The Vector rule that drops voice transcripts before Loki stopped
  matching after the 3.8.1 image: a regex written for `INFO:wyoming_faster_whisper.handler:` no
  longer matched the new line, so the filter would have let transcripts through without any error.
- **Root cause:** 3.8.1 logs the transcript from `dispatch_handler.py` as
  `DEBUG:wyoming_faster_whisper.dispatch_handler:<text>` (2.x: `INFO:...handler:<text>`). The
  filter matched only `handler`.
- **How detected:** reading the 3.8.1 source while preparing the image, before the cutover.
- **Fix:** the `drop_voice_transcripts` condition in `system/loki/vector-agent.yaml` is now
  `^(INFO|DEBUG):wyoming_faster_whisper\.(dispatch_)?handler:`.
- **Guard:** the Vector unit tests in the same file
  (`whisper_transcript_line_is_dropped`, `whisper_debug_transcript_line_is_dropped`,
  `whisper_dispatch_handler_transcript_line_is_dropped`) cover the three line shapes with synthetic
  text. Run `vector test` after any Whisper version change. A logger rename is a privacy change.

## STT: GTX 1060 (sm_61) offers only float32, int8 and int8_float32 in CTranslate2

- **Symptom:** `large-v3-turbo` does not fit or does not run fast with the usual precisions.
- **Root cause:** on Pascal `sm_61` CTranslate2 lists only `float32`, `int8` and `int8_float32` as
  supported compute types (no usable `float16`). float32 `large-v3-turbo` does not fit in the
  card's 3 GB next to the display and other users (about 1.1 GiB is used by the int8 model).
- **How detected:** `ctranslate2.get_supported_compute_types("cuda")` on the target card and the
  documented CTranslate2 quantisation table; VRAM read from the GPU exporter.
- **Fix:** run `--compute-type int8` (executed as `int8_float32`). The same limit makes small
  fp16 models slow on this card; the decision-model study ran fp32.
- **Guard:** the canary step in [whisper-image-build.md](whisper-image-build.md) prints the
  supported compute types and fails on CUDA kernel errors before the real STT is touched, and the
  [registry](model-registry.md) records the precision of every model.

## Home Assistant: `assist_pipeline: debug_recording_dir` records continuous room audio with no cap or expiry

- **Symptom:** none yet; found while enabling request audio for Whisper fine-tuning and front-end
  work.
- **Root cause:** with a server-side wake word every pipeline run starts at the wake word stage, so
  the debug recording writes every chunk the satellites stream, continuously (about 115 MB per
  listening hour per satellite), not just the speech after a wake. Home Assistant has no size
  limit or expiry for that directory.
- **How detected:** reading the `assist_pipeline` debug recording code and the run layout
  (`00_wake-*.wav`, `01_stt-*.wav`).
- **Fix:** a dedicated 500 GiB volume outside `/config` and `/media`, enabled only on the owner's
  approval, with a daily cleanup.
- **Guard:** the CronJob `home-assistant-voice-recordings-cleanup` (90 days from the last write,
  oldest runs first above 90 % use) and rows 5 and 6 of the [data register](data-register.md),
  which state what is recorded and who may read it.

## Home Assistant: `.storage/homeassistant.exposed_entities` shows only manual overrides

- **Symptom:** reading the file suggested 0 entities were exposed to Assist, when 113 were.
- **Root cause:** the file stores only manual exposure overrides; entities exposed by default are
  not listed.
- **How detected:** the count differed from the websocket answer.
- **Fix:** use the websocket command `homeassistant/expose_entity/list`, as Whisper's name list and
  the analysis scripts do (it needs an administrator token).
- **Guard:** [STT hint list](stt-hint-list-and-learning.md) names the websocket call as the source
  of exposed entities; never infer exposure from `.storage`.

## Whisper logs: most DEBUG lines are "Sent info" replies, not voice traffic

- **Symptom:** about 90 % of the Whisper DEBUG lines looked like activity, so request counts from
  line counts were far too high.
- **Root cause:** Home Assistant polls the server with a Wyoming `describe` request regularly, and
  each reply logs "Sent info".
- **How detected:** counting line kinds in the pod log (counts only, no text read).
- **Fix:** count requests with the "Audio stopped" line, one per utterance.
- **Guard:** the analysis scripts count "Audio stopped", and the data register describes the
  stream's contents so counts are not taken from raw line totals.

## Forgejo: building and pushing the Whisper image

- **Symptom:** four separate failures in the first runs of the image workflow in
  `agil/wyoming-whisper`.
- **Root cause and fix:**
  1. Repository secret names cannot start with `FORGEJO_` (reserved prefix), so the planned
     `FORGEJO_PACKAGES_PAT` was rejected. Use another prefix.
  2. The automatic Actions token cannot push packages. A dedicated personal access token with only
     `write:package` is needed; it is created and set by the owner and kept in `pass`.
  3. The 1.64 GB layer push timed out. The push step needs a retry loop.
  4. The package cleanup rule (keep the last 3 versions) can only be set in the web UI, not by API.
- **How detected:** failed workflow runs and the registry responses.
- **Guard:** the workflow in the image repository carries the retry loop, the owner's checklist in
  [whisper-image-build.md](whisper-image-build.md) lists the token and the UI-only rule, and the
  image is pinned by digest in `apps/home-assistant/values.yaml`.

## Workstation: load stalls evaluations and a broad `pkill` killed other sessions' workers

- **Symptom:** evaluation runs slowed to a crawl and timed out, and a cleanup killed Python workers
  that belonged to other sessions.
- **Root cause:** the workstation is shared by several sessions. Under load (average 100 to 200)
  CPU-heavy evaluations stall, and a `pkill` with a generic pattern matches every Python worker, not
  just the caller's.
- **How detected:** load average and the other sessions' runs failing at the same moment.
- **Fix:** cap the worker count of every evaluation, and stop jobs by PID or process group only.
- **Guard:** rule for all sessions: never use a generic `pkill`/`killall` pattern. Start long jobs in
  their own process group and record the PID, so cleanup targets only that group.

## Whisper: the GTX 1060 cannot host a second Whisper instance

- **Symptom:** during a Whisper settings benchmark (2026-11-05, 18:45-18:53 UTC), live STT requests
  failed with CUDA out of memory and then "invalid device ordinal"; 67 of about 190 live requests took
  more than 5 s (up to 47 s) instead of about 0.9 s. A live AEC experiment on a TV was running at the
  same time and its transcript-based results were contaminated.
- **Root cause:** the benchmark assumed the live Whisper was idle and that a second large-v3-turbo
  int8 instance (peak about 1.8 GiB) fits next to the live one (about 1.1 GiB) on the 3 GiB card. The
  card reached 2967 of 3072 MiB, and the live service was not idle.
- **How detected:** the live Whisper log showed the CUDA errors; request latencies were split by
  overlap with the benchmark using log timestamps only (`whisper_bench/live_overlap.py`).
- **Fix:** the benchmark pod was deleted; the live service recovered without a restart.
- **Guard:** never run a second GPU model on the 1060 while the live service runs. Benchmark on the
  workstation GPU for accuracy, or schedule a short announced outage (live Whisper scaled to 0) for
  1060 latency numbers, and check that no live experiment is using voice at the time.
