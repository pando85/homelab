#!/usr/bin/env python3
"""Kivi v4 ("oye Kivi"): real household takes of OTHER phrases as negative training windows.

Rows come from the manifest at the current tools (`kivi_real_common.read_manifest`, so the later split
decisions and superseded marks apply) through `negative_rows_for_phrase(rows, "oye kivi")`: the
"kivi" takes, the near-word takes and the session background segments of every train / train-only
session and source; superseded rows and the test session never enter. `--include-needs-review
SESSION` also takes that session's current (not superseded) rows flagged `needs_review`: for a
different phrase a loose cut still holds no "oye Kivi", so it is a valid negative even though it is
not a valid positive for "kivi".

Every row's clip becomes `--copies` augmented signals, as v3_real.py does for real takes:
- tv-* sources (already through the satellite's front end): gain `--tv-gain-db`, and TV-like
  interference at `--snr` on the clip with p `--p-bg-tv`, the interference alone passed through the
  front-end emulation and then added;
- pc-mic / webcam-mic: RIR with p 0.5, interference with p `--p-bg`, level (loudest 0.5 s ~
  N(-23, 5) dBFS, or uniform in the quiet range with p `--quiet`), the calibrated mic floor, then the
  front-end emulation.
A mirrored copy of the clip's first `--lead` seconds is put before it, so the server's feature
pipeline and NS have context without a jump to silence. Server-exact embeddings of the whole signal;
every 16-embedding window that ends inside the clip, with stride `--stride` (cues) or
`--stride-seg` (background segments), becomes a negative. One .npy per group:
<prefix>_kivi.npy (the "kivi" takes), <prefix>_near.npy (near-words), <prefix>_bg.npy (segments),
each with a .sources.txt (session prefix and source per window). Aggregates only; nothing is played.

    v4_realneg.py --out-prefix v4/real/neg --include-needs-review button-20261107T141100Z-spk02-tv-c3b \
        --bg data/bg_tv:3 --bg data/bg_mls_es:2 --rir-dir data/mit_rirs --frontend FE --stream-onnx MEL,EMB
"""
import argparse
import collections
import glob
import json
import os
import sys
from pathlib import Path

import numpy as np

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import kivi_real_common as C  # noqa: E402

RATE = 16000


def _cfg(a):
    bgs = []
    for spec in a.bg:
        d, wgt = spec.rsplit(":", 1)
        bgs += sorted(glob.glob(os.path.join(d, "*.wav"))) * int(wgt)
    return {"bgs": bgs, "snr": tuple(float(v) for v in a.snr.split(",")), "p_bg": a.p_bg, "p_bg_tv": a.p_bg_tv,
            "rirs": sorted(glob.glob(os.path.join(a.rir_dir, "*.wav"))) if a.rir_dir else [], "p_rir": 0.5,
            "level": (-23.0, 5.0), "quiet": tuple(float(v) for v in a.quiet.split(",")),
            "tv_gain": tuple(float(v) for v in a.tv_gain_db.split(",")),
            "floor": (-62.0, -48.0), "frontend": a.frontend, "lead": int(a.lead * RATE)}


def _loudest(y):
    if len(y) <= 8000:
        return np.sqrt(np.mean(y ** 2)) + 1e-9
    c = np.cumsum(y ** 2)
    return np.sqrt(((c[8000:] - c[:-8000]) / 8000).max()) + 1e-9


def _aug_row(args):
    """All copies of one clip -> list of int16 signals (lead + clip)."""
    x, is_tv, n, cfg, seed = args
    from scipy.signal import fftconvolve
    from v2_frontend import Frontend
    from v4_augment import _bg_segment, _level, _read
    rng = np.random.default_rng(seed)
    fe = Frontend(cfg["frontend"], tmpdir=os.environ.get("V2_FE_TMP"))
    L = min(cfg["lead"], len(x))
    base = np.concatenate([x[:L][::-1], x]).astype(np.float64)
    tv_out, desk, bg_mix, bg_idx = {}, [], [], []
    for k in range(n):
        seg = base.copy()
        if is_tv:
            seg *= 10 ** (rng.uniform(*cfg["tv_gain"]) / 20)
            tv_out[k] = seg
            if cfg["bgs"] and rng.random() < cfg["p_bg_tv"]:
                b = _bg_segment(rng, cfg["bgs"], len(seg))
                r = _loudest(seg[L:])
                bg_mix.append(b * (r / (np.sqrt(np.mean(b ** 2)) + 1e-9)) / 10 ** (rng.uniform(*cfg["snr"]) / 20))
                bg_idx.append(k)
            continue
        if cfg["rirs"] and rng.random() < cfg["p_rir"]:
            h = _read(cfg["rirs"][rng.integers(len(cfg["rirs"]))])
            h = h / (np.max(np.abs(h)) + 1e-9)
            j = int(np.argmax(np.abs(h)))
            y = fftconvolve(seg, h)[j:j + len(seg)]
            seg = y * (np.sqrt(np.mean(seg ** 2)) / (np.sqrt(np.mean(y ** 2)) + 1e-9))
        if cfg["bgs"] and rng.random() < cfg["p_bg"]:
            b = _bg_segment(rng, cfg["bgs"], len(seg))
            r = _loudest(seg[L:])
            seg = seg + b * (r / (np.sqrt(np.mean(b ** 2)) + 1e-9)) / 10 ** (rng.uniform(*cfg["snr"]) / 20)
        seg = seg * 32768 * 10 ** (_level(rng, *cfg["level"], quiet=cfg["quiet"]) / 20) / _loudest(seg[L:])
        seg = seg + 32768 * 10 ** (rng.uniform(*cfg["floor"]) / 20) * rng.standard_normal(len(seg))
        desk.append((k, seg))
    out = [None] * n
    if desk:
        for (k, _), y in zip(desk, fe.process_batch([s for _, s in desk])):
            out[k] = y
    bg_fe = dict(zip(bg_idx, fe.process_batch(bg_mix))) if bg_mix else {}
    for k, y in tv_out.items():
        if k in bg_fe:
            y = y + bg_fe[k].astype(np.float64)
        out[k] = np.clip(np.round(y), -32768, 32767).astype(np.int16)
    return L, out


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--dataset", default=C.DEFAULT_DATASET_ROOT)
    ap.add_argument("--phrase", default="oye kivi")
    ap.add_argument("--include-needs-review", action="append", default=[], metavar="SESSION")
    ap.add_argument("--copies", type=int, default=30)
    ap.add_argument("--copies-seg", type=int, default=6)
    ap.add_argument("--stride", type=int, default=2)
    ap.add_argument("--stride-seg", type=int, default=6)
    ap.add_argument("--lead", type=float, default=1.5)
    ap.add_argument("--bg", action="append", default=[])
    ap.add_argument("--snr", default="10,30")
    ap.add_argument("--p-bg", type=float, default=0.5)
    ap.add_argument("--p-bg-tv", type=float, default=0.4)
    ap.add_argument("--quiet", default="0.3,-45,-30")
    ap.add_argument("--tv-gain-db", default="-12,6")
    ap.add_argument("--rir-dir")
    ap.add_argument("--frontend", required=True)
    ap.add_argument("--stream-onnx", required=True)
    ap.add_argument("--jobs", type=int, default=8)
    ap.add_argument("--seed", type=int, default=401)
    ap.add_argument("--out-prefix", required=True)
    a = ap.parse_args()

    import multiprocessing as mp
    import torch  # noqa: F401  CUDA libraries for onnxruntime-gpu
    from fast_eval import StreamFeatures
    torch.set_num_threads(4)
    rows = C.read_manifest(a.dataset)
    use = C.negative_rows_for_phrase(rows, a.phrase)
    have = {r["clip"] for r in use}
    for sid in a.include_needs_review:
        use += [r for r in rows if r.get("session") == sid and r.get("needs_review") and not r.get("superseded")
                and r.get("clip") and r["clip"] not in have and C.row_phrase(r) != C.normalize_phrase(a.phrase)]
    use = [r for r in use if r.get("clip") and r.get("kind") in ("cue", "segment")]
    cfg = _cfg(a)
    rng = np.random.default_rng(a.seed)
    F = StreamFeatures(*a.stream_onnx.split(","))
    groups = {"kivi": [], "near": [], "bg": []}
    srcs = {"kivi": [], "near": [], "bg": []}
    stats = collections.Counter()
    tasks, meta = [], []
    for r in use:
        x = C.read_pcm16(Path(a.dataset) / r["clip"]).astype(np.float64)
        if r["kind"] == "segment":
            g = "bg"
        else:
            g = "kivi" if str(r.get("label_original", r.get("label"))) == "kivi" else "near"
        is_tv = str(r["source"]).startswith("tv-")
        n = a.copies_seg if g == "bg" else a.copies
        tasks.append((x, is_tv, n, cfg, int(rng.integers(1 << 31))))
        meta.append((g, f"{r['session'][:24]}:{r['source']}"))
        stats[(g, r["source"], r["session"][:12])] += 1
    with mp.get_context("spawn").Pool(a.jobs) as pool:
        for (g, tag), (L, sigs) in zip(meta, pool.imap(_aug_row, tasks, chunksize=4)):
            stride = a.stride_seg if g == "bg" else a.stride
            for y in sigs:
                emb = F._get_embeddings(np.concatenate([y, np.zeros(1760, np.int16)]))
                # embedding e ends at about sample 1280 e + 12640; keep windows whose last embedding ends
                # inside the clip (after the mirrored lead)
                e0 = max(15, int(np.ceil((L - 12640) / 1280)))
                for e in range(e0, len(emb), stride):
                    groups[g].append(emb[e - 15:e + 1].astype(np.float16))
                    srcs[g].append(tag)
    out = Path(a.out_prefix)
    out.parent.mkdir(parents=True, exist_ok=True)
    os.chmod(out.parent, 0o700)
    summary = {"rows": {f"{k[0]}|{k[1]}|{k[2]}": v for k, v in sorted(stats.items())}}
    for g, X in groups.items():
        f = f"{a.out_prefix}_{g}.npy"
        A = np.stack(X) if X else np.zeros((0, 16, 96), np.float16)
        np.save(f, A)
        os.chmod(f, 0o600)
        with open(f + ".sources.txt", "w") as fo:
            fo.write("\n".join(srcs[g]) + "\n")
        summary[g] = {"windows": int(len(A)), "by_source": dict(collections.Counter(srcs[g]))}
        print(f, A.shape, flush=True)
    json.dump(summary, open(f"{a.out_prefix}_summary.json", "w"), indent=1)
    print(json.dumps(summary["rows"]))


if __name__ == "__main__":
    main()
