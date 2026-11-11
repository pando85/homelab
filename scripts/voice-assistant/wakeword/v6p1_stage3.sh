#!/bin/bash
# shellcheck disable=SC2086  # flag lists in variables are split on purpose
# Kivi v6.1 STAGE 3 (the gate, run once). After 2026-11-13 has ended in Europe/Madrid (morning of 2026-11-14), with
# v6p1/selection.json from stage 2. Pre-registered 2026-11-11 (v6/results.md, homelab oye-kivi-v6.md "v6.1").
set -e
H=$HOME/homelab/scripts/voice-assistant/wakeword
W=/work/workspaces/kivi-wakeword
UNTIL=${UNTIL:?set UNTIL to an ISO UTC time about 15 min ago, e.g. 2026-11-14T07:00:00Z}
read -r ARM THR < <(python3 -c "import json; c=json.load(open('$W/v6p1/selection.json'))['chosen']; print(c['arm'], c['threshold'])")
CAND=$W/v6p1/runs/$ARM
V5=$W/releases/v5-oye-kivi-2026-11-08/model/oye_kivi_next_uncalibrated
cd $H
# 1. the rest of the copy (same command: existing pieces are skipped); 11-13 pieces are test3-v6p1
python3 kivi_negatives.py --hours 1000 --since 2026-11-11T16:52:39Z --until "$UNTIL" --exclude-pipeline "Kiwi Test" \
  --out-name negatives-v6p1 --part-test-day test3-v6p1 --part-mine-day sel-v6p1
python3 kivi_live_wakes.py extract
$W/.venv/bin/python -I kivi_live_wakes.py autolabel
cd $W; mkdir -p v6p1/gate; chmod 700 v6p1/gate
SCOPE="systemd-run --user --scope -p MemoryMax=12G -p CPUQuota=600% -- nice -n 10"
PY=".venv/bin/python -I"
# 2. C6 first: calibrated file (server 0.5 = THR), runtime check
mkdir -p v6p1/final
.venv-tf/bin/python $H/export_tflite.py $CAND/kivi.onnx v6p1/final/oye_kivi_ctx_uncalibrated.tflite --check-features v6/feat/pos_test.npy
.venv-tf/bin/python $H/export_tflite.py $CAND/kivi.onnx v6p1/final/oye_kivi_ctx.tflite --calibrate-threshold $THR --check-features v6/feat/pos_test.npy
$PY $H/v6_check_calibration.py v6p1/final oye_kivi_ctx $THR > v6p1/gate/c6_calibration.json
(cd v6p1/final && sha256sum -- *.tflite > SHA256SUMS)
# 3. C3a: fresh odd day 2026-11-13 (TFLite replay as the v5-era mining pass)
$SCOPE env JOBS=6 $PY $H/v6_replay.py --set negatives-v6p1 --part test3-v6p1 --local-date 2026-11-13 \
  --model v5=$V5.tflite:0.9754 --model cand=$CAND/oye_kivi_uncalibrated.tflite:$THR --gate cand:v5 --out v6p1/gate/c3a_test3.json
# 4. C3b: test2-v5era, the one final run (pass: total <= 5 + 1 = 6, evenings as in c3_verdict)
$SCOPE env JOBS=6 $PY $H/v6_replay.py --set negatives-v5era --part test2-v5era \
  --model v5=$V5.tflite:0.9754 --model cand=$CAND/oye_kivi_uncalibrated.tflite:$THR --gate cand:v5 --out v6p1/gate/c3b_test2.json
# 5. C1/C2: the 2026-11-11 session (and any later odd-day session: add --session lines)
$SCOPE $PY $H/kivi_score.py --session datasets/real/20261111T171918Z-spk01-tv-c3a --model oye_kivi_next=$V5.tflite \
  --model cand=$CAND/oye_kivi_uncalibrated.tflite --thresholds 0.9754,$THR --also-pc --json v6p1/gate/score_1111.json --md v6p1/gate/score_1111.md
# 6. C4: live clips of every test day (2026-11-09, -11, -13), exact replay
$PY $H/v6_live_eval.py --role test --model v5=$V5.tflite:0.9754 --model cand=$CAND/oye_kivi_uncalibrated.tflite:$THR \
  --json v6p1/gate/live_test.json > v6p1/gate/live_test.txt
# 7. C5: synthetic held-out voices
$PY $H/v6_seq_eval.py --model v5=$V5.onnx:0.9754 --model cand=$CAND/kivi.onnx:$THR --set clean=vnext/feat/eval_pos_clean.npz \
  --set tv10=vnext/feat/eval_pos_snr10.npz --set quiet=vnext/feat/eval_pos_quiet.npz --json v6p1/gate/synth.json > v6p1/gate/synth.txt
echo STAGE3_DONE  # then fill the C1-C6 PASS/FAIL table in v6p1/results.md by the pre-registered rules
