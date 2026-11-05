"""Placeholder for piper-sample-generator's `generate_samples`.

openWakeWord's train.py imports `generate_samples` at start-up even when `--generate_clips` is not
given. The clips here come from gen_clips.py (Spanish phonemes, Piper ONNX voices), so the English
PyTorch generator is not needed; this stub only satisfies the import.
"""


def generate_samples(*args, **kwargs):
    raise RuntimeError("clip generation is done by gen_clips.py; run train.py without --generate_clips")
