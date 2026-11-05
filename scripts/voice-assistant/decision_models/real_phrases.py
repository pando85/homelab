#!/usr/bin/env python3
"""Templated Spanish commands with gold expectations, generated from an inventory (the fake home
or the owner's real one, read by fetch_inventory.py), for a text-only run of decision_eval.py.

Templates cover what the built-in intents tend to miss ("pon ... al N por ciento", "a la mitad",
"para ...", "apágalo todo en ...", "la siguiente canción en ...") and what they match, plus
state questions, an unknown target and the general/live questions of agent_eval (no action allowed).
Gold targets follow decide.py's semantics (an area plus a kind means every device of that kind in the
area). Output: JSONL; with a real inventory it holds real names, so write it to the private directory.

  python real_phrases.py --inventory $KIVI_PRIVATE/decision-models/inventory_real.yaml --out .../phrases_real.jsonl
"""
import argparse
import json
import os
import random
import sys
from pathlib import Path

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE))
sys.path.insert(0, str(HERE.parent / "agent_eval"))
import decide as D  # noqa: E402


def de(area_name):
    """'de la cocina' / 'del salón' (a rough gender guess from the last letter)."""
    n = area_name.lower()
    if n.startswith(("el ", "la ", "los ", "las ")):
        return "de " + n if not n.startswith("el ") else "del " + n[3:]
    return ("de la " if n.endswith(("a", "ión", "d")) and not n.endswith("día") else "del ") + n


def en(area_name):
    """'en la cocina' / 'en el salón'."""
    d = de(area_name)
    return "en el " + d[4:] if d.startswith("del ") else "en " + d[3:]


def item(pid, template, text, expect, gold="act"):
    return {"id": pid, "template": template, "category": "control" if gold == "act" or template.startswith(
        ("state", "unknown")) else "general", "text": text, "expect": expect, "gold": gold}


def generate(inv, rng, max_per_template=12):
    home = D.Home(inv)
    out = []
    n = 0

    def add(template, text, expect, gold="act"):
        nonlocal n
        n += 1
        out.append(item(f"t{n:04d}", template, text, expect, gold))

    by_area = {}
    for eid, k in home.kind.items():
        a = home.entities[eid].get("area")
        if k and a:
            by_area.setdefault((a, k), []).append(eid)
    unique_names = {}
    for eid in home.entities:
        if home.kind[eid]:
            unique_names.setdefault(D.fold(home.entities[eid]["name"]), []).append(eid)
    named = [ids[0] for ids in unique_names.values() if len(ids) == 1]

    def pick(seq, k=max_per_template):
        seq = list(seq)
        rng.shuffle(seq)
        return seq[:k]

    # how a speaker names the area: the first alias that starts in lower case (a common noun such as
    # "salón"), else the area name
    areas = {aid: next((x for x in a.get("aliases", []) if x and x[0].islower()), a["name"])
             for aid, a in home.areas.items()}
    for (a, k), ids in pick([x for x in by_area.items() if x[0][1] == "luz"]):
        pct = rng.choice([10, 20, 30, 40, 60, 70, 80, 90])
        add("light_area_pct", f"Pon la luz {de(areas[a])} al {pct} por ciento",
            {"tool": "light__HassLightSet", "targets": ids, "args": {"brightness": pct}})
        add("light_area_on", f"Enciende las luces {de(areas[a])}", {"tool": "intent__HassTurnOn", "targets": ids})
        add("light_area_dim_words", f"Pon las luces {de(areas[a])} a la mitad",
            {"tool": "light__HassLightSet", "targets": ids, "args": {"brightness": 50}})
    for eid in pick([e for e in named if home.kind[e] == "luz"]):
        nm = home.entities[eid]["name"].lower()
        add("light_name_off", f"Apaga {nm}", {"tool": "intent__HassTurnOff", "targets": [eid]})
        add("light_name_max", f"Pon {nm} al máximo",
            {"tool": "light__HassLightSet", "targets": [eid], "args": {"brightness": 100}})
    for (a, k), ids in pick([x for x in by_area.items() if x[0][1] in ("persiana", "toldo")]):
        word = "el toldo" if k == "toldo" else "la persiana"
        add("cover_half", f"Pon {word} {de(areas[a])} a la mitad",
            {"tool": "intent__HassSetPosition", "targets": ids, "args": {"position": 50}})
        add("cover_stop", f"Para {word} {de(areas[a])}", {"tool": "intent__HassStopMoving", "targets": ids})
        add("cover_close", f"Baja {word} {de(areas[a])}",
            {"any_of": [{"tool": "intent__HassTurnOff", "targets": ids},
                        {"tool": "intent__HassSetPosition", "targets": ids, "args": {"position": 0}}]})
    for (a, k), ids in pick([x for x in by_area.items() if x[0][1] == "clima"]):
        t = rng.choice([19, 20, 21, 22, 23, 24, 25])
        add("climate_temp", f"Pon el aire {de(areas[a])} a {t} grados",
            {"tool": "climate__HassClimateSetTemperature", "targets": ids, "args": {"temperature": t}})
    for eid in pick([e for e in named if home.kind[e] in ("tele", "altavoz")]):
        nm = home.entities[eid]["name"].lower()
        add("media_next", f"Pon la siguiente canción en {nm}", {"tool": "media_player__HassMediaNext", "targets": [eid]})
        add("media_quieter", f"Ponme {nm} más bajito",
            {"tool": "media_player__HassSetVolumeRelative", "targets": [eid], "args": {"volume_dir": "down"}})
        vol = rng.choice([15, 25, 35, 45])
        add("media_volume", f"Pon el volumen de {nm} al {vol}",
            {"tool": "media_player__HassSetVolume", "targets": [eid], "args": {"volume_level": vol}})
    for eid in pick([e for e in named if home.kind[e] == "ventilador"]):
        nm = home.entities[eid]["name"].lower()
        add("fan_pct", f"Pon {nm} al 50 por ciento",
            {"tool": "fan__HassFanSetSpeed", "targets": [eid], "args": {"percentage": 50}})
    for eid in pick([e for e in named if home.kind[e] == "enchufe"]):
        nm = home.entities[eid]["name"].lower()
        add("switch_off_colloquial", f"Desconecta {nm}", {"tool": "intent__HassTurnOff", "targets": [eid]})
        add("switch_on_polite", f"¿Puedes encender {nm}?", {"tool": "intent__HassTurnOn", "targets": [eid]})
    for a in pick([a for a in areas if by_area.get((a, "luz"))], 8):
        inc = by_area.get((a, "luz"), [])
        within = [e for e in home.of_kind("todo", a)]
        add("all_off_area", f"Apágalo todo {en(areas[a])}",
            {"tool": "intent__HassTurnOff", "targets_include": inc, "targets_within": within})
    for eid in pick([e for e in named if home.kind[e] == "luz"], 6):
        add("state_light", f"¿Está encendida {home.entities[eid]['name'].lower()}?",
            {"tools_any": ["homeassistant__GetLiveContext"]}, gold="noact")
    missing = next(w for w in ("trastero", "garaje", "buhardilla", "bodega") if
                   all(w not in D.fold(n) for n in areas.values()))
    add("unknown_area", f"Enciende la luz del {missing}", {"no_success_claim": True}, gold="noact")
    import run as ae
    for it in ae.load_phrases():
        if it["category"] in ("general", "live"):
            add("general_" + it["id"], it["turns"][0]["text"], it["turns"][0]["expect"], gold="noact")
    return out


def main():
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--inventory", required=True)
    ap.add_argument("--out", required=True)
    ap.add_argument("--seed", type=int, default=7)
    a = ap.parse_args()
    import yaml
    inv = yaml.safe_load(open(a.inventory, encoding="utf-8"))
    items = generate(inv, random.Random(a.seed))
    old = os.umask(0o077)
    with open(a.out, "w", encoding="utf-8") as fh:
        for it in items:
            fh.write(json.dumps(it, ensure_ascii=False) + "\n")
    os.umask(old)
    print(f"{len(items)} items", file=sys.stderr)


if __name__ == "__main__":
    main()
