#!/bin/bash
# shellcheck disable=SC2086  # flag lists in variables are split on purpose
# Kivi v6.1 stage 1 (2026-11-11): v6's recipe plus hard negatives mined by v6 (raw >= 0.5, +-1 step) and v5 (>= 0.3)
# on ALL even-day room audio (2026-11-06, -08, -10); room negatives of 11-05..11-10 (11-05 grandfathered). 500 synthetic
# context clips held out of training for the stage-2 arm choice. Three arms, picked in stage 2 on 2026-11-12:
#   p1a  warm start v6 (oye_kivi_ctx), v6 hard negatives day w6 / evening w12, v5 hard negatives w3
#   p1b  warm start v5 (oye_kivi_next), same data and weights as p1a
#   p1c  warm start v6, hard negatives doubled: day w12 / evening w24 (48 + 48 per step), v5 hard w6
set -e
cd /work/workspaces/kivi-wakeword
H=$HOME/homelab/scripts/voice-assistant/wakeword
PY=".venv/bin/python -I"
F=v6/feat; R=v6/real; P=v6p1/feat; RU=v6p1/runs; mkdir -p $RU
BASE="$PY $H/train_simple.py --work $PWD --pos-train $F/pos_train.npy --neg-train $F/near_train.npy --pos-test $F/pos_test.npy --neg-test $F/near_test.npy --acav data/acav/acav_sub400k.npy --mls v2/feat/T_mls_train0_w8.npy --layer 64 --lr 2e-4 --steps 4000 --adv 128 --seed 2 --n-pos 192"
NEG="--extra-neg v2/feat/hard_r1.npy:128 --extra-neg $P/room_train_w8.npy:128 --extra-neg v4/feat/mls_hard4.npy:128 \
 --extra-neg $R/neg_kivi.npy:64:6 --extra-neg $R/neg_near.npy:32:6 --extra-neg $R/neg_bg.npy:32 \
 --extra-neg $F/live_hard.npy:32:6 --extra-neg $F/live_negpart.npy:16:3"
POS="--extra-pos $R/real_tv_train.npy:64 --extra-pos $R/real_pc_train.npy:48 --extra-pos $R/real_webcam_train.npy:16 \
 --extra-pos $F/live_pos_spk01.npy:16 --extra-pos $F/live_pos_spk02.npy:16 --extra-pos $F/live_pos_unk.npy:8"
CTX="--extra-pos $P/ctxp_syn_pos_tr.npy:64 --extra-neg $F/ctxp_syn_negpart.npy:32:3 --extra-neg $F/ctxn_syn_conf.npy:64:6"
HN1="--extra-neg $P/hard_v6_day.npy:32:6 --extra-neg $P/hard_v6_evening.npy:32:12 --extra-neg $P/hard_v5.npy:128:3"
HN2="--extra-neg $P/hard_v6_day.npy:48:12 --extra-neg $P/hard_v6_evening.npy:48:24 --extra-neg $P/hard_v5.npy:128:6"
V6=v6/runs/v6m/kivi.onnx; V5=vnext/runs/vn_h_c120_w64/kivi.onnx
run() { n=$1; shift; date +%T; echo $n; [ -f $RU/$n/kivi.onnx ] || $BASE "$@" --out $RU/$n > $RU/$n.log 2>&1 || echo FAIL $n; }
run p1a --init $V6 $POS $NEG $CTX $HN1
run p1b --init $V5 $POS $NEG $CTX $HN1
run p1c --init $V6 $POS $NEG $CTX $HN2
for n in p1a p1b p1c; do
  .venv-tf/bin/python $H/export_tflite.py $RU/$n/kivi.onnx $RU/$n/oye_kivi_uncalibrated.tflite --check-features $F/pos_test.npy 2>&1 | tail -1
done
date +%T; echo TRAIN_DONE
