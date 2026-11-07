#!/usr/bin/env python3
"""Kivi v4 ("oye Kivi") model and threshold selection: the room selection part only, never the test part.

As v3_select.py: per ONNX head, the threshold is the lowest raw threshold (fine logit grid) with at
most `--max-fa` false accepts (2 s refractory) on the room-audio selection part (`--room`, server-exact
features of v3_room.py embed, part `sel`). At that threshold it reports:
- recall on positive clip sets (`--clips LABEL=npz`: held-out voices clean, TV 10 dB, TV 0 dB, quiet);
- acceptance on near-phrase sets (`--near LABEL=npz`), overall and per phrase (the text field of the
  clip names), with "kivi" alone reported on its own;
- on window sets (`--pos-windows` / `--neg-windows LABEL=npy[:MANIFEST]`, one (16, 96) window per
  clip, as v4_augment.py train writes them, e.g. the train voices' validation sets): the share of
  windows above the threshold, and per phrase when a manifest is given (texts through the npy's
  .files.txt). v4 ranks candidates on these validation windows only, so the held-out voices stay
  independent of the choice.
Also the false accepts at fixed raw thresholds. Aggregates only.

    v4_select.py --model a=runs/a/kivi.onnx --room v3/feat/room_c3-a_sel.npz --room v3/feat/room_c3-b_sel.npz \
        --clips clean=v4/feat/eval_pos_clean.npz --near near=v4/feat/eval_near.npz --json out.json
"""
import argparse
import collections
import json
import os
import sys

import numpy as np

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from eval_stream import poisson_ci  # noqa: E402
from fast_eval import score_model, windows  # noqa: E402
from fast_report import load_server_exact  # noqa: E402

GRID = 1 / (1 + np.exp(-np.linspace(0.0, 14.0, 1401)))


def session(path):
    import onnx
    import onnxruntime as ort
    m = onnx.load(path)
    m.graph.input[0].type.tensor_type.shape.dim[0].dim_param = "N"
    m.graph.output[0].type.tensor_type.shape.dim[0].dim_param = "N"
    so = ort.SessionOptions()
    so.intra_op_num_threads = 4
    return ort.InferenceSession(m.SerializeToString(), sess_options=so, providers=["CPUExecutionProvider"])


def count_fa(per, t, refr=2.0):
    k = 0
    for p in per:
        last = -1e9
        for i in np.nonzero(p > t)[0]:
            if (i - last) * 0.08 >= refr:
                k += 1
                last = i
    return k


def clip_max(sess, z):
    return np.array([p.max() if len(p) else 0.0 for p in score_model(sess, z["data"], windows(z))])


def room_threshold(sess, rooms, max_fa):
    per = []
    for z in rooms:
        per += score_model(sess, z["data"], z["spans"])
    hours = sum(len(p) + 15 for p in per) * 0.08 / 3600
    hot = [p for p in per if len(p) and p.max() > GRID[0]]
    fa_grid = np.array([count_fa(hot, t) for t in GRID])
    ok = np.nonzero(fa_grid <= max_fa)[0]
    thr = float(GRID[ok[0]]) if len(ok) else 1.0
    fa = int(fa_grid[ok[0]]) if len(ok) else 0
    lo, hi = poisson_ci(fa, hours)
    return thr, {"threshold": round(thr, 5), "sel_hours": round(hours, 3), "sel_fa": fa,
                 "sel_fa_per_h": [round(fa / hours, 3), round(lo, 3), round(hi, 3)],
                 "fa_at": {f"{t}": int(fa_grid[np.argmin(np.abs(GRID - t))]) for t in (0.5, 0.7, 0.8, 0.9, 0.95, 0.97, 0.99)}}


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--model", action="append", required=True)
    ap.add_argument("--room", action="append", required=True)
    ap.add_argument("--clips", action="append", default=[])
    ap.add_argument("--near", action="append", default=[])
    ap.add_argument("--pos-windows", action="append", default=[])
    ap.add_argument("--neg-windows", action="append", default=[])
    ap.add_argument("--max-fa", type=int, default=2)
    ap.add_argument("--json")
    a = ap.parse_args()
    rooms = []
    for f in a.room:
        z = load_server_exact(f)
        rooms.append({"data": z["data"], "spans": windows(z)})
    clips = {lab: load_server_exact(f) for lab, f in (s.split("=", 1) for s in a.clips)}
    nears = {lab: load_server_exact(f) for lab, f in (s.split("=", 1) for s in a.near)}
    def _win(spec):
        lab, rest = spec.split("=", 1)
        f, _, man = rest.partition(":")
        texts = None
        if man:
            import csv
            t = {r["file"]: r["text"] for r in csv.DictReader(open(man, encoding="utf-8"))}
            texts = [t[x] for x in open(f + ".files.txt").read().split()]
        return lab, (np.load(f, mmap_mode="r"), texts)
    posw = dict(_win(s) for s in a.pos_windows)
    negw = dict(_win(s) for s in a.neg_windows)
    out = {}
    for spec in a.model:
        lab, path = spec.split("=", 1)
        sess = session(path)
        thr, res = room_threshold(sess, rooms, a.max_fa)
        for c, z in clips.items():
            res[c] = round(float(np.mean(clip_max(sess, z) > thr)), 4)
        for c, z in nears.items():
            mx = clip_max(sess, z)
            texts = [str(n).split("|")[2] for n in z["names"]]
            res[c] = round(float(np.mean(mx > thr)), 4)
            by = collections.defaultdict(list)
            for t, m in zip(texts, mx):
                by[t].append(m > thr)
            res[c + "_kivi_alone"] = f"{int(sum(by.get('kivi', [])))}/{len(by.get('kivi', []))}"
            res[c + "_by_text"] = {t: f"{int(sum(v))}/{len(v)}" for t, v in sorted(by.items(), key=lambda kv: -np.mean(kv[1]))}
        name = sess.get_inputs()[0].name
        for c, (X, texts) in list(posw.items()) + list(negw.items()):
            s = np.concatenate([sess.run(None, {name: np.asarray(X[k:k + 8192], np.float32)})[0].ravel()
                                for k in range(0, len(X), 8192)])
            res[c] = round(float(np.mean(s > thr)), 5)
            if texts is not None:
                by = collections.defaultdict(list)
                for t, v in zip(texts, s):
                    by[t].append(v > thr)
                res[c + "_kivi_alone"] = round(float(np.mean(by["kivi"])), 5) if by.get("kivi") else None
                res[c + "_by_text"] = {t: round(float(np.mean(v)), 4) for t, v in sorted(by.items(), key=lambda kv: -np.mean(kv[1]))}
        out[lab] = res
        short = {k: v for k, v in res.items() if not k.endswith("_by_text") and k != "fa_at"}
        print(lab, json.dumps(short), flush=True)
    if a.json:
        json.dump(out, open(a.json, "w"), indent=1, ensure_ascii=False)


if __name__ == "__main__":
    main()
