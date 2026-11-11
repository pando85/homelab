#!/usr/bin/env python3
"""Room false wakes through the server's own runtime, like for like with the v5-era mining pass.

Uses `kivi_replay_mine.stream_range` (pyopen-wakeword TFLite runtime, 1200 s ranges with 60 s warm-up, whole 1280-sample
chunks) on the pieces of one copied set (`kivi_negatives.py --out-name`), filtered by part and local date, for any
models at their own raw thresholds. Detections: above the raw threshold, 2 s refractory, per piece; evening = 18-24
Europe/Madrid. Output: events and evening events per device, hours and evening hours, per evening with exact Poisson
95 % intervals, and the stage-3 C3 verdict (`c3_verdict`) when --gate is given. Numbers only, never audio.

    v6_replay.py --set negatives-v5era --part test2-v5era --out r.json \
        --model v5=oye_kivi_next_uncalibrated.tflite:0.9754 --model cand=cand_uncalibrated.tflite:0.9595
    v6_replay.py --set negatives-v6p1 --part test3-v6p1 --local-date 2026-11-13 --gate cand:v5 --out r.json --model ...
"""
import argparse
import json
import multiprocessing as mp
import os
import sys
from datetime import datetime
from zoneinfo import ZoneInfo

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

import numpy as np  # noqa: E402

import kivi_replay_mine as K  # noqa: E402
import v6_room as R  # noqa: E402

TZ = ZoneInfo("Europe/Madrid")
SPECS_ENV = "V6_REPLAY_SPECS"


def parse_specs(specs):
    files = {s.split("=", 1)[0]: s.split("=", 1)[1].rsplit(":", 1)[0] for s in specs}
    thr = {s.split("=", 1)[0]: float(s.rsplit(":", 1)[1]) for s in specs}
    return files, thr


def _install(specs):
    """Point the mining tool's runtime at these models (also in spawn workers, via the environment)."""
    files, _ = parse_specs(specs)
    K.MODEL_FILES = files
    K.NAMES = tuple(files)


if os.environ.get(SPECS_ENV):
    _install(json.loads(os.environ[SPECS_ENV]))


def _task(args):
    clip, path, s0, s1 = args
    if "rt" not in K._W:
        K._W["rt"] = K.load_runtime()
    t, p = K.stream_range(*K._W["rt"], path, s0, s1, 60.0)
    keep = (p > 0.3).any(axis=1)
    return clip, t[keep], p[keep]


def poisson(k, t):
    lo, hi = R.poisson_ci(k, t) if t > 0 else (0.0, 0.0)
    return [round(lo, 3), round(hi, 3)]


def c3_verdict(dev, cand, ref, slack=1):
    """Stage-3 C3 (pre-registered 2026-11-11): (a) per device, evening events <= max(1, round(evening hours / 6));
    (b) pooled, evening events / (evening hours / 6) <= 1; (c) total events <= REF's total + slack."""
    c = dev[cand]
    ok_dev = all(v["k_evening"] <= max(1, int(round(v["evening_h"] / 6.0))) for v in c.values())
    eh = sum(v["evening_h"] for v in c.values())
    ke = sum(v["k_evening"] for v in c.values())
    ok_pool = eh == 0 or ke / (eh / 6.0) <= 1.0
    tot_c = sum(v["k"] for v in c.values())
    tot_r = sum(v["k"] for v in dev[ref].values())
    return {"per_device_evening_ok": ok_dev, "pooled_per_evening_ok": ok_pool, "total": tot_c, "ref_total": tot_r,
            "total_ok": tot_c <= tot_r + slack, "pass": ok_dev and ok_pool and tot_c <= tot_r + slack}


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--dataset-root", default="/work/workspaces/kivi-wakeword/datasets/real")
    ap.add_argument("--set", required=True, help="directory of the copied pieces (kivi_negatives.py --out-name)")
    ap.add_argument("--part", required=True)
    ap.add_argument("--local-date", help="comma list of Europe/Madrid dates")
    ap.add_argument("--model", action="append", required=True, metavar="NAME=uncalibrated.tflite:RAW")
    ap.add_argument("--gate", metavar="CAND:REF", help="also print the C3 verdict of CAND against REF")
    ap.add_argument("--jobs", type=int, default=6)
    ap.add_argument("--out", required=True)
    a = ap.parse_args()
    os.environ[SPECS_ENV] = json.dumps(a.model)
    _install(a.model)
    _, thr = parse_specs(a.model)
    dates = set(a.local_date.split(",")) if a.local_date else None
    pieces = [p for p in K.read_pieces(a.dataset_root, a.set) if p["part"] == a.part and
              (dates is None or datetime.fromtimestamp(p["start"], TZ).date().isoformat() in dates)]
    tasks = K.plan_tasks(pieces, 1200.0)
    res = {}
    with mp.get_context("spawn").Pool(a.jobs) as pool:
        for i, (clip, t, p) in enumerate(pool.imap_unordered(_task, tasks)):
            res.setdefault(clip, []).append((t, p))
            if i % 20 == 0:
                print(f"{i + 1}/{len(tasks)}", file=sys.stderr, flush=True)
    dev = {n: {} for n in K.NAMES}
    for pc in pieces:
        d = pc["device"]
        hours = pc["n"] / 16000 / 3600
        eh = sum(R.evening_hours(datetime.fromtimestamp(pc["start"], TZ).isoformat(), pc["n"]).values())
        parts = res.get(pc["clip"], [])
        t = np.concatenate([x[0] for x in parts]) if parts else np.zeros(0)
        p = np.concatenate([x[1] for x in parts]) if parts else np.zeros((0, len(K.NAMES)))
        o = np.argsort(t, kind="stable")
        t, p = t[o], p[o]
        for k, n in enumerate(K.NAMES):
            c = dev[n].setdefault(d, {"k": 0, "k_evening": 0, "hours": 0.0, "evening_h": 0.0})
            c["hours"] += hours
            c["evening_h"] += eh
            for x in K.detections_of(t, p[:, k], thr[n]):
                c["k"] += 1
                c["k_evening"] += int(18 <= datetime.fromtimestamp(pc["start"] + x, TZ).hour < 24)
    out = {"set": a.set, "part": a.part, "local_date": a.local_date, "thresholds": thr, "pieces": len(pieces),
           "tasks": len(tasks), "models": {}}
    for n, dd in dev.items():
        h = sum(v["hours"] for v in dd.values())
        eh = sum(v["evening_h"] for v in dd.values())
        k = sum(v["k"] for v in dd.values())
        ke = sum(v["k_evening"] for v in dd.values())
        out["models"][n] = {"per_device": {d: {**v, "hours": round(v["hours"], 3), "evening_h": round(v["evening_h"], 3),
                                               "per_evening": round(v["k_evening"] / (v["evening_h"] / 6), 3) if v["evening_h"] else None,
                                               "ci95_per_evening": poisson(v["k_evening"], v["evening_h"] / 6)}
                                           for d, v in dd.items()},
                            "k": k, "hours": round(h, 3), "per_hour": round(k / h, 4) if h else None,
                            "ci95_per_hour": poisson(k, h), "k_evening": ke, "evening_equiv": round(eh / 6, 3),
                            "per_evening": round(ke / (eh / 6), 3) if eh else None, "ci95_per_evening": poisson(ke, eh / 6)}
    if a.gate:
        cand, ref = a.gate.split(":")
        out["c3"] = c3_verdict(dev, cand, ref)
    with open(a.out, "w") as f:
        json.dump(out, f, indent=1)
    print(json.dumps({n: {k: v[k] for k in ("k", "per_hour", "ci95_per_hour", "k_evening", "evening_equiv")}
                      for n, v in out["models"].items()} | ({"c3": out["c3"]} if a.gate else {}), indent=1))


if __name__ == "__main__":
    main()
