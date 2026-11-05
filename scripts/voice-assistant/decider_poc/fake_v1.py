#!/usr/bin/env python3
"""kivi-fake-v1: a broader synthetic Spanish dataset for judging a decider behind Home Assistant's
built-in intents. No real home data: four invented homes, invented room and person names.

Homes (naming styles):
  generica   generic names that repeat the room ("Luz del salón"), Spain, ~40 exposed entities
  coloquial  colloquial and brand-like invented names with aliases ("Lámpara Arco", "Split"), ~60
  personas   rooms named after invented people, duplicate device names across rooms, 2 floors, ~80
  latam      large Mexican-Spanish home ("recámara", "foco", "pantalla", "bocina", "clima"), many
             duplicates, 2 floors, ~120
Exposed sensors fill the homes to a realistic entity count; only controllable domains are targets.

Every command is generated from a structured action (intent, target option, value), so its gold
label is known. Styles: direct, polite, short, word_order, disfluent (fillers, repetitions,
self-corrections), relative, implicit, multi, latam, spain_colloquial; negatives: general, live,
chitchat, tv_dialogue (film lines with command verbs), state (questions about the home),
missing_target (commands for devices or rooms the home does not have), out_of_domain.

Gold kind and route:
  command      one action the decider's intent set covers                      route execute
  unsupported  a home request outside that set (relative brightness, +2 degrees) route kiais
  unclear      implicit or verb-less ("No veo nada aquí", "Luz salón")          route kiais
  multi        two actions in one request                                       route kiais
  question     general, live, chit-chat, TV dialogue, state questions           route kiais
  other        out-of-domain commands, devices or rooms not in the home         route kiais

Output: <out>/homes/<home>.yaml (agent_eval inventory shape) and <out>/items_template.jsonl; the
schema is in <out>/SCHEMA.md (written by this script).

  python fake_v1.py --out /work/workspaces/kivi-decider-poc/data/kivi-fake-v1 --per-home 300
"""
import argparse
import json
import random
import re
from pathlib import Path

import yaml

HERE = Path(__file__).resolve().parent
import gen_data as gd  # noqa: E402  (agreement, articles, values, render)
import numbers_es as ne  # noqa: E402
import schema as sc  # noqa: E402

# ---- homes ---------------------------------------------------------------------------------------
# area: (id, name, gender, aliases, floor); device: (domain, name, gender, aliases)
HOMES = {
    "generica": dict(
        satellite="salon",
        areas=[("salon", "Salón", "m", ["sala"], 0), ("cocina", "Cocina", "f", [], 0),
               ("dormitorio", "Dormitorio principal", "m", ["dormitorio"], 0),
               ("invitados", "Dormitorio de invitados", "m", [], 0), ("bano", "Baño", "m", [], 0),
               ("aseo", "Aseo", "m", [], 0), ("pasillo", "Pasillo", "m", [], 0), ("terraza", "Terraza", "f", [], 0),
               ("despacho", "Despacho", "m", [], 0)],
        devices={"salon": [("light", "Luz del salón", "f", []), ("cover", "Persiana del salón", "f", []),
                           ("climate", "Aire del salón", "m", ["aire"]), ("media_player", "Tele del salón", "f", ["tele"])],
                 "cocina": [("light", "Luz de la cocina", "f", []), ("media_player", "Altavoz de la cocina", "m", []),
                            ("switch", "Cafetera", "f", []), ("cover", "Persiana de la cocina", "f", [])],
                 "dormitorio": [("light", "Luz del dormitorio", "f", []), ("cover", "Persiana del dormitorio", "f", []),
                                ("climate", "Aire del dormitorio", "m", [])],
                 "invitados": [("light", "Luz de invitados", "f", []), ("cover", "Persiana de invitados", "f", [])],
                 "bano": [("light", "Luz del baño", "f", []), ("fan", "Extractor del baño", "m", [])],
                 "aseo": [("light", "Luz del aseo", "f", [])],
                 "pasillo": [("light", "Luz del pasillo", "f", [])],
                 "terraza": [("light", "Luz de la terraza", "f", []), ("cover", "Toldo", "m", [])],
                 "despacho": [("light", "Luz del despacho", "f", []), ("fan", "Ventilador del despacho", "m", []),
                              ("switch", "Estufa del despacho", "f", [])]},
        sensors=16),
    "coloquial": dict(
        satellite="salon",
        areas=[("salon", "Salón", "m", [], 0), ("cocina", "Cocina", "f", [], 0), ("dormitorio", "Dormitorio", "m", ["cuarto"], 0),
               ("bano", "Baño", "m", [], 0), ("estudio", "Estudio", "m", [], 0), ("terraza", "Terraza", "f", [], 0),
               ("entrada", "Entrada", "f", ["recibidor"], 0)],
        devices={"salon": [("light", "Lámpara de pie", "f", ["lámpara"]), ("light", "Lámpara Arco", "f", ["arco"]),
                           ("light", "Tira LED", "f", ["tira"]), ("media_player", "Tele grande", "f", ["tele"]),
                           ("media_player", "Barra de sonido", "f", ["barra"]), ("climate", "Split", "m", ["aire"]),
                           ("cover", "Estor", "m", []), ("light", "Foco Nube", "m", ["nube"])],
                 "cocina": [("light", "Plafón", "m", []), ("light", "Luz de la encimera", "f", ["encimera"]),
                            ("media_player", "Altavoz Lumo", "m", ["lumo"]), ("switch", "Cafetera", "f", []),
                            ("switch", "Hervidor", "m", [])],
                 "dormitorio": [("light", "Lamparita", "f", ["mesilla"]), ("light", "Guirnalda", "f", []),
                                ("media_player", "Tele pequeña", "f", []), ("climate", "Split del dormitorio", "m", []),
                                ("cover", "Persiana", "f", []), ("fan", "Ventilador", "m", [])],
                 "bano": [("light", "Espejo", "m", ["luz del espejo"]), ("switch", "Calefactor", "m", [])],
                 "estudio": [("light", "Flexo", "m", []), ("light", "Bombilla Lux", "f", ["lux"]),
                             ("cover", "Persiana del estudio", "f", [])],
                 "terraza": [("light", "Farolillos", "m", []), ("cover", "Toldo", "m", [])],
                 "entrada": [("light", "Aplique", "m", [])]},
        sensors=28),
    "personas": dict(
        satellite="salon",
        areas=[("salon", "Salón", "m", [], 0), ("cocina", "Cocina", "f", [], 0), ("bano_abajo", "Baño de abajo", "m", [], 0),
               ("garaje", "Garaje", "m", [], 0), ("jardin", "Jardín", "m", [], 0),
               ("cuarto_candela", "Cuarto de Candela", "m", ["habitación de Candela"], 1),
               ("cuarto_telmo", "Cuarto de Telmo", "m", ["habitación de Telmo"], 1),
               ("cuarto_abuela", "Cuarto de la abuela", "m", [], 0),
               ("despacho_noa", "Despacho de Noa", "m", [], 1), ("bano_arriba", "Baño de arriba", "m", [], 1),
               ("buhardilla", "Buhardilla", "f", [], 1)],
        devices={"salon": [("light", "Luz del techo", "f", []), ("light", "Lámpara de pie", "f", []),
                           ("cover", "Persiana", "f", []), ("cover", "Persiana pequeña", "f", []),
                           ("media_player", "Tele", "f", ["televisión"]), ("climate", "Aire", "m", ["aire acondicionado"])],
                 "cocina": [("light", "Luz del techo", "f", []), ("light", "Luz de la encimera", "f", []),
                            ("media_player", "Radio", "f", []), ("switch", "Cafetera", "f", [])],
                 "bano_abajo": [("light", "Luz del techo", "f", []), ("fan", "Extractor", "m", [])],
                 "garaje": [("light", "Luz del techo", "f", []), ("cover", "Puerta del garaje", "f", [])],
                 "jardin": [("light", "Farolas", "f", []), ("switch", "Riego", "m", [])],
                 "cuarto_candela": [("light", "Luz del techo", "f", []), ("light", "Lámpara de la mesilla", "f", []),
                                    ("cover", "Persiana", "f", []), ("climate", "Aire", "m", []),
                                    ("media_player", "Altavoz", "m", [])],
                 "cuarto_telmo": [("light", "Luz del techo", "f", []), ("light", "Lámpara de la mesilla", "f", []),
                                  ("cover", "Persiana", "f", []), ("fan", "Ventilador", "m", [])],
                 "cuarto_abuela": [("light", "Luz del techo", "f", []), ("cover", "Persiana", "f", []),
                                   ("media_player", "Tele", "f", []), ("switch", "Manta eléctrica", "f", [])],
                 "despacho_noa": [("light", "Flexo", "m", []), ("light", "Luz del techo", "f", []),
                                  ("climate", "Aire", "m", []), ("switch", "Estufa", "f", [])],
                 "bano_arriba": [("light", "Luz del techo", "f", []), ("light", "Luz del espejo", "f", [])],
                 "buhardilla": [("light", "Luz del techo", "f", []), ("cover", "Claraboya", "f", []),
                                ("media_player", "Proyector", "m", [])]},
        sensors=38),
    "latam": dict(
        satellite="sala",
        areas=[("sala", "Sala", "f", [], 0), ("comedor", "Comedor", "m", [], 0), ("cocina", "Cocina", "f", [], 0),
               ("recamara_principal", "Recámara principal", "f", ["recámara"], 1),
               ("recamara_ninos", "Recámara de los niños", "f", [], 1), ("recamara_visitas", "Recámara de visitas", "f", [], 1),
               ("bano_principal", "Baño principal", "m", [], 1), ("medio_bano", "Medio baño", "m", [], 0),
               ("estudio", "Estudio", "m", [], 1), ("lavado", "Cuarto de lavado", "m", [], 0), ("patio", "Patio", "m", [], 0),
               ("cochera", "Cochera", "f", [], 0), ("terraza", "Terraza", "f", [], 1),
               ("pasillo", "Pasillo de arriba", "m", [], 1)],
        devices={"sala": [("light", "Foco", "m", []), ("light", "Lámpara", "f", []), ("light", "Tira de luz", "f", []),
                          ("cover", "Cortina", "f", []), ("cover", "Persiana", "f", []), ("media_player", "Pantalla", "f", ["tele"]),
                          ("media_player", "Bocina", "f", []), ("climate", "Clima", "m", ["minisplit"]),
                          ("fan", "Ventilador de techo", "m", [])],
                 "comedor": [("light", "Foco", "m", []), ("light", "Candil", "m", []), ("cover", "Cortina", "f", []),
                             ("fan", "Ventilador de techo", "m", [])],
                 "cocina": [("light", "Foco", "m", []), ("light", "Luz de la barra", "f", []), ("switch", "Cafetera", "f", []),
                            ("media_player", "Bocina", "f", []), ("switch", "Extractor", "m", [])],
                 "recamara_principal": [("light", "Foco", "m", []), ("light", "Lámpara de buró", "f", ["buró"]),
                                        ("cover", "Cortina", "f", []), ("cover", "Persiana", "f", []),
                                        ("media_player", "Pantalla", "f", []), ("climate", "Clima", "m", []),
                                        ("fan", "Ventilador de techo", "m", [])],
                 "recamara_ninos": [("light", "Foco", "m", []), ("light", "Lámpara de noche", "f", []),
                                    ("cover", "Cortina", "f", []), ("climate", "Clima", "m", []),
                                    ("fan", "Ventilador de techo", "m", []), ("media_player", "Bocina", "f", [])],
                 "recamara_visitas": [("light", "Foco", "m", []), ("cover", "Cortina", "f", []),
                                      ("fan", "Ventilador de techo", "m", [])],
                 "bano_principal": [("light", "Foco", "m", []), ("light", "Luz del espejo", "f", []),
                                    ("switch", "Calentador", "m", [])],
                 "medio_bano": [("light", "Foco", "m", [])],
                 "estudio": [("light", "Foco", "m", []), ("light", "Lámpara de escritorio", "f", []),
                             ("cover", "Persiana", "f", []), ("climate", "Clima", "m", []),
                             ("media_player", "Bocina", "f", [])],
                 "lavado": [("light", "Foco", "m", []), ("switch", "Bomba de agua", "f", [])],
                 "patio": [("light", "Reflector", "m", []), ("light", "Luces de jardín", "f", []), ("switch", "Riego", "m", [])],
                 "cochera": [("light", "Foco", "m", []), ("cover", "Portón", "m", [])],
                 "terraza": [("light", "Foco", "m", []), ("light", "Serie de luces", "f", []), ("cover", "Toldo", "m", [])],
                 "pasillo": [("light", "Foco", "m", []), ("light", "Luz de escalera", "f", [])]},
        sensors=60),
}


def build_home(hid, spec):
    areas, gender, ents = {}, {}, {}
    floors = {"planta_baja": {"name": "Planta baja"}, "planta_alta": {"name": "Planta alta"}}
    for aid, name, g, aliases, fl in spec["areas"]:
        areas[aid] = {"name": name, "floor": "planta_alta" if fl else "planta_baja", "aliases": list(aliases)}
        gender[f"area:{aid}"] = g
    for aid, devs in spec["devices"].items():
        for d, name, g, aliases in devs:
            eid = f"{d}.{aid}_{gd.slug(name)}"
            e = {"name": name, "area": aid, "aliases": list(aliases)}
            if d == "media_player":
                e.update(state="playing", device_class="tv" if re.search(r"tele|pantalla|proyector", name, re.I)
                         else "speaker", attributes={"volume_level": 0.3})
            elif d == "cover":
                e.update(state="open", device_class="awning" if name == "Toldo" else
                         ("garage" if re.search(r"garaje|port", name, re.I) else "blind"),
                         attributes={"current_position": 100})
            elif d == "climate":
                e.update(state="cool", attributes={"temperature": 23, "current_temperature": 24.0})
            else:
                e["state"] = "off"
            ents[eid] = e
            gender[eid] = g
    kinds = [("temperatura", "temperature", "°C"), ("humedad", "humidity", "%"), ("movimiento", None, None),
             ("puerta", None, None), ("consumo", "power", "W")]
    aids = list(areas)
    n = 0
    while n < spec["sensors"]:
        aid = aids[n % len(aids)]
        word, dc, unit = kinds[(n // len(aids)) % len(kinds)]
        dom = "binary_sensor" if dc is None else "sensor"
        eid = f"{dom}.{aid}_{word}"
        if eid not in ents:
            ents[eid] = {"name": word.capitalize(), "area": aid, "state": "20" if dom == "sensor" else "off",
                         "attributes": {k: v for k, v in (("device_class", dc), ("unit_of_measurement", unit)) if v}}
        n += 1
    ents["weather.casa"] = {"name": "Tiempo", "aliases": ["el tiempo"], "state": "sunny",
                            "attributes": {"temperature": 18}}
    return {"now": "2026-11-05T18:42:00+01:00", "timezone": "Europe/Madrid", "satellite_area": spec["satellite"],
            "floors": floors, "areas": areas, "entities": ents, "gender": gender, "id": hid,
            "calendar_events": {"today": [], "week": []}}


# ---- referring expressions ------------------------------------------------------------------------

def name_has_area(home, eid):
    e = home["entities"][eid]
    a = home["areas"][e["area"]]
    return ne.fold(a["name"]) in ne.fold(e["name"]) or any(ne.fold(x) in ne.fold(e["name"]) for x in a["aliases"])


def refs(home, opt):
    """[(np, gender, short form)] for a target option; short form drops articles and prepositions."""
    k, out = opt.key, []
    if k.startswith("e:"):
        eid = k[2:]
        e = home["entities"][eid]
        area = home["areas"][e["area"]]["name"].lower()
        if gd.unique_name(home, eid):
            np_, g = gd.np_entity(home, eid, "name")
            out.append((np_, g, e["name"].lower()))
        if not name_has_area(home, eid):
            np_, g = gd.np_entity(home, eid, "name+area")
            out.append((np_, g, f"{e['name'].lower()} {area}"))
        if e.get("aliases") and gd.unique_name(home, eid, alias=True):
            np_, g = gd.np_entity(home, eid, "alias")
            out.append((np_, g, e["aliases"][0].lower()))
        return out
    if k.startswith("h:"):
        return [("todas las luces de la casa", "fp", "todas las luces"), ("todas las luces", "fp", "todas las luces")]
    _, aid, g = k.split(":")
    area = gd.np_area(home, aid)
    aname = home["areas"][aid]["name"].lower()
    if g == "light":
        out = [(f"las luces {gd.de(area)}", "fp", f"luces {aname}"), (f"la luz {gd.de(area)}", "f", f"luz {aname}")]
        if aid == home["satellite_area"]:
            out.append(("la luz", "f", "luz"))
        return out
    if g == "cover" and len(opt.entities) > 1:
        word = "cortinas" if all("cortina" in home["entities"][e]["name"].lower() for e in opt.entities) else "persianas"
        return [(f"las {word} {gd.de(area)}", "fp", f"{word} {aname}"), (f"todas las {word} {gd.de(area)}", "fp", f"{word} {aname}")]
    return []


# ---- templates --------------------------------------------------------------------------------------
# {t} target NP, {de_t} {en_t} {a_t}, {s} short form, {adj:stem}, {lo}, {pct} {temp} {dur}, {area}...
TPL = {
    "turn_on": {
        "direct": ["Enciende {t}", "Pon {t}", "Activa {t}"],
        "polite": ["¿Podrías encender {t}?", "Por favor, enciende {t}", "¿Me haces el favor de encender {t}?"],
        "short": ["Encender {s}", "{s}, encender", "{s} on"],
        "word_order": ["{t}, enciénde{lo}", "{t}, enciéndeme{lo}", "{t}, ¿{lo} enciendes?"],
        "latam": ["Prende {t}", "¿Me prendes {t}?", "Préndeme {t} porfa"],
        "spain_colloquial": ["Dale a {t}", "Dale {t}", "Ponme {t}"],
    },
    "turn_off": {
        "direct": ["Apaga {t}", "Desconecta {t}", "Apaga {t} ya"],
        "polite": ["¿Podrías apagar {t}?", "Por favor, apaga {t}", "¿Me haces el favor de apagar {t}?"],
        "short": ["Apagar {s}", "{s}, apagar", "{s} off"],
        "word_order": ["{t}, apága{lo}", "{t}, apágame{lo}", "{t}, ¿{lo} apagas?"],
        "latam": ["Apágame {t} porfa", "¿Me apagas {t}?", "Apaga {t}, por fa"],
        "spain_colloquial": ["Quita {t}", "Apágame {t}", "Fuera {t}"],
    },
    "open_cover": {
        "direct": ["Abre {t}", "Sube {t}", "Sube {t} del todo"],
        "polite": ["¿Podrías abrir {t}?", "Por favor, sube {t}", "¿Me haces el favor de subir {t}?"],
        "short": ["Subir {s}", "{s} arriba", "Abrir {s}"],
        "word_order": ["{t}, ábre{lo}", "{t}, súbe{lo}", "{t} arriba, por favor"],
        "latam": ["Abre {t}, porfa", "¿Me abres {t}?", "Sube {t} porfa"],
        "spain_colloquial": ["Levanta {t}", "Súbeme {t}", "Recoge {t}"],
    },
    "close_cover": {
        "direct": ["Cierra {t}", "Baja {t}", "Baja {t} del todo"],
        "polite": ["¿Podrías cerrar {t}?", "Por favor, baja {t}", "¿Me haces el favor de bajar {t}?"],
        "short": ["Bajar {s}", "{s} abajo", "Cerrar {s}"],
        "word_order": ["{t}, ciérra{lo}", "{t}, bája{lo}", "{t} abajo, por favor"],
        "latam": ["Cierra {t}, porfa", "¿Me cierras {t}?", "Baja {t} porfa"],
        "spain_colloquial": ["Echa {t}", "Bájame {t}", "Chapa {t}"],
    },
    "cover_position": {
        "direct": ["Pon {t} al {pct} por ciento", "Deja {t} al {pct} por ciento", "Pon {t} a la mitad"],
        "polite": ["¿Podrías poner {t} al {pct} por ciento?", "Por favor, deja {t} a la mitad"],
        "short": ["{s} al {pct}", "{s} {pct} por ciento"],
        "word_order": ["Al {pct} por ciento {t}, por favor", "A la mitad {t}"],
    },
    "stop_cover": {
        "direct": ["Para {t}", "Detén {t}"],
        "polite": ["¿Puedes parar {t}?", "Por favor, para {t}"],
        "short": ["{s} stop", "Parar {s}"],
        "spain_colloquial": ["Vale, para {t} ahí", "Ya, ya, para {t}"],
    },
    "light_set": {
        "direct": ["Pon {t} al {pct} por ciento", "Sube el brillo {de_t} al {pct} por ciento", "Pon {t} al máximo"],
        "polite": ["¿Podrías poner {t} al {pct} por ciento?", "Por favor, baja el brillo {de_t} al {pct}"],
        "short": ["{s} al {pct}", "{s} {pct} por ciento", "Brillo {s} {pct}"],
        "word_order": ["Al {pct} por ciento {t}, por favor", "El brillo {de_t}, al {pct}", "{t} al {pct}, ¿vale?"],
        "latam": ["Bájale {a_t} al {pct} por ciento", "Súbele {a_t} al {pct}"],
    },
    "set_volume": {
        "direct": ["Pon el volumen {de_t} al {pct}", "Volumen {de_t} a {pct}"],
        "polite": ["¿Podrías poner el volumen {de_t} a {pct}?", "Por favor, pon el volumen {de_t} al {pct}"],
        "short": ["Volumen {s} {pct}", "{s} volumen {pct}"],
        "word_order": ["A {pct} el volumen {de_t}", "El volumen {de_t}, a {pct}"],
        "latam": ["Ponle el volumen {a_t} en {pct}", "Ponle {pct} de volumen {a_t}"],
    },
    "volume_up": {
        "direct": ["Sube el volumen {de_t}", "Más volumen {en_t}"],
        "polite": ["¿Podrías subir el volumen {de_t}?", "Por favor, sube el volumen {de_t}"],
        "short": ["Volumen arriba {s}", "{s} más alto"],
        "relative": ["Sube un poco el volumen {de_t}", "Un poco más alto {en_t}, por favor", "Sube un poquito {t}"],
        "latam": ["Súbele {a_t}", "Súbele al volumen {de_t}"],
        "spain_colloquial": ["Ponme {t} más {adj:alt}", "No se oye {t}, súbe{lo}"],
    },
    "volume_down": {
        "direct": ["Baja el volumen {de_t}", "Menos volumen {en_t}"],
        "polite": ["¿Podrías bajar el volumen {de_t}?", "Por favor, baja el volumen {de_t}"],
        "short": ["Volumen abajo {s}", "{s} más bajo"],
        "relative": ["Baja un poco el volumen {de_t}", "Un poco más bajo {en_t}, por favor", "Baja un poquito {t}"],
        "latam": ["Bájale {a_t}", "Bájale al volumen {de_t}"],
        "spain_colloquial": ["Ponme {t} más {adj:bajit}", "Está muy {adj:alt} {t}, bája{lo}"],
    },
    "mute": {
        "direct": ["Silencia {t}", "Pon {t} en silencio"],
        "polite": ["¿Podrías silenciar {t}?", "Por favor, quita el sonido {de_t}"],
        "short": ["{s} silencio", "Silenciar {s}"],
        "latam": ["Ponle mute {a_t}", "Mutea {t}"],
        "spain_colloquial": ["Que se calle {t}", "Calla {t}"],
    },
    "unmute": {
        "direct": ["Quita el silencio {de_t}", "Activa el sonido {de_t}"],
        "polite": ["¿Podrías quitar el silencio {de_t}?"],
        "latam": ["Quítale el mute {a_t}", "Desmutea {t}"],
    },
    "pause": {
        "direct": ["Pausa {t}", "Pon {t} en pausa"],
        "polite": ["¿Podrías pausar {t}?", "Por favor, pausa {t}"],
        "short": ["Pausa {s}", "{s} pausa"],
        "latam": ["Ponle pausa {a_t}", "Pausa {t}, porfa"],
        "spain_colloquial": ["Para {t} un momento", "Espera, para {t}"],
    },
    "resume": {
        "direct": ["Reanuda {t}", "Continúa {t}"],
        "polite": ["¿Podrías reanudar {t}?", "Por favor, quita la pausa {de_t}"],
        "short": ["{s} play", "Play {s}"],
        "latam": ["Ponle play {a_t}", "Dale play {a_t}"],
        "spain_colloquial": ["Sigue con {t}", "Que siga {t}"],
    },
    "next": {
        "direct": ["Siguiente canción {en_t}", "Pasa a la siguiente canción {en_t}"],
        "polite": ["¿Podrías pasar a la siguiente canción {en_t}?"],
        "short": ["Siguiente {s}", "{s} siguiente"],
        "latam": ["Cámbiale de canción {a_t}", "Pásale a la que sigue {en_t}"],
        "spain_colloquial": ["Pasa de esta canción {en_t}", "Quita esta canción {en_t}, la siguiente"],
    },
    "previous": {
        "direct": ["Canción anterior {en_t}", "Pon la canción anterior {en_t}"],
        "polite": ["¿Podrías volver a la canción anterior {en_t}?"],
        "latam": ["Regresa a la canción anterior {en_t}", "Regrésale a la anterior {en_t}"],
    },
    "climate_set": {
        "direct": ["Pon {t} a {temp} grados", "Pon {t} a {temp}"],
        "polite": ["¿Podrías poner {t} a {temp} grados?", "Por favor, pon {t} a {temp} grados"],
        "short": ["{s} {temp} grados", "{s} a {temp}"],
        "word_order": ["A {temp} grados {t}, por favor", "{t}, pon{lo} a {temp}"],
        "latam": ["Ponle {temp} grados {a_t}", "Ajústale {a_t} a {temp}"],
    },
    "fan_speed": {
        "direct": ["Pon {t} al {pct} por ciento", "Velocidad {de_t} al {pct}"],
        "polite": ["¿Podrías poner {t} al {pct} por ciento?"],
        "short": ["{s} al {pct}"],
        "latam": ["Ponle {a_t} al {pct}"],
    },
    "start_timer": {
        "direct": ["Pon un temporizador de {dur}", "Temporizador de {dur}"],
        "polite": ["¿Podrías poner un temporizador de {dur}?", "Por favor, pon un temporizador de {dur}"],
        "short": ["Temporizador {dur}", "{dur}, temporizador"],
        "word_order": ["{dur} de temporizador", "Un temporizador, de {dur}"],
        "latam": ["Pon un timer de {dur}", "Ponme un timer de {dur}"],
        "spain_colloquial": ["Avísame en {dur}", "Ponme un temporizador de {dur} para la pasta"],
    },
    "cancel_timer": {
        "direct": ["Cancela el temporizador", "Quita el temporizador"],
        "polite": ["¿Podrías cancelar el temporizador?"],
        "short": ["Temporizador fuera", "Cancelar temporizador"],
        "latam": ["Cancela el timer", "Quita el timer"],
    },
}
# Requests about the home outside the decider's intent set (route kiais): relative brightness,
# relative temperature, relative cover moves. {t} is a target of the given domain.
UNSUPPORTED = {
    "light": ["Sube un poco {t}", "Un poco más de luz {en_area}", "Baja un poco la luz {de_area}", "Más luz, por favor"],
    "climate": ["Sube dos grados {t}", "Baja un poco {t}", "Pon {t} un grado más alto"],
    "cover": ["Sube un poco {t}", "Baja {t} un poquito", "Abre {t} solo un poco"],
}
IMPLICIT = ["No veo nada aquí", "Qué oscuro está esto", "Hace un frío que pela", "Me muero de calor",
            "No oigo nada", "Esto está muy alto", "Qué ruido hay", "Me voy a dormir", "Ya me voy de casa",
            "Hay demasiada luz", "Está muy oscuro aquí", "Aquí hace mucho calor", "Tengo frío",
            "Me molesta el sol", "No se oye bien"]
MULTI = ["{V1} {t1} y {v2} {t2}", "{V1} {t1} y también {t2}", "{V1} {t1}, y luego {v2} {t2}"]
VERB = {"turn_on": "enciende", "turn_off": "apaga", "open_cover": "sube", "close_cover": "baja", "mute": "silencia",
        "pause": "pausa"}
NEG = {
    "general": ["¿Cuál es la capital de Portugal?", "¿Cuánto es doce por ocho?", "¿Quién escribió La Celestina?",
                "¿En qué año llegó el hombre a la Luna?", "¿Por qué el cielo es azul?", "¿Cómo se dice ventana en inglés?",
                "¿Cuántos huesos tiene el cuerpo humano?", "¿Quién pintó el Guernica?", "Explícame qué es un agujero negro",
                "¿Cómo se hace una tortilla de patatas?", "¿Cuál es el río más largo de España?",
                "¿Qué significa la palabra efímero?", "¿Cuántas patas tiene una araña?", "¿Cuál es el planeta más grande?",
                "¿Cuánto mide la Torre Eiffel?", "¿De qué color es una esmeralda?"],
    "live": ["¿Qué tiempo va a hacer mañana?", "¿Va a llover esta tarde?", "¿Qué hora es?", "¿Qué día es hoy?",
             "¿Qué noticias hay hoy?", "¿Cómo ha quedado el partido?", "¿Hay mucho tráfico para ir al centro?",
             "¿A qué hora abre la farmacia?", "¿Cuánto está el euro?", "¿Qué ponen hoy en la tele?"],
    "chitchat": ["Hola, ¿qué tal estás?", "Buenas noches", "Gracias", "Muchas gracias", "¿Cómo te llamas?",
                 "Cuéntame un chiste", "Me aburro", "¿Qué me recomiendas cenar hoy?", "Nada, olvídalo", "Da igual",
                 "Vale, perfecto", "Eso es todo", "Te quiero", "¿Eres una persona?"],
    "tv_dialogue": ["¡Apaga eso y vámonos!", "Sube, que te llevo", "Baja el arma ahora mismo", "Abre la puerta, policía",
                    "Enciende el motor, rápido", "Cierra la boca y escucha", "Para el coche aquí",
                    "Pon las manos donde pueda verlas", "Apaga la luz, que nos van a ver", "Sube el volumen, que es mi canción",
                    "Baja la voz, que duerme el niño", "Le pidió que apagara la luz antes de salir",
                    "Cuando bajó la persiana ya era de noche", "La tele estuvo encendida toda la noche",
                    "Y ahora, el tiempo para mañana en toda la península", "Volvemos después de la publicidad",
                    "No apagues la radio, que ahora viene lo mejor", "Pausa, pausa, que no me entero",
                    "Corre, cierra la ventana", "Enciende la tele, que empieza el partido"],
    "state": ["¿Está {adj:encendid} {t}?", "¿Está {adj:apagad} {t}?", "¿Quién ha encendido {t}?",
              "¿Qué temperatura hace {en_area}?", "¿Está {adj:abiert} {t}?", "¿Cuánto falta para el temporizador?"],
    "out_of_domain": ["Pon música relajante", "Pon algo de jazz", "Pon Netflix", "Cambia a la uno",
                      "Añade leche a la lista de la compra", "Recuérdame mañana llamar al médico", "Despiértame a las siete",
                      "Pide una pizza", "Llama a mi hermana", "Pon una alarma para las ocho", "Graba el partido de esta noche",
                      "Busca recetas de lentejas", "Abre la aplicación del banco", "Pon la película de anoche"],
    "missing_target": ["Enciende {t}", "Apaga {t}", "Abre {t}", "Baja {t}", "Pon {t} al {pct} por ciento", "Silencia {t}"],
}
FILLERS = ["Eh, ", "Mmm, ", "A ver, eh, ", "Oye, ", "Eeeh... ", "Pues "]


def fill(rng, home, intent, tpl, opts, style):
    """Render a command template for one compatible target. Returns (item dict) or None."""
    slots = {"g": "f", "np": "", "area_np": gd.np_area(home, home["satellite_area"])}
    target = None
    needs_t = any(p in tpl for p in ("{t}", "{de_t}", "{en_t}", "{a_t}", "{s}"))
    if intent not in sc.NO_TARGET and needs_t:
        cands = [o for o in opts if sc.compatible(intent, o) and not o.key.endswith(":all")]
        rng.shuffle(cands)
        for o in cands:
            rs = refs(home, o)
            if rs:
                np_, g, short = rng.choice(rs)
                slots.update(np=np_, g=g)
                tpl = tpl.replace("{s}", short)
                target = o.key
                break
        if target is None:
            return None
    elif intent not in sc.NO_TARGET:
        return None
    vk = sc.INTENTS[intent][4]
    value = None
    if vk == "percent":
        value = 50 if "mitad" in tpl else (100 if "máximo" in tpl else gd.sample_percent(rng))
        slots["value"] = value
    elif vk == "temperature":
        value = gd.sample_temp(rng)
        slots["value"] = value
    elif vk == "duration":
        value, slots["dur_words"], slots["dur_written"] = gd.sample_duration(rng)
    return dict(kind="command", style=style, actions=[dict(intent=intent, target=target, value=value)],
                spoken=gd.render(tpl, slots, "words"), written=gd.render(tpl, slots, "digits"), template=tpl)


def disfluent(rng, home, opts):
    """Fillers, repeated article, or a self-correction whose LAST target is the gold one."""
    intent = rng.choice(["turn_on", "turn_off", "open_cover", "close_cover", "light_set", "set_volume", "climate_set",
                         "pause", "mute", "start_timer"])
    tpl = rng.choice(TPL[intent]["direct"])
    mode = rng.choice(["filler", "repeat", "correct"])
    if mode == "correct" and "{t}" in tpl and intent not in sc.NO_TARGET:
        cands = [o for o in opts if sc.compatible(intent, o) and not o.key.endswith(":all") and refs(home, o)]
        if len(cands) < 2:
            return None
        o1, o2 = rng.sample(cands, 2)
        r1, r2 = rng.choice(refs(home, o1)), rng.choice(refs(home, o2))
        if ne.fold(r1[0]) == ne.fold(r2[0]):
            return None
        it = fill(rng, home, intent, tpl, [o2], "disfluent")
        if it is None:
            return None
        for k in ("spoken", "written"):
            it[k] = it[k].replace(r2[0], f"{r1[0]}, no, perdón, {r2[0]}", 1) if r2[0] in it[k] else None
        if it["spoken"] is None or it["written"] is None or it["actions"][0]["target"] != o2.key:
            return None
        it["template"] = "correct:" + tpl
        return it
    it = fill(rng, home, intent, tpl, opts, "disfluent")
    if it is None:
        return None
    if mode == "filler":
        f = rng.choice(FILLERS)
        for k in ("spoken", "written"):
            it[k] = f + it[k][0].lower() + it[k][1:]
    else:
        for k in ("spoken", "written"):
            it[k] = re.sub(r"\b(la|el|las|los) ", lambda m: f"{m.group(1)}... {m.group(1)} ", it[k], count=1)
    it["template"] = f"{mode}:" + tpl
    return it


def missing_np(rng, home):
    np_, g = gd.missing_target_np(rng, home)
    return np_, g


def negative(rng, home, style, opts):
    tpl = rng.choice(NEG[style])
    slots = {"g": "f", "np": "", "area_np": gd.np_area(home, rng.choice(list(home["areas"]))), "value": gd.sample_percent(rng)}
    kind = {"out_of_domain": "other", "missing_target": "other"}.get(style, "question")
    if "{t}" in tpl:
        if style == "missing_target":
            slots["np"], slots["g"] = missing_np(rng, home)
        else:
            ents = [o for o in opts if o.key.startswith("e:") and refs(home, o)]
            np_, g, _s = rng.choice(refs(home, rng.choice(ents)))
            slots.update(np=np_, g=g)
    return dict(kind=kind, style=style, actions=[], spoken=gd.render(tpl, slots, "words"),
                written=gd.render(tpl, slots, "digits"), template=tpl)


def other_kinds(rng, home, style, opts):
    if style == "implicit":
        t = rng.choice(IMPLICIT)
        return dict(kind="unclear", style=style, actions=[], spoken=t, written=t, template=t)
    if style == "short_bare":   # verb-less short form: intent unknown
        o = rng.choice([o for o in opts if o.key.startswith(("e:", "a:")) and refs(home, o)])
        s = rng.choice(refs(home, o))[2]
        s = s[0].upper() + s[1:]
        return dict(kind="unclear", style="short", actions=[], spoken=s, written=s, template="{s}")
    if style == "unsupported":
        d = rng.choice(list(UNSUPPORTED))
        tpl = rng.choice(UNSUPPORTED[d])
        cands = [o for o in opts if o.key.startswith("e:") and sc.domain(o.key[2:]) == d and refs(home, o)]
        if not cands:
            return None
        o = rng.choice(cands)
        np_, g, _ = rng.choice(refs(home, o))
        aid = home["entities"][o.key[2:]]["area"]
        slots = {"g": g, "np": np_, "area_np": gd.np_area(home, aid)}
        return dict(kind="unsupported", style="relative", actions=[], spoken=gd.render(tpl, slots, "words"),
                    written=gd.render(tpl, slots, "digits"), template=tpl)
    if style == "multi":
        i1, i2 = rng.choice(list(VERB)), rng.choice(list(VERB))
        o1s = [o for o in opts if sc.compatible(i1, o) and not o.key.endswith(":all") and refs(home, o)]
        o2s = [o for o in opts if sc.compatible(i2, o) and not o.key.endswith(":all") and refs(home, o)]
        if not o1s or not o2s:
            return None
        o1, o2 = rng.choice(o1s), rng.choice(o2s)
        if o1.key == o2.key:
            return None
        tpl = rng.choice(MULTI)
        if "también" in tpl:
            i2 = i1
            if not sc.compatible(i2, o2):
                return None
        t1, t2 = rng.choice(refs(home, o1))[0], rng.choice(refs(home, o2))[0]
        text = tpl.replace("{V1}", VERB[i1].capitalize()).replace("{v2}", VERB[i2]).replace("{t1}", t1).replace("{t2}", t2)
        return dict(kind="multi", style="multi", actions=[dict(intent=i1, target=o1.key, value=None),
                                                          dict(intent=i2, target=o2.key, value=None)],
                    spoken=text, written=text, template=tpl)
    return None


STYLE_WEIGHTS = {  # commands (route execute) by style, then route-kiais kinds
    "direct": 14, "polite": 10, "short": 7, "word_order": 7, "disfluent": 8, "relative": 4, "latam": 9,
    "spain_colloquial": 7,
    "implicit": 3, "short_bare": 2, "unsupported": 4, "multi": 4,
    "general": 4, "live": 3, "chitchat": 3, "tv_dialogue": 5, "state": 4, "out_of_domain": 3, "missing_target": 4,
}

SCHEMA_MD = """# kivi-fake-v1 schema

Synthetic, no real home data. Generator: homelab `scripts/voice-assistant/decider_poc/fake_v1.py`
(templates) and `paraphrase.py` (local LLM paraphrases, validated).

`homes/<home>.yaml`: inventory in the shape of agent_eval/inventory_es.yaml (areas, floors,
entities with name, area, aliases, state, attributes; satellite_area). `homes/<home>.meta.json`:
grammatical gender of names (generator only).

`items.jsonl`, one JSON object per line:

| field | meaning |
| --- | --- |
| id | `<home>-<n>` (templates) or `<home>-<n>-p<k>` (paraphrase k of item n) |
| home | generica, coloquial, personas, latam |
| kind | command, unsupported, unclear, multi, question, other |
| route | `execute` if kind is command (one action of the decider's intent set), else `kiais` |
| style | direct, polite, short, word_order, disfluent, relative, latam, spain_colloquial, implicit, multi, general, live, chitchat, tv_dialogue, state, out_of_domain, missing_target |
| actions | list of {intent, target, value}: intent from decider_poc/schema.py INTENTS, target an option key of schema.target_options(home) (`e:<entity_id>`, `a:<area>:<light,cover,all>`, `h:light`) or null (timers), value: percent int, temperature float, duration seconds int, or null. One action for command, two for multi, empty otherwise |
| entities | for each action, the sorted entity ids the target resolves to |
| text | written form (digits), what a transcript would look like |
| spoken | the same with numbers as words (Piper input) |
| source | template or llm (qwen3:14b paraphrase, validated) |
| template | the template, or the source item id for a paraphrase |
| audio | true if the item is in the stratified audio subset (see stt_*.csv) |
"""


def main():
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--out", type=Path, required=True)
    ap.add_argument("--per-home", type=int, default=400)
    ap.add_argument("--seed", type=int, default=11)
    a = ap.parse_args()
    rng = random.Random(a.seed)
    (a.out / "homes").mkdir(parents=True, exist_ok=True)
    items = []
    for hid, spec in HOMES.items():
        home = build_home(hid, spec)
        inv = {k: v for k, v in home.items() if k not in ("gender", "id")}
        (a.out / "homes" / f"{hid}.yaml").write_text(yaml.safe_dump(inv, allow_unicode=True, sort_keys=False))
        (a.out / "homes" / f"{hid}.meta.json").write_text(json.dumps({"gender": home["gender"]}, ensure_ascii=False))
        opts = sc.target_options(home)
        by_key = {o.key: o for o in opts}
        seen, n, tries = set(), 0, 0
        styles, weights = list(STYLE_WEIGHTS), list(STYLE_WEIGHTS.values())
        while n < a.per_home and tries < a.per_home * 50:
            tries += 1
            st = rng.choices(styles, weights)[0]
            if st in NEG:
                it = negative(rng, home, st, opts)
            elif st in ("implicit", "short_bare", "unsupported", "multi"):
                it = other_kinds(rng, home, st, opts)
            elif st == "disfluent":
                it = disfluent(rng, home, opts)
            else:
                intents = [i for i, d in TPL.items() if st in d]
                intent = rng.choice(intents)
                it = fill(rng, home, intent, rng.choice(TPL[intent][st]), opts, st)
            if it is None or it["written"] in seen:
                continue
            seen.add(it["written"])
            it["entities"] = [sorted(by_key[x["target"]].entities) if x["target"] else [] for x in it["actions"]]
            it.update(id=f"{hid}-{n:04d}", home=hid, route="execute" if it["kind"] == "command" else "kiais",
                      source="template", text=it.pop("written"))
            items.append(it)
            n += 1
    order = ["id", "home", "kind", "route", "style", "actions", "entities", "text", "spoken", "source", "template"]
    with open(a.out / "items_template.jsonl", "w", encoding="utf-8") as fh:
        for it in items:
            fh.write(json.dumps({k: it[k] for k in order}, ensure_ascii=False) + "\n")
    (a.out / "SCHEMA.md").write_text(SCHEMA_MD)
    import collections
    print(len(items), "items", collections.Counter(i["kind"] for i in items))
    print(collections.Counter(i["style"] for i in items))
    for hid, spec in HOMES.items():
        h = build_home(hid, spec)
        print(hid, "entities", len(h["entities"]), "target options", len(sc.target_options(h)))


if __name__ == "__main__":
    main()
