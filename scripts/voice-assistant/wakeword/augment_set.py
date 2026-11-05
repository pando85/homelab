#!/usr/bin/env python3
"""Augment one clip set and compute its openWakeWord features, optionally leaving out clips by text.

Two modes:
- `oww` (default): openWakeWord's `augment_clips` (what `train.py --augment_clips` does).
- `simple`: own augmentation with the SNR measured on the word itself: clip end-aligned in a 2 s
  window (0-200 ms end jitter), room impulse response with p=0.5, background (AudioSet, MLS Spanish
  speech) with p=0.8 at 0-20 dB SNR against the clip's own RMS, peak -20 to -1 dBFS. The default
  `augment_clips` sets the SNR against the RMS of the whole padded window (-10 to +15 dB), which for a
  0.45 s word leaves many positives buried; models trained on it collapsed (see the experiment doc).

Used to rebuild the adversarial-negative features without the phrases that contain the target
sound itself (for example "aquí vi" = a-kˈi-βi), which are label noise for a "kivi" detector.

    augment_set.py --config $WORK/kivi.rendered.yaml --set negative_train \
        --manifest $WORK/train_out/kivi/negative_train.manifest.csv --exclude-regex '^(y )?aquí vi' \
        --out $WORK/train_out/kivi/negative_features_train.npy
"""
import argparse
import csv
import os
import re

import numpy as np
import yaml


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--config", required=True)
    ap.add_argument("--set", required=True)
    ap.add_argument("--manifest", required=True)
    ap.add_argument("--exclude-regex", default="")
    ap.add_argument("--total-length", type=int, default=32000)
    ap.add_argument("--out", required=True)
    ap.add_argument("--mode", choices=["oww", "simple"], default="oww")
    ap.add_argument("--seed", type=int, default=0)
    ap.add_argument("--snr", default="0,20", help="simple mode: SNR range in dB against the clip RMS")
    ap.add_argument("--p-bg", type=float, default=0.8, help="simple mode: probability of background")
    ap.add_argument("--stream-onnx", help="MEL_ROWMAX.onnx,EMBEDDING.onnx: compute server-exact features "
                                          "(the last 16 embeddings the server would compute at the clip end)")
    ap.add_argument("--jobs", type=int, default=8)
    ap.add_argument("--max-clips", type=int, default=0, help="use a random subset of this many clips")
    a = ap.parse_args()

    import torch
    from openwakeword.data import augment_clips
    from openwakeword.utils import compute_features_from_generator

    cfg = yaml.safe_load(open(a.config))
    clip_dir = os.path.join(cfg["output_dir"], cfg["model_name"], a.set)
    rx = re.compile(a.exclude_regex) if a.exclude_regex else None
    rows = list(csv.DictReader(open(a.manifest, encoding="utf-8")))
    keep = [os.path.join(clip_dir, r["file"]) for r in rows if not (rx and rx.search(r["text"]))]
    if a.max_clips and len(keep) > a.max_clips:
        import random
        keep = sorted(random.Random(a.seed).sample(keep, a.max_clips))
    print(f"{a.set}: {len(keep)} of {len(rows)} clips kept")
    rirs = [e.path for d in cfg["rir_paths"] for e in os.scandir(d)]
    bg = []
    for d, rate in zip(cfg["background_paths"], cfg["background_paths_duplication_rate"]):
        bg += [e.path for e in os.scandir(d)] * rate
    if a.mode == "simple":
        simple(keep, rirs, bg, a)
        return
    gen = augment_clips(keep, total_length=a.total_length, batch_size=cfg["augmentation_batch_size"],
                        background_clip_paths=bg, RIR_paths=rirs)
    compute_features_from_generator(gen, n_total=len(keep), clip_duration=a.total_length, output_file=a.out,
                                    device="gpu" if torch.cuda.is_available() else "cpu", ncpu=1)


def _mix_chunk(args):
    import soundfile as sf
    from scipy.signal import fftconvolve
    files, rirs, bg, total, seed, snr_lo, snr_hi, p_bg = args
    rng = np.random.default_rng(seed)
    out = np.zeros((len(files), total), np.int16)
    for i, f in enumerate(files):
        x, _ = sf.read(f, dtype="float32")
        x = x[-(total - 3200):]
        if rng.random() < 0.5:
            h, _ = sf.read(rirs[rng.integers(len(rirs))], dtype="float32")
            h = h if h.ndim == 1 else h[:, 0]
            h = h / (np.max(np.abs(h)) + 1e-9)
            y = fftconvolve(x, h)[: len(x) + 1600]
            x = y * (np.sqrt(np.mean(x ** 2)) / (np.sqrt(np.mean(y ** 2)) + 1e-9))
        w = np.zeros(total, np.float32)
        end = total - int(rng.uniform(0, 0.2) * 16000)
        x = x[-end:]
        w[end - len(x):end] = x
        rms = np.sqrt(np.mean(x ** 2)) + 1e-9
        if rng.random() < p_bg:
            b, _ = sf.read(bg[rng.integers(len(bg))], dtype="float32")
            b = b if b.ndim == 1 else b[:, 0]
            if len(b) < total:
                b = np.resize(b, total)
            k = rng.integers(0, len(b) - total + 1)
            b = b[k:k + total]
            lo, hi = (snr_lo, snr_hi) if not isinstance(snr_lo, tuple) else \
                (snr_lo if rng.random() < 0.5 else snr_hi)
            snr = rng.uniform(lo, hi)
            w = w + b * (rms / (np.sqrt(np.mean(b ** 2)) + 1e-9)) / 10 ** (snr / 20)
        w = w / (np.max(np.abs(w)) + 1e-9) * 10 ** (rng.uniform(-20, -1) / 20)
        out[i] = (w * 32767).astype(np.int16)
    return out


def simple(files, rirs, bg, a, total=32000):
    import multiprocessing as mp
    if a.stream_onnx:
        from fast_eval import StreamFeatures, server_exact_tail
        F = StreamFeatures(*a.stream_onnx.split(","))
        total = max(a.total_length, 40960)
    else:
        from openwakeword.utils import AudioFeatures
        F = AudioFeatures(device="gpu")
    rng = np.random.default_rng(a.seed)
    files = list(files)
    rng.shuffle(files)
    v = [float(x) for x in a.snr.split(",")]
    # "a,b": one range; "a,b,c,d": two ranges, each chosen with probability 0.5
    lo, hi = (v[0], v[1]) if len(v) == 2 else ((v[0], v[1]), (v[2], v[3]))
    chunks = [(files[k:k + 256], rirs, bg, total, int(rng.integers(1 << 31)), lo, hi, a.p_bg)
              for k in range(0, len(files), 256)]
    res = []
    with mp.get_context("spawn").Pool(a.jobs) as pool:
        for i, audio in enumerate(pool.imap(_mix_chunk, chunks)):
            if a.stream_onnx:
                res.append(server_exact_tail(F, audio))
            else:
                res.append(F.embed_clips(audio, batch_size=256).astype(np.float32))
            print(f"\r{(i + 1) * 256}/{len(files)}", end="", flush=True)
    feats = np.concatenate(res)
    np.save(a.out, feats)
    with open(a.out + ".files.txt", "w") as f:  # row order, for per-text analysis
        f.write("\n".join(os.path.basename(x) for x in files) + "\n")
    print(f"\n{a.out}: {feats.shape}")


if __name__ == "__main__":
    main()
