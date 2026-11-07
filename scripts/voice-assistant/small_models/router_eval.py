#!/usr/bin/env python3
"""Router evaluation for Kivi: split a transcript into "casa" (handled in the
house: Home Assistant's built-in intents or a small local agent) and "kiais"
(general knowledge, live web information, chat: the cloud gateway).

Methods, against a local Ollama server (native API):
  builtin       "casa" when HA's strict built-in matching (prefer_local_intents) matches
  emb:<model>   multilingual sentence embedding, nearest centroid and 3-NN over
                ROUTER_TRAIN (synthetic phrases, disjoint from the test set)
  llm:<model>   small LLM with JSON-schema constrained output {"ruta": "casa"|"kiais"}
Every non-builtin method is also reported combined with builtin (built-in match
first, the classifier only for the rest), which is how it would sit behind
prefer_local_intents.

Test sets: the agent_eval phrases (references) or Whisper transcripts of them
(stt_eval.py runs, one condition), so routing is also scored on STT errors.

  python router_eval.py --methods builtin emb:paraphrase-multilingual llm:qwen2.5:0.5b --stt turbo_b5:snr+5
"""
import argparse
import csv
import json
import math
import os
import sys
import time
import urllib.request
from pathlib import Path

HERE = Path(__file__).resolve().parent
AGENT_EVAL = Path(os.environ.get("AGENT_EVAL_DIR", HERE.parent / "agent_eval"))
WEBOS = Path(os.environ.get("WEBOS_REPO", Path.home() / "webos-voice-satellite"))
PRIVATE = Path(os.environ.get("KIVI_PRIVATE", WEBOS / "devices" / "private" / "kivi"))
sys.path.insert(0, str(AGENT_EVAL))

# Phrases answered in the house although they are not device commands.
LIVE_LOCAL = {"l01", "l03", "l04"}      # outside weather (HA weather entity), time, date
LIVE_EITHER = {"l05", "l06"}            # calendar: a local agent with the calendar tool, or Kiais; not scored

# Synthetic training phrases for the embedding router. Written for this study,
# deliberately different wording from phrases_es.yaml (no shared sentences).
ROUTER_TRAIN = {
    "casa": [
        "enciende las luces de la habitación", "apaga la lámpara", "baja las persianas",
        "sube un poco la persiana de la cocina", "cierra el toldo", "pon la calefacción a veinte grados",
        "apaga el aire", "pon la tele más alta", "baja el volumen del altavoz", "silencia el televisor",
        "pausa la música", "siguiente canción", "pon un temporizador de cinco minutos", "cancela la alarma",
        "cuánto queda del temporizador", "enciende el ventilador", "apaga la cafetera",
        "qué temperatura hay en la cocina", "está encendida la luz del baño", "hay alguna luz encendida",
        "están cerradas las persianas", "qué hora es ahora", "qué día es mañana", "hace frío fuera",
        "qué humedad hay en el salón", "pon la luz al cincuenta por ciento", "ilumina el pasillo",
        "apágalo todo", "deja la luz más tenue", "abre la persiana del baño", "para la persiana",
        "me voy a dormir apaga todo", "quiero más luz aquí", "ponme la tele un poco más baja",
        "baja la temperatura del aire", "enciende la tele", "quita la música", "vuelve a poner la serie",
        "qué tiempo hace en la terraza", "enciende la luz de la mesilla",
    ],
    "kiais": [
        "quién ganó el mundial de dos mil diez", "cuál es el río más largo de europa",
        "cuántos kilómetros hay de madrid a barcelona", "explícame qué es un agujero negro",
        "dime una frase célebre", "cómo se hace una paella", "qué significa la palabra efímero",
        "cuántos habitantes tiene méxico", "quién fue cervantes", "cuéntame algo interesante",
        "cuánto es ciento veinte entre ocho", "traduce perro al alemán", "cuál es la montaña más alta",
        "qué noticias hay de economía", "cómo va la bolsa hoy", "a qué hora juega el barcelona",
        "qué película me recomiendas", "hay huelga de metro mañana", "qué tiempo hará el lunes en sevilla",
        "cuál es el precio de la gasolina hoy", "escribe un poema corto", "cuántos años tiene la tierra",
        "por qué el cielo es azul", "qué es la fotosíntesis", "quién es el presidente de francia",
        "recomiéndame un libro", "cómo se llama la capital de australia", "cuántas calorías tiene una manzana",
        "dame ideas para cenar", "qué pasó hoy en el mundo", "a cuánto está el euro frente al dólar",
        "quién canta esta canción famosa", "cuántos días faltan para navidad", "hola qué tal estás",
        "cuéntame una historia para dormir", "cuál es tu color favorito", "qué es la inteligencia artificial",
        "cuándo abre el supermercado", "cómo se escribe hipopótamo", "qué hora es en nueva york",
    ],
}

LLM_SYSTEM = (
    "Eres el enrutador del asistente de voz de una casa. Decide a quién va la petición.\n"
    "casa: controlar o consultar dispositivos de la casa (luces, persianas, toldo, aire, ventilador, tele, "
    "altavoz, cafetera), temporizadores y alarmas, la hora o la fecha de hoy, la temperatura o el tiempo de "
    "la casa o de fuera ahora mismo. También frases indirectas sobre la casa (\"hace calor\", \"está oscuro\").\n"
    "kiais: preguntas de cultura general, cálculos, traducciones, recetas, conversación, chistes, y "
    "información de internet (noticias, deportes, bolsa, tráfico, previsiones de otros días o lugares).\n"
    "Zonas: {areas}. Dispositivos: {devices}.\n"
    "Responde solo con JSON."
)
# Few-shot pairs (synthetic, not in the test set). Without them and without quoting the
# transcript, small models answer the question instead of classifying it.
FEW_SHOT = [("apaga la lámpara del dormitorio", "casa"), ("quién escribió la odisea", "kiais"),
            ("hace frío aquí", "casa"), ("qué noticias hay de deportes", "kiais")]
SCHEMA = {"type": "object", "properties": {"ruta": {"type": "string", "enum": ["casa", "kiais"]}}, "required": ["ruta"]}
SERVER = os.environ.get("KIVI_OLLAMA", "http://127.0.0.1:11500")


def post(server, path, body, timeout=300):
    req = urllib.request.Request(server.rstrip("/") + path, json.dumps(body).encode(), {"Content-Type": "application/json"})
    with urllib.request.urlopen(req, timeout=timeout) as r:
        return json.load(r)


def embed(server, model, texts):
    return post(server, "/api/embed", {"model": model, "input": texts, "keep_alive": "10m"})["embeddings"]


def norm(v):
    n = math.sqrt(sum(x * x for x in v)) or 1.0
    return [x / n for x in v]


def dot(a, b):
    return sum(x * y for x, y in zip(a, b))


def test_items(stt=None):
    import run as ae
    items = []
    for it in ae.load_phrases():
        if it["category"] == "followup" or it["id"] in LIVE_EITHER:
            continue
        label = "casa" if it["category"] == "control" or it["id"] in LIVE_LOCAL else "kiais"
        items.append({"id": it["id"], "category": it["category"], "label": label, "text": it["turns"][0]["text"]})
    if not stt:
        return items
    name, cond = stt.split(":")
    lab = {i["id"]: i for i in items}
    out = []
    for r in csv.DictReader(open(PRIVATE / "stt-runs" / f"{name}.csv")):
        if r["cond"] == cond and r["id"] in lab and r["voice"] not in ("mls9972", "mls10246"):
            out.append(dict(lab[r["id"]], text=r["hyp"], voice=r["voice"]))
    return out


def make_builtin():
    import builtin_intents as bi
    import ha_fake
    home = ha_fake.FakeHome()
    match = bi.build_matcher(home)
    # HA hands HassMediaSearchAndPlay and HassGetState to an LLM agent that can control the house.
    skip = {"HassMediaSearchAndPlay", "HassGetState"}

    def route(text):
        r = match(bi.remove_punctuation(text))
        return "casa" if (r is not None and r.intent.name not in skip) else "kiais"
    return route


def make_emb(server, model):
    train = [(lab, t) for lab, ts in ROUTER_TRAIN.items() for t in ts]
    vecs = [norm(v) for v in embed(server, model, [t for _, t in train])]
    cent = {}
    for lab in ROUTER_TRAIN:
        vs = [v for (l, _), v in zip(train, vecs) if l == lab]
        cent[lab] = norm([sum(c) / len(vs) for c in zip(*vs)])

    def route(text):
        v = norm(embed(server, model, [text])[0])
        knn = sorted(((dot(v, tv), l) for (l, _), tv in zip(train, vecs)), reverse=True)[:3]
        votes = sum(1 if l == "casa" else -1 for _, l in knn)
        c = max(cent, key=lambda lab: dot(v, cent[lab]))
        return {"centroid": c, "knn3": "casa" if votes > 0 else "kiais"}
    return route


def make_llm(server, model, num_ctx=1024):
    import ha_fake
    home = ha_fake.FakeHome()
    areas = ", ".join(sorted({a["name"] for a in home.areas.values()}))
    devices = ", ".join(sorted({e["name"] for e in home.entities.values()}))
    system = LLM_SYSTEM.format(areas=areas, devices=devices)
    stats = {"prompt_tokens": None}

    def messages(text):
        m = [{"role": "system", "content": system}]
        for s, lab in FEW_SHOT:
            m += [{"role": "user", "content": f"Clasifica la petición: «{s}»"},
                  {"role": "assistant", "content": json.dumps({"ruta": lab})}]
        return m + [{"role": "user", "content": f"Clasifica la petición: «{text}»"}]

    def route(text):
        body = {"model": model, "stream": False, "format": SCHEMA, "think": False, "keep_alive": "10m",
                "options": {"temperature": 0, "num_predict": 16, "num_ctx": num_ctx},
                "messages": messages(text)}
        try:
            r = post(server, "/api/chat", body)
        except Exception:  # some models reject "think"
            body.pop("think")
            r = post(server, "/api/chat", body)
        stats["prompt_tokens"] = r.get("prompt_eval_count")
        try:
            return json.loads(r["message"]["content"])["ruta"]
        except Exception:
            return "invalid"
    route.stats = stats
    return route


def score(preds, items):
    n = len(items)
    ok = sum(p == i["label"] for p, i in zip(preds, items))
    casa = [(p, i) for p, i in zip(preds, items) if i["label"] == "casa"]
    kia = [(p, i) for p, i in zip(preds, items) if i["label"] == "kiais"]
    c_ok = sum(p == "casa" for p, _ in casa)
    k_ok = sum(p == "kiais" for p, _ in kia)
    wrong = sorted({i["id"] for p, i in zip(preds, items) if p != i["label"]})
    return {"acc": f"{ok}/{n} ({ok / n:.0%})", "casa_kept": f"{c_ok}/{len(casa)}", "kiais_sent": f"{k_ok}/{len(kia)}",
            "wrong_ids": " ".join(wrong[:14]) + (" ..." if len(wrong) > 14 else "")}


def main():
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--server", default=SERVER)
    ap.add_argument("--methods", nargs="+", required=True)
    ap.add_argument("--stt", default=None, help="<run>:<cond>, e.g. turbo_b5:snr+5")
    ap.add_argument("--num-ctx", type=int, default=1024)
    a = ap.parse_args()
    items = test_items(a.stt)
    print(f"test set: {len(items)} items ({'references' if not a.stt else a.stt})")
    print("| method | accuracy | home kept local | general/live sent to Kiais | p50 / p90 ms (warm) | misrouted ids |")
    print("| --- | --- | --- | --- | --- | --- |", flush=True)
    b = make_builtin()
    base = [b(it["text"]) for it in items]
    for m in a.methods:
        kind, _, model = m.partition(":")
        route = {"builtin": lambda: b, "emb": lambda: make_emb(a.server, model),
                 "llm": lambda: make_llm(a.server, model, a.num_ctx)}[kind]()
        route(items[0]["text"])  # warm-up (model load excluded)
        preds, ts = [], []
        for it in items:
            t0 = time.perf_counter()
            preds.append(route(it["text"]))
            ts.append((time.perf_counter() - t0) * 1000)
        ts.sort()
        lat = f"{ts[len(ts) // 2]:.0f} / {ts[int(len(ts) * 0.9)]:.0f}"
        variants = {"": preds} if not isinstance(preds[0], dict) else {f" {k}": [p[k] for p in preds] for k in preds[0]}
        for suffix, ps in variants.items():
            s = score(ps, items)
            print(f"| {m}{suffix} | {s['acc']} | {s['casa_kept']} | {s['kiais_sent']} | {lat} | {s['wrong_ids']} |")
            if kind != "builtin":
                comb = ["casa" if bb == "casa" else p for bb, p in zip(base, ps)]
                s = score(comb, items)
                print(f"| builtin+{m}{suffix} | {s['acc']} | {s['casa_kept']} | {s['kiais_sent']} | | {s['wrong_ids']} |")
        if kind == "llm" and route.stats["prompt_tokens"]:
            print(f"<!-- {m}: prompt tokens {route.stats['prompt_tokens']} -->")
        sys.stdout.flush()


if __name__ == "__main__":
    main()
