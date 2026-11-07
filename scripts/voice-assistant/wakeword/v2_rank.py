#!/usr/bin/env python3
"""Rank Kivi candidates without touching the held-out evaluation: for each model, the threshold at
which the selection set (MLS Spanish dev through the front-end) has at most K false accepts
(2 s refractory), then the recall on the training-voice validation positives at that threshold.

    v2_rank.py --dev v2/feat/L_mls_es_dev.npz --val v2/feat/pos_test.npy run_a/kivi.onnx run_b/kivi.onnx
"""
import argparse
import os
import sys

import numpy as np

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from fast_eval import score_model, windows  # noqa: E402


def session(path):
    import onnx
    import onnxruntime as ort
    m = onnx.load(path)
    m.graph.input[0].type.tensor_type.shape.dim[0].dim_param = "N"
    m.graph.output[0].type.tensor_type.shape.dim[0].dim_param = "N"
    so = ort.SessionOptions()
    so.intra_op_num_threads = 4
    return ort.InferenceSession(m.SerializeToString(), sess_options=so, providers=["CPUExecutionProvider"])


def count_fa(per, t):
    k = 0
    for p in per:
        last = -1e9
        for i in np.nonzero(p > t)[0]:
            if (i - last) * 0.08 >= 2.0:
                k += 1
                last = i
    return k


def threshold_for(per, k_max):
    lo, hi = 0.0, 1.0 - 1e-7
    for _ in range(40):
        mid = (lo + hi) / 2
        if count_fa(per, mid) <= k_max:
            hi = mid
        else:
            lo = mid
    return hi


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("models", nargs="+")
    ap.add_argument("--dev", required=True)
    ap.add_argument("--val", required=True)
    ap.add_argument("--k", default="2,4,8")
    a = ap.parse_args()
    dev = np.load(a.dev)
    D, spans = dev["data"], windows(dev)
    P = np.load(a.val).astype(np.float32)
    for path in a.models:
        s = session(path)
        per = score_model(s, D, spans)
        pv = s.run(None, {s.get_inputs()[0].name: P})[0].ravel()
        row = []
        for k in (int(v) for v in a.k.split(",")):
            t = threshold_for(per, k)
            row.append(f"dev<={k}: thr {t:.5f} val-recall {np.mean(pv > t):.3f}")
        print(path, " | ".join(row), flush=True)


if __name__ == "__main__":
    main()
