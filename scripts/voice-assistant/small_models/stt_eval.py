#!/usr/bin/env python3
"""Transcribe the semi-synthetic STT corpus (stt_corpus.py) with faster-whisper
and score WER and built-in intent success.

transcribe: one CSV per configuration under $KIVI_PRIVATE/stt-runs/
  python stt_eval.py transcribe --name turbo_b5 --model large-v3-turbo --compute-type int8 --beam-size 5
  python stt_eval.py transcribe --name turbo_b5_prompt --initial-prompt auto
score: WER (Phase 2 normalization v1.1 of webos-voice-satellite's tools/audio-rig/p2_score.py,
  numerals spelled on both sides) and the share of control phrases that Home
  Assistant's built-in Spanish intents still resolve to the right call
  (agent_eval/builtin_intents.py: hassil, fake inventory)
  python stt_eval.py score turbo_b5 turbo_b5_prompt

The transcribe call mirrors wyoming-faster-whisper 2.4.0 (language fixed, beam
size, optional initial prompt; no VAD filter, no hotwords). Needs faster-whisper
(CUDA or CPU); score needs hassil==3.12.1, home-assistant-intents==2026.10.6 and pyyaml.
"""
import argparse
import csv
import math
import os
import sys
import time
from pathlib import Path

HERE = Path(__file__).resolve().parent
AGENT_EVAL = Path(os.environ.get("AGENT_EVAL_DIR", HERE.parent / "agent_eval"))
WEBOS = Path(os.environ.get("WEBOS_REPO", Path.home() / "webos-voice-satellite"))
PRIVATE = Path(os.environ.get("KIVI_PRIVATE", WEBOS / "devices" / "private" / "kivi"))
sys.path.insert(0, str(AGENT_EVAL))
sys.path.insert(0, str(WEBOS / "tools" / "audio-rig"))
CORPUS = PRIVATE / "stt-v1"
RUNS = PRIVATE / "stt-runs"
EXCLUDED_VOICES = {"mls9972", "mls10246"}  # es_ES-mls_*-low: unintelligible even digitally (P2 session 2)


def auto_prompt():
    """Initial prompt built only from what HA knows: area and entity names/aliases
    of the exposed (here: fake) inventory, plus the assistant's name."""
    import ha_fake
    home = ha_fake.FakeHome()
    words = []
    for a in home.areas.values():
        words += [a["name"]] + a.get("aliases", [])
    for e in home.entities.values():
        words += [e["name"]] + e.get("aliases", [])
    seen, out = set(), []
    for w in words:
        if w.lower() not in seen:
            seen.add(w.lower())
            out.append(w)
    return "Kivi, asistente de casa. " + ", ".join(out) + "."


# Generic command vocabulary of HA's Spanish intents (verbs and a few nouns); no test sentences.
VERBS_ES = ("Enciende, apaga, sube, baja, abre, cierra, para, pausa, reanuda, silencia, quita el silencio, "
            "pon, temporizador, minutos, volumen, brillo, por ciento, grados")


def auto_prompt_verbs():
    return auto_prompt() + " " + VERBS_ES + "."


def names_only():
    """The names part of auto_prompt without the assistant's name (as --hass-token would add it)."""
    return auto_prompt().split(". ", 1)[1]


PROMPTS = {
    "auto": auto_prompt,                                   # "Kivi, asistente de casa." + names
    "auto+verbs": auto_prompt_verbs,                       # the above + verbs
    "verbs": lambda: VERBS_ES + ".",                       # static verbs only (works on 2.4.0)
    "verbs+names": lambda: VERBS_ES + ". " + names_only(),  # order of 3.8.1: static prompt first, HA names after
}


def cmd_transcribe(a):
    from faster_whisper import WhisperModel
    import soundfile as sf
    prompt = PROMPTS.get(a.initial_prompt, lambda: a.initial_prompt)() or None
    hot = auto_prompt() if a.hotwords == "auto" else (a.hotwords or None)
    kw = {"cpu_threads": a.cpu_threads} if a.cpu_threads else {}
    t_load = time.perf_counter()
    model = WhisperModel(a.model, device=a.device, compute_type=a.compute_type, **kw)
    t_load = time.perf_counter() - t_load
    rows = list(csv.DictReader(open(CORPUS / "manifest.csv")))
    if not a.all_voices:
        rows = [r for r in rows if r["voice"] not in EXCLUDED_VOICES]
    if a.conds:
        rows = [r for r in rows if r["cond"] in a.conds]
    if a.limit:
        rows = rows[: a.limit]
    RUNS.mkdir(parents=True, exist_ok=True)
    out = RUNS / f"{a.name}.csv"
    x, _ = sf.read(CORPUS / rows[0]["cond"] / rows[0]["file"], dtype="float32")  # warm-up, not timed
    list(model.transcribe(x, language="es", beam_size=a.beam_size, initial_prompt=prompt, hotwords=hot)[0])
    with open(out, "w", newline="") as fh:
        w = csv.DictWriter(fh, fieldnames=["id", "group", "kind", "voice", "cond", "text", "hyp", "audio_s", "stt_s"])
        w.writeheader()
        for i, r in enumerate(rows):
            x, fs = sf.read(CORPUS / r["cond"] / r["file"], dtype="float32")
            t0 = time.perf_counter()
            segs, _info = model.transcribe(x, language="es", beam_size=a.beam_size, initial_prompt=prompt,
                                           hotwords=hot)
            hyp = " ".join(s.text.strip() for s in segs).strip()
            dt = time.perf_counter() - t0
            w.writerow(dict(id=r["id"], group=r["group"], kind=r["kind"], voice=r["voice"], cond=r["cond"],
                            text=r["text"], hyp=hyp, audio_s=round(len(x) / fs, 2), stt_s=round(dt, 3)))
            if i % 200 == 0:
                print(a.name, i, len(rows), file=sys.stderr, flush=True)
    print(f"{out} (model load {t_load:.1f} s, excluded)")


def wilson(k, n, z=1.96):
    if n == 0:
        return (math.nan, math.nan)
    p = k / n
    d = 1 + z * z / n
    c = (p + z * z / (2 * n)) / d
    h = z * math.sqrt(p * (1 - p) / n + z * z / (4 * n * n)) / d
    return c - h, c + h


def wer(ref_text, hyp_text):
    """P2 normalization v1.1, with numerals spelled on BOTH sides (the phrase set has digits)."""
    import p2_score
    ref, _ = p2_score.normalize((ref_text or "").replace("%", " por ciento "), spell_numbers=True)
    hyp, _ = p2_score.normalize((hyp_text or "").replace("%", " por ciento "), spell_numbers=True)
    ref, hyp = p2_score._v11(ref), p2_score._v11(hyp)
    return p2_score.edit_distance(ref, hyp), len(ref)


def intent_scorer():
    """Returns f(phrase_id, text) -> 'pass' | 'fail' | 'nomatch' | ... using the
    built-in intent matcher of builtin_intents.py against the fake inventory."""
    import builtin_intents as bi
    import grade
    import ha_fake
    import run as ae
    home = ha_fake.FakeHome()
    match = bi.build_matcher(home)
    tool_names = [t["function"]["name"] for t in ha_fake.build_tools()]
    expect = {i["id"]: i["turns"][0]["expect"] for i in ae.load_phrases()}

    def score(pid, text):
        res = match(bi.remove_punctuation(text))
        if res is None:
            return "nomatch"
        _intent, tool, slots = bi.to_call(res, home, tool_names)
        if tool is None:
            return "matched-other"
        _result, meta = home.execute(tool, slots)
        call = {"name": tool, "args": slots, "ok": meta["ok"], "targets": meta["targets"], "meta_args": meta["args"]}
        g = grade.grade_turn(bi._strip_answer(expect[pid]), {"rounds": [{"tool_calls": [call]}], "final_text": "?"},
                             ha_fake.READ_ONLY_TOOLS)
        return g["status"]
    return score


def cmd_score(a):
    score = intent_scorer()
    ref_ok = {}
    print("| config | cond | n | WER | items WER=0 | built-in intent pass (control) | p50 / p90 STT s |")
    print("| --- | --- | --- | --- | --- | --- | --- |")
    for name in a.names:
        rows = [r for r in csv.DictReader(open(RUNS / f"{name}.csv"))
                if a.all_voices or r["voice"] not in EXCLUDED_VOICES]
        by = {}
        for r in rows:
            by.setdefault(r["cond"], []).append(r)
        for cond in sorted(by, key=lambda c: (c != "clean", c)):
            rs = by[cond]
            err = nref = zero = ip = ic = 0
            for r in rs:
                e, n = wer(r["text"], r["hyp"])
                err += e
                nref += n
                zero += e == 0
                if r["group"] == "control":
                    if r["id"] not in ref_ok:
                        ref_ok[r["id"]] = score(r["id"], r["text"])
                    if ref_ok[r["id"]] == "pass":  # only phrases the built-in agent handles from the reference
                        ic += 1
                        ip += score(r["id"], r["hyp"]) == "pass"
            t = sorted(float(r["stt_s"]) for r in rs)
            lo, hi = wilson(ip, ic)
            print(f"| {name} | {cond} | {len(rs)} | {err / nref:.3f} | {zero}/{len(rs)} | {ip}/{ic} ({ip / max(ic, 1):.0%}, "
                  f"{lo:.0%}-{hi:.0%}) | {t[len(t) // 2]:.2f} / {t[int(len(t) * 0.9)]:.2f} |")
    n_ok = sum(v == "pass" for v in ref_ok.values())
    print(f"\nReference texts the built-in agent resolves correctly: {n_ok}/{len(ref_ok)} control phrases "
          f"(intent pass is scored on those only).")


def mcnemar_p(b, c):
    """Exact two-sided McNemar p-value for b and c discordant pairs."""
    n = b + c
    if n == 0:
        return 1.0
    k = min(b, c)
    p = sum(math.comb(n, i) for i in range(k + 1)) / 2 ** n
    return min(1.0, 2 * p)


def cmd_paired(a):
    """Paired comparison of two runs on the same items: built-in intent pass on
    control phrases (those the reference passes) and per-item WER changes."""
    score = intent_scorer()
    load = lambda n: {(r["id"], r["voice"], r["cond"]): r for r in csv.DictReader(open(RUNS / f"{n}.csv"))
                      if r["voice"] not in EXCLUDED_VOICES}
    A, B = load(a.base), load(a.other)
    keys = sorted(set(A) & set(B))
    ref_ok = {}
    print(f"| {a.base} -> {a.other} | n (control) | pass base | pass other | fixed | broken | McNemar p | "
          f"WER items better / worse (all groups) |")
    print("| --- | --- | --- | --- | --- | --- | --- | --- |")
    for cond in sorted({k[2] for k in keys}, key=lambda c: (c != "clean", c)):
        n = pa = pb = fx = br = wb = ww = 0
        for k in (k for k in keys if k[2] == cond):
            ra, rb = A[k], B[k]
            ea, eb = wer(ra["text"], ra["hyp"])[0], wer(rb["text"], rb["hyp"])[0]
            wb += eb < ea
            ww += eb > ea
            if ra["group"] != "control":
                continue
            if ra["id"] not in ref_ok:
                ref_ok[ra["id"]] = score(ra["id"], ra["text"]) == "pass"
            if not ref_ok[ra["id"]]:
                continue
            x, y = score(ra["id"], ra["hyp"]) == "pass", score(rb["id"], rb["hyp"]) == "pass"
            n += 1
            pa += x
            pb += y
            fx += y and not x
            br += x and not y
        print(f"| {cond} | {n} | {pa} | {pb} | {fx} | {br} | {mcnemar_p(fx, br):.3f} | {wb} / {ww} |")


def main():
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    sub = ap.add_subparsers(dest="cmd", required=True)
    t = sub.add_parser("transcribe")
    t.add_argument("--name", required=True)
    t.add_argument("--model", default="large-v3-turbo")
    t.add_argument("--device", default="cuda")
    t.add_argument("--compute-type", default="int8")
    t.add_argument("--beam-size", type=int, default=5)
    t.add_argument("--initial-prompt", default=None, help="text or one of: " + ", ".join(PROMPTS))
    t.add_argument("--hotwords", default=None, help="text, or 'auto' (faster-whisper only; not in wyoming 2.4.0)")
    t.add_argument("--cpu-threads", type=int, default=0)
    t.add_argument("--conds", nargs="*")
    t.add_argument("--limit", type=int, default=0)
    t.add_argument("--all-voices", action="store_true", help="include the two unintelligible mls voices")
    s = sub.add_parser("score")
    s.add_argument("names", nargs="+")
    s.add_argument("--all-voices", action="store_true", help="include the two unintelligible mls voices")
    p = sub.add_parser("paired")
    p.add_argument("base")
    p.add_argument("other")
    a = ap.parse_args()
    {"transcribe": cmd_transcribe, "score": cmd_score, "paired": cmd_paired}[a.cmd](a)


if __name__ == "__main__":
    main()
