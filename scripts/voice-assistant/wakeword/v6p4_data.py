#!/usr/bin/env python3
"""Kivi v6.4 data, pre-registered in kivi-models docs/plan/2026-11-false-wake-plan.md section 13.
`v6p3r_data.py` stays frozen; this builder reuses its recipe with the v6.4 day sets and a rebuilt clean inventory. Counts only.

    v6p4_data.py clean     # kivi_clean.py build into $KIVI_HOME/v6p4/clean (the v6p3r/clean cache is read, never written)
    v6p4_data.py build     # -> $KIVI_HOME/v6p4/feat/*.npy, v6p4/room_exclude.json, v6p4/data_summary.json
    v6p4_data.py counts    # counts per source, day role, TV and label (labels read fresh) as markdown tables

Day rule: training reads even Madrid days and the grandfathered 2026-11-05 to -07, i.e. 2026-11-05, -06, -08, -10 and
-12 (2026-11-10 was the v6.3r selection day: none of its material was trained on, so it is free now). **2026-11-14 is the
v6.4 selection day: no room window, mined moment, labelled window or live clip of 2026-11-14 is used.** 2026-11-16 (even,
not collected) contributes only the owner's take clips (`v6p4_takes.sh`). No odd day from 2026-11-09 on is read.

Labels are read fresh at build time (the owner labels concurrently): window tables (`kivi_clean.window_rows`, every table),
`live/live_wakes.csv` and the plan (`labelq/plan.csv`, pending items). Speakers are never used (K-102): one positive group.

Exclusions (room-derived negatives: room windows and mined moments): the v6p3 exclusions (ASR phrase flags, owner
attempts, amendment-5 events), the intervals of `v6p3r/clean/drop.json`, every owner-labelled attempt (+-3 s), and every
window that still waits for the owner or is labelled unclear (+-3 s): an unreviewed oye-like moment may be a call.

Sets written (v6p4/feat/), as v6p3r_data.py unless noted:
  pos_live      v6p3 live positives of <= 2026-11-08 (spk01 + spk02 merged) and the owner-labelled or trusted true live
                clips of 2026-11-10 and -12 (clean, the last 1 s of the wake tail, repeated to 150 rows per clip)
  pos_att       v6p3 attempt windows of <= 2026-11-08 and owner attempt windows of later train days (repeated to 150)
  hard, conv, negpart, winneg   v6p3 live sets (<= 2026-11-08) plus the false live clips (owner, owner-button, trusted auto)
                of 2026-11-10 and -12 (clean tail -> hard, request audio -> conv; 180 / 30 rows per clip)
  hwin_eve, hwin_day   labelled false windows of every table (owner other-speech / tv-media / noise, trusted auto
                other-speech / noise incl. auto-quiet), train days, minus `drop.json` windows; evening = 18-24 Madrid
  mined_v6x_day, mined_v6x_eve, mined_v5   room moments of `labelq/moments.json` on train days (as v6p3r)
  room_w8       v6p3 room windows (2026-11-05/-06/-08, stride 8) minus the exclusions
  room_1110, room_1112   room windows of 2026-11-10 and -12 (stride 32, kivi_clean `room_new`) minus the exclusions
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
OUT = KH / "v6p4"
OLD = KH / "v6p3r"
CLEAN = OUT / "clean"
P3 = KH / "v6p3" / "feat"
SEL_DAY = "2026-11-14"
TRAIN_DAYS = ("2026-11-05", "2026-11-06", "2026-11-07", "2026-11-08", "2026-11-10", "2026-11-12")
CLEAN_DAYS = ("2026-11-10", "2026-11-12")      # live clips and room windows come from the clean inventory
TRUSTED_LIVE = ("owner", "owner-button", "auto-v1", "")
FALSE_WIN = ("other-speech", "tv-media", "noise")
AUTO_FALSE_WIN = ("other-speech", "noise")
MATCH_S = 3.0
PER_CLIP = {"pos_live": 150, "hard": 180, "conv": 30, "pos_att": 150}


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
    drop = json.loads((OLD / "clean" / "drop.json").read_text())
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


def cmd_clean(a):
    """kivi_clean.py build into v6p4/clean: the inventory of the even days (windows, live clips, mined moments, room windows
    of 2026-11-10 and -12), features cached per item. The frozen v6p3r/clean cache is read when an item is there, never
    written, so v6p3r stays reproducible."""
    CLEAN.mkdir(parents=True, exist_ok=True)
    os.chmod(CLEAN, 0o700)
    KC.OUT = CLEAN
    orig = KC.feat_cache

    def cache(key):
        new = orig(key)                                    # KC.OUT is now v6p4/clean
        old = OLD / "clean" / "feat" / new.name
        return old if old.exists() and not new.exists() else new
    KC.feat_cache = cache
    KC.cmd_build(a)


def cmd_build(a):
    KC.OUT = CLEAN
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
                ok = is_train(day) and day not in CLEAN_DAYS and window_class(w) == need and f"{table}:{ref}" not in drop_wins
            else:
                r = live.get(s)
                if r is None:
                    summ["dropped"][f"v6p3 {g}: clip gone"] += 1
                    continue
                day = KC.madrid(L.detection_epoch(r))[0]
                ok = is_train(day) and day not in CLEAN_DAYS and live_class(r) == need and run_of[s] not in drop_runs
            keep[i] = ok
            if not ok:
                summ["dropped"][f"v6p3 {g}: {'selection day' if day == SEL_DAY else 'clean inventory day' if day in CLEAN_DAYS else 'label / drop'}"] += 1
        out[dst].append(np.asarray(X[keep], np.float16))
        summ["items"][f"v6p3 {g} sources kept"] += len({s for s, k in zip(src, keep) if k})

    # 2. kivi_clean items (clean features), labels read now
    items = KC.read_items()
    H = {s: np.load(CLEAN / "feat" / f"house_{s}.npy", mmap_mode="r")
         for s in ("live_pos", "live_hard", "live_conv", "win_neg", "mined", "room_new")}
    seen_lq = set()
    for it in items:
        s = it["set"]
        if s in ("live_pos", "live_hard", "live_conv"):
            ref = it["ref"]
            r = next((x for x in live.values() if x["run_id"] == ref), None)
            if r is None or it["day"] not in CLEAN_DAYS:       # <= 11-08 clips come from the v6p3 sets (augmented)
                continue
            if ref in drop_runs:
                summ["dropped"][f"{s} {it['day']}: drop.json"] += 1
                continue
            a0, b0 = it["rows"]
            if s == "live_pos":
                if live_class(r) != "true":
                    summ["dropped"][f"live_pos {it['day']}: not a trusted call now"] += 1
                    continue
                rows = np.asarray(H[s][a0:b0], np.float16)[[i for i in it["score_rows"]]]
                dst = "pos_live"
            else:
                if live_class(r) != "false":
                    summ["dropped"][f"{s} {it['day']}: label now {live_class(r)}"] += 1
                    continue
                rows = np.asarray(H[s][a0:b0], np.float16)
                dst = "hard" if s == "live_hard" else "conv"
            if len(rows):
                idx = np.resize(np.arange(len(rows)), PER_CLIP[dst])
                out[dst].append(rows[idx])
                summ["items"][f"{dst} {it['day']} clips|{r['device']}|{r['label_source'] or 'none'}"] += 1
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
            if it["day"] not in CLEAN_DAYS:
                continue
            a0, b0 = it["rows"]
            m = ~inside_any(iv, it["device"], it["row_t"])
            name = "room_" + it["day"][5:].replace("-", "")
            out[name].append(np.asarray(H[s][a0:b0], np.float16)[m])
            summ["dropped"][f"{name} rows excluded"] += int((~m).sum())
        elif s == "win_att":
            w = win_by_ref.get((it["table"], it["ref"]))
            if w is None or not is_train(it["day"]) or it["day"] <= "2026-11-08":     # <= 11-08: v6p3 pos_att
                continue
            if window_class(w) != "attempt" or f"{it['table']}:{it['ref']}" in drop_wins:
                continue
            a0, b0 = it["rows"]
            rows = np.asarray(H[s][a0:b0], np.float16)
            out["pos_att"].append(rows[np.resize(np.arange(len(rows)), PER_CLIP["pos_att"])])
            summ["items"][f"pos_att owner attempts {it['day']}|{it['device']}"] += 1
    # windows labelled false that the (just rebuilt) clean inventory does not hold: counted, never featurised here
    for w in windows:
        day = KC.madrid(w["t"])[0]
        if (w["table"], w["id"]) in seen_lq or not is_train(day) or w["table"] == KC.TABLE:
            continue
        if window_class(w) == "false" and ("window", w["table"], w["id"]) not in pending \
                and f"{w['table']}:{w['id']}" not in drop_wins:
            summ["dropped"]["window labelled false but not in the clean inventory (run `clean` again)"] += 1

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
                 "mined_v6x_eve", "mined_v5", "room_w8", "room_1110", "room_1112"):
        parts = [p for p in out.get(name, []) if len(p)]
        save(name, np.concatenate(parts) if parts else [], summ)
    summ["items"] = dict(sorted(summ["items"].items()))
    summ["dropped"] = dict(sorted(summ["dropped"].items()))
    summ["train_days"] = TRAIN_DAYS
    summ["selection_day"] = SEL_DAY
    (OUT / "data_summary.json").write_text(json.dumps(summ, indent=1))
    os.chmod(OUT / "data_summary.json", 0o600)
    print(json.dumps(summ, indent=1))


def role(day):
    """train (feeds v6.4), selection (2026-11-14), test (odd, never read), later (even, after the build days)."""
    if day == SEL_DAY:
        return "selection"
    if is_train(day):
        return "train"
    return "test" if int(day[-2:]) % 2 else "later"


def table_md(head, counter, cols):
    """Markdown table of a Counter keyed by tuples: the first len(cols) - 1 key parts are the row, the last one the count."""
    rows = sorted(counter.items())
    out = ["| " + " | ".join(head) + " | n |", "| " + " | ".join("---" for _ in head) + " | ---: |"]
    out += ["| " + " | ".join(str(x) for x in k) + f" | {v} |" for k, v in rows]
    return "\n".join(out)


def cmd_counts(a):
    """Counts only, labels read now: labelled windows and live clips by source, day role, TV and class."""
    pend = KC.plan_pending_refs()
    win = Counter()
    for w in KC.window_rows():
        day = KC.madrid(w["t"])[0]
        cls = window_class(w)
        if ("window", w["table"], w["id"]) in pend and cls == "false":
            cls = "pending (blind audit of an auto label)"
        src = w["source"] if cls in ("false", "attempt") else ""
        if w["table"] == KC.TABLE:
            continue                                        # the cleaning items duplicate material counted elsewhere
        win[(role(day), w["device"], cls, src or "-")] += 1
    live = Counter()
    for r in L.read_table(ROOT / "live" / "live_wakes.csv"):
        day = KC.madrid(L.detection_epoch(r))[0]
        live[(role(day), r["device"], r.get("trigger", "") or "-", live_class(r), r["label_source"] or "-")] += 1
    print("Windows (label tables except the cleaning table): role, TV, class, label source\n")
    print(table_md(["role", "TV", "class", "source"], win, 4))
    print("\nLive clips: role, TV, trigger, class, label source\n")
    print(table_md(["role", "TV", "trigger", "class", "source"], live, 5))
    summ = OUT / "data_summary.json"
    if summ.exists():
        rows = json.loads(summ.read_text())["rows"]
        print("\nFeature sets (rows, v6p4/data_summary.json)\n")
        print("| set | rows |\n| --- | ---: |\n" + "\n".join(f"| `{k}` | {v:,} |" for k, v in rows.items()))


def main():
    ap = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    ap.add_argument("what", choices=["clean", "build", "counts"])
    a = ap.parse_args()
    {"clean": cmd_clean, "build": cmd_build, "counts": cmd_counts}[a.what](a)


if __name__ == "__main__":
    main()
