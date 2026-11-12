#!/usr/bin/env python3
"""Kivi v6.3: the known calls of the 2026-11-12 fair recount (TEST days 2026-11-09 and -11, REUSED: reporting only, run
after the arm and threshold are frozen), scored for any model exactly as `kivi_fair_fa.py` does, without a full room replay:

- live clips labelled true (trusted sources, detection after the room-copy start): the model crosses within
  [tail end - 3 s, tail end + 1 s] of wake tail + request audio streamed on the server grid (kivi_fair_fa.score_clip);
- owner-labelled attempt windows (unique per TV within 3 s, five window tables): the room piece that holds the attempt is
  replayed from 60 s before to 5 s after it on the server grid (`kivi_replay_mine.stream_range`, src_sample0 of the piece);
  detected = a detection (above raw, 2 s refractory) within 3 s of the window time.
Counts only.

    v6p3_calls.py --model v6=releases/.../oye_kivi_ctx_uncalibrated.tflite:0.9595 --model p3a=v6p3/runs/p3a/...:0.98 \
        --out v6p3/eval/known_calls.json
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

import kivi_fair_fa as F  # noqa: E402
import kivi_real_common as C  # noqa: E402

KH = C.kivi_home()
TEST_DAYS = ("2026-11-09", "2026-11-11")


def main():
    ap = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    ap.add_argument("--model", action="append", required=True, metavar="NAME=uncalibrated.tflite:RAW")
    ap.add_argument("--out", required=True)
    a = ap.parse_args()
    import eval_stream as E
    import kivi_live_wakes as L
    import kivi_replay_mine as K
    specs = {s.split("=", 1)[0]: (str(KH / s.split("=", 1)[1].rsplit(":", 1)[0]), float(s.rsplit(":", 1)[1])) for s in a.model}
    F.MODELS = {n: (f, t, "") for n, (f, t) in specs.items()}
    feats, models = E.load_models([f"{n}={f}" for n, (f, _) in specs.items()])
    res = {"models": {n: t for n, (_, t) in specs.items()}, "live_true": {n: [0, 0] for n in specs},
           "attempts": {n: [0, 0] for n in specs}, "per_tv": {}}
    for r in L.read_table(F.ROOT / "live" / "live_wakes.csv"):
        t = L.detection_epoch(r)
        day = F.madrid(t)[0]
        if day not in TEST_DAYS or t < L.epoch(F.SINCE) or F.live_class(r) != "true":
            continue
        d = F.ROOT / "live" / r["device"] / r["utc"]
        tail = C.read_pcm16(d / "wake_tail.wav")
        stt = C.read_pcm16(d / "stt.wav") if (d / "stt.wav").exists() else np.zeros(0, np.int16)
        sc = F.score_clip(feats, models, tail, stt, E.wake_samples(r["wake_len"] or 0))
        for n, (_, _, hit) in sc.items():
            res["live_true"][n][0] += int(hit)
            res["live_true"][n][1] += 1
            pt = res["per_tv"].setdefault(f"{r['device']}|live_true|{n}", [0, 0])
            pt[0] += int(hit)
            pt[1] += 1
    # attempts: unique per TV within MATCH_S, on the test days
    att = []
    for w in F.read_windows():
        if w["label"] != "attempt" or F.madrid(w["t"])[0] not in TEST_DAYS:
            continue
        if not any(x["device"] == w["device"] and abs(x["t"] - w["t"]) <= F.MATCH_S for x in att):
            att.append(w)
    pieces = []
    for st in ("negatives-v5era", "negatives-daily", "negatives-1111eve"):
        pieces += [p for p in K.read_pieces(F.ROOT, st, bracket="mid")]
    K.MODEL_FILES = {n: f for n, (f, _) in specs.items()}
    K.NAMES = tuple(specs)
    rt = K.load_runtime()
    for w in att:
        pc = next((p for p in pieces if p["device"] == w["device"] and p["start"] <= w["t"] <= p["start"] + p["n"] / 16000
                   and p["grid"] != "unknown"), None)
        if pc is None:
            res.setdefault("attempts_without_piece", 0)
            res["attempts_without_piece"] += 1
            continue
        s = int((w["t"] - pc["start"]) * 16000)
        t, p = K.stream_range(*rt, pc["path"], max(0, s - 5 * 16000), s + 5 * 16000, 60.0, src0=pc["src0"])
        for k, n in enumerate(K.NAMES):
            hit = any(abs(pc["start"] + x - w["t"]) <= F.MATCH_S for x in K.detections_of(t, p[:, k], specs[n][1]))
            res["attempts"][n][0] += int(hit)
            res["attempts"][n][1] += 1
            pt = res["per_tv"].setdefault(f"{w['device']}|attempts|{n}", [0, 0])
            pt[0] += int(hit)
            pt[1] += 1
    res["all_calls"] = {n: [res["live_true"][n][0] + res["attempts"][n][0], res["live_true"][n][1] + res["attempts"][n][1]]
                        for n in specs}
    Path(a.out).parent.mkdir(parents=True, exist_ok=True)
    Path(a.out).write_text(json.dumps(res, indent=1))
    os.chmod(a.out, 0o600)
    print(json.dumps({k: res[k] for k in ("models", "live_true", "attempts", "all_calls")}, indent=1))


if __name__ == "__main__":
    main()
