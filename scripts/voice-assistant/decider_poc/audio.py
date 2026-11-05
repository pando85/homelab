#!/usr/bin/env python3
"""Speak the decider PoC items with Piper, mix TV content under them, transcribe with Whisper.

synth: every item of items_<split>.jsonl spoken by one random usable Spanish Piper voice (the
  five intelligible voices of small_models/stt_corpus.py), active speech at -22 dBFS, clean and
  mixed with the TV content set at +10, +5 and 0 dB. Train items get ONE random condition each;
  dev and test items get all four (paired per-SNR comparison).
    python audio.py synth --data $POC/data --split test --audio $POC/audio --workers 8
transcribe: faster-whisper large-v3-turbo int8 on CUDA, beam 5, language es and the static
  verbs-only initial prompt of the deployed plan (small_models/stt_eval.py VERBS_ES), i.e. the
  call of wyoming-faster-whisper with --initial-prompt.
    python audio.py transcribe --data $POC/data --split test --audio $POC/audio [--shard 0/2]
kivi-fake-v1 audio subset (items with "audio": true, clean / +5 / 0 dB):
    python audio.py synth --items $D/items.jsonl --data $D --split fake --audio $D/audio --conds clean snr+5 snr+0
    python audio.py transcribe --data $D --split fake --audio $D/audio
  -> $POC/data/stt_<split>[.<shard>].csv  (id, cond, voice, hyp, audio_s, stt_s)

Needs piper-tts, faster-whisper 1.2.1, numpy, soundfile and the webos-voice-satellite checkout
for voices, TV content and make_corpus helpers (see stt_corpus.py).
"""
import argparse
import csv
import json
import os
import sys
import time
from pathlib import Path

import numpy as np
import soundfile as sf

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE.parent / "small_models"))
import stt_corpus as sc_  # noqa: E402  (VOICES, CONTENT, active_mask, rms, mc.Synth, mc.resample)
import stt_eval  # noqa: E402  (VERBS_ES, EXCLUDED_VOICES)

FS = 16000
CONDS = ["clean", "snr+10", "snr+5", "snr+0"]
VOICES = [v for v in sc_.VOICES if v[5] not in stt_eval.EXCLUDED_VOICES]
VERBS_PROMPT = stt_eval.VERBS_ES + "."


def load_items(data, split):
    return [json.loads(line) for line in open(Path(data) / f"items_{split}.jsonl", encoding="utf-8")]


def plan(items, split, seed, conds_all=None):
    """[(item, voice tuple, [conds])] deterministic per split. Train items get one random
    condition; other splits get every condition of conds_all (default all four)."""
    rng = np.random.default_rng(seed + {"train": 0, "dev": 1, "test": 2}.get(split, 3))
    out = []
    for it in items:
        v = VOICES[int(rng.integers(len(VOICES)))]
        conds = [CONDS[int(rng.integers(4))]] if split == "train" else list(conds_all or CONDS)
        out.append((it, v, conds, int(rng.integers(1 << 30))))
    return out


def _synth_worker(args):
    jobs, audio_dir, content_dir, voices_dir = args
    synth = sc_.mc.Synth(voices_dir)
    content = {}
    for cat, files in sc_.CONTENT.items():
        content[cat] = []
        for f in files:
            x, fs = sf.read(Path(content_dir) / f, dtype="float64")
            x = x.mean(axis=1) if x.ndim == 2 else x
            content[cat].append((f, sc_.mc.resample(x, fs, FS)))
    rows = []
    for it, (voice, spk, _l, _lang, _g, code), conds, seed in jobs:
        rng = np.random.default_rng(seed)
        x48 = synth.say(voice, spk, it["spoken"], 1.0, 0.667, 0.8)
        x = sc_.mc.resample(x48, 48000, FS)
        x = np.concatenate([np.zeros(int(0.3 * FS)), x, np.zeros(int(0.5 * FS))])
        mask, _ = sc_.active_mask(x, FS)
        sp = sc_.rms(x[mask]) if mask.any() else sc_.rms(x)
        g = 10 ** (-22.0 / 20) / sp
        x, sp = x * g, sp * g
        for c in conds:
            y = x
            src = ""
            if c != "clean":
                snr = float(c[3:])
                cat = list(content)[int(rng.integers(len(content)))]
                f, bed = content[cat][int(rng.integers(len(content[cat])))]
                off = int(rng.integers(0, len(bed) - len(x)))
                seg = bed[off:off + len(x)].copy()
                seg *= (sp / 10 ** (snr / 20)) / sc_.rms(seg)
                y = x + seg
                pk = np.max(np.abs(y))
                if pk > 0.98:
                    y = y * 0.98 / pk
                src = f"{cat}:{f}@{off / FS:.2f}"
            p = Path(audio_dir) / it["split"] / c / f"{it['id']}.wav"
            p.parent.mkdir(parents=True, exist_ok=True)
            sf.write(p, y.astype(np.float32), FS, subtype="PCM_16")
            rows.append(dict(id=it["id"], cond=c, voice=code, content=src, audio_s=round(len(y) / FS, 2),
                             path=str(p.relative_to(audio_dir))))
    return rows


def cmd_synth(a):
    from multiprocessing import Pool
    if a.items:  # an arbitrary item file (e.g. kivi-fake-v1), only the items marked "audio": true
        items = [json.loads(x) for x in open(a.items, encoding="utf-8")]
        items = [dict(it, split=a.split) for it in items if it.get("audio")]
    else:
        items = load_items(a.data, a.split)
    jobs = plan(items, a.split, a.seed, a.conds)
    chunks = [jobs[i::a.workers] for i in range(a.workers)]
    with Pool(a.workers) as pool:
        parts = pool.map(_synth_worker, [(c, a.audio, a.content_dir, a.voices_dir) for c in chunks])
    rows = sorted((r for p in parts for r in p), key=lambda r: (r["id"], CONDS.index(r["cond"])))
    with open(Path(a.data) / f"audio_{a.split}.csv", "w", newline="") as fh:
        w = csv.DictWriter(fh, fieldnames=list(rows[0]))
        w.writeheader()
        w.writerows(rows)
    print(a.split, len(rows), "clips")


def cmd_transcribe(a):
    from faster_whisper import WhisperModel
    rows = list(csv.DictReader(open(Path(a.data) / f"audio_{a.split}.csv")))
    k, n = (int(x) for x in a.shard.split("/"))
    rows = rows[k::n]
    model = WhisperModel(a.model, device="cuda", compute_type="int8")
    out = Path(a.data) / (f"stt_{a.split}.{k}.csv" if n > 1 else f"stt_{a.split}.csv")
    done = set()
    if out.exists():
        done = {(r["id"], r["cond"]) for r in csv.DictReader(open(out))}
    fh = open(out, "a", newline="")
    w = csv.DictWriter(fh, fieldnames=["id", "cond", "voice", "hyp", "audio_s", "stt_s"])
    if not done:
        w.writeheader()
    t_start = time.time()
    for i, r in enumerate(rows):
        if (r["id"], r["cond"]) in done:
            continue
        x, _fs = sf.read(Path(a.audio) / r["path"], dtype="float32")
        t0 = time.perf_counter()
        segs, _info = model.transcribe(x, language="es", beam_size=5, initial_prompt=VERBS_PROMPT)
        hyp = " ".join(s.text.strip() for s in segs).strip()
        w.writerow(dict(id=r["id"], cond=r["cond"], voice=r["voice"], hyp=hyp, audio_s=r["audio_s"],
                        stt_s=round(time.perf_counter() - t0, 3)))
        if i % 500 == 0:
            fh.flush()
            print(a.split, k, i, len(rows), f"{time.time() - t_start:.0f}s", file=sys.stderr, flush=True)
    fh.close()
    print(out)


def main():
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    sub = ap.add_subparsers(dest="cmd", required=True)
    s = sub.add_parser("synth")
    s.add_argument("--data", required=True)
    s.add_argument("--split", required=True)
    s.add_argument("--audio", required=True)
    s.add_argument("--workers", type=int, default=8)
    s.add_argument("--seed", type=int, default=23)
    s.add_argument("--items", default=None, help="item JSONL; only items with audio=true are spoken")
    s.add_argument("--conds", nargs="*", default=None, help="conditions for non-train splits")
    s.add_argument("--voices-dir", default=str(sc_.WEBOS / "artifacts" / "audio-rig" / "voices"))
    s.add_argument("--content-dir", default=str(sc_.WEBOS / "devices" / "private" / "content" / "p2-v1"))
    t = sub.add_parser("transcribe")
    t.add_argument("--data", required=True)
    t.add_argument("--split", required=True)
    t.add_argument("--audio", required=True)
    t.add_argument("--model", default="large-v3-turbo")
    t.add_argument("--shard", default="0/1")
    a = ap.parse_args()
    {"synth": cmd_synth, "transcribe": cmd_transcribe}[a.cmd](a)


if __name__ == "__main__":
    main()
