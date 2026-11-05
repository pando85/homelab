#!/usr/bin/env python3
"""Paraphrase kivi-fake-v1 command items with a LOCAL LLM and keep only label-preserving ones.

A private Ollama on the loopback interface (model qwen3:14b by default, thinking off, JSON output)
rewrites a stratified sample of template commands in a requested register. Each paraphrase is
validated automatically and dropped unless:
  * target: the entity name or one of its aliases (or, for a group, the area name and the
    domain word) is still present (fuzzy, rapidfuzz partial ratio >= 90 on folded text), and the
    area is still present whenever the source named it;
  * value: numbers_es parses the same value (percent, temperature, duration);
  * intent: a cue word of the intent is present and no cue of its opposite (on/off, open/close,
    up/down, mute/unmute, pause/resume, next/previous) is;
  * shape: one request (no " y " joining a second verb), not identical to the source, <= 25 words.

  OLLAMA_HOST=127.0.0.1:11510 python paraphrase.py --data $POC/data/kivi-fake-v1 --n 380
  -> items_llm.jsonl (kept) and paraphrase_rejected.jsonl (with the reason)
Then merge: python paraphrase.py --data ... --merge   -> items.jsonl (template + kept llm)
"""
import argparse
import collections
import json
import os
import random
import re
import sys
import time
import urllib.request
from pathlib import Path

import yaml

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE))
import numbers_es as ne  # noqa: E402
import schema as sc  # noqa: E402

REGISTERS = [
    "más coloquial, como se habla en casa en España",
    "más educada y un poco más larga",
    "con otro orden de palabras",
    "como lo diría alguien de México",
    "muy corta, casi telegráfica, pero con verbo",
    "con una muletilla o duda al principio (eh, a ver, oye)",
    "indirecta pero clara (¿me puedes...?, quiero que...)",
]
PROMPT = """Reescribe esta orden para un asistente de voz de casa, en español, de forma {reg}.
Reglas: la orden debe hacer EXACTAMENTE lo mismo; conserva el nombre exacto del dispositivo{area_rule};
conserva el número y su unidad si hay; una sola orden; sin comillas ni explicaciones.
Orden: "{text}"
Devuelve JSON: {{"paraphrases": ["...", "..."]}} con 2 versiones distintas."""

CUES = {
    "turn_on": r"enciend|encend|prend|activ|conect|\bpon|dale|da la|ilumin|marcha|\bon\b",
    "turn_off": r"apag|desconect|quit|fuera|cort|\boff\b",
    "open_cover": r"abr|abier|sub|levant|recog|arriba",
    "close_cover": r"cierr|cerr|baj|echa|chap|abajo",
    "cover_position": r".",
    "stop_cover": r"par[ae]|det[eé]n|stop|fren",
    "light_set": r".",
    "set_volume": r"volumen",
    "volume_up": r"sub|alto|mas volumen|no se oye|subele",
    "volume_down": r"baj|bajit|menos volumen|bajale",
    "mute": r"silenci|mute|call|sin sonido|quita el sonido|quitale el sonido",
    "unmute": r"quita el silencio|quitale el silencio|activa el sonido|desmute|quitale el mute|sonido",
    "pause": r"paus|par[ae]|det[eé]n",
    "resume": r"reanud|contin|sigue|siga|play|quita la pausa",
    "next": r"siguiente|pasa|salta|cambia|que sigue|adelant",
    "previous": r"anterior|atras|regres|vuelve|de antes",
    "climate_set": r"grado|temperatura|\bpon|ajust|\ba \d",
    "fan_speed": r".",
    "start_timer": r"temporizador|timer|avis|cuenta|cronometro",
    "cancel_timer": r"temporizador|timer",
}
OPPOSITE = {"turn_on": "turn_off", "turn_off": "turn_on", "open_cover": "close_cover", "close_cover": "open_cover",
            "volume_up": "volume_down", "volume_down": "volume_up", "mute": "unmute", "pause": "resume",
            "resume": "pause", "next": "previous", "previous": "next"}
OPP_CUES = {  # cues of the opposite intent that are unambiguous enough to reject on
    "turn_on": r"apag|desconect", "turn_off": r"enciend|prend", "open_cover": r"cierr|baj|abajo",
    "close_cover": r"abr|sub|arriba", "volume_up": r"baj|menos", "volume_down": r"sub|mas alto|subele",
    "mute": r"quita el silencio", "pause": r"reanud|contin|play", "resume": r"paus",
    "next": r"anterior|regres", "previous": r"siguiente"}


def chat(host, model, prompt, timeout=120):
    body = json.dumps({"model": model, "think": False, "stream": False, "format": "json",
                       "options": {"temperature": 0.8, "num_ctx": 2048},
                       "messages": [{"role": "user", "content": prompt}]}).encode()
    req = urllib.request.Request(f"http://{host}/api/chat", data=body, headers={"Content-Type": "application/json"})
    with urllib.request.urlopen(req, timeout=timeout) as r:
        return json.loads(r.read())["message"]["content"]


def present(needle, hay, thr=90):
    from rapidfuzz import fuzz
    n, h = ne.fold(needle), ne.fold(hay)
    return n in h or fuzz.partial_ratio(n, h) >= thr


def validate(item, para, home):
    """'' if the paraphrase keeps the label, else the reason."""
    act = item["actions"][0]
    intent, target, value = act["intent"], act["target"], act["value"]
    f = ne.fold(para)
    if ne.fold(item["text"]) == f:
        return "identical"
    if len(para.split()) > 25:
        return "too long"
    if re.search(r"\by (luego |despues |tambien )?(enciende|apaga|sube|baja|abre|cierra|pon|para|pausa|silencia)", f):
        return "two requests"
    if target:
        src = ne.fold(item["text"])
        if target.startswith("e:"):
            e = home["entities"][target[2:]]
            if not any(present(n, para) for n in [e["name"]] + e.get("aliases", [])):
                return "target name lost"
            a = home["areas"][e["area"]]
            anames = [a["name"]] + a.get("aliases", [])
            if any(ne.fold(x) in src for x in anames) and not any(present(x, para) for x in anames):
                return "area lost"
        elif target.startswith("a:"):
            _, aid, g = target.split(":")
            a = home["areas"][aid]
            anames = [a["name"]] + a.get("aliases", [])
            if any(ne.fold(x) in src for x in anames) and not any(present(x, para) for x in anames):
                return "area lost"
            word = {"light": r"luz|luces", "cover": r"persiana|cortina|toldo|estor", "all": r"todo"}[g]
            if not re.search(word, f):
                return "group word lost"
        else:
            if not re.search(r"luz|luces", f):
                return "group word lost"
    if target and not target.startswith("h:"):
        aid = home["entities"][target[2:]]["area"] if target.startswith("e:") else target.split(":")[1]
        own = {ne.fold(x) for x in [home["areas"][aid]["name"]] + home["areas"][aid].get("aliases", [])}
        src = ne.fold(item["text"])
        for oid, oa in home["areas"].items():
            for x in [oa["name"]] + oa.get("aliases", []):
                fx = ne.fold(x)
                if oid != aid and fx not in own and re.search(rf"\b{re.escape(fx)}\b", f) and fx not in src \
                        and not any(fx in o for o in own):
                    return "other area added"
    vk = sc.INTENTS[intent][4]
    if vk and ne.parse_value(vk, para) != value:
        return f"value {ne.parse_value(vk, para)} != {value}"
    if not re.search(CUES[intent], f):
        return "intent cue lost"
    if intent in OPP_CUES and re.search(OPP_CUES[intent], f):
        return "opposite intent cue"
    return ""


def cmd_generate(a):
    host = os.environ.get("OLLAMA_HOST", "127.0.0.1:11510")
    items = [json.loads(x) for x in open(a.data / "items_template.jsonl", encoding="utf-8")]
    homes = {h: yaml.safe_load(open(a.data / "homes" / f"{h}.yaml", encoding="utf-8")) for h in
             {i["home"] for i in items}}
    rng = random.Random(a.seed)
    cmds = [i for i in items if i["kind"] == "command"]
    by = collections.defaultdict(list)
    for i in cmds:
        by[(i["home"], i["style"])].append(i)
    pick = []
    while len(pick) < a.n and any(by.values()):
        for k in sorted(by):
            if by[k] and len(pick) < a.n:
                pick.append(by[k].pop(rng.randrange(len(by[k]))))
    kept, rej = open(a.data / "items_llm.jsonl", "w", encoding="utf-8"), \
        open(a.data / "paraphrase_rejected.jsonl", "w", encoding="utf-8")
    stats = collections.Counter()
    t0 = time.time()
    for n, it in enumerate(pick):
        reg = rng.choice(REGISTERS)
        tgt = it["actions"][0]["target"]
        area_rule = " y de la habitación si aparece" if tgt else ""
        try:
            out = json.loads(chat(host, a.model, PROMPT.format(reg=reg, text=it["text"], area_rule=area_rule)))
            paras = [p for p in out.get("paraphrases", []) if isinstance(p, str)][:2]
        except Exception as exc:  # noqa: BLE001  (a bad JSON reply only loses this item)
            stats["llm error"] += 1
            print("error", it["id"], exc, file=sys.stderr)
            continue
        for k, p in enumerate(paras):
            p = p.strip().strip('"').strip()
            why = validate(it, p, homes[it["home"]])
            rec = dict(it, id=f"{it['id']}-p{k}", text=p, spoken=p, source="llm", template=it["id"],
                       style=it["style"], register=reg)
            if why:
                stats["rejected: " + why] += 1
                rej.write(json.dumps(dict(rec, reject=why), ensure_ascii=False) + "\n")
            else:
                stats["kept"] += 1
                kept.write(json.dumps(rec, ensure_ascii=False) + "\n")
        if n % 25 == 0:
            print(n, len(pick), f"{time.time() - t0:.0f}s", dict(stats), file=sys.stderr, flush=True)
    kept.close()
    rej.close()
    print(dict(stats))


def cmd_merge(a):
    """Re-validate every paraphrase candidate (kept and rejected) with the current rules, then
    write items.jsonl = template items + kept paraphrases, with the stratified audio subset."""
    tpl = [json.loads(x) for x in open(a.data / "items_template.jsonl", encoding="utf-8")]
    cands = [json.loads(x) for x in open(a.data / "items_llm.jsonl", encoding="utf-8")] + \
        [json.loads(x) for x in open(a.data / "paraphrase_rejected.jsonl", encoding="utf-8")]
    homes = {h: yaml.safe_load(open(a.data / "homes" / f"{h}.yaml", encoding="utf-8")) for h in
             {i["home"] for i in tpl}}
    src = {i["id"]: i for i in tpl}
    llm, stats = [], collections.Counter()
    with open(a.data / "paraphrase_rejected.final.jsonl", "w", encoding="utf-8") as rej:
        for c in cands:
            c.pop("reject", None)
            why = validate(src[c["template"]], c["text"], homes[c["home"]])
            stats["kept" if not why else "rejected: " + why.split(" ")[0]] += 1
            if why:
                rej.write(json.dumps(dict(c, reject=why), ensure_ascii=False) + "\n")
            else:
                llm.append(c)
    print(dict(stats))
    llm.sort(key=lambda x: x["id"])
    for x in llm:
        x.pop("register", None)
        if not re.search(r"\d", x["text"]):
            x["spoken"] = x["text"]
    items = tpl + llm
    # Stratified audio subset: up to a.audio items, balanced over (kind, style, source).
    rng = random.Random(a.seed)
    chosen = set()
    prev = a.data / "items.jsonl"
    if prev.exists():  # keep an existing audio subset stable (audio may already be synthesised)
        ids = {x["id"] for x in items}
        chosen = {x["id"] for x in map(json.loads, open(prev, encoding="utf-8")) if x.get("audio")} & ids
    by = collections.defaultdict(list)
    for x in items:
        by[(x["kind"], x["style"], x["source"])].append(x)
    for v in by.values():
        rng.shuffle(v)
    while len(chosen) < a.audio and any(by.values()):
        for k in sorted(by):
            if by[k] and len(chosen) < a.audio:
                x = by[k].pop()["id"]
                if x not in chosen:
                    chosen.add(x)
    with open(a.data / "items.jsonl", "w", encoding="utf-8") as fh:
        for x in items:
            x["audio"] = x["id"] in chosen
            fh.write(json.dumps(x, ensure_ascii=False) + "\n")
    print(len(items), "items,", len(chosen), "in the audio subset")


def main():
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--data", type=Path, required=True)
    ap.add_argument("--model", default="qwen3:14b")
    ap.add_argument("--n", type=int, default=380)
    ap.add_argument("--seed", type=int, default=3)
    ap.add_argument("--merge", action="store_true")
    ap.add_argument("--audio", type=int, default=500)
    a = ap.parse_args()
    (cmd_merge if a.merge else cmd_generate)(a)


if __name__ == "__main__":
    main()
