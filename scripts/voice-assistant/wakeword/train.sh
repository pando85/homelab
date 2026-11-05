#!/usr/bin/env bash
# Train the "kivi" openWakeWord model with the official train.py (augmentation, features,
# auto_train) on clips made by gen_clips.py, then export ONNX -> TFLite.
# Usage: WORK=/path/to/workdir train.sh [--skip-augment]
# Expects in $WORK: .venv (training), .venv-tf (TensorFlow for the export), src/openWakeWord,
# data/ (see README.md) and train_out/kivi/{positive,negative}_{train,test}/.
set -euo pipefail
: "${WORK:?set WORK to the work directory}"
HERE="$(cd "$(dirname "$0")" && pwd)"
cfg="$WORK/kivi.rendered.yaml"
sed -e "s#@WORK@#$WORK#g" -e "s#@STUB@#$HERE/stub#g" "$HERE/kivi.yaml" > "$cfg"

steps=(--augment_clips --train_model)
if [[ "${1:-}" == "--skip-augment" ]]; then
  steps=(--train_model)
fi

cd "$WORK"
# train.py's --convert_to_tflite defaults to the string "False", which is truthy, so it always tries
# onnx_tf after saving the ONNX model and fails; the ONNX model is converted below instead.
start=$(date +%s)
"$WORK/.venv/bin/python" -m openwakeword.train --training_config "$cfg" "${steps[@]}" || {
  if [[ ! -f "$WORK/train_out/kivi.onnx" || $(stat -c %Y "$WORK/train_out/kivi.onnx") -lt $start ]]; then
    echo "training failed" >&2
    exit 1
  fi
}
"$WORK/.venv-tf/bin/python" "$HERE/export_tflite.py" "$WORK/train_out/kivi.onnx" \
  "$WORK/train_out/kivi.tflite" --check-features "$WORK/train_out/kivi/positive_features_test.npy"
sha256sum "$WORK/train_out/kivi.onnx" "$WORK/train_out/kivi.tflite"
