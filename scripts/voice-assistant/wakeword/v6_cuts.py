#!/usr/bin/env python3
"""Kivi v6: cut tables (JSONL for `v6_augment.py ctx`) from clips with context around the wake phrase.

The model scores a window of about 1.3 s (16 embeddings) ending at the newest audio. A cut is the sample that ends
the window. Plan section 7 (kivi-models docs/plan/2026-11-v6-plan.md):
- positives end at the phrase end (jitter), never inside the command;
- negatives from the same clip: windows ending before "Kivi", windows mostly in the command, command-only windows;
- clips without a reliable phrase end are dropped.

`synth`: v6_gen_ctx.py clips (exact sample boundaries in the manifest).
  ctx_pos: positive cut = phrase_end + U(0, 0.2) s (the v4/v5 geometry: word end 0-200 ms before the window end);
  negatives: phrase_end - U(0.5, 0.9) s (needs 0.15 s of the utterance before it), phrase_end + U(1.0, 1.6) s (mostly
  command) and phrase_end + 1.6 s + U(0, rest) (command only), the last two only when the clip goes on that long.
  ctx_neg (confusers in sentences, "kivi" without "oye", commands): cut = focus_end + U(0, 0.2) s when known, plus one
  cut uniform over the clip.
`live`: live wake clips of TRAIN days (Europe/Madrid even dates, `kivi_real_common.day_role == "mine"`). Labels:
  owner or auto-v1 `true` -> positives; `false` with trigger `wake` (the live model fired) -> hard negatives;
  `unclear`, `not-a-wake` and `false` button runs are not used. Phrase end = the exact-replay detection step
  (`peak_t_s`, end of that step's audio; the live model crossed there) for wake-triggered clips; for a button run whose
  request audio holds the phrase (`phrase_in_stt`), the v5 replay peak on stt.wav within -0.15..+0.6 s of the word
  timestamp end, kept only if that peak is >= --stt-min-peak. Request audio of a same-run clip is appended to the tail
  (the satellite streams continuously), which is what makes the command windows possible.
  Positive cuts: phrase end + U(-0.2, +0.15) s (`--copies` each). Hard negatives: the detection step + U(-0.2, +0.15) s
  plus cuts uniform over the tail.

    v6_cuts.py synth --manifest v6/clips_ctx/ctx_pos_train.manifest.csv --clips-dir v6/clips_ctx/ctx_pos_train \
        --out v6/cuts/synth_pos.jsonl
    v6_cuts.py live --live-dir DATASET/live --out v6/cuts/live.jsonl --summary v6/cuts/live_summary.json

Numbers only; no audio content is printed.
"""
import argparse
import csv
import json
import os
import sys
from datetime import datetime, timezone
from pathlib import Path
from zoneinfo import ZoneInfo

import numpy as np

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import kivi_real_common as C  # noqa: E402

RATE = 16000


def S(sec):
    return int(round(sec * RATE))


def synth_cuts(r, rng, pos_copies=2):
    """[(cut, group, copies)] for one v6_gen_ctx.py manifest row."""
    total = int(r["total_samples"])
    ps = int(r["phrase_start"] or 0)
    pe = int(r["phrase_end"]) if r.get("phrase_end") not in (None, "") else None
    cuts = []
    if r["kind"] == "ctx_pos":
        for _ in range(pos_copies):
            cuts.append((pe + S(rng.uniform(0, 0.2)), "pos", 1))
        c = pe - S(rng.uniform(0.5, 0.9))
        if c - ps >= S(0.15):
            cuts.append((c, "negpart", 1))
        c = pe + S(rng.uniform(1.0, 1.6))
        if c <= total:
            cuts.append((c, "negpart", 1))
        rest = total - (pe + S(1.6))
        if rest > S(0.3):
            cuts.append((pe + S(1.6) + int(rng.integers(0, rest)), "negpart", 1))
    else:
        if pe is not None:
            cuts.append((pe + S(rng.uniform(0, 0.2)), "conf", 1))
        cuts.append((int(rng.integers(min(total, S(0.5)), total + 1)), "conf", 1))
    return cuts


def cmd_synth(a):
    rng = np.random.default_rng(a.seed)
    n_clips, n_cuts = 0, {}
    Path(a.out).parent.mkdir(parents=True, exist_ok=True)
    with open(a.out, "w", encoding="utf-8") as fo:
        for r in csv.DictReader(open(a.manifest, encoding="utf-8")):
            cuts = synth_cuts(r, rng, a.pos_copies)
            if not cuts:
                continue
            n_clips += 1
            for _, g, k in cuts:
                n_cuts[f"{a.group_prefix}{g}"] = n_cuts.get(f"{a.group_prefix}{g}", 0) + k
            fo.write(json.dumps({"id": r["file"], "audio": [str(Path(a.clips_dir) / r["file"])], "tv": False,
                                 "cuts": [[c, f"{a.group_prefix}{g}", k] for c, g, k in cuts]}) + "\n")
    print(json.dumps({"clips": n_clips, "windows": n_cuts}))


def madrid_date(utc):
    t = datetime.strptime(utc, "%Y%m%dT%H%M%S.%fZ").replace(tzinfo=timezone.utc)
    return t.astimezone(ZoneInfo(C.DAY_TZ)).date()


def stt_peak(stt, we_s, scorer):
    """Time (s, end of the step's audio) and raw v5 score of the highest step within -0.15..+0.6 s of `we_s`."""
    from eval_stream import stream
    feats, models = scorer
    probs, times = stream(feats, models, stt)
    p = probs["v5"]
    m = (times >= we_s - 0.15) & (times <= we_s + 0.6)
    if not m.any():
        return None, 0.0
    i = np.flatnonzero(m)[int(np.argmax(p[m]))]
    return float(times[i]), float(p[i])


def cmd_live(a):
    from eval_stream import load_models
    rng = np.random.default_rng(a.seed)
    root = Path(a.live_dir)
    rows = list(csv.DictReader(open(root / "live_wakes.csv", encoding="utf-8")))
    scorer = load_models([f"v5={a.v5}"])
    out, summ = [], {"rows_train_day": 0, "used": {}, "dropped": {}, "positives_by_speaker": {},
                     "positives_by_device": {}, "hard_negatives_by_device": {}}

    def drop(why):
        summ["dropped"][why] = summ["dropped"].get(why, 0) + 1

    for r in rows:
        d = madrid_date(r["utc"])
        if C.day_role(d) != "mine":
            continue
        summ["rows_train_day"] += 1
        cdir = root / r["device"] / r["utc"]
        tail_p, stt_p = cdir / "wake_tail.wav", cdir / "stt.wav"
        if not tail_p.exists():
            drop("no tail")
            continue
        same = not r.get("stt_run")
        tail = C.read_pcm16(tail_p)
        label, trig = r["label"], r["trigger"]
        audio = [str(tail_p)] + ([str(stt_p)] if same and stt_p.exists() else [])
        n_audio = len(tail) + (len(C.read_pcm16(stt_p)) if same and stt_p.exists() else 0)
        cuts = []
        kind = None
        if label == "true" and trig == "wake" and r.get("peak_t_s"):
            pe = S(float(r["peak_t_s"]))
            kind = "pos"
        elif label == "true" and r.get("phrase_in_stt") == "true" and stt_p.exists() and r.get("stt_phrase_end_s"):
            stt = C.read_pcm16(stt_p)
            t, p = stt_peak(stt, float(r["stt_phrase_end_s"]), scorer)
            if t is None or p < a.stt_min_peak:
                drop(f"phrase in stt, v5 peak below {a.stt_min_peak} near the word end")
                continue
            audio, n_audio, pe = [str(stt_p)], len(stt), S(t)
            kind = "pos"
        elif label == "false" and trig == "wake" and r.get("peak_t_s"):
            pe = S(float(r["peak_t_s"]))
            kind = "hard"
        else:
            drop(f"label {label or 'empty'} / trigger {trig or 'empty'}")
            continue
        spk = r["speaker"] if r["speaker"] in ("spk01", "spk02") else "unk"
        if kind == "pos":
            for _ in range(a.copies):
                c = pe + S(rng.uniform(-0.2, 0.15))
                if c <= n_audio:
                    cuts.append([c, f"pos_{spk}", 1])
            for _ in range(a.neg_copies):
                c = pe - S(rng.uniform(0.6, 1.0))
                if c > S(0.3):
                    cuts.append([c, "negpart", 1])
            if len(audio) == 2 and r.get("phrase_in_stt") != "true":
                for _ in range(a.neg_copies):
                    c = pe + S(rng.uniform(1.0, 1.6))
                    if c <= n_audio:
                        cuts.append([c, "negpart", 1])
                    rest = n_audio - (pe + S(1.6))
                    if rest > S(0.3):
                        cuts.append([pe + S(1.6) + int(rng.integers(0, rest)), "negpart", 1])
            summ["positives_by_speaker"][spk] = summ["positives_by_speaker"].get(spk, 0) + 1
            summ["positives_by_device"][r["device"]] = summ["positives_by_device"].get(r["device"], 0) + 1
        else:
            for _ in range(a.copies):
                c = pe + S(rng.uniform(-0.2, 0.15))
                if c <= n_audio:
                    cuts.append([c, "hard", 1])
            for _ in range(a.neg_copies):
                cuts.append([int(rng.integers(S(0.5), len(tail) + 1)), "hard", 1])
            summ["hard_negatives_by_device"][r["device"]] = summ["hard_negatives_by_device"].get(r["device"], 0) + 1
        summ["used"][kind] = summ["used"].get(kind, 0) + 1
        out.append({"id": f"{r['device']}/{r['utc']}", "audio": audio, "tv": True, "cuts": cuts})
    Path(a.out).parent.mkdir(parents=True, exist_ok=True)
    with open(a.out, "w", encoding="utf-8") as fo:
        for o in out:
            fo.write(json.dumps(o) + "\n")
    os.chmod(a.out, 0o600)
    win = {}
    for o in out:
        for _, g, k in o["cuts"]:
            win[g] = win.get(g, 0) + k
    summ["windows"] = win
    print(json.dumps(summ, indent=1))
    if a.summary:
        Path(a.summary).write_text(json.dumps(summ, indent=1))


def main():
    ap = argparse.ArgumentParser()
    sp = ap.add_subparsers(dest="cmd", required=True)
    p = sp.add_parser("synth")
    p.add_argument("--manifest", required=True)
    p.add_argument("--clips-dir", required=True)
    p.add_argument("--out", required=True)
    p.add_argument("--group-prefix", default="syn_")
    p.add_argument("--pos-copies", type=int, default=2)
    p.add_argument("--seed", type=int, default=601)
    p = sp.add_parser("live")
    p.add_argument("--live-dir", default=os.path.join(C.DEFAULT_DATASET_ROOT, "live"))
    p.add_argument("--v5", default="/work/workspaces/kivi-wakeword/releases/v5-oye-kivi-2026-11-08/model/"
                                    "oye_kivi_next_uncalibrated.tflite")
    p.add_argument("--stt-min-peak", type=float, default=0.2)
    p.add_argument("--copies", type=int, default=150)
    p.add_argument("--neg-copies", type=int, default=30)
    p.add_argument("--out", required=True)
    p.add_argument("--summary")
    p.add_argument("--seed", type=int, default=602)
    a = ap.parse_args()
    {"synth": cmd_synth, "live": cmd_live}[a.cmd](a)


if __name__ == "__main__":
    main()
