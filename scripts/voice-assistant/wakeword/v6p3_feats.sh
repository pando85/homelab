#!/bin/bash
# shellcheck disable=SC2086  # flag lists in variables are split on purpose
# Kivi v6.3 features (kivi-models docs/plan/2026-11-false-wake-plan.md section 9). Even days only; run in one systemd scope
# (MemoryMax=12G, CPUQuota=800%, nice 10, <= 7 jobs). Counts only.
#   1. room exclusions, hard negatives (retry rule undone), room negative windows of 2026-11-05/-06/-08
#   2. live clips and windows of 2026-11-08/-10 -> features through v6's real-clip chain (gain -6..+6 dB, TV interference
#      with p 0.4); no level or floor augmentation (section 9.1: not supported by the diagnosis)
set -e
cd "${KIVI_HOME:-$HOME/kivi-models/data}"
H=$HOME/homelab/scripts/voice-assistant/wakeword
PY=".venv/bin/python -I"
export V2_FE_TMP=$PWD/v6p3/tmp OMP_NUM_THREADS=1; mkdir -p $V2_FE_TMP v6p3/feat; chmod 700 v6p3 v6p3/feat $V2_FE_TMP
FE=v2/fe_build/release/examples/aec_offline
SO=emb_s_melrow.onnx,src/openWakeWord/openwakeword/resources/models/embedding_model.onnx
J=${JOBS:-7}
date +%T; echo exclude-hard-room
$PY $H/v6p3_data.py exclude
[ -f v6p3/feat/hard_v5.npy ] || $PY $H/v6p3_data.py hard
[ -f v6p3/feat/room_train_w8.npy ] || $PY $H/v6_room.py windows --index v6/room_emb/index_v6p2.json --part old-even,old-odd,even-trn \
  --stride 8 --exclude-json v6p3/room_exclude.json --out v6p3/feat/room_train_w8.npy
date +%T; echo live-cuts
$PY $H/v6p3_data.py live > v6p3/cuts/live_summary.txt
CX="$PY $H/v6_augment.py ctx --stream-onnx $SO --frontend $FE --jobs $J --bg data/bg_tv:3 --bg data/bg_mls_es:2 --snr 10,30 --rir-dir data/mit_rirs"
[ -f v6p3/feat/live_std_hard.npy ] || $CX --table v6p3/cuts/live.jsonl --out-prefix v6p3/feat/live_std --seed 6331
date +%T; echo FEATS_DONE
