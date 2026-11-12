#!/bin/bash
# shellcheck disable=SC2086,SC2046  # flag lists in variables and $(house) are split on purpose
# Kivi v6.3 (pre-registered 2026-11-12, kivi-models docs/plan/2026-11-false-wake-plan.md section 9): the v6.2 recipe with
# the retry rule undone, every even-day false live clip and labelled window as a household negative, the conversation
# after false wakes and the v6.2-mined room events. No level or floor augmentation (the section 9.1 diagnosis does not support one).
# Training data: even days only (room 2026-11-05 grandfathered, -06, -08; live clips and windows 2026-11-08 and -10).
# 2026-11-10 room audio is held out for the threshold; nothing of 2026-11-12 or of an odd day is read.
#   p3a  warm start v6 (oye_kivi_ctx), household weights as below
#   p3b  warm start v5 (oye_kivi_next), same data and weights as p3a
#   p3c  warm start v6, household negatives (hard, conv, winneg) doubled per step and in loss weight
set -e
cd "${KIVI_HOME:-$HOME/kivi-models/data}"
H=$HOME/homelab/scripts/voice-assistant/wakeword
PY=".venv/bin/python -I"
F=v6/feat; Q=v6p2/feat; RQ=v6p2/real; P=v6p3/feat; RU=v6p3/runs; mkdir -p $RU; chmod 700 $RU
BASE="$PY $H/train_simple.py --work $PWD --pos-train $Q/pos_train.npy --neg-train $Q/near_train.npy --pos-test $F/pos_test.npy --neg-test $F/near_test.npy --acav data/acav/acav_sub400k.npy --mls v2/feat/T_mls_train0_w8.npy --layer 64 --lr 2e-4 --steps 4000 --adv 128 --seed 2 --n-pos 192"
NEG="--extra-neg v2/feat/hard_r1.npy:128 --extra-neg $P/room_train_w8.npy:128 --extra-neg v4/feat/mls_hard4.npy:128 \
 --extra-neg v6/real/neg_kivi.npy:64:6 --extra-neg v6/real/neg_near.npy:32:6 --extra-neg v6/real/neg_bg.npy:32"
POS="--extra-pos $RQ/real_tv_train.npy:96 --extra-pos $RQ/real_pc_train.npy:48 --extra-pos $RQ/real_webcam_train.npy:16"
CTX="--extra-pos $Q/ctxp_syn_pos_tr.npy:32 --extra-neg $Q/ctxp_syn_negpart.npy:32:3 --extra-neg $Q/ctxn_syn_conf.npy:64:6"
HARD="--extra-neg $P/hard_v6x_day.npy:48:12 --extra-neg $P/hard_v6x_evening.npy:32:24 --extra-neg $P/hard_v5.npy:128:6"
# household (even-day live clips and windows): positives per speaker and attempts; negatives; $1 = 1 (p3a/p3b) or 2 (p3c)
house() {
  local k=$1 L=$P/live_std
  echo "--extra-pos ${L}_pos_spk01.npy:24 --extra-pos ${L}_pos_spk02.npy:16 --extra-pos ${L}_pos_att.npy:8 \
 --extra-neg ${L}_hard.npy:$((48 * k)):$((12 * k)) --extra-neg ${L}_negpart.npy:16:3 \
 --extra-neg ${L}_conv.npy:$((32 * k)):$((6 * k)) --extra-neg ${L}_winneg.npy:$((16 * k)):$((6 * k))"
}
V6=v6/runs/v6m/kivi.onnx; V5=vnext/runs/vn_h_c120_w64/kivi.onnx
run() { n=$1; shift; date +%T; echo "$n"; [ -f "$RU/$n/kivi.onnx" ] || $BASE "$@" --out "$RU/$n" > "$RU/$n.log" 2>&1 || echo "FAIL $n"; }
run p3a --init $V6 $POS $NEG $CTX $HARD $(house 1)
run p3b --init $V5 $POS $NEG $CTX $HARD $(house 1)
run p3c --init $V6 $POS $NEG $CTX $HARD $(house 2)
for n in p3a p3b p3c; do
  .venv-tf/bin/python $H/export_tflite.py $RU/$n/kivi.onnx $RU/$n/oye_kivi_uncalibrated.tflite --check-features $F/pos_test.npy 2>&1 | tail -1
done
date +%T; echo TRAIN_DONE
