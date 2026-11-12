#!/usr/bin/env python3
"""Fair false-wake comparison of the household wake word models on the server's true grid (2026-11-12).

Why: the room audio copied by kivi_negatives.py leaves out the end of every wake file the LIVE model closed, so the live
model's own false wakes are never in its room replay count, while every candidate is counted on everything; and every replay
before 2026-11-12 ran on the piece's own grid, not the server's (kivi_replay_mine.grid_phase). Here each model M gets

    false wakes(M) = (a) + (b)
    (a) M's detections on the room pieces at the server grid (v6_replay.py), minus those within MATCH_S of an owner-labelled
        attempt; labelled other speech / tv-media / noise count, and so do unlabelled and unclear ones (conservative);
    (b) the live clips labelled false (owner, owner-button, trusted auto) on which M crosses anywhere in the wake tail plus
        the request audio, streamed on the server grid from wake-file sample 0 (eval_stream.stream with n_total).

and recall on the live clips labelled true (crossing within RECALL_WIN of the tail end) and on the owner-labelled attempts
of the window tables (a detection within MATCH_S). Steps (numbers only; no audio is played, printed or saved):

    kivi_fair_fa.py room    [--jobs 6]   # v6_replay.py at the server grid on the room sets of the days (one heavy job)
    kivi_fair_fa.py bracket              # the pieces whose grid is only bracketed: every offset of the bracket and 8 phases
    kivi_fair_fa.py live                 # the live clips (tail + request), every model; 2026-11-12 on: the live model only
    kivi_fair_fa.py report               # reads the label tables fresh, writes RESULTS.md and results.json (counts only)
    kivi_fair_fa.py cut                  # (a) events with no labelled window within MATCH_S: 6 s windows appended to the
                                         # owner's queue (daily_miss_candidates.csv, blind: no model name, no score)

Every step takes `--config FILE` (JSON; v6.3 gate 2026-11-12): models {name: [release file under $KIVI_HOME, raw,
live-wakes model id]}, days, reserved, since, room_runs [[tag, "set:part", "dates"]], out (dir under $KIVI_HOME), ref (the
paired reference, first in the recall table), primary_device, gate {cand, ref, device} (adds the pre-registered gate block).
Without --config the 2026-11-12 recount (v5 / v6 / v6.2 on 2026-11-08 to -11) is reproduced.

Days (Europe/Madrid): DAYS below; odd = TEST (reported, nothing chosen), even = info. No model other than the live one is
scored on 2026-11-12 or 2026-11-13 audio (reserved for the pre-registered v6.1 stages 2 and 3).
"""
import argparse
import csv
import json
import math
import os
import subprocess
import sys
from collections import Counter, defaultdict
from datetime import datetime, timezone
from pathlib import Path
from zoneinfo import ZoneInfo

for _v in ("OMP_NUM_THREADS", "OPENBLAS_NUM_THREADS", "MKL_NUM_THREADS"):
    os.environ.setdefault(_v, "1")
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

import numpy as np  # noqa: E402

import kivi_live_wakes as L  # noqa: E402
import kivi_real_common as C  # noqa: E402

TZ = ZoneInfo("Europe/Madrid")
RATE = 16000
KH = C.kivi_home()
ROOT = KH / "datasets" / "real"
OUT = KH / "audit" / "fair_fa"
HERE = Path(__file__).resolve().parent

MODELS = {   # name: (release model file under $KIVI_HOME, raw threshold, live-wakes model id)
    "v5": ("releases/v5-oye-kivi-2026-11-08/model/oye_kivi_next_uncalibrated.tflite", 0.9754, "oye_kivi_next"),
    "v6": ("releases/v6-oye-kivi-2026-11-11/model/oye_kivi_ctx_uncalibrated.tflite", 0.9595, "oye_kivi_ctx"),
    "v6.2": ("releases/v6-p2-oye-kivi-2026-11-12/model/oye_kivi_ctx2_uncalibrated.tflite", 0.9825, "oye_kivi_ctx2"),
}
DAYS = ("2026-11-08", "2026-11-09", "2026-11-10", "2026-11-11")
RESERVED = ("2026-11-12", "2026-11-13")         # v6.1 stages 2 and 3: only the live model's own clips
SINCE = "2026-11-08T13:30:00Z"                  # the room copy starts here (v5-era copy --since); live clips before it are out
# room sources per day: the v5-era copy (every closed file 2026-11-08 13:30Z to 11-11 16:30Z, cut at local midnight) for
# 11-08 to 11-10, and the whole-day copy of 11-11 (it contains the 11-11 pieces of the v5-era copy and the evening copy)
ROOM_RUNS = (("v5era", "negatives-v5era:*", "2026-11-08,2026-11-09,2026-11-10"),
             ("d1111", "negatives-daily:d-20261111", "2026-11-11"))
WINDOW_TABLES = ("daily_false_wakes.csv", "daily_miss_candidates.csv", "c3_events.csv", "c3_events_r2.csv",
                 "miss_candidates.csv")
MATCH_S = 3.0                   # a detection within this of a labelled window's time is that window
MERGE_S = 2.0                   # detections of different models this close (same TV) are one event
RECALL_WIN = (-3.0, 1.0)        # a true live clip is detected when the model crosses in [tail end - 3 s, tail end + 1 s]
FALSE_LIVE = ("false",)
REF = "v5"                      # paired reference (config `ref`); the other models are candidates
GATE = None                     # config `gate`: {"cand", "ref", "device"}
CUT_TABLE = "daily_miss_candidates.csv"


def apply_config(path):
    """Override the module settings from a JSON config (see the module doc)."""
    global MODELS, DAYS, RESERVED, SINCE, ROOM_RUNS, OUT, REF, GATE
    cfg = json.loads(Path(path).read_text())
    MODELS = {n: tuple(v) for n, v in cfg["models"].items()}
    DAYS = tuple(cfg["days"])
    RESERVED = tuple(cfg.get("reserved", ()))
    SINCE = cfg.get("since", SINCE)
    ROOM_RUNS = tuple(tuple(r) for r in cfg["room_runs"])
    OUT = KH / cfg["out"]
    REF = cfg.get("ref", next(iter(MODELS)))
    GATE = cfg.get("gate")
    if REF in MODELS:                 # the reference first: recall() pairs every model with names[0]
        MODELS = {REF: MODELS[REF], **{n: v for n, v in MODELS.items() if n != REF}}
    return cfg
TRUSTED = ("owner", "owner-button", "auto-v1", "")     # label sources: owner, the false-wake button, trusted auto, no source


def madrid(t):
    d = datetime.fromtimestamp(t, TZ)
    return d.date().isoformat(), 18 <= d.hour < 24


def day_role(day):
    return "test" if int(day[-2:]) % 2 else "info"


def utc_epoch(stamp):
    return datetime.strptime(stamp, "%Y%m%dT%H%M%S.%fZ").replace(tzinfo=timezone.utc).timestamp()


def scope(cmd):
    """The resource scope of every heavy python run on the shared machine."""
    return ["systemd-run", "--user", "--scope", "-q", "-p", "MemoryMax=12G", "-p", "CPUQuota=800%", "nice", "-n", "10"] + cmd


# ---------------------------------------------------------------------------------------------
# room: v6_replay.py at the server grid
# ---------------------------------------------------------------------------------------------


def cmd_room(a):
    py = sys.executable
    for tag, src, dates in ROOM_RUNS:
        out = OUT / f"room_{tag}.json"
        if out.exists() and not a.redo:
            print(f"{out.name} exists (--redo to score again)")
            continue
        cmd = [py, str(HERE / "v6_replay.py"), "--dataset-root", str(ROOT), "--src", src, "--local-date", dates,
               "--bracket", "mid", "--require-grid", "--jobs", str(a.jobs), "--out", str(out)]
        cmd += [f"--model={n}={KH / f}:{thr}" for n, (f, thr, _) in MODELS.items()]
        print(" ".join(cmd[:8]) + " ...", flush=True)
        subprocess.run(scope(cmd), check=True)
        os.chmod(out, 0o600)


# ---------------------------------------------------------------------------------------------
# bracketed pieces: every offset of the bracket (the truth is one of them) and the 8 grid phases
# ---------------------------------------------------------------------------------------------


def _models_runtime():
    import kivi_replay_mine as K
    K.MODEL_FILES = {n: KH / f for n, (f, _, _) in MODELS.items()}
    K.NAMES = tuple(MODELS)
    return K, K.load_runtime()


def cmd_bracket(a):
    K, rt = _models_runtime()
    out = []
    for _tag, src, dates in ROOM_RUNS:
        st, part = src.split(":")
        for pc in K.read_pieces(ROOT, st):
            if pc["grid"] != "bracketed" or (part != "*" and pc["part"] != part) or madrid(pc["start"])[0] not in dates:
                continue
            g = K.read_grid(ROOT / st)[pc["clip"]]
            offs = sorted(set(range(g["lo"], g["hi"] + 1)) | {g["lo"] + k * 160 for k in range(8)})
            for s0 in offs:
                t, p = K.stream_range(*rt, pc["path"], 0, pc["n"], warm_s=0, src0=s0)
                row = {"clip": pc["clip"], "device": pc["device"], "src0": s0, "in_bracket": g["lo"] <= s0 <= g["hi"],
                       "hours": pc["n"] / RATE / 3600}
                for k, (n, (_, thr, _)) in enumerate(MODELS.items()):
                    row[f"{n}_peak"] = round(float(p[:, k].max()) if len(p) else 0.0, 4)
                    row[f"{n}_dets"] = len(K.detections_of(t, p[:, k], thr))
                out.append(row)
    path = OUT / "bracket.json"
    path.write_text(json.dumps(out, indent=1))
    os.chmod(path, 0o600)
    print(f"{len(out)} piece-offset rows -> {path.name}")


# ---------------------------------------------------------------------------------------------
# live clips: wake tail + request audio, server grid from wake-file sample 0
# ---------------------------------------------------------------------------------------------


def live_models_for(row):
    """Models to score on one live clip: all on DAYS; on the reserved days only the clip's own (live) model."""
    t = L.detection_epoch(row)
    day = madrid(t)[0]
    if t < L.epoch(SINCE):
        return []
    if day in RESERVED:
        return [n for n, (_, _, mid) in MODELS.items() if mid == row["model"]]
    return list(MODELS) if day in DAYS else []


def score_clip(feats, models, tail, stt, n_wake):
    """{model: (first crossing time from the tail end or None, max prob in the recall window, crossing in it)} of one clip:
    tail + request streamed as the end of a stream of n_wake + len(stt) samples (the tail ends at wake sample n_wake)."""
    from eval_stream import stream
    x = np.concatenate([tail, stt]) if len(stt) else tail
    probs, times = stream(feats, models, x, n_total=max(n_wake, len(tail)) + len(stt))
    t_end = len(tail) / RATE
    res = {}
    for n, p in probs.items():
        thr = MODELS[n][1]
        rel = times - t_end
        cross = np.flatnonzero(p > thr)
        win = (rel >= RECALL_WIN[0]) & (rel <= RECALL_WIN[1])
        res[n] = (float(rel[cross[0]]) if len(cross) else None, float(p[win].max()) if win.any() else 0.0,
                  bool((p[win] > thr).any()))
    return res


def cmd_live(a):
    import eval_stream as E
    rows = L.read_table(ROOT / "live" / "live_wakes.csv")
    feats, models = E.load_models([f"{n}={KH / f}" for n, (f, _, _) in MODELS.items()])
    out = []
    for r in rows:
        names = live_models_for(r)
        if not names:
            continue
        d = ROOT / "live" / r["device"] / r["utc"]
        tail = C.read_pcm16(d / "wake_tail.wav")
        stt = C.read_pcm16(d / "stt.wav") if (d / "stt.wav").exists() else np.zeros(0, np.int16)
        res = score_clip(feats, {n: models[n] for n in names}, tail, stt, E.wake_samples(r["wake_len"] or 0))
        rec = {"run_id": r["run_id"], "device": r["device"], "model_live": r["model"], "trigger": r["trigger"]}
        for n, (first, pk, hit) in res.items():
            rec[f"{n}_first_s"] = "" if first is None else round(first, 2)
            rec[f"{n}_win_peak"] = round(pk, 4)
            rec[f"{n}_win_cross"] = int(hit)
        out.append(rec)
    keys = sorted({k for r in out for k in r}, key=lambda k: (k[0] != "r", k))
    path = OUT / "live_scores.csv"
    with open(path, "w", newline="") as f:
        w = csv.DictWriter(f, keys)
        w.writeheader()
        w.writerows(out)
    os.chmod(path, 0o600)
    print(f"{len(out)} live clips scored -> {path.name}")


# ---------------------------------------------------------------------------------------------
# report (pure parts first)
# ---------------------------------------------------------------------------------------------


def poisson_ci(k, t):
    from scipy.stats import chi2
    if t <= 0:
        return (math.nan, math.nan)
    lo = 0.0 if k == 0 else chi2.ppf(0.025, 2 * k) / 2
    return lo / t, chi2.ppf(0.975, 2 * k + 2) / 2 / t


def binom_two_sided(k, n):
    from scipy.stats import binomtest
    return 1.0 if n == 0 else float(binomtest(k, n, 0.5).pvalue)


def read_windows():
    """[{device, t, label, table, id}] of every labelled-or-not window row (all tables; read fresh)."""
    out = []
    for name in WINDOW_TABLES:
        p = ROOT / "live-misses" / name
        if not p.exists():
            continue
        for r in L.read_miss_table(p):
            out.append({"device": r["device"], "t": utc_epoch(r["utc"]), "label": r["label"] or "", "table": name,
                        "id": r["id"]})
    return out


def window_class(windows, device, t, match_s=MATCH_S):
    """attempt / false (other speech, tv, noise) / unclear / unlabelled of a detection, from the windows within match_s;
    an attempt among them wins (it is a call), then a false label, then unclear; also the number of distinct labels."""
    labs = {w["label"] for w in windows if w["device"] == device and abs(w["t"] - t) <= match_s}
    labs.discard("")
    if "attempt" in labs:
        cls = "attempt"
    elif labs & {"other-speech", "tv-media", "noise"}:
        cls = "false"
    elif "unclear" in labs:
        cls = "unclear"
    else:
        cls = "unlabelled"
    return cls, len(labs)


def room_events(room, names):
    """[{device, t, day, evening, models: set}] from v6_replay outputs: detections merged across models within MERGE_S."""
    dets = defaultdict(list)
    for res in room:
        for pc in res["pieces_detail"]:
            for n, ts in pc.get("times", {}).items():
                if n in names:
                    for x in ts:
                        dets[pc["device"]].append((pc["start"] + x, n))
    out = []
    for dev, xs in dets.items():
        xs.sort()
        cur = None
        for t, n in xs:
            if cur is not None and t - cur["t_last"] <= MERGE_S:
                cur["models"].add(n)
                cur["t_last"] = t
            else:
                day, ev = madrid(t)
                cur = {"device": dev, "t": t, "t_last": t, "day": day, "evening": ev, "models": {n}}
                out.append(cur)
    return out


def room_hours(room):
    """{(device, day): [hours, evening hours]} of the scored room pieces."""
    h = defaultdict(lambda: [0.0, 0.0])
    for res in room:
        for pc in res["pieces_detail"]:
            for d, v in pc["hours_by_date"].items():
                h[(pc["device"], d)][0] += v
            for d, v in pc["evening_h_by_date"].items():
                h[(pc["device"], d)][1] += v
    return h


def live_class(row):
    lab, src = row["label"], row["label_source"]
    if lab == "true" and src in TRUSTED:
        return "true"
    if lab == "false" and src in TRUSTED:
        return "false"
    if lab in ("unclear", "not-a-wake"):
        return lab
    return "unlabelled"


def cmd_report(a):
    names = list(MODELS)
    room = [json.loads((OUT / f"room_{tag}.json").read_text()) for tag, _, _ in ROOM_RUNS]
    windows = read_windows()
    hours = room_hours(room)
    events = room_events(room, names)
    for e in events:
        e["cls"], e["n_labels"] = window_class(windows, e["device"], e["t"])
    live = {r["run_id"]: r for r in L.read_table(ROOT / "live" / "live_wakes.csv")}
    scores = list(csv.DictReader(open(OUT / "live_scores.csv")))
    clips = []
    for s in scores:
        r = live[s["run_id"]]
        t = L.detection_epoch(r)
        day, ev = madrid(t)
        clips.append({"run_id": s["run_id"], "device": r["device"], "day": day, "evening": ev, "cls": live_class(r),
                      "source": r["label_source"] or "no-source",
                      "trigger": r["trigger"], "model_live": r["model"],
                      "fires": {n for n in names if s.get(f"{n}_first_s", "") != ""},
                      "recall": {n for n in names if s.get(f"{n}_win_cross") == "1"},
                      "scored": {n for n in names if f"{n}_win_cross" in s and s[f"{n}_win_cross"] != ""}})
    res = {"generated_utc": datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ"), "models": {n: MODELS[n][1] for n in names},
           "cells": [], "pooled": {}, "paired": {}, "recall": {}, "label_state": label_state(live, windows),
           "grid": [r.get("grid") for r in room], "room_events": len(events)}
    devs = sorted({d for d, _ in hours})
    for n in names:
        for dev in devs + ["both"]:
            for day in DAYS:
                res["cells"].append(cell(n, dev, day, events, clips, hours))
        for role, days in (("test", [d for d in DAYS if day_role(d) == "test"]), ("info", [d for d in DAYS if day_role(d) == "info"])):
            for dev in devs + ["both"]:
                res["pooled"][f"{n}|{role}|{dev}"] = cell(n, dev, days, events, clips, hours)
    for role, days in (("test", [d for d in DAYS if day_role(d) == "test"]), ("info", [d for d in DAYS if day_role(d) == "info"])):
        for cand in [n for n in names if n != REF]:
            for scope_ev in (False, True):
                for dev in [None] + devs:
                    key = f"{cand}-vs-{REF}|{role}|{'evening' if scope_ev else 'day'}" + (f"|{dev}" if dev else "")
                    res["paired"][key] = paired(cand, REF, days, events, clips, scope_ev, device=dev)
        res["recall"][role] = recall(names, days, clips, windows, events)
    res["reserved_live"] = reserved_live(clips)
    if GATE:
        res["gate"] = gate_block(res, GATE)
    write_unlabelled(events)
    res["bracket"] = bracket_summary()
    (OUT / "results.json").write_text(json.dumps(res, indent=1, default=list))
    os.chmod(OUT / "results.json", 0o600)
    (OUT / "RESULTS.md").write_text(render(res))
    os.chmod(OUT / "RESULTS.md", 0o600)
    print((OUT / "RESULTS.md").read_text())


def counted(e, n):
    return n in e["models"] and e["cls"] != "attempt"


def cell(n, dev, days, events, clips, hours):
    days = [days] if isinstance(days, str) else list(days)
    ds = [dev] if dev != "both" else ["c3-a", "c3-b"]
    sel_e = [e for e in events if e["device"] in ds and e["day"] in days]
    sel_c = [c for c in clips if c["device"] in ds and c["day"] in days and n in c["scored"]]
    a_all = [e for e in sel_e if counted(e, n)]
    b_all = [c for c in sel_c if c["cls"] == "false" and n in c["fires"]]
    h = sum(hours[(d, x)][0] for d in ds for x in days)
    eh = sum(hours[(d, x)][1] for d in ds for x in days)
    ae = sum(e["evening"] for e in a_all)
    be = sum(c["evening"] for c in b_all)
    ev_n = eh / 6.0
    k_ev = ae + be
    lo, hi = poisson_ci(k_ev, ev_n) if ev_n > 0 else (math.nan, math.nan)
    by = Counter(e["cls"] for e in a_all)
    return {"model": n, "device": dev, "days": days, "a": len(a_all), "a_evening": ae, "a_false_labelled": by["false"],
            "a_unclear": by["unclear"], "a_unlabelled": by["unlabelled"],
            "attempts_excluded": sum(1 for e in sel_e if n in e["models"] and e["cls"] == "attempt"),
            "b": len(b_all), "b_evening": be, "b_by_source": dict(Counter(c["source"] for c in b_all)), "total": len(a_all) + len(b_all), "total_evening": k_ev,
            "hours": round(h, 2), "evenings": round(ev_n, 2),
            "per_evening": round(k_ev / ev_n, 2) if ev_n > 0 else None,
            "ci95_per_evening": [round(lo, 2), round(hi, 2)] if ev_n > 0 else None,
            "notawake_fired": sum(1 for c in sel_c if c["cls"] == "not-a-wake" and n in c["fires"]),
            "unclear_live_fired": sum(1 for c in sel_c if c["cls"] == "unclear" and n in c["fires"]),
            "unlabelled_live_fired": sum(1 for c in sel_c if c["cls"] == "unlabelled" and n in c["fires"])}


def paired(cand, ref, days, events, clips, evening_only, device=None):
    """Discordant false-wake units between two models on the same audio: room events (merged across models) and live false
    clips, optionally of one TV. Exact two-sided sign test on the discordant units (and the one-sided p of "the candidate
    has fewer"); Clopper-Pearson interval of the candidate's share of them."""
    units = []
    for e in events:
        if e["day"] in days and e["cls"] != "attempt" and (e["evening"] or not evening_only) and device in (None, e["device"]):
            units.append((cand in e["models"], ref in e["models"]))
    for c in clips:
        if c["day"] in days and c["cls"] == "false" and cand in c["scored"] and ref in c["scored"] and \
                (c["evening"] or not evening_only) and device in (None, c["device"]):
            units.append((cand in c["fires"], ref in c["fires"]))
    only_c = sum(1 for x, y in units if x and not y)
    only_r = sum(1 for x, y in units if y and not x)
    both = sum(1 for x, y in units if x and y)
    from scipy.stats import binomtest
    n = only_c + only_r
    ci = binomtest(only_c, n).proportion_ci() if n else None
    return {"cand": cand, "ref": ref, "both": both, "only_cand": only_c, "only_ref": only_r,
            "cand_total": both + only_c, "ref_total": both + only_r, "p_two_sided": round(binom_two_sided(only_c, n), 4),
            "p_one_sided_fewer": round(float(binomtest(only_c, n, 0.5, alternative="less").pvalue), 4) if n else 1.0,
            "cand_share_ci95": [round(ci.low, 3), round(ci.high, 3)] if ci else None}


def recall(names, days, clips, windows, events):
    out = {}
    tr = [c for c in clips if c["day"] in days and c["cls"] == "true"]
    att = []
    for w in windows:                                    # unique attempts (same TV within MATCH_S = one)
        if w["label"] != "attempt" or madrid(w["t"])[0] not in days:
            continue
        if not any(x["device"] == w["device"] and abs(x["t"] - w["t"]) <= MATCH_S for x in att):
            att.append(w)
    units = [{n: any(n in e["models"] and e["device"] == w["device"] and abs(e["t"] - w["t"]) <= MATCH_S for e in events)
              for n in names} for w in att] + \
        [{n: n in c["recall"] for n in names} for c in tr if all(n in c["scored"] for n in names)]
    from scipy.stats import binomtest
    for cand in names[1:]:
        a = sum(u[cand] and not u[names[0]] for u in units)
        b = sum(u[names[0]] and not u[cand] for u in units)
        out[f"{cand}-vs-{names[0]}"] = {"calls": len(units), "only_cand": a, "only_ref": b,
                                        "p_two_sided": round(float(binomtest(a, a + b).pvalue), 4) if a + b else 1.0}
    for n in names:
        sel = [c for c in tr if n in c["scored"]]
        k = sum(1 for c in sel if n in c["recall"])
        hits = sum(1 for w in att if any(n in e["models"] and e["device"] == w["device"] and abs(e["t"] - w["t"]) <= MATCH_S
                                         for e in events))
        out[n] = {"live_true": [k, len(sel)], "live_true_by_trigger": {
            tg: [sum(1 for c in sel if c["trigger"] == tg and n in c["recall"]), sum(1 for c in sel if c["trigger"] == tg)]
            for tg in sorted({c["trigger"] for c in sel})}, "attempts": [hits, len(att)],
            "all_calls": [k + hits, len(sel) + len(att)]}     # live true clips favour the live model, attempts the others
    return out


def write_unlabelled(events):
    """Private list (times and models only) of the true-grid room events with no labelled window: to cut and label."""
    path = OUT / "unlabelled_events.csv"
    rows = [{"device": e["device"], "utc": L.dir_stamp(e["t"]), "madrid_day": e["day"], "role": day_role(e["day"]),
             "evening": int(e["evening"]), "models": "+".join(sorted(e["models"]))}
            for e in sorted(events, key=lambda e: e["t"]) if e["cls"] in ("unlabelled", "unclear")]
    with open(path, "w", newline="") as f:
        w = csv.DictWriter(f, ["device", "utc", "madrid_day", "role", "evening", "models"])
        w.writeheader()
        w.writerows(rows)
    os.chmod(path, 0o600)


def reserved_live(clips):
    """The live model's own clips on the reserved days (live-model data only)."""
    out = Counter()
    for c in clips:
        if c["day"] in RESERVED:
            n = next(iter(c["scored"]), "?")
            out[f"{c['day']}|{c['device']}|{n}|{c['cls']}|{'fires' if n in c['fires'] else 'no-cross'}"] += 1
    return dict(out)


def bracket_summary():
    p = OUT / "bracket.json"
    if not p.exists():
        return None
    rows = json.loads(p.read_text())
    out = {}
    for n in MODELS:
        by = defaultdict(list)
        for r in rows:
            by[(r["clip"], r["in_bracket"])].append(r[f"{n}_dets"])
        out[n] = {"pieces": len({r["clip"] for r in rows}), "hours": round(sum({r["clip"]: r["hours"] for r in rows}.values()), 3),
                  "dets_in_bracket_min_max": [sum(min(v) for (c, ib), v in by.items() if ib), sum(max(v) for (c, ib), v in by.items() if ib)],
                  "dets_8_phases_min_max": [sum(min(v) for (c, ib), v in by.items()), sum(max(v) for (c, ib), v in by.items())]}
    return out


def label_state(live, windows):
    lc = Counter(live_class(r) for r in live.values())
    wc = Counter((w["table"], w["label"] or "unlabelled") for w in windows)
    return {"live": dict(lc), "windows_unlabelled": {t: wc[(t, "unlabelled")] for t in WINDOW_TABLES},
            "windows_unclear": {t: wc[(t, "unclear")] for t in WINDOW_TABLES}}


def gate_block(res, g):
    """The pre-registered v6.3 gate (kivi-models docs/plan/2026-11-false-wake-plan.md section 9): primary = the candidate's
    evening false wakes on the gate TV against the reference, paired (one-sided exact sign test, alpha 0.05); recall = all
    known calls of the test days, the candidate detects no fewer than the reference (paired counts)."""
    cand, ref, dev = g["cand"], g["ref"], g["device"]
    pk = f"{cand}-vs-{ref}|test|evening|{dev}"
    pr = res["paired"].get(pk, {})
    c_ev = res["pooled"].get(f"{cand}|test|{dev}", {})
    r_ev = res["pooled"].get(f"{ref}|test|{dev}", {})
    rc = res["recall"].get("test", {})
    calls_c, calls_r = rc.get(cand, {}).get("all_calls", [0, 0]), rc.get(ref, {}).get("all_calls", [0, 0])
    both_tv_c = res["pooled"].get(f"{cand}|test|both", {}).get("total", 0)
    both_tv_r = res["pooled"].get(f"{ref}|test|both", {}).get("total", 0)
    out = {"cand": cand, "ref": ref, "device": dev, "evenings": c_ev.get("evenings"),
           "cand_evening": c_ev.get("total_evening"), "ref_evening": r_ev.get("total_evening"),
           "cand_unlabelled_a": c_ev.get("a_unlabelled"), "paired": pr,
           "primary_shown": bool(pr) and pr.get("p_one_sided_fewer", 1.0) < 0.05,
           "not_worse": bool(pr) and (c_ev.get("total_evening", 0) <= r_ev.get("total_evening", 0)),
           "calls": {cand: calls_c, ref: calls_r}, "recall_ok": calls_c[0] >= calls_r[0],
           "total_day_both_tv": {cand: both_tv_c, ref: both_tv_r}, "total_ok": both_tv_c <= both_tv_r}
    return out


def cmd_cut(a):
    """Blind 6 s windows of the (a) events no labelled window covers, appended to the owner's queue table."""
    import kivi_collect as KC
    import kivi_replay_mine as K
    names = list(MODELS)
    room = [json.loads((OUT / f"room_{tag}.json").read_text()) for tag, _, _ in ROOM_RUNS]
    windows = read_windows()
    pieces = {}
    for res in room:
        for pc in res["pieces_detail"]:
            pieces[(pc["device"], pc["clip"])] = pc
    rows, misses = [], ROOT / "live-misses"
    for res in room:
        for pc in res["pieces_detail"]:
            ts = sorted({x for n, xs in pc.get("times", {}).items() if n in names for x in xs})
            last = None
            for x in ts:
                t = pc["start"] + x
                if (last is not None and t - last <= MERGE_S) or madrid(t)[0] not in DAYS:
                    last = t
                    continue
                last = t
                if window_class(windows, pc["device"], t)[0] != "unlabelled":
                    continue
                path = ROOT / pc["clip"]
                n = K.piece_samples(path)
                first, lst = K.window_bounds(t, pc["start"], n)
                utc = L.dir_stamp(t)
                rel = f"gate/{OUT.name}/{pc['device']}/{utc}/window.wav"
                if not (misses / rel).exists():
                    C.private_mkdir((misses / rel).parent, ROOT)
                    rms, pkd = K.cut_window(path, first, lst, misses / rel)
                else:
                    rms, pkd = K.wav_levels(np.fromfile(misses / rel, dtype="<i2", offset=44))
                rows.append({k: "" for k in L.MISS_FIELDS} | {"id": f"g-{OUT.name}-{len(rows) + 1:03d}", "device": pc["device"],
                             "part": f"gate-{OUT.name}", "utc": utc, "rank": "0", "level_dbfs": rms, "peak_dbfs": pkd, "clip": rel})
    added = KC.append_window_rows(ROOT / "live-misses" / CUT_TABLE, rows)
    print(f"{len(rows)} unlabelled (a) events, {added} new windows appended to {CUT_TABLE} (label them with the queue)")


def render(res):
    f = lambda c: (f"{c['per_evening']:.2f} ({c['ci95_per_evening'][0]:.2f}-{c['ci95_per_evening'][1]:.2f})"   # noqa: E731
                   if c["per_evening"] is not None else "n/a")
    out = ["# Fair false-wake comparison on the server grid (counts only)", "",
           f"Generated {res['generated_utc']} by `kivi_fair_fa.py report`. Models at raw "
           + ", ".join(f"{n} {t}" for n, t in res["models"].items()) + ". Odd days are TEST (nothing chosen from them); "
           "even days are info. (a) room detections at the server grid minus owner-labelled attempts; (b) live clips labelled "
           "false on which the model crosses (tail + request). Per evening = evening (a + b) / (room evening hours / 6), "
           "exact Poisson 95 % interval.", "",
           "Live model (its own detections are cut out of the room audio, so its (a) is empty by construction and its false "
           "wakes are all in (b)): v5 `oye_kivi_next` on both TVs from 2026-11-08 13:28Z (room copy from 13:30Z) to 2026-11-11 "
           "22:11Z; v6 `oye_kivi_ctx` 22:11Z to about 22:40Z; v5 again until 2026-11-12 07:40:55Z; v6.2 `oye_kivi_ctx2` since "
           "(2026-11-12 is reserved: only v6.2's own clips are reported).", "",
           "| model | TV | day | role | (a) | (a) evening | (b) | (b) evening | total | evening total | room h / evenings | per evening (95 % CI) | (a): labelled false / unclear / unlabelled | attempts excluded |",
           "| --- | --- | --- | --- | --- | --- | --- | --- | --- | --- | --- | --- | --- | --- |"]
    for c in res["cells"]:
        out.append(f"| {c['model']} | {c['device']} | {c['days'][0]} | {day_role(c['days'][0])} | {c['a']} | {c['a_evening']} | "
                   f"{c['b']} | {c['b_evening']} | {c['total']} | {c['total_evening']} | {c['hours']} / {c['evenings']} | {f(c)} | "
                   f"{c['a_false_labelled']} / {c['a_unclear']} / {c['a_unlabelled']} | {c['attempts_excluded']} |")
    out += ["", "## Pooled", "", "| model | role | TV | (a) | (b) | total | evening total | evenings | per evening (95 % CI) | (a) unlabelled | (b) by label source | live not-a-wake / unclear / unlabelled fired |",
            "| --- | --- | --- | --- | --- | --- | --- | --- | --- | --- | --- | --- |"]
    for k, c in res["pooled"].items():
        n, role, dev = k.split("|")
        out.append(f"| {n} | {role} | {dev} | {c['a']} | {c['b']} | {c['total']} | {c['total_evening']} | {c['evenings']} | {f(c)} | "
                   f"{c['a_unlabelled']} | " + (", ".join(f"{k} {v}" for k, v in sorted(c["b_by_source"].items())) or "-")
                   + f" | {c['notawake_fired']} / {c['unclear_live_fired']} / {c['unlabelled_live_fired']} |")
    out += ["", f"## Paired comparison with {REF} (same audio; discordant units, exact sign test)", "",
            f"| comparison | role | scope | TV | both | only candidate | only {REF} | candidate total | {REF} total | p (two-sided) | p (one-sided, fewer) | candidate share of discordant (95 % CI) |",
            "| --- | --- | --- | --- | --- | --- | --- | --- | --- | --- | --- | --- |"]
    for k, p in res["paired"].items():
        cmp_, role, sc, *dv = k.split("|")
        out.append(f"| {cmp_} | {role} | {sc} | {dv[0] if dv else 'both'} | {p['both']} | {p['only_cand']} | {p['only_ref']} | "
                   f"{p['cand_total']} | {p['ref_total']} | {p['p_two_sided']} | {p.get('p_one_sided_fewer', '')} | {p['cand_share_ci95']} |")
    if res.get("gate"):
        g = res["gate"]
        out += ["", "## Gate (pre-registered; kivi-models plan section 9)", "",
                f"- primary, {g['device']} evening false wakes on the test days ({g['evenings']} evening-equivalents): "
                f"{g['cand']} {g['cand_evening']} against {g['ref']} {g['ref_evening']} ({g['cand_unlabelled_a']} unlabelled (a) "
                f"events counted as false wakes); paired one-sided p = {g['paired'].get('p_one_sided_fewer')}: "
                f"**improvement {'shown' if g['primary_shown'] else 'not shown'}**; not worse on the point count: {g['not_worse']}",
                f"- recall, all known calls of the test days: {g['cand']} {g['calls'][g['cand']][0]}/{g['calls'][g['cand']][1]}, "
                f"{g['ref']} {g['calls'][g['ref']][0]}/{g['calls'][g['ref']][1]}: {'ok' if g['recall_ok'] else '**FAIL**'}",
                f"- whole-day false wakes, both TVs: {g['total_day_both_tv']}: {'ok' if g['total_ok'] else '**FAIL**'}"]
    out += ["", "## Recall", "", "Live true clips are the calls the live model (v5) caught; owner-labelled attempts are calls it "
            "missed (found by replays). Each favours one side; all known calls is their sum.", "",
            "| role | model | live true clips detected | by trigger | owner-labelled attempts detected (room, true grid) | all known calls |",
            "| --- | --- | --- | --- | --- | --- |"]
    for role, rr in res["recall"].items():
        for n, r in rr.items():
            if "-vs-" in n:
                continue
            out.append(f"| {role} | {n} | {r['live_true'][0]}/{r['live_true'][1]} | "
                       + ", ".join(f"{t or 'empty'} {a}/{b}" for t, (a, b) in r["live_true_by_trigger"].items())
                       + f" | {r['attempts'][0]}/{r['attempts'][1]} | {r['all_calls'][0]}/{r['all_calls'][1]} |")
    out += ["", "Paired on all known calls (sign test on the calls only one model detects; the attempts were found by "
            "replays of the candidates, so this favours them): "
            + "; ".join(f"{role} {k}: {v['only_cand']} only candidate, {v['only_ref']} only v5 of {v['calls']}, p {v['p_two_sided']}"
                        for role, rr in res["recall"].items() for k, v in rr.items() if "-vs-" in k)]
    out += ["", "## Live-model data on the reserved days (the live model only; no candidate scored)", ""]
    out += [f"- {k}: {v}" for k, v in sorted(res["reserved_live"].items())] or ["- none"]
    out += ["", "## Bracketed pieces", "", f"`{json.dumps(res['bracket'])}`", "", "## Label state at the recount", "",
            f"`{json.dumps(res['label_state'])}`", ""]
    return "\n".join(out)


def main(argv=None):
    ap = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    ap.add_argument("what", choices=["room", "bracket", "live", "report", "cut"])
    ap.add_argument("--config", help="JSON settings (module doc); default: the 2026-11-12 recount")
    ap.add_argument("--jobs", type=int, default=6)
    ap.add_argument("--redo", action="store_true")
    a = ap.parse_args(argv)
    if a.config:
        apply_config(a.config)
    if not OUT.exists():
        C.private_mkdir(OUT, KH)
    {"room": cmd_room, "bracket": cmd_bracket, "live": cmd_live, "report": cmd_report, "cut": cmd_cut}[a.what](a)


if __name__ == "__main__":
    main()
