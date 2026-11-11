#!/usr/bin/env python3
"""Kivi v6.1 stage 2: threshold per arm and the arm, by the rule pre-registered on 2026-11-11 (before 2026-11-12's
audio existed). Selection data: the 2026-11-12 room audio (part `sel-v6p1`, even day, used by nothing else), the
2026-11-12 live wake clips, and validation windows no arm was trained on. No odd-day data is read.

Rule (see homelab docs/voice-assistant/experiments/oye-kivi-v6.md, "v6.1"):
1. Reference: v5 (`oye_kivi_next`) at raw 0.9754 on the selection room audio: total events E5 (2 s refractory).
2. Threshold per arm: the lowest raw value t on the grid 0.9000, 0.9005, ..., 0.9995 such that for t and every grid
   value above it (a) total events <= max(E5, 1) and (b) on each device, evening (18-24 Madrid) events <=
   max(1, round(evening hours / 6)). An arm with no such t is not eligible.
3. Arm: the highest R = mean of the accept rates at its threshold on (i) the isolated synthetic validation windows
   (`v6/feat/pos_test.npy`, train voices, held out of every training run), (ii) the synthetic context validation
   windows (`v6p1/feat/ctxp_syn_pos_val.npy`, 500 clips held out of the v6.1 arms), (iii) the selection day's live
   true wakes (label `true`, trigger `wake`, exact replay), only when there are at least 3 of them.
4. Tie-break when the best two R are within 1.0 point: fewer selection-room events at their thresholds, then the lower
   accept rate on the synthetic near-phrase validation windows (`v6/feat/near_test.npy`), then the order p1a, p1c, p1b.

    v6p1_select.py --index v6/room_emb/index.json --part sel-v6p1 --local-date 2026-11-12 --live-date 2026-11-12 \
        --arm p1a=v6p1/runs/p1a --arm p1b=v6p1/runs/p1b --arm p1c=v6p1/runs/p1c --out v6p1/selection.json
"""
import argparse
import csv
import json
import os
import sys
from datetime import datetime, timezone
from pathlib import Path
from zoneinfo import ZoneInfo

import numpy as np

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import v6_room as R  # noqa: E402

WORK = Path(os.environ.get("WORK", "/work/workspaces/kivi-wakeword"))
V5 = ("v5", WORK / "releases/v5-oye-kivi-2026-11-08/model/oye_kivi_next_uncalibrated", 0.9754)
GRID = np.round(np.arange(0.9, 0.99951, 0.0005), 4)
ORDER = ["p1a", "p1c", "p1b"]
TZ = ZoneInfo("Europe/Madrid")


def room_events(head, pieces, floor=0.5):
    """[(device, time s, prob, evening)] of the steps above floor; per device hours and evening hours."""
    steps_out, hours, eh = [], {}, {}
    for pc in pieces:
        d = pc["device"]
        hours[d] = hours.get(d, 0.0) + pc["samples"] / R.RATE / 3600
        eh[d] = eh.get(d, 0.0) + sum(R.evening_hours(pc["start_utc"], pc["samples"]).values())
        e = np.load(pc["emb"], mmap_mode="r")
        steps, p = R.piece_probs(head, e)
        t = R.step_time(steps)
        m = p > floor
        steps_out.append((d, pc["start_utc"], t[m], p[m]))
    return steps_out, hours, eh


def counts(ev, thr):
    """{device: [events, evening events]} at threshold thr (server rule: > thr, 2 s refractory, per piece)."""
    out = {}
    for d, start, t, p in ev:
        c = out.setdefault(d, [0, 0])
        for i in R.detections(t, p, thr):
            c[0] += 1
            c[1] += int(R.is_evening(start, float(t[i]))[0])
    return out


def pick_threshold(ev, eh, cap):
    ok = []
    for t in GRID:
        c = counts(ev, float(t))
        tot = sum(v[0] for v in c.values())
        ev_ok = all(c.get(d, [0, 0])[1] <= max(1, int(round(eh[d] / 6.0))) for d in eh)
        ok.append(tot <= cap and ev_ok)
    best = None
    for i in range(len(GRID) - 1, -1, -1):
        if ok[i]:
            best = float(GRID[i])
        else:
            break
    return best


def accept(head, path, thr):
    X = np.load(path, mmap_mode="r")
    p = head(np.asarray(X, np.float32))
    return float((p > thr).mean()), int(len(p))


def live_recall(tflite, thr, dates, root):
    import kivi_real_common as C
    from eval_stream import load_models, stream, wake_samples
    feats, models = load_models([f"m={tflite}"])
    k = n = 0
    fired_false = n_false = 0
    for r in csv.DictReader(open(Path(root) / "live" / "live_wakes.csv", encoding="utf-8")):
        d = datetime.strptime(r["utc"], "%Y%m%dT%H%M%S.%fZ").replace(tzinfo=timezone.utc).astimezone(TZ).date().isoformat()
        if d not in dates or r["trigger"] != "wake" or r["label"] not in ("true", "false"):
            continue
        tail = Path(root) / "live" / r["device"] / r["utc"] / "wake_tail.wav"
        if not tail.exists():
            continue
        probs, _ = stream(feats, models, C.read_pcm16(tail), n_total=wake_samples(r["wake_len"]))
        hit = bool(len(probs["m"]) and probs["m"].max() > thr)
        if r["label"] == "true":
            n += 1
            k += hit
        else:
            n_false += 1
            fired_false += hit
    return k, n, fired_false, n_false


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--index", required=True)
    ap.add_argument("--part", default="sel-v6p1")
    ap.add_argument("--local-date", default="2026-11-12")
    ap.add_argument("--live-date", default="2026-11-12")
    ap.add_argument("--dataset-root", default=str(WORK / "datasets/real"))
    ap.add_argument("--arm", action="append", required=True, metavar="NAME=RUN_DIR")
    ap.add_argument("--pos-val", default=str(WORK / "v6/feat/pos_test.npy"))
    ap.add_argument("--ctx-val", default=str(WORK / "v6p1/feat/ctxp_syn_pos_val.npy"))
    ap.add_argument("--near-val", default=str(WORK / "v6/feat/near_test.npy"))
    ap.add_argument("--out", required=True)
    a = ap.parse_args()
    a_ns = argparse.Namespace(index=a.index, part=a.part, local_date=a.local_date)
    pieces = R.load_index(a_ns)
    if not pieces:
        sys.exit("no selection pieces (copy and embed the selection day first)")
    dates = set(a.live_date.split(","))
    h5 = R.Head(f"{V5[1]}.onnx")
    ev5, hours, eh = room_events(h5, pieces)
    c5 = counts(ev5, V5[2])
    e5 = sum(v[0] for v in c5.values())
    cap = max(e5, 1)
    res = {"selection": {"part": a.part, "local_date": a.local_date, "hours": hours, "evening_hours": eh,
                         "v5_events": c5, "v5_total": e5, "cap": cap}, "arms": {}}
    k5, n5, f5, nf5 = live_recall(f"{V5[1]}.tflite", V5[2], dates, a.dataset_root)
    res["selection"]["v5_live"] = {"true_detected": k5, "true_n": n5, "false_fired": f5, "false_n": nf5}
    for spec in a.arm:
        name, run = spec.split("=", 1)
        head = R.Head(Path(run) / "kivi.onnx")
        ev, _, _ = room_events(head, pieces)
        t = pick_threshold(ev, eh, cap)
        arm = {"threshold": t, "eligible": t is not None}
        if t is not None:
            c = counts(ev, t)
            pv, npv = accept(head, a.pos_val, t)
            cv, ncv = accept(head, a.ctx_val, t)
            nv, _ = accept(head, a.near_val, t)
            k, n, f, nf = live_recall(Path(run) / "oye_kivi_uncalibrated.tflite", t, dates, a.dataset_root)
            parts = [pv, cv] + ([k / n] if n >= 3 else [])
            arm.update(room_events=c, room_total=sum(v[0] for v in c.values()), pos_val=pv, ctx_val=cv,
                       near_val=nv, live_true=[k, n], live_false_fired=[f, nf], R=float(np.mean(parts)),
                       R_parts=len(parts))
        res["arms"][name] = arm
        print(name, json.dumps(arm), flush=True)
    elig = [n for n, v in res["arms"].items() if v["eligible"]]
    if not elig:
        res["chosen"] = None
    else:
        elig.sort(key=lambda n: -res["arms"][n]["R"])
        best = elig[0]
        if len(elig) > 1 and res["arms"][elig[0]]["R"] - res["arms"][elig[1]]["R"] <= 0.01:
            tied = [n for n in elig if res["arms"][elig[0]]["R"] - res["arms"][n]["R"] <= 0.01]
            tied.sort(key=lambda n: (res["arms"][n]["room_total"], res["arms"][n]["near_val"],
                                     ORDER.index(n) if n in ORDER else 99))
            best = tied[0]
            res["tie_break"] = tied
        res["chosen"] = {"arm": best, "threshold": res["arms"][best]["threshold"]}
    Path(a.out).write_text(json.dumps(res, indent=1))
    print(json.dumps({"v5_total": e5, "chosen": res["chosen"]}))


if __name__ == "__main__":
    main()
