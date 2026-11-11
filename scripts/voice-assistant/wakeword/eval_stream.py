#!/usr/bin/env python3
"""Offline evaluation of openWakeWord models the way the cluster's Wyoming server runs them.

Uses pyopen-wakeword (the library inside rhasspy/wyoming-openwakeword 2.x, TFLite C API) to stream
16 kHz audio in 80 ms chunks and records every model probability. Detection logic is applied
afterwards as in wyoming_openwakeword/handler.py: a detection when prob > threshold, trigger level 1,
then a refractory time (default 2 s) for that model.

    eval_stream.py probs  --model kivi=kivi.tflite --model alexa=builtin --out CACHE FILE.wav|DIR ...
    eval_stream.py clips  --model ... --clips DIR --manifest M.csv --out RESULT.csv \
                          [--bg-dir DIR --snr-db 10 --seed 1]
    eval_stream.py report --cache CACHE[:category] ... --clips-csv RESULT.csv[:label] ... \
                          --thresholds 0.3,0.5,0.7 [--refractory 2.0] [--json OUT.json]

`probs` writes one .npz per input file (per-step probabilities of each model). `clips` runs each clip
as its own stream (state reset, as on a new Wyoming AudioStart), with 1 s of lead-in and 1 s of tail,
optionally mixed with a random segment of background audio at a given SNR, and stores the maximum
probability per model. `report` turns both into false accepts per hour per category and recall per
voice group at each threshold, with 95 % intervals (exact Poisson, Wilson).
"""
import argparse
import csv
import glob
import json
import math
import multiprocessing as mp
import os
import random
from pathlib import Path

import numpy as np

RATE = 16000
CHUNK = 1280


def load_models(specs):
    from pyopen_wakeword import Model, OpenWakeWord, OpenWakeWordFeatures
    feats = OpenWakeWordFeatures.from_builtin()
    models = {}
    for spec in specs:
        name, path = spec.split("=", 1)
        models[name] = OpenWakeWord.from_builtin(Model(name)) if path == "builtin" else \
            OpenWakeWord.from_model(path)
    return feats, models


FRAME = 160          # the remainder after the last whole chunk is fed in frames of this size (10 ms)


def wake_samples(wake_len_s):
    """Length in samples of a full HA wake file from its `wake_len` (seconds, printed with 0.01 s resolution). The file
    length is a multiple of 160 samples, so the rounding is exact."""
    return int(round(float(wake_len_s) * RATE / FRAME)) * FRAME


def grid_skip(n_audio, n_total=None):
    """Samples to drop from the start of audio that is the last `n_audio` samples of a stream of `n_total` samples, so that
    the rest starts on the server's 1280-sample grid (anchored at sample 0 of the stream). 0 when `n_total` is None."""
    if n_total is None:
        return 0
    return (-(int(n_total) - int(n_audio))) % CHUNK


def stream(feats, models, pcm16, n_total=None):
    """Feed int16 audio the way the Wyoming server does; returns {name: probs array} and step times (s from the start of
    `pcm16`: the end of the audio consumed when the step was formed).

    `n_total`: length in samples of the full stream `pcm16` is the tail of (None: `pcm16` is the stream from sample 0).
    The server's 1280-sample grid is anchored at sample 0 of the stream, so the first `grid_skip` samples of a tail are
    dropped (they belong to a step formed before the tail) and the rest is fed from the grid. Whole 1280-sample chunks
    first, then the remainder in 160-sample frames including the final partial frame: the last step of a file is formed in
    the remainder (HA stops recording 10-20 ms after the detection step), so dropping it loses the live detection."""
    feats.reset()
    for m in models.values():
        m.reset()
    out = {n: [] for n in models}
    times = []
    skip = min(grid_skip(len(pcm16), n_total), len(pcm16))
    n_chunks = (len(pcm16) - skip) // CHUNK
    for i in range(n_chunks):
        k = skip + i * CHUNK
        for emb in feats.process_streaming(pcm16[k:k + CHUNK].tobytes()):
            for n, m in models.items():
                out[n].extend(m.process_streaming(emb))
        times.append((k + CHUNK) / RATE)
    pos = skip + n_chunks * CHUNK
    for k in range(pos, len(pcm16), FRAME):
        end = min(k + FRAME, len(pcm16))
        for emb in feats.process_streaming(pcm16[k:end].tobytes()):
            for n, m in models.items():
                out[n].extend(m.process_streaming(emb))
            times.append(end / RATE)
    # The feature extractor starts with 8 s of zeros ("autofill"), so the first outputs belong to no input chunk; align
    # the newest output with the newest step and drop the surplus at the start.
    n_steps = min([len(times)] + [len(v) for v in out.values()])
    return {n: np.asarray(v[len(v) - n_steps:], dtype=np.float32) for n, v in out.items()}, \
        np.asarray(times[len(times) - n_steps:])


def read16(path):
    import soundfile as sf
    x, sr = sf.read(path, dtype="int16", always_2d=True)
    assert sr == RATE, f"{path}: need 16 kHz"
    return x[:, 0].copy()


_W = {}


def _probs_worker(args):
    path, out_dir, specs = args
    if "m" not in _W:
        _W["m"] = load_models(specs)
    feats, models = _W["m"]
    dst = Path(out_dir) / (Path(path).stem + ".npz")
    if dst.exists():
        return str(dst)
    x = read16(path)
    probs, times = stream(feats, models, x)
    np.savez_compressed(dst, times=times, duration_s=len(x) / RATE, source=str(path),
                        **{f"p_{n}": v for n, v in probs.items()})
    return str(dst)


def cmd_probs(a):
    files = []
    for p in a.inputs:
        files += sorted(glob.glob(os.path.join(p, "*.wav"))) if os.path.isdir(p) else [p]
    Path(a.out).mkdir(parents=True, exist_ok=True)
    tasks = [(f, a.out, a.model) for f in files]
    with mp.get_context("spawn").Pool(a.jobs) as pool:
        for i, _ in enumerate(pool.imap_unordered(_probs_worker, tasks)):
            print(f"\r{i + 1}/{len(tasks)}", end="", flush=True)
    print()


def _clip_worker(args):
    rows, clips_dir, specs, bg_files, snr_db, seed = args
    if "m" not in _W:
        _W["m"] = load_models(specs)
    feats, models = _W["m"]
    rng = random.Random(seed)
    out = []
    for r in rows:
        x = read16(os.path.join(clips_dir, r["file"])).astype(np.float32)
        y = np.concatenate([np.zeros(RATE, np.float32), x, np.zeros(RATE, np.float32)])
        if bg_files:
            bg = read16(rng.choice(bg_files)).astype(np.float32)
            if len(bg) > len(y):
                k = rng.randrange(len(bg) - len(y))
                bg = bg[k:k + len(y)]
            else:
                bg = np.resize(bg, len(y))
            ps = np.mean(x ** 2) + 1e-9
            pb = np.mean(bg ** 2) + 1e-9
            y = y + bg * math.sqrt(ps / pb / 10 ** (snr_db / 10))
            peak = np.max(np.abs(y))
            if peak > 32000:
                y *= 32000 / peak
        probs, _ = stream(feats, models, y.astype(np.int16))
        out.append({**{k: r[k] for k in ("file", "voice", "kind", "text") if k in r},
                    **{f"max_{n}": round(float(v.max()) if len(v) else 0.0, 5) for n, v in probs.items()}})
    return out


def cmd_clips(a):
    rows = list(csv.DictReader(open(a.manifest, encoding="utf-8")))
    bg = sorted(glob.glob(os.path.join(a.bg_dir, "*.wav"))) if a.bg_dir else []
    k = max(1, len(rows) // (a.jobs * 4))
    tasks = [(rows[i:i + k], a.clips, a.model, bg, a.snr_db, f"{a.seed}-{i}") for i in range(0, len(rows), k)]
    res = []
    with mp.get_context("spawn").Pool(a.jobs) as pool:
        for r in pool.imap_unordered(_clip_worker, tasks):
            res += r
            print(f"\r{len(res)}/{len(rows)}", end="", flush=True)
    print()
    res.sort(key=lambda r: r["file"])
    with open(a.out, "w", newline="", encoding="utf-8") as f:
        w = csv.DictWriter(f, fieldnames=list(res[0]))
        w.writeheader()
        w.writerows(res)


def detections(p, times, thr, refractory):
    det, last = [], -1e9
    for i in np.nonzero(p > thr)[0]:
        if times[i] - last >= refractory:
            det.append(float(times[i]))
            last = times[i]
    return det


def poisson_ci(k, t_h):
    from scipy.stats import chi2
    lo = 0.0 if k == 0 else chi2.ppf(0.025, 2 * k) / 2
    hi = chi2.ppf(0.975, 2 * k + 2) / 2
    return lo / t_h, hi / t_h


def wilson(k, n, z=1.96):
    if n == 0:
        return (0.0, 0.0)
    p = k / n
    d = 1 + z * z / n
    c = (p + z * z / (2 * n)) / d
    h = z * math.sqrt(p * (1 - p) / n + z * z / (4 * n * n)) / d
    return (max(0.0, c - h), min(1.0, c + h))


def cmd_report(a):
    thrs = [float(t) for t in a.thresholds.split(",")]
    report = {"thresholds": thrs, "refractory_s": a.refractory, "false_accepts": {}, "clips": {}}
    caches = {}
    for spec in a.cache or []:  # several dirs may share a category (one dir per model)
        d, _, cat = spec.partition(":")
        caches.setdefault(cat or Path(d).name, []).append(d)
    for cat, dirs in caches.items():
        hours, counts, times_by = {}, {}, {}
        for f in sorted(f for d in dirs for f in glob.glob(os.path.join(d, "*.npz"))):
            z = np.load(f)
            for key in z.files:
                if not key.startswith("p_"):
                    continue
                n = key[2:]
                p, tt = z[key], z["times"]
                k = min(len(p), len(tt))
                p, tt = p[len(p) - k:], tt[len(tt) - k:]
                hours[n] = hours.get(n, 0.0) + float(z["duration_s"]) / 3600
                for t in thrs:
                    det = detections(p, tt, t, a.refractory)
                    counts[(n, t)] = counts.get((n, t), 0) + len(det)
                    if det:
                        times_by.setdefault((n, t), []).append((Path(f).stem, det[:20]))
        rows = []
        for (n, t), k in sorted(counts.items()):
            lo, hi = poisson_ci(k, hours[n])
            rows.append({"model": n, "threshold": t, "false_accepts": k, "hours": round(hours[n], 3),
                         "per_hour": round(k / hours[n], 3), "ci95": [round(lo, 3), round(hi, 3)],
                         "where": times_by.get((n, t), [])[:10]})
        report["false_accepts"][cat] = rows
    for spec in a.clips_csv or []:
        path, _, label = spec.partition(":")
        label = label or Path(path).stem
        rs = list(csv.DictReader(open(path, encoding="utf-8")))
        models = [k[4:] for k in rs[0] if k.startswith("max_")]
        out = []
        for n in models:
            for t in thrs:
                groups = {}
                for r in rs:
                    for g in ("all", r.get("voice", "?"), "text:" + r.get("text", "?")):
                        groups.setdefault(g, [0, 0])
                        groups[g][0] += float(r[f"max_{n}"]) > t
                        groups[g][1] += 1
                for g, (k, nn) in groups.items():
                    lo, hi = wilson(k, nn)
                    out.append({"model": n, "threshold": t, "group": g, "k": k, "n": nn,
                                "rate": round(k / nn, 4), "ci95": [round(lo, 4), round(hi, 4)]})
        report["clips"][label] = out
    if a.json:
        json.dump(report, open(a.json, "w"), indent=1, ensure_ascii=False)
    for cat, rows in report["false_accepts"].items():
        for r in rows:
            print(f"FA  {cat:24s} {r['model']:6s} thr {r['threshold']:.2f}: {r['false_accepts']:4d} in "
                  f"{r['hours']:.2f} h = {r['per_hour']:.2f}/h  CI {r['ci95']}")
    for label, rows in report["clips"].items():
        for r in rows:
            if r["group"] == "all":
                print(f"CLIP {label:24s} {r['model']:6s} thr {r['threshold']:.2f}: {r['k']}/{r['n']} = "
                      f"{r['rate']:.3f}  CI {r['ci95']}")


def main():
    ap = argparse.ArgumentParser()
    sp = ap.add_subparsers(dest="cmd", required=True)
    for name in ("probs", "clips"):
        p = sp.add_parser(name)
        p.add_argument("--model", action="append", required=True, help="name=path.tflite or name=builtin")
        p.add_argument("--out", required=True)
        p.add_argument("--jobs", type=int, default=max(1, (os.cpu_count() or 2) - 4))
        if name == "probs":
            p.add_argument("inputs", nargs="+")
        else:
            p.add_argument("--clips", required=True)
            p.add_argument("--manifest", required=True)
            p.add_argument("--bg-dir")
            p.add_argument("--snr-db", type=float, default=10.0)
            p.add_argument("--seed", type=int, default=1)
    p = sp.add_parser("report")
    p.add_argument("--cache", action="append")
    p.add_argument("--clips-csv", action="append")
    p.add_argument("--thresholds", default="0.3,0.4,0.5,0.6,0.7,0.8,0.9")
    p.add_argument("--refractory", type=float, default=2.0)
    p.add_argument("--json")
    a = ap.parse_args()
    {"probs": cmd_probs, "clips": cmd_clips, "report": cmd_report}[a.cmd](a)


if __name__ == "__main__":
    main()
