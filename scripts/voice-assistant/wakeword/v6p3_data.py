#!/usr/bin/env python3
"""Kivi v6.3 data (pre-registered in kivi-models docs/plan/2026-11-false-wake-plan.md section 9). Even days only:
2026-11-06 and -08 room audio (plus 2026-11-05, grandfathered) for training, 2026-11-10 room audio held out for the threshold;
live clips and labelled windows of 2026-11-08 and -10. Nothing of 2026-11-12 or of any odd day is read. Counts only.

    v6p3_data.py exclude   # room/hard-negative exclusions: ASR phrase flags (data quality), owner-labelled attempts (+-3 s,
                           # every window table), the four v6.2 amendment-5 attempt events (+-2 s) -> v6p3/room_exclude.json
    v6p3_data.py hard      # hard negatives mined on the training room days by v6 and v6.2 (raw >= 0.5) and v5 (>= 0.3),
                           # +-1 step; dropped only by the exclusions above. The v6.1 retry rule (a live wake, an stt-only
                           # run or another model event within 30 s, a miss candidate within 6 s) is NOT applied: the
                           # owner's blind test showed it is no attempt signal. Its would-be drops are counted as restored.
    v6p3_data.py live      # cut table (v6_augment.py ctx) of the even-day live clips and labelled windows

Live clips (label sources owner, owner-button, trusted auto-v1, older no-source; Madrid dates 2026-11-08 and -10):
- true, trigger wake: positives at the exact-replay detection step (phrase end) + U(-0.2, +0.15) s, `--copies` per clip,
  group pos_<speaker>; before-Kivi and command windows of the same clip -> negpart (as v6_cuts.py live);
- true, phrase in the request audio (button run): as v6_cuts.py (v5 peak near the word end);
- false, trigger wake: hard (detection step + jitter, and uniform over the tail), and `conv` = uniform cuts over the
  request audio of the same run (the conversation that went on after the false wake);
- false, trigger button / unknown: tail and request audio uniform cuts only -> conv (no phrase was detected there);
Windows (6 s, centred on a replay peak) on even days: labelled other-speech / tv-media / noise -> winneg (uniform cuts);
labelled attempt -> pos_att (cut at the centre + U(-0.2, +0.15) s).
"""
import argparse
import csv
import json
import os
import sys
from collections import Counter, defaultdict
from datetime import datetime, timezone
from pathlib import Path
from zoneinfo import ZoneInfo

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

import numpy as np  # noqa: E402

import kivi_real_common as C  # noqa: E402

TZ = ZoneInfo("Europe/Madrid")
RATE = 16000
KH = C.kivi_home()
ROOT = KH / "datasets" / "real"
OUT = KH / "v6p3"
TRAIN_LIVE_DAYS = ("2026-11-08", "2026-11-10")
TRUSTED = ("owner", "owner-button", "auto-v1", "")
WINDOW_TABLES = ("daily_false_wakes.csv", "daily_miss_candidates.csv", "c3_events.csv", "c3_events_r2.csv",
                 "miss_candidates.csv")
FALSE_WIN = ("other-speech", "tv-media", "noise")
AMEND5_ATTEMPTS = (("c3-a", "20261108T204116.000Z"), ("c3-b", "20261108T192906.950Z"), ("c3-b", "20261106T122550.000Z"),
                   ("c3-b", "20261108T223311.000Z"))
HEADS = {"v5": ("releases/v5-oye-kivi-2026-11-08/model/oye_kivi_next_uncalibrated.onnx", 0.3),
         "v6": ("releases/v6-oye-kivi-2026-11-11/model/oye_kivi_ctx_uncalibrated.onnx", 0.5),
         "v6.2": ("releases/v6-p2-oye-kivi-2026-11-12/model/oye_kivi_ctx2_uncalibrated.onnx", 0.5)}


def S(sec):
    return int(round(sec * RATE))


def utc_epoch(stamp):
    return datetime.strptime(stamp, "%Y%m%dT%H%M%S.%fZ").replace(tzinfo=timezone.utc).timestamp()


def madrid_day(t):
    return datetime.fromtimestamp(t, TZ).date().isoformat()


def windows_rows():
    out = []
    for name in WINDOW_TABLES:
        p = ROOT / "live-misses" / name
        if p.exists():
            for r in csv.DictReader(open(p, encoding="utf-8")):
                out.append({**r, "table": name, "t": utc_epoch(r["utc"])})
    return out


def save_private(path, text):
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(text)
    os.chmod(path, 0o600)


# ------------------------------------------------------------------------------------------------
# exclusions
# ------------------------------------------------------------------------------------------------


def cmd_exclude(a):
    iv = defaultdict(list)
    why = Counter()
    for name, pad in (("roomneg_phrase_flags.csv", 2.0), ("hardneg_phrase_flags.csv", 1.5)):
        for r in csv.DictReader(open(KH / "quality" / name, encoding="utf-8")):
            f, conf = r["flag"], float(r.get("confidence") or 0)
            room = r["event_id"].startswith("rn-")
            if f in ("phrase", "kivi_word") or (f == "hotword_only" and (room or conf >= 0.5)):
                t = utc_epoch(r["event_id"].split("-", 3)[3])
                iv[r["device"].replace("tv-c3", "c3-")].append([t - pad, t + pad])
                why[f"asr-{name.split('_')[0]}"] += 1
    for w in windows_rows():
        if w["label"] == "attempt":
            iv[w["device"]].append([w["t"] - 3.0, w["t"] + 3.0])
            why["owner-attempt"] += 1
    for dev, stamp in AMEND5_ATTEMPTS:
        t = utc_epoch(stamp)
        iv[dev].append([t - 2.0, t + 2.0])
        why["amendment-5"] += 1
    save_private(OUT / "room_exclude.json", json.dumps(dict(iv)))
    print(json.dumps({"intervals": {d: len(v) for d, v in iv.items()}, "why": why}))


def excluded(iv, dev, t):
    return any(a <= t <= b for a, b in iv.get(dev, ()))


# ------------------------------------------------------------------------------------------------
# hard negatives
# ------------------------------------------------------------------------------------------------


def cmd_hard(a):
    import v6_room as R
    import v6p1_filter_hard as FH
    import kivi_replay_mine as K
    iv = json.loads((OUT / "room_exclude.json").read_text())
    idx = json.loads((KH / a.index).read_text())["pieces"]
    parts = set(a.parts.split(","))
    pieces = [p for p in idx if p["part"] in parts]
    days = Counter(R.local_date(p["start_utc"]) for p in pieces)
    assert not any(int(d[-2:]) % 2 and d >= "2026-11-08" for d in days), days     # odd days after the rule: never
    assert "2026-11-10" not in days and "2026-11-12" not in days, days               # selection day / not used
    heads = {n: R.Head(KH / f) for n, (f, _) in HEADS.items()}
    # the retry rule's inputs, only to COUNT what it would have dropped (restored here)
    live = K.live_detection_times(ROOT)
    stt = json.loads((KH / "v6p1/stt_only_times.json").read_text())
    cands = defaultdict(list)
    for r in csv.DictReader(open(ROOT / "live-misses/miss_candidates.csv", encoding="utf-8")):
        cands[r["device"]].append(utc_epoch(r["utc"]))
    kept = {"v6x_day": [], "v6x_evening": [], "v5": []}
    summ = defaultdict(lambda: {"events": 0, "windows": 0, "excluded_events": 0, "restored_events": 0})
    per_piece, others = [], defaultdict(list)
    os.chdir(KH)
    for pc in pieces:
        e = np.load(pc["emb"], mmap_mode="r")
        t0 = R._utc(pc["start_utc"]).timestamp()
        probs = {}
        for n, h in heads.items():
            steps, p = R.piece_probs(h, e)
            probs[n] = (steps, p)
            times = R.step_time(steps)
            for i in R.detections(times, p, 0.5):
                others[pc["device"]].append(t0 + float(times[i]))
        per_piece.append((pc, e, t0, probs))
    for pc, e, t0, probs in per_piece:
        v = R.window_view(e)
        groups = (("v6x", ("v6", "v6.2")), ("v5", ("v5",)))
        for gname, members in groups:
            steps = probs[members[0]][0]
            p = np.max(np.stack([probs[m][1] for m in members]), axis=0)
            floor = HEADS[members[0]][1]
            times = R.step_time(steps)
            ev_idx = R.detections(times, p, floor)
            if not ev_idx:
                continue
            ev_t = np.array([times[i] for i in ev_idx])
            hit = np.flatnonzero(p > floor)
            sel = sorted({i + d for i in hit for d in (-1, 0, 1) if 0 <= i + d < len(p)})
            owner = {i: int(np.argmin(np.abs(ev_t - times[i]))) for i in sel}
            evinfo = []
            for ti in ev_t:
                tab = t0 + float(ti)
                evening, day = R.is_evening(pc["start_utc"], float(ti))
                key = f"{gname}|{pc['device']}|{day}|{'evening' if evening else 'day'}"
                ex = excluded(iv, pc["device"], tab)
                retry = FH.flags_for(tab, pc["device"], live, stt,
                                     {pc["device"]: [x for x in others[pc["device"]] if abs(x - tab) > 2.0]}, cands)
                summ[key]["events"] += 1
                summ[key]["excluded_events"] += int(ex)
                summ[key]["restored_events"] += int(bool(retry) and not ex)
                evinfo.append((ex, evening, key))
            for i in sel:
                ex, evening, key = evinfo[owner[i]]
                if ex:
                    continue
                summ[key]["windows"] += 1
                w = np.asarray(v[steps[i] - 15], np.float16)[None]
                kept["v5" if gname == "v5" else ("v6x_evening" if evening else "v6x_day")].append(w)
    fd = OUT / "feat"
    fd.mkdir(parents=True, exist_ok=True)
    os.chmod(fd, 0o700)
    for name, parts_ in kept.items():
        X = np.concatenate(parts_) if parts_ else np.zeros((0, 16, 96), np.float16)
        np.save(fd / f"hard_{name}.npy", X)
        os.chmod(fd / f"hard_{name}.npy", 0o600)
        print(fd / f"hard_{name}.npy", X.shape, flush=True)
    save_private(OUT / "hard_summary.json", json.dumps(dict(sorted(summ.items())), indent=1))
    tot = Counter()
    for k, c in summ.items():
        for kk, vv in c.items():
            tot[f"{k.split('|')[0]}|{kk}"] += vv
    print(json.dumps(tot, indent=1))


# ------------------------------------------------------------------------------------------------
# live clips and windows -> cut table
# ------------------------------------------------------------------------------------------------


def cmd_live(a):
    import kivi_live_wakes as L
    from eval_stream import load_models
    import v6_cuts as VC
    rng = np.random.default_rng(a.seed)
    scorer = load_models([f"v5={KH / HEADS['v5'][0].replace('.onnx', '.tflite')}"])
    out, summ = [], {"clips": Counter(), "windows": Counter(), "speakers_pos": Counter(), "dropped": Counter()}
    for r in L.read_table(ROOT / "live" / "live_wakes.csv"):
        t = L.detection_epoch(r)
        if madrid_day(t) not in TRAIN_LIVE_DAYS:
            continue
        if r["label_source"] not in TRUSTED or r["label"] not in ("true", "false"):
            summ["dropped"][f"label {r['label'] or 'empty'} / {r['label_source'] or 'no source'}"] += 1
            continue
        cdir = ROOT / "live" / r["device"] / r["utc"]
        tail_p, stt_p = cdir / "wake_tail.wav", cdir / "stt.wav"
        if not tail_p.exists():
            summ["dropped"]["no tail"] += 1
            continue
        same = not r.get("stt_run")
        tail = C.read_pcm16(tail_p)
        n_stt = len(C.read_pcm16(stt_p)) if stt_p.exists() else 0
        audio = [str(tail_p)] + ([str(stt_p)] if same and n_stt else [])
        n_audio = len(tail) + (n_stt if same else 0)
        label, trig = r["label"], r["trigger"]
        spk = r["speaker"] if r["speaker"] in ("spk01", "spk02") else "unk"
        cuts = []
        if label == "true":
            if trig == "wake" and r.get("peak_t_s"):
                pe = S(float(r["peak_t_s"]))
            elif r.get("phrase_in_stt") == "true" and stt_p.exists() and r.get("stt_phrase_end_s"):
                stt = C.read_pcm16(stt_p)
                tt, pk = VC.stt_peak(stt, float(r["stt_phrase_end_s"]), scorer)
                if tt is None or pk < 0.2:
                    summ["dropped"]["true, phrase in stt, v5 peak low"] += 1
                    continue
                audio, n_audio, pe = [str(stt_p)], len(stt), S(tt)
            else:
                summ["dropped"][f"true / trigger {trig or 'empty'} without a phrase end"] += 1
                continue
            for _ in range(a.copies):
                c = pe + S(rng.uniform(-0.2, 0.15))
                if c <= n_audio:
                    cuts.append([c, f"pos_{spk}", 1])
            for _ in range(a.neg_copies):
                c = pe - S(rng.uniform(0.6, 1.0))
                if c > S(0.3):
                    cuts.append([c, "negpart", 1])
                rest = n_audio - (pe + S(1.6))
                if len(audio) == 2 and rest > S(0.3):
                    cuts.append([pe + S(1.6) + int(rng.integers(0, rest)), "negpart", 1])
            summ["speakers_pos"][f"{r['device']}|{spk}"] += 1
            kind = "true"
        else:
            if trig == "wake" and r.get("peak_t_s"):
                pe = S(float(r["peak_t_s"]))
                for _ in range(a.copies):
                    c = pe + S(rng.uniform(-0.2, 0.15))
                    if c <= n_audio:
                        cuts.append([c, "hard", 1])
                for _ in range(a.neg_copies):
                    cuts.append([int(rng.integers(S(0.5), len(tail) + 1)), "hard", 1])
                kind = "false-wake"
            else:
                for _ in range(a.neg_copies):
                    cuts.append([int(rng.integers(S(0.5), len(tail) + 1)), "conv", 1])
                kind = f"false-{trig or 'empty'}"
            if len(audio) == 2:
                for _ in range(a.conv_copies):
                    cuts.append([len(tail) + int(rng.integers(S(1.0), max(S(1.0) + 1, n_stt))), "conv", 1])
        summ["clips"][f"{r['device']}|{kind}|{r['label_source'] or 'no-source'}"] += 1
        out.append({"id": f"{r['device']}/{r['utc']}", "audio": audio, "tv": True, "cuts": cuts})
    seen = []                       # the same event can sit in two tables (copied labels): one window per TV within 3 s
    for w in windows_rows():
        if madrid_day(w["t"]) not in TRAIN_LIVE_DAYS or not w.get("clip"):
            continue
        if w["label"] not in FALSE_WIN + ("attempt",):
            continue
        if any(d == w["device"] and abs(t - w["t"]) <= 3.0 for d, t in seen):
            summ["dropped"]["window: same event in another table"] += 1
            continue
        seen.append((w["device"], w["t"]))
        p = ROOT / "live-misses" / w["clip"]
        if not p.exists():
            continue
        n = len(C.read_pcm16(p))
        cuts = []
        if w["label"] in FALSE_WIN:
            for _ in range(a.win_copies):
                cuts.append([int(rng.integers(S(1.3), n + 1)), "winneg", 1])
            summ["windows"][f"{w['device']}|{w['label']}"] += 1
        elif w["label"] == "attempt":
            for _ in range(a.copies):
                cuts.append([n // 2 + S(rng.uniform(-0.2, 0.15)), "pos_att", 1])
            summ["windows"][f"{w['device']}|attempt|{w.get('speaker') or 'unknown'}"] += 1
        else:
            continue
        out.append({"id": f"win:{w['table']}:{w['id']}", "audio": [str(p)], "tv": True, "cuts": cuts})
    (OUT / "cuts").mkdir(parents=True, exist_ok=True)
    os.chmod(OUT / "cuts", 0o700)
    with open(OUT / "cuts" / "live.jsonl", "w", encoding="utf-8") as fo:
        for o in out:
            fo.write(json.dumps(o) + "\n")
    os.chmod(OUT / "cuts" / "live.jsonl", 0o600)
    win = Counter()
    for o in out:
        for _, g, k in o["cuts"]:
            win[g] += k
    summ["windows_per_group"] = win
    save_private(OUT / "cuts" / "live_summary.json", json.dumps(summ, indent=1))
    print(json.dumps(summ, indent=1))


def main():
    ap = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    ap.add_argument("what", choices=["exclude", "hard", "live"])
    ap.add_argument("--index", default="v6/room_emb/index_v6p2.json")
    ap.add_argument("--parts", default="old-even,even-trn")
    ap.add_argument("--copies", type=int, default=150)
    ap.add_argument("--neg-copies", type=int, default=30)
    ap.add_argument("--conv-copies", type=int, default=30)
    ap.add_argument("--win-copies", type=int, default=60)
    ap.add_argument("--seed", type=int, default=6303)
    a = ap.parse_args()
    {"exclude": cmd_exclude, "hard": cmd_hard, "live": cmd_live}[a.what](a)


if __name__ == "__main__":
    main()
