#!/usr/bin/env python3
"""Kivi v6.3 retry: threshold and arm by the rule pre-registered in kivi-models docs/plan/2026-11-false-wake-plan.md
section 11.2 (committed before training). Reads only the synthetic validation windows, the 2026-11-10 room audio and live
clips (the selection day; no v6.3r arm trained on it) and the window tables (owner-labelled attempts). Counts only.

1. Threshold per arm: the highest raw value t on 0.9000, 0.9005, ..., 0.9995 with R(t) >= R_v6 (as v6p3_select.py; R =
   mean accept rate on `v6/feat/pos_test.npy` and `v6p1/feat/ctxp_syn_pos_val.npy`, R_v6 = v6 at raw 0.9595); fallbacks:
   the highest grid values with R >= R_v6 - 1, - 2, - 3 points.
2. Count on 2026-11-10 at t: (a) TFLite detections at the server grid (v6_replay.py --require-grid, 2 s refractory) minus
   those within 3 s of an owner-labelled attempt, plus (b) the 2026-11-10 live clips labelled false (trusted) on which the
   model crosses anywhere in tail + request audio (kivi_fair_fa.score_clip). v6 at 0.9595 in the same run: E6, E6_b (c3-b).
3. Eligible at t: c3-b <= E6_b + 2 and total <= E6 + 3; else the next fallback; none: the arm is out.
4. Arm: fewest c3-b; tie: fewest total; tie: fewest 2026-11-10 room detections at t - 0.02; tie: lower
   `v6/feat/near_test.npy` accept rate; tie: r1, r3, r2.

    v6p3r_select.py --arm r1=v6p3r/runs/r1 --arm r2=v6p3r/runs/r2 --arm r3=v6p3r/runs/r3 --out v6p3r/selection.json
"""
import argparse
import json
import os
import sys
from pathlib import Path

for _v in ("OMP_NUM_THREADS", "OPENBLAS_NUM_THREADS", "MKL_NUM_THREADS"):
    os.environ.setdefault(_v, "1")
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

import numpy as np  # noqa: E402

import kivi_real_common as C  # noqa: E402
import v6p3_select as S  # noqa: E402

KH = C.kivi_home()
ORDER = ["r1", "r3", "r2"]
SEL_DAY = "2026-11-10"
SLACK_B, SLACK_TOTAL = 2, 3


def live_false(specs):
    """{model: {device: crossed false clips}} and the clip count per device, 2026-11-10 live clips labelled false."""
    import eval_stream as E
    import kivi_fair_fa as F
    import kivi_live_wakes as L
    F.MODELS = {n: (f, t, "") for n, (f, t) in specs.items()}
    feats, models = E.load_models([f"{n}={f}" for n, (f, _) in specs.items()])
    hits = {n: {"c3-a": 0, "c3-b": 0} for n in specs}
    total = {"c3-a": 0, "c3-b": 0}
    for r in L.read_table(F.ROOT / "live" / "live_wakes.csv"):
        if F.madrid(L.detection_epoch(r))[0] != SEL_DAY or F.live_class(r) != "false":
            continue
        d = F.ROOT / "live" / r["device"] / r["utc"]
        tail = C.read_pcm16(d / "wake_tail.wav")
        stt = C.read_pcm16(d / "stt.wav") if (d / "stt.wav").exists() else np.zeros(0, np.int16)
        sc = F.score_clip(feats, models, tail, stt, E.wake_samples(r["wake_len"] or 0))
        total[r["device"]] += 1
        for n, (first, _, _) in sc.items():
            hits[n][r["device"]] += int(first is not None)
    return hits, total


def main():
    ap = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    ap.add_argument("--arm", action="append", required=True, metavar="NAME=RUN_DIR")
    ap.add_argument("--out", required=True)
    ap.add_argument("--jobs", type=int, default=7)
    a = ap.parse_args()
    import v6_room as R
    arms = {s.split("=", 1)[0]: KH / s.split("=", 1)[1] for s in a.arm}
    v6h = R.Head(KH / (S.V6[0] + ".onnx"))
    r_v6 = float(np.mean([(v6h(np.load(KH / f)) > S.V6[1]).mean() for f in S.R_SETS]))
    near = np.load(KH / S.NEAR)
    res = {"rule": "plan section 11.2", "R_v6": round(100 * r_v6, 2), "arms": {}}
    tries = {}
    for n, d in arms.items():
        h = R.Head(d / "kivi.onnx")
        rc, _ = S.r_curve(h, S.R_SETS)
        tt = []
        for drop in (0.0, 0.01, 0.02, 0.03):
            k = np.flatnonzero(rc >= r_v6 - drop - 1e-12)
            if len(k) and float(S.GRID[k.max()]) not in tt:
                tt.append(float(S.GRID[k.max()]))
        tries[n] = tt
        res["arms"][n] = {"tries": tt, "R_at": {f"{t:.4f}": round(100 * float(rc[list(S.GRID).index(t)]), 2) for t in tt},
                          "near_test_at": {f"{t:.4f}": round(100 * float((h(near) > t).mean()), 3) for t in tt}}
    specs = {"v6": (str(KH / (S.V6[0] + ".tflite")), S.V6[1])}
    for n, tt in tries.items():
        for t in tt:
            f = str(arms[n] / "oye_kivi_uncalibrated.tflite")
            specs[f"{n}@{t:.4f}"] = (f, t)
            specs[f"{n}@{t:.4f}-0.02"] = (f, round(t - 0.02, 4))
    S.SEL_DAY = SEL_DAY
    dets = S.replay(specs, Path(a.out).with_name("sel_replay_1110.json"), a.jobs)
    lhits, ltotal = live_false(specs)
    import kivi_clean as KC
    windows = [{"device": w["device"], "t": w["t"], "label": "attempt"} for w in KC.window_rows()
               if w["label"] == "attempt" and w["source"] in KC.OWNER]          # every table, owner attempts only

    def count(name):
        c = S.count(dets[name], windows)
        c["live_false"] = lhits[name]
        c["c3-b"] += lhits[name]["c3-b"]
        c["c3-a"] += lhits[name]["c3-a"]
        c["total"] = c["c3-b"] + c["c3-a"]
        return c

    e6 = count("v6")
    res["v6_1110"] = e6
    res["live_false_clips_1110"] = ltotal
    lim_b, lim = e6["c3-b"] + SLACK_B, e6["total"] + SLACK_TOTAL
    res["limits"] = {"c3-b": lim_b, "total": lim}
    chosen = []
    for n, tt in tries.items():
        info = res["arms"][n]
        info["tried"] = {}
        for t in tt:
            c = count(f"{n}@{t:.4f}")
            c["room_at_t-0.02"] = len(dets[f"{n}@{t:.4f}-0.02"])
            info["tried"][f"{t:.4f}"] = c
            if c["c3-b"] <= lim_b and c["total"] <= lim:
                info.update({"eligible": True, "threshold": t, "events_1110": c,
                             "near_test": info["near_test_at"][f"{t:.4f}"]})
                chosen.append(n)
                break
        else:
            info["eligible"] = False
    key = lambda n: (res["arms"][n]["events_1110"]["c3-b"], res["arms"][n]["events_1110"]["total"],  # noqa: E731
                     res["arms"][n]["events_1110"]["room_at_t-0.02"], res["arms"][n]["near_test"], ORDER.index(n))
    res["chosen"] = None
    if chosen:
        best = sorted(chosen, key=key)[0]
        res["chosen"] = {"arm": best, "threshold": res["arms"][best]["threshold"]}
    Path(a.out).write_text(json.dumps(res, indent=1))
    os.chmod(a.out, 0o600)
    print(json.dumps(res, indent=1))


if __name__ == "__main__":
    main()
