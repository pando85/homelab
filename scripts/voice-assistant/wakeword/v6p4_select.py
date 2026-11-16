#!/usr/bin/env python3
"""Kivi v6.4 threshold and arm, by the rule pre-registered in kivi-models docs/plan/2026-11-false-wake-plan.md section
13.3 (committed before training). Reads only: the synthetic validation and C5 sets (no day data), the 2026-11-14 room audio
and live clips (the selection day; no v6.4 arm trained on it, and neither did v6.3) and the window tables (owner-labelled
attempts). Counts only.

Reference = the live model v6.3 (`oye_kivi_ctx3`, raw 0.9450).
1. Tries per arm: the highest raw value t on the grid 0.9000, 0.9005, ..., 0.9995 with R(t) >= R_ref (mean accept rate on
   `v6/feat/pos_test.npy` and `v6p1/feat/ctxp_syn_pos_val.npy`), then the highest grid values with R >= R_ref - 1, - 2, - 3
   points (up to four tries per arm).
2. Must-hold at a try (K-302), all of:
   (a) C5: accept rate of the held-out-voice positives (clean, TV 10 dB, quiet) within 5 points of v6.3's at 0.9450, each;
   (b) calls on the selection day: live clips labelled true (trusted) detected (a crossing within [tail end - 3 s, tail end
       + 1 s], kivi_fair_fa.score_clip) plus owner-labelled attempt windows (a detection within 3 s on the room replay) >=
       the same count for v6.3 at 0.9450. A try that fails is out; an arm with no try left is out.
3. False fires on the selection day at a try: (a) TFLite detections at the server grid (`v6_replay.py`, 2 s refractory) of the
   whole day minus those within 3 s of an owner-labelled attempt, plus (b) the live clips of the day labelled false (trusted)
   on which the model crosses anywhere in tail + request audio. Counted per TV and in total; v6.3 in the same run.
4. Choice among all eligible (arm, try): fewest c3-b fires; tie: fewest in total; tie: fewest room detections at t - 0.02
   (a smoother proxy, same replay); tie: fewest of the 787 decoy evaluation clips fired; tie: lower accept rate on
   `v6/feat/near_test.npy`; tie: lower t (more recall); tie: s1, s3, s2. No eligible (arm, try): no v6.4, nothing packaged.

    v6p4_select.py --arm s1=v6p4/runs/s1 --arm s2=v6p4/runs/s2 --arm s3=v6p4/runs/s3 --out v6p4/selection.json
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
ORDER = ["s1", "s3", "s2"]
REF = ("releases/v6-p3-oye-kivi-2026-11-13/model/oye_kivi_ctx3_uncalibrated", 0.945)     # v6.3, live
SEL_DAY = "2026-11-14"
R_SETS = ("v6/feat/pos_test.npy", "v6p1/feat/ctxp_syn_pos_val.npy")
NEAR = "v6/feat/near_test.npy"
C5_SETS = (("clean", "vnext/feat/eval_pos_clean.npz"), ("tv10", "vnext/feat/eval_pos_snr10.npz"),
           ("quiet", "vnext/feat/eval_pos_quiet.npz"))
DECOY = "v6p3r/decoys/feat/eval_decoy_clean.npz"
C5_POINTS = 0.05
MATCH_S = 3.0
SLACKS = (0.0, 0.01, 0.02, 0.03)


def r_curve(head, sets):
    s = [head(np.load(KH / f)) for f in sets]
    return np.array([np.mean([(x > t).mean() for x in s]) for t in GRID])


def replay(specs, out, jobs):
    """v6_replay.py on the selection day's room audio at the server grid; {name: [(device, epoch)]} of detections."""
    if not Path(out).exists():
        cmd = [sys.executable, str(HERE / "v6_replay.py"), "--dataset-root", str(KH / "datasets/real"),
               "--src", "negatives-daily:*", "--local-date", SEL_DAY, "--bracket", "mid", "--require-grid",
               "--jobs", str(jobs), "--out", str(out)] + [f"--model={n}={f}:{t}" for n, (f, t) in specs.items()]
        subprocess.run(cmd, check=True)
        os.chmod(out, 0o600)
    res = json.loads(Path(out).read_text())
    dets = {n: [] for n in specs}
    for pc in res["pieces_detail"]:
        for n, ts in pc.get("times", {}).items():
            dets[n] += [(pc["device"], pc["start"] + x) for x in ts]
    return dets


def live_scores(specs):
    """Per model: crossed false clips and caught true clips (trusted labels) of the selection day, per TV."""
    import eval_stream as E
    import kivi_fair_fa as F
    import kivi_live_wakes as L
    F.MODELS = {n: (f, t, "") for n, (f, t) in specs.items()}
    feats, models = E.load_models([f"{n}={f}" for n, (f, _) in specs.items()])
    false = {n: {"c3-a": 0, "c3-b": 0} for n in specs}
    true = {n: {"c3-a": 0, "c3-b": 0} for n in specs}
    total = {"false": {"c3-a": 0, "c3-b": 0}, "true": {"c3-a": 0, "c3-b": 0}}
    for r in L.read_table(F.ROOT / "live" / "live_wakes.csv"):
        if F.madrid(L.detection_epoch(r))[0] != SEL_DAY:
            continue
        cls = F.live_class(r)
        if cls not in ("false", "true"):
            continue
        d = F.ROOT / "live" / r["device"] / r["utc"]
        tail = C.read_pcm16(d / "wake_tail.wav")
        stt = C.read_pcm16(d / "stt.wav") if (d / "stt.wav").exists() else np.zeros(0, np.int16)
        sc = F.score_clip(feats, models, tail, stt, E.wake_samples(r["wake_len"] or 0))
        total[cls][r["device"]] += 1
        for n, (first, _pk, hit) in sc.items():
            if cls == "false":
                false[n][r["device"]] += int(first is not None)
            else:
                true[n][r["device"]] += int(hit)
    return false, true, total


def c5_rates(head, raw):
    import v6_seq_eval as SE
    return {lab: float((SE.clip_max(head, np.load(KH / f)) > raw).mean()) for lab, f in C5_SETS}


def main():
    ap = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    ap.add_argument("--arm", action="append", required=True, metavar="NAME=RUN_DIR")
    ap.add_argument("--out", required=True)
    ap.add_argument("--jobs", type=int, default=7)
    a = ap.parse_args()
    import v6_room as R
    import v6_seq_eval as SE
    import kivi_clean as KC
    arms = {s.split("=", 1)[0]: KH / s.split("=", 1)[1] for s in a.arm}
    ref_h = R.Head(KH / (REF[0] + ".onnx"))
    r_ref = float(np.mean([(ref_h(np.load(KH / f)) > REF[1]).mean() for f in R_SETS]))
    c5_ref = c5_rates(ref_h, REF[1])
    near = np.load(KH / NEAR)
    dz = np.load(KH / DECOY)
    res = {"rule": "plan section 13.3", "selection_day": SEL_DAY, "R_ref": round(100 * r_ref, 2),
           "c5_ref": {k: round(100 * v, 2) for k, v in c5_ref.items()}, "arms": {}}
    heads, tries = {}, {}
    for n, d in arms.items():
        h = heads[n] = R.Head(d / "kivi.onnx")
        rc = r_curve(h, R_SETS)
        tt = []
        for drop in SLACKS:
            k = np.flatnonzero(rc >= r_ref - drop - 1e-12)
            if len(k) and float(GRID[k.max()]) not in tt:
                tt.append(float(GRID[k.max()]))
        tries[n] = tt
        mx = {lab: SE.clip_max(h, np.load(KH / f)) for lab, f in C5_SETS}
        mxd = SE.clip_max(h, dz)
        info = {"tries": tt, "per_try": {}}
        for t in tt:
            info["per_try"][f"{t:.4f}"] = {
                "R": round(100 * float(rc[list(GRID).index(t)]), 2),
                "c5": {lab: round(100 * float((m > t).mean()), 2) for lab, m in mx.items()},
                "c5_ok": all(float((m > t).mean()) >= c5_ref[lab] - C5_POINTS for lab, m in mx.items()),
                "decoy_fired": int((mxd > t).sum()), "near_test": round(100 * float((h(near) > t).mean()), 3)}
        res["arms"][n] = info
    specs = {"v6.3": (str(KH / (REF[0] + ".tflite")), REF[1])}
    for n, tt in tries.items():
        f = str(arms[n] / "oye_kivi_uncalibrated.tflite")
        for t in tt:
            specs[f"{n}@{t:.4f}"] = (f, t)
            specs[f"{n}@{t:.4f}-0.02"] = (f, round(t - 0.02, 4))
    dets = replay(specs, Path(a.out).with_name("sel_replay_1114.json"), a.jobs)
    lfalse, ltrue, ltotal = live_scores(specs)
    res["live_clips_selection_day"] = ltotal
    attempts = [{"device": w["device"], "t": w["t"], "label": "attempt"} for w in KC.window_rows()
                if w["label"] == "attempt" and w["source"] in KC.OWNER and KC.madrid(w["t"])[0] == SEL_DAY]
    res["owner_attempt_windows_selection_day"] = len(attempts)
    import kivi_fair_fa as F

    def count(name):
        kept = [(d, t) for d, t in dets[name] if F.window_class(attempts, d, t)[0] != "attempt"]
        c = {"c3-b": sum(d == "c3-b" for d, _ in kept) + lfalse[name]["c3-b"],
             "c3-a": sum(d == "c3-a" for d, _ in kept) + lfalse[name]["c3-a"],
             "room": len(kept), "live_false": lfalse[name]}
        c["total"] = c["c3-a"] + c["c3-b"]
        caught_att = sum(any(d == x["device"] and abs(t - x["t"]) <= MATCH_S for d, t in dets[name]) for x in attempts)
        c["calls"] = sum(ltrue[name].values()) + caught_att
        c["calls_detail"] = {"live_true": ltrue[name], "attempt_windows": caught_att}
        return c

    ref = count("v6.3")
    res["v6.3_selection_day"] = ref
    chosen = []
    for n, tt in tries.items():
        for t in tt:
            c = count(f"{n}@{t:.4f}")
            c["room_at_t-0.02"] = len(dets[f"{n}@{t:.4f}-0.02"])
            pt = res["arms"][n]["per_try"][f"{t:.4f}"]
            pt["selection_day"] = c
            pt["calls_ok"] = c["calls"] >= ref["calls"]
            pt["eligible"] = bool(pt["c5_ok"] and pt["calls_ok"])
            if pt["eligible"]:
                chosen.append((n, t))

    def key(nt):
        n, t = nt
        pt = res["arms"][n]["per_try"][f"{t:.4f}"]
        c = pt["selection_day"]
        return (c["c3-b"], c["total"], c["room_at_t-0.02"], pt["decoy_fired"], pt["near_test"], t, ORDER.index(n))
    res["chosen"] = None
    if chosen:
        n, t = min(chosen, key=key)
        res["chosen"] = {"arm": n, "threshold": t}
    Path(a.out).write_text(json.dumps(res, indent=1))
    os.chmod(a.out, 0o600)
    print(json.dumps(res, indent=1))


if __name__ == "__main__":
    main()
