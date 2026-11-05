#!/usr/bin/env python3
"""Generate synthetic Spanish wake-word clips with Piper ONNX voices for openWakeWord training.

Writes 16 kHz mono 16-bit WAV clips into <out>/<set>/ and <out>/<set>.manifest.csv (outside the
clip directory, because train.py counts every file in it). Every clip is rendered from
Spanish phonemes, also for non-Spanish voices (speaker variety with a Spanish pronunciation).
Nothing is played; the output must stay outside git (it is synthetic speech, but the voices have
their own licenses and the sets are large).

    gen_clips.py --data kivi_data.yaml --voices-dir <piper voices> --out <dir> \
        --set positive_train --n 60000 [--seed 1] [--jobs 24]

Sets: positive_train, positive_test (train voices, target word), negative_train, negative_test
(train voices, adversarial words and sentences), eval_kivi, eval_alexa, eval_nearmiss (held-out
voices only). openWakeWord's train.py reads the four training sets from
<output_dir>/<model_name>/{positive,negative}_{train,test}/.
"""
import argparse
import csv
import json
import multiprocessing as mp
import os
import random
from pathlib import Path

import numpy as np
import yaml

RATE = 16000


def weighted(rng, d):
    keys = list(d)
    return rng.choices(keys, weights=[d[k] for k in keys])[0]


def load_voice(path):
    """PiperVoice with a single-threaded ONNX session (many worker processes run at once)."""
    import onnxruntime
    from piper import PiperVoice
    from piper.config import PiperConfig
    from piper.voice import ESPEAK_DATA_DIR

    cfg = json.load(open(f"{path}.json", encoding="utf-8"))
    so = onnxruntime.SessionOptions()
    so.intra_op_num_threads = 1
    so.inter_op_num_threads = 1
    return PiperVoice(config=PiperConfig.from_dict(cfg),
                      session=onnxruntime.InferenceSession(str(path), sess_options=so,
                                                           providers=["CPUExecutionProvider"]),
                      espeak_data_dir=Path(ESPEAK_DATA_DIR), download_dir=Path.cwd())


_PHON = {}


def phonemize(text, lang, voices_dir):
    """Spanish phonemes of `text` (lang es or es-419), cached per process."""
    key = (text, lang)
    if key not in _PHON:
        stem = "es_ES-davefx-medium" if lang == "es" else "es_MX-ald-medium"
        if lang not in _PHON:
            _PHON[lang] = load_voice(Path(voices_dir) / f"{stem}.onnx")
        sents = _PHON[lang].phonemize(text)
        out = []
        for s in sents:
            out.extend(s + [" "])
        _PHON[key] = "".join(out).strip()
    return _PHON[key]


def trim(x, rate, margin_s=0.06, rel_db=-40.0):
    """Cut leading/trailing silence, keeping a short margin."""
    win = int(0.01 * rate)
    n = len(x) // win
    if n == 0:
        return x
    e = np.sqrt(np.mean(x[: n * win].reshape(n, win) ** 2, axis=1) + 1e-12)
    thr = e.max() * 10 ** (rel_db / 20)
    idx = np.where(e > thr)[0]
    if len(idx) == 0:
        return x
    m = int(margin_s * rate)
    a = max(0, idx[0] * win - m)
    b = min(len(x), (idx[-1] + 1) * win + m)
    return x[a:b]


def resample(x, sr):
    if sr == RATE:
        return x
    from math import gcd
    from scipy.signal import resample_poly
    g = gcd(sr, RATE)
    return resample_poly(x, RATE // g, sr // g)


def work(args):
    """Synthesize one chunk of items that share a voice model."""
    model_path, items, out_dir = args
    import soundfile as sf
    from piper import SynthesisConfig
    voice = load_voice(model_path)
    sr = voice.config.sample_rate
    rows = []
    for it in items:
        dst = os.path.join(out_dir, it["file"])
        if os.path.exists(dst):  # resume: the plan is deterministic per seed and set
            try:
                rows.append({**it, "duration_s": round(sf.info(dst).frames / RATE, 3), "attempts": 0})
                continue
            except RuntimeError:
                os.remove(dst)
        ids = voice.phonemes_to_ids(list(it["phonemes"]))
        cfg = SynthesisConfig(speaker_id=it["speaker_id"], length_scale=it["length_scale"],
                              noise_scale=it["noise_scale"], noise_w_scale=it["noise_w_scale"],
                              normalize_audio=True)
        # Some voices (the MLS "low" ones) babble or repeat on very short inputs: reject clips much
        # longer than the phoneme count allows and synthesize again (the noise differs every call).
        max_s = 0.6 + 0.15 * len(it["phonemes"]) * it["length_scale"]
        for attempt in range(6):
            audio = voice.phoneme_ids_to_audio(ids, cfg)
            x = trim(resample(np.asarray(audio, dtype=np.float32).reshape(-1), sr), RATE)
            if len(x) / RATE <= max_s:
                break
        else:
            continue  # dropped; counted in the summary
        peak = float(np.max(np.abs(x))) or 1.0
        x = x / peak * 10 ** (it["peak_db"] / 20)
        sf.write(os.path.join(out_dir, it["file"]), (x * 32767).astype(np.int16), RATE, subtype="PCM_16")
        rows.append({**it, "duration_s": round(len(x) / RATE, 3), "attempts": attempt + 1})
    return rows


def plan(data, set_name, n, rng, voices_dir):
    role = "heldout" if set_name.startswith("eval_") else "train"
    pool = {k: v for k, v in data["voices"].items() if v[1] == role}
    if set_name.startswith("eval_"):
        # equal coverage per voice group, cycling speakers, for per-voice reporting
        names = [k for k in pool for _ in range(max(1, round(pool[k][3] * 4)))]
    texts = [(w, "word") for w in data["adversarial_words"]] + \
            [(s, "sentence") for s in data["adversarial_sentences"]]
    items = []
    for i in range(n):
        vname = names[i % len(names)] if set_name.startswith("eval_") else \
            rng.choices(list(pool), weights=[pool[k][3] for k in pool])[0]
        stem, _, spk, _, lang = pool[vname]
        cfg = json.load(open(Path(voices_dir) / f"{stem}.onnx.json", encoding="utf-8"))
        nspk = cfg.get("num_speakers", 1)
        if spk == "all":
            speaker_id = rng.randrange(nspk) if nspk > 1 else None
        else:
            speaker_id = rng.randint(spk[0], spk[1])
        if lang == "mix":
            lang = rng.choice(["es", "es-419"])
        if set_name.startswith("positive") or set_name == "eval_kivi":
            phon = weighted(rng, data["positive_phonemes"]) + weighted(rng, data["endings"])
            text, kind = "kivi", "positive"
        elif set_name == "eval_alexa":
            phon = weighted(rng, data["alexa_phonemes"]) + weighted(rng, data["endings"])
            text, kind = "alexa", "alexa"
        else:
            if set_name == "eval_nearmiss":
                text, kind = texts[i % len(texts)]
            else:
                text, kind = rng.choice(texts)
            phon = phonemize(text, lang, voices_dir)
        items.append({
            "file": f"{set_name}_{i:06d}.wav", "voice": vname, "model": stem, "speaker_id": speaker_id,
            "lang": lang, "kind": kind, "text": text, "phonemes": phon,
            "length_scale": round(rng.uniform(*data["length_scale"]), 3),
            "noise_scale": round(rng.uniform(*data["noise_scale"]), 3),
            "noise_w_scale": round(rng.uniform(*data["noise_w_scale"]), 3),
            "peak_db": round(rng.uniform(-12, -1), 1),
        })
    return items


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--data", required=True)
    ap.add_argument("--voices-dir", required=True)
    ap.add_argument("--out", required=True, help="directory that receives <set>/ (one per set)")
    ap.add_argument("--set", required=True, choices=["positive_train", "positive_test", "negative_train",
                                                     "negative_test", "eval_kivi", "eval_alexa", "eval_nearmiss"])
    ap.add_argument("--n", type=int, required=True)
    ap.add_argument("--seed", type=int, default=1)
    ap.add_argument("--jobs", type=int, default=max(1, (os.cpu_count() or 2) - 4))
    ap.add_argument("--chunk", type=int, default=400)
    a = ap.parse_args()

    data = yaml.safe_load(open(a.data, encoding="utf-8"))
    rng = random.Random(f"{a.seed}-{a.set}")
    items = plan(data, a.set, a.n, rng, a.voices_dir)
    out_dir = Path(a.out) / a.set
    out_dir.mkdir(parents=True, exist_ok=True)

    by_model = {}
    for it in items:
        by_model.setdefault(it["model"], []).append(it)
    tasks = []
    for stem, its in by_model.items():
        for k in range(0, len(its), a.chunk):
            tasks.append((str(Path(a.voices_dir) / f"{stem}.onnx"), its[k:k + a.chunk], str(out_dir)))
    rows = []
    with mp.get_context("spawn").Pool(a.jobs) as pool:
        for i, r in enumerate(pool.imap_unordered(work, tasks)):
            rows.extend(r)
            print(f"\r{a.set}: {len(rows)}/{len(items)}", end="", flush=True)
    print()
    rows.sort(key=lambda r: r["file"])
    with open(Path(a.out) / f"{a.set}.manifest.csv", "w", newline="", encoding="utf-8") as f:
        w = csv.DictWriter(f, fieldnames=list(rows[0]))
        w.writeheader()
        w.writerows(rows)
    d = np.array([r["duration_s"] for r in rows])
    print(f"{a.set}: {len(rows)} clips ({len(items) - len(rows)} dropped after 6 tries), duration median {np.median(d):.2f} s, p95 {np.percentile(d, 95):.2f} s")


if __name__ == "__main__":
    main()
