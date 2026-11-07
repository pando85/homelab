# Custom wake word "kivi" (openWakeWord)

Scripts to train and evaluate a custom [openWakeWord](https://github.com/dscripka/openWakeWord)
model for the Spanish wake word "kivi" (/ˈki.βi/) and to compare it with the stock `alexa` model,
offline on a PC. The method, the results and the deployment notes are in
[docs/voice-assistant/experiments/kivi-wake-word.md](../../../docs/voice-assistant/experiments/kivi-wake-word.md).

Nothing here plays or records audio. Datasets, generated clips, features and trained models stay in
a private work directory outside git (`$WORK`). They are not committed: the datasets and the Piper
voices have their own licenses, and the negative features (ACAV100M) are CC BY-NC-SA 4.0, so a
model trained with them carries non-commercial share-alike terms (the same as the stock
openWakeWord models).

| File | Purpose |
| --- | --- |
| `kivi_data.yaml` | Voices (train / held-out split), pronunciation variants, Spanish adversarial words and sentences |
| `gen_clips.py` | Synthetic clips with Piper ONNX voices, rendered from Spanish phonemes (also for English, Italian, Portuguese and Catalan voices, for speaker variety) |
| `prep_data.py` | Dataset preparation: partial ACAV100M feature file, MIT RIRs, background clips, Spanish speech features (MLS), long evaluation streams (MLS, VoxPopuli) |
| `augment_set.py` | Augmentation and features of one clip set: openWakeWord's `augment_clips`, or `--mode simple` (SNR on the word; used for the final model) |
| `train_simple.py` | **Final training:** the openWakeWord DNN head with a plain balanced loop, fixed negative weight, optional mined hard negatives |
| `kivi.yaml`, `train.sh`, `train_driver.py`, `stub/` | The official `train.py` / `auto_train` path (collapsed for "kivi", kept for reference and comparison) |
| `export_tflite.py` | ONNX to TFLite for the openWakeWord DNN (no `onnx_tf`), output check, `--calibrate-threshold T` (T becomes 0.5) |
| `stream_mel.py` | Melspectrogram model with a per-row max, so batched features equal the server's per-chunk features |
| `fast_eval.py` | Embedding cache (`embed --stream-onnx`: server-exact, GPU), model ranking (`score`), hard-negative mining (`mine`) |
| `fast_report.py` | **Reported numbers:** recall per voice and word, false accepts per hour with intervals; refuses features that are not server-exact |
| `eval_stream.py` | The server's runtime itself (`pyopen-wakeword`, TFLite C API): per-clip and long-audio streaming; slow, used for `alexa` and for checks |
| `test_server_exact_features.py` | Regression test: server streaming vs batched server-exact features, and the report's refusal of other features |

**Never report numbers from batch features** (`AudioFeatures.embed_clips`, `fast_eval.py embed`
without `--stream-onnx`): openWakeWord's melspectrogram clamps at max - 80 dB over its whole input,
so batch features differ from what the server computes per 80 ms chunk. See
[lessons learned](../../../docs/voice-assistant/lessons-learned.md).

## Setup (PC, GPU optional)

```sh
export WORK=/work/workspaces/kivi-wakeword    # private, outside git
H=$PWD/scripts/voice-assistant/wakeword
mkdir -p $WORK/src $WORK/data && cd $WORK
git clone --depth 1 https://github.com/dscripka/openWakeWord.git src/openWakeWord
uv venv -p 3.11 .venv && . .venv/bin/activate
uv pip install torch torchaudio --index-url https://download.pytorch.org/whl/cu124
uv pip install "numpy<2" "scipy<1.13" "setuptools<70" "pyarrow<18" mutagen==1.47.0 torchinfo==1.8.0 \
  torchmetrics==1.2.0 speechbrain==0.5.16 audiomentations==0.33.0 torch-audiomentations==0.11.0 \
  acoustics==0.2.6 pronouncing==0.2.0 pyyaml tqdm soundfile onnx piper-tts requests scikit-learn "pytest<8"
uv pip uninstall onnxruntime && uv pip install onnxruntime-gpu==1.20.2   # piper-tts pulls the CPU build
uv pip install --no-deps -e src/openWakeWord pyopen-wakeword==1.1.0
python -c "from openwakeword.utils import download_models as d; d(['alexa'])"   # feature models
uv venv -p 3.11 .venv-tf && uv pip install -p .venv-tf/bin/python tensorflow-cpu==2.16.2 "numpy<2" onnx==1.16.2 onnxruntime
```

Data (about 12 GB downloaded; sizes as of 2026-11):

| Data | Source | License | Use |
| --- | --- | --- | --- |
| ACAV100M features, first 8 GiB of 17.3 GB (HTTP range, about 990 h) | `davidscripka/openwakeword_features` | CC BY-NC-SA 4.0 | general negatives (`prep_data.py acav-trim` fixes the header) |
| `validation_set_features.npy` (185 MB, 11.3 h) | same | CC BY-NC-SA 4.0 | false-positive validation during training |
| MLS Spanish train shards 0-2 (1.45 GB) | `facebook/multilingual_librispeech` | CC BY 4.0 | Spanish speech negatives (20 h of shard 0 as features) and background clips (1500 x 10 s of shard 2) |
| MLS Spanish dev and test (315 MB, 10 h each) | same | CC BY 4.0 | evaluation only (speakers disjoint from train) |
| VoxPopuli Spanish test (994 MB, 4.6 h) | `facebook/voxpopuli` | CC0 | evaluation only |
| AudioSet balanced train shard 00 (688 MB, 500 clips) | `agkphysics/AudioSet` | CC BY 4.0 | background noise and music |
| MIT environmental impulse responses (270) | `davidscripka/MIT_environmental_impulse_responses` | not stated on the card | reverberation |
| Piper voices (es_ES, es_MX, es_AR, en_US LibriTTS-R, en_GB VCTK, it_IT, pt_BR, ca_ES) | `rhasspy/piper-voices` | per model card: CC0, CC BY 3.0/4.0, CC BY-SA, Apache-2.0, Unlicense, public domain | synthetic positives and adversarial negatives |

```sh
HF=https://huggingface.co/datasets
curl -L -r 0-8589934591 -o data/acav/acav_part.npy $HF/davidscripka/openwakeword_features/resolve/main/openwakeword_features_ACAV100M_2000_hrs_16bit.npy
python -I $H/prep_data.py acav-trim data/acav/acav_part.npy
python -I $H/prep_data.py rirs data/mit_rirs
python -I $H/prep_data.py background data/audioset/bal_train_00.parquet --out data/bg_audioset
python -I $H/prep_data.py background data/mls_es/train-00002.parquet --out data/bg_mls_es --max 1500
python -I $H/prep_data.py features data/mls_es/train-00000.parquet --out data/feat_mls_es.npy --max-hours 20
python -I $H/prep_data.py evalaudio data/mls_es/test.parquet --out eval_audio/mls_es_test --name mls_test
```

## Generate, train, evaluate (what produced the reported model)

```sh
G="python $H/gen_clips.py --data $H/kivi_data.yaml --voices-dir data/voices"
$G --out train_out/kivi --set positive_train --n 60000   # also positive_test 3000, negative_train 60000, negative_test 3000
$G --out eval_clips --set eval_kivi --n 1800             # held-out voices; also eval_alexa, eval_nearmiss
sed -e "s#@WORK@#$WORK#g" -e "s#@STUB@#$H/stub#g" $H/kivi.yaml > kivi.rendered.yaml

# augmentation with SNR on the word (5-30 dB), without the "aquí vi" family in the negatives
A="python $H/augment_set.py --config kivi.rendered.yaml --mode simple --snr 5,30 --p-bg 0.7"
$A --set positive_train --manifest train_out/kivi/positive_train.manifest.csv --out train_out/kivi/positive_features_train_simple2.npy
$A --set negative_train --manifest train_out/kivi/negative_train.manifest.csv --exclude-regex '^(y )?aquí vi' \
   --out train_out/kivi/negative_features_train_simple2.npy          # and the two *_test sets

# hard negatives from 31 h of other Spanish speech, scored by a first model
python -I $H/prep_data.py evalaudio data/mls_es/train-00001.parquet --out mine_audio/mls_train1 --name mls_train1
python -I $H/fast_eval.py embed mine_audio/mls_train1 --out emb/mls_train1.npz
python -I $H/fast_eval.py mine --model runs/first/kivi.onnx --emb emb/mls_train1.npz --out emb/hard_r1.npy --min-score 0.05

python $H/train_simple.py --work $WORK --out runs/s5 --layer 64 --steps 6000 --adv 64 \
   --pos-suffix _simple2 --neg-suffix _simple2 --extra-neg emb/hard_r1.npy:128
.venv-tf/bin/python $H/export_tflite.py runs/s5/kivi.onnx final/kivi.tflite --calibrate-threshold 0.98

# server-exact evaluation
python $H/stream_mel.py src/openWakeWord/openwakeword/resources/models/melspectrogram.onnx melrow.onnx
E="python -I $H/fast_eval.py embed --stream-onnx melrow.onnx,src/openWakeWord/openwakeword/resources/models/embedding_model.onnx"
$E --clips eval_clips/eval_kivi --manifest eval_clips/eval_kivi.manifest.csv --out emb_s/kivi_clean.npz
$E --clips eval_clips/eval_kivi --manifest eval_clips/eval_kivi.manifest.csv --bg-dir eval_audio/p2_content --snr-db 10 --out emb_s/kivi_snr10.npz
$E eval_audio/mls_es_test --out emb_s/L_mls_es_test.npz          # and every other long-audio set
python -I $H/fast_report.py --model runs/s5/kivi.onnx --clips clean=emb_s/kivi_clean.npz \
   --long mls_es_test=emb_s/L_mls_es_test.npz --json results/report.json
python -m pytest $H/test_server_exact_features.py                 # needs the training venv
```

`gen_clips.py` resumes an interrupted set (the plan is deterministic per seed). Voices that babble on a
single word (the MLS "low" Spanish voices and `ca_ES-upc_pau-x_low`) are excluded; clips much longer
than their phoneme count allows are synthesized again. `eval_stream.py` runs the server's own
runtime and is the reference for `alexa`; it is slow (about 14x real time per process on an idle
machine, far slower under load).
