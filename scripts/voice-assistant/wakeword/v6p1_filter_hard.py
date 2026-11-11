#!/usr/bin/env python3
"""Kivi v6.1 stage 1 amendment (2026-11-11): drop mined hard negatives that may be real "oye Kivi" attempts.

The v6 (raw >= 0.5) and v5 (raw >= 0.3) hard negatives of the even-day room audio may contain calls that v5 missed live
(the owner reports frequent misses); training them as negatives would teach the model to ignore real calls. A mined
event is FLAGGED and its windows DROPPED (never relabelled as positives) when, on the same TV, any of these lies near it:
- a live wake detection (end of the wake file, `live_wakes.csv`) within 30 s before or after;
- an stt-only run (mic button / push to talk / continued conversation; listing of the HA recordings, read only) starting
  within 30 s before or after;
- another event of v4, v5 or v6 (raw >= 0.5, 2 s refractory) 5 to 30 s before or after;
- a missed-wake candidate (`live-misses/miss_candidates.csv`, 6 s windows) within 6 s;
- (v6.2, `--phrase-flags`) a data-quality ASR phrase flag of the event (`hardneg_phrase_flags.csv`, matched by TV and
  time within 1.5 s): flag `phrase` or `kivi_word`, or `hotword_only` with confidence >= 0.5.
Windows are the hit steps +-1 step, each assigned to its event. Output: the kept windows under the names the training
script reads (the unfiltered arrays are kept with suffix `_unfiltered`) and counts per model, day and evening.

    v6p1_filter_hard.py --index v6/room_emb/index_v6p1.json --parts even-v5era,old-even --out-dir v6p1/feat
"""
import argparse
import csv
import json
import os
import sys
from datetime import datetime, timezone
from pathlib import Path

import numpy as np

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import v6_room as R  # noqa: E402

WORK = Path(os.environ.get("WORK", "/work/workspaces/kivi-wakeword"))
HEADS = {"v4": WORK / "v4/final/oye_kivi_uncalibrated.onnx",
         "v5": WORK / "releases/v5-oye-kivi-2026-11-08/model/oye_kivi_next_uncalibrated.onnx",
         "v6": WORK / "v6/runs/v6m/kivi.onnx"}
MINED = {"v6": 0.5, "v5": 0.3}          # the stage-1 mining floors
EVENT_FLOOR = 0.5                       # "another v4/v5/v6 event"
RETRY_S = 30.0
OTHER_MIN_S = 5.0
CAND_S = 6.0


def utc_epoch(s):
    return datetime.strptime(s, "%Y%m%dT%H%M%S.%fZ").replace(tzinfo=timezone.utc).timestamp()


def phrase_flagged(paths):
    """{device: [epoch]} of the events the data-quality ASR flags as holding the phrase (v6.2 rule). Hard-negative flags
    (`hn-` ids): `phrase`, `kivi_word`, or `hotword_only` with confidence >= 0.5; room-negative flags (`rn-` ids, 1c): every
    flag other than `none` (the conservative choice of the data-quality report)."""
    out = {}
    for path in paths.split(","):
        for r in csv.DictReader(open(path, encoding="utf-8")):
            f = r["flag"]
            conf = float(r.get("confidence") or 0)
            rn = r["event_id"].startswith("rn-")
            if f in ("phrase", "kivi_word") or (f == "hotword_only" and (rn or conf >= 0.5)):
                dev = r["device"].replace("tv-c3", "c3-")
                out.setdefault(dev, []).append(utc_epoch(r["event_id"].split("-", 3)[3]))
    return out


def flags_for(t, dev, live, stt, others, cands, phrase=None):
    out = []
    if phrase and any(abs(x - t) <= 1.5 for x in phrase.get(dev, ())):
        out.append("asr-phrase")
    if any(abs(x - t) <= RETRY_S for x in live.get(dev, ())):
        out.append("live")
    if any(abs(x - t) <= RETRY_S for x in stt.get(dev, ())):
        out.append("stt-only")
    if any(OTHER_MIN_S <= abs(x - t) <= RETRY_S for x in others.get(dev, ())):
        out.append("other-event")
    if any(abs(x - t) <= CAND_S for x in cands.get(dev, ())):
        out.append("miss-candidate")
    return out


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--index", required=True)
    ap.add_argument("--parts", default="even-v5era,old-even")
    ap.add_argument("--dataset-root", default=str(WORK / "datasets/real"))
    ap.add_argument("--out-dir", required=True)
    ap.add_argument("--stt-json", help="cached {device: [epoch]} of stt-only runs (else listed from the pod, read only)")
    ap.add_argument("--context", default="grigri")
    ap.add_argument("--namespace", default="home-assistant")
    ap.add_argument("--pod", default="home-assistant-hass-0")
    ap.add_argument("--container")
    ap.add_argument("--recordings-root", default="/voice-recordings")
    ap.add_argument("--devices")
    ap.add_argument("--devices-file")
    ap.add_argument("--from-dir")
    ap.add_argument("--phrase-flags", help="comma list: data-quality hardneg_phrase_flags.csv, roomneg_phrase_flags.csv (v6.2)")
    ap.add_argument("--drop-on", default="live,stt-only,other-event,miss-candidate,asr-phrase",
                    help="reasons that drop an event (v6.2: live,stt-only,miss-candidate,asr-phrase)")
    a = ap.parse_args()
    import kivi_live_wakes as L
    import kivi_replay_mine as K
    root = Path(a.dataset_root)
    live = K.live_detection_times(root)
    if a.stt_json and Path(a.stt_json).exists():
        stt = json.loads(Path(a.stt_json).read_text())
    else:
        stt = K.stt_only_times(L.get_fetcher(a), L.devices_prefix(a))
        if a.stt_json:
            Path(a.stt_json).write_text(json.dumps(stt))
    cands = {}
    for r in csv.DictReader(open(root / "live-misses/miss_candidates.csv", encoding="utf-8")):
        cands.setdefault(r["device"], []).append(utc_epoch(r["utc"]))
    phrase = phrase_flagged(a.phrase_flags) if a.phrase_flags else None
    drop_on = set(a.drop_on.split(","))
    pieces = R.load_index(argparse.Namespace(index=a.index, part=a.parts, local_date=None))
    heads = {n: R.Head(p) for n, p in HEADS.items()}
    # pass 1: every model's events (floor 0.5) per device, absolute times
    per_piece, others = [], {}
    for pc in pieces:
        e = np.load(pc["emb"], mmap_mode="r")
        t0 = R._utc(pc["start_utc"]).timestamp()
        probs = {}
        for n, h in heads.items():
            steps, p = R.piece_probs(h, e)
            probs[n] = (steps, p)
            times = R.step_time(steps)
            for i in R.detections(times, p, EVENT_FLOOR):
                others.setdefault(pc["device"], []).append(t0 + float(times[i]))
        per_piece.append((pc, e, t0, probs))
    # pass 2: mined events, flags, kept windows
    kept = {"v6_day": [], "v6_evening": [], "v5": []}
    summ = {}
    for pc, e, t0, probs in per_piece:
        v = R.window_view(e)
        for n, floor in MINED.items():
            steps, p = probs[n]
            times = R.step_time(steps)
            ev_idx = R.detections(times, p, floor)
            if not ev_idx:
                continue
            ev_t = np.array([times[i] for i in ev_idx])
            hit = np.flatnonzero(p > floor)
            sel = sorted({i + d for i in hit for d in (-1, 0, 1) if 0 <= i + d < len(p)})
            owner = {i: int(np.argmin(np.abs(ev_t - times[i]))) for i in sel}
            evflags = []
            for k, ti in enumerate(ev_t):
                tab = t0 + float(ti)
                fl = flags_for(tab, pc["device"], live, stt,
                               {pc["device"]: [x for x in others.get(pc["device"], []) if abs(x - tab) > 2.0]}, cands, phrase)
                fl = [f for f in fl if f in drop_on]
                evening, day = R.is_evening(pc["start_utc"], float(ti))
                evflags.append((fl, evening, day))
                key = f"{n}|{day}|{'evening' if evening else 'day'}"
                c = summ.setdefault(key, {"events": 0, "dropped": 0, "windows": 0, "windows_dropped": 0, "why": {}})
                c["events"] += 1
                if fl:
                    c["dropped"] += 1
                    for f in fl:
                        c["why"][f] = c["why"].get(f, 0) + 1
            for i in sel:
                fl, evening, day = evflags[owner[i]]
                key = f"{n}|{day}|{'evening' if evening else 'day'}"
                summ[key]["windows"] += 1
                if fl:
                    summ[key]["windows_dropped"] += 1
                    continue
                w = np.asarray(v[steps[i] - 15], np.float16)[None]
                if n == "v5":
                    kept["v5"].append(w)
                else:
                    kept["v6_evening" if evening else "v6_day"].append(w)
    out = Path(a.out_dir)
    for name, fname in (("v6_day", "hard_v6_day.npy"), ("v6_evening", "hard_v6_evening.npy"), ("v5", "hard_v5.npy")):
        dst = out / fname
        bak = out / fname.replace(".npy", "_unfiltered.npy")
        if dst.exists() and not bak.exists():
            os.replace(dst, bak)
        X = np.concatenate(kept[name]) if kept[name] else np.zeros((0, 16, 96), np.float16)
        np.save(dst, X)
        os.chmod(dst, 0o600)
        print(dst, X.shape, flush=True)
    (out / "hard_filter_summary.json").write_text(json.dumps(dict(sorted(summ.items())), indent=1))
    for k, c in sorted(summ.items()):
        print(k, c, flush=True)


if __name__ == "__main__":
    main()
