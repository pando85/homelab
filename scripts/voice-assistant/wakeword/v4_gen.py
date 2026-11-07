#!/usr/bin/env python3
"""Kivi v4: synthetic clips for the wake phrase "oye Kivi" and its near-phrases (Piper voices).

Builds on gen_clips.py (voice loading, trimming, resampling); the data plan is v4_oye_kivi.yaml.
Nothing is played. Clips are 16 kHz mono 16-bit WAV in <out>/<set>/ with <out>/<set>.manifest.csv.

Sets (train voices: *_train, *_test; held-out voices: eval_*):
- positive_train / positive_test / eval_pos: "oye" (yeismo variants, or "oiga") + joiner (space, comma,
  run together, or two renderings with a 80-450 ms gap) + "kivi" (v1-v3 variants) + ending.
- negative_train / negative_test / eval_near: near-phrases (`KIVI` = "kivi" alone with the positives'
  variants) and short sentences; eval_near cycles all phrases equally for per-phrase reporting.
Variety: Piper length (speed) and noise scales, and a resampling warp (pitch, formants and tempo) with p.

    v4_gen.py gen --data v4_oye_kivi.yaml --voices-dir VOICES --out v4/clips --set positive_train --n 60000
    v4_gen.py check --data v4_oye_kivi.yaml --voices-dir VOICES --phrase "hoy Kivi" ... --json out.json

`check` renders the target and each phrase deterministically (noise scales 0) with the same voices and
compares log-mel sequences by DTW, against the distance between two renderings of the target itself
(length scale 1.0 and 1.08). A ratio near 1 means the TTS says the phrase like the target, so it
cannot be a negative (the "kibi" lesson of v3). Numbers only.
"""
import argparse
import csv
import json
import multiprocessing as mp
import os
import random
import sys
from pathlib import Path

import numpy as np
import yaml

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from gen_clips import RATE, load_voice, phonemize, resample, trim, weighted  # noqa: E402


def _speaker(rng, spk, nspk):
    if spk == "all":
        return rng.randrange(nspk) if nspk > 1 else None
    return rng.randint(spk[0], spk[1])


def target_parts(rng, data):
    """Phoneme parts of one "oye Kivi" rendering: a list of strings (two when the joiner is a gap)."""
    oye = weighted(rng, data["oye_phonemes"])
    kivi = weighted(rng, data["kivi_phonemes"])
    end = weighted(rng, data["endings"])
    j = weighted(rng, data["joiners"])
    if j == "space":
        return [f"{oye} {kivi}{end}"], j
    if j == "comma":
        return [f"{oye}, {kivi}{end}"], j
    if j == "together":
        return [oye.replace("ˈ", "ˌ") + kivi + end], j
    return [oye, kivi + end], j


def plan(data, set_name, n, rng, voices_dir):
    role = "heldout" if set_name.startswith("eval_") else "train"
    pool = {k: v for k, v in data["voices"].items() if v[1] == role}
    if role == "heldout":
        names = [k for k in pool for _ in range(max(1, round(pool[k][3] * 4)))]
    texts = [(t, "near", w) for t, w in data["near_phrases"].items()] + \
            [(t, "sentence", w) for t, w in data["near_sentences"].items()]
    cfgs = {}
    items = []
    for i in range(n):
        vname = names[i % len(names)] if role == "heldout" else \
            rng.choices(list(pool), weights=[pool[k][3] for k in pool])[0]
        stem, _, spk, _, lang = pool[vname]
        if stem not in cfgs:
            cfgs[stem] = json.load(open(Path(voices_dir) / f"{stem}.onnx.json", encoding="utf-8"))
        speaker_id = _speaker(rng, spk, cfgs[stem].get("num_speakers", 1))
        if lang == "mix":
            lang = rng.choice(["es", "es-419"])
        joiner = ""
        if set_name.startswith("positive") or set_name == "eval_pos":
            parts, joiner = target_parts(rng, data)
            text, kind = "oye kivi", "positive"
        else:
            if set_name == "eval_near":
                text, kind, _ = texts[i % len(texts)]
            else:
                text, kind, _ = rng.choices(texts, weights=[t[2] for t in texts])[0]
            if text == "KIVI":
                parts = [weighted(rng, data["kivi_phonemes"]) + weighted(rng, data["endings"])]
                text = "kivi"
            else:
                parts = [phonemize(text, lang, voices_dir)]
        lo_w = data["warp"]
        items.append({
            "file": f"{set_name}_{i:06d}.wav", "voice": vname, "model": stem, "speaker_id": speaker_id,
            "lang": lang, "kind": kind, "text": text, "joiner": joiner, "phonemes": "|".join(parts),
            "length_scale": round(rng.uniform(*data["length_scale"]), 3),
            "noise_scale": round(rng.uniform(*data["noise_scale"]), 3),
            "noise_w_scale": round(rng.uniform(*data["noise_w_scale"]), 3),
            "gap_s": round(rng.uniform(*data["gap_s"]), 3) if len(parts) > 1 else 0.0,
            "gap_rel_db": round(rng.uniform(*data["gap_rel_db"]), 1) if len(parts) > 1 else 0.0,
            "warp": round(rng.uniform(lo_w[1], lo_w[2]), 3) if rng.random() < lo_w[0] else 1.0,
            "peak_db": round(rng.uniform(-12, -1), 1),
        })
    return items


def _render(voice, phon, it, syn_cfg):
    """One phoneme string -> trimmed 16 kHz float audio, or None after 6 babbling attempts."""
    ids = voice.phonemes_to_ids(list(phon))
    max_s = 0.6 + 0.15 * len(phon) * it["length_scale"]
    for _ in range(6):
        x = trim(resample(np.asarray(voice.phoneme_ids_to_audio(ids, syn_cfg), np.float32).reshape(-1),
                          voice.config.sample_rate), RATE)
        if len(x) / RATE <= max_s:
            return x
    return None


def _warp(x, f):
    if f == 1.0:
        return x
    from scipy.signal import resample_poly
    up = int(round(1000 / f))
    return resample_poly(x, up, 1000).astype(np.float32)  # f > 1: shorter, higher pitch and formants


def work(args):
    model_path, items, out_dir = args
    import soundfile as sf
    from piper import SynthesisConfig
    voice = load_voice(model_path)
    rows = []
    for it in items:
        dst = os.path.join(out_dir, it["file"])
        if os.path.exists(dst):
            try:
                rows.append({**it, "duration_s": round(sf.info(dst).frames / RATE, 3)})
                continue
            except RuntimeError:
                os.remove(dst)
        cfg = SynthesisConfig(speaker_id=it["speaker_id"], length_scale=it["length_scale"],
                              noise_scale=it["noise_scale"], noise_w_scale=it["noise_w_scale"],
                              normalize_audio=True)
        parts = [_render(voice, p, it, cfg) for p in it["phonemes"].split("|")]
        if any(p is None for p in parts):
            continue
        if len(parts) == 2:
            a, b = parts
            b = b * (np.max(np.abs(a)) / (np.max(np.abs(b)) + 1e-9)) * 10 ** (it["gap_rel_db"] / 20)
            x = np.concatenate([a, np.zeros(int(it["gap_s"] * RATE), np.float32), b])
        else:
            x = parts[0]
        x = _warp(x, it["warp"])
        x = x / (float(np.max(np.abs(x))) or 1.0) * 10 ** (it["peak_db"] / 20)
        sf.write(dst, (x * 32767).astype(np.int16), RATE, subtype="PCM_16")
        rows.append({**it, "duration_s": round(len(x) / RATE, 3)})
    return rows


def cmd_gen(a):
    data = yaml.safe_load(open(a.data, encoding="utf-8"))
    rng = random.Random(f"{a.seed}-{a.set}")
    items = plan(data, a.set, a.n, rng, a.voices_dir)
    out_dir = Path(a.out) / a.set
    out_dir.mkdir(parents=True, exist_ok=True)
    by_model = {}
    for it in items:
        by_model.setdefault(it["model"], []).append(it)
    tasks = [(str(Path(a.voices_dir) / f"{stem}.onnx"), its[k:k + a.chunk], str(out_dir))
             for stem, its in by_model.items() for k in range(0, len(its), a.chunk)]
    rows = []
    with mp.get_context("spawn").Pool(a.jobs) as pool:
        for r in pool.imap_unordered(work, tasks):
            rows.extend(r)
            print(f"\r{a.set}: {len(rows)}/{len(items)}", end="", flush=True)
    print()
    rows.sort(key=lambda r: r["file"])
    with open(Path(a.out) / f"{a.set}.manifest.csv", "w", newline="", encoding="utf-8") as f:
        w = csv.DictWriter(f, fieldnames=list(rows[0]))
        w.writeheader()
        w.writerows(rows)
    d = np.array([r["duration_s"] for r in rows])
    print(f"{a.set}: {len(rows)} clips ({len(items) - len(rows)} dropped), duration median {np.median(d):.2f} s, "
          f"p5 {np.percentile(d, 5):.2f}, p95 {np.percentile(d, 95):.2f} s")


# ---- check: is a near-phrase rendered like the target? ----

def _logmel(x, n_mels=40):
    from scipy.signal import stft
    _, _, Z = stft(x, RATE, nperseg=400, noverlap=240, nfft=512)
    p = np.abs(Z) ** 2
    hz = np.linspace(0, RATE / 2, p.shape[0])
    mel = lambda f: 2595 * np.log10(1 + f / 700)  # noqa: E731
    edges = np.linspace(mel(60), mel(7600), n_mels + 2)
    fb = np.zeros((n_mels, len(hz)))
    m = mel(hz)
    for i in range(n_mels):
        lo, c, hi = edges[i:i + 3]
        fb[i] = np.clip(np.minimum((m - lo) / (c - lo), (hi - m) / (hi - c)), 0, None)
    lm = np.log(fb @ p + 1e-8).T
    return lm - lm.mean(axis=0)


def _dtw(A, B):
    An = A / (np.linalg.norm(A, axis=1, keepdims=True) + 1e-9)
    Bn = B / (np.linalg.norm(B, axis=1, keepdims=True) + 1e-9)
    C = 1 - An @ Bn.T
    n, m = C.shape
    D = np.full((n + 1, m + 1), np.inf)
    D[0, 0] = 0
    for i in range(1, n + 1):
        for j in range(1, m + 1):
            D[i, j] = C[i - 1, j - 1] + min(D[i - 1, j], D[i, j - 1], D[i - 1, j - 1])
    return D[n, m] / (n + m)


def _check_voice(args):
    model_path, spks, phons, target = args
    from piper import SynthesisConfig
    voice = load_voice(model_path)
    out = []
    for spk in spks:
        def ren(ph, ls=1.0):
            cfg = SynthesisConfig(speaker_id=spk, length_scale=ls, noise_scale=0.0, noise_w_scale=0.0,
                                  normalize_audio=True)
            ids = voice.phonemes_to_ids(list(ph))
            return trim(resample(np.asarray(voice.phoneme_ids_to_audio(ids, cfg), np.float32).reshape(-1),
                                 voice.config.sample_rate), RATE)
        T = _logmel(ren(target))
        ref = _dtw(T, _logmel(ren(target, 1.08)))
        out.append({lab: _dtw(T, _logmel(ren(ph))) / (ref + 1e-9) for lab, ph in phons.items()})
    return out


def cmd_check(a):
    data = yaml.safe_load(open(a.data, encoding="utf-8"))
    target = "ˈoʝe kˈiβi"
    phons = {}
    for p in a.phrase:
        phons[p] = p[1:] if p.startswith("=") else phonemize(p, "es", a.voices_dir)
    rng = random.Random(a.seed)
    tasks = []
    for name, (stem, role, spk, _, _) in data["voices"].items():
        if role != "train":
            continue
        nspk = json.load(open(Path(a.voices_dir) / f"{stem}.onnx.json", encoding="utf-8")).get("num_speakers", 1)
        spks = [_speaker(rng, spk, nspk) for _ in range(a.per_voice if nspk > 1 else 1)]
        tasks.append((str(Path(a.voices_dir) / f"{stem}.onnx"), spks, phons, target))
    res = []
    with mp.get_context("spawn").Pool(a.jobs) as pool:
        for r in pool.imap_unordered(_check_voice, tasks):
            res.extend(r)
    summary = {}
    for lab in phons:
        v = np.array([r[lab] for r in res])
        summary[lab] = {"phonemes": phons[lab], "n": len(v), "median_ratio": round(float(np.median(v)), 3),
                        "p10_ratio": round(float(np.percentile(v, 10)), 3),
                        "share_below_1.5": round(float(np.mean(v < 1.5)), 3)}
        print(f"{lab:22s} {phons[lab]:20s} median {summary[lab]['median_ratio']:.2f}  p10 {summary[lab]['p10_ratio']:.2f}  "
              f"<1.5: {summary[lab]['share_below_1.5']:.2f}  (n {len(v)})", flush=True)
    if a.json:
        json.dump({"target": target, "renderings": len(res), "phrases": summary}, open(a.json, "w"), indent=1,
                  ensure_ascii=False)


def main():
    ap = argparse.ArgumentParser()
    sp = ap.add_subparsers(dest="cmd", required=True)
    p = sp.add_parser("gen")
    p.add_argument("--data", required=True)
    p.add_argument("--voices-dir", required=True)
    p.add_argument("--out", required=True)
    p.add_argument("--set", required=True, choices=["positive_train", "positive_test", "negative_train",
                                                    "negative_test", "eval_pos", "eval_near"])
    p.add_argument("--n", type=int, required=True)
    p.add_argument("--seed", type=int, default=4)
    p.add_argument("--jobs", type=int, default=8)
    p.add_argument("--chunk", type=int, default=400)
    p = sp.add_parser("check")
    p.add_argument("--data", required=True)
    p.add_argument("--voices-dir", required=True)
    p.add_argument("--phrase", action="append", required=True, help="text, or =PHONEMES")
    p.add_argument("--per-voice", type=int, default=4, help="speakers per multi-speaker voice")
    p.add_argument("--seed", type=int, default=4)
    p.add_argument("--jobs", type=int, default=8)
    p.add_argument("--json")
    a = ap.parse_args()
    {"gen": cmd_gen, "check": cmd_check}[a.cmd](a)


if __name__ == "__main__":
    main()
