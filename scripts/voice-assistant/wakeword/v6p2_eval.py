#!/usr/bin/env python3
"""Kivi v6.2 morning comparison verdict (pre-registered 2026-11-11, amendments 2-5): v6.2 against v5 (the live model)
with the original C1-C6, C3 strict on test2-v5era (with the owner's blind attempt labels) and on the fresh 2026-11-11
evening copy; v6 is reported as information. Reads v6p2/compare/ (v6p2_compare.sh) and the C3 label files. Numbers only.

    v6p2_eval.py --out v6p2/compare/verdict.json
"""
import argparse
import csv
import json
from datetime import datetime
from pathlib import Path
from zoneinfo import ZoneInfo

W = Path("/work/workspaces/kivi-wakeword")
C = W / "v6p2/compare"
TZ = ZoneInfo("Europe/Madrid")
CELLS = ("3m_tv-off", "3m_tv-on", "1.5m_tv-on", "1.5m_tv-off")


def thr_of(d, t):
    return next(k for k in d if abs(float(k) - t) < 1e-6)


def session(js, name, t):
    out = {}
    for e in js:
        src = e["source"]
        ov = e["overall"]
        cell = {c: ov["kivi_by_condition"][c]["cue_hits"][name][thr_of(ov["kivi_by_condition"][c]["cue_hits"][name], t)]["k"]
                for c in CELLS}
        a = ov["kivi_by_condition"]["ALL"]["cue_hits"][name]
        nh = ov["near_total"]["cue_hits"][name]
        out[src] = {"all": a[thr_of(a, t)]["k"], "n": a[thr_of(a, t)]["n"], "cells": cell, "near": nh[thr_of(nh, t)]["k"]}
    return out


def room(js, model, set_name, attempts):
    """events, evening events, per device (k, k_evening, evening_h) after removing attempt-labelled events (+-2 s)."""
    per = {}
    for row in js["pieces_detail"]:
        if row["set"] != set_name:
            continue
        d = row["device"]
        c = per.setdefault(d, {"k": 0, "k_evening": 0, "evening_h": 0.0, "hours": 0.0, "attempts_removed": 0})
        c["hours"] += sum(row["hours_by_date"].values())
        c["evening_h"] += sum(row["evening_h_by_date"].values())
        start = None
        for x in row.get("times", {}).get(model, []):
            if start is None:
                start = piece_start(row)
            t = start + x
            if any(dd == d and abs(t - ta) <= 2.0 for dd, ta in attempts):
                c["attempts_removed"] += 1
                continue
            c["k"] += 1
            c["k_evening"] += int(18 <= datetime.fromtimestamp(t, TZ).hour < 24)
    return per


_PIECES = {}


def piece_start(row):
    if not _PIECES:
        import sys
        sys.path.insert(0, str(Path(__file__).resolve().parent))
        import kivi_replay_mine as K
        for s in ("negatives-v5era", "negatives-1111eve"):
            for p in K.read_pieces(W / "datasets/real", s):
                _PIECES[(s, p["clip"])] = p["start"]
    return _PIECES[(row["set"], row["clip"])]


def c3_ok(per, ref_total, slack=1):
    ok_dev = all(v["k_evening"] <= max(1, int(round(v["evening_h"] / 6.0))) for v in per.values())
    eh = sum(v["evening_h"] for v in per.values())
    ke = sum(v["k_evening"] for v in per.values())
    tot = sum(v["k"] for v in per.values())
    return {"total": tot, "ref_total": ref_total, "evening": ke, "evening_equiv": round(eh / 6, 2),
            "per_device_evening_ok": ok_dev, "pooled_per_evening_ok": eh == 0 or ke / (eh / 6) <= 1.0,
            "total_ok": tot <= ref_total + slack, "pass": ok_dev and (eh == 0 or ke / (eh / 6) <= 1.0) and tot <= ref_total + slack}


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--out", required=True)
    a = ap.parse_args()
    sel = json.load(open(W / "v6p2/selection.json"))["chosen"]
    T = {"oye_kivi_next": 0.9754, "v6m": 0.9595, "v62": sel["threshold"]}
    js = json.load(open(C / "score_1111.json"))
    S = {m: session(js, m, t) for m, t in T.items()}
    v5, v62 = S["oye_kivi_next"], S["v62"]
    c1 = all(v62[s]["all"] >= v5[s]["all"] for s in v5) and \
        all(v62[s]["cells"][c] >= v5[s]["cells"][c] - 1 for s in v5 for c in CELLS)
    c2 = all(v62[s]["near"] <= v5[s]["near"] + 1 for s in v5)
    rows = {r["id"]: r for r in csv.DictReader(open(W / "datasets/real/live-misses/c3_events.csv", encoding="utf-8"))}
    mp = json.load(open(W / "v6/all/c3_events_mapping.json"))
    attempts = [(m["device"], m["t_abs"]) for m in mp if rows[m["id"]]["label"] == "attempt"]
    rj = json.load(open(C / "room.json"))
    R = {}
    for m in ("v5", "v6", "v62"):
        R[m] = {"test2": room(rj, m, "negatives-v5era", attempts), "test2_raw": room(rj, m, "negatives-v5era", []),
                "eve": room(rj, m, "negatives-1111eve", [])}
    tot = lambda per: sum(v["k"] for v in per.values())  # noqa: E731
    c3a = c3_ok(R["v62"]["test2"], tot(R["v5"]["test2"]))
    c3a_raw = c3_ok(R["v62"]["test2_raw"], tot(R["v5"]["test2_raw"]))
    c3b = c3_ok(R["v62"]["eve"], tot(R["v5"]["eve"]))
    c3b = {**c3b, "pass": c3b["total_ok"]}       # the evening copy: total <= v5 + 1 (pre-registered)
    lv = json.load(open(C / "live_test.json"))["summary"]
    k = lambda lab, m: int(lv[lab]["detected"][m].split("/")[0])  # noqa: E731
    c4 = k("true", "v62") >= k("true", "v5") and k("false", "v62") <= k("false", "v5")
    sy = json.load(open(C / "synth.json"))
    c5 = all(sy[s]["v62"]["rate"] >= sy[s]["v5"]["rate"] - 0.05 for s in ("clean", "tv10", "quiet"))
    cal = json.load(open(W / "v6p2/final/calibration_check.json"))
    c6 = cal["disagree"] == 0 and cal["pyopen_wakeword"] == "1.1.0"
    out = {"chosen": sel, "session": S, "room": R, "C1": c1, "C2": c2, "C3_test2": c3a, "C3_test2_unlabelled": c3a_raw,
           "C3_eve": c3b, "C4": {"pass": c4, "live": {lab: lv[lab]["detected"] for lab in ("true", "false", "unclear")}},
           "C5": {"pass": c5, "rates": {s: {m: sy[s][m]["rate"] for m in sy[s]} for s in ("clean", "tv10", "quiet")}},
           "C6": c6}
    out["PASS"] = all([c1, c2, c3a["pass"], c3b["pass"], c4, c5, c6])
    Path(a.out).write_text(json.dumps(out, indent=1, default=str))
    print(json.dumps({k: (v["pass"] if isinstance(v, dict) and "pass" in v else v) for k, v in out.items()
                      if k.startswith("C") or k == "PASS"}, indent=1))
    print(json.dumps({"C3_test2": c3a, "C3_eve": c3b}, indent=1))


if __name__ == "__main__":
    main()
