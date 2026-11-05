#!/usr/bin/env python3
"""Recall, near-word acceptance and false accepts per hour of one ONNX wake-word head over cached
server-exact embeddings (`fast_eval.py embed --tflite-models ...`), at several thresholds.

    fast_report.py --model final/kivi_raw.onnx --clips clean=emb_s/kivi_clean.npz ... \
        --long mls_es_dev=emb_s/mls_es_dev.npz ... --thresholds 0.9,0.97,0.99 --json out.json

Clips count as detected when any window scores above the threshold. Long audio counts detections
with a 2 s refractory time (the server's default), and reports exact Poisson 95 % intervals.
"""
import argparse
import json
import math
import os
import sys

import numpy as np

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from eval_stream import poisson_ci, wilson  # noqa: E402
from fast_eval import score_model, windows  # noqa: E402


def load_server_exact(path):
    """Embeddings for reported numbers must come from the server-exact path (pyopen-wakeword chunking,
    per-chunk mel clamp); batch features (training-style) give wrong recall and false accepts."""
    z = np.load(path)
    kind = str(z["features"]) if "features" in z.files else "unknown"
    if kind != "server-exact":
        raise SystemExit(f"{path}: features are '{kind}', not 'server-exact'; "
                         "embed with fast_eval.py embed --stream-onnx (or --tflite-models)")
    return z


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--model", required=True)
    ap.add_argument("--clips", action="append", default=[])
    ap.add_argument("--long", action="append", default=[])
    ap.add_argument("--thresholds", default="0.5,0.9,0.95,0.97,0.98,0.99,0.995,0.998")
    ap.add_argument("--json")
    a = ap.parse_args()
    import onnx
    import onnxruntime as ort
    m = onnx.load(a.model)
    m.graph.input[0].type.tensor_type.shape.dim[0].dim_param = "N"
    m.graph.output[0].type.tensor_type.shape.dim[0].dim_param = "N"
    so = ort.SessionOptions()
    so.intra_op_num_threads = 4
    sess = ort.InferenceSession(m.SerializeToString(), sess_options=so, providers=["CPUExecutionProvider"])
    thr = [float(t) for t in a.thresholds.split(",")]
    out = {"model": a.model, "thresholds": thr, "clips": {}, "long": {}}
    for spec in a.clips:
        label, path = spec.split("=", 1)
        z = load_server_exact(path)
        mx = np.array([p.max() if len(p) else 0.0 for p in score_model(sess, z["data"], windows(z))])
        names = [n.split("|") for n in z["names"]]
        res = []
        for t in thr:
            groups = {}
            for (f, voice, text), v in zip(names, mx):
                for g in ("all", "voice:" + voice, "text:" + text):
                    groups.setdefault(g, [0, 0])
                    groups[g][0] += int(v > t)
                    groups[g][1] += 1
            for g, (k, n) in groups.items():
                lo, hi = wilson(k, n)
                res.append({"threshold": t, "group": g, "k": k, "n": n, "rate": round(k / n, 4),
                            "ci95": [round(lo, 4), round(hi, 4)]})
        out["clips"][label] = res
        print(label, " ".join(f"{t}:{r['k']}/{r['n']}" for t in thr for r in res
                              if r["threshold"] == t and r["group"] == "all"), flush=True)
    for spec in a.long:
        label, path = spec.split("=", 1)
        z = load_server_exact(path)
        per = score_model(sess, z["data"], windows(z))
        hours = sum(len(p) + 15 for p in per) * 0.08 / 3600
        res = []
        for t in thr:
            k = 0
            for p in per:
                last = -1e9
                for i in np.nonzero(p > t)[0]:
                    if (i - last) * 0.08 >= 2.0:
                        k += 1
                        last = i
            lo, hi = poisson_ci(k, hours)
            res.append({"threshold": t, "false_accepts": k, "hours": round(hours, 3),
                        "per_hour": round(k / hours, 3), "ci95": [round(lo, 3), round(hi, 3)]})
        out["long"][label] = res
        print(label, f"{hours:.2f} h", " ".join(f"{r['threshold']}:{r['false_accepts']}" for r in res), flush=True)
    if a.json:
        json.dump(out, open(a.json, "w"), indent=1, ensure_ascii=False)


if __name__ == "__main__":
    main()
