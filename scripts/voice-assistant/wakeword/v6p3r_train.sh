#!/bin/bash
# shellcheck disable=SC2086,SC2046  # flag lists in variables and $(house) are split on purpose
# Kivi v6.3 retry (pre-registered 2026-11-12, kivi-models docs/plan/2026-11-false-wake-plan.md section 11): the v6p3 recipe
# with the labelled household windows of every table, the re-filtered mined moments, 2026-11-12 and the "oye ..." decoys.
# Training data: 2026-11-05 to -08 and 2026-11-12; 2026-11-10 is the selection day (nothing of it is trained on).
#   r1  warm start v5 (oye_kivi_next)
#   r2  warm start v6 (oye_kivi_ctx), same data and weights as r1
#   r3  warm start v5, household negatives (hard, conv, winneg, hwin_eve, hwin_day) doubled per step and in loss weight
# Run under `flock $KIVI_HOME/v6p3r/heavy.lock` in a systemd scope (GPU; another job holds most of the VRAM).
set -e
cd "${KIVI_HOME:-$HOME/kivi-models/data}"
H=$HOME/homelab/scripts/voice-assistant/wakeword
PY=".venv/bin/python -I"
F=v6/feat; Q=v6p2/feat; RQ=v6p2/real; P=v6p3r/feat; RU=v6p3r/runs; mkdir -p $RU; chmod 700 $RU
BASE="$PY $H/train_simple.py --work $PWD --pos-train $Q/pos_train.npy --neg-train $Q/near_train.npy --pos-test $F/pos_test.npy --neg-test $F/near_test.npy --acav data/acav/acav_sub400k.npy --mls v2/feat/T_mls_train0_w8.npy --layer 64 --lr 2e-4 --steps 4000 --adv 128 --seed 2 --n-pos 192"
NEG="--extra-neg v2/feat/hard_r1.npy:128 --extra-neg $P/room_w8.npy:128 --extra-neg $P/room_1112.npy:32 --extra-neg v4/feat/mls_hard4.npy:128 \
 --extra-neg v6/real/neg_kivi.npy:64:6 --extra-neg v6/real/neg_near.npy:32:6 --extra-neg v6/real/neg_bg.npy:32"
POS="--extra-pos $RQ/real_tv_train.npy:96 --extra-pos $RQ/real_pc_train.npy:48 --extra-pos $RQ/real_webcam_train.npy:16"
CTX="--extra-pos $Q/ctxp_syn_pos_tr.npy:32 --extra-neg $Q/ctxp_syn_negpart.npy:32:3 --extra-neg $Q/ctxn_syn_conf.npy:64:6"
MINED="--extra-neg $P/mined_v6x_day.npy:48:12 --extra-neg $P/mined_v6x_eve.npy:32:24 --extra-neg $P/mined_v5.npy:128:6"
DECOY="--extra-neg v6p3r/decoys/feat/v6p3r_decoy_conf.npy:32:6"
# household: one positive group (no speaker weighting), attempts; negatives; $1 = 1 (r1, r2) or 2 (r3)
house() {
  local k=$1
  echo "--extra-pos $P/pos_live.npy:40 --extra-pos $P/pos_att.npy:8 --extra-neg $P/negpart.npy:16:3 \
 --extra-neg $P/hard.npy:$((48 * k)):$((12 * k)) --extra-neg $P/conv.npy:$((32 * k)):$((6 * k)) \
 --extra-neg $P/winneg.npy:$((8 * k)):$((6 * k)) --extra-neg $P/hwin_eve.npy:$((32 * k)):$((24 * k)) \
 --extra-neg $P/hwin_day.npy:$((32 * k)):$((12 * k))"
}
V6=v6/runs/v6m/kivi.onnx; V5=vnext/runs/vn_h_c120_w64/kivi.onnx
run() { n=$1; shift; date +%T; echo "$n"; [ -f "$RU/$n/kivi.onnx" ] || $BASE "$@" --out "$RU/$n" > "$RU/$n.log" 2>&1 || echo "FAIL $n"; }
run r1 --init $V5 $POS $NEG $CTX $MINED $DECOY $(house 1)
run r2 --init $V6 $POS $NEG $CTX $MINED $DECOY $(house 1)
run r3 --init $V5 $POS $NEG $CTX $MINED $DECOY $(house 2)
for n in r1 r2 r3; do
  .venv-tf/bin/python $H/export_tflite.py $RU/$n/kivi.onnx $RU/$n/oye_kivi_uncalibrated.tflite --check-features $F/pos_test.npy 2>&1 | tail -1
done
date +%T; echo TRAIN_DONE
