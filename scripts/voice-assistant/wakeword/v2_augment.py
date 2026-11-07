#!/usr/bin/env python3
"""Kivi v2 data: interference mixing, satellite front-end emulation, server-exact features.

Three modes, all through the same chain (mix -> level -> front-end -> server-exact features):

- `train`: one (16, 96) feature window per clip, the last 16 embeddings the server computes when the
  word has just ended (word end 0-200 ms before the end of the last embedding's audio).
- `eval`: clip sequences in the `fast_eval.py embed` npz format (each clip after 2.48 s of lead and
  with 1 s before and after it), tagged server-exact, for `fast_report.py`.
- `long`: long WAV files (false accepts per hour), in 1 min pieces as `fast_eval.py embed`.

Mixing (train and eval):
- room impulse response with p `--p-rir` (train only; v1 setting 0.5);
- TV-like interference (`--bg DIR:WEIGHT`, for example AudioSet music/speech/TV and Spanish speech)
  with p `--p-bg` at an SNR drawn from `--snr LO,HI` dB measured against the clip's own RMS (the
  word), continuing through the lead so the noise suppression has adapted before the word;
  no stationary-noise class is used (see v2_prep_bg.py);
- a mic self-noise floor (white, `--floor-db`, default -62 to -48 dBFS in post-gain units before the
  front-end; NS lowers it by 13 dB, so the output floor is -75 to -61 dBFS, the range of the quietest
  80 ms frames of real satellite wake audio): the sensor floor, not interference. Without it the
  clips would contain digital silence, which the server's per-chunk clamp turns into features the
  real satellite never produces;
- level: the mixture is scaled so that the loudest 0.5 s of the word region has a post-gain RMS
  drawn from N(`--level-db` mean, sd) clipped to [-45, -10] dBFS (calibrated on real wake audio);
- front-end: the satellite's own chain (v2_frontend.py: HPF 120 Hz, AEC3 with a silent reference,
  NS, +24 dB, saturating S16), or none (`--frontend none`).

    v2_augment.py train --clips-dir DIR --manifest CSV --out X.npy --bg data/bg_tv:1 \
        --bg data/bg_mls_es:1 --rir-dir data/mit_rirs --frontend aec_offline --stream-onnx MEL,EMB
"""
import argparse
import csv
import glob
import os
import re
import sys

import numpy as np

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

RATE = 16000
TOTAL = 40160      # 31 server chunks: (40160 - 1760) / 1280 + 1
# End of the audio the last of the 16 embeddings sees: a 10 ms burst changes the last embedding up
# to sample 39 360 and not after (measured 2026-11-07). augment_set.py --stream-onnx put the word end
# 0-200 ms before 40 960, which cut up to 100 ms off the word in about half of the positives.
LAST_AUDIO = 39280
LEAD = 32000       # background before the window, so NS has adapted (train)
EVAL_LEAD = 39680  # the fast_eval.py clip lead (31 chunks)


def _read(f):
    import soundfile as sf
    x, _ = sf.read(f, dtype="float32", always_2d=True)
    return x[:, 0]


def _bg_segment(rng, bgs, n):
    b = _read(bgs[rng.integers(len(bgs))])
    if len(b) < n:
        b = np.resize(b, n)
    k = rng.integers(0, len(b) - n + 1)
    return b[k:k + n].astype(np.float64)


def parse_level(text):
    """`MEAN,SD` (normal, clipped to [-45, -10] dBFS) or `u:LO,HI` (uniform, not clipped; v5 quiet sets)."""
    if text.startswith("u:"):
        lo, hi = (float(v) for v in text[2:].split(","))
        return ("u", lo, hi)
    return tuple(float(v) for v in text.split(","))


def _level(rng, mean, sd, hi=None):
    if mean == "u":
        return float(rng.uniform(sd, hi))
    return float(np.clip(rng.normal(mean, sd), -45.0, -10.0))


def _loudest_rms(y, lo, hi):
    seg = y[max(0, lo):hi]
    if len(seg) < 8000:
        return np.sqrt(np.mean(seg ** 2)) + 1e-9
    e = np.convolve(seg ** 2, np.ones(8000) / 8000, "valid")
    return np.sqrt(e.max()) + 1e-9


def mix_clip(rng, x, cfg, total, lead, word_end, snr=None, use_bg=None, rir=True):
    """Returns the float mixture (lead + total samples, post-gain S16 units) and the word span."""
    from scipy.signal import fftconvolve
    if rir and cfg["rirs"] and rng.random() < cfg["p_rir"]:
        h = _read(cfg["rirs"][rng.integers(len(cfg["rirs"]))])
        h = h / (np.max(np.abs(h)) + 1e-9)
        y = fftconvolve(x, h)[: len(x) + 1600]
        x = y * (np.sqrt(np.mean(x ** 2)) / (np.sqrt(np.mean(y ** 2)) + 1e-9))
    n = lead + total
    w = np.zeros(n, np.float64)
    end = lead + word_end
    x = x[-end:]
    w[end - len(x):end] = x
    rms = np.sqrt(np.mean(x ** 2)) + 1e-9
    if use_bg is None:
        use_bg = cfg["bgs"] and rng.random() < cfg["p_bg"]
    if use_bg:
        b = _bg_segment(rng, cfg["bgs"], n)
        s = rng.uniform(*cfg["snr"]) if snr is None else snr
        w = w + b * (rms / (np.sqrt(np.mean(b ** 2)) + 1e-9)) / 10 ** (s / 20)
    target = 32768 * 10 ** (_level(rng, *cfg["level"]) / 20)
    w *= target / _loudest_rms(w, end - len(x), end)
    floor = 32768 * 10 ** (rng.uniform(*cfg["floor"]) / 20)
    w += floor * rng.standard_normal(n)
    return w


def _frontend(cfg):
    if cfg["frontend"] == "none":
        return None
    from v2_frontend import Frontend
    return Frontend(cfg["frontend"], tmpdir=os.environ.get("V2_FE_TMP"))


def _through(fe, ws):
    if fe is None:
        return [np.clip(np.round(w), -32768, 32767).astype(np.int16) for w in ws]
    return fe.process_batch(ws)


def _train_chunk(args):
    files, cfg, seed = args
    rng = np.random.default_rng(seed)
    fe = _frontend(cfg)
    ws = []
    for f in files:
        x = _read(f).astype(np.float64)
        word_end = LAST_AUDIO - int(rng.uniform(0, 0.2) * RATE)
        ws.append(mix_clip(rng, x, cfg, TOTAL, LEAD, word_end))
    out = _through(fe, ws)
    return np.stack([o[LEAD:] for o in out])


def _eval_chunk(args):
    files, cfg, seed, snr = args
    rng = np.random.default_rng(seed)
    fe = _frontend(cfg)
    ws = []
    for f in files:
        x = _read(f).astype(np.float64)
        n = EVAL_LEAD + RATE + len(x) + RATE
        n += -n % 1280 + 1280
        total = n - EVAL_LEAD
        ws.append(mix_clip(rng, x, cfg, total, EVAL_LEAD, RATE + len(x), snr=snr,
                           use_bg=snr is not None, rir=False))
    return _through(fe, ws)


def _long_file(args):
    f, cfg, seed = args
    rng = np.random.default_rng(seed)
    x = _read(f).astype(np.float64) * 32768
    fr = x[: len(x) // 1280 * 1280].reshape(-1, 1280)
    p90 = np.percentile(np.sqrt(np.mean(fr ** 2, axis=1)), 90) + 1e-9
    x = x * (32768 * 10 ** (cfg["long_level"] / 20)) / p90
    x = x + 32768 * 10 ** (np.mean(cfg["floor"]) / 20) * rng.standard_normal(len(x))
    fe = _frontend(cfg)
    return _through(fe, [x])[0]


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("mode", choices=["train", "eval", "long"])
    ap.add_argument("inputs", nargs="*", help="long mode: directories of WAV files")
    ap.add_argument("--clips-dir")
    ap.add_argument("--manifest")
    ap.add_argument("--exclude-regex", default="")
    ap.add_argument("--max-clips", type=int, default=0)
    ap.add_argument("--out", required=True)
    ap.add_argument("--bg", action="append", default=[], help="DIR:WEIGHT (integer duplication)")
    ap.add_argument("--snr", default="0,30")
    ap.add_argument("--p-bg", type=float, default=0.8)
    ap.add_argument("--eval-snr", type=float, help="eval mode: background at this SNR (default: clean)")
    ap.add_argument("--rir-dir")
    ap.add_argument("--p-rir", type=float, default=0.5)
    ap.add_argument("--level-db", default="-23,5", help="mean,sd of the word's loudest-0.5 s RMS, dBFS, or u:LO,HI (uniform)")
    ap.add_argument("--long-level-db", type=float, default=-24.0, help="long mode: p90 80 ms RMS, dBFS")
    ap.add_argument("--floor-db", default="-62,-48")
    ap.add_argument("--frontend", default="none", help="path to aec_offline, or none")
    ap.add_argument("--stream-onnx", required=True)
    ap.add_argument("--jobs", type=int, default=8)
    ap.add_argument("--seed", type=int, default=0)
    a = ap.parse_args()

    bgs = []
    for spec in a.bg:
        d, wgt = spec.rsplit(":", 1)
        bgs += sorted(glob.glob(os.path.join(d, "*.wav"))) * int(wgt)
    cfg = {"bgs": bgs, "snr": tuple(float(v) for v in a.snr.split(",")), "p_bg": a.p_bg,
           "rirs": sorted(glob.glob(os.path.join(a.rir_dir, "*.wav"))) if a.rir_dir else [],
           "p_rir": a.p_rir, "level": parse_level(a.level_db),
           "floor": tuple(float(v) for v in a.floor_db.split(",")), "frontend": a.frontend,
           "long_level": a.long_level_db}
    import multiprocessing as mp
    import torch  # noqa: F401  CUDA libraries for onnxruntime-gpu
    from fast_eval import StreamFeatures, server_exact_tail
    F = StreamFeatures(*a.stream_onnx.split(","))
    rng = np.random.default_rng(a.seed)
    tag = "server-exact"
    meta = f"frontend={'satellite' if a.frontend != 'none' else 'none'}"

    if a.mode == "long":
        files = [f for d in a.inputs for f in sorted(glob.glob(os.path.join(d, "*.wav")))]
        seqs, names = [], []
        with mp.get_context("spawn").Pool(a.jobs) as pool:
            for f, y in zip(files, pool.imap(_long_file, [(f, cfg, a.seed + i) for i, f in enumerate(files)])):
                for k in range(0, len(y), 60 * RATE):
                    if len(y) - k < 2 * RATE:
                        continue
                    piece = np.concatenate([y[k:k + 60 * RATE + 12400], np.zeros(RATE // 2, np.int16)])
                    seqs.append(F._get_embeddings(piece).astype(np.float16))
                    names.append(f"{os.path.basename(f)}@{k // RATE}")
        np.savez(a.out, names=np.array(names), lengths=np.array([len(s) for s in seqs]),
                 data=np.concatenate(seqs), features=np.array(tag), meta=np.array(meta))
        print(a.out, len(seqs), sum(len(s) for s in seqs))
        return

    rx = re.compile(a.exclude_regex) if a.exclude_regex else None
    rows = [r for r in csv.DictReader(open(a.manifest, encoding="utf-8")) if not (rx and rx.search(r["text"]))]
    if a.max_clips and len(rows) > a.max_clips:
        idx = np.sort(rng.choice(len(rows), a.max_clips, replace=False))
        rows = [rows[i] for i in idx]
    files = [os.path.join(a.clips_dir, r["file"]) for r in rows]
    print(f"{a.mode}: {len(files)} clips, {len(bgs)} background entries, frontend {a.frontend}", flush=True)

    if a.mode == "train":
        order = rng.permutation(len(files))
        files = [files[i] for i in order]
        chunks = [(files[k:k + 256], cfg, int(rng.integers(1 << 31))) for k in range(0, len(files), 256)]
        res = []
        with mp.get_context("spawn").Pool(a.jobs) as pool:
            for i, audio in enumerate(pool.imap(_train_chunk, chunks)):
                res.append(server_exact_tail(F, audio))
                if i % 20 == 0:
                    print(f"{(i + 1) * 256}/{len(files)}", flush=True)
        feats = np.concatenate(res).astype(np.float16)
        np.save(a.out, feats)
        with open(a.out + ".files.txt", "w") as fo:
            fo.write("\n".join(os.path.basename(x) for x in files) + "\n")
        print(f"{a.out}: {feats.shape}")
        return

    # eval: sequences per clip, as fast_eval.py embed --clips (one long signal, segments on chunk bounds)
    chunks = [(files[k:k + 128], cfg, a.seed * 7919 + k, a.eval_snr) for k in range(0, len(files), 128)]
    seqs = []
    with mp.get_context("spawn").Pool(a.jobs) as pool:
        for segs in pool.imap(_eval_chunk, chunks):
            bounds, pos = [], 0
            for s in segs:
                bounds.append((pos // 1280, (pos + len(s)) // 1280))
                pos += len(s)
            emb = F._get_embeddings(np.concatenate(segs + [np.zeros(1760, np.int16)]))
            for c0, c1 in bounds:
                # from the first window that ends at the clip's 1 s pre-pad (chunk c0 + 31): the
                # background-only lead is not scored, so it cannot count as a detection
                seqs.append(emb[c0 + 7:max(0, c1 - 9)].astype(np.float16))
    names = [r["file"] + "|" + r.get("voice", "") + "|" + r.get("text", "") for r in rows]
    np.savez(a.out, names=np.array(names), lengths=np.array([len(s) for s in seqs]),
             data=np.concatenate(seqs), features=np.array(tag), meta=np.array(meta))
    print(a.out, len(seqs), sum(len(s) for s in seqs))


if __name__ == "__main__":
    main()
