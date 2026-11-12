#!/usr/bin/env python3
"""Kivi v6 data: the v4/v5 augmentation chain with the audit's level and floor changes, and windows cut from clips
with context ("...words... oye Kivi, <command>") at given points.

Changes against v4_augment.py (data audit 2026-11-11, section 8.5/8.6), applied by replacing `v4_augment._level` at
import time (also in the spawn workers, which import this module as __mp_main__):
- word level (loudest 0.5 s, post-gain dBFS): with p LOUD[0] uniform in [LOUD[1], LOUD[2]] (the owner's TV speech,
  -22..-14), with p QUIET[0] uniform in [QUIET[1], QUIET[2]] (quiet c3-b wakes down to about -47), else N(mean, sd)
  clipped to [-52, -10] as v4/v5;
- the mic floor draw is a CLI value (`--floor-db`), v6 uses -62,-40 (post-NS -75..-53: covers c3-a's -54..-57 floor
  as well as c3-b's -75).

Modes:
- `synth`: v4_augment.py `train` (one window per clip, word end 0-200 ms before the last embedding's audio) on a
  filtered manifest: `--drop-file` names, `--max-dur` seconds, `--drop-voice` voices. Other flags pass through.
- `ctx`: windows at explicit cut points of longer clips (a JSONL cut table, one clip per line:
  {"audio": [wav, ...] concatenated, "tv": bool, "cuts": [[sample, group, copies], ...]}). A cut is the sample that
  becomes the end of the last embedding's audio (LAST_AUDIO), so the window holds what the server has heard when it
  scores that moment. Raw synthetic clips (tv false) go through the full chain (RIR, interference, level, floor,
  satellite front end) with `x[:cut]`; real satellite clips (tv true, already through the front end) get a gain and,
  with p `--p-bg-tv`, TV-like interference passed alone through the front end and added (v3_real.py's tv branch);
  with p `--p-tv-floor` (v6.3, default 0) stationary pink noise at U(`--tv-floor-db`) dBFS RMS is added after the gain
  (a raised floor, as on c3-a, for clips from a very quiet capture such as c3-b's).
  Output: <prefix>_<group>.npy (n, 16, 96) float16 per group plus .sources.txt (clip id per window).

    v6_augment.py synth --clips-dir C --manifest M --out X.npy --max-dur 1.5 --drop-file zeros.txt ... (v4 flags)
    v6_augment.py ctx --table cuts.jsonl --out-prefix v6/feat/ctx --frontend FE --stream-onnx MEL,EMB --bg ...

Aggregates only; nothing is played or printed but counts.
"""
import csv
import json
import os
import sys
import tempfile

import numpy as np

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import v4_augment as A  # noqa: E402

LOUD = (0.25, -22.0, -14.0)
QUIET = (0.30, -48.0, -30.0)


def level_v6(rng, mean, sd, quiet=None):
    u = rng.random()
    if u < LOUD[0]:
        return float(rng.uniform(LOUD[1], LOUD[2]))
    if u < LOUD[0] + QUIET[0]:
        return float(rng.uniform(QUIET[1], QUIET[2]))
    return float(np.clip(rng.normal(mean, sd), -52.0, -10.0))


A._level = level_v6


def cmd_synth(argv):
    import argparse
    ap = argparse.ArgumentParser()
    ap.add_argument("--manifest", required=True)
    ap.add_argument("--max-dur", type=float, default=0.0)
    ap.add_argument("--drop-file", action="append", default=[], help="text file of clip file names to leave out")
    ap.add_argument("--drop-voice", action="append", default=[])
    ap.add_argument("--keep-list", help="write the kept file names here")
    a, rest = ap.parse_known_args(argv)
    drop = set()
    for f in a.drop_file:
        drop |= {line.strip() for line in open(f) if line.strip()}
    rows = list(csv.DictReader(open(a.manifest, encoding="utf-8")))
    kept, why = [], {"listed": 0, "duration": 0, "voice": 0}
    for r in rows:
        if r["file"] in drop:
            why["listed"] += 1
        elif a.max_dur and float(r["duration_s"]) > a.max_dur:
            why["duration"] += 1
        elif r["voice"] in a.drop_voice:
            why["voice"] += 1
        else:
            kept.append(r)
    print(f"synth: {len(kept)} of {len(rows)} kept; dropped {why}", flush=True)
    tmp = tempfile.NamedTemporaryFile("w", suffix=".csv", delete=False, encoding="utf-8")
    w = csv.DictWriter(tmp, fieldnames=list(rows[0]))
    w.writeheader()
    w.writerows(kept)
    tmp.close()
    if a.keep_list:
        open(a.keep_list, "w").write("\n".join(r["file"] for r in kept) + "\n")
    sys.argv = [sys.argv[0], "train", "--manifest", tmp.name] + rest
    try:
        A.main()
    finally:
        os.unlink(tmp.name)


# ------------------------------------------------------------------------------------------------
# ctx: windows at cut points
# ------------------------------------------------------------------------------------------------

def _load(paths):
    import soundfile as sf
    xs = []
    for p in paths:
        x, sr = sf.read(p, dtype="int16", always_2d=True)
        assert sr == A.RATE, p
        xs.append(x[:, 0])
    return np.concatenate(xs).astype(np.float64)


def _tv_window(rng, x, cut, cfg):
    """Real satellite audio: [cut - LAST_AUDIO - LEAD, cut + TOTAL - LAST_AUDIO), mirrored at the start when the clip
    is shorter, zeros after its end (those samples never reach the last embedding)."""
    lo = cut - A.LAST_AUDIO - A.LEAD
    hi = cut + A.TOTAL - A.LAST_AUDIO
    seg = x[max(0, lo):min(len(x), hi)]
    if lo < 0:
        seg = np.pad(seg, (-lo, 0), mode="symmetric")
    if hi > len(x):
        seg = np.concatenate([seg, np.zeros(hi - len(x))])
    y = seg * 10 ** (rng.uniform(*cfg["tv_gain"]) / 20)
    if cfg.get("p_tv_floor", 0.0) > 0 and rng.random() < cfg["p_tv_floor"]:
        y = y + pink_noise(rng, len(y)) * 32768.0 * 10 ** (rng.uniform(*cfg["tv_floor"]) / 20)
    return y


def pink_noise(rng, n):
    """Unit-RMS pink (1/f power) noise of n samples."""
    w = rng.standard_normal(n + 1024)
    f = np.fft.rfft(w)
    k = np.arange(len(f), dtype=np.float64)
    k[0] = 1.0
    y = np.fft.irfft(f / np.sqrt(k), len(w))[512:512 + n]
    return y / (np.sqrt(np.mean(y ** 2)) + 1e-12)


def _ctx_chunk(args):
    items, cfg = args
    from v2_frontend import Frontend
    fe = Frontend(cfg["frontend"], tmpdir=os.environ.get("V2_FE_TMP"))
    raw, raw_i, tv, bg, bg_i = [], [], {}, [], []
    cache = {}
    for n, (paths, is_tv, cut, seed) in enumerate(items):
        key = tuple(paths)
        if key not in cache:
            cache = {key: _load(paths)}
        x = cache[key]
        rng = np.random.default_rng(seed)
        if is_tv:
            y = _tv_window(rng, x, cut, cfg)
            tv[n] = y
            if cfg["bgs"] and rng.random() < cfg["p_bg_tv"]:
                b = A._bg_segment(rng, cfg["bgs"], len(y))
                end = A.LEAD + A.LAST_AUDIO
                w = y[end - 8000:end]
                s = rng.uniform(*cfg["snr"])
                bg.append(b * ((np.sqrt(np.mean(w ** 2)) + 1e-9) / (np.sqrt(np.mean(b ** 2)) + 1e-9)) / 10 ** (s / 20))
                bg_i.append(n)
        else:
            raw.append(A.mix_clip(rng, x[:cut], cfg, A.TOTAL, A.LEAD, A.LAST_AUDIO))
            raw_i.append(n)
    out = [None] * len(items)
    if raw:
        for n, y in zip(raw_i, A._through(fe, raw)):
            out[n] = y[A.LEAD:]
    bgf = dict(zip(bg_i, A._through(fe, bg))) if bg else {}
    for n, y in tv.items():
        if n in bgf:
            y = y + bgf[n].astype(np.float64)
        out[n] = np.clip(np.round(y[A.LEAD:]), -32768, 32767).astype(np.int16)
    return np.stack(out)


def cmd_ctx(argv):
    import argparse
    import glob
    import multiprocessing as mp
    ap = argparse.ArgumentParser()
    ap.add_argument("--table", required=True)
    ap.add_argument("--out-prefix", required=True)
    ap.add_argument("--bg", action="append", default=[])
    ap.add_argument("--snr", default="10,30")
    ap.add_argument("--p-bg", type=float, default=0.5)
    ap.add_argument("--p-bg-tv", type=float, default=0.4)
    ap.add_argument("--rir-dir")
    ap.add_argument("--p-rir", type=float, default=0.5)
    ap.add_argument("--level-db", default="-23,5")
    ap.add_argument("--floor-db", default="-62,-40")
    ap.add_argument("--tv-gain-db", default="-6,6")
    ap.add_argument("--tv-floor-db", default="-66,-54")
    ap.add_argument("--p-tv-floor", type=float, default=0.0)
    ap.add_argument("--frontend", required=True)
    ap.add_argument("--stream-onnx", required=True)
    ap.add_argument("--jobs", type=int, default=6)
    ap.add_argument("--seed", type=int, default=0)
    a = ap.parse_args(argv)
    import torch  # noqa: F401
    from fast_eval import StreamFeatures, server_exact_tail
    bgs = []
    for spec in a.bg:
        d, wgt = spec.rsplit(":", 1)
        bgs += sorted(glob.glob(os.path.join(d, "*.wav"))) * int(wgt)
    cfg = {"bgs": bgs, "snr": tuple(float(v) for v in a.snr.split(",")), "p_bg": a.p_bg, "p_bg_tv": a.p_bg_tv,
           "rirs": sorted(glob.glob(os.path.join(a.rir_dir, "*.wav"))) if a.rir_dir else [], "p_rir": a.p_rir,
           "level": tuple(float(v) for v in a.level_db.split(",")), "quiet": (0.0, -45.0, -30.0),
           "floor": tuple(float(v) for v in a.floor_db.split(",")), "frontend": a.frontend,
           "tv_gain": tuple(float(v) for v in a.tv_gain_db.split(",")),
           "tv_floor": tuple(float(v) for v in a.tv_floor_db.split(",")), "p_tv_floor": a.p_tv_floor}
    rng = np.random.default_rng(a.seed)
    groups = {}
    for line in open(a.table, encoding="utf-8"):
        c = json.loads(line)
        for cut, grp, copies in c["cuts"]:
            for _ in range(int(copies)):
                groups.setdefault(grp, []).append((c["audio"], bool(c["tv"]), int(cut), int(rng.integers(1 << 31)),
                                                   c.get("id", "")))
    F = StreamFeatures(*a.stream_onnx.split(","))
    for grp, its in sorted(groups.items()):
        # keep the items of one clip together (one file load per chunk); chunks are shuffled in training anyway
        its.sort(key=lambda t: (tuple(t[0]), t[2]))
        chunks = [([(p, tv, cut, s) for p, tv, cut, s, _ in its[k:k + 128]], cfg) for k in range(0, len(its), 128)]
        res = []
        with mp.get_context("spawn").Pool(a.jobs) as pool:
            for audio in pool.imap(_ctx_chunk, chunks):
                res.append(server_exact_tail(F, audio))
        X = np.concatenate(res).astype(np.float16)
        out = f"{a.out_prefix}_{grp}.npy"
        np.save(out, X)
        os.chmod(out, 0o600)
        with open(out + ".sources.txt", "w") as fo:
            fo.write("\n".join(t[4] for t in its) + "\n")
        print(out, X.shape, f"from {len({t[4] for t in its})} clips", flush=True)


def main():
    mode = sys.argv[1] if len(sys.argv) > 1 else ""
    if mode == "synth":
        cmd_synth(sys.argv[2:])
    elif mode == "ctx":
        cmd_ctx(sys.argv[2:])
    else:
        sys.exit(__doc__)


if __name__ == "__main__":
    main()
