#!/usr/bin/env python3
"""Kivi v6: synthetic "context" clips: "oye Kivi" (or a confuser) inside a real sentence, with no pause.

Builds on v4_gen.py / gen_clips.py (Piper voices, Spanish phonemes, trimming); the data plan is v6_ctx.yaml
(which names vnext_oye_kivi.yaml as base plan for voices, oye/kivi phonemes, warp and noise ranges).
Nothing is played. Clips are 16 kHz mono 16-bit WAV in <out>/<set>/ with <out>/<set>.manifest.csv and
<out>/<set>.drops.csv (idx, voice, reason). Every clip is rendered as separate parts joined at exact sample
boundaries, recorded in the manifest:

  head  = [prefix] + target "oye Kivi" (ctx_pos) or confuser (ctx_neg), ONE phoneme utterance (coarticulated),
          or the part of a "Kivi" sentence up to and including the word, or a plain command (+ prefix)
  gap   = 0 .. 0.12 s of zeros (most mass < 0.06 s)
  tail  = the command, rendered separately with the same voice, speaker and settings (absent in ~15 % of the
          prefixed ctx clips and for sentences like "compra kivis")

Sets: ctx_pos_train / ctx_neg_train (train voices minus the excluded ones), ctx_pos_eval / ctx_neg_eval
(held-out voices). phrase_start is 0 (the head starts the clip); phrase_end is the end of "oye Kivi" for ctx_pos
and the end of the confuser (or of the "Kivi" word) for ctx_neg (empty for plain commands). With a prefix the
target is not separable, so (phrase_end - phrase_start) includes the prefix.

    v6_gen_ctx.py gen --voices-dir V --out O --set all [--jobs 6]
    v6_gen_ctx.py stats --out O

The plan is deterministic per seed, set and clip index; the audio is not bit-exact (Piper noise).
"""
import argparse
import csv
import json
import multiprocessing as mp
import os
import random
import sys
import time
from pathlib import Path

import numpy as np
import yaml

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from gen_clips import RATE, load_voice, phonemize, trim, weighted  # noqa: E402
from v4_gen import _render, _speaker, _warp, target_parts  # noqa: E402

HERE = Path(__file__).resolve().parent
SETS = ["ctx_pos_train", "ctx_neg_train", "ctx_pos_eval", "ctx_neg_eval"]
MANIFEST_COLS = [
    "file", "set", "voice", "model", "speaker_id", "lang", "kind", "subtype", "variant", "prefix", "prefix_words",
    "has_prefix", "has_command", "command", "text", "joiner", "phonemes", "phrase_start", "phrase_end", "cmd_start",
    "total_samples", "head_samples", "gap_s", "length_scale", "noise_scale", "noise_w_scale", "warp", "peak_db",
    "cmd_rel_db", "zero_share", "idx"]


# ---------------------------------------------------------------- planning (pure, no Piper)

def load_plan(ctx_path=None):
    """(ctx, base): the v6 data file and the base v4/vnext plan it names."""
    ctx_path = Path(ctx_path or HERE / "v6_ctx.yaml")
    ctx = yaml.safe_load(open(ctx_path, encoding="utf-8"))
    base = yaml.safe_load(open(ctx_path.parent / ctx["base_plan"], encoding="utf-8"))
    return ctx, base


def set_role(set_name):
    return "heldout" if set_name.endswith("_eval") else "train"


def voice_pool(ctx, base, role):
    pool = {k: v for k, v in base["voices"].items() if v[1] == role}
    if role == "train":
        for k in ctx["exclude_train_voices"]:
            pool.pop(k, None)
    return pool


def draw_gap(rng, ctx):
    w, lo, hi = rng.choices(ctx["gap_mix"], weights=[g[0] for g in ctx["gap_mix"]])[0]
    return round(rng.uniform(lo, hi), 3)


def _json_nspk(voices_dir):
    cache = {}

    def nspk(stem):
        if stem not in cache:
            cfg = json.load(open(Path(voices_dir) / f"{stem}.onnx.json", encoding="utf-8"))
            cache[stem] = cfg.get("num_speakers", 1)
        return cache[stem]
    return nspk


def plan_item(ctx, base, set_name, i, seed, nspk_of):
    """Plan of clip number `i` of `set_name`: texts, phonemes of the target, voice, speaker and render settings."""
    rng = random.Random(f"{seed}-{set_name}-{i}")
    pos = set_name.startswith("ctx_pos")
    role = set_role(set_name)
    pool = voice_pool(ctx, base, role)
    if role == "heldout":
        names = [k for k in pool for _ in range(max(1, round(pool[k][3] * 4)))]
        vname = names[i % len(names)]
    else:
        vname = rng.choices(list(pool), weights=[pool[k][3] for k in pool])[0]
    stem, _, spk, _, lang = pool[vname]
    speaker_id = _speaker(rng, spk, nspk_of(stem))
    if lang == "mix":
        lang = rng.choice(["es", "es-419"])

    r1, r2 = rng.random(), rng.random()
    prefix_txt = weighted(rng, ctx["prefixes"])           # drawn always, so the stream does not depend on use
    command = weighted(rng, ctx["commands"])
    tdata = {"oye_phonemes": base["oye_phonemes"], "kivi_phonemes": base["kivi_phonemes"],
             "joiners": ctx["joiners"], "endings": ctx["endings"]}
    parts, joiner = target_parts(rng, tdata)
    confuser = weighted(rng, ctx["confusers"])
    sentence = weighted(rng, ctx["kivi_sentences"])
    gap = draw_gap(rng, ctx)

    subtype = "target" if pos else weighted(rng, ctx["neg_mix"])
    prefix, tail, focus = "", command, True
    variant, head = "target", None
    if subtype in ("target", "confuser"):
        if r1 < ctx["no_command_share"]:
            tail, prefix = "", prefix_txt
        elif r2 < ctx["prefix_share"]:
            prefix = prefix_txt
        pre = [["text", prefix]] if prefix else []
        if subtype == "target":
            head = pre + [["ph", parts[0]]]
        else:
            variant = confuser
            head = pre + [["text", confuser]]
            joiner = ""
    elif subtype == "kivi_sentence":
        variant = sentence.replace("|", " ").replace("  ", " ").strip()
        h, tail = sentence.split("|")
        head, joiner = [["text", h]], ""
    else:  # plain: an ordinary command, no confuser; optional prefix, one utterance
        subtype, variant, joiner, focus, tail = "plain", "none", "", False, ""
        if r1 < ctx["plain_prefix_share"]:
            prefix = prefix_txt
        head = ([["text", prefix]] if prefix else []) + [["text", command]]
    has_cmd = bool(tail) or subtype == "plain"
    cmd_text = command if subtype == "plain" else tail
    shown = {"target": "oye kivi", "confuser": variant}.get(subtype, "")
    text = variant if subtype == "kivi_sentence" else " ".join(
        x for x in ([prefix, command] if subtype == "plain" else [prefix, shown, tail]) if x)
    lo_w = base["warp"]
    return {
        "idx": i, "file": f"{set_name}_{i:06d}.wav", "set": set_name, "voice": vname, "model": stem,
        "speaker_id": speaker_id, "lang": lang, "kind": "ctx_pos" if pos else "ctx_neg", "subtype": subtype,
        "variant": variant, "prefix": prefix, "prefix_words": len(prefix.split()), "has_prefix": int(bool(prefix)),
        "has_command": int(has_cmd), "command": cmd_text,
        "text": text, "joiner": joiner, "head": head, "tail": tail, "focus": focus,
        "length_scale": round(rng.uniform(*ctx["length_scale"]), 3),
        "noise_scale": round(rng.uniform(*base["noise_scale"]), 3),
        "noise_w_scale": round(rng.uniform(*base["noise_w_scale"]), 3),
        "gap_s": gap if tail else None,
        "cmd_rel_db": round(rng.uniform(*ctx["cmd_rel_db"]), 1),
        "warp": round(rng.uniform(lo_w[1], lo_w[2]), 3) if rng.random() < lo_w[0] else 1.0,
        "peak_db": round(rng.uniform(-12, -1), 1),
    }


# ---------------------------------------------------------------- assembly and filters (pure, numpy only)

def tight(x, margin_s):
    return trim(x, RATE, margin_s=margin_s)


def assemble(head, tail, gap_s, cmd_rel_db, peak_db):
    """Concatenate head + gap zeros + tail (tail scaled to the head's peak plus cmd_rel_db), normalise the whole
    clip to peak_db and return (int16 samples, boundaries). All boundaries are exact sample indexes."""
    head = np.asarray(head, np.float32)
    if tail is None or len(tail) == 0:
        x, cmd_start = head, None
    else:
        tail = np.asarray(tail, np.float32)
        tail = tail * (float(np.max(np.abs(head))) / (float(np.max(np.abs(tail))) + 1e-9)) * 10 ** (cmd_rel_db / 20)
        gap_n = int(round((gap_s or 0.0) * RATE))
        x = np.concatenate([head, np.zeros(gap_n, np.float32), tail])
        cmd_start = len(head) + gap_n
    x = x / (float(np.max(np.abs(x))) or 1.0) * 10 ** (peak_db / 20)
    xi = np.clip(np.round(x * 32767), -32768, 32767).astype(np.int16)
    return xi, {"phrase_start": 0, "phrase_end": len(head), "cmd_start": cmd_start, "head_samples": len(head),
                "total_samples": len(xi)}


def drop_reason(ctx, it, head_samples, xi):
    """Why a rendered clip is dropped, or None."""
    if it["kind"] == "ctx_pos" or it["subtype"] == "confuser":
        limit = it["prefix_words"] * ctx["per_prefix_word_s"] + ctx["max_target_s"]
        if head_samples / RATE > limit:
            return "utt_too_long" if it["prefix_words"] else "target_too_long"
    if float(np.mean(xi == 0)) > ctx["max_zero_share"]:
        return "zeros"
    return None


# ---------------------------------------------------------------- rendering (needs Piper)

def _phon_pieces(pieces, lang, voices_dir):
    return " ".join(phonemize(s, lang, voices_dir) if mode == "text" else s for mode, s in pieces)


def render_item(ctx, voice, it, cfg, voices_dir):
    """-> (int16 samples, info dict) or a drop reason string."""
    head_ph = _phon_pieces(it["head"], it["lang"], voices_dir)
    head = _render(voice, head_ph, it, cfg)
    if head is None:
        return "babble"
    tail = None
    if it["tail"]:
        tail = _render(voice, phonemize(it["tail"], it["lang"], voices_dir), it, cfg)
        if tail is None:
            return "babble"
    m = ctx["trim_margin_s"]
    head = _warp(tight(head, m), it["warp"])
    if tail is not None:
        tail = _warp(tight(tail, m), it["warp"])
    xi, info = assemble(head, tail, it["gap_s"], it["cmd_rel_db"], it["peak_db"])
    reason = drop_reason(ctx, it, info["head_samples"], xi)
    if reason:
        return reason
    info["phonemes"] = head_ph
    info["zero_share"] = round(float(np.mean(xi == 0)), 4)
    return xi, info


def manifest_row(it, info):
    row = {k: it.get(k, "") for k in MANIFEST_COLS}
    row.update({k: v for k, v in info.items() if k in MANIFEST_COLS})
    if not it["focus"]:
        row["phrase_start"] = row["phrase_end"] = ""
        row["cmd_start"] = 0 if not it["has_prefix"] else ""
    for k in ("cmd_start", "gap_s"):
        if row[k] is None:
            row[k] = ""
    return row


def work(args):
    ctx, model_path, items, out_dir, voices_dir = args
    import soundfile as sf
    from piper import SynthesisConfig
    voice = load_voice(model_path)
    rows, drops = [], []
    for it in items:
        cfg = SynthesisConfig(speaker_id=it["speaker_id"], length_scale=it["length_scale"],
                              noise_scale=it["noise_scale"], noise_w_scale=it["noise_w_scale"],
                              normalize_audio=True)
        res = render_item(ctx, voice, it, cfg, voices_dir)
        if isinstance(res, str):
            drops.append({"idx": it["idx"], "voice": it["voice"], "reason": res})
            continue
        xi, info = res
        sf.write(os.path.join(out_dir, it["file"]), xi, RATE, subtype="PCM_16")
        rows.append(manifest_row(it, info))
    return rows, drops


def gen_set(a, ctx, base, pool, set_name, n, log):
    nspk = _json_nspk(a.voices_dir)
    out_dir = Path(a.out) / set_name
    out_dir.mkdir(parents=True, exist_ok=True)
    kept, drops, nxt, rnd, t0 = [], [], 0, 0, time.time()
    while len(kept) < n:
        need = n - len(kept)
        if nxt >= n * 3:
            raise SystemExit(f"{set_name}: too many drops ({nxt} planned, {len(kept)} kept)")
        items = [plan_item(ctx, base, set_name, i, a.seed, nspk) for i in range(nxt, nxt + need)]
        nxt += need
        rnd += 1
        by_model = {}
        for it in items:
            by_model.setdefault(it["model"], []).append(it)
        tasks = [(ctx, str(Path(a.voices_dir) / f"{stem}.onnx"), its[k:k + a.chunk], str(out_dir), a.voices_dir)
                 for stem, its in by_model.items() for k in range(0, len(its), a.chunk)]
        done = 0
        for rows, dr in pool.imap_unordered(work, tasks):
            kept.extend(rows)
            drops.extend(dr)
            done += 1
            if done % 10 == 0 or done == len(tasks):
                log(f"{set_name} round {rnd}: task {done}/{len(tasks)}, kept {len(kept)}/{n}, dropped {len(drops)}, "
                    f"{time.time() - t0:.0f} s")
    kept.sort(key=lambda r: r["file"])
    with open(Path(a.out) / f"{set_name}.manifest.csv", "w", newline="", encoding="utf-8") as f:
        w = csv.DictWriter(f, fieldnames=MANIFEST_COLS)
        w.writeheader()
        w.writerows(kept)
    drops.sort(key=lambda r: r["idx"])
    with open(Path(a.out) / f"{set_name}.drops.csv", "w", newline="", encoding="utf-8") as f:
        w = csv.DictWriter(f, fieldnames=["idx", "voice", "reason"])
        w.writeheader()
        w.writerows(drops)
    reasons = {}
    for d in drops:
        reasons[d["reason"]] = reasons.get(d["reason"], 0) + 1
    log(f"{set_name}: {len(kept)} clips kept, {len(drops)} dropped {reasons}, planned {nxt}, "
        f"{time.time() - t0:.0f} s")


def cmd_gen(a):
    ctx, base = load_plan(a.data)
    sets = SETS if a.set == "all" else [a.set]
    os.makedirs(a.out, mode=0o700, exist_ok=True)
    os.chmod(a.out, 0o700)

    def log(msg):
        print(msg, flush=True)
    with mp.get_context("spawn").Pool(a.jobs) as pool:
        for s in sets:
            gen_set(a, ctx, base, pool, s, a.n or ctx["default_n"][s], log)


# ---------------------------------------------------------------- sanity numbers (no listening, no ASR)

def frame_db(x, win_s=0.02, hop_s=0.01):
    """RMS in dB (full scale = 0 dB) of 20 ms frames every 10 ms; returns (db, frame centre times in s)."""
    x = np.asarray(x, np.float64)
    if x.dtype != np.float64 or np.max(np.abs(x)) > 1.5:
        x = x / 32768.0
    w, h = int(win_s * RATE), int(hop_s * RATE)
    n = max(0, (len(x) - w) // h + 1)
    if n == 0:
        return np.zeros(0), np.zeros(0)
    idx = np.arange(w)[None, :] + h * np.arange(n)[:, None]
    rms = np.sqrt(np.mean(x[idx] ** 2, axis=1))
    return 20 * np.log10(rms + 1e-6), (h * np.arange(n) + w / 2) / RATE


def boundary_check(x, phrase_end, cmd_start, tol=0.15, dip_db=10.0):
    """Energy evidence that phrase_end sits at the end of the phrase: (dip, onset).
    dip: within +-tol of phrase_end the frame level falls >= dip_db below the head's speech level (90th percentile
    of the 0.5 s before). onset: a command starts after (cmd_start) and rises >= dip_db above that window's minimum."""
    db, t = frame_db(x)
    pe = phrase_end / RATE
    head = db[(t >= pe - 0.5) & (t < pe)]
    win = db[(t >= pe - tol) & (t <= pe + tol)]
    if len(head) == 0 or len(win) == 0:
        return False, False
    dip = float(np.percentile(head, 90) - win.min()) >= dip_db
    onset = False
    if cmd_start is not None:
        cs = cmd_start / RATE
        after = db[(t >= cs) & (t <= cs + tol)]
        onset = len(after) > 0 and float(after.max() - win.min()) >= dip_db and abs(cs - pe) <= tol
    return dip, onset


def _pct(v, ps=(5, 25, 50, 75, 95)):
    v = np.asarray(v, float)
    return " ".join(f"p{p} {np.percentile(v, p):.2f}" for p in ps) if len(v) else "n/a"


def _read_manifest(path):
    return list(csv.DictReader(open(path, encoding="utf-8")))


def cmd_stats(a):
    import soundfile as sf
    rng = random.Random(a.seed)
    for s in SETS:
        mp_ = Path(a.out) / f"{s}.manifest.csv"
        if not mp_.exists():
            continue
        rows = _read_manifest(mp_)
        drops = _read_manifest(Path(a.out) / f"{s}.drops.csv")
        reasons = {}
        for d in drops:
            reasons[d["reason"]] = reasons.get(d["reason"], 0) + 1
        print(f"== {s}: {len(rows)} clips, dropped {len(drops)} {reasons}")
        tot = np.array([int(r["total_samples"]) for r in rows]) / RATE
        head = np.array([int(r["head_samples"]) for r in rows]) / RATE
        print(f"   whole clip s: {_pct(tot)}")
        print(f"   head utterance s (prefix + phrase): {_pct(head)}")
        npf = np.array([r["has_prefix"] == "1" for r in rows])
        if rows[0]["kind"] == "ctx_pos":
            print(f"   target-only s (no prefix, n {int((~npf).sum())}): {_pct(head[~npf])}")
        print(f"   with prefix {npf.mean():.3f}; with command {np.mean([r['has_command'] == '1' for r in rows]):.3f}")
        gaps = np.array([float(r["gap_s"]) for r in rows if r["gap_s"] != ""])
        print(f"   gap_s: share 0 {np.mean(gaps == 0):.2f}, < 0.06 {np.mean(gaps < 0.06):.2f}, >= 0.04 "
              f"{np.mean(gaps >= 0.04):.2f}, max {gaps.max():.3f}")
        ls = np.array([float(r["length_scale"]) for r in rows])
        print(f"   length_scale min {ls.min():.2f} max {ls.max():.2f}; zero_share max "
              f"{max(float(r['zero_share']) for r in rows):.3f}")
        for key in ("subtype", "variant"):
            cnt = {}
            for r in rows:
                if key == "variant" and r["subtype"] == "plain":
                    continue
                cnt[r[key]] = cnt.get(r[key], 0) + 1
            if len(cnt) > 1 and (key == "subtype" or rows[0]["kind"] == "ctx_neg"):
                print(f"   {key}: " + ", ".join(f"{k} {v}" for k, v in sorted(cnt.items(), key=lambda kv: -kv[1])))
        jn = {}
        for r in rows:
            jn[r["joiner"] or "-"] = jn.get(r["joiner"] or "-", 0) + 1
        print("   joiner: " + ", ".join(f"{k} {v}" for k, v in sorted(jn.items())))
        vc = {}
        for r in rows:
            vc[r["voice"]] = vc.get(r["voice"], 0) + 1
        print("   per voice: " + ", ".join(f"{k} {v}" for k, v in sorted(vc.items(), key=lambda kv: -kv[1])))
        dv = {}
        for d in drops:
            dv[d["voice"]] = dv.get(d["voice"], 0) + 1
        if dv:
            print("   dropped per voice: " + ", ".join(f"{k} {v}" for k, v in sorted(dv.items(), key=lambda kv: -kv[1])))
        if s == "ctx_pos_train" or a.all_sets:
            if rows[0]["kind"] != "ctx_pos":
                continue
            groups = {"50 random": rng.sample(rows, min(50, len(rows)))}
            g4 = [r for r in rows if r["gap_s"] != "" and float(r["gap_s"]) >= 0.04]
            groups["50 random with gap >= 0.04 s"] = rng.sample(g4, min(50, len(g4)))
            for name, sel in groups.items():
                res = []
                for r in sel:
                    x, _ = sf.read(Path(a.out) / s / r["file"], dtype="int16")
                    cs = int(r["cmd_start"]) if r["cmd_start"] != "" else None
                    res.append(boundary_check(x, int(r["phrase_end"]), cs))
                res = np.array(res)
                print(f"   boundary by energy, {name} (n {len(sel)}): dip>=10 dB {res[:, 0].mean():.2f}, "
                      f"command onset {res[:, 1].mean():.2f}, either {res.any(axis=1).mean():.2f}")


def main():
    ap = argparse.ArgumentParser()
    sp = ap.add_subparsers(dest="cmd", required=True)
    p = sp.add_parser("gen")
    p.add_argument("--data", default=str(HERE / "v6_ctx.yaml"))
    p.add_argument("--voices-dir", required=True)
    p.add_argument("--out", required=True)
    p.add_argument("--set", required=True, choices=SETS + ["all"])
    p.add_argument("--n", type=int, help="clips per set (default: default_n of the data file)")
    p.add_argument("--seed", type=int, default=6)
    p.add_argument("--jobs", type=int, default=6)
    p.add_argument("--chunk", type=int, default=80)
    p = sp.add_parser("stats")
    p.add_argument("--out", required=True)
    p.add_argument("--seed", type=int, default=6)
    p.add_argument("--all-sets", action="store_true", help="energy spot-check on every ctx_pos set")
    a = ap.parse_args()
    {"gen": cmd_gen, "stats": cmd_stats}[a.cmd](a)


if __name__ == "__main__":
    main()
