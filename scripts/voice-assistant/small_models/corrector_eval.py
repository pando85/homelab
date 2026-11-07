#!/usr/bin/env python3
"""STT-corrector evaluation for Kivi: a small LLM rewrites a Whisper transcript
into the intended command, constrained to the home's area and device names, and
must leave non-command speech unchanged.

Scored with Home Assistant's built-in Spanish intent matching (agent_eval/
builtin_intents.py, hassil, fake inventory) on Whisper transcripts of the
synthetic corpus (stt_eval.py runs):
  recovered    control phrase whose raw transcript fails but corrected passes
  broken       control phrase whose raw transcript passes but corrected fails
  false cmd    general/live phrase that does not match a built-in intent raw
               but matches one after correction (a command the user never gave)
  changed      general/live phrases whose text the corrector changed (normalized)
Model "none" is the identity baseline.

  python corrector_eval.py --stt turbo_b5:snr+5 --models none qwen2.5:1.5b gemma3:1b
"""
import argparse
import csv
import json
import os
import re
import sys
import time
import unicodedata
import urllib.request
from pathlib import Path

HERE = Path(__file__).resolve().parent
AGENT_EVAL = Path(os.environ.get("AGENT_EVAL_DIR", HERE.parent / "agent_eval"))
WEBOS = Path(os.environ.get("WEBOS_REPO", Path.home() / "webos-voice-satellite"))
PRIVATE = Path(os.environ.get("KIVI_PRIVATE", WEBOS / "devices" / "private" / "kivi"))
sys.path.insert(0, str(AGENT_EVAL))
sys.path.insert(0, str(HERE))

SYSTEM = (
    "Corriges transcripciones de un reconocedor de voz en español para el asistente de una casa.\n"
    "La transcripción puede tener palabras mal oídas (por ejemplo \"para\" por \"apaga\", \"pesiana\" por "
    "\"persiana\", nombres de zonas o dispositivos deformados).\n"
    "Reglas:\n"
    "1. Si es una orden o pregunta sobre la casa, devuélvela corregida, con la misma intención, usando los "
    "nombres exactos de la lista.\n"
    "2. Si no es sobre la casa (cultura general, noticias, conversación), devuélvela EXACTAMENTE igual.\n"
    "3. No inventes órdenes, dispositivos ni números. No añadas explicaciones.\n"
    "Zonas: {areas}.\nDispositivos: {devices}.\n"
    "Responde solo con JSON."
)
# Few-shot pairs (synthetic, not in the test set): one fix, one unchanged question.
FEW_SHOT = [("para la luz del baño", "Apaga la luz del baño"),
            ("enciende la lampara de la mesia", "Enciende la lamparita de noche"),
            ("quién escribió la odisea", "quién escribió la odisea")]
SCHEMA = {"type": "object", "properties": {"texto": {"type": "string"}}, "required": ["texto"]}
SERVER = os.environ.get("KIVI_OLLAMA", "http://127.0.0.1:11500")


def post(server, path, body, timeout=300):
    req = urllib.request.Request(server.rstrip("/") + path, json.dumps(body).encode(), {"Content-Type": "application/json"})
    with urllib.request.urlopen(req, timeout=timeout) as r:
        return json.load(r)


def simple(t):
    t = unicodedata.normalize("NFD", t.lower())
    t = "".join(c for c in t if not unicodedata.combining(c))
    return " ".join(re.sub(r"[^a-z0-9 ]", " ", t).split())


def make_corrector(server, model, num_ctx):
    import ha_fake
    home = ha_fake.FakeHome()
    areas = ", ".join(sorted({n for a in home.areas.values() for n in [a["name"]] + a.get("aliases", [])}))
    devices = ", ".join(sorted({n for e in home.entities.values() for n in [e["name"]] + e.get("aliases", [])}))
    system = SYSTEM.format(areas=areas, devices=devices)

    def messages(text):
        m = [{"role": "system", "content": system}]
        for raw, fixed in FEW_SHOT:
            m += [{"role": "user", "content": f"Transcripción: «{raw}»"},
                  {"role": "assistant", "content": json.dumps({"texto": fixed}, ensure_ascii=False)}]
        return m + [{"role": "user", "content": f"Transcripción: «{text}»"}]

    def correct(text):
        body = {"model": model, "stream": False, "format": SCHEMA, "think": False, "keep_alive": "10m",
                "options": {"temperature": 0, "num_predict": 64, "num_ctx": num_ctx},
                "messages": messages(text)}
        try:
            r = post(server, "/api/chat", body)
        except Exception:
            body.pop("think")
            r = post(server, "/api/chat", body)
        try:
            return json.loads(r["message"]["content"])["texto"].strip() or text
        except Exception:
            return text
    return correct


def main():
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--server", default=SERVER)
    ap.add_argument("--stt", nargs="+", required=True, help="<run>:<cond> ...")
    ap.add_argument("--models", nargs="+", required=True)
    ap.add_argument("--num-ctx", type=int, default=1024)
    ap.add_argument("--dump", default="", help="write per-item CSV (private) to this directory")
    a = ap.parse_args()
    import stt_eval
    score = stt_eval.intent_scorer()
    import builtin_intents as bi
    import ha_fake
    home = ha_fake.FakeHome()
    match = bi.build_matcher(home)

    def any_match(text):
        r = match(bi.remove_punctuation(text))
        return r is not None and r.intent.name not in ("HassMediaSearchAndPlay",)

    rows = []
    for spec in a.stt:
        name, cond = spec.split(":")
        for r in csv.DictReader(open(PRIVATE / "stt-runs" / f"{name}.csv")):
            if r["cond"] == cond and r["voice"] not in stt_eval.EXCLUDED_VOICES:
                rows.append(r)
    ref_ok = {}
    for r in rows:
        if r["group"] == "control" and r["id"] not in ref_ok:
            ref_ok[r["id"]] = score(r["id"], r["text"]) == "pass"
    print(f"items: {len(rows)} from {' '.join(a.stt)}; control phrases the built-in agent handles from the reference: "
          f"{sum(ref_ok.values())}/{len(ref_ok)}")
    print("| corrector | control pass raw -> corrected | recovered | broken | general/live false commands | "
          "general/live changed | p50 / p90 ms (warm) |")
    print("| --- | --- | --- | --- | --- | --- | --- |", flush=True)
    for model in a.models:
        corr = (lambda t: t) if model == "none" else make_corrector(a.server, model, a.num_ctx)
        corr("Enciende la luz")  # warm-up (model load excluded)
        raw_pass = cor_pass = rec = brk = fc = chg = nong = nctl = 0
        ts, dump = [], []
        for r in rows:
            t0 = time.perf_counter()
            c = corr(r["hyp"])
            ts.append((time.perf_counter() - t0) * 1000)
            if r["group"] == "control":
                if not ref_ok[r["id"]]:
                    continue
                nctl += 1
                p0 = score(r["id"], r["hyp"]) == "pass"
                p1 = score(r["id"], c) == "pass"
                raw_pass += p0
                cor_pass += p1
                rec += (not p0) and p1
                brk += p0 and not p1
            else:
                nong += 1
                fc += (not any_match(r["hyp"])) and any_match(c)
                chg += simple(c) != simple(r["hyp"])
            dump.append(dict(id=r["id"], voice=r["voice"], cond=r["cond"], text=r["text"], hyp=r["hyp"], corrected=c))
        ts.sort()
        print(f"| {model} | {raw_pass}/{nctl} -> {cor_pass}/{nctl} | {rec} | {brk} | {fc}/{nong} | {chg}/{nong} | "
              f"{ts[len(ts) // 2]:.0f} / {ts[int(len(ts) * 0.9)]:.0f} |", flush=True)
        if a.dump:
            Path(a.dump).mkdir(parents=True, exist_ok=True)
            with open(Path(a.dump) / f"corr_{model.replace(':', '_').replace('/', '_')}.csv", "w", newline="") as fh:
                w = csv.DictWriter(fh, fieldnames=list(dump[0]))
                w.writeheader()
                w.writerows(dump)


if __name__ == "__main__":
    main()
