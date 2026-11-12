#!/usr/bin/env python3
"""Kivi v6.3 step A (2026-11-12): why does c3-b fire more than c3-a? Even days only, counts and levels only.

Three parts, all on Europe/Madrid EVEN days (2026-11-06, -08, -10; 2026-11-12 and every odd day are not read):

    v6p3_diag.py levels    # room level statistics per TV and day: floor (p10 of 50 ms frames), median, p90, p99,
                           # share of frames above -50 / -40 dBFS, from seeded 30 s excerpts of the copied room pieces
    v6p3_diag.py dist      # room score distribution per TV: events (2 s refractory, piece grid, cached embeddings) per
                           # room hour above 0.5 / 0.8 / 0.9 / 0.95 / own threshold for v5, v6, v6.2
    v6p3_diag.py xform     # transform test: the even-day false-wake material and positives scored clean and after a
                           # cheap front-end change (pink noise floor, attenuation, gain, clip-level AGC); TFLite runtime
    v6p3_diag.py report    # DIAG.md from the three JSON files

Material of `xform` (even days only; no audio is played, printed or saved, only scores and levels):
- neg-live: live clips labelled false (owner, owner-button, trusted auto, no source) with trigger `wake`, wake tail plus
  request audio streamed on the server grid (eval_stream.stream with n_total); a hit = the model crosses anywhere;
- neg-win: window clips (6 s) labelled other-speech / tv-media / noise in the five window tables, 4 grid phases;
- neg-room: room events of the cached-embedding pass (v6 or v6.2 head above 0.5, merged within 2 s), minus those within
  3 s of an owner-labelled attempt or flagged by the data-quality ASR check (`phrase`, `kivi_word`, `hotword_only`);
  a 10 s cut ending 2 s after the event, on the server grid when the piece's src_sample0 is known, else 4 phases;
- pos-takes: the 2026-11-08 TV-microphone session takes (c3-a, grandfathered train data), a hit = crossing within
  -0.5..+1.0 s of the word end, 4 phases; pos-live: live clips labelled true (trigger wake), crossing in the recall window
  (tail end -3..+1 s); pos-win: owner-labelled attempt windows (4 phases).
Transforms act on the satellite output (what Home Assistant hears): `pinkL` adds stationary pink noise at L dBFS RMS
(L = -66, -60, -54; comfort noise / a raised floor), `attD` multiplies by -D dB (a quieter capture: c3-a material at c3-b's
floor), `gain10` +10 dB, `agc` scales the clip so that its p95 50 ms frame level is -26 dBFS (gain limited to +-20 dB;
a non-causal clip-level normaliser, i.e. an upper bound of what a real AGC can do).
"""
import argparse
import csv
import json
import multiprocessing as mp
import os
import sys
from collections import defaultdict
from datetime import datetime, timezone
from pathlib import Path
from zoneinfo import ZoneInfo

for _v in ("OMP_NUM_THREADS", "OPENBLAS_NUM_THREADS", "MKL_NUM_THREADS"):
    os.environ.setdefault(_v, "1")
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

import numpy as np  # noqa: E402

import kivi_real_common as C  # noqa: E402

TZ = ZoneInfo("Europe/Madrid")
RATE = 16000
KH = C.kivi_home()
ROOT = KH / "datasets" / "real"
OUT = KH / "v6p3" / "diag"
EVEN_DAYS = ("2026-11-06", "2026-11-08", "2026-11-10")
MODELS = {"v5": ("releases/v5-oye-kivi-2026-11-08/model/oye_kivi_next_uncalibrated", 0.9754),
          "v6": ("releases/v6-oye-kivi-2026-11-11/model/oye_kivi_ctx_uncalibrated", 0.9595),
          "v6.2": ("releases/v6-p2-oye-kivi-2026-11-12/model/oye_kivi_ctx2_uncalibrated", 0.9825)}
WINDOW_TABLES = ("daily_false_wakes.csv", "daily_miss_candidates.csv", "c3_events.csv", "c3_events_r2.csv",
                 "miss_candidates.csv")
FALSE_WIN = ("other-speech", "tv-media", "noise")
TRUSTED = ("owner", "owner-button", "auto-v1", "")
XFORMS = ("clean", "pink-66", "pink-60", "pink-54", "att10", "att16", "gain10", "agc")
PHASES = (0, 320, 640, 960)


def madrid_day(t):
    return datetime.fromtimestamp(t, TZ).date().isoformat()


def utc_epoch(stamp):
    return datetime.strptime(stamp, "%Y%m%dT%H%M%S.%fZ").replace(tzinfo=timezone.utc).timestamp()


def frame_db(x, frame=800):
    f = np.asarray(x, np.float64) / 32768.0
    n = len(f) // frame
    if n == 0:
        return np.zeros(0)
    return 20 * np.log10(np.sqrt((f[:n * frame].reshape(n, frame) ** 2).mean(1)) + 1e-9)


# ------------------------------------------------------------------------------------------------
# levels
# ------------------------------------------------------------------------------------------------


def even_pieces():
    import kivi_replay_mine as K
    out = []
    for st in ("negatives", "negatives-v5era"):
        if not (ROOT / st / "manifest.jsonl").exists():
            continue
        for p in K.read_pieces(ROOT, st):
            if p["part"].startswith("test"):
                continue
            day = madrid_day(p["start"])
            if day in EVEN_DAYS:
                out.append({**p, "day": day, "set": st, "path": str(p["path"])})
    return out


def read_range(path, s0, n):
    with open(path, "rb") as f:
        f.seek(44 + 2 * s0)
        return np.frombuffer(f.read(2 * n), np.int16)


def cmd_levels(a):
    rng = np.random.default_rng(1203)
    pcs = even_pieces()
    acc = defaultdict(list)
    for p in pcs:
        k = max(1, int(p["n"] / RATE / 3600 * a.per_hour))
        for _ in range(k):
            s0 = int(rng.integers(0, max(1, p["n"] - 30 * RATE)))
            db = frame_db(read_range(p["path"], s0, 30 * RATE))
            if len(db) < 100:
                continue
            acc[(p["device"], p["day"])].append([np.percentile(db, 10), np.median(db), np.percentile(db, 90),
                                                 np.percentile(db, 99), (db > -50).mean(), (db > -40).mean()])
    res = {}
    for (dev, day), v in sorted(acc.items()):
        v = np.array(v)
        res[f"{dev}|{day}"] = {"excerpts": len(v), "floor_p10_med": round(float(np.median(v[:, 0])), 1),
                               "floor_iqr": [round(float(np.percentile(v[:, 0], 25)), 1), round(float(np.percentile(v[:, 0], 75)), 1)],
                               "median_med": round(float(np.median(v[:, 1])), 1), "p90_med": round(float(np.median(v[:, 2])), 1),
                               "p99_med": round(float(np.median(v[:, 3])), 1),
                               "frac_above_-50": round(float(v[:, 4].mean()), 3), "frac_above_-40": round(float(v[:, 5].mean()), 3)}
    hours = defaultdict(float)
    for p in pcs:
        hours[f"{p['device']}|{p['day']}"] += p["n"] / RATE / 3600
    for k in res:
        res[k]["hours"] = round(hours[k], 1)
    save("levels.json", res)


# ------------------------------------------------------------------------------------------------
# score distribution (cached embeddings, piece grid)
# ------------------------------------------------------------------------------------------------


def cmd_dist(a):
    import v6_room as R
    idx = json.loads((KH / "v6/room_emb/index_v6p1.json").read_text())["pieces"]
    pieces = [p for p in idx if p["part"] in ("even-v5era", "old-even") and R.local_date(p["start_utc"]) in EVEN_DAYS]
    heads = {n: R.Head(KH / (f + ".onnx")) for n, (f, _) in MODELS.items()}
    levels = (0.5, 0.8, 0.9, 0.95)
    res = defaultdict(lambda: {"hours": 0.0, "evening_h": 0.0})
    for pc in pieces:
        e = np.load(KH / pc["emb"], mmap_mode="r") if not os.path.isabs(pc["emb"]) else np.load(pc["emb"], mmap_mode="r")
        key = f"{pc['device']}|{R.local_date(pc['start_utc'])}"
        res[key]["hours"] += pc["samples"] / RATE / 3600
        res[key]["evening_h"] += sum(R.evening_hours(pc["start_utc"], pc["samples"]).values())
        for n, h in heads.items():
            steps, p = R.piece_probs(h, e)
            t = R.step_time(steps)
            for lv in levels + (MODELS[n][1],):
                name = f"{n}>{'own' if lv == MODELS[n][1] else lv}"
                idx_d = R.detections(t, p, lv)
                ev = sum(1 for i in idx_d if R.is_evening(pc["start_utc"], float(t[i]))[0])
                res[key][name] = res[key].get(name, 0) + len(idx_d)
                res[key][name + "|evening"] = res[key].get(name + "|evening", 0) + ev
    out = {k: {kk: (round(vv, 2) if isinstance(vv, float) else vv) for kk, vv in v.items()} for k, v in sorted(res.items())}
    save("dist.json", out)


# ------------------------------------------------------------------------------------------------
# transform test
# ------------------------------------------------------------------------------------------------


def pink(rng, n):
    w = rng.standard_normal(n + 1024)
    f = np.fft.rfft(w)
    k = np.arange(len(f))
    k[0] = 1
    y = np.fft.irfft(f / np.sqrt(k), len(w))[512:512 + n]
    return y / (np.sqrt(np.mean(y ** 2)) + 1e-12)


def apply_xform(x, name, seed):
    y = np.asarray(x, np.float64)
    if name == "clean":
        return x
    if name.startswith("pink"):
        lv = float(name[4:])
        y = y + pink(np.random.default_rng(seed), len(y)) * 32768.0 * 10 ** (lv / 20)
    elif name.startswith("att"):
        y = y * 10 ** (-float(name[3:]) / 20)
    elif name.startswith("gain"):
        y = y * 10 ** (float(name[4:]) / 20)
    elif name == "agc":
        db = frame_db(x)
        g = -26.0 - (np.percentile(db, 95) if len(db) else -26.0)
        y = y * 10 ** (float(np.clip(g, -20, 20)) / 20)
    return np.clip(np.round(y), -32768, 32767).astype(np.int16)


_W = {}


def _runtime():
    if "rt" not in _W:
        import eval_stream as E
        _W["rt"] = E.load_models([f"{n}={KH / (f + '.tflite')}" for n, (f, _) in MODELS.items()])
    return _W["rt"]


def _audio(item):
    kind = item["src"]
    if kind == "live":
        d = ROOT / "live" / item["device"] / item["utc"]
        tail = C.read_pcm16(d / "wake_tail.wav")
        stt = C.read_pcm16(d / "stt.wav") if (d / "stt.wav").exists() else np.zeros(0, np.int16)
        return np.concatenate([tail, stt]) if len(stt) else tail
    if kind == "win":
        return C.read_pcm16(ROOT / "live-misses" / item["clip"])
    if kind == "room":
        return read_range(item["path"], item["s0"], item["n"]).copy()
    if kind == "take":
        if "takes" not in _W:
            _W["takes"] = np.load(KH / "v6p2/real/takes.npz")["audio"]
        return _W["takes"][item["i"]]
    raise ValueError(kind)


def _task(item):
    import eval_stream as E
    feats, models = _runtime()
    x0 = _audio(item)
    out = {"set": item["set"], "device": item["device"], "id": item["id"]}
    for xf in XFORMS:
        x = apply_xform(x0, xf, item["seed"])
        hits = {n: [] for n in models}
        peaks = {n: [] for n in models}
        for ph in item["phases"]:
            n_total = item.get("n_total")
            if n_total is not None:
                probs, times = E.stream(feats, models, x, n_total=n_total)
            else:
                probs, times = E.stream(feats, models, x[ph:])
                times = times + ph / RATE
            lo, hi = item["win"]
            m = (times >= lo) & (times <= hi)
            for n, p in probs.items():
                pk = float(p[m].max()) if m.any() else 0.0
                peaks[n].append(pk)
                hits[n].append(pk > MODELS[n][1])
        for n in models:
            out[f"{xf}|{n}|hit"] = float(np.mean(hits[n]))
            out[f"{xf}|{n}|peak"] = round(float(np.mean(peaks[n])), 4)
    db = frame_db(x0)
    out["floor_db"] = round(float(np.percentile(db, 10)), 1) if len(db) else None
    out["p95_db"] = round(float(np.percentile(db, 95)), 1) if len(db) else None
    return out


def build_items(a):
    import kivi_live_wakes as L
    items = []
    rng = np.random.default_rng(77)
    # live clips
    for r in L.read_table(ROOT / "live" / "live_wakes.csv"):
        t = L.detection_epoch(r)
        if madrid_day(t) not in EVEN_DAYS or r["trigger"] != "wake" or r["label_source"] not in TRUSTED:
            continue
        if r["label"] not in ("true", "false"):
            continue
        d = ROOT / "live" / r["device"] / r["utc"]
        if not (d / "wake_tail.wav").exists():
            continue
        import eval_stream as E
        n_tail = C.read_pcm16(d / "wake_tail.wav").shape[0]
        n_stt = C.read_pcm16(d / "stt.wav").shape[0] if (d / "stt.wav").exists() else 0
        n_wake = max(E.wake_samples(r["wake_len"] or 0), n_tail)
        t_end = n_tail / RATE
        win = (0.0, 1e9) if r["label"] == "false" else (t_end - 3.0, t_end + 1.0)
        items.append({"set": "neg-live" if r["label"] == "false" else "pos-live", "device": r["device"], "id": r["run_id"],
                      "src": "live", "utc": r["utc"], "n_total": n_wake + n_stt, "phases": (0,), "win": win,
                      "seed": int(rng.integers(1 << 31))})
    # window tables
    attempts = defaultdict(list)
    for name in WINDOW_TABLES:
        p = ROOT / "live-misses" / name
        if not p.exists():
            continue
        for r in csv.DictReader(open(p, encoding="utf-8")):
            t = utc_epoch(r["utc"])
            if r["label"] == "attempt":
                attempts[r["device"]].append(t)
            if madrid_day(t) not in EVEN_DAYS or not r.get("clip") or not (ROOT / "live-misses" / r["clip"]).exists():
                continue
            if r["label"] in FALSE_WIN or r["label"] == "attempt":
                items.append({"set": "neg-win" if r["label"] != "attempt" else "pos-win", "device": r["device"],
                              "id": f"{name}:{r['id']}", "src": "win", "clip": r["clip"], "phases": PHASES,
                              "win": (0.0, 1e9), "seed": int(rng.integers(1 << 31))})
    # room events (cached-embedding pass, v6 / v6.2 heads above 0.5)
    import v6_room as R
    import kivi_replay_mine as K
    flags = asr_flag_times()
    idx = json.loads((KH / "v6/room_emb/index_v6p1.json").read_text())["pieces"]
    grid = {}
    for st in ("negatives", "negatives-v5era"):
        for p in K.read_pieces(ROOT, st):
            grid[p["clip"]] = (str(p["path"]), p["src0"], p["n"])
    heads = {n: R.Head(KH / (MODELS[n][0] + ".onnx")) for n in ("v6", "v6.2")}
    for pc in idx:
        if pc["part"] not in ("even-v5era", "old-even") or R.local_date(pc["start_utc"]) not in EVEN_DAYS:
            continue
        if pc["clip"] not in grid:
            continue
        path, src0, n_piece = grid[pc["clip"]]
        e = np.load(KH / pc["emb"], mmap_mode="r")
        t0 = R._utc(pc["start_utc"]).timestamp()
        ts = []
        for h in heads.values():
            steps, p = R.piece_probs(h, e)
            t = R.step_time(steps)
            ts += [float(t[i]) for i in R.detections(t, p, 0.5)]
        ts.sort()
        merged = []
        for x in ts:
            if not merged or x - merged[-1] > 2.0:
                merged.append(x)
        for x in merged:
            ta = t0 + x
            if any(abs(ta - y) <= 3.0 for y in attempts.get(pc["device"], ())) or \
                    any(abs(ta - y) <= 1.5 for y in flags.get(pc["device"], ())):
                continue
            s_end = int(x * RATE) + 2 * RATE
            s0 = max(0, s_end - 10 * RATE)
            phases = PHASES
            if src0 is not None:
                g = K.grid_phase(src0)
                s0 = g + ((s0 - g) // 1280 + (1 if (s0 - g) % 1280 else 0)) * 1280
                phases = (0,)
            n = min(s_end, n_piece) - s0
            if n < 4 * RATE:
                continue
            items.append({"set": "neg-room", "device": pc["device"], "id": f"{pc['clip']}@{x:.1f}", "src": "room",
                          "path": path, "s0": int(s0), "n": int(n), "phases": phases, "win": (n / RATE - 4.0, 1e9),
                          "seed": int(rng.integers(1 << 31))})
    # session takes (c3-a TV microphone, 2026-11-08)
    z = np.load(KH / "v6p2/real/takes.npz")
    for i, (src, lab, we) in enumerate(zip(z["source"], z["label"], z["we"])):
        if src == "tv-c3a" and lab == "kivi":
            items.append({"set": "pos-takes", "device": "c3-a", "id": f"take{i}", "src": "take", "i": i, "phases": PHASES,
                          "win": (we / RATE - 0.5, we / RATE + 1.0), "seed": int(rng.integers(1 << 31))})
    return items


def asr_flag_times():
    out = defaultdict(list)
    for name in ("hardneg_phrase_flags.csv", "roomneg_phrase_flags.csv"):
        p = KH / "quality" / name
        if not p.exists():
            continue
        for r in csv.DictReader(open(p, encoding="utf-8")):
            if r["flag"] in ("phrase", "kivi_word", "hotword_only"):
                out[r["device"].replace("tv-c3", "c3-")].append(utc_epoch(r["event_id"].split("-", 3)[3]))
    return out


def cmd_xform(a):
    items = build_items(a)
    cnt = defaultdict(int)
    for it in items:
        cnt[f"{it['set']}|{it['device']}"] += 1
    print(json.dumps(cnt, indent=1), flush=True)
    res = []
    with mp.get_context("spawn").Pool(a.jobs) as pool:
        for k, r in enumerate(pool.imap_unordered(_task, items, chunksize=2)):
            res.append(r)
            if k % 50 == 0:
                print(f"{k + 1}/{len(items)}", flush=True)
    save("xform_rows.json", res)
    summ = {}
    for key in sorted({f"{r['set']}|{r['device']}" for r in res}):
        rows = [r for r in res if f"{r['set']}|{r['device']}" == key]
        s = {"n": len(rows), "floor_db_med": float(np.median([r["floor_db"] for r in rows if r["floor_db"] is not None])),
             "p95_db_med": float(np.median([r["p95_db"] for r in rows if r["p95_db"] is not None]))}
        for xf in XFORMS:
            for n in MODELS:
                s[f"{xf}|{n}|hits"] = round(sum(r[f"{xf}|{n}|hit"] for r in rows), 2)
                s[f"{xf}|{n}|peak_med"] = round(float(np.median([r[f"{xf}|{n}|peak"] for r in rows])), 4)
        summ[key] = s
    save("xform.json", summ)


def save(name, obj):
    OUT.mkdir(parents=True, exist_ok=True)
    os.chmod(OUT, 0o700)
    p = OUT / name
    p.write_text(json.dumps(obj, indent=1))
    os.chmod(p, 0o600)
    print(f"-> {p}", flush=True)


# ------------------------------------------------------------------------------------------------
# report
# ------------------------------------------------------------------------------------------------


def cmd_report(a):
    out = ["# v6.3 step A: c3-b diagnosis on even days (counts and levels only)", ""]
    lv = json.loads((OUT / "levels.json").read_text())
    out += ["## Room levels (seeded 30 s excerpts, 50 ms frames, dBFS)", "",
            "| TV, day | hours | excerpts | floor p10 (IQR) | median | p90 | p99 | above -50 | above -40 |",
            "| --- | --- | --- | --- | --- | --- | --- | --- | --- |"]
    for k, v in lv.items():
        out.append(f"| {k} | {v['hours']} | {v['excerpts']} | {v['floor_p10_med']} ({v['floor_iqr'][0]} to {v['floor_iqr'][1]}) | "
                   f"{v['median_med']} | {v['p90_med']} | {v['p99_med']} | {v['frac_above_-50']} | {v['frac_above_-40']} |")
    if (OUT / "dist.json").exists():
        ds = json.loads((OUT / "dist.json").read_text())
        cols = [c for c in next(iter(ds.values())) if ">" in c and "|" not in c]
        out += ["", "## Room events per hour (cached embeddings, piece grid, 2 s refractory; all hours / evening count)", "",
                "| TV, day | hours | " + " | ".join(cols) + " |", "| --- | --- | " + " | ".join("---" for _ in cols) + " |"]
        for k, v in ds.items():
            out.append(f"| {k} | {v['hours']} | " + " | ".join(f"{v[c] / v['hours']:.2f} ({v[c + '|evening']})" for c in cols) + " |")
    if (OUT / "xform.json").exists():
        xs = json.loads((OUT / "xform.json").read_text())
        for n in MODELS:
            out += ["", f"## Transform test, {n} at raw {MODELS[n][1]} (hits; mean over grid phases where the grid is unknown)", "",
                    "| set, TV | n | floor / p95 dB | " + " | ".join(XFORMS) + " |", "| --- | --- | --- | " + " | ".join("---" for _ in XFORMS) + " |"]
            for k, v in xs.items():
                out.append(f"| {k} | {v['n']} | {v['floor_db_med']:.0f} / {v['p95_db_med']:.0f} | "
                           + " | ".join(f"{v[f'{xf}|{n}|hits']:g}" for xf in XFORMS) + " |")
    (OUT / "DIAG.md").write_text("\n".join(out) + "\n")
    os.chmod(OUT / "DIAG.md", 0o600)
    print("\n".join(out))


def main():
    ap = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    ap.add_argument("what", choices=["levels", "dist", "xform", "report"])
    ap.add_argument("--per-hour", type=float, default=2.0, help="levels: excerpts per room hour")
    ap.add_argument("--jobs", type=int, default=6)
    a = ap.parse_args()
    {"levels": cmd_levels, "dist": cmd_dist, "xform": cmd_xform, "report": cmd_report}[a.what](a)


if __name__ == "__main__":
    main()
