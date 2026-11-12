#!/usr/bin/env python3
"""Kivi v6.3 threshold and arm, by the rule pre-registered in kivi-models docs/plan/2026-11-false-wake-plan.md section 9.2
(committed before training). Reads only: synthetic validation windows (no day data), the 2026-11-10 room audio (even,
held out of every v6.x training run) and the window tables (owner-labelled attempts on 2026-11-10 are removed). Counts only.

1. Recall-matched threshold per arm: the highest raw value t on the grid 0.9000, 0.9005, ..., 0.9995 with R(t) >= R_v6,
   R = mean accept rate on `v6/feat/pos_test.npy` (2,367 isolated synthetic windows) and `v6p1/feat/ctxp_syn_pos_val.npy`
   (1,000 held-out synthetic context windows); R_v6 = v6 at raw 0.9595.
2. Eligibility on 2026-11-10 (TFLite replay at the server grid, v6_replay.py, 2 s refractory, minus detections within 3 s of
   an owner-labelled attempt): events on c3-b <= max(E6_b, 1) and in total <= max(E6, 1), E6 = v6 at 0.9595 on the same
   replay. Not eligible at t: the highest grid values with R >= R_v6 - 1, - 2 and - 3 points are tried in turn; else the
   arm is out.
3. Arm: fewest 2026-11-10 c3-b events at its threshold; tie: fewest in total; tie: fewest cached-embedding (piece grid)
   2026-11-10 room events above t - 0.02 (a smoother false-wake proxy); tie: lower accept rate on `v6/feat/near_test.npy`;
   tie: the order p3a, p3c, p3b.

    v6p3_select.py --arm p3a=v6p3/runs/p3a --arm p3b=v6p3/runs/p3b --arm p3c=v6p3/runs/p3c --out v6p3/selection.json
"""
import argparse
import json
import os
import subprocess
import sys
from pathlib import Path

for _v in ("OMP_NUM_THREADS", "OPENBLAS_NUM_THREADS", "MKL_NUM_THREADS"):
    os.environ.setdefault(_v, "1")
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

import numpy as np  # noqa: E402

import kivi_real_common as C  # noqa: E402

KH = C.kivi_home()
HERE = Path(__file__).resolve().parent
GRID = np.round(np.arange(0.9, 0.99951, 0.0005), 4)
ORDER = ["p3a", "p3c", "p3b"]
V6 = ("releases/v6-oye-kivi-2026-11-11/model/oye_kivi_ctx_uncalibrated", 0.9595)
SEL_DAY = "2026-11-10"
R_SETS = ("v6/feat/pos_test.npy", "v6p1/feat/ctxp_syn_pos_val.npy")
NEAR = "v6/feat/near_test.npy"
SLACK_R = 0.03


def r_curve(head, sets):
    s = [head(np.load(KH / f)) for f in sets]
    return np.array([np.mean([(x > t).mean() for x in s]) for t in GRID]), s


def replay(models, out, jobs):
    """v6_replay.py on 2026-11-10 at the server grid; returns {name: [(device, epoch)]} of detections."""
    if not Path(out).exists():
        cmd = [sys.executable, str(HERE / "v6_replay.py"), "--dataset-root", str(KH / "datasets/real"),
               "--src", "negatives-v5era:*", "--local-date", SEL_DAY, "--bracket", "mid", "--require-grid",
               "--jobs", str(jobs), "--out", str(out)] + [f"--model={n}={f}:{t}" for n, (f, t) in models.items()]
        subprocess.run(cmd, check=True)
        os.chmod(out, 0o600)
    res = json.loads(Path(out).read_text())
    dets = {n: [] for n in models}
    for pc in res["pieces_detail"]:
        for n, ts in pc.get("times", {}).items():
            dets[n] += [(pc["device"], pc["start"] + x) for x in ts]
    return dets


def count(dets, windows):
    import kivi_fair_fa as F
    kept = [(d, t) for d, t in dets if F.window_class(windows, d, t)[0] != "attempt"]
    return {"c3-b": sum(d == "c3-b" for d, _ in kept), "c3-a": sum(d == "c3-a" for d, _ in kept), "total": len(kept),
            "attempts_removed": len(dets) - len(kept)}


def shadow_events(head, thr):
    import v6_room as R
    idx = json.loads((KH / "v6/room_emb/index_v6p2.json").read_text())["pieces"]
    n = 0
    cwd = os.getcwd()
    os.chdir(KH)
    try:
        for pc in idx:
            if pc["part"] != "even-sel" or R.local_date(pc["start_utc"]) != SEL_DAY:
                continue
            steps, p = R.piece_probs(head, np.load(pc["emb"], mmap_mode="r"))
            n += len(R.detections(R.step_time(steps), p, thr))
    finally:
        os.chdir(cwd)
    return n


def main():
    ap = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    ap.add_argument("--arm", action="append", required=True, metavar="NAME=RUN_DIR")
    ap.add_argument("--out", required=True)
    ap.add_argument("--jobs", type=int, default=6)
    a = ap.parse_args()
    import v6_room as R
    import kivi_fair_fa as F
    arms = {s.split("=", 1)[0]: KH / s.split("=", 1)[1] for s in a.arm}
    v6h = R.Head(KH / (V6[0] + ".onnx"))
    r_v6 = float(np.mean([(v6h(np.load(KH / f)) > V6[1]).mean() for f in R_SETS]))
    near = np.load(KH / NEAR)
    res = {"rule": "plan section 9.2", "R_v6": round(100 * r_v6, 2), "arms": {}}
    cand_t = {}
    for n, d in arms.items():
        h = R.Head(d / "kivi.onnx")
        rc, _ = r_curve(h, R_SETS)
        ok = np.flatnonzero(rc >= r_v6 - 1e-12)
        if not len(ok):
            res["arms"][n] = {"eligible": False, "why": "R below R_v6 at every grid value"}
            continue
        t0 = float(GRID[ok.max()])
        tries = []
        for drop in (0.0, 0.01, 0.02, SLACK_R):      # the recall-matched t, then R_v6 - 1, - 2, - 3 points
            k = np.flatnonzero(rc >= r_v6 - drop - 1e-12)
            if len(k) and float(GRID[k.max()]) not in tries:
                tries.append(float(GRID[k.max()]))
        cand_t[n] = tries
        res["arms"][n] = {"t_recall_matched": t0, "R_curve_at": {f"{t:.4f}": round(100 * float(r), 2)
                                                                  for t, r in zip(GRID, rc) if float(t) in tries},
                          "near_test_at_t0": round(100 * float((h(near) > t0).mean()), 3)}
    # one replay with every arm at its first tries (the recall-matched t and the next few values) and v6
    models = {"v6": (str(KH / (V6[0] + ".tflite")), V6[1])}
    for n, tries in cand_t.items():
        for t in tries[:4]:
            models[f"{n}@{t:.4f}"] = (str(arms[n] / "oye_kivi_uncalibrated.tflite"), t)
    out = Path(a.out).with_name("sel_replay_1110.json")
    dets = replay(models, out, a.jobs)
    windows = F.read_windows()
    c6 = count(dets["v6"], windows)
    res["v6_1110"] = c6
    lim_b, lim = max(c6["c3-b"], 1), max(c6["total"], 1)
    chosen = []
    for n, tries in cand_t.items():
        h = R.Head(arms[n] / "kivi.onnx")
        info = res["arms"][n]
        info["tried"] = {}
        pick = None
        for t in tries[:4]:
            c = count(dets[f"{n}@{t:.4f}"], windows)
            info["tried"][f"{t:.4f}"] = c
            if c["c3-b"] <= lim_b and c["total"] <= lim:
                pick = (t, c)
                break
        if pick is None:
            info["eligible"] = False
            info["why"] = "over the 2026-11-10 limits at every tried threshold (or R would drop more than 3 points)"
            continue
        t, c = pick
        rc, _ = r_curve(h, R_SETS)
        info.update({"eligible": True, "threshold": t, "R": round(100 * float(rc[list(GRID).index(t)]), 2), "events_1110": c,
                     "shadow_events_1110": shadow_events(h, t - 0.02), "near_test": round(100 * float((h(near) > t).mean()), 3)})
        chosen.append(n)
    key = lambda n: (res["arms"][n]["events_1110"]["c3-b"], res["arms"][n]["events_1110"]["total"],   # noqa: E731
                     res["arms"][n]["shadow_events_1110"], res["arms"][n]["near_test"], ORDER.index(n))
    if chosen:
        best = sorted(chosen, key=key)[0]
        res["chosen"] = {"arm": best, "threshold": res["arms"][best]["threshold"]}
    else:
        res["chosen"] = None
    Path(a.out).write_text(json.dumps(res, indent=1))
    os.chmod(a.out, 0o600)
    print(json.dumps(res, indent=1))


if __name__ == "__main__":
    main()
