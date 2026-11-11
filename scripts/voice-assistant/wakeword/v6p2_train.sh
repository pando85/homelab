#!/bin/bash
# shellcheck disable=SC2086  # flag lists in variables are split on purpose
# Kivi v6.2 (pre-registered 2026-11-11, homelab docs/voice-assistant/experiments/oye-kivi-v6.md "v6.2"): the v6.1 recipe
# plus the data-quality fixes. Inputs under v6p2/feat and v6p2/real are the filtered copies (v6p2_apply_flags.py); no
# 2026-11-10 room audio (the threshold day) and no 2026-11-12/-13 audio. Two arms, warm start from v6 (oye_kivi_ctx):
#   p2a  v6 hard negatives day 32/step w6, evening 32/step w12, v5 hard 128/step w3
#   p2b  v6 hard negatives day 48/step w12, evening 32/step w24, v5 hard 128/step w6
set -e
cd /work/workspaces/kivi-wakeword
H=$HOME/homelab/scripts/voice-assistant/wakeword
PY=".venv/bin/python -I"
F=v6/feat; Q=v6p2/feat; RQ=v6p2/real; RU=v6p2/runs; mkdir -p $RU
BASE="$PY $H/train_simple.py --work $PWD --pos-train $Q/pos_train.npy --neg-train $Q/near_train.npy --pos-test $F/pos_test.npy --neg-test $F/near_test.npy --acav data/acav/acav_sub400k.npy --mls v2/feat/T_mls_train0_w8.npy --layer 64 --lr 2e-4 --steps 4000 --adv 128 --seed 2 --n-pos 192"
NEG="--extra-neg v2/feat/hard_r1.npy:128 --extra-neg $Q/room_train_w8.npy:128 --extra-neg v4/feat/mls_hard4.npy:128 \
 --extra-neg v6/real/neg_kivi.npy:64:6 --extra-neg v6/real/neg_near.npy:32:6 --extra-neg v6/real/neg_bg.npy:32 \
 --extra-neg $Q/live_hard.npy:32:6 --extra-neg $Q/live_negpart.npy:16:3"
POS="--extra-pos $RQ/real_tv_train.npy:64 --extra-pos $RQ/real_pc_train.npy:48 --extra-pos $RQ/real_webcam_train.npy:16 \
 --extra-pos $F/live_pos_spk01.npy:16 --extra-pos $F/live_pos_spk02.npy:16 --extra-pos $F/live_pos_unk.npy:8"
CTX="--extra-pos $Q/ctxp_syn_pos_tr.npy:64 --extra-neg $Q/ctxp_syn_negpart.npy:32:3 --extra-neg $Q/ctxn_syn_conf.npy:64:6"
HNA="--extra-neg $Q/hard_v6_day.npy:32:6 --extra-neg $Q/hard_v6_evening.npy:32:12 --extra-neg $Q/hard_v5.npy:128:3"
HNB="--extra-neg $Q/hard_v6_day.npy:48:12 --extra-neg $Q/hard_v6_evening.npy:32:24 --extra-neg $Q/hard_v5.npy:128:6"
V6=v6/runs/v6m/kivi.onnx
run() { n=$1; shift; date +%T; echo "$n"; [ -f "$RU/$n/kivi.onnx" ] || $BASE "$@" --out "$RU/$n" > "$RU/$n.log" 2>&1 || echo "FAIL $n"; }
run p2a --init $V6 $POS $NEG $CTX $HNA
run p2b --init $V6 $POS $NEG $CTX $HNB
for n in p2a p2b; do
  .venv-tf/bin/python $H/export_tflite.py $RU/$n/kivi.onnx $RU/$n/oye_kivi_uncalibrated.tflite --check-features $F/pos_test.npy 2>&1 | tail -1
done
date +%T; echo TRAIN_DONE
