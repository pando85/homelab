#!/usr/bin/env python3
"""Kivi v6.3 retry ("v6.3r") data, pre-registered in kivi-models docs/plan/2026-11-false-wake-plan.md section 11.
`v6p3_data.py` stays frozen; this builder reuses its outputs and the clean features of `kivi_clean.py build`. Counts only.

    v6p3r_data.py build    # -> $KIVI_HOME/v6p3r/feat/*.npy, v6p3r/room_exclude.json, v6p3r/data_summary.json

Day rule: training reads even Madrid days and the grandfathered 2026-11-05 to -07, i.e. 2026-11-05, -06, -08 and -12
(released for training on 2026-11-12, plan 10.1). **2026-11-10 is the selection day: no room window, mined moment,
labelled window or live clip of 2026-11-10 is used** (the v6p3 live features of 2026-11-10 are removed row by row). No
odd day from 2026-11-09 on is read.

Labels are read fresh at build time (the owner labels concurrently): window tables (`kivi_clean.window_rows`, every table),
`live/live_wakes.csv` and the plan (`labelq/plan.csv`, pending items). Speakers are never used (only the owner's speaker
labels would count, and they put every call on one speaker): the positives are one group.

Exclusions (room-derived negatives: room windows and mined moments): the v6p3 exclusions (ASR phrase flags, owner
attempts, amendment-5 events), the intervals of `v6p3r/clean/drop.json`, every owner-labelled attempt (+-3 s), and every
window that still waits for the owner or is labelled unclear (+-3 s): an unreviewed oye-like moment may be a call.

Sets written (v6p3r/feat/):
  pos_live      v6p3 live positives (spk01 + spk02 merged; clips still labelled true by a trusted source) and the
                2026-11-12 owner-labelled calls (clean, the last 1 s of the wake tail, repeated to 150 rows per clip)
  pos_att       v6p3 attempt windows still labelled attempt (none on 2026-11-10)
  hard, conv, negpart, winneg   v6p3 live sets minus 2026-11-10, minus `drop.json` live runs, minus clips whose label
                changed; plus the 2026-11-12 false clips (clean tail -> hard, request audio -> conv; 180 / 30 rows per clip)
  hwin_eve, hwin_day   labelled false windows of every table (owner other-speech / tv-media / noise, trusted auto
                other-speech / noise), train days, minus `drop.json` windows; evening = 18-24 Madrid (clean features)
  mined_v6x_day, mined_v6x_eve, mined_v5   room moments of `labelq/moments.json` on train days, v6 or v6.2 >= 0.5
                (v6x) else v5 >= 0.3, the peak step +-1 (kivi_clean features), minus the exclusions
  room_w8       v6p3 room windows (2026-11-05/-06/-08, stride 8) minus the exclusions
  room_1112     2026-11-12 room windows (stride 32, kivi_clean `room_new`) minus the exclusions
The decoys (`v6p3r/decoys/feat/v6p3r_decoy_conf.npy`) are used as they are.
"""
import argparse
import json
import os
import sys
from collections import Counter, defaultdict

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

import numpy as np  # noqa: E402

import kivi_clean as KC  # noqa: E402
import kivi_live_wakes as L  # noqa: E402
import kivi_real_common as C  # noqa: E402

KH = C.kivi_home()
ROOT = KH / "datasets" / "real"
OUT = KH / "v6p3r"
P3 = KH / "v6p3" / "feat"
SEL_DAY = "2026-11-10"
TRAIN_DAYS = ("2026-11-05", "2026-11-06", "2026-11-07", "2026-11-08", "2026-11-12")
TRUSTED_LIVE = ("owner", "owner-button", "auto-v1", "")
FALSE_WIN = ("other-speech", "tv-media", "noise")
AUTO_FALSE_WIN = ("other-speech", "noise")
MATCH_S = 3.0
PER_CLIP = {"pos_live": 150, "hard": 180, "conv": 30}


def is_train(day):
    return day in TRAIN_DAYS


def save(name, X, summ):
    fd = OUT / "feat"
    fd.mkdir(parents=True, exist_ok=True)
    os.chmod(fd, 0o700)
    X = np.asarray(X, np.float16) if len(X) else np.zeros((0, 16, 96), np.float16)
    np.save(fd / f"{name}.npy", X)
    os.chmod(fd / f"{name}.npy", 0o600)
    summ["rows"][name] = int(len(X))


def live_rows():
    rows = {}
    for r in L.read_table(ROOT / "live" / "live_wakes.csv"):
        rows[f"{r['device']}/{r['utc']}"] = r
    return rows


def live_class(r):
    if r["label_source"] in TRUSTED_LIVE and r["label"] in ("true", "false"):
        return r["label"]
    return r["label"] or "unlabelled"


def window_class(w):
    """false / attempt / unclear / pending / other of a window row, from its current label and source."""
    lab, src = w["label"], w["source"]
    if not lab:
        return "pending"
    if lab == "attempt":
        return "attempt" if src in KC.OWNER else "pending"     # an auto `attempt` always waits for the owner
    if lab == "unclear":
        return "unclear"
    if lab in FALSE_WIN and (src in KC.OWNER or (src.startswith("auto") and lab in AUTO_FALSE_WIN)):
        return "false"
    return "other"


def build_exclusions(windows, pending):
    iv = defaultdict(list)
    why = Counter()
    for dev, xs in json.loads((KH / "v6p3" / "room_exclude.json").read_text()).items():
        iv[dev] += [tuple(x) for x in xs]
        why["v6p3"] += len(xs)
    drop = json.loads((OUT / "clean" / "drop.json").read_text())
    for dev, xs in drop["intervals"].items():
        iv[dev] += [tuple(x) for x in xs]
        why["drop.json"] += len(xs)
    for w in windows:
        c = window_class(w)
        if ("window", w["table"], w["id"]) in pending and c == "false":
            c = "pending"                      # an audit item: its auto label waits for the owner
        if c in ("attempt", "unclear", "pending"):
            iv[w["device"]].append((w["t"] - MATCH_S, w["t"] + MATCH_S))
            why[c] += 1
    for dev in iv:
        iv[dev].sort()
    return iv, why, drop


def inside_any(iv, dev, ts):
    """Boolean mask of the times inside any interval of the TV (vectorised)."""
    ts = np.asarray(ts, float)
    m = np.zeros(len(ts), bool)
    for a, b in iv.get(dev, ()):
        m |= (ts >= a) & (ts <= b)
    return m


def cmd_build(a):
    OUT.mkdir(parents=True, exist_ok=True)
    windows = KC.window_rows()
    win_by_ref = {(w["table"], w["id"]): w for w in windows}
    pending = KC.plan_pending_refs()
    iv, why, drop = build_exclusions(windows, pending)
    (OUT / "room_exclude.json").write_text(json.dumps({d: [list(x) for x in v] for d, v in iv.items()}))
    os.chmod(OUT / "room_exclude.json", 0o600)
    summ = {"exclusions": dict(why), "rows": {}, "items": Counter(), "dropped": Counter()}
    live = live_rows()
    run_of = {k: r["run_id"] for k, r in live.items()}
    drop_runs = set(drop["live_run_ids"])
    drop_wins = set(drop["window_ids"])
    out = defaultdict(list)

    # 1. the v6p3 live sets, row by row (sources.txt: <device>/<utc> of a live clip or win:<table>:<id>)
    want = {"pos_spk01": ("pos_live", "true"), "pos_spk02": ("pos_live", "true"), "pos_att": ("pos_att", "attempt"),
            "hard": ("hard", "false"), "conv": ("conv", "false"), "negpart": ("negpart", "true"),
            "winneg": ("winneg", "false")}
    for g, (dst, need) in want.items():
        X = np.load(P3 / f"live_std_{g}.npy", mmap_mode="r")
        src = (P3 / f"live_std_{g}.npy.sources.txt").read_text().split("\n")[:len(X)]
        keep = np.zeros(len(X), bool)
        for i, s in enumerate(src):
            if s.startswith("win:"):
                _, table, ref = s.split(":", 2)
                w = win_by_ref.get((table, ref))
                if w is None:
                    summ["dropped"][f"v6p3 {g}: window gone"] += 1
                    continue
                day = KC.madrid(w["t"])[0]
                ok = is_train(day) and window_class(w) == need and f"{table}:{ref}" not in drop_wins
            else:
                r = live.get(s)
                if r is None:
                    summ["dropped"][f"v6p3 {g}: clip gone"] += 1
                    continue
                day = KC.madrid(L.detection_epoch(r))[0]
                ok = is_train(day) and live_class(r) == need and run_of[s] not in drop_runs
            keep[i] = ok
            if not ok:
                summ["dropped"][f"v6p3 {g}: {'selection day' if day == SEL_DAY else 'label / drop'}"] += 1
        out[dst].append(np.asarray(X[keep], np.float16))
        summ["items"][f"v6p3 {g} sources kept"] += len({s for s, k in zip(src, keep) if k})

    # 2. kivi_clean items (clean features), labels read now
    items = KC.read_items()
    H = {s: np.load(OUT / "clean" / "feat" / f"house_{s}.npy", mmap_mode="r")
         for s in ("live_pos", "live_hard", "live_conv", "win_neg", "mined", "room_new")}
    seen_lq = set()
    for it in items:
        s = it["set"]
        if s in ("live_pos", "live_hard", "live_conv"):
            ref = it["ref"]
            r = next((x for x in live.values() if x["run_id"] == ref), None)
            if r is None or it["day"] != "2026-11-12":         # 11-08 clips come from the v6p3 sets (augmented)
                continue
            if ref in drop_runs:
                summ["dropped"][f"{s} 11-12: drop.json"] += 1
                continue
            a0, b0 = it["rows"]
            if s == "live_pos":
                if not (live_class(r) == "true" and r["label_source"] in KC.OWNER):
                    summ["dropped"]["live_pos 11-12: not an owner call now"] += 1
                    continue
                rows = np.asarray(H[s][a0:b0], np.float16)[[i for i in it["score_rows"]]]
                dst = "pos_live"
            else:
                if live_class(r) != "false":
                    summ["dropped"][f"{s} 11-12: label now {live_class(r)}"] += 1
                    continue
                rows = np.asarray(H[s][a0:b0], np.float16)
                dst = "hard" if s == "live_hard" else "conv"
            if len(rows):
                idx = np.resize(np.arange(len(rows)), PER_CLIP[dst])
                out[dst].append(rows[idx])
                summ["items"][f"{dst} 11-12 clips"] += 1
        elif s == "win_neg":
            w = win_by_ref.get((it["table"], it["ref"]))
            if w is None or not is_train(it["day"]):
                continue
            seen_lq.add((it["table"], it["ref"]))
            if window_class(w) != "false" or ("window", w["table"], w["id"]) in pending:
                summ["dropped"]["window: not a definite false label now"] += 1
                continue
            if f"{it['table']}:{it['ref']}" in drop_wins:
                summ["dropped"]["window: drop.json"] += 1
                continue
            a0, b0 = it["rows"]
            dst = "hwin_eve" if it["evening"] else "hwin_day"
            out[dst].append(np.asarray(H[s][a0:b0], np.float16))
            summ["items"][f"{dst} windows|{it['device']}"] += 1
        elif s == "mined":
            if not is_train(it["day"]):
                continue
            if inside_any(iv, it["device"], [it["t"]])[0]:
                summ["dropped"]["mined: excluded"] += 1
                continue
            sc = it["scores"]
            v6x = sc.get("v6", 0) >= 0.5 or sc.get("v6.2", 0) >= 0.5
            if not v6x and sc.get("v5", 0) < 0.3:
                continue
            dst = ("mined_v6x_eve" if it["evening"] else "mined_v6x_day") if v6x else "mined_v5"
            a0, b0 = it["rows"]
            out[dst].append(np.asarray(H[s][a0:b0], np.float16))
            summ["items"][f"{dst} moments|{it['device']}"] += 1
        elif s == "room_new":
            if it["day"] != "2026-11-12":
                continue
            a0, b0 = it["rows"]
            m = ~inside_any(iv, it["device"], it["row_t"])
            out["room_1112"].append(np.asarray(H[s][a0:b0], np.float16)[m])
            summ["dropped"]["room_1112 rows excluded"] += int((~m).sum())
    # windows labelled false since the kivi_clean build (not in its items): clean features now
    emb = None
    for w in windows:
        day, evening = KC.madrid(w["t"])
        if (w["table"], w["id"]) in seen_lq or not is_train(day) or w["table"] == KC.TABLE:
            continue
        if window_class(w) != "false" or ("window", w["table"], w["id"]) in pending:
            continue
        if f"{w['table']}:{w['id']}" in drop_wins:
            continue
        path = ROOT / "live-misses" / w["clip"]
        if not w.get("clip") or not path.exists():
            summ["dropped"]["new window: clip missing"] += 1
            continue
        emb = emb or KC.Embedder()
        X, _ = KC.cached_windows(emb, f"win:{w['table']}:{w['id']}:{w['clip']}", lambda p=path: KC.read_wav(p))
        dst = "hwin_eve" if evening else "hwin_day"
        out[dst].append(np.asarray(X[::2], np.float16))
        summ["items"][f"{dst} windows|{w['device']} (labelled after the clean build)"] += 1

    # 3. room windows of the v6p3 builder minus the new exclusions
    X = np.load(P3 / "room_train_w8.npy", mmap_mode="r")
    devs, ts = KC.w8_meta()
    assert len(devs) == len(X), (len(devs), len(X))
    keep = np.ones(len(X), bool)
    for dev in set(devs.tolist()):
        sel = devs == dev
        keep[sel] = ~inside_any(iv, dev, ts[sel])
    out["room_w8"].append(np.asarray(X[keep], np.float16))
    summ["dropped"]["room_w8 rows excluded"] = int((~keep).sum())

    for name in ("pos_live", "pos_att", "hard", "conv", "negpart", "winneg", "hwin_eve", "hwin_day", "mined_v6x_day",
                 "mined_v6x_eve", "mined_v5", "room_w8", "room_1112"):
        parts = [p for p in out.get(name, []) if len(p)]
        save(name, np.concatenate(parts) if parts else [], summ)
    summ["items"] = dict(sorted(summ["items"].items()))
    summ["dropped"] = dict(sorted(summ["dropped"].items()))
    summ["train_days"] = TRAIN_DAYS
    summ["selection_day"] = SEL_DAY
    (OUT / "data_summary.json").write_text(json.dumps(summ, indent=1))
    os.chmod(OUT / "data_summary.json", 0o600)
    print(json.dumps(summ, indent=1))


def main():
    ap = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    ap.add_argument("what", choices=["build"])
    a = ap.parse_args()
    {"build": cmd_build}[a.what](a)


if __name__ == "__main__":
    main()
