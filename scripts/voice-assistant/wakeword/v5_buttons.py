#!/usr/bin/env python3
"""Kivi v5: training takes from a button-take session (`kivi_extract.py --button-takes`, say-then-press).

Writes takes in the `v3_real.py takes` format (int16 audio with 5 s before and 1 s after the word end, plus
labels; `cue` = take number), so `v3_real.py feats` augments them like the session takes. Two outputs:

- `--out` (clean): the session's manifest rows that may train (`read_manifest` + `training_rows`: split
  train, not superseded, not excluded, not `needs_review`). The word span is the row's clip window minus the
  extractor's padding (`--pad`, as `kivi_extract.py --pad`, default 1.0 s), checked against the energy-burst
  span computed here (counts printed).
- `--out-extra` (optional): the other takes of the extraction's take log that are not excluded (the
  `needs_review` takes and the takes with no clip), cut here from the raw wake file. The window is the
  extractor's: max(press - 4 s, previous take's last press + 0.3 s) .. last press + 1 s; bursts of the window's
  audio against the whole file's 10th-percentile 20 ms floor (margin 10 dB, SNR >= 10 dB, merge gap 0.3 s).
  A take is kept only when a burst starts before the press, and then by
    rule 1: first..last burst starting before the press, when that span is <= 2.5 s (the extractor's rule), or
    rule 2: the last burst starting before the press alone, when it is <= 1.2 s (span > 2.5 s otherwise).
  Takes with only bursts after the press, or none, are dropped. Counts per rule are printed and stored.

The audio is re-cut from the stored raw wake file, so the context before the word is the real room (it can hold
the previous take's word when takes are 2-4 s apart). `--holdout N` keeps N of the clean takes out for
validation (random by take, seed), written to `--holdout-file` as {"takes": [[1, take], ...]}.

Scoring of button sessions is `kivi_score.py --button-takes` (the server runtime); this tool only cuts.

    v5_buttons.py --session button-20261107T141100Z-spk02-tv-c3b --out v5/real/s2_takes.npz \
        --out-extra v5/real/s2x_takes.npz --holdout 4 --holdout-file v5/real/s2_holdout.json --seed 2026
"""
import argparse
import json
import os
import sys
from datetime import datetime
from pathlib import Path

import numpy as np

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import kivi_real_common as C  # noqa: E402

RATE = C.RATE
PRE, POST = 5 * RATE, RATE
KEYS = ("audio", "we", "wl", "source", "label", "word", "block", "cue", "session", "stage", "rule")


def _ts(s):
    return datetime.fromisoformat(s.replace("Z", "+00:00"))


def extraction(sdir, revision):
    recs = []
    for f in sorted(Path(sdir).glob("extract-*.json")):
        d = json.loads(f.read_text(encoding="utf-8"))
        if d.get("takes_log") and (d.get("revision") or "") == revision:
            recs.append(d)
    if not recs:
        raise SystemExit(f"{sdir}: no extraction record of revision {revision!r} with a take log")
    return recs[-1]


def take_windows(ex, before=4.0, after=1.0, min_gap=0.3):
    """{take: (raw file name, window start s, window end s, first press s)} in each raw file's own time."""
    out = {}
    for t in ex["takes_log"]:
        p, pl = _ts(t["press_utc"]), _ts(t["last_press_utc"])
        f = [r for r in ex["raw_files"] if _ts(r["t0_utc"]) <= p]
        if not f:
            continue
        r = f[-1]
        t0 = _ts(r["t0_utc"])
        rel, rel_last = (p - t0).total_seconds(), (pl - t0).total_seconds()
        lo = rel - before
        if t["prev_last_press_utc"]:
            lo = max(lo, (_ts(t["prev_last_press_utc"]) - t0).total_seconds() + min_gap)
        out[int(t["take"])] = (r["name"], lo, rel_last + after, rel)
    return out


def floor_db(x, pct=10.0):
    n = int(0.02 * RATE)
    f = x[:len(x) // n * n].astype(np.float32).reshape(-1, n) / 32768.0
    return float(np.percentile(20 * np.log10(np.sqrt((f ** 2).mean(axis=1)) + 1e-7), pct))


def burst_span(x, lo, hi, press, floor, max_word_s=2.5, single_max_s=1.2):
    """(t0, t1, rule) of the word, or (None, reason)."""
    a, b = max(0, int(lo * RATE)), min(len(x), int(hi * RATE))
    segs = C.energy_segments(x[a:b], a / RATE, margin_db=10.0, min_len_s=0.15, max_len_s=max_word_s,
                             merge_gap_s=0.3, floor_db=floor)
    c = [s for s in segs if not s["long"] and lo - 0.1 <= s["t0"] <= hi and s["snr_db"] >= 10.0]
    pre = [s for s in c if s["t0"] <= press]
    if not pre:
        return None, ("after_press_only" if c else "no_burst")
    t0, t1 = min(s["t0"] for s in pre), max(s["t1"] for s in pre)
    if t1 - t0 <= max_word_s:
        return (t0, t1, 1), None
    last = max(pre, key=lambda s: s["t0"])
    if last["t1"] - last["t0"] <= single_max_s:
        return (last["t0"], last["t1"], 2), None
    return None, "span_too_long"


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--dataset", default=C.DEFAULT_DATASET_ROOT)
    ap.add_argument("--session", required=True, help="button session id inside the dataset")
    ap.add_argument("--revision", default="stp3", help="extraction revision of the rows and the take log")
    ap.add_argument("--phrase", default="kivi")
    ap.add_argument("--pad", type=float, default=1.0, help="the extractor's --pad around the word")
    ap.add_argument("--out", required=True)
    ap.add_argument("--out-extra")
    ap.add_argument("--holdout", type=int, default=0)
    ap.add_argument("--holdout-file")
    ap.add_argument("--seed", type=int, default=2026)
    a = ap.parse_args()

    root = Path(a.dataset)
    sdir = root / a.session
    session = json.loads((sdir / "session.json").read_text(encoding="utf-8"))
    ex = extraction(sdir, a.revision)
    win = take_windows(ex)
    s_t0 = _ts(session["t0_utc"])
    all_rows = [r for r in C.read_manifest(root) if r["session"] == a.session and (r.get("revision") or "") == a.revision]
    clean = {int(r["cue_index"]): r for r in C.training_rows(all_rows, phrase=a.phrase)}
    excluded = {int(r["cue_index"]) for r in all_rows if r.get("excluded")}
    tv = ex.get("tv_source") or session.get("tv_source")
    raws = {}

    def raw(name):
        if name not in raws:
            x = C.read_pcm16(sdir / tv / "raw" / name)
            raws[name] = (x, floor_db(x), (_ts(next(f["t0_utc"] for f in ex["raw_files"] if f["name"] == name)) - s_t0).total_seconds())
        return raws[name]

    outs = {"clean": {k: [] for k in KEYS}, "extra": {k: [] for k in KEYS}}
    st = {"takes": len(win), "excluded": sorted(excluded), "clean_rows": len(clean), "clean_kept": 0,
          "clean_burst_end_within_50ms": 0, "clean_burst_end_absdiff_max_s": 0.0, "extra_candidates": 0,
          "extra_rule1": [], "extra_rule2": [], "extra_dropped": {}}

    def add(grp, take, x, w0, w1, rule, src):
        we = int(round(w1 * RATE))
        if we - PRE < 0 or we + POST > len(x):
            st.setdefault("no_context", []).append(take)
            return
        o = outs[grp]
        for k, v in (("audio", x[we - PRE:we + POST].astype(np.int16)), ("we", PRE), ("wl", int(round((w1 - w0) * RATE))),
                     ("source", src), ("label", "kivi"), ("word", a.phrase), ("block", 1), ("cue", take),
                     ("session", a.session), ("stage", ""), ("rule", rule)):
            o[k].append(v)

    for take, (name, lo, hi, press) in sorted(win.items()):
        if take in excluded:
            continue
        x, fl, f0 = raw(name)
        g, why = burst_span(x, lo, hi, press, fl)
        if take in clean:
            r = clean[take]
            w0, w1 = r["start_s"] - f0 + a.pad, r["end_s"] - f0 - a.pad
            if g is not None:
                d = abs(g[1] - w1)
                st["clean_burst_end_within_50ms"] += d <= 0.05
                st["clean_burst_end_absdiff_max_s"] = round(max(st["clean_burst_end_absdiff_max_s"], d), 3)
            add("clean", take, x, w0, w1, 0, r["source"])
            st["clean_kept"] += 1
            continue
        st["extra_candidates"] += 1
        if g is None:
            st["extra_dropped"].setdefault(why, []).append(take)
            continue
        st[f"extra_rule{g[2]}"].append(take)
        add("extra", take, x, g[0], g[1], g[2], tv)

    for grp, path in (("clean", a.out), ("extra", a.out_extra)):
        if not path:
            continue
        Path(path).parent.mkdir(parents=True, exist_ok=True)
        os.chmod(Path(path).parent, 0o700)
        np.savez(path, **{k: np.array(v) for k, v in outs[grp].items()})
        os.chmod(path, 0o600)
        wl = np.array(outs[grp]["wl"]) / RATE
        st[f"{grp}_word_span_median_s"] = round(float(np.median(wl)), 2) if len(wl) else None
    if a.holdout:
        rng = np.random.default_rng(a.seed)
        hold = sorted(int(t) for t in rng.choice(sorted(outs["clean"]["cue"]), a.holdout, replace=False))
        json.dump({"takes": [[1, t] for t in hold], "rule": f"{a.holdout} of the {st['clean_kept']} clean takes, random "
                   f"by take (seed {a.seed}); session {a.session}, revision {a.revision}"}, open(a.holdout_file, "w"), indent=1)
        st["holdout"] = hold
    print(json.dumps(st))
    if a.out:
        json.dump(st, open(str(a.out) + ".stats.json", "w"), indent=1)


if __name__ == "__main__":
    main()
