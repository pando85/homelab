#!/bin/bash
# shellcheck disable=SC2086  # command prefixes in variables are split on purpose
# Kivi v6.2 morning comparison against v5, the live model (pre-registered 2026-11-11, amendment 2; v6 = information). Odd-day honest sets only; test2-v5era and the
# 2026-11-11 session are REUSED test sets. Needs v6p2/selection.json (the chosen arm and threshold on 2026-11-10).
set -e
H=$HOME/homelab/scripts/voice-assistant/wakeword
W=/work/workspaces/kivi-wakeword
cd $W
read -r ARM THR < <(python3 -c "import json; c=json.load(open('v6p2/selection.json'))['chosen']; print(c['arm'], c['threshold'])")
C=v6p2/runs/$ARM
V6=v6/runs/v6m/oye_kivi_uncalibrated
V5=releases/v5-oye-kivi-2026-11-08/model/oye_kivi_next_uncalibrated
O=v6p2/compare; mkdir -p $O; chmod 700 $O
PY=".venv/bin/python -I"
SCOPE="systemd-run --user --scope -p MemoryMax=8G -p CPUQuota=${CPU:-400}% -- nice -n 10"
# C3: both room sets in one TFLite pass (as the v5-era mining pass)
$SCOPE env OMP_NUM_THREADS=1 $PY $H/v6_replay.py --src negatives-v5era:test2-v5era --src "negatives-1111eve:*" \
  --model v5=$V5.tflite:0.9754 --model v6=$V6.tflite:0.9595 --model v62=$C/oye_kivi_uncalibrated.tflite:$THR \
  --jobs "${JOBS:-4}" --out $O/room.json
# C1 / C2: the 2026-11-11 session
$SCOPE $PY $H/kivi_score.py --session datasets/real/20261111T171918Z-spk01-tv-c3a --model oye_kivi_next=$V5.tflite --model v6m=$V6.tflite \
  --model v62=$C/oye_kivi_uncalibrated.tflite --thresholds 0.9754,0.9595,$THR --also-pc --json $O/score_1111.json --md $O/score_1111.md
# C4: test-day live clips
$PY $H/v6_live_eval.py --role test --model v5=$V5.tflite:0.9754 --model v6=$V6.tflite:0.9595 --model v62=$C/oye_kivi_uncalibrated.tflite:$THR \
  --json $O/live_test.json > $O/live_test.txt
# C5: held-out-voice synthetic sets
$PY $H/v6_seq_eval.py --model v5=$V5.onnx:0.9754 --model v6=v6/runs/v6m/kivi.onnx:0.9595 --model v62=$C/kivi.onnx:$THR \
  --set clean=vnext/feat/eval_pos_clean.npz --set tv10=vnext/feat/eval_pos_snr10.npz --set quiet=vnext/feat/eval_pos_quiet.npz \
  --json $O/synth.json > $O/synth.txt
echo COMPARE_DONE
