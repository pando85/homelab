#!/usr/bin/env python3
"""Semi-synthetic Spanish STT corpus for the small-local-models study.

Synthesizes every phrase of agent_eval/phrases_es.yaml (control, general, live)
with the Spanish Piper voices of the satellite project's Phase 2 corpus, then
mixes each item with a random excerpt of its TV content set at several
speech-to-content ratios (SNR). No real voices, no recordings. Output (private,
outside this repository):

  $KIVI_PRIVATE/stt-v1/<cond>/<phrase>__<voice>.wav  16 kHz mono
  $KIVI_PRIVATE/stt-v1/manifest.csv

Needs piper-tts, numpy, scipy, soundfile and pyyaml. Voices, content set and
helpers come from a webos-voice-satellite checkout (WEBOS_REPO, default
~/webos-voice-satellite): artifacts/audio-rig/voices, devices/private/content/p2-v1
(tools/audio-rig/make_content.py) and tools/audio-rig/make_corpus.py.
KIVI_PRIVATE defaults to its git-ignored devices/private/kivi.
"""
import argparse
import csv
import os
import sys
from pathlib import Path

import numpy as np
import soundfile as sf

HERE = Path(__file__).resolve().parent
AGENT_EVAL = Path(os.environ.get("AGENT_EVAL_DIR", HERE.parent / "agent_eval"))
WEBOS = Path(os.environ.get("WEBOS_REPO", Path.home() / "webos-voice-satellite"))
PRIVATE = Path(os.environ.get("KIVI_PRIVATE", WEBOS / "devices" / "private" / "kivi"))
sys.path.insert(0, str(AGENT_EVAL))
sys.path.insert(0, str(WEBOS / "tools" / "audio-rig"))
import make_corpus as mc  # noqa: E402  (Synth, resample)
import run as ae  # noqa: E402  (phrase loader of the agent_eval harness)

FS = 16000
VOICES = [v for v in mc.VOICES if v[3] == "es"]  # 7 Spanish voices/speakers (ES and MX)
# Content categories mixed under the speech: what a TV in the room plausibly plays.
CONTENT = {
    "speech_es": ["speech_es__caperucita_roja.wav", "speech_es__lazarillo_tratados_6_7.wav"],
    "dialogue_en": ["speech_en__tears_of_steel_dialogue.wav", "speech_en__sherlock_scandal_in_bohemia.wav"],
    "music_vocals": ["music_vocals__woodward_cherubs.wav", "music_vocals__woodward_go.wav",
                     "music_vocals__woodward_saturday.wav"],
}
DEFAULT_SNRS = [10, 5, 0]


def active_mask(x, fs, frame_s=0.02, rel_db=-35.0):
    n = int(frame_s * fs)
    frames = len(x) // n
    e = np.array([np.sqrt(np.mean(x[i * n:(i + 1) * n] ** 2) + 1e-12) for i in range(frames)])
    thr = e.max() * 10 ** (rel_db / 20)
    m = np.repeat(e > thr, n)
    return np.concatenate([m, np.zeros(len(x) - len(m), bool)]), n


def rms(x):
    return float(np.sqrt(np.mean(x ** 2) + 1e-12))


def main():
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--out", type=Path, default=PRIVATE / "stt-v1")
    ap.add_argument("--voices-dir", type=Path, default=WEBOS / "artifacts" / "audio-rig" / "voices")
    ap.add_argument("--content-dir", type=Path, default=WEBOS / "devices" / "private" / "content" / "p2-v1")
    ap.add_argument("--snr", type=float, nargs="*", default=DEFAULT_SNRS)
    ap.add_argument("--groups", nargs="*", default=["control", "general", "live"])
    ap.add_argument("--seed", type=int, default=19)
    args = ap.parse_args()

    rng = np.random.default_rng(args.seed)
    phrases = [dict(id=i["id"], group=i["category"], kind=i["kind"], text=i["turns"][0]["text"])
               for i in ae.load_phrases() if i["category"] in args.groups]
    content = {}
    for cat, files in CONTENT.items():
        content[cat] = []
        for f in files:
            x, fs = sf.read(args.content_dir / f, dtype="float64")
            x = x.mean(axis=1) if x.ndim == 2 else x
            content[cat].append((f, mc.resample(x, fs, FS)))

    synth = mc.Synth(args.voices_dir)
    conds = ["clean"] + [f"snr{int(s):+d}" for s in args.snr]
    for c in conds:
        (args.out / c).mkdir(parents=True, exist_ok=True)
    rows = []
    for p in phrases:
        for voice, spk, _label, _lang, gender, code in VOICES:
            x48 = synth.say(voice, spk, p["text"], 1.0, 0.667, 0.8)
            x = mc.resample(x48, 48000, FS)
            x = np.concatenate([np.zeros(int(0.3 * FS)), x, np.zeros(int(0.5 * FS))])
            mask, _ = active_mask(x, FS)
            sp_rms = rms(x[mask]) if mask.any() else rms(x)
            g = 10 ** (-22.0 / 20) / sp_rms  # active speech at -22 dBFS, as in the P2 corpus
            x = x * g
            sp_rms *= g
            name = f"{p['id']}__{code}.wav"
            base = dict(id=p["id"], group=p["group"], kind=p.get("kind", ""), voice=code, gender=gender,
                        text=p["text"], file=name)
            sf.write(args.out / "clean" / name, x.astype(np.float32), FS, subtype="PCM_16")
            rows.append(dict(base, cond="clean", snr="", content="", offset_s=""))
            for s in args.snr:
                cat = list(CONTENT)[int(rng.integers(len(CONTENT)))]
                f, bed = content[cat][int(rng.integers(len(content[cat])))]
                off = int(rng.integers(0, len(bed) - len(x)))
                seg = bed[off:off + len(x)].copy()
                seg *= (sp_rms / 10 ** (s / 20)) / rms(seg)
                y = x + seg
                peak = np.max(np.abs(y))
                if peak > 0.98:
                    y *= 0.98 / peak
                c = f"snr{int(s):+d}"
                sf.write(args.out / c / name, y.astype(np.float32), FS, subtype="PCM_16")
                rows.append(dict(base, cond=c, snr=s, content=f"{cat}:{f}", offset_s=round(off / FS, 2)))
        print(p["id"], file=sys.stderr, flush=True)
    with open(args.out / "manifest.csv", "w", newline="") as fh:
        w = csv.DictWriter(fh, fieldnames=list(rows[0]))
        w.writeheader()
        w.writerows(rows)
    print(f"{len(rows)} items, {len(phrases)} phrases x {len(VOICES)} voices x {len(conds)} conditions -> {args.out}")


if __name__ == "__main__":
    main()
