#!/usr/bin/env python3
"""Kivi v3 model and threshold selection (never the test session, never the test room part).

For each ONNX head: the threshold is the lowest raw threshold at which the room-audio selection part
(`--room`, server-exact features of v3_room.py embed, part `sel`) has at most `--max-fa` false
accepts (2 s refractory), from a fine grid in logit space. At that threshold it reports recall on
held-out synthetic voices (`--clips`, fast_eval/v2_augment eval npz) and on real validation takes
(`--real`, v3_real.py eval npz; names carry `holdout` or `train`), and near-word acceptance.
Aggregates only.

    v3_select.py --model a=runs/a/kivi.onnx --room v3/feat/room_c3-a_sel.npz --room v3/feat/room_c3-b_sel.npz \
        --clips clean=v2/feat/kivi_clean.npz --clips snr10=v2/feat/kivi_snr10.npz --real v3/real/real_eval.npz --json out.json
"""
import argparse
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


def _reals(a):
    """[(label suffix, path)]: a plain path keeps v3's keys (real_...), LABEL=path gives realLABEL_..."""
    out = []
    for spec in a.real:
        lab, f = spec.split("=", 1) if "=" in spec else ("", spec)
        out.append((("_" + lab) if lab else "", f))
    return out


def evaluate(sess, a, cache):
    per = []
    for f in a.room:
        per += score_model(sess, cache[f]["data"], cache[f]["spans"])
    hours = sum(len(p) + 15 for p in per) * 0.08 / 3600
    # only pieces with a score above the grid's lowest value can count, so counting is cheap at every grid point
    hot = [p for p in per if len(p) and p.max() > GRID[0]]
    fa_grid = np.array([count_fa(hot, t) for t in GRID])
    ok = np.nonzero(fa_grid <= a.max_fa)[0]
    thr = float(GRID[ok[0]]) if len(ok) else 1.0
    res = {"threshold": round(thr, 5), "sel_hours": round(hours, 3), "sel_fa": int(fa_grid[ok[0]]) if len(ok) else None}
    lo, hi = poisson_ci(res["sel_fa"] or 0, hours)
    res["sel_fa_per_h"] = [round((res["sel_fa"] or 0) / hours, 3), round(lo, 3), round(hi, 3)]
    for spec in a.clips:
        lab, f = spec.split("=", 1)
        mx = cache[f]
        res[lab] = round(float(np.mean(mx > thr)), 4)
    for lab_r, f in _reals(a):
        z = cache[f]
        mx = z["max"]
        for grp, sel in z["groups"].items():
            res[f"real{lab_r}_" + grp] = f"{int((mx[sel] > thr).sum())}/{int(sel.sum())}"
    res["fa_at"] = {f"{t}": int(fa_grid[np.argmin(np.abs(GRID - t))]) for t in (0.9, 0.95, 0.97, 0.98, 0.99)}
    return res


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--model", action="append", required=True)
    ap.add_argument("--room", action="append", required=True)
    ap.add_argument("--clips", action="append", default=[])
    ap.add_argument("--real", action="append", default=[],
                    help="v3_real.py eval npz, or LABEL=npz (keys real<LABEL>_...); repeatable (v5: one per speaker)")
    ap.add_argument("--max-fa", type=int, default=2)
    ap.add_argument("--json")
    a = ap.parse_args()
    out = {}
    for spec in a.model:
        lab, path = spec.split("=", 1)
        sess = session(path)
        cache = {}
        for f in a.room:
            z = load_server_exact(f)
            cache[f] = {"data": z["data"], "spans": windows(z)}
        for c in a.clips:
            f = c.split("=", 1)[1]
            cache[f] = clip_max(sess, load_server_exact(f))
        for _, f_real in _reals(a):
            z = load_server_exact(f_real)
            names = [n.split("|") for n in z["names"]]
            mx = clip_max(sess, z)
            g = {}
            for i, (take, src, lab_) in enumerate(names):
                srcn, split = src.split(":")
                kind = lab_.split(":")[0]
                for key in (f"{kind}_{split}_all", f"{kind}_{split}_{'tv' if srcn.startswith('tv') else 'desk'}"):
                    g.setdefault(key, np.zeros(len(names), bool))[i] = True
            cache[f_real] = {"max": mx, "groups": dict(sorted(g.items()))}
        out[lab] = evaluate(sess, a, cache)
        print(lab, json.dumps(out[lab]), flush=True)
    if a.json:
        json.dump(out, open(a.json, "w"), indent=1)


if __name__ == "__main__":
    main()
