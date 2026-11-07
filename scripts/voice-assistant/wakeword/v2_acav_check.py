#!/usr/bin/env python3
"""Does the batch/stream feature mismatch matter for the ACAV100M negatives? (Kivi v2, step 1)

The ACAV100M features were computed by openWakeWord in batch mode (melspectrogram clamp at
max - 80 dB over the whole batch tensor); the server clamps per 80 ms chunk. The ACAV audio is not
available, so this measures the mismatch on proxy audio of the same kind (AudioSet clips and
Spanish read speech, 10 s each, random peak level), computed both ways on the same clips:

1. paired embedding difference (median and 99th percentile of |batch - stream|);
2. the v1 head's scores on the same 16-frame windows (fraction above 0.5 / 0.9 / 0.98, and the
   mean |score difference|);
3. a domain probe: logistic regression on flattened windows, batch vs stream, split by clip;
   test AUC near 0.5 means a head cannot tell the domains apart, near 1.0 means it can.

Prints aggregates only (no audio, no transcripts).

    v2_acav_check.py --clips DIR [DIR ...] --model final/kivi_uncalibrated.onnx \
        --stream-onnx emb_s_melrow.onnx,embedding_model.onnx --n 400 --json out.json
"""
import argparse
import glob
import json
import os
import sys

import numpy as np

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--clips", nargs="+", required=True)
    ap.add_argument("--model", required=True)
    ap.add_argument("--stream-onnx", required=True)
    ap.add_argument("--n", type=int, default=400, help="clips per directory")
    ap.add_argument("--seed", type=int, default=0)
    ap.add_argument("--json")
    a = ap.parse_args()
    import soundfile as sf
    import torch  # noqa: F401  CUDA libraries for onnxruntime-gpu
    import onnx
    import onnxruntime as ort
    from openwakeword.utils import AudioFeatures
    from fast_eval import StreamFeatures

    rng = np.random.default_rng(a.seed)
    L = 160000
    audio, src = [], []
    for d in a.clips:
        fs = sorted(glob.glob(os.path.join(d, "*.wav")))
        for f in rng.choice(fs, min(a.n, len(fs)), replace=False):
            x, _ = sf.read(f, dtype="float32")
            x = np.resize(x, L) if len(x) < L else x[:L]
            x = x / (np.abs(x).max() + 1e-9) * 10 ** (rng.uniform(-30, -1) / 20)
            audio.append((x * 32767).astype(np.int16))
            src.append(os.path.basename(d.rstrip("/")))
    X = np.stack(audio)
    B = AudioFeatures(device="gpu").embed_clips(X, batch_size=128).astype(np.float32)  # (n, frames, 96)
    S = StreamFeatures(*a.stream_onnx.split(","))
    Ss = [S._get_embeddings(x) for x in X]
    n = min(B.shape[1], min(len(s) for s in Ss))
    St = np.stack([s[:n] for s in Ss]).astype(np.float32)
    B = B[:, :n]
    d = np.abs(B - St)
    out = {"clips": len(X), "frames_per_clip": n, "sources": sorted(set(src)),
           "emb_absdiff_median": float(np.median(d)), "emb_absdiff_p99": float(np.percentile(d, 99)),
           "emb_absdiff_max": float(d.max()), "emb_value_p99": float(np.percentile(np.abs(St), 99))}

    m = onnx.load(a.model)
    m.graph.input[0].type.tensor_type.shape.dim[0].dim_param = "N"
    m.graph.output[0].type.tensor_type.shape.dim[0].dim_param = "N"
    sess = ort.InferenceSession(m.SerializeToString(), providers=["CPUExecutionProvider"])
    idx = np.arange(0, n - 15, 4)

    def wins(E):
        return np.concatenate([np.stack([e[i:i + 16] for i in idx]) for e in E])
    WB, WS = wins(B), wins(St)
    name = sess.get_inputs()[0].name
    sb = np.concatenate([sess.run(None, {name: WB[k:k + 8192]})[0].ravel() for k in range(0, len(WB), 8192)])
    ss = np.concatenate([sess.run(None, {name: WS[k:k + 8192]})[0].ravel() for k in range(0, len(WS), 8192)])
    out["windows"] = int(len(sb))
    out["v1_score_absdiff_mean"] = float(np.mean(np.abs(sb - ss)))
    for t in (0.5, 0.9, 0.98):
        out[f"v1_frac_above_{t}"] = {"batch": float(np.mean(sb > t)), "stream": float(np.mean(ss > t))}

    # domain probe, split by clip (first 70 % of clips train, rest test; both domains of a clip together)
    from sklearn.linear_model import LogisticRegression
    from sklearn.metrics import roc_auc_score
    per = len(idx)
    order = rng.permutation(len(X))
    tr_c, te_c = order[: int(0.7 * len(X))], order[int(0.7 * len(X)):]

    def rows(cs):
        r = np.concatenate([np.arange(c * per, (c + 1) * per) for c in cs])
        r = r[rng.random(len(r)) < 0.25]  # subsample windows (they overlap)
        Xd = np.concatenate([WB[r].reshape(len(r), -1), WS[r].reshape(len(r), -1)])
        yd = np.r_[np.zeros(len(r)), np.ones(len(r))]
        return Xd, yd
    Xtr, ytr = rows(tr_c)
    Xte, yte = rows(te_c)
    mu, sd = Xtr.mean(0), Xtr.std(0) + 1e-6
    clf = LogisticRegression(max_iter=2000, C=1.0).fit((Xtr - mu) / sd, ytr)
    p = clf.predict_proba((Xte - mu) / sd)[:, 1]
    out["domain_probe_test_auc"] = float(roc_auc_score(yte, p))
    out["domain_probe_test_acc"] = float(np.mean((p > 0.5) == yte))
    print(json.dumps(out, indent=1))
    if a.json:
        json.dump(out, open(a.json, "w"), indent=1)


if __name__ == "__main__":
    main()
