#!/usr/bin/env python3
"""Kivi v3: real session takes as training and validation features (server-exact).

Two steps, run on the private dataset only (nothing is played, printed or committed; the outputs are
feature arrays and aggregate counts):

`takes`: for every cue take of the given TRAIN sessions (manifest rows from `read_manifest`, split
train / train-only, not superseded, not `needs_review`, `--exclude-cue` left out), re-cut the take
from its raw file with more context than the 4 s clip: the manifest row gives the clip window
(`start_s`, session clock) and the offset used to cut it (`align_offset_s`), so the raw sample index
is round((to_ref(start_s) + offset) * 16000), exactly as `kivi_extract.py` cut the clip. The re-cut
is checked against the stored clip (sample for sample). The word end comes from the desk mic (energy
only, `kivi_score.word_span_from_ref`), mapped into each source's file by the same offset. Writes
`takes.npz` (int16 audio with 5 s before and 1 s after the word end, plus labels) into a private
directory (mode 700/600). Takes whose context is not inside the raw file are dropped and counted.

`feats`: augmented training windows (16, 96) per take, and clean evaluation sequences per take:
- tv-* sources (already through the satellite's front end): level change, TV-like interference at
  an SNR on the word (the interference alone goes through the front-end emulation, so NS has acted
  on it as on the TV), no second front-end pass;
- pc-mic / webcam-mic: room impulse response, level (word loudest 0.5 s ~ N(-23, 5) dBFS), TV-like
  interference, the calibrated mic floor (the desk mic gates to digital zero) and the front-end
  emulation, as v2_augment.py does for synthetic clips.
Word end in the window 0-200 ms (plus 50 ms either side for the energy-burst uncertainty) before the
last embedding's audio, as v2. `--holdout-file` lists takes (block, cue) kept out of the training
windows (validation); evaluation sequences are written for every take, tagged server-exact.

    v3_real.py takes --dataset ROOT --session SID --exclude-cue B4c5 --out v3/real/takes.npz
    v3_real.py feats --takes v3/real/takes.npz --holdout-file v3/real/holdout.json --copies 200 \
        --bg data/bg_tv:3 --bg data/bg_mls_es:2 --rir-dir data/mit_rirs --frontend FE --stream-onnx MEL,EMB \
        --out-prefix v3/feat/real
"""
import argparse
import glob
import json
import os
import sys
from pathlib import Path

import numpy as np

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import kivi_real_common as C  # noqa: E402
from v2_augment import LAST_AUDIO, LEAD, RATE, TOTAL, _bg_segment, _level, _loudest_rms, _read  # noqa: E402

PRE = 5 * RATE    # samples kept before the word end
POST = RATE       # and after it
JITTER = (-0.05, 0.25)


def _private(path):
    p = Path(path)
    p.parent.mkdir(parents=True, exist_ok=True)
    os.chmod(p.parent, 0o700)


def _alignment_for(sdir, label, exid):
    for f in sorted((Path(sdir) / label).glob("alignment*.json")):
        al = json.loads(f.read_text(encoding="utf-8"))
        if al.get("extract_id") == exid:
            return al
    raise SystemExit(f"{sdir}/{label}: no alignment record for {exid}")


def _raw_for(sdir, label, al, run):
    cands = [f for f in al["files"] if (f.get("run") or "") == (run or "") and f.get("included", True)]
    if len(cands) != 1:
        raise SystemExit(f"{label}: {len(cands)} included files for run {run!r}")
    name = cands[0]["name"]
    for sub in ("raw", "raw-stt"):
        p = Path(sdir) / label / sub / name
        if p.exists():
            return p
    raise SystemExit(f"{label}: raw file {name} not found")


def cmd_takes(a):
    from kivi_score import word_span_from_ref
    root = Path(a.dataset)
    rows = C.training_rows(C.read_manifest(root))
    excl = {(int(x[1:].split("c")[0]), int(x.split("c")[1])) for x in a.exclude_cue}
    out = {"audio": [], "we": [], "wl": [], "source": [], "label": [], "word": [], "block": [], "cue": [],
           "session": [], "stage": []}
    stats = {"rows": 0, "excluded": 0, "no_burst": 0, "no_context": 0, "recut_mismatch": 0, "kept": 0}
    for sid in a.session:
        sdir = root / sid
        session = json.loads((sdir / "session.json").read_text(encoding="utf-8"))
        to_ref, _ = C.session_to_ref(session)
        pc = C.read_pcm16(sdir / "pc-mic" / "raw" / "pc-mic.wav")
        raw_cache = {}
        for r in rows:
            if r["session"] != sid or r["kind"] != "cue":
                continue
            stats["rows"] += 1
            if (r["block"], r["cue_index"]) in excl:
                stats["excluded"] += 1
                continue
            tau0, tau1 = float(to_ref(r["start_s"])), float(to_ref(r["end_s"]))
            g = word_span_from_ref(pc, tau0, tau1)
            if g is None:
                stats["no_burst"] += 1
                continue
            d = float(r["align_offset_s"] or 0.0)
            al = _alignment_for(sdir, r["source"], r["extract_id"])
            rp = _raw_for(sdir, r["source"], al, r["source_run"])
            if rp not in raw_cache:
                raw_cache[rp] = C.read_pcm16(rp)
            x = raw_cache[rp]
            j0 = int(round((tau0 + d) * RATE))
            clip = C.read_pcm16(root / r["clip"])
            if j0 < 0 or not np.array_equal(x[j0:j0 + len(clip)], clip):
                stats["recut_mismatch"] += 1
                continue
            we = int(round((g[1] + d) * RATE))
            if we - PRE < 0 or we + POST > len(x):
                stats["no_context"] += 1
                continue
            out["audio"].append(x[we - PRE:we + POST].astype(np.int16))
            out["we"].append(PRE)
            out["wl"].append(int(round((g[1] - g[0]) * RATE)))
            out["source"].append(r["source"])
            out["label"].append("kivi" if r["label"] == "kivi" else "near")
            out["word"].append(r.get("word") or "")
            out["block"].append(r["block"])
            out["cue"].append(r["cue_index"])
            out["session"].append(sid)
            out["stage"].append(r.get("stage") or "")
            stats["kept"] += 1
    _private(a.out)
    np.savez(a.out, **{k: np.array(v) for k, v in out.items()})
    os.chmod(a.out, 0o600)
    import collections
    print(json.dumps(stats), dict(collections.Counter(zip(out["source"], out["label"]))))
    wl = np.array(out["wl"]) / RATE
    print(f"word length from the desk-mic burst: median {np.median(wl):.2f} s, p5 {np.percentile(wl, 5):.2f}, "
          f"p95 {np.percentile(wl, 95):.2f}")


def _cfg(a):
    bgs = []
    for spec in a.bg:
        d, wgt = spec.rsplit(":", 1)
        bgs += sorted(glob.glob(os.path.join(d, "*.wav"))) * int(wgt)
    return {"bgs": bgs, "snr": tuple(float(v) for v in a.snr.split(",")), "p_bg": a.p_bg,
            "p_bg_tv": a.p_bg_tv, "rirs": sorted(glob.glob(os.path.join(a.rir_dir, "*.wav"))) if a.rir_dir else [],
            "p_rir": a.p_rir, "level": tuple(float(v) for v in a.level_db.split(",")),
            "tv_gain": tuple(float(v) for v in a.tv_gain_db.split(",")),
            "floor": tuple(float(v) for v in a.floor_db.split(",")), "frontend": a.frontend}


def _word_rms(y, we, wl):
    seg = y[max(0, we - max(wl, 1600)):we]
    return np.sqrt(np.mean(seg ** 2)) + 1e-9


def _aug_chunk(args):
    """One worker: augmented windows (n, TOTAL) int16 for a list of (audio, we, wl, is_tv, seed)."""
    items, cfg = args
    from scipy.signal import fftconvolve
    from v2_frontend import Frontend
    fe = Frontend(cfg["frontend"], tmpdir=os.environ.get("V2_FE_TMP"))
    tv_out, desk_mix, desk_idx, bg_mix, bg_idx = {}, [], [], [], []
    for n, (x, we, wl, is_tv, seed) in enumerate(items):
        rng = np.random.default_rng(seed)
        jit = int(rng.uniform(*JITTER) * RATE)
        ws = we - (LAST_AUDIO - jit)              # window start: word end at LAST_AUDIO - jitter
        seg = x[ws - LEAD:ws + TOTAL].astype(np.float64)
        wend = LEAD + LAST_AUDIO - jit
        if is_tv:
            y = seg * 10 ** (rng.uniform(*cfg["tv_gain"]) / 20)
            tv_out[n] = y
            if cfg["bgs"] and rng.random() < cfg["p_bg_tv"]:
                b = _bg_segment(rng, cfg["bgs"], len(seg))
                s = rng.uniform(*cfg["snr"])
                bg_mix.append(b * (_word_rms(y, wend, wl) / (np.sqrt(np.mean(b ** 2)) + 1e-9)) / 10 ** (s / 20))
                bg_idx.append(n)
            continue
        if cfg["rirs"] and rng.random() < cfg["p_rir"]:
            h = _read(cfg["rirs"][rng.integers(len(cfg["rirs"]))])
            h = h / (np.max(np.abs(h)) + 1e-9)
            k = int(np.argmax(np.abs(h)))
            r = fftconvolve(seg, h)[k:k + len(seg)]
            seg = r * (np.sqrt(np.mean(seg ** 2)) / (np.sqrt(np.mean(r ** 2)) + 1e-9))
        rms = _word_rms(seg, wend, wl)
        if cfg["bgs"] and rng.random() < cfg["p_bg"]:
            b = _bg_segment(rng, cfg["bgs"], len(seg))
            s = rng.uniform(*cfg["snr"])
            seg = seg + b * (rms / (np.sqrt(np.mean(b ** 2)) + 1e-9)) / 10 ** (s / 20)
        target = 32768 * 10 ** (_level(rng, *cfg["level"]) / 20)
        seg = seg * target / _loudest_rms(seg, wend - max(wl, 8000), wend)
        seg = seg + 32768 * 10 ** (rng.uniform(*cfg["floor"]) / 20) * rng.standard_normal(len(seg))
        desk_mix.append(seg)
        desk_idx.append(n)
    out = [None] * len(items)
    if desk_mix:
        for n, y in zip(desk_idx, fe.process_batch(desk_mix)):
            out[n] = y[LEAD:]
    bg_fe = dict(zip(bg_idx, fe.process_batch(bg_mix))) if bg_mix else {}
    for n, y in tv_out.items():
        if n in bg_fe:
            y = y + bg_fe[n].astype(np.float64)
        out[n] = np.clip(np.round(y[LEAD:]), -32768, 32767).astype(np.int16)
    return np.stack(out)


def _eval_audio(x, is_tv, fe, rng, level_db=-23.0, floor_db=-55.0, wl=8000):
    if is_tv:
        return x.astype(np.int16)
    y = x.astype(np.float64)
    y = y * 32768 * 10 ** (level_db / 20) / _loudest_rms(y, PRE - max(wl, 8000), PRE)
    y = y + 32768 * 10 ** (floor_db / 20) * rng.standard_normal(len(y))
    return fe.process_batch([y])[0]


def cmd_feats(a):
    import multiprocessing as mp
    import torch  # noqa: F401
    from fast_eval import StreamFeatures, server_exact_tail
    from v2_frontend import Frontend
    T = {k: v.copy() for k, v in np.load(a.takes).items()}   # load once: NpzFile re-reads on every access
    n = len(T["audio"])
    hold = set()
    if a.holdout_file:
        hold = {(b, c) for b, c in json.load(open(a.holdout_file))["takes"]}
    keys = [(int(b), int(c)) for b, c in zip(T["block"], T["cue"])]
    is_tv = np.array([s.startswith("tv-") for s in T["source"]])
    cfg = _cfg(a)
    F = StreamFeatures(*a.stream_onnx.split(","))
    rng = np.random.default_rng(a.seed)
    # training windows: copies per take, train takes only
    tr = [i for i in range(n) if keys[i] not in hold]
    for lab in ("kivi", "near"):
        for grp in ("tv", "desk"):
            idx = [i for i in tr if T["label"][i] == lab and bool(is_tv[i]) == (grp == "tv")]
            items = [(T["audio"][i].copy(), int(T["we"][i]), int(T["wl"][i]), bool(is_tv[i]), int(rng.integers(1 << 31)))
                     for i in idx for _ in range(a.copies)]
            src = [str(T["source"][i]) for i in idx for _ in range(a.copies)]
            order = rng.permutation(len(items))
            items = [items[k] for k in order]
            src = [src[k] for k in order]
            chunks = [(items[k:k + 128], cfg) for k in range(0, len(items), 128)]
            res = []
            with mp.get_context("spawn").Pool(a.jobs) as pool:
                for audio in pool.imap(_aug_chunk, chunks):
                    res.append(server_exact_tail(F, audio))
            X = np.concatenate(res).astype(np.float16) if res else np.zeros((0, 16, 96), np.float16)
            out = f"{a.out_prefix}_{lab}_{grp}_train.npy"
            np.save(out, X)
            with open(out + ".sources.txt", "w") as fo:
                fo.write("\n".join(src) + "\n")
            print(out, X.shape, {s_: src.count(s_) for s_ in sorted(set(src))}, f"from {len(idx)} take-sources", flush=True)
    # evaluation sequences: every take, clean (tv as recorded; desk sources at -23 dBFS through the front end)
    fe = Frontend(a.frontend, tmpdir=os.environ.get("V2_FE_TMP"))
    erng = np.random.default_rng(a.seed + 1)
    seqs, names = [], []
    for i in range(n):
        y = _eval_audio(T["audio"][i], bool(is_tv[i]), fe, erng, wl=int(T["wl"][i]))
        emb = F._get_embeddings(np.concatenate([y, np.zeros(1760, np.int16)]))
        we = int(T["we"][i])
        # embedding e ends at about sample 1280 * e + 12640; keep 16-windows ending -0.2 s .. +0.6 s around the word end
        e_lo = int(np.ceil((we - 0.2 * RATE - 12640) / 1280))
        e_hi = int(np.floor((we + 0.6 * RATE - 12640) / 1280))
        seqs.append(emb[e_lo - 15:e_hi + 1].astype(np.float16))
        split = "holdout" if keys[i] in hold else "train"
        names.append(f"{T['session'][i]}:B{keys[i][0]}c{keys[i][1]}|{T['source'][i]}:{split}|{T['label'][i]}:{T['word'][i]}")
    out = f"{a.out_prefix}_eval.npz"
    np.savez(out, names=np.array(names), lengths=np.array([len(s) for s in seqs]), data=np.concatenate(seqs),
             features=np.array("server-exact"), meta=np.array("real takes; tv as recorded, desk through the front end"))
    print(out, len(seqs))


def main():
    ap = argparse.ArgumentParser()
    sp = ap.add_subparsers(dest="cmd", required=True)
    p = sp.add_parser("takes")
    p.add_argument("--dataset", default=C.DEFAULT_DATASET_ROOT)
    p.add_argument("--session", action="append", required=True)
    p.add_argument("--exclude-cue", action="append", default=[])
    p.add_argument("--out", required=True)
    p = sp.add_parser("feats")
    p.add_argument("--takes", required=True)
    p.add_argument("--holdout-file")
    p.add_argument("--copies", type=int, default=200)
    p.add_argument("--bg", action="append", default=[])
    p.add_argument("--snr", default="10,30")
    p.add_argument("--p-bg", type=float, default=0.5)
    p.add_argument("--p-bg-tv", type=float, default=0.4)
    p.add_argument("--rir-dir")
    p.add_argument("--p-rir", type=float, default=0.5)
    p.add_argument("--level-db", default="-23,5")
    p.add_argument("--tv-gain-db", default="-6,6")
    p.add_argument("--floor-db", default="-62,-48")
    p.add_argument("--frontend", required=True)
    p.add_argument("--stream-onnx", required=True)
    p.add_argument("--jobs", type=int, default=8)
    p.add_argument("--seed", type=int, default=0)
    p.add_argument("--out-prefix", required=True)
    a = ap.parse_args()
    {"takes": cmd_takes, "feats": cmd_feats}[a.cmd](a)


if __name__ == "__main__":
    main()
