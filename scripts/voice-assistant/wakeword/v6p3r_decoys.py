#!/usr/bin/env python3
"""Kivi v6.3 retry: synthetic "oye <not Kivi>" decoys as training negatives (Piper TTS, v6 context-clip recipe).

Why: on the household TV microphones most false wakes are household conversation, and "oye" opens much household
Spanish (plan section 9.1 / 10). The v6 data had few "oye + another word" sentences (`v6_ctx.yaml` confusers, the
vnext near-phrases). These decoys add many voices saying "oye" followed by something that is not "Kivi", near-sound
words of "Kivi" inside sentences, and "oye" + a near-sound word, each optionally followed by a command or a
conversational sentence, with the same "oye" phoneme variants as the positives.

Privacy: the phrase list below is generic Spanish and phonetic neighbours, written by hand; nothing comes from a
household transcript. Household data is used only as aggregate category shares (`phrases`): the share of the even-day
high-score room moments (label queue P3 windows) whose local-ASR evidence flags (`auto_evidence`: oye=, kivi=,
weak=, ksim=, words=) say "oye + other word", "oye + Kivi-like word" or "Kivi-like word without oye". No word is read.

    v6p3r_decoys.py phrases [--table T] [--out O]          # phrase plan + category weights -> O/phrases.json
    v6p3r_decoys.py gen --set train|eval [--n N] [--start K] [--jobs 6]
                                                           # Piper rendering -> O/<set>/*.wav, O/<set>.parts/*.jsonl
    v6p3r_decoys.py manifest                               # merge parts -> O/manifest.jsonl, O/<set>.manifest.csv
    v6p3r_decoys.py augment --set train|eval [--jobs 7]    # train: v6_cuts synth geometry + v6_augment.py ctx (the v6
                                                           # chain: RIR, TV/MLS interference, level, floor, satellite
                                                           # front end) -> O/feat/v6p3r_decoy_conf.npy; eval: v4_augment.py
                                                           # eval clean -> O/feat/eval_decoy_clean.npz
    v6p3r_decoys.py counts                                 # -> O/counts.json

`gen` is resumable and chunkable: clip i of a set is fully determined by (seed, set, i); `--start/--n` render a range
and skip clips whose part rows exist. Nothing is played; only counts are printed. No training here.
"""
import argparse
import csv
import hashlib
import json
import multiprocessing as mp
import os
import random
import subprocess
import sys
import time
from collections import Counter
from pathlib import Path

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

HERE = Path(__file__).resolve().parent
KH = Path(os.environ.get("KIVI_HOME", Path.home() / "kivi-models" / "data"))
OUT = KH / "v6p3r" / "decoys"
TABLE = KH / "datasets" / "real" / "live-misses" / "labelq_v6p3r.csv"
VOICES_DIR = KH / "v4" / "voices"
SEED = 6303
SETS = {"train": 8000, "eval": 800}
DROP_VOICES = ("vctk_tr",)                    # quality report 2026-11-11: unintelligible by OOF and ASR
DOWN_WEIGHT = {"siwis": 0.5, "upmc": 0.5}     # low by OOF (and siwis by ASR)
CATEGORIES = ("oye_other", "oye_near", "near_sentence")
WEIGHT_FLOOR = 0.15
DEFAULT_WEIGHTS = {"oye_other": 0.5, "oye_near": 0.25, "near_sentence": 0.25}
TAIL_SHARE = {"oye_other": 0.7, "oye_near": 0.6}   # share of oye-heads followed by a command or sentence
CMD_VS_CHAT = 0.5                                   # of those tails: a home command (as the calls) or a chat sentence
PREFIX_SHARE = 0.25
PER_WORD_S = 0.6                                    # head duration limit: v6 max_target_s for "oye" + one word,
MAX_HEAD2_S = 1.5                                   # plus per_prefix_word_s per extra word
CTX_FLAGS = ["--floor-db=-62,-40", "--bg", "data/bg_tv:3", "--bg", "data/bg_mls_es:2", "--snr", "10,30",
             "--rir-dir", "data/mit_rirs"]          # v6/feats.sh, ctx-feats (CX)
FE = "v2/fe_build/release/examples/aec_offline"
SO = "emb_s_melrow.onnx,src/openWakeWord/openwakeword/resources/models/embedding_model.onnx"

# ------------------------------------------------------------------------------------------------ phrase lists
# Generic Spanish only. "oye" is rendered from the positives' oye phoneme variants; the rest is phonemized text.

OYE_OTHER = {   # what follows "oye" (vocatives, discourse markers, questions); never Kivi-like
    "mira": 3, "cariño": 2, "qué": 2, "Kiko": 2, "tú": 2, "perdona": 2, "una cosa": 2, "mamá": 2, "papá": 2,
    "tío": 2, "tía": 1, "chico": 1, "chica": 1, "guapa": 1, "guapo": 1, "amor": 2, "cielo": 1, "mi vida": 1,
    "nene": 1, "nena": 1, "hijo": 1, "hija": 1, "abuela": 1, "abuelo": 1, "ven": 2, "espera": 1, "escucha": 2,
    "y eso": 2, "qué tal": 1, "sabes qué": 2, "perdón": 1, "una pregunta": 1, "me oyes": 1, "por cierto": 1,
    "a ver": 1, "que sí": 1, "que no": 1, "ahora": 1, "déjalo": 1, "no sé": 1, "vale": 1, "venga": 1,
    "fíjate": 1, "cuidado": 1, "tranquilo": 1, "una cosita": 1, "dime": 1, "date prisa": 1, "aquí": 1,
    "otra vez": 1, "eso es": 1, "lo de ayer": 1, "mañana": 1, "esta noche": 1,
}
NEAR_WORDS_OYE = {   # phonetic neighbours of "Kivi" after "oye" that the v6 near-phrase list does not have
    "Kevin": 2, "quizá": 2, "guiri": 2, "Quique": 2, "Kike": 1, "Kika": 1, "pivi": 1, "Kini": 1,
    "Mimi": 1, "Nini": 1, "Didi": 1, "chiqui": 1, "Kimi": 1, "Tiki": 1, "Pipo": 1, "Kira": 1, "Kito": 1,
    "Gigi": 0.5, "Kili": 0.5, "Kiri": 0.5, "Tivi": 0.5, "Chivi": 0.5, "Fifi": 0.5,
}
NEAR_SENTENCES = {   # "head (ends at the word)|rest"; a Kivi-like word inside ordinary speech, no "oye"
    "quizá|mañana llueva": 2, "quizá|no venga nadie": 1, "pues quizá|sí": 1, "Kevin|no viene hoy": 2,
    "ha llamado Kevin|por la tarde": 1, "compra kiwis|en el mercado": 1, "pásame un kiwi|del frutero": 1,
    "el kiwi|está verde todavía": 1, "un guiri|me preguntó la hora": 1, "eso es de guiris|": 1,
    "Kiki|está dormida": 1, "dile a Kiki|que baje": 1, "mi chiqui|ya ha cenado": 1, "Quique|llega tarde": 1,
    "es muy cuqui|ese vestido": 1, "el chivi|no funciona": 1, "en la tivi|ponen fútbol": 1,
    "la Kika|tiene frío": 1, "y Kevin|qué dice": 1, "hoy vi|a tu hermano": 1, "yo vi|la película": 1,
    "quién vino|ayer": 1, "aquí vive|mucha gente": 1, "qué vivo|está el niño": 1, "que vi|un perro": 1,
    "se fue Kiko|a casa": 1, "con Kimi|al parque": 1, "la Mimi|está jugando": 1, "tiki taka|como siempre": 1,
    "no es Kiri|es otra": 1, "ve a por el kiwi|que se acaba": 1, "el pipi|del perro": 1,
}
CHAT = {   # conversational tails after "oye X"
    "has visto eso": 1, "ven un momento": 1, "dónde están las llaves": 1, "mañana tengo médico": 1,
    "qué hay de cena": 1, "pon la mesa": 1, "te acuerdas de lo de ayer": 1, "no me lo puedo creer": 1,
    "qué has dicho": 1, "esto está buenísimo": 1, "a qué hora vienes": 1, "has comprado pan": 1,
    "baja la basura": 1, "llama a tu madre": 1, "no hagas ruido": 1, "mira qué gracioso": 1, "vamos a salir": 1,
    "cuánto cuesta esto": 1, "me pasas el mando": 1, "qué película es esta": 1, "ya ha empezado": 1,
    "lo has apagado": 1, "tengo sueño": 1, "hace frío aquí": 1,
}


def _fold_tokens(text):
    return [w for w in text.replace(",", " ").replace("¿", " ").replace("?", " ").split() if w]


def phrase_check(text):
    """(ok, why) of one decoy text: never the wake phrase. Uses kivi_autolabel.match_words on the words (as if a
    transcript), and drops any text where an exact folded "Kivi" (kibi, quivi, kiwi, ...) follows "oye" within two
    words (the matcher's `phrase`)."""
    import kivi_autolabel as A
    words = [(w, float(i), float(i + 1), 1.0) for i, w in enumerate(_fold_tokens(text))]
    m = A.match_words(words)
    if m["phrase"]:
        return False, "phrase"
    toks = [A.fold(w[0]) for w in words]
    for i, t in enumerate(toks):
        if A.oye_sim(t) >= A.OYE_MIN_SIM and any(A.kivi_sim(words[j][0]) == 1.0 for j in range(i + 1, min(len(toks), i + 3))):
            return False, "oye+kivi-exact"
    return True, ""


def clean_lists():
    """The hand lists after phrase_check: (oye_other, near_words, near_sentences, dropped [(list, text, why)])."""
    dropped = []
    keep = []
    for name, d, fmt in (("oye_other", OYE_OTHER, "oye {}"), ("oye_near", NEAR_WORDS_OYE, "oye {}"),
                         ("near_sentence", NEAR_SENTENCES, "{}")):
        out = {}
        for k, w in d.items():
            ok, why = phrase_check(fmt.format(k.replace("|", " ")))
            if ok:
                out[k] = w
            else:
                dropped.append((name, k, why))
        keep.append(out)
    return keep[0], keep[1], keep[2], dropped


# ------------------------------------------------------------------------------------------------ household shares

def evidence(s):
    return dict(kv.split("=", 1) for kv in (s or "").split(";") if "=" in kv)


def category_of(ev, near_sim=0.75):
    """Broad class of one window's ASR evidence flags (no words): oye_other / oye_near / near_no_oye / speech / none,
    or None for a phrase (an attempt)."""
    if ev.get("ph") == "1":
        return None
    oye = ev.get("oye") == "1"
    near = ev.get("weak") == "1" or ev.get("kivi") == "1" or float(ev.get("ksim", 0) or 0) >= near_sim
    if oye:
        return "oye_near" if near else "oye_other"
    if near:
        return "near_no_oye"
    return "speech" if int(float(ev.get("words", 0) or 0)) > 0 else "none"


def household_shares(table=TABLE, split="train", tier="P3"):
    """Aggregate counts of the evidence classes of the even-day P3 windows (read-only; counts only)."""
    cnt = Counter()
    p = Path(table)
    if not p.exists():
        return cnt
    with open(p, newline="", encoding="utf-8") as f:
        for r in csv.DictReader(f):
            if r.get("split") != split or r.get("tier") != tier:
                continue
            c = category_of(evidence(r.get("auto_evidence")))
            cnt[c or "phrase"] += 1
    return cnt


def weights_from_shares(cnt, floor=WEIGHT_FLOOR):
    """Decoy category weights: the shares of oye_other, oye_near and near_no_oye (-> near_sentence) among the moments
    of those three classes, each floored at `floor`, renormalised. Default weights when there is nothing."""
    m = {"oye_other": cnt.get("oye_other", 0), "oye_near": cnt.get("oye_near", 0),
         "near_sentence": cnt.get("near_no_oye", 0)}
    tot = sum(m.values())
    if tot == 0:
        return dict(DEFAULT_WEIGHTS)
    w = {k: max(floor, v / tot) for k, v in m.items()}
    s = sum(w.values())
    return {k: round(v / s, 4) for k, v in w.items()}


def cmd_phrases(a):
    out = Path(a.out)
    out.mkdir(mode=0o700, parents=True, exist_ok=True)
    oye_other, near_words, near_sent, dropped = clean_lists()
    cnt = household_shares(a.table)
    weights = weights_from_shares(cnt)
    tot = sum(cnt.values()) or 1
    ctx, base = _plan()
    ov = overlap(oye_other, near_words, near_sent, ctx, base)
    doc = {"weights": weights, "household_even_p3_shares": {k: round(v / tot, 4) for k, v in sorted(cnt.items())},
           "household_even_p3_n": sum(cnt.values()), "weight_floor": WEIGHT_FLOOR,
           "oye_other": oye_other, "oye_near": near_words, "near_sentence": near_sent, "chat": CHAT,
           "dropped": [{"list": n, "text": t, "why": w} for n, t, w in dropped], "overlap_v6": ov,
           "drop_voices": list(DROP_VOICES), "down_weight": DOWN_WEIGHT}
    _write_json(out / "phrases.json", doc)
    print(json.dumps({"weights": weights, "shares": doc["household_even_p3_shares"], "n": doc["household_even_p3_n"],
                      "texts": {"oye_other": len(oye_other), "oye_near": len(near_words),
                                "near_sentence": len(near_sent)}, "dropped": len(dropped),
                      "overlap_v6": {k: v["n"] for k, v in ov.items()}}, indent=1))


def _norm(t):
    import kivi_autolabel as A
    return " ".join(A.fold(w) for w in _fold_tokens(t.replace("|", " ")))


def overlap(oye_other, near_words, near_sent, ctx, base):
    """Texts already in the v6 confusers / near-phrases / near-sentences / Kivi sentences (folded comparison)."""
    v6 = {"ctx_confusers": set(ctx["confusers"]), "ctx_kivi_sentences": set(ctx["kivi_sentences"]),
          "near_phrases": set(base["near_phrases"]), "near_sentences": set(base["near_sentences"])}
    v6n = {k: {_norm(t) for t in v} for k, v in v6.items()}
    mine = {f"oye {k}" for k in oye_other} | {f"oye {k}" for k in near_words} | set(near_sent)
    res = {}
    for k, s in v6n.items():
        hit = sorted(t for t in mine if _norm(t) in s)
        res[k] = {"n": len(hit), "texts": hit}
    res["new_texts"] = {"n": len([t for t in mine if not any(_norm(t) in s for s in v6n.values())]), "texts": []}
    return res


# ------------------------------------------------------------------------------------------------ planning (pure)

def _plan():
    import v6_gen_ctx as G
    return G.load_plan(HERE / "v6_ctx.yaml")


def voice_pool(ctx, base, role):
    import v6_gen_ctx as G
    pool = G.voice_pool(ctx, base, role)
    if role == "train":
        for k in DROP_VOICES:
            pool.pop(k, None)
        pool = {k: (v[:3] + [v[3] * DOWN_WEIGHT.get(k, 1.0)] + v[4:]) for k, v in pool.items()}
    return pool


def plan_item(i, set_name, ctx, base, phrases, nspk_of, seed=SEED):
    """Clip number i of a set: text pieces, voice, speaker and render settings (deterministic per seed, set, i)."""
    import v6_gen_ctx as G
    from gen_clips import weighted
    from v4_gen import _speaker
    rng = random.Random(f"{seed}-decoy-{set_name}-{i}")
    role = "heldout" if set_name == "eval" else "train"
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
    cat = weighted(rng, phrases["weights"])
    oye_ph = weighted(rng, base["oye_phonemes"])
    comma = rng.random() < 0.3
    prefix = weighted(rng, ctx["prefixes"]) if rng.random() < PREFIX_SHARE else ""
    command = weighted(rng, ctx["commands"])
    chat = weighted(rng, phrases["chat"])
    r_tail, r_kind = rng.random(), rng.random()
    if cat in ("oye_other", "oye_near"):
        word = weighted(rng, phrases[cat])
        tail = (command if r_kind < CMD_VS_CHAT else chat) if r_tail < TAIL_SHARE[cat] else ""
        head = ([["text", prefix]] if prefix else []) + [["ph", oye_ph + ("," if comma else "")], ["text", word]]
        head_text = " ".join(x for x in (prefix, "oye" + ("," if comma else ""), word) if x)
        n_words = len(prefix.split()) + 1 + len(word.split())
        variant = word
    else:
        sent = weighted(rng, phrases["near_sentence"])
        h, tail = sent.split("|")
        head, head_text, variant, prefix = [["text", h]], h, sent.replace("|", " ").strip(), ""
        n_words = len(h.split())
    text = " ".join(x for x in (head_text, tail) if x)
    lo_w = base["warp"]
    return {
        "idx": i, "id": f"decoy_{set_name}_{i:06d}", "file": f"decoy_{set_name}_{i:06d}.wav", "set": set_name,
        "category": cat, "variant": variant, "text": text, "voice": vname, "model": stem, "speaker_id": speaker_id,
        "lang": lang, "kind": "ctx_neg", "subtype": "decoy", "prefix": prefix, "prefix_words": 0,
        "head": head, "tail": tail, "focus": True, "head_words": n_words, "has_command": int(bool(tail)),
        "length_scale": round(rng.uniform(*ctx["length_scale"]), 3),
        "noise_scale": round(rng.uniform(*base["noise_scale"]), 3),
        "noise_w_scale": round(rng.uniform(*base["noise_w_scale"]), 3),
        "gap_s": G.draw_gap(rng, ctx) if tail else None,
        "cmd_rel_db": round(rng.uniform(*ctx["cmd_rel_db"]), 1),
        "warp": round(rng.uniform(lo_w[1], lo_w[2]), 3) if rng.random() < lo_w[0] else 1.0,
        "peak_db": round(rng.uniform(-12, -1), 1),
    }


def head_limit_s(it):
    """Longest allowed head: v6's 1.5 s for "oye" + one word, plus 0.6 s per extra word (prefix included)."""
    return MAX_HEAD2_S + PER_WORD_S * max(0, it["head_words"] - 2)


def manifest_row(it, info, sha):
    return {"idx": it["idx"], "id": it["id"], "file": it["file"], "set": it["set"], "category": it["category"], "variant": it["variant"],
            "text": it["text"], "voice": it["voice"], "model": it["model"], "speaker_id": it["speaker_id"],
            "lang": it["lang"], "speed": it["length_scale"],
            "aug": {"noise_scale": it["noise_scale"], "noise_w_scale": it["noise_w_scale"], "warp": it["warp"],
                    "peak_db": it["peak_db"], "gap_s": it["gap_s"], "cmd_rel_db": it["cmd_rel_db"]},
            "kind": "ctx_neg", "phrase_start": 0, "phrase_end": info["head_samples"], "cmd_start": info["cmd_start"],
            "total_samples": info["total_samples"], "duration_s": round(info["total_samples"] / 16000, 3),
            "has_command": it["has_command"], "zero_share": info.get("zero_share"), "sha256": sha}


# ------------------------------------------------------------------------------------------------ rendering

def _work(args):
    ctx, model_path, items, out_dir, voices_dir = args
    import numpy as np
    import soundfile as sf
    from piper import SynthesisConfig

    import v6_gen_ctx as G
    from gen_clips import load_voice
    voice = load_voice(model_path)
    rows, drops = [], []
    for it in items:
        cfg = SynthesisConfig(speaker_id=it["speaker_id"], length_scale=it["length_scale"],
                              noise_scale=it["noise_scale"], noise_w_scale=it["noise_w_scale"], normalize_audio=True)
        res = G.render_item(ctx, voice, it, cfg, voices_dir)
        if isinstance(res, str):
            drops.append({"idx": it["idx"], "voice": it["voice"], "category": it["category"], "reason": res})
            continue
        xi, info = res
        if info["head_samples"] / 16000 > head_limit_s(it):
            drops.append({"idx": it["idx"], "voice": it["voice"], "category": it["category"], "reason": "head_too_long"})
            continue
        sf.write(os.path.join(out_dir, it["file"]), xi, 16000, subtype="PCM_16")
        os.chmod(os.path.join(out_dir, it["file"]), 0o600)
        rows.append(manifest_row(it, info, hashlib.sha256(np.ascontiguousarray(xi).tobytes()).hexdigest()))
    return rows, drops


def _json_nspk(voices_dir):
    import v6_gen_ctx as G
    return G._json_nspk(voices_dir)


def cmd_gen(a):
    out = Path(a.out)
    phrases = json.loads((out / "phrases.json").read_text())
    ctx, base = _plan()
    clip_dir, part_dir = out / a.set, out / f"{a.set}.parts"
    for d in (clip_dir, part_dir):
        d.mkdir(mode=0o700, parents=True, exist_ok=True)
    done = set()
    for p in part_dir.glob("*.jsonl"):
        for line in p.read_text().splitlines():
            done.add(_idx(json.loads(line)))
    n = a.n if a.n is not None else SETS[a.set] - a.start
    nspk = _json_nspk(a.voices_dir)
    items = [plan_item(i, a.set, ctx, base, phrases, nspk, a.seed) for i in range(a.start, a.start + n) if i not in done]
    by_model = {}
    for it in items:
        by_model.setdefault(it["model"], []).append(it)
    tasks = [(ctx, str(Path(a.voices_dir) / f"{stem}.onnx"), its[k:k + a.chunk], str(clip_dir), str(a.voices_dir))
             for stem, its in by_model.items() for k in range(0, len(its), a.chunk)]
    t0, kept, drops = time.time(), 0, Counter()
    part = part_dir / f"part_{a.start:06d}_{int(time.time())}.jsonl"
    with mp.get_context("spawn").Pool(a.jobs) as pool, open(part, "w", encoding="utf-8") as fo:
        for k, (rows, dr) in enumerate(pool.imap_unordered(_work, tasks), 1):
            fo.writelines(json.dumps(r, ensure_ascii=False) + "\n" for r in rows)
            for d in dr:
                fo.write(json.dumps(d | {"dropped": True}) + "\n")
                drops[d["reason"]] += 1
            fo.flush()
            kept += len(rows)
            if k % 10 == 0 or k == len(tasks):
                print(f"{a.set} [{a.start}, {a.start + n}): task {k}/{len(tasks)}, kept {kept}, dropped "
                      f"{sum(drops.values())} {dict(drops)}, {time.time() - t0:.0f} s", flush=True)
    os.chmod(part, 0o600)


def _idx(r):
    """Clip number of a part row (kept rows of the first parts carry it only in the id)."""
    return r["idx"] if "idx" in r else int(r["id"].rsplit("_", 1)[1])


def _rows(out, set_name, dropped=False):
    rows = {}
    for p in sorted((Path(out) / f"{set_name}.parts").glob("*.jsonl")):
        for line in p.read_text().splitlines():
            r = json.loads(line)
            if bool(r.get("dropped")) == dropped:
                rows[_idx(r)] = r
    return [rows[k] for k in sorted(rows)]


def cmd_manifest(a):
    out = Path(a.out)
    with open(out / "manifest.jsonl", "w", encoding="utf-8") as fo:
        for s in SETS:
            rows = _rows(out, s)
            fo.writelines(json.dumps({k: v for k, v in r.items() if k != "idx"}, ensure_ascii=False) + "\n" for r in rows)
            with open(out / f"{s}.manifest.csv", "w", newline="", encoding="utf-8") as f:   # for v4_augment.py eval
                w = csv.DictWriter(f, fieldnames=["file", "text", "category", "voice", "kind", "phrase_start",
                                                  "phrase_end", "total_samples"], extrasaction="ignore")
                w.writeheader()
                w.writerows(rows)
            os.chmod(out / f"{s}.manifest.csv", 0o600)
            print(f"{s}: {len(rows)} clips, {len(_rows(out, s, dropped=True))} dropped")
    os.chmod(out / "manifest.jsonl", 0o600)


def cmd_augment(a):
    import numpy as np

    import v6_cuts as V
    out = Path(a.out)
    feat = out / "feat"
    feat.mkdir(mode=0o700, parents=True, exist_ok=True)
    env = dict(os.environ, OMP_NUM_THREADS="1", V2_FE_TMP=str(KH / "v6p3r" / "tmp"))
    Path(env["V2_FE_TMP"]).mkdir(mode=0o700, parents=True, exist_ok=True)
    py = [sys.executable, "-I"]
    if a.set == "train":
        rng = np.random.default_rng(a.seed)
        table = out / "cuts_train.jsonl"
        n = 0
        with open(table, "w", encoding="utf-8") as fo:
            for r in _rows(out, "train"):
                cuts = V.synth_cuts({k: str(v) if v is not None else "" for k, v in r.items()}, rng)
                n += len(cuts)
                fo.write(json.dumps({"id": r["file"], "audio": [str(out / "train" / r["file"])], "tv": False,
                                     "cuts": [[c, f"decoy_{g}", k] for c, g, k in cuts]}) + "\n")
        os.chmod(table, 0o600)
        print(f"cut table: {n} windows", flush=True)
        cmd = py + [str(HERE / "v6_augment.py"), "ctx", "--table", str(table), "--out-prefix", str(feat / "v6p3r"),
                    "--stream-onnx", SO, "--frontend", FE, "--jobs", str(a.jobs), "--seed", str(a.seed)] + CTX_FLAGS
    else:
        cmd = py + [str(HERE / "v4_augment.py"), "eval", "--clips-dir", str(out / "eval"), "--manifest",
                    str(out / "eval.manifest.csv"), "--out", str(feat / "eval_decoy_clean.npz"), "--stream-onnx", SO,
                    "--frontend", FE, "--jobs", str(a.jobs), "--seed", str(a.seed)]
    subprocess.run(cmd, cwd=KH, env=env, check=True)
    for p in feat.glob("*"):
        os.chmod(p, 0o600)


def cmd_counts(a):
    out = Path(a.out)
    phrases = json.loads((out / "phrases.json").read_text())
    res = {"weights": phrases["weights"], "household_even_p3_shares": phrases["household_even_p3_shares"],
           "household_even_p3_n": phrases["household_even_p3_n"], "texts_dropped_by_phrase_check": len(phrases["dropped"]),
           "dropped_texts": phrases["dropped"], "overlap_v6": {k: v["n"] for k, v in phrases["overlap_v6"].items()},
           "sets": {}}
    for s in SETS:
        rows, drops = _rows(out, s), _rows(out, s, dropped=True)
        if not rows and not drops:
            continue
        dur = [r["duration_s"] for r in rows]
        res["sets"][s] = {
            "clips": len(rows), "hours": round(sum(dur) / 3600, 3),
            "duration_s_p5_50_95": [round(x, 2) for x in _pct(dur, (5, 50, 95))],
            "per_category": dict(Counter(r["category"] for r in rows)),
            "with_tail": sum(r["has_command"] for r in rows),
            "per_voice": dict(sorted(Counter(r["voice"] for r in rows).items(), key=lambda kv: -kv[1])),
            "voices": len({r["voice"] for r in rows}), "speakers": len({(r["model"], r["speaker_id"]) for r in rows}),
            "distinct_texts": len({r["text"] for r in rows}), "distinct_heads": len({r["variant"] for r in rows}),
            "renders_per_head_median": _pct(list(Counter(r["variant"] for r in rows).values()), (50,))[0],
            "dropped": dict(Counter(d["reason"] for d in drops)),
            "duplicate_audio": len(rows) - len({r["sha256"] for r in rows})}
    feat = out / "feat"
    if feat.exists():
        import numpy as np
        res["features"] = {p.name: list(np.load(p, mmap_mode="r").shape) for p in sorted(feat.glob("*.npy"))}
    _write_json(out / "counts.json", res)
    print(json.dumps({k: v for k, v in res.items() if k != "dropped_texts"}, indent=1, ensure_ascii=False))


def _pct(v, ps):
    import numpy as np
    return [float(np.percentile(v, p)) for p in ps] if len(v) else [0.0 for _ in ps]


def _write_json(p, doc):
    p = Path(p)
    p.write_text(json.dumps(doc, indent=1, ensure_ascii=False) + "\n")
    os.chmod(p, 0o600)


def main(argv=None):
    ap = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    ap.add_argument("what", choices=["phrases", "gen", "manifest", "augment", "counts"])
    ap.add_argument("--out", default=str(OUT))
    ap.add_argument("--table", default=str(TABLE))
    ap.add_argument("--voices-dir", default=str(VOICES_DIR))
    ap.add_argument("--set", choices=list(SETS), default="train")
    ap.add_argument("--start", type=int, default=0)
    ap.add_argument("--n", type=int)
    ap.add_argument("--jobs", type=int, default=6)
    ap.add_argument("--chunk", type=int, default=80)
    ap.add_argument("--seed", type=int, default=SEED)
    a = ap.parse_args(argv)
    {"phrases": cmd_phrases, "gen": cmd_gen, "manifest": cmd_manifest, "augment": cmd_augment,
     "counts": cmd_counts}[a.what](a)


if __name__ == "__main__":
    main()
