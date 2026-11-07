"""Regression tests: offline wake-word numbers must use the server's features.

The server (wyoming-openwakeword 2.x, pyopen-wakeword) computes the melspectrogram per 1760-sample
chunk, and openWakeWord's melspectrogram model clamps at (max - 80 dB) with the max over its whole
input. Batched extraction (training-style) therefore gives different features. `fast_eval.py
embed --stream-onnx` (with the per-row-max model from `stream_mel.py`) must reproduce the
server's streaming embeddings, and `fast_report.py` must refuse anything else.

Needs the training venv (openwakeword with its downloaded feature models, onnxruntime, onnx,
pyopen-wakeword); skipped otherwise. Inputs are synthetic (tones and noise), no recordings.

    $WORK/.venv/bin/python -m pytest scripts/voice-assistant/wakeword/test_server_exact_features.py
"""
import os
import sys

import numpy as np
import pytest

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, HERE)

RATE = 16000
# Max |difference| between server-streaming and batched server-exact embeddings (ONNX on our side,
# TFLite on the server's); measured 2026-11-05 on the signal below, see the experiment doc.
EMB_TOL = 0.05


def synthetic_signal(seconds=6.0, seed=0):
    """Quiet noise, a loud chirp and a quiet tone: large level changes between chunks."""
    rng = np.random.default_rng(seed)
    t = np.arange(int(seconds * RATE)) / RATE
    x = 30 * rng.standard_normal(len(t))
    loud = (t > 1.5) & (t < 2.5)
    x[loud] += 12000 * np.sin(2 * np.pi * (300 + 1500 * (t[loud] - 1.5)) * t[loud])
    quiet = (t > 3.5) & (t < 4.5)
    x[quiet] += 300 * np.sin(2 * np.pi * 700 * t[quiet])
    return np.clip(x, -32768, 32767).astype(np.int16)


@pytest.fixture(scope="module")
def models(tmp_path_factory):
    oww = pytest.importorskip("openwakeword")
    pytest.importorskip("onnxruntime")
    pytest.importorskip("onnx")
    pyoww = pytest.importorskip("pyopen_wakeword")
    res = os.path.join(os.path.dirname(oww.__file__), "resources", "models")
    mel, emb = os.path.join(res, "melspectrogram.onnx"), os.path.join(res, "embedding_model.onnx")
    if not (os.path.exists(mel) and os.path.exists(emb)):
        pytest.skip("openWakeWord feature models not downloaded")
    import stream_mel
    rowmax = str(tmp_path_factory.mktemp("mel") / "melspectrogram_rowmax.onnx")
    stream_mel.main(mel, rowmax)
    return {"pyoww": pyoww, "mel": mel, "rowmax": rowmax, "emb": emb}


def server_stream_embeddings(pyoww, x):
    """What the server computes: pyopen-wakeword streaming in 80 ms chunks after a reset."""
    feats = pyoww.OpenWakeWordFeatures.from_builtin()
    feats.reset()
    out = []
    for k in range(0, len(x) - 1280 + 1, 1280):
        for e in feats.process_streaming(x[k:k + 1280].tobytes()):
            out.append(np.asarray(e, dtype=np.float32).reshape(96))
    return np.stack(out)


def test_batched_server_exact_matches_streaming(models):
    import fast_eval
    x = synthetic_signal()
    server = server_stream_embeddings(models["pyoww"], x)
    F = fast_eval.StreamFeatures(models["rowmax"], models["emb"])
    ours = F._get_embeddings(np.concatenate([np.zeros(8 * RATE, np.int16), x]))  # 8 s autofill
    n = min(len(server), len(ours))
    assert n > 50
    diff = np.abs(server[-n:] - ours[-n:]).max()
    assert diff < EMB_TOL, f"server-exact features drifted: max |diff| = {diff:.4f}"


def test_batch_features_differ_from_server(models):
    """The trap itself: training-style batch features are not what the server computes."""
    import fast_eval
    x = synthetic_signal()
    server = server_stream_embeddings(models["pyoww"], x)
    F = fast_eval.StreamFeatures(models["mel"], models["emb"])  # original model: max over the batch
    batch = F._get_embeddings(np.concatenate([np.zeros(8 * RATE, np.int16), x]))
    n = min(len(server), len(batch))
    assert np.abs(server[-n:] - batch[-n:]).max() > 10 * EMB_TOL


def test_report_refuses_non_server_exact(tmp_path):
    import fast_report
    p = tmp_path / "batch.npz"
    np.savez(p, names=np.array(["a"]), lengths=np.array([20]), data=np.zeros((20, 96), np.float16),
             features=np.array("batch"))
    with pytest.raises(SystemExit):
        fast_report.load_server_exact(str(p))
    q = tmp_path / "untagged.npz"
    np.savez(q, names=np.array(["a"]), lengths=np.array([20]), data=np.zeros((20, 96), np.float16))
    with pytest.raises(SystemExit):
        fast_report.load_server_exact(str(q))
    r = tmp_path / "ok.npz"
    np.savez(r, names=np.array(["a"]), lengths=np.array([20]), data=np.zeros((20, 96), np.float16),
             features=np.array("server-exact"))
    assert fast_report.load_server_exact(str(r))["data"].shape == (20, 96)
