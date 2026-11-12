#!/usr/bin/env python3
"""Real-use statistics of the "oye Kivi" wake word, from the labels as they stand (read fresh on every run, read only).

    kivi_usage.py report [--until ISO] [--out DIR]     # -> $KIVI_HOME/audit/usage/USAGE.md and usage.json

Counts only: no audio is read, played or transcribed; only the label tables are read, and nothing is written into them
(the owner may be labelling with `kivi_live_wakes.py queue` at the same time).

What is counted, per TV, speaker (spk01 / spk02 / other / unknown), model era (`kivi_live_wakes.ERAS`) and Madrid day,
evening (18-24) apart:
  calls caught live   live runs (`live/live_wakes.csv`) triggered by the wake word (trigger wake or unknown) and labelled
                      `true` by the owner, by a trusted auto label (kivi_autolabel.is_uncertain is False) or by an older
                      label without a source (shown apart);
  calls missed        owner-labelled `attempt` windows of every window table (`live-misses/*.csv`), unique per TV within
                      3 s, with no wake-triggered live run on that TV within 10 s; those that came from a P4 item of the
                      labelling plan (a moment followed by a live run) are shown apart. Retry evidence is not used;
  false wakes         wake-triggered live runs labelled `false` (owner, owner-button, trusted auto, older no-source), per
                      evening of exposure: the hours of 18-24 Madrid in which that model was the live model on that TV,
                      inside the live data (from kivi_live_wakes.SCOPE_START to --until) / 6;
  button sessions     live runs triggered by the mic button (trigger `button`), per day of exposure.
What the labels do not support is shown, never folded in: live runs with no trusted label (unlabelled, unclear,
not-a-wake, uncertain auto labels), calls with speaker unknown, and pending windows (no definite label yet) by their
hidden auto label (`labelq/key.csv`: attempt, unclear, audit of a trusted non-call, or none for older tables). A missed
call can only be found where room audio was copied and scanned (`labelq/moments.json` pieces) and a head reached raw
0.3 there; the scanned room hours per TV and day are listed. Rates: catch rate with a Clopper-Pearson 95 % interval and
a worst case that counts every pending attempt-like window as a missed call; per-evening and per-day rates with an
exact Poisson 95 % interval.
"""
import argparse
import json
import math
import os
import sys
from collections import Counter, defaultdict
from datetime import datetime, timedelta, timezone
from pathlib import Path
from zoneinfo import ZoneInfo

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

import kivi_live_wakes as L  # noqa: E402
import kivi_real_common as C  # noqa: E402

TZ = ZoneInfo("Europe/Madrid")
KH = C.kivi_home()
MISS_S = 10.0              # an attempt with a wake-triggered live run this close on the same TV was caught
DEDUP_S = 3.0              # attempt windows of one TV this close are one attempt (as section 8.1 of the plan)
STEP_S = 30.0              # exposure resolution (era edges are known to the second; error <= 30 s per edge)
MODEL_NAMES = {"oye_kivi": "v4", "oye_kivi_next": "v5", "oye_kivi_ctx": "v6", "oye_kivi_ctx2": "v6.2", "oye_kivi_ctx3": "v6.3"}
SPEAKERS = ("spk01", "spk02", "other", "unknown")
OWNER = ("owner", "owner-button")
SCAN_INDEXES = ("v6/room_emb/index.json", "labelq/room_emb/index.json")


# ---------------------------------------------------------------------------------------------
# pure helpers
# ---------------------------------------------------------------------------------------------


def madrid(t):
    lt = datetime.fromtimestamp(t, TZ)
    return lt.date().isoformat(), 18 <= lt.hour < 24


def stamp_epoch(s):
    return datetime.strptime(s, "%Y%m%dT%H%M%S.%fZ").replace(tzinfo=timezone.utc).timestamp()


def era(model_id):
    return MODEL_NAMES.get(model_id, model_id or "none")


def spk_class(s):
    return s if s in ("spk01", "spk02") else "other" if s == "spk-other" else "unknown"


def source_class(src):
    return "owner" if src in OWNER else "auto" if src.startswith("auto") else "none"


def live_class(row):
    """(class, source) of a live run: class call / false / not-a-wake / uncertain for wake-triggered runs (trigger wake or
    unknown), button-<label or unlabelled> for mic-button runs."""
    import kivi_autolabel as A
    src = source_class(row["label_source"])
    if row["trigger"] == "button":
        return "button-" + (row["label"] or "unlabelled"), src
    trusted = src in ("owner", "none") and row["label"] or (src == "auto" and not A.is_uncertain(row))
    if not trusted:
        return "uncertain", src
    if row["label"] == "true":
        return "call", src
    if row["label"] == "false":
        return "false", src
    if row["label"] == "not-a-wake":
        return "not-a-wake", src
    return "uncertain", src


def tiers_of(r):
    return {x for x in [r.get("tier") or ""] + (r.get("sources") or "").split("+") if x}


def unique_attempts(rows, dedup_s=DEDUP_S):
    """rows: [{device, t, speaker, tier, table, id}] of owner-labelled attempts -> one per TV within dedup_s (chained),
    first row's time, first non-empty speaker, union of tiers."""
    out = []
    for r in sorted(rows, key=lambda r: (r["device"], r["t"])):
        last = out[-1] if out else None
        if last and last["device"] == r["device"] and r["t"] - last["t_last"] <= dedup_s:
            last["t_last"] = r["t"]
            last["tiers"] |= tiers_of(r)
            last["refs"].append(f"{r['table']}:{r['id']}")
            if not last["speaker"] and r.get("speaker"):
                last["speaker"] = r["speaker"]
            continue
        out.append({"device": r["device"], "t": r["t"], "t_last": r["t"], "speaker": r.get("speaker") or "",
                    "tiers": tiers_of(r), "refs": [f"{r['table']}:{r['id']}"]})
    return out


def caught_by_live(att, wake_times, miss_s=MISS_S):
    return any(abs(x - att["t"]) <= miss_s for x in wake_times.get(att["device"], ()))


def exposure(devices, t0, t1, model_of, step_s=STEP_S):
    """Counter {(device, era, day, evening): hours} of the live model of each TV between t0 and t1 (era from model_of)."""
    out = Counter()
    if t1 <= t0:
        return out
    n = math.ceil((t1 - t0) / step_s)
    for dev in devices:
        for i in range(n):
            a = t0 + i * step_s
            w = min(step_s, t1 - a)
            day, ev = madrid(a + w / 2)
            out[(dev, era(model_of(dev, a + w / 2)), day, ev)] += w / 3600
    return out


def union_hours(intervals):
    """Counter {(day, evening): hours} of the union of [a, b) intervals (epoch s), split at Madrid hours."""
    out = Counter()
    cur = None
    merged = []
    for a, b in sorted(intervals):
        if cur and a <= cur[1]:
            cur[1] = max(cur[1], b)
        else:
            cur = [a, b]
            merged.append(cur)
    for a, b in merged:
        t = a
        while t < b:
            lt = datetime.fromtimestamp(t, TZ)
            nxt = (lt.replace(minute=0, second=0, microsecond=0) + timedelta(hours=1)).timestamp()
            e = min(b, nxt)
            out[madrid(t)] += (e - t) / 3600
            t = e
    return out


def clopper_pearson(k, n):
    from scipy.stats import beta
    if n == 0:
        return (math.nan, math.nan)
    lo = 0.0 if k == 0 else float(beta.ppf(0.025, k, n - k + 1))
    hi = 1.0 if k == n else float(beta.ppf(0.975, k + 1, n - k))
    return lo, hi


def poisson_ci(k, t):
    from scipy.stats import chi2
    if t <= 0:
        return (math.nan, math.nan)
    lo = 0.0 if k == 0 else float(chi2.ppf(0.025, 2 * k)) / 2
    return lo / t, float(chi2.ppf(0.975, 2 * k + 2)) / 2 / t


# ---------------------------------------------------------------------------------------------
# reading (fresh, read only)
# ---------------------------------------------------------------------------------------------


def live_until(root):
    """End of the live data: the newest live clip directory (written by the last extraction)."""
    newest = 0.0
    for d in (Path(root) / "live").glob("c3-*/*"):
        if d.is_dir():
            newest = max(newest, d.stat().st_mtime)
    return newest


def read_windows(root):
    """[{device, t, label, source, speaker, tier, table, id}] of every window table row, read fresh. Tables without a
    label_source column were labelled by the owner (kivi_live_wakes.py review-misses / queue)."""
    out = []
    for p in L.window_tables(root):
        for r in L.read_miss_table(p):
            try:
                t = stamp_epoch(r["utc"])
            except ValueError:
                continue
            has_src = "label_source" in r
            src = r.get("label_source", "") if has_src else ("owner" if r["label"] else "")
            out.append({"device": r["device"], "t": t, "label": r["label"], "source": src, "speaker": r["speaker"],
                        "tier": r.get("tier", ""), "sources": r.get("sources", ""), "table": p.name, "id": r["id"]})
    return out


def read_key(root):
    import csv
    p = Path(root) / "labelq" / "key.csv"
    if not p.exists():
        return {}
    with open(p, newline="", encoding="utf-8") as f:
        return {(r["kind"], r["ref"]): r for r in csv.DictReader(f)}


def scanned_hours(kh):
    """{device: Counter {(day, evening): hours}} of the room audio the miss scan covered (union of the pieces)."""
    iv = defaultdict(list)
    seen = set()
    for ix in SCAN_INDEXES:
        p = Path(kh) / ix
        if not p.exists():
            continue
        for pc in json.loads(p.read_text())["pieces"]:
            if pc["clip"] in seen:
                continue
            seen.add(pc["clip"])
            a = datetime.fromisoformat(pc["start_utc"].replace("Z", "+00:00")).timestamp()
            iv[pc["device"]].append((a, a + pc["samples"] / C.RATE))
    return {d: union_hours(v) for d, v in iv.items()}


# ---------------------------------------------------------------------------------------------
# analysis
# ---------------------------------------------------------------------------------------------


def analyse(root, kh, until=None):
    root = Path(root)
    live = L.read_table(root / "live" / "live_wakes.csv")
    t0 = L.epoch(L.SCOPE_START)
    t1 = until or live_until(root)
    live = [r for r in live if t0 <= L.detection_epoch(r) <= t1]
    devices = sorted({r["device"] for r in live} | {"c3-a", "c3-b"})
    cells = defaultdict(Counter)          # (device, speaker, era, day, evening) -> Counter of measures

    def add(dev, spk, era_, t, what, n=1):
        day, ev = madrid(t)
        cells[(dev, spk, era_, day, ev)][what] += n

    wake_times = defaultdict(list)
    for r in live:
        t = L.detection_epoch(r)
        cls, src = live_class(r)
        e = era(r["model"]) if r["model"] else era(L.model_of(r["device"], t))
        spk = spk_class(r["speaker"])
        if r["trigger"] != "button":
            wake_times[r["device"]].append(t)
        if cls == "call":
            add(r["device"], spk, e, t, "caught")
            add(r["device"], spk, e, t, f"caught_{src}")
        elif cls == "false":
            add(r["device"], "unknown", e, t, "false_wake")
            add(r["device"], "unknown", e, t, f"false_wake_{src}")
        elif cls == "not-a-wake":
            add(r["device"], spk, e, t, "wake_not_a_wake")
        elif cls == "uncertain":
            add(r["device"], spk, e, t, "live_uncertain")
        else:
            add(r["device"], spk, e, t, "button")
            add(r["device"], spk, e, t, cls.replace("-", "_"))
    windows = [w for w in read_windows(root) if t0 <= w["t"] <= t1]
    owner_att = [w for w in windows if w["label"] == "attempt" and w["source"] in OWNER]
    atts = unique_attempts(owner_att)
    n_att_caught = 0
    for a in atts:
        e = era(L.model_of(a["device"], a["t"]))
        if caught_by_live(a, wake_times):
            n_att_caught += 1
            continue
        spk = spk_class(a["speaker"])
        add(a["device"], spk, e, a["t"], "missed")
        if "P4" in a["tiers"]:
            add(a["device"], spk, e, a["t"], "missed_p4")
    # pending windows: no definite label (empty or unclear), no definite label of another table within 3 s
    key = read_key(root)
    definite = defaultdict(list)
    for w in windows:
        if w["label"] and w["label"] != "unclear":
            definite[w["device"]].append(w["t"])
    pend = []
    for w in windows:
        if w["label"] and w["label"] != "unclear":
            continue
        if any(abs(x - w["t"]) <= DEDUP_S for x in definite[w["device"]]):
            continue
        k = key.get(("window", w["id"]))
        if w["label"] == "unclear":
            kind = "owner_unclear"
        elif k is None:
            kind = "no_auto_label"
        elif k["audit"] == "1":
            kind = "audit_auto_noncall"
        elif k["auto_label"] == "attempt":
            kind = "auto_attempt"
        else:
            kind = "auto_unclear"
        pend.append((w, kind))
    for w, kind in pend:
        add(w["device"], "unknown", era(L.model_of(w["device"], w["t"])), w["t"], f"pending_{kind}")
        if kind != "audit_auto_noncall":
            add(w["device"], "unknown", era(L.model_of(w["device"], w["t"])), w["t"], "pending_attempt_like")
    expo = exposure(devices, t0, t1, L.model_of)
    scan = scanned_hours(kh)
    labelled_windows = Counter((w["source"] if w["source"] in OWNER else "auto" if w["source"].startswith("auto") else
                                "none") + "|" + (w["label"] or "pending") for w in windows)
    meta = {"from_utc": L.SCOPE_START, "until_utc": datetime.fromtimestamp(t1, timezone.utc).isoformat(timespec="seconds"),
            "live_runs": len(live), "attempt_windows_owner": len(owner_att), "attempts_unique": len(atts),
            "attempts_with_live_wake_within_10s": n_att_caught, "window_rows": len(windows),
            "window_labels": dict(sorted(labelled_windows.items()))}
    return cells, expo, scan, meta


def total(cells, pred):
    out = Counter()
    for k, v in cells.items():
        if pred(*k):
            out.update(v)
    return out


def hours(expo, pred, evening=None):
    return sum(h for (d, e, day, ev), h in expo.items() if pred(d, e, day) and (evening is None or ev == evening))


def rate_line(c, ev_h, day_h):
    caught, missed, pend = c["caught"], c["missed"], c["pending_attempt_like"]
    n = caught + missed
    lo, hi = clopper_pearson(caught, n)
    worst = caught / (n + pend) if n + pend else math.nan
    evn = ev_h / 6
    fe = c.get("false_wake_evening", 0)
    flo, fhi = poisson_ci(fe, evn) if evn > 0 else (math.nan, math.nan)
    dd = day_h / 24
    blo, bhi = poisson_ci(c["button"], dd) if dd > 0 else (math.nan, math.nan)
    return {"caught": caught, "missed": missed, "missed_p4": c["missed_p4"], "catch_rate": caught / n if n else math.nan,
            "catch_ci": [lo, hi], "pending_attempt_like": pend, "pending_auto_attempt": c["pending_auto_attempt"], "catch_rate_worst": worst,
            "false_wakes": c["false_wake"], "false_wakes_evening": fe, "evenings": round(evn, 3),
            "false_per_evening": fe / evn if evn > 0 else math.nan, "false_per_evening_ci": [flo, fhi],
            "button": c["button"], "days": round(dd, 3), "button_per_day": c["button"] / dd if dd > 0 else math.nan,
            "button_per_day_ci": [blo, bhi], "live_uncertain": c["live_uncertain"],
            "caught_by_source": {s: c[f"caught_{s}"] for s in ("owner", "auto", "none")},
            "false_by_source": {s: c[f"false_wake_{s}"] for s in ("owner", "auto", "none")},
            "pending": {k[8:]: v for k, v in sorted(c.items()) if k.startswith("pending_") and k != "pending_attempt_like"}}


def with_evening(cells):
    """Copy of cells with false_wake_evening filled (false wakes whose detection is in the evening)."""
    out = defaultdict(Counter)
    for k, v in cells.items():
        out[k].update(v)
        if k[4]:
            out[k]["false_wake_evening"] += v["false_wake"]
    return out


def pick(device=None, speaker=None, era_=None, day=None, evening=None):
    """Predicate on a cell key (device, speaker, era, day, evening); None matches anything."""
    want = (device, speaker, era_, day, evening)
    return lambda *k: all(w is None or w == v for w, v in zip(want, k))


def pick_hours(device=None, era_=None, day=None):
    return lambda d, e, dd: (device is None or d == device) and (era_ is None or e == era_) and (day is None or dd == day)


def build(cells, expo, scan, meta):
    cells = with_evening(cells)
    eras = sorted({k[2] for k in cells} | {k[1] for k in expo}, key=lambda e: (e == "none", e))
    days = sorted({k[3] for k in cells} | {k[2] for k in expo})
    devs = sorted({k[0] for k in cells} | {k[0] for k in expo})
    res = {"meta": meta, "by_tv_speaker": {}, "by_tv_era": {}, "by_tv_day": {}, "by_tv": {}}
    for d in devs:
        hp = pick_hours(device=d)
        res["by_tv"][d] = rate_line(total(cells, pick(device=d)), hours(expo, hp, True), hours(expo, hp))
        for s in SPEAKERS:
            c = total(cells, pick(device=d, speaker=s))
            n = c["caught"] + c["missed"]
            res["by_tv_speaker"][f"{d}|{s}"] = {
                "caught": c["caught"], "missed": c["missed"], "missed_p4": c["missed_p4"],
                "catch_rate": c["caught"] / n if n else math.nan, "catch_ci": list(clopper_pearson(c["caught"], n)),
                "caught_by_source": {x: c[f"caught_{x}"] for x in ("owner", "auto", "none")},
                "live_uncertain": c["live_uncertain"]}
        for e in eras:
            c = total(cells, pick(device=d, era_=e))
            hp = pick_hours(device=d, era_=e)
            if not c and hours(expo, hp) == 0:
                continue
            res["by_tv_era"][f"{d}|{e}"] = rate_line(c, hours(expo, hp, True), hours(expo, hp))
        for day in days:
            for ev in (False, True):
                c = total(cells, pick(device=d, day=day, evening=ev))
                h = hours(expo, pick_hours(device=d, day=day), ev)
                if not c and h == 0:
                    continue
                line = rate_line(c, h if ev else 0.0, h)
                line["live_hours"] = round(h, 2)
                line["scanned_room_hours"] = round(scan.get(d, Counter()).get((day, ev), 0.0), 2)
                line["eras"] = sorted({k[1] for k, hh in expo.items() if k[0] == d and k[2] == day and k[3] == ev
                                       and hh > 0.01})
                res["by_tv_day"][f"{d}|{day}|{'evening' if ev else 'rest'}"] = line
    return res


# ---------------------------------------------------------------------------------------------
# rendering
# ---------------------------------------------------------------------------------------------


def pct(x):
    return "-" if x is None or (isinstance(x, float) and math.isnan(x)) else f"{100 * x:.0f} %"


def ci(lo_hi, scale=1.0, fmt="{:.2f}"):
    lo, hi = lo_hi
    if lo is None or (isinstance(lo, float) and math.isnan(lo)):
        return "-"
    return f"{fmt.format(lo * scale)}-{fmt.format(hi * scale)}"


def num(x, fmt="{:.2f}"):
    return "-" if x is None or (isinstance(x, float) and math.isnan(x)) else fmt.format(x)


def render(res):
    m = res["meta"]
    out = ["# \"oye Kivi\" real-use statistics (counts only)", "",
           (f"Generated by `kivi_usage.py report` from the labels as they stood at the run (read fresh, read only). Live data "
           f"{m['from_utc']} to {m['until_utc']}; {m['live_runs']} live runs; window rows {m['window_rows']} "
           f"(labels by source: {', '.join(f'{k} {v}' for k, v in m['window_labels'].items())}); owner-labelled attempt "
           f"windows {m['attempt_windows_owner']} = {m['attempts_unique']} unique attempts per TV within 3 s, of which "
           f"{m['attempts_with_live_wake_within_10s']} had a wake-triggered live run within 10 s (caught, already "
           "counted as live calls)."), "",
           ("Definitions: caught = wake-triggered live run labelled true (owner, trusted auto, older no-source). Missed = "
           "owner-labelled attempt window without a wake-triggered live run within 10 s on that TV (retries are not "
           "evidence). Pending attempt-like = windows without a definite label whose hidden auto label is attempt or "
           "unclear, owner-unclear windows, and older-table rows without a label (they may hide missed calls; their "
           "speaker is unknown); the worst-case catch rate counts all of them as misses. Live uncertain = wake runs "
           "with no trusted label (unlabelled, unclear, uncertain auto). False wakes per evening: evening (18-24 "
           "Madrid) false wakes / (evening hours with that model live / 6). Missed calls can only be found in scanned "
           "room audio (hours listed per day) where some head reached raw 0.3. CI = 95 % (Clopper-Pearson for the "
           "catch rate, exact Poisson for rates)."), "",
           "## Per TV", "",
           ("| TV | caught | missed (P4) | catch rate (95 % CI) | pending attempt-like (auto attempt) | worst case | "
            "live uncertain | false wakes day / evening | evenings | false / evening (95 % CI) | button sessions | "
            "per day (95 % CI) |"),
           "| --- | --- | --- | --- | --- | --- | --- | --- | --- | --- | --- | --- |"]
    for d, r in res["by_tv"].items():
        out.append(f"| {d} | {r['caught']} | {r['missed']} ({r['missed_p4']}) | {pct(r['catch_rate'])} "
                   f"({ci(r['catch_ci'], 100, '{:.0f}')} %) | {r['pending_attempt_like']} ({r['pending_auto_attempt']}) | "
                   f"{pct(r['catch_rate_worst'])} | "
                   f"{r['live_uncertain']} | {r['false_wakes']} / {r['false_wakes_evening']} | {r['evenings']:.2f} | "
                   f"{num(r['false_per_evening'])} ({ci(r['false_per_evening_ci'])}) | {r['button']} | "
                   f"{num(r['button_per_day'])} ({ci(r['button_per_day_ci'])}) |")
    out += ["", "Label sources: " + "; ".join(
        f"{d}: caught owner {r['caught_by_source']['owner']} / auto {r['caught_by_source']['auto']} / no source "
        f"{r['caught_by_source']['none']}, false wakes owner {r['false_by_source']['owner']} / auto "
        f"{r['false_by_source']['auto']} / no source {r['false_by_source']['none']}, pending "
        + (", ".join(f"{k} {v}" for k, v in r["pending"].items()) or "none") for d, r in res["by_tv"].items()) + ".", "",
        "## Per TV and speaker (all eras)", "",
        "| TV | speaker | caught | missed (P4) | catch rate (95 % CI) | caught by source owner / auto / none | live uncertain |",
        "| --- | --- | --- | --- | --- | --- | --- |"]
    for k, r in res["by_tv_speaker"].items():
        d, s = k.split("|")
        cs = r["caught_by_source"]
        out.append(f"| {d} | {s} | {r['caught']} | {r['missed']} ({r['missed_p4']}) | {pct(r['catch_rate'])} "
                   f"({ci(r['catch_ci'], 100, '{:.0f}')} %) | {cs['owner']} / {cs['auto']} / {cs['none']} | "
                   f"{r['live_uncertain']} |")
    out += ["", "Pending windows and false wakes carry no speaker (they are counted under the TV only).", "",
            "## Per TV and model era", "",
            ("| TV | era | caught | missed (P4) | catch rate (95 % CI) | pending attempt-like (auto attempt) | worst case | "
             "live uncertain | false wakes day / evening | evenings | false / evening (95 % CI) | button | "
             "per day (95 % CI) |"),
            "| --- | --- | --- | --- | --- | --- | --- | --- | --- | --- | --- | --- | --- |"]
    for k, r in res["by_tv_era"].items():
        d, e = k.split("|")
        out.append(f"| {d} | {e} | {r['caught']} | {r['missed']} ({r['missed_p4']}) | {pct(r['catch_rate'])} "
                   f"({ci(r['catch_ci'], 100, '{:.0f}')} %) | {r['pending_attempt_like']} ({r['pending_auto_attempt']}) | "
                   f"{pct(r['catch_rate_worst'])} | "
                   f"{r['live_uncertain']} | {r['false_wakes']} / {r['false_wakes_evening']} | {r['evenings']:.2f} | "
                   f"{num(r['false_per_evening'])} ({ci(r['false_per_evening_ci'])}) | {r['button']} | "
                   f"{num(r['button_per_day'])} ({ci(r['button_per_day_ci'])}) |")
    out += ["", "## Per TV and day (evening 18-24 Madrid apart)", "",
            ("| TV | day | part | eras live | live h | scanned room h | caught | missed (P4) | pending attempt-like | "
             "live uncertain | false wakes | false / evening (95 % CI) | button |"),
            "| --- | --- | --- | --- | --- | --- | --- | --- | --- | --- | --- | --- | --- |"]
    for k, r in res["by_tv_day"].items():
        d, day, part = k.split("|")
        fpe = f"{num(r['false_per_evening'])} ({ci(r['false_per_evening_ci'])})" if part == "evening" else ""
        out.append(f"| {d} | {day} | {part} | {', '.join(r['eras'])} | {r['live_hours']} | {r['scanned_room_hours']} | "
                   f"{r['caught']} | {r['missed']} ({r['missed_p4']}) | {r['pending_attempt_like']} | "
                   f"{r['live_uncertain']} | {r['false_wakes']} | {fpe} | {r['button']} |")
    out += ["", ("Re-run after more labels: `$KIVI_HOME/.venv/bin/python ~/homelab/scripts/voice-assistant/wakeword/"
                 "kivi_usage.py report` (it reads every table fresh)."), ""]
    return "\n".join(out)


def cmd_report(a):
    root = Path(a.dataset_root).expanduser()
    until = L.epoch(a.until) if a.until else None
    cells, expo, scan, meta = analyse(root, KH, until)
    res = build(cells, expo, scan, meta)
    out = Path(a.out).expanduser() if a.out else KH / "audit" / "usage"
    C.private_mkdir(out, KH)
    js, md = out / "usage.json", out / "USAGE.md"
    js.write_text(json.dumps(res, indent=1, default=lambda x: None) + "\n")
    md.write_text(render(res))
    for p in (js, md):
        os.chmod(p, 0o600)
    for d, r in res["by_tv"].items():
        print(f"{d}: caught {r['caught']}, missed {r['missed']}, pending attempt-like {r['pending_attempt_like']}, "
              f"false wakes {r['false_wakes']} (evening {r['false_wakes_evening']}), button {r['button']}")
    print(f"wrote {md} and {js}")


def main(argv=None):
    ap = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    ap.add_argument("what", choices=["report"])
    ap.add_argument("--dataset-root", default=C.DEFAULT_DATASET_ROOT)
    ap.add_argument("--until", help="end of the live data (ISO UTC); default: the newest live clip directory")
    ap.add_argument("--out", help="output directory (default $KIVI_HOME/audit/usage)")
    a = ap.parse_args(argv)
    cmd_report(a)


if __name__ == "__main__":
    main()
